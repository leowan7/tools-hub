-- Ranomics tools-hub — request idempotency ledger
-- Stream G.1 (Wave-0 hardening). Safe to re-run.
--
-- Problem
--   Mutating tool routes (/tools/example-gpu/submit,
--   /developability/score, /library-planner/plan, future @requires_credits
--   handlers) can be replayed by the client — double-clicks, network
--   retries, an impatient user mashing submit. Without deduplication we
--   charge credits twice and launch two GPU jobs for one user intent.
--
-- Design
--   Per-request row storing the response for a short TTL (default 60s).
--   Keyed by either an explicit Idempotency-Key header OR a fingerprint
--   derived from (user_id, route, body_sha256) so clients get
--   dedup-by-default without any header wiring. Scoped to the requesting
--   user so two different users can legitimately POST identical payloads.
--
--   Writes happen from the Flask server via the service-role key; RLS is
--   enabled with no policies (same pattern as stripe_events) so an anon
--   or authenticated caller sees nothing. The key itself is treated as
--   opaque — it MAY contain a body hash but that body is always the
--   caller's own, so an anon caller getting a timing signal off the
--   primary key is not a material leak.

CREATE TABLE IF NOT EXISTS public.request_idempotency (
    key               text PRIMARY KEY,
    user_id           uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    route             text NOT NULL,
    -- NULL while the first request is still running. When the handler
    -- returns we UPDATE these with the cached response. A concurrent
    -- second request that sees a row with NULL status should 409; a row
    -- with a non-NULL status should replay the body.
    response_status   integer,
    response_body     text,
    content_type      text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    expires_at        timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS request_idempotency_expires_idx
    ON public.request_idempotency (expires_at);

-- Per-user index so a future admin / UI lookup on user_id is cheap.
-- (The primary key alone is enough for the hot path.)
CREATE INDEX IF NOT EXISTS request_idempotency_user_idx
    ON public.request_idempotency (user_id, created_at DESC);

ALTER TABLE public.request_idempotency ENABLE ROW LEVEL SECURITY;
-- No policies — service role bypasses RLS, anon and authenticated denied.
