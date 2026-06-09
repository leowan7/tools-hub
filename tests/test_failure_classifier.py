"""Tests for the failure classifier introduced by the tier-collapse PR.

Covers ``shared.jobs.classify_terminal_state`` (the pure mapping
function from terminal-state row to ``failure_class`` enum value),
``shared.jobs.is_billed_failure_class`` (the refund-policy gate),
and the invariants on the BILLED / REFUNDED frozensets that drive
``_settle_wallet_hold_for_completed_job``.

The classifier values must match the CHECK constraint in
``supabase/migrations/0029_tool_jobs_failure_class.sql``; any new
value added here without updating that migration will not survive a
write to the row.
"""
from __future__ import annotations

import pytest

from shared.jobs import (
    _BILLED_FAILURE_CLASSES,
    _ERROR_BUCKET_TO_FAILURE_CLASS,
    _REFUNDED_FAILURE_CLASSES,
    classify_terminal_state,
    is_billed_failure_class,
)


# Mirror of the CHECK constraint in migration 0029. Updating one
# without the other lets a classifier value through Python that the
# DB will reject -- this test catches that drift.
_DB_ENUM_VALUES = frozenset({
    "succeeded",
    "user_cancelled",
    "completed_no_yield",
    "safety_kill",
    "infra_crash",
    "tool_error",
    "preflight_miss",
    "no_progress_timeout",
    "unclassified",
})


# ---------------------------------------------------------------------------
# Invariants on the classifier enum + refund-policy frozensets
# ---------------------------------------------------------------------------


def test_billed_and_refunded_classes_are_disjoint():
    """No class can be both billed and refunded -- the routing depends on it."""
    overlap = _BILLED_FAILURE_CLASSES & _REFUNDED_FAILURE_CLASSES
    assert overlap == frozenset(), f"overlap: {overlap}"


def test_billed_plus_refunded_covers_every_classifier_value():
    """Every value the classifier can emit must have a settle policy.

    If a new failure_class slips into the enum without a refund-policy
    bucket, _settle_wallet_hold_for_completed_job will log "Unknown
    failure_class" and fall through to the legacy heuristic -- safe
    but a sign of drift.
    """
    union = _BILLED_FAILURE_CLASSES | _REFUNDED_FAILURE_CLASSES
    assert union == _DB_ENUM_VALUES, (
        f"missing policy: {_DB_ENUM_VALUES - union}, "
        f"extra: {union - _DB_ENUM_VALUES}"
    )


def test_classifier_enum_matches_db_check_constraint():
    """Every classifier value the function can emit must be in the DB CHECK.

    Walks every status the classifier handles and every error bucket
    it maps from. Catches the case where the Python side adds a new
    enum value but the migration is forgotten.
    """
    candidates = set()
    for status in ("succeeded", "cancelled", "timeout", "failed"):
        for result in (None, {"candidates": []}, {"candidates": [1, 2]}):
            for bucket in [None] + list(_ERROR_BUCKET_TO_FAILURE_CLASS.keys()) + ["mystery"]:
                err = {"bucket": bucket} if bucket else None
                v = classify_terminal_state(status=status, error=err, result=result)
                if v is not None:
                    candidates.add(v)
    unknown = candidates - _DB_ENUM_VALUES
    assert not unknown, f"classifier emits values not in DB CHECK: {unknown}"


# ---------------------------------------------------------------------------
# classify_terminal_state -- per-status mapping
# ---------------------------------------------------------------------------


def test_classify_succeeded_with_candidates():
    """Succeeded with at least one candidate -> 'succeeded'."""
    out = classify_terminal_state(
        status="succeeded", result={"candidates": [{"id": 1}]},
    )
    assert out == "succeeded"


def test_classify_succeeded_with_empty_candidates():
    """Succeeded but produced zero candidates -> 'completed_no_yield'."""
    out = classify_terminal_state(
        status="succeeded", result={"candidates": []},
    )
    assert out == "completed_no_yield"


def test_classify_succeeded_without_candidates_key_defaults_to_succeeded():
    """No candidates key (some tools don't emit one) -> 'succeeded'.

    The zero-yield detection is best-effort: only fires when the
    pipeline explicitly emits an empty list. Tools without the
    convention default to billed-as-succeeded.
    """
    out = classify_terminal_state(status="succeeded", result={"foo": "bar"})
    assert out == "succeeded"
    out = classify_terminal_state(status="succeeded", result=None)
    assert out == "succeeded"


def test_classify_cancelled_always_user_cancelled():
    """Cancellations are always user-initiated under the new cancel API.

    Mid-run safety kills go through mark_failed with bucket=
    overrun_safety_kill, not through mark_cancelled, so 'cancelled'
    status unambiguously means a user cancel.
    """
    assert classify_terminal_state(status="cancelled") == "user_cancelled"
    # Cancellations with arbitrary error / result payloads still classify
    # as user_cancelled.
    assert classify_terminal_state(
        status="cancelled", error={"bucket": "overrun_safety_kill"},
    ) == "user_cancelled"


def test_classify_timeout_always_no_progress_timeout():
    """Timeouts classify as infra-side stalls -> refunded."""
    assert classify_terminal_state(status="timeout") == "no_progress_timeout"


def test_classify_failed_known_buckets():
    """Each known error bucket maps to its declared classifier value."""
    cases = [
        # Real production bucket strings:
        ("pipeline",             "tool_error"),
        ("storage",              "infra_crash"),
        ("modal-submit",         "infra_crash"),
        ("preflight",            "preflight_miss"),
        ("cancelled",            "user_cancelled"),
        ("overrun_safety_kill",  "safety_kill"),
        # Reserved Modal-side buckets:
        ("modal_crash",          "infra_crash"),
        ("modal_oom",            "infra_crash"),
        ("modal_timeout",        "no_progress_timeout"),
    ]
    for bucket, expected in cases:
        out = classify_terminal_state(
            status="failed", error={"bucket": bucket},
        )
        assert out == expected, f"{bucket} -> got {out}, expected {expected}"


def test_classify_failed_unknown_bucket_is_unclassified():
    """Unknown error buckets default to 'unclassified' (refunded).

    The judgment-case fallback from the tier-collapse spec: any
    failure we cannot confidently attribute refunds in the user's
    favour.
    """
    out = classify_terminal_state(
        status="failed", error={"bucket": "mystery"},
    )
    assert out == "unclassified"
    assert out in _REFUNDED_FAILURE_CLASSES


def test_classify_failed_with_no_error_is_unclassified():
    """No error dict at all -> 'unclassified' (refunded)."""
    assert classify_terminal_state(status="failed") == "unclassified"
    assert classify_terminal_state(
        status="failed", error={},
    ) == "unclassified"
    # error.bucket is the key field; other keys are not consulted.
    assert classify_terminal_state(
        status="failed", error={"detail": "something"},
    ) == "unclassified"


def test_classify_non_terminal_status_returns_none():
    """Non-terminal statuses leave the column NULL.

    Reserves the 'not yet classified' signal so callers can tell
    apart "pending/running -> classifier hasn't run" from
    "terminal -> we have a verdict".
    """
    assert classify_terminal_state(status="pending") is None
    assert classify_terminal_state(status="running") is None
    # Made-up status also returns None (defensive default).
    assert classify_terminal_state(status="weird_status") is None


# ---------------------------------------------------------------------------
# is_billed_failure_class -- refund policy gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fc,expected", [
    ("succeeded",            True),
    ("completed_no_yield",   True),
    ("user_cancelled",       True),
    ("safety_kill",          True),
    ("infra_crash",          False),
    ("tool_error",           False),
    ("preflight_miss",       False),
    ("no_progress_timeout",  False),
    ("unclassified",         False),
    (None,                   False),  # NULL = unknown -> refund (safer)
    ("totally_made_up",      False),  # Unknown string -> refund
])
def test_is_billed_failure_class_matches_refund_policy(fc, expected):
    assert is_billed_failure_class(fc) is expected


# ---------------------------------------------------------------------------
# Convergence: every BILLED class is a value the classifier can emit
# ---------------------------------------------------------------------------


def test_every_billed_class_is_producible_by_classifier():
    """Sanity check: every billed class should be reachable via classify().

    If a class lives in _BILLED_FAILURE_CLASSES but the classifier
    never emits it, the row will never settle through that path --
    a sign the class is dead code.
    """
    producible = set()
    for status in ("succeeded", "cancelled", "timeout", "failed"):
        for result in (None, {"candidates": []}, {"candidates": [1]}):
            for bucket in [None] + list(_ERROR_BUCKET_TO_FAILURE_CLASS.keys()):
                err = {"bucket": bucket} if bucket else None
                v = classify_terminal_state(status=status, error=err, result=result)
                if v is not None:
                    producible.add(v)
    missing = _BILLED_FAILURE_CLASSES - producible
    assert not missing, f"billed classes never emitted: {missing}"


def test_every_refunded_class_is_producible_by_classifier():
    """Same sanity check for the refunded set."""
    producible = set()
    for status in ("succeeded", "cancelled", "timeout", "failed"):
        for result in (None, {"candidates": []}, {"candidates": [1]}):
            for bucket in [None] + list(_ERROR_BUCKET_TO_FAILURE_CLASS.keys()) + ["mystery", ""]:
                err = {"bucket": bucket} if bucket is not None else None
                v = classify_terminal_state(status=status, error=err, result=result)
                if v is not None:
                    producible.add(v)
    missing = _REFUNDED_FAILURE_CLASSES - producible
    assert not missing, f"refunded classes never emitted: {missing}"
