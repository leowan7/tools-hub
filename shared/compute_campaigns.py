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
from datetime import datetime, timezone
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
# invariants before the driver is hardened for scale. The cap is 20 (not
# 50) for a concrete Phase-1 reason: child holds go through the UNCHANGED
# reserve_hold, which enforces the $200/day single-job cap (SQL, 0020). At
# 20 sub-jobs even the priciest Phase-1 tool (boltzgen ~$8.74/chunk ->
# ~$175) stays under $200, so a solo campaign never stalls on the daily
# cap. Phase 2 adds a daily-cap-exempt child-hold RPC + a stall reaper and
# raises this for true ~150-500 sub-job runs.
MAX_SUBJOBS_PER_CAMPAIGN = 20

# Driver defaults persisted on the campaign row. Phase 1 keeps the
# concurrency target modest so the first wave (dispatched synchronously
# from POST /runs) stays well inside the request budget; Phase 2 raises it
# alongside async dispatch + fairness controls. (The 0034 column default
# is 20 but create_campaign always writes this value explicitly.)
DEFAULT_CONCURRENCY_TARGET = 8
DEFAULT_MAX_ATTEMPTS = 2

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


# ---------------------------------------------------------------------------
# Billing: prepaid pre-authorization + per-child estimate + admission
# ---------------------------------------------------------------------------
#
# Billing model (see docs/COMPUTE-CAMPAIGNS-PLAN.md): NO escrow debit. The
# pre-auth is a pure gate (balance + frozen + velocity + verification); it
# does NOT move money. Real money moves as ordinary per-child wallet holds
# placed by the driver via the UNCHANGED reserve_hold path and settled by
# the UNCHANGED settle path, so balance == SUM(ledger) holds automatically
# and delivered-only billing falls out for free. Phase 1 ships small
# campaigns whose children stay well under the $200 single-job daily cap,
# so the unchanged reserve_hold is safe; Phase 2 adds a daily-cap-exempt
# child-hold RPC before campaigns scale past that cap.


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


def campaign_preauth(user_id: str, budget_usd: Decimal) -> PreauthResult:
    """Prepaid gate for a campaign. Checks but never debits.

    A campaign will not run unless the wallet is unfrozen and holds at
    least the full authorized ``budget_usd`` (prepaid), the per-user daily
    campaign velocity cap is not exceeded, and — above the verification
    threshold — the account is approved (``per_job_cap_override_usd`` set
    high enough). The real money moves later, per child, via reserve_hold.
    """
    budget_usd = Decimal(str(budget_usd))
    from shared.wallet import get_or_create_wallet  # noqa: PLC0415

    wallet = get_or_create_wallet(user_id)
    if not wallet:
        return PreauthResult(False, PREAUTH_NO_WALLET, Decimal("0"), budget_usd)
    balance = Decimal(str(wallet.get("balance_usd") or 0))
    if wallet.get("wallet_frozen"):
        return PreauthResult(False, PREAUTH_FROZEN, balance, budget_usd)
    if balance < budget_usd:
        return PreauthResult(False, PREAUTH_INSUFFICIENT, balance, budget_usd)
    if budget_usd > VERIFICATION_THRESHOLD_USD:
        override = wallet.get("per_job_cap_override_usd")
        approved = override is not None and Decimal(str(override)) >= budget_usd
        if not approved:
            return PreauthResult(False, PREAUTH_VERIFICATION, balance, budget_usd)
    spent_today = _campaign_spend_today(user_id)
    if spent_today + budget_usd > DAILY_CAMPAIGN_CAP_USD:
        return PreauthResult(False, PREAUTH_VELOCITY, balance, budget_usd)
    return PreauthResult(True, PREAUTH_OK, balance, budget_usd)


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
    """USD estimate for one sub-job at ``design_count`` designs.

    Used by the driver to size each per-child wallet hold. boltzgen is
    flat per job (fixed 200-pool); the linear tools scale by the count.
    """
    return _estimate_chunk_cost(tool, int(design_count))


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

_TERMINAL_CHILD = ("succeeded", "failed", "timeout", "cancelled")


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


def _campaign_children(campaign_id: str) -> list[dict]:
    """Return ``[{chunk_index, status}, ...]`` for a campaign's sub-jobs."""
    client = get_service_client()
    if client is None:
        return []
    try:
        resp = (
            client.table("tool_jobs")
            .select("chunk_index,status")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        return list(getattr(resp, "data", None) or [])
    except Exception:
        logger.warning("_campaign_children failed for %s", campaign_id, exc_info=True)
        return []


def _tally(children: list[dict]) -> dict:
    counts = {status: 0 for status in _CHILD_STATUSES}
    for c in children:
        s = c.get("status")
        if s in counts:
            counts[s] += 1
    return counts


def _update_campaign(campaign_id: str, fields: dict) -> None:
    client = get_service_client()
    if client is None:
        return
    try:
        client.table(_TABLE).update(fields).eq("id", campaign_id).execute()
    except Exception:
        logger.warning("_update_campaign failed for %s", campaign_id, exc_info=True)


def fund_campaign(campaign_id: str) -> None:
    """Mark a draft campaign funded (called by the route after preauth)."""
    _update_campaign(campaign_id, {"status": "funded", "confirmed_at": _now_iso()})


def _dispatch_chunk(campaign: "ComputeCampaign", chunk_index: int) -> str:
    """Reserve a per-child hold, create the sub-job, and spawn it on Modal.

    Returns one of:
      * ``"launched"`` — a child row was created and a Modal run started.
      * ``"failed"``   — a child row exists but reached a terminal failure
                          (modal submit failed); the chunk IS dispatched.
      * ``"skipped"``  — NO child row was created (hold refused, duplicate,
                          transient insert failure); the chunk should be
                          retried on a later drive pass.
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

    estimate = estimate_child_cost(campaign.tool, design_count)
    hold_tx_id = reserve_hold(campaign.user_id, campaign.tool, None, estimate, base_inputs)
    if not hold_tx_id:
        logger.info(
            "campaign %s chunk %s: hold not placed (balance/cap); will retry",
            campaign.id, chunk_index,
        )
        return "skipped"

    child_inputs = dict(base_inputs)
    child_inputs["_wallet"] = {
        "hold_tx_id": hold_tx_id,
        "estimate_usd": str(estimate),
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
        # Duplicate (campaign_id, chunk_index, attempt) from a racing driver
        # (UNIQUE violation) or a transient insert failure. Release the hold
        # so nothing is stranded; the winning row keeps its own hold. Report
        # "skipped": if it was a duplicate the winner's row shows up in the
        # next pass's existing_idx (so we won't re-try it); if it was
        # transient, retrying is correct.
        try:
            release_hold(hold_tx_id, reason="campaign_chunk_create_failed")
        except Exception:
            logger.warning("release_hold after create fail raised", exc_info=True)
        return "skipped"

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

    job_spec = adapter.build_payload(child_inputs, presigned_url)
    webhook_url = _webhook_url(child.id, child.job_token)
    upload_urls_endpoint = _upload_urls_endpoint(child.id, child.job_token)

    try:
        submit_result = ModalClient().submit(
            campaign.tool,
            campaign.preset,
            inputs={
                **job_spec,
                "_input_pdb_url": presigned_url,
                "_input_presigned_url": presigned_url,
                "_upload_urls_endpoint": upload_urls_endpoint,
            },
            job_id=child.id,
            job_token=child.job_token,
            webhook_url=webhook_url,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("campaign %s chunk %s: Modal submit failed", campaign.id, chunk_index)
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


def drive_campaign(campaign_id: str) -> None:
    """Reconcile a campaign and dispatch as many sub-jobs as admission allows.

    Safe to call repeatedly and concurrently (the DB uniqueness + CAS
    launch make double-dispatch impossible). Triggered at create, by the
    inline hook on child completion, and by the cron backstop.
    """
    campaign = get_campaign(campaign_id)
    if campaign is None:
        return
    if campaign.status in ("draft", "completed", "completed_with_failures",
                           "failed", "cancelled"):
        return

    children = _campaign_children(campaign_id)
    existing_idx = {
        c["chunk_index"] for c in children if c.get("chunk_index") is not None
    }
    counts = _tally(children)
    in_flight = counts["pending"] + counts["running"]

    if campaign.status == "funded":
        _update_campaign(campaign_id, {"status": "running", "started_at": _now_iso()})
        campaign.status = "running"

    # Admission loop: fill open slots with the lowest chunk that has no row
    # yet. ``attempted`` guards against re-trying the same chunk in this pass
    # (a "skipped" chunk stays out of existing_idx so a LATER drive retries
    # it, but must not spin here). A chunk only counts toward existing_idx /
    # in_flight once a row actually exists — a refused hold wastes no slot.
    attempted: set = set()
    while in_flight < campaign.concurrency_target:
        idx = None
        for i in range(campaign.total_subjobs):
            if i not in existing_idx and i not in attempted:
                idx = i
                break
        if idx is None:
            break
        attempted.add(idx)
        outcome = _dispatch_chunk(campaign, idx)
        if outcome == "launched":
            existing_idx.add(idx)
            in_flight += 1
        elif outcome == "failed":
            # A (failed) child row exists for this chunk; it is dispatched.
            existing_idx.add(idx)
        # "skipped": no row created; leave for a later drive pass.

    _update_campaign(campaign_id, {"last_tick_at": _now_iso()})
    _maybe_finalize(campaign)


def _maybe_finalize(campaign: "ComputeCampaign") -> None:
    """Set the terminal campaign status once every chunk is dispatched + done."""
    children = _campaign_children(campaign.id)
    if len(children) < campaign.total_subjobs:
        return
    counts = _tally(children)
    terminal = sum(counts[s] for s in _TERMINAL_CHILD)
    if terminal < campaign.total_subjobs:
        return
    succeeded = counts["succeeded"]
    if succeeded >= campaign.total_subjobs:
        final = "completed"
    elif succeeded > 0:
        final = "completed_with_failures"
    else:
        final = "failed"
    _update_campaign(campaign.id, {"status": final, "completed_at": _now_iso()})


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
