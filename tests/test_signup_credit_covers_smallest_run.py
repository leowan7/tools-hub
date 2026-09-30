"""Which tools the signup credit admits at their SMALLEST REAL RUN, and which it does not.

What admits a job is the CUSHIONED HOLD: ``shared/wallet_guard`` reserves
``cushioned_hold_usd`` via ``shared.wallet.reserve_hold``, which refuses on
``balance < hold`` before any SQL runs. On any tool carrying a
``worst_case_gpu_seconds`` floor the hold sits well above the displayed price.

History: this file used to assert the credit admitted EVERY tool's smallest run,
which is why the credit went $15 -> $20 on 2026-09-10. On 2026-09-28 Leo cut the
credit to $5 on purpose (the 2026-09-28 funnel review, section 7, which is not
in this repository: at $20 nobody ever ran out). At $5 the binder design tools and OpenDDE need a top-up, and the copy
now says so (templates/pricing.html, templates/help/getting_started.html,
templates/email/send_signup_credit.html). This file pins that split, so a price,
cap or credit change that moves a tool across it fails here and sends someone to
re-read that copy.

A tool is "needs a top-up" when its smallest-run hold leaves less than $1.00 of
the credit: a hold EQUAL to the credit (rfdiffusion, $5.00) admits a user who
has spent nothing and refuses one who has spent a cent.

At one unit of the scaling parameter ``compute_hard_cap`` clamps its ratio to
1.0, so for the capped tools the hold is ``base_hard_cap_usd``; see
``test_hold_at_one_unit_is_the_per_tool_cap``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared import wallet_estimates as we
from shared.wallet import SIGNUP_CREDIT_USD

# Both p90 regimes are evaluated and the LARGER hold is used: the historical-p90
# branch engages at >= MIN_HISTORICAL_RUNS and can move the hold.
_P90_REGIMES = {"bootstrap": None, "p90_shrunk": 120.0}

_HEADROOM = Decimal("1.00")

# Tools a brand-new user cannot run from the signup credit alone. The copy
# listed in the module docstring names exactly this group.
NEEDS_TOPUP = {
    "bindcraft",
    "boltzgen",
    "esmfold2-design",
    "opendde",
    "pxdesign",
    "rfdiffusion",
}
# proteina and rfantibody left this set on 2026-09-30 when their bootstrap
# estimates were cut to measured runtimes (TOOL_SPECS in
# shared/wallet_estimates.py). The copy ("most binder design runs ... need a
# top-up") still holds: five binder design tools remain here.


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


@pytest.mark.parametrize("slug", sorted(set(we.TOOL_SPECS) - NEEDS_TOPUP))
def test_signup_credit_covers_smallest_run(slug: str, monkeypatch) -> None:
    """Every tool outside NEEDS_TOPUP runs one unit from the credit, with $1 spare."""
    hold, regime = _worst_hold(slug, monkeypatch)
    assert hold + _HEADROOM <= SIGNUP_CREDIT_USD, (
        f"{slug} ({regime}): the smallest real run holds {hold}, leaving under "
        f"{_HEADROOM} of the {SIGNUP_CREDIT_USD} signup credit. Either add it to "
        f"NEEDS_TOPUP and fix the copy that says the credit covers it, or lower "
        f"its base_hard_cap_usd."
    )


def test_needs_topup_is_exactly_the_tools_the_credit_cannot_run(monkeypatch) -> None:
    """Both directions: a NEEDS_TOPUP tool that became affordable fails too."""
    short = set()
    for slug in we.TOOL_SPECS:
        hold, _ = _worst_hold(slug, monkeypatch)
        if hold + _HEADROOM > SIGNUP_CREDIT_USD:
            short.add(slug)
    assert short == NEEDS_TOPUP, (
        f"tools the {SIGNUP_CREDIT_USD} credit cannot run moved: now {sorted(short)}. "
        "Update NEEDS_TOPUP and the copy named in this module's docstring."
    )


def test_hold_at_one_unit_is_the_per_tool_cap(monkeypatch) -> None:
    """Pin the mechanism: at one unit the hold never exceeds the per-tool cap."""
    for slug, spec in we.TOOL_SPECS.items():
        monkeypatch.setattr(we, "_historical_p90_seconds", lambda _s: None)
        params = _smallest_run_params(spec)
        cap = we.compute_hard_cap(slug, params)
        assert cap == min(spec.base_hard_cap_usd, spec.absolute_cap_usd), (
            f"{slug}: compute_hard_cap at one unit is {cap}, not "
            f"base_hard_cap_usd {spec.base_hard_cap_usd}."
        )
        hold, _ = _worst_hold(slug, monkeypatch)
        assert hold <= cap, f"{slug}: hold {hold} exceeds its own cap {cap}"
