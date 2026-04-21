"""Library complexity math for degenerate codon schemes.

All formulae here describe DNA-level diversity. The protein-level functional
diversity is a subset of the DNA diversity because of stop codons.

Schemes supported:
    NNK: N = any base, K = G or T. 32 codons encoding 20 AA plus the amber
         stop (TAG). Stop codon fraction = 1/32.
    NNS: N = any base, S = C or G. 32 codons encoding 20 AA plus the amber
         stop (TAG). Stop codon fraction = 1/32.
    NNN: 64 codons encoding 20 AA plus 3 stops (TAA, TAG, TGA). Stop codon
         fraction = 3/64.
    trimer: Codon-level synthesis of 20 AA, no stops. 20 codons per position.
         Stop codon fraction = 0.
"""

from __future__ import annotations

VALID_SCHEMES = ("NNK", "NNS", "NNN", "trimer")

# Codons per position for each scheme.
CODONS_PER_POSITION: dict = {
    "NNK": 32,
    "NNS": 32,
    "NNN": 64,
    "trimer": 20,
}

# Fraction of codons per position that are stop codons.
STOP_FRACTION: dict = {
    "NNK": 1.0 / 32.0,
    "NNS": 1.0 / 32.0,
    "NNN": 3.0 / 64.0,
    "trimer": 0.0,
}


def _validate(scheme: str, positions: int) -> None:
    """Validate combinatorics inputs.

    Args:
        scheme: Codon scheme name.
        positions: Number of diversified positions.

    Raises:
        ValueError: If scheme is unknown or positions is not a positive int.
    """
    if scheme not in VALID_SCHEMES:
        raise ValueError(
            f"unknown diversification_scheme: {scheme!r}. "
            f"Valid: {list(VALID_SCHEMES)}"
        )
    if not isinstance(positions, int) or isinstance(positions, bool):
        raise ValueError("diversification_positions must be an int")
    if positions < 1:
        raise ValueError("diversification_positions must be >= 1")


def theoretical_size(scheme: str, positions: int) -> int:
    """Return the DNA-level theoretical diversity of a degenerate library.

    Args:
        scheme: One of NNK, NNS, NNN, trimer.
        positions: Number of randomized positions.

    Returns:
        Integer number of distinct DNA sequences the scheme can encode.
    """
    _validate(scheme, positions)
    return CODONS_PER_POSITION[scheme] ** positions


def functional_size(scheme: str, positions: int) -> int:
    """Return the DNA-level diversity after removing stop-containing variants.

    A variant is functional only if none of its diversified positions is a
    stop codon. The fraction functional at a single position is
    ``1 - STOP_FRACTION[scheme]``. Across ``positions`` independent positions
    the functional fraction is that value raised to the ``positions`` power.

    Args:
        scheme: One of NNK, NNS, NNN, trimer.
        positions: Number of randomized positions.

    Returns:
        Integer estimated count of stop-free DNA variants.
    """
    _validate(scheme, positions)
    total = theoretical_size(scheme, positions)
    functional_fraction = (1.0 - STOP_FRACTION[scheme]) ** positions
    return int(round(total * functional_fraction))


def functional_amino_acid_space(positions: int) -> int:
    """Return the biological ceiling of protein-level diversity.

    Args:
        positions: Number of randomized positions.

    Returns:
        ``20 ** positions``. This is independent of codon scheme and is the
        ceiling that any DNA library can sample at the protein level.
    """
    if not isinstance(positions, int) or isinstance(positions, bool):
        raise ValueError("positions must be an int")
    if positions < 1:
        raise ValueError("positions must be >= 1")
    return 20 ** positions


def stop_free_fraction(scheme: str, positions: int) -> float:
    """Return the fraction of a library free of stop codons.

    Args:
        scheme: One of NNK, NNS, NNN, trimer.
        positions: Number of randomized positions.

    Returns:
        Float in [0, 1].
    """
    _validate(scheme, positions)
    return (1.0 - STOP_FRACTION[scheme]) ** positions
