"""Unit tests for shared/target_launch.py.

This module decides how much of a user's wallet ONE click can commit, so the
tests are written against the two ways that goes wrong rather than against the
happy path:

* preauth looped instead of summed, which passes N times on a balance that
  funds one;
* concurrency divided naively, which widens proteina past its A100 throttle
  and quadruples its first-wave hold.

``shared.target_launch`` is pure, but ``campaign_preauth`` reaches the wallet,
so every test that touches it patches it. The module-level ``isolate_supabase``
is belt-and-braces on top of that: app.py's ``load_dotenv()`` puts real
production credentials in the environment, and a preauth that escaped its
patch would read a live balance.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.compute_campaigns import (
    GLOBAL_USER_INFLIGHT_CAP,
    first_wave_hold_usd,
    launch_concurrency_for,
    plan_chunks,
)
from shared.target_launch import (
    PACE_BURST,
    PACE_STEADY,
    MultiLaunchPlan,
    ToolLaunchSpec,
    concurrency_note,
    divide_concurrency,
    plan_multi_launch,
    preauth_multi_launch,
)

# All 7 campaign tools. Kept explicit rather than derived from SUPPORTED_TOOLS
# so that adding a tool to the platform makes these tests fail loudly instead
# of quietly changing what "all of them" means.
ALL_SEVEN = [
    "rfdiffusion", "bindcraft", "boltzgen", "pxdesign",
    "rfantibody", "proteina", "iggm",
]


def _spec(tool, designs=24, preset="pilot"):
    return ToolLaunchSpec(tool=tool, preset=preset, requested_designs=designs,
                          params={})


# ---------------------------------------------------------------------------
# divide_concurrency
# ---------------------------------------------------------------------------


def test_one_tool_is_bit_identical_to_launching_it_alone():
    """The composition layer must not change the existing single-tool path.
    If it does, every campaign created through the target flow silently starts
    at a different concurrency than the same campaign created the old way."""
    for tool in ALL_SEVEN:
        assert divide_concurrency([tool]) == (launch_concurrency_for(tool),)


def test_proteina_is_never_widened_past_its_own_throttle():
    """One proteina shard is a full A100, which is why it is pinned to 4. A
    naive CAP // n hands it 16 at n=2 -- quadrupling the throttle AND its
    first-wave hold, on the most expensive tool in the fleet."""
    assert launch_concurrency_for("proteina") == 4
    # At n=2 the share is 16, well above proteina's 4.
    assert divide_concurrency(["rfdiffusion", "proteina"]) == (16, 4)
    assert divide_concurrency(["proteina", "proteina"]) == (4, 4)


def test_division_only_ever_narrows_a_tool():
    """The general form of the proteina case: no arrangement of tools may give
    any tool more than it would get by itself."""
    for n in range(1, 8):
        for tools in ([t] * n for t in ALL_SEVEN):
            got = divide_concurrency(tools)
            assert all(c <= launch_concurrency_for(t)
                       for c, t in zip(got, tools)), (n, tools, got)


def test_concurrency_is_never_zero_at_any_width_or_pace():
    """0 is the dangerous value, not a small one. create_campaign writes
    ``max(1, int(c)) if c else launch_concurrency_for(tool)`` and 0 is falsy,
    so a zero does not clamp -- it SILENTLY restores the tool default, undoing
    the division exactly when the launch is widest."""
    for pace in (PACE_BURST, PACE_STEADY):
        for n in range(1, 33):
            got = divide_concurrency(["bindcraft"] * n, pace)
            assert len(got) == n
            assert all(c >= 1 for c in got), (pace, n, got)


def test_a_repeated_tool_gets_its_own_slot():
    """Running the same tool twice on one target is legitimate (adding 400
    more BindCraft designs). A tool-keyed mapping collapses the two, so the
    second campaign would be sized as though the first did not exist."""
    got = divide_concurrency(["bindcraft", "bindcraft", "bindcraft"])
    assert len(got) == 3
    # Three entries share the cap, so each is narrower than one alone.
    assert all(c < launch_concurrency_for("bindcraft") for c in got)


def test_steady_starts_narrower_than_burst():
    burst = divide_concurrency(ALL_SEVEN, PACE_BURST)
    steady = divide_concurrency(ALL_SEVEN, PACE_STEADY)
    assert all(s <= b for s, b in zip(steady, burst))
    assert sum(steady) < sum(burst)


def test_an_unknown_pace_falls_back_to_burst_rather_than_dividing_by_zero():
    assert divide_concurrency(ALL_SEVEN, "nonsense") == \
        divide_concurrency(ALL_SEVEN, PACE_BURST)


def test_no_tools_divides_nothing():
    assert divide_concurrency([]) == ()


def test_the_widest_launch_stays_within_the_global_cap():
    """The cap is the reason to divide at all. Seven tools may not sum past
    it, or the first-wave gate collects for slots that cannot exist."""
    assert sum(divide_concurrency(ALL_SEVEN)) <= GLOBAL_USER_INFLIGHT_CAP


# ---------------------------------------------------------------------------
# plan_multi_launch
# ---------------------------------------------------------------------------


def test_budget_is_the_sum_of_the_per_tool_budgets():
    specs = [_spec(t) for t in ALL_SEVEN]
    plan = plan_multi_launch(specs)
    expected = sum((plan_chunks(t, 24, "pilot").budget_usd for t in ALL_SEVEN),
                   Decimal("0"))
    assert plan.budget_usd == expected


def test_first_wave_is_summed_per_tool_at_that_tools_own_concurrency():
    """Not blended. Tools have different per-chunk holds and different
    concurrency after the division, so one blended figure is wrong in both
    directions at once."""
    # Two tools whose concurrency genuinely DIFFERS after the division
    # (rfdiffusion 16, proteina 4). At seven tools every value converges to 4,
    # so per-tool and blended agree there and the check below would be
    # vacuous -- it passed against a blended implementation when written that
    # way.
    plan = plan_multi_launch([_spec("rfdiffusion", 200), _spec("proteina", 200)])
    assert plan.concurrency == (16, 4)

    expected = sum(
        (first_wave_hold_usd(p, c) for p, c in zip(plan.plans, plan.concurrency)),
        Decimal("0"),
    )
    assert plan.first_wave_usd == expected

    # Blending at either tool's concurrency gives a different, wrong answer:
    # too high for proteina at 16, too low for rfdiffusion at 4.
    for shared_conc in (16, 4):
        blended = sum((first_wave_hold_usd(p, shared_conc) for p in plan.plans),
                      Decimal("0"))
        assert blended != plan.first_wave_usd, shared_conc


def test_plans_and_concurrency_stay_aligned_with_specs():
    specs = [_spec("bindcraft"), _spec("proteina"), _spec("bindcraft")]
    plan = plan_multi_launch(specs)
    assert len(plan.plans) == len(plan.concurrency) == len(plan.specs) == 3
    assert [p.tool for p in plan.plans] == ["bindcraft", "proteina", "bindcraft"]
    # The middle entry is proteina's throttle, not its neighbours'.
    assert plan.concurrency[1] == 4


def test_a_bad_design_count_on_one_tool_blocks_the_whole_launch():
    """Any tool failing to plan must stop everything. A partial launch spends
    real money on a subset the user did not choose."""
    specs = [_spec("bindcraft"), _spec("boltzgen", designs=0)]
    with pytest.raises(ValueError):
        plan_multi_launch(specs)


def test_launching_no_tools_is_rejected():
    with pytest.raises(ValueError):
        plan_multi_launch([])


def test_steady_makes_the_start_gate_independent_of_the_design_count():
    """The escape valve's whole point. At steady the first wave is one chunk
    per tool, so asking for more designs raises the budget but not the amount
    that must be in the wallet to begin."""
    small = plan_multi_launch([_spec(t, 24) for t in ALL_SEVEN], PACE_STEADY)
    large = plan_multi_launch([_spec(t, 48) for t in ALL_SEVEN], PACE_STEADY)
    assert large.budget_usd > small.budget_usd
    assert large.first_wave_usd == small.first_wave_usd


def test_rows_itemise_every_tool():
    plan = plan_multi_launch([_spec(t) for t in ALL_SEVEN])
    rows = plan.rows()
    assert [r["tool"] for r in rows] == ALL_SEVEN
    assert all(r["first_wave_usd"] > 0 for r in rows)
    assert plan.total_designs == 24 * 7


# ---------------------------------------------------------------------------
# preauth_multi_launch -- the money gate
# ---------------------------------------------------------------------------


def test_preauth_is_called_exactly_once_for_the_whole_launch():
    """The defect this module exists to prevent. campaign_preauth never
    debits, so looping it reads the SAME balance N times: all 7 pass a gate
    only 1 can afford, and the driver then parks 6 in
    paused_insufficient_funds. The user clicked "run 7 tools", got 7 rows, and
    6 of them silently did nothing."""
    plan = plan_multi_launch([_spec(t) for t in ALL_SEVEN])
    with patch("shared.target_launch.campaign_preauth") as pre:
        preauth_multi_launch("u-1", plan)
    assert pre.call_count == 1


def test_preauth_gates_on_the_summed_budget_and_summed_first_wave():
    plan = plan_multi_launch([_spec(t) for t in ALL_SEVEN])
    with patch("shared.target_launch.campaign_preauth") as pre:
        preauth_multi_launch("u-1", plan)
    args = pre.call_args.args
    assert args[0] == "u-1"
    assert args[1] == plan.budget_usd
    assert args[2] == plan.first_wave_usd
    # Not one tool's figures. Seven tools must not gate like one.
    assert args[1] > plan.plans[0].budget_usd


def test_the_gate_is_the_first_wave_not_the_full_budget():
    """Under fund-and-drain the wallet is the ceiling and the rest funds as
    the campaigns drain, so gating on the full budget refuses launches that
    would have completed."""
    plan = plan_multi_launch([_spec(t, 200) for t in ALL_SEVEN])
    with patch("shared.target_launch.campaign_preauth") as pre:
        preauth_multi_launch("u-1", plan)
    _uid, budget, gate = pre.call_args.args
    assert gate < budget


# ---------------------------------------------------------------------------
# concurrency_note
# ---------------------------------------------------------------------------


def test_a_single_tool_launch_says_nothing_about_sharing():
    plan = plan_multi_launch([_spec("bindcraft")])
    assert concurrency_note(plan) is None


def test_a_narrowed_launch_explains_why():
    plan = plan_multi_launch([_spec(t) for t in ALL_SEVEN])
    note = concurrency_note(plan)
    assert note is not None
    assert str(GLOBAL_USER_INFLIGHT_CAP) in note
    # It must not imply the user pays more for running them together.
    assert "cost are unchanged" in note
