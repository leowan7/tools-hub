"""A results-table column tooltip states its bar once, in the words that judge it.

The header tooltip in components/candidate_table.html is assembled from two
sources: the PER-TOOL legend (shared/score_legends.py) and the GLOBAL glossary
entry for that metric (shared/metric_glossary.py), stacked into one string. The
glossary half used to contribute a trailing ``Range: ...`` whenever the legend
stated a bar, so the same bar was written twice in one tooltip -- once by the
tool and once by a global string keyed only on the metric name. The two
spellings had drifted apart in both directions:

  COMPARATOR. ``judge()`` (shared/score_legends.py:2172) is
  ``meets = seen <= good if lower_is_better else seen >= good``, so a value
  sitting exactly on the bar MEETS it, and every gating legend words it that
  way ("80 or more", "1.5 angstroms or less", "4 or more of 7"). So does the
  ``against_bar`` glossary definition, in prose the user reads: "a value
  exactly ON a bar counts as meeting it". The Range said "> 80", "< 1.5",
  "> 4". Measured across GATE_COLUMNS x SCORE_LEGENDS x GLOSSARY, 10 gate legs
  printed a strict comparator against the very number their own legend's
  ``good`` holds, so a design ON the bar read as meeting it in the cell and
  short of it four words later in the same tooltip. The whole-number case is
  the one a user hits: boltz2's hotspot count is an integer out of 7, where
  "4 or more" and "> 4" differ by a whole design.

  BAND. GLOSSARY["pLDDT"] calls "> 80" very high confidence. All six legends
  on the pLDDT column reserve that tier for 90 and up ("80 or more is
  confidently folded; 90 or more is high confidence"), as does the AlphaFold2
  paper the entry cites.

THE FIX IS SUBTRACTION, NOT REWORDING, and this file pins both halves of it.
The condition became ``not leg``: the global Range renders exactly where the
tool has no legend of its own, which is the only place it is the reader's only
answer to "what is good?".

WHY DROPPING IT COSTS THE READER NOTHING, because that was the live objection
and it has to be answered with a check rather than an assurance:
``test_a_suppressed_tooltip_still_says_what_good_is`` renders every suppressed
pair and asserts the bar number is in the LEGEND's own half of the tooltip,
not merely somewhere in the assembled string. It does,
for every pair whose Range stated a number at all. A previous control in
tests/test_boltzgen_iptm_has_no_cofold_bar.py asserted the opposite -- that a
tool WITH a bar still gets the global Range -- against exactly this worry; it
pinned the mechanism rather than the property, and was repointed at the
property when this landed.

SCOPE, stated exactly. This file checks the RESULTS-TABLE tooltip only. Three
other templates print ``good_range`` -- components/about_panel.html,
help/faq.html and help/tool_guide.html -- and none of them is changed: each
names the ``ipTM`` or ``recovery`` entry explicitly, and neither of those two
strings carries the drift. ipTM DOES gate four tools (boltz2, esmfold2-design,
pxdesign, rfdiffusion), but its Range reads "0.65 to 0.75 depending on the
tool" and states no comparator at all; recovery's states two ("> 0.4",
"> 0.6") but is a leg of no tool's bar, so judge() never runs against it.
tests/test_boltzgen_iptm_has_no_cofold_bar.py pins the about_panel and
tool_guide surfaces; the faq one is pinned by test_public_tool_pages.py.

The scan is driven by every glossary metric that COULD print a Range -- the
Range is nested inside ``{% if g.get('definition') %}``, so an entry without a
definition never prints one however the condition is written -- crossed with
every adapter slug, so a pair is covered whether or not it is a display column
today. Numbers are read out of the legend and the glossary, never retyped, so
recalibrating a bar cannot leave this passing against a value the product no
longer uses.
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

import app as _app  # noqa: F401  (import populates tools.base._REGISTRY)
from shared import metric_glossary, ranking, score_legends
from shared.score_legends import get_legend, legend_text, score_legends_for
from tools import base as tool_base

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

#: Adapter slugs, resolved once. ``import app`` above is what populates the
#: registry; without it this is empty and every assertion below is vacuous,
#: which ``test_the_scan_reaches_real_tooltips`` refuses.
_SLUGS = sorted(a.slug for a in tool_base.all_adapters())

#: Metrics whose glossary entry can reach a tooltip at all.
_RANGED_METRICS = sorted(
    metric
    for metric, entry in metric_glossary.GLOSSARY.items()
    if entry.get("good_range") and entry.get("definition")
)

#: A strict comparator immediately before a number, which is the spelling that
#: disagrees with ``judge()``. ``(?!=)`` keeps ">=" and "<=" out.
#
# ponytail: SYMBOLS ONLY, AND THE CEILING IS MEASURED, not guessed. Sixteen
# legends word a bar strictly in WORDS against their own ``good`` -- af2 iptm /
# mean_pLDDT / ptm, bindcraft RMSD / ipTM / pLDDT / shape_complementarity,
# boltz2 pTM, colabfold iptm / mean_pLDDT / ptm, esmfold mean_pLDDT, mpnn
# recovery / score, pxdesign pAE, rfdiffusion RMSD -- e.g. "Above 80 is
# confidently folded" beside ``good`` 80. This pattern does not see any of
# them.
#
# That is deliberate and it is not a hole today: NONE of those sixteen is a
# leg of its tool's GATE_COLUMNS, so ``judge()`` never runs against those
# numbers and no rendered verdict contradicts the sentence. The defect this
# file exists for is a tooltip disagreeing with the verdict in its own cell,
# which needs a gate leg to happen.
#
# Widening to the word forms would turn all sixteen red for no defect, which
# is how this repo reached its shelf of guards that certify false. If one of
# those columns ever becomes a gate leg, the durable check is a GATE-SCOPED
# one -- every leg of GATE_COLUMNS words its bar the way judge() reads it --
# not a longer pattern. Read a pass here as "no symbol comparator sits on a
# judged number", never as "every tooltip words its bar inclusively".
_STRICT = re.compile(r"([<>])(?!=)\s*(\d+(?:\.\d+)?)")

#: The 10 gate legs this file was written for: a leg of some tool's quality
#: bar whose glossary Range printed a strict comparator on that leg's own
#: ``good``. Each must now render NO Range. Hardcoded rather than recomputed,
#: because a set derived from the same data the assertion reads would go
#: vacuous in step with it.
_EXPECTED_SUPPRESSED = {
    ("boltz2", "pLDDT"),
    ("boltz2", "n_hotspot_contacts"),
    ("boltzgen", "pLDDT"),
    ("boltzgen", "refolding_rmsd"),
    ("pxdesign", "pLDDT"),
    ("rfantibody", "ipAE"),
    ("rfantibody", "pAE"),
    ("rfantibody", "pLDDT"),
    ("rfdiffusion", "i_pAE"),
    ("rfdiffusion", "pLDDT"),
}

#: The control, and the reason ``not leg`` is not ``False``. These are the
#: display columns carrying a glossary Range and NO per-tool legend, so the
#: global string is the reader's only answer and must survive. Measured over
#: shared/result_columns.py plus the column lists the per-tool results
#: templates set inline. A fix that stripped the Range unconditionally would
#: pass every other test in this file and silently cost these eight.
_EXPECTED_KEEPS_RANGE = {
    ("boltz2", "against_bar"),
    ("boltzgen", "against_bar"),
    ("esmfold", "pTM"),
    ("opendde", "ranking_score"),
    ("proteina", "total_reward"),
    ("pxdesign", "against_bar"),
    ("rfantibody", "against_bar"),
    ("rfdiffusion", "against_bar"),
}


def _bar_as_written(good: float | int) -> str:
    """``good`` the way a legend spells it: 10.0 is written "10", 1.5 "1.5"."""
    return f"{float(good):g}"


@pytest.fixture(scope="module")
def tooltip():
    """Render one column header tooltip, as the results page emits it.

    The real macro, not a Python restatement of its condition -- the whole
    defect lived in template logic, so a check that never renders the template
    could not have seen it. Environment and compiled template are built once;
    each call renders the macro for a single (tool, column) pair.

    Globals mirror the harness in tests/test_boltzgen_iptm_has_no_cofold_bar.py.
    The candidate list is empty on purpose: only the header is under test, and
    the per-row cell loop needs fixtures the header does not.
    """
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    env.globals.update(
        metric_glossary=metric_glossary.GLOSSARY,
        score_legends_for=score_legends_for,
        format_metric_value=metric_glossary.format_value,
        score_legend_for=get_legend,
        legend_text=legend_text,
        ordinal=ranking.ordinal,
        judge_design=score_legends.judge,
        verdict_text=score_legends.verdict_text,
        gate_bar_text=score_legends.gate_bar_text,
        shortfall_bar_text=score_legends.shortfall_bar_text,
        tool_has_bar=score_legends.tool_has_bar,
        raw_metric=score_legends.raw_metric,
        csrf_input=lambda: "",
        url_for=lambda _endpoint, **kw: "/static/" + kw.get("filename", ""),
    )
    tmpl = env.from_string(
        '{% from "components/candidate_table.html" import candidate_table %}'
        "{{ candidate_table(candidates, columns, job_id, tool_slug, clone_url,"
        "  campaign_id, target_id, multi_tool, sort_mode, split_tools, per_tool) }}"
    )

    def render(tool_slug: str, column: str) -> str:
        html = tmpl.render(
            candidates=[], columns=[column], job_id="j1", tool_slug=tool_slug,
            clone_url="", campaign_id="", target_id="", multi_tool=False,
            sort_mode="", split_tools=(), per_tool={},
        )
        match = re.search(r'data-tooltip="([^"]*)"', html)
        return unescape(match.group(1)) if match else ""

    return render


def test_the_scan_reaches_real_tooltips(tooltip):
    """Anti-vacuity. Every assertion below passes over an empty scan."""
    assert len(_SLUGS) >= 14, (
        f"adapter registry holds {len(_SLUGS)} tools; a registry that did not "
        "populate makes every assertion in this file vacuous"
    )
    assert len(_RANGED_METRICS) >= 8, _RANGED_METRICS
    rendered = sum(
        1 for slug in _SLUGS for metric in _RANGED_METRICS if tooltip(slug, metric)
    )
    assert rendered >= 50, (
        f"only {rendered} tooltips rendered across "
        f"{len(_SLUGS)}x{len(_RANGED_METRICS)} pairs; the macro stopped "
        "emitting data-tooltip and this file is checking nothing"
    )


def test_no_tooltip_contradicts_its_own_legend_bar(tooltip):
    """The invariant, stated on the property rather than on the mechanism.

    Catches a reworded Range as well as a re-added one: what is rejected is a
    STRICT comparator printed against the very number ``judge`` compares
    inclusively, wherever in the tooltip it appears.
    """
    wrong = []
    for slug in _SLUGS:
        for metric in _RANGED_METRICS:
            legend = get_legend(slug, metric) or {}
            good = legend.get("good")
            if good is None:
                continue
            text = tooltip(slug, metric)
            hits = [
                f"{cmp_}{num}"
                for cmp_, num in _STRICT.findall(text)
                if float(num) == float(good)
            ]
            if hits:
                wrong.append(f"({slug}, {metric}) good={good!r} states {hits}: {text}")
    assert not wrong, (
        "a column tooltip prints a strict comparator on the same number "
        "judge() compares inclusively, so a design exactly on the bar reads "
        "as meeting it in the cell and short of it in the tooltip:\n  "
        + "\n  ".join(wrong)
    )


@pytest.mark.parametrize(("slug", "metric"), sorted(_EXPECTED_SUPPRESSED))
def test_a_gate_leg_does_not_restate_its_bar_from_the_glossary(tooltip, slug, metric):
    """The mechanism, on the 10 legs that carried the defect."""
    entry = metric_glossary.GLOSSARY[metric]
    text = tooltip(slug, metric)
    assert text, f"no tooltip rendered for ({slug}, {metric})"
    assert entry["good_range"] not in text, (
        f"the global {metric} range {entry['good_range']!r} is stacked onto "
        f"{slug}'s own legend, which has just stated the same bar:\n\n{text}"
    )
    assert "Range:" not in text, text


@pytest.mark.parametrize(("slug", "metric"), sorted(_EXPECTED_KEEPS_RANGE))
def test_a_column_with_no_legend_keeps_the_global_range(tooltip, slug, metric):
    """The control. ``not leg`` has to keep meaning ``not leg``.

    These columns have no per-tool legend, so the glossary Range is the only
    thing on the page that answers "what is good?" for them.
    """
    assert get_legend(slug, metric) is None, (
        f"({slug}, {metric}) grew a legend; it is pinned here as a column "
        "that has none, so this control no longer tests what it claims"
    )
    text = tooltip(slug, metric)
    assert metric_glossary.GLOSSARY[metric]["good_range"] in text, (
        f"({slug}, {metric}) has no legend of its own and lost the global "
        f"Range, so nothing on the page says what a good value is:\n\n{text}"
    )


def test_the_range_renders_exactly_where_there_is_no_legend(tooltip):
    """The condition itself, over every pair, not over a sample of them.

    The two tests above name 10 suppressed pairs and 8 that keep the Range.
    The macro renders far more than 18: a fix that restored the Range on one
    of the unnamed legend pairs -- ipTM on boltz2, pxdesign and rfdiffusion
    among them -- would pass every other test in this file, because ipTM's
    Range states no comparator for
    ``test_no_tooltip_contradicts_its_own_legend_bar`` to object to. So state
    the biconditional: ``Range:`` appears if and only if the pair has no
    legend of its own.

    The two hardcoded sets are the anti-vacuity floor. A scan that stopped
    reaching legends, or stopped reaching legend-less columns, would satisfy
    an empty biconditional; it cannot satisfy these.
    """
    legended, legendless, stray, lost = set(), set(), [], []
    for slug in _SLUGS:
        for metric in _RANGED_METRICS:
            text = tooltip(slug, metric)
            if not text:
                continue
            has_range = "Range:" in text
            if get_legend(slug, metric) is None:
                legendless.add((slug, metric))
                if not has_range:
                    lost.append(f"({slug}, {metric}): {text}")
            else:
                legended.add((slug, metric))
                if has_range:
                    stray.append(f"({slug}, {metric}): {text}")
    assert _EXPECTED_SUPPRESSED <= legended, (
        "the scan no longer reaches every pair this file was written for: "
        f"{sorted(_EXPECTED_SUPPRESSED - legended)}"
    )
    assert _EXPECTED_KEEPS_RANGE <= legendless, (
        "the scan no longer reaches the legend-less controls: "
        f"{sorted(_EXPECTED_KEEPS_RANGE - legendless)}"
    )
    assert not stray, (
        f"{len(stray)} of {len(legended)} tooltips whose tool has a legend "
        "stack the global Range on top of it, which is the duplicate bar "
        "this change removed:\n  " + "\n  ".join(stray)
    )
    assert not lost, (
        f"{len(lost)} of {len(legendless)} tooltips have no legend and no "
        "Range either, so nothing on the page says what a good value is:\n  "
        + "\n  ".join(lost)
    )


def test_a_suppressed_tooltip_still_says_what_good_is(tooltip):
    """The property the subtraction had to preserve, checked not assumed.

    Every suppressed pair is a GATE leg, so every one has a numeric ``good``
    and there is no "this legend states no number" case to skip -- a pair that
    lost one would fail the assertion below rather than be waved through,
    which is the right way round: a gate leg with no bar in its own words is
    exactly the tooltip this change could have emptied.
    """
    silent = []
    for slug, metric in sorted(_EXPECTED_SUPPRESSED):
        legend = get_legend(slug, metric) or {}
        good = legend.get("good")
        # The legend's OWN words, not the whole tooltip. The glossary
        # definition and citation are stacked into the same string and both
        # can carry a number (a tolerance in angstroms, a publication year),
        # so a bar matched against the assembled string could be answered by
        # a coincidence rather than by the legend. No pair collides that way
        # today -- all ten bars are in their legend and in none of the
        # glossary halves -- so this is scoping, not a repair. The second
        # branch is what keeps the check a statement about the RENDERED page
        # rather than about score_legends alone: candidate_table.html seeds
        # tooltip_text with legend_text(leg) verbatim before stacking.
        own_words = legend_text(legend) if legend else ""
        text = tooltip(slug, metric)
        if good is None or _bar_as_written(good) not in own_words:
            want = "a bar of its own" if good is None else _bar_as_written(good)
            silent.append(
                f"({slug}, {metric}) dropped the Range and its legend never "
                f"writes {want}: {own_words!r}"
            )
        elif own_words not in text:
            silent.append(
                f"({slug}, {metric}) states its bar in the legend but the "
                f"legend is not in the rendered tooltip: {text!r}"
            )
    assert not silent, (
        "a tooltip lost the global Range without its own legend stating the "
        "bar, which is the one way this change could cost a reader an "
        "answer:\n  " + "\n  ".join(silent)
    )
