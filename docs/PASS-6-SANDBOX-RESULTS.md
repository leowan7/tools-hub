# Stripe Sandbox Pass 6 — E2E Smoke Results

**Date:** 2026-05-14
**Driven by:** Leo (UI), Claude (verification)
**Plan reference:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md` line 1072
**Session:** 8 (handoff `docs/HANDOFF-WALLET-PIVOT-SESSION-7.md`)

## Environment under test

| | |
|---|---|
| Preview URL | `https://web-preview-90b3.up.railway.app` |
| Railway env | `preview` (separate from production) |
| Stripe mode | Sandbox |
| Stripe webhook endpoint | `we_1TX2y1HK3YN42tFlo7xswCTl` |
| Webhook URL | `https://web-preview-90b3.up.railway.app/webhooks/stripe` |
| Subscribed events | `checkout.session.completed`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.dispute.created` |
| RESEND_API_KEY on preview | NOT SET — email steps will log-skip |

## Pre-flight checks (done before step 1)

- [x] Preview HTTP smoke (`/`, `/signup`, `/tools`, `/pricing` → 200; `/account/wallet*` → 302; `/webhooks/stripe` GET → 405)
- [x] Webhook signature verification (Stripe CLI `trigger checkout.session.completed` returned 200, `stripe_events` row written, classifier correctly ignored unmetadated event)

## Test user

| | |
|---|---|
| Email | TODO (Leo to fill in on signup) |
| User id | TODO (verify in user_wallets after signup) |
| Stripe customer id | TODO (verify after first auto-reload save card) |

## 16-step checklist

### Step 1 — Signup creates `user_wallets` row with `balance_usd = 5.00`

- **Leo does:** Sign up at `https://web-preview-90b3.up.railway.app/signup` with a test email
- **Verify:** `user_wallets` row exists, `balance_usd = 5.00`, ledger has `kind=signup_credit` row
- **Result:** PARTIAL — wallet creation is LAZY (only triggers on first `get_or_create_wallet` call from a wallet-aware route). After signup + sign-in alone, the wallet row does NOT yet exist. Need to hit `/account/wallet` or submit a tool job to trigger.
- **Findings while running this step:**
  - **Stale UI copy on auth page:** Signup confirmation says "Account created with 10 free credits." Should say "$5 in compute credit" per the wallet pivot. Leftover pre-pivot wording.
  - **Stale homepage copy:** `/` still pitches the old workspace SKU: "Design binders against your target", "$499 per target", "7-day money-back on your first Workspace". The wallet pivot was supposed to rewrite this hero (cf. `templates/pricing.html` was rewritten; `templates/index.html` or whatever drives `/` was missed).
  - **Missing nav link:** Top nav has `All tools | My runs | Pricing | + Activate target | Campaigns | <email> | Sign out`. No `Wallet` or `Account` link to reach `/account/wallet`. The `+ Activate target` CTA still points at the dead $499 workspace flow.
  - **Stale schema reference in `wallet/overview.html`:** template reads `wallet.signup_credit_used_usd` (line 109). That column does not exist on `user_wallets` (verified via `select=*` query — actual columns: user_id, balance_usd, auto_reload_*, daily_spend_cap_usd, per_job_cap_override_usd, wallet_frozen, wallet_frozen_reason, stripe_customer_id, stripe_payment_method_id, created_at, updated_at). With Jinja default Undefined, this will raise on the overview page render for any real user. Production bug, same pattern as Fix 8 caught in topup.html — overview.html needs the same `is defined and is not none` defensive guard.
- **Test user:** `leowan7@gmail.com`, user_id `03e51184-4d04-4acd-ab22-0cbd7fa08c77`

### Step 2 — `/account/wallet/topup` form shows suggested amounts

- **Leo does:** Navigate to `/account/wallet/topup`
- **Verify (manual):** Suggested $20/$50/$200/$500/$2,500 buttons render, cost table visible, auto-reload panel below
- **Result:** PENDING
- **Notes:**

### Step 3 — Click $20 → Stripe Checkout with test card `4242 4242 4242 4242`

- **Leo does:** Click "$20" radio, click "Continue to checkout", enter test card
- **Verify (manual):** Stripe Checkout page opens, $20 USD line item present, tax line appears when US-domestic address entered
- **Result:** PENDING
- **Notes:**

### Step 4 — Tax line appears on Checkout (US-domestic address)

- **Leo does:** Enter US-domestic shipping address in Checkout
- **Verify (manual):** Tax line visible (Stripe Tax: Origin Ontario / Canada means CAD-side rules; US-domestic typically $0 with Origin Ontario unless nexus)
- **Result:** PENDING
- **Notes:**

### Step 5 — Complete payment → redirect to `/account/topup-complete?session_id=cs_...` → success page renders with new balance

- **Leo does:** Submit Checkout
- **Verify:**
  - Browser lands on `/account/topup-complete?session_id=cs_test_...`
  - Page shows the Fix 7 success card: "Top up complete", "New balance $25.00", "View wallet" / "Browse tools" CTAs
  - `user_wallets.balance_usd = 25.00`
  - `wallet_ledger` has a `kind=topup` row with `amount_usd = 20.00`, `balance_after_usd = 25.00`
  - `stripe_events` has the `checkout.session.completed` event
- **Result:** PENDING
- **Notes:**

### Step 6 — Submit MPNN job → `hold` ledger entry appears

- **Leo does:** Navigate to `/tools/mpnn`, fill form, submit
- **Verify:**
  - `wallet_ledger` has a `kind=hold` row with `amount_usd` negative (~$0.15 estimate)
  - `user_wallets.balance_usd` reflects the hold
  - `jobs` row has `wallet_hold_id` populated
- **Result:** PENDING
- **Notes:**

### Step 7 — Job completes → settle: hold replaced by `charge` + `hold_release`

- **Leo does:** Wait for MPNN job to finish on Modal (~1-2 minutes)
- **Verify:**
  - `wallet_ledger` has a `kind=charge` row with actual cost
  - `wallet_ledger` has a `kind=hold_release` row with the original hold amount
  - `user_wallets.balance_usd` = previous balance minus actual cost
  - `jobs.status = 'succeeded'`, `gpu_seconds_used` populated
- **Result:** PENDING
- **Notes:**

### Step 8 — Force-fail a job mid-run → settle still fires, partial compute charged

- **Leo does:** Start MPNN, kill it in Modal dashboard (or inject a fault)
- **Verify:**
  - `jobs.status = 'failed'` or `'cancelled'`, `failure_reason` populated
  - If `gpu_seconds_used > 0`: `wallet_ledger` has a `kind=charge` row
  - If `gpu_seconds_used = 0`: `wallet_ledger` has a `kind=hold_release` row only (no charge)
  - No balance underrun
- **Result:** PENDING
- **Notes:**

### Step 9 — 5 concurrent jobs against insufficient balance → only N fit, rest get `hold_failed`

- **Leo does:** Spam-submit 5 MPNN jobs when balance is below the sum of 5 estimates
- **Verify:**
  - Only K jobs succeed where K * estimate ≤ balance
  - Remaining (5 - K) jobs return an error with reason `insufficient_balance` (or similar)
  - `wallet_ledger` shows K hold rows, no underrun in `balance_after_usd`
- **Result:** PENDING
- **Notes:**

### Step 10 — Auto-reload ON: threshold $20, amount $50, monthly cap $200

- **Leo does:** Navigate to `/account/wallet/topup#auto-reload`, toggle ON, set threshold/amount/cap, save
- **Verify:**
  - `user_wallets.auto_reload_enabled = true`
  - `user_wallets.auto_reload_threshold_usd = 20`
  - `user_wallets.auto_reload_amount_usd = 50`
  - `user_wallets.auto_reload_monthly_cap_usd = 200`
- **Result:** PENDING
- **Notes:**

### Step 11 — Run jobs to push balance below $20 → auto-reload fires off-session, balance ~$70

- **Leo does:** Run jobs until balance drops below $20 threshold
- **Verify:**
  - `wallet_ledger` has a `kind=auto_reload` row with `amount_usd = 50.00`
  - `user_wallets.balance_usd` reflects new balance
  - `wallet_ledger.auto_reload_count_calendar_month` (or similar) incremented
  - Stripe sandbox `payment_intents` shows the off-session charge
- **Result:** PENDING
- **Notes:**

### Step 12 — Trigger another low-balance condition within 24h → auto-reload BLOCKED by rate limit

- **Leo does:** Burn balance below threshold again within 24h
- **Verify:**
  - No new `kind=auto_reload` ledger row
  - `user_wallets.auto_reload_last_attempted_at` within last 24h
  - Email sent to user about rate limit (if `RESEND_API_KEY` set; otherwise log entry only)
- **Result:** PENDING
- **Notes:**

### Step 13 — Monthly cap stops further auto-reload after $200

- **Leo does:** Either: simulate by injecting 4 auto-reloads via SQL, OR repeat over multiple days
- **Verify:**
  - After 4 auto-reloads in the calendar month, the 5th attempt is blocked
  - `user_wallets.auto_reload_spent_calendar_month` ≥ 200
  - Email/Slack alert fires
- **Result:** PENDING (simplest: SQL-inject the prior 4 reloads to fast-forward)
- **Notes:**

### Step 14 — Use declined test card `4000 0000 0000 9995` on auto-reload → `payment_intent.payment_failed` → auto_reload flips OFF + email

- **Leo does:**
  - Add `4000 0000 0000 9995` as the saved card via Billing Portal or Setup intent
  - Trigger an auto-reload condition
- **Verify:**
  - `wallet_ledger` has NO new `auto_reload` row
  - `user_wallets.auto_reload_enabled = false`
  - Stripe sandbox shows the declined `payment_intent.payment_failed` event delivered
  - Email log entry shows auto-reload failed message
- **Result:** PENDING
- **Notes:**

### Step 15 — Trigger test dispute on a completed top-up → `charge.dispute.created` → `wallet_frozen=true` → new submissions blocked

- **Leo does:** In Stripe dashboard, find the $20 charge from step 5, click "Create dispute" (sandbox feature)
- **Verify:**
  - `charge.dispute.created` event delivered to `we_1TX2y1HK3YN42tFlo7xswCTl`
  - `user_wallets.wallet_frozen = true`
  - `user_wallets.frozen_reason` populated
  - Submitting a new MPNN job returns blocked
  - Ops Slack alert fires (if `SLACK_OPS_WEBHOOK_URL` set; otherwise log entry only)
- **Result:** PENDING
- **Notes:**

### Step 16 — Stripe dashboard webhook deliveries: all events "Succeeded", no failures

- **Leo does:** Dashboard → Developers → Webhooks → `we_1TX2y1HK3YN42tFlo7xswCTl` → Recent deliveries
- **Verify (manual):**
  - Every event from steps 5, 7, 11, 14, 15 is listed
  - Status: all "Succeeded" (HTTP 200 from the preview)
  - No retries needed
- **Result:** PENDING
- **Notes:**

## Outcome

(Filled in after all 16 steps complete)

- Total green: TBD / 16
- Blockers found: TBD
- Tickets to file: TBD

---

## Verification helpers (Claude side)

Reusable queries for verifying state during the run.

### Supabase: query user_wallets

```bash
# Set USER_EMAIL once you sign up
USER_EMAIL=your-test@example.com
SK=$(grep '^SUPABASE_SERVICE_ROLE_KEY=' .env | cut -d= -f2-)
SU=$(grep '^SUPABASE_URL=' .env | cut -d= -f2-)
curl -sS "$SU/rest/v1/user_wallets?select=*&user_email=eq.$USER_EMAIL" \
  -H "apikey: $SK" -H "Authorization: Bearer $SK"
```

### Supabase: query wallet_ledger for user

```bash
USER_ID=... # from user_wallets.user_id
curl -sS "$SU/rest/v1/wallet_ledger?select=*&user_id=eq.$USER_ID&order=created_at.desc&limit=20" \
  -H "apikey: $SK" -H "Authorization: Bearer $SK"
```

### Supabase: query stripe_events

```bash
curl -sS "$SU/rest/v1/stripe_events?select=event_id,event_type,processed_at&order=processed_at.desc&limit=10" \
  -H "apikey: $SK" -H "Authorization: Bearer $SK"
```

### Railway: tail preview logs

```bash
railway.cmd logs --service web --environment preview | tail -50
```

### Stripe: list recent events

```bash
SK_STRIPE=$(grep '^STRIPE_SECRET_KEY=' .env | cut -d= -f2-)
stripe events list --api-key="$SK_STRIPE" --limit 10
```

### Stripe: check webhook endpoint stats

```bash
curl -sS "https://api.stripe.com/v1/webhook_endpoints/we_1TX2y1HK3YN42tFlo7xswCTl" -u "$SK_STRIPE:"
```
