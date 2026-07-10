"""Stripe price-id to Workspace SKU mapping.

Configured via environment variables so the same code runs against
Stripe test mode and live mode without code changes. Each SKU expects:

    STRIPE_PRICE_<SKU>              the ``price_...`` id from Stripe

(Modal compute caps and durations are NOT env-tunable — they're product
contracts. Hardcoded in ``_DEFAULT_SKUS`` and mirrored in
``shared.workspaces._SKU_CONFIG``.)

Unset price ids are absent from the map — the webhook handler treats an
unknown price id as a no-op SKU lookup (still records the event for
audit).

History
-------
Previously this module mapped to subscription tiers
(``scout_pro``/``lab``/``lab_plus``) with monthly credit grants. That
model could not fund a real design campaign at sustainable margin (see
``docs/PRODUCT-PLAN.md`` §Pricing). Replaced 2026-05-11 with the
per-target Workspace SKU model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WorkspaceSKU:
    """A one-time Workspace product available for sale.

    Attributes
    ----------
    sku
        Internal identifier (``workspace_standard`` or ``workspace_xl``).
        Matches the ``workspace_sku`` enum in supabase migration 0014.
    modal_cap_usd
        Compute spend ceiling per workspace, in USD. Standard = $100;
        XL = $500.
    duration_days
        Workspace TTL after activation. 30 days for both SKUs.
    refund_eligible
        Whether this SKU participates in the first-Workspace 7-day refund
        policy. True for both launch SKUs.
    list_price_usd
        Headline retail price shown to the customer ($499 / $2,499).
        Stripe is the source of truth for actual charge amounts; this
        field is informational (used in margin reporting + the pricing
        page tooltip).
    """

    sku: str
    modal_cap_usd: float
    duration_days: int
    refund_eligible: bool
    list_price_usd: float


# Product contracts. Kept in sync with shared/workspaces.py _SKU_CONFIG.
_DEFAULT_SKUS = {
    "workspace_standard": WorkspaceSKU(
        sku="workspace_standard",
        modal_cap_usd=100.00,
        duration_days=30,
        refund_eligible=True,
        list_price_usd=499.00,
    ),
    "workspace_xl": WorkspaceSKU(
        sku="workspace_xl",
        modal_cap_usd=500.00,
        duration_days=30,
        refund_eligible=True,
        list_price_usd=2499.00,
    ),
}


SKU_NAMES = tuple(_DEFAULT_SKUS.keys())


def get_sku(sku: str) -> Optional[WorkspaceSKU]:
    """Return the canonical WorkspaceSKU for the given internal id."""
    return _DEFAULT_SKUS.get(sku)


def all_skus() -> list[WorkspaceSKU]:
    """All currently-sold Workspace SKUs, in display order."""
    return list(_DEFAULT_SKUS.values())


def price_to_sku() -> dict[str, WorkspaceSKU]:
    """Build the Stripe price-id -> WorkspaceSKU lookup from env."""
    mapping: dict[str, WorkspaceSKU] = {}
    for sku_id, sku in _DEFAULT_SKUS.items():
        price_id = os.environ.get(
            f"STRIPE_PRICE_{sku_id.upper()}", ""
        ).strip()
        if price_id:
            mapping[price_id] = sku
    return mapping


def lookup_sku(price_id: str) -> Optional[WorkspaceSKU]:
    """Return the WorkspaceSKU for a Stripe price id, or None if unmapped."""
    return price_to_sku().get(price_id)


# ---------------------------------------------------------------------------
# Customer-facing Workspace SKU display strings (headline prices, ledes)
# were RETIRED with the USD-wallet pivot. The tiered $499 / $2,499
# per-target Workspace product is no longer sold; the wallet is the sole
# money path, so nothing renders these strings anymore. The SKU config
# above (caps, durations, list_price_usd) is retained only for internal
# margin accounting and the compute lifecycle in shared/workspaces.py.
# ---------------------------------------------------------------------------
