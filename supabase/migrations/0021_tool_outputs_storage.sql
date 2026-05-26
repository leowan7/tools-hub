-- Ranomics tools-hub — Storage bucket for GPU-pipeline output files
-- (e.g. designed PDBs from pilot/full tier). Safe to re-run.
--
-- Bucket: tool-outputs
--   Per-job output files produced by Modal pipelines and POSTed back via
--   presigned PUT URLs. Object paths follow
--   "{user_id}/{job_id}/designs/{filename}" so the same RLS owner-check
--   policy as tool-inputs applies (string-prefix match on first
--   folder component).
--
-- Why this bucket exists
--   Pilot-tier (and eventually full-tier) jobs emit N candidate PDBs.
--   Inlining all N as base64 in tool_jobs.result.candidates[].pdb_content_b64
--   bloats the row and bumps up against Modal webhook payload limits.
--   Instead, tools-hub hands the pipeline presigned PUT URLs (via the
--   /api/upload-urls/<job_id> endpoint) and the pipeline POSTs each PDB
--   into this bucket. The results page resolves pdb_key -> presigned
--   GET URL at render time.
--
-- RLS
--   - Users can SELECT/INSERT/DELETE their own objects via the
--     authenticated role.
--   - The service role (tools-hub server) bypasses RLS to mint
--     presigned URLs for both directions.
--   - Modal pipelines never authenticate to Supabase directly — they
--     PUT to a presigned URL that already carries the auth.
--
-- Retention
--   No automatic TTL on this migration. Same sweeper as tool-inputs
--   (later wave) will reap objects older than 30 days. File-size cap
--   enforced at the application layer (shared.storage) and at the
--   bucket level (file_size_limit below).

-- Create the bucket if it does not already exist.
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'tool-outputs',
    'tool-outputs',
    false,  -- NOT public — access is gated by presigned URLs and RLS
    20971520,  -- 20 MB per object (designed PDBs are <1 MB each; headroom for CIF + multimer outputs)
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


-- Owner-scoped policies. ``auth.uid()::text`` prefix match gates read/insert.
-- Same shape as 0006 for tool-inputs.

DROP POLICY IF EXISTS tool_outputs_insert_own ON storage.objects;
CREATE POLICY tool_outputs_insert_own ON storage.objects
    FOR INSERT TO authenticated
    WITH CHECK (
        bucket_id = 'tool-outputs'
        AND (storage.foldername(name))[1] = auth.uid()::text
    );

DROP POLICY IF EXISTS tool_outputs_select_own ON storage.objects;
CREATE POLICY tool_outputs_select_own ON storage.objects
    FOR SELECT TO authenticated
    USING (
        bucket_id = 'tool-outputs'
        AND (storage.foldername(name))[1] = auth.uid()::text
    );

DROP POLICY IF EXISTS tool_outputs_delete_own ON storage.objects;
CREATE POLICY tool_outputs_delete_own ON storage.objects
    FOR DELETE TO authenticated
    USING (
        bucket_id = 'tool-outputs'
        AND (storage.foldername(name))[1] = auth.uid()::text
    );
