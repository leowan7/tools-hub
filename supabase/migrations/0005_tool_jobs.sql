-- Ranomics tools-hub — GPU tool job records
-- Stream C (Wave-2 launch prep). Safe to re-run.
--
-- Purpose
--   Every submission to a GPU tool (BindCraft, RFantibody, BoltzGen,
--   PXDesign, and future D-series atomics) records a row here so the
--   status page, credits reconciliation, and a later ops audit all point
--   at the same ledger. This is the tools-hub analogue to the Kendrew
--   backend's ``jobs`` table, scoped to the much simpler semantics we
--   need here: status + inline result payload, no multi-chunk session
--   logic.
--
-- Status lifecycle
--   pending     row just inserted; Modal function call not yet observed
--   running     Modal function is executing (first poll returned timeout)
--   succeeded   inline result parsed, credits debited
--   failed      Modal raised or run_pipeline wrote FAILED
--   timeout     exceeded preset's timeout without returning
--
-- RLS
--   Enabled with a self-read policy so a signed-in user can poll their
--   own job via the anon key. Writes happen from the Flask server with
--   the service-role key (bypasses RLS).

CREATE TABLE IF NOT EXISTS public.tool_jobs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    tool                    text NOT NULL,
    preset                  text NOT NULL,
    status                  text NOT NULL
        CHECK (status IN ('pending','running','succeeded','failed','timeout')),
    inputs                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    result                  jsonb,
    error                   jsonb,
    credits_cost            integer NOT NULL CHECK (credits_cost >= 0),
    modal_function_call_id  text,
    -- Shared secret stored at submission time; compared by the Modal
    -- callback webhook to reject spoofed POSTs. 32 random bytes hex-encoded.
    job_token               text NOT NULL,
    gpu_seconds_used        integer,
    created_at              timestamptz NOT NULL DEFAULT now(),
    started_at              timestamptz,
    completed_at            timestamptz
);

CREATE INDEX IF NOT EXISTS tool_jobs_user_created_idx
    ON public.tool_jobs (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS tool_jobs_fc_idx
    ON public.tool_jobs (modal_function_call_id);

CREATE INDEX IF NOT EXISTS tool_jobs_status_idx
    ON public.tool_jobs (status) WHERE status IN ('pending','running');

ALTER TABLE public.tool_jobs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tool_jobs_self_read ON public.tool_jobs;
CREATE POLICY tool_jobs_self_read ON public.tool_jobs
    FOR SELECT USING (auth.uid() = user_id);
