# Failed runs: whose fault? (2026-09-30)

Read-only triage for Leo's 2026-09-30 question, "should we refund when we did nothing wrong?". Nothing was re-run, refunded, emailed or written to the database.

## Method and sources

- **Data:** the Supabase dashboard SQL editor, logged in as Leo. Every query ran inside `begin transaction read only; … commit;`. There are four queries:
  - **Q1** returns all 58 failed, timed-out or unclassified `tool_jobs` rows (all time), with error, GPU-seconds, and the wallet rows linked through `inputs->'_wallet'->>'hold_tx_id'`.
  - **Q2** returns each customer row's `inputs`, trimmed, without `_wallet`.
  - **Q3** returns counts per tool, status, `use_templates`, framework and preset, across ALL runs, successes included.
  - **Q4** returns wall seconds from `started_at` to `completed_at` for each customer row.
- **Scope:** all time. The first failed row is 2026-04-29 (Q1). The WHERE clause is `status in ('failed','timeout') or failure_class in (no_progress_timeout, unclassified, infra_crash, tool_error, preflight_miss)`, plus cancelled rows with a null class. User-cancelled runs are billed and are not in the tables below.
- **Excluded accounts:** 23 rows from Leo's two accounts (the ranomics.com address and the personal gmail).
- **Included but flagged:** one Firefox Relay (mozmail) address with 4 rows. It *may* be one of the audit accounts that docs/FUNNEL-2026-09-28.md excluded, but that is unverified. Totals are given with and without it.
- **Refunded price:** the `hold_release` amount in `wallet_transactions` (Q1).
- **Our Modal cost:**
  - Formula: seconds × $0.001028/s, which is the A100-80GB rate at `shared/wallet.py:182`, also used as `DEFAULT_USD_PER_SECOND`.
  - The GPU class is not recorded on these rows (Q4 `gpu_class` = `?`), so **every Modal figure is an upper-bound estimate**.
  - Seconds come from `gpu_seconds_used` when it is set, otherwise from wall time (Q4).
  - Two af2 hangs have unknown GPU time and are counted as $0. Their wall time was about 6 h, which is the stuck-job sweeper and not GPU time.
- **Refund policy under review:** `shared/jobs.py:686-718` (origin/main `d5c1c116`).

## Totals per group (35 customer runs)

| Group | Runs | Refunded / absorbed customer price | Our Modal cost (upper bound) |
|---|---|---|---|
| OUR FAULT | 29 | $150.91 | $53.29 (2 runs unknown) |
| CUSTOMER INPUT | 1 | $15.00 | $0.13 |
| UNKNOWN | 5 | $4.87 | $1.02 |
| **All** | **35** | **$170.78** | **$54.43** |

- Without the mozmail account, OUR FAULT is 25 runs and $150.28.
- The refunded / absorbed figure includes $3.63 absorbed on 763247f5. That run was billed, but the charge exceeded the customer's balance and the rest was absorbed (`absorbed_variance`, Q1).

**Answer to Leo's question:** on this data, the refund rule almost never refunded a customer's mistake. Only one run in 35 ($15) is attributable to the customer's input. Tightening the rule to "do not refund customer-input failures" would have saved at most $15 across all time.

## Per-run table

Gs means GPU-seconds used. Wall means seconds from start to finish (Q4). Refund is the hold released (Q1).

| Job | Date | Tool / preset | failure_class | Bucket: detail | Gs | Wall | Refund | Group | Cause and evidence |
|---|---|---|---|---|---|---|---|---|---|
| d73ab00d | 05-29 | rfantibody pilot | NULL (failed) | pipeline: read timeout (30 s) calling tools.ranomics.com | – | 489 | 4.37 | OUR | The design finished: `_progress` says "Uploading results", with 1 of 1 designs complete (Q2). The callback to our own host timed out. |
| dd7eaf99 | 05-29 | boltzgen pilot | NULL (failed) | timed out after 6600 s, budget 5 | – | 6630 | 8.74 | OUR | Container ceiling hit on a 5-design pilot, AF model target (Q1, Q2). |
| 6b813bd2 e92644aa dd89f66d 72a72103 6c69a764 75c48360 | 06-03 | rfantibody pilot (×6, one customer) | NULL (failed) | pipeline: RFdiffusion "Non-positive determinant", zero rotation frame | – | 137–225 | 4.37 each | OUR (mechanism inferred) | See note 1. |
| a7c21ab4 | 06-04 | mpnn standalone | NULL (failed) | pipeline: KeyError `seq_chain_A` | – | never started | 0.15 | UNKNOWN | The input was a design output named `designs_design_001 (8).pdb` with chain A selected (Q2). Chain A was not found in the parse. Customer chain choice or our parser? Not determinable without the file. |
| cd7150c1 | 06-11 | bindcraft pilot | no_progress_timeout | {} (overrun warned) | 5015 | 413271 | 4.37 | OUR | 5015 GPU-s is the same ceiling 763247f5 was killed at (5012). A 1-hotspot, 2-design pilot on a clean single-chain target (Q2 `_preflight` ok). The run exceeded our cap. |
| 763247f5 | 07-02 | bindcraft pilot | safety_kill (billed) | projected cost exceeded per-tool hard cap | 5012 | 5013 | 0 (+3.63 absorbed) | OUR | 2-design pilot, preflight "ready" (Q2). Our estimate or cap does not fit BindCraft's real runtime. |
| 3b33f7cc d5b2fcad 6664ce8d 0a6d9341 | 07-11/12 | esmfold2-design minibinder (mozmail, flagged) | tool_error | pipeline: CUDA OOM, 80 GB | 50/76/44/44 | ≤92 | 0.16 each | OUR | The inputs are the untouched preset: CD45 target, batch 3, 3 designs (Q2). A default that does not fit the GPU is our fault. |
| a336f2a1 | 07-15 | colabfold standalone | tool_error | pipeline: colabfold_batch exited 1 | – | 217 | 0.22 | UNKNOWN | 5-chain, 505 aa multimer with no templates (Q2). Other no-template colabfold runs succeeded 10/11 (Q3). The Modal log is needed. |
| 8d75b1c4 | 07-18 | af2 standalone | no_progress_timeout | {} | – | 22021 | 0.79 | OUR | 111 aa monomer (Q2). af2 standalone with templates succeeded 28 times (Q3), so neither input nor templates explain a 6 h hang. |
| a4eaf118 | 07-20 | af2 standalone | no_progress_timeout | {} | – | 22872 | 1.50 | OUR | Same as 8d75b1c4: a 111 aa monomer (Q2). |
| d70c956a | 07-20 | af2 standalone | timeout, never started | {} | – | never started | 1.50 | OUR | The job never started, so the customer's input never ran. |
| 07a0ddc1 | 08-14 | af2 standalone | tool_error | pipeline: colabfold_batch exited 1 | – | 18 | 1.50 | UNKNOWN | 618 aa. The FASTA header is very long and contains brackets, quotes and `5'` (Q2). A header or filename problem is a hypothesis only, unverified. |
| 0049514e | 08-27 | af2 standalone | tool_error | parser: pdb_missing | – | 395 | 1.50 | UNKNOWN | An ordinary 155 aa sequence (Q2). The fold ran for 395 s, then our parser found no PDB. Ours vs. upstream is not determinable without logs. |
| c70da107 | 08-27 | af2 batch | no_progress_timeout | {} | 131 | 22515 | 4.50 | OUR | 131 GPU-s, then 6 h with no heartbeat (Q1, Q4). A batch of 1 record. The heartbeat or finalisation was lost. |
| b3147e2f bd16eb45 | 08-27 | esmfold2-design scfv | tool_error | pipeline: CUDA OOM | 46/43 | ≤48 | 14.79 each | OUR | 141 aa target at the default batch 3 (Q2). The OOM was fixed by #242 (docs/FIRST-RUN-FAILURES-2026-09.md). |
| 7eae70ba | 08-28 | bindcraft pilot | unclassified | "Failed to download input PDB: HTTP 400" | – | 11346 | 13.11 | OUR | Our storage / signed-URL fetch failed. The preflight was "ready" (Q2). |
| b5707a1d | 09-09 | boltzgen pilot | unclassified | timed out after 6600 s, budget 50 | – | 6619 | 9.10 | OUR | Hit the ceiling at 421 residues, inside the size envelope we accept (docs/FIRST-RUN-FAILURES-2026-09.md). |
| 52eeaf55 90ec7966 3a75ca7b | 09-13 | colabfold standalone | tool_error | parser: scores_missing | – | 166–288 | 0.22 each | OUR | See note 2. |
| 3dfcb5a5 | 09-15 | esmfold2-design scfv | tool_error | pipeline: CUDA OOM | 128 | 162 | 15.00 | **CUSTOMER INPUT** | See note 3. |
| 70dd97f9 797f998a | 09-15 | esmfold2-design scfv | tool_error | pipeline: linalg.svd failed to converge | 36/26 | ≤40 | 15.00 each | OUR | See note 4. |
| c43329f3 | 09-22 | bindcraft pilot | unclassified | timed out after 14400 s | – | 14403 | 12.00 | OUR | 3-design pilot, preflight "ready" (Q2). We estimated 74.5 min (docs/FIRST-RUN-FAILURES-2026-09.md). |
| f42de6aa | 09-27 | colabfold standalone | unclassified | parser: scores_missing | – | 115 | 0.22 | OUR | Same templates cause as 52eeaf55 (Q2 `use_templates: true`). This ran the day before the refusal landed on 09-28. |
| c6b91dcd | 09-30 | af2 standalone | unclassified | parser: pdb_missing | – | 359 | 1.50 | UNKNOWN | An ordinary 521 aa construct (Q2). Same symptom as 0049514e. New since the prior triage. |

**Note 1: the six rfantibody runs on 06-03.**
- All six came from one customer, on crystal targets 3KKU, a trimmed 3KKU and a cleaned 3IUT (Q2).
- Commit `4cfaeb07`, the next day (06-04), added PDB normalisation plus a preflight gate. Its message names exactly this customer's 3IUT/3KKU targets, with "74 / 65 altloc records collapsed" (`git show 4cfaeb07`).
- All 16 no-framework rfantibody runs since 06-04 succeeded (Q3).
- **Conclusion:** we fed an uncleaned multi-conformer crystal file to the model. The PDB was a legitimate one. The causal link (altlocs produce the zero frame) is inferred, not reproduced.

**Note 2: the three colabfold runs on 09-13.**
- Templates were on (Q2). Colabfold with `use_templates=true` has failed **4 of 4** times, with 0 successes (Q3).
- The image lacks the template search stack.
- The option is now refused at submit (`tools/colabfold/__init__.py:114`, `_refuse_templates`, landed `6f2755e1` 2026-09-28).

**Note 3: 3dfcb5a5, the one customer-input failure.**
- The customer chose a 550 aa target at batch 6 (Q2). Both values are inside our accepted limits (`BATCH_SIZE_MAX = 6`, `tools/esmfold2_design/__init__.py:46`) but outside the validated envelope (docs/FIRST-RUN-FAILURES-2026-09.md).
- This is the only run where the customer's choice, not our default, made it fail.

**Note 4: the two linalg.svd runs, 70dd97f9 and 797f998a.**
- Both are on the same 125 aa target, at batch 3 and at batch 1 (Q2).
- Our own QA hit the same SVD error on the **untouched defaults** (docs/qa/QA-2026-09-30-functional.md, P0-4, job a327d5fe). So this is a tool bug, not this customer's target.

## Could a free submit-time check have refused the customer-input failure?

**3dfcb5a5 (550 aa × batch 6 OOM): not today.**
- A length × batch ceiling would be a free CPU check at submit. But no VRAM boundary has been measured for esmfold2-design, so there is nothing evidence-based to refuse at (docs/FIRST-RUN-FAILURES-2026-09.md, row 3dfcb5a5).
- One small GPU measurement would make it possible. That needs Leo's approval for GPU spend.

For the UNKNOWN runs, free checks would *probably* help, but this is unverified:
- **a7c21ab4:** parse the uploaded PDB's chain IDs at submit and refuse a chain that is absent.
- **07a0ddc1:** sanitise or refuse extreme FASTA headers.

## Top 3 OUR FAULT causes

**By cost** (refunded / absorbed price + Modal upper bound):

1. **BindCraft runtime exceeds our ceiling:** 3 runs (cd7150c1, 763247f5, c43329f3). $20.00 price + $25.11 Modal = **$45.11**.
   - Where: pilot limits in `gpu/modal_client.py:312` (the table now reads pilot 7200 s, though c43329f3 ran 14403 s; when that changed is unverified), the estimate / hard cap in `shared/wallet_estimates.py`, and the tool at `tools/bindcraft/`.
2. **BoltzGen pilot timeout:** 2 runs (dd7eaf99, b5707a1d). $17.84 + $13.62 = **$31.46**.
   - Where: `gpu/modal_client.py:316` (pilot limit now 3600 s, while these ran about 6600 s) and the accepted size envelope in the boltzgen preflight rules (`shared/pdb_preflight_rules.py`).
3. **esmfold2-design OOM on our own defaults:** 6 runs. $30.21 + $0.31 = **$30.52**.
   - Where: preset batch defaults in `tools/esmfold2_design/__init__.py` (lines 45-86 cover the batch bounds).
   - Runner-up, a near tie: **esmfold2-design linalg.svd**, 2 runs, $30.06. It fails on defaults today (QA P0-4) and is unfixed. Same tool.

**By count:**

1. **esmfold2-design OOM on defaults:** 6 runs. Location as above. The scfv half was fixed by #242; 4 of these 6 are the flagged mozmail account.
2. **rfantibody on uncleaned crystal PDBs:** 6 runs, $26.22 + $1.11. Now handled by `shared/pipeline_normalize.py` altloc collapse plus `shared/pdb_preflight.py` (commit `4cfaeb07`).
3. **colabfold with templates on an image without template search:** 4 runs, $0.87 + $0.77. Now refused at `tools/colabfold/__init__.py:114`.

**Still live (not fixed) among the OUR FAULT causes:**
- BindCraft and BoltzGen runtime vs. ceiling.
- esmfold2-design svd.
- af2 hangs, never-started runs and pdb_missing: 3 hangs and 1 never-started run, plus 2 pdb_missing listed as UNKNOWN.
- Storage HTTP 400 (1 run): whether this was fixed is unverified.

## Caveats

- The Modal figures are upper bounds. The rate is A100-80GB; af2 and colabfold may run on cheaper GPUs, and wall time overstates GPU time on hung runs.
- 7eae70ba's 11346 s of wall time is counted at the full rate, but the container may have died early. That would cut up to $11.66 from the OUR FAULT Modal total.
- Only one refund policy matters for this question. Refunds on OUR FAULT runs are correct under Leo's rule, and there is only one CUSTOMER INPUT run to reclassify.
- The UNKNOWN runs ($4.87) need Modal logs to settle.
- The Supabase SQL editor autosaved the queries as an "Untitled query" snippet. No data changed; you may delete the snippet.
