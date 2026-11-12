-- 0047_timeout_with_delivery_failure_class.sql
--
-- Adds one value to tool_jobs.failure_class: 'timeout_with_delivery'.
--
-- Context: Leo 2026-11-04 decided a run that times out keeps the designs
-- it had already streamed (PR #489), and then that such a run should be
-- BILLED for the GPU it consumed, while a timeout that delivered nothing
-- stays fully refunded. One class could not express both, because the
-- settle router branches purely on failure_class
-- (shared/jobs.py::_settle_wallet_hold_for_completed_job) and the page's
-- refund flag is set from membership of _REFUNDED_FAILURE_CLASSES
-- (shared/jobs.py::failure_notice, the `refunded =` assignment -- which
-- also flags a run that recorded no GPU seconds, whatever its class), so a
-- billed class has to be a class that set does not hold. Hence the billed
-- case gets its own class rather than a billed row claiming
-- 'no_progress_timeout'.
--
--   timeout_with_delivery -> charge actual GPU consumed (settle_hold /
--                            settle_live_run without refund): the same
--                            billed arm user_cancelled takes, though the
--                            two diverge at zero GPU seconds, where
--                            user_cancelled has a release_hold fast path
--                            and this class settles a zero charge instead
--                            (same money, different ledger row). The run
--                            hit its time limit but handed the user real
--                            designs; it is a partial delivery, never a
--                            complete one misfiled, because a run whose
--                            work provably finished is finalised as
--                            'succeeded' first
--                            (shared/job_recovery.py::recover_stuck_job_result).
--
--   no_progress_timeout   -> unchanged: full refund. Still the class for
--                            every timeout that delivered nothing, and
--                            also for one that delivered something but
--                            that Modal reported as failed or errored at
--                            all -- a crash, or a stall on our own side,
--                            is never billed off designs its own run left
--                            behind (the ``reclassify`` argument
--                            timeout_stuck_job passes to
--                            shared/jobs.py::_keep_streamed_designs).
--
-- Status stays 'timeout' for both. Only the money differs.
--
-- Must be applied BEFORE the PR #489 code deploys. Until it is, the
-- second CAS that writes the new class fails the CHECK, _cas_update
-- returns False (shared/jobs.py::_cas_update swallows and logs), and the
-- row keeps 'no_progress_timeout' -> full refund. The designs are written
-- by a separate earlier CAS, so they survive either way.
--
-- Safe to re-run. No backfill: no historical row can be reclassified,
-- because whether a past timeout delivered designs is not recoverable
-- from the row once its result was discarded.

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
            'timeout_with_delivery',
            'unclassified'
        )
    );
