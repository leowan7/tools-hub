"""Invoice / bank-transfer purchase request: validation, event, mail, and the
guard that it is not a money path.

Covers blueprints/invoice_requests.py, the ``invoice_requested`` event type,
the entry points on the top-up and pricing pages, and the claim in the module
docstring and the PR body that nothing here touches billing.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import render_template

pytestmark = pytest.mark.usefixtures("isolate_supabase")

ROUTE = "/account/wallet/invoice-request"

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "blueprints" / "invoice_requests.py"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture(autouse=True)
def _clear_throttle():
    """The throttle is process-global, so one test's hits leak into the next."""
    from scout import ratelimit
    ratelimit.reset()
    yield
    ratelimit.reset()


class _Ctx:
    user_id = "user-123"
    email = "buyer@lab.example"


GOOD = {
    "institution": "Example University",
    "contact_name": "A Buyer",
    "contact_email": "ap@lab.example",
    "amount_usd": "2000",
    "note": "PO 44812, pay within 30 days.",
}


def _signed_in(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = _Ctx.user_id
        sess["user_email"] = _Ctx.email
    return client


def _post(app, data, *, tool: str = "", came_from: str = "", client=None):
    """POST the form as a signed-in user. Returns (response, log_event, mail).

    Follows the post/redirect/get, so an accepted submit answers 200 with the
    confirmation and a refusal answers its own status with the form.
    """
    payload = dict(data)
    if tool:
        payload["tool"] = tool
    if came_from:
        payload["from"] = came_from
    client = client or _signed_in(app)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ), patch("shared.events.log_event") as log_event, patch(
        "shared.email.send_invoice_request", return_value=True
    ) as mail:
        resp = client.post(ROUTE, data=payload, follow_redirects=True)
    return resp, log_event, mail


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_accepted_request_records_the_event_then_mails_support(app):
    resp, log_event, mail = _post(app, GOOD)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Invoice request received" in body

    kwargs = log_event.call_args.kwargs
    assert kwargs["event_type"] == "invoice_requested"
    assert kwargs["user_id"] == _Ctx.user_id
    assert kwargs["props"] == {"amount_usd": 2000, "where": "topup"}

    sent = mail.call_args.kwargs
    assert sent["institution"] == "Example University"
    assert sent["contact_email"] == "ap@lab.example"
    assert sent["amount_usd"] == "2000"
    assert sent["account_email"] == _Ctx.email
    assert sent["user_id"] == _Ctx.user_id


def test_an_accepted_submit_redirects_so_a_refresh_cannot_resend(app):
    """Post/redirect/get. Rendering the confirmation from the POST meant F5
    sent a second mail and wrote a second event row."""
    client = _signed_in(app)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ), patch("shared.events.log_event") as log_event, patch(
        "shared.email.send_invoice_request", return_value=True
    ) as mail:
        resp = client.post(ROUTE, data=GOOD)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("?sent=1")
        # The refresh a browser would do: re-GET the confirmation.
        again = client.get(resp.headers["Location"])
        again2 = client.get(resp.headers["Location"])
    assert log_event.call_count == 1 and mail.call_count == 1
    for r in (again, again2):
        assert r.status_code == 200
        assert "Invoice request received" in r.get_data(as_text=True)


def test_the_confirmation_names_the_amount_and_institution_after_the_redirect(app):
    resp, _log_event, _mail = _post(app, GOOD)
    body = resp.get_data(as_text=True)
    assert "Example University" in body and "2000" in body


def test_a_bare_sent_flag_with_no_prior_submit_shows_the_form(app):
    """The flag alone must not fabricate a confirmation."""
    client = _signed_in(app)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ):
        body = client.get(ROUTE + "?sent=1").get_data(as_text=True)
    assert "Invoice request received" not in body
    assert 'name="institution"' in body


def test_event_props_carry_no_free_text_or_personal_data(app):
    _resp, log_event, _mail = _post(app, GOOD)
    props = log_event.call_args.kwargs["props"]
    assert set(props) == {"amount_usd", "where"}
    blob = repr(props)
    for secret in ("Example University", "A Buyer", "ap@lab.example", "44812"):
        assert secret not in blob


@pytest.fixture
def boltz2_enabled(monkeypatch):
    """Every tool flag is off in the test environment, and ``_safe_tool``
    requires the flag on (same rule as blueprints/wallet.py::wallet_topup).
    Turn one on so the live-slug branch is reachable."""
    from shared.feature_flags import flag_name
    monkeypatch.setenv(flag_name("boltz2"), "1")


def test_arriving_from_a_tool_gate_is_recorded_as_such(app, boltz2_enabled):
    _resp, log_event, mail = _post(app, GOOD, tool="boltz2")
    props = log_event.call_args.kwargs["props"]
    assert props["where"] == "gate" and props["tool"] == "boltz2"
    # The tool slug is routing context, not something support needs mailed.
    assert "tool" not in mail.call_args.kwargs


@pytest.mark.parametrize("came_from, expected", [
    ("pricing", "pricing"),
    ("", "topup"),
    ("something-else", "topup"),
])
def test_the_three_entry_points_are_told_apart(app, came_from, expected):
    """Without this the pricing anchor and the top-up block both counted as
    "topup" and no figure could separate them."""
    _resp, log_event, _mail = _post(app, GOOD, came_from=came_from)
    assert log_event.call_args.kwargs["props"]["where"] == expected


def test_the_pricing_anchor_passes_the_marker_and_the_form_forwards_it(app):
    with app.test_request_context("/pricing"):
        pricing = render_template("pricing.html", min_topup=5)
    assert ROUTE + "?from=pricing" in pricing

    client = _signed_in(app)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ):
        form = client.get(ROUTE + "?from=pricing").get_data(as_text=True)
    assert '<input type="hidden" name="from" value="pricing">' in form


@pytest.mark.parametrize("bogus", ["not-a-tool", "../admin", "alphafold2"])
def test_an_unknown_tool_slug_is_dropped_not_reflected(app, bogus, monkeypatch):
    """Both templates feed the slug to url_for, so an unknown one would build
    a link to a 404.

    Every case has its FLAG TURNED ON first, so the slug is refused by the
    adapter-registry lookup and not merely by the flag being off -- otherwise
    this test would pass with the registry check deleted. "alphafold2" is the
    case that matters: it IS a TOOL_SPECS key, so a TOOL_SPECS-based check
    would accept it, and its adapter registers as "af2".
    """
    from shared.feature_flags import flag_name
    monkeypatch.setenv(flag_name(bogus), "1")
    resp, log_event, _mail = _post(app, GOOD, tool=bogus)
    assert resp.status_code == 200
    props = log_event.call_args.kwargs["props"]
    assert props["where"] == "topup" and "tool" not in props
    assert bogus not in resp.get_data(as_text=True)


def test_a_live_tool_slug_survives(app, boltz2_enabled):
    resp, log_event, _mail = _post(app, GOOD, tool="boltz2")
    assert log_event.call_args.kwargs["props"]["tool"] == "boltz2"
    assert "/tools/boltz2" in resp.get_data(as_text=True)


def test_confirmation_tells_the_buyer_a_human_replies_and_the_balance_waits(app):
    resp, _log_event, _mail = _post(app, GOOD)
    body = resp.get_data(as_text=True)
    assert "within one business day" in body
    assert "does not change until then" in body
    # The fallback address, so a buyer whose quote never arrives has a route.
    assert "mailto:" in body


# ---------------------------------------------------------------------------
# Validation at the boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field, value", [
    ("institution", ""),
    ("institution", "   "),
    ("contact_name", ""),
    ("contact_email", ""),
    ("contact_email", "not-an-address"),
    ("contact_email", "a@b"),
    ("amount_usd", ""),
    ("amount_usd", "abc"),
    ("amount_usd", "4"),
    ("amount_usd", "1500.50"),
    ("amount_usd", "-100"),
    ("amount_usd", "9999999"),
])
def test_a_bad_field_is_refused_and_nothing_is_recorded(app, field, value):
    bad = dict(GOOD, **{field: value})
    resp, log_event, mail = _post(app, bad)
    assert resp.status_code == 400
    assert not log_event.called
    assert not mail.called


def test_a_refusal_keeps_every_other_field_the_buyer_typed(app):
    bad = dict(GOOD, amount_usd="abc")
    resp, _log_event, _mail = _post(app, bad)
    body = resp.get_data(as_text=True)
    assert 'value="Example University"' in body
    assert 'value="A Buyer"' in body
    assert 'value="ap@lab.example"' in body
    assert "PO 44812" in body


def test_oversized_fields_are_truncated_not_refused(app):
    from blueprints.invoice_requests import (
        MAX_INSTITUTION_CHARS,
        MAX_NOTE_CHARS,
    )
    big = dict(
        GOOD,
        institution="I" * (MAX_INSTITUTION_CHARS + 50),
        note="N" * (MAX_NOTE_CHARS + 500),
    )
    resp, _log_event, mail = _post(app, big)
    assert resp.status_code == 200
    sent = mail.call_args.kwargs
    assert len(sent["institution"]) == MAX_INSTITUTION_CHARS
    assert len(sent["note"]) == MAX_NOTE_CHARS


def test_the_note_is_optional(app):
    resp, _log_event, mail = _post(app, dict(GOOD, note=""))
    assert resp.status_code == 200
    assert mail.call_args.kwargs["note"] == ""


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


def test_the_fourth_request_in_an_hour_is_refused(app):
    from blueprints.invoice_requests import _THROTTLE_LIMIT
    for _ in range(_THROTTLE_LIMIT):
        assert _post(app, GOOD)[0].status_code == 200
    resp, log_event, mail = _post(app, GOOD)
    assert resp.status_code == 429
    assert not log_event.called and not mail.called
    assert "already have your request" in resp.get_data(as_text=True)


def test_a_refused_submit_does_not_spend_a_throttle_slot(app):
    """Validation runs before the throttle, so typos cannot lock a buyer out."""
    from blueprints.invoice_requests import _THROTTLE_LIMIT
    for _ in range(_THROTTLE_LIMIT + 2):
        assert _post(app, dict(GOOD, amount_usd="abc"))[0].status_code == 400
    assert _post(app, GOOD)[0].status_code == 200


# ---------------------------------------------------------------------------
# Mail failure must not lose the lead or scare the buyer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mail_kwargs", [
    {"return_value": False},            # no RESEND_API_KEY, or a non-2xx
    {"side_effect": RuntimeError("resend down")},
])
def test_the_buyer_is_confirmed_even_when_the_mail_fails(app, mail_kwargs):
    client = _signed_in(app)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ), patch("shared.events.log_event") as log_event, patch(
        "shared.email.send_invoice_request", **mail_kwargs
    ), patch("shared.email.alert_ops_slack") as alert:
        resp = client.post(ROUTE, data=GOOD, follow_redirects=True)
    assert resp.status_code == 200
    assert "Invoice request received" in resp.get_data(as_text=True)
    assert log_event.call_args.kwargs["event_type"] == "invoice_requested"
    assert alert.called


@pytest.mark.parametrize("mail_kwargs", [
    {"return_value": False},
    {"side_effect": RuntimeError("resend down")},
])
def test_an_undelivered_request_is_escalated_with_every_field_needed(
    app, mail_kwargs,
):
    """log_event can be SHED (shared/events.py sheds when its bounded
    semaphore is full and returns early with no service client), so the
    event row is not a fallback for the mail. The ops alert is: _post_slack
    logs its payload when SLACK_OPS_WEBHOOK_URL is unset, so these fields
    reach the application log on either branch."""
    client = _signed_in(app)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ), patch("shared.events.log_event"), patch(
        "shared.email.send_invoice_request", **mail_kwargs
    ), patch("shared.email.alert_ops_slack") as alert:
        client.post(ROUTE, data=GOOD, follow_redirects=True)
    kwargs = alert.call_args.kwargs
    assert kwargs["event"] == "invoice_request_mail_failed"
    assert kwargs["user_id"] == _Ctx.user_id
    assert kwargs["institution"] == GOOD["institution"]
    assert kwargs["contact_name"] == GOOD["contact_name"]
    assert kwargs["contact_email"] == GOOD["contact_email"]
    assert kwargs["amount_usd"] == GOOD["amount_usd"]
    assert kwargs["note"] == GOOD["note"]


def test_a_raising_ops_alert_does_not_break_the_response(app):
    client = _signed_in(app)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ), patch("shared.events.log_event"), patch(
        "shared.email.send_invoice_request", return_value=False
    ), patch(
        "shared.email.alert_ops_slack", side_effect=RuntimeError("slack down")
    ):
        resp = client.post(ROUTE, data=GOOD, follow_redirects=True)
    assert resp.status_code == 200
    assert "Invoice request received" in resp.get_data(as_text=True)


def test_a_delivered_request_raises_no_alert(app):
    _resp, _log_event, _mail = _post(app, GOOD)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ), patch("shared.events.log_event"), patch(
        "shared.email.send_invoice_request", return_value=True
    ), patch("shared.email.alert_ops_slack") as alert:
        _signed_in(app).post(ROUTE, data=GOOD, follow_redirects=True)
    assert not alert.called


def test_the_long_note_is_bounded_in_the_alert(app):
    big = dict(GOOD, note="N" * 1500)
    client = _signed_in(app)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ), patch("shared.events.log_event"), patch(
        "shared.email.send_invoice_request", return_value=False
    ), patch("shared.email.alert_ops_slack") as alert:
        client.post(ROUTE, data=big, follow_redirects=True)
    assert len(alert.call_args.kwargs["note"]) == 200


def test_the_event_is_written_before_the_mail_is_attempted(app):
    order: list[str] = []
    client = _signed_in(app)
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ), patch(
        "shared.events.log_event",
        side_effect=lambda **_k: order.append("event"),
    ), patch(
        "shared.email.send_invoice_request",
        side_effect=lambda **_k: order.append("mail") or True,
    ):
        client.post(ROUTE, data=GOOD)
    assert order == ["event", "mail"]


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_signed_out_visitor_is_sent_to_login(app, method):
    resp = getattr(app.test_client(), method)(ROUTE, data=GOOD)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_the_post_is_csrf_enforced(app, monkeypatch):
    monkeypatch.setenv("CSRF_PROTECT", "1")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = _Ctx.user_id
        sess["user_email"] = _Ctx.email
        sess["_csrf_token"] = "the-token"
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ), patch("shared.events.log_event"), patch(
        "shared.email.send_invoice_request", return_value=True
    ):
        assert client.post(ROUTE, data=GOOD).status_code == 403
        ok = client.post(ROUTE, data=dict(GOOD, _csrf="the-token"))
    assert ok.status_code == 302


def test_the_get_prefills_the_account_email_and_nothing_else(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = _Ctx.user_id
        sess["user_email"] = _Ctx.email
    with patch(
        "blueprints.invoice_requests.load_user_context", return_value=_Ctx()
    ):
        body = client.get(ROUTE).get_data(as_text=True)
    assert f'value="{_Ctx.email}"' in body
    assert 'name="institution"' in body and 'name="amount_usd"' in body
    assert "Request an invoice" in body


# ---------------------------------------------------------------------------
# NOT a money path
# ---------------------------------------------------------------------------


_FORBIDDEN_IMPORTS = ("billing", "webhooks", "stripe")

# Names this module must never reach. All but one are Python functions that
# write money (shared/wallet.py, billing/checkout.py). "credit_wallet" is NOT
# a function in this repo -- it is the Supabase RPC name passed as a string at
# shared/wallet.py:538, which is exactly why the string itself is guarded.
# "charge" is a bare substring, so it also catches a hand-rolled charge_*.
_FORBIDDEN_NAMES = (
    "top_up_wallet", "credit_wallet", "reserve_hold", "settle_hold",
    "settle_live_run", "create_topup_session", "charge",
)


def test_the_module_imports_nothing_from_the_money_path():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    assert modules, "parsed no imports; the AST walk is broken, not the module"
    for mod in modules:
        root = mod.split(".", 1)[0]
        assert root not in _FORBIDDEN_IMPORTS, mod
    assert not any(m.startswith("shared.wallet") for m in modules), modules


def test_the_module_names_no_money_writer():
    source = MODULE_PATH.read_text(encoding="utf-8")
    # Strip comments and docstrings: the prose explains what it does NOT do,
    # and that prose naming a writer is the point, not a violation.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    code = ast.unparse(ast.fix_missing_locations(tree))
    for name in _FORBIDDEN_NAMES:
        assert name not in code, f"{name} appears in executable code"


def test_the_money_modules_are_untouched_by_this_branch():
    """The diff must not reach billing, the Stripe webhook or the ledger."""
    import subprocess
    base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if base.returncode != 0:  # pragma: no cover — no origin/main locally
        pytest.skip("origin/main not available")
    changed = subprocess.run(
        ["git", "diff", "--name-only", base.stdout.strip(), "--"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    protected = re.compile(
        r"^(billing/|webhooks/|shared/wallet\.py|shared/wallet_guard\.py"
        r"|blueprints/wallet\.py)"
    )
    assert not [p for p in changed if protected.match(p)], changed


# ---------------------------------------------------------------------------
# Wiring: the event type, and the two entry points
# ---------------------------------------------------------------------------


def test_the_event_type_is_registered():
    from shared.events import USER_EVENT_TYPES
    assert "invoice_requested" in USER_EVENT_TYPES


def test_the_amount_floor_agrees_with_the_card_form():
    """MIN_AMOUNT_USD is a deliberate duplicate of MIN_TOPUP_USD, not an
    import: importing it would put shared.wallet in this blueprint's import
    list and break test_the_module_imports_nothing_from_the_money_path,
    which is worth more than the drift. This test is the thing that stops
    the duplicate drifting."""
    from decimal import Decimal
    from blueprints.invoice_requests import MIN_AMOUNT_USD
    from shared.wallet import MIN_TOPUP_USD
    assert Decimal(MIN_AMOUNT_USD) == MIN_TOPUP_USD


_TOPUP_CONTEXT = {
    # The auto-reload numerics are required: topup.html feeds them to
    # display_ledger_usd, which raises on a non-numeric (an Undefined
    # stringifies to "" and Decimal("") is an InvalidOperation).
    "wallet": {
        "balance_usd": 0.0, "auto_reload_enabled": False,
        "auto_reload_threshold_usd": 10.0, "auto_reload_amount_usd": 50.0,
        "auto_reload_monthly_cap_usd": 1000.0,
    },
    "min_topup_usd": 5, "min_topup": 5, "min_auto_reload": 25,
    "next_url": None, "topup_action_url": "/account/wallet/checkout",
    "topup_error": None,
}


@pytest.mark.parametrize("return_tool, expected", [
    (None, ROUTE + '"'),
    ("boltz2", ROUTE + "?tool=boltz2"),
])
def test_the_top_up_page_offers_the_invoice_route(app, return_tool, expected):
    with app.test_request_context("/account/wallet/topup"):
        html = render_template(
            "wallet/topup.html", return_tool=return_tool, **_TOPUP_CONTEXT
        )
    assert expected in html
    assert "Buying through your institution?" in html


def test_the_gate_render_of_the_same_template_offers_it_too(app):
    """One block covers the standalone page AND the submit gate."""
    with app.test_request_context("/tools/boltz2/submit"):
        html = render_template(
            "wallet/topup.html",
            return_tool="boltz2", gate_reason="wallet_empty",
            tool_slug="boltz2",
            **dict(_TOPUP_CONTEXT, next_url="/tools/boltz2"),
        )
    assert ROUTE in html


def test_the_pricing_page_links_to_it(app):
    with app.test_request_context("/pricing"):
        html = render_template("pricing.html", min_topup=5)
    assert ROUTE in html


# ---------------------------------------------------------------------------
# Copy constraints the hub lead set
# ---------------------------------------------------------------------------


_COPY_FILES = [
    REPO_ROOT / "templates" / "wallet" / "invoice_request.html",
    REPO_ROOT / "templates" / "wallet" / "topup.html",
    REPO_ROOT / "templates" / "pricing.html",
    REPO_ROOT / "templates" / "email" / "send_invoice_request.html",
    MODULE_PATH,
]

# The page must read as a purchasing route, not as a workaround, and must not
# name a country: we do not detect location and must not imply we do.
# "if stripe" is NOT in this list: topup.html's own Jinja contains
# `{% if stripe_session ... %}`, so the substring matches markup rather than
# copy. The words below were each checked against the two pre-existing files
# in _COPY_FILES (topup.html and pricing.html) at base b70c3ff3 and matched
# nothing there.
_BANNED_COPY = (
    "china", "chinese", "firewall", "great wall", "censor",
    "blocked", "unavailable in your", "cannot reach", "workaround",
)


@pytest.mark.parametrize("path", _COPY_FILES, ids=lambda p: p.name)
def test_no_copy_mentions_a_country_or_a_broken_checkout(path):
    text = path.read_text(encoding="utf-8").lower()
    hits = [w for w in _BANNED_COPY if w in text]
    assert not hits, f"{path.name}: {hits}"


def test_the_amount_is_presented_as_a_request_not_a_price():
    page = _COPY_FILES[0].read_text(encoding="utf-8")
    assert "not a commitment" in page
    assert "quote" in page.lower()


def test_the_invoice_page_does_not_render_a_balance():
    """It is not a wallet page. Rendering a balance invites the reading that
    the request has already moved money."""
    page = _COPY_FILES[0].read_text(encoding="utf-8")
    assert "display_balance_usd" not in page
    assert "balance_usd" not in page


# ---------------------------------------------------------------------------
# The email to support
# ---------------------------------------------------------------------------


def test_the_mail_goes_to_the_support_inbox_with_the_buyers_details(
    monkeypatch,
):
    monkeypatch.setenv("SUPPORT_EMAIL", "info@example.test")
    monkeypatch.setenv("RESEND_API_KEY", "")
    from shared.email import send_invoice_request
    with patch("shared.email._post_resend", return_value=True) as post:
        assert send_invoice_request(
            user_id="user-123", account_email="buyer@lab.example", **GOOD
        ) is True
    kwargs = post.call_args.kwargs
    assert kwargs["to_email"] == "info@example.test"
    assert "Example University" in kwargs["subject"]
    assert "2000" in kwargs["subject"]
    html = kwargs["html_body"]
    for field in ("A Buyer", "ap@lab.example", "buyer@lab.example",
                  "Example University", "PO 44812", "user-123"):
        assert field in html


def test_the_mail_says_nothing_has_been_charged(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    from shared.email import send_invoice_request
    with patch("shared.email._post_resend", return_value=True) as post:
        send_invoice_request(
            user_id="u", account_email="b@lab.example", **GOOD
        )
    html = post.call_args.kwargs["html_body"].lower()
    assert "nothing has been charged" in html


def test_an_unset_resend_key_returns_false_without_raising(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert os.environ.get("RESEND_API_KEY") is None
    from shared.email import send_invoice_request
    assert send_invoice_request(
        user_id="u", account_email="b@lab.example", **GOOD
    ) is False
