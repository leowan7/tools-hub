# RFantibody $0 balance stop test (2026-10-29)

**PASS.** The run was stopped when the wallet reached $0.00, after 32 delivered
designs. The 32 designs are kept, scored and listed on the job page (CSV, FASTA
and Structures ZIP offered, none opened), and the ledger shows eight run debits
whose balance-after chain walks the whole $2.30 down to $0.00.
This is the second tool on which the live-charging $0 stop has been seen to
fire, after boltz2 on 10-23 (`docs/qa/ZERO-STOP-BOLTZ2-RERUN-2026-10-23.md`).

Leo approved one paid rfantibody run in this session's chat on 2026-10-29
("ok im signed in, go ahead with test 1"), and then re-approved the re-sized
count ("yes, go with 16") after the 20-design plan turned out to cross the
single-container ceiling. One run was made.

## Pre-checks

- **Build.** `https://tools.ranomics.com/health` returned 200 with
  `3a8de1fcd8b77770d19c5f98c2b04d7d1d19203f` (#464, the GPU-start heartbeat),
  read at 16:49:42 UTC, immediately before submit.
- **Account.** `leowan7+t1@gmail.com`, read from `/account`. Leo signed in
  himself; no password was typed by this session.
- **Balance before.** **$2.30**, spent today $0.00, auto reload off, read from
  `/account/wallet`. The $2.30 came from an ADJUSTMENT row at 16:20 UTC, which
  Leo made before this session started. No balance SQL was run here.
- **No other job in flight.** `/jobs` showed no pending or running job.
- **Re-sized from 20 to 16 designs.** The inherited plan said 20. At 20 the
  form showed "This count runs as a full-size run in 2 pieces" and offered
  "Start a full-size run" instead of "Submit run", because
  `single_container_ceiling('rfantibody', 'pilot')`
  (`shared/compute_campaigns.py:619`) is **16**. The full-size path is the
  campaign path, which holds money up front, and
  `LIVE_CAMPAIGN_TOOLS = frozenset({"proteina"})`
  (`shared/wallet_guard.py:96`, read by `shared/compute_campaigns.py:2003`)
  does not include rfantibody,
  so a 20-design submit would not have tested live charging at all. At 16 the
  notice was gone and the button read "Submit run".
- **The money should run out before the run finishes.** Measured in the repo
  venv (`venv/Scripts/python.exe`), calling `compute_hard_cap`,
  `live_start_usd` and `estimated_cost_for_tool` with the parameter shape
  `{'preset': 'pilot', 'target_chain': 'A', 'hotspot_residues': '54,56,115',
  'cdr_lengths': 'H1:8,H2:7,H3:10-16', 'num_designs': 16}`:

  ```
  estimate      : 11.65
  hard cap      : 104.00
  start min     : 2.00
  ```

  At the A100-40GB live rate, $2.30 buys about 1895 GPU-s. Fitting the two
  measured 115-aa rfantibody runs (594 s at 2 designs, 834 s at 4) gives about
  120 s/design plus 354 s fixed, so 16 designs projected to about 2274 s —
  longer than the money lasts. That projection was the basis for expecting the
  stop to fire, and it did, at 2089 s.
- `window.confirm = () => true` was set on the form page before submit
  (memory `cancel-button-blocked-by-window-confirm`). Nothing needed it.

## Run

- Form state read from the form's own elements before submit: `preset`
  `pilot`, `target_pdb` `4ZQK.pdb` (309744 bytes, fetched in-page from
  `https://files.rcsb.org/download/4ZQK.pdb` — the site has no
  `/static/example/4ZQK.pdb`), `target_chain` `A`, `hotspot_residues`
  `54,56,115`, `cdr_lengths` `H1:8,H2:7,H3:10-16`, `num_designs` `16`,
  `campaign_label` `T1 zero-stop rfantibody 2026-10-29`.
- The free preflight panel read: "Ready to run", "Target: 4ZQK.pdb, chain A —
  115 residues", "Hotspots 54, 56, 115 — all preserved", 10 residues dropped
  (waters / HETATM / incomplete backbone), chain B dropped, "Settings look
  good. Nothing was charged and no run was started."
- Job id: **`c7d77130-c60a-43ab-87e6-a64eddf4b5b5`**, submitted
  2026-10-29 16:50 UTC.

## Watch

An in-page watcher on `/account/wallet` polled `/jobs/<id>/status.json` and
`/account/wallet` every 15 s, writing each sample to `localStorage` so a page
reload could not lose the log. The log holds 119 samples, from 16:50:48 to
17:25:30 UTC. The balance column is the first dollar figure after CURRENT
BALANCE on `/account/wallet`.

The table shows samples where a value changed; samples in between match the row
above. `designs` is the length of `partial_candidates`.

| UTC | status | gpu_seconds_used | designs | balance | stage |
|---|---|---|---|---|---|
| 16:50:48 | running | 2 | 0 | — | — |
| 16:51:04 | running | 2 | 0 | — | Running RFdiffusion |
| 16:51:30 | running | 2 | 0 | $2.25 | Running RFdiffusion |
| 16:55:46 | running | 307 | 0 | $1.92 | Running RFdiffusion |
| 17:01:01 | running | 615 | 0 | $1.55 | Running RFdiffusion |
| 17:05:48 | running | 868 | 0 | $1.20 | Running RFdiffusion |
| 17:11:01 | running | 1176 | 0 | $0.81 | Running RFdiffusion |
| 17:12:46 | running | 1327 | 0 | $0.81 | Running ProteinMPNN |
| 17:14:31 | running | 1424 | 0 | $0.81 | Running RF2 validation |
| 17:15:30 | running | 1487 | 1 | $0.81 | Running RF2 validation |
| 17:16:14 | running | 1487 | 1 | $0.47 | Running RF2 validation |
| 17:17:14 | running | 1556 | 5 | $0.47 | Running RF2 validation |
| 17:18:13 | running | 1623 | 8 | $0.47 | Running RF2 validation |
| 17:19:12 | running | 1692 | 12 | $0.47 | Running RF2 validation |
| 17:20:00 | running | 1762 | 16 | $0.47 | Running RF2 validation |
| 17:20:31 | running | 1762 | 16 | $0.12 | Running RF2 validation |
| 17:21:01 | running | 1828 | 18 | $0.12 | Running RF2 validation |
| 17:21:16 | running | 1832 | 20 | $0.12 | Running RF2 validation |
| 17:22:16 | running | 1902 | 24 | $0.12 | Running RF2 validation |
| 17:23:30 | running | 1971 | 28 | $0.12 | Running RF2 validation |
| 17:24:30 | running | 2037 | 30 | $0.12 | Running RF2 validation |
| 17:24:45 | running | 2041 | 32 | $0.12 | Running RF2 validation |
| 17:25:30 | **succeeded** | 2089 | 32 | **$0.00** | Running RF2 validation |

The `gpu_seconds_used` series steps about 61 s per sample throughout
(2, 63, 124, 185, …, 1115, 1176). `docs/qa/RFANTIBODY-LIVE-CANCEL-2026-10-21.md`
records the same ~61 s heartbeat spacing on the 10-21 run; the writer of the
heartbeat is in llm-proteinDesigner and was not read from this repo, so the 61 s
figure is an observation from two runs, not a constant read from code. The first
sample carried no stage and no balance figure; both were present from 16:51:04
and 16:51:30 on.

- `gpu_seconds_used` rose smoothly from the first sample at 2 s, so the GPU
  start heartbeat from #464 stamped `started_at` right away.
- **The first design landed at 1487 GPU-s — about 24.5 minutes in.** Nothing
  was in `partial_candidates` for the first 24 minutes: RFdiffusion ran all 16
  backbones (to 1327 s), then ProteinMPNN, and only then did RF2 validation
  start streaming. `compute_charge_usd(1487, 'A100-40GB')` = $1.8049, so the
  $2.00 start minimum **does** cover time-to-first-design at this size, with
  about $0.19 of headroom.
- **This measures the derived figure in `shared/wallet_guard.py:125-133` and
  confirms it.** That docstring puts the stretch before rfantibody's first
  design at "about 1490 s, $1.81 at the A100-40GB rate, a derived figure that
  assumes RF2's time splits evenly over the 4 designs", extrapolated from a
  412-aa 4-design stage split. This run measured 1487 s and $1.8049 on a 115-aa
  16-design run — within 3 seconds of the derivation. The docstring also says
  "A run whose stretch before its first design costs more than $2.00 can still
  be stopped at $0 with no design"; this run does **not** exhibit that, because
  its stretch cost less than $2.00.
- Once RF2 started, designs streamed steadily rather than all at the end:
  1 → 5 → 8 → 12 → 16 → 18 → 20 → 24 → 28 → 30 → 32 across successive samples,
  i.e. 2 to 4 new candidates per 15 s sample for the last ten minutes.
- The 17:25 debit that took the balance to $0.00 and the flip to `succeeded`
  fell in the same 15 s sample, so the gap between them is under 15 s here.

## After it settled

- `status.json` (last read): `status` **succeeded**, `gpu_seconds_used`
  **2089**, `progress`
  `{designs_completed: 32, designs_total: 32, stage: "Running RF2 validation"}`,
  `partial_candidates` 32 entries each with a `pdb_key`
  (`samples_design_0_dldesign_0_best.pdb` and so on), an `i_pae` and a `plddt`
  (ranks 1 and 2 read ipAE 15.67 / pLDDT 89 and ipAE 4.69 / pLDDT 90). Payload
  keys: `gpu_seconds_used, has_bar, id, partial_candidates, passed_count,
  preset, progress, started_at, status, tool`. There is no `stop_reason` key
  here; as on boltz2, the stop reason lives in the job's `result`.
- **Job page:** "Status succeeded", "Stopped: your wallet balance reached $0.
  Add funds", "This run stopped when your wallet balance reached $0, after 32
  designs. The finished designs are in your results and downloads. Add funds
  to run again.", "Candidates (32)", 32 rows with View 3D, and Download CSV,
  FASTA and Structures (ZIP) offered. I did not open any download. The header
  read $0.00.
- **`/account/wallet` ledger for this job**, as the page rounds it:

  | When | Kind | Amount | Balance after |
  |---|---|---|---|
  | 2026-10-29 16:50 | RUN DEBIT | −$0.05 | $2.25 |
  | 2026-10-29 16:55 | RUN DEBIT | −$0.32 | $1.92 |
  | 2026-10-29 17:00 | RUN DEBIT | −$0.37 | $1.55 |
  | 2026-10-29 17:05 | RUN DEBIT | −$0.35 | $1.20 |
  | 2026-10-29 17:10 | RUN DEBIT | −$0.39 | $0.81 |
  | 2026-10-29 17:15 | RUN DEBIT | −$0.34 | $0.47 |
  | 2026-10-29 17:20 | RUN DEBIT | −$0.35 | $0.12 |
  | 2026-10-29 17:25 | RUN DEBIT | −$0.12 | $0.00 |
  | 2026-10-29 17:25 | ABSORBED VARIANCE | $0.00 | $0.00 |
  | 2026-10-29 17:25 | CHARGE | $0.00 | $0.00 |

  The printed amounts sum to **$2.29**, while the balance-after chain walks
  $2.30 → $2.25 → $1.92 → $1.55 → $1.20 → $0.81 → $0.47 → $0.12 → $0.00, a
  travel of exactly $2.30, and "Spent today" reads **$2.30**. The one-cent gap
  is the page's rounding, not a billing gap: amounts are rounded half-up and
  balances are floored (memory `wallet-money-display-rounds-by-design`).
- **Balance after = $0.00**, spent today **$2.30**, read from
  `/account/wallet`. It did not go negative.
- **Metered vs billed.** `compute_charge_usd(2089, 'A100-40GB')` at the
  A100-40GB live rate is about $2.535, and $2.30 was billed, so roughly
  $0.23 of metered time went unbilled because the balance ran out. The
  ABSORBED VARIANCE row renders $0.00; its raw amount needs read-only SQL.
  **Unverified.**

## Verdict against the brief's criteria

| Criterion | Result | Evidence |
|---|---|---|
| Stopped when the balance reached $0 | yes | job page "Stopped: your wallet balance reached $0 … after 32 designs"; status `succeeded` and balance $0.00 in the 17:25:30 sample |
| Not negative beyond rounding | yes, $0.00 | `/account/wallet` CURRENT BALANCE |
| Designs before the stop kept and visible | yes, 32 | job page "Candidates (32)", 32 View 3D rows, `partial_candidates` 32 with `pdb_key` |
| Downloads offered | yes | CSV, FASTA, Structures (ZIP) on the job page; none opened |
| Ledger shows the charge | yes | eight RUN DEBIT rows, balance chain $2.30 → $0.00, spent today $2.30 |

**PASS.** Job `c7d77130-c60a-43ab-87e6-a64eddf4b5b5`, $2.30 spent, 2089 GPU-s,
32 designs kept out of the up-to-80 the run could have produced: the form field
is a backbone count, and stage 2 runs ProteinMPNN at `seqs_per_backbone=5`
(`tools/rfantibody/meta.py:170-184`, which cites
`llm-proteinDesigner/docker/rfantibody/run_pipeline.py`), so 16 backbones score
80 candidates.

## Not covered

- **The top-up email on a balance stop.** Gated on `stopped_for_balance`; this
  session cannot read `leowan7+t1@gmail.com`.
- **The downloads.** Offered, none opened, so the files themselves are
  unverified.
- **Unrounded ledger amounts** and the ABSORBED VARIANCE figure need read-only
  SQL.
- **The Modal side.** The job row went terminal. That the GPU container was
  actually cancelled and stopped billing Ranomics was not checked against
  Modal.
- **`passed_count`.** The key is present in `status.json`; its value was not
  recorded.
- **Other presets and tools.** This proves the stop on rfantibody `pilot` at
  16 designs only.

## Review of this report

Both saved reviewers were run on the two-commit delta (base `045a61dc`) before
it was pushed.

- **`review-code`: no findings.** The delta is two prose files; there is no code
  behaviour in it. It also noted that the two-dot diff against `origin/main`
  shows #468's backfill changes reversed, because `origin/main` has moved past
  this branch's merge base (`3a8de1fc`). That is base drift, not a change this
  branch makes; the PR diff is docs-only, and the branch was not rebased
  (memory `worktree-base-drift`).
- **`review-claims`: three FALSE, all fixed, all the same error.** The first
  draft claimed the $2.00 start minimum "would not have covered"
  time-to-first-design, and carried a follow-up headed "does not cover a first
  design at this size". Both were backwards: $1.8049 is **under** $2.00, as the
  same report's own $0.19-of-headroom figure said. The third was a quotation
  ("may not cover time-to-first-design") that is not in the
  `shared/wallet_guard.py` docstring. The direction of the comparison and the
  quoted wording are corrected above; the heading now says the minimum clears
  it by $0.19 and labels the behaviour at exactly $2.00 unverified. The
  reviewer verified 17 claims, including every figure re-run in the repo venv,
  and left 7 unverified (live-run evidence outside the repo, the external
  llm-proteinDesigner cites, and the 10-21 runtime figures); those are listed
  in "Not covered" and labelled inline.

## Follow-ups found in passing (not fixed)

- **`designs_total` is not the plan.** The first sample read
  `{designs_completed: 0, designs_total: 16}` and the last read
  `{designs_completed: 32, designs_total: 32}`, so on rfantibody the total is
  rewritten as candidates stream. This rests on the watcher samples alone: the
  writer of `designs_total` is not in this repo (grepped `shared/jobs.py`,
  `tools/rfantibody` and the shared code), and is presumably in
  llm-proteinDesigner. The job page therefore shows no
  "Returned 32 / 80" line, unlike boltz2's "Returned 8 / 16" — only the stop
  notice tells the user the run was cut short. A user who missed the notice
  cannot tell from the count that 48 candidates are missing.
- **The $2.00 rfantibody start minimum clears a first design at this size by
  only $0.19.** First design at 1487 GPU-s is $1.8049 of metered time, and that
  is after RFdiffusion has run all 16 backbones. A 16-design run started at
  exactly $2.00 would have about $0.19 of headroom past the first design — one
  design delivered, then the stop. The margin is thin and the $2.00 figure does
  not scale with `num_designs`, so a larger run started at the minimum would
  plausibly land on the wrong side of it and deliver nothing. Not measured
  here: this run started at $2.30, and the behaviour at exactly $2.00 is
  **unverified**.
- **The stop notice names designs, not backbones.** "after 32 designs" counts
  candidates; the user asked for 16 backbones. Not wrong, but the two counts
  are never shown side by side.
- The test account is now at $0.00. Test 2 (pxdesign) needs Leo to re-apply his
  balance adjustment before it can start.
