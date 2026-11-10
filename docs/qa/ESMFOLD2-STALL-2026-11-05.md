# A finished esmfold2-design run was refunded and its designs hidden (2026-11-05)

Job `e3703b7a-4ccb-410f-aef1-76bfb6fa8ae5`, user `ca51a861-1681-4bb3-90ab-4fd5e3c3a3f8`
(`tfwang@as.edu.tw`, academic lab), tool `esmfold2-design`, preset `minibinder`.

**The run succeeded.** It produced all three requested designs and uploaded all
three to Storage inside eleven minutes. It was then classified a timeout,
refunded in full, and its designs were withheld from the customer, who was never
emailed. We paid for the GPU and delivered nothing visible.

Every figure below was read from production on 2026-11-06 in the Supabase SQL
editor, or derived from those readings by the arithmetic shown. Claims about
code cite the file. Anything neither read nor cited is labelled unverified.

## 1. What production says

| field | value |
|---|---|
| `status` / `failure_class` | `timeout` / `no_progress_timeout` |
| `result` | **null** |
| `inputs._partial_candidates` | **3 designs**, with scores |
| `inputs._progress` | `{stage: folding, designs_completed: 0, designs_total: 0}` |
| `inputs.parameters.n_designs_total` | **3** |
| `inputs._seed_beats` | null |
| `gpu_seconds_used` | 655 |
| `inputs._wallet` | `{live: true, hold_tx_id: 1393, estimate_usd: "9.8614"}` |
| `n_seeds` / `batch_size` | 1 / 3 |
| migration 0047 | **applied** (`tool_jobs_failure_class_check` present) |

Storage bucket `tool-outputs`, prefix `ca51a861-.../e3703b7a-.../designs/`:

| object | bytes | created |
|---|---|---|
| `design_0_complex.pdb` | 177,551 | 07:04:59.78 |
| `design_1_complex.pdb` | 170,990 | 07:05:02.24 |
| `design_2_complex.pdb` | 178,847 | 07:05:06.41 |

Ledger for hold 1393: the hold itself `$0.0000` (a live run holds nothing),
then thirteen `run_debit` children 1394-1406, then one `hold_release` 1407.

## 2. Timeline

All times UTC, 2026-11-05. `started_at` is derived: the last debit carries
cumulative `gpu_seconds` 3662 and is stamped 07:55:13, and 07:55:13 minus
3662 s is 06:54:11.

| time | elapsed | event |
|---|---|---|
| 06:54:11 | 0 | first heartbeat stamps `started_at`; row to `running` |
| 06:56:01 | 1m50s | first `run_debit`, 109 cumulative GPU-s, -$0.4479 |
| 07:04:59 | 10m48s | `design_0_complex.pdb` lands in Storage |
| 07:05:02 | 10m51s | `design_1_complex.pdb` lands |
| 07:05:06 | **10m55s** | `design_2_complex.pdb` lands - **all three requested designs delivered** |
| 07:55:13 | 1h01m02s | thirteenth and last `run_debit`; debits stop at the $15.00 cap |
| 08:39:11 | 1h45m00s | latest the container can still have been alive (`_ORCHESTRATOR_TIMEOUT_S = 6300`, `tools/esmfold2_design/modal_app.py:148`) |
| 13:00:29 | **6h06m18s** | sweeper picks the row up (`STUCK_RUNNING_AGE_HOURS` default 6, `cron/sweep_stuck_jobs.py:223`) |
| 13:00:30 | | `hold_release` 1407 returns $15.0000, `failure_reason: timeout` |

`gpu_seconds_used = 655` is exactly 06:54:11 to 07:05:06, the span from
`started_at` to the last design upload. That is the figure
`billed_gpu_seconds` is meant to produce for a seed whose batch has finished
streaming (`shared/jobs.py:2928`, with `_seed_last_design_at`), and it is
correct: with `n_seeds: 1` there was nothing left to meter after 07:05:06. It
did not freeze by accident.

## 3. A - which arm swept it, and was the design-keep reached?

**The `running` arm**, matching on `started_at` age alone
(`cron/sweep_stuck_jobs.py:284-285`). Not a campaign piece, so
`_within_campaign_session` did not spare it.

**The keep was not reached.** This is a deduction, not a direct reading - the
Modal verdict is not persisted anywhere - but the code admits only one route to
the observed row:

- Storage held all three structures (section 1), and `reconstruct` keeps every
  streamed design whose structure is present, comparing names after
  `_safe_filename` (`shared/job_recovery.py:236-247`).
  `design_0_complex.pdb` and its siblings pass `secure_filename` unchanged.
- So had `_keep_streamed_designs` run, it would have had three candidates to
  write, and `result` would not be null.
- `result` is null. The only branch that skipped the keep entirely was the
  `exit_verdict not in ("failed", "failed_unconfirmed")` gate.

So `_probe_modal` returned `failed` or `failed_unconfirmed`
(`shared/job_recovery.py:256-290`). Six hours after the fact,
`failed_unconfirmed` is the likelier of the two - that value also covers a poll
Modal could not answer and our own `ModalCallTimeout` - but which one it was is
**unverified and now unrecoverable.**

One alternative is not excluded by the data: the keep could have run and had its
`result` CAS fail. The Railway log for the `tools-hub-sweep-stuck` service at
13:00 would settle that only partly. Two of the keep's exit paths log and name
the row - `reconstruct` raising
(`logger.warning`, `shared/jobs.py:2306-2309`) and the CAS call itself raising
(`logger.error`, `shared/jobs.py:3301-3306`) - but a CAS that simply matches
zero rows returns `False` with **no log at all**
(`shared/jobs.py:3308-3309`, then `:2318`), and so does an empty rebuild
(`:2311`). So a log naming this job would prove the keep ran and broke; silence
proves nothing either way. Nobody has read that log regardless; treat it as
unverified. It does not change the fix.

`recover_stuck_job_result` refused for the same reason the keep was skipped, and
would have refused anyway: `_progress.designs_total` is 0, so
`_completion_signal` returns `"unknown"` (`shared/job_recovery.py:382-388` -
the `total <= 0` arm, **not** the `"incomplete"` arm), and the gate at
`:467` requires either a clean exit or a `"complete"` signal.

## 4. B - did the customer get anything?

**No. Nothing at all.**

- **Email: none.** Two independent gates. The timeout path never calls
  `complete_job`, and `_send_completion_email` fires only on
  `{"succeeded", "failed"}` (`shared/jobs.py:3013`).
- **Results page: empty.** The page's copy and its results block both hang off
  `kept_designs`, which reads `result`.
- **CSV and FASTA: empty.** The exports have no status gate at all; they read
  `result` too. One `result` write would have fixed page, CSV and FASTA
  together (`tests/test_timeout_keeps_designs.py::test_exports_carry_the_kept_designs`).

The designs themselves are intact and retrievable: three PDBs in Storage
(section 1) with their scores on the row.

| design | ipTM | `filter_status` | file |
|---|---|---|---|
| design_0 | 0.9033 | `strict_pass` | `design_0_complex.pdb` |
| design_1 | 0.8608 | `strict_pass` | `design_1_complex.pdb` |
| design_2 | 0.8291 | `drop` | `design_2_complex.pdb` |

Every other score field is null, which is expected: this pipeline streamed ipTM
and the filter verdict and nothing else.

These designs were exported for the customer with the same builders the
owner-only download routes call (`shared/exports.py::candidates_to_zip`,
`::candidates_to_csv` over `shared/jobs.py::page_ordered_records`), and the
numbering in that output is worth stating because it looks like an off-by-one
and is not one. The CSV's `rank` column is a 1-based **position**, assigned by
the exporter (`shared/exports.py:221`, `{"rank": i + 1}`), so it reads 1/2/3;
the tool's own 0-based rank is carried in the same row as `source_rank`
(`shared/exports.py:260`), so it reads 0/1/2, and so do the filenames. A row
therefore pairs `rank` 1 with `design_0_complex.pdb`.

There is no mismatch between what a customer reads and what they open, on
either surface:

- **The page** labels a design from its own rank, not from a position:
  `design_<rank>` at `templates/tools/esmfold2_design_results.html:180`. It
  reads `design_0` and the file it links is `design_0_complex.pdb`. They agree.
- **The CSV** names the file in `pdb_key` in the same row as the `rank` that
  row carries, so the row is self-describing; `rank` is a position, which is
  the documented intent of that column (`shared/exports.py:213-221`).

No change is proposed here. Recorded so a future reader does not "fix" the
1-based position into a second 0-based column.

## 5. C - was the refund real?

**Yes, and it was complete.** Verified from the ledger, not from the code path.

The thirteen debits sum to **exactly $15.0000**, and `hold_release` 1407
returned **$15.0000** with `gpu_seconds 655` and `failure_reason: timeout`.
Exactly one settle row. The customer paid nothing.

That $15.00 is not a coincidence: it is `esmfold2-design`'s
`base_hard_cap_usd` (`shared/wallet_estimates.py:632`, in the
`esmfold2-design` spec that starts at `:561`). Twelve of the thirteen
debits match `delta_seconds * 0.002417 * 1.70` to the cent
(`shared/wallet_estimates.py:75` and `:54`); the thirteenth should have been
$1.1505 and charged **$1.1037**, which is the clamp landing the running total
on the cap and not a penny over.

Two things follow. The quote was $9.8614 and we debited $15.00 before refunding,
so the cap - not the estimate - is what bounded the charge. And **the ledger
cannot date the GPU's death**: debiting stopped because the cap was reached, not
because the container went quiet. The run may have been alive until 08:39:11.

## 6. D - what did we pay, and what is the worse defect?

**We paid for between 3,662 and 6,300 H100-seconds and recovered none of it.**
The lower bound is the last metered second; the upper is the orchestrator
ceiling. At the $0.002417/GPU-s basis in `shared/wallet_estimates.py:75` that is
**$8.85 to $15.23**, and Modal's own line settles upward by roughly 1.7% (measured on
earlier runs, not a repo fact) for
about an hour after a run, so a reading taken now is a lower bound.

The worse defect is not the detection lag. It is that **a run which delivered
its entire requested deliverable could not be recognised as having finished.**

The gate takes either of two proofs: a `clean_exit` verdict from the Modal
probe, which needs no `_progress` at all
(`shared/job_recovery.py:467`, pinned by
`tests/test_sweep_stuck_recovery.py::test_clean_exit_zero_webhook_lost_recovers_from_storage`),
or - failing that - a `"complete"` signal computed from
`_progress.designs_completed` against `designs_total`
(`shared/job_recovery.py:368-389`). This run got neither: the probe returned a
failure verdict, so the counters were the only proof left - and this pipeline
left both at 0 for the whole run. The row still says `stage: folding` after all
three designs had shipped, so the gate's answer was `"unknown"` and recovery
refused. Meanwhile
the delivered count sat on the same row in `_partial_candidates` (3) and the
requested count in `inputs.parameters.n_designs_total` (3). The evidence was
present and unread.

The blind spot is narrower than "always", and still tool-agnostic: whenever
Modal gives no clean-exit verdict, a pipeline that does not fill those two
counters can never be recovered, however much it delivered.

## 7. Was anything watching? No.

Nothing alerts on a paying run going silent.

- No non-test Python file imports Sentry. The word appears once, in a comment
  (`shared/idempotency.py:416`); `ALERTING.md:18` has Sentry "Deferred by
  decision" (that comment cites `:17`, off by one).
- The only `notify_operator_*` sender is `notify_operator_new_submission`
  (`shared/email.py:2158`). Unverified: that no other function alerts an
  operator by some other name - I did not audit every sender.
- The nearest existing option is not wired. `ALERTING.md:323-330` says a
  Railway cron running the smoke script "works, but the failure signal has to
  be wired", because Railway "does not natively alert on a script's exit code
  as cleanly as a deploy failure", and it recommends Option A as simpler.
  Unverified: whether anything wires that signal today - I found no sender,
  but did not read the Railway configuration.
- The only visibility is the daily digest's aggregate per-tool `timeout` count,
  which is a number, not a page.

**Two signals that look obvious are dead.** `inputs._seed_beats` is null on this
row, because that map is stamped only when a heartbeat carries a `seed` field
(`webhooks/modal.py:623-631`), so a stale-seed-beat scan would not have fired.
And `gpu_seconds_used` froze at 655 **correctly** (section 2), so a "meter
stopped moving" alarm would have been a false positive here and silent on the
real problem.

The signal that survives needs no heartbeat at all: **`status = running` past
the tool's own Modal session cap.** That cap is computable from `started_at`,
`now` and `gpu/modal_client.py::preset_gpu_seconds(tool, preset)`
(`:391-393`), the central table of per-tool, per-preset session seconds; for
this pair it returns 5400 s, 1h30m, matching this tool's own `_MAX_SESSION_S`
(`tools/esmfold2_design/modal_app.py:145`). Calling the whole table a
maintained mirror would be too strong - its own comment at
`gpu/modal_client.py:282-293` records that these two rows have almost no
live reader - so an alarm built on it should re-check the row it uses
against that tool's `modal_app.py`. This row
was **4h36m** past that when the sweeper found it. The cap is 15 minutes short
of the orchestrator ceiling the run is actually killed at
(`_ORCHESTRATOR_TIMEOUT_S = _MAX_SESSION_S + 15 * 60`,
`tools/esmfold2_design/modal_app.py:148`), so an alarm on it fires slightly
early - harmless for an alarm, and it is the same quantity a per-tool sweep
cutoff would need, so one change would serve both.

This is not implemented here. It would be a new recurring outward-facing send,
the per-tool scope and threshold are Leo's policy call, and it lands inside the
cron that settles wallet holds.

## 8. What this change fixes

**Keeping the designs and billing for them are now separate decisions.**

`timeout_stuck_job` used to skip `_keep_streamed_designs` outright on the two
failure verdicts, which withheld the bill *and* the designs together. It now
always keeps, and passes `reclassify=False` on all three verdicts that withhold
the bill (`failed`, `failed_unconfirmed`, `still_running`). The `still_running`
arm already worked this way; the change extends its treatment to the other two.

**No money moves.** `reclassify=False` leaves the class `mark_timeout` wrote,
which is `no_progress_timeout`, which is in `_REFUNDED_FAILURE_CLASSES`
(`shared/jobs.py:825`). Every existing settle assertion is unchanged
(`tests/test_sweep_stuck_recovery.py::TestGenuinelyDeadJobStillTimesOut`: the
four pre-existing tests at `:313`, `:334`, `:409` and `:464` still assert
`release` called once and `settle_hold` never, and the test added here asserts
the same. The fifth, `::test_incomplete_progress_without_ground_truth_times_out`,
asserts the opposite pair - settle once, release never - and is also
unchanged).

What changes is that the designs reach the customer. Run against this
incident's shape, the row carries all three designs and stays fully refunded
(`::test_a_failed_verdict_still_shows_the_designs_it_delivered`, which asserts
the three candidates, `partial`, the class and the refund). That the same
`result` reaches the page, the CSV and the FASTA is pinned separately
(`tests/test_timeout_keeps_designs.py::test_the_page_says_designs_were_kept`,
`::test_exports_carry_the_kept_designs`).

A design whose structure is not in Storage is still dropped by `reconstruct`, so
nothing is invented.

This does **not** recover the 2026-11-05 row, whose `result` is already null and
whose status is already terminal. That row needs a one-off backfill, which is
not in this change.

## 9. Defects, ranked

| # | Grade | Defect | State |
|---|---|---|---|
| 1 | **P0** | A run that delivered its full requested count cannot be recognised as complete: with no clean-exit verdict the recovery gate has only the `_progress` counters this pipeline never filled, while the delivered count sits unread in `_partial_candidates` (`shared/job_recovery.py:368-389`, gate at `:467`). Cost one finished job and $8.85-$15.23 of H100. | **open** - the repair changes billing classification (a delivered-complete stuck run would bill instead of refund), so it needs Leo's decision, not just a patch |
| 2 | **P1** | Delivered designs were withheld from the customer because Modal reported a failure - a billing rule applied to the display path. | **fixed here** |
| 3 | **P1** | Nothing alerts on a silent paying run, and the two obvious signals are dead (section 7). The live one is `running` past the tool's own ceiling. | open |
| 4 | **P1** | The debits bill raw cumulative wall clock (3,662 s) while the row's own `gpu_seconds_used` bills per-seed spans (655 s) - a 5.6x disagreement on one run. The debits kept charging for 50 minutes after the work was done. | open |
| 5 | **P1** | `base_hard_cap_usd` silently stops debiting at the cap. Past it the GPU is free to the customer and a pure loss to us, and the ledger can no longer date a run's death. | open |
| 6 | **P2** | One flat `STUCK_RUNNING_AGE_HOURS = 6` for every tool. `esmfold2-design` cannot exceed 6,300 s by its own design, so at least 4h20m of this gap was guaranteed dead time; for a longer-ceiling tool the same flat value kills live runs. | open |
| 7 | **P2** | A swept timeout emails the customer nothing, though the refunded-timeout wording already exists in `shared/email.py`. | open |
| 8 | **P2** | On `still_running` the sweeper refunds and leaves Modal running. Nothing cancels the call (`docs/design/LIVE-CHARGING-2026-10-01.md:428`). That the sweep service holds no Modal tokens is **unverified**: its only source is the same design note (`:440`), not the Railway configuration. | open |

Migration 0047 was previously listed here as unapplied, on a sibling session's
note. It **is** applied (section 1). Closed.
