-- ---------------------------------------------------------------------------
-- 0019_try_hold_for_job_hardcap.sql
-- ---------------------------------------------------------------------------
--
-- Adds p_hard_cap_usd parameter to public.try_hold_for_job to match what
-- shared/wallet.py:reserve_hold has been calling since Wave 1.
--
-- The 0017_wallet.sql definition shipped with 4 parameters
-- (p_user_id, p_amount_usd, p_tool_slug, p_job_id). Python has always
-- called it with 5 (also p_hard_cap_usd, derived from
-- wallet_estimates.compute_hard_cap). The mismatch made every tool
-- submit silently fail at hold time, kicking the user back to the
-- top up gate even when the wallet had ample balance.
--
-- Found during Stripe sandbox Pass 6 Step 6 (Session 8 / 2026-05-14):
-- $25 wallet, $0.02 estimate, submit MPNN, gate fires. Railway logs
-- show PGRST202 "Could not find the function public.try_hold_for_job(
-- p_amount_usd, p_hard_cap_usd, p_job_id, p_tool_slug, p_user_id)".
--
-- The new function enforces the hard cap at SQL level as defense in
-- depth. wallet_preflight already checks the cap before calling
-- reserve_hold, but having both layers means a params-changed race
-- between preflight and hold cannot bypass the ceiling.
--
-- Apply via the Supabase SQL editor on the prod project (paste the
-- whole file; runs in a single transaction).

DROP FUNCTION IF EXISTS public.try_hold_for_job(uuid, numeric, text, bigint);

CREATE OR REPLACE FUNCTION public.try_hold_for_job(
    p_user_id      uuid,
    p_amount_usd   numeric,
    p_tool_slug    text,
    p_job_id       bigint,
    p_hard_cap_usd numeric DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_balance numeric;
    v_tx_id   bigint;
    v_frozen  boolean;
BEGIN
    -- Defense in depth: if the parameter scaled hard cap is supplied,
    -- the estimated hold cannot exceed it. wallet_preflight checks
    -- this too, but a params change between preflight and hold could
    -- otherwise slip through.
    IF p_hard_cap_usd IS NOT NULL AND p_amount_usd > p_hard_cap_usd THEN
        RETURN NULL;
    END IF;

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

GRANT EXECUTE ON FUNCTION public.try_hold_for_job(uuid, numeric, text, bigint, numeric)
    TO service_role;
