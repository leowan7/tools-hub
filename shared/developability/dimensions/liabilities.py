"""Sequence liability motif scanner.

Flags common chemical-stability and developability liabilities: deamidation,
isomerization, N-linked glycosylation, oxidation-prone residues, free
cysteines, integrin binding motifs, and Asp-Pro fragmentation sites.

SEVERITY COMES FROM MEASURED EXPOSURE, OR NOT AT ALL
----------------------------------------------------
Each motif has a base severity from its chemistry alone. A motif the caller
has MEASURED to be solvent-exposed is escalated one step
(:func:`_escalate`), because a buried motif is far less reachable by solvent,
enzymes or a neighbouring molecule than an exposed one. Exposure is passed in
as relative accessibility by
``shared/surface_patch.py::surface_profile``'s ``residue_rsa``; with no
exposure supplied, every record keeps its base severity and reports
``exposed: None``, meaning unknown rather than buried.

This replaced a CDR-based escalation, and the reason is worth keeping. That
rule raised the severity of any motif falling inside a CDR, with the CDR
boundaries guessed from sequence LENGTH -- CDR-H3 was "the stretch from
``len - 25`` to ``len - 10``" -- rather than from any numbering scheme. The
hub lead's ruling of 2026-11-16 generalised Leo's surface rule to it: a flag
rests on something measured, not on a guessed region. The length heuristic,
the ``in_cdr`` key, and the ``chain_type`` argument that selected heavy- or
light-chain spans are all gone with it. Nothing here now varies by chain type.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Sequence

from shared.surface_patch import EXPOSED_RESIDUE_RSA

logger = logging.getLogger(__name__)

VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")

# Severity weights contribute to the deducted fraction of the score.
SEVERITY_WEIGHT = {"high": 1.0, "medium": 0.5, "low": 0.25}

# One step up, for a motif measured solvent-exposed. "high" is the top.
_ESCALATION = {"low": "medium", "medium": "high", "high": "high"}

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


def _motif_exposed(
    residue_rsa: Optional[Sequence], position: int, length: int
) -> Optional[bool]:
    """Whether a motif starting at 1-based ``position`` is solvent-exposed.

    ``length`` is the motif's length in residues: the MOST exposed residue of
    a motif decides, because a dipeptide or triplet is reachable if any part
    of it is on the surface.

    Returns ``None`` -- unknown, NOT buried -- when no exposure was supplied,
    when the motif runs past the end of the exposure list, or when ANY residue
    of it has unknown accessibility. Any, not every: one unmeasured residue
    makes the motif's exposure unknown rather than letting its neighbours
    decide it, which is the same conservative rule as the out-of-range case
    below. A caller must not read ``None`` as "buried": it means the question
    was not answered, and the severity stays at its base.
    """
    if residue_rsa is None:
        return None
    window = range(position - 1, position - 1 + length)
    # A motif that straddles the end of the list is UNKNOWN, not buried. The
    # earlier version dropped the out-of-range indices and answered from
    # whatever was left, so an NG whose G lay past the end reported
    # exposed=False -- a measurement of a residue nobody measured.
    if any(not (0 <= i < len(residue_rsa)) for i in window):
        return None
    values = [residue_rsa[i] for i in window]
    # Same rule for a residue of unknown type inside the motif: one None makes
    # the motif's exposure unknown rather than deciding it from its neighbours.
    if any(v is None for v in values):
        return None
    return max(values) > EXPOSED_RESIDUE_RSA


def _escalate(base: str, exposed: Optional[bool]) -> str:
    """``base`` raised one step if ``exposed`` is True, else ``base``.

    An unknown exposure (``None``) is treated exactly like a buried one: it
    does not escalate. The difference between the two is recorded on the
    record's ``exposed`` key, not in the severity.
    """
    return _ESCALATION[base] if exposed else base


def _record(
    position: int,
    motif: str,
    kind: str,
    base: str,
    residue_rsa: Optional[Sequence],
) -> dict:
    """One liability record, with severity resolved against measured exposure."""
    exposed = _motif_exposed(residue_rsa, position, len(motif))
    return {
        "position": position,
        "motif": motif,
        "type": kind,
        "base_severity": base,
        "severity": _escalate(base, exposed),
        "exposed": exposed,
    }


def find_liabilities(
    sequence: str, residue_rsa: Optional[Sequence] = None
) -> List[dict]:
    """Scan for canonical developability liability motifs.

    Args:
        sequence: Protein sequence to scan.
        residue_rsa: Optional per-residue relative solvent accessibility for
            THIS sequence, in sequence order, as
            ``surface_profile()["residue_rsa"][chain]`` returns it. Index
            ``i`` must be the accessibility of ``sequence[i]``; a caller that
            cannot guarantee that alignment must pass ``None`` rather than a
            list from a different chain. Entries may be ``None`` for a
            residue of unknown type.

    Returns:
        List of liability records. Each has ``position`` (1-based), ``motif``,
        ``type``, ``base_severity`` (chemistry alone), ``severity`` (after
        exposure escalation) and ``exposed`` (``True``/``False``/``None`` for
        unknown). Free-cysteine records carry a ``detail`` string too.
    """
    cleaned = _validate_sequence(sequence)

    # A MISALIGNED RSA LIST IS WORSE THAN NO RSA LIST, so refuse it.
    #
    # residue_rsa is applied positionally: index i is taken to be the
    # accessibility of cleaned[i]. If the structure is missing one residue the
    # ordered sequence has -- an unresolved terminus, a cleaved tag -- every
    # later index shifts, and the severities come from the wrong residues'
    # exposure with nothing to show it happened. A length mismatch is the one
    # cheap signal of that, so it discards the exposure data and every motif
    # reports exposed=None (unknown). Silently trusting a short or long list
    # was the earlier behaviour.
    #
    # This cannot catch an internal gap that leaves the length equal; it is a
    # floor, not a proof of alignment. The caller is still responsible for
    # passing the RSA of THIS sequence's own chain.
    if residue_rsa is not None and len(residue_rsa) != len(cleaned):
        logger.warning(
            "liabilities: residue_rsa has %d entries for a %d-residue "
            "sequence; discarding it as misaligned",
            len(residue_rsa),
            len(cleaned),
        )
        residue_rsa = None

    findings: List[dict] = []

    # Dipeptide scans (deamidation, isomerization, fragmentation).
    for i in range(len(cleaned) - 1):
        dipeptide = cleaned[i : i + 2]
        position = i + 1

        if dipeptide in DEAMIDATION_HIGH:
            findings.append(
                _record(position, dipeptide, "deamidation", "medium", residue_rsa)
            )
        elif dipeptide in DEAMIDATION_LOW:
            findings.append(
                _record(position, dipeptide, "deamidation", "low", residue_rsa)
            )
        elif dipeptide in ISOMERIZATION:
            findings.append(
                _record(position, dipeptide, "isomerization", "medium", residue_rsa)
            )
        elif dipeptide in FRAGMENTATION:
            findings.append(
                _record(position, dipeptide, "fragmentation", "medium", residue_rsa)
            )

    # N-linked glycosylation (N-X-S/T, X != P) — 3-residue regex scan.
    for match in N_GLYCOSYLATION_REGEX.finditer(cleaned):
        findings.append(
            _record(
                match.start() + 1,
                match.group(),
                "n_glycosylation",
                "high",
                residue_rsa,
            )
        )

    # Integrin binding tripeptide RGD.
    for i in range(len(cleaned) - 2):
        if cleaned[i : i + 3] in INTEGRIN:
            findings.append(
                _record(i + 1, cleaned[i : i + 3], "integrin_binding", "high", residue_rsa)
            )

    # Oxidation-prone residues: M and W. Base severity is low; an exposed one
    # escalates, which is the whole point -- a buried methionine is not
    # readily oxidised, and this used to be decided by a guessed CDR span.
    for index, residue in enumerate(cleaned):
        if residue in {"M", "W"}:
            findings.append(
                _record(index + 1, residue, "oxidation", "low", residue_rsa)
            )

    # Free cysteine: an odd cysteine count means an unpaired thiol. Reported
    # once, at the first cysteine, as the whole-chain property it is.
    #
    # Deliberately NOT exposure-escalated: the finding is about the count of
    # cysteines in the chain, not about the residue its position names, so the
    # accessibility of that one residue says nothing about it. ``exposed`` is
    # None (unknown) for the same reason, and the base severity is already the
    # top of the scale.
    cys_positions = [i + 1 for i, r in enumerate(cleaned) if r == "C"]
    if len(cys_positions) % 2 == 1:
        findings.append(
            {
                "position": cys_positions[0] if cys_positions else 0,
                "motif": "C",
                "type": "free_cysteine",
                "base_severity": "high",
                "severity": "high",
                "exposed": None,
                "detail": f"odd cysteine count ({len(cys_positions)})",
            }
        )

    return findings


def score_liabilities(
    sequence: str, residue_rsa: Optional[Sequence] = None
) -> dict:
    """Return liability count, per-motif list, and normalized score.

    Args:
        sequence: Protein sequence.
        residue_rsa: Per-residue relative accessibility, as for
            :func:`find_liabilities`. Supplying it raises the severity of
            motifs measured exposed, which LOWERS ``score``.

    Returns:
        Dictionary with ``liabilities`` (list), ``count``, ``weighted_count``
        (float) and ``score`` (0 to 1, higher is better).
    """
    cleaned = _validate_sequence(sequence)
    liability_records = find_liabilities(cleaned, residue_rsa=residue_rsa)
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
