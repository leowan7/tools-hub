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

import logging
import re
from typing import Optional

from shared.pdb_inspect import (
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
        binder_max = _maybe_int(source.get("binder_length"))

    return (binder_max, _maybe_int(source.get("num_designs")))


def _verify_reuse_pdb_bytes(
    adapter,
    pdb_bytes: bytes,
    *,
    target_chain: str,
    hotspots: list,
    filename: str,
    binder_max_aa: Optional[int] = None,
    num_designs: Optional[int] = None,
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
