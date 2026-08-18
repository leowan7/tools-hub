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

ponytail: in-process fixed-window counters. With N gunicorn workers the
effective limit is N x the configured number, and it resets on deploy.
That is the correct trade for a free tier whose worst case is wasted CPU
on one box — move the counters into Redis or a Supabase table if Scout
ever needs a limit that holds across the fleet.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from functools import wraps

from flask import jsonify, session

# Reuse the app's existing forwarded-header-aware client IP resolution
# rather than re-deriving it (Railway's edge is the socket peer).
from shared.metrics import _client_ip

# (bucket, key) -> (window_expires_at_monotonic, hits_in_window)
_WINDOWS: dict[tuple[str, str], tuple[float, int]] = {}
_LOCK = threading.Lock()

# Hard bound on the counter table so a spray of unique source addresses
# cannot grow it without limit. Expired entries are dropped first; if the
# table is still full the whole thing is reset (fail OPEN — a rate limiter
# is not worth an out-of-memory kill).
_MAX_KEYS = 20_000


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
                _WINDOWS.clear()

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
# anonymous scoring pipelines run at once. They are different failure modes:
# the pipeline is CPU-bound (freesasa + numpy), so under gevent it does not
# yield while it runs, and a handful of simultaneous anonymous runs will slow
# every signed-in request sharing the worker.

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
