"""Stripe webhook handler for the Ranomics tools hub.

Responsibilities (per the Wave-0 "Done when" clause):

1. Verify the ``Stripe-Signature`` header using ``STRIPE_WEBHOOK_SECRET``.
2. Gate on ``public.stripe_events.event_id`` — unique insert. Replays dedup.
3. On ``checkout.session.completed`` or ``customer.subscription.updated``:
   look up the price id, flip ``public.user_tier``, grant the plan's
   monthly credit budget on the ledger.

The ``stripe`` Python package is imported lazily so the module still loads
in dev environments where the dep is missing; signature verification then
fails closed with HTTP 500 and a clear log line.

Registering
-----------
    from webhooks.stripe import register_stripe_webhook
    register_stripe_webhook(flask_app)

The route is mounted at ``/webhooks/stripe`` (POST only).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from flask import Flask, Response, jsonify, request

from billing.tiers import TierPlan, lookup_plan
from shared.credits import get_service_client, record_grant

logger = logging.getLogger(__name__)


HANDLED_EVENT_TYPES = {
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.created",
    "invoice.paid",
}


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _verify_signature(payload: bytes, signature: str) -> Optional[dict]:
    """Verify the webhook signature and return the parsed event dict.

    Returns None if verification fails or the Stripe SDK is unavailable.
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.error(
            "STRIPE_WEBHOOK_SECRET not set — rejecting all webhooks."
        )
        return None
    try:
        import stripe  # noqa: PLC0415
    except ImportError:
        logger.error(
            "stripe package not installed. Add 'stripe' to "
            "requirements.txt before handling webhooks."
        )
        return None
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=signature,
            secret=secret,
        )
        # Stripe returns a ``Event`` proxy that behaves like a dict; coerce
        # to plain dict so downstream code is SDK-version agnostic.
        return dict(event)
    except Exception as exc:  # ValueError, SignatureVerificationError, etc.
        logger.warning("Stripe signature verification failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Idempotency + persistence
# ---------------------------------------------------------------------------


def _insert_event_once(event: dict) -> bool:
    """Insert the event into ``stripe_events``. Return False on duplicate.

    Uses the PRIMARY KEY on ``event_id`` as the idempotency gate: a unique
    violation means we have already handled this event. We treat any
    insert failure as a duplicate to fail closed — replays return 200 with
    an ``already_processed`` marker rather than re-charging the user.
    """
    client = get_service_client()
    if client is None:
        return False
    row = {
        "event_id": event.get("id"),
        "event_type": event.get("type"),
        "payload": event,
    }
    try:
        client.table("stripe_events").insert(row).execute()
        return True
    except Exception as exc:
        # Could be a unique-constraint violation (replay) or a transient
        # DB error. Log and treat as duplicate.
        logger.info(
            "stripe_events insert rejected (likely replay): %s", exc
        )
        return False


def _mark_processed(event_id: str) -> None:
    client = get_service_client()
    if client is None:
        return
    try:
        client.table("stripe_events").update(
            {"processed_at": "now()"}
        ).eq("event_id", event_id).execute()
    except Exception:
        logger.warning(
            "Could not mark stripe_event %s processed.", event_id
        )


# ---------------------------------------------------------------------------
# User lookup + tier / credit application
# ---------------------------------------------------------------------------


def _resolve_user_id_from_customer(
    customer_id: Optional[str],
    customer_email: Optional[str],
) -> Optional[str]:
    """Resolve a Supabase user_id from a Stripe customer id or email.

    Preference order:
      1. ``user_tier`` row where ``stripe_customer_id = customer_id``.
      2. Supabase Auth lookup by email.
    """
    if not customer_id and not customer_email:
        return None
    client = get_service_client()
    if client is None:
        return None
    if customer_id:
        try:
            response = (
                client.table("user_tier")
                .select("user_id")
                .eq("stripe_customer_id", customer_id)
                .limit(1)
                .execute()
            )
            rows = getattr(response, "data", None) or []
            if rows:
                return rows[0].get("user_id")
        except Exception:
            logger.warning(
                "Lookup by stripe_customer_id failed.", exc_info=True
            )
    if customer_email:
        try:
            page = client.auth.admin.list_users()
            users = getattr(page, "users", None) or page
            for user in users:
                email = getattr(user, "email", None) or (
                    user.get("email") if isinstance(user, dict) else None
                )
                if email and email.lower() == customer_email.lower():
                    return getattr(user, "id", None) or user.get("id")
        except Exception:
            logger.warning(
                "Lookup by Supabase email failed.", exc_info=True
            )
    return None


def _extract_price_id(event: dict) -> Optional[str]:
    """Pull the first subscription-item price id out of a Stripe event.

    Three shapes we handle:
      1. Subscription objects — ``items.data[0].price.id`` is inline.
      2. Checkout session in subscription mode — ``line_items`` is NOT
         included in the webhook payload by Stripe's design. We look up
         the referenced subscription via the API to recover the price id.
      3. Anything with ``metadata.price_id`` set by us (custom flows).
    """
    obj = event.get("data", {}).get("object", {}) or {}
    # Subscription objects carry items.data[].price.id directly.
    items = (obj.get("items") or {}).get("data") or []
    if items:
        price = items[0].get("price") or {}
        if price.get("id"):
            return price["id"]
    # Checkout session → resolve the associated subscription.
    sub_id = obj.get("subscription")
    if sub_id:
        try:
            import stripe  # noqa: PLC0415
            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
            sub = stripe.Subscription.retrieve(sub_id)
            price_id = sub["items"]["data"][0]["price"]["id"]
            if price_id:
                return price_id
        except Exception:
            logger.warning(
                "Could not retrieve subscription %s to extract price.",
                sub_id,
                exc_info=True,
            )
    # Custom-flow escape hatch.
    if obj.get("metadata") and obj["metadata"].get("price_id"):
        return obj["metadata"]["price_id"]
    return None


def _apply_subscription_event(event: dict) -> dict:
    """Flip tier + grant credits for a subscription-shaped event.

    Returns a small status dict for logging / response bodies.
    """
    obj = event.get("data", {}).get("object", {}) or {}
    customer_id = obj.get("customer")
    customer_email = (
        obj.get("customer_email")
        or obj.get("customer_details", {}).get("email")
    )

    # Subscription webhooks do NOT include customer_email; retrieve the
    # Stripe Customer object to fill it in before falling through to the
    # Supabase auth lookup. This is the canonical pattern per Stripe docs.
    if not customer_email and customer_id:
        try:
            import stripe  # noqa: PLC0415
            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
            customer_email = stripe.Customer.retrieve(customer_id).get("email")
        except Exception:
            logger.warning(
                "Could not retrieve Stripe customer %s to resolve email.",
                customer_id,
                exc_info=True,
            )

    user_id = _resolve_user_id_from_customer(customer_id, customer_email)
    if not user_id:
        logger.warning(
            "Stripe event %s had no resolvable user "
            "(customer=%s email=%s).",
            event.get("id"),
            customer_id,
            customer_email,
        )
        return {"status": "skipped", "reason": "user_not_found"}

    price_id = _extract_price_id(event)
    plan: Optional[TierPlan] = lookup_plan(price_id) if price_id else None
    if plan is None:
        logger.warning(
            "Stripe event %s had no mapped plan for price %s.",
            event.get("id"),
            price_id,
        )
        return {"status": "skipped", "reason": "unmapped_price"}

    client = get_service_client()
    if client is None:
        return {"status": "error", "reason": "service_client_unavailable"}

    # Upsert the tier row. period_ends_at comes from the subscription
    # object when present (Stripe gives us a unix timestamp).
    period_end = obj.get("current_period_end")
    period_iso = None
    if isinstance(period_end, (int, float)):
        from datetime import datetime, timezone  # noqa: PLC0415
        period_iso = datetime.fromtimestamp(
            period_end, tz=timezone.utc
        ).isoformat()

    tier_row = {
        "user_id": user_id,
        "tier": plan.tier,
        "stripe_customer_id": customer_id,
        "stripe_subscription_id": obj.get("id")
            if event.get("type", "").startswith("customer.subscription")
            else obj.get("subscription"),
        "period_ends_at": period_iso,
    }
    try:
        client.table("user_tier").upsert(
            tier_row, on_conflict="user_id"
        ).execute()
    except Exception:
        logger.error(
            "Could not upsert user_tier row for %s", user_id, exc_info=True
        )
        return {"status": "error", "reason": "user_tier_upsert_failed"}

    # Grant monthly credits on initial signup OR renewal, never on both for
    # the same billing period. Stripe fires several events per signup flow
    # (checkout.session.completed, customer.subscription.created, and an
    # invoice.paid with billing_reason="subscription_create"); granting on
    # all of them would multi-credit the user. Strategy:
    #   - initial signup: checkout.session.completed -> grant
    #   - renewal:        invoice.paid with billing_reason="subscription_cycle" -> grant
    #   - everything else (subscription.created/updated, first invoice.paid): tier upsert only
    granted = 0
    event_type = event.get("type", "")
    is_initial_signup = event_type == "checkout.session.completed"
    billing_reason = obj.get("billing_reason") if event_type == "invoice.paid" else None
    is_renewal = event_type == "invoice.paid" and billing_reason == "subscription_cycle"
    if (is_initial_signup or is_renewal) and plan.monthly_credits > 0:
        grant_reason = (
            f"{plan.tier} initial grant"
            if is_initial_signup
            else f"{plan.tier} renewal grant"
        )
        record_grant(
            user_id,
            plan.monthly_credits,
            reason=grant_reason,
            stripe_event_id=event.get("id"),
            metadata={
                "price_id": price_id,
                "event_type": event_type,
                "billing_reason": billing_reason,
            },
        )
        granted = plan.monthly_credits

    return {
        "status": "ok",
        "tier": plan.tier,
        "credits_granted": granted,
    }


# ---------------------------------------------------------------------------
# Flask integration
# ---------------------------------------------------------------------------


def register_stripe_webhook(flask_app: Flask) -> None:
    """Attach ``POST /webhooks/stripe`` to the given Flask app."""

    @flask_app.route("/webhooks/stripe", methods=["POST"])
    def stripe_webhook() -> Any:  # noqa: ANN401 — Flask route return
        signature = request.headers.get("Stripe-Signature", "")
        payload = request.get_data()

        event = _verify_signature(payload, signature)
        if event is None:
            return Response("invalid signature", status=400)

        event_id = event.get("id")
        event_type = event.get("type")
        if not event_id or not event_type:
            return Response("malformed event", status=400)

        # Idempotency gate. A duplicate event id returns 200 so Stripe does
        # not keep retrying.
        if not _insert_event_once(event):
            _observe(event_type, "duplicate")
            return jsonify(
                {"status": "already_processed", "event_id": event_id}
            )

        result: dict = {"status": "ignored", "event_type": event_type}
        outcome = "ignored"
        if event_type in HANDLED_EVENT_TYPES:
            try:
                result = _apply_subscription_event(event)
                outcome = str(result.get("status") or "ok")
            except Exception:
                logger.exception(
                    "Error applying Stripe event %s", event_id
                )
                # Still mark processed — the event row is the audit trail.
                # If you want retry-on-error semantics later, downgrade to
                # returning 500 here and let Stripe retry.
                result = {"status": "error"}
                outcome = "error"

        _mark_processed(event_id)
        _observe(event_type, outcome)
        result["event_id"] = event_id
        return jsonify(result)


def _observe(event_type: str, outcome: str) -> None:
    """Lazy-imported metrics hook. Never raises."""
    try:
        from shared.metrics import observe_stripe_event  # noqa: PLC0415
        observe_stripe_event(event_type, outcome)
    except Exception:  # pragma: no cover
        pass
