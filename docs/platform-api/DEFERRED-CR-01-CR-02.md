# Platform API — deferred CRITICAL findings (CR-01, CR-02)

Two CRITICAL findings from the 2026-06-04 fresh-context adversarial
review (REVIEW-PLATFORM-API-FRESH.md) are deferred. This file documents
why they're not blocking the first-key mint, what the fix looks like,
and when each must land.

## CR-01 — Single global `WEBHOOK_SIGNING_SECRET`

### What's broken

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

### Hard deadline

Before the **second** Platform-API customer is onboarded. Not before
the operator mints their first key.

---

## CR-02 — In-process dispatcher loses deliveries on backpressure / restart / crash

### What's broken

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

### Hard deadline

- **Before the first customer who depends on webhook reliability** —
  not necessarily before the first ARC-level dev integration test, but
  before any customer's pipeline routes off webhook signals.
- A simple heuristic: ship CR-02 before publishing the `/platform`
  page on ranomics.com to any audience beyond direct outreach.

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
