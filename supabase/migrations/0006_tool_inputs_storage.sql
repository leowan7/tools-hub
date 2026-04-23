-- Ranomics tools-hub — Storage bucket for user-uploaded GPU inputs
-- Stream C (Wave-2 launch prep). Safe to re-run.
--
-- Bucket: tool-inputs
--   Per-user uploaded files (PDB, CIF, FASTA) that a GPU pipeline needs
--   to read. Object paths follow "{user_id}/{job_id}/{filename}" so the
--   RLS owner-check policy is a simple string-prefix match.
--
-- RLS
--   - Users can INSERT/SELECT their own objects via the authenticated role.
--   - The service role (used by tools-hub server + Modal presigned URLs)
--     bypasses RLS. This is how we hand Kendrew pipelines a readable URL
--     without exposing the object to other authenticated users.
--
-- Retention
--   No automatic TTL on this migration. A sweeper deletes rows older than
--   30 days in a later wave. File-size cap enforced at the application
--   layer (shared.storage) before the upload is accepted.

-- Create the bucket if it does not already exist.
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'tool-inputs',
    'tool-inputs',
    false,  -- NOT public — access is gated by presigned URLs and RLS
    20971520,  -- 20 MB per object (PDB files sit well under 1 MB; headroom for CIF + FASTA bundles)
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
-- storage.objects has its own RLS flag toggled on at the Supabase level by
-- default; these policies add our allow rules.

DROP POLICY IF EXISTS tool_inputs_insert_own ON storage.objects;
CREATE POLICY tool_inputs_insert_own ON storage.objects
    FOR INSERT TO authenticated
    WITH CHECK (
        bucket_id = 'tool-inputs'
        AND (storage.foldername(name))[1] = auth.uid()::text
    );

DROP POLICY IF EXISTS tool_inputs_select_own ON storage.objects;
CREATE POLICY tool_inputs_select_own ON storage.objects
    FOR SELECT TO authenticated
    USING (
        bucket_id = 'tool-inputs'
        AND (storage.foldername(name))[1] = auth.uid()::text
    );

DROP POLICY IF EXISTS tool_inputs_delete_own ON storage.objects;
CREATE POLICY tool_inputs_delete_own ON storage.objects
    FOR DELETE TO authenticated
    USING (
        bucket_id = 'tool-inputs'
        AND (storage.foldername(name))[1] = auth.uid()::text
    );
