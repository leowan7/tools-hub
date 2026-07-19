"""Wallet cost estimation and per-job hard caps.

Computes the dollar estimate shown to the user before submit and the
parameter-scaled hard cap enforced inside ``hold_for_job``. These two
numbers drive the route gate, the form UI, and the mid-run safety kill.

Estimate sources, in priority order:

1. Per-tool historical p90 ``gpu_seconds`` over the last 30 days, when
   the tool has at least ``MIN_HISTORICAL_RUNS`` completed runs on
   record.
2. Tool author ``expected_gpu_seconds`` registered in :data:`TOOL_SPECS`
   (the pilot-tier default). Used for new tools without enough history.

(``ToolSpec.tier_gpu_seconds`` remains in the dataclass for forward-
compat with future cheap tiers; today every shipped tier falls through
to historical p90 or the pilot default.)

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

# Minimum number of historical rows before we trust per-tool p90.
MIN_HISTORICAL_RUNS = 20

# Lookback window for the p90 sample.
HISTORICAL_LOOKBACK_DAYS = 30

# Multiplier applied to the point estimate to size the wallet HOLD (the
# reservation), so actual usually lands under the hold and settle releases
# surplus instead of posting a variance charge. The reservation is clamped to
# the per-tool hard cap in cushioned_hold_usd: the customer is capped at the
# hard cap regardless (settle clamps there and Ranomics absorbs above), so
# reserving beyond it would only lock up wallet funds with no billing benefit.
HOLD_CUSHION_MULTIPLIER = Decimal("1.5")


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
    ``tier_gpu_seconds`` is retained as an empty override map for
    forward-compat with future cheap tiers; today no tier uses it.
    """

    slug: str
    gpu_class: str
    expected_gpu_seconds: float
    designs_per_run_baseline: int
    scaling_param: Optional[str]
    base_hard_cap_usd: Decimal
    absolute_cap_usd: Decimal
    tier_gpu_seconds: Mapping[str, float] = field(default_factory=dict)
    # Fixed-container tools that bill ACTUAL wall-clock up to a physical session
    # cap can always bill their worst case, so the HOLD must cover it regardless
    # of the historical-p90 estimate (which only right-sizes the DISPLAYED price).
    # When set, ``cushioned_hold_usd`` floors the hold at the marked-up charge for
    # this many GPU-seconds — the physical session cap — so a fast p90 can never
    # shrink the hold below what one heavy job can still be billed. ``None`` = an
    # ordinary per-design-scaling tool with no worst-case floor.
    worst_case_gpu_seconds: Optional[float] = None
    # Whether ``worst_case_gpu_seconds`` is a PER-CONTAINER cap on a tool that
    # spawns one physical container per unit of ``scaling_param`` (a fan-out
    # tool, e.g. esmfold2-design: one H100 container per seed). For such tools a
    # single job-level hold covers ALL the containers, so the worst-case floor
    # must scale by the same ratio the point estimate uses — a flat per-container
    # floor would only cover ONE container and still under-hold a multi-unit job
    # after p90. Leave ``False`` (the default) for single-container tools whose
    # whole job runs in ONE session-capped container regardless of the scaling
    # param (proteina: one shard = one container; af2: the whole batch folds
    # sequentially inside one container; opendde: scaling_param is None): there
    # the flat per-container floor already covers the entire job and scaling it
    # would over-reserve (clamped to the cap, so still money-safe, but it locks
    # wallet funds with no billing benefit).
    worst_case_scales_with_param: bool = False


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
        # Mirrors the af2 floor below (this historic key is never read by the prod
        # wallet route but is kept consistent with its live twin). One AF2 fold =
        # ONE A100-80GB container; a full-session hang bills up to the $1.50 cap,
        # so the floor clamps there. Flat (single container, scaling_param=None).
        worst_case_gpu_seconds=14400.0,
    ),
    # The AF2 ``ToolAdapter`` registers under slug ``af2``, not
    # ``alphafold2``. The historic ``alphafold2`` key above is preserved
    # for tests + the cross-domain SEO mapping in app.py:_RANOMICS_…
    # but is never read by the production wallet route. The ``af2``
    # entry below mirrors it and adds the batch scaling so the new
    # preset's held amount scales linearly with the record count.
    "af2": ToolSpec(
        slug="af2",
        gpu_class="A100-80GB",
        expected_gpu_seconds=300.0,
        designs_per_run_baseline=1,
        scaling_param="n_designs_total",
        base_hard_cap_usd=Decimal("1.50"),
        absolute_cap_usd=Decimal("500.00"),
        # worst_case_gpu_seconds=14400 (=_MAX_SESSION_S in tools/af2/modal_app.py).
        # The batch preset folds ALL records SEQUENTIALLY inside ONE A100-80GB
        # container physically capped at 14400 s, so the JOB worst case is one
        # container regardless of n_designs_total -> flat floor
        # (worst_case_scales_with_param stays False; scaling it would over-reserve
        # the batch). Floors the hold at the max chargeable (clamped to the
        # scaled cap) once p90 pulls the per-fold estimate down. Smaller blast
        # radius than proteina/esmfold2 (base cap $1.50 at one fold).
        worst_case_gpu_seconds=14400.0,
    ),
    "colabfold": ToolSpec(
        slug="colabfold",
        # ColabFold's Modal app runs on A100-40GB (see
        # tools/colabfold/modal_app.py _GPU). Standalone tier folds in
        # ~1-2 min warm.
        gpu_class="A100-40GB",
        expected_gpu_seconds=120.0,
        designs_per_run_baseline=1,
        # Batch preset stamps ``n_designs_total`` so the held amount
        # scales linearly with the record count. Standalone tier leaves
        # it at 1 and the estimate matches the existing single-fold cost.
        scaling_param="n_designs_total",
        base_hard_cap_usd=Decimal("0.50"),
        # Headroom for the 200-record batch ceiling: at the baseline
        # 120 s/fold, 200 folds × $0.000714/s × 1.7 markup ≈ $29. Capping
        # at $200 leaves room for historical drift / templates-on runs.
        absolute_cap_usd=Decimal("200.00"),
    ),
    "esmfold": ToolSpec(
        slug="esmfold",
        # ESMFold's Modal app runs on A100-40GB (see tools/esmfold/modal_app.py
        # _GPU). Standalone tier folds in ~30 s warm.
        gpu_class="A100-40GB",
        expected_gpu_seconds=60.0,
        designs_per_run_baseline=1,
        # Batch preset stamps ``n_designs_total`` so the held amount scales
        # linearly with the record count. Standalone tier leaves the param
        # at the baseline (1) and the estimate stays at the single-fold
        # cost. Mirrors Boltz-2's scaling shape.
        scaling_param="n_designs_total",
        base_hard_cap_usd=Decimal("0.30"),
        # Headroom for the 500-record batch ceiling: at the baseline
        # 60 s/fold, 500 folds × $0.000714/s × 1.7 markup ≈ $36. Capping
        # at $200 lets the historical p90 grow to ~150 s/fold without the
        # estimator silently clipping the hold.
        absolute_cap_usd=Decimal("200.00"),
    ),
    "rfdiffusion": ToolSpec(
        slug="rfdiffusion",
        gpu_class="A100-40GB",
        expected_gpu_seconds=1200.0,
        designs_per_run_baseline=10,
        scaling_param="num_designs",
        base_hard_cap_usd=Decimal("5.00"),
        absolute_cap_usd=Decimal("500.00"),
    ),
    "rfantibody": ToolSpec(
        slug="rfantibody",
        gpu_class="A100-40GB",
        expected_gpu_seconds=3600.0,
        designs_per_run_baseline=2,
        scaling_param="num_designs",
        base_hard_cap_usd=Decimal("13.00"),
        absolute_cap_usd=Decimal("500.00"),
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
    "boltz2": ToolSpec(
        slug="boltz2",
        gpu_class="A100-40GB",
        # Conservative bootstrap covering both presets: standalone ~60 s/design,
        # msa_server ~180 s/design. Holding at the higher value over-reserves on
        # standalone runs (released as surplus on settle) but never under-holds
        # an MSA fetch. Historical p90 supersedes this once >=20 runs land.
        expected_gpu_seconds=180.0,
        designs_per_run_baseline=1,
        scaling_param="n_designs_total",
        # ~2x the marked-up msa_server per-design cost at the baseline; scales
        # linearly with binder count up to the absolute cap.
        base_hard_cap_usd=Decimal("0.40"),
        absolute_cap_usd=Decimal("50.00"),
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
    ),
    "iggm": ToolSpec(
        slug="iggm",
        gpu_class="A100-40GB",
        # Refit from the canary: one diffusion pass measured ~24 s, plus a
        # ~30 s model load per job, so a single-pass job (complex_prediction,
        # I-1) ran ~51-59 s. 60 s/pass is the conservative bootstrap — it never
        # under-holds the 1-pass baseline and over-reserves multi-pass runs
        # (released as surplus on settle) until historical p90 takes over at
        # >=20 runs. NOTE: the scaling value is the TOTAL inference passes, not
        # raw num_samples — affinity_maturation expands per masked position, so
        # ``_effective_scaling_value`` multiplies by the FASTA mask count. The
        # session cap physically bounds any single job at ~3570 s ≈ $4.34.
        expected_gpu_seconds=60.0,
        designs_per_run_baseline=1,
        scaling_param="num_samples",
        # base_hard_cap is the ceiling at 1 pass. The scaled cap grows with the
        # effective pass count (compute_hard_cap) up to the $75 absolute ceiling.
        # For maturation (>=2 passes) the scaled cap is >= $6, above the ~$4.37 a
        # full 3600 s session can bill, so the customer is never over-charged.
        # For the rare 1-pass presets (complex_prediction / inverse_design) a
        # pathological full-session hang would bill ~$4.37, clamped here to
        # $3.00 with Ranomics absorbing the ~$1.37 remainder — an intentional
        # customer-protection choice, not an under-hold (holds are estimate-
        # driven; settle clamps the charge to this cap).
        base_hard_cap_usd=Decimal("3.00"),
        absolute_cap_usd=Decimal("75.00"),
    ),
    "proteina": ToolSpec(
        slug="proteina",
        gpu_class="A100-80GB",
        # Proteina runs as a fund-and-drain campaign of one-shard-per-container
        # jobs; it is a FIXED-container tool (see _FIXED_CONTAINER_TOOLS in
        # compute_campaigns), so the estimate AND the hold price at this baseline
        # (scale 1.0) — one whole container per shard — regardless of how many
        # designs survive the filter. Bootstrapping at 7200 s (the full 2 h
        # container the 7200 s Modal session physically enforces) makes the
        # per-shard estimate ~$12.58 marked-up and the cushioned hold clamp to
        # base_hard_cap ($15), which sits ABOVE the container's physical max spend
        # so a shard can never bill more than it held. This deliberately
        # over-reserves (released as surplus on delivered-only settle) until the
        # P4/P5 canaries measure real per-shard wall-clock and historical p90
        # takes over at >=20 runs. designs_per_run_baseline mirrors the 8-design
        # shard yield pinned in _CHUNK_SIZE_OVERRIDE. One spec covers all 4
        # presets; per-variant differences live in PRESET_CAPS + container sizing.
        #
        # CANARY-MEASURED wall-clock (P-2/P-3 @916eaaed, 8-design shard, A100-80GB):
        #   protein_binder 02_PDL1        ~553 s  -> ~$0.97 charge  (65 GB peak VRAM)
        #   ligand_binder  39_7V11_LIGAND ~1343 s -> ~$2.35 charge  (7.3 GB; slower
        #     because LigandMPNN designability runs in evaluate).
        # Both « the 7200 s cap and « the $15 hold, so the settle refunds most of
        # the hold. The hold is deliberately NOT lowered: as a fixed-container tool
        # it charges ACTUAL wall-clock and the hold must stay >= the container's
        # $12.58 physical-max charge to never under-hold a worst-case shard.
        # worst_case_gpu_seconds=7200 (=_MAX_SESSION_S in tools/proteina/modal_app.py)
        # FLOORS the cushioned hold at that $12.58 once historical p90 pulls the
        # displayed estimate down (>=20 runs): one shard = ONE fixed A100-80GB
        # container, so the floor does NOT scale with the design count
        # (worst_case_scales_with_param stays False). The child hold already prices
        # per shard at baseline (see compute_campaigns.child_hold_usd).
        expected_gpu_seconds=7200.0,
        designs_per_run_baseline=8,
        scaling_param="num_designs",
        base_hard_cap_usd=Decimal("15.00"),
        absolute_cap_usd=Decimal("60.00"),
        worst_case_gpu_seconds=7200.0,
    ),
    "esmfold2-design": ToolSpec(
        slug="esmfold2-design",
        gpu_class="H100",
        # ESMFold2 binder design fans out on n_seeds: EACH seed is a separate
        # H100 container (modal_app run_tool .spawn per seed), while batch_size
        # (1-6) runs its designs inside ONE gradient pass at the SAME wall-clock.
        # So COST scales with n_seeds, NOT n_designs_total (= n_seeds * batch_size);
        # scaling on n_designs_total would UNDER-hold up to 6x when batch_size<6.
        # Bootstrap 2400 s/seed so the 1.5x cushioned hold equals the container's
        # physical max (3600 s H100 * rate * 1.70 markup ~= $14.79/seed): the hold
        # never under-covers a worst-case seed and settle refunds the surplus.
        # base_hard_cap ($15) sits just above that per-seed max; absolute_cap
        # ($1000) covers the N_SEEDS_MAX=64 submit (~$946). Historical p90 refines
        # the displayed estimate down after >=20 runs. WAS UNREGISTERED -> the
        # atomic tier fell to the $0.10 / $10 no-spec default and under-held ~64x
        # on a max multi-seed run.
        #
        # worst_case_gpu_seconds=3600 (=_MAX_SESSION_S in
        # tools/esmfold2_design/modal_app.py) FLOORS the cushioned hold at the
        # per-seed physical max ($14.79) once p90 pulls the estimate below the 2400 s
        # bootstrap. Unlike proteina/af2 this is a FAN-OUT tool (one H100 container
        # per seed, run_tool .spawn per seed), and the single job-level hold covers
        # ALL n_seeds containers, so worst_case_scales_with_param=True makes the
        # floor scale by n_seeds — a flat per-seed floor would cover only ONE seed
        # and a p90-shrunk multi-seed job would still under-hold. Scaled floor at
        # n_seeds seeds = n_seeds * $14.79, clamped to the n_seeds-scaled hard cap.
        expected_gpu_seconds=2400.0,
        designs_per_run_baseline=1,
        scaling_param="n_seeds",
        base_hard_cap_usd=Decimal("15.00"),
        absolute_cap_usd=Decimal("1000.00"),
        worst_case_gpu_seconds=3600.0,
        worst_case_scales_with_param=True,
    ),
    "opendde": ToolSpec(
        slug="opendde",
        gpu_class="H100",
        # OpenDDE is an ATOMIC single-container tool: all seeds * samples run
        # sequentially INSIDE one H100 container physically capped at
        # _MAX_SESSION_S=3600 s (tools/opendde/modal_app.py). So the cost is a
        # FIXED container budget, not a per-design fan-out. scaling_param=None
        # (like alphafold2) prices every job at the worst-case container time:
        # 3600 s * $0.002417/s * 1.70 markup = $14.79, and base_hard_cap ($15)
        # sits just above that. n_designs_total (seeds * samples) is stamped for
        # the job record and packs predictions INTO the fixed budget; it does not
        # move the hold. worst_case_gpu_seconds=3600 FLOORS the cushioned hold at
        # $14.79 so that once historical p90 pulls the DISPLAYED estimate down
        # (>=20 runs), the HOLD still covers a single heavy job that runs the full
        # session — without the floor the p90 branch would shrink the hold and a
        # max-sampler job would under-hold. If a larger complex needs a longer
        # session, bump _MAX_SESSION_S AND these caps AND worst_case_gpu_seconds
        # TOGETHER. (Deviates from the plan's 900 s / n_designs_total / $150 model,
        # which would under-hold a single heavy run on a single container.)
        expected_gpu_seconds=3600.0,
        designs_per_run_baseline=1,
        scaling_param=None,
        base_hard_cap_usd=Decimal("15.00"),
        absolute_cap_usd=Decimal("15.00"),
        worst_case_gpu_seconds=3600.0,
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
    """
    params = dict(params or {})
    spec = TOOL_SPECS.get(tool_slug)
    preset = str(params.get("preset") or "").lower()

    if spec is None:
        # Conservative default: pretend the tool is one default A100-80GB minute.
        logger.warning(
            "estimated_cost_for_tool: no TOOL_SPEC for slug=%s", tool_slug
        )
        raw = Decimal("60") * Decimal(str(DEFAULT_USD_PER_SECOND))
        return _quantize_usd(raw * WALLET_MARKUP)

    # Per-tier bootstrap overrides retained for forward-compat with future
    # cheap tiers; today no tier sets one and we fall straight through to
    # the historical p90 lookup.
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
    actual = _effective_scaling_value(spec, params)
    scale_factor = max(actual / float(spec.designs_per_run_baseline), 1.0)
    scaled = spec.base_hard_cap_usd * Decimal(str(scale_factor))
    return _quantize_usd(min(scaled, spec.absolute_cap_usd))


def cushioned_hold_usd(
    user_id: Optional[str],
    tool_slug: str,
    params: Optional[Mapping[str, object]] = None,
) -> Decimal:
    """Return the USD amount to HOLD for one job: a cushioned point estimate.

    ``HOLD_CUSHION_MULTIPLIER`` times the point estimate
    (:func:`estimated_cost_for_tool`), clamped to the parameter-scaled hard
    cap (:func:`compute_hard_cap`). The cushion makes the reservation usually
    cover actual, so settle releases the surplus (a clean ledger) instead of
    posting a variance charge; the clamp keeps the hold at or below the tool's
    charge ceiling, where the customer is capped and Ranomics absorbs overage
    regardless, so reserving past it would just lock up wallet funds.

    This sizes the reservation only. The point estimate stays the displayed
    price and the value settle-monitoring reconciles against.
    """
    point = estimated_cost_for_tool(user_id, tool_slug, params)
    cap = compute_hard_cap(tool_slug, params)
    hold = HOLD_CUSHION_MULTIPLIER * point
    # Fixed-container floor: a tool that bills actual wall-clock up to a physical
    # session cap can ALWAYS bill its worst case, so the hold must cover it even
    # after historical p90 pulls the point estimate down. Without this the p90
    # branch lowers the HOLD (not just the displayed price) and a single heavy job
    # under-holds. The floor is itself clamped to the hard cap.
    spec = TOOL_SPECS.get(tool_slug)
    if spec is not None and spec.worst_case_gpu_seconds:
        rate = Decimal(str(GPU_USD_PER_SECOND.get(spec.gpu_class, DEFAULT_USD_PER_SECOND)))
        wc_seconds = Decimal(str(spec.worst_case_gpu_seconds))
        # Fan-out tools spawn one physical container per unit of the scaling
        # param, so their JOB-level worst case is one container's cap TIMES the
        # container count. Scale the floor by the same ratio the point estimate
        # uses (see _scale_seconds) so it covers the whole multi-unit job, not
        # just one container. Single-container tools skip this (flag defaults
        # False) and floor at one container.
        if (
            spec.worst_case_scales_with_param
            and spec.scaling_param
            and spec.designs_per_run_baseline > 0
        ):
            ratio = max(
                _effective_scaling_value(spec, params or {})
                / float(spec.designs_per_run_baseline),
                1.0,
            )
            wc_seconds *= Decimal(str(ratio))
        floor = wc_seconds * rate * WALLET_MARKUP
        hold = max(hold, min(floor, cap))
    return _quantize_usd(min(hold, cap))


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


def _iggm_mask_count(params: Mapping[str, object]) -> int:
    """Count masked (X) positions in the pasted antibody FASTA.

    The estimator receives the raw form, so ``params['fasta']`` is the
    antibody design FASTA (headers + sequence lines, X = design mask, no
    antigen record). Count X only on sequence lines. Returns 0 when the FASTA
    is absent (e.g. the live GET preview passes only query args) — the caller
    then falls back to the raw sample count.
    """
    fasta = params.get("fasta")
    if not isinstance(fasta, str) or not fasta:
        return 0
    # Uppercase before counting: ``tools.iggm.validate`` uppercases the FASTA
    # before computing n_masked (and design.py runs on the uppercased sequence),
    # so a lowercase ``x`` mask is a real design position. Counting only "X" here
    # would under-hold whenever the user typed lowercase masks.
    return sum(
        line.upper().count("X") for line in fasta.splitlines()
        if not line.lstrip().startswith(">")
    )


def _effective_scaling_value(spec: ToolSpec, params: Mapping[str, object]) -> float:
    """The scaling-parameter value, with tool-specific expansion applied.

    Almost every tool runs one inference pass per unit of its scaling
    parameter, so the raw value is used directly. IgGM's ``affinity_maturation``
    is the exception: it runs one pass PER masked position PER sample, so the
    true compute is ``num_samples * n_masked``. Scaling on raw ``num_samples``
    there would under-hold by the mask-count factor, so we expand it to mirror
    ``tools.iggm.validate`` (both derive the same product from the same inputs).

    Two callers pass different ``params`` shapes: the submit-time hold passes the
    raw form (has ``fasta`` but no ``total_passes``), while settle/estimate on a
    stored job passes the job_spec (has the pre-computed ``total_passes`` but the
    FASTA only as a parsed list, no top-level ``fasta`` string). Prefer the
    stored ``total_passes`` when present — it is the authoritative pass count —
    then fall back to deriving it from the FASTA mask count."""
    raw = _safe_float(params.get(spec.scaling_param), spec.designs_per_run_baseline)
    if spec.slug == "iggm" and str(params.get("preset") or "").lower() == "affinity_maturation":
        stored = params.get("total_passes")
        if stored is not None:
            val = _safe_float(stored, 0.0)
            if val > 0:
                return val
        n_masked = _iggm_mask_count(params)
        if n_masked > 0:
            return raw * n_masked
    return raw


def _scale_seconds(
    base_seconds: float, spec: ToolSpec, params: Mapping[str, object]
) -> float:
    """Scale a baseline gpu_seconds value by the relevant parameter ratio."""
    if not spec.scaling_param:
        return float(base_seconds)
    actual = _effective_scaling_value(spec, params)
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
