# Alerting and Outage Prevention

How we find out automatically when tools.ranomics.com breaks, and how to respond.

Written after the 2026-06-10 outage, which was discovered only because Leo
manually noticed the site stalling. The point of everything below is that the
next outage pages us instead.

## TL;DR status

| Piece | Tier | State | Needs from Leo |
| --- | --- | --- | --- |
| External uptime monitor (UptimeRobot) on `/health` + `/` | 1 | Done — 2 monitors live (`/health` Up, `/` active), 5-min, email leo@ | None (keyword upgrade needs a paid plan) |
| Railway native deploy-failure email | 1 | Verified — Deployment Failed/Crashed/OOM → Email & In-App to leo@ranomics.com | None |
| `/readyz` deep readiness endpoint (catches the failure mode the two monitors above miss) | 1b | Implemented (uncommitted); UptimeRobot monitor pre-created + paused | Resume that monitor after deploy |
| Synthetic monitor (smoke script on a schedule) | 2 | Script live (`a502308`); workflow created (uncommitted) | Add `RK_LIVE_KEY` repo secret |
| Sentry error tracking | 2 | Deferred by decision | Revisit later |
| Outbound-timeout audit | 3 | Done; Storage patch applied (uncommitted) | Review the Storage patch diff |
| Operator alert on new Platform API submission | Bonus | Shipped in working tree (uncommitted) | Review the diff |

Update (2026-06-10): the UptimeRobot monitors are created and Railway's deploy
alerts are verified (see those sections). The one remaining external action is
adding the `RK_LIVE_KEY` GitHub repository secret for the synthetic smoke;
entering that key is Leo's action (steps below).

## The incident, both failure modes

On 2026-06-10 tools.ranomics.com went fully down. Investigation surfaced **two
distinct failure modes**, both already fixed in code. They matter here because
they have **different detection signatures**, and a monitor that catches one can
completely miss the other.

### Mode A: worker wedge (whole site hangs)

The app ran a single gunicorn worker. A synchronous Supabase analytics insert in
the request path (`shared/events.py` `log_event`, via `POST /api/track`) blocked
on a stalled pooled HTTP/2 connection for the library-default 120s timeout. One
wedged request pinned the only worker, so every request hung. TLS completed but
no HTTP response ever came back.

Fixed in PR #28 (merged, deployed):
- 2 gunicorn workers via `gunicorn.conf.py` (`WEB_CONCURRENCY`, floored at 1).
- Bounded 30s Supabase PostgREST timeout in `shared/supabase_client.py`
  (`SUPABASE_CLIENT_TIMEOUT_S`, connect capped at 5s).
- `log_event` now runs inserts off the request thread (daemon threads behind a
  `BoundedSemaphore(4)`, shedding when full).

Detection signature: `/health` hangs or times out. An external uptime monitor
catches this immediately.

### Mode B: authenticated surface down, `/health` stays green

A follow-up commit (`56798cf`, landed 2026-06-10 after PR #28) documents a
second, scarier mode. `_client_options()` built a base `ClientOptions`, but
supabase-py's **sync** `create_client` reads `options.storage` when it builds
the auth sub-client. Base `ClientOptions` lacks that attribute, so construction
raised `AttributeError: 'ClientOptions' object has no attribute 'storage'`. The
factory caught it and returned `None`, so the Supabase client silently failed to
build. Every authenticated or DB-backed route (login, wallet, credits, Platform
API) went down, while `/health` (static, DB-free) and the anonymous landing page
`/` (static catalog) **both kept returning 200**.

Fixed in `56798cf` by using `SyncClientOptions` with a fallback to
`ClientOptions` for older supabase versions.

Detection signature: `/health` is green, `/` is green, but the app is broken for
every real user. **The two Tier-1 monitors below cannot see Mode B.** This is the
single most important design fact in this document, and it is why `/readyz`
(Tier 1b) and the synthetic monitor (Tier 2) exist.

## Detection architecture (defense in depth)

Each layer catches what the layer above misses.

1. **External uptime monitor** on `/health` and `/`. Catches a total outage
   (Mode A, process death, TLS failure, Railway down) even when the app cannot
   log its own errors. Off-network, so it sees what users see.
2. **`/readyz` deep readiness check.** Catches Mode B: DB or Supabase-client
   failures while `/health` stays green. One cheap bounded Supabase read.
3. **Synthetic monitor** running the end-to-end smoke (submit, idempotent
   replay, read-back) against the real Platform API. Catches deeper breakage:
   auth, idempotency, persistence, response-shape regressions.
4. **Railway native notifications.** Catches deploy failures, crashes, and OOM
   kills at the platform level, before or independent of HTTP symptoms.
5. **Error tracking (Sentry).** Catches unhandled exceptions and slow requests
   with stack traces. Deferred for now.

---

## Tier 1: External uptime monitor (UptimeRobot)

Provider chosen by Leo: **UptimeRobot** (free tier, 5-minute checks). Alert
destination: **email to leo@ranomics.com**.

### Why this is the most important single piece

It runs outside Railway. If the whole app, the dyno, or Railway's edge dies, an
in-app alert can never fire, but an external prober still notices and emails.

### Monitors to create

Sign in at https://uptimerobot.com (free account), then add two monitors:

**Monitor 1: health endpoint**
- Type: HTTP(s) (keyword)
- URL: `https://tools.ranomics.com/health`
- Keyword type: exists
- Keyword: `ok` (the endpoint returns `{"status":"ok"}`; keyword matching
  confirms it is the real app responding, not a Railway error page returning 200)
- Interval: 5 minutes (free-tier minimum)
- Alert contacts: email leo@ranomics.com

**Monitor 2: landing page**
- Type: HTTP(s)
- URL: `https://tools.ranomics.com/`
- Expect: HTTP 200
- Interval: 5 minutes
- Alert contacts: email leo@ranomics.com

### Alert contact setup

In UptimeRobot, **My Settings > Add Alert Contact > E-mail**, enter
leo@ranomics.com, confirm the verification email, then attach that contact to
both monitors. Set "notify when down" and "notify when back up".

### Known blind spot

Both monitors stay green during Mode B (see the incident section). They prove the
process is alive and serving static content. They do not prove the database or
the authenticated surface works. That gap is closed by `/readyz` below.

---

## Tier 1: Railway native notifications

Project: tools-hub (`607bc08f-6954-41d5-b3e8-543c8a8e73f4`). Web service:
`d118e62a-64e8-4f73-9b25-d5536e14c7a9`. Environment:
`ce3eedc4-fcd8-49c5-b0e2-23ca9de785d4`.

### Important: email vs webhook on Railway

Railway has two separate notification mechanisms:

1. **Native email notifications.** Railway emails project members when a
   deployment fails or crashes. This is the path for Leo's email choice. No
   webhook URL is involved.
2. **Webhooks** (Project Settings > Webhooks). These auto-format a payload for
   **Slack or Discord** (or a custom HTTP endpoint) and require a webhook URL.
   They support granular event types: Deployment Crashed, Oom Killed, Failed,
   Deployed, Redeployed, Slept, Resumed, Restarted, Removed, Building, Deploying,
   Waiting, plus Monitor Triggered/Resolved/Deleted. Webhooks **cannot send
   plain email.**

Because Leo chose email, we use mechanism 1.

### Setup (email path)

1. In the Railway dashboard, open the tools-hub project.
2. Confirm the account or project member email that receives notifications is
   leo@ranomics.com. If the workspace owner email is different, add
   leo@ranomics.com as a project member, or update notification settings under
   Project Settings so failure emails reach it.
3. Confirm deployment failure email notifications are enabled (they are on by
   default for project members).

### Optional upgrade to granular push

If Leo later wants instant phone push for the specific events Crashed / Oom
Killed / Failed, create a Slack or Discord incoming webhook, paste the URL into
Project Settings > Webhooks, and select those events. Hand me the webhook URL and
I will wire and document it. This is the only way to get per-event Railway alerts;
the native email path covers failed and crashed deploys but is less granular.

---

## Tier 1b: `/readyz` deep readiness endpoint (implemented, uncommitted)

**This is the recommended fix for Mode B and the highest-value server-side
change for the stated goal.** Implemented in `app.py` next to `/health`
(uncommitted, for review). The cross-session collision risk that held it back
has cleared: `_client_options()` is committed and clean, and the parallel
session is touching only templates and `tools/*/__init__.py`, not infra files.

Unlike `/health` (static, DB-free), `/readyz` performs one cheap, bounded
Supabase read and returns 503 when the authenticated surface is broken. An
UptimeRobot keyword monitor on it catches Mode B directly, every 5 minutes, with
no database rows created and no script to commit. It uses the **service-role**
client because `user_events` is service-role-only under RLS, and is placed above
the `login_required` routes so an external prober (which cannot log in) reaches
it. Verified locally: the `ready` / `no_client` / `db_error` paths return
200 / 503 / 503.

As implemented in `app.py`, next to `/health`:

```python
@flask_app.route("/readyz", methods=["GET"])
def readyz():
    """Deep readiness probe (catches incident 2026-06-10 Mode B)."""
    from shared.credits import get_service_client  # noqa: PLC0415

    try:
        client = get_service_client()
        if client is None:
            return jsonify({"status": "degraded", "reason": "no_client"}), 503
        client.table("user_events").select("id").limit(1).execute()
        return jsonify({"status": "ready"}), 200
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        logger.warning("readyz degraded: %s", exc)
        return jsonify({"status": "degraded", "reason": "db_error"}), 503
```

Then add an UptimeRobot keyword monitor: URL
`https://tools.ranomics.com/readyz`, keyword exists `ready`, interval 5 min,
email leo@ranomics.com. A degraded response returns 503 (monitor goes down) and
also fails the keyword check, so it alerts two ways.

---

## Tier 2: Synthetic monitor (end-to-end smoke)

Script: `scripts/smoke_platform_api.py`. It exercises the live submit, idempotent
replay, and read-back loop against `https://tools.ranomics.com/api/v1/*` using
`RK_LIVE_KEY`. Exit code 0 on all-pass, 1 on any failure. This is the deepest
check: it catches Mode B plus auth, idempotency, persistence, and response-shape
regressions that a simple URL ping cannot.

### Two prerequisites and one caveat

1. **The script is committed and pushed** (`a502308` on `main`), and the
   GitHub Actions workflow now exists at
   `.github/workflows/synthetic-smoke.yml` (uncommitted; it rides the next
   commit). The scheduled cron only starts firing once that workflow file is on
   the default branch, so it activates on the next push to `main`.
2. **It needs `RK_LIVE_KEY`**, a member-role Platform API key minted at
   https://tools.ranomics.com/account/api-keys, stored as a secret in whatever
   runs it.
3. **Caveat: the smoke cleans up after itself, with two exceptions.** Its final
   step is `DELETE /experiments/{id}`, which runs whenever the create response
   carried an experiment_id — including when create's own response-shape
   assertions failed, or the replay or read-back ones did — and then asserts the
   follow-up read 404s. A run therefore leaves no `lab_campaigns` row behind
   whether it passes or fails, and there is nothing to bulk-clean on a cadence.
   Three cases still leak a row, and all three fail the job. Only the first
   names the row in the log:
   - **Withdraw itself fails:** the run log prints the experiment_id and the
     `DELETE FROM lab_campaigns WHERE id = '...';` SQL to drop the row by hand.
     A connection that dies any time **after** the create response lands here
     too, rather than killing the run: `_http()` converts every transport error
     into a `status=0` sentinel — including the `RemoteDisconnected` /
     `IncompleteRead` / `ConnectionResetError` family that urllib does **not**
     wrap into `URLError` — so the smoke still reaches its summary and still
     names the row, instead of dying with a traceback that would leak it
     unreported.
   - **The 201 body carries no usable experiment_id** (not a JSON object, or no
     `experiment_id` field): the row exists, but no id ever reached the client,
     so there is nothing to withdraw and nothing to print.
   - **The create call itself dies in transit:** the server may already have
     committed the row, but no response came back, so again no id exists. The
     log shows `(no experiment_id captured; submit step did not return one)`.

   In the last two the row has to be found by hand, around the run's timestamp,
   at https://tools.ranomics.com/admin/lab-projects.

   (The script has a second, service-role cleanup path for its optional quote
   round-trip; it stays dormant in CI, which passes only `RK_LIVE_KEY`.)

### Option A (recommended): GitHub Actions scheduled workflow

Runs outside Railway (a second external vantage point), and GitHub emails on
workflow failure for free. **Created** at `.github/workflows/synthetic-smoke.yml`
(the committed file is the source of truth; beyond the outline below it also adds
a 10-minute timeout, a `concurrency` guard, a missing-secret guard step, and pins
Python 3.12). Outline:

```yaml
name: synthetic-smoke
on:
  schedule:
    - cron: "0 */6 * * *"   # every 6 hours
  workflow_dispatch: {}
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python scripts/smoke_platform_api.py
        env:
          RK_LIVE_KEY: ${{ secrets.RK_LIVE_KEY }}
```

Add the repo secret `RK_LIVE_KEY`, and confirm GitHub Actions failure
notifications go to leo@ranomics.com (GitHub account notification settings).

### Option B: Railway cron service

Railway can run a scheduled command in this repo (the app already uses a cron
module, see `Procfile` `release:` and `cron/`). A cron service running
`python scripts/smoke_platform_api.py` works, but the failure signal has to be
wired (it would have to emit to Resend or a webhook on non-zero exit, since
Railway cron does not natively alert on a script's exit code as cleanly as a
deploy failure). Option A is simpler and gives an external vantage point.

---

## Tier 2: Error tracking (Sentry)

Deferred by decision on 2026-06-10 to keep this change focused on Tier 1. When
revisited, the wiring is small: add `sentry-sdk[flask]` to `requirements.txt`,
initialize with the Flask integration guarded by a `SENTRY_DSN` env var (no-op
when unset), and enable `traces_sample_rate` for slow-request capture. It would
catch unhandled exceptions and slow transactions with stack traces, which the
uptime and readiness checks cannot. No account created yet; the free tier needs a
signup.

---

## Tier 3: Outbound request-path timeout audit

Goal: every outbound call on the **web-dyno request path** must have a bounded
timeout, so no single stalled downstream can re-wedge a worker (Mode A). Modal
side pipelines (`tools/*/run_pipeline.py`) run on GPU pods, not the web dyno, and
already carry their own timeouts; they are out of scope for worker-wedge but
listed as already-bounded for completeness.

### Results

| Call site | Path | Timeout | Verdict |
| --- | --- | --- | --- |
| `app.py:144` AlphaFold metadata fetch | request | 8s | bounded |
| `app.py:165` AlphaFold PDB fetch | request | 20s | bounded |
| `scout/routes.py:220` structure fetch | request | 30s | bounded |
| `shared/email.py` all 6 Resend posts | request | 10s | bounded |
| `shared/email.py:1705` Slack/Discord webhook | request | 10s | bounded |
| `shared/events.py:280` PostHog capture | off-thread | 2s | bounded |
| `shared/events.py` `log_event` Supabase insert | off-thread | 30s | bounded (PR #28) |
| `scout/epitope_db.py` all UniProt/RCSB calls | request | 12s (`_REQUEST_TIMEOUT_SEC`) | bounded |
| Supabase PostgREST (all `.table().execute()`) | request | 30s | bounded (PR #28) |
| Supabase Storage (`shared/storage.py` upload + signed URL) | request | 20s default, pinned to 30s | bounded (patch applied) |
| Stripe SDK (`billing/checkout.py`, `webhooks/stripe.py`) | request | 15s + 1 retry | bounded (patch applied) |
| Modal `fn.spawn()` (`gpu/modal_client.py:257`) | request | Modal gRPC deadline | not app-bounded |

### Finding 1: Supabase Storage timeout (bounded; patch applied)

`shared/storage.py` `upload_input` and `presigned_input_url` run inside the
job-submit request path. They use `get_service_client()`
(`shared/credits.py:51`), which builds the client with `_client_options()`. That
options object set only `postgrest_client_timeout`; Storage (storage3) uses a
**separate** `storage_client_timeout`.

**Correction (2026-06-10):** the original audit recorded this default as 120s.
In the installed supabase-py / storage3, `storage3.constants.DEFAULT_TIMEOUT` is
actually **20s** — already short, and tighter than the 30s PostgREST budget. So
there was no live 120s gap. The default is version-dependent (it has been larger
in other storage3 releases), so the hardening still has value as future-proofing,
just not as an active-outage fix.

**Applied (uncommitted):** `_client_options()` now pins `storage_client_timeout`
to the same `SUPABASE_CLIENT_TIMEOUT_S` budget (30s) under a `hasattr` guard, so
one env knob governs every Supabase sub-client and a future storage3 default bump
can never silently reintroduce a long, worker-pinning timeout (the Mode A failure
class). This nudges the Storage bound from 20s to 30s — consistent with
PostgREST's read bound and appropriate for upload-sized payloads, and both sit
well within the 2-worker + `GUNICORN_TIMEOUT` blast-radius cap. storage3 takes a
scalar int, so unlike PostgREST there is no separate 5s connect cap; the HTTP/1.1
forcing in `_force_supabase_http1()` (commit `fd39003`) already removes the
stale-h2 read-hang that originally motivated the bound.

### Finding 2: Stripe (bounded; patch applied)

`billing/checkout.py` previously set `stripe.api_key` with no explicit timeout,
so the Stripe SDK default (~80s socket timeout + 2 retries) applied to
`checkout.Session.create`, `PaymentIntent.create`, and the webhook `retrieve`
calls — bounded, but loose enough to pin a worker on the wallet/topup path.

**Applied (uncommitted), by the parallel hardening pass:** `_stripe_client()`
now sets `stripe.max_network_retries = 1` and a `RequestsClient(timeout=15)`,
version-guarded across the `stripe.http_client` / `stripe._http_client` rename so
it degrades to SDK defaults rather than crash. 15s is generous for a Checkout
Session create/retrieve and sits under the `GUNICORN_TIMEOUT` floor, so a worker
is never force-killed mid-call. The webhook signature path is unchanged.

### Finding 3: Modal `fn.spawn()` (not app-bounded)

`gpu/modal_client.py` `submit()` calls `modal.Function.from_name(...).spawn(...)`
on the request path. `spawn` enqueues the job and returns quickly in the normal
case, but it makes a synchronous gRPC call to Modal's control plane. There is no
app-level timeout on it; it relies on Modal's internal gRPC deadline. If Modal's
control plane stalls, the 2-worker plus `GUNICORN_TIMEOUT` config caps the blast
radius to one worker. If Modal control-plane stalls ever recur, wrap the spawn in
a thread with a join-timeout. Documented as residual, not changed.

### Finding 4: scout service clients now bounded

`scout/handoff.py` and `scout/quota.py` built their service-role clients with a
bare `create_client(url, key)`, bypassing `_client_options()` and so inheriting
the 120s library default on the scout request path. The parallel hardening pass
routed both through `_client_options()`, so they now carry the same bounded
PostgREST/Storage timeout and the HTTP/1.1 patch as `shared.credits`.
`requirements.txt` was also pinned to `supabase>=2.28,<2.32` to keep the private
module paths the HTTP/1.1 patch reaches into from shifting under a minor bump.

### Off-the-hot-path review (Mode A class)

The `events.py` off-thread pattern is the template for request-path side
effects. Remaining synchronous external writes in request handlers:
- `shared/events.py` `log_signup_rejection`: synchronous Supabase insert, but
  only on rejected (bot) signups, single insert, now bounded at 30s. Low volume,
  low risk. Could move off-thread later.
- Resend email sends in request handlers: synchronous but bounded at 10s. Adds up
  to 10s of latency in the worst case; acceptable, could move off-thread later.
- The new operator alert (Bonus, below) already runs off-thread by design.

---

## Bonus: operator alert on new Platform API submission

A real customer submission through the MCP server or REST API
(`POST /api/v1/experiments`) should not sit unseen. Shipped in the working tree
(uncommitted, for Leo's review):

- `shared/email.py` `notify_operator_new_submission(...)`: emails
  `OPERATOR_ALERT_EMAIL` (default leo@ranomics.com) with the experiment id, name,
  type, target, sequence count, and submitter user id. Runs in a daemon thread
  behind a `BoundedSemaphore(2)` (shed when full), reuses the bounded
  `_post_resend` (10s), and never raises. This mirrors the `events.py`
  off-the-hot-path rule, so it can never delay or wedge the API response.
- `tools/platform_api/routes.py` `create_experiment`: fires the alert only on a
  genuine create (HTTP 201). The idempotent-replay path returns 200 earlier, so
  replays do not alert.

Verified: `python -m pytest tests/test_platform_api.py` passes (10/10) and
`shared.email` imports clean. No-op without `RESEND_API_KEY`.

If Leo prefers Slack for this growth signal, `shared/email.py` already has
`alert_ops_slack` gated on `SLACK_OPS_WEBHOOK_URL`; say the word and I will route
it there in addition to or instead of email.

---

## Environment variables

| Var | Where | Default | Purpose |
| --- | --- | --- | --- |
| `OPERATOR_ALERT_EMAIL` | new (this work) | leo@ranomics.com | Recipient of the new-submission operator alert |
| `WEB_CONCURRENCY` | `gunicorn.conf.py` | 2 | Worker count; >1 prevents Mode A from downing the whole site |
| `GUNICORN_TIMEOUT` | `gunicorn.conf.py` | 120 (floor 60) | Worker recycle timeout |
| `SUPABASE_CLIENT_TIMEOUT_S` | `shared/supabase_client.py` | 30 | Bounds PostgREST and Storage (storage3) |
| `RESEND_API_KEY` | `shared/email.py` | unset | Enables outbound email; alerts no-op without it |
| `SLACK_OPS_WEBHOOK_URL` | `shared/email.py` | unset | Optional ops Slack channel |
| `RK_LIVE_KEY` | synthetic monitor runner | unset | Member-role Platform API key for the smoke |
| `SENTRY_DSN` | future | unset | Enables Sentry when Tier 2 error tracking is revisited |

---

## Runbook: responding to an alert

### UptimeRobot says `/health` or `/` is DOWN

1. Open https://tools.ranomics.com/health in a browser. Hanging or timing out
   confirms a real outage (likely Mode A or a dead dyno).
2. Railway dashboard, tools-hub web service: check Deployments (did a deploy just
   fail or crash?) and Metrics (CPU, memory, an OOM kill).
3. Check recent logs for stack traces, gunicorn worker timeouts, or a boot loop.
4. If a recent deploy broke it: roll back to the last good deployment in Railway.
5. If workers are wedged with no bad deploy: restart the service. Then find the
   stalled downstream (Supabase, Modal, Stripe, Resend) in the logs and confirm
   the relevant timeout from the audit table is actually firing.

### `/readyz` is DOWN but `/health` is UP (Mode B)

The process is alive but the database or Supabase client is broken. The
authenticated surface (login, wallet, Platform API) is down for users even though
the site looks up.

1. Check Supabase status and the Supabase project dashboard for an outage or
   connection limit.
2. Check app logs for "Could not build bounded Supabase ClientOptions", "Could
   not create Supabase client", or `AttributeError ... no attribute 'storage'`
   (the Mode B signature). A supabase-py version bump can reintroduce this.
3. If it is a code or dependency regression: roll back the last deploy.
4. If it is a Supabase-side outage: there is nothing to deploy; monitor Supabase
   and post status if needed.

### Synthetic smoke FAILED

Read which step failed in the output (targets, cost-estimate, create, replay,
read-back, withdraw). A create failure with `/health` green is Mode B. A replay
or read-back failure points at idempotency or persistence. Several steps failing
at once, each noting `network error:` and a status of `0`, is a transport
failure (edge, DNS, TLS, or a reset connection), not an API-logic bug.
Reproduce locally with `RK_LIVE_KEY=... python scripts/smoke_platform_api.py`.

Then check the cleanup line at the bottom of the summary:

- **"created and withdrawn; no leftover row"** — nothing to do.
- **"created but NOT cleaned up"** — the summary names the experiment_id and
  prints the `DELETE FROM lab_campaigns ...` SQL; run it.
- **"no experiment_id captured"** — a row may exist that the run never learned
  the id of (see caveat 3 above). Check
  https://tools.ranomics.com/admin/lab-projects around the run's timestamp.
- **No RESULTS block at all** — the smoke crashed before its summary, which is a
  bug in the smoke itself: it is supposed to convert every transport error into
  a `status=0` sentinel, and to decode bodies leniently, so the run always
  reaches `_summarise()` (regression tests in
  `tests/test_smoke_platform_api_network.py`). A row may have leaked
  unreported; find it at https://tools.ranomics.com/admin/lab-projects.

### Railway emailed a deploy failure / crash / OOM

1. Open the deployment in Railway and read the build or runtime logs.
2. Build failure: fix and push (Railway auto-deploys main).
3. Crash loop or OOM: check memory; OOM may mean a leak or a too-large worker
   count for the plan. Roll back if a recent change caused it.

### Operator alert: new Platform API submission

Not an incident. A customer submitted an experiment through the API or MCP
server. Review at https://tools.ranomics.com/admin/campaigns and follow up.

---

## Appendix: dashboards, URLs, IDs

- Live site: https://tools.ranomics.com
- Health (static, DB-free): https://tools.ranomics.com/health
- Readiness (DB-touching, once added): https://tools.ranomics.com/readyz
- Platform API base: https://tools.ranomics.com/api/v1
- Admin campaigns: https://tools.ranomics.com/admin/campaigns
- API key minting: https://tools.ranomics.com/account/api-keys
- UptimeRobot: https://uptimerobot.com (account to be created by Leo)
- Railway project tools-hub: `607bc08f-6954-41d5-b3e8-543c8a8e73f4`
- Railway web service: `d118e62a-64e8-4f73-9b25-d5536e14c7a9`
- Railway environment: `ce3eedc4-fcd8-49c5-b0e2-23ca9de785d4`
- Alert destination: email leo@ranomics.com
- Trunk: `main`, auto-deploys to Railway on push.
