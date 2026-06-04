"""Signed asynchronous webhook delivery for the Platform API.

When a lab_campaigns row transitions, customers with a ``webhook_url``
set on the row receive a signed POST with the new state. Delivery is:

- **Async** — fired from a background daemon thread so the API response
  returns immediately.
- **Signed** — Stripe-style ``X-Ranomics-Signature: t=<ts>,v1=<hmac>``.
- **Idempotent on the receiver side** — payload includes a stable
  ``delivery_id`` (the webhook_deliveries.id), so receivers can dedup
  retries.
- **Persistent** — every attempt writes/updates a ``webhook_deliveries``
  row so an operator can audit failures.
- **Retried** — up to 5 attempts, exponential backoff
  (30s, 2min, 10min, 1h, 6h, then give up and stamp delivered_at).

Scope note
----------
This module ships an in-process thread-based dispatcher. That's fine
for the private alpha (a handful of subscribers, low transition volume).
The longer-term move is a separate cron worker that polls
``webhook_deliveries WHERE delivered_at IS NULL AND next_retry_at <= now()``
— the schema already supports that (and ``next_retry_at`` is indexed),
so the cutover is mechanical when needed.

Security
--------
- ``WEBHOOK_SIGNING_SECRET`` is required for signed delivery. Without
  it the dispatcher logs an error and refuses to fire — failing closed
  is the right move because an unsigned payload is indistinguishable
  from a malicious attacker hitting the receiver's URL.
- The signing scheme follows Stripe's docs so the customer-side
  verification code is well-known and easy to copy.
- Receivers are URL-fetched with a hard 8-second timeout to avoid a
  hung subscriber pinning a worker thread.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from shared.credits import get_service_client

logger = logging.getLogger(__name__)

_TABLE = "webhook_deliveries"

# Backoff schedule (seconds). Indexed by attempts count *before* this
# delivery, so the first retry waits 30s after the failure, etc.
# After the schedule is exhausted, the row is stamped delivered_at
# with last_error retained — we don't keep retrying forever.
_BACKOFF_SECONDS = (30, 120, 600, 3600, 21600)
_MAX_ATTEMPTS = len(_BACKOFF_SECONDS) + 1  # 1 initial + 5 retries

_REQUEST_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class WebhookPayload:
    """Stable shape posted to ``webhook_url``.

    Keep this dict stable across versions — agents will parse it. New
    optional fields are fine; renames or removals are breaking.
    """

    delivery_id: str
    event_type: str
    experiment_id: str
    prev_status: Optional[str]
    new_status: str
    results_status: str
    timestamp: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "event_type": self.event_type,
            "experiment_id": self.experiment_id,
            "prev_status": self.prev_status,
            "new_status": self.new_status,
            "results_status": self.results_status,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def _signing_secret() -> Optional[str]:
    return (os.environ.get("WEBHOOK_SIGNING_SECRET") or "").strip() or None


def sign_payload(timestamp: int, body: bytes, secret: str) -> str:
    """Return the ``v1`` HMAC for a webhook payload.

    Stripe scheme: ``HMAC_SHA256(secret, f"{timestamp}.{body}")``,
    hex-encoded.
    """
    signed = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def format_signature_header(timestamp: int, v1: str) -> str:
    """Format the ``X-Ranomics-Signature`` header value."""
    return f"t={timestamp},v1={v1}"


def verify_signature(
    header_value: str,
    body: bytes,
    secret: str,
    *,
    tolerance_seconds: int = 300,
) -> bool:
    """Constant-time verification of an incoming signature header.

    Provided here so an internal receiver (or a test) can reuse the
    same code path the customer would.
    """
    if not header_value or not secret:
        return False
    parts = {}
    for piece in header_value.split(","):
        if "=" in piece:
            k, _, v = piece.strip().partition("=")
            parts[k.strip()] = v.strip()
    try:
        ts = int(parts.get("t", "0"))
    except (TypeError, ValueError):
        return False
    v1 = parts.get("v1", "")
    if not ts or not v1:
        return False
    if abs(time.time() - ts) > tolerance_seconds:
        return False
    expected = sign_payload(ts, body, secret)
    return hmac.compare_digest(expected, v1)


# ---------------------------------------------------------------------------
# Delivery ledger
# ---------------------------------------------------------------------------


def _enqueue_delivery(
    *,
    campaign_id: str,
    target_url: str,
    event_type: str,
    payload: dict[str, Any],
) -> Optional[str]:
    """Insert a webhook_deliveries row, returning its id."""
    client = get_service_client()
    if client is None:
        logger.warning("webhook enqueue: service client unavailable")
        return None
    row = {
        "campaign_id": campaign_id,
        "target_url": target_url,
        "event_type": event_type,
        "payload": payload,
        "next_retry_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = client.table(_TABLE).insert(row).execute()
    except Exception:
        logger.error("webhook enqueue: insert failed", exc_info=True)
        return None
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    return str(rows[0]["id"])


def _update_delivery(
    *,
    delivery_id: str,
    attempts: int,
    delivered_at: Optional[datetime] = None,
    last_error: Optional[str] = None,
    next_retry_at: Optional[datetime] = None,
) -> None:
    client = get_service_client()
    if client is None:
        return
    patch: dict[str, Any] = {"attempts": attempts}
    if delivered_at is not None:
        patch["delivered_at"] = delivered_at.isoformat()
    if last_error is not None:
        patch["last_error"] = last_error[:2000]
    if next_retry_at is not None:
        patch["next_retry_at"] = next_retry_at.isoformat()
    try:
        client.table(_TABLE).update(patch).eq("id", delivery_id).execute()
    except Exception:
        logger.warning("webhook update failed for %s", delivery_id, exc_info=True)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _post_once(target_url: str, body: bytes, signature: str) -> tuple[bool, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Ranomics-Signature": signature,
        "User-Agent": "Ranomics-Webhook/1.0",
    }
    try:
        resp = requests.post(
            target_url,
            data=body,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return (False, f"requests error: {exc}")
    if 200 <= resp.status_code < 300:
        return (True, f"http {resp.status_code}")
    snippet = (resp.text or "")[:200]
    return (False, f"http {resp.status_code}: {snippet}")


def _dispatch_loop(*, delivery_id: str, target_url: str, payload: dict[str, Any]) -> None:
    secret = _signing_secret()
    if not secret:
        logger.error(
            "webhook dispatch refused: WEBHOOK_SIGNING_SECRET not set "
            "(delivery_id=%s)",
            delivery_id,
        )
        _update_delivery(
            delivery_id=delivery_id,
            attempts=_MAX_ATTEMPTS,
            delivered_at=datetime.now(timezone.utc),
            last_error="WEBHOOK_SIGNING_SECRET not configured",
        )
        return

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    for attempt_index in range(_MAX_ATTEMPTS):
        ts = int(time.time())
        sig = format_signature_header(ts, sign_payload(ts, body, secret))
        ok, message = _post_once(target_url, body, sig)
        attempts = attempt_index + 1
        if ok:
            _update_delivery(
                delivery_id=delivery_id,
                attempts=attempts,
                delivered_at=datetime.now(timezone.utc),
                last_error=None,
            )
            return

        if attempt_index < len(_BACKOFF_SECONDS):
            wait_seconds = _BACKOFF_SECONDS[attempt_index]
            next_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
            _update_delivery(
                delivery_id=delivery_id,
                attempts=attempts,
                last_error=message,
                next_retry_at=next_at,
            )
            time.sleep(wait_seconds)
        else:
            # Out of retries. Stamp delivered_at so the queue forgets it,
            # but keep last_error for the audit trail.
            _update_delivery(
                delivery_id=delivery_id,
                attempts=attempts,
                delivered_at=datetime.now(timezone.utc),
                last_error=message,
            )
            return


def dispatch_webhook(
    *,
    campaign_id: str,
    event_type: str,
    payload: dict[str, Any],
    target_url: Optional[str],
) -> Optional[str]:
    """Fire-and-forget a signed webhook.

    Returns the ``delivery_id`` (so callers can correlate) or None when
    delivery cannot start (no URL, no service client, no signing secret).
    The actual POST happens on a daemon thread so the request handler
    returns immediately.
    """
    if not target_url:
        return None
    if not _signing_secret():
        # Same fail-closed posture as inside the loop, but logged here
        # so the operator sees it on the first transition rather than
        # buried in webhook_deliveries.last_error.
        logger.error(
            "dispatch_webhook refused: WEBHOOK_SIGNING_SECRET not set "
            "(campaign_id=%s)",
            campaign_id,
        )
        return None

    delivery_id = _enqueue_delivery(
        campaign_id=campaign_id,
        target_url=target_url,
        event_type=event_type,
        payload=payload,
    )
    if delivery_id is None:
        return None

    thread = threading.Thread(
        target=_dispatch_loop,
        kwargs={
            "delivery_id": delivery_id,
            "target_url": target_url,
            "payload": payload,
        },
        name=f"webhook-dispatch-{delivery_id[:8]}",
        daemon=True,
    )
    thread.start()
    return delivery_id
