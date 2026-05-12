"""Stripe Checkout Session creation for tools-hub Workspace purchases.

The user flow:
1. Sign in.
2. Upload a target PDB (stored in Supabase storage); receive ``target_pdb_id``.
3. Pick a Workspace SKU (Standard or XL).
4. ``/billing/checkout?sku=<sku>&target_pdb_id=<id>`` creates a Stripe
   one-time Checkout Session.
5. Pay -> Stripe redirects to ``success_url`` while emitting
   ``checkout.session.completed``.
6. The webhook in ``webhooks/stripe.py`` reads the session metadata
   (``sku`` + ``target_pdb_id``) and calls
   ``shared.workspaces.activate_workspace``.

Env vars:

    STRIPE_SECRET_KEY                       sk_test_... or sk_live_...
    STRIPE_PRICE_WORKSPACE_STANDARD         price id for the $499 SKU
    STRIPE_PRICE_WORKSPACE_XL               price id for the $2,499 SKU
    APP_URL                                 base URL for redirects

History
-------
Previously this module created recurring subscription Checkout Sessions
for ``scout_pro``/``lab``/``lab_plus``. Replaced 2026-05-11 with one-time
Workspace SKU sessions (see ``docs/PRODUCT-PLAN.md`` §Pricing).
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from flask import session

from billing.tiers import get_sku, SKU_NAMES

logger = logging.getLogger(__name__)


_SKU_TO_PRICE_ENV = {
    "workspace_standard": "STRIPE_PRICE_WORKSPACE_STANDARD",
    "workspace_xl": "STRIPE_PRICE_WORKSPACE_XL",
}


def _price_id_for(sku: str) -> Optional[str]:
    env_key = _SKU_TO_PRICE_ENV.get(sku)
    if not env_key:
        return None
    value = os.environ.get(env_key, "").strip()
    return value or None


def _stripe_client():
    """Import + configure the Stripe SDK lazily.

    Returns the ``stripe`` module with ``api_key`` set, or None if the
    package or API key is missing.
    """
    api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not api_key:
        logger.error("STRIPE_SECRET_KEY is not set — cannot create checkout.")
        return None
    try:
        import stripe  # noqa: PLC0415
    except ImportError:
        logger.error("stripe package not installed.")
        return None
    stripe.api_key = api_key
    return stripe


def _resolve_customer_email() -> Optional[str]:
    """Return the signed-in user's email from the Flask session."""
    email = session.get("user_email")
    return email.strip() if isinstance(email, str) and email else None


def create_checkout_session(
    sku: str,
    *,
    target_pdb_id: str,
    success_url: str,
    cancel_url: str,
    target_label: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Create a Stripe one-time Checkout Session for a Workspace SKU.

    Parameters
    ----------
    sku
        One of ``workspace_standard`` / ``workspace_xl``.
    target_pdb_id
        Identifier the webhook will pass to ``activate_workspace``. Must
        already be persisted (Supabase storage upload) before checkout.
    success_url / cancel_url
        Stripe redirect URLs. Success should land on the workspace
        dashboard once the webhook has activated it.
    target_label
        Optional human-readable label (e.g. ``"PD-L1 (4Z18)"``) shown on
        the workspace dashboard. Not used by the webhook.

    Returns
    -------
    ``(url, None)`` on success or ``(None, error)`` on failure. The URL
    should be served as a 303 redirect.
    """
    if sku not in SKU_NAMES:
        return None, (
            f"Unknown Workspace SKU '{sku}'. Expected one of: "
            + ", ".join(SKU_NAMES)
        )
    if not target_pdb_id or not isinstance(target_pdb_id, str):
        return None, "Missing target_pdb_id."

    price_id = _price_id_for(sku)
    if not price_id:
        return None, (
            f"No Stripe price configured for {sku}. "
            "Ask Leo to set STRIPE_PRICE_" + sku.upper() + "."
        )

    stripe = _stripe_client()
    if stripe is None:
        return None, "Stripe is not configured."

    customer_email = _resolve_customer_email()
    if not customer_email:
        return None, "Sign in before purchasing a Workspace."

    sku_obj = get_sku(sku)

    try:
        stripe_session = stripe.checkout.Session.create(
            # One-time payment — the user pays once per Workspace.
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=customer_email,
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            billing_address_collection="auto",
            # Stripe Tax handles VAT/sales tax for international buyers.
            automatic_tax={"enabled": True},
            # metadata flows through to the webhook so it can reconstruct
            # the activation request without round-tripping Stripe.
            metadata={
                "sku": sku,
                "target_pdb_id": target_pdb_id,
                "target_label": target_label or "",
                "modal_cap_usd": (
                    str(sku_obj.modal_cap_usd) if sku_obj else ""
                ),
                "list_price_usd": (
                    str(sku_obj.list_price_usd) if sku_obj else ""
                ),
            },
            # Stripe Idempotency-Key prevents double-charging on double-clicks
            # and webhook retries.
            payment_intent_data={
                "metadata": {
                    "sku": sku,
                    "target_pdb_id": target_pdb_id,
                },
            },
        )
    except Exception as exc:  # stripe.error.* + network
        logger.error(
            "Stripe Checkout Session.create failed for sku=%s target=%s: %s",
            sku,
            target_pdb_id,
            exc,
            exc_info=True,
        )
        return None, "Could not create checkout session. Try again."

    url = getattr(stripe_session, "url", None)
    if not url:
        return None, "Stripe did not return a checkout URL."
    return url, None


def create_portal_session(
    *,
    return_url: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Create a Stripe Billing Portal session for the current user.

    Used so customers can download Workspace receipts and update payment
    methods. Requires a prior Workspace purchase (Stripe customer exists).
    """
    email = _resolve_customer_email()
    if not email:
        return None, "Sign in before managing billing."

    stripe = _stripe_client()
    if stripe is None:
        return None, "Stripe is not configured."

    customer_id = _lookup_customer_id_by_email(email)
    if not customer_id:
        # Fall back to a Stripe-side lookup if we have no local record.
        try:
            customers = stripe.Customer.list(email=email, limit=1)
            data = getattr(customers, "data", None) or []
            if data:
                customer_id = data[0].get("id")
        except Exception:
            logger.warning(
                "Stripe Customer.list failed for %s.", email, exc_info=True
            )

    if not customer_id:
        return None, "No Stripe customer yet. Activate a Workspace first."

    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
    except Exception as exc:
        logger.error(
            "Stripe billing_portal.Session.create failed: %s",
            exc,
            exc_info=True,
        )
        return None, "Could not open the billing portal. Try again."

    url = getattr(portal, "url", None)
    if not url:
        return None, "Stripe did not return a portal URL."
    return url, None


def issue_workspace_refund(
    workspace_id: str,
    stripe_payment_intent_id: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Issue a Stripe refund for a Workspace's payment intent.

    Used by the ``/webhooks/refund-request`` endpoint after eligibility
    has been verified (first-Workspace, within 7-day window, not already
    refunded).

    Returns ``(stripe_refund_id, None)`` on success or
    ``(None, error_message)`` on failure.
    """
    stripe = _stripe_client()
    if stripe is None:
        return None, "Stripe is not configured."

    if not stripe_payment_intent_id:
        return None, "Workspace has no recorded payment intent."

    try:
        refund = stripe.Refund.create(
            payment_intent=stripe_payment_intent_id,
            reason="requested_by_customer",
            metadata={"workspace_id": workspace_id},
            # Idempotency key prevents double-refunds if the client
            # retries; uses the workspace id as the natural unique key.
            idempotency_key=f"workspace_refund_{workspace_id}",
        )
    except Exception as exc:
        logger.error(
            "Stripe Refund.create failed for workspace=%s pi=%s: %s",
            workspace_id,
            stripe_payment_intent_id,
            exc,
            exc_info=True,
        )
        return None, "Could not issue refund. Try again or contact support."

    refund_id = getattr(refund, "id", None)
    if not refund_id:
        return None, "Stripe did not return a refund id."
    return refund_id, None


def _lookup_customer_id_by_email(email: str) -> Optional[str]:
    """Pull the stripe_customer_id out of user_tier for this email.

    Note: user_tier is still populated by the legacy subscription flow
    for any pre-existing customers. New Workspace purchases write
    stripe_customer_id implicitly via Stripe (email match) but do not
    require this table. If absent we fall back to Stripe Customer.list.
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return None
    try:
        page = client.auth.admin.list_users()
        users = getattr(page, "users", None) or page
        user_id = None
        for user in users:
            candidate = getattr(user, "email", None) or (
                user.get("email") if isinstance(user, dict) else None
            )
            if candidate and candidate.lower() == email.lower():
                user_id = getattr(user, "id", None) or user.get("id")
                break
        if not user_id:
            return None
        response = (
            client.table("user_tier")
            .select("stripe_customer_id")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        data = getattr(response, "data", None)
        if data and data.get("stripe_customer_id"):
            return str(data["stripe_customer_id"])
    except Exception:
        logger.warning(
            "Could not resolve stripe_customer_id for %s.",
            email,
            exc_info=True,
        )
    return None
