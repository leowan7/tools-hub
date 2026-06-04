-- Ranomics tools-hub — Atomic API status-transition RPC
-- Safe to re-run.
--
-- Purpose
--   FIX #6 from the platform-API validation review. The Python
--   implementation of ``transition_api_status`` did a READ (status_log)
--   → MODIFY (append entry) → WRITE pattern, which loses one append if
--   two transitions race on the same row. This RPC moves the whole
--   operation into a single UPDATE so Postgres' row-level locking gives
--   us the atomicity for free.
--
-- The function:
--   - Filters on submission_source='api' so it can't accidentally mutate
--     web-form rows that use the legacy short status enum.
--   - Blocks forward-only FSM: refuses to transition out of
--     'Done'/'Cancelled' (returns the row unchanged).
--   - Refuses no-op transitions (status already == p_new_status).
--   - Optionally bumps results_status in the same write (atomic with
--     status), so a 'Done' transition + 'all' results-flip is visible
--     in the same row read.
--   - Returns the FULL row pre- and post- the transition so the caller
--     can both populate the API response and fire the webhook with the
--     real prev_status (not a hardcoded guess).
--
-- Security
--   SECURITY DEFINER so it runs with table-owner privs even when called
--   from service_role. EXECUTE is granted ONLY to service_role; revoked
--   from public, anon, authenticated to keep direct cookie-auth callers
--   off it.

CREATE OR REPLACE FUNCTION public.transition_lab_campaign_api(
    p_campaign_id   uuid,
    p_new_status    text,
    p_by            text DEFAULT 'system',
    p_results_status text DEFAULT NULL
)
RETURNS TABLE (
    prev_status      text,
    new_status       text,
    moved            boolean,
    campaign         public.lab_campaigns
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $LIVE$
DECLARE
    v_prev_status     text;
    v_prev_results    text;
    v_now             timestamptz := now();
    v_new_entry       jsonb;
    v_updated         public.lab_campaigns;
BEGIN
    -- Read the current row under the implicit lock we'll take with UPDATE.
    -- We need prev_status outside the UPDATE clause to return it cleanly.
    SELECT status, results_status
    INTO v_prev_status, v_prev_results
    FROM public.lab_campaigns
    WHERE id = p_campaign_id AND submission_source = 'api'
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::text, NULL::text, false, NULL::public.lab_campaigns;
        RETURN;
    END IF;

    -- No-op (same status) or terminal-already → return without mutation.
    IF v_prev_status = p_new_status
       OR v_prev_status IN ('Done', 'Cancelled')
    THEN
        SELECT * INTO v_updated FROM public.lab_campaigns WHERE id = p_campaign_id;
        RETURN QUERY SELECT v_prev_status, v_prev_status, false, v_updated;
        RETURN;
    END IF;

    v_new_entry := jsonb_build_object(
        'status', p_new_status,
        'at',     v_now::text,
        'by',     COALESCE(p_by, 'system')
    );

    UPDATE public.lab_campaigns
    SET status            = p_new_status,
        status_log        = COALESCE(status_log, '[]'::jsonb) || v_new_entry,
        last_transition_at = v_now,
        results_status    = COALESCE(p_results_status, results_status)
    WHERE id = p_campaign_id
    RETURNING * INTO v_updated;

    RETURN QUERY SELECT v_prev_status, p_new_status, true, v_updated;
END;
$LIVE$;

REVOKE ALL ON FUNCTION public.transition_lab_campaign_api(uuid, text, text, text) FROM public;
REVOKE ALL ON FUNCTION public.transition_lab_campaign_api(uuid, text, text, text) FROM anon;
REVOKE ALL ON FUNCTION public.transition_lab_campaign_api(uuid, text, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.transition_lab_campaign_api(uuid, text, text, text) TO service_role;
