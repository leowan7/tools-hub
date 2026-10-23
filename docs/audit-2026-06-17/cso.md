# tools-hub — Security Posture Report (`/cso --comprehensive`)

**Date:** 2026-06-17 · **Repo:** `tools-hub` @ `main` · **Mode:** comprehensive (2/10 bar) · **Method:** gstack `/cso`, 4 parallel domain scans + independent verification of HIGH/MED findings. Read-only — no source changed.

> Part of the comprehensive audit. Companions: `health.md`, `REVIEW.md`, `qa.md`, `benchmark.md`. Master index: `../AUDIT-2026-06-17-comprehensive.md`.

## Attack surface
- **Code:** auth (`/login` `/signup` `/forgot-password` `/reset-password`), wallet/billing (`/account/wallet/*`, `/billing/*`), tool forms + submit (`/tools/<tool>[/submit|/preflight]`), jobs (`/jobs/<id>[/status.json|/export.*|/cancel|/refold|/share]`), Platform API (`/api/v1/*`), Scout blueprint (`/scout/*`), admin (`/admin/*`), webhook ingress (`/webhooks/modal/<id>/<token>`, `/webhooks/heartbeat`, Stripe).
- **Infra:** 4 CI workflows (all `permissions: contents: read`, first-party actions, no `pull_request_target`), 5 Modal `Dockerfile.modal`, web on nixpacks+gunicorn. 1 tracked env file (`.env.example`, placeholders).
- **Secret management:** all via `os.environ` (Supabase/Stripe/Anthropic/Modal); CI via GitHub repo secrets. **No literal secrets in source or full git history.**

## Findings

| # | Sev | Conf | Status | Category | Finding | File:line |
|---|-----|------|--------|----------|---------|-----------|
| H1 | HIGH | 7/10 | VERIFIED | Wallet / A01 | Unauthenticated heartbeat bills on client-supplied GPU seconds → cross-user job-cancel + forced charge | `webhooks/modal.py:96,355` |
| M1 | MED | 8/10 | VERIFIED | A01 IDOR / traversal | Scout file routes read `tmp/<job_id>` with no UUID check, no `safe_join`, no owner check | `scout/routes.py:530-556,871`; `scout/handoff.py:145` |
| M2 | MED | 8/10 | VERIFIED | A05 | Session-cookie `Secure`/`SameSite`+CSRF only set when `ENABLE_PLATFORM_API=1` | `app.py:1134,1165-1167,1276-1301` |
| M3 | MED | 7/10 | VERIFIED | Secrets hygiene | `.gitignore` excludes only `.env`/`.env.local`; `.env.production`/`.env.staging` trackable | `.gitignore` |
| L1 | LOW | 8/10 | VERIFIED | A05 | Open redirect via protocol-relative `next` (`//evil.com` passes `startswith("/")`) | `app.py:1505-1521` |
| L2 | LOW | 7/10 | VERIFIED | A09 | Staff/admin state changes not audit-logged | `app.py:5831,5994,6177` |
| L3 | LOW | 7/10 | VERIFIED | Correctness | `/billing/portal` calls `create_portal_session` without required `customer_id` → 500 (fails closed) | `app.py:2366` |
| L4 | LOW | 6/10 | VERIFIED | Headers | `Content-Disposition` reflects raw filename (Werkzeug rejects CRLF → cosmetic) | `app.py:5437-5439` |
| L5 | LOW | 6/10 | VERIFIED | Privacy | PII (email/IP/UA) in `user_profiles`/`user_events`/`signup_rejections` with no retention policy | mig `0016` |
| T1 | TENT | 5/10 | UNVERIFIED | Supply chain | Floor-only version pins, no tracked lockfile (`pip-audit` unavailable → CVE scan SKIPPED) | `requirements.txt` |
| T2 | TENT | 5/10 | VERIFIED | Financial | Scout quota fails open on Supabase outage → bounded (auth-gated, 1024-tok Haiku) LLM cost | `scout/quota.py:199-213` |
| T3 | TENT | 7/10 | VERIFIED | LLM (dead code) | Raw protein name into LLM user-turn prompt — currently no caller | `scout/literature.py:36-46` |
| I1 | INFO | — | — | CI/CD | No CODEOWNERS on workflows; prod deploy == push access to `main` (branch protection is the control) | `.github/workflows/` |

### H1 — Unauthenticated heartbeat → cross-user job cancel + forced charge (HIGH)
`/webhooks/heartbeat` (`webhooks/modal.py:96-98`) has no token in its path; the docstring (`:37-41`) deems it "no security consequence." That holds for the stage string and the candidate branch (token-gated at `:319-335`), **but not the cost path**: `:355` reads `cumulative_gpu_seconds` from the request body unconditionally, and `:358-359` passes it to `_run_overrun_check` → `shared/jobs.py:mid_run_monitor_check`, which on an over-cap value cancels the Modal call and settles a `safety_kill` (BILLED) charge using the supplied seconds (clamped to the per-tool hard cap, $150–$500).
- **Exploit:** attacker learns a victim's *running* job UUID (leaks via the share URL, referrer, screenshots, support threads) → `POST /webhooks/heartbeat {"job_id":"<uuid>","cumulative_gpu_seconds":999999}` with no creds → victim's job is cancelled and their wallet is charged up to the per-tool cap for compute it never used.
- **Mitigating factor (why 7/10):** job UUID is unguessable (122-bit); needs a live job's id. Not mass-exploitable, but plausible.
- **Fix:** gate the heartbeat behind the same `job_token` the result webhook uses (add it to the URL/body + `hmac.compare_digest`); and never bill on externally-supplied seconds — bound `cumulative_secs` to `_elapsed_running_seconds(job)` (the fallback already exists at `:357`).

### M1 — Scout file routes: cross-user IDOR + path traversal (MEDIUM)
`/scout/pdb/<job_id>`, `/scout/download/<job_id>`, `/scout/feasibility/download/<job_id>` and `scout/handoff.py:145` build `Path("tmp") / job_id` (or `scout_job_id`) from user input with **no UUID validation, no `safe_join` confinement, and no per-user ownership check** (`scout/routes.py:530-556`). The main app scopes every read via `get_job(job_id, user_id=ctx.user_id)`; the Scout blueprint does not.
- **Exploit:** any logged-in user passing another user's `job_id` reads their uploaded structure + epitope/feasibility CSVs; `../` reaches fixed-name files outside `tmp/`.
- **Fix:** strict UUID regex on `job_id` at each route, `send_from_directory`/`werkzeug.utils.safe_join` under `tmp/`, and record + verify owning `user_id` at `create_job_dir` time.

### M2 — Cookie/CSRF hardening coupled to a feature flag (MEDIUM)
`SESSION_COOKIE_SECURE/SAMESITE` and the CSRF token are set **only inside** `if ENABLE_PLATFORM_API == "1"` (`app.py:1134,1165-1167,1276-1301`). The session cookie authenticates the whole web UI (wallet/account/admin). If the flag is off in prod, those POST surfaces run with Flask-default cookies (no `Secure`, SameSite≈Lax) and no CSRF token.
- **Action:** confirm whether `ENABLE_PLATFORM_API=1` in the Railway prod env. If not, this is live. **Fix:** hoist the three `SESSION_COOKIE_*` assignments + CSRF helper to unconditional `create_app()` config.

### M3 — `.gitignore` leaves `.env.production`/`.env.staging` trackable (MEDIUM) — affects the staging plan
Only `.env` and `.env.local` are ignored (`git check-ignore .env.production` → not ignored). A future `.env.production`/`.env.staging` with live Stripe/Supabase keys could be `git add`-ed and pushed.
- **Fix:** `.env*` then `!.env.example` in `.gitignore`. **Do this before creating the planned `.env.staging`.**

### Lower-severity (L1–L5, T1–T3, I1)
See the table. L1 open-redirect: reject `//`/`/\` prefixes or use `url_has_allowed_host_and_scheme`. L3: `/billing/portal` is a live 500 (resolve `customer_id` first). T1: track a hash-pinned lockfile + add `pip-audit`/dependabot. T3: delimit the protein name as untrusted data before wiring `fetch_literature_context` in.

## STRIDE (5 components, DoS row omitted)
- **Auth/session:** Supabase-verified creds, itsdangerous-signed cookie, generic errors + enumeration-safe reset, `/admin` 404-on-unauthorized. Gap: M2 cookie flags.
- **Wallet/billing:** Stripe signature-verified + event-id idempotent (sole credit authority), `try_hold_for_job`/`settle_hold` row-locked + `parent_tx_id` replay-guarded, charge clamped to per-tool cap. Strong.
- **Job dispatch/Modal:** CAS-guarded terminal transitions, `job_token` per job, owner-scoped reads. Gap: H1 heartbeat billing basis.
- **Webhook ingress:** result + upload-URL endpoints token-verified; outbound delivery has a strong SSRF guard (https-only, private/CGNAT/metadata blocked, per-attempt DNS-rebind re-validation, `allow_redirects=False`, HMAC-signed). Gap: H1 heartbeat unauthenticated.
- **Upload/storage:** `secure_filename`+`posixpath.basename` on keys, 20MB cap, no XXE (PDB/CIF not XML), owner-prefixed short-TTL signed URLs.

## Data classification (gaps flagged)
RESTRICTED — passwords (Supabase Auth, never in app DB), payment (Stripe tokens only, no PAN), PII (emails/IP/UA in profiles/events — **no retention policy, L5**). CONFIDENTIAL — API keys (SHA-256 hashed), webhook secrets (plaintext but column-grant-revoked), job tokens, infra keys (env-only, not logged). INTERNAL — logs (no secrets/PII leak found), `/metrics` IP-allowlisted.

## Confirmed clean (FP-filtered — no finding)
No leaked secrets in history · CI minimal-priv, no `pull_request_target`/script-injection · Modal Dockerfiles no baked secrets · safe random session-key fallback · main-app job/campaign routes owner-scoped · admin email allowlist (404) · API keys SHA-256 + `secrets.token_urlsafe` · wallet ledger row-locked + idempotent + Stripe-sig-verified (no free GPU / no double-spend / no forged credit) · `/webhooks/modal` token-verified + CAS replay-guard · outbound webhook SSRF guard (DNS-rebind re-validated) · no SQLi / command-injection / template-injection · AlphaFold/RCSB fetches host-pinned + regex-gated.

## Remediation priority
1. **H1** — gate `/webhooks/heartbeat` with `job_token`; never bill on client seconds. (highest: live cross-user financial harm)
2. **M1** — UUID-validate + `safe_join` + owner-check all Scout file routes.
3. **M2** — hoist cookie/CSRF hardening out of the `ENABLE_PLATFORM_API` flag (confirm prod flag first).
4. **M3** — broaden `.gitignore` to `.env*` (before creating `.env.staging`).
5. L1/L3, then L2/L5/T1.

---
*AI-assisted scan (gstack /cso). Not a substitute for a professional penetration test. For a revenue/PII/payments system, engage a qualified firm for a full assessment; use this to close low-hanging fruit between professional audits.*
