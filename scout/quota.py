"""Scout paywall — per-user run quota for the free tier.

Wave 1 (Stream B). Converts Epitope Scout from free-forever to a capped
free tier that funnels signed-in free users into the tools-hub Scout Pro
subscription.

Policy
------
Anonymous (not signed in)   — not reachable: every analysis route is already
                              protected by ``@login_required``. Anon users
                              simply never get a run to meter.
Signed-in, tier='free'      — 3 completed runs per rolling 30-day window.
                              The 4th run is blocked and the user is sent to
                              ``/upgrade``.
Signed-in, tier='scout_pro',
'lab', 'lab_plus'           — unlimited. Policy bypass.

Implementation
--------------
Reads ``public.user_tier.tier`` and ``public.scout_run_count_30d.runs_last_30d``
from the tools-hub Supabase project (which Scout already shares for auth).
Writes go through the service-role key so Row-Level Security does not
block the insert. If the service-role key is not configured the decorator
fails OPEN — the existing "unlimited free" behaviour is preserved and a
warning is logged so the operator notices in Railway logs before a paying
customer is mis-charged. This is the correct failure direction for a
pre-revenue rollout.

Environment
-----------
SUPABASE_URL                  — Supabase project URL (already used by auth)
SUPABASE_SERVICE_ROLE_KEY     — service-role key. NEW for Wave 1. Must be
                                set in Railway before the paywall activates.

Usage
-----
    from scout.quota import (
        requires_scout_quota,
        record_scout_run,
        quota_status,
    )

    @flask_app.route("/analyze", methods=["POST"])
    @login_required
    @requires_scout_quota
    def analyze():
        ...
        record_scout_run(session["user_email"], metadata={"chain": chain_id})
        return jsonify({...})
"""

from __future__ import annotations

import hashlib
import logging
import os
from functools import wraps
from typing import Optional

from flask import jsonify, redirect, request, session, url_for

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

# Monthly cap for signed-in free-tier users. PRODUCT-PLAN.md pricing table
# row "Free: Scout x3/mo". Do not change without updating the pricing page
# and the marketing claim — this number is load-bearing for the funnel.
FREE_TIER_RUN_CAP = 3

# Tiers that bypass the paywall. Kept in sync with migration 0001's
# ``public.ranomics_tier`` enum.
UNLIMITED_TIERS = frozenset({"scout_pro", "lab", "lab_plus"})


# ---------------------------------------------------------------------------
# Supabase clients
# ---------------------------------------------------------------------------


def _get_service_client():
    """Return a Supabase client authenticated with the service-role key.

    Returns None when either the URL or the service-role key is missing,
    or if the supabase package cannot be imported. Callers MUST treat None
    as "fail open" so the decorator never locks users out due to a config
    mistake on our side.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client  # noqa: PLC0415
        return create_client(url, key)
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


def _get_tier(user_id: str) -> str:
    """Return the tier for the given user id, defaulting to 'free'."""
    client = _get_service_client()
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


def _count_runs_last_30d(user_id: str) -> int:
    """Return completed Scout runs for the user in the trailing 30 days.

    Reads the ``scout_run_count_30d`` view so the window definition lives
    in SQL and is consistent across the paywall, the account page, and
    any future admin reporting.
    """
    client = _get_service_client()
    if client is None:
        return 0
    try:
        response = (
            client.table("scout_run_count_30d")
            .select("runs_last_30d")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        data = getattr(response, "data", None)
        if data and data.get("runs_last_30d") is not None:
            return int(data["runs_last_30d"])
    except Exception:
        logger.warning(
            "Could not read scout run count for user %s", user_id, exc_info=True
        )
    return 0


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def quota_status(email: str) -> dict:
    """Return the current paywall status for the given user email.

    Returns a dict with:
        tier:             'free' / 'scout_pro' / 'lab' / 'lab_plus' / 'unknown'
        runs_used:        integer count in the trailing 30 days
        runs_cap:         integer cap for the user's tier (0 meaning unlimited)
        runs_remaining:   integer runs remaining before the cap kicks in
        unlimited:        bool — True for Pro tiers or when Supabase is down
        user_id:          resolved Supabase user id or None

    ``unlimited=True`` when Supabase is unreachable. This is the fail-open
    direction used by ``requires_scout_quota`` and by the ``index.html``
    banner so a misconfigured backend never locks users out.
    """
    user_id = _resolve_user_id(email)
    if not user_id:
        # Supabase unreachable or user id not found — fail open.
        return {
            "tier": "unknown",
            "runs_used": 0,
            "runs_cap": FREE_TIER_RUN_CAP,
            "runs_remaining": FREE_TIER_RUN_CAP,
            "unlimited": True,
            "user_id": None,
        }

    tier = _get_tier(user_id)
    if tier in UNLIMITED_TIERS:
        return {
            "tier": tier,
            "runs_used": _count_runs_last_30d(user_id),
            "runs_cap": 0,
            "runs_remaining": -1,  # sentinel: unlimited
            "unlimited": True,
            "user_id": user_id,
        }

    used = _count_runs_last_30d(user_id)
    remaining = max(0, FREE_TIER_RUN_CAP - used)
    return {
        "tier": tier,
        "runs_used": used,
        "runs_cap": FREE_TIER_RUN_CAP,
        "runs_remaining": remaining,
        "unlimited": False,
        "user_id": user_id,
    }


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


def compute_run_hash(pdb_bytes: bytes, chain_id: str) -> str:
    """Compute a short SHA-256 over the input structure + chain selector.

    Helper for callers that want to dedupe identical re-submissions. The
    paywall itself does not use the hash — it counts rows, not distinct
    structures — but downstream reporting can.
    """
    h = hashlib.sha256()
    h.update(pdb_bytes)
    h.update(b"|")
    h.update(chain_id.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def requires_scout_quota(f):
    """Flask decorator — block route when a free-tier user is over cap.

    Behaviour
    ---------
    * Not logged in: passes through. ``login_required`` enforces auth
      upstream; this decorator does not duplicate that check.
    * Signed in on a paid tier: passes through.
    * Signed in on free tier, below cap: passes through.
    * Signed in on free tier, at or above cap:
        - JSON requests (Accept: application/json or XHR): returns HTTP 402
          with a JSON body describing the paywall.
        - Plain-HTML requests: redirects to ``/upgrade``.
    * Supabase unreachable: passes through (fail open). Logged as a warning.

    Keep this decorator *below* ``@login_required`` and *above* the route
    body in the decorator stack, e.g.::

        @flask_app.route("/analyze", methods=["POST"])
        @login_required
        @requires_scout_quota
        def analyze():
            ...
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        email = session.get("user_email")
        if not email:
            # login_required should have caught this; be defensive.
            return f(*args, **kwargs)

        status = quota_status(email)
        if status["unlimited"]:
            return f(*args, **kwargs)

        if status["runs_used"] < status["runs_cap"]:
            return f(*args, **kwargs)

        # At or above cap — block.
        logger.info(
            "Scout quota exceeded: email=%s used=%d cap=%d",
            email, status["runs_used"], status["runs_cap"],
        )
        wants_json = (
            "application/json" in (request.headers.get("Accept", "") or "")
            or request.is_json
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            return jsonify({
                "error": (
                    "Free tier limit reached: "
                    f"{status['runs_cap']} runs per 30 days. "
                    "Upgrade to Scout Pro for unlimited runs."
                ),
                "upgrade_url": "/upgrade",
                "runs_used": status["runs_used"],
                "runs_cap": status["runs_cap"],
            }), 402
        return redirect(url_for("upgrade"))

    return wrapped
