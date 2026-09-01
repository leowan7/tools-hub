"""Modified residues must still occlude the approach cone.

`score_approach_cone` built BOTH the centre of mass and the occluding
non-patch atom cloud from residues passing a bare `r.id[0] == " "`, so MSE and
SEC -- ordinary polymer residues the PDB deposits as HETATM -- contributed
neither. Two consequences, and the second is the surprising one:

  * LOCAL. A residue that is not in the cloud cannot block a ray, so the cone
    reads too OPEN. Strictly one-directional.
  * GLOBAL. The centre of mass is a WHOLE-CHAIN mean and
    `_fibonacci_hemisphere` is oriented by it, so dropping those atoms tilts
    the sampled directions for every epitope in the chain, including ones
    nowhere near an MSE.

Measured across 90 SeMet depositions / 223 MSE-bearing chains / 2900 epitopes:
39.8% of epitopes scored differently from a MET-ized control, median
|d access| 0.0167 and max 0.50 (geometric_access carries 0.25 of the
composite). Routing both loops through `scout.polymer.polymer_residues` takes
the MSE-free-epitope population from 410 differing to ZERO.

The re-spelling below changes ONLY the record type and residue name. Every
coordinate is untouched, so a correct implementation must return the identical
score -- which is what makes this a control rather than a plausibility check.
"""

import io
from pathlib import Path

import numpy as np
import pytest
from Bio.PDB import PDBParser

from scout.accessibility import score_approach_cone
from scout.sasa import STANDARD_AA

_FC = Path(__file__).resolve().parents[1] / "static" / "example" / "3ave_igg1_fc_dimer.pdb"

# A 20-residue epitope on 3ave chain A, partly occluded by the shell below.
# Measured on this fixture: the pre-fix code scored it 0.2727 and 0.3333 once
# the shell was re-spelled as MSE, so the arm carries a +0.0606 signal to
# detect; with the fix both arms read 0.2727. test_the_shell_really_is_what_
# occludes_this_epitope is what keeps that signal from silently going to zero.
_EPITOPE = frozenset({348, 349, 350, 351, 352, 365, 366, 367, 368, 381,
                      408, 410, 423, 424, 425, 438, 439, 440, 441, 442})

# The occluding shell, nearest non-patch residues to the epitope centroid.
# All twelve are ordinary residues (SER/VAL/GLU/TYR/LYS) carrying full
# backbones, so re-spelling them as MSE is a pure record-type change.
_SHELL = frozenset({364, 369, 379, 382, 383, 388, 391, 407, 409, 422, 426, 427})


def _respell(text, resnums, resname="MSE", record="HETATM", chain_id="A"):
    """Re-record these residues under a new resname. Coordinates untouched."""
    out, hits = [], 0
    for line in text.splitlines(True):
        if (line.startswith(("ATOM  ", "HETATM"))
                and line[21] == chain_id
                and line[22:26].strip().isdigit()
                and int(line[22:26]) in resnums):
            line = record.ljust(6) + line[6:17] + resname.ljust(3) + line[20:]
            hits += 1
        out.append(line)
    assert hits, f"re-spelled nothing for {sorted(resnums)[:4]}..."
    return "".join(out)


def _access(text, epitope, chain_id="A"):
    """Verbatim scout/pipeline.py construction for one epitope."""
    chain = PDBParser(QUIET=True).get_structure("x", io.StringIO(text))[0][chain_id]
    all_res, patch = [], []
    for r in chain.get_residues():
        if r.get_id()[0] != " " or r.resname not in STANDARD_AA:
            continue
        all_res.append(r)
        if r.get_id()[1] in epitope:
            patch.append(r)
    assert patch, "epitope selected no residues"
    coords = [a.get_vector().get_array() for r in all_res for a in r.get_atoms()]
    return score_approach_cone(
        patch, np.array(coords, dtype=float),
        patch_resnums=set(epitope), chain=chain,
    )


@pytest.fixture(scope="module")
def fc_text():
    return _FC.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("resname", ["MSE", "SEC"])
@pytest.mark.parametrize("record", ["HETATM", "ATOM"])
def test_modified_residues_still_occlude_the_approach_cone(fc_text, resname, record):
    """Re-spelling the occluding shell must not change the score.

    ``record`` covers both spellings: real depositions write HETATM, while
    design and refinement pipelines re-emit the same residue as ATOM. The
    predicate matches on resname alone, so the two must agree.

    Only the HETATM arms discriminate. An ATOM-spelled MSE carries a blank
    hetflag and so passed the OLD gate too, which means those two arms are
    green before and after -- a non-regression guard, not evidence for this
    change. Against the pre-fix code this file goes 2 red / 4 green, and the
    two reds are exactly the HETATM arms.
    """
    baseline = _access(fc_text, _EPITOPE)
    respelled = _access(_respell(fc_text, _SHELL, resname, record), _EPITOPE)
    assert respelled == pytest.approx(baseline, abs=1e-9), (
        f"{resname} spelled as {record} stopped occluding: score moved "
        f"{baseline} -> {respelled} with no atom moved"
    )


def test_the_shell_really_is_what_occludes_this_epitope(fc_text):
    """Non-vacuity floor.

    The arm above asserts a score does NOT move, which passes trivially if the
    shell is irrelevant to the epitope. DELETING the shell must move the score,
    or the test is asserting nothing.
    """
    baseline = _access(fc_text, _EPITOPE)
    without = "".join(
        line for line in fc_text.splitlines(True)
        if not (line.startswith(("ATOM  ", "HETATM"))
                and line[21] == "A"
                and line[22:26].strip().isdigit()
                and int(line[22:26]) in _SHELL)
    )
    assert _access(without, _EPITOPE) > baseline + 0.05, (
        "removing the shell did not open the cone, so this fixture cannot "
        "detect the shell failing to occlude"
    )


def test_a_free_ligand_still_does_not_occlude(fc_text):
    """The relaxation is polymer-aware, not "any HETATM".

    3ave carries a real ZN and 140 waters. They were never in the cloud and must
    stay out: `polymer_residues` admits MSE/SEC only where they are bonded into
    the chain, so a ligand cannot start blocking rays and depress the score.
    """
    chain = PDBParser(QUIET=True).get_structure("x", io.StringIO(fc_text))[0]["A"]
    het = {r.resname.strip() for r in chain.get_residues() if r.id[0] != " "}
    assert "ZN" in het and "HOH" in het, "fixture lost its heteroatoms"

    stripped = "".join(
        line for line in fc_text.splitlines(True)
        if not (line.startswith("HETATM") and line[17:20].strip() in het)
    )
    assert _access(stripped, _EPITOPE) == pytest.approx(_access(fc_text, _EPITOPE), abs=1e-9)
