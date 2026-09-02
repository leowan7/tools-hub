"""The bar is applied at READ time and no stored verdict is consulted.

Four production defects lived in the verdict layer and none in the
measurements (see the block comment in shared/score_legends.py). Each is
pinned here by BEHAVIOUR -- feed a record a stale label and check the label
does not move the answer -- rather than by a sentence in a docstring. This
repo has a long list of guards that passed while the thing they guarded was
broken; a test that only greps for a word is one of those.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shared.jobs import candidate_meets_bar, count_candidates_meeting_bar
from shared.score_legends import (
    GATE_COLUMNS,
    IMPLAUSIBLE_VALUES,
    gate_bar_text,
    get_legend,
    judge,
    tool_has_bar,
)

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# The declaration is complete and self-consistent
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(GATE_COLUMNS))
def test_every_gate_column_has_a_legend(tool):
    """A gate column carries no bar unless the legend for it exists.

    judge() degrades a legend-less gate column to "unmeasured", which is safe
    but silently drops a leg -- a tool could lose half its bar and every design
    would still read plausibly. This is the check that stops that.
    """
    for column in GATE_COLUMNS[tool]:
        legend = get_legend(tool, column)
        assert legend is not None, f"{tool}/{column} is gated on with no legend"
        assert legend["direction"] in ("higher_is_better", "lower_is_better")
        assert isinstance(legend["good"], (int, float))


@pytest.mark.parametrize("key", sorted(IMPLAUSIBLE_VALUES))
def test_implausible_values_are_declared_on_a_gate_column(key):
    tool, column = key
    assert column in GATE_COLUMNS.get(tool, ()), (
        f"{tool}/{column} declares placeholder values but is not gated on, "
        "so the declaration does nothing"
    )


def test_boltzgen_is_not_gated_on_iptm():
    """The defect that started this. BoltzGen refolds the design ALONE, so its
    ipTM is not the cofold quantity a 0.70 bar describes, and 65 production
    candidates were labelled "below threshold" against it. A legend for
    boltzgen/ipTM exists and MUST NOT be read as a gate leg -- which is exactly
    what "gate on every column that has a legend" would have done.
    """
    assert get_legend("boltzgen", "ipTM") is not None
    assert "ipTM" not in GATE_COLUMNS["boltzgen"]


def test_tools_without_a_bar_are_unjudged_not_failed():
    for tool in ("bindcraft", "proteina", "iggm", "opendde", "mpnn", ""):
        assert not tool_has_bar(tool)
        assert judge(tool, {"scores": {"ipTM": 0.01}}).verdict == "unjudged"


# --------------------------------------------------------------------------
# A stored verdict cannot change the answer
# --------------------------------------------------------------------------

# One record per gating tool that meets every leg of its bar, in the shape
# that tool actually persists. boltz2 is deliberately flat with lowercase root
# keys and a 0-1 pLDDT, because that is what its pipeline writes.
MEETING = {
    "boltzgen": {"scores": {"pLDDT": 88.0, "refolding_rmsd": 1.1}},
    "rfdiffusion": {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "i_pAE": 6.0}},
    "pxdesign": {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "pAE": 3.0}},
    "rfantibody": {"scores": {"pLDDT": 88.0, "ipAE": 6.0, "pAE": 3.0}},
    "boltz2": {"iptm": 0.9, "complex_plddt": 0.93, "n_hotspot_contacts": 6},
    "esmfold2_design": {"scores": {"ipTM": 0.9}},
}


def test_every_gating_tool_has_a_meeting_fixture():
    assert set(MEETING) == set(GATE_COLUMNS), (
        "a tool gained or lost a bar without this file noticing"
    )


@pytest.mark.parametrize("tool", sorted(MEETING))
def test_gating_tools_are_all_covered(tool):
    assert tool_has_bar(tool)


@pytest.mark.parametrize("tool", sorted(MEETING))
def test_a_stale_failing_label_does_not_sink_a_good_design(tool):
    """Defect 1, generalised. 65 BoltzGen candidates carry "below threshold"
    from a bar their container has since dropped, and no edit could correct
    them. A record meeting its bar today reads as meeting it, whatever word
    was frozen onto it.
    """
    record = dict(MEETING[tool])
    assert judge(tool, record).verdict == "meets"
    for stale in ("below threshold", "fail", "drop", "borderline"):
        poisoned = dict(record)
        poisoned["filter_status"] = stale
        poisoned["scores"] = dict(record.get("scores") or {})
        poisoned["scores"]["filter_status"] = stale
        assert judge(tool, poisoned).verdict == "meets"
        assert candidate_meets_bar(tool, poisoned)


@pytest.mark.parametrize("tool", sorted(MEETING))
def test_a_stale_passing_label_does_not_float_a_bad_design(tool):
    """The other direction, which matters more. bindcraft stamps an
    unconditional "pass" and pxdesign ORs the upstream tool's own word into
    its gate, so "pass" was never evidence of anything measured here.
    """
    record = dict(MEETING[tool])
    scores = dict(record.get("scores") or {})
    column = GATE_COLUMNS[tool][0]
    legend = get_legend(tool, column)
    # Push the first leg clearly past its bar, on whichever side is worse.
    worse = 0.5 if legend["direction"] == "higher_is_better" else 4.0
    bad = legend["good"] * worse
    if scores:
        scores[column] = bad
        record["scores"] = scores
    else:
        record[column] = bad
    for stale in ("pass", "strict_pass", "soft_pass"):
        poisoned = dict(record)
        poisoned["filter_status"] = stale
        if scores:
            poisoned["scores"] = dict(scores, filter_status=stale)
        assert judge(tool, poisoned).verdict == "below"
        assert not candidate_meets_bar(tool, poisoned)


# --------------------------------------------------------------------------
# Unmeasured is unjudged, never failed
# --------------------------------------------------------------------------

def test_recovered_record_missing_an_end_of_run_metric_is_unjudged():
    """Defect 2. shared/job_recovery rebuilds a lost result from records
    streamed DURING the run, and boltzgen's refold happens at the end, so the
    rebuilt record has pLDDT and no refolding_rmsd. 50 stored candidates froze
    a verdict about two measurements while carrying one.
    """
    verdict = judge("boltzgen", {"scores": {"pLDDT": 88.0, "ipTM": 0.9}})
    assert verdict.verdict == "unjudged"
    assert verdict.shortfalls == ()
    assert any("RMSD" in u for u in verdict.unmeasured)


def test_recovery_does_not_carry_a_verdict_across():
    """The same defect at its source: the rebuilt candidate must hold
    measurements only."""
    from shared.job_recovery import _candidate_from_partial

    rebuilt = _candidate_from_partial({
        "rank": 1,
        "pdb_key": "designs/d1.pdb",
        "iptm": 0.9,
        "plddt": 88.0,
        "filter_status": "pass",
    })
    assert "filter_status" not in rebuilt["scores"]
    assert "filter_status" not in rebuilt
    assert rebuilt["scores"] == {"ipTM": 0.9, "pLDDT": 88.0}


def test_a_missing_leg_never_falls_through_to_another_column():
    """Defect 3. On BoltzGen's peptide protocol the refold metrics are never
    produced. The bar must go unanswered rather than quietly re-form itself
    around whatever columns are present -- an ipTM of 0.98 does not stand in
    for an RMSD nobody measured.
    """
    verdict = judge("boltzgen", {"scores": {"pLDDT": 88.0, "ipTM": 0.98}})
    assert verdict.verdict == "unjudged"


def test_interface_plddt_does_not_answer_the_plddt_bar():
    """The same fall-through wearing a near-miss column name."""
    assert judge(
        "boltzgen",
        {"scores": {"complex_iplddt": 95.0, "refolding_rmsd": 1.0}},
    ).verdict == "unjudged"


def test_placeholder_rmsd_is_read_as_absent():
    """Defect 4. Two stored candidates carry refolding_rmsd exactly 0.00. It is
    a placeholder, and it clears a "<= 1.5" bar.
    """
    verdict = judge("boltzgen", {"scores": {"pLDDT": 88.0, "refolding_rmsd": 0.0}})
    assert verdict.verdict == "unjudged"
    # A real sub-angstrom refold still passes.
    assert judge(
        "boltzgen", {"scores": {"pLDDT": 88.0, "refolding_rmsd": 0.4}},
    ).verdict == "meets"


def test_a_measured_shortfall_beats_an_unmeasured_leg():
    """One leg definitively failing is a fact about the design, whatever else
    went unmeasured."""
    verdict = judge("rfdiffusion", {"scores": {"ipTM": 0.1, "pLDDT": 88.0}})
    assert verdict.verdict == "below"


def test_a_shortfall_names_the_reading_and_the_bar():
    """The cell has to carry a checkable fact, not a word. "below threshold"
    told a reader nothing they could act on."""
    verdict = judge("boltzgen", {"scores": {"pLDDT": 72.4, "refolding_rmsd": 1.0}})
    assert verdict.verdict == "below"
    assert verdict.shortfalls == ("pLDDT 72.4, below 80",)


# --------------------------------------------------------------------------
# Scale, and the two counting conventions
# --------------------------------------------------------------------------

def test_plddt_is_compared_on_the_scale_the_bar_is_written_on():
    """boltz2 stores pLDDT 0-1 and every legend is written for 0-100. Comparing
    0.93 against 80 would fail every boltz2 design ever run."""
    assert judge(
        "boltz2",
        {"iptm": 0.9, "complex_plddt": 0.93, "n_hotspot_contacts": 6},
    ).verdict == "meets"
    assert judge(
        "boltz2",
        {"iptm": 0.9, "complex_plddt": 93.0, "n_hotspot_contacts": 6},
    ).verdict == "meets"


def test_counting_needs_evidence_for_and_ordering_needs_evidence_against():
    """The one asymmetry in this design, and it is deliberate.

    ``count_candidates_meeting_bar`` (the campaign card) counts only designs
    shown to MEET the bar. ``shared.ranking`` sinks only designs shown to FALL
    SHORT. An unjudged design is excluded there and kept here.
    """
    result = {"candidates": [
        {"scores": {"pLDDT": 88.0, "refolding_rmsd": 1.0}},   # meets
        {"scores": {"pLDDT": 88.0}},                          # unjudged
        {"scores": {"pLDDT": 40.0, "refolding_rmsd": 1.0}},   # below
    ]}
    assert count_candidates_meeting_bar(result, "boltzgen") == 1

    from shared.ranking import annotate_rows

    rows = [dict(c, _source_tool="boltzgen") for c in result["candidates"]]
    passed = [r["_passed"] for r in annotate_rows(rows)]
    assert passed == [True, True, False]


def test_a_tool_with_no_bar_counts_every_delivered_design():
    result = {"candidates": [{"scores": {}}, {"scores": {}}]}
    assert count_candidates_meeting_bar(result, "bindcraft") == 2
    assert count_candidates_meeting_bar(result, "opendde") == 2


def test_opendde_score_availability_marker_is_not_read_as_a_verdict():
    """opendde persists "scored" / "no_score", a statement about whether a
    score was AVAILABLE. Read as a verdict, neither is a pass, so every stored
    opendde result counted zero designs through."""
    result = {"designs": [
        {"filter_status": "scored", "ranking": 0.9},
        {"filter_status": "no_score"},
    ]}
    assert count_candidates_meeting_bar(result, "opendde") == 2


# --------------------------------------------------------------------------
# The word cannot come back
# --------------------------------------------------------------------------

_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def test_no_template_expression_reads_filter_status():
    """A grep, and it is the WEAKEST check in this file on purpose -- it only
    catches the word coming back to a template. It is scoped to Jinja
    expressions so a comment explaining the history does not trip it, and the
    behavioural tests above are what actually pin the mechanism.
    """
    offenders = []
    for path in (REPO / "templates").rglob("*.html"):
        for expr in _JINJA.findall(path.read_text(encoding="utf-8")):
            if "filter_status" in expr:
                offenders.append(
                    f"{path.relative_to(REPO)}: {expr.strip()[:80]}"
                )
    assert not offenders, (
        "templates read a stored verdict again:\n" + "\n".join(offenders)
    )


def test_the_banner_states_a_measurable_fact():
    """The all-designs-fell-short banner has to name the bar it is talking
    about. "All designs fell below quality thresholds" is a verdict whose
    thresholds the reader cannot see; "no design here reaches pLDDT 80" is
    checkable."""
    shell = (REPO / "templates/components/results_shell.html").read_text(
        encoding="utf-8",
    )
    assert "No design here reaches {{ gate_bar_text(tool_slug) }}" in shell
    # Jinja comments stripped: the block above the banner quotes the old
    # wording to say why it went, and a check that cannot tell an explanation
    # from the thing it explains would forbid ever writing one down.
    rendered = _JINJA_COMMENT.sub("", shell)
    assert "fell below quality thresholds" not in rendered

    assert gate_bar_text("boltzgen").startswith("pLDDT 80")
    assert "1.5" in gate_bar_text("boltzgen")
    assert gate_bar_text("bindcraft") == ""
