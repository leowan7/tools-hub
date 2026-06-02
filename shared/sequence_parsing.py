"""Shared FASTA/multi-line sequence parser for batch-input tools.

Used by the structure-prediction tools (ESMFold, AF2, ColabFold, Boltz-2)
to parse a textarea blob into a list of ``{name, sequence}`` records.
Each tool layers its own per-record bounds and canonical-AA validation
on top of the shape this helper returns.

Lifted from ``tools/boltz2/__init__.py:_parse_binder_text`` so the same
contract drives every batch-capable form.
"""

from __future__ import annotations

from typing import Optional


CANONICAL_AA: frozenset[str] = frozenset("ACDEFGHIKLMNPQRSTVWYX")


def parse_fasta_or_lines(
    raw: str, default_name_prefix: str = "design"
) -> tuple[Optional[list[dict[str, str]]], Optional[str]]:
    """Parse a textarea blob into a list of ``{name, sequence}`` records.

    Accepts either:

    - **FASTA** — one or more ``>header`` records. Sequence lines after a
      header concatenate until the next header. Empty headers fall back
      to ``{default_name_prefix}_<i>``.
    - **Plain** — one sequence per line, no headers. Each line becomes
      ``{default_name_prefix}_<i>``.

    Sequences are upper-cased. Length and AA-composition checks are
    deliberately NOT done here — leave those to the caller so per-tool
    bounds (ESMFold 10–400 monomer; AF2/ColabFold up to 1500 with ``:``
    multimer separator; Boltz-2 20–400 binder) stay tool-local.

    Returns ``(records, None)`` on success or ``(None, error)`` on parse
    failure. ``records`` is a fresh list; the caller may mutate it.
    """
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if not lines:
        return None, "Paste at least one sequence."

    records: list[dict[str, str]] = []
    has_fasta_header = any(ln.startswith(">") for ln in lines)

    if not has_fasta_header:
        for i, ln in enumerate(lines):
            records.append(
                {"name": f"{default_name_prefix}_{i}", "sequence": ln.upper()}
            )
        return records, None

    header: Optional[str] = None
    buf: list[str] = []
    for ln in lines:
        if ln.startswith(">"):
            if header is not None:
                records.append({
                    "name": header,
                    "sequence": "".join(buf).upper(),
                })
            header = ln[1:].strip() or f"{default_name_prefix}_{len(records)}"
            buf = []
        else:
            buf.append(ln)
    if header is not None:
        records.append({"name": header, "sequence": "".join(buf).upper()})

    records = [r for r in records if r["sequence"]]
    if not records:
        return None, "Could not parse any sequences from the input."
    return records, None


def find_non_canonical_residues(
    seq: str, allowed: frozenset[str] = CANONICAL_AA
) -> list[str]:
    """Return a sorted list of any residues in ``seq`` not in ``allowed``.

    Empty list means the sequence is clean. Caller decides whether to
    treat non-canonicals as an error or a warning; structure-prediction
    tools currently reject any non-canonical (including ``:`` for tools
    that don't support the multimer separator).
    """
    return sorted(set(seq) - allowed)
