"""Per-residue relative solvent accessibility (RSA) calculation for Epitope Scout.

Uses the freesasa Python API (freesasa.calcBioPDB) to compute RSA values for
every standard amino acid residue in a Biopython Structure object. RSA is the
fraction of a residue's surface area that is solvent-exposed relative to an
extended reference state (Tien et al. 2013 normalization used by freesasa).

Key design notes:
- Only relativeTotal is used — raw SASA values (Å²) are NOT returned.
- freesasa residue keys are strings ("76"), not integers.
- Non-standard residues and insertion-code residues may be absent from the
  freesasa output; .get() with a 0.0 default prevents KeyError.
- RSA values can exceed 1.0 for highly exposed termini and flexible loops;
  the valid range is [0.0, ~1.5+]. Values above 2.0 indicate a data error.

Exports:
    SURFACE_RSA_THRESHOLD   -- float, default cutoff for "surface" classification
    STANDARD_AA             -- frozenset of 20 standard amino acid three-letter codes
    compute_rsa             -- primary entry point
"""

from __future__ import annotations

# freesasa is imported lazily inside compute_rsa() to reduce worker startup
# memory — importing it at module level caused OOM kills on the free tier.

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# RSA threshold above which a residue is considered solvent-exposed.
# 0.25 is the standard cutoff from Tien et al. 2013 (PMID 24217385).
# Consumers (e.g. patch detection) import this constant rather than
# hardcoding their own threshold.
SURFACE_RSA_THRESHOLD: float = 0.25

# The 20 canonical amino acids. Used to skip HETATM / non-standard residues
# that freesasa may not have entries for.
STANDARD_AA: frozenset[str] = frozenset({
    "ALA", "ARG", "ASN", "ASP", "CYS",
    "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO",
    "SER", "THR", "TRP", "TYR", "VAL",
})


# ---------------------------------------------------------------------------
# Primary entry point
# ---------------------------------------------------------------------------

def compute_rsa(
    structure,
    chain_id: str,
) -> dict[tuple[str, str], float]:
    """Compute per-residue relative solvent accessibility (RSA) for a chain.

    Calls freesasa.calcBioPDB on the full structure (context-dependent SASA),
    then extracts RSA values for the specified chain. RSA is relativeTotal from
    the freesasa residue area object — a value in [0.0, ~1.5] where 1.0 means
    fully exposed relative to the Gly-X-Gly reference state.

    Non-standard residues and insertion-code residues that freesasa does not
    report are assigned RSA 0.0 (treated as buried). This prevents KeyError
    on atypical entries while conservatively excluding them from surface
    classification.

    Args:
        structure: A Biopython Structure object (e.g. from PDBParser.get_structure).
            The full structure is passed to freesasa so that buried residues at
            chain interfaces are correctly accounted for.
        chain_id: Single-character chain identifier (e.g. "A"). Only residues
            belonging to this chain are returned.

    Returns:
        dict mapping (chain_id: str, res_num_str: str) -> float RSA.
        Keys use the string residue sequence number as it appears in the PDB
        file (e.g. ("A", "76")). Returns an empty dict if the chain is not
        found in the freesasa output.

    Raises:
        freesasa.FreeSASAError: If freesasa cannot process the structure
            (e.g. no valid atoms). Propagated to caller without wrapping so
            the cause is visible in tracebacks.
    """
    import freesasa  # noqa: PLC0415 — deferred to reduce worker startup memory

    # Suppress FreeSASA's C-level "guessing atom symbol" warnings.
    # These are harmless (guesses are correct for standard PDB atoms)
    # but spam hundreds of lines per structure, drowning real log output.
    try:
        freesasa.setVerbosity(freesasa.nowarnings)
    except AttributeError:
        pass  # older freesasa versions lack setVerbosity

    # Run freesasa on the entire structure.
    # calcBioPDB returns a 2-tuple: (Result, classifier).
    # We only need the Result object to call .residueAreas().
    freesasa_result, _classifier = freesasa.calcBioPDB(structure)

    # residueAreas() returns a nested dict:
    #   { chain_id_str: { res_num_str: ResidueArea, ... }, ... }
    # ResidueArea.relativeTotal is the RSA as a float.
    all_chain_areas = freesasa_result.residueAreas()

    # Use .get() with an empty dict so that an absent chain returns {} rather
    # than raising KeyError. This also gracefully handles structures where
    # freesasa skips a chain entirely (e.g. all-HETATM chains).
    chain_areas = all_chain_areas.get(chain_id, {})

    rsa_map: dict[tuple[str, str], float] = {}

    for res_num_str, area in chain_areas.items():
        # relativeTotal may theoretically be None for non-standard residues in
        # edge-case freesasa versions; default to 0.0 to be safe.
        rsa_value = area.relativeTotal if area.relativeTotal is not None else 0.0
        rsa_map[(chain_id, res_num_str)] = float(rsa_value)

    return rsa_map
