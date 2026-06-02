"""Stripe webhook handler for the tools-hub USD wallet.

Replaces the legacy Workspace activation handler. The wallet pivot
narrows the surface to exactly four event types and routes each one
through ``shared.wallet`` primitives.

Subscribed events
-----------------
* ``checkout.session.completed``    a top up the user started via
                                    Stripe Checkout. Routes to
                                    ``shared.wallet.top_up_wallet`` with
                                    ``kind=topup`` and dispatches the
                                    top up confirmation email.
* ``payment_intent.succeeded``      off session auto reload landed.
                                    Routes to
                                    ``shared.wallet.top_up_wallet`` with
                                    ``kind=auto_reload`` and dispatches
                                    the auto reload charged email.
* ``payment_intent.payment_failed`` off session auto reload declined.
                                    Flips ``auto_reload_enabled`` to
                                    false on the wallet and dispatches
                                    the auto reload failed email.
* ``charge.dispute.created``        chargeback received on a top up.
                                    Freezes the wallet via
                                    ``shared.wallet.freeze_wallet_on_dispute``
                                    so no new submissions can run.

Responsibilities
----------------
1. Verify ``Stripe-Signature`` against ``STRIPE_WEBHOOK_SECRET``.
   Malformed signatures return 400.
2. Idempotency gate on ``public.stripe_events.event_id`` (primary key,
   already present from migration 0001). Replays return 200 so Stripe
   stops retrying without touching the wallet a second time.
3. Per event handler dispatch. Each handler is a small adapter that
   pulls user_id and amount_usd out of the Stripe object metadata,
   then calls the matching ``shared.wallet`` primitive.

Registering
-----------
::

    from webhooks.stripe import register_stripe_webhook
    register_stripe_webhook(flask_app)
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any, Optional

from flask import Flask, Response, jsonify, request

from shared.credits import get_service_client
from shared.wallet import freeze_wallet_on_dispute, top_up_wallet

logger = logging.getLogger(__name__)


HANDLED_EVENT_TYPES = {
    "checkout.session.completed",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.dispute.created",
}


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _verify_signature(payload: bytes, signature: str) -> Optional[dict]:
    """Verify the webhook signature and return the parsed event dict.

    Returns ``None`` if verification fails, the secret is missing, or
    the Stripe SDK is unavailable. Callers must reject the request with
    a 400 when this returns ``None``.
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.error(
            "STRIPE_WEBHOOK_SECRET not set; rejecting all webhooks."
        )
        return None
    if not signature:
        logger.warning("Missing Stripe-Signature header.")
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
        return dict(event)
    except Exception as exc:  # ValueError, SignatureVerificationError, etc.
        logger.warning("Stripe signature verification failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Idempotency persistence on stripe_events
# ---------------------------------------------------------------------------


def _insert_event_once(event: dict) -> bool:
    """Insert the event id into ``stripe_events``. Return False on duplicate.

    The ``event_id`` primary key on ``stripe_events`` is the idempotency
    key. A duplicate event id raises a unique constraint violation and
    we treat that as a replay (return False so the route layer can short
    circuit).
    """
    client = get_service_client()
    if client is None:
        logger.error(
            "stripe_events insert: service role client unavailable."
        )
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
        # Could be a unique constraint violation (replay) or a transient
        # DB error. Either way, do not process again.
        logger.info(
            "stripe_events insert rejected (likely replay): %s", exc
        )
        return False


def _mark_processed(event_id: str) -> None:
    """Stamp ``processed_at`` on the stripe_events row. Best effort."""
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
# Helpers
# ---------------------------------------------------------------------------


def _amount_from_minor(value: Any) -> Decimal:
    """Convert Stripe's integer minor units (cents) into a Decimal USD."""
    try:
        cents = int(value)
    except (TypeError, ValueError):
        return Decimal("0")
    return (Decimal(cents) / Decimal("100")).quantize(Decimal("0.01"))


def _user_id_from_metadata(obj: dict) -> Optional[str]:
    """Extract user_id from a Stripe object's metadata dict."""
    metadata = obj.get("metadata") or {}
    user_id = metadata.get("user_id")
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()
    return None


def _kind_from_metadata(obj: dict) -> Optional[str]:
    """Extract the ``kind`` flag (topup, auto_reload, etc.) from metadata."""
    metadata = obj.get("metadata") or {}
    kind = metadata.get("kind")
    if isinstance(kind, str) and kind.strip():
        return kind.strip()
    return None


def _persist_payment_method_from_session(
    session_obj: dict, payment_intent_id: Optional[str], user_id: str
) -> None:
    """When Checkout saved a payment method, stash it on user_wallets.

    Only runs when the session metadata contains ``save_pm=true``. The
    saved card enables future auto reload off session PaymentIntents.
    Best effort: failures here do not block the credit.
    """
    metadata = session_obj.get("metadata") or {}
    if metadata.get("save_pm") != "true":
        return
    if not payment_intent_id:
        return
    try:
        import stripe  # noqa: PLC0415
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        pi = stripe.PaymentIntent.retrieve(payment_intent_id)
        payment_method = pi.get("payment_method")
        customer_id = session_obj.get("customer")
    except Exception:
        logger.warning(
            "Could not retrieve PaymentIntent %s for payment method capture.",
            payment_intent_id, exc_info=True,
        )
        return
    if not payment_method:
        logger.info(
            "PaymentIntent %s had no payment_method to persist.",
            payment_intent_id,
        )
        return
    client = get_service_client()
    if client is None:
        return
    update_payload: dict[str, Any] = {
        "stripe_payment_method_id": payment_method,
    }
    if customer_id:
        update_payload["stripe_customer_id"] = customer_id
    try:
        client.table("user_wallets").update(
            update_payload
        ).eq("user_id", user_id).execute()
    except Exception:
        logger.warning(
            "Could not persist payment method for user %s",
            user_id, exc_info=True,
        )


def _user_id_from_charge(charge_obj: dict) -> Optional[str]:
    """Resolve user_id for a dispute by walking back to the charge then PI.

    Disputes carry a charge id. The charge's metadata usually inherits
    from the PaymentIntent metadata, which our Checkout flow stamps with
    ``user_id``. Falls back to retrieving the PI explicitly when the
    charge metadata is bare.
    """
    user_id = _user_id_from_metadata(charge_obj)
    if user_id:
        return user_id
    payment_intent_id = charge_obj.get("payment_intent")
    if not payment_intent_id:
        return None
    try:
        import stripe  # noqa: PLC0415
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        pi = stripe.PaymentIntent.retrieve(payment_intent_id)
        return _user_id_from_metadata(dict(pi))
    except Exception:
        logger.warning(
            "Could not retrieve PaymentIntent %s while resolving dispute user.",
            payment_intent_id, exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Per event handlers
# ---------------------------------------------------------------------------


def _apply_checkout_session_completed(event: dict) -> dict:
    """Top up that the user started via Stripe Checkout has landed.

    Calls ``shared.wallet.top_up_wallet`` with the wallet's idempotent
    credit path (the SQL function rejects a replay on stripe_event_id).
    """
    obj = event.get("data", {}).get("object", {}) or {}

    payment_status = obj.get("payment_status")
    if payment_status not in (None, "paid", "no_payment_required"):
        logger.info(
            "checkout.session.completed with payment_status=%s; skipping.",
            payment_status,
        )
        return {"status": "skipped", "reason": f"payment_status_{payment_status}"}

    kind = _kind_from_metadata(obj)
    if kind != "topup":
        # Workspace SKUs are archived. Any other kind is unknown and
        # we deliberately ignore it so legacy stragglers do not bleed.
        logger.info(
            "checkout.session.completed kind=%r is not 'topup'; ignoring.",
            kind,
        )
        return {"status": "ignored", "reason": "non_wallet_kind"}

    user_id = _user_id_from_metadata(obj)
    if not user_id:
        logger.warning(
            "checkout.session.completed event %s missing user_id metadata.",
            event.get("id"),
        )
        return {"status": "skipped", "reason": "missing_user_id"}

    metadata = obj.get("metadata") or {}
    amount_meta = metadata.get("amount_usd")
    try:
        amount_usd = Decimal(str(amount_meta)) if amount_meta is not None else None
    except Exception:
        amount_usd = None
    if amount_usd is None or amount_usd <= 0:
        # Fall back to amount_total (in cents).
        amount_usd = _amount_from_minor(obj.get("amount_total"))
    if amount_usd <= 0:
        logger.warning(
            "checkout.session.completed event %s had amount of zero or less.",
            event.get("id"),
        )
        return {"status": "skipped", "reason": "non_positive_amount"}

    payment_intent_id = obj.get("payment_intent")
    if isinstance(payment_intent_id, dict):
        payment_intent_id = payment_intent_id.get("id")
    if not isinstance(payment_intent_id, str) or not payment_intent_id:
        # Top up still proceeds (event id alone is enough for idempotency
        # on the credit), but warn for paper trail.
        logger.warning(
            "checkout.session.completed event %s missing payment_intent id.",
            event.get("id"),
        )
        payment_intent_id = None

    wallet = top_up_wallet(
        user_id=user_id,
        amount_usd=amount_usd,
        stripe_payment_intent_id=payment_intent_id or "",
        stripe_event_id=str(event.get("id")),
        kind="topup",
    )
    if wallet is None:
        return {"status": "error", "reason": "credit_wallet_failed"}

    _persist_payment_method_from_session(obj, payment_intent_id, user_id)
    _send_email_safe(
        "send_topup_confirmation_email",
        user_id=user_id,
        amount_usd=amount_usd,
    )
    # D3 funnel fire. Mirrors the wallet credit into PostHog so the
    # signup -> first_job -> topup conversion view sees this row. No-op
    # when PUBLIC_POSTHOG_KEY is unset.
    from shared.events import EVENTS, emit  # noqa: PLC0415
    emit(
        EVENTS.TOPUP_COMPLETE,
        user_id=user_id,
        properties={
            "amount_usd": float(amount_usd),
            "kind": "topup",
        },
    )
    return {
        "status": "ok",
        "user_id": user_id,
        "amount_usd": str(amount_usd),
        "kind": "topup",
    }


def _apply_payment_intent_succeeded(event: dict) -> dict:
    """Off session auto reload PaymentIntent landed.

    Only credits when the PI metadata has ``kind=auto_reload``. Manual
    top ups also produce a ``payment_intent.succeeded``, but those are
    already credited via ``checkout.session.completed``; ignoring them
    here keeps credit responsibility single sourced and avoids any risk
    of a double credit if Stripe sends both events.
    """
    obj = event.get("data", {}).get("object", {}) or {}
    kind = _kind_from_metadata(obj)
    if kind != "auto_reload":
        logger.info(
            "payment_intent.succeeded kind=%r is not 'auto_reload'; ignoring.",
            kind,
        )
        return {"status": "ignored", "reason": "non_auto_reload"}

    user_id = _user_id_from_metadata(obj)
    if not user_id:
        logger.warning(
            "payment_intent.succeeded event %s missing user_id metadata.",
            event.get("id"),
        )
        return {"status": "skipped", "reason": "missing_user_id"}

    amount_usd = _amount_from_minor(obj.get("amount"))
    if amount_usd <= 0:
        logger.warning(
            "payment_intent.succeeded event %s amount of zero or less.",
            event.get("id"),
        )
        return {"status": "skipped", "reason": "non_positive_amount"}

    payment_intent_id = obj.get("id") or ""
    wallet = top_up_wallet(
        user_id=user_id,
        amount_usd=amount_usd,
        stripe_payment_intent_id=payment_intent_id,
        stripe_event_id=str(event.get("id")),
        kind="auto_reload",
    )
    if wallet is None:
        return {"status": "error", "reason": "credit_wallet_failed"}

    _send_email_safe(
        "send_auto_reload_charged_email",
        user_id=user_id,
        amount_usd=amount_usd,
    )
    # D3 funnel fire. Auto-reloads land on the same funnel event so the
    # dashboard does not need to know about the two checkout shapes.
    from shared.events import EVENTS, emit  # noqa: PLC0415
    emit(
        EVENTS.TOPUP_COMPLETE,
        user_id=user_id,
        properties={
            "amount_usd": float(amount_usd),
            "kind": "auto_reload",
        },
    )
    return {
        "status": "ok",
        "user_id": user_id,
        "amount_usd": str(amount_usd),
        "kind": "auto_reload",
    }


def _apply_payment_intent_failed(event: dict) -> dict:
    """Off session auto reload PaymentIntent declined or 3DS failed.

    Disables auto reload on the user's wallet so the system stops trying
    until the user updates their card, and notifies the user.
    """
    obj = event.get("data", {}).get("object", {}) or {}
    kind = _kind_from_metadata(obj)
    if kind != "auto_reload":
        logger.info(
            "payment_intent.payment_failed kind=%r is not 'auto_reload'; ignoring.",
            kind,
        )
        return {"status": "ignored", "reason": "non_auto_reload"}

    user_id = _user_id_from_metadata(obj)
    if not user_id:
        logger.warning(
            "payment_intent.payment_failed event %s missing user_id metadata.",
            event.get("id"),
        )
        return {"status": "skipped", "reason": "missing_user_id"}

    reason = (
        ((obj.get("last_payment_error") or {}).get("message"))
        or "card_declined"
    )

    client = get_service_client()
    if client is None:
        logger.error(
            "payment_intent.payment_failed: no service client to disable auto reload."
        )
        return {"status": "error", "reason": "missing_service_client"}
    try:
        client.table("user_wallets").update(
            {"auto_reload_enabled": False}
        ).eq("user_id", user_id).execute()
    except Exception:
        logger.error(
            "payment_intent.payment_failed: could not disable auto reload for %s",
            user_id, exc_info=True,
        )
        return {"status": "error", "reason": "disable_failed"}

    _send_email_safe(
        "send_auto_reload_failed_email",
        user_id=user_id,
        reason=reason,
    )
    return {"status": "ok", "user_id": user_id, "reason": reason}


def _apply_charge_dispute_created(event: dict) -> dict:
    """Chargeback received. Freeze the wallet so the user cannot drain it.

    The dispute object carries a ``charge`` id. The charge inherits
    metadata from the PaymentIntent, which our Checkout flow stamps
    with ``user_id``. If the charge metadata is bare we retrieve the
    PaymentIntent directly via the Stripe API.
    """
    dispute = event.get("data", {}).get("object", {}) or {}
    dispute_id = str(dispute.get("id") or "")
    if not dispute_id:
        logger.warning(
            "charge.dispute.created event %s missing dispute id.",
            event.get("id"),
        )
        return {"status": "skipped", "reason": "missing_dispute_id"}

    charge_id = dispute.get("charge")
    if isinstance(charge_id, dict):
        charge_id = charge_id.get("id")
    if not charge_id:
        logger.warning(
            "charge.dispute.created event %s missing charge id.",
            event.get("id"),
        )
        return {"status": "skipped", "reason": "missing_charge_id"}

    charge_obj: dict
    try:
        import stripe  # noqa: PLC0415
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        charge_obj = dict(stripe.Charge.retrieve(charge_id))
    except Exception:
        logger.error(
            "charge.dispute.created: could not retrieve charge %s",
            charge_id, exc_info=True,
        )
        return {"status": "error", "reason": "charge_retrieve_failed"}

    user_id = _user_id_from_charge(charge_obj)
    if not user_id:
        logger.warning(
            "charge.dispute.created event %s could not resolve user_id from charge %s.",
            event.get("id"), charge_id,
        )
        return {"status": "skipped", "reason": "user_not_found"}

    frozen = freeze_wallet_on_dispute(user_id=user_id, dispute_id=dispute_id)
    if not frozen:
        return {"status": "error", "reason": "freeze_failed"}
    return {"status": "ok", "user_id": user_id, "dispute_id": dispute_id}


# Mapping from Stripe event type to handler.
_HANDLERS = {
    "checkout.session.completed":    _apply_checkout_session_completed,
    "payment_intent.succeeded":      _apply_payment_intent_succeeded,
    "payment_intent.payment_failed": _apply_payment_intent_failed,
    "charge.dispute.created":        _apply_charge_dispute_created,
}


# ---------------------------------------------------------------------------
# Flask integration
# ---------------------------------------------------------------------------


def register_stripe_webhook(flask_app: Flask) -> None:
    """Attach the ``/webhooks/stripe`` route to ``flask_app``."""

    @flask_app.route("/webhooks/stripe", methods=["POST"])
    def stripe_webhook() -> Any:  # noqa: ANN401
        signature = request.headers.get("Stripe-Signature", "")
        payload = request.get_data()

        event = _verify_signature(payload, signature)
        if event is None:
            return Response("invalid signature", status=400)

        event_id = event.get("id")
        event_type = event.get("type")
        if not event_id or not event_type:
            return Response("malformed event", status=400)

        if event_type not in HANDLED_EVENT_TYPES:
            # Stripe should never send anything else if the dashboard is
            # configured correctly, but log + 200 so retries stop.
            logger.info(
                "Stripe event %s type=%s not in HANDLED_EVENT_TYPES; ignoring.",
                event_id, event_type,
            )
            _observe(event_type, "ignored")
            return jsonify({"status": "ignored", "event_id": event_id})

        # Idempotency gate. A duplicate event id returns 200 with status
        # 'already_processed' so Stripe stops retrying.
        if not _insert_event_once(event):
            _observe(event_type, "duplicate")
            return jsonify(
                {"status": "already_processed", "event_id": event_id}
            )

        handler = _HANDLERS[event_type]
        try:
            result = handler(event)
            outcome = str(result.get("status") or "ok")
        except Exception:
            logger.exception(
                "Error applying Stripe event %s type=%s",
                event_id, event_type,
            )
            result = {"status": "error"}
            outcome = "error"

        _mark_processed(event_id)
        _observe(event_type, outcome)
        result["event_id"] = event_id
        result["event_type"] = event_type
        return jsonify(result)


# ---------------------------------------------------------------------------
# Side effect helpers (email + metrics)
# ---------------------------------------------------------------------------


def _send_email_safe(func_name: str, **kwargs: Any) -> None:
    """Lazy lookup + invoke an email sender; never raises.

    Email is best effort. Webhook bookkeeping must never break on a
    transient Resend or template error. Agent G replaces the stub bodies
    in ``shared.email`` while keeping signatures stable, so calls here
    stay valid through that swap.
    """
    try:
        from shared import email as email_module  # noqa: PLC0415

        sender = getattr(email_module, func_name, None)
        if sender is None:
            logger.warning(
                "webhook email helper missing: %s (kwargs=%r)",
                func_name, kwargs,
            )
            return
        sender(**kwargs)
    except Exception:  # pragma: no cover (email is best effort)
        logger.warning(
            "webhook email dispatch failed: %s (kwargs=%r)",
            func_name, kwargs, exc_info=True,
        )


def _observe(event_type: str, outcome: str) -> None:
    """Lazy metrics hook (imported on demand). Never raises."""
    try:
        from shared.metrics import observe_stripe_event  # noqa: PLC0415

        observe_stripe_event(event_type, outcome)
    except Exception:  # pragma: no cover
        pass
