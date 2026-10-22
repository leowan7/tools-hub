-- $0 stop test, 2026-10-22. Sets ONE test account's wallet to exactly $1.50.
-- The run is boltz2, `msa_server` preset, 16 binders: it owes $4.16 if it runs
-- to the end (live_due_usd('boltz2', 3424, 'A100-40GB'), 16 x 214 s/design from
-- docs/VALIDATION-LOG.md Rung B), so the meter must stop it at $0 partway.
-- boltz2's live start minimum is $0.00 (shared/wallet_guard.py::live_start_usd),
-- so $1.50 starts the run. debit_live_run never takes more than the balance, so
-- the worst case is the $1.50 set here.
-- Run only once Leo has said yes to the paid run in this session.
DO $$
DECLARE
  v_uid uuid;
  v_sum numeric;
BEGIN
  SELECT id INTO STRICT v_uid FROM auth.users WHERE email = 'leowan7+t1@gmail.com';

  PERFORM 1 FROM public.user_wallets WHERE user_id = v_uid FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'No wallet yet. Open /account/wallet once as the test account, then rerun.';
  END IF;
  PERFORM 1 FROM public.wallet_transactions WHERE user_id = v_uid AND kind = 'signup_credit';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Signup credit not granted yet. Open /account/wallet once, then rerun.';
  END IF;

  SELECT COALESCE(SUM(amount_usd), 0) INTO v_sum
    FROM public.wallet_transactions WHERE user_id = v_uid;
  IF v_sum > 20 THEN
    RAISE EXCEPTION 'Balance % looks like a real account. Stopping.', v_sum;
  END IF;

  INSERT INTO public.wallet_transactions
    (user_id, kind, amount_usd, balance_after_usd, notes)
  VALUES
    (v_uid, 'adjustment', 1.50 - v_sum, 1.50,
     'T1 $0 stop test 2026-10-22: set balance to $1.50');
  UPDATE public.user_wallets SET balance_usd = 1.50 WHERE user_id = v_uid;
END $$;

-- Check: both columns should read 1.5000.
SELECT w.balance_usd,
       (SELECT SUM(t.amount_usd) FROM public.wallet_transactions t
         WHERE t.user_id = w.user_id) AS ledger_sum
  FROM public.user_wallets w
  JOIN auth.users u ON u.id = w.user_id
 WHERE u.email = 'leowan7+t1@gmail.com';
