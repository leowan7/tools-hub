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

## Scheduling

Not scheduled by default — enabling deletion of production data is an
operator decision. To run it monthly, add a Railway cron service with the
start command `flask pii:purge-old` (mirror the `tools-hub-sweep-stuck`
cron setup in [SESSION-HANDOFF-2026-05-26.md](SESSION-HANDOFF-2026-05-26.md)).
Run `--dry-run` once first to confirm the counts look right.
