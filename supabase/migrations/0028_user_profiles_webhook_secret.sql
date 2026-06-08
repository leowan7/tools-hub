-- 0028_user_profiles_webhook_secret.sql
--
-- CR-01 (fresh-review): a single ``WEBHOOK_SIGNING_SECRET`` env var
-- signs every outbound webhook. Two tenants verifying with that one
-- secret can each forge events the other will accept as authentic.
-- The fix: per-tenant HMAC material persisted on user_profiles.
--
-- This migration adds three nullable columns:
--
--   ``webhook_signing_secret``       Plaintext HMAC key. Stored in plain
--                                    because HMAC needs the actual bytes
--                                    (hashing it would make it unusable).
--                                    Column-level grants below prevent
--                                    anon / authenticated roles from
--                                    SELECTing it; only service_role
--                                    reads the column, and the API only
--                                    exposes the plaintext once at mint
--                                    time via the /account/api-keys page.
--   ``webhook_secret_prefix``        First 8 chars (``whsec_``) shown in
--                                    the dashboard so the user can match
--                                    "is this still the same secret?"
--                                    without revealing the random tail.
--   ``webhook_secret_rotated_at``    Audit timestamp.
--
-- A CHECK constraint pins the secret's shape (matches what
-- ``shared.api_keys._new_webhook_secret`` mints): ``whsec_`` prefix +
-- 22-128 url-safe base64 chars. Stripe / Resend convention.
--
-- Compatibility: rows with NULL secret fall back to ``WEBHOOK_SIGNING_SECRET``
-- env var during a transition window (legacy receivers already verify
-- with that secret). After the first tenant rotates, the env-var
-- fallback should only be needed for ARC-style internal tests.

ALTER TABLE public.user_profiles
    ADD COLUMN IF NOT EXISTS webhook_signing_secret    text,
    ADD COLUMN IF NOT EXISTS webhook_secret_prefix     text,
    ADD COLUMN IF NOT EXISTS webhook_secret_rotated_at timestamptz;

-- Column-level lockdown: SELECT on the secret column is denied to anon
-- and authenticated roles. Service-role keeps full access. Combined
-- with the existing self-read RLS policy, a logged-in user reading
-- their own row through the anon client will be REFUSED if they include
-- the secret column in their SELECT list. The dashboard reads the
-- prefix + rotated_at columns only.
REVOKE SELECT (webhook_signing_secret) ON public.user_profiles FROM PUBLIC;
REVOKE SELECT (webhook_signing_secret) ON public.user_profiles FROM anon;
REVOKE SELECT (webhook_signing_secret) ON public.user_profiles FROM authenticated;

-- Same lockdown on writes: a malicious user shouldn't be able to set
-- their own webhook secret to a known value via the anon client. Only
-- service-role mutates this column (from /account/api-keys flows).
REVOKE INSERT (webhook_signing_secret) ON public.user_profiles FROM PUBLIC;
REVOKE INSERT (webhook_signing_secret) ON public.user_profiles FROM anon;
REVOKE INSERT (webhook_signing_secret) ON public.user_profiles FROM authenticated;
REVOKE UPDATE (webhook_signing_secret) ON public.user_profiles FROM PUBLIC;
REVOKE UPDATE (webhook_signing_secret) ON public.user_profiles FROM anon;
REVOKE UPDATE (webhook_signing_secret) ON public.user_profiles FROM authenticated;

-- Format CHECK so a malformed write (e.g. operator hand-edit) is
-- rejected at the DB boundary. ``whsec_<22-128 url-safe chars>``.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'user_profiles_webhook_secret_format'
    ) THEN
        ALTER TABLE public.user_profiles
            ADD CONSTRAINT user_profiles_webhook_secret_format CHECK (
                webhook_signing_secret IS NULL
                OR webhook_signing_secret ~ '^whsec_[A-Za-z0-9_-]{22,128}$'
            );
    END IF;
END$$;

COMMENT ON COLUMN public.user_profiles.webhook_signing_secret IS
    'Per-tenant HMAC secret for Platform-API webhook signing (CR-01). '
    'Plaintext because HMAC needs the literal bytes; column-level revoke '
    'keeps it off the anon/authenticated read path. Service-role only. '
    'Returned to the user once via /account/api-keys; never echoed again.';

COMMENT ON COLUMN public.user_profiles.webhook_secret_prefix IS
    'First 8 chars of the secret (literally ``whsec_``). Display-only.';

COMMENT ON COLUMN public.user_profiles.webhook_secret_rotated_at IS
    'When the secret was last minted or rotated. Surfaced in the dashboard.';
