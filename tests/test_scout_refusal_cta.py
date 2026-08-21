"""Phase 5: the anonymous rate-limit wall is a funnel, and it must not lie.

``tests/test_scout_anon_charge_pairing.py`` proves the SERVER separates a
cookie-less refusal (``REASON_NO_SESSION``) from an ordinary over-allowance one
(``REASON_SESSION_LIMITED``). That separation only buys anything if the page
actually branches on it, and the branch lives in JavaScript, where no Python
test can see it. Asserting on template source text would not help either: a
regex for ``no_session`` cannot tell a guard from a comment mentioning one.

So this lifts the real block out of the shipped template and runs it under node
against a stubbed DOM (``tests/js/scout_refusal_harness.cjs``), the same way
``tests/test_hotspot_picker_runtime.py`` runs the real picker. Assertions are on
emitted behaviour: what got appended to the error element, and what came back
out of the refusal path.

THE PROPERTY THAT MATTERS. A visitor refused with ``no_session`` has cookies
blocked. The login session is a cookie too, so "sign in to keep going" is a
promise the product cannot keep — it sends them somewhere that cannot work and
loses them. ``test_a_cookie_blocked_visitor_is_never_offered_sign_in`` is the
one that holds that up; the rest guard the plumbing around it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "templates" / "scout" / "index.html"
HARNESS = REPO_ROOT / "tests" / "js" / "scout_refusal_harness.cjs"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

# The block runs verbatim; it carries no Jinja, so the template file is the
# shipped artifact and there is nothing for a render to substitute. Both
# anchors are asserted below, so deleting or renaming either fails loudly here
# instead of silently testing an empty string.
_START = "    var SIGNIN_HELPS"
_END = "    function _clearChainScopedResults()"


def _refusal_block() -> str:
    src = TEMPLATE.read_text(encoding="utf-8")
    start = src.find(_START)
    end = src.find(_END, start + 1)
    assert start != -1, f"{_START!r} not found — did the CTA block move?"
    assert end != -1, f"{_END!r} not found — did the block move?"
    block = src[start:end]
    assert "{{" not in block and "{%" not in block, "Jinja leaked into the block"
    for name in ("appendSigninCta", "signinCanHelp", "showError"):
        assert f"function {name}" in block, f"{name} missing from the block"

    # Every id the block reaches for must exist in the page. The DOM stub
    # auto-creates on demand, which keeps the harness robust to upstream
    # additions but also means a renamed or deleted element stays green here
    # while throwing at page load in a real browser — QC round 2's N4.
    ids = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", block))
    missing = sorted(i for i in ids if f'id="{i}"' not in src)
    assert not missing, f"block reads elements the page does not define: {missing}"
    return block


@pytest.fixture(scope="module")
def results(tmp_path_factory) -> dict:
    block = _refusal_block()
    js = tmp_path_factory.mktemp("scoutjs") / "block.js"
    js.write_text(block, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(HARNESS), str(js)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


# ----------------------------------------------------------------------
# Who is offered a way forward, and who is not
# ----------------------------------------------------------------------

@needs_node
@pytest.mark.parametrize(
    "reason", ["rate_limited", "session_rate_limited", "no_session"]
)
def test_a_cookie_blocked_visitor_is_never_offered_sign_in(results, reason):
    """THE lie guard, and it has to hold on EVERY rate-limit reason.

    An earlier version keyed this on the reason alone, excluding no_session and
    trusting the other two. Independent QC measured that as wrong at the wall
    that matters: scout/routes.py gives /upload, /fetch-pdb and /example no
    session tier, so a cookies-blocked visitor is refused there as
    ``rate_limited`` and was handed a sign-in link that could not work.
    """
    assert results[reason + "_cookies_off"] is None, (
        f"{reason} offered a sign-in link to a visitor with cookies disabled; "
        "the login session is a cookie too, so that link cannot work"
    )


@needs_node
@pytest.mark.parametrize(
    "reason", ["rate_limited", "session_rate_limited", "no_session"]
)
def test_every_rate_limit_reason_offers_a_way_forward_when_cookies_work(
    results, reason
):
    """The other direction, which the same QC round found broken too.

    ``no_session`` covers any caller with no session id yet — the key is minted
    lazily by the owner-key helper, not by a successful upload — so it covers a
    visitor whose cookies are fine and who simply has not uploaded yet. Signing
    in genuinely lifts the limiter for them — it short-circuits the decorator on
    session["user_email"] — so suppressing the link denied a working way out.
    """
    cta = results[reason + "_cookies_on"]
    assert cta is not None, f"{reason} left the visitor at a dead end"
    assert cta["href"].startswith("/login?next="), cta


@needs_node
@pytest.mark.parametrize("cookies", ["_cookies_on", "_cookies_off"])
def test_an_ordinary_error_never_gets_a_sign_in_link(results, cookies):
    """A parse failure is not a refusal and an account does not fix it."""
    assert results["undefined" + cookies] is None


@needs_node
@pytest.mark.parametrize("reason", ["busy", "at_capacity"])
def test_the_capacity_refusals_offer_the_link_too(results, reason):
    """An earlier version excluded these, reasoning that "the queue is global".
    It is not, and the code says so outright: anon_compute_slot yields True for
    a signed-in caller WITHOUT consuming a slot, and the live-job check returns
    None when _signed_in_owner_key() is set. QC measured both. Both server
    messages already say "sign in", so withholding the link meant the page
    refused to act on its own advice."""
    cta = results[reason + "_cookies_on"]
    assert cta is not None, f"{reason} withheld a link that would have worked"
    assert cta["href"].startswith("/login?next="), cta


@needs_node
@pytest.mark.parametrize("reason", ["busy", "at_capacity"])
def test_the_capacity_refusals_still_respect_the_cookie_gate(results, reason):
    assert results[reason + "_cookies_off"] is None


@needs_node
def test_a_browser_without_navigator_still_gets_the_offer(results):
    """Fail toward the working link. Cookies are on for the overwhelming
    majority, so a missing navigator must not silently delete the funnel — and
    must not throw inside an error handler, which would leave the page blank."""
    assert results["no_navigator"] is not None


@needs_node
def test_the_analyze_element_gets_the_cta_too(results):
    """Both error surfaces on the page, not just the upload one."""
    assert results["analyze_error_element"] is not None


# ----------------------------------------------------------------------
# The refusal text itself
# ----------------------------------------------------------------------

@needs_node
def test_the_server_message_survives_next_to_the_link(results):
    """The link is appended, not substituted — the server still owns the
    wording, and it is set as text so it is never parsed as HTML."""
    assert results["message_preserved"] == "Too many requests from this network."


@needs_node
def test_next_carries_the_full_current_location(results):
    cta = results["next_with_search"]
    assert cta["href"] == "/login?next=%2Fscout%2F%3Fref%3Demail", cta


# ----------------------------------------------------------------------
# The wiring, which the harness above CANNOT see.
#
# QC measured the hole: all five refusal call sites live outside the block
# the harness extracts, so reverting every one of them — deleting the entire
# user-facing feature — left the full suite byte-identically green. (No
# count quoted: an absolute pinned in prose is wrong by the next merge,
# and this one outlived two rounds after being 'removed' by an edit that
# silently matched nothing.) A JS syntax error just below the block did the same. The harness
# proves the helpers behave; nothing proved anyone calls them.
#
# These two are deliberately coarser than the harness. They assert structure,
# not behaviour, and that is the honest description: they exist to make a
# silent deletion loud, not to prove the page works.
# ----------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)
# A call whose FIRST argument comes from a server response. Those are the
# refusal paths; calls passing a literal ('Network error.') have no reason to
# forward and are correctly excluded.
_SERVER_ERROR_CALL = re.compile(r"show(?:Analyze)?Error\(\s*data\.[^;]*?\);")


def _main_script() -> str:
    src = TEMPLATE.read_text(encoding="utf-8")
    bodies = _SCRIPT_RE.findall(src)
    assert bodies, "no inline <script> found in the scout page"
    return max(bodies, key=len)


def test_every_server_rendered_error_forwards_the_reason():
    """Delete the reason at any call site and this goes red.

    Without it the helper still works perfectly and is simply never told which
    refusal it is rendering, so every visitor silently loses the way forward.
    """
    calls = _SERVER_ERROR_CALL.findall(_main_script())
    assert len(calls) >= 5, (
        f"expected at least the five refusal call sites, found {len(calls)}: {calls}"
    )
    missing = [c for c in calls if "data.reason" not in c]
    assert not missing, f"these render a server error but forward no reason: {missing}"


@needs_node
def test_the_page_script_still_parses(tmp_path):
    """A syntax error anywhere in this script kills the whole page — every
    handler, not just the refusal path — and the harness would not notice,
    because it only ever extracts and runs one block out of the middle."""
    body = _main_script()
    # One Jinja expression lives in this script; stub it so node sees only JS.
    body = re.sub(r"\{\{.*?\}\}", "false", body)
    path = tmp_path / "page.js"
    path.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        ["node", "--check", str(path)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"scout page script does not parse:\n{proc.stderr}"


# ----------------------------------------------------------------------
# The ids, checked against the RENDERED page.
#
# _refusal_block() greps the raw template, which certifies "this string is
# somewhere in the file" — not "this element exists for an anonymous
# visitor". QC round 3 got the suite to stay green over three broken pages:
# the old id left behind in a comment, the element wrapped in
# {% if session.get('user_email') %} so anonymous callers never receive it,
# and an <input> swapped for a <div>.
#
# So this renders /scout/ as a real anonymous request and collects ids with
# a parser rather than a substring search — a commented-out or
# Jinja-excluded element is genuinely absent from the output, and a parser
# is the only thing that can tell.
# ----------------------------------------------------------------------

from html.parser import HTMLParser  # noqa: E402


class _IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                self.ids.add(value)


@pytest.fixture
def scout_pages(monkeypatch):
    """Both renders of /scout/, because they are not the same page.

    The refusal is rendered for an ANONYMOUS visitor, but the same block ships
    on the signed-in page too, so both renders have to hold. QC round 4 gated
    #job-id behind
    {% if not session.get('user_email') %} — absent from the signed-in
    render — and the full suite stayed green, because the guard only ever
    looked at the anonymous one.
    """
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    pages = {}

    client = flask_app.test_client()
    resp = client.get("/scout/")
    assert resp.status_code == 200, resp.status_code
    pages["anonymous"] = resp.get_data(as_text=True)

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_email"] = "someone@example.com"
        sess["user_id"] = "u-test"
    resp = client.get("/scout/")
    assert resp.status_code == 200, resp.status_code
    pages["signed_in"] = resp.get_data(as_text=True)
    return pages


@pytest.mark.parametrize("render", ["anonymous", "signed_in"])
def test_every_element_the_block_touches_reaches_the_visitor(scout_pages, render):
    anon_page = scout_pages[render]
    block = _refusal_block()
    wanted = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", block))
    assert wanted, "extracted no element ids — the block or the regex moved"

    collector = _IdCollector()
    collector.feed(anon_page)
    missing = sorted(wanted - collector.ids)
    assert not missing, (
        "the refusal block reads elements this render does not "
        f"render: {missing}. The block reads them without a null check, so a "
        "missing element throws and takes the rest of the script with it."
    )


@pytest.mark.parametrize("render", ["anonymous", "signed_in"])
@pytest.mark.parametrize("element_id", ["error-message", "analyze-error"])
def test_both_refusal_surfaces_are_announced(scout_pages, render, element_id):
    """A refusal is the only thing telling a bounced visitor where to go, so it
    has to reach a screen reader.

    #analyze-error was already a live region. #error-message was not, and this
    change made that matter: it replaced a browser alert() on /example — which
    assistive tech announces — with text written into a silent div, and
    /example is the INTAKE wall, the first one a real lab meets.

    WHAT THIS ASSERTS, precisely, because an earlier version of it did not.
    Being a live region is a PROPERTY with two spellings: aria-live, or
    role="status", whose implicit aria-live IS polite. Either alone is enough
    and both together are redundant, so a test demanding one specific string
    fires on markup that works. It also has to reject aria-hidden, which
    removes the node from the accessibility tree and silences it no matter
    what else is on the element — QC measured the string version passing that.

    What it does NOT assert: that any particular screen reader announces a
    region transitioning from display:none to rendered. That is AT-dependent
    and unmeasurable from here; QC tried and reported it honestly as unproven.
    Nor does it assert focus moves — it does not, and alert() used to. The
    remaining gap wants tabindex + focus() or role="alert", as its own a11y
    pass.
    """
    page = scout_pages[render]
    m = re.search(rf'<[^>]*id="{element_id}"[^>]*>', page)
    assert m, f"#{element_id} missing from the {render} render"
    tag = m.group(0)

    live = 'aria-live="polite"' in tag or 'aria-live="assertive"' in tag
    status_role = 'role="status"' in tag or 'role="alert"' in tag
    assert live or status_role, (
        f"#{element_id} carries a refusal but is not a live region: {tag}"
    )
    assert 'aria-hidden="true"' not in tag, (
        f"#{element_id} is hidden from the accessibility tree, so nothing on "
        f"it can be announced: {tag}"
    )
