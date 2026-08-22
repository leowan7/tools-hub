# QC — anonymous rate limiting, Phase 6 (observability)

**Reviewed:** `feat/anon-phase6-observability` @ **`b33dc901e4fedcbe6516a0c8efb0ef03def33575`**
**Merge base:** **`d3c60c8`** (`d3c60c845c19e5a2dc5465ac2ea63b14e1aaefb8`, = `origin/main` at review time)
**Date:** 2026-08-22
**Reviewer:** independent QC agent — did not build this.

## Verdict

**PASS WITH FINDINGS.**

Every claim the builder made about the *code* reproduced under my own
measurements. The three deliverables work end-to-end on a real socket: real
refusals move the new counter, `/metrics` is gated on the bearer, and the
consumer turns an 83% refusal share into a non-zero exit. The limiter is
unchanged — I re-measured it on both sides of the merge base and the numbers
are identical.

What holds it back from a clean PASS is one operational regression in the
workflow (M1) and one mutation survivor that lets the branch's own headline
invariant be broken silently (M2). Neither is a defect in the shipped
behaviour today; both are cheap to close.

---

## Test baselines (measured first-hand, both sides)

Repo venv by absolute path, `-m pytest -q` from the worktree root, **no path
argument**, in worktrees under the session scratchpad (never `.claude/worktrees/`,
never the main tree). Exit codes are pytest's own, captured with `$?`
immediately after the run — not a pipeline's.

| Side | SHA | Result | pytest exit |
| --- | --- | --- | --- |
| Merge base | `d3c60c8` | **5796 passed, 21 skipped** in 292.23s | **0** |
| Branch | `b33dc90` | **5832 passed, 21 skipped** in 287.99s | **0** |

**Delta vs `d3c60c8`: +36 passed, 0 failed, skips unchanged (21 → 21).**

This matches the builder's claim exactly. No flakes; no re-runs needed.

---

## What I measured, and how

Four throwaway worktrees under the session scratchpad, all created by me:
`qc6` (branch, suite + report), `qc6base` (base, suite), `qc6probe` /
`qc6probebase` (probes, so probe `tmp/` churn could not collide with a running
suite), `qc6mut` (mutations only). The main tree at
`C:/Users/lab/Documents/Claude_projects/tools-hub` was never touched.

### A. Regression — does the limiter still bound anyone?

`shared/metrics.py` was heavily edited and `scout/ratelimit.py:106` imports
`_client_ip` from it. I ran the same probe file against **both** SHAs: 50
`GET /scout/example` from one socket peer, a distinct minted
`ANON_SESSION_KEY` per client (a bare test client carries none and they all
share the `anon:no-session` bucket), job dirs cleared between runs so
`ANON_MAX_LIVE_JOBS` (60) could not confound.

| `X-Forwarded-For` | sent | admitted @ `d3c60c8` | admitted @ `b33dc90` | refusals @ `b33dc90` |
| --- | --- | --- | --- | --- |
| rotating `203.0.113.{i}` | 50 | 50 | **50** | none |
| fixed `203.0.113.9` | 50 | 10 | **10** | 40 × `rate_limited` |
| absent | 50 | 10 | **10** | 40 × `rate_limited` |

**Zero drift.** Distinct per-IP buckets created: 50 / 1 / 1 on both sides.
`_client_ip` and `_trusted_proxy_hops` are present and their bodies are
untouched by the diff (only context lines appear).

On the same branch run the new counter moved in lockstep with the refusals:
`SCOUT_REFUSALS{reason="rate_limited",route="scout.example"}` +40 in each of
the two bounded rows, +0 in the rotating row. Limiter and metric agree because
they are reading the same traffic.

The rotating-XFF row is *pre-existing and unchanged*, and it is a harness
artifact, not a live hole: with `TRUSTED_PROXY_HOPS=1` and no proxy in front,
a single-value header IS the whole chain, so the rightmost trusted hop is the
forged value. In production Railway's edge sits in front, so the chain is
`forged, realclient` and the rightmost hop is the real client. Nothing on this
box can prove that; it is asserted in the `_client_ip` docstring. Out of scope
for Phase 6 either way — identical at base.

### B. The headline — site 5, the SSE `busy` shed

Driven with a **real** job (created by `GET /scout/example` and owned by the
same session) against a **really** saturated pool (`_INFLIGHT = 9999`,
`ANON_MAX_QUEUED_RUNS = 0` so nothing waits) — no monkeypatched
`anon_compute_slot`, no faked `_resolve_job_dir`.

```
http=200  mime=text/event-stream
body='data: {"stage": "error", "msg": "Epitope Scout is busy with other free
       runs right now. ...", "reason": "busy"}'
SCOUT_REFUSALS{busy,scout.progress}   delta = 1.0
REQUESTS_TOTAL{scout.progress,2xx}    delta = 1.0     <- on the SAME request
```

**Confirmed independently.** The refusal answers HTTP 200 and the status-code
counter files it as a success while the new counter files it as a refusal.
That contradiction is exactly why a refusal rate cannot be derived from status
codes on this app, and the branch's central claim is true.

**The unread-body case.** The Flask test client buffers, so I drove the raw
WSGI callable and never touched the returned iterable:

```
BEFORE any iteration:  busy delta=0.0   2xx delta=1.0
AFTER  iteration:      busy delta=1.0   2xx delta=1.0
ABANDONED (closed, never iterated): busy delta=0.0   2xx delta=1.0
```

The builder's admission is accurate and now measured. See finding **L3** for
my verdict on it.

### C. Site 6 and the `route` label

I proved the builder's premise rather than taking it. A minimal Flask app with
three call sites:

| where `observe_scout_refusal` is called | resulting `route` label |
| --- | --- |
| in a view | the endpoint name (`plain`) |
| in a generator **not** wrapped in `stream_with_context` | **`unknown`** |
| in a generator wrapped in `stream_with_context` | the endpoint name |
| outside any request context | `unknown`, no raise |
| request context with no matched endpoint (`endpoint is None`) | `unknown`, no raise |

So hoisting site 6 out of `_error_stream` (which is *not* wrapped) was
necessary — left inside, it would have logged under `route="unknown"`. And
site 5 is legitimately safe inside `_slotted()` because that one **is** wrapped;
my live run in §B labelled it `scout.progress`, so this is measured, not
inferred.

`has_request_context()` guard: present at `shared/metrics.py:419`, and the
whole body sits under a bare `except Exception` that logs at debug. Neither
raises into a refusal path — proven by the branch's own
`test_a_broken_counter_does_not_break_a_refusal`, which I confirmed goes RED
when the guard is removed only in the sense that nothing breaks (see L2).

**No current site can land under `route="unknown"`.** All six are in view
bodies or inside `stream_with_context`. All six increment sites confirmed:

| # | file:line | reason(s) | route observed |
| --- | --- | --- | --- |
| 1 | `scout/ratelimit.py:753` (`_refuse`) | `rate_limited`, `session_rate_limited`, `no_session` | `scout.example`, `scout.analyze` |
| 2 | `scout/routes.py:325` | `at_capacity` (fleet, 503) | `scout.example` |
| 3 | `scout/routes.py:337` | `at_capacity` (per-session, 429) | `scout.example` |
| 4 | `scout/routes.py:954` | `busy` (JSON, 503) | `scout.analyze` |
| 5 | `scout/routes.py:1400` | `busy` (SSE, **200**) | `scout.progress` |
| 6 | `scout/routes.py:1302` | `bad_request`, `job_expired` | `scout.progress` |

Seven reason codes, six sites. Confirmed by grep across the whole tree: the
only callers of `observe_scout_refusal` outside `shared/metrics.py` are these
six.

### D. Cardinality

Bounded. `reason` reaches the label only from module constants
(`scout/ratelimit.py:712-742`); `_refuse` is module-private with exactly two
call sites (`ratelimit.py:875`, `:892`), both passing constants. `route` is
`request.endpoint`, the same expression `_observe_request` uses at
`shared/metrics.py:358`, so numerator and denominator share one label
vocabulary. Adversarial probe: a request with a traversal `job_id` and a
quote/`,`-injected `chain` produced **no new label pair**. Ceiling is 7 reasons
× the five metered endpoints.

### E. The token gate, attacked as an auth control

Every case below was driven through a real request. **No case produced a 500
or an exception.**

| case | result |
| --- | --- |
| no `METRICS_TOKEN`, no header | 403 |
| no `METRICS_TOKEN`, correct-looking bearer | 403 |
| `METRICS_TOKEN=""`, any bearer | 403 |
| `METRICS_TOKEN=""` + `Authorization: Bearer ` (empty bearer) | **403** — empty does not match empty |
| `METRICS_TOKEN="   "` + `Bearer    ` | **403** |
| correct token | **200** (≈4 KB exposition) |
| wrong token | 403 |
| no `Bearer` prefix / `Basic …` / empty header / bare `Bearer` | 403 |
| `bearer …` / `BEARER …` (case) | 403 |
| non-ASCII bearer vs ASCII env (latin-1, CJK) | **403, not 500** |
| non-ASCII env **and** matching non-ASCII bearer | 200 |
| 100 000-char bearer | 403 |
| control characters `\x00\x01\x02` | 403 |
| forged `X-Forwarded-For` (`10.0.0.1`, `127.0.0.1`, `172.16.0.1`, 2-hop chain), no bearer | 403 in every case |
| correct bearer **+** forged `X-Forwarded-For` | 200 (header plays no part either way) |
| env rotated between requests (`first` → `second`) | old bearer 403, new bearer 200 — read per request |
| `METRICS_ALLOWED_CIDR=0.0.0.0/0` set, no token | **403** — the old variable is inert |

Control: `hmac.compare_digest("café", "café")` raises
`TypeError: comparing strings with non-ASCII characters is not supported` on
this interpreter. The bytes encoding at `shared/metrics.py:245-248` sidesteps
it, and mutation **20** proves a test pins that.

`_allowlist_cidrs` / `_ip_allowed` / `METRICS_ALLOWED_CIDR`: **gone from all
code**. A whole-tree grep (excluding `.git/`) finds them only in historical
`docs/qc/*.md` records and in the handoff, where the ground-truth row is
explicitly marked superseded. One stale forward-looking mention remains — see
L7.

**Case-sensitive `Bearer` — my judgement: defensible, keep it.** RFC 7235 makes
the scheme case-insensitive, so a third-party scraper could trip on it. But
every deviation fails *closed* with a clean 403 that shows up in the scraper's
own error state, the sole consumer sends `Bearer ` literally, and a
case-insensitive match buys nothing here. This is the safe direction of a
harmless deviation.

### F. The consumer, `scripts/check_refusal_rate.py`

AST-parsed the imports: `__future__`, `math`, `os`, `re`, `sys`,
`urllib.error`, `urllib.request`. **Third-party: none.** The workflow installs
nothing, so this holds.

Hand-computed example — requests 100; refusals `rate_limited` 15,
`session_rate_limited` 5, `busy` 2, `at_capacity` 1, `bad_request` 30,
`job_expired` 40. Expected numerator 15+5+2+1 = **23**, share 23.0%.
Measured: `refusal share: 23.0% (23/100) threshold 20.0%`, exit **1**. The
numerator excludes the 70 info-reason samples exactly as claimed.

| check | measured |
| --- | --- |
| threshold exactly AT (0.23 vs 23.0%) | exit 0 — comparison is strict `>` |
| threshold just UNDER (0.2299) | exit 1 |
| threshold just OVER (0.2301) | exit 0 |
| min-samples floor, 49 metered requests at 100% refusal | exit 0, `SKIP` |
| 50 metered requests at 100% refusal | exit 1, `FAIL` — floor boundary is `<` |
| 5 / 6 requests at 100% refusal | exit 0, `SKIP` — a tiny sample cannot fire |
| a reason code that does not exist yet, 25/100 | **exit 1** and it is printed |
| zero denominator, with and without refusals | exit 0, `SKIP`, no `ZeroDivisionError` |
| `NaN` / `+Inf` sample values | dropped by `parse_exposition` |
| a non-Scout route (`billing.checkout` 9999) in the exposition | excluded from the denominator |

**The "future reason is included by default" claim is what the code does, not
just what the comment says** — `refusals.items() if r not in INFO_REASONS` is a
blacklist, and mutation **18** (swap it for a `POLICY_REASONS` whitelist) goes
RED.

End-to-end against a real local HTTP server:

| `/metrics` answers | exit | first line |
| --- | --- | --- |
| 200 with samples, 5% share | 0 | report, `OK` |
| **403** | **1** | `ERROR: … answered HTTP 403. 403 means METRICS_TOKEN does not match …` |
| 500 | 1 | same shape |
| **200 with a zero-byte body** | **1** | `answered 200 with no parseable samples (0 bytes)` |
| 200 with an HTML error page | 1 | `no parseable samples (24 bytes)` |
| connection refused | 1 | `ERROR: could not reach …` |
| `METRICS_TOKEN` unset on the runner | 2 | `METRICS_TOKEN is not set` |

The server saw `Authorization: Bearer my-secret-value` — the bearer really is
sent. Env overrides work (`REFUSAL_RATE_THRESHOLD=0.01` → exit 1;
`REFUSAL_RATE_MIN_SAMPLES=500` → `SKIP`); a garbage threshold warns and falls
back to the default rather than crashing.

**403 fails the job rather than skipping — I agree with the choice.** A monitor
that goes silent when its own credential breaks is worse than a noisy one, the
credential is static so it cannot flap, and the job already fails wholesale
when Railway is unreachable, so this adds no new class of false alarm. The
zero-byte-200 guard is a good call given that failure has shipped here before
(`PROMETHEUS_MULTIPROC_DIR` after `--preload`).

### G. Full Phase 6 chain, on a real socket

`werkzeug.serving.make_server` on `127.0.0.1`, real HTTP, real cookie jars, one
socket peer, no monkeypatching anywhere:

```
60 real GET /scout/example from one peer  ->  {200: 10, 429: 50}
/metrics with the WRONG bearer            ->  403
/metrics with the RIGHT bearer            ->  200 (5646 bytes)
parsed 46 samples; metered requests=60; refusals={'rate_limited': 50.0}
check_refusal_rate.py                     ->  exit 1
    refusal share: 83.3% (50/60)  threshold 20.0%
    FAIL: Epitope Scout is refusing more anonymous traffic than the threshold allows.
```

Refusal → counter → token-gated `/metrics` → consumer → non-zero exit → the
job fails → the email goes out. The whole phase, proven.

### H. The workflow

`.github/workflows/synthetic-smoke.yml` parses under `yaml.safe_load`.
`METRICS_TOKEN: ${{ secrets.METRICS_TOKEN }}` is wired into job-level `env:`
(line 86) alongside the existing `RK_LIVE_KEY`. Trigger unchanged
(`schedule: 0 */6 * * *` + `workflow_dispatch`). The guard step fails cleanly
with `::error::` and `exit 1`. No step echoes the token, and `secrets.*` values
are masked by GitHub anyway. Step ordering is the problem — see **M1**.

---

## Findings

### M1 — MEDIUM. The new guard disables the existing Platform API monitor

`.github/workflows/synthetic-smoke.yml:103-107` (`Guard - METRICS_TOKEN is
set`) runs **before** `Run Platform API smoke` at line 113.

`METRICS_TOKEN` does not exist yet — the branch's own handoff edit says *"Leo
must set `METRICS_TOKEN` as a Railway service variable and add the same value
as a `METRICS_TOKEN` repository secret"*, i.e. it is a pending manual action.
Until it is done, **every 6-hourly run exits at line 106 and
`scripts/smoke_platform_api.py` never executes.** That script is ALERTING.md's
Tier 2 — the only end-to-end production monitor there is (`ALERTING.md:16`,
`:220`). A change whose purpose is to add a monitor silently removes one for
the whole gap between merge and Leo setting the secret.

It also defeats the branch's own stated ordering. The comment at lines 116-118
says the refusal check *"Runs after the API smoke so an outright outage is
reported as an outage rather than as an unreachable `/metrics`"* — correct
intent, undone by placing its precondition three steps earlier.

Evidence: parsed step list in order — Checkout, setup-python, Guard RK_LIVE_KEY,
**Guard METRICS_TOKEN**, Run Platform API smoke, Check Epitope Scout refusal
rate.

**Fix:** move the `Guard - METRICS_TOKEN is set` block so it sits immediately
above `Check Epitope Scout refusal rate`. One block moved, no other change.

### M2 — MEDIUM (test gap). The "must stay inside the generator" invariant is prose only

`scout/routes.py:1400`. `tests/test_scout_refusal_metrics.py:230-235` states in
its docstring that hoisting site 5 out of the generator *"would count a refusal
that never happened"*. Nothing tests it.

Mutation **10** moves the increment to view level, above `def _slotted():`.
`tests/test_scout_refusal_metrics.py` → **11 passed, exit 0.**

I then proved the mutation is genuinely wrong rather than an equivalent
refactor. Same probe, slots **free**, caller **not** shed:

| build | shed? | `SCOUT_REFUSALS{busy,scout.progress}` delta |
| --- | --- | --- |
| `b33dc90` as shipped | no | **0.0** (correct) |
| with mutation 10 applied | no | **1.0** — a refusal that never happened |

If that hoist ever ships, every `/scout/progress` request is counted as a
`busy` refusal, the 6-hourly check fires on a perfectly healthy service, and
the first thing an operator learns is to ignore the alert. The alarm's
credibility is the asset Phase 6 is buying.

**Fix:** one test — a `/scout/progress` request that is **not** shed must leave
`busy` unchanged. Every existing test either forces the shed or returns before
`_slotted` is reached, so no current test can see the difference.

### L1 — LOW (test gap). The constant-time comparison is not pinned

`shared/metrics.py:245-248`. Mutation **12** replaces the
`hmac.compare_digest(bytes, bytes)` call with `presented == expected`:
`tests/test_metrics.py` → **28 passed, exit 0.**

`/metrics` is now an authentication control, so the timing property is part of
its contract, and nothing holds it. Practical risk is low (a remote timing
attack on a 64-hex token over the public internet is not feasible), but the
gap is free to close. Note the *related* hazard IS pinned: mutation **20**
(`compare_digest` on `str`, which would 500 on a non-ASCII token) goes RED.

### L2 — LOW. The `has_request_context()` guard is unexercised and its stated reason is stale

`shared/metrics.py:419` and the docstring at `:409-417`. Mutation **22**
removes the guard: `test_scout_refusal_metrics.py` + `test_metrics.py` → **39
passed, exit 0.**

The docstring justifies the guard with *"one of the six sites lives inside a
streamed response's generator, and a generator body runs after Flask has popped
the request context unless it was wrapped in `stream_with_context`"*. After the
site-6 hoist that is no longer true of any site: site 5 **is** wrapped, and
site 6 is now in the view. The guard is correct to keep — it is the net for a
seventh site added carelessly — but the comment describes a hazard the code no
longer has, and nothing tests it.

### L3 — LOW. The site-5 count under-reports, and in the wrong direction

`scout/routes.py:1400`. Measured on the raw WSGI callable (§B): when the client
never iterates the body, `REQUESTS_TOTAL{scout.progress,2xx}` gains 1 and
`SCOUT_REFUSALS{busy}` gains 0.

**I agree it is unavoidable here** — the slot is only taken on iteration, so
the shed does not exist as a decision until the frame runs, and hoisting it
produces the *worse* error of counting shed events that never happened (M2).
What I disagree with is that it is fully accounted for: the bias has a
direction, and it is the unsafe one. The denominator grows while the numerator
does not, so the refusal share reads **lower** than reality — an outage
detector that quietly under-states the outage. It also gives a cheap way to
dilute the signal (open `/scout/progress` connections and never read them),
though the per-IP tier bounds that to 10 per 10 minutes.

`scripts/check_refusal_rate.py` is the right place to say so — it already
documents the counters-reset-on-deploy caveat honestly, and an operator staring
at a suspiciously low ratio will read that docstring, not `scout/routes.py`.

### L4 — LOW. Numerator and denominator have different populations

`scripts/check_refusal_rate.py:137-139`. The denominator is
`tools_hub_requests_total{route ∈ METERED_ROUTES}` — **every** request to those
five endpoints, signed-in included. The numerator counts anonymous refusals
only. The module docstring calls the result *"the anonymous refusal share of
metered Scout traffic"*.

Signed-in Scout traffic therefore dilutes the ratio and pushes the check toward
under-firing. Harmless today (Scout is overwhelmingly anonymous) and it errs
quiet rather than noisy, but the mismatch grows with the paid base and is
invisible when it does. Worth a sentence in the docstring at minimum;
`status_class` is not the discriminator, so there is no free fix in the current
label set.

### L5 — LOW. Uncounted anonymous refusals — three, not two

My own search of every non-2xx return reachable by an anonymous caller in
`scout/`, plus a repo-wide sweep for 413/429/503:

| path | file:line | tier-dependent? |
| --- | --- | --- |
| `_reject_oversized` 413 (upload) | `scout/routes.py:349-372` | **yes** — `ANON_MAX_UPLOAD_BYTES` vs `MAX_UPLOAD_BYTES` |
| `fetch_pdb` over-cap 413 | `scout/routes.py:807-814` | **yes** — same split |
| app-wide `MAX_CONTENT_LENGTH` 413 | `app.py:831-854` | no — 20 MB for everyone |

The builder named the first two and not the third.

**My verdict: leave all three uncounted — but not for the builder's reason.**
"They refuse a payload, not a caller" is wrong for the first two: the cap is
chosen by `_signed_in_owner_key()`, so those two *do* refuse the caller's tier.
The defensible reason is different. All three are deterministic on input size,
self-explanatory in the message, and completely independent of load and of what
other users are doing — so they can never present as *"an outage that does not
look like one"*, which is the sole condition this alert exists to detect. A
researcher who hits them learns why immediately and has a route forward. They
would only add noise to the ratio.

`requires_scout_quota` (`scout/quota.py:279-320`) passes anonymous callers
straight through and is not an anon refusal path. `serve_pdb` / `download`
404s are missing-resource, not refusal.

### L6 — LOW. `job_expired` is counted on one route and not its twin

`scout/routes.py:944` and `:947` — `POST /scout/analyze` answers 404 *"Job not
found or expired. Please re-upload your file."* for exactly the condition
`/scout/progress` counts as `job_expired` at site 6.

Zero effect on the alert (`job_expired` is in `INFO_REASONS` and is never
alerted on), so this is an accounting nit rather than a defect. It does mean
"six sites covering all seven reason codes" is complete *for the alert* but not
a complete census of the reasons.

### L7 — LOW (docs). The new alert has no runbook and no env-var row

- `ALERTING.md:462-472` — the environment-variable table lists `RK_LIVE_KEY`
  for the synthetic monitor but gains no `METRICS_TOKEN` row, even though the
  same workflow now hard-fails without it.
- `ALERTING.md:504+` — the *"Synthetic smoke FAILED"* runbook covers the API
  smoke only. The job can now fail for a second, completely different reason
  and the operator receiving the email has nothing to follow.
- `docs/qc/anon-load-baseline.md:543` still recommends *"read CPU from a
  `/metrics` scrape once `METRICS_ALLOWED_CIDR` is set"* — a variable that is
  now read by nothing. It should point at `METRICS_TOKEN`.

(The handoff's Phase 6 bullet at `docs/HANDOFF-2026-08-18-anon-rate-limiting.md:321`
also still reads *"currently 403 … because `METRICS_ALLOWED_CIDR` is unset"* —
that one is fine: it is the plan's original text with the `→ Neither.`
annotation directly beneath, which is how the rest of that document records
superseded decisions.)

### Informational (no action needed)

- **Redirects carry the bearer.** `urllib.request` copies the `Authorization`
  header across a 30x (CPython's `HTTPRedirectHandler.redirect_request` strips
  only content headers), so a redirect on `METRICS_URL` would hand the token to
  the redirect target. The default is a fixed https origin we own and the
  override is operator-set, so this is theoretical.
- **Keep `METRICS_TOKEN` ASCII.** A non-ASCII token matches in the test client
  but would *not* match in production — header values arrive latin-1-decoded
  while the env value is real UTF-8, so the two byte strings differ. It fails
  closed, and the workflow already recommends `openssl rand -hex 32`.
- **`except (TypeError, ValueError, UnicodeError)` at `shared/metrics.py:249`
  is near-dead** (mutation 21 survives). It is not *quite* dead —
  `str.encode(…, "surrogateescape")` can still raise on a lone high surrogate,
  which `os.environ` on Windows can produce. Correct to keep.

---

## Mutation table

Own mutations, not the builder's. Every row was applied by exact-string
replacement with an occurrence-count assertion (`count == 1`, else refuse),
then **`git diff --unified=0` was printed before the test ran** — the landed
line is reproduced below for each — then reverted and `git status --porcelain`
asserted empty before the next row. Any row whose pattern did not match exactly
once was recorded as *did not apply* and never scored.

**This trap fired.** Rows 9 and 10 were written in a first harness whose
`"\\n\\n"` literals were collapsed to real newlines by the heredoc that wrote
the file; the patterns then matched zero times. The harness refused to score
them rather than reporting a green run. They were rewritten with `chr(92)`
concatenation and re-run in a second pass, where row 9 goes RED and row 10 —
the interesting one — survives.

Targeted suites: **M** = `tests/test_scout_refusal_metrics.py`,
**T** = `tests/test_metrics.py`, **K** = `tests/test_check_refusal_rate.py`.

| # | mutation | file | landed diff | tests | result |
| --- | --- | --- | --- | --- | --- |
| 1 | delete the `_refuse` increment | `scout/ratelimit.py:753` | `-    observe_scout_refusal(reason)` | M | **RED** 3 failed, 8 passed |
| 2 | `_refuse` counts the wrong reason | `scout/ratelimit.py:753` | `-    observe_scout_refusal(reason)` / `+    observe_scout_refusal("busy")` | M | **RED** 3 failed, 8 passed |
| 3 | delete the fleet live-job increment | `scout/routes.py:325` | `-        observe_scout_refusal(REASON_AT_CAPACITY)` | M | **RED** 1 failed, 10 passed |
| 4 | delete the per-session live-job increment | `scout/routes.py:337` | `-        observe_scout_refusal(REASON_AT_CAPACITY)` | M | **RED** 1 failed, 10 passed |
| 5 | per-session live-job counts `busy` | `scout/routes.py:337` | `-…REASON_AT_CAPACITY)` / `+…REASON_BUSY)` | M | **RED** 1 failed, 10 passed |
| 6 | delete the JSON `busy` increment | `scout/routes.py:954` | `-            observe_scout_refusal(REASON_BUSY)` | M | **RED** 1 failed, 10 passed |
| 7 | delete the SSE error-stream increment | `scout/routes.py:1302` | `-        observe_scout_refusal(reason)` | M | **RED** 2 failed, 9 passed |
| 8 | SSE error-stream pinned to `bad_request` | `scout/routes.py:1302` | `-…(reason)` / `+…(REASON_BAD_REQUEST)` | M | **RED** 1 failed, 10 passed |
| 9 | delete the SSE `busy` increment (site 5) | `scout/routes.py:1400` | `-                observe_scout_refusal(REASON_BUSY)` | M | **RED** 1 failed, 10 passed |
| **10** | **hoist site 5 out of the generator** | `scout/routes.py:1400` + `:1374` | `-                observe_scout_refusal(REASON_BUSY)` / `+    observe_scout_refusal(REASON_BUSY)` | M | **GREEN — SURVIVED** 11 passed → **M2** |
| 11 | `route` label becomes `request.path` | `shared/metrics.py:419` | `-        route = (request.endpoint or "unknown") if has_request_context() else "unknown"` / `+        route = request.path if …` | M+T | **RED** 9 failed, 30 passed |
| **12** | **`compare_digest` → `==`** | `shared/metrics.py:245-248` | `-        return hmac.compare_digest(…)` / `+        return presented == expected` | T | **GREEN — SURVIVED** 28 passed → **L1** |
| 13 | token gate always true | `shared/metrics.py:229` | `+    return True` | T | **RED** 6 failed, 22 passed |
| 14 | empty `METRICS_TOKEN` stops denying | `shared/metrics.py:230-231` | `-    if not expected:` / `-        return False` | T | **RED** 1 failed, 27 passed |
| 15 | drop the `Bearer ` prefix check | `shared/metrics.py:236-237` | `-    if not header.startswith("Bearer "):` / `-        return False` / `+    pass` | T | **RED** 1 failed, 27 passed |
| 16 | remove the min-samples floor | `scripts/check_refusal_rate.py:171` | `-    if requests < min_samples:` / `+    if False:` | K | **RED** 1 failed, 20 passed |
| 17 | threshold `>` → `>=` | `scripts/check_refusal_rate.py:184` | `-    if share > threshold:` / `+    if share >= threshold:` | K | **RED** 1 failed, 20 passed |
| 18 | numerator whitelist instead of blacklist | `scripts/check_refusal_rate.py:169` | `-    refused = sum(… if r not in INFO_REASONS)` / `+    refused = sum(… if r in POLICY_REASONS)` | K | **RED** 1 failed, 20 passed |
| 19 | denominator takes any `scout.*` route | `scripts/check_refusal_rate.py:138` | `-            if labels.get("route") in METERED_ROUTES:` / `+            if str(labels.get("route", "")).startswith("scout."):` | K | **RED** 2 failed, 19 passed |
| 20 | `compare_digest` on `str` (the 500 hazard) | `shared/metrics.py:245-248` | `-        return hmac.compare_digest(…encode…)` / `+        return hmac.compare_digest(presented, expected)` | T | **RED** 1 failed, 27 passed |
| **21** | **narrow the `except` to `NotImplementedError`** | `shared/metrics.py:249` | `-    except (TypeError, ValueError, UnicodeError):` / `+    except NotImplementedError:` | T | **GREEN — SURVIVED** 28 passed → informational |
| **22** | **drop the `has_request_context()` guard** | `shared/metrics.py:419` | `-        route = (…) if has_request_context() else "unknown"` / `+        route = request.endpoint or "unknown"` | M+T | **GREEN — SURVIVED** 39 passed → **L2** |

**22 mutations landed and were scored, 18 RED, 4 GREEN.** Two more (the first
attempts at 9 and 10) were caught not applying and were re-run rather than
scored. The tree was verified clean (`git status --porcelain` empty) before and
after every single row; no row was scored from an unverified diff.

---

## Rule 5 — the goal question, measured

> **Can six researchers behind one NAT all use the tool in the same afternoon,
> and is one attacker still bounded? Name the first place either breaks.**

Phase 6 is observability and should not have moved this, so I ran the same
probe file against **both** `d3c60c8` and `b33dc90`. Six independent sessions,
one socket peer, `GET /scout/example` interleaved round-robin the way six
people at one institution actually behave.

| workload | intakes | admitted | refused | first refusal |
| --- | --- | --- | --- | --- |
| 6 people × 1 structure | 6 | **6** | none | — |
| 6 people × 2 structures | 12 | **10** | 2 × `rate_limited` (429) | intake #11 — researcher 5, structure 2 |
| 6 people × 3 structures | 18 | **10** | **8** × `rate_limited` (429) | intake #11 — researcher 5, structure 2 |

**Identical on both SHAs.** No Phase 6 drift.

**Can six researchers use it in the same afternoon?** Only if they look at one
structure each. Six people opening two structures each inside ten minutes is
already 12 intakes and two are refused. Three each is 18 intakes, 10 admitted,
8 refused — **44.4%**, which reproduces Phase 0's measured university afternoon
(*"18 intake attempts and 8 were refused — 44%"*) to the request.

**Where it breaks first:** `ANON_INTAKE_LIMIT = 10` at `scout/routes.py:131`,
a per-IP ceiling over `ANON_RATE_WINDOW_SECONDS = 600`, enforced at
`scout/ratelimit.py:892`. All three intake routes (`/scout/upload`,
`/scout/fetch-pdb`, `/scout/example`) share the one `scout_intake` bucket, so
the six of them are spending from a single pot of ten every ten minutes. The
eleventh request from that address in the window is refused no matter which
person makes it or which route they use. This is **not** a Phase 6 regression —
it is the standing state of the plan at Phase 5, unchanged by this branch.

**Is one attacker still bounded?** Yes, on the axis that matters. 200 intakes
from one socket peer with a fresh cookie on every single request: **10
admitted, 190 refused**. Rotating cookies buys nothing, because the per-IP tier
fires regardless of cookie state.

**Where the attacker's bound breaks first:** the correctness of
`TRUSTED_PROXY_HOPS` against the real edge topology, at
`shared/metrics.py:253` (`_trusted_proxy_hops`) feeding `_client_ip` at
`shared/metrics.py:266`. `_client_ip` counts hops from the right, so the key
is whichever entry our own outermost trusted proxy wrote. With one trusted hop
that is right whether Railway appends to `X-Forwarded-For` or overwrites it —
but it is right *because* a proxy is in front. On a direct origin with the
default `TRUSTED_PROXY_HOPS=1`, a single-value forged header **is** the whole
chain and the caller picks its own bucket: 50 sent, 50 admitted, 50 distinct
buckets, measured on both SHAs. Nothing on this box can prove production is
fronted the way the docstring says; that assumption is where the bound rests.
Put an untrusted hop in front of the edge, or move to a topology with a
different hop count without moving the variable, and the per-IP tier becomes
caller-chosen. `TRUSTED_PROXY_HOPS` is the knob and it is documented, but it is
a configuration invariant with no test behind it.

**What Phase 6 changes about all this:** nothing in the limiter, and everything
in the visibility. Driven end-to-end on a real socket (§G), that same NAT
afternoon scaled to 60 intakes now produces `refusal share: 83.3% (50/60)` and
exit 1 from `scripts/check_refusal_rate.py` — the alarm that Phase 0's 44%
afternoon would have rung, had anything been listening. That is the phase doing
its job.

---

## Anything the builder claimed that I could not reproduce

Nothing. All three deliverables, the six increment sites, the seven reason
codes, the token gate's full behaviour table, the consumer's arithmetic, and
the `5796 → 5832` test delta all reproduced under independent measurement.

Two claims are true but incompletely stated:

1. *"Hoisting it would count refusals that never happened."* True — I proved
   it — but nothing in the suite enforces it (**M2**).
2. *"`observe_scout_refusal` guards on `has_request_context()` … one of the six
   sites lives inside a streamed response's generator [that runs after the
   context is popped]."* The guard exists and works, but after the site-6 hoist
   no site actually needs it, so the stated reason no longer matches the code
   (**L2**).

One argument I reject while agreeing with its conclusion: the two 413 size
rejections were left uncounted on the grounds that they *"refuse a payload, not
a caller"*. They are tier-dependent and so do refuse the caller — the right
reason to exclude them is that they are input-determined and load-independent,
and so can never look like an outage (**L5**). There is also a third such path
the builder's search did not find, `app.py:831-854`.

---

## Recommendation

Merge after **M1** (move one YAML block) and **M2** (one test). Both are small
and neither touches shipped behaviour. The L-findings are all fit for follow-up
and none of them should hold the branch.
