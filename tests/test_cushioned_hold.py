"""Cushioned hold sizing (Compute Campaigns Phase 2 step 3a).

The wallet HOLD (reservation) is a cushion above the point estimate, clamped
to the per-tool hard cap, so actual usually settles under the hold and the
ledger shows a release instead of a variance charge. The point estimate stays
the displayed price and the value the child stores as estimate_usd.
"""

from decimal import Decimal

from shared.compute_campaigns import child_hold_usd, estimate_child_cost
from shared.wallet_estimates import (
    HOLD_CUSHION_MULTIPLIER,
    compute_hard_cap,
    cushioned_hold_usd,
    estimated_cost_for_tool,
)


def _pilot(n):
    return {"num_designs": n, "preset": "pilot"}


def test_cushion_below_cap_is_multiplier_times_point():
    # rfdiffusion at 12 designs: 1.5x the point estimate sits under the cap,
    # so the hold is exactly the cushion.
    params = _pilot(12)
    point = estimated_cost_for_tool(None, "rfdiffusion", params)
    cap = compute_hard_cap("rfdiffusion", params)
    hold = cushioned_hold_usd(None, "rfdiffusion", params)
    assert HOLD_CUSHION_MULTIPLIER * point <= cap  # precondition: under cap
    assert abs(hold - HOLD_CUSHION_MULTIPLIER * point) < Decimal("0.001")
    assert hold > point            # a real cushion above the point estimate
    assert hold <= cap


def test_cushion_clamped_to_cap_for_expensive_tool():
    # boltzgen baseline: 1.5x the point estimate exceeds the hard cap, so the
    # hold clamps to the cap (reserving beyond it has no billing benefit).
    params = _pilot(2)
    point = estimated_cost_for_tool(None, "boltzgen", params)
    cap = compute_hard_cap("boltzgen", params)
    hold = cushioned_hold_usd(None, "boltzgen", params)
    assert HOLD_CUSHION_MULTIPLIER * point > cap  # precondition: cushion over cap
    assert hold == cap
    assert point < hold <= cap     # still a cushion, up to the cap


def test_hold_never_exceeds_hard_cap_across_tools():
    for tool, n in [
        ("rfdiffusion", 12), ("bindcraft", 3), ("boltzgen", 50),
        ("mpnn", 8), ("af2", 4), ("rfantibody", 2),
    ]:
        params = _pilot(n)
        hold = cushioned_hold_usd(None, tool, params)
        cap = compute_hard_cap(tool, params)
        assert Decimal("0") < hold <= cap, f"{tool}: hold {hold} > cap {cap}"


def test_child_hold_is_cushioned_but_point_estimate_is_not():
    # The campaign driver reserves child_hold_usd (cushioned) yet stores
    # estimate_child_cost (the point estimate) on the child as estimate_usd.
    point = estimate_child_cost("rfdiffusion", 12)
    hold = child_hold_usd("rfdiffusion", 12)
    assert hold > point
    assert hold <= compute_hard_cap("rfdiffusion", _pilot(12))


def test_child_hold_boltzgen_prices_at_fixed_pool_baseline():
    # boltzgen is flat per job: the hold does not scale with the design budget.
    assert child_hold_usd("boltzgen", 10) == child_hold_usd("boltzgen", 50)


def test_child_hold_pxdesign_prices_at_fixed_container_baseline():
    # pxdesign is fixed-container like boltzgen: one 3600s container runs the
    # whole 24-design chunk, so the HOLD must not scale with the chunk's design
    # count. Regression guard: pricing per-design here (as the estimate path was
    # fixed but the hold path once was not) inflates the hold ~12x and the
    # first-wave START gate with it (money-safe, but a bogus admission block).
    assert child_hold_usd("pxdesign", 24) == child_hold_usd("pxdesign", 2)
    # The per-container hold matches the cushioned baseline, far below the naive
    # 24-design price a per-design hold would charge.
    baseline = cushioned_hold_usd(None, "pxdesign", _pilot(2))
    assert child_hold_usd("pxdesign", 24) == baseline
    naive_per_design = cushioned_hold_usd(None, "pxdesign", _pilot(24))
    assert child_hold_usd("pxdesign", 24) < naive_per_design / 5
