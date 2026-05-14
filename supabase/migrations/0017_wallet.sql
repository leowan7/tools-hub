-- 0017_wallet.sql
-- USD wallet + transaction ledger with hold lifecycle.
--
-- Replaces the Workspace-per-target SKU model introduced in 0014 with a
-- continuous USD wallet billed per second of Modal compute. The 0014
-- workspaces table is left in place as a paper trail and will be dropped
-- by a follow-up migration once the new ledger has been the source of
-- truth in production for one week.
--
-- Salvage notes vs. plan filename (plan was written when 0014 was the
-- latest migration; this repo has since added 0015_signup_rejections and
-- 0016_user_profiles_and_events, so the wallet pivot migration moves to
-- 0017).
--
-- Idempotency: every CREATE statement is guarded by IF NOT EXISTS or
-- DO $$ checks; the signup-credit backfill is gated by NOT EXISTS so a
-- second run of this migration does not double-credit existing users.

-- ---------------------------------------------------------------------------
-- Enum: wallet_tx_kind
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'wallet_tx_kind') THEN
        CREATE TYPE public.wallet_tx_kind AS ENUM (
            'signup_credit',     -- one-time $5 grant on signup
            'topup',             -- user-initiated Stripe Checkout payment
            'auto_reload',       -- threshold-triggered off-session PaymentIntent
            'hold',              -- pre-authorize estimate at job submit (negative amount)
            'hold_release',      -- on completion: surplus from true-up (positive amount, links to hold)
            'charge',            -- on completion: actual debit (replaces hold in effect)
            'absorbed_variance', -- internal: actual exceeded estimate beyond hard cap (Ranomics ate the cost)
            'promo',             -- manual credit (support / marketing)
            'adjustment',        -- manual correction
            'dispute_freeze'     -- Stripe chargeback received; wallet frozen
        );
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- Table: user_wallets
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.user_wallets (
    user_id                       uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    balance_usd                   numeric(12, 4) NOT NULL DEFAULT 0,
    auto_reload_enabled           boolean NOT NULL DEFAULT false,
    auto_reload_threshold_usd     numeric(8, 2) CHECK (auto_reload_threshold_usd >= 5),
    auto_reload_amount_usd        numeric(8, 2) CHECK (auto_reload_amount_usd >= 20),
    auto_reload_monthly_cap_usd   numeric(10, 2) NOT NULL DEFAULT 1000,
    daily_spend_cap_usd           numeric(8, 2) NOT NULL DEFAULT 200 CHECK (daily_spend_cap_usd > 0),
    per_job_cap_override_usd      numeric(8, 2),
    wallet_frozen                 boolean NOT NULL DEFAULT false,
    wallet_frozen_reason          text,
    stripe_customer_id            text UNIQUE,
    stripe_payment_method_id      text,
    created_at                    timestamptz NOT NULL DEFAULT now(),
    updated_at                    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Table: wallet_transactions
-- ---------------------------------------------------------------------------
--
-- amount_usd is signed: positive values are credits, negative values are
-- debits. balance_after_usd records the wallet balance after this row is
-- applied. The invariant SUM(amount_usd) over a user's rows equals that
-- user's user_wallets.balance_usd at any point in time.

CREATE TABLE IF NOT EXISTS public.wallet_transactions (
    id                          bigserial PRIMARY KEY,
    user_id                     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kind                        public.wallet_tx_kind NOT NULL,
    amount_usd                  numeric(12, 4) NOT NULL,
    balance_after_usd           numeric(12, 4) NOT NULL,
    stripe_payment_intent_id    text UNIQUE,
    stripe_event_id             text UNIQUE,
    tool_slug                   text,
    job_id                      bigint,
    estimated_cost_usd          numeric(12, 4),
    gpu_seconds                 numeric(10, 3),
    gpu_class                   text,
    parent_tx_id                bigint REFERENCES public.wallet_transactions(id),
    failure_reason              text,
    notes                       text,
    created_at                  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wallet_tx_user_created
    ON public.wallet_transactions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS wallet_tx_kind_created
    ON public.wallet_transactions (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS wallet_tx_user_kind_created
    ON public.wallet_transactions (user_id, kind, created_at DESC);
CREATE INDEX IF NOT EXISTS wallet_tx_job
    ON public.wallet_transactions (job_id)
    WHERE job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS wallet_tx_parent
    ON public.wallet_transactions (parent_tx_id)
    WHERE parent_tx_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Table: funnel_alerts
-- ---------------------------------------------------------------------------
--
-- Append-only log of funnel events. shared/wallet_funnel.py inserts a row
-- when a user crosses a 30-day cumulative spend tier (active_project at
-- $1,000, sales_qualified at $5,000, high_value at $10,000) and reads
-- the most recent row to deduplicate; without this table, repeated calls
-- to _maybe_trigger_funnel_alerts would fire the same email or Slack
-- alert on every job settle.

CREATE TABLE IF NOT EXISTS public.funnel_alerts (
    id              bigserial PRIMARY KEY,
    user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    tier            text NOT NULL,
        -- 'active_project' | 'sales_qualified' | 'high_value'
    spent_30d_usd   numeric(12, 4) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS funnel_alerts_user_created
    ON public.funnel_alerts (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS funnel_alerts_tier_created
    ON public.funnel_alerts (tier, created_at DESC);

-- ---------------------------------------------------------------------------
-- Row-Level Security
-- ---------------------------------------------------------------------------

ALTER TABLE public.user_wallets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wallet_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.funnel_alerts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS wallet_self_read ON public.user_wallets;
CREATE POLICY wallet_self_read ON public.user_wallets
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS wallet_tx_self_read ON public.wallet_transactions;
CREATE POLICY wallet_tx_self_read ON public.wallet_transactions
    FOR SELECT USING (auth.uid() = user_id);

-- funnel_alerts: service role reads/writes only. No user-facing policies.

-- ---------------------------------------------------------------------------
-- View: 30-day cumulative spend per user (for funnel triggers)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.wallet_30d_spend
WITH (security_invoker = on) AS
SELECT user_id,
       ABS(SUM(amount_usd) FILTER (WHERE kind = 'charge'))                                  AS spent_usd_30d,
       ABS(SUM(amount_usd) FILTER (WHERE kind = 'charge' AND tool_slug ILIKE '%bindcraft%')) AS bindcraft_spent_usd_30d,
       COUNT(*) FILTER (WHERE kind = 'charge')                                              AS charges_30d,
       MAX(created_at) FILTER (WHERE kind = 'charge')                                       AS last_charge_at
FROM public.wallet_transactions
WHERE created_at > now() - interval '30 days'
GROUP BY user_id;

-- ---------------------------------------------------------------------------
-- View: 24h auto-reload count per user (for the 1-per-24h cap)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.wallet_auto_reload_24h
WITH (security_invoker = on) AS
SELECT user_id,
       COUNT(*)         AS count_24h,
       SUM(amount_usd)  AS total_usd_24h
FROM public.wallet_transactions
WHERE kind = 'auto_reload'
  AND created_at > now() - interval '24 hours'
GROUP BY user_id;

-- ---------------------------------------------------------------------------
-- View: calendar-month auto-reload total per user (for the monthly cap)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.wallet_auto_reload_month
WITH (security_invoker = on) AS
SELECT user_id,
       SUM(amount_usd) AS total_usd_month
FROM public.wallet_transactions
WHERE kind = 'auto_reload'
  AND created_at >= date_trunc('month', now())
GROUP BY user_id;

-- ---------------------------------------------------------------------------
-- Function: try_hold_for_job
-- ---------------------------------------------------------------------------
--
-- Atomic balance check plus hold insert. Locks the wallet row, verifies
-- the balance covers the estimate, inserts a negative-amount hold tx,
-- and updates the wallet balance. Returns the new hold tx id on success
-- or NULL when the wallet is frozen or the balance is insufficient.
-- Concurrent submissions serialize through the row lock so two callers
-- cannot collectively overdraw the wallet.

CREATE OR REPLACE FUNCTION public.try_hold_for_job(
    p_user_id    uuid,
    p_amount_usd numeric,
    p_tool_slug  text,
    p_job_id     bigint
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_balance numeric;
    v_tx_id   bigint;
    v_frozen  boolean;
BEGIN
    -- Lock the wallet row for the duration of this transaction.
    PERFORM 1 FROM public.user_wallets WHERE user_id = p_user_id FOR UPDATE;

    SELECT wallet_frozen INTO v_frozen
    FROM public.user_wallets
    WHERE user_id = p_user_id;

    IF v_frozen THEN
        RETURN NULL;
    END IF;

    -- Compute balance from the ledger so the check is authoritative even
    -- if the cached user_wallets.balance_usd drifts.
    SELECT COALESCE(SUM(amount_usd), 0) INTO v_balance
    FROM public.wallet_transactions
    WHERE user_id = p_user_id;

    IF v_balance < p_amount_usd THEN
        RETURN NULL;
    END IF;

    INSERT INTO public.wallet_transactions
        (user_id, kind, amount_usd, balance_after_usd,
         tool_slug, job_id, estimated_cost_usd)
    VALUES
        (p_user_id, 'hold', -p_amount_usd, v_balance - p_amount_usd,
         p_tool_slug, p_job_id, p_amount_usd)
    RETURNING id INTO v_tx_id;

    UPDATE public.user_wallets
       SET balance_usd = v_balance - p_amount_usd
     WHERE user_id = p_user_id;

    RETURN v_tx_id;
END $$;

-- ---------------------------------------------------------------------------
-- Function: settle_hold
-- ---------------------------------------------------------------------------
--
-- True-up. Links a settle row to its parent hold via parent_tx_id, then
-- either releases surplus (hold_release), debits variance up to the hard
-- cap (charge), or records absorbed_variance when the wallet cannot cover
-- the deficit. Idempotency is the caller's responsibility: a single hold
-- tx id should only be settled once. Returns the inserted charge row id
-- when one was written, otherwise NULL.

CREATE OR REPLACE FUNCTION public.settle_hold(
    p_hold_tx_id     bigint,
    p_actual_usd     numeric,
    p_hard_cap_usd   numeric,
    p_gpu_seconds    numeric,
    p_gpu_class      text,
    p_failure_reason text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_user_id       uuid;
    v_estimate      numeric;
    v_capped_actual numeric;
    v_balance       numeric;
    v_charge_tx_id  bigint;
    v_diff          numeric;
BEGIN
    SELECT user_id, estimated_cost_usd
      INTO v_user_id, v_estimate
      FROM public.wallet_transactions
     WHERE id = p_hold_tx_id;

    IF v_user_id IS NULL THEN
        -- Hold row does not exist; nothing to settle.
        RETURN NULL;
    END IF;

    PERFORM 1 FROM public.user_wallets WHERE user_id = v_user_id FOR UPDATE;

    v_capped_actual := LEAST(p_actual_usd, p_hard_cap_usd);
    -- v_diff > 0 means estimate exceeded actual (surplus to release).
    -- v_diff < 0 means actual exceeded estimate (variance to debit).
    v_diff := v_estimate - v_capped_actual;

    SELECT COALESCE(SUM(amount_usd), 0) INTO v_balance
      FROM public.wallet_transactions
     WHERE user_id = v_user_id;

    IF v_diff > 0 THEN
        -- Release surplus back to the wallet.
        INSERT INTO public.wallet_transactions
            (user_id, kind, amount_usd, balance_after_usd,
             tool_slug, job_id, gpu_seconds, gpu_class,
             parent_tx_id, failure_reason, notes)
        SELECT v_user_id, 'hold_release', v_diff, v_balance + v_diff,
               tool_slug, job_id, p_gpu_seconds, p_gpu_class,
               p_hold_tx_id, p_failure_reason, 'true-up surplus released'
          FROM public.wallet_transactions
         WHERE id = p_hold_tx_id
        RETURNING id INTO v_charge_tx_id;

        UPDATE public.user_wallets
           SET balance_usd = v_balance + v_diff
         WHERE user_id = v_user_id;

    ELSIF v_diff < 0 THEN
        -- Actual exceeded estimate. Debit the variance if the wallet
        -- can cover it; otherwise record absorbed_variance and leave
        -- the balance where it is (Ranomics eats the deficit).
        IF v_balance + v_diff >= 0 THEN
            INSERT INTO public.wallet_transactions
                (user_id, kind, amount_usd, balance_after_usd,
                 tool_slug, job_id, gpu_seconds, gpu_class,
                 parent_tx_id, failure_reason, notes)
            SELECT v_user_id, 'charge', v_diff, v_balance + v_diff,
                   tool_slug, job_id, p_gpu_seconds, p_gpu_class,
                   p_hold_tx_id, p_failure_reason, 'true-up variance debit'
              FROM public.wallet_transactions
             WHERE id = p_hold_tx_id
            RETURNING id INTO v_charge_tx_id;

            UPDATE public.user_wallets
               SET balance_usd = v_balance + v_diff
             WHERE user_id = v_user_id;
        ELSE
            INSERT INTO public.wallet_transactions
                (user_id, kind, amount_usd, balance_after_usd,
                 tool_slug, job_id, gpu_seconds, gpu_class,
                 parent_tx_id, failure_reason, notes)
            SELECT v_user_id, 'absorbed_variance', v_diff, v_balance,
                   tool_slug, job_id, p_gpu_seconds, p_gpu_class,
                   p_hold_tx_id, p_failure_reason,
                   'variance exceeded balance; absorbed by Ranomics'
              FROM public.wallet_transactions
             WHERE id = p_hold_tx_id
            RETURNING id INTO v_charge_tx_id;
        END IF;
    ELSE
        -- v_diff = 0: estimate matched actual. Still write a zero-amount
        -- charge row so the hold has a settle counterpart in the ledger.
        INSERT INTO public.wallet_transactions
            (user_id, kind, amount_usd, balance_after_usd,
             tool_slug, job_id, gpu_seconds, gpu_class,
             parent_tx_id, failure_reason, notes)
        SELECT v_user_id, 'charge', 0, v_balance,
               tool_slug, job_id, p_gpu_seconds, p_gpu_class,
               p_hold_tx_id, p_failure_reason, 'estimate matched actual'
          FROM public.wallet_transactions
         WHERE id = p_hold_tx_id
        RETURNING id INTO v_charge_tx_id;
    END IF;

    RETURN v_charge_tx_id;
END $$;

-- ---------------------------------------------------------------------------
-- Trigger: keep user_wallets.updated_at fresh
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.tg_user_wallets_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS user_wallets_updated_at ON public.user_wallets;
CREATE TRIGGER user_wallets_updated_at
    BEFORE UPDATE ON public.user_wallets
    FOR EACH ROW EXECUTE FUNCTION public.tg_user_wallets_updated_at();

-- ---------------------------------------------------------------------------
-- Backfill: $5 signup credit for every existing auth user
-- ---------------------------------------------------------------------------
--
-- The user_wallets insert uses ON CONFLICT DO NOTHING so re-running this
-- migration leaves existing wallets untouched. The wallet_transactions
-- insert is gated by NOT EXISTS on a per-user signup_credit row, which
-- gives the same idempotency guarantee for the ledger entry.

INSERT INTO public.user_wallets (user_id, balance_usd)
SELECT u.id, 5.00
  FROM auth.users u
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO public.wallet_transactions
    (user_id, kind, amount_usd, balance_after_usd, notes)
SELECT u.id, 'signup_credit', 5.00, 5.00,
       'Migration 0017 backfill: replaces Workspace SKU model with USD wallet'
  FROM auth.users u
 WHERE NOT EXISTS (
     SELECT 1
       FROM public.wallet_transactions wt
      WHERE wt.user_id = u.id
        AND wt.kind = 'signup_credit'
 );
