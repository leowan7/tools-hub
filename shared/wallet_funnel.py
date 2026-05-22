"""Funnel signal triggers for the wallet pivot.

When a user crosses a 30-day spend threshold we send an email or
internal Slack alert so sales can follow up. The thresholds are
deliberately tiered so we do not spam ourselves on every charge.

Deduplication
-------------
The ``funnel_alerts`` table (created in the wallet migration) stores
one row per user per tier. Before emitting we check the last alert
tier for the user. We emit only if the new tier is strictly above the
last emitted tier, so the alerts step up exactly once per user across
their lifetime.

Thresholds (matching plan lines 1004-1014):

* ``active_project`` fires when 30-day spend reaches $1000 (pilot intro email)
* ``sales_qualified`` fires at $5000 (sales Slack alert)
* ``high_value`` fires at $10000 (high-value Slack alert)

This module reads from ``wallet_transactions``, writes to
``funnel_alerts``, and calls senders in :mod:`shared.email`. It does
not touch Stripe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from shared.credits import get_service_client

logger = logging.getLogger(__name__)


# Tier ordering so we only ever step up.
_TIER_ORDER = ("active_project", "sales_qualified", "high_value")
_TIER_INDEX = {name: idx for idx, name in enumerate(_TIER_ORDER)}


@dataclass(frozen=True)
class FunnelTrigger:
    """One funnel threshold + the email helper that should fire."""

    threshold_usd: Decimal
    tier: str
    email_handler: str  # name of the helper in :mod:`shared.email`


FUNNEL_TRIGGERS: tuple[FunnelTrigger, ...] = (
    FunnelTrigger(Decimal("1000"), "active_project", "send_pilot_intro_email"),
    FunnelTrigger(Decimal("5000"), "sales_qualified", "alert_sales_slack"),
    FunnelTrigger(Decimal("10000"), "high_value", "alert_sales_slack_high"),
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _maybe_trigger_funnel_alerts(
    user_id: str, spend_amount_cents: Optional[Decimal] = None
) -> Optional[str]:
    """Evaluate funnel triggers for ``user_id``.

    Returns the tier of the alert that was emitted, or ``None`` if no
    alert fired. ``spend_amount_cents`` is accepted for caller
    compatibility (it is the size of the most recent charge in dollars,
    not cents; the parameter name follows the plan's wording). The
    actual threshold check uses the 30-day total fetched from the ledger.
    """
    spent_30d = _wallet_30d_spend_usd(user_id)
    if spent_30d <= 0:
        return None

    candidate = _highest_eligible_tier(spent_30d)
    if candidate is None:
        return None

    last_tier = _last_funnel_tier(user_id)
    if not _is_step_up(last_tier, candidate.tier):
        return None

    handler = _resolve_handler(candidate.email_handler)
    if handler is None:
        logger.warning(
            "funnel handler missing: tier=%s handler=%s",
            candidate.tier, candidate.email_handler,
        )
        return None
    try:
        handler(user_id=user_id, spent_30d_usd=spent_30d)
    except TypeError:
        # Handler may use positional args in tests or stubs.
        try:
            handler(user_id, spent_30d)
        except Exception:  # pragma: no cover (best-effort)
            logger.warning(
                "funnel handler raised for tier=%s user=%s",
                candidate.tier, user_id, exc_info=True,
            )
            return None
    except Exception:  # pragma: no cover
        logger.warning(
            "funnel handler raised for tier=%s user=%s",
            candidate.tier, user_id, exc_info=True,
        )
        return None
    _record_funnel_alert(user_id, candidate.tier, spent_30d)
    return candidate.tier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _highest_eligible_tier(spent_30d: Decimal) -> Optional[FunnelTrigger]:
    """Return the highest funnel tier whose threshold is met."""
    eligible: Optional[FunnelTrigger] = None
    for trigger in FUNNEL_TRIGGERS:
        if spent_30d >= trigger.threshold_usd:
            if eligible is None or (
                _TIER_INDEX[trigger.tier] > _TIER_INDEX[eligible.tier]
            ):
                eligible = trigger
    return eligible


def _is_step_up(last_tier: Optional[str], next_tier: str) -> bool:
    """True if ``next_tier`` is strictly higher than ``last_tier``."""
    if last_tier is None:
        return True
    if last_tier not in _TIER_INDEX or next_tier not in _TIER_INDEX:
        return True
    return _TIER_INDEX[next_tier] > _TIER_INDEX[last_tier]


def _wallet_30d_spend_usd(user_id: str) -> Decimal:
    """Net USD a user spent on jobs over the last 30 days.

    Delegates to the canonical net-spend formula in
    :func:`shared.wallet._net_spend_usd` so the funnel tiers, the daily
    cap, and the wallet overview all measure spend the same way. The
    previous ``charge`` + ``absorbed_variance`` sum effectively never
    crossed a tier: job spend lands in ``hold`` rows, not ``charge``.
    """
    from shared.wallet import _net_spend_usd  # noqa: PLC0415

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    return _net_spend_usd(user_id, cutoff)


def _last_funnel_tier(user_id: str) -> Optional[str]:
    """Look up the most recent ``funnel_alerts`` tier for the user."""
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table("funnel_alerts")
            .select("tier")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .maybe_single()
            .execute()
        )
        data = getattr(response, "data", None) or {}
        tier = data.get("tier") if isinstance(data, dict) else None
        return str(tier) if tier else None
    except Exception:
        logger.warning(
            "last_funnel_tier lookup failed for %s",
            user_id, exc_info=True,
        )
        return None


def _record_funnel_alert(
    user_id: str, tier: str, spent_30d_usd: Decimal
) -> bool:
    """Append a row to ``funnel_alerts`` after a successful emission."""
    client = get_service_client()
    if client is None:
        return False
    row = {
        "user_id": user_id,
        "tier": tier,
        "spent_30d_usd": float(spent_30d_usd),
    }
    try:
        client.table("funnel_alerts").insert(row).execute()
        return True
    except Exception:
        logger.warning(
            "could not record funnel alert user=%s tier=%s",
            user_id, tier, exc_info=True,
        )
        return False


def _resolve_handler(name: str) -> Optional[Callable]:
    """Look up an email helper by name in :mod:`shared.email`.

    Returns ``None`` when the helper is missing. The wallet code
    treats missing email helpers as warnings rather than fatal errors.
    """
    try:
        from shared import email as email_module  # noqa: PLC0415

        return getattr(email_module, name, None)
    except Exception:  # pragma: no cover
        return None
