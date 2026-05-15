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

---

## Session 10 update — Pass 6 closeout (2026-05-15)

The original 16-step list above uses a different numbering than the handoff sequences in `HANDOFF-WALLET-PIVOT-SESSION-9.md`. This section reports against the **handoff numbering** (Step 7 = settle-to-charge happy, Step 9 = concurrent submits, Step 11 = rate-limit, Step 12 = monthly cap, Step 13 = declined card, Step 14 = dispute freeze, Step 15 = webhook sanity, Step 16 = settle variance).

Session 10 picked up the 8 remaining open steps. Production push (5 commits) landed before testing: `c1bbd0a..f3fd719` on `origin/main`.

### Step 7 — settle-to-charge happy path → GREEN

Pre-existing job `218edc15-880b-48b3-a4d5-daa1793695a8` already ran at 16:08:18 UTC against `1UBQ.cif` after the MPNN CIF→PDB fix landed on the Railway preview. Ledger sequence:

```
tx 71  hold          -$0.0241   bal 89.9759   (submit, estimate)
tx 72  hold_release  +$0.0136   bal 89.9895   (settle — surplus released)
```

Job status `succeeded`, runtime 6 GPU seconds, 5 ProteinMPNN sequences returned. Cached balance $89.9895 = ledger sum (✓ invariant holds). Net effective spend $0.0105.

**Note on the handoff's acceptance criterion:** the handoff line "wallet_transactions gets a `charge` row (not a `hold_release`)" is incorrect for the happy-path-with-surplus case. Per `supabase/migrations/0017_wallet.sql:296-307`, the SQL emits `hold_release` when `v_diff > 0` (estimate exceeded actual). `charge` only emits on `v_diff < 0` (actual exceeded estimate). The implementation is correct; the spec wording in the handoff conflated the two branches.

### Step 9 — concurrent submit race → GREEN

Tested by firing two parallel `try_hold_for_job` RPCs from a `ThreadPoolExecutor` with separate `supabase` clients per thread. Both calls hit the SQL function within the same DB tick.

```
T1 → tx 73   balance_after = 89.9654   (at 16:54:09.565)
T2 → tx 74   balance_after = 89.9413   (at 16:54:09.718)
```

Sequential balances (89.9895 → 89.9654 → 89.9413) confirm `FOR UPDATE` on `user_wallets` row in `try_hold_for_job` serialized the two calls. Lock-released gap was 153 ms. Both holds were then released cleanly (tx 75, 76 hold_release rows), balance restored to $89.9895.

Atomicity invariant: neither call read the same stale balance. No double-spend. No orphan holds.

### Step 11 — auto-reload rate-limit → GREEN

Procedure: temporarily raised `auto_reload_threshold_usd` to $100 (above the live $89.9895 balance) so `auto_reload_if_needed` reaches the rate-limit check. With tx 70 (yesterday's auto_reload) still within 24 h, `_auto_reload_count_24h` returned 1.

`auto_reload_if_needed(USER)` returned `'rate_limited'`. No new auto_reload tx was created. Threshold restored to $80.

### Step 12 — monthly cap → GREEN

Procedure: backdated tx 70's `created_at` to 25 h ago (drops `_auto_reload_count_24h` to 0 while preserving `_auto_reload_total_month` at $25), raised threshold to $100, set `auto_reload_monthly_cap_usd = 30`.

`auto_reload_if_needed(USER)` returned `'monthly_cap'` (because $25 month_total + $25 reload_amount > $30 cap). No new auto_reload tx. All state restored: tx 70 ts back to 16:02:04, threshold back to $80, cap back to default $1000.

### Step 13 — declined card auto-disable → GREEN (both criteria)

Procedure: created Stripe PM from `tok_chargeCustomerFail` (test card 4000000000000341, attaches fine, declines on off-session charge), attached to `cus_UWR3IFRvQ2R2GW`, swapped `stripe_payment_method_id` on the wallet, backdated tx 70, raised threshold.

`auto_reload_if_needed(USER)` returned `'stripe_error'` after Stripe SDK raised `CardError: Your card was declined`. PI request was `req_KRQTDPhdZ9WN52`.

Stripe fired `payment_intent.payment_failed` event `evt_3TXP6GHK3YN42tFl0oD2xLrg`. Our webhook handler at `webhooks/stripe.py:456` flipped `auto_reload_enabled` to `False` **within 2 seconds** of the dispatch. State restored after test: PM back to `pm_1TXNqcHK3YN42tFlNElV3diy`, declined PM detached, enabled=True, threshold=$80, tx 70 ts restored.

### Step 14 — dispute → freeze → GREEN with FINDING

Procedure: called `freeze_wallet_on_dispute(USER, 'dp_pass6_step14_synthetic')` directly (end-to-end webhook path with a real Stripe dispute would require the sandbox dashboard or Stripe CLI; Stripe REST API does not expose `disputes.create` even in test mode).

Results:
- `wallet_frozen` flipped to `True` ✓
- `wallet_frozen_reason` recorded as `chargeback_dispute:dp_pass6_step14_synthetic` ✓
- `wallet_preflight(USER, 'mpnn', $0.0241, ...)` returned `allow=False, reason='wallet_frozen'` ✓
- `create_topup_session(USER, ..., $25.00)` **created a Stripe Checkout Session successfully** (URL `cs_test_a14NOoz08g15BiXrgQAJVKctPbq1o2igidxTNGjbju9Cv0YjZVABaBRX2a`, expired after test) ✗

**FINDING (Step 14):** `create_topup_session` and the `/account/wallet/checkout` route lack a `wallet_frozen` guard. The handoff's Step 14 spec line ("Attempt a topup — same") expects topups to bounce when the wallet is frozen, but the current code allows topups to proceed against a frozen wallet. `wallet_preflight` correctly blocks tool submits; the equivalent guard is missing on the topup checkout path. Recommendation: add a `wallet_frozen` check at the start of `wallet_checkout` (`app.py:1818`) or inside `create_topup_session` (`billing/checkout.py:205`) that redirects back to `/account/wallet/topup?wallet_frozen=1` with an explanatory message.

End-to-end webhook path (real Stripe `charge.dispute.created` → handler → freeze) was not exercised live. Verified by code review at `webhooks/stripe.py:474` (`_apply_charge_dispute_created`) — the handler retrieves the charge, reads `metadata.user_id`, and calls `freeze_wallet_on_dispute` which we just verified end-to-end.

### Step 15 — webhook dashboard sanity → GREEN

Cross-referenced `stripe_events` table against Stripe API event list, 2 h window:

| Source | Count | Notes |
|---|---|---|
| `stripe_events` rows (last 2 h) | 6 | All have `processed_at` set |
| Stripe API events of subscribed types (last 2 h) | 6 | Same 6 IDs |
| Missing in DB | 0 | |
| Extra in DB | 0 | |

Endpoint `we_1TX2y1HK3YN42tFlo7xswCTl` subscribed events: `charge.dispute.created`, `checkout.session.completed`, `payment_intent.payment_failed`, `payment_intent.succeeded`. `payment_intent.created` and `checkout.session.expired` are NOT subscribed and correctly absent from `stripe_events`. Idempotency persistence is working as designed.

### Step 16 — settle variance → GREEN (16a live) + 16b code-review verified

The handoff line "absorbed_variance ledger row (kind=`absorbed_variance`) appears when actual < estimate (we eat the slack)" has the semantics inverted vs the actual SQL. Per `supabase/migrations/0017_wallet.sql:296-360`, the four settle branches are:

| Branch | Condition | Emits |
|---|---|---|
| Surplus | `v_diff > 0` (actual < estimate) | `hold_release`, balance restored |
| Overage covered | `v_diff < 0` AND wallet covers | `charge`, balance decremented by variance |
| Overage absorbed | `v_diff < 0` AND wallet cannot cover | `absorbed_variance`, balance unchanged (Ranomics eats it) |
| Exact | `v_diff = 0` | zero-amount `charge` marker |

**Sub-test 16a (overage covered → `charge`):** live test.
- `try_hold_for_job(USER, $0.001, 'mpnn', None, $0.05)` → tx 77 hold of -$0.001
- `settle_hold(p_hold_tx_id=77, p_actual_usd=$0.01, p_hard_cap_usd=$0.05, p_gpu_seconds=30, p_gpu_class='A100')` → tx 78 `charge` row, `amount_usd=-$0.009`, `balance_after_usd=$89.9795`, `parent_tx_id=77`, notes="true-up variance debit"
- Cached balance updated to $89.9795 (✓)
- Test reversal: adjustment row tx 79 +$0.01, balance restored to $89.9895

**Sub-test 16b (overage absorbed → `absorbed_variance`):** code-review verified only. Live test would require draining the wallet below $0.01 to force `v_balance + v_diff < 0`, which is intrusive on the active test wallet. SQL branch at `0017_wallet.sql:332-344` is structurally identical to the verified `charge` branch (same `SELECT ... FROM wallet_transactions WHERE id = p_hold_tx_id` pattern, different `kind` literal and different `balance_after_usd` source). Branch logic is verified by reading; future spike when a fresh test user is available.

**`daily_spend_cap_usd` enforcement** referenced in the handoff is also not exercised here; the column is on `user_wallets` but no reference appears in `settle_hold` or `wallet_preflight`. Possible separate finding (cap not currently enforced) worth its own audit.

## Findings summary

1. **Topup path missing `wallet_frozen` guard** (Step 14). `create_topup_session` proceeds to create Stripe Checkout sessions even when `user_wallets.wallet_frozen = True`. Recommend adding an explicit check + redirect.
2. **`daily_spend_cap_usd` may not be enforced**. Column exists on `user_wallets`, no reference in the wallet helpers. Audit needed to confirm whether this is a missing wiring or intentional deferral.
3. **Carry-over (from Session 9 handoff, not addressed):**
   - `wallet_transactions.job_id` is `bigint` while `tool_jobs.id` is `uuid`. Every hold/release/charge row has `job_id = NULL`. Ledger-to-job linkage runs through `tool_slug` + `parent_tx_id` + timing, not by ID.
   - `tool_jobs_p90` view referenced by `shared/wallet_estimates.py:290` does not exist. `wallet_estimates` falls back gracefully but expected the view to be present.
   - `/jobs/<id>` page still says "1 credits" — credit-era copy that survived the wallet pivot.

## Wallet state at start vs end of session

| | Pre-session 10 | Post-session 10 |
|---|---|---|
| `balance_usd` | 89.9895 | 89.9895 |
| `auto_reload_enabled` | True | True |
| `auto_reload_threshold_usd` | 80.0 | 80.0 |
| `auto_reload_amount_usd` | 25.0 | 25.0 |
| `auto_reload_monthly_cap_usd` | 1000.0 | 1000.0 |
| `wallet_frozen` | False | False |
| `stripe_customer_id` | cus_UWR3IFRvQ2R2GW | cus_UWR3IFRvQ2R2GW |
| `stripe_payment_method_id` | pm_1TXNqcHK3YN42tFlNElV3diy | pm_1TXNqcHK3YN42tFlNElV3diy |
| `last_tx_id` | 72 | 79 |

Net new ledger rows from Session 10 testing:
- tx 73, 74 hold (Step 9 atomicity test)
- tx 75, 76 hold_release (Step 9 cleanup)
- tx 77 hold (Step 16a)
- tx 78 charge (Step 16a settle)
- tx 79 adjustment +$0.01 (Step 16a reversal)

Cached balance and ledger sum both equal $89.9895 (✓ invariant holds).

## Pass 6 final tally

| Step | Status | Source |
|---|---|---|
| 1, 2 | GREEN | Session 8 |
| 3, 4, 5 | GREEN | Session 8 |
| 6 (wallet side) | GREEN | Session 9 |
| 7 | GREEN | Session 10 |
| 8 (force-fail) | GREEN | Session 9 |
| 9 | GREEN | Session 10 |
| 10 (auto-reload trigger) | GREEN | Session 9 |
| 11 | GREEN | Session 10 |
| 12 | GREEN | Session 10 |
| 13 | GREEN | Session 10 |
| 14 | GREEN with FINDING (topup guard gap) | Session 10 |
| 15 | GREEN | Session 10 |
| 16a | GREEN | Session 10 |
| 16b | code-review verified | Session 10 |

**Pass 6 complete.** Two follow-up items: topup frozen-guard fix and `daily_spend_cap_usd` audit. Pass 7 (Stripe live production smoke) is next per Session 9 handoff.
