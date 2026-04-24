"""ColabFold standalone (D3) — atomic primitive (no-MSA fast fold).

Modal app: ``ranomics-colabfold-prod``. GPU: A100-40GB.
Spec lives in ``docs/ATOMIC-TOOLS.md`` under D3. Sibling of D2 (AF2
standalone) but lighter / faster — single-sequence MSA, 1 recycle,
no templates by default.

The user uploads a FASTA (monomer or multimer), and receives a
predicted structure (PDB b64) plus pLDDT, PAE matrix (npz-b64), and
pTM / ipTM scores. 2-credit tool per PRODUCT-PLAN.md "Credit rates"
table.

Two tiers follow the D1 pattern:

- ``smoke`` — baked 76 aa ubiquitin monomer, num_recycles=1, no
  templates. No FASTA input needed. 0 credits. Demo-before-you-spend.
- ``standalone`` — caller-supplied FASTA (inline text field, no file
  upload required). 2 credits. Monomers up to 600 aa, multimers up
  to 600 aa total length.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds (also enforced on the pipeline side for direct ``modal run`` use).
# ---------------------------------------------------------------------------

RECYCLES_MIN = 1
RECYCLES_MAX = 5
SEQ_LEN_MIN = 10
SEQ_LEN_MAX = 600  # matches run_pipeline.SEQ_LEN_MAX
CANONICAL_AA = set("ACDEFGHIKLMNPQRSTVWYX")


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

    Branches on preset:
      - ``smoke``: baked 76 aa ubiquitin, 1 recycle, no templates.
      - ``standalone``: caller-supplied FASTA text + num_recycles +
        use_templates.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"smoke", "standalone"}:
        return None, "Pick a preset."

    if preset == "smoke":
        return (
            {
                "preset": preset,
                "fasta_text": "",  # empty — pipeline loads the baked fixture
                "num_recycles": 1,
                "use_templates": False,
                "target": "Ubiquitin (76 aa monomer, UniProt P0CG47)",
            },
            None,
        )

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


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the ColabFold job_spec shape ``run_pipeline.py`` expects.

    The FASTA travels inline under ``fasta_text`` (no file upload, no
    Storage round-trip — FASTAs are tiny) so the presigned URL is
    ignored. Keeping the ``presigned_url`` argument in the signature
    matches the ``BuildPayloadFn`` protocol in ``tools/base.py``.
    """
    return {
        "fasta_text": inputs.get("fasta_text", ""),
        "parameters": {
            "num_recycles": inputs["num_recycles"],
            "use_templates": inputs["use_templates"],
        },
    }


adapter = ToolAdapter(
    slug="colabfold",
    label="ColabFold — fast no-MSA fold",
    blurb=(
        "Paste a FASTA, get a predicted structure with pLDDT and PAE. "
        "No-MSA speed tier — A100-40GB, ~1-2 min per run."
    ),
    presets=(
        Preset(
            slug="smoke",
            label="Smoke — ubiquitin demo, 0 credits",
            credits_cost=0,
            description=(
                "Runs against a baked 76 aa ubiquitin fixture. "
                "Same pipeline, smallest preset — verifies the tool works "
                "before you spend credits."
            ),
        ),
        Preset(
            slug="standalone",
            label="Standalone — your FASTA, 2 credits",
            credits_cost=2,
            description=(
                "Paste a FASTA (monomer or multimer up to 600 aa total) "
                "and get pLDDT + PAE + pTM/ipTM. ~1-2 min on A100-40GB. "
                "No MSA, no templates — pair with D2 AF2 if you need the "
                "full MSA-backed fold."
            ),
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
