"""Unit tests for /metrics, /healthz, and the observation helpers.

Stream G.2 (Wave-0 hardening). Runs offline — no Supabase, no Prometheus
scraper required. Uses a Flask test client plus unittest.mock to stub out
the readiness probe, and monkeypatched env vars for the /metrics token gate.

Usage
-----
    pytest tests/test_metrics.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask, jsonify

from shared.metrics import (
    ECHOABLE_FORWARDING_HEADERS,
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
# /metrics bearer token
# ---------------------------------------------------------------------------
#
# This gate was an IP allowlist until 2026-08-22 and was never configured, so
# /metrics 403'd for everyone and the endpoint had no consumer at all. The
# allowlist cannot be made to work here: it resolved through _client_ip(), so
# it inherited X-Forwarded-For forgeability with a tiny guess space; its
# unforgeable alternative, request.remote_addr, is Railway's shared edge PoP;
# and the consumer that needed it is a GitHub-hosted runner with no stable
# address. See the shared/metrics.py docstring.


def _token_app(monkeypatch, token: str | None) -> Flask:
    if token is None:
        monkeypatch.delenv("METRICS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("METRICS_TOKEN", token)
    flask_app = Flask(__name__)
    register_metrics(flask_app)
    return flask_app


def test_metrics_is_forbidden_by_default(monkeypatch):
    """With no METRICS_TOKEN, every caller is denied — including one that
    presents an empty bearer, which must not compare equal to an unset var."""
    flask_app = _token_app(monkeypatch, None)
    assert flask_app.test_client().get("/metrics").status_code == 403
    assert flask_app.test_client().get(
        "/metrics", headers={"Authorization": "Bearer "}
    ).status_code == 403


def test_metrics_is_accessible_with_the_token(monkeypatch):
    """The right bearer gets the scrape."""
    flask_app = _token_app(monkeypatch, "s3cret-token")

    r = flask_app.test_client().get(
        "/metrics", headers={"Authorization": "Bearer s3cret-token"}
    )
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    # Prometheus exposition format always includes HELP + TYPE lines.
    assert "# HELP " in body
    assert "# TYPE " in body


def test_metrics_denies_a_wrong_token(monkeypatch):
    flask_app = _token_app(monkeypatch, "s3cret-token")

    r = flask_app.test_client().get(
        "/metrics", headers={"Authorization": "Bearer not-the-token"}
    )
    assert r.status_code == 403


def test_metrics_denies_a_malformed_authorization_header(monkeypatch):
    """Missing prefix, wrong scheme, or the raw token: all 403, none 500."""
    flask_app = _token_app(monkeypatch, "s3cret-token")
    client = flask_app.test_client()

    for header in (
        "s3cret-token",              # no scheme at all
        "Basic s3cret-token",        # wrong scheme
        "bearer s3cret-token",       # RFC says case-insensitive; we do not
        "Bearers3cret-token",        # no separating space
        "Bearer",                    # scheme only
        "",                          # present but empty
    ):
        r = client.get("/metrics", headers={"Authorization": header})
        assert r.status_code == 403, header


def test_metrics_denies_a_non_ascii_token_without_a_500(monkeypatch):
    """hmac.compare_digest raises TypeError on a non-ASCII str, so the
    comparison is done on bytes. A token full of emoji must be refused, not
    turned into a 500 that leaks a traceback."""
    flask_app = _token_app(monkeypatch, "s3cret-token")

    r = flask_app.test_client().get(
        "/metrics", headers={"Authorization": "Bearer ééé-café"}
    )
    assert r.status_code == 403


def test_metrics_accepts_a_non_ascii_token_when_it_is_the_configured_one(
    monkeypatch,
):
    """The bytes comparison must still be a real comparison, not a blanket
    refusal of everything non-ASCII."""
    flask_app = _token_app(monkeypatch, "café-token")

    r = flask_app.test_client().get(
        "/metrics", headers={"Authorization": "Bearer café-token"}
    )
    assert r.status_code == 200


def test_metrics_token_is_read_per_request_not_snapshotted(monkeypatch):
    """The allowlist this replaced was frozen at register_metrics() time, so
    rotating it needed a deploy. Reading per request is what makes a rotation
    in Railway take effect on the next request."""
    flask_app = _token_app(monkeypatch, "first")
    client = flask_app.test_client()
    assert client.get(
        "/metrics", headers={"Authorization": "Bearer first"}
    ).status_code == 200

    monkeypatch.setenv("METRICS_TOKEN", "second")
    assert client.get(
        "/metrics", headers={"Authorization": "Bearer first"}
    ).status_code == 403
    assert client.get(
        "/metrics", headers={"Authorization": "Bearer second"}
    ).status_code == 200


def test_the_token_comparison_is_constant_time(monkeypatch):
    """The bearer check must DECIDE through ``hmac.compare_digest``, not ``==``.

    Behavioural, not a source grep, and the distinction earned its place: an
    earlier version of this test asserted ``"hmac.compare_digest("`` appeared
    in the function's source, which a dead call sitting beside a live ``==``
    walks straight through. This forces ``compare_digest`` to return True and
    presents the WRONG token: if the verdict really flows through that call the
    gate opens, and if the code decided with ``==`` the wrong token is still
    refused and this test fails.

    No clock is involved on purpose. A wall-clock timing assertion is not
    reliably testable on a shared CI runner — it would either be so loose it
    passes for ``==`` too, or so tight it flakes under load. This pins the
    dataflow instead, which is the part a mutation actually changes.

    /metrics is an authentication control now (deny-by-default, this bearer is
    the only credential on it), so the comparison being constant-time is part
    of its contract and not an implementation detail.
    """
    flask_app = _token_app(monkeypatch, "the-real-token")
    wrong = {"Authorization": "Bearer wrong-token"}

    # Both halves matter, and the FIRST is why this is a differential rather
    # than a bare `== 200`: an endpoint with no gate at all also answers 200,
    # so the open half alone would pass against a deleted gate. Pinning the
    # closed half here means one test cannot be satisfied by removing the
    # thing it is testing.
    assert flask_app.test_client().get("/metrics", headers=wrong).status_code == 403, (
        "a wrong token reached /metrics even before compare_digest was "
        "patched — the gate is not closed at all"
    )

    monkeypatch.setattr("shared.metrics.hmac.compare_digest", lambda _a, _b: True)

    assert flask_app.test_client().get("/metrics", headers=wrong).status_code == 200, (
        "forcing hmac.compare_digest to True did not open the gate for a wrong "
        "token, so _metrics_token_ok is not deciding through it — a plain == "
        "is a timing oracle on the only credential guarding /metrics"
    )

    # The REJECT path alone is not enough. A short-circuiting
    # ``if presented == expected: return True`` placed AHEAD of a live
    # compare_digest passes both assertions above — every request they send
    # carries a wrong token, so the fast path never fires — while leaking the
    # matching-prefix length of a CORRECT-so-far token, which is the only
    # timing oracle an attacker can actually walk. Forcing compare_digest
    # FALSE and presenting the RIGHT token is what closes that: the accept
    # decision must flow through the call too, not just the reject decision.
    monkeypatch.setattr("shared.metrics.hmac.compare_digest", lambda _a, _b: False)

    assert flask_app.test_client().get(
        "/metrics", headers={"Authorization": "Bearer the-real-token"}
    ).status_code == 403, (
        "forcing hmac.compare_digest to False still admitted the CORRECT "
        "token, so something upstream of it accepts — an == short-circuit on "
        "the accept path is a timing oracle even with compare_digest present"
    )


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


def test_request_latency_recorded(app):
    """A round-trip to a real route must bump the request counter."""
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


def test_the_metrics_gate_no_longer_reads_the_forwarded_header_at_all(monkeypatch):
    """REWRITTEN 2026-08-22. This used to assert that the /metrics CIDR gate
    survived a forged X-Forwarded-For — a test whose whole premise was the
    worry that eventually killed the CIDR approach. The token gate does not
    consult the header, so the stronger invariant now holds: the header cannot
    grant access, and it cannot take it away either.

    _client_ip() itself is NOT retired — scout/ratelimit.py keys its per-IP
    buckets off it, which is why the tests above it stay.
    """
    monkeypatch.setenv("METRICS_TOKEN", "s3cret-token")
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    flask_app = Flask(__name__)
    register_metrics(flask_app)
    client = flask_app.test_client()

    forged = {"X-Forwarded-For": "10.1.2.3, 127.0.0.1, 203.0.113.7"}
    # No token: no header, however crafted, talks its way in.
    assert client.get("/metrics", headers=forged).status_code == 403
    # With the token: the header is irrelevant, not disqualifying.
    assert client.get(
        "/metrics", headers={**forged, "Authorization": "Bearer s3cret-token"}
    ).status_code == 200


# ---------------------------------------------------------------------------
# /metrics must render something under the DEPLOYED gunicorn configuration
# ---------------------------------------------------------------------------
#
# The counters are built when app.py is imported, and under ``preload_app``
# that import happens inside ``Arbiter.__init__`` — before ``Arbiter.start()``
# runs any hook. prometheus_client reads PROMETHEUS_MULTIPROC_DIR exactly
# once, at its own import, so provisioning the directory from a hook lands
# too late: every Counter stays process-local, no worker ever writes a db
# file, and /metrics answers 200 with an EMPTY body while looking perfectly
# healthy. Module scope is the only place early enough to prevent that,
# and it is the right place whether or not the deployment also supplies
# PROMETHEUS_MULTIPROC_DIR itself: if it ever does, the conf still has to
# create the directory before the import, or the preload crashes outright.
#
# tests/_metrics_boot_probe.py boots a real gunicorn arbiter in a subprocess
# and reports what a scrape would return. Its docstring explains why nothing
# cheaper can answer this without assuming the answer.

_BOOT_PROBE = Path(__file__).parent / "_metrics_boot_probe.py"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_boot_probe(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_BOOT_PROBE), *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"boot probe crashed (rc={proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    verdicts = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")]
    assert verdicts, f"boot probe printed no verdict\n{proc.stdout}\n{proc.stderr}"
    return json.loads(verdicts[-1][len("PROBE "):])


def test_metrics_renders_a_non_empty_body_under_the_gunicorn_boot_order():
    """A scrape after a real preloaded gunicorn boot must carry samples."""
    pytest.importorskip("prometheus_client")

    verdict = _run_boot_probe()

    assert verdict["preload_app"] is True, (
        "This guard only means anything while the app is preloaded. If "
        "preload_app was turned off deliberately, retire the guard with it."
    )
    assert verdict["imported_during_preload"] is True, (
        "The app was NOT imported by preload inside Arbiter.__init__, so "
        "this run proves nothing about when the env var has to be set."
    )
    assert verdict["prometheus"] is True, (
        "prometheus_client imports fine in this interpreter but not under "
        "the gunicorn boot, so shared.metrics fell back to its stub path."
    )
    # THE property. A non-empty body is corroboration, not proof: one stale
    # db file left in the shared multiprocess directory renders samples
    # while the backend is still process-local, which is exactly how this
    # guard would come back green over a live defect.
    assert verdict["value_class"] == "MmapedValue", (
        "prometheus_client froze ValueClass to "
        f"{verdict.get('value_class')}, so every counter is process-local "
        "and no worker writes a db file. PROMETHEUS_MULTIPROC_DIR "
        f"({verdict.get('multiproc_dir')!r}) reached it only after the app "
        "import. Provision the directory at gunicorn.conf.py module scope, "
        "not from a gunicorn hook."
    )
    assert verdict["body_bytes"] > 0, (
        "/metrics rendered a ZERO-BYTE body even though the multiprocess "
        "backend is active, so the collector and the counters disagree "
        f"about {verdict.get('multiproc_dir')!r}."
    )
    assert verdict["has_counter"], (
        "The scrape rendered bytes but not tools_hub_scout_runs_total, so "
        "the counters and the collector are not reading the same place."
    )


def test_the_boot_guard_stays_quiet_when_prometheus_client_is_absent():
    """The stub path in shared.metrics is legitimate, not a failure.

    shared.metrics deliberately stubs the Counter factory when
    prometheus_client is missing so an offline checkout still imports and
    /metrics answers an informative 503. The guard above must not turn that
    into a red suite.
    """
    verdict = _run_boot_probe("--no-prometheus")

    assert verdict["prometheus"] is False
    assert "body_bytes" not in verdict, (
        "With prometheus_client blocked there is no exposition to measure; "
        "the probe must report the stub path rather than a body size."
    )


# ---------------------------------------------------------------------------
# /debug/client-ip — the echo that makes the limiter's key observable
# ---------------------------------------------------------------------------
#
# Added after production measurement showed _client_ip() varying between
# identical requests, with no way to see what it resolved to. See
# docs/MEASUREMENT-2026-08-24-per-ip-key-is-not-stable.md.


def test_client_ip_echo_is_forbidden_by_default(monkeypatch):
    """Same deny-by-default as /metrics: an unset token refuses everyone.

    This endpoint describes the exact header the per-IP limiter trusts, so an
    open version of it is a forging aid.
    """
    flask_app = _token_app(monkeypatch, None)
    assert flask_app.test_client().get("/debug/client-ip").status_code == 403
    assert flask_app.test_client().get(
        "/debug/client-ip", headers={"Authorization": "Bearer "}
    ).status_code == 403


def test_client_ip_echo_denies_a_wrong_token(monkeypatch):
    flask_app = _token_app(monkeypatch, "right-token")
    assert flask_app.test_client().get(
        "/debug/client-ip", headers={"Authorization": "Bearer wrong-token"}
    ).status_code == 403


def test_client_ip_echo_reports_what_the_app_resolved(monkeypatch):
    """The whole point: it must report the RESOLVED key, not just the header.

    With one trusted hop, _client_ip() takes the RIGHTMOST entry, so a
    multi-value header must resolve to the last one. If this ever reports the
    leftmost, the limiter is keyed on a caller-chosen value.
    """
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    flask_app = _token_app(monkeypatch, "tok")

    r = flask_app.test_client().get(
        "/debug/client-ip",
        headers={
            "Authorization": "Bearer tok",
            "X-Forwarded-For": "192.0.2.111, 192.0.2.222",
        },
    )
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["client_ip"] == "192.0.2.222"
    assert payload["trusted_proxy_hops"] == 1
    assert payload["forwarding_headers"]["X-Forwarded-For"] == "192.0.2.111, 192.0.2.222"


def test_client_ip_echo_echoes_only_the_allowlist(monkeypatch):
    """The echo must return allowlisted headers and NOTHING else.

    This asserts the CLASS, not two literal names, and that distinction is the
    whole point. An earlier version of this test checked only that
    ``Authorization`` and ``Cookie`` were absent -- which "echo every header
    except those two" passes, and so does a prefix match on ``x-`` . Both were
    caught only by mutation, never by the assertion.

    ``X-Forwarded-Access-Token`` (oauth2-proxy) and ``X-Forwarded-Client-Cert``
    (Envoy/Istio, carrying a full client PEM and a SPIFFE identity) are the
    concrete reason: they start with ``x-forwarded`` but are credentials, and
    the endpoint's response is republished into a PUBLIC Actions log.
    """
    flask_app = _token_app(monkeypatch, "leaky-token")

    r = flask_app.test_client().get(
        "/debug/client-ip",
        headers={
            "Authorization": "Bearer leaky-token",
            "Cookie": "session=super-secret-session-value",
            "X-Forwarded-For": "192.0.2.9",
            "X-Forwarded-Access-Token": "ya29.a-live-oauth-access-token",
            "X-Forwarded-Client-Cert": "By=spiffe://cluster/ns/default;Cert=-----BEGIN%20CERT",
            "X-Forwarded-Email": "someone@example.com",
        },
    )
    assert r.status_code == 200
    body = r.data.decode("utf-8")

    # Nothing secret reaches the body, whatever the filter happens to be.
    for secret in (
        "leaky-token",
        "super-secret-session-value",
        "ya29.a-live-oauth-access-token",
        "spiffe://cluster",
        "someone@example.com",
    ):
        assert secret not in body, f"{secret!r} was echoed"

    # And the filter itself is a subset of the allowlist, so widening the
    # allowlist is the only way to echo anything new -- which lands here.
    echoed = {name.lower() for name in r.get_json()["forwarding_headers"]}
    assert echoed <= ECHOABLE_FORWARDING_HEADERS, (
        f"echoed outside the allowlist: {echoed - ECHOABLE_FORWARDING_HEADERS}"
    )
    assert "x-forwarded-for" in echoed, "the one header the diagnostic exists for"


def test_client_ip_echo_reports_non_default_hops(monkeypatch):
    """The hop count must reflect the real config, not a constant.

    Hardcoding ``"trusted_proxy_hops": 1`` survived every other test in this
    file. On a diagnostic whose job is to report the configuration, a field
    that stops tracking it is the failure that matters most.
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    flask_app = _token_app(monkeypatch, "tok")

    r = flask_app.test_client().get(
        "/debug/client-ip",
        headers={
            "Authorization": "Bearer tok",
            "X-Forwarded-For": "192.0.2.111, 192.0.2.222",
        },
    )
    payload = r.get_json()
    assert payload["trusted_proxy_hops"] == 2
    # Two trusted hops means the entry one further LEFT is ours.
    assert payload["client_ip"] == "192.0.2.111"


# ---------------------------------------------------------------------------
# X-Real-Ip is preferred, because it is the measured-unforgeable one
# ---------------------------------------------------------------------------


def test_client_ip_prefers_x_real_ip_over_the_forwarded_chain(monkeypatch):
    """The production shape, copied from a live probe on 2026-08-24.

    Railway sends ``X-Forwarded-For: <client>, <internal>`` with a ROTATING
    internal hop, plus ``X-Real-Ip: <client>``. Reading one hop from the right
    keyed on the rotating value, which is why the per-IP limiter never refused
    anyone. X-Real-Ip is constant across the same requests.
    """
    for internal in ("152.233.30.101", "152.233.30.102", "152.233.30.104"):
        got = _ip_under(
            {
                "X-Forwarded-For": f"4.236.158.49, {internal}",
                "X-Real-Ip": "4.236.158.49",
            },
            monkeypatch=monkeypatch,
        )
        assert got == "4.236.158.49", f"rotating hop {internal} leaked into the key"


def test_client_ip_falls_back_to_the_chain_without_x_real_ip(monkeypatch):
    """The fallback is unchanged, so every other deployment behaves as before."""
    got = _ip_under(
        {"X-Forwarded-For": "1.2.3.4, 198.51.100.9"}, monkeypatch=monkeypatch
    )
    assert got == "198.51.100.9"


def test_zero_hops_ignores_x_real_ip_too(monkeypatch):
    """TRUSTED_PROXY_HOPS=0 means no proxy in front, so NO header is trusted.

    If X-Real-Ip were honoured here, a direct-origin deployment would let any
    caller choose the limiter key by sending one -- the exact hole the hop
    count exists to close.
    """
    got = _ip_under(
        {"X-Real-Ip": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"},
        hops=0,
        monkeypatch=monkeypatch,
    )
    assert got == "203.0.113.7"


def test_a_blank_x_real_ip_does_not_shadow_the_chain(monkeypatch):
    """An empty or whitespace header must not resolve to "" and bucket
    every caller together -- that would rate-limit the whole internet as one
    user while looking like it worked."""
    for blank in ("", "   "):
        got = _ip_under(
            {"X-Real-Ip": blank, "X-Forwarded-For": "1.2.3.4, 198.51.100.9"},
            monkeypatch=monkeypatch,
        )
        assert got == "198.51.100.9", f"blank X-Real-Ip {blank!r} shadowed the chain"


def test_a_deeper_hop_count_selects_the_chain_not_x_real_ip(monkeypatch):
    """TRUSTED_PROXY_HOPS must keep meaning what its docstring says.

    Its stated purpose is "another proxy in front makes it 2". If X-Real-Ip
    short-circuited at every non-zero hop count, that knob would be silently
    dead -- and in the Cloudflare-in-front case it names, X-Real-Ip is
    Cloudflare's egress address, which collapses every visitor behind it onto
    one limiter key. Exactly the "rate-limit the whole internet as one user"
    failure the blank-value test guards, reached through a supported config.
    """
    got = _ip_under(
        {"X-Real-Ip": "1.2.3.4", "X-Forwarded-For": "5.5.5.5, 6.6.6.6, 7.7.7.7"},
        hops=2,
        monkeypatch=monkeypatch,
    )
    assert got == "6.6.6.6", "X-Real-Ip overrode an explicitly configured hop count"


def test_duplicate_x_real_ip_headers_do_not_become_the_key(monkeypatch):
    """Two X-Real-Ip headers MERGE into one comma-joined WSGI value.

    So "a single edge-written header has no index to shift" is only true once
    a comma-joined value is rejected. Without this the caller supplies half of
    a two-element list and the claim is false as written.
    """
    got = _ip_under(
        [
            ("X-Real-Ip", "198.51.100.7"),
            ("X-Real-Ip", "4.4.4.4"),
            ("X-Forwarded-For", "1.1.1.1, 2.2.2.2"),
        ],
        monkeypatch=monkeypatch,
    )
    assert got == "2.2.2.2", "a merged X-Real-Ip pair became the limiter key"


def test_a_non_address_x_real_ip_falls_through_to_the_chain(monkeypatch):
    """Anything that is not a bare IP is not a limiter key.

    Measured before the guard: 'evil.example:8080' was returned verbatim, as
    were hostnames, empty-ish values and 2000-character strings. Each of those
    is a key an upstream could choose.
    """
    for junk in ("evil.example:8080", "not-an-ip", "1.2.3.4, 5.6.7.8", "x" * 2000, "\t"):
        got = _ip_under(
            {"X-Real-Ip": junk, "X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
            monkeypatch=monkeypatch,
        )
        assert got == "2.2.2.2", f"junk X-Real-Ip {junk[:30]!r} became the key"


def test_ipv6_x_real_ip_is_still_accepted(monkeypatch):
    """The guard rejects junk, not legitimate v6."""
    got = _ip_under(
        {"X-Real-Ip": "2001:db8::1", "X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
        monkeypatch=monkeypatch,
    )
    assert got == "2001:db8::1"


# ---------------------------------------------------------------------------
# The limiter key's source is counted, because losing it fails SILENTLY
# ---------------------------------------------------------------------------


def _source_counts():
    """Read tools_hub_client_ip_source_total out of the live registry."""
    from shared.metrics import CLIENT_IP_SOURCE

    out = {}
    for metric in CLIENT_IP_SOURCE.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                out[sample.labels["source"]] = sample.value
    return out


def test_each_resolution_path_is_counted_by_source(monkeypatch):
    """x_real_ip / forwarded_chain / peer must each be distinguishable.

    If X-Real-Ip ever stops arriving, _client_ip() silently falls back to the
    chain and keys on Railway's ROTATING internal hop -- the per-IP limiter
    goes inert exactly as it was before #189, with no error, no refusal and no
    latency change. This counter is the only way that becomes visible.
    """
    before = _source_counts()

    _ip_under({"X-Real-Ip": "4.236.158.49"}, monkeypatch=monkeypatch)
    _ip_under({"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}, monkeypatch=monkeypatch)
    _ip_under({}, monkeypatch=monkeypatch)

    after = _source_counts()
    for source in ("x_real_ip", "forwarded_chain", "peer"):
        delta = after.get(source, 0.0) - before.get(source, 0.0)
        assert delta == 1, f"{source} moved by {delta}, expected 1"


def test_a_rejected_x_real_ip_counts_as_the_chain_not_as_x_real_ip(monkeypatch):
    """The counter must reflect what was USED, not what was present.

    A junk X-Real-Ip falls through to the chain. Counting it as x_real_ip
    would make the detector report health while the limiter keys on the
    rotating hop -- the exact failure this counter exists to catch.
    """
    before = _source_counts()
    _ip_under(
        {"X-Real-Ip": "evil.example:8080", "X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
        monkeypatch=monkeypatch,
    )
    after = _source_counts()

    assert after.get("x_real_ip", 0.0) == before.get("x_real_ip", 0.0)
    assert after.get("forwarded_chain", 0.0) - before.get("forwarded_chain", 0.0) == 1


def test_a_rejected_x_real_ip_gets_its_own_label(monkeypatch):
    """Present-but-unusable is NOT the same as absent, and an operator acts on
    them differently: a rejected value means the edge is still setting the
    header and something between it and the app is mangling the value.
    """
    before = _source_counts()
    _ip_under(
        {"X-Real-Ip": "evil.example:8080", "X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
        monkeypatch=monkeypatch,
    )
    after = _source_counts()
    assert after.get("x_real_ip_rejected", 0.0) - before.get("x_real_ip_rejected", 0.0) == 1
    assert after.get("x_real_ip", 0.0) == before.get("x_real_ip", 0.0)


def test_zero_hops_counts_as_peer(monkeypatch):
    """TRUSTED_PROXY_HOPS=0 skips every header, so the key is the socket peer.

    Uncovered until 2026-08-24: a mutation moving the peer increment inside
    `if hops:` left this path counting NOTHING, so a deployment that had turned
    headers off would show a silently shrinking denominator.
    """
    before = _source_counts()
    _ip_under(
        {"X-Real-Ip": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"},
        hops=0,
        monkeypatch=monkeypatch,
    )
    after = _source_counts()
    assert after.get("peer", 0.0) - before.get("peer", 0.0) == 1


def test_a_blank_x_real_ip_counts_as_the_chain(monkeypatch):
    """Blank means unused, so it must not be counted as x_real_ip -- that would
    report the detector healthy while the chain supplied the key."""
    before = _source_counts()
    _ip_under(
        {"X-Real-Ip": "   ", "X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
        monkeypatch=monkeypatch,
    )
    after = _source_counts()
    assert after.get("x_real_ip", 0.0) == before.get("x_real_ip", 0.0)
    assert after.get("forwarded_chain", 0.0) - before.get("forwarded_chain", 0.0) == 1


def test_a_valid_x_real_ip_at_another_hop_count_is_not_counted_as_used(monkeypatch):
    """TRUSTED_PROXY_HOPS != 1 selects the chain, so X-Real-Ip is present and
    valid but NOT used. Counting it as used would hide the very config change
    that turned the preference off."""
    before = _source_counts()
    _ip_under(
        {"X-Real-Ip": "1.2.3.4", "X-Forwarded-For": "5.5.5.5, 6.6.6.6, 7.7.7.7"},
        hops=2,
        monkeypatch=monkeypatch,
    )
    after = _source_counts()
    assert after.get("x_real_ip", 0.0) == before.get("x_real_ip", 0.0)
    assert after.get("forwarded_chain", 0.0) - before.get("forwarded_chain", 0.0) == 1


def test_a_failing_counter_cannot_break_the_rate_limit_gate(monkeypatch):
    """THE safety property. _client_ip() decides whether a caller is refused.

    Measured before the guard existed: OSError(28) out of `labels()` propagated
    straight through _client_ip(), which turns a full metrics disk into a 500
    on every anonymous Scout route. PROMETHEUS_MULTIPROC_DIR lives on Railway's
    ephemeral filesystem and this repo has a prior incident of a reaper
    deleting a shared tmp/, so it is not hypothetical.

    Every other counter in shared/metrics.py is wrapped the same way; this one
    needs it most, because it is the only one on a gate.
    """
    import shared.metrics as metrics_module
    from unittest import mock  # noqa: PLC0415

    exploding = mock.MagicMock()
    exploding.labels.side_effect = OSError(28, "No space left on device")
    monkeypatch.setattr(metrics_module, "CLIENT_IP_SOURCE", exploding)

    # Each of the three resolution paths, all of which increment.
    assert _ip_under({"X-Real-Ip": "4.236.158.49"}, monkeypatch=monkeypatch) == "4.236.158.49"
    assert _ip_under({"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}, monkeypatch=monkeypatch) == "2.2.2.2"
    assert _ip_under({}, monkeypatch=monkeypatch) == "203.0.113.7"
    assert exploding.labels.called, "the mutation-proof needs the counter to be reached"
