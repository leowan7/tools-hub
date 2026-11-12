# Stripe 15 sandbox pass: test plan

**Branch under test:** `claude/stripe-15-todict-seam` (PR #502): stripe-python `>=15.6.2,<16.0` plus the `shared/stripe_compat.as_dict` seam.
**Modelled on:** `docs/PASS-6-SANDBOX-RESULTS.md` (2026-05-14).
**Driven by:** Leo in the UI. Leo types every test card. Claude only reads the DB and the logs.
**Status:** NOT RUN. Every Result line below is PENDING.

## Why this pass exists

On 15.x, `StripeObject` is no longer a dict. Offline proof already covers that change:

- the seam audit;
- `tests/test_stripe_raw_reads_guard.py`;
- a real-library probe under 12.5.2 and 15.6.2 (scratch `--target` installs; the shared repo venv itself has 12.5.1).

This pass checks the one thing offline proof cannot: the real Stripe API, called by the 15.x SDK, against the real wallet code.

The pinned request API version also changes:

| SDK | Pinned API version |
|---|---|
| 12.5.2 | `2025-08-27.basil` |
| 15.6.2 | `2026-08-26.dahlia` |

The pins were read off `stripe.api_version` in each scratch install.

Webhook payloads are different. They render at the **webhook endpoint's** API version, not the SDK's. Sources: https://docs.stripe.com/webhooks/versioning and https://docs.stripe.com/upgrades. The upgrades page says "This setting is independent of the API version used by your server-side SDK."

So after this upgrade:

- API responses (Session create and retrieve, PaymentIntent create and retrieve, Charge retrieve, billing portal) come back dahlia-shaped.
- Webhook events keep their current shape.

## Hard rule: do not change any webhook endpoint's API version

Do not change the API version on the sandbox endpoint `we_1TX2y1HK3YN42tFlo7xswCTl` or the live endpoint `we_1TPPD4HK3YN42tFlJK8mQ6LS`. Do not click any "Upgrade API version" prompt in Workbench either; that prompt changes the account default, and with it every unpinned endpoint.

`construct_event` on 15.6.2 does not check the event's api_version:

- In `stripe/_webhook.py:67-87` it verifies the signature, then builds the event.
- The probe fed it a signed event stamped `2025-03-31.basil`, with DeprecationWarning promoted to an error. It passed.

That means nothing on our side needs the endpoint version to move.

Record each endpoint's API version before step 1 and again after step 9. The two readings must match.

| Endpoint | API version before | API version after |
|---|---|---|
| sandbox `we_1TX2y1HK3YN42tFlo7xswCTl` | TODO | TODO |

## Environment

| | |
|---|---|
| Preview URL | `https://web-preview-90b3.up.railway.app` |
| Railway env | `preview`, service `web` |
| How preview deploys | **Not git-connected.** It deploys manually from a local folder with `railway up --service web --environment preview --detach`. Source: `docs/HANDOFF-WALLET-PIVOT-SESSION-10.md:154` and `docs/HANDOFF-WALLET-PIVOT-SESSION-11.md:144`. |
| What preview runs now | UNKNOWN. `/health` returns `{"status":"ok"}` with no `build` field. Production returns `{"build":"a0b0261e…","status":"ok"}`. So the preview build predates the build stamp and is old. (Both read with `curl` on 2026-11-11.) |
| Stripe mode | Sandbox (`sk_test_`) |
| Supabase | **Shared with production** as of May. Source: `docs/HANDOFF-WALLET-PIVOT-SESSION-10.md:145`. Not re-checked: UNVERIFIED today. If it still holds, every sandbox credit lands in a real production wallet row. Use one dedicated test account and zero it out afterwards (see Cleanup). |

### Point preview at this branch (Leo runs this; Claude does not)

The Railway CLI on this machine is linked to `Project: tools-hub / Environment: production / Service: web` (`railway status`, 2026-11-11). The `--environment preview` flag below overrides that link for this one command, so nothing needs re-linking.

```
cd <a checkout of claude/stripe-15-todict-seam at the PR head>
railway up --service web --environment preview --detach
```

`railway up` uploads the local folder as it is, so the checkout must be clean and at the PR head.

Railway installs from `requirements.txt` and there is no lock file. Preview therefore gets the newest `stripe` in `>=15.6.2,<16.0`.

After the deploy, check the build log or `railway logs --service web --environment preview` for the installed stripe version. Write it here: **stripe ____**.

### Pre-flight

- [ ] `/health` on preview returns 200.
- [ ] `/webhooks/stripe` GET returns 405.
- [ ] The preview env still has the sandbox `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` and `STRIPE_WALLET_TOPUP_PRODUCT_ID`. Check that the names are set. Do not print the values.
- [ ] The sandbox endpoint still points at `https://web-preview-90b3.up.railway.app/webhooks/stripe` and subscribes to `checkout.session.completed`, `payment_intent.succeeded`, `payment_intent.payment_failed` and `charge.dispute.created`.
- [ ] Endpoint API version recorded in the table above.
- [ ] `RESEND_API_KEY` on preview: set or not set. If it is not set, email steps only log a skip. Check the logs instead of an inbox.

## Test user

| | |
|---|---|
| Email | TODO (a dedicated test account, not Leo's main one) |
| User id | TODO |
| Starting `balance_usd` | TODO (call it B0) |

## Steps

Read each DB check from `user_wallets`, `wallet_ledger` and `stripe_events` for the test user only.

### Step 1: Top-up $20 with card 4242 4242 4242 4242, card NOT saved

- **Leo does:**
  - Go to `/account/wallet/topup`.
  - Pick $20 and leave "save card" off.
  - Continue to Checkout.
  - Pay with `4242 4242 4242 4242`, any future expiry, any CVC.
- **Expected:**
  - The browser lands on `/account/topup-complete?session_id=cs_test_…`.
  - The page shows the "Top up complete" heading (`templates/wallet/topup.html:31`) and "New balance $B0+20.00" (`templates/wallet/topup.html:41`).
  - The sandbox dashboard shows one delivery of `checkout.session.completed` to the endpoint with HTTP **200**.
  - `stripe_events` has a row for that event id.
  - `wallet_ledger` has exactly one new row: `kind=topup`, `amount_usd=20.00`, `balance_after_usd=B0+20.00`, `stripe_event_id` = that event id.
  - `user_wallets.balance_usd = B0+20.00`.
  - `stripe_payment_method_id` is unchanged.
- **This proves on 15.x:**
  - Session create.
  - Session retrieve through `_attr`, including `metadata.get` for the owner check in `blueprints/wallet.py` `topup_complete`.
  - `construct_event` plus `as_dict(event)`.
- **Result:** PENDING

### Step 2: Top-up $20 with card 4242, card SAVED for auto-reload

- **Leo does:** the same as step 1, with "save card" on.
- **Expected:**
  - The same as step 1, with balance B0+40.00 and a second `kind=topup` row of 20.00.
  - `user_wallets.stripe_customer_id` is set (`cus_…`).
  - `user_wallets.stripe_payment_method_id` is set (`pm_…`).
- **This proves:** `PaymentIntent.retrieve` plus `as_dict(pi).get("payment_method")` at `webhooks/stripe.py:213`.
- **Result:** PENDING

### Step 3: Webhook replay gives no double credit

- **Leo does:** In the sandbox dashboard, open step 2's `checkout.session.completed` event and click **Resend** to the endpoint.
- **Expected:**
  - HTTP **200** with body `{"status": "already_processed", "event_id": "evt_…"}` (`webhooks/stripe.py:592-597`).
  - No new `wallet_ledger` row.
  - `balance_usd` is still B0+40.00.
- **Result:** PENDING

### Step 4: Bad signature returns 400

- **Leo (or Claude, on Leo's OK) runs:**
  ```
  curl -s -o /dev/null -w "%{http_code}\n" -X POST \
    -H "Content-Type: application/json" -H "Stripe-Signature: t=1,v1=deadbeef" \
    -d '{"id":"evt_fake","type":"checkout.session.completed"}' \
    https://web-preview-90b3.up.railway.app/webhooks/stripe
  ```
- **Expected:**
  - Prints `400`. The body is `invalid signature` (`webhooks/stripe.py:575`).
  - No `stripe_events` row for `evt_fake`.
  - No ledger change.
- **Result:** PENDING

### Steps 5 and 6: auto-reload (run the decline first)

Auto-reload fires only from `_post_settle_hooks` after a job settles (`shared/wallet.py:1682`). Two ways to trigger it:

- **A, no GPU spend.** Claude runs `auto_reload_if_needed(<user_id>)` once from a local shell. That shell runs this branch, with the scratch stripe 15.6.2 on `PYTHONPATH` and the sandbox key that Leo loads into the env (Claude does not read it). Pass 6 used the same direct `auto_reload_if_needed` call for its rate-limit, monthly-cap and decline steps (`docs/PASS-6-SANDBOX-RESULTS.md:294-308`). The PaymentIntent create runs locally on 15.x, and the webhooks still land on preview.
- **B, the real path.** Leo runs the cheapest tool once on preview. This is GPU spend and needs Leo's per-job approval.

**Order matters.** A successful reload writes a `kind=auto_reload` ledger row, and the 24h count then refuses any further reload for a day. A decline writes no ledger row. So run the decline (step 5) first, then the success (step 6).

The off-session charge uses `user_wallets.stripe_payment_method_id`, which is the card saved at Checkout. Swapping the card in the billing portal does not change that column. So the decline card goes in by hand.

**Setup before each of steps 5 and 6.** Run this SQL on the test user only:

- `auto_reload_enabled=true`
- `auto_reload_threshold_usd=20`
- `auto_reload_amount_usd=50`
- monthly cap of at least 50
- `auto_reload_last_dispatch_at='1970-01-01T00:00:00Z'`. This is the column default from `supabase/migrations/0042_wallet_auto_reload_dispatch_claim.sql:44-45`. Never set it to NULL: NULL never matches the claim filter, so auto-reload would silently never fire.
- `balance_usd` below 20. Call it B1.

### Step 5: declined auto-reload turns auto-reload off and sends an honest notice

- **Leo does:**
  - In the sandbox dashboard, open the test user's customer (`stripe_customer_id` from step 2).
  - Add a payment method with card `4000 0000 0000 0341`. That card attaches fine and then declines when charged.
  - Note its `pm_…` id.
  - Claude records the step 2 `pm_…` id, then sets `user_wallets.stripe_payment_method_id` to the new `pm_…` by SQL.
  - Run the setup above, then trigger with A or B.
- **Expected:**
  - The call returns `"stripe_error"`.
  - The logs show `retryable=False reason=card_declined`. The log line is at `shared/wallet.py:1274-1278`, and `_classify_off_session_error` in `billing/checkout.py` maps `CardError` to non-retryable.
  - `auto_reload_enabled = false`.
  - No new `auto_reload` ledger row.
  - The balance is unchanged at B1.
- **About the email:**
  - The synchronous path sends one `send_auto_reload_failed_email` (`shared/wallet.py:1290`).
  - If the sandbox also delivers `payment_intent.payment_failed`, the webhook handler sends the same email again (`webhooks/stripe.py:491`). That double mail is existing 12.x behaviour, not something 15.x adds. Record how many emails arrive.
  - A **false** notice is a FAIL. That means a failure email for a PaymentIntent that succeeded, or `retryable=True` on this decline.
- **What "wrongly disabled" means:** auto-reload switched off by a retryable error, such as a connection error or a rate limit. The sandbox cannot produce one on demand. The probe covers it offline: all six class names in `_classify_off_session_error` exist on 15.6.2, and `CardError` classifies as non-retryable.
- **Result:** PENDING

### Step 6: auto-reload off-session PaymentIntent succeeds

- **Leo / Claude does:**
  - Set `stripe_payment_method_id` back to the step 2 `pm_…` (the 4242 card).
  - Run the setup above, then trigger with the same route as step 5.
- **Expected:**
  - The call returns `"triggered"`.
  - The sandbox shows one succeeded $50.00 PaymentIntent, with metadata `kind=auto_reload` and `user_id`.
  - The `payment_intent.succeeded` delivery returns HTTP 200.
  - One new `wallet_ledger` row: `kind=auto_reload`, `amount_usd=50.00`, `balance_after_usd=B1+50.00`.
  - `balance_usd = B1+50.00`.
  - `auto_reload_enabled` stays true.
- **Proves:**
  - `PaymentIntent.create(off_session=True, confirm=True)` on 15.x.
  - `create_off_session_payment_intent` returning `as_dict(intent)`.
  - The `payment_intent.succeeded` handler.
- **Result:** PENDING

### Step 7: Dispute card 4000 0000 0000 0259 freezes the wallet

- **Leo does:** Top up $20 as in step 1, with card `4000 0000 0000 0259`. The sandbox opens a dispute on that charge automatically.
- **Expected, first:** the step 1 top-up results, balance +20.00.
- **Expected, within a few minutes:**
  - `charge.dispute.created` is delivered with HTTP 200.
  - `user_wallets.wallet_frozen = true`.
  - `wallet_frozen_reason = chargeback_dispute:dp_…` (`shared/wallet.py:1311`).
  - Submitting any tool on preview is refused by `wallet_preflight`.
  - The ops Slack alert fires. If `SLACK_OPS_WEBHOOK_URL` is not set on preview, the log shows the skip instead.
- **This proves on 15.x:**
  - `as_dict(stripe.Charge.retrieve(...))` at `webhooks/stripe.py:529`.
  - The `PaymentIntent.retrieve` fallback at `webhooks/stripe.py:263`, if the charge metadata is bare.
  - Pass 6 never ran this end to end. It called `freeze_wallet_on_dispute` directly.
- Opening `/account/wallet/topup` while frozen redirects to the wallet overview with `?wallet_frozen=1` (`blueprints/wallet.py:917-918`; the checkout POST has the same guard at `:966-967`). Pass 6 finding 1 reported this gap; it has since been closed.
- **Result:** PENDING

### Step 8: pass7 operator scripts, sandbox key, local, 15.x

Run each script from this branch with `PYTHONPATH=<scratch>/s15;.` and the sandbox key in the env. Claude can run these once Leo has loaded the key into the env.

| Script | Expected under `sk_test_` |
|---|---|
| `pass7_preflight_live_stripe.py` | Prints `FAIL: key prefix is sk_test_ …` and exits 1 (`scripts/deploy/pass7_preflight_live_stripe.py:52-54`). This is by design: it is a live-only audit. |
| `pass7_fix_webhook_events.py` (dry run) | `FAIL: STRIPE_SECRET_KEY is not a live key` and exits 1 (`:48-50`). |
| `pass7_create_live_topup_product.py` (dry run) | Refuses the non-live key and exits (`:60`). |
| `pass7_webhook_deliveries.py` | Runs to the end. It lists up to 20 recent sandbox events with id and type, and makes no writes. |

The first three cannot exercise their Stripe calls in sandbox; each stops at its own key-prefix check. Their calls (Account and tax settings retrieve, product list with auto-paging, endpoint retrieve) are covered offline:

- `tests/test_stripe_raw_reads_guard.py`;
- the probe's `as_dict` on a real `Account` built with `construct_from`.

The only full live check is a **dry run** with the live key: `pass7_preflight_live_stripe.py`, and `pass7_fix_webhook_events.py` without `--apply`. Both only read. Running them is Leo's call; it is not part of this pass.

- **Result:** PENDING

### Step 9: Dashboard sanity

- Every delivery from steps 1-7 shows "Succeeded" (HTTP 200), except the deliberate replay, which also returns 200.
- No retries.
- The endpoint API version still matches the table at the top.
- **Result:** PENDING

## Cleanup (test user only)

- Unfreeze the wallet.
- Detach the `4000 0000 0000 0341` card from the customer.
- Set `auto_reload_enabled=false`.
- If Supabase is still shared with production, add an `adjustment` row that returns the balance to B0. Do not delete ledger rows.
- Close the sandbox dispute.

## Outcome

PENDING. Pass bar: steps 1, 2, 3, 4, 6, 7 and 9 meet every expectation; step 5 has `retryable=False`, auto-reload off, and no ledger row; step 8 matches the table.

## Follow-up, out of scope for this pass

Stripe's changelog for API version `2026-09-30.endive` (the next major after `2026-08-26.dahlia`) lists removing `payment_method_types` from Checkout Sessions and PaymentIntents. billing/checkout.py:368 sends `"payment_method_types": ["card"]` on the top-up session. It is harmless on 15.x, which pins dahlia. The stripe-python major that pins endive will need that line changed first. UNVERIFIED: read from the changelog by a research agent, not exercised.
