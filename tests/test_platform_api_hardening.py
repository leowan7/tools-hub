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
        # branch and NOT call _dispatch_once.
        called_loop = {"yes": False}

        def _fake_loop(**_kw):
            called_loop["yes"] = True

        monkeypatch.setattr(wh, "_dispatch_once", _fake_loop)

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


# ===========================================================================
# Fresh-review (2nd adversarial pass) fixes
# ===========================================================================


# ---------------------------------------------------------------------------
# FIX HI-02 — re-validate webhook URL on every retry iteration
# ---------------------------------------------------------------------------


def test_dispatch_loop_revalidates_url_each_retry(monkeypatch):
    """A DNS-rebind that flips the host to a private IP between attempts
    must block the retry POST and stamp the delivery row.

    CR-02 (fresh-review) refactored the in-thread retry loop into a
    single-attempt-and-reschedule model: each call to _dispatch_once
    runs one POST, then either succeeds, schedules a future
    next_retry_at, or stamps delivered_at when out of retries. So this
    test now drives TWO separate _dispatch_once calls — the second one
    simulates the cron sweep picking up the rescheduled row, with DNS
    having rebound to a private IP in the gap.
    """
    from shared import webhooks as wh

    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "test-secret")

    update_calls: list[dict] = []
    monkeypatch.setattr(
        wh,
        "_update_delivery",
        lambda **kwargs: update_calls.append(kwargs),
    )

    # First attempt validation passes; the cron-retry attempt sees the
    # rebind and raises.
    validate_calls = {"n": 0}

    def _fake_validate(url):
        validate_calls["n"] += 1
        if validate_calls["n"] >= 2:
            raise wh.UnsafeWebhookURLError(
                "host rebind: resolved to 10.0.0.5"
            )

    monkeypatch.setattr(wh, "validate_webhook_url_safe", _fake_validate)

    posted = {"n": 0}

    def _fake_post(*a, **kw):
        posted["n"] += 1

        class _R:
            status_code = 500  # force retry
            text = "boom"

        return _R()

    monkeypatch.setattr(wh._session, "post", _fake_post)

    payload = {
        "event_type": "test",
        "experiment_id": "c1",
        "new_status": "QuoteSent",
    }

    # First attempt: POST happens, fails, row gets next_retry_at.
    wh._dispatch_once(
        delivery_id="d-rebind",
        target_url="https://attacker.example/hook",
        payload=payload,
        prior_attempts=0,
    )
    # Cron sweep would pick the row back up here. Second attempt: validation
    # raises because DNS now resolves to a private IP.
    wh._dispatch_once(
        delivery_id="d-rebind",
        target_url="https://attacker.example/hook",
        payload=payload,
        prior_attempts=1,
    )

    # Exactly ONE POST happened (the first attempt); the retry's
    # validation rejected before _post_once.
    assert posted["n"] == 1
    assert validate_calls["n"] == 2
    # Row was stamped with the rebind-detected error.
    stamps = [u for u in update_calls if "rebind" in (u.get("last_error") or "")]
    assert stamps, "delivery row should be stamped with rebind error"


# ---------------------------------------------------------------------------
# FIX HI-04 — _PREFIX_DISPLAY_LEN drops to 8 (no plaintext bits stored)
# ---------------------------------------------------------------------------


def test_api_key_prefix_carries_no_plaintext_randomness():
    """The persisted ``api_keys.prefix`` column is the literal scheme
    only; never any random bits from the plaintext token."""
    from shared import api_keys

    # The constant itself drives the slice in mint_token.
    assert api_keys._PREFIX_DISPLAY_LEN == len(api_keys._TOKEN_PREFIX)

    # And the slice produces exactly "rk_live_" — not "rk_live_abcd".
    plaintext = api_keys._new_plaintext()
    derived_prefix = plaintext[: api_keys._PREFIX_DISPLAY_LEN]
    assert derived_prefix == "rk_live_"
    assert len(derived_prefix) == 8


# ---------------------------------------------------------------------------
# FIX HI-05 — DNS lookup has a hard timeout
# ---------------------------------------------------------------------------


def test_webhook_url_dns_lookup_has_timeout(monkeypatch):
    """``validate_webhook_url_safe`` must not stall on a slow resolver.

    We replace ``socket.getaddrinfo`` with one that observes the current
    default socket timeout and asserts a sub-10s cap.
    """
    import socket as _socket

    from shared import webhooks as wh

    seen_timeout = {}

    def _fake_getaddrinfo(host, port, **_kw):
        seen_timeout["t"] = _socket.getdefaulttimeout()
        return [(_socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(wh.socket, "getaddrinfo", _fake_getaddrinfo)

    wh.validate_webhook_url_safe("https://example.com/hook")
    assert seen_timeout["t"] is not None
    assert seen_timeout["t"] <= 10.0  # cap should be small (default 2.0)


def test_webhook_url_dns_lookup_timeout_rejects(monkeypatch):
    """When the resolver times out, the URL is REJECTED, not accepted."""
    import socket as _socket

    from shared import webhooks as wh

    def _slow_getaddrinfo(host, port, **_kw):
        raise _socket.timeout("simulated slow DNS")

    monkeypatch.setattr(wh.socket, "getaddrinfo", _slow_getaddrinfo)

    with pytest.raises(wh.UnsafeWebhookURLError, match="DNS lookup timed out"):
        wh.validate_webhook_url_safe("https://example.com/hook")


# ---------------------------------------------------------------------------
# FIX HI-01 verification — IPv4-mapped IPv6 literals are rejected
# (false positive on the reviewer's premise of Python 3.11; we run 3.13)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:10.0.0.5]/",
        "https://[::ffff:169.254.169.254]/",
        "https://[::ffff:100.64.1.1]/",
        "https://[::1]/",
        "https://[::]/",
    ],
)
def test_ipv4_mapped_ipv6_literal_rejected(bad):
    """Python 3.12+ unwraps IPv4-mapped IPv6 in is_private/is_loopback.
    Production runs 3.13 (see runtime.txt). Pin the rejection so a future
    runtime downgrade can't silently re-open the bypass."""
    from shared import webhooks as wh

    with pytest.raises(
        wh.UnsafeWebhookURLError,
        match="private or special-use IP",
    ):
        wh.validate_webhook_url_safe(bad)


# ---------------------------------------------------------------------------
# FIX ME-01 — _is_unique_violation correctly detects postgrest.APIError
# ---------------------------------------------------------------------------


def test_is_unique_violation_detects_real_postgrest_api_error():
    """Production raises postgrest.exceptions.APIError, NOT RuntimeError.
    The detection function must match the real shape so the idempotency
    race catch path actually fires."""
    from shared import campaigns

    if campaigns._PostgrestAPIError is None:
        pytest.skip("postgrest not installed in test env")

    err = campaigns._PostgrestAPIError(
        {
            "code": "23505",
            "message": "duplicate key value violates unique constraint",
            "details": "Key (user_id, idempotency_key)=(u, k) already exists.",
            "hint": None,
        }
    )
    assert campaigns._is_unique_violation(err) is True


def test_is_unique_violation_rejects_non_23505_postgrest_error():
    """The catch must NOT trigger on unrelated postgrest errors."""
    from shared import campaigns

    if campaigns._PostgrestAPIError is None:
        pytest.skip("postgrest not installed in test env")

    err = campaigns._PostgrestAPIError(
        {
            "code": "42501",  # insufficient_privilege
            "message": "permission denied for table lab_campaigns",
            "details": None,
            "hint": None,
        }
    )
    assert campaigns._is_unique_violation(err) is False


# ---------------------------------------------------------------------------
# FIX ME-02 — _post_once converts unexpected exceptions to failure return
# ---------------------------------------------------------------------------


def test_post_once_swallows_unexpected_exception(monkeypatch):
    """A non-RequestException from the response decode (or anywhere)
    must convert to a failure return, not bubble up and kill the
    dispatch thread."""
    from shared import webhooks as wh

    class _ExplodingResp:
        status_code = 400

        @property
        def text(self):
            raise LookupError("unknown encoding: 'invalid-charset'")

    def _fake_post(*a, **kw):
        return _ExplodingResp()

    monkeypatch.setattr(wh._session, "post", _fake_post)

    ok, message = wh._post_once("https://example.com", b"{}", "sig")
    assert ok is False
    # The decode failure path should have engaged.
    assert "http 400" in message
    assert "undecodable" in message


def test_post_once_swallows_unexpected_post_exception(monkeypatch):
    """A non-requests exception from .post() must also convert to a
    failure return."""
    from shared import webhooks as wh

    def _fake_post(*a, **kw):
        raise MemoryError("simulated allocation failure")

    monkeypatch.setattr(wh._session, "post", _fake_post)

    ok, message = wh._post_once("https://example.com", b"{}", "sig")
    assert ok is False
    assert "unexpected error" in message
    assert "MemoryError" in message


# ---------------------------------------------------------------------------
# FIX ME-06 — throttle env var rejects zero/negative
# ---------------------------------------------------------------------------


def test_throttle_env_rejects_zero(monkeypatch):
    """``API_KEY_LAST_USED_THROTTLE_SECONDS=0`` would disable the
    throttle. Validator must clamp to 60s and log a warning."""
    import importlib

    from shared import api_keys

    monkeypatch.setenv("API_KEY_LAST_USED_THROTTLE_SECONDS", "0")
    # Re-derive via the helper directly (cheaper than re-import).
    assert api_keys._read_throttle_seconds() == 60


def test_throttle_env_rejects_negative(monkeypatch):
    from shared import api_keys

    monkeypatch.setenv("API_KEY_LAST_USED_THROTTLE_SECONDS", "-5")
    assert api_keys._read_throttle_seconds() == 60


def test_throttle_env_rejects_non_integer(monkeypatch):
    from shared import api_keys

    monkeypatch.setenv("API_KEY_LAST_USED_THROTTLE_SECONDS", "soon")
    assert api_keys._read_throttle_seconds() == 60


def test_throttle_env_accepts_valid(monkeypatch):
    from shared import api_keys

    monkeypatch.setenv("API_KEY_LAST_USED_THROTTLE_SECONDS", "90")
    assert api_keys._read_throttle_seconds() == 90


# ---------------------------------------------------------------------------
# FIX LO-08 — dispatch_webhook owns the delivery_id; caller doesn't pre-stuff None
# ---------------------------------------------------------------------------


def test_fire_webhook_caller_does_not_pass_delivery_id_sentinel(monkeypatch):
    """The route-level _fire_webhook must NOT pre-populate delivery_id=None
    in the payload dict — it now lets dispatch_webhook mint and graft
    the id. Sentinel field would survive into the persisted payload row
    if a future refactor removed the grafting step."""
    from tools.platform_api import routes as routes_mod

    captured = {}

    def _fake_dispatch(*, campaign_id, event_type, target_url, payload):
        captured.update(payload)
        return "dispatched"

    monkeypatch.setattr(routes_mod, "dispatch_webhook", _fake_dispatch)

    # Build a minimal Campaign with required fields.
    from shared.campaigns import Campaign

    camp = Campaign(
        id="c-1",
        user_id="u-1",
        source_job_id=None,
        candidate_indices=[0],
        target_name="t",
        target_context="",
        assay_type="yeast_display",
        affinity_goal_kd_nm=None,
        timeline_weeks=None,
        budget_band="custom",
        status="WaitingForConfirmation",
        ranomics_contact=None,
        notes_internal=None,
        created_at=None,
        reviewed_at=None,
        webhook_url="https://example.com/hook",
        results_status="none",
        last_transition_at="2026-06-04T00:00:00Z",
    )

    routes_mod._fire_webhook(
        camp, event_type="experiment.waiting_for_confirmation", prev_status="Draft"
    )

    assert captured  # dispatch was called
    assert "delivery_id" not in captured, (
        "caller must not pre-populate delivery_id; dispatch_webhook "
        "is the only minter"
    )


# ===========================================================================
# CR-02 — Single-attempt dispatch + cron sweep
# ===========================================================================


def test_dispatch_once_single_attempt_success_stamps_delivered(monkeypatch):
    """A successful attempt stamps delivered_at and never sleeps in-thread."""
    from shared import webhooks as wh

    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "x")
    monkeypatch.setattr(wh, "validate_webhook_url_safe", lambda _u: None)

    update_calls: list[dict] = []
    monkeypatch.setattr(
        wh, "_update_delivery", lambda **kw: update_calls.append(kw)
    )

    sleep_calls = {"n": 0}
    monkeypatch.setattr(
        wh.time, "sleep", lambda _s: sleep_calls.__setitem__("n", sleep_calls["n"] + 1)
    )

    class _OkResp:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(wh._session, "post", lambda *a, **kw: _OkResp())

    wh._dispatch_once(
        delivery_id="d-ok",
        target_url="https://example.com/hook",
        payload={"event_type": "t", "experiment_id": "c", "new_status": "Done"},
        prior_attempts=0,
    )

    # delivered_at was stamped; no in-thread sleep happened.
    assert any(u.get("delivered_at") for u in update_calls)
    assert sleep_calls["n"] == 0


def test_dispatch_once_failure_schedules_next_retry_does_not_sleep(monkeypatch):
    """A failed attempt writes next_retry_at and returns. The cron sweep
    is the new retry driver, not in-thread sleep."""
    from shared import webhooks as wh

    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "x")
    monkeypatch.setattr(wh, "validate_webhook_url_safe", lambda _u: None)

    update_calls: list[dict] = []
    monkeypatch.setattr(
        wh, "_update_delivery", lambda **kw: update_calls.append(kw)
    )

    sleep_calls = {"n": 0}
    monkeypatch.setattr(
        wh.time, "sleep", lambda _s: sleep_calls.__setitem__("n", sleep_calls["n"] + 1)
    )

    class _FailResp:
        status_code = 500
        text = "boom"

    monkeypatch.setattr(wh._session, "post", lambda *a, **kw: _FailResp())

    wh._dispatch_once(
        delivery_id="d-fail",
        target_url="https://example.com/hook",
        payload={"event_type": "t", "experiment_id": "c", "new_status": "QuoteSent"},
        prior_attempts=0,
    )

    # Row was updated with next_retry_at but NOT delivered_at.
    assert any(u.get("next_retry_at") for u in update_calls)
    assert not any(u.get("delivered_at") for u in update_calls)
    # And we never slept in-thread (the cron drives retries now).
    assert sleep_calls["n"] == 0


def test_dispatch_once_past_max_attempts_stamps_delivered(monkeypatch):
    """When attempts has burned through the backoff schedule, stamp
    delivered_at to drop the row from the queue."""
    from shared import webhooks as wh

    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "x")
    monkeypatch.setattr(wh, "validate_webhook_url_safe", lambda _u: None)

    update_calls: list[dict] = []
    monkeypatch.setattr(
        wh, "_update_delivery", lambda **kw: update_calls.append(kw)
    )

    class _FailResp:
        status_code = 500
        text = "still failing"

    monkeypatch.setattr(wh._session, "post", lambda *a, **kw: _FailResp())

    # prior_attempts == len(_BACKOFF_SECONDS) means we've burned through
    # the schedule. The next failure should stamp delivered_at.
    wh._dispatch_once(
        delivery_id="d-end",
        target_url="https://example.com/hook",
        payload={"event_type": "t", "experiment_id": "c", "new_status": "Done"},
        prior_attempts=len(wh._BACKOFF_SECONDS),
    )

    assert any(u.get("delivered_at") for u in update_calls)


def test_sweep_due_deliveries_dispatches_each_row(monkeypatch):
    """The sweep calls the claim RPC and dispatches each returned row."""
    from shared import webhooks as wh

    # Fake supabase client whose .rpc(...).execute() returns 3 rows.
    rows = [
        {
            "id": "d1",
            "target_url": "https://example.com/h1",
            "payload": {"event_type": "t1"},
            "attempts": 0,
            "last_error": None,
        },
        {
            "id": "d2",
            "target_url": "https://example.com/h2",
            "payload": {"event_type": "t2"},
            "attempts": 1,
            "last_error": "prev fail",
        },
        {
            "id": "d3",
            "target_url": "https://example.com/h3",
            "payload": {"event_type": "t3"},
            "attempts": 0,
            "last_error": None,
        },
    ]

    class _FakeExec:
        def execute(self):
            class _R:
                data = rows

            return _R()

    class _FakeClient:
        def rpc(self, name, params):
            assert name == "claim_due_webhook_deliveries"
            assert params.get("p_limit") == 50
            return _FakeExec()

    monkeypatch.setattr(wh, "get_service_client", lambda: _FakeClient())

    dispatched: list[dict] = []

    def _fake_bounded(*, delivery_id, target_url, payload, prior_attempts):
        dispatched.append(
            {
                "delivery_id": delivery_id,
                "target_url": target_url,
                "payload": payload,
                "prior_attempts": prior_attempts,
            }
        )

    monkeypatch.setattr(wh, "_bounded_dispatch", _fake_bounded)

    # Patch Thread to run inline so the test can observe dispatches
    # without waiting for daemon threads.
    class _InlineThread:
        def __init__(self, target, kwargs, name, daemon):
            self._target = target
            self._kwargs = kwargs

        def start(self):
            self._target(**self._kwargs)

    monkeypatch.setattr(wh.threading, "Thread", _InlineThread)

    count = wh.sweep_due_deliveries(limit=50)
    assert count == 3
    assert len(dispatched) == 3
    assert {d["delivery_id"] for d in dispatched} == {"d1", "d2", "d3"}
    # prior_attempts propagated correctly so backoff schedule continues.
    by_id = {d["delivery_id"]: d for d in dispatched}
    assert by_id["d2"]["prior_attempts"] == 1


def test_sweep_drops_rows_past_max_attempts(monkeypatch):
    """Rows whose attempts already exhausted the backoff get stamped
    delivered_at instead of being re-dispatched."""
    from shared import webhooks as wh

    rows = [
        {
            "id": "d-exhausted",
            "target_url": "https://example.com/h",
            "payload": {},
            "attempts": wh._MAX_ATTEMPTS,
            "last_error": "all gone",
        },
    ]

    class _FakeExec:
        def execute(self):
            class _R:
                data = rows

            return _R()

    class _FakeClient:
        def rpc(self, *a, **kw):
            return _FakeExec()

    monkeypatch.setattr(wh, "get_service_client", lambda: _FakeClient())

    update_calls: list[dict] = []
    monkeypatch.setattr(
        wh, "_update_delivery", lambda **kw: update_calls.append(kw)
    )
    monkeypatch.setattr(
        wh,
        "_bounded_dispatch",
        lambda **kw: pytest.fail("should not dispatch exhausted row"),
    )

    count = wh.sweep_due_deliveries(limit=50)
    assert count == 0
    assert any(u.get("delivered_at") for u in update_calls)


def test_sweep_handles_missing_service_client(monkeypatch):
    """When supabase is unreachable, sweep returns 0 (not crash)."""
    from shared import webhooks as wh

    monkeypatch.setattr(wh, "get_service_client", lambda: None)
    count = wh.sweep_due_deliveries(limit=50)
    assert count == 0


def test_sweep_skips_rows_with_missing_target_url(monkeypatch):
    """A row with no target_url is stamped delivered_at and not dispatched."""
    from shared import webhooks as wh

    rows = [
        {
            "id": "d-notarget",
            "target_url": "",
            "payload": {},
            "attempts": 0,
            "last_error": None,
        },
    ]

    class _FakeExec:
        def execute(self):
            class _R:
                data = rows

            return _R()

    class _FakeClient:
        def rpc(self, *a, **kw):
            return _FakeExec()

    monkeypatch.setattr(wh, "get_service_client", lambda: _FakeClient())

    update_calls: list[dict] = []
    monkeypatch.setattr(
        wh, "_update_delivery", lambda **kw: update_calls.append(kw)
    )
    monkeypatch.setattr(
        wh,
        "_bounded_dispatch",
        lambda **kw: pytest.fail("should not dispatch row with no target_url"),
    )

    count = wh.sweep_due_deliveries(limit=50)
    assert count == 0
    assert any(
        "no target_url" in (u.get("last_error") or "") for u in update_calls
    )
