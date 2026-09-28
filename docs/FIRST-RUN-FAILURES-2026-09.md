# First-run failures, 2026-08-26 → 2026-09-27

Triage of every non-succeeded run in the last-50-signups window: 57 runs, 42
succeeded, 13 failed, 1 timed out, 1 cancelled. Six users' FIRST run failed
and three of those never had a successful run (boltzgen, bindcraft ×2).

Source: one read-only SELECT over `tool_jobs` (id, tool, preset, status,
`failure_class`, `gpu_seconds_used`, timestamps, `error`, trimmed `inputs`)
joined to the `wallet_transactions` rows for each job's hold, run by Leo on
2026-09-27. The query is not in this repo; every figure below comes from its
output. Rows are quoted, not re-derived.

## Billing: nothing to fix

Every failed and timed-out run had its hold **released in full**. The one
cancelled run (rfdiffusion) was charged for its 76 consumed GPU-seconds only
(hold −2.1849, release +2.0521, `failure_reason: "cancelled"`), which is what
`_settle_wallet_hold_for_completed_job` prescribes for `user_cancelled`.

What was missing was not the refund but **saying so**: see "User-facing copy"
below.

## Fixed in this change

| Jobs | Tool | Cause | Fix |
|---|---|---|---|
| 3a75ca7b, 90ec7966, 52eeaf55 (2026-09-13), f42de6aa (2026-09-27) | colabfold standalone | All four carried `use_templates: true`. `colabfold_batch --templates` spawns `hhsearch`; `tools/colabfold/Dockerfile.modal` installs no hhsuite and bakes no PDB70. All four billed the GPU and surfaced as the parser's `scores_missing` with an empty output dir — the exit-0 arm, not the non-zero-exit one; that the swallowed exception is the missing binary is inferred from AF2's fix (`0bb3bbf8`), not proven here. | Refused for free in `validate` before billing; the form control is gone; `_effective_use_templates` downgrades on the GPU for payloads that bypass `validate`. |
| all of the above, plus every refunded run in the window | all | The job page and the completion email never said the run was refunded, and the page rendered nothing at all for a `timeout` row. | `shared/jobs.py::failure_notice`, consumed by both surfaces. |

The user behind the three 2026-09-13 colabfold failures switched to AF2 and
succeeded. AF2's image *does* ship hhsearch and carries the same runtime
downgrade (`tools/af2/run_pipeline.py::_hhsearch_available`, landed
`0bb3bbf8`), which is why the same option did not kill their AF2 run.

**Falsification still open.** The mechanism above predicts that the three
*successful* colabfold runs in this window had `use_templates` off. That was
not checked — the query returned failed rows only. A success with it ON would
weaken the claim; it would not rescue the option, since the image genuinely
ships neither dependency (pinned by
`tests/test_colabfold_templates_refused.py::test_the_image_still_ships_no_hhsuite`).

## Already fixed before this change — no action

| Jobs | Tool | Evidence |
|---|---|---|
| b3147e2f, bd16eb45 (2026-08-27, 141 aa, batch_size 3) | esmfold2-design scfv | CUDA OOM. Both PRE-DATE `f0cd26c2` (#242, 2026-09-09), whose own comment describes this exact failure: "every batch_size > 1 scfv run OOMed an 80 GB H100 for zero designs (default batch_size is 3, so that was the default web path)". Prod job `verify242-bs6-1789054528` then passed 6/6 at scfv/cd45/batch_size 6 (docs/VALIDATION-LOG.md). |

## Report only — infra / OOM, not guessed at

No fix is proposed for any row below. Each needs either Modal logs or a
GPU run, and GPU spend needs Leo's per-job approval.

| Job | Tool | What the row says | What it would take to close |
|---|---|---|---|
| 3dfcb5a5 (2026-09-15) | esmfold2-design scfv | CUDA OOM at a 550 aa target × `batch_size` 6 — AFTER #242. Inside the constants (`TARGET_SEQ_MAX` 800, `BATCH_SIZE_MAX` 6) but outside the only validated envelope, which is cd45 at batch 6. | A size × batch guard needs an anchor: the cd45 reference length, and at least one measured OOM boundary. VRAM was never instrumented for #242, so today there is no evidence-anchored threshold to refuse at — a guessed one would refuse paying work that fits. |
| 797f998a, 70dd97f9 (2026-09-15) | esmfold2-design scfv | `linalg.svd` failed to converge, on the SAME 125 aa target, at batch_size 3 AND batch_size 1, with different seeds. | Input-specific upstream numerics, not memory: batch 1 rules the size explanation out. Reproducing needs a GPU run on that target. |
| c43329f3 (2026-09-22) | bindcraft pilot | Subprocess hit the 14400 s ceiling (`gpu/modal_client.py`), against a preflight estimate of 74.5 minutes for the same input. | The estimate and the ceiling disagree by a factor this large on a real user's input; the GPU code lives in the sibling repo `llm-proteinDesigner`. Needs the Modal log for where the time went before either number is changed. |
| bindcraft, 2026-08-28 | bindcraft pilot | HTTP 400 fetching the input PDB from its signed URL — the run never got its input. | Storage / signed-URL path. One row, no second instance in the window; needs the Modal log to say whether the URL had expired or was never valid. |
| b5707a1d (2026-09-09) | boltzgen pilot | Subprocess hit 6600 s at 421 residues. Preflight only soft-warns there: comfort zone 360, hard cap 600. | Either the soft warning should become a refusal above some length, or the ceiling should rise. Both need a measured runtime-vs-length point, i.e. GPU spend. |
| af2, 2026-08-27 | af2 | `parser:pdb_missing`. | Same exit-0-with-no-output class as the colabfold rows, but AF2's image ships hhsearch and its worked example (`tools/af2/example/result.json`) completed 10/10 WITH `use_templates: true`, so templates are not the explanation here. Needs the Modal log. |
| c70da107 (2026-08-27) | af2 | 6 h 15 wall clock, 131 GPU-seconds consumed, `inputs._progress` claiming stage "complete" with 0 of 1 designs. Hold of $4.50 released in full. | A job that reported "complete" and produced nothing is a heartbeat/finalisation question, not a tool one. Needs the Modal log. |

## User-facing copy

Checked as asked. Before this change:

- `templates/job_detail.html` rendered, for `status == 'failed' and job.error`
  only, `Category: <raw error bucket>` plus a `<pre>` of the raw detail. No
  plain-words cause, no mention of the refund, and **nothing at all** for a
  `timeout` row (which typically carries no `error` dict) — job c70da107 saw
  an empty panel after a $4.50 hold was released.
- `shared/email.py::_result_summary` had the right sentence ("your wallet was
  not charged") but gated it on `job.status == "failed"`, so the same timeout
  was told nothing, although every non-succeeded status takes that tone.

Both now call `shared/jobs.py::failure_notice`, which returns the plain-words
cause for the `failure_class` and a `refunded` flag that mirrors the routing
in `_settle_wallet_hold_for_completed_job` — including the two cases where
"you were not charged" would be FALSE: a billed class with consumed seconds
(`user_cancelled`), and a NULL `failure_class` with consumed seconds, which
takes that function's legacy settle arm.
