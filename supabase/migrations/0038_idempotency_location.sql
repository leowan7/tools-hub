-- Ranomics tools-hub — preserve Location on a replayed idempotent response.
-- Safe to re-run (idempotent).
--
-- Why
--   shared/idempotency.py caches a handler's response so a replayed POST
--   inside the TTL returns the original result instead of re-running the
--   work. It persisted status + body + content-type only, so HEADERS were
--   dropped on replay.
--
--   Every return path in compute_campaign_refold is a redirect, so the
--   replayed response came back as a bare 302 with NO Location header, which
--   a browser renders as a blank/error page. Double-clicking "Re-fold"
--   reproduced it. The first request still did the real work and the replay
--   still correctly avoided duplicating it — the defect was purely that the
--   user was shown a broken response instead of the compare page.
--
--   Only Location is stored rather than a generic header blob: it is the one
--   header whose loss changes what the browser DOES, and keeping the column
--   typed keeps this table cheap (it is on the hot path of every guarded
--   mutating route).
--
-- Nullable with no default: non-redirect responses simply leave it NULL, and
-- every row written before this migration replays exactly as it does today.

ALTER TABLE public.request_idempotency
    ADD COLUMN IF NOT EXISTS location text;
