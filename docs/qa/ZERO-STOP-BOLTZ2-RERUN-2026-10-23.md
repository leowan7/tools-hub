# Boltz-2 $0 balance stop test, re-run (2026-10-23)

**PASS.** The run was stopped when the wallet reached $0.00 after 8 of its 16
designs. The 8 delivered designs are kept, scored and downloadable on the job
page, and the ledger shows four run debits totalling $1.10, the whole balance.
This is the first tool on which the live-charging $0 stop has been seen to fire.

This re-runs the failed 2026-10-22 test (`docs/qa/ZERO-STOP-BOLTZ2-2026-10-22.md`)
after #451 fixed the hard cap reading boltz2's nested design count. Leo approved
one paid run in this session's chat on 2026-10-23 ("yes, run the $0 stop
re-run"). One run was made.

## Pre-checks

- **Build.** `curl https://tools.ranomics.com/health` read
  `25ea4ccd3f729d2361bd73be96750407a1b3348b` (#451) at 14:30 UTC, then
  `edd6b5cf21bdd13a377b779923b8bf535c59a4fb` (#452) at 14:44:36 UTC, read by a
  polling loop on the same URL. The run was submitted after that, on
  `edd6b5cf`, which contains #451.
- **Account.** `leowan7+t1@gmail.com`, read from `/account` in Leo's Chrome.
- **Balance before.** **$1.10**, spent today $0.00, auto reload off, read from
  `/account/wallet` at about 14:32 UTC. Inside the $0.50–$10 range in the brief. No
  balance SQL was run.
- **The cap is now above the balance.** Re-derived from the code at
  `origin/main` = `edd6b5cf`:
  - `tools/boltz2/__init__.py:418` stores `"parameters": {"n_designs_total": len(binders)}`.
  - `shared/jobs.py:1922` prices the meter with `params = _tool_params(job)`,
    which drops only underscore keys (`shared/jobs.py:2624`), so `parameters`
    reaches the cap.
  - `shared/wallet_estimates.py:1217-1220` (#451) reads the scaling key top
    level first, then under `parameters`.
  - `shared/wallet.py:944-947`: `live_due_usd` = `min(compute_charge_usd, compute_hard_cap)`.
  - `shared/jobs.py:1933` skips the stop unless `debit["short"] > 0`.
  - `shared/wallet_guard.py:80-91` lists boltz2 in `_LIVE_ALL_PRESETS`, and
    `live_start_usd` reaches `return Decimal("0")` at `shared/wallet_guard.py:203`
    for boltz2, so a $1.10 balance can start the run.

  Measured in the repo venv (`venv/Scripts/python.exe -c ...` calling
  `compute_hard_cap`, `live_start_usd`, `compute_charge_usd`, `live_due_usd`
  with the stored shape `{'preset':'msa_server','target_chain':'A','parameters':{'n_designs_total':16}}`):

  ```
  cap as stored : 6.4000
  start min     : 0
  charge 1709s  : 2.0744
  due as stored : 2.0744
  ```

  So a 16-design run can owe up to $6.40, well above $1.10. At the
  A100-40GB rate, `compute_charge_usd(906, 'A100-40GB')` = $1.0997, so the
  balance should run out at about 906 GPU-s, after about 8–9 designs at the
  ~107 s/design measured on 10-22.
- `window.confirm = () => true` was set on the form page before submit
  (memory `cancel-button-blocked-by-window-confirm`). Nothing needed it.

## Run

- Form state read from the form's own elements before submit: `preset`
  `msa_server`, `target_pdb` `1HEW.pdb` (130653 bytes, fetched from the site's
  `/static/example/1HEW.pdb`), `target_chain` `A`, `hotspot_residues` empty,
  16 FASTA records, hidden `n_designs_total` `16`. The 10-22 report gives 132266
  bytes for the same file. The difference was not investigated (probably line
  endings).
- Binders: the 129-aa chain A of 1HEW with one alanine substitution each,
  `b01`–`b16`, at the first 16 non-alanine positions: K1A, V2A, F3A, G4A, R5A,
  C6A, E7A, L8A, M12A, K13A, R14A, H15A, G16A, L17A, D18A, N19A. The 10-22
  report does not record its positions, so these may differ from it. The
  binders are only there to fill the batch.
- **Submit was clicked by Leo**, not by this session: the session's permission
  check refused the click. The form had been filled in by this session and was
  not changed after that.
- Job id: **`c5ad001c-4991-4d94-ac92-99b78f124ed4`**, `started_at`
  `2026-10-23T14:54:37.27667+00:00` (from `status.json`).

## Watch

An in-page watcher on `/account/wallet` polled `/jobs/<id>/status.json` and
`/account/wallet` every 15 s. It wrote each sample to `localStorage`, so a page
reload could not lose the log. The log holds 71 samples, from 14:55:27 to
15:13:41 UTC. The spacing ran a little over 15 s because each sample waited for
both fetches.
The table shows every sample where a value changed. All the samples in between
match the row above them. The balance column is the first dollar figure on
`/account/wallet`, which is CURRENT BALANCE.

| UTC | status | gpu_seconds_used | designs in partial_candidates | balance |
|---|---|---|---|---|
| 14:55:27 | running | 4 | 0 | $1.10 |
| 14:56:12 | running | 4 | 0 | $0.99 |
| 14:56:42 | running | 113 | 1 | $0.99 |
| 14:58:28 | running | 226 | 2 | $0.99 |
| 15:00:27 | running | 226 | 2 | $0.68 |
| 15:00:42 | running | 355 | 3 | $0.68 |
| 15:02:28 | running | 463 | 4 | $0.68 |
| 15:04:12 | running | 574 | 5 | $0.68 |
| 15:05:57 | running | 574 | 5 | $0.28 |
| 15:06:27 | running | 709 | 6 | $0.28 |
| 15:08:42 | running | 836 | 7 | $0.28 |
| 15:10:59 | running | 975 | 8 | **$0.00** |
| 15:11:27 | **succeeded** | 979 | 8 | $0.00 |
| 15:13:41 | succeeded | 979 | 8 | $0.00 |

- The balance now falls on each meter tick, about every 5 minutes, past the
  $0.40 at which it froze on 10-22. The cap fix works on production.
- The 15:10 RUN DEBIT that took the balance to $0.00 (see the ledger below)
  landed between the 15:10:54 and 15:10:59 samples. The 15:10:59 sample
  already reads $0.00 while the status is still `running`. The status flipped
  to `succeeded` between the 15:10:59 and 15:11:27 samples, so less than
  33 s after the debit. Designs 9–16 never appeared.

## After it settled

- `status.json` (last sample): `status` **succeeded**, `gpu_seconds_used`
  **979**, `passed_count` 0, `progress`
  `{designs_completed: 8, designs_total: 16, stage: "folding"}`,
  `partial_candidates` 8 entries `b01`–`b08`, each with an `ipTM` (0.135–0.439)
  and a `pdb_key`. The payload's keys, as the watcher read them, were
  `gpu_seconds_used, has_bar, id, partial_candidates, passed_count, preset,
  progress, started_at, status, tool`. There is no `stop_reason`. The stop
  reason lives in the job's `result` instead.
- `succeeded` is what the stop writes: `shared/jobs.py:1964-1973` completes the
  job with `terminal_status="succeeded"` and a result of `partial: True,
  stop_reason: WALLET_STOP_REASON` (`"wallet_empty"`, `shared/jobs.py:1847`).
- **Job page:** "SUCCEEDED", "Stopped: your wallet balance reached $0. Add funds",
  "This run stopped when your wallet balance reached $0, after 8 designs. The
  finished designs are in your results and downloads.", "CANDIDATES (8)",
  "Returned 8 / 16", 8 rows each with a View 3D and a .pdb link, and Download CSV,
  FASTA and Structures (ZIP) offered. The notice text is what
  `stopped_for_balance` (`shared/run_notices.py:28`) gates on. I did not open
  any of the downloads.
- **`/account/wallet` ledger for this job**, as the page rounds it:

  | When | Kind | Amount | Balance after |
  |---|---|---|---|
  | 2026-10-23 14:54 | HOLD | $0.00 | $1.10 |
  | 2026-10-23 14:56 | RUN DEBIT | −$0.10 | $0.99 |
  | 2026-10-23 15:00 | RUN DEBIT | −$0.31 | $0.68 |
  | 2026-10-23 15:05 | RUN DEBIT | −$0.40 | $0.28 |
  | 2026-10-23 15:10 | RUN DEBIT | −$0.29 | $0.00 |
  | 2026-10-23 15:11 | ABSORBED VARIANCE | $0.00 | $0.00 |
  | 2026-10-23 15:11 | CHARGE | $0.00 | $0.00 |

  Debits total $0.10 + $0.31 + $0.40 + $0.29 = **$1.10**. The 14:56 debit
  shows −$0.10 while the balance moved $1.10 → $0.99, and the 15:10 debit
  shows −$0.29 while it moved $0.28 → $0.00. Both gaps are the page's
  rounding: amounts are rounded half-up and balances are floored (memory
  `wallet-money-display-rounds-by-design`). So $1.10 is a sum of rounded
  figures. The exact total needs read-only SQL.
- **Balance after = $0.00**, spent today **$1.10**, read from `/account/wallet`.
  It did not go negative.
- **Metered vs billed.** `compute_charge_usd(979, 'A100-40GB')` = $1.1883, and
  $1.10 was billed, so about $0.09 of metered time went unbilled because the
  balance ran out. The ABSORBED VARIANCE row renders $0.00. Its raw amount
  needs Leo's read-only SQL. **Unverified.**

## Verdict against the brief's criteria

| Criterion | Result | Evidence |
|---|---|---|
| Stopped when the balance reached $0 | yes | balance $0.00 first seen at 15:10:59 (status still running); status `succeeded` by 15:11:27; 8/16 designs |
| Not negative beyond rounding | yes, $0.00 | `/account/wallet` CURRENT BALANCE |
| Designs before the stop kept and visible | yes, 8 | job page "CANDIDATES (8)", `partial_candidates` 8 with `pdb_key` |
| Ledger shows the charge | yes | four RUN DEBIT rows, $1.10 total |

**PASS.** Job `c5ad001c-4991-4d94-ac92-99b78f124ed4`, $1.10 spent, 979 GPU-s,
8 of 16 designs kept.

## Not covered

- **The top-up email on a balance stop.** Gated on `stopped_for_balance` and
  should have been sent to `leowan7+t1@gmail.com`. Not checked: this session
  cannot read that inbox.
- **The downloads.** CSV, FASTA and ZIP are offered. None was opened.
- **Unrounded ledger amounts** and the ABSORBED VARIANCE figure need read-only
  SQL.
- **The Modal side.** The job row went terminal. That the GPU container was
  actually cancelled (`shared/jobs.py:1941`) and stopped billing Ranomics
  was not checked against Modal.
- **Other tools.** This proves the stop on boltz2 `msa_server` only.

## Follow-ups found in passing (not fixed)

- The preflight panel again reads "Target: 1HEW.pdb, chain A — 0 residues".
  This is the third sighting (10-22 report, `docs/VALIDATION-LOG.md` 2026-09-23
  Rung C finding iv). New this time: the results panel reads "ANTIGEN LENGTH
  — aa", where on 10-22's completed run it read "129 aa". So the antigen
  length is missing on a stopped run.
- The test account is now at $0.00. Any further T1 test needs a top-up or an
  adjustment from Leo.
