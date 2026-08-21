# QC round 2 — `fix/anon-ratelimit-harden-fixes` (`2346ebe`, `a6ea998`, `feea6f2`)

Independent review. I did not write this code, I did not write the QC round it
answers, and I re-measured every number rather than reading the author's.
Reviewed in my own detached worktrees under
`scratchpad/qc-r2`, `qc-r2-base`, `qc-r2-mut`, `qc-r2-mutbase` — created fresh
for this review, outside `.claude/worktrees/` so the main tree's `.env` is not
picked up by `app.py`'s bare `load_dotenv()`. The main working tree and every
pre-existing worktree (`fix-r2`, `qc-fixes`, `qc-harden`, `qc-base`, `qc-trunk`,
`trunkbase`, `rebase155`, `docsave`) were read but never written.

Throughout: **MEASURED** means I executed it and am quoting my own output.
**READ** means I read the code and it looks right, which is not verification.

---

## Verdict

**PASS WITH FINDINGS.**

All five findings this round set out to close (F-B, F-C, F-D, F-E, F-F) do close,
and the F-A qualification is accurate. Every number in `feea6f2`'s message
reproduces on my own runs — suite totals, scoped totals, both mutation deltas,
both mutation outcomes, the 25-request status split, and the five cited line
numbers — with no discrepancy anywhere. Chain integrity is sound despite the
reported `update-ref` guard failure: I established it from scratch and nothing
was lost or duplicated. Enforcement is unchanged and I re-proved it two ways.

Three findings, none a blocker. The one that matters for the next round is F-2:
a ±0 B "tidy-up" of the new discriminator silences the alarm for the RFC-legal
stacked framing and leaves the suite fully green.

**The specific thing I was asked to hunt — a NEW false claim shipped while
fixing the last round's — is present, but only as F-1, and only about
provenance.** The behaviour F-1 asserts is true; I measured it. This round's
prose is markedly more careful than the two before it: `feea6f2`'s message
correctly narrows the same claim to the two codings QC actually measured. The
source comment and the new test's docstring do not.

---

## Numbers I measured

Full suite, worktree root, no path argument,
`venv/Scripts/python.exe -m pytest -q`, each worktree clean before and after,
each run's `pwd`, `HEAD` and `git status --porcelain` written into the output
file by the same shell that ran pytest (see "on foreground runs" below).

| commit | mine | author's | delta |
|---|---|---|---|
| `a6ea998` (parent, tree-identical to `353fbcd`) | **5489 passed / 21 skipped** (8m13s) | 5489 / 21 | none |
| `feea6f2` (tip) | **5490 passed / 21 skipped** (7m35s) | 5490 / 21 | none |

Both runs exited 0 with zero `FAILED` or `ERROR` lines. The +1 is the single new
test. **No number in this commit set differs from the author's, anywhere.**

Scoped, `tests/test_scout_anon_charge_pairing.py`:

| commit | mine | author's |
|---|---|---|
| `a6ea998` | **41 passed** | 41 |
| `feea6f2` | **42 passed** | 42 |

`ruff check` on `scout/ratelimit.py`, `shared/metrics.py`,
`tests/test_scout_anon_charge_pairing.py`: **All checks passed** (MEASURED,
`--no-cache`). The repo as a whole has 144 pre-existing ruff errors; none is in
these three files.

> **On foreground runs.** The full suite takes 7m35s at the tip and exceeds this
> harness's 10-minute foreground cap — my first foreground attempt was killed at
> 90%. I ran the two full suites in one background job instead, and defeated the
> failure mode the brief warns about (a backgrounded run silently ignoring its
> own `cd`) by having the job itself write `pwd`, `HEAD`, `STATUS_PRE`, the
> pytest exit code, `HEAD_POST` and `STATUS_POST` into the same output file. Both
> files attest the correct worktree, the correct SHA, and an empty status before
> and after. Every scoped run and every mutation below was foreground, with HEAD
> and status printed immediately before and after.

---

## 1. Chain integrity — MEASURED from scratch, INTACT

The author reports that their `git update-ref … 353fbcd` old-value guard failed
(the branch was at temp commit `7d34ff2`), `set -e` did not abort, and a
following `git reset --hard` moved the branch. I did not take their proof.

* `c749480..feea6f2` is **6 commits, 0 merge commits**, linear.
  `git merge-base feea6f2 c749480` = **`c749480`** exactly.
* Parent chain: `feea6f2` → `a6ea998` → `2346ebe` → `17a392a` → `22cbe20` →
  `e5c0fd0` → `c749480`.
* **`2346ebe` is message-only**: tree `97adce7e5c06446c7b0f1c3565aed9f937bda8c1`,
  identical to `84aed72`'s; identical parent `17a392a`; `git diff --quiet` exit 0.
* **`a6ea998` is message-only**: tree `50656036e2834fb27ebeeedfd77b0a6bd559ddfb`,
  identical to `353fbcd`'s; `git diff --quiet` exit 0. Its parent differs
  (`2346ebe` vs `84aed72`), which is what rebuilding the chain onto the amended
  parent means.
* `git diff 353fbcd HEAD` and `git diff a6ea998 HEAD` are **byte-identical, both
  12806 B**.
* **The orphan is harmless.** `7d34ff2` ("TEMP: fix round 2 code") is reachable
  only from the branch reflog, is **not** an ancestor of `feea6f2` — and its tree
  is `1928c469fc580b09645bcb2d739e802d0bdd6ced`, which is **byte-identical to
  `feea6f2`'s tree**. `git diff 7d34ff2 feea6f2` is empty. Nothing was lost and
  nothing was duplicated; the temp commit was the same content on the old parent.
* **AST scan** of the changed files, `a6ea998` → `feea6f2`, top-level *and*
  methods: 0 missing, 0 duplicated, and exactly **one added definition** —
  `TestTheAlarmItself.test_a_transfer_coding_that_is_not_chunked_is_not_the_chunked_signal`.
  This is the check the repo's "rebuilt a file from one side's blob and lost a
  whole function" incident calls for.
* **Line endings**: 0 bare LF on disk in all three files (867 / 364 / 1389 CRLF).
* **The two amends contain only what they claim.** Diffing the messages:
  `84aed72` → `2346ebe` is exactly the F-F restoration (5,481 → 5,160 plus the
  paragraph explaining it). `353fbcd` → `a6ea998` is exactly the F-A rescope
  (bullet retitled `EXPORTED — ON THE PATH THAT WAS VERIFIED…` plus the
  `--preload` paragraph) and the F-D correction (F7's table header and the new
  `ONLY M7 CLOSED AN UNGUARDED HOLE` paragraph). Nothing else was slipped in.

## 2. The five fixes — MEASURED, all five land

### F-B — `shared/metrics.py`'s alerting instruction. CLOSED, and its numbers are real.

I did not read the new comment and nod. I drove 25 chunked
`POST /scout/analyze` for a nonexistent job through the real Flask app:

```
SESSION LIMIT = 8  IP LIMIT = 10
statuses in order      : 8x404 then 17x429
chunked metric delta   : 25.0        other metric delta : 0.0
unmetered_chunked_bodies : 25        unmetered_bodies   : 25
run_pipeline invocations : 0
bucket 'scout_analyze'         key '127.0.0.1'       charges 8
bucket 'scout_analyze:session' key 'anon:no-session' charges 25
```

Byte for byte the comment's figures, and the prior QC's. Every clause of the new
comment checks out:

* "The meter runs ahead of both limiter tiers and every refusal" — **MEASURED**:
  all 25, including the 17 refused, incremented the counter. **READ** in the
  source: `_metered_job_id` at `scout/ratelimit.py:808`, before `hit()` at `:829`
  and `:847` and before `_refuse()` at `:836` and `:851`. All five line numbers
  the author cites are correct.
* "no analysis run" — `run_pipeline` invoked 0 times.
* "nobody charged twice" — one charge per request per bucket, never two.
* "Any caller can pick this label by sending chunked framing" — true, and F-C's
  own evidence shows it was previously even cheaper than that.
* "It counts REQUESTS THE METER COULD NOT SIZE, and claims nothing beyond that" —
  one call site, `ratelimit.py:422`, inside `if length is None:`. A `CL: 0`
  request increments nothing (MEASURED, counter delta 0).

The over-claim F-B named ("sustained, it means … capacity has halved") is gone
and is replaced with a correctly weaker one ("a reason to INVESTIGATE … not proof
on its own"). See F-3 for the one clause of the new comment that is still not
exhaustive.

### F-C — the discriminator. CLOSED, and enforcement is unchanged.

I rebuilt the prior QC's rig — gunicorn 24.1.1's own `http.message.Request`
parser and `wsgi.create()`, on Windows, with shims for `fcntl`, `grp`, `pwd` and
`os.geteuid` (the parser calls none of them) — and then ran the **real**
`_metered_job_id` inside a real request context over each resulting environ.

| framing | CONTENT_LENGTH | HTTP_TRANSFER_ENCODING | returns | Δchunked | Δother | logs |
|---|---|---|---|---|---|---|
| bodiless POST | None | None | `''` | 0 | 1 | 0 |
| real chunked | None | `'chunked'` | `''` | 1 | 0 | 1 |
| `TE: identity`, 0-byte body | None | `'identity'` | `''` | 0 | 1 | 0 |
| `TE: compress`, 0-byte body | None | `'compress'` | `''` | 0 | 1 | 0 |
| `TE: deflate`, 0-byte body | None | `'deflate'` | `''` | 0 | 1 | 0 |
| `TE: gzip`, 0-byte body | None | `'gzip'` | `''` | 0 | 1 | 0 |
| `TE: Chunked` (mixed case) | None | `'Chunked'` | `''` | 1 | 0 | 1 |
| `tRaNsFeR-eNcOdInG: chunked` | None | `'chunked'` | `''` | 1 | 0 | 1 |
| `TE: gzip, chunked` (stacked) | None | `'gzip, chunked'` | `''` | 1 | 0 | 1 |
| `TE: br` | rejected by gunicorn — `UnsupportedTransferCoding` | | | | | |
| `TE:` (empty) | rejected by gunicorn — `UnsupportedTransferCoding` | | | | | |
| `CL: 0` bodiless | `'0'` | None | `''` | 0 | 0 | 0 |
| `CL: 5` + `TE: chunked` | rejected by gunicorn — `InvalidHeader: CONTENT-LENGTH` | | | | | |

**ENFORCEMENT IS UNCHANGED — MEASURED.** Every parseable framing returns `''` and
fails closed. Only the reporting is split. This is the strictest criterion in the
brief and it is a genuine pass, established independently of the tests.

The four non-chunked codings are all accepted, all arrive with no Content-Length,
all reach the branch and all now land on `other` — so the FACT the new prose
asserts is true (see F-1 for the attribution). `~90-byte request`: the smallest
`TE: gzip` bodiless request with a realistic Host is **83 bytes** on the wire.

**Enforcement is also pinned, not merely observed.** My own mutation **ME**
(+6 B, `return ""` → `return source()` on the unsized path — fail OPEN, the exact
regression the feature exists to prevent): **2 failed / 40 passed**.

### F-D — the M6 correction. CLOSED and accurate.

`a6ea998`'s F7 now says only M7 was green on the parent (37/37) and that M6 gives
1 failed / 36 passed. **VERIFIED READ**: `2346ebe`'s test file line **684** is
exactly `assert stub_pipeline == [job_id], "the finalise path re-ran the
pipeline"`, inside `test_a_whitespace_padded_job_id_still_redeems_its_own_credit`
(def at line 660). The citation is correct to the line. I did not re-run M6; the
prior QC did, twice, and the artefact it points at is where it says it is.

### F-E — `_LOG_EVERY`'s magnitude. CLOSED, and the bound is load-bearing, not cosmetic.

The brief asked whether the assert pins anything useful. **It does.** I measured
the whole guard band by editing the constant and running `TestTheAlarmItself`:

| `_LOG_EVERY` | result |
|---|---|
| 1 | **4 failed** — `count % 1 == 1` is never true, the alarm goes fully silent |
| 2 | **1 failed** — the sampled test (1 record per 3 requests) |
| 3 | green |
| 100 (shipped) | green |
| 1000 | green |
| 20000 | **1 failed** — the new assert |

So the guard band is **[3, 1000]**, and the failure mode F-E named — a period so
long the second sample never lands in a worker's lifetime, restoring the
once-per-deploy latch with a green suite — is genuinely closed. The bound is
one-sided in prose but not in effect: the low end is held by
`test_the_warning_is_sampled_and_not_one_per_request`. One honest caveat: at
`_LOG_EVERY = 3` the log cost is 33%, not the 1% the commit message quotes, and
that 1% figure is not itself pinned. Not a finding — 3 is not a plausible
regression.

### F-F — the renumbered historical figure. CLOSED.

`2346ebe` restores **5,160** on the fail-open mutation, labels it as the
pre-rebase tree it was measured on, and adds a paragraph saying explicitly that
renumbering it to 5,481 "attached a count nobody has ever measured under that
mutation to a specific past event". The message diff shows this is the only
change. Correct and complete.

### F-A qualification — accurate.

`a6ea998`'s bullet is retitled `EXPORTED — ON THE PATH THAT WAS VERIFIED, WHICH
IS NOT THE DEPLOYED ONE`, and the added paragraph records the `--preload`
ordering, that it is pre-existing and repo-wide, that it turns on whether Railway
sets `PROMETHEUS_MULTIPROC_DIR`, and that it is filed separately and not fixed
here. **VERIFIED READ** in the installed gunicorn 24.1.1: `arbiter.py:121` is
`self.app.wsgi()` under `if self.cfg.preload_app:`, and `arbiter.py:141` is
`self.cfg.on_starting(self)` in `start()`. Both citations are exact. The
paragraph is careful to say QC *read* the boot order and *simulated* the import
order, which is what the prior QC actually did.

## 3. Mutations — MEASURED, every delta and every outcome reproduces

Applied by binary read/replace/write on this pure-CRLF tree, byte delta printed,
the file re-read from disk and its content re-asserted (so the ±0 B mutation is
still proven to have landed), `git reset --hard HEAD` and an empty
`git status --porcelain` between each. Scoped to
`tests/test_scout_anon_charge_pairing.py`.

| Mut | change | my delta | author's | my result | author's | holds? |
|---|---|---|---|---|---|---|
| **MA** | discriminator back to the presence test | **−60 B** | −60 B | **1 failed / 41 passed** — the only failure is `TestTheAlarmItself::test_a_transfer_coding_that_is_not_chunked_is_not_the_chunked_signal`, at `:1205` (`unmetered_chunked_bodies == 0` got 2) | same | **YES** |
| **MB** @ `feea6f2` | `_LOG_EVERY` 100 → 20000 | **+2 B** | +2 B | **1 failed / 41 passed** — `test_the_warning_re_arms_instead_of_latching_off_for_the_deploy`, at `:1118`, the new assert | same | **YES** |
| **MB** @ `a6ea998` | same, on the parent's own source and tests | **+2 B** | +2 B | **41 passed, GREEN** | GREEN | **YES** — the prior QC's MW reproduced |

**On MA's missing GREEN-before row.** The author calls it "n/a by construction".
That is legitimate but weak on its own, so I established the property directly
instead: MA is the *minimal* single-variable revert of the discriminator — it
leaves the `other` label, the metric, and the new test untouched — and under it
**exactly one** test fails, the new one, for exactly the right reason. That is a
stronger statement than a green-before row would have been, and it is measured.

**My own additional mutations, at the tip:**

| Mut | change | delta | result |
|---|---|---|---|
| **MC** | drop the case fold: `"chunked" in encoding.lower()` → `"chunked" in encoding` | −8 B | **1 failed** (`:1223`) — case-insensitivity **is** pinned, by the `TE: Chunked` sub-assertion |
| **MD** | substring → equality: `"chunked" in encoding.lower()` → `encoding.lower() == "chunked"` | ±0 B (landing proven by on-disk content re-read) | **42 passed — SURVIVES.** See F-2 |
| **ME** | fail OPEN on the unsized path: `return ""` → `return source()` | +6 B | **2 failed** — enforcement is pinned |

---

## Findings

### F-1 — LOW. The new prose attributes to QC a measurement QC did not make. MEASURED.

`scout/ratelimit.py:363-365`, and again in the new test's docstring
(`tests/test_scout_anon_charge_pairing.py:1181-1184`):

> gunicorn also accepts `identity`, `compress`, `deflate` and `gzip` as transfer
> codings, and **QC measured a ZERO-byte body under each of those** arriving with
> no Content-Length

The prior QC measured **two** of the four. Its F-C table has rows for
`TE: identity` and `TE: gzip` only, and it explicitly marks the four-coding
acceptance as **READ**, `gunicorn/http/message.py:226-247`. `compress` and
`deflate` were never executed by anyone before this review.

This is the class of error the round exists to fix, appearing one file over — the
same shape as F-B, which took an over-claim out of the WARNING text and left it
in `metrics.py`. It is also the pattern the repo's own memory tracks: prose
asserting a stronger evidence class than exists.

Two things keep the severity at LOW:

1. **The underlying fact is true, and I have now measured it.** All four codings
   are accepted by gunicorn 24.1.1, none sets a Content-Length, all four reach
   the branch, and at the tip all four land on `other` (table in §2, F-C). The
   claim is true; only its provenance is wrong.
2. **`feea6f2`'s commit message gets this right.** It narrows the measured half
   correctly: "…and that `TE: identity` and `TE: gzip` with a ZERO-byte body were
   therefore both labelled `chunked`". It carries only the milder half of the
   error — "QC measured … that gunicorn accepts [all four]", where QC read it.

Also worth noting: the docstring names four codings and the loop under it
exercises two (`for coding in ("identity", "gzip")`). Adding `"compress"` and
`"deflate"` to that tuple and changing `== 2` to `== 4` would make the prose and
the test agree and would cost two lines. Alternatively, say "QC measured
`identity` and `gzip`; `compress` and `deflate` are read from
`gunicorn/http/message.py`" — which is now itself out of date, because I ran them.

### F-2 — LOW-MEDIUM. The substring semantics is unpinned, and the RFC-legal stacked framing is exactly what would go silent. MEASURED.

`scout/ratelimit.py:422` — `chunked="chunked" in encoding.lower()`.

Mutation **MD** rewrites that to `chunked=encoding.lower() == "chunked"`, a ±0 B
change that reads like a tidy-up, and **all 42 tests stay green**.

Under that change the alarm goes silent for `Transfer-Encoding: gzip, chunked`.
That is not hypothetical framing: RFC 9112 permits stacked transfer codings,
gunicorn's own `set_body_reader` handles the list form and comments "chunked
should be the last one", and I **measured** gunicorn 24.1.1 accepting it and
passing it through whole as `HTTP_TRANSFER_ENCODING='gzip, chunked'` with no
Content-Length. A gzipping-and-chunking intermediary is precisely the "edge
re-frames every POST" scenario this alarm exists to detect — so an equality test
would make the alarm silent in one of the cases it was built for, with a green
suite and no reviewer signal.

The shipped code is **correct**. This is a coverage gap, not a defect. The fix is
one line: add `_meter_one_body(app, chunked=True, transfer_encoding="gzip, chunked")`
alongside the existing `"Chunked"` assertion in the new test. Note the test
already pins the case fold (my MC goes red), so `in`-vs-`==` is the only
remaining unguarded half of the discriminator.

I rate this above F-1 because F-1 is about who measured what, and this is about a
live property with no guard — the repo's own "ask what has NO guard" lesson.

### F-3 — LOW. `other`'s enumeration is presented as exhaustive and is not. MEASURED.

`shared/metrics.py:155-157`, and the same claim in `feea6f2`'s message ("`other`
is true of everything that can reach it"):

> `other` is every remaining unmeasurable length: a POST with no body at all (a
> scanner's opening move), or a transfer coding that is not `chunked`.

There is a third shape. A POST with a **real body** and **neither**
Content-Length **nor** Transfer-Encoding is accepted by gunicorn 24.1.1 —
MEASURED on both HTTP/1.1 and HTTP/1.0 — and arrives as
`CONTENT_LENGTH=None, HTTP_TRANSFER_ENCODING=None` with `Body(EOFReader(...))`.
It reaches the branch and is labelled `other`, and it is neither of the two
things the comment names.

Fairness, and why this is LOW:

* The **label** is still correct — `other` is a catch-all, and the request is
  genuinely one the meter could not size. Only the gloss is incomplete.
* Enforcement is unaffected: it returns `''` like everything else.
* I proved gunicorn's **parser** accepts it; I did **not** establish that
  Railway's edge would ever forward such a request, and under HTTP/1.1 the RFC
  says a body needs framing. Treat it as reachable-in-principle.

Two shapes I checked and which are **not** gaps: a malformed `Content-Length`
(`abc`, `-5`, duplicated) is rejected by gunicorn with `InvalidHeader` before it
reaches the app, and werkzeug independently reads all four malformed values as
`0`, not `None`, so it never reaches the branch either (MEASURED both ways).

One clause would cover it: "…or any other request the meter could not size".

### F-4 — INFORMATIONAL. "six lines in `shared/metrics.py`" is neither of the two counts. MEASURED.

`a6ea998`'s message: *"It is now also exported beside `SCOUT_RUNS` as … — six
lines in `shared/metrics.py`."*

The hunk `2346ebe` → `a6ea998` adds **14 lines**: 8 comment lines, the 5-line
`Counter(...)` statement, and a blank separator. "Six" matches the `Counter`
statement plus its separator and nothing else.

I am recording this at INFORMATIONAL and explicitly **not** treating it as a
regression of this round: it was not among the prior QC's F-A…F-F, so it was not
in scope, and the point the sentence is making (this reuses the registry, the
factory and the stub rather than building anything) is true. It is here because
the brief asked for the count and because the class of error is under audit.

---

## What I attacked that came out clean — do not re-litigate these

* **Stale `no_body` references: NONE.** `git grep` over the whole tree at the tip:
  zero hits. Also zero in `docs/` (tracked *and* the untracked handoff files,
  including `docs/HANDOFF-2026-08-18-anon-rate-limiting.md`), zero in
  `docs/qc/*.md`, zero in `ALERTING.md`. No dashboard or alert file in this repo
  names the label at all.
* **`a6ea998`'s message still saying `no_body` is acceptable, and I am deciding
  that explicitly.** It appears twice (`{framing="chunked"|"no_body"}` and the
  `/metrics` render). Both are true statements about `a6ea998`'s own tree, which
  is what a commit message describes; `feea6f2` documents the rename under its
  own heading and explains why; and the counter has never been deployed. This is
  historical description, not a live false claim. Rewriting it would make the
  message false about its own commit.
* **Regression risk beyond the scoped file: none found.** `SCOUT_UNMETERED_BODIES`,
  `unmetered_bodies` and `unmetered_chunked_bodies` appear only in
  `scout/ratelimit.py`, `shared/metrics.py` and
  `tests/test_scout_anon_charge_pairing.py`. `tests/test_metrics.py` — the only
  other test that touches the registry — never references this counter and does
  not enumerate counters or label values. There is no env var of that name.
* **"The counter has never been deployed, so the rename costs nothing"** —
  MEASURED true: `SCOUT_UNMETERED_BODIES` does not exist in
  `origin/main:shared/metrics.py` (`origin/main` = `5ccdf2d`), and the branch has
  no remote ref.
* **The metric name, the label key and the `chunked` label value are unchanged**
  across the rename — confirmed by diff and by the render in my probe.
* **The `_meter_one_body` premise assert is not weakened by the new signature.**
  `flask_request.headers.get("Transfer-Encoding") == transfer_encoding` still
  fails loudly if the header is absent when one was asked for, or present when
  none was, including for `transfer_encoding=None` (absence required) and
  `""` (which would also go red). The `chunked` parameter now only controls
  `wsgi.input_terminated`, which nothing under test reads.
* **Case-insensitivity is pinned** — mutation MC, 1 RED.
* **Enforcement is pinned** — mutation ME, 2 RED — *and* independently measured
  fail-closed over 9 distinct parseable framings through gunicorn's own parser.
* **The `feea6f2` line citations are all correct**: `:808` / `:829` / `:836` /
  `:847` / `:851` for the meter-before-limiter ordering, `:356-366` for the
  rewritten comment block, `arbiter.py:121` / `:141` for the preload ordering,
  `tests/…:684` for the F-D artefact.
* **`ruff check` passes on all three changed files**; 0 bare LF on disk in all
  three; AST shows 0 duplicated and 0 missing definitions and exactly one added
  method.
* **The two amends carry only the changes they claim** — verified by diffing the
  old and new messages in full.
* **The orphaned temp commit `7d34ff2` cost nothing** — its tree is byte-identical
  to the tip's.

## What I could not verify

1. **Anything inside a running gunicorn worker.** gunicorn does not run on
   Windows. I ran its *parser* and `wsgi.create()` with three shims, which is why
   the framing table above is real rather than simulated; the worker model, fork
   behaviour and multiprocess mmap are not executed by me or by anyone in this
   chain.
2. **Whether Railway sets `PROMETHEUS_MULTIPROC_DIR` externally.** That single
   fact still decides whether the prior round's F-A applies at all. Unchanged
   from the last report, and correctly disclosed by `a6ea998`.
3. **Whether Railway's edge would ever forward the F-3 framing** (a POST with a
   body and no framing headers), or the F-2 framing (`gzip, chunked`). I proved
   gunicorn accepts both; I could not observe the edge.
4. **The author's process claims** — that their runs were foreground, in their own
   worktree, with clean status. I can only report that every *number* they quote
   reproduces on my own independent runs, which it does, without exception.
5. **Any CPU-second figure.** `freesasa` is not installable here, so
   `run_pipeline` is stubbed. The commit message quotes no CPU cost, so nothing
   above depends on this.
6. **M6 and M7** were not re-run by me; F-D is a correction to a prior QC
   measurement, and I verified the artefact it cites (line 684) rather than
   re-executing the mutation.
