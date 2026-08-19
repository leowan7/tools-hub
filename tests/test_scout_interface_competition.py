"""Scout feasibility — ``interface_competition`` must be measured, not constant.

``scout/pipeline.py`` "Step 8: Interface competition" scored a constant ``1.0``
for every Scout run from ``3ba4c5d`` until this test landed. The step imported
``scout.interfaces.detect_ppi_interfaces`` — a name that has never existed
anywhere in the repo's history — inside a bare ``try``, so the ``ImportError``
was swallowed on every call and the neutral default survived.

The damage was silent and one-directional:

* ``interface_competition`` carries 0.10 of the composite in
  ``scout.feasibility.DIMENSION_WEIGHTS``, so a tenth of every score was a
  free full-marks pass.
* ``scout.feasibility._identify_risk_factors`` gates its
  "overlaps a natural protein-protein interface" warning on ``< 0.50``, so that
  warning could never fire for any user.

An epitope buried entirely inside an endogenous PPI interface was reported as
"No endogenous binding partner contacts this epitope" with no risk factors.

These tests drive the real ``run_feasibility_pipeline`` over a synthetic
two-chain structure and pin both ends of the dimension: a buried epitope must
score low and a clear one must score 1.0. A constant of *any* value fails one
of the two, which is the regression this file exists to catch.

``compute_rsa`` is the one stub. It is the only freesasa entry point in the
pipeline and freesasa (a declared dependency, installed in CI) has no Windows
wheel, so stubbing it is what lets this run on every developer machine rather
than only on Linux. It is not on the path under test: in
``run_feasibility_pipeline`` the RSA map feeds only the ``max_burial``
normalisation for surface topology, while the epitope patch is selected by
residue number. Nothing else here is faked — BioPython parses a real file and
``detect_interfaces`` computes real inter-chain contacts.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

# Chain A: 30 residues along +x. Chain B: 12 residues laid alongside A at
# +4.0 A in y, which puts A residues 9-22 in heavy-atom contact with B.
_INTERFACE_RESIDUES = range(9, 23)
_BURIED_EPITOPE = [12, 13, 14, 15, 16, 17]   # entirely inside the B interface
_CLEAR_EPITOPE = [1, 2, 3, 4, 5, 6]          # far end of chain A, no contact


def _chain(chain_id: str, n_res: int, x0: float, y0: float, first_serial: int) -> list[str]:
    lines = []
    serial = first_serial
    for i in range(n_res):
        resnum = 1 + i
        bx = x0 + 3.5 * i
        for name, ox, oy, oz in (
            ("N", 0.0, 0.0, 0.0),
            ("CA", 1.0, 0.2, 0.0),
            ("C", 2.0, 0.0, 0.0),
            ("O", 2.2, 1.1, 0.0),
            ("CB", 1.0, -0.7, 1.2),
        ):
            lines.append(
                f"ATOM  {serial:5d}  {name:<3s} ALA {chain_id}{resnum:4d}    "
                f"{bx + ox:8.3f}{y0 + oy:8.3f}{oz:8.3f}  1.00 50.00           {name[0]}"
            )
            serial += 1
    return lines


@pytest.fixture
def two_chain_pdb(tmp_path: Path) -> Path:
    pdb = tmp_path / "two_chain.pdb"
    lines = _chain("A", 30, 0.0, 0.0, 1) + _chain("B", 12, 3.5 * 9, 4.0, 5000)
    pdb.write_text("\n".join(lines) + "\nEND\n")
    return pdb


@pytest.fixture
def feasibility_row(monkeypatch):
    """Return a callable(pdb, chain, residues) -> the feasibility_results.csv row."""
    # See the module docstring: freesasa is Linux-only in practice and is not on
    # the interface-competition path.
    monkeypatch.setattr("scout.pipeline.compute_rsa", lambda *a, **k: {})

    def _run(pdb_path: Path, chain_id: str, residues: list[int]) -> dict:
        from scout.pipeline import run_feasibility_pipeline

        csv_path = run_feasibility_pipeline(pdb_path, chain_id, residues)
        with csv_path.open(newline="") as fh:
            return next(csv.DictReader(fh))

    return _run


def test_detect_interfaces_sees_the_partner_chain(two_chain_pdb):
    """Precondition: the fixture really does contain a detectable interface.

    If this fails the scoring tests below are meaningless, so it is asserted
    separately rather than left implicit.
    """
    from scout.interfaces import detect_interfaces

    interfaces = detect_interfaces(two_chain_pdb, "A")
    assert len(interfaces) == 1, f"expected one partner chain, got {interfaces}"
    assert interfaces[0]["partner_chain"] == "B"
    contacts = set(interfaces[0]["contact_residues"])
    assert set(_BURIED_EPITOPE) <= contacts
    assert not set(_CLEAR_EPITOPE) & contacts


def test_buried_epitope_scores_below_the_neutral_default(two_chain_pdb, feasibility_row):
    """The regression guard: an epitope inside a PPI interface must not score 1.0."""
    row = feasibility_row(two_chain_pdb, "A", _BURIED_EPITOPE)
    competition = float(row["interface_competition"])

    assert competition < 1.0, (
        "interface_competition is at the neutral default for an epitope that is "
        "entirely buried in chain B's interface — the dimension has gone constant "
        "again (see this module's docstring)."
    )
    # Every requested residue is a contact residue, so overlap is 1.0 and the
    # score floors at 0.1.
    assert competition == pytest.approx(0.1)


def test_clear_epitope_scores_the_neutral_default(two_chain_pdb, feasibility_row):
    """The other end: the dimension must not be pinned to a low constant either."""
    row = feasibility_row(two_chain_pdb, "A", _CLEAR_EPITOPE)
    assert float(row["interface_competition"]) == pytest.approx(1.0)


def test_competition_discriminates_between_the_two_epitopes(two_chain_pdb, feasibility_row):
    """Same structure, same chain, different epitope — the score must move.

    This is the assertion that survives a future refactor changing the exact
    numbers: whatever the scale, a buried epitope cannot score the same as a
    clear one.
    """
    buried = float(feasibility_row(two_chain_pdb, "A", _BURIED_EPITOPE)["interface_competition"])
    clear = float(feasibility_row(two_chain_pdb, "A", _CLEAR_EPITOPE)["interface_competition"])
    assert buried < clear


def test_ppi_risk_factor_is_reachable(two_chain_pdb, feasibility_row):
    """``_identify_risk_factors``' ``< 0.50`` branch was dead code; it must fire."""
    row = feasibility_row(two_chain_pdb, "A", _BURIED_EPITOPE)
    assert "natural protein-protein interface" in row["risk_factors"]

    clear = feasibility_row(two_chain_pdb, "A", _CLEAR_EPITOPE)
    assert "natural protein-protein interface" not in clear["risk_factors"]


def test_pipeline_does_not_swallow_a_broken_detector(two_chain_pdb, feasibility_row, monkeypatch):
    """A detector that raises must fail the run, not fall back to a neutral score.

    The original bug was not the missing function so much as the bare ``try``
    that hid it. If this dimension is ever re-wrapped in a blanket
    ``except Exception``, this test goes red.
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr("scout.interfaces.detect_interfaces", _boom)

    with pytest.raises(RuntimeError, match="detector exploded"):
        feasibility_row(two_chain_pdb, "A", _BURIED_EPITOPE)


def test_dimension_weights_sum_to_one():
    """Dropping or reweighting a dimension must renormalise, or scores shift meaning."""
    from scout.feasibility import DIMENSION_WEIGHTS

    assert sum(DIMENSION_WEIGHTS.values()) == pytest.approx(1.0)
    assert DIMENSION_WEIGHTS["interface_competition"] > 0


# ---------------------------------------------------------------------------
# Gaps found by independent QC (docs/qc/scout-interface-competition-round1.md):
# the multi-partner aggregation is the behaviour the fix actually changed, and
# nothing pinned it; nor the single-chain case; nor the two defects below.
# ---------------------------------------------------------------------------


def test_single_chain_structure_scores_the_neutral_default(tmp_path, feasibility_row):
    """One chain means nothing to compete with — 1.0 is the correct answer here.

    This is also the case where the old constant happened to be right, which is
    why the bug survived: for single-chain uploads buggy and fixed agree exactly.
    """
    pdb = tmp_path / "one_chain.pdb"
    pdb.write_text("\n".join(_chain("A", 30, 0.0, 0.0, 1)) + "\nEND\n")

    row = feasibility_row(pdb, "A", _BURIED_EPITOPE)
    assert float(row["interface_competition"]) == pytest.approx(1.0)
    assert "natural protein-protein interface" not in row["risk_factors"]


def test_occlusion_by_several_partners_is_unioned_not_maxed(tmp_path, feasibility_row):
    """Two partners each burying part of the epitope must sum, not compete.

    Taking the largest single partner would score a fully-occluded epitope as
    half-free — optimistic in exactly the direction the original bug was.
    """
    # B covers the low half of the epitope, C the high half, from opposite sides.
    lines = (
        _chain("A", 30, 0.0, 0.0, 1)
        + _chain("B", 3, 3.5 * 11, 4.0, 5000)     # contacts ~A 11-14
        + _chain("C", 3, 3.5 * 15, -4.0, 6000)    # contacts ~A 15-18
    )
    pdb = tmp_path / "three_chain.pdb"
    pdb.write_text("\n".join(lines) + "\nEND\n")

    from scout.interfaces import detect_interfaces

    interfaces = detect_interfaces(pdb, "A")
    assert len(interfaces) == 2, f"fixture must present two partners, got {interfaces}"

    per_partner = [set(i["contact_residues"]) & set(_BURIED_EPITOPE) for i in interfaces]
    union = set().union(*per_partner)
    assert union > max(per_partner, key=len), (
        "fixture is not exercising the union: one partner already covers "
        "everything the other does"
    )

    row = feasibility_row(pdb, "A", _BURIED_EPITOPE)
    competition = float(row["interface_competition"])

    expected_union = max(0.1, 1.0 - len(union) / len(_BURIED_EPITOPE))
    expected_if_maxed = max(0.1, 1.0 - len(max(per_partner, key=len)) / len(_BURIED_EPITOPE))
    assert competition == pytest.approx(expected_union, abs=1e-3)
    assert competition < expected_if_maxed


def test_unresolved_requested_residues_do_not_dilute_the_score(two_chain_pdb, feasibility_row):
    """Residue numbers absent from the chain must not inflate the score.

    The buried fraction is scored over the residues that actually exist, the
    same set the other four dimensions use. Counting typed-but-unresolved
    residues in the denominator would report a fully-buried epitope as partly
    free.
    """
    resolved_only = float(
        feasibility_row(two_chain_pdb, "A", _BURIED_EPITOPE)["interface_competition"]
    )
    # 900-903 do not exist in chain A (which has residues 1-30).
    with_phantoms = float(
        feasibility_row(two_chain_pdb, "A", _BURIED_EPITOPE + [900, 901, 902, 903])[
            "interface_competition"
        ]
    )
    assert with_phantoms == pytest.approx(resolved_only)


def test_malformed_dbref_record_does_not_break_detection(tmp_path, feasibility_row):
    """A truncated DBREF must not raise out of the now-unguarded detector.

    ``_extract_chain_names`` indexes ``line[12]`` after testing only the record
    prefix. Chain names are cosmetic; before this guard a short DBREF line
    raised IndexError straight through run_feasibility_pipeline.
    """
    lines = ["DBREF X"] + _chain("A", 30, 0.0, 0.0, 1) + _chain("B", 12, 3.5 * 9, 4.0, 5000)
    pdb = tmp_path / "short_dbref.pdb"
    pdb.write_text("\n".join(lines) + "\nEND\n")

    row = feasibility_row(pdb, "A", _BURIED_EPITOPE)
    assert float(row["interface_competition"]) == pytest.approx(0.1)
