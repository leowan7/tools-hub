"""ESMFold standalone (D4) — atomic primitive (single-sequence fold, no MSA).

Modal app: ``ranomics-esmfold-prod``. GPU: A100-40GB.
Spec lives in ``docs/ATOMIC-TOOLS.md`` under D4. Sibling of D3 (ColabFold
standalone) — also a no-MSA fold tool, but uses Meta's ESM-2 language
model instead of AF2 weights. Monomer-only (ESMFold v1 does not support
multimers — unlike AF2 or ColabFold).

The user pastes a FASTA (single chain only), and receives a predicted
structure (PDB b64) plus per-residue pLDDT. 1-credit tool per
PRODUCT-PLAN.md "Credit rates" table.

A single ``standalone`` tier takes a caller-supplied FASTA (inline text
field, no file upload required). 1 credit. Single-chain monomers
10-400 aa (ESMFold's 3B model fits comfortably on A100-40GB at this
length).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from shared.sequence_parsing import (
    CANONICAL_AA as SHARED_CANONICAL_AA,
    find_non_canonical_residues,
    parse_fasta_or_lines,
)
from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds (also enforced on the pipeline side for direct ``modal run`` use).
# ---------------------------------------------------------------------------

SEQ_LEN_MIN = 10
SEQ_LEN_MAX = 400  # matches run_pipeline.SEQ_LEN_MAX — A100-40GB / ESMFold-3B
CANONICAL_AA = set(SHARED_CANONICAL_AA)
# Batch preset ceiling. Sequential per-fold at ~5 s on a warm A100-40GB,
# 500 records fits inside the 3600 s Modal session budget with ~10 min of
# headroom for the cold weight load + tail-end uploads.
MAX_BATCH = 500


def _parse_fasta_text(raw: str) -> tuple[list[tuple[str, str]], str]:
    """Minimal FASTA text parser.

    Returns ``(records, error)``. ``records`` is a list of
    ``(header, seq)`` tuples. On parse failure ``records`` is empty and
    ``error`` is non-empty.

    Accepts single-chain FASTA (one ``>header`` + sequence) and
    multi-chain FASTA (multiple records). A bare (headerless) sequence
    is treated as a single unnamed chain for convenience.
    """
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if not lines:
        return [], "FASTA is empty"
    if not lines[0].startswith(">"):
        # Accept a bare sequence — treat as a single unnamed chain.
        seq = "".join(lines).upper()
        return [("query", seq)], ""

    records: list[tuple[str, str]] = []
    header: str | None = None
    buf: list[str] = []
    for ln in lines:
        if ln.startswith(">"):
            if header is not None:
                records.append((header, "".join(buf).upper()))
            header = ln[1:].strip() or f"chain_{len(records) + 1}"
            buf = []
        else:
            buf.append(ln)
    if header is not None:
        records.append((header, "".join(buf).upper()))

    if not records:
        return [], "FASTA has no records"
    return records, ""


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the ESMFold job_spec shape.

    Two presets:

    - ``standalone`` (default): caller-supplied single-chain FASTA.
      Monomer only — multimer inputs (multiple ``>`` records OR a ``:``
      chain separator) are rejected because ESMFold v1 does not support
      multimers.
    - ``batch``: caller-supplied list of monomer sequences (FASTA records
      or one-per-line). Each record is folded independently and shipped
      back through the partial-results contract. Per-record bounds match
      the standalone tier (10–400 aa, 20 aa + X alphabet).

    A missing or blank preset is treated as ``standalone`` so the form's
    hidden preset field is robust.
    """
    preset = (form.get("preset") or "standalone").strip() or "standalone"
    if preset not in {"standalone", "batch"}:
        return None, "Pick a preset (standalone or batch)."

    if preset == "batch":
        return _validate_batch(form)

    # standalone tier — caller-supplied FASTA.
    fasta_text = (form.get("fasta_text") or "").strip()
    if not fasta_text:
        return None, "Paste a FASTA (>header + sequence) for the standalone tier."

    records, parse_err = _parse_fasta_text(fasta_text)
    if parse_err:
        return None, f"FASTA parse: {parse_err}"
    if not records:
        return None, "FASTA produced zero records."

    # ESMFold v1 is monomer-only — reject multi-record FASTA.
    if len(records) > 1:
        return None, (
            f"ESMFold v1 is monomer-only but FASTA has {len(records)} records. "
            "Remove extra chains or use ColabFold (D3) / AF2 (D2) for multimers."
        )

    header, seq = records[0]
    if not seq:
        return None, f"record {header!r} has no sequence."

    # Reject ':' chain separator (ColabFold multimer convention) — also a multimer.
    if ":" in seq:
        return None, (
            "ESMFold v1 is monomer-only but the sequence contains ':' "
            "(chain separator). Remove ':' or use ColabFold (D3) / AF2 (D2)."
        )

    if len(seq) < SEQ_LEN_MIN:
        return None, (
            f"sequence is {len(seq)} aa — min {SEQ_LEN_MIN}."
        )
    if len(seq) > SEQ_LEN_MAX:
        return None, (
            f"sequence is {len(seq)} aa — max {SEQ_LEN_MAX}. "
            "ESMFold-3B on A100-40GB fits monomers up to 400 aa in the 10-min budget."
        )
    non_canonical = set(seq) - CANONICAL_AA
    if non_canonical:
        return None, (
            f"sequence contains non-canonical residues: "
            f"{sorted(non_canonical)}"
        )

    # Normalise the FASTA — ensure there is always a header line for the pipeline.
    normalized_fasta = f">{header}\n{seq}"

    return (
        {
            "preset": preset,
            "fasta_text": normalized_fasta,
            "target": f"Your FASTA ({len(seq)} aa monomer)",
        },
        None,
    )


def _validate_batch(form: Mapping[str, Any]) -> tuple[Optional[dict], Optional[str]]:
    """Parse the ``sequences`` textarea into a batch_records list."""
    raw = form.get("sequences") or form.get("batch_sequences") or ""
    records, err = parse_fasta_or_lines(raw, default_name_prefix="design")
    if err:
        return None, err
    if not records:
        return None, "Paste at least one sequence."
    if len(records) > MAX_BATCH:
        return None, (
            f"Max {MAX_BATCH} sequences per batch run "
            f"(received {len(records)})."
        )

    for r in records:
        name = r["name"]
        seq = r["sequence"]
        if ":" in seq:
            return None, (
                f"Record {name!r} contains ':' (multimer separator) — "
                "ESMFold v1 is monomer-only. Drop ':' or split records, "
                "or use the ColabFold / AF2 batch tools for multimers."
            )
        if len(seq) < SEQ_LEN_MIN:
            return None, f"Record {name!r} is {len(seq)} aa — min {SEQ_LEN_MIN}."
        if len(seq) > SEQ_LEN_MAX:
            return None, (
                f"Record {name!r} is {len(seq)} aa — max {SEQ_LEN_MAX}. "
                "ESMFold-3B on A100-40GB fits monomers up to 400 aa."
            )
        bad = find_non_canonical_residues(seq, frozenset(CANONICAL_AA))
        if bad:
            return None, (
                f"Record {name!r} contains non-canonical residues: {bad}"
            )

    return (
        {
            "preset": "batch",
            "batch_records": records,
            "target": f"Batch fold ({len(records)} sequences)",
            "parameters": {"n_designs_total": len(records)},
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the ESMFold job_spec shape ``run_pipeline.py`` expects.

    The FASTA travels inline under ``fasta_text`` (no file upload, no
    Storage round-trip — FASTAs are tiny) so the presigned URL is
    ignored. Keeping the ``presigned_url`` argument in the signature
    matches the ``BuildPayloadFn`` protocol in ``tools/base.py``.

    Batch preset forwards ``batch_records`` to the pipeline; the
    standalone preset keeps the existing single-fold contract.
    """
    if inputs.get("preset") == "batch":
        return {
            "preset": "batch",
            "batch_records": inputs.get("batch_records", []),
            "parameters": inputs.get("parameters", {}),
        }
    return {
        "fasta_text": inputs.get("fasta_text", ""),
        "parameters": {},
    }


adapter = ToolAdapter(
    slug="esmfold",
    label="ESMFold",
    blurb=(
        "Single-sequence fold. Paste a FASTA, get a predicted structure "
        "with pLDDT. ESM-2 language-model fold, monomer only, ~30 s per "
        "run."
    ),
    presets=(
        Preset(
            slug="standalone",
            label="Standalone with your FASTA",
            description=(
                "Paste a single-chain FASTA (10 to 400 aa monomer) and "
                "get pLDDT plus predicted structure. ~30 s on A100-40GB "
                "once the 3B model is warm. No MSA, no multimer. Pair "
                "with ColabFold (D3) or AF2 (D2) when you need those."
            ),
        ),
        Preset(
            slug="batch",
            label="Batch for many monomers",
            description=(
                "Fold many monomer sequences (FASTA records or one per "
                "line) in a single job, up to 500 records. Each fold is "
                "shipped back through the partial-results contract; the "
                "results table renders per-design pLDDT, PDB download "
                "and NGL viewer as folds complete. Cost scales linearly "
                "with batch size."
            ),
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    # No file upload: FASTA comes inline. The generic submit route
    # skips PDB staging when requires_pdb=False on both adapter and preset.
    requires_pdb=False,
    form_template="tools/esmfold_form.html",
    results_partial="tools/esmfold_results.html",
)

register(adapter)
