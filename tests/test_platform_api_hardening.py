"""Tests for the platform-API hardening pass.

Each finding from the validation review has a dedicated test (or
small group of tests) that fails without the fix and passes with it.

    pytest tests/test_platform_api_hardening.py -v
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# FIX #4 — webhook payload carries the real delivery_id
# ---------------------------------------------------------------------------


def test_dispatch_webhook_bakes_delivery_id_into_payload(monkeypatch):
    """The signed body posted to the subscriber MUST contain the same
    delivery_id that's stored in webhook_deliveries — not ``null``."""
    from shared import webhooks as webhooks_mod

    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "test-secret")
    captured = {}

    class _FakeResp:
        status_code = 200
        text = "ok"

    def _fake_post(url, data, headers, timeout, allow_redirects):
        captured["body"] = data
        captured["url"] = url
        return _FakeResp()

    monkeypatch.setattr(webhooks_mod._session, "post", _fake_post)

    enqueued = {}

    def _fake_enqueue(*, delivery_id, campaign_id, target_url, event_type, payload):
        enqueued["delivery_id"] = delivery_id
        enqueued["payload"] = payload
        return delivery_id

    monkeypatch.setattr(webhooks_mod, "_enqueue_delivery", _fake_enqueue)
    monkeypatch.setattr(webhooks_mod, "_update_delivery", MagicMock())
    monkeypatch.setattr(webhooks_mod, "validate_webhook_url_safe", lambda _u: None)

    delivery_id = webhooks_mod.dispatch_webhook(
        campaign_id="c1",
        event_type="experiment.test",
        payload={
            "event_type": "experiment.test",
            "experiment_id": "c1",
            "new_status": "QuoteSent",
        },
        target_url="https://example.com/hook",
    )

    # Wait for the dispatch thread to finish.
    for _ in range(50):
        if "body" in captured:
            break
        time.sleep(0.05)

    assert delivery_id is not None
    # The id baked into the row matches the id we got back.
    assert enqueued["delivery_id"] == delivery_id
    # The id ALSO got baked into the persisted payload.
    assert enqueued["payload"]["delivery_id"] == delivery_id
    # And into the actually-signed bytes.
    import json
    body = json.loads(captured["body"].decode("utf-8"))
    assert body["delivery_id"] == delivery_id
    # No `null` delivery_id snuck through.
    assert body["delivery_id"] is not None


# ---------------------------------------------------------------------------
# FIX #9 — last_used_at throttle + revoked filter
# ---------------------------------------------------------------------------


def test_last_used_throttle_skips_recent_update(monkeypatch):
    """Calling resolve_token twice in quick succession should issue only
    one UPDATE — the second call's last_used_at is recent enough that the
    throttle short-circuits."""
    from shared import api_keys as ak

    fresh_iso = datetime.now(timezone.utc).isoformat()
    update_calls = []

    class _Table:
        def __init__(self, rows):
            self._rows = rows
            self._eqs = []
            self._is_null = []
            self._limit = None
            self._update_payload = None

        def select(self, *_a, **_kw):
            return self

        def eq(self, col, val):
            self._eqs.append((col, val))
            return self

        def is_(self, col, _sentinel):
            self._is_null.append(col)
            return self

        def limit(self, n):
            self._limit = n
            return self

        def update(self, payload):
            self._update_payload = payload
            return self

        def execute(self):
            if self._update_payload is not None:
                update_calls.append(
                    {
                        "payload": self._update_payload,
                        "eqs": list(self._eqs),
                        "is_null": list(self._is_null),
                    }
                )
                return type("R", (), {"data": [{"id": "k1"}]})()
            return type(
                "R",
                (),
                {
                    "data": [
                        {
                            "id": "k1",
                            "user_id": "u1",
                            "role": "member",
                            "prefix": "rk_live_aaaa",
                            "label": None,
                            "created_at": None,
                            "last_used_at": fresh_iso,
                            "revoked_at": None,
                            "hashed_token": ak._hash_token(
                                "rk_live_xxxxxxxxxxxxxxxx"
                            ),
                        }
                    ]
                },
            )()

    class _Client:
        def table(self, _n):
            return _Table([])

    monkeypatch.setattr(ak, "get_service_client", lambda: _Client())

    ctx = ak.resolve_token("rk_live_xxxxxxxxxxxxxxxx")
    assert ctx is not None
    # Throttle should have skipped the update entirely.
    assert update_calls == []


def test_last_used_throttle_updates_when_stale(monkeypatch):
    """When last_used_at is >60s old, the UPDATE fires AND filters on
    revoked_at IS NULL (FIX #9)."""
    from shared import api_keys as ak

    stale_iso = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    captured = {}

    class _Table:
        def __init__(self):
            self._eqs = []
            self._is_null = []
            self._update_payload = None

        def select(self, *_a, **_kw):
            return self

        def eq(self, col, val):
            self._eqs.append((col, val))
            return self

        def is_(self, col, _sentinel):
            self._is_null.append(col)
            return self

        def limit(self, n):
            return self

        def update(self, payload):
            self._update_payload = payload
            return self

        def execute(self):
            if self._update_payload is not None:
                captured["update"] = {
                    "payload": self._update_payload,
                    "eqs": list(self._eqs),
                    "is_null": list(self._is_null),
                }
                self._eqs.clear()
                self._is_null.clear()
                self._update_payload = None
                return type("R", (), {"data": [{"id": "k1"}]})()
            return type(
                "R",
                (),
                {
                    "data": [
                        {
                            "id": "k1",
                            "user_id": "u1",
                            "role": "member",
                            "prefix": "rk_live_aaaa",
                            "label": None,
                            "created_at": None,
                            "last_used_at": stale_iso,
                            "revoked_at": None,
                        }
                    ]
                },
            )()

    class _Client:
        def table(self, _n):
            return _Table()

    monkeypatch.setattr(ak, "get_service_client", lambda: _Client())

    ctx = ak.resolve_token("rk_live_xxxxxxxxxxxxxxxx")
    assert ctx is not None
    # The update happened.
    assert "update" in captured
    # And it filters on revoked_at IS NULL — the security half of the fix.
    assert "revoked_at" in captured["update"]["is_null"]


# ---------------------------------------------------------------------------
# FIX #11 — sequence validator rejects empty chains and over-cap chain count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        ":",
        "::",
        "A:",
        ":B",
        "A::B",
        "ACDE:::FGHI",
    ],
)
def test_sequence_validator_rejects_empty_chains(bad_value):
    from tools.platform_api.routes import _validate_sequences

    result, err = _validate_sequences({"d1": bad_value})
    assert result is None
    assert err is not None
    assert "empty chain" in err.lower() or "chain" in err.lower()


def test_sequence_validator_accepts_multi_chain():
    from tools.platform_api.routes import _validate_sequences

    result, err = _validate_sequences(
        {"d1": "ACDEFGHIKLMNPQRSTVWY:ACDEF"}
    )
    assert err is None
    assert result == {"d1": "ACDEFGHIKLMNPQRSTVWY:ACDEF"}


def test_sequence_validator_caps_chain_count():
    from tools.platform_api.routes import _validate_sequences

    bad = "A:B:C:D:E:F"  # 6 chains, cap is 4
    result, err = _validate_sequences({"d1": bad})
    assert result is None
    assert "chain" in err.lower()


# ---------------------------------------------------------------------------
# FIX #14 — SSRF guard rejects unsafe webhook URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url, expected_msg_fragment",
    [
        # (url, substring that MUST appear in the error message — pins
        # the rejection path so a refactor that accidentally routes IP
        # literals through the DNS fall-through path will fail loudly.)
        ("http://example.com/webhook", "https"),               # cleartext
        ("https://user:pass@example.com/webhook", "credentials"),  # creds
        ("https://127.0.0.1/webhook", "private or special"),   # loopback
        ("https://10.0.0.5/webhook", "private or special"),    # RFC1918
        ("https://192.168.1.1/webhook", "private or special"), # RFC1918
        ("https://169.254.169.254/latest/meta-data/", "private or special"),  # AWS
        ("https://100.64.1.2/webhook", "private or special"),  # CGNAT
        ("https://[::1]/webhook", "private or special"),       # IPv6 loop
        ("https://example.com:8080/webhook", "443"),           # bad port
    ],
)
def test_webhook_url_ssrf_guard_blocks(bad_url, expected_msg_fragment):
    from shared.webhooks import (
        UnsafeWebhookURLError,
        validate_webhook_url_safe,
    )

    with pytest.raises(UnsafeWebhookURLError, match=expected_msg_fragment):
        validate_webhook_url_safe(bad_url)


def test_webhook_url_ssrf_guard_public_ip_with_bad_port():
    """A publicly routable IP literal with a non-443 port must still be
    rejected by the PORT check, not the IP check (regression guard for
    re-review finding #1: literal-vs-DNS code path completeness)."""
    from shared.webhooks import (
        UnsafeWebhookURLError,
        validate_webhook_url_safe,
    )

    with pytest.raises(UnsafeWebhookURLError, match="443"):
        validate_webhook_url_safe("https://1.1.1.1:8080/hook")


def test_webhook_url_ssrf_guard_allows_public():
    """A standard https URL on a public host passes. We use a stable
    public host that the test runner can resolve."""
    from shared.webhooks import validate_webhook_url_safe

    # No exception → pass. (If example.com resolution fails the test will
    # error not fail, which is acceptable for a network-dependent assert.)
    validate_webhook_url_safe("https://example.com/hook")
    validate_webhook_url_safe("https://example.com:443/hook")


def test_webhook_url_validator_in_routes_uses_guard():
    """The route-level _validate_webhook_url must delegate to the shared
    guard — not its own duplicate logic."""
    from tools.platform_api.routes import _validate_webhook_url

    val, err = _validate_webhook_url("http://example.com/hook")
    assert val is None
    assert err is not None
    assert "https" in err.lower() or "cleartext" in err.lower()


# ---------------------------------------------------------------------------
# FIX #6 — TransitionResult shape returned by transition_api_status
# ---------------------------------------------------------------------------


def test_transition_result_dataclass_moved_and_noop():
    """TransitionResult should distinguish moved vs no-op so the route
    handler can decide whether to fire a webhook."""
    from shared.campaigns import TransitionResult

    moved = TransitionResult(moved=True, prev_status="Draft", campaign=None)
    noop = TransitionResult(moved=False, prev_status="QuoteSent", campaign=None)
    assert moved.moved is True
    assert noop.moved is False
    assert noop.prev_status == "QuoteSent"


def test_transition_api_status_calls_rpc(monkeypatch):
    from shared import campaigns as camp

    captured = {}

    class _RpcCall:
        def __init__(self, name, params):
            captured["name"] = name
            captured["params"] = params

        def execute(self):
            return type(
                "R",
                (),
                {
                    "data": [
                        {
                            "prev_status": "Draft",
                            "new_status": "WaitingForConfirmation",
                            "moved": True,
                            "campaign": {
                                "id": "c1",
                                "user_id": "u1",
                                "source_job_id": None,
                                "candidate_indices": [0],
                                "target_name": "T",
                                "target_context": "",
                                "assay_type": "yeast_display",
                                "budget_band": "custom",
                                "status": "WaitingForConfirmation",
                                "submission_source": "api",
                                "results_status": "none",
                                "name": "n",
                            },
                        }
                    ]
                },
            )()

    class _Client:
        def rpc(self, name, params):
            return _RpcCall(name, params)

    monkeypatch.setattr(camp, "get_service_client", lambda: _Client())

    result = camp.transition_api_status(
        "c1", new_status="WaitingForConfirmation", by="system"
    )
    assert captured["name"] == "transition_lab_campaign_api"
    assert captured["params"]["p_new_status"] == "WaitingForConfirmation"
    assert result.moved is True
    assert result.prev_status == "Draft"
    assert result.campaign is not None
    assert result.campaign.status == "WaitingForConfirmation"


# ---------------------------------------------------------------------------
# FIX #5 — Idempotency-Key race graceful replay
# ---------------------------------------------------------------------------


def test_create_api_campaign_raises_idempotent_replay_on_unique_violation(
    monkeypatch,
):
    """When two simultaneous POSTs with the same Idempotency-Key race,
    the second one's INSERT hits the partial unique index. The catch
    path must re-fetch and raise IdempotentReplay rather than 500."""
    from shared import campaigns as camp

    seq = iter([None, _fake_campaign_row()])

    def _fake_find(*, user_id, idempotency_key):
        row = next(seq)
        return camp.Campaign.from_row(row) if row else None

    monkeypatch.setattr(camp, "find_by_idempotency_key", _fake_find)

    class _Table:
        def insert(self, _payload):
            return self

        def execute(self):
            raise RuntimeError(
                "duplicate key value violates unique constraint "
                "\"lab_campaigns_user_idempotency_idx\" SQLSTATE 23505"
            )

    class _Client:
        def table(self, _n):
            return _Table()

    monkeypatch.setattr(camp, "get_service_client", lambda: _Client())

    with pytest.raises(camp.IdempotentReplay) as exc_info:
        camp.create_api_campaign(
            user_id="u1",
            name="x",
            assay_type="yeast_display",
            target_name="T",
            target_context="",
            sequences={"d1": "ACDE"},
            library_design={},
            webhook_url=None,
            idempotency_key="my-key",
        )
    assert exc_info.value.campaign.id == "c1"


def _fake_campaign_row():
    return {
        "id": "c1",
        "user_id": "u1",
        "source_job_id": None,
        "candidate_indices": [0],
        "target_name": "T",
        "target_context": "",
        "assay_type": "yeast_display",
        "budget_band": "custom",
        "status": "Draft",
        "submission_source": "api",
        "results_status": "none",
        "sequences": {"d1": "ACDE"},
    }


# ---------------------------------------------------------------------------
# FIX #10 — webhook concurrency semaphore caps in-flight dispatches
# ---------------------------------------------------------------------------


def test_webhook_dispatch_semaphore_backpressures(monkeypatch):
    """When the in-flight cap is saturated, additional dispatch_webhook
    calls must NOT spawn unbounded threads. They write a backpressure
    note onto webhook_deliveries and exit cleanly."""
    from shared import webhooks as wh

    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "x")
    monkeypatch.setattr(wh, "validate_webhook_url_safe", lambda _u: None)
    monkeypatch.setattr(
        wh, "_enqueue_delivery", lambda **kw: kw["delivery_id"]
    )

    update_calls = []

    def _capture_update(**kw):
        update_calls.append(kw)

    monkeypatch.setattr(wh, "_update_delivery", _capture_update)

    # Drain the semaphore so the next acquire fails.
    drained = []
    for _ in range(wh._MAX_INFLIGHT):
        ok = wh._dispatch_semaphore.acquire(blocking=False)
        drained.append(ok)
    assert all(drained)

    try:
        # In this state, _bounded_dispatch should hit the backpressure
        # branch and NOT call _dispatch_loop.
        called_loop = {"yes": False}

        def _fake_loop(**_kw):
            called_loop["yes"] = True

        monkeypatch.setattr(wh, "_dispatch_loop", _fake_loop)

        wh._bounded_dispatch(
            delivery_id="d1",
            target_url="https://example.com",
            payload={"x": 1},
        )
        assert called_loop["yes"] is False
        # And it stamped the row with a backpressure note.
        assert any(
            "backpressure" in (c.get("last_error") or "")
            for c in update_calls
        )
    finally:
        # Release the drained tokens so other tests aren't poisoned.
        for ok in drained:
            wh._dispatch_semaphore.release()
