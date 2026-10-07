-- Ranomics tools-hub: live charging, option (ii). A live run's cost comes off
-- the balance while it runs, and the run stops when the balance reaches $0.
-- Design: docs/design/LIVE-CHARGING-2026-10-01.md, "Option (ii) build plan".
-- Safe to re-run (idempotent).
--
-- One live run is one lineage under one anchor:
--   * anchor: a 'hold' row of amount 0, written by open_live_run at submit;
--   * one 'run_debit' row (new kind, negative amount) per tick that takes money,
--     written by debit_live_run with parent_tx_id = the anchor;
--   * one terminal row written by settle_live_run: a 'charge' (can be 0) when
--     the final cost is at least what the debits took, plus an
--     'absorbed_variance' at 0 when the balance did not cover it; or a
--     'hold_release' crediting back what the debits took above the final cost.
-- A lineage is settled once it has a child that is not a 'run_debit'.
--
-- Every function below takes the user_wallets row lock before reading the
-- ledger, as credit_wallet does (0018_wallet_rpcs.sql), and recomputes the
-- balance from the ledger.
--
-- The view compares kind::text, not the enum: the SQL editor and the supabase
-- CLI run this file as one transaction, and an enum value added in a
-- transaction cannot be used as an enum literal in that same transaction.
-- scripts/check_live_charging_local_pg.py applies this file to a local
-- Postgres and exercises every function.
--
-- Apply via the Supabase SQL editor IMMEDIATELY BEFORE deploying the matching
-- app change.

ALTER TYPE public.wallet_tx_kind ADD VALUE IF NOT EXISTS 'run_debit';

CREATE OR REPLACE FUNCTION public.open_live_run(
    p_user_id   uuid,
    p_tool_slug text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_frozen  boolean;
    v_balance numeric;
    v_tx_id   bigint;
BEGIN
    SELECT wallet_frozen INTO v_frozen
      FROM public.user_wallets
     WHERE user_id = p_user_id
       FOR UPDATE;
    IF NOT FOUND OR v_frozen THEN
        RETURN NULL;
    END IF;

    SELECT COALESCE(SUM(amount_usd), 0) INTO v_balance
      FROM public.wallet_transactions
     WHERE user_id = p_user_id;
    IF v_balance <= 0 THEN
        RETURN NULL;
    END IF;

    INSERT INTO public.wallet_transactions
        (user_id, kind, amount_usd, balance_after_usd,
         tool_slug, estimated_cost_usd, notes)
    VALUES
        (p_user_id, 'hold', 0, v_balance,
         p_tool_slug, 0, 'live run')
    RETURNING id INTO v_tx_id;
    RETURN v_tx_id;
END $$;

CREATE OR REPLACE FUNCTION public.debit_live_run(
    p_hold_tx_id  bigint,
    p_due_usd     numeric,
    p_gpu_seconds numeric,
    p_gpu_class   text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_hold    public.wallet_transactions%ROWTYPE;
    v_due     numeric;
    v_balance numeric;
    v_taken   numeric;
    v_step    numeric;
    v_take    numeric := 0;
BEGIN
    IF p_due_usd IS NULL OR p_due_usd < 0 THEN
        RAISE EXCEPTION 'debit_live_run: p_due_usd must be >= 0, got %', p_due_usd
            USING ERRCODE = '22023';
    END IF;
    v_due := round(p_due_usd, 4);

    SELECT * INTO v_hold
      FROM public.wallet_transactions
     WHERE id = p_hold_tx_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_hold.kind <> 'hold' OR v_hold.amount_usd <> 0 THEN
        RAISE EXCEPTION 'debit_live_run: % is not a live-run anchor', p_hold_tx_id
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1 FROM public.user_wallets WHERE user_id = v_hold.user_id FOR UPDATE;

    SELECT COALESCE(SUM(amount_usd), 0) INTO v_balance
      FROM public.wallet_transactions
     WHERE user_id = v_hold.user_id;
    SELECT COALESCE(-SUM(amount_usd), 0) INTO v_taken
      FROM public.wallet_transactions
     WHERE parent_tx_id = p_hold_tx_id
       AND kind = 'run_debit';

    IF EXISTS (
        SELECT 1 FROM public.wallet_transactions
         WHERE parent_tx_id = p_hold_tx_id
           AND kind <> 'run_debit'
    ) THEN
        RETURN jsonb_build_object(
            'settled', true, 'debited', 0, 'taken', v_taken,
            'short', 0, 'balance_after', v_balance);
    END IF;

    v_step := v_due - v_taken;
    IF v_step > 0 THEN
        v_take := GREATEST(LEAST(v_step, v_balance), 0);
    END IF;
    IF v_take > 0 THEN
        INSERT INTO public.wallet_transactions
            (user_id, kind, amount_usd, balance_after_usd,
             tool_slug, job_id, gpu_seconds, gpu_class, parent_tx_id)
        VALUES
            (v_hold.user_id, 'run_debit', -v_take, v_balance - v_take,
             v_hold.tool_slug, v_hold.job_id, p_gpu_seconds, p_gpu_class,
             p_hold_tx_id);
        v_balance := v_balance - v_take;
        UPDATE public.user_wallets
           SET balance_usd = v_balance
         WHERE user_id = v_hold.user_id;
    END IF;

    RETURN jsonb_build_object(
        'settled', false, 'debited', v_take, 'taken', v_taken + v_take,
        'short', GREATEST(v_step - v_take, 0), 'balance_after', v_balance);
END $$;

CREATE OR REPLACE FUNCTION public.settle_live_run(
    p_hold_tx_id     bigint,
    p_final_due_usd  numeric,
    p_gpu_seconds    numeric,
    p_gpu_class      text,
    p_failure_reason text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_hold     public.wallet_transactions%ROWTYPE;
    v_final    numeric;
    v_balance  numeric;
    v_taken    numeric;
    v_rest     numeric;
    v_charge   numeric := 0;
    v_released numeric := 0;
    v_absorbed numeric := 0;
BEGIN
    IF p_final_due_usd IS NULL OR p_final_due_usd < 0 THEN
        RAISE EXCEPTION 'settle_live_run: p_final_due_usd must be >= 0, got %', p_final_due_usd
            USING ERRCODE = '22023';
    END IF;
    v_final := round(p_final_due_usd, 4);

    SELECT * INTO v_hold
      FROM public.wallet_transactions
     WHERE id = p_hold_tx_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_hold.kind <> 'hold' OR v_hold.amount_usd <> 0 THEN
        RAISE EXCEPTION 'settle_live_run: % is not a live-run anchor', p_hold_tx_id
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1 FROM public.user_wallets WHERE user_id = v_hold.user_id FOR UPDATE;

    SELECT COALESCE(SUM(amount_usd), 0) INTO v_balance
      FROM public.wallet_transactions
     WHERE user_id = v_hold.user_id;
    SELECT COALESCE(-SUM(amount_usd), 0) INTO v_taken
      FROM public.wallet_transactions
     WHERE parent_tx_id = p_hold_tx_id
       AND kind = 'run_debit';

    IF EXISTS (
        SELECT 1 FROM public.wallet_transactions
         WHERE parent_tx_id = p_hold_tx_id
           AND kind <> 'run_debit'
    ) THEN
        RETURN jsonb_build_object(
            'settled_before', true, 'final', v_final, 'taken', v_taken,
            'charged', 0, 'released', 0, 'absorbed', 0,
            'balance_after', v_balance);
    END IF;

    IF v_final >= v_taken THEN
        v_rest := v_final - v_taken;
        v_charge := GREATEST(LEAST(v_rest, v_balance), 0);
        v_absorbed := v_rest - v_charge;
        INSERT INTO public.wallet_transactions
            (user_id, kind, amount_usd, balance_after_usd,
             tool_slug, job_id, gpu_seconds, gpu_class,
             parent_tx_id, failure_reason, notes)
        VALUES
            (v_hold.user_id, 'charge', -v_charge, v_balance - v_charge,
             v_hold.tool_slug, v_hold.job_id, p_gpu_seconds, p_gpu_class,
             p_hold_tx_id, p_failure_reason,
             CASE WHEN v_rest = 0
                  THEN 'live run: debits already took the final cost'
                  ELSE 'live run: rest of the final cost' END);
        v_balance := v_balance - v_charge;
        IF v_absorbed > 0 THEN
            INSERT INTO public.wallet_transactions
                (user_id, kind, amount_usd, balance_after_usd,
                 tool_slug, job_id, estimated_cost_usd,
                 gpu_seconds, gpu_class,
                 parent_tx_id, failure_reason, notes)
            VALUES
                (v_hold.user_id, 'absorbed_variance', 0, v_balance,
                 v_hold.tool_slug, v_hold.job_id, v_absorbed,
                 p_gpu_seconds, p_gpu_class,
                 p_hold_tx_id, p_failure_reason,
                 'live run: ' || v_absorbed::text ||
                 ' USD past a $0 balance; absorbed by Ranomics');
        END IF;
    ELSE
        v_released := v_taken - v_final;
        INSERT INTO public.wallet_transactions
            (user_id, kind, amount_usd, balance_after_usd,
             tool_slug, job_id, gpu_seconds, gpu_class,
             parent_tx_id, failure_reason, notes)
        VALUES
            (v_hold.user_id, 'hold_release', v_released, v_balance + v_released,
             v_hold.tool_slug, v_hold.job_id, p_gpu_seconds, p_gpu_class,
             p_hold_tx_id, p_failure_reason,
             'live run: returned what the debits took above the final cost');
        v_balance := v_balance + v_released;
    END IF;

    UPDATE public.user_wallets
       SET balance_usd = v_balance
     WHERE user_id = v_hold.user_id;

    RETURN jsonb_build_object(
        'settled_before', false, 'final', v_final, 'taken', v_taken,
        'charged', v_charge, 'released', v_released, 'absorbed', v_absorbed,
        'balance_after', v_balance);
END $$;

-- 0043's expire_signup_credit, with one change: its hold_open test now treats
-- a hold as open until it has a child that is not a 'run_debit', so the expiry
-- cannot run while a live run is still debiting.
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
                  AND c.kind <> 'run_debit'
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

-- 0020's wallet_30d_spend, with run_debit counted in spend the way charge is
-- (shared/wallet.py::_net_spend_usd counts it the same way).
CREATE OR REPLACE VIEW public.wallet_30d_spend
WITH (security_invoker = on) AS
SELECT user_id,
       GREATEST(
           COALESCE(SUM(ABS(amount_usd)) FILTER (WHERE kind::text = 'hold'), 0)
         - COALESCE(SUM(ABS(amount_usd)) FILTER (WHERE kind::text = 'hold_release'), 0)
         + COALESCE(SUM(ABS(amount_usd)) FILTER (WHERE kind::text = 'charge'), 0)
         + COALESCE(SUM(ABS(amount_usd)) FILTER (WHERE kind::text = 'run_debit'), 0),
           0
       ) AS spent_usd_30d,
       GREATEST(
           COALESCE(SUM(ABS(amount_usd)) FILTER (
               WHERE kind::text = 'hold' AND tool_slug ILIKE '%bindcraft%'), 0)
         - COALESCE(SUM(ABS(amount_usd)) FILTER (
               WHERE kind::text = 'hold_release' AND tool_slug ILIKE '%bindcraft%'), 0)
         + COALESCE(SUM(ABS(amount_usd)) FILTER (
               WHERE kind::text = 'charge' AND tool_slug ILIKE '%bindcraft%'), 0)
         + COALESCE(SUM(ABS(amount_usd)) FILTER (
               WHERE kind::text = 'run_debit' AND tool_slug ILIKE '%bindcraft%'), 0),
           0
       ) AS bindcraft_spent_usd_30d,
       COUNT(*)        FILTER (WHERE kind::text = 'charge') AS charges_30d,
       MAX(created_at) FILTER (WHERE kind::text = 'charge') AS last_charge_at
FROM public.wallet_transactions
WHERE created_at > now() - interval '30 days'
GROUP BY user_id;

REVOKE ALL ON FUNCTION public.open_live_run(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.open_live_run(uuid, text) FROM anon;
REVOKE ALL ON FUNCTION public.open_live_run(uuid, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.open_live_run(uuid, text) TO service_role;

REVOKE ALL ON FUNCTION public.debit_live_run(bigint, numeric, numeric, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.debit_live_run(bigint, numeric, numeric, text) FROM anon;
REVOKE ALL ON FUNCTION public.debit_live_run(bigint, numeric, numeric, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.debit_live_run(bigint, numeric, numeric, text) TO service_role;

REVOKE ALL ON FUNCTION public.settle_live_run(bigint, numeric, numeric, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.settle_live_run(bigint, numeric, numeric, text, text) FROM anon;
REVOKE ALL ON FUNCTION public.settle_live_run(bigint, numeric, numeric, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.settle_live_run(bigint, numeric, numeric, text, text) TO service_role;

REVOKE ALL ON FUNCTION public.expire_signup_credit(uuid, numeric, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.expire_signup_credit(uuid, numeric, bigint) FROM anon;
REVOKE ALL ON FUNCTION public.expire_signup_credit(uuid, numeric, bigint) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.expire_signup_credit(uuid, numeric, bigint) TO service_role;
