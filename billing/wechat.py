"""WeChat Pay wallet top-ups, charged in yuan, credited in US dollars.

Stripe's Checkout and Stripe.js domains are blocked in mainland China, so the
visitor's browser never talks to Stripe here. Our server creates and confirms a
PaymentIntent, turns ``next_action.wechat_pay_display_qr_code.data`` into a QR
image itself, and the page polls our own status route.

The Stripe account is Canadian, and WeChat Pay on a Canadian account charges
only CNY or CAD, so the USD amount the visitor picks is converted to CNY when
the quote is made. The USD credit, the CNY charge and the rate ride on the
PaymentIntent metadata; ``webhooks/stripe.py`` credits the stored ``amount_usd``.
"""

from __future__ import annotations

import logging
import time
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Optional, Tuple

import requests

from billing.checkout import _stripe_client, validate_topup_amount
from shared.stripe_compat import as_dict

logger = logging.getLogger(__name__)

CHANNEL = "wechat_pay"

# European Central Bank daily reference rate, served free without a key.
_RATE_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY"
_RATE_TTL_SECONDS = 3600
# A reading outside this band is treated as a broken feed: no quote is made.
_RATE_MIN = Decimal("5")
_RATE_MAX = Decimal("10")
# A quote shown to the visitor is honoured for this long, then re-quoted.
QUOTE_TTL_SECONDS = 15 * 60

# ponytail: per-process cache; each gunicorn worker fetches once an hour.
_rate_cache: dict = {}


def usd_cny_rate() -> Tuple[Optional[Decimal], Optional[str]]:
    """Return ``(rate, rate_date)`` for 1 USD in CNY, or ``(None, None)`` if unavailable."""
    now = time.time()
    if _rate_cache and now - _rate_cache["at"] < _RATE_TTL_SECONDS:
        return _rate_cache["rate"], _rate_cache["date"]
    try:
        resp = requests.get(_RATE_URL, timeout=5)
        resp.raise_for_status()
        body = resp.json()
        rate = Decimal(str(body["rates"]["CNY"]))
        rate_date = str(body.get("date") or "")
    except (requests.RequestException, ValueError, KeyError, TypeError, InvalidOperation):
        logger.warning("usd_cny_rate: rate fetch failed", exc_info=True)
        return None, None
    if not (_RATE_MIN <= rate <= _RATE_MAX):
        logger.error("usd_cny_rate: rate %s outside sanity band; refusing", rate)
        return None, None
    _rate_cache.update(rate=rate, date=rate_date, at=now)
    return rate, rate_date


def make_quote(amount_usd) -> Tuple[Optional[dict], Optional[str]]:
    """Quote a USD top-up in CNY. Returns ``(quote, None)`` or ``(None, error)``.

    CNY is rounded UP to the fen, so the visitor never pays less than the
    reference-rate equivalent of the credit.
    """
    amount, err = validate_topup_amount(amount_usd)
    if err:
        return None, err
    rate, rate_date = usd_cny_rate()
    if rate is None:
        return None, "WeChat Pay is unavailable right now. Try again in a few minutes."
    amount_cny = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return {
        "amount_usd": str(amount),
        "amount_cny": str(amount_cny),
        "usd_cny_rate": str(rate),
        "rate_date": rate_date,
        "quoted_at": int(time.time()),
    }, None


def create_wechat_payment_intent(
    user_id: str, quote: dict
) -> Tuple[Optional[dict], Optional[str]]:
    """Create and confirm a CNY WeChat Pay PaymentIntent for a stored quote.

    Returns ``(payment_intent_dict, None)`` or ``(None, error)``.
    """
    if not user_id:
        return None, "Missing user_id."
    try:
        amount_cny = Decimal(str(quote["amount_cny"]))
        amount_usd, err = validate_topup_amount(quote["amount_usd"])
        quoted_at = int(quote["quoted_at"])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None, "Quote is invalid. Pick an amount again."
    if err:
        return None, err
    if time.time() - quoted_at > QUOTE_TTL_SECONDS:
        return None, "That price has expired. Pick an amount again for a fresh price."
    stripe = _stripe_client()
    if stripe is None:
        return None, "Stripe is not configured."
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(amount_cny * 100),
            currency="cny",
            payment_method_types=["wechat_pay"],
            payment_method_data={"type": "wechat_pay"},
            payment_method_options={"wechat_pay": {"client": "web"}},
            confirm=True,
            description="Ranomics Tools wallet top-up",
            metadata={
                "user_id": user_id,
                "kind": "topup",
                "channel": CHANNEL,
                "amount_usd": str(amount_usd),
                "amount_cny": str(amount_cny),
                "usd_cny_rate": str(quote.get("usd_cny_rate") or ""),
                "rate_date": str(quote.get("rate_date") or ""),
            },
        )
    except Exception as exc:
        logger.error(
            "WeChat PaymentIntent.create failed user=%s usd=%s cny=%s: %s",
            user_id, amount_usd, amount_cny, exc, exc_info=True,
        )
        return None, "Could not start the WeChat Pay payment. Try again."
    pi = as_dict(intent)
    # Our own record of the quote the visitor accepted, next to the PI id.
    logger.info(
        "WeChat top-up PI created id=%s user=%s usd=%s cny=%s rate=%s rate_date=%s",
        pi.get("id"), user_id, amount_usd, amount_cny,
        quote.get("usd_cny_rate"), quote.get("rate_date"),
    )
    return pi, None


def retrieve_own_payment_intent(pi_id: str, user_id: str) -> Optional[dict]:
    """Fetch a WeChat top-up PI, or ``None`` unless it belongs to ``user_id``."""
    if not pi_id or not user_id:
        return None
    stripe = _stripe_client()
    if stripe is None:
        return None
    try:
        pi = as_dict(stripe.PaymentIntent.retrieve(pi_id))
    except Exception:
        logger.warning("WeChat PaymentIntent.retrieve failed for %s", pi_id, exc_info=True)
        return None
    meta = pi.get("metadata") or {}
    if meta.get("user_id") != user_id or meta.get("channel") != CHANNEL:
        logger.warning("WeChat PI %s does not belong to user %s", pi_id, user_id)
        return None
    return pi


def qr_payload(pi: dict) -> Optional[str]:
    """The string to encode in the QR. Stripe's hosted image URLs are never used."""
    action = pi.get("next_action") or {}
    data = (action.get("wechat_pay_display_qr_code") or {}).get("data")
    return data if isinstance(data, str) and data else None


def qr_svg_data_uri(payload: str) -> str:
    """Render ``payload`` as an inline SVG data URI (no network fetch by the browser)."""
    import segno  # noqa: PLC0415

    return segno.make(payload, error="m").svg_data_uri(scale=6, border=2)


def payment_state(pi: Optional[dict]) -> str:
    """Collapse a PI status to ``pending``, ``succeeded`` or ``failed``."""
    status = (pi or {}).get("status")
    if status == "succeeded":
        return "succeeded"
    if status in ("requires_action", "processing"):
        return "pending"
    # requires_payment_method after a confirmed WeChat attempt means the
    # visitor declined or the 2-hour QR window lapsed; canceled is final.
    return "failed"
