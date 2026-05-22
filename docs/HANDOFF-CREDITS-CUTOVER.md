# Handoff — Credits → Wallet Cutover (in progress)

**Date:** 2026-05-22
**Branch:** `wallet-credits-cutover` (in the `tools-hub` repo)
**Follows:** `HANDOFF-WALLET-PIVOT-SESSION-14.md`

---

## What this is

Finishing the wallet pivot: making the USD wallet the **sole** billing path for
GPU jobs and removing the legacy credits system that was still running in
parallel on the job submit + completion path.

**Scope chosen by the user: "money-path only."** That means:

- Wallet becomes the sole gate + charge for GPU jobs. Remove the parallel
  credits gate, `record_spend`, and `_refund_unused_credits`.
- Fix the latent cancel-job wallet-hold leak (see below).
- Switch the tool-form and job-page cost display to USD.
- **Leave alone** (deferred Scope-B cleanup): the `shared/credits.py` ledger
  helper functions themselves (they stay *defined* but uncalled), the navbar
  "credits" readout, the `/account` credits-ledger panel, `UserContext.balance`.
  Do NOT delete `shared/credits.py` functions in this pass.

### Latent bug being fixed as part of the cutover

`shared/jobs.py::cancel_job` refunded credits but **never released the wallet
hold** — its docstring claimed it did, but no `release_hold` call existed.
Harmless while credits was the real money path; once the wallet is the sole
path, every cancelled job would permanently strand a hold against the user's
balance. The cutover wires `cancel_job` to release the hold.

---

## Where the work lives — read this first

- The work is in the **`tools-hub` repo** at
  `C:\Users\lab\Documents\Claude_projects\tools-hub`, on branch
  **`wallet-credits-cutover`** (created for this task).
- The Claude Code worktree this was started from is the *Ranomics website*
  worktree — **not** tools-hub. Work directly in the tools-hub repo via
  absolute paths; just make sure the tools-hub repo stays on the
  `wallet-credits-cutover` branch.
- **`tools-hub` `main` auto-deploys to prod.** Do NOT commit to / merge to
  `main`. Nothing reaches prod until the user merges.
- User preference: **no auto-commits.** Leave the work uncommitted unless the
  user asks to commit. If committing, confirm the repo is on
  `wallet-credits-cutover`, not `main`.
- Money-IN (Stripe topup) is **already 100% wallet** — not part of this cutover.

---

## DONE — source edits already applied

### `app.py`
- Import block: removed `record_spend` and `requires_credits` from
  `from shared.credits import (...)` (kept `load_user_context`, `recent_ledger`).
- `example_gpu_submit`: removed the `@requires_credits(...)` decorator; updated
  its docstring and the stub-route comment above it.
- `requires_wallet` decorator: updated the no-user fall-through comment (dropped
  the `@requires_credits` reference).
- `tool_submit`: docstring "debit credits" → "place a wallet hold"; removed the
  `if ctx.balance < preset.credits_cost: return redirect(...)` gate; removed the
  `record_spend(...)` block after `set_modal_call`; updated stale comments
  (workspace gate, PDB pre-flight, create_job); modal-submit-failure error copy
  "No credits were charged" → "Your wallet was not charged".
- `job_cancel` route: docstring "full credit refund" → "wallet hold released";
  removed `"credits_refunded": job.credits_cost` from the JSON response (now
  returns just `id` + `status`).

### `shared/jobs.py`
- `complete_job`: docstring updated; removed the `_refund_unused_credits(fresh)`
  call.
- **Deleted the entire `_refund_unused_credits` function.**
- `cancel_job`: docstring + CAS-loss comment updated; replaced the credits-refund
  block with `_settle_wallet_hold_for_completed_job(fresh)` — this reads
  `inputs._wallet.hold_tx_id` and, for a cancelled row with no GPU time, calls
  `release_hold`. Idempotent; no-op for jobs with no hold.

### `webhooks/modal.py`
- `_apply_terminal` docstring: "refund unused credits" → "settle the wallet hold".

### `templates/job_detail.html`
- Removed the `{{ job.credits_cost }} credits` figure from the header.
- Cancelled-status copy → "Cancelled. Your wallet was not charged for this run."
- Cancel-confirm JS prompt updated; cancel-success JS no longer reads
  `credits_refunded`.

### `templates/jobs_list.html`
- Removed the "Credits" column (`<th>` + `<td>`); empty-state copy updated.

### `templates/components/about_panel.html`
- Removed the "Credits" column from the runtime table; "Runtime & cost" →
  "Runtime". (The `meta.py` `runtime_table` dicts still carry a now-unrendered
  `credits` key — harmless dead data, left for Scope-B.)

---

## Task 5 — tool form cleanup (DONE by subagent — verify)

A background subagent completed the form cleanup. **Trust-but-verify: the next
agent should confirm the 9 forms still render valid Jinja/HTML** (the Task 7
suite run covers route-render tests; also spot-check a couple of forms and run
`grep -rn cost_preview templates/`). What it reported doing:

- **All 9 GPU tool forms cleaned** (`templates/tools/*_form.html`). Note: only
  `mpnn_form.html` used the shared `cost_preview` macro — the other 8 had their
  own **inline** cost-preview blocks (scoped CSS + `#preset-cost-preview` div +
  `data-credits`/`data-minutes` attrs + a cost-preview branch of the form JS).
  The subagent removed those inline blocks too. It kept the separate
  `preset-description` div/JS (a distinct feature) and nudged one margin.
- **`templates/components/cost_preview.html` deleted.**
- **Preset labels:** 8 `tools/*/__init__.py` files updated; `bindcraft` was
  already clean. Real label format was `"Smoke — ubiquitin demo, 0 credits"`
  (descriptor + trailing `, N credits` clause); the subagent dropped only the
  trailing credits clause, kept the tier name + descriptor. `pilot`-tier labels
  had no credits clause and were untouched.
- Left alone (pre-existing orphans): unused `preset_runtime_rows` Jinja sets in
  `bindcraft_form.html` and `pxdesign_form.html`.

No `.py` logic, routes, or wallet partials were touched by the subagent.

---

## DONE — Task 6: update the test suite

Completed 2026-05-22. All 6 affected test files updated; `test_workspaces.py`
needed no change (its `record_grant / record_spend` mention is a comment and
both helpers still exist per Scope-B). What changed:

- **test_jobs_phase4.py** — docstring fixed; `_refund_unused_credits` patch
  dropped from the 4 `TestCompletionEmail` tests; `TestRefundUnusedCredits`
  deleted; `TestCancelJob` rewritten to assert `release_hold` not
  `record_refund` (`test_cancel_partial_spend_*` deleted — hold release is
  all-or-nothing); now-unused `ToolJob` import removed.
- **test_cancel_race.py** — module + class docstrings updated; the 3 race
  tests now patch `shared.wallet.release_hold` / `settle_hold`.
- **test_orphan_jobs.py** — `patch("app.record_spend")` dropped;
  `TestGetSpentForJob` docstring de-staled.
- **test_workspace_route_gating.py** — `patch("app.record_spend")` dropped
  from both submit `with`-blocks.
- **test_workspace_completion.py** — `_refund_unused_credits` patch dropped
  from the 2 `complete_job` integration tests.
- **test_jobs.py** — `_refund_unused_credits` `monkeypatch.setattr` dropped
  from the 3 `TestCompleteJobInvokesSettle` tests.

Also fixed 2 stale `_refund_unused_credits` doc references the cutover's
jobs.py edits missed: the `_charge_workspace_for_completed_job` docstring and
a comment in `_settle_wallet_hold_for_completed_job`.

The original per-file analysis is kept below for reference — line numbers are
now stale.

### `tests/test_jobs_phase4.py`
- Module docstring (~ll.1-14): "cancel_job refunds the full credit cost" →
  "releases the wallet hold."
- `TestCompletionEmail` (4 tests, ~ll.156-210): each `with` block contains
  `, patch.object(jobs_mod, "_refund_unused_credits", lambda _job: None)` — this
  now AttributeErrors (function deleted). Remove that patch from all 4. The
  5-line `with` block is identical across the 4, so a `replace_all` Edit works.
  Tests otherwise stay valid (the fake row has empty `inputs`, so
  `_settle_wallet_hold` / `_charge_workspace` early-return harmlessly).
- `TestRefundUnusedCredits` class + its "1b" section header (~ll.213-284):
  **delete entirely** — it calls `jobs_mod._refund_unused_credits` directly.
  Proration now lives in the wallet `settle_hold` SQL (covered by test_wallet.py).
- `TestCancelJob` (~ll.286-396): rewrite.
  - `test_cancel_running_job_refunds_and_marks` → rewrite as
    "releases hold and marks": prime row with
    `inputs={"_wallet": {"hold_tx_id": "hold-abc"}}`, patch
    `shared.wallet.release_hold`, assert it's called once + status cancelled +
    `fake_modal.cancel` called.
  - `test_cancel_orphaned_row_skips_refund` → rewrite as "hold-less row skips
    release": row with `inputs={}`, patch `shared.wallet.release_hold`, assert
    NOT called + status cancelled.
  - `test_cancel_partial_spend_refunds_only_what_was_spent` → **delete**
    (partial-spend has no wallet analogue; a hold release is all-or-nothing).
  - `test_cancel_already_terminal_is_refused` → keep; assert no `release_hold`
    instead of no `record_refund`.
  - `test_cancel_without_modal_fc_still_marks_cancelled` → keep; drop the
    credits patches.

### `tests/test_cancel_race.py`
- `TestCancelBeatsLateWebhook.test_late_webhook_is_noop_after_cancel`: Stage 1 —
  give the row `inputs={"_wallet": {"hold_tx_id": "..."}}`, patch
  `shared.wallet.release_hold`, assert called once. Stage 2 (late webhook →
  `complete_job` CAS-loss) — patch `shared.wallet.settle_hold` + `release_hold`,
  assert neither called.
- `TestWebhookBeatsCancel.test_cancel_after_success_is_rejected`: patch
  `shared.wallet.settle_hold`/`release_hold` instead of `record_refund`; the row
  has empty `inputs` so nothing fires anyway; still assert
  `(None, "already_succeeded")`. Update the stale `_refund_unused_credits`
  comment (~ll.239-242).
- `TestCancelCasLostRefundSkipped.test_refund_not_issued_when_cas_loses`: patch
  `shared.wallet.release_hold`, assert NOT called when the CAS loses.
- `TestWebhookHandlerAlreadyTerminal`: unaffected.
- Module docstring describes credits-era races — light update for accuracy.

### `tests/test_orphan_jobs.py`
- `test_with_file_attached_does_call_create_job` (~l.144): remove the
  `patch("app.record_spend")` line from the multi-`with` chain — `record_spend`
  is no longer imported into `app`, so the patch AttributeErrors. Nothing else
  changes.
- `TestGetSpentForJob` (~ll.174-237): `get_spent_for_job` is KEPT, so these
  tests still pass. Optional: de-stale the class docstring sentence claiming
  "cancel_job now uses" it (no longer true).
- `test_no_file_no_reuse_token_skips_create_job`, `TestNonPdbToolNotAffected`:
  unaffected.

### `tests/test_workspace_route_gating.py`
- Two `with` blocks (~ll.155, ~312) end with `, patch("app.record_spend")` —
  remove `patch("app.record_spend")` from both (same AttributeError reason).

### `tests/test_workspace_completion.py`
- Two tests (~ll.534, ~557) have
  `patch.object(jobs_mod, "_refund_unused_credits", lambda _job: None)` — remove
  that patch from both (function deleted). Tests are about the workspace charge
  and should pass with it removed.

### `tests/test_jobs.py`
- Mostly fine — already tests the wallet settle path
  (`_settle_wallet_hold_for_completed_job`, `mid_run_monitor`).
- `TestCompleteJobInvokesSettle` (3 tests, ~ll.445-571): each does
  `monkeypatch.setattr(jobs_mod, "_refund_unused_credits", lambda j: None)` —
  `monkeypatch.setattr` on a missing attribute fails. **Remove that line from
  all 3 tests.**

### `tests/test_workspaces.py`
- Only match is a comment in `patch_supabase` (~l.174) mentioning
  "record_grant / record_spend". Comment only — not a blocker; optionally tidy.

### Watch-for (not found in grep, confirm via the suite run)
- No test appears to assert the example-gpu route debits a credit, but
  `@requires_credits` was removed from `example_gpu_submit` — if the suite
  surfaces an example-gpu billing assertion, update it.

**Test fixture note:** `test_jobs.py` has a `_wallet_row(**over)` helper that
builds a row with `inputs._wallet.hold_tx_id = "tx-hold-001"`. The
`test_jobs_phase4.py` / `test_cancel_race.py` `_row(**over)` helpers accept an
`inputs=` override — pass `inputs={"_wallet": {"hold_tx_id": "..."}}` to give a
test row a hold to release. `cancel_job` → `_settle_wallet_hold_for_completed_job`
→ (cancelled, gpu_seconds 0) → `release_hold(hold_tx_id, reason="cancelled")`;
patch `shared.wallet.release_hold` to observe it.

---

## DONE — Task 7: run the suite

Completed 2026-05-22. Full suite green: **648 passed, 6 skipped, 0 failed** in
~73s (`venv\Scripts\python.exe -m pytest -q`, `PYTHONIOENCODING=utf-8`). The 6
skips are pre-existing env-gated tests, unrelated to the cutover. (The handoff
cited a 646 baseline; Task 6 removed 8 now-obsolete credits tests and the
branch's collected count had drifted up — net result is zero failures.)

---

## Going live (the user's separate steps — do not merge before these)

The pushed code is correct on its own, but the cutover should not reach prod
until (from `HANDOFF-WALLET-PIVOT-SESSION-14.md`):

1. Migration `0020_wallet_corrections.sql` is applied to Supabase project
   `wjlhbxfnihboqebdvnns` (makes `settle_hold` idempotent and the daily cap
   race-safe — both load-bearing once the wallet is the sole path).
2. The test-user auto-reload row is resolved
   (`03e51184-4d04-4acd-ab22-0cbd7fa08c77`).
3. A live MPNN spend-path test is run on prod.

Then the `wallet-credits-cutover` branch can be merged to `main` (which deploys).

---

## Discovered during Task 6/7 — decide before merge

Two surfaces still carry credits-era content that the cutover's source-edit
pass did not enumerate. Neither breaks the test suite; both are user-facing and
were left for a decision rather than changed autonomously.

1. **`shared/email.py` `_result_summary` (failed-job completion email).** For a
   failed job that consumed no GPU time it still emits "your N credits were
   refunded". Post-cutover that job's wallet hold is *released* (user billed
   nothing) — the economics are right but the wording is wrong. The copy is
   locked by `tests/test_email_failure_copy.py` (asserts
   `"10 credits were refunded"`), so fixing the copy means updating that test
   too. Recommend: reword to wallet language (mirror job_detail.html's "your
   wallet was not charged") before merge.
2. **`templates/components/submit_cta.html` cost-confirm modal.** The
   `submit_cta_script` macro reads each preset's `data-credits` attribute and
   pops a "this run will cost up to N credits" confirm dialog. The Task 5 form
   cleanup removed `data-credits` from the preset `<option>`s, so the modal now
   always reads 0 and never fires — dead code, not a crash. Recommend: delete
   the credits-confirm modal (the USD wallet has its own topup/confirm flow) or
   convert it to a USD figure.

---

## Scope-B follow-ups (explicitly deferred — NOT this pass)

- Remove `UserContext.balance` / `get_balance` and all `ctx.balance` readers;
  the navbar "credits" readout; the `/account` credits-ledger panel; delete the
  now-dead `shared/credits.py` credit functions (`record_spend`, `record_refund`,
  `get_spent_for_job`, `requires_credits`, `recent_ledger`, `record_grant`).
- `tools/*/meta.py` `runtime_table` / `PRESET_RUNTIME` still carry dead `credits`
  keys (harmless, no longer rendered).
- `tool_jobs.credits_cost` column is still populated by `create_job` (harmless
  historical data).
- `wallet_topup_gate`'s `_action=topup_and_run` form action appears unimplemented
  in `tool_submit` — pre-existing, unrelated, worth noting.
