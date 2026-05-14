# Tools-Hub Wallet Pivot, Session 8 Handoff

**Date:** 2026-05-14
**Supersedes:** `HANDOFF-WALLET-PIVOT-SESSION-7.md`
**Authoritative plan:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md`
**Pass 6 results doc:** `docs/PASS-6-SANDBOX-RESULTS.md`
**Cross diff review (binding):** `docs/WAVE2-REVIEW.md`

---

## TL;DR

Session 8 closed Wave 4 (Fix 7, Fix 8, Fix 9), deployed the Railway preview, pointed the Stripe sandbox webhook at it, and started Stripe sandbox Pass 6. Pass 6 immediately surfaced three real production blockers that the Wave 1 to Wave 4 work missed:

1. The `credit_wallet` SQL RPC was never written. shared/wallet.py called it for signup credit, top up, auto reload, dispute refund. Every wallet credit operation silently failed.
2. The `release_hold` SQL RPC was never written either. Holds for failed or cancelled jobs would have leaked.
3. The `try_hold_for_job` SQL RPC shipped with 4 parameters but shared/wallet.py has been calling it with 5 (adds `p_hard_cap_usd`). Every tool submit was bouncing to the top up gate even with ample balance.

Plus one production data integrity bug surfaced: every early return path in `tool_submit` (form validation, missing PDB, chain mismatch, etc.) leaked the wallet hold. The decorator placed the hold but the early returns never released it.

Pass 6 Steps 1 to 5 are GREEN. Step 6 is in progress.

**HEAD on `origin/main`:** `11b19c8` (8 new commits on top of the Session 7 baseline `ccc499c`).
**Full repo pytest:** 646 passed, 6 skipped, 0 failed (the previously-flaky prometheus tests came back green too, +16 new tests from Fix 8 + the one regression test in Step 1 fix).
**Wallet test suite:** 81 of 81 green at last spot check.

---

## Commits shipped this session (origin/main ccc499c..11b19c8)

| SHA | Title |
|---|---|
| `0b004ee` | feat(wallet): branch topup.html on topup_success / topup_error / return_tool |
| `a54f86f` | test(wallet): jinja render smoke tests + harden topup.html context defaults |
| `140143a` | docs(wallet): formalize WALLET-ENV-VARS.md as the canonical env table |
| `bf84afb` | fix(wallet): add missing credit_wallet + release_hold RPCs (0018 migration) |
| `9d9d383` | docs(wallet): Pass 6 sandbox results scaffold + Step 1 findings |
| `fbc1234` | fix(wallet): add p_hard_cap_usd param to try_hold_for_job RPC (0019) |
| `11b19c8` | fix(wallet): release hold on every early return path from tool_submit |

---

## Status snapshot

| Surface | Status | Notes |
|---|---|---|
| Strategic decisions | LOCKED | Carried from Session 4 |
| Marketing site (ranomics.com `/tools/pricing`) | LIVE | Carried |
| Migration 0017 on prod Supabase | APPLIED | Wave 1, Session 6 (carried) |
| Migration 0018 on prod Supabase | APPLIED | Session 8, Pass 6 unblock |
| Migration 0019 on prod Supabase | APPLIED | Session 8, Pass 6 unblock |
| Wave 1 backend | PUSHED | Carried |
| Wave 2 backend (5 agents) | PUSHED | Carried |
| Wave 3 cross diff review | PUSHED | Carried |
| Wave 4 HIGH fixes | PUSHED | Carried |
| Wave 4 MEDIUM fixes (Fix 7, 8, 9) | PUSHED | Session 8, all done |
| Hold-leak fix in `tool_submit` | PUSHED | Session 8, commit `11b19c8` |
| Stripe sandbox Pass 1 to 5 | DONE | Carried |
| Stripe sandbox Pass 6 (16 step E2E) | IN PROGRESS | Steps 1 to 5 GREEN. Step 6 not yet completed cleanly. |
| Railway preview deploy | LIVE | URL `https://web-preview-90b3.up.railway.app`, separate `preview` Railway env |
| Stripe sandbox webhook | POINTED AT PREVIEW | Endpoint `we_1TX2y1HK3YN42tFlo7xswCTl`, URL flipped from example.com placeholder to the preview, signing secret unchanged |
| Stripe live Pass 7 | NOT STARTED | After Pass 6 |
| Real $20 top up validation | NOT STARTED | After Pass 7 |

---

## What landed where

### Wave 4 closing (Fix 7, 8, 9)

**Fix 7 (`0b004ee`)**: `templates/wallet/topup.html` now branches on `topup_success` / `topup_error` / `return_tool`. The post Stripe Checkout redirect at `/account/topup-complete` shows the success panel with the charged amount, new balance, receipt id, and a "Return to <tool>" CTA when `return_tool_url` is present. Error paths show a banner above the existing form so the user can retry. CSS additions in `static/wallet.css` (`.wallet-topup-success` and `.wallet-topup-error`).

**Fix 8 (`a54f86f`)**: 15 jinja render smoke tests in `tests/test_wallet_templates.py` covering `wallet/overview.html`, `wallet/topup.html` (4 variants: standalone, gate, success, error), `wallet/transactions.html` (with rows, kind filter, empty, paginated), and `pricing.html` (logged in + anonymous). The tests caught a real production bug while being written: `wallet/topup.html` assumed every caller passed `deficit_usd` and `min_topup_usd` in the context, but the standalone `/account/wallet/topup` route and both error paths of `/account/topup-complete` pass neither. With Jinja default Undefined, the `is not none` test returned True on Undefined and the follow-up `|float` raised. Fix: `default(none, true)` filter on both vars.

**Fix 9 (`140143a`)**: `docs/WALLET-ENV-VARS.md` formalized as the canonical env var table. Five sections (quick reference / detailed table / aliases policy / hardcoded constants flagged as NOT env vars / Railway checklist + sandbox vs live separation). Caught two errors in the Session 7 working table: `WALLET_MARKUP` was listed as an env var but is hardcoded in `shared/wallet.py:73` and `shared/wallet_estimates.py:44`; `WALLET_LOW_BALANCE_THRESHOLD_USD` was listed but does not exist anywhere in the code.

### Pass 6 blockers (0018 + 0019 migrations + decorator fix)

**0018 (`bf84afb`)**: New `supabase/migrations/0018_wallet_rpcs.sql` plus an overview.html defensive guard plus a regression test. Two SQL functions:

* `credit_wallet(p_user_id, p_amount_usd, p_kind, p_stripe_event_id, p_stripe_payment_intent_id)` — atomic credit, locks the wallet row, recomputes balance from the ledger, idempotent via `stripe_event_id`. Validates `p_kind` is a credit-side enum value (signup_credit, topup, auto_reload, promo, adjustment, hold_release).
* `release_hold(p_hold_tx_id, p_reason)` — full release of a hold, links via `parent_tx_id`, idempotent (no-op if a row with `parent_tx_id = hold_tx_id` already exists).
* `GRANT EXECUTE ... TO service_role` on both.
* Backfill: every `user_wallets` row with `balance_usd = 0` and no `signup_credit` ledger entry gets the $5 retroactively (signed-up-between-0017-and-0018 users).

Bonus: `templates/wallet/overview.html:107-118` was reading `wallet.signup_credit_used_usd` directly, but that column does not exist on `user_wallets`. Defensive guard added using same `default(none, true)` pattern as Fix 8.

**0019 (`fbc1234`)**: New `supabase/migrations/0019_try_hold_for_job_hardcap.sql`. Drops the 4-param `try_hold_for_job` from 0017 and recreates it with a 5th `p_hard_cap_usd` parameter (default NULL). Enforces the cap at SQL level as defense in depth (return NULL if `amount > cap` when cap is supplied). `wallet_preflight` already checks the cap before calling `reserve_hold`, but the SQL guard means a params change between preflight and hold cannot bypass the ceiling.

**Hold leak fix (`11b19c8`)**: The `requires_wallet` decorator at `app.py:478` placed a hold then ran `tool_submit`. `tool_submit` has 8+ early-return paths (form validation, unknown preset, missing PDB, PDB inspection fail, chain mismatch, hotspot out of range, workspace gate, create_job returning None) and only the create_job-failed branch called `wallet_release_hold` explicitly. Every other early return leaked the hold. Fix is at the decorator level:

* Decorator sets `g.wallet_hold_consumed = False` after placing the hold.
* `tool_submit` sets `g.wallet_hold_consumed = True` only AFTER `create_job` returns a real row (the point at which the job owns the hold via `_settle_wallet_hold_for_completed_job`).
* Decorator's post-view block checks the flag and auto-releases if False.

`release_hold` is idempotent, so the explicit per-site releases that still exist further down `tool_submit` (storage upload error, Modal submit error) become belt-and-suspenders.

### Pass 6 results in flight

`docs/PASS-6-SANDBOX-RESULTS.md` captures Steps 1 to 5 GREEN, plus the bugs surfaced.

**Step 1 GREEN**: signup credit. User signed up as `leowan7@gmail.com` (`user_id = 03e51184-4d04-4acd-ab22-0cbd7fa08c77`). After 0018 backfill, `user_wallets.balance_usd = 5.00` and `wallet_transactions` has a `kind=signup_credit` row with `stripe_event_id = signup_credit:03e51184-...`.

**Step 2 GREEN**: `/account/wallet` renders cleanly with $5.00 balance, $0 spent, daily cap $200, auto-reload off. Side findings: the "Signup credit used" stat tile reads "$5.00 / trial credit available" because the schema lacks the column the template was reading; label says "used" but body says "available"; minor inconsistency, not blocking.

**Steps 3 to 5 GREEN**: $20 top-up via Stripe Checkout with test card `4242 4242 4242 4242`. Tax line $0.00 (correct: Origin Ontario, US-domestic customer, no nexus). Fix 7 success page rendered cleanly with "Top up complete" + new balance $25.00 + receipt id + "View wallet" / "Browse tools" CTAs. Backend: `balance_usd = 25.00`, ledger has the `kind=topup` row with `amount_usd=20.00`, `stripe_payment_intent_id`, `stripe_event_id`. Webhook delivered both `checkout.session.completed` and `payment_intent.succeeded`; the handler correctly used only the checkout.session.completed event for the wallet credit.

**Step 6 IN PROGRESS**: First submit at 20:58 hit the missing `try_hold_for_job` 5-param signature (PGRST202) — fixed by 0019. Second submit at 21:04 placed a hold successfully but `tool_submit` hit an early-return path (likely the "Upload a target PDB file" check at `app.py:2447`), bouncing the user back to the form. The hold leaked (tx 54, $0.0241) until I manually released it via direct SQL editor RPC call. Hold-leak fix shipped (`11b19c8`). Step 6 needs a clean retry with PDB attached.

### Railway preview infrastructure

* New Railway environment `preview` (separate from production), linked via `railway link --project 607bc08f-... --environment preview --service web`.
* 15 env vars seeded from local `.env`: SUPABASE_*, STRIPE_*, WALLET_*, PUBLIC_BASE_URL, RESEND_FROM_TRANSACTIONAL, SUPPORT_EMAIL, SESSION_SECRET_KEY, GPU_ENVIRONMENT. Excluded `WALLET_MARKUP` and `WALLET_LOW_BALANCE_THRESHOLD_USD` (per Fix 9 audit, these are not env vars).
* `PUBLIC_BASE_URL` set to `https://web-preview-90b3.up.railway.app` (the Railway-generated domain).
* 9 `FLAG_TOOL_*=on` vars copied from production (without these, `/tools` showed an empty list).
* 4 additional production vars copied: `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` (required for tool execution on Modal GPU pods), `RESEND_API_KEY` (so wallet emails actually deliver), `STAFF_NOTIFY_EMAIL`.
* Stripe sandbox webhook endpoint `we_1TX2y1HK3YN42tFlo7xswCTl` URL flipped from `https://example.com/webhooks/stripe` to `https://web-preview-90b3.up.railway.app/webhooks/stripe`. Signing secret unchanged.

---

## Lessons logged from this session

1. **The wallet pivot Wave 1 work shipped with broken SQL surface coverage.** `credit_wallet` and `release_hold` were called by Python but never defined in any migration. `try_hold_for_job` was defined but with a signature one parameter short of what Python called it with. **Carry-forward**: any future "X is done" status should be cross-verified against an actual end-to-end smoke run BEFORE the next session declares the work shipped. Pytest passes; production calls don't.
2. **Decorator-managed lifecycles leak when the wrapped view has many early returns.** The `requires_wallet` decorator placed a hold and only released on exception. Form validation, missing PDB, missing chain, etc. all returned non-exception responses that bypassed the release. **Carry forward**: any resource-acquiring decorator (wallet hold, idempotency lock, rate-limit token, file lock) needs an explicit "consumed" flag set by the wrapped view, with the decorator releasing in a finally-style block when the flag is False. The "released on exception only" pattern is fundamentally incomplete.
3. **Stripe sandbox webhook reaches preview correctly first try.** Setup + URL update + signature verification + Supabase event write all worked end to end with no debugging. The sandbox Stripe CLI `stripe trigger checkout.session.completed` is a clean validation tool.
4. **Don't trust the .env file to be the canonical config source.** The local `.env` had `WALLET_MARKUP` and `WALLET_LOW_BALANCE_THRESHOLD_USD` set, but neither is read by any code path. Real Modal tokens and Resend API keys were missing from `.env`. The Railway production env had additional vars that the local `.env` did not. **Carry forward**: when seeding a new Railway environment, diff against the prod env via `railway variables --kv` and add the production-only critical vars (Modal, Resend) explicitly.

---

## Critical gotchas (carried + new)

Carried from Session 7:

1. Migrations 0017, 0018, 0019.
2. No dashes in customer prose. Em, en, connector hyphens all out. Identifiers, URLs, slugs may keep hyphens.
3. Ranomics marketing tool MDX titles still use em dash as a parser convention. Leave those alone.
4. `shared/workspaces.py` still present until wallet has been live for at least a week.
5. No auto mode browser writes to prod Supabase. Manual paste only in SQL editor.
6. Supabase SQL editor batches are transactional. Paste full migrations in one go.
7. `FILTER` attaches only to aggregates. `ABS(SUM(x)) FILTER (...)` is invalid.
8. `INSERT ... UNION ALL` with enum literals needs explicit casts.
9. Modal/Windows: prepend `/c/Program Files/nodejs` to `PATH` for `npm`; set `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` for `modal deploy`.
10. Always tell agents to commit per fix, never at the end. Truncation is a recurring failure mode.
11. Main session should never `git add` while agents are writing. Race conditions are real.
12. `.env` loaded into `os.environ` leaks into test runs. Env pinning fixtures must `monkeypatch.delenv` aliases they want to suppress.
13. Stripe sandbox and classic test mode are separate environments.

New from Session 8:

14. **Wallet SQL surface is now in 0017 + 0018 + 0019. Future sessions should apply all three.** If you spin up a new Supabase project, copy all three SQL files into the SQL editor in order.
15. **`try_hold_for_job` requires 5 parameters including `p_hard_cap_usd`.** The 0019 migration enforces this. If you see a PGRST202 saying the function was not found, the migration is not applied or someone reverted to the 4-param signature.
16. **Don't seed a Railway preview env from `.env` alone.** Always diff against prod via `railway variables --kv` for both environments and copy the critical production-only vars (Modal tokens, Resend API key, staff notify email, FLAG_TOOL_*).
17. **The `requires_wallet` decorator now expects the wrapped view to set `g.wallet_hold_consumed = True` once `create_job` has succeeded.** If you add a new wallet-gated view (any handler that takes a hold), it must set this flag after writing the job row, or the hold will be auto-released by the decorator on return.
18. **The Railway preview URL is `https://web-preview-90b3.up.railway.app`** and the Stripe sandbox webhook endpoint `we_1TX2y1HK3YN42tFlo7xswCTl` is pointed at it. When live cutover happens, point the LIVE Stripe webhook at the live tools.ranomics.com URL and leave the sandbox one on the preview.

---

## Open marketing-surface gaps (separate session)

Surfaced during Pass 6 but out of scope. A self-contained prompt for a new session was authored mid-session 8 and pasted into the conversation (not committed). Summary:

* Homepage hero at `/` still pitches "$499 per target Workspace" pre-pivot SKU model.
* Top nav has "+ Activate target" CTA pointing at dead $499 flow, no "Wallet" link.
* Signup banner says "Account created with 10 free credits" instead of "$5 in compute credit".
* Tool form preset dropdowns still labeled in CREDITS (e.g. `smoke: 0 credits`, `standalone: 1 credit`) even though the wallet estimate panel below renders USD.
* Overview "Signup credit used" tile has label-vs-body wording mismatch when the signup_credit_used_usd column is absent.

These do NOT block the wallet plumbing. The wallet routes work; the marketing copy around them is just stale.

---

## Next session quickstart

Open Claude Code in `C:\Users\lab\Documents\Claude_projects\tools-hub`, paste this:

> Read `docs/HANDOFF-WALLET-PIVOT-SESSION-8.md` and `docs/PASS-6-SANDBOX-RESULTS.md`. We are at:
> - Wave 4 closed (Fix 7, 8, 9 shipped)
> - Migrations 0018 + 0019 applied to prod Supabase
> - Hold-leak decorator fix shipped
> - Stripe sandbox Pass 6 Steps 1 to 5 GREEN
> - Step 6 in progress: hold mechanism works, user needs to do a clean MPNN submit with 1UBQ attached. New deploy at `https://web-preview-90b3.up.railway.app` has the auto-release decorator fix live.
>
> Three things in order:
>
> 1. Drive Pass 6 Steps 6 to 16. User submits MPNN with 1UBQ attached, you verify the hold → settle ledger flow via DB queries against Supabase. Then steps 8 (force-fail), 9 (concurrent insufficient balance), 10 to 13 (auto-reload + rate limit + monthly cap), 14 (declined card), 15 (dispute freeze), 16 (Stripe dashboard webhook deliveries clean).
> 2. Update `docs/PASS-6-SANDBOX-RESULTS.md` as each step lands. Commit when Pass 6 is fully GREEN.
> 3. If time permits, plan Pass 7 (live mode). Repeat Stripe sandbox Passes 1 to 5 in live mode (separate Stripe account, separate product, separate webhook, separate keys), then real $20 top up on Leo's personal card. Live webhook URL is `https://tools.ranomics.com/webhooks/stripe`, not the preview.
>
> The marketing-surface rewrite (homepage hero, nav, signup banner, credits→USD on forms) is parked. Don't start that here.

---

## Open questions for Leo (none blocking)

Carried from Session 6:

1. Slack webhook URLs for `#sales-leads` and `#ops`. Without these, alerts log only.
2. Stripe Tax origin address for live mode (sandbox uses Canada (Ontario)).
3. Whether `auto_reload_monthly_cap_usd` default stays at $1,000 or goes higher.
4. Whether the daily spend cap default stays at $200 or goes higher.

New from Session 8:

5. When to clean up the dead workspace SKU code path (`shared/workspaces.py`, `+ Activate target` nav, $499 hero copy). Suggested: after Pass 7 + real $20 live top up validates the wallet end-to-end. Then the workspace path can be deleted in its own commit with a 1-week observation window first.

---

## Outcome

- 8 commits on origin/main beyond the Session 7 baseline.
- Three Wave 4 MEDIUM fixes closed.
- Two missing SQL migrations written, applied to prod, and committed.
- One production data integrity bug (hold leak on early returns) found and fixed at the decorator level.
- Stripe sandbox webhook pointed at Railway preview, end to end signature verification confirmed.
- Pass 6 Steps 1 to 5 GREEN.
- Pass 6 Step 6 hold mechanism validated; needs one clean retry with PDB attached to fully close.
- Marketing-surface rewrite prompt drafted for a parallel session.
- Sticky save at `origin/main` HEAD `11b19c8`.
- Ready for Session 9 to finish Pass 6, then move to Pass 7.
