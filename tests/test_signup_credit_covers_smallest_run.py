"""The signup credit must admit every tool's SMALLEST REAL RUN.

``SIGNUP_CREDIT_USD`` was historically sized against the dearest displayed PILOT
PRICE (``tests/test_help_tool_guides.py::test_signup_credit_actually_covers
_every_pilot`` still checks that, and it is the weaker of the two). What
ultimately admits a job is the CUSHIONED HOLD: ``shared/wallet_guard`` reserves
``cushioned_hold_usd`` via ``shared.wallet.reserve_hold``, which refuses on
``balance < hold`` before any SQL runs. On any tool carrying a
``worst_case_gpu_seconds`` floor the hold sits well above the price, so a credit
that clears every price can still refuse the tool outright.

That was live in production, on more than the tool that exposed it. At the
commit before this guard existed, with the credit at $15.00, the 1-unit holds
were:

    proteina         $15.0000   headroom $0.0000
    opendde          $15.0000   headroom $0.0000
    esmfold2-design  $14.7921   headroom $0.2079

Two tools were already refusing any new user who had spent a single cent.
esmfold2-design joined them when its container ceiling moved 3600 -> 5400 s and
its floor reached the $15/seed cap. The credit went to $20.00.

WHAT THIS FILE ACTUALLY DISCRIMINATES ON, stated plainly because the first draft
of this docstring oversold it. At one unit of the scaling parameter
``compute_hard_cap`` clamps its ratio to 1.0, so the hold can never exceed
``base_hard_cap_usd``. This file therefore fails only when some tool's
``base_hard_cap_usd`` (or the credit) moves. The cushion multiplier, the markup,
the GPU rate card, ``expected_gpu_seconds`` and ``worst_case_gpu_seconds`` can
all be changed arbitrarily without it noticing -- those are guarded, where they
are guarded at all, by ``tests/test_worst_case_hold_floor.py``. That is not a
defect in the rule: for a NEW USER the per-tool cap IS the binding number, and
it is the one nothing else was checking against the credit.

SCOPE: one unit of each tool's scaling parameter -- a new user's first action.
It does NOT assert the credit covers a scaled-up submit, and no credit could:
af2's batch accepts up to ``MAX_BATCH`` = 50 records and holds $39.32 there,
$21.00 at just 14. Past one unit, topping up is the intended path.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared import wallet_estimates as we
from shared.wallet import SIGNUP_CREDIT_USD

# The historical-p90 branch engages at >= MIN_HISTORICAL_RUNS and MOVES the
# hold: a shrunk estimate drops the cushion below the worst-case floor and the
# floor then decides. Both regimes are evaluated and the LARGER hold is the one
# asserted, rather than parametrising -- a review established that the shrunk
# regime never caught anything the bootstrap missed, so parametrising doubled
# the test count without adding discrimination. Taking the max keeps both code
# paths exercised and asserts against the worse of the two.
_P90_REGIMES = {"bootstrap": None, "p90_shrunk": 120.0}


def _smallest_run_params(spec: we.ToolSpec) -> dict:
    """One unit of whatever the tool scales on; ``{}`` if it does not scale."""
    return {spec.scaling_param: 1} if spec.scaling_param else {}


def _worst_hold(slug: str, monkeypatch) -> tuple[Decimal, str]:
    """The larger 1-unit hold across both p90 regimes, and which produced it."""
    spec = we.TOOL_SPECS[slug]
    params = _smallest_run_params(spec)
    worst, worst_regime = Decimal("-1"), ""
    for regime, p90 in _P90_REGIMES.items():
        monkeypatch.setattr(we, "_historical_p90_seconds", lambda _s, _v=p90: _v)
        hold = we.cushioned_hold_usd(None, slug, params)
        if hold > worst:
            worst, worst_regime = hold, regime
    return worst, worst_regime


@pytest.mark.parametrize("slug", sorted(we.TOOL_SPECS))
def test_signup_credit_covers_smallest_run(slug: str, monkeypatch) -> None:
    """A brand-new user can run one unit of every tool without topping up."""
    hold, regime = _worst_hold(slug, monkeypatch)
    params = _smallest_run_params(we.TOOL_SPECS[slug])
    assert hold <= SIGNUP_CREDIT_USD, (
        f"{slug} ({regime}): the smallest real run holds {hold}, above the "
        f"{SIGNUP_CREDIT_USD} signup credit, so a brand-new user is refused a "
        f"tool the page quotes at "
        f"{we.estimated_cost_for_tool(None, slug, params)}. Raise "
        f"SIGNUP_CREDIT_USD or lower this tool's base_hard_cap_usd."
    )


def test_credit_keeps_real_headroom_over_the_worst_tool(monkeypatch) -> None:
    """Not just >=, but at least $1.00 of slack.

    Equality is exactly what failed before: a $15.00 hold under a $15.00 credit
    satisfies ``<=`` and still refuses anyone who has spent a cent. Note the
    bound is a threshold, not a margin -- a credit that clears the worst hold by
    exactly $1.00 passes.
    """
    worst_slug, worst_hold, worst_regime = "", Decimal("-1"), ""
    for slug in we.TOOL_SPECS:
        hold, regime = _worst_hold(slug, monkeypatch)
        if hold > worst_hold:
            worst_slug, worst_hold, worst_regime = slug, hold, regime
    headroom = SIGNUP_CREDIT_USD - worst_hold
    assert headroom >= Decimal("1.00"), (
        f"the dearest smallest-run hold is {worst_slug} at {worst_hold} "
        f"({worst_regime}) against a {SIGNUP_CREDIT_USD} credit — only "
        f"{headroom} of headroom. A new user who spends that much is locked out "
        f"of {worst_slug} entirely, which is the defect this file exists for."
    )


def test_hold_at_one_unit_is_the_per_tool_cap(monkeypatch) -> None:
    """Pin the mechanism the two tests above actually rest on.

    They can only fail via ``base_hard_cap_usd`` because ``compute_hard_cap``
    floors its ratio at 1.0, making the cap the binding clamp at one unit. If
    that ever stops being true -- a scaling change, a cap that stops clamping --
    the tests above quietly start measuring something else, and this fails first
    and says so.
    """
    for slug, spec in we.TOOL_SPECS.items():
        monkeypatch.setattr(we, "_historical_p90_seconds", lambda _s: None)
        params = _smallest_run_params(spec)
        cap = we.compute_hard_cap(slug, params)
        assert cap == min(spec.base_hard_cap_usd, spec.absolute_cap_usd), (
            f"{slug}: compute_hard_cap at one unit is {cap}, not "
            f"base_hard_cap_usd {spec.base_hard_cap_usd}. The smallest-run "
            f"assertions in this file assume the cap binds there."
        )
        hold, _ = _worst_hold(slug, monkeypatch)
        assert hold <= cap, f"{slug}: hold {hold} exceeds its own cap {cap}"
