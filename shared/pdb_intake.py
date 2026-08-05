"""PDB / preflight intake helpers for the tool run routes.

Extracted verbatim from ``app.py`` (blueprint refactor, Commit 0) so the
``/tools/<tool>/preflight`` and ``/tools/<tool>/submit`` handlers — and any
future ``tools`` blueprint that owns them — can import these without pulling
in the whole ``app`` module.

Pure, Flask-free functions built on the existing ``shared.pdb_inspect`` /
``shared.pdb_preflight`` leaf modules. The AlphaFold fetch is wired into the
preflight route and the ``alphafold:<accession>`` reuse-token path in
``tool_submit``.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Optional

from shared.pdb_inspect import (
    CifConversionError,
    InspectionReport,
    convert_cif_to_pdb_bytes,
    hotspot_range_message,
    inspect_pdb_bytes,
    validate_hotspots,
    validate_target_chain,
)
from shared.pdb_preflight import (
    PREFLIGHT_TOOLS,
    PreflightVerdict,
    preflight_for_tool,
)
from shared.uniprot_lookup import alphafold_api_url

logger = logging.getLogger(__name__)

# Match the UniProt accession format that uniprot_lookup uses. Kept local
# so the helper stays a one-liner and we don't have to expose yet another
# private regex from the vendored module.
_AF_ACCESSION_RE = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})"
    r"(?:-\d+)?$"
)


def _fetch_alphafold_bytes(accession: str) -> Optional[bytes]:
    """Fetch the latest AlphaFold-DB PDB for a UniProt accession.

    Two-hop: first hit the prediction API to pin the current model URL
    (v4/v5/v6 vary across entries), then GET the PDB file. Returns the
    bytes on success, ``None`` on any failure (the caller surfaces a
    "couldn't fetch" message to the user).
    """
    if not _AF_ACCESSION_RE.match(accession or ""):
        return None
    import requests  # noqa: PLC0415
    try:
        api = requests.get(
            alphafold_api_url(accession),
            timeout=8,
            headers={"User-Agent": "ranomics-tools-hub/preflight"},
        )
    except Exception as exc:  # noqa: BLE001 - any network failure
        logger.warning("alphafold fetch metadata failed for %s: %s",
                       accession, exc)
        return None
    if api.status_code != 200:
        logger.info("alphafold metadata %s returned HTTP %d",
                    accession, api.status_code)
        return None
    try:
        meta_list = api.json()
        pdb_url = meta_list[0]["pdbUrl"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("alphafold metadata %s shape unexpected: %s",
                       accession, exc)
        return None
    try:
        pdb = requests.get(
            pdb_url, timeout=20,
            headers={"User-Agent": "ranomics-tools-hub/preflight"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("alphafold fetch pdb failed for %s: %s",
                       accession, exc)
        return None
    if pdb.status_code != 200:
        logger.info("alphafold pdb fetch %s returned HTTP %d",
                    accession, pdb.status_code)
        return None
    return pdb.content


def _verdict_to_json(verdict: PreflightVerdict, source_label: str) -> dict:
    """Project a PreflightVerdict into the JSON shape the panel JS expects."""
    af = None
    if verdict.alphafold is not None:
        af = {
            "accession": verdict.alphafold.uniprot_accession,
            "display_id": verdict.alphafold.display_id,
            "reuse_token": f"alphafold:{verdict.alphafold.uniprot_accession}",
        }
    gap_block = None
    if verdict.gap_analysis is not None and (
        verdict.gap_analysis.gaps
        or verdict.gap_analysis.warn_message
        or verdict.gap_analysis.hard_fail_message
    ):
        import math as _math
        gap_block = {
            "longest_gap": verdict.gap_analysis.longest_gap,
            "causes_hard_fail": verdict.gap_analysis.causes_hard_fail,
            "warn_message": verdict.gap_analysis.warn_message,
            "hard_fail_message": verdict.gap_analysis.hard_fail_message,
            "gaps": [
                {
                    "start": g.start,
                    "end": g.end,
                    "length": g.length,
                    "nearest_hotspot_distance": (
                        None
                        if g.nearest_hotspot_distance == _math.inf
                        else g.nearest_hotspot_distance
                    ),
                }
                for g in verdict.gap_analysis.gaps
            ],
        }
    size_block = None
    if verdict.size_envelope is not None:
        size_block = {
            "residue_count": verdict.size_envelope.residue_count,
            "hard_cap_target_aa": verdict.size_envelope.hard_cap_target_aa,
            "soft_warn_target_aa": verdict.size_envelope.soft_warn_target_aa,
            "hard_cap_combined_aa": verdict.size_envelope.hard_cap_combined_aa,
            "binder_max_aa": verdict.size_envelope.binder_max_aa,
            "combined_aa": verdict.size_envelope.combined_aa,
            "over_soft_warn": verdict.size_envelope.over_soft_warn,
            "over_hard_cap": verdict.size_envelope.over_hard_cap,
            "over_combined_cap": verdict.size_envelope.over_combined_cap,
            "runtime_estimate_min": (
                None
                if verdict.size_envelope.runtime_estimate_min is None
                else round(verdict.size_envelope.runtime_estimate_min, 1)
            ),
            "runtime_basis": verdict.size_envelope.runtime_basis,
            "gpu": verdict.size_envelope.gpu,
            "warn_message": verdict.size_envelope.warn_message,
            "hard_fail_message": verdict.size_envelope.hard_fail_message,
        }
    return {
        "kind": verdict.kind.value,
        "ok": verdict.ok,
        "tool_slug": verdict.tool_slug,
        "target_chain": verdict.target_chain,
        "source_label": source_label,
        "cleanup_items": list(verdict.cleanup.items),
        "residues_kept_on_target_chain":
            verdict.cleanup.residues_kept_on_target_chain,
        "hotspots": {
            "surviving": list(verdict.hotspot_status.get("surviving", [])),
            "dropped": list(verdict.hotspot_status.get("dropped", [])),
        },
        "reason": verdict.reason,
        "suggested_fix": verdict.suggested_fix,
        "alphafold": af,
        "nearest_clean_residues": list(verdict.nearest_clean_residues),
        "gap_analysis": gap_block,
        "size_envelope": size_block,
    }


def _parse_preflight_size_params(source) -> tuple[Optional[int], Optional[int]]:
    """Extract (binder_max_aa, num_designs) from a request.form-like mapping.

    Both are optional — when absent or unparseable, return (None, None) so
    preflight_for_tool skips the runtime estimate + combined-budget cap
    rather than firing on garbage. Used by both /preflight (request.form)
    and tool_submit (the validated ``inputs`` dict; .get works for both).
    """
    def _maybe_int(v) -> Optional[int]:
        if v is None or v == "":
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    # Binder size for the combined-complex cap. Tools name the field
    # differently: the binder-design forms use ``binder_length_max``;
    # pxdesign uses ``binder_length``; boltz2 carries ``binder_sequences``
    # (a list of {name, sequence}) and we take the longest.
    binder_max = _maybe_int(source.get("binder_length_max"))
    if binder_max is None:
        seqs = source.get("binder_sequences")
        if isinstance(seqs, list) and seqs:
            lengths = [
                len(s.get("sequence", ""))
                for s in seqs
                if isinstance(s, dict) and s.get("sequence")
            ]
            if lengths:
                binder_max = max(lengths)
    if binder_max is None:
        raw_len = source.get("binder_length")
        # proteina's VALIDATED inputs carry binder_length as a two-element
        # [min, max] list, not a scalar. _maybe_int(list) returned None, so on
        # the submit path binder_max was always None and the combined-budget
        # cap never evaluated — it fired only on the AJAX panel, where the raw
        # form supplies binder_length_max as a string. The hard gate was the
        # one that did not have it.
        #
        # THE FOURTH SHAPE, and the one that kept the campaign routes blind.
        # rfdiffusion's validator emits ``binder_length`` as a {min, max} DICT
        # (tools/rfdiffusion/__init__.py::_parse_binder_length). Neither the
        # list branch nor the scalar branch reads a dict, so rfdiffusion
        # returned None here — and once the money routes started calling this
        # helper, None is silently "no combined cap" rather than a failure. The
        # four live shapes are now: dict {min,max} (rfdiffusion), [min,max]
        # list (proteina), scalar int (pxdesign), and a separate
        # ``binder_length_max`` key handled above (boltzgen, bindcraft).
        # rfantibody has no binder length at all — CDR lengths instead — and
        # correctly yields None.
        if isinstance(raw_len, dict):
            binder_max = _maybe_int(raw_len.get("max"))
        elif isinstance(raw_len, (list, tuple)) and raw_len:
            binder_max = _maybe_int(raw_len[-1])
        else:
            binder_max = _maybe_int(raw_len)

    num_designs = _maybe_int(source.get("num_designs"))
    if num_designs is None:
        # Same shape problem for the design count: proteina's validated inputs
        # put it under designs_per_shard / parameters.n_designs_total, so the
        # runtime estimate never rendered on submit either.
        num_designs = _maybe_int(source.get("designs_per_shard"))
    if num_designs is None:
        params = source.get("parameters")
        if isinstance(params, dict):
            num_designs = _maybe_int(params.get("n_designs_total"))

    return (binder_max, num_designs)


def preflight_target_segments(source) -> Optional[list]:
    """The chain/residue contig a run will design against, or None.

    Two shapes reach preflight and both have to resolve to the same thing, or
    the advisory panel and the hard gate would size different runs:

      * ``_target_segments`` — already-parsed ``[(chain, lo, hi), ...]`` from an
        adapter's validator. This is what ``tool_submit`` has.
      * ``target_input`` — the raw contig string the user typed, which is what
        the AJAX ``/preflight`` route has.

    The raw string is parsed by the ADAPTER'S OWN parser via a lazy import, not
    by a second copy living here. A duplicate regex would drift, and the half
    that drifts is whichever one is not the money gate. ``shared`` already
    reaches into ``tools`` this way (compute_campaigns.py:1616).

    Returns None when nothing was declared, which the size envelope reads as
    the whole-chain default — the conservative direction, since counting whole
    chains can only over-count relative to a selection.
    """
    segments = source.get("_target_segments")
    if segments:
        return list(segments)

    raw = source.get("target_input")
    if not raw or not str(raw).strip():
        return None
    try:
        from tools.proteina import parse_target_segments  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - never break preflight over this
        return None
    try:
        return parse_target_segments(str(raw))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Target upload intake
# ---------------------------------------------------------------------------
# Shared by the run-create route and the target routes. Both take the same
# uploaded file and have to do the same four things before any of it reaches
# Storage: inspect it, validate the user-typed target chain against what is
# actually in the structure, convert mmCIF to PDB (the GPU pipelines only read
# PDB), and cheap-check an SDF. That block used to live inline in
# ``blueprints/campaigns.py``; a target can now be created from either route,
# so it lives here and the two cannot drift into validating uploads
# differently.

# proteina's ligand_binder variant is the only SDF consumer, and its real
# parse (RDKit sanitize + the SDF -> chain-A HETATM/CONECT PDB conversion)
# runs in-container because RDKit is not installed in the web tier. So this
# cap only has to stop obvious junk from being staged.
MAX_SDF_BYTES = 2_000_000


def sdf_sanity(data: bytes, filename: str) -> Optional[str]:
    """Cheap pre-GPU check that an uploaded SDF is a plausible molfile.

    Returns a user-facing error string, or None when the file passes. Only
    rejects obvious junk: a size bound plus a parseable V2000/V3000 counts
    line declaring at least one atom.
    """
    if len(data) > MAX_SDF_BYTES:
        return "SDF file is too large (max 2 MB)."
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if len(lines) < 4:
        return "SDF file is too short to be a valid molfile."
    counts = lines[3]
    if "V3000" in counts:
        natoms = None
        for ln in lines:
            if "V30 COUNTS" in ln:
                parts = ln.split()
                try:
                    natoms = int(parts[parts.index("COUNTS") + 1])
                except (ValueError, IndexError):
                    natoms = None
                break
        if not natoms or natoms < 1:
            return "SDF has no atoms (empty V3000 molfile)."
    else:
        try:
            natoms = int(counts[:3])
        except ValueError:
            return "SDF counts line is malformed (not a V2000/V3000 molfile)."
        if natoms < 1:
            return "SDF has no atoms (empty molfile)."
    return None


def chain_summary_json(report: Optional[InspectionReport]) -> Optional[dict]:
    """JSON-serializable view of an inspection, for ``design_targets.chain_summary``.

    Persisted so the target page and the launch form can offer chain choices
    and residue ranges without re-downloading and re-parsing the structure on
    every render.
    """
    if report is None or not report.ok:
        return None
    return {
        "model_count": report.model_count,
        "total_standard_residues": report.total_standard_residues,
        "total_hetatm_residues": report.total_hetatm_residues,
        "total_water_residues": report.total_water_residues,
        "warnings": list(report.warnings or []),
        "chains": [
            {
                "chain_id": c.chain_id,
                "standard_residue_count": c.standard_residue_count,
                "hetatm_resnames": list(c.hetatm_resnames or []),
                "water_count": c.water_count,
                "min_resnum": c.min_resnum,
                "max_resnum": c.max_resnum,
            }
            for c in (report.chains or [])
        ],
    }


@dataclass(frozen=True)
class TargetUpload:
    """A validated upload, ready to stage.

    ``data`` / ``filename`` are post-conversion: an mmCIF arrives as ``.cif``
    and leaves as PDB bytes under a ``.pdb`` name, because everything
    downstream (Storage, the presigned URL, the container) assumes PDB.
    ``sha256`` is over those staged bytes, so two uploads of the same
    structure hash alike even when one of them arrived as CIF.
    """

    data: bytes
    filename: str
    content_type: str
    kind: str
    sha256: str
    inspection: Optional[InspectionReport] = None

    @property
    def chain_summary(self) -> Optional[dict]:
        return chain_summary_json(self.inspection)


def resolve_target_upload(
    uploaded,  # noqa: ANN001 - werkzeug FileStorage
    *,
    target_chain: str = "",
    kind: str = "pdb",
) -> "tuple[Optional[TargetUpload], Optional[str]]":
    """Validate and normalize an uploaded target file.

    Args:
        uploaded: The werkzeug ``FileStorage``. Callers gate "was anything
            attached" themselves, because whether a missing file is an error
            is per-tool (proteina's curated-task path has no target at all).
        target_chain: The chain the caller's validated params name, if any.
            Checked against the structure so a typo is caught before GPU
            spend rather than after it. Empty means "do not check".
        kind: ``"pdb"`` or ``"sdf"``.

    Returns:
        ``(upload, None)`` on success, ``(None, error_message)`` on rejection.
        Never raises: a CIF that will not convert comes back as an error
        string, not a ``CifConversionError``.
    """
    if uploaded is None or not getattr(uploaded, "filename", ""):
        return None, "Upload a target file."

    raw = uploaded.read()
    if not raw:
        return None, "The uploaded file is empty."

    if kind == "sdf":
        err = sdf_sanity(raw, uploaded.filename)
        if err:
            return None, err
        return TargetUpload(
            data=raw,
            filename=uploaded.filename,
            content_type="chemical/x-mdl-sdfile",
            kind="sdf",
            sha256=hashlib.sha256(raw).hexdigest(),
        ), None

    inspection = inspect_pdb_bytes(raw, filename=uploaded.filename)
    if not inspection.ok:
        return None, inspection.error

    # Validate the chain against the ORIGINAL parse. Conversion below only
    # rewrites the container format and never the chain identifiers, so
    # checking here or after is equivalent — but here a bad chain is rejected
    # without paying for the conversion.
    chain = (target_chain or "").strip()
    if chain:
        chain_err = validate_target_chain(inspection, chain)
        if chain_err:
            return None, chain_err

    data = raw
    filename = uploaded.filename
    lowered = filename.lower()
    if lowered.endswith(".cif") or lowered.endswith(".mmcif"):
        try:
            data = convert_cif_to_pdb_bytes(raw, filename)
        except CifConversionError as exc:
            return None, str(exc)
        filename = filename.rsplit(".", 1)[0] + ".pdb"

    return TargetUpload(
        data=data,
        filename=filename,
        content_type="chemical/x-pdb",
        kind="pdb",
        sha256=hashlib.sha256(data).hexdigest(),
        inspection=inspection,
    ), None


def _verify_reuse_pdb_bytes(
    adapter,
    pdb_bytes: bytes,
    *,
    target_chain: str,
    hotspots: list,
    filename: str,
    binder_max_aa: Optional[int] = None,
    num_designs: Optional[int] = None,
    target_segments: Optional[list] = None,
) -> Optional[str]:
    """Re-run the upload gate on resolved reuse/handoff/resample bytes.

    Fresh uploads are inspected + gated at the upload boundary, but the
    reuse-token paths (job:/handoff:/resample:) stage bytes that
    skipped both. This mirrors that gate (inspect + chain/hotspot
    validation + per-tool hard-gate preflight) so a mismatch is caught
    upfront instead of crashing on the GPU. Reuse bytes are already PDB,
    so no CIF conversion is needed. Returns None when fit to ship, else an
    actionable error string. Never raises.
    """
    inspection = inspect_pdb_bytes(pdb_bytes, filename=filename)
    if not inspection.ok:
        return inspection.error or "The reused structure could not be read as PDB."
    tc = (target_chain or "").strip()
    if tc:
        chain_err = validate_target_chain(inspection, tc)
        if chain_err:
            return chain_err
        # boltz2 hotspots are 1-indexed sequence positions, range-checked
        # by position in its own preflight, not original PDB numbering.
        if hotspots and adapter.slug != "boltz2":
            _, out_of_range = validate_hotspots(inspection, tc, hotspots)
            if out_of_range:
                return hotspot_range_message(inspection, tc, out_of_range)
    if adapter.slug in PREFLIGHT_TOOLS:
        try:
            verdict = preflight_for_tool(
                adapter.slug, pdb_bytes,
                target_chain=tc, hotspots=hotspots or [],
                binder_max_aa=binder_max_aa, num_designs=num_designs,
                target_segments=target_segments,
            )
        except Exception:
            logger.exception(
                "reuse preflight unexpected error tool=%s", adapter.slug,
            )
            verdict = None
        if verdict is not None and not verdict.ok:
            msg = verdict.reason or "This reused target can't run as-is."
            if verdict.suggested_fix:
                msg = f"{msg} {verdict.suggested_fix}"
            return msg
    return None
