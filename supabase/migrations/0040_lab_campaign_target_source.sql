-- Ranomics tools-hub — target-wide shortlist -> lab handoff. Safe to re-run.
--
-- The exact shape 0037 introduced, one level up. A shortlist submitted from
-- /targets/<id> spans many sub-jobs of many COMPUTE CAMPAIGNS (and the
-- target's standalone jobs), so it cannot be expressed as one
-- source_campaign_id either. Introduce a fourth submission_source, 'target',
-- carrying:
--   * source_target_id -> the design_targets row it came from
--   * candidate_refs jsonb -> [{"job_id","index"}, ...] across tools
--
-- candidate_refs is REUSED, not duplicated: 0037 deliberately keyed it on
-- (job_id, index) rather than on any campaign-relative position, so the same
-- column already addresses a design pooled from any number of parents. That is
-- why this migration adds one pointer column and no new payload column, and
-- why static/js/candidate_table.js needs no change to feed it.
--
-- WHAT THIS DOES NOT IMPORT. 0034:14-17 forbids merging the compute product
-- into the wet-lab FSM. source_target_id is a POINTER, the same class of
-- column 0037 added — no status, no money, no chunking, no concurrency
-- crosses this line. lab_campaigns keeps its own short status enum and its own
-- lifecycle.
--
-- Additive and non-destructive: one new nullable column + widened CHECK
-- constraints. Legacy 'web', 'api' and 'campaign' rows are unaffected.

-- ON DELETE CASCADE (not SET NULL), for the two reasons 0037 records: a
-- 'target' row's shape CHECK requires source_target_id NOT NULL, so nulling it
-- on delete would make the row unsatisfiable and abort the delete; and
-- cascading keeps account deletion (auth.users -> design_targets and ->
-- lab_campaigns both CASCADE) order-independent.
--
-- The safety of CASCADE here rests on a UI promise, so state it: the product
-- must never offer a USER-FACING hard delete of a target. What exists today:
--
--   * archive_target / unarchive_target -- the soft path, and the only one
--     any route calls.
--   * _delete_target_row (shared/targets.py) -- a real DELETE, but reachable
--     only as creation rollback. TWO call sites, both inside create_target and
--     both after the row is inserted: the upload raises, OR the upload
--     SUCCEEDS and the follow-up _update_target that points the row at those
--     bytes does not. (An earlier draft of this line said "when the upload
--     fails", which is true of the first site and false of the second.)
--     Either way the target never became usable, and no lab_campaigns row can
--     reference a target that never finished being created, so this cascade
--     has nothing to destroy.
--   * account deletion (auth.users -> design_targets -> lab_campaigns, both
--     CASCADE), where cascading is the wanted behaviour.
--
-- An earlier draft of this comment said "no delete path" flatly, which is the
-- kind of claim that stops being checked (register item A-3). The rule is
-- about REACHABILITY, not about the absence of a DELETE statement: if a hard
-- delete ever becomes reachable from a target that has been shortlisted, it
-- silently destroys paid CRO scoping requests.
ALTER TABLE public.lab_campaigns
    ADD COLUMN IF NOT EXISTS source_target_id uuid
        REFERENCES public.design_targets(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS lab_campaigns_source_target_idx
    ON public.lab_campaigns(source_target_id);

-- Widen the submission_source enum to include 'target'.
ALTER TABLE public.lab_campaigns
    DROP CONSTRAINT IF EXISTS lab_campaigns_submission_source_enum;
ALTER TABLE public.lab_campaigns
    ADD CONSTRAINT lab_campaigns_submission_source_enum CHECK (
        submission_source IN ('web', 'api', 'campaign', 'target')
    );

-- Widen the shape constraint: a 'target' row has no single source_job_id and
-- no single source_campaign_id; it references a design target and carries a
-- non-empty candidate_refs array. The other three arms are reproduced verbatim
-- from 0037 because ADD CONSTRAINT replaces the whole predicate.
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
        OR
        (submission_source = 'target'
            AND source_target_id IS NOT NULL
            AND jsonb_typeof(candidate_refs) = 'array'
            AND jsonb_array_length(candidate_refs) > 0)
    );

-- lab_campaigns_shortlist_nonempty is NOT restated here. 0037 already relaxed
-- it to "candidate_indices non-empty OR candidate_refs non-empty", and a
-- 'target' row satisfies the second arm exactly as a 'campaign' row does.
-- Re-adding it would be a no-op that invites the two copies to drift.
