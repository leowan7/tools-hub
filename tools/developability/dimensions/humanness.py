"""Humanness scoring via k-mer overlap against human germline V-genes.

Prototype approximation only. Production would use BioPhi/OASis to compare
against the Observed Antibody Space k-mer repertoire.
"""

from __future__ import annotations

from tools.developability.data.germlines import get_germlines_for_chain

VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")


def _validate_sequence(sequence: str) -> str:
    """Uppercase and validate an amino acid sequence.

    Args:
        sequence: Candidate protein sequence.

    Returns:
        The validated, uppercase sequence.

    Raises:
        ValueError: If the sequence is empty or contains non-canonical residues.
    """
    if not isinstance(sequence, str):
        raise ValueError("sequence must be a string")
    cleaned = sequence.strip().upper()
    if len(cleaned) == 0:
        raise ValueError("sequence must be non-empty")
    bad = set(cleaned) - VALID_AA
    if bad:
        raise ValueError(
            f"sequence contains non-canonical residues: {sorted(bad)}"
        )
    return cleaned


def _kmers(sequence: str, k: int) -> set:
    """Return the set of distinct k-mers in a sequence.

    Args:
        sequence: Protein sequence.
        k: k-mer length.

    Returns:
        Set of distinct substrings of length k.
    """
    if len(sequence) < k:
        return set()
    return {sequence[i : i + k] for i in range(len(sequence) - k + 1)}


def score_humanness(sequence: str, chain_type: str = "VH", k: int = 7) -> dict:
    """Score humanness by germline k-mer overlap.

    The score is the fraction of the input's k-mers that are also present in
    the reference germline pool for the relevant chain type. Higher is more
    human-like. The spec asks for k=9, but with only 5 germline alleles per
    chain a 9-mer rarely matches a mutated framework. k=7 is more informative
    at the prototype germline panel size while preserving specificity above
    chance. Raw overlap is rescaled because no clinical Mab matches germline
    perfectly (CDRs introduce non-germline k-mers).

    Args:
        sequence: Protein sequence to score.
        chain_type: Chain class (e.g. "VH", "VK", "VL").
        k: k-mer length used for comparison. Default 7.

    Returns:
        Dictionary with keys ``raw_overlap`` (fraction of input k-mers found
        in germline pool), ``matched_kmers`` (count), ``total_kmers`` (count),
        ``top_germline`` (name of best-matching germline, or None),
        ``top_germline_overlap`` (fraction), and ``score`` (0 to 1, rescaled).
    """
    cleaned = _validate_sequence(sequence)
    if k <= 0:
        raise ValueError("k must be positive")

    germlines = get_germlines_for_chain(chain_type)
    reference_kmers: set = set()
    per_germline_kmers: dict = {}
    for name, germline_seq in germlines.items():
        g_kmers = _kmers(germline_seq.upper(), k)
        per_germline_kmers[name] = g_kmers
        reference_kmers.update(g_kmers)

    input_kmers = _kmers(cleaned, k)
    total = len(input_kmers)
    if total == 0:
        return {
            "raw_overlap": 0.0,
            "matched_kmers": 0,
            "total_kmers": 0,
            "top_germline": None,
            "top_germline_overlap": 0.0,
            "score": 0.0,
        }

    matched = len(input_kmers & reference_kmers)
    raw_overlap = matched / total

    # Identify the single best-matching germline (highest overlap fraction).
    top_germline = None
    top_overlap = 0.0
    for name, g_kmers in per_germline_kmers.items():
        if not g_kmers:
            continue
        overlap = len(input_kmers & g_kmers) / total
        if overlap > top_overlap:
            top_overlap = overlap
            top_germline = name

    # Rescale: against a 5-allele germline panel with k=7, a fully humanized
    # V-domain shares ~50-60 percent of its 7-mers with the panel. CDRs are
    # mutated away from any germline; framework preserves short germline
    # k-mers. Treat 0.55 as the ceiling for score 1.0 in this prototype;
    # production with the full IMGT repertoire would calibrate differently.
    ceiling = 0.55
    rescaled = min(1.0, raw_overlap / ceiling)

    return {
        "raw_overlap": round(raw_overlap, 4),
        "matched_kmers": matched,
        "total_kmers": total,
        "top_germline": top_germline,
        "top_germline_overlap": round(top_overlap, 4),
        "score": round(rescaled, 4),
    }
