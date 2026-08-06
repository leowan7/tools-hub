"""GET /lab-projects/<id> -- the confirmation page of a wet-lab order.

REGISTER ITEM A89. A shortlist over `_MAX_CANDIDATE_REFS` had no remedy the
customer could carry out. Three things had to be true for "star them and send a
second request" to work and none of them was: nothing cleared the browser
shortlist, `openCampaignModal` serialises it in stored order so the second POST
carried the identical first 500 refs, and this page printed a COUNT and no list,
so the customer could not see which 500 went. Following that advice created a
second PAID lab project covering the same designs, which is why the advice was
withdrawn.

WHAT THIS FILE PINS is the three parts that put it back:

  1. `campaign_detail` hands the page the REFS THIS REQUEST COVERED, only on
     `?submitted=1`, and the browser removes those and keeps every other star.
     A refusal that dropped stars would destroy a selection for nothing, so the
     absence is asserted as hard as the presence -- and on the WRONG-SCOPE axis
     too, since a payload aimed at `shortlist_<the wrong id>` reaches some other
     page's selection.
  2. The page lists the designs the request covers, from the row it already
     loaded. NOT through `blueprints.admin._ref_shortlist_view`, which re-reads
     each source job UNSCOPED because it is a staff view of another user's
     submission; calling it here would make a customer page issue cross-tenant
     reads. Its counting rules for `candidate_refs` are reimplemented, and the
     numbers it prints have to add up against the list beneath them.
  3. The advice renders only where that list does. The un-starring happens in a
     browser the server never hears back from, so the list is the safety net: a
     customer who follows the advice after a silent failure is shown the same
     designs listed back. The truncation FACT is not gated on the list -- only
     the instruction is.

`?submitted=1` IS NOT AN EVENT, AND TWO VERSIONS OF THIS FEATURE HAVE TRIED TO
MAKE IT ONE. The route is stateless, so that URL is a permanent instruction: a
reload, a bookmark, an omnibox completion, a history entry, a restored tab, a
brand-new tab session and a forward navigation after a bfcache eviction all
re-render it identically, and both the page's own copy and the confirmation
email invite the customer back to it.

The first version wiped the whole `shortlist_<scope>` key and put a one-shot
marker in front of it. That failed in both directions at once. It destroyed the
never-read remainder -- the 120 designs of a 620-star submit that the truncation
banner is ABOUT and that the row therefore cannot name -- leaving the customer a
list of the 500 that did go and no way to re-identify the rest. And the marker
lived in sessionStorage, which dies with the tab while the URL survives in
history, so the guard was gone by day 3 and a fresh 300-star selection was wiped
by a bookmark with no submit anywhere near it.

The design below removes that whole problem instead of guarding it: the page
NAMES the refs, so running it again removes refs that are already gone. There is
no marker to outlive, and what the payload does not name is never touched. The
tests say so out loud -- the SAME flagged URL is issued TWICE and the emitted
payload is asserted byte-identical, which is the property rather than the
workaround.

THE SESSIONSTORAGE KEY IS NOT SPELLED OUT ON THIS PAGE.
`window.dropShortlistRefs` in static/js/candidate_table.js builds it with the
same `storageKey()` the star toggle writes with, and compares designs with the
same `refKey()` the star toggle compares with; the page loads that file for that
one call. So the JS half below is a cross-boundary hook exactly like the ones in
tests/test_candidate_table_js_contract.py, and it reuses that file's
comment-stripper rather than growing a second one: a plain search would let the
header comment answer for a definition that had been renamed away.

AND THE JS HALF IS EXECUTED, which is new. `.github/workflows` installs no node
and this repo carries no JS test runner, but a hosted runner image can still put
one on PATH, so whether PART 4 runs on the machine that gates merges is NOT
established either way -- an earlier version of this paragraph inferred that it
does not. PART 4 runs `node` where it finds it and SKIPS where it does not, and
every assertion that is source-only says so in its own name or docstring. The
two properties whose loss would be worst, the filter's polarity and the bfcache
handler not re-binding, are pinned in PART 1 as source as well, so neither of
them depends on what CI turns out to have. The round that reviewed the first version of
this feature found two mutations to its one-shot that went GREEN against the
whole suite, because source-order assertions cannot execute anything. Part 4 is
what closes that: the removal is driven against the real file with a stubbed
sessionStorage, so "the uncovered tail survives" is measured rather than
reasoned about.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from blueprints.lab_projects import _MAX_LISTED_DESIGNS, _ordered_shortlist
# The audited stripper, not a second copy of it. See the module docstring.
from tests.test_candidate_table_js_contract import _JS as _STRIPPED_JS

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_TID = "aaaaaaaa-1111-2222-3333-444444444444"
_CID = "bbbbbbbb-1111-2222-3333-444444444444"
_JID = "cccccccc-1111-2222-3333-444444444444"


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


def _ctx():
    return SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com",
    )


_BASE_ROW = {
    "id": "lab-9", "user_id": "u-1", "target_name": "HER2",
    "assay_type": "yeast_display", "budget_band": "pilot",
    "status": "submitted", "candidate_indices": [],
}


def _row(**kw) -> dict:
    return dict(_BASE_ROW, **kw)


def _target_row(refs=None, **kw) -> dict:
    """The shape `_submit_target_shortlist` writes."""
    return _row(
        submission_source="target", source_target_id=_TID,
        candidate_refs=refs if refs is not None else [{"job_id": "j-bc", "index": 0}],
        **kw,
    )


def _page(client, row, query="") -> str:
    from shared.campaigns import Campaign
    campaign = Campaign.from_row(row)
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.lab_projects.load_user_context", return_value=_ctx()), \
            patch("shared.campaigns.get_campaign", return_value=campaign):
        resp = client.get("/lab-projects/" + str(row["id"]) + query)
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


# The CALL, not the mention. `window.dropShortlistRefs &&` guards the same
# line, so a search for the bare name matches whether or not anything is
# invoked. BOTH arguments are captured: the scope alone cannot tell a removal of
# named refs from a wipe of the whole key.
_CALL = re.compile(
    r"window\.dropShortlistRefs\("
    r"(\"(?:[^\"\\]|\\.)*\")\s*,\s*(\[.*?\])\);",
    re.S,
)


def _clear_call(html: str):
    """``(scope, refs)`` the page tells the browser to un-star, or ``None`` for
    a page that names nothing. Decoded through ``json.loads`` because the
    template emits both values through ``|tojson``, so the assertions are on the
    values the browser receives rather than on the source text around them."""
    matches = _CALL.findall(html)
    if not matches:
        return None
    # One call per page. Two would mean one of them is reachable without the
    # gates `campaign_detail` applies, and the non-greedy array match above
    # would silently read only the first.
    assert len(matches) == 1, matches
    scope, refs = matches[0]
    return json.loads(scope), json.loads(refs)


def _cleared_scope(html: str):
    """Just the scope half of :func:`_clear_call`."""
    call = _clear_call(html)
    return call[0] if call else None


def _dropped_refs(html: str):
    """Just the refs half of :func:`_clear_call`, or ``None``."""
    call = _clear_call(html)
    return call[1] if call else None


# This file's own directory's parent -- the repo root -- used to decide what
# counts as "this codebase" below. Derived rather than spelled, so a checkout
# under any path works.
_REPO_ROOT = os.path.normcase(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
# The file PART 4 executes. The same one `_STRIPPED_JS` is stripped from, so the
# source assertions and the runtime ones cannot be reading different code.
_JS_FILE = pathlib.Path(_REPO_ROOT) / "static" / "js" / "candidate_table.js"


def _client_sites() -> list:
    """``[(module, attr, original)]`` -- every live binding of a Supabase client
    factory in a module loaded FROM THIS REPO, found rather than named.

    Nearly every query in this codebase starts at
    `shared.credits.get_service_client` or `get_supabase_client`, and most
    modules bind those at import, so the source AND each already-bound alias
    have to be replaced together. Walking `sys.modules` is what keeps this from
    degenerating back into a list of function names, which is the form that let
    a real extra read through.

    NEARLY, AND THE GAP IS NAMED RATHER THAN GLOSSED. `scout/handoff.py` and
    `scout/quota.py` each define a private `_get_service_client` calling
    `supabase.create_client` directly, across seven call sites, and this sweep
    sees neither. So the two names below are still an allowlist and the path
    filter fixed only the other half of the staleness. Sound for what it is
    used for -- `campaign_detail` reaches no `scout` module -- and it would
    widen silently the day either of those is renamed to the public spelling.

    THE FILTER IS THE FILE PATH, NOT A PACKAGE LIST, and the package list is why
    this docstring was false when it was written. It named ("shared",
    "blueprints", "app", "cron"), which left `webhooks.modal`,
    `webhooks.stripe` and `scripts/calibration/poll_results.py` live and
    excluded. `billing`, `tools` and `gpu` bind neither name anywhere, so an
    earlier version of this sentence also listed exclusions that never existed. A list of
    top-level packages is a list of names wearing a different hat, and it goes
    stale the first time a package is added -- silently, in the direction that
    makes the guards below pass on code they never covered. A path test cannot:
    a new package under this root is inside it by construction. Third-party code
    is excluded because it lives outside the root, and the two explicit
    exclusions cover a venv checked out INSIDE it.
    """
    # The three modules the reach assertion below names, imported HERE so that
    # assertion is about the sweep rather than about which test ran first. Every
    # other caller reaches them through `_page`, which imports them lazily, so a
    # caller that does not render a page saw a shorter `sys.modules` and the
    # reach check failed for a reason that had nothing to do with the filter.
    import shared.campaigns  # noqa: PLC0415, F401
    import shared.credits  # noqa: PLC0415, F401
    import shared.jobs  # noqa: PLC0415, F401

    sites: list = []
    for _mod_name, module in list(sys.modules.items()):
        if module is None:
            continue
        try:
            path = getattr(module, "__file__", None)
        except Exception:  # noqa: BLE001 - a module with a hostile __getattr__
            continue
        if not path:
            continue
        path = os.path.normcase(os.path.abspath(path))
        if not path.startswith(_REPO_ROOT + os.sep):
            continue
        if "site-packages" in path or (os.sep + "venv" + os.sep) in path:
            continue
        for attr in ("get_service_client", "get_supabase_client"):
            try:
                original = getattr(module, attr, None)
            except Exception:  # noqa: BLE001
                continue
            if callable(original):
                sites.append((module, attr, original))
    names = {m.__name__ + "." + a for m, a, _ in sites}
    # The sweep asserts its own reach: a filter that stopped matching would
    # patch nothing and every guard built on it would pass on any code at all,
    # which is the defect these were written to retire.
    assert "shared.credits.get_service_client" in names, sorted(names)
    assert "shared.campaigns.get_service_client" in names, sorted(names)
    assert "shared.jobs.get_service_client" in names, sorted(names)
    return sites


def test_the_read_guard_reaches_bindings_outside_the_four_named_packages():
    """THE SWEEP'S OWN REACH, asserted against the defect it had.

    `_client_sites` filtered `sys.modules` on a hardcoded tuple of top-level
    packages, and a reviewer probe found `webhooks.modal.get_service_client`
    live and outside it. Every guard built on the sweep -- the "no client at
    all" one and the delta counter -- was therefore narrower than its own
    docstring, which said "every live binding".

    Driven by importing a module the old tuple excluded and asserting the sweep
    picks its binding up. Reverting the filter to the package list reds this
    line; nothing else in the suite noticed.
    """
    import webhooks.modal  # noqa: PLC0415, F401

    names = {m.__name__ + "." + a for m, a, _ in _client_sites()}
    assert "webhooks.modal.get_service_client" in names, sorted(names)


@contextlib.contextmanager
def _no_supabase_client():
    """Make every binding :func:`_client_sites` finds raise, for the block."""
    def _boom(*a, **kw):
        raise AssertionError("reached for a Supabase client")

    with contextlib.ExitStack() as stack:
        for module, attr, _ in _client_sites():
            stack.enter_context(patch.object(module, attr, _boom))
        yield


@contextlib.contextmanager
def _counted_supabase_clients():
    """Yields a list that grows by one name per client acquisition.

    Counting rather than forbidding, because a whole page render legitimately
    acquires several: `app.py::inject_workspace_context` is a template context
    processor, so the tier lookup, the navbar wallet chip, the workspace count
    and the onboarding-ribbon check all fire on EVERY authenticated page in this
    app and have nothing to do with this panel. A guard that forbade all reads
    would therefore be measuring the chrome. What this item may not do is add
    one, so what is compared is a DELTA.

    Each wrapper calls the ORIGINAL it captured rather than the patched
    attribute, so an alias and its source are never counted twice for one call.
    """
    calls: list = []

    def _wrap(original, name):
        def _counted(*a, **kw):
            calls.append(name)
            return original(*a, **kw)
        return _counted

    with contextlib.ExitStack() as stack:
        for module, attr, original in _client_sites():
            stack.enter_context(patch.object(
                module, attr, _wrap(original, module.__name__ + "." + attr)))
        yield calls


class _ListItems(HTMLParser):
    """Visible text of every ``<li>`` inside an ``<ol>``. The page chrome --
    base.html and the two templates it includes -- renders no ``<ol>``, and this
    page's own content block has exactly one, so these are the ordered designs
    and nothing else. Every assertion below compares the whole list or its exact
    length, so chrome that grew an ``<ol>`` reds them rather than passing
    unnoticed."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_ol = 0
        self._in_li = 0
        self._buf: list = []
        self.items: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "ol":
            self._in_ol += 1
        elif tag == "li" and self._in_ol:
            self._in_li += 1

    def handle_endtag(self, tag):
        if tag == "ol" and self._in_ol:
            self._in_ol -= 1
        elif tag == "li" and self._in_li:
            self._in_li -= 1
            self.items.append(re.sub(r"\s+", " ", "".join(self._buf)).strip())
            self._buf = []

    def handle_data(self, data):
        if self._in_li:
            self._buf.append(data)


def _designs(html: str) -> list:
    parser = _ListItems()
    parser.feed(html)
    return parser.items


class _VisibleText(HTMLParser):
    """Every character a reader sees, and none they do not. ``<script>`` and
    ``<style>`` are CDATA to html.parser, so their bodies arrive through
    ``handle_data`` like anything else and are skipped here by tag; Jinja has
    already dropped its own ``{# #}`` comments before this sees the page, and
    HTML comments never reach ``handle_data`` at all. So a word banned from the
    copy can still be discussed in either kind of comment, which is the point:
    the ban is on what the product SAYS."""

    _MUTE = ("script", "style")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._muted = 0
        self._buf: list = []

    @property
    def text(self) -> str:
        return "".join(self._buf)

    def handle_starttag(self, tag, attrs):
        if tag in self._MUTE:
            self._muted += 1

    def handle_endtag(self, tag):
        if tag in self._MUTE and self._muted:
            self._muted -= 1

    def handle_data(self, data):
        if not self._muted:
            self._buf.append(data)


# The commercial vocabulary this product's customer copy may not use. Word
# boundaries and explicit inflections: `\border\b` does not match "ordered", so
# a stem would have missed exactly the past tense the withdrawn panel used
# ("designs already ordered").
_COMMERCIAL = re.compile(
    r"\b(order|orders|ordered|ordering|purchase|purchases|purchased|"
    r"purchasing|buy|buys|buying|bought|invoice|invoices|invoiced)\b",
    re.IGNORECASE,
)

_ADVICE = ("To include anything that was over the limit, star it on the "
           "source page and send a second request.")
# The truncation FACT, which is disclosed whatever the list does. Kept apart
# from `_ADVICE` on purpose: they are asserted present and absent in different
# combinations, and one string covering both would make that untestable.
_FACT = "Up to 120 further starred designs were over the per-request limit."


# ---------------------------------------------------------------------------
# PART 1: the payload names what the request covered, on a submit and nothing
#         else
# ---------------------------------------------------------------------------

def test_a_submitted_request_names_the_refs_it_covered(client):
    """The whole remedy rests on this one call: without it the next submit
    re-posts the same refs in the same order and the cap cuts in the same
    place."""
    html = _page(client, _target_row(refs=[
        {"job_id": "j-bc", "index": 0},
        {"job_id": "j-de", "index": 4},
    ]), "?submitted=1")
    assert _clear_call(html) == (_TID, [
        {"job_id": "j-bc", "index": 0},
        {"job_id": "j-de", "index": 4},
    ])
    # The file that owns the key format, loaded for that call.
    assert "js/candidate_table.js" in html


def test_the_payload_names_what_was_covered_and_cannot_name_the_remainder(
        client):
    """THE HIGHEST FINDING OF THE ROUND THAT REVIEWED THIS FEATURE, and the
    reason the design changed shape rather than gaining another guard.

    A 620-star submit stores 500 refs and reports 120 as `truncated`; the 120
    were never parsed into pairs, so the row cannot name them and neither can
    this page. The first version removed the WHOLE key, which destroyed exactly
    those 120 -- the designs the truncation banner is about and the ones the
    advice tells the customer to send in a second request -- and left them a
    scroll box of the 500 that did go to diff by eye.

    Naming the refs is what fixes it: the payload is the 500, so the 120 the
    payload does not mention are the ones that survive. That the browser keeps
    them is measured in PART 4; what is asserted here is the server half, that
    the payload is exactly the covered set and never a wildcard.
    """
    covered = [{"job_id": "j-bc", "index": i} for i in range(_MAX_LISTED_DESIGNS)]
    html = _page(client, _target_row(refs=covered),
                 "?submitted=1&truncated=120")
    refs = _dropped_refs(html)
    assert refs == covered
    assert len(refs) == _MAX_LISTED_DESIGNS
    # The disclosure that the remainder exists is on the same page, so the
    # payload's silence about it is not the customer's only clue.
    assert _FACT in re.sub(r"\s+", " ", html)


def test_the_same_flagged_url_twice_emits_the_identical_payload(client):
    """IDEMPOTENT BY CONSTRUCTION, which is what retired the one-shot marker.

    `?submitted=1` is not an event. This route is stateless, so the URL is
    permanent: a reload, a bookmark, an omnibox completion, a history entry, a
    restored tab, a brand-new tab session and a forward navigation after a
    bfcache eviction each re-execute the call, and the page's own copy and the
    confirmation email both invite the customer back to it.

    The first version guarded a whole-key wipe with a sessionStorage marker,
    which put the guard in a store that dies with the TAB while the URL lives in
    history -- so day 3, new tab, 300 fresh stars, one bookmark click, all gone.
    Removing NAMED refs needs no guard: the second execution removes refs that
    are no longer there. So the two responses being identical is the property,
    not a workaround, and the whole emitted statement is compared so nothing
    per-request (a nonce, a timestamp) can be smuggled into it.
    """
    row = _target_row()
    first = _page(client, row, "?submitted=1")
    second = _page(client, row, "?submitted=1")
    assert _clear_call(first) == (_TID, [{"job_id": "j-bc", "index": 0}])
    assert _clear_call(second) == _clear_call(first)
    assert _CALL.search(first).group(0) == _CALL.search(second).group(0)


def test_a_legacy_index_row_names_the_job_its_stars_were_recorded_under(client):
    """THE SHAPE MISMATCH THE PAYLOAD HAS TO BRIDGE. A 'web' row stores bare
    integers in `candidate_indices`, but the browser stores `{j, i}` against the
    star button's `data-job` -- which templates/components/candidate_table.html
    sets to the row's source job, and which the legacy arm of `campaigns_submit`
    writes to `source_job_id` on the row it creates. So a design with no job of
    its own is emitted under that column.

    Emitting the bare index instead would key as "undefined#N" and match no
    stored star, so the whole arm would silently un-star nothing -- which is
    also why the resolution is asserted against `source_job_id` here rather than
    left to the JS to guess.
    """
    html = _page(client, _row(
        submission_source="web", source_job_id=_JID,
        candidate_indices=[0, 7]), "?submitted=1")
    assert _clear_call(html) == (_JID, [
        {"job_id": _JID, "index": 0},
        {"job_id": _JID, "index": 7},
    ])


def test_a_second_request_under_the_same_scope_names_only_its_own_refs(client):
    """THE PAIR. The customer stars the remainder under the same parent, submits
    again and lands on a DIFFERENT lab project row. That row names its own
    covered designs and no others, so the second confirmation page cannot reach
    back and un-star anything the first request left alone.

    A payload keyed to the scope rather than to the row would un-star the whole
    selection on the second arrival, which is the first version's defect wearing
    a new shape.
    """
    first = _page(client, _target_row(refs=[{"job_id": "j-bc", "index": 0}]),
                  "?submitted=1")
    second = _page(client, _target_row(
        id="lab-10", refs=[{"job_id": "j-bc", "index": 9}]), "?submitted=1")
    assert _clear_call(first) == (_TID, [{"job_id": "j-bc", "index": 0}])
    assert _clear_call(second) == (_TID, [{"job_id": "j-bc", "index": 9}])


def test_the_js_filters_the_stored_list_and_holds_no_marker(client):
    """SOURCE, NOT EXECUTION, and named as that: this asserts what the file
    SAYS. The behaviour is driven for real in PART 4, which skips where `node`
    is absent -- so this line is what holds wherever node is missing, and it is
    deliberately the shape of the code rather than its effect.

    Three absences and one presence, each of which was a live construct one
    round ago. `removeItem` at all: the whole-key wipe is what destroyed the
    never-read remainder. `sessionStorage` written outside the two helpers: the
    one-shot marker was a second key with its own name function, in a store that
    dies with the tab while the URL that triggers the wipe does not. And the
    presence of `saveShortlist(scope, kept)`: a filter whose result is never
    stored removes nothing at all.

    Plus the filter's POLARITY, which is not from that round and was the one
    property here that nothing source-only pinned at all.
    """
    src = _STRIPPED_JS
    assert "removeItem(" not in src
    assert "clearedKey" not in src
    assert "window.dropShortlistRefs = function" in src
    body = src[src.index("window.dropShortlistRefs = function"):]
    body = body[:body.index("\n  };")]
    # One identity function on both sides of the comparison -- the same one the
    # star toggle uses -- so a design is dropped exactly when clicking its star
    # would have matched it.
    assert body.count("refKey(") == 2, body
    assert "saveShortlist(scope, kept)" in body
    # THE POLARITY, and it is the whole safe/destructive axis. `=== true` keeps
    # exactly the refs this request covered and destroys the never-read
    # remainder -- round 1's defect verbatim -- while satisfying every other
    # assertion in this test. Added in round 3 after a reviewer replayed that
    # two-character edit against a copy and found every source-only assertion
    # in this file still green, with only node-gated tests catching it.
    assert "drop[refKey(r.j, r.i)] !== true" in body
    # It goes through the two helpers rather than touching the store itself,
    # which is what keeps `storageKey()` the single spelling of the key.
    assert "sessionStorage" not in body
    assert "loadShortlist(scope)" in body


def test_the_falsy_scope_guard_stands_in_front_of_all_of_it(client):
    """DEFENCE IN DEPTH, PINNED AS SOURCE ORDER AND ADMITTED AS THAT. The server
    already refuses to emit a call with an empty scope, and
    `test_a_row_with_no_parent_id_names_nothing_rather_than_a_bare_prefix` pins
    that half on a rendered page. PART 4 drives the JS half for real where node
    is available; this is what remains where it is not.

    It matters because the scope arrives from a database row. Falsy, it would
    reach `shortlist_undefined` -- a key some other page could be using.
    """
    src = _STRIPPED_JS
    i_def = src.index("window.dropShortlistRefs = function")
    i_guard = src.index("if (!scope || !refs || !refs.length) return;")
    # Anchored on the READ of the store, not on `loadShortlist(scope)`: that
    # substring also occurs in the function's own DEFINITION, higher up the
    # file, so the naive anchor compares against a position before `i_def` and
    # the assertion can never hold.
    assert i_def < i_guard < src.index("var before = loadShortlist(scope);")


def test_the_bfcache_handler_repaints_and_does_not_rebind(client):
    """SOURCE, NOT EXECUTION, and it exists because PART 4 was the only cover
    this handler had. Two properties, each removable by one line:

    `e.persisted`, without which the handler also runs on every ordinary load,
    repainting a page that has just painted. And the ABSENCE of `initTable`,
    which is the real hazard: re-initialising binds a second copy of every
    listener, so one star click toggles on and then straight back off and the
    star reads as dead software. This handler is the one thing THIS ITEM added
    that runs on a page nobody submitted anything from; the `DOMContentLoaded`
    boot and everything `initTable` binds have always done so.
    """
    src = _STRIPPED_JS
    assert src.count("window.addEventListener('pageshow'") == 1, src
    start = src.index("window.addEventListener('pageshow'")
    # BOUNDED TO THE HANDLER, not run to EOF. Unbounded it reddened on a
    # behaviour-preserving reorder of the file, and stayed GREEN when a
    # reviewer moved the re-bind one indirection away into a helper defined
    # above it. Bounding fixes the false positive. The false NEGATIVE is
    # inherent to a token search, so what this pins is the absence of the NAME
    # from the handler body; PART 4 is what covers the property itself.
    end = src.index("\n  });", start)
    body = src[start:end]
    assert "if (!e.persisted) return;" in body
    assert "initTable" not in body
    assert "restoreStarState(table, scope)" in body


def test_a_page_reached_without_the_submitted_flag_names_nothing(client):
    """THE PAIR. /lab-projects/<id> is linked from the dashboard, so this page
    is opened days after the request while a fresh selection sits in the same
    sessionStorage key. Naming refs on every view would un-star designs on an
    arrival that ordered nothing -- harmless only by luck, since the refs would
    still be this row's, and a claim the page cannot make either way."""
    assert _clear_call(_page(client, _target_row())) is None


@pytest.mark.parametrize(
    "query",
    ["?handoff=unverified", "?handoff=rejected", "?handoff=none",
     "?handoff=failed", "?truncated=120", "?dropped=3", "?submitted=0"],
)
def test_a_url_without_the_submitted_flag_names_nothing(client, query):
    """`?submitted=1` IS THE WHOLE GATE, and this asserts the other side of it
    rather than a property of the `handoff` values, which the earlier name
    claimed. `?submitted=1&handoff=rejected` is refusal-SHAPED and does emit;
    nothing constructs it, because no refusal exit of either ref arm reaches
    this route at all -- most redirect to the parent's own page carrying
    `?handoff=<reason>`, and the two that cannot name a parent go to its list --
    so the flag is all there is to test for.

    These URLs are therefore only hand-reachable, which is exactly why the gate
    is asserted against them: a refused submission that un-starred designs would
    take away the selection the user is being asked to retry, and
    `?handoff=unverified` is the transient fault where retrying is the whole
    remedy.

    `?truncated=120` is here for a second reason since the confirmation email's
    own link started carrying the counts: that link is a real URL a customer
    clicks, and it must reach the disclosure without reaching the payload.

    `?submitted=0` is here because the flag is compared for equality with the
    string "1"; a truthiness test would emit on it.
    """
    assert _clear_call(_page(client, _target_row(), query)) is None


def test_the_scope_is_the_parent_the_shortlist_was_starred_under(client):
    """The key is `shortlist_<scope>` and `scope` is the parent id the results
    table rendered under (templates/components/candidate_table.html), so the
    three row shapes reach three different keys. Any single hardcoded source
    here reads the wrong page's list for the other two -- silently, since a
    filter over a list that holds none of the named refs writes nothing and the
    stars that SHOULD have gone are still set."""
    target = _page(client, _target_row(), "?submitted=1")
    campaign = _page(client, _row(
        submission_source="campaign", source_campaign_id=_CID,
        candidate_refs=[{"job_id": "j-bc", "index": 0}]), "?submitted=1")
    web = _page(client, _row(
        submission_source="web", source_job_id=_JID,
        candidate_indices=[0, 1]), "?submitted=1")
    cleared = [_cleared_scope(target), _cleared_scope(campaign),
               _cleared_scope(web)]
    assert cleared == [_TID, _CID, _JID]
    # Three DIFFERENT keys, asserted over the RENDERED values. The earlier form
    # of this line compared the three module constants to each other, which is
    # a statement about the fixture and can never fail.
    assert len(set(cleared)) == 3


@pytest.mark.parametrize(
    ("parents", "expected"),
    [
        # All three, and then each adjacent pair. A row carrying two parent ids
        # is what distinguishes the orderings; the triple alone cannot, because
        # swapping campaign and job leaves the target-first answer unchanged.
        ({"source_target_id": _TID, "source_campaign_id": _CID,
          "source_job_id": _JID}, _TID),
        ({"source_target_id": _TID, "source_campaign_id": _CID}, _TID),
        ({"source_target_id": _TID, "source_job_id": _JID}, _TID),
        ({"source_campaign_id": _CID, "source_job_id": _JID}, _CID),
    ],
)
def test_a_row_naming_several_parents_uses_the_most_specific_one(
        client, parents, expected):
    """`campaigns_submit` dispatches target -> campaign -> job and the macro
    derives `scope` in that same order, so a row carrying more than one parent
    id was starred under the FIRST of them. Resolving in any other order reads
    a key nothing wrote and leaves the real one set.

    THE PAIRS ARE THE POINT. Only the triple used to be driven here, and
    swapping the campaign and job arms of the resolution passed the entire
    suite: the target still won. Three ids admit six orderings and one case
    separates none of them, so each adjacent pair is asserted and the chain is
    pinned link by link.
    """
    html = _page(client, _row(
        submission_source="target",
        candidate_refs=[{"job_id": "j-bc", "index": 0}], **parents),
        "?submitted=1")
    assert _cleared_scope(html) == expected


def test_a_row_with_no_parent_id_names_nothing_rather_than_a_bare_prefix(client):
    """`source_target_id`, `source_campaign_id` and `source_job_id` are all
    nullable, so the resolution can come out empty. The row driven here is the
    'web' shape with no `source_job_id`; an 'api' row leaves all three NULL too,
    and is covered separately below because it is excluded a second way.

    WHAT THIS PINS IS THE SERVER GATE: `campaign_detail` resolves "" and the
    template emits no call at all, so nothing can be asked to read the key
    `shortlist_`. `dropShortlistRefs` ALSO refuses a falsy scope, and that guard
    is defence in depth rather than the thing under test -- deleting it reds
    nothing here, because this body never runs any JS. PART 4 does run it, and
    covers that guard where node is available.

    THE SAME ROW IS ALSO WHY `_covered_refs` DROPS A REF IT CANNOT NAME A JOB
    FOR: bare indices with no `source_job_id` to resolve against would key as
    "#N", which is NOT harmless. `refKey` concatenates, so "#N" matches a
    stored star whose own job id is the empty string exactly. The drop is what
    makes the empty case unreachable, not a tidy-up on top of a guarantee.
    """
    html = _page(client, _row(
        submission_source="web", candidate_indices=[0]), "?submitted=1")
    assert _clear_call(html) is None


def test_a_bare_index_with_no_source_job_is_left_out_of_the_payload(client):
    """THE OTHER SIDE OF THE SAME RESOLUTION, and the only place the `continue`
    in `_covered_refs` is reachable. A row whose shortlist column holds bare
    integers but which names no `source_job_id` has nothing to resolve them
    against, so those refs would go out as `{"job_id": "", ...}` and key as
    "#N" -- which matches a stored star spelled the same way, since `refKey`
    only concatenates. Whether the macro's `data-job` can ever render empty is
    not pinned anywhere, so the payload is not allowed to depend on it.

    Constructed rather than found: no live arm writes this shape, because the
    legacy arm is the only one that stores bare integers and it always writes
    `source_job_id`. It is asserted because the branch exists, and a payload of
    refs that can only ever match nothing is a claim the page should not make.
    The panel still renders and the count still reconciles -- only the
    un-starring is withheld.
    """
    html = _page(client, _row(
        submission_source="target", source_target_id=_TID,
        candidate_indices=[0, 1]), "?submitted=1")
    assert _clear_call(html) is None
    assert _designs(html) == ["Candidate 1", "Candidate 2"]


def test_the_page_calls_the_one_function_that_owns_the_key_format(client):
    """THE CROSS-BOUNDARY HOOK, both ends. The page never spells
    `shortlist_<scope>` out: it loads static/js/candidate_table.js and calls the
    global, so `storageKey()` stays the single definition and `refKey()` the
    single identity. Rename that global on either side and the un-starring
    silently stops -- the page throws nothing, because the call is guarded, and
    the stars simply stay set.

    The JS half searches the COMMENT-STRIPPED source. The file's header
    advertises this global in prose, and a plain search would find that copy
    after the definition had been renamed away -- the exact hole round 21 of
    tests/test_candidate_table_js_contract.py closed for its three siblings.
    """
    assert "window.dropShortlistRefs = function" in _STRIPPED_JS
    html = _page(client, _target_row(), "?submitted=1")
    assert "js/candidate_table.js" in html
    assert _cleared_scope(html) == _TID


# ---------------------------------------------------------------------------
# PART 2: the designs the request covers, listed from the row already in hand
# ---------------------------------------------------------------------------

def test_the_page_lists_the_designs_that_were_ordered(client):
    """A COUNT was all this page had, which is why the customer could not tell
    which 500 of 620 went. The labels match what `openCampaignModal` puts in the
    review list before submitting, so the two readings of one shortlist agree."""
    html = _page(client, _target_row(refs=[
        {"job_id": "01c3b3a6dead", "index": 0},
        {"job_id": "01c3b3a6dead", "index": 4},
        {"job_id": "beef1234cafe", "index": 2},
    ]), "?submitted=1")
    assert _designs(html) == [
        "Candidate 1 · sub-job 01c3b3a6",
        "Candidate 5 · sub-job 01c3b3a6",
        "Candidate 3 · sub-job beef1234",
    ]


def test_the_legacy_index_shape_is_listed_by_the_same_block(client):
    """ARM-AGNOSTIC BY THE COLUMN. A 'web' row keeps its shortlist in
    `candidate_indices`, and the legacy single-job arm is due to be rerouted
    through the counted parser (register item A91) -- so this block reads
    whichever column the row carries rather than branching on
    `submission_source`, and that arm inherits the list and the advice with no
    edit here. A bare index names no sub-job, so none is printed."""
    html = _page(client, _row(
        submission_source="web", source_job_id=_JID,
        candidate_indices=[0, 7]), "?submitted=1")
    assert _designs(html) == ["Candidate 1", "Candidate 8"]


def test_the_listed_designs_do_not_cost_a_single_extra_read(client):
    """The refs are already on the row this page loaded. `_ref_shortlist_view`
    resolves each source job to name its tool -- UNSCOPED, because it is a staff
    view of another user's submission -- and reusing it here would issue up to
    60 cross-tenant reads to render a customer's own page.

    ASSERTED AS "NO READ", NOT AS "NOT THESE FOUR FUNCTIONS". The earlier form
    patched `get_job` and `read_job` at two import sites each, which proves only
    that those four names were not called: adding a real `list_jobs_by_ids`
    round trip to `_ordered_shortlist` left it green, so the test could not tell
    the fix from its absence.

    TWO HALVES, because the page is not read-free and cannot be made so.
    `app.py::inject_workspace_context` is a context processor: the tier lookup
    and the navbar wallet chip fire on every authenticated render in this app.
    So the builder is put under a guard that forbids a client OUTRIGHT, and the
    PAGE is measured as a delta -- a 60-ref row across 60 distinct sub-jobs
    against a 1-ref row and against a row with no list at all. A per-job
    resolution shows up in the first comparison and a single batched read in the
    second, and neither can hide behind the chrome's own read.
    """
    from shared.campaigns import Campaign
    wide = _target_row(refs=[
        {"job_id": "j-%d" % i, "index": i} for i in range(60)])
    with _no_supabase_client():
        assert _ordered_shortlist(Campaign.from_row(wide))["count"] == 60

    with _counted_supabase_clients() as calls:
        html = _page(client, wide, "?submitted=1")
        wide_reads = len(calls)
        del calls[:]
        _page(client, _target_row(), "?submitted=1")
        one_ref_reads = len(calls)
        del calls[:]
        # No panel at all: 'api' is the shape `_ordered_shortlist` declines.
        _page(client, _row(submission_source="api", candidate_indices=[0]))
        no_list_reads = len(calls)
    assert len(_designs(html)) == 60
    # Not vacuous: the chrome's own read is counted, so a counter that had
    # stopped firing would show as zero here rather than as three equal zeros.
    assert no_list_reads > 0
    assert (wide_reads, one_ref_reads) == (no_list_reads, no_list_reads)


def test_a_repeat_is_listed_once_and_accounted_for(client):
    """A repeated (job_id, index) names ONE physical design -- the write path
    dedupes on exactly that -- so listing it twice shows a paying customer two
    of something they are getting one of. The stored length still has to
    reconcile against the list, so the difference is printed rather than
    swallowed."""
    html = _page(client, _target_row(refs=[
        {"job_id": "j-bc", "index": 0},
        {"job_id": "j-bc", "index": 0},
        {"job_id": "j-bc", "index": 1},
    ]), "?submitted=1")
    assert _designs(html) == ["Candidate 1 · sub-job j-bc", "Candidate 2 · sub-job j-bc"]
    assert "The submitted list held 3 entries: 2 designs, 1 repeat of a " \
        "design already on it." in re.sub(r"\s+", " ", html)


def test_an_entry_that_is_not_a_design_is_counted_not_rendered(client):
    """`candidate_refs` is JSON off a database row, so it can hold a bare
    string, a missing job_id and an index that is not a number. A bare string
    handed to `.get` raised AttributeError and 500ed the staff page once
    already (register item A-5), and this page is reachable by a customer.

    Counted rather than dropped, because the number above the list is the stored
    length: 4 entries, 1 design, 3 that are not designs, and the customer can
    add them up."""
    html = _page(client, _target_row(refs=[
        "not-a-ref",
        {"job_id": "", "index": 2},
        {"job_id": "j-bc", "index": "nope"},
        {"job_id": "j-bc", "index": 0},
    ]), "?submitted=1")
    assert _designs(html) == ["Candidate 1 · sub-job j-bc"]
    flat = re.sub(r"\s+", " ", html)
    assert "The submitted list held 4 entries: 1 design, 3 entries this page " \
        "could not read as a design." in flat
    # 1 + 3 = 4, the number printed against "shortlisted" above it.
    assert "4 shortlisted" in flat


def test_a_one_entry_reconciliation_reads_as_english(client):
    """"held 1 entries" -- the one customer-visible defect either reviewer of
    this item found, and it is on a LIVE write path rather than a corrupted row.
    The legacy arm of `campaigns_submit` does `[int(i) for i in
    json.loads(raw_indices)]` with no range check, so a POST carrying
    `candidate_indices=[-1]` is accepted, stored, and printed by every later
    view of this page.

    The sentence pluralises both clauses AFTER the colon and neither reviewer's
    suite reached the one before it: the two tests that assert this string use
    `stored == 3` and `stored == 4`, where every noun in it is plural anyway.
    """
    html = _page(client, _row(
        submission_source="web", source_job_id=_JID,
        candidate_indices=[-1]), "?submitted=1")
    flat = re.sub(r"\s+", " ", html)
    assert "The submitted list held 1 entry: 0 designs, 1 entry this page " \
        "could not read as a design." in flat
    # The exact rendered defect, asserted as an absence so a partial fix that
    # pluralised only the second clause cannot pass on the line above alone.
    assert "held 1 entries" not in flat
    assert "1 shortlisted" in flat
    # A row that resolved to no designs names no refs: there is nothing this
    # request covered, so there is nothing to un-star.
    assert _clear_call(html) is None


def test_the_list_does_not_grow_without_bound_on_screen(client):
    """A 500-design order is an ordinary one -- that IS the per-request cap --
    and the stored column is not bounded by anything on the read side. The list
    is capped at `_MAX_LISTED_DESIGNS` and says so when the cap takes
    something."""
    html = _page(client, _row(
        submission_source="web", source_job_id=_JID,
        candidate_indices=list(range(_MAX_LISTED_DESIGNS + 100))), "?submitted=1")
    assert len(_designs(html)) == _MAX_LISTED_DESIGNS
    assert f"Showing the first {_MAX_LISTED_DESIGNS} of " \
        f"{_MAX_LISTED_DESIGNS + 100} designs on this request." in html


def test_a_full_size_ref_request_is_listed_in_full(client):
    """The pair. `_MAX_LISTED_DESIGNS` equals the per-request cap, so no row
    either ref arm can write is ever shown as a prefix -- which is what lets the
    advice say "the designs below" without hedging. A display cap set lower
    would make that sentence false on exactly the requests it is written for."""
    html = _page(client, _target_row(refs=[
        {"job_id": "j-bc", "index": i} for i in range(_MAX_LISTED_DESIGNS)
    ]), "?submitted=1&truncated=120")
    assert len(_designs(html)) == _MAX_LISTED_DESIGNS
    assert "Showing the first" not in html
    assert _ADVICE in html


def test_an_api_row_is_not_given_a_design_list(client):
    """'api' is not a shortlist arm. `create_api_campaign` sets
    `candidate_indices` to `range(len(sequences))` to satisfy a NOT NULL column,
    so listing it back prints "Candidate 1..N" for designs nobody starred. The
    count this page has always shown for those rows is unchanged."""
    html = _page(client, _row(
        submission_source="api", candidate_indices=[0, 1, 2]))
    assert _designs(html) == []
    assert "Designs in this request" not in html
    assert "3 shortlisted" in html


def test_an_api_row_is_not_given_a_payload_either(client):
    """THE SECOND HALF OF THE SAME EXCLUSION, and it used to be missing. 'api'
    was left out of the list and left IN the un-starring, so a row of that shape
    carrying a parent id would have named refs for a scope whose designs this
    page refuses to show. It was safe only because
    `shared.campaigns.create_api_campaign` writes no parent id -- an implicit
    dependency on another module that nothing asserted.

    The gate is now the list itself: `campaign_detail` resolves a scope only
    when `_ordered_shortlist` returned something, so the page never asks the
    browser to drop stars whose designs it is not printing. This row names a
    parent, which the resolution would otherwise find.
    """
    html = _page(client, _row(
        submission_source="api", source_job_id=_JID,
        candidate_indices=[0, 1, 2]), "?submitted=1")
    assert _clear_call(html) is None
    assert "3 shortlisted" in html


def test_a_row_whose_refs_column_is_not_a_list_renders_instead_of_500ing(client):
    """`candidate_refs` reaches `_ordered_shortlist` exactly as the driver
    decoded it -- `Campaign.from_row` passes that column through with no
    coercion -- and it is a JSON column, so a scalar fits it. `list(5)` raises
    TypeError, which is a 500 on the confirmation page of a paid request.

    Register item A-5 is the same failure one layer in: a bare string in this
    column raised AttributeError and took the staff page down. That fix hardened
    the ELEMENTS and left the CONTAINER open, and the comment invoking it sat
    four lines from the `list()` call that still crashed.
    """
    html = _page(client, _row(
        submission_source="target", source_target_id=_TID,
        candidate_refs=5), "?submitted=1")
    assert "Designs in this request" not in html
    # And nothing is cleared for a row this page cannot read a shortlist off.
    assert _clear_call(html) is None


@pytest.mark.parametrize("refs", [
    [{"job_id": "j", "index": 0}, {"job_id": "j", "index": 0}],
    ["a-bare-string"],
    # THE ONE THE SHAPE CONTRACT IS FOR. A bare int is a design out of
    # `candidate_indices` and malformed out of `candidate_refs`, because that
    # is the only column the staff view reads and it calls this malformed.
    [7],
    [7, {"job_id": "j", "index": 1}, "x", {}, {"job_id": "j", "index": -1}],
    [{"job_id": "j", "index": "nope"}],
])
def test_the_customer_and_the_staff_page_count_the_same_row_the_same_way(refs):
    """ONE PAID ROW, TWO SURFACES, and ops fulfils against one of them while the
    customer checks the other. `blueprints.admin._ref_shortlist_view` reads
    `candidate_refs` and only that column; `_ordered_shortlist` reads it too,
    for the customer, without its per-job reads. A shape either one classifies
    differently is a design that exists on one page and not the other.

    The claim was that the counting rules were "reimplemented", and they were
    not: this function accepted a bare int as a design out of EITHER column, so
    a `candidate_refs` array holding one gave ops a malformed entry and the
    customer a "Candidate 8". Driven against the real staff function rather than
    against a restatement of it, so a change to either side reds here.
    """
    from shared.campaigns import Campaign
    from blueprints.admin import _ref_shortlist_view
    campaign = Campaign.from_row(_target_row(refs=refs))
    mine = _ordered_shortlist(campaign)
    theirs = _ref_shortlist_view(campaign, lambda _jid: None)
    assert (mine["count"], mine["duplicates"], mine["malformed"]) == (
        theirs["count"], theirs["duplicates"], theirs["malformed"]), refs


def test_ordered_shortlist_answers_for_every_non_empty_refs_column():
    """THE SERVER-SIDE INVARIANT THE EMAIL'S SENTENCE LEANS ON. That body says
    "check your campaign page for what this request covers" and carries no list
    of its own, so `_ordered_shortlist` answering None for a row that stored
    refs would send the reader to a page with nothing on it.

    None means one of exactly two things: the 'api' shape, which no caller
    passes `truncated` for, and a row with no readable shortlist column at all,
    which neither ref arm can create -- each refuses an empty selection before
    it writes.

    RENAMED, BECAUSE THE OLD NAME CLAIMED A PANEL AND THIS RENDERS NO PAGE. It
    drives `_ordered_shortlist` directly -- deliberately, so the whole space of
    entry shapes is covered including the all-unreadable one -- and reverting
    the template's panel gate to `{% if shortlist and shortlist.designs %}` left
    it green while reddening
    `test_the_truncation_fact_survives_a_row_with_no_readable_designs`, which is
    where the rendered half actually lives.
    """
    for refs in ([{"job_id": "j", "index": 0}], ["junk"], [None],
                 [{"job_id": "", "index": 1}], [{}], [0, 1, 2]):
        row = _target_row(refs=refs)
        from shared.campaigns import Campaign
        view = _ordered_shortlist(Campaign.from_row(row))
        assert view is not None, refs
        assert view["stored"] == len(refs), refs
        # The reconciliation identity, on every shape: this is what the page
        # prints and what a customer adds up.
        assert view["stored"] == (
            view["count"] + view["duplicates"] + view["malformed"]), refs


# ---------------------------------------------------------------------------
# PART 3: the advice, and the list it may not appear without
# ---------------------------------------------------------------------------

def test_the_advice_never_renders_without_the_list_it_points_at(client):
    """THE PROPERTY THE WHOLE SHAPE EXISTS FOR. "Star them and send a second
    request" standing alone is the sentence that produced duplicate paid
    requests. It is sayable now because `campaign_detail` names the refs this
    request covered and the browser un-stars exactly those -- but in a browser
    the server never hears back from, so if that fails silently the customer's
    only warning is seeing the same designs listed back. Advice without the list
    is the withdrawn advice again.

    The row here stores entries that resolve to no designs, so there is no list
    to print.
    """
    html = _page(client, _target_row(refs=["not-a-ref"]),
                 "?submitted=1&truncated=120")
    assert _designs(html) == []
    assert _ADVICE not in html


def test_the_truncation_fact_survives_a_row_with_no_readable_designs(client):
    """THE PAIR, AND IT USED TO FAIL. The fact and the advice were one `<p>`
    inside the list's own block, so a row whose entries this page cannot read
    lost BOTH: the customer saw "3 shortlisted", nothing beneath it, and no
    mention anywhere that 120 further designs never reached the lab.

    These counts ride the query string precisely because this page is the only
    place that gets told what did NOT arrive (register item A-7), and the row
    that has least to go on is the last one that should be told nothing. Only
    the INSTRUCTION needed the list as a safety net; the disclosure never did.

    The reconciliation goes with it: `stored` = `count` + `duplicates` +
    `malformed` has to hold when `count` is 0, which is exactly the A-4/A-6
    failure -- a printed number with nothing under it to check against.
    """
    html = _page(client, _target_row(refs=["junk", "junk2",
                                           {"job_id": "", "index": 1}]),
                 "?submitted=1&truncated=120")
    flat = re.sub(r"\s+", " ", html)
    assert _designs(html) == []
    assert _ADVICE not in flat
    assert _FACT in flat
    assert "3 shortlisted" in flat
    assert "The submitted list held 3 entries: 0 designs, 3 entries this page " \
        "could not read as a design." in flat


def test_the_advice_never_renders_over_a_partial_list(client):
    """The other half: the sentence says "the designs BELOW", so a list the
    display cap has cut is not what the request covers. Unreachable from either
    ref arm today -- their cap and the display cap are the same number -- and
    it is the uncapped `candidate_indices` arm (register item A91) that makes it
    reachable at all.

    The FACT still ships, for the reason above: a truncated request whose list
    is also too long to print is the case with the most missing and the least
    said about it.
    """
    html = _page(client, _row(
        submission_source="web", source_job_id=_JID,
        candidate_indices=list(range(_MAX_LISTED_DESIGNS + 1))),
        "?submitted=1&truncated=9")
    assert "Showing the first" in html
    assert _ADVICE not in html
    assert "Up to 9 further starred designs were over the per-request limit." \
        in re.sub(r"\s+", " ", html)


def test_the_advice_renders_with_the_over_the_limit_fact_it_belongs_to(client):
    """The pair for both absences above: an advice block that never rendered
    would satisfy them and leave a customer who lost 120 designs with no way to
    ask for them."""
    html = _page(client, _target_row(), "?submitted=1&truncated=120")
    flat = re.sub(r"\s+", " ", html)
    assert _FACT in flat
    assert "This request covers the 1 design below." in flat
    assert _ADVICE in flat
    assert _designs(html) == ["Candidate 1 · sub-job j-bc"]


def test_the_advice_stands_above_the_list_the_word_below_points_at(client):
    """"the N designs BELOW" is a claim about POSITION, and only the words were
    ever asserted. Moving the paragraph after `</ol>` keeps every other test in
    this file green and makes the sentence false, so the order of the two is
    pinned as an order.
    """
    html = _page(client, _target_row(), "?submitted=1&truncated=120")
    flat = re.sub(r"\s+", " ", html)
    assert flat.index(_ADVICE) < flat.index("<ol")
    assert flat.index("<ol") < flat.index("Candidate 1")


def test_a_clean_request_gets_the_list_and_no_advice(client):
    """The list is not conditional on a shortfall -- it is what the request
    covers -- but the advice is. Rendering the advice unconditionally would tell
    every customer that part of their request is missing."""
    html = _page(client, _target_row(), "?submitted=1")
    assert _designs(html) == ["Candidate 1 · sub-job j-bc"]
    assert _ADVICE not in html
    assert "over the per-request limit" not in html


def test_the_page_never_calls_this_an_order_or_a_purchase(client):
    """"Order" and "purchase" are commercial states this product has not
    entered. The dashboard heading says "Scoping requests you have submitted",
    the shortlist modal says "No commitment -- this is a scoping request", the
    confirmation email's subject says "Scoping request received", and THIS page
    renders a Declined badge, so the row can still be refused. The panel this
    item added called it an order four times.

    THE NAME PROMISED "PURCHASE" AND THE BODY BANNED ONLY "order", twice --
    "Thank you for your purchase" would have stayed green. Every word the name
    covers is in the pattern now, in both the noun and the verb forms the copy
    could reach for.

    AND THE BAN IS ANCHORED. `"order" not in text` also reds "reorder",
    "borderline" and "sort order", which is why the pattern below uses word
    boundaries and enumerates the inflections rather than relying on a stem:
    `\\border\\b` does not match "ordered". Two false positives survive that --
    "in order to" and "sort order" -- and are accepted, because neither belongs
    in customer copy on a page whose whole point is that this is not an order.

    Asserted over the rendered page rather than the template source, so a
    comment may still discuss "the order" while nothing shown to a customer
    does.
    """
    def _visible(page: str) -> str:
        parser = _VisibleText()
        parser.feed(page)
        return parser.text

    # THE BASELINE, described as what it is. An 'api' row renders none of the
    # strings this item added, so this is the rest of THIS TEMPLATE plus the
    # layout around it -- not "the chrome", which is what this comment used to
    # claim while rendering the whole of detail.html minus one panel. Narrower
    # than that in turn: the three conditional banners are absent from this row
    # as well, so a commercial word added to one of THEM reports on the second
    # render below and never here. What this line is good for is the one-way
    # reading -- whatever it catches, the panel did not introduce.
    baseline = _visible(_page(client, _row(
        submission_source="api", candidate_indices=[0])))
    assert not _COMMERCIAL.search(baseline), _COMMERCIAL.search(baseline)

    html = _page(client, _target_row(refs=[
        {"job_id": "j-bc", "index": 0}, {"job_id": "j-bc", "index": 0},
    ]), "?submitted=1&truncated=120&dropped=2")
    text = _visible(html)
    assert not _COMMERCIAL.search(text), _COMMERCIAL.search(text)
    # The fixture reaches every string this item added: the panel, its badge,
    # the advice, the reconciliation footnote and both banners.
    assert "Designs in this request" in text
    assert _ADVICE in re.sub(r"\s+", " ", text)
    assert "The submitted list held 2 entries" in re.sub(r"\s+", " ", text)


# ---------------------------------------------------------------------------
# PART 4: the browser half, EXECUTED
#
# Every other JS assertion in this repo is a source search, and the round that
# reviewed the first version of this feature demonstrated what that costs: two
# mutations to the one-shot -- replacing the marker read with a bare read, and
# turning `catch (_) { return; }` into `catch (_) {}` -- were predicted to go
# GREEN against the whole suite, and they did, because nothing executed the
# file. The property this item now delivers is a property of a RUN: the covered
# refs go, the rest stay, and a second run changes nothing.
#
# So this part drives the real file under `node` with a stubbed sessionStorage.
# It SKIPS where node is absent. WHETHER THAT INCLUDES CI IS NOT ESTABLISHED:
# .github/workflows installs no node, but a hosted runner image can still put
# one on PATH, and this file asserted the inference as fact until round 3. The
# cheap way to settle it is the skip count on a CI run of this branch -- 15 with
# node present, 23 without, because 8 tests here carry `@_needs_node`. The
# source-order assertions in PART 1 hold wherever node is missing, and each of
# them says so in its own docstring. A skipping test is worth having anyway:
# it is the only thing in this repo that can tell a working removal from a
# no-op, and it runs wherever a developer has node on PATH.
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")

# The three globals candidate_table.js touches at load: it registers a
# DOMContentLoaded listener, registers a pageshow listener, and assigns its four
# exports onto `window`. Nothing else runs until a listener fires, and the stubs
# below CAPTURE those listeners rather than dropping them, so a test can fire
# one. `window` IS globalThis so the exports land somewhere reachable.
_JS_STUBS = """
var __store = {};
var __listeners = {};
function __record(type, fn) {
  (__listeners[type] = __listeners[type] || []).push(fn);
}
globalThis.sessionStorage = {
  getItem: function (k) {
    return Object.prototype.hasOwnProperty.call(__store, k) ? __store[k] : null;
  },
  setItem: function (k, v) { __store[k] = String(v); },
  removeItem: function (k) { delete __store[k]; }
};
globalThis.document = {
  addEventListener: __record,
  querySelectorAll: function () { return []; },
  getElementById: function () { return null; }
};
globalThis.window = globalThis;
globalThis.addEventListener = __record;
"""


def _run_js_raw(tmp_path, driver: list):
    """Load the real candidate_table.js under the stubs, run ``driver``, and
    return the JSON object its last ``console.log`` printed."""
    script = tmp_path / "drive.js"
    script.write_text(
        _JS_STUBS + _JS_FILE.read_text(encoding="utf-8") + "\n"
        + "\n".join(driver) + "\n", encoding="utf-8")
    # `encoding` explicitly, not `text=True` alone: node writes UTF-8 and
    # Python would otherwise decode with the Windows ANSI codepage, which turns
    # the star glyphs this file compares into mojibake.
    out = subprocess.run(
        [_NODE, str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _run_js(tmp_path, seed_scope, stored, calls):
    """Seed ``shortlist_<seed_scope>`` with ``stored``, then apply each
    ``(scope, refs)`` in ``calls`` through ``window.dropShortlistRefs``.

    Returns ``(stored_after, writes)`` -- the parsed list still under the SEEDED
    key (``None`` if it is gone) and how many times ``setItem`` was called, so
    "removed nothing" can be told apart from "removed nothing but rewrote the
    value anyway". `loadShortlist` coerces as it reads, so an unconditional save
    would rewrite entries it was never asked to touch.
    """
    driver = [
        "var __key = 'shortlist_' + %s;" % json.dumps(seed_scope),
        "__store[__key] = %s;" % json.dumps(json.dumps(stored)),
        "var __writes = 0;",
        "var __set = sessionStorage.setItem;",
        "sessionStorage.setItem = function (k, v) { __writes++;"
        " return __set(k, v); };",
    ]
    for scope_arg, refs in calls:
        driver.append("window.dropShortlistRefs(%s, %s);" % (
            json.dumps(scope_arg), json.dumps(refs)))
    driver.append(
        "console.log(JSON.stringify({stored: __store[__key] === undefined"
        " ? null : JSON.parse(__store[__key]), writes: __writes}));")
    result = _run_js_raw(tmp_path, driver)
    return result["stored"], result["writes"]


_needs_node = pytest.mark.skipif(
    _NODE is None, reason="no node on PATH; the JS half is source-only here")


@_needs_node
def test_running_it_drops_the_covered_refs_and_keeps_the_remainder(tmp_path):
    """THE PROPERTY THE WHOLE REDESIGN EXISTS FOR, measured.

    The browser holds 620 stars. The request carried the first 500 and reported
    120 as `truncated`; the row cannot name those 120, so the payload is the
    500. What must survive is exactly the 120 -- and in their original order,
    because the next `openCampaignModal` serialises the stored list as it
    stands, so a reordering would change which designs the cap takes next time.

    Wiping the key returned an empty list here, which is the defect this
    replaced: the customer was left with zero stars and a scroll box of the 500
    that had already gone.
    """
    stored = [{"j": "j-bc", "i": i} for i in range(620)]
    covered = [{"job_id": "j-bc", "index": i} for i in range(500)]
    after, writes = _run_js(tmp_path, "t-1", stored, [("t-1", covered)])
    assert after == [{"j": "j-bc", "i": i} for i in range(500, 620)]
    assert writes == 1


@_needs_node
def test_running_it_twice_is_the_same_as_running_it_once(tmp_path):
    """IDEMPOTENCE, EXECUTED. `?submitted=1` is a permanent URL -- a reload, a
    bookmark, a history entry, a restored tab, a new tab session -- so the call
    runs an unbounded number of times and each run must be a no-op after the
    first. The one-shot marker existed to buy this and bought it only until the
    tab closed.

    The second run must also not WRITE: a re-save would rewrite the customer's
    stored list on every visit, and `loadShortlist` coerces as it reads, so an
    unconditional save would silently rewrite entries it was not asked to touch.
    """
    stored = [{"j": "j-bc", "i": 0}, {"j": "j-bc", "i": 5},
              {"j": "j-de", "i": 2}]
    covered = [{"job_id": "j-bc", "index": 0}]
    once, writes_once = _run_js(tmp_path, "t-1", stored, [("t-1", covered)])
    twice, writes_twice = _run_js(
        tmp_path, "t-1", stored, [("t-1", covered), ("t-1", covered)])
    assert once == [{"j": "j-bc", "i": 5}, {"j": "j-de", "i": 2}]
    assert twice == once
    assert (writes_once, writes_twice) == (1, 1)


@_needs_node
def test_a_star_added_after_the_submit_is_not_touched(tmp_path):
    """THE FLOW THE PAGE'S OWN COPY ASKS FOR, end to end. Submit, star the rest
    on the source page, come back to this URL to check what the first request
    covered. Under the old design the return trip was the danger and needed a
    marker to survive; under this one the new stars are simply not named.

    Driven as a SECOND call over the post-removal list, which is what a return
    visit actually is.
    """
    covered = [{"job_id": "j-bc", "index": 0}]
    after_first, _ = _run_js(
        tmp_path, "t-1", [{"j": "j-bc", "i": 0}], [("t-1", covered)])
    assert after_first == []
    # The customer now stars two more designs, then re-opens the same URL.
    restarred = [{"j": "j-de", "i": 7}, {"j": "j-de", "i": 8}]
    after_second, writes = _run_js(
        tmp_path, "t-1", restarred, [("t-1", covered)])
    assert after_second == restarred
    assert writes == 0


@_needs_node
def test_a_covered_design_still_starred_is_dropped_again(tmp_path):
    """THE ONE CASE THAT IS NOT A NO-OP, asserted rather than left to be
    discovered. A customer who re-stars a design this request already covered
    and then returns to the confirmation URL loses that star again.

    Accepted, not fixed: un-starring a design already sent to the lab is the
    defensible reading, and the alternative is a marker -- which is the
    machinery this redesign removed and which failed in a worse direction. Filed
    as register item A102 so it is a known cost rather than a surprise.
    """
    covered = [{"job_id": "j-bc", "index": 0}]
    after, writes = _run_js(
        tmp_path, "t-1", [{"j": "j-bc", "i": 0}, {"j": "j-de", "i": 1}],
        [("t-1", covered)])
    assert after == [{"j": "j-de", "i": 1}]
    assert writes == 1


@_needs_node
def test_a_falsy_or_wrong_scope_leaves_every_store_alone(tmp_path):
    """The two ways the payload can be aimed at the wrong list, EXECUTED --
    which is what PART 1 can only assert as source order.

    THE FALSY CASE IS CONSTRUCTED TO DISCRIMINATE, and the obvious construction
    does not. Seeding `shortlist_t-1` and calling with `""` leaves that key
    untouched whether or not the guard exists, because the empty scope resolves
    to a DIFFERENT key. So the bare `shortlist_` key is seeded instead, holding
    a star the payload names: with the guard nothing happens, without it the
    empty scope lands on exactly that key and filters it.

    The second half is the scope actually selecting a key at all -- a filter
    that matched on the index alone would strip "Candidate 1" out of every other
    page's selection.
    """
    named = [{"job_id": "j-bc", "index": 0}]
    star = [{"j": "j-bc", "i": 0}]
    bare, writes = _run_js(tmp_path, "", star, [("", named)])
    assert (bare, writes) == (star, 0)
    other, writes = _run_js(tmp_path, "t-1", star, [("t-2", named)])
    assert (other, writes) == (star, 0)


@_needs_node
def test_the_legacy_bare_index_shape_survives_rather_than_being_dropped(
        tmp_path):
    """`loadShortlist` coerces a bare int to `{j: null, i}`, a shape nothing
    writes today. It keys as "null#N" and matches no server ref, which is stated
    in the JS comment as a claim about behaviour -- so it is executed here
    rather than believed.

    A star left standing is the recoverable direction; the alternative reading,
    where "null#N" collided with a real ref, would drop a star belonging to a
    design the request never covered. The payload here names index 3, which is
    exactly the bare entry, so a match would show.

    AND THE STORED BYTES ARE UNTOUCHED, not merely equivalent. Because nothing
    was removed nothing is written, so the raw `3` survives as a `3` rather than
    being rewritten as the coerced `{j: null, i: 3}`. That is what the write
    count buys: an unconditional save would leave this list logically identical
    and physically rewritten on every visit to the confirmation URL.
    """
    after, writes = _run_js(tmp_path, "t-1", [3, {"j": "j-bc", "i": 0}], [
        ("t-1", [{"job_id": "j-bc", "index": 3}])])
    assert after == [3, {"j": "j-bc", "i": 0}]
    assert writes == 0


_PAGESHOW_DOM = """
var __tableListeners = 0;
var __btn = {
  dataset: {job: 'j-bc', refIdx: '0', idx: '0'},
  classList: {toggle: function (c, on) { __btn.starred = on; },
              add: function () {}, remove: function () {}},
  textContent: '\\u2605'
};
var __table = {
  querySelectorAll: function (sel) {
    return sel === '.star-btn' ? [__btn] : [];
  },
  addEventListener: function () { __tableListeners++; }
};
var __wrap = {dataset: {candTableId: 'tbl', scope: 't-1'}};
document.querySelectorAll = function (sel) {
  return sel === '[data-cand-table-id]' ? [__wrap] : [];
};
document.getElementById = function (id) { return id === 'tbl' ? __table : null; };
"""


@_needs_node
def test_a_bfcache_restore_repaints_the_stars_without_rebinding(tmp_path):
    """A results page restored from the back/forward cache keeps the DOM it was
    left with and `DOMContentLoaded` does not fire again, so a star painted
    before this feature pruned the store stays painted afterwards. Before this
    item nothing outside the results document ever wrote `shortlist_<scope>`, so
    the DOM and the store could not diverge; this change is what made the
    divergence possible.

    THREE PROPERTIES, and the middle one is why `initTable` is not reused.
    Firing `pageshow` with `persisted: false` -- an ordinary navigation, where
    `DOMContentLoaded` has already run -- must do nothing, or every normal load
    repaints twice. Firing it with `persisted: true` must repaint from the
    store. And neither may bind a listener: `initTable` attaches two click
    handlers to the table, so re-running it on every restore makes one star
    click toggle twice and then four times.
    """
    result = _run_js_raw(tmp_path, [
        _PAGESHOW_DOM,
        # The store no longer holds the star the DOM is showing.
        "__store['shortlist_t-1'] = '[]';",
        "var __show = __listeners['pageshow'] || [];",
        "__show.forEach(function (fn) { fn({persisted: false}); });",
        "var __afterPlain = __btn.starred;",
        "__show.forEach(function (fn) { fn({persisted: true}); });",
        "console.log(JSON.stringify({handlers: __show.length,"
        " afterPlain: __afterPlain === undefined ? null : __afterPlain,"
        " starred: __btn.starred, glyph: __btn.textContent,"
        " bound: __tableListeners}));",
    ])
    assert result["handlers"] == 1
    # An ordinary navigation left the button untouched: `classList.toggle` was
    # never called, so the recorder is still unset.
    assert result["afterPlain"] is None
    # The restore repainted it against the pruned store.
    assert result["starred"] is False
    assert result["glyph"] == "☆"
    assert result["bound"] == 0


@_needs_node
def test_the_stub_harness_can_observe_a_removal_at_all(tmp_path):
    """THE FIXTURE'S OWN ASSUMPTION. Every assertion above is over a store this
    file stubs, so a harness whose `setItem` never reached `__store` -- or whose
    `window` was not where the exports landed -- would report "nothing removed"
    for every case and read as six passes.

    Driven as the one unambiguous removal: one stored star, that same star
    named, empty list left behind and exactly one write.
    """
    after, writes = _run_js(tmp_path, "t-1", [{"j": "j-bc", "i": 0}], [
        ("t-1", [{"job_id": "j-bc", "index": 0}])])
    assert (after, writes) == ([], 1)
