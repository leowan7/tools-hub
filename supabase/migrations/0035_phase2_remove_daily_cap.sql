-- 0035_phase2_remove_daily_cap.sql
--
-- Compute Campaigns Phase 2, build step 2 (see
-- docs/COMPUTE-CAMPAIGNS-PHASE-2-PLAN.md sections 2 and 4).
--
-- Two changes, both idempotent and safe to re-run:
--
--   1. Redefine try_hold_for_job to DROP the per-day spend cap block.
--      Under fund-and-drain the prepaid wallet balance is the only spend
--      ceiling: a hold is refused when balance < amount under a row lock,
--      so total spend can never exceed funded money. The daily cap only
--      rate-limited spend WITHIN already-funded balance, which is exactly
--      what metered campaign compute must not do. The frozen check, the
--      ledger-authoritative balance check, and the p_hard_cap_usd guard
--      are ALL kept (money-safety invariant 5 in the plan).
--
--   2. Widen the compute_campaigns.status CHECK to allow the new
--      'paused_insufficient_funds' state. Nothing writes it yet (that is
--      build step 4, the pause/resume state machine); this only forward
--      declares the value so the later change needs no schema migration.
--
-- Apply via the Supabase SQL editor on the prod project (paste the whole
-- file). Every statement is CREATE OR REPLACE / DROP IF EXISTS / ADD, so
-- the migration is safe to re-run.

-- ---------------------------------------------------------------------------
-- Function: try_hold_for_job  (drops the daily-cap block from 0020)
-- ---------------------------------------------------------------------------
--
-- Signature is unchanged from 0019 / 0020 (Python still calls it with five
-- args); only the body changes, so CREATE OR REPLACE is enough.

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
    -- Defense in depth: if the parameter-scaled hard cap is supplied,
    -- the estimated hold cannot exceed it.
    IF p_hard_cap_usd IS NOT NULL AND p_amount_usd > p_hard_cap_usd THEN
        RETURN NULL;
    END IF;

    -- Lock the wallet row for the duration of this transaction so the
    -- balance check below is race-safe against a burst of concurrent
    -- submissions that could otherwise each pass the check before either
    -- has placed its hold.
    PERFORM 1 FROM public.user_wallets WHERE user_id = p_user_id FOR UPDATE;

    SELECT wallet_frozen
      INTO v_frozen
      FROM public.user_wallets
     WHERE user_id = p_user_id;

    IF v_frozen THEN
        RETURN NULL;
    END IF;

    -- Compute balance from the ledger so the check is authoritative even
    -- if the cached user_wallets.balance_usd drifts. This prepaid balance
    -- is the sole spend ceiling now that the daily cap is gone.
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

-- ---------------------------------------------------------------------------
-- compute_campaigns.status: allow 'paused_insufficient_funds'
-- ---------------------------------------------------------------------------
--
-- 0034 declared the CHECK inline on the status column, so Postgres named it
-- compute_campaigns_status_check (single check on that column, no collision
-- suffix). Drop and re-add it widened. Widening an IN list cannot violate any
-- existing row, so this needs no data backfill.

ALTER TABLE public.compute_campaigns
    DROP CONSTRAINT IF EXISTS compute_campaigns_status_check;

ALTER TABLE public.compute_campaigns
    ADD CONSTRAINT compute_campaigns_status_check
    CHECK (status IN (
        'draft', 'funded', 'running', 'completing',
        'completed', 'completed_with_failures', 'failed', 'cancelled',
        'paused_insufficient_funds'
    ));
