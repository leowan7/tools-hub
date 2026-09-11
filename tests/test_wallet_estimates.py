"""Unit tests for :mod:`shared.wallet_estimates`.

Covers the four sources the spec calls out:

* per-tool historical p90 path (via patched ``tool_jobs`` rows)
* tool author ``expected_gpu_seconds`` fallback when history is absent
  or below the minimum sample size
* parameter scaling on the cost estimate
* hard cap clamping when the scaled estimate exceeds the per-tool cap

Plus the per-tool absolute ceiling on :func:`compute_hard_cap`, and the
per-unit normalisation that keeps the historical p90 in the same units as
``expected_gpu_seconds``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from unittest.mock import patch

import pytest

from shared import wallet_estimates as we
from shared.wallet_estimates import (
    GPU_USD_PER_SECOND,
    HISTORICAL_LOOKBACK_DAYS,
    MIN_HISTORICAL_RUNS,
    TOOL_SPECS,
    WALLET_MARKUP,
    compute_hard_cap,
    cushioned_hold_usd,
    estimated_cost_for_tool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def job_rows(count: int, seconds: float, units: Optional[object]) -> list[dict]:
    """``count`` PostgREST-shaped ``tool_jobs`` rows of one size.

    ``units`` is the scaling parameter as the jsonb ``->>`` operator returns
    it: a string, or ``None`` when the job never stored one.
    """
    return [
        {
            "gpu_seconds_used": seconds,
            "preset": "pilot",
            "units": None if units is None else str(units),
            "nested_units": None,
            "total_passes": None,
        }
        for _ in range(count)
    ]


class _FakeJobsTable:
    """Minimal Supabase-shaped wrapper around a list of ``tool_jobs`` rows.

    Records every filter so tests can assert on the query itself — the
    lookback window is applied server-side, so the only way to pin it is to
    check the ``created_at`` bound that went out.
    """

    def __init__(self, rows: list[dict], calls: list[tuple[str, Any]]) -> None:
        self._rows = rows
        self._calls = calls

    def select(self, *args: Any, **_kwargs: Any) -> "_FakeJobsTable":
        self._calls.append(("select", args[0] if args else ""))
        return self

    def eq(self, col: str, val: Any) -> "_FakeJobsTable":
        self._calls.append((f"eq:{col}", val))
        return self

    def gt(self, col: str, val: Any) -> "_FakeJobsTable":
        self._calls.append((f"gt:{col}", val))
        return self

    def gte(self, col: str, val: Any) -> "_FakeJobsTable":
        self._calls.append((f"gte:{col}", val))
        return self

    def order(self, col: str, desc: bool = False) -> "_FakeJobsTable":
        self._calls.append((f"order:{col}", desc))
        return self

    def limit(self, n: int) -> "_FakeJobsTable":
        self._calls.append(("limit", n))
        return self

    def execute(self) -> Any:
        return type("R", (), {"data": list(self._rows)})()


class _FakeClient:
    def __init__(self, rows: Optional[list[dict]], calls: list) -> None:
        self._rows = rows or []
        self._calls = calls

    def table(self, name: str) -> _FakeJobsTable:
        if name == "tool_jobs":
            return _FakeJobsTable(self._rows, self._calls)
        return _FakeJobsTable([], self._calls)


@pytest.fixture
def patched_client():
    """Patch :func:`shared.credits.get_service_client` with a stub.

    The fixture returns a callable that swaps in a fresh client serving the
    given ``tool_jobs`` rows, and hands back the list the client records its
    query filters into.
    """
    patches: list = []

    def _set(rows: Optional[list[dict]]):
        calls: list[tuple[str, Any]] = []
        p = patch(
            "shared.credits.get_service_client",
            return_value=_FakeClient(rows, calls),
        )
        p.start()
        patches.append(p)
        return calls

    yield _set
    # REVERSE order. A test may swap the client more than once (comparing two
    # samples), and mock.patch saves the value it displaced: stopping the
    # first-started patch first restores the original, then stopping the
    # second restores the FIRST patch's mock and leaves it installed for the
    # rest of the process. That leak priced an unrelated bindcraft test off
    # this file's fixture rows.
    for p in reversed(patches):
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
    patched_client(job_rows(MIN_HISTORICAL_RUNS - 1, 999.0, 8))
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
    """A baseline-sized sample prices at its own p90."""
    spec = TOOL_SPECS["mpnn"]
    # Every row is a baseline-sized run (num_seq_per_target = 8), so the
    # per-unit normalisation is a no-op and 120 s comes straight through.
    patched_client(job_rows(20, 120.0, spec.designs_per_run_baseline))
    expected_raw = Decimal("120") * Decimal(str(GPU_USD_PER_SECOND[spec.gpu_class]))
    expected = (expected_raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    estimate = estimated_cost_for_tool(None, "mpnn", {"preset": "pilot"})
    assert estimate == expected


def test_historical_path_filters_to_the_lookback_window(patched_client):
    """The window is applied server-side, so pin the bound that goes out.

    The old aggregate view carried a ``lookback_days`` column to filter on;
    reading ``tool_jobs`` directly means the estimator owns the cutoff, and a
    missing or wrong bound would silently price against all history.
    """
    calls = patched_client(job_rows(20, 120.0, 8))
    estimated_cost_for_tool(None, "mpnn", {"preset": "pilot"})

    sent = dict(calls)
    assert sent["eq:tool"] == "mpnn"
    assert sent["eq:status"] == "succeeded"
    assert sent["gt:gpu_seconds_used"] == 0
    assert sent["order:created_at"] is True
    assert sent["limit"] == we._HISTORICAL_ROW_CAP
    cutoff = datetime.fromisoformat(sent["gte:created_at"])
    age_days = (datetime.now(timezone.utc) - cutoff).total_seconds() / 86400
    assert abs(age_days - HISTORICAL_LOOKBACK_DAYS) < 1


def test_historical_query_selects_exactly_the_columns_it_reads(patched_client):
    """Pin the whole select string, not just its shape.

    The fake client cannot validate a query, so a typo in a real column name
    is a PostgREST 400 in production, swallowed by ``except Exception``,
    silently reverting every tool to its spec constant -- with the suite
    still green. Pinning the string is the only thing here that catches it.

    It also pins what is NOT selected: ``inputs`` holds pasted FASTA and
    preflight payloads (max 28 KB/row on prod) and this SELECT is uncached on
    every public tool-page render, so the parameters are read by jsonb path
    rather than by hauling the column.
    """
    calls = patched_client(job_rows(20, 120.0, 8))
    estimated_cost_for_tool(None, "mpnn", {"preset": "pilot"})

    selected = [c.strip() for c in dict(calls)["select"].split(",")]
    assert selected == [
        "gpu_seconds_used",
        "preset",
        "units:inputs->>num_seq_per_target",
        "nested_units:inputs->parameters->>num_seq_per_target",
        "total_passes:inputs->>total_passes",
    ]
    assert "inputs" not in selected


# ---------------------------------------------------------------------------
# Per-unit normalisation: the historical p90 must be in the same units as
# expected_gpu_seconds, or _scale_seconds multiplies by the design count a
# second time and the price tracks past job SIZES instead of per-design cost.
# ---------------------------------------------------------------------------

def _rfd_usd(baseline_equivalent_seconds: float) -> Decimal:
    """Price a 10-design rfdiffusion job from a baseline-equivalent p90."""
    spec = TOOL_SPECS["rfdiffusion"]
    raw = Decimal(str(baseline_equivalent_seconds)) * Decimal(
        str(GPU_USD_PER_SECOND[spec.gpu_class])
    )
    return (raw * WALLET_MARKUP).quantize(Decimal("0.0001"))


# The fixtures below deliberately do NOT reproduce expected_gpu_seconds.
# 20 rows of 2220 s / 8 designs normalises to 277.5/design -> 2775 baseline
# equivalent, which is EXACTLY spec.expected_gpu_seconds, so every assertion
# on it also passes when the historical lookup returns None -- on the old
# code, or in production if the query ever fails closed. 138.75/design is the
# same arithmetic with no such collision.
_RFD_HALF_RATE_SECONDS = 1387.5  # 138.75 s/design x baseline 10
_RFD_HALF_RATE_USD = Decimal("1.6841")


def test_historical_p90_is_normalised_per_design(patched_client):
    """A sample of 8-design runs prices a 10-design chunk per design."""
    patched_client(job_rows(20, 1110.0, 8))
    estimate = estimated_cost_for_tool(
        None, "rfdiffusion", {"preset": "pilot", "num_designs": 10}
    )
    assert estimate == _RFD_HALF_RATE_USD == _rfd_usd(_RFD_HALF_RATE_SECONDS)
    # Guard the guard: if this ever equals the spec fallback, the test has
    # stopped proving the historical path ran at all.
    spec = TOOL_SPECS["rfdiffusion"]
    assert estimate != _rfd_usd(spec.expected_gpu_seconds)


def test_job_size_mix_does_not_move_the_price(patched_client):
    """The defect this closes: same per-design cost, different job sizes.

    Both samples ran at 138.75 GPU-s per design. Percentiling RAW per-job
    seconds made the 4-design sample look half as expensive, and
    ``_scale_seconds`` then multiplied by the design count again -- which is
    how a 10-design campaign chunk priced off prod's 4-and-8-design sample
    would have held $2.75 against a $3.34 charge.

    The fixtures are exactly proportional, so this pins that the CODE adds no
    size dependence. Real runs carry a fixed startup cost, so a sample of
    small jobs still reads slightly high -- see _historical_p90_seconds.
    """
    params = {"preset": "pilot", "num_designs": 10}

    patched_client(job_rows(20, 1110.0, 8))
    from_eight = estimated_cost_for_tool(None, "rfdiffusion", params)

    patched_client(job_rows(20, 555.0, 4))
    from_four = estimated_cost_for_tool(None, "rfdiffusion", params)

    assert from_eight == from_four == _RFD_HALF_RATE_USD
    # Under the old raw-per-job percentile these two differed by 2x.
    assert from_four != _rfd_usd(555.0 * 10 / TOOL_SPECS[
        "rfdiffusion"].designs_per_run_baseline)


def test_rows_without_the_parameter_read_as_baseline_sized(patched_client):
    """A row that stored no parameter is read as a baseline-sized run.

    Dropping such rows instead was tried and is worse -- see the sibling test
    below, which pins why.
    """
    patched_client(job_rows(20, _RFD_HALF_RATE_SECONDS, None))
    estimate = estimated_cost_for_tool(
        None, "rfdiffusion", {"preset": "pilot", "num_designs": 10}
    )
    assert estimate == _RFD_HALF_RATE_USD


def test_the_cheap_tier_cannot_crowd_out_the_expensive_one(patched_client):
    """REGRESSION. Skipping parameter-less rows under-holds colabfold/esmfold.

    af2, colabfold and esmfold omit the scaling parameter only on their
    standalone preset, where the job really is one fold and the baseline (1)
    is exact. Dropping those rows leaves the sample made of batch rows alone,
    whose per-unit cost is far lower -- so the p90 stops seeing the expensive
    standalone tier and the hold for a standalone fold lands UNDER its charge,
    reopening the variance-debit path.

    COLABFOLD deliberately, not af2: af2 carries a ``worst_case_gpu_seconds``
    floor that pins its hold at the cap, so the harm cannot show there and a
    test using it would assert a consequence its own tool is immune to.

    25 batch rows at 2000 s / 40 records = 50 s per fold; 30 standalone rows
    at 200 s = 200 s per fold. The honest answer for a standalone job is 200.
    NOT 120 s: that is colabfold's own ``expected_gpu_seconds``, so a fixture
    built on it passes with the historical path switched off entirely.
    """
    batch = [
        {"gpu_seconds_used": 2000.0, "preset": "batch",
         "units": None, "nested_units": "40", "total_passes": None}
        for _ in range(25)
    ]
    standalone = [
        {"gpu_seconds_used": 200.0, "preset": "standalone",
         "units": None, "nested_units": None, "total_passes": None}
        for _ in range(30)
    ]
    patched_client(batch + standalone)
    spec = TOOL_SPECS["colabfold"]
    assert spec.worst_case_gpu_seconds is None, (
        "colabfold grew a hold floor; this test would stop proving anything"
    )
    rate = Decimal(str(GPU_USD_PER_SECOND[spec.gpu_class]))
    usd = lambda s: (Decimal(str(s)) * rate * WALLET_MARKUP).quantize(
        Decimal("0.0001")
    )
    estimate = estimated_cost_for_tool(None, "colabfold", {"preset": "standalone"})
    assert estimate == usd(200)
    # The batch-only figure, which skipping the standalone rows produces.
    assert estimate != usd(50)
    # And not the spec fallback, or none of the above would mean anything.
    assert estimate != usd(spec.expected_gpu_seconds)


def test_malformed_rows_fall_back_instead_of_raising(patched_client):
    """An unexpected row shape must not 500 a public tool page.

    This SELECT is uncached and runs on every /tools/<slug> render, and
    neither ``estimated_cost_for_tool`` nor ``_pilot_context`` above it has
    an exception handler. The row loop therefore lives INSIDE the try; moving
    it out again turns a bad row into a page error instead of a fallback.
    """
    spec = TOOL_SPECS["rfdiffusion"]
    params = {"preset": "pilot", "num_designs": 10}
    for rows in (["not-a-dict"] * 20, [None] * 20):
        patched_client(rows)
        assert estimated_cost_for_tool(None, "rfdiffusion", params) == _rfd_usd(
            spec.expected_gpu_seconds
        )


def test_non_finite_rows_are_filtered_not_percentiled(patched_client):
    """NaN and +inf must be dropped PER ROW, before the percentile.

    ``_safe_float`` turns the string "nan" into a real NaN and every
    comparison with it is False, so it slips past a ``> 0`` test. Guarding
    the percentile afterwards is not enough: ``sorted()`` is not NaN-safe, so
    a NaN reorders the sample and the cut point lands on a real but WRONG
    value -- on this fixture, 180.0 or 190.0 against an honest 181.0
    (or NaN, from 7 of the 21 insertion positions), depending only on where
    in the list the NaN sat. +inf survives a ``> 0`` guard outright and pegs
    the estimate to the hard cap.

    So the assertion is not "falls back" -- it is that poisoned rows do not
    change the answer the honest rows give.
    """
    params = {"preset": "pilot", "num_designs": 10}
    honest = [
        {"gpu_seconds_used": float(i * 10), "preset": "pilot",
         "units": "10", "nested_units": None, "total_passes": None}
        for i in range(1, 21)
    ]
    patched_client(honest)
    clean = estimated_cost_for_tool(None, "rfdiffusion", params)
    assert clean == _rfd_usd(18.1 * 10)  # p90 of per-unit 1..20

    for poison in ("nan", "inf", "-inf"):
        patched_client(honest + job_rows(5, 100.0, poison))
        assert estimated_cost_for_tool(None, "rfdiffusion", params) == clean, poison
    for poison in ("nan", "inf"):
        patched_client(honest + [
            dict(r, gpu_seconds_used=poison) for r in job_rows(5, 0.0, 10)
        ])
        assert estimated_cost_for_tool(None, "rfdiffusion", params) == clean, poison

    # The QUOTIENT can go non-finite even when both operands are finite and
    # positive: 1e300 / 5e-324 overflows to +inf (which would price at the
    # cap) and 1e-300 / 1e308 underflows to 0.0 (which the estimator accepts
    # as a price, since it only rejects None). Guarding the operands alone
    # misses both.
    for seconds, units in ((1e300, "5e-324"), (1e-300, "1e308")):
        patched_client(honest + job_rows(5, seconds, units))
        assert estimated_cost_for_tool(
            None, "rfdiffusion", params
        ) == clean, (seconds, units)


def test_a_fully_degenerate_sample_falls_back(patched_client):
    """No usable row at all means no history, not a zero price."""
    spec = TOOL_SPECS["rfdiffusion"]
    patched_client(job_rows(20, 0.0, 10))
    assert estimated_cost_for_tool(
        None, "rfdiffusion", {"preset": "pilot", "num_designs": 10}
    ) == _rfd_usd(spec.expected_gpu_seconds)


def test_the_nested_parameters_path_resolves_the_divisor(patched_client):
    """af2, colabfold, esmfold and boltz2 nest the parameter one level down.

    Their validate() returns ``{"parameters": {"n_designs_total": N}}``, so
    reading only the top level leaves them permanently unresolvable -- and
    because their baseline is 1, an unresolved row returns the RAW p90 and
    the double-scaling this whole function exists to remove survives intact.
    """
    rows = [
        {"gpu_seconds_used": 900.0, "preset": "batch",
         "units": None, "nested_units": "50", "total_passes": None}
        for _ in range(20)
    ]
    patched_client(rows)
    spec = TOOL_SPECS["af2"]
    # 900 s for 50 records = 18 s/record; baseline is 1, so 18 s is the
    # baseline-equivalent, and a 50-record job scales it back to 900.
    estimate = estimated_cost_for_tool(
        None, "af2", {"preset": "batch", "n_designs_total": 50}
    )
    raw = Decimal("900") * Decimal(str(GPU_USD_PER_SECOND[spec.gpu_class]))
    assert estimate == (raw * WALLET_MARKUP).quantize(Decimal("0.0001"))


def test_flat_tools_use_history_unscaled(patched_client):
    """opendde has no scaling_param, so raw seconds ARE baseline-equivalent."""
    rows = [
        {"gpu_seconds_used": 1800.0, "preset": "pilot",
         "units": None, "nested_units": None, "total_passes": None}
        for _ in range(20)
    ]
    patched_client(rows)
    spec = TOOL_SPECS["opendde"]
    estimate = estimated_cost_for_tool(None, "opendde", {"preset": "pilot"})
    raw = Decimal("1800") * Decimal(str(GPU_USD_PER_SECOND[spec.gpu_class]))
    expected = (raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    assert estimate == min(expected, spec.base_hard_cap_usd)


def test_iggm_affinity_maturation_divides_by_total_passes(patched_client):
    """IgGM runs one pass per masked position PER sample.

    ``_effective_scaling_value`` expands ``num_samples`` by the mask count and
    prefers the stored ``total_passes``. The historical rows must divide by
    that same product, or a maturation run looks n_masked times more
    expensive per sample than it is.
    """
    rows = [
        {"gpu_seconds_used": 960.0, "preset": "affinity_maturation",
         "units": "8", "nested_units": None, "total_passes": "96"}
        for _ in range(20)
    ]
    patched_client(rows)
    spec = TOOL_SPECS["iggm"]
    # 960 s / 96 passes = 10 s per pass; baseline 1 -> 10 s baseline-equivalent.
    estimate = estimated_cost_for_tool(
        None, "iggm",
        {"preset": "affinity_maturation", "num_samples": 8, "total_passes": 96},
    )
    raw = Decimal("960") * Decimal(str(GPU_USD_PER_SECOND[spec.gpu_class]))
    assert estimate == (raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    # Dividing by num_samples instead of total_passes would be 12x this.
    assert estimate != (
        raw * Decimal("12") * WALLET_MARKUP
    ).quantize(Decimal("0.0001"))


def test_percentile_matches_percentile_cont(patched_client):
    """The percentile must be R-7 / ``percentile_cont``, not the default.

    ``statistics.quantiles`` defaults to method="exclusive", which is NOT
    what Postgres computes. A strictly increasing sample separates them:
    per-unit 1..20 gives 18.1 inclusive against 18.9 exclusive, so dropping
    the argument moves the price. A flat sample with one outlier cannot tell
    them apart, which is why this fixture is a ramp.
    """
    rows = [
        {"gpu_seconds_used": float(i * 10), "preset": "pilot",
         "units": "10", "nested_units": None, "total_passes": None}
        for i in range(1, 21)
    ]
    patched_client(rows)
    estimate = estimated_cost_for_tool(
        None, "rfdiffusion", {"preset": "pilot", "num_designs": 10}
    )
    assert estimate == _rfd_usd(18.1 * 10)
    assert estimate != _rfd_usd(18.9 * 10)


# ---------------------------------------------------------------------------
# Per-tier override (retained as a forward-compat hook; today every shipped
# tier has an empty tier_gpu_seconds map, so this just asserts the fall-
# through path keeps working.)
# ---------------------------------------------------------------------------


def test_tier_without_override_falls_through_to_default(patched_client):
    """A preset with no per-tier override uses p90/expected_gpu_seconds."""
    patched_client(None)
    spec = TOOL_SPECS["mpnn"]  # no tier_gpu_seconds entries
    expected_raw = Decimal(str(spec.expected_gpu_seconds)) * Decimal(
        str(GPU_USD_PER_SECOND[spec.gpu_class])
    )
    expected = (expected_raw * WALLET_MARKUP).quantize(Decimal("0.0001"))
    estimate = estimated_cost_for_tool(None, "mpnn", {"preset": "pilot"})
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
