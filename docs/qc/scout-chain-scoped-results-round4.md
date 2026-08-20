# QC round 4 — chain-scoped Scout results

Independent adversarial review of the six fixes made in response to round 3.
I did not write any of this code.

Environment: worktree `.claude/worktrees/suspicious-dewdney-13b07e`,
HEAD = main = `7fd180d`, interpreter `venv/Scripts/python.exe`, Windows,
`freesasa` absent so every route-level probe runs against a stubbed
`run_pipeline` writing the real CSV format through the real `_CSV_COLUMNS_BASE`.

**All six touched files are restored byte-identical.** Every mutation was
applied at byte level with the file's own terminator (CRLF for the five tracked
files, LF for the new test file), confirmed landed by md5 before any conclusion
was drawn, and md5-verified restored after each one. Proof at the bottom.

Baseline for every mutation row: `-k scout` → **172 passed, 3 skipped**
(round 3 measured 168; the author has since added four tests). Runs were
serialised — no background pytest ran at any point.

---

## Verdict

**DO NOT SHIP** — for one root cause, in the same function and the same defect
class that stopped round 3, plus a stale-download path the change's own comment
says cannot exist.

The backend correctness core is now good. I could not make a legitimate
analysis reach the new 422; the legacy-CSV path rescores correctly on
`/scout/analyze`; the 409 fires only under a genuine steal and never in a
sequential flow; every chain id `_valid_chain` accepts round-trips through the
CSV faithfully; round 3's two blocking items (D1 UniProt, D3 untested
feasibility guard) are fixed **and** now mutation-caught. 29 of 36 mutants die.

What stops it is that the fix for the stale-element class was applied
**conditionally, in the caller's `else` branch**, when the pattern the author
himself adopted for the UniProt bar and the known-binders table is
**unconditionally, before the render**. Two elements were missed by that
inconsistency (`#flag-reference` / `#flag-ref-grid`), and on the *success* path
the epitope table, legend and flag cards are the previous chain's for the whole
duration of the async viewer load — permanently if `/scout/pdb` fails. All of it
closes with the three lines that make the clearing unconditional.

---

# PART A — the new 422 path

## Can a legitimate analysis that DID produce results reach the 422? No.

`run_pipeline` raises `ValueError` at `pipeline.py:346` (too few surface
residues) and `:361` (no patches), and the row loop at `:412` has no `continue`
— so **`patches` non-empty ⇒ `rows` non-empty**. `chain_id` is validated
`in available_chains` (`:307`) and written verbatim at `:491`, with no
normalisation anywhere. A successful `run_pipeline` therefore always leaves at
least one data row stamped with the exact requested string, and the re-read
hits. The two pipeline `ValueError`s are answered by the *pre-existing* 422 at
`routes.py:672` carrying the pipeline's own message, before anything is written.

Every way `_results_csv_chain_id` can return None, executed:

```
   absent                           -> None
   header-only                      -> None
   header+blank lines               -> None
   legacy (no chain_id column)      -> None
   column present, cell EMPTY       -> ''          <- NOT None; see D6
   undecodable bytes                -> 'ÿþ'        (cp1252; UnicodeDecodeError -> None on Linux)
   path is a DIRECTORY              -> None
```

and what each becomes at the route:

```
   pipeline leaves header-only            -> 422  No surface patches could be scored for chain A...
   pipeline leaves a legacy file          -> 422  No surface patches could be scored for chain A...
   pipeline leaves EMPTY chain_id cell    -> 409  Another analysis on this job replaced the results...
   pipeline leaves a DIFFERENT chain      -> 409  Another analysis on this job replaced the results...
```

The unreadable-file route to a 422 is not reachable in practice: the same
unreadability makes the gate miss, so `run_pipeline` runs and its own write
fails first, which is a 500 from `except Exception`, not a 422.

**The 422 is also nearly invisible in the real UI.** `/scout/progress`
(`routes.py:1011`) calls `run_pipeline` unconditionally with no cache gate, and
the page always opens that stream before POSTing `/scout/analyze`. A chain with
no scoreable surface therefore errors out on the SSE with the pipeline's own
message and `/scout/analyze` is never called. The new 422 is a correctness
backstop, not a user-facing message — which is fine, but worth knowing before
anyone invests in its wording.

## The legacy-CSV interaction — tested end to end, not reasoned about

`/scout/analyze` **rescores, correctly**:

```
   on-disk header: epitope_id,residues,residue_count,mean_rsa,composite_score,h
   _results_csv_chain_id -> None
   SAME chain A as the legacy file : 200  pipeline=['A']  resi=[10..16]
   DIFFERENT chain B               : 200  pipeline=['B']  resi=[60..66]
```

`/scout/feasibility/*` **does not** — see D4.

## Does the 409 still fire, and only when it should? Yes.

```
### a genuine steal (results.csv replaced mid-run)
   stolen mid-run -> 409 'Another analysis on this job replaced the results...'

### the sequential flows a real user produces
   ('A', 200, [10..16])  ('B', 200, [60..66])  ('A', 200, [10..16])
   ('A', 200, [10..16])  ('B', 200, [60..66])
   pipeline calls: ['A', 'B', 'A', 'B']        <- cache still works, no 409, no 422
```

## Does the 422 leave the job dir sane? No — D1.

---

# PART B — confirmed defects

## D1 (MEDIUM — the previous chain's file served from a stable URL) — the 422 and 409 early returns skip the derived-file cleanup, and the comment says they cannot

**Where.** `scout/routes.py:719-738` returns before `:847-877`, where the
`else: …unlink(missing_ok=True)` pair added in round 2 lives. Three lines below
the returns, `:843-846` states:

> Both top-3 files are rewritten or removed on every run, never left alone.

That is false on both new error paths. **Executed** — analyse A, then a chain B
whose run produces no rows:

```
   analyze A -> 200   /scout/download top3 : 200 '1,A,"ALA10,...,ALA16",7,0.55,0.72,'
   analyze B -> 422 'No surface patches could be scored for chain B...'
   job dir now: ['.owner','analyze_cache.json','epitopes.csv','epitopes_annotated.csv',
                 'input.pdb','results.csv','results_annotated.csv']
   /scout/download top3   after the 422 : 200 '1,A,"ALA10,...,ALA16",7,0.55,0.72,'
   /scout/download?full=1 after the 422 : 200 '1,A,"ALA10,...,ALA16",7,0.55,0.72,'
   analyze_cache chain still: A
   results.csv rows: 0
```

This is round 1's D1 re-opened on the paths this round created. `results.csv`
now holds zero rows while the two files `/scout/download/<job_id>` serves still
hold chain A's — and that endpoint takes no chain parameter, so it cannot tell.

**Two things hold the severity down.** The page hides `#results-section` at the
start of every run (`index.html:971`), so the link is not on screen after the
error; and the stamp means the delivered CSV *says* `chain_id = A`, so the
artefact is stale rather than mislabelled. What is exposed is the URL the user
already has — history, a bookmark, a second tab.

**Fix.** Move the two `unlink` blocks above the early returns, or clear both
derived files in the 422/409 branch. Either way the comment at `:843` becomes
true instead of aspirational.

---

## D2 (MEDIUM — the previous chain's scientific annotation on screen) — `_handleAnalysisResult` still leaves `#flag-reference` populated

**Where.** `templates/scout/index.html:471-479` (the zero-epitope branch clears
`viewer-container`, `epitope-legend`, `epitope-table-body` and nothing else)
versus `:725-748` (`renderFlagReference`, the only writer and the only hider of
`#flag-reference`).

`renderFlagReference` is reachable **only** through
`renderEpitopeTable` ← `renderViewer` ← `epitopes.length > 0`. The card set is
per chain: `allFlags` is collected from *this chain's* `ep.quality_flags`
(`:673-679`). So a chain with no epitopes keeps the previous chain's flag cards,
and each card is an interpretation, not a label —
*"Glycans can block binder access in vivo even when absent from crystal
structures…"* — attached to a chain that never raised that flag.

**Evidence — executed** against the real page JS (extracted from the
Flask-rendered `/scout/`) in a DOM stub. Chain A with one epitope flagged
`glycan proximity`, then chain B with none:

```
--- after chain B (0 epitopes, no uniprot, no binders) ---
  uniprot-bar          hidden=true                            <- fixed this round
  known-binders-section hidden=true                           <- fixed round 2
  epitope-table-body   (empty)                                <- fixed round 2
  epitope-legend       (empty)                                <- fixed round 2
  viewer-container     (empty)                                <- fixed round 2
  flag-reference       hidden=false                           <- *** CHAIN A ***
  flag-ref-grid        html="<div class=\"flag-ref-header\"><span class=\"badge badge-red\">glycan proxim…"
  feasibility-note     hidden=false                           <- chain A (static text, cosmetic)
```

`resetAll()` (`:864-891`) misses both as well, so the cards also survive a Reset
into a completely different upload until the next chain that scores something.

This is the fifth element in the sequence round 2 → round 3 → round 4 has been
walking one at a time. The reason it keeps happening is D3's root cause.

---

## D3 (MEDIUM — same class, and it fires on the SUCCESS path) — the clearing is conditional when it should be unconditional

**Where.** `templates/scout/index.html:466-479`. `renderViewer` is `async` and
is called **without `await`**, and `renderEpitopeTable` / `renderLegend` run at
its *end* (`:568-570`), after the `/scout/pdb` fetch. The clearing of the table,
legend and viewer lives in the caller's `else` branch, so on the
`epitopes.length > 0` path nothing is cleared before the render starts.

Two consequences, both executed:

**(a) A window on every chain switch.** With a 200 ms structure fetch, mid-flight:

```
   uniprot-bar.hidden      = true          <- already chain B (cleared)
   download-link.display   = inline-flex   <- already chain B
   epitope-table shows     = ALA10         <- STILL CHAIN A
       ...<td class="mono-cell">ALA10</td>...<span class="badge badge-red">glycan proximity</span>
       ...href="...&epitope_id=1&chain=A"...
   epitope-legend nonempty = true          <- STILL CHAIN A
   flag-reference.hidden   = false         <- STILL CHAIN A
```

so for the whole load the page shows chain B's headings, chain B's UniProt bar
and chain B's download links over chain A's results table. (The stale row's
feasibility link still carries `chain=A`, so a click during the window is at
least a self-consistent A pair — the backend fix protects against harm here,
the display does not.)

**(b) Permanently, when the structure fetch fails.** `renderViewer` returns
early at `:561-563` — `container.textContent = 'Could not load structure for
visualization.'; return;` — **before** `renderLegend` and `renderEpitopeTable`:

```
--- chain B scored 1 epitope but /scout/pdb 404'd ---
  viewer-container   html="Could not load structure for visualization."
  epitope-table-body html=<chain A's row>          <- STILL CHAIN A
  epitope-legend     html=<chain A's legend>       <- STILL CHAIN A
  flag-reference     hidden=false                  <- STILL CHAIN A
  download-link      display='inline-flex'         <- chain B's file
```

No error banner in this state: `showAnalyzeError` is not on that path.

**Fix (the one that also closes D2).** Clear `#viewer-container`,
`#epitope-legend`, `#epitope-table-body`, `#flag-reference` and
`#flag-ref-grid` *unconditionally, before* the `if (epitopes.length > 0)` — the
exact shape already used for the UniProt bar at `:453-454` and known-binders at
`:488-489`. Then the `else` only has to call `showAnalyzeError`, and
`resetAll` should hide the two flag elements too.

---

## D4 (MEDIUM–LOW — refuses work that should succeed, for one deploy window) — a pre-deploy job dir 404s the feasibility routes

**Where.** `scout/routes.py:1109-1111` and `:1205-1206`.

`/scout/analyze` rescues an unstamped `results.csv` by rescoring. Neither
feasibility route has a rescore, so a job dir written before this deploy — the
user analysed chain A five minutes ago, clicked *Assess feasibility* — gets:

```
   POST /scout/feasibility/analyze  -> 404 {'error': 'No Epitope Scout results found for
                                            chain A on this job. Run epitope analysis on
                                            that chain first.'}
   GET  /scout/feasibility/progress -> data: {"stage": "error", "msg": "No Epitope Scout
                                            results found for chain A on this job..."}
   pipeline runs triggered by the feasibility path: []
```

On `main` that request succeeded. It is a real regression for live jobs, bounded
by `cleanup_old_jobs`'s hour, and the message does name the remedy that works
(re-running Analyze re-stamps the file). **Correctly traded** — trusting an
unstamped file is the silent-wrong-answer bug this change exists to kill — but
nothing in the change says so and nothing tests it. It belongs in the commit
message as a deploy note, one line.

---

## D5 (LOW — a docstring that certifies false) — fix 5 removed the disproven SSE rationale from one of the three places it lives

`scout/routes.py:303-307` was rewritten and is now correct. Both copies in the
test file survive:

`tests/test_scout_chain_scoped_results.py:416-417`, class docstring:

> What genuinely cannot be carried: control characters, because chain_id is
> interpolated into an SSE frame that a newline would terminate.

`:492`, `test_a_newline_chain_cannot_forge_an_sse_frame`:

> The actual reason control characters are refused.

Round 3's D7 named the routes.py comment **and** this test's docstring. Every
SSE emitter builds its payload with `json.dumps` (`routes.py:943, 996, 1005,
1022, 1157, 1206, 1256, 1265`); the claim is false wherever it is written, and a
test docstring is the copy a future reader is most likely to trust, because it
sits next to a passing assertion.

The same docstring (`:409-413`) also still asserts the formula-injection
dismissal round 3 D4 refuted — *"There is no second party to attack"* — as
settled fact. The threat on record was never a cross-user read; it was the
victim uploading the attacker's `.cif` and opening their own CSV.

---

## D6 (LOW — docstring vs code, not reachable through the app) — an empty `chain_id` cell is a 409, not a miss

`_results_csv_chain_id`'s docstring (`routes.py:353-359`) says None covers
"every way the file fails to name a chain" and that "a file that cannot name its
chain is not evidence about any chain". A present-but-empty cell returns `''`:

```
   _results_csv_chain_id -> ''
   _results_csv_for_chain(A) -> None        (miss — correct)
   route              -> 409 'Another analysis on this job replaced the results...'
```

so the one shape that most obviously "cannot name its chain" is reported as a
collision that did not happen — the exact failure the 409/422 split was made to
end. Not reachable today (`_valid_chain` forbids an empty chain and only
`run_pipeline` writes the file), so this is a docstring-vs-code defect. One
token fixes it: `if stamped:` rather than `if stamped is not None:`.

---

## D7 (LOW — a claim a future caller will trust) — not every reader goes through the helper

`_results_csv_for_chain`'s docstring (`routes.py:337-339`):

> Every reader of the file goes through here so a new caller inherits the check
> instead of the bug

`download()` at `:945` reads `job_dir / "results.csv"` directly as the `?full=1`
fallback. Harmless today (the file self-labels, and `results_annotated.csv`
exists after any successful run) but the sentence is not true as written.

---

## D8 (cosmetic — dead code the fix left behind) — `routes.py:722`

`fieldnames: list[str] = []` is never read: the only branch that skips the
`with` block now returns, and the block reassigns it immediately.

---

# PART C — mutation table, measured independently

36 mutants, each applied alone at byte level, md5-confirmed landed, `-k scout`
re-run, restored, md5-confirmed clean. **29 caught, 7 not.** Round 3 measured
16/21 on a smaller mutant set; the four rows that changed are called out below.

| # | Behaviour reverted | Verdict | Tests that fail |
|---|---|---|---|
| F1a | `_valid_chain` → `[A-Za-z0-9]{1,8}` | **CAUGHT (10)** | `test_parser_reachable_ids_are_not_refused` ×8, `test_a_parser_reachable_id_analyses_end_to_end`, `test_a_long_chain_id_the_dropdown_offers_is_not_refused` |
| F1b | `_valid_chain` accepts everything | **CAUGHT (14)** | `test_json_routes_reject_unsafe` ×7, `test_sse_routes_reject_unsafe` ×7 (+forge, +absurd) |
| F1c | guard off `POST /scout/analyze` | **CAUGHT (7)** | `test_json_routes_reject_unsafe`, `test_an_absurd_chain_id_is_still_refused` |
| F1d | guard off `GET /scout/progress` | **CAUGHT (7)** | `test_sse_routes_reject_unsafe`, the forge test |
| **F1e** | **guard off `POST /scout/feasibility/analyze`** | **CAUGHT (6)** — round 3's D3 **CLOSED** | `test_json_routes_reject_unsafe` ×6 |
| F1f | guard off `GET /scout/feasibility/progress` | **CAUGHT (6)** | `test_sse_routes_reject_unsafe` ×6 |
| **F1g** | **`_CHAIN_ID_MAX_LEN` back to 16** | **CAUGHT (1)** | `test_a_long_chain_id_the_dropdown_offers_is_not_refused` |
| M-cache | analyze gate back to bare existence | **CAUGHT (7)** | the whole chain-scoping class |
| M3 | analyze CSV reader back to job-scoped | **CAUGHT (2)** | stolen-results + scores-nothing |
| M9 | a legacy CSV becomes a HIT | **CAUGHT (1)** | `test_a_pre_fix_results_csv_is_a_miss_not_a_wrong_answer` |
| M11 | a header-only CSV becomes a HIT | **CAUGHT (2)** | `test_a_header_only_results_csv_is_a_miss` + scores-nothing |
| **M-pipe** | **`run_pipeline` stops stamping `chain_id`** | **NOT CAUGHT locally** | CI-only (`test_analyze_runs_the_real_pipeline`, needs freesasa) |
| M-flags | `scout/flags.py` column list drifts | **CAUGHT (18)** | incl. `test_flags_column_list_matches_the_pipeline` |
| F2 | 409/422 → the destructive empty-200 fall-through | **CAUGHT (2)** | stolen-results + scores-nothing |
| **F8** | **the new 422 collapsed back into the 409** | **CAUGHT (1)** | `test_a_chain_that_scores_nothing_is_not_reported_as_a_collision` |
| **F8b** | **the 409 collapsed into the 422** | **CAUGHT (1)** | `test_a_stolen_results_file_is_a_409_not_an_empty_200` |
| NC1 | `epitopes_annotated.csv` no longer removed | **CAUGHT (1)** | `test_top3_download_never_serves_the_previous_chain` |
| NC2 | `epitopes.csv` no longer removed | **CAUGHT (1)** | same |
| M4a | `_get_binder_overlaps` chain gate removed | **CAUGHT (2)** | both directions |
| M4b | `_get_binder_overlaps` → `return []` | **CAUGHT (1)** | `test_binder_overlaps_are_actually_returned_for_the_right_chain` |
| M5 | feasibility_analyze `epitope_id` job-scoped | **CAUGHT (1)** | `test_epitope_id_is_not_resolved_against_another_chain` |
| M6 | feasibility_progress `epitope_id` job-scoped | **CAUGHT (2)** | + `test_sse_separates_no_results_from_unknown_epitope` |
| F4 | 3-way SSE message → 2-way | **CAUGHT (1)** | `test_sse_separates_no_results_from_unknown_epitope` |
| F3a | `chain_id` out of `FEASIBILITY_CSV_COLUMNS` | **CAUGHT (8)** | all of `test_scout_interface_competition.py` — but only because `DictWriter` raises |
| **F3b** | **`run_feasibility_pipeline` stops stamping** | **NOT CAUGHT** | — unchanged from round 3 |
| **F5** | **`showAnalyzeError`'s btn lookup → `null` (neutered)** | **NOT CAUGHT** | — see note |
| F5b | the same two lines **deleted** | **CAUGHT (1)** | `test_an_error_re_enables_the_analyze_button` |
| **F6a** | **zero-epitope clear disabled** | **NOT CAUGHT** | — unchanged from round 3 |
| F6b | top-3 link shown unconditionally | **CAUGHT (1)** | `test_an_empty_chain_clears_the_previous_chains_results` |
| **F6c** | **known-binders no longer cleared** | **NOT CAUGHT** | — unchanged from round 3 |
| F6d | feasibility link back to the live dropdown | **CAUGHT (1)** | `test_feasibility_link_uses_the_scored_chain_not_the_dropdown` |
| F6f | only the `epitope-table-body` clear removed | **CAUGHT (1)** | `test_an_empty_chain_clears_the_previous_chains_results` |
| **F6g** | **only the `epitope-legend` clear removed** | **NOT CAUGHT** | — the test names one id, not the set |
| **F7** | **UniProt clear removed (round 3's blocker)** | **CAUGHT (1)** | `test_the_uniprot_bar_does_not_survive_a_chain_switch` |
| F7b | only `uniprot-bar.hidden = true` dropped | **CAUGHT (1)** | same |
| **E1** | **feasibility 404 loses the chain name** | **NOT CAUGHT** | — see note |

## What the table says

* **Round 3's two blocking findings are genuinely closed and genuinely tested.**
  F1e now kills six tests; F7 and F7b both die. Both fixes are real.
* **The template tests catch deletion of a named string, nothing else.** F5
  survives when the element lookup is replaced by `null` while the asserted
  literal `btn.disabled = false` stays on the page; F5b (the same lines deleted)
  dies. F6g survives because
  `test_an_empty_chain_clears_the_previous_chains_results` asserts
  `"epitope-table-body" in handler` and says nothing about the legend. With no
  JS harness in the repo this is a ceiling, not an oversight — but the honest
  statement is narrower than round 3's: they catch deletion **of the specific id
  they name**. Three of the six template mutants slip through, and D2/D3 are
  live proof that string-presence tests do not constrain this function.
* **E1 is new and cheap to close.** The `for chain {chain_id} on this job`
  wording in the JSON feasibility 404 — the user-visible half of fix 3 on that
  route — can be reverted to the old job-scoped message with the whole suite
  still green. Its SSE sibling *is* pinned (F4 dies). One `assert "chain A" in
  …` in `test_epitope_id_is_not_resolved_against_another_chain` fixes it.
* **F3b unchanged**: the chain landing in `feasibility_results.csv` is still
  untested; F3a only catches the writer/column inconsistency.
* **M-pipe unchanged**: CI-only.

## Are the new tests real?

Yes, on the points I checked. `test_an_absurd_chain_id_is_still_refused` is a
genuine statement of the residual, not a hidden failure: it dies under F1b, so
it is load-bearing in both directions. `test_a_long_chain_id_the_dropdown_offers_is_not_refused`
dies under F1g, so the 16→64 move cannot be silently reverted.
`test_json_routes_reject_unsafe`'s message assertion is what closed F1e —
verified by reverting the guard, exactly the mutation round 3 used.

---

# PART D — the change read as one piece

## Is 64 the right line, and is stating the residual adequate?

**Yes to 64.** PDB column 22 is one byte; `auth_asym_id` in the archive runs to
a handful; the longest realistic case anyone has named here is the 20-character
entity-derived id from conversion pipelines, which the author's own test uses.
64 is 3× that and 16× the archive worst case, and it bounds what gets carried
into CSV cells and log lines. I could not construct a realistic structure it
refuses.

**Stating the residual is adequate but not optimal.** The complete fix — filter
`/scout/upload`'s offer through `_valid_chain` so the app never proposes what it
refuses — is *not* the one line round 2 implied: the chain list is built at
three sites (`routes.py:502, 594, 632`, for upload / fetch-by-id / example). A
shared helper would do it, but it is a real edit with its own blast radius, and
the failure it prevents needs a hand-crafted file. Deferring it with a test that
names the behaviour is a defensible call and much better than silence.

## The two explicit deferrals

**CSV formula injection — agree it is not a blocker, disagree with how it is
recorded.** The ownership model is sound and I re-verified nothing else; the
residual threat needs a social step and modern Excel warns on DDE. But the test
docstring still argues the *narrower* threat and concludes "There is no second
party to attack", which is the sentence round 3 refuted. Deferring a risk is
fine; a docstring asserting the risk does not exist is not a deferral, it is a
wrong claim with a passing test next to it (D5). Either escape at the writer —
one function, no legitimate chain id affected — or say "accepted, CWE-1236,
needs the attacker to supply the structure".

**Windows-only `unlink` PermissionError — agree, correctly deferred.** Prod and
CI are Linux. Worth noting the two `unlink(missing_ok=True)` calls this change
*adds* at `:858` and `:877` are the same class and equally unguarded, so the
deferral is at least self-consistent. It does bite here: four job dirs survived
`shutil.rmtree(ignore_errors=True)` during my runs and had to be cleared by hand
before the full suite.

## Should anything else have been deferred, or not?

The three items in Part B that I would not defer are D1, D2 and D3, because they
are the same defect class this change exists to eliminate and each is a few
lines. Everything else here is a comment, a docstring or a test assertion.

## Coherence, dead code, duplication

* `_results_csv_for_chain` is now a two-line wrapper over
  `_results_csv_chain_id`. Fine — it keeps the call sites readable and gives the
  invariant one home. No duplication introduced.
* The 409/422 split is well-argued and the comment at `:723-734` explains *why*
  the two states need different answers. Good work; it is the best-explained
  block in the diff.
* Contradictions found: the `:843` comment vs the early returns (D1); three
  docstring claims that are false as written (D5, D6, D7); one dead local (D8).
* Error-message consistency across routes: `/scout/analyze`'s 404 says "Please
  re-upload your file." and `/scout/feasibility/analyze`'s says "Please
  re-upload." — pre-existing, cosmetic. More relevant: the new 422 says "check
  the structure has resolved side chains for that chain" while the pipeline's
  own error for the condition a user would call the same thing says "Too few
  surface residues (N) to form patches. Check chain selection or RSA threshold."
  Since the SSE always fires first, the second is the one users read.

## Checked and NOT a defect

**Every chain id `_valid_chain` accepts survives the CSV round-trip**, so no
legitimate chain can become a permanent miss or a spurious 409:

```
chain id          valid  read back        round-trips
'A' 'ZZ' '=' '|'   yes   identical        HIT
'A,B'              yes   'A,B'            HIT
'A"B'              yes   'A"B'            HIT
"A'B"  'A B' 'A;B' yes   identical        HIT
'\\'  '#'  ' A'    yes   identical        HIT
'A'*64             yes   'A'*64           HIT
```

**The feasibility page has no equivalent stale-element bug.**
`renderFeasibilityResults` (`feasibility.html:478-557`) sets every field
unconditionally and both conditional sections (`risk-factors-section`,
`binder-overlaps-section`) have `else` branches that hide them.

**The 422 does not corrupt subsequent runs.** After the 422 for chain B,
`results.csv` holds B's zero rows; a later request for chain A misses, rescores
and returns A's numbers.

**`analyze_cache.json` after a 422/409 stays the winner's**, so
`_get_binder_overlaps`' gate still rejects correctly for the failed chain.

**Non-ASCII chain ids are a Windows-only artefact**, as round 3 said: `open()`
with no `encoding=` raises `UnicodeEncodeError` on cp1252, writes clean UTF-8 in
prod.

---

# PART E — status of what round 3 left open

| Item | Status |
|---|---|
| R3 D1 — UniProt bar not cleared (**the DO-NOT-SHIP defect**) | **CLOSED**, and mutation-caught two ways (F7, F7b) |
| R3 D2 — dropdown offers a chain analyze 400s | **CLOSED for every realistic id** (cap 64, tested, F1g caught). Residual >64 stated and tested. Three offer sites, not one, so the complete fix is not a one-liner |
| R3 D3 — `test_json_routes_reject_unsafe` could not see the feasibility guard | **CLOSED**, F1e now kills 6 tests |
| R3 D4 — CSV formula injection re-opened | **STILL OPEN by decision.** Agree it is not blocking; the docstring still argues the wrong threat (D5) |
| R3 D5 — header-only CSV a permanent 409 that re-runs the pipeline | **CLOSED** — now a 422 with a message that matches the state, F8/F8b both caught. The rescore-per-attempt is unchanged but the answer is now honest |
| R3 D6 — `renderPpiInterfaces` dead while `detect_interfaces` runs every analyze | **STILL OPEN, unchanged.** `index.html:754` definition, no call site; `_currentPpiInterfaces` (`:318`) declared, cleared at `:890`, never assigned or read; `detect_interfaces` still runs at `routes.py:671` and is serialised into the response twice |
| R3 D7 — the false SSE-forging rationale | **HALF CLOSED** — fixed in `routes.py`, both copies still in the test file (D5) |
| R3 D8 — `/scout/feasibility/analyze` says "epitope_residues or epitope_id is required" when both were supplied | **STILL OPEN, unchanged.** `routes.py:1130` |
| R2 D3 — rollback / mixed fleet `ValueError` → 500 | **STILL OPEN, reproduced:** `OLD writer + NEW row -> ValueError: dict contains fields not in fieldnames: 'chain_id'`; `NEW writer + OLD row -> ok, chain_id cell = ''`. Forward-compatible, backward-fatal for one job-dir lifetime. Nothing tests it, nothing in the change mentions deploy ordering |
| R1 D6 / R2 D4 — `feasibility_results.csv` served with no chain param | **MITIGATED, NOT CLOSED, unchanged.** `feasibility_download` (`routes.py:1316-1332`) takes only `job_id`; the stamp means the file now names its chain, so it is stale-but-labelled |
| FE-6 — Windows-only `unlink` PermissionError | **STILL OPEN**, correctly deferred |
| **New in this round** | `/scout/progress` runs `run_pipeline` unconditionally with no cache gate (`routes.py:1011`), so the UI recomputes on every Analyze click regardless of the chain-scoped cache. Pre-existing, wasted compute only, and `_results_csv_for_chain`'s docstring already flags it honestly |

---

# Full suite

From the worktree root, no path argument, not piped through `tail`:

```
$ venv/Scripts/python.exe -m pytest -q -rf
5321 passed, 20 skipped, 854 warnings in 579.89s (0:09:39)
EXIT=0
```

Exactly the author's measured baseline. No FAILED or ERROR lines.

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
?? tests/test_scout_chain_scoped_results.py

$ git diff --stat
 scout/flags.py                       |   1 +
 scout/pipeline.py                    |  12 ++
 scout/routes.py                      | 260 +++++++++++++++++++++++++++--------
 templates/scout/index.html           |  59 ++++++--
 tests/test_scout_anonymous_access.py |  11 +-
 5 files changed, 276 insertions(+), 67 deletions(-)

$ md5sum tests/test_scout_chain_scoped_results.py
df76e2c2e88991cb3cd3d61433c7d041   (identical to the pre-review value)

$ python mut.py verify
all 6 files byte-identical to snapshot
```

md5 of all six touched files was re-checked against the pre-review snapshot
after **every single one of the 36 mutations** and again at the end; the harness
aborts if any differ. The only file I added to the repo is this one. Every
probe, mutation script and extracted JS block lives in the session scratchpad.
Four job dirs left in the shared `tmp/` by `shutil.rmtree(ignore_errors=True)`
during the mutation runs were removed by hand before the full suite;
`tmp/calibration` was not touched.

# Things I could not verify

* **The real biophysics path.** `freesasa` is absent locally, so every
  route-level result uses a stubbed scorer writing the real CSV format through
  the real column list. `M-pipe` remains CI-only.
* **D2 and D3 in a real browser.** Both were driven against the page JS
  extracted from the Flask-rendered `/scout/`, in a DOM stub. The operations
  involved (`hidden`, `innerHTML`, `style.display`, an unawaited async call) are
  the ones the stub models exactly, and D3(b) is also plain in the source, but it
  is not Chrome.
* **How often `/scout/pdb` actually fails after a successful `/scout/analyze`**
  (D3b's permanent case). D3a's transient window needs nothing to fail and is
  the case I would act on.
