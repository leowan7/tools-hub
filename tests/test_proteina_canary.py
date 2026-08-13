"""Offline unit tests for the Proteina hotspot canary's decision logic.

Runs fully offline — no Modal, no network, no GPU, no filesystem beyond
``tmp_path``. That is the whole point: ``tools/proteina/_hotspot_canary.py``
imports ``modal`` at module scope and constructs a ``modal.App`` when its body
runs, so no test could import it without dragging Modal into every offline run —
and there was no test file for it at all. NOT ONE of its 500+ lines was covered.
The harness that is the last gate before ~$16 of A100 time had itself never been
tested. The logic that decides anything now lives in
``tools/proteina/_canary_scoring.py``, which imports nothing outside the
standard library, and this file covers it. ``_hotspot_canary.py`` is examined
here only as SOURCE (via ``ast``), never imported — see TestHarnessStructure.

THE FOUR DEFECTS THIS FILE EXISTS TO PIN, each of which shipped:

1. ``(pos.get("centroid_distance_median") or 999) <= 10.0`` — a PERFECT result
   of ``0.0`` is falsy, becomes ``999`` and FAILS. The harness condemned the
   best possible outcome. (TestFalsyZeroRegression)
2. ``(neg_cross or 0) <= 0.2`` — an UNMEASURABLE (``None``) negative control
   becomes ``0`` and PASSES. The harness blessed a feature nobody measured.
   (TestUnmeasurableNoneRegression)
3. ``len(good) >= 6`` with no third outcome — when the per-design outputs turn
   out not to be complexes (which is exactly what phase 1 exists to DISCOVER),
   every verdict evaluates False and the harness prints "PHASE 2 FAILED — do
   NOT enable FLAG_TOOL_PROTEINA". That is a condemnation issued on an
   unmeasurable. (TestThreeOutcomes)
4. ``abs(hash((label, seed)))`` — ``hash()`` of a str is salted per process, so
   the registry key changed on every run and no two runs were comparable.
   (TestKeyStability, which spawns real subprocesses because that is the only
   way to observe PYTHONHASHSEED salting.)

AND THE FOUR AN INDEPENDENT QC PASS FOUND AFTER THAT, all of which also
shipped:

5. THE TARGET'S CHAIN LABEL WAS ASSUMED, NOT VERIFIED. ``run_shard`` passed the
   INPUT PDB's chain ids as ``target_chains`` and called everything else
   binder, with nothing checking the design output preserved that labelling —
   which the harness's own docstring says is unknown, since discovering it is
   why phase 1 exists. A design that emitted the binder on the target's chain
   id inverted the roles silently and reported ``hotspot_recall = 1.0`` off the
   binder's own contacts: a fabricated PASS on $12 of A100 time.
   (TestTargetChainIdentity)
6. THE TEST PINNING THE FATAL LOCAL-READ BUG MISSED THE OBVIOUS REWRITE OF IT.
   It counted only ``ast.Attribute`` calls named read_text/read_bytes/open, so
   a builtin ``open(path).read()`` — an ``ast.Name`` plus a ``.read()`` — walked
   straight through, and with it a complete local re-score. Both are now
   allowlists rather than denylists. (TestHarnessStructure)
7. PHASE 2 SPENT ~$12 BEFORE ANY LOCAL CHECK OF THE POSITIVE SPEC.
   ``--negative`` skipped ``pick_far_patch``, the only local code touching it,
   and ``pick_far_patch`` itself only refused when NO positive hotspot
   resolved. (TestPickFarPatch, TestHarnessStructure)
8. THE NEGATIVE CONTROL'S SEPARATION WAS MEASURED FROM THE POSITIVE PATCH'S
   CENTROID, so a residue a nominal 25 A away could sit 17 A from the nearest
   hotspot — well inside the reach of a 60-120 residue binder, i.e. a spurious
   $12 FAIL. (TestPickFarPatch)
"""

from __future__ import annotations

import ast
import glob
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from tools.proteina import _canary_scoring as cs
from tools.proteina import run_pipeline as rp

_SCORING_PATH = Path(cs.__file__).resolve()
_CANARY_PATH = _SCORING_PATH.parent / "_hotspot_canary.py"


# ---------------------------------------------------------------------------
# Hand-written PDB fixtures. Every coordinate is chosen so the expected answer
# can be computed on paper; nothing here is a golden value copied from a run.
# ---------------------------------------------------------------------------


def _atom(serial, name, resname, chain, resseq, x, y, z,
          record="ATOM  ", element=None, icode=" ", altloc=" "):
    """One PDB ATOM/HETATM line in strict column layout."""
    if element is None:
        element = name.lstrip("0123456789")[:1]
    return (
        f"{record}{serial:5d} {name:<4}{altloc:1}{resname:>3} "
        f"{chain}{resseq:4d}{icode:1}   "
        f"{x:8.3f}{y:8.3f}{z:8.3f}{1.00:6.2f}{0.00:6.2f}          {element:>2}"
    )


# Target chain A: six CA atoms 6 A apart along +x, so every distance below is
# arithmetic. Binder chain B: two CA atoms 4 A above A1 and A2.
#
#   A1(0,0,0)  A2(6,0,0)  A3(12,0,0)  A4(18,0,0)  A5(24,0,0)  A6(30,0,0)
#   B1(0,4,0)  B2(6,4,0)
#
# 4.0 <= 5.0 so B1 touches A1 and B2 touches A2; the next nearest pair is
# sqrt(6^2 + 4^2) = 7.21 > 5.0. Contacts are therefore EXACTLY {A1, A2}.
#
# A1 also carries a sidechain atom 20 A away on +z. That is the CA-vs-heavy-atom
# discriminator: a centroid built from every heavy atom moves; a CA centroid
# does not.
def _complex_pdb(*, with_solvent=True, with_hydrogen=True, with_modres=True):
    lines = [
        _atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0),
        _atom(2, "NZ", "LYS", "A", 1, 0.0, 0.0, 20.0),  # long sidechain
        _atom(3, "CA", "ALA", "A", 2, 6.0, 0.0, 0.0),
        _atom(4, "CA", "ALA", "A", 3, 12.0, 0.0, 0.0),
        _atom(5, "CA", "ALA", "A", 4, 18.0, 0.0, 0.0),
        _atom(6, "CA", "ALA", "A", 5, 24.0, 0.0, 0.0),
        _atom(7, "CA", "ALA", "A", 6, 30.0, 0.0, 0.0),
        _atom(8, "CA", "GLY", "B", 1, 0.0, 4.0, 0.0),
        _atom(9, "CA", "GLY", "B", 2, 6.0, 4.0, 0.0),
    ]
    if with_modres:
        # biotite counts MSE as protein, so it must be a selectable residue.
        lines.append(_atom(10, "CA", "MSE", "A", 7, 36.0, 0.0, 0.0, record="HETATM"))
    if with_solvent:
        # A water and a CALCIUM ION, both parked within contact range of the
        # binder. The calcium's atom name AND residue name are both "CA": a
        # parser that filters on the atom name alone turns it into a residue.
        lines.append(_atom(11, "O", "HOH", "A", 101, 0.0, 4.5, 0.0,
                           record="HETATM", element="O"))
        lines.append(_atom(12, "CA", "CA", "A", 201, 6.0, 4.5, 0.0,
                           record="HETATM", element="CA"))
    if with_hydrogen:
        # A binder hydrogen 4.0 A from A6. If hydrogens counted, A6 would be a
        # contact; it must not be.
        lines.append(_atom(13, "HZ1", "GLY", "B", 3, 30.0, 4.0, 0.0, element="H"))
    return "\n".join(lines) + "\n"


COMPLEX_PDB = _complex_pdb()
TARGET_CHAINS = {"A"}


def _lobed_target_pdb():
    """Two well-separated lobes plus junk, for negative-patch selection.

    Lobe 1 is a 2x2x2 lattice at the origin — the positive site. Lobe 2 is a
    3x3x3 lattice at x=40 with 5 A spacing, which gives it a real burial
    gradient: its 8 corners see 10 CA neighbours within 10 A, its 12 edge
    residues 13, its 6 face centres 18 and its single core residue 26.

    A water and a calcium ion sit at x=100/101 — further from the positive site
    than anything else in the file, and 1 A apart so they look maximally
    "exposed". A picker that walks raw heavy atoms selects exactly THEM, and
    ``missing_hotspots`` then rejects the tokens, aborting the negative shard.
    """
    lines = []
    serial = 1

    def lobe(start_res, ox, dim, spacing=5.0):
        nonlocal serial
        for i in range(dim ** 3):
            x = ox + (i % dim) * spacing
            y = ((i // dim) % dim) * spacing
            z = ((i // (dim * dim)) % dim) * spacing
            lines.append(_atom(serial, "CA", "ALA", "A", start_res + i, x, y, z))
            serial += 1

    lobe(1, 0.0, dim=2)     # A1 .. A8
    lobe(50, 40.0, dim=3)   # A50 .. A76
    lines.append(_atom(900, "O", "HOH", "A", 999, 100.0, 0.0, 0.0,
                       record="HETATM", element="O"))
    lines.append(_atom(901, "CA", "CA", "A", 998, 101.0, 0.0, 0.0,
                       record="HETATM", element="CA"))
    return "\n".join(lines) + "\n"


LOBED_PDB = _lobed_target_pdb()


# A shape where "nearest to the far seed" is emphatically NOT "far":
#
#   A1 (0,0,0)   A2 (4,0,0)   -> the positive patch, centroid (2,0,0)
#   A24(24,0,0)  A26(26,0,0)  -> 22 A / 24 A away, INSIDE the 25 A floor
#   A28(28,0,0)               -> 26 A away
#   A40(2,0,40)               -> 40 A away and isolated, so the residues
#                                nearest to it are the POSITIVE ones (40.05 A)
#
# The original picked the furthest residue as a seed and then took the k
# residues nearest that seed out of the WHOLE structure, so here it returns the
# positive hotspot itself as part of the "negative" control.
# The positive patch, and a far region that is nothing but dense core: a 3x3x3
# lattice at 3 A spacing, where even the corner residues have 25 CA neighbours
# within 10 A. There is no designable surface site far enough away, so handing
# back a "negative control" here would measure "a binder cannot dock into a
# buried pocket" — which is true of any pocket and says nothing about hotspots.
def _buried_far_pdb():
    lines = [
        _atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0),
        _atom(2, "CA", "ALA", "A", 2, 5.0, 0.0, 0.0),
    ]
    serial = 3
    for i in range(27):
        lines.append(_atom(serial, "CA", "ALA", "A", 50 + i,
                           40.0 + (i % 3) * 3.0,
                           ((i // 3) % 3) * 3.0,
                           ((i // 9) % 3) * 3.0))
        serial += 1
    return "\n".join(lines) + "\n"


BURIED_FAR_PDB = _buried_far_pdb()


# The positive patch, an exposed 4-residue protrusion at x=40..52, and a dense
# buried ball at x=80..86 that is FURTHER from the positive site than the
# protrusion. Picking the seed by distance lands in the core; picking it by
# exposure lands on the protrusion.
def _protrusion_pdb():
    lines = [
        _atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0),
        _atom(2, "CA", "ALA", "A", 2, 5.0, 0.0, 0.0),
    ]
    for n, x in enumerate((40.0, 44.0, 48.0, 52.0)):
        lines.append(_atom(3 + n, "CA", "ALA", "A", 10 + n, x, 0.0, 0.0))
    serial = 7
    for i in range(27):
        lines.append(_atom(serial, "CA", "ALA", "A", 50 + i,
                           80.0 + (i % 3) * 3.0,
                           ((i // 3) % 3) * 3.0,
                           ((i // 9) % 3) * 3.0))
        serial += 1
    return "\n".join(lines) + "\n"


PROTRUSION_PDB = _protrusion_pdb()


# Two far residues that are BOTH isolated from each other, so "nearest to the
# far seed" out of the WHOLE structure is still a POSITIVE hotspot (40.05 A)
# rather than the other far residue (80 A). That is what exposes the
# bleed-back: a patch drawn from all residues puts the positive site inside the
# negative control.
#
# A24/A26/A28 sit 20/22/24 A from the NEAREST positive hotspot (A2 at x=4) even
# though A28 is 26 A from the positive CENTROID — the finding-8 gap, in the
# fixture.
BLEEDBACK_PDB = "\n".join([
    _atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0),
    _atom(2, "CA", "ALA", "A", 2, 4.0, 0.0, 0.0),
    _atom(3, "CA", "ALA", "A", 24, 24.0, 0.0, 0.0),
    _atom(4, "CA", "ALA", "A", 26, 26.0, 0.0, 0.0),
    _atom(5, "CA", "ALA", "A", 28, 28.0, 0.0, 0.0),
    _atom(6, "CA", "ALA", "A", 40, 2.0, 0.0, 40.0),
    _atom(7, "CA", "ALA", "A", 41, 2.0, 0.0, -40.0),
]) + "\n"


# A positive patch that SPANS 14 A, which is what a real 4-residue patch does.
#
#   A1(-7,0,0)  A2(7,0,0)     -> positive; centroid (0,0,0)
#   A25(25.5,0,0)             -> 25.5 A from the CENTROID (clears a 25 A floor
#                                measured there) but only 18.5 A from A2, the
#                                nearest positive hotspot. A 60-120 residue
#                                binder is over 40 A across, so a binder docked
#                                here brushes the positive site — and the
#                                resulting cross-recall is a spurious $12 FAIL.
#   A40..A56 (x = 40,44,48,52,56) -> genuinely far: >= 33 A from A2.
def _spanning_positive_pdb():
    lines = [
        _atom(1, "CA", "ALA", "A", 1, -7.0, 0.0, 0.0),
        _atom(2, "CA", "ALA", "A", 2, 7.0, 0.0, 0.0),
        _atom(3, "CA", "ALA", "A", 25, 25.5, 0.0, 0.0),
    ]
    for n, x in enumerate((40.0, 44.0, 48.0, 52.0, 56.0)):
        lines.append(_atom(4 + n, "CA", "ALA", "A", 40 + n * 4, x, 0.0, 0.0))
    return "\n".join(lines) + "\n"


SPAN_PDB = _spanning_positive_pdb()


# 60 residues, so a spec of ['A1','A2','A3','A99999'] has three tokens that
# resolve and one that does not — the shape that used to buy three A100
# startups before anything noticed.
SIXTY_RES_PDB = "\n".join(
    _atom(i, "CA", "ALA", "A", i, i * 4.0, 0.0, 0.0) for i in range(1, 61)
) + "\n"


# ---------------------------------------------------------------------------
# The chain-relabelling fixtures. A target whose sequence contains NO glycine,
# and a de-novo binder that is all glycine, so residue-name identity between
# them is exactly 0.0 and nothing about the comparison is a coincidence.
# ---------------------------------------------------------------------------

_TARGET_SEQ = (
    "ALA", "SER", "THR", "VAL", "LEU", "ILE", "PHE", "TYR", "TRP", "MET",
    "CYS", "ASN", "GLN", "ASP", "GLU", "LYS", "ARG", "HIS", "PRO", "ALA",
    "SER", "THR", "VAL", "LEU", "ILE", "PHE", "TYR", "TRP", "MET", "CYS",
)
assert "GLY" not in _TARGET_SEQ


def _trace(chain, resnames, *, y=0.0, spacing=4.0, first_res=1, serial0=1):
    """A straight CA trace along +x."""
    return [_atom(serial0 + i, "CA", rn, chain, first_res + i,
                  i * spacing, y, 0.0)
            for i, rn in enumerate(resnames)]


# What the operator uploaded: chain A, residues 1..30, a real sequence.
INPUT_TARGET_PDB = "\n".join(_trace("A", _TARGET_SEQ)) + "\n"

# What a design output looks like if upstream keeps the input's labelling:
# chain A is the target (same numbers, same names), chain B is the binder,
# sitting 4 A off residues A1..A4.
CORRECT_DESIGN_PDB = "\n".join(
    _trace("A", _TARGET_SEQ)
    + _trace("B", ["GLY"] * 4, y=4.0, serial0=100)
) + "\n"

# ...and if upstream labels the BINDER as chain A. Identical geometry, so the
# contact set and every number derived from it are the same — the ONLY
# difference is which molecule the residues belong to, which is precisely what
# coordinates cannot tell you and residue names can.
RELABELLED_DESIGN_PDB = "\n".join(
    _trace("A", ["GLY"] * 30)
    + _trace("B", _TARGET_SEQ[:4], y=4.0, serial0=100)
) + "\n"

# The target, correctly on chain A, but renumbered from 1000. Hotspot tokens
# are matched by number, so "A1" now names a different residue and no score
# computed against this file means anything.
RENUMBERED_DESIGN_PDB = "\n".join(
    _trace("A", _TARGET_SEQ, first_res=1000)
    + _trace("B", ["GLY"] * 4, y=4.0, serial0=100)
) + "\n"

# The reference ``run_shard`` builds from the uploaded PDB: (chain, resseq) ->
# residue name over the contig's selection.
TARGET_REFERENCE = cs.ca_resnames(cs.heavy_atoms(INPUT_TARGET_PDB))


def _shard(label="positive", *, n=8, recall=1.0, centroid=0.0, cross=None,
           exit_code=0, is_complex=True, error=None, target_verified=True,
           cross_reference_hotspots=("A1", "A2", "A3", "A4"), expected=None,
           cross_found=None, name=None):
    """A synthetic ``run_shard`` return value with the medians kept consistent.

    ``cross_reference_hotspots`` is not decoration: the negative verdict's
    ceiling is a COUNT of positive hotspots, so it needs the denominator the
    real shard always reports.

    ``expected`` is ``n_designs_expected`` — how many designs the shard ORDERED
    (``nsamples * replicas``) as opposed to how many came back. It defaults to
    ``n`` so a synthetic shard describes a run upstream did not filter, which is
    what every test written before the absolute floor existed meant; pass it
    explicitly to describe one it did. The real shard always reports it, so
    omitting it from this helper would exercise a shape production never emits.

    EVERY DESIGN GETS ITS OWN NAME, because upstream writes one file per sample
    and ``run_shard`` records the basename. This helper used to stamp
    ``"sample.pdb"`` on all ``n`` of them — the exact shape QC's F1 measured, one
    name across every "design" — so it described a run production does not emit
    and could not tell a real shard from a duplicated one. Pass ``name`` to
    describe a shard whose files really did collide.

    ``cross_found`` is ``cross_requested_found_in_structure``: how many of the
    cross-reference hotspots each design actually CONTAINS. It defaults to all
    of them, i.e. an output that carries the whole target, which is what every
    test written before the crop gate existed meant. Pass it to describe a
    design that cropped the target.
    """
    if error is not None:
        return {"label": label, "error": error}
    n_cross = len(cs.parse_spec(cross_reference_hotspots)[0])
    designs = []
    for i in range(n):
        entry = {"name": name or f"sample_{i}.pdb", "chains": ["A", "B"],
                 "is_complex": is_complex}
        if is_complex:
            entry["target_verified"] = target_verified
            entry.update(hotspot_recall=recall, centroid_distance=centroid,
                         cross_hotspot_recall=cross, contacts=4)
            if n_cross:
                entry["cross_requested_found_in_structure"] = (
                    n_cross if cross_found is None else cross_found)
        designs.append(entry)
    measurable = is_complex and target_verified
    return {
        "label": label,
        "exit_code": exit_code,
        "designs": designs,
        "n_designs_expected": n if expected is None else expected,
        "n_complexes": n if is_complex else 0,
        "n_target_verified": n if measurable else 0,
        "cross_reference_hotspots": list(cross_reference_hotspots),
        "hotspot_recall_median": recall if measurable else None,
        "centroid_distance_median": centroid if measurable else None,
        "cross_hotspot_recall_median": cross if measurable else None,
    }


# ---------------------------------------------------------------------------
# 1. Geometry — every expected value computed by hand from the fixture above
# ---------------------------------------------------------------------------


class TestGeometry:
    def test_contacts_are_exactly_the_two_touching_residues(self):
        assert cs.contacts(COMPLEX_PDB, TARGET_CHAINS) == {("A", 1), ("A", 2)}

    def test_a_residue_just_past_the_cutoff_is_not_a_contact(self):
        """A3 is sqrt(6^2+4^2)=7.21 A from the nearest binder atom."""
        hits = cs.contacts(COMPLEX_PDB, TARGET_CHAINS)
        assert ("A", 3) not in hits

    def test_cutoff_is_inclusive_at_exactly_5A(self):
        text = (_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0) + "\n"
                + _atom(2, "CA", "GLY", "B", 1, 5.0, 0.0, 0.0) + "\n")
        assert cs.contacts(text, {"A"}) == {("A", 1)}

    def test_water_and_metal_ions_are_never_contact_residues(self):
        """Both sit 0.5 A from a binder atom. A calcium ion has atom name AND
        residue name "CA", so an atom-name-only filter makes it a residue.

        Covered by the SOLVENT_RESNAMES drop in ``heavy_atoms``, not by the
        HETATM guard in ``_polymer_ca_atoms`` — see the two tests below, which
        are what actually cover that guard. Believing this one covered it is
        how the guard came to carry a docstring claiming a justification that
        was already handled somewhere else.
        """
        hits = cs.contacts(COMPLEX_PDB, TARGET_CHAINS)
        assert ("A", 101) not in hits, "a water became a contact residue"
        assert ("A", 201) not in hits, "a calcium ion became a contact residue"

    def test_a_hetatm_amino_acid_outside_modres_is_not_a_polymer_residue(self):
        """THE reachable path for the HETATM guard, and the real reason it
        exists.

        Norleucine is a HETATM carrying a genuine CA, and it is in NO solvent
        list — so ``heavy_atoms``'s resname drop does not touch it. Only the
        ``hetatm and resname not in MODRES_EQUIV`` guard excludes it. It must
        be excluded because ``run_pipeline.pdb_ca_residues`` excludes it by the
        identical rule: a residue the canary can see but upstream cannot is a
        residue the canary will put in a negative patch and upstream will then
        reject, aborting the shard.
        """
        text = (_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0) + "\n"
                + _atom(2, "CA", "NLE", "A", 2, 4.0, 0.0, 0.0, record="HETATM")
                + "\n")
        assert "NLE" not in cs.SOLVENT_RESNAMES
        assert "NLE" not in cs.MODRES_EQUIV
        atoms = cs.heavy_atoms(text)
        assert len(atoms) == 2, "heavy_atoms must not drop it — it is not solvent"
        assert cs.ca_positions(atoms) == {("A", 1): (0.0, 0.0, 0.0)}
        assert cs.ca_resnames(atoms) == {("A", 1): "ALA"}

    def test_the_polymer_filter_agrees_with_run_pipeline_on_hetatm_residues(
            self, tmp_path):
        """The two parsers must see the SAME residue set, or the canary
        computes a patch upstream refuses."""
        text = "\n".join([
            _atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0),
            _atom(2, "CA", "NLE", "A", 2, 4.0, 0.0, 0.0, record="HETATM"),
            _atom(3, "CA", "MSE", "A", 3, 8.0, 0.0, 0.0, record="HETATM"),
            _atom(4, "CA", "ORN", "A", 4, 12.0, 0.0, 0.0, record="HETATM"),
        ]) + "\n"
        path = tmp_path / "hetatm.pdb"
        path.write_text(text)
        upstream, _ = rp.pdb_ca_residues(path)
        assert {(c, r) for c, r, _icode in upstream} == set(
            cs.ca_positions(cs.heavy_atoms(text)))

    def test_a_calcium_ion_is_still_not_a_residue_without_the_solvent_drop(self):
        """``ca_positions`` is public and takes any atom list. With
        ``drop_solvent=False`` the calcium reaches it, and the HETATM guard is
        the only thing left standing between it and the residue set."""
        text = (_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0) + "\n"
                + _atom(2, "CA", "CA", "A", 201, 6.0, 0.0, 0.0,
                        record="HETATM", element="CA") + "\n")
        atoms = cs.heavy_atoms(text, drop_solvent=False)
        assert len(atoms) == 2, "the fixture must actually deliver the ion here"
        assert cs.ca_positions(atoms) == {("A", 1): (0.0, 0.0, 0.0)}

    def test_hydrogens_are_excluded(self):
        """The binder hydrogen is 4.0 A from A6 — inside the 5 A cutoff."""
        assert ("A", 6) not in cs.contacts(COMPLEX_PDB, TARGET_CHAINS)

    def test_numeric_prefixed_hydrogen_names_are_still_hydrogens(self):
        """PDB v2 writes some hydrogens as "1HB"; taking name[:1] yields "1"."""
        line = _atom(1, "1HB", "ALA", "A", 1, 0.0, 0.0, 0.0, element="")
        assert cs.heavy_atoms(line + "\n") == []

    def test_only_model_one_is_parsed(self):
        text = (COMPLEX_PDB + "ENDMDL\n"
                + _atom(99, "CA", "ALA", "A", 5000, 99.0, 99.0, 99.0) + "\n")
        assert ("A", 5000) not in cs.ca_positions(cs.heavy_atoms(text))

    def test_modified_residues_are_polymer(self):
        pos = cs.ca_positions(cs.heavy_atoms(COMPLEX_PDB))
        assert ("A", 7) in pos, "MSE must be selectable — biotite treats it as protein"

    def test_the_modres_set_matches_run_pipeline(self):
        """A residue upstream counts as protein must be selectable here too, or
        the canary computes a patch upstream then refuses."""
        assert cs.MODRES_EQUIV == rp._MODRES_EQUIV

    def test_the_modres_parent_table_matches_run_pipeline(self):
        """F11. ``run_pipeline._MODRES_PARENT`` is a hand-maintained copy of
        this one, and the comment above it says so: ``modal_app.py`` copies
        ``run_pipeline.py`` into the production image and nothing else, so
        ``_canary_scoring`` does not exist in production and cannot be
        imported. "Keep the two in step by hand" was the entire mechanism.

        Drift here is not cosmetic in either direction: the canary decides
        whether ~$4-$12 of A100 time is spent, and the production copy decides
        whether the delivered design carries the operator's residue numbers.
        The same table answering the two questions differently is how a run the
        canary blessed comes back in 1..N.
        """
        assert cs.MODRES_PARENT == rp._MODRES_PARENT

    def test_the_renumber_floors_match_run_pipeline(self):
        """The other half of the same hand-maintained duplication: the identity
        floor and both informative floors. This repo has already paid for an
        A100 on exactly this drift class, and a comment is not a mechanism."""
        assert cs.TARGET_MIN_SEQUENCE_IDENTITY == rp._RENUMBER_MIN_IDENTITY
        assert cs.TARGET_MIN_INFORMATIVE_RESIDUES == rp._RENUMBER_MIN_INFORMATIVE
        assert (cs.TARGET_MIN_INFORMATIVE_FRACTION
                == rp._RENUMBER_MIN_INFORMATIVE_FRACTION)

    def test_the_unknown_resnames_match_run_pipeline(self):
        """"I do not know what this is" has to mean the same thing on both
        sides. A name treated as unknown by one and as evidence by the other
        moves the informative COUNT and the informative FRACTION, which are the
        two floors the test above pins the values of."""
        assert cs.UNKNOWN_RESNAMES == rp._UNKNOWN_RESNAMES

    def test_ca_centroid_uses_ca_only_not_whole_residues(self):
        """A1 is CA(0,0,0) plus a sidechain at (0,0,20). Averaging every heavy
        atom gives (0,0,10); the CA centroid is (0,0,0)."""
        atoms = cs.heavy_atoms(COMPLEX_PDB)
        assert cs.ca_centroid(atoms, [("A", 1)]) == (0.0, 0.0, 0.0)

    def test_centroid_of_two_residues(self):
        atoms = cs.heavy_atoms(COMPLEX_PDB)
        assert cs.ca_centroid(atoms, [("A", 1), ("A", 2)]) == (3.0, 0.0, 0.0)

    def test_centroid_of_absent_residues_is_none_not_the_origin(self):
        atoms = cs.heavy_atoms(COMPLEX_PDB)
        assert cs.ca_centroid(atoms, [("Z", 1)]) is None

    def test_insertion_coded_twin_collapses_onto_its_parent(self):
        """Upstream's match key is chain+number with no insertion code."""
        text = (_atom(1, "CA", "ALA", "A", 13, 0.0, 0.0, 0.0) + "\n"
                + _atom(2, "CA", "LEU", "A", 13, 9.0, 0.0, 0.0, icode="A") + "\n")
        pos = cs.ca_positions(cs.heavy_atoms(text))
        assert pos == {("A", 13): (0.0, 0.0, 0.0)}

    def test_dist_is_euclidean(self):
        assert cs.dist((0, 0, 0), (3, 4, 0)) == 5.0

    def test_median_odd_and_even(self):
        assert cs.median([3, 1, 2]) == 2
        assert cs.median([1, 2, 3, 4]) == 2.5
        assert cs.median([None, None]) is None
        assert cs.median([]) is None

    def test_median_keeps_a_real_zero(self):
        """0.0 is a measurement, not a missing value."""
        assert cs.median([0.0, 0.0]) == 0.0


class TestScoreDesign:
    def test_perfect_hit_scores_recall_one_and_distance_zero(self):
        out = cs.score_design(COMPLEX_PDB, TARGET_CHAINS, ["A1", "A2"])
        assert out["hotspot_recall"] == 1.0
        assert out["centroid_distance"] == 0.0

    def test_half_hit_and_a_hand_computed_offset(self):
        """want={A1,A6} centroid (15,0,0); contacts={A1,A2} centroid (3,0,0),
        so the offset is exactly 12.0.

        The original averaged every heavy ATOM rather than one CA per residue,
        which over-weights A1 (it carries two atoms) and yields 8.0 here.
        """
        out = cs.score_design(COMPLEX_PDB, TARGET_CHAINS, ["A1", "A6"])
        assert out["hotspot_recall"] == 0.5
        assert out["centroid_distance"] == pytest.approx(12.0)

    def test_a_sidechain_cannot_move_the_scored_centroid(self):
        """Contacts are {A1,A2} at CA centroid (3,0,0); the requested patch is
        {A6} at (30,0,0), so the offset is exactly 27.0.

        A1 carries a sidechain atom 20 A off its backbone. Any centroid that
        includes sidechains puts the contact centroid at (3,0,5) and the offset
        at 27.46 — and moving that one atom further would move the score again.
        A number compared against a 10 A threshold must not be steerable by a
        single lysine.
        """
        base = cs.score_design(COMPLEX_PDB, TARGET_CHAINS, ["A6"])
        assert base["hotspot_recall"] == 0.0
        assert base["centroid_distance"] == pytest.approx(27.0)

        moved = COMPLEX_PDB.replace(
            _atom(2, "NZ", "LYS", "A", 1, 0.0, 0.0, 20.0),
            _atom(2, "NZ", "LYS", "A", 1, 0.0, 0.0, 120.0))
        assert moved != COMPLEX_PDB, "the fixture line was not replaced"
        after = cs.score_design(moved, TARGET_CHAINS, ["A6"])
        assert after["centroid_distance"] == pytest.approx(27.0)
        assert after["contacts"] == base["contacts"]

    def test_complete_miss_scores_zero_not_none(self):
        """A real, measured 0.0. Distinguishing this from "unmeasurable" is the
        entire reason the outcome type is three-valued."""
        out = cs.score_design(COMPLEX_PDB, TARGET_CHAINS, ["A5", "A6"])
        assert out["hotspot_recall"] == 0.0
        assert out["centroid_distance"] == pytest.approx(24.0)

    def test_binder_only_file_is_unmeasurable_not_zero(self):
        binder_only = "\n".join([
            _atom(1, "CA", "GLY", "B", 1, 0.0, 4.0, 0.0),
            _atom(2, "CA", "GLY", "B", 2, 6.0, 4.0, 0.0),
        ]) + "\n"
        out = cs.score_design(binder_only, TARGET_CHAINS, ["A1", "A2"])
        assert out["contacts"] == 0
        assert out["hotspot_recall"] is None
        assert out["centroid_distance"] is None

    def test_no_requested_hotspots_is_unmeasurable(self):
        """The null shard asks for nothing, so it has no own-recall at all."""
        out = cs.score_design(COMPLEX_PDB, TARGET_CHAINS, [])
        assert out["hotspot_recall"] is None

    def test_reference_patch_absent_from_the_structure_is_unmeasurable(self):
        """Recall 0.0 against a patch that is not even in the file would be a
        false FAIL, not a measurement."""
        out = cs.score_design(COMPLEX_PDB, TARGET_CHAINS, ["Z1", "Z2"])
        assert out["hotspot_recall"] is None
        assert out["requested_found_in_structure"] == 0

    def test_the_two_entry_points_agree_on_hand_computed_values(self):
        """``score_design_file`` reuses ONE contact search across both specs,
        so the cross score comes out of ``score_from_contacts`` while the
        one-shot wrapper is what the tests above pin. If those two ever
        disagree, every phase-2 cross score silently means something different
        from every own score.

        Each expectation is computed on paper from the fixture, so the test
        fails when either path changes — an agreement-only assertion passes
        happily while BOTH regress together, which is the failure mode that
        matters when one is derived from the other.
        """
        expected = {
            ("A1", "A2"): (1.0, 0.0),    # both contacts; centroids coincide
            ("A1", "A6"): (0.5, 12.0),   # want (15,0,0) vs hits (3,0,0)
            ("A5", "A6"): (0.0, 24.0),   # want (27,0,0) vs hits (3,0,0)
            (): (None, None),            # the null shard asks for nothing
            ("Z9",): (None, None),       # not in the structure at all
            ("x",): (None, None),        # unparsable -> unscorable
        }
        atoms = cs.heavy_atoms(COMPLEX_PDB)
        positions = cs.ca_positions(atoms)
        hits = cs.contacts_from_atoms(atoms, TARGET_CHAINS, positions=positions)
        assert hits == {("A", 1), ("A", 2)}
        for spec, (recall, offset) in expected.items():
            reusable = cs.score_from_contacts(hits, list(spec), positions)
            oneshot = cs.score_design(COMPLEX_PDB, TARGET_CHAINS, list(spec))
            assert reusable == oneshot, spec
            assert reusable["hotspot_recall"] == recall, spec
            if offset is None:
                assert reusable["centroid_distance"] is None, spec
            else:
                assert reusable["centroid_distance"] == pytest.approx(offset), spec
            # One search, reused: the contact count cannot depend on the spec.
            assert reusable["contacts"] == 2, spec

    def test_an_unreadable_token_makes_the_design_unscorable(self):
        """The original silently dropped tokens it could not parse, shrinking
        the denominator — so a run scored BETTER the more malformed its request
        was. That is the same silent-drop failure the canary exists to catch."""
        out = cs.score_design(COMPLEX_PDB, TARGET_CHAINS, ["A1", "A2", "oops"])
        assert out["unparsable_hotspots"] == ["oops"]
        assert out["hotspot_recall"] is None


class TestParseSpec:
    def test_basic(self):
        assert cs.parse_spec(["A45", "C73"]) == ({("A", 45), ("C", 73)}, [])

    def test_multi_character_chain_keeps_its_whole_label(self):
        assert cs.parse_spec(["AB45"]) == ({("AB", 45)}, [])

    def test_negative_residue_numbers(self):
        assert cs.parse_spec(["A-5"]) == ({("A", -5)}, [])

    def test_bad_tokens_are_returned_not_swallowed(self):
        ok, bad = cs.parse_spec(["A45", "45", "", "A4x"])
        assert ok == {("A", 45)}
        assert bad == ["45", "A4x"]


# ---------------------------------------------------------------------------
# 2. THE 0.0-is-falsy regression
# ---------------------------------------------------------------------------


class TestFalsyZeroRegression:
    def test_the_old_idiom_inverts_the_value_this_module_actually_computes(self):
        """The old idiom applied to a REAL output of this code, not to a
        hand-typed 0.0.

        A test that only asserts `(0.0 or 999) <= 10.0` is False asserts a
        property of CPython and cannot fail against this codebase however it
        changes. Feeding it the number ``score_design`` really produces for a
        perfect hit ties it to this module: it fails if the perfect case stops
        being 0.0, or if the threshold moves, or if the verdict regresses.
        """
        perfect = cs.score_design(COMPLEX_PDB, TARGET_CHAINS, ["A1", "A2"])
        assert perfect["centroid_distance"] == 0.0
        ceiling = cs.DEFAULT_THRESHOLDS.max_centroid_a
        assert ((perfect["centroid_distance"] or 999) <= ceiling) is False
        verdict = cs.positive_verdict(_shard(
            recall=perfect["hotspot_recall"],
            centroid=perfect["centroid_distance"]))
        assert verdict.outcome == cs.PASS, verdict.reason

    def test_a_perfect_centroid_distance_of_zero_passes(self):
        """THE regression. Fails against the old code, which computed
        `(0.0 or 999) <= 10.0` -> False and condemned the best possible run."""
        verdict = cs.positive_verdict(_shard(recall=1.0, centroid=0.0))
        assert verdict.outcome == cs.PASS, verdict.reason

    def test_a_perfect_negative_control_passes(self):
        """Same idiom, second site: `(neg.centroid_distance_median or 999)`."""
        neg = _shard("negative", recall=1.0, centroid=0.0, cross=0.0)
        verdict = cs.negative_verdict(neg)
        assert verdict.outcome == cs.PASS, verdict.reason

    def test_a_perfect_zero_cross_recall_is_not_treated_as_missing(self):
        neg = _shard("negative", recall=1.0, centroid=1.0, cross=0.0)
        assert cs.negative_verdict(neg).outcome == cs.PASS

    def test_a_centroid_just_over_the_threshold_still_fails(self):
        verdict = cs.positive_verdict(_shard(recall=1.0, centroid=10.5))
        assert verdict.outcome == cs.FAIL


# ---------------------------------------------------------------------------
# 3. THE unmeasurable-None regression (a false PASS)
# ---------------------------------------------------------------------------


class TestUnmeasurableNoneRegression:
    def test_the_old_idiom_passes_the_unmeasurable_value_this_module_produces(self):
        """Again with a real output rather than a typed ``None``.

        A binder-only design has no measurable cross-recall, ``median`` over a
        shard of them is ``None``, and ``(None or 0) <= 0.2`` is the shipped
        false PASS. Deriving the ``None`` from ``score_design`` + ``median``
        makes the test fail if either ever starts reporting an unmeasurable as
        a zero — which is the actual regression being guarded.
        """
        binder_only = "\n".join([
            _atom(1, "CA", "GLY", "B", 1, 0.0, 4.0, 0.0),
            _atom(2, "CA", "GLY", "B", 2, 6.0, 4.0, 0.0),
        ]) + "\n"
        per_design = [cs.score_design(binder_only, TARGET_CHAINS, ["A1", "A2"])
                      for _ in range(8)]
        assert all(d["hotspot_recall"] is None for d in per_design)
        cross = cs.median(d["hotspot_recall"] for d in per_design)
        assert cross is None
        assert ((cross or 0) <= 0.2) is True  # the shipped false pass
        neg = _shard("negative", recall=1.0, centroid=1.0, cross=cross)
        assert cs.negative_verdict(neg).outcome == cs.INCONCLUSIVE

    def test_a_none_cross_recall_does_not_pass_the_negative_verdict(self):
        """THE regression. Fails against the old code, where `None or 0` -> 0
        and the negative control passed without anything being measured."""
        neg = _shard("negative", recall=1.0, centroid=1.0, cross=None)
        verdict = cs.negative_verdict(neg)
        assert verdict.outcome != cs.PASS
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason

    def test_binder_only_designs_make_the_negative_control_inconclusive(self):
        neg = _shard("negative", is_complex=False)
        assert cs.negative_verdict(neg).outcome == cs.INCONCLUSIVE

    def test_a_measured_cross_recall_over_the_ceiling_fails(self):
        neg = _shard("negative", recall=1.0, centroid=1.0, cross=0.75)
        assert cs.negative_verdict(neg).outcome == cs.FAIL

    def test_an_unmeasurable_own_centroid_is_inconclusive_not_a_pass(self):
        neg = _shard("negative", recall=1.0, centroid=None, cross=0.0)
        assert cs.negative_verdict(neg).outcome == cs.INCONCLUSIVE


# ---------------------------------------------------------------------------
# 4. Three outcomes, and zero scorable designs is never a verdict
# ---------------------------------------------------------------------------


class TestThreeOutcomes:
    def test_the_exit_code_table_is_total_and_only_pass_is_green(self):
        """``_finish`` does ``cs.EXIT_CODES[outcome]`` with no default, so a
        missing entry is a KeyError at the end of a $12 run, and a duplicated
        code makes "we could not measure this" indistinguishable from "this is
        broken" at the shell — which is the whole reason there are three.
        """
        assert set(cs.OUTCOMES) == {cs.PASS, cs.FAIL, cs.INCONCLUSIVE}
        assert set(cs.EXIT_CODES) == set(cs.OUTCOMES), "the table is not total"
        assert len(set(cs.EXIT_CODES.values())) == 3, "two outcomes share a code"
        assert [o for o, c in cs.EXIT_CODES.items() if c == 0] == [cs.PASS]
        with pytest.raises(ValueError):
            cs.Verdict("x", "MAYBE", "an outcome outside the three")
        for outcome in cs.OUTCOMES:
            assert cs.Verdict("x", outcome, "").outcome == outcome

    def test_all_three_are_reachable_from_the_positive_verdict(self):
        outcomes = {
            cs.positive_verdict(_shard(recall=1.0, centroid=0.0)).outcome,
            cs.positive_verdict(_shard(recall=0.0, centroid=40.0)).outcome,
            cs.positive_verdict(_shard(is_complex=False)).outcome,
        }
        assert outcomes == {cs.PASS, cs.FAIL, cs.INCONCLUSIVE}

    def test_zero_scorable_designs_is_inconclusive_never_pass_never_fail(self):
        """Whether the outputs are complexes at all is what phase 1 DISCOVERS.
        Condemning the feature because we could not look at it is not a result.
        Fails against the old code, whose only options were pass and fail."""
        for shard in (_shard(is_complex=False), _shard(n=0)):
            verdict = cs.positive_verdict(shard)
            assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
            assert verdict.outcome != cs.PASS
            assert verdict.outcome != cs.FAIL

    def test_a_crashed_shard_is_a_fail_not_an_inconclusive(self):
        """A non-zero exit is a real failure; only a CLEAN run that produced
        nothing measurable is inconclusive."""
        assert cs.positive_verdict(_shard(exit_code=1)).outcome == cs.FAIL
        assert cs.positive_verdict(_shard(error="registration failed")).outcome == cs.FAIL
        assert cs.positive_verdict(None).outcome == cs.FAIL

    def test_a_missing_exit_code_is_a_fail(self):
        shard = _shard()
        shard.pop("exit_code")
        assert cs.positive_verdict(shard).outcome == cs.FAIL

    def test_verdict_refuses_to_be_used_as_a_boolean(self):
        """Every defect here came from treating a result as a truthy scalar."""
        verdict = cs.positive_verdict(_shard())
        with pytest.raises(TypeError):
            bool(verdict)

    def test_verdict_refuses_to_be_compared_with_an_outcome_string(self):
        """``__bool__`` raising left a hole exactly its own size: the frozen
        dataclass still generated ``__eq__``, so ``verdict == cs.PASS``
        returned False SILENTLY on a verdict that had passed. That is the same
        class of error — a three-valued result treated as one of its values —
        and it read as a clean negative rather than as a mistake."""
        verdict = cs.positive_verdict(_shard(recall=1.0, centroid=0.0))
        assert verdict.outcome == cs.PASS
        with pytest.raises(TypeError):
            verdict == cs.PASS
        with pytest.raises(TypeError):
            cs.PASS == verdict          # the reflected comparison too
        with pytest.raises(TypeError):
            verdict != cs.FAIL          # `!=` routes through `__eq__`
        with pytest.raises(TypeError):
            assert verdict in (cs.PASS, cs.FAIL)   # and so does `in`

    def test_two_verdicts_still_compare_as_values(self):
        """Refusing the string comparison must not cost ordinary equality."""
        a = cs.Verdict("positive", cs.PASS, "reason", {"k": 1})
        assert a == cs.Verdict("positive", cs.PASS, "reason", {"k": 1})
        assert a != cs.Verdict("negative", cs.PASS, "reason", {"k": 1})
        assert a != cs.Verdict("positive", cs.FAIL, "reason", {"k": 1})
        assert (a == 17) is False, "only strings are the trap; other types are not"

    def test_overall_outcome_precedence(self):
        def v(outcome):
            return cs.Verdict("x", outcome, "")
        assert cs.overall_outcome([v(cs.PASS), v(cs.PASS)]) == cs.PASS
        assert cs.overall_outcome([v(cs.PASS), v(cs.INCONCLUSIVE)]) == cs.INCONCLUSIVE
        assert cs.overall_outcome([v(cs.FAIL), v(cs.INCONCLUSIVE)]) == cs.FAIL

    def test_an_empty_verdict_set_is_never_a_pass(self):
        """`all([])` is True — the same vacuous-truth trap as phase 0's."""
        assert cs.overall_outcome([]) == cs.INCONCLUSIVE

    def test_the_hit_threshold_scales_with_the_scorable_design_count(self):
        """The original hardcoded `>= 6` against a shard that emits
        nsamples(4) * replicas(2) = 8 — a fraction dressed up as a count.

        Pinned BEHAVIOURALLY at the boundary rather than by re-deriving
        ``ceil(min_hit_fraction * n)``: recomputing the implementation's own
        formula in the assertion cannot detect a change to that formula, since
        both sides move together.
        """
        def with_on_patch(n, k):
            """n scorable designs, k of which reached the requested patch."""
            shard = _shard(n=n, recall=1.0, centroid=1.0)
            for design in shard["designs"][k:]:
                design["hotspot_recall"] = 0.0
            return cs.positive_verdict(shard)

        # 8 designs: 6 is the boundary. Both sides of it, no formula in sight.
        assert with_on_patch(8, 6).outcome == cs.PASS, with_on_patch(8, 6).reason
        assert with_on_patch(8, 5).outcome == cs.FAIL
        assert with_on_patch(8, 6).metrics["n_required"] == 6
        # 4 designs: 3 is the boundary — the old `>= 6` FAILED every shard of
        # this size no matter how good it was.
        assert with_on_patch(4, 4).outcome == cs.PASS
        assert with_on_patch(4, 3).outcome == cs.PASS
        assert with_on_patch(4, 2).outcome == cs.FAIL
        # ...and one design still needs that one design.
        assert with_on_patch(1, 1).outcome == cs.PASS
        assert with_on_patch(1, 0).outcome == cs.FAIL

    def test_phase2_report_is_inconclusive_when_nothing_is_measurable(self):
        blind = _shard(is_complex=False)
        report = cs.phase2_report(blind, dict(blind, label="negative"),
                                  dict(blind, label="null"))
        assert report["overall"] == cs.INCONCLUSIVE
        assert report["exit_code"] == cs.EXIT_CODES[cs.INCONCLUSIVE]
        assert [v.outcome for v in report["verdicts"]] == [cs.INCONCLUSIVE] * 3

    def test_phase2_report_passes_only_when_all_three_controls_do(self):
        pos = _shard("positive", recall=1.0, centroid=0.0, cross=1.0)
        neg = _shard("negative", recall=1.0, centroid=2.0, cross=0.0)
        null = _shard("null", recall=None, centroid=None, cross=0.0)
        null["hotspot_recall_median"] = None
        null["centroid_distance_median"] = None
        report = cs.phase2_report(pos, neg, null)
        assert report["overall"] == cs.PASS, [v.reason for v in report["verdicts"]]
        assert report["exit_code"] == 0


# ---------------------------------------------------------------------------
# 5. The null control — the feature-is-a-lie detector
# ---------------------------------------------------------------------------


class TestNullControl:
    def test_a_null_run_scoring_the_same_as_the_positive_run_fails(self):
        """THE detector. If a shard given NO hotspots lands on the requested
        patch just as often as one that asked for it, the argument was passed
        and ignored — and every other signal (exit code, design count, reward
        CSV) is identical to a correct run."""
        pos = _shard("positive", cross=0.9, recall=0.9, centroid=1.0)
        null = _shard("null", cross=0.9, recall=None, centroid=None)
        verdict = cs.null_verdict(pos, null)
        assert verdict.outcome == cs.FAIL, verdict.reason
        assert "ignored" in verdict.reason

    def test_a_null_run_that_misses_the_patch_passes(self):
        pos = _shard("positive", cross=1.0)
        null = _shard("null", cross=0.25)
        assert cs.null_verdict(pos, null).outcome == cs.PASS

    def test_the_margin_boundary_is_strict(self):
        pos = _shard("positive", cross=1.0)
        assert cs.null_verdict(pos, _shard("null", cross=0.75)).outcome == cs.FAIL
        assert cs.null_verdict(pos, _shard("null", cross=0.70)).outcome == cs.PASS

    def test_an_unmeasurable_null_is_inconclusive_not_a_pass(self):
        pos = _shard("positive", cross=1.0)
        null = _shard("null", is_complex=False)
        assert cs.null_verdict(pos, null).outcome == cs.INCONCLUSIVE

    def test_a_null_run_of_zero_recall_against_a_zero_positive_fails(self):
        """Both measured, both zero: the hotspots demonstrably did nothing."""
        pos = _shard("positive", cross=0.0)
        null = _shard("null", cross=0.0)
        assert cs.null_verdict(pos, null).outcome == cs.FAIL

    def test_a_crashed_null_shard_is_a_fail(self):
        pos = _shard("positive", cross=1.0)
        assert cs.null_verdict(pos, _shard("null", exit_code=137)).outcome == cs.FAIL


# ---------------------------------------------------------------------------
# 6. Registry key stability across PROCESSES
# ---------------------------------------------------------------------------


# The exact recipe tools/proteina/_hotspot_canary.py::_load_by_path uses, so
# these subprocesses load the module the way the CONTAINER does — including the
# sys.modules registration, without which @dataclass raises at import time.
_LOAD_BY_PATH = (
    "import importlib.util, sys\n"
    f"spec = importlib.util.spec_from_file_location('cs', {str(_SCORING_PATH)!r})\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "sys.modules['cs'] = m\n"
    "spec.loader.exec_module(m)\n"
)


def _key_in_subprocess(expr: str, hashseed: str) -> str:
    env = dict(os.environ, PYTHONHASHSEED=hashseed)
    proc = subprocess.run([sys.executable, "-c", _LOAD_BY_PATH + f"print({expr})\n"],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class TestKeyStability:
    SEEDS = ("0", "1", "2", "7", "12345", "99991")

    def test_the_key_is_identical_across_processes_and_hash_seeds(self):
        """THE regression. `abs(hash((label, seed)))` is salted per process by
        PYTHONHASHSEED, so the registry key changed on every invocation and no
        two runs could be correlated. Only a subprocess can observe this — the
        salt is fixed for the lifetime of an interpreter."""
        seen = {
            _key_in_subprocess("m.canary_task_key('positive', 1234)", s)
            for s in self.SEEDS
        }
        assert len(seen) == 1, f"key is not stable across processes: {seen}"
        assert seen.pop() == cs.canary_task_key("positive", 1234)

    def test_the_salted_and_the_stable_key_are_measured_side_by_side(self):
        """Control AND regression in one subprocess, so neither can be vacuous.

        Printing only ``abs(hash(...))`` proves PYTHONHASHSEED works in CPython
        and nothing at all about this module — it would pass with
        ``_canary_scoring.py`` deleted. Emitting the old expression and
        ``canary_task_key`` from the SAME interpreter makes the control
        meaningful (the harness demonstrably detects salting) and the assertion
        load-bearing (this module's key demonstrably does not move) in one
        measurement, under identical conditions.
        """
        code = _LOAD_BY_PATH + (
            "print(abs(hash(('positive', 1234))) % 10**9, "
            "m.canary_task_key('positive', 1234))\n"
        )
        salted, stable = set(), set()
        for seed in self.SEEDS:
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run([sys.executable, "-c", code],
                                  capture_output=True, text=True, env=env)
            assert proc.returncode == 0, proc.stderr
            old, new = proc.stdout.split()
            salted.add(old)
            stable.add(new)
        assert len(salted) > 1, (
            "PYTHONHASHSEED did not vary str hashing, so the sibling test's "
            "green result would mean nothing")
        assert stable == {cs.canary_task_key("positive", 1234)}

    def test_the_key_satisfies_the_adapter_task_regex(self):
        """It becomes ++generation.task_name, which the adapter bounds."""
        from tools import proteina as px
        key = cs.canary_task_key("positive", 1234)
        assert px._TASK_RE.match(key)
        assert key.startswith(cs.CANARY_KEY_PREFIX)

    def test_distinct_labels_and_seeds_give_distinct_keys(self):
        keys = {
            cs.canary_task_key("positive", 1),
            cs.canary_task_key("negative", 1),
            cs.canary_task_key("null", 1),
            cs.canary_task_key("positive", 2),
        }
        assert len(keys) == 4

    def test_the_key_never_collides_with_a_curated_task(self):
        from tools import proteina as px
        curated = set(px._DEFAULT_TASK.values())
        assert cs.canary_task_key("positive", 1234) not in curated


# ---------------------------------------------------------------------------
# 7. The Hydra composition assertion — and its false positive
# ---------------------------------------------------------------------------


class TestHydraAssertion:
    KEY = "hub_canaryabc123456789"

    def _cfg(self, task_name, hotspots=("A45",), contig="A1-150"):
        return {
            "generation": {"task_name": task_name},
            "target_dict_cfg": {
                "02_PDL1": {
                    "source": "bindcraft_targets", "target_input": "A1-115",
                    "hotspot_residues": ["A37", "A39"],
                },
                self.KEY: {
                    "source": "tools_hub_upload", "target_input": contig,
                    "hotspot_residues": list(hotspots),
                },
            },
        }

    def test_the_old_substring_test_could_not_tell_selection_from_presence(self):
        """binder_generate.yaml composes the WHOLE registry into
        target_dict_cfg, so our key is in the resolved config the instant it is
        REGISTERED — whether or not task_name ever selected it. `if key in
        json.dumps(cfg)` therefore passes for a run that designed against a
        completely different target. That is a false positive on the one
        assertion phase 1 spends ~$4 to make."""
        cfg = self._cfg(task_name="02_PDL1")
        assert self.KEY in json.dumps(cfg)                       # old: "proved" it
        assert cs.hydra_assertion(cfg, self.KEY, ["A45"])["ok"] is False  # new: no

    def test_a_genuinely_selected_key_with_matching_hotspots_is_ok(self):
        result = cs.hydra_assertion(self._cfg(self.KEY), self.KEY, ["A45"], "A1-150")
        assert result["ok"] is True
        assert result["task_name_selected"] is True
        assert result["hotspots_match"] is True

    def test_selected_but_with_different_hotspots_is_not_ok(self):
        cfg = self._cfg(self.KEY, hotspots=("A99",))
        result = cs.hydra_assertion(cfg, self.KEY, ["A45"])
        assert result["task_name_selected"] is True
        assert result["hotspots_match"] is False
        assert result["ok"] is False

    def test_a_reordered_hotspot_list_is_reported_but_is_not_a_failure(self):
        """The observed value is whatever OmegaConf round-tripped through a
        YAML write and a Hydra compose; neither guarantees list order, and
        upstream matches hotspots by MEMBERSHIP (``f"{chain}{res}" in
        target_hotspots``), so a reordering changes nothing about the run.
        Demanding ordered equality spent $4 to FAIL a correct run."""
        cfg = self._cfg(self.KEY, hotspots=("A45", "A67"))
        result = cs.hydra_assertion(cfg, self.KEY, ["A67", "A45"])
        assert result["hotspots_match"] is True
        assert result["hotspots_order_matches"] is False
        assert result["ok"] is True
        verdict = cs.phase1_verdict(
            {"label": "phase1", "exit_code": 0, "designs": [], "hydra": result})
        assert verdict.outcome == cs.PASS
        assert "reorder" in verdict.reason, verdict.reason

    def test_a_missing_or_extra_hotspot_is_still_a_failure(self):
        """Set equality, not "anything goes": a dropped or invented hotspot is
        exactly the silent-substitution the canary exists to catch."""
        dropped = cs.hydra_assertion(
            self._cfg(self.KEY, hotspots=("A45",)), self.KEY, ["A45", "A67"])
        assert dropped["hotspots_match"] is False
        assert dropped["hotspots_missing"] == ["A67"]
        assert dropped["ok"] is False
        extra = cs.hydra_assertion(
            self._cfg(self.KEY, hotspots=("A45", "A67", "A99")), self.KEY, ["A45", "A67"])
        assert extra["hotspots_match"] is False
        assert extra["hotspots_unexpected"] == ["A99"]
        assert extra["ok"] is False
        assert cs.phase1_verdict(
            {"label": "phase1", "exit_code": 0, "designs": [],
             "hydra": dropped}).outcome == cs.FAIL

    def test_a_wrong_contig_is_not_ok(self):
        cfg = self._cfg(self.KEY, contig="A1-100")
        result = cs.hydra_assertion(cfg, self.KEY, ["A45"], "A1-150")
        assert result["contig_matches"] is False
        assert result["ok"] is False

    def test_the_null_shard_registers_no_hotspots_and_still_matches(self):
        cfg = self._cfg(self.KEY, hotspots=())
        cfg["target_dict_cfg"][self.KEY]["hotspot_residues"] = None
        assert cs.hydra_assertion(cfg, self.KEY, [])["ok"] is True

    def test_a_missing_record_is_not_ok(self):
        cfg = self._cfg(self.KEY)
        cfg["target_dict_cfg"].pop(self.KEY)
        result = cs.hydra_assertion(cfg, self.KEY, ["A45"])
        assert result["record_present"] is False
        assert result["ok"] is False

    def test_phase1_verdict_reports_a_wrong_target_as_a_failure(self):
        shard = {"label": "phase1", "exit_code": 0, "designs": [],
                 "hydra": cs.hydra_assertion(self._cfg("02_PDL1"), self.KEY, ["A45"])}
        assert cs.phase1_verdict(shard).outcome == cs.FAIL

    def test_phase1_verdict_passes_on_wiring_even_with_no_complexes(self):
        """Phase 1 asserts WIRING. Finding no complexes is the OBSERVATION it
        was sent to make, not a failure."""
        shard = {"label": "phase1", "exit_code": 0, "designs": [], "n_complexes": 0,
                 "hydra": cs.hydra_assertion(self._cfg(self.KEY), self.KEY, ["A45"])}
        assert cs.phase1_verdict(shard).outcome == cs.PASS

    def test_phase1_verdict_is_inconclusive_with_no_resolved_config(self):
        shard = {"label": "phase1", "exit_code": 0, "designs": [], "hydra": None}
        assert cs.phase1_verdict(shard).outcome == cs.INCONCLUSIVE


# ---------------------------------------------------------------------------
# 8. Negative-control patch selection
# ---------------------------------------------------------------------------


class TestPickFarPatch:
    POSITIVE = ["A1", "A2"]

    @staticmethod
    def _nearest_hotspot(pdb_text, key, positive):
        pos = cs.ca_positions(cs.heavy_atoms(pdb_text))
        return min(cs.dist(pos[key], pos[h]) for h in cs.parse_spec(positive)[0])

    def test_every_selected_residue_clears_the_separation_floor(self):
        """The original picked a far SEED and then took the 4 residues nearest
        to it out of ALL residues, so a member could sit well inside the floor —
        a "negative" control partly on the positive site.

        The floor is measured to the NEAREST POSITIVE HOTSPOT. That is the only
        reference point comparable with a 5 A contact cutoff: the centroid of a
        patch that spans 10-15 A sits up to half a patch-width away from the
        hotspot a binder would actually touch.
        """
        tokens, info = cs.pick_far_patch(LOBED_PDB, self.POSITIVE)
        residues, bad = cs.parse_spec(tokens)
        pos = cs.ca_positions(cs.heavy_atoms(LOBED_PDB))
        assert bad == []
        assert len(residues) == len(tokens)
        for key in residues:
            assert key in pos, f"{key} is not a polymer residue"
            assert (self._nearest_hotspot(LOBED_PDB, key, self.POSITIVE)
                    >= cs.NEGATIVE_MIN_SEPARATION_A)
        assert info["min_separation_achieved_a"] >= cs.NEGATIVE_MIN_SEPARATION_A
        assert info["separation_measured_from"] == "nearest positive hotspot"

    def test_separation_is_measured_from_the_nearest_hotspot_not_the_centroid(self):
        """THE finding. A25 sits 25.5 A from the positive patch's CENTROID —
        it clears a floor measured there — but only 18.5 A from A2, the nearest
        positive hotspot. A 60-120 residue binder has a maximum dimension well
        over 40 A, so a binder docked on A25 brushes the positive site, the
        cross-recall climbs, and phase 2 returns a $12 FAIL: a CONDEMNATION of
        a feature that works.
        """
        pos = cs.ca_positions(cs.heavy_atoms(SPAN_PDB))
        c_pos = cs.ca_centroid(cs.heavy_atoms(SPAN_PDB), [("A", 1), ("A", 2)])
        # The fixture really does straddle the two reference points, or the
        # test would be asserting nothing.
        assert cs.dist(pos[("A", 25)], c_pos) >= cs.NEGATIVE_MIN_SEPARATION_A
        assert self._nearest_hotspot(SPAN_PDB, ("A", 25), ["A1", "A2"]) == (
            pytest.approx(18.5))

        tokens, info = cs.pick_far_patch(SPAN_PDB, ["A1", "A2"], patch_size=2)
        assert "A25" not in tokens, (
            "a residue 18.5 A from a positive hotspot was selected into the "
            "negative control because it was 25.5 A from the patch CENTROID")
        assert sorted(tokens) == ["A52", "A56"]
        assert info["min_separation_achieved_a"] >= cs.NEGATIVE_MIN_SEPARATION_A

    def test_patch_members_are_never_drawn_from_outside_the_far_pool(self):
        """The bleed-back bug, on a shape that actually exposes it: the far
        SEED is isolated, so the residues nearest to it are the positive
        hotspots themselves (40.05 A) rather than the other far residue (80 A).
        Picking the patch out of the whole structure therefore puts the
        positive site INSIDE the negative control — and a negative control that
        overlaps the positive one proves nothing.
        """
        tokens, info = cs.pick_far_patch(BLEEDBACK_PDB, ["A1", "A2"], patch_size=2)
        assert "A1" not in tokens and "A2" not in tokens, (
            "a POSITIVE hotspot was selected into the negative control")
        assert sorted(tokens) == ["A40", "A41"]
        for key in cs.parse_spec(tokens)[0]:
            assert (self._nearest_hotspot(BLEEDBACK_PDB, key, ["A1", "A2"])
                    >= cs.NEGATIVE_MIN_SEPARATION_A)
        assert info["min_separation_achieved_a"] >= cs.NEGATIVE_MIN_SEPARATION_A

    def test_a_residue_just_inside_the_floor_is_excluded(self):
        """A24/A26/A28 sit 20/22/24 A from A2, the nearest positive hotspot.
        A28 is the one that matters: 26 A from the CENTROID, so the old rule
        admitted it, and 24 A from the hotspot, so the real rule does not."""
        tokens, _info = cs.pick_far_patch(BLEEDBACK_PDB, ["A1", "A2"], patch_size=2)
        assert "A26" not in tokens
        assert "A24" not in tokens
        assert "A28" not in tokens

    def test_waters_and_ions_are_never_selected(self):
        """The water at x=100 and the calcium at x=101 are the two furthest
        things in the file, so the old all-heavy-atoms walk picked them — and
        missing_hotspots then rejects those tokens, aborting the shard."""
        tokens, _info = cs.pick_far_patch(LOBED_PDB, self.POSITIVE)
        assert "A999" not in tokens, "a water was selected as a hotspot"
        assert "A998" not in tokens, "a calcium ion was selected as a hotspot"

    def test_the_patch_is_registrable_by_run_pipeline(self, tmp_path):
        """End-to-end: run_pipeline must accept every token, or the negative
        shard refuses itself and phase 2 loses a third of its evidence."""
        path = tmp_path / "target.pdb"
        path.write_text(LOBED_PDB)
        tokens, _info = cs.pick_far_patch(LOBED_PDB, self.POSITIVE)
        residues, _ = rp.pdb_ca_residues(path)
        contig = rp.format_contig(rp.derive_segments(residues, ["A"]))
        selected = rp.select_residues(residues, rp.parse_target_input(contig))
        assert rp.missing_hotspots(selected, tokens) == []

    def test_the_patch_is_contiguous(self):
        tokens, info = cs.pick_far_patch(LOBED_PDB, self.POSITIVE)
        assert len(tokens) == 4
        assert info["patch_span_a"] <= 15.0, "the negative patch is scattered"

    def test_selection_is_deterministic(self):
        assert (cs.pick_far_patch(LOBED_PDB, self.POSITIVE)[0]
                == cs.pick_far_patch(LOBED_PDB, self.POSITIVE)[0])

    def test_the_seed_is_the_most_exposed_far_residue(self):
        """Lobe 2's corners see 10 CA neighbours, its edges 13, its faces 18 and
        its core 26. The seed must be a corner, and the core must never be in
        the patch — a binder cannot dock into a buried site for reasons that
        have nothing to do with hotspots."""
        tokens, info = cs.pick_far_patch(LOBED_PDB, self.POSITIVE)
        assert info["seed_neighbours"] == 10
        assert info["seed_neighbours"] <= cs.MAX_SURFACE_NEIGHBOURS
        assert max(info["neighbour_counts"].values()) <= 13
        assert info["patch_more_buried_than_median"] is False
        assert "A63" not in tokens, "the buried core residue was selected"

    def test_the_seed_is_chosen_by_exposure_not_by_distance(self):
        """The buried ball at x=80 is FURTHER from the positive patch than the
        exposed protrusion at x=40-52. "Furthest away" is not "most designable"
        — the original sorted purely by distance."""
        tokens, info = cs.pick_far_patch(PROTRUSION_PDB, ["A1", "A2"])
        assert info["seed_neighbours"] == 2
        assert sorted(tokens) == ["A10", "A11", "A12", "A13"]

    def test_a_target_whose_only_far_site_is_buried_is_refused(self):
        """Handing back a negative control nothing can bind is worse than
        refusing: the control would "pass" without testing the hypothesis."""
        with pytest.raises(ValueError, match="buried"):
            cs.pick_far_patch(BURIED_FAR_PDB, ["A1", "A2"])

    def test_the_burial_ceiling_is_what_refuses_it(self):
        """Same structure, ceiling lifted — proves the refusal comes from the
        burial guard and not from some unrelated precondition."""
        tokens, info = cs.pick_far_patch(BURIED_FAR_PDB, ["A1", "A2"],
                                         max_seed_neighbours=99)
        assert len(tokens) == 4
        assert info["seed_neighbours"] > cs.MAX_SURFACE_NEIGHBOURS

    def test_a_positive_patch_absent_from_the_structure_raises(self):
        with pytest.raises(ValueError, match="not in the target PDB"):
            cs.pick_far_patch(LOBED_PDB, ["Z1"])

    def test_a_partially_resolvable_positive_spec_raises_too(self, tmp_path):
        """THE $12 finding. ``['A1','A2','A3','A99999']`` on a 60-residue
        target used to return a patch and report "3/4" — because the only
        refusal was "the centroid could not be built at all", and three of four
        tokens build a centroid fine. Three A100 shards then spawned and each
        refused ITSELF in-container, after the money.

        ``missing_hotspots`` — pure, local, already imported here — knew the
        answer for free. The two must agree, so they are asserted together.
        """
        spec = ["A1", "A2", "A3", "A99999"]
        path = tmp_path / "sixty.pdb"
        path.write_text(SIXTY_RES_PDB)
        residues, _ = rp.pdb_ca_residues(path)
        contig = rp.format_contig(rp.derive_segments(residues, ["A"]))
        selected = rp.select_residues(residues, rp.parse_target_input(contig))
        assert len(selected) == 60
        assert rp.missing_hotspots(selected, spec) == ["A99999"]

        with pytest.raises(ValueError, match="A99999"):
            cs.pick_far_patch(SIXTY_RES_PDB, spec)
        # ...and the fully-resolvable version of the same request still works,
        # so the refusal is about the bad token and nothing else.
        tokens, _info = cs.pick_far_patch(SIXTY_RES_PDB, spec[:3])
        assert rp.missing_hotspots(selected, tokens) == []

    def test_an_unreadable_positive_token_raises(self):
        with pytest.raises(ValueError, match="unreadable"):
            cs.pick_far_patch(LOBED_PDB, ["nonsense"])

    def test_an_empty_positive_spec_raises(self):
        """There is nothing for a negative control to be far FROM."""
        with pytest.raises(ValueError, match="nothing"):
            cs.pick_far_patch(LOBED_PDB, [])

    def test_a_target_with_no_far_site_raises_instead_of_inventing_one(self):
        with pytest.raises(ValueError, match="larger target"):
            cs.pick_far_patch(COMPLEX_PDB, ["A1"], min_separation=500.0)

    def test_a_site_more_buried_than_the_structure_median_is_refused(self):
        """The scale-free half of the burial guard.

        The absolute ceiling of 18 CA neighbours is calibrated for a packed
        globular protein; on a small or extended structure everything
        undercounts, so a genuinely buried site clears it. That bias points at
        a $12 FALSE CONDEMNATION, not a false pass: a binder cannot dock into a
        buried patch, the negative shard's own centroid comes back large, and
        ``negative_verdict`` returns FAIL on a feature that works. Comparing
        against the structure's OWN median residue does not depend on scale.
        """
        # An EXTENDED structure — 33 residues on an 11 A grid, so every one of
        # them sees zero CA neighbours within 10 A — plus one compact far
        # pocket at 6 A spacing whose most exposed corner sees 6. Six is
        # nowhere near the absolute ceiling of 18, so that guard waves the
        # pocket through; relative to a structure whose median residue sees
        # nothing, the pocket is solid.
        offsets = sorted(
            ((i, j, k) for i in (-2, -1, 0, 1, 2) for j in (-2, -1, 0, 1, 2)
             for k in (-2, -1, 0, 1, 2) if i * i + j * j + k * k <= 4),
            key=lambda o: (o[0] ** 2 + o[1] ** 2 + o[2] ** 2, o))
        lines = [_atom(n + 1, "CA", "ALA", "A", n + 1,
                       i * 11.0, j * 11.0, k * 11.0)
                 for n, (i, j, k) in enumerate(offsets)]
        near = len(lines)
        for i in range(27):
            lines.append(_atom(near + 1 + i, "CA", "ALA", "A", 100 + i,
                               60.0 + (i % 3) * 6.0,
                               ((i // 3) % 3) * 6.0,
                               ((i // 9) % 3) * 6.0))
        text = "\n".join(lines) + "\n"
        positive = ["A1", "A2"]   # A1 is the grid centre, so nothing else is far
        counts = cs.neighbour_counts(cs.ca_positions(cs.heavy_atoms(text)))
        far_counts = [c for k, c in counts.items() if k[1] >= 100]
        assert near == 33 and len(far_counts) == 27
        assert min(far_counts) <= cs.MAX_SURFACE_NEIGHBOURS, (
            "the fixture must CLEAR the absolute ceiling, or it proves nothing "
            "about the relative one")
        assert min(far_counts) > cs.median(sorted(counts.values()))
        with pytest.raises(ValueError, match="median residue") as caught:
            cs.pick_far_patch(text, positive)
        # ...and it is the RELATIVE guard, not the absolute one: the absolute
        # refusal renders "(surface ceiling 18)", and this seed clears 18.
        assert f"(surface ceiling {cs.MAX_SURFACE_NEIGHBOURS})" not in str(
            caught.value)
        assert f"{min(far_counts)} CA neighbours" in str(caught.value)

    def test_the_burial_proxy_reports_when_it_is_too_small_to_be_trusted(self):
        """A CA-neighbour count cannot mean anything on a 6-residue toy, and
        saying so is the difference between a number and a number the operator
        knows how to weigh — before spending $12 on a control it chose."""
        _tokens, small = cs.pick_far_patch(BLEEDBACK_PDB, ["A1", "A2"], patch_size=2)
        assert small["structure_n_residues"] < cs.BURIAL_PROXY_MIN_RESIDUES
        assert small["burial_proxy_reliable"] is False
        assert "undercounts" in small["burial_proxy_caveat"]
        # A 60-residue target clears the floor and carries no caveat.
        _tokens, big = cs.pick_far_patch(SIXTY_RES_PDB, ["A1", "A2"])
        assert big["structure_n_residues"] >= cs.BURIAL_PROXY_MIN_RESIDUES
        assert big["burial_proxy_reliable"] is True
        assert big["burial_proxy_caveat"] is None


# ---------------------------------------------------------------------------
# 9. Phase 0 aggregation and registry hygiene
# ---------------------------------------------------------------------------


class TestPhase0Aggregation:
    def test_both_controls_passing_is_a_pass(self):
        assert cs.phase0_pass({"typo_control": {"pass": True},
                               "warm_container_control": {"pass": True}}) is True

    def test_an_empty_result_is_not_a_vacuous_pass(self):
        """`all(v.get("pass") for v in results.values() if isinstance(v, dict))`
        is True over an empty mapping — a green phase 0 that never ran."""
        assert cs.phase0_pass({}) is False

    def test_a_missing_control_fails(self):
        assert cs.phase0_pass({"typo_control": {"pass": True}}) is False

    def test_a_renamed_control_fails(self):
        assert cs.phase0_pass({"typo_control": {"pass": True},
                               "warm_container_kontrol": {"pass": True}}) is False

    def test_a_non_dict_control_fails(self):
        assert cs.phase0_pass({"typo_control": True,
                               "warm_container_control": {"pass": True}}) is False

    def test_pass_must_be_exactly_true(self):
        assert cs.phase0_pass({"typo_control": {"pass": "yes"},
                               "warm_container_control": {"pass": True}}) is False


class TestRegistryHygiene:
    NESTED = {
        "target_dict_cfg": {
            "02_PDL1": {"source": "bindcraft_targets"},
            "hub_canary000000000000": {"source": "tools_hub_upload"},
            "hub_deadbeefdeadbeef": {"source": "tools_hub_upload"},
        }
    }

    def test_records_are_unwrapped_from_target_dict_cfg(self):
        assert "02_PDL1" in cs.registry_records(self.NESTED)

    def test_a_flat_registry_still_reads(self):
        assert "hub_x" in cs.registry_records({"hub_x": {"source": "tools_hub_upload"}})

    def test_pruning_removes_only_canary_records(self):
        data = json.loads(json.dumps(self.NESTED))
        removed = cs.prune_canary_records(data)
        assert removed == ["hub_canary000000000000"]
        remaining = cs.registry_records(data)
        assert "02_PDL1" in remaining, "a curated benchmark target was deleted"
        assert "hub_deadbeefdeadbeef" in remaining, "a prod record was deleted"

    def test_pruning_mutates_the_structure_that_gets_dumped_back(self):
        """The records mapping is a REFERENCE into the outer dict; dumping the
        inner mapping instead would strip the wrapper Hydra composes."""
        data = json.loads(json.dumps(self.NESTED))
        cs.prune_canary_records(data)
        assert "target_dict_cfg" in data
        assert "hub_canary000000000000" not in data["target_dict_cfg"]

    def test_every_key_the_canary_writes_is_pruned_from_a_real_registry(self):
        """Re-asserting the prefix only restates ``canary_task_key``'s own
        format string. What has to hold is that the keys the canary ACTUALLY
        writes disappear from a registry shaped like the one Hydra composes,
        while everything else survives — the prune runs in a warm container
        against the image's real targets_dict.yaml, and over-deleting there
        destroys a curated benchmark target.
        """
        labels = ("phase0", "phase1", "positive", "negative", "null")
        keys = [cs.canary_task_key(label, 1234) for label in labels]
        assert len(set(keys)) == len(labels), "two labels collided on one key"
        data = {"target_dict_cfg": dict(
            [("02_PDL1", {"source": "bindcraft_targets"}),
             ("hub_deadbeefdeadbeef", {"source": "tools_hub_upload"})]
            + [(k, {"source": "tools_hub_upload"}) for k in keys]
        )}
        removed = cs.prune_canary_records(data)
        assert sorted(removed) == sorted(keys)
        assert sorted(cs.registry_records(data)) == [
            "02_PDL1", "hub_deadbeefdeadbeef"]

    def test_a_production_key_can_never_match_the_canary_prefix(self):
        """The prune is prefix-based, so it is only safe if run_pipeline's own
        keys cannot start with it. They are ``hub_`` + HEX, and "canary"
        contains n/r/y — not hex digits — so the two namespaces are disjoint by
        construction, not by luck."""
        assert cs.CANARY_KEY_PREFIX.startswith("hub_")
        suffix = cs.CANARY_KEY_PREFIX[len("hub_"):]
        assert not set(suffix) <= set("0123456789abcdef")
        for job in ("job-1", "job-2", "abc", ""):
            key = rp.custom_target_key(job, "s" * 64, {"target_input": "A1-150"})
            assert not key.startswith(cs.CANARY_KEY_PREFIX)


# ---------------------------------------------------------------------------
# 10. WHICH CHAIN IS THE TARGET — the fabricated-PASS defect
# ---------------------------------------------------------------------------


class TestTargetChainIdentity:
    """``run_shard`` knows the INPUT PDB's chain ids and used to call every
    other chain in a design output "binder" on that basis alone.

    Nothing guaranteed the output preserved the labelling. The harness's own
    module docstring says the output chain convention is UNKNOWN — discovering
    it is why phase 1 exists — so assuming it in phase 2 was assuming away the
    thing being tested. When the assumption is wrong the roles INVERT:
    ``is_complex`` stays True, contacts are computed over BINDER residues, and
    the hotspot tokens resolve against BINDER residue numbers. The result is
    not a wrong-ish number, it is a perfect one: ``hotspot_recall = 1.0`` with
    a small centroid offset, i.e. a $12 PASS manufactured from a binder's
    contacts with the target it was designed against.
    """

    SPEC = ["A1", "A2"]

    def test_the_relabelled_output_really_does_fabricate_a_perfect_score(self):
        """The trap itself, measured. Scoring by chain LABEL alone — which is
        all the old code did — turns a binder-on-chain-A output into a flawless
        result. This is what the gate below has to stop."""
        naive = cs.score_design(RELABELLED_DESIGN_PDB, {"A"}, self.SPEC)
        assert naive["hotspot_recall"] == 1.0
        assert naive["requested_found_in_structure"] == 2
        assert naive["centroid_distance"] == pytest.approx(4.0)
        assert naive["centroid_distance"] <= cs.DEFAULT_THRESHOLDS.max_centroid_a
        # ...and a shard of eight such designs would have PASSED outright.
        assert cs.positive_verdict(_shard(
            n=8, recall=naive["hotspot_recall"],
            centroid=naive["centroid_distance"])).outcome == cs.PASS

    def test_a_correctly_labelled_design_is_verified_and_scored(self):
        entry = cs.score_design_file(
            CORRECT_DESIGN_PDB, {"A"}, self.SPEC, [], TARGET_REFERENCE)
        assert entry["is_complex"] is True
        assert entry["target_verified"] is True
        assert entry["target_identity"]["sequence_identity"] == 1.0
        assert entry["target_identity"]["key_coverage"] == 1.0
        assert entry["hotspot_recall"] == 1.0
        assert entry["centroid_distance"] == pytest.approx(4.0)

    def test_a_file_carrying_only_the_target_is_not_a_complex(self):
        """``is_complex`` needs BOTH halves — a wanted chain AND some other
        chain — and the second half had no test, so dropping it stayed green.

        Without it a design output carrying only the target counts as a
        complex, and then NEITHER diagnostic fires: ``n_complexes`` is non-zero
        so phase 1 skips "no per-design file contains both target and binder
        chains", and the file genuinely IS the target so it verifies and the
        relabelling note is skipped too. Every metric comes back None and the
        operator is shown an unexplained INCONCLUSIVE — the same
        diagnostic-suppression shape the UNK gate had.
        """
        target_only = "\n".join(_trace("A", _TARGET_SEQ)) + "\n"
        entry = cs.score_design_file(target_only, {"A"}, self.SPEC, self.SPEC,
                                     TARGET_REFERENCE)
        assert entry["chains"] == ["A"]
        assert entry["is_complex"] is False
        assert "target_verified" not in entry, (
            "a non-complex is not scored, so it never reaches the gate")
        assert "hotspot_recall" not in entry
        # ...and the binder-only mirror image is not a complex either.
        binder_only = "\n".join(_trace("B", ["GLY"] * 8, y=4.0)) + "\n"
        assert cs.score_design_file(binder_only, {"A"}, self.SPEC, [],
                                    TARGET_REFERENCE)["is_complex"] is False

    def test_a_relabelled_design_is_unscorable_not_scored(self):
        """THE fix. Identical geometry to the correct case — same contacts,
        same distances — so nothing about the coordinates can tell them apart.
        Only the residue NAMES can."""
        entry = cs.score_design_file(
            RELABELLED_DESIGN_PDB, {"A"}, self.SPEC, self.SPEC, TARGET_REFERENCE)
        assert entry["is_complex"] is True, (
            "the trap requires this to still look like a complex")
        assert entry["target_verified"] is False
        assert "hotspot_recall" not in entry, (
            "a design whose target could not be verified must carry NO metric")
        assert "centroid_distance" not in entry
        assert "cross_hotspot_recall" not in entry
        assert entry["target_identity"]["sequence_identity"] == 0.0
        assert entry["target_identity"]["key_coverage"] == 1.0, (
            "a pure (chain, resseq) subset test would have PASSED this — the "
            "binder's A1..A30 are a perfect subset of the target's A1..A30")
        assert "different protein" in entry["unscorable_reason"]

    def test_the_chain_map_names_the_chain_that_is_actually_the_target(self):
        """Phase 1's entire job is to discover the output convention, so the
        refusal has to say what the convention IS, not just that it is wrong.
        Otherwise the operator re-runs $4 to rediscover what the shard already
        measured."""
        entry = cs.score_design_file(
            RELABELLED_DESIGN_PDB, {"A"}, self.SPEC, [], TARGET_REFERENCE)
        hints = entry["target_identity"]["chain_hints"]
        assert hints["A"]["best_match"]["sequence_identity"] == 0.0
        assert hints["B"]["best_match"]["sequence_identity"] == 1.0
        assert hints["B"]["best_match"]["reference_chain"] == "A"

    def test_a_target_chain_padded_with_foreign_residues_is_unscorable(self):
        """The key-coverage half, on the case the matched-count floor does NOT
        catch: chain A really does carry the target at A1..A30 — same numbers,
        same names, 30 matched residues, 100% identity — and then 20 more
        residues at A31..A50 that are in no reference at all.

        Contacts would be computed over all fifty, so the contact set, the
        centroid and the recall denominator would all absorb geometry from
        residues we cannot account for. Sixty percent of a target is not a
        target.
        """
        design = "\n".join(
            _trace("A", _TARGET_SEQ)
            + [_atom(200 + i, "CA", "GLY", "A", 31 + i, 120.0 + i * 4.0, 0.0, 0.0)
               for i in range(20)]
            + _trace("B", ["GLY"] * 4, y=4.0, serial0=100)) + "\n"
        entry = cs.score_design_file(
            design, {"A"}, self.SPEC, [], TARGET_REFERENCE)
        identity = entry["target_identity"]
        assert identity["n_matched_keys"] == 30, "the floor check must NOT fire"
        assert identity["sequence_identity"] == 1.0, "identity must NOT fire"
        assert identity["key_coverage"] == pytest.approx(0.6)
        assert entry["target_verified"] is False
        assert "hotspot_recall" not in entry
        assert "not the residues we uploaded" in entry["unscorable_reason"]

    def test_a_renumbered_target_is_restored_and_then_scorable(self):
        """CONTRACT CHANGED, DELIBERATELY — this test used to assert the
        opposite, and the old assertion was right for the code that existed.

        Hotspot tokens are matched by NUMBER, so a target renumbered from 1000
        makes ``A1`` name a different residue and every score computed against
        it meaningless. That was measured on real hardware afterwards: upstream
        renumbers EVERY chain to 1..N, so 8 of 8 designs of a completed Fc shard
        were unscorable and phase 2 would have returned INCONCLUSIVE for ~$12.

        The gate is unchanged. What changed is that ``restore_input_numbering``
        now runs FIRST and puts the keys back, and only when a
        position-for-position sequence match proves the correspondence — here
        the chain label and the sequence are both right, which is exactly the
        case it is entitled to repair. ``target_renumbering.applied`` is the
        record that it did.
        """
        entry = cs.score_design_file(
            RENUMBERED_DESIGN_PDB, {"A"}, self.SPEC, [], TARGET_REFERENCE)
        assert entry["target_renumbering"]["applied"] is True
        assert entry["target_renumbering"]["chains"]["A"]["identity"] == 1.0
        assert entry["target_verified"] is True
        assert entry["hotspot_recall"] is not None
        assert "unscorable_reason" not in entry

    def test_a_renumbering_the_sequence_cannot_prove_is_still_unscorable(self):
        """The other half of the contract, and the one that costs money if it
        ever stops holding.

        Same renumbering from 1000, but the chain carries GLY — absent from
        _TARGET_SEQ by construction — so no position matches. The restore
        declines, the gate sees the untouched keys, and the design is refused
        exactly as it was before the restore existed. A repair that cannot be
        proved must change nothing."""
        design = "\n".join(
            _trace("A", ["GLY"] * len(_TARGET_SEQ), first_res=1000)
            + _trace("B", ["GLY"] * 4, y=4.0, serial0=100)) + "\n"
        entry = cs.score_design_file(design, {"A"}, self.SPEC, [],
                                     TARGET_REFERENCE)
        assert entry["target_renumbering"]["applied"] is False
        assert entry["target_verified"] is False
        assert "hotspot_recall" not in entry
        assert entry["target_identity"]["n_matched_keys"] == 0

    def test_an_unverified_design_can_never_reach_a_verdict(self):
        """Belt and braces, and both are load-bearing: even if a metric were
        somehow present on an unverified design — a future caller, a bad merge,
        a hand-built dict — it must not be scorable. A fabricated 1.0 reaching
        a verdict is the whole defect."""
        shard = _shard(n=8, recall=1.0, centroid=0.0, cross=0.0,
                       target_verified=False)
        for design in shard["designs"]:
            assert design["hotspot_recall"] == 1.0, "the metric is present"
        assert cs.scorable_designs(shard, "hotspot_recall") == []
        assert cs.unverified_designs(shard) == shard["designs"]
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
        assert verdict.outcome != cs.PASS
        assert verdict.outcome != cs.FAIL

    def test_the_unmeasurable_reason_distinguishes_relabelling_from_no_complexes(self):
        """"No complexes at all" sends the operator hunting the refold
        artifact; "complexes with the wrong chain labels" sends them to the
        chain map. One message for both costs a phase-1 re-run to rediscover
        something the shard already reported."""
        relabelled = cs.positive_verdict(
            _shard(n=8, recall=1.0, centroid=0.0, target_verified=False))
        binder_only = cs.positive_verdict(_shard(n=8, is_complex=False))
        assert relabelled.outcome == binder_only.outcome == cs.INCONCLUSIVE
        assert "chain" in relabelled.reason
        assert relabelled.reason != binder_only.reason
        assert "refold" in binder_only.reason

    def test_all_three_phase2_verdicts_go_inconclusive_on_a_relabelled_run(self):
        """Never PASS, never FAIL — the money bought a measurement problem,
        which is neither evidence for nor against the feature."""
        blind = dict(_shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0,
                            target_verified=False))
        report = cs.phase2_report(blind, dict(blind, label="negative"),
                                  dict(blind, label="null"))
        assert report["overall"] == cs.INCONCLUSIVE
        assert [v.outcome for v in report["verdicts"]] == [cs.INCONCLUSIVE] * 3
        assert report["exit_code"] == cs.EXIT_CODES[cs.INCONCLUSIVE]

    @staticmethod
    def _shard_from_designs(label, design_texts, spec, cross_spec):
        """The aggregation ``run_shard`` performs, over real design files.

        Kept in lockstep with the shard by construction: same
        ``score_design_file``, same ``scorable_designs`` predicate under the
        medians. If the shard's aggregation and the verdicts' predicate ever
        diverge, a design excluded from ``n_scorable`` could still be inside a
        median — a number under a verdict that the verdict did not count.
        """
        designs = [cs.score_design_file(text, {"A"}, spec, cross_spec,
                                        TARGET_REFERENCE)
                   for text in design_texts]
        shard = {"label": label, "exit_code": 0, "designs": designs,
                 # As the real shard reports it: how many designs were ORDERED.
                 # These fixtures describe a run upstream did not filter, so it
                 # is the number of files; the absolute floor is exercised
                 # against a real ``run_shard`` in TestTheProducedCountIsAlsoASurvivorCount.
                 "n_designs_expected": len(design_texts),
                 "cross_reference_hotspots": list(cross_spec),
                 "n_complexes": sum(1 for d in designs if d.get("is_complex")),
                 "n_target_verified": sum(1 for d in designs
                                          if d.get("target_verified"))}
        for field in ("hotspot_recall", "centroid_distance",
                      "cross_hotspot_recall"):
            shard[f"{field}_median"] = cs.median(
                d.get(field) for d in cs.scorable_designs(shard, field))
        return shard

    def test_end_to_end_a_relabelled_phase2_is_inconclusive_not_a_verdict(self):
        """The whole path, from PDB bytes to exit code, on the exact output
        shape that fabricated evidence.

        Eight relabelled designs per shard. Scored by chain label alone — which
        is all the old code did — the POSITIVE shard reports a median recall of
        1.0 at a 4 A offset and its verdict PASSES, off the binder's contacts
        with the target it was designed against. What the other two shards then
        report depends on what upstream emitted, so the overall could land on
        either PASS or FAIL; both are verdicts on numbers that measured nothing.
        (Measured against the pre-gate code, this particular trio lands on FAIL
        with the positive verdict PASSing — a $12 condemnation built out of the
        same fabricated geometry as a $12 blessing.)

        With the gate there is only one possible answer: nothing was scorable,
        so nothing is concluded.
        """
        eight = [RELABELLED_DESIGN_PDB] * 8
        pos = self._shard_from_designs("positive", eight, self.SPEC, self.SPEC)
        neg = self._shard_from_designs("negative", eight, ["A20"], self.SPEC)
        null = self._shard_from_designs("null", eight, [], self.SPEC)
        assert pos["n_complexes"] == 8, "still complexes, still eight of them"
        assert pos["n_target_verified"] == 0
        assert pos["hotspot_recall_median"] is None, (
            "scored by chain label alone this is 1.0")
        assert pos["centroid_distance_median"] is None
        report = cs.phase2_report(pos, neg, null)
        assert report["overall"] == cs.INCONCLUSIVE, [
            v.reason for v in report["verdicts"]]
        assert [v.outcome for v in report["verdicts"]] == [cs.INCONCLUSIVE] * 3
        assert report["exit_code"] != 0, "INCONCLUSIVE is not a green light"
        assert report["exit_code"] != cs.EXIT_CODES[cs.FAIL], (
            "nor is it a condemnation")

    def test_end_to_end_a_correctly_labelled_phase2_still_reaches_a_verdict(self):
        """The gate must not make everything unscorable. Same path, correct
        chain labelling: eight designs on the requested patch, a negative
        control that touches none of it, and a null run that misses — PASS."""
        on_patch = [CORRECT_DESIGN_PDB] * 8
        off_patch = "\n".join(
            _trace("A", _TARGET_SEQ)
            + [_atom(100 + i, "CA", "GLY", "B", 1 + i,
                     104.0 + i * 4.0, 4.0, 0.0) for i in range(4)]) + "\n"
        pos = self._shard_from_designs("positive", on_patch, self.SPEC, self.SPEC)
        neg = self._shard_from_designs("negative", [off_patch] * 8,
                                       ["A27", "A28"], self.SPEC)
        null = self._shard_from_designs("null", [off_patch] * 8, [], self.SPEC)
        assert pos["n_target_verified"] == 8
        assert pos["hotspot_recall_median"] == 1.0
        assert neg["cross_hotspot_recall_median"] == 0.0
        report = cs.phase2_report(pos, neg, null)
        assert report["overall"] == cs.PASS, [
            v.reason for v in report["verdicts"]]
        assert report["exit_code"] == 0

    def test_verification_survives_modified_residues_and_unknowns(self):
        """A refold that writes MSE back as MET, or an UNK, must not push a
        genuine target below the identity floor — a false unscorable is a $12
        INCONCLUSIVE on a run that was fine."""
        seq = list(_TARGET_SEQ)
        reference = dict(TARGET_REFERENCE)
        reference[("A", 1)] = "MSE"
        seq[0] = "MET"
        seq[5] = "UNK"
        design = "\n".join(
            _trace("A", seq) + _trace("B", ["GLY"] * 4, y=4.0, serial0=100)) + "\n"
        entry = cs.score_design_file(design, {"A"}, self.SPEC, [], reference)
        assert entry["target_verified"] is True
        assert cs.same_residue("MSE", "MET") is True
        assert cs.same_residue("UNK", "TRP") is True
        assert cs.same_residue("ALA", "TRP") is False

    def test_a_handful_of_matched_residues_cannot_certify_a_target(self):
        """An absolute floor under the fractions: 2 of 2 residues agreeing is
        100% identity and 100% coverage and means nothing."""
        tiny = "\n".join(
            _trace("A", _TARGET_SEQ[:2])
            + _trace("B", ["GLY"] * 4, y=4.0, serial0=100)) + "\n"
        entry = cs.score_design_file(tiny, {"A"}, self.SPEC, [], TARGET_REFERENCE)
        assert entry["target_verified"] is False
        assert entry["target_identity"]["sequence_identity"] == 1.0
        assert entry["target_identity"]["n_matched_keys"] == 2
        assert entry["target_identity"]["min_matched_residues"] == (
            cs.TARGET_MIN_MATCHED_RESIDUES)

    def test_a_binder_only_file_never_reaches_the_identity_check(self):
        """Not a complex, so there is nothing to verify and no reason to
        pretend otherwise."""
        binder_only = "\n".join(_trace("B", ["GLY"] * 8, y=4.0)) + "\n"
        entry = cs.score_design_file(
            binder_only, {"A"}, self.SPEC, [], TARGET_REFERENCE)
        assert entry["is_complex"] is False
        assert "target_verified" not in entry
        assert "hotspot_recall" not in entry


# ---------------------------------------------------------------------------
# 11. The negative control's cross-recall ceiling, as a COUNT
# ---------------------------------------------------------------------------


class TestCrossRecallCeiling:
    def test_the_ceiling_is_a_count_of_hotspots_not_a_fraction(self):
        """0.2 sat between achievable medians. With 4 hotspots and 8 designs
        the reachable medians are 0, 0.125, 0.25, ... so "0.2" really meant
        "FAIL once 5 of 8 designs touch any one hotspot" — a boundary nobody
        chose and nobody could read off the number.
        """
        assert isinstance(cs.DEFAULT_THRESHOLDS.max_cross_hotspots, int)
        assert not hasattr(cs.DEFAULT_THRESHOLDS, "max_cross_recall")
        four = ("A1", "A2", "A3", "A4")
        # One brushed hotspot out of four: expected noise for a 60-120 residue
        # binder docked 25 A away, and a PASS.
        assert cs.negative_verdict(_shard(
            "negative", cross=0.25, centroid=1.0,
            cross_reference_hotspots=four)).outcome == cs.PASS
        # ...and the half-integer median that 0.2 used to fail.
        assert cs.negative_verdict(_shard(
            "negative", cross=0.125, centroid=1.0,
            cross_reference_hotspots=four)).outcome == cs.PASS
        # Two of four is a real overlap.
        assert cs.negative_verdict(_shard(
            "negative", cross=0.5, centroid=1.0,
            cross_reference_hotspots=four)).outcome == cs.FAIL

    def test_the_same_recall_means_different_things_at_different_patch_sizes(self):
        """A fraction hides the denominator; a count cannot. 0.25 is one
        hotspot of four and two of eight, and only one of those is noise."""
        common = dict(centroid=1.0, cross=0.25)
        four = cs.negative_verdict(_shard(
            "negative", cross_reference_hotspots=("A1", "A2", "A3", "A4"), **common))
        eight = cs.negative_verdict(_shard(
            "negative",
            cross_reference_hotspots=tuple(f"A{i}" for i in range(1, 9)), **common))
        assert four.outcome == cs.PASS
        assert eight.outcome == cs.FAIL
        assert four.metrics["cross_hotspots_touched_median"] == 1.0
        assert eight.metrics["cross_hotspots_touched_median"] == 2.0

    def test_a_shard_that_does_not_report_its_reference_patch_is_inconclusive(self):
        """No denominator, no count, no verdict — guessing one would put an
        invented number under a $12 decision."""
        shard = _shard("negative", cross=0.0, centroid=1.0)
        shard.pop("cross_reference_hotspots")
        verdict = cs.negative_verdict(shard)
        assert verdict.outcome == cs.INCONCLUSIVE
        assert verdict.outcome != cs.PASS


# ---------------------------------------------------------------------------
# 12. Structural pins on _hotspot_canary.py itself
# ---------------------------------------------------------------------------


# Everything that reads bytes off the local filesystem. An ALLOWLIST of names
# rather than a denylist of AST shapes: the previous test matched only
# ``ast.Attribute`` calls named read_text/read_bytes/open, so a builtin
# ``open(path)`` — an ``ast.Name`` — and ``.read()``, which was not on the list
# at all, both walked straight through it. ``_leak =
# open(d["container_path"]).read()`` inside main()'s phase-1 loop kept the
# suite green, which is to say the test pinning the fatal bug did not pin it.
_FILE_READ_CALLS = frozenset({
    "open", "read", "read_text", "read_bytes", "readline", "readlines",
    "load", "safe_load", "iglob", "glob", "listdir", "walk", "scandir",
})

# The ONLY names from _canary_scoring that the LOCAL entrypoint may call.
# An allowlist, because the previous denylist ("not score_design") rested on an
# argument rather than an enumeration: score_from_contacts needs atoms, atoms
# need a file read, the file read is blocked, therefore scoring is blocked. The
# middle link broke, and a complete local re-score —
# heavy_atoms(open(...).read()) -> ca_positions -> contacts_from_atoms ->
# score_from_contacts — passed every test in this file.
_MAIN_MAY_CALL_FROM_SCORING = frozenset({
    "phase0_pass", "phase1_verdict", "phase2_report", "pick_far_patch",
    # Pre-spend refusals. They raise from the covered module rather than from
    # the Modal one so the offline suite can execute the refusal itself.
    "refuse_unknown_phase", "refuse_empty_hotspot_spec",
    # A RENDERER, not a measurement: it takes the shard dict the container
    # already returned and turns the ``log_diagnostics`` block inside it into
    # console lines. It opens nothing, globs nothing and computes no geometry,
    # so it cannot do the thing this allowlist exists to prevent — producing a
    # number locally that looks like evidence. The collection itself is
    # ``_collect_run_logs``, which runs in the container where the files are;
    # ``test_the_local_entrypoint_reads_exactly_one_file`` still holds main() to
    # a single read_text, and that is what pins the split.
    "format_log_diagnostics",
    # WIDENED BY ONE NAME, on the same argument as the line above. It is a
    # RENDERER over two integers the container already reported
    # (``n_designs_expected`` and ``len(designs)``): it opens nothing, globs
    # nothing, takes no per-design data and computes no geometry, so it cannot
    # do the thing this allowlist exists to prevent — manufacture a number
    # locally that looks like evidence. It earns its place because phase 1 is
    # the $4 gate in front of the $12 run, and "upstream kept 1 of the 8 we
    # ordered" is the fact that decides whether to start it.
    "designs_yield_note",
    # WIDENED BY ONE MORE NAME, on the identical argument, and the criterion is
    # the allowlist's own rather than a fresh one: it is a RENDERER over three
    # integers and a string the container already reported (``exit_code``,
    # ``n_scored_designs``, ``n_reward_rows``, ``label``). It opens nothing,
    # globs nothing, takes no per-design geometry and computes no number that
    # is not already in the payload, so it cannot manufacture local evidence.
    # It earns its place because ``shard_failure`` no longer condemns a shard
    # that exited non-zero and still delivered scored designs, and the danger in
    # that change is the crash going quiet: if it no longer moves the verdict it
    # has to move the console. Note this is the RENDERER only — ``shard_
    # delivery``, which decides the state, stays off the list and is executed
    # directly by this suite.
    "delivery_note",
})

# Scoring primitives ``run_shard`` must never touch directly: going through
# them inline bypasses ``score_design_file``, which is where the chain-identity
# gate lives, and inline code in a modal-importing module is unreachable by
# this suite.
_SHARD_MAY_NOT_TOUCH = frozenset({
    "contacts_from_atoms", "score_from_contacts", "score_design", "ca_positions",
})


class TestHarnessStructure:
    """The FATAL defect — phase 2 re-opening container paths on the LOCAL
    machine — cannot be reproduced without Modal, so it is pinned at the source
    level instead. Reading the file as text needs no ``modal`` import.
    """

    @staticmethod
    def _tree():
        return ast.parse(_CANARY_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _function(tree, name):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} not found in {_CANARY_PATH}")

    @staticmethod
    def _body(func):
        """The function's BODY as a walkable tree — decorators excluded, so
        ``@app.local_entrypoint()`` does not count as a call main makes."""
        return ast.Module(body=list(func.body), type_ignores=[])

    @classmethod
    def _calls(cls, func):
        """Every call node in ``func``'s body."""
        return [n for n in ast.walk(cls._body(func)) if isinstance(n, ast.Call)]

    @classmethod
    def _call_names(cls, func):
        """The callee NAME of every call, whether ``x.foo()`` or ``foo()``.

        Both forms, because a builtin is an ``ast.Name`` and a method is an
        ``ast.Attribute``, and a detector that only understands one of them
        misses the most natural way to write the thing it forbids.
        """
        names = []
        for node in cls._calls(func):
            func_node = node.func
            if isinstance(func_node, ast.Attribute):
                names.append(func_node.attr)
            elif isinstance(func_node, ast.Name):
                names.append(func_node.id)
            else:
                names.append(ast.unparse(func_node))
        return names

    @classmethod
    def _calls_via(cls, func, module_name):
        """Names called as ``<module_name>.<name>(...)``."""
        return {n.func.attr for n in cls._calls(func)
                if isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == module_name}

    @classmethod
    def _attrs_via(cls, func, module_name):
        """EVERY ``<module_name>.<name>`` access, called or not.

        ``_calls_via`` only sees a name in call position, which is an asymmetry
        the sibling test on ``main`` does not have: ``f = cs.contacts_from_atoms``
        followed by ``f(atoms, ...)`` is an ``ast.Name`` call whose callee is
        ``f``, so a forbidden primitive slipped through simply by being bound to
        a local first. Binding it IS reaching for it, so the attribute access is
        what gets counted.
        """
        return {n.attr for n in ast.walk(cls._body(func))
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == module_name}

    @classmethod
    def _getattr_names(cls, func):
        """The literal second argument of every ``getattr(...)`` in the body.

        ``getattr(cs, "score_from_contacts")`` reaches a forbidden primitive
        without ever writing its name in an attribute or a call position, so
        every name-based check above is blind to it.
        """
        return {n.args[1].value for n in cls._calls(func)
                if isinstance(n.func, ast.Name) and n.func.id == "getattr"
                and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
                and isinstance(n.args[1].value, str)}

    @classmethod
    def _reached_names(cls, func):
        """EVERY name the body reaches for, by whatever route.

        ``_calls_via`` sees only ``cs.<name>(...)`` and ``_attrs_via`` only
        ``cs.<name>``; both are keyed on the literal module alias ``cs``, and
        that was an asymmetry the sibling check on ``main`` did not have —
        ``main`` also intersects bare callee names. Three routes therefore
        walked straight past the ``run_shard`` check while it used
        ``_attrs_via`` alone, each verified to survive by mutation:

          * ``_mod = cs`` then ``_mod.contacts_from_atoms(...)`` — the
            attribute's value is ``_mod``, not ``cs``;
          * ``getattr(cs, "score_from_contacts")`` — no attribute node at all;
          * a module-level ``f = cs.ca_positions`` called as a bare ``f(...)``.

        Counting the attribute NAME on any object, plus every bare identifier,
        plus getattr's literal, closes all three. It is deliberately broad:
        ``_SHARD_MAY_NOT_TOUCH`` holds four distinctive names that have no
        innocent reason to appear anywhere in ``run_shard``. Docstrings and
        dict keys are not counted (only ``getattr``'s literal is), so the
        function's own prose naming ``cs.score_design_file`` cannot trip it.
        """
        found = cls._getattr_names(func)
        for node in ast.walk(cls._body(func)):
            if isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.Name):
                found.add(node.id)
        return found

    def test_the_local_entrypoint_reads_exactly_one_file(self):
        """`Path(target_pdb).read_text()` — the operator's own PDB — and
        nothing else. The shipped code called `Path(d["file"]).read_text()`
        twice inside the local entrypoint on paths that only exist INSIDE the
        container, so phase 2 raised FileNotFoundError after the ~$12 was
        already spent and two of its three verdicts never existed.

        Counted as an allowlist over callee NAMES, so a builtin ``open`` and a
        bare ``.read()`` are both caught — neither was, before.
        """
        main = self._function(self._tree(), "main")
        reads = sorted(n for n in self._call_names(main) if n in _FILE_READ_CALLS)
        assert reads == ["read_text"], (
            f"main() performs local filesystem reads {reads}; exactly one is "
            "permitted, the operator's own --target-pdb")

    def test_no_scoring_primitive_may_be_called_from_the_local_entrypoint(self):
        """An ALLOWLIST, so a new scoring call fails by default.

        The per-design PDBs exist only inside the container. Any local attempt
        to score them either raises FileNotFoundError after the money is spent
        or — worse — succeeds against some other file and produces a number
        that looks like evidence.
        """
        main = self._function(self._tree(), "main")
        public = {
            name for name in dir(cs)
            if not name.startswith("_") and callable(getattr(cs, name, None))
        }
        assert "score_from_contacts" in public and "heavy_atoms" in public, (
            "the allowlist is checked against a real inventory of cs callables")
        # Every cs.<callable> the entrypoint so much as MENTIONS, not only the
        # ones in call position: `f = cs.score_from_contacts; f(...)` reads as a
        # call to `f`, and counting only calls let that through. getattr's
        # literal too, which names a primitive without writing an attribute.
        via_cs = (self._attrs_via(main, "cs") | self._getattr_names(main)) & public
        assert via_cs <= _MAIN_MAY_CALL_FROM_SCORING, (
            f"main() reaches for cs.{sorted(via_cs - _MAIN_MAY_CALL_FROM_SCORING)}"
            " — scoring must happen in-container")
        # ...and by bare name too, so re-importing the module under another
        # alias, or `from ... import score_from_contacts`, is caught as well.
        leaked = set(self._call_names(main)) & public - _MAIN_MAY_CALL_FROM_SCORING
        assert not leaked, f"main() calls scoring primitives {sorted(leaked)}"

    def test_the_shard_scores_through_the_covered_module_not_inline(self):
        """``run_shard`` runs inside a container that imports ``modal``, so
        nothing written inline in it is reachable by this suite. The
        chain-identity gate is the most expensive decision in the harness to
        get wrong, so it must live in ``_canary_scoring`` where these tests can
        execute it — and ``run_shard`` must go through it rather than
        re-deriving contacts and scores for itself.
        """
        tree = self._tree()
        shard = self._function(tree, "run_shard")
        via_cs = self._calls_via(shard, "cs")
        assert "score_design_file" in via_cs, (
            "run_shard must delegate the per-design decision to the covered "
            "module")
        # EVERY route to a primitive, not just `cs.<name>(...)`. Aliasing to a
        # local (`f = cs.contacts_from_atoms`), aliasing the MODULE (`_mod = cs`
        # then `_mod.contacts_from_atoms(...)`), a bare-name call through a
        # module-level alias, and `getattr(cs, "score_from_contacts")` were all
        # confirmed by mutation to survive the narrower attribute-only check.
        inline = self._reached_names(shard) & _SHARD_MAY_NOT_TOUCH
        assert not inline, (
            f"run_shard scores inline via {sorted(inline)}; that path is "
            "untestable offline and is where the chain-identity gate was "
            "missing")
        # ...and MODULE-WIDE, because an alias bound at module scope puts the
        # forbidden name nowhere in run_shard's body at all: `_f =
        # cs.contacts_from_atoms` up top, then a bare `_f(...)` in the shard,
        # defeats every body-only scan however broad, since `_f` is arbitrary.
        # Nothing in _hotspot_canary.py has any business naming these four —
        # they exist to be reached through score_design_file, which is the only
        # place the chain-identity gate runs — so the ban is simply total.
        module_wide = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {
            node.args[1].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "getattr" and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        }
        reached = module_wide & _SHARD_MAY_NOT_TOUCH
        assert not reached, (
            f"{_CANARY_PATH.name} reaches for {sorted(reached)} somewhere at "
            "module scope; binding a scoring primitive to any name makes it "
            "callable from run_shard without the gate and invisible to a "
            "body-only check")

    def test_every_spec_is_proved_resolvable_before_any_shard_is_spawned(self):
        """~$12 must not be spent on a request a local membership test can
        already refuse. ``--negative`` used to bypass ``pick_far_patch``, the
        only local code that touched the positive spec, so three A100 shards
        spawned and each returned {"error": ...} from a check that costs
        nothing here."""
        tree = self._tree()
        main = self._function(tree, "main")
        refusals = [n for n in self._calls(main)
                    if isinstance(n.func, ast.Name)
                    and n.func.id == "_refuse_unresolvable_hotspots"]
        spawns = [n for n in self._calls(main)
                  if isinstance(n.func, ast.Attribute) and n.func.attr == "spawn"]
        assert refusals, "main() spawns without proving the specs resolve"
        assert spawns, "the spawn loop moved; this test is now checking nothing"

        # PER PHASE BRANCH. BOTH phases spawn now — phase 1 used to block on
        # `.remote()`, which handed it no FunctionCall and so nothing to
        # cancel — so "every refusal precedes every spawn" is no longer even
        # expressible: phase 1's spawn necessarily sits above phase 2's
        # refusal. The property the money turns on is unchanged and is checked
        # branch by branch, which is strictly more than the old single ordering
        # said (it never looked at phase 1 at all).
        phase1 = next(node for node in ast.walk(main)
                      if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "phase == 1")
        p1_refusals = [n for n in refusals if n.lineno <= phase1.end_lineno
                       and n.lineno >= phase1.lineno]
        p1_spawns = [n for n in spawns if n.lineno <= phase1.end_lineno
                     and n.lineno >= phase1.lineno]
        assert p1_refusals, "phase 1 spawns $4 of A100 without a local refusal"
        assert p1_spawns, "phase 1 no longer spawns; this half checks nothing"
        assert max(n.lineno for n in p1_refusals) < min(n.lineno for n in p1_spawns), (
            "phase 1's local refusal must run BEFORE its shard is launched")

        p2_refusals = [n for n in refusals if n.lineno > phase1.end_lineno]
        p2_spawns = [n for n in spawns if n.lineno > phase1.end_lineno]
        assert p2_refusals and p2_spawns, "the phase-2 region moved"
        assert max(n.lineno for n in p2_refusals) < min(n.lineno for n in p2_spawns), (
            "the local refusal must run BEFORE any shard is launched")
        assert len(p1_spawns) + len(p2_spawns) == len(spawns), (
            "a .spawn sits outside both phase branches and is unaccounted for")
        phase2 = max(p2_refusals, key=lambda n: n.lineno)
        rendered = ast.unparse(phase2)
        assert "'positive'" in rendered and "'negative'" in rendered, (
            "phase 2 must check BOTH specs; the --negative path is the one "
            f"that skipped the positive check entirely. Got: {rendered}")
        # The refusal must use run_pipeline's own matcher, not a lookalike.
        checker = self._function(tree, "_refuse_unresolvable_hotspots")
        used = self._calls_via(checker, "rp_local")
        assert {"pdb_ca_residues", "select_residues", "missing_hotspots"} <= used, (
            f"the local refusal must reuse run_pipeline's matching; got {used}")

    def test_an_interrupt_between_spawn_and_collect_cancels_the_shards(self):
        """``except Exception`` catches neither KeyboardInterrupt nor
        SystemExit, so a Ctrl-C after the three shards are launched left all
        three A100 containers running to completion with nobody reading the
        result — the one way to spend the full ~$12 and receive nothing at
        all."""
        tree = self._tree()
        main = self._function(tree, "main")
        handlers = [
            handler
            for node in ast.walk(self._body(main)) if isinstance(node, ast.Try)
            for handler in node.handlers
            if handler.type is not None
            and "BaseException" in ast.unparse(handler.type)
        ]
        assert handlers, "main() has no BaseException handler around the shards"
        # The CALL, not the word. This used to be
        # `any("cancel" in ast.unparse(h) for h in handlers)`, which the
        # handler's own print("...cancelling any shard still running")
        # satisfied all by itself: deleting the real
        # `_cancel_outstanding(handles, results)` left 136/136 green, so the
        # single test guarding the largest money-loss path was satisfied by
        # prose. (The behavioural proof is
        # TestHarnessBehaviour.test_an_interrupt_cancels_every_outstanding_shard;
        # this pin is the cheap structural half.)
        cancel_calls = [
            node
            for handler in handlers
            for node in ast.walk(ast.Module(body=list(handler.body),
                                            type_ignores=[]))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_cancel_outstanding"
        ]
        assert cancel_calls, (
            "the BaseException handler does not CALL _cancel_outstanding — a "
            "print() mentioning 'cancelling' is not a cancellation")
        assert any(len(call.args) >= 2 for call in cancel_calls), (
            "_cancel_outstanding must be given the handles and what was already "
            "collected, or it cannot tell which shards are still billing")
        canceller = self._function(tree, "_cancel_outstanding")
        cancels = [n for n in self._calls(canceller)
                   if isinstance(n.func, ast.Attribute) and n.func.attr == "cancel"]
        assert cancels, "_cancel_outstanding does not cancel anything"
        # ...and it must ask for the CONTAINERS to die, not just the inputs.
        # modal.FunctionCall.cancel(terminate_containers: bool = False) with the
        # default marks the inputs TERMINATED and leaves the A100 running, so a
        # bare cancel() stops nothing that costs money — which is the entire
        # reason this handler exists.
        assert any(
            kw.arg == "terminate_containers"
            and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for call in cancels for kw in call.keywords
        ), ("cancel() must pass terminate_containers=True; the default leaves "
            "the billing container alive")

    def test_the_three_phase2_shards_are_spawned_not_awaited_serially(self):
        """Three blocking .remote() calls run strictly serially: worst case
        3 x the 7200 s per-shard cap = 6 h instead of 2."""
        main = self._function(self._tree(), "main")
        attrs = self._call_names(main)
        assert attrs.count("spawn") >= 1
        assert attrs.count("remote") <= 2, "phase 2 must not block per shard"

    def test_the_per_design_path_key_is_named_for_the_container(self):
        source = _CANARY_PATH.read_text(encoding="utf-8")
        assert '"container_path"' in source
        assert '"file":' not in source, (
            'the ambiguous "file" key is what invited a local read'
        )

    def test_the_module_under_test_does_not_import_modal(self):
        source = _SCORING_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "modal" not in imported
        assert imported <= {
            "__future__", "hashlib", "math", "re", "dataclasses", "typing",
        }, f"the scoring module must stay stdlib-only, got {sorted(imported)}"

    def test_the_scoring_module_loads_with_modal_unimportable(self):
        """Exactly how the container loads it (by path), in an interpreter
        where `import modal` raises."""
        code = ("import sys\nsys.modules['modal'] = None\n"
                + _LOAD_BY_PATH + "print(m.overall_outcome([]))\n")
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == cs.INCONCLUSIVE

    def test_the_scoring_module_ships_into_the_image(self):
        """It must be added to the Modal image, or the container-side import
        fails only at runtime, on the GPU, with the money already committed.

        Checked on the AST of the ``image = ...`` assignment, not by counting a
        substring: the string "add_local_file" also appears in a docstring, so a
        count-based test passes even with the call deleted.
        """
        tree = self._tree()
        image_assign = [
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "image" for t in node.targets)
        ]
        assert len(image_assign) == 1, "no top-level `image = ...` assignment"
        added = [
            {ast.unparse(a) for a in node.args}
            for node in ast.walk(image_assign[0])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_local_file"
        ]
        assert any({"_SCORING_LOCAL", "_SCORING_REMOTE"} <= names for names in added), (
            f"_canary_scoring.py is not added to the image; add_local_file "
            f"calls were {added}"
        )
        assert any({"_RUN_PIPELINE_LOCAL", "_RUN_PIPELINE_REMOTE"} <= names
                   for names in added)

    def test_the_files_the_image_copies_are_the_files_that_exist(self):
        """Both halves of the coupling, not just the literal string.

        Pinning ``_SCORING_REMOTE == '/opt/proteina/_canary_scoring.py'`` only
        restates the line above it. What can actually break is (a) the LOCAL
        path naming a file that is not there — ``add_local_file`` then fails at
        image build, on the operator's clock, and (b) the loader looking
        somewhere the image never wrote — which fails in the container, on the
        GPU, with the money already committed. Both are checked against the
        real filesystem and the real loader.
        """
        tree = self._tree()
        consts = {
            node.targets[0].id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        }
        repo_root = _SCORING_PATH.parents[2]
        tool = ast.literal_eval(consts["_TOOL"])
        for name, target in (("_SCORING_LOCAL", _SCORING_PATH),
                             ("_RUN_PIPELINE_LOCAL",
                              _SCORING_PATH.parent / "run_pipeline.py")):
            rendered = ast.unparse(consts[name]).replace(
                "f'", "'").replace("{_TOOL}", tool)
            relative = ast.literal_eval(rendered)
            assert (repo_root / relative).resolve() == target, (
                f"{name} is {relative!r}, which is not {target}")
            assert target.is_file()
        # ...and the loader reads from the SAME constant the image writes to.
        loader = self._function(tree, "_load_scoring")
        assert "_SCORING_REMOTE" in ast.unparse(loader)
        assert "_canary_scoring.py" in ast.literal_eval(
            ast.unparse(consts["_SCORING_REMOTE"]))


# ---------------------------------------------------------------------------
# 13. BEHAVIOURAL execution of _hotspot_canary.py's own function bodies
#
# WHY THIS SECTION EXISTS. A mutation pass over the harness found that EVERY
# refusal written in _hotspot_canary.py survived deletion of its effect while
# the whole suite stayed green:
#
#   * _refuse_unresolvable_hotspots computing everything and never raising
#     (--hotspots A99999 then spawns three A100s, ~$12);
#   * its empty-PDB refusal never raising;
#   * _cancel_outstanding short-circuiting so no handle is cancelled (Ctrl-C
#     leaves three A100s billing to completion);
#   * run_shard's two in-container refusals deleted;
#   * run_shard's identity reference no longer restricted to the contig;
#   * the phase-1 pre-spawn refusal deleted outright, ~$4.
#
# All six are "keep the call, delete the effect", and a structural AST test
# cannot see any of them - TestHarnessStructure asserts a call NODE exists, not
# that calling it does anything. The previous author's stated reason for
# leaving them unpinned was that they "could not be given a behavioural test
# without importing the modal-dependent module". That is false in both
# directions: the decisions belong in _canary_scoring (where they now are, and
# where they are executed directly), and the surrounding Modal-module bodies
# can be EXECUTED here without importing modal at all - the module is parsed,
# the wanted function definitions are lifted out with their decorators
# stripped, and they run against injected stand-ins for _load_rp / subprocess /
# the modal handles. Nothing below imports modal, touches the network or
# spawns a container; `import modal`, the App and the Image never enter the
# extracted body.
# ---------------------------------------------------------------------------


_CANARY_SOURCE = _CANARY_PATH.read_text(encoding="utf-8")

# Module-level constants the lifted functions read. Everything else at module
# scope - `import modal`, `cs = _load_scoring()`, `image = ...`, `app = ...` -
# is deliberately NOT carried over.
_CANARY_CONSTS = frozenset({
    "_TOOL", "_DOCKERFILE", "_RUN_PIPELINE_LOCAL", "_RUN_PIPELINE_REMOTE",
    "_SCORING_LOCAL", "_SCORING_REMOTE", "_GPU", "_MAX_SESSION_S",
    "_COLLECT_TIMEOUT_S", "_FALLBACK_PDB",
    # The shard's design order. Lifted because ``run_shard`` reads them for
    # BOTH the Hydra override and ``n_designs_expected``, and a test that
    # rebinds them in the namespace is how "the two cannot drift" is checked.
    "_NSAMPLES", "_REPLICAS",
    # How long ``run_shard`` waits for the VRAM poller to finish its last
    # sample. Lifted rather than stubbed because the number is the fix: a join
    # shorter than one poll iteration throws the final reading away.
    "_VRAM_JOIN_TIMEOUT_S",
})

# Lifted whether or not a caller asks for it: EVERY function in the harness
# prints through ``_emit`` (the print that cannot raise), so leaving it out
# turns each one of those prints into a NameError inside the lifted body and
# every caller would have to remember to name it.
#
# ``_prealloc_disabled`` joins it for the same reason from the other side: it
# is what ``_poll_vram`` derives its provenance flag from, and a caller lifting
# the poller always means the real derivation. Stubbing it would put back
# exactly the hardcoded answer this pair exists to prevent.
#
# ``_scored_design_counts`` joins them from the same side. It is what turns
# ``run_shard``'s directory into the two numbers the DELIVERY state is decided
# from, and it is deliberately built on production's own ``parse_designs``;
# stubbing it would put back a canary-local answer to "would production have
# delivered this", which is the divergence the whole delivery split exists to
# remove.
_CANARY_ALWAYS_LIFT = frozenset({
    "_emit", "_prealloc_disabled", "_scored_design_counts"})


def load_canary_functions(names, **injected):
    """Execute the REAL bytes of the named _hotspot_canary functions.

    The module cannot be imported (it builds a modal.App at import time and the
    suite must run where modal is absent), but its functions are ordinary
    Python. Their definitions are lifted from the parsed source with decorators
    dropped - ``@app.function(...)`` and ``@app.local_entrypoint()`` are Modal
    plumbing, not behaviour - and executed in a namespace holding the real
    ``_canary_scoring`` plus whatever stand-ins the caller injects.

    Injection happens AFTER exec so it also replaces functions defined in the
    lifted body: the functions resolve their globals from this namespace at
    CALL time, so ``_load_rp``, ``_prune_registry``, ``subprocess`` and friends
    can all be swapped for something that neither spawns a process nor bills an
    A100.
    """
    tree = ast.parse(_CANARY_SOURCE, filename=str(_CANARY_PATH))
    wanted = set(names) | set(_CANARY_ALWAYS_LIFT)
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            node.decorator_list = []
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in _CANARY_CONSTS
            for t in node.targets
        ):
            body.append(node)
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert wanted <= found, f"not found in {_CANARY_PATH}: {sorted(wanted - found)}"
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "__name__": "_hotspot_canary_behavioural",
        "cs": cs, "json": json, "os": os, "sys": sys, "glob": glob,
        "shutil": shutil, "subprocess": subprocess, "threading": threading,
        "time": time, "importlib": importlib, "Path": Path,
    }
    exec(compile(module, str(_CANARY_PATH), "exec"), namespace)
    assert "modal" not in namespace, "the lifted body must never touch modal"
    namespace.update(injected)
    return namespace


class _Handle:
    """A stand-in for ``modal.FunctionCall`` that records what was asked of it.

    ``cancel_raises`` may be a single exception — raised on EVERY cancel, which
    is a container that will not die — or a LIST consumed one entry per call,
    which is the transient failure: a cancel that raises once and succeeds on
    the retry. The second form is what proves a failed cancel is retried rather
    than written off; the first is unchanged for the callers that had it.
    """

    def __init__(self, result=None, raises=None, cancel_raises=None):
        self.result = result
        self.raises = raises
        self.cancel_raises = cancel_raises
        self.cancels = []
        self.gets = 0

    def get(self, timeout=None):
        self.gets += 1
        if self.raises is not None:
            raise self.raises
        return self.result

    def cancel(self, terminate_containers=False):
        self.cancels.append(terminate_containers)
        exc = self.cancel_raises
        if isinstance(exc, list):
            exc = exc.pop(0) if exc else None
        if exc is not None:
            raise exc


class _Remote:
    """``run_shard`` / ``phase0`` without Modal: records calls, returns canned
    results, and NEVER starts anything."""

    def __init__(self, result=None, handles=None):
        self.result = result
        self.handles = list(handles or [])
        self.remote_calls = []
        self.spawn_calls = []

    def remote(self, *args, **kwargs):
        self.remote_calls.append(args)
        return self.result

    def spawn(self, *args, **kwargs):
        self.spawn_calls.append(args)
        if self.handles:
            return self.handles.pop(0)
        if self.result is not None:
            # A caller that only cares what comes BACK, not about the handle.
            # Still a handle, because phase 1 and phase 2 both spawn now —
            # `.remote()` gives no `FunctionCall` and so nothing to cancel.
            return _Handle(result=self.result)
        raise AssertionError("spawned more shards than the test provided")


def _fake_rp(home):
    """``run_pipeline`` with only its side-effecting calls replaced.

    Everything that DECIDES anything - pdb_ca_residues, parse_target_input,
    select_residues, missing_hotspots, derive_segments, format_contig,
    build_target_add_cmd - is the real function, because the whole point of the
    in-container refusal is that it agrees exactly with the local one. Only
    run_streaming (spawns a process), read_targets_dict / registration_mismatch
    (need a real registry) and build_design_cmd (whose output would be run) are
    stubbed.
    """
    fake = types.SimpleNamespace(**{
        name: getattr(rp, name) for name in (
            "pdb_ca_residues", "parse_target_input", "select_residues",
            "missing_hotspots", "format_contig", "derive_segments",
            "build_target_add_cmd", "hotspot_keys", "_HUB_SOURCE",
            # REAL, and it has to be. It is the whole point of the DELIVERY
            # split that the canary answers "would production have delivered
            # this" with PRODUCTION'S OWN parser over the real reward CSV in the
            # real inference tree. A stub here would put back a canary-local
            # answer to that question, which is the divergence being removed.
            "parse_designs",
            # REAL for the same reason, and this one cost a paid A100 to learn.
            # ``stage_cropped_target`` IS production's staging step - the crop
            # plus the count self-check - and ``_stage`` is required to go
            # through it rather than write the upload verbatim. A stub here
            # would let the canary stage whatever it liked and the suite would
            # still be green, which is precisely the state that reproduced
            # upstream's assertion on real hardware.
            "stage_cropped_target", "TargetCropError",
            "crop_pdb_to_contig", "selected_residue_keys",
            # Production's negative-numbering guard, which the canary had no
            # equivalent of until the same audit found it.
            "unrenderable_segments",
        )
    })
    fake.PROTEINA_HOME = str(home)
    fake._TARGETS_DICT = str(home / "targets_dict.yaml")
    # Composed exactly the way run_pipeline composes it, so the canary's
    # staging really is "wherever prod stages" and not a second constant that
    # happens to agree today. tmp_path stands in for PROTEINA_HOME.
    fake._HUB_TARGET_DIR = f"{home}/hub_targets"
    fake.streamed = []
    fake.run_streaming = lambda cmd, cwd: (fake.streamed.append(list(cmd)) or 0)
    fake.read_targets_dict = lambda path: {}
    fake.registration_mismatch = lambda record, expected: None
    # RECORDED, not just stubbed: ``n_designs_expected`` must be the product of
    # the SAME nsamples/replicas the design command was built with, and the only
    # way to see that is to keep what was passed.
    fake.design_cmd_kwargs = []
    fake.build_design_cmd = lambda **kwargs: (
        fake.design_cmd_kwargs.append(dict(kwargs)) or ["designed"])
    fake._rf3_enabled = lambda: False
    # REAL, not stubbed. The canary must launch its design under the same
    # allocator policy production uses — JAX otherwise preallocates 61,440 MB
    # and every VRAM number the shard reports is that constant rather than
    # demand. Lifting the real function is what makes "the canary measures what
    # production runs" checkable instead of assumed.
    fake.design_subprocess_env = rp.design_subprocess_env
    return fake


def _shard_namespace(tmp_path, design_files=(), rc=0):
    """``run_shard`` lifted and wired to a fake container.

    ``design_files`` are written into the inference tree the shard globs, and
    the design command is recorded rather than executed. A name may carry
    directories (``"copy_0/sample_0.pdb"``) — upstream's tree is nested and the
    glob is recursive, so two files under different directories CAN share a
    basename, which is the layout QC's F1 measured. Non-PDB names go in the same
    way, because the shard globs the reward CSVs out of the same tree.
    """
    home = tmp_path / "proteina"
    inference = home / "inference"
    inference.mkdir(parents=True, exist_ok=True)
    for name, text in design_files:
        path = inference / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    fake = _fake_rp(home)
    ran = []

    def _run(cmd, **kwargs):
        ran.append(list(cmd))
        return types.SimpleNamespace(returncode=rc)

    # The design is launched with Popen now, not subprocess.run: the VRAM
    # poller needs the child's pid to attribute memory to the design rather
    # than to the whole device. The fake records the SAME ``ran`` list, and
    # records the env it was handed so a test can assert the allocator flags
    # actually reach the child.
    popen_env = []

    def _popen(cmd, **kwargs):
        ran.append(list(cmd))
        popen_env.append(kwargs.get("env") or {})
        return types.SimpleNamespace(
            pid=4321,
            wait=lambda timeout=None: rc,
            kill=lambda: None,
        )

    namespace = load_canary_functions(
        # ``_collect_run_logs`` / ``_collect_tree`` / ``_mtime`` are lifted, not
        # stubbed: they are the container-side halves of the new diagnostics and
        # the point is to execute them against a real (tmp_path) log tree.
        # ``_stage_dir`` likewise: WHERE the shard stages is now the thing under
        # test, so it must be the real function reading the real
        # ``rp._HUB_TARGET_DIR``.
        {"run_shard", "_stage", "_stage_dir", "_collect_run_logs",
         "_collect_tree", "_mtime"},
        _load_rp=lambda: fake,
        _prune_registry=lambda module: [],
        # ``child_env`` is part of the real signature now: the provenance flag
        # is DERIVED from the env the design child was handed rather than
        # asserted, so the poller has to be told what that env was. The stub
        # accepts and ignores it — what it stands in for is the nvidia-smi
        # polling, not the derivation, which
        # test_the_real_poller_is_the_one_that_sets_the_provenance_keys covers
        # on the real function.
        _poll_vram=lambda stop, out, pid=None, child_env=None: out.update(
            peak_vram_mb=0, peak_proc_vram_mb=0,
            vram_poll_interval_s=1, vram_prealloc_disabled=True,
            vram_poll_complete=True),
        _device_used_mb=lambda: 0,
        _read_hydra_assertion=lambda *a, **k: {"ok": True},
        # rmtree would delete the design files placed above; nothing else in
        # the lifted body needs it.
        shutil=types.SimpleNamespace(rmtree=lambda *a, **k: None),
        subprocess=types.SimpleNamespace(
            run=_run, Popen=_popen,
            TimeoutExpired=subprocess.TimeoutExpired),
    )
    namespace["_fake_rp"] = fake
    namespace["_design_commands"] = ran
    namespace["_design_env"] = popen_env
    return namespace


# A 30-residue target on chain A plus a 10-residue chain B, so a contig naming
# only chain A is a strict subset of the input file's chains.
TWO_CHAIN_INPUT_PDB = "\n".join(
    _trace("A", _TARGET_SEQ)
    + _trace("B", _TARGET_SEQ[:10], y=40.0, serial0=500)) + "\n"


class TestHarnessBehaviour:
    """Every refusal in _hotspot_canary.py, executed rather than inspected."""

    # -- _refuse_unresolvable_hotspots -------------------------------------

    @staticmethod
    def _refusal_namespace():
        return load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)

    def test_an_unresolvable_hotspot_token_actually_raises(self, tmp_path):
        """MUTATION 1: compute everything, never raise. The old suite could only
        see that the call existed, so ``--hotspots A99999`` went on spawning
        three A100s (~$12) to learn what a membership test knew for free."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        namespace = self._refusal_namespace()
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                str(target), "", [("positive", ["A1", "A2", "A3", "A99999"])])
        message = str(excinfo.value)
        assert "A99999" in message and "NO GPU TIME WAS USED" in message
        assert "positive" in message

    def test_a_resolvable_spec_returns_the_contig_and_does_not_raise(self, tmp_path):
        """The refusal must not be a blanket one, or the harness never runs."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        namespace = self._refusal_namespace()
        resolved = namespace["_refuse_unresolvable_hotspots"](
            str(target), "", [("positive", ["A1", "A60"])])
        assert resolved == "A1-60"

    def test_a_target_with_no_ca_residues_actually_raises(self, tmp_path):
        """MUTATION 2: the empty-PDB refusal never raising. Without it the run
        proceeds to spawn against a file from which no hotspot could ever
        resolve."""
        target = tmp_path / "empty.pdb"
        target.write_text("REMARK nothing here at all\n")
        namespace = self._refusal_namespace()
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                str(target), "", [("positive", ["A1"])])
        assert "no CA residues" in str(excinfo.value)

    def test_the_negative_spec_is_checked_too_not_just_the_positive(self, tmp_path):
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        namespace = self._refusal_namespace()
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                str(target), "",
                [("positive", ["A1"]), ("negative", ["A70", "A71"])])
        assert "negative" in str(excinfo.value)

    # -- _cancel_outstanding ------------------------------------------------

    def test_every_uncollected_shard_is_actually_cancelled(self):
        """MUTATION 3: short-circuit so no handle is cancelled. Ctrl-C then
        leaves three A100 containers running to completion with nobody reading
        the result - the one way to spend the full ~$12 and receive nothing."""
        namespace = load_canary_functions({"_cancel_outstanding"})
        done, running_a, running_b = _Handle(), _Handle(), _Handle()
        namespace["_cancel_outstanding"](
            [("positive", done), ("negative", running_a), ("null", running_b)],
            {"positive"})
        assert done.cancels == [], "an already-collected shard is not running"
        assert running_a.cancels == [True], (
            "the outstanding shard was not cancelled with "
            "terminate_containers=True; a bare cancel() leaves the A100 billing")
        assert running_b.cancels == [True]

    def test_one_refusing_handle_does_not_save_the_others(self):
        """Best-effort per handle: a shard that will not die must not stop the
        two that would."""
        namespace = load_canary_functions({"_cancel_outstanding"})
        angry = _Handle(cancel_raises=RuntimeError("gRPC is unhappy"))
        calm_a, calm_b = _Handle(), _Handle()
        namespace["_cancel_outstanding"](
            [("positive", angry), ("negative", calm_a), ("null", calm_b)], set())
        assert angry.cancels == [True]
        assert calm_a.cancels == [True] and calm_b.cancels == [True]

    # -- run_shard ----------------------------------------------------------

    def test_the_shard_refuses_its_own_unresolvable_hotspots(self, tmp_path):
        """MUTATION 4a: the in-container refusal removed. The design command
        must not run."""
        namespace = _shard_namespace(tmp_path)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A99999"], "", 1234, [60, 120],
            False, ["A1"])
        assert "A99999" in out["error"]
        assert "designs" not in out
        assert namespace["_design_commands"] == [], (
            "the shard ran the design command on a spec that resolves to "
            "nothing")

    def test_the_shard_refuses_an_unresolvable_cross_reference(self, tmp_path):
        """MUTATION 4b. Scoring every shard against a reference patch that is
        not in the structure gives recall 0.0 everywhere and a null verdict of
        PASS on nothing."""
        namespace = _shard_namespace(tmp_path)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "negative", ["A20"], "", 1234, [60, 120],
            False, ["A99999"])
        assert "cross-reference" in out["error"] and "A99999" in out["error"]
        assert namespace["_design_commands"] == []

    def test_the_design_runs_under_productions_allocator_policy(self, tmp_path):
        """A canary that measures a different allocator measures nothing.

        The two paid shards recorded ~67.5 GB peak, of which 61,440 MB was
        JAX's default preallocation (0.75 x 80 GB, claimed on the first JAX op
        regardless of target size). That is why they agreed to within 24 MB
        while the chain count doubled, and why no size cap could be derived
        from them. run_pipeline now disables preallocation, and the canary must
        launch its design through the SAME helper — not a copy, and not the
        bare environment, or the next measurement is spoiled the same way.
        """
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        envs = namespace["_design_env"]
        assert envs, "the design was never launched through Popen"
        assert envs[-1].get("XLA_PYTHON_CLIENT_PREALLOCATE") == "false", (
            "the canary launched its design without production's allocator "
            "flags, so its VRAM figure is a preallocation constant again")

    def test_the_shard_reports_which_allocator_it_measured_under(self, tmp_path):
        """Readings from before and after the allocator fix are not comparable,
        so a shard has to say which it is. Without this flag the next operator
        cannot tell a 67 GB preallocation reading from a 67 GB real one."""
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        assert out["vram_prealloc_disabled"] is True
        # Device-wide, process-only and the pre-run baseline are all reported:
        # the single device figure is what was misread as demand.
        assert "peak_proc_vram_mb" in out
        assert "baseline_vram_mb" in out

    def test_the_real_poller_is_the_one_that_sets_the_provenance_keys(self):
        """The test above runs against a STUBBED _poll_vram, so on its own it
        only proves run_shard copies keys through — deleting the real poller's
        provenance fields would not fail it. This executes the real function
        (stop already set, so it takes one sample and returns) and pins that it
        is the poller, not the stub, that reports the interval and the
        allocator state, plus the per-process figure the device reading has to
        be checked against.

        THE ENV IS NOW PASSED, and that is the point rather than a detail. This
        test used to call the poller with no env at all and assert the flag was
        True — which passed only because the flag WAS an unconditional literal.
        The assertion was therefore a restatement of the defect: it would have
        gone on passing for an operator who exported
        XLA_PYTHON_CLIENT_PREALLOCATE=true and got a child that preallocated
        61,440 MB under a shard reporting `disabled=True`. Handing it
        production's real env keeps `is True` while making it mean something —
        that the derivation reads this env as preallocation-off — and
        ``test_an_operator_override_is_reported_as_preallocating`` covers the
        other side.
        """
        import threading as _threading
        namespace = load_canary_functions(
            {"_poll_vram"},
            _device_used_mb=lambda: 1234,
            _proc_used_mb=lambda pid: 567,
        )
        stop = _threading.Event()
        stop.set()
        out: dict = {}
        namespace["_poll_vram"](stop, out, 999,
                                child_env=rp.design_subprocess_env())
        assert out["vram_prealloc_disabled"] is True
        assert out["vram_poll_interval_s"] == 1, (
            "the poll interval is part of the reading: the existing "
            "measurements were sampled at 5 s and are lower bounds")
        assert out["peak_vram_mb"] == 1234
        assert out["peak_proc_vram_mb"] == 567

    def test_the_poller_reports_an_overridden_allocator_honestly(self):
        """The companion the assertion above needs to mean anything. Same real
        function, same call, an env where the operator put preallocation back
        on — the flag has to follow the CHILD, not this file's intentions."""
        import threading as _threading
        namespace = load_canary_functions(
            {"_poll_vram"},
            _device_used_mb=lambda: 1234,
            _proc_used_mb=lambda pid: 567,
        )
        stop = _threading.Event()
        stop.set()
        out: dict = {}
        env = dict(rp.design_subprocess_env())
        env["XLA_PYTHON_CLIENT_PREALLOCATE"] = "true"
        namespace["_poll_vram"](stop, out, 999, child_env=env)
        assert out["vram_prealloc_disabled"] is False, (
            "a shard whose child preallocated still claims the allocator fix "
            "was in force; that is the mislabelling this field exists to stop"
        )

    def test_a_resolvable_shard_scores_its_designs(self, tmp_path):
        """The refusals must not be blanket ones: the same path with a good
        spec has to reach the per-design scoring."""
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        assert out.get("error") is None
        assert namespace["_design_commands"], "the design command never ran"
        assert out["n_complexes"] == 1
        assert out["n_target_verified"] == 1
        assert out["hotspot_recall_median"] == 1.0

    def test_the_identity_reference_is_restricted_to_the_contig(self, tmp_path):
        """MUTATION 5: the reference no longer restricted to the contig's
        selection.

        The contig names A1-20; the design output carries the UNCROPPED chain
        A1-30. Restricted, only 20 of the design's 30 chain-A residues are
        accountable - 67% coverage, under the floor - so the design is
        UNSCORABLE and says so. Unrestricted, the reference silently grows to
        all 30, coverage is 100%, and the shard scores geometry against
        residues upstream was never told to use.
        """
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "A1-20", 1234,
            [60, 120], False, ["A1", "A2"])
        assert out["n_reference_residues"] == 20, (
            "the reference must be the contig's selection, not the whole file")
        assert out["n_complexes"] == 1
        assert out["n_target_verified"] == 0, (
            "a design carrying residues outside the contig is not accountable")
        assert out["hotspot_recall_median"] is None

    def test_the_scored_target_chains_come_from_the_contig_not_the_file(
            self, tmp_path):
        """FINDING 7. ``target_chains`` was every chain of the INPUT file while
        the identity reference was the contig's selection, so a contig naming a
        subset of the chains made the two disagree.

        Here the input is chains A+B and the contig is chain A only. Scoring
        every input chain as target makes ``present - wanted`` empty for a
        design of exactly A+B - not even a complex, so ~$12 buys an
        INCONCLUSIVE - while the reference contains chain A alone.
        """
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        out = namespace["run_shard"](
            TWO_CHAIN_INPUT_PDB, "positive", ["A1", "A2"], "A1-30", 1234,
            [60, 120], False, ["A1", "A2"])
        assert out["input_chains"] == ["A", "B"]
        assert out["target_chains"] == ["A"], (
            "only the contig's chains may be scored as target")
        assert out["n_reference_residues"] == 30
        assert out["n_complexes"] == 1, (
            "chain B of the design is the binder, so this IS a complex")
        assert out["n_target_verified"] == 1
        assert out["hotspot_recall_median"] == 1.0

    # -- main ---------------------------------------------------------------

    @staticmethod
    def _main_namespace(shard=None, phase0=None, handles=None):
        return load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp,
            run_shard=shard if shard is not None else _Remote(handles=handles),
            phase0=phase0 if phase0 is not None else _Remote(result={}),
        )

    @pytest.mark.parametrize("phase", [3, 5, -1, 99])
    def test_an_unknown_phase_refuses_instead_of_running_the_12_dollar_one(
            self, phase, tmp_path):
        """FINDING 4. ``main`` branched on ``phase == 0`` and ``phase == 1``
        and then FELL THROUGH to phase 2, so ``--phase 3`` silently spawned the
        three-shard ~$12 run."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        shard = _Remote()
        namespace = self._main_namespace(shard=shard)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["main"](phase=phase, target_pdb=str(target),
                              hotspots="A1 A2", negative="A50 A51")
        assert str(phase) in str(excinfo.value)
        assert shard.spawn_calls == [] and shard.remote_calls == [], (
            f"--phase {phase} launched a shard")

    def test_phase_one_with_an_empty_hotspot_spec_refuses(self, tmp_path):
        """FINDING 6. ``positive = []`` makes the pre-spawn refusal vacuous, the
        shard registers no hotspots, ``hotspots_match`` compares set() with
        set() and phase 1 reports PASS - "carries our hotspots" - having
        asserted nothing, for $4."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        shard = _Remote()
        namespace = self._main_namespace(shard=shard)
        with pytest.raises(cs.CanaryRefusal):
            namespace["main"](phase=1, target_pdb=str(target), hotspots="")
        assert shard.spawn_calls == [] and shard.remote_calls == [], (
            "phase 1 spent $4 on an empty spec")

    def test_phase_two_with_an_empty_spec_and_a_negative_refuses(self, tmp_path):
        """FINDING 5. ``--negative <spec>`` skips ``pick_far_patch``, so its
        empty-spec refusal never runs; ``missing_hotspots(selected, [])`` is
        vacuously []; three shards spawn and every metric comes back None. ~$12
        for a guaranteed INCONCLUSIVE/INCONCLUSIVE/INCONCLUSIVE."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        shard = _Remote()
        namespace = self._main_namespace(shard=shard)
        with pytest.raises(cs.CanaryRefusal):
            namespace["main"](phase=2, target_pdb=str(target), hotspots="",
                              negative="A50 A51 A52 A53")
        assert shard.spawn_calls == [], "phase 2 spawned three shards for nothing"

    def test_phase_one_refuses_an_unresolvable_spec_before_spending(self, tmp_path):
        """MUTATION 6: the phase-1 pre-spawn refusal deleted outright, ~$4."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        shard = _Remote()
        namespace = self._main_namespace(shard=shard)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["main"](phase=1, target_pdb=str(target),
                              hotspots="A1 A99999")
        assert "A99999" in str(excinfo.value)
        assert shard.spawn_calls == [] and shard.remote_calls == [], (
            "phase 1 spawned on an unresolvable spec")

    def test_phase_one_does_run_the_shard_when_the_spec_resolves(self, tmp_path):
        """...and the refusal is not a blanket one.

        Read off ``spawn_calls``: phase 1 SPAWNS rather than blocking on
        ``.remote()``, because ``.remote()`` yields no ``FunctionCall`` and so
        leaves nothing for ``_cancel_outstanding`` to kill.
        """
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        result = _shard("phase1", n=4, recall=1.0, centroid=0.0)
        result["hydra"] = {"task_name_selected": True, "hotspots_match": True,
                           "hotspots_order_matches": True}
        shard = _Remote(result=result)
        namespace = self._main_namespace(shard=shard)
        namespace["main"](phase=1, target_pdb=str(target), hotspots="A1 A2")
        assert len(shard.spawn_calls) == 1
        assert shard.remote_calls == [], (
            "a blocking .remote() gives phase 1 no handle to cancel")
        assert shard.spawn_calls[0][2] == ["A1", "A2"], "the spec that was sent"

    def test_an_interrupt_cancels_every_outstanding_shard(self, tmp_path):
        """FINDING 3 / MUTATION 3, end to end.

        The test that used to guard this asserted
        ``any("cancel" in ast.unparse(h) for h in handlers)`` - satisfied by the
        handler's own ``print("...cancelling any shard still running")``. With
        the real ``_cancel_outstanding(handles, results)`` deleted the suite
        stayed at 136/136 while a Ctrl-C left three A100s billing to
        completion. This calls main and interrupts the collect.
        """
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        handles = [_Handle(raises=KeyboardInterrupt()), _Handle(), _Handle()]
        shard = _Remote(handles=list(handles))
        namespace = self._main_namespace(shard=shard)
        with pytest.raises(KeyboardInterrupt):
            namespace["main"](phase=2, target_pdb=str(target),
                              hotspots="A1 A2", negative="A50 A51 A52 A53")
        assert len(shard.spawn_calls) == 3, "all three shards were launched"
        for handle in handles:
            assert handle.cancels == [True], (
                "an interrupted phase 2 left a shard running and billing")

    def test_phase_two_spawns_three_shards_and_the_null_one_has_no_hotspots(
            self, tmp_path):
        """The positive path, so the refusals above are shown not to block it -
        and the null shard's spec really is empty, which is the one place an
        empty spec is correct."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        results = [
            _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0),
            _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0),
            _shard("null", n=8, recall=None, centroid=1.0, cross=0.0),
        ]
        handles = [_Handle(result=r) for r in results]
        shard = _Remote(handles=list(handles))
        namespace = self._main_namespace(shard=shard)
        namespace["main"](phase=2, target_pdb=str(target), hotspots="A1 A2",
                          negative="A50 A51 A52 A53")
        assert [call[1] for call in shard.spawn_calls] == [
            "positive", "negative", "null"]
        assert shard.spawn_calls[0][2] == ["A1", "A2"]
        assert shard.spawn_calls[1][2] == ["A50", "A51", "A52", "A53"]
        assert shard.spawn_calls[2][2] == [], "the null shard carries NO hotspots"
        # ...and every shard is cross-scored against the POSITIVE patch.
        for call in shard.spawn_calls:
            assert call[7] == ["A1", "A2"]
        for handle in handles:
            assert handle.cancels == [], "a collected shard must not be cancelled"

    def test_a_failing_phase_two_exits_with_the_fail_code(self, tmp_path):
        """A null run that scores as well as the positive one: the hotspots
        were passed and ignored."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        results = [
            _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0),
            _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0),
            _shard("null", n=8, recall=None, centroid=1.0, cross=1.0),
        ]
        shard = _Remote(handles=[_Handle(result=r) for r in results])
        namespace = self._main_namespace(shard=shard)
        with pytest.raises(SystemExit) as excinfo:
            namespace["main"](phase=2, target_pdb=str(target),
                              hotspots="A1 A2", negative="A50 A51 A52 A53")
        assert excinfo.value.code == cs.EXIT_CODES[cs.FAIL]

    # -- the delivery note is WIRED IN, not merely written ------------------
    #
    # ``cs.delivery_note`` had tests; ``main`` calling it had none, so deleting
    # both call sites passed 561/561. That is not a silent hole - the verdict
    # reason carries a ``[DELIVERED-DEGRADED]`` prefix through ``_print_verdict``
    # independently - but a crash that no longer moves the verdict must move the
    # console, and "must" is worth an assertion rather than an argument.

    @staticmethod
    def _degraded(label, **over):
        """A shard that CRASHED and DELIVERED, in ``_shard``'s shape."""
        out = _shard(label, exit_code=1, **over)
        out["n_scored_designs"] = out["n_designs_expected"]
        out["n_reward_rows"] = out["n_designs_expected"]
        return out

    def test_phase_one_prints_the_delivery_note_for_a_crashed_delivering_shard(
            self, tmp_path, capsys):
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        result = self._degraded("phase1", n=8, recall=1.0, centroid=0.0)
        result["hydra"] = {"task_name_selected": True, "hotspots_match": True,
                           "hotspots_order_matches": True}
        namespace = self._main_namespace(shard=_Remote(result=result))
        namespace["main"](phase=1, target_pdb=str(target), hotspots="A1 A2")
        out = capsys.readouterr().out
        assert "DELIVERED-DEGRADED" in out
        assert "Production would have shipped this run" in out, (
            "main() no longer prints cs.delivery_note for phase 1")
        assert "fully scored 8" in out

    def test_phase_one_prints_nothing_extra_for_a_clean_shard(self, tmp_path, capsys):
        """The other half: a healthy run's console must be unchanged."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        result = _shard("phase1", n=8, recall=1.0, centroid=0.0)
        result["hydra"] = {"task_name_selected": True, "hotspots_match": True,
                           "hotspots_order_matches": True}
        namespace = self._main_namespace(shard=_Remote(result=result))
        namespace["main"](phase=1, target_pdb=str(target), hotspots="A1 A2")
        assert "DEGRADED" not in capsys.readouterr().out

    def test_phase_two_prints_the_delivery_note_per_shard(self, tmp_path, capsys):
        """PER SHARD, and the loop is what makes it per shard: a phase-2 run
        where only one control crashed-but-delivered is exactly the case the
        label has to attribute."""
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        results = [
            _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0),
            self._degraded("negative", n=8, recall=1.0, centroid=1.0, cross=0.0),
            _shard("null", n=8, recall=None, centroid=1.0, cross=0.0),
        ]
        namespace = self._main_namespace(
            shard=_Remote(handles=[_Handle(result=r) for r in results]))
        namespace["main"](phase=2, target_pdb=str(target), hotspots="A1 A2",
                          negative="A50 A51 A52 A53")
        out = capsys.readouterr().out
        assert "DELIVERED-DEGRADED [negative]" in out, (
            "main() no longer prints cs.delivery_note for phase 2")
        assert "DELIVERED-DEGRADED [positive]" not in out
        assert "DELIVERED-DEGRADED [null]" not in out


# ---------------------------------------------------------------------------
# 14. THE IDENTITY GATE FAILING **OPEN**
#
# ``same_residue`` returns True when EITHER side is UNK/UNX/XAA/X, so a design
# chain whose residues are ALL unknown scored sequence_identity = 1.0 against
# ANY reference and was CERTIFIED as the target. That is not an exotic input:
# Proteina generates BACKBONES, and a backbone has no sequence, so
# sequence-free output is the shape the tool is most likely to emit.
#
# Measured on the pre-fix code, with the binder written as chain A (the input
# target's chain id) and the real target on chain B:
#
#   binder residues       key_coverage  sequence_identity  verified  recall
#   a real, different seq     1.0            0.04           False    refused
#   poly-GLY                  1.0            0.05           False    refused
#   poly-UNK / UNX / XAA      1.0            1.0            TRUE     1.0 FABRICATED
#
# and 120 synthetic phase-2 runs in that shape returned overall FAIL 120/120
# where the only correct answer is INCONCLUSIVE. Worse, because every design
# "verified", n_target_verified == n_complexes and the UNSCORABLE note and the
# chain map never printed - the diagnostic that would have revealed the
# inversion was suppressed by the gate meant to catch it.
#
# WHY THE OLD SUITE MISSED IT. The relabelling fixtures use a poly-GLY binder
# against a target asserted GLY-free, so identity is exactly 0.0 - the safe
# direction. The single UNK test placed ONE unknown inside an otherwise-correct
# target, which is also the safe direction. Nothing tested a chain that is
# PREDOMINANTLY unknown, which is where the wildcard turns into a blanket pass.
# ---------------------------------------------------------------------------


def _unknown_relabelled_pdb(unknown="UNK", n=30):
    """The binder written on the target's chain id, with NO sequence at all.

    Same geometry as RELABELLED_DESIGN_PDB, so every coordinate-derived number
    is identical and only the residue NAMES differ - which is the whole point:
    coordinates cannot tell a binder from a target, names can, and an unknown
    name is not a name.
    """
    return "\n".join(
        _trace("A", [unknown] * n)
        + _trace("B", _TARGET_SEQ[:4], y=4.0, serial0=100)) + "\n"


class TestUnknownResiduesCannotCertify:
    SPEC = ["A1", "A2"]

    @pytest.mark.parametrize("unknown", sorted(cs.UNKNOWN_RESNAMES))
    def test_an_all_unknown_chain_is_unscorable_not_verified(self, unknown):
        """THE defect. Every one of these scored identity 1.0 and was certified,
        then reported a fabricated hotspot_recall off the binder's own
        self-contacts."""
        entry = cs.score_design_file(
            _unknown_relabelled_pdb(unknown), {"A"}, self.SPEC, self.SPEC,
            TARGET_REFERENCE)
        assert entry["is_complex"] is True, "the trap needs this to look normal"
        assert entry["target_verified"] is False, (
            f"a chain of {unknown} carries no sequence evidence and must not "
            "certify anything")
        assert "hotspot_recall" not in entry, "a fabricated recall reached a verdict"
        assert "cross_hotspot_recall" not in entry
        identity = entry["target_identity"]
        assert identity["n_informative"] == 0
        assert identity["sequence_identity"] is None, (
            "an all-unknown chain has no identity, and it is not 1.0")
        assert identity["key_coverage"] == 1.0, (
            "key overlap alone still certifies it - identity is what has to "
            "refuse")
        assert "UNK" in entry["unscorable_reason"]

    def test_an_unknown_binder_longer_than_the_target_is_still_refused(self):
        """The pre-fix gate certified a 120-residue unknown chain against a
        115-residue target: coverage 0.958 clears the floor and identity was
        1.0. Same shape here at 33 vs 30."""
        entry = cs.score_design_file(
            _unknown_relabelled_pdb(n=33), {"A"}, self.SPEC, [],
            TARGET_REFERENCE)
        coverage = entry["target_identity"]["key_coverage"]
        assert coverage >= cs.TARGET_MIN_KEY_COVERAGE, (
            "the coverage half must NOT be what refuses this, or the test is "
            "measuring the wrong gate")
        assert entry["target_verified"] is False
        assert "hotspot_recall" not in entry

    def test_the_diagnostic_that_reveals_the_inversion_now_prints(self):
        """The second cost of failing open: with every design 'verified',
        ``n_target_verified == n_complexes`` and both the UNSCORABLE note and
        the chain map are suppressed - the operator is shown a confident FAIL
        with nothing pointing at the measurement problem."""
        designs = [cs.score_design_file(_unknown_relabelled_pdb(), {"A"},
                                        self.SPEC, self.SPEC, TARGET_REFERENCE)
                   for _ in range(8)]
        n_complexes = sum(1 for d in designs if d.get("is_complex"))
        n_verified = sum(1 for d in designs if d.get("target_verified"))
        assert n_complexes == 8
        assert n_verified == 0, (
            "n_target_verified == n_complexes is exactly what silenced the "
            "UNSCORABLE branch in main() and in run_shard's summary")
        assert all(d.get("unscorable_reason") for d in designs)
        assert all(d["target_identity"]["chain_hints"] for d in designs)

    def test_the_chain_map_does_not_claim_an_unknown_chain_matches_everything(self):
        """The chain map is the diagnostic the operator reads to work out what
        upstream emitted. An all-UNK chain 'looked like' every reference chain
        at 100%, which points at nothing."""
        entry = cs.score_design_file(
            _unknown_relabelled_pdb(), {"A"}, self.SPEC, [], TARGET_REFERENCE)
        hints = entry["target_identity"]["chain_hints"]
        assert hints["A"]["best_match"]["sequence_identity"] is None
        assert hints["A"]["best_match"]["n_informative"] == 0
        # ...while the chain that really IS the target still says so.
        assert hints["B"]["best_match"]["sequence_identity"] == 1.0
        assert hints["B"]["best_match"]["reference_chain"] == "A"

    def test_end_to_end_an_all_unknown_phase2_is_inconclusive_not_a_verdict(self):
        """The 120-run shape, whole. Pre-fix this returned overall FAIL - a $12
        CONDEMNATION of a feature that may work fine, built entirely out of
        numbers measured off the binder's contacts with itself. A false PASS was
        never excluded either: the fabricated recall took values 0.0 / 0.25 /
        0.375 / 0.75 / 1.0 across geometries, i.e. unconstrained garbage rather
        than something systematically low."""
        eight = [_unknown_relabelled_pdb()] * 8
        make = TestTargetChainIdentity._shard_from_designs
        pos = make("positive", eight, self.SPEC, self.SPEC)
        neg = make("negative", eight, ["A20"], self.SPEC)
        null = make("null", eight, [], self.SPEC)
        assert pos["n_complexes"] == 8
        assert pos["n_target_verified"] == 0
        assert pos["hotspot_recall_median"] is None, (
            "scored through the pre-fix gate this is a fabricated 1.0")
        report = cs.phase2_report(pos, neg, null)
        assert report["overall"] == cs.INCONCLUSIVE, [
            v.reason for v in report["verdicts"]]
        assert report["overall"] != cs.FAIL, (
            "condemning the feature on unmeasurable evidence is the pre-fix "
            "behaviour, 120/120")
        assert report["exit_code"] == cs.EXIT_CODES[cs.INCONCLUSIVE]

    def test_a_few_unknowns_inside_a_real_chain_are_still_fine(self):
        """The wildcard is legitimate at low dose - a refold that writes a
        handful of UNKs must not make a genuine target unscorable, which would
        be a $12 INCONCLUSIVE on a run that was fine."""
        seq = list(_TARGET_SEQ)
        for i in (5, 11, 17):
            seq[i] = "UNK"
        design = "\n".join(
            _trace("A", seq) + _trace("B", ["GLY"] * 4, y=4.0, serial0=100)) + "\n"
        entry = cs.score_design_file(design, {"A"}, self.SPEC, [],
                                     TARGET_REFERENCE)
        assert entry["target_verified"] is True
        assert entry["target_identity"]["n_informative"] == 27
        assert entry["target_identity"]["sequence_identity"] == 1.0
        assert entry["hotspot_recall"] == 1.0

    def test_a_predominantly_unknown_chain_is_refused_by_the_fraction(self):
        """Between "a handful" and "all of them" there has to be a line. Half
        the matched keys unknown is not a chain identified by its sequence, even
        though 15 informative residues clear the absolute count."""
        seq = list(_TARGET_SEQ)
        for i in list(range(0, 30, 2)) + [1]:
            seq[i] = "UNK"
        design = "\n".join(
            _trace("A", seq) + _trace("B", ["GLY"] * 4, y=4.0, serial0=100)) + "\n"
        entry = cs.score_design_file(design, {"A"}, self.SPEC, [],
                                     TARGET_REFERENCE)
        identity = entry["target_identity"]
        assert identity["n_informative"] == 14, (
            "14 clears the absolute floor of "
            f"{cs.TARGET_MIN_INFORMATIVE_RESIDUES}, so the COUNT is not what "
            "refuses this")
        assert identity["sequence_identity"] == 1.0, "and identity is perfect"
        assert identity["informative_fraction"] < cs.TARGET_MIN_INFORMATIVE_FRACTION
        assert entry["target_verified"] is False
        assert "unknown" in entry["unscorable_reason"]

    def test_a_reference_with_no_sequence_of_its_own_certifies_nothing(self):
        """The other direction of the same hole: if the INPUT target is itself
        sequence-free, no comparison can tell it apart from a binder, so the
        answer is UNSCORABLE rather than 'everything matches'."""
        blank_reference = {k: "UNK" for k in TARGET_REFERENCE}
        entry = cs.score_design_file(
            CORRECT_DESIGN_PDB, {"A"}, self.SPEC, [], blank_reference)
        assert entry["target_verified"] is False
        assert "INPUT target itself" in entry["unscorable_reason"]
        assert "hotspot_recall" not in entry

    def test_same_residue_is_still_permissive_and_the_denominator_is_not(self):
        """The wildcard behaviour itself is kept - it is what stops one stray
        UNK from failing a real target. What changed is that a True meaning "I
        could not tell" is no longer counted as evidence FOR the chain."""
        assert cs.same_residue("UNK", "TRP") is True
        assert cs.same_residue("MSE", "MET") is True
        assert cs.same_residue("ALA", "TRP") is False
        assert cs.is_informative_pair("UNK", "TRP") is False
        assert cs.is_informative_pair("MSE", "MET") is True
        assert all(cs.is_unknown_resname(name) for name in cs.UNKNOWN_RESNAMES)
        stats = cs.identity_stats({("A", 1): "UNK", ("A", 2): "ALA"},
                                  {("A", 1): "TRP", ("A", 2): "ALA"})
        assert (stats.n_matched, stats.n_informative, stats.n_identical) == (2, 1, 1)
        assert stats.sequence_identity == 1.0, "over the informative pair only"
        assert stats.informative_fraction == 0.5

    def test_the_informative_floor_is_capped_at_what_the_reference_offers(self):
        """A genuinely tiny target must still be verifiable, and a reference
        with nothing informative must still refuse."""
        assert cs.informative_floor(500) == cs.TARGET_MIN_INFORMATIVE_RESIDUES
        assert cs.informative_floor(3) == 3
        assert cs.informative_floor(0) == 1, (
            "capping to 0 would restore the fail-open: no design can supply 1 "
            "informative residue against a reference that has none")


# ---------------------------------------------------------------------------
# 15. POOLED IDENTITY HIDES A MINORITY CHAIN
# ---------------------------------------------------------------------------


class TestPerChainIdentity:
    """Identity was computed over every matched key on every wanted chain at
    once, so up to 10% of the "target" could be an entirely different molecule
    and still clear the 0.9 floor. Both cases below were reproduced as
    VERIFIED against the pre-fix gate.
    """

    @staticmethod
    def _repeat(n):
        return [_TARGET_SEQ[i % len(_TARGET_SEQ)] for i in range(n)]

    def test_a_foreign_minority_chain_no_longer_passes_on_the_pool(self):
        """Input A(600) + B(60); the design writes A correctly and something
        else entirely as B. Pooled identity 0.912 - VERIFIED pre-fix."""
        big, small = self._repeat(600), self._repeat(60)
        reference = cs.ca_resnames(cs.heavy_atoms("\n".join(
            _trace("A", big) + _trace("B", small, y=60.0, serial0=2000)) + "\n"))
        design = "\n".join(
            _trace("A", big)
            + _trace("B", ["GLY"] * 60, y=60.0, serial0=2000)
            + _trace("C", ["GLY"] * 8, y=4.0, serial0=5000)) + "\n"
        entry = cs.score_design_file(design, {"A", "B"}, ["A1"], [], reference)
        identity = entry["target_identity"]
        assert identity["sequence_identity"] >= cs.TARGET_MIN_SEQUENCE_IDENTITY, (
            "the POOLED identity still clears the floor - that is the defect, "
            "and if this assertion ever fails the test is measuring something "
            "else")
        assert entry["target_verified"] is False
        assert identity["per_chain"]["A"]["sequence_identity"] == 1.0
        assert identity["per_chain"]["B"]["sequence_identity"] == 0.0
        assert "chain B" in entry["unscorable_reason"]
        assert "hotspot_recall" not in entry

    def test_a_relabelled_chain_of_a_homotetramer_no_longer_passes(self):
        """A/B/C/D x 300 with the binder written onto chain A: pooled 0.904,
        VERIFIED pre-fix, and the binder's own contacts would then have been
        scored as target occupancy."""
        seq = self._repeat(300)
        reference = cs.ca_resnames(cs.heavy_atoms("\n".join(
            line for i, chain in enumerate("ABCD")
            for line in _trace(chain, seq, y=i * 40.0, serial0=1 + i * 400)
        ) + "\n"))
        design_lines = list(_trace("A", ["GLY"] * 100))
        for i, chain in enumerate("BCD", start=1):
            design_lines += _trace(chain, seq, y=i * 40.0, serial0=1 + i * 400)
        atoms = cs.heavy_atoms("\n".join(design_lines) + "\n")
        check = cs.verify_target_identity(atoms, {"A", "B", "C", "D"}, reference)
        assert check["sequence_identity"] >= cs.TARGET_MIN_SEQUENCE_IDENTITY, (
            "the pooled identity still clears the floor")
        assert check["verified"] is False
        assert check["per_chain"]["A"]["sequence_identity"] == 0.0
        assert "chain A" in check["reason"]

    def test_a_foreign_minority_chain_with_no_sequence_is_refused_too(self):
        """The same shape with the foreign chain written as a bare backbone.
        The pooled informative fraction is 0.91 - well clear - so only the
        PER-CHAIN informative floor catches this one."""
        big, small = self._repeat(600), self._repeat(60)
        reference = cs.ca_resnames(cs.heavy_atoms("\n".join(
            _trace("A", big) + _trace("B", small, y=60.0, serial0=2000)) + "\n"))
        design = "\n".join(
            _trace("A", big)
            + _trace("B", ["UNK"] * 60, y=60.0, serial0=2000)
            + _trace("C", ["GLY"] * 8, y=4.0, serial0=5000)) + "\n"
        entry = cs.score_design_file(design, {"A", "B"}, ["A1"], [], reference)
        identity = entry["target_identity"]
        assert identity["informative_fraction"] > cs.TARGET_MIN_INFORMATIVE_FRACTION
        assert identity["n_informative"] >= cs.TARGET_MIN_INFORMATIVE_RESIDUES
        assert entry["target_verified"] is False
        assert identity["per_chain"]["B"]["n_informative"] == 0
        assert "chain B" in entry["unscorable_reason"]

    def test_a_half_renumbered_minority_chain_is_refused_by_coverage(self):
        """The per-chain COVERAGE floor, on the only case where it is the sole
        refuser — and it had no test at all, so deleting it left the suite green.

        Input A(600) + B(60). The design writes A perfectly and RENUMBERS half
        of B into the 5000s. Every check except one passes: pooled coverage is
        95% (clear), pooled and per-chain identity are 1.0, and chain B's 30
        surviving residues are all informative, so neither the informative count
        nor the informative fraction fires. Only ``chain B has 50% of its
        residues at the same key`` refuses it.

        It has to. Hotspot tokens are matched by NUMBER — upstream's key is the
        literal ``f"{chain_id}{res_id}"`` — so a chain half of which is
        renumbered resolves ``B37`` to a different residue or to none, and the
        contact set and recall denominator are then measured against residues
        upstream never saw. Pooling hides it because 30 bad residues out of 660
        is 4.5%.
        """
        big, small = self._repeat(600), self._repeat(60)
        reference = cs.ca_resnames(cs.heavy_atoms("\n".join(
            _trace("A", big) + _trace("B", small, y=60.0, serial0=2000)) + "\n"))
        # B1..B30 keep the input's numbering; B31..B60 are re-emitted as
        # B5031..B5060, which exist in the design and match no reference key.
        # (Their y is arbitrary: verification fails before any geometry runs.)
        #
        # THE RENUMBERED HALF CARRIES GLY, AND THAT IS WHAT KEEPS THIS TEST
        # POINTED AT THE COVERAGE FLOOR. ``restore_input_numbering`` repairs an
        # IN-ORDER renumbering before this gate ever sees it, and it would
        # repair this one too — same length, same order — leaving coverage at
        # 1.0 and this test measuring nothing. GLY is absent from _TARGET_SEQ by
        # construction, so the positional sequence check fails, the restore
        # declines, and chain B's 50% coverage is once again the only thing
        # refusing this design. The repairable case is pinned separately by
        # TestRenumberedTargetsAreRestored.
        design = "\n".join(
            _trace("A", big)
            + _trace("B", small[:30], y=60.0, serial0=2000)
            + _trace("B", ["GLY"] * 30, y=100.0, first_res=5031, serial0=3000)
            + _trace("C", ["GLY"] * 8, y=4.0, serial0=5000)) + "\n"
        entry = cs.score_design_file(design, {"A", "B"}, ["A1"], [], reference)
        identity = entry["target_identity"]
        assert identity["key_coverage"] >= cs.TARGET_MIN_KEY_COVERAGE, (
            "the POOLED coverage still clears the floor — that is the defect, "
            "and if this assertion ever fails the test is measuring the wrong "
            "gate")
        assert identity["sequence_identity"] == 1.0, (
            "identity is perfect, so it is not what refuses this")
        assert identity["n_informative"] >= cs.TARGET_MIN_INFORMATIVE_RESIDUES
        assert identity["informative_fraction"] == 1.0, (
            "nothing here is UNK, so no informative check refuses it either")
        assert identity["per_chain"]["A"]["key_coverage"] == 1.0
        assert identity["per_chain"]["B"]["key_coverage"] == 0.5
        assert identity["per_chain"]["B"]["sequence_identity"] == 1.0
        assert entry["target_verified"] is False
        assert "chain B" in entry["unscorable_reason"]
        assert "hotspot_recall" not in entry

    def test_a_correct_multi_chain_target_is_still_verified(self):
        """The per-chain floor must not make every real multi-chain target
        unscorable - that would be a $12 INCONCLUSIVE by construction."""
        big, small = self._repeat(600), self._repeat(60)
        target = "\n".join(
            _trace("A", big) + _trace("B", small, y=60.0, serial0=2000)) + "\n"
        reference = cs.ca_resnames(cs.heavy_atoms(target))
        design = "\n".join(
            _trace("A", big) + _trace("B", small, y=60.0, serial0=2000)
            + _trace("C", ["GLY"] * 8, y=4.0, serial0=5000)) + "\n"
        entry = cs.score_design_file(design, {"A", "B"}, ["A1"], [], reference)
        assert entry["target_verified"] is True
        assert set(entry["target_identity"]["per_chain"]) == {"A", "B"}
        assert entry["hotspot_recall"] is not None


# ---------------------------------------------------------------------------
# 16. Two small closures: a hashable Verdict, and the one place the canary's
#     residue filter and run_pipeline's diverge.
# ---------------------------------------------------------------------------


class TestVerdictIsHashable:
    """``@dataclass(frozen=True, eq=False)`` plus an explicit ``__eq__`` sets
    ``__hash__ = None``, so a frozen value type could not go in a set, be a
    dict key, or be passed to ``hash()``. Latent rather than live - nothing
    hashes a Verdict today - but it is a trap laid for the first caller who
    groups verdicts by identity, and closing it costs three lines.
    """

    def test_a_verdict_can_be_hashed_and_used_as_a_key(self):
        one = cs.Verdict("positive", cs.PASS, "reason", {"n": 1})
        same = cs.Verdict("positive", cs.PASS, "reason", {"n": 1})
        other = cs.Verdict("negative", cs.FAIL, "other", {})
        assert hash(one) == hash(same)
        assert len({one, same, other}) == 2
        assert {one: "kept"}[same] == "kept"

    def test_unhashable_metrics_do_not_break_the_hash(self):
        """``metrics`` holds nested dicts and lists, so it is deliberately not
        part of the hash. That stays consistent with ``__eq__``: verdicts that
        compare equal necessarily agree on the three fields that ARE hashed."""
        verdict = cs.Verdict("null", cs.INCONCLUSIVE, "why",
                             {"chain_hints": {"A": {"best": [1, 2]}}})
        assert isinstance(hash(verdict), int)
        # Different metrics: still UNEQUAL (that is what __eq__ says) but the
        # hashes collide, which is legal and is the price of not hashing a
        # nested structure. Both live in a set without either raising.
        thin = cs.Verdict("null", cs.INCONCLUSIVE, "why", {})
        assert verdict != thin
        assert hash(verdict) == hash(thin)
        assert len({verdict, thin}) == 2

    def test_hashing_did_not_reintroduce_the_string_comparison_hole(self):
        verdict = cs.Verdict("positive", cs.PASS, "reason")
        with pytest.raises(TypeError):
            verdict == cs.PASS
        with pytest.raises(TypeError):
            bool(verdict)


class TestFilterDivergenceFromRunPipeline:
    """``_polymer_ca_atoms`` and ``run_pipeline.pdb_ca_residues`` apply the
    SAME HETATM/MODRES rule - that lockstep is asserted elsewhere and is real -
    but the two filters are not identical overall: ``heavy_atoms`` also drops
    ``SOLVENT_RESNAMES`` on ``ATOM`` records, which ``pdb_ca_residues`` does
    not. A calcium written as an ATOM record is therefore a residue upstream
    and not one here.

    The direction is the safe one and that is the whole reason it is tolerable,
    so it is pinned rather than fixed: the canary is strictly STRICTER, so it
    can only decline to select a token upstream would have accepted, never
    propose one upstream would reject. An inversion of that direction - the
    canary seeing a residue upstream does not - would let it compute a patch
    upstream then silently drops, which is the failure the whole harness
    exists to catch.
    """

    PDB = "\n".join([
        _atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0),
        # A calcium ion written as an ATOM record rather than a HETATM. Legal,
        # and some minimisation pipelines emit exactly this.
        _atom(2, "CA", "CA", "A", 201, 10.0, 0.0, 0.0, element="CA"),
    ]) + "\n"

    def test_run_pipeline_counts_an_atom_record_calcium_and_the_canary_does_not(
            self, tmp_path):
        path = tmp_path / "ca.pdb"
        path.write_text(self.PDB)
        upstream, _ = rp.pdb_ca_residues(path)
        assert [(c, r) for c, r, _icode in upstream] == [("A", 1), ("A", 201)], (
            "run_pipeline has no solvent filter, so the ATOM-record calcium is "
            "a residue there")
        mine = sorted(cs.ca_positions(cs.heavy_atoms(self.PDB)))
        assert mine == [("A", 1)], (
            "heavy_atoms drops SOLVENT_RESNAMES on ATOM records too")

    def test_the_divergence_only_ever_runs_in_the_safe_direction(self, tmp_path):
        """Stricter, never looser: everything the canary calls a residue must
        be one upstream would also accept."""
        path = tmp_path / "ca.pdb"
        path.write_text(self.PDB)
        upstream = {(c, r) for c, r, _icode in rp.pdb_ca_residues(path)[0]}
        mine = set(cs.ca_positions(cs.heavy_atoms(self.PDB)))
        assert mine <= upstream, (
            "the canary sees a residue upstream does not - it could then "
            "select a hotspot upstream silently drops, which is the exact "
            "failure this harness exists to detect")
        assert mine != upstream, (
            "if these ever become equal the divergence is gone and this test "
            "should be deleted rather than left asserting nothing")

    def test_a_solvent_calcium_is_never_selectable_as_a_negative_patch(self):
        """What the divergence actually protects: pick_far_patch draws from the
        canary's residue set, so an ion can never become a hotspot token that
        ``missing_hotspots`` would then reject in-container."""
        tokens, _info = cs.pick_far_patch(LOBED_PDB, ["A1", "A2"])
        assert "A998" not in tokens and "A999" not in tokens


# ---------------------------------------------------------------------------
# 18. A PRINT KILLING THE RUN AFTER THE MONEY IS COMMITTED
#
# Reproduced live on 2026-08-04:
#
#     modal run tools/proteina/_hotspot_canary.py --phase 0
#     +- Error --------------------------------------------------------+
#     | 'charmap' codec can't encode character '✓' in position 0   |
#     +----------------------------------------------------------------+
#
# The character is not ours. Upstream's ``complexa target add`` prints
# "  <U+2713> Updated target '<key>'" and "  <U+1F4CD> Saved to: ...";
# ``run_shard`` runs that command INSIDE the container; modal streams container
# output to the LOCAL console; and writing either character to a Windows cp1252
# console raises ``UnicodeEncodeError``.
#
# WHY IT IS A MONEY DEFECT AND NOT A COSMETIC ONE. Phase 2 ``.spawn()``s THREE
# A100 shards and then waits, and ``complexa target add`` runs inside each one
# — i.e. every one of those characters is printed while the GPUs are already
# billing. Measured here against the unfixed code, with the tick arriving
# during the collect exactly as modal delivers it:
#
#     spawned 3   collected 0   cancelled 0   verdict FAIL   exit 1
#
# ~$12 of A100 time spent, all three results thrown away, and the feature
# CONDEMNED by a console codepage — the same shape as every other defect this
# file pins: a local crash after the money is gone. Interrupt anywhere else in
# that window and the entrypoint dies before ``_cancel_outstanding`` runs, and
# the three containers bill on to ``_MAX_SESSION_S`` = 7200 s (~$38).
#
# ``PYTHONIOENCODING=utf-8`` makes it go away. That is how the live run
# eventually succeeded and it is NOT the fix: it is a thing an operator forgets
# exactly once, and the once is the expensive one.
#
# THE FIX HAS TWO LAYERS AND BOTH ARE PINNED BELOW, because neither covers the
# other's ground:
#
#   (a) ``cs.harden_stream`` reconfigures sys.stdout/sys.stderr IN PLACE at
#       module import. Only this can reach a write made by code this repo does
#       not own — modal's log pump, rich's renderer, the interpreter's own
#       traceback printer — each of which holds its own reference to the stream
#       object, which is why it must be mutated rather than replaced.
#   (b) ``_emit`` sanitises and swallows at every print site in the harness.
#       Only this protects the strings the harness formats itself, including
#       its own prose: "PHASE 0 — a control did not pass" carries an em dash,
#       which is unencodable on cp437, and losing that print loses the verdict.
# ---------------------------------------------------------------------------


# What upstream's CLI actually prints, verbatim in shape.
UPSTREAM_TICK = "  ✓ Updated target 'hub_canary0123456789ab'"
UPSTREAM_PIN = "  \U0001f4cd Saved to: configs/targets/targets_dict.yaml"

# The harness's OWN prose, copied from _finish's phase-0 message. cp1252 can
# carry an em dash; cp437 — the other console codepage a Windows operator meets
# — cannot.
HARNESS_EM_DASH = "PHASE 0 — a control did not pass, see above"


def _console(encoding="cp1252"):
    """``(raw bytes sink, text stream)`` behaving like a Windows console.

    A real console differs from ``io.StringIO`` in the one way that matters:
    it has an ENCODING, so an unencodable character raises on write instead of
    being stored.
    """
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding=encoding, newline="")


def _unencodable_print(fragment=""):
    """An ``_emit`` stand-in that raises, optionally only on one message.

    Injected in place of the real one to prove the money path does not DEPEND
    on printing being safe — belt and braces are tested separately.
    """
    def _emit(message="", *, flush=False):
        if fragment in str(message):
            raise UnicodeEncodeError("charmap", "✓", 0, 1,
                                     "character maps to <undefined>")
    return _emit


class TestConsoleEncodingCannotKillTheRun:
    """Layer (a) and the pure helpers under it."""

    def test_the_upstream_line_kills_a_cp1252_console_until_it_is_hardened(self):
        """THE defect, reproduced, then fixed, in one test.

        The first half is the live failure: cp1252 has no code point for
        U+2713, so the write raises. The second half is the whole fix in one
        call — the same stream, the same string, no exception.
        """
        _raw, stream = _console()
        with pytest.raises(UnicodeEncodeError) as excinfo:
            print(UPSTREAM_TICK, file=stream)
        assert "charmap" in str(excinfo.value)

        hardened = cs.harden_stream(stream)
        print(UPSTREAM_TICK, file=hardened)
        print(UPSTREAM_PIN, file=hardened)
        hardened.flush()

    def test_hardening_is_in_place_so_modals_own_stream_reference_is_fixed(self):
        """WHY IT MUTATES RATHER THAN WRAPS.

        The write that killed the live run was made by modal, not by this repo.
        Modal's output manager, rich's Console and the traceback printer each
        capture ``sys.stdout`` when they start, so assigning a safe wrapper to
        ``sys.stdout`` afterwards leaves every one of them writing to the
        original object and still dying. Mutating the object's error handler
        fixes all of them at once — including holders that do not exist yet.
        """
        _raw, stream = _console()
        modals_reference = stream            # captured BEFORE we harden

        hardened = cs.harden_stream(stream)
        assert hardened is stream, (
            "the stream was replaced rather than reconfigured; a holder of the "
            "original — modal's log pump — would still be writing to a strict "
            "cp1252 stream and would still kill the entrypoint")
        assert stream.errors == cs.CONSOLE_ERRORS, (
            f"error handler is still {stream.errors!r}")
        # The proof, from the holder's side.
        modals_reference.write(UPSTREAM_TICK + "\n")

    def test_safe_text_makes_anything_encodable_and_touches_nothing_it_neednt(self):
        """The print-site sanitiser. Lossless where the console can cope."""
        rendered = cs.safe_text(UPSTREAM_TICK, "cp1252")
        rendered.encode("cp1252")            # the assertion: this must not raise
        assert "\\u2713" in rendered, (
            "backslashreplace keeps WHICH character failed; '?' would not")
        assert cs.safe_text(UPSTREAM_TICK, "utf-8") == UPSTREAM_TICK, (
            "a console that can carry the text must get it unmangled")
        assert cs.safe_text(HARNESS_EM_DASH, "cp437").encode("cp437")
        # An unknown codec, a non-string, and no encoding at all are all
        # survivable: this runs on the money path, so it may not raise either.
        assert cs.safe_text(UPSTREAM_TICK, "not-a-codec")
        assert cs.safe_text({"tick": UPSTREAM_TICK})
        cs.safe_text(UPSTREAM_TICK, None).encode("ascii")

    def test_a_stream_that_cannot_be_reconfigured_is_wrapped_instead(self):
        """The fallback, for a console someone else has already wrapped
        (colorama does exactly this on Windows) and left without
        ``reconfigure``."""

        class _NoReconfigure:
            encoding = "cp1252"

            def __init__(self):
                self.written = []

            def write(self, text):
                text.encode(self.encoding)   # what a real console does
                self.written.append(text)
                return len(text)

            def flush(self):
                pass

            def fileno(self):
                return 7

        sink = _NoReconfigure()
        hardened = cs.harden_stream(sink)
        hardened.write(UPSTREAM_TICK + "\n")
        assert sink.written, "the text never reached the underlying stream"
        # run_shard hands sys.stdout straight to subprocess.run, which needs a
        # real file descriptor, so the wrapper must be transparent.
        assert hardened.fileno() == 7
        assert hardened.encoding == "cp1252"
        # ...and something that cannot raise is not wrapped at all: a layer
        # between the caller and a real fd is a cost, not a safety measure.
        plain = io.StringIO()
        assert cs.harden_stream(plain) is plain
        assert cs.harden_stream(None) is None

    def test_the_module_hardens_both_streams_before_it_imports_modal(self):
        """ORDER IS THE POINT and only the source can show it.

        The hardening has to be in effect before anything that can print
        exists, ``import modal`` most of all — modal is what streams the
        container's output, so a console it can kill is a console it can kill
        from the first log line onwards.
        """
        tree = ast.parse(_CANARY_SOURCE, filename=str(_CANARY_PATH))
        hardened = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            call = node.value
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "harden_stream"):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "sys"):
                    hardened[target.attr] = node.lineno
        assert set(hardened) == {"stdout", "stderr"}, (
            f"{_CANARY_PATH.name} hardens {sorted(hardened)} at module scope; "
            "both streams must be hardened — a traceback goes to stderr")
        modal_imports = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            and any(alias.name == "modal" for alias in node.names)
        ]
        assert modal_imports, (
            "modal is no longer imported here, so this ordering test is "
            "checking nothing — delete it or point it at what replaced it")
        assert max(hardened.values()) < min(modal_imports), (
            "modal is imported before the console is hardened")

    def test_no_bare_print_survives_anywhere_in_the_harness(self):
        """Every print goes through ``_emit``.

        An allowlist of one, because the failure mode is a new print added
        later on a path nobody thought about — which is exactly how the phase-2
        tail came to be able to kill a completed $12 run.
        """
        tree = ast.parse(_CANARY_SOURCE, filename=str(_CANARY_PATH))
        prints = [node.lineno for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id == "print"]
        assert not prints, (
            f"{_CANARY_PATH.name} calls print() at lines {prints}; a raw print "
            "can raise UnicodeEncodeError on a cp1252 console, and every one of "
            "these runs after money has been committed")
        emits = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_emit"]
        assert len(emits) > 20, (
            "the harness has stopped reporting anything, which passes this "
            "test for the wrong reason")

    def test_the_real_module_body_survives_a_cp1252_console(self):
        """END TO END on the REAL file, which no other test here can be.

        Everything else lifts function bodies; this executes the module scope
        that does the hardening, in a child interpreter whose stdout really is
        cp1252 (PYTHONIOENCODING), and then prints upstream's two characters at
        it. ``modal`` is stubbed in ``sys.modules`` before the import, so
        nothing here builds an Image, contacts Modal or needs the package.
        """
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        proc = subprocess.run(
            [sys.executable, "-c", _CP1252_IMPORT_PROBE, str(_CANARY_PATH)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=180)
        assert proc.returncode == 0, (
            "importing the canary and printing upstream's line to a cp1252 "
            f"console killed the interpreter:\n{proc.stderr[-2000:]}")
        assert "EXIT-OK" in proc.stdout, proc.stdout
        assert f"errors: {cs.CONSOLE_ERRORS}" in proc.stdout, (
            "module import did not reconfigure the child's stdout: "
            f"{proc.stdout!r}")
        assert "UnicodeEncodeError" not in proc.stderr, proc.stderr


# Runs in a child interpreter, never here: `modal` is replaced by a stub BEFORE
# the canary is loaded, so the real package is never imported and no Image, App
# or Volume is ever constructed.
_CP1252_IMPORT_PROBE = r'''
import importlib.util, sys, types

stub = types.ModuleType("modal")


class _Image:
    @staticmethod
    def from_dockerfile(*a, **k):
        return _Image()

    def add_local_file(self, *a, **k):
        return self


class _Volume:
    @staticmethod
    def from_name(*a, **k):
        return _Volume()


class _App:
    def __init__(self, *a, **k):
        pass

    def function(self, *a, **k):
        return lambda fn: fn

    def local_entrypoint(self, *a, **k):
        return lambda fn: fn


stub.Image, stub.Volume, stub.App = _Image, _Volume, _App
sys.modules["modal"] = stub

spec = importlib.util.spec_from_file_location("_hotspot_canary_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["_hotspot_canary_probe"] = module
spec.loader.exec_module(module)

print("encoding:", sys.stdout.encoding, "errors:", sys.stdout.errors)
print("  ✓ Updated target 'hub_canary0123456789ab'")
print("  \U0001f4cd Saved to: configs/targets/targets_dict.yaml")
sys.stderr.write("  ✓ on stderr\n")
sys.stdout.flush()
print("EXIT-OK")
'''


class TestTheConsoleCannotCostMoney:
    """Layer (b): the money path, driven with a console that cannot take the
    text the container is sending it."""

    @staticmethod
    def _target(tmp_path):
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        return target

    def test_a_print_that_raises_cannot_stop_the_other_shards_being_cancelled(
            self):
        """(b) PROVED INDEPENDENTLY OF (a).

        ``_emit`` is replaced by one that always raises, which is the worst
        case the fix is allowed to face: every cancel must still have been
        attempted. This is why the canceller cancels everything FIRST and
        reports afterwards — with the report interleaved, the first shard's
        line kills the loop and the other two A100s bill on.
        """
        namespace = load_canary_functions({"_cancel_outstanding"},
                                          _emit=_unencodable_print())
        running = [_Handle(), _Handle(), _Handle()]
        with pytest.raises(UnicodeEncodeError):
            namespace["_cancel_outstanding"](
                list(zip(("positive", "negative", "null"), running)), set())
        assert [h.cancels for h in running] == [[True], [True], [True]], (
            "a print that raised left a shard running; the containers must be "
            "terminated before anything is written to the console")

    def test_a_print_that_raises_in_the_interrupt_handler_still_cancels(
            self, tmp_path):
        """The same guarantee one level up.

        The handler's own notice — "interrupted — cancelling any shard still
        running" — is a console write, and what brought us into the handler may
        BE a console write. It sits in a ``try`` whose ``finally`` does the
        cancelling, so failing to print the word cannot cost three A100s.
        """
        handles = [_Handle(raises=KeyboardInterrupt()), _Handle(), _Handle()]
        shard = _Remote(handles=list(handles))
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, run_shard=shard,
            phase0=_Remote(result={}),
            _emit=_unencodable_print("interrupted"))
        with pytest.raises(UnicodeEncodeError):
            namespace["main"](phase=2, target_pdb=str(self._target(tmp_path)),
                              hotspots="A1 A2", negative="A50 A51 A52 A53")
        assert len(shard.spawn_calls) == 3
        for handle in handles:
            assert handle.cancels == [True], (
                "the interrupt notice failed to print and the shards were "
                "abandoned — ~$12 of A100 time billing with nobody reading it")

    def test_a_non_ascii_cancel_failure_does_not_abandon_the_other_shards(
            self, monkeypatch):
        """The realistic version of the test above: a real console, and a gRPC
        error message quoting the container line that started all this."""
        _raw, stream = _console()
        monkeypatch.setattr(sys, "stdout", stream)
        namespace = load_canary_functions({"_cancel_outstanding"})
        angry = _Handle(cancel_raises=RuntimeError(
            f"cancel refused: {UPSTREAM_TICK}"))
        calm_a, calm_b = _Handle(), _Handle()
        namespace["_cancel_outstanding"](
            [("positive", angry), ("negative", calm_a), ("null", calm_b)],
            set())
        assert [h.cancels for h in (angry, calm_a, calm_b)] == [
            [True], [True], [True]]

    def test_the_container_tick_during_collect_does_not_discard_three_shards(
            self, monkeypatch, tmp_path):
        """THE $12 CASE, end to end.

        ``complexa target add`` runs inside every shard, so its tick reaches
        the local console while all three A100s are billing — modelled here by
        a handle that prints upstream's two lines when ``get`` is called, which
        is where modal delivers them. The console is a genuine cp1252 stream
        and the module-import hardening is applied to it exactly as
        ``_hotspot_canary`` applies it (the lifted bodies never run module
        scope).

        Against the unfixed code the write raises inside ``handle.get``, the
        collect loop's ``except Exception`` turns each shard into an error
        record, all three RESULTS ARE THROWN AWAY, and phase 2 reports FAIL:
        ~$12 spent to condemn a feature over a codepage.
        """
        _raw, stream = _console()
        monkeypatch.setattr(sys, "stdout", cs.harden_stream(stream))

        class _TickingHandle(_Handle):
            def get(self, timeout=None):
                print(UPSTREAM_TICK)     # modal's log pump, not our code
                print(UPSTREAM_PIN)
                return super().get(timeout=timeout)

        results = [
            _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0),
            _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0),
            _shard("null", n=8, recall=None, centroid=1.0, cross=0.0),
        ]
        handles = [_TickingHandle(result=r) for r in results]
        shard = _Remote(handles=list(handles))
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, run_shard=shard,
            phase0=_Remote(result={}))
        namespace["main"](phase=2, target_pdb=str(self._target(tmp_path)),
                          hotspots="A1 A2", negative="A50 A51 A52 A53")
        assert [h.gets for h in handles] == [1, 1, 1], (
            "a shard was billed for and its result never read")
        assert [h.cancels for h in handles] == [[], [], []], (
            "a collected shard must not be cancelled")

    def test_the_phase_two_tail_reports_a_verdict_when_an_error_carries_the_tick(
            self, monkeypatch, tmp_path):
        """The money is already spent here, so what is at stake is the ANSWER.

        A shard whose error string quotes the container's output is printed by
        the phase-2 tail with an f-string, not through ``json.dumps`` — nothing
        escapes it to ASCII — so the tail raised and the run ended with no
        verdict and no exit code after ~$12.
        """
        _raw, stream = _console()
        monkeypatch.setattr(sys, "stdout", stream)
        results = [
            {"label": "positive", "error": f"the container said: {UPSTREAM_TICK}"},
            _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0),
            _shard("null", n=8, recall=None, centroid=1.0, cross=0.0),
        ]
        shard = _Remote(handles=[_Handle(result=r) for r in results])
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, run_shard=shard,
            phase0=_Remote(result={}))
        with pytest.raises(SystemExit) as excinfo:
            namespace["main"](phase=2, target_pdb=str(self._target(tmp_path)),
                              hotspots="A1 A2", negative="A50 A51 A52 A53")
        assert excinfo.value.code in {cs.EXIT_CODES[cs.FAIL],
                                      cs.EXIT_CODES[cs.INCONCLUSIVE]}, (
            "phase 2 must still reach a verdict and an exit code")
        stream.flush()
        assert b"\\u2713" in _raw.getvalue(), (
            "the line was dropped rather than degraded; the operator needs to "
            "see WHAT the container said")

    def test_the_phase_one_tail_survives_a_non_ascii_unscorable_reason(
            self, monkeypatch, tmp_path):
        """Phase 1's per-design block prints ``unscorable_reason`` and the chain
        map, both built in the container from container-side text."""
        _raw, stream = _console()
        monkeypatch.setattr(sys, "stdout", stream)
        res = {
            "label": "phase1", "exit_code": 0, "n_complexes": 1,
            "n_target_verified": 0, "n_target_unverified": 1,
            "peak_vram_mb": 41000, "runtime_s": 900,
            "hydra": {"task_name_selected": True, "hotspots_match": True,
                      "hotspots_order_matches": True},
            "designs": [{
                "name": "sample_0.pdb", "chains": ["A", "B"],
                "is_complex": True, "target_verified": False, "contacts": 12,
                "unscorable_reason": f"upstream reported {UPSTREAM_TICK}",
                "target_identity": {"chain_hints": {
                    "A": {"n_residues": 30,
                          "best_match": {"reference_chain": "B"}}}},
            }],
        }
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, run_shard=_Remote(result=res),
            phase0=_Remote(result={}))
        namespace["main"](phase=1, target_pdb=str(self._target(tmp_path)),
                          hotspots="A1 A2")
        stream.flush()
        assert b"UNSCORABLE" in _raw.getvalue(), (
            "the diagnostic that tells the operator phase 2 would waste ~$12 "
            "never printed")

    def test_the_harness_own_em_dash_does_not_cost_the_verdict_on_cp437(
            self, monkeypatch):
        """NOT EVERY UNENCODABLE CHARACTER COMES FROM UPSTREAM.

        cp437 is the other codepage a Windows console turns up in, and it has
        no em dash — which the harness's own verdict messages are full of. The
        exit code IS the verdict for anything scripted around this harness, so
        a print that raises inside ``_finish`` does not merely lose a line, it
        loses the answer and exits non-deterministically.
        """
        assert "—" in _CANARY_SOURCE, (
            "the harness no longer uses an em dash; this test is checking "
            "nothing and should be repointed at whatever replaced it")
        with pytest.raises(UnicodeEncodeError):
            HARNESS_EM_DASH.encode("cp437")

        _raw, stream = _console("cp437")
        monkeypatch.setattr(sys, "stdout", stream)
        namespace = load_canary_functions({"_finish"})
        with pytest.raises(SystemExit) as excinfo:
            namespace["_finish"](cs.FAIL, HARNESS_EM_DASH)
        assert excinfo.value.code == cs.EXIT_CODES[cs.FAIL]
        # ...and PASS still exits 0 rather than dying on the way out.
        namespace["_finish"](cs.PASS, HARNESS_EM_DASH)


# ---------------------------------------------------------------------------
# 16. FAILURE DIAGNOSTICS — the text upstream wrote and the operator never saw
#
# TWO LIVE PHASE-1 RUNS FAILED ON 2026-08-05 AND TAUGHT US NOTHING. Both came
# back exit_code 1, runtime 49 s, peak_vram 4 MB, n_complexes 0, tree [],
# csv_files {}, hydra null, every median null — and a verdict reading "the
# phase 1 shard did not complete: the design command exited 1", which is the
# input restated as the answer.
#
# ``complexa design`` runs ``generate`` as a SUB-subprocess with its output
# redirected into ``logs/design_pipeline_*/generate.log``, so its stderr never
# reaches ``run_shard``'s stream and Modal forwards nothing else. Meanwhile
# every collector in the harness — designs, tree, csv, hydra — globbed under
# ``inference/``, which a run that dies in ``generate`` never fills. So
# ``tree: []`` was literally true and completely uninformative: the harness
# looked only where a SUCCESSFUL run puts its outputs and nowhere a failing one
# puts its reasons.
#
# EVERY TEST BELOW IS BEHAVIOURAL. Each one either executes ``_canary_scoring``'s
# pure functions directly, or executes the REAL lifted bodies of ``run_shard`` /
# ``main`` against a real temporary log tree, and asserts on the TEXT that came
# back or reached the console. Not one of them can be satisfied by a call node,
# by a log message or by the shape of the source — earlier rounds of this
# harness shipped exactly those and shipped them green.
# ---------------------------------------------------------------------------


# What upstream's generate.log actually ends with when it dies. The tick is not
# decoration: the log is upstream-authored text, U+2713 is the character that
# killed a run on a cp1252 console on 2026-08-04, and this change carries that
# text out of the container and onto the operator's terminal for the first time.
GENERATE_TRACEBACK = (
    "[2026-08-05 00:58:47] Loading checkpoint ckpts/proteina.ckpt\n"
    "  ✓ target hub_canary207abdf0a915 resolved\n"
    "Traceback (most recent call last):\n"
    '  File "proteinfoundation/generate.py", line 431, in main\n'
    "    target = load_target_from_pdb(cfg.generation.task_name)\n"
    "RuntimeError: THE ACTUAL REASON THE RUN DIED\n"
)
PIPELINE_LOG_TEXT = "design_pipeline: generate failed with exit code 1\n"


def _write_run_logs(work_dir, stamp, generate=GENERATE_TRACEBACK,
                    sibling=PIPELINE_LOG_TEXT, mtime=None, **stage_logs):
    """Upstream's log layout for one run, exactly as its own message names it:
    ``logs/design_pipeline_<key>_<run>_<stamp>/generate.log`` plus the sibling
    ``logs/design_pipeline_<key>_<run>_<stamp>.log`` next to it."""
    logs = Path(work_dir) / "logs"
    run_dir = logs / f"design_pipeline_hub_canaryabc_canary_phase1_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    if generate is not None:
        (run_dir / "generate.log").write_bytes(
            generate if isinstance(generate, bytes) else generate.encode("utf-8"))
    for name, text in stage_logs.items():
        (run_dir / f"{name}.log").write_text(text, encoding="utf-8")
    sibling_path = logs / f"{run_dir.name}.log"
    if sibling is not None:
        sibling_path.write_text(sibling, encoding="utf-8")
    if mtime is not None:
        for path in (run_dir, sibling_path):
            if path.exists():
                os.utime(path, (mtime, mtime))
    return run_dir


def _blob(report):
    """Every byte of tail text a report carries, as one string."""
    return "\n".join(str(e.get("tail") or "")
                     for e in (report or {}).get("files") or [])


def _diagnostics(out):
    """The shard's diagnostics block, or a NAMED failure.

    ``out["log_diagnostics"]`` would raise KeyError when the collection is
    gone, and a bare KeyError is an incidental-looking failure: it says a key
    is missing, not that the shard came back from a failed $4 run carrying no
    explanation. Asserting here makes every caller below fail for the reason
    it is actually testing.
    """
    report = out.get("log_diagnostics")
    assert isinstance(report, dict), (
        "the shard returned no log_diagnostics at all — this is the state the "
        "two live runs came back in, where the exit code was the only "
        f"evidence. The shard's keys were: {sorted(out)}")
    return report


def _tree(out):
    """The shard's file listing, or a NAMED failure. Same reasoning."""
    tree = out.get("tree")
    assert isinstance(tree, list), (
        "the shard returned no file listing at all, so there is no file-level "
        f"evidence of what upstream produced. Keys: {sorted(out)}")
    return tree


class TestTheFailureDiagnosticsLeaveTheContainer:
    """The REAL ``run_shard`` body, against a real log tree on disk."""

    def test_a_failing_shard_returns_the_log_its_stream_never_carried(
            self, tmp_path):
        """THE defect, reproduced and closed in one test.

        Against the unfixed collector the shard returns exit_code 1 and nothing
        else — no key, no path, no text — which is exactly what two live runs
        returned. The assertion is on the REASON STRING inside upstream's log,
        so it cannot be satisfied by a key existing or by a call node.
        """
        namespace = _shard_namespace(tmp_path, rc=1)
        _write_run_logs(tmp_path / "proteina", "Y2026_M08_D05_H00_M58_S46")
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1", "A2"], "", 1234, [60, 120],
            True, ["A1", "A2"])
        report = _diagnostics(out)
        assert "RuntimeError: THE ACTUAL REASON THE RUN DIED" in _blob(report), (
            "the log was globbed but its text never came back")
        # ...and the paths it resolved, so a FUTURE failure to find the log is
        # itself diagnosable instead of another silent empty result.
        assert report["globs"], "the report does not say where it looked"
        assert any("design_pipeline" in g for g in report["globs"])
        assert report["n_matched"] >= 1 and report["selected"]
        assert any(e["path"].endswith("generate.log") for e in report["files"])

    def test_the_sibling_pipeline_log_comes_back_too(self, tmp_path):
        """Upstream's error names TWO files — "Check log for details" points at
        the sibling, not at generate.log. Reading one of them leaves the
        operator to guess which."""
        namespace = _shard_namespace(tmp_path, rc=1)
        _write_run_logs(tmp_path / "proteina", "Y2026_M08_D05_H00_M58_S46")
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1"], "", 1234, [60, 120], True, ["A1"])
        assert "generate failed with exit code 1" in _blob(
            _diagnostics(out))

    def test_a_clean_exit_with_no_complexes_is_collected_too(self, tmp_path):
        """THE SECOND BLIND CASE, and the one that costs a re-run.

        rc == 0 with no design output: every median is None and the verdict is
        a polite INCONCLUSIVE telling the operator to go and read a run tree
        that does not exist. Collecting only on a non-zero exit leaves this
        case exactly as uninformative as it was.
        """
        namespace = _shard_namespace(tmp_path, rc=0)
        _write_run_logs(tmp_path / "proteina", "Y2026_M08_D05_H01_M00_S00")
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1"], "", 1234, [60, 120], True, ["A1"])
        assert out["exit_code"] == 0 and out["n_complexes"] == 0
        assert "RuntimeError: THE ACTUAL REASON THE RUN DIED" in _blob(
            out.get("log_diagnostics")), (
            "a command that exits 0 and produces nothing is just as blind as "
            "one that exits 1, and got no diagnostics")

    def test_a_successful_shard_pays_nothing_for_diagnostics(self, tmp_path):
        """The other direction, or the fix is merely 'always collect'. A shard
        that produced a scorable complex has a verdict made of numbers; 24 KB
        of upstream log appended to it is cost with no answer in it."""
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)], rc=0)
        _write_run_logs(tmp_path / "proteina", "Y2026_M08_D05_H01_M00_S00")
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        assert out["n_complexes"] == 1
        assert "log_diagnostics" not in out
        assert "tree" not in out, (
            "a healthy phase-2 shard asked for no listing and must not be "
            "charged for one")

    def test_the_newest_run_directory_is_the_one_read(self, tmp_path):
        """Modal reuses warm containers, so several ``design_pipeline_*``
        directories accumulate and ``glob`` returns them in arbitrary order.
        A PREVIOUS run's log presented as this one's is worse than no log: it
        is confidently wrong and nothing in it says so."""
        home = tmp_path / "proteina"
        _write_run_logs(home, "Y2026_M08_D01_H00_M00_S00",
                        generate="STALE: a run from four days ago\n",
                        sibling="stale sibling\n", mtime=1_000_000)
        _write_run_logs(home, "Y2026_M08_D05_H00_M58_S46", mtime=2_000_000)
        namespace = _shard_namespace(tmp_path, rc=1)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1"], "", 1234, [60, 120], True, ["A1"])
        report = _diagnostics(out)
        blob = _blob(report)
        assert "RuntimeError: THE ACTUAL REASON THE RUN DIED" in blob
        assert "STALE" not in blob, (
            "a previous run's log came back as this run's diagnosis")
        assert "D05" in str(report["selected"])
        # Both directories are still REPORTED, so a selection that went wrong
        # is visible rather than invisible.
        assert report["n_matched"] >= 4

    def test_the_sibling_read_is_the_selected_runs_not_the_newest_file(
            self, tmp_path):
        """Upstream writes ``<rundir>.log`` beside ``<rundir>/``, so the right
        sibling is knowable exactly. Choosing it by mtime instead pairs THIS
        run's generate.log with a PREVIOUS run's pipeline log the moment the
        two disagree by a second — the same stale-evidence failure the
        newest-directory selection exists to prevent, one line later.

        Here the stale run's sibling is the newest FILE in ``logs/`` while the
        fresh run's DIRECTORY is the newest directory.
        """
        home = tmp_path / "proteina"
        stale = _write_run_logs(home, "Y2026_M08_D01_H00_M00_S00",
                                generate="STALE generate\n",
                                sibling="STALE SIBLING\n", mtime=1_000_000)
        _write_run_logs(home, "Y2026_M08_D05_H00_M58_S46", mtime=2_000_000)
        os.utime(home / "logs" / f"{stale.name}.log", (9_000_000, 9_000_000))
        namespace = _shard_namespace(tmp_path, rc=1)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1"], "", 1234, [60, 120], True, ["A1"])
        blob = _blob(_diagnostics(out))
        assert "generate failed with exit code 1" in blob
        assert "STALE SIBLING" not in blob, (
            "the pipeline log came from a different run than the generate.log "
            "beside it")

    def test_a_missing_generate_log_is_reported_not_silently_absent(
            self, tmp_path):
        """"We looked at logs/.../generate.log and it was not there" is the
        finding when upstream moves its layout. An absent entry is
        indistinguishable from an absent collector, which is the non-answer
        this whole section exists to end."""
        namespace = _shard_namespace(tmp_path, rc=1)
        _write_run_logs(tmp_path / "proteina", "Y2026_M08_D05_H00_M58_S46",
                        generate=None)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1"], "", 1234, [60, 120], True, ["A1"])
        entries = {e["path"]: e for e in _diagnostics(out)["files"]}
        missing = [e for p, e in entries.items() if p.endswith("generate.log")]
        assert missing and "FileNotFoundError" in missing[0]["error"], (
            f"a missing generate.log left no trace at all: {entries}")

    def test_when_no_path_matches_the_report_still_says_where_it_looked(
            self, tmp_path):
        """No ``logs/`` directory at all. The report must name the globs and
        say plainly that nothing matched, or the next failure reproduces
        ``tree: []`` — an empty result with no way to tell a wrong pattern from
        an empty directory."""
        namespace = _shard_namespace(tmp_path, rc=1)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1"], "", 1234, [60, 120], True, ["A1"])
        report = _diagnostics(out)
        assert report["n_matched"] == 0 and report["files"] == []
        assert report["globs"], "an empty result with no record of the pattern"
        assert "no path matched" in report["note"]

    def test_an_empty_log_is_distinguishable_from_a_missing_one(self, tmp_path):
        """Different next actions: an empty generate.log means the subprocess
        died before writing anything (exec failure, OOM kill); a missing one
        means we are globbing the wrong place."""
        namespace = _shard_namespace(tmp_path, rc=1)
        _write_run_logs(tmp_path / "proteina", "Y2026_M08_D05_H00_M58_S46",
                        generate=b"")
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1"], "", 1234, [60, 120], True, ["A1"])
        entry = [e for e in _diagnostics(out)["files"]
                 if e["path"].endswith("generate.log")][0]
        assert entry.get("empty") is True and "error" not in entry
        assert entry["bytes"] == 0

    def test_the_collector_can_never_raise(self, tmp_path):
        """It runs AFTER the GPU time is spent, so a collector that raises
        converts an informative failure into an uninformative one — the exact
        trade this change exists to reverse.

        Driven on ``_collect_run_logs`` directly rather than through
        ``run_shard``: the guard is inside the collector, and breaking ``glob``
        for the whole lifted namespace would take out the per-design scan
        first and test the wrong thing.
        """
        def _boom(*a, **k):
            raise OSError("the volume went away")

        namespace = load_canary_functions(
            {"_collect_run_logs", "_mtime"},
            glob=types.SimpleNamespace(glob=_boom))
        report = namespace["_collect_run_logs"](tmp_path)
        assert "OSError" in report["error"]
        assert cs.format_log_diagnostics({"label": "x",
                                          "log_diagnostics": report})


class TestTheLogTailIsCapped:
    """A tail that is not capped puts a megabyte of upstream log into the return
    payload and onto the console — which is already the most fragile part of
    this harness."""

    def test_the_tail_keeps_the_END_and_the_boundary_is_exact(self):
        """The end, because the traceback is the last thing in the file and a
        head-truncated log is 6 KB of CUDA banner. Both sides of the boundary
        are pinned so ``>`` cannot drift to ``>=`` unnoticed."""
        exact = cs.truncate_log_tail(b"x" * 64, 64)
        assert exact["truncated"] is False and exact["kept_bytes"] == 64
        over = cs.truncate_log_tail(b"HEAD" + b"x" * 61, 61)
        assert over["truncated"] is True
        assert over["tail"] == "x" * 61, "the head was kept instead of the tail"
        assert over["kept_bytes"] == 61 and over["bytes"] == 65

    def test_a_cut_through_a_multibyte_character_does_not_raise(self):
        """The slice is on BYTES, so a cut mid-character is not merely possible
        but likely — upstream's own log carries a tick."""
        raw = ("✓" * 10).encode("utf-8")          # 30 bytes, 3 per char
        out = cs.truncate_log_tail(raw, 8)
        assert out["kept_bytes"] == 8
        out["tail"].encode("utf-8")                    # the assertion: no raise

    def test_the_default_per_file_cap_is_in_the_4_to_8_kb_band(self):
        assert 4096 <= cs.LOG_TAIL_BYTES <= 8192
        assert cs.LOG_TOTAL_BYTES >= cs.LOG_TAIL_BYTES

    def test_the_total_budget_stops_the_files_adding_up(self):
        """The per-file cap alone multiplies by however many logs exist: five
        stage logs at 6 KB each is 30 KB, which is not a cap."""
        files = [(f"/w/{i}.log", b"z" * 10_000) for i in range(5)]
        entries = cs.collect_log_entries(files, per_file_bytes=100,
                                         total_bytes=250)
        assert sum(e["kept_bytes"] for e in entries) <= 250
        assert [e["kept_bytes"] for e in entries] == [100, 100, 50, 0, 0]

    def test_a_file_dropped_for_budget_says_so_rather_than_looking_empty(self):
        """"This log was empty" and "this log was cut to stay inside the cap"
        send the operator in different directions."""
        entries = cs.collect_log_entries(
            [("/w/a.log", b"a" * 100), ("/w/b.log", b"b" * 100)],
            per_file_bytes=100, total_bytes=100)
        assert entries[1]["kept_bytes"] == 0
        assert entries[1].get("budget_exhausted") is True
        assert entries[1].get("empty") is not True
        assert entries[1]["bytes"] == 100, (
            "the real size must survive the trim, or a dropped log reads as a "
            "log that was never written")

    def test_a_read_that_failed_is_recorded_not_dropped(self):
        entries = cs.collect_log_entries(
            [("/w/gone.log", FileNotFoundError(2, "No such file"))])
        assert len(entries) == 1, (
            "the failed read was dropped instead of recorded; an absent entry "
            "is indistinguishable from an absent collector")
        assert entries[0]["path"] == "/w/gone.log"
        assert "FileNotFoundError" in entries[0]["error"]

    def test_the_real_shard_stays_inside_the_cap_on_a_huge_log(self, tmp_path):
        """END TO END, because a cap is only worth something if the shard
        applies it."""
        namespace = _shard_namespace(tmp_path, rc=1)
        _write_run_logs(
            tmp_path / "proteina", "Y2026_M08_D05_H00_M58_S46",
            generate=b"NOISE\n" * 200_000 + b"LAST LINE OF THE LOG\n",
            filter="f" * 100_000, evaluate="e" * 100_000,
            analyze="a" * 100_000)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1"], "", 1234, [60, 120], True, ["A1"])
        report = _diagnostics(out)
        assert report["total_tail_bytes"] <= cs.LOG_TOTAL_BYTES
        assert "LAST LINE OF THE LOG" in _blob(report), (
            "the cap kept the wrong end of the file")


class TestNewestPathSelection:
    def test_the_newest_wins_and_an_empty_set_is_None(self):
        assert cs.newest_path([("/a", 1.0), ("/b", 9.0), ("/c", 5.0)]) == "/b"
        assert cs.newest_path([]) is None

    def test_a_tie_breaks_on_the_timestamped_name_not_on_glob_order(self):
        """Upstream stamps the directory ``..._Y2026_M08_D05_H00_M58_S46``, so
        on an mtime tie the lexicographically greater name is the later run.
        Glob order is not an ordering."""
        pairs = [("/l/design_pipeline_x_Y2026_M08_D05_H00_M58_S46", 7.0),
                 ("/l/design_pipeline_x_Y2026_M08_D05_H00_M58_S01", 7.0)]
        assert cs.newest_path(pairs).endswith("S46")
        assert cs.newest_path(list(reversed(pairs))).endswith("S46")

    def test_an_unorderable_mtime_is_skipped_rather_than_raising(self):
        """This runs after the money is spent; comparing a str against a float
        raises TypeError and would lose the diagnostics entirely."""
        assert cs.newest_path([("/a", "not-a-time"), ("/b", 3.0)]) == "/b"


class TestTheTreeCoversTheWholeWorkDirectory:
    """``tree: []`` was the entire file-level evidence of two failed runs, and
    it was empty because the glob was scoped to ``inference/`` — a directory a
    run that dies in ``generate`` never fills."""

    def test_the_listing_shows_what_upstream_wrote_outside_inference(
            self, tmp_path):
        namespace = _shard_namespace(tmp_path, rc=1)
        _write_run_logs(tmp_path / "proteina", "Y2026_M08_D05_H00_M58_S46")
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "phase1", ["A1"], "", 1234, [60, 120], True, ["A1"])
        tree = _tree(out)
        assert any(p.endswith("generate.log") for p in tree), (
            "the listing is still blind to everything upstream wrote outside "
            f"inference/: {tree}")
        assert any(p.startswith("logs/") for p in tree)

    def test_a_failing_phase2_shard_gets_a_listing_it_did_not_ask_for(
            self, tmp_path):
        """Phase 2 spawns with dump_tree=False, so gating the listing on that
        flag alone leaves a failing $12 shard with no file-level evidence."""
        namespace = _shard_namespace(tmp_path, rc=1)
        _write_run_logs(tmp_path / "proteina", "Y2026_M08_D05_H00_M58_S46")
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "null", [], "", 1234, [60, 120], False, ["A1"])
        assert any(p.endswith("generate.log") for p in _tree(out))

    def test_the_mounted_volumes_are_excluded_from_the_listing(self):
        """``ckpts`` and ``rewards`` are seeded INPUTS and enormous; listing
        them spends the whole cap on files the operator already knows about."""
        assert cs.select_tree_entries([
            "ckpts/proteina.ckpt", "rewards/af2/params.npz",
            "logs/design_pipeline_x/generate.log", "inference/sample_0.pdb",
        ]) == ["logs/design_pipeline_x/generate.log", "inference/sample_0.pdb"]

    def test_the_cap_cannot_evict_the_two_subtrees_the_listing_is_for(self):
        """Plain ``sorted(...)[:limit]`` is alphabetical, and ``assets`` and
        ``configs`` sort ahead of ``inference`` and ``logs`` — so a work
        directory holding a few hundred config files returns a cap's worth of
        files nobody asked about and none of the ones they did."""
        noise = [f"configs/c{i:03d}.yaml" for i in range(50)]
        noise += [f"assets/a{i:03d}.pdb" for i in range(50)]
        assert cs.select_tree_entries(
            noise + ["logs/design_pipeline_x/generate.log",
                     "inference/sample_0.pdb"],
            limit=2) == ["logs/design_pipeline_x/generate.log",
                         "inference/sample_0.pdb"]

    def test_the_listing_is_deduplicated_and_separator_normalised(self):
        """The globs overlap on purpose (``*`` and ``*/*`` both reach the top
        level), and these tests do not run on the container's OS."""
        assert cs.select_tree_entries(
            ["logs/x", "logs\\x", "./logs/x"]) == ["logs/x"]
        assert cs.select_tree_entries([".hydra/config.yaml"]) == [
            ".hydra/config.yaml"], (
            "a leading dot is not a './' prefix — .hydra/config.yaml is the "
            "single most useful entry in the whole listing")


class TestShouldCollectLogs:
    def test_both_blind_cases_collect_and_a_good_run_does_not(self):
        assert cs.should_collect_logs(1, 8) is True
        assert cs.should_collect_logs(124, 0) is True
        assert cs.should_collect_logs(0, 0) is True
        assert cs.should_collect_logs(0, 8) is False

    def test_a_result_we_cannot_read_counts_as_blind(self):
        """Guessing "it succeeded" costs the diagnostics exactly when the shard
        is most broken."""
        assert cs.should_collect_logs(None, 8) is True
        assert cs.should_collect_logs(0, None) is True
        assert cs.should_collect_logs("nan", 8) is True


class TestTheDiagnosticsReachTheOperator:
    """Collecting the log into a returned dict is worth nothing if no tail
    prints it — which is the shape of every defect this file was rebuilt to
    catch: the work is done and the answer reaches nobody."""

    @staticmethod
    def _target(tmp_path):
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        return target

    @staticmethod
    def _failed_shard(label):
        return {
            "label": label, "exit_code": 1, "runtime_s": 49, "peak_vram_mb": 4,
            "n_complexes": 0, "n_target_verified": 0, "n_target_unverified": 0,
            "designs": [], "tree": ["logs/design_pipeline_x/generate.log"],
            "hydra": None, "cross_reference_hotspots": ["A1", "A2"],
            "log_diagnostics": cs.build_log_report(
                globs=["/opt/proteina/logs/design_pipeline_*"],
                matched=["/opt/proteina/logs/design_pipeline_x"],
                selected="/opt/proteina/logs/design_pipeline_x",
                files=[("/opt/proteina/logs/design_pipeline_x/generate.log",
                        GENERATE_TRACEBACK.encode("utf-8"))]),
        }

    def _run_main(self, monkeypatch, tmp_path, phase_kwargs, **injected):
        _raw, stream = _console("utf-8")
        monkeypatch.setattr(sys, "stdout", cs.harden_stream(stream))
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, phase0=_Remote(result={}), **injected)
        try:
            namespace["main"](target_pdb=str(self._target(tmp_path)),
                              hotspots="A1 A2", **phase_kwargs)
        except SystemExit:
            pass                       # the verdict; the OUTPUT is under test
        stream.flush()
        return _raw.getvalue().decode("utf-8")

    def test_the_phase_one_tail_prints_the_upstream_log(
            self, monkeypatch, tmp_path):
        """The whole point of collecting it. Against the unfixed tail the
        operator sees exit_code 1 and a verdict that restates it."""
        printed = self._run_main(
            monkeypatch, tmp_path, {"phase": 1},
            run_shard=_Remote(result=self._failed_shard("phase1")))
        assert "RuntimeError: THE ACTUAL REASON THE RUN DIED" in printed, (
            "phase 1 collected the log and never showed it to anyone")
        assert "/opt/proteina/logs/design_pipeline_x/generate.log" in printed, (
            "the resolved path is part of the diagnosis")

    def test_the_log_arrives_as_readable_lines_not_an_escaped_json_string(
            self, monkeypatch, tmp_path):
        """``json.dumps`` escapes every newline, so routing the tail through
        the existing JSON dump renders a 6 KB traceback as one unreadable line
        of ``\\n``-separated text. That is technically 'surfaced' and is not
        something anyone can read."""
        printed = self._run_main(
            monkeypatch, tmp_path, {"phase": 1},
            run_shard=_Remote(result=self._failed_shard("phase1")))
        assert any(
            line.strip() == "RuntimeError: THE ACTUAL REASON THE RUN DIED"
            for line in printed.splitlines()), (
            "the traceback never arrived as lines anyone can read")
        # ...and NOT ALSO as the escaped copy. The JSON dump runs first and is
        # what the operator's eye lands on, so leaving the block in it means a
        # 6 KB single-line string is the first rendering of the failure they
        # see — and the readable one, 20 KB further down, is the one that gets
        # scrolled past. It also doubles the console write.
        assert "DIED\\n" not in printed, (
            "the log is ALSO being dumped through json.dumps, which escapes "
            "every newline; the readable block below it is then a duplicate "
            "nobody reaches")

    def test_the_phase_two_tail_prints_the_log_of_the_shard_that_died(
            self, monkeypatch, tmp_path):
        """Phase 2's tail never read ``designs`` and printed six numbers per
        shard, so a shard that died inside ``generate`` reported ``rc=1`` and
        nothing else — for ~$12."""
        results = [_shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0),
                   self._failed_shard("negative"),
                   _shard("null", n=8, recall=None, centroid=1.0, cross=0.0)]
        printed = self._run_main(
            monkeypatch, tmp_path, {"phase": 2, "negative": "A50 A51 A52 A53"},
            run_shard=_Remote(handles=[_Handle(result=r) for r in results]))
        assert "RuntimeError: THE ACTUAL REASON THE RUN DIED" in printed, (
            "the phase-2 tail still reports rc=1 and nothing else")
        assert "upstream log tail [negative]" in printed, (
            "the log must be attributed to the shard it came from, or a "
            "three-shard run cannot say which one died")

    def test_a_healthy_phase_two_run_adds_no_log_block(
            self, monkeypatch, tmp_path):
        """Or every green run grows a block of nothing."""
        results = [_shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0),
                   _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0),
                   _shard("null", n=8, recall=0.0, centroid=1.0, cross=0.0)]
        printed = self._run_main(
            monkeypatch, tmp_path, {"phase": 2, "negative": "A50 A51 A52 A53"},
            run_shard=_Remote(handles=[_Handle(result=r) for r in results]))
        assert "upstream log tail" not in printed

    def test_the_upstream_tick_inside_the_log_cannot_kill_the_tail(
            self, monkeypatch, tmp_path):
        """THE REGRESSION THIS CHANGE COULD HAVE INTRODUCED.

        The text now being surfaced is UPSTREAM-AUTHORED, it carries upstream's
        tick, and it is being written to a Windows console for the first time.
        A bare print of it raises UnicodeEncodeError on cp1252 — the first
        assertion is that live proof — and would lose the verdict and the exit
        code after the money was already spent, which is precisely what
        ``harden_stream`` / ``_emit`` exist to prevent.
        """
        assert "✓" in GENERATE_TRACEBACK
        with pytest.raises(UnicodeEncodeError):
            GENERATE_TRACEBACK.encode("cp1252")

        _raw, stream = _console("cp1252")
        monkeypatch.setattr(sys, "stdout", stream)
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, phase0=_Remote(result={}),
            run_shard=_Remote(result=self._failed_shard("phase1")))
        with pytest.raises(SystemExit) as excinfo:
            namespace["main"](phase=1, target_pdb=str(self._target(tmp_path)),
                              hotspots="A1 A2")
        assert excinfo.value.code == cs.EXIT_CODES[cs.FAIL], (
            "the log tail killed the run before it reached a verdict")
        stream.flush()
        printed = _raw.getvalue()
        assert b"THE ACTUAL REASON THE RUN DIED" in printed
        assert b"\\u2713" in printed, (
            "the unencodable character was dropped rather than degraded; the "
            "operator needs to see WHAT upstream printed")

    def test_a_shard_with_no_diagnostics_renders_nothing_at_all(self):
        assert cs.format_log_diagnostics(_shard("positive")) == []
        assert cs.format_log_diagnostics({"label": "x"}) == []
        assert cs.format_log_diagnostics(None) == []

    def test_a_collector_that_failed_says_so_instead_of_going_quiet(self):
        lines = cs.format_log_diagnostics(
            {"label": "phase1", "log_diagnostics": {"error": "OSError: gone"}})
        assert any("OSError: gone" in line for line in lines)


# ---------------------------------------------------------------------------
# 19. THE VERDICT QUORUM'S DENOMINATOR — a PASS on one design out of eight
#
# ``positive_verdict`` computed
#
#     required = max(1, ceil(min_hit_fraction * len(scorable)))
#
# i.e. the quorum was normalised onto the SURVIVORS, so every design the shard
# discarded made the bar LOWER. The arithmetic at the limit:
#
#     8 designs produced, 7 refused as unverifiable, 1 lands on the patch
#       -> len(scorable) == 1
#       -> required == max(1, ceil(0.75 * 1)) == 1
#       -> on_patch == 1 >= 1              -> PASS, exit 0
#
# and the ``max(1, ...)`` floor made it unconditional: no shard with a single
# survivor could fail the count. That is a green light for FLAG_TOOL_PROTEINA
# off ONE design — the harness reproducing, inside itself, the exact upstream
# failure it was built to detect: a number computed against a denominator
# nobody chose, indistinguishable from a correct one.
#
# The shards below are not hand-typed. They come out of the REAL ``run_shard``
# body scoring REAL design files (7 relabelled + 1 correct), so these fail if
# the scoring stops producing that shape as well as if the verdict stops
# refusing it.
# ---------------------------------------------------------------------------


def _mixed_shard(tmp_path, n_relabelled, n_correct, label="positive"):
    """A genuine ``run_shard`` return value with a known scorable fraction.

    ``RELABELLED_DESIGN_PDB`` is a complex whose chain A is the BINDER, so
    ``verify_target_identity`` refuses it and it reaches the verdicts with no
    metric keys at all — the shape a discarded design really has. The correct
    files sit 4 A off A1..A4, so each is on the requested patch.
    """
    files = [(f"relabelled_{i}.pdb", RELABELLED_DESIGN_PDB)
             for i in range(n_relabelled)]
    files += [(f"sample_{i}.pdb", CORRECT_DESIGN_PDB) for i in range(n_correct)]
    namespace = _shard_namespace(tmp_path, design_files=files)
    out = namespace["run_shard"](
        INPUT_TARGET_PDB, label, ["A1", "A2"], "", 1234, [60, 120],
        False, ["A1", "A2"])
    assert out.get("error") is None, out.get("error")
    assert len(out["designs"]) == n_relabelled + n_correct
    assert out["n_target_verified"] == n_correct
    return out


class TestTheQuorumDenominator:
    """A PASS must be a statement about the shard, not about its survivors."""

    def test_one_scorable_design_of_eight_is_not_a_pass(self, tmp_path):
        """THE DEFECT, in the exact shape it ships in: 8 designs, 7 refused as
        unverifiable, 1 on the patch. Against the old code this returned PASS
        and exit 0, which flips a production feature flag."""
        shard = _mixed_shard(tmp_path, n_relabelled=7, n_correct=1)
        assert len(shard["designs"]) == 8 and shard["n_target_verified"] == 1
        assert shard["hotspot_recall_median"] == 1.0, (
            "the one survivor really is on the patch; this is the case that "
            "used to pass")
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome != cs.PASS, verdict.reason
        assert verdict.outcome == cs.INCONCLUSIVE, (
            "7 of 8 designs unmeasurable is not evidence the feature is "
            f"broken either; got {verdict.outcome}: {verdict.reason}")
        assert verdict.metrics["n_designs"] == 8
        assert verdict.metrics["n_scorable"] == 1
        assert verdict.metrics["n_required"] == 6, (
            "the quorum must be six of the EIGHT designs produced, not one of "
            "the one design that survived")

    def test_the_whole_phase_two_report_refuses_that_run(self, tmp_path):
        """...and it reaches the exit code, which is what any script wrapped
        around this harness actually reads."""
        pos = _mixed_shard(tmp_path, 7, 1, "positive")
        report = cs.phase2_report(pos, dict(pos, label="negative"),
                                  dict(pos, label="null"))
        by_name = {v.name: v for v in report["verdicts"]}
        assert by_name["positive"].outcome != cs.PASS, (
            "named explicitly: 'the report is not a PASS' can be satisfied by "
            "some OTHER control failing, which would leave the defect covered "
            "by an accident")
        assert report["overall"] != cs.PASS
        assert report["exit_code"] != 0, "phase 2 exited 0 on one design"

    def test_a_shard_whose_designs_all_score_still_passes(self, tmp_path):
        """The fix must not be a blanket refusal: the same path with every
        design scorable is the run phase 2 exists to bless."""
        shard = _mixed_shard(tmp_path, n_relabelled=0, n_correct=8)
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome == cs.PASS, verdict.reason
        assert verdict.metrics["n_scorable"] == 8

    def test_the_boundary_is_the_produced_count_not_the_scorable_count(self):
        """Six of eight is the bar either way; what changed is what "eight"
        counts. Pinned at the boundary rather than by re-deriving the formula,
        for the reason given in
        ``test_the_hit_threshold_scales_with_the_scorable_design_count``.
        """
        def shard(produced, scorable, on_patch):
            out = _shard(n=produced, recall=1.0, centroid=1.0)
            for design in out["designs"][scorable:]:
                # Exactly what score_design_file emits for a design whose
                # putative-target chain is not the target: no metric keys.
                design.update(target_verified=False)
                design.pop("hotspot_recall", None)
                design.pop("centroid_distance", None)
            for design in out["designs"][on_patch:scorable]:
                design["hotspot_recall"] = 0.0
            return out

        # All eight scorable: unchanged behaviour, six is the bar.
        assert cs.positive_verdict(shard(8, 8, 6)).outcome == cs.PASS
        assert cs.positive_verdict(shard(8, 8, 5)).outcome == cs.FAIL
        # Six scorable, all six on the patch: six of the eight PRODUCED
        # designs reached the patch, so this is a real PASS.
        assert cs.positive_verdict(shard(8, 6, 6)).outcome == cs.PASS
        # Six scorable, five on the patch: five of eight is under the quorum.
        # The old code compared 5 against ceil(0.75 * 6) = 5 and passed.
        assert cs.positive_verdict(shard(8, 6, 5)).outcome == cs.FAIL
        # Five scorable: the quorum of six is unreachable for want of
        # measurable designs — an unmeasured run, not a broken one.
        assert cs.positive_verdict(shard(8, 5, 5)).outcome == cs.INCONCLUSIVE

    def test_the_scorable_floor_is_a_threshold_not_a_literal(self):
        """It lives in ``Thresholds`` with everything else phase 2 turns on, so
        an operator can read it and move it without editing a verdict."""
        assert cs.DEFAULT_THRESHOLDS.min_scorable_fraction == 0.75
        loose = cs.Thresholds(min_scorable_fraction=0.1, min_hit_fraction=0.1)
        thin = _shard(n=8, recall=1.0, centroid=1.0)
        for design in thin["designs"][1:]:
            design.update(target_verified=False)
            design.pop("hotspot_recall", None)
        assert cs.positive_verdict(thin).outcome == cs.INCONCLUSIVE
        assert cs.positive_verdict(thin, loose).outcome == cs.PASS, (
            "the floor is not being read from the thresholds")

    def test_nothing_scorable_at_all_keeps_its_own_message(self):
        """The thin-scorable reason must not steal the message that sends the
        operator to the chain map — they prescribe different next moves."""
        verdict = cs.positive_verdict(_shard(n=8, target_verified=False))
        assert verdict.outcome == cs.INCONCLUSIVE
        assert "chain_hints" in verdict.reason, (
            "a shard with NO scorable designs must still get the relabelling "
            "message, not the fraction one")

    def test_the_negative_control_cannot_be_blessed_by_one_design(self, tmp_path):
        """The same denominator hole, median-shaped. ``cross`` is a median over
        the scorable designs, so one survivor out of eight reports a property
        of one design as a property of the shard."""
        neg = _mixed_shard(tmp_path, 7, 1, "negative")
        neg["cross_hotspot_recall_median"] = 0.0      # a perfect negative
        neg["centroid_distance_median"] = 1.0
        verdict = cs.negative_verdict(neg)
        assert verdict.outcome != cs.PASS, verdict.reason
        assert verdict.outcome == cs.INCONCLUSIVE
        assert verdict.metrics["n_scorable_cross"] == 1
        assert verdict.metrics["n_designs"] == 8

    def test_a_fully_scorable_negative_control_still_passes(self):
        neg = _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0)
        assert cs.negative_verdict(neg).outcome == cs.PASS

    def test_the_null_margin_cannot_rest_on_one_design_per_side(self):
        """The margin is the number that says the hotspots were not silently
        dropped. It must not be read off a shard nobody could measure."""
        def thin(label, cross):
            out = _shard(label, n=8, recall=1.0, centroid=1.0, cross=cross)
            for design in out["designs"][1:]:
                design.update(target_verified=False)
                design.pop("cross_hotspot_recall", None)
                design.pop("hotspot_recall", None)
            return out

        fat_pos = _shard("positive", n=8, cross=1.0)
        fat_null = _shard("null", n=8, cross=0.0)
        assert cs.null_verdict(fat_pos, fat_null).outcome == cs.PASS

        assert cs.null_verdict(thin("positive", 1.0), fat_null).outcome == (
            cs.INCONCLUSIVE), "one scorable positive design decided the margin"
        assert cs.null_verdict(fat_pos, thin("null", 0.0)).outcome == (
            cs.INCONCLUSIVE), "one scorable null design decided the margin"


class TestTheVerdictSaysWhy:
    """A near-miss and a catastrophe must not read identically."""

    def test_the_counts_behind_a_non_pass_verdict_are_rendered(self, tmp_path):
        verdict = cs.positive_verdict(_mixed_shard(tmp_path, 7, 1))
        lines = cs.verdict_diagnostics(verdict)
        assert lines, "a non-PASS verdict rendered no numbers at all"
        rendered = " ".join(lines)
        assert "designs produced 8" in rendered
        assert "scorable 1" in rendered
        assert "on the requested patch 1" in rendered
        assert "needed for a PASS 6" in rendered

    def test_a_pass_prints_nothing_extra(self, tmp_path):
        verdict = cs.positive_verdict(_mixed_shard(tmp_path, 0, 8))
        assert verdict.outcome == cs.PASS
        assert cs.verdict_diagnostics(verdict) == [], (
            "a green run's console must be what it was")

    def test_a_near_miss_and_a_catastrophe_render_differently(self):
        def with_on_patch(k):
            out = _shard(n=8, recall=1.0, centroid=1.0)
            for design in out["designs"][k:]:
                design["hotspot_recall"] = 0.0
            return cs.verdict_diagnostics(cs.positive_verdict(out))
        assert with_on_patch(5) != with_on_patch(0), (
            "5-of-8 and 0-of-8 render identically; the operator cannot tell a "
            "near-miss from a catastrophe")

    def test_a_verdict_with_nested_metrics_does_not_render_a_dict(self):
        """The null verdict's crash path carries per-shard blocks. Flattening
        them into the line would make it unreadable, which is the failure being
        fixed, not a fix for it."""
        verdict = cs.null_verdict(_shard("positive", exit_code=1),
                                  _shard("null", cross=0.0))
        assert verdict.outcome == cs.FAIL
        for line in cs.verdict_diagnostics(verdict):
            assert "{" not in line and "[" not in line

    def test_none_renders_as_na_rather_than_disappearing(self):
        verdict = cs.Verdict("x", cs.INCONCLUSIVE, "r",
                             {"n_designs": 8, "n_on_patch": None})
        rendered = " ".join(cs.verdict_diagnostics(verdict))
        assert "n/a" in rendered, (
            "an unmeasurable count that prints nothing is indistinguishable "
            "from one that was never collected")

    def test_it_survives_anything_that_is_not_a_verdict(self):
        assert cs.verdict_diagnostics(None) == []
        assert cs.verdict_diagnostics(cs.Verdict("x", cs.FAIL, "r", {})) == []

    def test_the_operator_actually_sees_them(self, monkeypatch, tmp_path):
        """The rendering is worth nothing if no tail prints it — the shape of
        every defect this file exists to catch. Driven through the REAL
        ``main``, on a phase-2 run whose positive shard is 1-of-8."""
        results = [_mixed_shard(tmp_path, 7, 1, "positive"),
                   _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0),
                   _shard("null", n=8, recall=None, centroid=1.0, cross=0.0)]
        _raw, stream = _console("utf-8")
        monkeypatch.setattr(sys, "stdout", cs.harden_stream(stream))
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, phase0=_Remote(result={}),
            run_shard=_Remote(handles=[_Handle(result=r) for r in results]))
        with pytest.raises(SystemExit) as excinfo:
            namespace["main"](phase=2, target_pdb=str(target),
                              hotspots="A1 A2", negative="A50 A51 A52 A53")
        assert excinfo.value.code == cs.EXIT_CODES[cs.INCONCLUSIVE]
        stream.flush()
        printed = _raw.getvalue().decode("utf-8")
        assert "needed for a PASS 6" in printed, (
            "the verdict block still shows only prose; the counts that say HOW "
            "badly it missed never reach the operator")
        assert "designs produced 8" in printed and "scorable 1" in printed


# ---------------------------------------------------------------------------
# 20. CANCELLATION ON EVERY LOCAL EXIT PATH, not just Ctrl-C
#
# ``_cancel_outstanding`` was wired to the ``except BaseException`` handler
# around spawn-and-collect and to nothing else. Two holes, both of which end
# with A100s billing to _MAX_SESSION_S = 7200 s at ~$12.58 a shard:
#
#  (1) ``collected`` was the ``results`` dict, and the collect loop writes an
#      ``{"error": ...}`` entry for a ``get`` that RAISED. But
#      ``FunctionCall.get(timeout=)`` maps to ``poll_function`` and terminates
#      NOTHING — a local timeout leaves the shard running. So the one shard
#      that certainly was still billing was the one recorded as collected, and
#      it could never be cancelled, on any path, ever.
#
#  (2) everything after the collect loop — formatting a shard's output, the
#      scoring, ``_finish``'s SystemExit, which is the NORMAL exit for FAIL and
#      INCONCLUSIVE — ran outside any handler that cancels.
#
# The fix is a ``try/finally`` around the whole region plus a ``settled`` set
# that only a ``get`` which RETURNED can add to. The interrupt handler stays,
# because it is the only thing that can say why; the cancel is idempotent so
# both can run.
# ---------------------------------------------------------------------------


class TestEveryExitPathCancels:

    @staticmethod
    def _target(tmp_path):
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        return target

    @staticmethod
    def _namespace(handles, **injected):
        return load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, phase0=_Remote(result={}),
            run_shard=_Remote(handles=list(handles)), **injected)

    def _run(self, tmp_path, handles, **injected):
        namespace = self._namespace(handles, **injected)
        return namespace["main"], str(self._target(tmp_path))

    def test_a_shard_whose_get_timed_out_is_cancelled_not_written_off(
            self, tmp_path):
        """THE DEFECT. ``get(timeout=)`` polls; it does not terminate. The
        shard that timed out is the one still burning an A100, and recording an
        error for it marked it collected — so it was skipped by every cancel
        and billed to the 7200 s ceiling."""
        timed_out = _Handle(raises=TimeoutError("the shard is still running"))
        ok_a = _Handle(result=_shard("negative", n=8, recall=1.0, centroid=1.0,
                                     cross=0.0))
        ok_b = _Handle(result=_shard("null", n=8, recall=None, centroid=1.0,
                                     cross=0.0))
        main, target = self._run(tmp_path, [timed_out, ok_a, ok_b])
        with pytest.raises(SystemExit) as excinfo:
            main(phase=2, target_pdb=target, hotspots="A1 A2",
                 negative="A50 A51 A52 A53")
        assert excinfo.value.code == cs.EXIT_CODES[cs.FAIL], (
            "the run must still reach a verdict")
        assert timed_out.cancels == [True], (
            "a shard whose get() raised was never cancelled; it is still "
            "running on an A100 and will bill to _MAX_SESSION_S")
        assert ok_a.cancels == [] and ok_b.cancels == [], (
            "a shard whose get() RETURNED is finished; cancelling it is noise")

    def test_a_raise_in_the_tail_does_not_abandon_a_running_shard(self, tmp_path):
        """Not an interrupt, not an exit: an ordinary exception between the
        collect and the verdict — a formatting bug, an AttributeError, anything
        this module does after the money is committed."""
        boom = RuntimeError("the tail blew up while formatting")

        def explode(_verdict):
            raise boom

        timed_out = _Handle(raises=TimeoutError("still running"))
        ok = _Handle(result=_shard("negative", n=8, recall=1.0, centroid=1.0,
                                   cross=0.0))
        ok2 = _Handle(result=_shard("null", n=8, recall=None, centroid=1.0,
                                    cross=0.0))
        main, target = self._run(tmp_path, [timed_out, ok, ok2],
                                 _print_verdict=explode)
        with pytest.raises(RuntimeError) as excinfo:
            main(phase=2, target_pdb=target, hotspots="A1 A2",
                 negative="A50 A51 A52 A53")
        assert excinfo.value is boom, "the original exception must survive"
        assert timed_out.cancels == [True], (
            "the entrypoint died after the spawn and left an A100 billing")

    def test_a_failed_spawn_does_not_leave_the_earlier_shards_running(
            self, tmp_path):
        """The first shard is launched, the second spawn raises. The first is
        billing; nothing collects it, because the loop it would have been
        collected in never ran."""
        launched = _Handle(raises=TimeoutError("never collected"))

        class _OneThenBoom(_Remote):
            def spawn(self, *args, **kwargs):
                self.spawn_calls.append(args)
                if len(self.spawn_calls) == 1:
                    return launched
                raise RuntimeError("modal said no")

        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, phase0=_Remote(result={}),
            run_shard=_OneThenBoom())
        with pytest.raises(SystemExit):
            namespace["main"](phase=2, target_pdb=str(self._target(tmp_path)),
                              hotspots="A1 A2", negative="A50 A51 A52 A53")
        assert launched.cancels == [True], (
            "a spawn failure abandoned the shard that HAD started")

    def test_cancellation_is_idempotent_across_the_two_call_sites(
            self, tmp_path):
        """The interrupt handler and the finally both run on a Ctrl-C. A second
        cancel of the same call is at best a wasted round trip and at worst an
        exception raised out of a ``finally``, so each container is handled
        exactly once."""
        handles = [_Handle(raises=KeyboardInterrupt()), _Handle(), _Handle()]
        main, target = self._run(tmp_path, handles)
        with pytest.raises(KeyboardInterrupt):
            main(phase=2, target_pdb=target, hotspots="A1 A2",
                 negative="A50 A51 A52 A53")
        for handle in handles:
            assert handle.cancels == [True], (
                f"cancelled {len(handle.cancels)} times, not once")

    def test_a_cancel_that_raises_cannot_mask_the_verdict(self, tmp_path):
        """A second Ctrl-C landing inside the cleanup must not replace the
        exception that is already unwinding — SystemExit(1) carries the FAIL
        verdict, and every script around this harness reads that code."""
        timed_out = _Handle(raises=TimeoutError("still running"),
                            cancel_raises=KeyboardInterrupt())
        ok = _Handle(result=_shard("negative", n=8, recall=1.0, centroid=1.0,
                                   cross=0.0))
        ok2 = _Handle(result=_shard("null", n=8, recall=None, centroid=1.0,
                                    cross=0.0))
        main, target = self._run(tmp_path, [timed_out, ok, ok2])
        with pytest.raises(SystemExit) as excinfo:
            main(phase=2, target_pdb=target, hotspots="A1 A2",
                 negative="A50 A51 A52 A53")
        assert excinfo.value.code == cs.EXIT_CODES[cs.FAIL], (
            "the cleanup replaced the verdict with its own exception")
        assert timed_out.cancels == [True], "the cancel was still attempted"

    def test_a_healthy_run_cancels_nothing(self, tmp_path):
        """The finally must be a no-op on the path that matters most."""
        results = [_shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0),
                   _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0),
                   _shard("null", n=8, recall=None, centroid=1.0, cross=0.0)]
        handles = [_Handle(result=r) for r in results]
        main, target = self._run(tmp_path, handles)
        main(phase=2, target_pdb=target, hotspots="A1 A2",
             negative="A50 A51 A52 A53")
        assert [h.cancels for h in handles] == [[], [], []]

    def test_the_spawn_and_collect_region_sits_inside_a_cancelling_finally(self):
        """The structural half. A ``finally`` is the only construct that covers
        the exits an ``except`` clause cannot see, and the test that guarded
        this before only knew about the BaseException handler."""
        tree = ast.parse(_CANARY_PATH.read_text(encoding="utf-8"))
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        guarded = [
            node for node in ast.walk(main)
            if isinstance(node, ast.Try) and node.finalbody and any(
                isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "_cancel_outstanding"
                for stmt in node.finalbody
                for call in ast.walk(stmt))
        ]
        assert guarded, (
            "no try/finally in main() cancels the shards; every exit that is "
            "not a KeyboardInterrupt leaves three A100s billing")
        spawns = [n for n in ast.walk(main)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute) and n.func.attr == "spawn"]
        assert spawns, "the spawn loop moved; this test is checking nothing"

        def covering(node):
            """The cancelling try/finallys whose BODY contains ``node``."""
            return [try_node for try_node in guarded
                    if any(node is call
                           for stmt in try_node.body for call in ast.walk(stmt))]

        # PER SPAWN, not "one try holds them all": phase 1 and phase 2 each
        # spawn, in separate branches, so a single covering try is impossible —
        # but EVERY spawn must still be inside one. Phase 1 used to block on
        # `.remote()`, which returns no FunctionCall at all, so its A100 was
        # uncancellable on every local exit path including the SystemExit that
        # `_finish` raises for FAIL and INCONCLUSIVE. That is ~$4 normally and
        # up to the ~$12.58 per-shard ceiling if it runs to the wall.
        for spawn in spawns:
            assert covering(spawn), (
                f"the .spawn on line {spawn.lineno} is outside every cancelling "
                "try/finally; whatever it launches bills on after any local "
                "death")
        # ...and every verdict/exit tail that FOLLOWS a spawn is inside one too,
        # not after it. Phase 0 finishes long before any shard exists and has
        # nothing to cancel, so only the `_finish` calls past the first spawn
        # count.
        first_spawn = min(n.lineno for n in spawns)
        finishes = [n for n in ast.walk(main)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_finish" and n.lineno > first_spawn]
        assert finishes, "the shard phases no longer finish through _finish"
        for finish in finishes:
            assert covering(finish), (
                f"the _finish on line {finish.lineno} raises SystemExit for "
                "FAIL and INCONCLUSIVE, so it is a local exit path and must be "
                "inside a cancelling try")

    def test_no_gpu_shard_is_awaited_with_a_blocking_remote(self):
        """``.remote()`` RETURNS A RESULT, NOT A HANDLE, and a handle is the
        only thing that can be cancelled.

        Phase 1 called ``run_shard.remote(...)`` and therefore held nothing: the
        `finally`, `_cancel_outstanding` and `terminate_containers=True` all
        existed and all had nothing to act on, so a local death — a cp1252
        UnicodeEncodeError killed one on 2026-08-04 — left an A100 billing to
        `_MAX_SESSION_S`. Only ``phase0`` may be awaited: it is CPU-only, capped
        at 900 s and free.
        """
        tree = ast.parse(_CANARY_PATH.read_text(encoding="utf-8"))
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        awaited = {
            ast.unparse(node.func.value)
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "remote"
            and isinstance(node.func.value, ast.Name)
        }
        assert awaited <= {"phase0"}, (
            f"a GPU shard is awaited with a blocking .remote(): {sorted(awaited)}"
            " — it yields no modal.FunctionCall, so nothing can cancel it")

    # -- a cancel that FAILED is not a cancel -------------------------------

    @staticmethod
    def _cancel():
        return load_canary_functions({"_cancel_outstanding"})["_cancel_outstanding"]

    def test_a_cancel_that_raised_is_retried_by_the_next_caller(self):
        """THE DEFECT. ``settled.add(label)`` ran BEFORE ``handle.cancel(...)``,
        so a transient gRPC error on the first attempt wrote the shard down as
        settled and every later caller skipped it — including the outer
        ``finally``, which exists precisely to be the second chance.

        ``settled`` means "no longer billing", which a cancel that RAISED has
        established the opposite of. One Ctrl-C plus one flaky RPC and an A100
        bills to ``_MAX_SESSION_S`` (~$12.58) with the console already claiming
        the shard was handled.
        """
        cancel = self._cancel()
        # Fails once, succeeds on the retry: a transient failure, not a dead
        # container.
        handle = _Handle(cancel_raises=[RuntimeError("transient gRPC failure")])
        handles = [("negative", handle)]
        settled: set = set()

        cancel(handles, settled)                      # the interrupt handler
        assert handle.cancels == [True]
        assert settled == set(), (
            "a cancel that RAISED marked the shard settled; it is still "
            "running on an A100 and no later call will touch it")

        cancel(handles, settled)                      # the outer finally
        assert handle.cancels == [True, True], (
            "the second attempt never happened — this is the shard that is "
            "certainly still billing")
        assert settled == {"negative"}, "the successful cancel did not settle it"

        cancel(handles, settled)                      # ...and now it stops
        assert handle.cancels == [True, True], (
            "a cancel that SUCCEEDED was repeated; each container is killed once")

    def test_the_two_call_sites_are_two_real_attempts_end_to_end(self, tmp_path):
        """The same defect through the real ``main``: Ctrl-C during collect, a
        transient failure on the interrupt handler's cancel, and the outer
        ``finally`` must still reach the container."""
        flaky = _Handle(raises=KeyboardInterrupt(),
                        cancel_raises=[RuntimeError("transient gRPC failure")])
        ok, ok2 = _Handle(), _Handle()
        main, target = self._run(tmp_path, [flaky, ok, ok2])
        with pytest.raises(KeyboardInterrupt):
            main(phase=2, target_pdb=target, hotspots="A1 A2",
                 negative="A50 A51 A52 A53")
        assert flaky.cancels == [True, True], (
            "the finally skipped the one shard whose cancel had failed")
        assert ok.cancels == [True] and ok2.cancels == [True], (
            "the shards that cancelled cleanly were cancelled twice")

    def test_a_cancel_that_keeps_failing_is_retried_and_masks_nothing(
            self, tmp_path):
        """The guarantee the pre-fix ordering claimed to buy, VERIFIED rather
        than assumed: it is held independently by the ``except Exception``
        swallow, so moving the mark cannot cost it. Two failing attempts, and
        the KeyboardInterrupt that started the unwind is what comes out."""
        stubborn = _Handle(raises=KeyboardInterrupt(),
                           cancel_raises=RuntimeError("modal is down"))
        ok, ok2 = _Handle(), _Handle()
        main, target = self._run(tmp_path, [stubborn, ok, ok2])
        with pytest.raises(KeyboardInterrupt):
            main(phase=2, target_pdb=target, hotspots="A1 A2",
                 negative="A50 A51 A52 A53")
        assert stubborn.cancels == [True, True]

    def test_a_base_exception_from_a_cancel_still_re_raises_when_alone(self):
        """The OTHER half of the masking guarantee, also verified directly: it
        is suppressed only while something else is unwinding
        (``sys.exc_info()``), so called on its own it must still surface."""
        cancel = self._cancel()
        handle = _Handle(cancel_raises=KeyboardInterrupt())
        settled: set = set()
        with pytest.raises(KeyboardInterrupt):
            cancel([("positive", handle)], settled)
        assert handle.cancels == [True]
        assert settled == set(), (
            "a cancel interrupted mid-flight left the label permanently "
            "unreachable; nothing can retry it")

    def test_the_rest_of_the_loop_still_runs_after_a_failed_cancel(self):
        """Best-effort per handle: the one that refuses must not take the
        others with it, and only the survivors are settled."""
        cancel = self._cancel()
        bad = _Handle(cancel_raises=RuntimeError("no"))
        good_a, good_b = _Handle(), _Handle()
        settled: set = set()
        cancel([("positive", bad), ("negative", good_a), ("null", good_b)],
               settled)
        assert bad.cancels == [True]
        assert good_a.cancels == [True] and good_b.cancels == [True]
        assert settled == {"negative", "null"}

    # -- phase 1 holds a handle at all --------------------------------------

    @staticmethod
    def _phase1_result(*, wired=True):
        res = _shard("phase1", n=8, recall=1.0, centroid=0.0)
        res["hydra"] = {"task_name_selected": wired, "hotspots_match": True,
                        "hotspots_order_matches": True,
                        "task_name_values": ["somebody_elses_target"]}
        return res

    def test_a_ctrl_c_during_the_phase_one_wait_kills_the_container(
            self, tmp_path):
        """FINDING 6. Phase 1 called ``run_shard.remote(...)``, which blocks and
        returns a RESULT rather than a ``modal.FunctionCall`` — so it held no
        handle, and every piece of cancellation machinery in this module had
        nothing to act on. A local death during the wait left the A100 billing
        to ``_MAX_SESSION_S``: ~$4 for a normal phase-1 duration, up to ~$12.58
        at the ceiling. A cp1252 ``UnicodeEncodeError`` killed a local
        entrypoint mid-run on 2026-08-04, which is exactly this shape."""
        interrupted = _Handle(raises=KeyboardInterrupt())
        main, target = self._run(tmp_path, [interrupted])
        with pytest.raises(KeyboardInterrupt):
            main(phase=1, target_pdb=target, hotspots="A1 A2")
        assert interrupted.cancels == [True], (
            "phase 1 abandoned its A100; nothing it holds is cancellable")

    def test_a_phase_one_get_that_timed_out_is_cancelled_not_abandoned(
            self, tmp_path):
        """``FunctionCall.get(timeout=)`` maps to ``poll_function`` and
        terminates nothing, so the shard whose collect timed out is the one
        certainly still burning an A100."""
        timed_out = _Handle(raises=TimeoutError("the shard is still running"))
        main, target = self._run(tmp_path, [timed_out])
        with pytest.raises(TimeoutError):
            main(phase=1, target_pdb=target, hotspots="A1 A2")
        assert timed_out.cancels == [True]

    def test_phase_one_reuses_the_one_cancellation_path(self):
        """Not a bespoke second one. A separate cancel in the phase-1 branch
        would be a second thing to get wrong and would not inherit the
        retry-a-failed-cancel fix above."""
        tree = ast.parse(_CANARY_PATH.read_text(encoding="utf-8"))
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        killers = {
            node.func.attr if isinstance(node.func, ast.Attribute)
            else node.func.id
            for node in ast.walk(main) if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
        } & {"cancel", "terminate", "_cancel_outstanding"}
        assert killers == {"_cancel_outstanding"}, (
            f"main() kills containers through {sorted(killers)}; there must be "
            "exactly one cancellation path and both phases must use it")

    def test_a_healthy_phase_one_run_cancels_nothing(self, tmp_path):
        """The finally must be a no-op once the get has returned: that shard
        has finished and cancelling it is a wasted round trip."""
        handle = _Handle(result=self._phase1_result())
        main, target = self._run(tmp_path, [handle])
        main(phase=1, target_pdb=target, hotspots="A1 A2")
        assert handle.cancels == []

    def test_phase_one_still_reaches_its_verdict_and_exit_code(self, tmp_path):
        """The spawn must not have cost phase 1 its answer. A shard whose
        resolved config named somebody else's target is still a FAIL with exit
        1 — and there is nothing to cancel, because the get returned."""
        handle = _Handle(result=self._phase1_result(wired=False))
        main, target = self._run(tmp_path, [handle])
        with pytest.raises(SystemExit) as excinfo:
            main(phase=1, target_pdb=target, hotspots="A1 A2")
        assert excinfo.value.code == cs.EXIT_CODES[cs.FAIL]
        assert handle.cancels == []
        assert handle.gets == 1


# ---------------------------------------------------------------------------
# 21. THE CANARY MUST STAGE WHERE PRODUCTION STAGES
#
# ``prepare_custom_target`` writes ``$PROTEINA_HOME/hub_targets/<key>.pdb`` and
# registers it with ``--target-filename <staged.stem>``, so the file's stem IS
# the registry key. The canary wrote ``/tmp/canary_targets/<label>.pdb`` and
# registered a stem of "phase1" / "positive" / "null" against a key of
# ``hub_canary<hex>``: a different directory AND a different stem.
#
# Upstream matches on the literal strings in that record, and the whole premise
# of this harness is detecting a silent path/registration mismatch. Exercising
# a shape production never emits is the one way to answer the question about
# some other request.
# ---------------------------------------------------------------------------


class TestStagingMatchesProduction:

    @staticmethod
    def _phase0_namespace(tmp_path):
        fake = _fake_rp(tmp_path / "proteina")
        namespace = load_canary_functions(
            {"phase0", "_stage", "_stage_dir"},
            _load_rp=lambda: fake, _prune_registry=lambda module: [])
        namespace["_fake_rp"] = fake
        return namespace

    @staticmethod
    def _registered(cmd):
        """``{--flag: value}`` for the single-valued flags of a target add."""
        out = {}
        for i, token in enumerate(cmd):
            if str(token).startswith("--") and i + 1 < len(cmd):
                out.setdefault(token, cmd[i + 1])
        return out

    def test_the_shard_stages_in_the_hub_target_dir_under_the_key(self, tmp_path):
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        fake = namespace["_fake_rp"]
        key = out["key"]
        expected = Path(fake._HUB_TARGET_DIR) / f"{key}.pdb"
        assert expected.is_file(), (
            f"the target was not staged at {expected}; production stages "
            "every uploaded target there and nowhere else")
        # THIS ASSERTION USED TO READ ``== INPUT_TARGET_PDB``, AND IT ENCODED
        # THE WRONG EXPECTATION. It said the canary stages the upload VERBATIM,
        # which was true until production grew a crop and then became the bug:
        # a paid phase-1 shard staged uncropped bytes and reproduced upstream's
        # ``metric_utils.py:217`` assertion on real hardware. Replaced, not
        # weakened - the staged bytes must now be what PRODUCTION'S staging step
        # produces for the same input, which is a strictly stronger claim than
        # equality with any literal this file could hold.
        reference = tmp_path / "reference.pdb"
        raw = tmp_path / "raw.pdb"
        raw.write_text(INPUT_TARGET_PDB)
        residues, _ = rp.pdb_ca_residues(raw)
        rp.stage_cropped_target(
            reference, INPUT_TARGET_PDB, residues,
            rp.derive_segments(residues, sorted({r[0] for r in residues})))
        assert expected.read_text() == reference.read_text()
        add = self._registered(fake.streamed[0])
        assert add["--target-path"] == str(expected)
        assert add["--target-filename"] == key, (
            "production passes filename_stem=staged.stem and the stem is the "
            "KEY; registering a label here tests a request prod never makes")

    def test_phase_zero_stages_the_same_way(self, tmp_path):
        namespace = self._phase0_namespace(tmp_path)
        results = namespace["phase0"](INPUT_TARGET_PDB)
        assert results["pass"] is True, results
        fake = namespace["_fake_rp"]
        key = cs.canary_task_key("phase0", 0)
        expected = Path(fake._HUB_TARGET_DIR) / f"{key}.pdb"
        assert expected.is_file()
        add = self._registered(fake.streamed[0])
        assert add["--target-path"] == str(expected)
        assert add["--target-filename"] == key

    def test_the_staging_rule_is_the_one_run_pipeline_uses(self, tmp_path):
        """Both halves, against ``run_pipeline`` itself rather than against a
        restatement: the DIRECTORY is prod's, and the STEM is the key."""
        fake = _fake_rp(tmp_path / "proteina")
        namespace = load_canary_functions({"_stage", "_stage_dir"})
        key = cs.canary_task_key("positive", 1234)
        staged, _raw, _contig = namespace["_stage"](fake, INPUT_TARGET_PDB, key)
        assert staged.parent == Path(fake._HUB_TARGET_DIR)
        assert staged.stem == key
        # run_pipeline's own expression, evaluated here: `target_dir /
        # f"{key}.pdb"` with target_dir = Path(_HUB_TARGET_DIR).
        assert staged == Path(fake._HUB_TARGET_DIR) / f"{key}.pdb"
        # ...and the constant the canary reads is prod's, not a copy of it.
        assert namespace["_stage_dir"](rp) == Path(rp._HUB_TARGET_DIR)
        assert rp._HUB_TARGET_DIR == f"{rp.PROTEINA_HOME}/hub_targets"

    def test_the_canary_no_longer_stages_under_tmp(self):
        """A source pin, because the failure is invisible at runtime: staging
        somewhere prod never touches still produces a clean-looking run.

        Over the string CONSTANTS the module evaluates, not over its text — the
        history of that path belongs in the comments explaining why it moved,
        and a raw substring check would forbid writing it down.
        """
        tree = ast.parse(_CANARY_PATH.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        staging_paths = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings and "/tmp/" in node.value
        ]
        assert staging_paths == [], (
            f"the canary still names a /tmp path as a value: {staging_paths}")
        stage = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_stage_dir")
        assert "_HUB_TARGET_DIR" in ast.unparse(stage), (
            "the canary must read prod's staging directory rather than name "
            "its own")

    def test_pruning_deletes_only_the_pdbs_this_harness_staged(self, tmp_path):
        """The staging directory is now SHARED with production, so the old
        ``shutil.rmtree`` of it would delete files the canary did not write.
        A curated benchmark target is not in this directory at all, and a prod
        upload's key is ``hub_`` + hex, which cannot start with the canary
        prefix."""
        fake = _fake_rp(tmp_path / "proteina")
        target_dir = Path(fake._HUB_TARGET_DIR)
        target_dir.mkdir(parents=True)
        mine = target_dir / f"{cs.canary_task_key('positive', 1234)}.pdb"
        theirs = target_dir / "hub_deadbeefdeadbeef.pdb"
        curated = target_dir / "02_PDL1.pdb"
        other = target_dir / "incoming.pdb"
        for path in (mine, theirs, curated, other):
            path.write_text("ATOM\n")
        namespace = load_canary_functions({"_prune_staged", "_stage_dir"})
        removed = namespace["_prune_staged"](fake)
        assert removed == [mine.name]
        assert not mine.exists()
        for survivor in (theirs, curated, other):
            assert survivor.exists(), f"{survivor.name} was deleted"

    def test_pruning_a_missing_directory_is_not_an_error(self, tmp_path):
        """It runs at shard START, before anything has been staged."""
        fake = _fake_rp(tmp_path / "nothing_here")
        namespace = load_canary_functions({"_prune_staged", "_stage_dir"})
        assert namespace["_prune_staged"](fake) == []

    def test_the_file_prune_and_the_registry_prune_agree_on_what_is_ours(self):
        """Two prefix filters over one namespace; if they disagree, one of them
        is either leaving litter or deleting somebody else's target."""
        keys = [cs.canary_task_key(label, 1234)
                for label in ("phase0", "phase1", "positive", "negative", "null")]
        listing = [f"{k}.pdb" for k in keys] + [
            "hub_deadbeefdeadbeef.pdb", "02_PDL1.pdb", "incoming.pdb",
            "hub_canary000000000000.txt",
        ]
        assert cs.canary_staged_pdbs(listing) == sorted(
            f"{k}.pdb" for k in keys)
        data = {"target_dict_cfg": {k: {"source": "tools_hub_upload"}
                                    for k in keys}}
        assert sorted(cs.prune_canary_records(data)) == sorted(keys)


# ---------------------------------------------------------------------------
# 22. THE PRODUCED COUNT IS ITSELF A SURVIVOR COUNT — a PASS on one design,
#     one level up
#
# Moving the quorum off ``len(scorable)`` onto ``designs_produced`` closed the
# 1-of-8 hole and left the SAME defect class one step further back, because
# ``designs_produced`` is ``len(shard["designs"])`` and that list is built by
# globbing ``inference/**/*.pdb`` while SKIPPING ``filtered_out_samples`` —
# upstream's own filter bucket — and silently dropping unreadable files. So it
# counts post-filter survivors, not the ``nsamples * replicas`` the shard
# ordered, and nothing compared the two:
#
#     positive  PASS   1/1 designs (1 scorable) recall >= 0.5 ... (needed 1)
#     negative  PASS   the median negative design touches 0.00 of the 4 ...
#     null      PASS   a no-hotspot run recalls 0.00 vs 1.00 (margin 1.00)
#     OVERALL: PASS exit 0
#
# Eight designs ordered, seven filtered out by upstream, one left — and a green
# light for FLAG_TOOL_PROTEINA off a single design, for $12, with nothing in the
# report even hinting that eight were asked for. The shard is the only code that
# knows the number; it now says so, and the verdicts hold it to it.
#
# The shards below come out of the REAL ``run_shard`` body scoring REAL design
# files, so these fail if the shard stops reporting the count as well as if the
# verdicts stop enforcing it.
# ---------------------------------------------------------------------------


class TestTheProducedCountIsAlsoASurvivorCount:

    @staticmethod
    def _produced(tmp_path, n_files, label="positive"):
        """A genuine ``run_shard`` return value that emitted ``n_files``
        designs while having ordered ``_NSAMPLES * _REPLICAS``."""
        namespace = _shard_namespace(tmp_path, design_files=[
            (f"sample_{i}.pdb", CORRECT_DESIGN_PDB) for i in range(n_files)])
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, label, ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        assert out.get("error") is None, out.get("error")
        assert len(out["designs"]) == n_files
        return out

    def test_a_shard_that_emitted_one_design_is_not_a_pass(self, tmp_path):
        """THE HEADLINE CASE, in the exact shape QC observed: one design file,
        exit 0, on the requested patch. Against the shipped code this returned
        ``PASS 1/1 designs (1 scorable) ... (needed 1)`` and exit 0."""
        shard = self._produced(tmp_path, 1)
        assert shard["exit_code"] == 0
        assert shard["n_designs_expected"] == 8, (
            "the shard must report the count it ORDERED, not the count it kept")
        assert shard["hotspot_recall_median"] == 1.0, (
            "the one design really is on the patch; this is the case that used "
            "to pass")
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome != cs.PASS, verdict.reason
        assert verdict.outcome == cs.INCONCLUSIVE, (
            "upstream discarding most of the samples is not evidence the "
            f"feature is broken either; got {verdict.outcome}: {verdict.reason}")
        assert verdict.metrics["n_designs_expected"] == 8
        assert verdict.metrics["n_designs"] == 1

    def test_the_whole_phase_two_report_refuses_that_run(self, tmp_path):
        """...and it reaches the exit code, which is what any script wrapped
        around this harness reads. Named per control, because "the report is
        not a PASS" can be satisfied by some OTHER shard failing."""
        pos = self._produced(tmp_path, 1, "positive")
        neg = dict(pos, label="negative", cross_hotspot_recall_median=0.0,
                   centroid_distance_median=1.0)
        null = dict(pos, label="null", cross_hotspot_recall_median=0.0)
        report = cs.phase2_report(pos, neg, null)
        by_name = {v.name: v for v in report["verdicts"]}
        for name in ("positive", "negative", "null"):
            assert by_name[name].outcome == cs.INCONCLUSIVE, (
                f"the {name} control blessed a one-design run: "
                f"{by_name[name].outcome} — {by_name[name].reason}")
        assert report["overall"] != cs.PASS
        assert report["exit_code"] != 0, "phase 2 exited 0 on one design"

    def test_the_boundary_sweep_that_used_to_pass(self):
        """QC's sweep: ``(N=1,s=1,on=1) -> required=1 -> PASS``, and the same
        at 2 and 3. Pinned at the boundary rather than by re-deriving
        ``ceil(min_hit_fraction * expected)``, for the reason given in
        ``test_the_hit_threshold_scales_with_the_scorable_design_count``."""
        def emitted(k):
            """k of the 8 ordered designs came back, all on the patch."""
            return cs.positive_verdict(_shard(n=k, recall=1.0, centroid=1.0,
                                              expected=8))
        for k in (1, 2, 3, 4, 5):
            verdict = emitted(k)
            assert verdict.outcome == cs.INCONCLUSIVE, (
                f"{k} of 8 ordered designs returned {verdict.outcome}: "
                f"{verdict.reason}")
        # Six of the eight ordered is the boundary: from there the PASS quorum
        # for a run of eight is reachable, and a run that reaches it passes.
        for k in (6, 7, 8):
            assert emitted(k).outcome == cs.PASS, emitted(k).reason

    def test_the_floor_moves_with_the_count_the_shard_ordered(self):
        """Not a hardcoded 8. A shard that ordered four is judged against four,
        and one that ordered sixteen against sixteen."""
        assert cs.positive_verdict(
            _shard(n=3, recall=1.0, centroid=1.0, expected=4)).outcome == cs.PASS
        assert cs.positive_verdict(
            _shard(n=3, recall=1.0, centroid=1.0, expected=16)).outcome == (
            cs.INCONCLUSIVE)

    def test_the_negative_control_cannot_be_blessed_by_one_produced_design(
            self, tmp_path):
        """A negative control filtered down to one file reports a median cross
        recall of 0.00 — a textbook clean negative — off one design."""
        neg = self._produced(tmp_path, 1, "negative")
        neg["cross_hotspot_recall_median"] = 0.0
        neg["centroid_distance_median"] = 1.0
        verdict = cs.negative_verdict(neg)
        assert verdict.outcome != cs.PASS, verdict.reason
        assert verdict.outcome == cs.INCONCLUSIVE
        assert verdict.metrics["n_designs_expected"] == 8
        assert verdict.metrics["n_designs"] == 1

    def test_the_null_margin_cannot_rest_on_one_produced_design_per_side(self):
        """The margin is the single number that says the hotspots were not
        silently dropped. Either side being a one-file run is enough to sink
        it."""
        fat_pos = _shard("positive", n=8, cross=1.0, expected=8)
        fat_null = _shard("null", n=8, cross=0.0, expected=8)
        assert cs.null_verdict(fat_pos, fat_null).outcome == cs.PASS

        thin_pos = _shard("positive", n=1, cross=1.0, expected=8)
        thin_null = _shard("null", n=1, cross=0.0, expected=8)
        assert cs.null_verdict(thin_pos, fat_null).outcome == cs.INCONCLUSIVE, (
            "one produced positive design decided the margin")
        assert cs.null_verdict(fat_pos, thin_null).outcome == cs.INCONCLUSIVE, (
            "one produced null design decided the margin")

    def test_the_expected_count_is_the_one_the_design_command_asked_for(
            self, tmp_path):
        """Both numbers come from the same two constants, so they cannot
        describe different runs. Checked by MOVING them: a bare assertion that
        the product is 8 would pass against a hardcoded 8."""
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        fake = namespace["_fake_rp"]

        def run():
            return namespace["run_shard"](
                INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
                False, ["A1", "A2"])

        out = run()
        sent = fake.design_cmd_kwargs[-1]
        assert out["n_designs_expected"] == sent["nsamples"] * sent["replicas"]
        assert (sent["nsamples"], sent["replicas"]) == (4, 2), (
            "the shard no longer orders nsamples=4 x replicas=2; the floor "
            "follows it, but the operator's expectation of 8 does not")

        # ...and it FOLLOWS them rather than agreeing with them by luck.
        namespace["_NSAMPLES"], namespace["_REPLICAS"] = 3, 5
        moved = run()
        sent = fake.design_cmd_kwargs[-1]
        assert (sent["nsamples"], sent["replicas"]) == (3, 5)
        assert moved["n_designs_expected"] == 15, (
            "n_designs_expected is hardcoded; it must be derived from what the "
            "shard actually asked for")

    def test_a_shard_that_does_not_say_what_it_ordered_is_inconclusive(self):
        """No denominator, no verdict — the same rule the cross-reference patch
        size already lives by. Assuming 8 because 8 is what the code passes
        today puts an invented number under a $12 decision, and it would let a
        refactor that drops the key restore the defect in silence."""
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        neg = _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0)
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.0)
        assert cs.phase2_report(pos, neg, null)["overall"] == cs.PASS, (
            "the same trio WITH the count is the run phase 2 exists to bless")
        for shard in (pos, neg, null):
            shard.pop("n_designs_expected")
        report = cs.phase2_report(pos, neg, null)
        assert [v.outcome for v in report["verdicts"]] == [cs.INCONCLUSIVE] * 3
        assert "did not report how many designs" in report["verdicts"][0].reason

    def test_a_nonsense_expectation_is_treated_as_no_expectation(self):
        """"0 designs were ordered" makes the floor vacuous, which is the
        permissive answer wearing a number."""
        for bad in (0, -4, "eight", None):
            shard = _shard(n=1, recall=1.0, centroid=1.0)
            shard["n_designs_expected"] = bad
            assert cs.designs_expected(shard) is None, bad
            assert cs.positive_verdict(shard).outcome == cs.INCONCLUSIVE, bad

    def test_the_diagnostics_separate_a_filtered_run_from_an_unverified_one(
            self):
        """The two thin runs prescribe DIFFERENT next moves — read upstream's
        filter log, versus read the per-design chain map — and each reads as
        "1 design" without the pair of counts."""
        filtered = _shard(n=1, recall=1.0, centroid=1.0, expected=8)
        unverified = _shard(n=8, recall=1.0, centroid=1.0, expected=8)
        for design in unverified["designs"][1:]:
            design.update(target_verified=False)
            design.pop("hotspot_recall", None)

        a = " ".join(cs.verdict_diagnostics(cs.positive_verdict(filtered)))
        b = " ".join(cs.verdict_diagnostics(cs.positive_verdict(unverified)))
        assert "designs requested 8" in a and "designs produced 1" in a
        assert "designs requested 8" in b and "designs produced 8" in b
        assert "scorable 1" in a and "scorable 1" in b
        assert a != b, (
            "upstream filtering 7 of 8 and us failing to verify 7 of 8 render "
            "identically; the operator cannot tell which file to open")

    def test_the_verdict_text_says_how_many_were_ordered(self):
        """"6/8 designs" means one thing if 8 was ordered and another if 8 is
        what survived a filter. The console must not make the operator guess."""
        verdict = cs.positive_verdict(
            _shard(n=8, recall=1.0, centroid=1.0, expected=8))
        assert verdict.outcome == cs.PASS
        assert "8 requested" in verdict.reason, verdict.reason

    def test_a_full_run_is_still_the_pass_phase_two_exists_to_bless(
            self, tmp_path):
        """The fix must not be a blanket refusal."""
        shard = self._produced(tmp_path, 8)
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome == cs.PASS, verdict.reason
        assert verdict.metrics["n_designs_expected"] == 8
        assert verdict.metrics["n_designs"] == 8

    def test_extra_files_of_an_ordered_design_are_not_penalised(self):
        """Upstream writing MORE FILES than were ordered — a refold artifact,
        a second copy of a sample — is not a reason to refuse: the duplicates
        collapse onto the design they are copies of and the shard is scored as
        the run it ordered.

        THIS TEST USED TO SAY SOMETHING ELSE, AND THE THING IT SAID WAS WRONG.
        It asserted ``_shard(n=10, expected=8)`` is a PASS, reasoning that "the
        bar is the larger of the two counts either way, so this can only be
        stricter". That holds only if the 10 records are 10 INDEPENDENT designs.
        They are not, in the case that matters: ``designs_produced`` counted
        files, so 10 files of one design also read as 10, and the inflation
        lands on ``produced >= hit_quorum(expected)`` — the one absolute floor
        phase 2 has, and the only comparison uniform duplication does not cancel
        out of. The sibling test below pins the new answer for 10 genuinely
        distinct designs.
        """
        shard = _shard(n=10, recall=1.0, centroid=1.0, expected=8,
                       name="sample_0.pdb")
        assert cs.design_files(shard) == 10
        assert cs.designs_produced(shard) == 1, (
            "ten files of one name are one design")
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason

        # Eight ordered, eight designs, two of them written twice.
        ten_files_eight_designs = _shard(n=8, recall=1.0, centroid=1.0,
                                         expected=8)
        ten_files_eight_designs["designs"].extend(
            dict(d) for d in ten_files_eight_designs["designs"][:2])
        assert cs.design_files(ten_files_eight_designs) == 10
        assert cs.designs_produced(ten_files_eight_designs) == 8
        assert cs.positive_verdict(ten_files_eight_designs).outcome == cs.PASS

    def test_more_distinct_designs_than_were_ordered_is_inconclusive(self):
        """Ten DIFFERENT designs out of an order for eight is not a stricter
        bar, it is two records nobody paid for sitting inside the numerator of
        the absolute floor. What they are is not guessable, so the run is
        unmeasured rather than blessed."""
        verdict = cs.positive_verdict(
            _shard(n=10, recall=1.0, centroid=1.0, expected=8))
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
        assert "asked for 8 designs and came back with 10" in verdict.reason

    def test_the_weakest_reachable_pass_is_bounded_and_stated(self):
        """WHAT A PASS NOW MEANS, AT ITS WEAKEST, exhaustively — because that
        is the claim the production flag is turned on against.

        Pre-fix the answer was ONE design (1 produced, 1 scorable, 1 on patch).
        The absolute floor puts a bound on it, and the bound is the number to
        argue about, so it is measured here rather than reasoned about.
        """
        passes = []
        for produced in range(0, 13):
            for scorable in range(0, produced + 1):
                for on_patch in range(0, scorable + 1):
                    shard = TestTheThresholdsCannotDisagree._shard(
                        produced, scorable, on_patch)
                    shard["n_designs_expected"] = 8
                    if cs.positive_verdict(shard).outcome == cs.PASS:
                        passes.append((produced, scorable, on_patch))
        assert passes, "nothing passes any more; the floor is a blanket refusal"
        assert min(p[0] for p in passes) == 6, (
            "a PASS is reachable with fewer than 6 of the 8 ordered designs "
            "even present")
        assert min(p[2] for p in passes) == 5, (
            "a PASS rests on fewer than 5 designs demonstrably on the "
            f"requested patch: weakest is {min(passes, key=lambda p: p[2])}")

    def test_phase_one_warns_before_the_twelve_dollar_run(self):
        """The $4 gate's job. Phase 1 asserts WIRING and does not fail on a thin
        yield — but "upstream kept 1 of the 8 we ordered" is the fact that
        decides whether phase 2 is worth starting, and it was in the JSON with
        nothing pointing at it."""
        thin = _shard("phase1", n=1, recall=1.0, centroid=0.0, expected=8)
        lines = cs.designs_yield_note(thin)
        assert lines, "a shard that lost 7 of 8 designs said nothing about it"
        text = " ".join(lines)
        assert "1 of the 8" in text and "filtered_out_samples" in text
        assert "INCONCLUSIVE" in text, (
            "the note must say what phase 2 would do, or it is trivia")
        # A full run's console is byte-for-byte what it was.
        assert cs.designs_yield_note(
            _shard("phase1", n=8, expected=8)) == []
        assert cs.designs_yield_note(_shard("phase1", n=10, expected=8)) == []
        # ...and a shard that does not report the order says nothing rather
        # than guessing one.
        unknown = _shard("phase1", n=1)
        unknown.pop("n_designs_expected")
        assert cs.designs_yield_note(unknown) == []
        assert cs.designs_yield_note(None) == []

    def test_the_operator_sees_the_yield_note_in_phase_one(self, monkeypatch,
                                                           tmp_path):
        """The rendering is worth nothing if no tail prints it — the shape of
        every defect this file exists to catch. Driven through the REAL
        ``main``."""
        res = _shard("phase1", n=1, recall=1.0, centroid=0.0, expected=8)
        res["hydra"] = {"task_name_selected": True, "hotspots_match": True,
                        "hotspots_order_matches": True}
        _raw, stream = _console("utf-8")
        monkeypatch.setattr(sys, "stdout", cs.harden_stream(stream))
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, phase0=_Remote(result={}),
            run_shard=_Remote(handles=[_Handle(result=res)]))
        namespace["main"](phase=1, target_pdb=str(target), hotspots="A1 A2")
        stream.flush()
        printed = _raw.getvalue().decode("utf-8")
        assert "1 of the 8 designs this shard ordered came back" in printed, (
            "phase 1 collected the count and never showed it to anyone; the "
            "operator learns it from three INCONCLUSIVE verdicts and ~$12")


# ---------------------------------------------------------------------------
# 23. TWO KNOBS THAT COULD DISAGREE, AND A COUNT PAIRED WITH THE WRONG MEDIAN
#
# (a) ``Thresholds`` is a public frozen dataclass with two independently
#     settable fractions, and the "an unreachable quorum is INCONCLUSIVE, never
#     FAIL" design holds only while ``min_hit_fraction <= min_scorable_fraction``.
#     Raising the first alone gave:
#
#         N=8 scorable=6 on_patch=6 -> FAIL (required: 8, scorable: 6)
#
#     Every design that could be measured landed on the patch and the verdict
#     CONDEMNED the feature. Latent, because the defaults are equal — and the
#     one knob in here that can produce a wrong answer silently.
#
# (b) ``null_verdict`` chose the positive median by "is the cross median None?"
#     and the positive COUNT by "is the cross-scorable count falsy?". Two tests
#     for one decision, agreeing only because ``run_shard`` cannot currently
#     emit a non-None median over zero designs. A shard that did would have had
#     the thin gate applied to a count belonging to the other measurement.
# ---------------------------------------------------------------------------


class TestTheThresholdsCannotDisagree:

    @staticmethod
    def _shard(produced, scorable, on_patch):
        out = _shard(n=produced, recall=1.0, centroid=1.0, expected=produced)
        for design in out["designs"][scorable:]:
            design.update(target_verified=False)
            design.pop("hotspot_recall", None)
            design.pop("centroid_distance", None)
        for design in out["designs"][on_patch:scorable]:
            design["hotspot_recall"] = 0.0
        return out

    def test_a_hit_fraction_above_the_scorable_floor_is_refused(self):
        """The combination that produced a $12 wrong FAIL. This module raises
        rather than let a wrong answer through — ``Verdict.__bool__`` and
        ``Verdict.__eq__`` both do — and this is the one knob that could."""
        with pytest.raises(ValueError) as excinfo:
            cs.Thresholds(min_hit_fraction=1.0)
        message = str(excinfo.value)
        assert "min_hit_fraction" in message and "min_scorable_fraction" in message
        assert "unreachable" in message

    def test_the_shipped_defaults_are_admissible(self):
        assert cs.DEFAULT_THRESHOLDS.min_hit_fraction == 0.75
        assert cs.DEFAULT_THRESHOLDS.min_scorable_fraction == 0.75
        assert cs.Thresholds() == cs.DEFAULT_THRESHOLDS

    def test_a_scorable_floor_above_the_hit_fraction_is_fine(self):
        """The safe direction: demanding MORE measurable designs than the
        quorum needs can only turn a FAIL into an INCONCLUSIVE."""
        loose = cs.Thresholds(min_hit_fraction=0.5, min_scorable_fraction=0.9)
        assert loose.min_hit_fraction == 0.5
        assert cs.positive_verdict(self._shard(8, 8, 4), loose).outcome == cs.PASS
        assert cs.positive_verdict(self._shard(8, 6, 6), loose).outcome == (
            cs.INCONCLUSIVE)

    def test_the_strict_quorum_is_reachable_once_both_knobs_move(self):
        """The operator who wanted "every design must land" gets it, and the
        run where six of eight could be measured is INCONCLUSIVE — unmeasured —
        rather than CONDEMNED."""
        strict = cs.Thresholds(min_hit_fraction=1.0, min_scorable_fraction=1.0)
        assert cs.positive_verdict(self._shard(8, 8, 8), strict).outcome == cs.PASS
        assert cs.positive_verdict(self._shard(8, 8, 7), strict).outcome == cs.FAIL
        verdict = cs.positive_verdict(self._shard(8, 6, 6), strict)
        assert verdict.outcome == cs.INCONCLUSIVE, (
            "every measurable design landed on the patch and the verdict "
            f"condemned the feature: {verdict.reason}")

    def test_the_invariant_holds_for_every_admissible_pair(self):
        """The property, not the message: below the scorable floor the quorum
        must never be reachable-but-missed, i.e. never a FAIL."""
        for hit, floor in ((0.75, 0.75), (0.5, 0.5), (0.5, 0.75), (0.25, 1.0),
                           (1.0, 1.0)):
            thresholds = cs.Thresholds(min_hit_fraction=hit,
                                       min_scorable_fraction=floor)
            for produced in range(1, 13):
                for scorable in range(1, produced + 1):
                    verdict = cs.positive_verdict(
                        self._shard(produced, scorable, scorable), thresholds)
                    assert verdict.outcome != cs.FAIL, (
                        f"every scorable design landed and the verdict was "
                        f"FAIL at hit={hit} floor={floor} produced={produced} "
                        f"scorable={scorable}: {verdict.reason}")


class TestTheNullCountMatchesItsMedian:

    def test_the_count_and_the_median_come_from_the_same_field(self):
        """A cross median with nothing under it. The count used to fall back to
        the OTHER metric's scorable set — 8 — so the thin gate passed and the
        margin was blessed by designs that were never part of it."""
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=None)
        assert cs.scorable_designs(pos, "cross_hotspot_recall") == [], (
            "no design carries a cross recall")
        pos["cross_hotspot_recall_median"] = 0.9      # a median over nothing
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.0)

        verdict = cs.null_verdict(pos, null)
        assert verdict.metrics["positive_recall_field"] == "cross_hotspot_recall"
        assert verdict.metrics["n_scorable_positive"] == 0, (
            "the count belongs to hotspot_recall while the median under the "
            "verdict is a cross recall")
        assert verdict.outcome != cs.PASS, verdict.reason
        assert verdict.outcome == cs.INCONCLUSIVE

    def test_the_fallback_to_own_recall_pairs_with_its_own_count(self):
        """A REGRESSION GUARD, not a reproduction — it passes before the fix as
        well. When there is genuinely no cross median the margin falls back to
        the positive shard's own recall, and the count must follow it there."""
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=None)
        assert pos["cross_hotspot_recall_median"] is None
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.0)
        verdict = cs.null_verdict(pos, null)
        assert verdict.metrics["positive_recall_field"] == "hotspot_recall"
        assert verdict.metrics["n_scorable_positive"] == 8
        assert verdict.outcome == cs.PASS, verdict.reason

    def test_the_ordinary_cross_scored_case_is_unchanged(self):
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.0)
        verdict = cs.null_verdict(pos, null)
        assert verdict.metrics["positive_recall_field"] == "cross_hotspot_recall"
        assert verdict.metrics["n_scorable_positive"] == 8
        assert verdict.outcome == cs.PASS


# ---------------------------------------------------------------------------
# 24. A FILE IS NOT A DESIGN, A CROPPED VIEW IS NOT A CLEAN NEGATIVE, AND AN
#     ABSENT VERIFICATION IS NOT A VERIFICATION
#
# The third round of QC on this harness, and the third false PASS found in the
# same spot — each one inside the fix for the last:
#
#   1. the quorum was normalised onto ``len(scorable)``      -> PASS on 1-of-8
#   2. the fix moved it onto ``designs_produced``, itself a
#      survivor count                                        -> PASS on 1-of-1
#   3. ``designs_produced`` counted FILES, not designs        -> this section
#
# (F1) ``run_shard`` globs ``inference/**/*.pdb`` with no de-duplication, so
#      every phase-2 gate counted files. QC measured, with 8 ordered:
#
#          unique  copies  files  overall
#               1       6      6  PASS      <- ONE design, six paths, exit 0
#               2       3      6  PASS
#               2       4      8  PASS
#          control, no duplication: 1,2,3,5 -> INCONCLUSIVE ; 6 -> PASS
#
#      Uniform duplication cancels out of every fraction — it is invisible in
#      ``on_patch / produced`` — and shows up only in ``produced >=
#      hit_quorum(expected)``, the one absolute floor phase 2 has. Production
#      already refuses this assumption: ``run_pipeline`` uses the byte-identical
#      glob and guards its index pairing on ``len(all_pdbs) == total_rows``.
#
# (F2) ``score_from_contacts`` divides by every REQUESTED hotspot while its
#      numerator can only count residues present in the design's output, and the
#      identity gate deliberately admits a design that CROPS the target. A
#      negative design carrying 1 of the 4 positive hotspots and sitting on it
#      scored recall 0.25, which ``negative_verdict`` turned back into "touches
#      1.00 of the 4" and PASSED — when it had touched 100% of everything that
#      was measurable. ``requested_found_in_structure`` was computed per design
#      and no verdict consulted it; for the cross patch it was not even kept.
#
# (F3) ``scorable_designs`` tested ``target_verified is not False``, so a design
#      with the key simply OMITTED was scorable. Eight of those made the
#      positive, negative and null verdicts all PASS. The docstring claims the
#      gate holds against "a future caller, a merge, a hand-built dict"; an
#      omitted key is the likeliest shape of all and ``is not False`` did not
#      hold against it.
# ---------------------------------------------------------------------------


def _rewards_csv(nrows):
    """An ``all_rewards`` table with ``nrows`` sample rows, as upstream writes."""
    return "\n".join(["sample_id,reward"]
                     + [f"{i},0.5" for i in range(nrows)]) + "\n"


class TestAFileIsNotADesign:
    """F1, driven through the REAL ``run_shard`` body over real files."""

    @staticmethod
    def _duplicated(tmp_path, unique, copies, label="positive", csv_rows=None):
        """A genuine ``run_shard`` return value over ``unique`` design names,
        each written into ``copies`` different directories."""
        files = [(f"copy_{c}/sample_{u}.pdb", CORRECT_DESIGN_PDB)
                 for c in range(copies) for u in range(unique)]
        if csv_rows is not None:
            files.append(("all_rewards_canary.csv", _rewards_csv(csv_rows)))
        namespace = _shard_namespace(tmp_path, design_files=files)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, label, ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        assert out.get("error") is None, out.get("error")
        return out

    def test_one_design_written_six_times_is_not_a_pass(self, tmp_path):
        """QC'S EXACT SCENARIO. 8 ordered, ONE design, six paths. Against the
        pre-fix code this returned ``6/6 designs (8 requested, 6 scorable)``,
        PASS, exit 0 — a green light for FLAG_TOOL_PROTEINA off one design."""
        shard = self._duplicated(tmp_path, unique=1, copies=6)
        assert shard["n_designs_expected"] == 8
        assert len(shard["designs"]) == 6, "six files really were globbed"
        assert cs.design_files(shard) == 6
        assert cs.designs_produced(shard) == 1, (
            "six copies of one name are one design")
        assert cs.duplicate_design_names(shard) == ["sample_0.pdb x6"]

        verdict = cs.positive_verdict(shard)
        assert verdict.outcome != cs.PASS, verdict.reason
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
        assert verdict.metrics["n_designs"] == 1
        assert verdict.metrics["n_design_files"] == 6

    def test_the_whole_phase_two_report_refuses_it(self, tmp_path):
        """...and it reaches the exit code, which is what any wrapper reads."""
        pos = self._duplicated(tmp_path, unique=1, copies=6, label="positive")
        neg = dict(pos, label="negative", cross_hotspot_recall_median=0.0,
                   centroid_distance_median=1.0)
        null = dict(pos, label="null", hotspot_recall_median=0.0,
                    cross_hotspot_recall_median=0.0,
                    centroid_distance_median=1.0)
        report = cs.phase2_report(pos, neg, null)
        assert [v.outcome for v in report["verdicts"]] == [cs.INCONCLUSIVE] * 3
        assert report["overall"] == cs.INCONCLUSIVE
        assert report["exit_code"] == 3

    def test_the_measured_duplication_table(self, tmp_path):
        """Every row QC measured, and the undulplicated control beside it. The
        three duplicated rows were PASS / exit 0 before the fix."""
        for (unique, copies), n_files in {(1, 6): 6, (2, 3): 6, (2, 4): 8}.items():
            shard = self._duplicated(tmp_path / f"d{unique}x{copies}",
                                     unique, copies)
            assert len(shard["designs"]) == n_files
            assert cs.designs_produced(shard) == unique
            outcome = cs.positive_verdict(shard).outcome
            assert outcome == cs.INCONCLUSIVE, (unique, copies, outcome)

        # The control: no duplication at all. 1/2/3/5 are under the floor of 6
        # and 6 clears it — unchanged by the fix, so it is not a blanket refusal.
        for n, expected in ((1, cs.INCONCLUSIVE), (2, cs.INCONCLUSIVE),
                            (3, cs.INCONCLUSIVE), (5, cs.INCONCLUSIVE),
                            (6, cs.PASS), (8, cs.PASS)):
            shard = self._duplicated(tmp_path / f"c{n}", unique=n, copies=1)
            assert cs.designs_produced(shard) == n
            outcome = cs.positive_verdict(shard).outcome
            assert outcome == expected, (n, outcome)

    def test_the_operator_can_see_the_two_counts(self, tmp_path):
        """QC's shard printed ``6/6 designs (8 requested, 6 scorable)`` while
        one name accounted for all six, and nothing showed that. A PASS prints
        no diagnostic line, so the file count has to reach the verdict TEXT."""
        # 8 ordered, 8 designs, 2 of them written a second time -> still a PASS,
        # and the sentence says where the extra files went.
        files = [(f"copy_0/sample_{i}.pdb", CORRECT_DESIGN_PDB) for i in range(8)]
        files += [(f"copy_1/sample_{i}.pdb", CORRECT_DESIGN_PDB) for i in range(2)]
        namespace = _shard_namespace(tmp_path, design_files=files)
        shard = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome == cs.PASS, verdict.reason
        assert "from 10 design files" in verdict.reason, verdict.reason

        # ...and a run with no duplication says nothing extra, so a healthy
        # console is byte-for-byte what it was.
        clean = self._duplicated(tmp_path / "clean", unique=8, copies=1)
        assert "design files" not in cs.positive_verdict(clean).reason

    def test_the_diagnostic_line_carries_the_file_and_row_counts(self):
        """A non-PASS renders them too, next to the design count, because
        "produced 6 | design files 6" and "produced 1 | design files 6" are the
        same run to every ratio in the report."""
        shard = _shard(n=5, recall=1.0, centroid=1.0, expected=8)
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
        line = " ".join(cs.verdict_diagnostics(verdict))
        assert "designs produced 5" in line
        assert "design files 5" in line
        assert "reward-table rows n/a" in line


class TestTheRewardTableIsTheIndependentWitness:
    """The second half of F1: assert the design count against a number the file
    layout cannot move."""

    def test_the_row_count_is_collected_in_phase_two_not_only_under_dump_tree(
            self, tmp_path):
        """It used to be gated on ``dump_tree``, and phase 2 spawns with
        ``dump_tree=False`` — so the witness was collected precisely nowhere
        that spends money."""
        files = [(f"sample_{i}.pdb", CORRECT_DESIGN_PDB) for i in range(8)]
        files.append(("all_rewards_canary.csv", _rewards_csv(8)))
        namespace = _shard_namespace(tmp_path, design_files=files)
        shard = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        assert "csv_files" in shard, "phase 2 collected no CSV at all"
        assert cs.design_table_rows(shard) == 8
        assert cs.positive_verdict(shard).outcome == cs.PASS

    def test_more_designs_than_the_reward_table_scored_is_inconclusive(self):
        """The file layout says 10 designs, upstream's own sample table says 8.
        Which is the denominator is then unknown, and every phase-2 gate divides
        by it."""
        shard = _shard(n=10, recall=1.0, centroid=1.0, expected=10)
        shard["csv_files"] = {"/x/inference/all_rewards_c.csv":
                              {"columns": ["a"], "nrows": 8}}
        assert cs.design_table_rows(shard) == 8
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
        assert "reward table lists only 8" in verdict.reason
        assert verdict.metrics["n_design_rows"] == 8

    def test_fewer_designs_than_rows_is_the_ordinary_filtered_run(self):
        """ONE-DIRECTIONAL ON PURPOSE. Upstream filtering samples into
        ``filtered_out_samples`` — which the glob skips by design — leaves fewer
        files than rows, and ``missing_designs_reason`` already holds that
        against the count the shard ORDERED. Refusing it here as well would cost
        $12 on a case that is handled."""
        shard = _shard(n=8, recall=1.0, centroid=1.0, expected=8)
        shard["csv_files"] = {"/x/inference/all_rewards_c.csv": {"nrows": 12}}
        assert cs.design_count_disagreement(shard) is None
        assert cs.positive_verdict(shard).outcome == cs.PASS

    def test_the_design_table_is_chosen_by_name_not_by_being_first(self):
        """``top_samples`` is a ranked SUBSET and ``timing`` is not a design
        table at all; taking whichever CSV came back first is a denominator
        nobody picked."""
        shard = _shard(n=8, recall=1.0, centroid=1.0, expected=8)
        shard["csv_files"] = {
            "/x/inference/all_rewards_canary_0.csv": {"nrows": 8},
            "/x/inference/rewards_canary_0.csv": {"nrows": 8},
            "/x/inference/top_samples_canary.csv": {"nrows": 3},
            "/x/inference/timing_0.csv": {"nrows": 1},
        }
        assert cs.design_table_rows(shard) == 8, (
            "the top-k table or the timing table was taken for the sample list")
        assert cs.positive_verdict(shard).outcome == cs.PASS

    def test_an_ambiguous_or_absent_table_is_not_guessed_and_not_fatal(self):
        """None, never a number — and never a refusal on its own. The absolute
        floor against ``n_designs_expected`` still stands, and a missing
        corroboration must not be able to cost $12 by itself."""
        base = _shard(n=8, recall=1.0, centroid=1.0, expected=8)
        assert cs.design_table_rows(base) is None
        assert cs.positive_verdict(base).outcome == cs.PASS
        for csvs in ({}, {"/x/all_rewards_a.csv": {"nrows": 8},
                          "/x/y/all_rewards_b.csv": {"nrows": 4}},
                     {"/x/all_rewards_a.csv": {"nrows": 0}},
                     {"/x/all_rewards_a.csv": {"error": "boom"}},
                     {"/x/all_rewards_a.csv": {"nrows": "eight"}},
                     {"/x/timing_0.csv": {"nrows": 8}}):
            shard = dict(base, csv_files=csvs)
            assert cs.design_table_rows(shard) is None, csvs
            assert cs.positive_verdict(shard).outcome == cs.PASS, csvs


class TestACroppedViewIsNotACleanNegative:
    """F2. The negative control PASSed because it could not see the hotspots."""

    @staticmethod
    def _cropped_design():
        """A design carrying A1 and A10..A20 of the target: it VERIFIES (12
        matched keys, 100% coverage, 100% identity) and only ONE of the four
        positive hotspots A1..A4 exists in it at all. The binder sits on its own
        far patch (A10, A11) and on A1."""
        lines = [_atom(1, "CA", _TARGET_SEQ[0], "A", 1, 0.0, 0.0, 0.0),
                 _atom(2, "CA", _TARGET_SEQ[9], "A", 10, 6.0, 0.0, 0.0),
                 _atom(3, "CA", _TARGET_SEQ[10], "A", 11, 12.0, 0.0, 0.0)]
        for i, res in enumerate(range(12, 21)):
            lines.append(_atom(10 + i, "CA", _TARGET_SEQ[res - 1], "A", res,
                               60.0 + i * 4.0, 0.0, 0.0))
        for i, x in enumerate((0.0, 6.0, 12.0)):
            lines.append(_atom(200 + i, "CA", "GLY", "B", i + 1, x, 4.0, 0.0))
        return "\n".join(lines) + "\n"

    def _negative_shard(self):
        entry = cs.score_design_file(
            self._cropped_design(), {"A"}, ["A10", "A11"],
            ["A1", "A2", "A3", "A4"], TARGET_REFERENCE)
        designs = [dict(entry, name=f"sample_{i}.pdb") for i in range(8)]
        return entry, {
            "label": "negative", "exit_code": 0, "designs": designs,
            "n_designs_expected": 8, "n_complexes": 8, "n_target_verified": 8,
            "cross_reference_hotspots": ["A1", "A2", "A3", "A4"],
            "hotspot_recall_median": cs.median(
                d.get("hotspot_recall") for d in designs),
            "centroid_distance_median": cs.median(
                d.get("centroid_distance") for d in designs),
            "cross_hotspot_recall_median": cs.median(
                d.get("cross_hotspot_recall") for d in designs),
        }

    def test_the_crop_really_does_dilute_the_cross_recall(self):
        """The premise, measured rather than asserted: the design verifies, the
        binder touches the only positive hotspot it contains, and the recall
        comes back 0.25 because the denominator is all four."""
        entry, _ = self._negative_shard()
        assert entry["target_verified"] is True, "a crop is admitted on purpose"
        assert entry["contact_residues"] == ["A1", "A10", "A11"]
        assert entry["cross_hotspot_recall"] == 0.25
        assert entry["cross_requested_found_in_structure"] == 1, (
            "one of the four positive hotspots is in this structure, and it is "
            "the one the binder is sitting on")

    def test_a_negative_control_that_could_not_see_the_patch_does_not_pass(self):
        """Against the pre-fix code: ``PASS — the median negative design touches
        1.00 of the 4 positive hotspots (max 1)``, on a design that touched 100%
        of what was measurable."""
        _, neg = self._negative_shard()
        verdict = cs.negative_verdict(neg)
        assert verdict.outcome != cs.PASS, verdict.reason
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
        assert verdict.metrics["cross_hotspots_touched_median"] == 1.0
        assert verdict.metrics["cross_hotspots_visible_median"] == 1
        assert verdict.metrics["cross_hotspots_unmeasurable"] == 3
        assert "cropped out" in verdict.reason

    def test_the_crop_is_visible_in_the_diagnostics(self):
        """F2's other half: the number existed per design and nothing surfaced
        it, so a crop diluted the metric silently."""
        _, neg = self._negative_shard()
        line = " ".join(cs.verdict_diagnostics(cs.negative_verdict(neg)))
        assert "positive hotspots present (median) 1" in line
        assert "positive hotspots cropped out (median) 3" in line

    def test_a_full_view_is_unchanged_and_still_passes(self):
        """The fix is arithmetically identical when the whole patch is present,
        so it cannot cost a healthy negative control its PASS."""
        neg = _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.25)
        assert neg["designs"][0]["cross_requested_found_in_structure"] == 4
        verdict = cs.negative_verdict(neg)
        assert verdict.outcome == cs.PASS, verdict.reason
        assert verdict.metrics["cross_hotspots_touched_median"] == 1.0
        assert verdict.metrics["cross_hotspots_unmeasurable"] == 0

    def test_demonstrated_overlap_is_still_a_fail_whatever_was_cropped(self):
        """A crop can only ever turn a PASS into an INCONCLUSIVE. Overlap that
        was actually SEEN is evidence, and evidence is not weakened by the parts
        we could not look at."""
        neg = _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.75,
                     cross_found=2)
        verdict = cs.negative_verdict(neg)
        assert verdict.outcome == cs.FAIL, verdict.reason

    def test_a_shard_that_does_not_report_the_coverage_is_inconclusive(self):
        """The denominator is not guessed. A payload from before the shard
        recorded it cannot be told apart from a crop, and assuming full
        visibility is exactly the invented denominator this module refuses."""
        neg = _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0)
        for design in neg["designs"]:
            design.pop("cross_requested_found_in_structure")
        verdict = cs.negative_verdict(neg)
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
        assert "does not say how many of them its designs actually CONTAIN" in (
            verdict.reason)

    def test_the_positive_side_surfaces_its_own_coverage_without_gating(self):
        """On the positive shard the same dilution pushes recall DOWN, so it can
        only cost a PASS, never fabricate one. Surfaced, not gated — and fixing
        it by changing the recall denominator would have LOOSENED the $12 PASS."""
        shard = _shard(n=8, recall=1.0, centroid=1.0, expected=8)
        for design in shard["designs"]:
            design["requested_found_in_structure"] = 2
        verdict = cs.positive_verdict(shard)
        assert verdict.outcome == cs.PASS, verdict.reason
        assert verdict.metrics["requested_found_median"] == 2


class TestAnAbsentVerificationIsNotAVerification:
    """F3."""

    def test_designs_with_the_key_omitted_are_not_scorable(self):
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        for design in pos["designs"]:
            design.pop("target_verified")
        assert cs.scorable_designs(pos, "hotspot_recall") == [], (
            "a design nothing verified reached the metrics")

    def test_the_whole_report_refuses_a_shard_nothing_verified(self):
        """Against the pre-fix code all three controls returned PASS and the
        report exited 0."""
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        neg = _shard("negative", n=8, recall=1.0, centroid=1.0, cross=0.0)
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.0)
        for shard in (pos, neg, null):
            for design in shard["designs"]:
                design.pop("target_verified")
        report = cs.phase2_report(pos, neg, null)
        assert report["overall"] != cs.PASS
        assert [v.outcome for v in report["verdicts"]] == [cs.INCONCLUSIVE] * 3
        assert report["exit_code"] == 3

    def test_a_verified_design_is_still_scorable(self):
        """Not a blanket refusal: ``run_shard`` sets the key on every complex it
        emits, so nothing production produces changes meaning."""
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        assert len(cs.scorable_designs(pos, "hotspot_recall")) == 8
        assert cs.positive_verdict(pos).outcome == cs.PASS

    def test_the_real_shard_always_sets_the_key_on_a_complex(self, tmp_path):
        """The gate is only reachable by a hand-built dict BECAUSE the shard
        fills it in — pinned here so a refactor that stops cannot be silent."""
        namespace = _shard_namespace(tmp_path, design_files=[
            (f"sample_{i}.pdb", CORRECT_DESIGN_PDB) for i in range(8)])
        shard = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        assert all(d["target_verified"] is True for d in shard["designs"])
        assert cs.positive_verdict(shard).outcome == cs.PASS


class TestTheNullMarginIsNotMadeOfCroppedHotspots:
    """F2 REACHES THE NULL VERDICT TOO — found while fixing the negative one.

    ``score_from_contacts`` divides by every REQUESTED hotspot while only those
    present in the design's output can be counted, and ``null_verdict`` compares
    two such medians. A null shard whose designs crop the target therefore
    reports a recall far below what it achieved, and the margin that number
    creates is read as proof the hotspots steered the search — the exact
    conclusion this verdict exists to REFUSE:

        null cross recall 0.25 (1 of 4 positive hotspots present, and touched)
        positive           1.00
        -> PASS, margin 0.75 > 0.25

    The no-hotspot run had landed on the patch as well as the hotspot run did.
    Only the NULL side needs the worst case: a crop on the positive side lowers
    ``pos_recall`` and shrinks the margin, so it can cost a PASS but never
    manufacture one.
    """

    def test_a_null_run_that_could_not_see_the_patch_does_not_pass(self):
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.25,
                      cross_found=1)
        verdict = cs.null_verdict(pos, null)
        assert verdict.outcome != cs.PASS, verdict.reason
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
        assert verdict.metrics["margin"] == 0.75
        assert verdict.metrics["null_recall_worst_case"] == 1.0
        assert verdict.metrics["margin_worst_case"] == 0.0
        assert "cropped out" in verdict.reason

    def test_a_full_view_null_control_is_unchanged(self):
        """Arithmetically identical when the whole patch is present, so a
        healthy run keeps its PASS and its sentence."""
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.25)
        verdict = cs.null_verdict(pos, null)
        assert verdict.outcome == cs.PASS, verdict.reason
        assert verdict.metrics["null_recall_worst_case"] == 0.25
        assert "worst case" not in verdict.reason

    def test_a_crop_small_enough_to_survive_the_worst_case_still_passes(self):
        """Not a blanket refusal: one hotspot missing out of four still leaves a
        margin of 0.75 - 0.25 = 0.50 in the worst case."""
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.0,
                      cross_found=3)
        verdict = cs.null_verdict(pos, null)
        assert verdict.outcome == cs.PASS, verdict.reason
        assert verdict.metrics["null_recall_worst_case"] == 0.25
        assert "worst case 0.25" in verdict.reason

    def test_a_measured_overlap_is_still_a_fail(self):
        """A crop can only turn a PASS into an INCONCLUSIVE. A null run measured
        to reach the patch is still the feature being a lie."""
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=1.0,
                      cross_found=1)
        verdict = cs.null_verdict(pos, null)
        assert verdict.outcome == cs.FAIL, verdict.reason
        assert "passed and ignored" in verdict.reason

    def test_a_shard_that_does_not_report_the_coverage_is_inconclusive(self):
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.0)
        for design in null["designs"]:
            design.pop("cross_requested_found_in_structure")
        verdict = cs.null_verdict(pos, null)
        assert verdict.outcome == cs.INCONCLUSIVE, verdict.reason
        assert "does not say how many of those hotspots" in verdict.reason

    def test_the_crop_is_visible_in_the_diagnostics(self):
        pos = _shard("positive", n=8, recall=1.0, centroid=0.0, cross=1.0)
        null = _shard("null", n=8, recall=1.0, centroid=1.0, cross=0.25,
                      cross_found=1)
        line = " ".join(cs.verdict_diagnostics(cs.null_verdict(pos, null)))
        assert "positive hotspots present in the null designs (median) 1" in line
        assert "margin if every cropped hotspot was touched 0" in line


# ===========================================================================
# VRAM INSTRUMENTATION PROVENANCE
# ===========================================================================
#
# The FIRST two VRAM numbers this tool produced (67,546 MB and 67,570 MB)
# turned out to be ~91% a JAX preallocation constant, because the design
# subprocess inherited JAX's default PREALLOCATE=true and reserved
# 0.75 x 81,920 = 61,440 MB on its first op whatever the target size.
# run_pipeline now builds an allocator env for its children, and three further
# readings have been taken under it — 8,943 MB at 130 aa, 15,541 at 260 and
# 25,457 at 415 — which is the whole basis of _PROTEINA's size envelope. Five
# readings, then, in two incomparable regimes, which is precisely why the
# canaries matter: they are what TAKE the measurements, so three properties of
# theirs decide whether the next reading means anything:
#
#   * the child actually gets that env (else the reading is the constant again);
#   * the poller takes a sample even when the design outlives it by a hair
#     (else a fast or early-dying shard reports peak 0, which reads as "used no
#     VRAM" rather than "was never measured");
#   * the run says which allocator policy produced it, DERIVED from the env
#     handed to the child rather than asserted, so a pre-fix and a post-fix
#     reading can never be silently compared.
#
# _design_canary.py had none of the three. It is a live paid A100 harness whose
# own docstring says its VRAM feeds the 40-vs-80GB decision, and run today it
# would have reproduced the same discredited ~67.5 GB constant.
#
# Neither canary is importable (both build a modal.App at module scope), so the
# structural half is AST over the source and the behavioural half EXECUTES the
# real function bodies in a bare namespace -- the code under test, not a copy.
# ===========================================================================

_DESIGN_CANARY_PATH = _SCORING_PATH.parent / "_design_canary.py"
_BOTH_CANARIES = [_CANARY_PATH, _DESIGN_CANARY_PATH]


def _canary_func(path, name, also=(), **injected):
    """The named function, compiled standalone from the file's real source.

    Neither canary can be imported (``modal.App`` at module scope), and a
    hand-copied duplicate of the body would be a test of the copy. Pulling the
    ``FunctionDef`` out of the parsed module and exec'ing it gives the
    production bytecode with none of the module-level imports.

    ``also`` names sibling functions to lift alongside it — the ones the target
    genuinely calls and whose real behaviour is part of what is under test
    (``_prealloc_disabled`` is always one). ``injected`` replaces names AFTER
    exec, which is how the nvidia-smi readers become constants without
    touching the body being tested. Same shape as ``load_canary_functions``,
    scoped to work on either canary file rather than only the hotspot one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {name, "_prealloc_disabled", *also}
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in wanted]
    for node in body:
        # ``@app.function(...)`` / ``@app.local_entrypoint()`` are Modal
        # plumbing, not behaviour, and ``app`` does not exist in this namespace
        # by design — same rule as ``load_canary_functions``. Without this only
        # the undecorated helpers could be lifted, which is exactly the set that
        # excludes both entrypoints.
        node.decorator_list = []
    found = {n.name for n in body}
    assert name in found, f"{name} not found in {path.name}"
    ns: dict = {"subprocess": subprocess, "threading": threading}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), ns)
    ns.update(injected)
    return ns[name]


def _poll_iteration_budget(path):
    """Worst-case seconds ONE poll iteration can take, from the source.

    The nvidia-smi calls do not all live in ``_poll_vram``: the hotspot canary
    factors them into ``_device_used_mb`` / ``_proc_used_mb``, the design
    canary inlines one. Summing the timeouts of every nvidia-smi call reachable
    from the poller covers both layouts, so the join-headroom assertion is
    about the real cost rather than about where the code happens to sit.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    poller = funcs["_poll_vram"]
    reachable = [poller] + [
        funcs[c.func.id] for c in ast.walk(poller)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        and c.func.id in funcs
    ]
    total = 0
    for fn in reachable:
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and "nvidia-smi" in ast.unparse(call):
                for kw in call.keywords:
                    if kw.arg == "timeout" and isinstance(kw.value, ast.Constant):
                        total += kw.value.value
    return total


def _func_node(path, name):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


class TestPreallocProvenanceIsDerived:
    """``vram_prealloc_disabled`` must report the RUN, not the intent."""

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_an_operator_override_is_reported_as_preallocating(self, path):
        """THE HOLE. ``design_subprocess_env`` uses ``setdefault`` so an
        operator can override any flag per run -- deliberately. Exporting
        XLA_PYTHON_CLIENT_PREALLOCATE=true therefore produces a child that DOES
        preallocate, and a hardcoded ``True`` would stamp that run as if the
        allocator fix had been in force. That is the original mislabelling with
        a certificate of authenticity on it."""
        fn = _canary_func(path, "_prealloc_disabled")
        assert fn({"XLA_PYTHON_CLIENT_PREALLOCATE": "true"}) is False
        assert fn({"XLA_PYTHON_CLIENT_PREALLOCATE": "1"}) is False

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_the_fixed_env_is_reported_as_not_preallocating(self, path):
        fn = _canary_func(path, "_prealloc_disabled")
        assert fn({"XLA_PYTHON_CLIENT_PREALLOCATE": "false"}) is True
        assert fn({"XLA_PYTHON_CLIENT_PREALLOCATE": "FALSE"}) is True

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_an_absent_flag_is_neither_true_nor_false(self, path):
        """None, not False. Absent means JAX's own default (preallocation ON)
        applies, which has the same effect as False but is a DEFAULT rather
        than a declaration -- and a reader deciding whether two readings are
        comparable should be able to see which it was."""
        fn = _canary_func(path, "_prealloc_disabled")
        assert fn({}) is None
        assert fn(None) is None
        assert fn({"SOMETHING_ELSE": "x"}) is None

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_the_real_production_env_reads_as_disabled(self, path):
        """Driven through the ACTUAL env production builds, so a change to
        ``_ALLOCATOR_ENV`` that silently stops disabling preallocation fails
        here rather than on a paid shard."""
        fn = _canary_func(path, "_prealloc_disabled")
        assert fn(rp.design_subprocess_env()) is True

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_the_flag_is_never_a_literal(self, path):
        """The structural half. ``out["vram_prealloc_disabled"] = True`` is
        what shipped, and no value-level test can catch its return."""
        node = _func_node(path, "_poll_vram")
        for assign in [n for n in ast.walk(node) if isinstance(n, ast.Assign)]:
            for tgt in assign.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == "vram_prealloc_disabled"):
                    assert not isinstance(assign.value, ast.Constant), (
                        f"{path.name} assigns a literal to "
                        f"vram_prealloc_disabled; it must be derived from the "
                        f"env the child received"
                    )


class TestThePollerAlwaysTakesASample:
    """`while not stop.is_set()` takes ZERO samples when the design finishes
    before the thread is first scheduled, and then reports peak 0 -- which reads
    as "used no VRAM" on exactly the shard whose memory you most want to see.
    Both canaries must sample first and test the flag second."""

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_the_loop_does_not_gate_the_first_sample_on_the_stop_flag(self, path):
        node = _func_node(path, "_poll_vram")
        for loop in [n for n in ast.walk(node) if isinstance(n, ast.While)]:
            assert not (
                isinstance(loop.test, ast.UnaryOp)
                and isinstance(loop.test.op, ast.Not)
            ), (
                f"{path.name}::_poll_vram still loops on `while not ...`, so a "
                f"design that outruns the poller reports peak 0 rather than "
                f"'never measured'"
            )

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_an_already_stopped_poller_still_records_a_reading(self, path):
        """The behavioural half, on the real body. The stop flag is ALREADY set
        before the poller starts -- the worst case -- and it must still produce
        a measurement rather than silence."""
        fn = _canary_func(
            path, "_poll_vram", also=("_device_used_mb", "_proc_used_mb"),
            _device_used_mb=lambda: 4321, _proc_used_mb=lambda pid: 21,
        )
        out: dict = {}
        stop = threading.Event()
        stop.set()
        fn(stop, out)
        assert "peak_vram_mb" in out, (
            f"{path.name}::_poll_vram took no sample at all when the design "
            f"had already finished"
        )
        assert out.get("vram_poll_complete") is True


class TestBothCanariesRunTheChildUnderProductionsAllocator:
    """A canary that measures a different allocator than production measures
    nothing production can act on -- which is precisely how the two existing
    readings became unusable. ``_design_canary`` was still on the old path."""

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_the_design_child_is_launched_with_an_env(self, path):
        src = path.read_text(encoding="utf-8")
        assert "rp.design_subprocess_env()" in src, (
            f"{path.name} does not build the production allocator env; its "
            f"child will inherit JAX's PREALLOCATE=true default and every "
            f"VRAM number it reports will be the 61,440 MB reservation"
        )
        tree = ast.parse(src)
        spawns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("run", "Popen")
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "subprocess"
            # nvidia-smi calls are instrumentation, not the design; identified
            # by their literal argv rather than excluded by position.
            and "nvidia-smi" not in ast.unparse(n)
        ]
        assert spawns, f"no design subprocess found in {path.name}"
        for call in spawns:
            kwargs = {kw.arg for kw in call.keywords}
            assert "env" in kwargs, (
                f"{path.name}:{call.lineno} spawns the design without env=, so "
                f"it runs under JAX's default allocator: "
                f"{ast.unparse(call)[:90]}"
            )


class TestTheJoinOutlastsTheFinalSample:
    """``poller.join(timeout=10)`` could expire mid-sample -- after
    ``stop.set()`` the poller still has a full iteration of nvidia-smi calls,
    each capped at 10 s -- leaving every VRAM key None, which reads as "never
    measured" on a shard that measured fine."""

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_the_join_timeout_clears_one_full_poll_iteration(self, path):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        joins = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "join"
            and any(kw.arg == "timeout" for kw in n.keywords)
        ]
        assert joins, f"no poller join with a timeout found in {path.name}"
        needed = _poll_iteration_budget(path)
        assert needed > 0, f"no nvidia-smi timeout found in {path.name}"
        declared = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (isinstance(tgt, ast.Name)
                            and tgt.id == "_VRAM_JOIN_TIMEOUT_S"
                            and isinstance(node.value, ast.Constant)):
                        declared = node.value.value
        assert declared is not None, (
            f"{path.name} has no named join timeout constant"
        )
        assert declared > needed, (
            f"{path.name} joins the poller after {declared}s but one poll "
            f"iteration can take {needed}s, so the final sample can be cut off "
            f"and every VRAM key reported as None"
        )

    @pytest.mark.parametrize("path", _BOTH_CANARIES, ids=lambda p: p.name)
    def test_a_cut_off_poller_is_distinguishable_from_a_silent_one(self, path):
        """The other half of the fix: even with headroom the join CAN expire,
        so the timeout case must be tellable from a genuine no-sample. The
        completion flag is written last and only on the normal exit."""
        assert "vram_poll_complete" in path.read_text(encoding="utf-8"), (
            f"{path.name} cannot distinguish 'the poller was cut off' from "
            f"'the poller measured nothing'"
        )


# ===========================================================================
# DELIVERY: the canary must not condemn a run production would have shipped
# ===========================================================================
#
# THE MISJUDGEMENT, and it was a real one. A shard produced 8 designs, 8 files,
# 8 reward rows and 8 complexes, then crashed in `evaluate`. The canary printed
# FAILED. Production's rule, in run_pipeline immediately after `complexa design`
# returns, is:
#
#     n_scored = sum(1 for d in designs if d.get("total_reward") is not None)
#     if rc != 0:
#         if n_scored == 0:
#             _fail("search", "complexa", ...)
#         logger.warning("... but %d/%d designs are fully scored - delivering")
#
# and the reward CSV it reads is written by the GENERATE stage, not by evaluate.
# So that run would have SHIPPED 8 scored designs to a paying customer. A
# measurement campaign was nearly cancelled on the canary's reading of it.
#
# THE VOCABULARY IS THREE-VALUED AND ORTHOGONAL TO THE OUTCOME. Delivery
# (clean / degraded / failed) answers "would production have shipped this";
# the outcome (PASS / FAIL / INCONCLUSIVE) answers "did the binders land on the
# patch". They are independent, and folding one into the other is what produced
# the wrong answer. A DEGRADED shard is stamped onto every verdict it touches
# and printed in full, because the fix is to stop calling it FAILED, not to stop
# reporting it.
# ===========================================================================


def _delivering_shard(**over):
    """A shard that CRASHED and DELIVERED: the exact shape that was misjudged."""
    shard = {
        "label": "phase1", "exit_code": 1,
        "n_designs_expected": 8, "n_scored_designs": 8, "n_reward_rows": 8,
        "designs": [{"name": f"sample_{i}.pdb", "is_complex": True}
                    for i in range(8)],
        "n_complexes": 8,
        "hydra": {"task_name_selected": True, "hotspots_match": True},
    }
    shard.update(over)
    return shard


class TestDeliveryIsNotTheExitCode:
    """``shard_delivery`` executed directly - it is the decision, so it is not
    on the local entrypoint's renderer allowlist and is covered here."""

    def test_a_clean_shard_is_clean(self):
        assert cs.shard_delivery({"exit_code": 0}) == (cs.CLEAN, "")
        assert cs.shard_failure({"exit_code": 0}) is None
        assert cs.shard_degradation({"exit_code": 0}) is None

    def test_a_crash_that_delivered_is_not_a_failure(self):
        """THE FIX. Production delivers on this reading; the canary said FAILED."""
        shard = _delivering_shard()
        state, detail = cs.shard_delivery(shard)
        assert state == cs.DEGRADED
        assert cs.shard_failure(shard) is None, (
            "a shard production would have shipped must not read as a failure")
        assert cs.shard_degradation(shard) == detail
        assert "8 design(s) came back fully scored" in detail
        assert "exited 1" in detail

    def test_a_crash_with_nothing_scored_is_still_a_failure(self):
        """The other side. Production _fail()s here, so the canary must too."""
        shard = _delivering_shard(n_scored_designs=0)
        assert cs.shard_delivery(shard)[0] == cs.FAILED
        assert "no scored designs" in cs.shard_failure(shard)
        assert cs.shard_degradation(shard) is None

    def test_a_crash_that_did_not_report_a_count_is_a_failure(self):
        """CONSERVATIVE ON PURPOSE. "we cannot tell" is not "it delivered":
        guessing the other way blesses a broken run, and it keeps every
        hand-built payload in this suite on its original verdict unless it opts
        in by reporting the count."""
        shard = _delivering_shard()
        del shard["n_scored_designs"]
        assert cs.shard_delivery(shard)[0] == cs.FAILED
        assert "did not report" in cs.shard_failure(shard)

    @pytest.mark.parametrize("value", [None, "eight", -1, True, [8]])
    def test_an_unusable_count_is_not_a_delivery(self, value):
        shard = _delivering_shard(n_scored_designs=value)
        assert cs.scored_design_count(shard) is None
        assert cs.shard_delivery(shard)[0] == cs.FAILED, value

    def test_the_old_hard_failures_are_untouched(self):
        """Nothing that used to be a FAIL for a reason other than the exit code
        may have become one of the new soft states."""
        for shard, fragment in (
            (None, "no result was returned"),
            ({"error": "boom", "exit_code": 0, "n_scored_designs": 8}, "boom"),
            ({"n_scored_designs": 8}, "no exit code"),
            ({"exit_code": "x", "n_scored_designs": 8}, "non-numeric"),
        ):
            assert cs.shard_delivery(shard)[0] == cs.FAILED, shard
            assert fragment in cs.shard_failure(shard)

    def test_it_agrees_with_production_on_every_combination(self):
        """THE ALIGNMENT ASSERTION, stated as the rule rather than as examples.

        Production fails a shard exactly when a non-zero exit left nothing
        scored. Anything else it delivers. The canary must draw the same line.
        """
        for rc in (0, 1, 2, 124):
            for n_scored in (0, 1, 8):
                shard = _delivering_shard(exit_code=rc, n_scored_designs=n_scored)
                production_fails = rc != 0 and n_scored == 0
                canary_fails = cs.shard_failure(shard) is not None
                assert canary_fails == production_fails, (
                    f"rc={rc} scored={n_scored}: production "
                    f"{'fails' if production_fails else 'delivers'} but the "
                    f"canary {'fails' if canary_fails else 'delivers'}")

    def test_production_still_writes_the_rule_this_is_aligned_to(self):
        """If run_pipeline's delivery rule ever moves, the test above is
        asserting agreement with something that no longer exists. Read the
        source rather than trusting the comment."""
        source = Path(rp.__file__).read_text(encoding="utf-8")
        assert 'sum(1 for d in designs if d.get("total_reward") is not None)' in source
        assert "if n_scored == 0:" in source
        assert '_fail("search", "complexa"' in source


class TestADegradedRunIsStillLoud:
    """The danger in the fix is the opposite of the defect: a crash going quiet
    because it no longer moves the verdict. It has to move the console."""

    def test_the_verdict_says_so_in_the_one_line_that_always_prints(self):
        verdict = cs.phase1_verdict(_delivering_shard())
        assert verdict.outcome == cs.PASS, verdict.reason
        assert verdict.reason.startswith("[DELIVERED-DEGRADED]")
        assert "exited 1" in verdict.reason
        assert verdict.metrics["delivery"] == cs.DEGRADED
        assert verdict.metrics["n_scored_designs"] == 8

    def test_a_clean_run_is_not_labelled(self):
        """A healthy run's console must be byte-for-byte what it was."""
        verdict = cs.phase1_verdict(_delivering_shard(exit_code=0))
        assert verdict.outcome == cs.PASS
        assert "DEGRADED" not in verdict.reason
        assert verdict.metrics["delivery"] == cs.CLEAN
        assert cs.delivery_note(_delivering_shard(exit_code=0)) == []

    def test_the_console_note_names_the_numbers_behind_it(self):
        lines = cs.delivery_note(_delivering_shard())
        assert lines, "a crashed-but-delivering shard printed nothing"
        text = " ".join(lines)
        assert "DELIVERED-DEGRADED" in text and "phase1" in text
        assert "reward-table rows 8" in text and "fully scored 8" in text
        assert "Production would have shipped this run" in text

    def test_a_failed_shard_is_not_given_the_degraded_note(self):
        """The two states are mutually exclusive; a FAILED shard's reason
        already says it once."""
        assert cs.delivery_note(_delivering_shard(n_scored_designs=0)) == []

    def test_every_phase_two_verdict_carries_the_stamp(self):
        pos = _delivering_shard(label="positive")
        neg = _delivering_shard(label="negative")
        null = _delivering_shard(label="null")
        for verdict in (cs.positive_verdict(pos), cs.negative_verdict(neg),
                        cs.null_verdict(pos, null)):
            assert verdict.metrics["delivery"] == cs.DEGRADED, verdict.name
            assert verdict.reason.startswith("[DELIVERED-DEGRADED]"), verdict.name
            assert verdict.metrics["delivery_detail"], verdict.name

    def test_the_null_verdict_takes_the_worse_of_its_two_shards(self):
        """A comparison is only as sound as its weaker half."""
        clean = _delivering_shard(label="positive", exit_code=0)
        degraded = _delivering_shard(label="null")
        verdict = cs.null_verdict(clean, degraded)
        assert verdict.metrics["delivery"] == cs.DEGRADED
        assert verdict.metrics["delivery_detail"][0].startswith("null:")

    def test_the_diagnostics_block_prints_the_delivery_state(self):
        """For a FAIL or an INCONCLUSIVE, where the reason prefix is not the
        only thing an operator reads."""
        shard = _delivering_shard(hydra={"task_name_selected": False,
                                         "task_name_values": ["other"]})
        verdict = cs.phase1_verdict(shard)
        assert verdict.outcome == cs.FAIL
        line = " ".join(cs.verdict_diagnostics(verdict))
        assert "delivery degraded" in line
        assert "designs fully scored (production would deliver) 8" in line


class TestTheShardReportsWhatProductionWouldDeliver:
    """The count itself, through the REAL ``run_shard`` body over real files."""

    @staticmethod
    def _rewards(rows):
        """A reward table in the shape ``run_pipeline.parse_designs`` reads:
        one row per design with a ``total_reward``. ``None`` for a row's reward
        writes an empty cell, which is what an unscored sample looks like."""
        head = "sample,total_reward,af2folding_plddt,af2folding_plddt_log"
        body = [f"design_{i},{'' if r is None else r},0.1,0.9"
                for i, r in enumerate(rows)]
        return "\n".join([head, *body]) + "\n"

    def _shard(self, tmp_path, rows, rc=0):
        files = [(f"sample_{i}.pdb", CORRECT_DESIGN_PDB) for i in range(len(rows))]
        files.append(("rewards_canary_0.csv", self._rewards(rows)))
        namespace = _shard_namespace(tmp_path, design_files=files, rc=rc)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        assert out.get("error") is None, out.get("error")
        return out

    def test_the_shard_counts_scored_designs_with_productions_parser(self, tmp_path):
        out = self._shard(tmp_path, [0.5] * 8, rc=1)
        assert out["n_reward_rows"] == 8
        assert out["n_scored_designs"] == 8
        assert cs.shard_delivery(out)[0] == cs.DEGRADED

    def test_an_unscored_row_is_not_counted_as_delivered(self, tmp_path):
        """``total_reward is not None`` is production's test, not ``nrows``."""
        out = self._shard(tmp_path, [0.5, None, None, 0.7], rc=1)
        assert out["n_reward_rows"] == 4
        assert out["n_scored_designs"] == 2

    def test_a_crash_with_a_wholly_unscored_table_still_fails(self, tmp_path):
        out = self._shard(tmp_path, [None, None], rc=1)
        assert out["n_scored_designs"] == 0
        assert cs.shard_delivery(out)[0] == cs.FAILED

    def test_no_reward_table_at_all_is_zero_not_unknown(self, tmp_path):
        """``parse_designs`` returns [] when it finds no CSV, and zero scored
        designs on a non-zero exit is precisely what production fails on."""
        namespace = _shard_namespace(
            tmp_path,
            design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)], rc=1)
        out = namespace["run_shard"](
            INPUT_TARGET_PDB, "positive", ["A1", "A2"], "", 1234, [60, 120],
            False, ["A1", "A2"])
        assert out["n_scored_designs"] == 0
        assert cs.shard_delivery(out)[0] == cs.FAILED

    def test_a_broken_counter_never_kills_the_shard_it_describes(self, tmp_path):
        """A diagnostic that can fail the run it is describing would be the same
        defect wearing a different hat. It reports None, which reads as FAILED
        - the conservative direction - and the shard still returns."""
        namespace = load_canary_functions({"_scored_design_counts"})

        class _Boom:
            @staticmethod
            def parse_designs(path):
                raise OSError("the volume went away")

        assert namespace["_scored_design_counts"](_Boom(), tmp_path) == {
            "n_scored_designs": None, "n_reward_rows": None}


class TestTheDesignCanaryHasTheSameDivergence:
    """``_design_canary.py`` carried the identical rule (``exit_code != 0 ->
    SHARD FAILED``) in its local entrypoint. Same fix, same three states."""

    @staticmethod
    def _main(res, capsys):
        main = _canary_func(
            _DESIGN_CANARY_PATH, "main",
            run_design_canary=_Remote(result=res),
            json=json, sys=sys)
        code = 0
        try:
            main()
        except SystemExit as exc:
            code = exc.code
        return code, capsys.readouterr().out

    @staticmethod
    def _res(**over):
        base = {"preset": "protein_binder", "task_name": "02_PDL1",
                "exit_code": 0, "n_scored_designs": 8, "n_reward_rows": 8,
                "csv_files": {}}
        base.update(over)
        return base

    def test_a_clean_run_still_exits_zero(self, capsys):
        code, out = self._main(self._res(), capsys)
        assert code == 0
        assert "FAILED" not in out and "DEGRADED" not in out

    def test_a_crash_that_delivered_is_no_longer_called_failed(self, capsys):
        code, out = self._main(self._res(exit_code=1), capsys)
        assert code == 0, "production would have shipped these 8 designs"
        assert "SHARD FAILED" not in out
        assert "DELIVERED-DEGRADED" in out
        assert "exited 1" in out and "8 of 8 reward rows" in out
        assert "still a real defect" in out, (
            "the crash must stay visible; the fix is to stop calling it FAILED")

    def test_a_crash_with_nothing_scored_still_fails(self, capsys):
        code, out = self._main(self._res(exit_code=1, n_scored_designs=0), capsys)
        assert code == 1
        assert "SHARD FAILED" in out and "no scored designs" in out

    def test_a_crash_with_no_count_still_fails(self, capsys):
        res = self._res(exit_code=1)
        del res["n_scored_designs"]
        code, out = self._main(res, capsys)
        assert code == 1
        assert "SHARD FAILED" in out and "no usable scored count" in out

    @pytest.mark.parametrize("rc", [None, "x"])
    def test_an_unusable_exit_code_fails_rather_than_reading_as_delivered(
            self, rc, capsys):
        """``if rc == 0: return`` alone let a shard with no exit code fall
        through to the DELIVERED-DEGRADED line and print "exited None"."""
        code, out = self._main(self._res(exit_code=rc), capsys)
        assert code == 1
        assert "SHARD FAILED" in out and "no usable exit code" in out
        assert "DELIVERED-DEGRADED" not in out

    @staticmethod
    def _rooted_path(root):
        """``Path`` with the container's hardcoded ``/opt/proteina`` rebased.

        ``run_design_canary`` writes that path as a literal, not a constant, so
        there is nothing to inject except ``Path`` itself. Every other value
        passes straight through to the real class.
        """
        def _P(value=""):
            text = str(value)
            return Path(root) if text == "/opt/proteina" else Path(text)
        return _P

    def _run_shard(self, tmp_path, rewards, rc=0):
        """EXECUTE ``run_design_canary`` and return the dict it really builds.

        THE POINT OF THIS HARNESS, and why the source-substring test it replaced
        was not good enough: setting ``n_scored = None`` immediately after the
        parse kept every substring that test asserted, passed, and made the
        canary report no count on every run. Only running the function and
        reading the payload can catch that.

        The design command is stubbed, and the stub WRITES the reward CSV -
        which is faithful, because the CSV is what the design command produces,
        and because the body wipes ``inference/`` before it runs. ``rp`` is the
        real ``run_pipeline`` loaded from the repo, so the count comes from
        production's parser over a real file on disk.
        """
        inference = tmp_path / "inference"

        def _run(cmd, **kwargs):
            inference.mkdir(parents=True, exist_ok=True)
            (inference / "rewards_canary_0.csv").write_text(rewards)
            return types.SimpleNamespace(returncode=rc)

        run_design_canary = _canary_func(
            _DESIGN_CANARY_PATH, "run_design_canary",
            also=("_scored_design_counts",),
            _RUN_PIPELINE_REMOTE=str(Path(rp.__file__)),
            _VRAM_JOIN_TIMEOUT_S=1,
            Path=self._rooted_path(tmp_path),
            sys=sys, time=time, glob=glob, json=json, threading=threading,
            subprocess=types.SimpleNamespace(
                run=_run, TimeoutExpired=subprocess.TimeoutExpired),
            _poll_vram=lambda stop, out, child_env=None: out.update(
                peak_vram_mb=0, vram_poll_interval_s=1,
                vram_prealloc_disabled=True, vram_poll_complete=True),
        )
        return run_design_canary(
            "protein_binder", "search_binder_local_pipeline", "02_PDL1", 4, 2)

    def test_the_payload_really_carries_the_count_the_entrypoint_judges_on(
            self, tmp_path):
        """The two halves live in different processes. If the container stops
        putting a real number in the payload, the entrypoint fails every run for
        want of it - and no assertion over the SOURCE can tell."""
        result = self._run_shard(
            tmp_path, TestTheShardReportsWhatProductionWouldDeliver._rewards(
                [0.5] * 8), rc=1)
        assert result["n_reward_rows"] == 8
        assert result["n_scored_designs"] == 8
        assert cs.shard_delivery(result)[0] == cs.DEGRADED

    def test_an_unscored_row_is_not_counted_in_the_payload(self, tmp_path):
        """``total_reward is not None``, not ``nrows`` - production's test,
        executed here rather than asserted about."""
        result = self._run_shard(
            tmp_path, TestTheShardReportsWhatProductionWouldDeliver._rewards(
                [0.5, None, None, 0.7]), rc=1)
        assert (result["n_reward_rows"], result["n_scored_designs"]) == (4, 2)

    def test_the_payload_and_the_entrypoint_agree_end_to_end(self, tmp_path, capsys):
        """The shard's real payload driven straight into the real entrypoint:
        the two halves are only useful if they meet."""
        result = self._run_shard(
            tmp_path, TestTheShardReportsWhatProductionWouldDeliver._rewards(
                [None, None]), rc=1)
        code, out = self._main(result, capsys)
        assert code == 1, "a crash with a wholly unscored table must still fail"
        assert "no scored designs" in out

    def test_a_broken_counter_never_kills_the_design_shard_either(self, tmp_path):
        """THE ASYMMETRY THIS CLOSES. The hotspot canary's twin is pinned;
        this one was not, so turning its ``except`` into a ``raise`` survived
        the whole suite - a diagnostic able to kill the paid shard it exists to
        describe."""
        counts = _canary_func(_DESIGN_CANARY_PATH, "_scored_design_counts")

        class _Boom:
            @staticmethod
            def parse_designs(path):
                raise OSError("the volume went away")

        assert counts(_Boom(), tmp_path) == {
            "n_scored_designs": None, "n_reward_rows": None}


# ===========================================================================
# THE CANARY'S STAGED BYTES ARE PRODUCTION'S, NOT A LOOKALIKE
# ===========================================================================
#
# WHAT THIS COST. ``prepare_custom_target`` grew a crop - the staged file is
# reduced to the contig's residues so upstream's ``metric_utils.py:217`` count
# assertion holds - and ``_stage`` went on doing ``p.write_text(pdb_text)``. Its
# docstring said "Stage the target EXACTLY the way ``prepare_custom_target``
# does" under a block-capital "THE CANARY MUST NOT EXERCISE A PATH PRODUCTION
# NEVER RUNS". True when written; false the moment the crop landed. A paid A100
# phase-1 shard then staged the uncropped file and reproduced the exact
# assertion the crop prevents, on the same contig (A236-300,B236-300 on 3S7G).
# Production was correct the whole time. The harness that exists to prove it
# was not.
#
# WHY "DOES _stage PRODUCE CROPPED OUTPUT" IS THE WRONG TEST. It can be
# satisfied by a second implementation inside the canary, which is the same
# defect one commit later. What has to be pinned is that the bytes come from
# PRODUCTION'S OWN function, so a future change to cropping cannot leave the
# canary behind - the rule ``_stage_dir`` already states for the path
# ("derived, not copied ... the canary follows it in the same commit or not at
# all"), applied to the bytes.
# ===========================================================================


def _three_chain_upload(sub_range=False):
    """A multi-chain upload with the shape that failed on hardware.

    Chain A 236-300 and B 236-300 carry 65 residues each; chain C is the one a
    two-chain contig does not name. When ``sub_range`` is set, A and B run to
    350 instead, so a contig of A236-300,B236-300 selects a strict subset of
    each named chain - the case upstream's count assertion rejects.
    """
    hi = 350 if sub_range else 300
    lines = []
    serial = 1
    for chain in ("A", "B", "C"):
        seq = [_TARGET_SEQ[i % len(_TARGET_SEQ)] for i in range(hi - 236 + 1)]
        lines += _trace(chain, seq, first_res=236, serial0=serial)
        serial += len(seq)
    return "\n".join(lines) + "\n"


class TestTheCanaryStagesThroughProduction:

    _CONTIG = "A236-300,B236-300"

    @staticmethod
    def _upstream_counts(residues, contig):
        """(left, right) of ``metric_utils.py:217`` for a file with ``residues``.

        ``named`` discards the ranges exactly as ``binder_eval_utils`` does.
        """
        segments = rp.parse_target_input(contig)
        named = {seg[0] for seg in segments}
        return (sum(1 for r in residues if r[0] in named),
                len(rp.select_residues(residues, segments)))

    def test_the_staged_bytes_come_from_productions_own_function(self, tmp_path):
        """THE TEST THAT WOULD HAVE CAUGHT IT, and it does not mention cropping.

        ``stage_cropped_target`` is replaced with a sentinel. If ``_stage``
        calls it, the staged file carries the sentinel; if ``_stage`` writes the
        bytes itself - verbatim OR by re-implementing the crop correctly - it
        does not. That is the property that survives the next change to
        cropping, which "the output looks cropped" would not.
        """
        fake = _fake_rp(tmp_path / "proteina")
        marks = []

        def _sentinel(dest, pdb_text, residues, segments):
            marks.append((Path(dest).name, len(residues), list(segments)))
            dest.write_text("REMARK PRODUCTION STAGED THIS\nEND\n")
            return 1, 1

        fake.stage_cropped_target = _sentinel
        namespace = load_canary_functions({"_stage", "_stage_dir"})
        key = cs.canary_task_key("positive", 1234)
        staged, _raw, _contig = namespace["_stage"](
            fake, _three_chain_upload(sub_range=True), key, self._CONTIG)
        assert staged.read_text() == "REMARK PRODUCTION STAGED THIS\nEND\n", (
            "_stage wrote the staged file itself instead of going through "
            "run_pipeline.stage_cropped_target; a canary that stages its own "
            "bytes can drift from production again, which is what cost a paid "
            "A100 shard")
        assert len(marks) == 1, "production's staging step was not called"
        name, n_residues, segments = marks[0]
        assert name == f"{key}.pdb"
        # ...and it was handed the RAW residues and the operator's contig, i.e.
        # production's arguments, not something already narrowed.
        assert n_residues == 345, "the raw upload's residues, all three chains"
        assert segments == [("A", 236, 300), ("B", 236, 300)]

    def test_a_sub_range_contig_stages_a_file_upstream_will_accept(self, tmp_path):
        """THE PAID FAILURE, REPRODUCED OFFLINE, through the real ``run_shard``.

        This is the assertion the A100 hit: CA atoms of the named chains in the
        staged file against the residues the contig selects.
        """
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        out = namespace["run_shard"](
            _three_chain_upload(sub_range=True), "phase1", ["A236", "B300"],
            self._CONTIG, 1234, [60, 120], False, ["A236", "B300"])
        assert out.get("error") is None, out.get("error")
        staged = Path(namespace["_fake_rp"]._HUB_TARGET_DIR) / f"{out['key']}.pdb"
        residues, _ = rp.pdb_ca_residues(staged)
        left, right = self._upstream_counts(residues, self._CONTIG)
        assert left == right == 130, (
            f"the shard staged a file upstream's evaluate stage would reject "
            f"({left} != {right}) - this is the run that failed on hardware")

    def test_the_uncropped_upload_would_have_failed_that_assertion(self, tmp_path):
        """The premise, so the test above cannot pass vacuously."""
        raw = tmp_path / "raw.pdb"
        raw.write_text(_three_chain_upload(sub_range=True))
        residues, _ = rp.pdb_ca_residues(raw)
        left, right = self._upstream_counts(residues, self._CONTIG)
        assert (left, right) == (230, 130) and left != right

    def test_input_chains_still_names_every_chain_the_upload_carried(self, tmp_path):
        """The crop removes chain C from the staged file, and ``input_chains``
        is documented as "every chain the input file carried" - read off the
        UPLOAD, not off the staged file, or the key silently becomes a copy of
        ``target_chains`` and the two can never disagree again."""
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        out = namespace["run_shard"](
            _three_chain_upload(sub_range=True), "phase1", ["A236"],
            self._CONTIG, 1234, [60, 120], False, ["A236"])
        assert out["input_chains"] == ["A", "B", "C"]
        assert out["target_chains"] == ["A", "B"]

    def test_a_crop_mismatch_becomes_a_refusal_record_not_a_process_exit(
            self, tmp_path, monkeypatch):
        """Production converts ``TargetCropError`` into ``_fail`` and exits.
        The canary cannot: a ``sys.exit`` inside a billed container throws away
        the diagnostic the run was bought for. Same error, different handling -
        which is exactly why ``stage_cropped_target`` raises instead of
        deciding."""
        # Patched on ``run_pipeline`` itself, not on the fake: the self-check
        # lives INSIDE production's ``stage_cropped_target``, which resolves the
        # crop from its own module globals. That is the point - the canary
        # inherits the check by calling the function, and cannot opt out of it.
        monkeypatch.setattr(rp, "crop_pdb_to_contig", lambda text, keep: text)
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        out = namespace["run_shard"](
            _three_chain_upload(sub_range=True), "phase1", ["A236"],
            self._CONTIG, 1234, [60, 120], False, ["A236"])
        assert "target staging" in (out.get("error") or "")
        assert "230" in out["error"] and "130" in out["error"]
        assert namespace["_design_commands"] == [], (
            "the shard ran the design command on a file upstream would reject")

    def test_phase_zero_stages_through_production_too(self, tmp_path):
        """No contig: the crop resolves to the whole of every chain, so the
        bytes are the input again - but by the same route, not around it."""
        fake = _fake_rp(tmp_path / "proteina")
        namespace = load_canary_functions(
            {"phase0", "_stage", "_stage_dir"},
            _load_rp=lambda: fake, _prune_registry=lambda module: [])
        results = namespace["phase0"](INPUT_TARGET_PDB)
        assert results["pass"] is True, results
        staged = (Path(fake._HUB_TARGET_DIR)
                  / f"{cs.canary_task_key('phase0', 0)}.pdb")
        residues, _ = rp.pdb_ca_residues(staged)
        assert self._upstream_counts(residues, "A1-30") == (30, 30)

    def test_the_staged_file_is_the_one_that_gets_registered(self, tmp_path):
        """The crop is pointless if the registration names a different file."""
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        out = namespace["run_shard"](
            _three_chain_upload(sub_range=True), "phase1", ["A236"],
            self._CONTIG, 1234, [60, 120], False, ["A236"])
        add = namespace["_fake_rp"].streamed[0]
        registered = Path(add[add.index("--target-path") + 1])
        assert registered.name == f"{out['key']}.pdb"
        residues, _ = rp.pdb_ca_residues(registered)
        assert self._upstream_counts(residues, self._CONTIG) == (130, 130)
        assert add[add.index("--target-input") + 1] == self._CONTIG, (
            "omitting the contig makes upstream default to A1-100")

    def test_no_incoming_file_is_left_behind(self, tmp_path):
        """``incoming.pdb`` is not ``canary_``-prefixed, so ``_prune_staged``
        would not collect it from a warm container."""
        namespace = _shard_namespace(
            tmp_path, design_files=[("sample_0.pdb", CORRECT_DESIGN_PDB)])
        namespace["run_shard"](
            _three_chain_upload(sub_range=True), "phase1", ["A236"],
            self._CONTIG, 1234, [60, 120], False, ["A236"])
        stage_dir = Path(namespace["_fake_rp"]._HUB_TARGET_DIR)
        assert not (stage_dir / "incoming.pdb").exists()


class TestTheNegativeNumberingGuardReachesTheCanaryToo:
    """THE SECOND INSTANCE OF THE SAME CLASS, found auditing the first.

    ``run_pipeline`` refuses a negative author residue number before the GPU
    (``unrenderable_segments``): atomworks' ``CONTIG_REGEX`` carries no sign, so
    a construct that keeps its expression tag derives ``A-5-240`` and raises
    inside ``complexa design`` - after checkpoints are loaded. Every other
    pre-GPU check passes on such a target. The canary had no equivalent, so
    ``--target-pdb <tagged construct>`` would have spent ~$4 in phase 1 or ~$12
    in phase 2 to learn what a regex knows for free.
    """

    @staticmethod
    def _tagged(tmp_path):
        """A construct numbered from -5, as an expression tag leaves it."""
        path = tmp_path / "tagged.pdb"
        path.write_text("\n".join(
            _trace("A", _TARGET_SEQ, first_res=-5)) + "\n")
        return path

    def test_a_tagged_construct_is_refused_before_any_shard_spawns(self, tmp_path):
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                str(self._tagged(tmp_path)), "", [("positive", ["A1"])])
        message = str(excinfo.value)
        assert "A-5-24" in message and "NO GPU TIME WAS USED" in message
        assert "CONTIG_REGEX" in message

    def test_main_does_not_spawn_on_a_tagged_construct(self, tmp_path):
        """Through ``main``, because the refusal is only worth anything if the
        spawn is downstream of it."""
        shard = _Remote()
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, run_shard=shard,
            phase0=_Remote(result={}))
        with pytest.raises(cs.CanaryRefusal):
            namespace["main"](phase=1, target_pdb=str(self._tagged(tmp_path)),
                              hotspots="A1 A2")
        assert shard.spawn_calls == [] and shard.remote_calls == [], (
            "phase 1 spent $4 on a contig upstream cannot parse")

    def test_a_normally_numbered_target_is_not_refused(self, tmp_path):
        """The guard must not be a blanket one, or the harness never runs."""
        path = tmp_path / "ok.pdb"
        path.write_text(INPUT_TARGET_PDB)
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        assert namespace["_refuse_unresolvable_hotspots"](
            str(path), "", [("positive", ["A1"])]) == "A1-30"

    def test_the_predicate_is_run_pipelines_and_not_a_restatement(self, tmp_path):
        """The whole point of this round: call production's function, do not
        re-derive its answer. A canary with its own idea of "unrenderable" is
        the drift this audit exists to remove."""
        assert rp.unrenderable_segments([("A", -5, 240)]) == [("A", -5, 240)]
        assert rp.unrenderable_segments([("A", 0, 240)]) == []
        tree = ast.parse(_CANARY_PATH.read_text(encoding="utf-8"))
        refusal = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "_refuse_unresolvable_hotspots")
        called = {
            node.func.attr for node in ast.walk(refusal)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "unrenderable_segments" in called, (
            "the canary must ASK run_pipeline whether a contig is renderable")


# ===========================================================================
# THE POST-RUN INSTRUCTION MUST DESCRIBE THE ENVELOPE THAT EXISTS
# ===========================================================================
#
# The canary closed every run with "SET shared/pdb_preflight_rules.py::
# _PROTEINA SizeEnvelope.hard_cap_target_aa FROM THIS RUN before flag-on."
# That was right while the cap was a placeholder and no shard had ever sized
# it. It has since been carried out: three completed shards at 130, 260 and
# 415 aa (preallocation disabled) produced the scaling curve the envelope is
# derived from.
#
# Left standing, the imperative would be worse than stale. It instructs an
# operator to re-derive a money cap from ONE reading — the single-point
# reasoning that produced the two discredited ~67.5 GB numbers — and doing it
# would replace a three-point fit with something strictly weaker. The output
# of a paid harness is the one place that instruction gets read, so it is
# corrected here rather than removed.
# ===========================================================================

def _emit_literals() -> str:
    """Every string literal the canary hands to ``_emit`` — i.e. its OUTPUT.

    Used by ONE test in the class below, deliberately. The others read the
    module as source text with ``#`` lines dropped, and for what they check
    ("this retired imperative is not in the file any more") that is the right
    artifact. It is NOT the right artifact for "the operator is shown these
    three measurements": a module-level string literal that nothing prints
    reads exactly the same in the source, so a footer could be gutted and the
    rows parked somewhere dead with the assertion none the wiser. EXECUTED,
    not reasoned: replace the footer ``_emit`` with ``"(table removed)"`` and
    move the three rows into an unused module-level constant, and the
    source-text reading passes this class 4/4 while the console the operator
    reads after a ~$12 shard shows no measurements at all.

    So this walks the AST and keeps only what reaches ``_emit``, the single
    function every print in that module goes through. f-string interpolations
    are dropped and their literal segments kept; the footer this pins is plain
    adjacent literals, which the parser has already concatenated for us. One
    line per emitted literal, so a match cannot be assembled across two
    separate calls that merely print next to each other.
    """
    tree = ast.parse(_CANARY_PATH.read_text(encoding="utf-8"))
    parts: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_emit"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                parts.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                parts.append("".join(
                    p.value for p in arg.values
                    if isinstance(p, ast.Constant)
                    and isinstance(p.value, str)
                ))
    return "\n".join(" ".join(p.split()) for p in parts)


class TestTheCanaryTellsTheOperatorWhatTheNumbersAreFor:

    def test_the_retired_set_the_cap_from_this_run_imperative_is_gone(self):
        source = _CANARY_PATH.read_text(encoding="utf-8")
        emitted = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "FROM THIS RUN" not in emitted, (
            "the canary still tells the operator to set the size cap from a "
            "single run; the envelope is a three-point fit and one reading "
            "cannot lawfully replace it"
        )

    def test_it_names_where_measurement_stops_and_what_would_extend_it(self):
        """The replacement has to carry the two facts an operator acts on:
        that 415 aa is the edge of measurement, and that only a COMPLETED
        shard beyond it moves the envelope."""
        source = _CANARY_PATH.read_text(encoding="utf-8")
        emitted = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "415" in emitted
        assert "does NOT raise the cap" in emitted
        assert "ABOVE 415 aa is the only thing that" in emitted
        # And the allocator caveat travels with it, because a reading taken
        # with preallocation on is not a data point at all.
        assert "prealloc_disabled must read True" in emitted

    def test_the_quoted_envelope_matches_the_shipped_one(self):
        """Two copies of a number drift, and the one in the paid harness is
        the one an operator reads at 2 a.m. Pin them together."""
        from shared.pdb_preflight_rules import TOOL_RULES

        env = TOOL_RULES["proteina"].size
        source = _CANARY_PATH.read_text(encoding="utf-8")
        emitted = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert f"hard_cap_target_aa={env.hard_cap_target_aa}" in emitted
        assert f"soft_warn_target_aa={env.soft_warn_target_aa}" in emitted

    def test_the_quoted_measurements_match_the_canonical_table(self):
        """THE DATA, not only the caps it was derived into.

        The test above pins the two CAPS this footer quotes, so a drifted cap
        dies. The three MEASUREMENTS it quotes alongside them were pinned by
        nothing at all: mutating 8,943 -> 9,943 MB, 25,457 -> 55,457 MB, or
        576 -> 999 s in the footer left the entire suite green. That is the
        worst place in the repo for an unguarded number. This text is what an
        operator reads at 2 a.m. after a ~$12 shard, and its only job is to
        let them decide whether the reading they just took extends the
        envelope or merely re-confirms it — a drifted row here is a wrong cap
        two commits later, argued from a table nobody re-checked.

        Pinned against ``tests/test_pdb_preflight.py::_PROTEINA_CANARY``, the
        same tuple the envelope's provenance test refits, so the harness copy,
        the shipped caps and the provenance proof cannot disagree about what
        was actually measured.

        ASSERTED AGAINST WHAT THE CANARY PRINTS, via ``_emit_literals`` — see
        that helper for why the source-text reading the rest of this class
        uses is the wrong artifact for this particular claim, and for the
        mutant that walks through it.
        """
        from tests.test_pdb_preflight import _PROTEINA_CANARY

        printed = _emit_literals()
        for aa, mb, secs in _PROTEINA_CANARY:
            quoted = f"{aa} aa / {mb:,} MB / {secs} s"
            assert quoted in printed, (
                f"nothing this canary prints quotes {quoted!r}; the "
                f"measurement table the operator is shown after a paid run "
                f"has drifted from the one the size envelope is derived from"
            )
        # And it prints those three and no others, so an invented fourth row
        # cannot be smuggled in beside them.
        assert printed.count(" MB / ") == len(_PROTEINA_CANARY), (
            f"the canary prints {printed.count(' MB / ')} measurement rows, "
            f"not the {len(_PROTEINA_CANARY)} completed shards the envelope "
            f"has"
        )
_SIZE_REFUSAL_RE = re.compile(
    r"the contig (?P<contig>.*?) selects (?P<count>\d+) residue\(s\) of "
    r"(?P<target>.*?), fewer than the (?P<floor>\d+) production requires")

_EMPTY_REFUSAL_RE = re.compile(
    r"the contig (?P<contig>.*?) names (?P<dead>.*?), which selects no residue "
    r"of (?P<target>.*?)\. The file contains: (?P<spans>.*?)\. ")


def _size_refusal_fields(message):
    """The size refusal's four values, BY ROLE rather than by substring.

    ``assert str(floor) in message`` is not a test of this message, and pinning
    that down is why this exists. Three separate mutations at the call site left
    the whole suite green: transposing the count with the floor ("selects 20
    residue(s) ... fewer than the 19 required" — which tells the operator to
    widen to a number below the floor), passing the FILE's residue count instead
    of the selection's ("the contig A1-19 selects 60 residue(s) ... fewer than
    the 20"), and transposing the target path with the contig. Every one of them
    still contains both numbers somewhere, so every ``in`` assertion passed.

    Worse, the substrings were partly supplied by the INPUTS. The old test used
    contig ``A1-19`` against a floor of 20, so ``"19" in message`` was satisfied
    by the contig text and never by the count; and because the message
    interpolates ``target_pdb``, which is a pytest ``tmp_path``, ``"20" in
    message`` passed outright on any run whose basetemp counter contained "20"
    — demonstrated at ``pytest-20`` and ``pytest-120``, about one run in a
    hundred at the current counter. The suite never went red; the mutation
    matrix that is this change's evidence just silently lost power.
    """
    match = _SIZE_REFUSAL_RE.search(message)
    assert match, f"the size refusal no longer renders its fields: {message}"
    return (match.group("contig"), int(match.group("count")),
            match.group("target"), int(match.group("floor")))


def _empty_refusal_fields(message):
    """The dead-segment refusal's four values, by role. Same reason as above."""
    match = _EMPTY_REFUSAL_RE.search(message)
    assert match, f"the dead-segment refusal no longer renders its fields: {message}"
    return (match.group("contig"), match.group("dead"),
            match.group("target"), match.group("spans"))


_UNPARSABLE_REFUSAL_RE = re.compile(
    r"--contig (?P<contig>.*?) cannot be parsed against (?P<target>.*?): "
    r"(?P<detail>.*?)\. A contig segment")


def _unparsable_refusal_fields(message):
    """The unparsable refusal's three values, by role.

    THE HELPER ABOVE WAS BUILT AND THIS REFUSAL WAS NOT GIVEN ONE, in the same
    commit that added it — so the defect the other two were hardened against
    was live here the whole time. An independent QC pass found it by mutation:
    transposing the first two arguments at the call site
    (``refuse_unparsable_contig(resolved, target_pdb, exc)``) left all 659 tests
    green, and renders

        [canary] --contig /data/fc.pdb cannot be parsed against Zz9: ...

    telling the operator their FILE PATH is the unparsable contig and their
    contig is the target file. The covering tests survived it because the only
    substrings they asserted — ``"Zz9"`` and ``"NO GPU TIME WAS USED"`` — are
    supplied by the input and by boilerplate, and ``"Zz9"`` appears whichever
    slot it lands in. Exactly the failure ``_size_refusal_fields`` documents,
    one refusal along.
    """
    match = _UNPARSABLE_REFUSAL_RE.search(message)
    assert match, f"the unparsable refusal no longer renders its fields: {message}"
    return (match.group("contig"), match.group("target"), match.group("detail"))


class _RpWithVerdict:
    """The real ``run_pipeline`` with ONE answer overridden.

    Everything is delegated to ``rp`` except ``target_too_small``, which returns
    whatever the test says. That is how "the canary obeys production's verdict"
    is separated from "the canary happens to agree with production on this
    fixture" — the two are indistinguishable while both sides say the same
    thing, and telling them apart is the entire subject of this round.
    """

    def __init__(self, verdict: bool):
        self._verdict = verdict
        self.asked = []

    def __getattr__(self, name):
        return getattr(rp, name)

    def target_too_small(self, residues, segments):
        # The WHOLE question, not its length. Recording only a count is what let
        # a mutation hand the predicate the file's residues instead of the
        # contig's segments and stay green.
        self.asked.append((len(residues), [tuple(s) for s in segments]))
        return self._verdict


class TestTheMinimumTargetSizeGuardReachesTheCanaryToo:
    """THE THIRD INSTANCE OF THE SAME CLASS, and by now it is a class.

    ``prepare_custom_target`` refuses a contig selecting fewer than
    ``MIN_SELECTED_RESIDUES`` before any GPU is started. The canary's pre-spawn
    refusals checked that the selection was non-EMPTY and nothing more, so
    ``--contig A10-20`` passed every one of them and spawned: one A100 in phase
    1 (~$4), three in phase 2 (~$12).

    THIS ONE COSTS MONEY IN BOTH DIRECTIONS, which the first two did not. A
    tagged construct and an uncropped file both CRASH the shard, so the money is
    lost but the verdict is at least honest. WHAT UPSTREAM DOES WITH A SLIVER IS
    UNVERIFIED: nothing in this repo evidences whether ``complexa design``
    refuses a sub-floor selection or designs against it, and no GPU run has
    tested it. If it refuses, the money is spent for an honest answer; if it
    designs, the metrics come back, the harness can report PASS, and the number
    measured is recall over a target production would have refused to accept.
    The pre-GPU answer is free under either branch, which is the argument for
    the refusal being here — not a claim about upstream that this repo cannot
    make.

    The two already closed are ``stage_cropped_target`` (the crop) and
    ``unrenderable_segments`` (negative numbering), both by CALLING production's
    own code rather than restating it. Same shape here: the number and the
    comparison are ``rp.MIN_SELECTED_RESIDUES`` and ``rp.target_too_small``, and
    the tests below are written against the constant, never against 20.
    """

    @staticmethod
    def _target(tmp_path):
        """60 residues on chain A, numbered A1..A60."""
        path = tmp_path / "t.pdb"
        path.write_text(SIXTY_RES_PDB)
        return path

    @staticmethod
    def _contig(n):
        """A chain-A contig selecting exactly ``n`` residues of SIXTY_RES_PDB."""
        return f"A1-{n}"

    def _below(self):
        return self._contig(rp.MIN_SELECTED_RESIDUES - 1)

    def _at(self):
        return self._contig(rp.MIN_SELECTED_RESIDUES)

    def _namespace(self, shard, rp_local=None):
        return load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=(lambda: rp_local if rp_local is not None else rp),
            run_shard=shard, phase0=_Remote(result={}))

    def test_a_sliver_of_a_contig_is_refused_before_any_shard_spawns(self, tmp_path):
        """EVERY FIELD BY ROLE. See ``_size_refusal_fields``: the substring form
        of this test was satisfied by the contig text and by the pytest temp
        directory's own digits, and three call-site transpositions survived it.
        """
        floor = rp.MIN_SELECTED_RESIDUES
        target = str(self._target(tmp_path))
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                target, self._below(), [("positive", ["A1", "A2"])])
        message = str(excinfo.value)
        contig, count, named, quoted_floor = _size_refusal_fields(message)
        assert count == floor - 1, (
            "the operator needs the number of residues the contig actually "
            f"selected, in that slot: {message}")
        assert quoted_floor == floor, (
            f"...and the floor they have to clear, in that one: {message}")
        assert count < quoted_floor, (
            f"the refusal quotes a floor below its own count: {message}")
        assert contig == self._below() and named == target, (
            f"the contig and the file are transposed: {message}")
        assert "$4" in message and "$12" in message, (
            f"the operator decides on the cost this refusal just saved: {message}")
        assert "NO GPU TIME WAS USED" in message

    def test_main_does_not_spawn_the_four_dollar_shard_on_a_sliver(self, tmp_path):
        """Through ``main``, because the refusal is only worth anything if the
        spawn is downstream of it. MUTATION: delete the
        ``cs.refuse_target_too_small`` call and phase 1 bills $4."""
        shard = _Remote()
        namespace = self._namespace(shard)
        with pytest.raises(cs.CanaryRefusal):
            namespace["main"](phase=1, target_pdb=str(self._target(tmp_path)),
                              hotspots="A1 A2", contig=self._below())
        assert shard.spawn_calls == [] and shard.remote_calls == [], (
            "phase 1 spent $4 designing against a contig production refuses")

    def test_main_does_not_spawn_the_three_twelve_dollar_shards_either(
            self, tmp_path):
        """``--negative`` skips ``pick_far_patch``, so this is the path with the
        least local code between the operator and three A100 startups."""
        shard = _Remote()
        namespace = self._namespace(shard)
        with pytest.raises(cs.CanaryRefusal):
            namespace["main"](phase=2, target_pdb=str(self._target(tmp_path)),
                              hotspots="A1 A2", negative="A8 A9 A10",
                              contig=self._below())
        assert shard.spawn_calls == [] and shard.remote_calls == [], (
            "phase 2 spawned three shards on a contig production refuses")

    def test_a_contig_at_the_floor_is_not_refused(self, tmp_path):
        """The guard must not be a blanket one, and the bound is ``<``: exactly
        ``MIN_SELECTED_RESIDUES`` residues is acceptable to production, so a
        canary that refuses it is refusing a run production would have run."""
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        assert namespace["_refuse_unresolvable_hotspots"](
            str(self._target(tmp_path)), self._at(),
            [("positive", ["A1"])]) == self._at()

    def test_a_whole_chain_target_is_not_refused(self, tmp_path):
        """The ordinary case, with no ``--contig`` at all."""
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        assert namespace["_refuse_unresolvable_hotspots"](
            str(self._target(tmp_path)), "", [("positive", ["A1"])]) == "A1-60"

    def test_the_size_refusal_precedes_the_hotspot_one(self, tmp_path):
        """PRODUCTION'S ORDER, AND THE ACTIONABLE MESSAGE.

        A sliver also puts most tokens outside the selection, so the hotspot
        refusal fires on the same input. Answering with "A55 does not resolve"
        sends the operator to fix a hotspot that is fine; the range is what is
        wrong, and ``prepare_custom_target`` checks the size first for exactly
        this reason.
        """
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                str(self._target(tmp_path)), self._below(),
                [("positive", ["A1", "A55"])])
        message = str(excinfo.value)
        _contig, count, _named, quoted_floor = _size_refusal_fields(message)
        assert (count, quoted_floor) == (rp.MIN_SELECTED_RESIDUES - 1,
                                         rp.MIN_SELECTED_RESIDUES)
        assert "Widen" in message
        assert "A55" not in message, (
            "the size refusal must win; a hotspot message here sends the "
            f"operator to the wrong fix: {message}")

    def test_a_two_chain_selection_at_the_floor_is_not_refused(self, tmp_path):
        """THE OVER-REFUSAL CONTROL ON THE MULTI-CHAIN SHAPE #109 ENABLED.

        Two near-miss counts pass every single-chain test in this class and
        refuse this one: counting the first segment's chain only, and counting
        distinct residue NUMBERS chain-blind. Both chains are numbered from 1
        here precisely so the chain-blind count is wrong rather than merely
        different.
        """
        hi = rp.MIN_SELECTED_RESIDUES - 1
        path = tmp_path / "two.pdb"
        path.write_text("\n".join(
            _trace("A", _TARGET_SEQ[:hi])
            + _trace("B", _TARGET_SEQ[:hi], y=30.0, serial0=100)) + "\n")
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        assert namespace["_refuse_unresolvable_hotspots"](
            str(path), f"A1-{hi},B1-{hi}", [("positive", ["A1"])]
        ) == f"A1-{hi},B1-{hi}"

    def test_the_numbering_refusal_precedes_the_size_one(self, tmp_path):
        """PRODUCTION'S ORDER, THE OTHER SIDE. ``A-5-5`` on a construct numbered
        from -5 is a sliver AND unrenderable; production refuses the numbering
        first, and "widen the range" would send the operator to a fix that
        cannot work while the range still starts below zero."""
        path = tmp_path / "tagged.pdb"
        path.write_text("\n".join(_trace("A", _TARGET_SEQ, first_res=-5)) + "\n")
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                str(path), "A-5-5", [("positive", ["A1"])])
        assert "CONTIG_REGEX" in str(excinfo.value)
        assert "Widen --contig" not in str(excinfo.value)

    def test_the_canary_obeys_production_s_verdict_rather_than_its_own(
            self, tmp_path):
        """THE DRIFT TEST, and the reason the threshold was lifted out of
        ``prepare_custom_target`` at all.

        A canary carrying its own ``< 20`` passes every other test in this
        class — it agrees with production today. It fails this one, in both
        directions, because production's answer is the only thing consulted. If
        the floor moves, the canary moves with it in the same commit or not at
        all.
        """
        # (a) production says too small; the target is 60 residues and the
        #     canary must refuse anyway.
        fake = _RpWithVerdict(True)
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: fake)
        with pytest.raises(cs.CanaryRefusal):
            namespace["_refuse_unresolvable_hotspots"](
                str(self._target(tmp_path)), "", [("positive", ["A1"])])
        assert fake.asked == [(60, [("A", 1, 60)])], (
            "the canary must hand production the file it read and the segments "
            f"it resolved, not a count of something else: {fake.asked}")

        # (b) production says it is fine; the contig is a sliver and the canary
        #     must NOT invent a floor of its own.
        fake = _RpWithVerdict(False)
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: fake)
        assert namespace["_refuse_unresolvable_hotspots"](
            str(self._target(tmp_path)), self._below(),
            [("positive", ["A1"])]) == self._below()

    def test_the_threshold_is_run_pipelines_and_not_a_restatement(self):
        """The structural half: the canary ASKS, and the number is not written
        down IN THE CALL THAT ENFORCES IT.

        SCOPED TO THE CALL, NOT TO THE FUNCTION, and the narrowing is a fix.
        Scanning every integer literal in ``_refuse_unresolvable_hotspots``
        meant any unrelated constant that happened to equal the floor would red
        the suite — the function already contains a literal ``1``, so a floor of
        1 would "fail the drift test" for a reason that has nothing to do with
        drift. The property being pinned is that the threshold reaches this call
        from ``run_pipeline``, and that is a property of the call.
        """
        residues = [("A", i, "") for i in range(1, 61)]
        assert rp.target_too_small(
            residues, [("A", 1, rp.MIN_SELECTED_RESIDUES - 1)])
        assert not rp.target_too_small(
            residues, [("A", 1, rp.MIN_SELECTED_RESIDUES)])
        refusal = next(
            n for n in ast.walk(ast.parse(_CANARY_PATH.read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_refuse_unresolvable_hotspots")
        called = {
            node.func.attr for node in ast.walk(refusal)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "target_too_small" in called, (
            "the canary must ASK run_pipeline whether the contig selects enough")
        guard = next(
            node for node in ast.walk(refusal)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "refuse_target_too_small")
        literals = {node.value for node in ast.walk(guard)
                    if isinstance(node, ast.Constant) and isinstance(node.value, int)
                    and not isinstance(node.value, bool)}
        assert not literals, (
            f"the size refusal carries its own numbers ({sorted(literals)}); "
            "every one of them must come from rp_local")
        assert "MIN_SELECTED_RESIDUES" in ast.unparse(guard), (
            "the floor must reach the message from run_pipeline's constant")

    def test_the_refusal_actually_raises_rather_than_computing_and_returning(self):
        """MUTATION: ``cs.refuse_target_too_small`` computing the verdict and
        returning it. Both earlier refusals shipped in exactly that shape — the
        suite could see the call existed and nothing more — while
        ``--hotspots A99999`` went on spawning three A100s."""
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            cs.refuse_target_too_small("t.pdb", "A10-20", True, 11, 20)
        assert "NO GPU TIME WAS USED" in str(excinfo.value)
        assert cs.refuse_target_too_small("t.pdb", "A1-60", False, 60, 20) is None

    @pytest.mark.parametrize("verdict", [True, 1, "yes", [0], 0.5])
    def test_any_truthy_verdict_refuses(self, verdict):
        """THE VERDICT IS PRODUCTION'S AND ITS TYPE IS NOT THIS MODULE'S
        BUSINESS. ``if too_small is not True: return`` passed the whole suite:
        the guard was armed only for a literal ``bool``, so any predicate that
        came to mean "too small" by returning a count, a list of offending
        residues, or a numpy bool would disarm it silently — on the side that
        spends money."""
        with pytest.raises(cs.CanaryRefusal):
            cs.refuse_target_too_small("t.pdb", "A1-11", verdict, 11, 20)

    @pytest.mark.parametrize("verdict", [False, 0, "", None, []])
    def test_any_falsy_verdict_permits(self, verdict):
        """The other direction, and the more expensive one to get wrong: a
        canary that refuses what production accepts stops runs that would have
        answered the question."""
        assert cs.refuse_target_too_small(
            "t.pdb", "A1-60", verdict, 60, 20) is None

    def test_a_zero_residue_selection_still_refuses_here(self):
        """``if not too_small or n_selected == 0: return`` also passed the
        suite. Zero is the MOST too-small a selection can be, and treating it as
        a special case disarms the guard exactly where it matters.

        In the canary, a zero selection is normally settled earlier and better
        by ``refuse_empty_segments``, which can say WHICH segment died and what
        the file contains. This refusal never sees it in practice — and must
        still be total, because "the case upstream of me handles it" is how the
        original hole was argued for.
        """
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            cs.refuse_target_too_small("t.pdb", "Z1-50", True, 0, 20)
        assert "NO GPU TIME WAS USED" in str(excinfo.value)

    def test_the_message_carries_the_resolved_contig_not_the_operators(
            self, tmp_path):
        """``--contig`` is optional, and on the path where it is omitted the
        message must still name the contig the canary DERIVED. Passing the raw
        argument instead renders an empty one, which no test exercised because
        every message test supplied a contig."""
        tiny = tmp_path / "tiny.pdb"
        tiny.write_text("\n".join(
            _atom(i, "CA", "ALA", "A", i, i * 4.0, 0.0, 0.0)
            for i in range(1, rp.MIN_SELECTED_RESIDUES - 4)) + "\n")
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                str(tiny), "", [("positive", ["A1"])])
        contig, count, _named, floor = _size_refusal_fields(str(excinfo.value))
        assert contig == f"A1-{rp.MIN_SELECTED_RESIDUES - 5}", (
            f"the derived contig did not reach the message: {excinfo.value}")
        assert (count, floor) == (rp.MIN_SELECTED_RESIDUES - 5,
                                  rp.MIN_SELECTED_RESIDUES)


class _CanaryProbe:
    """The pre-spawn refusal, driven directly and through ``main``.

    Every class below needs the same two views — the message a refusal carries,
    and the fact that ``run_shard`` was never reached — so they share one
    harness rather than four copies of ``load_canary_functions``.
    """

    @staticmethod
    def write(tmp_path, name, text):
        path = tmp_path / name
        path.write_text(text)
        return str(path)

    @staticmethod
    def refuse(target_pdb, contig, hotspots=("A1",)):
        """``_refuse_unresolvable_hotspots`` on the real ``run_pipeline``."""
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        return namespace["_refuse_unresolvable_hotspots"](
            target_pdb, contig, [("positive", list(hotspots))])

    @staticmethod
    def spawns(phase, target_pdb, contig, hotspots="A1 A2", negative="A8 A9 A10"):
        """Drive the REAL ``main`` and report what it asked Modal to start.

        ``_Remote`` records and never starts anything, so a refusal that does
        not actually stop the run shows up here as a non-empty list rather than
        as a bill. Phase 2 is given ``--negative`` because that is the path with
        the least local code between the operator and three A100 startups.
        """
        shard = _Remote()
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, run_shard=shard, phase0=_Remote(result={}))
        kwargs = {"phase": phase, "target_pdb": target_pdb, "contig": contig,
                  "hotspots": hotspots}
        if phase == 2:
            kwargs["negative"] = negative
        with pytest.raises(cs.CanaryRefusal):
            namespace["main"](**kwargs)
        return shard.spawn_calls + shard.remote_calls


class TestAnOverlappingContigCannotDefeatTheFloor:
    """ONE COMMA BOUGHT THE RUN THE FLOOR EXISTS TO STOP.

    ``select_residues`` appends per segment and never de-duplicates, and the
    size gate measured ``len`` of it. Measured on a 60-residue chain A:

      ``A10-20``          -> 11 counted, 11 distinct -> refused
      ``A10-20,A10-20``   -> 22 counted, 11 distinct -> NOT refused, spawned
      ``A1-7,A1-7,A1-7``  -> 21 counted,  7 distinct -> NOT refused, spawned

    ``--contig A10-20`` is the exact input the round that added the floor cites
    as its reproducer. The crop stages ``selected_residue_keys`` — the DISTINCT
    set — so the gate was counting one thing and the design engine receiving
    another. On the web route production is shielded by the adapter's
    one-range-per-chain rule; ``prepare_custom_target`` is not, and the canary
    does not go through the adapter at all.
    """

    def _target(self, tmp_path):
        return _CanaryProbe.write(tmp_path, "t.pdb", SIXTY_RES_PDB)

    def _doubled(self):
        return f"A1-{rp.MIN_SELECTED_RESIDUES - 1},A1-{rp.MIN_SELECTED_RESIDUES - 1}"

    def test_the_fixture_really_double_counts(self, tmp_path):
        """The premise, pinned. If ``select_residues`` ever de-duplicated on its
        own, every assertion below would pass for the wrong reason."""
        residues, _ = rp.pdb_ca_residues(Path(self._target(tmp_path)))
        segments = rp.parse_target_input(self._doubled())
        floor = rp.MIN_SELECTED_RESIDUES
        assert len(rp.select_residues(residues, segments)) == 2 * (floor - 1)
        assert rp.n_selected_residues(residues, segments) == floor - 1

    def test_a_sliver_named_twice_is_still_refused(self, tmp_path):
        target = self._target(tmp_path)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            _CanaryProbe.refuse(target, self._doubled())
        floor = rp.MIN_SELECTED_RESIDUES
        contig, count, named, quoted = _size_refusal_fields(str(excinfo.value))
        assert count == floor - 1, (
            f"the count must be the DISTINCT one: {excinfo.value}")
        assert count != 2 * (floor - 1), "the message quoted the doubled total"
        assert (contig, named, quoted) == (self._doubled(), target, floor)
        assert "NO GPU TIME WAS USED" in str(excinfo.value)

    def test_a_sliver_named_three_times_is_still_refused(self, tmp_path):
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            _CanaryProbe.refuse(self._target(tmp_path), "A1-7,A1-7,A1-7",
                                hotspots=["A1"])
        _contig, count, _named, _floor = _size_refusal_fields(str(excinfo.value))
        assert count == 7, f"21 counted for 7 residues: {excinfo.value}"

    def test_main_does_not_spawn_on_a_doubled_sliver(self, tmp_path):
        """MUTATION: count ``len(select_residues(...))`` again and phase 1
        bills $4 on 11 residues it thinks are 22."""
        assert _CanaryProbe.spawns(1, self._target(tmp_path), self._doubled(),
                                   hotspots="A1 A2") == []
        assert _CanaryProbe.spawns(2, self._target(tmp_path), self._doubled(),
                                   hotspots="A1 A2", negative="A3 A4") == []

    def test_a_legitimate_overlap_above_the_floor_is_not_refused(self, tmp_path):
        """The guard must not become "no chain twice". Production's own gate is
        a size, and 40 distinct residues clear it however they were named."""
        assert _CanaryProbe.refuse(
            self._target(tmp_path), "A1-30,A20-40") == "A1-30,A20-40"

    def test_the_gate_counts_what_the_crop_stages(self):
        """The invariant behind the fix, stated where it can fail: the number
        the floor compares is the number of residues the design engine is
        handed, not the number of times the contig mentioned one."""
        residues = [("A", i, "") for i in range(1, 61)]
        segments = [("A", 1, 15), ("A", 10, 25)]
        assert (rp.n_selected_residues(residues, segments)
                == len(rp.selected_residue_keys(residues, segments)) == 25)

    def test_the_hotspot_refusal_reports_the_distinct_count_too(self, tmp_path):
        """THE OTHER PLACE THE OLD COUNT COULD COME BACK.

        The size refusal was the reason the de-duplication was introduced, and
        it is well covered. ``refuse_unresolvable_hotspots`` takes a residue
        count as well, on the same line of this commit's diff, and reverting it
        to ``len(selected)`` left the whole suite green — found by mutation in
        an independent QC pass.

        Nothing is spent either way: the refusal fires regardless. But the
        operator reads that number to decide whether to widen the contig, and
        on ``A1-30,A20-40`` the repeated count says 51 residues where the crop
        stages 40. A message that overstates the target by a quarter is a
        message that argues against the fix it is reporting.
        """
        target = tmp_path / "t.pdb"
        target.write_text(SIXTY_RES_PDB)
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                str(target), "A1-30,A20-40", [("positive", ["A99999"])])
        message = str(excinfo.value)
        # 41 distinct residues (A1..A40 is 40, plus the overlap counted once);
        # the repeated count for the same contig is 51.
        distinct = rp.n_selected_residues(
            [("A", i, "") for i in range(1, 61)],
            [("A", 1, 30), ("A", 20, 40)])
        assert f"{distinct} residues selected" in message, (
            f"the hotspot refusal must quote the DISTINCT count ({distinct}), "
            f"not the repeated one: {message}")


class TestADeadSegmentCannotHideBehindAHealthyOne:
    """PRODUCTION REFUSES PER SEGMENT; THE CANARY CHECKED THE AGGREGATE.

    ``--contig A1-300,Z1-50`` against a file whose only chain is A selects 300
    residues, clears the size floor, resolves its hotspots inside chain A — and
    spawns. Production settles the same request for free, one segment at a
    time. PR #109 made multi-segment contigs the ordinary input shape, which is
    what turned this from latent into reachable.

    It also repairs a misdirection: ``--contig Z1-50`` alone used to come back
    as "selects 0 residue(s) ... fewer than the 20 production requires ... Widen
    --contig", which sends the operator to widen a range on a chain the upload
    does not contain.
    """

    def _target(self, tmp_path):
        return _CanaryProbe.write(tmp_path, "t.pdb", SIXTY_RES_PDB)

    def test_the_healthy_segment_does_not_hide_the_dead_one(self, tmp_path):
        target = self._target(tmp_path)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            _CanaryProbe.refuse(target, "A1-300,Z1-50")
        contig, dead, named, spans = _empty_refusal_fields(str(excinfo.value))
        assert dead == "Z1-50", (
            f"the refusal must name the offending segment ONLY: {excinfo.value}")
        assert spans == "A1-60", (
            f"...and what the file actually contains: {excinfo.value}")
        assert (contig, named) == ("A1-300,Z1-50", target), (
            f"the contig and the file are transposed: {excinfo.value}")
        assert "$4" in str(excinfo.value) and "$12" in str(excinfo.value)
        assert "NO GPU TIME WAS USED" in str(excinfo.value)

    def test_main_does_not_spawn_on_a_dead_segment(self, tmp_path):
        """MUTATION: delete the ``cs.refuse_empty_segments`` call and phase 1
        bills $4, phase 2 bills ~$12, for a contig production refuses."""
        assert _CanaryProbe.spawns(1, self._target(tmp_path), "A1-300,Z1-50") == []
        assert _CanaryProbe.spawns(2, self._target(tmp_path), "A1-300,Z1-50") == []

    def test_a_dead_chain_alone_no_longer_says_widen_the_range(self, tmp_path):
        """The message that misdirected. Chain Z is not in the file; no width
        of range on chain Z can change that."""
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            _CanaryProbe.refuse(self._target(tmp_path), "Z1-50")
        _contig, dead, _named, spans = _empty_refusal_fields(str(excinfo.value))
        assert (dead, spans) == ("Z1-50", "A1-60")
        assert "fewer than" not in str(excinfo.value), (
            "the size refusal answered a question about a missing chain: "
            f"{excinfo.value}")

    def test_an_unresolvable_bare_chain_is_refused_the_same_way(self, tmp_path):
        """``--contig Z``: expansion leaves it alone because there is no span to
        expand to, and this is the refusal that names it."""
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            _CanaryProbe.refuse(self._target(tmp_path), "Z")
        _contig, dead, _named, spans = _empty_refusal_fields(str(excinfo.value))
        assert (dead, spans) == ("chain Z", "A1-60")
        assert "None" not in dead, (
            f"an unexpanded segment leaked its None bounds: {excinfo.value}")

    def test_a_healthy_multi_segment_contig_is_not_refused(self, tmp_path):
        """The over-refusal control. Multi-segment contigs are the normal input
        shape since #109 and must go through."""
        path = _CanaryProbe.write(
            tmp_path, "two.pdb",
            "\n".join(_trace("A", _TARGET_SEQ)
                      + _trace("B", _TARGET_SEQ, y=30.0, serial0=100)) + "\n")
        assert _CanaryProbe.refuse(path, "A1-30,B1-30") == "A1-30,B1-30"

    def test_the_empty_refusal_precedes_the_size_one(self, tmp_path):
        """PRODUCTION'S ORDER. ``A1-5,Z1-50`` is both a sliver and a dead
        segment; production checks the segments first, and the fix for a chain
        that is not in the file is not "widen the range"."""
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            _CanaryProbe.refuse(self._target(tmp_path), "A1-5,Z1-50")
        _contig, dead, _named, _spans = _empty_refusal_fields(str(excinfo.value))
        assert dead == "Z1-50"
        assert "fewer than" not in str(excinfo.value)

    def test_the_negative_numbering_refusal_still_precedes_it(self, tmp_path):
        """The other side of the same ordering. ``A-5-0`` on a file numbered
        from 1 is BOTH unrenderable and empty; production refuses the numbering
        first, because that is the fault the operator has to fix."""
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            _CanaryProbe.refuse(self._target(tmp_path), "A-5-0")
        assert "CONTIG_REGEX" in str(excinfo.value)

    def test_the_predicate_is_run_pipelines_and_not_a_restatement(self):
        residues = [("A", i, "") for i in range(1, 61)]
        assert rp.empty_segments(residues, [("A", 1, 60), ("Z", 1, 50)]) == [
            ("Z", 1, 50)]
        assert rp.empty_segments(residues, [("A", 1, 60)]) == []
        refusal = next(
            n for n in ast.walk(ast.parse(_CANARY_PATH.read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_refuse_unresolvable_hotspots")
        called = {
            node.func.attr for node in ast.walk(refusal)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "empty_segments" in called, (
            "the canary must ASK run_pipeline which segments select nothing")

    def test_the_refusal_actually_raises_rather_than_computing_and_returning(self):
        """MUTATION: ``cs.refuse_empty_segments`` returning its verdict instead
        of raising. That is the shape three earlier refusals shipped in."""
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            cs.refuse_empty_segments("t.pdb", "A1-60,Z1-50", [("Z", 1, 50)],
                                     "A1-60")
        assert "NO GPU TIME WAS USED" in str(excinfo.value)
        assert cs.refuse_empty_segments("t.pdb", "A1-60", [], "A1-60") is None


class TestABareChainIdReachesTheNegativeNumberingGuard:
    """``--contig A`` SKIPPED THE GUARD ENTIRELY, BY DESIGN AND BY MISTAKE.

    ``parse_target_input`` yields ``('A', None, None)`` for a bare chain id, and
    the canary filtered exactly those out before asking
    ``unrenderable_segments`` — so a construct numbered from -5 passed every
    pre-spawn check and died inside ``from_contig`` on a billed A100.

    THE FIX IS TO EXPAND, NOT TO REFUSE. Production ACCEPTS a bare chain id: it
    resolves ``A`` to the chain's observed span and applies the numeric guards
    to THAT, so the same input is refused for negative numbering rather than for
    being bare. A canary that refused the id itself would stop runs production
    would have accepted, which on this branch is its own defect class.
    """

    def _tagged(self, tmp_path):
        return _CanaryProbe.write(
            tmp_path, "tagged.pdb",
            "\n".join(_trace("A", _TARGET_SEQ, first_res=-5)) + "\n")

    def _normal(self, tmp_path):
        return _CanaryProbe.write(tmp_path, "ok.pdb", SIXTY_RES_PDB)

    def test_a_bare_chain_on_a_tagged_construct_is_refused(self, tmp_path):
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            _CanaryProbe.refuse(self._tagged(tmp_path), "A")
        message = str(excinfo.value)
        assert "A-5-24" in message, (
            f"the refusal must name the SPAN the bare id expands to: {message}")
        assert "CONTIG_REGEX" in message and "NO GPU TIME WAS USED" in message

    def test_main_does_not_spawn_on_a_bare_chain_of_a_tagged_construct(
            self, tmp_path):
        """MUTATION: put the ``s[1] is not None`` filter back and phase 1 bills
        $4, phase 2 ~$12, to die in ``from_contig``."""
        assert _CanaryProbe.spawns(1, self._tagged(tmp_path), "A") == []
        assert _CanaryProbe.spawns(2, self._tagged(tmp_path), "A",
                                   negative="A3 A4") == []

    def test_a_bare_chain_on_a_normal_target_is_NOT_refused(self, tmp_path):
        """THE OVER-REFUSAL CONTROL, and the reason this is an expansion rather
        than a refusal. ``--contig A`` is a legitimate request that production
        honours; a canary that refused it would refuse a run that bills happily
        and returns a real answer."""
        assert _CanaryProbe.refuse(self._normal(tmp_path), "A") == "A"

    def test_a_bare_chain_still_clears_the_size_floor_when_it_should(
            self, tmp_path):
        """The expansion has to feed the SIZE gate too, or ``--contig A`` on a
        60-residue chain would be measured as zero selected residues."""
        residues, _ = rp.pdb_ca_residues(Path(self._normal(tmp_path)))
        segments = rp.expand_bare_chains(residues, rp.parse_target_input("A"))
        assert segments == [("A", 1, 60)]
        assert rp.n_selected_residues(residues, segments) == 60
        assert not rp.target_too_small(residues, segments)

    def test_the_expansion_is_run_pipelines_and_not_a_restatement(self):
        assert rp.expand_bare_chains(
            [("A", -5, ""), ("A", 24, "")], [("A", None, None)]) == [("A", -5, 24)]
        refusal = next(
            n for n in ast.walk(ast.parse(_CANARY_PATH.read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_refuse_unresolvable_hotspots")
        called = {
            node.func.attr for node in ast.walk(refusal)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "expand_bare_chains" in called, (
            "the canary must ASK run_pipeline to expand a bare chain id")
        assert "None" not in ast.unparse(refusal), (
            "the canary is filtering unexpanded segments again; expanding is "
            "what makes the numeric guards apply to a bare chain id")


class TestAnUnparsableContigIsARefusalNotATraceback:
    """NO MONEY IS AT STAKE AND IT IS STILL A DEFECT.

    ``--contig Zz9`` came out of ``_refuse_unresolvable_hotspots`` as a bare
    ``ValueError: unparsable target_input segment 'Zz9'``, which is the one
    failure in the harness that did not tell the operator what every other
    refusal tells them: that nothing was spent. Production converts the
    identical exception, from the identical parser, into a ``_fail``.
    """

    def _target(self, tmp_path):
        return _CanaryProbe.write(tmp_path, "t.pdb", SIXTY_RES_PDB)

    def test_an_unparsable_contig_raises_a_refusal(self, tmp_path):
        target = self._target(tmp_path)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            _CanaryProbe.refuse(target, "Zz9")
        message = str(excinfo.value)
        assert "NO GPU TIME WAS USED" in message
        # BY ROLE, not by substring. "Zz9" appears in this message whichever
        # slot it lands in, so `"Zz9" in message` passed while the contig and
        # the file path were transposed. See _unparsable_refusal_fields.
        contig, shown_target, detail = _unparsable_refusal_fields(message)
        assert contig == "Zz9", (
            f"the contig slot must hold the contig, got {contig!r}")
        assert shown_target == str(target), (
            f"the target slot must hold the file, got {shown_target!r}")
        assert "Zz9" in detail, (
            f"the parser's own complaint must survive, got {detail!r}")

    def test_it_is_no_longer_a_bare_value_error(self, tmp_path):
        """MUTATION: drop the ``except ValueError`` and this test sees a
        ``ValueError`` where the harness's contract promises a refusal."""
        with pytest.raises(cs.CanaryRefusal):
            _CanaryProbe.refuse(self._target(tmp_path), "A1-20,Zz9")

    def test_main_does_not_spawn_on_an_unparsable_contig(self, tmp_path):
        assert _CanaryProbe.spawns(1, self._target(tmp_path), "Zz9") == []
        assert _CanaryProbe.spawns(2, self._target(tmp_path), "Zz9",
                                   negative="A3 A4") == []

    def test_the_parser_is_run_pipelines_and_only_the_wrapping_is_local(self):
        with pytest.raises(ValueError):
            rp.parse_target_input("Zz9")
        refusal = next(
            n for n in ast.walk(ast.parse(_CANARY_PATH.read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_refuse_unresolvable_hotspots")
        called = {
            node.func.attr for node in ast.walk(refusal)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert {"parse_target_input", "refuse_unparsable_contig"} <= called

    def test_the_refusal_actually_raises(self):
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            cs.refuse_unparsable_contig("t.pdb", "Zz9", ValueError("nope"))
        assert "NO GPU TIME WAS USED" in str(excinfo.value)



def _uneven_chains(hi_a=443, hi_b=442):
    """The 3S7G shape at small scale: chain B one residue shorter than chain A.

    A 236-443 and B 236-442 are the real spans of the Fc target whose contig
    ``A236-443,B236-443`` burned an A100. Built from 236 so the numbers in the
    refusal are the ones an operator would recognise.
    """
    lines, serial = [], 1
    for chain, hi in (("A", hi_a), ("B", hi_b)):
        seq = [_TARGET_SEQ[i % len(_TARGET_SEQ)] for i in range(hi - 236 + 1)]
        lines += _trace(chain, seq, first_res=236, serial0=serial)
        serial += len(seq)
    return "\n".join(lines) + "\n"


class TestTheEndpointGuardReachesTheCanaryToo:
    """THE THIRD TIME PRODUCTION GREW A REFUSAL AND THE CANARY HAD TO FOLLOW.

    The class is now well established: production adds a pre-GPU refusal, the
    canary does not follow, and the canary can then spend $4-$12 on a target
    production would have refused — or return PASS on one. It cost a paid shard
    to find with the staging crop, an audit to find with the negative-numbering
    guard, and another with the 20-residue floor.

    This one is ``missing_endpoints``: a range end that names no residue.
    ``A236-443,B236-443`` on the real Fc target died as ``ValueError('No atoms
    found for selection: B/*/443')`` ~60 s into a billed A100, and every cheaper
    check passed on the way there — the range still selects the 207 residues of
    chain B that DO exist, so the count is right, the crop's self-check
    balances, and nothing notices the end that is missing.
    """

    def test_the_real_failure_is_refused_before_any_shard_spawns(self, tmp_path):
        path = tmp_path / "fc.pdb"
        path.write_text(_uneven_chains())
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                str(path), "A236-443,B236-443", [("positive", ["A236"])])
        message = str(excinfo.value)
        assert "residue 443 on chain B" in message
        assert "B/*/443" in message
        assert "A236-443, B236-442" in message, "the spans, so the fix is visible"
        assert "NO GPU TIME WAS USED" in message

    def test_main_does_not_spawn_on_an_endpoint_that_does_not_exist(self, tmp_path):
        """Through ``main``, because the refusal is only worth anything if the
        spawn is downstream of it."""
        path = tmp_path / "fc.pdb"
        path.write_text(_uneven_chains())
        shard = _Remote()
        namespace = load_canary_functions(
            {"main", "_refuse_unresolvable_hotspots", "_cancel_outstanding",
             "_finish", "_print_verdict"},
            _load_rp_local=lambda: rp, run_shard=shard,
            phase0=_Remote(result={}))
        with pytest.raises(cs.CanaryRefusal):
            namespace["main"](phase=1, target_pdb=str(path),
                              contig="A236-443,B236-443", hotspots="A236 A237")
        assert shard.spawn_calls == [] and shard.remote_calls == [], (
            "phase 1 spent $4 on a contig upstream cannot resolve")

    def test_the_corrected_contig_is_not_refused(self, tmp_path):
        """The guard must not be a blanket one, or the harness never runs."""
        path = tmp_path / "fc.pdb"
        path.write_text(_uneven_chains())
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        assert namespace["_refuse_unresolvable_hotspots"](
            str(path), "A236-443,B236-442",
            [("positive", ["A236"])]) == "A236-443,B236-442"

    def test_a_derived_contig_is_not_refused(self, tmp_path):
        """No ``--contig``: the canary derives it from the structure, so both
        ends exist by construction and nothing may refuse it."""
        path = tmp_path / "fc.pdb"
        path.write_text(_uneven_chains())
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        assert namespace["_refuse_unresolvable_hotspots"](
            str(path), "", [("positive", ["A236"])]) == "A236-443,B236-442"

    def test_the_predicate_is_run_pipelines_and_not_a_restatement(self):
        """THE PIN THAT MATTERS, and it is on DATA FLOW, not merely on the call
        existing — production's answer must be the argument the refusal decides
        on. Pinning "``missing_endpoints`` appears somewhere in this function"
        is NOT enough and was the first version of this test: QC demonstrated a
        canary that calls it, discards the result, and refuses on a locally
        computed list passing all 629 proteina tests. "Someone inlines the logic
        but leaves the call behind" is the realistic drift shape, not "someone
        deletes the call".

        The sibling pin on ``unrenderable_segments`` still has the weaker form
        and the same hole. Not widened here — it is a different guard and this
        commit does not own it — but it should be, and it is the reason to read
        this docstring before copying that one."""
        tree = ast.parse(_CANARY_PATH.read_text(encoding="utf-8"))
        refusal = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "_refuse_unresolvable_hotspots")
        decisions = [
            node for node in ast.walk(refusal)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "refuse_missing_endpoints"
        ]
        assert len(decisions) == 1, (
            "exactly one endpoint refusal is expected; with two the assertion "
            "below could be satisfied by whichever one is still wired up")
        produced_inline = {
            arg.func.attr for arg in decisions[0].args
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
        }
        assert "missing_endpoints" in produced_inline, (
            "the canary must pass run_pipeline's OWN return value straight into "
            "the refusal, not call it and decide on something else — a second "
            "implementation is the drift that has now cost a paid shard once "
            "and been caught by audit twice")

    def test_the_refusal_fires_before_the_hotspot_one(self, tmp_path):
        """Production checks endpoints (step 4b) before hotspots (step 5), and
        the canary must agree: on a contig whose end does not exist, the
        selection is not a trustworthy basis for a hotspot verdict."""
        path = tmp_path / "fc.pdb"
        path.write_text(_uneven_chains())
        namespace = load_canary_functions(
            {"_refuse_unresolvable_hotspots"}, _load_rp_local=lambda: rp)
        with pytest.raises(cs.CanaryRefusal) as excinfo:
            namespace["_refuse_unresolvable_hotspots"](
                # BOTH wrong: a bad endpoint and a hotspot matching nothing.
                str(path), "A236-443,B236-443", [("positive", ["A99999"])])
        assert "residue 443 on chain B" in str(excinfo.value)

    def test_the_shared_predicate_agrees_with_productions_own_answer(self, tmp_path):
        """Same function, same file, same verdict — the property the delegation
        pin exists to make unbreakable, asserted directly."""
        path = tmp_path / "fc.pdb"
        path.write_text(_uneven_chains())
        residues, _ = rp.pdb_ca_residues(path)
        assert rp.missing_endpoints(
            residues, rp.parse_target_input("A236-443,B236-443")) == [("B", 443)]
        assert rp.missing_endpoints(
            residues, rp.parse_target_input("A236-443,B236-442")) == []



# ===========================================================================
# UPSTREAM RENUMBERS THE TARGET, AND THE RESTORE IS WHAT MAKES PHASE 2 LEGIBLE
# ===========================================================================
#
# Measured on 8 of 8 designs of a completed Fc shard: input chains A 234-444
# (211 residues) and B 237-444 (208) came back as A 1-211 and B 1-208 —
# contiguous, labels preserved, order preserved, 100.0% positional sequence
# identity on both chains. Hotspots are matched by (chain, resseq), so every
# design was UNSCORABLE and phase 2 would have spent ~$12 to return
# INCONCLUSIVE.
#
# The gate that refused them is correct and is NOT relaxed here. The restore
# runs before it and puts the keys back, and only when sequence proves the
# correspondence. Everything below is about the second half of that sentence:
# what must NOT be repaired.


def _repeat_seq(n):
    """``n`` residues cycling _TARGET_SEQ — the module-level twin of the
    ``_repeat`` helper the older identity classes carry as a method."""
    return [_TARGET_SEQ[i % len(_TARGET_SEQ)] for i in range(n)]


class TestRenumberedTargetsAreRestored:
    """``restore_input_numbering`` — the repair, and everything it must refuse."""

    @staticmethod
    def _rows(chains):
        rows = []
        for i, (c, seq, first) in enumerate(chains):
            rows += _trace(c, seq, y=40.0 * i, serial0=1 + 1000 * i,
                           first_res=first)
        return rows

    def _ref(self, chains):
        return cs.ca_resnames(cs.heavy_atoms("\n".join(self._rows(chains)) + "\n"))

    def _design(self, chains, binder=("GLY",) * 8):
        rows = self._rows(chains) + _trace("Z", list(binder), y=4.0,
                                           serial0=9000)
        return "\n".join(rows) + "\n"

    def test_the_measured_shape_one_based_contiguous_is_repaired(self):
        """The exact shape hardware produced: input numbered from 234, design
        numbered from 1, same order, same sequence."""
        seq = _repeat_seq(60)
        reference = self._ref([("A", seq, 234)])
        design = self._design([("A", seq, 1)])
        entry = cs.score_design_file(design, {"A"}, ["A234"], [], reference)
        rn = entry["target_renumbering"]
        assert rn["applied"] is True
        assert rn["chains"]["A"]["identity"] == 1.0
        assert rn["chains"]["A"]["map"][1] == 234, "design 1 IS input 234"
        assert rn["chains"]["A"]["map"][60] == 293
        assert entry["target_verified"] is True

    def test_an_in_order_half_renumbering_is_repaired(self):
        """The case TestPerChainIdentity's coverage test used to own. Half the
        chain keeps the input numbering and half is thrown into the 5000s, but
        the RESIDUES are the same ones in the same order, so the positional map
        is right and repairing it is correct. That test now uses a sequence the
        restore cannot prove, so it still measures the coverage floor."""
        seq = _repeat_seq(60)
        reference = self._ref([("A", seq, 1)])
        rows = (_trace("A", seq[:30], first_res=1)
                + _trace("A", seq[30:], first_res=5031, serial0=3000)
                + _trace("Z", ["GLY"] * 8, y=4.0, serial0=9000))
        entry = cs.score_design_file("\n".join(rows) + "\n", {"A"}, ["A1"], [],
                                     reference)
        assert entry["target_renumbering"]["applied"] is True
        assert entry["target_renumbering"]["chains"]["A"]["map"][5031] == 31
        assert entry["target_verified"] is True

    def test_a_design_already_in_input_numbering_is_left_alone(self):
        """No-op, and it must SAY no-op rather than silently rewriting: a
        needless remap is a second place for the keys to go wrong."""
        seq = _repeat_seq(40)
        reference = self._ref([("A", seq, 7)])
        entry = cs.score_design_file(self._design([("A", seq, 7)]), {"A"},
                                     ["A7"], [], reference)
        rn = entry["target_renumbering"]
        assert rn["applied"] is False
        assert rn["already_input_numbering"] is True
        assert entry["target_verified"] is True, (
            "declining to remap must not make a correct design unscorable")

    def test_a_length_mismatch_refuses_rather_than_aligning_a_prefix(self):
        """THE ROLE INVERSION, and the reason the map is length-exact. A binder
        relabelled onto the target's chain id is shorter; aligning it to the
        first N residues of the target would hand back a perfect recall
        measured on the binder's own contacts. Reproduced on real output: a
        74-residue binder on chain A against a 211-residue input chain."""
        reference = self._ref([("A", _repeat_seq(60), 234)])
        design = self._design([("A", _repeat_seq(20), 1)])
        entry = cs.score_design_file(design, {"A"}, ["A234"], [], reference)
        assert entry["target_renumbering"]["applied"] is False
        assert "length differs" in entry["target_renumbering"]["reason"]
        assert entry["target_verified"] is False
        assert entry.get("hotspot_recall") is None, "never a fabricated score"

    def test_an_all_unknown_chain_cannot_certify_a_map(self):
        """``same_residue`` counts UNK as a match, so an all-UNK chain scores
        1.0 against any reference. The informative floor is what stops a
        sequence-free backbone model certifying a map to anything."""
        reference = self._ref([("A", _repeat_seq(60), 234)])
        design = self._design([("A", ["UNK"] * 60, 1)])
        entry = cs.score_design_file(design, {"A"}, ["A234"], [], reference)
        assert entry["target_renumbering"]["applied"] is False
        assert "informative" in entry["target_renumbering"]["reason"]
        assert entry["target_verified"] is False

    def test_a_mostly_unknown_chain_with_a_few_lucky_matches_refuses(self):
        """THE CASE THE INFORMATIVE FLOOR ACTUALLY OWNS, and the one that was
        missing: deleting the floor left the suite green.

        An all-UNK chain is already refused by the identity check, because zero
        informative pairs makes ``identity`` None. The floor only bites when
        there are a FEW informative pairs and they all match — 3 real residues
        inside 57 unknowns scores a perfect 1.0 on a sample of 3, and without
        the floor that certifies a map for the whole 60-residue chain. A
        backbone model emitting mostly sequence-free output with a few residues
        resolved is the realistic way to land here."""
        seq = ["UNK"] * 60
        ref_seq = _repeat_seq(60)
        for i in (5, 20, 41):
            seq[i] = ref_seq[i]
        reference = self._ref([("A", ref_seq, 234)])
        design = self._design([("A", seq, 1)])
        entry = cs.score_design_file(design, {"A"}, ["A234"], [], reference)
        rn = entry["target_renumbering"]
        assert rn["chains"]["A"]["n_informative"] == 3
        assert rn["chains"]["A"]["identity"] == 1.0, (
            "identity is PERFECT on its tiny sample — so identity is not what "
            "refuses this, and if that ever changes this test has stopped "
            "measuring the floor")
        assert rn["applied"] is False
        assert "informative" in rn["reason"]
        assert entry["target_verified"] is False

    def test_a_different_chain_of_the_same_length_refuses(self):
        """Length alone is not evidence. GLY is absent from _TARGET_SEQ, so
        every position mismatches and the identity floor refuses."""
        reference = self._ref([("A", _repeat_seq(60), 234)])
        design = self._design([("A", ["GLY"] * 60, 1)])
        entry = cs.score_design_file(design, {"A"}, ["A234"], [], reference)
        assert entry["target_renumbering"]["applied"] is False
        assert "identity" in entry["target_renumbering"]["reason"]
        assert entry["target_verified"] is False

    def test_all_target_chains_must_map_or_none_is_applied(self):
        """A partial remap leaves one chain keyed to the input and another to
        1..N — worse than not trying, because the gate would then see a mixture
        and could still clear a pooled floor."""
        seq_a, seq_b = _repeat_seq(60), _repeat_seq(40)
        reference = self._ref([("A", seq_a, 234), ("B", seq_b, 300)])
        # A is repairable; B is the wrong length and is not.
        rows = (_trace("A", seq_a, first_res=1)
                + _trace("B", _repeat_seq(25), y=40.0, first_res=1, serial0=1001)
                + _trace("Z", ["GLY"] * 8, y=4.0, serial0=9000))
        entry = cs.score_design_file("\n".join(rows) + "\n", {"A", "B"},
                                     ["A234"], [], reference)
        rn = entry["target_renumbering"]
        assert rn["chains"]["A"]["ok"] is True, "A alone would have mapped"
        assert rn["chains"]["B"]["ok"] is False
        assert rn["applied"] is False, "one bad chain vetoes the whole remap"
        assert entry["target_verified"] is False
