-- Ranomics tools-hub — User profiles + behavioral event log
--
-- Two tables:
--
--   public.user_profiles   One row per signed-up user. Captures domain
--                          classification (business/personal/academic),
--                          the human-readable "what are you working on"
--                          note submitters give at signup, and the
--                          internal signup_quality tag. Joins auth.users
--                          by user_id. The daily digest leans on this
--                          row to surface qualifying info per signup.
--
--   public.user_events     Append-only event log. One row per page view,
--                          tool form open, tool form submit, pricing
--                          view, login, signup completion, credit
--                          exhaustion, etc. Anonymous users land here
--                          too (user_id NULL, session_id only) so we
--                          can link an anon pricing view to the signup
--                          that follows.
--
-- These tables are written from the Flask app via the service-role
-- client. RLS is enabled with deny-by-default — only staff routes read
-- the data, and they go through service-role too.

-- ---------------------------------------------------------------------------
-- user_profiles
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.user_profiles (
    user_id         uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    domain_class    text NOT NULL,
        -- 'business' | 'personal' | 'academic'
    signup_quality  text NOT NULL,
        -- 'business' | 'academic' | 'personal_explained'
    purpose         text,
        -- Free-text "what are you working on" note. Required when
        -- domain_class='personal'; optional otherwise.
    ip              inet,
    user_agent      text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_profiles_created_at_idx
    ON public.user_profiles (created_at DESC);
CREATE INDEX IF NOT EXISTS user_profiles_signup_quality_idx
    ON public.user_profiles (signup_quality);

ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;

-- Self-read so a logged-in user can see their own row through the anon
-- client if a future "Settings" page wants to display it. Writes are
-- service-role only.
DROP POLICY IF EXISTS "user_profiles_self_select" ON public.user_profiles;
CREATE POLICY "user_profiles_self_select" ON public.user_profiles
    FOR SELECT
    USING (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- user_events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.user_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid REFERENCES auth.users(id) ON DELETE CASCADE,
        -- NULL for anonymous events (logged-out visitors).
    session_id      text,
        -- Opaque client-side identifier (cookie/localstorage). Lets us
        -- stitch an anon pricing view to the signup that follows even
        -- when user_id is NULL on the anon row.
    event_type      text NOT NULL,
        -- Common types (validated at write site, not in DB):
        --   page_view, tool_form_open, tool_form_submit,
        --   pricing_view, login, signup_completed, credit_exhausted
    path            text,
    props           jsonb NOT NULL DEFAULT '{}'::jsonb,
    ip              inet,
    user_agent      text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_events_user_created_idx
    ON public.user_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS user_events_type_created_idx
    ON public.user_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS user_events_created_idx
    ON public.user_events (created_at DESC);
CREATE INDEX IF NOT EXISTS user_events_session_idx
    ON public.user_events (session_id, created_at DESC)
    WHERE session_id IS NOT NULL;

ALTER TABLE public.user_events ENABLE ROW LEVEL SECURITY;

-- No public-facing policies. Service role reads/writes only.
