# rfantibody live charging — one paid run (2026-10-21)

Leo approved one paid rfantibody run in this session: "go, run the stop test". One run was made. **The cancel was never placed: the run finished on its own in 594 GPU-seconds, before a design-bearing cancel was possible.** So this run proves live metering and the charge arithmetic, and leaves the $0 stop and the cancel-keeps-designs path untested.

## Pre-checks

- `/health` build = `66109abeb1bf3848082ec3e6a7b51fcf96d91ec1`, read by `curl https://tools.ranomics.com/health` at 03:35 UTC. `git merge-base --is-ancestor a2f25612 66109abe` exits 0, so the deployed build contains #445 (`a2f25612`, the rfantibody live-charging commit).
- Container `llm-proteinDesigner` #38 (the 60 s heartbeat + per-design upload) merged 2026-10-20T19:57:11Z, and the "Deploy rfantibody to main" job of the `d3771ee9` Deploy Modal apps run succeeded (`gh run list --workflow "Deploy Modal apps"`).
- Account = `leowan7+t1@gmail.com`, read from `/account`.
- Balance before = **$14.52**, spent today $0.00, auto reload off. Read from `/account/wallet`.
- `/jobs` listed six jobs for this account, all terminal (5 CANCELLED, 1 SUCCEEDED). No pending or running rfantibody job for this account. Global `tool_jobs` state was not read — that needs DB access this session does not have. No Modal redeploy was done.
- `window.confirm` was stubbed to `() => true` before submit, as a guard against the native-dialog trap (memory `cancel-button-blocked-by-window-confirm`). It was never reached.

## Inputs

Read from the form's own elements before submit: preset `pilot` (the page's "trial run" tier), `target_pdb` = 4ZQK downloaded whole from RCSB, `target_chain` A, `hotspot_residues` 54,56,115, `cdr_lengths` H1:8,H2:7,H3:10-16, `num_designs` **2** (the cheapest backbone count; the default is 4), group `rfantibody-live-cancel-test`.

The free "Check my settings" ran first and returned "Ready to run": 115-residue target after cleanup, all three hotspots preserved, 10 residues and chain B dropped. Nothing was charged for it.

## Run

- Submit clicked at **03:36:10–03:36:18 UTC** (the stub ran at 03:36:10.335, the job page was loaded by 03:36:18.489).
- Job id: **`1efd6669-751f-4c57-a58e-57fc594d3482`**.
- The $2.00 start minimum passed at a balance of $14.52. **The gate was not exercised near its boundary** — that is covered only by the pins in `tests/test_live_charging.py` (`test_estimate_deficit_matches_the_guard`'s two rfantibody rows, at exactly $2.00 and a hundredth of a cent below), not by this run.

## Watch

From an in-page watcher polling `/jobs/<id>/status.json` every 15 s:

| UTC | status | gpu_seconds_used | partial_candidates |
|---|---|---|---|
| 03:36:37 | running | 3 | 0 |
| 03:37:38 | running | 63 | 0 |
| 03:38:40 | running | 124 | 0 |
| 03:39:40 | running | 184 | 0 |
| 03:40:38 | running | 245 | 0 |
| 03:41:40 | running | 307 | 0 |
| 03:47:07 | succeeded | 594 | 10 |

**Heartbeats arrive every ~60 s**, which is what container #38 changed (the old default was 300 s): the counter steps 3 → 63 → 124 → 184 → 245 → 307, i.e. about 61 s per step.

The job page reloaded itself between 03:41:40 and 03:47:07 and took the watcher with it, so the window in which the first design landed (307 s → 594 s) was not sampled. **Whether designs appeared in `partial_candidates` while the run was still running is therefore unobserved**, and this run is not evidence for it.

## Charge

`/account/wallet` ledger for this job, as the page rounds it:

| When (UTC) | Kind | Amount | Balance after |
|---|---|---|---|
| 2026-10-21 03:36 | HOLD | $0.00 | $14.52 |
| 2026-10-21 03:40 | RUN DEBIT | −$0.25 | $14.26 |
| 2026-10-21 03:45 | RUN DEBIT | −$0.43 | $13.83 |
| 2026-10-21 03:46 | CHARGE | −$0.04 | $13.79 |

- Balance after = **$13.79**, spent today **$0.73**. Both read from `/account/wallet`.
- **The hold was $0.00 and needed no release.** That is what `_LIVE_ALL_PRESETS` membership does; it is the same shape the rfdiffusion and esmfold2-design runs showed.
- **Two debits landed mid-run**, five minutes apart, before the run finished. Pay-as-you-go is live for rfantibody.
- **The total matches the rate.** A100-40GB $0.000714/s × markup 1.70 = $0.0012138 per GPU-second (`shared/wallet.py:183`, `shared/wallet.py:74`; GPU class from the tool page's "A100-40GB" line and the preflight size-envelope line). 594 s × $0.0012138 = **$0.7210**. The printed amounts sum to $0.72 and the balance moved $0.73 — amounts are rounded half-up and balances floored, which is the designed behaviour (memory `wallet-money-display-rounds-by-design`), not a defect.
- Unrounded ledger rows were not read. That needs Leo's read-only SQL with this job id.

## Designs

- 10 candidates, matching the documented 5-per-backbone fan-out for `num_designs` 2. ipAE 13.35–17.37 Å, pLDDT 89–91, pAE 7.53–9.73 Å. None clears the ipAE 10 / pAE 5 bar; the page says so plainly and names the failing measurement per row. That is a scientific outcome, not a billing one.
- `partial_candidates[].pdb_key` reads `samples_design_0_dldesign_0_best.pdb` — the quiver tag name. The pre-#38 code uploaded `design_{rank:03d}.pdb`. **The tag-based naming is evidence the streaming image from #38 is the one that ran.**
- CSV, FASTA and Structures (ZIP) buttons are all present on the finished page. Their contents were not downloaded.

## Not covered

- **The cancel.** The run completed in 9m 54s, faster than the cancel could be usefully placed after a design arrived. No cancel was clicked, so: the `window.confirm` fallback, the in-page `#cancel-confirm` warning, whether streamed designs survive a cancel, and the hold-return-on-cancel path are all still untested for rfantibody.
- **The $0 stop.** Never approached; the balance never fell near zero.
- **The start-minimum boundary**, and the known limit above ~460 aa (see the note in the shared session notes and #445's body). This target was 115 aa.
- **Mid-run visibility of streamed designs** in `partial_candidates`, for the sampling gap above.

A cancel test needs a second paid run, and that needs a fresh yes from Leo. A run with `num_designs` 6–8 on this same 115 aa target would give a long enough RF2 stage to cancel into, at roughly the same per-second rate.
