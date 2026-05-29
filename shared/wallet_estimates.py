"""Wallet cost estimation and per-job hard caps.

Computes the dollar estimate shown to the user before submit and the
parameter-scaled hard cap enforced inside ``hold_for_job``. These two
numbers drive the route gate, the form UI, and the mid-run safety kill.

Estimate sources, in priority order:

1. A fixed tiny estimate for the smoke tier so the wallet UX shows a
   non-zero number even when there is no cost.
2. A per-tier ``gpu_seconds`` override (``ToolSpec.tier_gpu_seconds``)
   for cheap preview tiers like ``mini_pilot``. Takes precedence over
   p90 because the p90 view is tool-wide, not tier-aware, and is
   dominated by the heavy pilot runs.
3. Per-tool historical p90 ``gpu_seconds`` over the last 30 days, when
   the tool has at least ``MIN_HISTORICAL_RUNS`` completed runs on
   record.
4. Tool author ``expected_gpu_seconds`` registered in :data:`TOOL_SPECS`
   (the pilot-tier default). Used for new tools without enough history.

Parameter scaling: when the submitted ``params`` include a scaling
parameter (``num_designs`` and friends), the base ``gpu_seconds`` is
multiplied by ``actual / baseline``. The resulting USD estimate is then
clamped to the per-job absolute ceiling so a typo cannot translate
straight through into a giant hold.

Hard cap is computed the same way but starts from
``base_hard_cap_usd`` rather than the estimate and clamps to
``absolute_cap_usd``. The two ladders run in parallel: scaling pushes
both the estimate and the hard cap up together.

This module reads but does not write the database. Persistence is
handled by :mod:`shared.wallet`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

# Markup applied to raw Modal compute cost. Kept in sync with
# :data:`shared.wallet.WALLET_MARKUP`. Duplicated here so estimates do
# not pull in the full wallet module at import time.
WALLET_MARKUP = Decimal("1.70")

# Modal GPU rate card (USD per second). Copied verbatim from
# :mod:`shared.workspaces` so this module does not depend on the legacy
# Workspace code. See the comment in :data:`shared.wallet.GPU_USD_PER_SECOND`.
GPU_USD_PER_SECOND: Mapping[str, float] = {
    "A10G":      0.000208,
    "A100-40GB": 0.000714,
    "A100-80GB": 0.001028,
    "H100":      0.002417,
    "L4":        0.000236,
    "L40S":      0.000597,
    "T4":        0.000164,
}

DEFAULT_USD_PER_SECOND = 0.001028  # A100-80GB rate.

# Smoke tier is a fixed nominal estimate so the wallet UI shows a
# non-zero number even though smoke runs are free.
SMOKE_TIER_ESTIMATE_USD = Decimal("0.10")

# Minimum number of historical rows before we trust per-tool p90.
MIN_HISTORICAL_RUNS = 20

# Lookback window for the p90 sample.
HISTORICAL_LOOKBACK_DAYS = 30


# ---------------------------------------------------------------------------
# Per-tool specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Per-tool defaults consumed by the estimator.

    ``base_hard_cap_usd`` is the ceiling at default parameters. The
    scaled cap grows with the scaling parameter and saturates at
    ``absolute_cap_usd``.

    ``expected_gpu_seconds`` is the default (pilot-tier) bootstrap.
    ``tier_gpu_seconds`` overrides it for cheaper preview tiers keyed by
    preset slug (e.g. ``{"mini_pilot": 450}``). A preview tier runs a
    fixed tiny job on the baked target, so it costs a small, stable
    fraction of a pilot run; without a per-tier value it would inherit
    the pilot-calibrated ``expected_gpu_seconds`` and over-reserve. The
    smoke tier never reads this (it short-circuits to a fixed nominal).
    """

    slug: str
    gpu_class: str
    expected_gpu_seconds: float
    designs_per_run_baseline: int
    scaling_param: Optional[str]
    base_hard_cap_usd: Decimal
    absolute_cap_usd: Decimal
    tier_gpu_seconds: Mapping[str, float] = field(default_factory=dict)


# Per-tool spec table. Mirrors the absolute caps in
# :data:`shared.wallet.PER_JOB_HARD_CAP_USD`. New tools register by
# adding an entry here.
TOOL_SPECS: Mapping[str, ToolSpec] = {
    "mpnn": ToolSpec(
        slug="mpnn",
        gpu_class="L4",
        expected_gpu_seconds=60.0,
        designs_per_run_baseline=8,
        scaling_param="num_seq_per_target",
        base_hard_cap_usd=Decimal("0.15"),
        absolute_cap_usd=Decimal("150.00"),
    ),
    "alphafold2": ToolSpec(
        slug="alphafold2",
        gpu_class="A100-80GB",
        expected_gpu_seconds=300.0,
        designs_per_run_baseline=1,
        scaling_param=None,
        base_hard_cap_usd=Decimal("1.50"),
        absolute_cap_usd=Decimal("500.00"),
    ),
    "colabfold": ToolSpec(
        slug="colabfold",
        gpu_class="A100-80GB",
        expected_gpu_seconds=480.0,
        designs_per_run_baseline=1,
        scaling_param=None,
        base_hard_cap_usd=Decimal("2.50"),
        absolute_cap_usd=Decimal("500.00"),
    ),
    "esmfold": ToolSpec(
        slug="esmfold",
        gpu_class="A100-80GB",
        expected_gpu_seconds=60.0,
        designs_per_run_baseline=1,
        scaling_param=None,
        base_hard_cap_usd=Decimal("0.30"),
        absolute_cap_usd=Decimal("100.00"),
    ),
    "rfdiffusion": ToolSpec(
        slug="rfdiffusion",
        gpu_class="A100-40GB",
        expected_gpu_seconds=1200.0,
        designs_per_run_baseline=10,
        scaling_param="num_designs",
        base_hard_cap_usd=Decimal("5.00"),
        absolute_cap_usd=Decimal("500.00"),
        # mini_pilot ran 246-352 GPU-s in validation (VALIDATION-LOG.md);
        # bootstrap just above observed so the preview tier stops
        # inheriting the 1200s pilot default.
        tier_gpu_seconds={"mini_pilot": 450.0},
    ),
    "rfantibody": ToolSpec(
        slug="rfantibody",
        gpu_class="A100-40GB",
        expected_gpu_seconds=3600.0,
        designs_per_run_baseline=2,
        scaling_param="num_designs",
        base_hard_cap_usd=Decimal("13.00"),
        absolute_cap_usd=Decimal("500.00"),
        # mini_pilot ran 166-264 GPU-s in validation (VALIDATION-LOG.md);
        # bootstrap just above observed so the preview tier stops
        # inheriting the 3600s pilot default.
        tier_gpu_seconds={"mini_pilot": 350.0},
    ),
    "bindcraft": ToolSpec(
        slug="bindcraft",
        gpu_class="A100-40GB",
        expected_gpu_seconds=3600.0,
        designs_per_run_baseline=2,
        scaling_param="num_designs",
        base_hard_cap_usd=Decimal("8.00"),
        absolute_cap_usd=Decimal("500.00"),
    ),
    "pxdesign": ToolSpec(
        slug="pxdesign",
        gpu_class="A100-40GB",
        expected_gpu_seconds=3600.0,
        designs_per_run_baseline=2,
        scaling_param="num_designs",
        base_hard_cap_usd=Decimal("13.00"),
        absolute_cap_usd=Decimal("500.00"),
    ),
    "boltzgen": ToolSpec(
        slug="boltzgen",
        gpu_class="A100-80GB",
        # 2026-05-28 recal: the first prod pilot (job 758c45e5) used 4944
        # GPU-s / $8.64. The prior 1800 under-reserved by ~2.7x, so overrun
        # runs took a true-up 'charge' on settle. 5000 ~= a typical pilot
        # (estimate ~$8.74, just over observed actual); historical p90
        # supersedes this bootstrap once >=20 runs land.
        expected_gpu_seconds=5000.0,
        designs_per_run_baseline=2,
        scaling_param="num_designs",
        base_hard_cap_usd=Decimal("10.00"),
        absolute_cap_usd=Decimal("300.00"),
        # The 5000s default is the pilot bootstrap; mini_pilot ran
        # ~360 GPU-s in validation (VALIDATION-LOG.md). Without this the
        # cheap preview tier reserved ~$8.74 and released the surplus.
        tier_gpu_seconds={"mini_pilot": 450.0},
    ),
}


def get_tool_spec(tool_slug: str) -> Optional[ToolSpec]:
    """Return the :class:`ToolSpec` for ``tool_slug`` or ``None``."""
    return TOOL_SPECS.get(tool_slug)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimated_cost_for_tool(
    user_id: Optional[str],
    tool_slug: str,
    params: Optional[Mapping[str, object]] = None,
) -> Decimal:
    """Return the USD estimate for one job, rounded to 4 decimal places.

    Looks up the per-tool spec, picks the best ``gpu_seconds`` source
    (per-tier override for preview tiers, else historical p90, falling
    back to spec.expected_gpu_seconds), applies parameter scaling,
    converts to USD via the GPU rate card and ``WALLET_MARKUP``, and
    clamps to the parameter-scaled hard cap.

    ``user_id`` is accepted so future implementations can use per-user
    historical data. The current implementation ignores it.

    Returns :data:`SMOKE_TIER_ESTIMATE_USD` for smoke-tier presets, which
    are free at the GPU layer but appear in the wallet ledger as a
    near-zero hold for symmetry with paid runs.
    """
    params = dict(params or {})
    spec = TOOL_SPECS.get(tool_slug)

    # Smoke tier short-circuits to the fixed minimal estimate.
    preset = str(params.get("preset") or "").lower()
    if preset == "smoke":
        return SMOKE_TIER_ESTIMATE_USD

    if spec is None:
        # Conservative default: pretend the tool is one default A100-80GB minute.
        logger.warning(
            "estimated_cost_for_tool: no TOOL_SPEC for slug=%s", tool_slug
        )
        raw = Decimal("60") * Decimal(str(DEFAULT_USD_PER_SECOND))
        return _quantize_usd(raw * WALLET_MARKUP)

    # Per-tier bootstrap overrides take precedence for cheap preview
    # tiers: the historical p90 view is tool-wide (not tier-aware) and is
    # dominated by heavy pilot runs, so using it for mini_pilot would
    # reintroduce the over-reservation the override exists to prevent.
    tier_override = spec.tier_gpu_seconds.get(preset)
    if tier_override is not None:
        base_seconds = float(tier_override)
    else:
        base_seconds = _historical_p90_seconds(tool_slug)
        if base_seconds is None:
            base_seconds = float(spec.expected_gpu_seconds)

    scaled_seconds = _scale_seconds(base_seconds, spec, params)
    rate = Decimal(str(GPU_USD_PER_SECOND.get(spec.gpu_class, DEFAULT_USD_PER_SECOND)))
    raw_usd = Decimal(str(scaled_seconds)) * rate
    marked_up = raw_usd * WALLET_MARKUP

    scaled_cap = compute_hard_cap(tool_slug, params)
    estimate = min(marked_up, scaled_cap)
    return _quantize_usd(estimate)


def compute_hard_cap(
    tool_slug: str, params: Optional[Mapping[str, object]] = None
) -> Decimal:
    """Return the parameter-scaled hard cap, clamped to the absolute ceiling.

    Tools without a scaling parameter return ``base_hard_cap_usd``.
    Tools with a scaling parameter return
    ``base_hard_cap_usd * max(actual/baseline, 1.0)`` clamped at
    ``absolute_cap_usd``.
    """
    spec = TOOL_SPECS.get(tool_slug)
    if spec is None:
        # Conservative fallback when the tool is not yet registered.
        return Decimal("10.00")
    params = dict(params or {})
    if not spec.scaling_param:
        return spec.base_hard_cap_usd
    actual = _safe_float(params.get(spec.scaling_param), spec.designs_per_run_baseline)
    scale_factor = max(actual / float(spec.designs_per_run_baseline), 1.0)
    scaled = spec.base_hard_cap_usd * Decimal(str(scale_factor))
    return _quantize_usd(min(scaled, spec.absolute_cap_usd))


# ---------------------------------------------------------------------------
# Historical lookup
# ---------------------------------------------------------------------------


def _historical_p90_seconds(tool_slug: str) -> Optional[float]:
    """Return the p90 ``gpu_seconds`` for the last 30 days, or ``None``.

    Uses the ``tool_jobs_p90`` view when present (computed in the
    migration shipped by Agent A). Falls back to ``None`` when the
    Supabase service client is missing, the view is empty for the slug,
    or the row count is below :data:`MIN_HISTORICAL_RUNS`.
    """
    from .credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table("tool_jobs_p90")
            .select("p90_gpu_seconds,sample_size")
            .eq("tool_slug", tool_slug)
            .eq("lookback_days", HISTORICAL_LOOKBACK_DAYS)
            .maybe_single()
            .execute()
        )
        data = getattr(response, "data", None) or {}
        sample = int(data.get("sample_size") or 0)
        if sample < MIN_HISTORICAL_RUNS:
            return None
        value = data.get("p90_gpu_seconds")
        if value is None:
            return None
        return float(value)
    except Exception:
        logger.warning(
            "Could not read tool_jobs_p90 for %s", tool_slug, exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scale_seconds(
    base_seconds: float, spec: ToolSpec, params: Mapping[str, object]
) -> float:
    """Scale a baseline gpu_seconds value by the relevant parameter ratio."""
    if not spec.scaling_param:
        return float(base_seconds)
    actual = _safe_float(params.get(spec.scaling_param), spec.designs_per_run_baseline)
    if spec.designs_per_run_baseline <= 0:
        return float(base_seconds)
    ratio = max(actual / float(spec.designs_per_run_baseline), 1.0)
    return float(base_seconds) * ratio


def _safe_float(value: object, default: float) -> float:
    """Best-effort numeric coercion. Returns ``default`` on failure."""
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _quantize_usd(value: Decimal) -> Decimal:
    """Round a Decimal to 4 decimal places, banker rounding flipped to half-up."""
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
