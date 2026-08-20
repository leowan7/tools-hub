# QC round 6 — chain-scoped Scout results

Independent adversarial review of the changes made in response to round 5. I did
not write any of this code.

Environment: worktree `.claude/worktrees/suspicious-dewdney-13b07e`, HEAD = main =
`7fd180d`, interpreter
`C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe`,
Windows, `freesasa` absent so every route-level probe runs against a stubbed
scorer writing the real CSV format through the real `_CSV_COLUMNS_BASE`.
Production is gunicorn, `workers = max(1, WEB_CONCURRENCY or 2)`, sync worker
class, one machine, one shared `tmp/` (`Procfile`, `gunicorn.conf.py:42`) — so two
concurrent workers on one job dir is a real configuration, and `--preload` means
old and new code do not coexist across a deploy.

---

## Verdict

**SHIP.**

Round 5's blocking defect is closed at the right layer — at the writer, not at a
return path — and I could not reproduce it. `run_pipeline` has exactly two callers
and both are followed immediately by `_remove_derived_result_files`; the SSE-only
chain switch that round 5 executed into `top3 -> 200 chain_id='A'` now gives
`404` with `?full=1` serving chain B's own file. I attacked the binding from every
direction the brief names — a third writer of `results.csv` (there is none), a
`pdb_path.parent` that could differ from `job_dir` (it cannot), a raise between the
write and the cleanup (only a mid-`writerows` crash, pre-existing), a window in
which `/scout/download` serves the previous chain (bounded to two adjacent
statements, and the browser never sees it because the SSE `done` payload's
`download_url` is dead code) — and **found no path that returns a wrong scientific
result**. 51 of 63 mutants die; only 4 of the 12 survivors are real coverage gaps
and each is one line of test.

What I did find is smaller than a blocker and worth fixing on the way past:

```
LOW  | scout/routes.py:913, :934 | the two `else: …unlink()` branches are the only
     |   deletes NOT covered by the new best-effort guard, so the same filesystem
     |   error the helper absorbs escapes the view as a 500. REPRODUCED.
     |   They are also redundant (M15/M16 survive) — delete them, do not guard them.
LOW  | scout/routes.py:386       | the stale-download warning logs at `warning`,
     |   the level this module already uses for two per-request info lines
     |   (:728, :735). It is unfilterable, and it never names the chain that now
     |   owns results.csv. Use `logger.error` and add the chain.
LOW  | scout/routes.py:357, :792-794, :895-901, :309-311 | four comments that
     |   assert more than the code does — including one that says the cleanup
     |   covers "everything derived from results.csv" while `analyze_cache.json`
     |   is not covered. This repo has nine documented "guards that certify false".
LOW  | scout/routes.py:1242, templates/scout/index.html:555 | two CALL SITES can
     |   drop the chain they thread with the whole suite green (M33, M55). The
     |   functions are tested; the wiring is not. One assertion each.
LOW  | templates/scout/index.html:894-900 + tests/…:1108-1112 | round 5's
     |   "a test enforcing dead code" was relocated, not removed.
```

None of these changes behaviour, none is reachable in production as a wrong
result, and none of them is worth a seventh round. Ship it and take the cut list
as follow-ups.

---

# PART A — is the cleanup bound to the right thing?

## A1. Exactly two `run_pipeline` call sites, both covered

```
$ grep -rn "run_pipeline\|run_feasibility_pipeline" scout/ templates/ blueprints/
scout/pipeline.py:239:def run_pipeline(
scout/pipeline.py:557:def run_feasibility_pipeline(
scout/routes.py:716:                run_pipeline(pdb_path, chain_id)
scout/routes.py:717:                _remove_derived_result_files(job_dir)
scout/routes.py:1065:                run_pipeline(pdb_path, chain_id, progress_callback=callback)
scout/routes.py:1066:                _remove_derived_result_files(pdb_path.parent)
scout/routes.py:1198:        feasibility_csv = run_feasibility_pipeline(...)
scout/routes.py:1341:                run_feasibility_pipeline(pdb_path, chain_id, epitope_residues, ...)
```

Two callers, two cleanups, adjacent. **Round 5's D1 is CLOSED**, and closed at the
writer rather than at a return path. M06 (drop the progress-side call) dies on
`test_progress_alone_invalidates_the_previous_chains_downloads`; M05 (drop the
analyze-side call) dies on
`test_a_chain_that_scores_nothing_leaves_no_downloadable_file`. Both halves are
load-bearing, which is exactly what round 5 asked for.

## A2. Only one writer of `results.csv` exists anywhere

`scout/pipeline.py:409` is the sole production writer. Every other `results.csv`
reference in the repo is a reader (`routes.py`), a docstring, or a test fixture.
`run_feasibility_pipeline` writes `feasibility_results.csv`, a different file that
nothing derives from `results.csv`. No script, management command or cron job
writes into a job dir. **NOT A DEFECT.**

## A3. `pdb_path.parent` and `job_dir` are provably the same directory

```python
def _find_input_file(job_dir: Path) -> "Path | None":
    for ext in (".pdb", ".cif"):
        candidate = job_dir / f"input{ext}"
        if candidate.exists():
            return candidate
    return None
```

It joins two hard-coded basenames onto `job_dir` and never touches user input, so
the return is always a direct child of `job_dir`, which itself comes from
`resolve_owned_job_dir` (validate + confine + ownership). `_find_input_file`
cannot return a path outside the job dir, so the SSE cleanup cannot clear the
wrong directory or nothing. Executed on a real uploaded job:

```
P1  _find_input_file always returns a direct child of job_dir
  job_dir      = tmp\5d7095dc-044a-4884-894c-fd4c1a909ea4
  input file   = tmp\5d7095dc-044a-4884-894c-fd4c1a909ea4\input.pdb
  parent==dir  : True
```

**NOT A DEFECT.**

Style note, not a defect: `progress()` already has `job_dir` in scope
(`routes.py:1028`). Passing `pdb_path.parent` there and `job_dir` in `analyze()`
makes two spellings of one value and forces exactly the proof above. One spelling
is one less thing for round 7 to re-derive.

## A4. `run_pipeline` raising — traced

The `results.csv` write (`pipeline.py:513-516`) is the last real statement in
`run_pipeline`; only a log line, `_emit("ranking", 95)` and `return` follow.

* raise **before** the write → `results.csv` and the derived files both still
  describe the previous chain. Coherent, `/scout/download` truthful.
* raise **after** the write → only `_emit` can, and it just puts on a queue.
* **crash during `writerows`** → `open("w")` has already truncated, so a partial
  `results.csv` naming the new chain sits beside the old chain's derived files and
  `/scout/download` serves the old chain. There is no atomic write (no temp file +
  `os.replace`).

UNVERIFIED CONCERN, low: needs a mid-write failure (disk full, SIGKILL). It is
pre-existing and nothing in this change made it worse. Writing
`results.csv.tmp` then `os.replace` would make the whole stamp swap atomic — one
line in `pipeline.py`, worth a follow-up ticket, not a blocker.

The two handled paths (`except ValueError → 422` at `routes.py:745-746`,
`except Exception → 500` at `:747-749`) are safe *because* the cleanup now runs
inside the `try` immediately after `run_pipeline`, not because the handlers
changed. That is the right shape.

## A5. The stale-serve window is bounded, and the UI never opens it

In-process the gap between `run_pipeline` returning and
`_remove_derived_result_files` executing is two adjacent statements, so the other
gunicorn worker can hit it for microseconds. The interesting window is the other
one — after progress's cleanup and before `/scout/analyze` rewrites the derived
files, which is a whole request. In it:

* `?full=1` → `results_annotated.csv` gone → falls back to `results.csv`, the
  chain just scored. **Truthful.**
* top-3 → `epitopes_annotated.csv` gone → falls back to `epitopes.csv`, also gone
  → **404**, not stale data.

And the browser cannot expose either. `runAnalysis()` sets
`results-section.hidden = true` (`index.html:984`) before opening the stream, the
download anchors live inside `#results-section` (`:171-172`), and the SSE `done`
payload's `download_url` is **never read** — `openProgressStream`'s handler calls
`_finalizeAnalysis(jobId, chain)` and touches nothing else (`:336-352`). The links
are only re-shown by `_handleAnalysisResult`, i.e. after `/scout/analyze`
succeeded. **NOT A DEFECT** — the trade is "refuse briefly" rather than "serve the
wrong chain", which is the correct direction.

Executed — round 5's exact reproduction, re-run against this code:

```
==============================================================================
P3/P5  progress-only chain switch: is the previous chain still served?
==============================================================================
  analyze A -> 200
  {'files': [... 'epitopes.csv', 'epitopes_annotated.csv', 'results.csv', 'results_annotated.csv'],
   'results.csv stamp': 'A'}
  top3   (200, ['A'], 'epitope_id,chain_id,residues,...')
  progress B -> stage done: True   pipeline calls ['A', 'B']
  {'files': ['.owner', 'analyze_cache.json', 'input.pdb', 'results.csv'],
   'results.csv stamp': 'B'}
  top3   (404, [], '{"error":"Results not found. Please run analysis first."}')
  full=1 (200, ['B'], 'epitope_id,chain_id,residues,...')
```

(the second element of each tuple is the set of distinct `chain_id` cells in the
delivered CSV.) Round 5 got `top3 -> 200 chain_id='A'` here. It is now a 404 and
`?full=1` is chain B's own file. **Round 5's D1 does not reproduce.**

## A9. CONFIRMED (LOW, refuses valid work briefly) — re-running the SAME chain destroys its top-3 until `/scout/analyze` rebuilds it

`/scout/progress` still has no cache gate, so it re-runs `run_pipeline`
unconditionally — including for a chain that was just scored — and the new cleanup
now fires on that path too. Executed:

```
==============================================================================
P6  re-running the SAME chain through progress destroys its top-3
==============================================================================
  after analyze A : top3 (200, ['A'], 'epitope_id,chain_id,residues,...')
  after progress A (same chain, no follow-up analyze):
    {'files': ['.owner', 'analyze_cache.json', 'input.pdb', 'results.csv'],
     'results.csv stamp': 'A'}
    top3   (404, [], '{"error":"Results not found. Please run analysis first."}')
    full=1 (200, ['A'], 'epitope_id,chain_id,residues,...')
```

Nothing stale is served — `?full=1` is still chain A, correctly — but a top-3 CSV
that was valid a moment ago is gone, and on main it would not have been (the
derived files were left alone). It is repaired by the `/scout/analyze` the page
issues immediately afterwards, and the UI hides both anchors across that window
(A5), so it only bites when that second request never lands: tab closed, network
drop, 503 busy, 429. The cleanup is doing its job — it cannot tell "same chain" from
"different chain" because it is bound to the write, which is the right binding.
The cheap close is the cache gate `/scout/progress` has never had: it would skip
the rescore *and* the cleanup for a chain already stamped in `results.csv`, which
also deletes the wasted pipeline run round 4 and round 5 both flagged.

## A6. CONFIRMED DEFECT (LOW) — the best-effort policy is applied to one of the two places that delete these files

`_remove_derived_result_files` wraps its `unlink` in `try/except OSError` because,
in the author's own words, "a file that will not delete must not take a successful
scoring run down with it". Two lines later the same file deletes the same files
with no guard at all:

```
$ grep -n "unlink" scout/routes.py
375:    part, and the worst case of a failed unlink is a stale download that the
384:            (job_dir / name).unlink(missing_ok=True)     <- guarded
913:        epitopes_annotated_path.unlink(missing_ok=True)  <- NOT guarded
```

(`routes.py:931-932` was the third; see A7 — it is the `epitopes.csv` twin.)
`:913` sits **outside** every `try` in the route: the route's `try` closes at
`:748`, so an `OSError` there escapes the view entirely.

Reproduced. Same `Path.unlink` monkeypatch as the author's own
`test_a_failed_cleanup_does_not_lose_the_run`, but with a chain that scores rows
that do not clear `_MIN_COMPOSITE`, so `top3` is empty and the `else:` branch runs:

```
==============================================================================
P2  the two `else: unlink` siblings are NOT best-effort
==============================================================================
  analyze RAISED out of the view: PermissionError: [Errno 32] The process cannot access the file
  control: the same monkeypatch with a QUALIFYING chain (author's test)
    -> HTTP 200  (helper's try/except absorbed it)
```

Identical filesystem error, identical files: absorbed on one path, fatal on the
other. (`TESTING=True` propagates the exception; in production it is a 500.)

Severity is held at LOW because production and CI are Linux, where `unlink` on an
open file succeeds and an `OSError` after a successful `results.csv` write into
the same directory is essentially unreachable (the process demonstrably has
write+execute on that directory). It is reachable on Windows — the exact sequence
the helper's docstring cites — and it is an inconsistency the author's own
rationale condemns. The fix is not to add a second `try`; it is A7.

## A7. CONFIRMED (LOW, dead weight) — two of the change's own lines are now redundant, and the mutants prove it both ways

Moving the cleanup to the writer made the two `else: …unlink(missing_ok=True)`
branches (`routes.py:912-913` and `:931-932`) unreachable-as-effective:

* **M15** (delete the `epitopes_annotated.csv` branch entirely) → **SURVIVED**
* **M16** (delete the `epitopes.csv` branch entirely) → **SURVIVED**
* **M62** (keep the branch but drop `missing_ok=True`) → **CAUGHT** by
  `test_top3_download_never_serves_the_previous_chain`

Read together those three say the branch is *required to be a no-op* and *is* a
no-op: the file is already gone when it runs, because
`_remove_derived_result_files` removed it after `run_pipeline`. Round 5 predicted
exactly this ("it also makes the `:773` call site and the 409 special-case
unnecessary"); the author kept them. Deleting both branches removes 4 lines, the
last unguarded unlink (A6), and the only reason `:895-901`'s comment has to
describe two mechanisms instead of one.

The one path that could still reach them is a *transient* `parse_pdb` failure:
`_chain_total` is computed under a bare `except Exception: pass` (`routes.py:755-763`)
and feeds `_max_resi`, so two analyses over one `results.csv` can in principle
disagree about `top3`. If the author wants to keep the branches for that, they
should say so — but then the same reasoning demands they be guarded like their
sibling.

## A8. CONFIRMED (LOW, comment vs code) — four claims the code does not make

This repo has a documented "guards that certify false" pattern (9 instances), so
these are findings rather than style notes.

1. **`routes.py:357`** — "Invalidate everything derived from `results.csv`."
   `analyze_cache.json` (`routes.py:944-952`) stores `epitopes: top3`, built from
   `results.csv`, and is **not** invalidated. It is safe only because its single
   reader gates on `cache["chain"]` (`:445`). Say "the three files
   `/scout/download` can serve", or add it.
2. **`routes.py:895-901`** — "The paths that return before this point clear them
   instead, via `_remove_derived_result_files`." False for the 400 / 404 / 503
   returns, which clear nothing — correctly, because nothing ran. Round 5 called
   this comment "actively false"; it is now merely over-broad, but it is still not
   true, and after A7 it describes code that should not exist.
3. **`routes.py:792-794`** — "the `run_pipeline` above already invalidated the
   derived files". Not guaranteed: reach the 422 via *cache hit, then a concurrent
   run leaves a header-only `results.csv`* and no `run_pipeline` ran in this
   request. The outcome is still safe (the concurrent run did its own cleanup),
   but the stated reason is not the operative one.
4. **`routes.py:309-311`** — "Control characters are refused because they are
   meaningless in a chain id and would otherwise reach CSV cells and log lines."
   Only C0 and DEL are. Executed against `_valid_chain`:

   ```
   C0 0x01          valid=False        C1 NEL 0x85    valid=True
   DEL 0x7f         valid=False        C1 CSI 0x9b    valid=True
   NUL              valid=False        LS  U+2028     valid=True
   empty            valid=False        PS  U+2029     valid=True
   64 chars         valid=True         RLO U+202e     valid=True
   65 chars         valid=False        "=cmd|'/C calc'!A0"  valid=True
   ```

   Round 5 found this and it is unchanged. I looked for a sink for the accepted
   ones and found none (SSE is `json.dumps`, the href is `encodeURIComponent`, the
   banner is `textContent`), so it is a comment-vs-code gap, not a defect — but
   the last line of that table is the one thing a chain id genuinely is unsafe to
   carry into a CSV, and the comment claims the guard is there for exactly that
   class of risk.

---

# PART B — the best-effort `except OSError`

**`logger` is bound.** `scout/routes.py:52`, module scope,
`logger = logging.getLogger(__name__)`; the helper is a module-level function in
the same file. **NOT A DEFECT.**

**Is the trade right? Yes.** On Linux (prod and CI, `ubuntu-latest`) `unlink` of an
open file succeeds, and an `OSError` after a *successful* `run_pipeline` write into
the same directory is close to impossible — a read-only filesystem or a
permissions problem would have failed the `results.csv` write first, which raises
inside the `try` and returns 422/500 without reaching the cleanup. The residual —
a failed unlink means `/scout/download` serves the previous chain until the next
run — is real, but strictly better than destroying a completed, paid-for scoring
run over a file-handle race. M10 (unguarded unlink) dies on
`test_a_failed_cleanup_does_not_lose_the_run`, so the guard is load-bearing. The
loop also continues after a failure and uses `missing_ok=True`, both correct;
M11 (`missing_ok=False`) is an equivalent mutant — `FileNotFoundError` **is** an
`OSError`, so it is caught, and the only effect is three spurious warnings per
fresh job.

**CONFIRMED DEFECT (LOW, observability) — the level makes the one message that
means "we are now serving the wrong chain" indistinguishable from chatter.**
`routes.py:386` logs at `warning`. So do `routes.py:728` and `:735`, **both of
which fire on every single `/scout/analyze`**:

```python
            logger.warning(
                "UniProt resolution: id=%s name=%s identity=%s",
                uniprot_id or "(empty)", uniprot_name or "(none)", uniprot_identity_pct,
            )
            ...
                    logger.warning(
                        "Known binder lookup for %s: %d binders found",
                        uniprot_id, len(known_binders),
                    )
```

The docstring says "Logged rather than swallowed silently, because a cleanup that
keeps failing is worth seeing." It is not seeable: any level-based filter on
WARNING in this module is already saturated by two per-request info lines. Use
`logger.error`. The message is otherwise good — file, job dir, consequence — but it
never names **which chain now owns `results.csv`**, the one fact a responder needs
to judge whether a delivered CSV was wrong. One more `%s` closes that.

---

# PART C — `resetAll` vs `_clearChainScopedResults`, enumerated

| element | `_clearChainScopedResults` (`:865-875`) | `resetAll` (`:877-904`) | cleared per-run by |
|---|---|---|---|
| `viewer-container` | yes | via delegation | — |
| `epitope-legend` | yes | via delegation | — |
| `epitope-table-body` | yes | via delegation | — |
| `known-binders-body` | yes | via delegation | — |
| `known-binders-section.hidden` | yes | via delegation | — |
| `uniprot-info` | yes | via delegation | — |
| `uniprot-bar.hidden` | yes | via delegation | — |
| `flag-ref-grid` | yes | via delegation | — |
| `flag-reference.hidden` | yes | via delegation | — |
| `download-link` / `-full` display | **no** | yes (`:887-888`) | `runAnalysis:984` hides all of `#results-section`; `_handleAnalysisResult:440-449` sets **both** anchors on **both** branches |
| `analyze-error` | **no** | yes (`:890`) | `runAnalysis:983`, `runExample:1073` |
| `ppi-table-body` / `ppi-section` / `_currentPpiInterfaces` | **no** (deliberate) | yes (`:898-900`) | nothing populates them at all |
| `known-binders-count`, `ppi-count`, `feasibility-note` | no | no | inside hidden sections / chain-agnostic (round 5; unchanged) |

**Is anything cleared by exactly one that should be cleared by both? No.** Each of
the three `resetAll`-only groups is covered per-run elsewhere, traced above: the
download anchors are children of `#results-section`, which `runAnalysis` hides
before the stream opens and only `_handleAnalysisResult` re-shows — and it now
sets `display` on both anchors in both branches, so neither can survive a chain
switch; `#analyze-error` is hidden by `runAnalysis` before every run; the PPI trio
is dead in both directions.

**Is moving PPI into `resetAll` coherent? Behaviourally yes — but it did not do
what round 5 asked.** Round 5's objection was "three lines maintaining dead code
plus two parametrised tests *enforcing* it". The two test params are gone and the
three lines moved — into `resetAll`, where a **new** test now enforces them:

```python
        # tests/test_scout_chain_scoped_results.py:1108-1112
        for element_id in ("ppi-table-body", "ppi-section"):
            assert element_id in reset, (element_id, reset)
```

The count of tests standing guard over `renderPpiInterfaces` — a function with no
call site — is still two. The enforcement was relocated, not removed. NOT A DEFECT
(`resetAll`'s pre-refactor semantics are correctly preserved) but it is the same
over-build one file over. See the cut list.

**Still open, unchanged from round 5 (LOW):** the shared clear is a completeness
guard, not a concurrency guard. `renderViewer` is `async` and unawaited, so a
late-resolving render from the previous chain can still repaint the table and flag
cards over the new chain's page. Its comment presents "unconditional and first" as
the answer to `renderViewer` being unawaited; it is the answer to half of it.

---

# PART D — mutation table

**Method.** 63 mutants, each applied alone at byte level with the file's own
terminator (CRLF for all four production files), sha256-confirmed landed before
any conclusion, restored from an **on-disk** snapshot and sha256-confirmed after.
Baseline, measured not quoted:
`pytest -q -p no:randomly tests/test_scout_chain_scoped_results.py tests/test_scout_anonymous_access.py`
-> **128 passed, 1 skipped, 0 failed**. Runs used `-x`, so the table names the
**first** failing test, not all of them: up to four other sessions were running the
full suite on this machine and fail-fast was the only way to fit 63 mutants.
Every one of the 12 survivors was then re-run against the wider `-k scout` scope
(**192 passed, 3 skipped** baseline) and **none flipped**.

| # | mutation | verdict | first test that fails |
|---|---|---|---|
| M01 | revert chain-scoped cache key to bare exists() (the original bug) | **CAUGHT** | `test_chain_b_after_chain_a_returns_chain_b` |
| M02 | `_results_csv_chain_id` drops `or None` (blank cell becomes a value) | **CAUGHT** | `test_a_blank_chain_id_cell_is_a_miss_not_a_collision` |
| M03 | header-only CSV claims a chain | **CAUGHT** | `test_a_header_only_results_csv_is_a_miss` |
| M04 | undecodable results.csv raises instead of missing | **NOT CAUGHT** | - |
| M34 | `CSV_COLUMNS` loses `chain_id` | **CAUGHT** | `test_flags_column_list_matches_the_pipeline` |
| M35 | `run_pipeline` stops stamping the chain | **NOT CAUGHT** | - |
| M36 | `run_pipeline` stamps a hardcoded chain | **NOT CAUGHT** | - |
| M37 | `flags.py` column list loses `chain_id` | **CAUGHT** | `test_chain_b_after_chain_a_returns_chain_b` |
| M38 | `FEASIBILITY_CSV_COLUMNS` loses `chain_id` | **CAUGHT** | `test_the_column_list_carries_chain_id` |
| M39 | feasibility row stops stamping the chain | **CAUGHT** | `test_the_writer_row_matches_the_declared_columns` |
| M61 | feasibility row stamps a hardcoded chain | **NOT CAUGHT** | - |
| M05 | drop the cleanup call in `/scout/analyze` | **CAUGHT** | `test_a_chain_that_scores_nothing_leaves_no_downloadable_file` |
| M06 | drop the cleanup call in `/scout/progress` | **CAUGHT** | `test_progress_alone_invalidates_the_previous_chains_downloads` |
| M07 | cleanup skips `epitopes.csv` | **CAUGHT** | `test_a_chain_that_scores_nothing_leaves_no_downloadable_file` |
| M08 | cleanup skips `epitopes_annotated.csv` | **CAUGHT** | `test_a_chain_that_scores_nothing_leaves_no_downloadable_file` |
| M09 | cleanup skips `results_annotated.csv` | **CAUGHT** | `test_a_chain_that_scores_nothing_leaves_no_downloadable_file` |
| M10 | cleanup unlink unguarded (no `except OSError`) | **CAUGHT** | `test_a_failed_cleanup_does_not_lose_the_run` |
| M11 | cleanup uses `missing_ok=False` | **NOT CAUGHT** | - |
| M12 | cleanup is a no-op | **CAUGHT** | `test_a_chain_that_scores_nothing_leaves_no_downloadable_file` |
| M15 | delete the `else:` unlink of `epitopes_annotated.csv` | **NOT CAUGHT** | - |
| M16 | delete the `else:` unlink of `epitopes.csv` | **NOT CAUGHT** | - |
| M62 | that same unlink loses `missing_ok=True` | **CAUGHT** | `test_top3_download_never_serves_the_previous_chain` |
| M13 | 409 branch never taken (stolen file becomes a 422) | **CAUGHT** | `test_a_stolen_results_file_is_a_409_not_an_empty_200` |
| M14 | 409 branch always taken (nothing-scored reported as a collision) | **CAUGHT** | `test_a_chain_that_scores_nothing_is_not_reported_as_a_collision` |
| M58 | analyze re-reads results.csv without the chain gate | **CAUGHT** | `test_a_stolen_results_file_is_a_409_not_an_empty_200` |
| M17 | binder cache ignores the chain it was built for | **CAUGHT** | `test_explicit_residues_do_not_inherit_another_chains_binders` |
| M18 | undecodable `analyze_cache.json` raises | **NOT CAUGHT** | - |
| M33 | **binder overlaps called without the chain (call site)** | **NOT CAUGHT** | - |
| M19 | `_valid_chain` accepts anything | **CAUGHT** | `test_json_routes_reject_unsafe[]` |
| M20 | `_valid_chain` drops the control-character check | **CAUGHT** | `test_json_routes_reject_unsafe[A\nB]` |
| M21 | `_valid_chain` drops the length cap | **CAUGHT** | `test_json_routes_reject_unsafe[A*65]` |
| M22 | `_valid_chain` drops the emptiness check | **CAUGHT** | `test_json_routes_reject_unsafe[]` |
| M23 | cap tightened back to 16 (round 3's mismatch) | **CAUGHT** | `test_a_long_chain_id_the_dropdown_offers_is_not_refused` |
| M24 | guard off `POST /scout/analyze` | **CAUGHT** | `test_json_routes_reject_unsafe[A\nB]` |
| M25 | guard off `GET /scout/progress` | **CAUGHT** | `test_sse_routes_reject_unsafe[A\nB]` |
| M26 | guard off `POST /scout/feasibility/analyze` | **CAUGHT** | `test_json_routes_reject_unsafe[A\nB]` |
| M27 | guard off `GET /scout/feasibility/progress` | **CAUGHT** | `test_sse_routes_reject_unsafe[A\nB]` |
| M63 | `_valid_chain` allows DEL (0x7f) | **NOT CAUGHT** | - |
| M28 | feasibility JSON route back to job-scoped epitope lookup | **CAUGHT** | `test_epitope_id_is_not_resolved_against_another_chain` |
| M29 | feasibility SSE route back to job-scoped epitope lookup | **CAUGHT** | `test_sse_separates_no_results_from_unknown_epitope` |
| M30 | JSON route loses the unknown-epitope 3-way split | **CAUGHT** | `test_an_unknown_epitope_id_says_so_on_the_json_route_too` |
| M31 | SSE route loses the 3-way split | **CAUGHT** | `test_sse_separates_no_results_from_unknown_epitope` |
| M32 | SSE route collapses no-results into unknown-epitope | **CAUGHT** | `test_sse_separates_no_results_from_unknown_epitope` |
| M40 | the clear call removed from `_handleAnalysisResult` | **CAUGHT** | `test_the_clear_runs_before_anything_renders` |
| M41 | the clear deferred via `setTimeout` | **CAUGHT** | `test_the_clear_runs_before_anything_renders` |
| M56 | the clear MOVED to the end, literal preserved | **CAUGHT** | `test_the_clear_runs_before_anything_renders` |
| M57 | the clear made conditional on there being epitopes | **CAUGHT** | `test_the_clear_runs_before_anything_renders` |
| M42-M50 | the clear skips exactly one element (9 mutants, one per id) | **CAUGHT** (9/9) | `test_every_chain_scoped_element_is_cleared[<id>]`, or `test_the_uniprot_bar_does_not_survive_a_chain_switch` for the two uniprot ids |
| M51 | `resetAll` stops delegating to the shared clear | **CAUGHT** | `test_reset_all_still_clears_every_chain_scoped_element` |
| M59 | `resetAll` stops clearing `ppi-table-body` | **CAUGHT** | `test_reset_all_still_clears_every_chain_scoped_element` |
| M52 | feasibility href back to the live dropdown | **CAUGHT** | `test_feasibility_link_uses_the_scored_chain_not_the_dropdown` |
| M55 | **`renderEpitopeTable` call site drops the chain argument** | **NOT CAUGHT** | - |
| M53 | `showAnalyzeError` stops re-enabling the button | **CAUGHT** | `test_an_error_re_enables_the_analyze_button` |
| M54 | dead top-3 download link shown unconditionally | **CAUGHT** | `test_the_dead_top_3_download_link_stays_hidden` |
| M60 | the zero-epitope explanation deleted | **NOT CAUGHT** | - |

## What the table says — 51/63 (81%), and only 4 of the 12 survivors are gaps

Round 5 measured 36/43 (83.7%). The raw rates are not comparable: 20 of my 63
mutants are new, and I deliberately included call-site, equivalence and redundancy
probes that a "did the fix land" sweep would not contain. Classified:

| survivor | verdict |
|---|---|
| M11 | **Equivalent mutant.** `FileNotFoundError` **is** an `OSError`, so `missing_ok=False` is caught by the same handler; the only effect is three spurious warnings per fresh job. Nothing to test |
| M15, M16 | **Not a coverage gap — a redundancy proof.** See A7: with M62 caught and M15/M16 surviving, the branch is required to be a no-op and is one. Delete the branches rather than test them |
| M04, M18, M63 | **Defence in depth with no reachable trigger.** A `results.csv` / `analyze_cache.json` this app wrote is not going to be undecodable, and I found no sink a DEL byte can reach (SSE is `json.dumps`, the href is `encodeURIComponent`, the banner is `textContent`). Correctly untested |
| M35 | **CI-caught, not a gap.** With the real pipeline, an empty stamp makes `_results_csv_chain_id` return None -> 422, so `test_analyze_runs_the_real_pipeline` (which asserts 200) fails wherever `freesasa` is installed. Confirmed by reading, not by running — `freesasa` is absent here |
| M60 | **Minor gap, cosmetic.** Round 5's M43, unchanged: the user-facing empty-chain explanation has no assertion |
| **M33, M55, M36, M61** | **REAL GAPS — four, all one line each** |

### The four real gaps

| gap | what regresses silently | the one line that closes it |
|---|---|---|
| **M33** `routes.py:1242` stops passing `chain_id` to `_get_binder_overlaps` | every feasibility response returns `known_binder_overlaps: []`; known binders vanish from the report with no error | in `test_explicit_residues_do_not_inherit_another_chains_binders`, repeat the request with `"chain": "A"` and assert the overlaps are **non-empty** |
| **M55** `index.html:555` stops passing `chain` to `renderEpitopeTable` | `chain` is `undefined`, so `chain \|\| chain-select.value` falls back to the **live dropdown** — round 3's bug, restored | `assert "renderEpitopeTable(epitopes, chain);" in page`, beside the two asserts already in `test_feasibility_link_uses_the_scored_chain_not_the_dropdown` |
| **M36** `run_pipeline` stamps a literal instead of `chain_id` | every `results.csv` claims chain A; every other chain becomes a permanent 422 | assert the stamp equals the requested chain inside the freesasa-gated `test_analyze_runs_the_real_pipeline`, and use a chain that is not `"A"` |
| **M61** `run_feasibility_pipeline` stamps a literal | `feasibility_results.csv` mislabels its chain — precisely what the stamp exists to prevent, on the file served with no chain parameter | `tests/test_scout_interface_competition.py` already drives the real feasibility pipeline (it patches `compute_rsa`, so `freesasa` is not needed): add `assert row["chain_id"] == chain` |

M33 and M55 are the same shape, and it is the shape this review keeps hitting:
**the function is tested, the wiring into it is not.**
`test_binder_overlaps_are_actually_returned_for_the_right_chain` calls
`_get_binder_overlaps` directly; `test_feasibility_link_uses_the_scored_chain_not_the_dropdown`
asserts the *definition* signature and the *href expression*. Neither touches the
single call site that supplies the chain. M36/M61 are the same shape one level
down: nothing anywhere, locally or in CI, asserts the stamped value **is** the
chain that was scored — only that a `chain_id` column exists and is non-empty
(`test_the_writer_row_matches_the_declared_columns` parses the dict literal's
KEYS). Verified by AST that the code is correct today:

```
P7  is the stamp the chain that was scored, or just non-empty?
  run_pipeline row chain_id             <- Name(id='chain_id', ctx=Load())
  run_feasibility_pipeline row chain_id <- Name(id='chain_id', ctx=Load())
```

### Where template string tests are, and are not, adequate

**Adequate.** `test_every_chain_scoped_element_is_cleared` parametrises over the
whole set, so "the id is named in the function" is equivalent to "the element is
cleared" for any single-line deletion — M42-M50 all die, 9 for 9.
`test_the_clear_runs_before_anything_renders` is stronger than its round-5 version:
it kills M56, which **keeps the exact literal `_clearChainScopedResults();`** and
only moves it past the renderers, and M57, which only wraps it in an `if`. That is
a real ordering test, not a name check. And M51 dying closes round 5's M37 gap —
`resetAll`'s delegation is now pinned.

**Not adequate.** Anything whose correctness depends on the named thing being
*wired* rather than *present*. M55 is the proof: the definition
`function renderEpitopeTable(epitopes, chain)` and the href expression both stay on
the page while the one call that supplies `chain` is gone, and every test passes.
Round 5's M38 (neutering the button lookup to `null`) is the same ceiling. With no
JS harness in the repo that is a ceiling rather than an oversight — but M55 shows
it is one string assertion away from being closed for the case that matters.

---

# PART E — the change read as one piece

**It is bigger than the change round 5 called over-built.** Measured, not
estimated:

```
scout/routes.py            +267 -57   58 comment-only, 23 blank, 186 other
                                      (186 includes ~36 docstring lines and the
                                       ~35-line CSV loop that was RE-INDENTED)
templates/scout/index.html  +72 -16   41 comment-only (57% of the additions)
scout/pipeline.py           +12  -0    8 comment-only, 4 code
scout/flags.py               +1  -0
tests/test_scout_chain_scoped_results.py
                           1220 lines, 39 tests, 105 asserts,
                           242 docstring + 42 comment = 284 prose lines
                           -> 2.7 prose lines per assertion, 31 file lines per test
```

Round 5 measured routes `+236`, template `+65`, tests `1055 / 33 / 88`. Since then
the change has grown by **+31 routes lines, +7 template lines, +165 test lines,
+6 tests** — while the author's stated trim removed two test params and one dead
local. Round 5's cut-list item 6, the one item that would have made it *smaller*,
was declined; M15/M16 now show the code it would have removed is dead weight (A7).

The correctness core remains small and right: stamp `chain_id` (4 lines), compare
it in the gate (3 lines), invalidate the derived files after each `run_pipeline`
(2 lines). Everything else found along the way — the derived files, the binder
cache, the feasibility epitope lookup — is legitimately in scope and is the best
part of the change.

## Concrete cut list

**Must go — redundant, or asserts something the code does not do**

| what | lines | why |
|---|---|---|
| `scout/routes.py:912-913` and `:933-934` — both `else: <path>.unlink(missing_ok=True)` | 4 | M15/M16 survive: nothing depends on them, because `_remove_derived_result_files` already removed the file. They are also the last unguarded unlinks, i.e. the only remaining way a filesystem error kills a completed run (A6, reproduced). Deleting them fixes A6 and A7 at once |
| `scout/routes.py:895-901` — "Both top-3 files are rewritten or removed… The paths that return before this point clear them instead" | 7 | False for the 400/404/503 returns, and after the cut above it describes code that no longer exists. Delete the whole comment |
| `scout/routes.py:357` — "Invalidate everything derived from `results.csv`" | 1 | `analyze_cache.json` is a fourth derived file and is not invalidated (A8.1). Reword to "the three files `/scout/download` can serve" |
| `scout/routes.py:792-794` — "the `run_pipeline` above already invalidated the derived files" | 3 | Not guaranteed on the cache-hit-then-concurrent-miss route (A8.3). State the real reason: whoever last wrote `results.csv` cleared them |
| `scout/routes.py:309-311` — "Control characters are refused" | 3 | Executed: C0 and DEL are refused; C1 `0x85`/`0x9B`, U+2028, U+2029 and U+202E all pass. Say "C0 and DEL" |

**Should go — QC archaeology and duplication (round 5's list, unchanged)**

| what | lines | why |
|---|---|---|
| `scout/routes.py:307-311`, `:317`, `:785-786`; `templates/scout/index.html:837-842`, `:858-860` | ~22 | "an earlier version of this comment claimed otherwise and was wrong", "the mismatch QC round 3 found at 16", "the destructive behaviour QC round 2 found here", "Only `_finalizeAnalysis`'s finally used to do it", "Four rounds of QC found four different elements missing from four different places". Review history belongs in the PR body; in six months it is noise |
| `tests/…:400-521` `TestChainIdIsValidatedAtTheBoundary` | ~122 | Round 5 asked for two parametrised tests. Keep `_valid_chain` (5 lines, input validation at a trust boundary) and `test_json_routes_reject_unsafe` / `test_sse_routes_reject_unsafe` — M19-M27 all die on them. Drop the 25-line class docstring and `test_a_newline_chain_cannot_forge_an_sse_frame`, whose own docstring says it "passes with `_valid_chain` deleted entirely" |
| `tests/…:870-882` `test_the_uniprot_bar_does_not_survive_a_chain_switch` | 13 | Confirmed pure duplication: it asserts `"uniprot-bar" in clear_fn and "uniprot-info" in clear_fn`, which `test_every_chain_scoped_element_is_cleared[uniprot-bar]` and `[uniprot-info]` already assert against the same extracted string |
| `templates/scout/index.html:894-900` + `tests/…:1108-1112` + `renderPpiInterfaces` + `_currentPpiInterfaces` + `#ppi-section` markup | ~65 | Either wire the PPI table up or delete all of it. Keeping the dead code AND a test that enforces it is the thing round 5 objected to; this round moved both rather than removing either (PART C) |

**Must stay**

`_results_csv_for_chain` + `_results_csv_chain_id` + the stamp; both
`_remove_derived_result_files` call sites; its `try/except OSError` (M10 dies
without it — raise the level, do not remove the guard); the 409/422 split (M13 and
M14 both die); `_get_binder_overlaps`' chain gate (M17); both feasibility chain
gates (M28, M29); the shared clear and its ordering (M40, M41, M56, M57);
`resetAll`'s delegation (M51).

**Add — four one-line test additions**, in PART D's "four real gaps" table.

---

# PART F — deferred items, adjudicated

| item | verdict |
|---|---|
| **CSV formula injection** (`chain_id` unescaped in the delivered CSV) | **Correctly deferred. NOT a blocker.** Executed: `_valid_chain("=cmd\|'/C calc'!A0") → True`. The payload must be a *real* chain id in a real structure, so PDB (one byte in column 22) cannot carry one — only a hand-crafted mmCIF with a multi-character `auth_asym_id`. Ownership (`resolve_owned_job_dir`) means nobody else can fetch the CSV, so the exposure is a user opening or forwarding their own results after uploading someone else's structure. The test-class docstring states the residual honestly. One tension: the code comment at `:300-305` says the guard validates "what is unsafe to carry", and the one thing genuinely unsafe to carry into a CSV is the thing it explicitly does not block |
| **>64-char chain id offered-then-refused** | **Correctly deferred. NOT a blocker.** Executed: 64 passes, 65 fails, cap pinned by M23 and by `test_an_absurd_chain_id_is_still_refused`. Reaching it needs a hand-built mmCIF |
| **rollback / mixed-fleet `ValueError` → 500** | **Correctly deferred as code; still not written down.** A pre-change worker reading a post-change `results.csv` builds `CSV_COLUMNS_ANNOTATED` without `chain_id`, and `DictWriter.writerow` on a row that has one raises `ValueError: dict contains fields not in fieldnames` — at `routes.py:904-913`, **outside every `try`**, so an unhandled 500. `--preload` means workers restart together, so this is **rollback only**, bounded by job-dir lifetime (reaped within the hour). Round 5 asked for one line in the commit message ("forward-only; do not roll back with live job dirs"). It is still not there, and it is free |
| **`feasibility_results.csv` served with no chain param** | **Correctly deferred. NOT a blocker.** `feasibility_download` takes only `job_id`, but `run_feasibility_pipeline` runs unconditionally on every `/scout/feasibility/analyze` and the download URL reaches the browser only on a 200 (`feasibility.html:526`) — so the reachable file is the one the user's own successful run just wrote. The stale case needs a refused chain-B request after a successful chain-A one plus a kept URL, and the `chain_id` stamp (now pinned by `test_the_column_list_carries_chain_id` and `test_the_writer_row_matches_the_declared_columns`, M38/M39 both die) makes it stale-but-labelled |
| **`renderPpiInterfaces` dead while `detect_interfaces` runs on every analyze** | **NOT correctly deferred — same verdict as round 5, now four rounds old.** `detect_interfaces(pdb_path, chain_id)` executes on every `/scout/analyze` (`routes.py:743`) and its result is serialised twice (response + `analyze_cache.json`). Nothing renders it. This round removed two tests enforcing the dead code and added one. Not a blocker — wasted compute and dead code, no wrong answers — but the fix is a delete, and it keeps being deferred into the next round |

---

# Status of every earlier round's open item

| item | status |
|---|---|
| R5 D1 — cleanup on analyze's return paths only; `/scout/progress` uncovered | **CLOSED.** Both `run_pipeline` call sites, mutation-caught both ways (M05, M06), and round 5's reproduction no longer reproduces (A5) |
| R5 D2 — the shared clear is not a concurrency guard | **STILL OPEN, LOW.** Unchanged. `renderViewer` is still async and unawaited; no generation token |
| R5 D3 — `renderPpiInterfaces` dead, with tests enforcing it | **HALF CLOSED.** The two parametrised params are gone; three lines and one enforcing assertion moved into `resetAll` and its test (PART C) |
| R5 D4 — dead `fieldnames: list[str] = []` local | **CLOSED.** Removed; `fieldnames` is now assigned only inside the `with` block |
| R5 D5 — the JSON feasibility route's third state | **CLOSED.** Same 3-way split as the SSE route; M30 dies on `test_an_unknown_epitope_id_says_so_on_the_json_route_too` |
| R5 M28 / M29 — the feasibility half of the stamp untested | **CLOSED.** `TestFeasibilityCsvNamesItsChain` kills M38 and M39. Fidelity of the stamped VALUE is still untested (M61) |
| R5 M37 — `resetAll`'s delegation untested | **CLOSED.** M51 dies on `test_reset_all_still_clears_every_chain_scoped_element` |
| R5 M43 — the zero-epitope explanation untested | **STILL OPEN**, cosmetic (M60) |
| R4 D4 — pre-deploy job dirs 404 the feasibility routes | **OPEN by decision.** Still nothing in the change says so |
| R2 D3 — rollback `ValueError` -> 500 | **OPEN.** Still no line in the commit message (PART F) |
| R1 D6 / R2 D4 — `feasibility_results.csv` served with no chain param | **MITIGATED, NOT CLOSED**, and the mitigation is now tested (PART F) |
| `/scout/progress` has no cache gate | **STILL OPEN**, and it now costs a little more than wasted compute (A9) |

# Things I could not verify

* **The real biophysics path.** `freesasa` is absent locally, so every route-level
  result comes from a stub writing the real CSV format through the real column
  list. M35's CI behaviour is reasoned from the code, not executed.
* **A6 on Linux.** Reproduced on Windows; on Linux `unlink` of an open file
  succeeds, so the crash is unreachable there — which is why it is LOW, and why
  the fix is deletion rather than a second `try`.
* **A4's mid-write crash.** Mechanism traced from the source; not induced.
* **R5 D2 in a real browser.** Not re-tested this round; nothing changed.

# Process note and one incident, disclosed

Up to four other sessions ran the full suite on this machine throughout, in
sibling worktrees with their own `tmp/`. That could not contaminate my results —
every file was hash-checked before each mutation and after each restore — but it
made a `-k scout` run take 66 s to 5 min depending on the minute, which is why the
sweep used the two scout test files as an explicit scope with `-x`, and why the
survivors were re-checked separately under the wider `-k scout`.

**Incident.** My first sweep driver kept its pristine copies **in memory**. When I
killed it to change the test scope, it died with mutation M04 still applied and
left `scout/routes.py` dirty (`42ece012…` instead of `7cca48a2…`). I caught it on
the next hash check, reversed the single byte-level edit, and re-verified against
the expected sha256 and against `git diff --stat` before running anything else.
The driver was then changed to restore from an **on-disk** snapshot, which is what
every later kill used. No pytest run was made against a dirty tree, and the final
state below is byte-identical to the pre-review one. This is the second round in a
row this has happened; a driver that cannot restore after being killed is the
wrong tool for a shared machine.

# Full suite

From the worktree root, no path argument, not piped through `tail`, once, after
every mutation had been restored:

```
$ venv/Scripts/python.exe -m pytest -q -rf
5341 passed, 20 skipped, 854 warnings in 501.54s (0:08:21)
EXIT=0

$ grep -cE "^FAILED|^ERROR" <output>
0
```

**Exactly the author's stated baseline: 5341 passed, 20 skipped, 0 failed.** (The
machine had gone quiet by then, hence 8:21 against round 5's 36:41 for the same
suite.)

# Restoration proof

```
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
?? docs/qc/scout-chain-scoped-results-round5.md
?? docs/qc/scout-chain-scoped-results-round6.md     <- the only file I added
?? tests/test_scout_chain_scoped_results.py

$ git diff --stat
 scout/flags.py                       |   1 +
 scout/pipeline.py                    |  12 ++
 scout/routes.py                      | 325 +++++++++++++++++++++++++++++------
 templates/scout/index.html           |  88 ++++++++--
 tests/test_scout_anonymous_access.py |  11 +-
 5 files changed, 362 insertions(+), 75 deletions(-)

$ sha256sum <the six touched files>
7cca48a274e343cc7681e8cb2946eeda5ada4a3214d1de5c125cc98d62327089  scout/routes.py
b4d8c62a9ee52c54671835dc1f744d62a36824a827b2b9492ca9b14c83758f0c  scout/pipeline.py
139c21ca8de895be52ac7be3fe5e49939d39fa0e94b8063220d28ee8c4b46045  scout/flags.py
ca5c59e22719b7b7e2447cdaf38f810f1afcde439c2f3096696ee58eb1e0907c  templates/scout/index.html
97d55d2916e944598da0e43f5b5a2299ea636b22dfdca2a3f6245fc71adecebf  tests/test_scout_anonymous_access.py
bf3da4769851f2f740ca59dec819600e1fbc89cd814a704e6651d70de1dee416  tests/test_scout_chain_scoped_results.py
```

Every hash is identical to the pre-review value taken before the first mutation
(`bf3da476…` for the untracked test file, which I never modified at all), and the
diff stat is byte-for-byte the one I started from: **362 insertions, 75 deletions**.
The only file I added to the repo is this report.
