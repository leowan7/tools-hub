"""The five ``?handoff=`` banners on /targets/<id>, RENDERED.

``blueprints/lab_projects.py`` has five exits that send a failed lab handoff
back to the target page carrying a reason, and ``blueprints/targets.py``
whitelists those five and hands the survivor to the template. Nothing rendered
any of them: mutating ``handoff=handoff`` to ``handoff=""`` in the route passed
the entire suite, which means the whole disclosure could be deleted with no
test noticing -- on the one action in this product that hands work to a wet lab.

WHY THE RENDERED TEXT AND NOT THE TEMPLATE SOURCE. A source-substring
assertion cannot tell which Jinja branch it matched, or whether any branch was
taken at all; every sentence below lives in the template unconditionally. So
the page is served through the real route, the response HTML is parsed, and
the assertions read the VISIBLE TEXT.

Each test also asserts the other four sentences are ABSENT. That is what makes
"the right banner" a stronger claim than "a banner": the five differ only in
what they tell the user to do next, and a reason that renders two of them, or
renders the wrong one, is the defect that produced this file (round 19 sent an
all-rejected shortlist to the `none` banner, which told the user to press the
button again forever).
"""

from __future__ import annotations

import re
import uuid
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

from blueprints.targets import HANDOFF_REASONS
from shared.targets import DesignTarget

# The one sentence per reason that no other reason says.
_DISTINGUISHING = {
    "none":       "arrived with no designs in it",
    "rejected":   "none of them could be matched to a design on this target",
    "noname":     "needs a name for the target",
    "unverified": "could not read the full list of runs on this target",
    "failed":     "Your request could not be submitted",
}


def test_every_whitelisted_reason_has_a_sentence_of_its_own():
    """THE COUPLING, asserted rather than assumed.

    This dict used to sit under a comment claiming that "a reason added to
    that whitelist without a banner of its own shows up here as a missing key
    rather than as silence". Nothing derived one from the other, so it was
    false: adding a sixth reason to the route rendered the `failed` arm's copy
    -- "Your request could not be submitted" -- for an unrelated cause, with
    the entire suite green. Confirmed by mutation twice.

    Set equality in BOTH directions. A missing key would let a new reason fall
    through to `{% else %}`; an extra key would let this file test a banner the
    route can never produce, which is how a dead branch survives a rename.

    `_DISTINGUISHING["failed"]` is deliberately the `{% else %}` arm's own
    sentence, so a sixth reason is INDISTINGUISHABLE from `failed` at the
    template. That is exactly why the guard has to live here, at the key set,
    rather than in an assertion about rendered text.
    """
    assert set(_DISTINGUISHING) == set(HANDOFF_REASONS), (
        sorted(set(_DISTINGUISHING) ^ set(HANDOFF_REASONS)))


class _Text(HTMLParser):
    """Visible text of a page, with <script>/<style> bodies excluded.

    Also collects each ``<p>``'s own text, which is what lets an assertion be
    scoped to the handoff sentence rather than to the whole page. The target
    page carries other banners (the launch summary, the stranded-draft empty
    state) that legitimately discuss charges, so a page-wide assertion about
    the word "charged" would be testing them instead.
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


def _parse(html: str) -> _Text:
    parser = _Text()
    parser.feed(html)
    return parser


def _visible(html: str) -> str:
    return _parse(html).text


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


def _target():
    return DesignTarget(
        id=str(uuid.uuid4()),
        user_id="u-1",
        name="HER2",
        filename="her2.pdb",
        storage_path="u-1/target-x/her2.pdb",
        target_chain="A",
        chain_summary={
            "total_standard_residues": 210,
            "chains": [{
                "chain_id": "A", "standard_residue_count": 210,
                "hetatm_resnames": [], "water_count": 0,
                "min_resnum": 1, "max_resnum": 210,
            }],
        },
    )


def _agg():
    """A minimal ``aggregate_target_candidates`` envelope.

    Mirrors ``tests/test_target_routes.py::_agg``. The banners under test are
    above the results table and independent of it, so the envelope stays empty:
    what these tests must not do is depend on the pooled read, which is the
    part of this page most likely to change shape.
    """
    return {
        "ok": True, "partial": False, "candidates": [], "total": 0,
        "shown": 0, "unranked": 0, "capped": False, "columns": [],
        "tools": [], "per_tool": {}, "campaigns": [],
        "standalone_jobs": 0, "refold_jobs": 0, "passed_total": 0,
        "provisional": False, "sort_mode": "percentile", "multi_tool": False,
        "limit": 300, "split_tools": [],
    }


def _render_target_page(client, query="", *, paragraphs=False):
    """GET /targets/<id><query> and return its VISIBLE text, or -- with
    ``paragraphs=True`` -- the text of each ``<p>`` on the page.

    ``list_campaigns_for_target`` is patched explicitly rather than left to the
    blanked Supabase env: the route calls it on the empty-runs path this
    envelope produces, and a test whose isolation depends on a client
    happening to be unavailable is not isolated.
    """
    target = _target()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=target), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=[]):
        resp = client.get(f"/targets/{target.id}{query}")
    assert resp.status_code == 200, resp.status_code
    parsed = _parse(resp.get_data(as_text=True))
    return parsed.paragraphs if paragraphs else parsed.text


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
    """The five reasons, one at a time, on the real page.

    Parametrised so a reason added to the route's whitelist without a template
    branch fails here instead of silently falling through to the ``{% else %}``
    arm and telling the user their submission failed for an unrelated reason.
    """
    _assert_only(_render_target_page(client, f"?handoff={reason}"), reason)


def test_the_page_carries_no_banner_without_a_handoff_reason(client):
    """The pair for all five. Every sentence above lives in the template
    unconditionally, so a test that only ever renders WITH a reason cannot tell
    a working ``{% if handoff %}`` from a block that always draws."""
    text = _render_target_page(client)
    for sentence in _DISTINGUISHING.values():
        assert sentence not in text


def test_a_crafted_handoff_value_renders_nothing(client):
    """The whitelist in ``blueprints/targets.py``. ``handoff`` arrives from a
    query string, so an unknown value must render no banner at all rather than
    an empty alert panel or the ``{% else %}`` failure text -- which would let
    any link make a user believe their submission failed."""
    text = _render_target_page(client, "?handoff=' or 1=1--")
    for sentence in _DISTINGUISHING.values():
        assert sentence not in text


def test_the_rejected_banner_does_not_tell_the_user_to_retry(client):
    """The distinction round 19 collapsed, stated as behaviour rather than as
    a difference in wording. `none` means the POST carried no designs and
    retrying is the right advice; `rejected` means every design it carried was
    refused, and the SAME refs will be refused identically, so telling that
    user to press the button again is telling them to repeat a no-op.
    """
    rejected = _render_target_page(client, "?handoff=rejected")
    none = _render_target_page(client, "?handoff=none")
    assert "Try the button again" in none
    assert "Try the button again" not in rejected
    assert "will be refused the same way" in rejected


def test_no_banner_claims_a_charge_was_avoided(client):
    """/lab-projects/submit has no wallet or Stripe code on any path, and the
    modal that opens this flow says "No commitment -- this is a scoping
    request". A reassurance that no money moved implies money could have
    moved, contradicting that modal; the compute these designs came from was
    charged long before this button existed.

    Scoped to the handoff paragraph, found by its own sentence. Other banners
    on this page -- the launch summary and the stranded-draft empty state --
    discuss charges for good reason, and a page-wide assertion would be
    testing those instead of this one.
    """
    for reason, sentence in _DISTINGUISHING.items():
        paras = _render_target_page(client, f"?handoff={reason}",
                                    paragraphs=True)
        banner = [p for p in paras if sentence in p]
        assert len(banner) == 1, (
            f"?handoff={reason} did not render exactly one banner paragraph"
        )
        assert "charge" not in banner[0], (
            f"?handoff={reason} still reassures about a charge this route "
            f"cannot make: {banner[0]!r}"
        )


# ---------------------------------------------------------------------------
# ROUND 21: what the per-request size cap discarded, on the paths that FAILED
#
# `?truncated=` is orthogonal to all five reasons above: a shortlist can be
# unnamed, or unverifiable, or wholly rejected, AND ALSO be over the cap. The
# submit computed the number on every exit and spent three of the four throwing
# it away, so on exactly the paths where the user is already being told
# something went wrong, up to 120 designs they starred went unmentioned.
# ---------------------------------------------------------------------------

_OVER_LIMIT = "over the per-request limit"


def test_a_failed_handoff_also_states_what_the_size_cap_discarded(client):
    """The second paragraph, and the reason it is a second paragraph rather
    than a clause: it is a different fact from the reason, survives every
    reason, and must not read as a softening of one."""
    text = _render_target_page(client, f"?handoff=rejected&truncated=120")
    assert "up to 120 of your starred designs were " + _OVER_LIMIT in text
    assert "were not read at all" in text
    # It joins the reason rather than replacing it.
    _assert_only(text, "rejected")


def test_the_size_cap_paragraph_rides_every_reason(client):
    """Parametrised by hand over all five, because the count is computed before
    the guards and attached to four different exits; a template that rendered it
    only under one reason would satisfy the test above."""
    for reason in _DISTINGUISHING:
        text = _render_target_page(client, f"?handoff={reason}&truncated=7")
        assert "up to 7 of your starred designs" in text, reason


def test_a_shortlist_inside_the_cap_draws_no_size_paragraph(client):
    """The pair. Rendering it unconditionally would satisfy both tests above
    while telling every ordinary failed handoff that designs were cut."""
    text = _render_target_page(client, "?handoff=rejected")
    assert _OVER_LIMIT not in text


def test_a_crafted_truncated_value_draws_no_size_paragraph(client):
    """`truncated` arrives from the query string and is read straight in the
    template, so the `|int` filter is the whole guard: anything that is not a
    number yields 0 and the block does not render. A negative one likewise, or
    the page would announce a shortfall of minus five designs."""
    for crafted in ("' or 1=1--", "1e9999", "-5", "", "NaN"):
        text = _render_target_page(client, f"?handoff=rejected&truncated={crafted}")
        assert _OVER_LIMIT not in text, crafted


def test_the_size_paragraph_needs_a_reason_to_hang_on(client):
    """It lives inside `{% if handoff %}`. A bare `?truncated=` on the target
    page is not a failed handoff -- it is a link somebody pasted -- and drawing
    an alert panel for it would tell a user with nothing pending that their
    submission lost designs."""
    text = _render_target_page(client, "?truncated=120")
    assert _OVER_LIMIT not in text
    for sentence in _DISTINGUISHING.values():
        assert sentence not in text
