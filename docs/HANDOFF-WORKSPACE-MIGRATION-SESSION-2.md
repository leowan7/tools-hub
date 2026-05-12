# Session Handoff — Workspace Migration (Session 2)

**Created:** 2026-05-12
**Updated:** 2026-05-12 (items #6 + #7 shipped in code — session 2 continued)
**Branch:** `main` (uncommitted — session-1 diff PLUS the item-#6 + item-#7 wiring below)
**Previous handoff:** [`docs/HANDOFF-WORKSPACE-MIGRATION.md`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/docs/HANDOFF-WORKSPACE-MIGRATION.md)
**Plan file:** [`C:\Users\lab\.claude\plans\for-my-tools-hub-cheerful-nygaard.md`](file:///C:/Users/lab/.claude/plans/for-my-tools-hub-cheerful-nygaard.md)

This continues from the session-1 handoff. Read that first for the **why** behind the rewrite — this note only covers what changed in session 2.

---

## What got done this session

### Blocker #1 — Migration 0014 applied to Supabase
Applied [`supabase/migrations/0014_workspaces.sql`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/supabase/migrations/0014_workspaces.sql) via the Supabase SQL editor (project `wjlhbxfnihboqebdvnns`).

Validated with a single consolidated check query — all 9 checks PASS:

| Check | Result |
|---|---|
| `pgcrypto` extension | PASS (v1.3) |
| enum `workspace_sku` | PASS (`workspace_standard,workspace_xl`) |
| enum `workspace_status` | PASS (`active,expired,refunded`) |
| table `public.workspaces` | PASS (16 columns) |
| indexes on workspaces | PASS (5+ indexes incl. unique on `stripe_payment_intent_id`) |
| RLS enabled | PASS (`rowsecurity=true`) |
| RLS policy `workspaces_self_read` | PASS (SELECT) |
| view `workspaces_active` | PASS |
| view `workspaces_history` | PASS |

The validation query is reproducible — see session-1 handoff "Verification queries" section or scroll the SQL editor history (saved as "Workspace purchases and usage tracking").

### Item #6 — `charge_for_job` wired into `complete_job`
Modal compute is now deducted from the active Workspace cap when a job
reaches a terminal state (`succeeded` or `failed` with measured GPU
time). New code in [`shared/jobs.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/shared/jobs.py):

| Change | Location | Notes |
|---|---|---|
| `create_job(target_pdb_id=..., workspace_id=...)` kwargs | `create_job` body | Optional. Stashes context in `inputs._workspace` (no schema migration) — same jsonb pattern as the heartbeat `_progress` key. Old call sites compile unchanged. |
| `_charge_workspace_for_completed_job(job)` helper | new function | Reads `inputs._workspace.target_pdb_id`, snapshots the pre-charge `modal_spent_usd`, calls `shared.workspaces.charge_for_job`, and on a 0%→80% crossing dispatches `shared.email.send_workspace_cap_warning`. Wrapped in try/except so a flaky Resend POST or workspaces-module hiccup never aborts terminal-state finalisation. |
| Wired into `complete_job` | between `_refund_unused_credits` and `_send_completion_email` | The legacy credits-ledger refund path still runs — Workspaces and credits coexist (per the "credits_ledger stays in place" gotcha in the original handoff). |

GPU SKU resolution priority: `result.gpu_sku` (pipeline self-report) →
`inputs._workspace.gpu_sku` (submission-time hint) → `None`
(`charge_for_job` falls back to the conservative
`DEFAULT_USD_PER_SECOND` rate).

**Verification:**
- New file [`tests/test_workspace_completion.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/tests/test_workspace_completion.py) — 19/19 pass
- Adjacent suites unchanged: `test_workspaces.py` 35/35, `test_jobs_phase4.py` all green
- Full suite: **353 passed, 6 skipped** (was 334 before — +19 new tests, zero regressions)

### Item #7 — Tool routes gated on Workspace pre-flight

`/tools/<tool>/submit` (the central GPU-tool submit handler covering all
8 design tools via the adapter pattern) now consults
`shared.workspaces.workspace_preflight` whenever the form carries
`workspace_id` + `target_pdb_id`. Submissions without that context fall
through to the legacy credits gate (transitional — flip to a hard
reject once Phase 4 validation is clean and all entry points route
through Workspace activation).

| Change | Location | Notes |
|---|---|---|
| `workspace_hidden_inputs` macro | new in [`templates/tools/_prefill.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/tools/_prefill.html) | Renders two `<input type="hidden">` for `workspace_id` and `target_pdb_id`. Guarded — only emits when both are present. |
| Macro imported + invoked | all 9 tool form templates (af2, bindcraft, boltzgen, colabfold, esmfold, mpnn, pxdesign, rfantibody, rfdiffusion) | One-line addition right after each `<form method="POST">`. |
| `tool_form` GET handler | [`app.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/app.py) `tool_form` | Reads `workspace_id` + `target_pdb_id` query params, builds `workspace_ctx` dict, forwards to template. |
| `tool_submit` POST handler | [`app.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/app.py) `tool_submit` | Reads form values, calls `workspace_preflight` when present, redirects to `/workspaces/new` on `no_workspace` and to `/workspaces/<id>` on `cap_exceeded` / `expired`. Forwards `target_pdb_id` + `workspace_id` to `create_job` so item-#6's completion-side charge wiring picks them up. All 9 in-handler `render_template(adapter.form_template, ...)` error renders also forward `workspace_ctx` so a validation error doesn't drop the user out of the workspace flow. |

The /workspaces/<id>/detail page already emits the right link shape
(`?workspace_id=...&target_pdb_id=...`), so no template change there.

**Verification:**
- New file [`tests/test_workspace_route_gating.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/tests/test_workspace_route_gating.py) — 8/8 pass
  - GET form: workspace query params surface as hidden inputs (3 tests)
  - POST submit + active workspace: preflight called, IDs propagate to create_job
  - POST submit + no_workspace: 302 redirect to `/workspaces/new`
  - POST submit + cap_exceeded: 302 redirect to `/workspaces/<id>`
  - POST submit + expired: 302 redirect to `/workspaces/<id>`
  - POST submit + no workspace context: legacy fallback, no preflight call
- Full suite: **361 passed, 6 skipped** (was 334 at session-2 start; +27 new across #6 + #7, zero regressions)

### Blocker #2 — Stripe test-mode products created
Created via Stripe dashboard at `dashboard.stripe.com/test/products/create`:

| SKU | Stripe price ID (test mode) |
|---|---|
| `workspace_standard` ($499 one-time) | `price_1TW0O9HK3YN42tFl3ncD11yu` |
| `workspace_xl` ($2,499 one-time) | `price_1TW0PzHK3YN42tFlu2uisT2X` |

`.env` updated at [.env:35-41](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/.env) — new vars added above the legacy block. The legacy `STRIPE_PRICE_SCOUT_PRO/LAB/LAB_PLUS` lines kept in place (dead in code) until blocker #3 archives the underlying Stripe products.

**Verification:**
- `tests/test_workspaces.py` — 35/35 pass
- `billing.tiers.price_to_sku()` resolves both IDs end-to-end through `.env`

---

## What we tried that didn't work (avoid re-trying)

### `supabase db push --db-url <pooler-url>`
The pooler URL stored in `supabase/.temp/pooler-url` contains no embedded password — format is `postgresql://postgres.wjlhbxfnihboqebdvnns@aws-1-us-east-2.pooler.supabase.com:5432/postgres`. CLI errors with `failed SASL auth (28P01)` and asks for `SUPABASE_DB_PASSWORD`. No such env var present locally.

**Next-session paths if CLI access is needed:**
- Set `SUPABASE_DB_PASSWORD` in `.env` (DB password from Supabase dashboard → Settings → Database)
- OR run `supabase login` to store an access token, then `supabase db push --linked`
- OR keep doing SQL editor for one-off migrations (what we did — fine for low frequency)

### `stripe products list` with `.env` STRIPE_SECRET_KEY
The `sk_test_...` key in `.env` is **expired** — Stripe API returns "The API key provided has expired."

**Next-session paths:** either regenerate from Stripe dashboard → Developers → API keys, or keep using dashboard for product creation (what we did). The expired key doesn't break the running app since Railway prod uses a different (live) key.

---

## State at end of session

```
Branch: main
Tests: 361 passed, 6 skipped (full suite)
       35/35 workspaces  +  19/19 workspace_completion  +  8/8 workspace_route_gating
Uncommitted diff: session-1 baseline (12 modified + 4 untracked) PLUS:
                    M  shared/jobs.py                          (item #6)
                    M  app.py                                  (item #7 GET + POST)
                    M  templates/tools/_prefill.html           (item #7 macro)
                    M  templates/tools/{af2,bindcraft,boltzgen,colabfold,
                       esmfold,mpnn,pxdesign,rfantibody,rfdiffusion}_form.html
                                                               (item #7 macro hookup x9)
                    ?? tests/test_workspace_completion.py      (item #6 coverage)
                    ?? tests/test_workspace_route_gating.py    (item #7 coverage)
                    M  docs/HANDOFF-WORKSPACE-MIGRATION-SESSION-2.md
Supabase: migration 0014 applied + verified (Blocker #1 done)
Stripe (test mode): 2 workspace products live + price IDs in local .env (Blocker #2 done)
Stripe (live mode): nothing done yet — Railway prod still on legacy live products
```

When you do commit, use one squashed commit referencing the full
Workspace pivot — see session-1 handoff "Files changed" section for the
session-1 list, plus the four files added by items #6 + #7 above.

---

## What's still open (priority order)

### Pre-launch blockers (still from session 1)

#### 3. Archive old subscription products in Stripe + remove env vars
- Stripe dashboard → archive (don't delete) `Scout Pro`, `Lab`, `Lab+`, credit top-ups
- Remove `STRIPE_PRICE_SCOUT_PRO`, `STRIPE_PRICE_LAB`, `STRIPE_PRICE_LAB_PLUS` from `.env` AND Railway env
- Do this in **both** test mode and live mode

#### 4. Enable Stripe Tax (org-level toggle)
- Stripe dashboard → Tax → Enable
- `billing/checkout.py:146` passes `automatic_tax={"enabled": True}` — checkout will 400 if Tax is off
- Easier to do this before any Phase 4 E2E test card flow

#### 5. Verify `STRIPE_WEBHOOK_SECRET` set in Railway prod
- Without this, all webhooks return 400 (signature verification at `webhooks/stripe.py:65`)
- Already set locally; verify in Railway dashboard before flipping live products

### High-priority code work

#### 6. ~~Wire `charge_for_job` into Modal completion webhook~~ — DONE (session 2)
See "Item #6" section above. Submission-side stamping is via the new
`create_job(target_pdb_id=..., workspace_id=...)` kwargs — those kwargs
are wired to a route in item #7 below for each tool.

#### 7. ~~Gate tool routes on Workspace preflight~~ — DONE (session 2)
See "Item #7" section above. Single shared submit route handles all 8
tools via the adapter pattern — one handler change covered the lot.
Legacy credits gate kept as transitional fallback for entry points that
don't yet route through Workspace activation (Scout handoff, direct
form URLs); flip to a hard reject once Phase 4 validation runs cleanly
on PD-L1 + lysozyme.

**Follow-up worth tracking** (not blocking launch):
- Update Scout handoff path to require workspace activation before
  forwarding to the tool form (today it lands on `/tools/<tool>?handoff=...`
  with no workspace context — falls through legacy credits).
- Add a `WORKSPACE_REQUIRED=1` env-var flag to flip the legacy
  fallback into a hard reject, then enable in Railway.

### Medium priority (from session 1)
- #8 `expire_workspaces` daily cron in Railway
- #9 Sample reports on homepage (chicken-and-egg with Phase 4 verification runs)
- #10 Pre-sell to 3–5 Ranomics CRO contacts

### New work surfaced in session 2

#### 11. Create LIVE-mode Stripe products (when ready to ship)
The two price IDs in `.env` are **test mode only**. Before Railway prod can sell Workspaces:
- Repeat blocker #2 in Stripe dashboard → switch to live mode → create same two products (same names, descriptions, prices, one-time)
- Capture live-mode `price_...` IDs (will start `price_1...` but be different)
- Set in Railway prod env: `STRIPE_PRICE_WORKSPACE_STANDARD=...` + `STRIPE_PRICE_WORKSPACE_XL=...`
- Do **not** copy test-mode IDs to prod — they'll silently fail in live mode

#### 12. Regenerate local Stripe test secret key
`.env` has an expired `sk_test_...`. Not blocking the running app (it won't hit Stripe locally without a checkout attempt), but blocks any local Stripe API debugging and the `/stripe` CLI commands. Regenerate from Stripe dashboard → Developers → API keys (test mode).

---

## Phase 4 validation gates — still un-run

None of the 10 E2E flows from the session-1 handoff have been executed yet. The order of operations is:

1. Knock out blockers #3, #4, #5, #12
2. Wire items #6 and #7 (Modal webhook + route gating)
3. Then start Phase 4 step 1: browse `localhost:<port>/pricing`, see 3 SKU cards, complete one Standard checkout with test card `4242 4242 4242 4242`, watch the webhook fire and the new `workspaces` row appear

The handoff's 10 validation steps are the gate for flipping live. Two consecutive clean runs on PD-L1 + lysozyme before announcing.

---

## Suggested first prompt for next session

> Resume the tools-hub Workspace migration. Session-2 handoff is at
> `docs/HANDOFF-WORKSPACE-MIGRATION-SESSION-2.md`. All Phase-1/2/3 +
> items #6 (charge wiring) and #7 (route gating) are done — full suite
> is 361 passed, 6 skipped, zero regressions. The uncommitted diff
> needs one squashed commit before pushing.
>
> Blockers #3/#4/#5/#11/#12 are dashboard-only and need Leo to do them
> from the Stripe and Railway UIs. After those, Phase-4 E2E validation
> (10 flows, two consecutive runs on PD-L1 + lysozyme) is the gate to
> flip live products.
>
> If you want more code work, the small follow-ups noted under item #7
> (Scout handoff routing, `WORKSPACE_REQUIRED` env-var flag) and item
> #8 (`expire_workspaces` daily cron) are next-best.
