-- Ranomics tools-hub — Epitope Scout run counter for free-tier paywall
-- Wave 1 (Stream B). Safe to re-run (IF NOT EXISTS / IF NOT EXISTS guards).
--
-- Purpose
--   Back the 3-runs-per-month cap on scout.ranomics.com for signed-in
--   free-tier users. The tools-hub user_tier table from migration 0001
--   tells us whether the user is 'free' (paywalled) or 'scout_pro'+
--   (unlimited). This table records one row per completed Scout run so
--   the paywall decorator can count runs in the trailing 30-day window.
--
-- Data model
--   public.scout_runs       one row per completed analysis run
--   public.scout_run_count  view; convenience rolling-30-day count per user
--
-- Why "completed runs" not "submissions"
--   A user who uploads a malformed PDB and hits HTTP 422 should not burn
--   a quota slot. The application records a scout_runs row only after the
--   scoring pipeline returns successfully, which matches the user's
--   intuition that "a run" means "a useful result".
--
-- RLS
--   RLS is enabled with a USING (auth.uid() = user_id) policy so a signed-in
--   caller can only read their own rows. Inserts happen from the Flask
--   server using the service-role key (bypasses RLS), so no insert policy
--   is needed for clients. Matches migration 0001's pattern exactly.

-- ---------------------------------------------------------------------------
-- Scout run ledger
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.scout_runs (
    id           bigserial PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- SHA-256 hex digest of the input-PDB bytes + chain selector. Lets us
    -- dedupe accidental double-submits (same file, same chain) without
    -- tracking a formal request id. Nullable for migration tolerance.
    result_hash  text,
    -- Free-form metadata for future instrumentation (filename, structure
    -- title, pipeline duration, etc). Not indexed.
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS scout_runs_user_created_idx
    ON public.scout_runs (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS scout_runs_created_idx
    ON public.scout_runs (created_at DESC);

ALTER TABLE public.scout_runs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS scout_runs_self_read ON public.scout_runs;
CREATE POLICY scout_runs_self_read ON public.scout_runs
    FOR SELECT USING (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Convenience view — rolling 30-day count per user
-- ---------------------------------------------------------------------------
-- The Flask paywall reads this view with a .eq("user_id", ...) filter via
-- the service-role client. Keeping the math in one place (SQL) so we can
-- tune the window (7 / 30 / calendar-month) without shipping new Python.

CREATE OR REPLACE VIEW public.scout_run_count_30d AS
SELECT
    user_id,
    COUNT(*)::integer AS runs_last_30d,
    MAX(created_at)   AS last_run_at
FROM public.scout_runs
WHERE created_at >= (now() - interval '30 days')
GROUP BY user_id;
