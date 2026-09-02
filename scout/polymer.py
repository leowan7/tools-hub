"""Shared polymer-residue selection for the geometric scout paths.

WHY THIS MODULE EXISTS
----------------------
Selenomethionine (MSE) and selenocysteine (SEC) are ordinary polymer residues
that the PDB deposits as HETATM. A filter that gates on the Biopython hetflag
alone drops them, so an MSE sitting in an epitope is invisible to contact and
interface detection. Fixing that by simply admitting the two names introduces
two NEW defects, both of which cost more than the bug being fixed:

1. DOUBLE COUNTING. Partial Se incorporation deposits MET and MSE at one
   residue number as two altlocs. Their hetflags differ (" " vs "H_MSE"), so
   Biopython yields two separate Residue objects and a naive fix reports that
   position twice. ``scout/interfaces.py`` thresholds and publishes a LIST
   length, so a duplicate can push an interface past _MIN_CONTACT_RESIDUES --
   the filter whose stated job is rejecting crystal contacts and incidental
   chain proximity.

   (Note the list was never fully unique: insertion codes 100/100A already
   collapse to the same reported number, because only ``id[1]`` is emitted.
   Admitting MSE widens that pre-existing weakness rather than creating it.)

2. FREE LIGANDS. A lone MSE or SEC in its own chain is chemically identical to
   a chain link at the residue level. Admitted blind, it fabricates a
   protein-protein interface out of nothing -- eight phantom contact residues
   on a synthetic probe, five on one lifted out of 1BJ1 -- which then propagates into competition_score and prints a
   confident false claim that the epitope sits in a natural PPI interface.

``scout/parser.py`` solved (1) for chain lengths and explicitly documented that
it CANNOT solve (2), because it has no connectivity test. This module has the
coordinates, so it does both, and lives in one place because a guard that
exists in only one of four call sites is exactly how (1) got reintroduced.

WHAT THIS IS NOT
----------------
Not a replacement for ``scout/parser.py::_is_polymer_residue`` or
``scout/scoring.py::_is_pydssp_polymer_residue``. Those answer "is this residue
an amino acid" for subsystems with different needs, and parser.py's docstring
argues -- correctly -- against collapsing them into one predicate. This module
answers a narrower question that only the geometric paths ask: "which residues
of this chain should contribute atoms, counted once each".

Nor is it for the patch-construction path. ``scout/pipeline.py`` gates on
``scout/sasa.py``'s 20-name set, and ``scout/scoring.py`` gives two distinct
reasons for rejecting MSE that are easy to conflate: freesasa has no radii for
MSE (that one is about ``scout/sasa.py``), and an epitope centred on a
selenomethionine is not something the scoring model was built for (that one is
about these gates). The second is a real design choice; do not route patch
construction through here on the strength of the first.

Those gates are NOT neutral, though, and this module does not fix them: the
same loop builds the atom set used for burial counting, so an MSE-adjacent
patch loses exactly the 8 heavy atoms of each MSE dropped. Two independent
reviews measured the shift on 1B24 and reported different absolute pairs
(76/84, and 73/81 with 93/101) under different SASA backends; the delta of 8
atoms per MSE is what both agree on and what follows from the code. Ranking
normalisation is min-max across patches, so it is not a uniform offset. That is a separate, unfixed bias of the
same family as ``scout/accessibility.py``'s two gates.
"""

from __future__ import annotations

# The two names, and only these two. shared/pdb_inspect.py::MODRES_EQUIV
# carries nine for the campaign path; two is a deliberately narrower choice for
# scout, pinned by the whitelist tests.
MODIFIED_AA: frozenset[str] = frozenset({"MSE", "SEC"})

# C(i)->N(i+1) in a real peptide bond is ~1.33 A. Biopython's PPBuilder
# defaults to 1.8 A; 2.0 is 11% looser, to tolerate modest refinement error.
# The upper bound is a real constraint but it is NOT what stops the double
# count: the (resseq, icode) dedupe collapses a MET/MSE altloc pair whatever
# the threshold is, verified at 2.0, 3.0 and 10.0 A. What the bound does buy is
# margin against a self-certifying junction -- intra-residue N...C measures
# 2.27-2.57 A on 1B24 chain A, so 2.0 clears the closest case by 0.27 A.
_PEPTIDE_BOND_MAX_ANGSTROM = 2.0

# CA--CA in consecutive residues is 3.78-3.82 A in trans peptides, versus
# ~1.33 A for the C->N bond. Dividing the CA distance by this factor lets one
# threshold serve both tests without a second constant to keep in sync.
#
# 1.95, not 2.1: the scaled threshold is 2.0 * scale, so 2.1 accepted CA--CA up
# to 4.2 A and admitted free ligands sitting 4.0 A from a chain end -- a
# distance no peptide bond produces. 1.95 puts the ceiling at 3.9 A, which
# still clears genuine 3.8 A chain spacing with margin while rejecting 4.0.
_CA_TRACE_SCALE = 1.95


def _bond_length(residues: list, idx_a: int, idx_b: int) -> float:
    """Length of the peptide bond that would join a to b, or infinity.

    Measures the FORWARD pair only -- ``a["C"] -> b["N"]`` -- because that is
    the bond. Callers always pass (upstream, downstream); both sweeps are
    pinned on that, since reversing either one admits a free ligand.

    Falls back to CA--CA when the forward pair cannot be measured, so a CA-only
    trace (low-resolution X-ray, EM) still resolves: consecutive CA atoms sit
    3.78-3.82 A apart and a stray ligand does not.

    THE REVERSE DIRECTION IS NOT CONSULTED, and three separate defects came
    from consulting it. C(i+1)...N(i) runs 4.1-6.1 A on genuinely bonded pairs,
    so it never rescues a real bond -- it is pure downside:

      * Taken as the answer, it made admission non-monotone: a CA-only MSE was
        admitted, and ADDING its C atom let the reverse measure and drop it.
      * Read as "we could measure something", it let the CA proxy override a
        forward measurement that had already answered.
      * Folded into a min, it admitted a free ligand 6.0 A forward and 8.0 A
        CA--CA whose C happened to sit 1.5 A from the previous residue's N --
        a clash, not a bond.

    Order-independence is deliberately NOT a property here. It was, when both
    directions were measured, and that is exactly what cost the three defects
    above. A file whose records invert two list-adjacent residues will drop a
    genuine link; no generator observed produces that layout.
    """
    a, b = residues[idx_a], residues[idx_b]
    try:
        return a["C"] - b["N"]
    except KeyError:
        pass
    try:
        return (a["CA"] - b["CA"]) / _CA_TRACE_SCALE
    except KeyError:
        return float("inf")


def polymer_residues(chain) -> list:
    """Residues of ``chain`` that should contribute atoms, deduped by position.

    Admits every blank-hetflag residue, plus MSE/SEC that are peptide-linked
    back to one of them -- transitively, so a fully substituted stretch inside a
    real chain still counts, while a free SeMet peptide never seeds itself.

    DEDUPLICATION IS ON ``(resseq, icode)``, which is what makes a MET/MSE
    altloc pair count once. It does NOT make the callers' reported lists unique:
    they emit the sequence number alone, so residues 100 and 100A still appear
    as two entries spelled "100". That predates this module -- see the header.

    Args:
        chain: A Biopython Chain.

    Returns:
        List of Residue objects in chain order.
    """
    residues = list(chain)
    # A blank hetflag IS the definition of a polymer residue here; those seed
    # everything else. Note this admits blank-hetflag nucleotides too, exactly
    # as the gate it replaced did.
    admitted = [residue.id[0] == " " for residue in residues]
    candidate = [
        not admitted[i] and residue.resname.strip() in MODIFIED_AA
        for i, residue in enumerate(residues)
    ]

    # Two linear sweeps reach the same fixpoint as iterating to convergence,
    # because admission only ever propagates along the residue order: a run
    # anchored on its left is caught going forward, on its right going back.
    for i in range(len(residues)):
        if candidate[i] and i and admitted[i - 1]                 and _bond_length(residues, i - 1, i) < _PEPTIDE_BOND_MAX_ANGSTROM:
            admitted[i] = True
    for i in range(len(residues) - 2, -1, -1):
        if candidate[i] and admitted[i + 1]                 and _bond_length(residues, i, i + 1) < _PEPTIDE_BOND_MAX_ANGSTROM:
            admitted[i] = True

    seen_positions = set()
    kept = []
    for i, residue in enumerate(residues):
        if not admitted[i]:
            continue
        position = residue.get_id()[1:]  # (resseq, icode)
        if position in seen_positions:
            continue
        seen_positions.add(position)
        kept.append(residue)
    return kept
