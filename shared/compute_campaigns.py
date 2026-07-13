"""Self-serve compute campaigns ("Campaigns").

A campaign is a large design request (wallet-bounded and size-agnostic: the
prepaid balance is the ceiling, not any design count) that the system splits
into many ordinary ``tool_jobs`` sub-jobs, each sized to fit one GPU
container's timeout, fanned out on Modal's autoscaler with server-side
admission control. ``MAX_SUBJOBS_PER_CAMPAIGN`` is a runaway guard, not a
product ceiling. The sub-jobs reach terminal state ONLY through the
existing poll / webhook / heartbeat / cancel / sweeper writers; this layer
is READ + LAUNCH + RECONCILE and never writes a child's terminal state.

This module (Phase 1) owns the pure planning + persistence surface:

* chunk sizing per tool (rfdiffusion / bindcraft scale linearly with the
  design count; boltzgen is budget-based against a fixed 200-design pool);
* the ``ComputeCampaign`` row dataclass and its CRUD;
* aggregate progress counts over a campaign's children.

Billing (pre-auth + admission + per-child holds) and the driver
(``drive_campaign`` + the cron tick) land in later slices; see
docs/COMPUTE-CAMPAIGNS-PLAN.md.

Deliberately separate from ``shared/campaigns.py`` (the wet-lab CRO
"Lab projects" funnel), which has an incompatible FSM, RLS, and billing
model. Do not overload that module.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Optional

from shared.credits import get_service_client
from shared.wallet_estimates import (
    cushioned_hold_usd,
    estimated_cost_for_tool,
    get_tool_spec,
)
from gpu.modal_client import preset_gpu_seconds

logger = logging.getLogger(__name__)

_TABLE = "compute_campaigns"


# ---------------------------------------------------------------------------
# Phase 1 constants
# ---------------------------------------------------------------------------

# Only tools that chunk sanely at the current pilot container ceiling are
# self-serve in Phase 1. rfantibody (~1 design/pilot container) and
# pxdesign (validator caps num_designs at 24) are gated behind Phase 4's
# campaign_chunk preset. See the plan.
SUPPORTED_TOOLS: tuple[str, ...] = ("rfdiffusion", "bindcraft", "boltzgen")

# The tool-specific form field that carries the per-chunk design count.
# boltzgen varies ``budget`` (top-N returned) rather than ``num_designs``
# (its pool is a fixed 200 inside build_payload).
_DESIGN_PARAM_KEY: Mapping[str, str] = {
    "rfdiffusion": "num_designs",
    "bindcraft": "num_designs",
    "boltzgen": "budget",
}

# boltzgen returns up to ``budget`` designs (validator caps budget at 50)
# from a fixed 200-pool per job, so 50 delivered designs is the most one
# boltzgen sub-job can yield. Chunk on that, not the pool size.
BOLTZGEN_DESIGNS_PER_JOB = 50

# Fraction of the container time budget we let a chunk plan to consume, so
# a cold-start / slow fold still finishes inside the container timeout.
_CONTAINER_UTILIZATION = 0.8

# Max sub-jobs per campaign. This is a RUNAWAY GUARD, not a product ceiling:
# fund-and-drain means campaign size is bounded by the prepaid wallet (it pauses
# when the balance cannot fund the next chunk), and the driver is now O(1) per
# tick (indexed COUNTs + contiguous-prefix dispatch, not an all-rows load), so a
# large campaign no longer degrades the engine. 50,000 sub-jobs is ~2.5M designs
# (boltzgen) / 600k (rfdiffusion), well past any real request; it only rejects
# absurd input (a typo of a billion designs). Raise freely if ever needed.
MAX_SUBJOBS_PER_CAMPAIGN = 50000

# Slack on the per-pass dispatch-attempt bound, above the open admission slots.
# The driver dispatches the next hole (idx = dispatched_count) and re-reads the
# count on a duplicate (a concurrent driver claimed it); this slack absorbs a few
# such collisions per pass while still bounding the loop so it can never spin.
_DISPATCH_ATTEMPT_SLACK = 8

# Driver defaults persisted on the campaign row. Concurrency is the per-campaign
# in-flight target; the first wave is dispatched ASYNC (drive_campaign_async, a
# daemon thread) so raising it does not block POST /runs. It is bounded by the
# global per-user in-flight cap below (2 campaigns x 16 = the 32 cap). (The 0034
# column default is 20 but create_campaign always writes this value explicitly.)
DEFAULT_CONCURRENCY_TARGET = 16
DEFAULT_MAX_ATTEMPTS = 2

# Global per-user in-flight sub-job cap across ALL of a user's campaigns. A
# load/fairness guard (stops one user flooding Modal), NOT a spend guard - the
# prepaid wallet bounds spend. Soft: concurrent drivers may briefly overshoot,
# which is harmless. ~2 campaigns at the default concurrency of 16.
GLOBAL_USER_INFLIGHT_CAP = 32

# Head-room multiplier on the summed chunk estimate so the authorized
# budget comfortably covers historical drift. Delivered-only billing
# refunds whatever is not consumed, so a conservative budget never
# overcharges — it only gates admission.
BUDGET_BUFFER = Decimal("1.15")

# Money guardrails ("Open" posture, Leo 2026-07-03). The $1000 single-job
# self-serve ceiling does NOT apply to campaigns; these replace it.
#   * Prepaid gate: a campaign will not run unless balance covers the full
#     authorized budget (checked in campaign_preauth; no debit there).
#   * Velocity cap: total campaign budgets authorized per user per UTC day.
#   * Verification: authorizations above the threshold require an approved
#     account (per_job_cap_override_usd >= budget) rather than a hard block.
DAILY_CAMPAIGN_CAP_USD = Decimal("25000")
VERIFICATION_THRESHOLD_USD = Decimal("5000")

# ID/verification (KYC) gate kill-switch. Default OFF (KYC disabled): a large
# authorization no longer requires an approved account. Re-enable without a
# code change by setting CAMPAIGN_KYC_ENABLED=1 (mirrors the CSRF_PROTECT env
# kill-switch pattern in app.py). Applies to the campaign path only; the
# velocity/daily-cap gate below is a separate guard and is always active.
CAMPAIGN_KYC_ENABLED = os.environ.get("CAMPAIGN_KYC_ENABLED", "0").strip() == "1"

# Campaign lifecycle states (mirror the CHECK in migration 0034).
CAMPAIGN_STATUSES: frozenset[str] = frozenset({
    "draft", "funded", "running", "completing",
    "completed", "completed_with_failures", "failed", "cancelled",
})

# tool_jobs statuses the progress rollup buckets.
_CHILD_STATUSES: tuple[str, ...] = (
    "pending", "running", "succeeded", "failed", "timeout", "cancelled",
)


def _quantize_usd(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Chunk planning (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkPlan:
    """The split of one design request into sub-jobs, plus its budget."""

    tool: str
    preset: str
    requested_designs: int
    chunk_size: int
    total_subjobs: int
    design_param_key: str
    est_cost_per_chunk: Decimal
    budget_usd: Decimal

    def designs_for_chunk(self, index: int) -> int:
        """Design count for the ``index``-th chunk (last chunk may be smaller)."""
        start = index * self.chunk_size
        return max(0, min(self.chunk_size, self.requested_designs - start))


# bindcraft's 3-designs/chunk pilot sizing (7200s container) is the worst-scaling
# campaign tool (6667 sub-jobs for 20k designs). Its Modal function allows a 23h
# session, so campaigns size bindcraft against a much larger container AND pass a
# matching session budget (see _campaign_session_inputs), cutting its sub-job
# count ~5x. Kept well under the 23h ceiling; bindcraft streams results per
# candidate, so a slow chunk that reaches its budget still returns what it made.
_BINDCRAFT_CAMPAIGN_CONTAINER_S = 36000  # 10h -> 16 designs/chunk at 0.8 util


def _campaign_container_seconds(tool: str) -> int:
    """GPU-seconds a campaign sizes ONE sub-job against.

    Defaults to the pilot container; bindcraft campaigns use a bigger one because
    its Modal session runs up to 23h.
    """
    if tool == "bindcraft":
        return _BINDCRAFT_CAMPAIGN_CONTAINER_S
    return preset_gpu_seconds(tool, "pilot")


def _campaign_session_inputs(tool: str) -> dict:
    """Extra Modal inputs so a bigger campaign chunk gets a matching session budget.

    A bindcraft campaign chunk holds ~16 designs (vs the 3/chunk pilot), which
    needs more than the default 4h ``_total_budget_hours`` or the pipeline stops
    early. Other tools keep the default (their chunks are far shorter).
    """
    if tool == "bindcraft":
        return {"_total_budget_hours": _BINDCRAFT_CAMPAIGN_CONTAINER_S / 3600}
    return {}


def _chunk_size_for(tool: str) -> int:
    """Designs per sub-job for ``tool``, sized to one campaign container.

    boltzgen is special (budget-based, fixed pool). The linear tools derive
    the size from GPU-seconds-per-design vs the campaign container's GPU-seconds
    budget, so a chunk comfortably fits one container.
    """
    if tool == "boltzgen":
        return BOLTZGEN_DESIGNS_PER_JOB
    spec = get_tool_spec(tool)
    if spec is None or spec.designs_per_run_baseline <= 0:
        # Conservative fallback; should not happen for SUPPORTED_TOOLS.
        return 1
    gpu_s_per_design = float(spec.expected_gpu_seconds) / float(
        spec.designs_per_run_baseline
    )
    container_s = _campaign_container_seconds(tool)
    if gpu_s_per_design <= 0 or container_s <= 0:
        return spec.designs_per_run_baseline
    size = int((container_s * _CONTAINER_UTILIZATION) / gpu_s_per_design)
    # Never chunk below the tool's own baseline (keeps per-chunk cost
    # efficient; the wallet estimate floors its multiplier at 1.0 anyway).
    return max(size, spec.designs_per_run_baseline)


def single_container_ceiling(tool: str) -> int:
    """Max designs one GPU container reliably does for ``tool``.

    This is exactly the campaign chunk size: the point above which a single-job
    submit would need more than one container and should instead fan out as a
    campaign. Used by the tool forms (D1 auto-route threshold) and the
    ``tool_submit`` backstop. Only meaningful for ``SUPPORTED_TOOLS``.
    """
    return _chunk_size_for(tool)


def _estimate_chunk_cost(tool: str, chunk_size: int) -> Decimal:
    """USD estimate for ONE sub-job of ``tool`` at ``chunk_size`` designs."""
    spec = get_tool_spec(tool)
    if tool == "boltzgen":
        # One fixed 200-design pool per job — the returned-count (budget)
        # does not change GPU cost, so estimate at the baseline (scale
        # factor 1.0) rather than scaling by the chunk's budget.
        designs_for_estimate = spec.designs_per_run_baseline if spec else 2
    else:
        designs_for_estimate = chunk_size
    return estimated_cost_for_tool(
        None, tool, {"num_designs": designs_for_estimate, "preset": "pilot"}
    )


def plan_chunks(tool: str, requested_designs: int) -> ChunkPlan:
    """Plan how a design request splits into sub-jobs. Pure; raises ValueError.

    The ValueError messages are user-facing (surfaced on the create form).
    """
    if tool not in SUPPORTED_TOOLS:
        raise ValueError(
            f"{tool} is not available for self-serve campaigns yet."
        )
    try:
        requested = int(requested_designs)
    except (TypeError, ValueError):
        raise ValueError("Number of designs must be a whole number.")
    if requested < 1:
        raise ValueError("Number of designs must be at least 1.")

    chunk_size = _chunk_size_for(tool)
    total_subjobs = math.ceil(requested / chunk_size)
    if total_subjobs > MAX_SUBJOBS_PER_CAMPAIGN:
        max_designs = MAX_SUBJOBS_PER_CAMPAIGN * chunk_size
        raise ValueError(
            f"That request needs {total_subjobs} sub-jobs, over the current "
            f"per-campaign limit of {MAX_SUBJOBS_PER_CAMPAIGN} "
            f"({max_designs} designs for {tool}). Reduce the count or split "
            f"it into multiple campaigns."
        )

    est_per_chunk = _estimate_chunk_cost(tool, chunk_size)
    budget = _quantize_usd(
        Decimal(total_subjobs) * est_per_chunk * BUDGET_BUFFER
    )
    return ChunkPlan(
        tool=tool,
        preset="pilot",
        requested_designs=requested,
        chunk_size=chunk_size,
        total_subjobs=total_subjobs,
        design_param_key=_DESIGN_PARAM_KEY[tool],
        est_cost_per_chunk=est_per_chunk,
        budget_usd=budget,
    )


def sanitize_shared_params(tool: str, params: Mapping[str, object]) -> dict:
    """Strip per-chunk + private keys so every wave rebuilds an identical base.

    Drops underscore-prefixed keys (private wiring like ``_workspace``),
    the design-count key (injected per chunk), and ``preset`` (carried on
    the campaign row).
    """
    # Strip EVERY tool's design key (not just this tool's) plus preset.
    # A stray cross-tool key must not survive into job.inputs: e.g. a bogus
    # num_designs field on a boltzgen campaign (whose own design key is
    # `budget`) would otherwise reach compute_hard_cap, whose boltzgen
    # scaling_param IS num_designs, and inflate the per-child hard cap.
    design_keys = set(_DESIGN_PARAM_KEY.values())
    out: dict = {}
    for key, value in dict(params or {}).items():
        if not isinstance(key, str):
            continue
        if key.startswith("_"):
            continue
        if key in design_keys or key == "preset":
            continue
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# Campaign row
# ---------------------------------------------------------------------------


@dataclass
class ComputeCampaign:
    """A row of ``public.compute_campaigns``."""

    id: str
    user_id: str
    tool: str
    preset: str
    status: str
    requested_designs: int
    chunk_size: int
    total_subjobs: int
    concurrency_target: int
    max_attempts: int
    budget_usd: Decimal
    reserved_usd: Decimal
    spent_usd: Decimal
    refunded_usd: Decimal
    params: dict = field(default_factory=dict)
    name: Optional[str] = None
    target_pdb_id: Optional[str] = None
    target_storage_path: Optional[str] = None
    target_name: Optional[str] = None
    escrow_tx_id: Optional[int] = None
    created_at: Optional[str] = None
    confirmed_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_tick_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "ComputeCampaign":
        def _dec(key: str) -> Decimal:
            return Decimal(str(row.get(key) or 0))

        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            tool=row["tool"],
            preset=row["preset"],
            status=row["status"],
            requested_designs=int(row.get("requested_designs") or 0),
            chunk_size=int(row.get("chunk_size") or 0),
            total_subjobs=int(row.get("total_subjobs") or 0),
            concurrency_target=int(
                row.get("concurrency_target") or DEFAULT_CONCURRENCY_TARGET
            ),
            max_attempts=int(row.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
            budget_usd=_dec("budget_usd"),
            reserved_usd=_dec("reserved_usd"),
            spent_usd=_dec("spent_usd"),
            refunded_usd=_dec("refunded_usd"),
            params=row.get("params") or {},
            name=row.get("name"),
            target_pdb_id=row.get("target_pdb_id"),
            target_storage_path=row.get("target_storage_path"),
            target_name=row.get("target_name"),
            escrow_tx_id=row.get("escrow_tx_id"),
            created_at=row.get("created_at"),
            confirmed_at=row.get("confirmed_at"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            last_tick_at=row.get("last_tick_at"),
        )

    def to_dict(self) -> dict:
        """JSON-friendly view for templates + status.json."""
        return {
            "id": self.id,
            "name": self.name,
            "tool": self.tool,
            "preset": self.preset,
            "status": self.status,
            "requested_designs": self.requested_designs,
            "chunk_size": self.chunk_size,
            "total_subjobs": self.total_subjobs,
            "target_name": self.target_name,
            "budget_usd": float(self.budget_usd),
            # NOTE: spent/reserved/refunded are advisory columns the Phase-1
            # driver does not populate (the wallet ledger is the source of
            # truth), so they are deliberately NOT emitted here — reporting a
            # flat $0 spent while a campaign is billing would be misleading.
            # Phase 3 reconciles them from the ledger and restores them.
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    def designs_for_chunk(self, index: int) -> int:
        start = index * self.chunk_size
        return max(0, min(self.chunk_size, self.requested_designs - start))


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_campaign(
    *,
    user_id: str,
    tool: str,
    params: Mapping[str, object],
    requested_designs: int,
    name: Optional[str] = None,
    target_pdb_id: Optional[str] = None,
    target_storage_path: Optional[str] = None,
    target_name: Optional[str] = None,
) -> Optional[ComputeCampaign]:
    """Insert a ``draft`` campaign row from a validated request.

    Raises ValueError (user-facing) when the request cannot be planned
    (unsupported tool, bad count, over the sub-job cap). Returns None on a
    persistence failure.
    """
    plan = plan_chunks(tool, requested_designs)
    client = get_service_client()
    if client is None:
        logger.error("create_campaign: Supabase service client unavailable.")
        return None

    clean_name: Optional[str] = None
    if isinstance(name, str) and name.strip():
        clean_name = name.strip()[:80]

    row = {
        "user_id": user_id,
        "name": clean_name,
        "tool": tool,
        "preset": plan.preset,
        "target_pdb_id": target_pdb_id,
        "target_storage_path": target_storage_path,
        "target_name": target_name,
        "params": sanitize_shared_params(tool, params),
        "requested_designs": plan.requested_designs,
        "chunk_size": plan.chunk_size,
        "total_subjobs": plan.total_subjobs,
        "concurrency_target": DEFAULT_CONCURRENCY_TARGET,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "status": "draft",
        "budget_usd": float(plan.budget_usd),
    }
    try:
        response = client.table(_TABLE).insert(row).execute()
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
        return ComputeCampaign.from_row(rows[0])
    except Exception:
        logger.error("create_campaign: insert failed", exc_info=True)
        return None


def get_campaign(
    campaign_id: str, *, user_id: Optional[str] = None
) -> Optional[ComputeCampaign]:
    """Fetch a campaign by id. Pass ``user_id`` to enforce owner scope."""
    client = get_service_client()
    if client is None:
        return None
    try:
        query = client.table(_TABLE).select("*").eq("id", campaign_id)
        if user_id is not None:
            query = query.eq("user_id", user_id)
        response = query.single().execute()
    except Exception:
        return None
    data = getattr(response, "data", None)
    if not data:
        return None
    return ComputeCampaign.from_row(data)


def list_campaigns_for_user(
    user_id: str, *, limit: int = 100
) -> list[ComputeCampaign]:
    """Return the user's campaigns, newest first."""
    client = get_service_client()
    if client is None:
        return []
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
    except Exception:
        logger.warning("list_campaigns_for_user failed for %s", user_id, exc_info=True)
        return []
    return [ComputeCampaign.from_row(r) for r in rows]


def get_progress_counts(campaign_id: str) -> dict:
    """Aggregate sub-job counts by status for a campaign.

    Count-based: one indexed exact ``COUNT`` (head request, no row transfer) per
    status bucket plus the total, over the partial (campaign_id, status) index.
    This keeps the UI status poll O(1) per bucket for extreme-N campaigns instead
    of loading every sub-job row (it was the last O(N) path; the driver is already
    O(1)). Returns ``{"total": n, <status>: count, ...}`` with every bucket in
    :data:`_CHILD_STATUSES` present (0 when absent).

    The total is read FIRST with a sentinel default: if that read fails, return a
    self-consistent all-zeros dict rather than risk buckets summing above a zero
    total (the pre-count-based behavior). A per-bucket read failure then only
    UNDER-counts that bucket (always <= total), so the dict stays internally
    consistent.
    """
    total = _count_children(campaign_id, default=-1)
    if total < 0:
        counts = {status: 0 for status in _CHILD_STATUSES}
        counts["total"] = 0
        return counts
    counts = {
        status: _count_children(campaign_id, (status,))
        for status in _CHILD_STATUSES
    }
    counts["total"] = total
    return counts


# ---------------------------------------------------------------------------
# Billing: prepaid pre-authorization + per-child estimate + admission
# ---------------------------------------------------------------------------
#
# Billing model (see docs/COMPUTE-CAMPAIGNS-PLAN.md): NO escrow debit. The
# pre-auth is a pure gate (balance + frozen + velocity + verification); it
# does NOT move money. Real money moves as ordinary per-child wallet holds
# placed by the driver via the UNCHANGED reserve_hold path and settled by
# the UNCHANGED settle path, so balance == SUM(ledger) holds automatically
# and delivered-only billing falls out for free. Phase 2 migration 0035
# retired the per-day spend cap, so per-child holds via the unchanged
# reserve_hold path are bounded only by the prepaid wallet balance; a
# campaign pauses when the balance cannot fund the next chunk.


PREAUTH_OK = "ok"
PREAUTH_NO_WALLET = "wallet_unavailable"
PREAUTH_FROZEN = "wallet_frozen"
PREAUTH_INSUFFICIENT = "insufficient_balance"
PREAUTH_VERIFICATION = "verification_required"
PREAUTH_VELOCITY = "daily_campaign_cap"


@dataclass(frozen=True)
class PreauthResult:
    """Outcome of the campaign prepaid pre-authorization gate."""

    ok: bool
    reason: str
    balance_usd: Decimal
    budget_usd: Decimal
    required_usd: Decimal = Decimal("0")  # balance needed to START (first wave)


def campaign_preauth(
    user_id: str,
    budget_usd: Decimal,
    first_wave_usd: Optional[Decimal] = None,
) -> PreauthResult:
    """Prepaid START gate for a campaign. Checks but never debits.

    Under fund-and-drain the wallet balance IS the ceiling, so the start gate is
    "can the wallet fund the FIRST WAVE" (``first_wave_usd`` = concurrency_target
    x per-chunk hold), not the full budget. The rest is funded as the campaign
    drains: it pauses (``paused_insufficient_funds``) when the balance cannot
    cover the next chunk and resumes on a top-up. ``budget_usd`` stays a
    non-binding forecast, used only for the (interim) verification + velocity
    gates. When ``first_wave_usd`` is None the gate falls back to the full budget
    (legacy callers).
    """
    budget_usd = Decimal(str(budget_usd))
    gate_usd = (
        Decimal(str(first_wave_usd)) if first_wave_usd is not None else budget_usd
    )
    from shared.wallet import get_or_create_wallet  # noqa: PLC0415

    wallet = get_or_create_wallet(user_id)
    if not wallet:
        return PreauthResult(False, PREAUTH_NO_WALLET, Decimal("0"), budget_usd, gate_usd)
    balance = Decimal(str(wallet.get("balance_usd") or 0))
    if wallet.get("wallet_frozen"):
        return PreauthResult(False, PREAUTH_FROZEN, balance, budget_usd, gate_usd)
    if balance < gate_usd:
        return PreauthResult(False, PREAUTH_INSUFFICIENT, balance, budget_usd, gate_usd)
    if CAMPAIGN_KYC_ENABLED and budget_usd > VERIFICATION_THRESHOLD_USD:
        override = wallet.get("per_job_cap_override_usd")
        approved = override is not None and Decimal(str(override)) >= budget_usd
        if not approved:
            return PreauthResult(
                False, PREAUTH_VERIFICATION, balance, budget_usd, gate_usd
            )
    spent_today = _campaign_spend_today(user_id)
    if spent_today + budget_usd > DAILY_CAMPAIGN_CAP_USD:
        return PreauthResult(False, PREAUTH_VELOCITY, balance, budget_usd, gate_usd)
    return PreauthResult(True, PREAUTH_OK, balance, budget_usd, gate_usd)


def _campaign_spend_today(user_id: str) -> Decimal:
    """Sum of budgets authorized for this user's campaigns since UTC midnight.

    Draft and cancelled campaigns are excluded (a draft never funded; a
    cancelled campaign refunded its unspent budget). Feeds the velocity cap.
    """
    client = get_service_client()
    if client is None:
        return Decimal("0")
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    try:
        resp = (
            client.table(_TABLE)
            .select("budget_usd,status")
            .eq("user_id", user_id)
            .gte("created_at", start)
            .execute()
        )
        rows = list(getattr(resp, "data", None) or [])
    except Exception:
        logger.warning("_campaign_spend_today failed for %s", user_id, exc_info=True)
        return Decimal("0")
    total = Decimal("0")
    for r in rows:
        if r.get("status") in ("draft", "cancelled"):
            continue
        total += Decimal(str(r.get("budget_usd") or 0))
    return total


def estimate_child_cost(tool: str, design_count: int) -> Decimal:
    """Point-estimate USD for one sub-job at ``design_count`` designs.

    The non-binding forecast input and the value stored on the child as
    ``_wallet.estimate_usd``. boltzgen is flat per job (fixed 200-pool); the
    linear tools scale by the count. The wallet HOLD (reservation) is sized
    separately and cushioned, see :func:`child_hold_usd`.
    """
    return _estimate_chunk_cost(tool, int(design_count))


def child_hold_usd(tool: str, design_count: int) -> Decimal:
    """Cushioned USD to HOLD for one sub-job (the wallet reservation).

    A cushion above the point estimate so actual usually settles under the
    hold, releasing surplus (a clean ledger) instead of posting a variance
    charge. Clamped to the per-tool hard cap by
    :func:`shared.wallet_estimates.cushioned_hold_usd`. boltzgen prices at its
    fixed-pool baseline; the linear tools scale by the design count.
    """
    spec = get_tool_spec(tool)
    if tool == "boltzgen":
        designs_for_estimate = spec.designs_per_run_baseline if spec else 2
    else:
        designs_for_estimate = int(design_count)
    return cushioned_hold_usd(
        None, tool, {"num_designs": designs_for_estimate, "preset": "pilot"}
    )


def first_wave_hold_usd(
    plan: "ChunkPlan", concurrency_target: int = DEFAULT_CONCURRENCY_TARGET
) -> Decimal:
    """Wallet amount needed to START a campaign under fund-and-drain.

    Enough to hold the first concurrency wave of sub-jobs (worst case: a full
    wave of full-size chunks at the cushioned per-chunk hold). The remaining
    chunks are funded as the campaign drains; it pauses if the balance runs low
    and resumes on a top-up. This is a START gate, NOT a ceiling.
    """
    waves = min(int(plan.total_subjobs), int(concurrency_target))
    waves = max(waves, 1)
    return _quantize_usd(child_hold_usd(plan.tool, plan.chunk_size) * waves)


def can_dispatch_more(
    campaign: "ComputeCampaign", counts: Mapping[str, int], dispatched_count: int
) -> bool:
    """Pure admission predicate: is there room to launch another sub-job?

    True iff undispatched chunks remain AND the in-flight (pending +
    running) count is under the campaign's concurrency target. The budget
    is respected structurally (``total_subjobs`` chunks priced into
    ``budget_usd`` at create) and, per child, by the atomic balance +
    hard-cap guards inside reserve_hold.
    """
    if dispatched_count >= campaign.total_subjobs:
        return False
    in_flight = int(counts.get("pending", 0)) + int(counts.get("running", 0))
    return in_flight < campaign.concurrency_target


# ---------------------------------------------------------------------------
# Driver: fund -> dispatch -> reconcile -> finalize
# ---------------------------------------------------------------------------
#
# The driver is READ + LAUNCH + RECONCILE. It reads aggregate child state,
# creates NEW sub-job rows, and updates the campaign row — it NEVER flips
# an existing child's terminal state (that stays with the poll / webhook /
# heartbeat / cancel / sweeper writers). Idempotency against concurrent
# drivers rests on the DB UNIQUE(campaign_id, chunk_index, attempt) index
# plus the CAS launch (set_modal_call only on a NULL function_call_id row);
# a per-campaign advisory lock + retries are Phase 2.

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_base() -> str:
    import os  # noqa: PLC0415
    return os.environ.get("PUBLIC_BASE_URL", "https://tools.ranomics.com").rstrip("/")


def _webhook_url(job_id: str, job_token: str) -> str:
    return f"{_public_base()}/webhooks/modal/{job_id}/{job_token}"


def _upload_urls_endpoint(job_id: str, job_token: str) -> str:
    return f"{_public_base()}/api/upload-urls/{job_id}/{job_token}"


def _ensure_adapters() -> None:
    """Import the supported tool packages so their adapters are registered.

    Cheap + idempotent; a no-op when app.py has already imported them.
    """
    try:
        import tools.rfdiffusion  # noqa: F401,PLC0415
        import tools.bindcraft  # noqa: F401,PLC0415
        import tools.boltzgen  # noqa: F401,PLC0415
    except Exception:
        logger.warning("_ensure_adapters: tool import failed", exc_info=True)


def _update_campaign(campaign_id: str, fields: dict) -> None:
    client = get_service_client()
    if client is None:
        return
    try:
        client.table(_TABLE).update(fields).eq("id", campaign_id).execute()
    except Exception:
        logger.warning("_update_campaign failed for %s", campaign_id, exc_info=True)


def _cas_transition(
    campaign_id: str,
    to_status: str,
    allowed_from: tuple,
    extra: Optional[dict] = None,
) -> bool:
    """Atomically move ``status`` to ``to_status`` only from an allowed state.

    Returns True iff this call actually performed the transition (i.e. the row
    was in one of ``allowed_from`` when the UPDATE ran). Because the WHERE
    filters on the pre-update status, exactly one of several concurrent drivers
    wins the transition — this is what makes the pause email fire exactly once.
    """
    client = get_service_client()
    if client is None:
        return False
    fields = {"status": to_status}
    if extra:
        fields.update(extra)
    try:
        resp = (
            client.table(_TABLE)
            .update(fields)
            .eq("id", campaign_id)
            .in_("status", list(allowed_from))
            .execute()
        )
        return bool(getattr(resp, "data", None))
    except Exception:
        logger.warning("_cas_transition failed for %s", campaign_id, exc_info=True)
        return False


def _wallet_balance_below(user_id: str, amount) -> bool:  # noqa: ANN001
    """True if the wallet balance cannot cover ``amount`` (a fund pause).

    Best-effort and advisory only (the authoritative refusal already happened
    atomically inside reserve_hold). On any read failure return False so the
    chunk is retried as a transient skip rather than mislabeled a fund pause.
    """
    from shared.wallet import get_or_create_wallet  # noqa: PLC0415
    try:
        wallet = get_or_create_wallet(user_id)
    except Exception:
        logger.warning(
            "_wallet_balance_below read failed for %s", user_id, exc_info=True
        )
        return False
    if not wallet:
        return False
    try:
        balance = Decimal(str(wallet.get("balance_usd") or 0))
        return balance < Decimal(str(amount))
    except Exception:
        return False


def fund_campaign(campaign_id: str) -> None:
    """Mark a draft campaign funded (called by the route after preauth)."""
    _update_campaign(campaign_id, {"status": "funded", "confirmed_at": _now_iso()})


def _dispatch_chunk(campaign: "ComputeCampaign", chunk_index: int) -> str:
    """Reserve a per-child hold, create the sub-job, and spawn it on Modal.

    Returns one of:
      * ``"launched"`` — a child row was created and a Modal run started.
      * ``"failed"``   — a child row exists but reached a terminal failure
                          (modal submit failed); the chunk IS dispatched.
      * ``"skipped"``  — NO child row was created (transient hold refusal or
                          transient insert failure); retry this SAME index on a
                          later drive pass (the frontier does not advance).
      * ``"duplicate"`` — NO new row: a concurrent driver already created this
                          (campaign_id, chunk_index). The index IS claimed, so
                          the caller resyncs the frontier and moves on.
      * ``"insufficient_funds"`` — the wallet balance cannot cover this
                          chunk's hold; the campaign should pause (the next
                          chunk cannot be funded either) and resume on top-up.
    On any failure the hold is released so no reservation is stranded.
    """
    from shared.wallet import release_hold, reserve_hold  # noqa: PLC0415
    from shared.jobs import (  # noqa: PLC0415
        create_job, mark_failed, set_modal_call,
    )
    from shared.storage import presigned_input_url  # noqa: PLC0415
    from gpu.modal_client import ModalClient  # noqa: PLC0415
    from tools import base as tool_base  # noqa: PLC0415

    _ensure_adapters()
    adapter = tool_base.get(campaign.tool)
    if adapter is None:
        logger.error("campaign %s: no adapter for %s", campaign.id, campaign.tool)
        return "skipped"

    design_count = campaign.designs_for_chunk(chunk_index)
    if design_count <= 0:
        return "skipped"

    # Reconstruct the validated tool inputs for this chunk: shared params
    # + the per-chunk design count under the tool's design key + preset.
    base_inputs = dict(campaign.params or {})
    base_inputs[_DESIGN_PARAM_KEY[campaign.tool]] = design_count
    base_inputs["preset"] = campaign.preset

    point_estimate = estimate_child_cost(campaign.tool, design_count)
    hold_amount = child_hold_usd(campaign.tool, design_count)
    hold_tx_id = reserve_hold(
        campaign.user_id, campaign.tool, None, hold_amount, base_inputs
    )
    if not hold_tx_id:
        # Classify the refusal. reserve_hold already made the authoritative,
        # atomic decision; this read only routes the campaign to the right
        # state. A balance shortfall pauses the campaign (the next chunk costs
        # the same or more, so spinning is pointless); a transient/duplicate/
        # cap refusal is retried on a later pass.
        if _wallet_balance_below(campaign.user_id, hold_amount):
            logger.info(
                "campaign %s chunk %s: insufficient funds for hold %s; pausing",
                campaign.id, chunk_index, hold_amount,
            )
            return "insufficient_funds"
        logger.info(
            "campaign %s chunk %s: hold not placed (transient/cap); will retry",
            campaign.id, chunk_index,
        )
        return "skipped"

    child_inputs = dict(base_inputs)
    child_inputs["_wallet"] = {
        "hold_tx_id": hold_tx_id,
        # The point estimate, NOT the cushioned hold: this is the job's
        # forecast price and what the settle-monitor reconciles against.
        "estimate_usd": str(point_estimate),
        "tool_slug": campaign.tool,
    }

    child = create_job(
        user_id=campaign.user_id,
        tool=campaign.tool,
        preset=campaign.preset,
        inputs=child_inputs,
        campaign_id=campaign.id,
        chunk_index=chunk_index,
        attempt=1,
        campaign_label=campaign.name or None,
    )
    if child is None:
        # No row: a racing driver won the UNIQUE(campaign_id, chunk_index,
        # attempt) (real duplicate), OR a transient insert error (create_job
        # returns None for both). Release the hold so nothing is stranded; the
        # winner (if any) keeps its own hold. Report "duplicate"; the caller
        # resyncs the frontier from the count, which advances only for a real
        # duplicate, so a transient failure is retried next pass, not skipped.
        try:
            release_hold(hold_tx_id, reason="campaign_chunk_create_failed")
        except Exception:
            logger.warning("release_hold after create fail raised", exc_info=True)
        return "duplicate"

    presigned_url = ""
    if campaign.target_storage_path:
        try:
            presigned_url = presigned_input_url(
                campaign.target_storage_path, expires_seconds=7200
            )
        except Exception:
            logger.warning(
                "campaign %s chunk %s: presign failed", campaign.id, chunk_index,
                exc_info=True,
            )

    # Build the payload + submit under ONE guard: a build_payload or
    # URL-build failure here (after the row + hold already exist) must take
    # the same fail-and-refund path as a submit failure, or the child would
    # orphan as pending holding money until the 30-min sweeper.
    try:
        job_spec = adapter.build_payload(child_inputs, presigned_url)
        webhook_url = _webhook_url(child.id, child.job_token)
        upload_urls_endpoint = _upload_urls_endpoint(child.id, child.job_token)
        submit_result = ModalClient().submit(
            campaign.tool,
            campaign.preset,
            inputs={
                **job_spec,
                "_input_pdb_url": presigned_url,
                "_input_presigned_url": presigned_url,
                "_upload_urls_endpoint": upload_urls_endpoint,
                # Bigger campaign chunks (bindcraft) need a matching session budget
                # or the pipeline stops at the default 4h.
                **_campaign_session_inputs(campaign.tool),
            },
            job_id=child.id,
            job_token=child.job_token,
            webhook_url=webhook_url,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("campaign %s chunk %s: dispatch failed", campaign.id, chunk_index)
        # Mirror tool_submit: fail the child row and release its hold
        # explicitly. Deliberately NOT complete_job — that would re-fire the
        # inline drive hook and recurse through the remaining chunks if Modal
        # is down. The child is terminal-failed (modal-submit) with a full
        # refund; the cron tick reconciles + finalizes.
        mark_failed(child.id, error={"bucket": "modal-submit", "detail": str(exc)})
        try:
            release_hold(hold_tx_id, reason="campaign_modal_submit_failed")
        except Exception:
            logger.warning("release_hold after submit fail raised", exc_info=True)
        return "failed"

    fc_id = submit_result.get("function_call_id")
    if fc_id:
        set_modal_call(child.id, fc_id)
    return "launched"


def _user_inflight_subjobs(user_id: str) -> int:
    """Count a user's in-flight (pending+running) campaign sub-jobs across ALL
    their campaigns. Feeds the global per-user in-flight cap (a load/fairness
    guard, not a spend guard). Best-effort: 0 on any read failure so a transient
    read error never blocks dispatch.
    """
    client = get_service_client()
    if client is None:
        return 0
    try:
        resp = (
            client.table("tool_jobs")
            .select("campaign_id")
            .eq("user_id", user_id)
            .in_("status", ["pending", "running"])
            .execute()
        )
        rows = list(getattr(resp, "data", None) or [])
    except Exception:
        logger.warning(
            "_user_inflight_subjobs failed for %s", user_id, exc_info=True
        )
        return 0
    return sum(1 for r in rows if r.get("campaign_id"))


def _count_children(
    campaign_id: str,
    statuses: "tuple | list | None" = None,
    default: int = 0,
) -> int:
    """COUNT of a campaign's sub-jobs, optionally filtered to ``statuses``.

    A head + exact count (no row transfer) over the partial (campaign_id, status)
    index, so it is O(1)-ish regardless of campaign size. This is what keeps the
    driver from loading every sub-job row on each tick.

    Returns ``default`` on any read failure (or a missing count). Callers pass a
    fail-safe default: dispatch reads 0 (a failed count just retries next pass),
    while finalize reads a value that PREVENTS finalizing on a failed count, so a
    transient error can never terminalize a campaign whose children are still
    running.
    """
    client = get_service_client()
    if client is None:
        return default
    try:
        q = (
            client.table("tool_jobs")
            .select("id", count="exact", head=True)
            .eq("campaign_id", campaign_id)
        )
        if statuses:
            q = q.in_("status", list(statuses))
        resp = q.execute()
        cnt = getattr(resp, "count", None)
        return int(cnt) if cnt is not None else default
    except Exception:
        logger.warning("_count_children failed for %s", campaign_id, exc_info=True)
        return default


def _chunk_row_exists(campaign_id: str, chunk_index: int) -> bool:
    """True if a sub-job row already exists at ``chunk_index`` (indexed count)."""
    client = get_service_client()
    if client is None:
        return False
    try:
        resp = (
            client.table("tool_jobs")
            .select("id", count="exact", head=True)
            .eq("campaign_id", campaign_id)
            .eq("chunk_index", chunk_index)
            .execute()
        )
        cnt = getattr(resp, "count", None)
        return int(cnt) > 0 if cnt is not None else False
    except Exception:
        logger.warning(
            "_chunk_row_exists failed for %s/%s", campaign_id, chunk_index,
            exc_info=True,
        )
        return False


def _lowest_missing_chunk_index(campaign_id: str, total: int) -> "int | None":
    """Lowest chunk_index in ``[0, total)`` with no row, or None if none.

    O(N) — the ONLY non-O(1) path in the driver, and it runs solely to REPAIR a
    non-contiguous campaign (a legacy/anomalous gap). The count-based driver
    never creates gaps for campaigns it started, so this is a safety net for rows
    that predate it, not a hot path.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        resp = (
            client.table("tool_jobs")
            .select("chunk_index")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        present = {r.get("chunk_index") for r in getattr(resp, "data", None) or []}
    except Exception:
        logger.warning(
            "_lowest_missing_chunk_index failed for %s", campaign_id, exc_info=True
        )
        return None
    for i in range(total):
        if i not in present:
            return i
    return None


def drive_campaign(campaign_id: str, max_dispatch: "int | None" = None) -> int:
    """Reconcile a campaign and dispatch as many sub-jobs as admission allows.

    Returns the number of chunks launched on THIS call. ``max_dispatch`` caps how
    many chunks a single call launches; the cron's round-robin fairness uses it to
    interleave a user's concurrent campaigns over their shared wallet. ``None``
    means no cap. The cap can only launch FEWER chunks, never more, and never
    forces a pause, so it is money-neutral: a capped call simply defers the rest to
    the next drive (cron backstop / inline hook).

    Safe to call repeatedly and concurrently (the DB uniqueness + CAS
    launch make double-dispatch impossible). Triggered at create (async via
    :func:`drive_campaign_async`), by the inline hook on child completion, and
    by the cron backstop.

    O(1) per tick regardless of campaign size: it reads a couple of indexed
    COUNTs and dispatches the next hole, instead of loading every sub-job row.
    Chunks are dispatched lowest-index-first and the frontier only advances when
    a row is created, so the rows are always a CONTIGUOUS PREFIX ``[0,
    dispatched)`` and the next chunk to launch is exactly ``dispatched_count``.
    A duplicate (a concurrent driver claimed that index) just re-reads the count.
    """
    campaign = get_campaign(campaign_id)
    if campaign is None:
        return 0
    if campaign.status in ("draft", "completed", "completed_with_failures",
                           "failed", "cancelled"):
        return 0

    entry_status = campaign.status  # funded | running | paused_insufficient_funds

    total = campaign.total_subjobs
    dispatched = _count_children(campaign_id)
    in_flight = _count_children(campaign_id, ("pending", "running"))

    # Admission loop, count-based. ``dispatched`` is the frontier: rows exist for
    # [0, dispatched), so the next chunk to launch is index ``dispatched``. The
    # frontier only advances when a row is created, so no holes form.
    #
    # Two admission bounds: the per-campaign concurrency target AND the global
    # per-user in-flight cap across all the user's campaigns (a load guard, soft
    # under concurrent drivers). ``user_inflight`` already includes this
    # campaign's own in-flight children, so we increment it as we launch. The
    # attempt bound guarantees the pass terminates even under contention.
    user_inflight = _user_inflight_subjobs(campaign.user_id)
    launched_any = False
    launched_count = 0
    hit_insufficient = False
    attempts = 0
    attempt_budget = (campaign.concurrency_target - in_flight) + _DISPATCH_ATTEMPT_SLACK
    while (in_flight < campaign.concurrency_target
           and user_inflight < GLOBAL_USER_INFLIGHT_CAP
           and dispatched < total
           and attempts < attempt_budget
           and (max_dispatch is None or launched_count < max_dispatch)):
        attempts += 1
        outcome = _dispatch_chunk(campaign, dispatched)
        if outcome == "launched":
            dispatched += 1
            in_flight += 1
            user_inflight += 1
            launched_any = True
            launched_count += 1
        elif outcome == "failed":
            # A (failed) child row exists at this index; the frontier advances.
            dispatched += 1
        elif outcome == "duplicate":
            # create_job returned None: a concurrent driver claimed this index
            # (real duplicate) OR a transient insert error. Resync the frontier
            # from the authoritative count.
            resynced = _count_children(campaign_id)
            if resynced > dispatched:
                # The count advanced: a concurrent driver claimed the frontier;
                # move to the next hole.
                dispatched = resynced
            elif not _chunk_row_exists(campaign_id, dispatched):
                # Count unchanged and this index is still empty: the None was a
                # transient insert error, not a duplicate. Stop and retry this
                # same index on the next drive (no hole forms).
                break
            else:
                # Count unchanged yet this index IS filled: the rows are NOT a
                # contiguous prefix (a legacy/anomalous gap below the frontier;
                # the count-based driver never creates one). Repair it by jumping
                # to the true lowest hole. O(N), but only on a real gap.
                hole = _lowest_missing_chunk_index(campaign_id, total)
                if hole is None or hole >= total or hole == dispatched:
                    break
                dispatched = hole
        elif outcome == "insufficient_funds":
            hit_insufficient = True
            break
        else:  # "skipped": transient, no row created; retry this index later.
            break

    undispatched_remain = dispatched < total

    # Fund-and-drain state machine. Pause when the wallet cannot fund the next
    # chunk and work remains; resume when it can again. Pause takes precedence
    # over the funded/paused -> running flip: a pass that launched some chunks
    # but then ran the wallet dry still ends paused, waiting for a top-up. Only
    # the CAS winner emails, so the pause email is at most once even under
    # concurrent inline-hook + cron drives.
    if hit_insufficient and undispatched_remain:
        paused = _cas_transition(
            campaign_id, "paused_insufficient_funds", ("funded", "running"),
            {"paused_at": _now_iso()},
        )
        if paused:
            _notify_campaign_paused(campaign)
    elif launched_any and entry_status in ("funded", "paused_insufficient_funds"):
        # Resume clears the pause bookkeeping so a later pause re-notifies and the
        # 14-day TTL clock restarts. (paused_at was already NULL from a fresh
        # funded start; writing NULL again is harmless.)
        extra: dict = {"paused_at": None, "pause_notified_at": None}
        if entry_status == "funded":
            extra["started_at"] = _now_iso()
        _cas_transition(
            campaign_id, "running", ("funded", "paused_insufficient_funds"), extra
        )

    _update_campaign(campaign_id, {"last_tick_at": _now_iso()})
    _maybe_finalize(campaign)
    return launched_count


def drive_campaign_async(campaign_id: str) -> None:
    """Kick the first-wave dispatch off the request path.

    POST /runs returns immediately; this daemon thread runs the initial drive so
    the first wave fans out without blocking the response (at the raised
    concurrency an inline drive would make many Modal + Supabase round-trips
    before returning). The cron tick is the reliable backstop if the thread dies
    or the worker recycles before it finishes.
    """
    import threading  # noqa: PLC0415

    def _run() -> None:
        try:
            drive_campaign(campaign_id)
        except Exception:
            logger.warning(
                "drive_campaign_async: drive failed for %s", campaign_id,
                exc_info=True,
            )

    threading.Thread(
        target=_run, name=f"campaign-drive-{str(campaign_id)[:8]}", daemon=True
    ).start()


def _maybe_finalize(campaign: "ComputeCampaign") -> None:
    """Set the terminal campaign status once every chunk is dispatched + done.

    Count-based (no all-rows load): finalize only when every chunk has a row
    (``dispatched == total``) AND none is still in flight (``in_flight == 0``,
    i.e. all rows are terminal). The counts are authoritative reads, so this
    never finalizes while a chunk is still running or undispatched.
    """
    total = campaign.total_subjobs
    # Fail-safe defaults: any count read failure must PREVENT finalizing, never
    # cause it. dispatched<total (default 0) returns; in_flight>0 (default 1)
    # returns; a failed succeeded read (default -1) returns. So a transient
    # count error can never terminalize a campaign with children still running.
    if _count_children(campaign.id, default=0) < total:
        return
    if _count_children(campaign.id, ("pending", "running"), default=1) > 0:
        return
    succeeded = _count_children(campaign.id, ("succeeded",), default=-1)
    if succeeded < 0:
        return
    if succeeded >= total:
        final = "completed"
    elif succeeded > 0:
        final = "completed_with_failures"
    else:
        final = "failed"
    # CAS from a non-terminal state only, so a concurrent cancel (or another
    # driver's finalize) is never overwritten: finalizing a campaign a user just
    # cancelled would resurrect it out of "cancelled".
    _cas_transition(
        campaign.id,
        final,
        ("funded", "running", "completing", "paused_insufficient_funds"),
        {"completed_at": _now_iso()},
    )


def _notify_campaign_paused(campaign: "ComputeCampaign") -> None:
    """Pause email with DURABLE delivery. Never raises into the driver.

    Sends the email and, ONLY on a confirmed send (the sender returns True),
    stamps ``pause_notified_at``. If the send is dropped (transient Resend
    failure, or no address yet), the flag stays NULL and the cron's paused sweep
    (:func:`sweep_paused_campaigns`) re-sends it — so a pause notification is not
    lost (step 4b upgrades the old at-most-once behaviour). Called on the winning
    CAS pause transition; a resume clears the flag so a later re-pause notifies
    again. Proactive auto-reload-on-pause is already covered upstream: the wallet
    settle hook calls ``auto_reload_if_needed`` when the funding chunk settled the
    balance low, so no separate trigger is needed here.
    """
    sent = False
    try:
        from shared.email import send_campaign_paused_email  # noqa: PLC0415
        sent = bool(send_campaign_paused_email(
            user_id=campaign.user_id,
            campaign_id=campaign.id,
            campaign_name=campaign.name or "",
        ))
    except Exception:
        logger.warning(
            "campaign %s: pause email raised (cron will retry)", campaign.id,
            exc_info=True,
        )
    if sent:
        _update_campaign(campaign.id, {"pause_notified_at": _now_iso()})


_PAUSE_TTL_DAYS = 14


def _ttl_finalize_paused(campaign_id: str) -> bool:
    """CAS-finalize a campaign starved past the pause TTL.

    Produced designs stay on their sub-job pages (downloadable); undispatched
    chunks never placed a hold, so there is nothing to release. Finalizes to
    ``completed_with_failures`` when any chunk delivered, else ``cancelled``.
    Fail-safe: a still-in-flight chunk (should not exist after the TTL) or a
    failed count read leaves the campaign paused for the next sweep.
    """
    if _count_children(campaign_id, ("pending", "running"), default=1) > 0:
        return False
    succeeded = _count_children(campaign_id, ("succeeded",), default=-1)
    if succeeded < 0:
        return False
    final = "completed_with_failures" if succeeded > 0 else "cancelled"
    return _cas_transition(
        campaign_id, final, ("paused_insufficient_funds",),
        {"completed_at": _now_iso()},
    )


def sweep_paused_campaigns(*, now=None, ttl_days: int = _PAUSE_TTL_DAYS) -> dict:
    """Cron housekeeping over paused campaigns: TTL auto-finalize + email retry.

    * ``paused_at`` older than ``ttl_days`` -> finalize as partial
      (:func:`_ttl_finalize_paused`), so a never-topped-up campaign does not
      linger forever.
    * ``pause_notified_at`` still NULL -> re-send the pause email (durable
      delivery). Bounded: once the TTL finalizes the campaign it drops out of the
      paused set, so a user with no address is retried only until the TTL.

    Idempotent + CAS-guarded; safe alongside the drive tick and overlapping cron
    ticks. Returns ``{"finalized": n, "renotified": m}``.
    """
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    summary = {"finalized": 0, "renotified": 0}
    client = get_service_client()
    if client is None:
        return summary
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=ttl_days)).isoformat()

    try:
        stale = (
            client.table(_TABLE).select("id")
            .eq("status", "paused_insufficient_funds")
            .lt("paused_at", cutoff)
            .execute().data or []
        )
    except Exception:
        logger.warning("sweep_paused_campaigns: TTL query failed", exc_info=True)
        stale = []
    for row in stale:
        cid = row.get("id")
        if cid and _ttl_finalize_paused(str(cid)):
            summary["finalized"] += 1

    try:
        unnotified = (
            client.table(_TABLE).select("*")
            .eq("status", "paused_insufficient_funds")
            .is_("pause_notified_at", "null")
            .execute().data or []
        )
    except Exception:
        logger.warning(
            "sweep_paused_campaigns: renotify query failed", exc_info=True
        )
        unnotified = []
    for row in unnotified:
        _notify_campaign_paused(ComputeCampaign.from_row(row))
        summary["renotified"] += 1

    return summary


def maybe_drive_campaign_for_job(job) -> None:  # noqa: ANN001
    """Inline-hook entry point called from a child's terminal write.

    Best-effort: never raises into the terminal-write path. Re-drives the
    owning campaign so a finishing child immediately frees its slot for the
    next chunk.
    """
    campaign_id = getattr(job, "campaign_id", None)
    if not campaign_id:
        return
    try:
        drive_campaign(campaign_id)
    except Exception:
        logger.warning(
            "maybe_drive_campaign_for_job: drive failed for campaign %s",
            campaign_id, exc_info=True,
        )


def cancel_campaign(campaign_id: str, user_id: str) -> bool:
    """Cancel a campaign: stop dispatch, cancel in-flight children, refund.

    Each in-flight child is cancelled through the owner-scoped cancel_job
    CAS path, which bills consumed GPU and refunds the surplus. Undispatched
    chunks simply never run. Returns True if the campaign was cancellable.
    """
    campaign = get_campaign(campaign_id, user_id=user_id)
    if campaign is None:
        return False
    if campaign.status in ("completed", "completed_with_failures", "failed", "cancelled"):
        return False

    _update_campaign(campaign_id, {"status": "cancelled", "completed_at": _now_iso()})

    from shared.jobs import cancel_job  # noqa: PLC0415
    from gpu.modal_client import ModalClient  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return True
    try:
        resp = (
            client.table("tool_jobs")
            .select("id,status")
            .eq("campaign_id", campaign_id)
            .in_("status", ["pending", "running"])
            .execute()
        )
        rows = list(getattr(resp, "data", None) or [])
    except Exception:
        logger.warning("cancel_campaign: child query failed for %s", campaign_id, exc_info=True)
        rows = []
    mc = ModalClient()
    for r in rows:
        try:
            cancel_job(str(r["id"]), user_id=user_id, modal_client=mc)
        except Exception:
            logger.warning(
                "cancel_campaign: cancel_job raised for child %s", r.get("id"),
                exc_info=True,
            )
    return True
