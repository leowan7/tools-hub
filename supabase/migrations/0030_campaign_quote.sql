-- Ranomics tools-hub — operator quote fields on lab_campaigns
-- Safe to re-run.
--
-- Purpose
--   Phase 1 of the Platform API operator-fulfillment build. Today
--   GET /api/v1/experiments/{id}/quote returns a stub (total_usd null,
--   line_items []): there is no column for an operator to record a price,
--   so 'QuoteSent' is only a marker and a customer's agent cannot
--   cost-gate. These columns give the admin UI a place to persist a real
--   quote that the API then hands back unchanged.
--
--   Five additive columns on public.lab_campaigns:
--     - quote_total_usd   numeric     — authoritative quote total.
--     - quote_currency    text        — currency code; 'USD' for the alpha.
--     - quote_line_items  jsonb       — breakdown array of
--                                       {name, amount_usd, notes} objects,
--                                       matching the OpenAPI Quote schema.
--     - quote_valid_until timestamptz — quote expiry (informational in the
--                                       alpha; no auto-reject yet).
--     - quote_notes       text        — operator notes surfaced to the
--                                       customer alongside the quote.
--
--   These are read only for API-source rows; web-funnel campaigns ignore
--   them. Nothing writes them until an operator saves the quote form, so
--   deploying the reading code ahead of this migration is safe (the model
--   reads each column with row.get(...) -> NULL).
--
-- Reversibility
--   All adds are nullable or default-valued; rollback is DROP COLUMN.

ALTER TABLE public.lab_campaigns
    ADD COLUMN IF NOT EXISTS quote_total_usd   numeric,
    ADD COLUMN IF NOT EXISTS quote_currency    text NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS quote_line_items  jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS quote_valid_until timestamptz,
    ADD COLUMN IF NOT EXISTS quote_notes       text;

-- A quote total is never negative. Guarded so the migration re-runs cleanly.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'lab_campaigns_quote_total_nonneg'
    ) THEN
        ALTER TABLE public.lab_campaigns
            ADD CONSTRAINT lab_campaigns_quote_total_nonneg CHECK (
                quote_total_usd IS NULL OR quote_total_usd >= 0
            );
    END IF;
END$$;
