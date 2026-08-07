"""The five ``?handoff=`` banners on /jobs/<id>, RENDERED.

The third sibling of ``tests/test_target_handoff_banners.py`` and
``tests/test_run_handoff_banners.py``. ``blueprints/jobs.py`` whitelists the
five reasons and hands the survivor to ``templates/job_detail.html``, which
renders the shared copy with this page's noun. Before register item A91 this
page had no banner at all: the legacy single-job shortlist is starred and
submitted from this page's own results table, and every way that submit could
refuse ended somewhere other than here. Which exits send a reason is written in
``blueprints/lab_projects.py`` and is not this file's subject; what this file
holds is that a reason arriving here renders this page's own wording, and that
an unrecognised one renders nothing.

WHY THE RENDERED TEXT AND NOT THE TEMPLATE SOURCE. A source-substring assertion
cannot tell which Jinja branch it matched, or whether any branch was taken at
all; every sentence below lives in the shared partial unconditionally. So the
page is served through the real route, the response HTML is parsed, and the
assertions read the VISIBLE TEXT.

THE PARTIAL'S NOUN IS WHY THIS PAGE NEEDED MORE THAN A COPY OF THE SIBLING.
``rejected`` and ``unverified`` name the page they are on. The noun was a
two-arm ``{% if parent == 'run' %}...{% else %}`` for as long as there were two
callers, so this page's caller would have taken the else arm: rendered with
``parent='job'`` before the fix, the sentence came out byte-identical to the
target page's, on a page with no target. It is a lookup with a noun-free default
now, and both halves of that are pinned below: each known noun renders its own
wording, and an unknown one renders none of the three.
"""

from __future__ import annotations

import re
import uuid
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

from blueprints.jobs import JOB_HANDOFF_REASONS

_JID = str(uuid.uuid4())

# The one sentence per reason that no other reason says.
_DISTINGUISHING = {
    "none":       "arrived with no designs in it",
    "noname":     "needs a name for the target",
    "rejected":   "none of them could be matched to a design this job produced",
    "unverified": "could not confirm that the designs you starred belong to this job",
    "failed":     "Your request could not be submitted",
}


def test_every_whitelisted_reason_has_a_sentence_of_its_own():
    """THE COUPLING, asserted rather than assumed.

    Set equality in BOTH directions. A missing key would let a sixth reason
    added to the route fall through to the partial's ``{% else %}`` arm and tell
    the user "your request could not be submitted" for an unrelated cause, with
    the whole suite green -- which ``blueprints/targets.py`` records having paid
    for twice. An extra key would let this file test a banner the route can
    never produce, which is how a dead branch survives a rename.

    ``_DISTINGUISHING["failed"]`` is deliberately the ``{% else %}`` arm's own
    sentence, so a sixth reason is INDISTINGUISHABLE from ``failed`` at the
    template. That is why the guard has to live here, at the key set, rather
    than in an assertion about rendered text.
    """
    assert set(_DISTINGUISHING) == set(JOB_HANDOFF_REASONS), (
        sorted(set(_DISTINGUISHING) ^ set(JOB_HANDOFF_REASONS)))


class _Text(HTMLParser):
    """Visible text of a page, with <script>/<style> bodies excluded.

    Also collects each ``<p>``'s own text, which is what lets an assertion be
    scoped to the handoff sentence rather than to the whole page.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: list = []
        self._para: list = []
        self._in_p = 0
        self.paragraphs: list = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "p":
            self._in_p += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "p" and self._in_p:
            self._in_p -= 1
            if not self._in_p:
                self.paragraphs.append(re.sub(r"\s+", " ", "".join(self._para)))
                self._para = []

    def handle_data(self, data):
        if self._skip:
            return
        self._parts.append(data)
        if self._in_p:
            self._para.append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts))


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _ctx(user_id="u-1"):
    return SimpleNamespace(
        user_id=user_id, tier="free", balance=100, email="u@example.com",
    )


def _job():
    """The minimal object ``job_detail.html`` reads, in a NON-TERMINAL state.

    ``status='running'`` keeps the results container empty, which is the point:
    the banners under test sit above it and must not depend on the candidate
    table, the part of this page most likely to change shape. It also keeps the
    route off its ``succeeded`` branch, which resolves user metadata for the
    share button through a second read.
    """
    return SimpleNamespace(
        id=_JID,
        tool="esmfold",
        preset="pilot",
        status="running",
        created_at="2026-01-01T00:00:00+00:00",
        inputs={},
        result=None,
        error=None,
        gpu_seconds_used=None,
    )


def _render_job_page(client, query="", *, paragraphs=False):
    """GET /jobs/<id><query> and return its VISIBLE text, or -- with
    ``paragraphs=True`` -- the text of each ``<p>`` on the page.

    ``get_job`` is patched where ``blueprints.jobs`` bound it. The route reads
    its job through that two-outcome call and answers None with 404.html, so
    this helper hands back a job and asserts a 200.
    """
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.jobs.load_user_context", return_value=_ctx()), \
            patch("blueprints.jobs.get_job", return_value=_job()):
        resp = client.get(f"/jobs/{_JID}{query}")
    assert resp.status_code == 200, resp.status_code
    parser = _Text()
    parser.feed(resp.get_data(as_text=True))
    return parser.paragraphs if paragraphs else parser.text


def _assert_only(text: str, reason: str):
    """The reason's own sentence is rendered and no other reason's is."""
    assert _DISTINGUISHING[reason] in text, (
        f"?handoff={reason} rendered no banner of its own"
    )
    for other, sentence in _DISTINGUISHING.items():
        if other == reason:
            continue
        assert sentence not in text, (
            f"?handoff={reason} also rendered the {other} banner"
        )


@pytest.mark.parametrize("reason", sorted(_DISTINGUISHING))
def test_each_handoff_reason_renders_its_own_banner(client, reason):
    """The five reasons, one at a time, on the real page. Parametrised so a
    reason whose branch is deleted or reordered fails here instead of silently
    falling through to the ``{% else %}`` arm."""
    _assert_only(_render_job_page(client, f"?handoff={reason}"), reason)


def test_the_job_page_carries_no_banner_without_a_handoff_reason(client):
    """THE PAIR for all five. Every sentence above lives in the partial
    unconditionally, so a test that only ever renders WITH a reason cannot tell
    a working ``{% if handoff %}`` from a block that always draws."""
    text = _render_job_page(client)
    for sentence in _DISTINGUISHING.values():
        assert sentence not in text


def test_a_crafted_handoff_value_renders_nothing(client):
    """The whitelist in ``blueprints/jobs.py``. ``handoff`` arrives from a query
    string, so an unknown value must render no banner at all rather than an
    empty alert or the ``{% else %}`` failure text -- which would let any link
    make a user believe their submission failed."""
    text = _render_job_page(client, "?handoff=' or 1=1--")
    for sentence in _DISTINGUISHING.values():
        assert sentence not in text


def test_the_rejected_banner_does_not_tell_the_user_to_retry(client):
    """Permanent against transient, as behaviour rather than as a difference in
    wording. ``none`` means the POST carried no designs and retrying is right;
    ``rejected`` means every design it carried was refused by a check that ran,
    so the SAME refs will be refused identically and telling that user to press
    the button again is telling them to repeat a no-op."""
    rejected = _render_job_page(client, "?handoff=rejected")
    none = _render_job_page(client, "?handoff=none")
    assert "Try the button again" in none
    assert "Try the button again" not in rejected
    assert "will be refused the same way" in rejected


def test_the_unverified_banner_names_no_particular_read(client):
    """The sentence names the page and no cause, and both halves are asserted.

    The copy is ``templates/components/lab_handoff_banner.html``, shared with
    the target and run pages, so a wording that named one particular read would
    reach this page too. The two absence assertions are the two such wordings
    that existed: the run page's old "could not read every sub-job your starred
    designs came from" and the target page's old "the full list of runs on this
    target", both deleted by register item A90. Neither exists anywhere now;
    these are regression guards against restoring either, in the shared copy or
    anywhere it is read from.

    A sentence naming one cause is false whenever another fired, which is how
    the target page's wording went stale in the first place. This page's own
    sentence, asserted present, names none.
    """
    text = _render_job_page(client, "?handoff=unverified")
    assert "the full list of runs on this target" not in text
    assert "could not read every sub-job your starred designs came from" not in text
    assert _DISTINGUISHING["unverified"] in text


def test_no_banner_claims_a_charge_was_avoided(client):
    """/lab-projects/submit has no wallet or Stripe code on any path, and the
    modal that opens this flow says "No commitment". A reassurance that no money
    moved implies money could have moved; the compute this job's designs came
    from was charged when the job was dispatched.

    Scoped to the banner's paragraphs, each found by its own sentence, so a
    page-wide assertion cannot pass or fail on copy that belongs to something
    else on the page.

    BOTH OF THE BANNER'S PARAGRAPHS, not just the reason. The reason paragraph
    is one of five and the size-cap paragraph rides any of them, so a `charge`
    reassurance added to the second would satisfy a loop that only ever rendered
    the first.
    """
    for reason, sentence in _DISTINGUISHING.items():
        paras = _render_job_page(client, f"?handoff={reason}&truncated=7",
                                 paragraphs=True)
        banner = [p for p in paras if sentence in p]
        assert len(banner) == 1, (
            f"?handoff={reason} did not render exactly one banner paragraph"
        )
        assert "charge" not in banner[0], (
            f"?handoff={reason} still reassures about a charge this route "
            f"cannot make: {banner[0]!r}"
        )
        capped = [p for p in paras if _OVER_LIMIT in p]
        assert len(capped) == 1, (
            f"?handoff={reason}&truncated=7 did not render exactly one "
            f"size-cap paragraph"
        )
        assert "charge" not in capped[0], (
            f"the size-cap paragraph reassures about a charge this route "
            f"cannot make: {capped[0]!r}"
        )


# ---------------------------------------------------------------------------
# THE NOUN. Register item A91's own defect, pinned in both directions.
#
# `rejected` is the sentence that attributes a design to the page it is on, and
# the partial resolves the phrase through a mapping with a total default rather
# than through a fall-through. A caller whose noun is not a key gets the
# default, which names no page at all, instead of whichever arm happened to be
# last -- which is what handed this page the target page's wording.
# ---------------------------------------------------------------------------

_REJECTED_SCOPES = {
    "run":    "a design produced by this run",
    "target": "a design on this target",
    "job":    "a design this job produced",
}
_REJECTED_DEFAULT = "a design in the scope this request was made against"


def _macro_sentence(app, reason, parent) -> str:
    """One rendering of the shared macro, at an arbitrary noun.

    The macro is called directly because no route passes a noun outside the
    three known ones, and the half of the fix that matters most is what happens
    to a noun nobody has added yet. It is still rendered output and not template
    source: the branch is taken here, exactly as it is on the page.
    """
    tpl = app.jinja_env.get_template("components/lab_handoff_banner.html")
    return re.sub(
        r"\s+", " ", str(tpl.module.handoff_sentence(reason, parent))
    ).strip()


@pytest.mark.parametrize("parent", sorted(_REJECTED_SCOPES))
def test_each_known_parent_gets_its_own_rejected_wording(app, parent):
    """One noun per page, and no page's wording reachable from another's.

    THE ABSENCE HALF IS THE POINT. Before the fix ``parent='job'`` rendered the
    target arm's sentence byte for byte, so "this noun is right" is a claim
    about the two wordings that must NOT appear as much as about the one that
    must. The default is asserted absent for the same reason: a known noun that
    silently fell to it would still read as a working banner.
    """
    text = _macro_sentence(app, "rejected", parent)
    assert _REJECTED_SCOPES[parent] in text, (parent, text)
    for other, phrase in _REJECTED_SCOPES.items():
        if other == parent:
            continue
        assert phrase not in text, (parent, other, text)
    assert _REJECTED_DEFAULT not in text, (parent, text)


@pytest.mark.parametrize("parent", ["campaign", "widget", "", None])
def test_an_unknown_parent_names_no_page_at_all(app, parent):
    """The default arm, which is the reason the noun is a mapping.

    A noun that is not a key must degrade to a sentence true of any parent
    rather than to another parent's claim -- that is what a fall-through cannot
    give, and it is the whole difference between the two shapes. Asserted both
    ways: the noun-free phrase is rendered, and none of the three page wordings
    is. No route passes a noun outside the three keys today (the four call sites
    pass 'run', 'target', 'job', and ``blueprints/targets.py`` passes 'target'
    into templates/unavailable.html), which is exactly why the fourth one has to
    be exercised here.
    """
    text = _macro_sentence(app, "rejected", parent)
    assert _REJECTED_DEFAULT in text, (parent, text)
    for phrase in _REJECTED_SCOPES.values():
        assert phrase not in text, (parent, text)


def test_the_page_says_this_job_and_names_no_other_page(client):
    """The route-level half of the two tests above, on both sentences that
    carry a noun. The macro tests fix the mapping; this one fixes what
    ``templates/job_detail.html`` passes into it, which is a separate way to get
    the wrong page's wording onto this page."""
    rejected = _render_job_page(client, "?handoff=rejected")
    assert _REJECTED_SCOPES["job"] in rejected
    assert _REJECTED_SCOPES["run"] not in rejected
    assert _REJECTED_SCOPES["target"] not in rejected
    assert _REJECTED_DEFAULT not in rejected

    unverified = _render_job_page(client, "?handoff=unverified")
    assert "belong to this job" in unverified
    assert "belong to this run" not in unverified
    assert "belong to this target" not in unverified


# ---------------------------------------------------------------------------
# What the per-request size cap discarded, on the paths that FAILED
#
# `?truncated=` is orthogonal to all five reasons above: a shortlist can be
# unnamed, or unverifiable, or wholly rejected, AND ALSO be over the cap.
# ---------------------------------------------------------------------------

_OVER_LIMIT = "over the per-request limit"


def test_a_failed_handoff_also_states_what_the_size_cap_discarded(client):
    """The second paragraph, and the reason it is a second paragraph rather
    than a clause: it is a different fact from the reason, survives every
    reason, and must not read as a softening of one."""
    text = _render_job_page(client, "?handoff=rejected&truncated=120")
    assert "up to 120 of your starred designs were " + _OVER_LIMIT in text
    assert "were not read at all" in text
    # It joins the reason rather than replacing it.
    _assert_only(text, "rejected")


def test_a_single_discarded_design_is_counted_in_the_singular(client):
    """The macro switches noun and verb on the count, so 1 is the value that
    tells a working agreement from "1 designs were"."""
    text = _render_job_page(client, "?handoff=rejected&truncated=1")
    assert "up to 1 of your starred design was " + _OVER_LIMIT in text
    assert "was not read at all" in text


def test_the_size_cap_paragraph_rides_every_reason(client):
    """By hand over all five, because the count is orthogonal to the reason; a
    template that rendered it under one reason only would satisfy the test
    above."""
    for reason in _DISTINGUISHING:
        text = _render_job_page(client, f"?handoff={reason}&truncated=7")
        assert "up to 7 of your starred designs" in text, reason


def test_a_shortlist_inside_the_cap_draws_no_size_paragraph(client):
    """THE PAIR. Rendering it unconditionally would satisfy both tests above
    while telling every ordinary failed handoff that designs were cut."""
    text = _render_job_page(client, "?handoff=rejected")
    assert _OVER_LIMIT not in text


def test_a_crafted_truncated_value_draws_no_size_paragraph(client):
    """``truncated`` is read straight in the template, so the ``|int`` filter is
    the whole guard: anything that is not a number yields 0 and the block does
    not render. A negative one likewise, or the page would announce a shortfall
    of minus five designs."""
    for crafted in ("' or 1=1--", "1e9999", "-5", "", "NaN"):
        text = _render_job_page(client, f"?handoff=rejected&truncated={crafted}")
        assert _OVER_LIMIT not in text, crafted


def test_the_size_paragraph_needs_a_reason_to_hang_on(client):
    """It lives inside ``{% if handoff %}``. A bare ``?truncated=`` on this page
    is not a failed handoff -- it is a link somebody pasted -- and drawing an
    alert for it would tell a user with nothing pending that their submission
    lost designs."""
    text = _render_job_page(client, "?truncated=120")
    assert _OVER_LIMIT not in text
    for sentence in _DISTINGUISHING.values():
        assert sentence not in text
