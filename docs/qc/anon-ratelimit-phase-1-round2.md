# QC — Anonymous rate limiting, Phase 1 round 2

**Verdict: PASS WITH CORRECTIONS.**

Round 1's blocker is gone twice over: the worker class is reverted, *and* the
Modal gRPC calls are bounded anyway. Nothing here is a strict regression on a
live path except one tuning error (§4.3). The six defects are real fixes and
five of them are live under the deployed worker model.

But the commit is filed as Phase 1 and Phase 1's headline deliverable is not
delivered. I measured it: with the worker class reverted, the retained
2-slot semaphore and 2-place queue are **unreachable in production** — peak
in-flight is 1, no request is ever shed or queued (§2). The builder says so in
four places, which is honest, but the plan still says Phase 1 is "the
load-bearing phase" that makes the cost bound real, and after this commit it
does not. That has to be written down in the plan, not only in code comments.

And the mutation claim does not survive an independent set. **4 of my 23
mutations survived GREEN**, all four in the Modal deadline work, including the
exact ordering guard the commit message cites as proof (§4.2, §9).

**SHAs reviewed**

| | |
|---|---|
| Subject | `fb6d13d9928ef6aa50ff4dcd532a60a294913157` |
| Base / merge parent | `60f1a451ecdff03f3b43a4ac5b49ff802b2d19f4` |
| Also in the branch, not on trunk | `85a4fb641a60842c9c98abd6fc25536595c30b76` (SAbDab repoint, PR #155) |
| Merge base with `origin/main` | `fa938b09fd43aae6fc06976756f5c6fe379a537f` |
| `origin/main` at review time | **`48b4b71eedd2f791142ee4d020ee977a6961a6be`** — the task said `66388af`; **trunk moved during the task, again** |

`git merge-tree origin/main fb6d13d` is CLEAN. Trunk's only touch to a file
this branch edits is `scout/routes.py:1189` (`VALID_HANDOFF_TOOLS` re-export),
~500 lines away from every hunk here, so the clean automerge is genuine rather
than the invisible-lost-hunk kind.

**Isolation.** My worktree:
`C:\Users\lab\Documents\Claude_projects\tools-hub\.claude\worktrees\agent-a8b318e316f872a94`
(`git worktree list`: 16 live worktrees). The builder's worktree
`agent-a030522f6a1283bbb` and round 1's `agent-a3d23aed1b18bf18e` were read
only — `git show` through the shared object store, plus read-only file reads.
Nothing checked out, written, pushed, merged or pruned there. No production
contact of any kind; the `X-Forwarded-For` experiment was not run. Outbound
requests were to `rest.uniprot.org` only, the same public endpoint the code
calls. Working tree verified clean (0 modified tracked files) after every
mutation and at the end.

**Reviewed:** 2026-08-18. Reviewer did not build this phase.

---

## 1. Contract compliance

| Contract item | Status |
|---|---|
| 1. Pin the SHA reviewed | done, above |
| 2. Suite measured first-hand both sides, repo venv, `-m pytest -q` from repo root, no path arg, never piped through `tail` | done, §8 |
| 3. Re-run verification criteria empirically | done — real `create_app()` under a real WSGI server, §3 |
| 4. Mutation-test with my own mutations, each verified landed | done — 23, §9 |
| 5. Answer the goal question in writing | done, §10 |
| 6. Write `docs/qc/anon-ratelimit-phase-<N>.md` | this file |

---

## 2. The headline decision — is the retained cap reachable? Measured: NO

Gunicorn is Unix-only, so I modelled the sync worker the way gunicorn
implements it: **one request at a time per process**
(`gunicorn/workers/sync.py:31` runs `handle(...)` inline on the accept-loop
thread). Real `create_app()`, real decorators, real limiter, real slot; only
`run_pipeline` / `resolve_uniprot_id` / `fetch_known_binders` /
`detect_interfaces` stubbed, with a 1.5 s hold standing in for the CPU.
Six concurrent callers, each with its own cookie jar and its own job, released
through a barrier.

```
=== SINGLE-THREADED (= sync worker, AS DEPLOYED)  N=6 hold=1.5s slots=2 queue=2 wait=15.0 ===
   {'idx': 0, 'status': 200, 'elapsed': 1.52}
   {'idx': 1, 'status': 200, 'elapsed': 3.04}
   {'idx': 2, 'status': 200, 'elapsed': 4.56}
   {'idx': 4, 'status': 200, 'elapsed': 6.07}
   {'idx': 5, 'status': 200, 'elapsed': 7.58}
   {'idx': 3, 'status': 200, 'elapsed': 9.10}
   status counts: {200: 6}
   PEAK _INFLIGHT observed inside the slot: 1
   after: inflight=0 queued=0

=== THREADED (post-flip)  N=6 hold=1.5s slots=2 queue=2 wait=15.0 ===
   {'idx': 4, 'status': 503, 'elapsed': 0.04, 'reason': 'busy'}
   {'idx': 5, 'status': 503, 'elapsed': 0.04, 'reason': 'busy'}
   {'idx': 1, 'status': 200, 'elapsed': 1.54}
   {'idx': 0, 'status': 200, 'elapsed': 1.55}
   {'idx': 2, 'status': 200, 'elapsed': 3.05}
   {'idx': 3, 'status': 200, 'elapsed': 3.06}
   status counts: {503: 2, 200: 4}
   PEAK _INFLIGHT observed inside the slot: 2
   after: inflight=0 queued=0
```

**As deployed the code path is inert, in exactly the way the original 4-slot
cap was inert.** Peak in-flight 1, zero sheds, zero queue entries; the six
requests came out strictly serialised at 1.5 s intervals. The queueing that
did happen happened in the kernel accept backlog — unbounded, unmeasured, and
invisible to the app, which is the same place the plan warns an unbounded
application queue would end up.

The threaded run is the control, and it shows the guard is **correct and
correctly sized**: 2 admitted immediately, 2 admitted after queueing, 2 shed in
40 ms with `reason: "busy"`, no slot or queue place leaked. So this is not
broken code. It is right code that nothing can reach.

Say it plainly, because it was Phase 1's whole purpose: **the anonymous
compute cap is still decoration in production after this commit.** The commit
message, `scout/ratelimit.py`, `scout/routes.py` and `gunicorn.conf.py` all say
so; the *plan* does not, and Phase 4 quotes the plan.

What this commit *does* deliver live under sync workers:

| Change | Live today? |
|---|---|
| Modal gRPC 30 s deadline | **yes** — and it is the most valuable thing here |
| Third structure parse deleted from `/analyze` (~0.8 CPU-s/analysis) | **yes** |
| UniProt accession grammar at the trust boundary | **yes** |
| `_CACHE` bounded at 2048 | **yes** |
| `reason` on every refusal | **yes** |
| scipy fallback deleted | no-op in prod (scipy present); removes a latent cliff elsewhere |
| Lock no longer held across `yield` | correct, but unreachable — same as the semaphore |
| `gunicorn.conf.py` evaluation + a test that reddens on the flip | process value, real |

---

## 3. Claim 1 — the gunicorn watchdog. **The builder is right; round 1 is wrong.**

Read in the **installed** gunicorn (`venv/Lib/site-packages/gunicorn/__init__.py:5`
→ `version_info = (24, 1, 1)`), not from documentation:

```
workers/gthread.py:287    while self.alive:
workers/gthread.py:288        # Notify the arbiter we are alive
workers/gthread.py:289        self.notify()
workers/gthread.py:292        can_accept = self.nr_conns < self.worker_connections
workers/gthread.py:293-294    if can_accept != self._accepting: self.set_accept_enabled(can_accept)
workers/gthread.py:297        self.wait_for_and_dispatch_events(timeout=1.0)
workers/gthread.py:215        fs = self.tpool.submit(self.handle, conn)      # WSGI runs on the pool
workers/sync.py:61-62     while self.alive: self.notify()   ... :31 self.handle(...) INLINE
arbiter.py:515            if time.monotonic() - worker.tmp.last_update() <= self.timeout: continue
```

`notify()` is at the **top of the loop and unconditional** — it fires before
`can_accept` is even computed, and the loop is never blocked by request work
because requests go to a `ThreadPoolExecutor`. So under gthread a worker whose
every thread is wedged still heartbeats every ≤1 s and the arbiter never kills
it. Lowering `worker_connections` stops it accepting; it does **not** restore
the watchdog.

**Adjudication: the builder's mechanism claim is exact. The statement
attributed to round 1 — "`timeout` still covers a worker that stops accepting
entirely" — is false.** For the record, that exact sentence does not appear in
`docs/qc/anon-ratelimit-phase-1.md`; §2.1 of that document reaches the same
conclusion the builder does ("Conclusion: correct"). So the disagreement is
with something said outside the written QC, and on the substance the builder
wins.

Under sync workers `notify()` fires only *between* requests, so `timeout=120`
genuinely bounds a slow request. That property is what makes every remaining
unbounded blocking call in the app survivable, and preserving it is the single
strongest argument in the commit.

---

## 4. Claim 2 — the Modal deadline

### 4.1 Coverage of the call sites: complete

Every `modal.*` attribute access in `gpu/modal_client.py` is inside
`_bounded_modal_call` — lines 361 (`Function.from_name`), 416 and 471
(`FunctionCall.from_id`) — plus `fn.spawn` (:367), `fc.get` (:421) and
`fc.cancel` (:474). Grep for `modal.` in that file returns exactly those and a
docstring. Every path from the web process to Modal goes through
`ModalClient.submit/poll/cancel` (`blueprints/jobs.py:310,529`,
`blueprints/tools.py:1912`, `shared/jobs.py:990`, `webhooks/modal.py:406`,
`shared/compute_campaigns.py:1930,2569,2639`, `shared/job_recovery.py:152`).
**No missed entry point.**

Two things sit just outside the bound and are fine: `getattr(function_call,
"object_id", ...)` after `spawn` (a plain attribute on a hydrated object) and
`_interpret_pipeline_return` (pure).

This file has history — the cross-job stale-result leak (#133) and the still-
open `exit_code` gate. The gate now lives at `gpu/modal_client.py:554` / `:619`
inside `_interpret_pipeline_return`; **this diff does not touch it**, so that
open item is neither closed nor disturbed.

### 4.2 The `ModalCallTimeout`-before-`TimeoutError` ordering: correct code, **no guard**

First, the premise, checked in the installed **modal 1.4.2**:

- `modal.exception.TimeoutError.__mro__` is `(TimeoutError, modal.exception.Error, Exception, BaseException, object)` — it does **not** subclass the builtin. (`tools/proteina/shard_driver.py:709` records this.)
- But `modal/_functions.py:327` `raise TimeoutError()` is the **builtin** — `TimeoutError` is not among the names imported from `.exception` at `:63-68`. So `fc.get(timeout=0)`'s "not finished yet" really is a builtin `TimeoutError`, and `poll`'s `except TimeoutError:` really does catch it. The builder's premise holds.

Second, the consequence, executed. I wedged `fc.get` (not `from_id`), which is
the only place the clause can fire:

```
WITH the shipped `except ModalCallTimeout: raise` clause:
   poll -> error
Same wedge, clause absent:
   ModalCallTimeout is caught by a bare `except TimeoutError` -> poll would return status='running'
```

So the clause is genuinely load-bearing: without it a permanently dead channel
is reported as a healthy running job forever.

**And nothing tests it.** My mutation M11 replaced `except ModalCallTimeout:`
with `except ValueError:` — the whole of `tests/test_modal_call_deadline.py`
stayed **GREEN, 10 passed**. The reason is structural: `_WedgedModal` blocks at
`Function.from_name` / `FunctionCall.from_id`, which are *outside* the inner
`try`, so `fc.get` is never reached. The same structure means M13
(`fn.spawn` unwrapped), M14 (`fc.get` unwrapped) and M15 (`fc.cancel`
unwrapped) also survived GREEN.

> **Of the six wrapped Modal call sites, only the two first calls
> (`from_name`, `from_id`) are covered by a test. `spawn`, `get`, `cancel` and
> the ordering clause are unguarded.** The commit message asserts the ordering
> as a checked property; it is not.

This is the third time in this repo a guard has certified a property it does
not test. It is a test defect, not a production defect — the shipped code is
right — but it is exactly the item the review was asked to break-test, and it
does not break.

**Fix (cheap):** give the fake a second mode that succeeds at `from_id` and
wedges at `get`, and a third that wedges at `spawn` / `cancel`. Three extra
fixtures, no new machinery.

### 4.3 CORRECTION — 30 s is set **below** Modal's own connect budget

`modal/_utils/grpc_utils.py:231`:

```python
@retry(n_attempts=18, base_delay=0.1, attempt_timeout=10.0, max_delay=5.0, total_timeout=63.0)
async def connect_channel(channel): ...
```

Establishing the channel has a **63 s** retry budget by design — 18 attempts,
for exactly the transient blips this deadline is meant to survive. With
`_MODAL_CALL_TIMEOUT_SEC = 30.0`, a cold worker or a transient Modal blip that
the SDK would have ridden out at, say, 40 s now becomes a hard `submit`
failure: the caller releases the wallet hold and the user sees an error, where
before it succeeded well inside gunicorn's 120 s.

Worse, `submit` makes **two** bounded calls, so a slow-but-working submit can
also be cut in the middle: `from_name` inside 30 s, `spawn` cut at 30 s — and a
`spawn` that lands a second after we gave up enqueues a **billed GPU job with
no job row tracking it**.

**Recommended:** wrap `from_name` + `spawn` in **one** `_bounded_modal_call`
with a single budget of ~90 s. That is above the SDK's 63 s connect budget,
below gunicorn's 120 s watchdog, keeps the existing assertion
(`0 < _MODAL_CALL_TIMEOUT_SEC <= 120`) green, and removes the split-submit
orphan. One lambda, fewer threads.

### 4.4 The orphaned daemon thread is rate-limited, not bounded

The `ponytail:` comment says the leak "is bounded by how often Modal can time
out". That is a rate, not a bound. Under sync workers a dead channel gives one
timed-out poll every ~30 s per worker — ~120 leaked threads per hour per
worker, and `gunicorn.conf.py` sets **no `max_requests`**, so no worker ever
recycles to reclaim them. Container pid limits are commonly ~4096, i.e. ~day-
scale to exhaustion during a sustained outage.

This is still better than round 1's alternative (the old code blocked forever
and took the worker with it). But it converts an unbounded *per-request* wedge
into an unbounded *over-time* thread leak with nothing to sweep it.
**Recommended, one line:** `max_requests = 1000` + `max_requests_jitter = 100`
in `gunicorn.conf.py`. Not a blocker.

State corruption on a late-returning orphan: none. `box` is a per-call local,
the thread writes only into it, and nothing reads it after the timeout. Verified
by reading; the `raise box["error"]` path re-raises the original instance, so
callers' existing error handling is unchanged (test
`test_an_exception_is_re_raised_on_the_caller` covers this and is real).

---

## 5. Claim 3 — the lock across the yield: reproduced independently

I did not read the source and agree. Own probe, own threads: one caller holds
the only slot; a second enters the shed branch (`max_waiting=0`) and holds its
`with` body for 0.35 s; a third samples `_INFLIGHT_LOCK.acquire(blocking=False)`
one third of the way through the body.

- shed body, lock acquired mid-body: **True** (lock free)
- granted body, lock acquired mid-body: **True** (lock free)

And the mutations confirm the probe can see the defect: M9 (put the shed
`yield False` back under the lock) → **RED**; M10 (put the granted `yield True`
under the lock) → **RED** (9 failures, and the file took 427 s instead of 5 s —
the serialisation is visible in the wall clock as well as the assertion).

The `Condition()`-is-RLock-backed note in `scout/ratelimit.py` is correct and
worth keeping: reentrancy is what let this hide.

Accounting checked alongside it: the shed path acquires nothing, so it releases
nothing; the granted path releases in a `finally` and `reset()` now clears
`_WAITING` too (M5 red). M3 (drop `_WAITING -= 1`) and M4 (drop `notify()`) are
both red, so the queue cannot leak a place and cannot lose a wakeup.

---

## 6. Claim 4 — the scipy deletion. Local green is **not** CI green here

Confirmed with the parent's finding: **`scipy` is absent from the repo venv**
(`ModuleNotFoundError`), while `requirements.txt:17` has `scipy>=1.11`.

- `.github/workflows/pytest.yml:84` installs `requirements-dev.txt` = requirements + pytest, so **CI does have scipy**.
- Therefore `test_the_happy_path_still_finds_the_interface` (`pytest.importorskip("scipy")`) is **skipped locally, runs in CI**. Identified, not assumed — `pytest -q -rs`:

```
SKIPPED [1] tests\test_scout_interfaces_scipy.py:87: scipy is not installed here
SKIPPED [1] tests\test_scout_epitope_db_sabdab.py:432: set SCOUT_SABDAB_LIVE=1 ...
```

**Yes — the +1 skip (branch 22 vs base 21) is exactly that test.**

What that means, precisely:

1. **The local 5114-passing run never exercises the real `cKDTree` path.** Every local call to `detect_interfaces` returns `[]` at the up-front import guard. Anything downstream of `ppi_interfaces` is exercised only against the empty case here. CI does cover it.
2. **On this box the two absence tests pass for the ambient reason, not the simulated one.** Demonstrated, not argued: I neutered the `no_scipy` fixture (`if name == "scipy" or ...` → `if False:`, verified landed) and re-ran the file — **`2 passed, 1 skipped`, unchanged**. The fixture is only load-bearing where scipy exists, i.e. in CI.
3. They are still not vacuous *here*: my mutation M23 reinstated a working pure-Python fallback behind the import and the file went **RED (2 failed)**. So the "no fallback behind scipy" property is genuinely guarded on both boxes; what is unguarded locally is "the real path returns anything at all".

**Is ERROR-and-skip the right degradation for a free-tier user-visible
feature?** For an env that is built right, yes — it can never fire. For an env
that is built wrong it is arguable, and there is a live precedent in the same
module that argues against it:

> **`scout/pipeline.py:693` imports `detect_ppi_interfaces` from
> `scout/interfaces.py`. That function has never existed** — `git log -S` puts
> the import at `3ba4c5d` and the name is absent from `scout/interfaces.py` at
> `60f1a45`, `fb6d13d` and `origin/main`. The surrounding
> `try/except Exception` swallows the `ImportError` and logs a warning, so
> **"Step 8: Interface competition" has scored a constant `1.0` for every run
> since it was written**, on the free tier, in the ranked output.

Pre-existing, not introduced here, and out of scope — but it is the exact
failure mode the commit is legislating about, one function away, and it is the
best available answer to "is a logged degradation loud enough?" Empirically:
no. If the image can be built without scipy, prefer failing at boot
(`import scipy` at module import of `scout/interfaces.py`) over an ERROR line
per request. That also makes the local venv's state impossible to ignore.

---

## 7. Claims 5-8

### 7.1 Claim 5 — the re-sizing arithmetic. Numbers right, inference wrong.

**The model is right, and I measured it rather than agreeing with it.** N
threads in one CPython process, each doing a fixed mixed pure-Python + numpy
load of 0.32 CPU-s:

| N | first done | last done | spread first→last | effective cores |
|---|---|---|---|---|
| 1 | 0.31 s | 0.31 s | 0.0% | 1.01 |
| 2 | 0.63 s | 0.63 s | 0.4% | 1.00 |
| 4 | 1.26 s | 1.27 s | 0.7% | 1.00 |
| 8 | 2.60 s | 2.62 s | 0.5% | 0.97 |

Every N finishes within **under 1%** of itself, and the **first** completion
lands at `N x cost / E`, not at `cost`. So the claim that "the first slot to
free frees LATER the more slots there are" is empirically correct, and it is
the genuinely new observation of this round — both earlier rounds sized as if
slot 1 freed at `cost`. (My unit is loop-dominated so `E ≈ 1.0`; the real
workload's `1.07` comes from numpy/freesasa releasing the GIL. The shape is
identical.)

The arithmetic that follows from it is also correct:

| N | `N x 15 / 1.07` | commit says |
|---|---|---|
| 2 | 28.0 s | ~28 s ✓ |
| 4 | 56.1 s | ~56 s ✓ |

The typical case: `2 x 2 / 1.07 = 3.7 s` ✓ ("~4 s typical").
Served worst case `15 + 28 = 43 s` ✓.

**The inference does not follow.** `scout/ratelimit.py` argues:

> `N=4 -> ~56 s worst case: longer than any wait a browser should hold, so a
> queued caller could never be served at all under adversarial load. The queue
> would be decoration. ... Two is what makes the queue able to do its job.`

At N=2 the first free slot is at **28 s, also longer than the 15 s wait**. So a
queued caller cannot be served under adversarial load at N=2 either — and the
shipped test *asserts* that (`first_free > ANON_QUEUE_WAIT_SEC`). Conversely
under typical load N=4 frees a slot at 7.5 s, inside both the old 20 s and the
new 15 s wait, so the queue did its job at N=4 too. **N=2 versus N=4 does not
discriminate on the stated criterion in either direction.**

The real, and defensible, effect of 4→2 is elsewhere: it halves the worst-case
in-process anonymous CPU burst (60 → 30 CPU-s) and the served worst case
(79 → 43 s). Say that instead. The GIL-interleaving insight is genuinely good
and genuinely was missed before; it just sizes the *wait*, not the *slots*.

Second-order: `1.07 effective cores` was measured for a **gthread** process.
Under the 2 sync workers actually deployed there are two real processes and
therefore ~2 real cores of anonymous parallelism, with no GIL sharing at all.
Every number in this section describes a configuration that does not exist yet.
That is fine as pre-derivation for the flip, and it should be labelled as such.

### 7.2 Claim 6 — the parses

`chains.json` is written by all three intake routes (`upload`, `fetch_pdb`,
`example`) after the `result.error` check, so `result.chains` is always a list
of `ChainInfo(id: str, residue_count: int)` dataclasses (`scout/parser.py:58-70`).
Read-back failure modes all fall through to the in-slot parse:

| chains.json state | `_chain_residue_count` |
|---|---|
| absent | `OSError` → parse (in slot) ✓ |
| truncated / not JSON | `ValueError` → parse ✓ |
| valid JSON but a list/number | `AttributeError` on `.get` → parse ✓ |
| valid, chain missing | `None` → `isinstance` false → parse ✓ |
| valid, chain present | returned, no parse ✓ |
| parse also fails | `None` → `_max_resi = None`, filter disabled — identical to the pre-commit `except Exception: pass` ✓ |

The in-slot placement is guarded, and the guard works: **M18** (move the call
back below the `with`) → **RED** on
`test_the_fallback_parse_runs_inside_the_compute_slot`, which asserts
`inflight_anon_runs() >= 1` around a monkeypatched `parse_pdb`. **M19**
(stop writing the index in `example()`) → **RED** (4 failures). The assert
cannot fire in normal operation: it is a test-side observation, not a
production `assert`, and it only claims something for anonymous callers, who
always hold the slot on that path.

Nit: `_save_chain_index` catches only `OSError` while its docstring promises
"best effort". Any other exception 500s an intake that had already succeeded.
Widen to `Exception`.

**Leaving the other parses is defensible** — three different residue-selection
predicates across three modules, ~10% of the cost, and collapsing them changes
ranked output on the free tier. But Phase 4 will trip over something bigger
that this commit does not touch:

> **`/scout/progress` runs `run_pipeline` unconditionally** (`scout/routes.py:940-946`)
> — there is no `results.csv` existence check, unlike `/analyze`. So an
> attacker who simply re-requests `/scout/progress?job_id=...` on one job pays
> the full ~9 CPU-s pipeline every time. The metered bucket `scout_analyze` is
> shared by `/analyze` and `/progress`, so the worst adversarial spend per IP
> per window is **20 hits all aimed at `/progress` ≈ 180 CPU-s**, not
> 10 analyses × 15. Sizing Phase 4 from "cost per analysis" understates it.

### 7.3 Claim 7 — the cache bounds. Verified by execution.

Grammar, against the live UniProt API — 1,500 real accessions (500 Swiss-Prot,
500 TrEMBL, 500 reviewed human), lengths `{6, 10}`:

```
swissprot: 500 accessions, rejected=0
trembl:    500 accessions, rejected=0
human_sp:  500 accessions, rejected=0
TOTAL 1500 REJECTED 0
```

`ZZ9QC001` → `''`, at the extractor **and** at `fetch_known_binders`:

```
extract(bogus DBREF ZZ9QC001)   -> ''
extract(real DBREF P00698)      -> 'P00698'
fetch_known_binders('ZZ9QC001') -> []   cache size: 0
```

The regex is UniProt's own published grammar, both forms, correctly anchored;
10-character accessions (`A0A022YWF9`, `A0A0C5B5G6`) pass. I found no valid
accession it rejects.

One behaviour change to record: **isoform-suffixed accessions (`P00698-2`) are
now rejected.** RCSB puts isoforms in `_struct_ref.pdbx_db_isoform`, not in
`pdbx_db_accession`, so this should not fire on RCSB-derived files; a
hand-written mmCIF carrying `P00698-2` in the accession field silently loses
the DBREF fast path and falls through to the UniProt sequence search — one
extra bounded outbound GET, not a failure.

Bound and eviction:

```
after 2098 puts: size = 2048  (cap 2048)
'K0' present? False | 'K49' present? False | 'K50' present? True | newest present? True
re-put of an existing key keeps its insertion position: True  ['a','b','c']
```

So it is FIFO on first insertion, not LRU — which matches the comment
("oldest-inserted"). **Can eviction be turned into a quota reset? No.** The
values are binder-lookup results, not counters; evicting one costs the *server*
a recomputed lookup, and inserting the 2048 keys needed to force an eviction
costs ~4.2 CPU-s each anyway — strictly more expensive for the attacker than
the eviction is worth. The builder's argument holds, and the rate limiter's
lowest-hit-count ordering is correctly left alone. M16 (grammar disabled) →
**RED, 10 failures**; M17 (eviction removed) → **RED, 3 failures**.

### 7.4 Claim 8 — refusal reasons. The guard does fail when broken.

`REASON_RATE_LIMITED` / `REASON_BUSY` / `REASON_AT_CAPACITY` are on all six
refusal bodies: the per-IP 429 JSON and SSE frame (`scout/ratelimit.py`), the
`/analyze` 503 shed and the `/progress` SSE shed, and both
`_anon_capacity_error` bodies (503 global, 429 per-session). Verified live in
§2 — the shed responses carried `reason: "busy"` over real HTTP.

One gap, small but in a field now declared an API surface: `/scout/progress`'s
`_error_stream` (`scout/routes.py:917-923`, expired or missing job) emits
`{"stage": "error", "msg": ...}` with **no `reason`**, so a front end switching
on `reason` sees `undefined` for the most common non-limiter error on that
route. Add a fourth constant or reuse `at_capacity`'s shape.

Break-tested, all three:

| mutation | result |
|---|---|
| M20 SSE shed reuses `"rate_limited"` so the two SSE refusals become identical | **RED** |
| M21 `/analyze` 503 loses its `reason` | **RED** |
| M22 `at_capacity` reason dropped from the 503 body | **RED** |

`test_the_two_sse_refusals_are_identical_apart_from_reason` is a good test: it
asserts status, mimetype and `stage` are equal and only `reason` differs, so it
fails both if the distinction is removed and if the two refusals stop being
otherwise identical.

---

## 8. Suite, measured first-hand on both sides

Repo venv `C:\Users\lab\Documents\Claude_projects\tools-hub\venv\Scripts\python.exe`
(Python 3.13.0; there is no venv in any worktree), `python -m pytest -q` from
the repo root, **no path argument**, stdout redirected to a file and read
afterwards — never piped through `tail`.

| Side | SHA | Result |
|---|---|---|
| Base | `60f1a45` | **5066 passed, 21 skipped** in 471.89 s |
| Branch | `fb6d13d` | **5114 passed, 22 skipped** in 509.37 s |

Delta: **+48 passed, +1 skipped**, 0 failed, 0 errors either side.

**Both numbers match the builder's report exactly**, and the base also matches
round 1's independently measured `60f1a45` figure. The two node tests known to
flake under load did not fire in either run.

Environment caveat that travels with these numbers: this box has **no scipy and
no freesasa**, CI has both. Three of the 22 skips in the five files I
mutation-tested are dependency skips, one of them the new
`test_the_happy_path_still_finds_the_interface` (§6). Local green is weaker
evidence than CI green for anything touching `scout/interfaces.py` or
`scout/pipeline.py`.

---

## 9. Mutation testing — my own 23, every one verified landed

Harness rules, applied to all 23: the exact old text must appear **exactly
once**; the write must change the file bytes; the new text must be present on
read-back; all I/O explicit `encoding="utf-8"` (this repo has lost a mutation
to a Windows em-dash before); `git checkout --` after each, with a byte-equality
assertion on the restore. **0 failed to land.** Tracked files verified clean at
the end.

Unmutated baseline for the five files: **145 passed, 3 skipped** in 74 s.

| # | Mutation | Landed | Result |
|---|---|---|---|
| M1 | `ANON_MAX_CONCURRENT_RUNS` 2 → 4 | ✓ | RED |
| M2 | queue ceiling removed (`elif _WAITING < max_waiting` → `elif True`) | ✓ | RED |
| M3 | `_WAITING -= 1` dropped from the `finally` | ✓ | RED |
| M4 | `_INFLIGHT_LOCK.notify()` removed | ✓ | RED |
| M5 | `reset()` stops clearing `_WAITING` | ✓ | RED |
| M6 | signed-in callers made to consume a slot | ✓ | RED |
| M7 | `max_waiting` default frozen at import | ✓ | RED |
| M8 | `worker_class = "gthread"` + `threads = 8` | ✓ | RED |
| M9 | shed `yield False` put back under the lock | ✓ | RED |
| M10 | granted `yield True` put under the lock | ✓ | RED (9 failed, 427 s) |
| **M11** | **`except ModalCallTimeout:` → `except ValueError:`** | ✓ | **GREEN — SURVIVED** |
| M12 | `Function.from_name` unwrapped | ✓ | RED |
| **M13** | **`fn.spawn` unwrapped** | ✓ | **GREEN — SURVIVED** |
| **M14** | **`fc.get` unwrapped in `poll`** | ✓ | **GREEN — SURVIVED** |
| **M15** | **`fc.cancel` unwrapped in `cancel`** | ✓ | **GREEN — SURVIVED** |
| M16 | accession grammar disabled | ✓ | RED (10 failed) |
| M17 | `_CACHE` eviction loop removed | ✓ | RED (3 failed) |
| M18 | fallback parse moved back outside the slot | ✓ | RED |
| M19 | `example()` stops writing the chain index | ✓ | RED (4 failed) |
| M20 | SSE shed reuses the rate-limit reason | ✓ | RED |
| M21 | `/analyze` 503 loses its `reason` | ✓ | RED |
| M22 | `at_capacity` reason dropped | ✓ | RED |
| M23 | pure-Python fallback reinstated behind scipy | ✓ | RED (2 failed) |

**23 landed, 19 RED, 4 GREEN.** All four survivors are `gpu/modal_client.py`,
and they are one structural defect, not four: the test's fake wedges at the
*first* call in each method, so no test ever reaches the second. See §4.2.

---

## 10. The goal question

### Can six researchers behind one NAT all use the tool in the same afternoon?

**Measured, end to end, on the branch, through a real server.** Six sessions
from one address, sequential, no concurrency at all, each doing the real front-
end flow (`/scout/example` → `/scout/progress` → `POST /scout/analyze`):

```
   researcher | /example | /progress        | /analyze
      1       | 200      | 200              | 200
      2       | 200      | 200              | 200
      3       | 200      | 200              | 200
      4       | 200      | 200              | 200
      5       | 200      | 200              | 200
      6       | 200      | 200/rate_limited | 429/rate_limited
```

**Five fit per worker; the sixth is refused, on the very first analysis, with
no concurrency involved at all.** One analysis costs 2 hits in the
`scout_analyze` bucket (`/progress` + `/analyze`) against `ANON_ANALYZE_LIMIT
= 10`, so the wall lands on hit 11 = researcher 6.

In production there are 2 workers with independent in-memory counters, so
6 × 2 = 12 hits *can* fit if they split 6/6. Gunicorn sync workers accept from a
shared socket and the split is whoever-wakes-first, not round robin. So the
honest answer is: **six researchers behind one NAT is exactly on the boundary,
and whether they all get through is decided by accept-queue luck, not by
design. A seventh, or any one of the six doing a second analysis, is refused.**

> **First place it breaks: the `scout_analyze` bucket, at the 6th researcher on
> a worker (or the 3rd analysis by any one of them). Unchanged by this round.**

Round 1 said the same thing in different words ("2nd-3rd concurrent
researcher"); my number is measured with the minimal one-analysis-each
workload, which is why it is 5 rather than 2-3.

**This round did not move that answer, and could not have** — it does not touch
the limiter, the bucket, the window, or the two-hits-per-analysis cost. What it
moved is the answer to a different question: whether the box stays sane
*behind* the wall. It did not move that either, because the bound it added is
unreachable (§2). What it did move is the failure modes on either side of the
wall: refusals are now machine-labelled, the Modal wedge is bounded, and one
unmetered parse per analysis is gone.

### Is one attacker still bounded?

**Yes, and slightly better than before, but the bound is entirely the per-IP
limiter — nothing in this commit contributes to it.**

Per IP per 10-minute window: 20 metered `scout_analyze` hits fleet-wide. Worst
case is all 20 aimed at `/scout/progress`, which re-runs the whole pipeline
every time (§7.2): **~180 CPU-s against Phase 0's ~1,200 available, ~15% of the
box.** (Round 1's ~320 CPU-s / ~27% counted 20 *analyses* rather than 20 hits;
the pair `/progress` + `/analyze` is one analysis. Use ~180.) The third-parse
deletion trims ~0.8 CPU-s per `/analyze`; the accession grammar removes the
free permanent cache key. Both help; neither changes the order.

> **First place it breaks: it is not the anonymous path and it is not one
> attacker — it is about seven distinct source addresses.** The per-IP limiter
> is the only bound, and it is per-IP by construction, so `1,200 / 180 ≈ 7`
> addresses saturate the two workers completely (Phase 0's box capacity is
> `2 workers x 600 s ≈ 1,200 CPU-s per 10-min window`,
> `docs/qc/anon-ratelimit-phase-0.md:364`). Under sync workers a saturated box
> does not shed, it
> queues in the kernel and every other route, `/healthz` included, waits behind
> the anonymous compute. That is the fairness problem Phase 1 exists to solve
> and it is still open.
>
> Second: the daemon-thread leak under a sustained Modal outage (§4.4), which
> needs no attacker at all.

---

## 11. Is this safe to open as a PR? Can it ship before the idempotency fix?

**Yes to both, with the corrections below applied first.**

**"Nothing in this commit increases request concurrency" — verified.** Against
trunk, not just against `60f1a45`: `worker_class` is unset in `gunicorn.conf.py`,
`Procfile` and `nixpacks.toml` (all three read; `threads` absent, so gunicorn
cannot auto-promote sync → gthread); §2 measures peak in-flight = 1;
`_bounded_modal_call` adds a worker thread but the request thread blocks on
`join`, so request concurrency is unchanged; and `85a4fb6` *removes* a
40-thread fan-out. **This branch does not depend on `shared/idempotency.py` or
`shared/wallet.py` landing first.** The worker-class flip does, and
`gunicorn.conf.py` says so in the right place.

**Corrections to apply before the PR**

1. **`_MODAL_CALL_TIMEOUT_SEC` (§4.3).** Wrap `from_name` + `spawn` in one
   `_bounded_modal_call` with a ~90 s budget: above Modal's 63 s connect
   retry, below gunicorn's 120 s, and it removes the split-submit orphan-GPU-job
   window. **The one item that is a live regression risk on a revenue path.**
2. **Test the other four Modal call sites (§4.2).** M11/M13/M14/M15 must go RED.
3. **Restate the 4→2 rationale (§7.1).** The stated criterion does not
   discriminate 2 from 4; the honest reason is halving the worst-case burst and
   the served worst case.
4. **Say "Phase 1 did not deliver its headline" in the plan**, not only in code
   comments (§2, §12).

**Cheap, same PR:** `max_requests` + jitter (§4.4); widen `_save_chain_index`'s
`except OSError` to `Exception` (§7.2); add `webhooks/stripe.py`'s ~240 s
`PaymentIntent.retrieve` to the "TO FLIP IT" prerequisite list.

**Bisect hazard, decide before merging.** The branch is three commits:
`85a4fb6` → `60f1a45` → `fb6d13d`. **`60f1a45` sets `worker_class = "gthread"`
and this branch reverses it in a later commit rather than by rebase.** If this
lands as a merge commit (the repo's convention — see `Merge pull request #143`),
`60f1a45` goes onto `main` and any bisect landing on it gets a gthread config
with the money-path races wide open. **Squash-merge, or rebase `60f1a45` out.**
`85a4fb6` is already open as PR #155 and is unaffected either way.

**The two disclosed-and-deferred items are correctly deferred.**

- `webhooks/stripe.py:209-211, 260-262, 526-527` — bare `PaymentIntent.retrieve`
  at ~240 s (SDK `timeout=80` × `max_network_retries=2`). Under the sync workers
  this commit *keeps*, gunicorn's `timeout=120` bounds it at the cost of a
  worker. Deferring is right; it must be on the flip list, and it is not.
- `worker_connections` unset (default 1000) — read only by threaded/async
  worker classes, so it is inert under sync. The "TO FLIP IT" note already says
  to set it. Correct.

**On the mis-attribution.** I did not repeat it. `blueprints/campaigns.py:238-242`
records a double-submit caused by the route **missing `@idempotent()`**, since
fixed — evidence that real double-submits reach this app, not that
`_claim_key`'s upsert race has fired. That distinction *strengthens* the
sequencing argument rather than weakening it: the decorator's race is unproven
in the wild precisely because the decorator is now present, and the traffic
pattern that would exercise it is demonstrably real. So "the races must land
before the flip" survives losing its cited incident. The commit message should
be corrected anyway — it is the second time this branch's prose has asserted
something the code does not show.

---

## 12. What Phases 2-6 must now assume differently

**Yes, the plan's Phase 1 premise needs rewriting.** Concretely:

- **Phase 1 is not complete and must not be marked complete.** The plan's
  sentence "Today `_INFLIGHT` can never exceed 1, so the 4-slot cap is
  decoration" is still true after this commit, with `2` for `4`. Rewrite Phase 1
  as: *the anonymous cost is now bounded per request (Modal deadline, unmetered
  parse removed, cache key space closed) and the in-process concurrency guard is
  built, tested and pre-sized, but it is inert until the worker class changes,
  which is gated on the `shared/idempotency.py` and `shared/wallet.py` races.*
  Add a Phase 1b: flip the worker class.
- **The builder is right that Phase 1's stated content was partly wrong, but
  for a narrower reason than claimed.** "The box is already bounded at 2 workers
  × wall time" is **true** and is the strongest part of the argument — total
  anonymous concurrency is ≤2 fleet-wide regardless. "Load already queues in the
  kernel backlog" is **true but cuts both ways**: the plan invokes the same
  kernel backlog as the reason an application queue must have a ceiling. Queuing
  invisibly, with no shed, no `Retry-After` and `/healthz` in the same line, is
  not the same as queuing. So **Phase 1's real content is fairness, and fairness
  is still undelivered**, which is the opposite of a reason to close the phase.
- **Phase 4's premise needs the same correction, in the other direction.**
  "Phase 4 doesn't need a semaphore under sync workers" is right that loosening
  the per-IP ceiling cannot raise *concurrency* past 2 — and wrong that this
  makes it safe. Phase 0's budget is a **CPU-time** budget, not a concurrency
  budget. Loosening per-IP to O(100) with 2 sync workers lets anonymous work own
  100% of both workers' wall time for a whole window; a semaphore of 2 with 2
  workers does nothing about time-share either. **What makes Phase 4 safe is a
  worker-count increase, or gthread with `slots < threads`, or route-level
  admission control that reserves capacity for non-anonymous traffic — not the
  presence or absence of this semaphore.** Phase 4 must not cite Phase 1 as its
  safety argument.
- **Size Phase 4 against `/scout/progress`, not "per analysis".** `/progress`
  re-runs `run_pipeline` unconditionally and shares the `scout_analyze` bucket,
  so the adversarial worst case is **~180 CPU-s per IP per window**, and adding
  a `results.csv` check to `/progress` is probably the single cheapest CPU win
  left on this path.
- **`WEB_CONCURRENCY` is a hidden limiter knob.** In-memory counters mean the
  experienced per-IP limit is `workers × 10`. Raising the worker count to buy
  capacity silently doubles the anonymous allowance. Phase 3 removes this
  coupling; until it does, nobody may change `WEB_CONCURRENCY` without
  re-deriving the limit.
- **Phase 6 gets its `reason` field for free and should build on it, not on
  status codes.** `rate_limited` / `busy` / `at_capacity` are declared an API
  surface in `scout/ratelimit.py` and are on every refusal. `shared/metrics.py:292`
  still counts both SSE refusals as 2xx successes, so the counters Phase 6 needs
  must read `reason`, not the status class.
- **Phase 3 does not inherit a live concurrency model** (round 1 said it would;
  that was true of `60f1a45` and is not true of `fb6d13d`). Its counter is hit
  by one request per worker. It must still not hold a lock across I/O — the
  mistake this commit fixed one module over.
- **The `timeout` watchdog is still a request watchdog.** Round 1's
  "treat that assumption as removed fleet-wide" is **reverted**: under sync
  workers `timeout=120` still kills a slow request, and `test_the_timeout_watchdog_is_still_a_request_watchdog`
  now pins it. Every remaining unbounded blocking call in the app is survivable
  again, which is why the Stripe item can wait.
- **Process:** the plan, both Phase 0 documents and round 1's QC are **untracked
  files**, and `docs/qc/anon-ratelimit-phase-1.md` exists only inside the
  temporary worktree `agent-a3d23aed1b18bf18e`. Pruning that worktree destroys
  the evidence this round was written against. Commit them.
- **Unrelated but adjacent, found in passing:** `tmp/calibration/dispatched.json`
  and `tmp/calibration/results.json` are **tracked files under the gitignored
  `tmp/`**. Any reaper or test cleanup that does `rm -rf tmp` deletes tracked
  data — the same shape as the Scout reaper incident. And
  `scout/pipeline.py:693` has imported a function that never existed since
  `3ba4c5d`, so `interface_competition` has been a constant `1.0` in every
  ranked result (§6).

---

*Reviewer note: no application code was modified. All 23 mutations were applied
and reverted inside my own worktree only, each verified landed and each verified
restored byte-for-byte; `git status` clean (0 modified tracked files) at the end.
Harnesses were written to a scratch directory outside the repo and not committed.
No production contact: no forged headers, no rate-limit bucket consumed against
production, no load test against production. The only external requests were
1,500 accession lookups against `rest.uniprot.org`, a public endpoint the code
itself calls.*
