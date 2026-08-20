# QC round 2 (backend) — chain-scoped Scout results

Independent adversarial review. I did not write the change. Round 1's question
was "what did the fix miss?"; mine is the opposite — **what does the fix now
break, or over-reject, that used to work?**

Every finding below was reproduced by execution against a running Flask app.
Nothing here is reasoned-only unless it says so.

---

## The tree moved four times under me. Read this first.

`scout/routes.py`, by blob, in the order I observed it:

| time | blob | state |
|---|---|---|
| session start | `a12628b` | the 5-file change I was briefed on |
| ~11:30 | `bbc5d47` | **all four production files reverted to HEAD**; only the test edit and the two untracked files survived |
| ~11:45 | `19433f8` | change back, but `_valid_chain` gone from `/scout/feasibility/analyze` |
| ~12:0x | `a12628b` | briefed version again |
| ~12:0x + seconds | `6b0c1df` | `_results_csv_for_chain` rewritten |
| final observation | `e5368d7` | `_results_csv_for_chain` rewritten again |

`HEAD` never moved (`7fd180d`). Nothing was stashed and the reflog is empty, so
these were working-tree operations by something outside this session.

Because the code vanished mid-review I rebuilt it from the `git diff` I captured
on my first tool call, into `scratchpad/fixed/`. That reconstruction is
**byte-identical to the briefed version**: git hashes it as blob `a12628b`,
exactly what the original diff header named, and the author's own suite passes
on it 21/21. Every result below was produced against `fixed/` (the briefed
state) and, where it matters, re-run against `base/` (pristine `7fd180d`) and
against snapshots of the live tree.

**D1–D5 are findings against the briefed change (`a12628b`). D6 is about the
live edits, and one of them reverts part of the fix.** If you are reading this
after the churn settles, check `git hash-object scout/routes.py` before trusting
the line numbers.

Environment: repo venv (`venv/Scripts/python.exe`, CPython 3.13.0), `freesasa`
absent, Windows. `scout.pipeline` imports fine; only `compute_rsa` needs
freesasa, so every route-level result below comes from a stubbed `run_pipeline`
writing a real-format CSV through the real `_CSV_COLUMNS_BASE`.

---

## Verdict

The fix is directionally right and closes the reported bug — I re-proved that
against pristine `HEAD` on three separate paths. But **every one of its refusals
is a new failure mode, and three of them are worse than "refuse"**: one destroys
a completed run's download files and serves an empty CSV as a success (D1), one
hard-400s structures the app itself offered a chain for (D2), and one 500s
during the rolling deploy that ships it (D3).

None of D1–D5 is a blocker on its own. **D1 and D3 should not ship
unaddressed.** D6 is a blocker only if either live variant of
`_results_csv_for_chain` lands — check the blob before merging.

Nothing here contradicts round 1. D4 is round 1's D6 (`feasibility_results.csv`)
promoted from INFO with execution behind it; the rest is new ground, because
round 1 asked what the fix missed and this round asked what it broke.

---

# CONFIRMED DEFECTS

## D1 (MEDIUM–HIGH) — an overlapping run on another chain deletes the finished run's downloads and serves an empty CSV as HTTP 200

**Where.** `scout/routes.py:690` (`csv_path = _results_csv_for_chain(job_dir, chain_id)`),
consumed by `:801` and `:822` (`unlink(missing_ok=True)`) and `:804-810`
(unconditional `results_annotated.csv` rewrite).

**Mechanism.** `analyze()` checks the cache at `:637`, then does the slow work —
`resolve_uniprot_id` (a network call), `fetch_known_binders`, `detect_interfaces`
— and only then re-reads `results.csv` at `:690`. If another request for a
different chain overwrote the file in that gap, the new helper correctly says
"not my chain" and returns `None`. But the code below cannot tell *"this chain
genuinely scored nothing"* from *"the file I was told to read is no longer
mine"*, and takes the first branch: `all_epitopes` stays empty, `top3` stays
empty, and the `else:` arms added by round 1's D1 fix **delete both top-3 files**
while `results_annotated.csv` is rewritten with zero rows.

**Evidence — executed.** A whole second `/analyze` for chain B issued from
inside chain A's request, at the exact point a second worker's `run_pipeline`
would land. Same script, both trees:

```
########## FIXED (under review) ##########
chain B (inner, completed first) -> (200, [[60, 61, 62, 63, 64, 65, 66]])
chain A (outer, finished last)   -> 200 []

--- what the user's chain-B tab can still download ---
  Top 3 CSV        status=404 data_rows=- first='{"error":"Results not found. Please run analysis first."}'
  All patches CSV  status=200 data_rows=0 first='epitope_id,chain_id,residues,residue_count,mean_rsa,composite_score,hy'
  analyze_cache.json chain = A epitopes = 0

########## BASE (pre-fix) ##########
chain B (inner, completed first) -> (200, [[10, 11, 12, 13, 14, 15, 16]])
chain A (outer, finished last)   -> 200 [[10, 11, 12, 13, 14, 15, 16]]

--- what the user's chain-B tab can still download ---
  Top 3 CSV        status=200 data_rows=1 first='1,"ALA10,ALA11,...",7,0.55,0.72,...'
  All patches CSV  status=200 data_rows=1 first='1,"ALA10,ALA11,...",7,0.55,0.72,...'
  analyze_cache.json chain = A epitopes = 1
```

Note what the base run shows in passing: chain B being served chain A's residues
is the original bug, and the fix does close it. What the fix adds is the row
underneath. Chain B's request **succeeded** and its epitopes are on screen; the
late-finishing chain-A request then:

- deleted `epitopes.csv` and `epitopes_annotated.csv` → the "Top 3 CSV" button
  now returns a JSON 404 body which the browser saves as `top3_epitopes.csv`;
- truncated `results_annotated.csv` to header-only → the "All patches CSV"
  button returns **HTTP 200, `text/csv`, zero data rows**;
- overwrote `analyze_cache.json` with `chain=A, epitopes=[]` → chain B's
  known-binder overlaps on the feasibility page now come back `[]`;
- returned `200 {"epitopes": []}` for chain A, a chain that *was* scored
  successfully — and still called `record_scout_run`, burning a quota unit for
  an empty answer.

An empty `all_patches.csv` served as a success reads as "this target has no
surface patches", which is a wrong scientific conclusion, not an error. That is
the severity band the whole change exists to eliminate.

A narrower repro (no nested request — just the file swapped at the same point)
is in `p3_derived_files.py` part B and shows the same disk state.

**Reachability — partly unverified.** I reproduced this at the API level, not
through a browser. Two overlapping requests need the same `job_id` and the same
session. The shipped UI makes that hard: `runAnalysis()` disables the Analyze
button (`templates/scout/index.html:930-931`) and `job_id` is not shareable
across tabs. But `/scout/analyze` is an unauthenticated JSON API, `WEB_CONCURRENCY`
defaults to 2 so two sync workers really do run in parallel, and
`ANON_MAX_CONCURRENT_RUNS = 4` explicitly permits it.

**Smallest fix.** Do not treat "the file is not mine any more" as "no epitopes".
At `:690`, `csv_path is None` after the gate at `:637` already ran or hit for
this chain is a lost race, not a result — return 409/503 and touch nothing:

```python
csv_path = _results_csv_for_chain(job_dir, chain_id)
if csv_path is None:
    return jsonify({"error": _BUSY_MESSAGE}), 503
```

That also removes the empty-`results_annotated.csv` write and both unlinks from
the race path, and costs one branch.

---

## D2 (MEDIUM) — `_valid_chain` hard-400s chain ids the upload endpoint itself puts in the dropdown

**Where.** `scout/routes.py:309` (`_CHAIN_ID_RE = re.compile(r"\A[A-Za-z0-9]{1,8}\Z")`),
`:312-313`, applied at `:621`, `:919`, `:1038` (briefed state) and `:1137`.

**The premise is false.** The comment above the regex says "Chain identifiers
are alphanumeric in both formats Scout accepts". PDB column 22 is *one
character*, any character. mmCIF `auth_asym_id` is an unconstrained code —
`.` is what a converter writes when the author chain is not specified.
`parse_pdb` (`scout/parser.py:290`) passes `chain.get_id()` through verbatim and
`/scout/upload` lists whatever comes back.

**Evidence — executed.** Upload → read the dropdown → POST `/scout/analyze` with
the chain the dropdown offered. Identical script, both trees:

```
########## BASE (HEAD, pre-fix) ##########
kind chain-in-file  | dropdown offers | POST /analyze status
pdb  '_'          | ['_']           | 200   ran_pipeline=1
pdb  '-'          | ['-']           | 200   ran_pipeline=1
pdb  'a'          | ['a']           | 200   ran_pipeline=1
cif  '.'          | ['.']           | 200   ran_pipeline=1
cif  'A-2'        | ['A-2']         | 200   ran_pipeline=1
cif  'PROTEIN_1'  | ['PROTEIN_1']   | 200   ran_pipeline=1
cif  'AAA'        | ['AAA']         | 200   ran_pipeline=1

########## FIXED (under review) ##########
pdb  '_'          | ['_']           | 400   ran_pipeline=0  job_id and a valid chain id are required.
pdb  '-'          | ['-']           | 400   ran_pipeline=0  job_id and a valid chain id are required.
pdb  'a'          | ['a']           | 200   ran_pipeline=1
cif  '.'          | ['.']           | 400   ran_pipeline=0  job_id and a valid chain id are required.
cif  'A-2'        | ['A-2']         | 400   ran_pipeline=0  job_id and a valid chain id are required.
cif  'PROTEIN_1'  | ['PROTEIN_1']   | 400   ran_pipeline=0  job_id and a valid chain id are required.
cif  'AAA'        | ['AAA']         | 200   ran_pipeline=1
```

Five of seven structures that scored fine an hour ago are now unanalysable, and
the only chain on offer is the one being refused.

**What the user sees.** Legible, but wrong and unrecoverable. `runAnalysis()`
opens the SSE first, so `/scout/progress` is what answers:

```
dropdown offers: ['.']
SSE -> data: {"stage": "error", "msg": "job_id and a valid chain id are required."}
```

rendered through `showAnalyzeError` → `el.textContent` (`index.html:819-823`),
so no XSS — but it blames the user for a chain the app itself offered, names no
recovery, and the error branch in `openProgressStream` (`index.html:340-345`)
calls `resetProgress()`, which does **not** re-enable the Analyze button. The
page is stuck on "Analyzing…" until reload. (That last part is pre-existing —
any SSE error does it — but this guard is a new way to reach it.)

**The codebase already has the right pattern and this departs from it.**
`shared/pdb_inspect.validate_target_chain` — the validator every other tool in
this repo uses — checks the chain against *the chains actually present in the
uploaded file*, not against a charset, and its docstring is explicit that it is
case-sensitive because guessing is worse. `run_pipeline` (`scout/pipeline.py:307`)
already does exactly that for Scout. `shared/pdb_inspect.py:273` even names
"multi-character chain IDs" as a real thing.

**Not a regression, but worth knowing:** a PDB file with a blank chain id
(column 22 = space) is offered by the dropdown as `' '` and 400s — before *and*
after the change, because the routes `.strip()` it to `""`. Pre-existing dead
end, unchanged.

**Smaller fix that keeps the protection.** The threat is a formula character in
a downloaded cell; the boundary that matters is the download, not the request.
Either keep validating the chain against the parsed structure (already free —
`run_pipeline` raises `ValueError` → 422 with a message that names the available
chains) and escape at the writer, or, if a charset guard is wanted, at minimum
widen it to what the parsers can emit and make `/scout/upload` refuse to *offer*
a chain the analyze route will refuse — offering it and then rejecting it is the
part that has no defence.

---

## D3 (MEDIUM) — a rolling deploy of this change 500s: old code + new `results.csv` raises out of the route

**Where.** `scout/routes.py:799` in the pre-change numbering
(`writer.writerow(row)` for `epitopes_annotated.csv`), which sits **outside**
`analyze()`'s `try/except` (the last `except Exception` is at `:669`).

**Evidence — executed.** Same script, both trees: a job dir holding a
`results.csv` written by *the other* version, then `POST /scout/analyze`.

```
########## FIXED (new code, pre-deploy CSV) ##########
this tree's _CSV_COLUMNS_BASE has chain_id: True
results.csv written by the OTHER version -> no chain_id
POST /scout/analyze chain A -> 200
  pipeline re-ran: ['A']
  epitopes: 1

########## BASE (rolled-back code, post-deploy CSV) ##########
this tree's _CSV_COLUMNS_BASE has chain_id: False
results.csv written by the OTHER version -> has chain_id
POST /scout/analyze RAISED OUT OF THE APP: ValueError dict contains fields not in fieldnames: 'chain_id'
```

The **forward** direction is clean — round 1's claim holds, an unstamped legacy
CSV is a cache miss and gets rescored. The **backward** direction is an uncaught
`ValueError` → HTTP 500.

**Window.** Not only rollback. During any rolling restart the SSE
(`/scout/progress`, which always runs the pipeline) and the follow-up POST
(`/scout/analyze`) can land on different workers. New worker writes the stamped
CSV, old worker reads it → 500. The reverse pairing (old writes, new reads) is
harmless: a cache miss and a rescore. Because job dirs live for
`cleanup_old_jobs(max_age_seconds=3600)`, a rollback keeps the hazard alive for
up to an hour.

**Self-healing, so it is transient not permanent:** an old worker serving
`/scout/progress` rewrites `results.csv` in the old format, and the UI always
calls `/progress` before `/analyze`. Nothing in the change mentions the deploy
ordering, though, and nothing tests it.

---

## D4 (LOW–MEDIUM) — the new 404 gate makes the stale `feasibility_results.csv` *more* likely, and it is the one job-dir file that still cannot name its chain

**Where.** `scout/routes.py:1052-1054` (the new `_results_csv_for_chain` gate)
vs `scout/pipeline.py:531-548` (`FEASIBILITY_CSV_COLUMNS`, no chain column) and
`scout/routes.py:1250-1265` (`/scout/feasibility/download/<job_id>`, takes no
chain).

**Evidence — executed.** Chain A feasibility, then the chain-B request:

```
########## FIXED ##########
feasibility A -> 200 residues: A10
  on disk: 1,"A10",7,0.5,Moderate
feasibility B -> 404 No Epitope Scout results found for chain B on this job. ...
  on disk STILL: 1,"A10",7,0.5,Moderate
  GET /scout/feasibility/download -> 200 filename=feasibility_e31d023d.csv
  body: 1,"A10",7,0.5,Moderate

########## BASE (pre-fix) ##########
feasibility A -> 200 residues: A10
feasibility B -> 200
  on disk STILL: 1,"B10",7,0.5,Moderate     <- overwritten
```

Pre-fix the chain-B request overwrote the file (wrongly, but freshly). Post-fix
it bails out at the new gate and chain A's numbers stay on disk, still served at
HTTP 200 by a download URL the user already has in history from the chain-A run.
The fix widened the stale window rather than closing it. Round 1 logged this as
D6/INFO; this is the executed proof that the change makes it worse.

**Full job-dir inventory, executed** (`p7_case_and_files.py`):

```
after upload                          ['.owner', 'input.pdb']
after POST /scout/analyze A           [... 'analyze_cache.json', 'epitopes.csv',
                                        'epitopes_annotated.csv', 'results.csv',
                                        'results_annotated.csv']
after POST /feasibility/analyze A     [... + 'feasibility_results.csv']
after POST /scout/analyze B (no top3) ['.owner', 'analyze_cache.json',
                                        'feasibility_results.csv', 'input.pdb',
                                        'results.csv', 'results_annotated.csv']
feasibility_results.csv still holds chain A's run: True
```

| file | chain-identifiable? | rewritten every analyze? |
|---|---|---|
| `.owner`, `input.pdb`/`.cif` | n/a | n/a |
| `results.csv` | yes (new column) | on cache miss |
| `results_annotated.csv` | yes (carries the column) | yes, unconditional |
| `epitopes.csv` | yes | yes — written **or deleted** |
| `epitopes_annotated.csv` | yes | yes — written **or deleted** |
| `analyze_cache.json` | yes (`"chain"`) | yes, unconditional |
| **`feasibility_results.csv`** | **no** | **no — left on disk on any failure** |

`run_feasibility_pipeline` writes it in one shot at the end
(`scout/pipeline.py:759-762`), so a raise leaves the previous chain's file fully
intact. A `chain_id` column costs one line and closes the last one.

---

## D5 (LOW) — the new SSE message asserts something false whenever the epitope_id, not the chain, is what is missing

**Where.** `scout/routes.py:1172-1177`. The branch is
`if epitope_id and not epitope_str`, which cannot distinguish a chain mismatch
from an `epitope_id` that simply is not in this chain's CSV, or one that is not
an integer.

**Evidence — executed**, after analysing chain A:

```
########## FIXED ##########
  chain A, epitope_id=1  (exists)                     -> None  (stream proceeds)
  chain A, epitope_id=99 (chain matches, id does not) -> No Epitope Scout results found for chain A on this job. Run epitope analysis on that chain first.
  chain B, epitope_id=1  (chain really is unscored)   -> No Epitope Scout results found for chain B on this job. Run epitope analysis on that chain first.
  chain A, epitope_id=abc (not an int)                -> No Epitope Scout results found for chain A on this job. Run epitope analysis on that chain first.
  no epitope at all                                   -> No epitope residues specified.

########## BASE (pre-fix) ##########
  chain A, epitope_id=99                              -> No epitope residues specified.
  chain B, epitope_id=1                               -> None   <- the original bug: it resolved against chain A
  chain A, epitope_id=abc                             -> No epitope residues specified.
```

Row 2 is the defect: chain A's results are on disk and the page tells the user
they are not, and to re-run an analysis that is already done. Both UI paths open
this stream before the JSON route, so this is the text the user actually reads —
which was the whole point of round 1's D3 fix. Pre-fix the message was vague;
now it is specific and wrong.

Reachable without any adversary: a bookmarked or back-buttoned feasibility link
carrying `epitope_id=3` after a re-run that produced two patches.

---

## D6 (HIGH if it lands; transient when observed) — two live edits to `_results_csv_for_chain` reverted the fix, and neither moved a single test

**Status.** Observed in the working tree at blobs `6b0c1df` and `e5368d7`. By my
last check `scout/routes.py` was back to `a12628b` and neither edit is present.
Recorded in full because both reintroduce the bug the change exists to fix, the
suite is green on both, and whoever is iterating on this function is one commit
away from shipping either.

**Where.** `scout/routes.py:342` as of blob `e5368d7`:

```python
    # briefed (a12628b):
    if first_row is None or first_row.get("chain_id") != chain_id:
        return None
    # live (e5368d7):
    if first_row is not None and first_row.get("chain_id") != chain_id:
        return None
```

`first_row is None` — a zero-byte or header-only `results.csv` — now
short-circuits the `and` and **returns the path**: a cache HIT for any chain.
An intermediate edit I caught seconds earlier (blob `6b0c1df`) went further,
`first_row.get("chain_id", chain_id) != chain_id`, which defaults the missing
column to the requested chain and makes a *legacy, column-less* CSV a HIT too —
i.e. serves chain A's scores for a chain B request, the exact bug this whole
change exists to fix. That one appears to have been replaced within seconds;
`e5368d7` is what I measured.

The docstring three lines above is untouched and now certifies false:

> A CSV with no `chain_id` column (written before this stamp existed) and one
> with **no data rows are both misses**: neither can name its chain, and
> rescoring is the only answer that cannot be silently wrong.

**Evidence — executed.** A truncated write (header written, rows not), then
three `/analyze` calls:

```
### BRIEFED a12628b ###
  analyze A -> 200 epitopes=[... residue_numbers [10..16]]  pipeline_runs=['A']
  analyze A -> 200 epitopes=[... residue_numbers [10..16]]  pipeline_runs=['A']
  analyze B -> 200 epitopes=[... residue_numbers [60..66]]  pipeline_runs=['A', 'B']
  results.csv on disk still header-only: False

### LIVE e5368d7 ###
  analyze A -> 200 epitopes=[]  pipeline_runs=[]
  analyze A -> 200 epitopes=[]  pipeline_runs=[]
  analyze B -> 200 epitopes=[]  pipeline_runs=[]
  results.csv on disk still header-only: True

### BASE (pre-fix) ###
  analyze A -> 200 epitopes=[]  pipeline_runs=[]
  analyze A -> 200 epitopes=[]  pipeline_runs=[]
  analyze B -> 200 epitopes=[]  pipeline_runs=[]
  results.csv on disk still header-only: True
```

The live version is byte-for-byte the pre-fix behaviour: HTTP 200, zero
epitopes, the pipeline never runs, for every chain, permanently — until
`cleanup_old_jobs` reaps the dir an hour later. Round 1 assessed exactly this
case and concluded "in the truncated-write case the pre-fix behaviour was
*worse* — it treated the empty file as a hit and returned zero epitopes
forever." That regression is now back in.

The helper-edge sweep shows the same thing directly:

```
                     briefed a12628b   live e5368d7
zero bytes                miss             HIT
header only               miss             HIT
legacy header (no col)    miss             miss
```

**Reachability.** `run_pipeline` cannot emit a header-only file — it raises at
`pipeline.py:346-349` / `:361-364` before the writer. It takes a worker killed
between `writeheader()` and `writerows()` (`pipeline.py:513-516`) or a full
disk. Low probability, silent, and unrecoverable for the life of the job dir.

**No test catches it:** the chain-scoped suite is still 21/21 green on
`e5368d7`. `test_a_pre_fix_results_csv_is_a_miss_not_a_wrong_answer` covers the
missing-*column* case, not the missing-*rows* case.

---

## D7 (LOW, transient) — an earlier live edit dropped `_valid_chain` from `/scout/feasibility/analyze` while the error string kept claiming it

Observed at blob `19433f8` and reverted by the time I finished; recorded because
the coverage hole it exposed is permanent.

**Where.** `scout/routes.py:1038` as it stood in `19433f8`:

```python
    if not job_id or not chain_id:
        return jsonify({"error": "job_id and a valid chain id are required."}), 400
```

The briefed version (`a12628b`) had `_valid_chain(chain_id)` there. This is the
only difference between the two blobs.

**Evidence — executed**, same script against both:

```
##### BRIEFED STATE (blob a12628b) #####
POST /scout/analyze                 -> 400 job_id and a valid chain id are required.
GET  /scout/progress                -> job_id and a valid chain id are required.
POST /scout/feasibility/analyze     -> 400 job_id and a valid chain id are required.
GET  /scout/feasibility/progress    -> job_id and a valid chain id are required.

##### LIVE AT THE TIME (blob 19433f8) #####
POST /scout/analyze                 -> 400 job_id and a valid chain id are required.
GET  /scout/progress                -> job_id and a valid chain id are required.
POST /scout/feasibility/analyze     -> 200 (accepted)
     feasibility pipeline called with chain: ['=cmd|calc!A1']
     response 'chain' field echoed back: '=cmd|calc!A1'
GET  /scout/feasibility/progress    -> job_id and a valid chain id are required.
```

Three consequences:

1. The error string at that route now advertises validation the route no longer
   does.
2. The sibling SSE route still rejects what the POST accepts. The UI opens the
   SSE first, so the laxity is unreachable through the browser and the workflow
   still dead-ends — the inconsistency buys nothing.
3. **No test catches it.** `TestChainIdIsValidatedAtTheBoundary` covers
   `/scout/analyze` and `/scout/progress` only, so the author's suite is still
   21/21 green with the guard gone. A test class named "at the boundary" that
   covers half the boundaries is the guards-that-certify-false pattern.

Not a security hole: `FEASIBILITY_CSV_COLUMNS` has no chain column so the value
never reaches a downloadable cell, and `run_feasibility_pipeline`
(`scout/pipeline.py:602-605`) raises `ValueError` on an unknown chain → clean
422. If the removal was a deliberate response to D2, it is the right instinct
applied to one of four boundaries.

---

# NOT A DEFECT — checked, and what I ran to check it

**Chain-id normalisation: there is none, anywhere.** The brief's worst case
(a permanent cache miss, or a permanent feasibility 404) does not exist.
Traced end to end and executed on a file containing both `a` and `A`:

```
dropdown offers: ['a', 'A']
 analyze 'a' -> 200  pipeline_runs_so_far=['a']
 analyze 'a' -> 200  pipeline_runs_so_far=['a']            <- cache hit
 analyze 'A' -> 200  pipeline_runs_so_far=['a', 'A']       <- correct miss
 analyze 'A' -> 200  pipeline_runs_so_far=['a', 'A']       <- cache hit
 analyze 'a' -> 200  pipeline_runs_so_far=['a', 'A', 'a']  <- correct miss
results.csv chain_id cell after last run: 'a'
```

`parse_pdb` passes `chain.get_id()` through untouched (`parser.py:290`); the
routes only `.strip()`; `run_pipeline` compares exactly (`pipeline.py:307`) and
writes the same string (`pipeline.py:491`); the helper compares exactly. The one
`.upper()` in `parser.py` (`:295`, `:119-121`) is for the chain *name* lookup and
never touches the id.

**`_results_csv_for_chain` raises nothing I could throw at it.** Sixteen
degenerate files, called directly:

```
missing file                               miss
zero bytes                                 miss
header only                                miss
header + one row                           HIT
blank first line then header+row           miss
header + blank line + row                  HIT
legacy header (no chain_id)                miss
truncated mid-first-row (writer killed)    HIT
truncated before chain_id                  miss
NUL byte in the data                       miss
invalid utf-8 byte                         miss
one 200 KB field (> csv field limit)       miss
no trailing newline anywhere               HIT
CRLF everywhere                            HIT
utf-8 BOM before header                    HIT
results.csv is a DIRECTORY                 miss
```

Nothing raised. Every failure mode is a miss (safe), never a wrong hit. The
`(OSError, csv_module.Error, UnicodeDecodeError)` clause is adequate — I could
not construct an input that escapes it. `csv_path.exists()` sits outside the
`try`, but `Path.exists()` swallows `OSError` itself.

**Reading only the FIRST data row is justified.** `run_pipeline` writes every
row for one chain in a single `writer.writerows(rows)` (`pipeline.py:513-516`),
so a file mixing two chains is not producible. And the one case where the first
row lies — a write truncated after row 1 — was *equally* a cache hit before the
fix, when mere existence was the gate. The interleaving is no worse there.

**No positional CSV readers exist, so inserting `chain_id` at index 1 breaks
nothing.** `grep -rn "csv.reader\|csv_module.reader"` across `scout/`,
`blueprints/`, `shared/`, `scripts/`, `tests/` returns **zero hits** — every read
in the repo is a `DictReader`. No hardcoded header string or column count in any
test, template, doc or script; the only two literal CSV headers in tests are
fixtures writing their own minimal files.

**`results_annotated.csv` has exactly one reader** (`routes.py:887`, handed
straight to `send_file`). It is never parsed, so the extra column is inert.

**Forward legacy compatibility is clean.** New code + a pre-`chain_id`
`results.csv` = cache miss → rescore → 200, executed above under D3. Round 1's
"bounded transient" claim holds in this direction.

**`_get_binder_overlaps`'s new chain check cannot over-reject.**
`analyze_cache.json` has exactly one writer (`routes.py:838-840`) and it has
*always* stamped `"chain"` — verified against `HEAD` (`git show
HEAD:scout/routes.py`, line 768). There is no path that writes it without the
key or under a different key. The only mismatch the check can produce is a
genuine cross-chain one. Separately, the single link to `/scout/feasibility`
(`index.html:671`) always carries `&chain=`, so the template's
`|| 'A'` fallback (`feasibility.html:258`) is not reachable through the UI.

**`unlink(missing_ok=True)` adds no crash surface in production.** On Linux
(prod per `Procfile`/`nixpacks.toml`, and CI per `.github/workflows/pytest.yml`)
unlinking a file a concurrent `send_file` holds open is legal, and `missing_ok`
covers a dir the reaper has taken. On Windows it raises:

```
platform: Windows
unlink while open      -> PermissionError: [WinError 32] ...
unlink while os.open'd -> PermissionError: [WinError 32] ...
the unlink lines are at : [801, 822]
analyze()'s except lines: [528, 661, 667, 669, 684, 964]
```

so on a Windows dev box that would be an uncaught 500. Dev-only; not a
production defect.

**Round 1's "`fieldnames` is unbound-by-luck" note is closed.** `fieldnames:
list[str] = []` at `:689` removes it. And `top3` non-empty while `fieldnames` is
empty is unreachable — both are populated inside the same
`if csv_path is not None` block.

**`/scout/download`'s non-full fallback can no longer serve a stale
`epitopes.csv`** — the new delete closes it, executed under D1. The `?full=1`
fallback still reads `results.csv` with no chain check (`routes.py:888`, round
1's footnote); the file now at least carries the column, and reaching it needs a
hand-built URL because `results_annotated.csv` is written unconditionally.

**Regression suite: no regressions anywhere in the repo.** Full run, no path
argument, not piped through `tail`, `venv/Scripts/python.exe -m pytest -q`:

```
# against the reconstructed briefed state (routes.py a12628b)
5294 passed, 20 skipped in 306.64s (0:05:06)

# against the live state as it stood at blob 19433f8 (D7)
5294 passed, 20 skipped in 196.47s (0:03:16)
```

Identical counts, which is itself the D7 evidence: removing a guard from a
production route moved nothing. The author's chain-scoped suite is 21/21 on
`a12628b`, on `19433f8` (D7) and on `e5368d7` (D6) alike.

---

# Final tree state when I stopped

```
$ git hash-object scout/routes.py scout/flags.py scout/pipeline.py templates/scout/index.html
a12628bf22eeef72a20e7cbdbe38bbd4d140a7b1     <- the briefed version
c2b68605ff8753b012485a2faa6eabddb2befbfc
e68e66ef00db72cb6ee22bc802d31b8ada336320
cbe938a12c5f32f68dc2e29effcf427b7790d824
$ git rev-parse HEAD
7fd180df35086cfc5da3710ff336024901d8e73b
```

So D1–D5 apply to the tree as it stands. D6 and D7 were transient. I modified
nothing in the repo except this file; all probes ran against copies under my own
temp dir, and the job dirs they created were reaped by the probes themselves.

---

# Things I could not verify

- **The real biophysics path.** `freesasa` is not installed in this venv, so
  `run_pipeline` cannot execute locally; every route-level result uses a stubbed
  scorer writing the real CSV format. The `chain_id` stamp is a plain dict key
  on a row the writer already builds, so the risk there is low, but no evidence
  here covers real scoring.
- **D1 through a browser.** Reproduced at the API level only. The UI's disabled
  Analyze button makes the two-tab route hard; I did not establish whether any
  real browser sequence produces it.
- **How often a real upload carries a non-alphanumeric chain id (D2).** I proved
  the parsers accept `_ - . ? A-2 PROTEIN_1` from hand-built files and that the
  dropdown offers them. I did not survey RCSB or a corpus of tool outputs to put
  a rate on it.
