-- 0029_tool_jobs_failure_class.sql
--
-- Adds a failure_class column to tool_jobs so the wallet settlement
-- path can decide refund vs charge based on a typed classification
-- rather than scraping job.error.bucket strings.
--
-- Context: the tier-collapse PR drops the pilot/full split and shifts
-- the billing model from "hold then settle once at terminal" to a
-- burn-down model where users pay for actual GPU minutes consumed.
-- The classifier drives the settlement branch:
--
--   succeeded             -> charge actual (hold_release surplus or
--                            charge variance up to hard cap)
--   user_cancelled        -> charge actual consumed-so-far. No refund.
--   completed_no_yield    -> charge actual. Pipeline ran cleanly but
--                            produced 0 passing designs. User pays.
--   safety_kill           -> charge actual, clamped to hard cap.
--                            Already existed as overrun_safety_kill
--                            error bucket; classifier promotes it.
--   infra_crash           -> full refund. Modal pod OOM / container
--                            died / infra-side failure that consumed
--                            time but produced no useful work.
--   tool_error            -> full refund. Our docker image bug, broken
--                            venv, missing dependency, or any other
--                            failure where the subprocess itself
--                            errored from our side.
--   preflight_miss        -> full refund. Preflight accepted the input
--                            but the subprocess died because of it.
--                            Our preflight is incomplete; we eat it.
--   no_progress_timeout   -> full refund. Subprocess exceeded its
--                            timeout with no heartbeat / progress
--                            signal. Treat as infra-side stall.
--   unclassified          -> default refund. Judgment-case fallback so
--                            ambiguous errors do not bill the user.
--
-- The classifier is set by the Python lifecycle hooks in
-- shared/jobs.py:complete_job and shared/jobs.py:cancel_job. Ops can
-- override via direct UPDATE if a case is mis-classified.
--
-- Migration is safe to re-run. Backfill is best-effort: only the
-- obvious mappings (status -> class) are filled; anything ambiguous
-- stays NULL so ops can review.

ALTER TABLE public.tool_jobs
    ADD COLUMN IF NOT EXISTS failure_class text;

-- Constrain to the known classifier values. NULL is allowed (legacy
-- rows and in-flight jobs that have not been classified yet).
ALTER TABLE public.tool_jobs
    DROP CONSTRAINT IF EXISTS tool_jobs_failure_class_check;

ALTER TABLE public.tool_jobs
    ADD CONSTRAINT tool_jobs_failure_class_check
    CHECK (
        failure_class IS NULL
        OR failure_class IN (
            'succeeded',
            'user_cancelled',
            'completed_no_yield',
            'safety_kill',
            'infra_crash',
            'tool_error',
            'preflight_miss',
            'no_progress_timeout',
            'unclassified'
        )
    );

-- Index for ops queries grouping failures by class over a time window.
CREATE INDEX IF NOT EXISTS tool_jobs_failure_class_created_idx
    ON public.tool_jobs (failure_class, created_at DESC)
    WHERE failure_class IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Backfill: best-effort classifier for historical rows
-- ---------------------------------------------------------------------------
--
-- Only the obvious mappings are filled. status -> class is unambiguous
-- for succeeded / cancelled / timeout / safety_kill. Generic 'failed'
-- rows stay NULL because the bucket usually does not survive to a
-- single refund decision; ops can sweep them manually.

UPDATE public.tool_jobs
   SET failure_class = 'succeeded'
 WHERE failure_class IS NULL
   AND status = 'succeeded';

UPDATE public.tool_jobs
   SET failure_class = 'user_cancelled'
 WHERE failure_class IS NULL
   AND status = 'cancelled';

UPDATE public.tool_jobs
   SET failure_class = 'no_progress_timeout'
 WHERE failure_class IS NULL
   AND status = 'timeout';

UPDATE public.tool_jobs
   SET failure_class = 'safety_kill'
 WHERE failure_class IS NULL
   AND status = 'failed'
   AND error->>'bucket' = 'overrun_safety_kill';
