# QC — Anonymous rate limiting, Phase 1 (make the cost bound real)

**Verdict: FAIL — split the branch.**

`85a4fb6` (SAbDab repoint + fan-out removal) **PASSES clean** and is safe to
ship on its own. `60f1a45` (gthread + bounded queue) **must not ship as
written**: it deletes a watchdog that is today the only thing bounding an
outbound call the builder's audit did not cover, and it widens two
pre-existing read-then-act races on money by roughly 12x.

The Phase 1 *deliverable* is well built. The queue, the semaphore and the
repoint are correct, tested, and I reproduced the builder's headline load
result exactly. The failure is not in what was written; it is in the blast
radius of the one-line global change it rides on.

**SHAs reviewed:**
- `85a4fb641a60842c9c98abd6fc25536595c30b76` — repoint the SAbDab lookup, delete the 40-thread fan-out
- `60f1a451ecdff03f3b43a4ac5b49ff802b2d19f4` — gthread workers plus a bounded queue
- Merge base / baseline: `fa938b09fd43aae6fc06976756f5c6fe379a537f`
- Trunk at review time: `origin/main` = `4b7af64d7773a56f32acd8a254f89cd46493f8d2` (unmoved since the task was written; `git merge-tree` still CLEAN)

**Reviewed:** 2026-08-18. Reviewer did not build this phase.

**QC worktree:** `C:\Users\lab\Documents\Claude_projects\tools-hub\.claude\worktrees\agent-a3d23aed1b18bf18e`
(confirmed via `git worktree list`; 13 live worktrees). The builder's worktree
`agent-ab74427286b934161` and the two Phase 0 worktrees were read only through
the shared object store (`git show <sha>:<path>`) — never checked out, written
to, or pruned. Nothing was pushed, merged, or deleted. No production contact of
any kind; the `X-Forwarded-For` resolving experiment was not run.

---

## Contract compliance

| Contract item | Status |
|---|---|
| 1. Pin the SHA reviewed | done, above |
| 2. Suite measured first-hand **both sides**, repo venv, `-m pytest -q` from repo root, no path arg, never piped through `tail` | done, §6 |
| 3. Re-run verification criteria empirically, not by reading the diff | done — real threaded WSGI server, §4 |
| 4. Mutation-test with **my own** mutations, verifying each landed | done — 16 mutations, §5 |
| 5. Answer the goal question in writing | done, §8 |
| 6. Write `docs/qc/anon-ratelimit-phase-<N>.md` | this file |

One correction to my own method, recorded because it would have produced a
spectacularly wrong headline: my first measurement of `detect_interfaces` read
**323 CPU-s** on a 6 MB upload. That was an artifact of *this box*, not
production — `scipy` is in `requirements.txt` (`scipy>=1.11`) but was absent
from the local venv, so the code took its pure-Python brute-force fallback
(`scout/interfaces.py:240-255`) instead of the `cKDTree` path. I installed
scipy 1.18.0 into a scratch directory and re-measured: **0.09-0.64 CPU-s**. The
323 figure appears nowhere in the arithmetic below. It is recorded here only
because it is a live latent hazard — see finding L4.

---

## 1. gthread is a global change and Phase 0's SAFE verdict was Scout-only

Phase 0 scoped its thread-safety audit to the Scout request path and said so.
`worker_class = "gthread"` changes the execution model for **every** route.
I audited the rest of the app independently. The builder's claim — everything
semaphore-bounded except `compute_campaigns.drive_campaign_async` — **does not
hold**.

### 1.1 BLOCKING — the duplicate-submit guard does not guard (`shared/idempotency.py:290`)

I verified this one by reading the code myself rather than accepting it second
hand, because it is the most serious claim in this review.

`_claim_key` does SELECT (`:261-266`) → decide → then:

```python
client.table(_TABLE).upsert(claim_row, on_conflict="key").execute()
```

`INSERT ... ON CONFLICT (key) DO UPDATE` **succeeds for both** concurrent
callers. Neither raises. Both receive `("claimed", None)` and both run the
handler. The comment two lines above it is therefore false as written:

> `# Not claimed (or existing rows are all stale) — claim it. The PK`
> `# guarantees only one of concurrent callers wins.`

A PK guarantees that for a plain `INSERT`. An upsert exists precisely to *not*
do that. The module's own `_store_response` docstring (`:319-321`) already
documents the breach as audit A42, and `blueprints/campaigns.py:238-242`
records the live incident this decorator exists to prevent: *two identical
POSTs → created=2, funded=2*.

`@idempotent()` guards ten money-spending POSTs — `blueprints/campaigns.py:236`,
`:968`; `blueprints/targets.py:505`, `:1195`; `blueprints/tools.py:113`, `:210`,
`:1111`; `blueprints/jobs.py:549`, `:639`; `blueprints/lab_projects.py:1282`.
Losing the race means a second wallet hold and a second Modal GPU job.

**Why Phase 1 makes this a blocker rather than a background debt:** the race
window today is two concurrent request threads, because there are two sync
workers and each serves one request at a time. After this branch it is
2 workers x 12 threads = **24**. The same code, an order of magnitude more
reachable. The correct fix is a plain `.insert()` with the unique violation as
the loser signal — the pattern already used correctly at `webhooks/stripe.py:136`.

### 1.2 BLOCKING — auto-reload gates a real card charge on non-atomic reads (`shared/wallet.py:721-828`)

Read balance (`:743`) → threshold check (`:755`) → 24 h count (`:778`) →
monthly cap (`:781`) → **charge** (`:823`, `create_off_session_payment_intent`).
Both gates count `wallet_transactions` rows that are only written when the
Stripe webhook lands, so the gate is blind for the whole Stripe round trip, and
`billing/checkout.py:546` creates the PaymentIntent with **no
`idempotency_key`**. There is no SQL guard: `auto_reload` appears in the
migrations only as an enum value, two views and column names — no unique index,
no advisory lock.

`_post_settle_hooks:1081` runs on **every** settle, and settles arrive in
bursts from Modal completion webhooks for campaign sub-jobs of one user. Under
gthread x12 that is up to 12 concurrent charges of `auto_reload_amount_usd` for
a single user.

### 1.3 The thread-spawn claim is wrong in both directions

| Site | Bound | Builder's claim |
|---|---|---|
| `shared/compute_campaigns.py:2342` | **none** | disclosed — but `blueprints/targets.py:1423` calls it in a **loop**, one thread per tool per request, which was not disclosed |
| `scout/epitope_db.py:888-895` | **none** — 5 threads/call, `join(timeout=20)` then orphaned | not mentioned |
| `shared/webhooks.py:305-313` | **none** — one DNS thread per validate, orphaned on timeout by design | claimed "DNS joined with a timeout" — the join is bounded, the thread is not |
| `shared/events.py:150` | correct, semaphore(4) before spawn | ✓ |
| `shared/email.py:1548/1622` | correct, semaphore(2) before spawn | ✓ |
| `shared/webhooks.py:734`, `:822` | semaphore acquired *inside* the target, so the thread is created then exits on shed — no pile-up | ✓ in effect |

### 1.4 The 38-threads-per-worker budget assumes every analysis is anonymous

`gunicorn.conf.py` sizes worst-case OS threads per worker at **~38**
(12 + 4x5 contact + 4 event + 2 alert), taking the 4x5 from the four anonymous
compute slots. But `scout/ratelimit.py:201` yields True immediately for
signed-in callers **without consuming a slot**, and the route body runs
`fetch_known_binders` — and therefore its 5 contact threads — regardless of
auth. A worker full of signed-in analyses is 12 x 5 = **60** contact threads,
not 20. Realistic ceiling is **~72 per worker / ~144 fleet-wide**, not 38/76,
plus threads orphaned by `join(timeout=20)`.

This does not by itself sink the change, but the number is stated as a safety
budget and it is understated by ~2x.

### 1.5 Verified clean (so these are not re-opened later)

- **All DB money mutations are atomic.** `try_hold_for_job`, `settle_hold`,
  `release_hold`, `credit_wallet` all `PERFORM 1 ... FOR UPDATE`
  (migrations 0017/0018/0019/0020/0035). `wallet_transactions.stripe_event_id`
  is `UNIQUE` (`0017_wallet.sql:78`), which backstops the racy Python duplicate
  checks at `shared/wallet.py:341` and `:413` — the loser gets an exception,
  not a double credit.
- **Stripe webhook replay gate** (`webhooks/stripe.py:116-144`) is a bare
  insert on a PK. Genuinely atomic.
- **No shared non-thread-safe clients.** `shared/supabase_client.py:218` and
  `shared/credits.py:67` build a client per call. `app.py:783` shares one
  `ModalClient` but its only instance attribute is `self.environment`
  (`gpu/modal_client.py:226`) — stateless.
- **Zero `global` statements** in `blueprints/admin.py`, `auth.py`, `wallet.py`,
  `shared/auth.py`, `shared/credits.py`; no module-level mutable state in any
  wallet/billing/admin/auth module (AST-verified, not grep).
- No `os.environ` writes at request time, no `os.chdir`, no matplotlib /
  `random.seed` / `locale` / `warnings` / `signal` use in the web process.
  `tempfile.NamedTemporaryFile` (`shared/pdb_preflight.py:516`) uses unique
  names and is not on the Scout path.
- Flask `g` is used correctly for request-scoped money state
  (`shared/wallet_guard.py:184-207`, `blueprints/tools.py:1654`).

Low severity: `blueprints/campaigns.py:101,110-117` `_last_status_reconcile` is
an unlocked dict, but it is only a poll throttle — costs extra Modal polls, not
money. `billing/checkout.py:246,257,263` rebinds process-global `stripe.api_key`
/ `max_network_retries` / `default_http_client` at request time; same values
every time so benign today, but `:263` constructs a fresh `RequestsClient` and
rebinds the global while other threads are mid-request. Set these once at import.

---

## 2. The watchdog claim — mechanism CONFIRMED, safety argument FALSE

### 2.1 The gunicorn behaviour is exactly as the builder describes

Verified against the **installed** gunicorn **24.1.1**
(`venv/Lib/site-packages/gunicorn/__init__.py:5`; `requirements.txt:3` pins
`gunicorn>=22.0,<25.0`), not against documentation or memory.

- **sync** — `workers/sync.py:61-62` `while self.alive:` / `self.notify()`, then
  `:69` `accept()` → `:31` `handle(...)` runs the WSGI app **inline on the same
  thread**. `notify()` therefore fires only *between* requests, and a request
  exceeding `timeout` starves it.
- **gthread** — `workers/gthread.py:287-289` `while self.alive:` /
  `self.notify()`, then `:297` `wait_for_and_dispatch_events(timeout=1.0)`, a
  hard-coded 1 s poll. Requests are handed to a `ThreadPoolExecutor` at `:215`
  (`self.tpool.submit(self.handle, conn)`), never the main thread. The only
  callbacks the main loop dispatches (`:219`, `:131`, `:234`) run no WSGI code.
- **arbiter** — `arbiter.py:515`
  `if time.monotonic() - worker.tmp.last_update() <= self.timeout: continue`.

**Conclusion: correct.** Under gthread a request running longer than
`timeout` (here `120`, `gunicorn.conf.py:113`) can never get its worker killed.

Two amplifications the builder did not state. gthread does not merely fail to
kill — it **keeps accepting**: `gthread.py:292`
`can_accept = self.nr_conns < self.worker_connections`, and
`worker_connections` defaults to **1000** (`config.py:760`). With all 12 threads
wedged, the worker accepts up to 1000 more connections into the executor's
queue. No kill, no 503, no backpressure, and `/healthz` queues behind them —
invisible to the arbiter *and* to any liveness monitor.

### 2.2 BLOCKING — the safety argument does not survive leaving `scout/` and `shared/`

The builder's argument is: nothing can wedge, because every outbound HTTP call
in `scout/` and `shared/` passes an explicit timeout, AST-verified. The first
half of that is true and I re-verified it. The wedge is simply not in either
directory, and it is not `requests`, so the scan could not have seen it.

**`gpu/modal_client.py:285-290` — Modal gRPC calls carry no deadline.**

```python
fn = modal.Function.from_name(modal_app_name(tool), "run_tool",
                              environment_name=self.environment)
function_call = fn.spawn(payload)
```

Traced in the installed **modal 1.4.2**, and I read these lines myself:

- `modal/_utils/grpc_utils.py:250-260` — `class Retry` defaults
  `attempt_timeout: Optional[float] = None`, `total_timeout: Optional[float] = None`
- `modal/_grpc_client.py:67,114` — `_DEFAULT_RETRY = Retry()` is applied as the default
- `modal/_utils/grpc_utils.py:375-383` — `timeouts` is built only from those
  two; both `None` → the `else:` branch sets **`timeout = None`**
- `modal/_utils/grpc_utils.py:388` —
  `return await fn_callable(req, metadata=attempt_metadata, timeout=timeout)`

No transport backstop either: `grpc_utils.py:193-196` sets only HTTP/2 window
sizes, and grpclib's client keepalive default is off, so a half-open connection
is never detected. Only the handshake is bounded (`:231`, `attempt_timeout=10.0`,
`total_timeout=63.0`).

**Reachable from request handlers**, traced: `blueprints/jobs.py:529` and
`blueprints/tools.py:1912` → `current_app.modal_client.submit(...)`;
`blueprints/jobs.py:310` → `.poll(...)`. (`gpu/modal_client.py:341`
`fc.get(timeout=0)` bounds Modal's *wait-for-result* semantics, not the gRPC
transport.)

This is the exact failure this codebase has already eaten once — documented in
`shared/supabase_client.py:18-24` for 2026-06-10: Railway egress, idle HTTP/2
goes stale, handshake succeeds, response never arrives. **Sync gunicorn killed
the worker at 120 s and the fleet self-healed. After this branch nothing kills
it.** Twelve job submits against a stale Modal channel take out that worker's
entire thread pool, permanently and silently, and with 2 workers that is half
the fleet.

This is a strict regression *introduced by this branch*, on an authenticated
revenue path. It is the single reason this review is a FAIL rather than a PASS
WITH CORRECTIONS.

**Secondary (P1):** `webhooks/stripe.py:209-211`, `:260-262`, `:526-527` do a
bare `import stripe; stripe.api_key = ...` then `PaymentIntent.retrieve(...)`.
A worker that takes a Stripe webhook *before* any checkout call gets SDK
defaults — `stripe/_http_client.py:580` `timeout=80` x
`stripe/__init__.py:44` `max_network_retries=2` → **~240 s + backoff**, i.e.
bounded, but at 2x the gunicorn timeout that used to kill it.

**Verified genuinely clean:** Supabase is properly bounded
(`shared/supabase_client.py:164-167` PostgREST `httpx.Timeout(30, connect=5.0)`,
`:183-184` storage 30 s, `:37-40` forces HTTP/1.1) and *every* path goes through
it. Every `requests` call in the web process has an explicit timeout —
AST-verified repo-wide, not just `scout/` + `shared/`: `shared/email.py` (6
sites), `shared/webhooks.py:487-491`, `shared/events.py:279`,
`shared/indexnow.py:71`, `shared/pdb_intake.py:59,80`, `scout/epitope_db.py` (6
sites), `scout/routes.py:445`. Zero `timeout=None`. All semaphores are
`acquire(blocking=False)`. The `subprocess.run` calls without timeouts
(`tools/*/run_pipeline.py`) run inside Modal GPU containers, not the web
process. The scanner was break-tested against a 9-construct bad fixture before
being trusted.

---

## 3. HIGH — `_INFLIGHT_LOCK` is held across the shed, and gthread makes that path live

Not raised by the builder, and it is a direct consequence of this commit.

`anon_compute_slot` is a `@contextmanager`. Both shed exits sit **inside**
`with _INFLIGHT_LOCK:` (`scout/ratelimit.py:213-215` and `:229-231`):

```python
with _INFLIGHT_LOCK:
    if _INFLIGHT >= limit:
        if _WAITING >= max_waiting:
            yield False          # <-- suspends HERE, still holding the lock
            return
        ...
        if not got_slot:
            yield False          # <-- and HERE
            return
    _INFLIGHT += 1
```

A `yield` in a context manager hands control to the caller's `with` **body**
and does not return until the body finishes. So the entire shed response is
produced while the process-wide compute lock is held.

On `/scout/analyze` (`routes.py:575-576`) that body is a `jsonify` — short.
On `/scout/progress` it is `routes.py:935-939`, inside the streaming generator:
it **writes an SSE frame to the client socket**. A slow or stalled reader
therefore holds `_INFLIGHT_LOCK` for as long as the write blocks — and that lock
serialises every anonymous slot acquire, every release, and both
`inflight_anon_runs()` / `queued_anon_runs()` in the process.

The `yield False`-under-lock is pre-existing, but it was **unreachable**: under
sync workers `_INFLIGHT` could never reach the cap, which is the builder's own
central argument for why this commit is needed. Phase 1 turns a dead branch into
a live one, and the branch holds a global lock across I/O.

**Confirmed empirically, not by reading.** One thread takes the only slot, a
second thread enters the shed branch (`max_waiting=0`) and holds its `with`
body for 1.0 s, and a third thread probes the lock:

```
shed body held for 1.0s
non-blocking acquire during shed body succeeded: False
blocking acquire waited: 1.00s

>>> CONFIRMED: _INFLIGHT_LOCK IS HELD across the shed body.
```

The blocking acquire waits exactly as long as the shed body runs. Every
anonymous slot acquire and release in the process serialises behind whatever
that body does.

**Related, and worth naming:** the commit changes `_INFLIGHT_LOCK` from
`threading.Lock()` to `threading.Condition()` and says "It is still a lock —
every existing `with _INFLIGHT_LOCK` site works unchanged." True, but
incomplete: `threading.Condition()` with no argument is backed by an **RLock**
(CPython `threading.py`, `Condition.__init__`), so the mutex went from
non-reentrant to **reentrant**. That is what stops `inflight_anon_runs()` from
self-deadlocking if it is ever called from inside a shed body — the reentrancy
masks the defect above rather than fixing it, and it means a future same-thread
re-entry will silently succeed where it used to deadlock loudly.

---

## 4. The adversarial cost, the cache, and the queue arithmetic

### 4.1 The builder is right that cost went UP, and right about roughly how much

Measured on this box, `time.process_time()`, scipy 1.18.0 / numpy 2.5.2 /
biopython present (the same pins Phase 0's QC used):

| `fetch_known_binders`, cold cache | CPU-s | binders |
|---|---|---|
| `P00533` (EGFR) | 3.28 | 3 |
| `P0DTC2` (spike) | 4.19 | 15 |
| `P00698` (lysozyme) | 3.67 | 10 |
| `P01308` (insulin, no SAbDab hits) | 0.28 | 0 |

The builder's "~5-6.2 CPU-s per distinct UniProt target" is the right shape and
slightly conservative; I measure **0.3-4.2**. The old fan-out burned ~1.9 CPU-s
returning nothing, so **the repoint does cost more CPU and buys a working
feature** — the builder's framing is honest and I confirm it.

### 4.2 CORRECTION — the true adversarial single-request cost is ~16 CPU-s, not 9.0

Phase 0's 9.0 CPU-s covered `run_pipeline` only. `POST /scout/analyze` parses
the uploaded structure **three times**. Measured on `1FFK` (5,343,975 bytes,
admissible under the 8,192 KB cap):

| Stage | CPU-s | Inside the compute slot? |
|---|---|---|
| `run_pipeline` | **9.0** (Phase 0, at the cap boundary; freesasa is absent here so this is not my number) | yes |
| `_extract_chain_sequence`, inside `resolve_uniprot_id` — **2nd full parse** | 0.50 | yes |
| `_extract_uniprot_from_dbref` | 0.02 | yes |
| `fetch_known_binders`, uncached | 3.3-4.2 | yes |
| `detect_interfaces` | 0.64 | yes |
| `parse_pdb` (`routes.py:620`) — **3rd full parse** | 0.53 | **NO — outside the slot** |

Scaling the parse-bound terms to the 8,192 KB cap (~1.5x):

```
  9.0  run_pipeline (Phase 0, at the cap)
+ 0.8  second parse, resolve_uniprot_id
+ 4.2  fetch_known_binders, distinct UniProt, uncached
+ 1.0  detect_interfaces
+ 0.8  third parse, OUTSIDE the compute slot
= ~15.8 CPU-s per anonymous analysis on this box
```

**Phase 4 should size against ~16 CPU-s on this box, not 9.0 and not 5-6.2.**
Phase 0's unresolved ~2x production factor (its own finding H) would put that
near ~32 CPU-s in production, but that factor was calibrated from intake wall
time only and I did not re-derive it — treat ~16 as measured and the production
multiplier as still open.

Against Phase 0's ~1,200 CPU-s per 10-minute window, ~16 CPU-s per analysis
gives **~75 analyses per 10 min to saturate the whole box** — i.e. ~37 clicks,
since one analysis is 2 metered hits. That reinforces Phase 0's O(100) ceiling
and rules out O(1,000) even harder than Phase 0 did.

### 4.3 CONFIRMED — the cache is trivially defeatable, and `_CACHE` has no bound

`_CACHE` is keyed on the resolved UniProt accession and has **no maxsize and no
eviction** (`scout/epitope_db.py:82`). Three ways to defeat it, all confirmed:

1. **Distinct real antigens.** Thousands of accessions have SAbDab entries;
   each costs a full uncached lookup (§4.1). No special effort required.
2. **Arbitrary attacker-chosen keys.** `_extract_uniprot_from_dbref`
   (`epitope_db.py:160-165`) returns whatever 8 characters sit in columns 33-41
   of any `DBREF` line whose db field is `UNP`/`SWS`/`TRE` — **no format
   validation at all**. `_validate_and_build(..., must_validate=False)` then
   accepts it *unvalidated* whenever UniProt returns no sequence for it, which
   is exactly what happens for a nonexistent accession. Executed:

   ```
   _extract_uniprot_from_dbref('ZZ9QC001') -> 'ZZ9QC001'
   resolve_uniprot_id           -> {'uniprot_id': 'ZZ9QC001', 'source': 'dbref'}
   _CACHE entries after one bogus accession: 0 -> 1
   ```

   So one request mints one permanent cache entry with a caller-chosen key.
3. The `if uniprot_id:` gate at `routes.py:595` means an anonymous **custom
   upload with no resolvable UniProt does not run the binder lookup at all** —
   the answer to the question as posed. But it still pays the extra full parse
   in `resolve_uniprot_id`, and an attacker *wants* the lookup, so they supply a
   DBREF line.

**Severity, honestly stated:** a miss caches `[]`, roughly ~150 bytes, so the
growth rate is low — millions of requests to reach a gigabyte, which the per-IP
limiter does not permit today. Populated entries are larger but bounded in
practice by the number of real antigen accessions. So this is **unbounded but
slow**: a real defect to fix (a `maxsize` or TTL), not a P0. It gets worse, not
better, if Phase 4 loosens the per-IP ceiling.

### 4.4 The 5-structure cap bounds count, not bytes

`_MAX_CONTACT_STRUCTURES = 5` bounds how many structures get contacts computed.
It does **not** bound their size: `_fetch_and_compute_contacts`
(`epitope_db.py:567-575`) issues `requests.get(url, timeout=12)` with no
`stream=`, no `Content-Length` check and no size cap, then materialises
`resp.text`. The 8 MB anonymous upload cap does not apply to these — they are
RCSB downloads chosen by the *server*. The attacker's control is indirect (they
choose the UniProt; the five are then picked by best resolution) but real: shop
for accessions whose top five are large. Practically bounded near ~10-12 MB by
the `.pdb` format's own 99,999-atom ceiling, so this is a **bounded-but-unstated**
cost, not an unbounded one.

### 4.5 The sizing and the queue

`12 = 4 + 4 + 4` and the 54 s worst case both check out arithmetically:
4 worst-case pipelines at 9.0 CPU-s over ~1.07 effective cores is ~34 s, plus a
20 s queue wait = 54 s as an upper bound on a *served* request. Fine as far as
it goes — but it is built on the 9.0 figure, and §4.2 replaces that with ~16.
Re-derived: 4 x 15.8 / 1.07 = **~59 s** to drain four slots, so the served worst
case is nearer **20 + 59 = ~79 s**, and a waiter that arrives while four
worst-case runs are in flight **cannot be served at all** — the first slot frees
at ~59 s, well past the 20 s wait, so it times out and sheds anyway. The queue
therefore helps ordinary bursts (the typical analysis is ~2 CPU-s, which is the
case the builder argues for and is right about) and does nothing under genuine
worst-case load. That is acceptable behaviour; the numbers in the comments are
just optimistic by ~1.5x and should be restated.

**Barging (low).** A newcomer that finds `_INFLIGHT < limit` takes the slot
directly without ever queueing, and `Condition.wait_for` re-loops when it loses.
`notify()` wakes one waiter, but that waiter must re-acquire a lock CPython does
not make FIFO-fair. Under *sustained* arrivals a queued waiter can be jumped
repeatedly until it times out. It degrades to the pre-Phase-1 shed, so it is not
a safety issue, and it does not affect the burst case (where there are no
newcomers) that motivates the queue.

### 4.6 The load result reproduced, on a real threaded server

Not by reading the semaphore: I stood up the **real app** (`create_app()`) under
a genuinely threaded WSGI server (`werkzeug.serving.make_server(..., threaded=True)`,
one OS thread per request), gave each of 9 callers its own cookie jar, had each
upload a real structure, then fired all 9 `POST /scout/analyze` through a
barrier. Only `run_pipeline`/`resolve_uniprot_id`/`detect_interfaces` were
stubbed, to make the hold time deterministic and keep the test offline; every
decorator, the rate limiter, the slot and the queue are the shipped code.

```
server on http://127.0.0.1:61569  threaded=True  N=9 hold=1.5s
limit slots=4 queue=4 wait=20.0

 idx  kind      status  elapsed_s  body
   5  analyze      503      0.07   Epitope Scout is busy with other free runs right now...
   0  analyze      200      1.56
   1  analyze      200      1.56
   7  analyze      200      1.57
   8  analyze      200      1.57
   4  analyze      200      3.04
   6  analyze      200      3.05
   2  analyze      200      3.05
   3  analyze      200      3.06

200=8  503(shed)=1  429(ratelimit)=0  other=0
AFTER: inflight=0 queued=0
peak observed _INFLIGHT inside pipeline = 4
pipeline entry offsets: 0.03, 0.04, 0.04, 0.05, 1.54, 1.54, 1.54, 1.55
```

**The builder's headline result reproduces essentially exactly**: 4 immediate
(~1.6 s), 4 queued then served (~3.1 s), 1 shed in 0.07 s with the busy message,
peak in-slot concurrency exactly 4, and no slot or queue place leaked. The entry
offsets show two clean waves, one per slot release.

### 4.7 Shed vs 429 — separable on `/analyze`, NOT on `/progress`

| Path | Per-IP limit | Compute shed |
|---|---|---|
| `POST /scout/analyze` | **429** + `Retry-After` (`ratelimit.py:301-305`) | **503** + `_BUSY_MESSAGE` (`routes.py:576`) — verified above |
| `GET /scout/progress` (SSE) | **HTTP 200** `text/event-stream`, `Retry-After` header (`ratelimit.py:284-295`) | **HTTP 200** `text/event-stream`, **no** `Retry-After` (`routes.py:935-939`) |

So Phase 1 improves things on `/analyze` — the shed is a 503 and is cleanly
separable from the 429, which is what Phase 6 needs. But on the SSE route
**both refusals are HTTP 200**, distinguishable only by body text and by the
presence of `Retry-After`. `shared/metrics.py:292` records
`REQUESTS_TOTAL.labels(route, status_class)`, so both SSE refusals are counted
as **successes**, and neither refusal path increments any refusal counter
anywhere. Phase 0 found two 429s conflated; Phase 1 adds a third refusal that is
invisible at the status-code layer. Note also that `503` is now shared between
the compute shed and `_anon_capacity_error`'s global live-job cap
(`routes.py:221`), with different bodies.

---

## 5. Mutation testing — my own mutations, 16/16 landed and RED

I did not rely on the builder's self-assessment. I wrote 16 mutations, and each
one is **verified to have landed** before its result is believed: the exact old
text must appear exactly once, the write must change the file bytes, and the new
text must be present afterwards. All file I/O is explicit `encoding="utf-8"` —
this repo has had a mutation silently fail on a Windows encoding mismatch over
an em-dash, and these files are full of them.

Baseline unmutated: `test_scout_anon_concurrency.py` 15 passed;
`test_scout_epitope_db_sabdab.py` 21 passed, 1 skipped.

| # | Mutation | Landed | Result |
|---|---|---|---|
| M1 | queue bound removed (`if _WAITING >= max_waiting` → `if False`) | ✓ | RED |
| M2 | default frozen at import instead of resolved at call time | ✓ | RED |
| M3 | `_WAITING -= 1` dropped from the `finally` (queue place leaked) | ✓ | RED |
| M4 | `_INFLIGHT_LOCK.notify()` removed | ✓ | RED |
| M5 | signed-in callers made to consume a slot | ✓ | RED |
| M6 | slot not released (`_INFLIGHT = _INFLIGHT`) | ✓ | RED |
| M7 | `worker_class = "sync"` | ✓ | RED |
| M8 | `threads` 12 → 8 (no headroom over 4+4) | ✓ | RED |
| M9 | `max(1, ...)` floor removed from `threads` | ✓ | RED |
| **M10** | **backoff re-gated on `_SUMMARY_INDEX is not None`** | ✓ | **RED** |
| M11 | required-column check disabled | ✓ | RED |
| M12 | zero-row parse accepted | ✓ | RED |
| M13 | miss cached even while upstream is down | ✓ | RED |
| M14 | error TTL raised to the 24 h success TTL | ✓ | RED |
| M15 | thread fan-out reintroduced in `query_sabdab` | ✓ | RED |
| M16 | URL reverted to the retired webapp endpoint | ✓ | RED |

**16 landed, 16 RED, 0 green, 0 failed to land.** The working tree was restored
(`git checkout --`) after every mutation and verified clean at the end.

**M10 is the one the task singled out** — the error-backoff the builder admits
initially certified false. It is genuinely fixed. `_sabdab_summary_index`
(`epitope_db.py:709`) gates on `time.monotonic() < _SUMMARY_EXPIRES_AT`
**alone**; re-adding the `_SUMMARY_INDEX is not None` conjunct — which reads as
equivalent and is not — goes red. And the guard that catches it,
`test_a_dead_upstream_is_not_refetched_on_every_lookup:340-364`, **counts
requests** (`assert len(calls) == 1`), it does not inspect a timestamp. That is
the right shape of test and it does the job. The separate TTL-value test
(`:327-338`) inspects the expiry, but it is testing the TTL constant, not the
backoff behaviour, so that is appropriate.

**Commit-message accuracy for `85a4fb6`** (the builder admits a previous message
asserted a property the code lacked). Every checkable claim holds:

| Claim | Verified |
|---|---|
| "~11.7 MB of CSV but ~1.2 MB on the wire" | ✓ exactly — 11,694,156 bytes decoded, `Content-Length: 1256258` gzipped |
| "11,458 PDB entries" | ✓ exactly |
| "21,914 rows" | ✓ exactly |
| "all 21,914 rows match `pdb_0000` plus four characters" | ✓ — 0 failures across all 21,914 |
| "EGFR 0 → 3 binders, lysozyme 0 → 10" | ✓ exactly (P00533 → 3, P00698 → 10) |
| "backoff keyed on the expiry ALONE … verified by counting requests" | ✓ — code and test both, M10 red |
| "22 guards" | ✓ — 21 passed + 1 skipped |
| "~6.7 MB resident" | ✗ **I measure 10.89 MB** (deep `sys.getsizeof` walk, shared objects counted once) — ~1.6x understated |
| "cold build ~2.0 s" | ✗ ~3.5 s here (3.2 s fetch + 0.25 s parse); network-dependent, minor |

---

## 6. Suite baseline, measured first-hand on both sides

Repo venv `C:\Users\lab\Documents\Claude_projects\tools-hub\venv\Scripts\python.exe`
(Python 3.13.0 — there is no venv in the worktrees), `-m pytest -q` from the
repo root, **no path argument**, output redirected to a file and read with
`tail` afterwards — never piped through `tail`, so nothing could truncate the
run.

| Side | SHA | Result |
|---|---|---|
| Base | `fa938b0` | **5029 passed, 20 skipped** in 439.62 s |
| Branch | `60f1a45` | **5066 passed, 21 skipped** in 516.45 s |

Delta: **+37 passed, +1 skipped**, no failures, no errors either side.

**Both numbers match the builder's report exactly** (base 5029/20, branch
5066/21). The extra skip is the network-gated live-endpoint guard in
`tests/test_scout_epitope_db_sabdab.py`. No flakes were seen in either run —
the two node tests known to flake under load did not fire, so there was nothing
to chase.

---

## 7. Caches and leaks

| Claim | Verdict |
|---|---|
| `_SUMMARY_INDEX` bounded, ~6.7 MB resident | **bounded ✓, size ✗** — it is the whole upstream database (11,458 entries / 21,914 rows), so an attacker cannot grow it at all; but I measure **10.89 MB**, ~1.6x the claim. x2 workers ≈ 22 MB. |
| One fetch per worker per 24 h | ✓ — expiry-keyed (`epitope_db.py:709`), `_SUMMARY_TTL_SEC = 86400`, and M10 confirms a dead upstream costs 1 request per error-TTL, not one per call |
| `_CACHE` still unbounded | ✓ confirmed — no `maxsize`, no eviction, no TTL (`epitope_db.py:82`) |
| Attacker can grow `_CACHE` | ✓ confirmed by execution — see §4.3; ~150 bytes per bogus key, so unbounded but slow |
| Wire size 1.2 MB gzipped, not 14.7 MB | ✓ **the correction is right, and both figures are true of different endpoints** — 14.7 MB was Phase 0's `/api/rcsb-pdb-annotations` JSON; this branch uses `/api/download/all-summary`, measured `Content-Length: 1256258` (1.20 MB) gzipped / 11,694,156 bytes decoded |

Under gthread `_CACHE` is now shared across 12 threads rather than serialised.
It is correctly guarded by `_CACHE_LOCK` for mutation, so there is no corruption.
There is a benign check-then-act: two threads missing the same key both compute
it and both write (`epitope_db.py:858-900`) — duplicate work, not a wrong
answer, bounded by the 4 compute slots. Worth a note, not a fix.

**L4 (latent).** `scout/interfaces.py:240-255` falls back to a pure-Python
`O(n x m)` double loop when `scipy` cannot be imported. I measured that fallback
at **323 CPU-s** on a 6 MB structure versus **0.64 CPU-s** on the `cKDTree`
path — a ~500x cliff, inside the compute slot, triggered by an import failure
rather than by input. `scipy>=1.11` is in `requirements.txt`, so production takes
the fast path today and **this is not a current production number**. But before
this branch a request that hit the fallback exceeded `timeout` and gunicorn
killed the worker; after it, four such requests hold all four compute slots for
20+ minutes and nothing intervenes. The fallback should either be deleted (let
the import error surface) or bounded.

---

## 8. The goal question

### Can six researchers behind one NAT all use the tool in the same afternoon?

**Yes across an afternoon; and inside a shared 10-minute window Phase 1 has NOT
moved the answer.** Phase 0 found six were fine across an afternoon but broke at
the 2nd-3rd analysis inside any 10-minute window. That is still exactly true.

Phase 1 did not touch the per-IP limiter. `ANON_ANALYZE_LIMIT` is still 10 per
worker per 600 s, `/scout/analyze` and `/scout/progress` still both carry the
decorator, so one analysis still costs **2 metered hits**, and the wall still
lands on request 11 per worker. Six researchers doing one beeline analysis each
is still 12 analyze hits against ~20 fleet-wide and still only fits under
perfect load balancing; the thorough one still exceeds a worker's allowance
single-handedly.

> **First place it breaks, unchanged from Phase 0: the `scout_analyze` bucket,
> at the 2nd-3rd concurrent researcher, or the 1st thorough one.**

What Phase 1 *did* improve is the failure mode **once past the limiter**: a
burst of concurrent analyses now queues instead of being refused instantly
(§4.6, 4 served immediately and 4 more served after queueing where previously
four of those would have been 503s), and one analysis no longer monopolises a
whole worker process, so `/healthz` and signed-in routes stay responsive during
anonymous load. Those are real gains for the lab-meeting scenario. They are just
gains *behind* the wall, and the wall is where the six researchers actually
stop. Moving it is Phases 3-4's job.

### Is one attacker still bounded?

**Yes — bounded, and by roughly the same amount as before, but for a worse
reason, and the fleet's failure mode is now worse.**

The CPU bound per IP is unchanged in structure (~20 metered hits per 10 min per
IP) but the per-request cost is ~16 CPU-s rather than the 9.0 Phase 0 assumed
(§4.2), so one IP can demand ~320 CPU-s against ~1,200 available — ~27% of the
box, comparable to Phase 0's 15-30%. Phase 1 adds a genuine second bound that
did not previously exist: at most 4 concurrent anonymous pipelines per worker,
8 fleet-wide, verified live in §4.6. That is the phase's central claim and it
is true.

> **First place it breaks: not the anonymous path at all — it is
> `gpu/modal_client.py:285-290`.** The attacker who matters is no longer the
> anonymous one. Removing the sync-worker watchdog (§2.2) means a stale Modal
> gRPC channel now wedges request threads permanently instead of self-healing at
> 120 s, and the worker keeps accepting up to 1000 more connections while doing
> it. That needs no attacker at all — it is the 2026-06-10 incident this repo
> has already had once, with its recovery mechanism removed.

Second place: `shared/idempotency.py:290`, where the widened race lets a
double-submit place two wallet holds and two GPU jobs (§1.1).

---

## 9. Is this safe to open as a PR?

**`85a4fb6` — yes, on its own, today.** It is bisect-safe (zero `gthread`
references in its diff), strictly reduces per-request cost and thread count,
restores a feature that had been silently dead, and closes the silent-failure
pattern that let it rot. Its 22 guards survive 7 of my mutations. Its commit
message is accurate on every claim I could check. Ship it.

**`60f1a45` — not as written.** Three things must land first or with it:

1. **Bound the Modal gRPC calls** (`gpu/modal_client.py:285-290`, `:337`, `:383`).
   Pass an explicit `Retry(total_timeout=...)`, or wrap the three entry points
   in one bounded-thread helper — `shared/webhooks.py:283-313` already has that
   pattern to copy. **This is the blocker.** Without the sync-worker kill,
   nothing else bounds it.
2. **Make the idempotency claim atomic** (`shared/idempotency.py:290`): plain
   `.insert()`, treat the unique violation as the loser signal, as
   `webhooks/stripe.py:136` already does. Fix the false comment at `:279-280`
   either way.
3. **Release `_INFLIGHT_LOCK` before yielding False** (§3) — restructure so the
   shed decision is made under the lock and the `yield` happens outside it.

Worth doing in the same PR, cheaply: correct the ~38-thread budget to ~72 (§1.4),
restate the 34 s / 54 s figures against ~16 CPU-s (§4.5), and give `_CACHE` a
`maxsize` (§4.3). `auto_reload_if_needed` (§1.2) is pre-existing and needs its
own PR, but it should not be left open indefinitely once gthread is live.

A reasonable alternative, if the Modal fix is not wanted in this branch: land
`85a4fb6` now and hold `60f1a45` until the three items above are done. The two
commits are cleanly separable and the first is the one with immediate value.

---

## 10. What Phases 2-6 must now assume differently

- **Phase 4 sizing input changes from 9.0 to ~16 CPU-s** per adversarial
  anonymous analysis (§4.2). Saturation is ~75 analyses per 10-minute window,
  not ~133. Phase 0's O(100) per-IP ceiling survives; O(1,000) is now ~15x
  worse than Phase 0 already judged it.
- **The per-request cost is no longer dominated by `run_pipeline`.** Three full
  parses of the upload plus the binder lookup are roughly two-thirds of it, and
  one of those parses (`routes.py:620`) is **outside** the compute slot, so the
  semaphore does not bound the whole request. Any later phase reasoning about
  "the slot bounds anonymous CPU" must account for that.
- **The cache is not a defence.** Phase 4 must not assume the binder lookup is
  amortised: an attacker mints a fresh cache key per request with an unvalidated
  DBREF line (§4.3). Validating the accession format in
  `_extract_uniprot_from_dbref` is a cheap, separate win.
- **Phase 6 gets one of its two asks for free and a new problem.** The compute
  shed is a 503, cleanly separable from the per-IP 429 on `/scout/analyze`. But
  both refusals on `/scout/progress` are HTTP 200 and are therefore counted as
  successes by `shared/metrics.py:292` (§4.7). Refusal-rate alerting cannot be
  built from status codes on the SSE route; it needs explicit counters.
  `503` is also now shared between the compute shed and the live-job cap.
- **Phase 3 inherits a live concurrency model.** Its shared counter will be hit
  by 12 threads per worker, not 1. Whatever it builds must be thread-safe in
  process as well as correct across workers — and it must not hold a lock across
  I/O, which is the mistake in §3.
- **The gunicorn `timeout` is no longer a backstop for anything.** Any later
  phase that reasons "worst case, the worker gets killed and we recover" is
  wrong from this branch onward. That assumption should be treated as removed
  fleet-wide, not just for Scout.
- **Phase 2 is unaffected by this branch** and its Phase 0 blockers still stand
  (the `/scout/` probe targets an unmetered route; use `/scout/example`).

---

*Reviewer note: no application code was modified. Mutations were applied and
reverted in this worktree only, verified clean afterwards. Measurement harnesses
were written to a scratch directory outside the repo and not committed. scipy
was installed into a scratch directory, not into the shared venv. No production
contact: no forged headers, no rate-limit bucket consumed, no load test against
production. The only external requests were to `sabdab.opig.stats.ox.ac.uk`,
`files.rcsb.org`, `search.rcsb.org` and `rest.uniprot.org` — the same public
endpoints the code itself calls.*
