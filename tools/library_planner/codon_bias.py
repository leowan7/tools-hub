"""S. cerevisiae codon preference analysis for degenerate library schemes.

Each degenerate scheme (NNK, NNS, NNN, trimer) commits to a specific subset
of codons at every diversified position. When the scheme over-samples codons
that yeast decodes poorly, display levels drop and the effective functional
library shrinks.

This module flags the specific codons within each scheme that are rare in
S. cerevisiae (below ``RARE_CODON_THRESHOLD`` per 1000) and recommends a
scheme given scaffold and diversity scale.
"""

from __future__ import annotations

from typing import List

from tools.library_planner.data.scerevisiae_codons import (
    CODON_TO_AA,
    NNK_CODONS,
    NNS_CODONS,
    RARE_CODON_THRESHOLD,
    SCER_CODON_FREQ_PER_1000,
)

VALID_SCHEMES = ("NNK", "NNS", "NNN", "trimer")


def _codons_for_scheme(scheme: str) -> List[str]:
    """Return the set of codons used by a given degenerate scheme.

    Args:
        scheme: One of NNK, NNS, NNN, trimer.

    Returns:
        List of DNA codons encoded by the scheme.

    Raises:
        ValueError: If the scheme is not recognized.
    """
    if scheme == "NNK":
        return list(NNK_CODONS)
    if scheme == "NNS":
        return list(NNS_CODONS)
    if scheme == "NNN":
        return list(CODON_TO_AA.keys())
    if scheme == "trimer":
        # Trimer synthesis is codon level. A canonical trimer set picks one
        # preferred codon per amino acid for the host organism. For yeast we
        # use the highest-frequency codon for each of the 20 amino acids.
        preferred: dict = {}
        for codon, aa in CODON_TO_AA.items():
            if aa == "*":
                continue
            freq = SCER_CODON_FREQ_PER_1000.get(codon, 0.0)
            if aa not in preferred or freq > preferred[aa][1]:
                preferred[aa] = (codon, freq)
        return [c for c, _ in preferred.values()]
    raise ValueError(
        f"unknown scheme {scheme!r}; valid: {list(VALID_SCHEMES)}"
    )


def bias_warnings(scheme: str, scaffold: str) -> List[dict]:
    """Return rare-codon warnings for a given scheme in a given scaffold.

    The scaffold argument is currently used only to contextualize the warning
    message (e.g. Arg bias matters more for VHH CDR3 which tends to be
    arginine rich). The codon bias numbers themselves are host-dependent, not
    scaffold-dependent.

    Args:
        scheme: One of NNK, NNS, NNN, trimer.
        scaffold: Name of the scaffold (e.g. scFv, VHH, Fab, DARPin, custom).

    Returns:
        List of dicts, each with ``codon``, ``amino_acid``, ``frequency``,
        and ``issue`` fields.
    """
    if scheme not in VALID_SCHEMES:
        raise ValueError(
            f"unknown scheme {scheme!r}; valid: {list(VALID_SCHEMES)}"
        )
    if not isinstance(scaffold, str) or not scaffold:
        raise ValueError("scaffold must be a non-empty string")

    codons = _codons_for_scheme(scheme)
    warnings: List[dict] = []
    for codon in codons:
        aa = CODON_TO_AA.get(codon, "?")
        if aa == "*":
            continue
        freq = SCER_CODON_FREQ_PER_1000.get(codon, 0.0)
        if freq < RARE_CODON_THRESHOLD:
            warnings.append({
                "codon": codon,
                "amino_acid": aa,
                "frequency_per_1000": round(freq, 2),
                "issue": (
                    f"{codon} ({aa}) is a rare codon in S. cerevisiae at "
                    f"{freq:.1f} per 1000. Variants using this codon may "
                    f"display poorly; consider trimer synthesis or a host "
                    f"with matching tRNA pool."
                ),
            })
    return warnings


def recommend_scheme(positions: int, scaffold: str) -> dict:
    """Recommend a degenerate scheme given positions and scaffold.

    Rules of thumb encoded here:

    - For <= 6 positions on a high-value scaffold (VHH, DARPin), trimer
      synthesis is feasible and gives the cleanest amino acid distribution.
    - For 7-12 positions, NNS or NNK is the standard compromise. NNS avoids
      the NNK skew toward amino acids with 3 codons (Arg, Leu, Ser) because
      NNS codons end in C or G, reducing the number of Arg-encoding codons.
    - For > 12 positions the library is typically stop-diluted beyond
      practical recovery with NNN, so NNK or NNS with stop-codon cleanup
      (amber-suppression strain or affinity pre-selection) is standard.
    - Naive screening campaigns use NNK by default because of cost.

    Args:
        positions: Number of randomized positions.
        scaffold: Scaffold name.

    Returns:
        Dict with ``scheme`` and ``rationale`` keys.
    """
    if not isinstance(positions, int) or isinstance(positions, bool):
        raise ValueError("positions must be an int")
    if positions < 1:
        raise ValueError("positions must be >= 1")
    if not isinstance(scaffold, str) or not scaffold:
        raise ValueError("scaffold must be a non-empty string")

    scaffold_lower = scaffold.lower()
    if positions <= 6 and scaffold_lower in ("vhh", "darpin"):
        return {
            "scheme": "trimer",
            "rationale": (
                f"At {positions} positions on {scaffold}, trimer synthesis "
                f"is practical and eliminates both stop codons and S. "
                f"cerevisiae rare codons. Cost is manageable below ~8 "
                f"positions."
            ),
        }
    if positions <= 12:
        return {
            "scheme": "NNK",
            "rationale": (
                f"At {positions} positions, NNK is the standard cost-"
                f"effective choice. 32 codons cover all 20 AA with a single "
                f"stop codon (TAG) and the library survives yeast "
                f"transformation up to ~1e9."
            ),
        }
    return {
        "scheme": "NNK",
        "rationale": (
            f"At {positions} positions the full amino acid space "
            f"(20^{positions}) exceeds yeast transformation ceiling by many "
            f"orders of magnitude. NNK is chosen by cost, but any naive "
            f"library at this diversity will sample only a tiny fraction "
            f"of sequence space. Consider hierarchical or computationally "
            f"designed sub-libraries."
        ),
    }
