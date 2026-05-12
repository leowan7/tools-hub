-- Ranomics tools-hub — Target Workspace SKU model
-- Replaces the monthly subscription + credit-bucket pricing introduced in
-- 0001_credits_ledger.sql. Customers now activate a Workspace on a single
-- target PDB; all design tool runs against that target deduct from the
-- Workspace's Modal compute cap. The credits_ledger remains as internal
-- accounting (one Workspace = a credit grant equal to its USD cap, charged
-- against actual Modal spend at job completion) — but the customer-facing
-- unit-of-sale is the Workspace, not credits.
--
-- Data model
--   public.workspaces            one row per activated target purchase
--   public.workspaces_active     view of currently-active workspaces per user
--
-- RLS: standard auth.uid() = user_id self-read. Writes use service role.

-- ---------------------------------------------------------------------------
-- Workspace SKU enum
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'workspace_sku') THEN
        CREATE TYPE public.workspace_sku AS ENUM (
            'workspace_standard',
            'workspace_xl'
        );
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'workspace_status') THEN
        CREATE TYPE public.workspace_status AS ENUM (
            'active',
            'expired',
            'refunded'
        );
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- Workspaces table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.workspaces (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    -- Target reference: a free-text identifier (PDB id, uploaded filename,
    -- or scout-handoff slug). We do not FK to a strict targets table; the
    -- platform accepts arbitrary user uploads and built-in demo targets.
    target_pdb_id            text NOT NULL,
    target_label             text,
    sku                      public.workspace_sku NOT NULL,
    -- Modal compute cap in USD (the customer's spend ceiling for this
    -- workspace). Standard = 100.00; XL = 500.00. Kept as numeric so we
    -- can sum partial dollar amounts (Modal bills in seconds).
    modal_cap_usd            numeric(10, 2) NOT NULL,
    modal_spent_usd          numeric(10, 2) NOT NULL DEFAULT 0,
    activated_at             timestamptz NOT NULL DEFAULT now(),
    expires_at               timestamptz NOT NULL,
    -- Refund window: 7 days from activation if this is the user's first
    -- ever workspace. NULL means not refund-eligible (subsequent purchases).
    refund_eligible_until    timestamptz,
    refunded_at              timestamptz,
    status                   public.workspace_status NOT NULL DEFAULT 'active',
    stripe_payment_intent_id text UNIQUE,
    stripe_refund_id         text,
    stripe_event_id          text,  -- which checkout.session.completed event spawned us
    created_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT workspaces_cap_non_negative CHECK (modal_cap_usd >= 0),
    CONSTRAINT workspaces_spent_non_negative CHECK (modal_spent_usd >= 0),
    CONSTRAINT workspaces_expires_after_activate CHECK (expires_at > activated_at)
);

CREATE INDEX IF NOT EXISTS workspaces_user_status_idx
    ON public.workspaces (user_id, status);

CREATE INDEX IF NOT EXISTS workspaces_target_status_idx
    ON public.workspaces (target_pdb_id, status);

CREATE INDEX IF NOT EXISTS workspaces_expires_active_idx
    ON public.workspaces (expires_at)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS workspaces_stripe_pi_idx
    ON public.workspaces (stripe_payment_intent_id);

-- ---------------------------------------------------------------------------
-- Row-Level Security
-- ---------------------------------------------------------------------------

ALTER TABLE public.workspaces ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS workspaces_self_read ON public.workspaces;
CREATE POLICY workspaces_self_read ON public.workspaces
    FOR SELECT USING (auth.uid() = user_id);

-- No INSERT/UPDATE policies — writes come from the service-role client in
-- the Stripe webhook handler and shared.workspaces module. Anon + auth
-- roles cannot mutate workspaces from the client.

-- ---------------------------------------------------------------------------
-- Helper view: a user's currently-active workspaces
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.workspaces_active
WITH (security_invoker = on) AS
SELECT
    w.id,
    w.user_id,
    w.target_pdb_id,
    w.target_label,
    w.sku,
    w.modal_cap_usd,
    w.modal_spent_usd,
    (w.modal_cap_usd - w.modal_spent_usd)        AS modal_remaining_usd,
    CASE WHEN w.modal_cap_usd > 0
         THEN ROUND((w.modal_spent_usd / w.modal_cap_usd) * 100, 1)
         ELSE 0 END                              AS pct_used,
    w.activated_at,
    w.expires_at,
    GREATEST(EXTRACT(EPOCH FROM (w.expires_at - now())) / 86400, 0)::numeric(10, 2)
                                                  AS days_remaining,
    w.refund_eligible_until,
    (w.refund_eligible_until IS NOT NULL
     AND w.refund_eligible_until > now()
     AND w.refunded_at IS NULL)                  AS refund_eligible_now
FROM public.workspaces w
WHERE w.status = 'active'
  AND w.expires_at > now();

-- ---------------------------------------------------------------------------
-- Helper view: workspace history (all statuses) for the user's account page
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.workspaces_history
WITH (security_invoker = on) AS
SELECT
    w.id,
    w.user_id,
    w.target_pdb_id,
    w.target_label,
    w.sku,
    w.status,
    w.modal_cap_usd,
    w.modal_spent_usd,
    w.activated_at,
    w.expires_at,
    w.refunded_at
FROM public.workspaces w
ORDER BY w.activated_at DESC;
