"""A refused AF2 download must not throw the user off the results page.

``templates/tools/af2_results.html`` offers two downloads -- the predicted
structure (``jobs.af2_download_pdb``) and the PAE matrix
(``jobs.af2_download_pae``). Both are plain anchors with no ``download``
attribute, and both refuse without a ``Content-Disposition``:

* 404 ``text/plain`` -- the key is missing from ``job.result``
* 500 ``text/plain`` -- the base64 does not decode
* 302 to ``/login`` -- ``@login_required`` on a dead session
* 404 ``text/html`` -- ``render_template("404.html")`` on someone else's job,
  or on a job that is not an af2 job

A click therefore NAVIGATES. The two text ones at least say something, as a
bare comment line on a blank page, but the rendered results, the metrics table
and the live NGL viewer are gone and the only way back is the Back button.

THE ONE THAT SAYS NOTHING AT ALL is the redirect, and it is the one a stale
tab gets: ``login_required`` (``shared/auth.py``) answers a dead session with
a 302 to ``/login?next=...``, so the click becomes a login form with nothing
on it about a download. That is the same invisibility ``#281`` fixed for
Scout, not a milder cousin of it.

The fix reuses that guard, ``static/js/scout-download.js``, rather than adding
a second one. It already carries the lessons this page needs -- ``fetch``
follows the 302 so ``r.ok`` is a false pass, the passthrough flag lives on the
element, the href is re-checked on resolve. Only the message-extraction branch
had to grow: Scout's routes compose JSON ``{"error": ...}``, these compose
``text/plain``. A response that declares no content-type still takes the JSON
branch, which is what leaves Scout's behaviour unchanged; the assertions on
that half stay in ``tests/test_scout_download_refusals.py``, which drives the
same harness. This file asserts only the branch AF2 added.

Deleting the anchors' ``download`` attribute is not on the table here -- they
have none. Adding one would not help either: ``Content-Disposition`` is what
names the file, and the attribute does nothing about a refusal.

    pytest tests/test_af2_download_refusals.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "static" / "js" / "scout-download.js"
HARNESS = REPO_ROOT / "tests" / "js" / "scout_download_harness.cjs"
TEMPLATE = REPO_ROOT / "templates" / "tools" / "af2_results.html"
STYLESHEET = REPO_ROOT / "static" / "style.css"
SURFACE_ID = "af2-download-error"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

# Every refusal shape these two routes can answer with, plus the offline case.
REFUSALS = [
    "text_refusal",
    "text_refusal_pae",
    "text_refusal_blank",
    "html_refusal",
    "session_expired",
    "network",
]


@pytest.fixture(scope="module")
def results() -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS), str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, "harness failed:\n" + proc.stderr
    return json.loads(proc.stdout)


# ----------------------------------------------------------------------
# The text/plain refusal reaches the user, on the page
# ----------------------------------------------------------------------


@needs_node
@pytest.mark.parametrize(
    "case,expected",
    [
        ("text_refusal", "No PDB in this job's result."),
        ("text_refusal_pae", "Malformed PAE payload."),
    ],
)
def test_the_servers_own_text_is_what_the_user_reads(results, case, expected):
    """Verbatim, minus the leading marker. These routes write the body as a
    shell comment because it was built to be saved to disk; on a page it is
    the sentence that matters, and collapsing the two into one house string
    would lose which of the two files went missing."""
    assert results[case]["errors"] == [expected], results[case]


@needs_node
def test_a_body_with_no_words_left_is_never_shown_as_an_empty_box(results):
    """A body that is nothing but the marker strips to ''. Showing that paints
    an empty error box, which reads as a click that did nothing -- the silence
    the guard exists to end -- so it has to fall back to a real sentence."""
    shown = results["text_refusal_blank"]["errors"]
    assert len(shown) == 1 and shown[0].strip(), shown
    assert shown[0].strip() != "#", shown


@needs_node
def test_an_html_refusal_is_not_quoted_at_the_user(results):
    """``render_template("404.html")`` on another user's job is a whole HTML
    document. Only text/plain is ours to read out; anything else still has to
    say something, but not by reciting markup."""
    shown = results["html_refusal"]["errors"]
    assert len(shown) == 1 and shown[0].strip(), shown
    assert "<" not in shown[0], shown


@needs_node
@pytest.mark.parametrize("case", REFUSALS)
def test_a_refusal_never_downloads_and_is_never_silent(results, case):
    got = results[case]
    assert got["downloads"] == 0, f"{case}: something was still handed over"
    assert len(got["errors"]) == 1 and got["errors"][0].strip(), (
        f"{case}: {got['errors']!r}"
    )


@needs_node
def test_a_dead_session_says_so_instead_of_becoming_a_login_form(results):
    """The refusal this page could not show at all. fetch follows the 302 to
    /login, so ``r.ok`` is TRUE and an ok/not-ok guard would hand the browser
    the login page; unguarded, the click navigates there and the user is
    looking at a sign-in form with no idea their download was refused."""
    got = results["session_expired"]
    assert got["downloads"] == 0, got
    assert "sign in" in got["errors"][0].lower(), got["errors"]


@needs_node
def test_a_text_plain_body_on_a_good_response_is_still_a_download(results):
    """The content-type is read only after ok-ness has been decided. A guard
    that sniffed it first would turn a successful download served as text into
    an error message and no file."""
    got = results["text_ok"]
    assert got["downloads"] == 1, got
    assert got["errors"] == [], got


# ----------------------------------------------------------------------
# ...and the page actually uses the guard
# ----------------------------------------------------------------------
# Without these the JS above can be perfect while the page is unchanged: an
# unbound anchor, or a page that never loads the script, is the whole defect
# back. The anchor list is DERIVED from the template rather than written out
# here, so a third af2 download link added later is caught rather than assumed.


def _af2_download_anchors(src: str) -> dict[str, str]:
    """{anchor id: tag} for every anchor pointing at an af2 download route."""
    found = {}
    for tag in re.findall("<a [^>]*>", src, re.S):
        if "jobs.af2_download" not in tag:
            continue
        m = re.search('id="([^"]+)"', tag)
        # An id-less anchor is yielded under its own markup, so the bind test
        # below fails naming it rather than skipping it and certifying it
        # bound -- there is no id to look it up by, which is the defect.
        found[m.group(1) if m else tag] = tag
    return found


def test_the_anchor_scan_finds_the_routes_it_claims_to():
    # The three tests below are only as good as this scan.
    pdb = "<a id=\"p\" href=\"{{ url_for('jobs.af2_download_pdb') }}\">x</a>"
    assert list(_af2_download_anchors(pdb)) == ["p"]
    assert _af2_download_anchors('<a id="c" href="/clone">x</a>') == {}
    bare = "<a href=\"{{ url_for('jobs.af2_download_pae') }}\">x</a>"
    assert list(_af2_download_anchors(bare)) == [bare[: bare.index(">") + 1]]


def test_both_download_routes_are_still_on_the_page():
    """If a route loses its anchor this file is testing nothing; if one gains
    an anchor, the bind test below must see it."""
    src = TEMPLATE.read_text(encoding="utf-8")
    missing = [
        r
        for r in ("jobs.af2_download_pdb", "jobs.af2_download_pae")
        if r not in src
    ]
    assert not missing, missing
    anchors = _af2_download_anchors(src)
    assert len(anchors) == 2, anchors


def test_the_page_loads_the_guard():
    src = TEMPLATE.read_text(encoding="utf-8")
    assert "js/scout-download.js" in src, (
        "af2_results.html has download anchors but never loads the guard"
    )


@pytest.mark.parametrize(
    "anchor_id",
    sorted(_af2_download_anchors(TEMPLATE.read_text(encoding="utf-8"))),
)
def test_every_af2_download_anchor_is_bound_to_the_guard(anchor_id):
    src = TEMPLATE.read_text(encoding="utf-8")
    var = re.search(
        "var ([A-Za-z_][A-Za-z0-9_]*) = "
        + re.escape("document.getElementById('" + anchor_id + "')"),
        src,
    )
    assert var, anchor_id + " is never read into a var to bind"
    assert re.search(
        re.escape("bindGuardedDownload(" + var.group(1)) + "[,)]", src
    ), anchor_id + " is an unguarded download link"


# ----------------------------------------------------------------------
# ...and the refusal lands somewhere the user can SEE
# ----------------------------------------------------------------------


def test_the_surface_the_script_writes_to_is_the_one_in_the_markup():
    """Two copies of one id. A typo in either binds the guard to an element
    that is not there and the message goes nowhere, exactly as if nothing had
    been fixed."""
    src = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="' + SURFACE_ID + '"' in src, "no error surface in the markup"
    assert "document.getElementById('" + SURFACE_ID + "')" in src, (
        "the script never looks the surface up"
    )


def test_the_surface_is_unhidden_before_it_is_written():
    """aria-live announces a CHANGE to a region already in the tree, and this
    surface ships ``hidden``, so writing the text first and unhiding second
    makes the refusal the region's initial content: on screen for everyone
    else, silent for a screen reader -- this whole defect again, for the
    users least placed to notice a download that did not happen. Scout holds
    the order with a node run
    (``tests/test_scout_download_refusals.py``
    ``test_the_refusal_is_announced_not_just_painted``); this reads the source
    order, which is what a later tidy-up would reverse."""
    src = TEMPLATE.read_text(encoding='utf-8')
    parts = src.split('function showDownloadError(msg) {', 1)
    assert len(parts) == 2, 'showDownloadError moved or was renamed'
    body = parts[1].split('}', 1)[0]
    unhidden, written = body.find('.hidden'), body.find('.textContent')
    assert unhidden != -1 and written != -1, body
    assert unhidden < written, (
        'the message is written while the surface is still hidden, so '
        'aria-live has no change to announce -- unhide first'
    )


def test_the_surface_is_styled_by_the_stylesheet_this_page_loads():
    """Why this page cannot borrow Scout's ``.error-message``: that class
    ships in Scout's own stylesheet and has NO rule in ``static/style.css``,
    which with ``wallet.css`` is all ``templates/base.html`` loads here. It
    sets colour, border and padding and no ``display``, so the refusal would
    still be read -- as unstyled body text, with nothing marking it as an
    error. This reads the class the markup actually carries, so a surface
    restyled into a class this page does not load fails here."""
    src = TEMPLATE.read_text(encoding="utf-8")
    tag = re.search(
        "<p [^>]*" + re.escape('id="' + SURFACE_ID + '"') + "[^>]*>",
        src,
        re.S,
    )
    assert tag, "the surface is not the <p> this test expects"
    classes = re.search('class="([^"]*)"', tag.group(0))
    assert classes, "the surface carries no class at all"
    css = STYLESHEET.read_text(encoding="utf-8")
    for cls in classes.group(1).split():
        assert re.search(re.escape("." + cls) + " *[,{]", css), (
            "." + cls + " has no rule in static/style.css -- the refusal "
            "would paint invisible"
        )
