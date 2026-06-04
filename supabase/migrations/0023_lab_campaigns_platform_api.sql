-- Ranomics tools-hub — Platform API columns on lab_campaigns
-- Safe to re-run.
--
-- Purpose
--   Today every lab_campaigns row originates from a finished tool_jobs
--   run (the user shortlists candidates from a job they ran in the web
--   UI). The Platform API needs to accept submissions where the caller
--   already ran compute elsewhere and is handing us a dict of sequences
--   plus library-design context. Two structural changes:
--
--     1. source_job_id becomes nullable. Existing web-form submissions
--        still populate it; API submissions leave it NULL.
--
--     2. A new sequences jsonb + library_design jsonb pair carries the
--        API-direct payload. A new submission_source enum disambiguates
--        the two flows for the admin UI.
--
--   Three orthogonal additions support the API contract itself:
--
--     - results_status (none | partial | all) mirrors Adaptyv's enum so
--       agents poll a single boolean before pulling the heavier results
--       endpoint.
--     - webhook_url + idempotency_key support the standard agent
--       integration patterns (event-driven notifications, safe retries).
--     - last_transition_at + status_log give us an append-only timeline
--       for the lifecycle states the API exposes.
--
--   The status CHECK is extended additively: every existing value
--   ('submitted', 'reviewed', 'scoped', 'accepted', 'declined') is still
--   accepted, plus the longer Adaptyv-compatible FSM the API uses
--   (Draft → ... → Done). Web UI keeps writing the short set; API path
--   uses the longer set. A future migration can collapse them once
--   product is sure they should converge.
--
-- Reversibility
--   All adds are nullable or default-valued; rolling back the migration
--   needs only DROP COLUMN statements. The source_job_id NOT NULL drop
--   is reversible by re-adding the constraint after deleting any rows
--   with NULL source_job_id (i.e. API-direct rows, which only exist
--   when ENABLE_PLATFORM_API is on).

-- ---------------------------------------------------------------------------
-- New columns
-- ---------------------------------------------------------------------------

ALTER TABLE public.lab_campaigns
    ADD COLUMN IF NOT EXISTS name              text,
    ADD COLUMN IF NOT EXISTS webhook_url       text,
    ADD COLUMN IF NOT EXISTS idempotency_key   text,
    ADD COLUMN IF NOT EXISTS sequences         jsonb,
    ADD COLUMN IF NOT EXISTS library_design    jsonb,
    ADD COLUMN IF NOT EXISTS submission_source text NOT NULL DEFAULT 'web',
    ADD COLUMN IF NOT EXISTS results_status    text NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS last_transition_at timestamptz,
    ADD COLUMN IF NOT EXISTS status_log        jsonb NOT NULL DEFAULT '[]'::jsonb;

-- ---------------------------------------------------------------------------
-- Allow API-direct submissions to omit source_job_id
-- ---------------------------------------------------------------------------

ALTER TABLE public.lab_campaigns
    ALTER COLUMN source_job_id DROP NOT NULL;

-- API rows have no source_job_id but always have a sequences payload.
-- Web rows always have a source_job_id and never use sequences.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'lab_campaigns_submission_source_shape'
    ) THEN
        ALTER TABLE public.lab_campaigns
            ADD CONSTRAINT lab_campaigns_submission_source_shape CHECK (
                (submission_source = 'web' AND source_job_id IS NOT NULL)
                OR
                (submission_source = 'api' AND source_job_id IS NULL AND sequences IS NOT NULL)
            );
    END IF;
END$$;

-- Disambiguate the enum.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'lab_campaigns_submission_source_enum'
    ) THEN
        ALTER TABLE public.lab_campaigns
            ADD CONSTRAINT lab_campaigns_submission_source_enum CHECK (
                submission_source IN ('web', 'api')
            );
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'lab_campaigns_results_status_enum'
    ) THEN
        ALTER TABLE public.lab_campaigns
            ADD CONSTRAINT lab_campaigns_results_status_enum CHECK (
                results_status IN ('none', 'partial', 'all')
            );
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- Extend the status CHECK additively.
--
-- We drop the original CHECK and re-add it covering both the legacy
-- web-form values and the new API FSM values. Existing rows are not
-- touched. Web UI code keeps using the old values; API code uses the
-- new ones.
-- ---------------------------------------------------------------------------

ALTER TABLE public.lab_campaigns
    DROP CONSTRAINT IF EXISTS lab_campaigns_status_check;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'lab_campaigns_status_check2'
    ) THEN
        ALTER TABLE public.lab_campaigns
            ADD CONSTRAINT lab_campaigns_status_check2 CHECK (
                status IN (
                    -- legacy web-form set
                    'submitted', 'reviewed', 'scoped', 'accepted', 'declined',
                    -- API FSM set (Adaptyv-compatible)
                    'Draft',
                    'WaitingForConfirmation',
                    'QuoteSent',
                    'WaitingForMaterials',
                    'LibraryConstruction',
                    'Sorting',
                    'NGS',
                    'DataAnalysis',
                    'InReview',
                    'Done',
                    'Cancelled'
                )
            );
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- Idempotency uniqueness — partial so existing NULL rows are unaffected.
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS lab_campaigns_user_idempotency_idx
    ON public.lab_campaigns (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Useful for the API "list my experiments" path.
CREATE INDEX IF NOT EXISTS lab_campaigns_user_results_status_idx
    ON public.lab_campaigns (user_id, results_status, created_at DESC);
