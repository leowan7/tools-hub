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
    SCORE_LEGENDS,
    gate_bar_text,
    get_legend,
    judge,
    shortfall_bar_text,
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


def test_every_tool_key_is_a_registered_slug():
    """THE GUARD THAT WAS NOT HERE, AND IT COST THE WHOLE FEATURE FOR ONE TOOL.

    GATE_COLUMNS and the legends shipped keyed on "esmfold2_design", the
    PACKAGE DIRECTORY. The registered slug is "esmfold2-design" with a hyphen,
    which is what the template passes and what job.tool holds. Nothing raised:
    an unknown tool simply has no bar, so every cell on that tool's page
    rendered the bare string "Not measured:" with nothing after it, and the
    test file agreed with the typo because its own fixture used the same key.
    shared/tool_meta.py records the same trap silently dropping a PILOT card
    on this same tool.

    A key here that no tool answers to is dead weight at best and a silently
    disabled bar at worst, so it is an error rather than a shrug.
    """
    import app  # noqa: F401  -- populates the registry; empty without it
    from tools.base import _REGISTRY

    assert _REGISTRY, "the adapter registry is empty, so this checks nothing"
    slugs = set(_REGISTRY)
    keyed = {t for t, _c in SCORE_LEGENDS} | set(GATE_COLUMNS)
    # af2 / colabfold / esmfold / mpnn are predictors with legends and no bar;
    # all four are registered tools, so nothing here is exempt.
    unknown = sorted(keyed - slugs)
    assert not unknown, (
        f"legend/gate keys that are not registered tool slugs: {unknown}. "
        f"Registered: {sorted(slugs)}"
    )


@pytest.mark.parametrize("key", sorted(IMPLAUSIBLE_VALUES))
def test_implausible_values_are_declared_on_a_gate_column(key):
    tool, column = key
    assert column in GATE_COLUMNS.get(tool, ()), (
        f"{tool}/{column} declares placeholder values but is not gated on, "
        "so the declaration does nothing"
    )


def test_pxdesign_is_not_gated_on_pae():
    """The second column with a legend that must not become a gate leg, and
    for a different reason than boltzgen's ipTM. pxdesign's pAE arrives on two
    scales: the container prefers the Angstrom form but falls back to
    PXDesign's [0,1] normalised af2_ipae / af2_pae, and a 0-1 reading clears
    an Angstrom bar of 5 unconditionally. A leg that can silently pass
    everything is worse than no leg."""
    assert get_legend("pxdesign", "pAE") is not None
    assert "pAE" not in GATE_COLUMNS["pxdesign"]
    assert judge(
        "pxdesign", {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "pAE": 0.42}},
    ).verdict == "meets"


# The bars this site applies, against the thresholds the containers apply, as
# claimed in the comment above GATE_COLUMNS. Kept as data here so the claim is
# checked rather than trusted -- an earlier version of that comment listed four
# of the five and asserted it was complete.
CONTAINER_THRESHOLDS = {
    ("boltzgen", "refolding_rmsd"): ("RMSD_THRESHOLD", 2.0),
    ("rfdiffusion", "ipTM"): ("IPTM_THRESHOLD", 0.70),
    ("rfantibody", "pAE"): ("PAE_THRESHOLD", 10.0),
    ("pxdesign", "ipTM"): ("IPTM_THRESHOLD", 0.70),
}
_CONTAINER_REPO = REPO.parent / "llm-proteinDesigner"


def test_the_documented_divergences_are_the_actual_divergences():
    """Every leg where this site's bar differs from its container's is listed.

    The comment above GATE_COLUMNS names five, and a reader is entitled to
    treat that list as exhaustive: a leg missing from it moves delivered work
    with nothing saying why. This checks the four that are a plain container
    constant; boltz2's is the fifth and is checked separately below, because
    its pipeline lives in THIS repo on a different scale.

    Skips when the sibling container repo is not checked out beside this one.
    It is a real cross-repo claim and there is no way to verify it without the
    other repo; asserting it from memory is what put a wrong number here.
    """
    import re

    if not _CONTAINER_REPO.is_dir():
        pytest.skip(f"container repo not present at {_CONTAINER_REPO}")

    for (tool, column), (const, expected) in CONTAINER_THRESHOLDS.items():
        path = _CONTAINER_REPO / "docker" / tool / "run_pipeline.py"
        if not path.is_file():
            pytest.skip(f"{path} not present")
        source = path.read_text(encoding="utf-8", errors="replace")
        found = re.search(rf"^{const}\s*=\s*([0-9.]+)", source, re.M)
        assert found, f"{tool}: {const} not found"
        assert float(found.group(1)) == expected, (
            f"{tool}/{column}: the container now uses {found.group(1)}, not "
            f"{expected}. The divergence table above GATE_COLUMNS is stale."
        )
        legend = get_legend(tool, column)
        assert legend is not None and float(legend["good"]) != expected, (
            f"{tool}/{column} no longer diverges; drop it from the table."
        )


def test_boltz2s_plddt_bar_is_looser_than_its_own_pipelines():
    """The fifth divergence, and the only one whose pipeline is in this repo.

    STRICT_PLDDT is written on the 0-1 scale boltz2 stores and this site
    renders everything on 0-100, so the two numbers are not comparable until
    one is scaled -- which is exactly how a comment came to claim they were
    "the same bars the pipeline used".
    """
    import re

    source = (
        REPO / "tools" / "boltz2" / "run_pipeline.py"
    ).read_text(encoding="utf-8")
    found = re.search(r"^STRICT_PLDDT\s*=\s*([0-9.]+)", source, re.M)
    assert found, "STRICT_PLDDT not found"
    pipeline_bar = float(found.group(1)) * 100.0
    assert pipeline_bar == 85.0
    assert float(get_legend("boltz2", "pLDDT")["good"]) == 80.0


def test_every_gate_leg_with_a_parse_default_declares_it():
    """A container that substitutes a default when a column will not parse has
    that default declared, or the page reports the parse failure as a
    confident measured shortfall.

    Two rounds of this fix each covered a subset: the first named pxdesign and
    rfantibody, the second added rfdiffusion, and boltzgen -- the tool the
    IMPLAUSIBLE_VALUES comment is written about -- was last.
    """
    expected = {
        ("boltzgen", "pLDDT"), ("boltzgen", "refolding_rmsd"),
        ("rfdiffusion", "ipTM"), ("rfdiffusion", "pLDDT"),
        ("rfdiffusion", "i_pAE"),
        ("pxdesign", "ipTM"), ("pxdesign", "pLDDT"),
        ("rfantibody", "pLDDT"), ("rfantibody", "ipAE"), ("rfantibody", "pAE"),
    }
    assert expected <= set(IMPLAUSIBLE_VALUES), sorted(
        expected - set(IMPLAUSIBLE_VALUES)
    )
    # And each declared value really does sit on the failing side of its bar,
    # which is why leaving it undeclared is not harmless.
    for (tool, column), values in IMPLAUSIBLE_VALUES.items():
        legend = get_legend(tool, column)
        for value in values:
            record = {"scores": {column: value}}
            assert judge(tool, record).verdict == "unjudged", (tool, column)


def test_the_bar_is_unanswerable_for_a_tool_that_has_none():
    """``all()`` over no columns is True, so a tool with no bar would report
    its bar as answerable and the caller would count designs as meeting one.
    Unreachable today only because blueprints/jobs asks tool_has_bar first."""
    from shared.score_legends import bar_is_answerable

    assert not bar_is_answerable("opendde", [{"scores": {"ipTM": 0.9}}])
    assert not bar_is_answerable("bindcraft", [{"scores": {"ipTM": 0.9}}])


def test_a_label_is_split_only_on_a_real_unit():
    """"Shape complementarity (SC)" is an ABBREVIATION, not a unit, and
    splitting it produced the reading "Shape complementarity 0.700 SC"."""
    from shared.score_legends import _label_and_unit

    assert _label_and_unit("refolding_rmsd") == ("Refolding RMSD", " \u00c5")
    assert _label_and_unit("shape_complementarity") == (
        "Shape complementarity (SC)", "",
    )
    assert _label_and_unit("ipTM") == ("ipTM", "")


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
    # esmfold2-design is in this list on purpose. Its bar is mode-dependent
    # (scFv on a CDR proxy, minibinder on ipTM AND pI) and cannot be written
    # as one conjunction over its columns; an ipTM-only bar printed "meets" on
    # the high-ipTM design its own worked example exists to warn you off.
    for tool in ("bindcraft", "proteina", "iggm", "opendde", "mpnn",
                 "esmfold2-design", "esmfold2_design", ""):
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
    "pxdesign": {"scores": {"ipTM": 0.9, "pLDDT": 88.0}},
    "rfantibody": {"scores": {"pLDDT": 88.0, "ipAE": 6.0, "pAE": 3.0}},
    "boltz2": {"iptm": 0.9, "complex_plddt": 0.93, "n_hotspot_contacts": 6},
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


def test_a_smoke_stub_is_never_judged():
    """Fabricated scores get no verdict. The smoke tier invents deterministic
    numbers when no model output exists and marks them "stub (smoke)"; that
    marker is PROVENANCE, a fact about where a number came from, and it is the
    one thing this module still reads out of that field. Applying a bar to
    invented values printed "Meets pLDDT 80" over numbers no model produced --
    the boltzgen stub at rank 10 is pLDDT 80.0 and RMSD 1.5, clearing its own
    tool's bar exactly."""
    from shared.score_legends import is_fabricated

    stub = {"scores": {"pLDDT": 80.0, "refolding_rmsd": 1.5,
                       "filter_status": "stub (smoke)"}}
    assert is_fabricated(stub)
    verdict = judge("boltzgen", stub)
    assert verdict.verdict == "unjudged"
    # UNUSABLE, not unmeasured. The columns beside a stub row are not empty:
    # they hold invented numbers, which is the textbook declared placeholder.
    # It matters beyond wording -- ranking sinks an unusable row and floats an
    # unmeasured one, and a stub has no business outranking a real design.
    assert verdict.unusable == ("smoke-test stub, scores fabricated",)
    assert verdict.unmeasured == ()
    # The same numbers without the marker DO meet the bar, which is what makes
    # the marker load-bearing rather than decorative.
    assert judge(
        "boltzgen", {"scores": {"pLDDT": 80.0, "refolding_rmsd": 1.5}},
    ).verdict == "meets"


def test_a_container_parse_failure_is_unmeasured_not_a_shortfall():
    """Every container parser substitutes a default when a column is present
    but will not parse: 0.0 for pLDDT / ipTM, 99.0 for the PAE family. Each
    lands on the FAILING side of its own bar, so without a placeholder
    declaration the page reports a parse failure as a confident measured
    shortfall."""
    assert judge(
        "rfantibody",
        {"scores": {"pLDDT": 0.0, "ipAE": 99.0, "pAE": 99.0}},
    ).verdict == "unjudged"
    assert judge(
        "pxdesign", {"scores": {"ipTM": 0.0, "pLDDT": 0.0}},
    ).verdict == "unjudged"


def test_a_parse_failure_never_counts_as_meeting_the_bar():
    """The placeholder rule must not become a way for a broken record to pass.

    Unjudged is not failed, which is what keeps a recovered job's designs in
    their place. It is also not PASSED: count_candidates_meeting_bar wants
    evidence a design met the bar, and a parse-failure sentinel is evidence of
    the opposite kind.
    """
    from shared.jobs import count_candidates_meeting_bar

    result = {"candidates": [
        {"scores": {"ipTM": 0.0, "pLDDT": 0.0, "i_pAE": 99.0}},   # all default
        {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "i_pAE": 6.0}},   # measured
    ]}
    assert count_candidates_meeting_bar(result, "rfdiffusion") == 1


def test_a_broken_record_does_not_outrank_a_measured_one():
    """ABSENT AND BROKEN ARE DIFFERENT, and the ordering has to tell them apart.

    ``passed`` leads the ranking sort key and unjudged counts as passed, which
    is what keeps a recovered job's designs in their place: judging unmeasured
    rows as failures once sank 240 recovered pxdesign rows at ipTM 0.99 below
    100 bindcraft rows at 0.70.

    A DECLARED PLACEHOLDER is not that. It is evidence the pipeline could not
    produce a number, and collapsing the two put one rfdiffusion row of
    0.0 / 0.0 / 99.0 -- the exact triple its AF2 reader returns when the score
    JSON has no such keys -- at the top of a table of 1200 measured designs.
    Judgement.unusable separates them; ranking and the campaign sort both read
    it.
    """
    from shared.ranking import rank_candidates

    rows = [
        {"_source_tool": "rfdiffusion", "_source_job_id": "j",
         "_source_index": 0,
         "scores": {"ipTM": 0.0, "pLDDT": 0.0, "i_pAE": 99.0}},   # broken
        {"_source_tool": "rfdiffusion", "_source_job_id": "j",
         "_source_index": 1,
         "scores": {"ipTM": 0.30, "pLDDT": 88.0, "i_pAE": 6.0}},  # measured
        {"_source_tool": "boltzgen", "_source_job_id": "b",
         "_source_index": 2,
         "scores": {"pLDDT": 88.0}},                              # recovered
    ]
    by_index = {
        r["_source_index"]: r for r in rank_candidates(rows, limit=None)["rows"]
    }
    assert by_index[2]["_passed"] is True, "a recovered design must keep its place"
    assert by_index[0]["_passed"] is False, "a placeholder must not read as passed"
    assert by_index[1]["_passed"] is False


def test_a_broken_leg_and_a_missing_leg_are_reported_differently():
    """They are different facts, so the cell may not word them alike. "Not
    measured" over a column that holds 0.0 is close enough to wrong: the column
    is not empty, it is untrustworthy."""
    broken = judge("rfdiffusion", {"scores": {"ipTM": 0.0, "pLDDT": 88.0,
                                              "i_pAE": 6.0}})
    assert broken.unusable == ("ipTM",) and broken.unmeasured == ()

    missing = judge("boltzgen", {"scores": {"pLDDT": 88.0}})
    assert missing.unmeasured == ("Refolding RMSD",) and missing.unusable == ()

    # Both leave the verdict unjudged: neither supports a claim about the
    # design, which is the half they DO share.
    assert broken.verdict == missing.verdict == "unjudged"


def test_a_negative_plddt_is_unmeasured_not_a_shortfall():
    """A negative confidence is a broken payload. plddt_on_100 passes it
    through so a reader SEES it in the table; a bar must not turn it into
    "pLDDT -5, below 80", which reads as something that was measured."""
    assert judge(
        "boltzgen", {"scores": {"pLDDT": -5, "refolding_rmsd": 1.0}},
    ).verdict == "unjudged"


def test_a_reading_never_contradicts_itself():
    """THE PAGE JUDGES WHAT IT SHOWS.

    %g gave six significant figures and then trimmed, so an ipTM of 0.7499999
    printed "0.75" and the cell read "ipTM 0.75, below 0.75". Formatting the
    reading at the metric's display precision only moved the contradiction to
    "ipTM 0.750, below 0.75" -- an earlier version of this test asserted that
    exact string, so its name certified the opposite of what it pinned.

    The comparison now uses the DISPLAYED value. A page cannot assert a
    distinction it does not render, so a reading that shows as 0.750 is 0.750
    for the purpose of a 0.75 bar. The bar itself keeps its exact value: it is
    a chosen threshold rather than a measurement.
    """
    assert judge(
        "pxdesign", {"scores": {"ipTM": 0.7499999, "pLDDT": 88.0}},
    ).verdict == "meets"

    # And a value that genuinely renders below it still falls short, naming
    # the number the reader can see in the ipTM column beside it.
    (short,) = judge(
        "pxdesign", {"scores": {"ipTM": 0.7494, "pLDDT": 88.0}},
    ).shortfalls
    assert short == "ipTM 0.749, below 0.75"


def test_a_value_exactly_on_the_bar_meets_it():
    """The convention the glossary now states, pinned so the prose cannot
    drift from it."""
    assert judge(
        "boltzgen", {"scores": {"pLDDT": 80.0, "refolding_rmsd": 1.5}},
    ).verdict == "meets"


def test_a_smoke_stub_marker_is_matched_as_a_whole_word():
    """docker/pxdesign passes an arbitrary value through from its upstream
    summary CSV, so the marker vocabulary is not closed. A substring test on
    "stub" also fires on "no_stub" and "substub"."""
    from shared.score_legends import is_fabricated

    for marker in ("stub (smoke)", "STUB", "smoke stub"):
        assert is_fabricated({"scores": {"filter_status": marker}}), marker
    for marker in ("no_stub", "substub", "stubborn", "pass", ""):
        assert not is_fabricated({"scores": {"filter_status": marker}}), marker


def test_the_live_counter_says_nothing_rather_than_zero(monkeypatch):
    """A boltzgen partial can never carry the refolding RMSD its bar needs, so
    "0 met the bar" is a claim the run has not made. The predicate that
    shipped first -- "some partial is not unjudged" -- pinned the counter at a
    0 it could never leave, because one partial short on a leg that DOES
    stream flips the set out of all-unjudged."""
    from shared.score_legends import bar_is_answerable

    # Exactly the shape webhooks/modal._sanitize_candidate stores from a
    # boltzgen heartbeat: ipTM, pLDDT, i_pae. No refolding RMSD, ever.
    partials = [
        {"iptm": 0.5, "plddt": 92.0, "i_pae": None},
        {"iptm": 0.4, "plddt": 60.0, "i_pae": None},
    ]
    assert not bar_is_answerable("boltzgen", partials)
    # rfdiffusion streams all three of its legs, so its counter does work.
    assert bar_is_answerable("rfdiffusion", [
        {"iptm": 0.8, "plddt": 90.0, "i_pae": 5.0},
    ])
    assert not bar_is_answerable("boltzgen", [])


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


# The ONE function allowed to read that field, and only for its provenance
# half. Named rather than pattern-matched, so a second reader cannot slip in
# beside it by looking similar.
_PROVENANCE_CARVE_OUT = ("shared/score_legends.py", "is_fabricated")


def test_no_template_expression_reads_filter_status():
    """A grep, and the WEAKEST check in this file -- the behavioural tests
    above are what pin the mechanism. Scoped to Jinja expressions so a comment
    explaining the history does not trip it."""
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


def _verdict_reads(source: str):
    """Line numbers where ``source`` READS the ``filter_status`` key.

    An AST walk, not a grep, and the difference is the whole usefulness of the
    check. Every module in the change explains this field at length in prose,
    and a substring match flags all of that; a docstring is not a read. It
    also flags ``_STALE_VERDICT_KEYS = frozenset({"filter_status"})``, which
    names the key in order to EXCLUDE it from every CSV -- the opposite of
    reading it.

    So: subscripts (``d["filter_status"]``) and ``.get("filter_status")``
    calls, which is what a read of a JSON-ish record looks like in this repo.
    """
    import ast

    hits = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "filter_status"
        ):
            hits.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "filter_status"
        ):
            hits.append(node.lineno)
    return sorted(set(hits))


def test_no_python_module_reads_filter_status_for_a_verdict():
    """The other two thirds of the claim the comments make.

    shared/jobs.py and shared/score_legends.py both tell a reader this grep
    keeps the word out of shared/, blueprints/ AND templates/. It walked
    templates only, so two thirds of that sentence was decoration. It walks all
    three now, with one named carve-out: score_legends.is_fabricated reads the
    "stub (smoke)" provenance marker stored in the same field, which is a fact
    about where a number came from rather than a judgement about a design.
    """
    offenders = []
    for folder in ("shared", "blueprints"):
        for path in (REPO / folder).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel == _PROVENANCE_CARVE_OUT[0]:
                continue
            for lineno in _verdict_reads(path.read_text(encoding="utf-8")):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "a module reads the stored verdict again: " + ", ".join(offenders)
    )


def test_the_provenance_carve_out_is_exactly_one_function():
    """The carve-out above exempts a whole FILE, so this bounds what may live
    in it: the marker may be read inside is_fabricated and nowhere else."""
    import ast

    source = (REPO / _PROVENANCE_CARVE_OUT[0]).read_text(encoding="utf-8")
    reading_lines = set(_verdict_reads(source))
    assert reading_lines, "nothing reads the marker; this check is hollow"

    owners = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef):
            span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
            if reading_lines & set(span):
                owners.add(node.name)
    assert owners == {_PROVENANCE_CARVE_OUT[1]}, sorted(owners)


def test_the_banner_states_a_measurable_fact():
    """The all-designs-fell-short banner has to name the bar it is talking
    about. "All designs fell below quality thresholds" is a verdict whose
    thresholds the reader cannot see; "no design here reaches pLDDT 80" is
    checkable."""
    shell = (REPO / "templates/components/results_shell.html").read_text(
        encoding="utf-8",
    )
    assert (
        "No design here reaches "
        "{{ shortfall_bar_text(tool_slug, ns.legs) }}"
    ) in shell
    # Jinja comments stripped: the block above the banner quotes the old
    # wording to say why it went, and a check that cannot tell an explanation
    # from the thing it explains would forbid ever writing one down.
    rendered = _JINJA_COMMENT.sub("", shell)
    assert "fell below quality thresholds" not in rendered

    # The WHOLE string, not a prefix. Two earlier assertions here stopped one
    # character before the unit and so could not see it rendering in the wrong
    # place ("Refolding RMSD (A) 1.5", with the unit stranded ahead of the
    # number it belongs to).
    assert gate_bar_text("boltzgen") == "pLDDT 80 and Refolding RMSD 1.5 \u00c5"
    assert gate_bar_text("bindcraft") == ""
    # A banner may name only the legs it saw fall short.
    assert shortfall_bar_text("boltzgen", ("pLDDT",)) == "pLDDT 80"
    assert shortfall_bar_text("boltzgen", ()) == ""
