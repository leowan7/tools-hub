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
    /metrics       — Prometheus text exposition. IP-allowlisted via the
                     ``METRICS_ALLOWED_CIDR`` env var (comma-separated
                     CIDRs). Deny by default.

Why deny by default
    The Railway service is on the public internet; anyone on the web can
    hit /metrics if the endpoint is open. Prometheus scrape contents can
    leak traffic patterns, user counts, and error rates — low but
    non-zero signal for a motivated attacker. Requiring an explicit
    allowlist is the right posture; a misconfigured allowlist simply
    denies the scraper, which is visible in the scraper's own error
    state.

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

import ipaddress
import logging
import os
import time
from typing import Any, Callable

from flask import Flask, Response, g, jsonify, request

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

IDEMPOTENCY_OUTCOMES = Counter(
    "tools_hub_idempotency_outcomes_total",
    "Outcome of the idempotency middleware per request.",
    ["outcome"],  # claimed|replay|in_flight|open
)


# ---------------------------------------------------------------------------
# Endpoint wiring
# ---------------------------------------------------------------------------


def _allowlist_cidrs() -> list[ipaddress._BaseNetwork]:
    """Parse METRICS_ALLOWED_CIDR into a list of network objects."""
    raw = os.environ.get("METRICS_ALLOWED_CIDR", "").strip()
    if not raw:
        return []
    nets: list[ipaddress._BaseNetwork] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid METRICS_ALLOWED_CIDR entry: %s", entry)
    return nets


def _client_ip() -> str:
    """Best-effort resolution of the caller's IP.

    Railway puts its edge in front of the app so the direct socket peer
    is the edge, not the real client. Fall back to X-Forwarded-For first
    hop when present.
    """
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def _ip_allowed(allowlist: list[ipaddress._BaseNetwork]) -> bool:
    ip_str = _client_ip()
    if not ip_str or not allowlist:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in allowlist)


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

    allowlist = _allowlist_cidrs()

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
        if not _ip_allowed(allowlist):
            return Response("forbidden", status=403, content_type="text/plain")
        return _render_metrics()


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


def observe_idempotency_outcome(outcome: str) -> None:
    try:
        IDEMPOTENCY_OUTCOMES.labels(outcome=outcome).inc()
    except Exception:  # pragma: no cover
        logger.debug("idempotency_outcomes metric increment failed", exc_info=True)


def observe_scout_run() -> None:
    try:
        SCOUT_RUNS.inc()
    except Exception:  # pragma: no cover
        logger.debug("scout_runs metric increment failed", exc_info=True)
