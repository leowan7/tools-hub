"""Candidate serialization shared by per-job and campaign exports.

The per-job export routes (``/jobs/<id>/export.{csv,fasta,zip}``) and the new
campaign routes (``/campaigns/<id>/export.*``) produce the same three formats
over a list of candidate records — the only difference is that a campaign's
candidates come from many sub-jobs, so each carries a ``_source_job_id`` tag
(set by ``aggregate_campaign_candidates``) used to fetch its PDB and to
namespace it inside the ZIP. Keeping the serializers here means both paths stay
byte-for-byte identical and a bug is fixed once.

All three serializers take their leading columns, FASTA ids, and ZIP prefixes
from :func:`export_key`, so a row's identity is the same whichever format the
user downloads. That matters most once an export merges several tools over one
target: ``rank`` and ``pdb_key`` are no longer unique on their own there, since
every tool emits a rank 1 and a ``design_1.pdb``.

These are pure functions over candidate dicts; the ZIP builder takes a
``fetch_bytes(job_id, filename)`` callback so this module never imports the
Storage layer (and stays trivially testable).
"""

from __future__ import annotations

import base64
import csv
import io
import zipfile
from typing import Callable, Optional

from shared import metric_glossary as _metric_glossary
from shared import pdb_bfactors as _pdb_bfactors


def _dict_candidates(candidates) -> list:
    return [c for c in (candidates or []) if isinstance(c, dict)]


def _decode_b64(encoded) -> Optional[bytes]:
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except Exception:
        return None


def _safe_arcname(name: str, prefix: str = "") -> str:
    """A ZIP entry name with any traversal (``..``, absolute, backslash)
    stripped, legit sub-directories preserved, optionally namespaced.

    Only ``name`` is cleaned here. ``prefix`` is trusted and must already be
    built from :func:`_safe_component` segments — see :func:`candidates_to_zip`,
    the only caller that passes one.
    """
    cleaned = (name or "").replace("\\", "/").lstrip("/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".", "..")]
    safe = "/".join(parts) or "candidate.pdb"
    return f"{prefix}{safe}" if prefix else safe


def _safe_component(value, fallback: str = "unknown") -> str:
    """One ZIP path segment, with separators and traversal removed.

    A namespace prefix is interpolated into the arcname rather than passed
    through :func:`_safe_arcname`'s cleaner, so anything that reaches a prefix
    has to be made safe here. Today's inputs cannot contain a separator (a tool
    slug comes from the adapter registry and a job id is a uuid), so this is
    defence in depth rather than a fix: the traversal guard belongs with the
    function whose job is traversal safety, not with the caller that happens to
    supply clean values.
    """
    text = str(value or "").replace("\\", "/")
    segment = "".join(p for p in text.split("/") if p not in ("", ".", ".."))
    return segment or fallback


# Provenance the aggregator stamps onto every merged candidate, mapped to the
# column name it exports under. Ordered: these lead the CSV, ahead of pdb_key.
# A key absent from every candidate (a single-job export has no source job; a
# single-campaign export has no tool column) is omitted rather than exported
# blank, so each surface's CSV carries exactly the provenance it actually has.
_PROVENANCE_COLUMNS = (
    ("tool", "_source_tool"),
    ("campaign_id", "_source_campaign_id"),
    ("source_job", "_source_job_id"),
    ("source_chunk", "_source_chunk"),
)


def export_key(cand: dict, i: int) -> dict:
    """The provenance block for one exported row, at global index ``i``.

    ``rank`` is the row's 1-based index in the CANDIDATE LIST handed to the
    serializer. For the CSV that is also its position in the file. For the FASTA
    it is not: :func:`candidates_to_fasta` skips rows carrying no sequence but
    still numbers from the full list, so a target whose best design is a
    backbone with no sequence produces a file whose first record is ``rank2``.
    The numbers are monotonic and unique either way, which is what the ids need
    them for; they are not a count of the file's own records.

    It is NOT a cross-surface identifier. This docstring used to claim it
    "matches the on-screen order of the merged table", which holds only while
    the page is uncapped: the target page ranks with
    :data:`shared.ranking.DEFAULT_LIMIT` and these files rank the whole set, so
    a row the page numbers 298 can be rank 377 here whenever
    :func:`shared.ranking.select_under_cap`'s per-tool floor reserved it from
    beyond the cap, which is the very case the floor exists for. ``source_job``
    plus ``pdb_key`` identify a design across both surfaces; the rank does not.

    The tool's own rank is demoted to
    ``source_rank``: across a merged export those collide (every tool emits a
    rank 1), so using it as the export rank made the CSV look shuffled and made
    "row 7" ambiguous. ``pdb_key`` collides the same way (every tool emits
    ``design_1.pdb``), which is why the source job and chunk travel beside it.

    All three serializers derive from this one function so the CSV, the FASTA
    ids, and the ZIP entry names cannot disagree about where a row came from.
    """
    key: dict = {"rank": i + 1}
    for column, source in _PROVENANCE_COLUMNS:
        value = cand.get(source)
        if value is not None:
            key[column] = value
    key["pdb_key"] = cand.get("pdb_key", "")
    key["source_rank"] = cand.get("rank", i + 1)
    return key


def _export_keys(cands: list) -> tuple[list[dict], list[str]]:
    """Per-row provenance plus the leading column names actually present."""
    keys = [export_key(c, i) for i, c in enumerate(cands)]
    leading = ["rank"]
    for column, _ in _PROVENANCE_COLUMNS:
        if any(column in k for k in keys):
            leading.append(column)
    leading += ["pdb_key", "source_rank"]
    return keys, leading


def _basename(pdb_key: str, fallback: str) -> str:
    """Last path segment of a pdb_key. Keys arrive as ``designs/design_0.pdb``
    for most tools; a ``/`` inside a FASTA id terminates parsing in several
    downstream tools, so the prefix is stripped rather than escaped."""
    tail = (pdb_key or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    # Whitespace ends a FASTA id and turns the rest into a free-text
    # description, which would silently truncate the provenance we just added.
    return "_".join(tail.split()) or fallback


# Root-level keys that are never metrics: identity, provenance, bulk payloads.
# Everything else scalar at the root IS exported (see _metric_columns).
# The verdict a pipeline stamped, which is NOT a metric and is no longer true
# of anything. Excluded from BOTH loops in _metric_columns -- a CSV is the one
# copy of a result that leaves this site, and shipping "below threshold" beside
# the measurements would hand a customer a file contradicting the page they
# downloaded it from, on candidates whose bar has since moved.
#
# EXCEPT WHEN IT IS THE PROVENANCE MARKER. The same field carries "stub
# (smoke)", which says the numbers beside it were fabricated rather than
# measured. Stripping that shipped a CSV of invented values with nothing
# saying so, while the page beside it said "scores fabricated" -- the same
# page-versus-export disagreement, pointing the other way. See
# ``_keep_verdict_key``.
_STALE_VERDICT_KEYS = frozenset({"filter_status"})


def _keep_verdict_key(cands: list, key: str) -> bool:
    """Is ``key`` worth exporting despite being a stored verdict field?

    Only for the fabrication marker, and only when a row actually carries it,
    so an ordinary export gains no empty column.
    """
    from shared.score_legends import is_fabricated  # noqa: PLC0415

    return key == "filter_status" and any(is_fabricated(c) for c in cands)


_NON_METRIC_ROOT_KEYS = frozenset({
    "pdb_key", "name", "rank", "scores",
    "sequence", "binder_sequence", "designed_sequence",
    "pdb_content_b64", "pdb_content", "cif_content_b64",
})


def _metric_columns(cands: list, leading: list[str]) -> list[str]:
    """Metric column names, in first-seen order.

    Reads ``scores`` AND the record root. The designs-shape pipelines (boltz2,
    af2, colabfold, esmfold, iggm, opendde) put every metric at the root and
    have no ``scores`` dict at all — their results templates reshape inline, so
    the screen looked fine while a scores-only export produced a file with the
    row count right and every metric missing.

    Root names are the pipeline's own (``iptm``, not ``ipTM``); mapping those
    onto canonical display names is a per-tool concern and belongs with the
    cross-tool aliasing work, not here. Exporting the real numbers under their
    real names beats exporting nothing.
    """
    out: list[str] = []
    stale = {k for k in _STALE_VERDICT_KEYS if not _keep_verdict_key(cands, k)}
    for cand in cands:
        for k in (cand.get("scores") or {}):
            if k not in out and k not in leading and k not in stale:
                out.append(k)
    for cand in cands:
        for k, v in cand.items():
            if k in stale:
                continue
            if k in out or k in leading or k in _NON_METRIC_ROOT_KEYS:
                continue
            if k.startswith("_"):          # provenance tags
                continue
            if not _is_metric_value(v):
                continue
            out.append(k)
    return out


# No metric is a long string. The cap is insurance against a pipeline putting a
# bulk payload (structure text, a base64 blob) under a key not in the denylist:
# without it one such key would inline megabytes into every CSV row.
_MAX_METRIC_STR = 512


def _is_metric_value(v) -> bool:
    """Scalar, and small enough to belong in a spreadsheet cell."""
    if v is None or isinstance(v, (int, float, bool)):
        return True
    if isinstance(v, str):
        return len(v) <= _MAX_METRIC_STR
    return False                           # lists/dicts (per-residue contacts)


def candidates_to_csv(candidates) -> str:
    """Provenance columns (:func:`export_key`) + every metric found."""
    cands = _dict_candidates(candidates)
    keys, leading = _export_keys(cands)
    all_score_keys = _metric_columns(cands, leading)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=leading + all_score_keys, extrasaction="ignore",
    )
    writer.writeheader()
    for cand, key in zip(cands, keys):
        # Root metrics first, then scores (which win, matching how
        # candidate_metric resolves), then provenance (which wins outright).
        # A key can be scalar on one candidate and a list on another, so the
        # value is re-checked here and not just at column-discovery time.
        row = {
            k: cand[k] for k in all_score_keys
            if k in cand and _is_metric_value(cand[k])
        }
        row.update(cand.get("scores") or {})
        row.update(key)
        # Same scale as the page this was downloaded from. Without this
        # the workflow "read the table, export the CSV, filter > 70"
        # returns an empty file for every tool that stores 0-1, with no
        # hint why -- the page says 88.5 and the CSV says 0.885.
        for col in _metric_glossary.PLDDT_COLUMNS & row.keys():
            scaled = _metric_glossary.plddt_on_100(row[col])
            # Rounded, because 0.8728 * 100 is 86.24000000000001 and the
            # page shows 86.24. Fixing a 100x disagreement and opening a
            # 1e-14 one is not fixing it.
            row[col] = scaled if scaled is None else round(scaled, 2)
        writer.writerow(row)
    return buf.getvalue()


def candidates_to_fasta(candidates, sequences=None) -> str:
    """FASTA body for a job/campaign. Binder-design tools carry a
    ``sequence`` / ``binder_sequence`` per candidate; MPNN's sequence-design
    output arrives as a separate ``sequences`` list (seq + score + recovery).
    Returns ``""`` when there is nothing to write (caller supplies the empty
    message so the download still names sensibly)."""
    lines: list[str] = []
    cands = _dict_candidates(candidates)
    for i, cand in enumerate(cands):
        seq = cand.get("sequence") or cand.get("binder_sequence") or ""
        if not seq:
            continue
        key = export_key(cand, i)
        # rank{global}_{tool}_{job8}_{basename}: unique across a merged export,
        # where the old rank+pdb_key pair was not (every tool emits a rank 1
        # and a design_1.pdb). Segments absent from this export are omitted.
        parts = [f"rank{key['rank']}"]
        if key.get("tool"):
            parts.append(str(key["tool"]))
        if key.get("source_job"):
            parts.append(str(key["source_job"])[:8])
        parts.append(_basename(key["pdb_key"], f"candidate_{i + 1}"))
        lines.append(">" + "_".join(parts))
        for start in range(0, len(seq), 80):
            lines.append(seq[start:start + 80])
    for i, seq_obj in enumerate(sequences or []):
        if not isinstance(seq_obj, dict):
            continue
        seq = seq_obj.get("seq") or ""
        if not seq:
            continue
        header_parts = [f">mpnn_rank{i + 1}"]
        score = seq_obj.get("score")
        recovery = seq_obj.get("recovery")
        if score is not None:
            header_parts.append(f"score={score}")
        if recovery is not None:
            header_parts.append(f"recovery={recovery}")
        lines.append(" ".join(header_parts))
        for start in range(0, len(seq), 80):
            lines.append(seq[start:start + 80])
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def candidates_to_zip(
    candidates,
    fetch_bytes: Callable[[str, str], Optional[bytes]],
    *,
    default_job_id: Optional[str] = None,
    namespace: bool = False,
) -> bytes:
    """Bundle candidate PDBs into a ZIP (bytes).

    Per candidate, try inline ``pdb_content_b64`` first, then
    ``fetch_bytes(source_job_id, pdb_key)`` (Storage). ``source_job_id`` is the
    candidate's ``_source_job_id`` (campaign merge) or ``default_job_id``
    (single job). Candidates that resolve via neither path are skipped rather
    than failing the archive.

    With ``namespace=True`` each entry is prefixed so identically-named designs
    from different sources do not collide. Which prefix depends on the
    provenance the rows actually carry:

    * ``<tool>/<job8>/`` when the row carries ``_source_tool``, which only the
      TARGET aggregate stamps. Chunk index is not enough there: every campaign
      starts at chunk 0, so a bindcraft and a boltzgen ``chunk000/design_1.pdb``
      would be one arcname and one design would silently overwrite the other.
      The job id rather than the chunk is the second segment because two
      campaigns of the SAME tool on one target both have a chunk 0 too.
    * ``chunk###/`` or ``<job8>/`` otherwise, which is every campaign and
      single-job export. Gating on ``_source_tool`` is what keeps those
      byte-identical to what they produced before this branch existed.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, cand in enumerate(_dict_candidates(candidates)):
            key = export_key(cand, i)
            pdb_key = key["pdb_key"] or f"candidate_{i + 1}.pdb"
            job_id = key.get("source_job") or default_job_id
            data = _decode_b64(cand.get("pdb_content_b64"))
            if data is None and job_id and key["pdb_key"]:
                data = fetch_bytes(job_id, key["pdb_key"])
            if data is None:
                continue
            # One conversion covers the job, campaign and target ZIP
            # routes, which all come through here. Same whole-file gate
            # as every other download: a structure that is not a
            # fractional confidence is archived untouched.
            data = _pdb_bfactors.bfactors_on_100_bytes(data)
            prefix = ""
            if namespace:
                tool = key.get("tool")
                chunk = key.get("source_chunk")
                if tool:
                    tool_seg = _safe_component(tool, "unknown-tool")
                    job_seg = _safe_component(str(job_id or "")[:8], "unknown-job")
                    prefix = f"{tool_seg}/{job_seg}/"
                elif chunk is not None:
                    prefix = f"chunk{int(chunk):03d}/"
                elif job_id:
                    prefix = f"{str(job_id)[:8]}/"
            zf.writestr(_safe_arcname(pdb_key, prefix), data)
    buf.seek(0)
    return buf.read()
