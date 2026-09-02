"""Glycosylation sequon detection and proximity scoring for feasibility assessment.

N-linked glycosylation occurs at Asn residues in the NxS/T motif (where x is
not Pro). Glycan chains near a binder epitope can sterically occlude the
binding interface, reducing hit rates in de novo design campaigns.

This module detects sequon positions on a target chain and scores how close
they are to a candidate epitope patch. Closer glycans = higher risk = lower
feasibility score.

Exports:
    detect_glycosylation_sequons  -- find NxS/T motifs on a chain
    score_glycan_proximity        -- 0-1 score based on nearest sequon distance
"""

from __future__ import annotations

import logging

import numpy as np

from scout.patches import get_cb_coord

logger = logging.getLogger(__name__)

# Standard one-letter to three-letter mapping for sequon detection.
# MSE/SEC are spelled as scout/epitope_db.py spells them, so the same
# structure yields the same letters in both modules; see _MODIFIED_AA.
_THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "MSE": "M", "SEC": "C",
}

# The two modified residues the REST OF THE PACKAGE already treats as amino
# acids -- scout/parser.py STANDARD_AA, scout/epitope_db.py _THREE_TO_ONE,
# scout/scoring.py _MODIFIED_AA all hold exactly these two. Deliberately not
# "every modified residue that is really an amino acid": see the scope note
# at the end. Matched on resname ALONE, without the hetflag, exactly as
# scout/scoring.py::_is_pydssp_polymer_residue does -- the PDB records them as
# HETATM so Biopython stamps an "H_<resname>" hetflag, but design and
# refinement pipelines often re-emit them as ATOM, and both spellings mean the
# same chemistry.
#
# The hetflag gate STAYS on the canonical 20. There is no peptide-bond
# continuity test here, so relaxing it wholesale would thread free ligands
# into the sequence -- pinned by
# test_a_ligand_cannot_enter_the_polymer_sequence, which has to SPLICE an
# interior het-recorded GLY in to see it: every heteroatom in the repo
# fixtures is a non-amino-acid sitting past the last polymer ATOM line, so the
# obvious version of that test passes with the gate deleted.
#
# This is a scoring fix, not a cosmetic one. The list below is indexed
# POSITIONALLY at i, i+1, i+2, so a dropped in-polymer residue does not leave
# a hole -- it CLOSES the gap and welds two sequence-distant residues
# adjacent. It therefore fails BOTH ways, which a MET-ized control shows
# minimally (MSE is chemically MET, so the control must agree):
#
#     N-MSE-T        (a real sequon, x=MSE)  -> []             MISSED
#     N-MET-T        (control)               -> [("N-M-T", 1)]
#     N-MSE-MSE-A-S  (no real sequon)        -> [("N-A-S", 1)] FABRICATED
#     N-MET-MET-A-S  (control)               -> []
#
# MEASURED on 597 real SeMet depositions sampled across RCSB's 10278
# MSE-bearing entries: 1542 MSE-bearing chains, 8369 MSE, 1625 control
# sequons. The old selector missed 40 real sequons and fabricated 64, wrong on
# 90 of 1542 chains (5.8%); this version matches the control on all 1625 with
# zero differences. That zero is not self-certifying: re-running with the fix
# removed puts it at 104. Sequons feed score_glycan_proximity and glycan_risk
# carries 0.15 of the composite (scout/feasibility.py), and a fabricated
# sequon also fabricates the user-facing warning gated at feasibility.py:165.
# Sampling every residue CB on the 90 affected chains as a stand-in for an
# epitope centroid, the composite moved at 8086 of 26898 of them, by a median
# of 0.052 and by the full 0.15 at worst. Read that median as "over centroids
# that moved", not per chain, and the CB population as a proxy for the real
# patch centroids of scout/pipeline.py.
#
# Note what that 5.8% is NOT. The nine-chain SeMet corpus behind the
# scoring.py fix shows ZERO difference here, and the null is uninformative: it
# holds 12 control sequons, so at the rate measured above it expects well
# under one affected site on any basis (0.41 per residue, 0.61 per chain, 0.77
# per sequon) and a null is near a coin flip even if the fix is entirely real.
# A null on that corpus measures the corpus.
#
# TWO LIMITS, both deliberate.
#
# (1) SEC HAS NO FIELD MEASUREMENT. The corpus above was selected on MSE and
# contains zero SEC residues, so the numbers say nothing about it; SEC rests
# on the unit tests and on being chemically CYS. It is far rarer than MSE.
#
# (2) THE OTHER MODIFIED RESIDUES ARE STILL DROPPED, and weld exactly the same
# way. Over the same corpus, 95 non-whitelisted residues carrying a full
# N/CA/C backbone are peptide-bonded into a chain -- CSO 19, MLY 17, LLP 10,
# CSS 9, KCX 7, MLZ 7, OCS 6, SEP 5 and a tail. Widening this set ALONE would
# be wrong: it would put glycan.py's residue list out of step with the
# residue_count the user is shown and with scout/sasa.py, which is what patch
# construction and the occlusion cloud filter on. That belongs in one
# package-wide change, not here.
#
# Residual risk, accepted: a FREE MSE in solvent has the same resname and the
# same backbone atoms as one in the polymer, so it is now threaded in. Over
# the corpus, 8 of 8369 are bonded to neither neighbour and NONE of those sits
# mid-list -- all are at a list end, where the window is provably harmless
# because a trailing MSE reads as x="M" and never as S/T. The MET-ized control
# shares this choice and so can never reveal it; the count above is a direct
# measurement, not a control result.
#
# Same root cause as _MODIFIED_AA and _ScoutPPBuilder in scout/scoring.py (the
# pydssp backbone and the phi/psi peptide); pinned by the MSE/MET control arms
# in tests/test_glycan.py.
_MODIFIED_AA = frozenset({"MSE", "SEC"})


def detect_glycosylation_sequons(chain) -> list[dict]:
    """Find N-linked glycosylation sequons (N-x-S/T, x != P) on a chain.

    Args:
        chain: A Biopython Chain object.

    Returns:
        List of dicts, each with keys:
            resnum   -- int, residue number of the Asn
            resname  -- str, three-letter code ("ASN")
            cb_coord -- np.ndarray (3,) or None if Cb/Ca missing
            motif    -- str, e.g. "N-K-T" showing the tripeptide
    """
    # Deduplicated on (resseq, icode). Partial Se incorporation deposits MET and
    # MSE at ONE residue number as two altlocs whose hetflags differ (" " vs
    # "H_MSE"), so Biopython yields two Residue objects and both pass the test
    # above. This list is then indexed POSITIONALLY at i, i+1, i+2, so the twin
    # shifts the window and a real sequon is missed: an N-[MET/MSE]-T chain
    # reads N-M-M-T and reports nothing. Admitting MSE without this dedupe made
    # that case WORSE than the gate it replaced, which dropped the HETATM twin
    # and found the sequon. scout/parser.py and scout/polymer.py carry the same
    # guard for the same reason.
    # Collapse ONLY an ADJACENT residue at the same (resseq, icode). That is
    # what a MET/MSE altloc twin is: partial Se incorporation writes both
    # spellings of ONE residue, so Biopython yields two Residue objects side by
    # side. Anything else sharing a residue number -- a duplicate-numbered
    # segment in a fusion construct, a free ligand written at the file head --
    # is a DIFFERENT residue and must keep its own slot in this list, because
    # the list is indexed positionally at i, i+1, i+2.
    #
    # Resolving a collision by hetflag alone, without asking whether the two
    # are the same residue, was wrong in both directions: a PRO sharing the
    # number from 200 A away evicted a real in-polymer MSE and deleted a true
    # N-x-S/T sequon, and a free MSE at the file head hoisted a real ASN into
    # its slot and FABRICATED one. Adjacency is the cheap test that separates
    # a twin from a coincidence.
    standard_residues = []
    for r in chain.get_residues():
        if not (r.resname.strip() in _MODIFIED_AA
                or (r.id[0] == " " and r.resname.strip() in _THREE_TO_ONE)):
            continue
        if standard_residues:
            _previous = standard_residues[-1]
            if _previous.get_id()[1:] == r.get_id()[1:]:
                # An altloc twin of the residue just kept. Prefer the
                # blank-hetflag spelling, but keep exactly one either way.
                if _previous.id[0] != " " and r.id[0] == " ":
                    standard_residues[-1] = r
                continue
        standard_residues.append(r)

    sequons = []
    for i in range(len(standard_residues) - 2):
        r0, r1, r2 = standard_residues[i], standard_residues[i + 1], standard_residues[i + 2]
        aa0 = _THREE_TO_ONE.get(r0.resname.strip(), "?")
        aa1 = _THREE_TO_ONE.get(r1.resname.strip(), "?")
        aa2 = _THREE_TO_ONE.get(r2.resname.strip(), "?")

        if aa0 == "N" and aa1 != "P" and aa2 in ("S", "T"):
            sequons.append({
                "resnum": r0.id[1],
                "resname": r0.resname,
                "cb_coord": get_cb_coord(r0),
                "motif": f"{aa0}-{aa1}-{aa2}",
            })

    logger.info("Found %d N-linked glycosylation sequon(s) on chain %s",
                len(sequons), chain.id)
    return sequons


def score_glycan_proximity(
    sequons: list[dict],
    patch_centroid: np.ndarray,
    max_dist: float = 20.0,
    min_dist: float = 5.0,
) -> float:
    """Score glycan risk based on proximity of nearest sequon to the epitope.

    Returns 1.0 (no risk) if no sequons exist or all are beyond max_dist.
    Returns 0.0 (high risk) if a sequon is at or closer than min_dist.
    Linear interpolation between min_dist and max_dist.

    Args:
        sequons: Output of detect_glycosylation_sequons().
        patch_centroid: (3,) array, centroid of the epitope patch.
        max_dist: Distance in Angstroms beyond which glycans pose no risk.
        min_dist: Distance at which glycan risk is maximal (score = 0).

    Returns:
        Float in [0.0, 1.0]. Higher = less glycan risk = better feasibility.
    """
    if not sequons:
        return 1.0

    coords = [s["cb_coord"] for s in sequons if s["cb_coord"] is not None]
    if not coords:
        return 1.0

    distances = [np.linalg.norm(c - patch_centroid) for c in coords]
    nearest = min(distances)

    if nearest >= max_dist:
        return 1.0
    if nearest <= min_dist:
        return 0.0

    return (nearest - min_dist) / (max_dist - min_dist)
