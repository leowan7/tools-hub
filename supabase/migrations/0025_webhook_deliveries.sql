-- Ranomics tools-hub — Platform API webhook delivery ledger
-- Safe to re-run.
--
-- Purpose
--   Every status transition on an API-submitted lab_campaigns row fires
--   a POST to the campaign's ``webhook_url`` if set. This table records
--   each delivery attempt for observability + retry scheduling. A
--   misbehaving subscriber URL must never block a state change in the
--   campaign itself, so this ledger is the only place delivery state
--   lives — the campaign row's status is updated unconditionally.
--
-- Lifecycle
--   1. shared.campaigns.transition_status inserts a row with
--      delivered_at IS NULL + attempts = 0 + next_retry_at = now().
--   2. The background dispatch task picks rows where delivered_at IS NULL
--      AND next_retry_at <= now() ORDER BY next_retry_at, makes the HTTP
--      POST, and on success sets delivered_at = now().
--   3. On failure, attempts += 1, last_error captured, next_retry_at
--      pushed out exponentially. After 5 attempts (≈24h) the row is
--      marked delivered_at = now() with last_error retained, so the
--      backlog never grows unbounded.
--
-- Signature
--   Each POST carries
--     X-Ranomics-Signature: t=<unix_ts>,v1=<hex(hmac_sha256(secret, t.body))>
--   The shared signing secret lives in the WEBHOOK_SIGNING_SECRET env
--   var on the Flask process. Customers can rotate by re-issuing keys.
--
-- RLS
--   Internal table. Service role only. No policies.

CREATE TABLE IF NOT EXISTS public.webhook_deliveries (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     uuid NOT NULL REFERENCES public.lab_campaigns(id) ON DELETE CASCADE,
    target_url      text NOT NULL,
    event_type      text NOT NULL,
    payload         jsonb NOT NULL,
    attempts        integer NOT NULL DEFAULT 0,
    last_error      text,
    next_retry_at   timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Dispatch picks queued rows by (delivered_at IS NULL, next_retry_at).
CREATE INDEX IF NOT EXISTS webhook_deliveries_queue_idx
    ON public.webhook_deliveries (next_retry_at)
    WHERE delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS webhook_deliveries_campaign_idx
    ON public.webhook_deliveries (campaign_id, created_at DESC);

ALTER TABLE public.webhook_deliveries ENABLE ROW LEVEL SECURITY;
-- No policies — service role bypasses; anon/authenticated denied.
