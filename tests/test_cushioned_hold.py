"""Cushioned hold sizing (Compute Campaigns Phase 2 step 3a).

The wallet HOLD (reservation) is a cushion above the point estimate, clamped
to the per-tool hard cap, so actual usually settles under the hold and the
ledger shows a release instead of a variance charge. The point estimate stays
the displayed price and the value the child stores as estimate_usd.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from shared.compute_campaigns import child_hold_usd, estimate_child_cost
from shared.wallet_estimates import (
    HOLD_CUSHION_MULTIPLIER,
    compute_hard_cap,
    cushioned_hold_usd,
    estimated_cost_for_tool,
)


# Money tests must not price against the live tool_jobs_p90 view: without
# this the estimate path reads production history through the service-role
# key in the repo-root .env. The two p90 tests below stub the lookup
# explicitly, but the other six in this file relied on the ambient absence
# of a client -- an assumption, not a guarantee, until now.
pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _pilot(n):
    return {"num_designs": n, "preset": "pilot"}


def test_cushion_below_cap_is_multiplier_times_point():
    # rfantibody at its 2-design baseline: 1.5x the point estimate ($4.3697 ->
    # $6.5546) sits under the $13.00 cap, so the hold is exactly the cushion.
    #
    # Was rfdiffusion@12, which LEFT this branch when expected_gpu_seconds was
    # corrected 1200 -> 2775 against the measured 277.5 s/design: its point
    # estimate rose to $4.0420 at n=12 and 1.5x that now exceeds the scaled
    # $6.00 cap, so rfdiffusion clamps and can no longer witness "cushion below
    # cap". Note the failure surfaced on the PRECONDITION below, not on the
    # cushion assertion -- re-pointing it at a smaller design count would have
    # kept it green while testing the clamp branch its sibling already covers.
    # rfantibody is the nearest equivalent: a campaign tool on the same GPU
    # class, and one of the specs with no worst_case_gpu_seconds floor to blur
    # what is being pinned (see the sibling test's note on af2/proteina/opendde).
    params = _pilot(2)
    point = estimated_cost_for_tool(None, "rfantibody", params)
    cap = compute_hard_cap("rfantibody", params)
    hold = cushioned_hold_usd(None, "rfantibody", params)
    assert HOLD_CUSHION_MULTIPLIER * point <= cap  # precondition: under cap
    assert abs(hold - HOLD_CUSHION_MULTIPLIER * point) < Decimal("0.001")
    assert hold > point            # a real cushion above the point estimate
    assert hold <= cap


def test_cushion_clamped_to_cap_for_expensive_tool():
    # bindcraft baseline: 1.5x the point estimate exceeds the hard cap, so the
    # hold clamps to the cap (reserving beyond it has no billing benefit).
    #
    # Was boltzgen, which stopped clamping AT THIS BASELINE when its gpu_class
    # was corrected from A100-80GB to the A100-40GB its container has always
    # run on: the point estimate fell to $6.07 and 1.5x that fits under the $10
    # cap. Only at the baseline -- the ratio is not scale-invariant, because
    # the scaled cap saturates at absolute_cap ($300) while the point estimate
    # keeps growing, so boltzgen clamps again from num_designs=66 up. That is a
    # property of the pure function: boltzgen's form submits `budget` (1-50),
    # never `num_designs`, so no real submission reaches n=66.
    # bindcraft moved the other way (40GB -> 80GB, $6.29 against an $8 cap) and
    # is the only clamping spec whose clamp is on the cushion ALONE: af2,
    # alphafold2, proteina and opendde also clamp at these params but each has
    # a worst_case_gpu_seconds floor in the same expression, which would blur
    # what this test is pinning. The precondition below is asserted, not
    # assumed, so a future rate change makes this loud instead of vacuous.
    params = _pilot(2)
    point = estimated_cost_for_tool(None, "bindcraft", params)
    cap = compute_hard_cap("bindcraft", params)
    hold = cushioned_hold_usd(None, "bindcraft", params)
    assert HOLD_CUSHION_MULTIPLIER * point > cap  # precondition: cushion over cap
    assert hold == cap
    assert point < hold <= cap     # still a cushion, up to the cap


def test_rfdiffusion_hold_sits_on_its_cap_while_the_spec_fallback_holds():
    # Pins a BRANCH, not a standing property of the tool. While the spec
    # fallback is in force the cushion (1.5x) exceeds the scaled cap by
    # ~1.05% at every design count, so the hold clamps AT the cap -- and
    # because settle_hold caps the charge with the same compute_hard_cap on
    # the same num_designs, charge <= hold and 0017_wallet.sql's variance
    # branches stay shut. The sibling test below asserts only hold <= cap,
    # which passed before expected_gpu_seconds was corrected too.
    #
    # The p90 is STUBBED rather than left to the environment. Tests have no
    # Supabase client, so the fallback is what runs here anyway -- but then
    # this test would pin that branch BY ACCIDENT and stay green after the
    # handover moved the behaviour underneath it. Naming the branch is the
    # difference between pinning a property and pinning a coincidence.
    with patch("shared.wallet_estimates._historical_p90_seconds", return_value=None):
        for n in (1, 8, 10, 12, 100):
            params = _pilot(n)
            hold = cushioned_hold_usd(None, "rfdiffusion", params)
            cap = compute_hard_cap("rfdiffusion", params)
            assert hold == cap, f"n={n}: hold {hold} != cap {cap}"


def test_rfdiffusion_hold_leaves_its_cap_if_the_p90_lands_below_2746():
    # The other half, and the half that costs money: IF the 30-day p90 lands
    # below ~2746 GPU-s, estimated_cost_for_tool prefers it, the hold drops
    # under the cap, and the variance-debit / absorbed_variance branches
    # reopen. 2220 is the one measured post-update run (job 25471e07).
    #
    # Whether that actually happens is undetermined and this test does not
    # claim it does -- the repo's two runtime models for a 10-design chunk
    # straddle the threshold (2775 flat, 2600 on meta.py's fixed+per-design).
    # It is pinned so that "the variance path is closed for rfdiffusion"
    # cannot be read as unconditional: it is a property of the bootstrap
    # constant, and this is the condition that ends it.
    params = _pilot(8)
    with patch("shared.wallet_estimates._historical_p90_seconds", return_value=2220.0):
        hold = cushioned_hold_usd(None, "rfdiffusion", params)
    cap = compute_hard_cap("rfdiffusion", params)
    assert hold == Decimal("4.0419")
    assert hold < cap


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
