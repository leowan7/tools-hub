"""Site-wide "service is disrupted" banner, driven by UptimeRobot.

Apart from the STATUS_BANNER override below, the banner shows only while
UptimeRobot reports a watched monitor down (status 8 "seems down" or 9
"down"). Rendering never waits on the network:
``banner_message()`` reads a process-local flag, and a daemon thread
refreshes it from the UptimeRobot API at most once per ``CHECK_INTERVAL_S``.
Supabase is never touched, since the database is what is usually down.

Each gunicorn worker holds its own flag and runs its own poll.

Env:
  UPTIMEROBOT_READONLY_KEY  read-only (or monitor-specific) API key. Unset
                            means no poll and no banner.
  UPTIMEROBOT_MONITOR_IDS   optional, dash-separated ids to watch, e.g.
                            "800123-800124". Unset means whatever monitors
                            one getMonitors page returns for the key, so
                            set it (or use a monitor-specific key) on an
                            account with many monitors.
  STATUS_BANNER             forces a banner with that text, for planned or
                            partial incidents, without a code change. It is
                            read from the process environment, so it takes
                            effect when the service restarts with the value.

Any poll failure (UptimeRobot unreachable, bad key, bad reply) clears the
flag: no banner.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 60.0
CHECK_TIMEOUT_S = 3.0
_API_URL = "https://api.uptimerobot.com/v2/getMonitors"
_DOWN_STATUSES = {8, 9}

DISRUPTION_MESSAGE = (
    "Service disruption: sign-in, wallet and run results may be slow or "
    "unavailable. We are working on it."
)

_lock = threading.Lock()
_down = False
_last_started = float("-inf")
_in_flight = False


def _uptimerobot_down() -> bool:
    """True if any watched monitor is down. Raises on any API failure."""
    import httpx  # noqa: PLC0415

    data = {
        "api_key": os.environ.get("UPTIMEROBOT_READONLY_KEY", "").strip(),
        "format": "json",
    }
    ids = os.environ.get("UPTIMEROBOT_MONITOR_IDS", "").strip()
    if ids:
        data["monitors"] = ids
    resp = httpx.post(_API_URL, data=data, timeout=CHECK_TIMEOUT_S)
    resp.raise_for_status()
    body = resp.json()
    if body.get("stat") != "ok":
        raise ValueError(f"UptimeRobot stat={body.get('stat')!r}")
    return any(m.get("status") in _DOWN_STATUSES for m in body["monitors"])


def refresh() -> None:
    """Run one check and store the result. Blocking; the thread target."""
    global _down, _in_flight
    try:
        down = _uptimerobot_down()
    except Exception as exc:  # noqa: BLE001 - fail closed to no banner
        logger.warning("status banner poll failed: %s", exc)
        down = False
    with _lock:
        _down = down
        _in_flight = False


def _maybe_start_check() -> None:
    global _last_started, _in_flight
    if not os.environ.get("UPTIMEROBOT_READONLY_KEY", "").strip():
        return
    now = time.monotonic()
    with _lock:
        if _in_flight or now - _last_started < CHECK_INTERVAL_S:
            return
        _in_flight = True
        _last_started = now
    try:
        threading.Thread(
            target=refresh, name="status-banner", daemon=True
        ).start()
    except RuntimeError:
        logger.warning("status banner check thread did not start")
        with _lock:
            _in_flight = False


def banner_message() -> str | None:
    """Text for the banner, or None. Never touches the database inline."""
    forced = os.environ.get("STATUS_BANNER", "").strip()
    if forced:
        return forced
    _maybe_start_check()
    return DISRUPTION_MESSAGE if _down else None
