"""Self-serve compute campaigns ("Campaigns").

A campaign is a large design request (up to ~20k designs) that the system
splits into many ordinary ``tool_jobs`` sub-jobs, each sized to fit one GPU
container's timeout, fanned out on Modal's autoscaler with server-side
admission control. The sub-jobs reach terminal state ONLY through the
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
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Optional

from shared.credits import get_service_client
from shared.wallet_estimates import estimated_cost_for_tool, get_tool_spec
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

# Phase 1 ships SMALL campaigns to prove the state machine + money
# invariants before the driver is hardened for scale. Phase 2 raises this
# (and the driver's concurrency handling) for true ~150-500 sub-job runs.
MAX_SUBJOBS_PER_CAMPAIGN = 50

# Driver defaults persisted on the campaign row.
DEFAULT_CONCURRENCY_TARGET = 20
DEFAULT_MAX_ATTEMPTS = 2

# Head-room multiplier on the summed chunk estimate so the authorized
# budget comfortably covers historical drift. Delivered-only billing
# refunds whatever is not consumed, so a conservative budget never
# overcharges — it only gates admission.
BUDGET_BUFFER = Decimal("1.15")

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


def _chunk_size_for(tool: str) -> int:
    """Designs per sub-job for ``tool`` at the pilot container ceiling.

    boltzgen is special (budget-based, fixed pool). The linear tools derive
    the size from GPU-seconds-per-design vs the pilot preset's GPU-seconds
    cap, so a chunk comfortably fits one container.
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
    container_s = preset_gpu_seconds(tool, "pilot")
    if gpu_s_per_design <= 0 or container_s <= 0:
        return spec.designs_per_run_baseline
    size = int((container_s * _CONTAINER_UTILIZATION) / gpu_s_per_design)
    # Never chunk below the tool's own baseline (keeps per-chunk cost
    # efficient; the wallet estimate floors its multiplier at 1.0 anyway).
    return max(size, spec.designs_per_run_baseline)


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
    design_key = _DESIGN_PARAM_KEY.get(tool)
    out: dict = {}
    for key, value in dict(params or {}).items():
        if not isinstance(key, str):
            continue
        if key.startswith("_"):
            continue
        if key == design_key or key == "preset":
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
            "reserved_usd": float(self.reserved_usd),
            "spent_usd": float(self.spent_usd),
            "refunded_usd": float(self.refunded_usd),
            "remaining_usd": float(
                max(Decimal("0"), self.budget_usd - self.reserved_usd - self.spent_usd)
            ),
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

    Returns ``{"total": n, <status>: count, ...}`` with every bucket in
    :data:`_CHILD_STATUSES` present (0 when absent). Reads only the
    ``status`` column over the partial (campaign_id, status) index.
    """
    counts = {status: 0 for status in _CHILD_STATUSES}
    counts["total"] = 0
    client = get_service_client()
    if client is None:
        return counts
    try:
        response = (
            client.table("tool_jobs")
            .select("status")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
    except Exception:
        logger.warning(
            "get_progress_counts failed for %s", campaign_id, exc_info=True
        )
        return counts
    for r in rows:
        status = r.get("status")
        if status in counts:
            counts[status] += 1
        counts["total"] += 1
    return counts
