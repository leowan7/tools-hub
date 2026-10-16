# IgGM live charging: STEP 1 report (2026-10-16)

Free checks only: no GPU, no prod reads or writes. Code read at `origin/main` 6e322cc3. Every `file:line` below is at that commit, except the lines in `tools/iggm/run_pipeline.py` that PR #432 changes, which are called out as such.

The STEP 3 PR, which would add IgGM to the live list, does not exist yet. Every line below saying IgGM is not live describes main today.

## (a) Does IgGM stream designs mid-run? Yes

- `run_iggm` runs `design.py` under `Popen` and calls `on_poll` every 5 s while the child is alive (`tools/iggm/run_pipeline.py::run_iggm`, `poll_seconds: float = 5.0`).
- `main` passes `on_poll=lambda: _sweep(True)`, which calls `sweep_designs(out_dir, stream, upload_one, require_stable=True, ...)`.
- `sweep_designs` takes a design only when its size is unchanged since the previous sweep (the stability gate) and `_looks_complete` counts at least `max(expected_chains, 2)` distinct chain ids in it. It then uploads the design and sends a `new_candidate` heartbeat, one per design.
- So the delivery shape live charging needs is present: designs reach Storage and `inputs._partial_candidates` before the run ends. **No GPU run has ever exercised it** (#423, deployed 50ceb89d 10-08), which is what STEP 2 is for.

**One job class could never stream, and PR #432 fixes it.** The antibody records are `H` plus an optional `L`, and the antigen kept its PDB chain id in the FASTA IgGM reads, so an antigen chain named `H` or `L` shared an id with an antibody chain in IgGM's output. `_parse_pdb_chains` merged the two, `_looks_complete` could never reach `expected_chains = len(antibody) + 1`, and the job streamed nothing. STEP 2 avoids the question by using an antigen whose chain is `A`, so the test does not depend on #432 being deployed.

## (b) Can the live meter run on IgGM? Yes, with a caveat

- `meter_live_runs` (`shared/jobs.py`) meters wall-clock from `started_at`, and only once `inputs._progress` exists. The first heartbeat stamps `started_at` (`webhooks/modal.py::_handle_heartbeat`).
- IgGM sends a `designing` heartbeat before `run_iggm` starts, so `started_at` is stamped at the top of the run and the meter engages from there.
- When a debit comes back short the meter cancels the Modal call, reconstructs, and completes the job as a succeeded partial with `WALLET_STOP_REASON`. Nothing in that path is tool-specific.

**F1, money (not fixed, not in PR #432).** IgGM has **no periodic heartbeat**. It beats once at the start and then once per streamed design. `cancel_job` settles at the row's `gpu_seconds_used`, which is written only on a heartbeat, so a cancel landing between two designs settles at the *last design's* figure and releases the meter debits taken after it: the customer is undercharged for the gap. BindCraft avoids this with a 60 s tick. The fix is a heartbeat inside `run_iggm`'s existing `on_poll` loop; it is a separate change, not a blocker for going live, and its direction is in the customer's favour.

## (c) Start minimum: Leo's choice, three candidates

The rate is the A100-40GB rate $0.000714/s (`shared/wallet_estimates.py::GPU_USD_PER_SECOND`) × markup 1.70, i.e. **$0.0012138 per GPU-second billed**. IgGM runs on A100-40GB, not the A100-80GB of rfdiffusion and BindCraft, so its figures are much smaller.

| Candidate | Figure | What it is |
| --- | --- | --- |
| Derived the way #427 derived rfdiffusion's | **$0.0728** | 60 s, the spec's one-pass bootstrap (`expected_gpu_seconds=60.0`; canary measured 51–59 s for a 1-pass job) |
| The base hard cap, the way #431 chose BindCraft's | **$3.00** | `base_hard_cap_usd` — the most a 1-pass job can be charged |
| The full-session worst case | **$4.3333** | 3570 s, the subprocess limit inside the 3600 s Modal session cap (`tools/iggm/modal_app.py::_MAX_SESSION_S`) |

**A difference from BindCraft worth knowing before you pick.** For BindCraft the base cap is $8.00, above the $5 signup credit, so choosing the cap refused the credit. IgGM's base cap is $3.00, *below* $5. No start minimum at or below $5 can refuse the signup credit on IgGM, and refusing it would mean a start minimum above the tool's own 1-pass charge ceiling. I am not deciding this.

## (d) Cost of the STEP 2 cancel test

Preset `complex_prediction`, `num_samples=2` — the smallest run that can stream a design *mid-run* (at `num_samples=1` the only design lands as the run ends, so the cancel would not be mid-run and the test would prove nothing).

| | GPU-seconds | Cost |
| --- | --- | --- |
| **Expected** (cancel on the first design) | ~54 s | **$0.07** |
| If it runs to completion instead | ~78 s | $0.09 |
| **At the cut-off** (10 min, no design) | 600 s | **$0.73** |
| **Worst case**, only if the cancel fails and the run hangs the whole session | 3570 s | **$4.33** |

The $4.33 is not clamped away: `compute_hard_cap` scales the $3.00 base cap by `num_samples`, so at 2 samples the cap is $6.00.

**No balance SQL is needed.** IgGM is not on the live list (`shared/wallet_guard.py::_LIVE_ALL_PRESETS`), so the run takes the pre-run cushioned hold: 1.5 × the point estimate, clamped to the cap. At the 60 s/pass bootstrap that is 1.5 × $0.1457 = **$0.22**. The +t1 account's balance after the BindCraft retry was $7.59 (`docs/qa/BINDCRAFT-LIVE-STEP1-2026-10-14.md`, PR #431 branch), which covers the hold and the worst case. If run history has pulled the point estimate up, the hold can only rise to the $6.00 cap, which $7.59 still covers.

## STEP 2 plan (needs Leo's yes typed in this session)

- **Account:** the +t1 test account only, checked by fetching `/account` right before submitting and confirming `/account/wallet` reads "Auto reload is off". Never the owner's main account.
- **Inputs:** antigen PDB with chain **A** (so the test does not depend on PR #432 being deployed), a real VH FASTA as the antibody, 3 epitope residues (an epitope is required for every preset), preset `complex_prediction`, `num_samples=2`. The exact file and residues go in the STEP 2 record, read from the form's own `FormData` just before submit.
- **Stop:** wait for the first entry in `partial_candidates`, then click the job page's real Cancel button. `window.confirm` wedges the browser tab, so stub it in the page with `window.confirm = () => true` first, then click. The record will say this method was used.
- **Cut-off:** cancel at 10 minutes even with no design ($0.73). Such a run proves nothing, and the retry would need a fresh yes.
- **Reads:** the job page only after its 5 s poll has fired, so the read is not of a pre-hydration placeholder.
- **Record:** job id, GPU-seconds at the cancel, whether the streamed design survives in the table, `status.json` and every download, and the wallet ledger rows as the page rounds them. Leo runs any prod SQL himself; I run none.
- **What this test cannot cover:** the balance-runs-out stop. The meter stops only live runs, and IgGM becomes live only once a STEP 3 change is merged and deployed. The cancel path is the only stop available now — the same limit STEP 2 had for BindCraft.

## STEP 2: paid cancel test, run 2026-10-16

Leo approved it directly in this session: "yes, run the paid cancel test — use the $3.00 start minimum".

### What ran

- **Account:** the +t1 test account. `/account` was read right before submitting and showed that one email and nothing else; balance $7.59, spent today $0.00. `/account/wallet` read "Auto reload is off."
- **Job:** `6d894373-c3af-4cfb-b8ac-532901640825`. Submit clicked 13:40:00 UTC; `started_at` 13:40:11.774 UTC (`/jobs/<id>/status.json`).
- **Inputs**, read from the form's own `FormData` just before submit: `preset` complex_prediction, `fasta` one `>H` record, a 113-aa VHH framework sequence (a generic framework, not a validated binder — this test is about billing, not binding), `target_pdb` `4ZQK_A.pdb` at 70,802 bytes (chain A of the same 4ZQK copy the BindCraft and rfdiffusion tests used, cropped to its `ATOM` records so IgGM sees one antigen chain), `target_chain` A, `epitope` `54 56 115`, `num_samples` 2, `max_antigen_size` 2000, `fasta_origin` and `campaign_label` empty. The form's own check read "Ready to run — Nothing to clean."
- **Submit:** `#tool-submit-btn` clicked from a page script, the method the BindCraft retry had to fall back to (a mouse click does nothing while the tab is hidden).
- **Watch:** an in-page script polled `status.json` every 8 s and was set to cancel on the first entry in `partial_candidates`, or at 13:50:00 UTC if none arrived.

### What happened

- **Polls before the stop:** 13:40:26 and 13:40:34 UTC both read `status` running, `gpu_seconds_used` 2, `partial_candidates` empty, progress 0 of 2, stage "designing".
- **The stop:** the watcher saw `partial_candidates` non-empty while `status` was still `running`, set `window.confirm = () => true` and clicked the page's real Cancel button (`#cancel-job-btn`). The click reloaded the page, which discarded the watcher's own log, so **the exact second of the cancel is not recorded**. It was after the 13:40:34 poll and before the next read at 13:41:34 UTC, which showed `status` cancelled, `gpu_seconds_used` 37, `partial_candidates` 2, progress 2 of 2, stage "complete".
- **Designs kept.** The job page reads "Cancelled after consuming 37 GPU-seconds. Your wallet was charged for time used. Any surplus from the original hold was returned." and "Cancelled, 2 designs kept. They are in your results and downloads." The candidates table shows both rows, "Returned 2 / 2".
- **Downloads.** Every link on the page was fetched with the session cookie; all returned 200:

  | Download | Bytes | Content |
  | --- | --- | --- |
  | `export.csv` | 582 | header `rank,pdb_key,source_rank,sequence_chainA,sequence_chainH`, then both designs |
  | `export.fasta` | 592 | first record `>rank1_000_design_1.pdb_chainH`, the submitted `>H` sequence |
  | `export.zip` | 43,344 | not opened |
  | `designs/000_design_1.pdb` | 99,004 | `REMARK 250 Structure predicted by IgGM` |
  | `designs/001_design_0.pdb` | 99,004 | same header |

- **Ledger** (from `/account/wallet`, read after the cancel):

  | When (UTC) | Kind | Amount | Balance after |
  | --- | --- | --- | --- |
  | 2026-10-16 13:40 | hold, iggm | −$0.22 | $7.37 |
  | 2026-10-16 13:40 | hold release, iggm | +$0.17 | $7.54 |

  Net charge **$0.05**, and "Spent today" reads $0.05. The hold is the $0.22 this document predicted in (d). 37 s × $0.0012138 = $0.0449, so the release is $0.22 − $0.0449 = $0.1751, shown as $0.17. These are the page's rounded figures; the unrounded rows need Leo's SQL and I read none.
- **Not checked:** whether the Modal container actually stopped. `gpu_seconds_used` stayed at 37, but that is the hub's record, not Modal's.

### Verdict: passed

- **IgGM streams designs mid-run and a cancelled IgGM run keeps them.** `partial_candidates` held designs while the job was still `running`, and after the cancel both survive in the table, in `status.json` and in all five downloads. This is the first run ever to exercise #423's streaming.
- **The cancel charged the metered time and returned the rest of the hold:** $0.05 of a $0.22 hold.
- **F1 was not measured by this run.** The settlement used 37 GPU-seconds, the figure the last per-design heartbeat wrote; 37 s after `started_at` is 13:40:48.8 UTC. Because the cancel's own timestamp was lost with the watcher's log, whether elapsed time had passed 37 s at the cancel is **unproven here**. F1 remains open on the code reading in (b).
- **F2, new, cosmetic.** On this cancelled run the job page's IgGM panel reads "ANTIGEN chain ?" and "EPITOPE none". A cancelled run's result is rebuilt from the streamed candidates alone — `shared/job_recovery.py::reconstruct` returns a `candidates` list and no tool-level keys — and the template falls back when `output.antigen_chain` is missing (`templates/tools/iggm_results.html:19`, `:61`). The designs, the CSV chain columns and the PDBs are unaffected, so this is a display gap, not a money or data defect. Follow-up, not in the STEP 3 PR.
- **What this run did not test:** the balance-runs-out stop, which exists only for live runs, exactly as the plan above said.

## STEP 3: the live-list change

Leo picked the **$3.00** start minimum in the same message: IgGM's `base_hard_cap_usd` (`shared/wallet_estimates.py`, the `"iggm"` `ToolSpec`). It is below the $5 signup credit, so the credit still admits an IgGM run — the difference from BindCraft noted in (c).
