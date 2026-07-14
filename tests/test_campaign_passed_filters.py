"""Shape-tolerant passing-candidate counting for the campaign "Passed filters"
card.

Regression guard for the counter that under-reported vs. the child sub-jobs.
The campaign detail card summed every candidate (not just the passing ones)
and only ever read ``result["candidates"]``, so:

* composite binder-design children (candidates[] with ``scores.filter_status``)
  had their pass/fail ignored, and
* cofold/design children (designs[] with a flat ``filter_status`` and NO
  ``candidates`` key) contributed nothing at all.

These lock the pure helpers ``candidate_records`` / ``candidate_passed_filter``
that the counter now builds on. The DB rollup itself (``_campaign_passed_filters``
in app.py) is a thin sum over these, so pinning the helpers pins the fix.
"""

from __future__ import annotations

from shared.jobs import (
    PASS_FILTER_STATUSES,
    candidate_passed_filter,
    candidate_records,
    count_passed_candidates,
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


# --- candidate_passed_filter: nested vs flat, and the pass vocabularies -----


def test_pass_nested_scores_shape():
    # pxdesign / rfantibody shape: filter_status under candidate["scores"].
    assert candidate_passed_filter(
        {"rank": 1, "scores": {"ipTM": 0.76, "filter_status": "pass"}}
    )


def test_pass_flat_shape():
    # boltz2 designs shape: filter_status at the candidate root.
    assert candidate_passed_filter({"rank": 1, "iptm": 0.8, "filter_status": "pass"})


def test_strict_pass_counts_as_pass():
    # boltz2 / esmfold2_design use the stricter token.
    assert candidate_passed_filter({"scores": {"filter_status": "strict_pass"}})
    assert candidate_passed_filter({"filter_status": "strict_pass"})


def test_case_and_whitespace_insensitive():
    assert candidate_passed_filter({"scores": {"filter_status": " PASS "}})


def test_non_pass_statuses_excluded():
    for status in ("fail", "borderline", "drop", "stub", "", None):
        assert not candidate_passed_filter({"scores": {"filter_status": status}})
        assert not candidate_passed_filter({"filter_status": status})


def test_explicit_passed_boolean_wins():
    # An explicit boolean flag overrides filter_status if a tool sets one.
    assert candidate_passed_filter({"passed": True, "filter_status": "fail"})
    assert not candidate_passed_filter(
        {"scores": {"passed": False, "filter_status": "pass"}}
    )


def test_bad_candidate_shapes_do_not_pass():
    assert not candidate_passed_filter(None)
    assert not candidate_passed_filter("pass")
    assert not candidate_passed_filter({})


# --- count_passed_candidates: per-result filter vs. delivered-count fallback


def test_count_filters_when_filter_status_present():
    # pxdesign / rfdiffusion: filter_status present -> count only passes.
    result = {
        "candidates": [
            {"scores": {"filter_status": "pass"}},
            {"scores": {"filter_status": "pass"}},
            {"scores": {"filter_status": "fail"}},
        ]
    }
    assert count_passed_candidates(result) == 2


def test_count_falls_back_to_delivered_when_no_filter_signal():
    # bindcraft / rfantibody pre-filter and return ONLY keepers, omitting
    # filter_status. Every delivered record must count — not collapse to 0.
    result = {
        "candidates": [
            {"scores": {"ipTM": 0.82, "pTM": 0.71}},
            {"scores": {"ipTM": 0.78, "pTM": 0.69}},
            {"scores": {"ipTM": 0.75, "pTM": 0.66}},
        ]
    }
    assert count_passed_candidates(result) == 3


def test_count_falls_back_for_flat_designs_without_filter():
    result = {"designs": [{"iptm": 0.8}, {"iptm": 0.7}]}
    assert count_passed_candidates(result) == 2


def test_count_uses_filter_when_any_record_carries_it():
    # A single filtered record flips the whole result into filter mode; the
    # record with no status does not pass.
    result = {
        "candidates": [
            {"scores": {"filter_status": "pass"}},
            {"scores": {"ipTM": 0.5}},
        ]
    }
    assert count_passed_candidates(result) == 1


def test_count_zero_for_empty_or_missing():
    assert count_passed_candidates(None) == 0
    assert count_passed_candidates({}) == 0
    assert count_passed_candidates({"candidates": []}) == 0


def test_count_unwraps_output_wrapper():
    result = {"output": {"candidates": [{"scores": {"filter_status": "pass"}}]}}
    assert count_passed_candidates(result) == 1


# --- end-to-end: campaign total is SUM over children, no tool zeroed out -----


def test_campaign_sum_across_mixed_children():
    # Three succeeded children: pxdesign (filtered), rfantibody (pre-filtered,
    # no filter_status), boltz2-style designs (filtered). The campaign total is
    # the sum of each child's job-page count.
    pxdesign = {  # 2 pass, 1 fail -> 2
        "candidates": [
            {"scores": {"filter_status": "pass"}},
            {"scores": {"filter_status": "pass"}},
            {"scores": {"filter_status": "fail"}},
        ]
    }
    rfantibody = {  # pre-filtered keepers, no filter_status -> 4
        "candidates": [{"scores": {"ipTM": v}} for v in (0.8, 0.79, 0.77, 0.75)]
    }
    boltz_like = {  # 1 strict_pass, 2 fail (flat) -> 1
        "designs": [
            {"filter_status": "strict_pass"},
            {"filter_status": "fail"},
            {"filter_status": "fail"},
        ]
    }
    total = sum(
        count_passed_candidates(c) for c in (pxdesign, rfantibody, boltz_like)
    )
    assert total == 2 + 4 + 1 == 7


def test_pass_statuses_constant_is_frozenset():
    assert "pass" in PASS_FILTER_STATUSES
    assert "strict_pass" in PASS_FILTER_STATUSES
    assert "borderline" not in PASS_FILTER_STATUSES
