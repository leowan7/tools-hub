# QC — `a45252f` "test(scout): pin the body-size bound, which is load-bearing three ways over"

Independent review. I did not write this commit. Everything below was run in
detached worktrees under the session scratchpad (`qc-harden` @ `a45252f`,
`qc-base` @ `17a392a`, `qc-trunk` @ `c749480`). The main working tree and every
other worktree were left untouched; nothing here is staged, committed or pushed.

Throughout: **"measured"** means I ran it and read the output. **"read"** means I
inspected the source and reasoned. Only the first counts, and I say which.

---

## Verdict

**PASS WITH FINDINGS.**

The four mutations the author claimed all reproduce exactly — GREEN on the base
`17a392a`, RED on `a45252f`, and each one kills a *specific* new test rather than
collapsing the file. The behaviour change (`length is None` → fail closed) is
correct, fails closed, and cannot hurt the real front-end path. The suite, ruff,
and every pre-existing guard are clean.

The findings are all in the one piece of genuinely **new production code** in
this commit — `unmetered_bodies` and its once-per-process WARNING — which the
commit message headlines as "THE SILENT FAILURE IS NOW AUDIBLE". Measured, it is
substantially less audible than claimed:

* the alarm is permanently disarmed by **one unauthenticated request carrying no
  body at all**, and I measured a subsequent 25-request "the edge re-framed
  everything" outage producing **zero** log records (F1);
* the counter meant to distinguish noise from outage is **read by nothing in
  production** (F1);
* the WARNING text asserts an outcome ("the analysis was charged twice") that is
  false in the two most reachable cases (F2);
* and the two comment-asserted properties that justify the design — "fires ONCE
  per process" and "the counter keeps rising after the log" — are **pinned by
  nothing**. I mutated both and all 174 scout tests stayed green (F3).

F3 is the one that stings, because it is this commit's own thesis: *"the shipped
code was correct; what is missing is anything that KEEPS it correct."* The new
observability code ships with exactly the gap it was written to close, which is
the repo's recorded "guards that certify false" shape — now on the guard itself.

None of this is a correctness regression and none of it re-opens the billing
hole, so FAIL would be wrong. F1 is worth acting on before anyone relies on this
alarm to notice an outage.

---

## Baselines I measured

All measured first-hand with the repo venv
(`venv/Scripts/python.exe -m pytest -q`), from each worktree root, no path
argument, redirected to a file and read from the file — never piped through
`tail`.

| Tree | Commit | Result |
|---|---|---|
| `qc-trunk` | `c749480` (trunk) | **5372 passed, 21 skipped** — 346.67 s |
| `qc-base` | `17a392a` (parent) | **5481 passed, 21 skipped** — 380.37 s |
| `qc-harden` | `a45252f` (under review) | **5485 passed, 21 skipped** — 310.81 s |

Both stated baselines reproduce exactly (trunk 5372/21, branch 5485/21). The +4
from parent to branch is the four new tests. No flakes — the two known
node-related flaky tests did not fire, so no re-runs were needed.

Scout-only subset used for every mutation
(`test_scout_anon_charge_pairing.py`, `test_scout_anon_concurrency.py`,
`test_scout_anonymous_access.py`, `test_scout_access_control.py`), measured clean
on both trees before any mutation:

* `qc-base` — 170 passed, 1 skipped
* `qc-harden` — 174 passed, 1 skipped

`ruff check scout/ratelimit.py scout/routes.py tests/test_scout_anon_charge_pairing.py`
— clean on the branch.

One caveat on the parent number: my first `17a392a` full run was contaminated —
a mis-invoked harness applied and reverted a mutation in that worktree while the
run was in flight. I discarded it and re-ran on a `git status --porcelain`-clean
tree. **5481/21 is the clean re-run**, and it agreed with the contaminated one.

---

## Mutation method

Every mutation was a **byte-exact binary replacement** on a pure-CRLF tree
(`scout/ratelimit.py`: 784 CRLF / 784 LF on base, 832/832 on branch — no mixed
endings), with three guards:

1. the anchor string must occur **exactly once** — checked for all 11 mutations
   before any of them ran;
2. the replacement must be **present on disk** after writing (re-read and
   asserted), and the byte delta recorded;
3. `git checkout -- <file>` then `git status --porcelain` must show the tree
   **clean** again before the next mutation.

Base and branch received the **same semantics, not the same bytes**. The branch
splits the base's single `if length is None or length > _MAX:` into two guards,
so a byte-identical patch is impossible for MM and MK; in both cases I applied to
the branch exactly the code the base mutation produces.

---

## Mutation results — MM / ML / MK / MC

All four: **GREEN on `17a392a`, RED on `a45252f`.** The author's claim holds, and
each mutation kills a named test rather than a swathe of them.

### MM — make the oversize path fail OPEN (fall back to the query string)

`_metered_job_id`, oversize branch: `return ""` → `return job_id_in_query()`.

| Tree | Delta | Result |
|---|---|---|
| `qc-base` | +15 B | **GREEN** — 170 passed, 1 skipped |
| `qc-harden` | +15 B | **RED** — 1 failed, 173 passed |

Killed exactly `test_an_oversize_body_cannot_divert_the_credit_either`
(`tests/test_scout_anon_charge_pairing.py:579`).

This is the one that matters, and it is genuinely new coverage: its sibling
`test_a_query_string_cannot_divert_the_credit_on_analyze` (`:544`) sends a small
body and stays green under this mutation, which is precisely the blind spot the
commit message describes. Verified by mutation, not read.

My delta is +15 B rather than the +28 B the message reports — same fail-open
semantics, fewer characters. The verdicts are what I checked.

### ML — raise the bound past the test payload (4 KiB → 20 MB)

`scout/ratelimit.py:342` `_MAX_FOLLOWUP_BODY_BYTES = 4096` → `= 20 * 1024 * 1024`.

| Tree | Delta | Result |
|---|---|---|
| `qc-base` | +12 B | **GREEN** — 170 passed, 1 skipped |
| `qc-harden` | +12 B | **RED** — 1 failed, 173 passed |

Killed `test_a_refused_analyze_does_not_parse_a_large_body` (`:841`).

This confirms the correction the commit claims. On the base the payload was
`_MAX_FOLLOWUP_BODY_BYTES * 4` and scaled with the constant, so raising the bound
to the app-wide `MAX_CONTENT_LENGTH` (20 MB — `app.py:676`) left the test green
with the regression fully restored. The absolute literal fixes that. See F5 for
the residual gap.

### MK — relax the unknown-length clause

Base: `if length is None or length > _MAX:` → `if length is not None and length > _MAX:` (+5 B, exactly the delta claimed).
Branch: the whole two-guard block replaced with that same single line (−282 B, because the branch's guard carries a comment the base's does not).

| Tree | Delta | Result |
|---|---|---|
| `qc-base` | +5 B | **GREEN** — 170 passed, 1 skipped |
| `qc-harden` | −282 B | **RED** — 2 failed, 172 passed |

Killed both `test_a_refused_analyze_does_not_parse_a_body_of_unknown_length`
(`:876`) and `test_a_body_of_unknown_length_cannot_redeem_a_credit_and_says_so`
(`:904`). The two-tests-for-one-clause split is justified — they fail for
different reasons (a parse that should not have happened; a credit that should
not have been redeemed).

### MC — drop `.strip()` on the QUERY side

`job_id_in_query`: `return job_id.strip() if …` → `return job_id if …`.

| Tree | Delta | Result |
|---|---|---|
| `qc-base` | −8 B | **GREEN** — 170 passed, 1 skipped |
| `qc-harden` | −8 B | **RED** — 1 failed, 173 passed |

Killed `test_a_whitespace_padded_job_id_in_the_query_grants_its_own_credit`
(`:686`). The commit's "CORRECTIONS" claim — that the previous round's message
wrongly said both surviving `.strip()` mutations were killed when only the body
side was — is confirmed by the base column being green.

---

## My own mutations — do the NEW assertions pin what the NEW comments claim?

Seven further mutations, branch only, same harness. This is the part nobody has
done: attacking the hardening itself.

| Tag | Mutation | Delta | Result | Reads on |
|---|---|---|---|---|
| QC1 | counter capped at 1 (`+= 1` → `min(+1, 1)`) | +26 B | **GREEN** — 174 passed | **F3** — "the counter keeps rising" is unpinned |
| QC2 | WARNING on every request (`if first:` → `if first or True:`) | +8 B | **GREEN** — 174 passed | **F3** — "ONCE per process" is unpinned |
| QC3 | `reset()` no longer re-arms the alarm | +27 B | **RED** — kills `:904` | **F8** — the two chunked tests are order-coupled |
| QC4 | WARNING text no longer says "no Content-Length" | −10 B | **RED** — kills `:904` | log assertion is real |
| QC5 | counter never increments (log still fires) | −31 B | **RED** — kills `:904` | counter assertion is real |
| QC6 | bound 4 KiB → 1 048 575 B (just under the test literal) | +3 B | **GREEN** — 174 passed | **F5** — a 256× loosening is silent |
| QC7 | size bound removed entirely, `None` guard kept | −28 B | **RED** — kills `:841` | the bound's existence is pinned |

QC4 and QC5 independently confirm the commit message's claim that the two new
assertions were each broken on purpose and each went red alone.

---

## Findings

### F1 — The alarm can be disarmed by one bodiless request, and its counter is unreadable in production (Medium)

`scout/ratelimit.py:365-378` (`_note_unmetered_body`), comment at `:344-361`.

**What is wrong.** Werkzeug's `get_content_length` (read in
`werkzeug/sansio/utils.py`, werkzeug 3.1.8) returns `None` for a chunked request
**and** for any request that simply omits `Content-Length` — the two are
indistinguishable at `request.content_length`, which is the only thing
`_metered_job_id` reads. So the trigger is not "chunked framing"; it is "no
measurable length", which a bodiless POST satisfies.

Three things I **measured** on the branch:

1. **A bodiless POST trips it.** Over a real TCP socket against a real WSGI
   server (`werkzeug.serving.make_server`, not the test client):

   ```
   POST /scout/analyze HTTP/1.1
   Host: x
   Content-Type: application/json
   Connection: close
   ```

   → `HTTP/1.1 400 BAD REQUEST`, `unmetered_bodies == 1`, WARNING fired. No
   analysis, no credit, nothing charged twice. That is a two-line curl or any
   scanner touching a public POST endpoint.

2. **One noise request permanently disarms the alarm.** After that single
   bodiless POST I sent 25 consecutive chunked `POST /scout/analyze` requests —
   the exact "Railway's edge re-framed everything" scenario the comment
   describes. Measured: **0 log records**, counter 26.

3. **Nothing in production reads the counter.** `git grep unmetered_bodies` over
   `*.py`/`*.html`/`*.json` returns only `scout/ratelimit.py` itself and the two
   test assertions at `tests/…:923` and `:936`. `shared/metrics.py` exposes a
   Prometheus multiprocess registry at `/metrics` and already carries a
   scout-specific counter (`SCOUT_RUNS`, `shared/metrics.py:137`) plus a `Counter`
   factory that degrades to a stub when `prometheus_client` is absent
   (`:96`) — this counter is simply not registered with it. `reset()` is a test
   helper. `gunicorn.conf.py` sets **no `max_requests`** (grep: 0 hits there, in
   the Procfile, or in nixpacks), so workers are never recycled: the alarm is
   once per *deploy*, not once per hour.

**Concrete failure scenario.** A scanner sends one bodiless `POST /scout/analyze`
an hour after a deploy. The WARNING fires once, reads as "one odd client", and is
ignored — the log's own wording invites exactly that reading. Weeks later
Railway's edge starts re-framing `/scout/analyze` as chunked. Every anonymous
analysis loses its credit and is billed twice, effective capacity halves back to
five researchers per window, the refusal rate does not move because nothing is
refused — and **nothing logs, because the alarm was consumed weeks earlier**. The
counter climbs in two worker processes where nobody can read it. That is the
"outage that does not look like one", unchanged, plus one global variable.

The comment at `:359-361` states: *"The counter keeps rising after the log, which
is what separates 'one odd client' from 'the edge re-framed everything'."* The
first half is true (measured: 5 chunked posts → counter 5). The second half is
not achievable, because no production reader exists. This is a comment asserting
an operational capability the code does not provide.

**On the stated cost trade.** The comment justifies once-per-process because "a
log write per refused request would hand straight back the per-request cost the
bound above exists to remove". A *rate-limited* WARNING — first occurrence plus
one per N, or one per minute — costs nothing like a 20 MB body parse, and
registering the counter next to `SCOUT_RUNS` is a few lines against
infrastructure that already exists and is already scout-aware. Deferring the full
metric to Phase 6 is defensible; shipping the deferral described as "now audible"
is what I am flagging.

### F2 — The WARNING asserts an outcome that did not happen in the reachable cases (Low-Medium)

`scout/ratelimit.py:372-373`, and the counter's own comment at `:344-345`.

The log says the meter *"could not read its follow-up credit and **the analysis
was charged twice**."* The comment says the counter is *"How many follow-up
bodies arrived with NO Content-Length **and therefore lost their credit**."*

Both over-claim on two reachable paths:

* **A bodiless POST** (measured above) returns 400. There was no analysis, no
  credit, and nothing was charged twice.
* **A request the limiter then refuses.** `_metered_job_id` runs *before* both
  tiers (`scout/ratelimit.py:725`), so a caller already over the ceiling
  increments this counter — and can fire this WARNING — while being charged once
  and refused. Again not "charged twice".

**Failure scenario.** An operator reads the WARNING after a scanner sweep, opens
a double-billing investigation with no subject, and — the worse half — learns to
discount the message. Then the real event, if it ever logs at all (F1), is
discounted too.

### F3 — Two claims that justify the alarm's design are pinned by nothing (Low, but it is this commit's own standard)

`scout/ratelimit.py:356-361`.

* *"The WARNING fires ONCE per process, not per request"*, with a cost
  justification attached. **QC2**: `if first:` → `if first or True:`, +8 B.
  **All 174 scout tests stayed green.**
* *"The counter keeps rising after the log"* — the property the comment says
  separates one odd client from an edge re-frame. **QC1**: cap the counter at 1,
  +26 B. **All 174 scout tests stayed green.**

The single new test that touches this area (`tests/…:904`) sends exactly one
chunked request and asserts `unmetered_bodies == 1` plus the existence of one
WARNING record. That assertion is satisfied identically by a counter that stops
at 1 and by a log that fires every time. Both stated properties are free-floating.

Two extra lines in that same test — a second chunked request, then
`unmetered_bodies == 2` with still exactly one matching record — kill QC1 and
QC2 together.

### F4 — The routes.py headline claim is unpinned, and the commit says so (Low, disclosed)

`scout/routes.py:117-128`.

The new comment asserts *"THIS IS NOW THE FIRST WALL A REAL LAB MEETS"* and
*"The 10 == ANON_ANALYZE_LIMIT balance is ACCIDENTAL and nothing asserts it."*

**Verified by grep:** `ANON_INTAKE_LIMIT` and `ANON_ANALYZE_LIMIT` never appear
in the same assertion anywhere under `tests/`. `ANON_INTAKE_LIMIT` is referenced
only in `tests/test_scout_anonymous_access.py`, always as a loop bound for the
intake bucket alone. Nothing relates the two numbers. The commit's own
"unverifiable #4" is therefore **confirmed correct**.

I **read** rather than measured the arithmetic behind the claim: one intake + one
progress + one analyze per researcher; three routes on `scout_intake`
(`routes.py:525` upload, `:584` fetch-pdb, `:677` example) at limit 10; two on
`scout_analyze` (`:718` analyze, `:1015` progress) at limit 10; so both buckets
reach 10 after ten researchers and the eleventh's *first* request is the intake
one. That checks out. I did **not** construct the ten-researcher scenario
end to end.

Listed as a finding only because the commit adds a behavioural assertion to a
comment and adds no test for it, in a commit about adding tests.

### F5 — The bound can still be loosened 256-fold with every test green (Note)

`tests/test_scout_anon_charge_pairing.py:56` — `BODY_OVER_THE_METER_BOUND = 1024 * 1024`.

The absolute literal is a real improvement over `_MAX_FOLLOWUP_BODY_BYTES * 4`,
and ML proves it. But the property the test names is *"the meter never reads an
unbounded body ahead of the tier that refuses it"*, and **QC6** — raising
`_MAX_FOLLOWUP_BODY_BYTES` from 4096 to 1 048 575, a 256× increase in the
per-refused-request parse cost the bound exists to remove — left all 174 tests
green. The comment's own wording ("raise the bound past 1 MB") is literally
accurate, so this is a note on guard strength, not a false claim.

QC7 confirms the complementary half: removing the size comparison entirely *is*
caught (kills `:841`). What is unpinned is the bound's *value*, not its
existence.

### F6 — The commit message's suite counts no longer describe its own tree (Note)

The message reports *"5160 passed / 21 skipped at 4afb6ff, 5164 passed / 21
skipped here"*. Post-rebase the tree measures **5481** at the parent and **5485**
here — off by ~320. The `+4` delta survives; the absolute numbers are from the
pre-rebase base. Given this repo's history with quoted-vs-measured counts, worth
correcting in the message rather than leaving a number a future reader will try
to reconcile against a tree that never had it.

### F7 — The chunked-credit test never asserts the analysis actually happened (Note)

`tests/test_scout_anon_charge_pairing.py:904`.

Its docstring frames the behaviour as *fails closed but still works*: "Declining
an unreadable-size body **costs the caller its credit**, so the pair is billed
twice." The test asserts charges, the counter and the log — but never the
response status and never `stub_pipeline`. Every sibling in
`TestTheMeterAndTheViewReadTheSameJobId` asserts `stub_pipeline`; this one does
not.

**Measured** (a scratch probe reusing the file's own fixtures, since deleted):
the chunked finalise returns **200**, charges go 1 → 2, `unmetered_bodies == 1`,
and the pipeline list stays at the single `/progress` run — so the described
behaviour is exactly right today. It is simply not pinned. If a future change
made a chunked `/scout/analyze` fail outright, the test stays green while
"but still works" is gone.

### F8 — Ordering coupling on the new global (Info)

`tests/…:876` uses the `app` fixture and calls `ratelimit.reset()` only at the
*start*, so it leaves `unmetered_bodies == 1` behind. It is safe today only
because its sibling at `:904`, which asserts `unmetered_bodies == 0`, uses the
`client` fixture, which resets at setup — **QC3 demonstrates the coupling**:
removing the re-arm from `reset()` turns `:904` red.

CI runs `python -m pytest -q` serially (`.github/workflows/pytest.yml:89`), no
xdist and no random-order plugin is installed (`importlib.metadata` shows
`pytest` alone), so there is no live problem. Recorded because this is the first
piece of cross-test state in this module that no fixture teardown owns.

---

## What I could not verify, and why

I re-checked each of the author's four self-declared unverifiables. Three hold.
One is stated more pessimistically than the evidence requires, and that
pessimism is where F1 and F3 were hiding.

1. **"Whether an 18 MB refused body surfaces a 502 rather than a 429 once a real
   proxy is in front."** **Genuinely unverifiable here.** There is no Railway
   edge, no CDN and no reverse proxy in this environment, and the behaviour in
   question is entirely the proxy's — whether it tolerates an upstream answering
   before the request body is drained.

   The *local* half I did measure, over a real socket against a real WSGI server,
   with the per-IP analyze bucket already burned: an 18 MB refused body returned
   `HTTP/1.1 429 TOO MANY REQUESTS` in 0.011 s with no send error, a 1 MB one in
   0.002 s, and a small control in 0.003 s. So on loopback the fast refusal is
   clean. **This says nothing about a real proxy** and must not be quoted as if
   it did.

2. **"Whether the chunked-framing alarm's trigger condition actually fires
   (unprovable off production)."** **Partly wrong.** The trigger condition *is*
   provable off production and I proved it twice — through the Flask test client
   and over a real socket with genuine chunked framing (`1c\r\n{…}\r\n0\r\n\r\n`),
   both reaching `_note_unmetered_body` and both firing the WARNING. What is
   genuinely unprovable is only whether *Railway's edge would ever produce that
   framing*. Writing the whole thing off as unprovable is what left the disarm
   path (F1) and the two unpinned properties (F3) unexamined.

3. **"All CPU-second figures are converted from Phase 0, not measured."**
   **Confirmed unverifiable here.** `import freesasa` raises
   `ModuleNotFoundError` on this box — observed directly from `scout/sasa.py:85`
   in an unstubbed run — so `run_pipeline` cannot execute and no CPU-second
   figure in any comment or docstring in this commit is reproducible. The
   sub-second socket timings quoted in the docstrings (0.0053 s → 0.1936 s,
   0.336 s → 0.074 s) are likewise not reproducible to their exact values on a
   differently-loaded box; I reproduced the *direction* only.

4. **"`ANON_INTAKE_LIMIT == ANON_ANALYZE_LIMIT` is asserted by nothing."**
   **Verified true** — see F4. This one I could check, and the commit is right.

Also explicitly not verified:

* The ten-researchers-behind-one-NAT scenario end to end (F4) — read, not run.
* Behaviour under `gthread`. `gunicorn.conf.py` still sets no `worker_class`, so
  sync workers ship. `_note_unmetered_body` takes the same `_LOCK` as the rest of
  the module and is never called while that lock is held — `_metered_job_id` is
  invoked from `wrapped()` at `scout/ratelimit.py:725`, outside every `with
  _LOCK` block — so I see no deadlock. **Read, not executed under threads.**
* Whether gunicorn specifically (rather than `werkzeug.serving`) forwards an
  absent `Content-Length` as an absent `CONTENT_LENGTH` environ key. My socket
  test used werkzeug's server. The logic that yields `None` lives in
  `werkzeug.sansio.utils.get_content_length` and is shared, but the environ
  upstream of it is the server's.

---

## Things I checked that were fine

Stated so the clean results are not mistaken for gaps.

* `ruff check` passes on all three touched files — the new module-level global
  and its two `global` statements do not trip `PLW0603` under this config.
* No test anywhere asserts an empty `caplog`, so the new WARNING breaks no
  existing log guard. `scout.ratelimit` has no `propagate = False`, and `app.py`
  uses plain `logging.basicConfig`, so `caplog` genuinely captures it.
* **The real front end can never trip the new guard.**
  `templates/scout/index.html:392-396` uses `fetch` with a `JSON.stringify`
  string body (~55 bytes), which always carries `Content-Length`. The
  fail-closed change cannot hurt a real user today.
* `_note_unmetered_body` is unreachable for authenticated users
  (`anon_rate_limit` short-circuits on `session.get("user_email")` before
  `_metered_job_id`) and unreachable from `GET /scout/progress` (the length check
  is gated on `source is job_id_in_body`).
* **The `CHUNKED` test helper is a faithful stand-in, and I checked rather than
  assumed.** Measured, it produces `request.content_length is None` via the
  `Transfer-Encoding` header while `environ["CONTENT_LENGTH"]` is still `'29'` —
  a hybrid gunicorn would not produce. But `content_length` is the only thing the
  code reads, and I independently confirmed with **real** chunked framing over a
  socket that the same guard trips and the same counter increments. The helper's
  comment claims (werkzeug leaves `content_length` None; `wsgi.input_terminated`
  keeps the body readable) are both correct — the readable half verified by the
  view successfully parsing the body and running the pipeline.
* `reset()` correctly zeroes `unmetered_bodies` under `_LOCK` and re-arms the
  alarm; its "test helper, not used by request handling" docstring is still true.
* `_burn_the_per_ip_analyze_limit` (`:210`) and `_count_json_parses` (`:222`) are
  faithful extractions of the previously inlined code — same order of operations,
  same `parses.clear()` placement, and `_counting_get_json` records *before*
  delegating so it also counts parses that raise.
* Counting parses rather than timing is the right call; a wall-clock assertion
  would flake, and QC7/ML both show the parse count is sensitive enough.
* `BODY_OVER_THE_METER_BOUND` (1 MB) sits well under the app-wide
  `MAX_CONTENT_LENGTH` (20 MB, `app.py:676`), so the payload is not rejected by
  Werkzeug before reaching the code under test.
* The pre-existing structural guard `test_every_paired_route_reads_the_source_its_meter_declares`
  (`:716`) is untouched and still passes; the commit adds no route.

---

## Suggested follow-ups (not applied — report only)

1. Register `unmetered_bodies` with the existing registry in `shared/metrics.py`
   next to `SCOUT_RUNS`, or log at a bounded rate rather than exactly once.
   Either one makes the "one odd client vs the edge re-framed everything"
   distinction the comment already claims. (F1)
2. Reword the WARNING and the counter comment to say what is actually known — a
   follow-up body arrived with no measurable length and no credit could be read —
   rather than asserting a double-billing that often did not occur. (F2)
3. Add a second chunked request to `tests/…:904` and assert `unmetered_bodies == 2`
   with still exactly one WARNING record. Two lines; kills QC1 and QC2. (F3)
4. Assert the response status and `stub_pipeline` in that same test, so "fails
   closed **but still works**" is what is pinned. (F7)
5. Correct the suite counts in the commit message to the post-rebase numbers. (F6)
