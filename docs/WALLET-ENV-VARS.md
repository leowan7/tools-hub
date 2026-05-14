# Wallet env vars

Single source of truth for environment variables used by the wallet
pivot surface: Stripe Checkout, the webhook handler, the wallet
preflight decorator, the email senders, and the Slack alerters.

Locks the canonical names that Railway and the Stripe sandbox / live
dashboards should be configured against. Aliases listed in this doc
exist as a transition affordance and should not be relied on long term.

**Source code authority:** `billing/checkout.py`, `webhooks/stripe.py`,
`shared/email.py`, `shared/wallet.py`. If this doc disagrees with the
code, the code wins; please file an issue.

**Source plan authority:** plan lines 1592 to 1670 and
`docs/HANDOFF-WALLET-PIVOT-SESSION-7.md` section "Canonical env vars".

---

## Quick reference

### Required for any wallet flow to work

| Var | Set in |
|---|---|
| `STRIPE_SECRET_KEY` | Stripe dashboard, sandbox + live separately |
| `STRIPE_WEBHOOK_SECRET` | Stripe dashboard webhook endpoint detail page |
| `STRIPE_WALLET_TOPUP_PRODUCT_ID` | Stripe dashboard product page |
| `PUBLIC_BASE_URL` | Railway service (per environment) |
| `RESEND_API_KEY` | Resend dashboard |

### Strongly recommended

| Var | Purpose |
|---|---|
| `RESEND_FROM_TRANSACTIONAL` | Sender address on every wallet email |
| `SUPPORT_EMAIL` | Reply to address shown in error and dispute emails |
| `SLACK_SALES_WEBHOOK_URL` | Pilot qualified lead alerts to #sales |
| `SLACK_OPS_WEBHOOK_URL` | Dispute and wallet freeze alerts to #ops |

### Optional overrides for wallet policy

These have sane defaults baked into the code. Set in Railway only when
you want to deviate.

| Var | Default | Lever |
|---|---|---|
| `WALLET_MIN_TOPUP_USD` | `20` | Floor on a single top up |
| `WALLET_MAX_TOPUP_USD` | `5000` | Ceiling on a single top up |
| `WALLET_SIGNUP_CREDIT_USD` | `5` | Display value in welcome emails |
| `WALLET_DEFAULT_DAILY_CAP_USD` | `200` | Display value in setup emails |

---

## Detailed table

| Canonical name | Aliases honoured | Used by | Default if unset | Behaviour when missing |
|---|---|---|---|---|
| `STRIPE_SECRET_KEY` | none | `billing/checkout.py`, `webhooks/stripe.py` | none | Stripe calls fail; top up flow blocks; webhook signature verification fails |
| `STRIPE_WEBHOOK_SECRET` | none | `webhooks/stripe.py` | none | Every webhook event returns 400; wallet credits never apply |
| `STRIPE_WALLET_TOPUP_PRODUCT_ID` | `STRIPE_TOPUP_PRODUCT_ID` (shorthand from Wave 2 dispatch prompt) | `billing/checkout.py` | none | `create_topup_session` returns the error "Set STRIPE_WALLET_TOPUP_PRODUCT_ID in the environment." |
| `PUBLIC_BASE_URL` | `APP_BASE_URL`, `APP_URL` (read inside `billing/checkout.py:_base_url` only) | `billing/checkout.py`, `shared/email.py`, `app.py`, `cron/daily_digest.py` | `https://tools.ranomics.com` (from `shared/email.py:DEFAULT_BASE_URL`) | Stripe success and cancel URLs and every link inside every email fall back to the production domain |
| `RESEND_API_KEY` | none | `shared/email.py` (every sender) | none | Every email send is skipped silently and a `logger.info` is emitted; no exception is raised |
| `RESEND_FROM_TRANSACTIONAL` | `EMAIL_FROM` (legacy) | `shared/email.py` (every sender) | `Ranomics Tools <noreply@tools.ranomics.com>` (from `shared/email.py:DEFAULT_FROM`) | Falls back to the default from address |
| `SUPPORT_EMAIL` | none | `shared/email.py` | `support@ranomics.com` | Falls back to the default support address |
| `WALLET_MIN_TOPUP_USD` | none | `billing/checkout.py` | `20.00` (from `shared.wallet.MIN_TOPUP_USD`) | Falls back to the constant |
| `WALLET_MAX_TOPUP_USD` | none | `billing/checkout.py` | `5000.00` (constant inside `billing/checkout.py`) | Falls back to the constant |
| `WALLET_SIGNUP_CREDIT_USD` | none | `shared/email.py` (signup credit email body only) | `5` | Falls back; this is display only, the actual credit applied at signup is the constant in `shared/wallet.py:SIGNUP_CREDIT_USD` |
| `WALLET_DEFAULT_DAILY_CAP_USD` | none | `shared/email.py` (welcome email body only) | `200` | Falls back; this is display only, the actual default cap is set in `shared/wallet.py` |
| `SLACK_SALES_WEBHOOK_URL` | `WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL` (per plan Railway table) | `shared/email.py:alert_sales_slack`, `alert_sales_slack_high` | none | Slack post is skipped silently; only a `logger.info` is emitted |
| `SLACK_OPS_WEBHOOK_URL` | none | `shared/email.py:alert_ops_slack` | none | Slack post is skipped silently; only a `logger.info` is emitted |

---

## Aliases policy

Aliases exist because Wave 2 was dispatched across five parallel agents
who each chose names independently. The cross diff review at
`docs/WAVE2-REVIEW.md` section 4.7 unified them.

**Resolution order:**

1. The canonical name wins when both are set.
2. The alias is honoured when only it is set.
3. Both unset means the default kicks in.

This applies to:

* `STRIPE_WALLET_TOPUP_PRODUCT_ID` (canonical) vs `STRIPE_TOPUP_PRODUCT_ID` (alias). `billing/checkout.py:160`.
* `PUBLIC_BASE_URL` (canonical) vs `APP_BASE_URL` then `APP_URL` (aliases, only inside billing). `billing/checkout.py:146 to 148`.
* `RESEND_FROM_TRANSACTIONAL` (canonical) vs `EMAIL_FROM` (alias). `shared/email.py:852 then 855`.
* `SLACK_SALES_WEBHOOK_URL` (canonical) vs `WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL` (alias). `shared/email.py:1481 to 1484`.

**For Railway config:** populate only the canonical name. Leave the
alias slot empty. The alias slots are kept as a transition affordance
and may be removed in a future cleanup pass.

---

## Hardcoded constants that are NOT env vars

These look like configuration but are baked into source code. Changing
them requires a code change and a deploy, not a Railway edit.

| Constant | Defined in | Value | Why not an env var |
|---|---|---|---|
| `WALLET_MARKUP` | `shared/wallet.py:73` and `shared/wallet_estimates.py:44` | `Decimal("1.70")` | Pricing margin. Lives in code so the wallet model and the estimate preview cannot diverge under runtime config. Also duplicated in two modules; both must change together. |
| `MIN_TOPUP_USD` | `shared/wallet.py:76` | `Decimal("20.00")` | Default for the optional `WALLET_MIN_TOPUP_USD` override |
| `SIGNUP_CREDIT_USD` | `shared/wallet.py:85` | `Decimal("5.00")` | The wallet credit applied at signup. The optional `WALLET_SIGNUP_CREDIT_USD` env var only changes the number shown in the welcome email, not the actual credit |
| `SELF_SERVE_CEILING_USD` | `shared/wallet.py:92` | `Decimal("1000.00")` | Ceiling above which a job estimate routes to the Binder Pilot pitch instead of letting the self serve flow proceed |
| `DEFAULT_AUTO_RELOAD_MONTHLY_CAP_USD` | `shared/wallet.py:82` | `Decimal("1000.00")` | Per user policy, set per row in the wallet table |

If any of these need to become runtime configurable in the future,
they should be added to this table with their env var name, the
default constant should reference `os.environ.get(...)` consistently
across every call site (no per module duplication), and the docs
should be updated.

---

## Differences from the Session 7 handoff table

The Session 7 handoff `Canonical env vars` table was the working draft
for this doc. Reconciled against the live code:

* `WALLET_MARKUP` was listed as an env var with default `1.70`. It is
  not. It is a hardcoded constant in two modules. Moved to the
  "Hardcoded constants" section above.
* `WALLET_LOW_BALANCE_THRESHOLD_USD` was listed as an env var with
  default `5`. No such env var or matching constant exists anywhere in
  `billing/`, `webhooks/`, `shared/`, or `app.py`. Removed.
* All other rows match the live code.

---

## Quick Railway checklist for a new environment

Paste this list of variable names into Railway, fill in the values,
and ship.

```
# Required
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_WALLET_TOPUP_PRODUCT_ID=
PUBLIC_BASE_URL=
RESEND_API_KEY=

# Strongly recommended
RESEND_FROM_TRANSACTIONAL=
SUPPORT_EMAIL=
SLACK_SALES_WEBHOOK_URL=
SLACK_OPS_WEBHOOK_URL=

# Optional policy overrides; leave blank to use defaults
WALLET_MIN_TOPUP_USD=
WALLET_MAX_TOPUP_USD=
WALLET_SIGNUP_CREDIT_USD=
WALLET_DEFAULT_DAILY_CAP_USD=
```

---

## Sandbox vs live mode

Stripe sandbox and Stripe live are separate accounts. Every Stripe
keyed var has a sandbox value and a live value, scoped per account:

* `STRIPE_SECRET_KEY`: sandbox `sk_test_...`, live `sk_live_...`
* `STRIPE_WEBHOOK_SECRET`: separate `whsec_...` per webhook endpoint, per account
* `STRIPE_WALLET_TOPUP_PRODUCT_ID`: separate `prod_...` per account

Configure each Railway environment (preview, staging, production)
with one set, never mix sandbox keys against a live product.

The Slack, Resend, support email, and policy override vars can be the
same value across environments unless you want sandbox alerts to go
to a separate channel.
