"""BoltzGen's ipTM legend may not carry a bar from a different measurement.

BoltzGen scores with Boltz-2 weights, so the boltz2 legend sitting directly
below it in shared/score_legends.py looks like the obvious thing to copy —
and that is what happened: ``good`` 0.7 / ``excellent`` 0.8, plus an
explanation asserting "Above 0.7 is a credible binder".

Those are CO-FOLD bars. On the audited run BoltzGen does not cofold: its one
refold folds the binder on its own, so it has no interface to score — all
seven of its numeric ``designfolding-*iptm`` columns read 0.0 and its
``designfolding-min_interaction_pae`` is the 100000.25 "no interaction"
sentinel. What reaches this legend is instead the generator's own confidence
head reading its own output. THAT is the objection: the wrong ruler.

THE REACH ARGUMENT IS SEPARATE, AND MUST BE SCOPED AND TAKEN OFF THE RIGHT
COLUMN. On the audited 100-design replicate:

    design_to_target_iptm  (what this legend describes)  0.084-0.583, 0/100
    bare iptm              (what "460 designs, max 0.650" is)  0.450-0.649

The second is an interface-pTM averaged over EVERY chain pair, so on a
homodimer target it carries the target's own crystal interface. Cite the
first. Quoting the 460 figure to justify this legend would reproduce, inside
the justification, the wrong-quantity error the legend exists to fix.

Even on the right column that is a property of those runs, not of the metric,
and the unscoped version ("it can never reach 0.7") is false twice over:

  * the same self-hosted pipeline on peptide-anything reaches 0.777, with
    16/36 over 0.70  (boltzgen-workspace/mdm2-peptide)
  * the HOSTED Boltz API clears 0.70 routinely, max 0.983 (42.5% over 0.70 on
    the matched 3AVE/miniprotein subset; 60.3% across all hosted designs). A
    different SERVICE, aggregated retrospectively from campaign manifests by
    feld1/12_engine_evidence.py -- not a control run by 11_engine_audit.py,
    which makes no HTTP call at all. Its records DO carry an `iptm` field, and
    whether that is the same quantity as ours is explicitly open
    (11_engine_audit.py says so). Keep the populations apart for that reason,
    not because we know they differ.

Pooling those populations is how a reviewer talks themselves into either
"the bar was fine" or "the premise collapsed". Neither follows. The bar comes
off because 0.70 is calibrated on a cofold and this is not one.

For contrast, the same campaign's designs re-scored on a real Boltz-2 cofold
span 0.166-0.806 (29 rows, 1 over 0.70) on `binder_to_target` — the
per-chain-pair column feld1/13_boltz_cofold.py exists to read, precisely
because it cannot pick up target-internal contacts. Its complex-wide sibling
`cofold_iptm` reads 0.263-0.852 and is the wrong column here for the same
reason the 460 figure is. Do not join either to the audit by spec id; they
are separate stochastic runs.

Pinned here because ``good``/``excellent`` are inert today (only
``explanation`` and ``caveat`` render), which is exactly what lets a wrong
value sit unnoticed until someone wires the field up to a colour.
"""
from __future__ import annotations

import re
from html import unescape
from pathlib import Path

from shared.score_legends import (
    SCORE_LEGENDS,
    get_legend,
    legend_text,
    score_legends_for,
)

BOLTZGEN_IPTM = ("boltzgen", "ipTM")
BOLTZ2_IPTM = ("boltz2", "ipTM")


def test_boltzgen_iptm_asserts_no_bar():
    legend = SCORE_LEGENDS[BOLTZGEN_IPTM]
    assert "good" not in legend and "excellent" not in legend, (
        f"boltzgen ipTM claims good={legend.get('good')} / "
        f"excellent={legend.get('excellent')}. Nothing pairs the in-run number "
        f"against a cofold on the same designs, so there is no bar to state. "
        f"Omit it rather than invent one."
    )


def test_boltzgen_iptm_does_not_inherit_the_boltz2_cofold_scale():
    """The specific copy that was made. boltz2 keeps its bars — it IS the
    calibrated cofold — so this compares against the live values rather than
    against a hardcoded 0.7, and keeps holding if boltz2 is ever recalibrated.
    """
    cofold = SCORE_LEGENDS[BOLTZ2_IPTM]
    assert cofold.get("good") and cofold.get("excellent"), (
        "boltz2 ipTM lost its bars, so this test compares against nothing; "
        "re-point it rather than leave it passing"
    )
    text = legend_text(SCORE_LEGENDS[BOLTZGEN_IPTM])
    for value in (cofold["good"], cofold["excellent"]):
        assert not re.search(
            rf"above\s+{re.escape(str(value))}", text, re.I
        ), (
            f"boltzgen ipTM text promises a value above {value}, the Boltz-2 "
            f"cofold bar, for a number measured on a different kind of fold"
        )


def test_boltzgen_iptm_says_the_cofold_scale_does_not_apply():
    """Removing the false claim is not the same as telling the truth. A user
    reading 0.42 needs to know it is not the 0.7-scale number they know from
    every other tool here, or they will apply that scale themselves."""
    text = legend_text(SCORE_LEGENDS[BOLTZGEN_IPTM]).lower()
    assert "cofold" in text, text
    assert "0.7" in text, text


def test_the_legends_measured_on_the_refold_keep_their_bars():
    """The reason ipTM alone drops its bar is that ipTM alone has no reading
    of its own kind. pLDDT and refolding RMSD are measured on the design-only
    refold, so each is about the binder and nothing else — which is what 80
    and 1.5 A were calibrated on. Dropping their bars too would be
    over-correcting, and this pins that it did not happen."""
    for column in ("pLDDT", "refolding_rmsd"):
        legend = SCORE_LEGENDS[("boltzgen", column)]
        assert legend.get("good") is not None, column
        assert legend.get("excellent") is not None, column


# ---------------------------------------------------------------------------
# The bar also has a GLOBAL home, and three tool-scoped surfaces render it
# ---------------------------------------------------------------------------
# Clearing ``good`` off the legend is not enough on its own. The same bar lives
# a second time in shared/metric_glossary.py as GLOSSARY["ipTM"]["good_range"]
# = "> 0.75 strong; > 0.65 acceptable", which is keyed by METRIC and is global,
# and three templates that DO know which tool they are rendering print it:
#
#   components/candidate_table.html  stacks it onto the per-tool legend, in one
#                                    tooltip, on the results table itself
#   components/about_panel.html      the form page, before the user pays
#   help/tool_guide.html             the page that teaches how to read the score
#
# So before this, a boltzgen user read "so 0.7 does not apply" and then, four
# words later in the same tooltip, "Range: > 0.75 strong; > 0.65 acceptable".
# The glossary entry is correct for every other tool and stays; the three
# tool-scoped surfaces suppress it when the tool's own legend declines a bar.
#
# The global bar is READ here, never retyped, so recalibrating the glossary
# cannot leave these passing against a number the product no longer uses.

import pytest  # noqa: E402
from jinja2 import Environment, FileSystemLoader  # noqa: E402

import app as _app  # noqa: E402,F401  (populates tools.base._REGISTRY)
from shared import metric_glossary, ranking  # noqa: E402
from tools import base as tool_base  # noqa: E402

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
_GLOBAL_IPTM_RANGE = metric_glossary.GLOSSARY["ipTM"]["good_range"]


@pytest.fixture
def _app_client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    for adapter in tool_base.all_adapters():
        monkeypatch.setenv(
            "FLAG_TOOL_" + adapter.slug.upper().replace("-", "_"), "on"
        )
    flask_app = _app.create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _column_tooltip(tool_slug: str) -> str:
    """The assembled ipTM header tooltip for one tool, as the page emits it."""
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    env.globals.update(
        metric_glossary=metric_glossary.GLOSSARY,
        score_legends_for=score_legends_for,
        format_metric_value=metric_glossary.format_value,
        score_legend_for=get_legend,
        legend_text=legend_text,
        ordinal=ranking.ordinal,
        csrf_input=lambda: "",
        url_for=lambda _endpoint, **kw: "/static/" + kw.get("filename", ""),
    )
    tmpl = env.from_string(
        '{% from "components/candidate_table.html" import candidate_table %}'
        "{{ candidate_table(candidates, columns, job_id, tool_slug, clone_url,"
        "  campaign_id, target_id, multi_tool, sort_mode, split_tools, per_tool) }}"
    )
    html = tmpl.render(
        candidates=[], columns=["ipTM"], job_id="j1", tool_slug=tool_slug,
        clone_url="", campaign_id="", target_id="", multi_tool=False,
        sort_mode="", split_tools=(), per_tool={},
    )
    match = re.search(r'data-tooltip="([^"]*)"', html)
    assert match, f"no ipTM tooltip rendered for {tool_slug}"
    return unescape(match.group(1))


def test_the_results_tooltip_does_not_re_add_the_bar_from_the_glossary():
    """The flat contradiction: one tooltip saying 0.7 does not apply and then
    quoting a 0.65-0.75 band. This is the surface that shows the numbers the
    bar judges, so it is the one that turned an ordinary run into a failure."""
    tooltip = _column_tooltip("boltzgen")
    assert _GLOBAL_IPTM_RANGE not in tooltip, (
        f"the global ipTM range {_GLOBAL_IPTM_RANGE!r} is stacked onto "
        f"boltzgen's own legend, which has just finished saying that scale "
        f"does not apply:\n\n{tooltip}"
    )


def test_the_tooltip_keeps_the_definition_and_the_citation():
    """Suppressing the BAR is not suppressing the glossary. The definition and
    the AlphaFold-Multimer citation are true of ipTM wherever it appears, and
    dropping the whole entry would be the over-correction."""
    tooltip = _column_tooltip("boltzgen")
    glossary = metric_glossary.GLOSSARY["ipTM"]
    assert glossary["definition"] in tooltip, tooltip
    assert glossary["citation"] in tooltip, tooltip
    # ...and the seam where the range was removed is not left ".." or " ."
    assert ".." not in tooltip and " ." not in tooltip, tooltip


def test_a_tool_that_states_a_bar_still_gets_the_global_range():
    """The control. boltz2 IS the calibrated cofold, so the band is true for
    it — a fix that stripped the range from every tool would pass the test
    above and quietly cost every other tool its answer to "what is good?"."""
    tooltip = _column_tooltip("boltz2")
    assert _GLOBAL_IPTM_RANGE in tooltip, tooltip


def test_boltzgen_is_the_only_legend_without_a_bar():
    """Pins the blast radius of the three template conditions. If a second
    legend ever drops its bar, that tool's surfaces change too — which may be
    right, but it should be a decision, not a surprise."""
    barless = {k for k, v in SCORE_LEGENDS.items() if "good" not in v}
    assert barless == {BOLTZGEN_IPTM}, barless


def test_the_form_page_does_not_quote_the_band_to_a_boltzgen_user(_app_client):
    """about_panel, on the page where the user decides whether to pay. It said
    tools sit "a little either side" of > 0.65 — a band from a cofold, which
    is not the measurement boltzgen's number comes from."""
    resp = _app_client.get("/tools/boltzgen")
    assert resp.status_code == 200, resp.status_code
    body = unescape(resp.get_data(as_text=True))
    assert _GLOBAL_IPTM_RANGE not in body, (
        f"the boltzgen form page quotes the global ipTM band "
        f"{_GLOBAL_IPTM_RANGE!r} as if boltzgen sat near it"
    )


def test_the_tool_guide_does_not_quote_the_band_to_a_boltzgen_user(_app_client):
    """help/tool_guide, the page that teaches people how to read the score."""
    resp = _app_client.get("/help/tools/boltzgen")
    assert resp.status_code == 200, resp.status_code
    body = unescape(resp.get_data(as_text=True))
    assert _GLOBAL_IPTM_RANGE not in body, (
        f"the boltzgen guide quotes the global ipTM band "
        f"{_GLOBAL_IPTM_RANGE!r} as this tool's acceptable range"
    )


def test_another_tool_s_guide_still_quotes_the_band(_app_client):
    """Control for the two above, for the same reason as the tooltip control:
    these pages are shared by every tool and must keep working for them."""
    resp = _app_client.get("/help/tools/boltz2")
    assert resp.status_code == 200, resp.status_code
    assert _GLOBAL_IPTM_RANGE in unescape(resp.get_data(as_text=True))
