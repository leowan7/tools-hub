"""Sequence liability motif scanner.

Flags common chemical-stability and developability liabilities:
deamidation, isomerization, N-linked glycosylation, oxidation-prone
residues (elevated severity in CDRs), free cysteines, integrin binding
motifs, and Asp-Pro fragmentation sites.

CDR boundaries are estimated using a very simple positional heuristic
(roughly Kabat-like for a standard V-domain). Production would use ANARCI
or HMMER with IMGT numbering for accurate CDR assignment.
"""

from __future__ import annotations

import re
from typing import List

VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")

# Severity weights contribute to the deducted fraction of the score.
SEVERITY_WEIGHT = {"high": 1.0, "medium": 0.5, "low": 0.25}

DEAMIDATION_HIGH = {"NG", "NS", "NT"}
DEAMIDATION_LOW = {"NH", "NA"}
ISOMERIZATION = {"DG", "DS", "DT", "DH"}
N_GLYCOSYLATION_REGEX = re.compile(r"N[^P][ST]")
FRAGMENTATION = {"DP"}
INTEGRIN = {"RGD"}


def _validate_sequence(sequence: str) -> str:
    """Validate and uppercase an amino acid sequence.

    Args:
        sequence: Candidate protein sequence.

    Returns:
        Uppercase cleaned sequence.

    Raises:
        ValueError: If the sequence is empty or contains non-canonical residues.
    """
    if not isinstance(sequence, str):
        raise ValueError("sequence must be a string")
    cleaned = sequence.strip().upper()
    if not cleaned:
        raise ValueError("sequence must be non-empty")
    bad = set(cleaned) - VALID_AA
    if bad:
        raise ValueError(
            f"sequence contains non-canonical residues: {sorted(bad)}"
        )
    return cleaned


def _approximate_cdr_spans(length: int, chain_type: str) -> List[tuple]:
    """Return 1-based (start, end) inclusive CDR spans from length heuristic.

    Heuristic spans are based on canonical Kabat-like positions for a ~120 aa
    V-domain. These are approximate; accurate CDR annotation requires
    numbering schemes (Kabat/Chothia/IMGT) via ANARCI.

    Args:
        length: Sequence length in residues.
        chain_type: "VH", "VK", or "VL". Heavy and light use slightly
            different defaults.

    Returns:
        List of (start, end) 1-based inclusive CDR residue ranges.
    """
    normalized = chain_type.strip().upper() if chain_type else "VH"

    if normalized in {"VH", "HEAVY", "H", "VHH"}:
        # Heavy chain Kabat-like: CDR-H1 ~26-35, CDR-H2 ~50-65, CDR-H3 ~95-102
        # Scale endpoints slightly if the sequence is materially longer/shorter.
        cdr1 = (26, min(35, length))
        cdr2 = (50, min(65, length))
        cdr3_start = max(1, length - 25)
        cdr3_end = min(length - 10, length)
        cdr3 = (cdr3_start, cdr3_end)
    else:
        # Light chain approximate spans.
        cdr1 = (24, min(34, length))
        cdr2 = (50, min(56, length))
        cdr3 = (89, min(97, length))

    # Filter spans to valid ranges.
    spans = []
    for start, end in (cdr1, cdr2, cdr3):
        if start <= length and end >= start:
            spans.append((start, min(end, length)))
    return spans


def _is_in_cdr(position_1based: int, cdr_spans: List[tuple]) -> bool:
    """Check whether a 1-based residue position lies in any CDR span."""
    return any(start <= position_1based <= end for start, end in cdr_spans)


def find_liabilities(sequence: str, chain_type: str = "VH") -> List[dict]:
    """Scan for canonical developability liability motifs.

    Args:
        sequence: Protein sequence to scan.
        chain_type: Chain type used to approximate CDR boundaries for
            oxidation severity elevation.

    Returns:
        List of liability records. Each record has keys ``position`` (1-based),
        ``motif``, ``type``, ``severity`` (``low``, ``medium``, ``high``), and
        ``in_cdr`` (bool).
    """
    cleaned = _validate_sequence(sequence)
    cdr_spans = _approximate_cdr_spans(len(cleaned), chain_type)
    findings: List[dict] = []

    # Dipeptide scans (deamidation, isomerization, fragmentation).
    for i in range(len(cleaned) - 1):
        dipeptide = cleaned[i : i + 2]
        position = i + 1
        in_cdr = _is_in_cdr(position, cdr_spans)

        if dipeptide in DEAMIDATION_HIGH:
            findings.append(
                {
                    "position": position,
                    "motif": dipeptide,
                    "type": "deamidation",
                    "severity": "high" if in_cdr else "medium",
                    "in_cdr": in_cdr,
                }
            )
        elif dipeptide in DEAMIDATION_LOW:
            findings.append(
                {
                    "position": position,
                    "motif": dipeptide,
                    "type": "deamidation",
                    "severity": "low",
                    "in_cdr": in_cdr,
                }
            )
        elif dipeptide in ISOMERIZATION:
            findings.append(
                {
                    "position": position,
                    "motif": dipeptide,
                    "type": "isomerization",
                    "severity": "high" if in_cdr else "medium",
                    "in_cdr": in_cdr,
                }
            )
        elif dipeptide in FRAGMENTATION:
            findings.append(
                {
                    "position": position,
                    "motif": dipeptide,
                    "type": "fragmentation",
                    "severity": "medium",
                    "in_cdr": in_cdr,
                }
            )

    # N-linked glycosylation (N-X-S/T, X != P) — 3-residue regex scan.
    for match in N_GLYCOSYLATION_REGEX.finditer(cleaned):
        position = match.start() + 1
        findings.append(
            {
                "position": position,
                "motif": match.group(),
                "type": "n_glycosylation",
                "severity": "high",
                "in_cdr": _is_in_cdr(position, cdr_spans),
            }
        )

    # Integrin binding tripeptide RGD.
    for i in range(len(cleaned) - 2):
        tripeptide = cleaned[i : i + 3]
        if tripeptide in INTEGRIN:
            position = i + 1
            findings.append(
                {
                    "position": position,
                    "motif": tripeptide,
                    "type": "integrin_binding",
                    "severity": "high",
                    "in_cdr": _is_in_cdr(position, cdr_spans),
                }
            )

    # Oxidation-prone residues: M and W. Elevate severity in CDRs.
    for index, residue in enumerate(cleaned):
        if residue in {"M", "W"}:
            position = index + 1
            in_cdr = _is_in_cdr(position, cdr_spans)
            findings.append(
                {
                    "position": position,
                    "motif": residue,
                    "type": "oxidation",
                    "severity": "high" if in_cdr else "low",
                    "in_cdr": in_cdr,
                }
            )

    # Free cysteine check: odd number of cysteines indicates unpaired disulfide.
    cys_positions = [i + 1 for i, r in enumerate(cleaned) if r == "C"]
    if len(cys_positions) % 2 == 1:
        # Flag every cysteine so caller can see context, but as a single
        # liability the unpaired residue is the concern; we add a summary
        # finding at the first cysteine position for simplicity.
        findings.append(
            {
                "position": cys_positions[0] if cys_positions else 0,
                "motif": "C",
                "type": "free_cysteine",
                "severity": "high",
                "in_cdr": False,
                "detail": f"odd cysteine count ({len(cys_positions)})",
            }
        )

    return findings


def score_liabilities(sequence: str, chain_type: str = "VH") -> dict:
    """Return liability count, per-motif list, and normalized score.

    Args:
        sequence: Protein sequence.
        chain_type: Chain type passed to the scanner (affects CDR severity).

    Returns:
        Dictionary with ``liabilities`` (list), ``weighted_count`` (float),
        ``score`` (0 to 1, higher is better).
    """
    cleaned = _validate_sequence(sequence)
    liability_records = find_liabilities(cleaned, chain_type=chain_type)
    weighted_count = sum(
        SEVERITY_WEIGHT.get(record.get("severity", "low"), 0.25)
        for record in liability_records
    )
    score = 1.0 - (weighted_count / len(cleaned))
    score = max(0.0, min(1.0, score))
    return {
        "liabilities": liability_records,
        "count": len(liability_records),
        "weighted_count": round(weighted_count, 3),
        "score": round(score, 4),
    }
