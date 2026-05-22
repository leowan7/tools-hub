# Tools-Hub Wallet Pivot, Session 13 Handoff

**Date:** 2026-05-22
**Supersedes:** `HANDOFF-WALLET-PIVOT-SESSION-12.md`
**Authoritative plan:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md`
**Pass 6 results doc:** `docs/PASS-6-SANDBOX-RESULTS.md`

---

## TL;DR

Session 13 cleared both Session 12 blockers and ran **Pass 7 (the live Stripe
smoke test) to completion**.

The Session 12 wallet fix is committed and pushed (`973a7f0`). The Pass 7
webhook gap is fixed (3 events added to the live endpoint). A second,
undocumented blocker surfaced: `STRIPE_WALLET_TOPUP_PRODUCT_ID` was never set
in Railway production because the live wallet topup Stripe product was never
created. Fixed: live product `prod_UZ2ZIctuj6GIaf` created, env var set,
production redeployed.

Pass 7 then ran green end to end: a live $20 topup credited the wallet via the
webhook, the charge was refunded in Stripe, and the wallet credit was reversed
with `pass7_rollback_topup.py`. Ledger invariant holds. Test user balance is
back to the pre-Pass-7 baseline of $89.9895.

**The wallet pivot's topup/credit path is now validated live.** The spend path
(a real job's hold then settle) has NOT been exercised on a live wallet. That
is the main thing left.

**One landmine:** the test user has `auto_reload_enabled = True` but its
`stripe_customer_id` and `stripe_payment_method_id` are still SANDBOX ids. See
"Test user state".

---

## What Session 13 did

1. **Committed and pushed the Session 12 work.** The 3-file daily-cap fix went
   in as `973a7f0`; the Session 11 + 12 handoff docs as `163f336`. Both pushed
   to `origin/main`, which redeployed production.

2. **Fixed the Pass 7 webhook gap.** Added `payment_intent.succeeded`,
   `payment_intent.payment_failed`, `charge.dispute.created` to live endpoint
   `we_1TPPD4HK3YN42tFlJK8mQ6LS` via `.deploy-logs/pass7_fix_webhook_events.py
   --apply`. The signing secret is not rotated by an enabled-events update, so
   Railway's `STRIPE_WEBHOOK_SECRET` stayed valid. Pre-flight then re-ran 4/4
   GREEN.

3. **Found and fixed a second Pass 7 blocker the pre-flight missed.** The topup
   button on production returned "Wallet top up product is not configured. Set
   STRIPE_WALLET_TOPUP_PRODUCT_ID in the environment." `create_topup_session`
   (`billing/checkout.py:269`) builds the Checkout line item from a Stripe
   Product id read out of `STRIPE_WALLET_TOPUP_PRODUCT_ID`. That product was
   created only in the Stripe sandbox during Pass 2 and the env var was never
   set in Railway production. Fix: created the live product with
   `.deploy-logs/pass7_create_live_topup_product.py --apply` (result
   `prod_UZ2ZIctuj6GIaf`, tax code `txcd_10000000`, idempotent metadata tag),
   set `STRIPE_WALLET_TOPUP_PRODUCT_ID` in Railway production via the CLI, which
   triggered a redeploy (BUILDING then SUCCESS in about 35 seconds).

4. **Ran Pass 7 live.** Leo did the $20 topup in the browser. The webhook
   credited the wallet (topup row id=80, +$20, balance to $109.9895, event
   `evt_1TZuf7HK3YN42tFleue4xKDE`). Leo refunded the $22.60 charge in the Stripe
   dashboard. The wallet credit was reversed with
   `.deploy-logs/pass7_rollback_topup.py --apply` (adjustment row id=81, -$20,
   balance to $89.9895; `SUM(amount_usd) = balance_usd` verified PASS).

5. **Committed the Pass 7 toolkit.** The 7 `.deploy-logs/pass7_*.py` scripts
   were all untracked; committed as `acfd746` (not pushed).

---

## Commits this session

```
acfd746  chore(wallet): add pass 7 live Stripe operational scripts   [LOCAL ONLY, unpushed]
163f336  docs(wallet): session 11 + 12 handoffs                      [pushed]
973a7f0  fix(wallet): count hold rows for daily spend cap + display  [pushed]
```

Production runs `163f336` code (the Session 12 daily-cap fix is live) plus the
`STRIPE_WALLET_TOPUP_PRODUCT_ID` env var added this session. `acfd746` only adds
standalone `.deploy-logs/` scripts that nothing imports, so it does not need to
be deployed.

---

## Pass 7 — COMPLETE

| Step | Result |
|---|---|
| Topup button | Fixed: live product `prod_UZ2ZIctuj6GIaf` + Railway env var |
| Live $20 topup | Session `cs_live_a1ezzws...`, $22.60 charged ($20 + $2.60 Ontario HST 13%) |
| Webhook credit | Event `evt_1TZuf7HK3YN42tFleue4xKDE`, topup row id=80, balance to $109.9895 |
| Refund | $22.60 fully refunded in Stripe, May 22 11:14 EDT |
| Wallet reversal | adjustment row id=81 (-$20), balance to $89.9895, invariant PASS |

A failed MasterCard attempt preceded the successful charge and fired a
`payment_intent.payment_failed` webhook (logged, no wallet effect). That
incidentally confirms one of the newly-subscribed events delivers.

**Validated:** live Stripe Checkout topup, the `checkout.session.completed`
webhook, `top_up_wallet` credit, the ledger row, the balance update, and
`stripe_event_id` idempotency, all in live mode.

**Not covered by Pass 7:** the spend path (a real job's `hold` then settle),
auto-reload off-session PaymentIntent, dispute handling.

---

## Test user state (post-Pass-7)

- **user_id:** `03e51184-4d04-4acd-ab22-0cbd7fa08c77` (leowan7@gmail.com)
- **balance_usd:** `$89.9895` (back to the pre-Pass-7 baseline)
- **wallet_frozen:** False
- **stripe_customer_id:** `cus_UWR3IFRvQ2R2GW` — STILL SANDBOX
- **stripe_payment_method_id:** `pm_1TXNqcHK3YN42tFlNElV3diy` — STILL SANDBOX
- **auto_reload_enabled:** True (threshold $80, amount $25, monthly cap $1000)
- **daily_spend_cap_usd:** $200, now enforced (Session 12 fix, live in prod)

Ledger gained 2 rows vs the Session 12 baseline: topup id=80 (+$20) and
adjustment id=81 (-$20), net zero.

### LANDMINE: auto-reload points at sandbox Stripe ids

The live Pass 7 topup did NOT update `stripe_customer_id` or
`stripe_payment_method_id`, because the "save card for auto-reload" box was not
checked (`create_topup_session` only sets `customer_creation: always` and
`setup_future_usage: off_session` when `save_payment_method = True`). Both
fields still hold their old sandbox values.

With `auto_reload_enabled = True`, balance $89.99, and threshold $80: the first
time auto-reload fires, `create_off_session_payment_intent` will charge a
sandbox customer and PM against the live Stripe key, which Stripe rejects ("No
such customer" in live mode).

Cheap MPNN jobs (about $0.01 per the 2026-05-15 ledger rows) will not drop the
balance below $80 quickly, so this is latent rather than immediate. Resolve it
before exercising auto-reload or running enough live spend to matter. Pick one:

- Set `auto_reload_enabled = False` on the test user (simplest), or
- Do one more live topup with "save card for auto-reload" checked to populate a
  live customer + PM, then refund and roll back as in Pass 7.

---

## What's left

### Immediate next priorities

1. **Live spend-path validation (the main remaining gap).** Submit one real job
   on production against the live wallet and watch the ledger produce `hold`
   then a settle row (`hold_release` for a normal job where actual is at or
   under estimate). Confirm the balance math and that the now-fixed daily cap
   engages. Use the cheapest tool first (MPNN holds were about $0.001 to $0.01).
   Poll with `.deploy-logs/pass7_watch.py`. Resolve the auto-reload landmine
   first. Session 12 called this the "optional Pass 7 extension"; it is now the
   top item.
2. **Push `acfd746`** if you want the Pass 7 scripts on the remote. Harmless to
   the deployed app either way.

### Finding 2 follow-ups (opt-in, carried from Session 12, all still open)

1. **Precise spend display.** Holds-based "spent" is conservative (it ignores
   surplus refunds and cancelled-before-run holds). To show true spend on the
   wallet overview, net each hold against its settle row(s). About 20 lines
   across 2 query sites (`_spent_today_usd` and the `app.py` 30-day block).
2. **Race-safe daily cap.** The daily-cap check is Python-only, with a TOCTOU
   window between the preflight read and the `try_hold_for_job` insert. Close it
   with a daily-cap check inside `try_hold_for_job` under the existing
   `FOR UPDATE` lock. New migration `0020`. Low practical risk.
3. **`spent_usd_30d` SQL view bug.** `0017_wallet.sql:153` sums
   `kind = 'charge'`, the identical bug Finding 2 fixed in Python. Admin-facing,
   not load-bearing. Needs a migration.

### Observed, not investigated (carried from Session 12)

`settle_hold`'s `absorbed_variance` branch (`0017_wallet.sql`, around line 333)
inserts a row with a nonzero `amount_usd` but does not UPDATE
`user_wallets.balance_usd`. That looks like it breaks the documented
`SUM(amount_usd) = balance_usd` invariant whenever Ranomics absorbs an overrun.
Could be intentional or a real bug. Not chased.

### Observed this session (new)

- `.deploy-logs/pass7_preflight_live_stripe.py` checks the key, account
  activation, Stripe Tax, and webhook events, but NOT
  `STRIPE_WALLET_TOPUP_PRODUCT_ID` or whether the Stripe product exists. That
  gap is exactly why the missing live product slipped through to a GREEN
  pre-flight. Pass 7 is done so this is moot now; if another environment or a
  Pass 8 is set up, add a product check to the pre-flight.
- **Downloadable invoices.** The topup-complete page has no "download invoice"
  link. The topup is a one-time Checkout payment (`mode=payment`) with no
  `invoice_creation`, so Stripe issues a card receipt rather than an invoice.
  Leo confirmed the receipt email arrived and is fine with that. Optional future
  enhancement: add `invoice_creation={"enabled": True}` to `create_topup_session`
  and surface the hosted invoice PDF on the topup-complete page.

### Deferred (long-standing, from Session 11)

- `wallet_transactions.job_id` bigint vs `tool_jobs.id` uuid mismatch.
- `tool_jobs_p90` view missing (referenced in `wallet_estimates.py`).
- Credit-era copy cleanup ("1 credits" on `/jobs/<id>`, MPNN form estimate text).

---

## Corrections to earlier handoffs

- Session 12 and earlier stated `.deploy-logs/` is **gitignored**. It is NOT; it
  was merely untracked. Session 13 committed the 7 `pass7_*.py` scripts
  (`acfd746`). The `.deploy-logs/*.log` and `.txt` build logs from the Bug 8 era
  remain untracked on purpose (ephemeral artifacts).
- Session 12's test-user baseline said `stripe_customer_id` would be
  "overwritten on first live checkout." It is NOT, unless the save-card box is
  checked. After Pass 7 the field still holds the sandbox id. See the landmine
  above.

---

## Known mess to clean up

- **`acfd746` is unpushed.** Local `main` is 1 commit ahead of `origin/main`.
- **`.deploy-logs/` clutter.** About 35 untracked `.log` / `.txt` build logs
  show in `git status`. A one-line `.gitignore` rule (`.deploy-logs/*.log` and
  `.deploy-logs/*.txt`) would clear it. Not done, out of scope this session.
- **`.claude/`** is untracked (local Claude Code state). Leave it.
- **Memory.** `project_tools_hub_wallet_pivot.md` still says "Session 11 paused
  mid-Pass-7". Update it now that Pass 7 is complete.
- **This handoff is uncommitted.** Commit it (and consider it a `docs(wallet):`
  commit) along with the next session's work, as was done for Sessions 11/12.

---

## Quick reference

- **HEAD on `main`:** `acfd746` (unpushed). **`origin/main`:** `163f336`.
- **Production:** `https://tools.ranomics.com`, git-connected to `main`,
  currently running `163f336` plus the `STRIPE_WALLET_TOPUP_PRODUCT_ID` env var.
- **Test user:** `03e51184-4d04-4acd-ab22-0cbd7fa08c77` (`leowan7@gmail.com`),
  balance `$89.9895`.
- **Live Stripe account:** `acct_17ntxDHK3YN42tFl` / Ranomics Inc.
- **Live webhook:** `we_1TPPD4HK3YN42tFlJK8mQ6LS` at
  `tools.ranomics.com/webhooks/stripe`, API version `2026-03-25.dahlia`,
  subscribed to all 4 wallet-required events.
- **Live wallet topup product:** `prod_UZ2ZIctuj6GIaf` ("Ranomics Tools wallet
  top up", tax code `txcd_10000000`). Value of `STRIPE_WALLET_TOPUP_PRODUCT_ID`
  in Railway production.
- **Production env query pattern:** `railway run --service web --environment
  production -- <abs-python-path> <script>`. Note `railway run` resolves the
  command through cmd.exe, so pass an absolute backslash venv-python path
  (`C:\Users\lab\Documents\Claude_projects\tools-hub\venv\Scripts\python.exe`),
  not a forward-slash relative path.
- **Ledger model:** see the "Ledger model reference" section of the Session 12
  handoff. A job is a `hold` plus one settle row; spend lives in the `hold`.
- **Wallet test suites:** `tests/test_wallet.py`, `tests/test_wallet_api.py`,
  `tests/test_wallet_templates.py`, run with `./venv/Scripts/python.exe -m
  pytest`. Baseline 87 pass / 1 pre-existing fail
  (`test_auto_reload_triggers_when_eligible`, Stripe-not-configured).

---

## To start the next session

Read this handoff. Then:

1. Decide whether to push `acfd746`.
2. **Resolve the test-user auto-reload landmine** (disable auto-reload, or do a
   live topup with the save-card box checked). Required before any live spend
   testing that could approach the $80 threshold.
3. **Run the live spend-path validation:** one cheap job on production, watch
   `hold` then settle with `pass7_watch.py`, confirm balance math and daily-cap
   enforcement. This closes the last live-validation gap for the wallet pivot.
4. Otherwise, pick from the opt-in Finding 2 follow-ups or the deferred list.
