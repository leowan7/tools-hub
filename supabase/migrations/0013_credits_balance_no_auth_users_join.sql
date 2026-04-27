-- Ranomics tools-hub — drop auth.users dependency from credits_balance view
-- Stream: launch-day hotfix. Safe to re-run.
--
-- Gap closed
--   Migration 0003 redefined credits_balance with WITH (security_invoker = true)
--   for RLS hardening. Newer Supabase revisions revoked SELECT on auth.users
--   from the service_role, so the view's LEFT JOIN auth.users now raises
--   "permission denied for table users" (SQLSTATE 42501) on every read —
--   silently turning the navbar credit balance into 0 for every user.
--
-- Fix
--   Recreate the view without joining auth.users. The original join only
--   existed so users with no ledger entries got a 0-balance row; the
--   application already treats a missing row as "balance 0" (see
--   shared/credits.py::get_balance), so dropping the join changes nothing
--   user-visible while restoring read access. RLS on credits_ledger still
--   gates underlying rows.

DROP VIEW IF EXISTS public.credits_balance;
CREATE VIEW public.credits_balance
    WITH (security_invoker = true) AS
SELECT
    user_id,
    COALESCE(SUM(delta), 0)::integer AS balance,
    MAX(created_at)                  AS last_entry_at
FROM public.credits_ledger
GROUP BY user_id;
