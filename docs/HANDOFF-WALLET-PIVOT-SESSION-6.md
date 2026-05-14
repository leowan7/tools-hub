# Tools-Hub Wallet Pivot, Session 6 Handoff

**Date:** 2026-05-14
**Supersedes:** `HANDOFF-WALLET-PIVOT-SESSION-5.md` (Session 5 shipped Wave 1 backend on local main; Session 6 closed Gate A on prod and is ready to dispatch Wave 2)
**Authoritative plan:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md`

---

## TL;DR

**Gate A is closed.** Migration `0017_wallet.sql` is live on production Supabase (`wjlhbxfnihboqebdvnns`) and `test_0017_wallet.sql` ran clean (12 NOTICE lines, BEGIN..ROLLBACK left no residue). Wave 1 backend (8 commits) is pushed to `origin/main`. Two source-file bugs were fixed during the run and remain uncommitted on the working tree. The pre-existing dirty state in `app.py` / `shared/auth.py` / `templates/login.html` flagged in Session 5 has been resolved (no longer in `git status`).

The next session opens with Wave 2 fully unblocked. Five parallel agents can dispatch in a single message and run to completion in roughly the time the user takes to do the Stripe dashboard passes in another tab.

---

## Status snapshot

| Surface | Status | Notes |
|---|---|---|
| Strategic decisions | LOCKED | See decision table in Session 4 handoff |
| Marketing site (ranomics.com `/tools/pricing` plus copy) | LIVE on production | commit `73d9aed` on `ranomics-website-2026`, Vercel deployed |
| Tools-hub: `0017_wallet.sql` on prod Supabase | APPLIED 2026-05-14 | Via SQL editor copy-paste against `wjlhbxfnihboqebdvnns` |
| Tools-hub: `test_0017_wallet.sql` against prod | RAN CLEAN 2026-05-14 | 12 NOTICE lines fired, rolled back |
| Tools-hub: Wave 1 commits pushed | YES | `origin/main` includes all 8 wallet commits |
| Tools-hub: two source fixes from Gate A run | UNCOMMITTED | See "Uncommitted fixes" section |
| Tools-hub: `billing/checkout.py` rewrite | NOT STARTED | Wave 2 Agent C |
| Tools-hub: `webhooks/stripe.py` handlers | NOT STARTED | Wave 2 Agent D |
| Tools-hub: `app.py` decorators + endpoints + `shared/jobs.py` settle path | NOT STARTED | Wave 2 Agent E |
| Tools-hub: 13 real email senders + templates | NOT STARTED | Wave 2 Agent G |
| Tools-hub: wallet UI + `templates/pricing.html` | NOT STARTED | Wave 2 Agent H |
| Stripe dashboard (7 passes, test then live) | NOT STARTED | External, run in parallel with Wave 2 |

---

## What Session 6 did

1. Drove the user's Chrome session into the Supabase SQL editor (auto-mode classifier blocked the browser write path, switched to manual copy-paste workflow).
2. User pasted `0017_wallet.sql`. First paste hit `42809: FILTER specified, but abs is not an aggregate function` in the `wallet_30d_spend` view. The whole batch rolled back. Diagnosed: `FILTER` was attached to the wrapping `ABS()` instead of the inner `SUM()`. Fixed in source. Second paste applied cleanly.
3. User pasted `test_0017_wallet.sql`. First paste hit `42804: column "kind" is of type wallet_tx_kind but expression is of type text`. Diagnosed: the seed `INSERT INTO ... UNION ALL` resolves all three branches as `text` before matching to the target column, unlike the migration's single-SELECT inserts. Added explicit `::public.wallet_tx_kind` casts. Re-paste passed through test 11.
4. Test 12 failed with `1 wallet(s) drifted from ledger sum`. Diagnosed: the invariant query summed all `wallet_transactions.amount_usd` per user, but `absorbed_variance` rows record cost the company eats when a job's actual exceeds what the wallet can cover. Those rows never move user balance by design (see test 9: hold 1.00, actual 100.00, absorbed_variance -99.00, balance stays at 3.00). Added `FILTER (WHERE kind <> 'absorbed_variance')` to both the SELECT list and the HAVING clause. Re-paste was clean.
5. Wrote this handoff.

---

## Uncommitted fixes (Wave 1 hardening)

`git status --short` shows two modified files:

```
 M supabase/migrations/0017_wallet.sql
 M tests/sql/test_0017_wallet.sql
```

These are real defects in the Wave 1 commits that the live SQL run surfaced. The fixes are small and self-contained. Recommended disposition: one follow-up commit, not amends (the Wave 1 commits are already pushed; amending would force-push).

**Suggested commit message:**

```
fix(wallet): correct FILTER scope + test 12 invariant for absorbed_variance

- supabase/migrations/0017_wallet.sql: wallet_30d_spend view had
  ABS(SUM(x)) FILTER (...) which is invalid. FILTER attaches to the
  aggregate, not its wrapper. Moved to ABS(SUM(x) FILTER (...)).
  Surfaced when applying 0017 against prod Supabase.

- tests/sql/test_0017_wallet.sql: seed inserts cast 'signup_credit'
  to public.wallet_tx_kind explicitly; the UNION ALL form does not
  inherit the target column type the way single-SELECT inserts do.

- tests/sql/test_0017_wallet.sql: test 12 ledger sum invariant now
  filters out absorbed_variance rows. By design those rows track
  company-eaten cost and never move user balance (see test 9), so
  including them in the sum-equals-balance check is incorrect.

Gate A closed against prod 2026-05-14 with these fixes in place.
```

---

## Pre-Wave-2 checklist (sequential, blocking)

In this order:

1. **Commit the two source fixes** with the suggested message above.
2. **Push** to `origin/main`.
3. **Decide on housekeeping** for the three untracked handoff docs (`docs/HANDOFF-WALLET-PIVOT-SESSION-{3,4,5}.md`) and this Session 6 doc. They live in `git status` as untracked. Commit them in a separate `docs:` commit before Wave 2 dispatch, or skip if you want a cleaner history at push time.

That's it. There is no other prerequisite. Stripe dashboard work is parallel to agents, not before them.

---

## Wave 2 parallel dispatch plan

Five agents, one message, all `general-purpose` subagents in `tools-hub`. Each touches a strictly-bounded file set so they never collide:

| Agent | Files (touch) | Files (read-only references) | Tests to add |
|---|---|---|---|
| **C — Stripe Checkout** | `billing/checkout.py` (rewrite) | `shared/wallet.py` (credit_wallet contract), env vars | `tests/test_checkout.py` |
| **D — Stripe Webhooks** | `webhooks/stripe.py` (rewrite), idempotency table SQL if needed | `shared/wallet.py` (credit_wallet contract) | `tests/test_stripe_webhooks.py` |
| **E — App wiring + jobs settle** | `app.py` (requires_wallet decorator, `/api/wallet/estimate`, `/account/topup-complete`), `shared/jobs.py` (settle on completion + failure + 15-min monitor) | `shared/wallet.py`, `shared/wallet_estimates.py` | `tests/test_wallet_api.py`, extend `tests/test_jobs.py` |
| **G — Real email senders** | `shared/email.py` (replace 14 stubs with Resend calls), `templates/email/*.html` (13 new templates) | env: `RESEND_API_KEY`, `RESEND_FROM_*` | `tests/test_email_real.py` |
| **H — Wallet UI** | `templates/wallet/*.html` (new), `templates/pricing.html` (rewrite), "Top up and run" partial on tool submit forms | None | Snapshot tests if any, else visual review |

**Shared seams that decouple the agents:**

- All Stripe identifiers (product, webhook secret, etc.) flow through env vars. No agent waits on the Stripe dashboard work.
- SQL RPC names are canonical and already shipped: `credit_wallet`, `try_hold_for_job`, `settle_hold`, `release_hold`. Agents call by name, no schema decisions.
- Email senders are imported from `shared/email`. Agent G replaces stub bodies while keeping signatures stable, so Agents C/D/E import the same names and never see the swap.

**Forbidden in every Wave 2 prompt:**

- No pushes (atomic local commits only).
- No edits to files outside the agent's allocated list.
- No dashes (em, en, connector hyphens) in customer-facing prose, error messages, log strings, or email templates. Identifiers and slugs may keep hyphens.
- Migration is `0017_wallet.sql` (not `0015`).

**Dispatch prompt template** (one of five, fill the per-agent fields):

```
You are Wave 2 Agent <ID> for the tools-hub USD wallet pivot.

Working directory: C:\Users\lab\Documents\Claude_projects\tools-hub
Authoritative plan: C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md
Latest handoff: docs/HANDOFF-WALLET-PIVOT-SESSION-6.md

Scope (do not exceed):
- Files to touch: <agent's allocated list from the table>
- Plan ranges to consult: <line ranges from the plan for this agent>
- Read-only references: <agent's read-only list>

Constraints:
- No edits outside scope files.
- No pushes. Atomic commits with conventional-commit messages are fine.
- No em or en dashes, no connector hyphens in any prose, error messages,
  log strings, or email copy. Identifiers and slugs may keep hyphens.
- SQL RPC names: credit_wallet, try_hold_for_job, settle_hold, release_hold.
- Migration file is 0017_wallet.sql (not 0015).

When done, report:
- Files changed
- Tests added and their pass count
- Any unilateral contract decisions you had to make (so Wave 3 can review)
- Any blockers
```

---

## Stripe dashboard work (parallel with Wave 2 agents)

Seven passes, test mode first, then live. Plan section "Stripe dashboard, click paths" has every click.

1. Archive legacy SKUs: `STRIPE_PRICE_SCOUT_PRO`, `STRIPE_PRICE_LAB`, `STRIPE_PRICE_LAB_PLUS`, `STRIPE_PRICE_WORKSPACE_STANDARD`, `STRIPE_PRICE_WORKSPACE_XL`.
2. Create product "Tools-Hub Wallet Top-Up" with no fixed Price (Checkout uses inline `price_data`).
3. Configure Stripe Tax with Ranomics legal entity address.
4. New webhook endpoint listening on `checkout.session.completed`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.dispute.created`.
5. Regenerate test secret key (closes Session 3 blocker #12).
6. Test-mode E2E smoke on a Railway preview deploy, 16 steps in the plan.
7. Live mode flip.

Estimated wall time for passes 1-5: 30-45 min. Run while agents are coding.

---

## Wave 3 (after all 5 agents report green)

1. **Cross-agent diff review** in a single subagent (`gsd-code-reviewer` or `general-purpose`) reading the union of files touched. Flag any contract violations, missed dash usage, missing tests.
2. **Local integration smoke** via `pytest` full suite + targeted curl against `flask run`:
   - Submit a tool with empty wallet, get the "Top up and run" gate.
   - Submit `topup-complete` with a test Stripe event, see wallet credit.
   - Submit a tool with funded wallet, see hold row + settle row.
3. **Railway preview deploy** + the 16-step E2E in test mode.
4. **Live cutover** — Stripe live mode, Railway env vars finalized, daily reconcile cron deployed.
5. **Real $20 top-up by Leo** on a personal card to validate the end-to-end production path.

---

## Critical gotchas (carried + updated)

1. **Migration is `0017`, not `0015`.** Any docs/tooling that say `0015_wallet.sql` are stale.
2. **No dashes in customer prose.** Em, en, connector hyphens all out. Hyphens in identifiers fine.
3. **Marketing tool MDX titles still use em dash** (`Epitope Scout — ...`). That is a parser convention for `tools/[...slug].astro` on the ranomics site, not customer prose. Leave those alone.
4. **`shared/workspaces.py` is still present.** Do not delete until wallet has been live and verified for at least a week. Modal cost rate card was duplicated out of it into `shared/wallet.py` and `shared/wallet_estimates.py`, so workspaces.py is only kept for historical record reads.
5. **No auto-mode browser writes to prod Supabase.** The classifier blocks them even after chat-level approval. If a future SQL run is needed against prod, either switch the mode in Claude Code settings or fall back to manual paste in the SQL editor (read-back via browser inspection is fine).
6. **Supabase SQL editor batches are transactional.** A single error rolls back every prior CREATE / INSERT in the same paste. Always paste a complete migration in one go, never piecemeal.
7. **`FILTER` attaches only to aggregates.** `ABS(SUM(x)) FILTER (...)` is rejected by Postgres; must be `ABS(SUM(x) FILTER (...))`. This bit Session 6 once.
8. **`INSERT ... UNION ALL` with enum literals needs explicit casts.** UNION resolves all branches' types before checking the target column. Single-SELECT inserts (the migration's pattern) infer correctly; UNION ALL inserts (the test fixture's pattern) do not. Cast literals to the enum.
9. **Modal/Windows quirks unchanged:** prepend `/c/Program Files/nodejs` to `PATH` for `npm`; set `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` for `modal deploy`.

---

## Next session quickstart

Open Claude Code in `C:\Users\lab\Documents\Claude_projects\tools-hub`, paste this:

> Read `docs/HANDOFF-WALLET-PIVOT-SESSION-6.md`. We are at Gate A green with two uncommitted fixes on disk. Three things in order:
>
> 1. Commit the two source fixes (`supabase/migrations/0017_wallet.sql` + `tests/sql/test_0017_wallet.sql`) using the message in the handoff's "Uncommitted fixes" section, then push to `origin/main`.
> 2. Optionally commit the four untracked handoff docs under `docs/` as a `docs:` commit.
> 3. Dispatch Wave 2 in a single message with five parallel `general-purpose` agents (C, D, E, G, H) using the per-agent scope tables and prompt template in the handoff's "Wave 2 parallel dispatch plan" section. None push. While they run, I will start the Stripe dashboard passes in another tab.
>
> When all five agents report green, do a Wave 3 cross-diff review per the handoff before any deploy.

---

## Open questions for Leo (none blocking)

1. Slack webhook URLs for `#sales-leads` and `#ops` (env vars for funnel alerts). Without these, alerts log only.
2. Stripe Tax origin address (Ranomics legal entity).
3. Whether `auto_reload_monthly_cap_usd` default stays at $1,000 or goes higher.
4. Whether the daily spend cap default stays at $200 or goes higher.

All four are policy levers, none block Wave 2 dispatch.
