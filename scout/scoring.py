"""Per-patch and per-residue scoring for epitope surface patches (STRUCT-03/04/05).

Three scoring dimensions are computed here and combined downstream to rank
candidate epitope regions for binder design:

  - Geometry score (STRUCT-03): Pure surface accessibility (inverted burial),
    normalized across all patches to [0, 1]. Chemistry is handled separately
    by hydrophobic_exposure and hot_spot_density in the composite score.
  - B-factor score (STRUCT-04): Z-scored backbone B-factor inverted so that
    rigid (low B-factor) residues score high. Detects AlphaFold pLDDT and
    maps it directly (high pLDDT = rigid = high score).
  - DSSP secondary structure (STRUCT-05): Maps DSSP codes to helix/strand/loop
    with a graceful fallback when mkdssp binary is absent.

Exports:
    HYDROPHOBIC_AA           -- frozenset of 8 nonpolar residue names
    BURIAL_RADIUS            -- float, heavy-atom count radius for burial proxy
    DSSP_HELIX_CODES         -- frozenset of DSSP codes mapped to "helix"
    DSSP_STRAND_CODES        -- frozenset of DSSP codes mapped to "strand"
    is_likely_plddt          -- detect AlphaFold pLDDT in B-factor column
    score_geometry           -- compute raw burial + hydrophobicity for one patch
    normalize_burial_scores  -- min-max normalize burial across all patches
    compute_bfactor_scores   -- Z-scored B-factor -> per-residue [0,1] score
    assign_dssp              -- DSSP secondary structure with fallback
"""

from __future__ import annotations

import logging

import numpy as np
from Bio.PDB.DSSP import DSSP
from Bio.PDB.Polypeptide import PPBuilder, is_aa

from scout.patches import get_cb_coord
from scout.pydssp_numpy import assign as pydssp_assign
from scout.sasa import STANDARD_AA

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Standard nonpolar residue set used in epitope/binder design literature
# to define hydrophobic patch character.
HYDROPHOBIC_AA: frozenset = frozenset({
    "ALA", "VAL", "ILE", "LEU", "MET", "PHE", "TRP", "PRO"
})

# Angstrom radius for counting heavy atoms around the patch centroid.
# 8 Å is a standard sphere for burial proxy in epitope scoring
# (Thornton et al. 1986; ProShape / patch analysis literature).
BURIAL_RADIUS: float = 8.0

# DSSP secondary structure code mappings.
# Codes: H=alpha-helix, G=3-10-helix, I=pi-helix -> "helix"
#        E=beta-strand, B=beta-bridge -> "strand"
#        All others (T=turn, S=bend, C=coil, " "=loop) -> "loop"
DSSP_HELIX_CODES: frozenset = frozenset({"H", "G", "I"})
DSSP_STRAND_CODES: frozenset = frozenset({"E", "B"})


# ---------------------------------------------------------------------------
# Geometry scoring — STRUCT-03
# ---------------------------------------------------------------------------

def score_geometry(patch_residues: list, all_atom_coords: np.ndarray) -> dict:
    """Compute raw burial and hydrophobicity scores for a single patch.

    Burial is approximated as the count of heavy atoms within BURIAL_RADIUS
    angstroms of the patch Cb centroid. Hydrophobicity is the fraction of
    patch residues that are in HYDROPHOBIC_AA.

    The scores are "raw" in the sense that burial is an integer count.
    Call normalize_burial_scores() on a list of these dicts to add the
    final geometry_score in [0, 1].

    Args:
        patch_residues: List of Biopython Residue objects forming one patch.
        all_atom_coords: (N, 3) numpy array of all heavy atom coordinates
            in the chain. Used for burial counting.

    Returns:
        Dict with keys:
            "burial_raw"     -- int, heavy-atom count within BURIAL_RADIUS
            "hydrophobicity" -- float in [0, 1], fraction of hydrophobic residues
    """
    # -- Compute Cb centroid of the patch --------------------------------
    cb_coords = []
    for residue in patch_residues:
        coord = get_cb_coord(residue)
        if coord is not None:
            cb_coords.append(coord)

    if not cb_coords:
        # No valid Cb coordinates in this patch — return zero scores.
        return {"burial_raw": 0, "hydrophobicity": 0.0}

    centroid = np.mean(cb_coords, axis=0)  # shape (3,)

    # -- Count heavy atoms within BURIAL_RADIUS of centroid --------------
    # Vectorized distance from all chain atoms to the centroid.
    distances = np.linalg.norm(all_atom_coords - centroid, axis=1)
    burial_raw = int(np.sum(distances <= BURIAL_RADIUS))

    # -- Hydrophobicity fraction -----------------------------------------
    num_residues = len(patch_residues)
    if num_residues == 0:
        hydrophobicity = 0.0
    else:
        num_hydrophobic = sum(
            1 for res in patch_residues if res.resname in HYDROPHOBIC_AA
        )
        hydrophobicity = num_hydrophobic / num_residues

    return {"burial_raw": burial_raw, "hydrophobicity": hydrophobicity}


def normalize_burial_scores(patches_data: list[dict]) -> list[dict]:
    """Add geometry_score to each patch dict via min-max burial normalization.

    geometry_score = accessibility (pure shape, no chemistry)

    Burial is INVERTED so that flat, exposed surfaces score high and deeply
    buried/concave pockets score low. Chemistry (hydrophobicity, hot spots)
    is handled by separate composite terms to avoid double-counting.

    accessibility = 1.0 - (burial_raw - min) / (max - min)

    When all patches have the same burial_raw (max == min), accessibility
    is set to 1.0 for all patches.

    The input list is mutated in place (geometry_score key is added) and
    also returned for convenience.

    Args:
        patches_data: List of dicts, each containing at minimum "burial_raw"
            (int). Produced by score_geometry().

    Returns:
        The same list, with "geometry_score" (float, rounded to 3 dp) added
        to each dict.
    """
    if not patches_data:
        return patches_data

    burial_values = [d["burial_raw"] for d in patches_data]
    burial_min = min(burial_values)
    burial_max = max(burial_values)

    # Guard: when all patches have identical burial, set denominator to 1
    # to avoid division by zero. All patches get accessibility = 1.0.
    denom = burial_max - burial_min if burial_max != burial_min else 1.0

    for patch_dict in patches_data:
        # Invert: low burial (exposed, flat) -> high accessibility score
        # Pure shape metric — no hydrophobicity component.
        accessibility = 1.0 - (patch_dict["burial_raw"] - burial_min) / denom
        patch_dict["geometry_score"] = round(accessibility, 3)

    return patches_data


# ---------------------------------------------------------------------------
# B-factor scoring — STRUCT-04
# ---------------------------------------------------------------------------

def _is_experimental_structure(pdb_path: str) -> bool:
    """Check PDB/mmCIF header for experimental method records.

    Returns True if the file contains EXPDTA (PDB) or _exptl.method
    (mmCIF) indicating an experimental structure (X-ray, NMR, cryo-EM).
    AlphaFold models lack these records or have 'THEORETICAL MODEL'.

    Args:
        pdb_path: Path to the structure file.

    Returns:
        True if experimental method detected, False otherwise.
    """
    experimental_methods = {
        "X-RAY DIFFRACTION", "SOLUTION NMR", "SOLID-STATE NMR",
        "ELECTRON MICROSCOPY", "ELECTRON CRYSTALLOGRAPHY",
        "NEUTRON DIFFRACTION", "FIBER DIFFRACTION",
    }
    try:
        with open(pdb_path, "r", errors="replace") as fh:
            for line in fh:
                # PDB format
                if line.startswith("EXPDTA"):
                    method = line[10:].strip().upper().rstrip(";").strip()
                    if any(m in method for m in experimental_methods):
                        return True
                    if "THEORETICAL MODEL" in method:
                        return False
                # mmCIF format
                if "_exptl.method" in line:
                    method = line.split()[-1].strip("'\"").upper()
                    if any(m in method for m in experimental_methods):
                        return True
                # Stop scanning after we hit coordinates
                if line.startswith("ATOM") or line.startswith("HETATM"):
                    break
                if line.startswith("_atom_site."):
                    break
    except OSError:
        pass
    return False


def is_likely_plddt(chain_residues: list, pdb_path: str = "") -> bool:
    """Detect if B-factor column likely contains AlphaFold pLDDT scores.

    Two-step detection:
    1. If the file header contains an experimental method (EXPDTA / _exptl.method),
       it is NOT pLDDT regardless of B-factor values.
    2. Otherwise, apply B-factor heuristic: all values in [0, 100] with mean > 50.

    Args:
        chain_residues: List of Biopython Residue objects from a single chain.
        pdb_path: Path to the structure file (for header check).

    Returns:
        True if the B-factor column likely contains pLDDT values.
    """
    # Step 1: header-based check (most reliable)
    if pdb_path and _is_experimental_structure(str(pdb_path)):
        return False

    # Step 2: B-factor heuristic fallback
    backbone_atoms = ("N", "CA", "C", "O")
    bfacs = []
    for residue in chain_residues:
        for atom_name in backbone_atoms:
            try:
                bfacs.append(residue[atom_name].get_bfactor())
            except KeyError:
                pass
    if len(bfacs) < 40:  # ~10 residues x 4 backbone atoms
        return False
    bfac_arr = np.array(bfacs)
    if float(np.min(bfac_arr)) < 0.0 or float(np.max(bfac_arr)) > 100.0:
        return False
    return float(np.mean(bfac_arr)) > 50.0


def compute_bfactor_scores(chain_residues: list, plddt_mode: bool = False) -> dict:
    """Compute per-residue B-factor scores as inverted Z-scores in [0, 1].

    Backbone atoms (N, CA, C, O) mean B-factor is computed per residue.
    Residues with no backbone atoms present are skipped.

    Two modes:

    **Crystallographic (default):** Z-score inverted so that low B-factor
    (rigid) residues score high.
        z = (bfac_mean - chain_mean) / chain_std
        score = 1.0 - clip(z, 0.0, 3.0) / 3.0

    **pLDDT mode (plddt_mode=True):** AlphaFold pLDDT values are mapped
    directly: high pLDDT (confident/rigid) → high score.
        score = clip(pLDDT, 0, 100) / 100

    Guard: if chain_std == 0 (all B-factors identical), chain_std is set
    to 1.0. All Z-scores become 0.0 and all scores return 1.0.

    Args:
        chain_residues: List of Biopython Residue objects from a single chain.
        plddt_mode: If True, treat B-factor column as AlphaFold pLDDT.

    Returns:
        Dict mapping residue.get_full_id() to float score in [0, 1].
        Rounded to 3 decimal places.
    """
    backbone_atoms = ("N", "CA", "C", "O")

    # -- Collect per-residue backbone mean B-factor ----------------------
    residue_bfactors: list[tuple] = []  # list of (full_id, mean_bfac)

    for residue in chain_residues:
        present_bfacs = []
        for atom_name in backbone_atoms:
            try:
                atom = residue[atom_name]
                present_bfacs.append(atom.get_bfactor())
            except KeyError:
                pass

        if not present_bfacs:
            continue

        mean_bfac = float(np.mean(present_bfacs))
        residue_bfactors.append((residue.get_full_id(), mean_bfac))

    if not residue_bfactors:
        return {}

    # -- pLDDT mode: direct mapping, no Z-scoring -----------------------
    if plddt_mode:
        scores = {}
        for full_id, mean_bfac in residue_bfactors:
            score = float(np.clip(mean_bfac, 0.0, 100.0)) / 100.0
            scores[full_id] = round(score, 3)
        return scores

    # -- Crystallographic mode: Z-score across the chain -----------------
    bfac_array = np.array([bfac for _, bfac in residue_bfactors])
    chain_mean = float(np.mean(bfac_array))
    chain_std = float(np.std(bfac_array))

    # Guard: zero std (all identical B-factors) -> treat as std = 1.0 so
    # all Z-scores are 0.0 and all scores return 1.0.
    if chain_std == 0.0:
        chain_std = 1.0

    scores = {}
    for full_id, mean_bfac in residue_bfactors:
        z_score = (mean_bfac - chain_mean) / chain_std
        # Clip to [0, 3]: residues below mean (low B-factor) clip to 0
        # giving score 1.0; residues far above mean clip at 3 giving 0.0.
        score = 1.0 - float(np.clip(z_score, 0.0, 3.0)) / 3.0
        scores[full_id] = round(score, 3)

    return scores


# ---------------------------------------------------------------------------
# DSSP secondary structure — STRUCT-05
# ---------------------------------------------------------------------------

class _ScoutPPBuilder(PPBuilder):
    """PPBuilder that also treats _MODIFIED_AA residues as amino acids.

    build_peptides asks two questions about each consecutive pair: _accept
    ("is this residue an amino acid") and _is_connected ("do these two share
    a real C->N peptide bond"). Only the first needs changing. _is_connected
    is the load-bearing half and is left exactly as Biopython wrote it.

    STRICTLY MORE PERMISSIVE THAN THE STOCK RULE, and that is deliberate.
    Stock _accept(residue, 1) is is_aa(residue, standard=True), which reads
    the RESNAME ONLY and ignores the hetflag. This ORs a term onto it rather
    than replacing it, so every residue Biopython accepted is still accepted
    and the peptide can only grow.

    An earlier version of this class reused _is_pydssp_polymer_residue
    instead, and that was a BUG, not a tidier spelling. That predicate also
    demands `hetflag == " "` for the canonical 20, which stock _accept does
    not -- so it was not a superset but INCOMPARABLE, and it cut the peptide
    at any in-polymer canonical residue recorded as HETATM. Measured on
    1HEW chain A with residues 60-62 re-recorded as HETATM (resnames
    unchanged): 129 labels -> 126, and residues 59 and 63 fell to "loop".
    That is verbatim the bug this change exists to remove, reintroduced
    somewhere else. Pinned by
    test_phi_psi_does_not_cut_at_hetatm_spelled_canonical_residues.

    The hetflag gate is right for pydssp and wrong here, because the two
    branches have different protection. pydssp has NO connectivity test, so
    a free ALA in the solvent would be welded straight into its coordinate
    array and only the hetflag keeps it out. This branch has _is_connected:
    a free residue is bonded to nothing, so it is cut out on the geometry
    regardless of how it is spelled. Importing pydssp's gate here buys
    nothing and costs real in-polymer residues.

    NOT build_peptides(aa_only=0), the obvious one-liner. That accepts every
    one of the 1032 entries in Biopython's protein_letters_3to1_extended,
    then ANY residue at all carrying an atom named CA. Measured: it takes
    NAG, LIG, HEM and ATP with a warning, and SEP and FME with NO warning
    (both are in the extended table). The silent pair is the problem --
    scout treats neither as an amino acid anywhere else. SEC is the mirror
    image: absent from the extended table, so aa_only=0 would reach it only
    through the atom-name branch.

    _accept is PRIVATE Biopython API and requirements.txt pins a RANGE
    (>=1.81,<2.0). If an upgrade renames it, the override stops being called
    and MSE splits the peptide again -- today's bug, restored in the safe
    direction but SILENTLY, which is why
    test_phi_psi_keeps_modified_residues_in_the_peptide pins it. If
    Biopython ever drops _accept, replicate build_peptides' loop here.
    """

    def _accept(self, residue, standard_aa_only):
        return (residue.resname.strip() in _MODIFIED_AA
                or is_aa(residue, standard=True))


def _assign_ss_by_phi_psi(model, chain_id=None) -> dict:
    """Fallback SS assignment using backbone phi/psi angles (Ramachandran).

    Used when mkdssp is not installed. Classifies each residue based on
    its backbone dihedral angles into generous Ramachandran regions:

        Helix:  -160 < phi < -20  AND  -80 < psi < 10
        Strand: -180 < phi < -60  AND  (100 < psi < 180  OR  -180 < psi < -120)
        Loop:   everything else

    These boundaries are deliberately generous to avoid under-assigning
    regular secondary structure. The classification is less accurate than
    DSSP (no hydrogen bond analysis) but sufficient for scoring purposes.

    Covers what _ScoutPPBuilder accepts AND that _is_connected joins to an
    accepted neighbour. That is NOT the same set _assign_ss_by_pydssp
    covers, and the difference is not incidental: pydssp additionally bails
    on chains outside _PYDSSP_MIN_RESIDUES.._PYDSSP_MAX_RESIDUES or with a
    residue missing a backbone atom, while this branch drops any residue
    peptide-bonded to nothing -- an isolated residue between two unresolved
    stretches gets no key here and pydssp labels it. ss_method records which
    branch ran, so the covered set does legitimately depend on the branch.

    MODIFIED RESIDUES USED TO SPLIT THE PEPTIDE HERE. PPBuilder's default
    aa_only=1 rejects MSE, and build_peptides ENDS the current peptide at
    any rejected residue. The failure is milder than the pydssp one -- the
    chain is cut, not welded, so no coordinates are corrupted -- but it
    still loses labels three ways: the MSE itself is in no peptide, the
    residue BEFORE it loses its psi (no following N), and the residue AFTER
    it loses its phi (no preceding C). Measured on a SeMet-ized 1HEW: one
    129-residue peptide became three (11/92/24), residues 12 and 105 lost
    their labels outright, and 11, 13 and 106 fell helix -> loop. Five
    residues wrong out of 129, all of them silently, since a partial
    phi/psi map is a normal result here.

    Args:
        model: Biopython Model object (structure[0]).
        chain_id: Restrict to this chain. None labels every chain.

    Returns:
        Dict mapping (chain_id, residue.get_id()) to one of
        "helix", "strand", or "loop".
    """
    ss_map = {}
    ppb = _ScoutPPBuilder()

    for chain in model.get_chains():
        cid = chain.get_id()
        if chain_id is not None and cid != chain_id:
            continue
        for pp in ppb.build_peptides(chain):
            phi_psi = pp.get_phi_psi_list()
            for residue, (phi, psi) in zip(pp, phi_psi):
                if phi is None or psi is None:
                    label = "loop"
                else:
                    phi_deg = np.degrees(phi)
                    psi_deg = np.degrees(psi)
                    if -160 < phi_deg < -20 and -80 < psi_deg < 10:
                        label = "helix"
                    elif (-180 < phi_deg < -60
                          and (100 < psi_deg < 180 or -180 < psi_deg < -120)):
                        label = "strand"
                    else:
                        label = "loop"
                key = (cid, residue.get_id())
                ss_map[key] = label

    return ss_map


# pydssp indexes backbone atoms positionally, so this order is load-bearing.
# A residue missing any of the four cannot be assigned, and one such residue
# aborts the whole map -- see _assign_ss_by_pydssp on why partial is not safe.
_PYDSSP_BACKBONE = ("N", "CA", "C", "O")
# Column order of the one-hot pydssp returns: np.stack([loop, helix, strand]).
_PYDSSP_LABELS = ("loop", "helix", "strand")
# turn5 reads the offset-5 diagonal of an l x l map, so a shorter chain
# raises on a shape mismatch rather than returning an empty result.
_PYDSSP_MIN_RESIDUES = 6

# Per-chain ceiling. pydssp is O(L^2) in BOTH time and memory -- the H-bond
# map and the four bridge terms are all l x l -- so cost grows ~4x for every
# doubling. (Upstream MATERIALISES four (L-1, L-1, 3) float64 arrays via einops
# `repeat`; the broadcast rewrite in scout/pydssp_numpy.py does not, which is
# why the figures below are lower than that model predicts. They are measured,
# not derived.) Measured on the repo venv:
#
#     L =   600     0.09 s    0.05 GB
#     L =  1000     0.26 s    0.13 GB
#     L =  2000     1.02 s    0.51 GB   <- the cap
#
# The phi/psi branch this replaced was O(L), so nothing upstream ever needed a
# residue bound. ANON_MAX_UPLOAD_BYTES (scout/routes.py) admits ~25,900
# backbone-only residues in a single chain, which extrapolates to ~85 GB and
# would OOM the worker: under Linux's default memory overcommit (the
# production platform) the allocation SUCCEEDS and
# there is no MemoryError to catch, the process just dies touching the pages.
# Above the cap, fall through to phi/psi -- worse labels, but O(L) and honest,
# because ss_method then says "phi_psi".
_PYDSSP_MAX_RESIDUES = 2000

# Modified residues that are ordinary polymer amino acids carrying a normal
# N/CA/C/O backbone. The PDB records these as HETATM, so Biopython stamps them
# with a "H_<resname>" hetflag, and the selector THIS function used to share
# with scout/sasa.py rejected them on that flag alone -- sasa's STANDARD_AA
# holds only the 20 canonical resnames, so the resname check never even ran.
#
# That is NOT uniform across the package, which matters to anyone tempted to
# "tidy" this by reusing another module's set. scout/parser.py used to reject
# the HETATM spelling on the hetflag despite listing both in its STANDARD_AA
# (the ATOM spelling always passed, e.g. 1CC1 chain L SEC 492); it no
# longer does -- see parser.py::_is_polymer_residue, which now gives the same
# answer this function does on every input, by the same rule, deliberately
# duplicated rather than shared. scout/epitope_db.py's _THREE_TO_ONE still
# maps both while a hetflag gate drops them, so its two entries are
# unreachable for the HETATM spelling; scout/glycan.py excludes them outright
# and then indexes the survivors POSITIONALLY, which welds sequence-distant
# residues together. Several modules, several different answers to "is this
# residue standard", and the divergence is not all deliberate.
#
# Rejecting them is correct for scout/sasa.py (freesasa has no radii for MSE)
# and for patch construction (an epitope centred on a selenomethionine is not
# something the scoring model was built for). It is WRONG here, because pydssp
# reads coordinates and nothing else: dropping a residue that is chemically
# part of the chain does not leave a hole, it CLOSES the gap and welds two
# sequence-distant residues together. That corrupts the pseudo-H of the
# following residue and slides the fixed i->i+3/4/5 turn and bridge offsets
# onto renumbered indices.
#
# Measured against a MET-ized copy of each structure, which is junction-free by
# construction because MSE is chemically MET -- so the control needs no mkdssp.
# Ten SeMet structures were chosen from an RCSB query; NINE could be measured.
# 1B24 is excluded for a reason that predates this change: chain A residue 179
# is a standard residue missing a backbone atom, so the all-or-nothing guard
# returns {} on both arms.
#
# Across those nine: 25 MSE residues, 1222 residues shared with the control,
# 31 taking a different label -- 97.46% agreement against the control's 100%,
# worst case 13CT at 89.66%, roughly 1.2 residues corrupted per junction. With
# this change all nine agree at 1.0000 and the shared count rises to 1247,
# exactly the 25 MSE residues now being labelled instead of skipped.
#
# The control that makes it causal rather than correlational: NINE structures
# containing no MSE, through the same comparison, agree at exactly 1.0000 both
# before and after -- 2012 shared residues, zero differences, byte-identical
# either side of the change. So the loss is attributable to MSE and not to the
# comparison method, and the change is inert on structures without it.
#
# Whether this also raises the published 97.9% headline (measured against real
# mkdssp on 30 chains) is NOT measured here: mkdssp is not installed in this
# environment. The DIRECTION can only be upward, since MSE-bearing chains in
# that corpus were scored with these junctions present -- but do not assume the
# MAGNITUDE is large. How much MSE that corpus actually contains is unclear:
# docs/qc/scout-dssp-fallback-measurement.md reports "1 residue in 4487"
# HETATM-flagged standard residues over the same 30 chains, yet 1ema:A alone
# holds 4 MSE, so that count appears to have been taken with a residue set
# that excludes MSE and therefore does not measure MSE at all. Settle the
# corpus composition before quoting any revised headline.
#
# Deliberately a whitelist, not "anything carrying a full backbone": the
# latter would weld genuine HETATM ligands that happen to use those atom names
# into the polymer. MSE and SEC are the two the rest of the package already
# treats as amino acids (scout/parser.py STANDARD_AA, scout/epitope_db.py
# _THREE_TO_ONE), so this adds no new notion of "standard" to the codebase.
#
# Residual risk, accepted knowingly: a FREE selenomethionine sitting in the
# solvent of the same chain would now be threaded into the backbone. It cannot
# be told apart by hetflag (a free MSE is "H_MSE" too) or by atom content (it
# carries N/CA/C/O as well). Distinguishing it needs a peptide-bond continuity
# test, which would be a far larger change to a branch whose gap behaviour is
# deliberate and measured. Free MSE as a ligand is rare; MSE in the polymer is
# routine, and the measurement above is what that trade is priced on.
#
# Price that risk at its REAL cost, which is not one bad residue. The 534-MSE
# backbone-completeness sweep behind this whitelist sampled POLYMER MSE in real
# depositions; a free MSE in solvent is exactly the population most likely to
# be PARTIALLY modelled, and a missing carbonyl O is common for one. Such a
# residue now enters `standard`, so it trips `len(residues) != len(standard)`
# below and empties the map for the ENTIRE chain -- where before the hetflag
# gate made it invisible and it cost nothing. Landing in phi/psi with
# ss_method truthful is why this is acceptable, but it is a whole-chain
# downgrade, not a single wrong label.
#
# TWO BEHAVIOUR CHANGES COME FROM MSE/SEC JOINING `standard`, and only the
# first is a consistency fix.
#
# (1) WHOLE-MODEL PATH ONLY. A chain made only of MSE now COUNTS, where before
# it had no standard residues and was skipped by `if not standard: continue`.
# So a 3-residue MSE-only chain trips _PYDSSP_MIN_RESIDUES and, per the
# all-or-nothing rule, empties the whole map -- measured: 129 labels -> 0 with
# chain_id=None. That is exactly what a 3-residue ALA-only chain ALREADY did on
# the old code (also measured), so the old asymmetry was the anomaly: a short
# canonical chain was fatal while a short MSE chain was invisible.
# run_pipeline always passes a chain_id (scout/pipeline.py:416), and with a
# chain_id set the short neighbour is never looked at, so production is
# unaffected -- 129 labels either way. Only a caller using the chain_id=None
# default can see this.
#
# (2) SCOPED PATH TOO, AND THIS ONE IS A DOWNGRADE -- IT IS WHY THE CHANGE IS
# NOT MONOTONE. _PYDSSP_MAX_RESIDUES is checked against len(standard), which
# MSE/SEC now inflate. A chain whose canonical count falls in
# (2000 - n_MSE, 2000] crosses the cap and loses its ENTIRE map -- measured on
# the SeMet-ized 1HEW fixture with the cap pinned at the old count: 127 labels
# -> 0. The 178-chain sweep that recorded no deleted keys could not see this;
# no chain in it came near 2000. "Monotone" is therefore a sweep observation,
# not a universal, and this is the counter-example.
#
# The cap is NOT wrong and is deliberately left alone: it bounds an O(L^2)
# allocation, and len(standard) is now what pydssp actually allocates for,
# where before it UNDER-counted. What a chain in that band loses is a map that
# was junction-corrupted at every MSE anyway, so the trade is "corrupted
# pydssp" for "phi/psi", not "correct" for "nothing". Narrow band, honest
# failure, ss_method still truthful. Recorded so the next reader does not
# rediscover it as a bug.
_MODIFIED_AA = frozenset({"MSE", "SEC"})


def _is_pydssp_polymer_residue(residue) -> bool:
    """True when this residue is part of the backbone pydssp should read.

    Accepts ordinary amino acids and the modified amino acids in
    _MODIFIED_AA. The modified ones are matched on resname ALONE,
    without consulting the hetflag: they are normally HETATM, but structures
    that have been through a design or refinement pipeline often re-emit them
    as ATOM, and both spellings mean the same chemistry. Water and every
    other heteroatom stay out.
    """
    resname = residue.resname.strip()
    if resname in _MODIFIED_AA:
        return True
    return residue.get_id()[0] == " " and resname in STANDARD_AA


def _try_ss(assign_fn, model, name: str, chain_id=None) -> dict:
    """Run one SS assigner, returning {} instead of raising.

    Fallback selection below is driven by whether a branch produced labels,
    so a failure has to look like an empty result rather than an exception.
    """
    try:
        return assign_fn(model, chain_id)
    except Exception as exc:  # noqa: BLE001 - any failure means "try the next one"
        logger.warning("%s SS assignment failed (%s)", name, exc)
        return {}


def _assign_ss_by_pydssp(model, chain_id=None) -> dict:
    """Assign SS from backbone geometry using DSSP's hydrogen-bond algorithm.

    Simplified relative to mkdssp (no beta-bulge annotation, approximate amide
    H, 3-state output) -- see scout/pydssp_numpy.py -- which is why agreement
    is 97.9% and not 100%.

    This is not a Ramachandran approximation like _assign_ss_by_phi_psi: it
    builds the electrostatic hydrogen-bond map and reads helices and bridge
    ladders off it, which is why it needs a complete N/CA/C/O backbone
    rather than just enough atoms for a dihedral. See scout/pydssp_numpy.py.

    Runs one chain at a time. Feeding chains separately hides inter-chain
    beta pairing from the bridge search, bounded at roughly 0.3 points of
    per-residue agreement -- a between-group comparison (single-chain
    structures vs the whole corpus), not a controlled measurement, so it is
    confounded with which structures happen to be multi-chain; concatenating them instead would invent a
    peptide bond at every chain junction, which is both worse and
    unmeasured, so per-chain is deliberate.

    A standard residue missing a backbone atom, a chain too short to carry a
    turn, and a chain over the size cap each abort the WHOLE map -- see
    Returns. Nothing is quietly dropped: a partial map would let ss_method
    name a branch that never labelled those residues.

    Gaps survive that rule, because a gap is not a skip. A residue the file
    never resolved, or one excluded as non-standard (any HETATM outside
    _MODIFIED_AA), never enters the standard-residue count either, so
    the guard above sees no mismatch and the coordinate array simply closes
    over it -- leaving two sequence-distant residues adjacent. Two
    consequences, neither clean:

      * the pseudo-H is derived from the C(i)->N(i+1) vector, so it is wrong
        for the single residue following each gap;
      * turns and bridges are found at fixed array offsets (i to i+3/4/5),
        and those offsets are now on RENUMBERED indices, so a turn can be
        declared between residues that are not 3/4/5 apart in sequence.

    An earlier version of this comment called the second case benign on the
    grounds that geometry "simply sees no hydrogen bond across the gap".
    That is not an argument: the H-bond energy is computed from real
    coordinates and can be satisfied across a closed gap.

    Two of the 30 accuracy chains are gapped -- 1ema:A and 1igy:B -- so the
    measured 97.9% agreement does include this cost, and the worst case still
    labelled 34 of its 35 patches correctly. But 2 chains is a thin sample; a
    structure with many short gaps could do worse.

    THAT SAMPLE IS THINNER THAN IT WAS ONCE RECORDED. This docstring used to
    say "1ema:A with 5" gaps. Four of those five were the excluded MSE at 78,
    88, 153 and 218; only the break between 64 and 68 is genuine unresolved
    density. So 1ema:A contributes ONE real gap, not five, and 1igy:B's 26
    (checked: that chain contains no MSE, so all 26 are real) carries almost
    the entire gap sample on its own.

    SELENOMETHIONINE USED TO BE ONE OF THESE JUNCTIONS and is not any more --
    see _MODIFIED_AA. It was the common case rather than an exotic one,
    because SeMet phasing is routine, and it was invisible: MSE was in neither
    the standard-residue count nor the labelled set, so the all-or-nothing
    guard saw no mismatch and the run still reported ss_method="pydssp".

    Args:
        model: Biopython Model object (structure[0]).

    Returns:
        Dict mapping (chain_id, residue.get_id()) to one of "helix", "strand",
        or "loop" -- covering EVERY residue of the chains it was asked for
        that _is_pydssp_polymer_residue accepts (the canonical 20, plus
        _MODIFIED_AA), or else empty. It is deliberately all-or-nothing:
        assign_dssp falls through only on an empty map, so a partial result
        would be reported as ss_method="pydssp" while the unlabelled residues
        silently read as "loop". Returns {} if a chain IN SCOPE has one of
        those residues missing a backbone atom, is shorter than
        _PYDSSP_MIN_RESIDUES, or is longer than _PYDSSP_MAX_RESIDUES.

        Scope is the point. With chain_id set, an unreadable NEIGHBOUR chain
        is irrelevant -- it is never looked at, so it can neither sink the
        map nor cost an O(L^2) allocation.
    """
    ss_map = {}
    for chain in model.get_chains():
        cid = chain.get_id()
        if chain_id is not None and cid != chain_id:
            continue
        standard, residues, coords = [], [], []
        for residue in chain.get_residues():
            if not _is_pydssp_polymer_residue(residue):
                continue
            standard.append(residue)
            if not all(atom in residue for atom in _PYDSSP_BACKBONE):
                continue
            residues.append(residue)
            coords.append([residue[atom].get_coord() for atom in _PYDSSP_BACKBONE])

        if not standard:
            continue  # no protein in this chain -- nothing to claim either way

        # All-or-nothing over whatever is IN SCOPE. Returning a PARTIAL map
        # would make ss_method lie: assign_dssp only falls through when the map
        # is entirely empty, so an unlabelled residue would still be reported
        # as "pydssp" while run_pipeline scored it on the "loop" floor
        # (ss_map.get(key, "loop")). Bail out and let phi/psi try instead: it
        # needs only N/CA/C, so it recovers an O-stripped chain in full
        # (measured 129/129 on 1HEW).
        #
        # That is a recovery, NOT a guarantee. A CA-only chain defeats phi/psi
        # too, and phi/psi returns whatever it CAN read under
        # ss_method="phi_psi". Scoping bounds how much that can cost: with a
        # chain_id, both branches see one chain, so phi/psi either covers it or
        # returns {} and the honest answer is "none". Only the whole-model path
        # (chain_id=None) can still produce a partial phi/psi map -- measured
        # 65/130 on 3s7g with chain B stripped to CA, the other 65 on the loop
        # floor. Guarded by test_phi_psi_is_scoped_to_the_chain_too.
        if len(residues) != len(standard):
            logger.warning(
                "pydssp: chain %s has %d standard residues missing a backbone "
                "atom; falling through so ss_method stays truthful",
                cid,
                len(standard) - len(residues),
            )
            return {}
        if len(standard) < _PYDSSP_MIN_RESIDUES:
            logger.warning(
                "pydssp: chain %s has only %d residues (needs %d)",
                cid,
                len(standard),
                _PYDSSP_MIN_RESIDUES,
            )
            return {}
        if len(standard) > _PYDSSP_MAX_RESIDUES:
            logger.warning(
                "pydssp: chain %s has %d residues, over the %d cap; falling "
                "through to keep the O(L^2) allocation bounded",
                cid,
                len(standard),
                _PYDSSP_MAX_RESIDUES,
            )
            return {}

        onehot = np.asarray(pydssp_assign(np.asarray(coords, dtype=float)))
        for residue, column in zip(residues, onehot.argmax(-1)):
            ss_map[(cid, residue.get_id())] = _PYDSSP_LABELS[column]
    return ss_map


def assign_dssp(model, pdb_path: str, chain_id=None) -> tuple[dict, str]:
    """Assign secondary structure labels to residues using DSSP.

    Three branches, tried in order, each reported by name in ``method``:

      1. ``"dssp"``    -- Bio.PDB.DSSP driving the real mkdssp binary.
      2. ``"pydssp"``  -- DSSP's H-bond algorithm in process, simplified
         (see scout/pydssp_numpy.py), from backbone coordinates alone.
         Agrees with mkdssp on 97.9% of residues. Needs N, CA, C and O.
      3. ``"phi_psi"`` -- Ramachandran classification from dihedrals, which
         needs no carbonyl O and so still covers O-stripped backbones that
         (2) cannot read. It does not cover CA-only models: PPBuilder needs
         C and N, so those return {} from both (2) and (3).

    A branch is skipped when it raises OR yields no labels at all, so an
    unreadable structure falls through rather than pinning ``method`` to a
    branch that measured nothing.

    NOTE ON DEPLOYMENT: whether branch (1) runs depends entirely on whether
    mkdssp is on PATH in the deployed image. Nothing in this repository
    installs it (checked 2026-08-19: nixpacks.toml, Procfile, the
    requirements files; the web service has no Dockerfile or Aptfile, though
    the GPU tools do), so unless it
    was added by hand in the Railway dashboard's build settings, branch (1)
    never runs in production. That absence has never been confirmed at
    runtime, so read the ss_method column of a real run rather than
    assuming either way.
    Do NOT try to fix this by adding dssp to nixpacks.toml: the Railway
    service builds with Railpack, which does not read that file, so the
    change is a silent no-op. That was attempted and dropped on
    2026-08-19 -- see docs/qc/scout-dssp-install-decision.md section 0.

    Branch (2) is therefore the normal path, and is why installing mkdssp
    stopped being worth the effort. Measured against mkdssp 4.2.2 on the
    same 30 chains used for the fallback measurement, pydssp agrees on
    97.9% of residues against 70.2% for phi/psi, and recovers true loops --
    the failure that made the displayed label wrong about half the time --
    at 0.981 recall against 0.339. It also reads headerless coordinate
    files that mkdssp 4.2.2 refuses outright, which matters because users
    upload design-pipeline output. See docs/qc/scout-pydssp-adoption.md.

    Before this change branch (2) did not exist and (3) was the normal path,
    so any results.csv with ss_method="phi_psi" carries the old accuracy:
    ~70% per-residue, two thirds of true loops called helix or strand, and
    ss_score biased ~+0.23 high.
    See docs/qc/scout-dssp-fallback-measurement.md.

    DSSP code mapping (branch 1 only -- 2 and 3 emit labels directly):
        H, G, I -> "helix"
        E, B    -> "strand"
        all else -> "loop" (T, S, '-', and any code not listed above)
    DSSP emits no "C"; Biopython normalises a blank code to "-"
    (Bio/PDB/DSSP.py:260). Biopython 1.87 documents H/B/E/G/I/T/S/-
    only (Bio/PDB/DSSP.py:24-37); DSSP 4.x is reported to add "P" for
    polyproline II, which is unverified here and maps to "loop" either
    way -- as does any future code, which is the point of the else.

    Args:
        model: Biopython Model object (structure[0]).
        pdb_path: Path to the PDB file on disk (required by DSSP wrapper).
        chain_id: The chain whose labels will actually be READ. Every branch
            is restricted to it, and the fall-through test below therefore
            asks "did this branch label the scored chain?" rather than "did
            it label anything?". None labels every chain, which is what the
            tests use to exercise multi-chain behaviour directly.

    Returns:
        ``(ss_map, method)``. ``ss_map`` maps (chain_id, residue.get_id())
        to one of "helix", "strand", or "loop"; missing residues are read as
        "loop" downstream. ``method`` names the branch that produced it --
        "dssp", "pydssp", "phi_psi", or "none" -- and is recorded per run so
        they stay distinguishable after the fact; see the ss_method column
        in results.csv. "none" means every branch came back empty, so the
        labels are a default rather than a measurement.
    """
    try:
        dssp_obj = DSSP(model, pdb_path, dssp="mkdssp")
        ss_map = {}
        for dssp_key in dssp_obj.property_keys:
            residue_data = dssp_obj[dssp_key]
            # Index 2 is the secondary-structure column. Index 1 is the
            # one-letter AMINO ACID -- reading it (as this code did until
            # 2026-08-19) silently classified every His/Gly/Ile as "helix"
            # and every Glu as "strand", because H/G/I/E are valid letters
            # in both alphabets. Measured against mkdssp 4.2.2 on 30 chains,
            # that mistake scored 38% per-residue agreement versus 70% for
            # the phi/psi fallback it was meant to improve on.
            #
            # The trap is that Bio.PDB.DSSP ships TWO tuple orders in
            # one module: the DSSP class yields
            #   (dssp_index, aa, ss, rel_acc, phi, psi, ...)  -> ss at 2
            # while dssp_dict_from_pdb_file/_make_dssp_dict yield
            #   (aa, ss, acc, phi, psi, dssp_index, ...)      -> ss at 1
            # so [1] is right for the dict API and wrong here. Check
            # which API you are on before touching this index.
            ss_code = residue_data[2]
            if ss_code in DSSP_HELIX_CODES:
                label = "helix"
            elif ss_code in DSSP_STRAND_CODES:
                label = "strand"
            else:
                label = "loop"
            if chain_id is None or dssp_key[0] == chain_id:
                ss_map[dssp_key] = label
        method = "dssp"
    except Exception as exc:
        logger.warning(
            "DSSP binary unavailable (%s); falling back to in-process assignment",
            exc,
        )
        # "none", not "dssp": the map is empty, so the branches below always
        # overwrite this. Naming the branch that just FAILED here would go
        # live the moment anyone adds an early return above them.
        ss_map, method = {}, "none"

    # Each branch is skipped when it raises OR yields no labels. Testing
    # emptiness here rather than only in the except block matters: DSSP()
    # can RETURN with zero property_keys instead of raising, and an earlier
    # version of this function treated that as a successful "dssp" run and
    # returned "none" without ever trying the two branches below.
    if not ss_map:
        ss_map, method = (
            _try_ss(_assign_ss_by_pydssp, model, "pydssp", chain_id), "pydssp")
    if not ss_map:
        # pydssp is more accurate but needs a complete N/CA/C/O backbone.
        # phi/psi needs no carbonyl O, so it still covers O-stripped models
        # that pydssp cannot read. It does NOT cover CA-only models --
        # PPBuilder needs C and N, so those yield {} from both branches and
        # ss_method is "none". O-stripped is the whole reason this is here.
        ss_map, method = (
            _try_ss(_assign_ss_by_phi_psi, model, "phi_psi", chain_id), "phi_psi")

    # An empty map scores every patch at the "loop" floor, so the term
    # contributed nothing regardless of which branch produced it. Report
    # that as "none" rather than crediting a branch that yielded no labels
    # -- _assign_ss_by_phi_psi returns {} through its NORMAL path when
    # PPBuilder finds no peptides, so this case logs no warning at all.
    return ss_map, (method if ss_map else "none")
