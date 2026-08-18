# PII retention (cso audit L5)

The two append-only event logs carry personal data:

- `public.user_events` — `ip`, `user_agent`
- `public.signup_rejections` — `email`, `ip`

`public.user_profiles` is the per-user account record (keyed by `user_id`),
not an event log, and is intentionally **not** purged.

## Command

```
flask pii:purge-old            # delete rows past the window
flask pii:purge-old --dry-run  # count only, delete nothing
```

Retention window: `PII_RETENTION_DAYS` env var (default **365**, floored at
**30** so a misconfiguration can never wipe recent data). Deletes are
batched (1000 rows/statement); both tables have an indexed `created_at`.

Mechanism lives in [`cron/purge_old_events.py`](../cron/purge_old_events.py);
CLI wrapper in `app.py` (`pii:purge-old`).

## Epitope Scout uploads (`tmp/<job_id>/`)

Scout is reachable **without an account**, so an uploaded structure may carry
no user identity at all — it is still user data (an unpublished target is
often the most sensitive thing a visitor has).

Policy, enforced in code rather than by a cron:

- **Window: 1 hour.** `scout.jobs.cleanup_old_jobs` (`max_age_seconds=3600`)
  deletes any job directory whose mtime is older than that. It runs as a side
  effect of the three intake routes (`/scout/upload`, `/scout/fetch-pdb`,
  `/scout/example`), so the sweep fires on live traffic, not on a schedule.
- **Scope: UUID-named directories only.** `tmp/` is shared with other tenants
  (`tmp/calibration/`, `tmp/pdb_compare/`); the reaper's name filter is the
  safety property. Do not widen it — see the incident note in the function's
  docstring and `tests/test_scout_access_control.py::TestCleanupOldJobsScope`.
- **Unparseable uploads are deleted immediately**, not held for the window.
- **Confidentiality:** job ids are UUID4 and every read is ownership-checked
  against a `.owner` marker. Anonymous jobs are owned by a random per-session
  id (`anon:<hex>`) held in the signed session cookie, so an anonymous job is
  readable only by the browser session that created it.
- **Anonymous volume is bounded** by `ANON_MAX_LIVE_JOBS` /
  `ANON_MAX_LIVE_JOBS_PER_SESSION` in `scout/routes.py`, so the amount of
  user data resident at any moment has a stated ceiling.

Nothing about an anonymous Scout run is written to Supabase: `record_scout_run`
is skipped when there is no session email, so no row and no PII leaves the box.

## Scheduling

Not scheduled by default — enabling deletion of production data is an
operator decision. To run it monthly, add a Railway cron service with the
start command `flask pii:purge-old` (mirror the `tools-hub-sweep-stuck`
cron setup in [SESSION-HANDOFF-2026-05-26.md](SESSION-HANDOFF-2026-05-26.md)).
Run `--dry-run` once first to confirm the counts look right.
