"""The "Run more candidates" offer on a finished job."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

SUGGESTED_COUNT = 100

# tool -> (the input key that carries the candidate count, the most one run takes)
SCALE_UP: dict[str, tuple[str, int]] = {
    "bindcraft": ("num_designs", 100),
    "pxdesign": ("num_designs", 100),
    "rfantibody": ("num_designs", 100),
    "rfdiffusion": ("num_designs", 100),
    "proteina": ("num_designs", 100),
    "iggm": ("num_samples", 100),
    "mpnn": ("num_seq_per_target", 100),
    # tools/boltzgen/__init__.py refuses a budget above 50.
    "boltzgen": ("budget", 50),
    # tools/esmfold2_design/__init__.py N_SEEDS_MAX.
    "esmfold2-design": ("n_seeds", 64),
}


@dataclass(frozen=True)
class ScaleUp:
    tool: str
    count: int
    clamped: bool
    route: str  # "split" (via /campaigns/new) or "single" (clone the form)
    price_usd: Decimal
    price_display: str
    topup_usd: int  # 0 when the balance covers the price


def topup_usd(price, balance) -> int:  # noqa: ANN001
    """Whole dollars the balance is short of ``price``, rounded up; 0 if none."""
    short = Decimal(str(price)) - Decimal(str(balance))
    return max(0, math.ceil(short))


def _current_count(inputs: dict, key: str) -> int:
    try:
        return int(inputs.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def quote(user_id: str, job) -> Optional[ScaleUp]:  # noqa: ANN001
    """The offer for ``job``, or None when there is nothing to offer."""
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    from shared.jobs import candidate_records  # noqa: PLC0415
    from shared.wallet import get_or_create_wallet  # noqa: PLC0415
    from shared.wallet_estimates import estimated_cost_for_tool  # noqa: PLC0415

    entry = SCALE_UP.get(job.tool)
    if entry is None or job.status != "succeeded" or job.preset == "validate":
        return None
    if not candidate_records(job.result):
        return None
    if not tool_enabled(job.tool):
        return None
    key, max_count = entry
    inputs = {k: v for k, v in (job.inputs or {}).items() if not k.startswith("_")}
    count = min(SUGGESTED_COUNT, max_count)
    if _current_count(inputs, key) >= count:
        return None
    preset = job.preset or "pilot"

    if job.tool in cc.SUPPORTED_TOOLS and count > cc.single_container_ceiling(job.tool, preset):
        from blueprints.campaigns import campaign_preset_refusal  # noqa: PLC0415
        if cc.campaign_tool_gated_off(job.tool) or campaign_preset_refusal(job.tool, preset):
            return None
        try:
            plan = cc.plan_chunks(job.tool, count, preset)
        except ValueError:
            return None
        price = plan.budget_usd
        # The start gate (cc.campaign_preauth) needs the first batch, which can
        # exceed the whole estimate when a run has fewer pieces than slots.
        needed = max(price, cc.first_wave_hold_usd(plan, cc.launch_concurrency_for(job.tool)))
        route = "split"
    else:
        price = estimated_cost_for_tool(
            user_id, job.tool, {**inputs, key: count, "preset": preset}
        )
        needed = price
        route = "single"

    wallet = get_or_create_wallet(user_id) or {}
    balance = Decimal(str(wallet.get("balance_usd") or 0))
    return ScaleUp(
        tool=job.tool,
        count=count,
        clamped=max_count < SUGGESTED_COUNT,
        route=route,
        price_usd=price,
        price_display=cc.display_cost_usd(price),
        topup_usd=topup_usd(needed, balance),
    )
