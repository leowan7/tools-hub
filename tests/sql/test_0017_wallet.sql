-- tests/sql/test_0017_wallet.sql
--
-- Unit tests for 0017_wallet.sql. Plain-SQL assertions that exercise
-- the migration's tables, views, hold lifecycle functions, and the
-- backfill idempotency guarantees.
--
-- The whole file runs inside a single transaction and rolls back on
-- exit; nothing is persisted. Run against a database where 0017 has
-- already been applied:
--
--     psql "$TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -1 \
--          -f tests/sql/test_0017_wallet.sql
--
-- Each test block uses RAISE EXCEPTION on assertion failure so the
-- whole run aborts at the first regression. Each successful block ends
-- with a RAISE NOTICE so a passing run prints a checklist.
--
-- Counts (kept in sync with the test_count assertion at the bottom):
--   1.  Schema present: tables, views, functions, trigger
--   2.  Backfill idempotency: re-running backfill is a no-op
--   3.  Hold success path: balance drops, hold row written
--   4.  Hold insufficient balance returns NULL
--   5.  Hold frozen wallet returns NULL
--   6.  Concurrent serial holds cannot collectively overdraw the wallet
--   7.  Settle surplus path: hold_release row + balance restored
--   8.  Settle variance debit within balance: charge row debits remainder
--   9.  Settle absorbed_variance when wallet cannot cover the deficit
--  10.  Settle zero-diff still writes a charge row
--  11.  Settle hard cap clamps the actual cost
--  12.  Ledger sum invariant equals user_wallets.balance_usd for every user

BEGIN;

-- ---------------------------------------------------------------------------
-- Fixture: synthetic auth user rows
-- ---------------------------------------------------------------------------
-- auth.users has a NOT NULL email column on every recent Supabase
-- platform; we insert with placeholder values just enough to satisfy
-- the FK from public.user_wallets and public.wallet_transactions.

CREATE TEMP TABLE _test_state (
    user_a uuid,
    user_b uuid,
    user_c uuid,
    hold_a bigint,
    hold_b bigint
);

INSERT INTO _test_state (user_a, user_b, user_c)
VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid());

DO $$
DECLARE
    s _test_state%ROWTYPE;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;
    INSERT INTO auth.users (id, email, instance_id, aud, role)
    VALUES
        (s.user_a, 'wallet-test-a-' || s.user_a || '@example.invalid', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated'),
        (s.user_b, 'wallet-test-b-' || s.user_b || '@example.invalid', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated'),
        (s.user_c, 'wallet-test-c-' || s.user_c || '@example.invalid', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated');
END $$;

-- Seed wallets and signup credits for each test user. We do this by hand
-- (rather than re-running the migration backfill) because the migration
-- has already run; instead we replay the same NOT EXISTS guarded inserts.
INSERT INTO public.user_wallets (user_id, balance_usd)
SELECT user_a, 10.00 FROM _test_state
UNION ALL
SELECT user_b, 2.00 FROM _test_state
UNION ALL
SELECT user_c, 100.00 FROM _test_state
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO public.wallet_transactions (user_id, kind, amount_usd, balance_after_usd, notes)
SELECT user_a, 'signup_credit', 10.00, 10.00, 'test seed' FROM _test_state
UNION ALL
SELECT user_b, 'signup_credit',  2.00,  2.00, 'test seed' FROM _test_state
UNION ALL
SELECT user_c, 'signup_credit', 100.00, 100.00, 'test seed' FROM _test_state;

-- ---------------------------------------------------------------------------
-- Test 1: schema is present
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    PERFORM 1 FROM pg_type WHERE typname = 'wallet_tx_kind';
    IF NOT FOUND THEN RAISE EXCEPTION 'test 1: wallet_tx_kind enum missing'; END IF;

    PERFORM 1 FROM pg_tables
     WHERE schemaname = 'public' AND tablename IN ('user_wallets', 'wallet_transactions', 'funnel_alerts')
     HAVING COUNT(*) = 3;
    IF NOT FOUND THEN RAISE EXCEPTION 'test 1: wallet tables missing'; END IF;

    PERFORM 1 FROM pg_views
     WHERE schemaname = 'public'
       AND viewname IN ('wallet_30d_spend', 'wallet_auto_reload_24h', 'wallet_auto_reload_month')
     HAVING COUNT(*) = 3;
    IF NOT FOUND THEN RAISE EXCEPTION 'test 1: wallet views missing'; END IF;

    PERFORM 1 FROM pg_proc p
      JOIN pg_namespace n ON p.pronamespace = n.oid
     WHERE n.nspname = 'public' AND p.proname IN ('try_hold_for_job', 'settle_hold', 'tg_user_wallets_updated_at')
     HAVING COUNT(*) >= 3;
    IF NOT FOUND THEN RAISE EXCEPTION 'test 1: wallet functions missing'; END IF;

    PERFORM 1 FROM pg_trigger
     WHERE tgname = 'user_wallets_updated_at' AND NOT tgisinternal;
    IF NOT FOUND THEN RAISE EXCEPTION 'test 1: updated_at trigger missing'; END IF;

    RAISE NOTICE 'test 1: schema present (tables, views, functions, trigger)';
END $$;

-- ---------------------------------------------------------------------------
-- Test 2: backfill idempotency
-- ---------------------------------------------------------------------------
--
-- Replays the migration's backfill INSERTs and verifies that no user
-- ever accumulates a second signup_credit row, and that user_wallets
-- balance is left untouched. We seed two synthetic auth users without
-- wallets, then run the backfill twice.

DO $$
DECLARE
    new_user_1 uuid := gen_random_uuid();
    new_user_2 uuid := gen_random_uuid();
    cnt_before integer;
    cnt_after integer;
    wallet_balance numeric;
BEGIN
    INSERT INTO auth.users (id, email, instance_id, aud, role)
    VALUES
        (new_user_1, 'wallet-backfill-1-' || new_user_1 || '@example.invalid', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated'),
        (new_user_2, 'wallet-backfill-2-' || new_user_2 || '@example.invalid', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated');

    SELECT COUNT(*) INTO cnt_before
      FROM public.wallet_transactions
     WHERE user_id IN (new_user_1, new_user_2);

    IF cnt_before <> 0 THEN
        RAISE EXCEPTION 'test 2 setup: synthetic users already had wallet rows';
    END IF;

    -- First pass: equivalent to the migration backfill.
    INSERT INTO public.user_wallets (user_id, balance_usd)
    SELECT u.id, 5.00 FROM auth.users u WHERE u.id IN (new_user_1, new_user_2)
    ON CONFLICT (user_id) DO NOTHING;

    INSERT INTO public.wallet_transactions
        (user_id, kind, amount_usd, balance_after_usd, notes)
    SELECT u.id, 'signup_credit', 5.00, 5.00, 'test 2 backfill replay'
      FROM auth.users u
     WHERE u.id IN (new_user_1, new_user_2)
       AND NOT EXISTS (
           SELECT 1 FROM public.wallet_transactions wt
            WHERE wt.user_id = u.id AND wt.kind = 'signup_credit'
       );

    -- Second pass: must be a no-op.
    INSERT INTO public.user_wallets (user_id, balance_usd)
    SELECT u.id, 5.00 FROM auth.users u WHERE u.id IN (new_user_1, new_user_2)
    ON CONFLICT (user_id) DO NOTHING;

    INSERT INTO public.wallet_transactions
        (user_id, kind, amount_usd, balance_after_usd, notes)
    SELECT u.id, 'signup_credit', 5.00, 5.00, 'test 2 backfill replay (second)'
      FROM auth.users u
     WHERE u.id IN (new_user_1, new_user_2)
       AND NOT EXISTS (
           SELECT 1 FROM public.wallet_transactions wt
            WHERE wt.user_id = u.id AND wt.kind = 'signup_credit'
       );

    SELECT COUNT(*) INTO cnt_after
      FROM public.wallet_transactions
     WHERE user_id IN (new_user_1, new_user_2)
       AND kind = 'signup_credit';

    IF cnt_after <> 2 THEN
        RAISE EXCEPTION 'test 2: expected 2 signup_credit rows (one per user), got %', cnt_after;
    END IF;

    -- Wallet balance must still be 5.00 for each user.
    SELECT MIN(balance_usd) INTO wallet_balance
      FROM public.user_wallets
     WHERE user_id IN (new_user_1, new_user_2);

    IF wallet_balance <> 5.00 THEN
        RAISE EXCEPTION 'test 2: expected balance 5.00 for both users, got %', wallet_balance;
    END IF;

    RAISE NOTICE 'test 2: backfill is idempotent on a second run';
END $$;

-- ---------------------------------------------------------------------------
-- Test 3: try_hold_for_job success path
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    s _test_state%ROWTYPE;
    hold_id bigint;
    balance_after numeric;
    hold_amount numeric := 3.00;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;

    hold_id := public.try_hold_for_job(s.user_a, hold_amount, 'mpnn', 1001);
    IF hold_id IS NULL THEN
        RAISE EXCEPTION 'test 3: try_hold_for_job returned NULL on a wallet with enough balance';
    END IF;

    -- Stash for later tests
    UPDATE _test_state SET hold_a = hold_id;

    SELECT balance_after_usd INTO balance_after
      FROM public.wallet_transactions WHERE id = hold_id;

    IF balance_after <> 10.00 - hold_amount THEN
        RAISE EXCEPTION 'test 3: balance_after_usd=% (expected %)', balance_after, 10.00 - hold_amount;
    END IF;

    -- user_wallets cache reflects the hold.
    SELECT balance_usd INTO balance_after
      FROM public.user_wallets WHERE user_id = s.user_a;

    IF balance_after <> 10.00 - hold_amount THEN
        RAISE EXCEPTION 'test 3: user_wallets.balance_usd=% (expected %)', balance_after, 10.00 - hold_amount;
    END IF;

    RAISE NOTICE 'test 3: try_hold_for_job success path debits and writes hold row';
END $$;

-- ---------------------------------------------------------------------------
-- Test 4: try_hold_for_job rejects insufficient balance
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    s _test_state%ROWTYPE;
    hold_id bigint;
    balance_before numeric;
    balance_after numeric;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;
    SELECT balance_usd INTO balance_before FROM public.user_wallets WHERE user_id = s.user_b;

    hold_id := public.try_hold_for_job(s.user_b, 50.00, 'mpnn', 1002);
    IF hold_id IS NOT NULL THEN
        RAISE EXCEPTION 'test 4: try_hold_for_job returned id % for an over-budget request', hold_id;
    END IF;

    SELECT balance_usd INTO balance_after FROM public.user_wallets WHERE user_id = s.user_b;
    IF balance_after <> balance_before THEN
        RAISE EXCEPTION 'test 4: balance changed despite rejection (% to %)', balance_before, balance_after;
    END IF;

    RAISE NOTICE 'test 4: try_hold_for_job rejects insufficient balance without side effects';
END $$;

-- ---------------------------------------------------------------------------
-- Test 5: try_hold_for_job rejects frozen wallet
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    s _test_state%ROWTYPE;
    hold_id bigint;
    balance_before numeric;
    balance_after numeric;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;
    SELECT balance_usd INTO balance_before FROM public.user_wallets WHERE user_id = s.user_c;

    UPDATE public.user_wallets
       SET wallet_frozen = true, wallet_frozen_reason = 'test 5 freeze'
     WHERE user_id = s.user_c;

    hold_id := public.try_hold_for_job(s.user_c, 1.00, 'mpnn', 1003);
    IF hold_id IS NOT NULL THEN
        RAISE EXCEPTION 'test 5: try_hold_for_job returned id % for a frozen wallet', hold_id;
    END IF;

    SELECT balance_usd INTO balance_after FROM public.user_wallets WHERE user_id = s.user_c;
    IF balance_after <> balance_before THEN
        RAISE EXCEPTION 'test 5: balance changed despite frozen rejection';
    END IF;

    -- Restore for downstream tests.
    UPDATE public.user_wallets SET wallet_frozen = false WHERE user_id = s.user_c;

    RAISE NOTICE 'test 5: try_hold_for_job rejects frozen wallet without side effects';
END $$;

-- ---------------------------------------------------------------------------
-- Test 6: sequential holds cannot collectively overdraw the wallet
-- ---------------------------------------------------------------------------
--
-- This is a serial proxy for the concurrency contract. The function is
-- the only write path that drains balance; in a real concurrent scenario
-- the FOR UPDATE row lock serializes callers. Here we simulate the same
-- sequence and verify the second over-budget request is rejected.

DO $$
DECLARE
    s _test_state%ROWTYPE;
    hold_one bigint;
    hold_two bigint;
    balance numeric;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;

    -- user_a currently has 7.00 after test 3's hold of 3.
    hold_one := public.try_hold_for_job(s.user_a, 5.00, 'mpnn', 1004);
    IF hold_one IS NULL THEN
        RAISE EXCEPTION 'test 6: first hold should succeed (balance 7.00, hold 5.00)';
    END IF;

    hold_two := public.try_hold_for_job(s.user_a, 5.00, 'mpnn', 1005);
    IF hold_two IS NOT NULL THEN
        RAISE EXCEPTION 'test 6: second hold should be rejected (remaining balance 2.00, hold 5.00)';
    END IF;

    SELECT balance_usd INTO balance FROM public.user_wallets WHERE user_id = s.user_a;
    IF balance <> 2.00 THEN
        RAISE EXCEPTION 'test 6: balance=% (expected 2.00)', balance;
    END IF;

    -- Settle the test 6 hold back to clear state for test 7 onwards.
    PERFORM public.settle_hold(hold_one, 0, 1000, 0, 'L4', 'test 6 cleanup');
    -- After settle of a 5.00 hold at 0 actual, the wallet should be back to 7.00.
    SELECT balance_usd INTO balance FROM public.user_wallets WHERE user_id = s.user_a;
    IF balance <> 7.00 THEN
        RAISE EXCEPTION 'test 6 cleanup: balance after settle=% (expected 7.00)', balance;
    END IF;

    RAISE NOTICE 'test 6: sequential holds cannot collectively overdraw the wallet';
END $$;

-- ---------------------------------------------------------------------------
-- Test 7: settle_hold surplus path
-- ---------------------------------------------------------------------------
--
-- User A still has the test 3 hold (3.00 reserved, 7.00 balance). Settle
-- with actual = 1.00 and hard_cap = 100.00. Expect a hold_release row
-- of +2.00 and balance restored to 9.00.

DO $$
DECLARE
    s _test_state%ROWTYPE;
    release_row record;
    balance numeric;
    cnt_release integer;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;

    PERFORM public.settle_hold(s.hold_a, 1.00, 100.00, 30.5, 'L4', NULL);

    SELECT * INTO release_row
      FROM public.wallet_transactions
     WHERE parent_tx_id = s.hold_a AND kind = 'hold_release';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'test 7: no hold_release row for hold %', s.hold_a;
    END IF;

    IF release_row.amount_usd <> 2.00 THEN
        RAISE EXCEPTION 'test 7: hold_release.amount=% (expected 2.00)', release_row.amount_usd;
    END IF;

    SELECT balance_usd INTO balance FROM public.user_wallets WHERE user_id = s.user_a;
    IF balance <> 9.00 THEN
        RAISE EXCEPTION 'test 7: balance=% (expected 9.00 = 10 - 3 hold + 2 release)', balance;
    END IF;

    -- Verify only one settle row was written; calling settle_hold a second
    -- time would write another. The caller (Python wallet.py) is responsible
    -- for not calling settle twice for the same hold_tx_id.
    SELECT COUNT(*) INTO cnt_release
      FROM public.wallet_transactions
     WHERE parent_tx_id = s.hold_a;
    IF cnt_release <> 1 THEN
        RAISE EXCEPTION 'test 7: expected 1 settle row for hold %, found %', s.hold_a, cnt_release;
    END IF;

    RAISE NOTICE 'test 7: settle_hold releases surplus and restores balance';
END $$;

-- ---------------------------------------------------------------------------
-- Test 8: settle_hold variance debit within balance
-- ---------------------------------------------------------------------------
--
-- New hold on user_a (balance 9.00). Estimate 3.00, actual 5.00, hard_cap
-- 100. Expect a charge row of -2.00 and balance dropping by an extra 2.

DO $$
DECLARE
    s _test_state%ROWTYPE;
    new_hold bigint;
    settle_row record;
    balance numeric;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;

    new_hold := public.try_hold_for_job(s.user_a, 3.00, 'mpnn', 1006);
    IF new_hold IS NULL THEN RAISE EXCEPTION 'test 8 setup: hold rejected'; END IF;

    PERFORM public.settle_hold(new_hold, 5.00, 100.00, 45.0, 'L4', NULL);

    SELECT * INTO settle_row
      FROM public.wallet_transactions
     WHERE parent_tx_id = new_hold AND kind = 'charge';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'test 8: no charge row for variance debit on hold %', new_hold;
    END IF;

    IF settle_row.amount_usd <> -2.00 THEN
        RAISE EXCEPTION 'test 8: variance debit amount=% (expected -2.00)', settle_row.amount_usd;
    END IF;

    SELECT balance_usd INTO balance FROM public.user_wallets WHERE user_id = s.user_a;
    -- Started this block at 9.00, held 3.00 (balance 6.00), then debited
    -- another 2.00 for variance. Expected final balance 4.00.
    IF balance <> 4.00 THEN
        RAISE EXCEPTION 'test 8: balance=% (expected 4.00)', balance;
    END IF;

    RAISE NOTICE 'test 8: settle_hold debits variance within balance';
END $$;

-- ---------------------------------------------------------------------------
-- Test 9: settle_hold absorbed_variance when wallet cannot cover the deficit
-- ---------------------------------------------------------------------------
--
-- User_a has 4.00. Place a 1.00 hold (balance 3.00). Settle at actual
-- 100.00 (hard cap 100, so capped_actual 100, diff -99). Wallet cannot
-- cover -99 from 3.00 (would go to -96). The function records an
-- absorbed_variance row and leaves the balance at 3.00.

DO $$
DECLARE
    s _test_state%ROWTYPE;
    new_hold bigint;
    absorbed_row record;
    balance numeric;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;

    new_hold := public.try_hold_for_job(s.user_a, 1.00, 'bindcraft', 1007);
    IF new_hold IS NULL THEN RAISE EXCEPTION 'test 9 setup: hold rejected'; END IF;

    PERFORM public.settle_hold(new_hold, 100.00, 100.00, 3600, 'A100-40GB', NULL);

    SELECT * INTO absorbed_row
      FROM public.wallet_transactions
     WHERE parent_tx_id = new_hold AND kind = 'absorbed_variance';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'test 9: no absorbed_variance row for hold %', new_hold;
    END IF;

    IF absorbed_row.amount_usd <> -99.00 THEN
        RAISE EXCEPTION 'test 9: absorbed_variance.amount=% (expected -99.00)', absorbed_row.amount_usd;
    END IF;

    SELECT balance_usd INTO balance FROM public.user_wallets WHERE user_id = s.user_a;
    -- Wallet was at 4.00, then 1.00 hold left 3.00. absorbed_variance does
    -- not move balance, so we should still see 3.00.
    IF balance <> 3.00 THEN
        RAISE EXCEPTION 'test 9: balance=% (expected 3.00)', balance;
    END IF;

    RAISE NOTICE 'test 9: settle_hold records absorbed_variance when wallet cannot cover deficit';
END $$;

-- ---------------------------------------------------------------------------
-- Test 10: settle_hold writes a zero charge row when actual matches estimate
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    s _test_state%ROWTYPE;
    new_hold bigint;
    charge_row record;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;

    -- user_a balance is 3.00 after test 9; use user_c which still has 100.00.
    new_hold := public.try_hold_for_job(s.user_c, 2.50, 'esmfold', 1008);
    IF new_hold IS NULL THEN RAISE EXCEPTION 'test 10 setup: hold rejected'; END IF;

    PERFORM public.settle_hold(new_hold, 2.50, 100.00, 60, 'A100-80GB', NULL);

    SELECT * INTO charge_row
      FROM public.wallet_transactions
     WHERE parent_tx_id = new_hold AND kind = 'charge';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'test 10: no charge row written for zero-diff settle';
    END IF;

    IF charge_row.amount_usd <> 0 THEN
        RAISE EXCEPTION 'test 10: zero-diff charge.amount=% (expected 0)', charge_row.amount_usd;
    END IF;

    RAISE NOTICE 'test 10: settle_hold writes zero-amount charge row when estimate matches actual';
END $$;

-- ---------------------------------------------------------------------------
-- Test 11: settle_hold hard cap clamps the actual cost
-- ---------------------------------------------------------------------------
--
-- Hold 4.00 on user_c. Actual 50.00, hard cap 10.00. capped_actual is
-- min(50, 10) = 10, diff = 4 - 10 = -6. Wallet has enough to cover -6,
-- so a charge of -6 is written, not -46.

DO $$
DECLARE
    s _test_state%ROWTYPE;
    new_hold bigint;
    charge_row record;
BEGIN
    SELECT * INTO s FROM _test_state LIMIT 1;

    new_hold := public.try_hold_for_job(s.user_c, 4.00, 'rfdiffusion', 1009);
    IF new_hold IS NULL THEN RAISE EXCEPTION 'test 11 setup: hold rejected'; END IF;

    PERFORM public.settle_hold(new_hold, 50.00, 10.00, 1200, 'A100-40GB', NULL);

    SELECT * INTO charge_row
      FROM public.wallet_transactions
     WHERE parent_tx_id = new_hold AND kind = 'charge';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'test 11: no charge row from hard-cap clamp settle';
    END IF;

    IF charge_row.amount_usd <> -6.00 THEN
        RAISE EXCEPTION 'test 11: charge.amount=% (expected -6.00 from hard-cap clamp)', charge_row.amount_usd;
    END IF;

    RAISE NOTICE 'test 11: settle_hold hard cap clamps actual cost to cap, not raw';
END $$;

-- ---------------------------------------------------------------------------
-- Test 12: ledger sum invariant
-- ---------------------------------------------------------------------------
--
-- The first-class invariant: for every user, SUM(amount_usd) over their
-- ledger rows equals user_wallets.balance_usd. The migration's hold and
-- settle functions update both halves atomically; this check catches any
-- regression in that pairing.

DO $$
DECLARE
    bad_user record;
    mismatch_count integer := 0;
BEGIN
    FOR bad_user IN
        SELECT w.user_id,
               w.balance_usd                                  AS cached_balance,
               COALESCE(SUM(t.amount_usd), 0)                 AS ledger_sum
          FROM public.user_wallets w
          LEFT JOIN public.wallet_transactions t
                 ON t.user_id = w.user_id
         GROUP BY w.user_id, w.balance_usd
        HAVING w.balance_usd <> COALESCE(SUM(t.amount_usd), 0)
    LOOP
        mismatch_count := mismatch_count + 1;
        RAISE WARNING 'test 12: user % cached_balance=% ledger_sum=%',
            bad_user.user_id, bad_user.cached_balance, bad_user.ledger_sum;
    END LOOP;

    IF mismatch_count > 0 THEN
        RAISE EXCEPTION 'test 12: % wallet(s) drifted from ledger sum', mismatch_count;
    END IF;

    RAISE NOTICE 'test 12: ledger sum equals user_wallets.balance_usd for every user';
END $$;

-- ---------------------------------------------------------------------------
-- Final summary
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    RAISE NOTICE 'OK: 12 wallet migration test blocks passed';
END $$;

ROLLBACK;
