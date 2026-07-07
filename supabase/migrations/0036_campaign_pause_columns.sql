-- 0036_campaign_pause_columns.sql
--
-- Compute Campaigns Phase 2, build step 4b (see
-- docs/COMPUTE-CAMPAIGNS-PHASE-2-PLAN.md section 4).
--
-- Two nullable bookkeeping columns on compute_campaigns, both idempotent:
--
--   * paused_at         — set when the driver moves a campaign into
--                         paused_insufficient_funds, cleared on resume. Feeds
--                         the 14-day pause TTL auto-finalize (a never-funded
--                         paused campaign eventually finalizes as partial rather
--                         than lingering forever).
--   * pause_notified_at — set ONLY when the pause email actually sent, so the
--                         cron re-sends if a transient Resend failure dropped it
--                         (durable delivery). Cleared on resume so a later pause
--                         re-notifies.
--
-- Apply via the Supabase SQL editor on the prod project BEFORE deploying the
-- step-4b code: the driver writes these columns on every pause/resume, so the
-- code must not ship ahead of the schema. ADD COLUMN IF NOT EXISTS is safe to
-- re-run.

ALTER TABLE public.compute_campaigns
    ADD COLUMN IF NOT EXISTS paused_at         timestamptz,
    ADD COLUMN IF NOT EXISTS pause_notified_at timestamptz;
