"""A paid run is charged at least ``MIN_CHARGE_USD``; a refund stays free.

Leo's ProteinMPNN pilot email read "Estimated $0.02, charged $0.00 (12
GPU-sec on A10G)": the wallet took $0.0042 and the email's two-decimal line
rounded it away. The floor lives in ``shared.wallet_estimates.apply_min_charge``,
called by ``compute_charge_usd`` (settle and the email) and by
``estimated_cost_for_tool`` (the quote, and through it the hold).
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import pytest

from shared import wallet
from shared import wallet_estimates as we
from shared.wallet import compute_charge_usd, release_hold, reserve_hold, settle_hold
from tests.test_wallet import _FakeClient, _seed_wallet, _Store

_USER = "00000000-0000-0000-0000-0000000000c5"


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: None)
    store = _Store()
    _seed_wallet(store, _USER, balance=Decimal("5.00"))
    client = _FakeClient(store)
    with patch("shared.wallet.get_service_client", return_value=client), \
            patch.object(wallet, "_send_email_safe", lambda *a, **kw: None):
        yield store


def _rows(store, kind):
    return [t for t in store.tables["wallet_transactions"] if t["kind"] == kind]


def test_a_tiny_mpnn_run_settles_at_the_minimum_and_the_email_agrees(store):
    assert we.MIN_CHARGE_USD == Decimal("0.05")
    hold = we.cushioned_hold_usd(None, "mpnn", {})
    assert we.estimated_cost_for_tool(None, "mpnn", {}) == Decimal("0.0500")
    hold_id = reserve_hold(_USER, "mpnn", 1, hold, {})
    settle_hold(hold_id, gpu_seconds=12, gpu_class="A10G", params={})
    [charge] = _rows(store, "charge")
    assert Decimal(str(charge["amount_usd"])) == Decimal("0.05")
    # The floor sits under the hold, so it never creates variance.
    assert Decimal("0.05") <= hold

    from shared.email import _cost_breakdown_line

    job = mock.Mock(
        tool="mpnn",
        inputs={"_wallet": {"hold_tx_id": hold_id, "estimate_usd": "0.05"}},
        result={"gpu_sku": "A10G"},
        gpu_seconds_used=12.0,
    )
    line = _cost_breakdown_line(job, tone="succeeded")
    assert line == "Estimated $0.05, charged $0.05 (12 GPU-sec on A10G).", line


def test_zero_seconds_and_refunds_stay_free(store):
    assert compute_charge_usd(0, "A10G") == Decimal("0")
    hold = we.cushioned_hold_usd(None, "mpnn", {})
    hold_id = reserve_hold(_USER, "mpnn", 1, hold, {})
    release_hold(hold_id, reason="tool_error")
    assert not _rows(store, "charge")
    assert Decimal(str(wallet._wallet(_USER)["balance_usd"])) == Decimal("5.00")


def test_a_free_preset_still_quotes_zero():
    assert we.estimated_cost_for_tool(None, "proteina", {"preset": "validate"}) == 0


def test_the_floor_sits_under_every_tools_hold_and_cap(monkeypatch):
    monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: None)
    for slug in we.TOOL_SPECS:
        cap = we.compute_hard_cap(slug, {})
        assert cap >= we.MIN_CHARGE_USD, slug
        hold = we.cushioned_hold_usd(None, slug, {})
        assert hold == 0 or hold >= we.MIN_CHARGE_USD, (slug, hold)
