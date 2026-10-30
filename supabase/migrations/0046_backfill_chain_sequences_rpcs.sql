-- Ranomics tools-hub: two functions for scripts/backfill_chain_sequences.py,
-- so the backfill reads and writes a job's result rows a piece at a time
-- instead of sending the whole tool_jobs.result each way. Safe to re-run.
--
-- The rows are the same top-level array shared.jobs._with_chain_sequences
-- picks: "candidates" when it is an array, else "designs". Chains are stored
-- under each row's "chain_sequences" key as [[chain_id, sequence], ...], the
-- shape shared.exports.stored_chains reads.
--
-- Apply via the Supabase SQL editor before running the backfill (dry run
-- included). No app code calls these.

-- {"key", "total", "rows"}: rows p_offset .. p_offset + p_limit - 1 (0-based)
-- of the job's row array; NULL when the job is missing or has no such array.
CREATE OR REPLACE FUNCTION public.backfill_candidate_page(
    p_job_id uuid, p_offset integer, p_limit integer
) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_result jsonb;
    v_key    text;
    v_rows   jsonb;
BEGIN
    SELECT result INTO v_result FROM public.tool_jobs WHERE id = p_job_id;
    IF jsonb_typeof(v_result -> 'candidates') = 'array' THEN
        v_key := 'candidates';
    ELSIF jsonb_typeof(v_result -> 'designs') = 'array' THEN
        v_key := 'designs';
    ELSE
        RETURN NULL;
    END IF;
    v_rows := v_result -> v_key;
    RETURN jsonb_build_object(
        'key', v_key,
        'total', jsonb_array_length(v_rows),
        'rows', COALESCE((
            SELECT jsonb_agg(e ORDER BY i)
            FROM jsonb_array_elements(v_rows) WITH ORDINALITY AS t(e, i)
            WHERE i > p_offset AND i <= p_offset + p_limit), '[]'::jsonb));
END $$;

-- Sets "chain_sequences" on the rows p_chains names ({"<0-based index>":
-- [[chain_id, sequence], ...]}) in one UPDATE. Writes only while the job's
-- status is still p_status and its p_key array still has p_total rows; returns
-- whether it wrote.
CREATE OR REPLACE FUNCTION public.backfill_store_chain_sequences(
    p_job_id uuid, p_status text, p_key text, p_total integer, p_chains jsonb
) RETURNS boolean
LANGUAGE plpgsql AS $$
BEGIN
    IF p_key NOT IN ('candidates', 'designs') THEN
        RAISE EXCEPTION 'backfill_store_chain_sequences: bad key %', p_key;
    END IF;
    UPDATE public.tool_jobs
    SET result = jsonb_set(result, ARRAY[p_key], COALESCE((
        SELECT jsonb_agg(
            CASE WHEN jsonb_typeof(e) = 'object' AND p_chains ? (i - 1)::text
                 THEN e || jsonb_build_object('chain_sequences', p_chains -> (i - 1)::text)
                 ELSE e END
            ORDER BY i)
        FROM jsonb_array_elements(result -> p_key) WITH ORDINALITY AS t(e, i)),
        '[]'::jsonb))
    WHERE id = p_job_id
      AND status = p_status
      AND jsonb_typeof(result -> p_key) = 'array'
      AND jsonb_array_length(result -> p_key) = p_total;
    RETURN FOUND;
END $$;

REVOKE ALL ON FUNCTION public.backfill_candidate_page(uuid, integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.backfill_candidate_page(uuid, integer, integer) FROM anon;
REVOKE ALL ON FUNCTION public.backfill_candidate_page(uuid, integer, integer) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.backfill_candidate_page(uuid, integer, integer) TO service_role;

REVOKE ALL ON FUNCTION public.backfill_store_chain_sequences(uuid, text, text, integer, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.backfill_store_chain_sequences(uuid, text, text, integer, jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.backfill_store_chain_sequences(uuid, text, text, integer, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.backfill_store_chain_sequences(uuid, text, text, integer, jsonb) TO service_role;
