-- Ranomics tools-hub — optional expiry date on Platform API keys.
-- Safe to re-run (idempotent).
--
-- What this adds
--   * api_keys.expires_at timestamptz, NULL by default. NULL means the key
--     never expires, so every existing key keeps working unchanged.
--     /account/api-keys sets it at creation (30 / 90 / 365 days) via
--     shared/api_keys.py mint_token; resolve_token refuses a key whose
--     expires_at has passed, the same way it refuses a revoked key.
--
-- Scope (full / read-only) needs no column: it is the existing
-- api_keys.role ('member' = full, 'viewer' = read-only), see
-- shared/api_keys.py SCOPE_BY_ROLE.
--
-- Apply via the Supabase SQL editor or `supabase db push` BEFORE deploying
-- the matching app change: mint_token's active-key count selects
-- expires_at, and that query fails until the column exists.

ALTER TABLE public.api_keys
    ADD COLUMN IF NOT EXISTS expires_at timestamptz;
