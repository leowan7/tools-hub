"""Route tests for the /campaigns/* compute-campaign endpoints.

Verifies the templates render and the endpoints wire to the module without
live Supabase/Modal (auth + wallet + persistence are mocked).
"""

from __future__ import annotations

from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.money_display_guard import assert_template_prints_no_raw_money

# These exercise real routes through a real create_app(), and app.py calls
# load_dotenv() at import, so without this fixture every render and every
# get_service_client() in here reaches the PRODUCTION Supabase project.
pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _ctx(user_id="u-1"):
    return SimpleNamespace(user_id=user_id, tier="free", balance=100, email="u@example.com")


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"


def test_runs_new_renders(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.get("/campaigns/new")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "New campaign" in body
    assert "rfdiffusion" in body  # supported tool option
    assert 'id="rp-submit"' in body  # cost-confirm submit


def test_runs_list_renders(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), patch(
        "shared.compute_campaigns.list_campaigns_for_user", return_value=[]
    ):
        resp = client.get("/campaigns")
    assert resp.status_code == 200
    assert "Campaigns" in resp.get_data(as_text=True)


def test_estimate_ok(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), patch(
        "shared.wallet.get_or_create_wallet",
        return_value={"balance_usd": "1000", "wallet_frozen": False},
    ), patch("shared.compute_campaigns.get_service_client", return_value=None):
        resp = client.get("/api/campaigns/estimate?tool=rfdiffusion&requested_designs=24")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["total_subjobs"] == 2
    assert float(data["budget_usd"]) > 0
    # Fund-and-drain: the start gate is the first wave, surfaced to the UI.
    assert float(data["first_wave_usd"]) > 0
    assert data["affordable"] is True


def test_estimate_over_cap(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.get("/api/campaigns/estimate?tool=rfdiffusion&requested_designs=999999")
    data = resp.get_json()
    assert data["ok"] is False
    assert "sub-jobs" in data["error"]


def test_estimate_unsupported_tool(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.get("/api/campaigns/estimate?tool=mpnn&requested_designs=10")
    data = resp.get_json()
    assert data["ok"] is False


# ---------------------------------------------------------------------------
# The consent figures on /campaigns/new (A48)
#
# This page shows a cost preview directly above a checkbox reading "I understand
# I pay only for compute that runs" and a Start button. It used to convert the
# wire's exact 4dp values to 2dp in JS, rounding to NEAREST, so a $2.6219 hold
# printed as "$2.62". The rounding now happens in Decimal on the server: a cost
# rounds UP, a balance rounds DOWN.
#
# rfdiffusion at 24 designs is the cohort throughout: 2 sub-jobs, and both the
# budget (4.0202) and the first wave (5.2438) round differently to nearest than
# to ceiling, so the direction is observable rather than assumed.
# ---------------------------------------------------------------------------

_COST_KEYS = ("budget_usd", "first_wave_usd", "per_chunk_usd")


def _estimate_json(client, query, balance="1000"):
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), patch(
        "shared.wallet.get_or_create_wallet",
        return_value={"balance_usd": balance, "wallet_frozen": False},
    ), patch("shared.compute_campaigns.get_service_client", return_value=None):
        resp = client.get("/api/campaigns/estimate?" + query)
    assert resp.status_code == 200
    return resp.get_json()


def test_the_estimate_ships_display_strings_that_never_understate_a_cost(client):
    """A displayed cost below the real one is not a ceiling."""
    from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal

    _login(client)
    data = _estimate_json(client, "tool=rfdiffusion&requested_designs=24")
    assert data["ok"] is True

    # Precondition, asserted rather than assumed: if ceiling and nearest agree
    # on a figure, every assertion about it passes with the direction reversed
    # and pins nothing. Only two of the three keys can observe it -- the
    # per-chunk price is 1.7479, which is 1.75 either way -- so that is stated
    # here rather than quietly relied on.
    for key in ("budget_usd", "first_wave_usd"):
        exact = Decimal(data[key])
        assert (
            exact.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
            != exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        ), f"{key}={exact} cannot observe the rounding direction"

    for key in _COST_KEYS:
        exact, shown = Decimal(data[key]), data[f"{key}_display"]
        assert isinstance(shown, str)
        assert Decimal(shown).as_tuple().exponent == -2, f"{key} not 2dp: {shown}"
        assert Decimal(shown) >= exact, f"{key} understated: {shown} < {exact}"
        assert Decimal(shown) == exact.quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )
    # The figure the checkbox refers to, spelled out so the regression is named.
    assert data["first_wave_usd"] == "5.2438"
    assert data["first_wave_usd_display"] == "5.25"


def test_the_estimate_never_overstates_the_balance(client):
    """A balance rounded UP claims money the wallet does not have."""
    from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

    _login(client)
    balance = "573.6756"
    exact = Decimal(balance)
    # Chosen so FLOOR and NEAREST differ. 573.6736 would not: both give 573.67,
    # so the test would only have caught a switch to ceiling and not the switch
    # to nearest this change exists to prevent.
    assert (
        exact.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        != exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    ), f"{balance} floors the same way it rounds to nearest; pick another"

    data = _estimate_json(
        client, "tool=rfdiffusion&requested_designs=24", balance=balance,
    )
    assert data["balance_usd"] == balance
    assert data["balance_usd_display"] == "573.67"
    assert Decimal(data["balance_usd_display"]) <= exact


def test_the_campaign_page_does_no_money_rounding_of_its_own(client):
    """The drift guard. See tests/money_display_guard.py for what it cannot do."""
    assert_template_prints_no_raw_money("templates/runs/new.html")


def test_the_campaign_page_puts_each_figure_in_its_own_slot():
    """A right figure under the wrong label is a wrong figure.

    Crossing budget into the "Held to start" slot left 292 tests green. The
    budget is always the larger number, so that swap overstates the amount
    about to be held, directly above the line consenting to it. Unlike the
    provenance question this is statically decidable -- the assignment names
    both the destination and the source on one line -- it was just never
    checked.
    """
    from tests.money_display_guard import assert_money_slots_are_not_crossed
    assert_money_slots_are_not_crossed("templates/runs/new.html", {
        "rp-budget": "budget_usd_display",
        "rp-perchunk": "per_chunk_usd_display",
        "rp-firstwave": "first_wave_usd_display",
        "rp-balance": "balance_usd_display",
    })


def test_a_failed_estimate_disarms_the_campaign_submit_button(client):
    """The reason the display helpers are allowed to raise.

    They fail closed only if a 500 from the estimate leaves the page unable to
    submit. ``latest`` holds the LAST SUCCESSFUL estimate, so a handler that
    only sets a warning leaves the button armed beside figures that priced a
    different design count, and the earlier version of this handler did exactly
    that.

    Static, like its siblings: no test executes this script (A47). It pins the
    three statements AND their order, not the behaviour they produce. Order
    matters and is not decoration: `syncSubmit()` reads `latest`, so running it
    before the reset re-arms the button from the last successful estimate and
    leaves it armed. A reviewer confirmed that reordering these three lines
    leaves the button armed with the suite green, so presence alone was not
    enough.
    """
    import re

    src = open("templates/runs/new.html", encoding="utf-8").read()
    catch = re.search(r"\.catch\(function \(\) \{(.*?)\n      \}\);", src, re.S)
    assert catch, "the estimate fetch has no .catch handler in the expected shape"
    body = catch.group(1)
    positions = {}
    for label, needle, why in (
        ("reset", "latest = { ok: false", "a failed estimate leaves latest armed"),
        ("clear", "clearFigures()", "a failed estimate leaves stale money on screen"),
        ("sync", "syncSubmit()", "nothing re-evaluates the submit button"),
    ):
        assert needle in body, why
        positions[label] = body.index(needle)
    assert positions["reset"] < positions["sync"], (
        "syncSubmit() runs before latest is reset, so it re-arms the button "
        "from the previous successful estimate"
    )
    assert positions["clear"] < positions["sync"], (
        "the figures are cleared after the button is re-evaluated"
    )


def test_post_missing_pdb_rerenders_with_error(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.post("/campaigns", data={
            "tool": "rfdiffusion",
            "requested_designs": "24",
            "target_chain": "A",
            "hotspot_residues": "417,453",
            "binder_length_min": "55",
            "binder_length_max": "65",
        })
    assert resp.status_code == 400
    assert "Upload a target PDB" in resp.get_data(as_text=True)


def test_post_over_cap_rerenders_with_error(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.post("/campaigns", data={
            "tool": "rfdiffusion",
            "requested_designs": "999999",
        })
    assert resp.status_code == 400
    assert "sub-jobs" in resp.get_data(as_text=True)


def test_a_bindcraft_campaign_gets_past_preset_validation(client):
    """Regression: every bindcraft campaign used to 400 "Pick a preset."

    The create form's two `name="preset"` selects are both disabled unless the
    tool is proteina or iggm, so nothing posts the field for the five pilot
    tools, and the route built its validation dict straight from request.form.
    Four of the five adapters default the preset internally; bindcraft is the
    one that does not, so it alone rejected. This posts exactly what the real
    form posts for bindcraft -- note the deliberate absence of `preset` -- and
    asserts the run gets far enough to need a PDB, which is the NEXT check
    after validation.

    Do not "improve" this by adding preset to the payload: sending it is what
    the browser cannot do, and the test would then pass against the bug.
    """
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.post("/campaigns", data={
            "tool": "bindcraft",
            "requested_designs": "16",
            "target_chain": "A",
            "hotspot_residues": "417,453",
            "binder_length_min": "50",
            "binder_length_max": "100",
        })
    body = resp.get_data(as_text=True)
    assert "Pick a preset" not in body
    assert "Upload a target PDB" in body


def test_the_single_tool_refusal_passes_its_own_display_string():
    """A SOURCE guard, for the same reason as the multi-tool one.

    ``compute_campaign_create``'s refusal passes ``required_display=`` so the
    sentence quotes the same string the panel prints. Today that is an
    EQUIVALENT MUTANT: ``pre.required_usd`` is ``gate_usd`` is ``first_wave``,
    so the default derives the identical string and deleting the kwarg leaves
    every behavioural test green. A reviewer confirmed it -- 247 passed with the
    kwarg removed -- so no assertion on the rendered sentence can pin this.

    That is exactly the argument round 8 accepted for ``nothing_charged`` on the
    other money route, and then did not apply here, leaving a comment claiming
    the two are "the same string by construction". They are the same string by
    coincidence. The construction is this kwarg.

    What it protects: the day ``api_runs_estimate``'s figures become a row sum,
    as the multi-tool estimate's already are, the default starts rounding the
    exact total while the panel sums displayed rows, and
    ``sum(ceil(row)) >= ceil(sum(row))`` puts the sentence a cent BELOW the
    panel. That is the round-8 defect, and a "this kwarg is just the default"
    tidy-up re-opens it with CI green.

    Proves the call's shape, not its value. Same limit as its sibling.
    """
    import ast

    src = open("blueprints/campaigns.py", encoding="utf-8").read()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "compute_campaign_create"
    )
    calls = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "preauth_message"
    ]
    assert calls, "compute_campaign_create no longer calls preauth_message"
    for call in calls:
        args = {kw.arg for kw in call.keywords}
        assert "required_display" in args, (
            "preauth_message is called without required_display=. The refusal "
            "sentence must quote the string the panel prints, not re-derive "
            "one from the exact figure."
        )


def _campaign_row(budget="4.0202"):
    from decimal import Decimal
    return SimpleNamespace(
        id="c-1", user_id="u-1", tool="rfdiffusion", status="running",
        requested_designs=24, total_subjobs=2, chunk_size=12,
        budget_usd=Decimal(budget), preset="pilot", launch_group_id=None,
        target_id=None, created_at="2026-07-30T00:00:00Z",
        concurrency_target=16, est_cost_per_chunk=Decimal("1.7479"),
    )


@pytest.mark.parametrize("path,patches", [
    ("/campaigns/c-1", "detail"),
    ("/campaigns", "list"),
])
def test_the_stored_budget_renders_with_the_servers_rounding(client, path, patches):
    """The budget a user already authorized must read the same on every screen.

    Both these templates formatted it themselves with ``'%.2f'|format``, which
    is round-half-even over a float and rounds to NEAREST. Once the estimate
    panels moved to Decimal ceiling, the SAME campaign printed two different
    budgets: rfdiffusion's 4.0202 is $4.03 on the panel that took consent and
    was $4.02 here. 5 of the 7 campaign tools diverge (rfdiffusion, bindcraft,
    rfantibody, proteina, iggm).

    Neither template is in the launch diff, which is the point: the diff moved
    the panel and these two were never asked whether they agreed. A display
    rule's blast radius is every surface that prints the figure.

    Red if either template formats money itself again, or if the
    ``display_cost_usd`` Jinja global stops being registered.
    """
    from shared.compute_campaigns import display_cost_usd
    row = _campaign_row()
    expected = display_cost_usd(row.budget_usd)
    assert expected == "4.03" and "%.2f" % float(row.budget_usd) == "4.02", (
        "fixture no longer distinguishes ceiling from nearest"
    )

    _login(client)
    patches_to_apply = [
        patch("blueprints.campaigns.load_user_context", return_value=_ctx())
    ]
    if patches == "detail":
        patches_to_apply += [
            patch("shared.compute_campaigns.get_campaign", return_value=row),
            patch("shared.compute_campaigns.get_progress_counts",
                  return_value={"succeeded": 0, "failed": 0, "timeout": 0,
                                "running": 0, "queued": 0, "total": 2}),
            patch("shared.compute_campaigns.aggregate_campaign_candidates",
                  return_value={"candidates": [], "columns": [], "total": 0,
                                "capped": False}),
        ]
    else:
        patches_to_apply.append(
            patch("shared.compute_campaigns.list_campaigns_for_user",
                  return_value=[row])
        )
    with ExitStack() as stack:
        for p in patches_to_apply:
            stack.enter_context(p)
        resp = client.get(path)

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f"${expected}" in body, f"{path} does not print ${expected}"
    assert "$4.02" not in body, (
        f"{path} still prints the NEAREST-rounded budget, which understates "
        f"what the user authorized"
    )


def _debounced_body(path="templates/runs/new.html"):
    """``debounced()``'s source, brace-matched, with // comments stripped.

    Stripping matters: the body explains why the invalidation precedes the
    refetch, and naming ``setTimeout`` in that sentence made the ordering
    assertion below match the PROSE rather than the code. It failed on a
    correct implementation, which is the direction that gets a real guard
    deleted for being flaky.
    """
    src = open(path, encoding="utf-8").read()
    start = src.index("function debounced()")
    i = src.index("{", start)

    # A single pass that knows about strings AND comments, because handling
    # either one alone is wrong in a way that passes for the wrong reason:
    #
    # - Counting braces naively treats a `{` inside a string as real and runs
    #   past the closing brace. Measured: the body widened from 310 to 578
    #   characters and swallowed the listener block below the function, so
    #   every ordering assertion here would match text from OUTSIDE it.
    # - Stripping `//` comments before scanning would delete a `//` inside a
    #   string, e.g. a URL.
    # - Tracking strings before comments treats the apostrophe in `// the
    #   user's balance` as opening a string. Both these files contain exactly
    #   that, and it made the extraction fail outright.
    #
    # So comments are skipped as comments and strings as strings, in one pass,
    # and the kept text is returned with comment spans dropped.
    depth, j, quote, kept = 0, i, None, []
    while j < len(src):
        two = src[j:j + 2]
        if quote is None and two == "//":
            j = src.find("\n", j)
            if j == -1:
                break
            continue
        if quote is None and two == "/*":
            j = src.find("*/", j) + 2
            continue
        ch = src[j]
        if quote:
            kept.append(ch)
            if ch == "\\":
                kept.append(src[j + 1:j + 2])
                j += 2
                continue
            if ch == quote:
                quote = None
            j += 1
            continue
        kept.append(ch)
        if ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(kept)
        j += 1
    raise AssertionError("debounced() is not brace-balanced")


# Both estimate-backed pages take consent above a price, so both need the same
# invalidation. The first version of these tests covered only the campaign page
# -- the page A51 was filed against -- and left the multi-tool launch page,
# whose checkbox reads "the amount above will be held against my wallet
# balance", pinned by nothing. Deleting its untick was green.
_CONSENT_PAGES = [
    ("templates/runs/new.html", "confirm.checked = false", "clearFigures()"),
    ("templates/targets/launch.html", "confirmBox.checked = false",
     "clearTotals()"),
]

# The design-count input is the control A51 was filed against: tick at 24, type
# 5000, submit inside the debounce. If it is not wired to debounced(), every
# assertion about debounced()'s body is describing dead code.
#
# Each entry also names the SELECTOR the listener is attached through, and that
# selector must resolve against the page's own markup. A bare substring test was
# not a wiring test: repointing either page's selector at a class or id that
# does not exist left it green, and so did replacing the entire listener block
# with a COMMENT containing the same text.
_PRICE_INPUT_WIRING = [
    ("templates/runs/new.html",
     "count.addEventListener('input', debounced)",
     "getElementById('requested_designs')", 'id="requested_designs"'),
    # The class literal is the full attribute as written, not a bare token:
    # the element carries `class="field-input tool-designs"`, and asserting
    # `class="tool-designs"` failed against correct markup.
    ("templates/targets/launch.html",
     "el.addEventListener('input', debounced)",
     "querySelectorAll('.tool-designs')", 'class="field-input tool-designs"'),
]


@pytest.mark.parametrize("path,wiring,selector,markup", _PRICE_INPUT_WIRING)
def test_the_design_count_input_is_wired_to_the_repricing_handler(
    path, wiring, selector, markup
):
    """A correct handler nothing calls is not a fix.

    Both A51 tests read ``debounced()``'s body. Neither asserted anything
    invokes it, and a grep of all of ``tests/`` for ``addEventListener``
    returned nothing at all. Deleting the design-count listener from either page
    left 100 tests green while making the entire A51 remediation unreachable --
    the exact input the defect was filed against.

    This is round 10's finding one level up. That round caught a test asserting
    a statement was PRESENT without asserting WHERE; this is a test asserting a
    function body is correct without asserting anything CALLS it.

    Static, same limit as its siblings (A47): it proves the listener is
    registered in LIVE SOURCE against a selector the page's own markup
    satisfies. It does not prove the browser fires it.

    The first version was a bare substring test over the raw file, which is not
    a wiring test at all: it survived repointing the selector at a class that
    does not exist, and survived the entire listener block being replaced by a
    comment containing the same characters.
    """
    import re as _re

    raw = open(path, encoding="utf-8").read()
    # Comments are not code. Strip them before asserting anything is "in" the
    # source, or a commented-out listener satisfies the assertion.
    live = _re.sub(r"//[^\n]*", "", _re.sub(r"/\*.*?\*/", "", raw, flags=_re.S))

    assert wiring in live, (
        f"{path}: the design-count input is no longer wired to debounced(), so "
        f"changing the count reprices nothing and consent survives the change"
    )
    assert selector in live, (
        f"{path}: the listener is registered but {selector} is gone, so it "
        f"attaches to nothing"
    )
    assert markup in raw, (
        f"{path}: {selector} resolves against no element in this page's own "
        f"markup ({markup} absent), so the listener attaches to nothing"
    )


@pytest.mark.parametrize("path,untick,clear", _CONSENT_PAGES)
def test_repricing_invalidates_consent_before_it_refetches(path, untick, clear):
    """A51. Consent is per price, so a new price must take a new tick.

    ``debounced()`` used to only schedule a refetch. Tick the box at 24 designs
    with $4.03 on screen, type 5000, submit inside the 250 ms window, and the
    POST prices 5000 against consent recorded for 24. The server re-gates on the
    wallet, so this is a consent defect and not unbounded spend -- but the
    ceiling is the whole balance, not the figure the user agreed to.

    **Consent has no server-side component.** Neither checkbox carries a
    ``name``, neither is POSTed, and no route reads one. The submit button's
    ``disabled`` attribute is the entire mechanism and ``syncSubmit()`` is the
    only thing that sets it, so ``syncSubmit()`` must run AFTER the resets it
    reads. Hoisting it to the top of ``debounced()`` restores the whole defect
    while leaving the box visibly unticked and the figures blank -- it reads
    MORE correct than the bug does. The first version of this test asserted
    ``syncSubmit()`` was merely present and that mutation stayed green.

    STATIC, and that is a real limit: no test executes this script (A47), so
    this proves the statements are present and ordered, not that the browser
    does the right thing.
    """
    body = _debounced_body(path)
    for stmt in ("reqSeq += 1", untick, clear, "syncSubmit()"):
        assert stmt in body, f"{path}: debounced() no longer does: {stmt}"

    for stmt, why in (
        (untick, "consent is unticked after the refetch is scheduled, which "
                 "leaves the stale-consent window exactly as wide as before"),
        (clear, "figures are cleared after the refetch is scheduled, so the "
                "previous price stays rendered through the repricing window"),
    ):
        assert body.index(stmt) < body.index("setTimeout"), f"{path}: {why}"

    # The assertion the first version omitted.
    sync = body.index("syncSubmit()")
    assert body.index(untick) < sync, (
        f"{path}: syncSubmit() runs before the consent box is unticked, so it "
        f"computes `disabled` from the PREVIOUS tick and nothing re-evaluates "
        f"the button for the whole debounce"
    )
    assert body.index("latest = { ok: false") < sync, (
        f"{path}: syncSubmit() runs before `latest` is reset, so it re-arms "
        f"the button against the previous estimate"
    )


@pytest.mark.parametrize("path", [p for p, _, _ in _CONSENT_PAGES])
def test_a_superseded_estimate_cannot_overwrite_a_newer_one(path):
    """Two fetches in flight, and the slow one must not win.

    The debounce only collapses edits inside 250 ms. Two edits further apart put
    two requests in flight, and without a sequence check whichever RESOLVES last
    renders -- so a slow response for 24 designs can overwrite a fast one for
    5000 and re-arm the button against the wrong price.

    Parametrized over both consent pages. The launch page had this guard first
    and was still covered by nothing; dropping its ``.then`` check was green.

    Red if either handler on either page drops its guard. Static, same limit.
    """
    src = open(path, encoding="utf-8").read()
    assert src.count("if (mySeq !== reqSeq) { return; }") == 2, (
        "expected the sequence guard in BOTH the .then and the .catch handler"
    )
    assert "var mySeq = reqSeq;" in src, "fetchEstimate does not capture a sequence"
    then_at = src.index(".then(function (d) {")
    catch_at = src.index(".catch(function () {")
    for name, at in (("then", then_at), ("catch", catch_at)):
        guard = src.index("if (mySeq !== reqSeq) { return; }", at)
        assert guard - at < 120, (
            f"the {name} handler does work before checking its sequence"
        )


# ---------------------------------------------------------------------------
# A58 / A59: POST /campaigns was the only money-spending POST with no
# @idempotent(), and it discarded fund_campaign's boolean.
#
# The store fake and CSRF token are imported rather than re-declared. Two copies
# of a fake drift, and the weaker copy is the one that decides whether a
# regression ships -- this suite has already lost three rounds to fakes that
# omitted a method the production path calls.
# ---------------------------------------------------------------------------

from tests.test_target_multi_launch_routes import (  # noqa: E402
    _CSRF, _IdemStore,
)


@pytest.fixture
def csrf_campaign_app(monkeypatch):
    """The real app with PRODUCTION CSRF enforcement, not the suite default.

    This is not optional detail. `_enforce_csrf` reads request.form, which
    consumes the stream, so under CSRF_PROTECT=1 the idempotency key must fall
    back to a canonical encoding of the form. Under the suite default of 0 the
    raw body is still there and these tests would exercise a path production
    never takes.
    """
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("CSRF_PROTECT", "1")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


_PDB = (
    "ATOM      1  N   MET A  42      11.104  13.207  10.000  1.00 20.00           N\n"
    "ATOM      2  CA  MET A  42      12.560  13.207  10.000  1.00 20.00           C\n"
    "ATOM      3  N   GLY A  88      14.100  15.300  11.000  1.00 20.00           N\n"
    "END\n"
)


def _campaign_form(**kw):
    """A form the route will actually accept.

    The upload has to be PRESENT or the route stops at "Upload a target PDB
    file." and never reaches the fund step these tests are about, so the
    idempotency assertion would be measuring two identical 400s.

    Its CONTENT is never inspected. `resolve_target_upload` is mocked, and
    `tools/rfdiffusion` never reads its `files` argument; the chain and hotspot
    checks run only on the stored-target branch, which no test here takes. The
    one property the bytes carry is their LENGTH, which `_form_fingerprint`
    folds into the idempotency key. An earlier version of this docstring claimed
    the residues made it pass the chain and hotspot checks; they do not run.
    """
    import io
    data = {
        "tool": "rfdiffusion",
        "requested_designs": "24",
        "target_chain": "A",
        "hotspot_residues": "42,88",
        "binder_length_min": "55",
        "binder_length_max": "65",
        "_csrf": _CSRF,
        "target_pdb": (io.BytesIO(_PDB.encode()), "target.pdb"),
    }
    data.update(kw)
    return data


class _CampaignRecorder:
    def __init__(self):
        self.created, self.funded, self.driven = [], [], []
        # Every (campaign_id, kwargs) the route passed to `get_campaign`. The
        # owner scoping on that read is the whole tenancy boundary, and a patch
        # with a bare `return_value` ignores its arguments, so dropping
        # `user_id=` would leave the suite green.
        self.reads = []

    def create(self, **kw):
        row = SimpleNamespace(id=f"c-{len(self.created)}", tool=kw.get("tool"),
                              status="draft")
        self.created.append(row)
        return row

    def fund(self, campaign_id):
        self.funded.append(campaign_id)
        return True

    def drive(self, campaign_id):
        self.driven.append(campaign_id)


_UNSET = object()


def _post_campaign(client, rec, form=None, fund=None, get_campaign=_UNSET,
                   drive=None):
    from contextlib import ExitStack

    def _read_campaign(campaign_id, **kw):
        rec.reads.append((campaign_id, kw))
        return get_campaign

    patches = [
        patch("blueprints.campaigns.load_user_context", return_value=_ctx()),
        patch("shared.compute_campaigns.create_campaign", side_effect=rec.create),
        patch("shared.compute_campaigns.fund_campaign",
              side_effect=(fund if fund is not None else rec.fund)),
        patch("shared.compute_campaigns.drive_campaign_async",
              side_effect=(drive if drive is not None else rec.drive)),
        # `shared.compute_campaigns`, NOT `shared.target_launch`. The route does
        # `from shared import compute_campaigns as cc` and calls
        # `cc.campaign_preauth`; `shared/target_launch.py` merely imports the
        # same name into its own namespace, so patching the importer's copy
        # leaves the REAL gate running. It approved anyway (wallet mocked to
        # $1000, KYC off, spend-today 0 under isolate_supabase), so these tests
        # passed by coincidence and would turn red on any preauth env change.
        patch("shared.compute_campaigns.campaign_preauth",
              return_value=SimpleNamespace(ok=True, reason=None,
                                           balance_usd=Decimal("1000"),
                                           required_usd=Decimal("1"),
                                           needs_verification=False)),
        patch("shared.wallet.get_or_create_wallet",
              return_value={"balance_usd": "1000", "wallet_frozen": False}),
        # Storage is mocked at the two points the route touches it: the
        # validating parse of the upload, and the staging write. Neither is what
        # these tests are about, and leaving either real would reach Supabase.
        patch("blueprints.campaigns.resolve_target_upload",
              return_value=(SimpleNamespace(filename="target.pdb",
                                            data=_PDB.encode(),
                                            content_type="chemical/x-pdb"), None)),
        patch("blueprints.campaigns.upload_input",
              return_value="u-1/campaign/target.pdb"),
    ]
    # `_UNSET`, not `None`. `None` is a MEANINGFUL injected value here (the row
    # was unreadable), and an `is not None` guard silently turned that case into
    # "apply no patch", leaving the real `get_campaign` to return None only
    # because isolate_supabase blanked the credentials. The test then asserted
    # against a fixture side effect rather than an injected value.
    if get_campaign is not _UNSET:
        patches.append(patch("shared.compute_campaigns.get_campaign",
                             side_effect=_read_campaign))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return client.post("/campaigns", data=form or _campaign_form())


def test_csrf_is_actually_enforced_in_the_campaign_fixture(csrf_campaign_app):
    """Precondition. If enforcement silently switched off, the tests below would
    pass while exercising the CSRF_PROTECT=0 path the rest of the file uses, and
    the blind spot this fixture exists for would be back."""
    client = csrf_campaign_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
        sess["_csrf_token"] = _CSRF
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.post("/campaigns", data=_campaign_form(_csrf="wrong"))
    assert resp.status_code == 403


def test_a_double_submit_creates_one_campaign(csrf_campaign_app):
    """A58. This route had no @idempotent() at all.

    Every other money-spending POST carries it. Without it a double-click funds
    TWO campaigns against ONE consent, gating the same first wave twice against
    the same balance -- measured at created=2, funded=2 before the fix. There is
    no client-side guard either: runs/new.html registers no submit handler, the
    CSRF token is session-scoped and reusable, and the POST takes seconds.

    Red if the decorator is removed.
    """
    store = _IdemStore()
    client = csrf_campaign_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
        sess["_csrf_token"] = _CSRF
    rec = _CampaignRecorder()
    with patch("shared.idempotency.get_service_client", return_value=store):
        first = _post_campaign(client, rec)
        second = _post_campaign(client, rec)

    assert first.status_code == 302
    assert second.status_code == 302
    assert second.headers["Location"] == first.headers["Location"], (
        "the replay must reproduce the redirect, not a 302 to nowhere"
    )
    assert len(rec.created) == 1, (
        f"two clicks created {len(rec.created)} campaigns; each one funds a "
        f"separate first wave against the same consent"
    )
    assert len(rec.funded) == 1, (
        f"two clicks funded {len(rec.funded)} times against one consent"
    )


def test_two_different_campaigns_in_the_ttl_both_run(csrf_campaign_app):
    """The other half of A58, and the half that makes the decorator SAFE.

    The test above posts two IDENTICAL forms, so it cannot tell "the key is a
    function of the form" from "the key is a function of nothing". Under
    CSRF_PROTECT=1 -- the production default -- `_enforce_csrf` has already
    consumed the raw body by the time `_compute_key` runs, so
    `request.get_data()` is b"" and the form fingerprint is the ONLY
    discriminator. Delete it and every submission to this route inside the 60s
    TTL collapses onto sha256(user_id + path): a user's second, genuinely
    different campaign is answered with a replay of the first one's redirect and
    never runs.

    Red if `_form_fingerprint` stops contributing to the key.
    """
    store = _IdemStore()
    client = csrf_campaign_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
        sess["_csrf_token"] = _CSRF
    rec = _CampaignRecorder()
    with patch("shared.idempotency.get_service_client", return_value=store):
        first = _post_campaign(client, rec)
        second = _post_campaign(client, rec,
                                form=_campaign_form(requested_designs="12"))

    assert first.status_code == 302
    assert second.status_code == 302
    assert len(rec.created) == 2, (
        f"a differently sized campaign was deduped against the first; only "
        f"{len(rec.created)} ran. The user's second campaign vanished."
    )
    assert second.headers["Location"] != first.headers["Location"], (
        "the second campaign was answered with the first one's redirect"
    )


def test_a_confirmed_draft_after_a_failed_fund_is_reported_as_not_started(
    csrf_campaign_app,
):
    """A59. The return value used to be discarded entirely.

    A failed fund redirected as success, and the row sat at `draft` forever:
    `cron/tick_campaigns.py` excludes draft from _ACTIVE_STATES, so nothing ever
    picks it up. That is the round-5 inversion in the other direction -- round 5
    told a charged user nothing was charged; this told an uncharged user their
    campaign had started.

    Confirmed draft is the ONE case where "nothing was charged" is true: no hold
    was placed, drive_campaign early-returns on draft, _campaign_spend_today
    skips it. That matters because this route is now @idempotent and idempotency
    releases its claim on any 4xx, so the retry this invites has to be safe --
    and the store is injected here so that release is OBSERVED rather than
    asserted in prose. Without it the real client is None under
    isolate_supabase, `_claim_key` returns "open", and the decorator is a no-op.

    Red if the route stops reading the boolean, stops confirming it, or stops
    releasing the claim on the 4xx.
    """
    store = _IdemStore()
    client = csrf_campaign_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
        sess["_csrf_token"] = _CSRF
    rec = _CampaignRecorder()
    draft_row = SimpleNamespace(id="c-0", tool="rfdiffusion", status="draft")
    with patch("shared.idempotency.get_service_client", return_value=store):
        resp = _post_campaign(
            client, rec, fund=lambda _cid: False, get_campaign=draft_row,
        )
    assert resp.status_code == 400, (
        "a campaign that never left draft was reported as started"
    )
    assert "nothing was charged" in resp.get_data(as_text=True)
    assert rec.driven == [], (
        "a draft campaign was driven; drive_campaign early-returns on draft, "
        "so this is a wasted thread and a misleading success"
    )
    assert rec.reads == [("c-0", {"user_id": "u-1"})], (
        f"the confirming read was {rec.reads!r}; it is the whole tenancy "
        f"boundary, so it must be owner-scoped and must happen exactly once"
    )
    assert store.rows == {}, (
        "the claim survived a 4xx, so the corrected resubmission this error "
        "invites is answered with the stale rejection for the rest of the TTL"
    )


def test_a_fund_that_reported_false_but_moved_the_row_is_started(
    csrf_campaign_app,
):
    """The third branch, and the reason the read is three-valued at all.

    `fund_campaign` returns False when the row was not in `draft` -- which
    includes the case where a concurrent driver already advanced it, or where
    the UPDATE committed in Postgres and the response timed out. The row IS
    funded and billing. Reporting "nothing was charged" here is the round-5
    inversion and invites a duplicate launch of real money.

    Neither of the other two fund tests covers this: one injects a draft row,
    the other injects None. Red if the branch collapses to "False means draft".
    """
    client = csrf_campaign_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
        sess["_csrf_token"] = _CSRF
    rec = _CampaignRecorder()
    funded_row = SimpleNamespace(id="c-0", tool="rfdiffusion", status="funded")
    resp = _post_campaign(
        client, rec, fund=lambda _cid: False, get_campaign=funded_row,
    )
    assert resp.status_code == 302, (
        "a campaign whose row is already funded was reported as not started; "
        "that user is being billed and has just been invited to launch again"
    )
    assert rec.driven == ["c-0"], "a funded campaign was not driven"


def test_an_unconfirmable_fund_is_treated_as_started(csrf_campaign_app):
    """False is three-valued, and the expensive error is the other one.

    `fund_campaign` cannot raise -- `_cas_transition` swallows everything and
    returns False -- so False means EITHER "the row was not in draft" OR "the
    UPDATE raised and I cannot tell". A write that commits in Postgres while the
    response times out lands in the second bucket.

    Telling that user "nothing was charged" invites a re-launch of a campaign
    that is funded and billing. So an unreadable row falls through to started,
    which is the same policy target_launch_submit applies. Keep them identical.
    """
    client = csrf_campaign_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
        sess["_csrf_token"] = _CSRF
    rec = _CampaignRecorder()
    resp = _post_campaign(
        client, rec, fund=lambda _cid: False, get_campaign=None,
    )
    assert resp.status_code == 302, (
        "an unreadable row was reported as not charged; that invites a "
        "duplicate launch of a campaign that may be billing"
    )
    assert rec.driven == ["c-0"], "the campaign was not driven"
    assert rec.reads == [("c-0", {"user_id": "u-1"})], (
        f"the confirming read was {rec.reads!r}. An empty list means None was "
        f"never INJECTED: the helper skipped its patch and the real "
        f"get_campaign returned None only because isolate_supabase blanked the "
        f"credentials, so this test would be asserting on a fixture side effect"
    )


def test_a_failed_drive_spawn_does_not_double_fund_the_campaign(
    csrf_campaign_app,
):
    """A60. The decorator added by A58 made this line's failure expensive.

    `drive_campaign_async` is fallible: the `try` inside it wraps the drive, not
    `threading.Thread(...).start()`, which raises RuntimeError when the process
    cannot start another thread. If that escapes the view, `@idempotent`
    RELEASES the claim and re-raises, Flask returns 500, and the retry the error
    invites re-runs the whole handler -- creating and funding a SECOND campaign
    against one consent. That is verbatim the A58 failure the decorator exists
    to stop, reached through the fix's own error path, and thread exhaustion is
    process-wide so it hits every concurrent submitter at once.

    Red if the try/except around the spawn is removed.
    """
    store = _IdemStore()
    client = csrf_campaign_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
        sess["_csrf_token"] = _CSRF
    rec = _CampaignRecorder()

    def _boom(_cid):
        raise RuntimeError("can't start new thread")

    with patch("shared.idempotency.get_service_client", return_value=store):
        first = _post_campaign(client, rec, drive=_boom)
        # The retry a 500 would invite. Must replay, not re-run.
        second = _post_campaign(client, rec, drive=_boom)

    assert first.status_code == 302, (
        f"the drive spawn failed with {first.status_code}; the campaign is "
        f"funded and the cron tick will drive it, so this is not an error"
    )
    assert second.status_code == 302
    assert len(rec.created) == 1, (
        f"the retry created {len(rec.created)} campaigns. The claim was "
        f"released by the escaping exception, so one consent funded two."
    )
    assert len(rec.funded) == 1, (
        f"one consent funded {len(rec.funded)} campaigns"
    )


# ---------------------------------------------------------------------------
# /campaigns/<id> WHEN THE RUN READ DID NOT COMPLETE, and why nothing here
# asserts a 503 (register items A90 and A94).
#
# This route reads through the two-outcome `cc.get_campaign`, so an unreadable
# run is indistinguishable from an absent one and takes the launch-cutover
# fallback: the wet-lab forward is consulted, says None under the same fault,
# and the user lands on the runs list with HTTP 200 and no message. Benign, and
# unchanged by A90 -- the two tests that pin it are
# `test_wetlab_email_link_forwards_to_lab_projects` and
# `test_compute_miss_that_is_not_wetlab_falls_back_to_list` in
# tests/test_campaigns_cutover_redirects.py.
#
# The TARGET arm is not symmetric with this one and that is deliberate: its
# absent answer is a rendered `404.html`, so a read that never completed was
# telling the user their own target does not exist. Its 503 is pinned in
# tests/test_target_routes.py. Mirroring it here was built and reverted for the
# request-cost reason recorded beside `compute_campaign_detail`; A94 carries the
# residual, which is that a `?handoff=unverified` refusal whose fault outlives
# the redirect does not reach a banner on this arm.
# ---------------------------------------------------------------------------
