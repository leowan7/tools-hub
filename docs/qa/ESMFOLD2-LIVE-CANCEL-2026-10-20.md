# esmfold2_design live cancel test (2026-10-20)

Leo approved one paid run in chat on 10-20, with a worst case of $15.00. He typed "yes, submit it" in this test session. One run was made.

## Pre-checks
- An earlier attempt was blocked. The wallet read $6.99, below the $15.00 start minimum. Leo then topped the account up: `/account/wallet` shows "2026-10-20 15:31 ADJUSTMENT +$9.00 $16.00".
- `/health` build = `a769acb0f0ebf82751d0eaadd0112a5d91b1651e`, which is the #442 merge commit. It sits on top of #443 (`a0250c08`) and #440 (`8764b44a`). This was read by a 60 s polling loop over `gh pr view 442` and `curl https://tools.ranomics.com/health`, at 15:52:18 UTC.
- Account = `leowan7+t1@gmail.com`, read from `/account` in Leo's Chrome.
- Balance before = **$16.00**, spent today $0.00, auto reload off. Read from `/account/wallet`.
- In `templates/job_detail.html` at `a769acb0`, the in-page `#cancel-confirm` box shows only while `data-delivered` is 0. After that, the handler falls back to the native `window.confirm` (line 553). So the test stubbed `window.confirm = () => true` before clicking, as a guard. The in-page box was what actually showed.

## Run
- Inputs were read from the form's elements just before submit: preset `minibinder`, target_mode `preset`, target_name `cd45`, seed 0, n_seeds 1, batch_size 1 (the form default is 3), n_designs_total 1.
- Submit was clicked from a page script at **15:55:45.280 UTC**.
- Job id: **`de0bf6ac-3c06-42d0-b562-ac9ef5bb643b`**.
- `started_at` = **15:56:01.929 UTC**, from `/jobs/<id>/status.json`. It did not change on any read, including the read after the cancel.

## Watch
These come from an in-page watcher that polled `status.json` and `/account/wallet` about every 13 s and logged to localStorage.

| UTC | status | gpu_seconds_used | partial_candidates | wallet esmfold2 rows |
|---|---|---|---|---|
| 15:59:04 | running | 123 | 0 | HOLD $0.00 |
| 15:59:17 | running | 184 | 0 | HOLD $0.00 |
| 16:00:37 | running | 246 | 0 | HOLD $0.00 |
| 16:00:51 | running | 246 | 0 | + RUN DEBIT −$1.13 → $14.87 |
| 16:01:17–16:01:44 | running | 307 | 0 | same |

So GPU seconds now update mid-run. This differs from 10-13, when they stayed `null` until the end; see memory `esmfold2-design-metering-invisible-mid-run`.

The watcher was meant to cancel on its own, but it never fired. It looked for the text "RUN DEBIT" in uppercase, but the page source says "run debit" (the capitals come from CSS). So I cancelled by hand about 70 s after the debit appeared. This was a bug in the test, not in the product.

## Cancel
- I ran the page's real `#cancel-job-btn` click from a script at **16:02:00.179 UTC**, with `window.confirm` stubbed.
- The **in-page warning appeared** in `#cancel-confirm` (not a native dialog, and the tab stayed responsive). Its text: "No designs have been saved yet. Cancelling now is charged for the GPU time used and keeps no designs, because designs arrive only when a seed's batch finishes."
- I clicked "Cancel anyway" (`#cancel-confirm-yes`) at **16:02:00.179 UTC**. The status note read "Cancelling...".

## After it settled
- `status.json` at 16:02:45 UTC: `status` cancelled, `gpu_seconds_used` **360**, `partial_candidates` [], progress 0/0, stage "designing".
- Job page: "CANCELLED", "Cancelled after consuming 360 GPU-seconds. Your wallet was charged for time used. Any surplus from the original hold was returned."
- No designs streamed, because batch 1 delivers its one design only when the seed finishes. So there were no designs or CSV to keep, and the "designs kept" part of the test was **not exercised**.
- `/account/wallet` ledger for this job, as the page rounds it:

  | When | Kind | Amount | Balance after |
  |---|---|---|---|
  | 2026-10-20 15:55 | HOLD | $0.00 | $16.00 |
  | 2026-10-20 16:00 | RUN DEBIT | −$1.13 | $14.87 |
  | 2026-10-20 16:02 | CHARGE | −$0.35 | $14.52 |

- Balance after = **$14.52**, spent today $1.48. Both read from `/account/wallet`.

## Checks
- **Balance before minus after equals the charges.** $16.00 − $14.52 = $1.48, and $1.13 + $0.35 = $1.48. Yes.
- **The total matches the rate.** H100 $0.002417/s × markup 1.70 = $0.0041089 per GPU-second (`shared/wallet.py` `GPU_USD_PER_SECOND["H100"]`, `WALLET_MARKUP`; GPU class from `shared/wallet_estimates.py` esmfold2-design spec). 360 s × $0.0041089 = $1.4792, which rounds to **$1.48**. The −$1.13 debit equals about 275 s at that rate. Yes.
- **#442 bills up to the cancel moment.** The click landed 358.25 s after `started_at`. The bill is 360 s, about 2 s more, which is the POST's travel time plus rounding. The last heartbeat figure before the cancel was 307 s, so the bill is not frozen at the last heartbeat. Yes.
- **Exactly one charge.** One HOLD of $0.00, one RUN DEBIT and one CHARGE. Nothing is hold-sized, and no hold needs releasing, because the hold was $0.00. Yes. I have not read the unrounded ledger rows; that needs Leo's read-only SQL in the shared notes `proteina-live-charging-2026-10-16.md`, run with this job id.
- **Spend stayed under about $3.** $1.48.

## Not covered
- Designs and CSV kept on a cancel. This needs a cancel after a seed has delivered, with batch ≥ 2 or n_seeds ≥ 2. It would also exercise the `window.confirm` fallback path. It needs a new yes.
- Multi-seed metering under #443, since this run had one seed.
- The $0 balance stop.

**PASS** (cancel path and billing) — job `de0bf6ac-3c06-42d0-b562-ac9ef5bb643b`. 360 GPU-s. Charged $1.48 = RUN DEBIT $1.13 + CHARGE $0.35. Balance $16.00 → $14.52. Hold $0.00. In-page warning shown. No designs existed to keep, so that part was not tested.
