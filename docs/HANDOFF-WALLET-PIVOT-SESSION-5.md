# Tools-Hub Wallet Pivot, Session 5 Handoff

**Date:** 2026-05-14
**Supersedes:** `HANDOFF-WALLET-PIVOT-SESSION-4.md` (Session 4 shipped marketing site; Session 5 shipped Wave 1 backend)
**Authoritative plan:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md` (about 2000 lines, contains every SQL block, Python module, Stripe click path, and email template)

---

## TL;DR

Wave 1 backend is done locally and tested. 8 commits sit on local `main` in `tools-hub`, none pushed. 94 of 94 wallet Python unit tests pass; full suite at 455 pass, 6 pre-existing skips. The SQL migration is parser-clean via `pglast` but has not been executed against a real Postgres yet, which is the only remaining Gate A blocker. Wave 2 (Stripe checkout, webhooks, route wiring, emails plus UI) is ready to dispatch the moment Gate A is signed off.

---

## Status snapshot

| Surface | Status | Notes |
|---|---|---|
| Strategic decisions | LOCKED | See decision table in Session 4 handoff |
| Marketing site (ranomics.com `/tools/pricing` plus all related copy) | LIVE on production | commit `73d9aed` on `ranomics-website-2026`, Vercel deployed |
| Tools-hub: SQL migration `0017_wallet.sql` | DONE local, NOT applied to prod Supabase | commit `7527371` |
| Tools-hub: psql test file `tests/sql/test_0017_wallet.sql` | DONE local, NOT executed against live Postgres | commit `2a6df06` |
| Tools-hub: `shared/wallet.py` plus estimates plus funnel modules | DONE local | commits `abf1543`, `814649e`, `faa3aae`, `bcd2529` |
| Tools-hub: 4 Python test files (94 tests, all pass) | DONE local | commit `2bf75e6` |
| Tools-hub: 14 stub email senders in `shared/email.py` | DONE local (as stubs) | commit `abf1543` |
| Tools-hub: `billing/checkout.py` rewrite | NOT STARTED | Wave 2 Agent C |
| Tools-hub: `webhooks/stripe.py` rewrite | NOT STARTED | Wave 2 Agent D |
| Tools-hub: `app.py` `requires_wallet` decorator plus replay-submit flow | NOT STARTED | Wave 2 Agent E |
| Tools-hub: `shared/jobs.py` settle plus 15-min progress monitor | NOT STARTED | Wave 2 Agent E |
| Tools-hub: 13 real email senders (replacing stubs) | NOT STARTED | Wave 2 Agent G |
| Tools-hub: wallet UI templates and `templates/pricing.html` rewrite | NOT STARTED | Wave 2 Agent H |
| Stripe dashboard work (archive SKUs, new product, Tax, webhook endpoint, live mode) | NOT STARTED | External, interleave with Wave 2 |
| 8 unpushed commits on local `main` | Pending review | See "Unpushed commits" section below |

---

## What Session 5 did

1. Read `HANDOFF-WALLET-PIVOT-SESSION-4.md` and the relevant ranges of the authoritative plan.
2. Discovered a migration numbering collision. The plan was written assuming `0014` was the latest migration. In reality the repo had moved on and `0015_signup_rejections.sql` plus `0016_user_profiles_and_events.sql` are already deployed. The new wallet migration needed to be `0017_wallet.sql`, not the plan's `0015_wallet.sql`.
3. Dispatched 2 Wave 1 backend agents in parallel via `general-purpose` subagents.
4. Verified both agents completed cleanly and committed atomically without pushing.
5. Wrote this handoff doc.

The Wave 1 marketing-side agents (C and D in the plan's Wave 1 quartet) were already complete from Session 4. Only the two backend agents were dispatched this session.

---

## Wave 1 deliverables on disk

| File | Lines | Source |
|---|---|---|
| `supabase/migrations/0017_wallet.sql` | 403 | Agent A |
| `tests/sql/test_0017_wallet.sql` | 592 | Agent A |
| `tests/sql/README.md` | 39 | Agent A |
| `shared/wallet.py` | 1014 | Agent B |
| `shared/wallet_estimates.py` | 337 | Agent B |
| `shared/wallet_funnel.py` | 230 | Agent B |
| `tests/test_wallet.py` | 1106 | Agent B |
| `tests/test_wallet_estimates.py` | 325 | Agent B |
| `tests/test_wallet_funnel.py` | 273 | Agent B |
| `tests/test_wallet_invariants.py` | 262 | Agent B |
| `shared/email.py` (modified, plus 97 lines) | n/a | Agent B (14 stub senders) |

Total new code roughly 4,500 lines.

---

## Unpushed commits

Run `cd tools-hub && git log --oneline origin/main..HEAD` to see the current set. As of Session 5 end:

```
a2737c2 docs: session handoff notes for 2026-05-13 work block   (auto-committed Session 4 handoff doc)
2bf75e6 test(wallet): unit tests for wallet core, estimates, funnel, invariants
bcd2529 feat(wallet): funnel alert trigger in shared.wallet_funnel
faa3aae feat(wallet): wallet core in shared.wallet
814649e feat(wallet): estimator and per-tool hard caps in shared.wallet_estimates
abf1543 feat(wallet): stub email senders in shared.email
2a6df06 test(wallet): psql assertions for 0017_wallet.sql hold lifecycle
7527371 feat(wallet): 0017_wallet.sql USD wallet schema + hold lifecycle
```

Eight commits ahead of `origin/main`. None of these have been pushed. The user preference is "no auto-commits without review" so the push happens only after Gate A is signed off.

---

## Pre-existing uncommitted changes (not Session 5)

`git status --short` also shows:

```
 M app.py
 M shared/auth.py
 M templates/login.html
?? .claude/
?? .deploy-logs/
```

These were dirty in the working tree before Session 5 started. Wave 1 agents were instructed not to touch `app.py` or templates, and they did not. Confirm via `git diff app.py shared/auth.py templates/login.html` what these are; they predate the wallet pivot and need a separate disposition (commit them, stash them, or revert them) before Wave 2 starts. Wave 2 Agent E will touch `app.py` so the file must be clean by then.

---

## Gate A checklist

| Criterion | Status | How to verify |
|---|---|---|
| All Wave 1 unit tests green | YES | `pytest tests/test_wallet*.py -v` shows `94 passed in 0.55s` |
| Full project test suite still green | YES | Agent B reported `455 passed, 6 skipped` |
| Migration parses cleanly | YES | Agent A validated via `pglast` (libpg_query, same parser Postgres uses) |
| Migration runs cleanly on real Postgres | NOT VERIFIED | See "How to close the SQL gap" below |
| Migration is idempotent (re-run is a no-op) | Asserted in test file | Need live run to confirm |
| `$5` signup credit backfills exactly once per user | Asserted in test file | Need live run to confirm |
| Hold lifecycle invariants (no negative balance, no double-settle) | Asserted in test file | Need live run to confirm |
| Marketing build green | YES | Already on `ranomics.com` main, Vercel deployed |
| Manual diff review of all 8 commits | PENDING USER | `git log -p origin/main..HEAD` |

Gate A is one live SQL run plus one diff review away from green.

---

## How to close the SQL gap

Three options, pick whichever is fastest:

1. **Supabase CLI against linked project (recommended for speed):**
   ```
   cd C:\Users\lab\Documents\Claude_projects\tools-hub
   supabase db query --linked --file tests/sql/test_0017_wallet.sql
   ```
   The test file wraps all assertions in `BEGIN; ... ROLLBACK;` so nothing persists.

2. **Local Docker stack:**
   ```
   supabase db start
   psql $LOCAL_DB_URL -f supabase/migrations/0017_wallet.sql
   psql $LOCAL_DB_URL -f tests/sql/test_0017_wallet.sql
   ```

3. **Scratch Postgres container:**
   ```
   docker run --rm -p 5544:5432 -e POSTGRES_PASSWORD=test postgres:15
   psql postgresql://postgres:test@localhost:5544 -f supabase/migrations/0017_wallet.sql
   psql postgresql://postgres:test@localhost:5544 -f tests/sql/test_0017_wallet.sql
   ```

Known caveat from Agent A: the synthetic `auth.users` INSERT in the test fixture uses `(id, email, instance_id, aud, role)`. Newer managed Supabase versions may require additional NOT NULL columns. If the live run flags any, extend the INSERT and re-run.

---

## Contract decisions Agent A made unilaterally (eyeball these)

1. **Renamed plan's `0015_wallet.sql` to `0017_wallet.sql`.** Forced by the collision with already-deployed `0015_signup_rejections.sql` and `0016_user_profiles_and_events.sql`. Internal header comment explains the renumbering.

2. **Added `funnel_alerts` table to the migration.** The plan referenced this table from `shared/wallet_funnel.py` but never defined the schema. Agent A added a minimal append-only schema with two indexes, RLS on, no policies (service role only). Agent B's `wallet_funnel.py` calls match that schema. If a different shape is wanted, both halves need adjustment.

3. **Added `RETURNING id INTO v_charge_tx_id`** to the surplus, variance-debit, and absorbed-variance branches of `settle_hold`. The plan only captured the id in the zero-diff branch. This makes the return value useful for callers' audit logs.

4. **Added an `IF v_user_id IS NULL THEN RETURN NULL` guard** at the top of `settle_hold` so passing an unknown hold transaction id is a no-op rather than a NULL-NULL crash.

5. **Replaced all em and en dashes** in SQL comments with restructured prose per the no-dashes rule.

## Contract decisions Agent B made unilaterally (eyeball these)

1. **SQL RPC name for top-ups is `credit_wallet(p_user_id, p_amount_usd, p_kind, p_stripe_event_id, p_stripe_payment_intent_id)`** rather than the plan's free-form `_credit_wallet_rpc`. Agent A's migration uses the canonical name; Agent B's code calls it.

2. **`try_hold_for_job` RPC accepts `p_hard_cap_usd`** so the SQL function enforces the cap inside the same transaction as the balance check. The plan version only passed `estimate` plus `tool_slug`; passing the scaled cap explicitly makes the SQL function self-contained.

3. **Property-based tests use parametrized random walks across 10 seeds with a deterministic RNG** instead of `hypothesis`. `hypothesis` is not in `requirements.txt`, and per Agent B's spec, an example-based equivalent was acceptable. If you later want true property-based testing, add `hypothesis` to `requirements.txt` and rewrite `tests/test_wallet_invariants.py`.

4. **Modal cost rate card duplicated.** Agent B copied the `GPU_USD_PER_SECOND` dict from `shared/workspaces.py` into BOTH `shared/wallet.py` AND `shared/wallet_estimates.py`. The duplication keeps `wallet_estimates.py` from importing the full wallet module at import time, which avoids a circular dependency risk. If a single source of truth is preferred, extract the rate card to `shared/gpu_costs.py` and import from both.

5. **`shared/workspaces.py` was not touched.** Agent B salvaged code by reading it and re-typing into `wallet.py`, then left the original file alone. Per the plan, `workspaces.py` deletion happens in a later wave after wallet code is verified in production.

---

## Wave 2 readiness

Wave 2 is four parallel agents that depend on Wave 1 outputs but not on each other. Per the plan, all four run as `general-purpose` subagents in `tools-hub`. Each works against a different surface so concurrent edits do not collide.

| Agent | Scope | Files |
|---|---|---|
| C, Stripe checkout | Variable-amount Checkout, SetupIntent for auto-reload, Stripe customer creation | `billing/checkout.py` |
| D, Stripe webhooks | Handlers for `checkout.session.completed`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.dispute.created`; idempotency table | `webhooks/stripe.py` |
| E, app wiring | `requires_wallet` decorator applied to tool submit routes; `/api/wallet/estimate` reactive endpoint; `/account/topup-complete` replay-submit flow; `shared/jobs.py` settle on completion plus failure paths; 15-min progress monitor for long jobs | `app.py`, `shared/jobs.py`, `routes/*` |
| G, emails | Replace the 14 stub senders in `shared/email.py` with real Resend calls; copy the 13 email templates from the plan; ensure no dashes in any prose | `shared/email.py`, `templates/email/*` |
| H, UI | Wallet UI templates, `templates/pricing.html` rewrite, "Top up and run" combined-flow button on tool forms | `templates/wallet/*`, `templates/pricing.html`, partials |

Note that the plan groups H with C and D as Wave 2, with G a separate Wave 3 dependency. Pragmatically all five can run in parallel since none of them touch the same files. The single shared file is `shared/email.py`, which Agent G owns; Wave 1 only added stubs there, so Agent G has a clean diff to start from.

Recommended dispatch order:
1. Close Gate A first (live SQL run plus diff review).
2. Then in one message, dispatch C, D, E, G, H in parallel. Each prompt should:
   - Point at `tools-hub` as the working directory.
   - Cite the specific plan line ranges for that agent's scope.
   - Forbid touching files outside scope.
   - Allow atomic commits but forbid pushes.
   - Restate the no-dashes rule.
   - Specify the migration is `0017_wallet.sql` and the SQL RPC names are `credit_wallet`, `try_hold_for_job`, `settle_hold`, `release_hold`.

---

## Stripe dashboard work (external, interleave with Wave 2)

Seven passes, do test mode first. Full click paths in plan section "Stripe dashboard, click paths".

1. Archive legacy SKUs: `STRIPE_PRICE_SCOUT_PRO`, `STRIPE_PRICE_LAB`, `STRIPE_PRICE_LAB_PLUS`, `STRIPE_PRICE_WORKSPACE_STANDARD`, `STRIPE_PRICE_WORKSPACE_XL`. Test mode and live mode.
2. Create product "Tools-Hub Wallet Top-Up". No fixed Price; Checkout uses inline `price_data`.
3. Configure Stripe Tax in test, then live.
4. New webhook endpoint listening on `checkout.session.completed`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.dispute.created`.
5. Regenerate test secret key, which closes the lingering Session 3 blocker.
6. Test-mode E2E smoke on a Railway preview deploy, 16 steps in the plan, do not skip.
7. Live mode flip.

---

## Critical gotchas (carried from Session 4 plus new ones)

1. **Migration numbering is now `0017`, not `0015`.** Any tooling or docs that reference `0015_wallet.sql` is stale; the live file is `0017_wallet.sql`.

2. **Worktrees folder is a Windows worktree.** LF line endings convert to CRLF on file write. Triggers Git warnings, harmless.

3. **Node.js PATH gotcha:** prepend `/c/Program Files/nodejs` to `PATH` before `npm`. Use `export PATH="/c/Program Files/nodejs:$PATH" && npm run build`.

4. **PYTHONIOENCODING for Modal deploys:** set `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` before `modal deploy` on Windows.

5. **No em or en dashes, no connector hyphens in customer-facing prose.** Applies to Stripe receipts, email templates, UI strings, error messages, log messages. Hyphens in identifiers (`auth-uid`, `payment-intent`) are fine. Restructure sentences when tempted.

6. **The marketing site `tools/[...slug].astro` and `tools/index.astro` split tool titles on `—` (em dash).** This is a structural convention for tool MDX files, not customer prose. Tool titles MUST have the em dash pattern (e.g., `Epitope Scout — ...`). The no-dashes rule does not apply here.

7. **Pre-existing dirty state in `app.py`, `shared/auth.py`, `templates/login.html` must be resolved before Wave 2.** Wave 2 Agent E rewrites `app.py`, so the file needs to be at a known starting point.

8. **`shared/workspaces.py` is still present and untouched.** Do not delete it until the wallet model has been live in production and verified for at least a week. The Modal cost rate card was copied out of it, but historical reads of old workspace records still need the file.

9. **The 6 to 9 unpushed local-main commits include the workspace pivot.** Per the plan, salvage by reading, not by cherry-picking. The wallet implementation has already absorbed everything salvageable, so the workspace commits remain as a historical paper trail. Do not push them.

10. **Stripe test secret key is currently invalid** (Session 3 blocker #12). Pass 5 in the Stripe dashboard work regenerates it. Anyone running Stripe code locally before Pass 5 will see auth failures.

---

## Next session quickstart

1. Open Claude Code in `C:\Users\lab\Documents\Claude_projects\tools-hub`.

2. Paste this as the first message:

   > Read `docs/HANDOFF-WALLET-PIVOT-SESSION-5.md` and the line ranges of `~/.claude/plans/i-am-in-the-moonlit-quill.md` that it cites. We are at Gate A. Do two things in order. First, help me close Gate A by running `supabase db query --linked --file tests/sql/test_0017_wallet.sql` (or the Docker equivalent if linked is not available) and surfacing any failures with proposed fixes. Second, once Gate A passes and I confirm the diff review is clean, dispatch Wave 2 in a single message with five parallel `general-purpose` agents (C, D, E, G, H). Use the per-agent scope tables in the handoff plus the plan line ranges for the actual code blocks. None of them push. Pre-existing dirty state in `app.py`, `shared/auth.py`, `templates/login.html` needs disposition before Agent E starts; ask me what to do with those files.

3. Or, if you want to do Gate A manually first and only invoke Claude for Wave 2:

   ```
   cd C:\Users\lab\Documents\Claude_projects\tools-hub
   supabase db query --linked --file tests/sql/test_0017_wallet.sql
   git log -p origin/main..HEAD | less
   ```

   Then start Claude with a Wave 2 dispatch prompt.

---

## Verification checklist (you are done with the whole pivot when)

- [ ] `0017_wallet.sql` applied to production Supabase
- [ ] All `pytest tests/test_wallet*.py` green in CI
- [ ] Property-based invariant tests green
- [ ] Concurrency tests green (N concurrent submits cannot overdraw)
- [ ] Stripe test-mode E2E: 16 steps green on Railway preview
- [ ] Stripe live mode: legacy SKUs archived, wallet product created, Tax enabled, webhook live
- [ ] Railway env vars updated per plan section "Railway environment variables, final set"
- [ ] Daily reconcile cron deployed (`scheduled/reconcile_wallets.py`)
- [ ] Real $20 top-up by Leo on a personal card succeeds end-to-end on production
- [ ] Real ProteinMPNN job runs, debits balance correctly
- [ ] Real BindCraft pilot at 1000 designs runs, parameter-scaled cap kicks in, settles correctly
- [ ] Funnel-signal Slack alert fires on synthetic $5k spend
- [ ] All 13 emails deliver via Resend test address
- [ ] Wallet-frozen flow tested by triggering a Stripe test dispute
- [ ] Auto-reload tested: enable, run jobs to threshold, see SetupIntent fire, confirm 24h cap rejects second auto-reload
- [ ] Monthly auto-reload cap tested
- [ ] `shared/workspaces.py` deletion scheduled for one week after wallet live

---

## Open questions for Leo (none blocking)

1. Slack webhook URLs for `#sales-leads` and `#ops`. Env vars `WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL` and ops equivalent. Without these, funnel alerts log only.
2. Stripe Tax origin address. Needs Ranomics legal entity address.
3. Whether `auto_reload_monthly_cap_usd` default stays at $1,000 or goes higher.
4. Whether the daily spend cap default stays at $200 or goes higher.
5. Disposition for the pre-existing dirty state in `app.py`, `shared/auth.py`, `templates/login.html`. Commit, stash, or revert?

All five are small policy levers, none block Wave 2 dispatch.
