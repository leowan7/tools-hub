"""USD wallet primitives for the Ranomics tools hub.

Replaces the per-target Workspace SaaS model with a pre-auth wallet:
users top up a USD balance, every job places an atomic hold for its
estimated cost before enqueue, and settlement on completion releases
any surplus or debits any variance up to a per-tool hard cap.

Lifecycle
---------
::

    record_signup_credit()                  # one-time SIGNUP_CREDIT_USD grant
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
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from functools import wraps
from typing import Any, Callable, Mapping, Optional

from shared.credits import get_service_client
from shared.wallet_estimates import apply_min_charge
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
#
# Raised 5.00 -> 15.00 (2026-08-18). The tool pages now recommend a named
# "pilot" as a new user's first run, and the pricier pilots cost well over $5
# -- so a $5 grant meant the very first action the site recommended cost more
# than the balance it had just advertised, and a new user's real first step was
# a top-up. 15.00 still clears the most expensive pilot: the priciest are
# proteina and pxdesign at $12.58, leaving $2.42. The 2026-09-01 gpu_class
# corrections moved the band (bindcraft $4.37 -> $6.29, pxdesign $8.74 ->
# $12.58, boltzgen $8.74 -> $6.07). esmfold2-design's pilot is $9.86, under
# both. opendde is the one expensive tool with no PILOT at all ($14.79 a run;
# see the note in tools/opendde/meta.py for why it publishes no pilot card).
# (That paragraph is kept for the history. Its SIZING RULE was wrong -- see
# below.)
#
# Raised 15.00 -> 20.00 (2026-09-10). This is a LIVE DEFECT FIX, not a
# precaution, and an earlier draft of this comment got that backwards by
# telling the story as if esmfold2-design's session-ceiling change had created
# the problem. Measured at the commit before that change, with the credit at
# $15.00, the 1-unit cushioned holds were:
#
#     proteina         $15.0000    headroom $0.0000
#     opendde          $15.0000    headroom $0.0000
#     esmfold2-design  $14.7921    headroom $0.2079
#
# proteina and opendde were ALREADY at exactly zero headroom in production,
# refusing any new user who had spent a single cent. esmfold2-design was the
# one that still had room, and it joined them when its ceiling moved
# 3600 -> 5400 s and its floor reached the $15/seed cap.
#
# WHY THE OLD RULE MISSED IT. It sized the credit against the displayed PILOT
# PRICE. What admits a job is the CUSHIONED HOLD, and on any tool carrying a
# worst_case_gpu_seconds floor the hold sits well above the price:
# esmfold2-design displays $9.87 (raw estimate $9.8614; the panel ceils to
# cents, templates/wallet/_partials.html fmtUp) and holds $15.00. A credit that
# clears every price can still refuse those tools outright.
#
# WHERE THE REFUSAL ACTUALLY HAPPENS, since this is easy to get wrong: the
# guard calls wallet_preflight on the point ESTIMATE first
# (shared/wallet_guard.py), which passes. It then calls reserve_hold with the
# cushioned hold, and reserve_hold runs its OWN wallet_preflight on THAT amount
# and returns None -- so the refusal is in Python and the SQL is never reached.
# (try_hold_for_job does carry a balance check of its own, but the live
# definition is migration 0035, not the 0020 an earlier draft of this comment
# cited.) The guard then re-preflights against the held amount to render an
# honest deficit. Note hold >= estimate always, but NOT strictly: both are
# clamped by the same compute_hard_cap, so they are EQUAL whenever the estimate
# saturates the cap -- boltzgen at its own documented pool size
# (num_designs=200) quotes $300.00 and holds $300.00.
#
# THE OLD RULE was that this constant must clear the largest 1-unit hold of
# every tool, which is why it went to 20.00. That rule is retired; see below.
# Past one unit no credit could cover a scaled-up submit anyway: af2's batch
# holds $39.32 at MAX_BATCH=50 records. Note the panel quotes the PRICE while
# the HOLD is what refuses -- a price/hold split in blueprints/wallet.py that
# this constant cannot fix.
#
# Cut 20.00 -> 5.00 (2026-09-28, Leo). At $20 nobody had ever run out of free
# credit (the 2026-09-28 funnel review, section 7; that document is not in
# this repository), so the credit never led anyone
# to a top-up. At $5 the structure prediction and sequence tools still run
# from the credit with $1.00 spare, and the binder design tools with a hold
# over $4.00 (plus opendde) need a top-up; the copy says so.
# ``tests/test_signup_credit_covers_smallest_run.py`` pins which tools fall on
# each side (its NEEDS_TOPUP set), so a price, cap or credit change that moves
# one fails there. Existing grants are not touched: expiry reads the
# signup_credit ledger row, not this constant.
#
# This number is user-visible in ~18 places. Do NOT hardcode it in copy:
# templates read it through the ``signup_credit`` jinja global and Python
# callers import this constant, both sourced from here. There is no env
# override -- WALLET_SIGNUP_CREDIT_USD was removed 2026-08-18 because it
# changed only the welcome email, never the grant.
SIGNUP_CREDIT_USD = Decimal("5.00")

# Unspent signup credit is removed this many days after the grant. The same
# 30 is the column default in supabase/migrations/0043_signup_credit_expiry.sql;
# tests/test_signup_credit_expiry.py fails if the two differ.
SIGNUP_CREDIT_EXPIRY_DAYS = 30

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

# Not the cap billing uses: the parameter-scaled cap saturates at
# ``TOOL_SPECS[...].absolute_cap_usd`` instead
# (shared/wallet_estimates.py::compute_hard_cap). colabfold differs: 500 here,
# 200 in TOOL_SPECS.
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
    "iggm":        Decimal("75.00"),
    # proteina: per-shard ceiling. One shard is one 7200 s A100-80GB container
    # (~$12.58 marked-up physical max), so $60 sits well above any real single
    # shard and never clips a legit charge while still bounding a pricing bug.
    # A campaign runs many shards; total exposure is bounded by the prepaid
    # wallet (fund-and-drain), not this per-shard cap. Mirrors TOOL_SPECS.
    "proteina":    Decimal("60.00"),
    # esmfold2-design: atomic H100 binder-design tool that fans out on n_seeds
    # (one container per seed). $1000 covers the N_SEEDS_MAX=64 submit, which
    # settle can charge at most $960 for — 64 x the $15/seed base_hard_cap, the
    # binding clamp. Note that is a CAP-bound max, not a physical one: since
    # _MAX_SESSION_S went to 5400 s, 64 full sessions are $1420 of marked-up
    # compute, so the caps now sit BELOW physical and Ranomics absorbs the
    # difference on a pathological max run rather than clipping the customer.
    # (At the old 3600 s ceiling physical was $946 and sat under both caps.)
    # Mirrors TOOL_SPECS absolute_cap_usd.
    "esmfold2-design": Decimal("1000.00"),
    # opendde: atomic H100 co-folding tool. One container per job, physically
    # capped at _MAX_SESSION_S=3600 s ($14.79 marked-up worst case), so $15 is
    # the TRUE per-job ceiling — there is no fan-out that could push it higher.
    # Mirrors TOOL_SPECS absolute_cap_usd. (Deviates from the plan's $150, which
    # assumed a multi-shard scaling model that does not fit a single container.)
    "opendde":     Decimal("15.00"),
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
    """Customer-facing charge in USD: raw cost times markup, raised to
    ``MIN_CHARGE_USD`` when positive. Zero GPU-seconds stay $0, and refunds go
    through :func:`release_hold`, which never calls this."""
    raw = compute_modal_cost_usd(gpu_seconds, gpu_class)
    return apply_min_charge(
        (raw * WALLET_MARKUP).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    )


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
# Signup credit expiry
# ---------------------------------------------------------------------------

_CREDIT_INFLOW_KINDS = frozenset({"signup_credit", "topup", "auto_reload", "promo"})
_LEDGER_PAGE = 1000


def unspent_signup_credit(rows: list[Mapping], balance: Decimal) -> Decimal:
    """How much of the signup credit is still unspent, given the whole ledger.

    ``rows`` are the user's ``wallet_transactions`` rows with signed amounts as
    the SQL functions write them (holds negative). Spend is minus the sum of
    every row after the grant that is not money coming in: holds net of their
    releases, charges, freezes and negative adjustments. Credit is treated as
    spent first, so paid money is only ever spent after it. The result is
    ``grant - spend`` floored at 0 and capped at ``balance``; it is 0 when
    there is no grant or an expiry row already exists.
    """
    grant_id = None
    grant = Decimal("0")
    for r in rows:
        if r.get("kind") == "signup_credit_expiry":
            return Decimal("0")
        if r.get("kind") == "signup_credit" and grant_id is None:
            grant_id = r.get("id")
            grant = Decimal(str(r.get("amount_usd") or 0))
    if grant_id is None:
        return Decimal("0")
    spend = Decimal("0")
    for r in rows:
        if r.get("id") is None or r["id"] <= grant_id:
            continue
        amount = Decimal(str(r.get("amount_usd") or 0))
        kind = r.get("kind")
        if kind in _CREDIT_INFLOW_KINDS or (kind == "adjustment" and amount > 0):
            continue
        spend -= amount
    return max(Decimal("0"), min(grant - spend, balance))


def _ledger_rows(client, user_id: str) -> list[dict]:
    rows: list[dict] = []
    while True:
        resp = (
            client.table("wallet_transactions")
            .select("id,kind,amount_usd,parent_tx_id")
            .eq("user_id", user_id)
            .order("id")
            .range(len(rows), len(rows) + _LEDGER_PAGE - 1)
            .execute()
        )
        page = list(getattr(resp, "data", None) or [])
        rows.extend(page)
        if len(page) < _LEDGER_PAGE:
            return rows


def _has_open_hold(rows: list[Mapping]) -> bool:
    parents = {
        r.get("parent_tx_id") for r in rows
        if r.get("parent_tx_id") is not None and r.get("kind") != "run_debit"
    }
    return any(r.get("kind") == "hold" and r.get("id") not in parents for r in rows)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def signup_credit_status(user_id: str, wallet: Optional[Mapping] = None) -> Optional[dict]:
    """``{"remaining_usd", "grant_usd", "expires_at"}`` while unspent signup credit remains.

    None when nothing is left, the expiry already ran, or the lookup failed.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        if wallet is None:
            wallet = _wallet(user_id)
        if not wallet or wallet.get("signup_credit_expired_at"):
            return None
        expires_at = _parse_ts(wallet.get("signup_credit_expires_at"))
        if expires_at is None:
            return None
        rows = _ledger_rows(client, user_id)
        balance = sum((Decimal(str(r.get("amount_usd") or 0)) for r in rows), Decimal("0"))
        remaining = unspent_signup_credit(rows, balance)
    except Exception:
        logger.warning("signup_credit_status failed for %s", user_id, exc_info=True)
        return None
    if remaining <= 0:
        return None
    grant = next(Decimal(str(r.get("amount_usd"))) for r in rows if r.get("kind") == "signup_credit")
    return {"remaining_usd": remaining, "grant_usd": grant, "expires_at": expires_at}


def expire_signup_credit(user_id: str) -> str:
    """Remove this user's unspent signup credit if its expiry date has passed.

    Returns the outcome string of the ``expire_signup_credit`` SQL function
    (``expired``, ``not_due``, ``already_expired``, ``ledger_moved``,
    ``hold_open``, ...), ``hold_open`` without calling it when a hold is
    visible here, or ``error``. Anything but ``expired`` / ``already_expired``
    leaves the wallet untouched for the next run to retry. The SQL function
    re-checks due date, open holds and the ledger tail under the wallet row
    lock (supabase/migrations/0043_signup_credit_expiry.sql), which is what
    makes a second concurrent run a no-op.
    """
    client = get_service_client()
    if client is None:
        return "error"
    try:
        rows = _ledger_rows(client, user_id)
        if _has_open_hold(rows):
            return "hold_open"
        balance = sum((Decimal(str(r.get("amount_usd") or 0)) for r in rows), Decimal("0"))
        amount = unspent_signup_credit(rows, balance).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        last_id = rows[-1]["id"] if rows else None
        resp = client.rpc(
            "expire_signup_credit",
            {
                "p_user_id": user_id,
                "p_amount_usd": str(amount),
                "p_seen_last_tx_id": last_id,
            },
        ).execute()
        return str(getattr(resp, "data", None) or "error")
    except Exception:
        logger.warning("expire_signup_credit failed for %s", user_id, exc_info=True)
        return "error"


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


# ---------------------------------------------------------------------------
# Live runs (supabase/migrations/0045_live_run_debits.sql): a $0 anchor at
# submit, a run_debit per tick that takes money, one closing row at settle.
# ---------------------------------------------------------------------------


def _rpc_data(name: str, args: dict) -> Any:
    client = get_service_client()
    if client is None:
        return None
    try:
        return getattr(client.rpc(name, args).execute(), "data", None)
    except Exception:
        logger.error("%s RPC failed: %s", name, args, exc_info=True)
        return None


def open_live_run(user_id: str, tool_slug: str) -> Optional[str]:
    """The new anchor's id, or None when the SQL refused or the call failed."""
    data = _rpc_data("open_live_run", {"p_user_id": user_id, "p_tool_slug": tool_slug})
    if isinstance(data, list):
        data = data[0] if data else None
    return str(data) if data is not None else None


def live_due_usd(
    tool_slug: str,
    gpu_seconds: float,
    gpu_class: Optional[str],
    params: Optional[Mapping[str, object]] = None,
) -> Decimal:
    """What a live run owes for ``gpu_seconds``, clamped to the tool's cap as settle_hold clamps."""
    from .wallet_estimates import compute_hard_cap  # noqa: PLC0415

    return min(
        compute_charge_usd(gpu_seconds, gpu_class),
        compute_hard_cap(tool_slug, dict(params or {})),
    )


def _decimals(data: Mapping, keys: tuple) -> dict:
    return {k: Decimal(str(data.get(k) or 0)) for k in keys}


def debit_live_run(
    hold_tx_id: str,
    user_id: str,
    due_usd: Decimal,
    gpu_seconds: float,
    gpu_class: Optional[str],
) -> Optional[dict]:
    """Bring what the run has taken up to ``due_usd``, as far as the balance goes.

    Returns ``{"settled", "debited", "taken", "short", "balance_after"}``
    from the SQL, or None when the call failed or the anchor is unknown. A
    debit that took money runs the post-settle hooks for that amount.
    """
    data = _rpc_data("debit_live_run", {
        "p_hold_tx_id": hold_tx_id,
        "p_due_usd": str(due_usd),
        "p_gpu_seconds": float(gpu_seconds or 0),
        "p_gpu_class": gpu_class,
    })
    if not isinstance(data, Mapping):
        return None
    out = _decimals(data, ("debited", "taken", "short", "balance_after"))
    out["settled"] = data.get("settled") is True
    if out["debited"] > 0:
        _post_settle_hooks(user_id, {"balance_usd": out["balance_after"]}, out["debited"])
    return out


def settle_live_run(
    hold_tx_id: str,
    gpu_seconds: float,
    gpu_class: Optional[str],
    params: Optional[Mapping[str, object]] = None,
    failure_reason: Optional[str] = None,
    refund: bool = False,
) -> Optional[dict]:
    """Close a live run at its final cost, or at $0 when ``refund``.

    Returns the SQL's ``{"settled_before", "final", "taken", "charged",
    "released", "absorbed", "balance_after"}``, or None on a failure.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        hold = getattr(
            client.table("wallet_transactions")
            .select("user_id,tool_slug")
            .eq("id", hold_tx_id)
            .maybe_single()
            .execute(),
            "data", None,
        )
    except Exception:
        logger.error("settle_live_run: anchor lookup failed id=%s", hold_tx_id, exc_info=True)
        return None
    if not hold:
        logger.error("settle_live_run: anchor not found id=%s", hold_tx_id)
        return None
    final = Decimal("0") if refund else live_due_usd(
        hold.get("tool_slug") or "", gpu_seconds, gpu_class, params,
    )
    data = _rpc_data("settle_live_run", {
        "p_hold_tx_id": hold_tx_id,
        "p_final_due_usd": str(final),
        "p_gpu_seconds": float(gpu_seconds or 0),
        "p_gpu_class": gpu_class,
        "p_failure_reason": failure_reason,
    })
    if not isinstance(data, Mapping):
        return None
    out = _decimals(data, ("final", "taken", "charged", "released", "absorbed", "balance_after"))
    out["settled_before"] = data.get("settled_before") is True
    _post_settle_hooks(hold.get("user_id"), {"balance_usd": out["balance_after"]}, out["charged"])
    return out


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

# Last time each user was mailed each no-charge auto-reload notice.
# Keyed on (user_id, notice) rather than user_id alone: the three
# branches that mail carry different wording, and one key would let the
# first to fire inside the window suppress another and leave the user
# holding the wrong explanation. Pinned by
# tests/test_wallet.py::test_the_skip_notices_do_not_share_a_throttle_slot
# ponytail: per-process dict, and the check-then-set below is unlocked, so
# the ceiling is one mail per notice per worker per period and two settles
# racing inside one worker can both pass the check. A users-table column with
# a conditional update would make it exact; the dict turns "one mail per job
# settle" into that ceiling, which is the part that matters during a wave.
_SKIP_MAIL_SENT: dict[tuple[str, str], datetime] = {}

# How long each notice stays suppressed. The periods differ because the
# conditions do. The rate_limited notice
# describes one that cannot change inside its own window -- dispatch
# is bounded to one per 24h by ``_claim_auto_reload_dispatch``, whose cutoff
# is that window -- so re-mailing it inside the window repeats a sentence
# that is still true and still unactionable; at one hour a user with jobs
# settling all day gets it ~24 times a day, which is not far off the
# per-settle storm this throttle exists to stop. The refusal is the opposite
# case: the unreadable guard behind it can clear and recur, each recurrence
# is news, and the brief for that notice is that it be visible. The
# monthly_cap notice takes 24h because ``_auto_reload_total_month`` sums the
# calendar month, so the cap stays reached until the month rolls over or the
# user changes their cap or reload amount. Pinned by
# tests/test_wallet.py::test_each_skip_notice_is_throttled_for_its_own_period
_SKIP_MAIL_EVERY = {
    "rate_limited": timedelta(hours=24),
    "guard_unavailable": timedelta(hours=1),
    "monthly_cap": timedelta(hours=24),
}


def _mail_auto_reload_skipped(
    user_id: str, notice: str, sender: str, **kwargs: Any
) -> None:
    """Mail a no-charge auto-reload notice about once per user per period.

    Every notice routed here is reached once per job settle: Modal
    completions arrive in waves, so without this a user below their
    threshold gets one identical email per settling job.

    Each caller logs on every occurrence, unthrottled, so suppressing the
    mail suppresses no record: :func:`_refuse_auto_reload_unverified` at
    ERROR, and the ``reloads_24h >= 1`` and monthly-cap branches of
    :func:`auto_reload_if_needed` at INFO. Those two logs exist because of
    this throttle -- the per-settle email used to be each branch's only
    trace. All three are pinned by the settle-wave tests in
    ``tests/test_wallet.py`` (``..._against_a_dead_ledger_mails_once``,
    ``..._inside_the_24h_window_mails_once`` and
    ``..._over_the_monthly_cap_mails_once``), which assert five log records
    against one email.

    ``notice`` is part of the throttle key, so no notice suppresses another
    (``test_the_skip_notices_do_not_share_a_throttle_slot``), and it picks
    the period from ``_SKIP_MAIL_EVERY``. ``sender`` and ``kwargs`` go to
    ``_send_email_safe`` with ``user_id``. The timestamp is recorded before
    the send and ``_send_email_safe`` swallows delivery failures, so a dead
    mailer costs the user that period's email; the caller's log is the
    durable record.

    "About" is deliberate: the ceiling is per notice per worker, and the
    check-then-set below is unlocked (see the comment at ``_SKIP_MAIL_SENT``),
    so a wave can still yield one mail per worker and two settles racing
    inside one worker can yield two. The sequential case is what the tests
    pin. Either way the cost is a duplicate notification: no money path reads
    this dict.
    """
    now = datetime.now(timezone.utc)
    key = (user_id, notice)
    last = _SKIP_MAIL_SENT.get(key)
    if last is not None and now - last < _SKIP_MAIL_EVERY[notice]:
        return
    _SKIP_MAIL_SENT[key] = now
    _send_email_safe(sender, user_id=user_id, **kwargs)


def _refuse_auto_reload_unverified(user_id: str, guard: str) -> str:
    """Refuse an auto-reload whose safety guard could not be read.

    Both pre-charge guards -- :func:`_auto_reload_count_24h` and
    :func:`_auto_reload_total_month` -- return ``None`` on an unreadable
    ledger, and this is where that ``None`` becomes a refusal instead of a
    pass. It returns before the dispatch claim and before the Stripe call, so
    nothing is charged and no 24h window is burned.

    Auto-reload is deliberately left ENABLED: an unreadable ledger says
    nothing about the user's card, so disabling here (as the
    ``no_payment_method`` and permanent-Stripe-failure branches do) would make
    a Supabase blip cost the user a manual re-enable.

    Every refusal is logged at ERROR; the email is throttled by
    :func:`_mail_auto_reload_skipped`.
    """
    logger.error(
        "auto_reload_if_needed: could not read %s for %s; refusing to "
        "auto-reload rather than charge past a limit it cannot verify.",
        guard, user_id,
    )
    _mail_auto_reload_skipped(
        user_id, "guard_unavailable", "send_auto_reload_rate_limited_email",
        reason="a safety check on your account could not be completed",
    )
    return "guard_unavailable"


def auto_reload_if_needed(user_id: str) -> Optional[str]:
    """Fire an off-session top-up if the user qualifies.

    Returns the reason string for the action taken, useful for logging
    and tests:

    * ``"not_enabled"`` (user has not opted in)
    * ``"above_threshold"`` (balance still above auto-reload threshold)
    * ``"no_payment_method"`` (no saved card or Stripe customer;
      auto-reload disabled)
    * ``"no_amount_configured"`` (reload amount unset; auto-reload disabled)
    * ``"rate_limited"`` (a reload already landed, or a charge was already
      dispatched, within the last 24h)
    * ``"monthly_cap"`` (current-month total plus reload would exceed cap)
    * ``"guard_unavailable"`` (a pre-charge guard could not be read, so the
      user could not be shown to be under both limits; see
      :func:`_refuse_auto_reload_unverified`)
    * ``"triggered"`` (off-session PaymentIntent dispatched)
    * ``"stripe_error"`` (the off-session charge failed; on a permanent
      failure such as a declined or unusable card, auto-reload is
      disabled and the user is emailed)
    * ``"missing_service_client"`` (no service-role client available)

    Bounded to one off-session charge per rolling 24h by
    :func:`_claim_auto_reload_dispatch`, which is taken BEFORE the Stripe call
    and never released. The price of failing closed: when the charge then
    fails (for ANY reason) auto-reload will not retry until the window rolls,
    and the user tops up by hand in the meantime. Preferred over releasing the
    claim, which would risk a second charge every time a dispatch reached
    Stripe but the response did not reach us.

    That covers a case worth naming, because it is the one a user notices. A
    DECLINED card takes the permanent branch below: auto-reload is disabled
    and the user is emailed to fix the card. If they fix it and re-enable
    inside the window, the next settle is silently answered "rate_limited" —
    no charge, and no mail — for up to 24h after we asked them to act. Stripe
    definitively did not charge in that case, so releasing the claim there
    would be safe and would remove the dead wait, EXCEPT that
    ``_classify_off_session_error``
    (billing/checkout.py::_classify_off_session_error) also routes UNKNOWN
    failures to the same non-retryable branch, and an unknown failure may well
    have charged the card. Two of its reasons are already unambiguous —
    ``expired_card`` and ``insufficient_funds`` reach the caller only from a
    ``CardError`` carrying that code, so neither one charged — but plain
    ``card_declined`` is also what the unknown fallback returns, and that is
    the reason a real decline usually carries. Releasing the claim on the two
    safe reasons alone would be sound; it is not in this change, and until it
    is, the dead wait is the safe side and is left in place knowingly.

    The monthly cap rides on the same claim rather than getting one of its
    own, and is only partly bounded by it. The cap reads SETTLED
    ``auto_reload`` credits, so it under-reads by every charge whose webhook
    has not landed. The claim bounds dispatches to one per 24h WINDOW, which
    is not the same as one outstanding: if auto-reload credits stop settling
    while job settles keep arriving, N windows dispatch N charges, the month
    totals N × ``reload_amount``, and the cap is over-run by whatever that
    total exceeds it by. At the shipped $1000 default with a $50 reload, 30
    windows charge $1500 — $500 past the cap, not one reload past it.

    An earlier version of this docstring claimed at most one charge could be
    in flight, so the cap could be overshot by at most a single
    ``reload_amount``. That was wrong, it conflated one-per-window with
    one-outstanding, and QC reproduced the counterexample: cap $100, reload
    $50, webhook never lands, four dispatches across four windows, $200
    charged — $100 past the cap, two reloads' worth. Do not restore it.

    What the claim does buy the cap is the wave: before it, one stale read let
    a whole settle wave through at once, so the overshoot had no bound in time
    at all. Closing the rest needs the cap to count DISPATCHED rather than
    settled credits, which is a second migration and is deliberately not in
    this change.
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
    reloads_24h = _auto_reload_count_24h(user_id)
    if reloads_24h is None:
        return _refuse_auto_reload_unverified(
            user_id, "the 24h auto-reload count"
        )
    if reloads_24h >= 1:
        # Logged on every settle, while the email is throttled -- to one a
        # DAY on this branch, the "rate_limited" period in
        # `_SKIP_MAIL_EVERY`. Before the throttle the email
        # WAS the record of this branch; this line is what replaces it.
        logger.info(
            "auto_reload_if_needed: %s already auto-reloaded in the last 24h;"
            " skipping", user_id,
        )
        _mail_auto_reload_skipped(
            user_id, "rate_limited", "send_auto_reload_rate_limited_email"
        )
        return "rate_limited"
    month_total = _auto_reload_total_month(user_id)
    if month_total is None:
        return _refuse_auto_reload_unverified(
            user_id, "this month's auto-reload total"
        )
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
        logger.info(
            "auto_reload_if_needed: %s at the monthly auto-reload cap "
            "(month %s + reload %s > cap %s); skipping",
            user_id, month_total, reload_amount, monthly_cap,
        )
        _mail_auto_reload_skipped(
            user_id, "monthly_cap", "send_auto_reload_monthly_cap_email",
            total_usd=month_total, cap_usd=monthly_cap,
        )
        return "monthly_cap"
    # Resolved BEFORE the claim, though it reads like setup. This import has
    # no side effects, and its failure path returns without charging -- so
    # taken after the claim it would burn a user's whole 24h window on a
    # dispatch that never happened, and report "triggered" for it. Nothing
    # between the claim and the charge may be able to return early.
    # `test_a_missing_billing_module_does_not_burn_the_dispatch_window` pins
    # THIS import's position and nothing further -- a newly added step here
    # with its own early return would not red it. The invariant is wider than
    # its guard.
    #
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
    # Last gate before money moves, and the authoritative one. Placed after
    # every cheap reject above so a wallet that was never going to reload
    # (above threshold, capped, misconfigured) does not burn its 24h window.
    #
    # No email here. The count-based branch above owns the user-facing
    # rate-limit notice; losing the claim means a dispatch went out within the
    # last 24h that the ledger cannot see yet, and telling someone whose card
    # was charged seconds ago that we declined to top them up would be false —
    # the top-up is already on its way. The exception is the declined-card
    # case documented above, where the silence is a real cost.
    if not _claim_auto_reload_dispatch(user_id):
        logger.info(
            "auto_reload_if_needed: dispatch claim already held for %s "
            "(charge in flight or settled within 24h); not charging again.",
            user_id,
        )
        return "rate_limited"
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
                return redirect(url_for("auth.login"))

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
                        url_for("auth.account") + "?insufficient_balance=1"
                    )
                if pre.reason == REASON_WALLET_FROZEN:
                    return redirect(url_for("auth.account") + "?wallet_frozen=1")
                return redirect(
                    url_for("auth.account") + f"?wallet_blocked={pre.reason}"
                )
            return f(*args, **kwargs)

        return wrapped

    return decorator


def job_spend_by_hold(user_id: str, hold_ids: list[str]) -> dict[str, Optional[dict]]:
    """What the ledger has taken for each hold: ``{hold_id: {"usd", "settled", "held", "taken"}}``.

    ``usd`` is minus the sum of ``amount_usd`` over the hold and every row
    whose ``parent_tx_id`` is that hold, the same group net the wallet page
    annotates (``blueprints/wallet.py::_build_tx_lineage_annotations``). ``settled`` is
    whether any such child row other than a ``run_debit`` exists; without one,
    ``usd`` is the amount still reserved, or for a live run the amount taken so
    far. ``held`` is minus the hold row's own amount (zero when that row was
    not returned, and zero for a live run's anchor). ``taken`` is what the
    hold's ``run_debit`` rows took. A hold whose rows carry an unreadable amount maps to
    ``None``; a hold the ledger returned no row for is absent. A failed
    lookup returns ``{}``.
    """
    ids = [str(h) for h in hold_ids if h]
    client = get_service_client()
    if client is None or not ids:
        return {}
    try:
        rows = []
        for column in ("id", "parent_tx_id"):
            resp = (
                client.table("wallet_transactions")
                .select("id,parent_tx_id,amount_usd,kind")
                .eq("user_id", user_id)
                .in_(column, ids)
                .execute()
            )
            rows.extend(getattr(resp, "data", None) or [])
    except Exception:
        logger.warning("job_spend_by_hold lookup failed for %s", user_id, exc_info=True)
        return {}
    out: dict[str, Optional[dict]] = {}
    seen: set = set()
    for r in rows:
        if not isinstance(r, Mapping) or r.get("id") in seen:
            continue
        seen.add(r.get("id"))
        parent = r.get("parent_tx_id")
        key = str(parent) if parent is not None else str(r.get("id"))
        if key not in ids:
            continue
        if key in out and out[key] is None:
            continue
        try:
            amount = Decimal(str(r.get("amount_usd")))
        except (ArithmeticError, ValueError, TypeError):
            amount = None
        if amount is None or not amount.is_finite():
            out[key] = None
            continue
        entry = out.setdefault(
            key,
            {"usd": Decimal("0"), "settled": False, "held": Decimal("0"), "taken": Decimal("0")},
        )
        entry["usd"] -= amount
        if r.get("kind") == "run_debit":
            entry["taken"] -= amount
        elif parent is not None:
            entry["settled"] = True
        else:
            entry["held"] = -amount
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _net_spend_usd(user_id: str, since: datetime) -> Decimal:
    """USD a user has actually spent on jobs since ``since``.

    Net spend nets each job's settlement against its hold::

        spend = sum(|hold|) - sum(|hold_release|) + sum(|charge|) + sum(|run_debit|)

    ``hold`` rows commit the per-job estimate; ``hold_release`` rows
    return surplus (or the whole hold on a cancel-before-run);
    ``charge`` rows debit a true-up overrun; ``run_debit`` rows are what a
    live run took while it ran. ``absorbed_variance`` is
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
            .in_("kind", ["hold", "hold_release", "charge", "run_debit"])
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
        elif kind in ("charge", "run_debit"):
            charges += amount
    return max(Decimal("0"), holds - releases + charges)


def _spent_today_usd(user_id: str) -> Decimal:
    """Net USD spent on jobs since UTC midnight (the "Spent today" figure)."""
    start_of_day = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return _net_spend_usd(user_id, start_of_day)


def _auto_reload_count_24h(user_id: str) -> Optional[int]:
    """How many auto-reload credits have been recorded in the last 24h.

    ``None`` means the ledger could not be read. It is a separate value from
    ``0`` on purpose: every integer this returns is a number the caller
    compares against a limit, and the smallest of them -- ``0`` -- reads as
    permission to charge. Returning ``0`` for an unreadable ledger, which is
    what this did before, turned a Supabase blip into a pass on a money guard.

    The refusal lives in the caller, not here:
    :func:`auto_reload_if_needed` routes ``None`` to
    :func:`_refuse_auto_reload_unverified`, and
    ``tests/test_wallet.py::test_auto_reload_refuses_when_the_24h_count_cannot_be_read``
    is what fails if that route is removed.
    """
    client = get_service_client()
    if client is None:
        return None
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
        rows = getattr(response, "data", None)
        if rows is None:
            return None
        return len(list(rows))
    except Exception:
        logger.warning(
            "auto_reload_count_24h failed for %s", user_id, exc_info=True
        )
        return None


def _claim_auto_reload_dispatch(user_id: str) -> bool:
    """Reserve this user's one auto-reload dispatch for the next 24h.

    Returns True iff this call won the claim and may charge the card.

    :func:`_auto_reload_count_24h` cannot bound dispatches on its own. The
    ``kind='auto_reload'`` row it counts is written by :func:`top_up_wallet`
    from the ``payment_intent.succeeded`` handler in ``webhooks/stripe.py``,
    so it appears only after Stripe settles the charge. Between dispatch and
    that webhook the count still reads 0, and since :func:`_post_settle_hooks`
    calls :func:`auto_reload_if_needed` on every job settle — and Modal
    completions arrive in waves — two settles a second apart both read 0 and
    both used to fire their own PaymentIntent. Nothing downstream caught it:
    ``billing.checkout.create_off_session_payment_intent`` sends no Stripe
    idempotency key, and :func:`top_up_wallet` dedups on ``stripe_event_id``,
    which catches webhook redelivery rather than two distinct charges.

    Claiming at dispatch time closes that window. The filter is on the
    PRE-update value, so Postgres re-evaluates it after taking the row lock
    and exactly one of several concurrent callers wins — the same
    compare-and-set shape as ``shared.compute_campaigns._cas_transition``.
    Sequential callers, which is what a settle wave actually produces, are
    excluded by the committed value alone.

    Both ``now`` and the cutoff are computed on the app server, not by
    Postgres, so two web hosts whose clocks differ by more than 24h could each
    win. NTP makes that remote and the cited ``_cas_transition`` has no time
    term to get wrong; a Postgres-side ``now()`` via RPC, the way
    ``claim_due_webhook_deliveries`` does it, would remove the dependency
    entirely.

    Fails CLOSED. A claim we could not write is treated as lost, so a Supabase
    blip stops auto-reload instead of reopening an unbounded number of
    charges; the manual top-up path is unaffected. That is also why the claim
    is never rolled back when the Stripe call then fails — a dispatch that
    timed out may still have reached Stripe, and releasing the claim to be
    tidy would let the next settle charge the card a second time. The cost of
    that choice is stated on :func:`auto_reload_if_needed`.
    """
    client = get_service_client()
    if client is None:
        return False
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    try:
        response = (
            client.table("user_wallets")
            .update({"auto_reload_last_dispatch_at": now.isoformat()})
            .eq("user_id", user_id)
            .lt("auto_reload_last_dispatch_at", cutoff.isoformat())
            .execute()
        )
        return bool(getattr(response, "data", None))
    except Exception:
        logger.warning(
            "auto-reload dispatch claim failed for %s; treating it as held "
            "elsewhere so no charge goes out.", user_id, exc_info=True,
        )
        return False


def _auto_reload_total_month(user_id: str) -> Optional[Decimal]:
    """Sum of auto-reload credits in the current calendar month (UTC).

    ``None`` means the ledger could not be read, and is not interchangeable
    with ``Decimal("0")`` for the same reason as in
    :func:`_auto_reload_count_24h`: the caller's only use of this number is
    ``month_total + reload_amount > monthly_cap``, and the lowest total it can
    return is the one most likely to clear that comparison. A failed read used
    to be indistinguishable from a month with no reloads in it.

    The refusal lives in the caller:
    ``tests/test_wallet.py::test_auto_reload_refuses_when_the_monthly_total_cannot_be_read``
    is what fails if it is removed.
    """
    client = get_service_client()
    if client is None:
        return None
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
        rows = getattr(response, "data", None)
        if rows is None:
            return None
        return sum(
            (Decimal(str(r.get("amount_usd") or 0)) for r in rows),
            Decimal("0"),
        )
    except Exception:
        logger.warning(
            "auto_reload_total_month failed for %s", user_id, exc_info=True
        )
        return None


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
    # Only on the settle that takes the balance from the threshold or above to
    # below it, so a $5.00 signup-credit wallet is mailed once, after its first
    # run, and not again after every later run.
    if balance < LOW_BALANCE_EMAIL_THRESHOLD <= balance + actual_cost:
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
