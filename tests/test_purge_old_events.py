"""PII retention sweeper tests (cso L5).

Covers the dry-run count path, the batched live delete, the 30-day floor
guardrail, and the no-client degradation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from cron import purge_old_events as mod


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class _FakeTable:
    def __init__(self, store: dict, name: str):
        self.store = store
        self.name = name
        self._op = "select"
        self._count = False
        self._lt = None
        self._limit = None
        self._ids = None

    def select(self, *_cols, count=None):
        self._op, self._count = "select", (count == "exact")
        return self

    def delete(self):
        self._op = "delete"
        return self

    def lt(self, col, val):
        self._lt = (col, val)
        return self

    def in_(self, _col, vals):
        self._ids = set(vals)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = self.store[self.name]
        if self._op == "delete":
            self.store[self.name] = [r for r in rows if r["id"] not in self._ids]
            return SimpleNamespace(data=[], count=None)
        matched = [
            r for r in rows
            if self._lt is None or r[self._lt[0]] < self._lt[1]
        ]
        total = len(matched)
        if self._limit:
            matched = matched[: self._limit]
        return SimpleNamespace(
            data=[{"id": r["id"]} for r in matched], count=total
        )


class _FakeClient:
    def __init__(self, store: dict):
        self.store = store

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(self.store, name)


def _store():
    return {
        "user_events": [
            {"id": f"ue-old-{i}", "created_at": _iso(400)} for i in range(3)
        ] + [{"id": "ue-recent", "created_at": _iso(10)}],
        "signup_rejections": [
            {"id": "sr-old", "created_at": _iso(500)},
            {"id": "sr-recent", "created_at": _iso(5)},
        ],
    }


def test_dry_run_counts_without_deleting():
    store = _store()
    # get_service_client is imported inside the function from shared.credits.
    with patch("shared.credits.get_service_client", lambda: _FakeClient(store)):
        summary = mod.purge_old_events(retention_days=365, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["user_events"] == 3       # the 3 old rows counted
    assert summary["signup_rejections"] == 1
    # Nothing actually deleted.
    assert len(store["user_events"]) == 4
    assert len(store["signup_rejections"]) == 2


def test_live_delete_removes_only_old_rows():
    store = _store()
    with patch("shared.credits.get_service_client", lambda: _FakeClient(store)):
        summary = mod.purge_old_events(retention_days=365, dry_run=False)
    assert summary["user_events"] == 3
    assert summary["signup_rejections"] == 1
    # Recent rows survive.
    assert [r["id"] for r in store["user_events"]] == ["ue-recent"]
    assert [r["id"] for r in store["signup_rejections"]] == ["sr-recent"]


def test_retention_floored_at_30_days():
    # A tiny window would otherwise wipe near-current data; it must floor to 30.
    store = {
        "user_events": [{"id": "ue-15d", "created_at": _iso(15)}],
        "signup_rejections": [],
    }
    with patch("shared.credits.get_service_client", lambda: _FakeClient(store)):
        summary = mod.purge_old_events(retention_days=1, dry_run=False)
    assert summary["retention_days"] == 30
    # 15-day-old row is inside the 30-day window -> NOT deleted.
    assert len(store["user_events"]) == 1


def test_no_service_client_degrades():
    with patch("shared.credits.get_service_client", lambda: None):
        summary = mod.purge_old_events(retention_days=365)
    assert "no service client" in summary["errors"]
