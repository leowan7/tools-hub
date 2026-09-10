"""The signup credit must admit every tool's SMALLEST REAL RUN.

``SIGNUP_CREDIT_USD`` used to be sized against the dearest displayed PILOT
PRICE (``tests/test_help_tool_guides.py::test_signup_credit_actually_covers
_every_pilot`` still checks that, and it is not enough on its own). What
ultimately admits a job is the CUSHIONED HOLD: ``shared/wallet_guard`` reserves
``cushioned_hold_usd`` and the SQL refuses on ``balance < hold``
(``supabase/migrations/0020_wallet_corrections.sql``). For any tool carrying a
``worst_case_gpu_seconds`` floor the hold sits well above the price, so a
credit that clears every price can still refuse the tool.

That is not hypothetical. esmfold2-design held $14.79 against a $15.00 credit
-- 21 cents of headroom, invisible -- until its container ceiling moved
3600 -> 5400 s, the floor reached the $15/seed hard cap, and the hold landed on
exactly $15.00. A new user who had spent one cent was then refused a tool the
page quoted at $9.86. The credit went to $20.00; this file is what stops the
gap reopening silently.

SCOPE, deliberately narrow: one unit of each tool's scaling parameter -- a new
user's first action. It does NOT assert the credit covers a scaled-up submit,
because no credit could: af2's batch accepts up to ``MAX_BATCH`` = 50 records
and holds $39.32 there. Past one unit, topping up is the intended path.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared import wallet_estimates as we
from shared.wallet import SIGNUP_CREDIT_USD

# The p90 branch only engages at >= MIN_HISTORICAL_RUNS, and it MOVES the
# answer: a shrunk estimate drops the cushion below the worst-case floor, and
# the floor then decides the hold. Both regimes have to clear the credit, so
# both are checked -- bootstrap (no history) and a p90 far below every session
# cap.
_P90_REGIMES = {"bootstrap": None, "p90_shrunk": 120.0}


def _smallest_run_params(spec: we.ToolSpec) -> dict:
    """One unit of whatever the tool scales on; ``{}`` if it does not scale."""
    return {spec.scaling_param: 1} if spec.scaling_param else {}


@pytest.mark.parametrize("regime", sorted(_P90_REGIMES))
@pytest.mark.parametrize("slug", sorted(we.TOOL_SPECS))
def test_signup_credit_covers_smallest_run(slug: str, regime: str, monkeypatch) -> None:
    """A brand-new user can run one unit of every tool without topping up."""
    monkeypatch.setattr(
        we, "_historical_p90_seconds", lambda _slug: _P90_REGIMES[regime]
    )
    spec = we.TOOL_SPECS[slug]
    params = _smallest_run_params(spec)
    hold = we.cushioned_hold_usd(None, slug, params)
    assert hold <= SIGNUP_CREDIT_USD, (
        f"{slug} ({regime}): the smallest real run holds {hold}, above the "
        f"{SIGNUP_CREDIT_USD} signup credit, so a brand-new user is refused a "
        f"tool the page quotes at "
        f"{we.estimated_cost_for_tool(None, slug, params)}. Raise "
        f"SIGNUP_CREDIT_USD or lower this tool's hold."
    )


@pytest.mark.parametrize("regime", sorted(_P90_REGIMES))
def test_credit_keeps_real_headroom_over_the_worst_tool(regime: str, monkeypatch) -> None:
    """Not just >=, but enough slack that spending a little does not lock a user out.

    Equality is what failed before: a $15.00 hold under a $15.00 credit passes a
    ``>=`` check and still refuses anyone who has spent a cent. This asserts the
    margin is a real one rather than a rounding coincidence.
    """
    monkeypatch.setattr(
        we, "_historical_p90_seconds", lambda _slug: _P90_REGIMES[regime]
    )
    worst_slug, worst_hold = max(
        (
            (slug, we.cushioned_hold_usd(None, slug, _smallest_run_params(spec)))
            for slug, spec in we.TOOL_SPECS.items()
        ),
        key=lambda pair: pair[1],
    )
    headroom = SIGNUP_CREDIT_USD - worst_hold
    assert headroom >= Decimal("1.00"), (
        f"{regime}: the dearest smallest-run hold is {worst_slug} at "
        f"{worst_hold} against a {SIGNUP_CREDIT_USD} credit — only {headroom} "
        f"of headroom. A new user who spends that much is locked out of "
        f"{worst_slug} entirely."
    )
