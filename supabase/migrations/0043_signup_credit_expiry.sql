-- Ranomics tools-hub — signup credit expires 30 days after it is granted.
-- Safe to re-run (idempotent).
--
-- What this adds
--   * wallet_tx_kind value 'signup_credit_expiry' for the debit row.
--   * user_wallets.signup_credit_expires_at: DEFAULT now() + 30 days.
--     New wallets get creation time + 30 days; the signup credit is granted
--     in the same request that creates the wallet
--     (shared/wallet.py _create_wallet_with_signup_credit). Existing wallets
--     get the time THIS MIGRATION RUNS + 30 days (a non-volatile default is
--     evaluated once at ALTER time), so nobody loses credit on deploy day.
--     The 30 must match shared/wallet.py SIGNUP_CREDIT_EXPIRY_DAYS
--     (tests/test_signup_credit_expiry.py checks it).
--   * user_wallets.signup_credit_expired_at: set once the expiry has been
--     processed, whatever amount it removed (including zero).
--   * user_wallets.signup_credit_reminder_sent_at: claim column for the
--     one-time "credit expires soon" email.
--   * expire_signup_credit(): writes the debit under the wallet row lock.
--     Python (shared/wallet.py expire_signup_credit) computes the amount from
--     a ledger snapshot and passes the id of the newest row it saw; this
--     function refuses unless the ledger still ends at that row, so a hold,
--     settle or top-up that landed in between makes it skip (retry next run).
--     Every other ledger writer takes the same lock before inserting
--     (0018 credit_wallet / release_hold, 0020 settle_hold, 0035
--     try_hold_for_job), so under the lock max(id) covers every committed row.
--
-- Apply via the Supabase SQL editor or `supabase db push` IMMEDIATELY BEFORE
-- deploying the matching app change. The existing-user grace period is
-- counted from when this runs, not from the deploy.

ALTER TYPE public.wallet_tx_kind ADD VALUE IF NOT EXISTS 'signup_credit_expiry';

ALTER TABLE public.user_wallets
    ADD COLUMN IF NOT EXISTS signup_credit_expires_at timestamptz
        NOT NULL DEFAULT (now() + interval '30 days'),
    ADD COLUMN IF NOT EXISTS signup_credit_expired_at timestamptz,
    ADD COLUMN IF NOT EXISTS signup_credit_reminder_sent_at timestamptz;

CREATE OR REPLACE FUNCTION public.expire_signup_credit(
    p_user_id          uuid,
    p_amount_usd       numeric,
    p_seen_last_tx_id  bigint
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_expires  timestamptz;
    v_expired  timestamptz;
    v_last     bigint;
    v_grant    numeric;
    v_balance  numeric;
BEGIN
    IF p_amount_usd IS NULL OR p_amount_usd < 0 THEN
        RAISE EXCEPTION 'expire_signup_credit: p_amount_usd must be >= 0, got %', p_amount_usd
            USING ERRCODE = '22023';
    END IF;

    SELECT signup_credit_expires_at, signup_credit_expired_at
      INTO v_expires, v_expired
      FROM public.user_wallets
     WHERE user_id = p_user_id
       FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'no_wallet';
    END IF;
    IF v_expired IS NOT NULL THEN
        RETURN 'already_expired';
    END IF;
    IF v_expires > now() THEN
        RETURN 'not_due';
    END IF;

    SELECT max(id) INTO v_last
      FROM public.wallet_transactions
     WHERE user_id = p_user_id;
    IF v_last IS DISTINCT FROM p_seen_last_tx_id THEN
        RETURN 'ledger_moved';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM public.wallet_transactions h
         WHERE h.user_id = p_user_id
           AND h.kind = 'hold'
           AND NOT EXISTS (
               SELECT 1 FROM public.wallet_transactions c
                WHERE c.parent_tx_id = h.id
           )
    ) THEN
        RETURN 'hold_open';
    END IF;

    SELECT COALESCE(SUM(amount_usd), 0) INTO v_grant
      FROM public.wallet_transactions
     WHERE user_id = p_user_id
       AND kind = 'signup_credit';
    SELECT COALESCE(SUM(amount_usd), 0) INTO v_balance
      FROM public.wallet_transactions
     WHERE user_id = p_user_id;
    IF p_amount_usd > v_grant OR p_amount_usd > v_balance THEN
        RETURN 'amount_too_large';
    END IF;

    IF p_amount_usd > 0 THEN
        INSERT INTO public.wallet_transactions
            (user_id, kind, amount_usd, balance_after_usd,
             stripe_event_id, notes)
        VALUES
            (p_user_id, 'signup_credit_expiry', -p_amount_usd,
             v_balance - p_amount_usd,
             'signup_credit_expiry:' || p_user_id::text,
             'unspent signup credit expired');
        UPDATE public.user_wallets
           SET balance_usd = v_balance - p_amount_usd
         WHERE user_id = p_user_id;
    END IF;

    UPDATE public.user_wallets
       SET signup_credit_expired_at = now()
     WHERE user_id = p_user_id;
    RETURN 'expired';
END $$;

REVOKE ALL ON FUNCTION public.expire_signup_credit(uuid, numeric, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.expire_signup_credit(uuid, numeric, bigint) FROM anon;
REVOKE ALL ON FUNCTION public.expire_signup_credit(uuid, numeric, bigint) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.expire_signup_credit(uuid, numeric, bigint) TO service_role;
