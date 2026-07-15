"""USD wallet primitives for the Ranomics tools hub.

Replaces the per-target Workspace SaaS model with a pre-auth wallet:
users top up a USD balance, every job places an atomic hold for its
estimated cost before enqueue, and settlement on completion releases
any surplus or debits any variance up to a per-tool hard cap.

Lifecycle
---------
::

    record_signup_credit()                  # one-time $5 grant on first signup
        |
        v
    top_up_wallet()                         # Stripe Checkout success webhook
        |
        v
    reserve_hold() / hold_for_job()         # called by route gate before enqueue
        |
        v
    settle_hold() / settle_job()            # called from shared/jobs.py on
        |                                   #   completion OR failure
        v
    auto_reload_if_needed()                 # off-session PaymentIntent if balance
                                            #   below threshold (Agent E wires
                                            #   the Stripe call in Wave 2)

All ledger writes go through SQL functions defined in
``supabase/migrations/0017_wallet.sql``:

* ``try_hold_for_job`` does an atomic balance check plus hold row
  insert behind a ``select ... for update`` row lock on
  ``user_wallets``.
* ``settle_hold`` replaces a hold with a charge row plus optional
  ``hold_release`` row, clamped to the per-tool hard cap.
* ``release_hold`` is an explicit release for cancel-before-run flows.

Ledger invariants the SQL layer enforces (drift checks in
``shared.wallet_funnel`` plus property-based tests in
``tests/test_wallet_invariants.py``):

1. ``user_wallets.balance_usd == sum(wallet_transactions.amount_usd)``.
2. Every ``hold_release`` and ``charge`` row references a parent hold.
3. ``stripe_event_id`` is unique per row.
4. ``auto_reload`` count in last 24h is at most 1 per user.
5. No ``charge`` row exceeds the parameter-scaled per-tool hard cap.

Stripe code and route wiring live elsewhere. This module owns the
balance math, the hold lifecycle, the auto-reload safety logic, and
the dispute freeze flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from typing import Any, Callable, Mapping, Optional

from shared.credits import get_service_client
from shared.supabase_client import get_supabase_client  # noqa: F401  (re-export OK)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Multiplier applied to raw Modal compute cost when charging the user.
WALLET_MARKUP = Decimal("1.70")

# Minimum top-up amount allowed via Stripe Checkout.
MIN_TOPUP_USD = Decimal("20.00")

# DB default for the (now inert) daily_spend_cap_usd column. Phase 2
# fund-and-drain retired the daily cap (migration 0035 dropped the block
# from try_hold_for_job); this value is no longer written on the
# wallet-creation path and only mirrors the schema default for tests.
DEFAULT_DAILY_CAP_USD = Decimal("200.00")

# Default monthly auto-reload safety cap.
DEFAULT_AUTO_RELOAD_MONTHLY_CAP_USD = Decimal("1000.00")

# Signup credit grant amount.
SIGNUP_CREDIT_USD = Decimal("5.00")

# Send the low-balance email when balance drops below this.
LOW_BALANCE_EMAIL_THRESHOLD = Decimal("5.00")

# Self-serve ceiling per single job. Anything above routes the user
# into the Binder Pilot funnel rather than running unattended.
SELF_SERVE_CEILING_USD = Decimal("1000.00")

# Modal GPU rate card in USD per second. Copied verbatim from
# :mod:`shared.workspaces` so this module is self-contained and the
# Workspace module can be retired without breaking wallet pricing.
# Public Modal rate card as of 2026-05; sourced from modal.com/pricing.
# These are conservative upper bounds (slightly above sticker price)
# so the per-charge margin never under-bills the customer.
GPU_USD_PER_SECOND: Mapping[str, float] = {
    "A10G":      0.000208,   # $0.75/hr
    "A100-40GB": 0.000714,   # $2.57/hr (rounded up from $2.10 list)
    "A100-80GB": 0.001028,   # $3.70/hr
    "H100":      0.002417,   # $8.70/hr (incl. premium tier)
    "L4":        0.000236,   # $0.85/hr
    "L40S":      0.000597,   # $2.15/hr
    "T4":        0.000164,   # $0.59/hr
}

# Fallback rate when the GPU SKU is missing or unknown.
DEFAULT_USD_PER_SECOND = 0.001028  # A100-80GB rate.

# Absolute per-tool hard caps. The parameter-scaled cap saturates here
# regardless of the value of the scaling parameter. Kept in sync with
# :data:`shared.wallet_estimates.TOOL_SPECS` entries.
PER_JOB_HARD_CAP_USD: Mapping[str, Decimal] = {
    "mpnn":        Decimal("150.00"),
    # ``alphafold2`` retained for backward compat with existing tests +
    # wallet ledger rows recorded under that key. The production route
    # uses the ``af2`` adapter slug, mirrored below.
    "alphafold2":  Decimal("500.00"),
    "af2":         Decimal("500.00"),
    "colabfold":   Decimal("500.00"),
    "esmfold":     Decimal("200.00"),
    "rfdiffusion": Decimal("500.00"),
    "rfantibody":  Decimal("500.00"),
    "bindcraft":   Decimal("500.00"),
    "pxdesign":    Decimal("500.00"),
    "boltzgen":    Decimal("300.00"),
    "boltz2":      Decimal("50.00"),
}


# ---------------------------------------------------------------------------
# Reason strings used by route gates and tests
# ---------------------------------------------------------------------------


REASON_OK = "ok"
REASON_WALLET_FROZEN = "wallet_frozen"
REASON_INSUFFICIENT = "insufficient_balance"
REASON_PER_TOOL_CAP = "per_tool_cap_exceeded"
REASON_SELF_SERVE_CEILING = "self_serve_ceiling_exceeded"


def _round_up_topup_amount(deficit: Decimal) -> Decimal:
    """Round the deficit up to the nearest $5, with a floor of MIN_TOPUP_USD.

    Mirrors the formula in the plan's Moment 2 spec:
    ``ceil((estimate - balance) / 5) * 5`` with a $20 minimum.

    Lives here (not in the route layer) so both the wallet-gate renderer
    (:func:`shared.wallet_guard._render_topup_gate`) and the reactive
    ``/api/wallet/estimate`` endpoint share one rounding rule.
    """
    if deficit <= 0:
        return MIN_TOPUP_USD
    five = Decimal("5")
    bumped = (deficit / five).to_integral_value(rounding="ROUND_CEILING") * five
    return max(bumped, MIN_TOPUP_USD)


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of a wallet pre-flight check."""

    allow: bool
    reason: str
    estimated_cost_usd: Decimal
    balance_usd: Decimal
    deficit_usd: Decimal
    hard_cap_usd: Decimal


# ---------------------------------------------------------------------------
# Modal cost conversion
# ---------------------------------------------------------------------------


def gpu_usd_per_second(gpu_class: Optional[str]) -> float:
    """Return the USD per second for a Modal GPU class.

    Falls back to ``DEFAULT_USD_PER_SECOND`` when the class is missing
    or unknown.
    """
    if not gpu_class:
        return DEFAULT_USD_PER_SECOND
    return GPU_USD_PER_SECOND.get(gpu_class, DEFAULT_USD_PER_SECOND)


def compute_modal_cost_usd(
    gpu_seconds: float, gpu_class: Optional[str] = None
) -> Decimal:
    """Raw Modal cost in USD before markup.

    Used by :func:`settle_hold` and by mid-run progress callbacks. Returns
    ``Decimal('0')`` for non-positive input.
    """
    if not gpu_seconds or gpu_seconds <= 0:
        return Decimal("0")
    rate = gpu_usd_per_second(gpu_class)
    return (Decimal(str(gpu_seconds)) * Decimal(str(rate))).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )


def compute_charge_usd(gpu_seconds: float, gpu_class: Optional[str] = None) -> Decimal:
    """Customer-facing charge in USD (raw cost times markup)."""
    raw = compute_modal_cost_usd(gpu_seconds, gpu_class)
    return (raw * WALLET_MARKUP).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Wallet bootstrap + lookups
# ---------------------------------------------------------------------------


def get_or_create_wallet(user_id: str) -> Optional[dict]:
    """Return the ``user_wallets`` row for ``user_id``, creating it if absent.

    Idempotent. On a fresh row the helper grants the signup credit by
    calling :func:`record_signup_credit`.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table("user_wallets")
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        existing = getattr(response, "data", None)
        if existing:
            return existing
        return _create_wallet_with_signup_credit(client, user_id)
    except Exception:
        logger.warning(
            "get_or_create_wallet failed for %s", user_id, exc_info=True
        )
        return None


def _wallet(user_id: str) -> Optional[dict]:
    """Cheap wallet read used by post-settle helpers."""
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table("user_wallets")
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return getattr(response, "data", None)
    except Exception:
        logger.warning("wallet lookup failed for %s", user_id, exc_info=True)
        return None


def _create_wallet_with_signup_credit(client, user_id: str) -> Optional[dict]:
    """Insert a fresh wallet row + the signup credit ledger entry."""
    try:
        response = (
            client.table("user_wallets")
            .insert(
                {
                    "user_id": user_id,
                    "balance_usd": 0,
                    "auto_reload_enabled": False,
                    "auto_reload_monthly_cap_usd": float(
                        DEFAULT_AUTO_RELOAD_MONTHLY_CAP_USD
                    ),
                    "wallet_frozen": False,
                }
            )
            .execute()
        )
        data = getattr(response, "data", None) or []
        if not data:
            logger.error("_create_wallet_with_signup_credit: empty insert response.")
            return None
        wallet = data[0]
    except Exception:
        logger.error(
            "Could not create wallet row for %s", user_id, exc_info=True
        )
        return None
    record_signup_credit(user_id)
    return _wallet(user_id) or wallet


def record_signup_credit(user_id: str) -> bool:
    """Grant the one-time signup credit. Idempotent on ``user_id``.

    Inserts a ``signup_credit`` ledger row with a synthetic unique key
    so a duplicate call returns without crediting twice.
    """
    client = get_service_client()
    if client is None:
        return False
    synthetic_event_id = f"signup_credit:{user_id}"
    try:
        dup = (
            client.table("wallet_transactions")
            .select("id")
            .eq("stripe_event_id", synthetic_event_id)
            .limit(1)
            .execute()
        )
        if getattr(dup, "data", None):
            logger.info("record_signup_credit: idempotent skip for %s", user_id)
            return True
    except Exception:
        logger.warning(
            "record_signup_credit: dup check failed for %s", user_id, exc_info=True
        )
        # Continue. The SQL helper will fail loudly if there is a real conflict.
    try:
        client.rpc(
            "credit_wallet",
            {
                "p_user_id": user_id,
                "p_amount_usd": float(SIGNUP_CREDIT_USD),
                "p_kind": "signup_credit",
                "p_stripe_event_id": synthetic_event_id,
                "p_stripe_payment_intent_id": None,
            },
        ).execute()
        try:
            from shared.email import send_signup_credit_email  # noqa: PLC0415
            send_signup_credit_email(user_id=user_id)
        except Exception:  # pragma: no cover (email is best-effort)
            logger.warning(
                "record_signup_credit: email dispatch failed for %s",
                user_id, exc_info=True,
            )
        return True
    except Exception:
        logger.error(
            "record_signup_credit: credit_wallet failed for %s",
            user_id, exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Top-ups (Stripe Checkout + auto-reload PaymentIntent)
# ---------------------------------------------------------------------------


def top_up_wallet(
    user_id: str,
    amount_usd: Decimal,
    *,
    stripe_payment_intent_id: str,
    stripe_event_id: str,
    kind: str = "topup",
) -> Optional[dict]:
    """Credit the wallet with a top-up. Idempotent on ``stripe_event_id``.

    ``kind`` is one of ``topup``, ``auto_reload``, or ``promo``. The
    underlying SQL function enforces the unique-index on
    ``stripe_event_id`` so a webhook replay does not double-credit.
    Returns the post-credit wallet row.
    """
    if amount_usd <= 0:
        raise ValueError("Top-up amount must be positive.")
    if kind not in {"topup", "auto_reload", "promo", "adjustment"}:
        raise ValueError(f"Unsupported top-up kind: {kind}")
    client = get_service_client()
    if client is None:
        logger.error("top_up_wallet: Supabase service client missing.")
        return None
    try:
        dup = (
            client.table("wallet_transactions")
            .select("id")
            .eq("stripe_event_id", stripe_event_id)
            .limit(1)
            .execute()
        )
        if getattr(dup, "data", None):
            logger.info(
                "top_up_wallet: idempotent skip for event=%s user=%s",
                stripe_event_id, user_id,
            )
            return _wallet(user_id)
    except Exception:
        logger.warning(
            "top_up_wallet: dup check failed event=%s",
            stripe_event_id, exc_info=True,
        )
    try:
        client.rpc(
            "credit_wallet",
            {
                "p_user_id": user_id,
                "p_amount_usd": float(amount_usd),
                "p_kind": kind,
                "p_stripe_event_id": stripe_event_id,
                "p_stripe_payment_intent_id": stripe_payment_intent_id,
            },
        ).execute()
        logger.info(
            "top_up_wallet: credited user=%s amount=%s kind=%s",
            user_id, amount_usd, kind,
        )
        return _wallet(user_id)
    except Exception:
        logger.error(
            "top_up_wallet: credit_wallet RPC failed for %s",
            user_id, exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Pre-flight + hold lifecycle
# ---------------------------------------------------------------------------


def wallet_preflight(
    user_id: str,
    tool_slug: str,
    estimated_cost_usd: Decimal,
    params: Optional[Mapping[str, object]] = None,
) -> PreflightResult:
    """Pre-flight check used by the form-render path and the submit gate.

    Returns a structured result so the route layer can decide whether
    to render the submit button, the top-up CTA, or the Pilot CTA.
    Does NOT place a hold. The atomic check-and-reserve is in
    :func:`reserve_hold`.
    """
    from .wallet_estimates import compute_hard_cap  # noqa: PLC0415

    params = dict(params or {})
    wallet = get_or_create_wallet(user_id)
    balance = Decimal(str((wallet or {}).get("balance_usd") or 0))
    hard_cap = compute_hard_cap(tool_slug, params)
    deficit = max(Decimal("0"), estimated_cost_usd - balance)

    if not wallet:
        return PreflightResult(
            allow=False,
            reason=REASON_WALLET_FROZEN,
            estimated_cost_usd=estimated_cost_usd,
            balance_usd=balance,
            deficit_usd=deficit,
            hard_cap_usd=hard_cap,
        )
    if wallet.get("wallet_frozen"):
        return PreflightResult(
            allow=False,
            reason=REASON_WALLET_FROZEN,
            estimated_cost_usd=estimated_cost_usd,
            balance_usd=balance,
            deficit_usd=deficit,
            hard_cap_usd=hard_cap,
        )
    if estimated_cost_usd > SELF_SERVE_CEILING_USD:
        return PreflightResult(
            allow=False,
            reason=REASON_SELF_SERVE_CEILING,
            estimated_cost_usd=estimated_cost_usd,
            balance_usd=balance,
            deficit_usd=deficit,
            # The ceiling — not the per-tool scaled cap — is what blocked
            # this job, so the capped-job email must show $1000.
            hard_cap_usd=SELF_SERVE_CEILING_USD,
        )
    if estimated_cost_usd > hard_cap:
        return PreflightResult(
            allow=False,
            reason=REASON_PER_TOOL_CAP,
            estimated_cost_usd=estimated_cost_usd,
            balance_usd=balance,
            deficit_usd=deficit,
            hard_cap_usd=hard_cap,
        )
    # Phase 2 fund-and-drain retired the per-day spend cap (migration 0035
    # drops the matching block from try_hold_for_job). The prepaid balance is
    # the only spend ceiling: the balance check below and the in-lock refusal
    # in try_hold_for_job mean total spend can never exceed funded money. A
    # daily rate limit within already-funded balance only got in the way of
    # metered campaign compute.
    if balance < estimated_cost_usd:
        return PreflightResult(
            allow=False,
            reason=REASON_INSUFFICIENT,
            estimated_cost_usd=estimated_cost_usd,
            balance_usd=balance,
            deficit_usd=deficit,
            hard_cap_usd=hard_cap,
        )
    return PreflightResult(
        allow=True,
        reason=REASON_OK,
        estimated_cost_usd=estimated_cost_usd,
        balance_usd=balance,
        deficit_usd=Decimal("0"),
        hard_cap_usd=hard_cap,
    )


def reserve_hold(
    user_id: str,
    tool_slug: str,
    job_id: Optional[int],
    estimated_cost_usd: Decimal,
    params: Optional[Mapping[str, object]] = None,
) -> Optional[str]:
    """Atomically reserve ``estimated_cost_usd`` from the user wallet.

    Returns the ``hold_tx_id`` on success, ``None`` if the hold cannot
    be placed. The SQL function re-checks wallet-frozen state, the
    parameter-scaled hard cap, and sufficient balance under a row lock,
    so those three are race-safe. The self-serve ceiling is enforced only
    by :func:`wallet_preflight` in Python; the in-lock balance check here
    still bounds total exposure. Phase 2 fund-and-drain retired the per-day
    spend cap, so the prepaid balance is the only spend ceiling.

    The route layer should also call :func:`wallet_preflight` first to
    surface a friendly reason for the user. ``reserve_hold`` is the
    canonical integrity point and the only source of truth on whether
    the hold actually landed.
    """
    if estimated_cost_usd <= 0:
        raise ValueError("Hold amount must be positive.")
    from .wallet_estimates import compute_hard_cap  # noqa: PLC0415

    params = dict(params or {})
    pre = wallet_preflight(user_id, tool_slug, estimated_cost_usd, params)
    if not pre.allow:
        _emit_preflight_email(user_id, tool_slug, pre)
        return None

    client = get_service_client()
    if client is None:
        return None
    hard_cap = compute_hard_cap(tool_slug, params)
    try:
        response = client.rpc(
            "try_hold_for_job",
            {
                "p_user_id": user_id,
                "p_amount_usd": float(estimated_cost_usd),
                "p_tool_slug": tool_slug,
                "p_job_id": job_id,
                "p_hard_cap_usd": float(hard_cap),
            },
        ).execute()
        data = getattr(response, "data", None)
        if not data:
            logger.info(
                "reserve_hold: SQL returned null for user=%s tool=%s amount=%s",
                user_id, tool_slug, estimated_cost_usd,
            )
            return None
        # try_hold_for_job RETURNS bigint, which PostgREST passes through
        # as a JSON int. Older callers may see a list/dict wrapper from the
        # supabase-py driver depending on its version, so handle all three.
        if isinstance(data, list):
            hold_id = data[0] if data else None
        elif isinstance(data, dict):
            hold_id = data.get("hold_tx_id")
        else:
            hold_id = data
        return str(hold_id) if hold_id is not None else None
    except Exception:
        logger.error(
            "reserve_hold: try_hold_for_job RPC failed for %s",
            user_id, exc_info=True,
        )
        return None


# Alias for callers wired to the older spec name.
hold_for_job = reserve_hold


def settle_hold(
    hold_tx_id: str,
    gpu_seconds: float,
    gpu_class: Optional[str],
    params: Optional[Mapping[str, object]] = None,
    failure_reason: Optional[str] = None,
) -> Optional[dict]:
    """Close out a hold against the actual compute consumed.

    Charges for actual compute even on failure (per policy). The actual
    USD is clamped to the parameter-scaled hard cap. If the actual is
    below the hold, the surplus is released. If the actual exceeds the
    hold, the variance is debited from the wallet, or recorded as
    absorbed variance if the wallet has no slack.

    Idempotent on ``hold_tx_id`` via a check inside the SQL function.
    """
    from .wallet_estimates import compute_hard_cap  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return None
    try:
        hold_resp = (
            client.table("wallet_transactions")
            .select("*")
            .eq("id", hold_tx_id)
            .maybe_single()
            .execute()
        )
        hold = getattr(hold_resp, "data", None)
        if not hold:
            logger.error("settle_hold: hold row not found id=%s", hold_tx_id)
            return None
    except Exception:
        logger.error("settle_hold: hold lookup failed id=%s",
                     hold_tx_id, exc_info=True)
        return None

    tool_slug = hold.get("tool_slug") or ""
    user_id = hold.get("user_id")
    params = dict(params or {})
    hard_cap = compute_hard_cap(tool_slug, params)
    actual_cost = compute_charge_usd(gpu_seconds, gpu_class)

    try:
        client.rpc(
            "settle_hold",
            {
                "p_hold_tx_id": hold_tx_id,
                "p_actual_usd": float(actual_cost),
                "p_hard_cap_usd": float(hard_cap),
                "p_gpu_seconds": float(gpu_seconds or 0),
                "p_gpu_class": gpu_class,
                "p_failure_reason": failure_reason,
            },
        ).execute()
    except Exception:
        logger.error(
            "settle_hold: settle_hold RPC failed hold=%s",
            hold_tx_id, exc_info=True,
        )
        return None

    wallet = _wallet(user_id) if user_id else None
    _post_settle_hooks(user_id, wallet, actual_cost)
    return wallet


# Alias kept for compatibility with the plan's wording.
settle_job = settle_hold


def release_hold(hold_tx_id: str, reason: str = "cancelled_before_run") -> bool:
    """Release a hold without charging. Used for cancel-before-run flows.

    Idempotent: a hold that has already been settled or released is a
    no-op.
    """
    client = get_service_client()
    if client is None:
        return False
    try:
        client.rpc(
            "release_hold",
            {"p_hold_tx_id": hold_tx_id, "p_reason": reason},
        ).execute()
        return True
    except Exception:
        logger.error(
            "release_hold failed hold=%s reason=%s",
            hold_tx_id, reason, exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Auto-reload
# ---------------------------------------------------------------------------


def auto_reload_if_needed(user_id: str) -> Optional[str]:
    """Fire an off-session top-up if the user qualifies.

    Returns the reason string for the action taken, useful for logging
    and tests:

    * ``"not_enabled"`` (user has not opted in)
    * ``"above_threshold"`` (balance still above auto-reload threshold)
    * ``"no_payment_method"`` (no saved card or Stripe customer;
      auto-reload disabled)
    * ``"no_amount_configured"`` (reload amount unset; auto-reload disabled)
    * ``"rate_limited"`` (already reloaded once in the last 24h)
    * ``"monthly_cap"`` (current-month total plus reload would exceed cap)
    * ``"triggered"`` (off-session PaymentIntent dispatched)
    * ``"stripe_error"`` (the off-session charge failed; on a permanent
      failure such as a declined or unusable card, auto-reload is
      disabled and the user is emailed)
    * ``"missing_service_client"`` (no service-role client available)
    """
    client = get_service_client()
    if client is None:
        return "missing_service_client"
    wallet = _wallet(user_id)
    if not wallet:
        return "missing_service_client"
    if wallet.get("wallet_frozen"):
        # A frozen wallet (chargeback dispute) must not auto-reload, even
        # if a settle from a job that submitted just before the freeze
        # lands afterwards.
        return "wallet_frozen"
    if not wallet.get("auto_reload_enabled"):
        return "not_enabled"
    threshold = Decimal(str(wallet.get("auto_reload_threshold_usd") or 0))
    balance = Decimal(str(wallet.get("balance_usd") or 0))
    if balance >= threshold:
        return "above_threshold"
    if not wallet.get("stripe_payment_method_id") or not wallet.get(
        "stripe_customer_id"
    ):
        # An off-session charge needs both a saved card and the Stripe
        # customer it is attached to. Missing either means auto-reload
        # can never succeed, so disable it rather than fail on every
        # settle.
        try:
            client.table("user_wallets").update(
                {"auto_reload_enabled": False}
            ).eq("user_id", user_id).execute()
        except Exception:
            logger.warning(
                "auto_reload_if_needed: could not disable for %s",
                user_id, exc_info=True,
            )
        _send_email_safe(
            "send_auto_reload_failed_email",
            user_id=user_id, reason="no_payment_method",
        )
        return "no_payment_method"
    if _auto_reload_count_24h(user_id) >= 1:
        _send_email_safe("send_auto_reload_rate_limited_email", user_id=user_id)
        return "rate_limited"
    month_total = _auto_reload_total_month(user_id)
    reload_amount = Decimal(str(wallet.get("auto_reload_amount_usd") or 0))
    monthly_cap = Decimal(
        str(wallet.get("auto_reload_monthly_cap_usd")
            or DEFAULT_AUTO_RELOAD_MONTHLY_CAP_USD)
    )
    if reload_amount <= 0:
        # Misconfigured wallet. Disable auto-reload so it stops trying.
        try:
            client.table("user_wallets").update(
                {"auto_reload_enabled": False}
            ).eq("user_id", user_id).execute()
        except Exception:
            logger.warning(
                "auto_reload_if_needed: could not disable for %s",
                user_id, exc_info=True,
            )
        _send_email_safe(
            "send_auto_reload_failed_email",
            user_id=user_id, reason="no_amount_configured",
        )
        return "no_amount_configured"
    if month_total + reload_amount > monthly_cap:
        _send_email_safe(
            "send_auto_reload_monthly_cap_email",
            user_id=user_id, total_usd=month_total, cap_usd=monthly_cap,
        )
        return "monthly_cap"
    # Stripe off-session PaymentIntent. Wave 2 Agent E provides
    # :func:`billing.checkout.create_off_session_payment_intent`. Import
    # lazily so this module is testable without the Stripe SDK on path.
    try:
        from billing.checkout import (  # noqa: PLC0415
            create_off_session_payment_intent,
        )
    except Exception:
        logger.info(
            "auto_reload_if_needed: Stripe helper not present yet for %s",
            user_id,
        )
        return "triggered"
    try:
        create_off_session_payment_intent(
            stripe_customer_id=wallet.get("stripe_customer_id"),
            payment_method_id=wallet.get("stripe_payment_method_id"),
            amount_usd=reload_amount,
            metadata={"user_id": user_id, "kind": "auto_reload"},
        )
    except Exception as exc:
        # An off-session charge failure does not heal itself between
        # job settles. If it is permanent (declined or unusable card,
        # invalid Stripe customer) disable auto-reload so it stops
        # firing on every settle, and email the user so they can fix
        # the card and re-enable. Retryable failures (Stripe outage,
        # our API key missing) leave auto-reload on for the next settle.
        retryable = bool(getattr(exc, "retryable", False))
        reason = getattr(exc, "reason", "card_declined")
        logger.error(
            "auto_reload_if_needed: Stripe PI dispatch failed for %s "
            "(retryable=%s reason=%s)",
            user_id, retryable, reason, exc_info=True,
        )
        if not retryable:
            try:
                client.table("user_wallets").update(
                    {"auto_reload_enabled": False}
                ).eq("user_id", user_id).execute()
            except Exception:
                logger.warning(
                    "auto_reload_if_needed: could not disable auto-reload "
                    "after Stripe failure for %s", user_id, exc_info=True,
                )
            _send_email_safe(
                "send_auto_reload_failed_email",
                user_id=user_id, reason=reason,
            )
        return "stripe_error"
    return "triggered"


# ---------------------------------------------------------------------------
# Chargeback freeze
# ---------------------------------------------------------------------------


def freeze_wallet_on_dispute(user_id: str, dispute_id: str) -> bool:
    """Freeze the wallet so no new submissions can run while a dispute is open."""
    client = get_service_client()
    if client is None:
        return False
    try:
        client.table("user_wallets").update(
            {
                "wallet_frozen": True,
                "wallet_frozen_reason": f"chargeback_dispute:{dispute_id}",
            }
        ).eq("user_id", user_id).execute()
        _send_email_safe(
            "send_wallet_frozen_email",
            user_id=user_id, dispute_id=dispute_id,
        )
        _send_email_safe(
            "alert_ops_slack",
            event="wallet_frozen", user_id=user_id, dispute_id=dispute_id,
        )
        return True
    except Exception:
        logger.error(
            "freeze_wallet_on_dispute failed user=%s dispute=%s",
            user_id, dispute_id, exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Decorator (definition only; Agent F wires it to routes)
# ---------------------------------------------------------------------------


def requires_wallet(tool_slug: str, *, allow_zero: bool = False) -> Callable:
    """Flask decorator: gate a submit route on a successful preflight.

    The decorator looks up the current user via the same session
    shape ``shared.credits`` uses, computes the estimate via
    :func:`shared.wallet_estimates.estimated_cost_for_tool`, and either
    invokes the wrapped handler or redirects.

    NOTE: this decorator is defined here for completeness. It is NOT
    applied to any existing route in this Wave. Agent F wires it onto
    every GPU submit route in Wave 2 along with the rest of the route
    layer changes.

    ``allow_zero`` lets smoke-tier presets through even when the
    wallet balance is zero (smoke runs are free).
    """

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapped(*args: Any, **kwargs: Any):
            # Defer the Flask + estimate imports to call time so that
            # the decorator itself is importable from a non-Flask
            # context (unit tests, Celery workers).
            from flask import redirect, request, session, url_for  # noqa: PLC0415

            from .wallet_estimates import estimated_cost_for_tool  # noqa: PLC0415

            user_id = session.get("user_id")
            if not user_id:
                return redirect(url_for("login"))

            params: dict = {}
            try:
                params = request.form.to_dict() or {}
            except Exception:
                params = {}

            estimate = estimated_cost_for_tool(user_id, tool_slug, params)
            if allow_zero and estimate <= Decimal("0"):
                return f(*args, **kwargs)

            pre = wallet_preflight(user_id, tool_slug, estimate, params)
            if not pre.allow:
                _emit_preflight_email(user_id, tool_slug, pre)
                if pre.reason == REASON_INSUFFICIENT:
                    return redirect(
                        url_for("account") + "?insufficient_balance=1"
                    )
                if pre.reason == REASON_WALLET_FROZEN:
                    return redirect(url_for("account") + "?wallet_frozen=1")
                return redirect(
                    url_for("account") + f"?wallet_blocked={pre.reason}"
                )
            return f(*args, **kwargs)

        return wrapped

    return decorator


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _net_spend_usd(user_id: str, since: datetime) -> Decimal:
    """USD a user has actually spent on jobs since ``since``.

    Net spend nets each job's settlement against its hold::

        spend = sum(|hold|) - sum(|hold_release|) + sum(|charge|)

    ``hold`` rows commit the per-job estimate; ``hold_release`` rows
    return surplus (or the whole hold on a cancel-before-run);
    ``charge`` rows debit a true-up overrun. ``absorbed_variance`` is
    excluded because Ranomics, not the user, paid it.

    Absolute values are used so the figure is correct regardless of the
    sign a row was written with (the SQL ledger stores holds negative).
    Clamped at zero so a stray release without an in-window hold cannot
    produce a negative spend.

    This is the one canonical spend definition; the wallet overview and
    the sales funnel consume it.
    """
    client = get_service_client()
    if client is None:
        return Decimal("0")
    try:
        response = (
            client.table("wallet_transactions")
            .select("kind,amount_usd")
            .eq("user_id", user_id)
            .in_("kind", ["hold", "hold_release", "charge"])
            .gte("created_at", since.isoformat())
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
    except Exception:
        logger.warning(
            "net_spend_usd lookup failed for %s", user_id, exc_info=True
        )
        return Decimal("0")
    holds = releases = charges = Decimal("0")
    for r in rows:
        amount = Decimal(str(r.get("amount_usd") or 0)).copy_abs()
        kind = r.get("kind")
        if kind == "hold":
            holds += amount
        elif kind == "hold_release":
            releases += amount
        elif kind == "charge":
            charges += amount
    return max(Decimal("0"), holds - releases + charges)


def _spent_today_usd(user_id: str) -> Decimal:
    """Net USD spent on jobs since UTC midnight (the "Spent today" figure)."""
    start_of_day = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return _net_spend_usd(user_id, start_of_day)


def _auto_reload_count_24h(user_id: str) -> int:
    """How many auto-reload credits have been recorded in the last 24h."""
    client = get_service_client()
    if client is None:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    try:
        response = (
            client.table("wallet_transactions")
            .select("id")
            .eq("user_id", user_id)
            .eq("kind", "auto_reload")
            .gte("created_at", cutoff.isoformat())
            .execute()
        )
        return len(list(getattr(response, "data", None) or []))
    except Exception:
        logger.warning(
            "auto_reload_count_24h failed for %s", user_id, exc_info=True
        )
        return 0


def _auto_reload_total_month(user_id: str) -> Decimal:
    """Sum of auto-reload credits in the current calendar month (UTC)."""
    client = get_service_client()
    if client is None:
        return Decimal("0")
    now = datetime.now(timezone.utc)
    month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    try:
        response = (
            client.table("wallet_transactions")
            .select("amount_usd")
            .eq("user_id", user_id)
            .eq("kind", "auto_reload")
            .gte("created_at", month_start.isoformat())
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        return sum(Decimal(str(r.get("amount_usd") or 0)) for r in rows)
    except Exception:
        logger.warning(
            "auto_reload_total_month failed for %s", user_id, exc_info=True
        )
        return Decimal("0")


def _post_settle_hooks(
    user_id: Optional[str], wallet: Optional[dict], actual_cost: Decimal
) -> None:
    """Run the post-settle side effects (auto-reload, emails, funnel)."""
    if not user_id:
        return
    try:
        auto_reload_if_needed(user_id)
    except Exception:
        logger.warning(
            "auto_reload_if_needed raised after settle for %s",
            user_id, exc_info=True,
        )
    balance = Decimal(str((wallet or {}).get("balance_usd") or 0))
    if balance < LOW_BALANCE_EMAIL_THRESHOLD:
        _send_email_safe(
            "send_low_balance_email", user_id=user_id, balance_usd=balance
        )
    try:
        from .wallet_funnel import _maybe_trigger_funnel_alerts  # noqa: PLC0415

        _maybe_trigger_funnel_alerts(user_id, actual_cost)
    except Exception:
        logger.warning(
            "funnel alerts raised for %s", user_id, exc_info=True
        )


def _emit_preflight_email(
    user_id: str, tool_slug: str, pre: PreflightResult
) -> None:
    """Dispatch the matching email when a preflight check fails."""
    if pre.allow:
        return
    if pre.reason == REASON_PER_TOOL_CAP or pre.reason == REASON_SELF_SERVE_CEILING:
        _send_email_safe(
            "send_job_capped_email",
            user_id=user_id,
            tool_slug=tool_slug,
            attempted_usd=pre.estimated_cost_usd,
            cap_usd=pre.hard_cap_usd,
        )


def _send_email_safe(func_name: str, **kwargs: Any) -> None:
    """Lazy lookup + invoke an email sender; swallow any errors.

    Used so email failures never break wallet bookkeeping. The Wave 2
    Agent G fill-in replaces the stubs with real Resend calls.
    """
    try:
        from shared import email as email_module  # noqa: PLC0415

        sender = getattr(email_module, func_name, None)
        if sender is None:
            logger.warning(
                "wallet email helper missing: %s (kwargs=%r)", func_name, kwargs
            )
            return
        sender(**kwargs)
    except Exception:  # pragma: no cover (email is best-effort)
        logger.warning(
            "wallet email dispatch failed: %s (kwargs=%r)",
            func_name, kwargs, exc_info=True,
        )
