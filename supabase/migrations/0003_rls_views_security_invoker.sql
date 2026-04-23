-- Ranomics tools-hub — RLS hardening for views
-- Stream G (Wave-0 hardening). Safe to re-run.
--
-- Gap closed
--   Postgres views default to SECURITY DEFINER — they execute with the
--   permissions of the view's owner, not the caller. When migrations 0001
--   and 0002 created public.credits_balance and public.scout_run_count_30d
--   as the migration role, those views could see every row of the
--   underlying RLS-protected tables regardless of who queried them. Any
--   future code path that ever read the views with the anon or
--   authenticated key would have exposed other users' data.
--
-- Fix
--   Recreate both views with WITH (security_invoker = true) so they run in
--   the caller's permission context. RLS on credits_ledger and scout_runs
--   now applies transitively. Service-role reads are unaffected (service
--   role bypasses RLS regardless). Requires Postgres 15+ — Supabase has
--   been on 15 since 2023-07.

-- ---------------------------------------------------------------------------
-- credits_balance (depends on credits_ledger + auth.users)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS public.credits_balance;
CREATE VIEW public.credits_balance
    WITH (security_invoker = true) AS
SELECT
    u.id                                     AS user_id,
    COALESCE(SUM(l.delta), 0)::integer       AS balance,
    MAX(l.created_at)                        AS last_entry_at
FROM auth.users u
LEFT JOIN public.credits_ledger l ON l.user_id = u.id
GROUP BY u.id;

-- ---------------------------------------------------------------------------
-- scout_run_count_30d (depends on scout_runs)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS public.scout_run_count_30d;
CREATE VIEW public.scout_run_count_30d
    WITH (security_invoker = true) AS
SELECT
    user_id,
    COUNT(*)::integer AS runs_last_30d,
    MAX(created_at)   AS last_run_at
FROM public.scout_runs
WHERE created_at >= (now() - interval '30 days')
GROUP BY user_id;

-- ---------------------------------------------------------------------------
-- Assertion: every user-scoped table in public has RLS enabled.
-- Acts as a runtime tripwire on re-apply; if a future migration forgets
-- ENABLE ROW LEVEL SECURITY, this block raises and the migration aborts.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    offender text;
BEGIN
    SELECT string_agg(c.relname, ', ')
        INTO offender
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'r'
      AND c.relname IN ('user_tier', 'credits_ledger', 'stripe_events', 'scout_runs')
      AND c.relrowsecurity = false;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION 'RLS disabled on public table(s): %', offender;
    END IF;
END$$;
