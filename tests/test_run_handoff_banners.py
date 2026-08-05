"""The five ``?handoff=`` banners on /campaigns/<id>, RENDERED.

The sibling of ``tests/test_target_handoff_banners.py``.
``_submit_campaign_shortlist`` has five exits that send a failed lab handoff
back to the run page carrying a reason, and ``blueprints/campaigns.py``
whitelists those five and hands the survivor to the template. Before this file
existed the run page rendered none of them: every one of those exits was a bare
redirect with nothing on it, on the one action in this product that hands work
to a wet lab.

WHY THE RENDERED TEXT AND NOT THE TEMPLATE SOURCE. A source-substring assertion
cannot tell which Jinja branch it matched, or whether any branch was taken at
all; every sentence below lives in the template unconditionally. So the page is
served through the real route, the response HTML is parsed, and the assertions
read the VISIBLE TEXT.

TWO OF THE FIVE SENTENCES DIFFER FROM THE TARGET PAGE'S, and that is the point
of having a second file rather than one shared partial. This route's `rejected`
means "not a child of this run", not "not on this target"; and its `unverified`
has exactly one cause -- a sub-job read that never completed -- because
``_submit_campaign_shortlist`` compares ``job.campaign_id`` off the same row as
the job and makes no second, paged read that could come back short. The target
page's `unverified` sentence names that second read, so copying it here would
describe a lookup this route never performs.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

from blueprints.campaigns import LAB_HANDOFF_REASONS

_CID = str(uuid.uuid4())

# The one sentence per reason that no other reason says.
_DISTINGUISHING = {
    "none":       "arrived with no designs in it",
    "noname":     "needs a name for the target",
    "rejected":   "none of them could be matched to a design produced by this run",
    "unverified": "could not read every sub-job your starred designs came from",
    "failed":     "Your request could not be submitted",
}


def test_every_whitelisted_reason_has_a_sentence_of_its_own():
    """THE COUPLING, asserted rather than assumed.

    Set equality in BOTH directions. A missing key would let a sixth reason
    added to the route fall through to the template's ``{% else %}`` arm and
    tell the user "your request could not be submitted" for an unrelated cause,
    with the whole suite green -- which is exactly what
    ``blueprints/targets.py`` records having paid for twice. An extra key would
    let this file test a banner the route can never produce, which is how a
    dead branch survives a rename.

    ``_DISTINGUISHING["failed"]`` is deliberately the ``{% else %}`` arm's own
    sentence, so a sixth reason is INDISTINGUISHABLE from ``failed`` at the
    template. That is why the guard has to live here, at the key set, rather
    than in an assertion about rendered text.
    """
    assert set(_DISTINGUISHING) == set(LAB_HANDOFF_REASONS), (
        sorted(set(_DISTINGUISHING) ^ set(LAB_HANDOFF_REASONS)))


class _Text(HTMLParser):
    """Visible text of a page, with <script>/<style> bodies excluded.

    Also collects each ``<p>``'s own text, which is what lets an assertion be
    scoped to the handoff sentence rather than to the whole page. The run page
    carries a paused-wallet hint and a budget panel that legitimately discuss
    money, so a page-wide assertion about charges would be testing those.
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


def _campaign():
    """Matches ``_campaign_fixture`` in tests/test_runs_detail_template.py --
    the minimal object ``runs/detail.html`` reads."""
    return SimpleNamespace(
        id=_CID,
        name="Smoke Target",
        tool="rfdiffusion",
        status="completed",
        requested_designs=24,
        total_subjobs=6,
        target_name="Smoke Target",
        budget_usd=Decimal("12.00"),
    )


def _counts():
    return {
        "pending": 0, "running": 0, "succeeded": 6, "failed": 0,
        "timeout": 0, "cancelled": 0, "total": 6,
    }


def _agg():
    """A minimal ``aggregate_campaign_candidates`` envelope. The banners under
    test sit above the results table and are independent of it, so the envelope
    stays empty: what these tests must not depend on is the pooled read, the
    part of this page most likely to change shape."""
    return {"candidates": [], "columns": [], "total": 0, "capped": False}


def _render_run_page(client, query="", *, paragraphs=False):
    """GET /campaigns/<id><query> and return its VISIBLE text, or -- with
    ``paragraphs=True`` -- the text of each ``<p>`` on the page."""
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.compute_campaigns.get_campaign",
                  return_value=_campaign()), \
            patch("shared.compute_campaigns.get_progress_counts",
                  return_value=_counts()), \
            patch("shared.compute_campaigns.aggregate_campaign_candidates",
                  return_value=_agg()):
        resp = client.get(f"/campaigns/{_CID}{query}")
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
    _assert_only(_render_run_page(client, f"?handoff={reason}"), reason)


def test_the_run_page_carries_no_banner_without_a_handoff_reason(client):
    """THE PAIR for all five. Every sentence above lives in the template
    unconditionally, so a test that only ever renders WITH a reason cannot tell
    a working ``{% if handoff %}`` from a block that always draws."""
    text = _render_run_page(client)
    for sentence in _DISTINGUISHING.values():
        assert sentence not in text


def test_a_crafted_handoff_value_renders_nothing(client):
    """The whitelist in ``blueprints/campaigns.py``. ``handoff`` arrives from a
    query string, so an unknown value must render no banner at all rather than
    an empty alert or the ``{% else %}`` failure text -- which would let any
    link make a user believe their submission failed."""
    text = _render_run_page(client, "?handoff=' or 1=1--")
    for sentence in _DISTINGUISHING.values():
        assert sentence not in text


def test_the_rejected_banner_does_not_tell_the_user_to_retry(client):
    """Permanent against transient, as behaviour rather than as a difference in
    wording. ``none`` means the POST carried no designs and retrying is right;
    ``rejected`` means every design it carried was refused by a check that ran,
    so the SAME refs will be refused identically and telling that user to press
    the button again is telling them to repeat a no-op."""
    rejected = _render_run_page(client, "?handoff=rejected")
    none = _render_run_page(client, "?handoff=none")
    assert "Try the button again" in none
    assert "Try the button again" not in rejected
    assert "will be refused the same way" in rejected


def test_the_unverified_banner_names_the_sub_job_read_not_a_list_of_runs(client):
    """The one sentence that must NOT be copied from the target page.

    That page says "we could not read the full list of runs on this target",
    which describes ``campaign_ids_for_target`` -- a second, paged read whose
    completeness the target arm has to track. This arm makes no second read at
    all: ``job.campaign_id`` arrives on the same row as the job, so the only
    thing that can leave a ref undecided here is the sub-job read itself.
    Copying the target sentence would tell the user about a lookup this route
    never performs.
    """
    text = _render_run_page(client, "?handoff=unverified")
    assert "the full list of runs on this target" not in text
    assert "could not read every sub-job your starred designs came from" in text


def test_no_banner_claims_a_charge_was_avoided(client):
    """/lab-projects/submit has no wallet or Stripe code on any path, and the
    modal that opens this flow says "No commitment". A reassurance that no
    money moved implies money could have moved, contradicting that modal; the
    compute these designs came from was charged when the run was dispatched.

    Scoped to the handoff paragraph, found by its own sentence. This page's
    paused-wallet hint and budget panel discuss money for good reason, and a
    page-wide assertion would be testing those instead of this one.
    """
    for reason, sentence in _DISTINGUISHING.items():
        paras = _render_run_page(client, f"?handoff={reason}", paragraphs=True)
        banner = [p for p in paras if sentence in p]
        assert len(banner) == 1, (
            f"?handoff={reason} did not render exactly one banner paragraph"
        )
        assert "charge" not in banner[0], (
            f"?handoff={reason} still reassures about a charge this route "
            f"cannot make: {banner[0]!r}"
        )


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
    text = _render_run_page(client, "?handoff=rejected&truncated=120")
    assert "up to 120 of your starred designs were " + _OVER_LIMIT in text
    assert "were not read at all" in text
    # It joins the reason rather than replacing it.
    _assert_only(text, "rejected")


def test_the_size_cap_paragraph_rides_every_reason(client):
    """By hand over all five, because the count is computed before the guards
    and attached to four different exits; a template that rendered it under one
    reason only would satisfy the test above."""
    for reason in _DISTINGUISHING:
        text = _render_run_page(client, f"?handoff={reason}&truncated=7")
        assert "up to 7 of your starred designs" in text, reason


def test_a_shortlist_inside_the_cap_draws_no_size_paragraph(client):
    """THE PAIR. Rendering it unconditionally would satisfy both tests above
    while telling every ordinary failed handoff that designs were cut."""
    text = _render_run_page(client, "?handoff=rejected")
    assert _OVER_LIMIT not in text


def test_a_crafted_truncated_value_draws_no_size_paragraph(client):
    """``truncated`` is read straight in the template, so the ``|int`` filter is
    the whole guard: anything that is not a number yields 0 and the block does
    not render. A negative one likewise, or the page would announce a shortfall
    of minus five designs."""
    for crafted in ("' or 1=1--", "1e9999", "-5", "", "NaN"):
        text = _render_run_page(client, f"?handoff=rejected&truncated={crafted}")
        assert _OVER_LIMIT not in text, crafted


def test_the_size_paragraph_needs_a_reason_to_hang_on(client):
    """It lives inside ``{% if handoff %}``. A bare ``?truncated=`` on the run
    page is not a failed handoff -- it is a link somebody pasted -- and drawing
    an alert for it would tell a user with nothing pending that their
    submission lost designs."""
    text = _render_run_page(client, "?truncated=120")
    assert _OVER_LIMIT not in text
    for sentence in _DISTINGUISHING.values():
        assert sentence not in text
