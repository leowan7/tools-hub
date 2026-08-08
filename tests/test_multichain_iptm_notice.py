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
# the two discriminators that were checked and do not exist, are in the
# comment above MULTICHAIN_IPTM_UNRELIABLE_TOOLS in shared/score_legends.py.
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
_POINTS_AT_FURNITURE = re.compile(
    r"\bbelow\b|\babove\b|\bon this page\b|\bat the bottom\b|"
    r"\bre-?fold\b[^.]{0,40}\bBoltz",
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
    stored, at least one multi-chain boltzgen run predates the deploy, and
    neither a per-record marker nor a timestamp usable at all six call sites
    exists to tell them apart (both checked; see the comment above
    MULTICHAIN_IPTM_UNRELIABLE_TOOLS). So the caveat moved to the ipTM legend,
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
    of its six call sites it is on. It used to end "re-fold the top candidates
    with Boltz-2 below", which describes the Second-opinion fold panel
    components/results_shell.html draws — a panel two of the six call sites
    never draw:

      * templates/targets/detail.html calls candidate_table directly and has
        no re-fold control anywhere on the page;
      * a job page whose run returned zero candidates renders the notice (it
        is called OUTSIDE results_shell) while results_shell draws the panel
        only inside its non-empty branch.

    Both are checked below, because the second is what makes a per-caller
    parameter the wrong fix: the caller would have to recompute a condition
    that lives inside another macro, from a different value each time.
    """
    target_html = _render_target_page(flask_app, ["rfdiffusion"], "A,B")
    assert NOTICE_MARKER in target_html, "no banner to check"
    assert 'name="dest_tool"' not in target_html, (
        "the pooled target page has grown a re-fold control; this test's "
        "premise no longer holds and the copy decision should be revisited"
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
        f"the banner points at a control that is not on every page it "
        f"renders on: {banner!r}"
    )
    # Same copy everywhere, which is the property that makes one check enough.
    assert _banner_text(job_html) == banner
    assert _banner_text(empty_html) == banner


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
    # Thresholds were calibrated on single-chain runs where the two keys nearly
    # coincide, so they remain the best available anchor and must not drift
    # silently alongside a wording change.
    assert legend["good"] == 0.7
    assert legend["excellent"] == 0.8


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
