"""No pre-run figure on the signed-in surfaces (Leo, 2026-10-01).

The sibling of tests/test_no_prerun_numbers.py, which renders only anonymous
pages. A signed-in user with a short balance sees the tool-form top-up gate,
the /campaigns/new and target-launch balance notes, and the top-up page.
None of them may show the estimate, the hold, the shortfall, a first-batch
figure or a suggested top-up. The balance and the $20 minimum may stay.

The /campaigns/new and target-launch notes are written by page scripts from
JSON, so a render shows nothing. Instead every script in templates/ and
static/js is scanned for the JSON money fields it reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_no_prerun_numbers import _MONEY, _allowed_money, _visible

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_BODY = re.compile(
    r"<script(?![^>]*application/ld\+json)[^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
_COMMENT = re.compile(r"/\*.*?\*/|\{#.*?#\}|^\s*//[^\n]*|\s//\s[^\n]*",
                      re.DOTALL | re.MULTILINE)
_USD_FIELD = re.compile(r"\b\w+_usd(?:_display)?\b")
# The balance is not a run figure; amount_usd is the top-up page's own
# custom-amount input.
_ALLOWED_FIELDS = {"balance_usd", "balance_usd_display", "display_balance_usd",
                   "amount_usd"}
# The gate reads the estimate and the shortfall only as flags, never as text.
_PARTIALS_FLAG_USES = ("var deficit = parseFloat(data.deficit_usd || 0)",
                       "deficit > 0",
                       "data.estimate_usd === null",
                       "data.estimate_usd === undefined")
_GONE_LABELS = ("Estimated cost", "Held to start", "You need",
                "wallet-topup-suggested-covers")
_PRESETS = ("$2,500", "$500", "$200", "$50")


def _script(rel: str) -> str:
    src = (_ROOT / rel).read_text(encoding="utf-8")
    bodies = [src] if rel.endswith(".js") else _SCRIPT_BODY.findall(src)
    return _COMMENT.sub(" ", "\n".join(bodies))


def _short_wallet(balance=5.0):
    return {
        "user_id": "u-wallet", "balance_usd": balance,
        "auto_reload_enabled": False, "auto_reload_threshold_usd": 10.0,
        "auto_reload_amount_usd": 50.0, "auto_reload_monthly_cap_usd": 1000.0,
        "wallet_frozen": False, "spent_today_usd": 0.0, "spent_30d_usd": 0.0,
        "stripe_customer_id": None,
    }


def _figures(flask_app, page: str, *extra_allowed: str) -> list[str]:
    for gone in _GONE_LABELS:
        assert gone not in page, gone
    text = _visible(page)
    for allowed in extra_allowed + _allowed_money(flask_app):
        text = text.replace(allowed, "")
    return [text[max(0, m.start() - 60):m.end() + 20]
            for m in _MONEY.finditer(text)]


def test_scripts_read_no_estimate_derived_field():
    rels = sorted(p.relative_to(_ROOT).as_posix() for p in
                  [*(_ROOT / "templates").rglob("*.html"),
                   *(_ROOT / "static/js").glob("*.js")])
    assert "templates/runs/new.html" in rels and "static/js/preflight.js" in rels
    bad = {}
    for rel in rels:
        body = _script(rel)
        if rel.endswith("_partials.html"):
            for use in _PARTIALS_FLAG_USES:
                assert use in body, use
                body = body.replace(use, " ")
            for word in ("deficit", "fmtUp("):
                if re.search(r"\b" + re.escape(word), body):
                    bad.setdefault(rel, []).append(word)
        fields = set(_USD_FIELD.findall(body)) - _ALLOWED_FIELDS
        if fields:
            bad.setdefault(rel, []).extend(sorted(fields))
    assert not bad, bad


def test_tool_form_gate_markup_shows_only_the_balance(all_tools_app):
    flask_app, _slugs = all_tools_app
    with flask_app.test_request_context("/tools/bindcraft"):
        page = flask_app.jinja_env.from_string(
            '{% from "wallet/_partials.html" import wallet_topup_gate, '
            'wallet_estimate_panel %}'
            '{{ wallet_estimate_panel("bindcraft", balance_usd=5) }}'
            '{{ wallet_topup_gate("bindcraft", balance_usd=5) }}'
        ).render()
    assert "data-gate-balance" in page
    page = re.sub(r"(data-gate-balance[^>]*>)[^<]*<", r"\1<", page)
    assert not _figures(flask_app, page, "$5.00")


def test_topup_page_from_the_gate_link_shows_no_run_figure(all_tools_app):
    flask_app, _slugs = all_tools_app
    client = flask_app.test_client()
    with patch("blueprints.wallet.load_user_context") as ctx, patch(
        "blueprints.wallet.get_or_create_wallet",
        return_value=_short_wallet(),
    ):
        ctx.return_value.user_id = "u-wallet"
        with client.session_transaction() as sess:
            sess["user_email"] = "u@example.com"
            sess["user_id"] = "u-wallet"
        resp = client.get("/account/wallet/topup?tool=bindcraft")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert not _figures(flask_app, page, "$5.00", "$10.00", "$1,000.00",
                        *_PRESETS)


@pytest.mark.parametrize("reason, says", [
    ("insufficient_balance", "Your balance does not cover this run."),
    ("per_tool_cap_exceeded", "A top up does not change that."),
    ("self_serve_ceiling_exceeded", "A top up does not change that."),
    ("wallet_frozen", "Your wallet is on hold."),
])
def test_server_gate_render_shows_no_run_figure(all_tools_app, reason, says):
    flask_app, _slugs = all_tools_app
    from shared.wallet_guard import _render_topup_gate  # noqa: PLC0415

    with flask_app.test_request_context("/tools/bindcraft"), patch(
        "shared.wallet_guard.get_or_create_wallet",
        return_value=_short_wallet(),
    ):
        page = _render_topup_gate(tool_slug="bindcraft", reason=reason,
                                  form_snapshot={})
    assert says in page
    assert ("Top up to run your job" in page) == (reason == "insufficient_balance")
    assert not _figures(flask_app, page, "$5.00", "$10.00", "$1,000.00",
                        *_PRESETS)
