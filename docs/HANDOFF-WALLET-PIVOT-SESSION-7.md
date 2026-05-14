# Tools-Hub Wallet Pivot, Session 7 Handoff

**Date:** 2026-05-14
**Supersedes:** `HANDOFF-WALLET-PIVOT-SESSION-6.md`
**Authoritative plan:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md`
**Cross diff review (binding for next session):** `docs/WAVE2-REVIEW.md`

---

## TL;DR

Wave 2 dispatched 5 parallel agents (C, D, E, G, H) and landed 8 commits. Wave 3 cross diff review surfaced 2 HIGH + 6 MEDIUM findings. Wave 4 + v2 landed 6 fix commits closing all 3 HIGH and 3 of the 6 MEDIUM items. The main session committed Agent H deliverables and 3 contract reconciliation commits along the way. 17 commits total since the Session 6 docs baseline, all pushed to `origin/main`.

Stripe sandbox dashboard Passes 1 to 5 are closed. Pass 6 (E2E smoke, 16 steps) waits on a Railway preview deploy. Pass 7 (live mode + real $20 top up) waits on Pass 6.

Full repo pytest: **625 passed, 6 pre existing prometheus_client failures, 6 skipped.** Wallet specific suite: 166 of 166 green.

---

## Commits shipped this session (origin/main 1735c79..6c7e879)

| SHA | Author | Title |
|---|---|---|
| `c780edd` | Agent E | feat(jobs): wallet hold settle on completion plus mid-run monitor |
| `ecf06a0` | Agent D | feat(webhooks): rewrite stripe handler for the USD wallet |
| `76da4e1` | Agent C | feat(billing): rewrite checkout.py as wallet top up Session creator |
| `9fea8f8` | Agent G | feat(email): wire wallet senders to Resend with HTML templates |
| `20ef5c7` | Agent E | feat(app): wallet preflight decorator plus estimate + topup complete |
| `069c0fa` | Agent E | test(wallet): wallet HTTP surface plus jobs settle path coverage |
| `1e7b398` | Agent E | style(wallet): strip em dashes plus connector hyphens from prose |
| `56e4e25` | main session for H | feat(wallet-ui): wallet templates plus pricing rewrite and shared CSS |
| `2d4b05f` | Wave 3 reviewer | docs(wallet): wave 2 cross diff review |
| `0186b79` | Wave 4 agent | fix(checkout): prefer PUBLIC_BASE_URL alias over APP_BASE_URL |
| `c8c2a9c` | Wave 4 agent | style(wallet): strip em dashes from rendered title tags |
| `896bc3c` | main session for v1 | fix(app): extend /api/wallet/estimate response with partials contract keys |
| `3852ace` | Wave 4 v2 | feat(wallet): register /account/wallet flask routes for ui surface |
| `9ee3b14` | Wave 4 v2 | feat(tools): wire wallet estimate panel into tool submit forms |
| `b4ddbf6` | main session for v2 | feat(email): split overrun warning and kill into dedicated senders |
| `bb78a05` | main session | test(wallet): update mocks and fixtures to match wave 4 contracts |
| `6c7e879` | main session | fix(tools): defensive wallet field access in tool form partials |

---

## Status snapshot

| Surface | Status | Notes |
|---|---|---|
| Strategic decisions | LOCKED | Carried from Session 4 |
| Marketing site (ranomics.com `/tools/pricing`) | LIVE | Commit `73d9aed`, Vercel deployed (carried) |
| Migration `0017_wallet.sql` on prod Supabase | APPLIED | Gate A closed Session 6 (carried) |
| Wave 1 backend | PUSHED | Carried |
| Wave 2 backend (5 agents) | PUSHED | This session |
| Wave 3 cross diff review (`docs/WAVE2-REVIEW.md`) | PUSHED | This session |
| Wave 4 HIGH fixes (routes, estimate shape, tool form integration) | PUSHED | All 3 done |
| Wave 4 MEDIUM fixes | 3 of 6 done | Done: PUBLIC_BASE_URL alias, em dashes, overrun senders. Pending: Fix 7, Fix 8, Fix 9. |
| Stripe sandbox Pass 1 (archive legacy SKUs) | DONE | |
| Stripe sandbox Pass 2 (create wallet top up product) | DONE | `STRIPE_WALLET_TOPUP_PRODUCT_ID` populated in `.env` |
| Stripe sandbox Pass 3 (Stripe Tax) | DONE | Origin Canada (Ontario), automatic ON, USD/CAD tax added on top |
| Stripe sandbox Pass 4 (webhook endpoint) | DONE | Placeholder URL `https://example.com/webhooks/stripe`, 4 events subscribed, `whsec_...` captured into `.env` |
| Stripe sandbox Pass 5 (regenerate test secret key) | DONE | New `sk_test_...` in `.env` |
| Stripe sandbox Pass 6 (16 step E2E) | NOT STARTED | Needs Railway preview URL |
| Stripe live Pass 7 | NOT STARTED | After Pass 6 |
| Real $20 top up validation | NOT STARTED | After Pass 7 |

---

## What landed where (Wave 4 closing summary)

**HIGH items, all closed:**

1. **Wallet UI routes** registered in `app.py` (`3852ace`). Five routes: `/account/wallet`, `/account/wallet/topup`, `/account/wallet/checkout` (POST), `/account/wallet/transactions`, `/account/wallet/auto-reload` (POST). All `login_required`.
2. **`/api/wallet/estimate` JSON shape** extended (`896bc3c`) with 8 keys the partial JS reads: `deficit_usd`, `rounded_topup_usd`, `scaled_hard_cap_usd`, `soft_block`, `hard_block`, `self_serve_block`, `confirm_band`, `wallet_frozen`. Old keys preserved.
3. **Moment 1 partial + gate integration** into the 9 tool submit forms (`9ee3b14`). `from "wallet/_partials.html" import wallet_estimate_panel, wallet_topup_gate, wallet_partials_script`. Followed up by `6c7e879` to make the wallet field access defensive when the route passes an empty dict (broke 12 pre existing form smoke tests at first; now green).

**MEDIUM items, 3 closed:**

4. **`send_overrun_warning_email` and `send_overrun_kill_email`** added (`b4ddbf6`). Plan listed these as distinct senders at lines 1641 to 1642. Agent G originally reused `send_job_capped_email` with a `kind` kwarg, but the call sites in `shared/jobs.py` passed `user_email=` which the sender did not accept, so every overrun warning and every safety kill notice silently dropped. Now uses dedicated senders by name and resolves email via the service role client inside the sender.
5. **`PUBLIC_BASE_URL` alias** (`0186b79`) added to `billing/checkout.py:_base_url` ahead of `APP_BASE_URL` and `APP_URL`. `PUBLIC_BASE_URL` was already the incumbent across `shared/email.py`, `app.py`, and `cron/daily_digest.py`.
6. **Em dashes stripped** from three `{% block title %}` lines (`c8c2a9c`): `templates/wallet/topup.html:22`, `overview.html:18`, `transactions.html:23`.

**MEDIUM items, still pending (Session 8 work):**

- **Fix 7:** `templates/wallet/topup.html` does not branch on `topup_success` / `topup_error` / `return_tool`. After Stripe Checkout returns, the user lands on the same top up form instead of a confirmation banner. Low effort, high user impact.
- **Fix 8:** No jinja render smoke tests for `templates/wallet/{overview,topup,transactions}.html` or `templates/pricing.html`. Recommend `tests/test_wallet_templates.py`. Cheap, high value addition that would have caught Fix 2 and 4.1 contract mismatches at CI.
- **Fix 9:** Canonical env var table not yet written. Recommend appending `## Canonical env vars` to this handoff or a new `docs/WALLET-ENV-VARS.md`. Locks `PUBLIC_BASE_URL` as canonical and `SLACK_OPS_WEBHOOK_URL` as the ops channel name (Agent G coined the latter).

---

## Lessons logged from this session

1. **Agent truncation pattern.** Three of four Wave 2/3/4 agents (Agent H, Wave 4, Wave 4 v2) truncated their final report after the substantive work was done but before commit. Mitigation: tell agents to commit AFTER EACH FIX, not at the end. Wave 4 v2 ran with this instruction and lost only the in flight fix (Fix 4) instead of the whole batch. **Carry forward as a constraint in every future agent prompt.**
2. **Index lock contention from concurrent commits.** Five parallel agents trying to commit simultaneously caused one accidental cross agent bundle (`0d0c76f` swept Agent H templates into Agent C's commit). Agent C self recovered with `git reset --mixed HEAD~1` + `git commit --only`. Main session should NOT touch the index while agents are writing. **Carry forward.**
3. **Test pollution from .env loaded into os.environ.** The 4 test_checkout failures (`PUBLIC_BASE_URL` vs `APP_BASE_URL` priority) passed alone, failed in suite. Root cause: another test loads `.env` into the process env, and the fixture didn't clear `PUBLIC_BASE_URL`. Fix: `monkeypatch.delenv` defensively in env pinning fixtures.
4. **Sandbox vs classic test mode in Stripe.** The user's Stripe environment is Sandbox (the newer green-banner variant). Product, keys, and webhook configs are scoped to the sandbox account, not classic test mode. For live cutover, all Pass 1 to 5 work has to be repeated in live mode.

---

## Canonical env vars (Session 8 will formalize this into Fix 9)

Single source of truth derived from Wave 2 dispatch + WAVE2-REVIEW.md section 4.7. Update `docs/WALLET-ENV-VARS.md` when Fix 9 lands.

| Canonical name | Aliases honoured | Used by | Notes |
|---|---|---|---|
| `STRIPE_SECRET_KEY` | none | billing/checkout.py, webhooks/stripe.py | Rotated in Pass 5 |
| `STRIPE_WEBHOOK_SECRET` | none | webhooks/stripe.py | Set in Pass 4 |
| `STRIPE_WALLET_TOPUP_PRODUCT_ID` | `STRIPE_TOPUP_PRODUCT_ID` (shorthand from Wave 2 dispatch prompt) | billing/checkout.py | Captured in Pass 2 |
| `PUBLIC_BASE_URL` | `APP_BASE_URL`, `APP_URL` (legacy) | billing/checkout.py, shared/email.py, app.py, cron/daily_digest.py | Locked after Wave 4 Fix 5 |
| `RESEND_API_KEY` | none | shared/email.py | Senders skip silently if unset |
| `RESEND_FROM_TRANSACTIONAL` | `EMAIL_FROM` (legacy) | shared/email.py | |
| `SUPPORT_EMAIL` | none | shared/email.py | |
| `WALLET_MIN_TOPUP_USD` | none | billing/checkout.py, shared/wallet.py | Default 20 |
| `WALLET_MAX_TOPUP_USD` | none | billing/checkout.py | Agent C default 5000 |
| `WALLET_MARKUP` | none | shared/wallet.py | Default 1.70 |
| `WALLET_SIGNUP_CREDIT_USD` | none | shared/wallet.py, shared/email.py | Default 5 |
| `WALLET_DEFAULT_DAILY_CAP_USD` | none | shared/wallet.py, shared/email.py | Default 200 |
| `WALLET_LOW_BALANCE_THRESHOLD_USD` | none | shared/wallet.py | Default 5 |
| `WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL` | `SLACK_SALES_WEBHOOK_URL` (Wave 2 dispatch prompt shorthand) | shared/email.py:alert_sales_slack | Optional, logs only if unset |
| `SLACK_OPS_WEBHOOK_URL` | none | shared/email.py:alert_ops_slack | Optional, logs only if unset |

---

## Next session quickstart

Open Claude Code in `C:\Users\lab\Documents\Claude_projects\tools-hub`, paste this:

> Read `docs/HANDOFF-WALLET-PIVOT-SESSION-7.md` and `docs/WAVE2-REVIEW.md`. We are at all HIGH items closed, 3 of 6 MEDIUMs closed, sandbox Stripe Passes 1 to 5 done. Three things in order:
>
> 1. Close Wave 4 Fix 7 (templates/wallet/topup.html branches on topup_success / topup_error / return_tool), Fix 8 (jinja render smoke tests for wallet/topup, wallet/overview, wallet/transactions, pricing.html), and Fix 9 (formalize `docs/WALLET-ENV-VARS.md` from the table in handoff session 7). These are small and well bounded. Do them inline rather than dispatching a subagent.
> 2. Deploy Railway preview. Once the preview URL is live, edit the Stripe sandbox webhook endpoint to point at `https://<railway-preview>/webhooks/stripe` instead of the example.com placeholder. The signing secret does not change. Update `.env` and the Railway env if needed.
> 3. Run Stripe sandbox Pass 6, the 16 step E2E smoke. The full script is in the plan around line 1072 and reproduced in handoff session 6 section "Stripe dashboard work". Capture results.
>
> After Pass 6 is green, Pass 7 is live mode (repeat Passes 1 to 5 in live, then a real $20 top up by Leo).

---

## Open questions for Leo (none blocking)

Carried unchanged from Session 6:

1. Slack webhook URLs for `#sales-leads` and `#ops`. Without these, alerts log only.
2. Stripe Tax origin address for live mode (sandbox uses Canada (Ontario) per user's setup).
3. Whether `auto_reload_monthly_cap_usd` default stays at $1,000 or goes higher.
4. Whether the daily spend cap default stays at $200 or goes higher.

All four are policy levers, none block Pass 6 or 7.

---

## Critical gotchas (carried + new)

Carried from Session 6:

1. Migration is `0017`, not `0015`.
2. No dashes in customer prose. Em, en, connector hyphens all out. Identifiers, URLs, slugs may keep hyphens.
3. Ranomics marketing tool MDX titles still use em dash as a parser convention. Leave those alone.
4. `shared/workspaces.py` still present until wallet has been live for at least a week.
5. No auto mode browser writes to prod Supabase. Manual paste only in SQL editor.
6. Supabase SQL editor batches are transactional. Paste full migrations in one go.
7. `FILTER` attaches only to aggregates. `ABS(SUM(x)) FILTER (...)` is invalid.
8. `INSERT ... UNION ALL` with enum literals needs explicit casts.
9. Modal/Windows: prepend `/c/Program Files/nodejs` to `PATH` for `npm`; set `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` for `modal deploy`.

New from Session 7:

10. **Always tell agents to commit per fix, never at the end.** Truncation is a recurring failure mode (3 of 4 multi step agents this session). Per fix commits make truncation cost bounded.
11. **Main session should never `git add` while agents are writing.** Race conditions are real. Wait for completion notifications, then commit on the agent's behalf if the agent truncated before its own commit.
12. **`.env` loaded into `os.environ` leaks into test runs.** Env pinning fixtures must `monkeypatch.delenv` aliases they want to suppress, not just `setenv` the one they want active.
13. **Stripe sandbox and classic test mode are separate environments.** A `sk_test_...` from one is not valid in the other. Account ids differ. For live cutover, ALL configuration (product, webhook, tax) gets recreated in live mode.

---

## Outcome

- Wave 2 backend complete and pushed.
- All 3 HIGH cross diff findings closed.
- 3 of 6 MEDIUM findings closed.
- Stripe sandbox configured through Pass 5.
- 625 tests passing, only 6 pre existing prometheus failures remain.
- Sticky save at `origin/main` HEAD `6c7e879`.
- Ready for Session 8 to finish MEDIUMs, deploy Railway preview, and run Pass 6.
