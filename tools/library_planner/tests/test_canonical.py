"""Canonical smoke tests for the yeast display library planner."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

# Ensure the parent of the library_planner package is importable.
_HERE = Path(__file__).resolve().parent
_PACKAGE_PARENT = _HERE.parent.parent
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

from library_planner.planner import plan_library  # noqa: E402


def test_canonical_vhh_nnk_6_positions_naive_10nm():
    """6 NNK positions, VHH, naive, 10 nM: feasible canonical plan.

    Expected:
        theoretical ~ 32**6 ~ 1e9
        functional ~ theoretical * (31/32)**6 ~ 8.9e8
        recommended NGS reads ~ 3 * library (Poisson 95%)
        3 to 4 sort rounds
        feasibility: no hard errors (naive NNK+8 positions+<10nM warning
        does NOT apply at 6 positions)
    """
    plan = plan_library(
        scaffold="VHH",
        diversification_positions=6,
        diversification_scheme="NNK",
        target_kd_nm=10.0,
        starting_material="naive",
    )

    theoretical = plan["library"]["theoretical_size"]
    functional = plan["library"]["functional_size"]

    assert theoretical == 32 ** 6
    expected_functional = int(round(theoretical * (31 / 32) ** 6))
    assert functional == expected_functional
    assert 8e8 < theoretical < 2e9
    assert 8e8 < functional < 1e9

    # Library size is capped at the yeast transformation ceiling for
    # downstream NGS calculations.
    recommended = plan["ngs_depth"]["recommended"]
    sortable = min(functional, plan["library"]["yeast_transformation_ceiling"])
    assert abs(recommended - int(math.ceil(-sortable * math.log(1 - 0.95)))) < 10

    # 95% Poisson coverage is ~3x library.
    assert 2.5 * sortable < recommended < 3.5 * sortable

    sort_rounds = plan["sort_strategy"]
    assert 3 <= len(sort_rounds) <= 4

    # No hard errors at this scale.
    errors = [f for f in plan["feasibility"] if f["severity"] == "error"]
    assert errors == []
    assert plan["library"]["feasible_on_yeast"] is True


def test_naive_large_library_gets_macs_first():
    """A naive library far above 1e8 members must open on a MACS round.

    Principle 3 of the sort_strategy module docstring triggers MACS on
    ``library_size > 1e8`` for naive material
    (``tools/library_planner/sort_strategy.py::recommend_sort_rounds``).
    The planner
    used to hand that test ``min(functional, yeast_transformation_ceiling)``,
    and since the ceiling defaults to 1e8 the comparison could never be true,
    so every plan the tool returned was three FACS rounds. 7 NNK positions is
    ~2.8e10 stop-free variants, well above the trigger.
    """
    plan = plan_library(
        scaffold="VHH",
        diversification_positions=7,
        diversification_scheme="NNK",
        target_kd_nm=10.0,
        starting_material="naive",
    )

    assert plan["library"]["functional_size"] > 1e10
    rounds = plan["sort_strategy"]
    assert rounds[0]["method"] == "MACS", (
        "a naive library of "
        f"{plan['library']['functional_size']:.2e} members opened on "
        f"{rounds[0]['method']}; the MACS pre-enrichment exists so the full "
        "diversity is compressed before the FACS bottleneck"
    )
    assert [r["method"] for r in rounds] == ["MACS", "FACS", "FACS", "FACS"]
    assert "MACS" in plan["summary"]


@pytest.mark.parametrize(
    "ceiling, expected_macs_pool",
    [
        (10 ** 8, 10 ** 8),
        (10 ** 10, 10 ** 8),
        (5 * 10 ** 7, 5 * 10 ** 7),
    ],
)
def test_macs_round_is_priced_at_its_recovery_not_a_sort_gate(
    ceiling, expected_macs_pool
):
    """The MACS row is priced at what MACS recovers, at any ceiling.

    A MACS round has no sort gate: ``recommend_sort_rounds`` sets
    ``gate_percent`` to None and instead states the cells it hands to round 2
    (``sort_strategy.MACS_RECOVERY_CELLS``), a cytometer-throughput figure
    that does not scale with the transformation ceiling. Two ways to get this
    wrong, both pinned here:

    * substituting a stand-in gate for the missing one prices the MACS row at
      that gate AND pushes every FACS round one multiplication deeper,
      shrinking each of their read budgets by the same factor;
    * pricing it at the whole sortable library says MACS compressed nothing,
      and at a raised ceiling overstates every FACS round by the same factor
      while the round's own ``notes`` still promise ~1e8 cells.

    The third case is the other direction: a flask smaller than the recovery
    cannot hand on more cells than it holds.
    """
    plan = plan_library(
        scaffold="VHH",
        diversification_positions=7,
        diversification_scheme="NNK",
        target_kd_nm=10.0,
        starting_material="naive",
        yeast_transformation_ceiling=ceiling,
    )

    rounds = plan["sort_strategy"]
    per_round = plan["ngs_depth"]["per_round"]

    assert rounds[0]["method"] == "MACS"
    # The NGS pills render beside the sort card, so the numbering has to line
    # up round for round (templates/library_planner_results.html).
    assert [r["round"] for r in per_round] == [r["round"] for r in rounds]
    assert per_round[0]["pool_size"] == expected_macs_pool, (
        f"the MACS round reports a pool of {per_round[0]['pool_size']:,} "
        f"at a transformation ceiling of {ceiling:,}; it has no sort gate to "
        f"shrink the pool by, and its own notes say {rounds[0]['notes']!r}"
    )
    assert "1e+08 cells" in rounds[0]["notes"]
    assert per_round[0]["pool_size"] <= 10 ** 8, (
        "priced above the cell count the round's own notes promise"
    )

    pool = float(expected_macs_pool)
    for sort_round, ngs_round in zip(rounds[1:], per_round[1:]):
        pool *= sort_round["gate_percent"] / 100.0
        assert ngs_round["pool_size"] == int(math.ceil(pool))
        assert ngs_round["recommended_reads"] == int(
            math.ceil(-ngs_round["pool_size"] * math.log(1 - 0.90))
        )


def test_naive_small_library_skips_macs():
    """Below the 1e8 trigger a naive library still starts on FACS.

    Pins the other side of the boundary: 5 NNK positions is ~2.9e7 stop-free
    variants, which is FACS-tractable without a magnetic pre-enrichment.
    """
    plan = plan_library(
        scaffold="VHH",
        diversification_positions=5,
        diversification_scheme="NNK",
        target_kd_nm=10.0,
        starting_material="naive",
    )

    assert plan["library"]["functional_size"] < 1e8
    assert [r["method"] for r in plan["sort_strategy"]] == ["FACS"] * 3


def test_infeasible_scfv_nnk_20_positions():
    """20 NNK positions on scFv is infeasible and must trip the hard error.

    Library is ~32**20 ~ 1.2e30 which is ~22 orders of magnitude above the
    yeast transformation ceiling. Planner must surface a severity=error
    with code library_exceeds_transformation_ceiling, and
    library.feasible_on_yeast must flip to False.
    """
    plan = plan_library(
        scaffold="scFv",
        diversification_positions=20,
        diversification_scheme="NNK",
        target_kd_nm=1.0,
        starting_material="naive",
    )

    assert plan["library"]["theoretical_size"] == 32 ** 20
    assert plan["library"]["feasible_on_yeast"] is False

    error_codes = [
        f["code"] for f in plan["feasibility"] if f["severity"] == "error"
    ]
    assert "library_exceeds_transformation_ceiling" in error_codes


def test_trimer_10_positions_vhh_immunized_1nm():
    """Trimer, 10 positions, VHH, immunized, 1 nM.

    Expected theoretical = 20**10 = 1.024e13. No stop codon dilution with
    trimer so functional == theoretical. Trimer-at-10-positions does not
    trip the >12-position trimer warning, but the library is far above
    the yeast ceiling so the hard error should fire.
    """
    plan = plan_library(
        scaffold="VHH",
        diversification_positions=10,
        diversification_scheme="trimer",
        target_kd_nm=1.0,
        starting_material="immunized",
    )

    assert plan["library"]["theoretical_size"] == 20 ** 10
    assert plan["library"]["functional_size"] == 20 ** 10

    codes = [f["code"] for f in plan["feasibility"]]
    # At 10 trimer positions the library exceeds ceiling by ~5 orders, so
    # the hard error fires.
    assert "library_exceeds_transformation_ceiling" in codes


def test_trimer_12_positions_cost_warning():
    """Trimer at 13 positions fires the cost warning."""
    plan = plan_library(
        scaffold="VHH",
        diversification_positions=13,
        diversification_scheme="trimer",
        target_kd_nm=1.0,
        starting_material="immunized",
    )
    codes = [f["code"] for f in plan["feasibility"]]
    assert "trimer_cost_at_high_positions" in codes


def test_rejects_bad_scaffold():
    """Unknown scaffold name must raise ValueError."""
    with pytest.raises(ValueError):
        plan_library(
            scaffold="banana",
            diversification_positions=6,
            diversification_scheme="NNK",
            target_kd_nm=10.0,
            starting_material="naive",
        )


def test_rejects_negative_positions():
    """Negative or zero positions must raise ValueError."""
    with pytest.raises(ValueError):
        plan_library(
            scaffold="VHH",
            diversification_positions=-3,
            diversification_scheme="NNK",
            target_kd_nm=10.0,
            starting_material="naive",
        )
    with pytest.raises(ValueError):
        plan_library(
            scaffold="VHH",
            diversification_positions=0,
            diversification_scheme="NNK",
            target_kd_nm=10.0,
            starting_material="naive",
        )


def test_rejects_bad_scheme():
    """Unknown codon scheme must raise ValueError."""
    with pytest.raises(ValueError):
        plan_library(
            scaffold="VHH",
            diversification_positions=6,
            diversification_scheme="NNZ",
            target_kd_nm=10.0,
            starting_material="naive",
        )


def test_rejects_bad_starting_material():
    """Unknown starting material must raise ValueError."""
    with pytest.raises(ValueError):
        plan_library(
            scaffold="VHH",
            diversification_positions=6,
            diversification_scheme="NNK",
            target_kd_nm=10.0,
            starting_material="mystery_mix",
        )


def test_picomolar_kd_hard_error():
    """KD below 0.01 nM must trip the yeast display floor error."""
    plan = plan_library(
        scaffold="VHH",
        diversification_positions=5,
        diversification_scheme="NNK",
        target_kd_nm=0.005,
        starting_material="immunized",
    )
    codes = [
        f["code"] for f in plan["feasibility"] if f["severity"] == "error"
    ]
    assert "kd_below_yeast_display_floor" in codes


def test_output_shape_is_jsonable():
    """The full plan dict must serialize to JSON without error."""
    import json

    plan = plan_library(
        scaffold="VHH",
        diversification_positions=6,
        diversification_scheme="NNK",
        target_kd_nm=10.0,
        starting_material="naive",
    )
    # Must not raise.
    json.dumps(plan)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
