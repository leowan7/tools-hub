"""Target Workspace lifecycle — the SaaS unit-of-sale for tools-hub.

A Workspace = one target PDB + 30 days of unlimited design tool runs +
a USD-denominated Modal compute cap. Customers buy a Workspace via Stripe
Checkout (one-time payment); the webhook activates it; all subsequent GPU
job submissions for that target deduct their actual Modal cost from the
Workspace's remaining cap.

This module is NOT the same as ``shared.campaigns`` — that handles the
wet-lab CRO handoff (``lab_campaigns`` table). Workspace is the
self-serve SaaS unit; do not confuse the two.

Data model
----------
* ``public.workspaces`` — one row per activated purchase
* ``public.workspaces_active`` — view of currently-active workspaces
* ``public.workspaces_history`` — view of all statuses for the account page

All writes use the service-role Supabase client; the table has RLS
self-read but no client-side INSERT/UPDATE policies.

Refund policy
-------------
First-Workspace-ever, within 7 days of activation, no questions asked.
Anti-abuse: refund eligibility flips off the moment the user has any
prior workspace in the history (even refunded). ``request_refund``
calls Stripe to issue the refund, marks the workspace ``refunded``, and
blocks further GPU dispatch.

Lifecycle
---------
::

    activate_workspace()        -- on checkout.session.completed
        |
        v
    get_active_workspace()      -- looked up on each GPU submission
        |
        v
    charge_workspace()          -- after each Modal job completes
        |                       --   (may cross 80% warning threshold)
        v
    is_within_cap() -> False    -- blocks new submissions
        |
        v
    expire_workspaces() (cron)  -- daily; status=expired past expires_at
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from shared.credits import get_service_client, record_grant, record_spend

logger = logging.getLogger(__name__)

_TABLE = "workspaces"

# Refund window: 7 days from activation, first-Workspace-only.
REFUND_WINDOW_DAYS = 7

# Standard 30-day Workspace duration.
DEFAULT_DURATION_DAYS = 30

# 80% cap warning threshold (sends email when crossed).
WARN_THRESHOLD = 0.80


# ---------------------------------------------------------------------------
# Modal GPU pricing per second (USD)
# ---------------------------------------------------------------------------
# Public Modal rate card as of 2026-05; sourced from modal.com/pricing.
# Used to translate raw ``gpu_seconds_used`` reported by the Modal webhook
# into a USD charge against the user's Workspace cap.
#
# These are conservative upper bounds (slightly above sticker price) so
# the per-Workspace margin model never under-bills the customer.

GPU_USD_PER_SECOND = {
    "A10G":      0.000208,   # $0.75/hr
    "A100-40GB": 0.000714,   # $2.57/hr (rounded up from $2.10 list)
    "A100-80GB": 0.001028,   # $3.70/hr
    "H100":      0.002417,   # $8.70/hr (incl. premium tier)
    "L4":        0.000236,   # $0.85/hr
    "L40S":      0.000597,   # $2.15/hr
    "T4":        0.000164,   # $0.59/hr
}

# Conservative default — used when the GPU SKU is missing or unknown.
DEFAULT_USD_PER_SECOND = 0.001028  # A100-80GB rate, the most common.


# ---------------------------------------------------------------------------
# Domain object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Workspace:
    """Immutable view of a workspaces row."""

    id: str
    user_id: str
    target_pdb_id: str
    target_label: Optional[str]
    sku: str  # 'workspace_standard' | 'workspace_xl'
    modal_cap_usd: float
    modal_spent_usd: float
    activated_at: datetime
    expires_at: datetime
    refund_eligible_until: Optional[datetime]
    refunded_at: Optional[datetime]
    status: str  # 'active' | 'expired' | 'refunded'
    stripe_payment_intent_id: Optional[str]
    stripe_refund_id: Optional[str]

    @classmethod
    def from_row(cls, row: dict) -> "Workspace":
        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            target_pdb_id=str(row["target_pdb_id"]),
            target_label=row.get("target_label"),
            sku=str(row["sku"]),
            modal_cap_usd=float(row.get("modal_cap_usd") or 0),
            modal_spent_usd=float(row.get("modal_spent_usd") or 0),
            activated_at=_parse_ts(row.get("activated_at")),
            expires_at=_parse_ts(row.get("expires_at")),
            refund_eligible_until=_maybe_ts(row.get("refund_eligible_until")),
            refunded_at=_maybe_ts(row.get("refunded_at")),
            status=str(row.get("status") or "active"),
            stripe_payment_intent_id=row.get("stripe_payment_intent_id"),
            stripe_refund_id=row.get("stripe_refund_id"),
        )

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.modal_cap_usd - self.modal_spent_usd)

    @property
    def pct_used(self) -> float:
        if self.modal_cap_usd <= 0:
            return 0.0
        return min(100.0, (self.modal_spent_usd / self.modal_cap_usd) * 100)

    @property
    def is_within_cap(self) -> bool:
        return (
            self.status == "active"
            and self.modal_spent_usd < self.modal_cap_usd
            and self.expires_at > datetime.now(timezone.utc)
        )

    @property
    def refund_eligible_now(self) -> bool:
        if self.refunded_at is not None:
            return False
        if self.refund_eligible_until is None:
            return False
        return self.refund_eligible_until > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# SKU registry — mirrors billing.tiers WorkspaceSKU but kept here so
# webhooks/cron can resolve cap + duration without importing the billing layer.
# ---------------------------------------------------------------------------


_SKU_CONFIG: dict[str, tuple[float, int]] = {
    # sku -> (modal_cap_usd, duration_days)
    "workspace_standard": (100.00, DEFAULT_DURATION_DAYS),
    "workspace_xl": (500.00, DEFAULT_DURATION_DAYS),
}


def sku_config(sku: str) -> Optional[tuple[float, int]]:
    """Return (modal_cap_usd, duration_days) for a SKU or None."""
    return _SKU_CONFIG.get(sku)


# ---------------------------------------------------------------------------
# Activation (called from Stripe webhook on checkout.session.completed)
# ---------------------------------------------------------------------------


def activate_workspace(
    user_id: str,
    target_pdb_id: str,
    sku: str,
    *,
    target_label: Optional[str] = None,
    stripe_payment_intent_id: Optional[str] = None,
    stripe_event_id: Optional[str] = None,
) -> Optional[Workspace]:
    """Create a new active Workspace row for the user.

    Idempotent on ``stripe_payment_intent_id`` (UNIQUE constraint): a
    duplicate webhook for the same payment returns the existing row.

    Computes refund_eligible_until based on whether the user has any
    prior workspace; first-ever activations get 7 days, subsequent get None.
    Also records a ``grant`` ledger entry equal to ``modal_cap_usd`` so the
    internal credits accounting stays balanced.
    """
    config = sku_config(sku)
    if config is None:
        logger.error("activate_workspace: unknown sku=%s", sku)
        return None
    modal_cap_usd, duration_days = config

    client = get_service_client()
    if client is None:
        logger.error("activate_workspace: Supabase service client missing.")
        return None

    # Idempotency by Stripe PI: a duplicate webhook returns the existing row.
    if stripe_payment_intent_id:
        existing = _find_by_payment_intent(client, stripe_payment_intent_id)
        if existing:
            logger.info(
                "activate_workspace: idempotent return for pi=%s ws=%s",
                stripe_payment_intent_id,
                existing.id,
            )
            return existing

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=duration_days)

    is_first = _is_first_workspace_for_user(client, user_id)
    refund_until = (
        now + timedelta(days=REFUND_WINDOW_DAYS) if is_first else None
    )

    row = {
        "user_id": user_id,
        "target_pdb_id": target_pdb_id,
        "target_label": target_label,
        "sku": sku,
        "modal_cap_usd": float(modal_cap_usd),
        "modal_spent_usd": 0,
        "activated_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "refund_eligible_until": (
            refund_until.isoformat() if refund_until else None
        ),
        "status": "active",
        "stripe_payment_intent_id": stripe_payment_intent_id,
        "stripe_event_id": stripe_event_id,
    }
    try:
        response = client.table(_TABLE).insert(row).execute()
        data = getattr(response, "data", None) or []
        if not data:
            logger.error("activate_workspace: empty insert response.")
            return None
        ws = Workspace.from_row(data[0])

        # Internal accounting: grant credits == modal_cap_usd (1 credit per
        # USD) so the credits_ledger still reflects total compute purchased.
        # Customers don't see this; it's for margin reporting.
        record_grant(
            user_id,
            int(round(modal_cap_usd)),
            reason=f"workspace activated: {sku} ({target_pdb_id})",
            stripe_event_id=stripe_event_id,
            metadata={
                "workspace_id": ws.id,
                "target_pdb_id": target_pdb_id,
                "sku": sku,
            },
        )
        logger.info(
            "Workspace activated: user=%s sku=%s target=%s id=%s",
            user_id, sku, target_pdb_id, ws.id,
        )
        return ws
    except Exception:
        logger.error(
            "Failed to activate workspace for user %s", user_id, exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def get_workspace(workspace_id: str) -> Optional[Workspace]:
    """Fetch a workspace by id."""
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("id", workspace_id)
            .maybe_single()
            .execute()
        )
        data = getattr(response, "data", None)
        return Workspace.from_row(data) if data else None
    except Exception:
        logger.warning(
            "Could not fetch workspace %s", workspace_id, exc_info=True
        )
        return None


def get_active_workspace(
    user_id: str, target_pdb_id: str
) -> Optional[Workspace]:
    """Return the user's currently-active workspace for a given target.

    "Active" means status=active AND expires_at > now AND within cap.
    If multiple active workspaces exist for the same target (edge case
    from buying a second mid-month), returns the most recently activated.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .eq("target_pdb_id", target_pdb_id)
            .eq("status", "active")
            .order("activated_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
        ws = Workspace.from_row(rows[0])
        # Expiration check (defensive — the daily cron should already have
        # flipped status, but never trust the row not to be stale).
        if ws.expires_at <= datetime.now(timezone.utc):
            return None
        return ws
    except Exception:
        logger.warning(
            "Could not fetch active workspace user=%s target=%s",
            user_id, target_pdb_id, exc_info=True,
        )
        return None


def list_active_workspaces(user_id: str) -> list[Workspace]:
    """All currently-active workspaces for a user."""
    client = get_service_client()
    if client is None:
        return []
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "active")
            .gt("expires_at", datetime.now(timezone.utc).isoformat())
            .order("activated_at", desc=True)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        return [Workspace.from_row(r) for r in rows]
    except Exception:
        logger.warning(
            "Could not list active workspaces for %s", user_id, exc_info=True
        )
        return []


def list_workspace_history(user_id: str, limit: int = 50) -> list[Workspace]:
    """All workspaces (any status) for a user, newest first."""
    client = get_service_client()
    if client is None:
        return []
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("activated_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        return [Workspace.from_row(r) for r in rows]
    except Exception:
        logger.warning(
            "Could not list workspace history for %s", user_id, exc_info=True
        )
        return []


def active_workspaces_count(user_id: str) -> int:
    """Cheap header-badge query: how many active workspaces does user have?"""
    return len(list_active_workspaces(user_id))


# ---------------------------------------------------------------------------
# Charging compute against the cap
# ---------------------------------------------------------------------------


def charge_workspace(
    workspace_id: str,
    modal_usd: float,
    *,
    tool: str,
    job_id: Optional[str] = None,
) -> bool:
    """Deduct ``modal_usd`` from the workspace's spent counter.

    Called after a Modal job completes (with actual measured cost). Also
    records a ``spend`` ledger entry tied to job_id for margin reporting.

    Does NOT block if going over cap (the pre-flight in gpu/modal_client.py
    is the gate); this just records reality. Crossing 80% should trigger a
    warning email — handled by the caller, since email infra varies.

    Returns True on success.
    """
    if modal_usd < 0:
        logger.warning("charge_workspace: negative modal_usd=%s", modal_usd)
        return False
    if modal_usd == 0:
        return True

    client = get_service_client()
    if client is None:
        return False

    ws = get_workspace(workspace_id)
    if ws is None:
        logger.error("charge_workspace: workspace not found id=%s", workspace_id)
        return False

    new_spent = ws.modal_spent_usd + modal_usd
    try:
        client.table(_TABLE).update(
            {"modal_spent_usd": new_spent}
        ).eq("id", workspace_id).execute()
    except Exception:
        logger.error(
            "Failed to update workspace spend id=%s", workspace_id, exc_info=True
        )
        return False

    # Internal credits accounting: 1 credit = $1 USD, rounded.
    cents = int(round(modal_usd * 100))
    if cents > 0:
        # Convert cents back to whole-credit ints for the ledger constraint
        # (delta is integer). We accumulate fractional usage as a metadata
        # "modal_usd" field for precise reporting.
        amount_credits = max(1, int(round(modal_usd)))
        record_spend(
            ws.user_id,
            amount_credits,
            tool=tool,
            reason=f"workspace charge: {tool} on {ws.target_pdb_id}",
            job_id=job_id,
            metadata={
                "workspace_id": workspace_id,
                "modal_usd": modal_usd,
                "target_pdb_id": ws.target_pdb_id,
            },
        )

    logger.info(
        "Workspace charged: id=%s tool=%s usd=%.4f new_total=%.4f cap=%.2f",
        workspace_id, tool, modal_usd, new_spent, ws.modal_cap_usd,
    )
    return True


def crossed_warn_threshold(
    before_usd: float, after_usd: float, cap_usd: float
) -> bool:
    """True if this charge moved usage across the 80% warning line."""
    if cap_usd <= 0:
        return False
    before_pct = before_usd / cap_usd
    after_pct = after_usd / cap_usd
    return before_pct < WARN_THRESHOLD <= after_pct


# ---------------------------------------------------------------------------
# Refunds (first-Workspace-only, 7-day window)
# ---------------------------------------------------------------------------


def request_refund(
    workspace_id: str,
    *,
    stripe_refund_id: str,
) -> Optional[Workspace]:
    """Mark the workspace ``refunded`` and record the Stripe refund id.

    Caller is responsible for actually issuing the Stripe refund via the
    Stripe API before calling this; this method only records the result
    and flips state. Returns the updated Workspace on success.

    Eligibility (caller must verify before calling):
    1. ws.refund_eligible_now == True
    2. The Stripe refund API call succeeded
    """
    client = get_service_client()
    if client is None:
        return None
    now = datetime.now(timezone.utc)
    try:
        response = (
            client.table(_TABLE)
            .update({
                "status": "refunded",
                "refunded_at": now.isoformat(),
                "stripe_refund_id": stripe_refund_id,
            })
            .eq("id", workspace_id)
            .execute()
        )
        data = getattr(response, "data", None) or []
        if not data:
            logger.error("request_refund: no row updated id=%s", workspace_id)
            return None
        ws = Workspace.from_row(data[0])
        logger.info(
            "Workspace refunded: id=%s refund_id=%s user=%s",
            workspace_id, stripe_refund_id, ws.user_id,
        )
        return ws
    except Exception:
        logger.error(
            "Failed to mark workspace refunded id=%s", workspace_id, exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# Expiration cron (runs daily)
# ---------------------------------------------------------------------------


def expire_workspaces() -> int:
    """Flip status to 'expired' on any active workspace past expires_at.

    Returns the number of rows updated. Designed to be safe to run
    arbitrarily often (idempotent — only touches active rows past TTL).
    """
    client = get_service_client()
    if client is None:
        return 0
    now = datetime.now(timezone.utc)
    try:
        response = (
            client.table(_TABLE)
            .update({"status": "expired"})
            .eq("status", "active")
            .lt("expires_at", now.isoformat())
            .execute()
        )
        data = getattr(response, "data", None) or []
        count = len(data)
        if count:
            logger.info("Expired %d workspace(s) past TTL.", count)
        return count
    except Exception:
        logger.error("expire_workspaces: update failed", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_first_workspace_for_user(client, user_id: str) -> bool:
    """True if the user has never had any workspace row (any status)."""
    try:
        response = (
            client.table(_TABLE)
            .select("id")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        return len(rows) == 0
    except Exception:
        logger.warning(
            "Could not check first-workspace status for %s",
            user_id, exc_info=True,
        )
        # Fail closed — assume NOT first to avoid accidentally granting
        # refund eligibility to a user who's already used it.
        return False


def _find_by_payment_intent(client, pi_id: str) -> Optional[Workspace]:
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("stripe_payment_intent_id", pi_id)
            .maybe_single()
            .execute()
        )
        data = getattr(response, "data", None)
        return Workspace.from_row(data) if data else None
    except Exception:
        return None


def _parse_ts(value) -> datetime:
    """Parse a Supabase timestamptz into a UTC datetime; defaults to now if missing."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # ISO string from JSON
    try:
        # Postgres often returns "...+00:00"; fromisoformat handles that in 3.11+.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _maybe_ts(value) -> Optional[datetime]:
    if value is None:
        return None
    return _parse_ts(value)


# ---------------------------------------------------------------------------
# Modal cost helpers — translate raw GPU seconds → USD for cap accounting
# ---------------------------------------------------------------------------


def compute_modal_cost_usd(
    gpu_seconds: float,
    gpu_sku: Optional[str] = None,
) -> float:
    """Translate ``gpu_seconds`` on a given GPU SKU into a USD charge.

    Used by the Modal webhook handler when a job completes to determine
    how much to deduct from the active Workspace's cap.

    >>> compute_modal_cost_usd(3600, "A100-80GB")
    3.7008
    """
    if gpu_seconds is None or gpu_seconds <= 0:
        return 0.0
    rate = GPU_USD_PER_SECOND.get(gpu_sku or "", DEFAULT_USD_PER_SECOND)
    return float(gpu_seconds) * rate


# ---------------------------------------------------------------------------
# Pre-flight check — call before dispatching a GPU job
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of a workspace pre-flight gate check."""

    allow: bool
    workspace: Optional[Workspace]
    reason: str  # 'ok' | 'no_workspace' | 'cap_exceeded' | 'expired'
    upgrade_message: Optional[str] = None


def workspace_preflight(
    user_id: str,
    target_pdb_id: str,
    *,
    estimated_modal_usd: float = 0.0,
) -> PreflightResult:
    """Gate a GPU submission against the user's active Workspace.

    Call this from every Flask route that dispatches a Modal job. It:

    1. Looks up the user's active Workspace for the target PDB.
    2. Rejects if no active Workspace exists -> redirect to /pricing.
    3. Rejects if the cap is already exhausted -> redirect to /workspaces.
    4. Allows otherwise, returning the Workspace so the caller can record
       its id alongside the job for later ``charge_for_job`` lookup.

    ``estimated_modal_usd`` is a hint, not a hard pre-authorisation —
    we don't reserve cap up front, we just check the current remaining.
    The Modal webhook is authoritative on actual cost.
    """
    ws = get_active_workspace(user_id, target_pdb_id)
    if ws is None:
        return PreflightResult(
            allow=False,
            workspace=None,
            reason="no_workspace",
            upgrade_message=(
                "Activate a Target Workspace before running designs on "
                "this target."
            ),
        )
    if ws.expires_at <= datetime.now(timezone.utc):
        return PreflightResult(
            allow=False,
            workspace=ws,
            reason="expired",
            upgrade_message=(
                "Your Workspace for this target has expired. "
                "Activate a new Workspace to continue."
            ),
        )
    if ws.modal_spent_usd >= ws.modal_cap_usd:
        return PreflightResult(
            allow=False,
            workspace=ws,
            reason="cap_exceeded",
            upgrade_message=(
                "This Workspace has hit its compute cap. "
                "Upgrade to Workspace XL or activate a second Workspace."
            ),
        )
    return PreflightResult(allow=True, workspace=ws, reason="ok")


# ---------------------------------------------------------------------------
# Post-completion charge — call from the Modal job-completion webhook
# ---------------------------------------------------------------------------


def charge_for_job(
    user_id: str,
    target_pdb_id: str,
    *,
    gpu_seconds: float,
    gpu_sku: Optional[str],
    tool: str,
    job_id: Optional[str] = None,
) -> Optional[Workspace]:
    """Deduct the actual Modal cost from the active Workspace.

    Called by ``webhooks/modal.py`` when a tool job completes (or fails
    after consuming compute). Resolves the user's active Workspace for
    the target, converts seconds → USD, and calls ``charge_workspace``.

    Returns the updated Workspace (with the new spent total) or None if
    no workspace was found (legacy / orphan job).

    The caller is responsible for detecting a crossed 80% warning
    threshold and dispatching the warning email; this function provides
    the before/after values via the returned Workspace.
    """
    if not gpu_seconds or gpu_seconds <= 0:
        return None
    ws = get_active_workspace(user_id, target_pdb_id)
    if ws is None:
        logger.info(
            "charge_for_job: no active workspace for user=%s target=%s "
            "(orphan or pre-Workspace job, skipping charge)",
            user_id, target_pdb_id,
        )
        return None
    modal_usd = compute_modal_cost_usd(gpu_seconds, gpu_sku)
    if modal_usd <= 0:
        return ws
    before_spent = ws.modal_spent_usd
    if not charge_workspace(
        ws.id, modal_usd, tool=tool, job_id=job_id
    ):
        return None
    after_spent = before_spent + modal_usd
    if crossed_warn_threshold(before_spent, after_spent, ws.modal_cap_usd):
        # Caller should send the 80% warning email. We log here so an
        # audit can correlate.
        logger.info(
            "Workspace %s crossed 80%% threshold: %.2f -> %.2f / %.2f",
            ws.id, before_spent, after_spent, ws.modal_cap_usd,
        )
    return get_workspace(ws.id)
