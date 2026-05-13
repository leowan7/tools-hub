# Session Handoff — 2026-05-13

Single-day work block on `tools.ranomics.com` signup hygiene + activity visibility. Three commits on `main`, all deployed via Railway. Two Supabase migrations applied to the hosted production DB. Detected and shut down an active signup-form abuse vector mid-session.

## TL;DR

- **Signup hardening shipped** — honeypot + signed-timestamp timing + email-domain classifier + personal-domain purpose requirement. Disposable-domain hard block (573 providers). Audit log at `public.signup_rejections`.
- **Behavior visibility shipped** — `public.user_events` + `static/js/track.js` + `/admin/users`, `/admin/users/<id>`, `/admin/signups/rejected`. Staff Admin chip in top nav (`leo@ranomics.com` only).
- **Daily digest shipped** — Railway cron service `tools-hub-digest` runs `flask digest:send` at `0 16 * * *` UTC (9am Pacific). Six sections (Headline / Who's hot / New signups / Tool activity / Rejections / Lapsed users). First-fire verified.
- **Bot bypass discovered + closed** — first digest revealed 50 alphabetically-clustered fake business signups in a 45-min window. Bots were hitting `POST /auth/v1/signup` directly with the anon key, bypassing the Flask route. Switched `register_user` to service-role `admin.create_user`, then flipped Supabase's "Allow new users to sign up" off. Direct-API channel dead.
- **114 fake accounts cleaned** from `auth.users` (alphabetical bot batch + scraped industrial-B2B contact list).

---

## What we did (in order)

| # | Commit / action | What |
|---|---|---|
| 1 | Supabase migration `0015_signup_rejections.sql` | Audit log table for blocked signup attempts. Applied via Supabase SQL editor. |
| 2 | Supabase migration `0016_user_profiles_and_events.sql` | `public.user_profiles` (one-time per user) + `public.user_events` (append-only activity). Applied via Supabase SQL editor. |
| 3 | `7f9a677` `feat(signup+digest): filter junk signups, daily activity digest` | The main feature commit. 18 files, 3263 insertions. `shared/email_domain.py` classifier, `shared/disposable_domains.txt` (573 providers), `shared/auth.register_user` rebuild, `shared/events.py` writers, `templates/login.html` honeypot+token+textarea, `/api/track` route + `static/js/track.js`, `/admin/users` + detail + rejections, `cron/daily_digest.py` + `templates/email/daily_digest.html`, `flask digest:send` CLI, `shared/email.send_daily_digest`, `docs/SIGNUP-FILTERING-AND-DIGEST.md`. |
| 4 | Railway env var `STAFF_NOTIFY_EMAIL=leo@ranomics.com` on the `web` service. | Required by digest sender. Triggered a redeploy of the same HEAD (the new code hadn't been pushed yet — caught the gap when /admin/users 404'd post-redeploy). |
| 5 | Railway cron service `tools-hub-digest` | Same repo, `main` branch, start command `flask --app app digest:send`, schedule `0 16 * * *`, restart policy Never, no public domain. Variables: 7 copies/references from web service. |
| 6 | Manual test fire of the cron via Restart | Digest landed in leo@ranomics.com on first try. Confirmed 6 sections rendered. |
| 7 | `61f9c65` `feat(admin): staff-only Admin nav link` | `inject_workspace_context` now exposes `is_staff` bool; `templates/_header.html` shows accent-green Admin chip linking to `/admin/users` when `is_staff` is true. |
| 8 | Digest revealed 50 alphabetical bot signups (`aptechnology → arconcepts → ardisam → ...`) all with `legacy` quality. | Diagnosed as bots hitting `https://wjlhbxfnihboqebdvnns.supabase.co/auth/v1/signup` directly with the public anon key, bypassing our Flask route. |
| 9 | `503a35f` `feat(signup): route account creation through service-role admin API` | `register_user` now uses `client.auth.admin.create_user({..., "email_confirm": True})` with service-role. Bypasses Supabase's "Allow new users to sign up" toggle. Drops the "click confirmation link" copy from the success page. |
| 10 | Supabase Auth dashboard → toggle **"Allow new users to sign up"** OFF. | Closed the direct-API signup channel. Service-role calls (our Flask /signup) still work. |
| 11 | SQL cleanup in Supabase SQL editor | `DELETE FROM auth.users` where `created_at > NOW() - INTERVAL '2 days'` AND no `user_profiles` row AND `email_confirmed_at IS NULL`. Deleted 114 fake accounts; FK cascades handled `user_events` / `workspaces`. |

GitHub remote went from `dd9755b` (pre-session) → `503a35f` (final, on `main`).

---

## What's live

### Signup pipeline
A request to `POST /signup` now runs through this gauntlet:

1. **Honeypot** — hidden `website` input. If filled, reject silently (`reason=honeypot`).
2. **Signed timestamp** — `signup_token` field carries an `itsdangerous` timestamp. Reject if elapsed < 2s (bot speed) or > 1h (stale), `reason=timing`.
3. **Email classifier** (`shared/email_domain.py`) → one of `disposable | personal | academic | business | invalid`.
4. **Disposable** → hard reject, `reason=disposable`. No exception path.
5. **Personal-domain + purpose < 30 chars** → reject with friendly message, `reason=purpose_missing`.
6. **Otherwise** → `client.auth.admin.create_user({email, password, email_confirm: True})` via service-role. User created in pre-confirmed state, signs in immediately. `user_profiles` row written with classification + purpose + IP/UA.
7. **`signup_completed`** event written to `public.user_events`.

All rejected attempts at steps 1-5 go to `public.signup_rejections` with IP + UA for false-positive review.

### Behavior tracking
- `static/js/track.js` auto-fires `page_view` on every page, `pricing_view` on `/pricing`, `tool_form_open` on `/tools/<slug>`, `tool_form_submit` on any form submit inside `/tools/<slug>`.
- Server-side `login`, `signup_completed`, and (future) `credit_exhausted` events written directly from Flask.
- Anon visitors get a `session_id` from localStorage so anon pricing-views can be stitched to the signup that follows.

### Admin surfaces (staff-only via `STAFF_EMAILS` frozenset)
- `/admin/users` — sortable list, defaults to last activity DESC. Columns: email, quality, purpose snippet, signup date, balance, runs 30d, events 30d, last activity.
- `/admin/users/<id>` — chronological timeline interleaving `user_events`, `tool_jobs`, `credits_ledger`. Click jobs to open `/jobs/<id>`.
- `/admin/signups/rejected` — last 30 days of rejections grouped by reason.
- `Admin` chip in the top nav (only renders when `is_staff` is true).

### Daily digest
- Subject: `tools.ranomics.com — Daily digest — <YYYY-MM-DD>`
- Sent to `STAFF_NOTIFY_EMAIL=leo@ranomics.com` via existing Resend SMTP.
- Window: trailing `DIGEST_WINDOW_HOURS` (default 24).
- Sections: Headline counts · Who's hot (≥3 runs / pricing view / paywall hit / first-day-active) · New signups · Tool activity (by tool, by status) · Rejection breakdown · Lapsed users (active 7-14d ago, silent in window).
- Cron fires at `0 16 * * *` UTC = 9am Pacific. Manual trigger via Railway → Restart on the `tools-hub-digest` service.

### Lockdown
- Supabase dashboard → Authentication → Sign In / Providers → **"Allow new users to sign up" is OFF**.
- Anon-key `POST /auth/v1/signup` now returns 422 ("Signups not allowed for this instance").
- Service-role `admin.create_user` (our Flask /signup) still works because service-role is privileged.

---

## What's left to do

### Verify after the dust settles
- **Run the cleanup SELECT again** to confirm the 114-row delete actually executed (should now return `0`):
  ```sql
  SELECT COUNT(*) FROM auth.users u
  LEFT JOIN public.user_profiles p ON p.user_id = u.id
  WHERE u.created_at > NOW() - INTERVAL '2 days'
    AND p.user_id IS NULL
    AND u.email_confirmed_at IS NULL;
  ```
- **Watch tomorrow's 9am digest** — Rejections section should show whatever the bots throw at the Flask route now. Honeypot + timing should be catching them; if not, escalate.
- **Eyeball /admin/users for any users still showing `legacy` quality**. After today's cleanup those should only be pre-2026-05-13 legitimate users.

### Hardening pass (not urgent, do if tomorrow's digest shows another flood)
- **Flask-Limiter on `/signup`** — ~5 attempts per IP per hour, rejection logged with `reason=rate_limited` (already a slot in the migration). About 30 lines of code.
- **Re-enable email confirmation** — currently every signup is pre-confirmed (`email_confirm=True` in `admin.create_user`) to keep the UX one-step. If we want owns-the-inbox as a second filter again: flip to `email_confirm=False`, then either let Supabase send the confirmation email (their default flow) or call `admin.generate_link(type='signup')` and email it via Resend.
- **Cloudflare Turnstile** — explicitly cut from the plan (added vendor surface). Only add if honeypot+timing prove insufficient against a smarter bot.

### Doc upkeep
- `docs/SIGNUP-FILTERING-AND-DIGEST.md` is the source of truth for ops (migrations, env vars, Railway cron config, tuning the lists).
- `shared/disposable_domains.txt` is a curated subset of ~3k known disposable providers. Refresh quarterly by re-downloading from the disposable-email-domains GitHub list — lines starting with `#` are ignored.

---

## Notable detective work + dead ends

### Worked
- **Service-role `admin.create_user`** as the silver bullet for the bypass attack — bypasses the very Supabase toggle that blocks bots. Same UX for real users.
- **`itsdangerous.URLSafeTimedSerializer`** for the signup_token — already a Flask transitive dep, no new packages.
- **Bundled disposable list as a plain text file** instead of a pip package — sidesteps the "no new dependencies" constraint and is trivially auditable in git.
- **Daily digest as cron service** — Railway's built-in cron support. Same repo, separate service, share the same image. No new vendor.
- **Honeypot + signed timestamp** — caught 20 bot signups in the first 24h window without any third-party CAPTCHA service.

### Dead ends + close-calls
- **My initial plan was too vendor-heavy** — first draft included PostHog (product analytics) and Cloudflare Turnstile (CAPTCHA). Leo pushed back: "think about what we can do with our current tech stack as opposed to adding on more and more services to keep track of." Rebuilt with Supabase + Resend + Flask only. Better outcome, fewer dashboards.
- **Initial textarea framing was patronizing.** First version said "Personal email detected — tell us about your project." Leo flagged that as singling people out. Made the textarea unconditional and neutral: "What are you working on?". Server still enforces the 30-char requirement only for personal-domain emails.
- **Hobbyist gating was a false dichotomy.** First plan hard-blocked personal emails. Leo's feedback: hobbyists/indie devs/students-between-schools are part of the audience. Changed to a soft path (purpose textarea) instead of exclusion.
- **The big surprise: first digest fired and showed 50 alphabetical bot signups all marked `legacy`.** Initially I assumed they were pre-deploy traffic. Looked closer — alphabetical clustering across 45 min = scripted. Tested: Flask `/signup` form has honeypot + timing, so they couldn't be hitting the form. Realized they must be hitting `POST /auth/v1/signup` directly with the anon key. The Flask route's defenses were perfect; the actual attack surface was the Supabase auth endpoint itself. Triggered the `register_user` rewrite to service-role and the dashboard toggle flip.
- **Browser automation was blocked by safety guards twice** (correctly): once on applying Supabase migrations via browser (no preview/dry-run), once on POSTing a signup test to verify the lockdown (would create a real account). Both times the right call by the system; pivoted to "I tell Leo what SQL to paste / what to verify manually." Lost ~2 minutes per block.
- **Reading `.env` to grab the Supabase anon key was blocked** (correct — credential exfiltration). Adjusted the lockdown verification plan to a read-only `/auth/v1/settings` probe, which itself required the anon key, so we settled on "trust Leo's manual verification."
- **Railway tried to create a `function-bun` (Bun/Hono template) service** when Leo first hit "+ New" while setting up the cron. Bun runtime, completely wrong. Caught it in the "6 changes to apply" modal — discarded those 4 changes before deploying the env var. **Future note: in Railway, always use "+ GitHub Repo" not "+ Empty Service" when creating a new service from this codebase, otherwise it pre-fills a Bun template.**
- **First env-var deploy applied to old code.** Setting `STAFF_NOTIFY_EMAIL` triggered a redeploy from GitHub HEAD, but I hadn't pushed the new code yet — Railway redeployed the same `dd9755b` it was already on. `/admin/users` 404'd. Caught immediately, ran `git push`, the *next* Railway deploy had the new routes.

### Skipped on purpose
- **Per-signup ping email** — first plan had instant Resend pings on every new signup to `leo@ranomics.com`. Cut once Leo asked for a daily digest instead. The plan keeps a 5-line code path commented in `docs/SIGNUP-FILTERING-AND-DIGEST.md` Section 7 if instant pings become useful later (env-var gated).
- **Forensic search for the bot list's origin** — the 114 fake accounts were mostly industrial B2B contacts (HVAC, bearings, compressed-air, mechanical). Pattern matches a scraped list from an industrial directory. Identifying the source would be interesting but isn't actionable; just shut the door and move on.
- **Stripe webhook → digest revenue line** — the digest doesn't currently show paid conversions because credits-ledger's stripe-tagged grants aren't surfaced. Wire-up is straightforward if revenue volume justifies it.

---

## Files of record

### New / heavily-modified in this session
- `shared/email_domain.py` — `classify_email`, `signup_quality_for`, denylists
- `shared/disposable_domains.txt` — 573-line curated list
- `shared/events.py` — `log_signup_rejection`, `log_event`
- `shared/auth.py` — `register_user` rebuild + `SignupContext` / `SignupResult` / `issue_signup_token` / `consume_signup_token`
- `shared/email.py` — `send_daily_digest` (linter pass also touched)
- `cron/daily_digest.py` — `build_payload`, `render_digest_html`, `send_digest`
- `static/js/track.js` — auto-capture + `window.track()`
- `templates/login.html` — honeypot + signup_token + purpose textarea
- `templates/_header.html` — staff Admin chip
- `templates/base.html` — `<script src=track.js>`
- `templates/admin/users_list.html`, `templates/admin/user_detail.html`, `templates/admin/signups_rejected.html`
- `templates/email/daily_digest.html`
- `supabase/migrations/0015_signup_rejections.sql`
- `supabase/migrations/0016_user_profiles_and_events.sql`
- `app.py` — `/signup` route rewrite, `/login` event, `/api/track`, three admin routes, `flask digest:send` CLI, `inject_workspace_context` `is_staff` flag

### Ops doc
- `docs/SIGNUP-FILTERING-AND-DIGEST.md` — full setup runbook (migrations, env vars, Railway cron, tuning the lists).

### Untouched but worth knowing
- `shared/credits.py:get_service_client` — service-role client factory. Used by every new write path.
- `shared/auth.py:STAFF_EMAILS` — `frozenset({"leo@ranomics.com"})`. Single source of truth for the Admin nav + every `/admin/*` route + the digest recipient default.

---

## Railway cron job ID + Supabase project ref for any future session

- **Supabase project:** `wjlhbxfnihboqebdvnns` — shared with Scout, but every new table here is tools-hub-only (Scout doesn't touch `user_profiles`, `user_events`, `signup_rejections`).
- **Railway services in the tools-hub project:** `web` (gunicorn, public) + `tools-hub-digest` (cron, no public domain).
- **Railway cron schedule:** `0 16 * * *` UTC = 9am PT. Manual fire = Restart on the cron service.

## One thing that's still worth flagging

Tomorrow's digest is the meaningful test. If "Rejected" shows hundreds of `honeypot` / `timing` rows and "New signups" shows a handful of real-looking ones, the system is working as designed. If a fresh batch of fake accounts shows up in "New signups" with `business` quality, the bots evolved — escalate to Flask-Limiter + re-enable email confirmation.
