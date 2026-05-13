# Signup filtering + activity digest — setup notes

This document covers the ops side of the signup-hardening and
behaviour-tracking work added in May 2026. The application code is
self-contained; this file lists what needs to happen on Railway and
Supabase for the new functionality to come alive in production.

## 1. Apply Supabase migrations

Two new migrations were added:

- `supabase/migrations/0015_signup_rejections.sql`
- `supabase/migrations/0016_user_profiles_and_events.sql`

Apply them to the hosted Supabase project `wjlhbxfnihboqebdvnns` using
the Supabase CLI:

```bash
supabase db push --project-ref wjlhbxfnihboqebdvnns
```

Or paste each migration's SQL into the SQL editor in the Supabase
dashboard and run.

Verify after:

```sql
\d public.signup_rejections
\d public.user_profiles
\d public.user_events
```

All three tables should exist with the expected columns and indexes.
RLS should be enabled on all three.

## 2. Set Railway environment variables

In the Railway dashboard, on the tools-hub service, add:

| Name                 | Value                          | Required | Notes |
|----------------------|--------------------------------|----------|-------|
| `STAFF_NOTIFY_EMAIL` | `leo@ranomics.com`             | Yes      | Daily digest delivery target. |
| `DIGEST_WINDOW_HOURS`| `24`                            | Optional | Override the trailing window. |

These already exist and don't need changing:

- `RESEND_API_KEY` — must be set; the digest reuses it.
- `EMAIL_FROM` — defaults to `Ranomics Tools <noreply@tools.ranomics.com>`.
- `PUBLIC_BASE_URL` — defaults to `https://tools.ranomics.com`.
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` — required for admin
  views + digest queries; must already be set for credits to work.
- `SESSION_SECRET_KEY` — required for the signed signup-form
  timestamp. Already set.

## 3. Configure the Railway cron job

Railway supports cron jobs natively via a separate service that
shares the same image as the web service.

In the Railway project dashboard:

1. Click **+ New** → **Empty Service** (or **+ New** → **Cron Job** if
   the option exists in your account).
2. Name it `tools-hub-digest`.
3. Under **Settings → Source**, point it at the same repo as the web
   service.
4. Under **Settings → Deploy**, set:
   - Start command: `flask digest:send`
   - Cron schedule: `0 16 * * *`   (16:00 UTC = 09:00 PST)
5. Copy the env vars from the web service: at minimum
   `RESEND_API_KEY`, `EMAIL_FROM`, `PUBLIC_BASE_URL`, `SUPABASE_URL`,
   `SUPABASE_SERVICE_ROLE_KEY`, `SESSION_SECRET_KEY`,
   `STAFF_NOTIFY_EMAIL`. Use Railway's "Shared Variables" if you have
   it enabled.

If your Railway plan doesn't include cron, alternatives:

- **GitHub Actions cron** — `.github/workflows/digest.yml` running
  `gh railway run "flask digest:send"` on a schedule. Free, fits in
  the existing GH Actions allowance.
- **Vercel cron** triggering an authenticated `/admin/digest/send`
  webhook on tools-hub. Would require adding the webhook + auth.

## 4. Local test before going live

In a local shell with the same env vars set:

```bash
cd C:\Users\lab\Documents\Claude_projects\tools-hub
venv\Scripts\python -m flask --app app digest:send
```

You should see `digest:send sent` (or `failed (see logs)` plus a
traceback in Railway logs). Check `leo@ranomics.com`'s inbox; the
email should render in Gmail with the six sections populated.

For an extra-wide first run, force a 7-day window:

```powershell
$env:DIGEST_WINDOW_HOURS = "168"; venv\Scripts\python -m flask --app app digest:send
```

This lets you see the full template populated even on a fresh deploy
with little 24-hour activity.

## 5. New surfaces to know about

| Surface                              | Purpose |
|--------------------------------------|---------|
| `GET /admin/users`                    | Sortable per-user list with signup quality, purpose snippet, runs, last activity. Click any row for detail. |
| `GET /admin/users/<id>`               | Interleaved timeline merging `user_events`, `tool_jobs`, `credits_ledger` for one user. |
| `GET /admin/signups/rejected`         | Last 30 days of `signup_rejections`, grouped by reason. Scan weekly for false positives. |
| `POST /api/track`                     | Append-only event capture endpoint (used by `static/js/track.js`). |
| `flask digest:send`                   | Build + email the daily digest. |

## 6. Tuning the filters

If the digest's **Rejections** section shows real prospects being
blocked, change one of the curated lists:

- **Personal-domain list**:  `shared/email_domain.py` →
  `PERSONAL_DOMAINS`. Add a domain here to make those signups require
  the "what are you working on" note instead of going through
  silently. Remove a domain to make those signups behave like
  business.
- **Disposable-domain list**:  `shared/disposable_domains.txt`. Add a
  domain to block it. Lines starting with `#` are comments.
- **Academic suffixes**:  `shared/email_domain.py` →
  `ACADEMIC_SUFFIXES`. Add a TLD to recognise more institutions.
- **Honeypot / timing thresholds**:  `shared/auth.py` →
  `MIN_FILL_SECONDS`, `MAX_FILL_SECONDS`, `MIN_PURPOSE_CHARS`.

After any change, redeploy. No new migrations needed.

## 7. Re-enabling per-signup pings (if desired)

The plan intentionally cut per-signup ping emails to keep the digest
as the single signal stream. If you later want instant pings for
`business`-class signups only:

In `app.py`, inside the `/signup` route's success branch (right after
`log_event(event_type="signup_completed", ...)`), add:

```python
if (
    result.signup_quality == "business"
    and os.environ.get("PING_ON_BUSINESS_SIGNUP") == "true"
):
    try:
        from shared.email import _send_simple  # noqa: PLC0415
        _send_simple(
            api_key=os.environ.get("RESEND_API_KEY", ""),
            from_addr=os.environ.get(
                "EMAIL_FROM",
                "Ranomics Tools <noreply@tools.ranomics.com>",
            ),
            to_email=os.environ.get(
                "STAFF_NOTIFY_EMAIL", "leo@ranomics.com"
            ),
            subject=f"New business signup: {email}",
            html_body=f"<p>{email}</p><p><a href='{public_base}/admin/users/{result.user_id}'>Open in admin</a></p>",
            text_body=f"{email}\n{public_base}/admin/users/{result.user_id}",
            log_tag="business_signup_ping",
        )
    except Exception:
        logger.warning("business signup ping failed", exc_info=True)
```

Then set `PING_ON_BUSINESS_SIGNUP=true` on Railway. Removable in two
seconds if it gets noisy.
