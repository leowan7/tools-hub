-- 0049_email_events.sql
--
-- Resend delivery events, written by webhooks/resend.py (POST /webhooks/resend).
-- One row per Svix message: svix_id is the primary key, and the handler
-- upserts with ignore_duplicates, so a redelivered event adds no row.
-- email_kind and user_id come from the kind / user_id tags that
-- shared/email.py::_resend_tags puts on each send (user_id only where the
-- send knows the user); emails sent before this change carry no tags and
-- land with both NULL.

CREATE TABLE IF NOT EXISTS public.email_events (
    svix_id          text PRIMARY KEY,
    resend_email_id  text NOT NULL,
    event_type       text NOT NULL,
    recipient        text,
    email_kind       text,
    user_id          uuid,
    event_at         timestamptz,
    received_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS email_events_kind_time_idx
    ON public.email_events (email_kind, event_at DESC);

CREATE INDEX IF NOT EXISTS email_events_email_idx
    ON public.email_events (resend_email_id);

-- Same pattern as stripe_events (0001): RLS on with no policies, so only the
-- service role (which bypasses RLS) reads or writes.
ALTER TABLE public.email_events ENABLE ROW LEVEL SECURITY;
