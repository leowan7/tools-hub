"""Largest solvent-exposed hydrophobic patch, MEASURED on a stored structure.

Leo's ruling, 2026-11-16: stickiness is a surface property only. A hydrophobic
stretch is reportable only once we have measured that it is solvent-exposed, so
the sequence-window screen this repo used to display was deleted (see
``shared/developability/score.py``) and replaced by the figure here: per-atom
solvent exposure, apolar exposed atoms clustered by contact, and the largest
cluster's area in square angstroms.

WHAT THE NUMBER IS AND IS NOT
-----------------------------
It is an area in Å² computed by Biopython's Shrake-Rupley implementation
(``Bio.PDB.SASA.ShrakeRupley``) over the DESIGNED chains only, with the other
chains removed first. ``freesasa`` is deliberately not used: it has no build on
the development machine.

Measuring the designed chains in isolation models the FREE binder, which is the
state developability cares about -- what sticks to a column or to itself is the
surface the molecule presents when it is not bound. Measuring the whole complex
instead would bury the paratope under the target and report a smaller patch for
a binder that grips its target well, which is backwards for this purpose. The
cost is that a patch reported here may be partly or wholly the binding
interface; that is the intended reading, not an error.

It inherits every error of the structure it is measured on. For a predicted
complex that is the model's error, which is not quantified here and is not
small: no copy may describe this as a measurement of the physical molecule.
It is a measurement of a prediction.

MEASURED COST (not an estimate)
-------------------------------
Chain A of ``static/example/1HEW.pdb`` -- 129 residues and 1001 heavy protein
atoms, which is what reaches ShrakeRupley after the detach loops below drop
the 103 waters and any hydrogens. Whole call: parse, detach, SASA, cluster.

    n_points=100 (the ShrakeRupley default)   ~180 ms on an idle machine,
                                              270-350 ms under load
    n_points=960                              1.5-2.6 s per structure

THE SPREAD IS MACHINE LOAD, NOT INPUT, and the history is worth keeping
because it misled this file twice in opposite directions. Four measurement
sessions on the same call and the same bytes: min-of-7 gave 179 ms; a later
25-rep run during a full test suite gave min 274 / median 353 ms; an
independent reviewer measured min 178 / median 180 ms; a final 25-rep run gave
min 178 / median 180 / p90 188 / max 488 ms. So ~180 ms is the REPRODUCIBLE
figure and the 270-350 ms band was contention -- the opposite of what an
earlier version of this block concluded when it called 180 ms "the quietest
moment rather than the cost". The max of 488 ms is the tail a shared box
gives you.

Quote it as "a few hundred milliseconds" and never to three digits. Nothing
below depends on which end of the range is right.

Total SASA at the default is within 0.29% of the n_points=960 figure on that
structure, so the default is kept. Scaling is close to linear in atom count:
~1.3 ms per residue measured from 40 to 100 residues of the same chain, so a
designed binder of 60-120 residues sits at the low end of the range above.

Even at the fast end fifty rows is ~9 s of CPU and a hundred rows ~18 s; at
the loaded end, ~15 s and ~30 s. Taking the slower figure, BEFORE any
storage fetch -- and the results page fetches no structures at all today, so a
per-row figure would add a Storage round trip per row on top of the CPU. The
web tier dies at gunicorn's ``timeout`` (120 s by default, see
``gunicorn.conf.py``), and ``shared/exports.py`` already caps structure reads
for the same reason. So this cannot be computed for every row of a results
page inside the request: a caller wanting a value on every row has to
precompute. Computing it for ONE design on demand is affordable. That
conclusion holds across the whole measured range, which is why the spread
above does not change it.

Those timings are from the development machine under contention, not from
production, and the minimum of several reps rather than the mean -- the spread
between reps was larger than the difference between settings.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Apolar atoms. Carbon and sulfur, the usual definition of the apolar surface
# in a SAP/patch calculation -- nitrogen and oxygen are polar and excluded.
# Element symbols as Biopython reports them on ``Atom.element`` (uppercase).
HYDROPHOBIC_ELEMENTS = frozenset({"C", "S"})

# An atom counts as exposed above this much solvent-accessible area, in Å².
#
# CHOSEN, but not arbitrary: below about 10 Å² the apolar surface PERCOLATES
# and "largest patch" stops meaning anything. Measured on the four structures
# committed to this repo, as largest-patch area over total exposed apolar
# area, at contact cutoffs of 4.0/4.5/5.0 Å:
#
#   floor  1 Å²   1HEW-A 0.20/0.90/1.00   3ave-A 0.12/0.56/1.00
#                 3s7g-A 0.40/0.80/1.00   1igt-A 0.43/0.57/1.00
#   floor 10 Å²   1HEW-A 0.05/0.06/0.06   3ave-A 0.03/0.03/0.05
#                 3s7g-A 0.07/0.09/0.11   1igt-A 0.11/0.13/0.13
#
# RE-DERIVED TWICE, because two fixes to surface_profile each invalidated the
# table the thresholds had been chosen on. The first figures here were measured
# with crystallographic waters occluding the surface, and the second with
# hydrogens still occluding it; both are now detached. Waters flattered the
# floor-1 row on the two structures carrying them (1HEW-A at 4.5 Å read 0.59
# and is really 0.90; 3ave-A 0.18, really 0.56), and hydrogens moved only the
# 1igt-A floor-10 row, by 0.01. Against the original table the floor-10 row has
# moved by at most 0.03 (1HEW-A at 4.0 Å, 0.08 to 0.05); the direction is that
# floor 1 is WORSE than first recorded, not better. The conclusion the
# thresholds were chosen on survived both re-derivations unchanged.
#
# At 1 Å² the figure reaches the whole surface on all four at a 5 Å cutoff --
# one cluster covering everything, which is "total exposed apolar area" under
# another name -- and it swings by up to 8x across a 1 Å change in a cutoff
# nobody can derive (3ave-A, 0.12 at 4.0 Å to 1.00 at 5.0 Å). At 10 Å² nothing
# percolates on any of the four and the figure barely moves with the cutoff. A
# free carbon's sphere is ~121 Å², so 10 Å² is roughly 8% relative exposure.
#
# test_the_patch_does_not_percolate_on_any_committed_structure pins the
# non-percolation property, so loosening this back into the percolating regime
# fails rather than quietly returning a total.
EXPOSED_ATOM_SASA_A2 = 10.0

# Two exposed apolar atoms are in the same patch if their centres are within
# this distance, in Å. CHOSEN: a little over the 3.4-4.0 Å contact distance of
# two apolar atoms. The figure is not independent of it -- across 4.0 to 5.0 Å
# the floor-10 row of the table above spreads by 1.2x (1HEW-A, 0.05 to 0.06),
# 1.2x (1igt-A), 1.6x (3s7g-A) and 1.7x (3ave-A) -- so this value is part of
# the definition of the number, not a detail. Any copy quoting an area must
# not imply more precision than that.
#
# Both figures in the previous version of this sentence were wrong: it said the
# spread was "nil on 1HEW", which was true only while waters were occluding
# that structure, and attributed the widest spread to "the two smallest
# structures", when 3ave-A has the largest total apolar area of the four and
# the widest spread of them. The spread does not track structure size.
PATCH_CONTACT_A = 4.5


def _parse_model(data: bytes):
    """First model of PDB or mmCIF bytes, or ``None``.

    Format detection and its two traps are taken from
    ``shared/exports.py::structure_chain_sequences``, which carries the
    evidence: mmCIF is a file with a LINE starting ``data_`` rather than one
    whose first characters are ``data_`` (PXDesign's files open with ``#``
    lines before it), and ``PDBParser`` handed an mmCIF returns a structure
    with no model instead of raising.

    Every exception is swallowed and logged. A caller gets ``None`` and shows
    nothing: a structure we cannot read is not a reason to fail a page.
    """
    from Bio.PDB import MMCIFParser, PDBParser  # noqa: PLC0415

    try:
        text = data.decode("utf-8", errors="replace")
        parser = (
            MMCIFParser(QUIET=True)
            if re.search(r"^\s*data_\S", text, re.M)
            else PDBParser(QUIET=True)
        )
        structure = parser.get_structure("s", io.StringIO(text))
        return next(iter(structure), None)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning("surface patch: structure did not parse (%s)", type(exc).__name__)
        return None


# Empirical maximum solvent-accessible area per residue, Å², from Tien et al.
# 2013 (PLoS ONE 8:e80635), the "theoretical" column. Used only to turn an
# absolute per-residue SASA into a RELATIVE one: a buried tryptophan can carry
# more absolute area than an exposed glycine, so an absolute cut-off would call
# large residues exposed and small ones buried regardless of where they sit.
_MAX_ASA_A2 = {
    "A": 129.0, "R": 274.0, "N": 195.0, "D": 193.0, "C": 167.0,
    "E": 223.0, "Q": 225.0, "G": 104.0, "H": 224.0, "I": 197.0,
    "L": 201.0, "K": 236.0, "M": 224.0, "F": 240.0, "P": 159.0,
    "S": 155.0, "T": 172.0, "W": 285.0, "Y": 263.0, "V": 174.0,
}

# A residue counts as solvent-exposed above this relative accessibility
# (its SASA over the maximum above). CHOSEN: 0.25 is the conventional
# exposed/buried cut in the RSA literature. Nothing here measures it.
EXPOSED_RESIDUE_RSA = 0.25


def _is_protein_residue(residue) -> bool:
    """Whether ``residue`` is one ``structure_chain_sequences`` would read.

    Same table, so a position into the sequence that function returns indexes
    the same residue this module measures. Waters, ligands and ions are out.
    """
    from Bio.Data.PDBData import protein_letters_3to1_extended  # noqa: PLC0415

    return residue.get_resname().strip() in protein_letters_3to1_extended


def _is_hydrogen(atom) -> bool:
    """Whether ``atom`` is a hydrogen or deuterium.

    Reads ``element`` where the file supplies it and falls back to the atom
    NAME, because the element column is optional in PDB and a file written
    without it leaves ``element`` empty or guessed. The fallback uses the PDB
    convention that an H atom's name begins with H, or with a digit followed by
    H ("1HB", "2HG1"); a leading-digit name is why the test is not simply
    ``startswith("H")``. Deliberately conservative: no standard amino-acid
    heavy atom is named H* (they are N, CA, C, O, CB, SG and so on), so this
    cannot drop a carbon or sulfur by mistake.
    """
    element = (atom.element or "").strip().upper()
    if element:
        return element in {"H", "D"}
    name = atom.get_name().strip().upper()
    return name.startswith(("H", "D")) or (
        len(name) > 1 and name[0].isdigit() and name[1] in {"H", "D"}
    )


def surface_profile(
    data: bytes, designed_chains: Iterable[str]
) -> Optional[dict]:
    """Measured exposure of ``designed_chains``, or ``None``.

    Returns::

        {
          "largest_patch_a2": float,   # the headline figure
          "patch_atom_count": int,     # apolar exposed atoms in that patch
          "total_apolar_a2": float,    # exposed apolar area over all chains
          "residue_sasa": {chain_id: [per-residue Å², in chain order]},
          "residue_rsa":  {chain_id: [per-residue 0-1, in chain order]},
        }

    Both per-residue lists carry one entry per protein residue in file order,
    so a 1-based position into the chain's sequence indexes ``[position - 1]``.

    ``residue_rsa`` is the relative accessibility -- SASA over the residue
    type's maximum (:data:`_MAX_ASA_A2`) -- and is what the liability severity
    rule reads, so that an exposed motif ranks above a buried one.

    A ``None`` entry means "exposure unknown" and a caller must not read it as
    buried. IN PRACTICE NO ENTRY IS EVER ``None``, and the earlier claim here
    that a modified residue has no maximum was simply wrong. Every residue
    name :func:`_is_protein_residue` admits is a key of
    ``protein_letters_3to1_extended``, every value of that table is one of the
    twenty canonical letters, and every one of those twenty is a key of
    :data:`_MAX_ASA_A2` -- so the lookup below always finds a maximum. The
    ``None`` branch is defensive against a future edit to either table, not a
    case real structures reach.

    WHAT ACTUALLY HAPPENS TO A MODIFIED RESIDUE is that it is normalised to its
    parent type and scored against that type's maximum: selenomethionine (MSE)
    maps to M and is divided by methionine's 224 A2. That is an approximation,
    not an exact figure for the modified residue, and it is the reason a
    selenomethionine-rich structure's RSA should not be read to three digits.

    ``None`` -- never a zero, never an exception -- when the bytes do not
    parse, when none of ``designed_chains`` is present in the file, or when no
    protein residue survives. A zero would read as "measured, and flat", which
    is a different claim from "not measured". Callers show nothing instead.

    KNOWN LIMITATION, not a bug to work around: a structure whose chain column
    is blank parses to a chain whose id is ``" "``, and no caller can name it.
    Both ``[" "]`` and ``[""]`` strip to an empty set, so such a file always
    returns ``None``. That is the safe direction -- nothing shown rather than
    a figure attributed to the wrong chain -- and it is left as is because the
    eligible tools all label their chains. It is recorded here so a future
    reader does not mistake the silence for a parse failure.

    The patch area is the SUM of its member atoms' SASA, not a projected or
    fitted area.
    """
    wanted = {str(c).strip() for c in designed_chains if str(c).strip()}
    if not wanted:
        return None

    model = _parse_model(data)
    if model is None:
        return None

    # Drop every chain that is not a designed one BEFORE computing, so the
    # exposure is the free binder's and not the complex's (see module docstring).
    for chain in list(model):
        if str(chain.get_id()).strip() not in wanted:
            model.detach_child(chain.get_id())
    if not list(model):
        logger.warning(
            "surface patch: none of the designed chains %s are in the structure",
            sorted(wanted),
        )
        return None

    # Drop non-protein residues BEFORE computing, not just when summing.
    #
    # ShrakeRupley occludes with every atom in the model it is handed, so a
    # crystallographic water left in a designed chain buries the protein
    # surface underneath it -- and a water is solvent, which is exactly what
    # this figure is supposed to be measuring the accessibility TO. Measured on
    # static/example/1HEW.pdb chain A, which carries 103 HOH records in the
    # designed chain: leaving them in reports total_apolar_a2 1277.67 against
    # 2032.02 with them gone, largest_patch_a2 99.03 against 114.72, and 23 of
    # 129 residues fall the wrong side of EXPOSED_RESIDUE_RSA (residue 37 reads
    # 0.126 rather than 0.436). Every one of those errors is in the direction
    # of calling an exposed residue buried, which in find_liabilities means
    # failing to escalate a liability that is in fact reachable.
    #
    # A predicted structure carries no waters, so this changes nothing for the
    # design tools; it is the uploaded-or-crystal case it fixes. Held by
    # test_waters_in_the_designed_chain_do_not_bury_the_surface.
    #
    # HYDROGENS GO FOR THE SAME REASON, and it is the same bug: an H occludes
    # the carbon or sulfur it hangs off, and only C/S above EXPOSED_ATOM_SASA_A2
    # are counted, so a protonated file reports a smaller patch than the same
    # structure without hydrogens. That makes the figure depend on whether
    # whoever produced the file ran a protonation step, which is not a property
    # of the molecule. Measured on tests/fixtures/rfd4/1igt_fv_trimmed.pdb,
    # which carries 351 polar hydrogens: chain B alone read total_apolar_a2
    # 2237.40 and largest_patch_a2 138.88 with them in, against 2434.32 and
    # 144.92 with them stripped. A fully protonated file is far worse -- one to
    # three H on every carbon rather than polar-only.
    #
    # This also makes the percolation table above describe one surface rather
    # than two. THREE of its four structures carry no hydrogens at all (1HEW 0,
    # 3ave 0, 3s7g 0, counted on the element column) and the fourth, 1igt,
    # carries 351 -- so before this detach the table mixed heavy-atom rows with
    # one protonated row. Stripping them makes every row a heavy-atom surface,
    # which is what the thresholds are set on. It moved only the 1igt-A floor-10
    # row, by 0.01, as the re-derivation note above records.
    # Held by test_hydrogens_do_not_bury_the_surface.
    for chain in model:
        for residue in [r for r in chain if not _is_protein_residue(r)]:
            chain.detach_child(residue.get_id())
    for chain in model:
        for residue in chain:
            for atom in [a for a in residue if _is_hydrogen(a)]:
                residue.detach_child(atom.get_id())

    residues = {
        str(chain.get_id()).strip(): list(chain)
        for chain in model
    }
    if not any(residues.values()):
        logger.warning("surface patch: no protein residue in the designed chains")
        return None

    from Bio.PDB.SASA import ShrakeRupley  # noqa: PLC0415

    try:
        # level="A" so every atom carries its own .sasa; residue and patch
        # figures are summed from those rather than computed separately.
        ShrakeRupley().compute(model, level="A")
    except Exception as exc:  # noqa: BLE001 -- same contract as _parse_model
        logger.warning("surface patch: SASA failed (%s)", type(exc).__name__)
        return None

    from Bio.Data.PDBData import protein_letters_3to1_extended  # noqa: PLC0415

    residue_sasa = {
        cid: [round(sum(float(a.sasa) for a in r), 2) for r in rs]
        for cid, rs in residues.items()
    }
    residue_rsa = {
        cid: [
            None
            if (
                maximum := _MAX_ASA_A2.get(
                    protein_letters_3to1_extended.get(r.get_resname().strip(), "")
                )
            )
            is None
            else round(min(1.0, area / maximum), 3)
            for r, area in zip(rs, residue_sasa[cid])
        ]
        for cid, rs in residues.items()
    }

    exposed = [
        atom
        for rs in residues.values()
        for r in rs
        for atom in r
        if (atom.element or "").strip().upper() in HYDROPHOBIC_ELEMENTS
        and float(atom.sasa) > EXPOSED_ATOM_SASA_A2
    ]
    total_apolar = round(sum(float(a.sasa) for a in exposed), 2)
    if not exposed:
        # Parsed and measured, and there is genuinely no exposed apolar atom.
        # That IS a measurement, so it is reported as a zero-area patch rather
        # than as None.
        return {
            "largest_patch_a2": 0.0,
            "patch_atom_count": 0,
            "total_apolar_a2": 0.0,
            "residue_sasa": residue_sasa,
            "residue_rsa": residue_rsa,
        }

    largest_area, largest_count = _largest_cluster(exposed)
    return {
        "largest_patch_a2": round(largest_area, 2),
        "patch_atom_count": largest_count,
        "total_apolar_a2": total_apolar,
        "residue_sasa": residue_sasa,
        "residue_rsa": residue_rsa,
    }


def _largest_cluster(atoms: list) -> tuple[float, int]:
    """``(summed SASA, atom count)`` of the largest contact-linked cluster.

    Biopython's ``NeighborSearch`` (a KD-tree) finds the touching pairs, so
    this is not an O(n^2) scan over coordinates; the union-find below is over
    the pairs it returns.
    """
    from Bio.PDB import NeighborSearch  # noqa: PLC0415

    index = {id(a): i for i, a in enumerate(atoms)}
    parent = list(range(len(atoms)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in NeighborSearch(atoms).search_all(PATCH_CONTACT_A):
        ra, rb = find(index[id(a)]), find(index[id(b)])
        if ra != rb:
            parent[ra] = rb

    areas: dict[int, float] = {}
    counts: dict[int, int] = {}
    for i, atom in enumerate(atoms):
        root = find(i)
        areas[root] = areas.get(root, 0.0) + float(atom.sasa)
        counts[root] = counts.get(root, 0) + 1

    best = max(areas, key=lambda r: areas[r])
    return areas[best], counts[best]
