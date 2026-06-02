-- Ranomics tools-hub — campaign label on tool_jobs (C4)
-- Safe to re-run.
--
-- Purpose
--   Power users running 50 variations of one target see them grouped on
--   /jobs instead of as 50 flat rows. A free-form user-typed string at
--   submit time (e.g. "HER2-binder-v3"). NULL when omitted, in which
--   case the /jobs page renders rows under an "Uncategorized" header.
--
-- Why a column, not just inputs._campaign_label
--   C3 refold already stashed a per-batch label inside inputs JSON, but
--   the /jobs index cannot SELECT and ORDER BY a nested JSON key without
--   a partial GIN index. A first-class column is cheaper to filter and
--   index. We backfill from inputs._campaign_label as part of the same
--   migration so existing refold batches surface in the new grouped view.

ALTER TABLE public.tool_jobs
    ADD COLUMN IF NOT EXISTS campaign_label text DEFAULT NULL;

-- Index for the common /jobs query: same user, ordered newest-first,
-- optionally filtered by campaign_label. The partial WHERE keeps the
-- index small — uncategorized rows do not need to be indexed by label.
CREATE INDEX IF NOT EXISTS tool_jobs_user_campaign_idx
    ON public.tool_jobs (user_id, campaign_label, created_at DESC)
    WHERE campaign_label IS NOT NULL;

-- Backfill from the C3 refold convention so existing rows surface in
-- the grouped view. Idempotent — only fills rows that have no column
-- value yet but do carry the inputs._campaign_label hint.
UPDATE public.tool_jobs
   SET campaign_label = inputs->>'_campaign_label'
 WHERE campaign_label IS NULL
   AND inputs ? '_campaign_label'
   AND length(trim(inputs->>'_campaign_label')) > 0;
