"""A refused CSV download must SAY why, on the page the user is looking at.

The two Scout download surfaces are plain ``<a download>`` anchors, so the
browser -- not the page -- handles the response. When the server refuses, the
``error`` string it composes reaches nobody: Chrome writes no file and logs a
failed download in a UI the user is not looking at, Firefox saves the JSON
error body to disk AS the ``.csv``. Both routes have refusals a stale tab hits
on an ordinary path, because jobs reap after an hour (``scout/jobs.py``
``cleanup_old_jobs``):

* ``/scout/download/<job_id>`` (``templates/scout/index.html``, two anchors) --
  "Results not found. Please run analysis first."
* ``/scout/feasibility/download/<job_id>`` (``templates/scout/feasibility.html``)
  -- the same, plus ``@login_required``.

DELETING THE ``download`` ATTRIBUTE IS NOT THE FIX and was reverted (``97c275e``
on ``claude/fervent-engelbart-5f2fbd``). The HTML download algorithm consults
``Content-Disposition`` BEFORE the attribute, so removing it changes no
filename -- it only turns a refusal into a full navigation that throws the user
off the rendered report and the live 3Dmol viewer.

THE PROPERTY THAT MATTERS, and the one a naive ``!r.ok`` check gets wrong:
``login_required`` (``shared/auth.py::login_required``) answers a dead session
with a 302 to ``/login``, and ``fetch`` FOLLOWS redirects by default, yielding a
200 HTML page. ``r.ok`` is then TRUE, so an ok/not-ok guard hands the browser
the login page to save as a ``.csv`` -- the same class of silent wrong file the
guard exists to prevent.
``test_a_followed_login_redirect_is_never_treated_as_a_file`` holds that up.

A HEAD probe was considered and rejected: HEAD strips the body, which is the
``error`` string this whole change exists to surface.

The surface is RETIRED as well as written -- the guard clears it at the start
of every click -- so what is on screen always describes the click the user
just made, not one from before they fixed the cause.

The guard ships as ``static/js/scout-download.js`` and is exercised here by
running that file under node against a stubbed anchor and ``fetch``
(``tests/js/scout_download_harness.cjs``), the same way
``tests/test_scout_refusal_cta.py`` runs the real refusal block. Assertions are
on emitted behaviour: what the user was told, and whether a download ran.

    pytest tests/test_scout_download_refusals.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "static" / "js" / "scout-download.js"
HARNESS = REPO_ROOT / "tests" / "js" / "scout_download_harness.cjs"
TEMPLATES = {
    "index": REPO_ROOT / "templates" / "scout" / "index.html",
    "feasibility": REPO_ROOT / "templates" / "scout" / "feasibility.html",
}

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

REFUSALS = [
    "job_gone",
    "chain_mismatch",
    "session_expired",
    "non_json_refusal",
    "json_without_error",
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
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


# ----------------------------------------------------------------------
# The refusal reaches the user
# ----------------------------------------------------------------------


@needs_node
@pytest.mark.parametrize(
    "case,expected",
    [
        ("job_gone", "Results not found. Please run analysis first."),
        ("chain_mismatch", "These feasibility results are for chain B."),
    ],
)
def test_the_servers_own_error_string_is_what_the_user_reads(results, case, expected):
    """Verbatim, not a house message: these routes compose distinct refusals and
    collapsing them into one string is most of the invisibility being fixed."""
    assert results[case]["errors"] == [expected], results[case]


@needs_node
@pytest.mark.parametrize("case", REFUSALS)
def test_a_refusal_never_downloads_a_file(results, case):
    assert results[case]["downloads"] == 0, (
        f"{case}: the browser was still handed something to save"
    )


@needs_node
@pytest.mark.parametrize("case", REFUSALS)
def test_no_refusal_is_silent(results, case):
    """The failure mode being fixed is silence, so an empty message is the
    defect again regardless of which branch produced it."""
    msgs = results[case]["errors"]
    assert len(msgs) == 1 and msgs[0].strip(), f"{case}: {msgs!r}"


@needs_node
def test_a_followed_login_redirect_is_never_treated_as_a_file(results):
    """The 200-OK failure. fetch follows login_required's 302 to /login, so
    ``r.ok`` is true and the body is HTML; saving it writes the login page to
    disk as a .csv."""
    got = results["session_expired"]
    assert got["downloads"] == 0, "the login page was handed over as a CSV"
    assert "sign in" in got["errors"][0].lower(), (
        f"a dead session must say how to fix it, got {got['errors']!r}"
    )


# ----------------------------------------------------------------------
# ...and a clean response still downloads
# ----------------------------------------------------------------------


@needs_node
def test_a_clean_response_still_downloads(results):
    """The guard sits in front of a working feature; breaking the happy path
    would be a worse bug than the one it fixes."""
    assert results["ok"]["downloads"] == 1, results["ok"]
    assert results["ok"]["errors"] == [], results["ok"]
    assert results["ok"]["visible"] is None, results["ok"]


@needs_node
def test_the_probe_does_not_recurse_into_itself(results):
    """The guard downloads by re-clicking its own anchor, which re-enters the
    handler. Without the passthrough flag that is either an infinite loop or a
    second probe for every download."""
    assert results["ok"]["fetches"] == 1, results["ok"]


@needs_node
def test_re_rendering_results_does_not_stack_probes(results):
    """Both pages rebind on every render; ``onclick =`` replaces, where
    addEventListener would accumulate one probe -- and one error -- per render."""
    assert results["rebound"]["fetches"] == 1, results["rebound"]
    assert results["rebound"]["downloads"] == 1, results["rebound"]


@needs_node
def test_a_successful_probe_releases_the_body(results):
    """The probe downloads the whole CSV and reads none of it, then the
    passthrough click fetches the same file again. Cancelling the stream ends
    the first transfer; leaving it open pays for the file twice, which the
    all-patches CSV is big enough to notice."""
    got = results["ok_with_body"]
    assert got["downloads"] == 1, got
    assert got["bodyCancels"] == 1, f"the probe held the response body open: {got}"


@needs_node
@pytest.mark.parametrize("name", ["rebound_mid_probe", "session_expired"])
def test_a_discarded_probe_body_is_released_too(name, results):
    """The r.ok branch is not the only one that discards the body. A probe
    whose anchor moved mid-flight, and one that followed login_required's 302,
    both return without reading it; on the all-patches CSV that leaves a
    transfer running that nobody will ever read."""
    got = results[name]
    assert got["bodyCancels"] == 1, f"{name}: the probe held the body open: {got}"


@needs_node
def test_a_rebind_mid_probe_never_hands_over_the_new_file(results):
    """Analysing another chain re-renders and repoints the anchor
    (``templates/scout/index.html:412`` ``_handleAnalysisResult``). A probe
    still in flight answers for the file that was clicked, so passing it
    through saves a DIFFERENT chain's CSV under the name the user asked for --
    silently, and looking exactly like the file they wanted."""
    got = results["rebound_mid_probe"]
    assert got["downloaded"] == [], (
        f"the anchor moved mid-probe and the guard still saved {got['downloaded']}"
    )
    assert got["visible"] is None, got


@needs_node
def test_a_rebind_mid_probe_still_downloads_once(results):
    """Same file, new handler. The render that rebinds retires the closure the
    probe is waiting in, so a passthrough flag kept there is set on a function
    nothing will call again and the click re-enters a handler that probes all
    over instead of downloading."""
    got = results["rebound_mid_probe_same_href"]
    assert got["downloads"] == 1, got
    assert got["fetches"] == 1, f"the retired handler re-probed: {got}"


@needs_node
def test_the_probe_sends_the_session_cookie(results):
    """Without it the feasibility probe is anonymous, and every signed-in user
    is told their session expired."""
    assert (results["init"] or {}).get("credentials") == "same-origin", results["init"]


# ----------------------------------------------------------------------
# Every download anchor on both pages is actually guarded
# ----------------------------------------------------------------------
# Without these, the JS above can be perfect while the product is unchanged: an
# unbound anchor, or a page that never loads the script, is the whole defect
# back again.


def _anchor_ids(src: str) -> list[str]:
    ids = []
    for tag in re.findall(r"<a\b[^>]*>", src, re.S):
        # Matches the ATTRIBUTE, valued or bare. A "download=" substring test
        # skips a future <a download>, the very anchor this guard exists to
        # catch; a plain word-boundary match hits /scout/download/ in every href.
        if not re.search(r"\sdownload(?=[\s=>])", tag):
            continue
        m = re.search(r'id="([^"]+)"', tag)
        # Dropping an id-less anchor would certify it bound -- the same
        # vacuity the attribute match above avoids. Yield the raw tag so the
        # bind test fails naming the markup that has no id to look up.
        ids.append(m.group(1) if m else tag)
    return ids


def test_the_anchor_scan_sees_a_bare_download_attribute():
    # The two tests below are only as good as this scan: an anchor it skips
    # is an anchor it certifies as bound. A bare `download` is legal, and
    # likely here, because Content-Disposition -- not the attribute -- is
    # what names the file (see static/js/scout-download.js).
    assert _anchor_ids('<a id="bare" download>CSV</a>') == ["bare"]
    assert _anchor_ids('<a id="val" download="x.csv">CSV</a>') == ["val"]
    assert _anchor_ids('<a id="off" href="/scout/download/1">CSV</a>') == []
    assert _anchor_ids('<a href="/x" download>CSV</a>') == ['<a href="/x" download>']


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_page_loads_the_guard(name):
    src = TEMPLATES[name].read_text(encoding="utf-8")
    assert "js/scout-download.js" in src, (
        f"{name} has download anchors but never loads the guard"
    )


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_download_anchor_is_bound_to_the_guard(name):
    src = TEMPLATES[name].read_text(encoding="utf-8")
    anchors = _anchor_ids(src)
    assert anchors, f"{name}: no download anchors found -- did the markup move?"
    for anchor_id in anchors:
        var = re.search(
            r"var\s+(\w+)\s*=\s*document\.getElementById\(\s*'"
            + re.escape(anchor_id)
            + r"'\s*\)",
            src,
        )
        assert var, f"{name}: #{anchor_id} is never read into a var to bind"
        assert re.search(r"bindGuardedDownload\(\s*" + var.group(1) + r"\b", src), (
            f"{name}: #{anchor_id} is an unguarded download link"
        )


# ----------------------------------------------------------------------
# ...and the refusal it shows lands somewhere the user can SEE
# ----------------------------------------------------------------------
# Binding the guard is not enough, and on NEITHER page is #analyze-error a
# place to put this. feasibility.html's auto-load path -- how a user arrives
# from an epitope card on /scout/ -- hides the whole Input panel
# (`inputPanel.hidden = true`), and static/style.css's
# `[hidden] { display: none !important; }` beats the inline display a message
# handler sets. showDownloadError wrote into that panel, so on the main entry
# path the refusal was painted into a display:none subtree and scrollIntoView
# had nothing to scroll to: guard bound, message invisible, defect unchanged.

_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def _ancestor_ids(src: str, target: str) -> list[str] | None:
    """ids of the elements enclosing #target, or None if it is not there."""

    class Walk(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.found: list[str] | None = None

        def handle_starttag(self, tag, attrs):
            ids = dict(attrs).get("id") or ""
            if ids == target:
                self.found = [i for i in self.stack if i]
            if tag not in _VOID:
                self.stack.append(ids)

        def handle_endtag(self, tag):
            if tag not in _VOID and self.stack:
                self.stack.pop()

    walk = Walk()
    walk.feed(src)
    return walk.found


def _show_download_error(src: str) -> str:
    m = re.search(r"function showDownloadError\(msg\)\s*\{.*?\n\s*\}", src, re.S)
    assert m, "showDownloadError moved or was renamed"
    return m.group(0)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_a_refusal_is_shown_inside_the_results(name):
    src = TEMPLATES[name].read_text(encoding="utf-8")
    target = re.search(r"getElementById\('([^']+)'\)", _show_download_error(src))
    assert target, f"{name}: showDownloadError writes to no element"
    target = target.group(1)

    # Controls on the walk itself. A parse that nested everything, or
    # nothing, would certify whatever target it was handed; these two fail
    # first, and in opposite directions, if it has drifted.
    assert "results-section" in (_ancestor_ids(src, "viewer-container") or [])
    assert "results-section" not in (_ancestor_ids(src, "analyze-error") or [])

    chain = _ancestor_ids(src, target)
    assert chain is not None, (
        f"{name}: showDownloadError writes to #{target}, which is not in the markup"
    )
    assert "results-section" in chain, (
        f"{name}: showDownloadError writes to #{target}, which is outside "
        "#results-section -- the only subtree guaranteed visible "
        "whenever the download button is"
    )


# ----------------------------------------------------------------------
# ...and it is TAKEN BACK when it stops being true
# ----------------------------------------------------------------------
# The guard only ever added messages. A session that expires, is signed
# into again in a second tab, and then downloads, left the file arriving
# under 'Your session has expired.' -- so the surface has to be retired,
# which means the page must treat an empty message as 'hide', not as
# 'paint nothing'.

_DRIVER = """\
// Drives the page's OWN showDownloadError against a stub element, so the
// falsy-message contract is checked as behaviour on the shipped source
// rather than as a match on how it happens to be spelled.
// Starts HIDDEN, as both surfaces ship in the markup
// (`<p id="download-error" class="error-message" aria-live="polite" hidden>`).
// A stub that started visible would certify any write order at all.
const el = {
  _text: '',
  _hidden: true,
  _liveAtWrite: null,
  style: {},
  _scrolled: false,
  get textContent() { return this._text; },
  set textContent(v) {
    this._liveAtWrite = !this._hidden && this.style.display !== 'none';
    this._text = v;
  },
  get hidden() { return this._hidden; },
  set hidden(v) { this._hidden = v; },
  scrollIntoView() { el._scrolled = true; },
};
global.document = { getElementById: () => el };

__BODY__

const seen = [];
for (const msg of ['Results not found. Please run analysis first.', '']) {
  el._scrolled = false;
  el._liveAtWrite = null;
  showDownloadError(msg);
  // Whatever the page uses to hide it -- the hidden attribute, a display
  // style, or both -- this is what the user can see.
  seen.push({
    text: el.textContent,
    rendered: !el.hidden && el.style.display !== 'none',
    scrolled: el._scrolled,
    liveAtWrite: el._liveAtWrite,
  });
}
process.stdout.write(JSON.stringify(seen));
"""


@needs_node
def test_a_success_after_a_refusal_retires_the_message(results):
    got = results["refusal_then_success"]
    assert got["downloads"] == 1, got
    assert got["errors"] == [
        "Your session has expired. Please sign in again to download."
    ], got
    assert got["visible"] is None, (
        f"the download succeeded but the page still reads {got['visible']!r}"
    )


def _run_show_download_error(name: str, tmp_path) -> tuple[dict, dict]:
    """Runs the page's own showDownloadError under node -- a refusal, then a
    clear -- and returns what the user could see after each."""
    driver = tmp_path / "show_download_error.cjs"
    body = _show_download_error(TEMPLATES[name].read_text(encoding="utf-8"))
    driver.write_bytes(_DRIVER.replace("__BODY__", body).encode("utf-8"))
    proc = subprocess.run(
        ["node", str(driver)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"{name}:\n{proc.stderr}"
    shown, cleared = json.loads(proc.stdout)
    return shown, cleared


@needs_node
@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_an_empty_message_hides_the_surface(name, tmp_path):
    """The guard clears the surface at the start of every click, so a
    falsy message has to RETIRE it. A page that paints unconditionally
    shows an empty error box on every download instead."""
    shown, cleared = _run_show_download_error(name, tmp_path)

    assert shown["rendered"], f"{name}: a refusal is not visible at all"
    assert shown["text"] == "Results not found. Please run analysis first."
    assert shown["scrolled"], f"{name}: the refusal is never brought into view"
    assert not cleared["rendered"], (
        f"{name}: an empty message leaves the surface on screen -- a "
        "stale refusal survives the download that fixes it"
    )


@needs_node
@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_refusal_is_announced_not_just_painted(name, tmp_path):
    """Both surfaces are ``aria-live`` regions, and aria-live announces a
    CHANGE to a region that is in the accessibility tree. Both ship hidden, so
    writing the text first and unhiding second makes the refusal the region's
    initial content: on screen for everyone else, silent for a screen reader.
    That is this whole defect again, for the users least placed to notice a
    download that quietly did not happen."""
    shown, _ = _run_show_download_error(name, tmp_path)
    assert shown["liveAtWrite"], (
        f"{name}: the message is written while the surface is still hidden, so "
        "aria-live has no change to announce -- unhide first"
    )
