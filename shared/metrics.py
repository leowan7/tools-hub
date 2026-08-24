"""Prometheus metrics for the Ranomics tools-hub.

Stream G.2 (Wave-0 hardening). Exposes ``/metrics`` in the Prometheus
text format plus a ``/healthz`` readiness probe distinct from the existing
``/health`` liveness probe. All metric definitions live here so adding a
new counter means editing one file.

Design
------
Endpoints
    /health        — liveness. 200 always. Existing Railway port scanner
                     uses this. Do not add dependencies.
    /healthz       — readiness. 200 only if Supabase is reachable. Used by
                     schedulers / deploy gates.
    /metrics       — Prometheus text exposition. Gated on a bearer token in
                     the ``METRICS_TOKEN`` env var, presented as
                     ``Authorization: Bearer <token>``. Deny by default:
                     unset or empty means nobody gets in.

Why deny by default
    The Railway service is on the public internet; anyone on the web can
    hit /metrics if the endpoint is open. Prometheus scrape contents can
    leak traffic patterns, user counts, and error rates — low but
    non-zero signal for a motivated attacker. Requiring an explicit
    credential is the right posture; a misconfigured one simply denies the
    scraper, which is visible in the scraper's own error state.

Why a token and NOT an IP allowlist
    This endpoint was CIDR-gated until 2026-08-22 and the gate was never
    set, so /metrics 403'd for everyone. Do not bring the CIDR back — it
    cannot work on this stack, for three independent reasons:

    1. The allowlist resolved through ``_client_ip()``, which honours
       ``X-Forwarded-For``. That makes the gate inherit the whole
       forwarded-header trust question, and here the failure mode is auth
       bypass with a tiny guess space (``10/8``, ``172.16/12``).
    2. The unforgeable alternative, ``request.remote_addr``, is Railway's
       edge PoP (measured: Datacamp/CDN77, ``x-railway-edge: jfk1``),
       shared by every visitor on earth. Allowlisting it allowlists the
       internet.
    3. The consumer is a GitHub-hosted runner
       (``.github/workflows/synthetic-smoke.yml``), which has no stable
       address to allowlist in the first place.

    The token is read per request rather than snapshotted at
    ``register_metrics()`` time, so rotating it in Railway takes effect on
    the next request instead of the next deploy.

Multiprocess mode
    Gunicorn forks workers; the default ``prometheus_client`` backend
    uses per-process state which is invisible to cross-worker scrapes.
    Setting ``PROMETHEUS_MULTIPROC_DIR`` activates the shared-directory
    backend so counters aggregate across workers. The gunicorn conf
    provisions that directory on boot.

Usage
-----
    from shared.metrics import register_metrics, REQUEST_LATENCY, CREDITS_SPENT

    register_metrics(flask_app)

    CREDITS_SPENT.labels(tool="example-gpu").inc(amount)
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import time
from typing import Any

from flask import Flask, Response, g, has_request_context, jsonify, request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------
# All metric names are prefixed ``tools_hub_`` to make them unambiguous in a
# shared Grafana workspace that also ingests epitope-scout / kendrew.

try:
    from prometheus_client import (  # type: ignore[import-untyped]
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        multiprocess,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PROMETHEUS_AVAILABLE = False
    # Stub types so imports downstream don't fail in offline dev; the
    # endpoints themselves render an informative 503 when called.
    CONTENT_TYPE_LATEST = "text/plain"

    class _Stub:
        def labels(self, *_a: Any, **_kw: Any) -> "_Stub":
            return self

        def inc(self, *_a: Any, **_kw: Any) -> None:
            return None

        def dec(self, *_a: Any, **_kw: Any) -> None:
            return None

        def set(self, *_a: Any, **_kw: Any) -> None:
            return None

        def observe(self, *_a: Any, **_kw: Any) -> None:
            return None

    def Counter(*_a: Any, **_kw: Any) -> _Stub:  # type: ignore[misc]
        return _Stub()

    def Gauge(*_a: Any, **_kw: Any) -> _Stub:  # type: ignore[misc]
        return _Stub()

    def Histogram(*_a: Any, **_kw: Any) -> _Stub:  # type: ignore[misc]
        return _Stub()


REQUESTS_TOTAL = Counter(
    "tools_hub_requests_total",
    "HTTP requests handled, by route and status class.",
    ["route", "status_class"],
)

REQUEST_LATENCY = Histogram(
    "tools_hub_request_latency_seconds",
    "Request latency by route.",
    ["route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

CREDITS_SPENT = Counter(
    "tools_hub_credits_spent_total",
    "Credits debited for tool runs, by tool.",
    ["tool"],
)

CREDITS_GRANTED = Counter(
    "tools_hub_credits_granted_total",
    "Credits granted to users, by tier and event type.",
    ["tier", "event"],
)

STRIPE_EVENTS = Counter(
    "tools_hub_stripe_events_total",
    "Stripe webhook events received, by event type and outcome.",
    ["event_type", "outcome"],
)

SCOUT_RUNS = Counter(
    "tools_hub_scout_runs_total",
    "Epitope Scout analysis runs recorded (signed-in only).",
)

# The anonymous /scout/analyze meter needs Content-Length to decide whether a
# body is small enough to read for its follow-up credit (see
# ``scout.ratelimit._MAX_FOLLOWUP_BODY_BYTES``). When the length is unreadable
# it fails closed, and nothing is refused — so no refusal-rate metric moves.
# This is the one that does.
#
# It counts REQUESTS THE METER COULD NOT SIZE, and claims nothing beyond that.
# The meter runs ahead of both limiter tiers and every refusal, so refused
# requests count here too: 25 chunked POSTs for a nonexistent job measured as
# 8x404 then 17x429, all 25 on ``chunked``, no analysis run and nobody charged
# twice. A sustained rise in ``chunked`` is therefore a reason to INVESTIGATE
# whether the edge is re-framing bodies — correlate it against successful
# analyses, which is where the lost credits would show — not proof on its own
# that anonymous capacity has halved. ``chunked`` is exactly what the code
# tests and nothing more — a Transfer-Encoding value CONTAINING ``chunked``,
# in any case, stacked with other codings or not — so any caller can pick
# this label by sending that framing.
# ``other`` is that rule negated: every request the meter could not size whose
# framing is not that. A POST with no body at all (a scanner's opening move)
# and a transfer coding that is not ``chunked`` are the common cases, NOT a
# closed list — the label is the negation, not the enumeration.
SCOUT_UNMETERED_BODIES = Counter(
    "tools_hub_scout_unmetered_bodies_total",
    "POST /scout/analyze bodies the anonymous meter could not size, by framing.",
    ["framing"],  # chunked|other
)

# ONE logical event — "we refused an anonymous Epitope Scout caller" — leaves
# the app as THREE different HTTP status codes: 429 (both rate-limit tiers,
# and the per-session live-job cap), 503 (the fleet live-job cap and the JSON
# compute shed) and **200 text/event-stream** (the SSE compute shed, because
# EventSource cannot read a non-2xx body). ``REQUESTS_TOTAL`` labels only
# ``(route, status_class)``, so without this counter the refusals collapse into
# status classes and the SSE ones are counted as SUCCESSES. That is why a
# refusal rate cannot be derived from status codes here, and why this exists.
#
# ``reason`` takes the seven fixed values in ``scout.ratelimit`` (five policy
# refusals plus the two SSE non-refusals ``bad_request`` / ``job_expired``) and
# ``route`` is a Flask endpoint name — nothing caller-controlled on either
# label, so the cardinality is bounded and small.
SCOUT_REFUSALS = Counter(
    "tools_hub_scout_refusals_total",
    "Anonymous Epitope Scout requests refused, by reason and route.",
    ["reason", "route"],
)

IDEMPOTENCY_OUTCOMES = Counter(
    "tools_hub_idempotency_outcomes_total",
    "Outcome of the idempotency middleware per request.",
    ["outcome"],  # claimed|replay|in_flight|open|unavailable
)


# ---------------------------------------------------------------------------
# Endpoint wiring
# ---------------------------------------------------------------------------


def _metrics_token_ok() -> bool:
    """Is this request carrying the ``METRICS_TOKEN`` bearer?

    Deny by default: an unset or empty ``METRICS_TOKEN`` refuses everyone,
    including a caller who presents an empty bearer. ``X-Forwarded-For`` plays
    no part here at all — see the module docstring for why the address-based
    gate this replaced could not be made to work.
    """
    expected = os.environ.get("METRICS_TOKEN", "").strip()
    if not expected:
        return False
    header = request.headers.get("Authorization", "")
    # Case-SENSITIVE on purpose, though RFC 7235 says the scheme is not: the
    # only consumer is our own workflow, and every deviation here fails closed
    # with a 403 rather than opening anything.
    if not header.startswith("Bearer "):
        return False
    presented = header.removeprefix("Bearer ").strip()
    try:
        # BYTES, not str: hmac.compare_digest raises TypeError on a non-ASCII
        # str, and a malformed token must 403 rather than 500. surrogateescape
        # round-trips whatever os.environ handed us (POSIX env vars are bytes)
        # without raising; header values arrive latin-1-decoded, so they always
        # encode cleanly.
        return hmac.compare_digest(
            presented.encode("utf-8", "surrogateescape"),
            expected.encode("utf-8", "surrogateescape"),
        )
    except (TypeError, ValueError, UnicodeError):
        return False


def _trusted_proxy_hops() -> int:
    """How many trailing X-Forwarded-For entries were written by OUR proxies.

    Change ``TRUSTED_PROXY_HOPS`` if the edge topology changes — e.g. putting
    Cloudflare in front of Railway makes it 2. Set it to 0 to ignore the
    header entirely and trust only the socket peer (no proxy in front).
    """
    try:
        return max(0, int(os.environ.get("TRUSTED_PROXY_HOPS", "") or 1))
    except (TypeError, ValueError):
        return 1


# Exactly the headers /debug/client-ip may echo. An ALLOWLIST, deliberately,
# not a prefix match. "x-forwarded" as a prefix also matches
# X-Forwarded-Client-Cert (Envoy/Istio: the full client PEM plus a SPIFFE
# identity URI) and oauth2-proxy's X-Forwarded-Access-Token / -User / -Email /
# -Groups, which carry live credentials and identity assertions. Railway emits
# none of those today, so the prefix form was latent rather than leaking -- but
# it becomes live the moment an ingress, service mesh or oauth2-proxy lands in
# front, and the endpoint's own response is printed into a PUBLIC Actions log.
# An allowlist fails safe as the topology changes; a prefix match fails open.
ECHOABLE_FORWARDING_HEADERS = frozenset({
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-real-ip",
    "forwarded",
    "cf-connecting-ip",
    "true-client-ip",
})


def _client_ip() -> str:
    """Best-effort resolution of the caller's IP, safe to use as a gate key.

    Railway puts its edge in front of the app, so the direct socket peer is
    the edge and X-Forwarded-For carries the real client. The header is
    ordered ``client, proxy1, ..., proxyN``: everything a proxy did not
    write itself is attacker-controlled, so we count hops from the RIGHT and
    take the entry our own outermost trusted proxy contributed. Reading the
    LEFTmost entry — as this did until 2026-08-18 — lets any caller choose
    its own identity by sending ``X-Forwarded-For: <anything>``, which
    nullifies every per-IP control keyed off this function.

    At the default one hop, prefers ``X-Real-Ip``, which Railway's edge sets
    from the socket it actually saw and rewrites on every request — measured,
    not assumed. The ``X-Forwarded-For`` hop arithmetic below is the fallback
    whenever that header is absent or is not a bare IP address, and is
    unchanged. Setting ``TRUSTED_PROXY_HOPS`` to anything but 1 selects the
    hop arithmetic outright, so the knob still means what its docstring says.

    With one trusted hop, a single-value header returns that value whether
    the edge *appends* or *overwrites*, so the fallback is correct under
    both semantics. ``TRUSTED_PROXY_HOPS`` is the knob if an untrusted hop
    ever sits between the client and the edge.

    Falls back to the socket peer when the header is absent or unusable.
    """
    hops = _trusted_proxy_hops()
    if hops:
        # X-Real-Ip FIRST, because it is the only value here measured to be
        # both edge-written and unforgeable. On 2026-08-24 a live probe sent
        # forged X-Real-Ip, X-Forwarded-For, X-Forwarded-Host and
        # X-Forwarded-Proto: Railway's edge discarded every one and rewrote
        # X-Forwarded-For as `<real client>, <internal proxy>` with
        # X-Real-Ip set to the same real client.
        #
        # This is what makes the per-IP limiter work at all. The trailing
        # internal hop ROTATES across a pool (.101/.102/.104 within one probe
        # run), so counting one hop from the right keyed on a different value
        # nearly every request and the tier never refused anyone. See
        # docs/MEASUREMENT-2026-08-24-per-ip-key-is-not-stable.md.
        #
        # Preferred over raising TRUSTED_PROXY_HOPS to 2, and the honest
        # reason is NARROW: X-Real-Ip survives a change in the NUMBER of
        # internal hops, which a hop index does not -- add a second internal
        # hop and hops=2 silently starts reading the wrong entry.
        #
        # It is NOT that this "cannot fail open". Both options depend on the
        # header they read continuing to arrive. If X-Real-Ip stops being set,
        # control falls through to the hop arithmetic below, keys on the
        # rotating hop again, and the tier goes silently inert exactly as it
        # was before this fix. NOTHING ALARMS ON THAT. An earlier version of
        # this comment claimed the asymmetry ran the other way; it does not.
        # Gated on hops because TRUSTED_PROXY_HOPS=0 means "no proxy in front,
        # trust only the socket peer", and then no header is trustworthy.
        # hops == 1 ONLY. Any other value means the operator is telling us the
        # topology is not the measured one -- another proxy in front, per
        # _trusted_proxy_hops' own docstring -- and then X-Real-Ip is whatever
        # THAT proxy's egress looked like to Railway, which would collapse
        # every visitor behind it onto one key. The knob has to keep working.
        #
        # Validated as a bare IP, not merely non-blank. Duplicate X-Real-Ip
        # headers MERGE into one comma-joined WSGI value, so "a single header
        # has no index to shift" is only true once a comma-joined value is
        # rejected. ip_address() rejects that and the rest of the junk class
        # in one line: hostnames, host:port, empty, whitespace, 2000-char
        # strings. Anything it rejects falls through to the chain below rather
        # than becoming a limiter key verbatim.
        real_ip = request.headers.get("X-Real-Ip", "").strip()
        if real_ip and hops == 1:
            try:
                ipaddress.ip_address(real_ip)
            except ValueError:
                pass
            else:
                return real_ip
        chain = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",")]
        chain = [p for p in chain if p]
        if chain:
            # Clamped so a chain SHORTER than the configured hop count yields
            # the leftmost entry rather than raising.
            return chain[max(0, len(chain) - hops)]
    return request.remote_addr or ""


def _readiness_probe() -> tuple[bool, str]:
    """Cheap liveness-of-dependencies check for /healthz.

    Returns (ok, detail). OK means Supabase and Stripe secret key presence
    — we do NOT hit Stripe's API, only confirm the env var is populated,
    since stripe.com being up is Stripe's problem not ours.
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return False, "supabase_unavailable"
    try:
        # Cheapest possible Supabase round-trip: select 0 rows from a known
        # table. A 200 back with empty data proves reachability + auth.
        client.table("user_tier").select("user_id").limit(1).execute()
    except Exception:
        logger.warning("Readiness probe: Supabase query failed", exc_info=True)
        return False, "supabase_query_failed"

    if not os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip():
        return False, "stripe_webhook_secret_missing"

    return True, "ok"


def _render_metrics() -> Response:
    """Render the Prometheus text exposition from the active registry."""
    if not PROMETHEUS_AVAILABLE:
        return Response(
            "prometheus_client not installed",
            status=503,
            content_type="text/plain",
        )

    # If the multiproc dir is configured, aggregate across gunicorn workers.
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "").strip()
    if multiproc_dir:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        # Default process-local registry. Single-worker dev is fine.
        from prometheus_client import REGISTRY  # noqa: PLC0415

        registry = REGISTRY

    payload = generate_latest(registry)
    return Response(payload, content_type=CONTENT_TYPE_LATEST)


def register_metrics(flask_app: Flask) -> None:
    """Attach /metrics + /healthz to the app and install a latency hook."""

    @flask_app.before_request
    def _mark_request_start() -> None:
        g._tools_hub_request_start = time.monotonic()

    @flask_app.after_request
    def _observe_request(response: Response) -> Response:
        start = getattr(g, "_tools_hub_request_start", None)
        if start is not None:
            elapsed = time.monotonic() - start
            route = request.endpoint or "unknown"
            status_class = f"{response.status_code // 100}xx"
            REQUESTS_TOTAL.labels(route=route, status_class=status_class).inc()
            REQUEST_LATENCY.labels(route=route).observe(elapsed)
        return response

    @flask_app.route("/healthz", methods=["GET"])
    def healthz() -> Any:
        """Readiness probe — 200 only if Supabase + Stripe secret both present."""
        ok, detail = _readiness_probe()
        payload = {"status": "ok" if ok else "degraded", "detail": detail}
        return jsonify(payload), (200 if ok else 503)

    @flask_app.route("/metrics", methods=["GET"])
    def metrics_endpoint() -> Any:
        if not _metrics_token_ok():
            return Response("forbidden", status=403, content_type="text/plain")
        return _render_metrics()

    @flask_app.route("/debug/client-ip", methods=["GET"])
    def client_ip_echo() -> Any:
        """Echo what this process actually receives about the caller's address.

        The anonymous per-IP limiter keys on ``_client_ip()``. Production
        measurement on 2026-08-24 showed that key varying between identical
        requests, so the tier never refused anyone (FIXED the same day by
        preferring ``X-Real-Ip``; this endpoint is what established why) -- but nothing in the app
        could say what it was resolving to or why, because the resolution is
        pure inference from headers we never logged. See
        docs/MEASUREMENT-2026-08-24-per-ip-key-is-not-stable.md.

        Token-gated with the same bearer as ``/metrics``, deliberately. The
        forwarding chain and the edge's own address are infrastructure detail,
        and handing an anonymous caller the exact shape of the header the
        limiter trusts is a gift to anyone trying to forge it.

        Echoes only ``ECHOABLE_FORWARDING_HEADERS``, an exact allowlist. See
        that constant for why this is not a prefix match, and note that the
        response is republished into a PUBLIC GitHub Actions log by
        .github/workflows/client-ip-probe.yml, so the token gate is not the
        last line of defence here -- the allowlist is.
        """
        if not _metrics_token_ok():
            return Response("forbidden", status=403, content_type="text/plain")
        forwarding = {
            name: value
            for name, value in request.headers.items()
            if name.lower() in ECHOABLE_FORWARDING_HEADERS
        }
        return jsonify({
            "client_ip": _client_ip(),
            "remote_addr": request.remote_addr or "",
            "trusted_proxy_hops": _trusted_proxy_hops(),
            "forwarding_headers": forwarding,
        })


# ---------------------------------------------------------------------------
# Decorator helpers for external modules
# ---------------------------------------------------------------------------


def observe_credits_spent(tool: str, amount: int) -> None:
    """Hook for shared.credits.record_spend to publish the counter."""
    try:
        CREDITS_SPENT.labels(tool=tool).inc(amount)
    except Exception:  # pragma: no cover — metrics must never break app
        logger.debug("credits_spent metric increment failed", exc_info=True)


def observe_credits_granted(tier: str, event: str, amount: int) -> None:
    try:
        CREDITS_GRANTED.labels(tier=tier, event=event).inc(amount)
    except Exception:  # pragma: no cover
        logger.debug("credits_granted metric increment failed", exc_info=True)


def observe_stripe_event(event_type: str, outcome: str) -> None:
    try:
        STRIPE_EVENTS.labels(event_type=event_type, outcome=outcome).inc()
    except Exception:  # pragma: no cover
        logger.debug("stripe_events metric increment failed", exc_info=True)


def observe_scout_refusal(reason: str) -> None:
    """Count one anonymous Epitope Scout refusal. See ``SCOUT_REFUSALS``.

    The route label is resolved here, from ``request.endpoint``, the same way
    ``_observe_request`` does it — so the six call sites stay one-liners.

    Safe to call OUTSIDE a request context, where it degrades to ``unknown``
    rather than raising and losing the sample. **No current site needs that**:
    five sit in view bodies and the sixth sits in a generator that IS wrapped
    in ``stream_with_context``, so all six resolve a real endpoint name. The
    guard is there for the SEVENTH — an increment dropped into an unwrapped
    streamed generator runs after Flask has popped the request context, and
    ``request.endpoint`` raises there. Cheap defence-in-depth for a function
    that must never raise into a refusal path.
    """
    try:
        route = (request.endpoint or "unknown") if has_request_context() else "unknown"
        SCOUT_REFUSALS.labels(reason=reason, route=route).inc()
    except Exception:  # pragma: no cover — metrics must never break app
        logger.debug("scout_refusals metric increment failed", exc_info=True)


def observe_idempotency_outcome(outcome: str) -> None:
    try:
        IDEMPOTENCY_OUTCOMES.labels(outcome=outcome).inc()
    except Exception:  # pragma: no cover
        logger.debug("idempotency_outcomes metric increment failed", exc_info=True)
