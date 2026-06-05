-- 0027_webhook_deliveries_claim_rpc.sql
--
-- CR-02 (fresh-review): a cron-driven sweep needs to claim ready-to-fire
-- webhook_deliveries rows atomically so concurrent ticks (or future Railway
-- replicas) never fire the same row twice. supabase-py / PostgREST has no
-- SKIP LOCKED primitive, so we ship a SECURITY DEFINER RPC that:
--
--   1. Picks up to ``p_limit`` rows whose delivery is due
--      (``delivered_at IS NULL AND next_retry_at <= now()``).
--   2. Holds a row-level FOR UPDATE SKIP LOCKED lock so the row cannot
--      be picked by a peer in the same window.
--   3. Bumps ``next_retry_at`` by a short visibility timeout so even if
--      the worker dies before the dispatch completes, the row won't be
--      re-picked for ``p_lease_seconds``.
--   4. Returns the rows.
--
-- The dispatcher updates ``delivered_at`` on success or writes a new
-- ``next_retry_at`` on failure. The lease bump is therefore a safety
-- net for crashes between claim and update.
--
-- Apply with `psql` via the Supabase SQL editor or `supabase db push`.

CREATE OR REPLACE FUNCTION public.claim_due_webhook_deliveries(
    p_limit integer DEFAULT 50,
    p_lease_seconds integer DEFAULT 90
) RETURNS SETOF public.webhook_deliveries
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $LIVE$
DECLARE
    v_now timestamptz := now();
    v_lease_until timestamptz;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 THEN
        p_limit := 50;
    END IF;
    IF p_limit > 500 THEN
        -- Guardrail: a runaway caller can't drain the table in one call.
        p_limit := 500;
    END IF;
    IF p_lease_seconds IS NULL OR p_lease_seconds < 30 THEN
        p_lease_seconds := 90;
    END IF;
    v_lease_until := v_now + make_interval(secs => p_lease_seconds);

    RETURN QUERY
    WITH ready AS (
        SELECT id
        FROM public.webhook_deliveries
        WHERE delivered_at IS NULL
          AND next_retry_at IS NOT NULL
          AND next_retry_at <= v_now
        ORDER BY next_retry_at
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE public.webhook_deliveries d
       SET next_retry_at = v_lease_until
      FROM ready
     WHERE d.id = ready.id
    RETURNING d.*;
END;
$LIVE$;

REVOKE EXECUTE ON FUNCTION public.claim_due_webhook_deliveries(integer, integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.claim_due_webhook_deliveries(integer, integer) FROM anon;
REVOKE EXECUTE ON FUNCTION public.claim_due_webhook_deliveries(integer, integer) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.claim_due_webhook_deliveries(integer, integer) TO service_role;

COMMENT ON FUNCTION public.claim_due_webhook_deliveries(integer, integer) IS
    'CR-02 webhook sweep: atomically lease up to p_limit ready-to-fire '
    'webhook_deliveries rows. Bumps next_retry_at by p_lease_seconds so '
    'a worker crash leaves the row recoverable. service_role only.';
