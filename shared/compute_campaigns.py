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
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Iterable, Mapping, Optional

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

# Tools available for self-serve campaigns. pxdesign chunks at its validated
# 24-design pilot job (see _CHUNK_SIZE_OVERRIDE); rfantibody uses a bigger
# campaign container + session budget (see _RFANTIBODY_CAMPAIGN_CONTAINER_S),
# mirroring how bindcraft was scaled.
SUPPORTED_TOOLS: tuple[str, ...] = (
    "rfdiffusion", "bindcraft", "boltzgen", "pxdesign", "rfantibody",
    # proteina: 1 shard = 1 fixed A100-80GB container running one seeded
    # `proteinfoundation.generate` job; num_designs scales the shard count.
    # Gated by FLAG_TOOL_PROTEINA, which is **ON in live production** — verified
    # 2026-08-04 by reading it, not by inference:
    #   railway variables --kv | grep FLAG_        (project tools-hub / production / web)
    # This comment previously read "(off)", which was wrong and load-bearing: it
    # made proteina look dormant while it was in fact user-reachable, and that is
    # what let the upload-then-refuse trap in
    # docs/audit-2026-07-22-campaign-rework-open-items.md:35 read as non-urgent.
    # Re-check the live value before reasoning about blast radius; do not trust
    # this line. Its 4 presets are the design variants, not the "pilot" tier the
    # others carry.
    "proteina",
    # iggm: 1 shard = 1 A100-40GB container running design.py --num_samples
    # <chunk> for one antibody-design variant, each shard entropy-seeded by
    # design.py itself (random.seed(time.time()) + un-fixed torch RNG) so
    # shards diverge. num_samples scales the shard count. LINEAR (not
    # fixed-container); its campaign preset is the design VARIANT, like
    # proteina. Ships behind FLAG_TOOL_IGGM (off). affinity_maturation is
    # EXCLUDED from campaigns in blueprints/campaigns.py (its delivered count
    # = num_samples * n_masked breaks the count==chunk invariant).
    "iggm",
)

# The tool-specific form field that carries the per-chunk design count.
# boltzgen varies ``budget`` (top-N returned) rather than ``num_designs``
# (its pool is a fixed 200 inside build_payload).
_DESIGN_PARAM_KEY: Mapping[str, str] = {
    "rfdiffusion": "num_designs",
    "bindcraft": "num_designs",
    "boltzgen": "budget",
    "pxdesign": "num_designs",
    "rfantibody": "num_designs",
    "proteina": "num_designs",
    # iggm's per-chunk count is num_samples (design.py --num_samples). This is
    # also its wallet ToolSpec.scaling_param, so _scaling_key_for(iggm) keys the
    # hold on the same value (see Phase 0 scaling-key generalization).
    "iggm": "num_samples",
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

# Per-tool launch concurrency override. A proteina shard is heavy (A100-80GB with
# co-resident AF2/RF3 folders) and unproven, so a proteina campaign starts at a
# low in-flight target to keep the instantaneous held amount (~concurrency x the
# per-shard hold) and the Modal blast radius small until the P4/P5 canaries
# calibrate it. Fund-and-drain still runs the full campaign; this only throttles
# how many shards are in flight at once. Tools not listed use the global default.
_LAUNCH_CONCURRENCY_OVERRIDE: Mapping[str, int] = {"proteina": 4}

# Tools that ONLY run as a campaign — they have no viable single-job atomic tier
# (every run is too heavy for one container), so /tools/<slug> routes to the
# campaign create flow instead of an atomic form.
#
# EMPTY as of the bring-your-own-target work. proteina used to be here purely
# because it shipped no form template, not because a single shard is unviable:
# one shard IS one self-contained container that yields _CHUNK_SIZE_OVERRIDE
# (8) designs and holds ~$15 against a $60 per-job cap, which is a perfectly
# ordinary atomic run. It now has templates/tools/proteina_form.html, so the
# redirect in blueprints/tools.py has nothing left to protect. Kept as a
# constant (not deleted) because the routes and tests reference it and a future
# tool may genuinely need it.
CAMPAIGN_ONLY_TOOLS: frozenset[str] = frozenset()

# Campaign tools that ship behind a FLAG_TOOL_<NAME> gate: hidden from every
# create/launch form and rejected on POST/estimate until the operator flips the
# flag in prod (mirrors the atomic-tool flag-gating). The 5 original campaign
# tools are unconditionally live and must NOT be filtered by tool_enabled, which
# is fail-closed — a tool with no flag env reads as off — so filtering the whole
# list would wrongly hide all five.
FLAG_GATED_CAMPAIGN_TOOLS: frozenset[str] = frozenset({"proteina", "iggm"})


def visible_campaign_tools() -> tuple[str, ...]:
    """The campaign tools a user may currently see and pick.

    Read per call, not cached at import: the flag is an env var an operator
    flips on the running service, and a module-level snapshot would need a
    redeploy to take effect.
    """
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    return tuple(
        t for t in SUPPORTED_TOOLS
        if t not in FLAG_GATED_CAMPAIGN_TOOLS or tool_enabled(t)
    )


def campaign_tool_gated_off(tool: str) -> bool:
    """True when ``tool`` is gated and its flag is still off.

    The launch routes and the run-create POST answer this with the SAME message
    they use for an unknown tool, so a probe cannot distinguish "hidden behind
    a flag" from "does not exist". ``api_runs_estimate`` does NOT: it answers
    "That tool is not available yet." while an unknown tool falls through to
    ``plan_chunks``'s different message, which is distinguishable. Pre-existing
    and low impact (the slug is guessable anyway), but do not describe that
    endpoint as indistinguishable.
    """
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    return tool in FLAG_GATED_CAMPAIGN_TOOLS and not tool_enabled(tool)


def launch_concurrency_for(tool: str) -> int:
    """The in-flight shard target a new campaign of ``tool`` starts at.

    Mirrors what ``create_campaign`` writes onto the row, so the first-wave
    START gate (``first_wave_hold_usd``) can size the required balance to the
    shards that will actually launch first rather than the global default.
    """
    return _LAUNCH_CONCURRENCY_OVERRIDE.get(tool, DEFAULT_CONCURRENCY_TARGET)

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

# States after which a campaign will never dispatch another chunk. A campaign
# NOT in this set can still re-mint a presigned URL from its
# target_storage_path on a later wave (see _dispatch_chunk), so its input
# object must never be age-swept out from under it — cron/purge_old_storage.py
# reads this to protect live inputs.
CAMPAIGN_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "completed", "completed_with_failures", "failed", "cancelled",
})

# tool_jobs statuses the progress rollup buckets.
_CHILD_STATUSES: tuple[str, ...] = (
    "pending", "running", "succeeded", "failed", "timeout", "cancelled",
)


def _quantize_usd(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 2dp display strings
# ---------------------------------------------------------------------------
#
# Money is CARRIED at 4dp (`_quantize_usd`) and must be DISPLAYED at 2dp,
# because a wallet figure with four decimals reads as a bug. That conversion is
# lossy, and which way it loses decides whether the number lies in our favour
# or the user's.
#
# It has to happen here rather than in the page. `Number(x).toFixed(2)` rounds
# to NEAREST, so a $573.6736 hold rendered client-side became "$573.67" -- a
# figure below the amount actually reserved, printed directly above a checkbox
# reading "the amount above will be held against my wallet balance". That is the
# same understatement `preauth_message` calls out by name for the refusal
# sentence, and it was left on the number the user actually consents to. Doing
# it in Decimal also means it is covered by the Python suite; the launch page's
# script has no automated coverage at all (A47).


def _display_usd(value, rounding: str) -> str:  # noqa: ANN001
    """Shared body. Raises on anything that is not a finite number.

    ``is_finite()`` FIRST, and not folded into the ``quantize`` call, because
    quantize does not signal on NaN: ``Decimal("NaN").quantize(...)`` returns NaN
    happily and renders the literal string "NaN" into the page. That is the same
    trap ``preauth_message`` documents, and a "$NaN to start" is worse than an
    error, because the page's failure handling clears the price and DISABLES the
    submit button while a rendered NaN leaves it armed. (Neither failure handler
    unticks the consent box; see ``display_balance_usd``.)
    """
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError(f"cannot display a non-finite amount: {value!r}")
    return str(amount.quantize(Decimal("0.01"), rounding=rounding))


def display_cost_usd(value) -> str:  # noqa: ANN001
    """A 2dp string for money the user will be CHARGED or have HELD.

    Rounds UP. A displayed cost or hold that is below the real figure is not a
    ceiling, and consent recorded against it is consent to the wrong number.
    """
    return _display_usd(value, ROUND_CEILING)


def display_balance_usd(value) -> str:  # noqa: ANN001
    """A 2dp string for money the user HAS.

    Rounds DOWN, for the mirror-image reason: a balance rounded up claims funds
    that are not there.

    The asymmetry is visible in a narrow band, and that is deliberate rather
    than overlooked. With a balance between the exact requirement and the
    requirement as DISPLAYED, the page shows less available than it shows
    needed, while the launch is in fact affordable: a $9.1800 balance against a
    $9.1765 first wave reads "$9.18 available" under "$9.19 to start" and still
    starts. The button state comes from the server's `affordable` flag, never
    from comparing the two rendered strings. Erring toward "top up" beats
    erring toward a hold the balance cannot cover.

    Note the band is wider than the one cent this originally described. The
    multi-tool panel's displayed requirement is the SUM of its rows' ceilings,
    not the ceiling of the exact total, so it can sit a few cents above the
    exact figure the gate uses. Measured across 2- to 7-tool cohorts, the gap
    reached 2 cents.

    All three helpers raise rather than guess on a non-numeric or non-finite
    input. Callers split into two groups with DIFFERENT failure modes, and the
    difference matters:

    1. Estimate endpoints and what they reach: ``api_runs_estimate``,
       ``api_target_launch_estimate``, and ``MultiLaunchPlan.rows()`` (called
       by the estimate endpoint only, never by ``target_launch_submit``). Both
       pages answer a failed estimate by clearing the figures and DISABLING the
       submit button, so raising here fails CLOSED. The disabled button is the
       mechanism; do not restate it as unticking the consent box, because
       neither failure handler does that.
    2. Renders with no submit button to disable: ``compute_campaign_create``'s
       refusal path, ``target_launch_submit``'s refusal path, and the
       ``display_cost_usd`` Jinja global that ``runs/detail.html`` and
       ``runs/list.html`` call on a stored ``budget_usd``. A raise there is a
       500 on a page, not a blocked spend. No gate depends on it, but it is not
       "fail closed" either, and calling it that would be the kind of
       comfortable claim this module has been wrong about before.

    ``first_wave_display_at_pace()`` is in BOTH groups: ``blueprints/targets.py``
    calls it from the estimate (fails closed) and from ``target_launch_submit``'s
    refusal (a POST re-render, where a raise is a 500). An earlier version of
    this paragraph put it in group 1 only, having carefully qualified
    ``rows()`` one line above and then given its sibling no qualification.

    Do NOT write that raising here is "not a regression because the previous
    formatting raised on the same inputs". It did not. ``'%.2f' %
    Decimal('NaN')`` returns ``'nan'`` where these raise, and Postgres numeric
    can hold NaN; and ``compute_campaign_create``'s predecessor,
    ``preauth_message(pre)`` with no override, catches inside its own derived
    branch. Both of those paths became fallible, which may well be the right
    trade, but it is a change and not a no-op.
    """
    return _display_usd(value, ROUND_FLOOR)


def display_ledger_usd(value) -> str:  # noqa: ANN001
    """The EXACT stored amount, for a historical row that must reconcile.

    Not a rounding direction. 2dp when the value is exactly 2dp, full 4dp when
    it is not, so nothing is ever misstated in either direction.

    Why this exists rather than reusing the two above. The wallet ledger prints
    ``tx.amount_usd`` (a cost) beside ``tx.balance_after_usd`` (a balance) in
    the SAME ROW, and consecutive rows are meant to add up. Costs round UP and
    balances round DOWN, so applying those two rules here would make the column
    stop reconciling in a fixed direction, on the one page whose entire purpose
    is that the reader can check the arithmetic themselves.

    The direction rules exist to protect DECISIONS: never show a hold below what
    will be taken, never claim a balance the wallet does not have. A ledger is a
    record, not a decision surface -- nobody spends from it -- so the property
    worth protecting there is internal consistency, and the only display that
    gives it is the exact one.

    PRECONDITION: the value carries at most 4 decimal places. Every wallet money
    column is ``numeric(12,4)`` and every API figure passes through
    ``_quantize_usd``, so that holds for both current callers. It is enforced
    rather than assumed, because an earlier version of this docstring said
    "EXACT" while the code quantized anything finer to 4dp with the context
    default of ROUND_HALF_EVEN -- NEAREST, the very behaviour this whole family
    of helpers exists to remove, inside the one function documented as exact.

    Raises on a non-finite, non-numeric, or finer-than-4dp input. Note what that
    does and does not buy, in the same terms as ``display_balance_usd`` above:
    this renders a page with no submit button, so a raise here is a 500 on the
    wallet ledger, not a blocked spend. It is fail-FAST, not fail-closed.
    """
    # Same shape as _display_usd: is_finite() FIRST, because quantize does not
    # signal on NaN and would render the literal string "NaN" into the ledger.
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError(f"cannot display a non-finite amount: {value!r}")
    four = amount.quantize(Decimal("0.0001"))
    if four != amount:
        raise ValueError(
            f"display_ledger_usd is exact and cannot render more than 4 decimal "
            f"places without rounding to NEAREST: {value!r}"
        )
    two = amount.quantize(Decimal("0.01"))
    # `==` on Decimal compares numerically, so Decimal("2.50") == Decimal("2.5000")
    # is True and a stored 2.5000 still renders as the clean "2.50".
    return str(two if two == amount else four)


def display_total_usd(displays: Iterable[str]) -> str:
    """The 2dp total of a column of amounts that are ALREADY displayed at 2dp.

    Sums what is printed instead of re-rounding the exact total, because those
    are two different numbers: rounding each row up and rounding the exact sum
    up gives ``sum(ceil(row)) >= ceil(sum(row))``, so a total taken from the
    exact sum prints BELOW the column standing directly above it. Measured on
    the two-tool cohort rfdiffusion@12 + pxdesign@12 at burst, the rows printed
    $2.02 and $5.03 under a total of $7.04.

    A consent panel whose rows do not add up to its own total is worse than
    either figure being a cent out, because the sum is the one part of it the
    reader can check for themselves.

    Still a true ceiling: each row display is at or above its own exact value,
    so their sum is at or above the exact total. The gate is applied to the
    exact figures, never to these, so this can only overstate what will be held.

    Use this for any figure that must reconcile with a panel of rows the reader
    will see -- INCLUDING a panel on a screen they have not reached yet. The
    steady-pace alternative is the example that matters: "Starting narrow would
    need $X" is a promise about the panel produced by acting on it, so it is
    totalled from the steady rows (``first_wave_display_at_pace``), not ceiled
    from the steady exact sum. Those two differ in 64 of 120 2- to 7-tool
    cohorts, so picking the wrong one is not theoretical.

    An earlier version of this paragraph said the opposite -- that a standalone
    figure is ceiled from its exact value directly -- and it was already false
    when written, in the same change that made the alternative a row sum. The
    behaviour is correct and pinned; only the description was wrong, which is
    the more dangerous half, because it aims the next author straight back at
    the defect.
    """
    total = Decimal("0")
    for display in displays:
        amount = Decimal(str(display))
        if not amount.is_finite():
            raise ValueError(f"cannot total a non-finite amount: {display!r}")
        total += amount
    # A no-op on every real input: a sum of 2dp Decimals is already 2dp, whole
    # dollars included (Decimal("2.00") + Decimal("3.00") is "5.00", not "5").
    # It is here for the EMPTY sum only, where the seed would otherwise render
    # as "0". No PRODUCTION caller passes an empty sequence, but
    # test_the_total_helper_also_fails_closed asserts the "0.00" directly, so
    # this line is pinned even though no route reaches it.
    return str(total.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


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
# rfantibody mirrors bindcraft: ranomics-rfantibody-prod's Modal function timeout
# is 23h (_MAX_SESSION_S=82800), so a 10h chunk (16 designs at 0.8 util) sits well
# under the ceiling. Its pipeline streams scores only (a chunk is all-or-nothing),
# so this stays deliberately conservative rather than pushing toward 23h.
_RFANTIBODY_CAMPAIGN_CONTAINER_S = 36000  # 10h -> 16 designs/chunk at 0.8 util

# Tools sized against a bigger-than-pilot campaign container (GPU-seconds one
# sub-job is planned against). Tools not listed use the pilot container.
_CAMPAIGN_CONTAINER_S: Mapping[str, int] = {
    "bindcraft": _BINDCRAFT_CAMPAIGN_CONTAINER_S,
    "rfantibody": _RFANTIBODY_CAMPAIGN_CONTAINER_S,
}

# A campaign chunk that runs the tool's validated pilot job verbatim (no bigger
# container). pxdesign's pilot does up to 24 designs in one 3600s container, but
# the wallet spec's gpu_s/design is miscalibrated for it (it implies ~2), so pin
# the chunk to the validated single-job maximum of 24.
# proteina: one search shard emits nsamples x nrepeat x best_of_n.replicas designs
# (protein_binder default 4 x 1 x 2 = 8) from one fixed container, then the hub does
# global cross-shard top-K. Pin the chunk to that 8-design shard yield so num_designs
# splits into ceil(num_designs / 8) shards. Refit if a variant's default output count
# changes.
# iggm: pin the chunk to 40 designs/shard (what the linear formula yields for
# every iggm variant at PRESET_CAPS 3000s / 60 gpu-s-per-design). Pinned, not
# derived, because the live estimate endpoint defaults preset='pilot' (which has
# no ('iggm','pilot') PRESET_CAPS row) and would otherwise collapse the chunk to
# the baseline (1) and mis-size the preview. iggm is NOT fixed-container, so the
# per-chunk estimate + hold still scale with the 40-design count (num_samples).
_CHUNK_SIZE_OVERRIDE: Mapping[str, int] = {"pxdesign": 24, "proteina": 8, "iggm": 40}

# Tools whose GPU cost is one fixed container per sub-job regardless of the
# chunk's design count, so BOTH the point estimate and the wallet hold price at
# the baseline (scale 1.0) rather than scaling by chunk_size. Used by
# _estimate_chunk_cost AND child_hold_usd; keep them reading the same set so the
# estimate and the hold never drift (a per-design hold on a fixed-container tool
# inflates the first-wave gate ~12x).
#   * boltzgen: one fixed 200-design pool; returned-count (budget) is free.
#   * pxdesign: one 3600s pilot container does the whole 24-design chunk; the
#     spec's per-design rate is miscalibrated (implies ~12 containers for 24).
#   * proteina: one shard = one fixed container returning up to 8 designs; the
#     shard cost is one container regardless of how many designs survive filter,
#     so both the estimate AND the hold price at the baseline (scale 1.0). Omitting
#     proteina here would per-design-scale the hold and inflate the first-wave gate.
_FIXED_CONTAINER_TOOLS: tuple[str, ...] = ("boltzgen", "pxdesign", "proteina")


def _campaign_container_seconds(tool: str, preset: str = "pilot") -> int:
    """GPU-seconds a campaign sizes ONE sub-job against.

    Defaults to the pilot container; tools in ``_CAMPAIGN_CONTAINER_S`` use a
    bigger one because their Modal session runs up to 23h. ``preset`` selects the
    PRESET_CAPS row for tools whose campaign preset is not "pilot" (e.g. proteina
    variants); the 5 live campaign tools always pass "pilot", unchanged.
    """
    override = _CAMPAIGN_CONTAINER_S.get(tool)
    if override is not None:
        return override
    return preset_gpu_seconds(tool, preset)


def _campaign_session_inputs(tool: str) -> dict:
    """Extra Modal inputs so a bigger campaign chunk gets a matching session budget.

    A bindcraft campaign chunk holds ~16 designs (vs the 3/chunk pilot), which
    needs more than the default 4h ``_total_budget_hours`` or the pipeline stops
    early; derive the budget from the enlarged container. rfantibody carries the
    same input for parity (its pipeline currently ignores it and is bounded by
    the 23h Modal timeout, but a future budget-aware pipeline picks it up free).
    Other tools keep the default (their chunks are far shorter).
    """
    override = _CAMPAIGN_CONTAINER_S.get(tool)
    if override is not None:
        return {"_total_budget_hours": override / 3600}
    return {}


def _chunk_size_for(tool: str, preset: str = "pilot") -> int:
    """Designs per sub-job for ``tool``, sized to one campaign container.

    A tool in ``_CHUNK_SIZE_OVERRIDE`` pins the chunk to its validated pilot job.
    boltzgen is special (budget-based, fixed pool). The linear tools derive
    the size from GPU-seconds-per-design vs the campaign container's GPU-seconds
    budget, so a chunk comfortably fits one container.
    """
    if tool in _CHUNK_SIZE_OVERRIDE:
        return _CHUNK_SIZE_OVERRIDE[tool]
    if tool == "boltzgen":
        return BOLTZGEN_DESIGNS_PER_JOB
    spec = get_tool_spec(tool)
    if spec is None or spec.designs_per_run_baseline <= 0:
        # Conservative fallback; should not happen for SUPPORTED_TOOLS.
        return 1
    gpu_s_per_design = float(spec.expected_gpu_seconds) / float(
        spec.designs_per_run_baseline
    )
    container_s = _campaign_container_seconds(tool, preset)
    if gpu_s_per_design <= 0 or container_s <= 0:
        return spec.designs_per_run_baseline
    size = int((container_s * _CONTAINER_UTILIZATION) / gpu_s_per_design)
    # Never chunk below the tool's own baseline (keeps per-chunk cost
    # efficient; the wallet estimate floors its multiplier at 1.0 anyway).
    return max(size, spec.designs_per_run_baseline)


def single_container_ceiling(tool: str, preset: str = "pilot") -> int:
    """Max designs one GPU container reliably does for ``tool``.

    This is exactly the campaign chunk size: the point above which a single-job
    submit would need more than one container and should instead fan out as a
    campaign. Used by the tool forms (D1 auto-route threshold) and the
    ``tool_submit`` backstop. Only meaningful for ``SUPPORTED_TOOLS``.
    """
    return _chunk_size_for(tool, preset)


def _scaling_key_for(tool: str) -> str:
    """The params key the wallet estimator scales the per-chunk cost on.

    ``shared.wallet_estimates._effective_scaling_value`` reads
    ``params[spec.scaling_param]``. The campaign estimator/hold historically
    hardcoded ``"num_designs"``, which is correct only for a tool whose ToolSpec
    ``scaling_param`` IS ``num_designs`` (every current campaign tool) or which is
    fixed-container (priced at the baseline, scale 1.0, so key-insensitive). A
    future tool whose wallet ``scaling_param`` differs (iggm=``num_samples``, the
    fold tools=``n_designs_total``) would silently fall back to the 1-design
    baseline and UNDER-hold. Keying on the tool's real ``scaling_param`` fixes
    that while staying byte-identical for the existing tools: they all resolve to
    ``num_designs`` (or price at baseline regardless of the key).
    """
    spec = get_tool_spec(tool)
    if spec and spec.scaling_param:
        return spec.scaling_param
    return "num_designs"


def _estimate_chunk_cost(tool: str, chunk_size: int, preset: str = "pilot") -> Decimal:
    """USD estimate for ONE sub-job of ``tool`` at ``chunk_size`` designs."""
    spec = get_tool_spec(tool)
    # Fixed-container tools (see _FIXED_CONTAINER_TOOLS) cost one container
    # regardless of the chunk's design count, so estimate at the baseline
    # (scale 1.0) rather than scaling by chunk_size.
    if tool in _FIXED_CONTAINER_TOOLS:
        designs_for_estimate = spec.designs_per_run_baseline if spec else 2
    else:
        designs_for_estimate = chunk_size
    return estimated_cost_for_tool(
        None, tool, {_scaling_key_for(tool): designs_for_estimate, "preset": preset}
    )


def plan_chunks(tool: str, requested_designs: int, preset: str = "pilot") -> ChunkPlan:
    """Plan how a design request splits into sub-jobs. Pure; raises ValueError.

    The ValueError messages are user-facing (surfaced on the create form).
    ``preset`` is carried onto the plan/campaign row and selects the tool's
    container/cost profile; the 5 live campaign tools pass "pilot" (unchanged).
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

    chunk_size = _chunk_size_for(tool, preset)
    total_subjobs = math.ceil(requested / chunk_size)
    if total_subjobs > MAX_SUBJOBS_PER_CAMPAIGN:
        max_designs = MAX_SUBJOBS_PER_CAMPAIGN * chunk_size
        raise ValueError(
            f"That request needs {total_subjobs} sub-jobs, over the current "
            f"per-campaign limit of {MAX_SUBJOBS_PER_CAMPAIGN} "
            f"({max_designs} designs for {tool}). Reduce the count or split "
            f"it into multiple campaigns."
        )

    est_per_chunk = _estimate_chunk_cost(tool, chunk_size, preset)
    budget = _quantize_usd(
        Decimal(total_subjobs) * est_per_chunk * BUDGET_BUFFER
    )
    return ChunkPlan(
        tool=tool,
        preset=preset,
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
    # design_targets linkage (migration 0039). Nullable forever: a run
    # launched from a plain upload has no target, and proteina's curated-task
    # path has no structure at all. target_storage_path stays denormalized
    # even when target_id is set — that is what keeps the driver unchanged.
    target_id: Optional[str] = None
    launch_group_id: Optional[str] = None
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
            target_id=row.get("target_id"),
            launch_group_id=row.get("launch_group_id"),
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
            "target_id": self.target_id,
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
    preset: str = "pilot",
    name: Optional[str] = None,
    target_pdb_id: Optional[str] = None,
    target_storage_path: Optional[str] = None,
    target_name: Optional[str] = None,
    target_id: Optional[str] = None,
    launch_group_id: Optional[str] = None,
    concurrency_target: Optional[int] = None,
) -> Optional[ComputeCampaign]:
    """Insert a ``draft`` campaign row from a validated request.

    Raises ValueError (user-facing) when the request cannot be planned
    (unsupported tool, bad count, over the sub-job cap). Returns None on a
    persistence failure. ``preset`` selects the tool's container/cost profile
    (proteina variants); the 5 live campaign tools pass "pilot" (unchanged).

    ``target_id`` / ``launch_group_id`` / ``concurrency_target`` all default to
    today's behaviour: no target, no launch group, and the tool's own launch
    concurrency. They exist so a multi-tool launch can parent its runs to one
    target, group them, and divide the global in-flight cap between them
    without a second create path.
    """
    plan = plan_chunks(tool, requested_designs, preset)
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
        "concurrency_target": (
            max(1, int(concurrency_target))
            if concurrency_target
            else launch_concurrency_for(tool)
        ),
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "status": "draft",
        "budget_usd": float(plan.budget_usd),
    }
    # Only sent when actually set. PostgREST 400s on a column the schema does
    # not have, so a database still missing 0039 keeps creating ordinary
    # untargeted runs instead of failing every launch.
    if target_id is not None:
        row["target_id"] = target_id
    if launch_group_id is not None:
        row["launch_group_id"] = launch_group_id
    try:
        response = client.table(_TABLE).insert(row).execute()
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
        return ComputeCampaign.from_row(rows[0])
    except Exception:
        logger.error("create_campaign: insert failed", exc_info=True)
        return None


def _campaign_or_none(row) -> Optional[ComputeCampaign]:  # noqa: ANN001
    """:meth:`ComputeCampaign.from_row` without the raise.

    ``from_row`` subscripts five columns directly (``id``, ``user_id``, ``tool``,
    ``preset``, ``status``) and coerces five more through ``int()``, so a row
    missing a column or holding a non-numeric value raises KeyError or
    ValueError. Four call sites in this module had it OUTSIDE the ``try`` that
    exists to make them total, so a single unreadable row escaped as a 500 from
    ``/campaigns`` and the target detail page, or aborted the paused-campaign
    sweep part way -- in each case contradicting the ``None``/``[]``/best-effort
    contract those functions document.

    Row-shape faults are not currently reachable through ``select("*")`` on a
    table whose columns the migrations pin, so this is a guard against a partial
    migration or a renamed column, not a live bug. It is here because the
    functions above promise not to raise, and a promise a reader can check is
    worth more than one that happens to hold.

    ``from_row`` itself stays strict: a caller that wants to know goes there.
    """
    try:
        return ComputeCampaign.from_row(row)
    except Exception:
        logger.warning(
            "compute_campaigns: unreadable campaign row (id=%r)",
            (row or {}).get("id") if isinstance(row, dict) else None,
            exc_info=True,
        )
        return None


def get_campaign(
    campaign_id: str, *, user_id: Optional[str] = None
) -> Optional[ComputeCampaign]:
    """Fetch a campaign by id. Pass ``user_id`` to enforce owner scope.

    Returns None on every failure, INCLUDING an unreadable row. The launch
    route's fund loop leans on that: it calls this after the commit point to
    decide whether a campaign is really still `draft`, and a raise there would
    500 a request that has already spent money and release the idempotency
    claim with it.
    """
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
    return _campaign_or_none(data)


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
    # Unreadable rows are dropped and logged, not raised: this feeds
    # `GET /campaigns`, where one bad row must not 500 the whole list.
    return [c for c in (_campaign_or_none(r) for r in rows) if c is not None]


# Paging for the per-target run list. Must stay at or below the PostgREST
# max_rows in supabase/config.toml: ABOVE it a full page comes back clamped,
# len(batch) < page size reads as "last page", and the loop stops early with a
# silently short list. (At exactly max_rows a clamped page still returns
# page_size rows, so the short-page test does not misfire; only > breaks.)
#
# The assert compares two module literals -- it catches someone raising the
# page size here, NOT someone lowering max_rows in config.toml, which would
# truncate with the assert still green. Keeping the two in sync is manual.
_POSTGREST_MAX_ROWS = 1000
_TARGET_RUN_PAGE_SIZE = 500
_MAX_TARGET_RUN_PAGES = 20
assert _TARGET_RUN_PAGE_SIZE <= _POSTGREST_MAX_ROWS, (
    "page size must not exceed PostgREST max_rows or paging truncates silently"
)


def list_campaigns_for_target(
    target_id: str,
    *,
    user_id: Optional[str] = None,
    include_drafts: bool = False,
) -> list[ComputeCampaign]:
    """A target's COMPUTE-CAMPAIGN runs, newest first.

    ``draft`` rows are EXCLUDED by default. A multi-tool launch inserts every
    campaign as a draft and funds them afterwards, so a launch that fails part
    way leaves drafts behind. A draft is inert in every sense that matters --
    ``drive_campaign`` refuses it, ``_campaign_spend_today`` skips it, no hold
    is ever placed -- so it is not a run, and listing it under a heading that
    says "Runs" would assert something false about a row that never started.

    The omission is disclosed rather than hidden, but NOT by this function and
    not by anything it can guarantee. The count of what did not start rides the
    launch redirect's ``stalled`` query param, and
    ``templates/targets/detail.html`` gates that half of the banner on the param
    ALONE. It has to: this function's count cannot carry the news, because a
    stranded draft is exactly what it filters out, and because its own error
    path (below) can return short. Do not re-nest the stalled line under the
    started one -- the read failure that strands a campaign is usually the same
    one that empties this list.

    The real cost, stated because it is not obvious: this hides the draft HERE
    only. :func:`list_campaigns_for_user` is a DIFFERENT query, applies no
    status filter, and is deliberately not changed here, so a stranded draft
    still renders as a card on /campaigns while being absent from its own
    target's page. The two lists disagree by design; filed as **A37**.

    Its blast radius, measured rather than assumed: **one** production caller,
    ``blueprints/campaigns.py:95`` (``GET /campaigns``). Not the homepage, which
    loads ``list_jobs_for_user`` (``tool_jobs``, not campaigns) and only links
    to /campaigns.

    :func:`cancel_campaign` would accept a draft (it refuses only
    completed/completed_with_failures/failed/cancelled), but **no UI reaches
    it**: ``templates/runs/detail.html:102`` renders the Cancel button only for
    ``funded``/``running``/``completing``/``paused_insufficient_funds``, and
    ``templates/runs/list.html`` has no cancel affordance at all. So a stranded
    draft is visible on /campaigns and not clearable from there; clearing it
    needs the API or the database. Do not upgrade "the function accepts it" into
    "the user can act on it".

    Pass ``include_drafts=True`` for admin or diagnostic reads. No production
    caller passes it today; the launch and detail paths both want the default.

    Not every run against the target. This reads ``compute_campaigns`` only,
    and migration 0039 also puts ``target_id`` on ``tool_jobs``: a standalone
    run launched from the ``target:`` reuse token lands as a ``tool_jobs`` row
    with ``campaign_id`` NULL and is invisible here, as is a yardstick refold.
    A target with only those would render the "nothing has been run yet" empty
    state. Latent in Phase 1 -- no template mints a ``target:`` token yet -- and
    Phase 3's fan-in has to read both tables. Do not widen the summary line
    above without widening the query.

    Owner-scoped when ``user_id`` is given.

    Filtered server-side on ``target_id``. The obvious alternative -- pull the
    user's recent campaigns and keep the ones whose id matches -- is wrong in a
    way that is invisible in testing: that cap applies to the user's WHOLE
    campaign history, not to this target's runs, so a target whose runs all sit
    outside the window renders as having never been run. It also cost a second
    read.

    Paged past the row clamp for the usual reason: ``.limit()`` is clamped
    identically and a clamped read is indistinguishable from a complete one at
    the call site. Pages are ordered by ``id`` because that ordering is stable
    across page boundaries; the newest-first ordering the caller wants is
    applied afterwards, in memory, where ties cannot reshuffle a boundary.

    NOT guaranteed complete on the error path: a failed page returns the runs
    read so far and logs. That is deliberate for a read-only page (a short run
    strip beats a 500) and is why no deletion or billing decision may be taken
    from this list. The retention guard that CAN destroy data pages the same
    table and fails closed instead -- see cron/purge_old_storage.py.
    """
    client = get_service_client()
    if client is None:
        return []
    rows: list = []
    start = 0
    for _ in range(_MAX_TARGET_RUN_PAGES):
        try:
            query = client.table(_TABLE).select("*").eq("target_id", target_id)
            if user_id is not None:
                query = query.eq("user_id", user_id)
            if not include_drafts:
                # Server-side, not an in-memory filter after the read: the
                # page bound counts ROWS RETURNED, so dropping drafts locally
                # would let a target with many stranded drafts exhaust the
                # page budget and silently truncate its real runs.
                query = query.neq("status", "draft")
            response = (
                query.order("id")
                .range(start, start + _TARGET_RUN_PAGE_SIZE - 1)
                .execute()
            )
        except Exception:
            logger.warning(
                "list_campaigns_for_target failed for %s", target_id, exc_info=True
            )
            break
        batch = list(getattr(response, "data", None) or [])
        rows += batch
        if len(batch) < _TARGET_RUN_PAGE_SIZE:
            break
        start += _TARGET_RUN_PAGE_SIZE
    else:
        logger.error(
            "list_campaigns_for_target: page bound hit for target %s; "
            "the run list may be incomplete", target_id,
        )
    # Dropped-and-logged for the same reason the paging error above breaks
    # instead of raising: this is a read-only strip, and "a short run strip
    # beats a 500" is only true if converting the rows cannot raise either.
    campaigns = [c for c in (_campaign_or_none(r) for r in rows) if c is not None]
    campaigns.sort(key=lambda c: str(getattr(c, "created_at", "") or ""), reverse=True)
    return campaigns


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


# Rows per page when fanning a campaign's sub-jobs in. Must stay at or below
# the PostgREST max_rows in supabase/config.toml (1000) or a page comes back
# short and pagination stops early.
_CHILD_PAGE_SIZE = 500

# Hard bound on the paging loop: MAX_SUBJOBS_PER_CAMPAIGN attempts, each of
# which could in principle have max_attempts rows. Purely a runaway guard
# against a backend that keeps returning full pages.
_MAX_CHILD_PAGES = (MAX_SUBJOBS_PER_CAMPAIGN * DEFAULT_MAX_ATTEMPTS) // _CHILD_PAGE_SIZE + 2


def iter_succeeded_children(campaign_id: str, client, *, columns: str = None):
    """Yield every succeeded sub-job row of a campaign, paging past max_rows.

    A plain ``.select()`` is clamped by PostgREST to the project's ``max_rows``
    (1000 in ``supabase/config.toml``) while a campaign may hold up to
    ``MAX_SUBJOBS_PER_CAMPAIGN`` (50000) children, so the unpaged read silently
    truncated the fan-in: the merged table, the exports, and the "global top-N"
    the Boltz-2 validation refold spends real GPU on were all computed from at
    most the first 1000 rows, with nothing to indicate rows were missing.

    ``.limit()`` does NOT fix this (PostgREST clamps it the same way);
    ``.range()`` is the only way past it. Ordered by ``id`` so page boundaries
    are stable and no row is skipped or repeated across pages.
    """
    select_cols = columns or "id,chunk_index,attempt,result"
    start = 0
    for _ in range(_MAX_CHILD_PAGES):
        resp = (
            client.table("tool_jobs")
            .select(select_cols)
            .eq("campaign_id", campaign_id)
            .eq("status", "succeeded")
            .order("id")
            .range(start, start + _CHILD_PAGE_SIZE - 1)
            .execute()
        )
        batch = list(getattr(resp, "data", None) or [])
        yield from batch
        if len(batch) < _CHILD_PAGE_SIZE:
            return
        start += _CHILD_PAGE_SIZE
    logger.error(
        "iter_succeeded_children: page bound hit for campaign %s; "
        "results may be incomplete", campaign_id,
    )


def aggregate_campaign_candidates(
    campaign_id: str, *, user_id: Optional[str] = None,
    limit: Optional[int] = 300,
) -> dict:
    """Fan a campaign's sub-jobs' candidates into one globally-ranked list.

    ``limit`` bounds how many of the top-ranked candidates come back; pass
    ``None`` for no cap (used by the CSV / FASTA campaign exports, which must
    return the full ranked set). Sub-job rows are fetched through
    :func:`iter_succeeded_children`, which pages past the PostgREST
    ``max_rows`` clamp, so ``limit=None`` really is the full set.

    Returns ``{"candidates": [...top ``limit``...], "total": int,
    "columns": [...], "capped": bool, "tool": str}``. Each returned candidate
    is a shallow copy tagged with ``_source_job_id`` / ``_source_chunk`` /
    ``_source_index`` so the merged table can build per-candidate PDB, export,
    and shortlist references back to the child job that produced it.

    Ownership-gated: when ``user_id`` is given and the campaign is not that
    user's, returns an empty envelope (an IDOR-safe no-op). Reads only the
    metadata columns (``result`` already stores PDBs as Storage refs, so no
    structure bytes are transferred here).

    Ordering: passing designs first (per :func:`shared.jobs.candidate_passed_filter`),
    then by the tool's primary metric in its configured direction, missing
    metric last. Because passing designs sort ahead of failing ones, the top-N
    cap never drops a passing design in favour of a higher-raw-score failing one.
    Retry siblings are de-duplicated by keeping the highest ``attempt`` per
    ``chunk_index``.
    """
    from shared.jobs import candidate_records, candidate_passed_filter
    from shared.result_columns import (
        columns_for,
        primary_metric_for,
        candidate_metric,
        normalize_candidate,
    )

    empty = {
        "candidates": [], "total": 0, "columns": [],
        "capped": False, "tool": None,
    }
    campaign = get_campaign(campaign_id, user_id=user_id)
    if campaign is None:
        return empty
    tool = campaign.tool
    columns = columns_for(tool)
    metric_key, direction = primary_metric_for(tool)
    base = {**empty, "columns": columns, "tool": tool}

    client = get_service_client()
    if client is None:
        return base
    try:
        rows = list(iter_succeeded_children(campaign_id, client))
    except Exception:
        logger.warning(
            "aggregate_campaign_candidates: query failed for %s",
            campaign_id, exc_info=True,
        )
        return base

    # Dedupe retries: keep the highest attempt per chunk_index (rows lacking a
    # chunk_index — should not happen for campaign children — key on job id).
    best_by_chunk: dict = {}
    for r in rows:
        chunk = r.get("chunk_index")
        key = chunk if chunk is not None else r.get("id")
        attempt = r.get("attempt") or 1
        prev = best_by_chunk.get(key)
        if prev is None or attempt > (prev.get("attempt") or 1):
            best_by_chunk[key] = r

    merged: list[dict] = []
    for r in best_by_chunk.values():
        job_id = r.get("id")
        chunk = r.get("chunk_index")
        for local_idx, cand in enumerate(candidate_records(r.get("result"))):
            if not isinstance(cand, dict):
                continue
            # Lift any root-level headline metric into scores first, or the
            # tool's declared primary metric resolves to None and the merged
            # table is unordered (this is what made every iggm row rank equal).
            c = dict(normalize_candidate(cand, tool))
            c["_source_job_id"] = job_id
            c["_source_chunk"] = chunk
            c["_source_index"] = local_idx
            merged.append(c)

    total = len(merged)

    def _sort_key(c: dict):
        passed = 0 if candidate_passed_filter(c) else 1
        val = candidate_metric(c, metric_key)
        missing = 1 if val is None else 0
        ordv = 0.0 if val is None else (-val if direction == "desc" else val)
        return (passed, missing, ordv)

    merged.sort(key=_sort_key)

    # limit=None means "no cap" (full ranked set); merged[:None] is the whole
    # list and nothing is dropped, so capped is False.
    candidates = merged if limit is None else merged[:limit]
    capped = limit is not None and total > limit

    return {
        "candidates": candidates,
        "total": total,
        "columns": columns,
        "capped": capped,
        "tool": tool,
    }


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


# User-facing copy for a refused start gate, keyed on PreauthResult.reason.
# {required} is the FIRST WAVE, the balance needed to START, which under
# fund-and-drain is smaller than the budget. Say "to start", never "total".
# Placeholders are bare braces and the literal "$" is written into the message,
# because the previous "${required}" token INCLUDED the dollar sign in the
# string being replaced and so rendered "about 9.18 to start" with no currency.
_PREAUTH_MESSAGES: Mapping[str, str] = {
    PREAUTH_NO_WALLET: "Your wallet is unavailable. Try again in a moment.",
    PREAUTH_FROZEN: "Your wallet is on hold. Contact support to resume.",
    PREAUTH_INSUFFICIENT: (
        "Your balance does not cover the first batch of {subject} "
        "(about {required} to start). Top up your wallet and try again. "
        "You only pay for compute that runs, and {pauses} if "
        "your balance runs low."
    ),
    PREAUTH_VERIFICATION: (
        "Campaigns above {threshold} need an approved account. "
        "Contact us to raise your limit."
    ),
    PREAUTH_VELOCITY: (
        "This would exceed your daily campaign spending limit. "
        "Try again tomorrow or with {smaller}."
    ),
}


def preauth_message(
    pre: PreauthResult, *, count: int = 1, required_display: Optional[str] = None
) -> str:
    """One sentence explaining a refused start gate.

    ``count`` is how many runs the gate covered. A multi-tool launch passes one
    summed gate for N runs, so the singular copy ("this campaign cannot start")
    would misdescribe what was refused and would point the user at the wrong
    remedy: with several tools selected, dropping one is usually cheaper than
    topping up.

    ``required_display`` is the figure the PAGE is showing for the same hold,
    and callers that render a panel must pass it. Deriving it here instead used
    to be safe, because both were ``ceil(exact)``. It stopped being safe the
    moment the multi-tool panel started totalling its rows' 2dp displays, which
    is a slightly larger number (``sum(ceil) >= ceil(sum)``): the refusal
    sentence then named $9.18 while the panel above it, on the same 400, said
    $9.19 and the consent line under it said "the amount above will be held".
    A user who tops up to the number in the sentence is refused again.

    So there is one displayed hold per screen and the caller owns it. Omitting
    it falls back to rounding ``pre.required_usd`` up, which is right for a
    caller with no panel of its own.
    """
    plural = count > 1
    msg = _PREAUTH_MESSAGES.get(
        pre.reason,
        "These runs cannot start right now." if plural
        else "This campaign cannot start right now.",
    )
    required = getattr(pre, "required_usd", None)
    # ROUND_CEILING, not "%.2f". The gate holds a 4dp Decimal, so half-even
    # rounding names a figure BELOW the one that just refused the user: a
    # first wave of 573.6736 renders as $573.67, and a wallet topped to
    # exactly $573.67 is refused again by the same message. A required amount
    # has to round UP or it is not a ceiling.
    # Coerced through str() before quantizing, the same way campaign_preauth
    # coerces its own inputs, and wrapped: PreauthResult is a plain frozen
    # dataclass with no coercion, so nothing stops a caller passing a float
    # (no .quantize), a non-numeric string or an inf/NaN (InvalidOperation).
    # This is the one path whose entire job is to explain a refusal to a user,
    # so anything unrenderable falls back to the wording rather than becoming a
    # 500. Every current caller passes a Decimal; the guard is for the next one.
    shown = None
    if required_display:
        # Rendered verbatim. It is already the string on the screen, and
        # re-deriving it here is exactly how the two came apart. Truthiness, not
        # `is not None`: an empty string would otherwise render a bare "$", and
        # falling back to the derived figure is the better of the two wrong
        # answers.
        shown = required_display
    elif required is not None:
        try:
            amount = Decimal(str(required))
            # is_finite() first: NaN does NOT raise here, it quantizes to NaN
            # and renders as the words "about $NaN to start".
            if not amount.is_finite():
                raise ValueError("non-finite")
            shown = amount.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        except (ArithmeticError, TypeError, ValueError):
            logger.warning(
                "preauth_message: unrenderable required_usd %r", required,
            )
            shown = None
    return (
        msg.replace("{threshold}", f"${VERIFICATION_THRESHOLD_USD}")
           .replace("{subject}", f"these {count} runs" if plural else "this campaign")
           .replace("{pauses}", "they pause" if plural else "the campaign pauses")
           .replace("{smaller}", "fewer tools" if plural else "a smaller campaign")
           .replace(
               "{required}",
               f"${shown}" if shown is not None else "the first batch",
           )
    )


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


def estimate_child_cost(tool: str, design_count: int, preset: str = "pilot") -> Decimal:
    """Point-estimate USD for one sub-job at ``design_count`` designs.

    The non-binding forecast input and the value stored on the child as
    ``_wallet.estimate_usd``. boltzgen is flat per job (fixed 200-pool); the
    linear tools scale by the count. The wallet HOLD (reservation) is sized
    separately and cushioned, see :func:`child_hold_usd`.
    """
    return _estimate_chunk_cost(tool, int(design_count), preset)


def child_hold_usd(tool: str, design_count: int, preset: str = "pilot") -> Decimal:
    """Cushioned USD to HOLD for one sub-job (the wallet reservation).

    A cushion above the point estimate so actual usually settles under the
    hold, releasing surplus (a clean ledger) instead of posting a variance
    charge. Clamped to the per-tool hard cap by
    :func:`shared.wallet_estimates.cushioned_hold_usd`. Fixed-container tools
    (see _FIXED_CONTAINER_TOOLS) price at their one-container baseline, matching
    :func:`_estimate_chunk_cost`; the linear tools scale by the design count.
    """
    spec = get_tool_spec(tool)
    if tool in _FIXED_CONTAINER_TOOLS:
        designs_for_estimate = spec.designs_per_run_baseline if spec else 2
    else:
        designs_for_estimate = int(design_count)
    return cushioned_hold_usd(
        None, tool, {_scaling_key_for(tool): designs_for_estimate, "preset": preset}
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
    return _quantize_usd(child_hold_usd(plan.tool, plan.chunk_size, plan.preset) * waves)


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
        import tools.proteina  # noqa: F401,PLC0415
        import tools.iggm  # noqa: F401,PLC0415
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


def fund_campaign(campaign_id: str) -> bool:
    """Mark a draft campaign funded (called by the route after preauth).

    Returns True iff the row actually moved out of ``draft``. This matters
    because ``drive_campaign`` early-returns on a draft, so a fund that
    silently failed leaves a campaign the user believes is running parked
    forever with no signal. The old implementation went through
    ``_update_campaign``, which returns None and swallows every exception, so
    the caller could not tell. A multi-tool launch funds N rows in a loop and
    has to report which ones started (audit item A12).

    CAS on ``draft`` rather than an unconditional UPDATE, so this can no longer
    rewind a campaign that has already progressed to ``running``. Nothing calls
    it that way today; the constraint is here to keep it that way.
    """
    return _cas_transition(
        campaign_id, "funded", ("draft",), {"confirmed_at": _now_iso()}
    )


def _dispatch_chunk(campaign: "ComputeCampaign", chunk_index: int) -> str:
    """Reserve a per-child hold, create the sub-job, and spawn it on Modal.

    Returns one of:
      * ``"launched"`` — a child row was created and a Modal run started.
      * ``"failed"``   — a child row exists but reached a terminal failure
                          (modal submit failed); the chunk IS dispatched.
      * ``"skipped"``  — NO child row was created (transient hold refusal,
                          transient insert failure, or the target PDB could not
                          be presigned); retry this SAME index on a later drive
                          pass (the frontier does not advance).
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

    # Resolve the target URL BEFORE any money moves. A campaign persists the
    # storage PATH and re-mints a short-lived signed URL per wave, so a Storage
    # outage or a revoked object makes every remaining chunk unrunnable. This
    # used to sit after create_job with a bare except that left presigned_url
    # "" and dispatched anyway, so the driver kept placing holds and launching
    # containers with no input file until the campaign drained. Failing here
    # returns "skipped": no row, no hold, frontier does not advance, and the
    # chunk is retried on a later tick once Storage recovers.
    presigned_url = ""
    if campaign.target_storage_path:
        try:
            presigned_url = presigned_input_url(
                campaign.target_storage_path, expires_seconds=7200
            )
        except Exception:
            logger.error(
                "campaign %s chunk %s: presign failed; skipping dispatch",
                campaign.id, chunk_index, exc_info=True,
            )
            return "skipped"
        if not presigned_url:
            # Defensive: a falsy return without an exception is the same
            # unrunnable state as a raise, so treat it identically.
            logger.error(
                "campaign %s chunk %s: presign returned empty; skipping dispatch",
                campaign.id, chunk_index,
            )
            return "skipped"

    point_estimate = estimate_child_cost(campaign.tool, design_count, campaign.preset)
    hold_amount = child_hold_usd(campaign.tool, design_count, campaign.preset)
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
        # Stamped on the child too, not just the campaign, so a design is
        # target-attributable without joining back through compute_campaigns
        # — the target fan-in reads tool_jobs directly.
        target_id=campaign.target_id,
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
        # Skip an unreadable row rather than abort the sweep on it: the rows
        # after it still need their notification.
        campaign = _campaign_or_none(row)
        if campaign is None:
            continue
        _notify_campaign_paused(campaign)
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


def reconcile_campaign_children(campaign_id: str, *, max_poll: int = 64) -> int:
    """Poll a campaign's in-flight sub-jobs and terminalise the finished ones.

    Atomic-pattern tools (proteina, iggm, and the boltz2 / mpnn / af2 / … shape)
    return their result INLINE from the Modal function and never POST a terminal
    webhook. The driver deliberately never flips a child's terminal state, and
    the campaign status page polls only aggregate counts — so a finished
    atomic-pattern child that nobody is individually watching hangs in
    ``running`` (its wallet hold stranded) until the 6-hour stuck-job sweeper.
    This closes that gap: it polls each in-flight child's FunctionCall
    (non-blocking) and, on POSITIVE inline-success evidence, routes it through
    the same idempotent ``complete_job`` settle + drive path the per-job status
    poll uses.

    SUCCESS-ONLY, by design. A ``succeeded`` poll requires an inline
    ``smoke_result.status == "COMPLETED"``, which only the atomic tools emit.
    The composite pilots (bindcraft / boltzgen / pxdesign / rfantibody) take the
    webhook path and carry no inline payload, so ``poll`` reads ``failed`` for
    them even when the work succeeded (see ``_interpret_pipeline_return`` and
    ``shared.job_recovery``). We therefore NEVER fail a child from a poll here —
    ``failed`` / ``error`` / ``running`` polls are left untouched for the
    terminal webhook and the careful stuck-job recovery sweeper. The only state
    this writes is ``succeeded`` (plus a cosmetic pending→running when the
    FunctionCall is live but the first heartbeat was lost).

    Best-effort: every fault is swallowed so a poll error can never break the
    caller (a status read or the cron tick). Returns the number of children
    terminalised on this call.
    """
    client = get_service_client()
    if client is None:
        return 0
    try:
        resp = (
            client.table("tool_jobs")
            .select("id,status,modal_function_call_id")
            .eq("campaign_id", campaign_id)
            .in_("status", ["pending", "running"])
            .execute()
        )
        rows = list(getattr(resp, "data", None) or [])
    except Exception:
        logger.warning(
            "reconcile_campaign_children: child query failed for %s",
            campaign_id, exc_info=True,
        )
        return 0
    if not rows:
        return 0

    from shared.jobs import complete_job, mark_running  # noqa: PLC0415
    from gpu.modal_client import ModalClient  # noqa: PLC0415

    mc = ModalClient()
    reconciled = 0
    polled = 0
    for r in rows:
        fc_id = r.get("modal_function_call_id")
        if not fc_id:
            # Not yet submitted to Modal (row created, submit ack pending):
            # nothing to poll. The dispatcher / sweeper own that state.
            continue
        if polled >= max_poll:
            break
        polled += 1
        try:
            poll = mc.poll(str(fc_id))
        except Exception:
            logger.warning(
                "reconcile_campaign_children: poll raised for child %s",
                r.get("id"), exc_info=True,
            )
            continue
        status = poll.get("status") if isinstance(poll, dict) else None
        if status == "succeeded":
            try:
                complete_job(
                    str(r["id"]),
                    terminal_status="succeeded",
                    result=poll.get("result") or {},
                    gpu_seconds_used=poll.get("gpu_seconds_used"),
                )
                reconciled += 1
            except Exception:
                logger.warning(
                    "reconcile_campaign_children: complete_job raised for %s",
                    r.get("id"), exc_info=True,
                )
        elif status == "running" and r.get("status") == "pending":
            # FunctionCall is live but the row never advanced past pending
            # (first heartbeat lost). Anchor started_at so the stuck-job
            # sweeper measures runtime from the right point.
            try:
                mark_running(str(r["id"]))
            except Exception:
                logger.warning(
                    "reconcile_campaign_children: mark_running raised for %s",
                    r.get("id"), exc_info=True,
                )
        # failed / error: DO NOT terminalise here — a composite pilot's lost
        # webhook reads as ``failed`` even on success. Leave it to the terminal
        # webhook and the careful stuck-job recovery sweeper.
    return reconciled


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
