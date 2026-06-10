-- Ranomics tools-hub — customer-facing note on lab_campaigns
-- Safe to re-run.
--
-- Purpose
--   Phase 3 of the Platform API operator-fulfillment build. Admin status
--   changes on API rows are silent by product decision (the customer's
--   agent observes them on its next poll). This adds an opt-in path: the
--   operator can attach a customer-safe note and tick "notify customer" so
--   a chosen transition (QuoteSent / Done / Cancelled) fires a signed
--   webhook carrying the note.
--
--   notes_customer is distinct from notes_internal: notes_internal is
--   operator-only (feasibility, capacity, scope) and must NEVER reach the
--   customer; notes_customer is written to the API status view and the
--   notification webhook payload. Keeping them in separate columns is the
--   guardrail against leaking internal notes.
--
-- Reversibility
--   Single nullable column; rollback is DROP COLUMN.

ALTER TABLE public.lab_campaigns
    ADD COLUMN IF NOT EXISTS notes_customer text;
