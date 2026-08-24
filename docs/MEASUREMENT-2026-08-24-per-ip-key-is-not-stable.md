# The anonymous per-IP limiter keyed on a rotating edge hop — measured, fixed, verified

**2026-08-24.** Rewritten the same day, deliberately. Earlier versions carried
the findings as a body plus three layers of struck-through retraction; every
false claim this document has ever contained lived in that retraction prose,
never in a measurement. The corrections are now collected in one appendix at the
bottom, and `git log -p` on this file holds every earlier version.

Line numbers below are as of **`547d622`**, when the measurements were taken.
`#189` shifted several of them.

## Status

- **Mechanism.** Railway's edge OVERWRITES the whole `X-Forwarded-*` family and
  rewrites the header as `<real client>, <internal proxy>`. That trailing
  internal hop ROTATES across a pool.
- **Defect.** `_client_ip()` resolved one hop from the right, so it keyed on the
  rotating hop. The per-IP tier never refused anyone.
- **Fix.** Prefer `X-Real-Ip`, which the edge sets from the socket it saw.
  `#189`, main `237fbf3`.
- **Verified in production.** 46 admitted / 0 refused before; 20 admitted and
  refused at 21 with `reason="rate_limited"` after.
- **Still open.** The NAT case; the wall is `limit x workers` = 20, not 10; the
  counters reset on every deploy and worker recycle, so plan criterion 5 is NOT
  met; and nothing alarms if `X-Real-Ip` stops arriving.

## What was measured

Three facts, in the order they were established, against prod at `547d622`.

### 1. The fleet runs 2 worker processes

`hit()` (`scout/ratelimit.py:138`) is an in-process dict guarded by a thread
lock, with no shared store — Phase 3 was never built. So the fleet-wide wall for
any key is `limit x worker processes`, and worker count is measurable from
outside by finding a wall.

The cookieless session bucket is keyed on the **constant** string
`_NO_SESSION_KEY = "anon:no-session"` (`scout/ratelimit.py:180`) with
`ANON_ANALYZE_SESSION_LIMIT = 8`. Driving it cookieless, 48 x
`POST /scout/analyze` with no cookie and a bogus `job_id`: exactly **16
allowed**, then every subsequent request refused (`no_session`).

16 = 8 x 2. **`WEB_CONCURRENCY` is 2**, measured rather than read off a
dashboard. The refusals interleaved with allowances (10, 11, 12 refused; 13, 14,
15 allowed; 16 refused) before saturating — the signature of two independent
in-memory counters with requests distributed unevenly across them.

This is also the control for what follows: **a constant key does produce a wall,
on this fleet, in these minutes.**

### 2. A constant forged `X-Forwarded-For` produced no wall

`scout_intake` (`/upload`, `/fetch-pdb`, `/example`) passes no `session_limit`,
so `anon_rate_limit` skips the session tier entirely (`scout/ratelimit.py:863`)
and the bucket is keyed purely on `_client_ip()`, at `ANON_INTAKE_LIMIT = 10`.
With 2 workers a constant key must wall at 20.

| probe | header | result |
| --- | --- | --- |
| 26 x `GET /scout/example` | `X-Forwarded-For: 203.0.113.11` (constant) | 26 allowed, **0 refused** |
| 46 x `POST /scout/upload` | `X-Forwarded-For: 203.0.113.55` (constant) | 46 allowed, **0 refused** |

Two earlier shape probes on `/example` carried no header, making 28 there. Both
runs completed inside one 600 s window.

The requests were genuinely metered, not skipped: the `/metrics` denominator
read **exactly 122**, matching the probe's own total of 28 + 46 + 48, with
`rate_limited` at 0. Eviction is excluded — `_MAX_KEYS = 20_000`
(`scout/ratelimit.py:128`).

### 3. The mechanism, read directly rather than inferred

`GET /debug/client-ip` (added in `#188`) reports what the process actually
received. Ten identical requests plus four forged-header shapes, from two GitHub
runners:

| sent | received |
| --- | --- |
| nothing | `X-Forwarded-For: 4.236.158.49, 152.233.30.104` |
| `X-Forwarded-For: 192.0.2.111, 192.0.2.222` | `X-Forwarded-For: 4.236.158.49, 152.233.30.102` |
| `X-Real-Ip: 198.51.100.7` | `X-Real-Ip: 20.57.133.114` |
| `X-Forwarded-Host: evil.example` | `X-Forwarded-Host: tools.ranomics.com` |

Every forged header discarded. The trailing hop rotated across
`152.233.30.101/.102/.104` in one run and `84.17.44.225/.226/.229` from a second
runner, and `remote_addr` tracked it.

**Stated precisely:** the key came from a small rotating POOL, not a fresh value
per request. Three addresses x 2 workers x limit 10 puts the effective wall near
60, and the run that saw no refusal sent 46 — so the missing wall is fully
explained without "a different value every request". The pool's true size was
never measured.

## Verified in production

Fix deployed as `#189`, main `237fbf3`; `/health` returned
`{"build":"237fbf3...","status":"ok"}` before the probe ran.

```bash
for i in $(seq 1 26); do curl -s -o /dev/null -w "%{http_code} " -X POST https://tools.ranomics.com/scout/upload; done
```

**Sent 26, admitted 20, refused 6, first refusal at request 21**, body
`{"error":"Too many Epitope Scout requests from this network.","reason":"rate_limited","retry_after":591}`.

20 is exactly `ANON_INTAKE_LIMIT` (10) x 2 workers. The `rate_limited` label is
not the load-bearing part: `/scout/upload` passes no `session_limit`, so the
session tier is unreachable there by construction and a limiter refusal on that
route can only be the per-IP tier.

**This command carries no `X-Forwarded-For`,** unlike the "before" probes above.
It relies on the edge supplying the headers, which is the production path. It
therefore does NOT re-demonstrate that a constant forged header is ignored; that
half rests on section 3.

## What this does NOT establish

- **Only that ONE client is bounded.** The NAT case — several researchers behind
  one address — is now *live rather than theoretical*, because the ceiling binds
  at all for the first time. `DECISION-2026-08-22-per-ip-ceiling.md` declined to
  raise that ceiling while reasoning about a control that did not operate, and
  its own text says to re-read it once a fix lands. **That trigger has fired.**
- **The wall is 20, not the configured 10.** `WEB_CONCURRENCY = 2`
  (`gunicorn.conf.py:42`) and the limiter is per-process. Anyone sizing the NAT
  trade-off from `ANON_INTAKE_LIMIT = 10` alone will be off by 2x, and raising
  `WEB_CONCURRENCY` silently raises the wall.
- **The counters reset on every deploy and worker recycle**, because `_WINDOWS`
  is an in-process dict with no shared store. Plan **criterion 5** — "a deploy
  mid-window does not reset an attacker's quota" — remains **NOT met**. Phase 3
  is what fixes it.
- **Nothing alarms if `X-Real-Ip` stops arriving.** If the header disappears —
  an edge config change, a future Railway rewrite — `_client_ip()` falls through
  to the hop arithmetic, keys on the rotating hop again, and the tier goes
  silently inert exactly as before. Nothing detects that today.

## Two side findings

**Organic anonymous Scout traffic is zero.** The `/metrics` denominator read 122
at 06:20 and still read 122 at 12:20 — every metered anonymous Scout request
against that container was the probe's. Across two earlier container lifetimes
it read 3 and 28, also entirely ours. So the Phase 6 refusal-rate alarm, with a
50-sample floor and counters that reset every deploy, can rarely evaluate at all.

**The Phase 6 alarm works, proven by accident.** The probe's synthetic refusals
took the ratio to 26.2% (32/122) and the 06:31 and 12:20 scheduled runs both
failed correctly, naming `no_session`. First time it had fired on anything. It
also walked into the deadlock recorded in `ALERTING.md` under "A variable change
is not deploying": the failing runs redden main's suite, which blocks
same-commit Railway variable deploys.

## Reproducing this

Spends real anonymous budget. Two routes matter because neither allocates disk:
`POST /scout/upload` with no file is charged by the limiter and then 400s
(`scout/routes.py:768`), and `POST /scout/analyze` with a bogus `job_id` is
charged and then 404s. Use `/scout/example` sparingly — it allocates a job dir
per call against a GLOBAL `ANON_MAX_LIVE_JOBS` of 60, and filling that refuses
real visitors. The cookieless analyze probe fills the shared `anon:no-session`
bucket for up to 600 s, refusing real visitors whose cookies are blocked; that
was acceptable only because organic traffic measured zero.

Read the counters without handling the token by dispatching
`gh workflow run synthetic-smoke.yml --ref main` and reading the log — but a
probe that pushes the refusal share over 20% makes that run FAIL, with the
check-suite consequence above. `gh workflow run client-ip-probe.yml --ref main`
reads `/debug/client-ip` under six header shapes.

## Corrections

Four claims this document asserted and later retracted. Recorded because the
pattern matters: each lived in correction prose, none in a measurement.

1. **"A varying appended value is not on that list."** The edge OVERWRITES —
   `DECISION-2026-08-22`'s second case — it does not append. The wrinkle that
   table lacks is that it then adds its own rotating internal hop.
2. **"No `TRUSTED_PROXY_HOPS` setting rescues it… at `hops=2` it reads the
   client's own last entry, which is attacker-chosen."** Wrong: the edge
   overwrites, so the caller has no entry in the chain at all, and `hops=2`
   would in fact reach the client. `X-Real-Ip` was preferred for other reasons.
3. **"A single edge-written header has no index to shift."** Wrong: duplicate
   `X-Real-Ip` headers MERGE into one comma-joined WSGI value. The shipped code
   rejects any value that is not a bare IP (`ipaddress.ip_address`).
4. **"`hops=2` fails OPEN silently, whereas `X-Real-Ip` fails to a
   wrong-but-stable key."** Wrong in both halves. If `X-Real-Ip` stops arriving,
   `_client_ip()` falls through to the rotating hop and the tier goes silently
   inert — the same fail-open. And under that clause's own antecedent (Railway
   adds a second internal hop) `X-Real-Ip` still carries the real client, so the
   key would be correct, not wrong-but-stable. The honest reason to prefer
   `X-Real-Ip` is narrower: it survives a change in the NUMBER of internal hops,
   which `hops=2` does not. Both depend on the header they read continuing to
   arrive, and neither is alarmed on.

Also retracted, from an earlier draft of the fix's own PR: **"rollback is
`TRUSTED_PROXY_HOPS=0`."** At zero hops the key becomes `remote_addr`, which IS
the shared rotating edge address — that would bucket the whole anonymous
internet together. Rollback is a revert.
