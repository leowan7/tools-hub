# QC round 1 — chain-scoped Scout results

Independent adversarial review. I did not write the change.

## What I reviewed

Worktree `.claude/worktrees/suspicious-dewdney-13b07e`, branch
`claude/suspicious-dewdney-13b07e`.

```
$ git rev-parse HEAD
7fd180df35086cfc5da3710ff336024901d8e73b

$ git diff --stat
 scout/flags.py                       |  1 +
 scout/pipeline.py                    |  5 ++++
 scout/routes.py                      | 48 +++++++++++++++++++++++++++++-------
 tests/test_scout_anonymous_access.py | 11 +++++++--
 4 files changed, 54 insertions(+), 11 deletions(-)

$ git status --porcelain
 M scout/flags.py
 M scout/pipeline.py
 M scout/routes.py
 M tests/test_scout_anonymous_access.py
?? tests/test_scout_chain_scoped_results.py
```

**The diff grew under me mid-review.** That listing is what I captured on my
first tool call. Some minutes later a fifth file had appeared — nobody told me,
and `HEAD` never moved:

```
$ git diff --stat          # end of review
 scout/flags.py                       |  1 +
 scout/pipeline.py                    |  5 ++++
 scout/routes.py                      | 48 +++++++++++++++++++++++++++++-------
 templates/scout/index.html           | 11 ++++++---
 tests/test_scout_anonymous_access.py | 11 +++++++--
 5 files changed, 62 insertions(+), 14 deletions(-)
```

I got lucky: my first read of `templates/scout/index.html` happened *after* the
edit landed, so every front-end conclusion below is against the five-file state,
and I re-ran the Scout suites against it at the end (111 passed, 1 skipped). But
treat the review as covering the five-file working tree, not the four-file one I
was pointed at. See the assessment of that fifth file under "The late front-end
change" below.

All changes uncommitted, plus the untracked new test file. Nothing in the
worktree was modified by me except this report. Mutation testing was done on
copies under my own temp dir.

**Full suite, final state, no path argument, not piped through `tail`:**

```
$ ../../../venv/Scripts/python.exe -m pytest -q
5280 passed, 20 skipped, 854 warnings in 912.54s (0:15:12)
```

No regressions anywhere in the repo.

Environment: repo venv (`venv/Scripts/python.exe`), `freesasa` absent, `scipy`
1.18.0, Windows/cp1252.

---

## Verdict: **PASS WITH FIXES**

The fix is correct, the tests are real, and it closes the reported bug — I
proved all three by execution. But it is **not complete**: I found and
reproduced a second live cross-chain leak in the download path that the change
leaves open, and a third in the known-binder panel. Both are the same class of
defect as the one being fixed (silent, HTTP 200, wrong-but-plausible science),
and both are one- or two-line fixes.

Do not ship without D1. D2 should go in the same commit — it is two lines and
the stamp it needs is already being written to disk and ignored.

---

## Confirmed defects

### D1 (HIGH) — `/scout/download` still hands back the previous chain's top-3 CSV

**Where.** `scout/routes.py:756-757` (`if top3:` guards the rewrite of
`epitopes_annotated.csv`) and `scout/routes.py:776-777` (`if top3 and
fieldnames:` guards `epitopes.csv`), consumed with no chain check by
`scout/routes.py:849-861`.

`results_annotated.csv` (`scout/routes.py:767-774`) is rewritten
unconditionally and is correct. The two *top-3* files are not.

The same route also holds the one `results.csv` read in the codebase that does
**not** go through the new helper: `scout/routes.py:850`,
`fallback_path = job_dir / "results.csv"`, used when `results_annotated.csv` is
absent. That is only reachable when `/scout/progress` ran the pipeline but
`/scout/analyze` never completed, and the UI never emits that URL in that state
— so it is a footnote, not a second leak, and the file it serves now at least
carries the `chain_id` column. It does contradict the helper docstring's
"Every reader of the file goes through here".

**Scenario.** Analyse chain A, which yields a top-3. Then analyse chain B on
the same job, and have chain B yield *no* qualifying epitope — every patch
below `_MIN_COMPOSITE = 0.40`, or under `_MIN_RESI_COUNT = 5`, or over
`_MAX_PATCH_FRACTION = 0.30` of a short chain (`scout/routes.py:644-646`).
`/scout/analyze` correctly returns `{"epitopes": []}` for B. The stale
`epitopes_annotated.csv` from chain A is never overwritten.
`templates/scout/index.html:431-437` then shows the "Top 3 CSV" button
unconditionally — it is not gated on `epitopes.length` — pointed at
`/scout/download/<job_id>`, which serves chain A's rows.

**Evidence — executed.** Probe against the fixed source, chain A scored good,
chain B scored below threshold:

```
CHAIN B epitopes returned: []
TOP3 DOWNLOAD after analysing chain B:
epitope_id,chain_id,residues,...
1,A,"ALA10,ALA11,ALA12,ALA13,ALA14,ALA15,ALA16",7,0.55,0.72,...

FULL DOWNLOAD after analysing chain B:
epitope_id,chain_id,residues,...
1,B,"ALA60,ALA61,ALA62,ALA63,ALA64,ALA65,ALA66",7,0.55,0.10,...
```

The user is looking at "no epitopes found for chain B" and downloading chain
A's epitopes. The new `chain_id` column does make the stale file
self-identifying, which is a real mitigation — but a header column is not what
a user reads, and the UI is presenting the file as the current result.

**Fix.** Take the two writes out of the `if top3` guard. Simplest version that
holds: write header-only files when `top3` is empty, e.g. hoist
`writer.writeheader()` out and let the row loop write nothing. `fieldnames` at
`:777` needs a default (see the note below) if you do that.

---

### D2 (MEDIUM) — known-binder overlaps still cross chains

**Where.** `scout/routes.py:331-336`. `_get_binder_overlaps` reads
`analyze_cache.json` and never looks at its `chain` key — which
`scout/routes.py:794-802` writes and, verified by repo-wide grep, **nothing
reads**. The fix stamped a second file with the chain and left the first
stamp unused.

**Scenario.** Analyse chain A of a two-chain structure. Known binders are
resolved from *chain A's* UniProt id (`scout/routes.py:614-633`) and cached with
their `contact_residues` as bare integers. Go to the feasibility page, select
chain B, type residue numbers that exist in chain B (the free-text box at
`templates/scout/feasibility.html:73`). `feasibility_analyze` takes the
explicit-residues branch, so the new `_results_csv_for_chain` gate at
`scout/routes.py:1013-1016` is never reached, and `scout/routes.py:1082` calls
`_get_binder_overlaps(job_dir, epitope_residues)` with chain A's cache. Chains
routinely share numbering, so the intersection is non-empty and the "Known
binder overlaps" table (`templates/scout/feasibility.html:213-231`, filled at `:533-540`) claims a
published antibody binds the chain-B epitope when it binds chain A.

**Evidence — executed.** Chain A analysed with a stub binder whose contacts are
residues 10-16; feasibility then requested for **chain B** on residues 10-16
(which exist in chain B of the test structure):

```
feasibility(chain B) status 200
known_binder_overlaps: [
 { "pdb_id": "1ABC", "binder_type": "mAb", "overlap_count": 7,
   "overlap_residues": [10,11,12,13,14,15,16], "total_contacts": 7 }
]
```

**Fix.** Pass the chain in and compare against the stamp that is already there:

```python
def _get_binder_overlaps(job_dir, epitope_residues, chain_id):
    ...
    if cache.get("chain") != chain_id:
        return []
```

---

### D3 (LOW–MEDIUM) — the improved 404 message never reaches a user

**Where.** The new message at `scout/routes.py:1015-1016` is only produced by
`POST /scout/feasibility/analyze`. Both feasibility UI paths open the SSE
stream first — `templates/scout/feasibility.html:397-404` (manual) and
`:630-635` (auto-load from a Scout link) — and only `fetch` the POST after the
stream reports `done`. On a chain mismatch the stream fails first, at
`scout/routes.py:1128-1130`, with the generic text.

**Scenario, and it is a new dead end.** Analyse chain A; the epitope table
renders feasibility links carrying `chain=A`
(`templates/scout/index.html:671`). Analyse chain B on the same job — this
overwrites `results.csv`. Now open the chain-A link (back button, a tab opened
earlier, a bookmark, the `/login?next=` round trip). Before this change that
produced a *wrong* feasibility score. After it, the page shows:

> Could not auto-load: No epitope residues specified. Please upload the
> structure manually.

which names the wrong cause and gives advice that will not help (the structure
is fine; the user needs to re-run Scout on chain A).

**Evidence — executed.** Analysed A then B, then requested the chain-A link:

```
--- SSE the browser receives (auto-load path, chain A link) ---
data: {"stage": "error", "msg": "No epitope residues specified."}

--- POST /feasibility/analyze status 404 ---
{"error":"No Epitope Scout results found for chain A on this job. Run epitope analysis on that chain first."}
```

The good message exists and is unreachable from the UI.

**Fix.** One line: in `feasibility_progress`, when the `epitope_id` branch
misses because `_results_csv_for_chain` returned `None`, emit the same
chain-specific text instead of falling through to
`"No epitope residues specified."` at `scout/routes.py:1128-1130`.

---

### D4 (LOW) — `chain_id` is now attacker-controlled free text inside a downloadable CSV

**Where.** `scout/pipeline.py:491` stamps the raw request string;
`scout/routes.py:767-774` copies it into `results_annotated.csv`;
`scout/routes.py:849-861` serves that as an attachment named
`all_patches.csv`.

`run_pipeline` does validate that the chain exists (`scout/pipeline.py:306-310`),
so the string has to be a real chain id in the uploaded file — but an mmCIF
`auth_asym_id` is an arbitrary string, and the file is user-supplied.

**Evidence — executed.** A hand-built `.cif` whose `auth_asym_id` is
`=cmd|calc!A1`:

```
BIOPYTHON CHAIN IDS: ['=cmd|calc!A1']
parse_pdb error:
parse_pdb chains: [('=cmd|calc!A1', 10)]
```

so `/scout/upload` offers it as a selectable chain, and it lands verbatim in
the CSV (stubbed-pipeline probe, real writer):

```
epitope_id,chain_id,residues,...
1,"=HYPERLINK(""http://evil.example/x"",""ok"")","ALA10,...",5,...
```

Note this is genuinely *new* surface, not pre-existing: every other column in
this file is machine-generated — numbers, `STANDARD_AA` residue names, DSSP
letters, `0`/`1`. `chain_id` is the first free-text column in it.

**Blast radius is small.** Job dirs are owner-scoped (`_resolve_job_dir`,
`scout/routes.py:195-201`), so the person who downloads the poisoned CSV is the
person who uploaded the `.cif`. The realistic path is social — "run my
structure through Scout and send me the CSV" — not remote.

**Same root cause, second symptom.** On cp1252 (this dev box) a non-ASCII chain
id makes the CSV write raise:

```
preferred encoding: cp1252
WRITE RAISED: UnicodeEncodeError 'charmap' codec can't encode character 'α' ...
```

That is a 500 on Windows dev only; production is Linux/UTF-8 (`Procfile`,
`nixpacks.toml`) and unaffected.

**Fix.** One guard at the route boundary closes both: reject `chain` that does
not match something like `[A-Za-z0-9_-]{1,8}` in `/analyze`, `/progress` and
the two feasibility routes. Real mmCIF chain ids are alphanumeric.

**Not XSS.** I checked all four client sinks that render these error strings —
`templates/scout/index.html:816`, `:819-823` and
`templates/scout/feasibility.html:342-346`, `:348-352` — every one uses
`textContent`. The interpolated chain in the new 404 message cannot execute.

---

### D5 (LOW) — `except OSError` is narrower than the read it guards

**Where.** `scout/routes.py:321-325`. `csv.Error` and `UnicodeDecodeError` are
not subclasses of `OSError`, so a malformed or half-written `results.csv`
raises out of the helper instead of degrading to a cache miss — a 500 where the
whole design intent is "a miss costs a rescore and nothing else".

Reachability is narrow but not zero: `anon_compute_slot(ANON_MAX_CONCURRENT_RUNS
= 4)` (`scout/routes.py:119`, `:604`) permits two requests for different chains
of the same job to run concurrently, and both write the same
`job_dir/results.csv`.

**Reasoned by reading, not executed** — I did not construct the race.

**Fix.** `except (OSError, csv_module.Error, UnicodeDecodeError):`

---

### D6 (INFO) — `feasibility_results.csv` still cannot name its chain

`FEASIBILITY_CSV_COLUMNS` (`scout/pipeline.py:531-548`) has no chain column,
and `/scout/feasibility/download/<job_id>` (`scout/routes.py:1200-1225`) takes
no chain. There is no cache bug here — `run_feasibility_pipeline` is called
unconditionally (see the verified claims below) so the file always matches the
last successful run — but if that run raised (`scout/routes.py:1041-1042`) the
previous chain's file stays on disk and is still downloadable. Symmetric to the
fix just applied on the other CSV; a `chain_id` column costs one line.

---

## The late front-end change (`templates/scout/index.html`)

Assessed separately because it arrived after the brief was written.

**It is correct, and it closes the front-end half of the same bug.**
`renderEpitopeTable` now takes the chain as a parameter
(`templates/scout/index.html:637`) and its one and only caller
(`:541`, inside `renderViewer`) passes `data.chain` from the `/analyze`
response (`:455`) rather than re-reading the live dropdown. Before this, a user
who analysed chain A and then moved the dropdown to B — without re-running —
had every "Assess feasibility" link built as `epitope_id` from chain A's table
plus `chain=B`. Pre-fix that silently mis-scored; post-fix it would 404. Now the
link is internally consistent. The added `encodeURIComponent` is right too, and
incidentally hardens the href against the hostile chain id in D4.

Dead branch, harmless: the `chain || document.getElementById('chain-select').value`
fallback at `:671` can never take the right-hand side, since the sole caller
always passes a defined chain. Leave it or drop it.

**It does not address D1 or D3.** The "Top 3 CSV" button is still shown
unconditionally at `:431-437`, and D3's stale-link path (back button, a tab
opened before the second chain was analysed, a bookmark, the `/login?next=`
round trip) still reaches the feasibility page with a chain that
`results.csv` no longer holds.

---

## Note, not a defect: `fieldnames` is now unbound-by-luck

`scout/routes.py:777` reads `fieldnames`, which is only bound inside
`if csv_path is not None:` at `:661`, on line `:664`. Before this change, `results.csv`
always existed by that point, so `fieldnames` was always bound. The change makes
`csv_path is None` reachable *after* a successful `run_pipeline` (a header-only
CSV), which puts `fieldnames` on an unbound path.

It does not raise, because `top3` is necessarily empty whenever `csv_path` is
`None`, and `and` short-circuits before evaluating `fieldnames`. I confirmed by
execution that the route returns 200 in that state. But it is load-bearing luck
across three separate code blocks. `fieldnames = []` before `:660` removes it,
and D1's fix will touch that line anyway.

---

## Claims I checked and found TRUE

**The bug is real and the fix closes it.** Proved by mutation test, not by
re-running the author's suite in place. I copied the tree to my temp dir,
restored `scout/routes.py`, `scout/pipeline.py` and `scout/flags.py` from
`HEAD` (confirmed: `grep -c _results_csv_for_chain scout/routes.py` → `0`),
kept the new test file, and ran it:

```
4 failed, 3 passed
FAILED ...::test_chain_b_after_chain_a_returns_chain_b
FAILED ...::test_chain_b_actually_runs_the_pipeline
FAILED ...::test_a_pre_fix_results_csv_is_a_miss_not_a_wrong_answer
FAILED ...::test_epitope_id_is_not_resolved_against_another_chain
```

with the original bug printed verbatim:

```
E  AssertionError: feasibility resolved epitope_id 1 against chain A's results
   while analysing chain B: [{'chain': 'B', 'residues': [10, 11, 12, 13, 14, 15, 16]}]
```

Against the fixed source all 7 pass.

**The new tests are not tautological.** They assert on the residue numbers that
came out of the CSV (`CHAIN_RESIDUES = {"A": 10..16, "B": 60..66}`), not on the
`chain` field the response echoes back from the request, and
`test_chain_b_actually_runs_the_pipeline` asserts on a list of chain ids the
stub recorded. The author's docstring says exactly this and it is accurate.

**The cache still works.** `test_same_chain_twice_still_reuses_the_cached_run`
passes on the fixed source; my own probe reproduced it independently
(`pipeline runs: ['A']` for two identical requests).

**The normal browser flow's cost is unchanged.** Read-verified:
`templates/scout/index.html:923-950` calls `/scout/quota`, then
`openProgressStream`; `/scout/progress` (`scout/routes.py:864-980`) always runs
the pipeline with no cache gate; `:341` then calls `_finalizeAnalysis`, which
POSTs `/scout/analyze` (`:392-395`), which now hits the CSV the SSE run just
stamped. One pipeline run per analysis, before and after.

**`feasibility_results.csv` has no cache gate.** `scout/routes.py:1037-1040`
calls `run_feasibility_pipeline` unconditionally — no existence check, nothing
to chain-scope. Author's claim confirmed by reading.

**No column-order or header contract is broken by inserting `chain_id` at index
1.** Repo-wide grep for `results.csv`, `results_annotated.csv`, `epitopes.csv`,
`epitopes_annotated.csv`, `analyze_cache.json`, `feasibility_results.csv` across
`.py`, `.html`, `.js`, `.md`, `.json`, `.yml`; grep for `csv.reader` /
`csv_module.reader` across `scout/`, `tests/`, `scripts/`, `blueprints/` — no
positional readers anywhere. `contracts/CONTRACTS_SHA256.lock` covers only
`rpc.py` and `upload_urls.py`. No doc, template, static asset or script asserts
a header string or a column count. The one place a `DictWriter` fieldname
mismatch could raise `ValueError` — `scout/routes.py:759` and `:769` writing
rows read from `results.csv` against `CSV_COLUMNS_ANNOTATED` — is exactly what
the new `test_flags_column_list_matches_the_pipeline` now guards, and that guard
did not exist before. Good addition.

**The test-helper edit is load-bearing and weakens nothing.** I restored only
`tests/test_scout_anonymous_access.py` from `HEAD` against the fixed source:

```
3 failed, 57 passed, 1 skipped
FAILED ...::test_analyze_scores_the_example
FAILED ...::test_results_are_readable_back
FAILED ...::test_signed_in_users_never_consume_a_slot
...
ModuleNotFoundError: No module named 'freesasa'
```

Exactly as the docstring claims: without the stamp those tests cache-miss into
the real pipeline. The assertions themselves are untouched — they still require
`body["epitopes"]` with `composite_score > 0`. The contract they pin has become
strictly narrower (results.csv stamped with the *requested* chain), and the
mismatch case they no longer cover is covered by the new file.

**Helper edge cases behave safely.** Called directly:

```
missing file        -> None
empty file          -> None
header only         -> None
stamped A, ask 'A'  -> True
stamped A, ask 'a'  -> False
stamped A, ask ' A' -> False
stamped A, ask 'B'  -> False
header-less         -> False | ask '1': False
mixed rows, ask 'A' -> True | ask 'B': False
dir named results   -> None
```

Every failure mode is a miss (safe) rather than a wrong hit. Case and
whitespace differences miss rather than match — safe, and not even a cost
problem, because the routes `.strip()` the chain and `run_pipeline` rejects a
chain id that is not in the structure with the same case sensitivity
(`scout/pipeline.py:307`), so a case-mismatched request 422s rather than
silently rescoring.

**Legacy job dirs are a bounded transient.** `cleanup_old_jobs` reaps job
directories after `max_age_seconds = 3600` (`scout/jobs.py:163`), so unstamped
pre-deploy `results.csv` files — which the fix correctly treats as misses —
disappear within an hour. The rescore cost of the rollout is ≤ 1 hour, not
permanent.

---

## Claims I checked and found FALSE or unproven

**"`/scout/download` needs no chain param" — REFUTED.** See D1. The route needs
no chain param *if* the derived files are always rewritten, and two of the four
are not.

**"Every reader of the file goes through here" (`_results_csv_for_chain`
docstring) — true only of the four *cache-gating* reads.** The four reads that
decide whether a chain is already scored do route through the helper
(`scout/routes.py:608`, `:660`, `:1014`, `:1112`), verified by grep. But
`scout/routes.py:850` reads `results.csv` directly (see D1). But the derived files have readers
that do not: `epitopes_annotated.csv` / `epitopes.csv` (D1),
`analyze_cache.json` (D2), `feasibility_results.csv` (D6). The docstring reads
as a completeness claim about the fix and it is not one.

**"an unstamped stub here would send *every one* of these tests into the real
freesasa pipeline" (test helper docstring) — 3 of the 5 call sites, not every
one.** Measured above. Cosmetic overstatement; the substantive claim holds.

**The "zero-patch CSV is a permanent cache miss" concern — mechanism real,
trigger not reachable through a normal run.** I did prove the mechanism: given a
header-only `results.csv`, two identical same-chain `/analyze` calls run the
pipeline twice (`pipeline runs: ['A', 'A']`) where pre-fix it ran once. But I
then checked whether `run_pipeline` can actually emit a header-only file, and it
cannot — it raises `ValueError` at `scout/pipeline.py:346-349` (too few surface
residues) and `:361-364` (no patches formed) before reaching the writer. The only
ways to get one on disk are a truncated write (worker killed between
`writeheader()` and `writerows()`) or a legacy dir, and in the truncated-write
case the pre-fix behaviour was *worse* — it treated the empty file as a hit and
returned zero epitopes forever. Not a defect. I am recording it because I
initially scored it as one.

---

## Could not verify

- **Real-pipeline behaviour end to end.** `freesasa` is not installed in this
  venv, so `run_pipeline` cannot execute here. Every route-level result above
  comes from a stubbed `scout.pipeline.run_pipeline` writing a CSV in the real
  format via the real `_CSV_COLUMNS_BASE`. The one test that would have
  exercised the real scorer,
  `test_scout_anonymous_access.py::test_analyze_runs_the_real_pipeline`, is the
  single skip in this suite (`SKIPPED [1] tests\test_scout_anonymous_access.py:195:
  freesasa is not installed in this environment`) — so no evidence I relied on
  is vacuous, but no evidence covers the real biophysics path either. The
  `chain_id` stamp itself is a plain dict key on a row the writer already
  builds, so the risk there is low.
- **The concurrent-write race in D5.** Reasoned from the code; I did not build a
  two-worker reproduction.
- **Whether a real-world mmCIF from RCSB can carry a hostile `auth_asym_id`.** I
  proved Biopython and `parse_pdb` accept one from a hand-built file, which is
  the case that matters (uploads are user-supplied), but I did not check whether
  `/scout/fetch-pdb` could pull one from RCSB.
