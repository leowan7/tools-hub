-- Ranomics tools-hub — Signup rejection audit log
--
-- Captures every signup attempt that the /signup route rejects before
-- it reaches Supabase Auth: bot honeypot hits, suspicious form-submit
-- timing, disposable-email domains, and personal-domain submissions
-- that arrived without the required "purpose" note.
--
-- Purpose: spot false positives. If a legitimate prospect is being
-- blocked by an overly aggressive disposable list or a stricter rule,
-- the row lands here and shows up in /admin/signups/rejected + the
-- daily digest, so the filters stay tunable instead of silently
-- swallowing good signups.
--
-- The row never carries password material — only the email, the
-- reason code, and request context (IP, user agent).

CREATE TABLE IF NOT EXISTS public.signup_rejections (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email           text NOT NULL,
    reason          text NOT NULL,
    -- Allowed reasons (validated at write site, not in DB so we can
    -- add new ones without a migration):
    --   'honeypot'         hidden field filled (bot)
    --   'timing'           form submitted too fast or stale token
    --   'disposable'       email domain is in disposable list
    --   'purpose_missing'  personal domain submit with empty/short purpose
    --   'invalid'          malformed address
    --   'rate_limited'     reserved for future use
    ip              inet,
    user_agent      text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS signup_rejections_created_at_idx
    ON public.signup_rejections (created_at DESC);
CREATE INDEX IF NOT EXISTS signup_rejections_reason_created_at_idx
    ON public.signup_rejections (reason, created_at DESC);

-- Writes go through service-role; nothing here is user-readable.
ALTER TABLE public.signup_rejections ENABLE ROW LEVEL SECURITY;

-- No public-facing policies. RLS deny-by-default is the policy.
