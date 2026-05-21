# Tools-Hub Wallet Pivot, Session 11 Handoff

**Date:** 2026-05-15 (session paused mid-Pass-7 pre-flight)
**Supersedes:** `HANDOFF-WALLET-PIVOT-SESSION-10.md`
**Authoritative plan:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md`
**Pass 6 results doc:** `docs/PASS-6-SANDBOX-RESULTS.md`

---

## TL;DR

Session 11 did two things:

1. **Finding 1 (topup frozen guard) shipped to `origin/main` as commit `84b79f0`.** Production now blocks `/account/wallet/topup` (GET) and `/account/wallet/checkout` (POST) when `wallet_frozen=True`, redirecting users to `/account/wallet?wallet_frozen=1` where the existing overview banner already promises "New jobs and top ups are paused" — the code now matches the copy. 3 regression tests added covering the GET redirect, the GET pass-through when not frozen, and the POST redirect with an assertion that `create_topup_session` is never called.

2. **Pass 7 (live Stripe smoke) pre-flight passed for items 1, 2, 3 but BLOCKED on item 4.** The live Stripe account is fully activated and Tax is configured, but the live webhook endpoint is subscribed to only 1 of the 4 events the wallet code requires. Pass 7's $20 real-money topup is on hold pending a 2-minute dashboard edit by Leo.

**HEAD on `main` (origin/main + local in sync):** `84b79f0` fix(wallet): block topup routes when wallet_frozen.

---

## What Session 11 did

1. **Validated Session 10 work end-to-end.** Confirmed the 4 Session 9 hotfix commits (`ce30076`, `d8e5451`, `7d6d13f`, `f3fd719`) are on `origin/main` at `db7c008`. Verified the test user wallet invariant: balance `$89.9895` exactly equals the sum of all 28 ledger rows.

2. **Implemented Finding 1.** Mirrored the existing `wallet_preflight` frozen-guard pattern (`shared/wallet.py:437` and route redirect at `shared/wallet.py:858`) into the two topup routes. Single point of control at each route's top, before any other validation. Redirect target chosen to land on the existing frozen banner copy in `templates/wallet/overview.html:46`.

3. **Wrote 3 regression tests.** New class `TestWalletTopupFrozenGuard` in `tests/test_wallet_api.py` (lines 715-786). Includes a fuller wallet fixture (`_wallet` staticmethod) so the not-frozen pass-through test does not crash Jinja on missing auto_reload_* keys. All 3 pass; full wallet test suite is 86/87 pass with the one failure pre-existing on `main` (Stripe-not-configured in `test_auto_reload_triggers_when_eligible`).

4. **Ran Pass 7 pre-flight via `railway run`.** Wrote a `.deploy-logs/pass7_preflight_live_stripe.py` script that queries the live Stripe account using prod-injected env vars and audits the four pre-flight items (key mode, account activation, Tax, webhook endpoints). Items 1-3 GREEN; item 4 found the gap.

5. **Updated memory.** Refreshed `project_tools_hub_wallet_pivot.md` to Session 11 state. Added `reference_railway_run_prod_env.md` for future agents who need to query prod-only state.

---

## Pass 7 pre-flight tally

| Item | Status | Notes |
|---|---|---|
| 1. `STRIPE_SECRET_KEY` is `sk_live_` | GREEN | Confirmed via `railway run` |
| 2. Stripe account fully activated | GREEN | `acct_17ntxDHK3YN42tFl` / Ranomics Inc. — `charges_enabled`, `payouts_enabled`, `details_submitted` all True; no Connect requirements |
| 3. Stripe Tax | GREEN | status `active`, default `txcd_10000000`, `tax_behavior` inferred_by_currency |
| 4. Live webhook endpoint events | **BLOCKED** | Endpoint exists at right URL but only 1 of 4 required events subscribed |
| 5. `STRIPE_WEBHOOK_SECRET` shape | OK | Length and prefix sane; signature-verification correctness still unproven |

### The webhook gap

- **Endpoint id:** `we_1TPPD4HK3YN42tFlJK8mQ6LS`
- **URL:** `https://tools.ranomics.com/webhooks/stripe`
- **API version:** `2026-03-25.dahlia` (pinned — leave as-is)
- **Currently subscribed:** `checkout.session.completed`, plus several subscription/invoice events for other products
- **MISSING (required by wallet code):**
  - `payment_intent.succeeded` — auto-reload off-session charges depend on this
  - `payment_intent.payment_failed` — failed-card handling depends on this
  - `charge.dispute.created` — `freeze_wallet_on_dispute` depends on this

### Impact if Pass 7 runs without fixing this

- $20 happy-path topup: would succeed (the one subscribed event is the right one for that flow).
- Auto-reload: silently no-ops on first balance drop — wallet never re-credited even though the off-session PI clears.
- Card failures during topup: silently lose state — failure handler never runs.
- Chargebacks: `wallet_frozen` never flips — Finding 1's guard we just shipped would never trigger in prod.

### How Leo fixes it (2-minute dashboard edit)

1. Open https://dashboard.stripe.com/webhooks in **Live mode** (top-right toggle).
2. Click endpoint with URL `https://tools.ranomics.com/webhooks/stripe` (id `we_1TPPD4HK3YN42tFlJK8mQ6LS`).
3. Edit endpoint → add the three missing event types: `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.dispute.created`. Keep the API version pinned at `2026-03-25.dahlia`. Save.
4. Verify the signing secret did not rotate. It should still match Railway env's `whsec_jgn9E0wj1QtDIeNZ2s6Gqm76aVtUcELs`. If it rotated, copy the new secret and `railway variables --set STRIPE_WEBHOOK_SECRET=whsec_...` for production, then `railway up`.
5. Use "Send test webhook" with `payment_intent.succeeded` to validate the signature path before running Pass 7.

Once that's done, re-run the pre-flight to confirm all 4 events show up:

```
cd C:/Users/lab/Documents/Claude_projects/tools-hub
railway run --service web --environment production -- "C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe" .deploy-logs/pass7_preflight_live_stripe.py
```

---

## Test user baseline (carry into Pass 7)

- **user_id:** `03e51184-4d04-4acd-ab22-0cbd7fa08c77` (leowan7@gmail.com)
- **balance_usd:** `$89.9895`
- **wallet_frozen:** False
- **stripe_customer_id:** `cus_UWR3IFRvQ2R2GW` (SANDBOX — will be overwritten on first live checkout)
- **stripe_payment_method_id:** `pm_1TXNqcHK3YN42tFlNElV3diy` (SANDBOX — will be overwritten if save-card checked)
- **auto_reload_enabled:** True (threshold $80, amount $25, monthly cap $1000)
- **Ledger invariant:** balance matches `SUM(amount_usd)` across all 28 rows.

Post-Pass-7 expectation: balance ~$109.9895, new `topup +$20` row, `stripe_customer_id`/`stripe_payment_method_id` flip to live values. The live watch script in `.deploy-logs/pass7_watch.py` flags those flips with `[LIVE]` vs `[sandbox]` markers.

---

## Carry-over concerns (unchanged from Session 10)

1. **`wallet_transactions.job_id` bigint vs `tool_jobs.id` uuid.** Every ledger row has `job_id = NULL`; linkage runs via `parent_tx_id` + `tool_slug` + timing.
2. **`tool_jobs_p90` view missing.** `shared/wallet_estimates.py:290` references it; baseline-estimate fallback handles the absence gracefully.
3. **"1 credits" copy on `/jobs/<id>`** and MPNN form estimate text — leftover credit-era language.
4. **Sub-test 16b live** (absorbed_variance branch) — code-review verified only in Session 10, needs intrusive wallet drain to exercise live.
5. **Finding 2 (`daily_spend_cap_usd`).** Column exists on `user_wallets` but no enforcement path found anywhere. Audit before treating it as user-visible behavior.

---

## What's left

### Immediate next-session priorities

1. **Re-run Pass 7 pre-flight** after Leo fixes the webhook events. If item 4 flips GREEN, proceed to the actual $20 live topup.
2. **Pass 7 execution.** Leo runs the live topup on `https://tools.ranomics.com` while a watch script (`.deploy-logs/pass7_watch.py`) polls the wallet. Verify: new `topup +$20` ledger row, `cus_*`/`pm_*` flip to live values, no `signature verification failed` in Railway logs.
3. **Optional Pass 7 extension.** After topup, submit an MPNN job to exercise the full hold→settle path on prod with a live wallet (Pass 6 Step 7 equivalent, but on production).
4. **Audit Finding 2.** Decide whether `daily_spend_cap_usd` is groundwork or missed wiring.

### Deferred (long-standing)

- Job_id type mismatch migration.
- `tool_jobs_p90` view (add or remove the reference).
- Credit-era copy cleanup.

---

## Known mess to clean up

**Untracked-but-intentional:** `tools-hub/.deploy-logs/` is gitignored. Holds:

- `pass7_baseline.py` — one-shot wallet snapshot
- `pass7_watch.py` — live-poll watch (Ctrl+C to stop)
- `pass7_preflight_live_stripe.py` — live Stripe audit script (item 1-4 above)
- `pass7_webhook_deliveries.py` — recent live events list (limited usefulness; the Stripe API does not expose per-endpoint delivery status here)

Reusable for future live-Stripe work; safe to leave in place.

**Branches:** clean, `main` synced.

---

## Quick reference

- **Test user:** `03e51184-4d04-4acd-ab22-0cbd7fa08c77` (`leowan7@gmail.com`), sandbox `cus_UWR3IFRvQ2R2GW`, sandbox `pm_1TXNqcHK3YN42tFlNElV3diy`
- **Live Stripe account:** `acct_17ntxDHK3YN42tFl` / Ranomics Inc.
- **Live webhook endpoint:** `we_1TPPD4HK3YN42tFlJK8mQ6LS` at `https://tools.ranomics.com/webhooks/stripe`
- **Production URL:** `https://tools.ranomics.com` (git-connected to `main`)
- **Production env query pattern:** `railway run --service web --environment production -- <abs-python-path> <script>` (see `~/.claude/projects/.../memory/reference_railway_run_prod_env.md`)
- **Preview URL:** `https://web-preview-90b3.up.railway.app` (NOT git-connected; manual `railway up --service web --environment preview --detach`)
- **Stripe sandbox webhook (unchanged):** `we_1TX2y1HK3YN42tFlo7xswCTl` at preview URL, 4 events subscribed

---

## To start the next session

Read this handoff and the Session 10 handoff. Then either:

1. **If Leo says "webhook fixed":** re-run the pre-flight script, confirm GREEN on all 4 items, and proceed to Pass 7 live topup with the watch script running in parallel.
2. **If Leo says "do Finding 2 first":** audit `daily_spend_cap_usd` — grep `shared/wallet.py`, `webhooks/stripe.py`, `app.py`, and migrations for any reference; decide whether the column is groundwork or missed wiring.
3. **If Leo says "something else":** read the carry-over concerns list and pick the most load-bearing item.
