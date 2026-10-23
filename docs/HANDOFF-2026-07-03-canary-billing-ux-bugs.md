# HANDOFF 2026-07-03 — compute-campaign canary: 3 bugs found, paused

Status: **UPDATED 2026-07-03 (later session). Bug 2 is RESOLVED as NOT a money bug** (prod ledger confirmed the settle path is correct). Bug 1 (label) and the real underlying defect (under-estimation, see below) remain open. No code changed. See the "UPDATE" section immediately below; the original Bug 2 write-up is kept for the record but is superseded.

## UPDATE 2026-07-03 (later) — Bug 2 REFUTED, real defect reframed

**Bug 2 ("hold surplus not released", the alleged HIGH-priority money bug) is closed: there is no money bug.** The original note misread a correct ledger.

Why the original read was wrong:
- `settle_hold` (SQL migrations 0017 and 0020 — the only two that ever existed in git) writes a `charge` row ONLY when `actual > estimate`, and its amount is the **variance** (`actual − estimate`), never the full actual. So a `-$1.45` charge means `actual = hold estimate ($1.75) + $1.45 = $3.20`, and `hold + charge = $3.20 = actual`. Net debit = actual. Correct.
- Independent magnitude check: rfdiffusion validates on **A100-80GB** (`$0.001028/s`); the canary's ~1896/1830 GPU-s × 1.70 markup = **$3.2–3.3/job**, which equals hold+charge — not the "$3.02" the original note inferred by *summing the charge rows* (circular: it assumed charge = actual to conclude actual = charge).
- "No release rows" is EXPECTED here, not a bug: the estimator runs low, so `actual > estimate` is the common case, so settle takes the variance-charge branch, not the release branch.

**Prod evidence (Leo ran these 2026-07-03):**
- `SELECT count(*) FROM wallet_transactions WHERE kind='hold_release';` → **105**. The release path is alive and firing in prod.
- `SELECT count(*) ... WHERE kind='hold_release' AND notes='true-up surplus released';` → **56**. That is settle's *own* surplus-release branch (distinct from cancel refunds), so surplus IS released when actual < estimate.
- Prior "bug" examples reinterpret the same way: boltzgen 06-15 held $8.74, charged $1.26 → actual = $10.00 (16% over estimate), no surplus, correct.

**The REAL defect that remains (this is what to fix):** the per-child estimate ran ~55% of actual ($1.75 vs ~$3.20), so the canary spent **$6.51 against a $4.02 "authorized budget."** The authorized figure is NOT a true ceiling — that is the actual violation of the always-overestimate policy, and it is what made a correct ledger *look* like theft. Fix is **estimator calibration** (migration 0020 adds the `tool_jobs_p90` history view; plus a cushion that raises the estimate AND the hard cap in lockstep so the clamp does not swallow it), NOT a settle change. Bug 3's "rip out the ceilings" plan stays dropped.

Re-prioritized next-move order: (1) estimator calibration so authorized budget is a real ceiling; (2) Bug 1 relabel; (3) Phase 2 for 20k-design campaigns. No money emergency gates any of them.

---

_Original investigation below (Bug 2 section superseded by the UPDATE above)._

## One-paragraph context
Compute Campaigns (Phase 1) is SHIPPED + LIVE (merge `bb9607f`, migration 0034 applied, cron `tools-hub-campaigns-tick` live). We ran the Phase-1 canary: one real rfdiffusion campaign, 24 designs, 2 sub-jobs, on Leo's own account. The canary did NOT behave the way we feared (no runaway kill, no overcharge above the authorized number). Instead it surfaced three separate issues. This note records all three, the evidence, and the recommended order to fix them.

## The canary run (evidence)
- Campaign: `2f683e2f-b402-4075-a1d2-a32ce0a1c5a1` "Canary — 6M0J RBD smoke test", rfdiffusion, pilot preset, 24 designs, 2 sub-jobs. Target = SARS-CoV-2 RBD (6M0J chain E).
- Sub-job 1 `0826c66c-1ebb-4956-b02a-9b6016bdd644`: **SUCCEEDED**, ran cleanly, 1896 "GPU-seconds", produced all 12 designs, all below quality threshold (ipTM 0.06-0.10, pLDDT 31-38, i_pAE 25-27).
- Sub-job 2 `721a9457-8e3d-4f03-97f0-d2d37ba632ef`: **SUCCEEDED**, ran cleanly, 1830 "GPU-seconds", produced all 12 designs, all below threshold (same score range).
- Both jobs' designs are present and downloadable (CSV / FASTA / PDB zip) on their job pages.
- Ledger (from `/account/wallet/transactions`): 2 HOLD rows of $1.75 at 17:53; 2 CHARGE rows of $1.45 (18:23) and $1.57 (18:25). **No HOLD RELEASE rows.** Balance $55.17 -> $48.66 = **$6.51 spent for $3.02 of actual compute.** Campaign "Authorized budget" was $4.02.

## Bug 1 — Misleading "0 / 24 delivered" (no-result UX)
**Symptom.** The campaign page (`/runs/2f683e2f...`) shows `COMPLETED`, `0 / 24 designs delivered`, `2 DONE`. Reads as "the run died and you got nothing."
**Reality.** Both sub-jobs succeeded and produced all 24 designs. They are downloadable right now. The counter is counting *passing* designs (above the quality filter), and all 24 scored below it, so it shows 0. The word "delivered" is wrong for a run that delivered 24 designs.
**Root cause (to confirm).** Campaign progress/rollup counts hits-above-threshold, not designs-produced. Sub-job status "DONE" + campaign "0 delivered" together make a clean success look like a total loss.
**Fix direction.** Either (a) relabel "delivered" -> "designs that passed filters" / "hits", and separately show "24 designs produced (view / download)", or (b) count produced designs in the "delivered" number and add a "0 passed filters" sub-line. Point users to the sub-jobs where the designs actually live.
**Files.** `templates/runs/detail.html` (campaign detail, the progress block), `templates/runs/list.html` (list rollup), and the progress computation in `shared/compute_campaigns.py` (grep for the delivered/progress counter).
**Priority.** Medium. Not money, but it makes a working product look broken and will confuse every user whose small batch yields no hits (which is most small batches).

## Bug 2 — Hold surplus not released on settle (REAL MONEY, highest priority) — SUPERSEDED, REFUTED (see UPDATE at top)
**Symptom.** When a job settles, the CHARGE (actual) posts but the original HOLD is not returned. User is debited hold + charge instead of just actual.
**Evidence.**
- Canary: held 2 x $1.75 = $3.50, charged $1.45 + $1.57 = $3.02, balance fell $6.51. Expected fall if surplus released = ~$3.02. No release rows.
- NOT campaign-specific and NOT new: prior single job boltzgen 06-15 HOLD -$8.74 -> 06-16 CHARGE -$1.26, and the $7.48 surplus never returned (17 days, no release row). Also esmfold2-design 06-04 jobs show $0.10 holds not released.
- So this is a pre-existing settle-path bug affecting ordinary single jobs too, not just campaigns.
**What to check first.**
- `shared/jobs.py` `_settle_wallet_hold_for_completed_job` (~L873) — does it call the release path, and does it release surplus when actual < hold?
- The `settle_hold` SQL function (grep `supabase/migrations` for `settle_hold`; per memory it landed in migration 0020). Memory note: ledger is a "2-row hold: hold + hold_release, pivot via hold_tx_id." Confirm the hold_release row is actually being written on the BILLED-success path.
- Rule out async timing: the boltzgen 17-day gap argues this is a real miss, not a pending release. Confirm no sweeper is expected to post it later.
**Expected correct behavior.** On settle: charge = LEAST(actual, hard_cap); release = hold - charge when hold > charge (surplus back to balance). Net balance impact = actual only.
**Caveat before calling it confirmed.** Read the settle code + the 0020 SQL before declaring a bug. The ledger strongly implies it, but the invariant `balance == SUM(ledger)` holds, so the miss is either a missing release row or a release the display filters (checked the full history view — no release rows for these, so it looks genuinely missing).
**Priority.** HIGH. Real user dollars sitting reserved and unreturned, across all tools.

## Bug 3 (design question, DEPRIORITIZED) — ceiling + mid-run kill
**Original worry (this session, pre-canary).** The mid-run cost kill fires at `max(2 x estimate, hard_cap)` (`shared/jobs.py:1198`), which could kill a legitimate expensive job and return no data; and a naive "hold at hard cap" fix would move the kill wall to 2x the cap and make Ranomics eat runaway overage.
**Why deprioritized.** The canary did NOT hit any kill. Both jobs succeeded at ~$1.45-1.57 actual, far under the $6/sub-job hard cap. The "we got nothing" was Bug 1 (label), not a kill. **Leo's decision: DROP the "rip out the ceilings" plan.** It was aimed at a problem that did not occur.
**Related finding worth keeping (verify later).** The mid-run monitor warned at $2.63 / $2.67 (screenshots) but the final billed charges were only $1.45 / $1.57. The monitor's cost basis looks like server wall-clock (`webhooks/modal.py` `_elapsed_running_seconds`), while billing uses actual GPU-seconds (lower). So the "Completed in 1896 GPU-seconds" display is likely wall-clock, and true billed GPU-time is ~1.6x lower. Implication: the current kill/warn fires on an INFLATED number. If we ever revisit the kill, fix the cost source first.
**If revisited (not now).** Keep the billing ceiling (Leo's always-overestimate policy), replace the cost-based kill with a stall/no-progress kill, anchor any hard kill at `hard_cap` (not 2x estimate), and base the monitor on true GPU-seconds not wall-clock.

## Still-open items from earlier this session (context, not blocking)
- **Always-overestimate policy** is set (memory `always-overestimate-cost`): any authorized/displayed dollar figure must be a conservative ceiling, bill actual <= authorized. NOTE the canary actually billed UNDER the authorized $4.02, so the feared overcharge did not happen here — but the policy still stands as direction.
- **Campaign authorize-at-hard-cap + relabel** (previously approved "yes", paused on an edge-case question). Re-sequence BELOW Bug 2. The edge-case answer we reached: keep the mid-run kill anchored at `hard_cap` by keeping the monitor's `estimate_usd` at the point estimate (do NOT overwrite it with the raised hold), else the kill wall moves to 2x the cap.
- **Global overestimate cushion** for single-job path (Leo chose this) — a >=1.5x multiplier in `shared/wallet_estimates.py` `estimated_cost_for_tool` before the cap clamp. Separate change, touches all live users, needs independent QC. Not started.

## Recommended next-move order
1. **Bug 2 (hold release)** — read `_settle_wallet_hold_for_completed_job` + `settle_hold` SQL, confirm the miss, fix, add a regression test asserting `balance delta == actual` (surplus released). Real money, all tools.
2. **Bug 1 (delivered label)** — relabel + surface produced designs on campaign detail/list. Cheap, high clarity win.
3. Then the paused campaign authorize-at-hard-cap relabel and the global overestimate cushion, as separate QC'd changes.
4. Bug 3 kill redesign only if a future run actually gets killed.

## Environment / gotchas
- tools-hub is a NESTED git repo. Git ops: `git -C /c/Users/lab/Documents/Claude_projects/tools-hub ...`.
- Cannot query prod Supabase directly (Leo's manual step). Verified billing via the live UI at `tools.ranomics.com/account/wallet/transactions` (Leo signed in; do NOT enter his password to sign in).
- Standing prefs: auto-commit only after independent QC, commit != push (manual), no emojis, no em/en dashes, minimal focused changes.
- Full plan: `docs/COMPUTE-CAMPAIGNS-PLAN.md`. Prior canary memory: `project_tools_hub_compute_campaigns.md` (its CANARY FINDING paragraph pre-dates this note and overstates the overcharge angle — this handoff supersedes it).
