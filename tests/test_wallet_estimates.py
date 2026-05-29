"""Unit tests for :mod:`shared.wallet_estimates`.

Covers the four sources the spec calls out:

* per-tool historical p90 path (via patched ``tool_jobs_p90`` lookup)
* tool author ``expected_gpu_seconds`` fallback when history is absent
  or below the minimum sample size
* parameter scaling on the cost estimate
* hard cap clamping when the scaled estimate exceeds the per-tool cap

Plus the per-tool absolute ceiling on :func:`compute_hard_cap`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional
from unittest.mock import patch

import pytest

from shared import wallet_estimates as we
from shared.wallet_estimates import (
    GPU_USD_PER_SECOND,
    MIN_HISTORICAL_RUNS,
    TOOL_SPECS,
    WALLET_MARKUP,
    compute_hard_cap,
    estimated_cost_for_tool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeP90Table:
    """Minimal Supabase-shaped wrapper around a single fixture row."""

    def __init__(self, fixture: Optional[dict]) -> None:
        self._fixture = fixture
        self._filters: list[tuple[str, Any]] = []

    def select(self, *_args: Any, **_kwargs: Any) -> "_FakeP90Table":
        return self

    def eq(self, col: str, val: Any) -> "_FakeP90Table":
        self._filters.append((col, val))
        return self

    def maybe_single(self) -> "_FakeP90Table":
        return self

    def execute(self) -> Any:
        if not self._fixture:
            return type("R", (), {"data": None})()
        for col, val in self._filters:
            if str(self._fixture.get(col)) != str(val):
                return type("R", (), {"data": None})()
        return type("R", (), {"data": dict(self._fixture)})()


class _FakeClient:
    def __init__(self, p90_fixture: Optional[dict] = None) -> None:
        self._fixture = p90_fixture

    def table(self, name: str) -> _FakeP90Table:
        if name == "tool_jobs_p90":
            return _FakeP90Table(self._fixture)
        return _FakeP90Table(None)


@pytest.fixture
def patched_client():
    """Patch :func:`shared.credits.get_service_client` with a stub.

    The fixture returns a callable that swaps in a fresh client with
    the given p90 fixture row.
    """
    patches: list = []

    def _set(fixture: Optional[dict]):
        p = patch(
            "shared.credits.get_service_client",
            return_value=_FakeClient(fixture),
        )
        p.start()
        patches.append(p)
        return fixture

    yield _set
    for p in patches:
        p.stop()


# ---------------------------------------------------------------------------
# Expected-gpu-seconds fallback (no history)
# ---------------------------------------------------------------------------


def test_fallback_uses_expected_gpu_seconds_when_no_history(patched_client):
    """When the p90 view is empty the estimator uses the spec default."""
    patched_client(None)
    spec = TOOL_SPECS["alphafold2"]
    expected_raw = Decimal(str(spec.expected_gpu_seconds)) * Decimal(
        str(GPU_USD_PER_SECOND[spec.gpu_class])
    )
    expected = (expected_raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    estimate = estimated_cost_for_tool(None, "alphafold2", {"preset": "pilot"})
    assert estimate == expected


def test_fallback_when_sample_size_below_min(patched_client):
    """A small sample size is treated as no history."""
    patched_client(
        {
            "tool_slug": "mpnn",
            "lookback_days": 30,
            "p90_gpu_seconds": 999.0,
            "sample_size": MIN_HISTORICAL_RUNS - 1,
        }
    )
    estimate = estimated_cost_for_tool(None, "mpnn", {"preset": "pilot"})
    spec = TOOL_SPECS["mpnn"]
    # Baseline value of num_seq_per_target = 8; default param means scale=1.0
    expected_raw = Decimal(str(spec.expected_gpu_seconds)) * Decimal(
        str(GPU_USD_PER_SECOND[spec.gpu_class])
    )
    expected = (expected_raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    assert estimate == expected


# ---------------------------------------------------------------------------
# Historical p90 path
# ---------------------------------------------------------------------------


def test_uses_historical_p90_when_sample_large_enough(patched_client):
    patched_client(
        {
            "tool_slug": "mpnn",
            "lookback_days": 30,
            "p90_gpu_seconds": 120.0,
            "sample_size": 100,
        }
    )
    spec = TOOL_SPECS["mpnn"]
    expected_raw = Decimal("120") * Decimal(str(GPU_USD_PER_SECOND[spec.gpu_class]))
    expected = (expected_raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    estimate = estimated_cost_for_tool(None, "mpnn", {"preset": "pilot"})
    assert estimate == expected


def test_historical_path_respects_lookback_filter(patched_client):
    """Lookback filter mismatch falls through to expected_gpu_seconds."""
    patched_client(
        {
            "tool_slug": "mpnn",
            "lookback_days": 7,  # wrong window
            "p90_gpu_seconds": 120.0,
            "sample_size": 100,
        }
    )
    estimate = estimated_cost_for_tool(None, "mpnn", {"preset": "pilot"})
    spec = TOOL_SPECS["mpnn"]
    expected_raw = Decimal(str(spec.expected_gpu_seconds)) * Decimal(
        str(GPU_USD_PER_SECOND[spec.gpu_class])
    )
    expected = (expected_raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    assert estimate == expected


# ---------------------------------------------------------------------------
# Per-tier override (cheap preview tiers)
# ---------------------------------------------------------------------------


def test_mini_pilot_uses_tier_override(patched_client):
    """A preview tier with a per-tier override ignores the pilot default."""
    patched_client(None)
    spec = TOOL_SPECS["boltzgen"]
    override = spec.tier_gpu_seconds["mini_pilot"]
    expected_raw = Decimal(str(override)) * Decimal(
        str(GPU_USD_PER_SECOND[spec.gpu_class])
    )
    expected = (expected_raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    estimate = estimated_cost_for_tool(None, "boltzgen", {"preset": "mini_pilot"})
    assert estimate == expected
    # And it must sit far below the pilot estimate — the over-reservation
    # this override exists to prevent.
    pilot = estimated_cost_for_tool(None, "boltzgen", {"preset": "pilot"})
    assert estimate < pilot


def test_mini_pilot_override_beats_tool_wide_p90(patched_client):
    """Tool-wide p90 must not override the per-tier preview estimate.

    The p90 view is not tier-aware and is dominated by heavy pilot runs,
    so for a preview tier the per-tier bootstrap wins even when a large
    p90 sample exists.
    """
    patched_client(
        {
            "tool_slug": "boltzgen",
            "lookback_days": 30,
            "p90_gpu_seconds": 5000.0,
            "sample_size": 100,
        }
    )
    spec = TOOL_SPECS["boltzgen"]
    override = spec.tier_gpu_seconds["mini_pilot"]
    expected_raw = Decimal(str(override)) * Decimal(
        str(GPU_USD_PER_SECOND[spec.gpu_class])
    )
    expected = (expected_raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    estimate = estimated_cost_for_tool(None, "boltzgen", {"preset": "mini_pilot"})
    assert estimate == expected


def test_tier_without_override_falls_through_to_default(patched_client):
    """A preset with no per-tier override still uses p90/expected_gpu_seconds."""
    patched_client(None)
    spec = TOOL_SPECS["mpnn"]  # no tier_gpu_seconds entries
    expected_raw = Decimal(str(spec.expected_gpu_seconds)) * Decimal(
        str(GPU_USD_PER_SECOND[spec.gpu_class])
    )
    expected = (expected_raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    estimate = estimated_cost_for_tool(None, "mpnn", {"preset": "mini_pilot"})
    assert estimate == expected


# ---------------------------------------------------------------------------
# Parameter scaling on the estimate
# ---------------------------------------------------------------------------


def test_estimate_scales_with_num_designs(patched_client):
    patched_client(None)
    spec = TOOL_SPECS["bindcraft"]
    base_estimate = estimated_cost_for_tool(
        None,
        "bindcraft",
        {"preset": "pilot", "num_designs": spec.designs_per_run_baseline},
    )
    big_estimate = estimated_cost_for_tool(
        None,
        "bindcraft",
        {"preset": "pilot", "num_designs": spec.designs_per_run_baseline * 4},
    )
    assert big_estimate > base_estimate


def test_estimate_clamped_at_absolute_cap(patched_client):
    """Pushing num_designs absurdly high cannot exceed the per-tool ceiling."""
    patched_client(None)
    spec = TOOL_SPECS["bindcraft"]
    # 50,000 designs over baseline 2 = 25,000x scale. Estimate would be
    # millions, but the clamp pins it to the absolute ceiling.
    estimate = estimated_cost_for_tool(
        None,
        "bindcraft",
        {"preset": "pilot", "num_designs": 50000},
    )
    assert estimate == spec.absolute_cap_usd


def test_below_baseline_scaling_does_not_scale_down(patched_client):
    """Submitting fewer than baseline does not reduce the estimate below baseline."""
    patched_client(None)
    spec = TOOL_SPECS["bindcraft"]
    baseline_estimate = estimated_cost_for_tool(
        None,
        "bindcraft",
        {"preset": "pilot", "num_designs": spec.designs_per_run_baseline},
    )
    half_estimate = estimated_cost_for_tool(
        None,
        "bindcraft",
        {"preset": "pilot", "num_designs": 1},
    )
    assert half_estimate == baseline_estimate


# ---------------------------------------------------------------------------
# compute_hard_cap
# ---------------------------------------------------------------------------


def test_compute_hard_cap_no_scaling_param_returns_base():
    cap = compute_hard_cap("alphafold2", {})
    assert cap == TOOL_SPECS["alphafold2"].base_hard_cap_usd


def test_compute_hard_cap_scales_with_num_designs():
    spec = TOOL_SPECS["bindcraft"]
    cap = compute_hard_cap(
        "bindcraft", {"num_designs": spec.designs_per_run_baseline * 10}
    )
    assert cap > spec.base_hard_cap_usd
    assert cap <= spec.absolute_cap_usd


def test_compute_hard_cap_saturates_at_absolute_cap():
    spec = TOOL_SPECS["bindcraft"]
    cap = compute_hard_cap("bindcraft", {"num_designs": 1_000_000})
    assert cap == spec.absolute_cap_usd


def test_compute_hard_cap_unknown_tool_returns_default():
    cap = compute_hard_cap("not_a_real_tool", {})
    assert cap == Decimal("10.00")


def test_compute_hard_cap_baseline_floor():
    """A param below baseline still gets at least the base cap."""
    spec = TOOL_SPECS["bindcraft"]
    cap = compute_hard_cap("bindcraft", {"num_designs": 1})
    assert cap == spec.base_hard_cap_usd


# ---------------------------------------------------------------------------
# Unknown tool fallback
# ---------------------------------------------------------------------------


def test_unknown_tool_falls_back_to_conservative_default(patched_client):
    patched_client(None)
    estimate = estimated_cost_for_tool(None, "not_a_real_tool", {"preset": "pilot"})
    expected = (
        Decimal("60")
        * Decimal(str(we.DEFAULT_USD_PER_SECOND))
        * WALLET_MARKUP
    ).quantize(Decimal("0.0001"))
    assert estimate == expected


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_non_numeric_param_falls_back_to_baseline(patched_client):
    patched_client(None)
    spec = TOOL_SPECS["bindcraft"]
    estimate_baseline = estimated_cost_for_tool(
        None,
        "bindcraft",
        {"preset": "pilot", "num_designs": spec.designs_per_run_baseline},
    )
    estimate_garbage = estimated_cost_for_tool(
        None,
        "bindcraft",
        {"preset": "pilot", "num_designs": "not-a-number"},
    )
    assert estimate_garbage == estimate_baseline


def test_estimate_is_decimal_quantized(patched_client):
    patched_client(None)
    estimate = estimated_cost_for_tool(None, "mpnn", {"preset": "pilot"})
    # Must have <= 4 decimal places of precision.
    exponent = estimate.as_tuple().exponent
    assert exponent <= 0
    assert exponent >= -4
