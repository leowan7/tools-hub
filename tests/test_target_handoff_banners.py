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

from shared.targets import DesignTarget

# The one sentence per reason that no other reason says. Keyed by the query
# value the route whitelists, so a reason added to that whitelist without a
# banner of its own shows up here as a missing key rather than as silence.
_DISTINGUISHING = {
    "none":       "arrived with no designs in it",
    "rejected":   "none of them could be matched to a design on this target",
    "noname":     "needs a name for the target",
    "unverified": "could not read the full list of runs on this target",
    "failed":     "Your request could not be submitted",
}


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
