"""Unit tests for /metrics, /healthz, and the observation helpers.

Stream G.2 (Wave-0 hardening). Runs offline — no Supabase, no Prometheus
scraper required. Uses a Flask test client plus unittest.mock to stub out
the readiness probe and IP allowlist.

Usage
-----
    pytest tests/test_metrics.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import Flask, jsonify

from shared.metrics import (
    observe_credits_granted,
    observe_credits_spent,
    observe_idempotency_outcome,
    observe_stripe_event,
    register_metrics,
)


@pytest.fixture
def app():
    flask_app = Flask(__name__)

    @flask_app.route("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    # Dummy route so latency/request counters have something to observe.
    @flask_app.route("/echo", methods=["POST"])
    def echo():
        return jsonify({"ok": True}), 200

    register_metrics(flask_app)
    return flask_app


# ---------------------------------------------------------------------------
# /health vs /healthz
# ---------------------------------------------------------------------------


def test_health_is_liveness_only(app):
    """/health must stay a dumb 200 — no dependency calls."""
    r = app.test_client().get("/health")
    assert r.status_code == 200
    assert r.json == {"status": "ok"}


def test_healthz_returns_200_when_dependencies_ok(app):
    """/healthz returns 200 when Supabase + Stripe are both configured."""
    with patch(
        "shared.metrics._readiness_probe", return_value=(True, "ok")
    ):
        r = app.test_client().get("/healthz")
    assert r.status_code == 200
    assert r.json["status"] == "ok"


def test_healthz_returns_503_when_supabase_down(app):
    """/healthz returns 503 if the readiness probe reports a failure."""
    with patch(
        "shared.metrics._readiness_probe",
        return_value=(False, "supabase_unavailable"),
    ):
        r = app.test_client().get("/healthz")
    assert r.status_code == 503
    assert r.json["status"] == "degraded"
    assert r.json["detail"] == "supabase_unavailable"


# ---------------------------------------------------------------------------
# /metrics IP allowlist
# ---------------------------------------------------------------------------


def test_metrics_is_forbidden_by_default(app):
    """With no METRICS_ALLOWED_CIDR, every caller is denied."""
    r = app.test_client().get("/metrics")
    assert r.status_code == 403


def test_metrics_is_accessible_when_allowlisted(monkeypatch):
    """A caller whose IP falls inside METRICS_ALLOWED_CIDR gets the scrape."""
    monkeypatch.setenv("METRICS_ALLOWED_CIDR", "127.0.0.0/8")
    flask_app = Flask(__name__)
    register_metrics(flask_app)

    r = flask_app.test_client().get("/metrics")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    # Prometheus exposition format always includes HELP + TYPE lines.
    assert "# HELP " in body
    assert "# TYPE " in body


def test_metrics_denies_caller_outside_allowlist(monkeypatch):
    """Allowlist must actually exclude non-matching IPs."""
    monkeypatch.setenv("METRICS_ALLOWED_CIDR", "10.0.0.0/8")
    flask_app = Flask(__name__)
    register_metrics(flask_app)

    # Test client default remote_addr is 127.0.0.1, outside 10.0.0.0/8.
    r = flask_app.test_client().get("/metrics")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Counters increment via the observation helpers
# ---------------------------------------------------------------------------


def test_observe_credits_spent_increments_counter():
    # Grab the raw metric to compare before/after.
    before = _sample("tools_hub_credits_spent_total", {"tool": "unit-test"})
    observe_credits_spent("unit-test", 3)
    after = _sample("tools_hub_credits_spent_total", {"tool": "unit-test"})
    assert after == before + 3


def test_observe_credits_granted_increments_counter():
    labels = {"tier": "scout_pro", "event": "unit-test"}
    before = _sample("tools_hub_credits_granted_total", labels)
    observe_credits_granted("scout_pro", "unit-test", 50)
    after = _sample("tools_hub_credits_granted_total", labels)
    assert after == before + 50


def test_observe_stripe_event_increments_counter():
    labels = {"event_type": "invoice.paid", "outcome": "ok"}
    before = _sample("tools_hub_stripe_events_total", labels)
    observe_stripe_event("invoice.paid", "ok")
    after = _sample("tools_hub_stripe_events_total", labels)
    assert after == before + 1


def test_observe_idempotency_outcome_increments_counter():
    labels = {"outcome": "replay"}
    before = _sample("tools_hub_idempotency_outcomes_total", labels)
    observe_idempotency_outcome("replay")
    after = _sample("tools_hub_idempotency_outcomes_total", labels)
    assert after == before + 1


def test_request_latency_recorded(app, monkeypatch):
    """A round-trip to a real route must bump the request counter."""
    monkeypatch.setenv("METRICS_ALLOWED_CIDR", "127.0.0.0/8")
    # Hit the route once.
    app.test_client().post("/echo", data=b"x")
    value = _sample(
        "tools_hub_requests_total",
        {"route": "echo", "status_class": "2xx"},
    )
    assert value >= 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample(metric_name: str, labels: dict[str, str]) -> float:
    """Return the current value of a labelled counter from the default registry."""
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(metric_name, labels)
    return value if value is not None else 0.0


# ---------------------------------------------------------------------------
# _client_ip — X-Forwarded-For hop selection
# ---------------------------------------------------------------------------
#
# _client_ip is the key for two security gates: the /metrics CIDR allowlist
# just above, and scout.ratelimit's per-IP buckets. Until 2026-08-18 it read
# the LEFTmost X-Forwarded-For entry, which the caller writes, so either gate
# could be defeated by sending a chosen header. These tests pin the hop
# arithmetic that fixes it.


def _ip_under(headers, hops=None, monkeypatch=None, remote_addr="203.0.113.7"):
    """Resolve _client_ip() inside a request context with these headers."""
    from shared.metrics import _client_ip

    if monkeypatch is not None:
        if hops is None:
            monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
        else:
            monkeypatch.setenv("TRUSTED_PROXY_HOPS", str(hops))
    flask_app = Flask(__name__)
    with flask_app.test_request_context(
        "/", headers=headers, environ_base={"REMOTE_ADDR": remote_addr}
    ):
        return _client_ip()


def test_client_ip_falls_back_to_socket_peer_without_the_header(monkeypatch):
    assert _ip_under({}, monkeypatch=monkeypatch) == "203.0.113.7"


def test_client_ip_uses_the_only_value_of_a_single_entry_header(monkeypatch):
    """THE load-bearing case: correct under BOTH edge semantics.

    If Railway's edge *overwrites* X-Forwarded-For with the peer it saw, the
    header has one entry and it is the client. If the edge *appends* and the
    client sent no header, the header also has one entry and it is the
    client. One trusted hop returns that entry either way, so the fix does
    not depend on knowing which behaviour Railway has.
    """
    got = _ip_under({"X-Forwarded-For": "198.51.100.9"}, monkeypatch=monkeypatch)
    assert got == "198.51.100.9"


def test_client_ip_takes_the_rightmost_of_two_entries(monkeypatch):
    """Append semantics with a spoofed leading entry: trust only our hop."""
    got = _ip_under(
        {"X-Forwarded-For": "1.2.3.4, 198.51.100.9"}, monkeypatch=monkeypatch
    )
    assert got == "198.51.100.9"


def test_client_ip_ignores_a_long_spoofed_chain(monkeypatch):
    """A caller padding the header cannot push its chosen value into play."""
    spoof = ", ".join(f"10.0.0.{i}" for i in range(20))
    got = _ip_under(
        {"X-Forwarded-For": f"{spoof}, 198.51.100.9"}, monkeypatch=monkeypatch
    )
    assert got == "198.51.100.9"


def test_client_ip_tolerates_whitespace_and_empty_entries(monkeypatch):
    got = _ip_under(
        {"X-Forwarded-For": "  1.2.3.4 ,, ,   198.51.100.9   "},
        monkeypatch=monkeypatch,
    )
    assert got == "198.51.100.9"


def test_client_ip_falls_back_when_the_header_is_only_separators(monkeypatch):
    got = _ip_under({"X-Forwarded-For": " , ,, "}, monkeypatch=monkeypatch)
    assert got == "203.0.113.7"


def test_client_ip_honours_a_deeper_trusted_hop_count(monkeypatch):
    """Two trusted proxies (e.g. Cloudflare in front of Railway)."""
    got = _ip_under(
        {"X-Forwarded-For": "1.2.3.4, 198.51.100.9, 192.0.2.1"},
        hops=2,
        monkeypatch=monkeypatch,
    )
    assert got == "198.51.100.9"


def test_client_ip_clamps_when_the_chain_is_shorter_than_the_hop_count(monkeypatch):
    """Must not raise IndexError; yields the leftmost entry available."""
    got = _ip_under(
        {"X-Forwarded-For": "198.51.100.9"}, hops=3, monkeypatch=monkeypatch
    )
    assert got == "198.51.100.9"


def test_client_ip_zero_hops_ignores_the_header_entirely(monkeypatch):
    """TRUSTED_PROXY_HOPS=0 is the no-proxy deployment: trust only the peer."""
    got = _ip_under(
        {"X-Forwarded-For": "1.2.3.4"}, hops=0, monkeypatch=monkeypatch
    )
    assert got == "203.0.113.7"


def test_client_ip_ignores_a_malformed_hop_count(monkeypatch):
    """A bad env value must not crash a request; falls back to one hop."""
    got = _ip_under(
        {"X-Forwarded-For": "1.2.3.4, 198.51.100.9"},
        hops="banana",
        monkeypatch=monkeypatch,
    )
    assert got == "198.51.100.9"


def test_metrics_allowlist_cannot_be_defeated_by_a_forged_header(monkeypatch):
    """The /metrics CIDR gate is the second consumer of the same fix.

    A caller outside the allowlist must not be able to talk its way in by
    prepending an allowlisted address to X-Forwarded-For.
    """
    monkeypatch.setenv("METRICS_ALLOWED_CIDR", "10.0.0.0/8")
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    flask_app = Flask(__name__)
    register_metrics(flask_app)

    r = flask_app.test_client().get(
        "/metrics", headers={"X-Forwarded-For": "10.1.2.3, 203.0.113.7"}
    )
    assert r.status_code == 403
