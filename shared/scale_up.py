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


# Fold predictors get an offer too, so a successful fold is not a dead end.
# The step is the one the results partials already link to unpriced: feed the
# predicted structure into ProteinMPNN (shared/resample.py). Count and defaults
# come from shared.resample.RESAMPLE_MPNN_DEFAULTS, which is what the form
# prefill actually applies, so the quote prices the run the button opens.
FOLD_TOOLS: frozenset[str] = frozenset({"af2", "colabfold", "esmfold"})


@dataclass(frozen=True)
class ScaleUp:
    tool: str
    count: int
    clamped: bool
    # "split"  — via /campaigns/new
    # "single" — clone this tool's form with the count raised
    # "resample" — open the MPNN form on this job's predicted structure
    # "clone"  — clone this tool's form, count unchanged
    route: str
    price_usd: Decimal
    price_display: str
    topup_usd: int  # 0 when the balance covers the price
    offer_text: str = ""  # sentence after "Want more?"; count-based when blank
    cta_text: str = ""  # button label; count-based when blank


def topup_usd(price, balance) -> int:  # noqa: ANN001
    """Whole dollars the balance is short of ``price``, rounded up; 0 if none."""
    short = Decimal(str(price)) - Decimal(str(balance))
    return max(0, math.ceil(short))


def _current_count(inputs: dict, key: str) -> int:
    try:
        return int(inputs.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _stored_count(inputs: dict) -> int:
    """How many designs the job actually ran, from its stored inputs.

    The batch adapters record the count one level down, under ``parameters``
    (tools/af2/__init__.py:351, tools/colabfold/__init__.py:337,
    tools/esmfold/__init__.py:231, tools/boltz2/__init__.py:418). None of them
    writes it at the top level, so a flat read returns 0 and prices a batch of
    any size as a single design. Pinned by
    tests/test_scale_up.py::test_clone_offer_prices_the_stored_batch_size.
    """
    nested = inputs.get("parameters")
    if isinstance(nested, dict):
        count = _current_count(nested, "n_designs_total")
        if count:
            return count
    records = inputs.get("batch_records") or inputs.get("binder_sequences")
    if isinstance(records, list) and records:
        return len(records)
    return _current_count(inputs, "n_designs_total")


def _offer(
    user_id: str, *, tool: str, count: int, route: str, params: dict,
    offer_text: str, cta_text: str,
) -> Optional[ScaleUp]:
    """Price ``params`` for ``tool`` and wrap it as an offer."""
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.wallet import get_or_create_wallet  # noqa: PLC0415
    from shared.wallet_estimates import (  # noqa: PLC0415
        cushioned_hold_usd,
        estimated_cost_for_tool,
    )

    price = estimated_cost_for_tool(user_id, tool, params)
    # The submit reserves the cushioned hold, not the estimate
    # (shared/wallet_guard.py), so the top-up figure has to clear that.
    needed = cushioned_hold_usd(user_id, tool, params)
    wallet = get_or_create_wallet(user_id) or {}
    balance = Decimal(str(wallet.get("balance_usd") or 0))
    return ScaleUp(
        tool=tool,
        count=count,
        clamped=False,
        route=route,
        price_usd=price,
        price_display=cc.display_cost_usd(price),
        topup_usd=topup_usd(needed, balance),
        offer_text=offer_text,
        cta_text=cta_text,
    )


def _next_step(user_id: str, job) -> Optional[ScaleUp]:  # noqa: ANN001
    """The offer for a tool with no candidate count to raise, or None.

    A fold that produced a structure points at MPNN sequence design on it;
    everything else here points at another run of the same shape. Both open a
    form the existing prefill already fills (``?resample_from=`` and
    ``?clone_from=`` in blueprints/tools.py::tool_form), so the gates here
    mirror that route's: a resample needs the predicted PDB the token resolver
    decodes, a clone needs nothing beyond the job.
    """
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    from shared.resample import (  # noqa: PLC0415
        RESAMPLE_DESTINATION,
        RESAMPLE_MPNN_DEFAULTS,
        can_resample,
    )

    if job.tool in FOLD_TOOLS and can_resample(job.tool):
        has_pdb = bool(((job.result or {}).get("pdb_b64") or "").strip())
        if has_pdb and tool_enabled(RESAMPLE_DESTINATION):
            count = int(RESAMPLE_MPNN_DEFAULTS["num_seq_per_target"])
            return _offer(
                user_id,
                tool=RESAMPLE_DESTINATION,
                count=count,
                route="resample",
                params=dict(RESAMPLE_MPNN_DEFAULTS),
                offer_text=(
                    f"Design {count} sequences on the structure you just "
                    f"predicted, with ProteinMPNN"
                ),
                cta_text=f"Design {count} sequences",
            )
    # Everything else here has no structure to hand on and no count field the
    # card can raise, so the offer is another run of the same shape. The
    # ``?clone_from=`` prefill copies the whole stored input
    # (blueprints/tools.py:1227), and for boltz2 that includes the pasted
    # ``binder_sequences`` list, so the quote prices the batch the form will
    # actually carry rather than a round number the user never submits.
    if (job.tool in FOLD_TOOLS or job.tool == "boltz2") and tool_enabled(job.tool):
        inputs = {k: v for k, v in (job.inputs or {}).items()
                  if not k.startswith("_")}
        count = max(1, _stored_count(inputs))
        screening = job.tool == "boltz2"
        if count == 1:
            offer_text = (
                "Screen another binder against the same target" if screening
                else "Fold another structure on the same settings"
            )
            cta_text = "Screen another" if screening else "Fold another"
        else:
            offer_text = (
                f"Screen another {count} binders against the same target"
                if screening
                else f"Fold another {count} on the same settings"
            )
            cta_text = f"Screen {count} more" if screening else f"Fold {count} more"
        return _offer(
            user_id,
            tool=job.tool,
            count=count,
            route="clone",
            params={
                **inputs,
                "n_designs_total": count,
                "preset": job.preset or "standalone",
            },
            offer_text=offer_text,
            cta_text=cta_text,
        )

    return None


def quote(user_id: str, job) -> Optional[ScaleUp]:  # noqa: ANN001
    """The offer for ``job``, or None when there is nothing to offer."""
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    from shared.jobs import candidate_records  # noqa: PLC0415
    from shared.wallet import get_or_create_wallet  # noqa: PLC0415
    from shared.wallet_estimates import (  # noqa: PLC0415
        cushioned_hold_usd,
        estimated_cost_for_tool,
    )

    if job.status != "succeeded" or job.preset == "validate":
        return None
    entry = SCALE_UP.get(job.tool)
    if entry is None:
        return _next_step(user_id, job)
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
        params = {**inputs, key: count, "preset": preset}
        price = estimated_cost_for_tool(user_id, job.tool, params)
        # The submit reserves this, not the estimate (shared/wallet_guard.py).
        needed = cushioned_hold_usd(user_id, job.tool, params)
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
