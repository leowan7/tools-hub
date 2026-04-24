-- Ranomics tools-hub — allow cancelled status on tool_jobs
-- Phase 4. Safe to re-run.
--
-- User-initiated cancellation is a distinct outcome from failed/timeout:
-- the user gets a full credit refund (no GPU work was their fault) and
-- the status page renders a quiet "cancelled" pill instead of a loud
-- failure. This migration drops the existing CHECK constraint and
-- re-creates it with 'cancelled' in the allowed set.

ALTER TABLE public.tool_jobs
    DROP CONSTRAINT IF EXISTS tool_jobs_status_check;

ALTER TABLE public.tool_jobs
    ADD CONSTRAINT tool_jobs_status_check
    CHECK (status IN ('pending','running','succeeded','failed','timeout','cancelled'));
