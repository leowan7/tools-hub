"""Resend delivery-event webhook: POST /webhooks/resend.

Resend signs each delivery with Svix: headers ``svix-id``, ``svix-timestamp``
and ``svix-signature`` (space-separated ``v1,<base64>`` entries), an
HMAC-SHA256 over ``{svix-id}.{svix-timestamp}.{raw body}`` keyed by the
base64 part of ``RESEND_WEBHOOK_SECRET`` (``whsec_...``).

Each verified event becomes one ``public.email_events`` row keyed on
``svix-id``; a retried delivery keeps its ``svix-id``, so the upsert with
``ignore_duplicates`` leaves the first row as is. Kind and user come from
the ``kind`` / ``user_id`` tags that ``shared.email._resend_tags`` puts on
each send (``user_id`` only where the send's log tag carries ``user=``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any, Optional

from flask import Flask, Response, jsonify, request

from shared.credits import get_service_client

logger = logging.getLogger(__name__)

TOLERANCE_SECONDS = 300
STORED_EVENT_TYPES = frozenset({
    "email.sent",
    "email.delivered",
    "email.delivery_delayed",
    "email.bounced",
    "email.complained",
    "email.opened",
    "email.clicked",
})


def verify_signature(
    secret: str, msg_id: str, timestamp: str, signature_header: str, body: bytes,
    *, now: Optional[float] = None,
) -> bool:
    """True when one ``v1`` signature matches and the timestamp is fresh."""
    if not (secret and msg_id and timestamp and signature_header):
        return False
    try:
        ts = int(timestamp)
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (ValueError, binascii.Error):
        return False
    if abs((time.time() if now is None else now) - ts) > TOLERANCE_SECONDS:
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for entry in signature_header.split():
        version, _, sig = entry.partition(",")
        if version == "v1" and hmac.compare_digest(sig.encode(), expected.encode()):
            return True
    return False


def _uuid_or_none(value: Any) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        return None


def event_row(msg_id: str, event: dict) -> Optional[dict]:
    """The ``email_events`` row for ``event``, or None when it is not stored."""
    data = event.get("data") or {}
    if event.get("type") not in STORED_EVENT_TYPES or not data.get("email_id"):
        return None
    tags = data.get("tags")
    if isinstance(tags, list):  # send-side shape, in case Resend echoes it
        tags = {t.get("name"): t.get("value") for t in tags if isinstance(t, dict)}
    tags = tags if isinstance(tags, dict) else {}
    to = data.get("to")
    recipient = to[0] if isinstance(to, list) and to else (to if isinstance(to, str) else None)
    return {
        "svix_id": msg_id,
        "resend_email_id": str(data["email_id"]),
        "event_type": event["type"].removeprefix("email."),
        "recipient": recipient,
        "email_kind": tags.get("kind"),
        "user_id": _uuid_or_none(tags.get("user_id")),
        "event_at": event.get("created_at"),
    }


def register_resend_webhook(flask_app: Flask) -> None:
    """Attach ``/webhooks/resend`` to ``flask_app``."""

    @flask_app.route("/webhooks/resend", methods=["POST"])
    def resend_webhook() -> Any:  # noqa: ANN401
        secret = os.environ.get("RESEND_WEBHOOK_SECRET", "").strip()
        if not secret:
            logger.error("RESEND_WEBHOOK_SECRET unset; refusing Resend webhook")
            return Response("webhook not configured", status=503)
        body = request.get_data()
        msg_id = request.headers.get("svix-id", "")
        if not verify_signature(
            secret, msg_id, request.headers.get("svix-timestamp", ""),
            request.headers.get("svix-signature", ""), body,
        ):
            return Response("invalid signature", status=400)
        try:
            event = json.loads(body)
        except ValueError:
            return Response("malformed event", status=400)
        row = event_row(msg_id, event) if isinstance(event, dict) else None
        if row is None:
            return jsonify({"status": "ignored"})
        client = get_service_client()
        if client is None:
            return Response("event store unavailable", status=503)
        try:
            client.table("email_events").upsert(
                row, on_conflict="svix_id", ignore_duplicates=True,
            ).execute()
        except Exception:
            logger.warning("email_events write failed for %s", msg_id, exc_info=True)
            return Response("event store unavailable", status=503)
        return jsonify({"status": "stored"})
