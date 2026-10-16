# BindCraft live charging: STEP 1 report (2026-10-14)

Free checks only: no GPU, no prod reads or writes. Code read at origin/main 6a47aeb9 (tools-hub) and lpd origin/master 18382cd (BindCraft container, scratchpad copy). STEP 1 changed nothing in the repo. Every tools-hub `file:line` below is at 6a47aeb9; every lpd one is at 18382cd.

The STEP 3 PR, which adds this file, puts BindCraft on the live list. The lines below that say BindCraft is not live describe main before that PR.

## (a) to (d), already reported

- Start minimum, derived the way #427 derived rfdiffusion's: 600 s × $0.0017476/s = **$1.0486** (A100-80GB rate `shared/wallet.py:183-184` × markup 1.70 `shared/wallet.py:74`). Probably too low for BindCraft, whose first design can take far longer than 10 minutes.
- Stop test cost: about **$1.00 to $1.60** if cancelled soon after the first design. A 30-minute deadline with no design costs about **$3.15** and proves nothing. Worst case, only if the cancel fails: the cap, **$8.00**.
- Zero-design risk is real (job 489a17e0 made 0 designs in 103 min). Reuse job 1c4d5803's target (4ZQK chain A), which did produce designs. An empty stop is still charged (decision D3).

## (e) A live BindCraft run at the time limit, the cost cap, or the stuck-job sweeper

### Time limit (the one that bites)

1. The pipeline runs BindCraft with a 4-hour subprocess limit: `run_command(cmd, timeout=14400)` (lpd `docker/bindcraft/run_pipeline.py:1719`, `subprocess.run(... timeout=timeout)` at :484). Modal's own limit is 23 h (`infrastructure/modal/bindcraft_app.py:46`, :185), so the 4 h limit fires first.
2. The timeout raises into the catch-all, which posts `{"error": "Pipeline failed: ..."}` (run_pipeline.py:1896-1903). `post_webhook` sends it as FAILED with `{"category", "message"}` and **no `bucket`** (run_pipeline.py:512-517).
3. The webhook stores a `failed` row with `result=None` (`webhooks/modal.py:151-162`). With no bucket the class is `unclassified` (`shared/jobs.py:874-881`), a refunded class (`shared/jobs.py:798-804`).
4. On a live run the refund goes to `settle_live_run(..., refund=True)` (`shared/jobs.py:2484-2491`, :2500-2511). Final cost $0 (`shared/wallet.py:1013`). The SQL then returns **every debit the run took** as one `hold_release` (`supabase/migrations/0045_live_run_debits.sql:230-243`).

**Money:** the customer pays $0. Ranomics pays the whole 4 h of GPU: 14400 s × $0.0017476 = $25.17 at the billed rate, or about **$14.80** of raw GPU cost. The debits taken during the run stop at the cap (below), so the balance falls by up to $8 (≤ 2 designs) and then comes back.
**Designs:** the streamed designs are in Storage, but the failed page shows a failure card and no designs (`templates/job_detail.html:312-314`). The live table exists only in the running block (`templates/job_detail.html:186`, :227-230). The status JSON still returns `partial_candidates` (`blueprints/jobs.py:1031`), with no check on status.
**Customer sees:** "Job failed. See details below." (`templates/job_detail.html:253-256`), then "The run stopped for a reason we could not identify." (`shared/jobs.py:950`) and a refunded line (`failure_notice`, `shared/jobs.py:978-983`).
**Not new with live:** a non-live run takes the same path today (failed-runs audit, `docs/qa/FAILED-RUNS-2026-09-30.md` in the main checkout: c43329f3, 14400 s, refunded $12.00).

### Per-tool cost cap

- The meter's charge is capped at the tool cap: `live_due_usd = min(charge, compute_hard_cap)` (`shared/wallet.py:944-947`). For BindCraft the cap is $8.00 × max(num_designs / 2, 1), at most $500 (`shared/wallet_estimates.py:360-368`, :890-895). At $0.0017476/s the $8 cap is reached at about 76 min.
- Once the debits reach the cap, `v_step` is 0, so `short` is 0 (0045 :121-124, :141). The meter stops a run **only** when `short > 0` (`shared/jobs.py:1925`). **Nothing stops the run at the cap.** It keeps the GPU until it finishes or hits the 4 h limit.
- Nothing kills a run for passing the cap. `safety_kill` comes only from the `overrun_safety_kill` bucket, and nothing in this repo writes it (`shared/jobs.py:827-830`).
- If the run then succeeds, it settles at the capped figure (`shared/wallet.py:1013-1015`). The SQL writes `absorbed_variance` only when the **balance** is short (0045 :216-229), so the cost above the cap is **not recorded anywhere in the ledger**. The non-live path has the same clamp.

### Stuck-job sweeper

- It ends any `running` row whose `started_at` is over 6 h old (`cron/sweep_stuck_jobs.py:110-116`, :167-176). It looks at age only, not at the last heartbeat. A standalone BindCraft run reaches 6 h only if its 4 h FAILED webhook was lost.
- `timeout_stuck_job` first tries to recover a finished result. If it does, the run settles as succeeded through `complete_job` (`shared/jobs.py:1795-1814`). If not, `mark_timeout` gives `no_progress_timeout`, which is refunded (`shared/jobs.py:1827-1833`, :871-872). On a live run that is the same full return of the debits as above. The page says "Job timed out before producing a result." (`templates/job_detail.html:257-260`).

### Is the refund rule consistent with live debits?

**Yes.** Every class takes the same route for live and non-live runs (`shared/jobs.py:2484-2496`). Refunded classes give back everything the debits took (0045 :230-243). Billed classes settle at `live_due_usd(gpu_seconds)`: they charge the rest when the final cost is above the debits, and release the difference when it is below (0045 :200-243). No debit is kept on a refunded run.

### Findings for Leo (none fixed)

- **F1, money.** A live BindCraft run that hits the 4 h limit is fully refunded and its streamed designs are not shown. Ranomics absorbs about $14.80 of GPU per such run, and the customer gets nothing they can see. This exists today for non-live runs; live does not make it worse. The cheap fix would be for the timeout to post partial designs as a billed partial. **Not proposed in this PR.**
- **F2, money.** Past the cap the run keeps the GPU for free, up to 4 h, and the overage is not in the ledger. Same as non-live today.
- **F3, customer.** Every debit runs the auto-reload check (`shared/wallet.py:977-978` → :1789). A live run can therefore top up the card mid-run, and if the run is later refunded the money stays as wallet credit. This is the reason the test must avoid the owner's main account, which has auto-reload on.

## STEP 2 plan (needs Leo's yes in this session)

- Account: **the +t1 test account only**, and only after the rfdiffusion test session (local_d09a8b9f) is finished with it.
- Input: 4ZQK chain A, the target of job 1c4d5803.
- Stop: wait for the first streamed design, then press the job page's real Cancel button. `templates/job_detail.html:534` uses `window.confirm`, and a native dialog wedges the built-in browser tab, so first stub it in the page with `window.confirm = () => true`, then click. The report will say this method was used.
- Deadline: cancel at 30 min even with no design (about $3.15, and that run proves nothing).
- Record: job id, cost, kept designs, ledger lines. Leo runs the guarded balance script himself. I run no prod SQL.
- Balance needed: BindCraft is not on the live list in production (`shared/wallet_guard.py:57-58`), so the run takes the pre-run hold. That hold is 1.5 × the point estimate, clamped to the $8.00 cap (`shared/wallet_estimates.py:103`, :898-951), and the gate refuses a balance below it (`shared/wallet_guard.py:285-300`). At BindCraft's 3600 s baseline the hold is the full $8.00 (1.5 × $6.29, clamped); run history can lower it, never raise it. The guarded script refuses a ledger sum over $10, so set about $9.00, after the rfdiffusion test has finished with the account. The cancel releases whatever the hold holds above the billed time.
- A balance-run-out stop cannot be tested yet: the meter stops only live runs (`shared/jobs.py:1849-1974`), and BindCraft becomes live only once the STEP 3 change is merged and deployed. So the cancel test is the only test available now.

## STEP 2: paid cancel test, run 2026-10-15

Leo approved it directly in this session: "Yes: run the paid BindCraft cancel test now."

### What ran

- **Account:** the +t1 test account. Checked by fetching `/account` right before submitting (the only email on the page). Balance $9.00. `/account/wallet` read "Auto reload is off."
- **Job:** `d73cfaa5-b176-4b19-91ef-92ad13de00db`. Submitted 15:04 UTC; `started_at` 15:04:37 UTC (`/jobs/<id>/status.json`).
- **Inputs:** read from the form's own `FormData` just before submit: `preset` pilot, `target_pdb` 4ZQK.pdb at 309,744 bytes (the copy the rfdiffusion test downloaded from files.rcsb.org), `target_chain` A, `hotspot_residues` 54,56,115, binder length 50-100, `num_designs` 2. The form's own check read "Ready to run … Hotspots 54, 56, 115 — all preserved".
- **Watch:** a script in the job page polled `status.json` about every 16 s. It would cancel on the first entry in `partial_candidates`, or at 15:34 UTC if none had arrived.

### What happened

- **No design arrived.** Every poll showed `partial_candidates` empty, `designs_completed` 0 of 2, and stage "Running BindCraft". `gpu_seconds_used` rose from 3 to 1716.
- **The stop:** at 15:34:07 UTC the script set `window.confirm = () => true` and clicked the page's real Cancel button (`#cancel-job-btn`, `templates/job_detail.html:475`, handler :530-546). Status went to `cancelled`. The page reads "Cancelled after consuming 1716 GPU-seconds. Your wallet was charged for time used. Any surplus from the original hold was returned."
- **Ledger** (from `/account/wallet`, read after the cancel):

  | When (UTC) | Kind | Amount | Balance after |
  | --- | --- | --- | --- |
  | 2026-10-15 15:04 | hold, bindcraft | −$8.00 | $1.00 |
  | 2026-10-15 15:34 | hold release, bindcraft | +$5.00 | $6.00 |

  Net charge **$3.00**: 1716 s × $0.0017476 = $2.9989, which rounds to $3.00. The page's "Spent today $3.99" is this run's $3.00 plus the rfdiffusion test's $0.99. These are the page's rounded figures. The unrounded ledger rows need Leo's SQL, and I did not read them.
- **Kept designs:** none, because the run made none.
- **Not checked:** whether the Modal container actually stopped. The hub only shows the status change.

### Verdict

- **The cancel path works for BindCraft:** it charged the GPU time used and returned the rest of the hold.
- **The question this test was for is still open:** does a stopped BindCraft run keep the designs it has already sent? No design arrived in 28.6 GPU-minutes, so nothing was there to keep. The STEP 1 plan said a deadline stop with no design "proves nothing". Under the brief, STEP 3 does not start.
- **For STEP 3's start minimum:** the only earlier anchor, job `1c4d5803` on the same target, finished both trajectories in 1170 GPU-s (`docs/qa/RUNTIME-CEILING-2026-09-30.md:32`). This run reached 1716 s with none. The derived $1.0486 (600 s) is well below either figure.
- **A retry needs a top-up:** the balance is now $6.00, and the gate refuses a balance below the $8.00 hold (`shared/wallet_guard.py:285-300`).

## STEP 2 retry: paid cancel test with a 60-minute cut-off, run 2026-10-15

Leo approved it directly in this session: "Retry the BindCraft cancel test with a 60-minute cut-off, same inputs." He set +t1 to $9.00 with `docs/qa/T1-SET-BALANCE-9.00-BINDCRAFT.sql` (main checkout); the wallet shows that as a +$3.00 adjustment at 15:44 UTC.

### What ran

- **Account:** the +t1 test account, checked on `/account` right before submitting. Balance $9.00, "Auto reload is off".
- **Job:** `f8666763-0c13-40ad-85c4-f471c606aec6`. Submitted 15:50 UTC, `started_at` 15:50:30 UTC.
- **Inputs:** the same as d73cfaa5, read from the form's `FormData` before submit: `preset` pilot, `target_pdb` 4ZQK.pdb at 309,744 bytes, `target_chain` A, `hotspot_residues` 54,56,115, binder length 50-100, `num_designs` 2. The form's check read "Hotspots 54, 56, 115 — all preserved".
- **Submit:** two mouse clicks on Submit run did nothing, because the Chrome tab was hidden; `/jobs` showed no new job after either click. A third try clicked the same button (`#tool-submit-btn`, `templates/tools/bindcraft_form.html:331`) from a page script with `.click()`, and the job page loaded.
- **Watch:** the same in-page script as the first try, now with a 16:50 UTC cut-off. It polled `status.json` about every 16 s and was set to cancel on the first `partial_candidates` entry.

### What happened

- **A design arrived.** At 16:03:53 UTC the script saw one entry in `partial_candidates` at `gpu_seconds_used` 804: `design_l93_s938895_mpnn10_model1.pdb`, ipTM 0.83, pLDDT 0.96, i_pAE 0.16, `filter_status` pass. It set `window.confirm = () => true` and clicked the real Cancel button (`#cancel-job-btn`, `templates/job_detail.html:475`).
- **After the cancel**, `status.json` read `status` cancelled, `gpu_seconds_used` 804, progress 1 of 2 designs, and `partial_candidates` still held that one design. `/jobs` lists the run as cancelled, 13m 26s, $1.41.
- **Kept designs:** the page reads "Cancelled after consuming 804 GPU-seconds. Your wallet was charged for time used. Any surplus from the original hold was returned." and "Cancelled, 1 design kept. They are in your results and downloads." The candidates table shows the one row (ipTM 0.830, pLDDT 96.0). Every download link on the page was fetched with the session cookie, and each returned 200:

  | Download | Bytes | Content |
  | --- | --- | --- |
  | CSV | 350 | 3 lines (header, the one design, trailing newline) |
  | FASTA | 314 | 7 lines |
  | Structures (ZIP) | 62,724 | not opened |
  | .pdb (row link) | 283,023 | 3,492 `ATOM` lines |

- **Ledger** (from `/account/wallet`, read after the cancel):

  | When (UTC) | Kind | Amount | Balance after |
  | --- | --- | --- | --- |
  | 2026-10-15 15:44 | adjustment (Leo's script) | +$3.00 | $9.00 |
  | 2026-10-15 15:50 | hold, bindcraft | −$8.00 | $1.00 |
  | 2026-10-15 16:03 | hold release, bindcraft | +$6.59 | $7.59 |

  Net charge **$1.41**: 804 s × $0.0017476 = $1.4051, and the release is $8.00 − $1.4051 = $6.5949, shown as $6.59. "Spent today $5.40" is $3.00 + $0.99 + $1.41. These are the page's rounded figures; I did not read the unrounded rows.
- **Not checked:** whether the Modal container stopped. `gpu_seconds_used` stayed at 804 after the cancel, but that is the hub's record, not Modal's.

### Verdict

- **Passed: a cancelled BindCraft run keeps the designs it has already streamed.** The result page, `status.json`, the table and all four downloads still have the design. The cancel charged 804 GPU-s and returned the rest of the $8.00 hold.
- **What this run did not test:** the stop when the balance hits $0. That stop exists only for live runs (`shared/jobs.py:1849-1974`), and BindCraft is not live yet (`shared/wallet_guard.py:57-58`). This run used the cancel path; the test plan above said that is the only stop available before STEP 3.
- **First-design time, three runs on 4ZQK chain A:** 804 s or less (this run; the first poll that saw it), none by 1716 s (d73cfaa5, same inputs), and the whole run in 1170 s for 2 trajectories (1c4d5803, `docs/qa/RUNTIME-CEILING-2026-09-30.md:32`; its hotspots and lengths are not recorded there). A start minimum sized at 600 s ($1.0486) is below all three.
