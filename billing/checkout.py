"""Stripe Checkout Session + Billing Portal creation for tools-hub.

Sits in front of the existing webhook-driven tier/credit flow in
``webhooks/stripe.py``. Users land on ``/pricing``, pick a tier, click
through to ``/billing/checkout?plan=<tier>``, get a Stripe Checkout
Session, pay, and return to ``/account`` with credits already granted
(the webhook grants credits as a side effect of the
``checkout.session.completed`` event).

Env vars used:

    STRIPE_SECRET_KEY            — sk_test_... or sk_live_...
    STRIPE_PRICE_SCOUT_PRO       — price id for the Scout Pro plan
    STRIPE_PRICE_LAB             — price id for the Lab plan
    STRIPE_PRICE_LAB_PLUS        — price id for the Lab+ plan
    APP_URL                      — public base URL for redirect targets
                                   (e.g. https://tools.ranomics.com).
                                   Falls back to request.url_root.

If the ``stripe`` package is missing or no price id is configured for
the requested plan, the helpers return an error string so the caller
can render a user-facing message instead of a 500.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from flask import session

logger = logging.getLogger(__name__)


_TIER_TO_PRICE_ENV = {
    "scout_pro": "STRIPE_PRICE_SCOUT_PRO",
    "lab": "STRIPE_PRICE_LAB",
    "lab_plus": "STRIPE_PRICE_LAB_PLUS",
}


def _price_id_for(plan: str) -> Optional[str]:
    env_key = _TIER_TO_PRICE_ENV.get(plan)
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
    plan: str,
    *,
    success_url: str,
    cancel_url: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Create a Stripe Checkout Session for a subscription plan.

    Returns ``(url, None)`` on success, ``(None, error_message)`` on
    failure. The returned URL should be served as a 303 redirect.
    """
    price_id = _price_id_for(plan)
    if not price_id:
        return None, (
            f"No Stripe price configured for plan '{plan}'. "
            "Ask Leo to set STRIPE_PRICE_" + plan.upper() + "."
        )

    stripe = _stripe_client()
    if stripe is None:
        return None, "Stripe is not configured."

    customer_email = _resolve_customer_email()
    if not customer_email:
        return None, "Sign in before upgrading."

    try:
        stripe_session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=customer_email,
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            billing_address_collection="auto",
            # metadata flows through to the webhook so custom flows can
            # recover the price id without re-reading the subscription.
            metadata={"price_id": price_id, "plan": plan},
        )
    except Exception as exc:  # stripe.error.* + network
        logger.error(
            "Stripe Checkout Session.create failed for %s: %s",
            plan,
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

    The user must already have a ``stripe_customer_id`` recorded on the
    ``user_tier`` row (written by the checkout.session.completed
    webhook). Returns ``(url, None)`` or ``(None, error_message)``.
    """
    email = _resolve_customer_email()
    if not email:
        return None, "Sign in before managing billing."

    stripe = _stripe_client()
    if stripe is None:
        return None, "Stripe is not configured."

    # Resolve the Stripe customer id from user_tier (written by the
    # checkout.session.completed webhook). Falls back to a lookup by
    # email via Stripe if the local row is missing.
    customer_id = _lookup_customer_id_by_email(email)
    if not customer_id:
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
        return None, "No Stripe customer yet. Upgrade a plan first."

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


def _lookup_customer_id_by_email(email: str) -> Optional[str]:
    """Pull the stripe_customer_id out of user_tier for this email."""
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
            .single()
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
