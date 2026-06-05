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
import ipaddress
import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

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

# DNS lookup cap for validate_webhook_url_safe. The default Linux
# resolver timeout is ~30s, which lets a malicious hostname stall any
# request thread that hits this code path. 2 seconds is generous for
# any legitimate authoritative NS in the relevant geos.
_DNS_LOOKUP_TIMEOUT_SECONDS = float(
    os.environ.get("WEBHOOK_DNS_LOOKUP_TIMEOUT_SECONDS", "2.0")
)

# Concurrency cap on in-flight dispatch threads.
# Each thread sleeps up to 6h between retries, holds an HTTPS
# connection-pool slot, and runs the Supabase service client through
# 5 update calls per delivery. At Railway's RAM ceiling, unbounded
# threads turn a misbehaving subscriber into a service-wide RAM exhaustion
# vector. The semaphore is per-process — if Railway scales to N replicas,
# each replica gets its own pool of _MAX_INFLIGHT.
_MAX_INFLIGHT = int(os.environ.get("WEBHOOK_DISPATCH_MAX_INFLIGHT", "8"))
_dispatch_semaphore = threading.BoundedSemaphore(_MAX_INFLIGHT)

# Process-wide HTTP session for connection reuse + bounded pool.
_session = requests.Session()
_session.mount(
    "https://",
    requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=_MAX_INFLIGHT),
)
_session.mount(
    "http://",
    requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=_MAX_INFLIGHT),
)


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


# ---------------------------------------------------------------------------
# Webhook URL safety (FIX #14 from the validation review).
#
# An attacker-controlled webhook_url can be pointed at:
#   - Cloud metadata endpoints (169.254.169.254 → AWS / 100.100.100.200 → Alibaba)
#   - Railway's internal CIDR (100.64.0.0/10) or other private networks
#   - localhost / loopback (127.0.0.0/8) / link-local / multicast
#   - Cleartext HTTP (signature verification still works on the receiver,
#     but the body is observable on the path)
#
# Each delivery's `last_error` column captures up to 200 chars of the
# response body, so any HTTP 200 from an internal endpoint leaks data
# back to the attacker. We reject these at validation time. Hostname
# resolution happens at dispatch time too because DNS rebinding can flip
# an allowed name to a private IP after the URL is stored.
# ---------------------------------------------------------------------------


# RFC1918 + RFC6598 (CGNAT, used by Railway) + RFC4291 (IPv6 unique-local /
# link-local) + loopback + link-local IPv4 + multicast.
def _is_private_or_special_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True  # Reject anything that isn't a real IP
    if ip.is_private:  # 10.0/8, 172.16/12, 192.168/16, 169.254/16, fc00::/7, fe80::/10
        return True
    if ip.is_loopback:
        return True
    if ip.is_link_local:
        return True
    if ip.is_multicast:
        return True
    if ip.is_reserved or ip.is_unspecified:
        return True
    # RFC6598 shared address space — used by Railway and CGNAT.
    if isinstance(ip, ipaddress.IPv4Address):
        if ipaddress.IPv4Network("100.64.0.0/10").supernet_of(
            ipaddress.IPv4Network(f"{addr}/32")
        ):
            return True
    return False


class UnsafeWebhookURLError(ValueError):
    """Raised when a webhook_url fails the SSRF guard."""


def validate_webhook_url_safe(url: str) -> None:
    """Validate that ``url`` is safe to POST to from the server.

    Raises :class:`UnsafeWebhookURLError` on:
      - Missing or malformed URL
      - http:// scheme (cleartext)
      - URL contains credentials (user:pass@ form)
      - Host is a private IP, loopback, link-local, multicast, etc.
      - Host resolves to a private/loopback/link-local IP via DNS
      - Non-default port outside the standard set (443 only allowed)

    The check runs at submission time AND immediately before dispatch
    (DNS rebinding defence). Service-internal callers MUST NOT bypass.
    """
    if not url:
        raise UnsafeWebhookURLError("webhook_url is empty")
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise UnsafeWebhookURLError(f"webhook_url is not a valid URL: {exc}")
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise UnsafeWebhookURLError(
            "webhook_url must use https:// (cleartext http is rejected)"
        )
    if parsed.username or parsed.password:
        raise UnsafeWebhookURLError(
            "webhook_url must not embed credentials"
        )
    host = parsed.hostname
    if not host:
        raise UnsafeWebhookURLError("webhook_url is missing a host")

    # IP-literal vs hostname dispatch. The `try` is narrow — only the
    # `ip_address()` call goes inside — because UnsafeWebhookURLError IS a
    # ValueError; if we wrap the rejection raise in the same try/except,
    # the rejection gets swallowed and execution falls through to the DNS
    # path. The current shape would still reject (DNS just echoes the
    # literal back), but the security claim "we check literals first"
    # would be wrong, error text would be misleading, and the port check
    # below would be unreachable. See validation re-review finding #1.
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False

    if is_literal:
        if _is_private_or_special_ip(host):
            raise UnsafeWebhookURLError(
                "webhook_url targets a private or special-use IP"
            )
    else:
        # FIX HI-05 (fresh-review): socket.getaddrinfo has no per-call
        # timeout argument and inherits the OS resolver's default (often
        # 30s on Linux). A malicious caller can submit a hostname whose
        # authoritative DNS is intentionally slow and stall the request
        # thread for the full default. Cap the lookup to 2 seconds via
        # the process-wide default socket timeout — narrowly scoped so
        # we restore the previous value on the way out and don't affect
        # any other socket op on this thread.
        prior_default = socket.getdefaulttimeout()
        socket.setdefaulttimeout(_DNS_LOOKUP_TIMEOUT_SECONDS)
        try:
            addrinfo = socket.getaddrinfo(
                host, None, proto=socket.IPPROTO_TCP
            )
        except socket.gaierror as exc:
            raise UnsafeWebhookURLError(
                f"webhook_url host could not be resolved: {exc}"
            )
        except socket.timeout as exc:
            raise UnsafeWebhookURLError(
                f"webhook_url DNS lookup timed out after "
                f"{_DNS_LOOKUP_TIMEOUT_SECONDS}s: {exc}"
            )
        finally:
            socket.setdefaulttimeout(prior_default)
        # Every record must be public. A multi-A response with a single
        # private entry is treated as unsafe (a sometimes-public DNS
        # rebinding attack lands here).
        for entry in addrinfo:
            addr = entry[4][0]
            if _is_private_or_special_ip(addr):
                raise UnsafeWebhookURLError(
                    f"webhook_url host resolves to a private IP ({addr})"
                )

    # Restrict ports. Runs for both literal and hostname paths so we
    # don't accept, e.g., https://1.1.1.1:9999/ — even though the IP
    # itself is public.
    port = parsed.port
    if port is not None and port != 443:
        raise UnsafeWebhookURLError(
            "webhook_url must use the default https port (443)"
        )


# ---------------------------------------------------------------------------
# Delivery ledger
# ---------------------------------------------------------------------------


def _enqueue_delivery(
    *,
    delivery_id: str,
    campaign_id: str,
    target_url: str,
    event_type: str,
    payload: dict[str, Any],
) -> Optional[str]:
    """Insert a webhook_deliveries row with a caller-supplied id.

    The id is generated by ``dispatch_webhook`` BEFORE the row insert so
    it can be baked into the signed payload (FIX #4 from validation
    review). Returns the id on success, None on DB failure.
    """
    client = get_service_client()
    if client is None:
        logger.warning("webhook enqueue: service client unavailable")
        return None
    row = {
        "id": delivery_id,
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
        resp = _session.post(
            target_url,
            data=body,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,  # No 30x to attacker-chosen targets.
        )
    except requests.RequestException as exc:
        return (False, f"requests error: {exc}")
    except Exception as exc:
        # FIX ME-02 (fresh-review): a non-requests exception here would
        # bubble out of _dispatch_loop and kill the worker thread, leaving
        # the webhook_deliveries row orphaned with no retry. Catch broadly
        # and convert to a normal failure return so the row records
        # last_error and the dispatch loop schedules the next retry.
        return (False, f"unexpected error: {exc.__class__.__name__}: {exc}")
    if 200 <= resp.status_code < 300:
        return (True, f"http {resp.status_code}")
    # Capture a short snippet but never let an attacker's body leak more
    # than 200 chars of internal state into our DB. Decoding the response
    # body can itself raise (LookupError on a bogus Content-Type charset,
    # UnicodeDecodeError on malformed bytes); contain those too.
    try:
        snippet = (resp.text or "")[:200]
    except Exception:
        snippet = "<response body undecodable>"
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
        # FIX HI-02 (fresh-review): re-validate the URL at the top of
        # every retry. The validation done in dispatch_webhook closes
        # the once-at-enqueue rebinding window, but a 6h retry schedule
        # gives a DNS-rebinding attacker plenty of room to flip the
        # host's A record to an internal IP between attempt N-1 and N.
        # Without this, retries 2-6 happily POST to whatever the resolver
        # returns at request time. We stamp the row and stop on rebind
        # so the operator sees the rejection in webhook_deliveries.last_error.
        try:
            validate_webhook_url_safe(target_url)
        except UnsafeWebhookURLError as exc:
            _update_delivery(
                delivery_id=delivery_id,
                attempts=attempt_index,
                delivered_at=datetime.now(timezone.utc),
                last_error=f"rebind blocked at retry {attempt_index}: {exc}",
            )
            return
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


def _bounded_dispatch(
    *,
    delivery_id: str,
    target_url: str,
    payload: dict[str, Any],
) -> None:
    """Wrap ``_dispatch_loop`` in the concurrency semaphore.

    If the semaphore can't be acquired immediately (too many in-flight
    deliveries), we stamp the row with last_error and a future
    next_retry_at so a later cron / next dispatch wave can pick it up.
    """
    if not _dispatch_semaphore.acquire(blocking=False):
        logger.warning(
            "webhook dispatch backpressure: %s queued (cap=%d in-flight)",
            delivery_id,
            _MAX_INFLIGHT,
        )
        _update_delivery(
            delivery_id=delivery_id,
            attempts=0,
            last_error=(
                f"backpressure: dispatch capacity ({_MAX_INFLIGHT}) saturated; "
                "row left queued for next attempt"
            ),
            next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        return
    try:
        _dispatch_loop(
            delivery_id=delivery_id, target_url=target_url, payload=payload
        )
    finally:
        _dispatch_semaphore.release()


def dispatch_webhook(
    *,
    campaign_id: str,
    event_type: str,
    payload: dict[str, Any],
    target_url: Optional[str],
) -> Optional[str]:
    """Fire-and-forget a signed webhook.

    Returns the ``delivery_id`` or None when delivery cannot start (no URL,
    no service client, no signing secret, unsafe target). The actual POST
    runs on a daemon thread bounded by a per-process semaphore so a single
    misbehaving subscriber cannot exhaust process RAM.

    The signed body INCLUDES the delivery_id so receivers can dedup
    retries — FIX #4 from the validation review. The id is minted here
    BEFORE row insert so the persisted payload (in webhook_deliveries.
    payload) and the signed bytes both reference the same id.
    """
    if not target_url:
        return None
    if not _signing_secret():
        logger.error(
            "dispatch_webhook refused: WEBHOOK_SIGNING_SECRET not set "
            "(campaign_id=%s)",
            campaign_id,
        )
        return None

    # Re-validate at dispatch time. URL was validated when stored, but
    # DNS rebinding can flip a once-safe host to an internal IP after
    # the row was written. Same code path, no bypass.
    try:
        validate_webhook_url_safe(target_url)
    except UnsafeWebhookURLError as exc:
        logger.warning(
            "dispatch_webhook refused: unsafe target_url (campaign=%s): %s",
            campaign_id,
            exc,
        )
        return None

    # Mint id locally so we can bake it into the signed payload before
    # persisting (FIX #4).
    delivery_id = str(uuid.uuid4())
    signed_payload = {**payload, "delivery_id": delivery_id}

    written = _enqueue_delivery(
        delivery_id=delivery_id,
        campaign_id=campaign_id,
        target_url=target_url,
        event_type=event_type,
        payload=signed_payload,
    )
    if written is None:
        return None

    thread = threading.Thread(
        target=_bounded_dispatch,
        kwargs={
            "delivery_id": delivery_id,
            "target_url": target_url,
            "payload": signed_payload,
        },
        name=f"webhook-dispatch-{delivery_id[:8]}",
        daemon=True,
    )
    thread.start()
    return delivery_id
