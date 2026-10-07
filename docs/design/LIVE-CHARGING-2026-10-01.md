# Live charging: design note (phase 1) and phase 2 decisions

- **Base commit:** `cc85754d`.
- **Status:** Sections 0-7 are the phase 1 note as written for Leo's approval. Phase 1 made no product code, migration, paid run or settings change. Phase 2 is built on it: see "Decisions (Leo, 2026-10-05)" at the end. On 2026-10-06 Leo replaced the debit mechanism with option (ii): see "Option (ii) build plan" at the end. Option (ii) removed some option (i′) code that the earlier sections cite by name (`_reserve_live_hold`, `stop_wallet_limited_jobs` and their tests); those names exist at commit `3bd13826`.
- **Decision being designed (Leo, 2026-10-01):**
  - A paid run starts whenever the balance is above $0.
  - The cost comes off the balance as the run goes.
  - At $0 the run stops, keeps its finished designs, and the customer gets a top-up email.
  - The per-tool hard caps stay as a hidden backstop.
  - Ranomics absorbs a small overshoot. The balance never goes negative.
  - Customers see no numbers before a run.
- **Citations:**
  - Line citations are against `cc85754d` unless they name the sibling repo `llm-proteinDesigner`.
  - Tool-pipeline citations come from a read-only census. Every line marked *(re-read)* was opened again for this note. The rest are the census's citations and were spot-checked by `grep -c "send_heartbeat("` and `sed` on the subprocess calls.

---

## 0. Summary

**Recommendation: option (i′).** It needs no migration and changes about four functions.

1. **Submit.**
   - Every paid single-job run reserves `min(balance, per-tool cap)` as its hold. Phase 2 enables this only for boltz2 on every preset and for af2, colabfold and esmfold on the batch preset (`shared/wallet_guard.py::_LIVE_ALL_PRESETS`, `::_LIVE_BATCH_ONLY`).
   - The run is refused only when the balance is ≤ $0, plus the existing frozen, ceiling, cap and #398 refusals. Phase 2 adds one more: `hold_failed` when two hold attempts both return no hold (`shared/wallet_guard.py::_reserve_live_hold`).
2. **Stop.** A cron check runs about once a minute (phase 2 found it runs every 5 minutes; see "§7 item 10: the cron" below). It stops a run when both hold:
   - the run's metered cost has reached its hold;
   - the hold was limited by the balance (hold < cap).
3. **Finish.** The stopped run is finished as a succeeded, partial run, built from the designs that already reached Storage.
4. **Settle.** The existing settle clamp and absorbed-variance branch keep the balance at or above $0 (`supabase/migrations/0020_wallet_corrections.sql:188`, `:218`).

**Two findings change what Leo was promised.**

- **F1. For most tools, a stop keeps nothing.**
  - Most tools write and upload designs only after one long subprocess exits. This includes proteina, bindcraft, boltzgen, iggm, opendde, esmfold2-design and mpnn.
  - A hub-side stop keeps finished designs only for:
    - the af2, colabfold and esmfold batch tiers, which upload per design;
    - boltz2, which uploads per chunk of 10.
  - Keeping in-flight designs for the other tools needs a change inside each tool's container (phase 2b, §6). Each change means an image rebuild and a paid GPU test.
- **F2. Heartbeats cannot drive the stop.**
  - Several tools are silent for 1 to 1.5 hours, and two tools never send one.
  - A heartbeat-driven stop would overshoot by up to about $22 on esmfold2-design.
  - A cron-driven stop bounds the overshoot to one tick: about $0.37 on an H100 at a 90 s tick (§2).

---

## 1. Partial results on stop

### 1.1 Today a cancelled job keeps nothing

| Step | What happens | Evidence |
|---|---|---|
| Cancel | `cancel_job` calls `ModalClient.cancel`, then `mark_cancelled`, which writes status, error and failure_class and **no `result`** | `shared/jobs.py:1803-1818`, `:1745-1769` |
| Page | Results render only for `status == 'succeeded' and job.result` | `templates/job_detail.html:311-312` |
| Exports | CSV, FASTA and ZIP read `page_ordered_records(job.tool, job.result)`. They have no status gate, but a cancelled row has no result | `blueprints/jobs.py:1497-1521`, `:1524-1554`, `:1757+` |
| Late container result | A terminal webhook arriving after the job is terminal is dropped as `already_terminal` | `webhooks/modal.py:134-142` |
| Live partials | Heartbeat candidates sit in `inputs._partial_candidates` (capped at 1000). Only the live poll JSON shows them | `webhooks/modal.py:561-600`, `blueprints/jobs.py:988` |

So the stop path must **write a `result`**.

### 1.2 What the stop path writes

The stop path reuses the stuck-job recovery builder `shared/job_recovery.py::reconstruct` (`:151-187`) and then calls:

```
complete_job(job_id, terminal_status="succeeded",
             result={"candidates": reconstruct(job), "partial": True,
                     "stop_reason": "wallet_empty"},
             gpu_seconds_used=<elapsed>)
```

What `reconstruct` does:

- It keeps the heartbeat partials whose file is present in the Storage listing `{user_id}/{job_id}/designs`.
- Otherwise it lists the Storage files themselves, with no scores.
- It returns `[]` when nothing is there.

Why `succeeded` with `partial`, and not `cancelled`:

- **Page and exports work unchanged** (`templates/job_detail.html:311`, `blueprints/jobs.py:1497-1554`).
- **Billing.** `classify_terminal_state` maps `succeeded` to `succeeded`, or to `completed_no_yield` when there are no candidates. Both are billed classes (`shared/jobs.py:752-798`).
- **Email.** The completion email already covers `succeeded` (`shared/jobs.py:2477-2501`). It already renders `run_notices` (`shared/email.py:218`, `templates/email/job_complete.html:148`).
- **Notice.** `run_notices.partial_line` already handles `succeeded` plus `result.partial` (`shared/run_notices.py:58-87`). It needs one more branch for `stop_reason`.
- **Concurrency.** `complete_job` is a compare-and-swap no-op on a job that is already terminal (`shared/jobs.py:1898-1915`, docstring). A container result that wins the race simply wins.

The **alternative** keeps `cancelled`, writes a result, and widens the template gate. That would also rescue partials on a *user* cancel. However, it needs a cancelled branch in the email, the notices and the exports copy. It is not recommended for v1.

### 1.3 What each tool keeps if stopped by the hub alone (no container change)

| Tool | Designs uploaded mid-run? | Kept on stop | Evidence |
|---|---|---|---|
| af2, colabfold (batch tier) | Yes, per design, as each record's rank_001 files appear | Finished designs | census: `tools/af2/run_pipeline.py:1344-1349`, `tools/colabfold/run_pipeline.py:1382-1387` |
| af2, colabfold (single tier) | No. A single fold sends no heartbeat | Nothing | census: `tools/af2/run_pipeline.py:1080`, `:617-630` (re-read: `timeout=1740`) |
| esmfold (batch / single) | Batch: per design. Single: no | Batch: finished. Single: nothing | census: `tools/esmfold/run_pipeline.py:1100-1105`, `:912` |
| boltz2 | Per chunk of `FOLD_CHUNK = 10` (re-read `tools/boltz2/run_pipeline.py:120`) | Completed chunks | census: `:784`, `:896-901` |
| iggm | No. All samples run in one `design.py` with no timeout (re-read `tools/iggm/run_pipeline.py:451-453`) | Nothing | census: `:598-601` comes after it |
| opendde | No. One subprocess with no timeout (re-read `tools/opendde/run_pipeline.py:418`) | Nothing | census: `:543-548` comes after it |
| proteina | No. Designs are parsed and uploaded after the search subprocess exits (re-read `tools/proteina/run_pipeline.py:4278`, `:4289`, `:4651`) | Nothing | The census cited these lines under `modal_app.py`; they are in `run_pipeline.py` |
| esmfold2-design | No heartbeats at all (re-read: 0 `send_heartbeat(` calls) | Nothing | census: `tools/esmfold2_design/run_pipeline.py:13`, `:100` |
| mpnn | No heartbeats at all (re-read: 0 calls) | Nothing | re-read `tools/mpnn/run_pipeline.py:657-662` |
| bindcraft | No. Candidates are uploaded after the 14400 s subprocess (re-read `llm-proteinDesigner/docker/bindcraft/run_pipeline.py:1418-1426`, `:1520-1545`) | Nothing | |
| boltzgen | No. Candidates are uploaded after the 6600 s subprocess (re-read `llm-proteinDesigner/docker/boltzgen/run_pipeline.py:1926-1952`, `:2070-2084`) | Nothing | |
| rfdiffusion, rfantibody, pxdesign | **UNVERIFIED.** No pipeline in this repo; `tools/<tool>/` holds only `meta.py` and an example (re-read `ls`) | **UNVERIFIED** | Board item 6a records that rfdiffusion partials carry no `pdb_key`, so `reconstruct` would fall back to the Storage listing |

Two more limits:

- **CSV sequences.** No longer a limit. The partial schema keeps no sequence field (`webhooks/modal.py::_sanitize_candidate`), but FASTA and the scores CSV both read sequences back from the stored structure: `shared/exports.py::candidates_to_fasta`, and `::candidate_table` through `::_add_chain_columns`, both call `::structure_chain_sequences`. #405 and #412, which brought this to the CSV, are merged.
- **"Finished designs" means designs whose files reached Storage.** A design still being written when the container is killed is lost.

---

## 2. Heartbeat census and worst overshoot

### 2.1 Facts common to every tool

- **No tool sends GPU-seconds.** The heartbeat body is `job_id, stage, designs_completed, designs_total`, plus `new_candidate` and `job_token` only when a candidate rides along (census: `tools/boltz2/run_pipeline.py:205-213`).
- **The hub meters wall-clock time** since `started_at` (`webhooks/modal.py:375-395`).
- **`started_at` is set by the first heartbeat** (`webhooks/modal.py:321-325`) **or by a page poll** that sees Modal running (`blueprints/jobs.py:977-978`). A tool that never sends a heartbeat stays `pending`, with no `started_at`, until someone opens its page.
- **`gpu_seconds_used` has one writer on a running job:** `mid_run_monitor_check` (`shared/jobs.py:2371`). It is called only from heartbeats (`webhooks/modal.py:375-377`). See adjacent finding A1.
- **Overshoot rates.** Customer-dollars per second = `GPU_USD_PER_SECOND × 1.70` (`shared/wallet.py:181-189`, `:74`). Ranomics' real cost is that figure divided by 1.70.

### 2.2 Per tool: the longest silent stretch, and the overshoot if heartbeats drove the stop

| Tool | GPU (`wallet_estimates` TOOL_SPECS) | Heartbeat pattern | Longest silent stretch | Overshoot at that stretch (customer $ / Ranomics raw $) |
|---|---|---|---|---|
| mpnn | A10G | none | whole run, ≤ 540 s (`tools/mpnn/run_pipeline.py:662`) | $0.19 / $0.11 |
| af2 single | A100-80GB | none on the single tier | ≤ 1740 s fold | $3.04 / $1.79 |
| af2 batch | A100-80GB | per stage, then per design | up to the first record. The cold start is recorded as > 18 min (census `tools/af2/modal_app.py:46-48`); the bound is 14000 s | ≥ $1.89, bound $24.47 |
| colabfold single / batch | A100-40GB | as af2 | 1740 s / up to the first record (bound 14000 s) | $2.11 / bound $16.99 |
| esmfold single | A100-40GB | none on the single tier | ≤ Modal timeout of 3600 s | $4.37 |
| esmfold batch | A100-40GB | first beat before the weights load, then per design | weight load (about 30 s, per its docstring) plus the first fold | small, UNVERIFIED |
| esmfold2-design | H100 | **none** | whole run, ≤ 5370 s (census `tools/esmfold2_design/modal_app.py:459`) | **$22.06 / $12.98** |
| boltz2 | A100-40GB | per stage, chunk and design | one chunk subprocess with no timeout, ≤ 3570 s wrapper bound | $4.33 / $2.55 |
| iggm | A100-40GB | per stage and design | the whole `design.py`, ≤ 3570 s | $4.33 / $2.55 |
| opendde | H100 | per stage and structure | one subprocess, ≤ 3570 s | $14.67 / $8.63 |
| proteina | A100-80GB | none during the search | ≤ 6780 s (`DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S`, re-read `tools/proteina/run_pipeline.py:3420`) | $11.85 / $6.97 |
| bindcraft | A100-80GB | `_HeartbeatThread` every 60 s (re-read `…/bindcraft/run_pipeline.py:1418-1424`) | 60 s | $0.10 / $0.06 |
| boltzgen (main path) | A100-40GB | keepalive every 300 s (re-read `…/boltzgen/run_pipeline.py:1929-1930`) | 300 s | $0.36 / $0.21 |
| boltzgen (preset path) | A100-40GB | none (census `…/boltzgen/run_pipeline.py:515-540`) | ≤ 1800 s | $2.18 / $1.29 |
| rfdiffusion, rfantibody, pxdesign | A100-40GB / A100-40GB / A100-80GB | **UNVERIFIED**. The only in-repo statement is the generic "every 60s" in `docs/ATOMIC-TOOLS.md:67` | UNVERIFIED | UNVERIFIED |

For a balance-limited run, the customer is charged more than the hold only when funds that arrived during the run, such as a top-up, cover the whole overshoot (§3.1, D5). Otherwise the overshoot column is what **Ranomics** absorbs.

**Conclusion: a heartbeat-driven stop is not bounded** for seven of the eleven in-repo or verified tools.

### 2.3 Cron-driven stop (recommended)

The cron check does not need heartbeats, because metering is wall-clock (`webhooks/modal.py:382-395`). The overshoot is at most (tick interval + cancel latency) × rate:

| Tick | A10G | A100-40GB | A100-80GB | H100 |
|---|---|---|---|---|
| 90 s | $0.03 | $0.11 | $0.16 | $0.37 |
| 300 s | $0.11 | $0.36 | $0.52 | $1.23 |

Unknowns behind these figures:

- **Tick interval: UNVERIFIED.** `cron/tick_campaigns.py:15` says "~60-90s". The schedule itself lives in Railway, outside the repo (`blueprints/targets.py:1457-1461`, filed as A46).
- **Cancel latency: UNVERIFIED.** The call is bounded by `_bounded_modal_call` (`gpu/modal_client.py:602-628`). How fast Modal then stops the container is unmeasured.
- **Cold start before `started_at`.** Time before `started_at` is not metered. The first heartbeat sets it, and so does a status poll: `blueprints/jobs.py::job_status` calls `mark_running` when the poll says `running`, and `ModalClient.poll` says `running` for any call that has not returned, queued or booting included (`gpu/modal_client.py`, the `except TimeoutError` branch). With the job page closed, the stop is late by the container boot and the extra is absorbed. With it open, queue and boot are metered and the stop is early by that much. When `started_at` is not set, the check falls back to `created_at` (in phase 2, only for a row with a Modal call id; see "Leo, 2026-10-06"). That makes the stop early by the queue and boot time.

---

## 3. Debit mechanism

### 3.1 Option (i′): hold = min(balance, cap), and the cron stops a balance-limited run (recommended, no migration)

**Submit** (`shared/wallet_guard.py::requires_wallet`, `:140-373`):

1. Free runs (`estimate <= 0`) keep skipping the hold (`:235`, `:282-287`).
2. The frozen, self-serve ceiling and per-tool cap refusals, and #398's runtime ceiling, stay as they are.
3. The `REASON_INSUFFICIENT` re-pricing gate (`:255-262`) becomes "refuse only when balance ≤ 0".
4. `hold = min(balance, compute_hard_cap(slug, params))` (`shared/wallet_estimates.py:870-890`) replaces `cushioned_hold_usd` (`:294-296`).
5. Placing the hold:
   - `reserve_hold` → `try_hold_for_job` returns NULL when `balance < amount` (`supabase/migrations/0035_phase2_remove_daily_cap.sql:74`). This is a race with a concurrent settle or hold.
   - On NULL, re-read the balance once and retry. If the balance is still ≤ 0, refuse.
6. `inputs._wallet` gains `hold_usd` and `balance_limited` (`hold < cap`), written at `blueprints/tools.py:2341-2351`.

**Invariant.** A run's charge is at most `min(metered, cap)` (`0020:188`), and hold = min(balance, cap). Two cases follow:

- **hold == cap.** The run can never need more than its hold. Behaviour is unchanged: no stop, and the clamp absorbs the rest.
- **hold < cap.** The run can need more only by passing its hold. That is exactly the stop condition.

**Stop check** (a new function, called from `cron/tick_campaigns.py::tick_campaigns`):

1. Select jobs that are `pending` or `running` with `_wallet.balance_limited`.
2. `elapsed = now − (started_at or created_at)`. Then `cost = compute_charge_usd(elapsed, gpu_class_for_job(job))`.
3. If `cost ≥ hold_usd`:
   - Call `modal_client.cancel(call_id)`.
   - If it returns `ok: False`, log it and leave the job running. The next tick retries.
   - Otherwise build the result with `reconstruct(job)` and call `complete_job(... "succeeded", partial, stop_reason, gpu_seconds_used=elapsed)` (§1.2).

**Settle.** Unchanged code (`shared/jobs.py:2118-2280` → `0020:180-265`). Here `actual ≥ hold` and `cap > hold`, so `v_diff < 0`:

- If a top-up arrived mid-run, `balance + diff ≥ 0`, and the overshoot is debited as `charge` (`0020:218`).
- Otherwise an `absorbed_variance` row is written with amount 0, carrying the magnitude in `estimated_cost_usd`.

Without a mid-run top-up the customer pays exactly their hold, which is their whole balance at submit. With one, the overshoot comes out of the top-up (D5, below). Either way **the balance never goes below $0.**

| Concern | Behaviour |
|---|---|
| Concurrent runs | The hold takes the whole balance when the balance is below the cap. A second paid run is then refused until the first settles. A well-funded user reserves the full cap per run, more than today's 1.5× estimate (`shared/wallet_estimates.py:103`). **Decision D1.** |
| Wallet history | Same rows as today: hold, then hold_release or charge or absorbed_variance. No new kinds. |
| Migrations | None. |
| Lost heartbeat | Irrelevant: the cron meters wall-clock time. |
| Webhook racing the stop | `complete_job` compare-and-swap: the loser no-ops (`shared/jobs.py:1898-1915`). Settle is idempotent per hold (`0020` parent_tx_id check). If the container's own result wins, the customer gets the full result, billed through the clamp. |
| Failed Modal cancel | The job stays running and is retried each tick. If the container finishes anyway, the terminal webhook settles through the same clamp: the overshoot is absorbed, or after a mid-run top-up it comes out of the top-up (D5). |
| Cancel stub reports success | `ModalClient.cancel` returns `ok: True` when Modal is missing or stubbed (`gpu/modal_client.py:602-628`). The cron process must have real Modal credentials. **UNVERIFIED** for the Railway cron service. |
| Cancelling fan-out children | esmfold2-design runs an H100 worker under a CPU orchestrator (census `tools/esmfold2_design/modal_app.py:145`, `:668-672`). Whether cancelling the parent call stops the child is **UNVERIFIED** and needs test T2. |
| Spoofed first heartbeat | Heartbeats are unauthenticated (`webhooks/modal.py:297-349`; only the candidate payload is token-gated). A forged beat can set `started_at` to "now" on someone else's pending job, which needs its UUID. The effect is an earlier stop. The charge still cannot pass the hold. Low risk; documented, not fixed. |
| Tiny balances | A $0.01 balance can start an H100 run. It is stopped at the first tick, and Ranomics absorbs about one tick plus cold start per run. **Decision D6.** |

### 3.2 Option (ii): per-heartbeat or per-tick ledger debits

- **Mechanism.** Debit `charge` rows as the run goes. A new SQL function locks the wallet with `FOR UPDATE` and never debits below 0. **This needs a migration.**
- **Wallet history.** One row per tick: about 80 rows for a two-hour run at 90 s. The only alternative is coalescing, which breaks the append-only ledger.
- **Refunds.** Refunded classes (infra_crash, tool_error, preflight_miss, no_progress_timeout, unclassified) must reverse the accumulated debits. That is a new refund path.
- **Readers that change.** Every reader of hold-parented rows changes: `job_spend_by_hold` (`shared/wallet.py:1371-1428`), `run_notices.overrun_line` (`shared/run_notices.py:24-51`), `_failure_money`, and the transactions template lineage (`templates/wallet/transactions.html:100-160`).
- **Upside.** No whole-balance reservation, so concurrent runs are fine. A mid-run top-up or auto-reload naturally keeps a run alive.
- **Verdict.** Not for v1 (2026-10-01). Leo chose it on 2026-10-06: see "Option (ii) build plan".

### 3.3 Option (iii): extendable hold

- **Mechanism.** When a top-up or auto-reload lands mid-run, grow the hold or add a second one. `settle_hold` settles exactly one hold against one actual (`0020:180-265`).
- **Cost.** It needs either a migration (a hold-increase function) or a two-hold settle in Python. The Python route writes a zero `charge` "estimate matched actual" row on the first hold, which is the F-17 wording.
- **Use.** This is the v2 path only if Leo wants top-ups to keep a running job alive (**D4**).

### 3.4 Rejected: keep cushioned holds and stop on the sum of live costs

- `settle_hold`'s variance debit is all-or-nothing (`0020:218`). When two runs overrun, the second run's whole variance is absorbed, even if the customer still has some balance.
- Getting exact "stop at $0" this way needs a partial-debit migration plus a per-user cross-job sum. That is larger than (i′).

---

## 4. Customer copy (no pre-run numbers)

These are drafts. Every surface here also changes in the no-numbers PR, so phase 2 lands after it.

**Start refusal at ≤ $0.** This replaces the `_render_topup_gate` deficit suggestion (`shared/wallet_guard.py:94`), because no deficit exists without an estimate.

> **Your wallet is empty.** Add funds to start this run. You pay only for the GPU time your run uses, and a run stops if your balance reaches $0.

The top-up form keeps its own minimum (`MIN_TOPUP_USD`, `shared/wallet_guard.py:129`). That is a top-up amount, not a run price. Is it allowed under the no-numbers rule? **D8.**

**Submit page, one line under the run button.** Optional.

> Runs draw from your wallet as they go. If your balance reaches $0, the run stops and keeps the designs it has finished.

**Job page, stopped for balance.** This needs a subtitle branch at `templates/job_detail.html:260-269` and a `partial_line` branch at `shared/run_notices.py:58-87`.

> Subtitle: **Stopped: your wallet balance reached $0.**
>
> Notice (designs came back): This run stopped when your wallet balance reached $0, after 7 of 20 designs. The finished designs are below and in the downloads. **Add funds** to run again.
>
> Notice (zero designs): This run stopped when your wallet balance reached $0, before any design finished. **Add funds** to run again.

**Top-up email.** This is the existing completion email, not a new sender. `send_job_complete_email` already renders `run_notices` (`shared/email.py:218`, `templates/email/job_complete.html:148`). Two edits:

- the subject, when `stop_reason == "wallet_empty"`, in place of "Your {tool} run is done" (`shared/email.py:72`);
- an "Add funds" button.

> Subject: **Your {tool} run stopped: your wallet balance reached $0**
>
> Body: the notice above, the usual results link, and an **Add funds** button linking to the top-up page.

**Wallet history.** Today the `absorbed_variance` row prints its raw note, "variance of X USD exceeded balance; absorbed by Ranomics" (`templates/wallet/transactions.html:100-160`). Proposed label:

> **Run stopped at $0.** Ranomics covered the extra GPU time. You were not charged for it.

The hold row's "reserved for this job" hint stays. The no-numbers worker was told to leave hold and reserved rows alone.

---

## 5. Interactions

| Area | Effect | Evidence |
|---|---|---|
| Free runs (estimate ≤ 0) | Unaffected. They take no hold, so they are never balance-limited | `shared/wallet_guard.py:235`, `:282-287` |
| Anonymous runs | None are paid. `/tools/<tool>/submit` is `@login_required` | `blueprints/tools.py:1897-1900` |
| Signup credit ($5) | It is ordinary balance (`shared/wallet.py:161`). Expiry refuses while any hold is open and never debits past the balance | `supabase/migrations/0043_signup_credit_expiry.sql:79-89`, `:99-100` |
| A new user's first run | $5 buys about 2860 s of A100-80GB, so a $5 proteina run is stopped at about 48 min. Proteina uploads nothing until its search exits (§1.3), so **the user loses $5 and gets 0 designs** unless proteina gets the phase 2b graceful stop. **D3, D7** | `shared/wallet.py:181-189`, `tools/proteina/run_pipeline.py:4278-4289` |
| Auto-reload mid-run | It fires only after settle (`shared/wallet.py:1639-1667`), so it never keeps a running job alive. After a stop it refills for the *next* run. **D4** | |
| Low-balance email | It fires on a downward crossing *at settle* (`shared/wallet.py:1646-1667`). A balance-limited hold has already taken the balance to $0 at submit, so it will not fire on a balance stop. The completion email (§4) is the top-up email | |
| Monthly-cap email storm (chip task_e9c31589) | Every balance stop settles at about $0 and then calls `auto_reload_if_needed`. A capped user would get the unthrottled monthly-cap email (`shared/wallet.py:1187-1192`) on every stop. **The storm fix should land before or with phase 2A** | |
| #398 runtime ceiling | Unchanged. It stays a submit-time refusal tied to the subprocess timeout, not to money | memory runtime-ceiling-gate-398 |
| Campaigns | Unchanged in v1. Children keep cushioned per-chunk holds (`shared/compute_campaigns.py:2061`, `:2083`) and pause on insufficient funds before a child starts (`:2382`, `:2514`). A child can still pass its hold under today's clamp. **D9** | |
| 1.5× overrun warning | Being removed by the no-numbers worker. The stop does not use `mid_run_monitor_check` | `shared/jobs.py:2288`, `:2314-2432` |

---

## 6. Phase 2 plan

As planned in phase 1: this was to start only after Leo approved **and** the no-numbers PR merged. Each PR goes through the saved `review-code` and `review-claims` agents on its delta before any push. Tests run with `venv/Scripts/python.exe -m pytest`.

| PR | Scope | Tests | Paid GPU test? |
|---|---|---|---|
| **0** (prerequisite) | Monthly-cap email storm fix (chip task_e9c31589) | its own | no |
| **2A** (money) | `requires_wallet` hold sizing, the ≤ $0 refusal, the retry on a NULL hold, the `_wallet.hold_usd` / `balance_limited` fields, and the stop check plus its call from `tick_campaigns` | Hold sizing: balance < cap, balance ≥ cap, balance ≤ 0, NULL then retry. Stop threshold, including the `created_at` fallback. Cancel `ok: False` leaves the job running. A container webhook winning the race. After stop: charged == hold, an absorbed_variance row, balance == 0 and never negative. With a mid-run top-up: the overshoot is debited. Free, campaign and hold == cap runs are untouched | yes: T1, T2 |
| **2B** (copy) | `partial_line` branch, job page subtitle, completion email subject and button, wallet history label, refusal page | Render tests per surface. A no-numbers guard on the new strings | no |
| **2b-proteina, 2b-bindcraft, 2b-boltzgen, …** (container, one PR per tool, after D7) | Graceful stop: on cancel (or a stop flag), end the subprocess, collect the designs on disk, upload them, and post a partial result. Proteina reportedly already has a partial path on a nonzero search exit (stated only in the docstring at `shared/run_notices.py:61-63`; the pipeline code is UNVERIFIED). The hub then waits a grace period for that webhook before falling back to `reconstruct` | Unit tests on the collector, plus an image build | yes, one per tool: T3 |

**Paid GPU tests.** Each needs Leo's per-job approval. None has been launched. Each needs a funded test account; how it is funded is Leo's call, because phase 1 forbids top-ups.

- **T1.**
  - Setup: a boltz2 run on a test account holding about $0.30, which is about 4 min of A100-40GB. Choose more than 10 designs so at least one chunk completes.
  - Check:
    - the stop fires within one tick;
    - the Modal call is actually stopped (Modal dashboard);
    - the finished designs appear on the page and in CSV, FASTA and ZIP;
    - the ledger shows hold, absorbed_variance (amount 0) and balance exactly $0;
    - the stop email arrives.
  - Cost: under $1, UNVERIFIED.
- **T2.**
  - Setup: an esmfold2-design run on a small balance.
  - Check: cancelling the orchestrator stops the H100 worker, and there is no stray GPU time on the Modal dashboard.
  - Cost: H100, under $1.50, UNVERIFIED.
- **T3 (per 2b tool).**
  - Setup: a run on a small balance.
  - Check: the designs finished before the stop come back.
  - Cost: per tool. Proteina is the largest, at about one balance plus one tick.

---

## 7. Decisions for Leo

1. **D1. Concurrency.** Every paid run reserves `min(balance, tool cap)`. A small balance therefore runs one paid job at a time, and a large balance reserves the full cap per run. *Recommended:* accept. It is what makes "stops at $0" exact with no migration.
2. **D2. Status of a stopped run.** "Succeeded, stopped early" or "cancelled". *Recommended:* succeeded, because it reuses the page, downloads, email and billing.
3. **D3. A stop that finished zero designs.** Bill it or refund it. *Recommended:* bill it (it is the `completed_no_yield` class). Refunding makes every tiny-balance run free GPU time. The cost is that, until 2b ships, a stopped proteina, bindcraft or boltzgen run returns nothing.
4. **D4. A top-up or auto-reload during a run.** *Recommended for v1:* it does not extend the run (§3.3 is v2).
5. **D5. The overshoot when a top-up landed mid-run.** Today's settle takes it from the new funds; it is cents, at most one tick. The other choice is always absorbing it. *Recommended:* take it, because that keeps the ledger honest about metered time.
6. **D6. Minimum balance to start.** Leo said "> $0". Tiny balances each cost Ranomics about one tick plus cold start. *Recommended:* keep "> $0" and watch it.
7. **D7. Graceful stop per tool (phase 2b).** Which tools, and in what order? *Recommended:* proteina first (it has the biggest silent stretch and a reported partial path, UNVERIFIED), then bindcraft and boltzgen (they already have keepalive threads).
8. **D8. Showing the minimum top-up amount** on the refusal page. Does it count as a "pre-run number"?
9. **D9. Campaigns.** *Recommended:* leave them unchanged in v1.
10. **Confirm** that the `campaigns:tick` Railway cron is scheduled, at what interval, and that it has Modal credentials. The stop depends on it (A46).

---

## Adjacent findings (out of scope in phase 1)

- **A1. User cancels of silent tools settle at $0.**
  - `cancel_job` bills `gpu_seconds_used` from the row (`shared/jobs.py:1830-1839`).
  - The only mid-run writer of that column is `mid_run_monitor_check` (`:2371`), which runs only on heartbeats.
  - So a user cancel of esmfold2-design, mpnn, or an af2, colabfold or esmfold single fold bills $0 for any runtime. Heartbeating tools are billed only up to their last beat.
  - The phase 2A stop computes elapsed time itself and does not inherit this.
- **A2. `cancel_job` ignores a failed Modal cancel.**
  - `ModalClient.cancel` reports failure by returning `{"ok": False}` and does not raise (`gpu/modal_client.py:602-628`). `cancel_job` only catches exceptions (`shared/jobs.py:1803-1811`).
  - A failed cancel still marks the job cancelled while the container keeps running.
  - Phase 2 fixes this for the stop and for single-job cancels; campaign cancels keep the old behaviour. See "A2 is fixed for the stop and for single-job user cancels" below. A1 and A3 are unchanged.
- **A3. Proteina citations.** The census cited proteina lines in `modal_app.py`, which has 337 lines (re-read with `wc -l`). They are in `tools/proteina/run_pipeline.py`.

---

## Decisions (Leo, 2026-10-05)

Phase 2 was first built on option (i′). Citations in this section, down to "Out of scope", are by symbol or test name at commit `3bd13826`, the last option (i′) commit on the phase 2 branch, not against `cc85754d`. Option (ii) removed several of them; see "What was built" at the end.

### Answers to §7

- **D1.** Hold = `min(balance, tool cap)`. `shared/wallet_guard.py::_reserve_live_hold`; `tests/test_live_charging.py::test_balance_under_the_cap_holds_the_whole_balance`, `::test_balance_over_the_cap_holds_the_cap`.
- **D2.** A stopped run finishes as succeeded and partial. `shared/jobs.py::stop_wallet_limited_jobs`; `::test_run_stops_when_its_cost_reaches_the_hold`.
- **D3.** A stop with zero designs is billed, as `completed_no_yield`, and the page and email say it stopped before any design finished. `::test_stop_before_any_design_finished_is_billed_as_no_yield`, `::test_stop_line_with_no_design_says_none_finished`, `::test_email_for_a_stopped_run_with_no_design`.
- **D4.** A top-up during a run does not extend it. The hold is fixed at submit.
- **D5.** The overshoot after a mid-run top-up comes out of the new funds. `::test_overshoot_after_a_mid_run_top_up_comes_out_of_the_top_up`.
- **D6.** Any balance above $0 can start a run. `::test_empty_wallet_is_refused`.
- **D7.** Phase 2b (graceful stop per tool) is out of scope. Live charging is enabled only where a hub-side stop keeps finished designs (below).
- **D8.** The refusal page may show `MIN_TOPUP_USD`. The top-up hero already shows it.
- **D9.** Campaigns are unchanged. The stop skips any job with a `campaign_id`.
- **Balance never negative.** The settle clamp and `absorbed_variance` hold it at or above $0. `::test_late_stop_charges_the_hold_and_absorbs_the_rest`.

### Leo, 2026-10-06 (on PR #416)

- **`created_at` fallback: built.** A run with no `started_at` is metered from `created_at` (`shared/jobs.py::stop_wallet_limited_jobs`; `::test_run_with_no_heartbeat_is_metered_from_created_at`). A run that has `started_at` is metered from it as before (`::test_run_keeps_going_while_its_cost_is_under_the_hold`, whose row has a `created_at` days earlier).
  - The trade-off Leo accepted: `created_at` includes Modal queue time and container boot, so such a run can be stopped, and charged its whole hold, before its first design. With a small balance on a cold af2 batch container that is likely.
  - Only a row with a Modal call id falls back. A row with neither `started_at` nor a call id is a submit Modal never acknowledged. The stop leaves it to `sweep-stuck`, which times out a pending row older than `STUCK_PENDING_AGE_MINUTES` (default 30; `cron/sweep_stuck_jobs.py::sweep_stuck_jobs`). `::test_rows_the_stop_never_touches`.
- **A2 covers every tool's single-job cancel**, as built. Leo's decision; see "A2 is fixed" below.
- **Queue and boot metered while the job page is open** (§2.3, "Cold start before `started_at`"): known, and out of scope for this PR.
- **D1: reversed, later on 2026-10-06.** Leo chose true per-minute deduction, option (ii): nothing set aside, the balance drops while the run goes, and the run stops at $0. See "Option (ii) build plan".
- **`STRIPE_SECRET_KEY` on the cron:** Leo added it himself. See the Stripe finding below. No setting was changed here.

### Which tools get live charging

`shared/wallet_guard.py::live_charging_enabled`, pinned by `::test_live_charging_only_on_the_enabled_tiers`.

| Tool | Tier | Why a hub-side stop keeps designs |
|---|---|---|
| boltz2 | every preset | `upload_pdb` inside the per-design loop in `main` |
| af2 | batch only | `upload_pdb` in `_run_batch._dispatch`, once per record |
| colabfold | batch only | `upload_pdb` in `_run_batch._dispatch`, once per record |
| esmfold | batch only | `upload_pdb` inside the loop in `_run_batch_folds` |

- The upload sites are pinned by `::test_live_tools_upload_each_design_mid_run`.
- The adapters send `batch_records` only on the batch preset, which is what routes a run into those batch functions. Pinned by `::test_batch_records_reach_the_container_only_on_the_batch_preset`.
- A single af2, colabfold or esmfold fold uploads only at the end, so a stop would keep nothing. Those tiers keep the cushioned hold.
- Every other tool also keeps the cushioned hold. Proteina uploads nothing until its search exits (§1.3). The rest need the 2b container work.

### §7 item 10: the cron (checked 2026-10-05, read-only)

- **Schedule.** `railway status --json`: the service `tools-hub-campaigns-tick` runs `flask --app app campaigns:tick` on `*/5 * * * *`. Its latest deploy was SUCCESS.
- **Credentials.** `railway variables` (names only): `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` are set.
- **Overshoot bound.** The tick is 300 s, not the 60-90 s in `cron/tick_campaigns.py`. The bound is therefore the 300 s row of §2.3, plus the cancel latency.
- **Cancel latency: UNVERIFIED.** It is measured by T1.

### Deviations from the note

- **The estimate endpoint.** `blueprints/wallet.py` asks for $0 for a live-charged tool when the balance is above $0, so the form does not raise a top-up gate the server would not raise. `::test_estimate_deficit_matches_the_guard`.
- **The stop line has no "of M".** None of the four enabled tools has a design-count input (`shared/compute_campaigns.py::design_param_key` returns `None` for each), so the line says "after N designs".
- **Wallet history wording.** The hint on an `absorbed_variance` row is generic: "Your balance did not cover all of this job's GPU time. Ranomics covered the rest, and you were not charged for it." The drafted "Run stopped at $0" would be false on rows that settle_hold absorbs for a run that was not stopped (`supabase/migrations/0020_wallet_corrections.sql:218-250` absorbs any overrun the balance cannot cover).
- **No Modal call id.** A job with no `modal_function_call_id` is finished without a cancel. `::test_run_with_no_modal_call_stops_without_a_cancel`.
- **D5 edge.** When the top-up is smaller than the overshoot, settle_hold absorbs the whole overshoot and the top-up is untouched. It does not take part (`supabase/migrations/0020_wallet_corrections.sql:218-250`; modelled in `::test_overshoot_bigger_than_the_top_up_is_absorbed_whole`).
- **The optional submit-page line** (§4) was not added.

### A2 is fixed for the stop and for single-job user cancels

- A failed Modal cancel (raised, or `ok: False`) leaves the job running.
- The stop retries it on the next tick: `::test_failed_modal_cancel_leaves_the_run_going`.
- A user cancel returns `modal_cancel_failed`: `::test_failed_user_cancel_leaves_the_job_running`.
- A campaign cancel keeps its old behaviour (D9): `cancel_campaign` passes `leave_running_if_cancel_fails=False`, so a child whose Modal cancel fails is still cancelled locally. `::test_campaign_child_is_cancelled_locally_when_modal_cancel_fails`, `tests/test_compute_campaigns_driver.py::test_cancel_campaign`.
- Side effects:
  - The cancel button shows the raw code: "Could not cancel: modal_cancel_failed" (`templates/job_detail.html:547`).
  - Whether Modal raises when cancelling a call that has already finished is UNVERIFIED.
  - It covers every tool's single-job cancel, not only live-charged runs: `blueprints/jobs.py::job_cancel` calls `cancel_job` with the default `leave_running_if_cancel_fails=True`.
  - If the cancel fails, the run goes on and is settled like any finished run: the cost is clamped at the tool's cap (`supabase/migrations/0020_wallet_corrections.sql:188`), and the part past the hold is charged only when the balance covers it (`:218-232`).
  - Pressing cancel again retries it: the 409 releases the idempotency claim (`shared/idempotency.py:759`).
  - A stop whose cancel keeps failing on a live container lets the run go on until a cancel lands or the tool's Modal function timeout ends it (`timeout=_MAX_SESSION_S` in each `tools/<tool>/modal_app.py`: 3600 s for boltz2 and esmfold, 14400 s for af2 and colabfold). The overrun past the hold is absorbed unless the balance covers it (`supabase/migrations/0020_wallet_corrections.sql:218-250`).
  - `ModalClient.cancel` (`gpu/modal_client.py`) returns `ok: False` for any exception. Whether Modal raises for a call it no longer keeps is UNVERIFIED. If it does, every press fails and the stop retries each tick until `sweep-stuck` ends the row: `cron/sweep_stuck_jobs.py::sweep_stuck_jobs` ends a running row older than `STUCK_RUNNING_AGE_HOURS` (default 6) through `shared/jobs.py::timeout_stuck_job`, which does not call Modal's cancel.
  - Leo's decision (2026-10-06): keep it for every tool, as built.

### Finding for Leo: Stripe is not set on the cron service

- **The gap (2026-10-05).** `railway variables` on `tools-hub-campaigns-tick` listed no `STRIPE_SECRET_KEY`.
- **Why it matters.**
  - A stop settles on the cron. The settle calls `auto_reload_if_needed` (`shared/wallet.py::_post_settle_hooks`).
  - That call takes the 24h dispatch claim (`_claim_auto_reload_dispatch`) before it calls Stripe.
  - Stripe then raises "Stripe is not configured." with `retryable=True` (`billing/checkout.py::create_off_session_payment_intent`).
  - A retryable failure sends no email.
  - So a user with auto-reload gets no reload and no notice for 24h after a stop.
- **Same gap elsewhere.** The `sweep-stuck` service has the same gap. It also has no Modal tokens.
- **Not new.** The cron already settles campaign children through `complete_job` (`shared/compute_campaigns.py::reconcile_campaign_children`). Each child carries its own hold (`child_inputs["_wallet"]` in `shared/compute_campaigns.py`), so a settled child reaches `auto_reload_if_needed` through `shared/wallet.py::settle_hold` and `::_post_settle_hooks`. This is from reading the code; no run has shown it.
- **Added (2026-10-06).** Leo added `STRIPE_SECRET_KEY` to `tools-hub-campaigns-tick`. The Railway UI lists it and the service redeployed; this is as reported by the hub lead and was not re-checked here. Whether its value matches the web service's is UNVERIFIED. It is off the pre-merge list.
- **No setting was changed.**

### Out of scope

- **A1** (user cancels of silent tools settle at $0) is not fixed.
- **The mail path in `auto_reload_if_needed`** belongs to the monthly-cap throttle PR (#413, merged). Its per-process throttle restarts with each cron run, so a stop that settles on the cron can send one monthly-cap email per stop.

---

## Option (ii) build plan (Leo, 2026-10-06)

**Status: built on `claude/live-charging-phase1` (PR #416), not merged and not deployed.** Leo approved the plan on 2026-10-06 (decisions 1 to 7 below). Migration 0045 is not applied to prod, and no paid run has been made. Where the build departs from this plan, "What was built" at the end of this section says so.

Leo reversed D1 on 2026-10-06. The cost will come off the balance while the run goes. Nothing will be set aside, and the run will stop at $0. This replaces option (i′) (§3.1) for the four live tools. PR #416 is not to be merged as it stands; it will be reworked on the same branch. #416 never reached prod, so no stored row carries its `balance_limited` flag and no data needs migrating.

### What stays from #416 and what goes

**Stays:**

- **The live-tool list and its pins.** See "Which tools get live charging" and `shared/wallet_guard.py::live_charging_enabled`.
- **The stop itself, from `shared/jobs.py::stop_wallet_limited_jobs`.**
  - Cancel on Modal.
  - Re-read the row.
  - Rebuild the finished designs with `job_recovery.reconstruct`.
  - Finish the run as succeeded and partial, with `stop_reason` = `WALLET_STOP_REASON`.
  - Only what triggers the stop changes.
- **The metering clock.** `started_at`, or `created_at` when the row has a Modal call id (Leo, 2026-10-06).
- **Existing behaviour.**
  - A2 on every tool's single-job cancel.
  - D9: campaigns are unchanged.
  - The stop's errors count in the tick's log line.
- **The `wallet_empty` refusal** at a balance ≤ $0, and the estimate route's "any balance above $0" (D6).
- **The copy:** the stopped-at-$0 subtitle, notice and email, the Add funds links, and the `absorbed_variance` hint.

**Goes or changes:**

- **The capped hold goes.** `shared/wallet_guard.py::_reserve_live_hold` and its `min(balance, cap)` hold, its two-attempt retry and `hold_failed` will go. A live run will open a $0 anchor instead (below).
- **The balance-limited test goes.** Gone: `_wallet.hold_usd`, `_wallet.balance_limited`, and the stop's "cost has reached the hold" test (`shared/jobs.py:1836-1864`). Every live run will be metered, not only balance-limited ones, and a `_wallet.live` flag will mark it.
- **The `wallet_empty` copy narrows.** Its sentence that funds held for a run still going come back, less what it used, will be true only of cushioned runs. It will be shown only when such a hold is open, or dropped.
- **D4 and D5 go away.** A top-up during a run will keep it going (below).
- **Settling a live run changes.** It will call the new `settle_live_run`, not `settle_hold` or `release_hold`.

### Ledger shape

One run will be one lineage under one anchor:

- **Anchor.** A `hold` row of $0.00, written at submit.
  - `_wallet.hold_tx_id` will point to it.
  - So the wallet page grouping (`blueprints/wallet.py::_build_tx_lineage_annotations`), `job_spend_by_hold` and `_failure_money` will find the run the way they find a hold today.
- **Debits.** `run_debit` rows, a new kind.
  - Each has `parent_tx_id` = the anchor and a negative amount.
  - There is one for each tick that takes money.
- **Terminal row.** Written once, by `settle_live_run`. It is one of:
  - a `charge` for the last part (which can be $0);
  - a `hold_release` that credits back what was taken, for a refund or an over-take.

  An `absorbed_variance` at $0 is added when the balance did not cover the run.
- **Settled.** A lineage is settled when it has a child that is not a `run_debit`.

Rejected: one running-total row updated in place. The ledger is append-only, and an updated row's `balance_after_usd` would go stale (§3.2).

### Migration 0045 (Leo applies it to prod before merge)

The file will be `supabase/migrations/0045_live_run_debits.sql`.

Each new function will:

- take `public.user_wallets ... FOR UPDATE`, as `credit_wallet` does (`0018_wallet_rpcs.sql:103`);
- recompute the balance from the ledger.

What the migration does:

1. **New kind.** `ALTER TYPE public.wallet_tx_kind ADD VALUE IF NOT EXISTS 'run_debit';`, the 0043 pattern (`0043_signup_credit_expiry.sql:31`).
2. **`open_live_run(p_user_id, p_tool_slug)`.**
   - Under the lock, refuse a frozen wallet or a balance ≤ $0.
   - Otherwise write the $0 anchor and return its id.
   - This closes the gap between today's balance read and the hold.
3. **`debit_live_run(p_hold_tx_id, p_due_usd, p_gpu_seconds, p_gpu_class)`.** `p_due_usd` is the run's whole cost so far. Under the lock:
   - If the lineage is settled, do nothing.
   - Otherwise taken = what the run's `run_debit` rows already took, and step = due − taken.
   - If step ≤ 0, do nothing.
   - Otherwise debit min(step, balance) as one `run_debit` row.
   - Return the amount taken, the amount short and the new balance.
   - The balance never goes below $0.
4. **`settle_live_run(p_hold_tx_id, p_final_due_usd, p_gpu_seconds, p_gpu_class, p_failure_reason, p_notes)`.** Once per lineage; a settled lineage is a no-op.
   - **final ≥ taken:** charge min(final − taken, balance) as `charge`, and record the rest as `absorbed_variance` at $0.
   - **final < taken:** credit back taken − final as `hold_release`.
   - A refunded class passes final = 0, which returns everything taken.
5. **`expire_signup_credit` redefined.**
   - Its `hold_open` refusal (`0043_signup_credit_expiry.sql:79-90`) treats a hold with any child as closed.
   - A debit child would therefore let the expiry run while a live run is still debiting.
   - The new test: a hold is open until it has a child that is not a `run_debit`.
6. **Spend counts debits.** `wallet_30d_spend` will be redefined to count `run_debit` in net spend. `shared/wallet.py::_net_spend_usd` (its `.in_("kind", ...)` filter) will change the same way.
7. **Grants.** `EXECUTE` will be granted to the new functions, as 0035 grants `try_hold_for_job`.

**Idempotency needs no key.**

- A debit is a target ("the run has cost $X so far"), not an increment.
- A repeated or overlapping tick computes the same or a larger target, and takes only the difference.

**How the SQL gets tested before prod.**

- The suite mocks the RPCs.
- `scripts/check_live_charging_local_pg.py` drives the three functions, the expiry and the view on the local Supabase stack. It does not apply the migration: run it after `supabase db reset --local`. It exits before any call when the API host is not 127.0.0.1 or localhost. Results are under "What was built".

### What triggers a debit, and its size

- **The trigger is the `campaigns:tick` cron, every 5 minutes** (`*/5`; "§7 item 10: the cron").
  - It will handle each live job that is not finished and not a campaign child.
  - Due = `compute_charge_usd` over the metered seconds, capped at the tool's cap. This is the formula settle uses.
  - Then it calls `debit_live_run`.
- **So the balance drops in 5-minute steps, not every minute.**
  - Railway's cron does not run more often than every 5 minutes. This comes from Railway's documentation and was not checked in this session.
  - A one-minute step needs a new always-on worker (decision 1).
- **The heartbeat is not used.**
  - It is unauthenticated (`webhooks/modal.py::_handle_heartbeat`).
  - af2 and colabfold are silent through their cold start (§2.2).
  - A debit there would not stop a run sooner, because the stop runs on the tick.
- **One step costs at most** $0.36 on A100-40GB (boltz2, colabfold, esmfold) and $0.52 on A100-80GB (af2), in customer dollars (§2.3, the 300 s row).

### Stop at $0, and a top-up mid-run

- **The stop.** When `debit_live_run` comes back short, the balance is $0. The tick will stop the run in the same pass, with the #416 stop path.
- **The overshoot.** The time past $0 is up to one step plus the cancel latency. Settle will record it as `absorbed_variance`. At Ranomics' own rate (the customer figure ÷ 1.70, §2.1) that is at most about $0.21 (A100-40GB) or $0.31 (A100-80GB) per stop.
- **A top-up keeps the run going** if it lands before the tick that would come up short. The next debit takes from the new funds. There is no hold to extend.
- **Auto-reload.**
  - After each debit, the tick will call `auto_reload_if_needed`, the same call `_post_settle_hooks` makes. Its mail path is not touched.
  - It fires once the balance falls under the user's threshold.
  - It only dispatches the Stripe charge (`"triggered"`). The credit lands later, through the Stripe webhook.
  - So a reload fired in the same tick that comes up short does not save that run (decision 4).
- **The low-balance email** will fire on the debit that crosses `LOW_BALANCE_EMAIL_THRESHOLD`. It uses the crossing test in `_post_settle_hooks` (`shared/wallet.py:1669`).
- **Stripe.** The tick will dispatch auto-reloads, so it needs `STRIPE_SECRET_KEY`. See the Stripe finding.

### Races

- **Two live runs on one balance.**
  - Every debit takes the wallet lock, so the debits queue.
  - Both runs draw on the same balance.
  - When the balance reaches $0, each run whose debit then comes up short is stopped in that tick.
  - The tick will take jobs oldest first, so the older run gets the last cents.
- **A live run beside a cushioned run.**
  - The cushioned hold left the balance at submit, so a debit cannot touch it.
  - When that run settles, anything its hold returns is free for the live run's next debit.
  - Its `settle_hold` variance debit competes for the same balance under the same lock. This is unchanged (§3.4).
- **Tick against settle.**
  - Both take the lock.
  - A debit after the settle finds the lineage settled and does nothing.
  - A settle after a debit takes only final − taken.
  - If a tick took more than the final cost, for example because the tick's clock ran ahead of the clock at completion, the settle credits back the difference.
- **Tick against a user cancel.**
  - Both end in `complete_job`. Its status compare-and-set (`allowed_current=_NON_TERMINAL`) lets only one of them finish the row.
  - `settle_live_run` runs once per lineage.
  - A failed cancel leaves the run going, and still debiting (A2).
- **A repeated debit.** No double charge, because the debit is a target (above).
- **A top-up against a debit.** `credit_wallet` takes the same lock (`0018_wallet_rpcs.sql:103`).

### Refunds

- `_settle_wallet_hold_for_completed_job` will send a live run (`_wallet.live`) to `settle_live_run`.
- **Refunded classes.** A run that ends in `_REFUNDED_FAILURE_CLASSES` (`shared/jobs.py:760`) passes final = 0. Everything taken comes back as one `hold_release`.
- **Zero-cost arms.** The zero-consumption cancel and the legacy no-compute arms pass 0 too.
- **Billed runs** pass the metered cost, capped at the tool's cap.

### Wallet history

**The ledger.** It will hold one `run_debit` per 5-minute step. That is 12 an hour, and at most 48 for a 4-hour af2 run (`_MAX_SESSION_S`, 14400 s). There is no row per minute.

**The wallet page.** It will show one line per run.

- The anchor line reads "<tool> run".
  - While the run goes, it shows the total so far.
  - Once settled, it shows the final cost, or "returned in full".
- The run's debit rows fold under that line and can be expanded.
- This builds on the existing grouping by `parent_tx_id` (`blueprints/wallet.py::_build_tx_lineage_annotations`).
- The "Charges" filter (`templates/wallet/transactions.html:82`) will include `run_debit`.

**Readers that change.**

- `job_spend_by_hold`: settled will mean a child that is not a `run_debit`.
- `_build_tx_lineage_annotations`: a debit is not a settlement, and the outcome reads as charged while it ran.
- `_failure_money`: the refund sentence will read from the `hold_release`.
- `_net_spend_usd`: as above.

### The submit check

- **A live tool will be refused when:**
  - the wallet is frozen;
  - the balance is ≤ $0 (`wallet_empty`, as now; D6);
  - the tool cap, the self-serve ceiling or the #398 check refuses it. These stay.
- **Otherwise it calls `open_live_run`.**
- **A second live run** is allowed while the first one goes, as long as the balance is above $0. Nothing is set aside.
- **Ranomics' exposure** is one step for each live run still going when the balance hits $0.

### Tools outside the live set

- **Unchanged:** the cushioned hold, `settle_hold` and `release_hold`.
- **This covers:** the single-fold tiers of af2, colabfold and esmfold, every other tool, and every campaign child (D9).
- **One switch.** `live_charging_enabled` decides which path a run takes.
- **The workspace meter** (`charge_for_job`, called at completion from `shared/jobs.py`; #345) is unchanged for every run.

### Decisions for Leo

1. **How often the balance drops.**
   - On the cron, the balance drops every 5 minutes, not every minute.
   - A true one-minute step needs a new always-on service.
   - Recommendation: every 5 minutes.
2. **Stop late or stop early.**
   - The run stops at the first 5-minute check that finds the money gone. So it can run up to about 5 minutes past $0 at Ranomics' cost: at most about 31 cents of real GPU time per stop.
   - The alternative charges 5 minutes ahead. It stops while a few minutes of the customer's balance are unused, and returns that money at the end.
   - Recommendation: stop late.
3. **Several runs at once.**
   - A second live run is allowed while one goes, as long as the balance is above $0.
   - Each run can overshoot by one step when the money runs out.
   - Recommendation: allow it, with no new limit. A grep of `shared/wallet_guard.py`, `shared/jobs.py` and `blueprints/jobs.py` found no per-user limit on running jobs today.
4. **Auto-reload in the last step.**
   - If auto-reload fires in the same check that finds the money gone, the run still stops, because the card charge lands a moment later.
   - The alternative waits one more check before stopping. That costs Ranomics up to one more step if the card fails.
   - Recommendation: stop. Auto-reload fires when the balance falls under the customer's own threshold, so it usually fires several checks before $0.
5. **Wallet history.**
   - One line per run, with the 5-minute debits folded under it.
   - Recommendation: yes.
6. **The migration.**
   - Migration 0045 adds one transaction kind and three wallet functions. It also changes the signup-credit expiry check and the 30-day spend view.
   - Leo applies it to prod before merge.
7. **The paid test (T1)**, approved per job:
   - one boltz2 or batch-tier run stopped at $0;
   - one run that ends in a refunded failure.

### What was built (2026-10-06)

Where the build departs from the plan above:

- **`settle_live_run` takes no `p_notes`.** The SQL writes its own note on each closing row (`supabase/migrations/0045_live_run_debits.sql`, `settle_live_run`).
- **The view compares `kind::text`.** An enum value added in a transaction cannot be used as an enum literal in that same transaction, and the SQL editor and the CLI run the file as one (the header of 0045).
- **One function meters and stops.** `shared/jobs.py::meter_live_runs` replaces `stop_wallet_limited_jobs`. Each tick it debits every live run, oldest first, and stops a run whose debit came back short. The stop path itself is the #416 one.
- **The Python `debit_live_run` takes the user id.** It runs `_post_settle_hooks` (auto-reload, low-balance email) for the amount a debit took, without a second read (`shared/wallet.py::debit_live_run`).
- **`hold_failed` stays.** When `open_live_run` returns no anchor and a re-read balance is still above $0, the gate reason is `hold_failed`, shown with the generic "Your job did not start" copy (`shared/wallet_guard.py::requires_wallet`; `templates/wallet/topup.html`).
- **The `wallet_empty` copy dropped** its sentence about funds held for a run still going (`templates/wallet/topup.html`).
- **Wallet history.**
  - The terminal row stays its own line, annotated as the settlement or release is today.
  - The unfiltered history leaves `run_debit` rows out; the "Charges" filter shows `charge` and `run_debit` (`blueprints/wallet.py::wallet_transactions`).
  - An anchor with no debits reads as a run, so an unused anchor shows a "run" line and a $0 release row.

**Local Postgres results** (`scripts/check_live_charging_local_pg.py` on the local stack at migration 0045, 2026-10-06):

- 60 of 60 checks pass.
- Three mutants of the SQL were each caught:
  - the debit as an increment instead of a target: 15 checks fail;
  - the expiry without its `run_debit` exclusion: 3 fail;
  - `debit_live_run` without the wallet lock, with a pause between its reads and its insert: 10 fail. One race left the ledger summing to -3.0 against a balance of 7.5.
- A scratch script applied each mutated 0045 to the local database, ran the harness, then re-applied the real file. The mutants are not in the harness.
