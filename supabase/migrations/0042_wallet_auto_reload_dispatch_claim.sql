-- Ranomics tools-hub — bound auto-reload to one off-session charge per 24h.
-- Safe to re-run (idempotent).
--
-- Why
--   `shared/wallet.py` `auto_reload_if_needed` gated the off-session charge on
--   `_auto_reload_count_24h`, which counts `kind='auto_reload'` rows in
--   `wallet_transactions`. That row is written by `top_up_wallet` from the
--   `payment_intent.succeeded` handler in `webhooks/stripe.py` — i.e. only
--   AFTER Stripe settles the charge. Dispatching the PaymentIntent wrote
--   nothing, so for the length of a Stripe round trip plus webhook delivery
--   the counter still read 0.
--
--   `auto_reload_if_needed` runs on EVERY job settle (`_post_settle_hooks`),
--   and Modal job completions arrive in waves, so two settles a second apart
--   both read 0 and both fired a PaymentIntent. No concurrency required.
--   `billing/checkout.py` sends no Stripe idempotency key, so each of those
--   was a genuinely distinct charge on the customer's saved card, and
--   `top_up_wallet`'s dedup is on `stripe_event_id`, which catches webhook
--   REDELIVERY rather than two real charges.
--
--   This column moves the gate to dispatch time. The app claims it with a
--   conditional UPDATE whose WHERE filters on the PRE-update value, so
--   Postgres re-evaluates the predicate after taking the row lock and exactly
--   one of several concurrent callers wins — the same compare-and-set shape
--   as `shared/compute_campaigns.py` `_cas_transition`.
--
-- NOT NULL defaulting to the epoch, not nullable. The claim filters on
-- `auto_reload_last_dispatch_at < now() - 24h`, and `NULL < anything` is NULL,
-- so a nullable column would never match and auto-reload would never fire
-- again for any wallet that had not already reloaded. Existing rows take the
-- default and are immediately claimable, which is correct: none of them is
-- holding a dispatch.
--
-- Apply via the Supabase SQL editor or `supabase db push` BEFORE deploying the
-- matching `shared/wallet.py` change. Without the column the claim UPDATE gets
-- a PostgREST error, and the claim fails CLOSED — auto-reload stops firing for
-- everyone rather than charging anyone twice.

ALTER TABLE public.user_wallets
    ADD COLUMN IF NOT EXISTS auto_reload_last_dispatch_at timestamptz
        NOT NULL DEFAULT '1970-01-01T00:00:00Z';

COMMENT ON COLUMN public.user_wallets.auto_reload_last_dispatch_at IS
    'When an off-session auto-reload PaymentIntent was last DISPATCHED (not '
    'settled). Claimed by shared.wallet._claim_auto_reload_dispatch to bound '
    'auto-reload to one charge per rolling 24h; the wallet_transactions '
    'auto_reload count cannot do that on its own because the row lands only '
    'after the Stripe webhook.';
