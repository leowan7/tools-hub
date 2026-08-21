# QC round 2 — the tests, and the front end

Scope: `tests/test_scout_chain_scoped_results.py` (new, 21 tests), the
`tests/test_scout_anonymous_access.py` edit, and `templates/scout/index.html`.
I did not write the code under review.

Environment: worktree `.claude/worktrees/suspicious-dewdney-13b07e`,
HEAD = main = `7fd180d`, interpreter `venv/Scripts/python.exe` from the repo
root, `freesasa` absent locally. All mutations were applied with an explicit
`newline=""` round-trip and each one was confirmed to have landed
(`git diff --stat` changed) before any conclusion was drawn from it.

**Every production and test file is restored.** Proof at the bottom.

---

## Verdict

The fix works, the full suite is green (`5294 passed, 20 skipped`), and the two
headline behaviours are genuinely pinned. But the test file is **thinner than
it looks**: of the 18 behaviours I reverted one at a time, **9 are caught by no
test at all**, including three of the four "chain id validated at the route
boundary" call sites the change advertises. One test —
`test_sse_route_rejects_it` — is **vacuous**: it passes identically with and
without the guard it claims to pin.

The front end has **four confirmed defects**, two of them new dead ends the
back-end change created.

---

## Part 1 — are the tests real?

### The "RED before, GREEN after" claim: TRUE, and I reproduced it

`git checkout HEAD -- scout/flags.py scout/pipeline.py scout/routes.py
templates/scout/index.html` (production source back to main, both test files
kept), then the scout suites:

```
18 failed, 63 passed, 1 skipped, 5232 deselected

FAILED tests/test_scout_anonymous_access.py::TestAnonymousCanRunScout::test_analyze_scores_the_example
FAILED tests/test_scout_anonymous_access.py::TestAnonymousCanRunScout::test_results_are_readable_back
FAILED tests/test_scout_anonymous_access.py::TestAnonymousJobsAreNotEnumerable::test_another_anonymous_session_cannot_read_the_job
FAILED tests/test_scout_anonymous_access.py::TestConcurrencyBound::test_signed_in_users_never_consume_a_slot
FAILED tests/test_scout_anonymous_access.py::TestConcurrencyBound::test_slot_is_released_after_a_run
FAILED tests/test_scout_chain_scoped_results.py::TestAnalyzeIsChainScoped::test_chain_b_after_chain_a_returns_chain_b
FAILED tests/test_scout_chain_scoped_results.py::TestAnalyzeIsChainScoped::test_chain_b_actually_runs_the_pipeline
FAILED tests/test_scout_chain_scoped_results.py::TestAnalyzeIsChainScoped::test_a_pre_fix_results_csv_is_a_miss_not_a_wrong_answer
FAILED tests/test_scout_chain_scoped_results.py::TestDerivedFilesAreChainScopedToo::test_top3_download_never_serves_the_previous_chain
FAILED tests/test_scout_chain_scoped_results.py::TestKnownBinderOverlapsAreChainScoped::test_explicit_residues_do_not_inherit_another_chains_binders
FAILED tests/test_scout_chain_scoped_results.py::TestChainIdIsValidatedAtTheBoundary::test_json_routes_reject_it[=cmd|calc!A1]
FAILED  ... [+A] [-A] [@A] [A,B] [../etc] [AAAAAAAAA]
FAILED tests/test_scout_chain_scoped_results.py::TestFeasibilityIsChainScoped::test_epitope_id_is_not_resolved_against_another_chain
```

13 of the 21 new tests go red pre-fix. The 8 that do not are the deliberate
regression guards (`test_same_chain_twice_still_reuses_the_cached_run`,
`test_real_chain_ids_still_work`, `test_epitope_id_still_resolves_for_the_matching_chain`,
`test_flags_column_list_matches_the_pipeline`, `test_json_routes_reject_it[""]`)
— **and the three `test_sse_route_rejects_it` params, which are not guards but
a vacuous test.** See below.

### The mutation table

Each row: I reverted exactly that one behaviour in the production source,
confirmed the edit landed, re-ran `pytest -q -k scout` (145 passed / 3 skipped
clean), then restored.

| # | Behaviour reverted | Where | Tests that caught it | Verdict |
|---|---|---|---|---|
| M1 | pipeline stamps `chain_id` into each row | `scout/pipeline.py:491` | **0 locally.** In CI: `test_analyze_runs_the_real_pipeline` only | CI-ONLY |
| M2 | `/scout/analyze` **cache gate** is chain-scoped | `scout/routes.py:637` | 3 — `test_chain_b_after_chain_a_returns_chain_b`, `test_chain_b_actually_runs_the_pipeline`, `test_a_pre_fix_results_csv_is_a_miss_not_a_wrong_answer` | COVERED |
| M3 | `/scout/analyze` **CSV reader** is chain-scoped | `scout/routes.py:690` | **0** | UNTESTED |
| M4 | `_get_binder_overlaps` chain gate | `scout/routes.py:368` | 1 — `test_explicit_residues_do_not_inherit_another_chains_binders` | COVERED |
| M4b | `_get_binder_overlaps` returns `[]` **always** | `scout/routes.py:347-` | **0** | UNTESTED (no positive control) |
| M5a | delete `epitopes_annotated.csv` when top-3 empty | `scout/routes.py:800-801` | 1 — `test_top3_download_never_serves_the_previous_chain` | COVERED |
| M5b | delete `epitopes.csv` when top-3 empty | `scout/routes.py:821-822` | 1 — same test | COVERED |
| M6a | `_valid_chain` on `POST /scout/analyze` | `scout/routes.py:621` | 7 — every non-empty `test_json_routes_reject_it` param | COVERED |
| M6b | `_valid_chain` on `GET /scout/progress` (SSE) | `scout/routes.py:919,923` | **0** | UNTESTED — the test that claims to cover it is vacuous |
| M6c | `_valid_chain` on `POST /scout/feasibility/analyze` | `scout/routes.py:1038` | **0** | UNTESTED |
| M6d | `_valid_chain` on `GET /scout/feasibility/progress` (SSE) | `scout/routes.py:1137,1139` | **0** | UNTESTED |
| M7 | chain-specific SSE error message (round-1 D3) | `scout/routes.py:1172-1178` | **0** | UNTESTED |
| M8a | `/scout/feasibility/analyze` results.csv chain gate | `scout/routes.py:1052` | 1 — `test_epitope_id_is_not_resolved_against_another_chain` | COVERED |
| M8b | `/scout/feasibility/progress` results.csv chain gate | `scout/routes.py:1150` | **0** | UNTESTED |
| M9 | an unstamped (legacy) CSV is a MISS | `scout/routes.py:342` | 1 — `test_a_pre_fix_results_csv_is_a_miss_not_a_wrong_answer` | COVERED |
| M10 | `flags.py` ↔ `pipeline.py` column-list sync | `scout/flags.py:17` | 11, incl. `test_flags_column_list_matches_the_pipeline` | COVERED |
| M11 | a header-only CSV is a MISS | `scout/routes.py:342` | **0** | UNTESTED |
| M12 | template passes the **response** chain to `renderEpitopeTable` | `templates/scout/index.html:541` | **0** | UNTESTED |

**9 of the 18 mutations were caught by nothing**: M3, M4b, M6b, M6c, M6d, M7,
M8b, M11, M12. The load-bearing ones are M6b/M6c/M6d — three of the four
"validated at 4 route boundaries" call sites the change advertises — plus M8b,
the feasibility SSE chain gate, which is the path the browser opens *first*,
and M7, the round-1 D3 message the change exists to deliver.

The ones I would not lose sleep over: M3 (redundant while M2 stands — a
different chain always forces a rescore, so the reader never sees a mismatched
CSV), M11 (a header-only CSV is unreachable today), M12 (no JS harness exists
in this repo at all).

Representative output, M6b:

```
$ python mutate.py scout/routes.py m6b.old m6b.new
MUTATION APPLIED to scout\routes.py (newline='\r\n')
$ pytest -q -rf --tb=no -k scout
145 passed, 3 skipped, 5166 deselected, 22 warnings in 50.29s
```

### `test_sse_route_rejects_it` is VACUOUS — CONFIRMED

It asserts only `payload["stage"] == "error"` and never looks at the message.
With the boundary guard removed, `/scout/progress` still emits `stage: error`
as its *first* event — because `run_pipeline` raises `Chain 'X' not found` before
any progress callback fires. Executed with M6b applied:

```
/scout/progress chain='=cmd|calc!A1' -> {'stage': 'error', 'msg': "Chain '=cmd|calc!A1' not found in structure. Available chains: A, B"}
/scout/progress chain='A,B'          -> {'stage': 'error', 'msg': "Chain 'A,B' not found in structure. Available chains: A, B"}
/scout/progress chain='AAAAAAAAA'    -> {'stage': 'error', 'msg': "Chain 'AAAAAAAAA' not found in structure. Available chains: A, B"}
```

Same three params, same assertion, guard absent — green. The test also passed
on unmodified `main` (see the pre-fix run above: the eight
`test_json_routes_reject_it` failures are listed, the three
`test_sse_route_rejects_it` params are not). Asserting the message would fix
it; the boundary guard's whole point is that the request is refused **before**
`run_pipeline` is entered and billed.

`test_json_routes_reject_it` is honest but mis-named: "json routes" is plural,
it only hits `/scout/analyze` (M6c proves `/scout/feasibility/analyze` is
untouched by it).

### The tests are NOT tautological — checked

* No assertion anywhere in the new file touches `response["chain"]`
  (`grep -n '\["chain"\]\|get("chain")' tests/test_scout_chain_scoped_results.py`
  → no matches). The brief's concern does not apply.
* The evidence is the residue numbers, and they are honest: the stub maps
  chain → a fixed distinct residue set (`A` → 10-16, `B` → 60-66), it does not
  echo the request. On a wrong cache hit the route reads the *other* chain's CSV
  and the wrong numbers come back — which is exactly what happened pre-fix.
* `test_same_chain_twice_still_reuses_the_cached_run` is a real negative
  control: without it, "always rescore" would pass the chain-scoping tests.

**One missing control (M4b) — CONFIRMED.**
`test_explicit_residues_do_not_inherit_another_chains_binders` asserts
`known_binder_overlaps == []`. Nothing asserts a **non-empty** result for the
matching chain, so `_get_binder_overlaps` can be gutted entirely and the suite
stays green:

```
$ python mutate.py scout/routes.py m4b.old m4b.new   # `return []` as the first statement
MUTATION APPLIED to scout\routes.py (newline='\r\n')
$ pytest -q -rf --tb=no -k scout
145 passed, 3 skipped, 5166 deselected, 22 warnings in 45.11s
```

Classic assert-empty trap: the feature could silently stop working and this
test would still "prove" it is chain-scoped.

### The `stub_scoring` / `_write_results_csv` edit MASKS NOTHING — checked

I stamped the wrong chain into it (`chain: str = "A"` → `"Z"`) and re-ran:

```
FAILED tests/test_scout_anonymous_access.py::TestAnonymousCanRunScout::test_analyze_scores_the_example
FAILED tests/test_scout_anonymous_access.py::TestAnonymousCanRunScout::test_results_are_readable_back
FAILED tests/test_scout_anonymous_access.py::TestConcurrencyBound::test_signed_in_users_never_consume_a_slot
3 failed, 57 passed, 1 skipped
```

So the stamp is load-bearing, not decorative: without a matching chain those
three fall through into the real freesasa pipeline and 500. The other two that
failed pre-fix (`test_another_anonymous_session_cannot_read_the_job`,
`test_slot_is_released_after_a_run`) survive a wrong chain because their
assertions never depended on `/analyze` succeeding — they were already weak in
that way before this change, so nothing *stopped* exercising what it was
written for. `test_slot_is_released_when_the_pipeline_raises` still pre-writes
nothing and still reaches `run_pipeline`. `test_analyze_runs_the_real_pipeline`
still pre-writes nothing, so CI still runs the true path.

### freesasa: exactly one gated test, and it would PASS in CI

`pytest -rs -k scout` skips:

```
SKIPPED [1] tests\test_rls.py:51: SUPABASE_URL / SUPABASE_ANON_KEY not configured
SKIPPED [1] tests\test_rls.py:68: SUPABASE_URL / SUPABASE_ANON_KEY not configured
SKIPPED [1] tests\test_scout_anonymous_access.py:195: freesasa is not installed in this environment
```

The two `test_rls` skips are Supabase config, unrelated. `requires_freesasa`
appears exactly once (`tests/test_scout_anonymous_access.py:42`, applied at
`:195`), on `test_analyze_runs_the_real_pipeline`.

I could not run freesasa, so I ran the **real** `run_pipeline` with only
`scout.pipeline.compute_rsa` replaced by Biopython's own `ShrakeRupley` — every
other line, including the CSV writer and the route's cache gate, is production
code:

```
$ python real_pipeline_probe.py static/example/1HEW.pdb A
rows: 6
header has chain_id: True
chain_id values: ['A']
```

and the CI test itself, transplanted verbatim:

```
$ pytest -q ci_sim_test.py
2 passed in 6.35s
```

**So `test_analyze_runs_the_real_pipeline` will pass in CI.** It is also the
*only* thing that catches M1 — with the `chain_id` stamp removed the same two
tests fail:

```
$ python mutate.py scout/pipeline.py m1.old m1.new     # drop "chain_id": chain_id
MUTATION APPLIED
$ pytest -q ci_sim_test.py
2 failed        # analyze returns epitopes: [] — the CSV can no longer name its chain
```

Caveat: this is a stand-in, not freesasa. It proves the chain-stamp path; it
does not prove freesasa's numerics. Ranked as verified-by-equivalent, and
the pipeline does not touch `chain_id` between the parameter and the CSV cell
(`scout/pipeline.py:241` → `:491`, no normalisation).

### `test_flags_column_list_matches_the_pipeline` CAN fail — proved

Not a guard-that-certifies-false. Renaming one entry in `scout/flags.py`:

```
$ pytest -q -k flags_column_list
    assert ['epitope_id'...e_score', ...] == ['epitope_id'...e_score', ...]
FAILED tests/test_scout_chain_scoped_results.py::test_flags_column_list_matches_the_pipeline
1 failed, 5313 deselected
```

and deleting `chain_id` from `flags.py` takes 11 tests with it, the guard
included. It detects drift in both directions of content and in order.

### Cases the 21 tests do not cover (named, not written)

1. `/scout/progress` refusing a bad chain id *before* the pipeline runs
   (assert the message, not just `stage`).
2. `/scout/feasibility/analyze` and `/scout/feasibility/progress` refusing a
   bad chain id at all.
3. `/scout/feasibility/progress` resolving `epitope_id` against the *wrong*
   chain's `results.csv` — the SSE path the UI hits first, and the one that
   burns a feasibility pipeline run on the wrong residues.
4. The round-1 D3 message itself (M7) — the fix's whole user-visible payoff.
5. A **positive** control on `_get_binder_overlaps` (non-empty for the matching
   chain).
6. `/scout/download?full=1` after a chain switch — `results_annotated.csv` is
   rewritten unconditionally today, but nothing pins that.
7. A chain switch where **both** chains qualify (only "B scores nothing" is
   covered).
8. A header-only `results.csv` being a miss (M11), which is what the helper
   docstring promises.
9. Anything at all in `templates/scout/index.html` (M12). There is no JS test
   harness in this repo and no `assert "renderEpitopeTable(epitopes, chain)" in
   body` string check either.

---

## Part 2 — does the front end match the back end?

The one template change is correct as far as it goes — I confirmed by executing
the real page JS that moving `#chain-select` to another value mid-run no longer
changes the feasibility link. It is not sufficient.

### FE-1 (HIGH) — the dropdown offers chain ids the backend now refuses

`templates/scout/index.html:885-891` builds the dropdown straight from
`/scout/upload`'s `chains` list. `scout/parser.py:290` takes the chain id
verbatim from Biopython. `_valid_chain` (`scout/routes.py:312`) is strictly
tighter than the parser:

```
chain col 'A': parser offers ['A']  _valid_chain -> [True]
chain col '1': parser offers ['1']  _valid_chain -> [True]
chain col '_': parser offers ['_']  _valid_chain -> [False]
chain col '-': parser offers ['-']  _valid_chain -> [False]
chain col '.': parser offers ['.']  _valid_chain -> [False]
chain col '*': parser offers ['*']  _valid_chain -> [False]
chain col '|': parser offers ['|']  _valid_chain -> [False]
chain col '+': parser offers ['+']  _valid_chain -> [False]
chain col '=': parser offers ['=']  _valid_chain -> [False]
```

End to end over HTTP:

```
chain '_': dropdown offers ['_'] -> /scout/analyze 400 '{"error":"job_id and a valid chain id are required."}'
chain '-': dropdown offers ['-'] -> /scout/analyze 400 '{"error":"job_id and a valid chain id are required."}'
chain '.': dropdown offers ['.'] -> /scout/analyze 400 '{"error":"job_id and a valid chain id are required."}'
```

Pre-fix those chains reached the pipeline. This is a **new** regression for any
structure whose chain column is not alphanumeric (modelling output, hand-edited
PDBs, mmCIF `auth_asym_id` is a free string). The message blames the user for
choosing what the app itself offered. `' '` (blank chain) was already broken
before this change — `.strip()` made it falsy — so that one is not new.

**Fix:** filter the chain list `/scout/upload` returns through `_valid_chain`
(one `if`), so the app never offers what it will refuse — and say *why* in the
400 (`"Chain 'X' contains characters Scout cannot use."`).

### FE-2 (HIGH) — after any SSE error the Analyze button never comes back

`runAnalysis()` (`:930-931`) sets `btn.disabled = true; btn.textContent =
'Analyzing…'`. Only `_finalizeAnalysis`'s `finally` (`:415-419`) puts it back,
and that runs **only** on the `stage: 'done'` path. `openProgressStream`'s error
branch (`:342-346`) and `onerror` (`:354-361`) call `resetProgress()`, which
touches nothing but the progress bar. Executed against the real page JS:

```
stream opened: /scout/progress?job_id=JOB-1&chain=_
button while running   : { disabled: true, text: 'Analyzing…' }

after the SSE error event:
  analyze-error text     : job_id and a valid chain id are required.
  analyze-error display  : block
  progress-container hid : true
  BUTTON disabled        : true
  BUTTON label           : "Analyzing…"

after an EventSource transport error:
  BUTTON disabled        : true
  BUTTON label           : "Analyzing…"
```

The user reads the error and cannot retry. The only escape is a page reload or
"Reset", which calls `resetAll()` and discards the uploaded job entirely.

This is pre-existing for the other SSE error classes (rate limit, pool busy,
job expired), but the new chain-validation error is a fresh way in — and via
FE-1 it is reachable with a chain the app offered, which makes it a
**permanently stuck page on a legitimate upload**. Answering the brief
directly: the user sees a message *and* a dead button — worse than a spinner,
because the page looks recoverable and is not.

**Fix:** move the button reset out of `_finalizeAnalysis` into
`resetProgress()`, or add the two lines to both error branches.

### FE-3 (MEDIUM) — a chain that scores nothing leaves the previous chain's table on screen next to a dead download

`_handleAnalysisResult` (`:426-463`) shows `#results-section` and both download
buttons **unconditionally** (`:433-437`), but only calls `renderViewer` — the
sole caller of `renderEpitopeTable` — when `epitopes.length > 0` (`:454`).
Nothing else clears `#epitope-table-body` except `resetAll()`, which is bound
only to the Reset button. Executed against the real page JS:

```
[1] chain A table link: href=".../scout/feasibility?job_id=JOB-1&epitope_id=1&chain=A"

[2] after analyze(chain=B) returned epitopes: []
    results-section hidden      : false
    download-link display       : inline-flex
    download-link href          : /scout/download/JOB-1
    epitope table row count     : 7          <-- chain A's row, still there
    stale link                  : href=".../scout/feasibility?...&chain=A"
```

and over HTTP, the same journey:

```
analyze A -> 200, epitopes=1
  files: [... 'epitopes.csv', 'epitopes_annotated.csv', 'results.csv', ...]
analyze B -> 200, epitopes=0
  download (top3)  -> 404 body='{"error":"Results not found. Please run analysis first."}'
  download (full)  -> 200 bytes=439
  files: ['.owner', 'analyze_cache.json', 'input.pdb', 'results.csv', 'results_annotated.csv']
```

So the page now shows: chain A's epitope rows, a "Top 3 CSV" button that 404s,
and no indication anywhere that the analysed chain is B — **the results section
never displays which chain it is showing** (checked the markup at `:160-215`;
there is no chain label, the dropdown is the only cue and the user may have
moved it).

Both download anchors carry a `download="…"` attribute (`:171-172`), so a
browser saves the 404 JSON body as `top3_epitopes.csv` rather than showing an
error. Round 1 flagged the unconditional button (D1's UI half) and it is still
unconditional; the *stale table* is new and was not flagged.

Is it acceptable? No — it is the same class of bug the whole change exists to
kill: output on screen labelled as the current chain that belongs to a
different one. **Fix (three lines):** in `_handleAnalysisResult`, when
`epitopes.length === 0`, clear `#epitope-table-body`, hide `#download-link`,
and say "Chain B produced no epitope above the scoring threshold."

The good news, verified: the stale link now fails **safe**. Clicking it gives

```
/scout/feasibility/progress chain=A : data: {"stage": "error", "msg": "No Epitope Scout results found for chain A on this job. Run epitope analysis on that chain first."}
/scout/feasibility/analyze  chain=A : 404 {"error":"No Epitope Scout results found for chain A on this job. Run epitope analysis on that chain first."}
```

and `feasibility.html:641-645` renders it as
`"Could not auto-load: <msg>. Please upload the structure manually."` The
round-1 D3 fix genuinely reaches the user. That is a real improvement.

### FE-4 (LOW) — `job_id` is still read from the DOM at the moment the table renders

The fix took `chain` from the response payload but left
`document.getElementById('job-id').value` in the same expression
(`:671`). Upload a new file while a run is in flight and the finishing run's
table is built with the **new** job id and the **old** chain:

```
[3] job-id changed to JOB-2 mid-run, old chain A result renders
    link: href=".../scout/feasibility?job_id=JOB-2&epitope_id=1&chain=A"
```

It fails safe today (JOB-2 has no `results.csv` → 404), and the analyze
response carries no bare `job_id` to read instead, so this is a note rather
than a demand. Same for `chainNameMap` at `:504-509`, which labels the viewer
legend from the live dropdown's option text.

The `chain || document.getElementById('chain-select').value` fallback at `:671`
is dead — `data.chain` is always a validated non-empty string. Round 1 said the
same. Drop it or leave it.

### Checked and holds (NOT defects)

* **The dropdown argument is sound for `index.html`.** Every read of
  `#chain-select` in the page: `:504-509` (`.options`, names only, viewer
  legend), `:671` (dead fallback), `:832`/`:884`/`:985`/`:1034-1043`
  (populate/clear), `:923` (`runAnalysis`, read once at the start). No live
  read of `.value` can pair one chain's data with another chain's id.
  Verified by execution — moving the dropdown to `Z` mid-run leaves the link on
  `chain=A`.
* **The four new 400s do not strand the user silently.** Neither SSE route
  returns an HTTP 400 — both degrade in band as `stage: error`, which
  `index.html:342-346` and `feasibility.html:411-414` / `:641-645` both render.
  The two JSON routes' 400/404 bodies are surfaced too
  (`_finalizeAnalysis:405-408`, `fetchFeasibilityResults:441-442`). The
  *message* lands; FE-2 is about what the button does afterwards.
* **JS is syntactically valid after the edit.** Extracted the inline blocks from
  the Flask-**rendered** pages (so Jinja is already substituted), skipping
  `<script src=…>` and `type="application/ld+json"`:
  ```
  /scout/            -> 200   block 2 index_2.js, block 3 index_3.js
  /scout/feasibility -> 200   block 2 feasibility_2.js
  node --check: feasibility_2.js OK, index_2.js OK, index_3.js OK
  ```
* **`fieldnames` unbound-by-luck (round-1 note) is fixed** —
  `scout/routes.py:689` now initialises it.
* **`/scout/download?full=1` is not affected** — `results_annotated.csv` is
  rewritten on every run, and after the chain-B run it contains chain B's rows,
  header included.

### FE-5 (LOW) — the new message is wrong when the chain matches but the epitope_id does not

The new `_msg` at `scout/routes.py:1172-1178` branches on `epitope_id and not
epitope_str`, not on *why* the residues came back empty. Analyse chain A, then
ask for an `epitope_id` that is not a row in chain A's `results.csv`:

```
  chain A analysed, epitope_id=99 (not in the CSV):
    SSE  : data: {"stage": "error", "msg": "No Epitope Scout results found for chain A on this job. Run epitope analysis on that chain first."}
    JSON : 400 {"error":"epitope_residues or epitope_id is required."}
```

The SSE claims chain A has no results when it plainly does, and tells the user
to re-run something that is already done; the JSON route answers the same
condition with a different, equally unhelpful message and a different status.
Hard to reach legitimately (the CSV keeps every patch's original id), so LOW —
but the message should say "epitope N is not in chain A's results" and the two
routes should agree.

### FE-6 (LOW) — the new `unlink` is unguarded and can 500 (Windows only)

Reproduced: download the top-3 CSV, then analyse a chain that scores nothing.
`epitopes_annotated_path.unlink(missing_ok=True)` (`scout/routes.py:801`) sits
outside the route's `try/except`, and on Windows `send_file`'s open handle makes
it raise:

```
PermissionError: [WinError 32] The process cannot access the file because it is
being used by another process: 'tmp\\…\\epitopes_annotated.csv'
```

Production and CI are Linux, where unlinking an open file is legal, so this is
not a prod defect — but it is an unguarded filesystem call on a user-facing
route, and it makes the test suite order-dependent on Windows dev boxes. One
`try/except OSError` around both unlinks removes it.

---

---

## What I would fix before shipping

Blocking:

1. **FE-1** — filter `/scout/upload`'s chain list through `_valid_chain`. The
   app must not offer a chain it will 400. One `if`.
2. **FE-2** — re-enable `#analyze-btn` in `resetProgress()` (or in both SSE
   error branches). Two lines. Without this, FE-1's 400 is a permanently stuck
   page.
3. **FE-3** — in `_handleAnalysisResult`, when `epitopes.length === 0`: clear
   `#epitope-table-body`, hide the Top-3 download, and say which chain scored
   nothing.

Should go in the same commit:

4. Assert the **message** in `test_sse_route_rejects_it`, so it stops being
   vacuous, and extend `test_json_routes_reject_it` to
   `/scout/feasibility/analyze` (M6c) and `test_sse_route_rejects_it` to
   `/scout/feasibility/progress` (M6d).
5. One test for M8b (`/scout/feasibility/progress` must not resolve an
   `epitope_id` against another chain's `results.csv` — it is the path the UI
   opens first, and today it runs a feasibility pipeline on the wrong residues).
6. A positive control on `_get_binder_overlaps` (M4b).

Nice to have: a test for M7's message, the `try/except OSError` of FE-6, and
dropping the dead `chain || …chain-select.value` fallback.

---

## No regressions

Full suite on the unmodified delta, from the repo root, no path argument:

```
$ venv/Scripts/python.exe -m pytest -q
5294 passed, 20 skipped, 854 warnings in 785.24s (0:13:05)
```

Scout subset alone: `145 passed, 3 skipped` (the 3 skips are 2× Supabase
config + 1× freesasa, listed above).

---

## Restoration proof

```
$ git diff --stat
 scout/flags.py                       |   1 +
 scout/pipeline.py                    |   5 ++
 scout/routes.py                      | 124 ++++++++++++++++++++++++++++-------
 templates/scout/index.html           |  11 +++-
 tests/test_scout_anonymous_access.py |  11 +++-
 5 files changed, 125 insertions(+), 27 deletions(-)
```

Matches the brief exactly. md5 of every file also matches the pre-review
snapshot. The only file I added to the repo is this report. All scratch work
(mutation markers, probes, extracted JS) is in the session scratchpad.

