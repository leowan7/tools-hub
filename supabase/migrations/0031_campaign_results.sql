-- Ranomics tools-hub — operator results envelope on lab_campaigns
-- Safe to re-run.
--
-- Purpose
--   Phase 2 of the Platform API operator-fulfillment build (gap G2).
--   GET /api/v1/experiments/{id}/results had no operator path to attach
--   data, and read from library_design['results'] (which nothing wrote).
--   This adds a dedicated jsonb column the admin UI writes and the API
--   reads, kept separate from library_design so internal download paths
--   never leak through the experiment_spec.library_design passthrough.
--
--   results jsonb holds the Adaptyv YDS envelope plus an internal
--   download-path map:
--     {
--       "rounds":    [ {round_id, sort_gate, input_diversity, output_diversity}, ... ],
--       "sequences": [ {user_key, sequence, pre_count, post_count,
--                       log2_enrichment, percentile, called_hit}, ... ],
--       "download_paths": { "enrichment_table_csv": "<id>/results/enrichment.csv", ... },
--       "downloads":      { "<name>": "https://external-url", ... }   -- optional, operator-supplied
--     }
--   GET /results resolves download_paths to fresh signed URLs at read
--   time (so the links never expire in storage) and merges any external
--   downloads. results_status (none|partial|all) already exists (0023)
--   and gates whether the endpoint serves this envelope.
--
-- Reversibility
--   Single nullable column; rollback is DROP COLUMN.

ALTER TABLE public.lab_campaigns
    ADD COLUMN IF NOT EXISTS results jsonb;
