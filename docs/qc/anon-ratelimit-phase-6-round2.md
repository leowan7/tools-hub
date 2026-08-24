# QC round 2 — anonymous rate limiting, Phase 6 (the FIXES)

**Reviewed:** the fix delta **`3d22904..7062a24`** on `feat/anon-phase6-observability`
**Branch tip:** **`7062a24`** (`7062a242459b0aa15155f76b63b7fb7ebb25cd52`)
**Round-1 report reviewed:** `3d22904` (`docs/qc/anon-ratelimit-phase-6.md`)
**Whole-branch base:** `d3c60c8` (`d3c60c845c19e5a2dc5465ac2ea63b14e1aaefb8`)
**Date:** 2026-08-23
**Reviewer:** second independent QC agent. Did not build Phase 6, did not run
round 1, did not write these fixes.

## Verdict

**PASS WITH FINDINGS.**

All five fixes do what their commit messages claim, and I reproduced every
numeric claim in the fixer's appendix exactly — suite count, both mutation
red-proofs, and the workflow's parsed step order. M1, M2, L1, L2 and L7 are
genuinely closed. The only production-code change in the entire delta is a
docstring, so nothing round 1 passed could have been disturbed by behaviour.

Three findings, one of which is a **regression introduced by this delta**: the
M1 fix changed what happens when `METRICS_TOKEN` is unset, and the one document
that tells the human who must set it what happens if he doesn't
(`docs/HANDOFF-2026-08-18-anon-rate-limiting.md:325-328`) still describes the
old behaviour. It promises a red build that will now never appear.

None of the three block merging. M3 should be a one-line edit before merge
because it is the forcing function for the pending manual action.

---

## Suite, measured first-hand

Repo venv by absolute path
(`C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe`),
`-m pytest -q` from the worktree root, **no path argument**, in a worktree I
created under the session scratchpad. pytest's own exit code, captured with
`$?` immediately after the run — not a pipeline's, and never piped through
`tail`.

| Side | SHA | Result | pytest exit |
| --- | --- | --- | --- |
| Branch tip | **`7062a24`** | **5834 passed, 21 skipped** in 314.05s | **0** |

**Matches the handed-down baseline for `7062a24` exactly.** Delta vs
`d3c60c8` (5796 passed / 21 skipped, taken as given): **+38 passed, 0 failed,
skips unchanged**. No flakes; no re-runs needed. Neither of the two known-flaky
node tests appeared.

Worktrees created by me, both under the session scratchpad, both detached:
`qc6r2` (suite + report) and `qc6r2mut` (mutations, so mutation churn could not
collide with the running suite). The main tree and the session's `p6` / `goal2`
worktrees were not touched; nothing was pushed, PR'd or merged.

---

## Scope observation that narrows everything

```
$ git diff 3d22904..7062a24 --stat -- '*.py' ':!tests/*'
 shared/metrics.py | 11 +++++++----
```

The **only** production-code change in the whole fix delta is the
`observe_scout_refusal` docstring (L2). Everything else is one workflow file,
two test files and three markdown files. Round 1's passed behavioural items
therefore cannot have been broken by a code change — only by a test change, and
the suite is green. This is what item 4 below rests on.

---

## Findings

### M3 — MEDIUM. **New regression.** The handoff still promises a red build that will never happen

`docs/HANDOFF-2026-08-18-anon-rate-limiting.md:325-328`:

> **Leo must set `METRICS_TOKEN` as a Railway service variable and add the same
> value as a `METRICS_TOKEN` repository secret**, or the new smoke step fails
> the job — deliberately, since a monitor that goes quiet when its own plumbing
> breaks is the failure this phase exists to stop.

`efcaf75` made the second half false. An unset `METRICS_TOKEN` no longer fails
the job — I executed the step body and it exits **0** (see the M1 table below).

This is not a stale-prose nit, because of *which* sentence it is. It is the
only place in the repo that states the consequence **to the person who has to
perform the manual action**. As written it tells Leo that skipping the action
produces a red build, i.e. that CI will remind him. It will not: the job goes
green every six hours with a warning annotation. The stale text therefore
manufactures a false expectation of a reminder, which is precisely the
mechanism that turns "not configured yet" into "not configured, indefinitely".

Round 1 explicitly examined this line and cleared it — correctly, at the time.
It was true when round 1 read it. The fix falsified it. That is what makes it a
round-2 finding rather than something round 1 missed.

`ALERTING.md:471` and `:584-586` describe the new behaviour accurately, and the
workflow's own comments do too. The handoff is the only stale one — I swept
every `METRICS_TOKEN` mention in every `.md` in the repo to confirm.

**Fix:** replace the clause after "or" with what now happens — the refusal-rate
check skips with a warning and the job stays green, so nothing will chase this.
One sentence.

### L8 — LOW. **New.** The skip branch exits 1 when `GITHUB_STEP_SUMMARY` is unset

`.github/workflows/synthetic-smoke.yml:145-150`. The heredoc block redirects to
`>> "$GITHUB_STEP_SUMMARY"`. With that variable unset the redirect target is the
empty string, the redirect fails, and under `-e` the shell exits **before**
reaching `exit 0` on line 151:

```
C: METRICS_TOKEN='' and GITHUB_STEP_SUMMARY UNSET
  flags: --noprofile --norc -e -o pipefail
  EXIT: 1
  STDERR: line 10: : No such file or directory
```

Same result under the plain default `bash -e` (row E). **This does not affect
GitHub Actions** — the runner sets `GITHUB_STEP_SUMMARY` for every `run:` step,
so the shipped path is fine, and I verified the intended exit 0 with the
variable set (rows A and B).

What it does mean: the step body cannot be exercised by hand the way the commit
message describes without also exporting `GITHUB_STEP_SUMMARY`, and the branch
silently inverts (skip becomes fail) for any reuse outside Actions — a local
reproduction, a `nektos/act` run, or the step being lifted into a composite
action. Given the whole point of this fix is "an unset new secret must never
fail the job", a shape that fails the job when one environment variable is
missing is worth the one character it costs.

**Fix:** `>> "${GITHUB_STEP_SUMMARY:-/dev/null}"`.

### L9 — LOW. L1's source-inspection test survives a dead-call variant

`tests/test_metrics.py:201-224`. The test asserts the string
`"hmac.compare_digest("` appears in `inspect.getsource(_metrics_token_ok)`.

It kills the realistic mutation (row 4 below: `==`, RED). It does **not** kill a
build where the substring is present but the call's result is discarded (row 5:
a dead `hmac.compare_digest(b"a", b"a")` line above a real `==` — **29 passed,
exit 0**). The test pins the *text*, not the dataflow. The same weakness admits
the more plausible variant of that: a future refactor that switches to `==` and
leaves an explanatory comment mentioning `hmac.compare_digest()` behind.

**Is source inspection acceptable here?** For the property as stated, mostly
yes, and I agree with the fixer's reasoning that a wall-clock timing assertion
on a shared runner is not the answer — it would be too loose to separate `==`
from `compare_digest` on a 64-byte token, or tight enough to flake under load.
That reasoning is sound and I would reject a timing test too.

But the fixer's framing — that timing is the only alternative — is not right.
There is a **behavioural** test that needs no clock and kills both variants.
Patch `compare_digest` to return `True` unconditionally, present the **wrong**
token, and assert the gate answers 200. If the verdict came from
`compare_digest`'s return value, it must; under `==` or a discarded call it
answers 403. I wrote it (15 lines, no timing, no new dependency) and ran it
against both mutations:

| build | source-inspection test | behavioural probe |
| --- | --- | --- |
| `7062a24` as shipped | pass | pass |
| mutation 4 (`==`) | **RED** | **RED** |
| mutation 5 (dead call + `==`) | pass — **survives** | **RED** |

The probe was a throwaway; it is not in this commit. Caveat if it is ever
adopted: it monkeypatches `shared.metrics.hmac.compare_digest`, which is the
shared `hmac` module object, so it must stay scoped to a single request.

This is a *cheaper honest alternative*, not a defect in what shipped. The
shipped test is strictly better than nothing and closes the realistic case.

---

## Item-by-item

### 1. M1 — the deviation. Executed, not read.

The fixer did not do what round 1 asked ("move the guard block below the
smoke"). It deleted the standalone `Guard - METRICS_TOKEN is set` step and
folded the check into `Check Epitope Scout refusal rate`, making an unset token
skip with a warning at exit 0.

**Static parse of the YAML** (`yaml.safe_load`, then the step list):

| check | result |
| --- | --- |
| step order | Checkout, setup-python, Guard RK_LIVE_KEY, **Run Platform API smoke**, **Check Epitope Scout refusal rate** |
| refusal check is the last step | **yes** (index 4 of 5) |
| any step *before* the smoke referencing `METRICS_TOKEN` | **NONE** |
| any `continue-on-error` anywhere | **NONE** |
| job-level `env` | `RK_LIVE_KEY`, `METRICS_TOKEN` — env only; sets no step's exit |

**Execution of the actual step body**, extracted from the YAML by
`yaml.safe_load` (not retyped), run under `bash --noprofile --norc -e -o
pipefail` — GitHub's documented flags for `shell: bash` — with the worktree as
cwd:

| # | condition | exit | observed |
| --- | --- | --- | --- |
| A | `METRICS_TOKEN` unset, `GITHUB_STEP_SUMMARY` set | **0** | `::warning title=Refusal-rate check SKIPPED::…`; summary file written with the 4-line block |
| B | `METRICS_TOKEN=""` (what a missing secret actually expands to), summary set | **0** | identical to A |
| C | `METRICS_TOKEN=""`, `GITHUB_STEP_SUMMARY` **unset** | **1** | warning printed, then `line 10: : No such file or directory` — **L8** |
| D | token set, `METRICS_URL` → closed port | **1** | `ERROR: could not reach …ConnectionRefusedError` |
| E | same as C under plain `bash -e` | **1** | same |
| F | `METRICS_TOKEN="   "` (whitespace) | **2** | falls through `-z`, script's own `.strip()` rejects it |

And against a real socket (stdlib `HTTPServer` on an ephemeral port, the same
extracted step body):

| condition | exit | observed |
| --- | --- | --- |
| token set, `/metrics` answers **403** | **1** | `ERROR: … answered HTTP 403. 403 means METRICS_TOKEN does not match…` |
| token set, refusal share **50%** vs 0.20 threshold | **1** | full per-reason split, then `FAIL: Epitope Scout is refusing more anonymous traffic…` |
| token set, refusal share 1% (sanity) | **0** | `OK` |
| token set, 200 with an empty body | **1** | `ERROR: … answered 200 with no parseable samples (0 bytes)` |

**The fold did not make the check unfailable.** Every failure mode round 1
cared about still exits non-zero; only the unset-token case was converted to a
skip. `scripts/smoke_platform_api.py` is unaffected in every row — it runs in
the step above and nothing in the refusal step can reach it.

Row F is worth naming though it is not a finding: the workflow's emptiness test
(`[ -z ]`) and the script's (`.strip()`) disagree, so a whitespace-only secret
falls past the skip and **fails the job** with a message reading "METRICS_TOKEN
is not set". Confusing, but it fails loud and a whitespace secret is a genuine
misconfiguration rather than a pending action, so the behaviour is arguably
correct. Leave it.

#### Verdict on the deviation

**Accept it. It is better than what round 1 asked for** — and it is incomplete
in a way round 1's version would also have been.

Round 1's literal instruction (move the guard below the smoke) fixes the
ordering but not the disease. The job would go **red every six hours, forever**,
until Leo sets the secret. GitHub's failure email is the entire alerting
mechanism here, and that email would be indistinguishable at the inbox from a
real Scout outage. Within about two cycles the operator learns that the
synthetic-smoke email means nothing, and Tier 2 is dead in the way that
actually kills monitors. The fixer was right to refuse it.

**But the trade is real and the fixer named it honestly.** With the token never
set, the job is green forever and the only trace is a `::warning::` annotation.
GitHub does not email on annotations. So, answering the question directly:

> Is the failure detectable by anyone not reading annotations on a green run?

**No.** Nothing emails, nothing goes red, no test asserts the secret exists, and
the scheduled runs at 00/06/12/18 UTC are not opened by a human in the ordinary
case. Round 1's M1 was "the new check kills the old alarm"; the fix has traded
it for "the new check never runs and nothing says so." That is a strictly
smaller failure — a monitor that was never turned on is better than a monitor
that turns another one off — but it is not zero, and it is *permanent* by
default rather than self-resolving.

What makes it acceptable is that the un-configured state is **short-lived by
intent and documented in two places** (`ALERTING.md:471` and the new runbook at
`:584`). What makes it not fully closed is **M3**: the single document that
creates urgency for the manual action now understates the cost of not doing it,
so the "short-lived by intent" premise is unfunded.

Cheapest honest closure, if one is wanted beyond fixing M3 — a deadline, not a
redesign: keep the skip, but fail once the skip has been running past a date.
Three lines in the same `if`, turning "silent forever" into "silent until a
date I chose, then loud". I am not calling for it; fixing M3 is enough to make
the state visible to the one person who can clear it.

### 2. M2 — the new test really kills the hoist, and is not vacuous

`tests/test_scout_refusal_metrics.py:288-322`
(`test_a_progress_run_that_is_not_shed_counts_no_busy_refusal`).

I applied the hoist myself (rows 1-2 of the mutation table), proved each landed
with `git diff --unified=1`, and confirmed RED. Reverted to
`git status --porcelain` empty both times.

- **Row 1**, the exact hoist round 1 described (out of `_slotted`, up to the
  view body): **1 failed, 11 passed, exit 1** — and only the new test failed,
  which independently reproduces round 1's finding that the other eleven cannot
  see it. Reverted: **12 passed, exit 0**.
- **Row 2**, a subtler variant I added: keep the increment inside the generator
  but move it *above* the `with anon_compute_slot(...)`, i.e. before the shed
  decision exists. This is the more plausible accidental refactor, since it
  still "looks like" it is in the right place. Also **1 failed, 11 passed,
  exit 1**.

**Vacuity.** Row 3 breaks the fixture so `run_pipeline` raises. The test does
**not** pass silently on the resulting error frame — the guard assertion fires
with a diagnostic naming the failure:

```
AssertionError: the fixture did not produce a successful run, so this test is
not exercising the not-shed path: 'data: {"stage": "error", "msg": "Analysis
failed. Check that the PDB is valid and try again."}\n\n'
```

So the `"stage": "done"` assertion is load-bearing, and a future fixture rot
that stops the stream reaching `done` is reported as such rather than
converting the test into a no-op. This is the check that matters most, because
a vacuous version of this test would not detect the hoist at all while looking
like it does.

**Which code path it exercises.** `_generate` has two branches — a gevent one
and a stdlib one. `gevent` is importable in neither the test venv nor
`requirements.txt`, so **the stdlib branch is both the tested and the production
path**. The test is not pinning a branch production does not take. (It is also
moot for what M2 pins: the increment lives in `_slotted`, outside `_generate`
entirely.)

### 3. L1 — see finding L9 above

Rows 4 and 5 of the mutation table. Kills the realistic mutation, survives the
dead-call variant. Verdict and the cheaper alternative are in L9.

### 4. Did the fixes break anything round 1 passed?

No. The scope observation above does most of the work — the only production
change is a docstring — but I spot-checked each named area rather than
inferring it:

- **The six increment sites.** All six still present, and I re-derived their
  structural classification with an AST pass (enclosing-function chain, plus a
  *direct*-yield check that does not confuse a nested generator for the
  enclosing function):

  | site | enclosing | innermost is a generator? |
  | --- | --- | --- |
  | `scout/ratelimit.py:753` | `_refuse` | no |
  | `scout/routes.py:325` | `_anon_capacity_error` | no |
  | `scout/routes.py:337` | `_anon_capacity_error` | no |
  | `scout/routes.py:954` | `analyze` | no |
  | `scout/routes.py:1302` | `progress` | no |
  | `scout/routes.py:1400` | `progress > _slotted` | **yes** — wrapped at `scout/routes.py:1406` |

  This **confirms the new L2 docstring's factual claim**: five non-generator
  sites and one generator site that is inside `stream_with_context`. I also
  traced the two helpers to make sure neither is reachable from an unwrapped
  generator — `_refuse` is called only from the `anon_rate_limit` wrapper body
  (`scout/ratelimit.py:875`, `:892`) and increments *before* building the SSE
  response (`:753`, above the `if sse:`); `_anon_capacity_error` is called from
  three view bodies (`scout/routes.py:731`, `:828`, `:871`). No site needs the
  guard today, exactly as the docstring now says.

- **The token gate's 403/200 table.** All seven gate tests still present
  (`tests/test_metrics.py:107`, `:117`, `:131`, `:140`, `:157`, `:169`, `:182`),
  including both non-ASCII cases —
  `test_metrics_denies_a_non_ascii_token_without_a_500` (`:157`) and
  `test_metrics_accepts_a_non_ascii_token_when_it_is_the_configured_one`
  (`:169`) — plus `test_the_metrics_gate_no_longer_reads_the_forwarded_header_at_all`
  (`:398`). `tests/test_metrics.py`: **29 passed, exit 0**.
- **The consumer's arithmetic.** `tests/test_check_refusal_rate.py`: **21
  passed, exit 0**. The live-socket rows in the M1 table above also exercise the
  real arithmetic end-to-end (50/100 fires, 1/100 does not).

### 5. Docs (L7 / L2)

- **Appended, not rewritten — verified by numstat, which is the only way to be
  sure.** All three doc files in the delta are **purely additive**:

  ```
  57  0  ALERTING.md
  26  0  docs/qc/anon-load-baseline.md
  60  0  docs/qc/anon-ratelimit-phase-6.md
  ```

  Zero deletions in each. The historical QC record (`anon-load-baseline.md`) has
  exactly one hunk, `@@ -555,3 +555,29 @@`, at the tail — nothing above it was
  touched. Round 1's own report likewise gained a closure appendix under a
  "Nothing above this line was edited" header, and the numstat confirms that is
  literally true.
- **The stale `has_request_context()` justification is gone.** The old docstring
  claimed a current site lives in an unwrapped generator; the new one says no
  current site needs the guard and keeps it for a hypothetical seventh. AST-
  verified correct (table above). One prose imprecision, not a finding: it says
  "five sit in view bodies", and three of those five sit in helper functions
  (`_refuse`, `_anon_capacity_error` ×2) called synchronously *from* view
  bodies. The load-bearing claim — all six run inside a live request context —
  holds.
- **`METRICS_ALLOWED_CIDR` really is dead.** Zero references in any `.py`,
  `.yml`, `.yaml`, `.toml` or `Dockerfile*` in the repo; the five remaining
  mentions are all in markdown. The correction note's claim that "the variable
  is read by no code" is accurate.
- **No prose-asserting test broke, because none exists here.** Nothing under
  `tests/` or `scripts/` references `ALERTING.md`, `anon-load-baseline` or
  `METRICS_ALLOWED_CIDR`. The doc changes were untestable and therefore
  unbreakable — which is also why M3 survived to be found by reading.

### 6. Anything new the fixer introduced

Four areas in one pass; I looked for the usual costs of that.

- **Flakiness — none found.** `tests/test_scout_refusal_metrics.py` +
  `tests/test_metrics.py` run five consecutive times: **41 passed** every time,
  3.79-3.93s, zero variance in outcome.
- **Test interdependence — none.** Both new tests pass **in isolation**
  (`::test_a_progress_run_that_is_not_shed_counts_no_busy_refusal` alone: 1
  passed; `::test_the_token_comparison_is_constant_time` alone: 1 passed) and in
  full-suite order (5834 passed).
- **Counter leakage — checked specifically, since `prometheus_client` counters
  are process-global and this file is the real hazard.** Every one of the 22
  `_refusals(...)` call sites in `tests/test_scout_refusal_metrics.py` is part
  of a before/after **delta**; there is not a single absolute-value
  assertion in the file (`grep -c "_refusals("` reports 23 lines, one of which
  is the helper's own `def` at `:52`). The
  new test reads `before` and asserts `== before`, so it is correct at any
  starting value — visible in the row-1 mutation failure, which reported
  `assert 2.0 == 1.0`, i.e. the counter was already at 1.0 from an earlier test
  in the same file and the delta logic handled it.
- **tmp_path / repo-tree leakage — none.** The `not_shed` fixture stages the job
  dir under `tmp_path` and patches `_resolve_job_dir` / `_find_input_file`, so
  the real `_remove_derived_result_files(pdb_path.parent)` that runs on success
  operates on `tmp_path`. Verified empirically, not by reading:
  `git status --porcelain` empty before and after running the file, and `tmp/`
  unchanged (only the tracked `calibration/`). Given this repo's history of a
  reaper walking the shared `tmp/`, that check was worth running.

---

## Mutation table

My own mutations, not the fixer's. Every row applied by byte-level exact-string
replacement with an **occurrence-count assertion** (`count == 1`, else the
script refuses and the row is never scored), CRLF-aware because every file here
is CRLF on disk and a naive text write would have rewritten every line ending.
`git add -A` before every row, then `git diff --unified=1` **printed before the
test ran** — the landed lines are reproduced below — then reverted with
`git checkout --` and `git status --porcelain` asserted empty before the next.

**The refuse-on-mismatch trap fired once, as designed.** Row 4's first pattern
was written as a single-line `return hmac.compare_digest(presented, expected)`;
the real call is four lines. The script reported *"REFUSED compare-eq: pattern
occurs 0 times, wanted 1"* and scored nothing. Had it been a `sed`, it would
have silently no-op'd and the row would have been recorded as a survivor —
which is exactly the false PASS this repo has shipped twice before.

| # | mutation | target | landed diff | tests run | result |
| --- | --- | --- | --- | --- | --- |
| 1 | hoist the busy increment out of `_slotted()` to the view body | `scout/routes.py:1374` / `:1400` | `+    observe_scout_refusal(REASON_BUSY)` above `def _slotted():`; `-                observe_scout_refusal(REASON_BUSY)` inside | `test_scout_refusal_metrics.py` | **RED** — 1 failed, 11 passed, **exit 1** |
| 2 | move the increment inside `_slotted()` but **above** `with anon_compute_slot(...)` | `scout/routes.py:1384` / `:1400` | `+        observe_scout_refusal(REASON_BUSY)` above the `with`; same removal | `test_scout_refusal_metrics.py` | **RED** — 1 failed, 11 passed, **exit 1** |
| 3 | break the `not_shed` fixture: `_fake_pipeline` raises | `tests/test_scout_refusal_metrics.py:122` | `-if progress_callback: … return None` / `+raise RuntimeError(...)` | `test_scout_refusal_metrics.py` | **RED** — 1 failed, 11 passed, **exit 1**, failing on the `"stage": "done"` guard (non-vacuity proven) |
| 4 | `hmac.compare_digest(...)` → `==` on the same two byte strings | `shared/metrics.py:245-248` | `-return hmac.compare_digest(\n  presented.encode(…),\n  expected.encode(…),\n)` / `+return presented.encode(…) == expected.encode(…)` | `test_metrics.py` | **RED** — 1 failed, 28 passed, **exit 1** |
| 5 | keep a **dead** `hmac.compare_digest(b"a", b"a")` line, decide with `==` | `shared/metrics.py:245-248` | `+hmac.compare_digest(b"a", b"a")` above `+return presented.encode(…) == expected.encode(…)` | `test_metrics.py` | **SURVIVES** — 29 passed, **exit 0** → finding **L9** |
| — | row 4's first pattern (single-line form) | `shared/metrics.py` | *did not apply* — 0 occurrences | — | **not scored** |

Rows 1, 2 and 3 reverted to a clean tree and re-ran green (12 passed, exit 0).
Rows 4 and 5 reverted to a clean tree; `test_metrics.py` 29 passed, exit 0.

---

## What the fixer claimed that I could NOT reproduce

One, and it is conditional rather than wrong.

`efcaf75`'s commit message says: *"The step body was then executed under
`bash --noprofile --norc -e -o pipefail`: with METRICS_TOKEN="" it exits 0."*
That holds **only if `GITHUB_STEP_SUMMARY` is also set**. With the token empty
and `GITHUB_STEP_SUMMARY` unset — the state of any shell outside Actions, and
therefore the default state of whatever the fixer ran in unless it exported the
variable — the same body exits **1** (finding L8). The claim is true of the
environment that matters (the runner always sets it) and I confirmed the exit 0
in rows A and B, so this is an under-specified verification note rather than a
false claim.

Everything else reproduced exactly: the parsed step order and the absence of any
`METRICS_TOKEN` reference before the smoke; mutation 10 giving 1 failed / 11
passed / exit 1 and 12 passed / exit 0 reverted; mutation 12 giving 1 failed /
28 passed / exit 1 and 29 passed / exit 0 reverted; and **5834 passed, 21
skipped, exit 0** at `7062a24`.

---

## Recommendation

**Merge.** M1 and M2 are properly closed and the deviation on M1 is the better
engineering call, not a shortcut. Fix **M3** first — one sentence in
`docs/HANDOFF-2026-08-18-anon-rate-limiting.md:325-328`, because it is the only
thing that will get `METRICS_TOKEN` actually set, and the M1 fix is only
acceptable on the premise that it will be. **L8** is a one-character change
worth taking in the same pass. **L9** is fit for follow-up or for never.
