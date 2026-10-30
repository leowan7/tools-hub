"""Site-wide "service is disrupted" banner.

The banner exists for outages where the database hangs (2026-10-30: /health
200, /readyz 503 after ~30 s). Rendering it must therefore never wait on the
database: ``banner_message()`` only reads a process-local flag, and the flag
is refreshed by a daemon thread that does the same read as /readyz with a
short timeout, at most once per ``CHECK_INTERVAL_S``.

Each gunicorn worker holds its own flag and runs its own check.

``STATUS_BANNER`` (env) forces a banner with that text, for planned or
partial incidents, without a deploy.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 30.0
CHECK_TIMEOUT_S = 5.0

DISRUPTION_MESSAGE = (
    "Service disruption: sign-in, wallet and run results may be slow or "
    "unavailable. We are working on it."
)

_lock = threading.Lock()
_db_down = False
_last_started = float("-inf")
_in_flight = False


def _db_ok() -> bool:
    """The /readyz read (user_events, one id) straight against PostgREST."""
    import httpx  # noqa: PLC0415

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.environ.get("SUPABASE_KEY", "").strip()
        or os.environ.get("SUPABASE_ANON_KEY", "").strip()
    )
    resp = httpx.get(
        f"{url.rstrip('/')}/rest/v1/user_events",
        params={"select": "id", "limit": "1"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=CHECK_TIMEOUT_S,
    )
    return resp.status_code == 200


def refresh() -> None:
    """Run one check and store the result. Blocking; the thread target."""
    global _db_down, _in_flight
    try:
        ok = _db_ok()
    except Exception as exc:  # noqa: BLE001 - any failure means down
        logger.warning("status banner check failed: %s", exc)
        ok = False
    with _lock:
        _db_down = not ok
        _in_flight = False


def _maybe_start_check() -> None:
    global _last_started, _in_flight
    if not os.environ.get("SUPABASE_URL", "").strip():
        return  # local dev / tests: nothing to probe
    now = time.monotonic()
    with _lock:
        if _in_flight or now - _last_started < CHECK_INTERVAL_S:
            return
        _in_flight = True
        _last_started = now
    threading.Thread(target=refresh, name="status-banner", daemon=True).start()


def banner_message() -> str | None:
    """Text for the banner, or None. Never touches the database inline."""
    forced = os.environ.get("STATUS_BANNER", "").strip()
    if forced:
        return forced
    _maybe_start_check()
    return DISRUPTION_MESSAGE if _db_down else None
