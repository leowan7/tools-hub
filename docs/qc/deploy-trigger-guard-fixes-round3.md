# QC round 3 — PR #160, the round-2 remedy

**SHA reviewed: `dcf6d86e5da3593895ebfe4fdd166cd529bba78b`** (head of PR #160).
Base `b73bfaddf350af3f36c8e647049d60b9a350e416`. Three commits in the range:
`ebe27fd` (round-1 fixes), `616c828` (the round-2 remedy — primary target),
`dcf6d86` (merge of `origin/main` @ `b73bfad`, i.e. #161).

Reviewed in a detached worktree at `dcf6d86`. I did not write rounds 1 or 2 and
am not bound by them. `modal` was never invoked.

## Verdict

**FAIL — one MEDIUM defect blocks, and it is the PR's own headline correction
applied to only one of the two places it lives.**

The substance of round 2's remedy is sound and I verified it first-hand rather
than taking it on report: the `**/` zero-segment claim really is documented,
the transcription of GitHub's table is exact in all 12 rows, the merge is
mechanically clean, both suite baselines reproduce to the test, and deleting
`positive_globstars` lost nothing.

But `tests/test_deploy_trigger_covers_dockerfile_copies.py:263` still says
``foo\*bar`` **is GitHub's escape** — the precise claim that lines 86–94 of the
same file now call *invented*. B3 fixed the module comment and missed the test
docstring. The file asserts both propositions, 170 lines apart, and the commit
message and PR body both report the correction as complete.

Separately, the conformance suite is **real but strictly one-directional**. A
translator that matches every path against every pattern passes all twelve
conformance rows. Two mutations survive the entire 30-test module, one of them
with a silent-staleness direction. Non-blocking, but the docstring's "pins every
row of the table" reads as much stronger protection than exists.

| criterion | verdict |
|---|---|
| 1 — both baselines, measured here | **PASS** |
| 2 — is the conformance suite real? | **PARTIAL** — not vacuous overall, but the table catches under-matching only |
| 3 — test data vs. the source | **PASS** — 12/12 exact, 15/15 rows accounted for |
| 4 — right population / same entry point | **PASS**, with a nuance worth stating |
| 5 — did deleting `positive_globstars` lose anything? | **PASS** — strictly subsumed |
| 6 — docstring accurate, citation resolves? | **FAIL** — D-R3-1 |
| 7 — merge commit for lost content | **PASS** — byte-identical to the mechanical merge |
| 8 — new overclaiming | **PASS** except D-R3-1 |
| 9 — what still has no guard | reported below |

---

## Criterion 1 — baselines. PASS

Both measured by me, in fresh worktrees, repo venv by absolute path, no path
argument, redirected to a file (never piped through `tail`).

```
$ cd .../qc160r3-base            # git worktree add --detach ... b73bfad
$ .../venv/Scripts/python.exe -m pytest -q > base_suite.txt 2>&1
5379 passed, 21 skipped in 346.18s (0:05:46)

$ cd .../qc160r3                 # detached at dcf6d86
$ .../venv/Scripts/python.exe -m pytest -q > head_suite.txt 2>&1
5393 passed, 21 skipped in 367.28s (0:06:07)
```

| tree | result |
|---|---|
| base `b73bfad` | **5379 passed, 21 skipped** |
| head `dcf6d86` | **5393 passed, 21 skipped** |
| delta | **+14** |

Both match the worker's reported figures exactly. Zero `FAILED`, zero `ERROR`
in either run, so no isolation rerun of the two known-flaky node tests was
needed — I did not probe their flakiness.

### Collected-count reconciliation — verified, not quoted

```
$ pytest -q --collect-only                    # base 5400 / head 5414
$ pytest -q --collect-only <guard module>     # base   16 / head   30
```

| | base | head | delta |
|---|---|---|---|
| whole suite collected | 5400 | 5414 | +14 |
| guard module collected | 16 | 30 | +14 |
| suite passed + skipped | 5379+21 = 5400 | 5393+21 = 5414 | — |

The **entire** suite delta is the guard module. Name-level diff of the module's
collected ids confirms the composition exactly — 12 parametrized rows plus two
tests, nothing else added or removed anywhere in the suite:

```
> ...::test_documented_rows_using_refused_constructs_are_refused
> ...::test_matches_githubs_documented_negation_examples
> ...::test_matches_githubs_documented_filter_pattern_examples[*]
> ...[**]  [*.js]  [**.js]  [docs/*]  [docs/**]  [docs/**/*.md]
> ...[**/docs/**]  [**/README.md]  [**/*src/**]  [**/*-post.md]  [**/migrate-*.sql]
```

The 16 → 30 claim is correct.

---

## Criterion 2 — is the conformance suite real, or vacuous? PARTIAL

I mutated the translator and ran the module against each mutation, recording
which test ids fail. Harness: `scratchpad/mutate.py` — it applies exactly one
source edit (anchored, asserted to match once), runs pytest on the module,
restores the file, and verifies the restored sha256 equals the original
(`MATCHES=True` printed at the end of the run).

### The mutation table

`star_vs_globstar` = `test_pattern_translation_distinguishes_star_from_globstar`.
`negation` = `test_matches_githubs_documented_negation_examples`.
`live×4` = `test_every_copied_file_is_on_a_deploy_trigger[af2|colabfold|esmfold|mpnn]`.

| # | mutation | conformance rows that caught it | other tests that caught it | module |
|---|---|---|---|---|
| M0 | none (control) | — | — | 30 passed |
| M1 | `*` crosses `/` | **0 / 12** | negation, star_vs_globstar | caught |
| M2 | `**` stops at `/` (all 3 branches) | **7**: `**`, `**.js`, `docs/**`, `docs/**/*.md`, `**/docs/**`, `**/*src/**`, `**/migrate-*.sql` | star_vs_globstar | caught |
| M3 | `**/` requires ≥1 leading segment | **6**: `docs/**/*.md`, `**/docs/**`, `**/README.md`, `**/*src/**`, `**/*-post.md`, `**/migrate-*.sql` | star_vs_globstar | caught |
| M4 | bare `**` behaves like `*` | **2**: `**`, `**.js` | *(none)* | caught by rows only |
| M5a | drop trailing `$` anchor | **0 / 12** | *(none)* | **SURVIVES — 30 passed** |
| M5b | full substring matching | **0 / 12** | negation | caught |
| M6 | invert later-wins (`hit = negated`) | **0 / 12** | negation, star_vs_globstar, live×4 | caught |
| M7 | first-match-wins instead of later-wins | **0 / 12** | negation, star_vs_globstar | caught |
| M8 | `!` loses all meaning | **0 / 12** | negation, star_vs_globstar | caught |
| M9 | **translator matches EVERYTHING** (`re.compile(".*")`) | **0 / 12 — all twelve rows GREEN** | negation, star_vs_globstar | caught, but by no conformance row |
| M10 | translator matches NOTHING | **12 / 12** | negation, star_vs_globstar, live×4 | caught |
| M11 | `/**` requires a child (`tools/**` no longer matches `tools`) | **0 / 12** | *(none)* | **SURVIVES — 30 passed** |
| M12 | drop `\` from the refused set | **0 / 12** | `test_to_regex_refuses_rather_than_guesses` | caught |
| M13 | drop `?` from the refused set | **0 / 12** | `test_documented_rows_using_refused_constructs_are_refused`, `to_regex_refuses` | caught |

### What the table says

**The suite is not vacuous.** 12 of the 14 mutations go red, several loudly, and
every new test added by this PR is load-bearing against at least one mutation
(M13 pins `test_documented_rows_using_refused_constructs_are_refused`; M1/M5b/M7/M8
pin the negation test; M2/M3/M4/M10 pin the conformance rows). B4 is a genuine
addition, not decoration.

**But the conformance table is strictly one-directional.** M9 is the decisive
result: replace the entire translator with `re.compile(".*")` — every pattern
matches every path — and **all twelve conformance rows pass**. The rows can only
ever detect *under*-matching. That is not a transcription failure; it is inherent
to the source, because GitHub's table lists only matches for those 12 rows. The
only negative evidence GitHub publishes lives in the negation row's *Does not
match* entries, and the PR does use both of them — which is why the negation test,
not the table, catches M1, M5b, M7, M8 and M9.

So the over-matching protection in this module comes from
`test_pattern_translation_distinguishes_star_from_globstar` (hand-written,
pre-existing) and the new negation test. The 12-row table adds under-matching
protection only.

### Rows that pass for the wrong reason

Three of the twelve are caught by nothing except a totally inert translator (M10):

- `*` → `README.md`, `server.rb`
- `*.js` → `app.js`, `index.js`
- `docs/*` → `docs/README.md`, `docs/file.txt`

All the listed matches are root-level or single-segment, so each row is satisfied
identically by `*`, `**`, or `.*`. The `*` row in particular contributes **zero**
evidence that `*` does not cross `/` — M1 leaves it green. Again a property of
GitHub's table, not of the transcription, but it means "twelve rows" overstates
the independent evidence: nine rows do work, three are near-tautologies.

### The two survivors

**M5a — the trailing `$` anchor is unguarded, and it has a silent direction.**
`re.compile("^" + "".join(out) + "$")` → `re.compile("^" + "".join(out))` leaves
all 30 tests green. GitHub's source states the rule this violates, one line above
the table: *"Path patterns must match the whole path, and start from the
repository's root."* Nothing in the module derives anchoring from it. Consequences
against the live trigger, computed both ways:

| path | GitHub (correct) | guard with `$` dropped | direction |
|---|---|---|---|
| `tools/af2/example_data.txt` | True | False | loud (false red) |
| `tools/af2/__init__.py.bak` | True | False | loud (false red) |
| `static/example/1HEW.pdb.bak` | False | **True** | **silent staleness** |
| `static/example/BPTI.fasta.orig` | False | **True** | **silent staleness** |

The negations prefix-match too much (loud), and the literal positives prefix-match
too much (silent — the guard would report a file as covered that GitHub does not
cover). Pre-existing behaviour, not a regression from this PR, but it is exactly
the failure class the module exists to catch and it is the cheapest gap to close:
one assertion, e.g. `assert not _to_regex("static/example/1HEW.pdb").match("static/example/1HEW.pdb.bak")`.

**M11 — `/**` matching the bare directory is unguarded.** `(?:/.*)?` → `/.*`, so
`tools/**` stops matching the string `tools`. All 30 green. GitHub's table is
silent here (`docs/**` lists `docs/README.md` and `docs/mona/octocat.txt`, never
bare `docs`), so no conformance row could catch it, and round 2 already recorded
this as something it could not verify. Informational, not a defect — a changed-file
path is never a bare directory.

---

## Criterion 3 — test data against the source. PASS

I re-fetched the source myself rather than trusting the report:

```
$ curl -sS -o gh_workflow_syntax.md \
    https://raw.githubusercontent.com/github/docs/main/content/actions/reference/workflows-and-actions/workflow-syntax.md
HTTP=200 BYTES=72444
sha256 8c70a3739211406cdd9e0d8beb9e45227d057fce6a996650c10242931d462015
$ grep -n "Patterns to match file paths" gh_workflow_syntax.md
1554:### Patterns to match file paths
```

Then parsed the raw markdown table and diffed it against `_DOC_ROWS`
programmatically. **All 12 entries match the published row exactly** — pattern
and every example, in order. Zero transcription errors, zero fabricated rows,
zero `_DOC_ROWS` entries without a source row.

The rows with three expected matches are all correct, including the one a
summarizing fetch gets wrong. My first (LLM-summarized) fetch returned
`'**/migrate-*.sql'` with only two examples and I flagged it as a suspected
transcription error; the **raw** source has three, and the test's third entry
`db/sept/migrate-v1.sql` is genuine. Had I stopped at the summarized fetch I
would have filed a false defect. Recording that as a method note.

Both negation rows, including the *does-not-match* entries:

| source | in the test |
|---|---|
| `'*.md'` + `'!README.md'` → matches `hello.md`; **does not match** `README.md`, `docs/hello.md` | all three asserted, both negatives |
| `'*.md'` + `'!README.md'` + `README*` → `hello.md`, `README.md`, `README.doc` | all three asserted |

**Row coverage is complete: 15 of 15.** 12 in `_DOC_ROWS`, `'*.jsx?'` in
`test_documented_rows_using_refused_constructs_are_refused`, and both negation
rows in `test_matches_githubs_documented_negation_examples`. Nothing is omitted
silently.

Cheat sheet also verified independently: exactly six bullets (`*`, `**`, `?`,
`+`, `[]`, `!`), and `grep -inc 'escape\|backslash' gh_workflow_syntax.md`
returns **0** over all 72,444 bytes. B3's stated grep is literally reproducible.

---

## Criterion 4 — does the suite test the right population? PASS, with a nuance

**Same entry point, proven by mutation rather than by reading imports.** The
conformance rows call `_to_regex` directly; the negation test calls
`_triggers_deploy`; the live property test calls `_triggers_deploy`. Mutating
`_to_regex` (M10) and mutating `_triggers_deploy` (M6) both turn
`test_every_copied_file_is_on_a_deploy_trigger[af2|colabfold|esmfold|mpnn]` red.
There is no parallel implementation; the conformance test cannot certify
something adjacent to what ships.

I also reproduced the PR body's non-vacuity claim exactly — strip the three
`static/example` entries from the workflow:

```
FAILED ...::test_every_copied_file_is_on_a_deploy_trigger[af2]
FAILED ...::test_every_copied_file_is_on_a_deploy_trigger[colabfold]
FAILED ...::test_every_copied_file_is_on_a_deploy_trigger[esmfold]
FAILED ...::test_every_copied_file_is_on_a_deploy_trigger[mpnn]
FAILED tests/test_deploy_paths_exclusions.py::test_workflow_still_carries_every_trigger_entry_in_order
5 failed, 66 passed
```

Red on exactly the four named apps plus the renamed pin. Restored; `git status`
clean.

**The nuance, which the PR does not state.** On the current tree the live property
test resolves to four files, all literal:

```
af2       -> ['static/example/BPTI.fasta']
colabfold -> ['static/example/ubiquitin.fasta']
esmfold   -> ['static/example/ubiquitin.fasta']
mpnn      -> ['static/example/1HEW.pdb']
boltz2, iggm, opendde, proteina -> []
```

Their verdict is decided entirely by the three literal `static/example/*` trigger
entries. That is why M2, M3 and M4 — real breakage of `**` semantics — leave the
live property test green while lighting up the conformance rows. So the `**`
machinery the whole conformance suite is about is currently exercised on the live
side only by the hand-written asserts in `test_pattern_translation_distinguishes_star_from_globstar`
(`tools/af2/modal_app.py`, `tools/af2/meta.py`, `tools/af2/example/...`). The
conformance table is protecting a path that matters for a *future* Dockerfile that
COPYs something under `tools/`. That is legitimate — it is a guard, not a
regression test — but "the conformance test and the live guard exercise the same
translator" is true at the code level while the live guard's current verdict does
not depend on the semantics being conformed to.

---

## Criterion 5 — did deleting `positive_globstars` lose anything? PASS. No.

Nothing else referenced it. `grep -rn "positive_globstars"` across the repo
outside `docs/qc/` returns nothing.

**And the property it was reaching for is still guarded, more broadly than it ever
managed.** `tests/test_deploy_paths_exclusions.py` pins `on.push.paths` to an
exact list (`assert actual == _EXPECTED_PATHS`, an 8-element literal). Injecting a
positive globstar entry:

```
$ # inject "  - '**/docs/**'" into on.push.paths
FAILED tests/test_deploy_paths_exclusions.py::test_workflow_still_carries_every_trigger_entry_in_order
1 failed, 70 passed
```

Because it is exact list equality, it fires on **any** trigger-list change,
including all four shapes that walked past `positive_globstars`' literal-substring
`"**/"` filter (`static/**`, `docs/**`, bare `**`, `**.py`). The deleted assertion
was strictly subsumed. Round 2's B2 was correct on both of its grounds, and I
confirmed both independently: the rationale was false (the reading is documented)
and the reach was narrower than its own message claimed.

Direct answer to "is there now any guard against someone adding a positive `**`
entry that over-includes": **yes for detection, no for evaluation, and that is
acceptable.** Any added entry goes red and forces a deliberate `_EXPECTED_PATHS`
edit under review. Nothing then evaluates whether the new entry over-includes —
but nothing did before either, and the specific hazard `positive_globstars` was
built for (a permissive `**/` reading that might be wrong) no longer exists now
that the reading is confirmed against GitHub's published table and pinned row by
row. Nothing needs to replace it.

---

## Criterion 6 — is the restored docstring accurate? FAIL

**The citation resolves.** Following it exactly as written — repo `github/docs`,
path `content/actions/reference/workflows-and-actions/workflow-syntax.md`,
section "Patterns to match file paths" — lands on the table at line 1554. A
reader can re-check in one step. Good.

**Every GitHub-behaviour claim in the new comment and docstring checks out**
against the raw source: the six specials, the verbatim `?` / `+` / `[]`
definitions, the zero-match `escape|backslash` grep, the 72 KB figure (72,444
bytes), the cheat-sheet quote "Matches zero or more of any character", the five
enumerated zero-segment rows and their examples, and `'*.jsx?'` matching both
`page.js` and `page.jsx`. I also verified the workflow comment's claim that the
matrix has no per-path routing (nine flat entries, no `if:` gate on paths, no
changed-files action) and the docstring's "a filter that has never used one" —
across the workflow's entire history the trigger has only ever contained
`tools/**`, the three `!tools/**/...` negations, `static/`, `static/example/**`,
the three `static/example/<file>` entries and the workflow path. No `?`, `+` or
`[]`, ever.

### D-R3-1 — MEDIUM — the `\` escape claim survives in the same file that calls it invented

`tests/test_deploy_trigger_covers_dockerfile_copies.py`, line 263, inside
`test_to_regex_refuses_rather_than_guesses`:

> ``foo\*bar`` is GitHub's escape: the literal string ``foo*bar``.

Lines 86–94 of the same file, added by `616c828`:

> ``\`` is refused for the opposite reason: not because it means something this
> translator cannot model, but because GitHub documents NO meaning for it at all.
> … (A previous revision of this file asserted ``\`` WAS a documented escape. It
> is not. That claim was invented; this comment is what the source actually says.)

Provenance, pinned:

```
$ git blame -L 260,268 -- tests/test_deploy_trigger_covers_dockerfile_copies.py
ebe27fd6 (qc 263)     ``foo\\*bar`` is GitHub's escape: the literal string ``foo*bar``. It is the
$ git diff 616c828^..616c828 -- tests/... | grep -c "GitHub.s escape"
0
```

Introduced by `ebe27fd` — the commit round 2 failed for precisely this. **Not
touched by `616c828`**, the remedy. It is live at `dcf6d86`.

This is round 2's **R2-2, incompletely remedied**. B3 fixed the module-level
comment and missed the test docstring. Meanwhile `616c828`'s commit message says
"B3 keeps the `\` refusal and fixes its justification: refused because GitHub
documents NO meaning for it", and the PR body says "So `\` is refused because
GitHub gives it **no** documented meaning … not because it is a known escape."
Both report a completed correction; the file still carries the uncorrected half.

Why it blocks despite being prose-only: the whole thesis of this PR is that a
green guard whose docstring misstates what it pins is a defect worth clearing off
trunk. This is that defect, in the PR that exists to remove it, in the file it
exists to fix, and — unlike a commit-message error — **a docstring reaches main
through a squash merge**. The remedy is one sentence, matching the language
already at lines 86–94: `\` is refused because GitHub documents no meaning for it,
and the `re.escape` rendering would match nothing on a POSIX path.

### D-R3-5 — COSMETIC — "Five rows" is six

`_to_regex`'s docstring: "Five rows of that table show a `**` consuming zero
segments." Six do — the five it enumerates plus `'**/*src/**'` → `my-src/code/js/app.js`,
where the leading `**/` also consumes zero. Undercount, safe direction, all five
enumerated examples correct.

### D-R3-6 — COSMETIC — `[]` is not a quantifier

Line ~78: "Three of those are regex quantifiers rather than wildcards — `?` … `+`
… `[]` is a single alphanumeric from the listed set or range." `[]` is a character
class, not a quantifier. The `[]` definition quoted is verbatim correct; only the
collective noun is loose.

---

## Criterion 7 — merge commit `dcf6d86` for lost content. PASS

Recomputed mechanically:

```
$ git merge-tree --write-tree 616c828 b73bfad
10b97641436d04cdfed3fbe9cf95a3417a46767b
$ git rev-parse dcf6d86^{tree}
10b97641436d04cdfed3fbe9cf95a3417a46767b
$ git diff <computed> dcf6d86^{tree}
(empty)
```

**Byte-identical.** The committed merge is exactly what git computes — no manual
edit, no hunk dropped, nothing to account for.

Stronger still, the two sides touch **disjoint** file sets, so there was no
contested file for a clean automerge to silently mis-resolve:

| side | files |
|---|---|
| vs `616c828` (what #161 brought in) | `docs/qc/scout-dssp-fallback-measurement.md`, `docs/qc/scout-interface-competition-round1.md`, `requirements.txt`, `scout/pipeline.py`, `scout/scoring.py`, `tests/test_scout_ss_assignment.py` |
| vs `b73bfad` (what the branch adds) | `.github/workflows/deploy-modal.yml`, both `docs/qc/deploy-trigger-*` reports, `tests/test_deploy_paths_exclusions.py`, `tests/test_deploy_trigger_covers_dockerfile_copies.py` |

The repo's recorded worktree-base-drift incident cannot have recurred here.

---

## Criterion 8 — new overclaiming across the delta. PASS except D-R3-1

Everything the worker claimed that I could test, I tested:

| claim | verdict |
|---|---|
| base `b73bfad` = 5379/21 | reproduced exactly |
| head `dcf6d86` = 5393/21 | reproduced exactly |
| guard module 16 → 30, delta +14 reconciles | reproduced, name by name |
| "Every documented row PASSES … including `'**.js'` → `index.js`, `js/index.js`, `src/js/app.js`" | true; the row is green at M0 and catches M2 and M4 |
| "No translator defect was found" | true — I found none either |
| non-vacuity: stripping the three entries → red on af2/colabfold/esmfold/mpnn + the renamed pin | reproduced exactly, 5 failures, no more |
| R2-8: `.get` applied to the second module | verified — the `paths`→`paths-ignore` swap now reports with its own message, no `KeyError`, no collection error |
| R2-3: "the old identifier appears in three QC reports" | exactly three files, correct |
| R2-4: renamed test's docstring spells out the old name | present; a grep for the old name now lands in `tests/test_deploy_paths_exclusions.py` |
| R2-6: the three/four metacharacter miscount | resolved by restructuring; counts now agree |
| workflow comment: "any entry here that fires redeploys all nine apps" | true — flat nine-app matrix, no path routing, no `if:` gate |
| "a filter that has never used one" (`?`/`+`/`[]`) | true across the workflow's whole history |
| "`\` is refused because GitHub documents no meaning for it" | **contradicted by line 263 of the same file — D-R3-1** |

Commit messages: `616c828` correctly self-corrects `ebe27fd`'s stale 5350/20
figures and states the re-measured pair. `ebe27fd`'s own message still carries the
stale numbers and still asserts the `\`-is-a-documented-escape claim, but this
repo **squash-merges** (every commit on main has one parent), so intermediate
commit-message errors never reach main. Not a blocker, per the brief. D-R3-1 is
different in kind precisely because a docstring is code and does reach main.

I found no fabricated defect and no unconditionally-succeeding check in this
delta.

---

## Criterion 9 — what still has NO guard

1. **GitHub's real filter engine is never executed here.** Conformance is against
   published worked examples, not the implementation. The PR body states this
   plainly in "Limits carried forward" — credit for that. It is a real and
   irreducible limit in this environment: documentation and engine can diverge,
   and for `\` specifically the engine's behaviour is genuinely unknown.
2. **Anchoring.** No test derives `^…$` from the source's own "Path patterns must
   match the whole path, and start from the repository's root." M5a proves it —
   dropping `$` leaves 30/30 green and has a silent-staleness direction. Cheapest
   real gap to close.
3. **Over-matching generally.** The 12 conformance rows cannot detect it (M9). All
   over-matching protection rests on one hand-written test and the two documented
   negation rows.
4. **`_DOC_ROWS` drift.** Nothing pins the transcription to the upstream file. If
   GitHub edits the table, `_DOC_ROWS` silently becomes a record of what the docs
   used to say, still green. Inherent to transcription; worth knowing.
5. **Bare-directory `/**`** (M11) — documented silence, unguarded, harmless today.
6. **Modal was never invoked**; no image was built. That the four images contain
   the fixtures is read off `COPY` lines via Modal's own parser.
7. **That a real `static/example/*` push fires the workflow** is observable only
   on a push to `main` — and this repo has previously seen GitHub drop push events
   entirely.
8. **That the redeployed image is what prod serves** — `/readyz` exposes no build
   SHA. Long-standing open item, unchanged.

---

## Were rounds 1 and 2 correct?

**Round 1: mostly right, one significant error.** Its D1, D3, D4, D5, D6 stand.
Its **D2 was wrong** — it called the documented `**/` zero-segment reading
"unverified", and the fix it prompted made the file less accurate. I confirmed
round 1's error independently from the raw source. Its stated cause is also
right: the rendered docs page behaves differently from the markdown source.

**Round 2: correct on everything I could re-test.** R2-1 and R2-2 both verified
independently — the table exists with the rows it names, and `escape|backslash`
returns zero hits over the whole file. R2-3 (three files, not one), R2-6, R2-7
(the substring-`"**/"` evasion) and R2-8 (the second module's bare `KeyError`) all
check out. Round 2 was right to fail `ebe27fd`.

Round 2's own remedy is where the miss is: R2-2 was fixed in one of the two places
the claim lives. Which is the round-3 lesson — the fix round did not introduce a
*new* false certification this time, it left half of an old one standing while
reporting it closed.

---

## Defects

| id | severity | blocks merge? | summary |
|---|---|---|---|
| **D-R3-1** | **MEDIUM** | **yes** | `tests/test_deploy_trigger_covers_dockerfile_copies.py:263` still asserts ``foo\*bar`` "is GitHub's escape", the claim lines 86–94 of the same file call invented. Round 2's R2-2 remedied in the module comment only. Commit message and PR body both report the correction complete. One-sentence fix. |
| D-R3-2 | LOW | no | The 12 conformance rows detect under-matching only. `re.compile(".*")` passes all twelve (M9). Three rows (`*`, `*.js`, `docs/*`) catch nothing short of total inertness. The docstring's "pins every row of the table" reads stronger than it is; worth one sentence saying the table supplies matches, not non-matches. |
| D-R3-3 | LOW | no | Trailing `$` anchor unguarded — M5a survives all 30 tests and has a silent-staleness direction (`static/example/1HEW.pdb.bak` reads as covered). Pre-existing, not a regression. One assertion closes it. |
| D-R3-4 | INFO | no | `/**` matching the bare directory unguarded (M11). GitHub's table is silent, so no conformance row could cover it. Round 2 already recorded it. |
| D-R3-5 | COSMETIC | no | "Five rows … show a `**` consuming zero segments" — six do. Undercount, safe direction. |
| D-R3-6 | COSMETIC | no | `[]` grouped under "regex quantifiers"; it is a character class. Quoted definition is verbatim correct. |

**Recommendation: fix D-R3-1 before merging.** It is one sentence and it is the
exact defect this PR exists to remove. D-R3-2 and D-R3-3 are worth taking in the
same pass — one sentence and one assertion — but neither blocks. D-R3-5/6 at
whim.

---

## What I could NOT verify — plainly, as unverified

1. **GitHub's actual filter engine.** I verified its *documentation*, fetched raw
   from `github/docs@main`. I did not run the engine. I make no claim about
   whether it treats `\` as an escape — that is precisely my objection to the
   docstring that does.
2. **"Rendered docs.github.com drops this table."** Asserted in the `_to_regex`
   docstring, the commit message and the PR body. My fetch of the rendered page
   returned a hedged negative ("the document cuts off before that section"),
   which I do not consider proof either way. **Unverified.** It is an aside about
   how round 1 erred, not a claim about GitHub's filter behaviour, so it does not
   affect the fix — but it is stated more confidently than I could confirm.
3. **That `docs/qc/deploy-trigger-guard-fixes-round2.md` was committed
   "unmodified."** No pre-commit original exists in git to diff against, and I did
   not enter the round-2 worktree. Not checkable from this tree.
4. **That a push touching `static/example/1HEW.pdb` queues the workflow.**
   Observable only on a real push to `main`.
5. **That Modal's build cache invalidates and the image is rebuilt** — `modal` was
   deliberately never invoked.
6. **That the redeployed image is what prod serves** — `/readyz` exposes no build
   SHA.
7. **The two known-flaky node tests.** Both full runs were green with zero
   `FAILED`/`ERROR` lines, so no isolation rerun was warranted; I did not probe
   their flakiness.
8. **Mutation coverage is not exhaustive.** Fourteen mutations is a sample, not a
   proof. Two survived; there may be others I did not think to write.

---

## Method notes

- Worktrees: `scratchpad/qc160r3` (detached at `dcf6d86`) and a scratch
  `scratchpad/qc160r3-base` (detached at `b73bfad`, removed after use). The main
  working tree was never touched; no `checkout`/`reset`/`stash`/`add -A`/`commit -a`
  was run in it. Nothing committed, pushed or merged. `modal` never invoked.
- All experiments restored. `git status` clean apart from this report; `git diff HEAD`
  empty. The working-tree files hash differently from the HEAD blobs only because
  `core.autocrlf=true` — verified byte-identical after newline normalisation.
- **Method warning worth carrying forward:** my first fetch of GitHub's table went
  through a summarizing fetch, which silently dropped the third example from the
  `'**/migrate-*.sql'` row. Acting on it would have produced a confident, wrong
  transcription-error defect. Conformance data must be diffed against the **raw**
  source. `curl` the raw file.
