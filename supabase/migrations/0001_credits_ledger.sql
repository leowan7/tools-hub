-- Ranomics tools-hub — credits ledger + tier + webhook idempotency
-- Wave 0 (Stream A). Safe to re-run (IF NOT EXISTS / IF NOT EXISTS guards).
--
-- Data model
--   public.user_tier        one row per user; current subscription tier
--   public.credits_ledger   append-only double-entry log (grant / spend / refund)
--   public.credits_balance  view; sums ledger to current balance per user
--   public.stripe_events    webhook idempotency — insert-or-ignore on event_id
--
-- All user-scoped tables are RLS-enabled with the standard
--   USING (auth.uid() = user_id)
-- pattern so a caller signed in through Supabase Auth can only see their own
-- rows. Service-role writes bypass RLS as usual.

-- ---------------------------------------------------------------------------
-- Tiers
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ranomics_tier') THEN
        CREATE TYPE public.ranomics_tier AS ENUM (
            'free',
            'scout_pro',
            'lab',
            'lab_plus'
        );
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS public.user_tier (
    user_id              uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    tier                 public.ranomics_tier NOT NULL DEFAULT 'free',
    stripe_customer_id   text,
    stripe_subscription_id text,
    period_ends_at       timestamptz,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_tier_stripe_customer_idx
    ON public.user_tier (stripe_customer_id);

ALTER TABLE public.user_tier ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_tier_self_read ON public.user_tier;
CREATE POLICY user_tier_self_read ON public.user_tier
    FOR SELECT USING (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Credits ledger (append-only, double-entry style)
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ranomics_ledger_kind') THEN
        CREATE TYPE public.ranomics_ledger_kind AS ENUM (
            'grant',
            'spend',
            'refund'
        );
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS public.credits_ledger (
    id           bigserial PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kind         public.ranomics_ledger_kind NOT NULL,
    -- Positive for grant/refund, negative for spend. The sign is authoritative
    -- so balance is a simple SUM; kind exists for human-readable reporting.
    delta        integer NOT NULL,
    reason       text NOT NULL,
    -- Optional references back to the action that produced this entry.
    tool         text,
    job_id       text,
    stripe_event_id text,
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT credits_ledger_sign_matches_kind CHECK (
        (kind = 'grant'  AND delta > 0) OR
        (kind = 'refund' AND delta > 0) OR
        (kind = 'spend'  AND delta < 0)
    )
);

CREATE INDEX IF NOT EXISTS credits_ledger_user_idx
    ON public.credits_ledger (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS credits_ledger_job_idx
    ON public.credits_ledger (job_id);

ALTER TABLE public.credits_ledger ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS credits_ledger_self_read ON public.credits_ledger;
CREATE POLICY credits_ledger_self_read ON public.credits_ledger
    FOR SELECT USING (auth.uid() = user_id);

-- Balance view — read side of the ledger. SUM() of deltas per user, with
-- zero-row fallback so new users show a clean 0.
CREATE OR REPLACE VIEW public.credits_balance AS
SELECT
    u.id                                     AS user_id,
    COALESCE(SUM(l.delta), 0)::integer       AS balance,
    MAX(l.created_at)                        AS last_entry_at
FROM auth.users u
LEFT JOIN public.credits_ledger l ON l.user_id = u.id
GROUP BY u.id;

-- ---------------------------------------------------------------------------
-- Stripe webhook idempotency
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.stripe_events (
    event_id     text PRIMARY KEY,
    event_type   text NOT NULL,
    payload      jsonb NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

CREATE INDEX IF NOT EXISTS stripe_events_type_idx
    ON public.stripe_events (event_type, received_at DESC);

-- stripe_events is not user-scoped; writes happen from the webhook handler
-- using the service-role key. RLS is enabled with NO policies: the service
-- role bypasses RLS (so the webhook handler still writes fine), while the
-- anon and authenticated roles are denied everything. Defense-in-depth
-- against an accidental anon-key read. If you ever need client-side
-- visibility, add an explicit policy — do not disable RLS.
ALTER TABLE public.stripe_events ENABLE ROW LEVEL SECURITY;
