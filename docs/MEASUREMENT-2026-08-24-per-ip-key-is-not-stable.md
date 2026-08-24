# Measurement: the anonymous per-IP limiter does not bound anything in production

Measured against production on 2026-08-24, app at `547d622`. This answers the
question [`DECISION-2026-08-22-per-ip-ceiling.md`](DECISION-2026-08-22-per-ip-ceiling.md)
named as the hard gate on Phase 2, and the answer is not one of the three cases
that decision anticipated.

## CORRECTION, same day, after the probe ran

**The headline below is confirmed. One conclusion in "What this changes" was
WRONG and is struck through there.** The probe landed hours after this document
merged and established the mechanism directly:

Railway's edge **OVERWRITES** the whole `X-Forwarded-*` family and rewrites the
header as `<real client>, <internal proxy>`. Forged `X-Forwarded-For`,
`X-Real-Ip`, `X-Forwarded-Host` and `X-Forwarded-Proto` were every one of them
discarded. The trailing internal hop **rotates** across a pool
(`152.233.30.101/.102/.104` in one run, `84.17.44.225/.226/.229` from a second
runner) and `remote_addr` tracks it. That is why one-hop resolution keyed on a
different value nearly every request.

So the forged-header bypass this document worried about **does not exist in
production** — the edge was discarding those headers all along. The tier is
inert for a different reason: the key it reads is edge-internal and rotating.

**And there IS a fix, which this document said there was not.** `X-Real-Ip`
carries the real client address, is set by the edge on every request, and was
measured unforgeable. Keying on it makes the per-IP tier work.

---

## Headline

`shared.metrics._client_ip()` resolves to a value that **varies from request to
request** in production. Since it is the key of the per-IP tier, every request
lands in a fresh bucket and the tier never refuses anyone. The tier
`scout/ratelimit.py:21` calls "the TRUE bound, because a cookie is free to
rotate" **bounds nothing at all as deployed**.

The per-session tier works correctly. It is the only anonymous limiter that
does. It is also the one an attacker skips by rotating a cookie, which is the
precise reason the per-IP tier exists.

## What was measured

Three facts, in the order they were established.

### 1. The fleet runs 2 worker processes

`hit()` (`scout/ratelimit.py:138`) is an in-process dict guarded by a thread
lock, with no shared store — Phase 3 was never built. So the fleet-wide wall for
any key is `limit × worker processes`, and worker count is measurable from
outside by finding a wall.

The cookieless session bucket is keyed on the **constant** string
`_NO_SESSION_KEY = "anon:no-session"` (`scout/ratelimit.py:180`) with
`ANON_ANALYZE_SESSION_LIMIT = 8`. Driving it cookieless:

- 48 × `POST /scout/analyze`, no cookie, bogus `job_id`
- → exactly **16 allowed**, then every subsequent request refused (`no_session`)

16 = 8 × 2. **`WEB_CONCURRENCY` is 2**, measured rather than read off a
dashboard. The refusals interleaved with allowances (10, 11, 12 refused; 13, 14,
15 allowed; 16 refused) before saturating — the signature of two independent
in-memory counters with requests distributed unevenly across them.

This is also the control for what follows: **a constant key does produce a wall,
on this fleet, in these minutes.**

### 2. A constant forged `X-Forwarded-For` produces no wall

`scout_intake` (`/upload`, `/fetch-pdb`, `/example`) passes no `session_limit`,
so `anon_rate_limit` skips the session tier entirely (`scout/ratelimit.py:863`)
and the bucket is keyed purely on `_client_ip()`, at `ANON_INTAKE_LIMIT = 10`.
With 2 workers a constant key must wall at 20.

| probe | header | result |
| --- | --- | --- |
| 26 × `GET /scout/example` | `X-Forwarded-For: 203.0.113.11` (constant) | 26 allowed, **0 refused** |
| 46 × `POST /scout/upload` | `X-Forwarded-For: 203.0.113.55` (constant) | 46 allowed, **0 refused** |

Both headers were constant within their run. Both runs completed inside one
600 s window. Neither produced a single refusal, at more than twice the wall.

The requests were genuinely metered, not skipped: the `/metrics` denominator
read **exactly 122**, matching the probe's own total of 28 + 46 + 48, with
`rate_limited` at 0.

### 3. Eviction is not the explanation

`_MAX_KEYS = 20_000` (`scout/ratelimit.py:128`). Nothing in this probe, and no
organic traffic (see below), comes near it.

## The conclusion, and its limit

**Established:** the key the per-IP tier uses is not stable across requests that
carry an identical header from one client. A constant key walls (16, measured);
the per-IP key does not wall (46, measured) on the same fleet minutes apart.

**Not established at the time of writing:** *what* varies, or why. **Now
established** — see the correction at the top. The guess recorded here was half
right: the edge does add its own rotating address, but it OVERWRITES rather than
appends, and that difference is what makes a fix possible.

**That step now exists.** `GET /debug/client-ip` reports `client_ip`,
`remote_addr`, `trusted_proxy_hops` and the forwarding headers as this process
actually received them, behind the same `METRICS_TOKEN` bearer as `/metrics`.
Run it with `gh workflow run client-ip-probe.yml --ref main`, which reads it
under six header shapes from a runner and prints the JSON, so nobody has to
handle the token. The first block sends ten identical requests; a `client_ip`
that differs across them is this document's headline confirmed directly rather
than inferred, which is what happened.

## What this changes

**The three-case table in `DECISION-2026-08-22-per-ip-ceiling.md` needs a fourth
row.** It anticipated append / overwrite / verbatim. The edge OVERWRITES — its
second row {D} but the table assumed overwriting leaves a one-entry header. It
does not: the edge adds its own rotating internal hop afterwards, so the
resolved value is neither the client nor anything stable.

~~no `TRUSTED_PROXY_HOPS` setting rescues it: at `hops=1` the app reads the
varying edge value, and at `hops=2` it reads the client's own last entry, which
is attacker-chosen. There is no hop count that yields a stable, unforgeable
key.~~ **WRONG, struck 2026-08-24.** That assumed the edge APPENDS to a
caller-supplied header. It overwrites, so the caller has no entry in the chain
at all and `hops=2` would in fact reach the client. The fix shipped keys on
`X-Real-Ip` instead — preferred over a hop index because an index is only safe
while the caller cannot lengthen the chain, and a single edge-written header has
no index to shift.

**Verification criterion 2 of the anonymous rate-limiting plan fails in
production.** "One IP rotating cookies stays bounded" is false as deployed: the
session tier is skipped by rotating the cookie, and the per-IP tier never fires.

**`DECISION-2026-08-22`'s conclusion survives; part of its reasoning does not.**
That decision said do not raise the ceiling, on three independent grounds. It is
still right, but the CPU-budget ground was reasoning about a control that does
not operate. The ceiling is not a ceiling. Do not cite it as one.

**Phase 2 is no longer an improvement to a working control.** It is the only
thing that would make a per-IP bound exist. If the mechanism above is confirmed,
the honest options are the ones that decision already named for its third case:
lean on the per-session tier plus sign-in, and stop describing a per-IP bound
that is not there.

## Two side findings

**Organic anonymous Scout traffic is zero.** The `/metrics` denominator read 122
at 06:20 and still read 122 at 12:20 — every metered anonymous Scout request
against this container was the probe's. Across two earlier container lifetimes it
read 3 and 28, also entirely ours. Consequences for Phase 6: the refusal-rate
alarm has a 50-sample floor and its counters reset on every deploy, so at this
traffic level and this deploy cadence it can rarely evaluate at all. That is a
real gap, though not the one this document is about.

**The alarm itself works, and this probe proved it end to end.** The synthetic
refusals pushed the ratio to 26.2% (32/122), and the 06:31 and 12:20 scheduled
runs both failed correctly, naming `no_session` as the reason. That is the first
time the Phase 6 alarm has fired on anything.

It also walked straight into the deadlock recorded in `ALERTING.md` under
"A variable change is not deploying" on the same day that was written: the
failing runs redden `main`'s check suite, which blocks same-commit Railway
service-variable deploys. Clearing it means resetting the counters, and the
refusals being synthetic is what makes that legitimate here — there is nothing to
fix. Merging any PR does it, since the deploy restarts the container.

## Reproducing this

Cheap and non-destructive, but it does spend real anonymous budget. Two routes
matter because neither allocates disk: `POST /scout/upload` with no file is
charged by the limiter and then 400s at `scout/routes.py:768`, and
`POST /scout/analyze` with a bogus `job_id` is charged and then 404s. Use
`/scout/example` only sparingly — it allocates a job dir per call against a
global `ANON_MAX_LIVE_JOBS` of 60, and filling that refuses real visitors.

Worker count — expect exactly 8 × W allowed, then saturation:

```bash
for i in $(seq 1 48); do curl -s -o /dev/null -w "%{http_code} " -X POST -H "Content-Type: application/json" -d '{"job_id":"00000000-0000-0000-0000-000000000000","chain":"A"}' https://tools.ranomics.com/scout/analyze; done
```

Per-IP wall — expect a refusal at 10 × W, observe none:

```bash
for i in $(seq 1 46); do curl -s -o /dev/null -w "%{http_code} " -X POST -H "X-Forwarded-For: 203.0.113.55" https://tools.ranomics.com/scout/upload; done
```

Read the counters afterwards without handling the token by dispatching
`gh workflow run synthetic-smoke.yml --ref main` and reading the run log — but
note that a probe which pushes the refusal share over 20% makes that run FAIL,
with the check-suite consequence described above.

The cookieless probe fills the shared `anon:no-session` bucket for up to 600 s,
which refuses real visitors who have cookies blocked. Measured organic traffic is
zero, which is what made that acceptable here; re-check before assuming it still
is.
