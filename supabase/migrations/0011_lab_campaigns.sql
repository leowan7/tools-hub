-- Ranomics tools-hub — Phase 3 wet-lab handoff (tools-hub -> Ranomics CRO)
-- Safe to re-run.
--
-- Table: public.lab_campaigns
--   Mirrors the scout_handoffs pattern but directionally flipped: a user
--   shortlists candidates on a finished tool_jobs run and submits them to
--   the Ranomics wet-lab team for a yeast display / DMS / mammalian display
--   scoping conversation. The submission copies the shortlisted PDBs into
--   the lab-campaigns/ bucket so Ranomics staff have durable access that
--   does not depend on the source job's payload lifecycle.
--
-- Bucket: lab-campaigns
--   Object paths follow "{campaign_id}/{candidate_idx}.pdb". Not public —
--   staff read via the service role, the submitter reads via RLS keyed on
--   lab_campaigns.user_id.
--
-- RLS
--   - Users SELECT their own campaign rows.
--   - Users INSERT their own campaign rows (user_id must match auth.uid()).
--   - No UPDATE / DELETE from authenticated; only the service role (used by
--     /admin/campaigns) mutates status + reviewed_at + notes_internal.

CREATE TABLE IF NOT EXISTS public.lab_campaigns (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    source_job_id           uuid NOT NULL REFERENCES public.tool_jobs(id) ON DELETE RESTRICT,
    candidate_indices       integer[] NOT NULL DEFAULT ARRAY[]::integer[],
    target_name             text NOT NULL,
    target_context          text NOT NULL DEFAULT '',
    assay_type              text NOT NULL CHECK (assay_type IN ('yeast_display', 'mammalian_display', 'dms')),
    affinity_goal_kd_nm     numeric,
    timeline_weeks          integer,
    budget_band             text NOT NULL CHECK (budget_band IN ('pilot', 'sprint', 'custom')),
    status                  text NOT NULL DEFAULT 'submitted'
                            CHECK (status IN ('submitted', 'reviewed', 'scoped', 'accepted', 'declined')),
    ranomics_contact        text,
    notes_internal          text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    reviewed_at             timestamptz,
    CONSTRAINT lab_campaigns_candidate_indices_nonempty CHECK (array_length(candidate_indices, 1) > 0)
);

CREATE INDEX IF NOT EXISTS lab_campaigns_user_idx
    ON public.lab_campaigns(user_id);
CREATE INDEX IF NOT EXISTS lab_campaigns_status_idx
    ON public.lab_campaigns(status);
CREATE INDEX IF NOT EXISTS lab_campaigns_created_at_idx
    ON public.lab_campaigns(created_at DESC);

ALTER TABLE public.lab_campaigns ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS lab_campaigns_self_read ON public.lab_campaigns;
CREATE POLICY lab_campaigns_self_read ON public.lab_campaigns
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

DROP POLICY IF EXISTS lab_campaigns_self_insert ON public.lab_campaigns;
CREATE POLICY lab_campaigns_self_insert ON public.lab_campaigns
    FOR INSERT TO authenticated
    WITH CHECK (user_id = auth.uid());


-- Storage bucket for the per-campaign PDB payload copies.
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'lab-campaigns',
    'lab-campaigns',
    false,  -- NOT public — staff read via service role, submitter via RLS
    20971520,  -- 20 MB per object — headroom above the tool-inputs cap
    ARRAY[
        'text/plain',
        'chemical/x-pdb',
        'chemical/x-cif',
        'chemical/x-mmcif',
        'application/octet-stream'
    ]
)
ON CONFLICT (id) DO UPDATE SET
    file_size_limit = EXCLUDED.file_size_limit,
    allowed_mime_types = EXCLUDED.allowed_mime_types;


-- Storage RLS: submitter reads their own campaign's objects.
-- Path convention: "{campaign_id}/{filename}" — we look up the campaign by
-- the folder prefix and gate on its user_id.

DROP POLICY IF EXISTS lab_campaigns_storage_select_own ON storage.objects;
CREATE POLICY lab_campaigns_storage_select_own ON storage.objects
    FOR SELECT TO authenticated
    USING (
        bucket_id = 'lab-campaigns'
        AND EXISTS (
            SELECT 1
            FROM public.lab_campaigns c
            WHERE c.id::text = (storage.foldername(name))[1]
              AND c.user_id = auth.uid()
        )
    );
