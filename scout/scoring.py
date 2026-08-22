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

    Args:
        model: Biopython Model object (structure[0]).
        chain_id: Restrict to this chain. None labels every chain.

    Returns:
        Dict mapping (chain_id, residue.get_id()) to one of
        "helix", "strand", or "loop".
    """
    from Bio.PDB.Polypeptide import PPBuilder  # noqa: PLC0415

    ss_map = {}
    ppb = PPBuilder()

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
    never resolved, or one excluded as non-standard (MSE, any HETATM), never
    enters the standard-residue count either, so the guard above sees no
    mismatch and the coordinate array simply closes over it -- leaving two
    sequence-distant residues adjacent. Two consequences, neither clean:

      * the pseudo-H is derived from the C(i)->N(i+1) vector, so it is wrong
        for the single residue following each gap;
      * turns and bridges are found at fixed array offsets (i to i+3/4/5),
        and those offsets are now on RENUMBERED indices, so a turn can be
        declared between residues that are not 3/4/5 apart in sequence.

    An earlier version of this comment called the second case benign on the
    grounds that geometry "simply sees no hydrogen bond across the gap".
    That is not an argument: the H-bond energy is computed from real
    coordinates and can be satisfied across a closed gap.

    Two of the 30 accuracy chains are gapped -- 1ema:A with 5 and 1igy:B with
    26 -- so the measured 97.9% agreement does include this cost, and the
    worst case still labelled 34 of its 35 patches correctly. But 2 chains is
    a thin sample; a structure with many short gaps could do worse.

    Args:
        model: Biopython Model object (structure[0]).

    Returns:
        Dict mapping (chain_id, residue.get_id()) to one of "helix", "strand",
        or "loop" -- covering EVERY standard residue of the chains it was
        asked for, or else empty. It is deliberately all-or-nothing:
        assign_dssp falls through only on an empty map, so a partial result
        would be reported as ss_method="pydssp" while the unlabelled residues
        silently read as "loop". Returns {} if a chain IN SCOPE has a standard
        residue missing a backbone atom, is shorter than _PYDSSP_MIN_RESIDUES,
        or is longer than _PYDSSP_MAX_RESIDUES.

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
            if residue.get_id()[0] != " " or residue.resname not in STANDARD_AA:
                continue
            standard.append(residue)
            if not all(atom in residue for atom in _PYDSSP_BACKBONE):
                continue
            residues.append(residue)
            coords.append([residue[atom].get_coord() for atom in _PYDSSP_BACKBONE])

        if not standard:
            continue  # no protein in this chain -- nothing to claim either way

        # All-or-nothing, deliberately. Returning a PARTIAL map would make
        # ss_method lie: assign_dssp only falls through when the map is
        # entirely empty, so one unlabelled chain among several would still be
        # reported as "pydssp" while run_pipeline scored that chain entirely on
        # the "loop" floor (ss_map.get(key, "loop")). Bail out instead and let
        # phi/psi try the whole model: it needs only N/CA/C, so it recovers an
        # O-stripped chain in full (measured 129/129 on 1HEW).
        #
        # That is a recovery, NOT a guarantee. A CA-only chain defeats phi/psi
        # too, and phi/psi returns its PARTIAL map under ss_method="phi_psi"
        # (measured 65/130 on 3s7g with chain B stripped to CA, the other 65
        # scored on the loop floor). All-or-nothing is this branch's rule;
        # phi/psi's pre-existing partial behaviour is untouched here.
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
