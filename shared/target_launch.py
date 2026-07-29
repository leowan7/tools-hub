"""Launch N tools against one target in a single gated action.

Phase 2 of the target-first rework. The compute engine already fans ONE tool
out over one target; this module is the composition layer that lets a user
pick several tools, see one itemised estimate, and pass one start gate.

Everything here is PURE: no Supabase, no wallet writes, no Modal. The one
function that touches money (:func:`preauth_multi_launch`) delegates to the
unmodified ``campaign_preauth``, which itself only reads. That matters because
this is the layer deciding how much of a user's balance a single click can
commit, and it should be testable without a database.

Two things it exists to get right, both of which are wrong under the obvious
implementation:

**Preauth must be summed, not looped.** ``campaign_preauth`` is a pure gate
with no debit and no hold. Calling it once per tool reads the SAME balance
every time, so N launches all pass a gate that only one of them can afford,
and the driver then parks N-1 in ``paused_insufficient_funds``. That is
correct fund-and-drain behaviour and a terrible launch experience: the user
clicked "run 7 tools", got 7 rows, and 6 of them silently did nothing. One
call with the summed budget and summed first wave asks the question the user
actually asked. Summing is sound precisely BECAUSE preauth never debits, and
it gets the daily velocity cap right too, since ``_campaign_spend_today``
already sums budgets.

**Concurrency must be divided, not repeated.** ``GLOBAL_USER_INFLIGHT_CAP``
already bounds in-flight sub-jobs across ALL of a user's campaigns, so gating
N campaigns at 16 each gates against work that physically cannot start. The
division composes with zero new arithmetic because ``first_wave_hold_usd``
already takes concurrency as a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from shared.compute_campaigns import (
    GLOBAL_USER_INFLIGHT_CAP,
    ChunkPlan,
    PreauthResult,
    campaign_preauth,
    first_wave_hold_usd,
    launch_concurrency_for,
    plan_chunks,
)

# Pace settings. Both drain to the SAME total spend -- delivered-only billing
# refunds whatever is not consumed -- so this changes only the start gate and
# the ramp, never the bill. It is the escape valve for the one complaint a
# 7-tool launch is guaranteed to produce: the first-wave gate is large, and
# the honest answer is "hold less at once by starting narrower".
PACE_BURST = "burst"
PACE_STEADY = "steady"
_STEADY_DIVISOR = 4

# Steady spreads the same cap over 4x the campaigns' worth of slots.
_PACE_DIVISORS: Mapping[str, int] = {PACE_BURST: 1, PACE_STEADY: _STEADY_DIVISOR}


@dataclass(frozen=True)
class ToolLaunchSpec:
    """One tool's slice of a multi-tool launch.

    ``params`` is the tool's OWN validated parameter dict, already through
    ``adapter.validate()`` and already merged with the shared target block by
    the caller. This module never validates and never guesses defaults; it
    plans and prices what it is handed.
    """

    tool: str
    preset: str
    requested_designs: int
    params: Mapping[str, object]


@dataclass(frozen=True)
class MultiLaunchPlan:
    """The priced, concurrency-divided plan for one multi-tool launch.

    ``plans`` and ``concurrency`` are index-aligned with ``specs``, NOT keyed
    by tool. The same tool twice against one target is a legitimate launch
    (adding 400 more BindCraft designs to a target that already has some), and
    a tool-keyed mapping would silently collapse the two.
    """

    specs: tuple[ToolLaunchSpec, ...]
    plans: tuple[ChunkPlan, ...]
    concurrency: tuple[int, ...]
    pace: str
    budget_usd: Decimal
    first_wave_usd: Decimal

    @property
    def total_subjobs(self) -> int:
        return sum(int(p.total_subjobs) for p in self.plans)

    @property
    def total_designs(self) -> int:
        return sum(int(p.requested_designs) for p in self.plans)

    def rows(self) -> list[dict]:
        """Per-tool breakdown for the estimate endpoint and the launch form.

        Itemised deliberately. A single summed figure for a seven-tool launch
        is the number most likely to be misread as the bill, and the user
        cannot decide what to drop without seeing which tool costs what.
        """
        out = []
        for spec, plan, conc in zip(self.specs, self.plans, self.concurrency):
            out.append({
                "tool": spec.tool,
                "preset": spec.preset,
                "requested_designs": int(plan.requested_designs),
                "chunk_size": int(plan.chunk_size),
                "total_subjobs": int(plan.total_subjobs),
                "concurrency": int(conc),
                "budget_usd": float(plan.budget_usd),
                "first_wave_usd": float(first_wave_hold_usd(plan, conc)),
            })
        return out


def divide_concurrency(
    tools: Sequence[str], pace: str = PACE_BURST
) -> tuple[int, ...]:
    """Split the global in-flight cap across the launch, aligned to ``tools``.

    Returns one value per ENTRY, so a repeated tool gets its own slot rather
    than sharing one. Each value is bounded three ways, and every bound is
    load-bearing:

    * **Never above the tool's own launch concurrency.** ``proteina`` is
      deliberately throttled to 4 because one shard is a full A100. A naive
      ``CAP // n`` hands it 16 at n=2, which quadruples both the throttle and
      its first-wave hold. The division may only ever narrow a tool, never
      widen it.
    * **Never below 1.** ``create_campaign`` writes
      ``max(1, int(concurrency_target)) if concurrency_target else
      launch_concurrency_for(tool)``, and 0 is falsy, so a zero here would not
      raise or clamp -- it would SILENTLY restore the tool default, undoing
      the division precisely when the launch is widest and most needs it.
    * **Never above what one campaign would get alone.** At n=1 this returns
      exactly ``launch_concurrency_for(tool)``, so a single-tool launch
      through this path is bit-identical to today's create route.

    ``pace`` trades start-gate size against ramp speed. Both settings reach
    the same total spend; ``concurrency_target`` is re-read by the driver
    every tick, so speeding a launch up afterwards is a one-column UPDATE.
    """
    entries = list(tools)
    if not entries:
        return ()
    divisor = _PACE_DIVISORS.get(pace, 1)
    share = GLOBAL_USER_INFLIGHT_CAP // (len(entries) * divisor)
    return tuple(
        max(1, min(launch_concurrency_for(t), share)) for t in entries
    )


def plan_multi_launch(
    specs: Sequence[ToolLaunchSpec], pace: str = PACE_BURST
) -> MultiLaunchPlan:
    """Price and size a multi-tool launch. Pure; raises ValueError.

    Chunking stays with ``plan_chunks``, which owns the per-tool sizing and
    the per-campaign sub-job ceiling, and whose ValueError messages are
    already user-facing. This adds only the composition: divided concurrency,
    the summed budget, and the summed first wave.

    The first wave is summed PER SPEC at that spec's own divided concurrency,
    not computed once from a total. Tools have different per-chunk holds and
    different concurrency after the division, so a single blended figure would
    be wrong in both directions at once.
    """
    specs = tuple(specs)
    if not specs:
        raise ValueError("Pick at least one tool to run against this target.")

    concurrency = divide_concurrency([s.tool for s in specs], pace)
    plans = tuple(
        plan_chunks(s.tool, s.requested_designs, s.preset) for s in specs
    )

    budget = sum((p.budget_usd for p in plans), Decimal("0"))
    first_wave = sum(
        (first_wave_hold_usd(p, c) for p, c in zip(plans, concurrency)),
        Decimal("0"),
    )
    return MultiLaunchPlan(
        specs=specs,
        plans=plans,
        concurrency=concurrency,
        pace=pace if pace in _PACE_DIVISORS else PACE_BURST,
        budget_usd=budget,
        first_wave_usd=first_wave,
    )


def preauth_multi_launch(
    user_id: str, plan: MultiLaunchPlan
) -> PreauthResult:
    """One start gate for the whole launch. Checks, never debits.

    Deliberately ONE call into the unmodified ``campaign_preauth`` rather than
    one per tool. See the module docstring: preauth reads the same balance
    every time it is called, so a loop passes N times on a balance that funds
    one, and the driver quietly parks the rest.

    The result's ``gate_usd`` is the summed FIRST WAVE, not the summed budget.
    Under fund-and-drain the wallet is the ceiling and the rest is funded as
    the campaigns drain, so gating on the full budget would refuse launches
    that would have completed. Copy that distinction into any user-facing
    message: this amount is HELD, not spent.
    """
    return campaign_preauth(user_id, plan.budget_usd, plan.first_wave_usd)


def concurrency_note(plan: MultiLaunchPlan) -> Optional[str]:
    """One sentence explaining a divided concurrency, or None if nothing moved.

    Returns None when every tool got what it would have got alone, so a
    single-tool launch says nothing new. Otherwise it names the cap, because
    "why is my 7-tool run slower per tool than my 1-tool run" is the obvious
    question and the answer is a fixed platform limit, not a fault.
    """
    solo = [launch_concurrency_for(s.tool) for s in plan.specs]
    if all(c == s for c, s in zip(plan.concurrency, solo)):
        return None
    return (
        f"Running {len(plan.specs)} tools at once shares one limit of "
        f"{GLOBAL_USER_INFLIGHT_CAP} sub-jobs in flight, so each starts "
        f"narrower than it would alone. Total designs and total cost are "
        f"unchanged; only the ramp is."
    )
