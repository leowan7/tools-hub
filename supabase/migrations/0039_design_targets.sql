-- Ranomics tools-hub — design targets (target-first rework, Phase 1).
-- Safe to re-run (idempotent).
--
-- Purpose
--   A target is one protein structure the user uploads ONCE and then runs
--   many tools against. Today a target is three columns on
--   public.compute_campaigns, staged per campaign under a throwaway
--   ``campaign-{uuid4}`` storage key, so running four tools on the same
--   antigen means four uploads of the same file with nothing linking them.
--   This table makes the target the parent object those runs hang off, so
--   their designs can be fanned back in to one ranked table.
--
--   This is a FREE organizing object, not a SKU. It is deliberately not a
--   revival of the retired ``workspaces`` table (which carried a spend cap
--   and sales routes): there is no money, no quota, and no status FSM here.
--   Billing stays exactly where it is — the prepaid wallet, drained per
--   sub-job hold.
--
-- Storage
--   No new bucket. Targets live in the existing ``tool-inputs`` bucket
--   under ``{user_id}/target-{target_id}/{filename}``, staged through the
--   unchanged upload_input(). Every run created from a target DENORMALIZES
--   that path onto compute_campaigns.target_storage_path, which is what
--   makes this migration additive below the create route: _dispatch_chunk
--   keeps re-minting its presigned URL from the campaign column and never
--   learns that targets exist.
--
-- RLS
--   Self-read policy so a signed-in user can read their own targets via the
--   anon key. All writes happen from the Flask server with the service-role
--   key (bypasses RLS), mirroring tool_jobs (0005) and compute_campaigns
--   (0034).

CREATE TABLE IF NOT EXISTS public.design_targets (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name              text,
    -- 'pdb' for every protein tool; 'sdf' for proteina's ligand_binder
    -- variant, whose RDKit -> chain-A PDB conversion happens in-container.
    kind              text NOT NULL DEFAULT 'pdb'
        CHECK (kind IN ('pdb', 'sdf')),
    -- NULLABLE on purpose. create_target inserts the row first (the id is
    -- needed to build the storage key) and fills this in after staging.
    -- It also stays NULL for proteina's curated-benchmark path, which
    -- legitimately has no uploaded structure.
    storage_path      text,
    filename          text,
    content_type      text,
    -- Content hash of the staged bytes, so a second upload of the same
    -- structure can be offered the existing target instead of silently
    -- splitting one protein's results across two unlinked targets.
    sha256            text,
    byte_size         integer,
    -- DEFAULTS, not constraints. A multi-chain target may want a different
    -- epitope per run, so each run may override these; the override is
    -- persisted on the run.
    target_chain      text,
    hotspot_residues  integer[],
    epitope_residues  integer[],
    -- inspect_pdb_bytes() output: per-chain residue counts and ranges, kept
    -- so the target page and the launch form can render chain choices
    -- without re-downloading and re-parsing the structure.
    chain_summary     jsonb,
    uniprot_accession text,
    source            text,
    notes             text,
    -- Archive is the ONLY user-facing removal. Today's reason: the campaign
    -- driver re-mints a presigned URL from the staged input on every wave, so
    -- deleting a structure out from under a live run breaks every chunk that
    -- has not dispatched. A second reason arrives with Phase 5's
    -- lab_campaigns.source_target_id, which will need ON DELETE CASCADE to
    -- satisfy its shape CHECK, making a hard delete destroy paid CRO scoping
    -- requests too. That column does not exist yet. Hard deletes happen only
    -- via account deletion, where cascading is what you want.
    archived_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_used_at      timestamptz
);

-- The targets list is "my live targets, newest first", so the partial index
-- matches the query exactly and archived rows stay out of it.
CREATE INDEX IF NOT EXISTS design_targets_user_created_idx
    ON public.design_targets (user_id, created_at DESC)
    WHERE archived_at IS NULL;

-- Duplicate-upload lookup. Deliberately NOT unique: re-uploading the same
-- structure is offered the existing target, never forced onto it.
CREATE INDEX IF NOT EXISTS design_targets_user_sha_idx
    ON public.design_targets (user_id, sha256)
    WHERE archived_at IS NULL AND sha256 IS NOT NULL;

ALTER TABLE public.design_targets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS design_targets_self_read ON public.design_targets;
CREATE POLICY design_targets_self_read ON public.design_targets
    FOR SELECT USING (auth.uid() = user_id);


-- ---------------------------------------------------------------------------
-- Linkage on compute_campaigns
-- ---------------------------------------------------------------------------
-- target_id is NULLABLE and stays that way. NOT NULL would break both the
-- existing create flow (a run launched from a plain upload has no target)
-- and proteina's curated-task path (no staged target at all). Historical
-- rows are NOT backfilled; they render as unparented, which is correct.
--
-- launch_group_id ties the N runs created by one multi-tool launch together.
-- It is a plain uuid with no table behind it: the group has no attributes of
-- its own beyond "these were launched together", and a table would need its
-- own RLS and lifecycle for nothing.

ALTER TABLE public.compute_campaigns
    ADD COLUMN IF NOT EXISTS target_id uuid;

ALTER TABLE public.compute_campaigns
    ADD COLUMN IF NOT EXISTS launch_group_id uuid;

-- ON DELETE SET NULL (not CASCADE): deleting a target must never destroy run
-- history or the wallet ledger entries that hang off those runs.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'compute_campaigns_target_id_fkey'
    ) THEN
        ALTER TABLE public.compute_campaigns
            ADD CONSTRAINT compute_campaigns_target_id_fkey
            FOREIGN KEY (target_id)
            REFERENCES public.design_targets(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

-- Phase 1 issues exactly ONE query against these columns: select id where
-- target_id = $1, ordered by id (shared/targets.py::campaign_ids_for_target),
-- which uses only the leading equality column below. The other two indexes
-- are for queries that do not exist yet -- the run strip's status rollup and
-- Phase 2's launch groups. They are cheap and the columns are already here,
-- so they ship now rather than in a later migration; do not read their
-- presence as evidence that something queries them.
CREATE INDEX IF NOT EXISTS compute_campaigns_target_created_idx
    ON public.compute_campaigns (target_id, created_at DESC)
    WHERE target_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS compute_campaigns_target_status_idx
    ON public.compute_campaigns (target_id, status)
    WHERE target_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS compute_campaigns_launch_group_idx
    ON public.compute_campaigns (launch_group_id)
    WHERE launch_group_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- Linkage on tool_jobs
-- ---------------------------------------------------------------------------
-- Two kinds of row need this. A campaign sub-job carries its campaign's
-- target_id so a design is target-attributable without a join through
-- compute_campaigns. A standalone job (an atomic tool form run, or a
-- yardstick re-fold) carries it with campaign_id NULL, which is how those
-- rows join the target's combined table at all.

ALTER TABLE public.tool_jobs
    ADD COLUMN IF NOT EXISTS target_id uuid;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'tool_jobs_target_id_fkey'
    ) THEN
        ALTER TABLE public.tool_jobs
            ADD CONSTRAINT tool_jobs_target_id_fkey
            FOREIGN KEY (target_id)
            REFERENCES public.design_targets(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

-- For the Phase 3 fan-in, which reads a target's standalone succeeded jobs
-- (campaign children arrive via their campaign). NOTHING queries this in
-- Phase 1 -- target_id is write-only here -- so the index is provisioned
-- ahead of its reader rather than serving one.
CREATE INDEX IF NOT EXISTS tool_jobs_target_status_idx
    ON public.tool_jobs (target_id, status)
    WHERE target_id IS NOT NULL;
