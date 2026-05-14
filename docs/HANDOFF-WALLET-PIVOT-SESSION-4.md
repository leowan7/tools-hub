# Tools-Hub Wallet Pivot — Session 4 Handoff

**Date:** 2026-05-13
**Supersedes:** `HANDOFF-WORKSPACE-MIGRATION-SESSION-3.md` (the Workspace-per-target work is being replaced by the wallet model)
**Authoritative plan:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md` (≈2000 lines, contains every code block, SQL, click-path, and email template)

---

## TL;DR

The pricing model is pivoting again. The previous two attempts (subscription tiers, then Workspace-per-target) are both being scrapped in favor of a **USD wallet with auto-reload** model that matches Modal / OpenAI / Anthropic mental models. The marketing site has shipped (ranomics.com pushed live 2026-05-13). The tools-hub backend has not been touched yet — that is what this session needs to do.

---

## Status snapshot

| Surface | Status | Where |
|---|---|---|
| Strategic decisions | LOCKED | This document + plan file |
| Marketing site (ranomics.com `/tools/pricing`) | **LIVE on main** | commit `73d9aed` on ranomics-website-2026 |
| Marketing-site copy on Epitope Scout, ProteinMPNN blog, tools/[slug], tools/index | **LIVE on main** | same commit |
| Marketing nav (Tools dropdown utility → /tools/pricing) | **LIVE on main** | same commit |
| Tools-hub repo: SQL migration | NOT STARTED | this repo |
| Tools-hub repo: wallet.py + estimates + funnel | NOT STARTED | this repo |
| Tools-hub repo: billing/checkout.py rewrite | NOT STARTED | this repo |
| Tools-hub repo: webhooks/stripe.py rewrite | NOT STARTED | this repo |
| Tools-hub repo: app.py `requires_wallet` + replay-submit flow | NOT STARTED | this repo |
| Tools-hub repo: shared/jobs.py settle + progress monitor | NOT STARTED | this repo |
| Tools-hub repo: 13 email senders | NOT STARTED | this repo |
| Tools-hub repo: wallet UI templates | NOT STARTED | this repo |
| Stripe dashboard: archive legacy SKUs | NOT STARTED | external |
| Stripe dashboard: new wallet top-up product | NOT STARTED | external |
| Stripe dashboard: new webhook endpoint | NOT STARTED | external |
| Stripe dashboard: live mode + Tax | NOT STARTED | external |
| 6 unpushed local-main commits (workspace pivot) | Sitting on local main | Will be salvaged, not pushed as-is |

---

## Decisions locked (do not reopen these in the next session)

| Decision | Value | Source |
|---|---|---|
| Pricing model | USD wallet + auto-reload | User confirmation 2026-05-13 |
| Tiers | None (single wallet, suggested top-up amounts only) | User: "I don't want tiers. i want people to just put value in and it gets used up" |
| Markup over raw Modal cost | **1.7x** | 5-agent research synthesis; Ariax floor at 1.4x, Replicate at 1.8x, scenario analysis revenue-peak at 1.7x |
| Minimum top-up | $20 | Plan section "Concrete pricing parameters" |
| Suggested top-up amounts | $20, $50, $200, $500, $2,500 | Plan |
| Signup credit | $5 one-time, no expiration | Plan |
| Refund policy | NONE — all sales final | User: "No refunds, balance is final" |
| Charge timing | Pre-authorize estimate at submit, true-up on completion | User: "Pre-authorize estimate then true-up on completion (Recommended)" |
| Failure billing | Charge for actual compute consumed even on failure | User: "Charge for actual compute consumed regardless of outcome" |
| Auto-reload safety | Max 1 per 24h + user-set monthly cap (default $1,000) | User: "Max 1 auto-reload per 24h + user-set monthly cap (Recommended)" |
| Insufficient-balance UX | Block submit, replace with one-click "Top up $X and run" combined flow | User: "Block submit + one-click 'Top up $X and run' (Recommended)" |
| Long-job protection | Parameter-scaled hard caps (up to $500/tool) + mid-run progress monitor (1.5× warning, 2× kill) | User: "Parameter-scaled hard cap + mid-run progress monitor (Recommended)" |
| Self-serve absolute ceiling | $1,000 per job; above this, route to Binder Pilot | Plan |
| Subscription option | NONE in v1 (packs / wallet forever) | User: "Skip subscription entirely, packs forever" |

---

## Reference docs

| Doc | Path | Use |
|---|---|---|
| **AUTHORITATIVE PLAN** | `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md` | Every code block, SQL, click-path, email template |
| This handoff | `tools-hub/docs/HANDOFF-WALLET-PIVOT-SESSION-4.md` | Quick orientation + start instructions |
| Memory pointer | `~/.claude/projects/.../memory/project_tools_hub_wallet_pivot.md` | Auto-loaded into next session via MEMORY.md |
| Tools-hub infra memory | `~/.claude/projects/.../memory/project_tools_hub_infra.md` | Existing infra reference; flagged at top with pivot status |
| Previous session 3 handoff | `tools-hub/docs/HANDOFF-WORKSPACE-MIGRATION-SESSION-3.md` | **SUPERSEDED** by this doc but useful for what the Workspace pivot built (some is salvageable) |
| Previous session 2 handoff | `tools-hub/docs/HANDOFF-WORKSPACE-MIGRATION-SESSION-2.md` | Also superseded |
| Product plan | `tools-hub/docs/PRODUCT-PLAN.md` | Workspace-pivot era doc, partly obsolete |
| Marketing site code | `C:\Users\lab\Documents\Claude_projects\ranomics-website-2026\src\pages\tools\pricing.astro` | Live customer-facing copy; reference for tone/numbers consistency |

---

## What this session did

1. **Read the previous session 3 handoff** and concluded the Workspace-per-target model was strategically wrong (big upfront commit, 30-day TTL waste, per-target binding, opaque caps)
2. **Explored both codebases** (tools-hub via Agent for billing/catalog/route gating; ranomics-website for pricing-related surface area)
3. **Designed the wallet model** through 3 rounds of `AskUserQuestion` and 5 parallel research agents (Opus + Sonnet + Haiku across 5 angles: competitors, AI inference, managed SaaS norms, scenario analysis, audience willingness-to-pay)
4. **Stress-tested with a `Plan` agent** which forced corrections on free-tier size, tier count, and identified the ~70% salvage path from Workspace code
5. **Wrote the comprehensive plan** at `~/.claude/plans/i-am-in-the-moonlit-quill.md` covering business model, full SQL migration, all Python code blocks, Stripe dashboard click-paths, 13 email templates, parallel agent orchestration plan, and verification strategy
6. **Shipped marketing site changes** to production (ranomics.com main, Vercel auto-deploys):
   - Created `/tools/pricing` page (hero, how-billing-works, cost table at 1.7x, suggested top-ups, billing-safety, Pilot funnel CTA, FAQ JSON-LD)
   - Stripped credit-based pricing labels from `/tools` GPU tool cards
   - Replaced the 3-tier subscription strip ($49/$299/$999) with the wallet model strip
   - Updated hero copy, title, meta description
   - Updated Epitope Scout MDX (removed Scout Pro $49/mo)
   - Updated ProteinMPNN blog post (credits → dollars)
   - Updated generic tool slug page CTA
   - Updated nav dropdown utility to point Pricing at `/tools/pricing`
7. **Updated auto-memory** with `project_tools_hub_wallet_pivot.md`, updated `project_tools_hub_infra.md` and `MEMORY.md` index

---

## State of the 6 unpushed local-main commits in this repo

The previous session built a Workspace-per-target model in 6 commits on local main, not pushed. **Do NOT push those commits.** They are scratch work that should be salvaged file-by-file into the wallet implementation.

Per the plan, salvage rates by file:

| File from session 3 commits | Salvage % | Note |
|---|---|---|
| `webhooks/stripe.py` signature + idempotency (lines 356–402) | 100% | Keep verbatim |
| `webhooks/stripe.py` `_apply_checkout_event` (lines 229–318) | 30% | Replace `activate_workspace()` with `top_up_wallet()`; add 3 new event handlers |
| `webhooks/stripe.py` refund endpoint (lines 404–485) | **0%** | Delete; no refunds under new policy |
| `billing/checkout.py` lines 81–182 | 60% | Rewrite for variable-amount + SetupIntent |
| `billing/checkout.py` lines 185–234 portal | 100% | Keep verbatim |
| `shared/workspaces.py` GPU cost rate card + Modal-cost-per-second logic | 100% | Salvage to `shared/wallet.py` |
| `shared/workspaces.py` everything else | 20% | Rewrite as `shared/wallet.py` per plan code block |
| `supabase/migrations/0014_workspaces.sql` | 0% | Keep as historical paper trail; add new `0015_wallet.sql` |
| `templates/workspaces/*` | 0% | Rewrite as `templates/wallet/*` |
| `app.py` `workspace_preflight` wiring | 0% | Replace with `requires_wallet` decorator pattern |
| `shared/jobs.py::_charge_workspace_for_completed_job` (lines 558–654) | 70% | Rename to `_settle_job_after_run`, reuse Modal cost logic, add 15-min progress monitor |
| `shared/email.py` 80% / cap-exhausted senders | 60% | Repurpose to low-balance + auto-reload-charged |
| `tests/test_workspace*.py` | 30–70% | Rewrite as `tests/test_wallet*.py` |

**Recommendation:** Do not try to revert or reset main. Cherry-pick the salvageable hunks file by file as you implement each wallet module, then drop the rest. The git history will look cleaner that way.

---

## Next session quickstart

1. **Open a new session in this repo:** `cd C:\Users\lab\Documents\Claude_projects\tools-hub` and start Claude Code there.

2. **First message to send:**
   > Read `docs/HANDOFF-WALLET-PIVOT-SESSION-4.md` and `~/.claude/plans/i-am-in-the-moonlit-quill.md`. We are executing the wallet pivot Wave 1 (per the parallel agent orchestration plan in the plan file). Start by running Wave 1 in parallel: 4 agents on schema migration, wallet core, marketing copy (already done), and pricing page (already done). Since the marketing-side agents are done, dispatch just the 2 backend agents: schema + wallet core.

3. **Or, if you prefer sequential single-agent execution:**
   - Step 1: Create `supabase/migrations/0015_wallet.sql` per the plan
   - Step 2: Create `shared/wallet.py`, `shared/wallet_estimates.py`, `shared/wallet_funnel.py` per the plan
   - Step 3: Unit tests
   - Step 4: Run migration locally + run tests (`pytest tests/test_wallet*.py`)
   - Step 5: If green, proceed to Wave 2 (Stripe + webhooks + routes + emails + UI in parallel)

4. **Stripe dashboard work** (interleave with code; do test mode first):
   - Pass 1: Archive Workspace Standard / XL / Scout Pro / Lab / Lab Plus SKUs
   - Pass 2: Create "Tools-Hub Wallet Top-Up" product
   - Pass 3: Stripe Tax setup
   - Pass 4: New webhook endpoint with `checkout.session.completed`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.dispute.created`
   - Pass 5: Regenerate test secret key (closes existing blocker #12)
   - Pass 6: Test-mode E2E smoke on Railway preview
   - Pass 7: Live mode

---

## Critical gotchas

1. **Worktrees folder is a Windows worktree.** `\n` line endings will be converted to CRLF on file write — that triggers Git warnings but is harmless.

2. **Node.js PATH:** prepend `/c/Program Files/nodejs` to PATH before running npm. Per memory: `export PATH="/c/Program Files/nodejs:$PATH" && npm run build`.

3. **PYTHONIOENCODING for Modal deploys:** `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` before `modal deploy` (existing memory note for Windows).

4. **The marketing site `/tools` page has stripped credit-based pricing labels off GPU tool cards.** Once tools-hub backend ships with the wallet model, that's consistent. But if backend is delayed and users land on `tools.ranomics.com` while it still shows credit pricing, there will be a mismatch. **Mitigation:** Don't link directly to GPU tool pages from external marketing until tools-hub wallet UI is live. The `/tools/pricing` page is fine to link to anywhere — it's all forward-compatible.

5. **The 6 unpushed local-main commits in this repo include a `workspaces` Supabase migration (`0014_workspaces.sql`)** that was meant to be deployed but never was. The new `0015_wallet.sql` should be the next migration. Do not run `0014_workspaces.sql` on production Supabase — keep it as a historical paper trail only.

6. **Stripe test-mode E2E checklist (Pass 6 in plan) is 16 steps.** Do not skip ahead to live mode until all 16 pass on Railway preview.

7. **No em/en dashes or connector hyphens in any customer-facing prose.** This applies to: Stripe receipts, email templates, UI strings, error messages. Memory: `feedback_no_dashes_in_posts.md`. Restructure sentences rather than use dashes.

8. **`tools/[...slug].astro` and `tools/index.astro` on the marketing site reference `tool.data.title.split('—')` and `tool.data.pricing.split('.')` — those depend on the em-dash pattern in tool MDX titles.** Not a tools-hub concern, but worth knowing if you ever need to add a new tool MDX file: titles MUST have the em-dash pattern (e.g., "Epitope Scout — ..."). This is a pre-existing convention; the no-dashes rule is for prose content, not these structural splits.

---

## Verification checklist (you are done when…)

- [ ] `0015_wallet.sql` migration applied to production Supabase
- [ ] `pytest tests/test_wallet*.py` green (target: 95%+ coverage on `shared/wallet.py`)
- [ ] Property-based tests for ledger invariants green (`tests/test_wallet_invariants.py`)
- [ ] Concurrency tests green (N concurrent submits cannot overspend)
- [ ] Stripe test-mode E2E: all 16 steps in plan Pass 6 pass on Railway preview
- [ ] Stripe live mode: legacy SKUs archived, new wallet product created, Tax enabled, webhook endpoint live
- [ ] Railway env vars set per plan ("Railway environment variables — final set" section)
- [ ] Daily reconcile cron deployed (`scheduled/reconcile_wallets.py`)
- [ ] Real $20 top-up by Leo from a personal card succeeds end-to-end on production
- [ ] Real MPNN job runs, debits balance correctly
- [ ] Real BindCraft pilot at 1000 designs runs, parameter-scaled cap kicks in, settles correctly
- [ ] Funnel-signal Slack alert fires on synthetic $5k spend
- [ ] All 13 emails deliverable via Resend test address
- [ ] Wallet-frozen flow tested by triggering a Stripe test dispute
- [ ] Auto-reload tested: enable, run jobs to threshold, see SetupIntent fire, confirm 24h cap rejects second auto-reload
- [ ] Monthly auto-reload cap tested: synthetic month-rollover via DB time travel or env-var override

---

## Open questions for Leo (none blocking — defaults locked in plan)

1. Slack webhook URL for `#sales-leads` and `#ops` channels — needed for funnel alerts and drift alerts. Plan defaults: env vars `WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL` and ops equivalent.
2. Stripe Tax origin address — needs Ranomics business legal entity address.
3. Whether to set `auto_reload_monthly_cap_usd` default at $1,000 (plan default) or higher.
4. Whether to keep the daily spend cap default at $200 (plan default) or higher.

These are all small policy levers; Leo can adjust them in the user_wallets defaults migration or in a follow-up after launch.
