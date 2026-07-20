-- Ranomics tools-hub — campaign-wide shortlist -> lab handoff. Safe to re-run.
--
-- A shortlist submitted from the compute-campaign results page can span MANY
-- sub-jobs, so it cannot be expressed as one source_job_id + candidate_indices
-- (the single-job "web" shape). Introduce a third submission_source,
-- 'campaign', carrying:
--   * source_campaign_id -> the compute_campaigns row it came from
--   * candidate_refs jsonb -> [{"job_id","index"}, ...] across sub-jobs
--
-- Additive and non-destructive: new nullable columns + widened CHECK
-- constraints. Legacy 'web' (single-job) and 'api' rows are unaffected.

-- ON DELETE CASCADE (not SET NULL): a 'campaign' row's shape CHECK requires
-- source_campaign_id NOT NULL, so nulling it on delete would make the row
-- unsatisfiable and abort the delete. Cascading also keeps account-deletion
-- (auth.users -> compute_campaigns and -> lab_campaigns both CASCADE)
-- order-independent.
ALTER TABLE public.lab_campaigns
    ADD COLUMN IF NOT EXISTS source_campaign_id uuid
        REFERENCES public.compute_campaigns(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS candidate_refs jsonb;

CREATE INDEX IF NOT EXISTS lab_campaigns_source_campaign_idx
    ON public.lab_campaigns(source_campaign_id);

-- Widen the submission_source enum to include 'campaign'.
ALTER TABLE public.lab_campaigns
    DROP CONSTRAINT IF EXISTS lab_campaigns_submission_source_enum;
ALTER TABLE public.lab_campaigns
    ADD CONSTRAINT lab_campaigns_submission_source_enum CHECK (
        submission_source IN ('web', 'api', 'campaign')
    );

-- Widen the shape constraint: a 'campaign' row has no single source_job_id;
-- it references a compute campaign and carries a non-empty candidate_refs array.
ALTER TABLE public.lab_campaigns
    DROP CONSTRAINT IF EXISTS lab_campaigns_submission_source_shape;
ALTER TABLE public.lab_campaigns
    ADD CONSTRAINT lab_campaigns_submission_source_shape CHECK (
        (submission_source = 'web' AND source_job_id IS NOT NULL)
        OR
        (submission_source = 'api' AND source_job_id IS NULL AND sequences IS NOT NULL)
        OR
        (submission_source = 'campaign'
            AND source_campaign_id IS NOT NULL
            AND jsonb_typeof(candidate_refs) = 'array'
            AND jsonb_array_length(candidate_refs) > 0)
    );

-- Relax the nonempty-indices check: a 'campaign' row keeps candidate_indices
-- empty (its shortlist lives in candidate_refs). Either signal being present
-- satisfies the row.
ALTER TABLE public.lab_campaigns
    DROP CONSTRAINT IF EXISTS lab_campaigns_candidate_indices_nonempty;
ALTER TABLE public.lab_campaigns
    DROP CONSTRAINT IF EXISTS lab_campaigns_shortlist_nonempty;
ALTER TABLE public.lab_campaigns
    ADD CONSTRAINT lab_campaigns_shortlist_nonempty CHECK (
        array_length(candidate_indices, 1) > 0
        OR (jsonb_typeof(candidate_refs) = 'array'
            AND jsonb_array_length(candidate_refs) > 0)
    );
