"""Invoice / bank-transfer purchase requests — LEAD CAPTURE, NOT A MONEY PATH.

A buyer who purchases through an institution rarely pays by card: a
purchasing office issues a PO and pays by bank transfer against an invoice.
Until now the only route to a balance was Stripe Checkout
(``blueprints/wallet.py::wallet_checkout`` -> ``billing/checkout.py``), so
that buyer had nowhere to go.

This module captures their request and tells a human about it. That is all
it does. It writes NO ledger row, credits NO wallet, calls NO Stripe API,
and imports nothing from ``billing/``, ``webhooks/`` or the write side of
``shared/wallet``. Crediting a wallet once a transfer clears is a manual
operation performed outside this application; nothing here prepares for it,
reserves anything, or implies an amount has been agreed.

Two side effects per accepted submit, in this order:

1. ``shared.events.log_event("invoice_requested", ...)`` — the analytics
   row, which answers "is this path used, and from where". It carries no
   free text (see ``_event_props``).
2. ``shared.email.send_invoice_request`` — the mail to the support inbox,
   carrying the free-text fields. **This is the notification that a human
   acts on.**

NEITHER SIDE EFFECT IS GUARANTEED, and nothing here makes the request
durable. ``log_event`` is explicitly best-effort: it returns without
inserting when no service client is available (``shared/events.py``,
``service_client_available`` guard) and SHEDS the event when its bounded
in-flight semaphore is full, and the insert runs on a daemon thread, so the
caller cannot learn whether the row landed. ``_post_resend`` returns False
on an unset ``RESEND_API_KEY`` or a non-2xx from Resend. So the event is
written first only because it is the cheaper call and cannot raise — NOT
because it is a fallback for the mail.

So a non-delivery is escalated rather than swallowed: ``_notify_failure``
logs a WARNING and fires the existing ``shared.email.alert_ops_slack``,
whose payload carries every field a human needs to pick the request up by
hand — and which ``_post_slack`` LOGS when ``SLACK_OPS_WEBHOOK_URL`` is
unset, so the details reach the application log on either branch. That is
the honest floor: recoverable from the log, not stored. A request that
cannot be lost at all needs its own table, which this change does not add.

The confirmation the user sees is still NOT conditional on the mail being
delivered: a buyer who filled in a form correctly must not be told it
failed because our outbound mail did, and the form and the confirmation
both name the support address so they have a route either way.
"""

from __future__ import annotations

import logging

from flask import Blueprint, render_template, request, session

from shared.auth import login_required
from shared.credits import load_user_context

logger = logging.getLogger(__name__)

invoice_requests_bp = Blueprint("invoice_requests", __name__)

ROUTE = "/account/wallet/invoice-request"

# Carries the two fields the confirmation prints across the POST -> GET
# redirect. Not a record of anything: the request itself is already in the
# mail and the event by the time this is written.
_SENT_SESSION_KEY = "invoice_request_sent"

# Field length ceilings. These bound what reaches the mail body and the
# ``user_events`` row; they are not business rules and no downstream code
# depends on the exact numbers.
MAX_NAME_CHARS = 120
MAX_INSTITUTION_CHARS = 160
MAX_EMAIL_CHARS = 320
MAX_NOTE_CHARS = 2000

# Amount bounds. The floor duplicates shared/wallet.py::MIN_TOPUP_USD so the
# two purchase routes do not disagree about the smallest sensible order. A
# deliberate duplicate rather than an import: importing it would put
# shared.wallet in this module's import list and defeat the guard in
# tests/test_invoice_request.py::
# test_the_module_imports_nothing_from_the_money_path. The drift that buys is
# pinned by ::test_the_amount_floor_agrees_with_the_card_form. The
# ceiling is a sanity bound on a free-text number, not a limit on what we
# will sell: a larger order is a conversation, which is what this form
# starts anyway.
MIN_AMOUNT_USD = 5
MAX_AMOUNT_USD = 500_000

# ponytail: in-process fixed-window throttle, so the limit is PER GUNICORN
# WORKER, not per deployment -- N workers allow N * limit. Accepted: the
# key is an authenticated user_id, the cost of a breach is extra mail in one
# inbox, and the alternative is a shared store this feature does not justify.
# Move to a Supabase-backed counter if invoice volume ever matters.
_THROTTLE_BUCKET = "invoice_request"
_THROTTLE_LIMIT = 3
_THROTTLE_WINDOW_SECONDS = 3600


def _clean(value: object, limit: int) -> str:
    """Strip ``value`` to text and truncate to ``limit`` characters."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _safe_tool(raw: object) -> str:
    """Return ``raw`` only if it is a live tool slug, else "".

    The slug arrives from a query string on GET and a hidden field on POST,
    and both templates feed it to ``url_for("tools.tool_form", tool=...)``.
    Unvalidated, any string builds a link to a 404. Resolved against the
    ADAPTER REGISTRY rather than TOOL_SPECS for the reason recorded at
    blueprints/wallet.py::wallet_topup: TOOL_SPECS still carries the historic
    key "alphafold2" whose adapter registers as "af2", so a TOOL_SPECS check
    would accept a slug that ``tools.tool_form`` then 404s.
    """
    slug = _clean(raw, 64)
    if not slug:
        return ""
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    from tools import base as tool_base  # noqa: PLC0415

    if tool_base.get(slug) is not None and tool_enabled(slug):
        return slug
    return ""


def _parse_amount(raw: str) -> tuple[int | None, str]:
    """Return ``(amount, error)``. ``amount`` is None when ``error`` is set.

    Whole dollars only. A float is rejected rather than rounded: a
    purchasing office that types 1500.50 should see what we will quote, not
    discover later that we read a different number.
    """
    if not raw:
        return None, "Tell us roughly how much you want to buy."
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        return None, "Enter the amount as a whole number of US dollars."
    if amount < MIN_AMOUNT_USD:
        return None, f"The smallest order we can invoice is ${MIN_AMOUNT_USD}."
    if amount > MAX_AMOUNT_USD:
        return None, (
            "That is larger than this form handles. Email us and we will "
            "scope it with you directly."
        )
    return amount, ""


def _validate(form) -> tuple[dict, dict]:  # noqa: ANN001
    """Validate the submitted form.

    Returns ``(values, errors)``. ``values`` always holds every field so a
    rejected submit can re-render with the user's own text intact — losing
    five fields to one bad character is how a form gets abandoned.
    """
    from shared.email_domain import EmailClass, classify_email  # noqa: PLC0415

    values = {
        "contact_name": _clean(form.get("contact_name"), MAX_NAME_CHARS),
        "institution": _clean(form.get("institution"), MAX_INSTITUTION_CHARS),
        "contact_email": _clean(form.get("contact_email"), MAX_EMAIL_CHARS),
        "amount_usd": _clean(form.get("amount_usd"), 32),
        "note": _clean(form.get("note"), MAX_NOTE_CHARS),
    }
    errors: dict[str, str] = {}

    if not values["contact_name"]:
        errors["contact_name"] = "Tell us who to address the invoice to."
    if not values["institution"]:
        errors["institution"] = (
            "Tell us the institution or company that will pay."
        )
    if not values["contact_email"]:
        errors["contact_email"] = "We need an address to send the invoice to."
    elif classify_email(values["contact_email"]) is EmailClass.INVALID:
        errors["contact_email"] = "That does not look like an email address."

    amount, amount_error = _parse_amount(values["amount_usd"])
    if amount_error:
        errors["amount_usd"] = amount_error
    else:
        values["amount_usd"] = str(amount)

    return values, errors


def _event_props(values: dict, *, where: str, tool: str) -> dict:
    """Props for the ``invoice_requested`` event.

    Deliberately NO name, institution, address or note: those are personal
    data and a free-text field, and they belong in the mail to the support
    inbox, not in an append-only analytics table. The keys here are the ones
    that answer "is this path used, and from where".

    ``where`` and ``tool`` take their KEY NAMES from ``credit_exhausted``
    (shared/wallet_guard.py, templates/wallet/_partials.html). The ``where``
    VALUES are this event's own -- "topup" / "gate" here against "submit" /
    "form" there -- so a join on ``where`` matches nothing; join these two
    events on ``user_id``, and on ``tool`` where both carry it.
    """
    props: dict[str, object] = {"where": where}
    try:
        props["amount_usd"] = int(values.get("amount_usd") or 0)
    except (TypeError, ValueError):  # pragma: no cover (validated upstream)
        props["amount_usd"] = 0
    if tool:
        props["tool"] = tool
    return props


def _notify_failure(*, user_id: str, values: dict) -> None:
    """Escalate an undelivered request so it can be recovered by hand.

    Called only when the mail did not go. Reuses the existing ops alerter
    rather than adding a channel: ``shared.email._post_slack`` logs its
    payload and returns False when ``SLACK_OPS_WEBHOOK_URL`` is unset, so
    the fields below reach the application log whether or not Slack is
    configured. Never raises -- this is the last step of a request the
    buyer has already been told succeeded.
    """
    # Every field is in THIS line, not only in the alert below. _post_slack
    # logs its payload when the webhook is unset, but on a Slack POST failure
    # it logs only its log_tag -- and the fault that fails the Resend POST is
    # the same fault that fails the Slack POST, so the two failures correlate.
    # Leaning on the alert for the contact address and the note would lose
    # exactly those on the one branch where both channels are down.
    logger.warning(
        "invoice_request NOT DELIVERED, follow up manually: "
        "user=%s institution=%r contact=%r <%s> amount_usd=%s note=%r",
        user_id,
        values.get("institution"),
        values.get("contact_name"),
        values.get("contact_email"),
        values.get("amount_usd"),
        (values.get("note") or "")[:200],
    )
    try:
        from shared.email import alert_ops_slack  # noqa: PLC0415

        alert_ops_slack(
            event="invoice_request_mail_failed",
            user_id=user_id,
            institution=values.get("institution") or "",
            contact_name=values.get("contact_name") or "",
            contact_email=values.get("contact_email") or "",
            amount_usd=values.get("amount_usd") or "",
            # Bounded: the note is up to MAX_NOTE_CHARS and this is a chat
            # message, not a record.
            note=(values.get("note") or "")[:200],
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "invoice_request: ops alert failed for user %s", user_id,
            exc_info=True,
        )


def _came_from() -> str:
    """Which entry point sent the buyer here, or "".

    Read from ``request.values``, so it is picked up from the query string
    on the GET and from the hidden field on the POST. Only the literal
    "pricing" is accepted; anything else is "". The handler passes it back
    into the template on every render, including a refusal, which is what
    makes it survive the correct-and-resubmit round trip -- reading
    ``request.args`` in the template did not, because the form posts to a
    bare path.
    """
    # ponytail: a SIGNED-OUT visitor who clicks the /pricing anchor is NOT
    # counted as "pricing". shared/auth.py::_login_next builds the post-login
    # `next` from request.path, which drops the query string, so the marker
    # does not survive the login bounce and that buyer records "topup". So
    # where="pricing" undercounts the pricing page by however many arrive
    # signed out -- it is a floor, not a total. Fixing it properly means
    # making _login_next preserve the query string, which changes the
    # post-login destination for every gated route in the app; that is a
    # wider change than an analytics label is worth here.
    value = _clean(request.values.get("from"), 16)
    return "pricing" if value == "pricing" else ""


def _render(
    *,
    values: dict,
    errors: dict,
    sent: bool = False,
    tool: str = "",
    came_from: str = "",
):
    """Render the form or the confirmation.

    ``support_email`` is NOT passed: app.py's context processor injects it
    into every template already, and the mail recipient is resolved inside
    ``shared.email.send_invoice_request`` from the same env var, so the two
    cannot drift.
    """
    return render_template(
        "wallet/invoice_request.html",
        values=values,
        errors=errors,
        invoice_sent=sent,
        return_tool=tool or None,
        came_from=came_from or None,
        min_amount_usd=MIN_AMOUNT_USD,
        action_url=ROUTE,
    )


@invoice_requests_bp.route(ROUTE, methods=["GET"])
@login_required
def invoice_request_form():
    """Render the form, or the confirmation after a successful POST.

    ``?sent=1`` is the GET half of the post/redirect/get below. Rendering
    the confirmation straight from the POST meant a browser refresh
    resubmitted the whole thing -- another mail, another event row -- so the
    POST redirects here instead and this route reads the two fields the
    confirmation names out of the session.
    """
    ctx = load_user_context()
    if ctx is None:
        from flask import redirect, url_for  # noqa: PLC0415

        return redirect(url_for("auth.login"))

    tool = _safe_tool(request.args.get("tool"))
    came_from = _came_from()
    confirmed = session.get(_SENT_SESSION_KEY)
    if request.args.get("sent") and isinstance(confirmed, dict):
        return _render(
            values={
                "institution": _clean(
                    confirmed.get("institution"), MAX_INSTITUTION_CHARS
                ),
                "amount_usd": _clean(confirmed.get("amount_usd"), 32),
                "contact_name": "", "contact_email": "", "note": "",
            },
            errors={},
            sent=True,
            tool=tool,
            came_from=came_from,
        )

    values = {
        "contact_name": "",
        "institution": "",
        "contact_email": ctx.email or "",
        "amount_usd": "",
        "note": "",
    }
    return _render(values=values, errors={}, tool=tool, came_from=came_from)


@invoice_requests_bp.route(ROUTE, methods=["POST"])
@login_required
def invoice_request_submit():
    """Validate, record the event, mail the support inbox, confirm.

    Writes no ledger row and touches no balance. A refusal re-renders the
    form with the user's text; an acceptance redirects to the confirmation
    whether or not the mail went out.
    """
    from urllib.parse import quote  # noqa: PLC0415

    from flask import redirect, url_for  # noqa: PLC0415

    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    tool = _safe_tool(request.form.get("tool"))
    came_from = _came_from()
    values, errors = _validate(request.form)
    if errors:
        return _render(
            values=values, errors=errors, tool=tool, came_from=came_from,
        ), 400

    from scout.ratelimit import hit  # noqa: PLC0415

    allowed, _retry_after = hit(
        _THROTTLE_BUCKET,
        ctx.user_id,
        limit=_THROTTLE_LIMIT,
        window_seconds=_THROTTLE_WINDOW_SECONDS,
    )
    if not allowed:
        # Plain text, escaped by Jinja like every other error. The form
        # branch of the template prints the support address under the
        # panel, which is what "below" refers to.
        errors = {
            "_form": (
                "We already have your request and someone is looking at it. "
                "If you need to change it, email us at the address below."
            )
        }
        return _render(
            values=values, errors=errors, tool=tool, came_from=came_from,
        ), 429

    # Which entry point this came from. "gate" needs a valid tool slug, and
    # only the tool-submit and refold gates pass one. "pricing" is the
    # anchor in templates/pricing.html, which passes ?from=pricing for
    # exactly this reason -- without it both it and the top-up page's block
    # would count as "topup" and no figure could tell them apart.
    if tool:
        where = "gate"
    elif came_from == "pricing":
        where = "pricing"
    else:
        where = "topup"

    # Analytics first, because it is the call that cannot raise -- NOT
    # because it is a fallback for the mail below. It is best-effort and may
    # be shed; see the module docstring.
    from shared.events import log_event  # noqa: PLC0415

    log_event(
        event_type="invoice_requested",
        user_id=ctx.user_id,
        session_id=session.get("anon_session_id"),
        path=request.path,
        props=_event_props(values, where=where, tool=tool),
    )

    from shared.email import send_invoice_request  # noqa: PLC0415

    try:
        delivered = send_invoice_request(
            account_email=ctx.email or "",
            user_id=ctx.user_id,
            **values,
        )
    except Exception:  # noqa: BLE001
        # Never surface an outbound-mail failure as a form error: the buyer
        # did nothing wrong, and the page names the support address.
        logger.warning(
            "invoice_request: mail raised for user %s", ctx.user_id,
            exc_info=True,
        )
        delivered = False
    if not delivered:
        _notify_failure(user_id=ctx.user_id, values=values)

    # Post/redirect/get: the confirmation is a GET, so a refresh re-reads it
    # instead of sending a second mail and writing a second event row. Only
    # the two fields the confirmation prints are carried.
    session[_SENT_SESSION_KEY] = {
        "institution": values["institution"],
        "amount_usd": values["amount_usd"],
    }
    target = ROUTE + "?sent=1"
    if tool:
        target += "&tool=" + quote(tool)
    if came_from:
        target += "&from=" + quote(came_from)
    return redirect(target)
