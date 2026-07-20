"""Candidate serialization shared by per-job and campaign exports.

The per-job export routes (``/jobs/<id>/export.{csv,fasta,zip}``) and the new
campaign routes (``/campaigns/<id>/export.*``) produce the same three formats
over a list of candidate records — the only difference is that a campaign's
candidates come from many sub-jobs, so each carries a ``_source_job_id`` tag
(set by ``aggregate_campaign_candidates``) used to fetch its PDB and to
namespace it inside the ZIP. Keeping the serializers here means both paths stay
byte-for-byte identical and a bug is fixed once.

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
    stripped, legit sub-directories preserved, optionally namespaced."""
    cleaned = (name or "").replace("\\", "/").lstrip("/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".", "..")]
    safe = "/".join(parts) or "candidate.pdb"
    return f"{prefix}{safe}" if prefix else safe


def candidates_to_csv(candidates) -> str:
    """Rank + pdb_key + the union of every candidate's ``scores`` keys."""
    cands = _dict_candidates(candidates)
    all_score_keys: list[str] = []
    for cand in cands:
        for k in (cand.get("scores") or {}):
            if k not in all_score_keys:
                all_score_keys.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=["rank", "pdb_key"] + all_score_keys,
        extrasaction="ignore",
    )
    writer.writeheader()
    for i, cand in enumerate(cands):
        scores = cand.get("scores") or {}
        row = {"rank": cand.get("rank", i + 1), "pdb_key": cand.get("pdb_key", "")}
        row.update(scores)
        writer.writerow(row)
    return buf.getvalue()


def candidates_to_fasta(candidates, sequences=None) -> str:
    """FASTA body for a job/campaign. Binder-design tools carry a
    ``sequence`` / ``binder_sequence`` per candidate; MPNN's sequence-design
    output arrives as a separate ``sequences`` list (seq + score + recovery).
    Returns ``""`` when there is nothing to write (caller supplies the empty
    message so the download still names sensibly)."""
    lines: list[str] = []
    for i, cand in enumerate(_dict_candidates(candidates)):
        seq = cand.get("sequence") or cand.get("binder_sequence") or ""
        if not seq:
            continue
        pdb_key = cand.get("pdb_key", f"candidate_{i + 1}")
        rank = cand.get("rank", i + 1)
        lines.append(f">rank{rank}_{pdb_key}")
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
    than failing the archive. With ``namespace=True`` each entry is prefixed by
    its source sub-job (``chunk###/`` or ``<job8>/``) so identically-named
    designs from different sub-jobs don't collide.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, cand in enumerate(_dict_candidates(candidates)):
            pdb_key = cand.get("pdb_key") or f"candidate_{i + 1}.pdb"
            job_id = cand.get("_source_job_id") or default_job_id
            data = _decode_b64(cand.get("pdb_content_b64"))
            if data is None and job_id and cand.get("pdb_key"):
                data = fetch_bytes(job_id, cand["pdb_key"])
            if data is None:
                continue
            prefix = ""
            if namespace:
                chunk = cand.get("_source_chunk")
                if chunk is not None:
                    prefix = f"chunk{int(chunk):03d}/"
                elif job_id:
                    prefix = f"{str(job_id)[:8]}/"
            zf.writestr(_safe_arcname(pdb_key, prefix), data)
    buf.seek(0)
    return buf.read()
