-- Ranomics tools-hub — self-serve compute campaigns ("Campaigns")
-- Phase 1 of docs/COMPUTE-CAMPAIGNS-PLAN.md. Safe to re-run (idempotent).
--
-- Purpose
--   A campaign is a large design request (up to ~20k designs) that the
--   system auto-splits into many ordinary tool_jobs sub-jobs, each sized
--   to fit one GPU container's timeout, fanned out on Modal's autoscaler
--   with server-side admission control. This table is the coordinator
--   row; the sub-jobs stay ordinary tool_jobs rows reaching terminal
--   state ONLY via the existing poll/webhook/heartbeat/cancel/sweeper
--   writers. The campaign layer is READ + LAUNCH + RECONCILE and never
--   writes a child's terminal state.
--
--   This is the SELF-SERVE COMPUTE campaign. It is deliberately separate
--   from public.lab_campaigns (migration 0011), which is the wet-lab CRO
--   sales handoff with an incompatible FSM, RLS, and (manual/off-platform)
--   billing model. Do not overload that table.
--
-- Billing model (see plan)
--   No escrow debit. compute_campaigns.budget_usd is the authorized
--   ceiling; reserved_usd is a DERIVED admission counter (sum of open
--   child holds); each sub-job places a NORMAL per-child wallet hold via
--   the unchanged reserve_hold path and is settled by the unchanged
--   settle path. So balance == SUM(wallet_transactions.amount_usd) holds
--   automatically and delivered-only billing falls out for free.
--
-- RLS
--   Self-read policy so a signed-in user can poll their own campaign via
--   the anon key. All writes happen from the Flask server with the
--   service-role key (bypasses RLS), mirroring tool_jobs (0005).

CREATE TABLE IF NOT EXISTS public.compute_campaigns (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name                text,
    tool                text NOT NULL,
    preset              text NOT NULL,
    -- Target PDB is staged to tool-inputs storage ONCE and shared by
    -- every chunk (do not re-upload per chunk). storage_path is the
    -- object key; the driver re-mints a fresh presigned URL per wave.
    target_pdb_id       text,
    target_storage_path text,
    target_name         text,
    -- Shared per-chunk tool params (minus num_designs and underscore
    -- keys) so every wave rebuilds an identical payload shape.
    params              jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_designs   integer NOT NULL CHECK (requested_designs > 0),
    chunk_size          integer NOT NULL CHECK (chunk_size > 0),
    total_subjobs       integer NOT NULL CHECK (total_subjobs > 0),
    concurrency_target  integer NOT NULL DEFAULT 20 CHECK (concurrency_target > 0),
    max_attempts        integer NOT NULL DEFAULT 2 CHECK (max_attempts >= 1),
    status              text NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'draft', 'funded', 'running', 'completing',
            'completed', 'completed_with_failures', 'failed', 'cancelled'
        )),
    -- Money columns. budget_usd is the authorized ceiling; reserved_usd
    -- is the derived admission counter (sum of currently-open child
    -- holds); spent_usd / refunded_usd are tick-reconciled advisory
    -- rollups (source of truth stays the wallet ledger).
    budget_usd          numeric(12, 4) NOT NULL DEFAULT 0 CHECK (budget_usd >= 0),
    reserved_usd        numeric(12, 4) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
    spent_usd           numeric(12, 4) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
    refunded_usd        numeric(12, 4) NOT NULL DEFAULT 0 CHECK (refunded_usd >= 0),
    created_at          timestamptz NOT NULL DEFAULT now(),
    confirmed_at        timestamptz,
    started_at          timestamptz,
    completed_at        timestamptz,
    last_tick_at        timestamptz
);

CREATE INDEX IF NOT EXISTS compute_campaigns_user_created_idx
    ON public.compute_campaigns (user_id, created_at DESC);

-- Cron work queue: the driver only ever scans campaigns that are still
-- in flight, so a partial index keeps the scan cheap.
CREATE INDEX IF NOT EXISTS compute_campaigns_active_idx
    ON public.compute_campaigns (status)
    WHERE status IN ('funded', 'running', 'completing');

ALTER TABLE public.compute_campaigns ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS compute_campaigns_self_read ON public.compute_campaigns;
CREATE POLICY compute_campaigns_self_read ON public.compute_campaigns
    FOR SELECT USING (auth.uid() = user_id);


-- ---------------------------------------------------------------------------
-- Sub-job linkage on tool_jobs
-- ---------------------------------------------------------------------------
-- A sub-job belongs to exactly one campaign, so a nullable FK column
-- beats a join table. chunk_index identifies which slice of the request
-- this row covers; attempt distinguishes a retry sibling from its
-- original. ON DELETE SET NULL so deleting a campaign never cascades away
-- the historical job rows (they keep their own wallet ledger).

ALTER TABLE public.tool_jobs
    ADD COLUMN IF NOT EXISTS campaign_id uuid;

ALTER TABLE public.tool_jobs
    ADD COLUMN IF NOT EXISTS chunk_index integer;

ALTER TABLE public.tool_jobs
    ADD COLUMN IF NOT EXISTS attempt integer DEFAULT 1;

-- Add the FK constraint separately + guarded so re-runs do not error and
-- an older DB missing the column still applies the column adds above.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'tool_jobs_campaign_id_fkey'
    ) THEN
        ALTER TABLE public.tool_jobs
            ADD CONSTRAINT tool_jobs_campaign_id_fkey
            FOREIGN KEY (campaign_id)
            REFERENCES public.compute_campaigns(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

-- The aggregate-progress query is exactly COUNT(*) ... GROUP BY status
-- over one campaign's children, so a partial (campaign_id, status) index
-- is the right shape. Uncategorized single jobs are not indexed here.
CREATE INDEX IF NOT EXISTS tool_jobs_campaign_status_idx
    ON public.tool_jobs (campaign_id, status)
    WHERE campaign_id IS NOT NULL;

-- DB-enforced sub-job idempotency: no chunk_index/attempt pair is ever
-- submitted twice for a campaign, even if two drivers race. INSERT with
-- ON CONFLICT DO NOTHING relies on this. Partial so single jobs (all
-- three columns NULL) are exempt.
CREATE UNIQUE INDEX IF NOT EXISTS tool_jobs_campaign_chunk_uniq
    ON public.tool_jobs (campaign_id, chunk_index, attempt)
    WHERE campaign_id IS NOT NULL;

-- Linkage integrity: a campaign sub-job MUST carry a non-NULL chunk_index
-- and attempt, or the partial UNIQUE(campaign_id, chunk_index, attempt)
-- index above cannot enforce single-submission (SQL NULLs are never equal,
-- so a NULL chunk_index would let duplicate chunks slip past
-- ON CONFLICT DO NOTHING). The Python create_job path always supplies them
-- and attempt has DEFAULT 1; this constraint makes the guarantee airtight
-- at the DB level. Existing single-job rows (campaign_id NULL) all pass.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'tool_jobs_campaign_linkage_ck'
    ) THEN
        ALTER TABLE public.tool_jobs
            ADD CONSTRAINT tool_jobs_campaign_linkage_ck
            CHECK (
                campaign_id IS NULL
                OR (chunk_index IS NOT NULL AND attempt IS NOT NULL)
            );
    END IF;
END
$$;

-- NOTE: intentionally NOT re-declaring the tool_jobs.status CHECK here.
-- Migration 0012 already widened it to include 'cancelled', so campaign
-- cancel is covered; 0034 leaves the constraint untouched. Do not add a
-- stricter status CHECK in this migration.
