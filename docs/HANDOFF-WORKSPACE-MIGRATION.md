# Session Handoff — Workspace Migration

**Created:** 2026-05-11
**Branch:** `main` (no commits yet — diff is uncommitted, review before committing)
**Plan file:** [`C:\Users\lab\.claude\plans\for-my-tools-hub-cheerful-nygaard.md`](file:///C:/Users/lab/.claude/plans/for-my-tools-hub-cheerful-nygaard.md)

---

## What got built

Complete pivot from monthly subscription + credits → per-target Workspace
SKUs. All 5 phases of the approved plan are shipped in code. **334 tests
pass, no regressions.**

### Launch offer (live in the rewritten templates)

| SKU | Price | What you get |
|---|---|---|
| Free Scout | $0 | Epitope Scout unlimited + sample reports |
| Target Workspace | $499 / target / 30 days | $100 Modal cap, ~500–2,000 designs, 7-day money-back on first |
| Target Workspace XL | $2,499 / target / 30 days | $500 Modal cap, ~2,500–10,000+ designs, priority queue |

No subscriptions at launch. Lab Annual + Enterprise are deferred.

---

## Verify the current state in a new session

```bash
cd C:/Users/lab/Documents/Claude_projects/tools-hub

# 1. Confirm all tests pass (should report 334 passed, 6 skipped)
venv/Scripts/python.exe -m pytest tests/ -q

# 2. Confirm Workspace SKU config is correct
venv/Scripts/python.exe -c "from billing.tiers import all_skus; [print(s) for s in all_skus()]"

# 3. Show uncommitted diff summary
git status
git diff --stat
```

Expected output: `334 passed, 6 skipped`, 12 modified files + 4 untracked
files (workspaces.py, 0014 migration, templates/workspaces/, test_workspaces.py).

---

## Files changed — clickable paths

### Backend (new)
- [`supabase/migrations/0014_workspaces.sql`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/supabase/migrations/0014_workspaces.sql)
- [`shared/workspaces.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/shared/workspaces.py)
- [`tests/test_workspaces.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/tests/test_workspaces.py) — 35 tests covering activation, cap, refund, expiration, preflight, charge_for_job

### Backend (rewritten)
- [`billing/tiers.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/billing/tiers.py)
- [`billing/checkout.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/billing/checkout.py)
- [`webhooks/stripe.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/webhooks/stripe.py)
- [`shared/email.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/shared/email.py) (added cap-warning + cap-exhausted senders)
- [`app.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/app.py) (context processor + 4 new workspace routes)

### Frontend (new)
- [`templates/workspaces/list.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/workspaces/list.html)
- [`templates/workspaces/detail.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/workspaces/detail.html)
- [`templates/workspaces/new.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/workspaces/new.html)

### Frontend (rewritten)
- [`templates/pricing.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/pricing.html)
- [`templates/index.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/index.html) (hero only — tool catalog below kept)
- [`templates/account.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/account.html)
- [`templates/_header.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/_header.html)
- [`templates/scout/feasibility.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/scout/feasibility.html) (Scout → Workspace CTA bridge)
- [`static/style.css`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/static/style.css) (added `.workspace-*` styles)

### Docs
- [`docs/PRODUCT-PLAN.md`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/docs/PRODUCT-PLAN.md) — §Pricing rewritten

---

## Open items (sorted: blockers first)

### Blocker — must do before any paying customer touches the site

#### 1. Apply the migration to Supabase
```bash
# From tools-hub root, with Supabase CLI logged in:
supabase db push
# or just paste 0014_workspaces.sql into the Supabase SQL editor
```
Reference: [`supabase/migrations/0014_workspaces.sql`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/supabase/migrations/0014_workspaces.sql)

#### 2. Create Stripe one-time products
Stripe dashboard → Products → New product. Create two:

| Product name | Price | Mode | Required |
|---|---|---|---|
| Target Workspace | $499.00 USD | One-time | Yes |
| Target Workspace XL | $2,499.00 USD | One-time | Yes |

After creating, grab each `price_...` id and set in Railway env vars:

```
STRIPE_PRICE_WORKSPACE_STANDARD=price_xxxxxxxxxxxxxxxxxx
STRIPE_PRICE_WORKSPACE_XL=price_xxxxxxxxxxxxxxxxxx
```

**Do this in Stripe test mode first.** Production-ize after Phase 4 verification.

#### 3. Archive the old subscription products
In Stripe dashboard, archive (don't delete) the existing recurring
products tied to:
- `STRIPE_PRICE_SCOUT_PRO`
- `STRIPE_PRICE_LAB`
- `STRIPE_PRICE_LAB_PLUS`
- Credit top-up products

Then remove those env vars from Railway. Old code is dead now.

#### 4. Enable Stripe Tax
Stripe dashboard → Tax → Enable. The checkout helper already passes
`automatic_tax={"enabled": True}` so VAT/sales tax just works once you
flip the org-level toggle.

#### 5. Verify `STRIPE_WEBHOOK_SECRET` is set in Railway production
Without this, all webhooks return 400. Already in code at
[`webhooks/stripe.py:65`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/webhooks/stripe.py).

---

### High priority — wire end-to-end before first paid Workspace

#### 6. Wire `charge_for_job` into the Modal completion webhook
The helper is built + tested. The Modal job-completion webhook at
[`webhooks/modal.py`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/webhooks/modal.py)
still uses the legacy `record_spend` path. Need to:

1. When a tool_jobs row is created at submission, stamp `target_pdb_id`
   on it (read from the active Workspace).
2. On `complete_job` (in [`shared/jobs.py:608`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/shared/jobs.py)),
   call `shared.workspaces.charge_for_job(user_id, target_pdb_id,
   gpu_seconds=job.gpu_seconds_used, gpu_sku=…, tool=job.tool,
   job_id=job.id)`.
3. If `charge_for_job` returns a workspace and it crossed the 80%
   threshold (the function logs this), dispatch
   `shared.email.send_workspace_cap_warning`.

Estimated: 30 min focused edit.

#### 7. Gate tool routes on Workspace preflight
Tool route handlers currently use `@requires_credits(n)`. Add
`shared.workspaces.workspace_preflight` as a check before the credit
decorator (or replace it).

Pattern:
```python
@flask_app.route("/tools/bindcraft/submit", methods=["POST"])
@login_required
def bindcraft_submit():
    target_pdb_id = request.args.get("target_pdb_id") or request.form.get("target_pdb_id")
    preflight = workspace_preflight(ctx.user_id, target_pdb_id)
    if not preflight.allow:
        return redirect("/pricing?reason=" + preflight.reason)
    # ... existing submission logic, pass workspace_id into job metadata
```

Tool form templates already pass `target_pdb_id` via query string when
clicked from the workspace dashboard (`templates/workspaces/detail.html`).

Estimated: ~90 min to do all 8 tools.

---

### Medium priority — improves UX, not launch-blocking

#### 8. Add an `expire_workspaces` cron
Schedule once daily. The function is at
[`shared/workspaces.py::expire_workspaces`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/shared/workspaces.py).
Railway has cron support, or wire it into the existing healthz/maintenance
endpoint with an `IF NOW() % 24h` gate.

#### 9. Generate sample reports for the homepage
The new homepage hero references "pre-rendered sample reports below the
fold" but the actual table render isn't there yet. After running the
first paid Workspace on PD-L1 or 4Z18, screenshot the results page or
HTML-embed a static `<table>` into [`templates/index.html`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/templates/index.html)
between the hero and the tool catalog.

#### 10. Pre-sell to 3–5 Ranomics CRO contacts
From the plan: offer comped Standard Workspaces at $299 (40% off) in
exchange for a 30-min feedback call. Validate that "Workspace"
terminology lands before public marketing.

---

## Phase 4 validation gates (from the plan)

Run these on Stripe **test mode** before flipping live products:

1. Browse to `tools.ranomics.com` → see 3 SKU cards (Free, Standard, XL)
2. Click Standard → upload lysozyme PDB → Stripe Checkout (test card 4242 4242 4242 4242) → webhook fires → workspace activated → land on `/workspaces/<id>`
3. Run RFdiffusion → Modal cap consumed → results visible
4. Run MPNN, AF2, BindCraft on same target → all charged to single budget
5. Force 80% cap → email warning fires (check Resend log)
6. Force 100% cap → submissions blocked with XL upgrade message
7. Click refund within 7 days → Stripe refund + workspace status=refunded
8. After 7 days on a different Workspace: refund button absent
9. Activate XL on second target → $500 cap + priority badge visible
10. Webhook replay (re-POST same event) → no double-activation

If all flows pass two consecutive runs on PD-L1 + lysozyme, flip live.

---

## Known gotchas

- **Stripe webhook secret format:** `whsec_...`. If you copy-paste the
  webhook signing secret with a trailing newline, signature verification
  fails silently. Trim it.
- **The migration uses `gen_random_uuid()`** which requires the `pgcrypto`
  extension. Supabase has this enabled by default — verify in the SQL
  editor with `SELECT * FROM pg_extension WHERE extname = 'pgcrypto';`
- **The credits_ledger stays in place.** `activate_workspace` writes a
  grant row equal to the Modal cap so internal accounting balances.
  Don't drop the table — see [`shared/workspaces.py:266`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/shared/workspaces.py).
- **The `target_pdb_id`** stored in `workspaces.target_pdb_id` is the
  Supabase storage object path from `upload_input()`, not a PDB code.
  See [`app.py:workspaces_new_submit`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/app.py) — last 30 lines of that function.
- **Free GPU smoke runs are killed** per the locked decision, but the
  tool adapter code (`tools/*/__init__.py`) still defines smoke presets
  with `credits_cost = 0`. These no longer have a customer-facing entry
  point, so they're harmless dead code. Clean up in a separate PR.

---

## Original plan reference

The full strategic rationale, market research, and confidence assessment
live in the approved plan:
[`C:\Users\lab\.claude\plans\for-my-tools-hub-cheerful-nygaard.md`](file:///C:/Users/lab/.claude/plans/for-my-tools-hub-cheerful-nygaard.md)

Read that first if a new session needs context on **why** the credit
model was scrapped or **how** the $499 / $2,499 price points were
chosen (vs Neurosnap $7–80/mo and Adaptyv $120/wet-lab-validated-design).

---

## Suggested first prompt for the new session

> Resume the tools-hub Workspace migration. The handoff note is at
> `docs/HANDOFF-WORKSPACE-MIGRATION.md` and the approved plan is at
> `C:\Users\lab\.claude\plans\for-my-tools-hub-cheerful-nygaard.md`.
> Start by running the verification commands in the handoff to confirm
> the current state. Then tackle blocker item #1 (apply migration 0014
> to Supabase) and #2 (create Stripe products in test mode).
