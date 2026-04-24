# Phase 4 — Cross-tool workflow, cancel, pagination, component system

Session: 2026-04-24. Continues from Phase 3 (commit `3ba4c5d`).

## What landed

- **Pagination on `/jobs`** — new `list_jobs_paginated(user_id, page=, page_size=)` in [shared/jobs.py](../shared/jobs.py) using PostgREST `.range()` + `count="exact"`. Route at [app.py](../app.py) accepts `?page=N` (default 1, page_size 25), redirects past-the-end pages back to `total_pages`, and renders Previous/Next controls in [templates/jobs_list.html](../templates/jobs_list.html).
- **Cancel running jobs** — end-to-end:
  - Migration [supabase/migrations/0012_cancel_jobs.sql](../supabase/migrations/0012_cancel_jobs.sql) adds `cancelled` to the `tool_jobs.status` CHECK constraint.
  - `ModalClient.cancel(function_call_id)` in [gpu/modal_client.py](../gpu/modal_client.py) — best-effort `fc.cancel()`, returns `{ok, error}`. Offline stub and missing-modal envs treat cancel as success so tools-hub stays the authoritative state.
  - `cancel_job(job_id, *, user_id, modal_client)` in [shared/jobs.py](../shared/jobs.py) — owner-scoped, rejects already-terminal jobs, best-effort Modal cancel, marks row `cancelled`, fully refunds `credits_cost` via `record_refund`. Idempotent.
  - `POST /jobs/<id>/cancel` route in [app.py](../app.py) — protected by `@login_required` + `@idempotent()`. Returns `{id, status, credits_refunded}` on success, 404/409 on failure.
  - Cancel button + JS confirmation dialog on [templates/job_detail.html](../templates/job_detail.html) — visible only while pending/running. Status poller now treats `cancelled` as terminal.
  - Webhook receiver at [webhooks/modal.py](../webhooks/modal.py) treats `cancelled` as terminal so a late Kendrew COMPLETED POST on a user-cancelled job is a quiet no-op.
- **Job-complete email verified** — already wired via `complete_job` → `_send_completion_email`. Phase 4 adds the test coverage in [tests/test_jobs_phase4.py](../tests/test_jobs_phase4.py::TestCompletionEmail): confirms email fires on `succeeded`/`failed`, not on `timeout`/`cancelled` or replayed terminal calls.
- **Shared component macros:**
  - [templates/components/status_badge.html](../templates/components/status_badge.html) — tints pills by status (green for succeeded, blue for running, red for failed, amber for timeout, grey for cancelled/pending). Consumed by [jobs_list.html](../templates/jobs_list.html) and [job_detail.html](../templates/job_detail.html).
  - [templates/components/results_shell.html](../templates/components/results_shell.html) — `results_panel` macro wrapping the panel header + candidate_table + empty-state caller block + GPU-time footer + Phase 4 handoff buttons. All four tool results partials ([bindcraft_results.html](../templates/tools/bindcraft_results.html), [rfantibody_results.html](../templates/tools/rfantibody_results.html), [boltzgen_results.html](../templates/tools/boltzgen_results.html), [pxdesign_results.html](../templates/tools/pxdesign_results.html)) rewritten as 15-line `{% call results_panel(...) %}` blocks — previously 29-37 lines of hand-rolled panel markup each.
- **Tool-to-tool handoff** — `from_job=<job_id>` query param on `/tools/<tool>` form. Unlike `clone_from` (same-tool, full-parameter reuse), `from_job` is cross-tool: copies only `target_chain` + `hotspot_residues` + the PDB reuse token, and defaults preset to `pilot`. The job detail view ([app.py::job_detail](../app.py)) computes `send_target_tools` for succeeded jobs with a staged PDB and passes it down through the results partial to the macro, which renders a "Reuse this target in: [RFantibody] [BoltzGen] [PXDesign] ..." button row below the candidate table.

## Tests

New suite: [tests/test_jobs_phase4.py](../tests/test_jobs_phase4.py) — 9 tests, all passing.
- `TestCompletionEmail` × 4 — send on succeeded/failed, skip on timeout/replay.
- `TestCancelJob` × 3 — running job refunds + marks cancelled + calls Modal; terminal jobs rejected without refund; missing FC is fine.
- `TestListJobsPaginated` × 2 — rows + total count with slicing, page/page_size clamping.

Full suite: `30 passed, 6 skipped` (unchanged RLS skips).

## Verification

- Jinja parse check via `app.jinja_env.get_template(...)` on every new/modified template — all 9 parse cleanly.
- Render smoke via `render_template` in a test context — job_detail with status=succeeded renders 19.9 KB containing `Candidates (1)`, `Reuse this target in:`, status badge, handoff buttons for RFantibody/BoltzGen/PXDesign. Running-status render shows cancel button + `You can close this tab` block.
- Dev server (`tools-hub-dev` on :5000) serving `/pricing` 200 and `/jobs` 302→login as expected.

## Pending / carry-overs

1. **Migration 0012 needs to run** in Supabase prod (and staging, once it exists) before the `cancelled` status actually persists — the service-role insert will reject `cancelled` under the current CHECK constraint. The migration is safe to re-run.
2. **Cancel race with inline-return smoke tier:** a smoke run that returns inline during the same poll cycle as the user's cancel could theoretically land COMPLETED before cancel marks cancelled. Current mitigation: `cancel_job` uses the atomic terminal-state check, and the webhook no-ops on cancelled rows. Not verified under stress.
3. **Empty `_default_results.html`** — fallback partial for unknown tools does not yet show the handoff buttons. Low priority (no unknown-tool jobs in practice).
4. **Tool-to-tool handoff requires the source job to have staged a PDB** (i.e. pilot-tier with a user upload). Smoke-tier jobs using the fixture PDB don't surface the buttons. Intentional — demo-tier re-runs should use `Run again`, not a real-target handoff.

## Deploy checklist

- [ ] Apply `0012_cancel_jobs.sql` in Supabase production.
- [ ] Confirm `RESEND_API_KEY` is set in Railway (email notifications will log-and-skip without it).
- [ ] Smoke-test `/jobs/<id>/cancel` on staging with a real pending BindCraft submission.
