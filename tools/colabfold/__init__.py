"""ColabFold standalone (D3) — atomic primitive (no-MSA fast fold).

Modal app: ``ranomics-colabfold-prod``. GPU: A100-40GB.
Spec lives in ``docs/ATOMIC-TOOLS.md`` under D3. Sibling of D2 (AF2
standalone) but lighter / faster — single-sequence MSA, 1 recycle,
no templates by default.

The user uploads a FASTA (monomer or multimer), and receives a
predicted structure (PDB b64) plus pLDDT, PAE matrix (npz-b64), and
pTM / ipTM scores. 2-credit tool per PRODUCT-PLAN.md "Credit rates"
table.

A single ``standalone`` tier takes a caller-supplied FASTA (inline text
field, no file upload required). 2 credits. Monomers up to 600 aa,
multimers up to 600 aa total length.
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

RECYCLES_MIN = 1
RECYCLES_MAX = 5
SEQ_LEN_MIN = 10
SEQ_LEN_MAX = 600  # matches run_pipeline.SEQ_LEN_MAX
CANONICAL_AA = set(SHARED_CANONICAL_AA)
# Batch preset ceiling. No-MSA ColabFold folds in ~1-2 min warm per
# record on A100-40GB. 200 records × ~2 min = ~7 h sequential — fits
# inside the 4 h Modal session budget only at the low end (~100 fast
# folds). For library-scale we still rely on splitting; future Modal-
# side fan-out lifts the ceiling.
MAX_BATCH = 200


def _parse_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default`` on failure."""
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value: Any, default: bool) -> bool:
    """Coerce an HTML-form checkbox value into bool."""
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _parse_fasta_text(raw: str) -> Optional[tuple[list[tuple[str, str]], str]]:
    """Minimal FASTA text parser.

    Returns ``(records, error)``. ``records`` is a list of
    ``(header, seq)`` tuples. On parse failure returns ``(None, error)``.

    Accepts both single-chain FASTA (one ``>header`` + sequence) and
    multi-chain (multiple ``>header`` records). Sequence whitespace
    (including newlines inside a record) is stripped.
    """
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if not lines:
        return [], "FASTA is empty"
    if not lines[0].startswith(">"):
        # Accept "bare sequence" — treat as a single unnamed chain.
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
    """Coerce form fields into the ColabFold job_spec shape.

    Two presets:

    - ``standalone`` (default): caller-supplied FASTA, num_recycles,
      use_templates. Multi-record FASTA is treated as ONE multimer fold
      (chains joined with ``:``).
    - ``batch``: caller-supplied list of fold targets. Each record is an
      independent fold; ``:`` inside a record marks chain breaks.

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

    total_len = 0
    for header, seq in records:
        if not seq:
            return None, f"record {header!r} has no sequence."
        if len(seq) < SEQ_LEN_MIN:
            return None, (
                f"record {header!r} is {len(seq)} aa — min {SEQ_LEN_MIN}."
            )
        if len(seq) > SEQ_LEN_MAX:
            return None, (
                f"record {header!r} is {len(seq)} aa — max {SEQ_LEN_MAX}."
            )
        non_canonical = set(seq) - CANONICAL_AA
        if non_canonical:
            return None, (
                f"record {header!r} contains non-canonical residues: "
                f"{sorted(non_canonical)}"
            )
        total_len += len(seq)

    if total_len > SEQ_LEN_MAX:
        return None, (
            f"total complex length {total_len} exceeds max {SEQ_LEN_MAX} aa "
            "for the no-MSA 10-min budget — split into smaller jobs."
        )

    num_recycles = _parse_int(form.get("num_recycles"), 1)
    if num_recycles < RECYCLES_MIN or num_recycles > RECYCLES_MAX:
        return (
            None,
            f"num_recycles must be between {RECYCLES_MIN} and {RECYCLES_MAX}.",
        )

    use_templates = _parse_bool(form.get("use_templates"), False)

    # Normalise the FASTA for ColabFold. ``colabfold_batch`` treats each
    # ``>header`` record as an independent job — which means two ``>``
    # records would silently fold two separate monomers and the parser
    # would return only the first (Codex P1). For multimers, ColabFold
    # expects ONE record whose sequence joins chains with ``:``. Do
    # that normalisation here so downstream code never sees multiple
    # records for a single fold.
    if len(records) == 1:
        header, seq = records[0]
        normalized_fasta = f">{header}\n{seq}"
    else:
        combined_header = "_".join(h for h, _ in records) or "multimer"
        combined_seq = ":".join(seq for _, seq in records)
        normalized_fasta = f">{combined_header}\n{combined_seq}"

    chain_label = (
        "monomer"
        if len(records) == 1
        else f"multimer ({len(records)} chains, {total_len} aa total)"
    )

    return (
        {
            "preset": preset,
            "fasta_text": normalized_fasta,
            "num_recycles": num_recycles,
            "use_templates": use_templates,
            "target": f"Your FASTA ({chain_label})",
        },
        None,
    )


def _validate_batch(form: Mapping[str, Any]) -> tuple[Optional[dict], Optional[str]]:
    """Parse the ``sequences`` textarea into a batch_records list.

    Each record's ``sequence`` may contain ``:`` chain breaks for an
    intra-record multimer fold. Per-record total AA ≤ SEQ_LEN_MAX.
    """
    raw = form.get("sequences") or form.get("batch_sequences") or ""
    records, err = parse_fasta_or_lines(raw, default_name_prefix="fold")
    if err:
        return None, err
    if not records:
        return None, "Paste at least one fold target."
    if len(records) > MAX_BATCH:
        return None, (
            f"Max {MAX_BATCH} records per batch run "
            f"(received {len(records)}). For larger libraries split into "
            "multiple batches or use ESMFold batch for the monomer "
            "screening pass."
        )

    for r in records:
        name = r["name"]
        seq = r["sequence"]
        chains = seq.split(":") if ":" in seq else [seq]
        chains = [c for c in chains if c]
        if not chains:
            return None, f"Record {name!r} parsed to zero chains."
        total = sum(len(c) for c in chains)
        if total > SEQ_LEN_MAX:
            return None, (
                f"Record {name!r} is {total} aa across {len(chains)} "
                f"chain(s) — max {SEQ_LEN_MAX} per record (no-MSA budget)."
            )
        for ci, chain in enumerate(chains):
            if len(chain) < SEQ_LEN_MIN:
                return None, (
                    f"Record {name!r} chain {ci + 1} is {len(chain)} aa "
                    f"— min {SEQ_LEN_MIN}."
                )
            bad = find_non_canonical_residues(chain, frozenset(CANONICAL_AA))
            if bad:
                return None, (
                    f"Record {name!r} chain {ci + 1} contains non-canonical "
                    f"residues: {bad}"
                )

    num_recycles = _parse_int(form.get("num_recycles"), 1)
    if num_recycles < RECYCLES_MIN or num_recycles > RECYCLES_MAX:
        return None, (
            f"num_recycles must be between {RECYCLES_MIN} and {RECYCLES_MAX}."
        )
    use_templates = _parse_bool(form.get("use_templates"), False)

    batch_records = [
        {
            "name": r["name"],
            "sequence": r["sequence"],
            "chains": (r["sequence"].split(":") if ":" in r["sequence"] else [r["sequence"]]),
        }
        for r in records
    ]

    return (
        {
            "preset": "batch",
            "batch_records": batch_records,
            "num_recycles": num_recycles,
            "use_templates": use_templates,
            "target": f"ColabFold batch ({len(batch_records)} records)",
            "parameters": {"n_designs_total": len(batch_records)},
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the ColabFold job_spec shape ``run_pipeline.py`` expects.

    The FASTA travels inline under ``fasta_text`` (no file upload, no
    Storage round-trip — FASTAs are tiny) so the presigned URL is
    ignored. Keeping the ``presigned_url`` argument in the signature
    matches the ``BuildPayloadFn`` protocol in ``tools/base.py``.

    Batch preset forwards ``batch_records`` and the per-job parameters;
    the standalone preset keeps the existing single-fold contract.
    """
    if inputs.get("preset") == "batch":
        return {
            "batch_records": inputs.get("batch_records", []),
            "parameters": {
                "num_recycles": inputs["num_recycles"],
                "use_templates": bool(inputs["use_templates"]),
                "n_designs_total": len(inputs.get("batch_records", [])),
            },
        }
    return {
        "fasta_text": inputs.get("fasta_text", ""),
        "parameters": {
            "num_recycles": inputs["num_recycles"],
            "use_templates": inputs["use_templates"],
        },
    }


adapter = ToolAdapter(
    slug="colabfold",
    label="ColabFold",
    blurb=(
        "Paste a sequence and get a predicted structure back in one to "
        "two minutes, with per-residue confidence. Trades a little "
        "accuracy for speed by skipping the search for related natural "
        "sequences."
    ),
    presets=(
        Preset(
            slug="standalone",
            label="Standalone with your FASTA",
            description=(
                "Paste a FASTA (monomer or multimer up to 600 aa total) "
                "and get pLDDT, PAE, and pTM/ipTM. ~1 to 2 min on "
                "A100-40GB. No MSA, no templates. Pair with D2 AF2 if "
                "you need the full MSA-backed fold."
            ),
        ),
        Preset(
            slug="batch",
            label="Batch for many fold targets",
            description=(
                "Fold many independent targets in one job (up to 200 "
                "records). Each record can be a monomer or a multimer "
                "(use ``:`` inside a record to break chains). Per-design "
                "results stream into the job page as folds complete. "
                "Fast no-MSA tier, ~1 to 2 min per fold."
            ),
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    # No file upload: FASTA comes inline. The generic submit route
    # skips PDB staging when requires_pdb=False on both adapter and preset.
    requires_pdb=False,
    form_template="tools/colabfold_form.html",
    results_partial="tools/colabfold_results.html",
)

register(adapter)
