"""Stripe Checkout Session creation for wallet top ups.

Replaces the prior Workspace SKU Checkout flow (2026-05-11 era, archived
2026-05-14 with the wallet pivot). The wallet model has a single Stripe
product (``Tools-Hub Wallet Top-Up`` is the dashboard product name) with
no fixed Price. Each Checkout Session passes an inline ``price_data``
with the user supplied amount, so the same product covers $20 and
$5,000 top ups alike.

Lifecycle of one top up
=======================
::

    [user clicks "Top up $X" on /account or pricing]
            |
            v
    create_topup_session(user_id, email, amount_usd)
            |
            v
    Stripe Checkout Session created with inline price_data
            |
            v
    [user pays on Stripe hosted page]
            |
            v
    success_url -> /account/topup-complete?session_id=cs_...
            |                 (Agent E renders the confirmation page)
            v
    checkout.session.completed webhook fires
            |                 (Agent D credits the wallet via
            |                  shared.wallet.top_up_wallet, which
            |                  is idempotent on the Stripe event id)
            v
    wallet balance reflects the top up

The webhook is the only authority that credits the wallet. The
success_url page is read only; it polls the wallet row until the
ledger entry appears.

Required environment variables
------------------------------

::

    STRIPE_SECRET_KEY                Stripe API key (sk_test_ or sk_live_)
    STRIPE_WALLET_TOPUP_PRODUCT_ID   prod_... id for the wallet product
    PUBLIC_BASE_URL                  base URL for success_url and cancel_url
                                     (e.g. https://tools.ranomics.com).
                                     Canonical name across the codebase.
                                     Aliases honoured: APP_BASE_URL,
                                     APP_URL. Falls back to localhost in
                                     dev.

Optional environment variables
------------------------------

::

    WALLET_MIN_TOPUP_USD             defaults to ``20.00`` per
                                     ``shared.wallet.MIN_TOPUP_USD``
    WALLET_MAX_TOPUP_USD             defaults to ``5000.00``. Caps the
                                     largest single Checkout amount.
                                     Auto reload monthly caps are
                                     enforced separately in the wallet
                                     module.

The wallet pivot exclusively uses ``automatic_tax.enabled=true``. The
Stripe dashboard product must have ``Tax behaviour`` configured before
this code goes live in production. See plan ``Pass 3`` for the
dashboard click path.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# Cents per dollar. Stripe amounts are integers in the smallest currency
# unit; we only support USD.
_CENTS_PER_DOLLAR = Decimal("100")


class OffSessionChargeError(RuntimeError):
    """An auto-reload off-session PaymentIntent could not be charged.

    Subclasses ``RuntimeError`` so existing ``except RuntimeError``
    callers keep working. Carries two extra fields the auto-reload
    logic in :mod:`shared.wallet` reads:

    * ``retryable`` -- ``True`` when the failure is transient or on the
      Ranomics side (Stripe outage, missing API key). The caller leaves
      auto-reload enabled and tries again on the next settle. ``False``
      when the failure is the customer's saved card or Stripe customer
      (declined, expired, "no such customer"); the caller disables
      auto-reload and emails the user.
    * ``reason`` -- a key into ``shared.email._AUTO_RELOAD_REASON_LABELS``
      so the failure email can name the cause.
    """

    def __init__(self, message: str, *, retryable: bool, reason: str) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason


# Stripe exception class names that are transient or Ranomics-side
# rather than a problem with the customer's saved card. Auto-reload
# stays enabled for these; everything else disables it.
_RETRYABLE_STRIPE_ERRORS = frozenset({
    "APIConnectionError",
    "RateLimitError",
    "AuthenticationError",
    "APIError",
})

# Stripe CardError decline codes that map to a known email reason label.
_KNOWN_DECLINE_REASONS = frozenset({
    "card_declined",
    "expired_card",
    "insufficient_funds",
})


def _classify_off_session_error(exc: Exception) -> Tuple[bool, str]:
    """Return ``(retryable, reason)`` for an off-session charge failure.

    Classifies by exception class name so it does not depend on a
    particular ``stripe`` SDK version's module layout.
    """
    name = type(exc).__name__
    if name in _RETRYABLE_STRIPE_ERRORS:
        return True, "card_declined"
    if name == "CardError":
        code = str(getattr(exc, "code", "") or "")
        reason = code if code in _KNOWN_DECLINE_REASONS else "card_declined"
        return False, reason
    if name == "InvalidRequestError":
        # Bad customer or payment-method id: the saved card is unusable.
        return False, "no_payment_method"
    # Unknown failure: treat as permanent so auto-reload stops retrying
    # silently. A false positive just means the user re-enables it.
    return False, "card_declined"


def _default_min_topup_usd() -> Decimal:
    """Return the configured floor for a single top up, in USD.

    Falls back to ``shared.wallet.MIN_TOPUP_USD`` (currently $20) if the
    env var is unset or malformed.
    """
    raw = os.environ.get("WALLET_MIN_TOPUP_USD", "").strip()
    if raw:
        try:
            return Decimal(raw)
        except (InvalidOperation, ValueError):
            logger.warning(
                "WALLET_MIN_TOPUP_USD=%r is not a valid decimal; "
                "falling back to module default.",
                raw,
            )
    try:
        from shared.wallet import MIN_TOPUP_USD  # noqa: PLC0415

        return MIN_TOPUP_USD
    except Exception:
        return Decimal("20.00")


def _default_max_topup_usd() -> Decimal:
    """Return the configured ceiling for a single top up, in USD.

    Defaults to $5,000 when the env var is unset. The monthly auto reload
    cap (``DEFAULT_AUTO_RELOAD_MONTHLY_CAP_USD``) is a separate guard and
    is enforced inside the wallet module, not here.
    """
    raw = os.environ.get("WALLET_MAX_TOPUP_USD", "").strip()
    if raw:
        try:
            return Decimal(raw)
        except (InvalidOperation, ValueError):
            logger.warning(
                "WALLET_MAX_TOPUP_USD=%r is not a valid decimal; "
                "falling back to module default.",
                raw,
            )
    return Decimal("5000.00")


def _base_url() -> str:
    """Return the URL prefix for Checkout success and cancel redirects.

    Reads in priority order:

    1. ``PUBLIC_BASE_URL`` (canonical name across the codebase, used by
       ``shared/email.py``, ``app.py``, and ``cron/daily_digest.py``).
    2. ``APP_BASE_URL`` (legacy alias from the initial wallet pivot
       draft).
    3. ``APP_URL`` (older Railway env name).
    4. ``http://localhost:5055`` (local dev fall back; never reaches
       production).
    """
    candidate = (
        os.environ.get("PUBLIC_BASE_URL", "").strip()
        or os.environ.get("APP_BASE_URL", "").strip()
        or os.environ.get("APP_URL", "").strip()
        or "http://localhost:5055"
    )
    return candidate.rstrip("/")


def _product_id() -> Optional[str]:
    """Return the wallet top up Stripe product id from env.

    Accepts ``STRIPE_WALLET_TOPUP_PRODUCT_ID`` (plan name) or
    ``STRIPE_TOPUP_PRODUCT_ID`` (shorthand seen in some handoff drafts).
    """
    for key in ("STRIPE_WALLET_TOPUP_PRODUCT_ID", "STRIPE_TOPUP_PRODUCT_ID"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def _stripe_client():
    """Return the configured ``stripe`` module, or ``None`` if missing.

    Lazy import keeps the module importable in environments without the
    Stripe SDK installed (e.g. CI containers that only run unit tests).
    """
    api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not api_key:
        logger.error(
            "STRIPE_SECRET_KEY is not set; cannot create a Checkout Session."
        )
        return None
    try:
        import stripe  # noqa: PLC0415
    except ImportError:
        logger.error("stripe package is not installed.")
        return None
    stripe.api_key = api_key
    return stripe


def _coerce_amount(amount_usd) -> Optional[Decimal]:
    """Normalise ``amount_usd`` to ``Decimal`` rounded to two places.

    Returns ``None`` if the value is not parseable.
    """
    if amount_usd is None:
        return None
    if isinstance(amount_usd, Decimal):
        value = amount_usd
    else:
        try:
            value = Decimal(str(amount_usd))
        except (InvalidOperation, ValueError):
            return None
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def create_topup_session(
    user_id: str,
    user_email: str,
    amount_usd,
    *,
    save_payment_method: bool = False,
    product_id: Optional[str] = None,
    base_url: Optional[str] = None,
    min_topup_usd: Optional[Decimal] = None,
    max_topup_usd: Optional[Decimal] = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """Create a Stripe Checkout Session for a wallet top up.

    Parameters
    ----------
    user_id
        Supabase ``auth.users.id`` for the signed in user. Flows through
        Session metadata so the webhook handler can credit the right
        wallet.
    user_email
        Used to prefill the Checkout Session.
    amount_usd
        The dollar amount the user wants to add. Accepted as a
        ``Decimal``, ``int``, ``float``, or numeric string. Bounded by
        ``min_topup_usd`` and ``max_topup_usd``.
    save_payment_method
        When ``True``, ask Stripe to retain the card so off session auto
        reload PaymentIntents can charge it later. The webhook stores
        ``stripe_payment_method_id`` on the wallet row when the Session
        completes.
    product_id, base_url, min_topup_usd, max_topup_usd
        Optional overrides. Mainly used by tests; production paths read
        these from the environment.

    Returns
    -------
    ``(session_dict, None)`` on success or ``(None, error_message)`` on
    failure. ``session_dict`` always contains ``id`` and ``url``.
    """
    if not user_id or not isinstance(user_id, str):
        return None, "Missing user_id."
    if not user_email or not isinstance(user_email, str):
        return None, "Missing user_email."

    amount = _coerce_amount(amount_usd)
    if amount is None or amount <= 0:
        return None, "Top up amount must be a positive number."

    floor = min_topup_usd if min_topup_usd is not None else _default_min_topup_usd()
    ceiling = (
        max_topup_usd if max_topup_usd is not None else _default_max_topup_usd()
    )
    if amount < floor:
        return None, (
            f"Top up amount {amount} USD is below the minimum of "
            f"{floor} USD."
        )
    if amount > ceiling:
        return None, (
            f"Top up amount {amount} USD exceeds the maximum of "
            f"{ceiling} USD per Checkout. Contact support for larger "
            "transfers."
        )

    resolved_product = (product_id or _product_id() or "").strip()
    if not resolved_product:
        return None, (
            "Wallet top up product is not configured. "
            "Set STRIPE_WALLET_TOPUP_PRODUCT_ID in the environment."
        )

    stripe = _stripe_client()
    if stripe is None:
        return None, "Stripe is not configured."

    resolved_base = (base_url or _base_url()).rstrip("/")
    success_url = (
        resolved_base
        + "/account/topup-complete?session_id={CHECKOUT_SESSION_ID}"
    )
    cancel_url = resolved_base + "/account?topup=cancelled"

    unit_amount_cents = int(
        (amount * _CENTS_PER_DOLLAR).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )

    session_args = {
        # One time payment. Auto reload uses PaymentIntent.create off
        # session and never touches this code path.
        "mode": "payment",
        "payment_method_types": ["card"],
        "customer_email": user_email,
        "line_items": [
            {
                "price_data": {
                    "currency": "usd",
                    "product": resolved_product,
                    "unit_amount": unit_amount_cents,
                },
                "quantity": 1,
            }
        ],
        # Stripe Tax must be enabled on every wallet top up Session;
        # the dashboard product is configured with the right tax
        # category (see Stripe dashboard Pass 3 in the plan).
        "automatic_tax": {"enabled": True},
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {
            "user_id": user_id,
            "kind": "topup",
            "amount_usd": str(amount),
        },
    }
    if save_payment_method:
        # Tag the underlying PaymentIntent so the webhook knows to
        # persist stripe_payment_method_id on the wallet row. The
        # ``customer_creation`` flag tells Stripe to attach the PM to
        # a Customer object even though this is a one-off mode=payment
        # Session; without it, off-session PaymentIntents have nothing
        # to charge against and auto-reload fails.
        session_args["customer_creation"] = "always"
        session_args["payment_intent_data"] = {
            "setup_future_usage": "off_session",
            "metadata": {
                "user_id": user_id,
                "kind": "topup",
                "save_pm": "true",
            },
        }
        session_args["metadata"]["save_pm"] = "true"

    try:
        stripe_session = stripe.checkout.Session.create(**session_args)
    except Exception as exc:  # stripe.error.* + network
        logger.error(
            "Stripe Checkout Session.create failed for user=%s amount=%s: %s",
            user_id,
            amount,
            exc,
            exc_info=True,
        )
        return None, "Could not create the Checkout Session. Try again."

    session_id = _attr(stripe_session, "id")
    session_url = _attr(stripe_session, "url")
    if not session_id or not session_url:
        return None, "Stripe did not return a Session id and url."

    logger.info(
        "Created top up Checkout Session: user=%s amount=%s session=%s "
        "save_pm=%s",
        user_id,
        amount,
        session_id,
        save_payment_method,
    )
    return (
        {
            "id": session_id,
            "url": session_url,
            "amount_usd": str(amount),
            "save_payment_method": bool(save_payment_method),
        },
        None,
    )


def retrieve_topup_session(session_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """Fetch a previously created Checkout Session for the success page.

    Used by Agent E's ``/account/topup-complete`` endpoint to display
    the just paid amount while the webhook credits the wallet behind
    the scenes.
    """
    if not session_id or not isinstance(session_id, str):
        return None, "Missing session_id."
    stripe = _stripe_client()
    if stripe is None:
        return None, "Stripe is not configured."
    try:
        stripe_session = stripe.checkout.Session.retrieve(session_id)
    except Exception as exc:
        logger.warning(
            "Stripe Checkout Session.retrieve failed for %s: %s",
            session_id,
            exc,
            exc_info=True,
        )
        return None, "Could not look up the Checkout Session."
    return (
        {
            "id": _attr(stripe_session, "id"),
            "url": _attr(stripe_session, "url"),
            "status": _attr(stripe_session, "status"),
            "payment_status": _attr(stripe_session, "payment_status"),
            "amount_total": _attr(stripe_session, "amount_total"),
            "currency": _attr(stripe_session, "currency"),
            "customer_email": _attr(stripe_session, "customer_email"),
            "metadata": _attr(stripe_session, "metadata") or {},
            "payment_intent": _attr(stripe_session, "payment_intent"),
        },
        None,
    )


def create_off_session_payment_intent(
    *,
    stripe_customer_id: Optional[str],
    payment_method_id: Optional[str],
    amount_usd,
    metadata: Optional[dict] = None,
) -> dict:
    """Create + confirm an off-session PaymentIntent for auto-reload.

    Stripe requires a Customer to be attached to a PaymentMethod for
    off-session reuse, so both ``stripe_customer_id`` and
    ``payment_method_id`` must be set. ``metadata`` is passed through
    to the PI so the ``payment_intent.succeeded`` webhook handler can
    route the credit (looks for ``kind=auto_reload`` and ``user_id``).

    Returns the Stripe PaymentIntent dict on success. Raises:
    * ``ValueError`` for missing inputs.
    * ``OffSessionChargeError`` (a ``RuntimeError`` subclass) for any
      Stripe-level failure. Its ``retryable`` flag tells the caller
      whether to keep auto-reload enabled.
    """
    if not stripe_customer_id:
        raise ValueError(
            "stripe_customer_id is required for off-session "
            "PaymentIntent. The wallet has no saved Stripe customer."
        )
    if not payment_method_id:
        raise ValueError(
            "payment_method_id is required for off-session "
            "PaymentIntent. The wallet has no saved Stripe PM."
        )
    amount = _coerce_amount(amount_usd)
    if amount is None or amount <= 0:
        raise ValueError("amount_usd must be a positive number.")

    stripe = _stripe_client()
    if stripe is None:
        # Missing API key is a Ranomics-side config problem, not the
        # customer's card. Retryable so auto-reload is not disabled.
        raise OffSessionChargeError(
            "Stripe is not configured.", retryable=True, reason="card_declined"
        )

    unit_amount_cents = int(amount * 100)
    try:
        intent = stripe.PaymentIntent.create(
            amount=unit_amount_cents,
            currency="usd",
            customer=stripe_customer_id,
            payment_method=payment_method_id,
            off_session=True,
            confirm=True,
            metadata=metadata or {},
        )
    except Exception as exc:
        # Classify so the caller (shared.wallet.auto_reload_if_needed)
        # can tell a bad saved card from a transient Stripe outage.
        logger.warning(
            "Off-session PI dispatch failed customer=%s pm=%s amount=%s: %s",
            stripe_customer_id, payment_method_id, amount, exc,
            exc_info=True,
        )
        retryable, reason = _classify_off_session_error(exc)
        raise OffSessionChargeError(
            f"Off-session PaymentIntent failed: {exc}",
            retryable=retryable, reason=reason,
        ) from exc

    logger.info(
        "Off-session PI created customer=%s pm=%s amount=%s id=%s",
        stripe_customer_id, payment_method_id, amount,
        _attr(intent, "id"),
    )
    return intent if isinstance(intent, dict) else intent.to_dict_recursive()


def create_portal_session(
    *,
    customer_id: str,
    return_url: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Open a Stripe Billing Portal for a wallet customer.

    The wallet model keeps a ``stripe_customer_id`` on every wallet row
    that has saved a payment method (for auto reload). The portal lets
    those customers remove or replace the card and download receipts
    for past top ups. Wallets without a saved card have no Stripe
    customer record and cannot use the portal.
    """
    if not customer_id:
        return None, "No Stripe customer on file. Top up to create one."
    stripe = _stripe_client()
    if stripe is None:
        return None, "Stripe is not configured."
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
    url = _attr(portal, "url")
    if not url:
        return None, "Stripe did not return a portal URL."
    return url, None


def _attr(obj, name: str):
    """Read ``name`` from a Stripe object or a plain dict.

    Real ``stripe.checkout.Session`` instances support both attribute
    and item access. The fake Sessions returned by the test suite are
    plain dicts, so this helper smooths the seam.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    if hasattr(obj, name):
        return getattr(obj, name)
    try:
        return obj[name]
    except Exception:
        return None
