# QC — Anonymous rate limiting, Phase 4 (fix round)

**Verdict: PASS WITH CORRECTIONS.**

The defect that failed `555448c` is **closed**, and closed structurally rather
than patched. I attacked it independently on a real server across nine
attack families — the original divert, its mirror, three alternative sources,
duplicate query keys, non-string and non-object JSON, seven whitespace and
unicode variants, and five Content-Length framings — and found **no path on
the shipped code where one charge buys more than one pipeline run**. The
mechanism is right: the meter and the view call *the same function*, so they
cannot disagree by construction, which is a stronger property than any
preference order could give.

Three claims I verified that the builder was right about and one it was wrong
about:

- **The empty-id change really is load-bearing** — proved by counterfactual,
  not by reading. With both guards removed, a `""` credit is minted by a
  no-`job_id` `/progress` and redeemed by an oversize body for a **free full
  pipeline run** (§3). The builder's reasoning is exactly correct.
- **The AST guard is real.** I broke it and watched it go red, with the
  correct assertion naming the offending view (§4).
- **The Phase 5 claim holds**: the first refusal a real NAT lab meets is
  intake at the 11th researcher, `REASON_RATE_LIMITED`, the "this network"
  message; intake carries no session tier; nothing asserts the 10/10 balance
  (§8).
- **But the prior QC round's Phase 5 guidance is wrong**, and this round
  should correct it: **signing in bypasses *both* tiers**, so the per-IP
  refusal *can* honestly be answered with "sign in" (§8.3).

Against that, five of my seventeen mutations survived, and three of them
matter. The most serious: **`MM`, a fail-open `_metered_job_id`, restores the
original diversion in full — one charge, two full pipeline runs — with the
entire 5,160-test suite green** (§5.1). The bound is written correctly; the
property that makes it safe is not pinned by anything. In a repo where five
guards have certified false during this one task, that is the finding to
carry forward.

Nothing here is exploitable on `4afb6ff` as written. Everything here is
"the code is right and the net under it has holes".

**SHAs reviewed**

| | |
|---|---|
| Subject | `4afb6ff8ac543786806aa8ab96e290af3bdf2828` (`fix/anon-ratelimit-phase-4`) |
| Failed by previous QC | `555448ce4258484fc0547d73165d62273788cc4d` |
| Also in the chain | `6e0a68cb538c7929397afdacd7c4636bf7f10c06`, `85a4fb641a60842c9c98abd6fc25536595c30b76` (PR #155) |
| Merge base with trunk | `fa938b09fd43aae6fc06976756f5c6fe379a537f` |
| `origin/main` at review time | `7fd180df35086cfc5da3710ff336024901d8e73b` (#158) |
| Rebased result (throwaway) | `c69094d9710911ba779d88ef0d6a0d379bc7706f` |
| Files touched | 3, +499 / −32 |

**Worktree isolation.** All work in
`C:\Users\lab\Documents\Claude_projects\tools-hub\.claude\worktrees\agent-a089f80e359f0df3b`
(`git worktree list` shows 32 live worktrees). `git checkout` in my own
worktree was blocked by the harness classifier, so the subject was read via
`git show` and exercised in four throwaway worktrees under the session
scratchpad (`base`, `branch`, `mut`, `reb`), all deleted at the end. The main
tree at `C:\Users\lab\Documents\Claude_projects\tools-hub` was **read only**.
Nothing pushed, no branch created, no PR, no production traffic. The
`X-Forwarded-For` experiment was not run.

**Method.** Every number marked *measured* came from a real Werkzeug HTTP
server over real sockets with real cookie jars, single-threaded (production
runs sync workers), driven by hand-built raw HTTP so nothing was normalised
behind my back. `freesasa` is not installed, so `run_pipeline` is a counting
stub that writes the `results.csv` a real run writes; *pipeline invocations*
are the load multiplier and are counted directly. CPU-second conversions use
Phase 0's ~9 CPU-s per pipeline run and ~6 for the finalise extras; **I did
not re-measure those** and say so wherever they appear.

---

## 1. The diversion, attacked independently

### 1.1 It is closed, and closed by construction

`_analysis_id()` is gone. In its place two functions that each read exactly
one source with no fallback, each named at the decorator and called by the
view:

```python
job_id_in_query()   # request.args only     -> declared at :1020, called at :1026
job_id_in_body()    # request.get_json only -> declared at  :723, called at  :727
```

The meter derives the id **once** (`_metered_job_id(job_id)`) and hands it to
`_followup_key(job_id)`. The view calls the same function. There is no order
to get backwards and no second source to disagree with.

### 1.2 Nine attack families, measured on a real server

| # | Attack | Result |
|---|---|---|
| A1 | **The original divert.** `POST /analyze?job_id=A`, body `{"job_id":"B"}` | **charged 1→2**, ran on B — one charge, one run |
| A2 | **The mirror.** `GET /progress?job_id=B` carrying a JSON body naming A | credit keyed on **B** (the query id, the job that ran) |
| A3a | job id as **form-encoded** body | charged, 400 |
| A3b | job id in **headers** (`X-Job-Id`, `Job-Id`) | charged, 400 |
| A3c | job id in the **path** (`/scout/analyze/<id>`) | 404, no such route |
| A3d | job id in a **cookie** | charged, 400 |
| A4a | **duplicate `job_id` query keys** on `/progress` | meter and view both take the first — same value |
| A4b | duplicate query keys + a diverting body on `/analyze` | charged |
| A5 | `job_id` as **list / null / int / dict / bool** | all charged (guarded by `isinstance`) |
| A6 | body is **valid JSON but not an object** (list, string, number, null, true) | all charged |
| A7 | **7 whitespace/unicode variants** (space, tab, newline, NBSP U+00A0, ideographic U+3000, VT, FF) | meter and view **agree in every one** |
| A8a | **chunked**, no Content-Length | charged — fails closed |
| A8b | no Content-Length, no TE | charged, 400 |
| A8c | body padded past 4 KiB | charged |
| A8d | boundary: exactly 4096 B / 4097 B | 4096 redeems, 4097 charged — matches `>` |
| A8e | **lying Content-Length** (declare 100, send 5 KB) | charged, 400 |

**What one charge can buy, in CPU-seconds: nothing above the honest ~15.** I
found no path on `4afb6ff` where a single charge yields two pipeline runs. On
`555448c` the same A1 bought ~24 CPU-s; here it buys ~15 and is charged twice
for two runs, which is the correct ratio.

### 1.3 The whitespace family needed a second look

My first detector flagged all seven A7 variants as diversions. It was
miscalibrated: both readers `.strip()`, so a padded id resolves to the *same*
job, and riding free is the **correct** outcome — one analysis, one charge.
Re-run asserting the real property (does the credit key equal the job that
actually ran?):

```
  plain            ran_on=['e4a43f0c'] credit_ids=['e4a43f0c'] AGREE=True
  trailing space   ran_on=['d76a221b'] credit_ids=['d76a221b'] AGREE=True
  tab / newline / NBSP / ideographic space          AGREE=True
  interior space   ran_on=[]           credit_ids=['de0fb28a'] AGREE=False
```

The last row is not a defect either: `<uuid> x` strips to itself, the view
404s, and the resulting credit is keyed on a job id that can never resolve —
the attacker paid one charge for an unspendable credit. Recorded so nobody
re-derives the false positive.

### 1.4 One pre-existing crash, unchanged by this commit

`POST /scout/analyze` with a JSON **array**, string, number or `true` body
returns **HTTP 500**, because `analyze()` does
`data = request.get_json(silent=True) or {}` and then `data.get("chain", ...)`
on what may be a list. I reproduced identical statuses on `555448c`, so it
**predates this commit** and is not part of it. It fails closed (charged).
Worth its own one-line fix, not a blocker here.

---

## 2. The new body-size bound

### 2.1 A legitimate `/analyze` is never refused, and never mischarged

The bound touches **only the meter**; the view still parses whatever it is
given once the request is allowed. So the failure mode is never a refusal —
it is at worst *losing the credit and paying twice*.

```
real front-end body = 64 bytes: {"job_id": "<uuid>", "chain": "A"}
bound = 4096 bytes -> 64x headroom
absurd 200-char chain id = 263 bytes -> still under
a chain id would need 4,032 characters to reach the bound
```

The front end sends `body: JSON.stringify({job_id, chain})`
(`templates/scout/index.html`), a string body, so the browser always sets
Content-Length. Nothing realistic approaches 4 KiB. **Confirmed.**

### 2.2 It fails closed at every framing

Verified above (A8a–A8e) and again directly: chunked → charged; no
Content-Length → charged; oversize → charged; lying length → charged. An
unparsed body is always a charge, never a free ride.

### 2.3 The timing, reproduced — direction confirmed, exact figures not

TTFB of a **refused** `POST /scout/analyze`, real sockets, median of 5:

| Body | base `555448c` | branch `4afb6ff` |
|---|---|---|
| 18 MB, Content-Length | **0.2489 s** | **0.0036 s** |
| 18 MB, chunked | 0.1308 s | 0.0053 s |
| 80 B | 0.0011 s | 0.0012 s |

The improvement is **real and larger than claimed** (~69x on the
Content-Length path). The builder's specific pair, `0.336 s → 0.074 s`, I
could **not** reproduce; my base is lower and my branch is lower still.
Directionally identical, so I record it as confirmed-in-substance and
not-reproduced-in-detail.

### 2.4 A Werkzeug artifact that will trap the next QC agent

Measuring total request time rather than TTFB, the branch appears to hang the
worker **forever** on any refused body over the bound. It is not the app.
`werkzeug/serving.py:356` drains unread request data after the WSGI call with
a blocking `self.rfile.read(10_000_000)`; because the app no longer consumes
the body, that read blocks until the client closes. Stack captured:

```
werkzeug/serving.py:356 in execute
    data = self.rfile.read(10_000_000)
```

The comment above it says the approach is naive and only safe because the dev
server has no keep-alive. **Production is gunicorn 24.1.1**, which does not
share this code path — and gunicorn cannot run on Windows, so I could not
test it. Recorded because a naive Werkzeug harness reports this as a P0 and
it is not one.

### 2.5 A genuine new production coupling, unmeasured

Two things this commit newly depends on that nothing tests and I cannot test
from here:

1. **`request.content_length` must be present in production.** If Railway's
   edge ever re-frames `/scout/analyze` as chunked, the meter declines every
   body, **every analysis loses its credit, and Phase 4's win silently
   evaporates** — ten researchers back to five — with no test able to see it.
   It fails closed, so this is a capacity regression, not a security one, but
   it is invisible. Phase 6 should count credit redemptions, not just
   refusals.
2. **The origin now answers before reading the request body.** That is a
   documented HTTP hazard with a proxy in front: an edge still streaming an
   18 MB body to an origin that has already responded and closed may surface
   a 502 to the client instead of the 429. Untested, and untestable without
   production.

---

## 3. The empty-id change — load-bearing, verified by counterfactual

The builder's claim is that without it the new bound would have opened a
fresh hole. **Correct, and I proved it by removing the guards and attacking
the result** rather than by reading the argument.

Guards removed (`MH` + `MI`, both landed, −33 B and −16 B):

```
3a. GET /scout/progress with NO job_id     -> a "" credit EXISTS: True
3c. POST /scout/analyze, >4 KiB body       -> charges 1->1, pipeline 0->1
    >>> FREE FULL PIPELINE RUN: True
```

With the guards in place, the same sequence charges `1->2` and the free run
does not happen. So a `""` credit, minted by a `/progress` that did no work,
would have been redeemable by *any* request whose body the meter declined to
read. The reasoning in the commit message is exactly right.

**It cannot be turned around.** Probed both directions:

| Probe | Result |
|---|---|
| Induce an empty id to get free compute | no — an empty id never spends; the request is charged |
| Induce an empty id to suppress a charge | no — `{"job_id": ""}` on `/analyze` is **charged**, and the real credit stays outstanding |
| `/progress` with an all-whitespace `job_id` | charged, no `""` credit |
| Normal flow still grants and spends | yes — one analysis, one charge, credit consumed |

**But only half of it is pinned.** `MH` — removing the guard on the *spend*
side alone — **survived green** (§5). It is not exploitable today only
because the *grant* side is tested and no `""` credit can therefore exist.
The commit says "an empty id now neither grants nor spends"; a test covers
"nor grants" only.

---

## 4. The structural AST test — broken on purpose, and it went red

I added a **third paired route** to `scout/routes.py` in four shapes and ran
only the guard:

| Probe | Landed | Result |
|---|---|---|
| P4a declares `job_id_in_query`, view calls `job_id_in_body` | +320 B | **RED** — as claimed |
| P4b declares a source, view calls neither | +342 B | **RED** |
| P4c control: declares and calls the same source | +321 B | GREEN |
| P4d calls the declared source, **discards it**, uses the other | +343 B | **GREEN — gap** |

P4a fails on the right assertion, naming the offender:

```
AssertionError: these views do not call the job id source their own decorator
declares, so the meter can charge one job while the view runs another:
{'third_paired_route': 'job_id_in_query'}
```

**The claim holds: a future third paired route that gets this wrong is
caught.** The residual is P4d — the guard tests *that the declared source is
called*, not *that its value is used*. A view that calls it decoratively and
then reads the raw source into the variable it actually uses passes. Low
likelihood, worth a sentence in the docstring rather than more machinery.

---

## 5. Mutations — my own 17: 12 RED, **5 SURVIVED**

Applied against a pure-CRLF tree (234 CRLF / 0 bare LF in
`scout/ratelimit.py`), written with `newline=""`, **each verified landed by
byte delta** before any verdict; a mutation that does not land is reported
SKIPPED, never RED. None were skipped.

| # | Mutation | Landed | Verdict |
|---|---|---|---|
| MA | `job_id_in_body` falls back to the query (the original defect, as a fallback) | +69 B | RED |
| MB | `job_id_in_query` falls back to the body (**round 1's M2**) | +141 B | RED |
| **MC** | **`job_id_in_query` drops `.strip()` (round 1's M3)** | **−8 B** | **GREEN — SURVIVED** |
| MD | `job_id_in_body` drops `.strip()` | −8 B | RED |
| ME | the two declarations swapped in `routes.py` | +1 B | RED |
| MF | `analyze()` stops calling `job_id_in_body`, re-reads the body | +14 B | RED |
| MG | `progress()` stops calling `job_id_in_query`, re-reads args | +21 B | RED |
| **MH** | **empty-id guard removed on SPEND** | **−33 B** | **GREEN — SURVIVED** |
| MI | empty-id guard removed on GRANT | −16 B | RED |
| MJ | the 4 KiB bound removed entirely | −163 B | RED |
| **MK** | **unknown Content-Length allowed through** | **+5 B** | **GREEN — SURVIVED** |
| **ML** | **the bound raised to 20 MB** | **+12 B** | **GREEN — SURVIVED** |
| **MM** | **the bound FAILS OPEN (oversize → query id)** | **+36 B** | **GREEN — SURVIVED** |
| MN | the `pair`/`job_id` pairing check removed | +10 B | RED |
| MO | any callable accepted as a job id source | +10 B | RED |
| MP | `_followup_key` ignores the job id it was handed | −4 B | RED |
| MQ | the meter derives from the *other* source than declared | +78 B | RED |

**Round 1's survivors, re-run:** `M2` (body-first swap) is now **RED** — the
builder killed it. `M3` (dropped `.strip()`) is **half killed**: RED on the
body side (`MD`), still **GREEN on the query side** (`MC`).
`test_a_whitespace_padded_job_id_still_redeems_its_own_credit` pads the id
only in the **body**, so the query-side `.strip()` remains untested despite a
docstring that says *"Both sides `.strip()`, and dropping it must go red."*
**The commit message's claim that both of QC's survivors were killed does not
hold.** Severity is low — a padded query id keys separately, misses its
credit and is charged, so it fails closed — but the claim should be
corrected.

### 5.1 `MM` is the one that matters

`MM` makes `_metered_job_id` fail **open**: on an oversize body it returns
`request.args.get("job_id")` instead of `""`. Measured on a real server:

```
SHIPPED    body=6,075B  charges 1->2  pipeline 1->2  ran_on_B=True   DIVERTED: False
WITH MM    body=6,075B  charges 1->1  pipeline 1->2  ran_on_B=True   DIVERTED: True
control (small body, what the tests exercise)        DIVERTED: False
```

**That is the original §1 exploit restored in full — one charge, two full
pipeline runs, ~24 CPU-s instead of ~15 — with all 5,160 tests green.** The
behavioural divert tests send a small body, so the oversize path is never
exercised for diversion at all. The shipped code is correct; the property
that keeps it correct is unguarded.

### 5.2 `MK` is load-bearing too

With `MK` applied, the chunked refusal cost comes straight back:

| | shipped | with MK |
|---|---|---|
| 18 MB **chunked**, refused, TTFB | 0.0053 s | **0.1936 s** |

So `length is None` is not belt-and-braces — it is the clause that stops an
attacker re-opening §2.3 simply by sending `Transfer-Encoding: chunked`.
Nothing tests it.

### 5.3 `ML` is a test that cannot detect what it is for

`test_a_refused_analyze_does_not_parse_a_large_body` builds its payload as
`"x" * (ratelimit._MAX_FOLLOWUP_BODY_BYTES * 4)`. The payload **scales with
the constant**, so raising the bound to 20 MB keeps the test green. The
number `4096` is pinned by nothing, and raising it restores the exact
regression this commit exists to fix. A literal (say 1 MB) would fix it.

---

## 6. The rebase — clean, and verified against both parents

`origin/main` is `7fd180d` (#158). Trunk is 6 commits ahead of the merge base
`fa938b0`, and **both sides touch `scout/interfaces.py` and
`scout/routes.py`** — the exact condition under which this repo has lost
hunks. Trunk's `interfaces.py` change is a DBREF length guard in
`_extract_chain_names`; the branch rewrites `detect_interfaces`. Different
functions, same file, auto-merges clean: the invisible-loss setup.

Rebased in a throwaway worktree. **No conflicts.** Verified by diffing the
diffs, ignoring only `index` lines and `@@` line numbers:

```
BRANCH-DELTA  before=fa938b0..4afb6ff   after=origin/main..rebased
  before=4854 lines  after=4854 lines  diff-of-diff=0 lines  -> IDENTICAL
TRUNK-DELTA   before=fa938b0..main      after=4afb6ff..rebased
  before=9731 lines  after=9731 lines  diff-of-diff=0 lines  -> IDENTICAL
```

Both contested hunks present in the rebased tree:

```
interfaces.py:120  if line.startswith("DBREF ") and len(line) > 12:      <- trunk (#158)
interfaces.py:166  "detection. scipy is a hard requirement ..."           <- branch
  (the pure-Python brute-force fallback: gone, as intended)
routes.py:1386     from scout.handoff import VALID_HANDOFF_TOOLS          <- trunk (#154)
routes.py:723/727/1020/1026  job_id_in_body / job_id_in_query             <- branch
```

**Suite on the rebased tree: 5404 passed, 21 skipped.** Throwaway worktree
deleted.

---

## 7. Suite, three trees, first-hand

`-m pytest -q` from each repo root, **no path argument**, repo venv at
`C:\Users\lab\Documents\Claude_projects\tools-hub\venv\Scripts\python.exe`,
output redirected to a file, never piped.

| Tree | Result | Wall |
|---|---|---|
| base `555448c` | **5151 passed, 21 skipped** | 331.8 s |
| branch `4afb6ff` | **5160 passed, 21 skipped** | 317.7 s |
| rebased `c69094d` | **5404 passed, 21 skipped** | 357.7 s |

Both of the builder's numbers match exactly. The +9 is confirmed by direct
collection of `tests/test_scout_anon_charge_pairing.py`: **24 → 33 tests**.

**Environment.** `scipy 1.18.0` present. **`freesasa` not importable** —
confirmed; no wheel and no MSVC toolchain on this box. Flask 3.1.3, gunicorn
24.1.1 installed but `sys.platform == "win32"`, so gunicorn cannot be run.

**What the freesasa gap leaves unverified:** every CPU-second figure in this
report is *converted* from Phase 0, not measured — `run_pipeline` is stubbed
in all tests and in my harness, so what I measured is *pipeline invocations*,
not their cost. Also unverified: the real shape of `results.csv`, and
anything about the production worker (gunicorn is POSIX-only).

---

## 8. The Phase 5 claim — verified, plus one correction

### 8.1 Where a real NAT lab actually hits the wall

Measured end to end, real server, one worker, distinct sessions each doing
the full front-end flow (`/example` → `/progress` → `/analyze`):

```
  researcher  1..10: example=200 progress=200 analyze=200   intake=10  analyze_ip=10
  FIRST REFUSAL -> route=intake /example  researcher=11  status=429
  reason='rate_limited'
  message='Too many Epitope Scout requests from this network. Wait a minute and
           try again, or sign in for a free account with a higher allowance.'
```

**Confirmed on all three points.** The first refusal is **intake**, at the
11th researcher, and it is `REASON_RATE_LIMITED` — the "this network"
message.

### 8.2 Intake carries no session tier, and the balance is accidental

```
  upload     session_limit=False  pair=False  bucket="scout_intake"
  fetch_pdb  session_limit=False  pair=False  bucket="scout_intake"
  example    session_limit=False  pair=False  bucket="scout_intake"

  grep 'ANON_ANALYZE_SESSION_LIMIT <'  -> 0 hits
  grep '== ANON_ANALYZE_LIMIT'         -> 0 hits
```

**Confirmed.** Intake refusals can only ever be `REASON_RATE_LIMITED`, and
nothing anywhere asserts that `ANON_INTAKE_LIMIT` and `ANON_ANALYZE_LIMIT`
are both 10. The balance is accidental exactly as the builder says.

**The stale comment is still shipped.** `scout/routes.py:115-116` still reads
*"QC measured the intake bucket as comfortable — the analyze bucket is where
the wall is."* The builder identified it as stale and did not fix it. It is
now the opposite of the truth and sits directly above the constant a later
phase will reach for.

### 8.3 Correction to the previous round: signing in *does* fix the per-IP wall

`scout/ratelimit.py:718` — `if session.get("user_email"): return f(*args, **kwargs)`
— sits above **both** tiers and above the pairing logic. Measured, per-IP
bucket fully burnt, same address:

```
  anonymous : status=429  reason='session_rate_limited'
  signed in : status=404   -> signing in bypasses the per-IP tier: True
```

So `_OVER_LIMIT_MESSAGE`'s "sign in for a free account with a higher
allowance" is **true**, and the previous round's instruction that *"the per-IP
refusal must not be turned into a signup prompt"* rests on a false premise.
It already is one, and honestly so.

**What Phase 5 must do with this:** instrument the **intake** refusal, not
only the analyze one. A funnel built on the analyze wall sits *behind* the
wall real labs actually hit — no lab reaching capacity will ever see it.

---

## 9. The goal question, answered

> **Can six researchers behind one NAT all use the tool in the same 10-minute
> window, and is one attacker still bounded?**

**Six: yes — ten, measured.** Ten distinct sessions from one address, one
worker, each running the real front-end flow, all 200. The eleventh is
refused.

**First place it breaks: `ANON_INTAKE_LIMIT`, at the 11th researcher, on
`/scout/example`** — an intake refusal reading `rate_limited` and *"from this
network"*. Not the analyze bucket. The two ceilings are exactly balanced at
10 and nothing asserts it, so raising either alone buys real users nothing.

**The attacker is bounded, and Phase 4 is CPU-neutral for the worst case.** I
found no path on `4afb6ff` where one charge buys more than one pipeline run,
across all nine attack families. The `555448c` divert (~24 CPU-s per charge)
is closed; worst adversarial spend returns to the ~300 CPU-s per IP per
window that the previous round established was *already* the base-commit
figure. So this commit roughly doubles what a real lab gets while leaving the
attacker's ceiling where it was — a strictly good trade.

**Where "bounded" stops meaning much is unchanged and not this commit's
fault:** ~300 CPU-s per address against a ~1,200 CPU-s fleet budget still
means **four addresses saturate the box**, and the per-IP count cannot fix
that (previous round §2.4). `ANON_MAX_UPLOAD_BYTES` remains the unscheduled
lever.

**The nearest thing to a break in the attacker bound is not in the code but
in the net around it:** `MM` shows a one-line refactor of `_metered_job_id`
restores the full exploit with a green suite.

---

## 10. Is this safe to open as a PR, fourth in a chain behind #155?

**Yes.** The rebase onto `7fd180d` is clean, both parents' deltas survive
byte-for-byte, both contested hunks are present, and the rebased tree runs
**5404 passed / 21 skipped**. The chain is `85a4fb6` (#155) → `6e0a68c` →
`555448c` → `4afb6ff`; #155 is still open, so this PR either stacks on it or
carries all four.

Two things to do first, neither of which needs new design:

1. **Correct the commit message.** It claims both of QC's survivors were
   killed. Only the body-side `.strip()` was; the query-side one still
   survives (§5).
2. **Delete or fix the stale intake comment** at `scout/routes.py:115-116`
   (§8.2). It is one line and it currently says the opposite of what this
   commit measured.

Recommended in the same PR, all test-only:

3. A test that pins **fail-closed on the oversize path** — the `MM` gap
   (§5.1). This is the one I would not ship without.
4. A literal payload in `test_a_refused_analyze_does_not_parse_a_large_body`
   so the bound's *value* is pinned (§5.3), and a case that pins the
   unknown-Content-Length branch (§5.2).
5. Pad the **query** side in the whitespace test, and assert the spend side
   of the empty-id guard (§3).

---

## 11. What Phases 2, 3, 5 and 6 must now assume differently

**Phase 2 (trust the peer).** Unchanged, and still the precondition for
raising the ceiling. `_followup_key` keys on `_client_ip()`, so whatever
Phase 2 does to that function changes the credit key as well as the counter
key.

**Phase 3 (shared counters).** `_FOLLOWUP` must move with `_WINDOWS`, and the
`P_w + A_w ≤ R` bound from the previous round depends on credits and counters
living in the **same** scope. New for this round: Phase 3 must also preserve
**one derivation, two callers**. Any design that recomputes the job id on the
shared-state side reintroduces the disagreement this commit removed.

**Phase 4's own leftovers.** `ANON_INTAKE_LIMIT` and `ANON_ANALYZE_LIMIT` are
both 10, both binding, and unasserted; `ANON_ANALYZE_SESSION_LIMIT` must stay
strictly below `ANON_ANALYZE_LIMIT` or the tier is inert. One import-time
assert covers both.

**Phase 5 (the funnel).** Four things:

- **Instrument the intake refusal, not just the analyze one** (§8.3). The
  first wall a real lab meets is intake, and a funnel behind the analyze wall
  is never reached by the population it is for.
- **The per-IP refusal *is* honestly answerable with "sign in"** — signing in
  bypasses both tiers (§8.3). This reverses the previous round's guidance.
- `REASON_SESSION_LIMITED` is still consumed by nothing but tests; the front
  end renders `msg`/`error` and never reads `reason`.
- The cookie-less population now gets `_NO_SESSION_MESSAGE` instead of the
  sign-in lie. That is the right fix and it landed here.

**Phase 6 (observability).** Add one metric this round makes necessary:
**count credit redemptions**, not just refusals. If production ever stops
setting `Content-Length` on `/scout/analyze` (§2.5), every credit silently
goes unredeemed, capacity halves, and *no refusal rate changes* — the failure
is invisible in every metric currently planned. Also: alert on
`session_rate_limited` **excluding** `_NO_SESSION_KEY`, or the metric is
sprayable.

---

## 12. Numbers I could not reproduce

| Claim | Status |
|---|---|
| refused 18 MB body `0.336 s → 0.074 s` | **Not reproduced as stated.** Direction and magnitude confirmed and better: TTFB `0.2489 s → 0.0036 s` (Content-Length), `0.1308 s → 0.0053 s` (chunked), median of 5, real sockets (§2.3). |
| "nine mutations, all RED, including **both** of QC's survivors" | **Does not hold.** The body-side `.strip()` is RED; the **query-side is still GREEN** (§5). I did not re-run the builder's nine; my own 17 gave 12 RED / 5 SURVIVED. |
| CPU-s per pipeline run (~9) and per finalise (~6) | **Not re-measured** — `freesasa` is not installable here. Taken from Phase 0 and used only as a multiplier on invocation counts I did measure. |
| Worst adversarial spend ~300 CPU-s/IP/window | Taken from the previous round; not re-derived. |
| Any statement about the **production** worker | **Unverifiable here.** gunicorn is POSIX-only and `sys.platform == "win32"`. §2.4's Werkzeug artifact and §2.5's proxy risk are both untestable without production, which was out of bounds. |
| Phase 0's `95-284` NAT population | Taken as given; out of scope. |
