# Session Handoff — Workspace Migration (Session 3)

**Created:** 2026-05-13
**Branch:** `main` (6 new commits, ahead of `origin/main` by 6, **not pushed yet**)
**Previous handoff:** [`docs/HANDOFF-WORKSPACE-MIGRATION-SESSION-2.md`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/docs/HANDOFF-WORKSPACE-MIGRATION-SESSION-2.md)
**Original plan:** [`C:\Users\lab\.claude\plans\for-my-tools-hub-cheerful-nygaard.md`](file:///C:/Users/lab/.claude/plans/for-my-tools-hub-cheerful-nygaard.md)

Continues from session 2. This note covers (a) the atomic-commit split
and (b) the dashboard-blocker walkthrough state.

---

## What landed this session

### 6 atomic commits on `main` (local, unpushed)

```
19c949e docs: Workspace pivot product plan + session handoffs
a309a35 feat(routes): Workspace UI + tool submit pre-flight gate
e27d3c9 feat(jobs): charge active Workspace cap on tool completion + 80% warning email
634e572 feat(email): workspace 80% cap warning + 100% cap exhausted senders
b2a1acb feat(billing): Stripe checkout + activation webhook for Workspace SKUs
36558c7 feat(workspaces): add data model + lifecycle for per-target SaaS pivot
```

| Commit | Files | Purpose |
|---|---|---|
| `36558c7` | migration 0014, `shared/workspaces.py`, `billing/tiers.py`, `tests/test_workspaces.py` | Per-target Workspace data model + lifecycle |
| `b2a1acb` | `billing/checkout.py`, `webhooks/stripe.py` | Stripe one-time checkout + activation webhook |
| `634e572` | `shared/email.py` | 80% warning + 100% exhausted Resend senders |
| `e27d3c9` | `shared/jobs.py`, `tests/test_workspace_completion.py` | Item #6 — charge Workspace cap on tool completion |
| `a309a35` | `app.py`, `templates/workspaces/*`, `templates/pricing.html`, `templates/index.html`, `templates/account.html`, `templates/_header.html`, `templates/scout/feasibility.html`, `static/style.css`, `templates/tools/_prefill.html`, 9 tool form templates, `tests/test_workspace_route_gating.py` | Workspace UI + Item #7 tool route pre-flight gate |
| `19c949e` | `docs/PRODUCT-PLAN.md`, `docs/HANDOFF-WORKSPACE-MIGRATION.md`, `docs/HANDOFF-WORKSPACE-MIGRATION-SESSION-2.md` | Pivot rationale + session 1/2 handoffs |

Working tree clean except `.claude/` and `.deploy-logs/` (local-only,
appropriately ignored). **Tests:** 361 passed, 6 skipped on the final
commit — same as session 2 (no regressions during the split).

### Dashboard blocker progress

| Blocker | Status | Notes |
|---|---|---|
| **#1** Migration 0014 applied | DONE (session 2) | 9/9 verification PASS |
| **#2** Stripe test-mode products created | DONE (session 2) | Price IDs in `.env` |
| **#3** Archive legacy Stripe products | NOT STARTED | Test + live mode, plus remove `STRIPE_PRICE_SCOUT_PRO` / `_LAB` / `_LAB_PLUS` from `.env` and Railway |
| **#4** Stripe Tax enabled | DONE (test mode) — **live mode unconfirmed** | Toggle at https://dashboard.stripe.com/tax for both test AND live modes; live blocks production checkouts |
| **#5** `STRIPE_WEBHOOK_SECRET` in Railway prod | IN PROGRESS | User has a `whsec_...` from Stripe but flagged it as the "tools.ranomics GPU webhook" — **needs verification it's the right webhook** (endpoint URL must end `/webhooks/stripe`, NOT `/webhooks/modal/...`) before pasting into Railway |
| **#11** Live-mode Stripe products | BLOCKED on Phase-4 validation | `price_1...` IDs differ between test and live mode — capturing test IDs does NOT carry over |
| **#12** Regenerate local `sk_test_...` | NOT STARTED | https://dashboard.stripe.com/test/apikeys → Roll → paste into `.env` |

---

## Open work, ordered

### A. Finish blocker #5 (next session resumes here)
1. Confirm the `whsec_...` user copied is from the **Stripe** webhook
   pointing at `https://tools.ranomics.com/webhooks/stripe` (not
   Modal). Stripe dashboard → Webhooks → click the endpoint → check
   the URL.
2. Railway dashboard navigation (the user got stuck here):
   - https://railway.app → tools-hub project
   - Click the web service tile (not the Postgres tile)
   - Tabs: Deployments / **Variables** / Settings / Logs / Metrics
   - Search for `STRIPE_WEBHOOK_SECRET`. If exists, edit; if not, **+ New Variable**.
   - Paste the `whsec_...` value, no trailing newline.
   - Railway auto-redeploys; watch Deployments tab go green.
3. Verify: Stripe → Webhooks → endpoint → **Send test webhook**
   (`checkout.session.completed`) → Railway Logs should show 2xx.

### B. Blocker #4 — confirm live mode also enabled
The session toggled test mode. Repeat at
https://dashboard.stripe.com/tax with the top-right mode switch on
**Live**. Without this, production checkouts will 400 once live
products are created.

### C. Blocker #12 — regenerate local Stripe test secret (5 min)
- https://dashboard.stripe.com/test/apikeys → standard secret key →
  **Roll** with 1 hour grace period → paste new `sk_test_...` over the
  existing line in `.env`.

### D. Blocker #3 — archive legacy Stripe products (cleanup)
- Test + live modes: archive Scout Pro, Lab, Lab+, credit top-ups (do
  NOT delete — archive preserves historical orders).
- Remove `STRIPE_PRICE_SCOUT_PRO` / `_LAB` / `_LAB_PLUS` /
  `_CREDITS_*` from `.env` AND Railway prod.

### E. Phase 4 E2E validation (10 flows, two consecutive clean runs on PD-L1 + lysozyme)
Full checklist at the bottom of
[`HANDOFF-WORKSPACE-MIGRATION.md`](file:///C:/Users/lab/Documents/Claude_projects/tools-hub/docs/HANDOFF-WORKSPACE-MIGRATION.md)
§"Phase 4 validation gates". Step 1 is browseable as soon as #5 + #4
(live) + #12 are done.

### F. Blocker #11 — only after Phase 4 passes
Create live-mode Stripe products, capture live `price_1...` IDs, set
`STRIPE_PRICE_WORKSPACE_STANDARD` / `_XL` in Railway prod env.

### G. Push commits when ready
The 6 commits are local-only. Once you're confident in the pivot
(after blocker #5 + a checkout smoke test), `git push origin main`.
Vercel/Railway will auto-deploy.

### H. Code follow-ups (not launch-blocking)
- Item #8 — `expire_workspaces` daily cron in Railway
- Item #9 — sample reports on homepage (chicken-and-egg with Phase 4)
- Item #10 — pre-sell to 3-5 Ranomics CRO contacts at $299
- Scout handoff route gap — `/tools/<tool>?handoff=<id>` currently
  bypasses Workspace gate (falls to legacy credits)
- `WORKSPACE_REQUIRED=1` env-var flag to flip legacy fallback into
  hard reject

---

## Suggested first prompt for next session

> Resume the tools-hub Workspace migration. Session-3 handoff is at
> `docs/HANDOFF-WORKSPACE-MIGRATION-SESSION-3.md`. Six atomic commits
> are on local `main` (unpushed). All Wave-2 code is shipped + 361/6
> tests green.
>
> Blocker #5 (Railway `STRIPE_WEBHOOK_SECRET`) is the next gate. The
> `whsec_...` was identified in the previous session but flagged as
> potentially the wrong webhook ("tools.ranomics GPU webhook" naming
> suggested it might be Modal, not Stripe). Walk me through:
> (1) confirming it's the Stripe webhook (endpoint URL ends
> `/webhooks/stripe`),
> (2) pasting it into Railway prod,
> (3) sending a Stripe test webhook to verify 2xx.
>
> Then knock out blockers #4 (live), #12, and #3 in that order, and
> we'll be unblocked for Phase-4 E2E validation.
