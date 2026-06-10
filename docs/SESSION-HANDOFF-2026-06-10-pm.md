# tools-hub - handoff 2026-06-10 PM (alerting + outage detection)

Set up automated detection so the next outage pages us instead of being noticed
by hand, like the 2026-06-10 stall was. Wrote ALERTING.md (the full runbook plus
setup steps), shipped an operator email alert on new Platform API submissions,
and audited every request-path outbound call for bounded timeouts. Nothing
external was created: the uptime monitor and the Railway email both need Leo to
create or confirm them. The key finding is that the outage has two failure modes,
and a green /health does not prove the app is actually up for users.

## Done this session
- Wrote ALERTING.md at the repo root: incident analysis (both failure modes), the detection architecture, exact UptimeRobot and Railway setup steps, the Tier-3 timeout audit table, env vars, and a per-alert runbook.
- Shipped the operator email alert on new Platform API submissions: notify_operator_new_submission in shared/email.py, wired into create_experiment in tools/platform_api/routes.py. Off the request thread, best-effort, no-op without RESEND_API_KEY, fires only on a genuine 201 (not idempotent replays). Verified: pytest tests/test_platform_api.py passes 10/10, shared.email imports clean.
- Audited every web-dyno request-path outbound call for bounded timeouts. All bounded except one real gap: Supabase Storage uses storage3's 120s default, not the 30s PostgREST bound. Wrote a ready patch but did not apply it (see Blocked).
- Confirmed both monitor targets respond 200 right now (/health returns the ok body, / returns 200, both about 0.2s), so the setup steps rest on a verified live baseline.
- Captured the scope decisions with Leo: UptimeRobot for uptime, email to leo@ranomics.com as the single alert channel, Sentry deferred.

## Next steps
- Leo: create the free UptimeRobot account and the two monitors (/health keyword "ok", / expects 200), alert email leo@ranomics.com. Exact configs are in ALERTING.md.
- Leo: in Railway, confirm the deploy-failure notification email is leo@ranomics.com. His email choice uses Railway's native deploy emails, not the Webhooks feature (those are Slack/Discord only).
- Decide whether to commit scripts/smoke_platform_api.py so it can run as a scheduled synthetic monitor (GitHub Actions recommended; needs an RK_LIVE_KEY secret; each run leaves one lab_campaigns row).
- On Leo's word: apply the Supabase Storage timeout patch, add the /readyz deep readiness endpoint (catches the green-/health-but-app-down mode), and route the operator alert to Slack too.

## Blocked / waiting on
- Supabase Storage timeout patch is held, waiting on coordination: _client_options() in shared/supabase_client.py was rewritten by a parallel session mid-task (commit 56798cf), so applying my patch there risks a cross-session collision. The ready-to-apply patch is in ALERTING.md; apply once that function settles.
- All external monitoring is awaiting Leo's account creation and dashboard confirmation. Nothing external was created, per the agreed no-auto-signup rule.

## Notes (optional)
- The big finding: the 2026-06-10 outage has two modes. Mode A is the worker wedge where /health goes down (fixed in PR #28). Mode B is Supabase client construction failing, so login, wallet, and the Platform API are all down while /health and / keep returning 200 (fixed in commit 56798cf). Monitoring /health and / alone cannot catch Mode B, which is why ALERTING.md proposes a /readyz endpoint and elevates the synthetic smoke.
- HEAD is now 56798cf, one commit past the 13857b0 (PR #28) merge the task referenced. That SyncClientOptions fix landed mid-session from a parallel session.
- Working tree, uncommitted, for review: ALERTING.md (new), shared/email.py (added notify_operator_new_submission plus an import threading), tools/platform_api/routes.py (alert wired into create_experiment). No commits made (no-auto-commit rule).
- Stripe (about 80s SDK default) and Modal fn.spawn() (relies on Modal's gRPC deadline) are bounded but loose, with blast radius capped by the 2-worker fix. Documented in ALERTING.md, not changed.
- New optional env var: OPERATOR_ALERT_EMAIL, defaults to leo@ranomics.com.
