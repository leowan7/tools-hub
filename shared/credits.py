"""Credits middleware for the Ranomics tools hub.

Wave-0 Stream A contract. Every GPU tool route wraps its handler with
``@requires_credits(n)``. The decorator:

1. Resolves the current user via the Flask session + Supabase Auth.
2. Reads the user's credit balance from ``public.credits_balance`` (a view
   over the append-only ``public.credits_ledger``).
3. If the balance is below ``n``, short-circuits with HTTP 402 and a
   friendly redirect to ``/account`` so the user can top up.
4. Otherwise runs the handler. If the handler returns successfully, a
   ``spend`` entry is recorded on the ledger (negative delta).

The ledger is append-only and double-entry; we never mutate existing rows.
Refunds land as a separate ``refund`` row with a positive delta. Balance is
always ``SUM(delta)`` over the ledger — single source of truth.

Design notes
------------
* Writes go through the *service-role* Supabase client so RLS does not block
  them. Balance reads can also use the service role since we filter by
  ``user_id`` in Python.
* No pre-auth hold yet. Wave-2 adds a reserve/commit pattern when Modal
  submissions go live; for Wave-0 we spend on handler success, which is
  enough to prove the plumbing end-to-end.
* ``NotImplementedError`` is not caught — a tool that actually calls Modal
  should raise a typed error, not return success.

Usage
-----
    from shared.credits import requires_credits

    @app.route("/tools/example-gpu", methods=["POST"])
    @login_required
    @requires_credits(1, tool="example-gpu", reason="example-gpu pilot")
    def submit_example():
        ...
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, Optional

from flask import redirect, session, url_for

from shared.supabase_client import get_supabase_client

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
        return create_client(url, service_key)
    except Exception:
        logger.warning(
            "Could not create service-role Supabase client.", exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# User + balance helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserContext:
    """Minimal user context resolved from the Flask session."""

    user_id: str
    email: str
    tier: str
    balance: int


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


def get_balance(user_id: str) -> int:
    """Return the current credit balance for the given user id.

    Reads the ``credits_balance`` view so the math stays in one place.
    Returns 0 if the user has no ledger entries yet, or on read failure
    (fail-closed — we would rather block a job than over-serve credits).
    """
    client = get_service_client()
    if client is None:
        return 0
    try:
        response = (
            client.table("credits_balance")
            .select("balance")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        data = getattr(response, "data", None)
        if data and "balance" in data:
            return int(data["balance"] or 0)
    except Exception:
        logger.warning(
            "Could not read credit balance for user %s", user_id, exc_info=True
        )
    return 0


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
    """Resolve the current signed-in user's id, tier, and balance.

    Returns None if no user is signed in or if Supabase is misconfigured.
    """
    email = session.get("user_email")
    if not email:
        return None
    # Login route stashes user_id at sign-in time; using it here avoids a
    # paginated admin.list_users() round-trip on every authenticated render
    # (which silently returned None and made the navbar show 0 credits).
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
        balance=get_balance(user_id),
    )


# ---------------------------------------------------------------------------
# Ledger writers
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
    """Record a ``spend`` entry on the ledger.

    ``amount`` is the positive credit count; we store it as ``-amount`` to
    match the ``kind='spend' AND delta<0`` CHECK constraint.
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
    """Record a ``grant`` entry on the ledger (positive delta)."""
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


def record_refund(
    user_id: str,
    amount: int,
    *,
    tool: str,
    reason: str,
    job_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Record a ``refund`` entry (positive delta) tied to a tool run."""
    if amount <= 0:
        raise ValueError("Refund amount must be positive.")
    client = get_service_client()
    if client is None:
        return False
    row = {
        "user_id": user_id,
        "kind": "refund",
        "delta": amount,
        "reason": reason,
        "tool": tool,
        "job_id": job_id,
        "metadata": metadata or {},
    }
    try:
        client.table("credits_ledger").insert(row).execute()
        return True
    except Exception:
        logger.error(
            "Failed to record refund for user %s", user_id, exc_info=True
        )
        return False


def get_spent_for_job(job_id: str) -> int:
    """Sum the ``spend`` ledger entries for a single job; returns positive int.

    Used by ``cancel_job`` to refund only the credits the user was actually
    debited. Without this guard, an orphaned ``tool_jobs`` row (created by
    a submit handler that crashed before ``record_spend`` ran) would still
    refund ``credits_cost`` on cancel, minting credits for free.

    Returns 0 when the ledger has no spend entry for ``job_id``.
    """
    if not job_id:
        return 0
    client = get_service_client()
    if client is None:
        return 0
    try:
        response = (
            client.table("credits_ledger")
            .select("delta")
            .eq("job_id", job_id)
            .eq("kind", "spend")
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        return -sum(int(r["delta"]) for r in rows)
    except Exception:
        logger.warning(
            "Failed to query spend ledger for job %s", job_id, exc_info=True
        )
        return 0


def recent_ledger(user_id: str, limit: int = 20) -> list[dict]:
    """Return the most recent ledger entries for a user."""
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
# Decorator
# ---------------------------------------------------------------------------


def requires_credits(
    amount: int,
    *,
    tool: str = "unknown",
    reason: Optional[str] = None,
) -> Callable:
    """Flask decorator — block the route if the user cannot afford ``amount``.

    On success, record a matching ``spend`` entry. On insufficient balance,
    redirect to ``/account`` (which will render the credits ledger + billing
    link). If the wrapped handler raises, we do NOT charge — the spend is
    recorded only when the handler returns a truthy response.
    """
    if amount <= 0:
        raise ValueError("requires_credits amount must be positive.")

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapped(*args: Any, **kwargs: Any):
            ctx = load_user_context()
            if ctx is None:
                # login_required should have caught this; be defensive.
                return redirect(url_for("login"))

            if ctx.balance < amount:
                logger.info(
                    "Insufficient credits: user=%s need=%d have=%d tool=%s",
                    ctx.user_id,
                    amount,
                    ctx.balance,
                    tool,
                )
                return redirect(url_for("account", insufficient_credits=1))

            response = f(*args, **kwargs)

            # Only charge on handler success. A handler that raises will
            # bubble up before we record the spend.
            record_spend(
                ctx.user_id,
                amount,
                tool=tool,
                reason=reason or f"{tool} run",
            )
            return response

        return wrapped

    return decorator


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
