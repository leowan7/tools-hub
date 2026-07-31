"""Launch N tools against one target in a single gated action.

Phase 2 of the target-first rework. The compute engine already fans ONE tool
out over one target; this module is the composition layer that lets a user
pick several tools, see one itemised estimate, and pass one start gate.

**Nothing here is database-free.** Two separate paths reach Supabase, and both
were previously documented as pure, so measure before believing any claim
about cost in this module:

* :func:`plan_multi_launch` and :meth:`MultiLaunchPlan.rows` price through
  ``plan_chunks`` -> ``_estimate_chunk_cost`` -> ``estimated_cost_for_tool`` ->
  ``_historical_p90_seconds``, which SELECTs ``tool_jobs_p90``. Measured: two
  reads per SPEC for ``plan_multi_launch`` and one per spec for ``rows()`` (per
  spec, not per tool -- the same tool twice is two entries and costs twice).
  It short-circuits to ``None`` when no service client is configured, which is
  exactly what the test fixtures do, so the suite never exercises the branch
  production takes.
* :func:`preauth_multi_launch` delegates to ``campaign_preauth``, which calls
  ``get_or_create_wallet`` (it INSERTS a wallet row on a user's first use) and
  ``_campaign_spend_today`` (SELECT). What it does NOT do is debit or hold, and
  that distinction, not purity, is why summing is sound (see below).

Database-free: :func:`divide_concurrency`, :func:`concurrency_note`, the
``ToolLaunchSpec`` and ``MultiLaunchPlan`` constructors, and the
``total_subjobs`` / ``total_designs`` properties. Everything that touches a
dollar figure reads. Callers on a debounced keystroke path should reuse an
existing ``MultiLaunchPlan.plans`` rather than re-planning to answer a what-if
(see :func:`first_wave_at_pace`).

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

        Money is ``str(Decimal)``, matching the existing single-tool estimate
        endpoint (``api_runs_estimate``) so the two APIs cannot disagree about
        how a dollar figure is encoded. Not float: these are 4dp-quantized
        Decimals and ``float`` does not preserve the quantum, so
        ``Decimal("4.0200")`` ships as ``4.02``. Every figure here is money the
        user is about to authorise, and a value that changes shape depending on
        which digits happen to be zero is the wrong thing to put in front of
        them. Counts stay ints.

        The itemisation is exact by construction, not by coincidence:
        ``budget_usd`` and ``first_wave_usd`` on the plan are the per-spec sums
        of these same two values, so the rows always add up to the totals.
        """
        from shared.compute_campaigns import display_cost_usd  # noqa: PLC0415

        out = []
        for spec, plan, conc in zip(self.specs, self.plans, self.concurrency):
            first_wave = first_wave_hold_usd(plan, conc)
            out.append({
                "tool": spec.tool,
                "preset": spec.preset,
                "requested_designs": int(plan.requested_designs),
                "chunk_size": int(plan.chunk_size),
                "total_subjobs": int(plan.total_subjobs),
                "concurrency": int(conc),
                "budget_usd": str(plan.budget_usd),
                "first_wave_usd": str(first_wave),
                # The exact 4dp value stays above because the anti-drift tests
                # price it against the planner. These are what the page prints:
                # the page must not do the 2dp conversion itself, because
                # rounding to NEAREST there understated the hold the consent
                # checkbox refers to.
                #
                # A 2dp row does not sum to a 2dp conversion of the exact total,
                # so the endpoint totals THESE strings rather than re-rounding
                # the plan's own totals. See compute_campaigns.display_total_usd
                # for why the panel has to agree with itself.
                "budget_usd_display": display_cost_usd(plan.budget_usd),
                "first_wave_usd_display": display_cost_usd(first_wave),
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
    * **Never above what one campaign would get alone.** At n=1 AND
      ``PACE_BURST`` this returns exactly ``launch_concurrency_for(tool)``, so
      a single-tool launch through this path is bit-identical to today's
      create route. That equality is specific to burst: ``PACE_STEADY``
      divides by 4 unconditionally, so one rfdiffusion at steady is 8, not 16.
      That is steady behaving as asked -- it is a deliberate "start narrower"
      control, not a width-dependent one -- and it is why the launch route
      defaults a single-tool launch to burst rather than papering over it here.

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
    """Price and size a multi-tool launch. Raises ValueError.

    NOT free: ``plan_chunks`` prices through the historical-p90 lookup, so this
    costs roughly two Supabase reads per spec. See the module docstring.

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


def first_wave_at_pace(plan: MultiLaunchPlan, pace: str) -> Decimal:
    """What the start gate would be at a different pace, reusing this plan.

    Answers "how much smaller would starting narrow be" without re-planning.
    Only the concurrency division differs between paces: the chunk sizing, the
    sub-job count and the budget are all pace-independent, so ``plan.plans`` is
    already the right answer and re-deriving it is waste.

    Not free, but roughly half of a re-plan: this costs one ``child_hold_usd``
    lookup per spec (which reaches the historical-p90 read), where
    ``plan_multi_launch`` costs that plus a ``plan_chunks`` estimate per spec.
    That matters because the caller is a debounced keystroke handler.
    """
    concurrency = divide_concurrency([s.tool for s in plan.specs], pace)
    return sum(
        (first_wave_hold_usd(p, c) for p, c in zip(plan.plans, concurrency)),
        Decimal("0"),
    )


def first_wave_display_at_pace(plan: MultiLaunchPlan, pace: str) -> str:
    """The 2dp hold the PAGE shows for this plan at ``pace``.

    Totals the per-row 2dp displays, which is what the panel prints, rather than
    rounding the exact total, which is a slightly smaller number. Any figure
    quoted beside that panel has to be the panel's figure: a refusal sentence
    naming $9.18 under a panel reading $9.19 sends the user to top up to an
    amount that is refused again.

    At ``pace == plan.pace`` this reproduces ``display_total_usd`` over
    ``plan.rows()`` exactly, because the rows use the same concurrency division.
    That equality is pinned by a test rather than left to inspection. Use this
    where the rows are not already in hand, and sum the rows where they are.
    """
    from shared.compute_campaigns import (  # noqa: PLC0415
        display_cost_usd, display_total_usd,
    )

    concurrency = divide_concurrency([s.tool for s in plan.specs], pace)
    return display_total_usd(
        display_cost_usd(first_wave_hold_usd(p, c))
        for p, c in zip(plan.plans, concurrency)
    )


def preauth_multi_launch(
    user_id: str, plan: MultiLaunchPlan
) -> PreauthResult:
    """One start gate for the whole launch. Checks, never debits.

    Deliberately ONE call into the unmodified ``campaign_preauth`` rather than
    one per tool. See the module docstring: preauth reads the same balance
    every time it is called, so a loop passes N times on a balance that funds
    one, and the driver quietly parks the rest.

    The amount gated on is the summed FIRST WAVE, not the summed budget. It
    comes back as ``PreauthResult.required_usd`` -- note that
    ``campaign_preauth`` calls its own local for the same number ``gate_usd``,
    which is not a field name and will not resolve on the result. Under
    fund-and-drain the wallet is the ceiling and the rest is funded as the
    campaigns drain, so gating on the full budget would refuse launches that
    would have completed. Copy that distinction into any user-facing message:
    this amount is HELD, not spent.
    """
    return campaign_preauth(user_id, plan.budget_usd, plan.first_wave_usd)


def concurrency_note(plan: MultiLaunchPlan) -> Optional[str]:
    """One sentence explaining a divided concurrency, or None if nothing moved.

    Two DIFFERENT things narrow a launch and they need different sentences.
    Sharing the global cap is a fixed platform limit the user cannot change.
    Choosing ``PACE_STEADY`` is their own setting, which they can undo. Telling
    someone a platform limit caused what their radio button caused sends them
    to support instead of to the control.

    Each cause is therefore detected on its own terms, NOT inferred from the
    launch width:

    * cap narrowing = the BURST division is already below solo. Burst is the
      widest this module ever returns, so anything it takes off is the cap.
    * pace narrowing = the chosen pace is below the burst division.

    Branching on ``len(specs) > 1`` looks equivalent and is not. Two tools at
    burst get ``32 // 2 = 16`` each, which is exactly their solo width, so at
    n=2 the cap takes nothing and every bit of narrowing comes from the pace --
    yet a width-based branch would blame the cap. That is the same
    mis-attribution as the original "Running 1 tools at once shares one limit"
    bug, moved from n=1 to n>=2 rather than fixed.
    """
    tools = [s.tool for s in plan.specs]
    solo = [launch_concurrency_for(t) for t in tools]
    burst = list(divide_concurrency(tools, PACE_BURST))
    cap_narrowed = any(b < s for b, s in zip(burst, solo))
    pace_narrowed = any(c < b for c, b in zip(plan.concurrency, burst))
    if not cap_narrowed and not pace_narrowed:
        return None

    parts = []
    if cap_narrowed:
        # "each tool gets a share of it", not "each starts narrower than it
        # would alone": proteina is pinned to 4 and is NOT narrowed by a
        # 7-tool division, so the stronger claim is false whenever it is in
        # the launch.
        parts.append(
            f"Running {len(tools)} tools at once shares one limit of "
            f"{GLOBAL_USER_INFLIGHT_CAP} sub-jobs in flight, so each tool "
            f"gets a share of it."
        )
    if pace_narrowed:
        parts.append(
            "Starting narrow holds less up front, so this ramps up more "
            "slowly than it would at full speed."
        )
    parts.append("Total designs and total cost are unchanged; only the ramp is.")
    return " ".join(parts)
