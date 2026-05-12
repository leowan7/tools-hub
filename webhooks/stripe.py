"""Stripe webhook handler for tools-hub Workspace activations + refunds.

Responsibilities
----------------
1. Verify ``Stripe-Signature`` via ``STRIPE_WEBHOOK_SECRET``.
2. Idempotency gate on ``public.stripe_events.event_id`` (PRIMARY KEY).
3. On ``checkout.session.completed``: read ``metadata.sku`` and
   ``metadata.target_pdb_id`` and call
   ``shared.workspaces.activate_workspace``.
4. ``POST /webhooks/refund-request`` (auth required, called from the
   workspace dashboard): verify first-Workspace + 7-day eligibility,
   issue Stripe refund, flip workspace status to ``refunded``.

History
-------
Previously this handler flipped a monthly subscription tier and granted
credits on ``checkout.session.completed`` / ``customer.subscription.*`` /
``invoice.paid``. Replaced 2026-05-11 with one-time Workspace SKU
activations.

Registering
-----------
::

    from webhooks.stripe import register_stripe_webhook
    register_stripe_webhook(flask_app)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from flask import Flask, Response, jsonify, request, session

from billing.checkout import issue_workspace_refund
from billing.tiers import lookup_sku
from shared.credits import get_service_client
from shared.workspaces import (
    activate_workspace,
    get_workspace,
    request_refund,
)

logger = logging.getLogger(__name__)


# Only one-time-payment events. No subscription/invoice events — the
# Workspace SKU is a one-shot purchase.
HANDLED_EVENT_TYPES = {
    "checkout.session.completed",
    # Stripe sends charge.refunded asynchronously after a refund is
    # issued; we already flip workspace state when we issue the refund
    # via /webhooks/refund-request, so this is informational only.
    "charge.refunded",
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
        return dict(event)
    except Exception as exc:  # ValueError, SignatureVerificationError, etc.
        logger.warning("Stripe signature verification failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Idempotency + persistence
# ---------------------------------------------------------------------------


def _insert_event_once(event: dict) -> bool:
    """Insert the event into ``stripe_events``. Return False on duplicate.

    Uses the PRIMARY KEY on ``event_id`` as the idempotency gate.
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
        # Could be unique-constraint violation (replay) or transient
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
# User resolution
# ---------------------------------------------------------------------------


def _resolve_user_id_from_customer(
    customer_id: Optional[str],
    customer_email: Optional[str],
) -> Optional[str]:
    """Resolve a Supabase user_id from a Stripe customer id or email.

    Preference order:
      1. ``user_tier`` row matched on stripe_customer_id (legacy table —
         still used to map repeat customers).
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


def _ensure_customer_mapping(
    user_id: str, customer_id: Optional[str]
) -> None:
    """Stash stripe_customer_id on user_tier for repeat-purchase lookups.

    Workspace SKUs are one-time payments but customers come back to buy
    more. The user_tier table is reused as a customer ↔ Stripe map
    (tier='free' for everyone; the Workspace state lives elsewhere).
    """
    if not customer_id:
        return
    client = get_service_client()
    if client is None:
        return
    try:
        client.table("user_tier").upsert(
            {
                "user_id": user_id,
                "tier": "free",  # legacy column; not used by Workspace flow
                "stripe_customer_id": customer_id,
            },
            on_conflict="user_id",
        ).execute()
    except Exception:
        logger.warning(
            "Could not stash stripe_customer_id for user %s",
            user_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Workspace activation handler
# ---------------------------------------------------------------------------


def _apply_checkout_event(event: dict) -> dict:
    """Activate a Workspace from a ``checkout.session.completed`` event.

    Reads ``metadata.sku`` and ``metadata.target_pdb_id`` and delegates
    to ``shared.workspaces.activate_workspace``. Returns a status dict
    for logging.
    """
    obj = event.get("data", {}).get("object", {}) or {}

    # Defensive: only process paid sessions.
    payment_status = obj.get("payment_status")
    if payment_status not in (None, "paid", "no_payment_required"):
        logger.info(
            "checkout.session.completed with payment_status=%s — skipping",
            payment_status,
        )
        return {"status": "skipped", "reason": f"payment_status_{payment_status}"}

    metadata = obj.get("metadata") or {}
    sku = (metadata.get("sku") or "").strip()
    target_pdb_id = (metadata.get("target_pdb_id") or "").strip()
    target_label = (metadata.get("target_label") or "").strip() or None

    # Sanity-check the SKU is one we recognise. Don't trust the price-id
    # alone — metadata is the contract with billing/checkout.py.
    if not sku or sku not in {"workspace_standard", "workspace_xl"}:
        # Fallback: try resolving via price id (single line item).
        price_id = _line_item_price_id(obj)
        sku_obj = lookup_sku(price_id) if price_id else None
        if sku_obj is None:
            logger.warning(
                "checkout.session.completed missing/unknown sku metadata "
                "(sku=%r price=%r event=%s)",
                sku, price_id, event.get("id"),
            )
            return {"status": "skipped", "reason": "unknown_sku"}
        sku = sku_obj.sku

    if not target_pdb_id:
        logger.warning(
            "checkout.session.completed missing target_pdb_id metadata "
            "(event=%s)", event.get("id"),
        )
        return {"status": "skipped", "reason": "missing_target_pdb_id"}

    # Resolve user.
    customer_id = obj.get("customer")
    customer_email = (
        obj.get("customer_email")
        or (obj.get("customer_details") or {}).get("email")
    )
    user_id = _resolve_user_id_from_customer(customer_id, customer_email)
    if not user_id:
        logger.warning(
            "Stripe event %s had no resolvable user (customer=%s email=%s)",
            event.get("id"), customer_id, customer_email,
        )
        return {"status": "skipped", "reason": "user_not_found"}

    _ensure_customer_mapping(user_id, customer_id)

    # Stripe stores the PaymentIntent id on the session. We need it for
    # later refund issuance.
    payment_intent_id = obj.get("payment_intent")
    if not isinstance(payment_intent_id, str) or not payment_intent_id:
        # Stripe sometimes returns a PaymentIntent object dict; coerce.
        if isinstance(payment_intent_id, dict):
            payment_intent_id = payment_intent_id.get("id")
        if not payment_intent_id:
            logger.warning(
                "checkout.session.completed missing payment_intent for event %s",
                event.get("id"),
            )

    ws = activate_workspace(
        user_id=user_id,
        target_pdb_id=target_pdb_id,
        sku=sku,
        target_label=target_label,
        stripe_payment_intent_id=payment_intent_id,
        stripe_event_id=event.get("id"),
    )
    if ws is None:
        return {"status": "error", "reason": "activation_failed"}
    return {
        "status": "ok",
        "workspace_id": ws.id,
        "sku": sku,
        "target_pdb_id": target_pdb_id,
    }


def _line_item_price_id(obj: dict) -> Optional[str]:
    """Best-effort: pull the single line-item price id from the session.

    Stripe omits line_items from the default webhook payload (privacy); the
    canonical pattern is to call ``stripe.checkout.Session.retrieve(
    expand=["line_items"])``. We only need this as a fallback when the
    metadata round-trip lost the sku, so do the retrieve here.
    """
    session_id = obj.get("id")
    if not session_id:
        return None
    try:
        import stripe  # noqa: PLC0415
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        full = stripe.checkout.Session.retrieve(
            session_id, expand=["line_items"]
        )
        line_items = (full.get("line_items") or {}).get("data") or []
        if line_items:
            price = line_items[0].get("price") or {}
            if price.get("id"):
                return price["id"]
    except Exception:
        logger.warning(
            "Could not expand line_items for session %s.",
            session_id, exc_info=True,
        )
    return None


# ---------------------------------------------------------------------------
# Flask integration
# ---------------------------------------------------------------------------


def register_stripe_webhook(flask_app: Flask) -> None:
    """Attach Stripe webhook + refund-request routes to the given app."""

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

        # Idempotency gate. Duplicate event id returns 200 so Stripe stops
        # retrying.
        if not _insert_event_once(event):
            _observe(event_type, "duplicate")
            return jsonify(
                {"status": "already_processed", "event_id": event_id}
            )

        result: dict = {"status": "ignored", "event_type": event_type}
        outcome = "ignored"
        if event_type == "checkout.session.completed":
            try:
                result = _apply_checkout_event(event)
                outcome = str(result.get("status") or "ok")
            except Exception:
                logger.exception(
                    "Error applying Stripe checkout event %s", event_id
                )
                result = {"status": "error"}
                outcome = "error"
        elif event_type == "charge.refunded":
            # Informational only — we already flipped state on the
            # refund-request endpoint when the refund was issued.
            result = {"status": "noted", "event_type": event_type}
            outcome = "noted"

        _mark_processed(event_id)
        _observe(event_type, outcome)
        result["event_id"] = event_id
        return jsonify(result)

    @flask_app.route("/webhooks/refund-request", methods=["POST"])
    def refund_request() -> Any:  # noqa: ANN401
        """Customer-initiated refund button on the workspace dashboard.

        Auth required (Flask session). Verifies eligibility, issues
        Stripe refund, flips workspace status to ``refunded``.

        Body (form-encoded or JSON):
            workspace_id: str  (required)
        """
        # Auth check.
        user_email = session.get("user_email")
        user_id = session.get("user_id")
        if not user_email or not user_id:
            return jsonify({"error": "not signed in"}), 401

        # Extract workspace_id from JSON body or form.
        workspace_id = None
        if request.is_json:
            body = request.get_json(silent=True) or {}
            workspace_id = (body.get("workspace_id") or "").strip()
        if not workspace_id:
            workspace_id = (request.form.get("workspace_id") or "").strip()
        if not workspace_id:
            return jsonify({"error": "missing workspace_id"}), 400

        ws = get_workspace(workspace_id)
        if ws is None:
            return jsonify({"error": "workspace not found"}), 404
        if ws.user_id != user_id:
            logger.warning(
                "Refund attempted by non-owner: user=%s ws.user=%s ws=%s",
                user_id, ws.user_id, workspace_id,
            )
            return jsonify({"error": "not authorized"}), 403
        if not ws.refund_eligible_now:
            return jsonify({
                "error": "not refund eligible",
                "reason": (
                    "Refund window expired or this is not your first Workspace."
                ),
            }), 409
        if ws.refunded_at is not None:
            return jsonify({"error": "already refunded"}), 409
        if not ws.stripe_payment_intent_id:
            logger.error(
                "Workspace %s has no payment_intent_id — cannot refund",
                workspace_id,
            )
            return jsonify({
                "error": "no payment intent on record",
                "reason": "Contact support — your purchase wasn't fully recorded.",
            }), 500

        # Issue the Stripe refund.
        refund_id, err = issue_workspace_refund(
            workspace_id, ws.stripe_payment_intent_id
        )
        if not refund_id:
            return jsonify({"error": err or "refund failed"}), 502

        # Flip workspace state.
        updated = request_refund(
            workspace_id, stripe_refund_id=refund_id
        )
        if updated is None:
            # Stripe issued the refund but DB update failed — log loudly.
            logger.error(
                "DB update failed AFTER Stripe refund issued: ws=%s refund=%s",
                workspace_id, refund_id,
            )
            return jsonify({
                "error": "refund issued but workspace state update failed",
                "stripe_refund_id": refund_id,
                "reason": "Contact support to reconcile.",
            }), 500

        return jsonify({
            "status": "refunded",
            "workspace_id": workspace_id,
            "stripe_refund_id": refund_id,
        })


def _observe(event_type: str, outcome: str) -> None:
    """Lazy-imported metrics hook. Never raises."""
    try:
        from shared.metrics import observe_stripe_event  # noqa: PLC0415
        observe_stripe_event(event_type, outcome)
    except Exception:  # pragma: no cover
        pass
