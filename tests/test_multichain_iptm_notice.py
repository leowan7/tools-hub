"""ipTM must be marked not-comparable on a multi-chain target.

``llm-proteinDesigner/docs/MULTI-CHAIN-TARGETS.md`` (the SIBLING repo — this
one has no such file) states the defect precisely: ipTM is a MAX over
residues, so a real crystal dimer's own chain-chain interface scores ~0.9 and
"dominates almost independently of binder quality". It is both the displayed
value AND the ranking key (``shared/result_columns.py``), so a mediocre binder
can rank first carrying a plausible-looking number.

Every test here asserts BOTH directions. A presence-only test passes against a
banner that renders unconditionally, which would put a scary caveat on every
single-chain run — the far more common case — and train users to ignore it.
"""
from __future__ import annotations

import os
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from shared.score_legends import (
    MULTICHAIN_IPTM_UNRELIABLE_TOOLS,
    multichain_iptm_unreliable,
)

pytestmark = pytest.mark.usefixtures("isolate_supabase")

NOTICE_MARKER = "data-multichain-iptm-notice"
BANNER_TOOLS = ("rfdiffusion", "pxdesign", "bindcraft", "boltzgen")


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


@pytest.mark.parametrize("tool", ["proteina", "rfantibody", "boltz2", "", None])
def test_unaffected_tools_are_never_flagged(tool):
    """proteina reports af2_iptm from a different scoring path and rfantibody
    cannot take a multi-chain target at all. Warning on either would be noise,
    and noise is what makes a real warning ignorable."""
    assert multichain_iptm_unreliable(tool, "A,B") is False


def test_a_pooled_table_is_flagged_if_any_tool_is_affected():
    """The target page pools several tools into one table."""
    assert multichain_iptm_unreliable(["proteina", "rfdiffusion"], "A,B") is True
    assert multichain_iptm_unreliable(["proteina"], "A,B") is False
    assert multichain_iptm_unreliable([], "A,B") is False


def test_boltzgen_is_still_in_the_set_until_its_fix_deploys():
    """boltzgen's real fix is llm-proteinDesigner PR #18 (design_iptm first in
    IPTM_KEYS). Until that is MERGED AND DEPLOYED the running container still
    reports the complex-wide value, so the notice has to stay.

    This test is the reminder. When the deploy lands, drop "boltzgen" from
    MULTICHAIN_IPTM_UNRELIABLE_TOOLS and delete this test, citing the deploy.
    """
    assert "boltzgen" in MULTICHAIN_IPTM_UNRELIABLE_TOOLS


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
    assert "maximum over residues" in body


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
# The macro is wired everywhere a candidate table is drawn
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", [
    "tools/rfdiffusion_results.html",
    "tools/pxdesign_results.html",
    "tools/bindcraft_results.html",
    "tools/boltzgen_results.html",
    "runs/detail.html",
    "targets/detail.html",
])
def test_every_candidate_table_page_calls_the_notice(template, flask_app):
    """A completeness check. The campaign page and the pooled target page draw
    the same ipTM column from the same tools; a notice on the job page alone
    would leave the two views a user actually compares runs in uncovered."""
    path = flask_app.jinja_env.get_or_select_template(template).filename
    body = open(path, encoding="utf-8").read()
    assert "multichain_iptm_notice(" in body, (
        f"{template} renders a candidate table but never calls the notice"
    )


def test_the_boltzgen_legend_does_not_outrun_the_deploy(flask_app):
    """The tooltip must not claim a fix that has not shipped.

    An earlier draft of this file asserted the opposite — that the legend
    names ``design_iptm``, "the binder-to-target interface". That is true only
    once llm-proteinDesigner#18 is merged AND DEPLOYED; until then the
    container still emits the complex-wide value, which is precisely why
    boltzgen is in MULTICHAIN_IPTM_UNRELIABLE_TOOLS. Asserting it early pinned
    a tooltip that contradicted the banner rendered directly above the same
    column on the same screen, and a test that pins a false claim is worse
    than no test at all.

    When the deploy lands, this test and the frozenset entry move together.
    """
    from shared.score_legends import (
        MULTICHAIN_IPTM_UNRELIABLE_TOOLS, get_legend,
    )

    legend = get_legend("boltzgen", "ipTM")
    assert "design_iptm" not in legend["explanation"], (
        "the legend claims a value the deployed container does not emit"
    )
    assert "chain-chain" in legend["explanation"], (
        "the legend must say the multi-chain number is not binder-only"
    )
    # The two must move together: while boltzgen is warned about, the legend
    # must not describe its number as binder-to-target only.
    assert "boltzgen" in MULTICHAIN_IPTM_UNRELIABLE_TOOLS
    # Thresholds were calibrated on single-chain runs where the two keys nearly
    # coincide, so they remain the best available anchor and must not drift
    # silently alongside a wording change.
    assert legend["good"] == 0.7
    assert legend["excellent"] == 0.8
