# Phase 2 step 3: cushioned hold sizing (all jobs, uniform)

Status: **PLAN, no code yet. Decisions final (Leo, 2026-07-04).** Parent:
`COMPUTE-CAMPAIGNS-PHASE-2-PLAN.md` section 3. Prereqs shipped: the
`tool_jobs_p90` view (migration 0020, applied) and the net-per-job ledger
display (PR #49).

## 1. Problem

The per-job hold equals the **point estimate** (`estimated_cost_for_tool` =
`min(marked_up_gpu_seconds, scaled_hard_cap)`, using p90 GPU-seconds once a tool
has 20+ runs, else the spec default). On the canary that estimate ran about 55%
of actual (rfdiffusion validates AF2 on an A100-80GB the spec under-prices), so
`actual > hold` and settle took the variance-charge branch instead of releasing
surplus. The money was correct; the ledger looked like an extra charge.

Fix: make the hold a **cushioned** amount so actual usually lands under it and
settle **releases** (clean ledger). Keep a separate point estimate for the
forecast and the single-job safety logic.

## 2. Single job vs campaign: there is no money-mechanics distinction

A campaign child IS a single job. Same `tool_jobs` table, same
`reserve_hold` -> run on Modal -> `settle_hold` path. The only marker is
`campaign_id` being set (plus `chunk_index` / `attempt`). A campaign is N
ordinary jobs: one big request chunked, fanned out, and aggregated. A 12-design
campaign chunk and a standalone 12-design job have identical cost, runaway risk,
and settlement.

- **Legitimately campaign-only (orchestration):** chunking, fan-out, aggregate
  progress, and the fund-and-drain pause/resume + fund-the-first-wave gate (you
  can pause a batch between chunks; you cannot pause one atomic container). Plus
  the product framing: single jobs carry the $1000 self-serve ceiling that
  routes big interactive asks to the Binder Pilot funnel; campaigns are the
  sanctioned self-serve-at-scale path.
- **Per-job and uniform (NO campaign_id split):** hold sizing, hard cap,
  cost-kill, settle. Same cost and risk per unit of compute.

So step 3 changes the per-job money mechanics **uniformly for every job**.

## 3. Decisions (final, uniform)

1. **Cushion = 1.5x the point estimate**, for every job's hold.
2. **Hard cap = max(tool_cap, cushion)**, for every job. For nearly all jobs the
   cushion sits under the tool's scaled cap, so nothing changes and the tool cap
   stays the runaway ceiling; only where the cushion would exceed the cap
   (boltzgen) does the cap rise to fit it. A rare underestimated run can still
   take a small variance charge above the hold up to the tool cap, made
   non-confusing by the net-per-job display. (The strict "cap every charge at
   1.5x estimate, Ranomics absorbs the rest" is a SEPARATE later billing-policy
   call that should ride with an estimate-recalibration pass; not in step 3.)
3. **Drop the cost-based mid-run kill for every job.** The customer is already
   bounded by the hold/cap, and killing a job loses the result for no customer
   benefit while they still paid the hold. The container timeout is the hard
   backstop; a stall / no-progress kill replaces cost-killing in step 4.
4. **Scope = all jobs (single and campaign), uniform.** No `campaign_id`
   branching in the money mechanics.

## 4. Mechanic

- `shared/wallet_estimates.py`:
  - `cushioned_hold_usd(user_id, tool, params)` = `1.5 x
    estimated_cost_for_tool(...)`, quantized. The point estimate stays available
    unchanged via `estimated_cost_for_tool` (forecast + display).
  - `hold_hard_cap_usd(tool, params)` = `max(compute_hard_cap(tool, params),
    cushioned_hold_usd(...))`. By construction `hold <= hard_cap`, so the SQL
    `p_hard_cap_usd` guard never spuriously refuses a well-formed hold.
- `shared/wallet.py` `reserve_hold`: gains an optional explicit `hard_cap_usd`
  argument (today it computes `compute_hard_cap` internally). The hold placed is
  `cushioned_hold_usd`; the guard/settle cap is `hold_hard_cap_usd`. Default
  callers that pass nothing keep today's behavior; the job-submit and campaign
  paths pass the cushioned pair.
- Persist BOTH numbers on the job's `_wallet`: `estimate_usd = cushioned hold`
  (what settle reconciles against, so it releases surplus in the common case)
  and `point_estimate_usd = point estimate` (what the forecast reads). Settle
  SQL is unchanged; it just reconciles against a larger, usually-sufficient hold.
- `shared/jobs.py`: remove the cost-based mid-run kill (the
  `_MID_RUN_WARN_RATIO` / `_MID_RUN_KILL_RATIO` cost path). The container timeout
  remains the backstop until the step-4 stall kill lands. Keep any warn-only
  telemetry if cheap; kill nothing on cost.
- Forecast: `plan_chunks` budget line and the create-screen number use the point
  estimate (non-binding forecast), not the cushioned hold, so the displayed
  number is not inflated 1.5x. The hold is internal.
- Single-job submit path: the same cushioned hold + `max(tool_cap, cushion)` cap
  flow through the job-submit `reserve_hold` call, uniformly.

## 5. Money-safety invariants to preserve

1. `balance_usd == SUM(wallet_transactions.amount_usd)` always.
2. A hold is refused atomically when `balance < hold` (unchanged path).
3. `hold <= hard_cap` by construction, so the SQL guard never spuriously refuses.
4. Billed per job is bounded by the hard cap; overage above it is
   `absorbed_variance`, never a user debit.
5. Settle SQL is unchanged. Only the numbers passed in (hold, hard cap) change.
6. Total spend across all jobs bounded by funded balance (hold refusal).

## 6. Tests

- `tests/test_wallet.py`: `cushioned_hold_usd` is 1.5x the point estimate;
  `hold_hard_cap_usd` = max(tool cap, cushion) (unchanged when cushion < cap,
  raised when cushion > cap, e.g. boltzgen); `reserve_hold` with an explicit
  `hard_cap_usd` binds and passes it through; a job whose actual lands under the
  cushion releases surplus (no variance charge); default callers unchanged.
- `tests/test_compute_campaigns.py`: a chunk reserves the cushioned hold; a
  boltzgen chunk (cushion > tool cap) reserves the cushion without a guard
  refusal; the forecast still uses the point estimate.
- `tests/test_jobs.py` (or the monitor's test): no cost-based kill fires; the
  container timeout path is intact.
- Full suite green; only the known env-gated `test_wallet_funnel` failures remain.

## 7. Coordination

Step 3 touches `shared/wallet.py` (`reserve_hold`) and
`shared/wallet_estimates.py`, which the daily-cap cleanup task
(`task_8a881377`) is also editing. Build step 3 in an isolated git worktree off
a fresh `origin/main` AFTER that cleanup PR lands (or rebase onto it) to avoid an
index collision. Other touch points: `shared/compute_campaigns.py`,
`shared/jobs.py`.

## 8. Sequencing note

Dropping the cost-kill in step 3 leaves the container timeout as the only
runaway backstop until step 4 adds the stall / no-progress kill. That is
acceptable (a cost-overrunning-but-progressing job SHOULD finish now that the
customer is capped by the hold), and truly stuck jobs still hit the container
timeout. If we want zero gap, step 4's stall kill can be pulled forward to land
in the same wave as step 3.

## 9. Implementation notes (build 2026-07-04)

Two refinements landed during the build; both preserve the decided outcomes.

**(a) Hard cap realized as a clamp, not a raise.** Decision 2 said
`max(tool_cap, cushion)`. Working through the settle mechanics, the hold is
implemented as `cushioned_hold_usd = min(1.5 x point_estimate, tool_cap)` (clamp
DOWN to the tool cap) rather than raising the cap to the cushion. This gives
IDENTICAL billing and absorption: the customer is capped at the tool cap either
way (settle clamps there, Ranomics absorbs above), so reserving beyond the cap
would only lock up wallet funds with no billing benefit. The clamp reserves
strictly less (better under a shared wallet) and keeps the tool cap as the one
true ceiling, so it touches NO gate: `reserve_hold`, `wallet_preflight`, and the
settle path are all unchanged (a cushion clamped to the cap always passes the
existing per-tool-cap and SQL guards, and every tool cap is <= $500 so it never
trips the $1000 self-serve ceiling). Net for boltzgen: hold clamps to the $10
tool cap (a real cushion above the then-$8.74 point estimate), not the ~$13 raw
1.5x. (2026-09-01: boltzgen's point estimate is now $6.07 after its gpu_class
was corrected to A100-40GB, so the 1.5x cushion is $9.10 and no longer clamps
to the $10 cap. The reasoning stands; the arithmetic moved.)

**(b) Step 3 split into 3a and 3b.** The cushion (3a) is independent of the
cost-kill removal (3b), and 3b has a much wider test and security-test surface
(`test_jobs.py` kill cases, `test_cancel_race`, `test_heartbeat_security`, the
safety_kill classifier routing). So 3a ships the cushion alone and 3b removes
the cost-kill as a focused follow-up. During the 3a->3b gap the cost-kill still
fires, but more leniently: the mid-run monitor still reads `estimate_usd` = the
point estimate (unchanged), so its ratio is unchanged from before this step.

**3a footprint (this PR):** `shared/wallet_estimates.py`
(`HOLD_CUSHION_MULTIPLIER`, `cushioned_hold_usd`); `shared/compute_campaigns.py`
(`child_hold_usd`, `_dispatch_chunk` reserves the cushioned hold, stores the
point estimate as `estimate_usd`); `app.py` single-job submit reserves the
cushioned hold; `tests/test_cushioned_hold.py`. No change to `shared/wallet.py`,
`shared/jobs.py`, the settle path, or any migration.
