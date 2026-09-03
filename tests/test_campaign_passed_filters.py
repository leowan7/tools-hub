"""Shape-tolerant counting for the campaign quality card.

Regression guard for the counter that under-reported vs. the child sub-jobs.
The campaign detail card summed every candidate (not just the ones that met
the bar) and only ever read ``result["candidates"]``, so:

* composite binder-design children (candidates[] with nested ``scores``) had
  their quality ignored, and
* cofold/design children (designs[] with flat metrics and NO ``candidates``
  key) contributed nothing at all.

WHAT CHANGED HERE, AND WHY THE OLD ASSERTIONS ARE GONE. Every test below used
to feed a record a ``filter_status`` word and assert the counter read it.
Nothing reads that word now: the card compares each design's MEASUREMENTS
against ``shared.score_legends.GATE_COLUMNS`` on every request. Keeping tests
that assert "pass" is counted as a pass would have pinned the defect -- 65
BoltzGen candidates carry a verdict from a bar their container has since
dropped, and a stored word cannot be corrected. ``tests/test_derived_verdicts``
holds the guards for the new mechanism; this file keeps the SHAPE tolerance,
which is what the original card bug actually was.
"""

from __future__ import annotations

from shared.jobs import (
    candidate_meets_bar,
    candidate_records,
    count_candidates_meeting_bar,
)


# --- candidate_records: which key holds the per-candidate list --------------


def test_records_from_candidates_key():
    result = {"candidates": [{"rank": 1}, {"rank": 2}]}
    assert len(candidate_records(result)) == 2


def test_records_from_designs_key_when_no_candidates():
    # boltz2 / esmfold2_design cofold shape: rows live under "designs".
    result = {"designs": [{"rank": 1}, {"rank": 2}, {"rank": 3}]}
    assert len(candidate_records(result)) == 3


def test_candidates_preferred_over_designs_when_both_present():
    # esmfold2_design emits both; the canonical "candidates" wins.
    result = {"candidates": [{"rank": 1}], "designs": [{"rank": 1}, {"rank": 2}]}
    assert len(candidate_records(result)) == 1


def test_records_unwraps_legacy_output_wrapper():
    result = {"output": {"candidates": [{"rank": 1}, {"rank": 2}]}}
    assert len(candidate_records(result)) == 2


def test_records_empty_for_missing_or_bad_shape():
    assert candidate_records(None) == []
    assert candidate_records({}) == []
    assert candidate_records({"candidates": "nope"}) == []
    assert candidate_records("not a dict") == []


# --- candidate_meets_bar: nested vs flat metric resolution ------------------


def test_meets_bar_nested_scores_shape():
    # pxdesign shape: metrics under candidate["scores"].
    assert candidate_meets_bar(
        "pxdesign",
        {"rank": 1, "scores": {"ipTM": 0.9, "pLDDT": 88.0, "pAE": 3.0}},
    )


def test_meets_bar_flat_shape():
    # boltz2 designs shape: metrics at the candidate root, lowercase, with
    # pLDDT on the 0-1 scale its pipeline writes.
    assert candidate_meets_bar(
        "boltz2",
        {"rank": 1, "iptm": 0.9, "complex_plddt": 0.93, "n_hotspot_contacts": 6},
    )


def test_one_short_leg_is_enough_to_miss_the_bar():
    assert not candidate_meets_bar(
        "pxdesign",
        {"scores": {"ipTM": 0.9, "pLDDT": 40.0, "pAE": 3.0}},
    )


def test_bad_candidate_shapes_do_not_meet_the_bar():
    assert not candidate_meets_bar("pxdesign", None)
    assert not candidate_meets_bar("pxdesign", "pass")
    assert not candidate_meets_bar("pxdesign", {})


def test_an_unmeasured_design_does_not_count_as_meeting_the_bar():
    """Counting needs evidence FOR. ``shared.ranking`` answers the other
    question the other way; see tests/test_derived_verdicts."""
    assert not candidate_meets_bar("pxdesign", {"scores": {"ipTM": 0.9}})


# --- count_candidates_meeting_bar: gated tools vs. tools with no bar --------


def test_count_applies_the_bar_for_a_gated_tool():
    result = {
        "candidates": [
            {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "pAE": 3.0}},
            {"scores": {"ipTM": 0.8, "pLDDT": 85.0, "pAE": 4.0}},
            {"scores": {"ipTM": 0.2, "pLDDT": 50.0, "pAE": 20.0}},
        ]
    }
    assert count_candidates_meeting_bar(result, "pxdesign") == 2


def test_count_is_the_delivered_count_for_a_tool_with_no_bar():
    """bindcraft ships only its own accepted designs, so every delivered
    record counts -- it must not collapse to 0. The regime is decided by the
    TOOL now, so it no longer depends on which container version happened to
    stamp a word onto the row."""
    result = {
        "candidates": [
            {"scores": {"ipTM": 0.82, "pTM": 0.71}},
            {"scores": {"ipTM": 0.78, "pTM": 0.69}},
            {"scores": {"ipTM": 0.75, "pTM": 0.66}},
        ]
    }
    assert count_candidates_meeting_bar(result, "bindcraft") == 3


def test_count_falls_back_for_flat_designs_of_an_ungated_tool():
    result = {"designs": [{"iptm": 0.8}, {"iptm": 0.7}]}
    assert count_candidates_meeting_bar(result, "iggm") == 2


def test_a_sibling_record_no_longer_changes_how_another_is_judged():
    """The old counter flipped the WHOLE result into filter mode as soon as one
    row carried a status, so an unsignalled sibling silently became a failure.
    Each record is now answered on its own measurements."""
    result = {
        "candidates": [
            {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "pAE": 3.0}},
            {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "pAE": 3.0}},
        ]
    }
    assert count_candidates_meeting_bar(result, "pxdesign") == 2


def test_count_zero_for_empty_or_missing():
    assert count_candidates_meeting_bar(None, "pxdesign") == 0
    assert count_candidates_meeting_bar({}, "pxdesign") == 0
    assert count_candidates_meeting_bar({"candidates": []}, "pxdesign") == 0


def test_count_unwraps_output_wrapper():
    result = {
        "output": {
            "candidates": [{"scores": {"ipTM": 0.9, "pLDDT": 88.0, "pAE": 3.0}}]
        }
    }
    assert count_candidates_meeting_bar(result, "pxdesign") == 1


# --- end-to-end: campaign total is SUM over children, no tool zeroed out -----


def test_campaign_sum_across_mixed_children():
    """Three succeeded children of three different tools. The campaign total is
    the sum of each child's job-page count, and no tool collapses to zero."""
    pxdesign = {  # 2 meet, 1 short -> 2
        "candidates": [
            {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "pAE": 3.0}},
            {"scores": {"ipTM": 0.8, "pLDDT": 85.0, "pAE": 4.0}},
            {"scores": {"ipTM": 0.2, "pLDDT": 50.0, "pAE": 20.0}},
        ]
    }
    bindcraft = {  # no bar declared -> every delivered design -> 4
        "candidates": [{"scores": {"ipTM": v}} for v in (0.8, 0.79, 0.77, 0.75)]
    }
    boltz_like = {  # 1 meets, 2 short (flat, 0-1 pLDDT) -> 1
        "designs": [
            {"iptm": 0.9, "complex_plddt": 0.93, "n_hotspot_contacts": 6},
            {"iptm": 0.3, "complex_plddt": 0.93, "n_hotspot_contacts": 6},
            {"iptm": 0.9, "complex_plddt": 0.40, "n_hotspot_contacts": 6},
        ]
    }
    total = (
        count_candidates_meeting_bar(pxdesign, "pxdesign")
        + count_candidates_meeting_bar(bindcraft, "bindcraft")
        + count_candidates_meeting_bar(boltz_like, "boltz2")
    )
    assert total == 2 + 4 + 1 == 7
