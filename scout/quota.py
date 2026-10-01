"""Scout run ledger — writes ``public.scout_runs`` for analytics.

This module used to be the Scout paywall: a 3-run-per-30-days cap on
signed-in free-tier users. The cap was removed on 2026-09-30 because it
inverted the funnel — anonymous visitors were never metered by it (the
decorator passed through when ``session["user_email"]`` was absent), so
signing up made the product strictly worse than staying anonymous.

Epitope Scout is now metered for everyone, signed in or not, by the
in-process rate limiter in ``scout.ratelimit`` (see its module docstring
for the live numbers). There is no per-user run cap and no refusal that
depends on who you are.

What is left here is the ledger write. ``record_scout_run`` still inserts
one row per completed analysis into ``public.scout_runs``; those rows are
retained for analytics and provenance (docs/PII-RETENTION.md:50). Nothing
reads them to refuse a request — the only remaining caller is the end of
``/scout/analyze`` (scout/routes.py, search ``record_scout_run``).

Implementation
--------------
Writes go through the service-role key so Row-Level Security does not
block the insert. If the service-role key is not configured the write is
skipped and a warning is logged; a ledger-write failure never affects the
analysis response (``record_scout_run`` returns False and never raises).

Environment
-----------
SUPABASE_URL                  — Supabase project URL (already used by auth)
SUPABASE_SERVICE_ROLE_KEY     — service-role key. Without it no rows are
                                written and a warning is logged.

Usage
-----
    from scout.quota import record_scout_run

    record_scout_run(session["user_email"], metadata={"chain": chain_id})
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Supabase clients
# ---------------------------------------------------------------------------


def _get_service_client():
    """Return a Supabase client authenticated with the service-role key.

    Returns None when either the URL or the service-role key is missing,
    or if the supabase package cannot be imported. Both callers treat None
    as "skip the work" — ``_resolve_user_id`` returns None and
    ``record_scout_run`` skips the ledger write; nothing here can refuse a
    request.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client  # noqa: PLC0415
        from shared.supabase_client import _client_options  # noqa: PLC0415

        # Bound the PostgREST/Storage timeout (and inherit the HTTP/1.1 patch)
        # so a stalled scout read cannot pin a worker for the 120s library
        # default — the 2026-06-10 worker-wedge class. Matches shared.credits.
        return create_client(url, key, options=_client_options())
    except Exception:
        logger.warning("Could not create Supabase service client.", exc_info=True)
        return None


def _resolve_user_id(email: str) -> Optional[str]:
    """Resolve the Supabase auth user id for the given email.

    Returns None if the user cannot be found or the service client is not
    configured. List-and-filter is fine at Wave-0 cohort size; move to a
    stored function when user counts climb.
    """
    client = _get_service_client()
    if client is None:
        return None
    try:
        from shared.credits import list_all_auth_users  # noqa: PLC0415

        for user in list_all_auth_users(client):
            candidate = getattr(user, "email", None) or (
                user.get("email") if isinstance(user, dict) else None
            )
            if candidate and candidate.lower() == email.lower():
                return getattr(user, "id", None) or user.get("id")
    except Exception:
        logger.warning("Could not resolve Supabase user id.", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def record_scout_run(
    email: str,
    *,
    result_hash: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Insert a completed-run row into ``public.scout_runs``.

    Returns True on successful insert, False if Supabase is unreachable
    or the insert fails. Never raises — a ledger-write failure must not
    take down a successful analysis response.
    """
    client = _get_service_client()
    if client is None:
        logger.warning(
            "record_scout_run: service client unavailable; run not logged."
        )
        return False
    user_id = _resolve_user_id(email)
    if not user_id:
        logger.warning(
            "record_scout_run: could not resolve user id for %s", email
        )
        return False
    row = {
        "user_id": user_id,
        "result_hash": result_hash,
        "metadata": metadata or {},
    }
    try:
        client.table("scout_runs").insert(row).execute()
        return True
    except Exception:
        logger.error("Failed to insert scout_runs row.", exc_info=True)
        return False
