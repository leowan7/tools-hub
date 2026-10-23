-- BindCraft stop test (manual cancel), 2026-10-15.
-- Sets ONE test account's wallet to exactly $9.00. BindCraft needs about $8
-- free before it will start; the run should cost ~$1.60-$3.15, $8 at worst.
-- Run only once Leo has said yes to the paid run in the BindCraft session.
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
  IF v_sum > 10 THEN
    RAISE EXCEPTION 'Balance % looks like a real account. Stopping.', v_sum;
  END IF;

  INSERT INTO public.wallet_transactions
    (user_id, kind, amount_usd, balance_after_usd, notes)
  VALUES
    (v_uid, 'adjustment', 9.00 - v_sum, 9.00,
     'T1 BindCraft stop test 2026-10-15: set balance to $9.00');
  UPDATE public.user_wallets SET balance_usd = 9.00 WHERE user_id = v_uid;
END $$;

-- Check: both columns should read 9.0000.
SELECT w.balance_usd,
       (SELECT SUM(t.amount_usd) FROM public.wallet_transactions t
         WHERE t.user_id = w.user_id) AS ledger_sum
  FROM public.user_wallets w
  JOIN auth.users u ON u.id = w.user_id
 WHERE u.email = 'leowan7+t1@gmail.com';
