"""Audit-log writers for signup rejections and user events.

Two append-only Supabase tables backed this module:

  public.signup_rejections   Inbound /signup attempts blocked before
                             reaching Supabase Auth — honeypot hits,
                             bad timing, disposable domains, missing
                             purpose notes.

  public.user_events         Behavioural events (page views, tool form
                             opens/submits, pricing views, logins,
                             signup completion, credit exhaustion).
                             Anonymous events are allowed (user_id NULL,
                             session_id only) so the daily digest can
                             stitch an anon pricing view to the signup
                             that follows.

Writes are best-effort. Logging an event must not break the request it
records — every helper catches and warns.

D3 (growth funnel)
------------------
On top of the Supabase audit tables, this module also exposes ``emit()``,
a fire-and-forget POST to PostHog's server-side capture endpoint. The
PostHog stream powers the funnel dashboard (visit -> signup -> first_job
-> second_job -> topup). Use the ``EVENTS`` namespace for the canonical
event names so the dashboard and the call sites agree on spelling.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)


# Behavioural-event inserts run OFF the request thread. A synchronous
# Supabase insert in the request path once pinned the (single) gunicorn
# worker for the full client timeout when a pooled connection stalled,
# taking the whole site down (incident 2026-06-10, POST /api/track ->
# log_event). Analytics must never be able to block a user request.
#
# Each insert runs in its own daemon thread — daemon so a stalled insert
# never holds up worker shutdown / graceful restart (a non-daemon thread
# would be joined at interpreter exit). A bounded semaphore caps how many
# inserts may be in flight at once; when a Supabase stall fills every slot,
# further events are SHED rather than queued, so a downstream stall can
# never accumulate unbounded threads or memory. The bounded client timeout
# (see shared.supabase_client) drains stuck slots within ~30s.
_EVENT_INFLIGHT = threading.BoundedSemaphore(4)


# Allowed reason codes for signup_rejections (validated client-side
# only; the table accepts any string so we can extend without a
# migration).
SIGNUP_REJECTION_REASONS: frozenset[str] = frozenset({
    "honeypot",
    "timing",
    "disposable",
    "purpose_missing",
    "invalid",
    "rate_limited",
})

# Allowed event types. Same kind of soft constraint — the table accepts
# any string; this set documents what the code path emits today.
USER_EVENT_TYPES: frozenset[str] = frozenset({
    "page_view",
    "tool_form_open",
    "tool_form_submit",
    "pricing_view",
    "login",
    "signup_completed",
    "signup_form_started",
    "signup_failed",
    "credit_exhausted",
})


def log_signup_rejection(
    *,
    email: str,
    reason: str,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """Append a row to public.signup_rejections.

    Never raises. On failure, logs at WARNING and returns. The caller
    has already rejected the signup before reaching this; failing to
    persist the audit row should not cascade into a 500.
    """
    if not email or not reason:
        return
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return
    try:
        client.table("signup_rejections").insert({
            "email": email.strip().lower()[:320],
            "reason": reason,
            "ip": ip,
            "user_agent": (user_agent or "")[:500] or None,
        }).execute()
    except Exception:
        logger.warning(
            "Failed to log signup rejection (email=%s reason=%s)",
            email, reason, exc_info=True,
        )


def log_event(
    *,
    event_type: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    path: Optional[str] = None,
    props: Optional[dict] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """Append a row to public.user_events.

    Either user_id or session_id should be set — anonymous events are
    fine as long as we can stitch them later via session_id. Never
    raises.
    """
    if not event_type:
        return
    if user_id is None and not session_id:
        # Without either an authenticated id or a client session id,
        # the event is unattributable. Drop to keep the table clean.
        return

    payload = {
        "user_id": user_id,
        "session_id": (session_id or "")[:64] or None,
        "event_type": event_type[:64],
        "path": (path or "")[:500] or None,
        "props": props or {},
        "ip": ip,
        "user_agent": (user_agent or "")[:500] or None,
    }

    if not _EVENT_INFLIGHT.acquire(blocking=False):
        # Every insert slot is busy — a Supabase stall is in progress.
        # Shed this event rather than queue it: analytics is best-effort
        # and must never accumulate unbounded work behind a downstream
        # stall.
        return

    def _write() -> None:
        try:
            from shared.credits import get_service_client  # noqa: PLC0415

            client = get_service_client()
            if client is None:
                return
            client.table("user_events").insert(payload).execute()
        except Exception:
            logger.warning(
                "Failed to log user_event (type=%s user=%s)",
                event_type, user_id, exc_info=True,
            )
        finally:
            _EVENT_INFLIGHT.release()

    try:
        threading.Thread(
            target=_write, name="user_event", daemon=True
        ).start()
    except Exception:
        # Could not start the worker thread (e.g. OS resource limit).
        # Release the slot we just took so it is not leaked permanently,
        # and drop the event.
        _EVENT_INFLIGHT.release()
        logger.warning("Could not start user_event thread", exc_info=True)


# ---------------------------------------------------------------------------
# D3: PostHog server-side ingest (funnel dashboard)
# ---------------------------------------------------------------------------


class EVENTS:
    """Canonical event names fired by the funnel instrumentation.

    Importers should reference these constants (e.g.
    ``emit(EVENTS.FIRST_JOB_SUBMITTED, ...)``) rather than typing the
    string literal so the dashboard query and the call sites cannot
    drift.
    """

    SIGNUP_COMPLETE = "signup_complete"
    TOPUP_COMPLETE = "topup_complete"
    FIRST_JOB_SUBMITTED = "first_job_submitted"
    FIRST_JOB_COMPLETED = "first_job_completed"
    NTH_JOB_SUBMITTED = "nth_job_submitted"
    NTH_JOB_COMPLETED = "nth_job_completed"
    CROSS_TOOL_HANDOFF_CLICKED = "cross_tool_handoff_clicked"
    SHARE_CLICKED = "share_clicked"
    REFOLD_SPAWNED = "refold_spawned"
    RESAMPLE_LOADED = "resample_loaded"


# PostHog server-side capture endpoint. The legacy /capture/ path is
# stable and accepts the same JSON shape as the v3 ingest path; we use
# the legacy path because it works against both EU and US clouds without
# version pinning.
_POSTHOG_CAPTURE_PATH = "/capture/"

# Posting a single event must never block the request thread; PostHog
# is best-effort telemetry. Two seconds is the documented soft timeout
# their own JS client uses.
_POSTHOG_TIMEOUT_S = 2


def _posthog_api_key() -> str:
    """Resolve the PostHog project API key from env.

    Reads ``PUBLIC_POSTHOG_KEY`` first (the funnel-dashboard convention)
    and falls back to ``POSTHOG_KEY`` (the existing env var that gates
    the client-side snippet in ``templates/base.html``). Returns an
    empty string when neither is set, which turns ``emit()`` into a
    no-op.
    """
    return (
        os.environ.get("PUBLIC_POSTHOG_KEY", "").strip()
        or os.environ.get("POSTHOG_KEY", "").strip()
    )


def _posthog_host() -> str:
    return (
        os.environ.get("POSTHOG_HOST", "").strip()
        or "https://us.i.posthog.com"
    ).rstrip("/")


def emit(
    event_name: str,
    *,
    user_id: Optional[str],
    properties: Optional[dict] = None,
) -> None:
    """Fire a server-side PostHog capture for ``event_name``.

    No-op when ``PUBLIC_POSTHOG_KEY`` / ``POSTHOG_KEY`` is unset, so
    dev and staging do not pollute production funnel data. Every
    exception is swallowed and logged at WARNING so that emitting an
    event never crashes the request it records. The HTTP call uses a
    2 second timeout so a slow PostHog responder cannot stall the
    response.
    """
    if not event_name:
        return
    api_key = _posthog_api_key()
    if not api_key:
        return
    # PostHog requires a distinct_id on every event. For server-fired
    # events on anonymous traffic we fall back to a stable sentinel so
    # the row still lands and gets stitched downstream.
    distinct_id = user_id or "anonymous"
    payload = {
        "api_key": api_key,
        "event": event_name,
        "distinct_id": distinct_id,
        "properties": dict(properties or {}),
    }
    url = _posthog_host() + _POSTHOG_CAPTURE_PATH
    try:
        import requests  # noqa: PLC0415

        requests.post(url, json=payload, timeout=_POSTHOG_TIMEOUT_S)
    except Exception:
        logger.warning(
            "PostHog emit failed (event=%s user=%s)",
            event_name, user_id, exc_info=True,
        )
