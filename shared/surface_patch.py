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
Min of 7 reps, chain A of ``static/example/1HEW.pdb`` (129 residues, 1104
atoms), on the development machine while other work was running:

    n_points=100 (the ShrakeRupley default)   ~350 ms per structure
    n_points=960                              ~1.6 s per structure

Total SASA at the default is within 0.42% of the n_points=960 figure on that
structure, so the default is kept. At ~350 ms, fifty rows is ~17 s of CPU and
a hundred rows ~35 s, BEFORE the per-row storage fetch. The web tier dies at
gunicorn's ``timeout`` (120 s by default, see ``gunicorn.conf.py``), and
``shared/exports.py`` already caps structure reads for the same reason. So this
cannot be computed for every row of a results page inside the request: a caller
wanting a value on every row has to precompute. Computing it for ONE design on
demand is affordable.

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
#   floor  1 Å²   1HEW-A 0.08/0.59/0.98   3ave-A 0.08/0.18/0.54
#                 3s7g-A 0.40/0.80/1.00   1igt-A 0.43/0.57/1.00
#   floor 10 Å²   1HEW-A 0.08/0.08/0.08   3ave-A 0.05/0.05/0.06
#                 3s7g-A 0.07/0.09/0.11   1igt-A 0.11/0.14/0.14
#
# At 1 Å² the figure reaches 98-100% of the total on two of the four -- one
# cluster covering the whole surface, which is "total exposed apolar area"
# under another name -- and it swings by an order of magnitude across a 1 Å
# change in a cutoff nobody can derive. At 10 Å² nothing percolates on any of
# the four and the figure barely moves with the cutoff. A free carbon's sphere
# is ~121 Å², so 10 Å² is roughly 8% relative exposure.
#
# test_the_patch_does_not_percolate_on_any_committed_structure pins the
# non-percolation property, so loosening this back into the percolating regime
# fails rather than quietly returning a total.
EXPOSED_ATOM_SASA_A2 = 10.0

# Two exposed apolar atoms are in the same patch if their centres are within
# this distance, in Å. CHOSEN: a little over the 3.4-4.0 Å contact distance of
# two apolar atoms. The figure is not independent of it -- on the table above
# the 4.0-to-5.0 spread is nil on 1HEW and up to ~1.7x on the two smallest
# structures -- so this value is part of the definition of the number, not a
# detail. Any copy quoting an area must not imply more precision than that.
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
    rule reads, so that an exposed motif ranks above a buried one. It is
    ``None`` for a residue whose type is not one of the twenty (a modified
    residue Biopython mapped into the sequence still has no maximum here), so
    a caller must treat ``None`` as "exposure unknown" and not as buried.

    ``None`` -- never a zero, never an exception -- when the bytes do not
    parse, when none of ``designed_chains`` is present in the file, or when no
    protein residue survives. A zero would read as "measured, and flat", which
    is a different claim from "not measured". Callers show nothing instead.

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

    residues = {
        str(chain.get_id()).strip(): [r for r in chain if _is_protein_residue(r)]
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
