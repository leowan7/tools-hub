"""Stripe price-id to tier + monthly credit grant mapping.

Configured via environment variables so the same code runs against
Stripe test mode and live mode without code changes. Each tier expects
two env vars:

    STRIPE_PRICE_<TIER>              the ``price_...`` id from Stripe
    STRIPE_CREDITS_<TIER>            credits granted per billing period

Unset prices are simply absent from the map — the webhook handler treats
an unknown price id as a no-op tier flip (but still records the event).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TierPlan:
    """Subscription plan mapped from a Stripe price id."""

    tier: str
    monthly_credits: int


# Default monthly credit grants per PRODUCT-PLAN.md §Pricing.
_DEFAULT_CREDITS = {
    "scout_pro": 10,
    "lab": 150,
    "lab_plus": 600,
}


def _credits_for(tier: str) -> int:
    env_key = f"STRIPE_CREDITS_{tier.upper()}"
    raw = os.environ.get(env_key, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_CREDITS.get(tier, 0)


def price_to_plan() -> dict[str, TierPlan]:
    """Build the Stripe price-id -> TierPlan lookup from env."""
    mapping: dict[str, TierPlan] = {}
    for tier in ("scout_pro", "lab", "lab_plus"):
        price_id = os.environ.get(
            f"STRIPE_PRICE_{tier.upper()}", ""
        ).strip()
        if price_id:
            mapping[price_id] = TierPlan(
                tier=tier, monthly_credits=_credits_for(tier)
            )
    return mapping


def lookup_plan(price_id: str) -> Optional[TierPlan]:
    """Return the TierPlan for a Stripe price id, or None if unmapped."""
    return price_to_plan().get(price_id)
