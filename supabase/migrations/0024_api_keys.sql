-- Ranomics tools-hub — Platform API keys
-- Safe to re-run.
--
-- Purpose
--   Bearer-token authentication for the agent-facing Platform API
--   (/api/v1/*). One row per active API key issued to a user via the
--   /account/api-keys page. The plaintext token is only ever exposed
--   once at creation time — what we store is its SHA-256 digest. The
--   first 12 chars of the plaintext (e.g. "rk_live_abcd") are saved
--   to ``prefix`` so the UI can show the user something recognisable
--   alongside the per-key label.
--
-- Token format
--   ``rk_live_<22 url-safe random>`` issued by shared.api_keys.mint_token.
--   The prefix conventions match Stripe (rk_live_ / rk_test_).
--
-- RLS
--   - Users may SELECT their own non-revoked keys (the UI lists them).
--   - Users may INSERT their own rows via /account/api-keys.
--   - UPDATE/DELETE only via service role (revocation goes through
--     /account/api-keys → service client).
--
-- Lookup path
--   /api/v1/* requests resolve the Bearer token via the service role:
--     SELECT * FROM api_keys WHERE hashed_token = sha256($1) AND revoked_at IS NULL
--   Constant-time on the index; never touches the user-scoped RLS path.

CREATE TABLE IF NOT EXISTS public.api_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    hashed_token  text NOT NULL UNIQUE,
    -- First 12 chars of the plaintext token (e.g. "rk_live_abcd").
    -- Used as a stable display handle in the UI and audit log.
    prefix        text NOT NULL,
    role          text NOT NULL CHECK (role IN ('member', 'viewer')),
    label         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

CREATE INDEX IF NOT EXISTS api_keys_user_idx
    ON public.api_keys (user_id, created_at DESC);

-- Active-keys partial index for the dashboard listing.
CREATE INDEX IF NOT EXISTS api_keys_user_active_idx
    ON public.api_keys (user_id)
    WHERE revoked_at IS NULL;

ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS api_keys_self_read ON public.api_keys;
CREATE POLICY api_keys_self_read ON public.api_keys
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

DROP POLICY IF EXISTS api_keys_self_insert ON public.api_keys;
CREATE POLICY api_keys_self_insert ON public.api_keys
    FOR INSERT TO authenticated
    WITH CHECK (user_id = auth.uid());

-- No UPDATE / DELETE policies — revocation runs via service role from
-- the /account/api-keys page after a confirm dialog.
