# Anonymous rate limiting: a per-IP ceiling that does not lock out a lab

**Status:** Phase 0 COMPLETE. Phase 4 SHIPPED. **Phase 1 is PARTIALLY**
**delivered and stays OPEN:** #171 landed its cost bound, but deliberately did
NOT flip `worker_class` to gthread, and §"Phase 1 premise" below says in terms
that *fairness remains UNDELIVERED* and that this is a reason to keep the phase
open. An earlier version of this header called Phase 1 complete, contradicting
the body of its own document. All of the above landed as PR #171, squashed to
`9eea7fb`; CI green and synthetic smoke green since.

**Next:** the plan's Wave D is Phases 5 and 6. Note the wave order has ALREADY
been departed from — Wave C says Phase 4 "needs 2 and 3 landed" and Phase 4
shipped with both still open. Phases 2 (peer-based XFF trust), 3 (shared counter
state) and 6 (observability) are open, and Phase 2 still rests on an unverified
premise: nobody has checked whether Railway's edge appends, overwrites or
forwards `X-Forwarded-For`.

**Evidence:** every `docs/qc/anon-*.md` here — the load baseline, Phase 0,
Phase 1 (2 rounds), Phase 4 (2 rounds), one QC of `a45252f`, and rounds 1-3
against `fix/anon-ratelimit-harden-fixes`.

**SHAs inside those reports are mostly PRE-SQUASH and no longer resolve** — but
not all of them: trunk pins such as `37a0f3a` and `2422bd1` are real commits on
main. Check before concluding a SHA is dead. The landed tree is `9eea7fb`.

**Written:** 2026-08-18, from QC evidence on PR #148 plus Railway production
config. **Status refreshed:** 2026-08-20, after #171 merged and after an
independent QC of this PR found the first version of this header wrong.

---

## Why this exists

Opening Epitope Scout to anonymous visitors (#148) makes the per-IP rate
limiter the only thing standing between the box and unbounded CPU. Two
measured facts frame the problem:

1. ~~**Each anonymous analysis pins 20-35 seconds of CPU**~~ **SUPERSEDED BY
   PHASE 0 MEASUREMENT.** Measured on the production pin (freesasa 2.2.1):
   **~2 CPU-s typical, 9.0 CPU-s worst case** at the 8,192 KB cap boundary.
   The 20-35 s figure is ~10-100x above typical but within ~2x of worst case.
   **Size against worst case: an attacker chooses their upload.** Still no
   account, no payment, no identity attached to the request.
2. **The limiter collides with the target user.** QC measured six researchers
   behind one university NAT: 18 intake attempts, **10 through, 8 refused**.
   Academic labs are exactly who the front-end redesign is for, and they are
   exactly who shares a public IP.

#148 does not cause this. The limit already existed; it was bypassable, so
nobody reached it. Making it effective makes NAT users strictly worse off than
before. That is the correct trade, and it is also the moment the cost becomes
real.

**The insight driving this plan:** the per-IP count is a *proxy* for the thing
actually being protected, which is CPU. It is a bad proxy — it cannot tell
fifty researchers at one institution from one attacker at that institution.
Bound the real resource, and the proxy can be loosened without losing
protection.

## Production ground truth (verified in the Railway dashboard, 2026-08-18)

| Fact | Value | Source |
|---|---|---|
| `WEB_CONCURRENCY` | **not set**, defaults to 2 | Railway service variables |
| Worker class | none set, so **sync workers** | `gunicorn.conf.py`, `nixpacks.toml`, `Procfile` |
| Effective per-IP limit | 10 per worker per 10 min, so **~20 fleet-wide** | derived; QC confirmed the wall at request 11 per worker |
| In-flight concurrency cap | 4 slots, **unreachable** (sync workers mean max 1) | QC, by config inspection |
| Limiter state | in-memory dict, **per worker**, lost on deploy | `scout/ratelimit.py` |
| `METRICS_ALLOWED_CIDR` | **not set**, so `/metrics` 403s for everyone | Railway service variables |

**[Superseded by Phase 6, 2026-08-22]** the last row is now history: the CIDR
gate is GONE and `METRICS_ALLOWED_CIDR` is read by nothing. Do not set it. It
could never have worked — `_ip_allowed()` resolved through `_client_ip()`, so
the allowlist inherited `X-Forwarded-For` forgeability with a two-guess space;
its unforgeable alternative `request.remote_addr` is Railway's shared edge PoP;
and the consumer that needed it is a GitHub-hosted runner with no stable
address. `/metrics` is now gated on a `METRICS_TOKEN` bearer, still deny by
default. See the `shared/metrics.py` docstring.

**[Note added at landing, 2026-08-20]** the worker-class row above cites
`nixpacks.toml`. That file is **INERT** — Railway builds this repo with Railpack
via mise, not Nixpacks, and editing it changes nothing. The row's CONCLUSION
(sync workers) is correct and independently confirmed by `gunicorn.conf.py` and
`Procfile`; only the third source is dead. Do not try to change the worker class
there — a dependency install was reviewed four times in this repo before anyone
noticed that same file does nothing.

Two consequences worth naming: the "configured" limit of 10 is not the limit
anyone experiences, and the number silently changes if `WEB_CONCURRENCY` is
ever set. Any fix that leaves state per-worker inherits both.

---

## Phase 0 outcome — these numbers supersede the estimates below

Measured 2026-08-18 at `37a0f3a`, then independently re-derived by a QC agent
that did not take the measurements. Where the two disagreed, QC's number is
recorded here; both documents are in `docs/qc/`.

| Number | Value | Confidence |
|---|---|---|
| CPU per analysis, typical | ~2 CPU-s | measured, both agents |
| CPU per analysis, worst case | **9.0 CPU-s** at the 8,192 KB cap | measured by QC, higher than builder's 5.12 |
| Metered cost of one analyse click | **2** bucket hits (`/analyze` + `/progress`) | measured against a real server |
| Metered cost of a page load | 0 | the `/scout/` index carries no decorator |
| Users per institutional egress IP | **95-284** | builder's ~300 did not survive QC |
| Defensible per-IP ceiling | **O(100)**, NOT ~1,000 | 1,000/IP = ~18,000 CPU-s demand vs ~1,200 available |
| Railway socket peer | Datacamp/CDN77 PoP (`x-railway-edge: jfk1`) | measured by controlled probe |
| Thread safety for `gthread` | **SAFE** | AST scan + runtime-mutation grep; the hash test alone was weak evidence |
| Production DSSP | not installed (`nixPkgs = ["gcc"]`) | so no subprocess CPU escapes `process_time()` |

Metered routes are exactly five: `/upload`, `/fetch-pdb`, `/example`,
`/analyze`, `/progress`.

**Caveat that must travel with the NAT number:** people-per-address is not
concurrent-users-per-IP-per-600s. 95-284 is an upper bound on population, not
a measurement of simultaneous demand. Do not treat it as the latter.

**Decided 2026-08-18:** the known-binder lookup is broken by a **stale URL**,
not dead upstream — SAbDab's old endpoint 301-redirects to an SPA, and a live
FastAPI backend returns 21,914 rows in one request. **Repoint it** rather than
delete it. This also removes the 40-thread fan-out, which is a Phase 1
prerequisite. Note `tests/test_scout_anonymous_access.py:133` monkeypatches
`fetch_known_binders` to `[]` — the only test on this path asserts the broken
value, which is why it rotted unnoticed. Fix the test with the URL.

---

## Phase 0 — Ground truth, no code change

Everything below depends on numbers nobody has measured. Do this first and do
not let a later phase guess.

- **Real cost per anonymous analysis.** Wall-clock and CPU-seconds for a
  typical intake plus analyse, on production-class hardware, not a dev box.
- **What a legitimate session actually does.** Requests per visitor per 10
  minutes for a first-time user who explores rather than beelines. QC
  estimated 1-3 intakes; confirm it.
- **Realistic NAT population.** How many distinct users behind one IP is
  plausible for the institutions in the funnel. This sets the ceiling.
- **The socket peer address on Railway.** One log line. This is the single
  fact Phase 2 needs, and it is currently unknown.

Deliverable: `docs/qc/anon-load-baseline.md` with the four numbers and how each
was obtained.

## Phase 1 premise — CORRECTED after two build rounds and two QC rounds

**The stated premise below is partly wrong. Read this first.**

- **The box is already bounded** at 2 sync workers x wall time, and load already
  queues in the kernel accept backlog. Phase 1 does not create a bound that was
  missing; its real and only content is **fairness** — stopping anonymous compute
  from starving `/healthz` and paying users.
- **A semaphore is inert under sync workers.** QC measured the real app under a
  single-threaded WSGI server with 6 concurrent callers: peak `_INFLIGHT` = **1**,
  all 6 serialised, **zero sheds, zero queue entries**. This is the same defect as
  the original 4-slot cap. Any slot/queue numbers are decoration until the worker
  class changes.
- **Option 2 (cross-worker semaphore) is NOT implementable on this stack.** There
  is no direct Postgres client anywhere — every query goes through Supabase
  PostgREST over HTTP, which cannot hold a session-level advisory lock. It also
  turns the wrong way: with 2 workers, fleet-wide anonymous concurrency is already
  <= 2, so any cross-worker cap >= 2 is a no-op and only 1 does anything, which
  halves capacity. **The plan is wrong to present it as a substitute for (1).**
- **gthread is the right mechanism, blocked on preconditions**, not on doubt:
  it permanently removes gunicorn's watchdog (verified in installed 24.1.1 —
  `gthread.py:289` calls `notify()` unconditionally at the top of the accept loop,
  before `can_accept`, so a request-wedged worker heartbeats forever and the
  arbiter never kills it), and it widens the unfixed `shared/idempotency.py` and
  `shared/wallet.py` races ~12x.

**Fairness remains UNDELIVERED.** That is a reason to keep this phase open, not
to close it.

### What Phase 4 must NOT do

**Phase 4 must not cite Phase 1 as its safety argument.** Phase 0's budget is CPU
*time*, not concurrency, so neither the semaphore nor its absence makes a loosened
per-IP ceiling safe.

Two measured numbers Phase 4 must size against instead:

- Adversarial cost is **~15-16 CPU-s per request**, not 9.0 (pipeline + binder
  lookup + interfaces + parse).
- **`/scout/progress` re-runs `run_pipeline` unconditionally** and shares the
  metered bucket, so worst adversarial spend is **~180 CPU-s per IP per window**,
  not "10 analyses". At the current ceiling **~7 addresses saturate the fleet** —
  and under sync workers a saturated box does not shed, it queues invisibly with
  `/healthz` behind it.

Measured, end to end: of six researchers behind one NAT, **the 6th is refused**
with no concurrency involved at all — one analysis costs 2 metered hits against a
limit of 10.

---

## Phase 1 — Make the cost bound real

**This is the load-bearing phase. Everything else is cheaper because of it.**

Today `_INFLIGHT` can never exceed 1, so the 4-slot cap is decoration and the
only real protection is refusing requests at the door. Give the app an actual
concurrency bound so load *queues* instead of pinning the box.

Options, ascending cost:

1. **Threaded workers** — `worker_class = "gthread"` plus a thread count in
   `gunicorn.conf.py`. Smallest diff; the existing in-process semaphore starts
   working as written. Audit the request path for anything that assumed
   one-request-per-process.
2. **A cross-worker semaphore** — Postgres advisory lock or a counter row.
   Correct regardless of worker model, survives a worker-count change.
3. **gevent** — deliberately not chosen. It was rejected once already, and it
   changes the execution model for every route, not just Scout.

Recommendation: (1) if the request path is thread-safe, (2) if it is not.
Phase 0's audit decides.

**Do not skip the queue-depth bound.** A queue with no ceiling is a slower way
to fall over. Cap it and shed with a clear message when full.

## Phase 2 — Trust the peer, not the header

Replace hop-counting with: **honour `X-Forwarded-For` only when the socket peer
is Railway's edge; otherwise use the peer address.**

Why this rather than `TRUSTED_PROXY_HOPS`: nobody has verified whether
Railway's edge appends, overwrites, or forwards the client header verbatim.
The hop-count fix in #148 is correct under the first two and **silently
bypassable under the third**. Peer-based trust is correct under all three, and
it closes the separate fail-open case where the origin is reached directly.

Keep `TRUSTED_PROXY_HOPS` as an override for a future multi-proxy setup, but it
stops being the primary mechanism. Fail **closed** when the peer is unknown.

## Phase 3 — Shared counter state

Move the window counter out of per-worker memory so the configured number is
the experienced number, and so a deploy does not hand everyone a fresh quota.

Supabase is already a dependency; a windowed counter table is very likely
enough and avoids adding Redis. Requirements: bounded rows, cheap eviction, and
no fail-open on error — a database blip must not mean unlimited access.

**Carry forward from #148's QC:** eviction must never drop a *limited* key
first. The obvious "evict oldest" policy hands an attacker exactly the reset
they are spraying for; the current in-memory version evicts lowest-hit-count
first for that reason. Whatever replaces it inherits that requirement.

## Phase 4 — Two tiers

- **Per-session (anon cookie):** tight. Catches ordinary over-use cheaply, and
  is the only limit a normal user will ever meet.
- **Per-IP:** generous, and still the true bound. Cookies are free to rotate,
  so this cannot be relaxed on the assumption that the session cap holds.

Phase 1 is what makes "generous" safe. Without it, this phase is just a bigger
hole. Set the ceiling from Phase 0's NAT number, not from intuition.

## Phase 5 inputs — MEASURED, and one earlier claim retracted

- **The first wall a real NAT lab meets is INTAKE, not analyze.** Measured: six
  researchers now pass (ten, in fact), and the 11th is refused by
  `ANON_INTAKE_LIMIT` on `/scout/example`, not by the analyze bucket. **A funnel
  instrumented only on the analyze refusal sits behind the wall labs actually
  hit.** Instrument both.
- The 10/10 balance between `ANON_INTAKE_LIMIT` and `ANON_ANALYZE_LIMIT` is
  **accidental** — neither was changed by Phase 4; what moved was the cost of an
  analysis (2 hits -> 1), which lifted the effective analyze ceiling 5 -> 10 and
  landed on intake's 10 by coincidence. Nothing ties them and nothing asserts it.
  **[Superseded at landing, 2026-08-20]** this bullet used to say the comment at
  `scout/routes.py:115-116` still claimed "the analyze bucket is where the wall
  is". #171 rewrote that comment in the same PR — it now states outright that
  intake is the first wall, and calls the 10/10 balance accidental. Nothing to
  do here; the code says it already.
- **RETRACTED:** an earlier round advised that the per-IP "this network" refusal
  must not become a signup prompt because signing in could not help. That is
  **false**. `scout/ratelimit.py` short-circuits the entire decorator on
  `session.get("user_email")`, so signing in bypasses **both** tiers. "Sign in to
  keep going" is honest at both walls. The one caller it cannot help is a
  cookies-blocked visitor, who gets a distinct message naming cookies.

---

## Phase 5 — Turn the wall into a funnel

Today the limit returns a bare 429. For a top-of-funnel free tool, that is a
bounced visitor.

The wall should read as **"you have used the free allowance — sign in to keep
going"**, with the signup link carrying enough state that the user resumes
rather than restarts. This serves the CPU budget and the conversion goal at
once, and it is the only phase here a bench biologist will ever notice.

Consider escalating friction rather than a cliff: anonymous for the first N,
then email, then account.

## Phase 6 — Observability

**BUILT 2026-08-22 on `feat/anon-phase6-observability`.** All three bullets are
addressed; what each turned into is recorded under the bullet.

- **Separate the two 429s.** QC found the per-IP limiter and the per-session
  live-job cap both return 429 with different bodies; any measurement taken
  from status codes alone conflates them. Distinct metrics, distinct messages.
  → The messages shipped with Phase 5 (seven reason codes). The metric is
  `tools_hub_scout_refusals_total{reason,route}`, incremented at six sites
  covering all seven reasons. The status codes were never the right key: one
  event leaves the app as 429, 503 **and HTTP 200 `text/event-stream`**, so the
  SSE refusals were being counted as *successes*.
- **Alert on refusal rate**, not just error rate. A limiter refusing 40% of
  real users is an outage that does not look like one.
  → This was a MISSING CONSUMER, not a missing threshold: nothing scraped
  `/metrics` at all. `scripts/check_refusal_rate.py` now runs as a step in the
  existing 6h `synthetic-smoke` workflow, which already runs outside Railway
  and already emails the owner on a non-zero exit. Note the honest limitation
  written into that script: counters reset on deploy, so the ratio is "since
  container boot", not a windowed rate, and a minimum denominator stops it
  firing on a handful of post-deploy samples.
- `/metrics` is currently 403 for everyone because `METRICS_ALLOWED_CIDR` is
  unset. Either set it or stop treating the endpoint as reachable.
  → Neither. The CIDR gate was REMOVED (see the superseded ground-truth row
  above for the three reasons it could not work) and replaced with a
  `METRICS_TOKEN` bearer. **Leo must set `METRICS_TOKEN` as a Railway service
  variable and add the same value as a `METRICS_TOKEN` repository secret.**

  **CI WILL NOT REMIND YOU.** The step keys off the REPO SECRET alone, and
  Railway's variable only matters once the step actually runs. Measured, all
  four cells:

  | repo secret | Railway var | job |
  |---|---|---|
  | unset | unset | GREEN — step skips with a warning |
  | unset | **set** | GREEN — step skips; Railway's value is never read |
  | **set** | unset | **RED** — step runs, `/metrics` 403s, exit 1 |
  | **set** | **set** | GREEN — the alarm is live |

  So the silent state is "repo secret unset", whatever Railway holds — an
  earlier version of this bullet said "neither set", which is wrong in the
  second row. The only loud misconfiguration is repo-secret-without-Railway.
  The skip is deliberate — an
  unset new secret must not turn the 6-hourly job red forever, because that
  failure email is indistinguishable from a real Scout outage and is how
  monitors get muted. The cost of the trade is that the new alarm is silently
  inactive until someone does the manual step, and GitHub does not email on
  warnings. **This bullet said the opposite until 2026-08-22** — it promised a
  red build that will never arrive, which is exactly the reason to defer the
  manual step. Round 1 of QC cleared that sentence correctly and the fix for
  its own M1 finding then falsified it.

---

## Execution model

Same as the front-end redesign: **one worker agent per phase, then a separate
QC agent that did not build it.** No phase advances on the builder's own
say-so. That rule has paid for itself repeatedly here, twice on this exact
feature — the builder's own verification passed a limiter a rotating header
walked straight through, and a later mutation table reported "all red" while
missing an unpinned clamp.

**Waves:**

- **Wave A (sequential, one worker):** Phase 0, then Phase 1. The worker-model
  choice changes what every later phase can assume.
- **Wave B (parallel, disjoint files):** Phase 2 (`shared/metrics.py`) and
  Phase 3 (`scout/ratelimit.py`).
- **Wave C (one worker):** Phase 4, which needs 2 and 3 landed.
- **Wave D (one worker):** Phases 5 and 6.

Rebase on `origin/main` at the start of each wave, and re-check
`git log origin/main -1` immediately before opening the PR. Trunk has moved
under a task in this repo seven separate times, twice inside a single task.

### QC agent contract

1. **Pin the SHA reviewed.** A report without one is not a QC round.
2. **Measure the test baseline first-hand** on the merge base and the branch.
   `-m pytest -q` from the repo root, no path argument, repo venv. Never quote
   a recorded count — there is no `pytest.ini`, so a path argument silently
   changes collection scope.
3. **Re-run the verification criteria empirically.** Not "the diff would do
   this" — drive real requests through a real server.
4. **Mutation-test the new tests with its own mutations**, and verify each
   mutation actually landed before drawing any conclusion from a green run.
   Two mutations have silently failed to apply on this repo: a missed sed
   pattern, and a Windows encoding mismatch on an em-dash.
5. **Answer the goal question in writing:** can six researchers behind one NAT
   all use the tool in the same afternoon, and is one attacker still bounded?
   Name the first place either breaks.
6. Write `docs/qc/anon-ratelimit-phase-<N>.md` with a verdict and evidence.

## Verification criteria

1. **NAT simulation.** N distinct sessions from one IP complete a realistic
   exploratory workload without refusal, at the Phase 0 number.
2. **Attacker simulation.** One IP rotating cookies, and one rotating
   `X-Forwarded-For`, both stay bounded. Report sent vs admitted.
3. **Direct origin.** With no proxy in front, forged headers are ignored.
4. **Concurrency.** Under load above the cap, requests queue and complete or
   shed cleanly; CPU stays bounded; the box stays responsive.
5. **State durability.** A deploy mid-window does not reset an attacker's
   quota. A database error does not fail open.
6. **Eviction.** From a limited key, spraying enough distinct keys to force
   eviction leaves the limited key limited.
7. **UX.** A refused legitimate user sees a route forward, not a dead end, and
   resumes without re-entering their work.
8. **Suite.** Full run, measured both sides.

## Out of scope

- CAPTCHAs and proof-of-work. Reconsider only if Phases 1-4 prove insufficient.
- Rate limiting authenticated routes — they have a wallet and an identity.
- Redis, unless Phase 3 shows Supabase genuinely cannot carry it.
- The four inline `X-Forwarded-For` copies in `blueprints/admin.py`,
  `blueprints/auth.py`, and `blueprints/public.py`. QC traced all four to audit
  logging with no enforcement path. They do record a forgeable IP, which is a
  forensics-quality issue worth its own small PR, not part of this one.
