-- 0048_settle_hold_clamp_note.sql
--
-- Reporting only: settle_hold's v_diff = 0 branch stops claiming the
-- estimate matched the actual cost on a settlement the hard cap clamped.
-- Nothing about what is charged, released or absorbed changes -- the
-- function below is the 0020_wallet_corrections.sql definition with the one
-- notes expression replaced, and
-- tests/test_wallet_templates.py::TestSettleHoldClampNote::test_0048_changes_only_the_note
-- asserts that statement for statement, comments and blank lines aside.
--
-- Rows written before this migration keep the old note. The ledger page
-- does not print it on a row it could annotate: blueprints/wallet.py reads
-- the lineage's amounts, and templates/wallet/transactions.html prints
-- tx.notes only where a row has no annotation -- which is every row when
-- the annotator falls back to an empty dict.

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
        -- v_diff = 0: nothing more to move either way. Still write a
        -- zero-amount charge row so the hold has a settle counterpart in
        -- the ledger.
        --
        -- The note has to tell the two ways v_diff reaches 0 apart. The
        -- clamp above manufactures the second one: when the metered actual
        -- is at or above the cap, v_capped_actual IS the cap, so a hold
        -- placed AT the cap gives v_estimate = v_capped_actual and lands
        -- here. That is the common clamped shape, because
        -- shared/wallet_estimates.py's cushioned_hold_usd ends
        -- min(hold, cap) over a worst-case floor that is itself clamped to
        -- the cap (bindcraft job 489a17e0: quoted $6.29, held the $8.00
        -- cap, metered $10.81, charged the cap, noted as a match --
        -- docs/qa/QA-2026-10-01-paid-sweep.md finding F-17). A clamp on a
        -- hold BELOW the cap makes v_diff negative instead and takes the
        -- true-up branch above, whose note is not false. So this arm is
        -- reached by two unlike settlements and may not assert either.
        INSERT INTO public.wallet_transactions
            (user_id, kind, amount_usd, balance_after_usd,
             tool_slug, job_id, gpu_seconds, gpu_class,
             parent_tx_id, failure_reason, notes)
        SELECT v_user_id, 'charge', 0, v_balance,
               tool_slug, job_id, p_gpu_seconds, p_gpu_class,
               p_hold_tx_id, p_failure_reason,
               CASE WHEN p_actual_usd > p_hard_cap_usd
                    THEN 'charge clamped at the tool hard cap of ' ||
                         p_hard_cap_usd::text || ' USD; metered ' ||
                         p_actual_usd::text || ' USD'
                    ELSE 'estimate matched actual' END
          FROM public.wallet_transactions
         WHERE id = p_hold_tx_id
        RETURNING id INTO v_charge_tx_id;
    END IF;

    RETURN v_charge_tx_id;
END $$;

GRANT EXECUTE ON FUNCTION public.settle_hold(bigint, numeric, numeric, numeric, text, text)
    TO service_role;
