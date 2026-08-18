"""Per-IP fixed-window rate limiting for the anonymous Epitope Scout flow.

Scout's landing, intake (upload / fetch-pdb / example) and analysis routes
are reachable without an account so a first-time visitor can decide their
hotspot residues before signing up. That makes them the only unauthenticated
*compute* + *upload* surface in the app, so they need their own meter:
signed-in callers are metered by ``scout.quota`` (3 runs / 30 days on the
free tier), anonymous callers are metered here.

Only anonymous requests are limited. A signed-in user is already capped by
the Supabase-backed quota, and keying a shared lab's NAT address into the
same bucket would punish paying users for their neighbours.

What the limits ACTUALLY are, fleet-wide
---------------------------------------

The counters live in process memory, so the numbers configured in
``scout.routes`` are per-worker, not per-fleet. Gunicorn runs
``WEB_CONCURRENCY`` workers (default 2, see ``gunicorn.conf.py``) and a
caller's requests land on whichever worker is free, so the real ceiling is
``workers x limit``. With the shipped defaults that is:

- intake:  10/worker  -> **20 per 10 min per IP** across a 2-worker fleet
- analyze: 10/worker  -> **20 per 10 min per IP** across a 2-worker fleet

and it goes up proportionally if ``WEB_CONCURRENCY`` is raised. The counters
also reset on every deploy and on any worker recycle, so a caller who is
limited can be un-limited by a deploy landing mid-window. Quote the doubled
numbers, not the configured ones, when reasoning about abuse cost.

ponytail: in-process fixed-window counters, deliberately. The worst case
this guards is wasted CPU on one box, which does not justify a Redis
dependency — move the counters into Redis or a Supabase table if Scout ever
needs a limit that holds exactly across the fleet and survives deploys.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from functools import wraps

from flask import jsonify, session

# Reuse the app's existing forwarded-header-aware client IP resolution rather
# than re-deriving it. That function counts X-Forwarded-For hops from the
# RIGHT (TRUSTED_PROXY_HOPS, default 1) precisely so this key cannot be chosen
# by the caller — a leftmost read would let one header nullify every bucket
# below. Do not swap it for an inline header parse.
from shared.metrics import _client_ip

# (bucket, key) -> (window_expires_at_monotonic, hits_in_window)
_WINDOWS: dict[tuple[str, str], tuple[float, int]] = {}
_LOCK = threading.Lock()

# Hard bound on the counter table so a spray of unique source addresses
# cannot grow it without limit. Expired entries are dropped first; if the
# table is still full we evict the entries CLOSEST TO EXPIRING, never the
# whole table. Clearing it would hand an attacker the control itself: fill
# the table and every currently-limited caller is re-allowed, which is a
# cheaper attack than the one the limiter exists to stop.
#
# Eviction order is LOWEST HIT COUNT first, ties broken by soonest expiry.
# That ordering is the whole point, so do not "simplify" it to oldest-first:
# the spray keys an attacker uses to apply the pressure have exactly 1 hit
# each and cost nothing to forget, while the entry that must survive is the
# one already at or over its limit. Evicting by age instead would evict the
# limited caller FIRST — it has been in the table longest — which is the
# reset the attacker was fishing for.
_MAX_KEYS = 20_000

# Evict in batches so a sustained spray does not pay a full sort on every
# request once the table is full — one sort per batch instead of per call.
# ponytail: O(n log n) sort per batch. If this ever gets hot, a heap or an
# expiry-bucketed ring would make it O(log n), but at 20k keys the sort is
# sub-millisecond and runs once per 200 requests.
_EVICT_BATCH = 200


def hit(bucket: str, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Record one call against ``(bucket, key)``.

    Returns ``(allowed, retry_after_seconds)``. ``allowed`` is False once the
    number of calls inside the current window exceeds ``limit``.
    """
    now = time.monotonic()
    slot = (bucket, key or "unknown")
    with _LOCK:
        if len(_WINDOWS) >= _MAX_KEYS:
            for stale, (expires, _) in list(_WINDOWS.items()):
                if expires <= now:
                    del _WINDOWS[stale]
            if len(_WINDOWS) >= _MAX_KEYS:
                overflow = len(_WINDOWS) - _MAX_KEYS + _EVICT_BATCH
                # (hits, expires) — cheapest-to-forget first. See _MAX_KEYS.
                cheapest = sorted(
                    _WINDOWS.items(), key=lambda kv: (kv[1][1], kv[1][0])
                )[:overflow]
                for stale, _ in cheapest:
                    del _WINDOWS[stale]

        expires, hits = _WINDOWS.get(slot, (0.0, 0))
        if expires <= now:
            expires, hits = now + window_seconds, 0
        hits += 1
        _WINDOWS[slot] = (expires, hits)
        return hits <= limit, max(1, int(expires - now))


def reset() -> None:
    """Drop every counter. Test helper; not used by request handling."""
    global _INFLIGHT
    with _LOCK:
        _WINDOWS.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT = 0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
#
# The rate limiter bounds how OFTEN an address may ask; this bounds how many
# anonymous scoring pipelines run at once in ONE process.
#
# Be honest about what this buys today: nothing. Gunicorn sets no
# ``worker_class`` (see ``gunicorn.conf.py``), so the workers are **sync** —
# each serves exactly one request at a time. ``_INFLIGHT`` therefore never
# exceeds 1 per worker and the configured 4-slot cap is never reached. The
# real concurrency bound is the worker count itself, not this counter.
#
# It is kept because it is the correct guard the moment the deployment grows
# threads or an async worker class (``--worker-class gthread``/``gevent``),
# at which point several CPU-bound freesasa+numpy pipelines really can share
# one process and starve the signed-in requests next to them. Changing the
# worker class is a deployment decision, out of scope here.

_INFLIGHT = 0
_INFLIGHT_LOCK = threading.Lock()


@contextmanager
def anon_compute_slot(limit: int):
    """Yield True when an anonymous compute slot was taken, False when full.

    Signed-in callers always get True without consuming a slot — the paywall
    already bounds them, and a free-tier visitor must never be able to starve
    someone who is paying.

    Released in a ``finally`` so an exception, or a client that hangs up
    mid-stream (which closes a streaming generator), cannot leak the slot and
    wedge the pool at "full" forever.
    """
    global _INFLIGHT
    if session.get("user_email"):
        yield True
        return
    with _INFLIGHT_LOCK:
        if _INFLIGHT >= limit:
            yield False
            return
        _INFLIGHT += 1
    try:
        yield True
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT = max(0, _INFLIGHT - 1)


def inflight_anon_runs() -> int:
    """Current anonymous pipelines in flight in this process. For tests/debug."""
    with _INFLIGHT_LOCK:
        return _INFLIGHT


_OVER_LIMIT_MESSAGE = (
    "Too many Epitope Scout requests from this network. Wait a minute and "
    "try again, or sign in for a free account with a higher allowance."
)


def anon_rate_limit(bucket: str, *, limit: int, window_seconds: int, sse: bool = False):
    """Decorator: cap anonymous calls to this route at ``limit`` per window.

    Signed-in requests pass straight through (``scout.quota`` meters those).
    Over-limit anonymous requests get a JSON 429 with ``Retry-After`` — the
    Scout page's fetch handlers already render a non-2xx ``{"error": ...}``
    body, so no front-end change is needed.

    ``sse=True`` is for the EventSource endpoints: ``EventSource`` cannot
    read a 429 body, so those get a 200 ``text/event-stream`` carrying the
    same ``{"stage": "error"}`` event the route already emits for a missing
    job, which the page renders inline.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if session.get("user_email"):
                return f(*args, **kwargs)
            allowed, retry_after = hit(
                bucket, _client_ip(), limit=limit, window_seconds=window_seconds
            )
            if allowed:
                return f(*args, **kwargs)
            if sse:
                from flask import current_app  # noqa: PLC0415

                def _limited_stream():
                    yield "data: " + json.dumps(
                        {"stage": "error", "msg": _OVER_LIMIT_MESSAGE}
                    ) + "\n\n"

                return current_app.response_class(
                    _limited_stream(),
                    mimetype="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Retry-After": str(retry_after),
                    },
                )
            response = jsonify({
                "error": _OVER_LIMIT_MESSAGE,
                "retry_after": retry_after,
            })
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            return response

        return wrapped

    return decorator
