# PXDesign $0 balance stop test — NOT RUN (2026-10-30)

**NOT RUN. The stop is judged unreachable at the $2.00 start minimum.** No GPU
time was bought and no job was submitted. Leo decided on 2026-10-30 to skip the
test rather than spend on a run that was not expected to reach the stop.

This was test 2 of the pair in this session's brief; test 1 (rfantibody) ran and
passed, see `docs/qa/ZERO-STOP-RFANTIBODY-2026-10-29.md`.

## Why it is unreachable

The stop fires only when the balance runs out **before** the run finishes. On
pxdesign the two constraints close the window from both sides.

- **The floor is $2.00.** `PXDESIGN_START_USD = Decimal("2.00")` in
  `shared/wallet_guard.py`, and pxdesign is in `_LIVE_ALL_PRESETS`, so
  `live_start_usd('pxdesign', 'pilot')` returns `2.00` (measured in the repo
  venv). The start gate refuses a balance below that, so no test can begin with
  less than $2.00 in the wallet.
- **$2.00–$2.30 buys far more GPU time than a pilot run needs.** At the
  A100-80GB live rate, `compute_charge_usd(1316, 'A100-80GB')` = $2.2998
  (measured in the repo venv), so $2.30 buys about **1316 GPU-s, about 22
  minutes**. The two recorded pxdesign pilot runs took **504 s** and **462 s**
  — about 8 minutes, $0.88 and $0.81 at the same rate. A run that ends at 8
  minutes never reaches a balance that lasts 22.
- **Raising the design count is not known to help.** Of those two runs, the
  504 s one was `num_designs=2` and the **462 s** one was `num_designs=5` — the
  larger count was the *faster* run, and both returned 2 candidates "via pilot
  fallback". So on the evidence available the pilot-tier runtime does not scale
  with `num_designs`, and pushing the count to the single-container ceiling
  (`single_container_ceiling('pxdesign', 'pilot')` = **24**, measured) is not
  expected to push the runtime past 22 minutes.

So the only way to reach the stop would be to start at roughly $0.80, which the
$2.00 gate refuses. This is the same dead end rfdiffusion hit on 2026-10-15,
where the start gate pre-funds a whole run (memory
`rfdiffusion-live-charging-427`).

## What is unverified in that reasoning

- **The two runtime figures are from 2026-05-26 and 2026-05-27**, five months
  old, read from `docs/VALIDATION-LOG.md` lines 116–117. They pre-date the
  pxdesign streaming work (llm-proteinDesigner #39 and #40, merged 10-21 and
  10-22) and everything else that has landed since. **The current runtime of a
  pxdesign pilot run is unverified**, and a 24-design run could in principle
  take longer than 22 minutes now. The judgement above is a projection from
  stale measurements, not a measurement.
- **That the runtime does not scale with `num_designs`** rests on those same
  two runs (n=2, one of them at the old form max of 5). It is a weak inference,
  not an established property of the tool.
- Whether a pxdesign run **streams designs at all** before it finishes is also
  unverified: no paid pxdesign run has happened since live charging landed, and
  no design has ever been seen to stream from it.

## What this leaves open

- **pxdesign live charging is unexercised.** That the meter ticks, that run
  debits appear in the ledger, and that designs stream mid-run have none of
  them been seen on a real pxdesign run. A ~$0.90 run at the ceiling would
  settle all three even if the stop never fired. Not done; Leo's call.
- **The $0 stop on pxdesign stays untested.** Reaching it needs either a start
  minimum below the cost of a whole run, or a pxdesign configuration that runs
  longer than the start minimum buys. Neither exists today.
- The test account is at $0.00 after test 1, so any later pxdesign test needs a
  top-up or an adjustment from Leo first.
