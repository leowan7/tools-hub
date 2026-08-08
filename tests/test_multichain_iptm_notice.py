"""ipTM must be marked not-comparable on a multi-chain target.

The defect: ipTM is computed over the interfaces of the whole complex, not the
binder-to-target pair alone, so on a multi-chain target the target's own
chain-chain interface holds the number up almost independently of binder
quality. It is both the displayed value AND the ranking key
(``shared/result_columns.py``), so a mediocre binder can rank first carrying a
plausible-looking number.

Stated at that level on purpose. An earlier version of this docstring, and of
the banner, said "a MAX over residues" and quoted "~0.9 for a real crystal
dimer" — and four pipeline files in this repo describe ipTM as interface-pTM
"averaged over EVERY chain pair" instead (tools/af2/run_pipeline.py:202 and
three siblings). The conclusion holds under either reduction; the figure does
not. See the comment above MULTICHAIN_IPTM_UNRELIABLE_TOOLS in
shared/score_legends.py.

Every test here asserts BOTH directions. A presence-only test passes against a
banner that renders unconditionally, which would put a scary caveat on every
single-chain run — the far more common case — and train users to ignore it.
"""
from __future__ import annotations

import os
import re
import uuid
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.score_legends import (
    MULTICHAIN_IPTM_UNRELIABLE_TOOLS,
    multichain_iptm_unreliable,
)
from shared.targets import TARGET_READ_OK, DesignTarget, TargetRead

pytestmark = pytest.mark.usefixtures("isolate_supabase")

NOTICE_MARKER = "data-multichain-iptm-notice"

# boltzgen is deliberately not here any more: llm-proteinDesigner#18 deployed,
# so its container reports the binder-to-target interface and the banner's
# claim is false of any new run. The caveat its PRE-deploy runs still need
# moved to the ipTM legend, which is per tool and per row. The reasoning, and
# the two discriminators that were checked — one absent, one merely not
# projected — are in the comment above MULTICHAIN_IPTM_UNRELIABLE_TOOLS in
# shared/score_legends.py.
BANNER_TOOLS = ("rfdiffusion", "pxdesign", "bindcraft")

# The load-bearing half of the mechanism sentence: the number is computed over
# interfaces that INCLUDE the target's own chain-chain contact. A shape, not a
# phrasing, and dash-agnostic — the copy uses an en dash and the extracted text
# carries it through convert_charrefs.
_INCLUDES_TARGET_INTERFACE = re.compile(
    r"includ\w+ the target'?s own chain.chain", re.I
)

# Anything that points at page furniture. The macro takes no parameter that
# could tell it which page it is on, and two of its six call sites have no
# re-fold control, so a locative promise cannot be true everywhere.
#
# A COLUMN IS FURNITURE TOO, and the second group below is the half that was
# missing. This regex used to match locatives only, so the copy that replaced
# the re-fold promise -- "Compare designs on pLDDT and the other columns in
# this table" -- was invisible to it and shipped false: in multi-cohort mode
# the pooled target page has NO metric columns, only Tool / Score / Pctile
# (components/candidate_table.html), so there is no pLDDT to compare on.
#
# ``\bof this table\b`` is deliberately NOT matched. The banner still says the
# ORDER "of this table" is indicative, and that is true wherever the macro
# renders, because every call site draws a candidate table directly beneath
# it. What cannot be promised is a particular COLUMN inside it.
_POINTS_AT_FURNITURE = re.compile(
    # locatives
    r"\bbelow\b|\babove\b|\bon this page\b|\bat the bottom\b|"
    r"\bre-?fold\b[^.]{0,40}\bBoltz|"
    # columns: the word itself, "in this table", and the metric names that
    # exist as columns on some call sites and not others
    r"\bcolumns?\b|\bin this table\b|"
    r"\bpLDDT\b|\bipAE\b|\bi_pAE\b|\bpAE\b|\bRMSD\b|\btotal_reward\b",
    re.I,
)


@pytest.fixture(scope="module")
def flask_app():
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


# ---------------------------------------------------------------------------
# The decision, in Python
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(MULTICHAIN_IPTM_UNRELIABLE_TOOLS))
@pytest.mark.parametrize("chain", ["A,B", "A B", "A, B", "A,B,C"])
def test_multi_chain_targets_are_flagged(tool, chain):
    assert multichain_iptm_unreliable(tool, chain) is True


@pytest.mark.parametrize("tool", sorted(MULTICHAIN_IPTM_UNRELIABLE_TOOLS))
@pytest.mark.parametrize("chain", ["A", " A ", "", None, "A,A", "B B"])
def test_single_chain_targets_are_not_flagged(tool, chain):
    """``"A,A"`` matters: the field de-duplicates everywhere else
    (tools/base.parse_target_chains), so a repeated chain is ONE chain and must
    not trip a caveat about cross-chain interfaces that do not exist."""
    assert multichain_iptm_unreliable(tool, chain) is False


@pytest.mark.parametrize(
    "tool", ["proteina", "rfantibody", "boltz2", "boltzgen", "", None]
)
def test_unaffected_tools_are_never_flagged(tool):
    """proteina reports af2_iptm from a different scoring path, rfantibody
    cannot take a multi-chain target at all, and boltzgen's container now
    reports the binder-to-target interface. Warning on any of them would be
    noise, and noise is what makes a real warning ignorable."""
    assert multichain_iptm_unreliable(tool, "A,B") is False


def test_a_pooled_table_is_flagged_if_any_tool_is_affected():
    """The target page pools several tools into one table."""
    assert multichain_iptm_unreliable(["proteina", "rfdiffusion"], "A,B") is True
    assert multichain_iptm_unreliable(["proteina"], "A,B") is False
    assert multichain_iptm_unreliable([], "A,B") is False


def test_boltzgen_left_the_banner_set_and_its_caveat_did_not_vanish():
    """The two halves of the B11 decision, pinned together on purpose.

    llm-proteinDesigner#18 is merged (311c29f) and deployed, so the container
    reports the binder-to-target interface and the banner would be telling
    every new boltzgen user something untrue about their run. It is out.

    What must NOT come with that is the silent loss of the caveat the
    PRE-deploy runs still need: a results page renders whatever the job
    stored, at least one multi-chain boltzgen run predates the deploy, there
    is no per-record marker saying which IPTM_KEYS entry produced the number,
    and the pooled rows the sixth call site renders carry no date because
    neither pooled read projects created_at (both checked; see the comment
    above MULTICHAIN_IPTM_UNRELIABLE_TOOLS, which is careful about the
    difference between absent and unprojected). So the caveat moved to the
    ipTM legend,
    which renders per tool and per row, and this test refuses to let one half
    of the trade happen without the other.
    """
    from shared.score_legends import get_legend

    assert "boltzgen" not in MULTICHAIN_IPTM_UNRELIABLE_TOOLS
    explanation = get_legend("boltzgen", "ipTM")["explanation"]
    assert "chain-chain" in explanation, (
        "boltzgen left the banner set without the legend picking up the "
        "pre-deploy caveat — the old runs now carry no warning anywhere"
    )


# ---------------------------------------------------------------------------
# The banner, in the rendered page
# ---------------------------------------------------------------------------

class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list = []

    def handle_data(self, data):
        self.chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self.chunks).split())


class _BannerText(HTMLParser):
    """Visible text INSIDE the notice element only.

    Scoped deliberately. Asserting on the whole page would let
    components/results_shell.html's own "Re-fold with Boltz-2 (cofold)" copy
    satisfy — or trip — a check about what the BANNER says, which is the
    unrelated-copy failure mode tests/test_multichain_form_affordances.py
    already paid for once.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self.chunks: list = []

    def handle_starttag(self, tag, attrs):
        if self._depth:
            self._depth += 1
        elif any(k == "data-multichain-iptm-notice" for k, _ in attrs):
            self._depth = 1

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self.chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self.chunks).split())


def _banner_text(html: str) -> str:
    parser = _BannerText()
    parser.feed(html)
    return parser.text


class _Headers(HTMLParser):
    """The visible label of every ``<th>`` on the page, in order.

    Used to pin the PREMISE of the furniture check rather than restating it:
    the copy may not name a column, and this is what says which columns the
    page has. Reading them off the rendered page means the premise fails
    loudly if candidate_table ever grows the metric columns back in
    multi-cohort mode, instead of the check quietly guarding nothing.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._chunks: list = []
        self.headers: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "th":
            self._depth = 1
            self._chunks = []
        elif self._depth:
            self._depth += 1

    def handle_endtag(self, tag):
        if not self._depth:
            return
        self._depth -= 1
        if self._depth == 0:
            self.headers.append(" ".join("".join(self._chunks).split()))

    def handle_data(self, data):
        if self._depth:
            self._chunks.append(data)


def _table_headers(html: str) -> list:
    parser = _Headers()
    parser.feed(html)
    return parser.headers


def _render_results(flask_app, tool: str, target_chain: str) -> str:
    job = SimpleNamespace(
        id="job-1",
        tool=tool,
        status="succeeded",
        inputs={"target_chain": target_chain, "hotspot_residues": ["A296"]},
        result={
            "candidates": [
                {
                    "design_name": "d0",
                    "sequence": "AAAA",
                    "scores": {"ipTM": 0.91, "pLDDT": 88.0, "filter_status": "pass"},
                }
            ],
            "tier": "pilot",
        },
    )
    from flask import render_template

    with flask_app.test_request_context(f"/jobs/{job.id}"):
        return render_template(
            f"tools/{tool}_results.html", job=job, send_target_tools=None
        )


@pytest.mark.parametrize("tool", BANNER_TOOLS)
def test_banner_appears_on_a_multi_chain_job(tool, flask_app):
    html = _render_results(flask_app, tool, "A,B")
    assert NOTICE_MARKER in html, f"{tool}: no notice on a multi-chain job"
    text = _Text()
    text.feed(html)
    body = text.text
    # The ranking consequence is the half that matters. A caveat that
    # disclaims only the VALUES while the ORDER silently persists is the
    # half-measure this exists to avoid.
    assert "ranked by it" in body, f"{tool}: notice never mentions ranking"
    # And the mechanism, at the level this repo can stand behind. This used to
    # assert "maximum over residues"; four pipeline files here call ipTM
    # interface-pTM "averaged over EVERY chain pair", so that assertion pinned
    # a claim the repo contradicts. What has to survive is WHY the number is
    # not about the binder: it is computed over interfaces that include the
    # target's own chain-chain contact. True under either reduction, and the
    # reason the warning exists at all.
    assert _INCLUDES_TARGET_INTERFACE.search(body), (
        f"{tool}: the notice no longer says the number includes the target's "
        f"own chain-chain interface, which is the whole claim. body={body!r}"
    )


@pytest.mark.parametrize("tool", BANNER_TOOLS)
def test_banner_is_absent_on_a_single_chain_job(tool, flask_app):
    html = _render_results(flask_app, tool, "A")
    assert NOTICE_MARKER not in html, (
        f"{tool}: notice rendered on a SINGLE-chain job — the common case. "
        f"A caveat shown to everyone is a caveat nobody reads."
    )


@pytest.mark.parametrize("tool", BANNER_TOOLS)
def test_banner_is_absent_when_the_job_has_no_inputs(tool, flask_app):
    """Older job rows predate the inputs column. A missing target_chain must
    read as "not known to be multi-chain", not raise."""
    from flask import render_template

    job = SimpleNamespace(
        id="job-1", tool=tool, status="succeeded", inputs=None,
        result={"candidates": [], "tier": "pilot"},
    )
    with flask_app.test_request_context("/jobs/job-1"):
        html = render_template(
            f"tools/{tool}_results.html", job=job, send_target_tools=None
        )
    assert NOTICE_MARKER not in html


def test_proteina_results_never_carry_the_banner(flask_app):
    """proteina's column is af2_iptm from a separate scoring path, which this
    change did not trace. Not warning is the honest position until it is."""
    html = _render_results(flask_app, "proteina", "A,B")
    assert NOTICE_MARKER not in html


# ---------------------------------------------------------------------------
# The other two call sites: the campaign page and the pooled target page
# ---------------------------------------------------------------------------
#
# WHY THESE ARE RENDERED AND NOT GREPPED. Both used to be "covered" by opening
# the template and asserting the source contains ``multichain_iptm_notice(``.
# That says the call is WRITTEN, and nothing about what it is called WITH or
# whether the branch it sits in is ever taken. Both arguments were mutated with
# the call text left intact --
#
#   runs/detail.html     ``.get('target_chain')``  -> ``.get('target_chains')``
#   targets/detail.html  ``target.target_chain``   -> ``target.chain``
#
# -- and the entire suite stayed byte-identical, while the banner became
# permanently dead on the two views a user actually compares runs in. Both
# mutations resolve to None/Undefined, the macro's guard reads that as "not
# known to be multi-chain", and it renders nothing at all: a silent failure
# with no error to notice.
#
# So these go through the REAL routes, against records shaped like the ones
# production stores, and assert on the rendered ``data-multichain-iptm-notice``
# in BOTH directions -- the same shape as the four job-page call sites above.

_CAMPAIGN_ID = str(uuid.uuid4())
_TARGET_ID = str(uuid.uuid4())


def _ctx():
    return SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com",
    )


def _candidates():
    """One design, because the notice on BOTH pages sits inside the block that
    draws the candidate table. With an empty pooled read neither page renders a
    banner in either direction, and the absent half of each pair would pass for
    a reason that has nothing to do with the chain."""
    return [{
        "pdb_key": "designs/design_0.pdb",
        "sequence": "MKTAY",
        "scores": {"ipTM": 0.91, "pLDDT": 88.0, "filter_status": "pass"},
        "_source_tool": "rfdiffusion",
        "_source_job_id": "job-aaaaaaaa",
        "_source_campaign_id": _CAMPAIGN_ID,
        "_source_chunk": 0,
        "_source_index": 0,
    }]


_COLUMNS = ["ipTM", "pLDDT", "filter_status"]


def _campaign(tool: str, target_chain: str):
    """The minimal campaign ``runs/detail.html`` reads.

    ``params`` is the sanitized submit payload the row actually carries:
    ``shared/compute_campaigns.create_campaign`` stores
    ``sanitize_shared_params(tool, params)``, which drops only
    underscore-prefixed wiring keys, so ``target_chain`` reaches the template
    under the name the form posted it under. That is the fact the grepped
    version could not check and the mutation above exploited.
    """
    return SimpleNamespace(
        id=_CAMPAIGN_ID,
        name="Fc dimer",
        tool=tool,
        status="completed",
        requested_designs=24,
        total_subjobs=6,
        target_name="Fc dimer",
        budget_usd=Decimal("12.00"),
        params={
            "target_chain": target_chain,
            "hotspot_residues": "A296",
            "num_designs": 24,
        },
    )


def _render_run_page(flask_app, tool: str, target_chain: str) -> str:
    """GET /campaigns/<id> — the real route, the real template."""
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    counts = {
        "pending": 0, "running": 0, "succeeded": 6, "failed": 0,
        "timeout": 0, "cancelled": 0, "total": 6,
    }
    agg = {
        "candidates": _candidates(), "columns": _COLUMNS,
        "total": 1, "capped": False,
    }
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.compute_campaigns.get_campaign",
                  return_value=_campaign(tool, target_chain)), \
            patch("shared.compute_campaigns.get_progress_counts",
                  return_value=counts), \
            patch("shared.compute_campaigns.aggregate_campaign_candidates",
                  return_value=agg):
        resp = client.get(f"/campaigns/{_CAMPAIGN_ID}")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def _target(target_chain: str):
    return DesignTarget(
        id=_TARGET_ID,
        user_id="u-1",
        name="Fc dimer",
        filename="fc.pdb",
        storage_path="u-1/target-x/fc.pdb",
        target_chain=target_chain,
        chain_summary={
            "total_standard_residues": 440,
            "chains": [
                {"chain_id": "A", "standard_residue_count": 220,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 1, "max_resnum": 220},
                {"chain_id": "B", "standard_residue_count": 220,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 1, "max_resnum": 220},
            ],
        },
    )


def _render_target_page(flask_app, tools, target_chain: str) -> str:
    """GET /targets/<id> — the real route, the real template.

    ``list_campaigns_for_target`` is patched explicitly rather than left to the
    blanked Supabase env: the route calls it on the empty-runs path this
    envelope produces, and a test whose isolation depends on a client happening
    to be unavailable is not isolated.
    """
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    agg = {
        "ok": True, "partial": False, "candidates": _candidates(), "total": 1,
        "shown": 1, "unranked": 0, "capped": False, "columns": _COLUMNS,
        "tools": list(tools), "per_tool": {}, "campaigns": [],
        "standalone_jobs": 0, "refold_jobs": 0, "passed_total": 1,
        "provisional": False, "sort_mode": "percentile",
        "multi_tool": len(tools) > 1, "limit": 300, "split_tools": [],
    }
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(_target(target_chain),
                                          TARGET_READ_OK)), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=agg), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=[]):
        resp = client.get(f"/targets/{_TARGET_ID}")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


@pytest.mark.parametrize("tool,chain,expected", [
    ("rfdiffusion", "A,B", True),
    ("rfdiffusion", "A", False),
    # The tool half of the same call, so the page cannot be "fixed" into
    # warning about every run.
    ("proteina", "A,B", False),
])
def test_the_campaign_page_notice_follows_the_run_it_describes(
    flask_app, tool, chain, expected
):
    """``runs/detail.html`` reads the chain out of ``campaign.params``, which is
    a dict on the row rather than a column, so a wrong key is not an error —
    it is silence."""
    html = _render_run_page(flask_app, tool, chain)
    assert (NOTICE_MARKER in html) is expected, (
        f"campaign page, tool={tool} chain={chain!r}: "
        f"notice {'missing' if expected else 'rendered'}"
    )


@pytest.mark.parametrize("tools,chain,expected", [
    (["rfdiffusion"], "A,B", True),
    (["rfdiffusion"], "A", False),
    # ``agg.tools`` is a LIST of slugs on this page, and the macro's contract is
    # that the caveat applies if ANY pooled tool ranks on a complex-wide ipTM.
    # Both halves are asserted, so neither the pooling nor the chain can be
    # broken without a failure here.
    (["proteina", "rfdiffusion"], "A,B", True),
    (["proteina"], "A,B", False),
])
def test_the_pooled_target_page_notice_follows_the_target_it_describes(
    flask_app, tools, chain, expected
):
    """``targets/detail.html`` reads ``target.target_chain`` off the row. A
    misspelled attribute is a Jinja ``Undefined``, which the macro's guard
    reads as "not known to be multi-chain" — so the banner disappears with no
    error anywhere."""
    html = _render_target_page(flask_app, tools, chain)
    assert (NOTICE_MARKER in html) is expected, (
        f"target page, tools={tools} chain={chain!r}: "
        f"notice {'missing' if expected else 'rendered'}"
    )


def test_the_banner_does_not_point_at_page_furniture_it_cannot_see(flask_app):
    """The copy is page-independent, so it may not describe a page's controls.

    The macro takes ``(tool_slug, target_chain)`` and nothing that says which
    of its six call sites it is on. FURNITURE HERE IS BOTH A WIDGET AND A
    COLUMN, and this test has been through one of each:

      * it used to end "re-fold the top candidates with Boltz-2 below", which
        describes the Second-opinion fold panel components/results_shell.html
        draws — a panel two of the six call sites never draw:
        templates/targets/detail.html calls candidate_table directly and has
        no re-fold control anywhere on the page, and a job page whose run
        returned zero candidates renders the notice (it is called OUTSIDE
        results_shell) while results_shell draws the panel only inside its
        non-empty branch. Both are checked below, because the second is what
        makes a per-caller parameter the wrong fix: the caller would have to
        recompute a condition that lives inside another macro, from a
        different value each time;
      * its replacement then said "Compare designs on pLDDT and the other
        columns in this table", and in MULTI-COHORT mode the pooled target
        page has no metric columns at all. That variant is rendered below and
        its headers are asserted, so the premise is read off the page rather
        than restated. The old version of this test rendered only the
        single-tool page, where pLDDT IS a column — which is why it passed.
    """
    target_html = _render_target_page(flask_app, ["rfdiffusion"], "A,B")
    assert NOTICE_MARKER in target_html, "no banner to check"
    assert 'name="dest_tool"' not in target_html, (
        "the pooled target page has grown a re-fold control; this test's "
        "premise no longer holds and the copy decision should be revisited"
    )

    # The multi-cohort pooled table: same banner, no metric columns.
    #
    # Reached by a SINGLE tool at two presets as well as by two tools
    # (shared/target_results.py sets multi_cohort on either), so this is the
    # ordinary multi-chain case and not a corner.
    pooled_html = _render_target_page(
        flask_app, ["proteina", "rfdiffusion"], "A,B",
    )
    assert NOTICE_MARKER in pooled_html, (
        "the multi-cohort pooled table lost the banner"
    )
    pooled_headers = _table_headers(pooled_html)
    assert "Score" in pooled_headers and "Pctile" in pooled_headers, (
        f"the multi-cohort table is not in pooled mode; this test is then "
        f"checking the same page twice. headers={pooled_headers!r}"
    )
    assert not [h for h in pooled_headers if "pLDDT" in h], (
        f"the multi-cohort table has grown metric columns back; the banner "
        f"may name one again, and this premise should be revisited. "
        f"headers={pooled_headers!r}"
    )

    # A page that DOES have the control, so the assertion above is not passing
    # because re-folding exists nowhere.
    job_html = _render_results(flask_app, "rfdiffusion", "A,B")
    assert 'name="dest_tool"' in job_html, (
        "the job results page lost its re-fold control"
    )

    # And the zero-candidate job page: banner yes, panel no.
    job = SimpleNamespace(
        id="job-1", tool="rfdiffusion", status="succeeded",
        inputs={"target_chain": "A,B"},
        result={"candidates": [], "tier": "pilot"},
    )
    from flask import render_template

    with flask_app.test_request_context("/jobs/job-1"):
        empty_html = render_template(
            "tools/rfdiffusion_results.html", job=job, send_target_tools=None,
        )
    assert NOTICE_MARKER in empty_html
    assert 'name="dest_tool"' not in empty_html, (
        "results_shell now draws the re-fold panel with no candidates; the "
        "second half of this test's premise no longer holds"
    )

    banner = _banner_text(target_html)
    assert banner, "the notice rendered with no text in it"
    assert not _POINTS_AT_FURNITURE.search(banner), (
        f"the banner points at furniture — a control or a column — that is "
        f"not on every page it renders on: {banner!r}"
    )
    # Same copy everywhere, which is the property that makes one check enough.
    # The multi-cohort page is in this list because it is the one whose
    # furniture differs MOST from the job pages the copy tends to be written
    # against.
    assert _banner_text(job_html) == banner
    assert _banner_text(empty_html) == banner
    assert _banner_text(pooled_html) == banner


# ---------------------------------------------------------------------------
# The macro is wired everywhere a candidate table is drawn
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", [
    "tools/rfdiffusion_results.html",
    "tools/pxdesign_results.html",
    "tools/bindcraft_results.html",
    "tools/boltzgen_results.html",
])
def test_every_candidate_table_page_calls_the_notice(template, flask_app):
    """A completeness check over the four job-result partials, whose rendered
    behaviour in both directions is pinned above.

    boltzgen is still here although it no longer trips the notice. The call is
    the page asking a shared decision function, not the page deciding; leaving
    it means a future change to MULTICHAIN_IPTM_UNRELIABLE_TOOLS reaches every
    results view at once instead of one that quietly lost its wiring.

    ``runs/detail.html`` and ``targets/detail.html`` used to be in this list and
    are deliberately no longer. A source grep was the ONLY thing covering them,
    and it could see neither argument; they are rendered through their real
    routes above instead."""
    path = flask_app.jinja_env.get_or_select_template(template).filename
    body = open(path, encoding="utf-8").read()
    assert "multichain_iptm_notice(" in body, (
        f"{template} renders a candidate table but never calls the notice"
    )


def test_the_boltzgen_legend_describes_both_sides_of_the_deploy(flask_app):
    """The tooltip is now the only place the era distinction is made, so it
    has to make it — in both directions.

    An earlier draft of this file asserted the legend must NOT say
    binder-to-target, because the deploy had not happened. It has, so that
    assertion would now pin a false claim, which is worse than no test. What
    replaces it is not the mirror image: the legend has to say what the
    number IS today AND what an older multi-chain run stored, because a
    results page shows whatever the job saved and nothing in the record says
    which container produced it.
    """
    from shared.score_legends import get_legend

    legend = get_legend("boltzgen", "ipTM")
    explanation = legend["explanation"]
    assert "binder-to-target" in explanation, (
        "the legend still describes a value the deployed container no longer "
        "emits"
    )
    assert "chain-chain" in explanation, (
        "the legend drops the caveat that a pre-deploy multi-chain run stored "
        "the complex-wide number"
    )
    assert "older run" in explanation, (
        "the legend states the current meaning without saying older runs "
        "differ, which reads as if every stored value were binder-to-target"
    )
    # BOTH HALVES OF THE MOVED CAVEAT, pinned together so they cannot separate
    # again. The banner said "these designs are ALSO RANKED BY IT, so both the
    # values and the ORDER of this table should be treated as indicative only",
    # and the first move brought the value half only — the words "rank" and
    # "order" then appeared nowhere on a pre-deploy boltzgen results page.
    # boltzgen ranks on ipTM (shared/result_columns.py) and the pooled reads
    # sort then truncate at limit=300, so the order decides which designs are
    # visible at all. Asserted as two words rather than a phrase so the
    # sentence can be reworded.
    assert "ranked" in explanation, (
        "the legend disclaims the VALUE but never says the designs are "
        "ranked on it, which is the half that decides what the user sees"
    )
    assert "order" in explanation, (
        "the legend never says the ORDER is affected; past limit=300 the "
        "ipTM order decides which designs appear at all"
    )
    # Thresholds were calibrated on single-chain runs where the two keys nearly
    # coincide, so they remain the best available anchor and must not drift
    # silently alongside a wording change.
    assert legend["good"] == 0.7
    assert legend["excellent"] == 0.8


# ---------------------------------------------------------------------------
# The general pages that describe ipTM for every tool at once
# ---------------------------------------------------------------------------

# The claim no page may make unqualified. ipTM's INTENT is the binder-to-target
# pair, and stating it as fact is false today for rfdiffusion, pxdesign and
# bindcraft on a multi-chain target, and for any boltzgen run predating the
# August 2026 container update. Dash-agnostic and space-tolerant, so a
# rewording between "binder to target" and "binder-to-target" does not slip
# past.
_CLAIMS_THE_BINDER_PAIR = re.compile(r"binder.to.target interface", re.I)

# The qualifier that makes the sentence honest.
_QUALIFIES_MULTI_CHAIN = re.compile(r"multi.chain", re.I)


def _visible(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return parser.text


@pytest.mark.parametrize("path", ["/help/tools/rfdiffusion", "/tools/rfdiffusion"])
def test_the_general_pages_do_not_state_iptm_as_the_binder_pair(
    flask_app, path,
):
    """A page that cannot know the tool must not make the per-tool claim.

    Both of these describe ipTM once, for every tool at once —
    templates/help/tool_guide.html and the logged-out shell
    templates/tools/_preview.html — and both said "Predicted confidence in the
    binder to target interface." The per-tool legend and the multi-chain banner
    exist because that is not true everywhere; a general page repeating it as
    fact undoes them one click away.

    ASSERTED ON RENDERED TEXT, NOT SOURCE. The fix left explanatory comments in
    both templates that quote the banned phrase in order to ban it, so a source
    grep would fail on the fix itself. HTMLParser routes comments to
    handle_comment, which _Text ignores.

    ``/tools/rfdiffusion`` is requested with NO session, which is what selects
    the preview shell rather than the form. ``tool_enabled`` is patched because
    the flag is off in a bare test env and the route answers 404 — the flag is
    not what is under test here.
    """
    client = flask_app.test_client()
    with patch("blueprints.tools.tool_enabled", return_value=True):
        resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    body = _visible(resp.get_data(as_text=True))
    assert "ipTM" in body, (
        f"{path} no longer describes ipTM at all; this check has nothing to "
        f"look at and should be re-pointed rather than left passing"
    )
    for match in _CLAIMS_THE_BINDER_PAIR.finditer(body):
        window = body[max(0, match.start() - 400):match.end() + 400]
        assert _QUALIFIES_MULTI_CHAIN.search(window), (
            f"{path} states ipTM as the binder-to-target interface with no "
            f"multi-chain qualifier near it: ...{window}..."
        )


class _Tooltips(HTMLParser):
    """Every ``data-tooltip`` on the page. HTMLParser unescapes attribute
    values, so the assertions see the string the user's browser shows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tooltips: list = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "data-tooltip" and value:
                self.tooltips.append(value)


def _iptm_tooltip(html: str) -> str:
    parser = _Tooltips()
    parser.feed(html)
    hits = [t for t in parser.tooltips if "Interface pTM" in t]
    assert len(hits) == 1, (
        f"expected exactly one ipTM tooltip, got {len(hits)}: {hits!r}"
    )
    return hits[0]


def test_the_boltzgen_iptm_tooltip_does_not_contradict_itself(flask_app):
    """One string, one meaning.

    components/candidate_table.html CONCATENATES the per-tool legend with the
    global metric_glossary entry into a single ``data-tooltip``. The legend now
    carries the pre-deploy caveat — "an older run stored a complex-wide value
    instead" — and the glossary used to end four words later with "Measures
    structural confidence at the binder–target interface SPECIFICALLY". Two
    statements on one screen, one of them false, is exactly the failure the
    legend rewrite existed to fix; putting them inside a single tooltip is that
    failure at its smallest possible scale.

    The glossary is global — it is shown for every tool's ipTM column — so it
    cannot be the surface that says which interface the number covers. The
    legend can, and does.

    Both halves are asserted present first, because a tooltip that stopped
    stacking them would satisfy the contradiction check by saying nothing.
    """
    tooltip = _iptm_tooltip(_render_results(flask_app, "boltzgen", "A,B"))
    assert "complex-wide" in tooltip, (
        f"the legend half is gone from the tooltip: {tooltip!r}"
    )
    assert "Template Modeling" in tooltip, (
        f"the glossary half is gone from the tooltip: {tooltip!r}"
    )
    assert not re.search(
        r"binder.target interface\s+(specifically|alone|only)", tooltip, re.I,
    ), (
        f"the tooltip disclaims the value as possibly complex-wide and then "
        f"asserts it is the binder-target pair and nothing else: {tooltip!r}"
    )


def test_boltzgen_results_no_longer_carry_the_banner(flask_app):
    """The decision, at the seam a user actually sees.

    The frozenset test above is the unit; this is the page. boltzgen still
    CALLS the macro — templates/tools/boltzgen_results.html is in the
    completeness check below — so the wiring stays and only the shared
    decision function changes. That is deliberate: the page asks, one place
    answers.
    """
    html = _render_results(flask_app, "boltzgen", "A,B")
    assert NOTICE_MARKER not in html, (
        "a boltzgen run gets the banner again; its container reports the "
        "binder-to-target interface, so the banner's mechanism sentence is "
        "false about it"
    )
