"""Which tools a minimum top-up admits at their SMALLEST REAL RUN, and which it does not.

What admits a job is the CUSHIONED HOLD: ``shared/wallet_guard`` reserves
``cushioned_hold_usd`` via ``shared.wallet.reserve_hold``, which refuses on
``balance < hold`` before any SQL runs. On any tool carrying a
``worst_case_gpu_seconds`` floor the hold sits well above the displayed price.
A live-charging run reserves no hold; what admits it is ``live_start_usd``
(``shared/wallet_guard.py`` ``requires_wallet``, the ``0 < balance <
live_start_usd`` refusal), so that is the figure measured for it here.

History: this file used to measure the signup credit. On 2026-10-23 new
accounts stopped receiving a credit and the minimum top-up became the entry
amount, so it now measures ``MIN_TOPUP_USD``. The copy that names the tools a
minimum top-up cannot start (templates/pricing.html,
templates/help/getting_started.html, templates/help/faq.html) names exactly
NEEDS_TOPUP. This file pins that split, so a price, cap or minimum change that
moves a tool across it fails here and sends someone to re-read that copy.

A tool "needs a larger balance" when its smallest-run hold leaves less than
$1.00 of the minimum top-up: a hold EQUAL to it admits a user who has spent
nothing and refuses one who has spent a cent.

At one unit of the scaling parameter ``compute_hard_cap`` clamps its ratio to
1.0, so for the capped tools the hold is ``base_hard_cap_usd``; see
``test_hold_at_one_unit_is_the_per_tool_cap``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared import wallet_estimates as we
from shared.wallet import MIN_TOPUP_USD
from shared.wallet_guard import live_charging_enabled, live_start_usd

# Both p90 regimes are evaluated and the LARGER hold is used: the historical-p90
# branch engages at >= MIN_HISTORICAL_RUNS and can move the hold.
_P90_REGIMES = {"bootstrap": None, "p90_shrunk": 120.0}

_HEADROOM = Decimal("1.00")

# Tools a minimum top-up cannot start. The copy listed in the module
# docstring names exactly this group.
NEEDS_TOPUP = {
    "bindcraft",
    "boltzgen",
    "esmfold2-design",
    "opendde",
}
# proteina and rfantibody left this set on 2026-09-30 when their bootstrap
# estimates were cut to measured runtimes (TOOL_SPECS in
# shared/wallet_estimates.py). The copy ("most binder design runs ... need a
# top-up") was read against this set then. rfdiffusion (#427) left it when it
# went live: $5 covers its live_start_usd. bindcraft went live in #431
# and stays, because its live_start_usd is its $8.00 cap. Leo, 2026-10-16: the
# copy stays as it is.
# pxdesign left this set when Leo set its live start minimum at $2.00 on
# 2026-10-21 (shared/wallet_guard.py::live_start_usd): $5 admits a
# pxdesign run, where its cushioned hold refused one -- that hold is $9.4371
# with no p90 history (cushioned_hold_usd(None, "pxdesign", {"num_designs": 1}),
# live charging off), NOT the $13.00 base cap, which is only its ceiling. The
# threshold that puts pxdesign back is not MIN_TOPUP_USD but MIN_TOPUP_USD less
# _HEADROOM: any start minimum above $4.00, the $13.00 cap among them, and this
# set goes with it. Since 2026-10-23 the pricing, getting started and FAQ copy
# names exactly this set as needing more than the minimum top-up.


def _smallest_run_params(spec: we.ToolSpec) -> dict:
    """One unit of whatever the tool scales on; ``{}`` if it does not scale."""
    return {spec.scaling_param: 1} if spec.scaling_param else {}


def _worst_hold(slug: str, monkeypatch) -> tuple[Decimal, str]:
    """The larger 1-unit hold across both p90 regimes, and which produced it.

    On a live-charging run, its start minimum instead (module docstring).
    """
    spec = we.TOOL_SPECS[slug]
    params = _smallest_run_params(spec)
    if live_charging_enabled(slug, params):
        return live_start_usd(slug, params), "live"
    worst, worst_regime = Decimal("-1"), ""
    for regime, p90 in _P90_REGIMES.items():
        monkeypatch.setattr(we, "_historical_p90_seconds", lambda _s, _v=p90: _v)
        hold = we.cushioned_hold_usd(None, slug, params)
        if hold > worst:
            worst, worst_regime = hold, regime
    return worst, worst_regime


@pytest.mark.parametrize("slug", sorted(set(we.TOOL_SPECS) - NEEDS_TOPUP))
def test_min_topup_covers_smallest_run(slug: str, monkeypatch) -> None:
    """Every tool outside NEEDS_TOPUP runs one unit from a minimum top-up, with $1 spare."""
    hold, regime = _worst_hold(slug, monkeypatch)
    assert hold + _HEADROOM <= MIN_TOPUP_USD, (
        f"{slug} ({regime}): the smallest real run holds {hold}, leaving under "
        f"{_HEADROOM} of the {MIN_TOPUP_USD} minimum top-up. Either add it to "
        f"NEEDS_TOPUP and fix the copy that says a minimum top-up starts it, or lower "
        f"its base_hard_cap_usd."
    )


def test_needs_topup_is_exactly_the_tools_the_minimum_cannot_run(monkeypatch) -> None:
    """Both directions: a NEEDS_TOPUP tool that became affordable fails too."""
    short = set()
    for slug in we.TOOL_SPECS:
        hold, _ = _worst_hold(slug, monkeypatch)
        if hold + _HEADROOM > MIN_TOPUP_USD:
            short.add(slug)
    assert short == NEEDS_TOPUP, (
        f"tools a {MIN_TOPUP_USD} minimum top-up cannot run moved: now {sorted(short)}. "
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
