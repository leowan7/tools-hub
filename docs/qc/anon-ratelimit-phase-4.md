# QC — Anonymous rate limiting, Phase 4

**Verdict: FAIL.**

One defect, in the one mechanism this commit exists to add. The follow-up
credit **can be diverted** — the property the commit names in its own bullet
list (*"it cannot be minted for free, replayed, banked, raced, stolen or
diverted"*) and asserts in a test called
`test_a_credit_cannot_be_diverted_to_a_different_job`. The test passes and the
property does not hold. `POST /scout/analyze?job_id=A` with a body naming job
B binds the credit to A and runs the pipeline on B, because `_analysis_id()`
prefers the query string and `analyze()` reads the body. Measured on a real
server: **one charge buys two full pipeline runs** (§1.2). This is the same
hole the commit message says was "found exploitable during self-review" and
closed by putting the job id in the key; the job id is in the key, but the
decorator and the route do not agree on what it is.

Everything else in the commit verified clean, and two of my findings are
**corrections in the builder's favour**:

- **The adversarial regression the builder honestly reports does not exist.**
  I measured worst adversarial spend on both commits. Before Phase 4 it was
  **already ~300 CPU-s per IP per window**, not ~180 — the builder compared
  the attacker's *second-best* play before (spam `/progress`, 180) against
  their *best* play after (300). The attacker's best play before was already
  standalone `/analyze` on fresh jobs, worth 300. Once the divert is fixed
  Phase 4 is **CPU-neutral for the worst case**, and addresses-to-saturate
  stays at ~4 rather than going 7 → 4 (§2).
- **Therefore do not take the offered `ANON_ANALYZE_LIMIT = 6` lever.** It
  costs real users 40% of their allowance to correct a regression that is not
  there, and it also makes the per-IP tier strictly tighter than the new
  session tier, so a lone visitor's first refusal reverts to the misleading
  *"this network"* message that `REASON_SESSION_LIMITED` was added to
  eliminate. Measured at 6: first refusal at request 7, reason
  `rate_limited` (§2.3).

The goal-question proof holds under exhaustive check (§3). The suite numbers
are exactly as claimed (§7). My own 17 mutations gave 15 RED and **2 GREEN**,
and both survivors are inside `_analysis_id` — the function that contains the
defect (§6).

**SHAs reviewed**

| | |
|---|---|
| Subject | `555448ce4258484fc0547d73165d62273788cc4d` (`fix/anon-ratelimit-phase-4`) |
| Base / merge parent | `6e0a68cb538c7929397afdacd7c4636bf7f10c06` (round 3) |
| Also in the branch, not on trunk | `85a4fb641a60842c9c98abd6fc25536595c30b76` (SAbDab repoint, PR #155) |
| Merge base with `origin/main` | `fa938b09fd43aae6fc06976756f5c6fe379a537f` |
| `origin/main` at review time | `48b4b71eedd2f791142ee4d020ee977a6961a6be` — **unchanged during this task** |
| Files touched | 4, +1065 / −51 |

**Worktree isolation.** All work in
`C:\Users\lab\Documents\Claude_projects\tools-hub\.claude\worktrees\agent-ac78b739f175af424`
(confirmed with `git worktree list`; 26 worktrees live). The main tree was
read only. Nothing pushed, no branch created, no PR.

**Method.** Every claim below marked *measured* was driven through a real
Werkzeug HTTP server over real sockets with real cookie jars, single-threaded
except where a race is under test (production runs sync workers). `freesasa`
is **not installed in the repo venv**, so `run_pipeline` is replaced by a
counting stub that writes the `results.csv` a real run writes — control flow
and accounting are identical, and *pipeline invocations* are the load
multiplier. CPU-seconds are converted from Phase 0's figures (~9 CPU-s per
pipeline run at the 8 MB cap, ~6 CPU-s for the finalise extras); I did not
re-measure those and say so wherever they are used.

---

## 1. The follow-up credit, attacked

### 1.1 The defect

`scout/ratelimit.py:261`

```python
def _analysis_id() -> str:
    job_id = request.args.get("job_id", "")      # QUERY STRING WINS
    if not job_id:
        body = request.get_json(silent=True)
        job_id = body.get("job_id", "") if isinstance(body, dict) else ""
    return job_id.strip() if isinstance(job_id, str) else ""
```

`scout/routes.py:706-707`, inside `analyze()`:

```python
data = request.get_json(silent=True) or {}
job_id = data.get("job_id", "").strip()          # BODY ONLY
```

`/scout/analyze` is a POST whose job id lives in the JSON body. Nothing stops
a caller adding a query string to it. When they do, the decorator keys the
credit on the query value and the route works on the body value. The credit's
job binding — *"a credit bought by `/progress?job_id=A` can only ever be spent
by `/analyze` on job A"* — is simply not true of `/analyze`.

The front end never sends a query string on `/scout/analyze`
(`templates/scout/index.html:392-395` posts JSON only, while `:332-333` puts
`job_id` in the query for `/scout/progress`), so this divergence exists purely
as an attacker affordance. No legitimate traffic depends on it.

### 1.2 Exploited, measured

```
E2  ATTACK: divert the credit with a query-string job_id
  charges 1 -> 1  (2 = closed, 1 = DIVERTED)
  analyze status 200
  pipeline runs 1 -> 2
  pipeline ran on the OTHER job: True

E2b control - same divert, body-only job_id (the path the builder's test uses)
  charges 1 -> 2  (binding holds here)
```

Sequence: `GET /scout/progress?job_id=A&chain=A` → charged, runs the full
pipeline on A, grants a credit keyed `(session, ip, "A")`. Then
`POST /scout/analyze?job_id=A` with body `{"job_id": "B", "chain": "A"}` →
`_analysis_id()` returns `"A"`, the credit is found and popped, the request is
**not charged at all**, and `analyze()` resolves job B, finds no `results.csv`
and runs `run_pipeline` itself (`scout/routes.py:728`).

**One charge, two full pipeline runs plus one finalise pass.** In Phase 0
terms that is ~9 + ~9 + ~6 = **~24 CPU-s for one charge**, against the ~15 the
commit's arithmetic assumes — the exact number the commit says it fixed by
adding the job id to the key.

Sustained over a whole window, measured (§2.1, strategy S4): **210 CPU-s per
worker, ~420 fleet-wide**, versus 150 / 300 without the divert.

### 1.3 The fix, and the trap in the obvious version

The minimal repair is to make the decorator derive the id the same way the
route does. **Do not simply swap the order** — reading the body first re-opens
the mirror image, because a `GET` may legally carry a body and `/scout/progress`
reads `request.args` (`scout/routes.py:1001`). Something method-aware is
needed, e.g.

```python
job_id = request.args.get("job_id", "") if request.method == "GET" else ""
if not job_id:
    body = request.get_json(silent=True)
    job_id = body.get("job_id", "") if isinstance(body, dict) else ""
```

My mutation **M2** is exactly the naive body-first swap, and it survived GREEN
(§6) — no test distinguishes the two orders, so a fix in that shape would land
un-pinned. Whatever ships needs a test that moves **only** the query string
while holding the body constant, and one that does the reverse on `/progress`.

### 1.4 Everything else I threw at the credit held

All measured on a real server unless noted.

| Attack | Result |
|---|---|
| Replay: N `/analyze` after one `/progress` | first free, rest charged — pop is single-use |
| **Race**: 2, 4 and 8 concurrent `/analyze` against one credit | charges +1, +3, +7 — **exactly one free ride at every width** (threaded server) |
| Steal: a second cookie jar spends a neighbour's credit, same IP, same job | charged (+1) |
| Steal: same session, different `X-Forwarded-For` | charged (+1) |
| Bank: 10 `/progress` then 10 `/analyze` | one credit outstanding, not ten |
| Expired credit (TTL forced negative) | refused and reclaimed in the same pop |
| Refused `/progress` grants a credit | no — grant is after both tiers allow |
| Replaying a job id after expiry | `/progress` only ever grants; every replay is charged |
| Forging another session's job id | `_resolve_job_dir` → `_owner_keys()` is session-scoped; 404 |
| Reaching `run_pipeline` with no charge at all | only via the §1.1 divert |

The race deserves a note because the commit's claim is that the pop happens
"under the same lock the counters use". It does (`scout/ratelimit.py:297-305`),
and it holds at 8-way concurrency, not just 2.

### 1.5 A second, smaller regression: refusals are no longer free

`_spend_followup(_followup_key())` runs **before** either tier
(`scout/ratelimit.py:619`), and `_analysis_id()` calls
`request.get_json(silent=True)`. So a `/scout/analyze` that is about to be
refused now parses its JSON body first. `MAX_CONTENT_LENGTH` is 20 MB
(`app.py:656`).

Measured, same box, same 18 MB body, request already over the per-IP ceiling:

| | refused 18 MB body | refused small body |
|---|---|---|
| base `6e0a68c` | **0.056 s** | 0.082 s |
| branch `555448c` | **0.45 s** | 0.009 s |

Refused requests are unbounded — the limiter keeps counting but never stops
answering — so this is ~8x cheaper for an attacker to convert into worker wall
time than it was, using requests that are *all being refused*. At two sync
workers, ~4.4 req/s of this saturates the box on refusals alone.

Not a blocker on its own, and the parse is unavoidable while the credit key
needs a body-carried job id. A cheap gate would be to skip the parse unless
some credit exists for the `(session, ip)` prefix.

---

## 2. The adversarial regression, adjudicated

### 2.1 Measured, both commits, every strategy an attacker has

Same harness, same box, one address, one worker. Pipeline runs and finalise
passes counted directly; CPU-s converted at Phase 0's ~9 and ~6.

| Strategy | base `6e0a68c` | branch `555448c` |
|---|---|---|
| S1 spam `/progress` on one job | 10 pipeline → **90/worker, 180 fleet** | 8 pipeline → 72 / 144 |
| **S2 standalone `/analyze` on fresh jobs** | 10 pipeline + 10 finalise → **150/worker, 300 fleet** | 10 + 10 → **150 / 300** |
| S3 honest pair, repeated | 5 + 5 → 75 / 150 | 8 + 8 → 120 / 240 |
| S4 **divert** (branch only) | — | 18 pipeline + 8 finalise → **210 / 420** |

### 2.2 The builder's arithmetic, checked

The *before* number is wrong, and wrong in the direction that makes this
commit look worse than it is.

- **`before ~180`** — reproduced exactly (S1). But 180 is what an attacker
  gets by aiming all 20 fleet-wide hits at `/progress`. It is not their best
  play. `/scout/analyze` on a job with no `results.csv` runs the whole
  pipeline *and* the finalise extras — ~15 CPU-s for one hit — and 20 fresh
  jobs cost exactly the 20 fleet-wide intake hits available. **S2 was already
  worth ~300 CPU-s per IP per window on base.** Phase 0 §2.2 and round 2 both
  documented that `/analyze` re-runs the pipeline when the stream drops;
  nobody costed it as the optimum.
- **`after ~300`** — correct, and reproduced (S2 on the branch, identical).
- **`~7 addresses` → `~4 addresses`** — the *after* half is right
  (1,200 / 300 ≈ 4). The *before* half is not: it was already ≈ 4.
- **`5 analyses per worker` → `10`** — both reproduced first-hand (§3.2, and
  the base run: researcher 6 refused on their first analysis at 12 hits).
- **`ANON_ANALYZE_LIMIT = 6` restores ~180 exactly** — arithmetically true
  (12 fleet-wide hits × 15). But see §2.3.

**So the honest headline is: Phase 4 does not raise worst adversarial spend at
all.** It is ~300 CPU-s per IP per window before and after. What it changes is
that the same 300 is now reached by an *ordinary* pattern rather than only by
an attacker who knows to skip `/progress` — which is a real thing to know, but
it is not the regression the commit apologises for. The only genuine increase
is the §1 divert, and that is a bug.

### 2.3 The `ANON_ANALYZE_LIMIT = 6` lever should not be pulled

Two reasons, the second of which the commit does not name.

1. It corrects a regression that is not there (§2.2).
2. **It makes the new session tier unreachable for the case it was built
   for.** Both tiers are charged on the same request, session first, each +1
   (`scout/ratelimit.py:625-643`), so for a single session the two counters
   move in lockstep and the *lower limit* refuses first. At per-IP 6 against
   per-session 8, a lone visitor meets the per-IP wall. Measured, real
   decorator, one session:

   ```
   ANON_ANALYZE_LIMIT=10, session_limit=8 -> first refusal at request 9, reason=session_rate_limited
   ANON_ANALYZE_LIMIT= 8, session_limit=8 -> first refusal at request 9, reason=session_rate_limited
   ANON_ANALYZE_LIMIT= 6, session_limit=8 -> first refusal at request 7, reason=rate_limited
   ```

   At 6, the first thing a lone over-user is told is *"Too many requests from
   this network"* — the misleading message the two-tier split and
   `REASON_SESSION_LIMITED` exist to eliminate, and the one Phase 5 cannot
   honestly answer with "sign in". The commit calls the gap between the tiers
   "thin"; at 6 it inverts.

   If the ceiling ever does drop, `ANON_ANALYZE_SESSION_LIMIT` must drop below
   it in the same commit. Nothing currently asserts the ordering; a one-line
   `assert ANON_ANALYZE_SESSION_LIMIT < ANON_ANALYZE_LIMIT` at import would
   make it impossible to get wrong.

### 2.4 Is a per-IP count defending anything, and is the builder's point 3 right?

**Yes, the builder is right, and the plan needs to hear it.** The argument
survives arithmetic:

- Phase 0 puts institutional NAT populations at **95-284 users**, and a
  defensible per-IP ceiling at **O(100)**.
- At the *adversarial* cost of ~15 CPU-s per analysis, O(100) analyses from
  one address is ~1,500 CPU-s — already more than the whole fleet's ~1,200
  CPU-s per window. **A ceiling generous enough for one real institution
  already saturates the box from one address.** There is no number that
  satisfies both constraints.
- At the *typical* ~2 CPU-s, O(100) is ~200 CPU-s and comfortably inside
  budget. The entire gap between "serves a lab" and "saturates the box" is the
  4.5x spread between typical and worst case — and Phase 0 attributes that
  spread to upload size, which the attacker chooses, capped at
  `ANON_MAX_UPLOAD_BYTES = 8 MB` (`scout/routes.py:89`).

So **`ANON_MAX_UPLOAD_BYTES` is the untaken lever, and it is the one that
makes a generous per-IP ceiling possible at all.** Phase 4 cannot deliver
safety on its own, and neither can any per-IP count.

Two qualifications the plan should carry with that:

- The size cap is not free. An 8 MB structure is a large complex; halving the
  cap excludes legitimate targets. It is a product decision, not a tuning knob.
- The count is not useless. It is the **only** bound on *repetition* — nothing
  stops an attacker re-running `/scout/progress` on one already-uploaded
  structure, and only the count stops that. Safety is
  `count × cost-per-run`; Phases 1-4 have only ever touched the first factor.

---

## 3. The goal-question proof

### 3.1 Verified exhaustively, not just on the happy path

The claim: for R researchers doing one analysis each, worst per-worker load is
R, not 2R, because a pair only misses its credit by splitting across workers,
which also splits the load.

I simulated the limiter directly with two independent `_WINDOWS` / `_FOLLOWUP`
states (what two gunicorn sync workers have) over **every** assignment of each
researcher's two requests to workers — all 4,096 arrangements at R = 6,
including the adversarial ones:

```
R = 6, arrangements tried = 4096
WORST per-worker charge count = 6   (builder claims R = 6)
VERDICT: PASS - never exceeds R
```

The algebra behind it is sound and worth writing down, because it is what
makes the bound safe under *any* future worker count: on worker *w*, the
analyses charged are its own `/progress` calls (P_w) plus the `/analyze` calls
that landed there without a credit, which can only be analyses whose
`/progress` went elsewhere — at most `R − P_w`. So `P_w + A_w ≤ R`, for any
number of workers.

I also checked the "thorough user" shape the session cap is sized for —
3 researchers × 2 analyses each — and worst per worker is 6, i.e. one charge
per analysis, as claimed.

### 3.2 Measured end to end, real server, one worker

```
   researcher  1: example=200 progress=200 analyze=200 ip_charges=1
   ...
   researcher 10: example=200 progress=200 analyze=200 ip_charges=10
   researcher 11: example=429 ...                       <-- REFUSED (intake cap)
```

**Ten researchers through on one worker**, versus five on base — where I
reproduced round 2's result exactly, including the detail that matters:

```
   base 6e0a68c: researcher 6: example=200 progress=200 analyze=429  <-- REFUSED
   ANALYSES COMPLETED per worker = 5
```

The 11th is stopped by `ANON_INTAKE_LIMIT`, not the analyze bucket. That is a
new fact worth recording: **after Phase 4 the intake ceiling (10) binds before
the analyze ceiling (10)** for one-analysis-each traffic, because an analysis
now costs 1 analyze hit and 1 intake hit rather than 2 and 1. Phase 0 called
the intake bucket "comfortable"; after this commit the two are exactly
balanced, and any future rise in `ANON_ANALYZE_LIMIT` without a matching rise
in `ANON_INTAKE_LIMIT` buys nothing for real users.

---

## 4. Two things checked independently

### 4.1 Where "6 analyses" actually comes from

**It is not a conflation of "6 researchers", but the attribution in the code
comment is wrong twice over.**

`scout/routes.py:174` says:

> QC measured a thorough first-time visitor at 6 analyses in a session

Traced to source:

- The number is real and traceable: `docs/qc/anon-load-baseline.md` §2.3,
  table row **"Thorough: 2 uploads, 6 analyses | 3 | 12"**, followed by
  *"Design input for Phase 4: budget the analyze bucket at 2 hits per run, and
  size for ~6 runs per user session, not 1-3."* So 8 = 6 + a third is a
  faithful reading of Phase 0's own recommendation. It is a different number
  from the six researchers, which lives in a different document and a
  different table.
- **But `anon-load-baseline.md` is the *builder's* Phase 0 measurement doc**
  ("Phase 0 ground truth… Measured: 2026-08-18"), not the QC doc. The Phase 0
  QC report calls it *"the builder's own §2.3"*
  (`docs/qc/anon-ratelimit-phase-0.md:581`) and adopts it as a concern without
  re-deriving it.
- **And §2.3 is a behavioural projection, not a measurement.** §2.1 describes
  what was measured — *cost per user action*, on a real server — and §2.2
  reports it: one analyse click = 2 metered hits. How many actions a visitor
  takes was never observed; §2.3's three rows (Beeline / Explorer / Thorough)
  are modelled. The doc promises that "anything not measured is labelled **NOT
  MEASURED**", and its §7 register does not list this, so the projection is
  the one number in that document that slipped the labelling rule.

**Required correction:** the comment must not say "QC measured". Something
like *"Phase 0's baseline projects a thorough first-time visitor at ~6
analyses (`docs/qc/anon-load-baseline.md` §2.3) — modelled from the flow, not
observed"* is accurate. The cap of 8 itself does **not** need re-deriving;
it is a faithful application of Phase 0's stated design input. What needs
fixing is a provenance claim that would let a later phase treat a model as
data.

### 4.2 The cookie-less shared bucket

**The lockout is real and I measured it; the consequence the builder claims is
also real, and it makes the lockout close to harmless.**

Measured, real server, sprayer sending no cookie at all:

```
E8  sprayer got 4/12 session refusals
    no-session bucket charges = 12
    cookie-less victim locked out of /progress: True
    (their intake still succeeded: 200)
```

So yes — one cookie-less sprayer exhausts `_NO_SESSION_KEY`
(`scout/ratelimit.py:177`) in 8 requests and every subsequent cookie-less
caller is refused with `session_rate_limited`. But the builder's defence holds
on inspection and on execution: `_resolve_job_dir` (`scout/routes.py:205`)
resolves through `_owner_keys()` (`:193`), which for an anonymous caller is
just the session's `scout_anon_id`. **With no session cookie there is no owner
key, so every analysis 404s regardless of the limiter.** A cookies-blocked
visitor was already unable to use Scout; the shared bucket denies them nothing
they had.

Three residual consequences worth carrying forward rather than dismissing:

1. **The refusal message becomes a lie.** They now see *"You have used the
   free Epitope Scout allowance for this session. Sign in for a free
   account…"* instead of *"Job not found or expired."* Their actual problem is
   blocked cookies, and **signing in will not fix it either** — the login
   session is a cookie too. Phase 5 is about to turn that exact string into a
   signup funnel, so it will be pointing the one population it cannot help at
   a door that does not open for them.
2. **Phase 6's `session_rate_limited` counter is pollutable** by any
   cookie-less sprayer, for free, without touching the per-IP bucket. The
   commit positions that reason as the signal for "one caller over their own
   allowance — the conversion moment". It is not clean enough to alert on
   without excluding `_NO_SESSION_KEY`.
3. Mildly in the design's favour: because the session tier returns **without**
   charging the per-IP bucket, a cookie-less sprayer is refused at 8 and never
   spends institutional budget. That is the fail-closed behaviour the commit
   intends, and it works.

---

## 5. Eviction and Phase 3

**Both eviction policies verified by execution; neither is convertible.**

Counter table (`_WINDOWS`), with the per-IP key genuinely over its ceiling and
`_MAX_KEYS` set to 1,000 so it stays large relative to `_EVICT_BATCH = 200`,
as in production:

```
limited key before spray : (…, 14)
limited key after  spray : (…, 14)     [8,000 distinct spray keys]
still refused over real HTTP : True
```

The lowest-hit-count-first rule works exactly as its comment claims: spray
keys carry 1 hit each and are the cheapest to forget, so the limited caller
survives. *(A first attempt with `_MAX_KEYS = 50` wiped the whole table and
looked like a failure — with `overflow = len − MAX + 200` and only 50 entries,
the slice takes everything. That is an artifact of an unrealistic cap, not a
defect; at production ratios it cannot occur. Recorded so nobody re-derives
the false positive.)*

Credit ledger (`_FOLLOWUP`), soonest-to-expire-first: 6,000 grants under a cap
of 1,000 leaves 1,000 entries, and `_spend_followup` on a never-granted key
returns `False`. **Eviction cannot manufacture a credit** — the only thing a
dropped entry does is charge somebody who would have ridden free.

**On Phase 3 the builder's reasoning is correct and correctly documented.** A
credit granted on worker 1 is invisible to worker 2, so a split pair is
charged twice: today's behaviour, fail-closed, safe to ship before Phase 3 and
not after. The warning is at the top of `scout/ratelimit.py` and at
`_FOLLOWUP`, and the deliberate inversion of the two eviction orders is
explained where both are defined. I have nothing to add except that §3.1's
`P_w + A_w ≤ R` bound is what makes the per-worker split *safe* rather than
merely *fail-closed*, and it is worth stating in the module docstring, because
Phase 3 must preserve it: any shared-state design that makes credits global
while counters stay per-worker would break the bound in the other direction.

---

## 6. Mutation testing — my own set

17 mutations, all applied with `newline=""` against a **pure-CRLF** working
tree (`core.autocrlf=true`; 380 CRLF / 0 bare LF in `scout/ratelimit.py`), each
verified landed by byte delta before any conclusion was drawn. LF patterns are
translated to CRLF first — without that every mutation reports "did not land",
which is the trap this repo has hit twice.

| # | Mutation | Landed | Verdict |
|---|---|---|---|
| M1 | `_analysis_id` ignores the query string | −28 B | RED (7 failed) |
| **M2** | **`_analysis_id` reads the BODY first** | **−4 B** | **GREEN — SURVIVED** |
| **M3** | **`_analysis_id` drops `.strip()`** | **−8 B** | **GREEN — SURVIVED** |
| M4 | credit no longer single-use (`pop`→`get`) | +0 B | RED (3 failed) |
| M5 | expired credit still redeems | +0 B | RED |
| M6 | a refused `/progress` still grants a credit | +94 B | RED |
| M7 | session refusal falls through to the per-IP bucket | −231 B | RED (5 failed) |
| M8 | cookie-less callers get a fresh bucket each request | +41 B | RED (2 failed) |
| M9 | credit key drops the SESSION | −12 B | RED |
| M10 | credit key drops the ADDRESS | −23 B | RED |
| M11 | credit key drops the JOB | −12 B | RED |
| M12 | `/progress` spends instead of granting | +1 B | RED (7 failed) |
| M13 | ledger eviction drops furthest-to-expire first | +1 B | RED |
| M14 | counter eviction drops highest-hit-count first | +1 B | RED |
| M15 | session limit raised to 100 | +2 B | RED |
| M16 | credit TTL set to zero | −2 B | RED |
| M17 | `hit()` off-by-one (`hits < limit`) | −1 B | RED |

**15 RED, 2 GREEN. Both survivors are inside `_analysis_id`.**

That is the finding, not a coincidence. The builder's set tests the *contents*
of the credit key thoroughly — M9, M10 and M11 are all RED, and the commit
message describes fixing two tests that passed for the wrong reason by moving
one variable at a time. What no test touches is how the job id in that key is
**derived**. M2 (the naive body-first swap) and M3 (dropping `.strip()`) both
change `_analysis_id`'s behaviour and nothing notices. The exploit in §1 lives
in exactly that gap.

M3 is much the milder of the two — a whitespace-variant job id keys separately,
misses its credit and gets charged, so it fails closed — but it is an untested
line in a security-relevant key derivation.

---

## 7. Suite, both sides, first-hand

`-m pytest -q` from the repo root, **no path argument**, repo venv at
`C:\Users\lab\Documents\Claude_projects\tools-hub\venv\Scripts\python.exe`,
output redirected to a file (never piped).

| Commit | Result | Wall |
|---|---|---|
| base `6e0a68c` | **5126 passed, 21 skipped** | 511.63 s |
| branch `555448c` | **5151 passed, 21 skipped** | 645.27 s |

Both match the builder's reported numbers exactly. +25 tests: 24 new in
`tests/test_scout_anon_charge_pairing.py` (counted directly) plus
`test_the_session_tier_bites_before_the_per_ip_one` in
`tests/test_scout_anonymous_access.py`.

**`scipy 1.18.0` is present** in the repo venv, confirmed by import.
`freesasa` is **not** installed — which is why the new tests stub
`run_pipeline`, and why my CPU figures are converted from Phase 0 rather than
re-measured. Flask 3.1.3.

---

## 8. The `results.csv` claim — confirmed, and it is worse than stated

The commit asserts that round 2's suggested `results.csv` check on
`/scout/progress` would have served chain A's results for a chain-B request.
**True.** `scout/pipeline.py:405`:

```python
results_csv_path = pdb_path.parent / "results.csv"
```

Job-scoped, no chain in the path. A `results.csv`-exists check on `/progress`
would therefore skip the pipeline for any chain once any chain had been run.

**And `/scout/analyze` already has exactly that check** (`scout/routes.py:728`,
`if not csv_path_prelim.exists()`), so the bug is live today. Measured on a
real server:

```
1) analyse chain A normally (progress then analyze)  -> pipeline ran, chain=A
2) POST /analyze for chain B on the SAME job, no /progress
   /analyze chain=B -> 200
   pipeline re-ran for chain B? False
```

A chain-B request returned **200 with chain A's results and no pipeline run**.
The reachable path is the one Phase 0 already documented — a dropped SSE
stream, after which the browser posts `/analyze` anyway — except that instead
of paying twice, a user who has previously scored another chain on that job
silently receives the wrong chain's epitopes. This is a **correctness** defect,
not a cost one, and it predates Phase 4.

Two notes for whoever fixes it:

- The credit key does not include the chain either
  (`_followup_key`, `scout/ratelimit.py:277`), so `/progress?job=J&chain=A`
  buys a credit that `/analyze` on job J chain B can spend. Free *and* wrong.
  An attacker gains nothing, but it is the same missing dimension.
- Making `results.csv` chain-scoped fixes both this and unblocks the cheap
  `/progress` short-circuit round 2 wanted. That is probably the right Phase 5
  or 6 item; it is out of scope here but should not be lost.

---

## 9. The goal question, answered

> **Can six researchers behind one NAT all use the tool in the same 10-minute
> window, and is one attacker still bounded?**

**Six: yes, and now by arithmetic rather than by luck. Ten, in fact.**
Measured end to end on a real server, one worker, six sessions each doing the
real front-end flow: all six through, and researchers 7 through 10 as well.
Before this commit, measured on the same harness at `6e0a68c`, **the sixth was
refused on their first analysis** with no concurrency involved. The exhaustive
two-worker check (§3.1, 4,096 arrangements) shows the per-worker bound is R
under every split, so this is no longer decided by accept-queue luck. **Phase 4
moved this answer, and it is the real content of the commit.**

**First place it breaks now: `ANON_INTAKE_LIMIT` at the 11th researcher**, not
the analyze bucket. The two ceilings are now exactly balanced at 10, so the
next person to raise one must raise both.

**The attacker is bounded at the same place as before — ~300 CPU-s per IP per
window — except through the §1 divert, which raises it to ~420.** Fix the
divert and Phase 4 is CPU-neutral against the worst case while roughly doubling
what a real lab gets. That is a strictly good trade, and better than the commit
claims for itself.

**But "bounded" is doing light work.** ~300 CPU-s per address against a fleet
budget of ~1,200 means **four addresses saturate the box**, before and after.
That was already true at `6e0a68c` and at `48b4b71`. The per-IP count is not
what makes this system safe and cannot be made to be (§2.4). Phase 4 is a
fairness and correctness fix, correctly scoped; the safety question is
unanswered and belongs to `ANON_MAX_UPLOAD_BYTES` and to Phase 1's undelivered
fairness.

---

## 10. Is this safe to open as a PR?

**Not as it stands** — §1 must be fixed first, with a test that moves only the
query string. It is a small fix in one function.

On the mechanics of the chain, which are fine:

- The branch is three commits deep: `85a4fb6` (PR #155) → `6e0a68c`
  (round 3) → `555448c`. All three are off trunk, and PR #155 has not merged,
  so this PR would either stack on #155 or carry all three.
- `origin/main` is `48b4b71` and **did not move during this task** — the first
  time in a while. But trunk is five commits ahead of the merge base
  `fa938b0`, and one of them **touches `scout/routes.py`**: `48b4b71` replaces
  the inline `VALID_HANDOFF_TOOLS` set with an import from `scout/handoff.py`,
  at line ~1189. Phase 4's hunks are at lines ~100, ~691 and ~982, so this will
  auto-merge cleanly and correctly. Given this repo's history — a clean
  automerge is exactly the condition under which a lost hunk is invisible —
  diff `scout/routes.py` against both parents after merging and confirm both
  the handoff import and all three Phase 4 hunks survive.
- Re-run the full suite after the rebase. My numbers are for `555448c` on
  `fa938b0`, not for the merged result.

---

## 11. What Phases 2, 3, 5 and 6 must now assume differently

**Phase 2 (trust the peer).** Unchanged in substance, and still the blocking
precondition for raising the ceiling — the commit is right to refuse to raise
it. One addition: `_followup_key` now keys on `_client_ip()` too
(`scout/ratelimit.py:277`), so whatever Phase 2 does to that function changes
the credit key as well as the counter key. A caller who can choose their own
IP can also choose which credit bucket they land in. Fix the address before
raising the count, as planned.

**Phase 3 (shared counters).** The register grows by one: `_FOLLOWUP` must
move with `_WINDOWS`, and the commit says so in two places. Add the constraint
from §3.1: the `P_w + A_w ≤ R` bound depends on credits and counters living in
the *same* scope. Making credits global while counters stay per-worker would
let one credit offset a charge on a worker that never granted it. And the two
eviction policies must stay opposite — lowest-hit-count for counters,
soonest-to-expire for credits — for the reason given at `_MAX_KEYS`.

**Phase 4's own leftovers.** `ANON_INTAKE_LIMIT` and `ANON_ANALYZE_LIMIT` are
now both 10 and both binding (§3.2); raising one alone does nothing. And
`ANON_ANALYZE_SESSION_LIMIT` must always stay strictly below
`ANON_ANALYZE_LIMIT` or the tier is inert (§2.3) — worth an import-time assert.

**Phase 5 (the funnel).** Three things:
- `REASON_SESSION_LIMITED` is the right hook and is currently consumed by
  nothing but tests — the front end (`templates/scout/index.html`) never reads
  `reason` at all, it renders `msg`/`error`. Phase 5 has to add that reading.
- The "sign in to keep going" string will be shown to cookie-less visitors as
  a *lie* (§4.2). Either exclude `_NO_SESSION_KEY` from that message or detect
  the no-cookie case and say so.
- The per-IP refusal must **not** be turned into a signup prompt. Signing in
  genuinely fixes the session case and genuinely does not fix the per-IP one;
  the commit is right about this and Phase 5 must preserve the distinction it
  bought.

**Phase 6 (observability).** Alert on `session_rate_limited` **excluding**
`_NO_SESSION_KEY`, or the metric is sprayable (§4.2). And the number to alert
on is not the refusal rate alone: after this commit the same CPU ceiling is
reached with half as many refusals, so a falling refusal rate no longer means
a falling load. Count pipeline executions, which is the thing that actually
costs.

**The plan document.** Phase 4's entry says "Per-IP: generous". It is not
being made generous and should not be until Phase 2 lands — the commit is
right, and the plan should be amended rather than left to imply this phase
failed to do its job. The plan should also record §2.4: no per-IP count can
both serve a 95-284-user institution and stop one address saturating the box
at the current per-analysis cost, so `ANON_MAX_UPLOAD_BYTES` is a Phase-4-class
lever that nobody has scheduled.

---

## 12. Numbers I could not reproduce

| Claim | Status |
|---|---|
| `before ~180 CPU-s/IP`, `~7 addresses saturate` | **Reproduced as a lower bound only.** 180 is strategy S1; the attacker's best play on base was already ~300 / ~4 addresses (§2.2). |
| adversarial `/progress` ~9 CPU-s, `/analyze` ~6 CPU-s | **Not re-measured** — `freesasa` is not installed in the repo venv. Taken from Phase 0 and used only as a multiplier on counts I did measure. |
| "QC measured a thorough first-time visitor at 6 analyses" | **Could not be traced to any measurement.** It is a projection in the builder's own Phase 0 doc §2.3 (§4.1). |
| "15/15 mutations verified as landed and RED" | Not re-run; my independent 17 gave 15 RED / 2 GREEN (§6). |
| Phase 0's `9.0 CPU-s` worst case and `95-284` NAT population | Taken as given; out of scope for this round. |
