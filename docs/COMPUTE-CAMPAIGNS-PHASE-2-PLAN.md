# Compute Campaigns Phase 2 Plan: fund-and-drain billing at scale

Status: **PLAN, no code yet** (Leo approved the model 2026-07-03). Supersedes the
estimate-and-authorize-a-ceiling model from Phase 1 and the "estimator
calibration" follow-up. Companion docs: `COMPUTE-CAMPAIGNS-PLAN.md` (Phase 1),
`HANDOFF-2026-07-03-canary-billing-ux-bugs.md` (why the old model was dropped:
Bug 2 was a misread, the real defect was that the authorized budget was not a
true ceiling).

## 1. The model shift

**Old (Phase 1):** compute a per-chunk dollar estimate, sum x1.15 into an
"authorized budget", make the user pre-authorize that number, then drain the
wallet per child. Problem: the estimate ran ~55% of actual, so the authorized
number was not a real ceiling (canary spent $6.51 against a $4.02 quote).

**New (Phase 2):** stop promising a ceiling. The **wallet balance is the
ceiling**, and it is physically impossible to exceed because a campaign pauses
the moment the next chunk cannot be funded. Flow:

1. User funds their wallet.
2. Campaign starts if the wallet can cover the first wave.
3. Each chunk draws a real hold, runs, settles at actual (surplus released).
4. When the balance cannot cover the next chunk, the campaign **pauses** and
   emails the user: top up to continue, or download the designs already
   produced (partial results are useful for design campaigns).

This satisfies the always-overestimate policy by construction: billed spend can
never exceed the money the user put in. Any number we show on the create screen
is a **non-binding forecast** ("roughly $X, you only pay for compute that runs,
we pause when your wallet runs low"), which does not need to be conservative or
dollar-accurate.

## 2. Remove the $200/day wallet cap

The `daily_spend_cap_usd` per-wallet cap ($200 default) is removed as a spend
gate for all jobs (single and campaign).

**Why it is safe to remove.** The wallet is **prepaid**. `reserve_hold` refuses
any hold when `balance < hold` under a row lock, so the balance can never go
negative and total spend can never exceed funded balance. The daily cap never
protected against overspend-beyond-wallet (impossible); it only rate-limited
spend *within* already-funded money. For metered campaign compute that rate
limit is exactly what we do not want.

**Where the real protection now lives:**
- **Prepaid balance** bounds total spend (can only spend what is funded).
- **Per-job / per-chunk hard cap** (`compute_hard_cap`) bounds any single
  unit; `settle_hold` clamps actual at the cap and Ranomics absorbs overage.
- **Auto-reload monthly cap** ($1000 default) bounds *new money* per month.
  This is the correct place for a velocity brake, because that is where card
  charges happen; draining a prepaid balance charges nothing new.
- **`wallet_frozen`** still blocks all activity on a chargeback/dispute.
- **Large-top-up verification** (see section 7) moves the KYC gate to the
  money-in side.

**Code surface:**
- New migration redefines `try_hold_for_job` to drop the daily-cap block
  (0020 lines 89-106). Keep the frozen check, the ledger-authoritative balance
  check, and the `p_hard_cap_usd` guard.
- `shared/wallet.py` `wallet_preflight`: remove the daily-cap branch and the
  `DEFAULT_DAILY_CAP_USD` fallback (wallet.py:472). Note the Python fallback
  currently means a NULL column still enforces $200, so nulling the column
  alone is not enough; the Python check must go too.
- Blast radius: single-job users also lose the $200/day cap. Intended.

## 3. Per-chunk hold sizing + ledger clarity

Two independent changes that together make the wallet history un-confusing.

**Hold size = cushioned estimate, clamped to the per-chunk hard cap.**
Recommend `hold = min( max(1.5 x point_estimate, p90_estimate), per_chunk_hard_cap )`.
- `p90_estimate` comes from the `tool_jobs_p90` view (created by migration
  0020; apply it). Falls back to the point estimate when a tool has < 20 runs.
- This usually covers actual, so settle usually **releases** (clean ledger),
  without grossly over-reserving the wallet the way a pure hard-cap hold would
  (which matters under multi-campaign, section 6).
- The estimate is now **internal** (reservation size + chunk sizing), never a
  customer promise, so it only has to be "usually enough", not conservative.

**Ledger display fix (kills the confusion for good).** The thing that looked
like double-charging was two scary rows (a hold and a variance charge) for one
job. Fix the wallet UI to show a **net per-job cost** line that nets
`hold + charge` or `hold - release` into a single "cost for this job: $X"
figure, with the raw ledger rows collapsible underneath. This makes any
residual variance charge non-confusing regardless of hold sizing.
- Files: `templates/wallet/transactions.html`, and the transaction assembly in
  the wallet route (`app.py` around the `/account/wallet/transactions` handler).

**Settle path is unchanged** (it already releases surplus / clamps at cap /
absorbs overage). No settle change is part of Phase 2.

## 4. Fund-to-start + pause/resume state machine

**Preauth simplifies.** `campaign_preauth` drops the full-budget authorization.
New gate: `not frozen` AND `balance >= first_wave_worst_case`, where
`first_wave_worst_case = concurrency_target x per_chunk_hold`. This launches the
campaign meaningfully instead of pausing on chunk one. No per-campaign
"authorized budget" is stored or shown as a ceiling.

**Build status (2026-07-04): SHIPPED as step 5, app-layer only, no migration.**
`campaign_preauth(user_id, budget_usd, first_wave_usd=None)` now gates the
balance on `first_wave_usd` (new helper `first_wave_hold_usd(plan)` =
`min(total_subjobs, DEFAULT_CONCURRENCY_TARGET) x child_hold_usd(tool,
chunk_size)`), not the full budget; `PreauthResult.required_usd` carries it. The
create + estimate routes pass it; the estimate JSON adds `first_wave_usd`; the
create screen is reframed to a non-binding forecast ("Estimated total", "Enough
to start", "you pay only for compute that runs, we pause if your balance runs
low") and the "I authorize up to $X" ceiling checkbox is gone. Paired with step
4a's pause/resume, a campaign now starts on a first-wave fund and drains.
**Interim (NOT retired this step):** `budget_usd` is still computed as a forecast
and still feeds the verification (`> $5k`) and velocity (`$25k/day`) gates, so
KYC coverage is unchanged and there is no gap. Retiring those two gates and
re-anchoring verification to the top-up side (section 7) is a separate follow-up,
because removing the campaign-path verification before the top-up-path check
exists would open a KYC hole.

**New campaign state `paused_insufficient_funds`** (migration alters the 0034
CHECK constraint; current states: draft, funded, running, completing,
completed, completed_with_failures, failed, cancelled).

**Driver changes (`drive_campaign` / `_dispatch_chunk`):**
- Today a balance-refused hold returns `"skipped"` and the chunk silently
  retries forever (this is the "no stall reaper" gap). Instead: distinguish a
  **balance refusal** from a transient skip. On balance refusal with
  undispatched chunks remaining, transition the campaign to
  `paused_insufficient_funds` and send the pause email **once**.
- `_maybe_finalize` must treat `paused_insufficient_funds` as non-terminal (do
  not finalize; wait for funds), but it must finalize normally once all
  dispatched chunks reach terminal AND no undispatched chunks remain.

**Resume:**
- The cron (`campaigns:tick`) re-drives `paused_insufficient_funds` campaigns.
  When the balance can cover a chunk hold again, dispatch resumes and the state
  returns to `running`.
- If the user has auto-reload enabled, a top-up fires automatically and the
  campaign resumes hands-free (respecting the monthly cap).

**Stop / download:**
- Completed sub-jobs' designs stay downloadable on a paused campaign.
- A "finalize now" action lets the user close a paused campaign and keep
  partials (state -> `completed_with_failures` or a new `completed_partial`).

**TTL:** auto-finalize a campaign left in `paused_insufficient_funds` for N days
(e.g. 14) as partial-complete, keeping all produced designs. Prevents zombie
campaigns.

**In-flight safety at pause:** only *undispatched* chunks pause. Chunks already
running settle normally (release surplus). No stranded or partial money.

**Build status (2026-07-04): split into 4a (shipped) + 4b (deferred), like
3a/3b.** 4a is the core pause/resume loop and is APP-LAYER ONLY (the
`paused_insufficient_funds` status is already an allowed value from migration
0035, applied to prod, so 4a needs no migration and deploys on merge):
- `_dispatch_chunk` now classifies a hold refusal: a balance shortfall returns
  `"insufficient_funds"` (advisory read after reserve_hold's authoritative
  atomic refusal), a transient/duplicate/cap refusal still returns `"skipped"`.
- `drive_campaign` runs the state machine: an `insufficient_funds` refusal with
  undispatched chunks remaining pauses the campaign via an atomic CAS
  (`funded|running -> paused_insufficient_funds`); only the CAS winner emails, so
  the pause email is sent AT MOST ONCE even under concurrent inline-hook + cron
  drives (at-most-once, not exactly-once: the CAS commits paused before the send,
  so a transient Resend failure drops that email and is not retried — the UI
  banner is the backstop). A later pass that can fund a chunk resumes via CAS
  (`funded|paused -> running`). Pause takes precedence over resume in a pass that
  launches some chunks then runs dry. `_maybe_finalize` is unchanged and already
  refuses to finalize while undispatched chunks remain (len(children) < total).
- `cron/tick_campaigns.py` `_ACTIVE_STATES` gains `paused_insufficient_funds` so
  the tick re-drives (and resumes) a paused campaign; nothing else triggers a
  paused campaign since no in-flight child completes to fire the inline hook.
- `shared/email.py` `send_campaign_paused_email` + `templates/email/send_campaign_paused.html`;
  `/runs/<id>` status `payload["paused"]` is the authoritative explicit state
  (the old nothing-in-flight heuristic is dropped so a transient dispatch blip no
  longer shows a false "add funds" prompt); the detail-page banner links to top
  up and the Cancel button now also shows for a paused campaign.

**4b (deferred), needs a `paused_at` migration:** the 14-day pause TTL
auto-finalize, proactive auto-reload-on-pause, and DURABLE pause-email delivery
(a `pause_notified_at` flag + cron retry so a dropped Resend send is re-sent
rather than lost). All want a new column, so they ride the same migration.
Deferred so 4a stays migration-free and ships immediately. Until 4b, a
never-funded paused campaign is not a hard zombie: it resumes the instant funds
arrive and otherwise costs one no-op cron drive per tick.

**Known gap (4a):** a *frozen* wallet (chargeback/dispute) refuses holds but
reads a sufficient balance, so it classifies as a transient `"skipped"` and
spins harmlessly rather than pausing. No money risk (frozen blocks all holds);
it resolves when the wallet unfreezes. A `paused_frozen` state is out of scope.

## 5. Scale: raise MAX_SUBJOBS + async dispatch

- **Raise `MAX_SUBJOBS_PER_CAMPAIGN`** from 20. With the daily cap gone and the
  wallet-drain pause as the real limit, set it high (e.g. 2000) with a sane
  request-size sanity max. 20k designs = 400 (boltzgen) / 1667 (rfdiffusion) /
  6667 (bindcraft) sub-jobs at current chunk sizes.
- **bindcraft needs a bigger-container campaign preset.** Its 3/chunk sizing is
  the worst-scaling by far (6667 sub-jobs for 20k). A campaign-only preset with
  a larger container -> bigger chunks -> far fewer sub-jobs. Without it,
  bindcraft at 20k is impractical even with the cap raised.
- **Async first-wave dispatch.** Do NOT dispatch the first wave synchronously in
  `POST /runs` (it would time out at high concurrency: each chunk is ~2 Modal
  calls + 3 Supabase round-trips). Create + fund the campaign, return
  immediately, and let the cron/background driver do all dispatch. Preserve the
  `UNIQUE(campaign_id, chunk_index, attempt)` + CAS-launch idempotency and add a
  per-campaign advisory lock so overlapping cron ticks cannot double-dispatch.
- **Raise `DEFAULT_CONCURRENCY_TARGET`** from 8, bounded by the global per-user
  in-flight cap (section 6).

**Build status (2026-07-05): SHIPPED as step 6a, app-layer only, no migration.**
`MAX_SUBJOBS_PER_CAMPAIGN` 20 -> 2000 (also the request-size sanity bound);
`DEFAULT_CONCURRENCY_TARGET` 8 -> 16; `GLOBAL_USER_INFLIGHT_CAP` = 32 enforced in
the admission loop via `_user_inflight_subjobs(user_id)` (counts pending+running
campaign sub-jobs across all the user's campaigns; a soft load guard, not a spend
guard); first-wave dispatch is now async via `drive_campaign_async` (a named
daemon thread, the house pattern from webhooks/events/email) with the 5-min cron
as the reliable backstop. `POST /runs` returns immediately. The per-campaign
advisory lock is DEFERRED to 6b (needs an RPC migration; the existing UNIQUE +
CAS-launch already makes double-dispatch impossible, so the lock is only a
duplicate-work optimization, not a correctness requirement). bindcraft's
bigger-container preset stays step 8.

### 5.1 Large-N efficiency + cap lift (SHIPPED 2026-07-05, app-layer, no migration)

Design principle (Leo correction): campaigns are WALLET-BOUNDED and
SIZE-AGNOSTIC. "20k" was only a reference target, never a ceiling; the size must
not matter. The blocker was engine cost: the old driver loaded EVERY sub-job row
on each tick (`_campaign_children` + a linear `range(total_subjobs)` scan for the
next chunk), and the inline hook drives on every child completion, so a big
campaign was O(N^2).

The driver is now O(1) per tick, independent of N:
- Chunks dispatch lowest-index-first and the frontier advances only when a row
  is created, so the rows are always a CONTIGUOUS PREFIX `[0, dispatched)`. The
  next chunk to launch is therefore exactly `dispatched_count`, found with an
  indexed `COUNT` (`_count_children`, a head+exact count, no row transfer), not
  an all-rows load.
- A "skipped" (transient) refusal now BREAKS the pass at the frontier (retry the
  same index next drive) instead of skipping past it, so no holes ever form. A
  "duplicate" (a concurrent driver claimed the frontier index) resyncs the
  frontier from the count: it advances only for a real duplicate, so a transient
  create failure is retried, never skipped. A per-pass attempt bound guarantees
  the loop terminates under contention.
- `_maybe_finalize` is count-based too (`dispatched == total` AND
  `in_flight == 0`), no all-rows load.
- `_dispatch_chunk` gains a distinct `"duplicate"` return (create_job returns
  None on a UNIQUE violation OR a transient error; the frontier-advance test
  distinguishes them). Removed the now-dead `_campaign_children` / `_tally` /
  `_TERMINAL_CHILD`.

Cap lifted: `MAX_SUBJOBS_PER_CAMPAIGN` 2000 -> 50,000, reframed as a runaway
guard (a typo of a billion designs), not a product ceiling. 50k sub-jobs is
~2.5M designs (boltzgen) / 600k (rfdiffusion). Raise freely if ever needed.

Not converted (follow-up): `get_progress_counts` (the UI status poll) still loads
rows; it is O(N) per poll, not the O(N^2) driver problem, and converting it needs
the second test fake reworked. Fine for now; convert if extreme-N campaigns are
actively watched.

## 6. Multi-campaign edge case (one boltzgen + one rfdiffusion at once)

The scenario: a user runs a boltzgen campaign and an rfdiffusion campaign
simultaneously. Everything draws from **one shared wallet balance**.

**This mostly falls out for free, because holds reduce `balance_usd`
immediately and atomically.**
- `try_hold_for_job` locks the wallet row `FOR UPDATE`, so two concurrent hold
  attempts (from the two campaigns' drivers) serialize. Neither can spend the
  same dollars twice. **No money-safety issue.**
- The second campaign's start check and every subsequent chunk hold read the
  **live** balance, which already reflects the first campaign's outstanding
  holds. So cross-campaign budgeting needs no special accounting; the shared
  balance is self-consistent.

**Concrete walkthrough ($100 wallet):**
1. boltzgen chunks hold ~$10 worst-case; at concurrency 8 that reserves up to
   ~$80.
2. rfdiffusion chunks hold ~$6; its first wave wants up to ~$48.
3. Combined first waves want ~$128 > $100, so once the balance is exhausted the
   next chunk's hold is refused and **that** campaign pauses. First-come wins at
   the hold level.
4. As chunks settle (release surplus), balance frees up and paused chunks
   resume. Both campaigns make progress, sharing the $100, churning as money
   frees.
5. Top up to $300 and both run at full concurrency.

This is coherent and money-safe. Two things to add so it behaves well:

**(a) Fairness: interleave dispatch across a user's active campaigns.** If the
cron always drives boltzgen before rfdiffusion, boltzgen could grab the balance
every tick and starve rfdiffusion indefinitely. In the cron tick, iterate a
user's active campaigns **round-robin** and cap per-tick dispatch per campaign,
so each gets a turn at the shared balance. Neither tool permanently starves the
other. (Per-campaign priority, "finish rfdiffusion first", is a Phase 3 nicety,
not Phase 2.)

**(b) Global per-user in-flight cap (infra, not money).** With the daily cap
gone, a user with a large balance running two campaigns could put a very large
number of sub-jobs in flight at once, hammering Modal and starving other users.
Add a cap on **total in-flight sub-jobs across all of a user's campaigns**
(e.g. 32), independent of the wallet. This is a fairness/load guard, not a spend
guard.

**Wallet UI:** show a "reserved by N running campaigns: $X" line so a user who
sees a low available balance understands it is their own in-flight holds, not a
charge. Pairs with the net-per-job-cost display from section 3.

**Pause/resume is global by balance:** low funds pause whichever campaign hits
the wall next; a single top-up can resume both. The user is never billed beyond
the shared balance across any number of concurrent campaigns.

## 7. What we retire / what we keep

**Retire:**
- The per-campaign "authorized budget" concept and its display as a ceiling.
- Estimator-calibration-as-a-conservative-ceiling (the estimate is now internal
  and only needs to be "usually enough"). NOTE: calibration as an *accuracy*
  improvement (learning the estimate from real runs) is still worth doing later,
  as its own backlog piece; see section 11. That is different from calibration as
  a customer-facing ceiling, which is what we retire here.
- The `$25,000/day` campaign velocity cap (`DAILY_CAMPAIGN_CAP_USD`) and the
  `$5,000` `VERIFICATION_THRESHOLD_USD` gate, both of which were hooks on the
  authorized-budget number we are removing.

**Keep / re-anchor:**
- A **non-binding forecast range** on the create screen (decision aid, not a
  promise).
- **Verification/KYC moves to the money-in side:** trigger verification on
  large **top-ups** (a big single card charge or a fast-growing balance),
  rather than on campaign size. This keeps fraud/AML coverage where new money
  actually enters.
- `wallet_frozen`, the auto-reload monthly cap, and the per-chunk hard cap all
  stay.

## 8. Money-safety invariants (mandatory review checklist)

Any implementation must preserve all of these; an independent reviewer should
verify each before merge:
1. `balance_usd == SUM(wallet_transactions.amount_usd)` for every user, always.
2. A hold is refused atomically when `balance < hold` (row lock held).
3. Every chunk's billed cost is bounded by its per-chunk hard cap.
4. Total user spend across all campaigns and jobs is bounded by funded balance
   (no path debits below zero; overage above cap is `absorbed_variance`, not a
   user debit).
5. Removing the daily cap must NOT remove the frozen check, the balance check,
   or the hard-cap guard from `try_hold_for_job`.
6. Dispatch is idempotent under overlapping cron ticks (UNIQUE + CAS + advisory
   lock); a retry/timeout cannot double-spawn or double-hold.
7. Pausing settles nothing prematurely: running chunks settle normally, only
   undispatched chunks pause.

## 9. Build order (suggested)

1. ~~Apply migration 0020 to prod (idempotent; adds `tool_jobs_p90`, guarantees
   the releasing/idempotent settle is live).~~ **DONE 2026-07-03** (Leo applied +
   verified in the prod Supabase SQL editor: corrected `wallet_30d_spend` view
   live with non-zero `spent_usd_30d`, `tool_jobs_p90` present, releasing settle
   confirmed by 56 `true-up surplus released` ledger rows).
2. Migration: drop the daily-cap block from `try_hold_for_job`; add the
   `paused_insufficient_funds` state to the campaign CHECK. Remove the Python
   daily-cap check.
3. Hold-sizing change (cushioned estimate clamped to hard cap) + ledger
   net-cost display fix.
4. Pause/resume state machine + pause email + auto-reload resume + TTL.
   **4a (pause/resume + email) SHIPPED 2026-07-04, app-layer only (see the
   Build status note under section 4). 4b (14-day TTL + proactive
   auto-reload-on-pause) DEFERRED, needs a `paused_at` migration.**
5. Preauth simplification (fund-the-first-wave gate) + non-binding forecast on
   the create screen. **SHIPPED 2026-07-04, app-layer only (see the Build status
   note under section 4).** DEFERRED (own follow-up): retire the
   velocity/verification gates and re-anchor verification to top-ups (kept
   interim to avoid a KYC gap).
6. Raise MAX_SUBJOBS + DEFAULT_CONCURRENCY_TARGET; add the global per-user
   in-flight cap; async first-wave dispatch + per-campaign advisory lock.
   **6a (MAX_SUBJOBS 2000 + concurrency 16 + in-flight cap 32 + async dispatch)
   SHIPPED 2026-07-05, app-layer only (see the Build status note under section
   5). 6b (per-campaign advisory lock) DEFERRED, needs an RPC migration.**
7. Round-robin cross-campaign dispatch fairness in the cron.
8. bindcraft bigger-container campaign preset.
9. Bug 1 relabel ("0 / N delivered" -> "N designs produced", surface produced
   designs) can land any time; independent of the above.

## 10. Decisions (locked 2026-07-04)

- **"Enough to start" threshold:** FUND THE FIRST WAVE. A campaign starts once
  the wallet covers the first concurrency wave (`DEFAULT_CONCURRENCY_TARGET`,
  raised 8 -> 16 in step 6a). It enters `paused_insufficient_funds` only if a
  later wave outruns the balance; a top-up resumes it.
- **Hold sizing:** CUSHIONED ESTIMATE CLAMPED TO HARD CAP. Reserve a cushioned
  per-child estimate, raising the estimate AND the hard cap in lockstep so the
  `min(marked_up, cap)` clamp does not swallow the cushion; never the full cap.
  The net-per-job ledger display (shipped in PR #49) keeps any true-up
  transparent, so the tighter reservation is safe on a shared wallet.
- **Pause TTL:** 14 DAYS. A campaign starved for funds auto-finalizes as partial
  after 14 days paused (surfaces produced designs, releases outstanding holds).
- **Global per-user in-flight sub-job cap:** 32 (about 2 campaigns at the default
  concurrency of 16, shipped in step 6a). Round-robin fairness across a user's
  active campaigns under this cap (step 7).
- **Verification (KYC) re-anchor:** a SINGLE TOP-UP of >= $5k triggers
  verification. Reuse the old $5k threshold, now on wallet top-up size (where the
  money enters) since campaign size no longer gates. Move
  `VERIFICATION_THRESHOLD_USD` from the campaign path to the top-up path.

## 11. Backlog: data-driven caps + estimate calibration (NOT Phase 2)

This is a follow-up piece, not a Phase 2 step. It replaces hand-set per-tool
numbers with learned ones. Land it after step 3a, once more tools cross the
20-run p90 threshold. Step 3a (the cushion) does not depend on it.

**Why it is only cosmetic/margin, never a safety issue.** The per-tool numbers
in `TOOL_SPECS` (`expected_gpu_seconds`, `base_hard_cap_usd`, `absolute_cap_usd`,
`baseline`) are hand-set. They drive the point estimate and the hard cap. They
are NOT the money safety net. The prepaid wallet is: a hold is refused when
`balance < hold`, and settle bills the real actual clamped at the cap. So a wrong
hand-set number can never cause a customer to overspend. It only costs margin or
ledger cosmetics. Concretely, each way a number can be wrong is bounded:
- Estimate too LOW: settle takes a small variance charge (actual > hold).
  Cosmetic, already softened by the net-per-job display (PR #49) and the step-3a
  cushion.
- Estimate too HIGH: the hold over-reserves the wallet. Matters only under a
  shared wallet (section 6 churn); it frees on settle.
- Cap too LOW: Ranomics absorbs the overage (`absorbed_variance`). A margin cost
  to us, never a customer debit.
- Cap too HIGH: a single unit's runaway bound is weaker, but total spend is still
  bounded by the funded balance.

**The cleaner design (when the data is there):**
- LEARN the estimate from real runs. `tool_jobs_p90` (migration 0020) already
  gives per-tool p90 GPU-seconds once a tool has 20+ runs, and the point estimate
  already uses it there. Extend coverage as tools accumulate runs, so fewer tools
  fall back to the hand-set `expected_gpu_seconds`.
- DERIVE the cap from the learned estimate: `hard_cap = min(k x learned_estimate,
  absolute_backstop)` (e.g. k = 3). This collapses two of the three hand-set cap
  numbers into one learned number plus a single backstop.
- KEEP one absolute backstop (per tool or one global) as a deliberate business
  ceiling. Today's hand-set cap doubles as policy ("we do not want one self-serve
  unit to bill more than $X"); a purely cost-tracking cap loses that lever, and
  the backstop preserves it.

**Trade-off.** A data-driven cap tracks cost, not policy, so it can drift up as a
tool gets more expensive; the absolute backstop guards against that. Net: three
hand-set numbers per tool shrink to roughly one (the backstop), and the estimate
self-corrects as usage accrues.

**Sequencing.** Do this as its own pass once enough tools have 20+ runs so the
learned p90 is trustworthy. Until then the hand-set numbers are fine, because the
wallet, not the numbers, is the safety net.
