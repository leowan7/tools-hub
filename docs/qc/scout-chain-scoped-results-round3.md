# QC round 3 — chain-scoped Scout results

Independent adversarial review of the seven fixes made in response to rounds 1
and 2. I did not write any of this code.

Environment: worktree `.claude/worktrees/suspicious-dewdney-13b07e`,
HEAD = main = `7fd180d`, interpreter `venv/Scripts/python.exe` (CPython 3.13,
Windows), `freesasa` absent so every route-level probe runs against a stubbed
`run_pipeline` writing the real CSV format through the real `_CSV_COLUMNS_BASE`.

Every mutation was applied at byte level with the file's own line endings
(CRLF for the five tracked files, LF for the new test file), and each one was
confirmed to have landed by md5 before any conclusion was drawn from it.
**All six files are restored byte-identical.** Proof at the bottom.

One self-inflicted result to disclose up front: my first `-k scout` run showed
2 failures. That was my own baseline full-suite job running concurrently
against the same `tmp/` — not a defect. After killing it and reaping `tmp/`,
`-k scout` is **168 passed, 3 skipped** clean, and that is the baseline every
mutation below is measured against.

---

## Verdict

**DO NOT SHIP as-is** — but the gap is small and none of it is in the backend
correctness core.

The seven fixes each do what they claim; I reproduced each failure round 2
described and showed it no longer happens. The 409 does not misfire in any
legitimate single-user flow (executed two ways). The loosened `_valid_chain`
opens no injection surface I could reach. Fifteen of twenty-one behaviours are
now caught by a test that was not there before.

What stops it is that **the fix for round 2's FE-3 cleared four stale elements
and missed the fifth** — the UniProt bar still shows the previous chain's
accession under the new chain's results (D1). That is the exact bug class this
whole change exists to eliminate, in the exact function that was edited to
eliminate it. Two lines.

Below that: the app still offers a chain it will 400 (D2 — round 2's FE-1
recommendation was not taken, only the boundary moved), and the one test round
2 explicitly asked for cannot see the guard it names (D3).

---

# PART A — did the seven fixes fix it?

| # | Fix | Verdict | Evidence |
|---|---|---|---|
| 1 | `_valid_chain` loosened | **YES** for the characters round 2 named | `_ - . = @ + \| *` all reach the pipeline; a structure whose chain is `=` scores end to end |
| 2 | 409 instead of the destructive fall-through | **YES**, and no false positives | F2 mutation → `test_a_stolen_results_file_is_a_409_not_an_empty_200` fails; sequential flows never 409 |
| 3 | `chain_id` in `FEASIBILITY_CSV_COLUMNS` | **YES** for the label, **NO** for the staleness | delivered CSV now reads `1,A,...`; the stale file is still served at 200 |
| 4 | 3-way SSE message | **YES** | F4 mutation → `test_sse_separates_no_results_from_unknown_epitope` fails; JSON sibling still disagrees (D8) |
| 5 | `showAnalyzeError` re-enables the button | **YES** | F5 mutation caught; DOM run shows `disabled=false, text='Analyze epitopes'` after the error |
| 6 | zero-epitope clears the previous chain | **PARTIAL** — four of five elements (D1) | viewer, legend, epitope table, known-binders cleared; **UniProt bar not** |
| 7 | Tests rewritten + new class | **PARTIAL** — 5 of 21 behaviours still uncovered | mutation table, Part C |

Fix 1, verified by execution — the round-2 D2 failure is gone:

```
POST /scout/analyze, job with chains A,B
  traversal        -> 200   (../../etc/passwd accepted, then scored)
  formula          -> 200
  sixteen pct      -> 200
  short html       -> 200
mmCIF whose auth_asym_id IS the payload
  traversal    dropdown=['../../etc/passwd', 'ZZ'] analyze=200
  formula      dropdown=['=cmd|calc!A1', 'ZZ']     analyze=200
```

Fix 2, verified by execution — **the 409 is not reachable without concurrency.**
Both real single-user sequences, alternating chains:

```
### the real UI sequence: progress(SSE) then analyze
  chain A: SSE stages=['done']  analyze=200  resi=[10..16]
  chain B: SSE stages=['done']  analyze=200  resi=[60..66]
  chain A: SSE stages=['done']  analyze=200  resi=[10..16]
  chain B: SSE stages=['done']  analyze=200  resi=[60..66]
  pipeline calls: ['A', 'B', 'A', 'B']

### analyze only, A B A A B B
  all 200, correct residues each time
  pipeline calls: ['A', 'B', 'A', 'B']       <- cache still works
```

Fix 5 and 6, verified by driving the **real page JS** (extracted from the
Flask-rendered `/scout/`) against a DOM stub — chain A with one epitope, then
chain B with none:

```
--- after chain B (epitopes=0, no uniprot, no binders) ---
   download-link (top3) display     = "none"          <- dead link hidden
   download-link-full display       = "inline-flex"   <- all-patches still there
   epitope-table-body rows          = 0               <- cleared
   epitope-legend                   = 0               <- cleared
   viewer-container                 = 0               <- cleared
   known-binders-section.hidden     = true            <- cleared
   analyze-error text  = "No epitopes scored above threshold for chain B. ..."
   analyze-btn disabled = false ; text = "Analyze epitopes"
   uniprot-bar.hidden  = false                        <- *** NOT cleared ***
   uniprot-info = "P12345 — Chain A protein (identity: 100)"   <- *** chain A ***
```

---

# PART B — what the fixes newly break

## D1 (MEDIUM–HIGH — wrong chain's data on screen) — the UniProt bar is never cleared on a chain switch

**Where.** `templates/scout/index.html:449-458` inside `_handleAnalysisResult`:

```js
if (data.uniprot_id) {
  ...
  document.getElementById('uniprot-info').innerHTML = info;
  document.getElementById('uniprot-bar').hidden = false;
}
```

There is no `else`. `#uniprot-bar` is hidden in exactly one place in the whole
page — `resetAll()` (`:880`), bound only to the Reset button, which discards
the job.

**Mechanism.** `resolve_uniprot_id(pdb_path, chain_id)` (`scout/routes.py:651`)
is per chain. Analyse a chain that resolves, then a chain that does not, and
the bar keeps the first chain's accession and protein name sitting above the
second chain's results. It is not limited to the zero-epitope case — a chain B
with three good epitopes and no UniProt hit shows chain A's protein name too.

**Evidence — executed** against the real page JS (see the block in Part A):
after `_handleAnalysisResult({chain:'B', epitopes:[], uniprot_id:''})`,
`uniprot-bar.hidden = false` and `uniprot-info` still reads
`P12345 — Chain A protein (identity: 100)`.

**Why this is the headline.** The fix under review cleared
`viewer-container`, `epitope-legend`, `epitope-table-body` and both
known-binder elements for precisely this reason, and wrote a comment saying so
("the same wrong-chain-labelled-as-right-chain result the backend fix exists to
remove, one layer up"). The UniProt bar is the same defect in the same
function, and it is the element that names the *protein*, which is the piece a
reader is most likely to take at face value. Realistic every time a heterodimer
has one chain UniProt can match and one it cannot — a peptide, a linker, a
designed binder.

**Fix (two lines).** Give the `if` an `else` that hides `#uniprot-bar`, or hide
it unconditionally before the `if`, the way known-binders is now handled at
`:483-484`.

---

## D2 (MEDIUM — refuses work that should succeed) — `/scout/upload` still offers a chain `/scout/analyze` 400s

**Where.** `scout/routes.py:310-318` (`_CHAIN_ID_MAX_LEN = 16`) versus
`scout/parser.py:290` (`cid = chain.get_id()`, verbatim) and the dropdown built
from it at `templates/scout/index.html:916-924` (`createElement` + `.value`/`.textContent`, so the dropdown itself is safe).

Round 2's FE-1 asked for the *offer* to be filtered through `_valid_chain`, so
the app never proposes what it will refuse. That was not done; only the
boundary moved. mmCIF `auth_asym_id` is an unconstrained code, so the
over-rejection survives at 17 characters.

**Evidence — executed.** Upload → read the dropdown → request the chain the
dropdown offered:

```
  17-char auth_asym_id   dropdown=['AAAAAAAAAAAAAAAAA', 'ZZ']
      POST /analyze -> 400 '{"error":"job_id and a valid chain id are required."}'
      SSE  /progress-> 'job_id and a valid chain id are required.'
  16-char auth_asym_id   dropdown=['AAAAAAAAAAAAAAAA', 'ZZ']
      POST /analyze -> 200
  20-char auth_asym_id   dropdown=['CHAIN_IDENT_TOO_LONG', 'ZZ']
      POST /analyze -> 400 '{"error":"job_id and a valid chain id are required."}'
```

`CHAIN_IDENT_TOO_LONG` is not a contrived string — entity-named chains out of
conversion and modelling pipelines look exactly like that.

**Severity is lower than round 2's** because fix 5 now un-sticks the page: the
error is legible and the Analyze button comes back. But the message still
blames the user for choosing the only thing on offer, and the same structure
scored fine on `main`.

**Fix.** One `if` in `/scout/upload`'s chain list, or drop the length cap and
let `run_pipeline`'s own "Chain 'X' not found. Available chains: ..." own the
decision, which is what `shared/pdb_inspect.validate_target_chain` — the
validator every other tool in this repo uses — already does.

---

## D3 (MEDIUM — a test that certifies false) — `test_json_routes_reject_unsafe` cannot see the `/scout/feasibility/analyze` guard

**Where.** `tests/test_scout_chain_scoped_results.py:408-414`. The test loops
over both JSON routes and asserts `resp.status_code == 400`. Round 2's M6c said
that boundary was uncovered and asked for exactly this test.

**It still is uncovered.** `/scout/feasibility/analyze` is called with no
`epitope_residues` and no `epitope_id`, so with the chain guard removed it
falls through to `return jsonify({"error": "epitope_residues or epitope_id is
required."}), 400` (`scout/routes.py:1090`). Same status, different reason,
assertion satisfied.

**Evidence — executed**, guard removed (`_valid_chain(chain_id)` →
`chain_id`, md5 `79ca6bb4…`):

```
  GUARD REMOVED  chain='A\n\ndata: forged'    -> 400  'epitope_residues or epitope_id is required.'
  GUARD REMOVED  chain='AAAAAAAAAAAAAAAAA'    -> 400  'epitope_residues or epitope_id is required.'
  GUARD REMOVED  chain='A\x00B'               -> 400  'epitope_residues or epitope_id is required.'
  with explicit residues -> 422; feasibility pipeline was asked for chain ['AAAAAAAAAAAAAAAAA']

full scout subset with the guard gone: 168 passed, 3 skipped
```

The last line is the point: the value really does reach
`run_feasibility_pipeline` once residues are supplied, and nothing notices.

**Fix (one line).** Assert the message, exactly as the author already did for
the SSE sibling: `assert "valid chain id" in resp.get_json()["error"]`.

---

## D4 (LOW–MEDIUM — security regression against round 2) — CSV formula injection is re-opened, and the stated justification does not cover the named threat

**Where.** `scout/pipeline.py:491` stamps the raw chain string;
`scout/routes.py:822` copies it into `results_annotated.csv`; `:916` serves
that as `all_patches.csv`.

The change's justification is that job dirs are session-owned, "so the only
person who can download a CSV containing a crafted chain id is whoever uploaded
the structure containing it."

**The ownership half is true — I verified it.** Every download route
(`/scout/download`, `/scout/feasibility/download`, `/scout/pdb`, the handoff)
goes through `_resolve_job_dir` → `resolve_owned_job_dir`, which requires a
strict-UUID job id, `safe_join` confinement, and a `.owner` match. Executed:

```
  GET /scout/download/<job>?full=1 -> 200 filename=all_patches.csv
     row   : '1,=cmd|calc!A1,"ALA200,...",7,0.'
  a DIFFERENT session GET -> 404 '{"error":"Results not found. Please run analysis first."}'
```

**The conclusion drawn from it does not hold.** The threat round 1 D4 actually
named was not cross-user reads; it was *"run my structure through Scout and
send me the CSV"* — the attacker supplies the `.cif`, the victim uploads it,
scores it, and opens `all_patches.csv` in a spreadsheet. Ownership is satisfied
throughout that path, because the victim is the owner. `chain_id` is still the
only free-text column in an otherwise machine-generated file (numbers,
`STANDARD_AA` names, DSSP letters, `0`/`1`).

I am ranking this LOW-MEDIUM, not blocking: it is CWE-1236, it needs a social
step, and modern Excel warns on DDE. But it is a knowing regression against the
state round 2 left, and the cheaper alternative round 2 named — escape at the
*writer* (prefix a leading `= + - @` with `'`), which costs one function and
keeps every legitimate chain id working — was not taken. Either take it or say
in the code comment that the risk is accepted; right now the comment argues a
narrower threat than the one on record.

**Second symptom, Windows-only.** Every CSV `open()` in `scout/pipeline.py` and
`scout/routes.py` omits `encoding=`, so a non-ASCII chain id raises
`UnicodeEncodeError` (a `ValueError` subclass → clean 422 with a codec message
in it) on cp1252. Production and CI are Linux/UTF-8; not a prod defect.

---

## D5 (LOW — wasted compute, misleading message) — a header-only `results.csv` is now a permanent 409 that re-runs the pipeline every time

**Where.** `scout/routes.py:695-707`.

`_results_csv_for_chain` correctly treats a header-only file as a miss, so the
gate at `:642` runs the pipeline; if the pipeline writes another header-only
file (killed worker, full disk) the re-read misses again and the route answers
409 — a message that says *"another analysis on this job replaced the results"*
when nothing of the sort happened.

**Evidence — executed**, `run_pipeline` stubbed to leave a header-only file:

```
  attempt 1: 409 'Another analysis on this job replaced the results while this one was running...'
  attempt 2: 409 'Another analysis on this job replaced the results while this one was running...'
  pipeline calls: ['A', 'A']          <- a full rescore burned per attempt
```

Reachability is narrow — `run_pipeline` guards at `pipeline.py:346` and `:361`
mean it cannot emit a header-only file on its own. Pre-fix this state was a
permanent silent 200-with-zero-epitopes, so the 409 is still the better answer;
only the wording and the unbounded re-run are wrong.

---

## D6 (LOW — wasted compute every analyze) — `renderPpiInterfaces` is dead, and the backend computes for it anyway

**Confirmed, not refuted.** Every reference to it in the page:

```
templates/scout/index.html:749  function renderPpiInterfaces(interfaces, epitopes) {   <- definition
templates/scout/index.html:878  document.getElementById('ppi-section').hidden = true;  <- resetAll
templates/scout/index.html:879  document.getElementById('ppi-table-body').innerHTML = '';
```

No call site. `let _currentPpiInterfaces = []` (`:318`) is declared, cleared in
`resetAll` (`:885`), and never assigned or read.

Meanwhile `scout/routes.py:671` runs `detect_interfaces(pdb_path, chain_id)` on
every `/analyze`, and the result is serialised into the response twice
(`:852` in `analyze_cache.json`, `:878` in the JSON body). The DOM run confirms
the consequence: `ppi-section.hidden = true` after a fully populated chain-A
result carrying one PPI interface.

So the interface detection is real work whose only surviving consumer is
`interface_competition` in the *feasibility* pipeline (a separate call at
`scout/pipeline.py:712`). On the analyze path it is computed, transmitted and
discarded. Not caused by this change; worth a line in the same commit that is
already touching this function.

---

## D7 (LOW — a guard whose stated reason is false) — no chain id can forge an SSE frame, with or without `_valid_chain`

The comment at `scout/routes.py:303-307` and the docstring of
`test_a_newline_chain_cannot_forge_an_sse_frame` both assert that control
characters are refused because "chain_id is interpolated into an SSE stream,
where a newline terminates the event and lets a crafted id forge a frame".

**All eight SSE emitters go through `json.dumps`** — `scout/routes.py:943, 996,
1005, 1022, 1157, 1206, 1256, 1265` — and there is no raw interpolation
anywhere. `json.dumps` escapes CR, LF and every control character:

```
'A\n\ndata: {"stage": "done"}'  ->  'data: {"stage": "error", "msg": "Chain \'A\\n\\ndata: {\\"stage\\": \\"done\\"}\' not found"}\n\n'
'A B'                      ->  ... "Chain \'A\\u2028B\' not found" ...
'A\x00B'                        ->  ... "Chain \'A\\u0000B\' not found" ...
```

The newline is two characters (`\` + `n`), not a line break, so the stream
still carries exactly one real frame.

The test *does* fail when the guard is removed (it is in F1b's 15), but only
because it counts the literal substring `data: ` and that substring survives
*escaped* inside the JSON payload. It never demonstrates a second frame, and
its sibling assertion (`'"stage": "done"' not in body`) passes with the guard
gone because the quotes come back escaped.

Keep the rule — control characters in a CSV cell and a metadata blob are worth
refusing on hygiene grounds — but the comment and the docstring should say what
is actually true, or the next person will take the SSE claim as verified.

---

## D8 (LOW) — round 2's FE-5 is half fixed: the two feasibility routes still disagree

`/scout/feasibility/progress` now says the right thing for an `epitope_id` that
is not in an analysed chain's results (fix 4, tested). `/scout/feasibility/analyze`
on the same condition still answers:

```
  chain='A' (analysed), epitope_id not present
  -> 400 'epitope_residues or epitope_id is required.'
```

which is false — both were supplied. Low, because the UI opens the SSE first,
so the correct message is the one the user reads. It is also the same
indistinguishable-400 that makes D3 possible.

---

# PART C — are the new tests real?

Baseline for every row: `-k scout` → **168 passed, 3 skipped** (the 3 skips are
2× Supabase config + 1× freesasa). Each behaviour was reverted alone, the
mutation confirmed landed by md5, the subset re-run, then restored.

| # | Behaviour reverted | Verdict | Tests that fail |
|---|---|---|---|
| F1a | `_valid_chain` back to `[A-Za-z0-9]{1,8}` | **CAUGHT (9)** | `test_parser_reachable_ids_are_not_refused` × 8 (`_ - . = @ + \| *`) + `test_a_parser_reachable_id_analyses_end_to_end` |
| F1b | `_valid_chain` accepts everything | **CAUGHT (15)** | `test_json_routes_reject_unsafe` × 7, `test_sse_routes_reject_unsafe` × 7, `test_a_newline_chain_cannot_forge_an_sse_frame` |
| F1c | guard off `POST /scout/analyze` | **CAUGHT (6)** | `test_json_routes_reject_unsafe` × 6 |
| F1d | guard off `GET /scout/progress` | **CAUGHT (7)** | `test_sse_routes_reject_unsafe` × 6 + the forge test |
| **F1e** | **guard off `POST /scout/feasibility/analyze`** | **NOT CAUGHT** | — (D3) |
| F1f | guard off `GET /scout/feasibility/progress` | **CAUGHT (6)** | `test_sse_routes_reject_unsafe` × 6 |
| F2 | 409 → the destructive empty-200 fall-through | **CAUGHT (1)** | `test_a_stolen_results_file_is_a_409_not_an_empty_200` |
| F3a | `chain_id` out of `FEASIBILITY_CSV_COLUMNS` (stamp kept) | **CAUGHT (8)** | all of `test_scout_interface_competition.py` — but only because `DictWriter` raises on the inconsistency, not because anything checks the column |
| **F3b** | **`chain_id` not stamped in `run_feasibility_pipeline`** | **NOT CAUGHT** | — (the column is written empty; nothing reads it) |
| F4 | 3-way SSE message → 2-way | **CAUGHT (1)** | `test_sse_separates_no_results_from_unknown_epitope` |
| F5 | `showAnalyzeError` stops re-enabling the button | **CAUGHT (1)** | `test_an_error_re_enables_the_analyze_button` |
| **F6a** | **zero-epitope clear disabled (`else` → `else if (false)`)** | **NOT CAUGHT** | — the test only checks that `epitope-table-body` *appears* in the handler text |
| F6b | top-3 link shown unconditionally | **CAUGHT (1)** | `test_an_empty_chain_clears_the_previous_chains_results` |
| **F6c** | **known-binders no longer cleared unconditionally** | **NOT CAUGHT** | — |
| F6d | feasibility link back to the live dropdown | **CAUGHT (1)** | `test_feasibility_link_uses_the_scored_chain_not_the_dropdown` |
| M3 | analyze CSV reader back to job-scoped | **CAUGHT (1)** | `test_a_stolen_results_file_is_a_409_not_an_empty_200` |
| M4b | `_get_binder_overlaps` gutted to `return []` | **CAUGHT (1)** | `test_binder_overlaps_are_actually_returned_for_the_right_chain` |
| M9 | unstamped legacy CSV becomes a HIT | **CAUGHT (1)** | `test_a_pre_fix_results_csv_is_a_miss_not_a_wrong_answer` |
| M11 | header-only CSV becomes a HIT | **CAUGHT (1)** | `test_a_header_only_results_csv_is_a_miss` |
| M-cache | analyze cache gate back to bare existence | **CAUGHT (7)** | the chain-scoping class + boundary tests |
| **M-pipe** | **pipeline stops stamping `chain_id`** | **NOT CAUGHT locally** | CI-only, via `test_analyze_runs_the_real_pipeline` (needs freesasa) — unchanged from round 2's M1 |

**16 of 21 caught, up from 9 of 18 in round 2.** The five that are not:

* **F1e** is the one round 2 explicitly asked for, and the test that was written
  for it cannot see it. See D3.
* **F3b** means fix 3's actual payload — the chain landing in the feasibility
  CSV — is untested. F3a only catches the writer/column *inconsistency*.
* **F6a / F6c** are template behaviours pinned by string-presence assertions on
  the rendered page rather than by behaviour, so a one-token disablement slips
  through. There is no JS test harness in this repo, so this is a known ceiling
  rather than an oversight; the honest statement is that the template tests
  catch *deletion*, not *disablement*.
* **M-pipe** is CI-only and unchanged.

## Are the new tests vacuous?

**No, on the point the brief raised — checked.** No assertion in the file reads
the echoed `chain` field:

```
grep -n 'get_json()\["chain"\]\|\.get("chain")' tests/test_scout_chain_scoped_results.py  -> no matches
```

Evidence is residue numbers (`CHAIN_RESIDUES = {"A": 10-16, "B": 60-66}`), and
the stub maps chain → a fixed distinct set rather than echoing the request, so a
wrong cache hit produces the *other* chain's numbers. `_residue_numbers()`
asserts `body["epitopes"]` is non-empty before indexing, so a zero-epitope
response cannot pass silently.

**The rewritten boundary class does fail against the old regex — the author's
"9 failures" claim is exact.** F1a, measured:

```
9 failed, 159 passed, 3 skipped
  test_parser_reachable_ids_are_not_refused[_] [-] [.] [=] [@] [+] [|] [*]
  test_a_parser_reachable_id_analyses_end_to_end
```

(The `1` and `a` params legitimately survive — they are alphanumeric.)

**The negative controls are present and load-bearing**:
`test_same_chain_twice_still_reuses_the_cached_run` (without it, "always
rescore" would pass everything), `test_epitope_id_still_resolves_for_the_matching_chain`,
`test_binder_overlaps_are_actually_returned_for_the_right_chain` (round 2's
missing M4b positive control — added, and it works).

---

# PART D — status of everything rounds 1 and 2 left open

| Item | Status |
|---|---|
| R1 D1 — `/scout/download` serves the previous chain's top-3 | **CLOSED**, tested (F6b + `test_top3_download_never_serves_the_previous_chain`) |
| R1 D2 — known-binder overlaps cross chains | **CLOSED**, tested both directions (M4b) |
| R1 D3 — the 404 message never reaches the user | **CLOSED** for the SSE (F4); the JSON sibling still disagrees (D8) |
| R1 D4 — `chain_id` is attacker-controlled free text in a downloadable CSV | **RE-OPENED** on purpose (D4) |
| R1 D5 — `except OSError` too narrow | **CLOSED** — now `(OSError, csv_module.Error, UnicodeDecodeError)` at `routes.py:345`, and `_get_binder_overlaps` gained `UnicodeDecodeError` too |
| R1 D6 / R2 D4 — `feasibility_results.csv` cannot name its chain | **MITIGATED, NOT CLOSED** — see below |
| R2 D1 — overlapping run deletes the finished run's downloads, serves empty CSV as 200 | **CLOSED** by the 409, tested (F2) |
| R2 D2 — `_valid_chain` 400s ids the app itself offers | **MOSTLY CLOSED**; residual at >16 chars (D2) |
| R2 D3 — rollback / mixed fleet `ValueError` → 500 | **STILL OPEN**, reproduced below |
| R2 D5 / FE-5 — the SSE message asserts something false | **CLOSED** for the SSE (F4); JSON half open (D8) |
| R2 D6 / D7 — transient live edits reverting the fix | **GONE** — `_results_csv_for_chain` is the briefed form; M9 and M11 both caught, so neither variant can land unnoticed now |
| FE-1 — dropdown offers what the backend refuses | **PARTIAL** (D2) |
| FE-2 — SSE error strands the Analyze button | **CLOSED**, tested (F5) |
| FE-3 — stale table next to a dead download | **CLOSED for the table, OPEN for the UniProt bar** (D1) |
| FE-4 — `job_id` still read from the DOM at render time | **UNCHANGED**, still fails safe; `chain \|\| chain-select.value` fallback still dead |
| FE-6 — Windows-only `unlink` PermissionError | **STILL OPEN**, reproduced below |

### R2 D3 — rollback still 500s. Reproduced.

`git show HEAD:scout/flags.py`'s `_CSV_COLUMNS_BASE` has no `chain_id`; the
worktree's does. An old worker reading a new `results.csv` builds row dicts that
carry the key and writes them with the old fieldnames, at `routes.py:810` and
`:822` — **outside** `analyze()`'s `try/except` (last `except` at `:674`):

```
  HEAD's _CSV_COLUMNS_BASE has chain_id: False
  worktree's has chain_id            : True
  OLD writer + NEW row  -> ValueError: dict contains fields not in fieldnames: 'chain_id'
  NEW writer + OLD row  -> ok, chain_id cell is ''
```

Forward is clean; backward is a 500 for up to `cleanup_old_jobs`'s one hour.
Self-healing (an old worker's `/scout/progress` rewrites the file in the old
format, and the UI always calls `/progress` first), but nothing tests it and
nothing in the change mentions deploy ordering. Note that fix 3 does **not**
widen this: `feasibility_results.csv` has exactly one reader
(`routes.py:1282`, straight to `send_file`) and every parse of it is a
`DictReader`, which ignores an unknown column in both directions.

### R1 D6 / R2 D4 — the feasibility CSV can now name its chain, but is still stale. Reproduced.

```
  feasibility A -> 200
  download after A: 200  '1,A,"10,11,...",7,0.5,Moderate,...'
  feasibility B -> 404 'No Epitope Scout results found for chain B on this job...'
  download after the refused B: 200
      header : 'epitope_id,chain_id,residues,residue_count,composite_feasibility,tier,'
      row    : '1,A,"10,11,...",7,0.5,Moderate,...'
```

So the delivered CSV now *says* `A`, which is the whole point of the stamp and a
real improvement. But `/scout/feasibility/download/<job_id>` still takes no
chain and still answers 200 with the previous chain's numbers to a URL the user
already has in history. Labelled, not closed — worth saying so out loud rather
than treating R1 D6 as done.

### FE-6 — the unguarded `unlink` still raises. Reproduced (Windows only).

```
  analyze B with the top-3 file held open
  -> RAISED OUT OF THE APP: PermissionError: [WinError 32] The process cannot access
     the file because it is being used by another process: 'tmp\...\epitopes_annotated.csv'
```

`routes.py:818` and `:839` sit outside the route's `try/except`. Legal on Linux
(prod and CI), so not a production defect — but it makes the suite
order-dependent on a Windows dev box. One `try/except OSError` closes it.

---

# NOT A DEFECT — checked, and what I ran

**The 409 does not misfire in a legitimate single-user flow.** Executed two
ways (Part A). The gate at `:642` and the re-read at `:695` cannot disagree
without a second writer: `run_pipeline` writes `chain_id` verbatim from the same
string the route validated (`pipeline.py:241 → :491`, no normalisation
anywhere), and its `patches`/`surface_residues` guards mean it cannot emit a
header-only file.

**No chain value reaches a filesystem path.** `../../etc/passwd` analysed end to
end through an mmCIF whose real chain id is that string:

```
  job dir now: ['.owner', 'analyze_cache.json', 'epitopes.csv',
                'epitopes_annotated.csv', 'input.cif', 'results.csv',
                'results_annotated.csv']
```

`chain_id` is used only for dict lookups (`model[chain_id]`) and map keys in
`pipeline.py`; `url_for` is never given a chain (`grep`: every call passes
`job_id` only); no `job_dir / <chain>` construction exists.

**No HTML sink for the request chain.** Driving the real `renderEpitopeTable`
with hostile ids:

```
"\"><b>x"                  -> href="/scout/feasibility?...&chain=%2522%253E%253Cb%253Ex"
"\"><img src=x onerror=1>" -> href="...chain=%2522%253E%253Cimg%2520src%253Dx%2520onerror%253D1%253E"
"=cmd|calc!A1"             -> href="...chain=%253Dcmd%257Ccalc!A1"
"../../etc/passwd"         -> href="...chain=..%252F..%252Fetc%252Fpasswd"
```

`encodeURIComponent` encodes `"`, `<`, `>` and `&`, so the attribute cannot be
broken out of in either the authenticated or the `login?next=` branch. Both SSE
error sinks are `textContent` (`index.html:846`, `feasibility.html:344`,
`:350`), including the newly-interpolated raw `epitope_id` in fix 4's message.
The one `innerHTML` that takes a chain id — `renderLegend`'s
`'<span class="legend-label">Chain ' + ci.id` (`index.html:600`) — is fed from
the 3Dmol parse of the user's own uploaded file, not from the request, and is
unchanged by this diff.

**All rendered inline JS parses.** Extracted from the Flask-rendered pages
(`src=` and `ld+json` blocks skipped), `node --check`:

```
  OK feasibility_3.js   OK index_3.js   OK index_4.js
```

**`var epitopes` is declared before every use** — `:437`, ahead of `:441`,
`:461` and `:486`.

**The "all patches" link still works when the top-3 link is hidden** — DOM run:
`download-link-full display = "inline-flex"`, href `/scout/download/JOB-1?full=1`.
`download_url_full` is unconditional in the response (`routes.py:872`).

**Clearing known-binders unconditionally does not break the populated case** —
`renderKnownBinders` already does `tbody.innerHTML = ''` (`:798`) and
`section.hidden = false` (`:834`); the DOM run shows `hidden = false` after
chain A and `true` after chain B.

**`csv.DictWriter` raises on no current path**, and `feasibility_results.csv`
has no compatibility hazard in either direction (one `send_file` reader, all
parses are `DictReader`).

**The ownership model is sound as far as it goes** — strict-UUID job ids,
`safe_join` confinement, `.owner` match on every read route; a second session
gets 404.

**`_get_binder_overlaps`'s chain gate cannot over-reject** — `analyze_cache.json`
has one writer (`routes.py:855-857`) that always stamps `"chain"`, and the 409
path returns before that write, so a lost race leaves the winner's cache intact.

**The 409 burns no quota** — `record_scout_run` is at `:860`, after the return;
`requires_scout_quota` only checks.

---

# Full suite

From the worktree root, no path argument, not piped through `tail`:

```
$ venv/Scripts/python.exe -m pytest -q -rf
5317 passed, 20 skipped, 854 warnings in 661.92s (0:11:01)
```

# Restoration proof

```
$ git diff --stat
 scout/flags.py                       |   1 +
 scout/pipeline.py                    |  12 ++
 scout/routes.py                      | 220 ++++++++++++++++++++++++++---------
 templates/scout/index.html           |  52 +++++++--
 tests/test_scout_anonymous_access.py |  11 +-
 5 files changed, 230 insertions(+), 66 deletions(-)

$ git hash-object <the five tracked files, then the new test file>
c2b68605ff8753b012485a2faa6eabddb2befbfc   scout/flags.py
52421e6116a11d033623992f2ed75e5a885aa2f6   scout/pipeline.py
3f9ae5823d5c3b74fffb124ff39410922a4eea70   scout/routes.py
791360c493ff44d36785cd4f806c50ef4c294d97   templates/scout/index.html
efdbd0ef9c26fa628d6e9a4faf1ee9a7152b5737   tests/test_scout_anonymous_access.py
8b54f30c5e115b9a75583908f936b4ede97feb88   tests/test_scout_chain_scoped_results.py
$ git rev-parse HEAD
7fd180df35086cfc5da3710ff336024901d8e73b
```

md5 of all six touched files re-checked against the pre-review snapshot after
every single mutation and again at the end:

```
$ python mut.py verify
DIFFERS: nothing - all 6 files byte-identical to snapshot
```

The two untracked files (`tests/test_scout_chain_scoped_results.py`, and the
round-1/round-2 reports) are intact. The only file I added to the repo is this
one; every probe, mutation marker and extracted JS block lives in the session
scratchpad, and every job dir a probe created was reaped by the probe.

# Things I could not verify

* **The real biophysics path.** `freesasa` is absent locally, so every
  route-level result uses a stubbed scorer writing the real CSV format through
  the real column list. `chain_id` is a plain dict key on a row the writer
  already builds, so the risk is low, but nothing here covers real scoring —
  and `M-pipe` is caught only by the CI-gated `test_analyze_runs_the_real_pipeline`.
* **D1 and D6 in a real browser.** Both were driven against the real page JS in
  a DOM stub, not Chrome. The DOM operations involved (`hidden`, `innerHTML`,
  `style.display`) are the ones the stub models exactly, so I rate the risk of a
  stub artefact as low, but it is not a browser.
* **How often a real upload carries a >16-character chain id (D2).** I proved
  the parser emits it and the dropdown offers it; I did not survey a corpus.
