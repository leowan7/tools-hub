"""Every surface that prints money, and the direction each one must round.

Four separate QC rounds each found a class of money surface nobody had
enumerated -- costs (round 9), balances (round 10), the required top-up
(round 11), outbound email (round 12) -- because each was formatted at its call
site with ``'%.2f'|format``, which rounds to NEAREST and so flatters the user
about half the time. This module exists so a fifth class cannot be added
silently.

The three directions, and why:

    display_cost_usd     costs, holds, spend, required top-up   -> UP
    display_balance_usd  balances, caps, thresholds             -> DOWN
    display_ledger_usd   historical rows that must reconcile    -> EXACT

A cap rounds DOWN with the balances: a cap shown above its real value overstates
the headroom, which is the same error as overstating a balance.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from shared import compute_campaigns as cc

# Imports app-adjacent modules and renders nothing, but the fixture is cheap and
# the cost of being wrong here is writes against the production project.
pytestmark = pytest.mark.usefixtures("isolate_supabase")


# ---------------------------------------------------------------------------
# The ledger formatter
# ---------------------------------------------------------------------------


def test_the_ledger_shows_the_exact_amount_so_rows_reconcile():
    """The transactions table prints a cost beside a balance in the SAME ROW.

    Costs round UP and balances round DOWN, so applying those two rules here
    would make consecutive rows stop adding up in a fixed direction, on the one
    page whose purpose is that the reader can check the arithmetic. A ledger is
    a record, not a decision surface, so it shows the exact stored value.
    """
    # Clean 2dp values stay clean; only sub-cent precision widens.
    assert cc.display_ledger_usd(Decimal("2.5000")) == "2.50"
    assert cc.display_ledger_usd(Decimal("2.50")) == "2.50"
    assert cc.display_ledger_usd(Decimal("100")) == "100.00"
    assert cc.display_ledger_usd(Decimal("24.4950")) == "24.4950"

    # The property that matters: a row reconciles against its neighbours.
    opening, amount = Decimal("100.0000"), Decimal("24.4950")
    closing = opening - amount
    assert (
        Decimal(cc.display_ledger_usd(opening))
        - Decimal(cc.display_ledger_usd(amount))
        == Decimal(cc.display_ledger_usd(closing))
    ), "the displayed figures do not reconcile, which is the whole point"

    # And it must NOT be either directional rule, or it would inherit the bug.
    assert cc.display_ledger_usd(Decimal("24.4950")) != cc.display_cost_usd(
        Decimal("24.4950")
    )
    assert cc.display_ledger_usd(Decimal("24.4950")) != cc.display_balance_usd(
        Decimal("24.4950")
    )


def test_the_ledger_formatter_fails_fast_on_anything_it_cannot_render_exactly():
    """Fail-FAST, not fail-closed: this page has no submit button, so a raise
    here is a 500, not a blocked spend. Same wording as display_balance_usd.

    quantize does NOT signal on NaN, so is_finite has to be checked first or the
    literal string "NaN" renders into the ledger.
    """
    for bad in ["abc", float("nan"), float("inf"), None, object()]:
        with pytest.raises(Exception):
            cc.display_ledger_usd(bad)


def test_the_ledger_refuses_a_figure_finer_than_it_can_show():
    """The function is documented as EXACT, so it must not quietly round.

    `quantize(Decimal("0.0001"))` uses the context default, ROUND_HALF_EVEN.
    Anything finer than 4dp was therefore rounded to NEAREST inside the one
    helper whose entire purpose is not to. Both current callers read
    numeric(12,4) columns, so this raises rather than silently truncating and
    the precondition is enforced instead of assumed.
    """
    from decimal import Decimal as D

    for bad in (D("1.23455"), D("12.999950"), D("0.000049")):
        with pytest.raises(ValueError, match="4 decimal"):
            cc.display_ledger_usd(bad)
    # 4dp and coarser still render.
    assert cc.display_ledger_usd(D("0.0001")) == "0.0001"
    assert cc.display_ledger_usd(D("24.4950")) == "24.4950"


# ---------------------------------------------------------------------------
# The enumeration guard: no template may format money itself
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every mechanism in this repo that rounds a number AT THE POINT OF DISPLAY.
# The first version of this guard knew only about `'%.2f'|format`, so it was
# blind to `|round(0, 'ceil')` in templates/wallet/topup.html and to every
# `.toFixed()` in the client, and it scanned only *.html so it could not see
# templates/email/*.txt either.
_ROUNDERS = re.compile(
    r"""(?:['"]%\.\d+f['"]\s*\|\s*format\()|(?:\|\s*round\()|(?:\.toFixed\()"""
)

# Substrings that mark an expression as a money value. Kept, but no longer the
# ONLY signal: a literal `$` on the same line now also qualifies. That is what
# the round-13 review found by hand and this guard could not -- two live sites
# whose expressions held no token, one because it reads a `{% set %}` alias
# (`_signup_used`) and one because the column is named `ann.net`.
_MONEY_TOKENS = (
    "balance_usd", "amount_usd", "cost_usd", "budget_usd", "deficit",
    "spent_", "cap_usd", "wallet_usd", "topup", "credit_usd", "quote_total",
    "first_wave", "per_chunk", "usd", "price", "_min", "amount_total",
)

# A JS rounding call is acceptable when an explicit direction is applied on the
# same line or just above it. The client has no Jinja globals, so `fmtUp` /
# `fmtDown` / an inline Math.floor IS the helper there.
_JS_DIRECTED = ("Math.floor(", "Math.ceil(", "_q(", "fmtUp(", "fmtDown(")
_JS_LOOKBACK = 3

_SCAN = (
    ("templates", ("*.html", "*.txt")),
    ("static", ("*.js",)),
)

# Sites that legitimately round a number that is NOT a misstatable money
# figure. Keyed on a substring of the OFFENDING LINE, not on a variable name, so
# an exemption cannot silently spread to a second site that mentions the same
# variable. Dead entries are a hard failure (see `_money_format_sites`), because
# the previous set contained two that could never match and they made the
# auto-reload fields look covered while they were not.
#
# The auto-reload entries are GONE. They rested on `step="1"` in the markup,
# and `blueprints/wallet.py::_coerce` is `Decimal(raw)` with three minimum
# clamps and no integrality check, so a crafted POST stores $5.60 and the
# `'%.0f'` display called it "$6" -- while the same `'%.0f'` in the form's
# `value` attribute rewrote it to 6 the next time the user pressed Save. Those
# figures now go through the display helpers and the inputs render exactly.
_ALLOWED = {
    # Stripe returns integer cents. Dividing by 100 yields at most 2dp, so
    # '%.2f' is exact here, not a rounding decision.
    ("templates/wallet/topup.html", "stripe_session.amount_total"),
    # The minimum top-up, `shared.wallet.MIN_TOPUP_USD`. A module constant, not
    # user data. Pinned by test_the_minimum_topup_is_a_whole_dollar_constant
    # below, which asserts against the CONSTANT rather than against markup --
    # the mistake the auto-reload exemption made.
    ("templates/wallet/topup.html", "|format(_min)"),
    # A deliberate ceil to whole dollars, and the direction is right: this is
    # the prefilled top-up amount, a required top-up, which must never be less
    # than the deficit. The copy two lines down says "We round to whole
    # dollars at checkout", so the user is told.
    ("templates/wallet/topup.html", "_default_amount = (_deficit_raw|float|round(0, 'ceil'))"),
    # A literal zero placeholder in the markup; the inline script overwrites it
    # via fmtUp/fmtDown before it is ever meaningful.
    ("templates/wallet/_partials.html", "'%.2f'|format(0)"),
}


def _money_format_sites(root: Path | None = None):
    """Every line that rounds a money figure at the point of display.

    `root` is a parameter so `test_the_enumeration_guard_can_actually_fail` can
    run THIS function over a fixture directory. The previous version of that
    test re-implemented the regex inline and never called this, which is why the
    guard could be green while two live sites were unrouted.
    """
    root = root or _REPO_ROOT
    assert root.is_dir(), f"scan root does not exist: {root}"
    out, hit_exemptions = [], set()
    for sub, globs in _SCAN:
        base = root / sub
        if not base.is_dir():
            continue
        for pattern in globs:
            for path in sorted(base.rglob(pattern)):
                rel = path.relative_to(root).as_posix()
                lines = path.read_text(encoding="utf-8").splitlines()
                for i, line in enumerate(lines):
                    if not _ROUNDERS.search(line):
                        continue
                    if "$" not in line and not any(
                        tok in line for tok in _MONEY_TOKENS
                    ):
                        continue
                    # `.toFixed(` exists only in JavaScript, so keying off the
                    # call itself is exact. An earlier version sniffed for a
                    # `<script` tag within 40 lines above, which missed the
                    # helpers in templates/wallet/_partials.html and reported
                    # two correctly-directed lines as defects.
                    if ".toFixed(" in line:
                        window = lines[max(0, i - _JS_LOOKBACK):i + 1]
                        if any(m in l for l in window for m in _JS_DIRECTED):
                            continue
                    exempt = next(
                        (e for e in _ALLOWED
                         if e[0] == rel and e[1] in line), None
                    )
                    if exempt is not None:
                        hit_exemptions.add(exempt)
                        continue
                    out.append((f"{rel}:{i + 1}", line.strip()[:110]))
    if root is None or root == _REPO_ROOT:
        dead = _ALLOWED - hit_exemptions
        assert not dead, (
            "these exemptions no longer match any line, so they are covering "
            "nothing and make the guard look wider than it is:\n  "
            + "\n  ".join(f"{p}: {s}" for p, s in sorted(dead))
        )
    return out


def test_no_template_rounds_a_money_figure_itself():
    """The guard that makes a FIFTH undiscovered class impossible.

    Every money figure must go through display_cost_usd, display_balance_usd or
    display_ledger_usd, so its direction is a deliberate choice recorded in one
    place rather than an accident of whoever wrote the line.

    If this fails on a new line, do not add it to _ALLOWED to make it pass. Ask
    which of the three the figure is, and use that helper. _ALLOWED is for
    numbers that are not displayed money at all.
    """
    sites = _money_format_sites()
    assert not sites, (
        "these templates round a money figure themselves, which rounds to "
        "NEAREST and understates about half the time:\n  "
        + "\n  ".join(f"{p}: {e}" for p, e in sites)
    )


def test_the_minimum_topup_is_a_whole_dollar_constant():
    """The justification for the only interesting _ALLOWED entry.

    Four sites render `_min` with '%.0f'. That is safe only while the value
    cannot express cents. Unlike the auto-reload exemption this replaces, the
    claim is about a MODULE CONSTANT rather than an HTML `step` attribute, so it
    is checkable: `step` was a browser hint the server never enforced, and
    asserting it proved nothing about what could be stored.
    """
    from shared.wallet import MIN_TOPUP_USD

    assert MIN_TOPUP_USD == MIN_TOPUP_USD.to_integral_value(), (
        f"MIN_TOPUP_USD is {MIN_TOPUP_USD}, which is no longer a whole dollar, "
        f"so the four '%.0f'|format(_min) sites now round real cents away"
    )


def test_the_auto_reload_route_accepts_cents_so_the_display_must_not_round():
    """The premise the deleted exemption got wrong, asserted the other way up.

    The old exemption said these fields hold whole dollars "by construction",
    citing `step="1"`. The server has no such constraint: `_coerce` is
    `Decimal(raw)` with three MINIMUM clamps and nothing else. This pins that
    fact, so if someone later adds real integrality enforcement they are told
    that the exemption could now be reinstated, and until then nobody can
    reintroduce a '%.0f' on these figures believing it is safe.
    """
    src = Path("blueprints/wallet.py").read_text(encoding="utf-8")
    block = src[src.index("def _coerce("):][:600]
    assert "return Decimal(raw)" in block, (
        "auto-reload coercion changed; recheck whether cents are still storable"
    )
    for rounder in ("to_integral", 'quantize(Decimal("1")', "int(Decimal"):
        assert rounder not in block, (
            f"_coerce now rounds via {rounder!r}. If it truly forces whole "
            f"dollars, the '%.0f' exemption for the auto-reload fields can come "
            f"back; until then those figures must use a display helper."
        )


def test_the_enumeration_guard_can_actually_fail():
    """A guard nobody has seen fail is a guard nobody knows works.

    Round 11's headline finding was a wiring assertion that passed against a
    commented-out listener, and the previous version of THIS test repeated the
    mistake in a subtler way: it re-implemented the regex inline and never
    called `_money_format_sites`, so the token filter, the exemption filter and
    the directory walk -- the three things that actually decide whether the
    guard fires -- were all untested. It was green while two live money sites
    were unrouted.

    This runs the real function over a fixture tree.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "templates" / "wallet").mkdir(parents=True)
        (root / "static" / "js").mkdir(parents=True)
        # 1. the printf form, caught by a money token
        (root / "templates" / "wallet" / "a.html").write_text(
            "${{ '%.2f'|format(wallet.balance_usd|float) }}", encoding="utf-8")
        # 2. no money token at all, caught only by the `$`. This is the exact
        #    shape of both sites the old guard missed.
        (root / "templates" / "wallet" / "b.html").write_text(
            "net ${{ '%.2f'|format((0 - ann.net)|float) }}", encoding="utf-8")
        # 3. a .txt template, a file type the old guard never opened
        (root / "templates" / "wallet" / "c.txt").write_text(
            "${{ '%.2f'|format(balance_usd) }}", encoding="utf-8")
        # 4. undirected client-side rounding
        (root / "static" / "js" / "d.js").write_text(
            'el.textContent = "$" + usd.toFixed(2);', encoding="utf-8")
        # 5. DIRECTED client-side rounding must NOT be reported
        (root / "static" / "js" / "e.js").write_text(
            'el.textContent = "$" + Math.floor(usd * 100) / 100).toFixed(2);',
            encoding="utf-8")
        found = {site.split(":")[0] for site, _ in _money_format_sites(root)}

    assert found == {
        "templates/wallet/a.html", "templates/wallet/b.html",
        "templates/wallet/c.txt", "static/js/d.js",
    }, f"the guard reported {sorted(found)}"


# ---------------------------------------------------------------------------
# Outbound email (A57) -- the first class found outside templates/
# ---------------------------------------------------------------------------


def test_money_directions_round_as_labelled():
    from shared.email import _money

    assert _money("24.4950", "down") == "24.49"
    assert _money("14.7920", "up") == "14.80"
    assert _money("24.4950", "nearest") == "24.50", (
        "the default is still NEAREST; only the directional call sites changed"
    )
    assert _money("24.4950") == "24.50", "the default must remain NEAREST"
    # Whole values keep their bare form, which is why the signup credit email
    # says "$5" and not "$5.00".
    assert _money("5", "down") == "5"


def test_an_unknown_money_direction_raises_rather_than_rounding_to_nearest():
    """A typo must not silently reintroduce the defect.

    On the template side a wrong helper name is an UndefinedError, so the server
    cannot fail quietly. `_money` took a bare string and fell through to NEAREST
    on anything it did not recognise, which made `_money(bal, "DOWN")` look
    deliberate and behave like the bug.
    """
    from shared.email import _money

    for bad in ("DOWN", "floor", "ceil", "exact", ""):
        with pytest.raises(ValueError):
            _money("24.4950", bad)


def test_the_low_balance_email_renders_a_floored_balance():
    """A61. Rendered, not grepped.

    The previous version of this test asserted a source substring, and the
    substring it asserted belonged to `send_reengagement_email` -- a different
    function. It was green while `send_low_balance_email`, the message this test
    is named after and the one whose entire purpose is to make the reader act on
    a balance, still rounded to NEAREST. Rendering the real body is the only
    form of this test that cannot pass for the wrong function.
    """
    from unittest.mock import patch as _patch

    import shared.email as em

    sent = {}

    def _capture(**kw):
        sent.update(kw)
        return True

    with _patch.object(em, "_resolve_user_email", return_value="u@example.com"), \
            _patch.object(em, "_post_resend", side_effect=_capture):
        assert em.send_low_balance_email(
            user_id="u-1", balance_usd=Decimal("24.4950")
        )

    body = sent["html_body"]
    assert "24.49" in body, f"the balance was not floored; body said: {body[:400]}"
    assert "24.50" not in body, "the low-balance email still overstates the balance"


def test_no_email_money_figure_is_left_on_the_nearest_default():
    """The enumeration guard for outbound email.

    `templates/` has one; `shared/email.py` did not, and that is how twelve of
    thirteen call sites stayed on NEAREST while the module docstring instructed
    the opposite. Costs quoted low in an overrun email are the same defect as a
    cost quoted low on a form.
    """
    src = Path("shared/email.py").read_text(encoding="utf-8")
    undirected = []
    for i, line in enumerate(src.splitlines(), 1):
        if "_money(" not in line or line.lstrip().startswith(("#", "*")):
            continue
        if "def _money(" in line or "_MONEY_DIRECTIONS" in line or "``" in line:
            continue
        if any(d in line for d in ('"up"', '"down"', '"nearest"')):
            continue
        undirected.append((i, line.strip()[:100]))
    # Every outbound money figure now names its direction explicitly, including
    # the signup credit -- it is a fixed advertised amount, so it passes
    # "nearest" rather than relying on the implicit default. Nothing is allowed
    # to sit on the default: an empty list is the whole expectation.
    assert [text for _, text in undirected] == [], (
        "these email money figures have no explicit direction, so they round to "
        "NEAREST and understate about half the time:\n  "
        + "\n  ".join(f"{i}: {t}" for i, t in undirected)
    )
