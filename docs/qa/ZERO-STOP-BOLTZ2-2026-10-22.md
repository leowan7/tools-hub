# Boltz-2 $0 balance stop test (2026-10-22)

**FAIL — the stop was never reached.** The run billed $0.40 of the $1.50 balance
and finished all 16 designs. The reason is a defect, not a mis-sized test: the
live charge for boltz2 is clamped to the **1-design** hard cap of $0.40 no matter
how many binders the run folds, so a boltz2 run cannot drive a wallet to $0
from any balance above $0.40. Detail in **The defect** below.

Leo approved one paid run, worst case about $1.50, relayed from the hub-lead
chat of 2026-10-22. One run was made. No retry (the brief says report, do not
retry).

## Pre-checks

- `/health` build = `66109abeb1bf3848082ec3e6a7b51fcf96d91ec1`, read by
  `curl https://tools.ranomics.com/health` at 18:36 UTC. This is the #447
  retrigger commit, **not** `fe47404e` as the brief asked for.
  Leo was shown the evidence below and chose to go on this build.
  - `gh run list --branch main` + `gh run view --log-failed`: the scheduled
    "Synthetic smoke (Platform API)" workflow failed at 16:09 UTC with
    `DEPLOY DRIFT -- production is running 66109ab but fe47404 landed on main
    25 minutes ago and never shipped`, naming `fe47404` (#449), `87e18b9`
    (#446) and `8ddb05e` (#448) as merged and not in production.
  - `git diff --stat 66109abe origin/main` in this worktree: the undeployed
    delta is `app.py` (+1), `cron/sweep_stuck_jobs.py`,
    `docs/design/LIVE-CHARGING-2026-10-01.md`, `shared/wallet_guard.py` and
    tests. Nothing under `tools/boltz2/` and nothing in
    `shared/jobs.py::meter_live_runs`, so the stop path under test is the same
    code on either build.
- Account = `leowan7+t1@gmail.com`, read from `/account` in Leo's Chrome.
- Balance before = **$1.50**, spent today $0.00, auto reload off, read from
  `/account/wallet`. Leo had just run
  `docs/qa/T1-SET-BALANCE-1.50-ZERO-STOP.sql` and reported "done, balance is
  $1.50"; the ledger row `2026-10-22 18:30 ADJUSTMENT −$12.30 $1.50` on
  `/account/wallet` is that script.
- `window.confirm = () => true` was stubbed from a page script before any click
  (memory `cancel-button-blocked-by-window-confirm`). Nothing in this run needed
  it: there was no cancel.

## Why boltz2 / msa_server / 16 binders was chosen

- boltz2 is in `_LIVE_ALL_PRESETS` (`shared/wallet_guard.py:80`), so every
  preset charges live, and it is named in no branch of `live_start_usd`, so it
  reaches the fall-through `return Decimal("0")` at `shared/wallet_guard.py:203`
  (under `if tool_slug != "rfdiffusion":`, line 202) and a $1.50 balance starts
  the run. proteina was rejected because the largest startable atomic proteina run
  prices at about $1.01–$1.53, which is not clearly above $1.50.
- `tools/boltz2/run_pipeline.py:749` sets `chunk_size = 1 if msa_server else
  FOLD_CHUNK` (`FOLD_CHUNK = 10`, line 120), so that preset folds **one design
  per process** and uploads each design as it lands. That is what the
  "keeps its designs" half of the test needs.
- 16 is the preset's own ceiling (`MAX_BINDERS_BY_PRESET = {"msa_server": 16}`,
  `tools/boltz2/__init__.py:196`, read at line 341 and enforced at line 342).
- The expectation, from the 214 s/design aggregate measured on the 2026-09-21
  Rung B row of `docs/VALIDATION-LOG.md` (643 s of pipeline runtime over 3
  designs, `msa_server`): 16 × 214 s = 3424 GPU-s = $4.16, so the
  balance should run out at about 1236 GPU-s, roughly 20 minutes in, after
  about 5 designs. **That expectation was wrong**, because it assumed the cap
  scaled with the design count. See **The defect**.

## Run

- Form state read from the form's own elements immediately before submit:
  `preset` `msa_server`, `target_pdb` `1HEW.pdb` (132266 bytes),
  `target_chain` `A`, `hotspot_residues` empty, 16 binder records,
  hidden `n_designs_total` `16`.
- Binders: the 129-aa chain A sequence of `static/example/1HEW.pdb` (hen
  egg-white lysozyme) with one position mutated to alanine per record, named
  `b01`–`b16`. Natural sequence, 16 distinct names and sequences — chosen only
  to fill the batch, not as designs.
- Submit clicked at about **18:36:30 UTC** (`date -u` read 18:36:16 Z
  immediately before the click).
- Job id: **`61e14125-fb9d-4132-9128-31f5caca38af`**.

## Watch

From an in-page watcher that polled `/jobs/<id>/status.json` and
`/account/wallet` every 15 s. The balance column is the first dollar figure on
`/account/wallet`, which is CURRENT BALANCE.

| UTC | status | gpu_seconds_used | designs in partial_candidates | balance |
|---|---|---|---|---|
| 18:37:08 | running | 1 | 0 | $1.50 |
| 18:38:08 | running | 1 | 0 | $1.50 |
| 18:40:19 | running | 107 | 1 | $1.50 |
| 18:41:19 | running | 208 | 2 | $1.21 |
| 18:42:19 | running | 310 | 3 | $1.21 |
| 18:43:22 | running | 310 | 3 | $1.21 |
| 18:44:22 | running | 407 | 4 | $1.21 |
| 18:44:37 | running | 407 | 4 | $1.21 |
| 18:47:19 | running | 509 | 5 | $1.10 |
| 18:49:19 | running | 741 | 7 | $1.10 |
| 18:50:19 | running | 741 | 7 | $1.10 |
| 18:51:37 | running | 835 | 8 | $1.10 |
| 18:52:22 | running | 835 | 8 | $1.10 |
| 18:55:19 | running | 1035 | 10 | $1.10 |
| 18:58:19 | running | 1280 | 12 | $1.10 |
| 18:59:19 | running | 1280 | 12 | $1.10 |
| 19:08:07 | succeeded | 1709 | 16 | $1.10 |

Every row above is a sample this session read and recorded at the time; the
poller ran more often than the rows shown, and gaps are samples whose output
was not kept, not pauses.

- Designs streamed one at a time, about 100 s each, and each appeared in
  `partial_candidates` with its own scores (`iptm`, `ptm`, `complex_plddt`,
  `pdb_key` like `b01_complex.pdb`) while the run was still going. So the
  per-design streaming the stop depends on does work on this preset.
- `started_at` was already set and `gpu_seconds_used` already non-null at the
  first sample, 38 s after submit.
- The balance stopped moving after the 18:45 debit while GPU seconds ran on from
  509 to 1709. That is the cap being reached, not a missed cron tick.
- The watcher's own log was lost when the job page reloaded itself on
  completion. Every row above was captured in the test transcript before that,
  and the final row was re-read directly from `status.json`.

## After it settled

- `status.json` at 19:08:07 UTC: `status` **succeeded**, `gpu_seconds_used`
  **1709**, `partial_candidates` 16 entries, `passed_count` 0,
  `progress` `{designs_completed: 16, designs_total: 16, stage: "complete"}`.
  There is no `stop_reason` key in the payload.
- Job page: "SUCCEEDED", "Completed in 1709 GPU-seconds.", "CANDIDATES (16)",
  "Returned 16 / 16", with Download CSV, FASTA and Structures (ZIP) offered.
  No balance-stop notice, which is correct — nothing stopped it.
- `/account/wallet` ledger for this job, as the page rounds it:

  | When | Kind | Amount | Balance after |
  |---|---|---|---|
  | 2026-10-22 18:36 | HOLD | $0.00 | $1.50 |
  | 2026-10-22 18:40 | RUN DEBIT | −$0.29 | $1.21 |
  | 2026-10-22 18:45 | RUN DEBIT | −$0.11 | $1.10 |
  | 2026-10-22 19:05 | CHARGE | $0.00 | $1.10 |

- Balance after = **$1.10**, spent today **$0.40**, both read from
  `/account/wallet`.
- The job page's "Screen 16 more" block carries "Top up your wallet to run
  this", so the low-balance prompt does render at $1.10.

## The defect

`shared/jobs.py::meter_live_runs` prices the run with
`live_due_usd(job.tool, elapsed, gpu_class, params)` where `params = _tool_params(job)`
(`shared/jobs.py:2624`) — the job's stored inputs with underscore keys dropped.
`shared/wallet.py::live_due_usd` (line 935) is
`min(compute_charge_usd(...), compute_hard_cap(...))`, and `compute_hard_cap`
scales the base cap by `_effective_scaling_value(spec, params)`, which reads
`params.get(spec.scaling_param)` at the **top level**
(`shared/wallet_estimates.py:1212`). boltz2's `scaling_param` is
`n_designs_total` (`shared/wallet_estimates.py:425`) with
`designs_per_run_baseline=1` (line 424) and `base_hard_cap_usd` $0.40 (line
428), but `tools/boltz2/__init__.py:418` stores the count **nested**, as
`"parameters": {"n_designs_total": len(binders)}`. So the lookup misses, the
value defaults to the baseline 1, and the cap stays at the base $0.40.

Measured in the repo venv against the two param shapes. This is re-runnable —
the command and its whole output:

```
$ venv/Scripts/python.exe -c "
from shared.wallet_estimates import compute_hard_cap
from shared.wallet import live_due_usd, compute_charge_usd
stored = {'preset':'msa_server','target_chain':'A','parameters':{'n_designs_total':16}}
flat   = {'preset':'msa_server','target_chain':'A','n_designs_total':16}
print('cap as stored :', compute_hard_cap('boltz2', stored))
print('cap flat      :', compute_hard_cap('boltz2', flat))
print('charge 1709s  :', compute_charge_usd(1709, 'A100-40GB'))
print('due as stored :', live_due_usd('boltz2', 1709, 'A100-40GB', stored))
print('due flat      :', live_due_usd('boltz2', 1709, 'A100-40GB', flat))
"
cap as stored : 0.4000
cap flat      : 6.4000
charge 1709s  : 2.0744
due as stored : 0.4000
due flat      : 2.0744
```

`stored` is the shape `validate()` returns and `_tool_params` hands the meter;
`flat` is the same count at the top level. The gpu_class is boltz2's own,
A100-40GB (`shared/wallet_estimates.py:380`).

That matches the ledger exactly: debits $0.29 + $0.11 = **$0.40**, then
`debit["short"]` is 0 forever, so the stop branch is never entered.

Consequences, in order of size:

1. **Revenue.** This run consumed 1709 GPU-s. At the A100-40GB rate in
   `shared/wallet.py` that is $2.0744 of metered time, and $0.40 was billed.
   Ranomics absorbed about $1.67 on one 16-design run, and the gap grows with
   batch size.
2. **The $0 stop is unreachable on boltz2** above a $0.40 balance, which is why
   this test could not run. It is not a defect in the stop code — the stop code
   was never reached.
3. The same shape appears for the other three tools whose `scaling_param` is
   `n_designs_total` and which also store it nested: `tools/af2/__init__.py:351`,
   `tools/colabfold/__init__.py:343`, `tools/esmfold/__init__.py:231`. Those
   three charge live only on the `batch` preset (`_LIVE_BATCH_ONLY` in
   `shared/wallet_guard.py`). **Unverified**: I read the storage shape only; I
   did not price those tools or run one.

No product code was changed in this session, per the brief.

## Not covered

- **The $0 balance stop itself.** Still unproven on every tool. A test needs a
  tool whose live charge is not clamped below the balance — or this cap defect
  fixed first, after which boltz2 at 16 binders would owe $2.07 against $1.50
  and stop at about 1236 GPU-s.
- **Designs kept across a stop.** Not exercised: nothing stopped. The run did
  show that designs stream individually on `msa_server` and that
  `partial_candidates` carries full per-design scores mid-run, which is the
  precondition.
- **The top-up email on a balance stop.** The stop-specific tone and the
  `topup_url` in `shared/email.py` (lines 213 and 233) are both gated on
  `stopped_for_balance(job)` (`shared/run_notices.py:28`), which needs the
  stop. Not exercised.
- **Unrounded ledger figures.** All amounts above are as `/account/wallet`
  renders them. Reading the raw rows needs Leo's read-only SQL.

## Follow-ups found in passing (not fixed)

- The preflight panel on both the form and the job page reads "Target:
  1HEW.pdb, chain A — 0 residues" while the results panel on the same job page
  reads "ANTIGEN LENGTH 129 aa". 129 is right. **Not new**: the same pair was
  recorded as free finding (iv) on the 2026-09-23 Rung C row of
  `docs/VALIDATION-LOG.md`, with root cause not investigated then either. This
  run is a second sighting, on a different target.
- The Railway deploy drift above: #446, #448 and #449 were merged and never
  shipped, and the scheduled synthetic smoke has been failing on main for that
  reason (02:08, 10:44 and 16:09 UTC on 10-22). Hub-lead's lane, recorded here
  because it changed which build this test ran on.

**FAIL** — job `61e14125-fb9d-4132-9128-31f5caca38af`, 1709 GPU-s, 16/16
designs delivered, charged **$0.40** of the $1.50 balance, balance $1.50 →
$1.10. The $0 stop was not reached and could not be, because boltz2's live
charge is capped at the 1-design $0.40 regardless of batch size.
