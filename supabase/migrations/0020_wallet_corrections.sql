-- ---------------------------------------------------------------------------
-- 0020_wallet_corrections.sql
-- ---------------------------------------------------------------------------
--
-- Four wallet-ledger corrections found while closing out the wallet
-- pivot (see docs/HANDOFF-WALLET-PIVOT-SESSION-14.md):
--
--   1. try_hold_for_job now enforces the daily spend cap under the same
--      row lock as the balance check. The cap was Python-only
--      (wallet_preflight), leaving a TOCTOU window where a burst of
--      concurrent submits could collectively step past the cap.
--
--   2. settle_hold is now idempotent. The 0017 version had no replay
--      guard ("idempotency is the caller's responsibility"), so a
--      retried completion webhook would write a second set of settle
--      rows and double-charge. A guard matching release_hold's pattern
--      closes this.
--
--   3. settle_hold's absorbed_variance branch wrote a row with a
--      nonzero amount_usd but did NOT move user_wallets.balance_usd,
--      breaking the ledger invariant SUM(amount_usd) = balance_usd. The
--      row is now written with amount_usd = 0 (it is a marker, not a
--      debit; the user is not charged for an absorbed overrun). The
--      absorbed magnitude is preserved in estimated_cost_usd and notes.
--
--   4. The wallet_30d_spend reporting view summed kind = 'charge' for
--      "spend". Job spend lands in 'hold' rows; 'charge' rows hold only
--      true-up overrun variance, so the view reported near-zero for
--      every user. It now uses the canonical net-spend definition
--      (|holds| - |releases| + |charges|), matching
--      shared.wallet._net_spend_usd.
--
-- Also adds the tool_jobs_p90 view that shared/wallet_estimates.py has
-- always queried but that no migration ever created; until now the
-- estimator silently fell back to the per-tool spec defaults.
--
-- Apply via the Supabase SQL editor on the prod project (paste the
-- whole file; runs in a single transaction). Every statement is
-- CREATE OR REPLACE / idempotent, so the migration is safe to re-run.

-- ---------------------------------------------------------------------------
-- Function: try_hold_for_job  (adds daily-cap enforcement)
-- ---------------------------------------------------------------------------
--
-- Signature is unchanged from 0019 (Python still calls it with five
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
    v_balance     numeric;
    v_tx_id       bigint;
    v_frozen      boolean;
    v_daily_cap   numeric;
    v_spent_today numeric;
    v_day_start   timestamptz;
BEGIN
    -- Defense in depth: if the parameter-scaled hard cap is supplied,
    -- the estimated hold cannot exceed it.
    IF p_hard_cap_usd IS NOT NULL AND p_amount_usd > p_hard_cap_usd THEN
        RETURN NULL;
    END IF;

    -- Lock the wallet row for the duration of this transaction so the
    -- daily-cap and balance checks below are race-safe against a burst
    -- of concurrent submissions.
    PERFORM 1 FROM public.user_wallets WHERE user_id = p_user_id FOR UPDATE;

    SELECT wallet_frozen, daily_spend_cap_usd
      INTO v_frozen, v_daily_cap
      FROM public.user_wallets
     WHERE user_id = p_user_id;

    IF v_frozen THEN
        RETURN NULL;
    END IF;

    -- Daily spend cap. wallet_preflight checks this in Python too, but
    -- only the in-lock check here is safe against two submits that both
    -- pass the Python check before either has placed its hold. Today's
    -- spend uses the canonical net-spend definition
    -- (|holds| - |releases| + |charges|) since 00:00 UTC.
    IF v_daily_cap IS NOT NULL THEN
        v_day_start := date_trunc('day', now() AT TIME ZONE 'UTC')
                       AT TIME ZONE 'UTC';
        SELECT COALESCE(SUM(ABS(amount_usd))
                          FILTER (WHERE kind = 'hold'), 0)
             - COALESCE(SUM(ABS(amount_usd))
                          FILTER (WHERE kind = 'hold_release'), 0)
             + COALESCE(SUM(ABS(amount_usd))
                          FILTER (WHERE kind = 'charge'), 0)
          INTO v_spent_today
          FROM public.wallet_transactions
         WHERE user_id = p_user_id
           AND created_at >= v_day_start;

        IF GREATEST(v_spent_today, 0) + p_amount_usd > v_daily_cap THEN
            RETURN NULL;
        END IF;
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

-- ---------------------------------------------------------------------------
-- Function: settle_hold  (idempotency guard + absorbed_variance fix)
-- ---------------------------------------------------------------------------
--
-- Replaces the 0017 definition. Two changes:
--   * a replay guard so a second settle on the same hold is a no-op;
--   * the absorbed_variance row is written with amount_usd = 0 so the
--     SUM(amount_usd) = balance_usd invariant holds (the user is not
--     debited for an overrun Ranomics absorbs).

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

    -- Idempotency: a hold that already has a settle child (hold_release,
    -- charge, or absorbed_variance) has been settled or released. A
    -- second call is a no-op so a retried completion webhook cannot
    -- double-charge. The check sits under the row lock above, so two
    -- concurrent settles serialize and the second sees the first's row.
    IF EXISTS (
        SELECT 1
          FROM public.wallet_transactions
         WHERE parent_tx_id = p_hold_tx_id
    ) THEN
        RETURN NULL;
    END IF;

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
            -- The overrun exceeds the wallet's remaining balance.
            -- Ranomics absorbs it: the user's balance does not move.
            -- amount_usd is 0 so SUM(amount_usd) still equals
            -- balance_usd; the absorbed magnitude is kept in
            -- estimated_cost_usd and the notes for internal accounting.
            INSERT INTO public.wallet_transactions
                (user_id, kind, amount_usd, balance_after_usd,
                 tool_slug, job_id, estimated_cost_usd,
                 gpu_seconds, gpu_class,
                 parent_tx_id, failure_reason, notes)
            SELECT v_user_id, 'absorbed_variance', 0, v_balance,
                   tool_slug, job_id, ABS(v_diff),
                   p_gpu_seconds, p_gpu_class,
                   p_hold_tx_id, p_failure_reason,
                   'variance of ' || ABS(v_diff)::text ||
                   ' USD exceeded balance; absorbed by Ranomics'
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

GRANT EXECUTE ON FUNCTION public.settle_hold(bigint, numeric, numeric, numeric, text, text)
    TO service_role;

-- ---------------------------------------------------------------------------
-- View: wallet_30d_spend  (canonical net-spend definition)
-- ---------------------------------------------------------------------------
--
-- spent_usd_30d is now |holds| - |releases| + |charges| over the last
-- 30 days, the same formula shared.wallet._net_spend_usd uses. ABS()
-- sits inside SUM() (FILTER must attach to the aggregate, not a scalar
-- wrapper -- see the 0017 wallet_30d_spend bug noted in Session 6).
-- Column names are kept identical to the 0017 view so this is an
-- in-place CREATE OR REPLACE; charges_30d / last_charge_at still count
-- charge (overrun) rows, honest to their names.

CREATE OR REPLACE VIEW public.wallet_30d_spend
WITH (security_invoker = on) AS
SELECT user_id,
       GREATEST(
           COALESCE(SUM(ABS(amount_usd)) FILTER (WHERE kind = 'hold'), 0)
         - COALESCE(SUM(ABS(amount_usd)) FILTER (WHERE kind = 'hold_release'), 0)
         + COALESCE(SUM(ABS(amount_usd)) FILTER (WHERE kind = 'charge'), 0),
           0
       ) AS spent_usd_30d,
       GREATEST(
           COALESCE(SUM(ABS(amount_usd)) FILTER (
               WHERE kind = 'hold' AND tool_slug ILIKE '%bindcraft%'), 0)
         - COALESCE(SUM(ABS(amount_usd)) FILTER (
               WHERE kind = 'hold_release' AND tool_slug ILIKE '%bindcraft%'), 0)
         + COALESCE(SUM(ABS(amount_usd)) FILTER (
               WHERE kind = 'charge' AND tool_slug ILIKE '%bindcraft%'), 0),
           0
       ) AS bindcraft_spent_usd_30d,
       COUNT(*)        FILTER (WHERE kind = 'charge') AS charges_30d,
       MAX(created_at) FILTER (WHERE kind = 'charge') AS last_charge_at
FROM public.wallet_transactions
WHERE created_at > now() - interval '30 days'
GROUP BY user_id;

-- ---------------------------------------------------------------------------
-- View: tool_jobs_p90  (per-tool p90 GPU seconds for the estimator)
-- ---------------------------------------------------------------------------
--
-- shared/wallet_estimates._historical_p90_seconds reads this view to
-- price a job from real history once a tool has at least
-- MIN_HISTORICAL_RUNS (20) succeeded runs in the lookback window. No
-- migration ever created it, so the estimator always fell back to the
-- per-tool spec defaults. The estimator only ever queries
-- lookback_days = 30, so the view exposes that single window.

CREATE OR REPLACE VIEW public.tool_jobs_p90
WITH (security_invoker = on) AS
SELECT tool                                                          AS tool_slug,
       30                                                            AS lookback_days,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY gpu_seconds_used)  AS p90_gpu_seconds,
       COUNT(*)                                                      AS sample_size
FROM public.tool_jobs
WHERE status = 'succeeded'
  AND gpu_seconds_used IS NOT NULL
  AND gpu_seconds_used > 0
  AND created_at > now() - interval '30 days'
GROUP BY tool;
