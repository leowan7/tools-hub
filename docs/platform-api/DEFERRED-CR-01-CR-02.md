# Platform API — deferred CRITICAL findings (CR-01, CR-02)

Two CRITICAL findings from the 2026-06-04 fresh-context adversarial
review (REVIEW-PLATFORM-API-FRESH.md) are deferred. This file documents
why they're not blocking the first-key mint, what the fix looks like,
and when each must land.

## CR-01 — Single global `WEBHOOK_SIGNING_SECRET`

**STATUS: SHIPPED 2026-06-08 (PR #15, commit `e2b900d`).** Migration
0028 applied to production Supabase the same day. Sections below
preserved as historical context; see "What shipped" at the bottom of
this section for the landed implementation.

### What's broken (historical)

`shared/webhooks.py:128` reads one process-wide secret and signs every
outbound webhook with it. Two customers verifying with the same secret
can each forge events the other will accept as authentic.

### Why it's not blocking first-key mint

- Alpha has **one customer** today: the operator.
- Cross-tenant attack requires two customers. We have time before the
  second customer onboards (Sprint sign or Pilot intake).
- Single-customer use is unaffected — the operator signs with their
  own secret and verifies with the same.

### What the fix looks like

Per-tenant signing secret. Sketch:

1. Migration: add `webhook_signing_secret_hashed text` column to
   `api_keys` (OR add `webhook_signing_secret_hashed text` to
   `profiles` keyed by `user_id` — TBD; profiles-keyed is cleaner for
   "rotate one secret across all keys").
2. `shared/api_keys.mint_token` returns a tuple of
   `(api_key_plaintext, webhook_secret_plaintext)`. Hash + persist
   webhook secret alongside the api key.
3. `/account/api-keys` page reveals BOTH at mint time. UI explains:
   "this is the only time you'll see either."
4. `shared/webhooks.dispatch_webhook` looks up the secret via
   `campaign.user_id` (not via env), feeds it to `sign_payload`.
5. Payload gains `owner_user_id` field for receiver-side cross-check.
6. OpenAPI manifest documents both the secret reveal and the new field.

### Hard deadline (historical)

Before the **second** Platform-API customer is onboarded. Not before
the operator mints their first key.

### What shipped (2026-06-08, PR #15)

1. `supabase/migrations/0028_user_profiles_webhook_secret.sql` —
   adds three nullable columns on `user_profiles`:
   `webhook_signing_secret` (plaintext HMAC key, format pinned to
   `whsec_<22-128 url-safe>`), `webhook_secret_prefix` (display-only
   first 8 chars), `webhook_secret_rotated_at` (audit timestamp).
   Column-level `REVOKE SELECT / INSERT / UPDATE` on the secret column
   for `PUBLIC`, `anon`, and `authenticated` keeps it off the
   anon/authenticated read path. Service-role only.
2. `shared/api_keys.py` — `mint_token` now returns a triple
   `(plaintext, prefix, webhook_secret_or_None)`. On a user's first
   API-key mint, `_new_webhook_secret()` produces a `whsec_` +
   `secrets.token_urlsafe(16)` value and persists it via
   `_write_webhook_secret`. Subsequent mints reuse the existing secret
   (idempotent). New helpers: `rotate_webhook_secret` (forces a new
   secret), `resolve_webhook_secret` (dispatch-time lookup),
   `get_webhook_secret_display` (dashboard helper that returns prefix
   + rotated_at only, never plaintext).
3. `shared/webhooks.py` — `_resolve_signing_secret(owner_user_id)`
   prefers the per-tenant secret, falls back to the
   `WEBHOOK_SIGNING_SECRET` env var during the transition window,
   returns `None` to fail closed. `dispatch_webhook` accepts an
   `owner_user_id` kwarg and grafts it into the signed payload so
   receivers can cross-check and the cron sweep can resolve the
   secret. `_dispatch_once` reads `owner_user_id` off the persisted
   payload and resolves the secret per-row, so the CR-02 sweep is
   safe across tenants.
4. `tools/platform_api/routes.py` — `_fire_webhook` now passes
   `owner_user_id=campaign.user_id` to `dispatch_webhook`.
5. `tools/platform_api/openapi_spec.py` — `webhook_url` description
   in the CreateExperimentRequest schema explains the per-tenant
   `whsec_` secret + Stripe-style `t=<ts>,v1=<hex hmac-sha256>` scheme.
   A new `WebhookEvent` schema documents the payload shape (required:
   `delivery_id`, `event_type`, `experiment_id`, `new_status`,
   `results_status`, `timestamp`; optional: `prev_status`,
   `owner_user_id`).
6. `app.py` + `templates/account_api_keys.html` — the dashboard now
   reveals the webhook secret exactly once on first mint (one-time
   panel; copy-now wording matches the API-key reveal). A status
   panel shows the prefix + last-rotated-at. A CSRF-protected
   "Rotate webhook secret" button posts to
   `account_api_keys_rotate_webhook_secret` and surfaces the new
   plaintext once.
7. Tests — 9 new in `tests/test_api_keys.py` (persist-on-first-mint,
   second-mint-no-rotate, rotate-replaces, resolve returns plaintext,
   unknown-user None, empty-id reject, display helpers, format
   pin) + 7 new in `tests/test_platform_api_hardening.py`
   (resolve-prefers-per-tenant, falls-back-to-env, both-missing,
   no-owner-falls-back, grafts-owner-into-payload, signs-with-
   per-tenant, refuses-when-no-secret). 88/88 platform-API tests
   pass.

The transition window: existing rows with NULL `webhook_signing_secret`
fall back to the `WEBHOOK_SIGNING_SECRET` env var, so legacy receivers
keep verifying as before. After the operator's next mint (or after
they hit the dashboard rotate button), the per-tenant column is
populated and the env-var path stops being exercised for that tenant.

---

## CR-02 — In-process dispatcher loses deliveries on backpressure / restart / crash

**STATUS: SHIPPED 2026-06-08 (PR #14, commit `17d8ffc`).** Migration
0027 applied to production Supabase the same day. Sections below
preserved as historical context; see "What shipped" at the bottom of
this section for the landed implementation.

### What's broken (historical)

`shared/webhooks.py:461-494` — the dispatcher runs in daemon threads
that sleep up to 6h between retries. Three failure modes silently
orphan rows in `webhook_deliveries`:

1. **Semaphore saturation** (`_bounded_dispatch` 's `acquire(blocking=False)`):
   row stamped with `next_retry_at`, nothing scans for it.
2. **Process restart** (Railway redeploys): in-thread `time.sleep` is
   killed, no resume logic.
3. **Unexpected exception** in `_dispatch_loop` (now narrower after
   ME-02 broadened `_post_once`'s except clause, but still possible
   elsewhere): thread dies between attempts.

Outcome: `delivered_at=NULL` forever. Customer's pipeline stalls. No
operator alarm.

### Why it's not blocking first-key mint

- First customer testing webhooks E2E will use a healthy local or
  Cloudflare-Workers endpoint. First attempt fires immediately and
  succeeds — no retry path engaged.
- For the operator's own debugging: a stuck delivery is visible by
  querying `webhook_deliveries WHERE delivered_at IS NULL` from the
  Supabase dashboard. Manual re-trigger is one INSERT away.
- The CR-02 risk materializes when both (a) the subscriber endpoint
  is initially unhealthy, AND (b) Railway redeploys (or the process
  crashes) during the retry sleep. That's an operations-day-30 issue,
  not a first-mint issue.

### What the fix looks like

A 60-second cron worker. Sketch:

1. In `app.py`, wrap `APScheduler` (already a candidate dep) or a
   bare `threading.Timer` recursion behind `ENABLE_PLATFORM_API=1`.
2. Worker polls:
   ```sql
   SELECT id, target_url, payload
     FROM webhook_deliveries
    WHERE delivered_at IS NULL
      AND next_retry_at <= now()
    ORDER BY next_retry_at
    LIMIT 50
    FOR UPDATE SKIP LOCKED;
   ```
3. For each row, call `_bounded_dispatch` (already idempotent).
4. Refactor `_dispatch_loop`: drop `time.sleep`. On failure, update
   `next_retry_at = now() + backoff` and return. Let the cron re-pick.
5. Operator alarm: a `webhook_deliveries WHERE delivered_at IS NULL
   AND created_at < now() - interval '1 hour'` count over 0 triggers a
   Slack notification (out of scope here; track separately).

`webhook_deliveries.next_retry_at` is already indexed (migration
0025), so the poll is cheap. `FOR UPDATE SKIP LOCKED` lets multiple
Railway replicas run the cron without double-firing.

### Hard deadline (historical)

- **Before the first customer who depends on webhook reliability** —
  not necessarily before the first ARC-level dev integration test, but
  before any customer's pipeline routes off webhook signals.
- A simple heuristic: ship CR-02 before publishing the `/platform`
  page on ranomics.com to any audience beyond direct outreach.

### What shipped (2026-06-08, PR #14)

1. `supabase/migrations/0027_webhook_deliveries_claim_rpc.sql` —
   `claim_due_webhook_deliveries(p_limit, p_lease_seconds)` SECURITY
   DEFINER RPC. `FOR UPDATE SKIP LOCKED` claims up to `p_limit` ready
   rows and bumps `next_retry_at` by the lease so a crashed worker
   can't double-fire. Service-role-only.
2. `shared/webhooks.py` — renamed `_dispatch_loop` to
   `_dispatch_once`. One attempt per call. Success stamps
   `delivered_at`; failure with retries left writes `next_retry_at`
   and returns; past max attempts stamps `delivered_at` with
   `last_error`. No more in-thread `time.sleep`. New
   `sweep_due_deliveries(limit=50)` calls the RPC and fans rows out to
   `_bounded_dispatch` threads.
3. `app.py` — APScheduler `BackgroundScheduler` registered behind
   `ENABLE_PLATFORM_API=1` and `WEBHOOK_SWEEP_ENABLED=1` (default ON).
   Interval = `WEBHOOK_SWEEP_INTERVAL_SECONDS` (default 60, floor 10).
   `coalesce=True, max_instances=1` so a slow tick doesn't pile up.
4. Tests: 7 new in `tests/test_platform_api_hardening.py` (single-
   attempt success, failure-reschedules-no-sleep, past-max-stamps,
   sweep-dispatches-each-row, drops-past-max, missing-service-client,
   skips-missing-target-url). 100/100 platform-API tests pass.
5. Smoke: post-merge live `GET https://tools.ranomics.com/api/v1/
   openapi.json` returns HTTP 200, OpenAPI 3.1.0, 7 endpoints.

The semaphore-saturation, process-restart, and unexpected-exception
failure modes are all closed: rows live in Postgres with an indexed
`next_retry_at` and the cron sweeps them every 60s. A Railway replica
won't double-fire because the RPC holds `FOR UPDATE SKIP LOCKED`.

---

## What was shipped today (fresh-review pass)

All HIGH + MEDIUM findings from REVIEW-PLATFORM-API-FRESH.md, plus
LO-08, plus the HI-01 false-positive verification (the reviewer
assumed Python 3.11 but production runs 3.13):

| ID | Fix | Test |
|---|---|---|
| HI-02 | Re-validate URL each retry iteration | `test_dispatch_loop_revalidates_url_each_retry` |
| HI-03 | CSRF token on `/account/api-keys` POSTs + SameSite=Strict | Flask test-client smoke (manual) |
| HI-04 | `_PREFIX_DISPLAY_LEN = len("rk_live_")` = 8 | `test_api_key_prefix_carries_no_plaintext_randomness` |
| HI-05 | 2.0s DNS timeout on `getaddrinfo` | `test_webhook_url_dns_lookup_has_timeout`, `..._timeout_rejects` |
| ME-01 | `_is_unique_violation` uses `postgrest.APIError isinstance` | `test_is_unique_violation_detects_real_postgrest_api_error`, `..._rejects_non_23505` |
| ME-02 | `_post_once` catches broad `Exception` + decode guard | `test_post_once_swallows_unexpected_exception`, `..._post_exception` |
| ME-05 | `get_campaign` logs swallowed exceptions at WARNING | exercised by existing tests + manual check |
| ME-06 | `_LAST_USED_THROTTLE_SECONDS` clamped to >= 1 | `test_throttle_env_rejects_zero/negative/non_integer/valid` |
| LO-08 | `_fire_webhook` stops passing `delivery_id: None` sentinel | `test_fire_webhook_caller_does_not_pass_delivery_id_sentinel` |
| HI-01 (false positive) | Verified `::ffff:127.0.0.1` rejected on Py 3.13 | `test_ipv4_mapped_ipv6_literal_rejected` (parametrized) |

Test count: **72/72 passing** in the platform-API suite.

The fresh-review reviewer's CR-01 and CR-02 findings remain valid;
this file is the receipt that they're tracked and timeboxed.
