"""Unit tests for :mod:`shared.wallet_funnel`.

Covers each tier threshold and the deduplication path via the
``funnel_alerts`` table. The Supabase client is faked so the tests
run offline.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from shared import wallet_funnel
from shared.wallet_funnel import (
    FUNNEL_TRIGGERS,
    _highest_eligible_tier,
    _is_step_up,
    _maybe_trigger_funnel_alerts,
)


# ---------------------------------------------------------------------------
# Fake Supabase client tailored to the two tables the funnel uses.
# ---------------------------------------------------------------------------


class _FakeTable:
    def __init__(self, store: dict[str, list[dict]], name: str) -> None:
        self._store = store
        self._name = name
        self._rows = list(store.get(name, []))
        self._filters: list[tuple[str, str, Any]] = []
        self._order: tuple[str, bool] = ("created_at", False)
        self._limit: int | None = None
        self._single: bool = False
        self._pending_insert: dict | None = None

    def select(self, *_: Any, **__: Any) -> "_FakeTable":
        return self

    def eq(self, col: str, val: Any) -> "_FakeTable":
        self._filters.append((col, "=", val))
        return self

    def in_(self, col: str, vals: list[Any]) -> "_FakeTable":
        self._filters.append((col, "in", list(vals)))
        return self

    def gte(self, col: str, val: Any) -> "_FakeTable":
        self._filters.append((col, ">=", val))
        return self

    def order(self, col: str, desc: bool = False) -> "_FakeTable":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "_FakeTable":
        self._limit = n
        return self

    def maybe_single(self) -> "_FakeTable":
        self._single = True
        return self

    def insert(self, payload: dict) -> "_FakeTable":
        self._pending_insert = payload
        return self

    def execute(self) -> Any:
        if self._pending_insert is not None:
            row = dict(self._pending_insert)
            row.setdefault("id", f"row-{len(self._rows) + 1}")
            row.setdefault("created_at", "2026-05-13T00:00:00+00:00")
            self._store.setdefault(self._name, []).append(row)
            return type("R", (), {"data": [row]})()
        rows = list(self._rows)
        for col, op, val in self._filters:
            if op == "=":
                rows = [r for r in rows if r.get(col) == val]
            elif op == "in":
                rows = [r for r in rows if r.get(col) in val]
            elif op == ">=":
                rows = [r for r in rows if str(r.get(col, "")) >= str(val)]
        col, desc = self._order
        rows.sort(key=lambda r: str(r.get(col, "")), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        if self._single:
            return type("R", (), {"data": rows[0] if rows else None})()
        return type("R", (), {"data": rows})()


class _FakeClient:
    def __init__(self) -> None:
        self.store: dict[str, list[dict]] = {
            "wallet_transactions": [],
            "funnel_alerts": [],
        }

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(self.store, name)


@pytest.fixture
def client():
    return _FakeClient()


@pytest.fixture(autouse=True)
def patch_client(client):
    with patch(
        "shared.wallet_funnel.get_service_client", return_value=client
    ):
        yield client


@pytest.fixture
def email_log():
    """Capture all funnel email calls without actually emitting them."""
    log: list[tuple[str, dict]] = []

    def make_stub(name: str):
        def stub(*args, **kwargs):
            payload = dict(kwargs)
            if args:
                payload["_args"] = args
            log.append((name, payload))
            return True
        return stub

    with patch.object(
        wallet_funnel, "_resolve_handler",
        side_effect=lambda n: make_stub(n),
    ):
        yield log


USER = "00000000-0000-0000-0000-0000000000aa"


def _add_charge(client: _FakeClient, amount_usd: float, created_at: str = "2026-05-12T00:00:00+00:00") -> None:
    client.store["wallet_transactions"].append(
        {
            "user_id": USER,
            "kind": "charge",
            "amount_usd": float(amount_usd),
            "created_at": created_at,
        }
    )


# ---------------------------------------------------------------------------
# Eligibility helpers
# ---------------------------------------------------------------------------


def test_no_eligible_tier_below_threshold():
    assert _highest_eligible_tier(Decimal("500")) is None


def test_highest_eligible_tier_picks_top_match():
    pick = _highest_eligible_tier(Decimal("7500"))
    assert pick is not None
    assert pick.tier == "sales_qualified"


def test_highest_eligible_tier_high_value():
    pick = _highest_eligible_tier(Decimal("12000"))
    assert pick is not None
    assert pick.tier == "high_value"


def test_step_up_first_alert_always_emits():
    assert _is_step_up(None, "active_project") is True


def test_step_up_blocks_same_tier():
    assert _is_step_up("sales_qualified", "sales_qualified") is False


def test_step_up_blocks_lower_tier():
    assert _is_step_up("sales_qualified", "active_project") is False


def test_step_up_allows_higher_tier():
    assert _is_step_up("active_project", "high_value") is True


# ---------------------------------------------------------------------------
# End-to-end firing behaviour
# ---------------------------------------------------------------------------


def test_no_alert_when_spend_below_active_threshold(client, email_log):
    _add_charge(client, 100)
    tier = _maybe_trigger_funnel_alerts(USER)
    assert tier is None
    assert email_log == []
    assert client.store["funnel_alerts"] == []


def test_active_project_threshold_fires(client, email_log):
    _add_charge(client, 1200)
    tier = _maybe_trigger_funnel_alerts(USER)
    assert tier == "active_project"
    assert email_log[0][0] == "send_pilot_intro_email"
    assert client.store["funnel_alerts"][0]["tier"] == "active_project"


def test_sales_qualified_threshold_fires(client, email_log):
    _add_charge(client, 6000)
    tier = _maybe_trigger_funnel_alerts(USER)
    assert tier == "sales_qualified"
    assert email_log[0][0] == "alert_sales_slack"


def test_high_value_threshold_fires(client, email_log):
    _add_charge(client, 12000)
    tier = _maybe_trigger_funnel_alerts(USER)
    assert tier == "high_value"
    assert email_log[0][0] == "alert_sales_slack_high"


def test_dedup_blocks_repeat_alert_at_same_tier(client, email_log):
    _add_charge(client, 1500)
    first = _maybe_trigger_funnel_alerts(USER)
    second = _maybe_trigger_funnel_alerts(USER)
    assert first == "active_project"
    assert second is None
    assert len(email_log) == 1
    assert len(client.store["funnel_alerts"]) == 1


def test_step_up_from_active_to_sales(client, email_log):
    _add_charge(client, 1500)
    first = _maybe_trigger_funnel_alerts(USER)
    assert first == "active_project"
    _add_charge(client, 5500)  # now total spend is 7000
    second = _maybe_trigger_funnel_alerts(USER)
    assert second == "sales_qualified"
    assert [e[0] for e in email_log] == [
        "send_pilot_intro_email",
        "alert_sales_slack",
    ]


def test_step_up_skips_intermediate_tier_when_user_spends_big(
    client, email_log
):
    """A first-time user crossing all three thresholds at once fires the top."""
    _add_charge(client, 15000)
    tier = _maybe_trigger_funnel_alerts(USER)
    assert tier == "high_value"
    assert email_log[0][0] == "alert_sales_slack_high"


def test_handler_missing_does_not_crash(client):
    """When :func:`_resolve_handler` returns None we log and skip."""
    _add_charge(client, 1500)
    with patch.object(wallet_funnel, "_resolve_handler", return_value=None):
        tier = _maybe_trigger_funnel_alerts(USER)
    assert tier is None
    assert client.store["funnel_alerts"] == []


def test_trigger_definitions_match_spec():
    tiers = {t.tier for t in FUNNEL_TRIGGERS}
    assert tiers == {"active_project", "sales_qualified", "high_value"}
    thresholds = sorted([t.threshold_usd for t in FUNNEL_TRIGGERS])
    assert thresholds == [Decimal("1000"), Decimal("5000"), Decimal("10000")]
