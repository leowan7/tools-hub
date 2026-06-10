"""Internal margin-accounting ledger for the Ranomics tools hub.

This module used to be the customer-facing credits middleware. After
the wallet pivot, the USD wallet (``shared.wallet``) is the sole money
path: every GPU job places a wallet hold and the wallet settles it on
completion. The credits ledger no longer gates anything user-visible.

What remains here is the **internal accounting ledger** consumed by
``shared.workspaces``: workspace activations write a ``grant`` row sized
to the Modal compute cap purchased, and per-job workspace charges write
a ``spend`` row, so the ledger continues to reflect total compute
purchased vs. consumed for margin reporting. Customers do not see any
of this — the wallet is what they top up and spend against.

Public surface still in use
---------------------------
* ``get_service_client`` — service-role Supabase client. Shared with
  ``shared.email``, ``shared.jobs``, ``shared.workspaces`` for any
  ledger / table read that needs to bypass RLS.
* ``UserContext`` + ``load_user_context`` — session-bound user resolution
  (id, email, tier). The customer-facing balance now lives in the wallet
  and is read separately.
* ``record_grant`` / ``record_spend`` — workspace internal-margin writes.
* ``recent_ledger`` — admin user-detail view.
* ``get_tier`` — tier-label read used by admin views and
  ``load_user_context``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from flask import session

from shared.supabase_client import _client_options, get_supabase_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Service-role client — used for ledger writes + balance reads. Distinct from
# the anon client auth.py uses. If SUPABASE_SERVICE_ROLE_KEY is absent we
# fall back to the standard client so local dev without a service key still
# boots; production MUST set the service-role key.
# ---------------------------------------------------------------------------


def get_service_client():
    """Return a Supabase client authenticated with the service-role key.

    Falls back to the anon client if the service-role key is not configured
    so local dev does not crash. In production, this key is mandatory.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not service_key:
        logger.warning(
            "SUPABASE_SERVICE_ROLE_KEY not set — falling back to anon "
            "client. Credits writes will fail under RLS in production."
        )
        return get_supabase_client()
    try:
        from supabase import create_client  # noqa: PLC0415
        return create_client(url, service_key, options=_client_options())
    except Exception:
        logger.warning(
            "Could not create service-role Supabase client.", exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# User context helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserContext:
    """Minimal user context resolved from the Flask session."""

    user_id: str
    email: str
    tier: str


def _resolve_user_id(email: str) -> Optional[str]:
    """Look up the Supabase auth user id for the given email.

    Returns None if the user cannot be found or the client is unavailable.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        # supabase-py v2: admin.list_users is paginated; filter client-side
        # since we expect small cohorts in Wave-0. Swap to a stored function
        # once user counts grow.
        page = client.auth.admin.list_users()
        users = getattr(page, "users", None) or page
        for user in users:
            candidate = getattr(user, "email", None) or (
                user.get("email") if isinstance(user, dict) else None
            )
            if candidate and candidate.lower() == email.lower():
                return getattr(user, "id", None) or user.get("id")
    except Exception:
        logger.warning("Could not resolve Supabase user id.", exc_info=True)
    return None


def get_tier(user_id: str) -> str:
    """Return the current tier label for the user ('free' if none)."""
    client = get_service_client()
    if client is None:
        return "free"
    try:
        response = (
            client.table("user_tier")
            .select("tier")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        data = getattr(response, "data", None)
        if data and data.get("tier"):
            return str(data["tier"])
    except Exception:
        logger.warning(
            "Could not read user tier for user %s", user_id, exc_info=True
        )
    return "free"


def load_user_context() -> Optional[UserContext]:
    """Resolve the current signed-in user's id, tier, and email.

    Returns None if no user is signed in or if Supabase is misconfigured.
    """
    email = session.get("user_email")
    if not email:
        return None
    # Login route stashes user_id at sign-in time; using it here avoids a
    # paginated admin.list_users() round-trip on every authenticated render.
    user_id = session.get("user_id") or _resolve_user_id(email)
    if not user_id:
        logger.warning(
            "load_user_context: no user_id for %s — context falls back to None.",
            email,
        )
        return None
    return UserContext(
        user_id=user_id,
        email=email,
        tier=get_tier(user_id),
    )


# ---------------------------------------------------------------------------
# Internal margin-accounting ledger writers (used by shared.workspaces)
# ---------------------------------------------------------------------------


def record_spend(
    user_id: str,
    amount: int,
    *,
    tool: str,
    reason: str,
    job_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Record a ``spend`` entry on the internal credits_ledger.

    ``amount`` is the positive credit count; stored as ``-amount`` to match
    the ``kind='spend' AND delta<0`` CHECK constraint. Used by
    ``shared.workspaces`` to track per-job compute consumed against a
    workspace's purchased Modal cap, for margin reporting.
    """
    if amount <= 0:
        raise ValueError("Spend amount must be positive.")
    client = get_service_client()
    if client is None:
        logger.error("Cannot record spend: Supabase service client missing.")
        return False
    row = {
        "user_id": user_id,
        "kind": "spend",
        "delta": -amount,
        "reason": reason,
        "tool": tool,
        "job_id": job_id,
        "metadata": metadata or {},
    }
    try:
        client.table("credits_ledger").insert(row).execute()
        _metric_credits_spent(tool, amount)
        return True
    except Exception:
        logger.error(
            "Failed to record spend for user %s", user_id, exc_info=True
        )
        return False


def record_grant(
    user_id: str,
    amount: int,
    *,
    reason: str,
    stripe_event_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Record a ``grant`` entry on the internal credits_ledger.

    Used by ``shared.workspaces`` on workspace activation to log the
    Modal compute cap purchased (1 credit per USD), for margin reporting.
    """
    if amount <= 0:
        raise ValueError("Grant amount must be positive.")
    client = get_service_client()
    if client is None:
        return False
    row = {
        "user_id": user_id,
        "kind": "grant",
        "delta": amount,
        "reason": reason,
        "stripe_event_id": stripe_event_id,
        "metadata": metadata or {},
    }
    try:
        client.table("credits_ledger").insert(row).execute()
        _metric_credits_granted(get_tier(user_id), reason, amount)
        return True
    except Exception:
        logger.error(
            "Failed to record grant for user %s", user_id, exc_info=True
        )
        return False


def recent_ledger(user_id: str, limit: int = 20) -> list[dict]:
    """Return the most recent ledger entries for a user. Admin view only."""
    client = get_service_client()
    if client is None:
        return []
    try:
        response = (
            client.table("credits_ledger")
            .select("created_at,kind,delta,reason,tool,job_id")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(getattr(response, "data", None) or [])
    except Exception:
        logger.warning(
            "Could not load recent ledger for user %s",
            user_id,
            exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# Metrics helpers — lazy-imported to avoid a shared.metrics → shared.credits
# circular import at module load. Safe to call from any ledger writer.
# ---------------------------------------------------------------------------


def _metric_credits_spent(tool: str, amount: int) -> None:
    try:
        from shared.metrics import observe_credits_spent  # noqa: PLC0415
        observe_credits_spent(tool, amount)
    except Exception:  # pragma: no cover — metrics must never break a write
        pass


def _metric_credits_granted(tier: str, event: str, amount: int) -> None:
    try:
        from shared.metrics import observe_credits_granted  # noqa: PLC0415
        observe_credits_granted(tier, event, amount)
    except Exception:  # pragma: no cover
        pass
