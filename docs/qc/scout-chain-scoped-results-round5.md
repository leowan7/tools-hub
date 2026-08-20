# QC round 5 — chain-scoped Scout results

Independent adversarial review of the changes made in response to round 4. I did
not write any of this code.

Environment: worktree `.claude/worktrees/suspicious-dewdney-13b07e`,
HEAD = main = `7fd180d`, interpreter
`C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe`,
Windows, `freesasa` absent so every route-level probe runs against a stubbed
scorer writing the real CSV format through the real `_CSV_COLUMNS_BASE`.

**All six touched files are restored byte-identical.** Every mutation was applied
at byte level with the file's own terminator (CRLF for the five tracked files, LF
for the untracked test file), md5-confirmed landed before any conclusion was
drawn, and md5-confirmed restored after. Proof at the bottom.

Baseline for every mutation row, measured not quoted: `-k scout` →
**188 passed, 0 failed** (round 4 measured 172; the author has since added 16).
My own runs were strictly serialised — no two of my pytest jobs ever overlapped.
Three *other* sessions ran the full suite concurrently from sibling worktrees;
see the process note at the end for why that could not corrupt these results and
how I checked.

Frontend findings were executed against the **Flask-rendered** page JS (`GET
/scout/`, 38,904 bytes of script) in a node DOM stub, not reasoned about.

---

## Verdict

**DO NOT SHIP — hold for two lines.**

Everything round 4 blocked on is genuinely closed and genuinely mutation-caught.
I attacked `_clearChainScopedResults` as a design, enumerated every writer in the
template, and **could not find a third missing element** — the list is complete
for every element that actually receives chain-specific data. `resetAll` got
strictly *stronger*, not weaker. Round 4's D2, D3, D5, D6 and D7 are all fixed.
The author is **right** on the `?full=1` point they disputed, and their weaker
assertion is load-bearing (M8 dies).

What stops it is D1. The cleanup that keeps the derived CSVs honest was put on
the *return paths of one route*, but `results.csv` has **two writers** —
`analyze()` and `progress()` — and the SSE one has no cleanup at all. So
`/scout/download` serves the **previous chain's** top-3 and all-patches CSVs
from the moment the SSE finishes until `/scout/analyze` completes, on **every
normal chain switch**, and permanently whenever that second request never lands
(tab closed, network drop, the 402 quota redirect, 503 busy, 404, or the 422/500
I reproduced). A comment three lines above the write states this cannot happen.
That is the round-4 shape lesson repeating exactly: the author fixed the return
the report named, not the class the report described. The fix is **two lines** —
one beside each `run_pipeline` call — and it deletes more code than it adds.

---

# PART A — `_clearChainScopedResults` attacked as a design

## Is the list complete? Yes. I walked every writer and found no third miss.

Every `innerHTML =`, `textContent =`, `.hidden =`, `style.display =` and
`appendChild` inside `#results-section`, checked against the 11-line list:

| element | only writer | in the clear list? | verdict |
|---|---|---|---|
| `viewer-container` | `renderViewer:484,492` | yes | ok |
| `epitope-legend` | `renderLegend:563-591` | yes | ok |
| `epitope-table-body` | `renderEpitopeTable:653-699` | yes | ok |
| `flag-reference` / `flag-ref-grid` | `renderFlagReference:712-732` | yes | ok |
| `uniprot-bar` / `uniprot-info` | `_handleAnalysisResult:458-459` | yes | ok |
| `known-binders-section` / `-body` | `renderKnownBinders:787-823` | yes | ok |
| `known-binders-count` | `renderKnownBinders:788` | **no** | inside `#known-binders-section`, which *is* hidden — never visible |
| `ppi-section` / `ppi-table-body` | `renderPpiInterfaces:742-777` | yes | writer **has no call site** — see D3 |
| `ppi-count` | `renderPpiInterfaces:743` | **no** | same, and inside the hidden section |
| `feasibility-note` | `renderEpitopeTable:706` (`hidden=false` only) | **no** | static explainer paragraph, carries no chain data — cosmetic |
| `seq-warning-box` | **nothing** | no | dead markup, no writer anywhere |
| `download-link` / `-full` | `_handleAnalysisResult:442-448` | no — handled in the caller, **both branches** | ok |
| `analyze-error` | `showAnalyzeError:835` | no — cleared by `runAnalysis:975` and `resetAll:890` | ok |

Executed, chain A (1 epitope, `glycan proximity` flag, UniProt `P00001`, 1 known
binder) then chain B (0 epitopes):

```
--- E2 after chain B (0 epitopes) — anything still chain A? ---
  uniprot-bar        hidden=true   EMPTY
  epitope-table-body hidden=false  EMPTY
  epitope-legend     hidden=false  EMPTY
  flag-reference     hidden=true   EMPTY        <- round 4 D2 CLOSED
  flag-ref-grid      hidden=false  EMPTY
  known-binders-section hidden=true EMPTY
  known-binders-count            html='1 structure'   <- stale, but its section is hidden
  feasibility-note   hidden=false  (static text)      <- stale, but chain-agnostic
  analyze-error      display='block' html='No epitopes scored above threshold for chain B…'
```

and chain B scoring 1 epitope with `/scout/pdb` returning 404 — round 4's D3(b),
the permanent case:

```
--- E3 chain B scored 1 epitope but /scout/pdb 404'd ---
  viewer-container   html='Could not load structure for visualization.'
  epitope-table-body EMPTY        <- was CHAIN A in round 4
  epitope-legend     EMPTY        <- was CHAIN A in round 4
  flag-reference     hidden=true  <- was CHAIN A in round 4
```

**Round 4's D2 and D3(b) are CLOSED.**

## Did `resetAll` get weaker? No — strictly stronger. Diffed line by line.

Old `resetAll` cleared 9 things: `epitope-table-body`, `known-binders-body`,
`known-binders-section.hidden`, `ppi-section.hidden`, `ppi-table-body`,
`uniprot-bar.hidden`, `viewer-container`, `epitope-legend`,
`_currentPpiInterfaces = []`. `_clearChainScopedResults` does all 9 **plus**
`uniprot-info.innerHTML`, `flag-ref-grid.innerHTML` and
`flag-reference.hidden`. Nothing was lost. Confirmed by execution (E5):

```
--- E5 after resetAll() ---
  uniprot-bar hidden=true  uniprot-info EMPTY  flag-reference hidden=true
  flag-ref-grid EMPTY  epitope-table-body EMPTY  epitope-legend EMPTY
  viewer-container EMPTY  known-binders-section hidden=true  results-section hidden=true
```

## Does clearing `_currentPpiInterfaces = []` break a consumer? No — see D3.

`let _currentPpiInterfaces = []` is declared at `:318`, cleared at `:873`, and
**never assigned and never read anywhere in the file**. There is no consumer to
break. That is itself the finding.

## Is `flag-reference` still repopulated on a normal run? Yes.

`renderFlagReference` ← `renderEpitopeTable:703` ← `renderViewer:555`, and
`renderViewer` runs whenever `epitopes.length > 0`. Adding it to the clear list
did **not** break the feature. Executed after a clear, chain B with a flag:

```
--- E4 normal run after the clear ---
  flag-reference  hidden=false
  flag-ref-grid   html='<div class="flag-ref-header"><span class="badge badge-red">glycan prox…'
```

## The async question: what actually happens when two analyses overlap? — D2

The clear is synchronous; `renderViewer` is `async` and unawaited, and
`_finalizeAnalysis`'s `finally` re-enables the Analyze button **while chain A's
`/scout/pdb` fetch is still in flight**. So a late-resolving `renderViewer`
*can* paint chain A into a page already cleared and re-rendered for chain B.
Executed, chain A's fetch parked until after chain B fully rendered:

```
--- page after chain B fully rendered ---
   table residues : ALA60,ALA61
   flag-reference : hidden=true  grid=empty
   uniprot-info   : P00002

--- page after chain A late-resolves into it ---
   table residues : ALA10,ALA11        <- CHAIN A, over chain B's page
   flag-reference : hidden=false grid=POPULATED   <- CHAIN A's flag cards
   uniprot-info   : P00002             <- still CHAIN B
```

`_clearChainScopedResults` is a **completeness** guard, not a **concurrency**
guard. It stops "no writer on this path leaves the old chain up"; it does not
stop "a writer from the old chain arrives late". The function's comment reads as
though being first solves the async problem — it solves only half of it. A
generation counter checked after the `await` in `renderViewer` is ~3 lines.

**Reachability is what holds this at LOW**: chain B never hits the cache
(`/scout/progress` runs `run_pipeline` unconditionally), so A's local
`/scout/pdb` fetch would have to outlast B's entire SSE + pipeline + analyze
round trip. Mechanism CONFIRMED; a real-world trigger is UNVERIFIED.

---

# PART B — `_remove_derived_result_files` and every early return

## Every `return` in `/scout/analyze`, classified — executed, not reasoned

| line | status | what it leaves behind | verdict |
|---|---|---|---|
| `:674` | 400 bad chain | nothing written | SAFE |
| `:678` / `:681` | 404 no job / no input | nothing written | SAFE |
| `:687` | 503 busy | nothing ran, `results.csv` + derived both still chain A | SAFE (coherent) |
| **`:720`** | **422 from `ValueError`** | **`results.csv` = B, derived = A** | **DEFECT — D1** |
| **`:723`** | **500 from `except Exception`** | **`results.csv` = B, derived = A** | **DEFECT — D1** |
| `:762` | 409 collision | everything the winner's, self-labelled | SAFE, decision correct |
| `:773` | 422 scores-nothing | `_remove_derived_result_files` ran | SAFE |
| `:943` | 200 | rewritten / unlinked | SAFE |

**And the route the enumeration does not cover.** `/scout/progress` also writes
`results.csv` (`:1045`) and has **no** cleanup on any of its paths, success
included. Enumerating `analyze()`'s returns is the wrong frame for the invariant
— see D1.

Executed. `_state()` re-reads the job dir and both `/scout/download` variants
after each:

```
--- after chain A (200) ---
    results.csv stamp: 'A'
    top3   -> 200 chain_id='A' residues='ALA10,ALA11,ALA12,ALA13,ALA14,ALA15,ALA16'

--- chain B -> 422 {'error': 'interface detection blew up'}    (raised at :720, AFTER run_pipeline)
    files            : ['.owner','analyze_cache.json','epitopes.csv','epitopes_annotated.csv',
                        'input.pdb','results.csv','results_annotated.csv']
    results.csv stamp: 'B'                                     <- chain B's file
    top3   -> 200 chain_id='A' residues='ALA10,…,ALA16'        <- *** CHAIN A ***
    full=1 -> 200 chain_id='A' residues='ALA10,…,ALA16'        <- *** CHAIN A ***
    pipeline calls: ['A', 'B']

--- chain B -> 500 {'error': 'Analysis failed. Check that the PDB is valid and try again.'}
    results.csv stamp: 'B'
    top3   -> 200 chain_id='A' residues='ALA10,…,ALA16'        <- *** CHAIN A ***

--- chain B -> 422 'Too few surface residues (2)…'  (raised BY run_pipeline, nothing written)
    results.csv stamp: 'A'   top3 -> chain_id='A'              <- coherent, SAFE

--- chain B -> 503 busy
    results.csv stamp: 'A'   top3 -> chain_id='A'              <- coherent, SAFE
```

## Is the 422 reachable in a flow where clearing destroys something wanted? No.

The `:773` 422 fires only when `results.csv` holds no stamped row after a run of
our own, i.e. the chain scored nothing. The derived files it deletes are, by
construction, a *different* chain's. Nothing wanted is destroyed.

## Is the 409-preserves decision right? Yes.

Executed (`_steal`: the request asks for B, a concurrent run leaves A):

```
--- chain B -> 409 'Another analysis on this job replaced the results…' ---
    results.csv stamp: 'A'
    top3   -> 200 chain_id='A' residues='ALA10,…,ALA16'
```

The whole job dir is coherently chain A's — `results.csv` *and* every derived
file — and the delivered CSV self-labels `chain_id=A`. The 409 caller cannot
mistake it for chain B's: the file says which chain it is. Deleting them would
destroy a live run's output, which is the round-2 destructive behaviour. Correct
call, and M9 (adding the delete to the 409 branch) is mutation-caught.

## The author's disputed round-4 point: they are RIGHT.

After the `:773` 422, `results_annotated.csv` is deleted, so `?full=1` falls
back to `results.csv` — which is **chain B's own header-only file**, not chain
A's:

```
--- chain B -> 422 'No surface patches could be scored for chain B…' ---
    files  : ['.owner','analyze_cache.json','input.pdb','results.csv']
    top3   -> 404 'Results not found. Please run analysis first.'
    full=1 -> 200 HEADER-ONLY (1 line)
```

Truthful, not chain A's. And the weaker assertion **is** adequate: M8 (dropping
`results_annotated.csv` from `_remove_derived_result_files`) is CAUGHT, so the
"chain A's residue numbers are absent" assertion is load-bearing for exactly the
regression that would re-open it.

---

# PART C — confirmed defects

## D1 (MEDIUM — `/scout/download` serves the previous chain's CSVs, in the normal flow, and a comment certifies it cannot happen) — `scout/routes.py:719-723` and `:1045`

**Where.** `except ValueError → 422` at `:719-720` and `except Exception → 500`
at `:721-723` both sit *after* `run_pipeline(pdb_path, chain_id)` has already
overwritten `results.csv` with the new chain. Neither calls
`_remove_derived_result_files`. Three lines above the write, `:873-876` states:

> Both top-3 files are rewritten or removed on every run that reaches here,
> never left alone. The paths that return before this point clear them
> instead, via `_remove_derived_result_files` — except the 409, where they
> belong to the concurrent run that now owns results.csv.

That is false for two of the four preceding returns. This is round 1's D1 and
round 4's D1 re-opened one step further out, and it is **strictly worse than
round 4's version**: there, everything in the job dir said chain A; here
`results.csv` says B while `/scout/download` — which takes no chain parameter —
hands over A. Evidence above.

**Is the trigger real?** The `except Exception → 500` is a catch-all whose
entire purpose is the unforeseen. And `ValueError` at `:720` is naturally
reachable: `detect_interfaces`' scipy call at `interfaces.py:220-225` is guarded
only by `except ImportError`, and `cKDTree` raises `ValueError` on non-finite
coordinates —

```
$ python -c "cKDTree(np.array([[1.,2.,nan]], dtype=np.float32))"
nan -> ValueError data must be finite, check for nan or inf values
inf -> ValueError data must be finite, check for nan or inf values
```

I could not construct a *single structure* that reaches it only on the second
chain (a NaN anywhere poisons every run on that file), so the ordering was
produced by injecting the exception. State CONFIRMED; a natural end-to-end
trigger UNVERIFIED.

**But the exception path is not the main way in. `/scout/progress` is.**
`run_pipeline` has **two** callers: `analyze()` at `routes.py:691` and
`progress()` at `routes.py:1045`. The SSE one has no cache gate and no cleanup,
and the page **always** opens that stream before POSTing `/scout/analyze`
(`index.html:993` → `openProgressStream` → `_finalizeAnalysis`). So the mismatch
is opened by the *normal* flow on *every* chain switch. Executed — chain A
analysed, then nothing but the SSE for chain B:

```
=== chain A analysed normally ===
    results.csv stamp: 'A'
    top3   -> 200 chain_id='A' residues='ALA10,ALA11,ALA12,ALA13,ALA14,ALA15,ALA1'

=== GET /scout/progress chain=B -> 200, stage done: True ===
  AFTER the SSE, BEFORE /scout/analyze
    results.csv stamp: 'B'                                    <- chain B's file
    top3   -> 200 chain_id='A' residues='ALA10,…,ALA16'       <- *** CHAIN A ***
    full=1 -> 200 chain_id='A' residues='ALA10,…,ALA16'       <- *** CHAIN A ***
    pipeline calls   : ['A', 'B']
```

The window closes when `/scout/analyze` succeeds and rewrites the derived files.
It stays open **permanently** whenever that second request never completes —
and there are six ordinary ways for that: the user closes the tab or navigates
away, the network drops, `@requires_scout_quota` returns 402 and
`_finalizeAnalysis` redirects to `/pricing` (`index.html:397-401`),
`anon_compute_slot` returns 503, the job 404s, or the 422/500 above. Executed
with the second request refused:

```
=== /scout/analyze chain=B refused -> 404 ===
  job dir the user is left with
    results.csv stamp: 'B'
    top3   -> 200 chain_id='A' residues='ALA10,…,ALA16'       <- *** CHAIN A ***
```

This is what moves D1 from "an exotic exception path" to "the default path with
a window that any interruption makes permanent", and it is why the fix has to
go next to the writer.

**Fix — put the cleanup next to the writer, not next to the returns.**
`_remove_derived_result_files(job_dir)` belongs immediately after **each**
`run_pipeline(...)` call — `routes.py:691` and `routes.py:1045`. The instant
`results.csv` changes owner, everything derived from the old one is invalid by
construction. Two lines. That covers the 422, the 500, the SSE-only flow, and
any *unhandled* exception between `:726` and `:943` (a full disk on
`epitopes_annotated_path.open("w")`, for instance) — none of which the current
per-branch arrangement can reach. It also makes the `:773` call site and the 409
special-case unnecessary: a loser that already ran its own pipeline cleared the
files before the winner wrote its own at the end of its own request.

The shape lesson, stated once: **the invariant is "the derived files describe
whatever chain `results.csv` holds", and `results.csv` has two writers.** No
number of guards in `analyze()`'s return paths can hold an invariant whose other
writer lives in `progress()`.

## D2 (LOW — mechanism confirmed, trigger unverified) — the clear is not a concurrency guard — `templates/scout/index.html:430`

Evidence and reasoning in Part A. `_clearChainScopedResults` cannot stop a
late-resolving `renderViewer` from the previous chain repainting its table and
flag cards over the new chain's page, because the render is async and nothing
carries a generation token. The function's comment presents "unconditional and
first" as the answer to `renderViewer` being unawaited; it is the answer to only
half of it.

## D3 (LOW — dead code the change ADDS, now with tests enforcing it) — `templates/scout/index.html:871-873`

Three of the eleven lines in `_clearChainScopedResults` clear things nothing can
populate:

```js
document.getElementById('ppi-table-body').innerHTML = '';
document.getElementById('ppi-section').hidden = true;
_currentPpiInterfaces = [];
```

`renderPpiInterfaces` (`:738`) has **no call site**; `_currentPpiInterfaces`
(`:318`) is never assigned and never read; `#ppi-section` is `hidden` in the
markup and nothing unhides it. Round 3's D6, still open — but this round adds
three lines maintaining it and **two parametrised test cases**
(`test_every_chain_scoped_element_is_cleared[ppi-table-body]` and
`[ppi-section]`) that now *enforce* the dead code. Meanwhile
`detect_interfaces` still runs on every analyze (`routes.py:717`) and is
serialised into the response and the cache twice. Either wire it up or delete
`renderPpiInterfaces`, `_currentPpiInterfaces`, those three lines, the two test
params and the markup — not both.

## D4 (cosmetic — round 4's D8, unfixed) — `scout/routes.py:741`

`fieldnames: list[str] = []` is still dead: the only branch that skips the
`with` block returns, and `:781` reassigns it before the only read at `:903`.

## D5 (cosmetic — one route got the fix, its sibling did not) — `scout/routes.py:1163`

`/scout/feasibility/progress` now distinguishes three states (nothing specified
/ no results for this chain / epitope not in this chain). `/scout/feasibility/
analyze` — the route that does the work — still answers the third state with
`"epitope_residues or epitope_id is required."`, which is false: both were
supplied. Round 3's D8, unchanged. The author's comment at `:1247-1253` argues
the SSE is what the user reads, which is fair, but the two routes now contradict
each other on the same input.

---

# PART D — status of every earlier round's open item

| Item | Status |
|---|---|
| R4 D1 — 422/409 skip the derived-file cleanup | **HALF CLOSED.** `:773` fixed and mutation-caught (M10). `:720` and `:723` still open → **D1** |
| R4 D2 — `#flag-reference` left populated | **CLOSED**, executed (E2/E3), and every element mutation-caught (M31/M32) |
| R4 D3 — clearing conditional, not unconditional | **CLOSED**, executed. Ordering pinned by `test_the_clear_runs_before_anything_renders` (M35/M36) |
| R4 D4 — pre-deploy job dirs 404 the feasibility routes | **STILL OPEN by decision.** Correctly traded; still nothing in the change says so. One line in the commit message |
| R4 D5 — false SSE-forging rationale in the test docstrings | **CLOSED.** Both copies corrected, and the formula-injection dismissal replaced with an honest "KNOWN, ACCEPTED gap" naming the sharing scenario |
| R4 D6 — blank `chain_id` cell reported as a collision | **CLOSED** via `or None`; mutation-caught (M6) |
| R4 D7 — "every reader goes through here" was false | **CLOSED.** The docstring now names `download()` as the deliberate exception |
| R4 D8 — dead `fieldnames` local | **STILL OPEN** → D4 |
| R3 D6 — `renderPpiInterfaces` dead | **STILL OPEN, and now load-bearing on two tests** → D3 |
| R3 D8 — "epitope_residues or epitope_id is required" | **STILL OPEN** → D5 |
| R2 D3 — rollback / mixed fleet `ValueError` → 500 | **STILL OPEN.** Nothing tests it, nothing mentions deploy ordering |
| R1 D6 / R2 D4 — `feasibility_results.csv` no chain param | **MITIGATED, NOT CLOSED.** `feasibility_download:1351` takes only `job_id`; the stamp makes it stale-but-labelled |
| FE-6 — Windows-only `unlink` PermissionError | **STILL OPEN**, correctly deferred (prod + CI are Linux) |
| `/scout/progress` has no cache gate | **STILL OPEN, and round 4 under-rated it.** It is not "wasted compute only" — it is the second writer of `results.csv`, and the one that opens D1's window in the normal flow. `_results_csv_for_chain`'s docstring calls it a future concern ("must use this one if it ever gains [a cache gate]"); it is a present one |

---

# PART E — the deferred items, adjudicated

| Deferred item | Verdict |
|---|---|
| **CSV formula injection** (`chain_id` unescaped in the delivered CSV) | **Correctly deferred, and now correctly recorded.** Round 4's objection was to the docstring claiming the risk did not exist; that text is gone, replaced by a statement that names the sharing scenario, says ownership narrows but does not close it, and says what a real fix costs (escape on write *and* before the cache comparison, or the two sides stop matching). Deferring a stated risk is fine. Not a blocker |
| **Windows-only `unlink` PermissionError** | **Correctly deferred.** Prod and CI are `ubuntu-latest`. Self-consistent: the two `unlink(missing_ok=True)` calls this change adds are the same class and equally unguarded. It did bite me locally — job dirs survived `shutil.rmtree(ignore_errors=True)` — but that is a test-harness cost, not a product one |
| **>64-char chain id offered-then-refused** | **Correctly deferred.** Reaching it needs a hand-crafted mmCIF with a 65-character `auth_asym_id`; PDB column 22 is one byte and real `auth_asym_id`s are a handful. The residual is stated *and* tested (`test_an_absurd_chain_id_is_still_refused`, which dies under M16), and 64 is mutation-pinned (M18). The complete fix needs all three chain-offer sites (`routes.py:519, 611, 649`), so it is not the one-liner round 2 implied |
| **Rollback / mixed-fleet `ValueError` → 500** | **Correctly deferred as a code change; NOT correctly deferred as a silence.** An old writer meeting a new row raises `dict contains fields not in fieldnames: 'chain_id'`. Forward-compatible, backward-fatal for one job-dir lifetime. It costs one line in the commit message ("deploy forward only; do not roll back with live job dirs") and nobody has written it |
| **`feasibility_results.csv` served with no chain param** | **Correctly deferred.** The stamp this change adds turns it from mislabelled into stale-but-labelled, which is the important half. Adding a chain parameter is a route-signature change with its own blast radius |
| **`renderPpiInterfaces` defined but never called** | **NOT correctly deferred — see D3.** Deferring dead code is fine. Adding three lines to maintain it and two parametrised tests to *enforce* those lines is not a deferral, it is investment in a feature that does not exist |

---

# PART F — mutation table

43 mutants, each applied alone at byte level, md5-confirmed landed,
`-k scout` re-run against a **188 passed / 0 failed** baseline, restored,
md5-confirmed clean. **36 caught, 7 not.**

| # | fix it reverts | mutation | verdict | tests that fail |
|---|---|---|---|---|
| M1 | round 1 - the cache key | analyze gate back to bare results.csv existence | **CAUGHT (9)** | `test_a_chain_that_scores_nothing_leaves_no_downloadable_file`, `test_a_conflicting_run_keeps_the_winners_files`, `test_a_header_only_results_csv_is_a_miss`, +6 more |
| M2 | round 1 - the cache key | _results_csv_for_chain ignores the chain (always a hit) | **CAUGHT (13)** | `test_a_chain_that_scores_nothing_is_not_reported_as_a_collision`, `test_a_chain_that_scores_nothing_leaves_no_downloadable_file`, `test_a_conflicting_run_keeps_the_winners_files`, +10 more |
| M3 | round 1 - the stamp | run_pipeline stops stamping chain_id | **NOT CAUGHT** | - |
| M4 | round 1 - the stamp | pipeline CSV_COLUMNS drifts from the flags list | **CAUGHT (1)** | `test_flags_column_list_matches_the_pipeline` |
| M5 | round 1 - the stamp | flags.py column list drifts (chain_id dropped) | **CAUGHT (22)** | `test_a_blank_chain_id_cell_is_a_miss_not_a_collision`, `test_a_chain_that_scores_nothing_leaves_no_downloadable_file`, `test_a_conflicting_run_keeps_the_winners_files`, +19 more |
| M6 | round 5 - blank cell is a miss | blank chain_id cell is a value again (drop `or None`) | **CAUGHT (1)** | `test_a_blank_chain_id_cell_is_a_miss_not_a_collision` |
| M7 | round 5 - _remove_derived_result_files | _remove_derived_result_files is a no-op | **CAUGHT (1)** | `test_a_chain_that_scores_nothing_leaves_no_downloadable_file` |
| M8 | round 5 - _remove_derived_result_files | _remove_derived_result_files drops results_annotated.csv | **CAUGHT (1)** | `test_a_chain_that_scores_nothing_leaves_no_downloadable_file` |
| M9 | round 5 - the 409 preserves | the 409 path ALSO wipes the winner's derived files | **CAUGHT (2)** | `test_a_conflicting_run_keeps_the_winners_files`, `test_a_stolen_results_file_is_a_409_not_an_empty_200` |
| M10 | round 5 - the 422 clears | 422 path stops clearing derived files | **CAUGHT (1)** | `test_a_chain_that_scores_nothing_leaves_no_downloadable_file` |
| M11 | round 3 - 409/422 split | 409/422 split collapsed: everything is a 409 | **CAUGHT (2)** | `test_a_chain_that_scores_nothing_is_not_reported_as_a_collision`, `test_a_chain_that_scores_nothing_leaves_no_downloadable_file` |
| M12 | round 3 - 409/422 split | 409/422 split collapsed: everything is a 422 | **CAUGHT (2)** | `test_a_conflicting_run_keeps_the_winners_files`, `test_a_stolen_results_file_is_a_409_not_an_empty_200` |
| M13 | round 2 - no destructive empty 200 | the whole miss branch falls through (destructive empty 200) | **CAUGHT (4)** | `test_a_chain_that_scores_nothing_is_not_reported_as_a_collision`, `test_a_chain_that_scores_nothing_leaves_no_downloadable_file`, `test_a_conflicting_run_keeps_the_winners_files`, +1 more |
| M14 | round 2 - derived files | epitopes_annotated.csv no longer removed when top3 is empty | **CAUGHT (1)** | `test_top3_download_never_serves_the_previous_chain` |
| M15 | round 2 - derived files | epitopes.csv no longer removed when top3 is empty | **CAUGHT (1)** | `test_top3_download_never_serves_the_previous_chain` |
| M16 | round 2/3 - _valid_chain | _valid_chain accepts everything | **CAUGHT (14)** | `test_a_newline_chain_cannot_forge_an_sse_frame`, `test_an_absurd_chain_id_is_still_refused`, `test_json_routes_reject_unsafe` x6, +1 more |
| M17 | round 2/3 - _valid_chain | _valid_chain back to strict [A-Za-z0-9]{1,8} | **CAUGHT (10)** | `test_a_long_chain_id_the_dropdown_offers_is_not_refused`, `test_a_parser_reachable_id_analyses_end_to_end`, `test_parser_reachable_ids_are_not_refused` x8 |
| M18 | round 3 - cap 16 -> 64 | _CHAIN_ID_MAX_LEN back to 16 | **CAUGHT (1)** | `test_a_long_chain_id_the_dropdown_offers_is_not_refused` |
| M19 | round 2 - guard placement | guard off POST /scout/analyze | **CAUGHT (7)** | `test_an_absurd_chain_id_is_still_refused`, `test_json_routes_reject_unsafe` x6 |
| M20 | round 2 - guard placement | guard off GET /scout/progress | **CAUGHT (7)** | `test_a_newline_chain_cannot_forge_an_sse_frame`, `test_sse_routes_reject_unsafe` x6 |
| M21 | round 3 - guard placement | guard off POST /scout/feasibility/analyze | **CAUGHT (6)** | `test_json_routes_reject_unsafe` x6 |
| M22 | round 2 - guard placement | guard off GET /scout/feasibility/progress | **CAUGHT (6)** | `test_sse_routes_reject_unsafe` x6 |
| M23 | round 2 - binder cache chain gate | _get_binder_overlaps chain gate removed | **CAUGHT (2)** | `test_binder_overlaps_are_actually_returned_for_the_right_chain`, `test_explicit_residues_do_not_inherit_another_chains_binders` |
| M24 | round 2 - feasibility chain scope | feasibility_analyze epitope_id back to job-scoped | **CAUGHT (2)** | `test_epitope_id_is_not_resolved_against_another_chain`, `test_the_feasibility_404_names_the_chain` |
| M25 | round 2 - feasibility chain scope | feasibility_progress epitope_id back to job-scoped | **CAUGHT (2)** | `test_feasibility_progress_will_not_resolve_another_chains_epitope`, `test_sse_separates_no_results_from_unknown_epitope` |
| M26 | round 4 - 404 names the chain | feasibility 404 loses the chain name | **CAUGHT (1)** | `test_the_feasibility_404_names_the_chain` |
| M27 | round 3 - 3-way SSE message | 3-way SSE message collapsed to the old 1-way | **CAUGHT (1)** | `test_sse_separates_no_results_from_unknown_epitope` |
| M28 | round 1 - feasibility stamp | run_feasibility_pipeline stops stamping chain_id | **NOT CAUGHT** | - |
| M29 | round 1 - feasibility stamp | chain_id out of FEASIBILITY_CSV_COLUMNS | **NOT CAUGHT** | - |
| M30 | round 5 - the shared clear | _clearChainScopedResults body emptied | **CAUGHT (3)** | `test_every_chain_scoped_element_is_cleared` x3 |
| M31 | round 5 - the shared clear | only the epitope-legend clear removed | **CAUGHT (1)** | `test_every_chain_scoped_element_is_cleared` |
| M32 | round 5 - the shared clear | only the flag-reference pair removed | **CAUGHT (2)** | `test_every_chain_scoped_element_is_cleared` x2 |
| M33 | round 5 - the shared clear | only the ppi pair removed | **CAUGHT (2)** | `test_every_chain_scoped_element_is_cleared` x2 |
| M34 | round 5 - the shared clear | only `_currentPpiInterfaces = []` removed from the clear | **NOT CAUGHT** | - |
| M35 | round 5 - the shared clear | the clear call removed from _handleAnalysisResult | **CAUGHT (1)** | `test_the_clear_runs_before_anything_renders` |
| M36 | round 5 - clear before render | the clear moved AFTER the uniprot render (ordering broken) | **CAUGHT (1)** | `test_the_clear_runs_before_anything_renders` |
| M37 | round 5 - resetAll delegates | resetAll stops delegating to the shared clear | **NOT CAUGHT** | - |
| M38 | round 3 - button re-enable | showAnalyzeError's button lookup neutered (element -> null) | **NOT CAUGHT** | - |
| M39 | round 3 - button re-enable | showAnalyzeError's re-enable lines deleted | **CAUGHT (1)** | `test_an_error_re_enables_the_analyze_button` |
| M40 | round 3 - feasibility link | feasibility link back to the live dropdown | **CAUGHT (1)** | `test_feasibility_link_uses_the_scored_chain_not_the_dropdown` |
| M41 | round 3 - feasibility link | renderEpitopeTable loses the chain parameter | **CAUGHT (1)** | `test_feasibility_link_uses_the_scored_chain_not_the_dropdown` |
| M42 | round 4 - dead top-3 link | dead top-3 download link shown unconditionally again | **CAUGHT (1)** | `test_the_dead_top_3_download_link_stays_hidden` |
| M43 | round 4 - empty-chain message | zero-epitope branch stops telling the user anything | **NOT CAUGHT** | - |

## What the table says — **36/43**, against round 4's 29/36

**Beaten on both axes**: a larger mutant set and a higher kill rate (83.7% vs
80.6%). Every mutant round 4 reported as caught is still caught, and two it
reported as *not* caught are now closed:

* **M26 closes round 4's E1.** The JSON feasibility 404 losing the chain name
  now kills `test_the_feasibility_404_names_the_chain`. Round 4 asked for exactly
  this one assertion and got it.
* **M31/M32/M33 close round 4's F6g.** Deleting *any single line* from
  `_clearChainScopedResults` now fails a test, because
  `test_every_chain_scoped_element_is_cleared` is parametrised over all 11 ids.
  This is the structural improvement of the round: the shared function plus the
  parametrised list is the first version of this fix that a future edit cannot
  silently erode.
* **M35/M36 pin the ordering**, so "clear first, unconditionally" cannot be
  quietly relaxed either.

### The seven survivors, and which of them matter

| # | survivor | verdict |
|---|---|---|
| **M3** | `run_pipeline` stops stamping `chain_id` | **CI-caught — now VERIFIED, not assumed.** I stubbed `compute_rsa` (the pipeline's single freesasa entry point, `sasa.py:51`) and ran the otherwise-real pipeline. Clean: `results.csv rows: 25, chain_id cells: ['A']`, analyze 200. With M3 applied: **analyze → 422**, `chain_id cells: ['']`. So `test_analyze_runs_the_real_pipeline`, which asserts 200, fails in CI. Acceptable |
| **M28** | `run_feasibility_pipeline` stops stamping | **REAL GAP.** Round 4's F3b, unchanged |
| **M29** | an extra column drifts into `FEASIBILITY_CSV_COLUMNS` | **REAL GAP.** `test_flags_column_list_matches_the_pipeline` pins `CSV_COLUMNS` ↔ `_CSV_COLUMNS_BASE` (M4 dies on it) — **nothing pins `FEASIBILITY_CSV_COLUMNS` at all.** Together with M28 this means *the entire feasibility half of the stamp is untested*: both the writer and the column list can lose `chain_id` with the suite green. The **code is correct** — same CI simulation, `feasibility_results.csv rows: 1, chain_id cells: ['A'], header has chain_id: True` — so this is a missing test, not a broken feature. One assertion on that CSV closes both |
| **M37** | **`resetAll` stops delegating to the shared clear** | **REAL GAP, and new this round.** The refactor that made Reset use the shared function is the one part of it nothing tests: delete that call and Reset leaves the previous chain's table, legend, flag cards and UniProt bar on screen, suite green. Round 4 never mutated this because `resetAll` still had its own copy. One assertion (`"_clearChainScopedResults();" in resetAll_body`) closes it |
| **M34** | `_currentPpiInterfaces = []` removed from the clear | **Correctly untested** — the variable is dead (D3) |
| **M38** | `showAnalyzeError`'s element lookup neutered to `null` | **Ceiling, not oversight.** Round 4's F5, unchanged: a string test cannot tell a real lookup from `null` when the asserted literal stays on the page. No JS harness in the repo |
| **M43** | the zero-epitope explanation deleted | **Minor gap.** The user-facing half of the empty-chain fix (M42's sibling) has no assertion. Cosmetic |

### Where template string-tests are and are not adequate

Adequate: `test_every_chain_scoped_element_is_cleared`. Because it parametrises
over the *whole set*, "the id is named in the function" is equivalent to "the
element is cleared" for every single-line deletion — M30/M31/M32/M33 all die.

Not adequate: anything whose correctness depends on the named element being
*real*. M38 survives because the literal survives. That is also why D2 (the
async repaint) and D3 (clearing elements nothing populates) are invisible to
every test in this file — the tests can see the text of the function, never its
effect.

---

# PART G — is the change over-built?

**Yes, and measurably — but the over-build is in prose and boundary tests, not
in the correctness core.**

Counted from the diff:

```
scout/routes.py     236 added lines = 56 comment-only + 36 docstring + 21 blank + ~123 code
                    (much of the 123 is the 35-line CSV loop RE-INDENTED, not new)
                    -> ~50-60 lines of genuinely new logic, wrapped in 92 lines of prose
templates/index.html 65 added lines = 33 comment + 30 code
tests/…chain_scoped… 1055 lines, 33 tests, 88 asserts, 223 docstring + 32 comment lines
                    -> ~2.9 prose lines per assertion, ~32 file lines per test
```

The reported bug was a one-line cache key. The root fix is genuinely small:
stamp `chain_id` (4 lines) + compare it in the gate (3 lines). Everything found
*during* the work that is the same job-scoped assumption — the derived files, the
binder cache, the feasibility epitope lookup — is legitimately in scope and is
the best part of the change.

**What I would cut:**

1. **The QC archaeology in the comments (~25 lines).** "the stale-download bug
   from round 1, re-opened by this early return until round 4 caught it", "the
   mismatch QC round 3 found at 16", "an earlier version of this comment claimed
   otherwise and was wrong". That is review history; it belongs in the PR body.
   In six months it is noise, and one of these narrations (`:873`) is **actively
   false** — D1.
2. **`TestChainIdIsValidatedAtTheBoundary` down from ~140 lines to two
   parametrised tests.** `_valid_chain` itself is input validation at a trust
   boundary and I would keep it (5 lines) — but the tests' own docstrings admit
   it is not load-bearing: the SSE-forging test "passes with `_valid_chain`
   deleted entirely", `json.dumps` closes that hole, every accepted id
   round-trips the CSV faithfully, and formula injection is explicitly *not*
   closed by it. Three rounds of QC have been spent tightening, loosening and
   re-capping a guard that closes no reachable hole. Its 15-line comment also
   over-claims: "control characters are refused" describes C0 and DEL only —
   executed,

   ```
   C0 0x01        valid=False      C1 NEL 0x85    valid=True
   DEL 0x7f       valid=False      C1 CSI 0x9b    valid=True
                                   LS  U+2028     valid=True
   ```

   I found no sink for those (SSE is `json.dumps` with `ensure_ascii`, the href
   is `encodeURIComponent`, the error banner is `textContent`), so this is a
   comment-vs-code gap rather than a defect — but it is one more reason the
   ceremony around this guard is out of proportion to what it does.
3. **The `ppi-*` clearing and its two test params** — D3.
4. **`fieldnames: list[str] = []`** — D4.
5. **`test_the_uniprot_bar_does_not_survive_a_chain_switch` (`:869`)** is now
   pure duplication: it asserts `"uniprot-bar" in clear_fn`, which
   `test_every_chain_scoped_element_is_cleared[uniprot-bar]` already asserts.
6. **`_remove_derived_result_files`'s two call sites and the two `else: unlink`
   branches collapse into ONE call after `run_pipeline`** — that is D1's fix and
   it makes the change *shorter*.

**Where the template tests are and are not adequate.** They assert the presence
of a string in the rendered page. That is adequate for
`test_every_chain_scoped_element_is_cleared` — the parametrisation over all 11
ids means deleting *any single line* from the function fails a test, which is
exactly what round 4's F6g gap needed and it is now closed (M31, M32, M33 all
die). It is **not** adequate for anything whose correctness depends on the
element being real rather than named: M38 neuters `showAnalyzeError`'s button
lookup to `null` while leaving the asserted literal `btn.disabled = false` on
the page. With no JS harness in the repo that is a ceiling, not an oversight —
but it is why D2 and D3 could not be caught by any test in this file.

---

# Full suite

From the worktree root, no path argument, not piped through `tail`:

```
$ venv/Scripts/python.exe -m pytest -q -rf
5337 passed, 20 skipped, 854 warnings in 2201.22s (0:36:41)
EXIT=0
```

**Exactly the author's stated baseline: 5337 passed, 20 skipped, 0 failed.** No
`FAILED` and no `ERROR` lines (`grep -E "^FAILED|^ERROR"` -> none). This also
serves as the post-sweep re-baseline that rules out contamination from the three
concurrent full-suite runs described in the process note: the scout tests inside
it are the same 188 that every mutation row was measured against.

# Restoration proof

```
$ python mut.py verify
  all 6 files byte-identical to snapshot

$ git status --short
 M scout/flags.py
 M scout/pipeline.py
 M scout/routes.py
 M templates/scout/index.html
 M tests/test_scout_anonymous_access.py
?? docs/qc/scout-chain-scoped-results-round1.md
?? docs/qc/scout-chain-scoped-results-round2-backend.md
?? docs/qc/scout-chain-scoped-results-round2-tests-frontend.md
?? docs/qc/scout-chain-scoped-results-round3.md
?? docs/qc/scout-chain-scoped-results-round4.md
?? docs/qc/scout-chain-scoped-results-round5.md     <- the only file I added
?? tests/test_scout_chain_scoped_results.py

$ git diff --stat
 scout/flags.py                       |   1 +
 scout/pipeline.py                    |  12 ++
 scout/routes.py                      | 293 ++++++++++++++++++++++++++++-------
 templates/scout/index.html           |  82 ++++++++--
 tests/test_scout_anonymous_access.py |  11 +-
 5 files changed, 323 insertions(+), 76 deletions(-)

$ md5sum <all six>
7646abedfc39c8842a3d4ef5e3b4f691  scout/flags.py
c483a15e94e457079baf601e4c02d084  scout/pipeline.py
e8682d5327b20a554c80531b632c91e4  scout/routes.py
93dae328a28c784dfdbb2aa69382bd06  templates/scout/index.html
8178369ea87d9f15bef65b7dc6ba173f  tests/test_scout_anonymous_access.py
96568173f71293096e9c5aae93006b7d  tests/test_scout_chain_scoped_results.py   <- identical to the pre-review value
```

The diff stat is byte-for-byte the one I started from (323 insertions, 76
deletions). Every hash was re-checked against the pre-review snapshot before
each of the 43 mutations and after each restore; the harness aborts on any
mismatch.

**One incident, disclosed.** My sweep driver was killed by the harness at
mutation 42 of 43 while a template mutation was still applied, and
`templates/scout/index.html` was left dirty (`0f46b888…` vs `93dae328…`). I
caught it on the next `verify`, restored from the snapshot, re-verified, and
re-ran M42 and M43 cleanly. No pytest run was made against the dirty tree, and
the final state above is byte-identical.

# Things I could not verify

* **The real biophysics path.** `freesasa` is absent locally; every route-level
  result uses a stubbed scorer writing the real CSV format through the real
  column list.
* **A natural single-file trigger for D1's ordering.** The state is reproduced;
  the exception was injected.
* **D2 in a real browser.** Driven against the Flask-rendered page JS in a node
  DOM stub. The operations involved (`hidden`, `innerHTML`, `style.display`, an
  unawaited async call) are the ones the stub models exactly, and the unawaited
  `renderViewer` plus the `finally` that re-enables the button are plain in the
  source — but it is not Chrome.

# Process note

**Three other agent sessions were running `python -m pytest -q` (full suite, no
path) concurrently with my sweep**, started 16:18:07, 16:18:16 and 16:19:03:

```
PID 26304 -> 17044   python -m pytest -q     (parent: a Claude Code bash.exe)
PID 16856 -> 12344   python -m pytest -q
PID 32408 ->  3184   python -m pytest -q
PID 16864 -> 20152 -> 2484 -> 35912   python -m pytest -q -k scout …   <- mine
```

They are **not** in this worktree. Each sibling worktree has its own `tmp/`, and
only mine was being written during the sweep:

```
suspicious-dewdney-13b07e   tmp/ mtime=16:23:40  <- mine, live
agitated-agnesi-6a06a1      tmp/ mtime=16:03:55
scout-err-redact            tmp/ mtime=15:49:19
jovial-hermann-c54d81       tmp/ mtime=14:23:55
great-wu-49c43d             tmp/ mtime=13:51:26
tools-hub/tmp (main tree)   no job dirs since 2026-08-04
```

So the shared state that gave round 3 phantom failures was **not** shared here.
What was shared is CPU: my `-k scout` round went from 85 s (measured clean at
15:45) to ~150 s once they started. To rule contamination out rather than argue
it, the baseline was **re-measured after the sweep** — see below. The exclusivity
claim in my brief held for the working *tree* (every file hash matched before
each mutation and after each restore); it did not hold for the machine.

The session scratchpad
(`…/25d31b86-f7bf-4b2c-abd7-fa23c6b3c97d/scratchpad/`) is **not** isolated to
this session: it already contained round 3's and round 4's harnesses
(`mut.py`, `ci_sim_test.py`, `fe_probe*.js`) and the author's own test-generation
scripts (`append_r3.py`, `append_r4.py`, `append_tests.py`). Nothing in the
working tree changed under me — every hash matched at every step — but "scratch
files are private to a session" is not true in this setup, and I overwrote
round 4's `mut.py` before noticing.
