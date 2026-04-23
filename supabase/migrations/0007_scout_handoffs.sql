-- Ranomics tools-hub — Wave 3C Scout -> Tools handoff
-- Safe to re-run.
--
-- Table: public.scout_handoffs
--   A handoff is a short-lived record created by Epitope Scout when a user
--   clicks "Design binder in Tools" on an epitope row. Scout uploads the
--   target PDB to the shared ``tool-inputs`` bucket under
--   ``{user_id}/scout-handoff-{handoff_id}/{filename}`` and records the
--   chain + hotspot list here. tools-hub reads the row by id when the user
--   lands on the tool form, pre-fills the visible fields, and on submit
--   copies the PDB into the new job's storage path.
--
-- RLS
--   Service role only. Neither scout nor tools-hub exposes this table to
--   the browser; both read/write via the service client.

CREATE TABLE IF NOT EXISTS public.scout_handoffs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    pdb_storage_path    text NOT NULL,
    pdb_filename        text NOT NULL,
    target_chain        text NOT NULL,
    hotspot_residues    integer[] NOT NULL DEFAULT ARRAY[]::integer[],
    scout_job_id        text,
    scout_epitope_id    text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    consumed_at         timestamptz,
    expires_at          timestamptz NOT NULL DEFAULT (now() + interval '2 hours')
);

CREATE INDEX IF NOT EXISTS scout_handoffs_user_idx
    ON public.scout_handoffs(user_id);

ALTER TABLE public.scout_handoffs ENABLE ROW LEVEL SECURITY;

-- No anon/authenticated policies: service role only. Scout uses the
-- service-role key to INSERT, tools-hub uses it to SELECT + UPDATE
-- consumed_at. The DB enforces that only logged-in users whose ids
-- Scout has resolved from auth.users end up as user_id.
