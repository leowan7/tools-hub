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
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


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
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return
    try:
        client.table("user_events").insert({
            "user_id": user_id,
            "session_id": (session_id or "")[:64] or None,
            "event_type": event_type[:64],
            "path": (path or "")[:500] or None,
            "props": props or {},
            "ip": ip,
            "user_agent": (user_agent or "")[:500] or None,
        }).execute()
    except Exception:
        logger.warning(
            "Failed to log user_event (type=%s user=%s)",
            event_type, user_id, exc_info=True,
        )
