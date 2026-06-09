# Tools-Hub Wallet Pivot, Session 14 Handoff

**Date:** 2026-05-22
**Supersedes:** `HANDOFF-WALLET-PIVOT-SESSION-13.md`
**Authoritative plan:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md`

---

## TL;DR

Session 13 closed Pass 7 (live Stripe topup). Session 14 hardened the
wallet ledger: it fixed the auto-reload "landmine" at the code level,
unified four divergent spend calculations into one formula, and wrote
migration `0020` correcting two SQL bugs in the money path.

All changes are code + one migration. The wallet test suite is green
(139 pass, up from 133 pass / 1 fail — the pre-existing failure is
fixed and 5 tests were added).

**One material finding:** the GPU job-submit path still runs the
**legacy credits system in parallel with the wallet**. `requires_wallet`
places a wallet hold, but the handler also runs the old
`credits_cost` gate and `record_spend`. Fully retiring credits is a
distinct workstream — see "The credits/wallet parallel-billing gap".

**Nothing is committed or pushed yet.** See "Git state".

---

## What Session 14 did

### 1. Auto-reload failure handling (the landmine, fixed in code)

The Session 13 landmine was framed as a data problem (the test user's
sandbox Stripe ids). The deeper bug is in code: `auto_reload_if_needed`
caught an off-session charge failure, logged it, returned
`"stripe_error"`, and did **nothing else** — so a bad or unusable card
made auto-reload retry and fail on *every* job settle, forever, with no
user notification. The `payment_intent.payment_failed` webhook does not
cover it: a "No such customer" error is raised synchronously by
`PaymentIntent.create`, so no PaymentIntent and no webhook ever exist.

Fix:

- `billing/checkout.py` — new `OffSessionChargeError(RuntimeError)`
  carrying `retryable` and `reason`. `_classify_off_session_error`
  sorts Stripe failures by class name: `CardError` /
  `InvalidRequestError` are permanent (bad card / bad customer);
  `APIConnectionError`, `RateLimitError`, `AuthenticationError`,
  `APIError` are retryable (Stripe outage, our key). A missing API key
  is also retryable. `create_off_session_payment_intent` now raises
  `OffSessionChargeError` instead of a bare `RuntimeError`.
- `shared/wallet.py` — `auto_reload_if_needed` now reacts to the
  failure. On a permanent failure it disables `auto_reload_enabled` and
  emails the user (`send_auto_reload_failed_email`) so they can fix the
  card and re-enable. On a retryable failure it leaves auto-reload on
  for the next settle. It also disables auto-reload when the wallet has
  a payment method but no Stripe customer (previously only the
  payment-method field was checked).

Effect on the test-user landmine: when auto-reload first fires for the
test user, the sandbox customer is rejected, `auto_reload_if_needed`
disables auto-reload and emails once, and stops. Blast radius went from
"infinite silent failure" to "one failed attempt, then self-healed".

### 2. One canonical net-spend formula

Spend was computed four different ways: `_spent_today_usd` summed
holds; the `app.py` 30-day block summed holds; `wallet_funnel` summed
`charge` + `absorbed_variance`; the `wallet_30d_spend` SQL view summed
`charge`. The funnel one meant the sales alerts at $1k / $5k / $10k of
30-day spend **never fired** — job spend lands in `hold` rows, not
`charge` rows.

`shared/wallet.py` now has one `_net_spend_usd(user_id, since)`:

    net spend = sum(|hold|) - sum(|hold_release|) + sum(|charge|)

excluding `absorbed_variance` (Ranomics paid that, not the user).
Absolute values are used so it is correct regardless of row sign
convention. `_spent_today_usd`, the `app.py` `wallet_overview` 30-day
block, and `wallet_funnel._wallet_30d_spend_usd` all delegate to it.

### 3. Migration 0020 (`supabase/migrations/0020_wallet_corrections.sql`)

Four corrections, all `CREATE OR REPLACE` / idempotent, one transaction:

- **`try_hold_for_job`** now enforces the daily spend cap under the
  same row lock as the balance check. The cap was Python-only
  (`wallet_preflight`), leaving a TOCTOU window where concurrent
  submits could collectively step past it.
- **`settle_hold`** is now idempotent — a replay guard (matching
  `release_hold`'s pattern) so a retried completion webhook cannot
  write a second set of settle rows and double-charge. The 0017
  version had no guard ("idempotency is the caller's responsibility"),
  and the caller never enforced it. This bug was masked because the
  test fake reimplements settle idempotently.
- **`settle_hold` absorbed_variance** now writes the row with
  `amount_usd = 0` instead of a nonzero negative. The old row broke
  the `SUM(amount_usd) = balance_usd` invariant (it lowered the sum
  without moving the balance). The absorbed magnitude is preserved in
  `estimated_cost_usd` and the notes.
- **`wallet_30d_spend` view** rewritten to the canonical net-spend
  formula. Column names are unchanged so it is an in-place
  `CREATE OR REPLACE`.

Also adds the **`tool_jobs_p90`** view that
`shared/wallet_estimates.py` has always queried but no migration ever
created — until now the estimator silently fell back to the per-tool
spec defaults instead of using run history.

### 4. Tests

`tests/test_wallet.py` and `tests/test_wallet_funnel.py` updated for
the new behavior; 5 tests added (net-spend formula, auto-reload
disable-on-permanent-failure, stay-enabled-on-retryable, no-customer
disable, Stripe-error classification). Suite: **139 pass**, up from the
133 pass / 1 fail baseline (the pre-existing
`test_auto_reload_triggers_when_eligible` failure is fixed).

---

## Files changed (uncommitted)

```
billing/checkout.py                          OffSessionChargeError + classify
shared/wallet.py                             auto-reload fix + _net_spend_usd
shared/wallet_funnel.py                      delegate to _net_spend_usd
app.py                                       wallet_overview 30d -> _net_spend_usd
supabase/migrations/0020_wallet_corrections.sql   NEW
tests/test_wallet.py                         rewrites + 5 new tests
tests/test_wallet_funnel.py                  fixture + helper fixed
docs/HANDOFF-WALLET-PIVOT-SESSION-13.md      NEW (untracked from Session 13)
docs/HANDOFF-WALLET-PIVOT-SESSION-14.md      NEW (this file)
```

---

## The credits/wallet parallel-billing gap (the real "not finished" item)

The GPU job-submit route (`app.py`, `tool_submit`, decorated
`@requires_wallet` at ~line 2367) runs **both** billing systems:

- the `requires_wallet` decorator (`app.py:479`) places a wallet
  **hold** via `wallet_reserve_hold` before the handler;
- inside the handler, the **legacy credits gate** still runs:
  `if ctx.balance < preset.credits_cost` (`app.py:2449`) and
  `record_spend(...)` (`app.py:2784`).

The comment at `app.py:2421` calls this an explicit transition state.
`tool_jobs.credits_cost` is still a populated column; `shared/credits.py`
and `shared/jobs.py` still own job spend/refund in credits.

Consequence for "fully developed product": the wallet ledger, Stripe,
auto-reload, holds, and settle are built and (after this session)
correct — but a job is still also gated and debited in the old credits
system. Retiring credits is its own workstream:

- apply the wallet preflight/hold as the *only* gate on every GPU
  submit route; remove the `credits_cost` gate and `record_spend`;
- wire `settle_hold` into job completion in `shared/jobs.py` in place
  of the credits refund/spend logic;
- migrate the tool-form cost preview (`templates/components/cost_preview.html`,
  `data-credits` attrs, `templates/job_detail.html`, `jobs_list.html`)
  to USD estimates from the `/api/estimate` endpoint;
- the leftover "credits" copy then disappears as a side effect.

This was scoped this session as "credit-era copy cleanup" but is not a
copy pass — it is a billing-system cutover and needs explicit scoping
plus live testing. Left for a decision.

---

## Test user state (unchanged from Session 13)

- **user_id:** `03e51184-4d04-4acd-ab22-0cbd7fa08c77` (leowan7@gmail.com)
- **balance_usd:** `$89.9895`
- **auto_reload_enabled:** True; **stripe_customer_id / payment_method_id:**
  still SANDBOX ids.

The Session 14 auto-reload fix means the landmine now self-heals (first
fire disables auto-reload + emails once). Still cleanest to resolve the
row directly before the live spend test so no spurious "auto-reload
failed" email goes out: set `auto_reload_enabled = False` on this row,
**or** do one live topup with the "save card" box checked.

---

## Git state

- HEAD on `main`: `acfd746` (**unpushed** — carried from Session 13).
- `origin/main`: `163f336`.
- Everything in "Files changed" above is uncommitted/untracked.
- Nothing in this session was committed or pushed; awaiting a decision.

---

## What's left

### Needs live access (cannot be done from code)

1. **Apply migration 0020** to the prod Supabase project
   (`wjlhbxfnihboqebdvnns`) via the SQL editor. One transaction,
   idempotent, safe to re-run. After applying, the daily cap is
   race-safe, settle is idempotent, the invariant holds, and the
   estimator can use run history.
2. **Resolve the test-user auto-reload row** (see "Test user state").
3. **Live spend-path validation** — Session 13's top priority, still
   open. Submit one cheap job (MPNN) on production, watch the ledger
   produce `hold` then a settle row with `scripts/deploy/pass7_watch.py`,
   confirm the balance math and that the now-race-safe daily cap
   engages. This closes the last live-validation gap for the wallet.

### Decision needed

4. **The credits -> wallet cutover** (see "The credits/wallet
   parallel-billing gap"). The wallet is correct but not yet the sole
   billing path.

### Housekeeping

5. Push `acfd746` (carried unpushed since Session 13) and commit this
   session's work.

---

## To start the next session

1. Decide on commit/push of this session's work + `acfd746`.
2. Apply migration 0020 to prod Supabase.
3. Resolve the test-user auto-reload row.
4. Run the live spend-path validation.
5. Scope the credits -> wallet cutover.
