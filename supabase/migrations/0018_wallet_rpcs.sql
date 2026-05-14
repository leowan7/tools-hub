-- ---------------------------------------------------------------------------
-- 0018_wallet_rpcs.sql
-- ---------------------------------------------------------------------------
--
-- Adds two SQL functions that shared/wallet.py expects but that were
-- never written in 0017_wallet.sql:
--
--   credit_wallet(p_user_id, p_amount_usd, p_kind, p_stripe_event_id,
--                 p_stripe_payment_intent_id)
--   release_hold(p_hold_tx_id, p_reason)
--
-- Without these, every wallet credit operation silently fails in
-- production (signup credit, Stripe top-up confirmation, auto-reload
-- credit, dispute refund) because shared/wallet.py wraps the rpc call
-- in a try / except that logs ERROR and returns False, leaving the
-- wallet at zero balance with no ledger row.
--
-- Found during Stripe sandbox Pass 6 (Session 8 / 2026-05-14). See
-- docs/PASS-6-SANDBOX-RESULTS.md for the full repro.
--
-- Apply via the Supabase SQL editor on the prod project; pasting the
-- whole file in one go runs in a single transaction (per the carried
-- Session 7 gotcha #6).

-- ---------------------------------------------------------------------------
-- Function: credit_wallet
-- ---------------------------------------------------------------------------
--
-- Atomic credit. Locks the wallet row, recomputes balance from the
-- ledger (authoritative, same pattern as try_hold_for_job), inserts a
-- positive amount_usd ledger row, and updates user_wallets.balance_usd.
-- Returns the new tx id on success, NULL on idempotent skip.
--
-- Idempotency is enforced when p_stripe_event_id is provided. A second
-- call with the same event id returns NULL without inserting a duplicate
-- row. Without an event id (e.g., manual promo or adjustment), every
-- call inserts a new row.
--
-- p_kind must be a value of public.wallet_tx_kind that represents a
-- credit: signup_credit, topup, auto_reload, promo, adjustment, or
-- hold_release. Debits (hold, charge, dispute_freeze, absorbed_variance)
-- must go through their own paths.
--
-- Used by shared/wallet.py:
--   record_signup_credit            (kind='signup_credit')
--   top_up_wallet                   (kind='topup' or 'auto_reload')
--   any future promo or adjustment path

CREATE OR REPLACE FUNCTION public.credit_wallet(
    p_user_id                  uuid,
    p_amount_usd               numeric,
    p_kind                     text,
    p_stripe_event_id          text DEFAULT NULL,
    p_stripe_payment_intent_id text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_kind     public.wallet_tx_kind;
    v_balance  numeric;
    v_tx_id    bigint;
    v_existing bigint;
BEGIN
    -- Reject obviously invalid amounts.
    IF p_amount_usd IS NULL OR p_amount_usd <= 0 THEN
        RAISE EXCEPTION 'credit_wallet: p_amount_usd must be positive, got %', p_amount_usd
            USING ERRCODE = '22023';
    END IF;

    -- Cast the text kind to the enum so the caller cannot insert a
    -- debit by mistake. Allowed credit kinds:
    v_kind := p_kind::public.wallet_tx_kind;
    IF v_kind NOT IN (
        'signup_credit'::public.wallet_tx_kind,
        'topup'::public.wallet_tx_kind,
        'auto_reload'::public.wallet_tx_kind,
        'promo'::public.wallet_tx_kind,
        'adjustment'::public.wallet_tx_kind,
        'hold_release'::public.wallet_tx_kind
    ) THEN
        RAISE EXCEPTION 'credit_wallet: kind % is not a credit kind', v_kind
            USING ERRCODE = '22023';
    END IF;

    -- Idempotency check: if a row with this stripe_event_id already
    -- exists, no-op. Caller can detect this because the return is NULL.
    IF p_stripe_event_id IS NOT NULL THEN
        SELECT id INTO v_existing
          FROM public.wallet_transactions
         WHERE stripe_event_id = p_stripe_event_id;
        IF v_existing IS NOT NULL THEN
            RETURN NULL;
        END IF;
    END IF;

    -- Ensure a wallet row exists. The Python side does this too via
    -- get_or_create_wallet, but credit_wallet is callable directly and
    -- should never fail because the wallet row hasn't been bootstrapped.
    INSERT INTO public.user_wallets (user_id, balance_usd)
    VALUES (p_user_id, 0)
    ON CONFLICT (user_id) DO NOTHING;

    -- Lock the wallet row for the duration of this transaction.
    PERFORM 1 FROM public.user_wallets WHERE user_id = p_user_id FOR UPDATE;

    -- Authoritative balance from the ledger.
    SELECT COALESCE(SUM(amount_usd), 0) INTO v_balance
      FROM public.wallet_transactions
     WHERE user_id = p_user_id;

    INSERT INTO public.wallet_transactions
        (user_id, kind, amount_usd, balance_after_usd,
         stripe_event_id, stripe_payment_intent_id)
    VALUES
        (p_user_id, v_kind, p_amount_usd, v_balance + p_amount_usd,
         p_stripe_event_id, p_stripe_payment_intent_id)
    RETURNING id INTO v_tx_id;

    UPDATE public.user_wallets
       SET balance_usd = v_balance + p_amount_usd
     WHERE user_id = p_user_id;

    RETURN v_tx_id;
END $$;

-- ---------------------------------------------------------------------------
-- Function: release_hold
-- ---------------------------------------------------------------------------
--
-- Full release of a pre-authorized hold. Used when a job is cancelled
-- before any compute runs, when a failed job consumed zero GPU time,
-- or when the mid-run overrun monitor kills a job. Distinct from
-- settle_hold which handles the success path with a known actual cost.
--
-- Inserts a hold_release row that credits back the original hold amount
-- in full, links it via parent_tx_id, and updates the wallet balance.
--
-- Idempotency: a second call for the same hold_tx_id returns NULL
-- without inserting a duplicate release. The check is on parent_tx_id
-- existing for a hold_release or charge row; if the hold has already
-- been settled (charge or hold_release present), no-op.
--
-- Used by shared/wallet.py:release_hold, called from shared/jobs.py at
-- cancel paths and the mid-run safety kill.

CREATE OR REPLACE FUNCTION public.release_hold(
    p_hold_tx_id bigint,
    p_reason     text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_user_id     uuid;
    v_hold_amount numeric;
    v_hold_kind   public.wallet_tx_kind;
    v_existing    bigint;
    v_balance     numeric;
    v_release_tx  bigint;
BEGIN
    SELECT user_id, amount_usd, kind
      INTO v_user_id, v_hold_amount, v_hold_kind
      FROM public.wallet_transactions
     WHERE id = p_hold_tx_id;

    -- Unknown hold id: nothing to do.
    IF v_user_id IS NULL THEN
        RETURN NULL;
    END IF;

    -- Defensive: the caller might pass a non-hold tx id by mistake.
    IF v_hold_kind <> 'hold'::public.wallet_tx_kind THEN
        RAISE EXCEPTION 'release_hold: tx % is not a hold (kind=%)', p_hold_tx_id, v_hold_kind
            USING ERRCODE = '22023';
    END IF;

    -- Idempotency: a hold that has already been settled or released
    -- has a child row with parent_tx_id = hold_tx_id. Either kind
    -- (hold_release or charge) counts as settled.
    SELECT id INTO v_existing
      FROM public.wallet_transactions
     WHERE parent_tx_id = p_hold_tx_id
     LIMIT 1;
    IF v_existing IS NOT NULL THEN
        RETURN NULL;
    END IF;

    -- Lock the wallet for the credit-back step.
    PERFORM 1 FROM public.user_wallets WHERE user_id = v_user_id FOR UPDATE;

    SELECT COALESCE(SUM(amount_usd), 0) INTO v_balance
      FROM public.wallet_transactions
     WHERE user_id = v_user_id;

    -- v_hold_amount is negative (hold is a debit). The release credits
    -- back the absolute value.
    INSERT INTO public.wallet_transactions
        (user_id, kind, amount_usd, balance_after_usd,
         parent_tx_id, notes)
    VALUES
        (v_user_id, 'hold_release'::public.wallet_tx_kind,
         ABS(v_hold_amount), v_balance + ABS(v_hold_amount),
         p_hold_tx_id, p_reason)
    RETURNING id INTO v_release_tx;

    UPDATE public.user_wallets
       SET balance_usd = v_balance + ABS(v_hold_amount)
     WHERE user_id = v_user_id;

    RETURN v_release_tx;
END $$;

-- ---------------------------------------------------------------------------
-- Grant execute to service role (Python side calls these via the
-- supabase service client; RLS does not apply to RPCs marked
-- SECURITY DEFINER, but explicit grants make the surface obvious).
-- ---------------------------------------------------------------------------

GRANT EXECUTE ON FUNCTION public.credit_wallet(uuid, numeric, text, text, text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.release_hold(bigint, text)
    TO service_role;

-- ---------------------------------------------------------------------------
-- Backfill: every user_wallets row created since 0017_wallet.sql with
-- balance = 0 and no signup_credit ledger entry should get the credit
-- applied now that credit_wallet exists.
-- ---------------------------------------------------------------------------
--
-- The backfill in 0017_wallet.sql ran the INSERT INTO user_wallets and
-- the INSERT INTO wallet_transactions independently. Any user who signed
-- up AFTER 0017 was applied but BEFORE 0018 lands hit the credit_wallet
-- RPC failure path: their user_wallets row exists with balance 0 and no
-- ledger row. Backfill them here.

INSERT INTO public.wallet_transactions
    (user_id, kind, amount_usd, balance_after_usd,
     stripe_event_id, notes)
SELECT w.user_id,
       'signup_credit'::public.wallet_tx_kind,
       5.00,
       5.00,
       'signup_credit:' || w.user_id::text,
       'Migration 0018 backfill: 0017 backfill skipped users whose ' ||
       'wallet was created after 0017 ran but before credit_wallet ' ||
       'existed; apply $5 signup credit retroactively'
  FROM public.user_wallets w
 WHERE w.balance_usd = 0
   AND NOT EXISTS (
       SELECT 1
         FROM public.wallet_transactions wt
        WHERE wt.user_id = w.user_id
          AND wt.kind = 'signup_credit'
   );

UPDATE public.user_wallets w
   SET balance_usd = 5.00
 WHERE w.balance_usd = 0
   AND EXISTS (
       SELECT 1
         FROM public.wallet_transactions wt
        WHERE wt.user_id = w.user_id
          AND wt.kind = 'signup_credit'
          AND wt.stripe_event_id = 'signup_credit:' || w.user_id::text
   );
