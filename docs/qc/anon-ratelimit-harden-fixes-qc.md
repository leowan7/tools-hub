# QC — `fix/anon-ratelimit-harden-fixes` (`84aed72`, `353fbcd`)

Independent review. I did not write the code, and I did not write the QC round that
prompted it. Reviewed in my own detached worktree at
`scratchpad/qc-fixes` (HEAD `353fbcd`, tree clean before and after every mutation).
The author's worktree `scratchpad/fix-harden` and every other worktree were untouched.

Throughout: **MEASURED** means I executed it and am quoting the output. **READ**
means I read the code and it looks right — it does not count as verification.

---

## Verdict

**PASS WITH FINDINGS.**

The headline claim is real and I reproduced it independently, through gunicorn's own
HTTP parser rather than through a test-client approximation. Every mutation in the
author's table reproduces, six of seven at byte-identical deltas. Enforcement is
unchanged and is genuinely pinned — I broke it and tests went red. The suite counts
in F6 are correct; I measured all three commits from scratch.

Five findings, none of them a blocker for what this commit set out to do. The most
substantial (F-A) is that the new Prometheus export — the fix for "nothing outside
this process can read a module global" — does not actually render under the gunicorn
configuration this app deploys with. That defect is pre-existing and affects every
counter in `shared/metrics.py` equally, but it is the load-bearing claim of this
commit's second half, and it was verified on the dev path rather than the deployed one.

---

## Baselines I measured

Full suite, repo root, no path argument, `venv/Scripts/python.exe -m pytest -q`,
each in its own clean detached worktree, each run in the **foreground**:

| commit | result |
|---|---|
| `17a392a` (grandparent) | **5481 passed / 21 skipped** |
| `84aed72` (parent) | **5485 passed / 21 skipped** |
| `353fbcd` (tip) | **5489 passed / 21 skipped** |

That is exactly F6's corrected table, and exactly the tip count claimed by `353fbcd`.
The +4 is this commit's four new tests.

Scoped baselines at the tip: `tests/test_scout_anon_charge_pairing.py` = 41 passed;
`-k scout` = 245 passed / 4 skipped (the parent's 241/4 plus the same four).

> Methodology note against myself: two runs I launched with `run_in_background` did
> not honour their own `cd` and reported the wrong tree's number. Every figure above
> is from a foreground run whose worktree HEAD and `git status --porcelain` I printed
> immediately before it.

---

## 1. Is `84aed72` really message-only? — MEASURED, YES

```
git diff --quiet a45252f 84aed72   -> exit 0
a45252f^{tree} = 97adce7e5c06446c7b0f1c3565aed9f937bda8c1
84aed72^{tree} = 97adce7e5c06446c7b0f1c3565aed9f937bda8c1
a45252f^ = 17a392a…   84aed72^ = 17a392a…
```

Identical tree object, identical parent. Nothing was smuggled in. The message diff is
the 5160/5164 → 5481/5485 correction plus a new closing paragraph explaining that the
absolutes are post-rebase tree properties and that `4afb6ff` no longer resolves.

## 2. Tree integrity — MEASURED, COHERENT

The reported mid-flight `git branch -D` left no damage that I can find:

* `c749480..353fbcd` is 5 commits, **0 merge commits**, linear; `merge-base` is exactly
  `c749480`.
* Every commit's tree object reads back cleanly (`git cat-file -p <c>^{tree}`).
* `a45252f..353fbcd` touches exactly the three named files and nothing else.
* AST scan of all three changed files: **no duplicate and no missing top-level
  definitions** (`scout/ratelimit.py` 18, `shared/metrics.py` 24, the test file 67).
  This is the check the repo's "rebuilt a file from one side's blob and lost a whole
  function" incident calls for.
* `_note_unmetered_body` has exactly one definition (`scout/ratelimit.py:382`) and
  exactly one call site (`:416`), and the call site passes the new keyword — a
  half-applied edit here would be an instant `TypeError`.
* `ruff check` on all three files: **All checks passed**.
* Full suite green at the tip.

## 3. The headline A/B — MEASURED, REPRODUCED AND REVERSED

I drove the Flask app as a raw WSGI callable using environs built by **gunicorn
24.1.1's own `http.message.Request` parser and `http.wsgi.create()`**, so the framing
is production's framing, not werkzeug's test-client approximation. One bodiless
`POST /scout/analyze`, then 25 genuine chunked POSTs, counting
`scout.ratelimit` WARNING records:

| | bodiless probe | then 25 chunked POSTs |
|---|---|---|
| `84aed72` (parent) | **1 log record** | **0 log records** |
| `353fbcd` (tip) | **0 log records** | **1 log record** |

Byte-for-byte the table in the commit message. The bodiless probe returns 400 on both
commits, as claimed. The alarm was spent by noise; it no longer is.

## 4. Does the FIRST occurrence still log? — MEASURED, YES, AND IT IS PINNED

`if chunked and count % _LOG_EVERY == 1` fires at count 1, 101, 201… The first
chunked body logs immediately; there is no 99-request silent window at the start.

I did not take that from reading. Mutation **MY** flips `== 1` to `== 0` (a ±0 B,
one-character change, so the sample fires at 100 and never on the first request):
**4 tests RED**. The property is guarded.

`reset()` zeroes both counters, so the sample re-arms on the next request — pinned by
the re-arm test and confirmed by M3. The noise path does not advance the sample:
`unmetered_chunked_bodies` only moves when `chunked` is true — pinned by M4.

## 5. Enforcement — MEASURED, UNCHANGED AND PINNED

Mutation **MX** (+6 B): on the unmeasurable-length path, `return ""` → `return source()`
— i.e. fail OPEN, the exact regression the whole feature exists to prevent.
**2 tests RED** (`…cannot_redeem_a_credit_and_says_so`,
`…does_not_parse_a_body_of_unknown_length`).

Independently, my gunicorn-environ probe ran the real `_metered_job_id` over six
framings — bodiless, real chunked, `TE: identity`, `TE: gzip`, mixed-case header,
`CL: 0` — and it returned `''` for **every** framing that reaches the branch. Only the
reporting was split. This is a genuine PASS on the strictest criterion in the brief.

---

## Reproduction of M1–M7

Scoped to `tests/test_scout_anon_charge_pairing.py` (41 at baseline). Every mutation
applied with binary read/replace/write on this CRLF tree, re-read from disk to prove
it landed, byte delta printed, `git reset --hard HEAD` and
`git status --porcelain` empty between each.

| Mut | change | my delta | author's delta | claimed | **MEASURED** | holds? |
|---|---|---|---|---|---|---|
| M1 | counter capped at 1 | +60 B | +34 B | 3 new tests RED | **4 RED** (all of `TestTheAlarmItself`) | YES (author under-claimed; my spelling caps both counters, hence the 4th) |
| M2 | WARNING every request | −28 B | −28 B | 2 RED | **2 RED** | YES |
| M3 | back to the `count == 1` latch | −13 B | −13 B | 1 RED (re-arm) | **1 RED** — `test_the_warning_re_arms_instead_of_latching_off_for_the_deploy` | YES |
| M4 | framing ignored (`chunked=True`) | −34 B | −34 B | 1 RED (noise) | **1 RED** — `test_a_bodiless_post_cannot_spend_the_chunked_signal` | YES |
| M5 | Prometheus `inc()` deleted | −86 B | −86 B | 2 RED (export assertions) | **2 RED** | YES |
| M6 | finalise re-runs pipeline (`if True:`) | −24 B | −24 B | new RED, **old GREEN** | new test **RED**; old test file **RED too** | **NO** — see F-D |
| M7 | length rule reaches the view | +42 B | +42 B | new RED, old GREEN | new test **RED**; old test file **37/37 GREEN** | YES |

Six of seven deltas match the author's table exactly, which is strong evidence the
mutation work was actually run rather than narrated.

**The prior round's blind spot, reproduced on the parent tree itself:** on `84aed72`'s
own source and tests, the +26 B "counter capped at 1" mutation and the +8 B "warn on
every request" mutation are both **GREEN (37 passed)**. The blind spot the previous QC
reported is real, and this commit's new tests kill both (M1 → 4 RED, M2 → 2 RED).

**Cross-check that the new tests are what does the killing:** with `84aed72`'s
unchanged test file against the branch's source, M4, M5 and M7 are all **37/37 green**.
Those three properties were previously unguarded and now are.

**My own extra mutations** (not in the author's table):

| Mut | change | delta | result |
|---|---|---|---|
| MX | enforcement fails OPEN on the unsized path | +6 B | **2 RED** — enforcement is pinned |
| MY | sample skips the first occurrence (`== 0`) | ±0 B | **4 RED** — first-fire is pinned |
| MZ | the two labels swapped | ±0 B | **2 RED** — label mapping is pinned |
| MW | `_LOG_EVERY` 100 → 20000 | +2 B | **41 GREEN — survives**, see F-E |

---

## Findings

### F-A — MEDIUM. The new export does not render under the gunicorn config this app deploys with

> **[Superseded — note added when this report was landed, 2026-08-20. The report
> below is the snapshot as written; nothing in it has been edited.]** This finding
> is CLOSED. `2422bd1` (#167) set `PROMETHEUS_MULTIPROC_DIR` before `--preload`
> imports the app, which is the fix. The `gunicorn.conf.py:178-192` cited below now
> resolves to the comment explaining it. Do not re-open this from this report.
> `/metrics` is still 403 for everyone because `METRICS_ALLOWED_CIDR` is unset —
> that is a separate, still-open issue, and Phase 6 owns it.

`shared/metrics.py:150`, `gunicorn.conf.py:178-192`, `nixpacks.toml:23`.

The commit's second headline is *"READABLE: `unmetered_bodies` was read by NOTHING …
nothing scrapes a module global … Verified rendering on `/metrics`."* That verification
is on the single-process path, not the deployed one.

Boot order, **READ** from `gunicorn/arbiter.py`: `Arbiter.__init__` calls `self.setup(app)`
(line 61), and `setup` performs the preload import at line 121; `Arbiter.start()` calls
`self.cfg.on_starting(self)` at line 141 — after `__init__` has returned. So the app,
and every `Counter` in `shared/metrics.py`, is imported **before** `on_starting`
(`gunicorn.conf.py:181-192`) sets `PROMETHEUS_MULTIPROC_DIR`.

**MEASURED**: `prometheus_client` fixes `values.ValueClass` at import time
(`values.py:128-139`) — env var absent → `MutexValue`; present → `MmapedValue`. And
**MEASURED** end to end, simulating that exact order (import first, set the var second,
then serve `/metrics` as a forked worker would):

```
ValueClass at import: <class 'prometheus_client.values.MutexValue'>
multiproc dir files: []
/metrics rendered length: 0        <-- zero bytes, no counters at all
CONTROL (no multiproc dir):  tools_hub_scout_unmetered_bodies_total{framing="chunked"} 1.0
                             tools_hub_scout_unmetered_bodies_total{framing="no_body"} 2.0
```

Failure scenario: an operator adds the alert this commit asks for, the alert never
fires, and the "outage that does not look like one" stays exactly as invisible as it
was — because `/metrics` in a worker takes the multiprocess branch
(`shared/metrics.py:276-279`, the env var *is* set by then, inherited from the master)
and reads an empty directory.

Scope, honestly: **pre-existing and not introduced here.** `SCOUT_RUNS`,
`CREDITS_SPENT`, `REQUESTS_TOTAL` and the rest are affected identically. Nothing about
the new counter is wrong. But the claim that the number is now readable in production
rests on a path I measured as empty.

**Cannot verify**: whether Railway's deployment environment sets
`PROMETHEUS_MULTIPROC_DIR` externally. If it does, the import-time decision goes the
other way and this finding does not apply. No file in the repo sets it (only
`gunicorn.conf.py` reads it with a `/tmp/prom` default and writes it in `on_starting`),
and I cannot see the Railway dashboard. Separately, `prometheus_client` documents
`--preload` as unsupported with multiprocess mode even when the var *is* set
(pre-fork counters take the master's pid file); I did not measure that.

### F-B — LOW-MEDIUM. The `shared/metrics.py` comment re-commits the over-claim F2 just removed

`shared/metrics.py:143-144`:

> Alert on the `chunked` label: sustained, it means the edge is re-framing every POST
> and anonymous capacity has halved.

**MEASURED false.** `_metered_job_id` runs at `scout/ratelimit.py:802`, *ahead* of both
limiter tiers and every refusal. I sent 25 chunked `POST /scout/analyze` for a
nonexistent job:

```
statuses: 8 x 404, then 17 x 429
chunked metric delta: 25.0
ratelimit.unmetered_chunked_bodies: 25
```

All 25 incremented `{framing="chunked"}`. No analysis ran, nobody was charged twice,
capacity did not halve. F2 removed exactly this over-claim from the WARNING text and
from `ratelimit.py`'s comment — and the newly written `metrics.py` comment restates it
one file over, as an **alerting instruction**, which is the place an operator will
actually act on it. Combined with F-C, that alert is attacker-triggerable from a
~90-byte request.

The WARNING text itself is correct after F2 ("What that cost depends on what happened
next"). It is only the metrics.py comment that reverts.

### F-C — LOW. The discriminator tests header *presence*; four non-chunked TE values get labelled `chunked`

`scout/ratelimit.py:416` — `chunked="Transfer-Encoding" in request.headers`.

**READ**, `gunicorn/http/message.py:226-247`: `set_body_reader` accepts `identity`,
`compress`, `deflate` and `gzip` as `Transfer-Encoding` values without `chunked`, and
rejects everything else with `UnsupportedTransferCoding`. None of those set a
Content-Length, so they fall to `EOFReader`.

**MEASURED** through gunicorn's real parser + `wsgi.create()`, then the real
`_metered_job_id` in a real request context:

| case | CL | TE hdr | returns | chunked count | logs |
|---|---|---|---|---|---|
| bodiless POST (scanner) | None | False | `''` | 0 | 0 |
| real chunked POST | None | True | `''` | 1 | 1 |
| **`TE: identity`, ZERO-byte body** | None | True | `''` | **1** | **1** |
| **`TE: gzip`, ZERO-byte body** | None | True | `''` | **1** | **1** |
| `TE:` (empty value) | rejected by gunicorn (`UnsupportedTransferCoding`) | | | | |
| `tRaNsFeR-eNcOdInG: chunked` | None | True | `''` | 1 | 1 |

So a request carrying no body at all is counted as `chunked` and fires the WARNING.
The comment at `scout/ratelimit.py:359-362` describes a stronger property — "a chunked
one leaves Content-Length unset and Transfer-Encoding set" — than the code implements.

Impact is bounded and I want to be fair about it: **enforcement is unaffected** (every
row returns `''`), and an attacker who wants the `chunked` label can simply send real
chunked framing, which is indistinguishable from an edge re-framing by construction.
What it does establish is that the `chunked` label is *caller-chosen*, which is what
makes F-B's alerting instruction unsafe. Lazier and stricter in the same number of
characters: `"chunked" in request.headers.get("Transfer-Encoding", "").lower()`.

Two of the commit's own claims came out **verified by execution rather than by reading**
in the process: mixed-case headers are handled (gunicorn uppercases, werkzeug's
`EnvironHeaders` is case-insensitive), and `Content-Length` together with
`Transfer-Encoding: chunked` is rejected by gunicorn with `InvalidHeader`
(`message.py:257-259`).

### F-D — LOW. M6 was not green on the parent's test file

Commit `353fbcd`, F7: *"Two mutations, each GREEN on the parent's own test file and RED
here."* True for M7. **MEASURED false for M6**, twice:

```
84aed72's test file + branch source + M6 (-24 B, byte-identical to the author's)
 -> 1 failed, 36 passed
FAILED …::test_a_whitespace_padded_job_id_still_redeems_its_own_credit
tests/test_scout_anon_charge_pairing.py:684:
    assert stub_pipeline == [job_id], "the finalise path re-ran the pipeline"
```

A sibling test in the same class already pinned "the finalise does not re-run the
pipeline". The new assertion is still worth having — it pins the property in the test
that *names* it and gives a better failure message — but it did not close an unguarded
hole, and the mutation table says it did. Severity is low; the concern is the class of
error, in a repo that tracks "guards that certify false" and "scope a claim to the
evidence that made it".

### F-E — LOW. `_LOG_EVERY`'s magnitude is not pinned; the latch is one constant away

**MEASURED**: `_LOG_EVERY = 100` → `20000` (+2 B) leaves **all 41 tests green**, because
both sampling tests read `ratelimit._LOG_EVERY` rather than a literal. Set it to
10,000,000 and the second sample never arrives in a worker's lifetime — which is
precisely the once-per-deploy behaviour this commit exists to remove, with a fully
green suite.

Mitigated, and I do not think this is a blocker: the FIRST occurrence always logs
whatever the constant is, and that *is* pinned (mutation MY, 4 RED), so the alarm can
never go fully silent the way the latch could. Adapting to the constant is also the
right call for the *shape* of the test. If it is worth closing, one line in the re-arm
test — `assert ratelimit._LOG_EVERY <= 1000` — covers it.

### F-F — INFORMATIONAL. A historical measurement was renumbered

`84aed72`'s F6 correction rewrote `a45252f`'s "QC measured it on a real server … with
ALL 5,160 TESTS GREEN" to "ALL 5,481 TESTS GREEN". That measurement was taken on the
pre-rebase tree, which had 5,160 tests; nobody ever ran 5,481 tests under that
mutation. The commit's new closing paragraph does disclose that the absolutes are
post-rebase tree properties, so this is disclosed rather than hidden — but the sentence
now attaches an unmeasured number to a specific past event. The other two rewritten
figures (5485 for this commit's tree, 5481 for its parent) are correct; I measured both.

---

## Things I attacked that came out clean

* **Prometheus stub path** (`prometheus_client` absent) — MEASURED with the import
  blocked: `PROMETHEUS_AVAILABLE = False`, and
  `SCOUT_UNMETERED_BODIES.labels(framing=…).inc()` no-ops without raising. The `_Stub`
  at `shared/metrics.py:76-97` handles the keyword form.
* **Label cardinality** — bounded at exactly 2. One call site, one ternary on a `bool`;
  no caller-supplied string ever reaches `.labels()`.
* **Metric name** — `Counter("tools_hub_scout_unmetered_bodies_total", …)` renders as
  `tools_hub_scout_unmetered_bodies_total{framing="…"}` (prometheus_client strips and
  re-adds `_total`), consistent with every sibling counter. MEASURED in the control
  render above. The test helper asserts the literal name, so renaming goes red.
* **Locking** — `unmetered_bodies`/`unmetered_chunked_bodies` are mutated under `_LOCK`;
  `count` is read inside it and the log write happens outside it, which is correct
  (no I/O under the lock) and cannot double-log or drop a sample, since each count
  value is handed to exactly one caller. Workers are sync today anyway.
* **`gunicorn.conf.py` sets no `max_requests`** — MEASURED by grep. The "once per
  process meant once per deploy" premise is true.
* **`reset()` is test-only** — no caller outside `tests/`.
* **Test-order fragility** — the `TestTheAlarmItself` tests leave the module counters
  non-zero, but every `client`-based test resets in its fixture, `pytest_randomly` is
  not installed, and no other file reads those globals. Not a flake risk.
* **Author's stated limitation is real** — gunicorn genuinely cannot run here:
  `import gunicorn.http.message` fails on `fcntl`, then `grp`, then `os.geteuid`. I got
  its *parser* running with shims for those three (the parser calls none of them),
  which is how the environ tables above are real rather than simulated. The worker
  model, fork and multiprocess mmap behaviour remain unexecuted.
* **The `_meter_one_body` test helper is faithful** on the two variables the code
  branches on: for gunicorn's real chunked environ, `content_length is None` and
  `"Transfer-Encoding" in headers` is True — the same two premises the helper asserts
  about itself before every call.

## What I could not verify

1. **Anything inside a running gunicorn worker.** Not installable/runnable on Windows
   (verified by execution). Boot ordering, fork, and multiprocess mmap behaviour in
   F-A are READ from source, not run. The HTTP parsing and environ construction in
   F-C **are** executed.
2. **Whether Railway sets `PROMETHEUS_MULTIPROC_DIR` in the deployment environment.**
   That single fact decides whether F-A applies at all.
3. **Any CPU-second figure.** `freesasa` is not installable here, so `run_pipeline` is
   stubbed. I measured no CPU cost — not the sampling's "1% of the log cost" claim, not
   the size bound's saving. Neither is asserted anywhere in the tests either.
4. **HTTP/2 framing.** Under h2 there is no `Transfer-Encoding` header at all, so a
   body with no content-length would be labelled `no_body` and never logged. gunicorn
   speaks only HTTP/1.x and Railway proxies to it as such, so this is theoretical —
   but the whole discriminator depends on that staying true.
