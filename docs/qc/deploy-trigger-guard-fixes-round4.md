# QC round 4 — deploy-trigger guard fixes (PR #160)

**SHA reviewed: `f10a643c686777a2c635ddd9b45e6643b58a7e27`**
Base: `41a0933` (`origin/main` at review time).
Worktree: `scratchpad/qc160r4`, detached at `f10a643`.
Independent round — I did not build this and did not write rounds 1, 2 or 3.

## Verdict

**MERGE.**

The six changed files are sound. Both baselines measured first-hand, zero
failures on either side, `+20` reconciling exactly with the guard module's
collected count, both merges recompute byte-identically, both live-guard reverts
fire with messages naming the right mechanism and the right failure direction,
and all three headline mutation claims (M5a closed, M9 uncaught by conformance
rows, M11 surviving) are confirmed by my own mutations.

Every claim about GitHub's behaviour **inside the six changed files** is
supported by primary source that I fetched raw and read raw — including all
twelve transcribed table rows, verified mechanically character-for-character.

I did find a fourth false claim, as warned. **It is in the PR description, not in
the code**: PR #160's `## D1` section still opens by asserting `` `\` is
GitHub's documented escape character`` as fact — the exact invention rounds 2
and 3 spent two rounds removing from the source — and then retracts it three
paragraphs later in the same section. That is a description edit, no commit and
no re-run. It does not block the code.

Four code-level findings remain, all of the shape *"stated reason wrong,
conclusion right"*. None can hide a stale image, none contradicts primary
source, and none changes behaviour. They are follow-ups, listed below.

---

## Criterion 1 — both baselines, measured by me. **PASS**

Repo venv by absolute path, run from each worktree root with no path argument,
redirected to a file (never piped through `tail`).

```
$ git worktree add --detach .../scratchpad/qc160r4base 41a0933
HEAD is now at 41a0933 fix(wallet): claim the auto-reload dispatch before charging the card (#163)

$ cd .../qc160r4base && venv/Scripts/python.exe -m pytest -q  > base_41a0933.txt
5391 passed, 21 skipped in 190.14s (0:03:10)
BASE_EXIT=0

$ cd .../qc160r4     && venv/Scripts/python.exe -m pytest -q  > head_f10a643.txt
5411 passed, 21 skipped in 197.11s (0:03:17)
HEAD_EXIT=0
```

| tree | result |
|---|---|
| base `41a0933` | **5391 passed, 21 skipped**, exit 0 |
| head `f10a643` | **5411 passed, 21 skipped**, exit 0 |
| delta | **+20**, zero failures either side |

No flakes; neither run needed a re-run in isolation.

### Collected-count reconciliation

```
$ pytest --collect-only -q tests/test_deploy_trigger_covers_dockerfile_copies.py
  head f10a643 : 36 tests collected
  base 41a0933 : 16 tests collected
$ pytest --collect-only -q tests/test_deploy_paths_exclusions.py
  head f10a643 : 41 tests collected
  base 41a0933 : 41 tests collected      (rename only — no count change)
```

Guard module `16 → 36 = +20`, exclusions module unchanged, suite delta `+20`.
**Reconciles exactly.** The +20 breaks down as the four new tests:

| new test | params | count |
|---|---|---|
| `test_matches_githubs_documented_filter_pattern_examples` | `_DOC_ROWS` | 12 |
| `test_rejects_what_githubs_documented_rules_exclude` | `_DOC_NON_MATCHES` | 6 |
| `test_matches_githubs_documented_negation_examples` | — | 1 |
| `test_documented_rows_using_refused_constructs_are_refused` | — | 1 |
| | | **20** |

Nothing else in the repo imports the guard module (grep found only two comment
references), so mutations to it are self-contained.

---

## Criterion 2 — the claim audit. **PASS for the six files; FAIL for the PR body**

### Sources, fetched raw

```
$ curl -sSL -o workflow-syntax.md https://raw.githubusercontent.com/github/docs/main/content/actions/reference/workflows-and-actions/workflow-syntax.md
$ wc -c workflow-syntax.md
72444 workflow-syntax.md
$ curl -sSL -o paths1.md https://raw.githubusercontent.com/github/docs/main/data/reusables/actions/workflows/triggering-a-workflow-paths1.md
$ wc -c paths1.md
1309 paths1.md
```

Heeding round 3's warning, I did **not** use a summarizing fetch. I dumped the
cheat-sheet section with `sed -n '1505,1585p'`, parsed the markdown table
programmatically, and counted rows:

```
TOTAL TABLE ROWS = 15
 1. ['*']                  ['README.md', 'server.rb']
 2. ['*.jsx?']             ['page.js', 'page.jsx']
 3. ['**']                 ['all/the/files.md']
 4. ['*.js']               ['app.js', 'index.js']
 5. ['**.js']              ['index.js', 'js/index.js', 'src/js/app.js']
 6. ['docs/*']             ['docs/README.md', 'docs/file.txt']
 7. ['docs/**']            ['docs/README.md', 'docs/mona/octocat.txt']
 8. ['docs/**/*.md']       ['docs/README.md', 'docs/mona/hello-world.md', 'docs/a/markdown/file.md']
 9. ['**/docs/**']         ['docs/hello.md', 'dir/docs/my-file.txt', 'space/docs/plan/space.doc']
10. ['**/README.md']       ['README.md', 'js/README.md']
11. ['**/*src/**']         ['a/src/app.js', 'my-src/code/js/app.js']
12. ['**/*-post.md']       ['my-post.md', 'path/their-post.md']
13. ['**/migrate-*.sql']   ['migrate-10909.sql', 'db/migrate-v1.0.sql', 'db/sept/migrate-v1.sql']
14. ['*.md','!README.md']  ['hello.md', 'README.md', 'docs/hello.md']  [has _Does not match_ column]
15. ['*.md','!README.md','README*'] ['hello.md', 'README.md', 'README.doc']
```

Then diffed `_DOC_ROWS` against those rows by AST:

```
_DOC_ROWS entries = 12
  ok '*' ... ok '**/migrate-*.sql'      (all twelve)
TRANSCRIPTION EXACT: True
Table rows NOT in _DOC_ROWS:
    ['*.jsx?']                          -> covered by test_documented_rows_using_refused_constructs_are_refused
    ['*.md','!README.md']               -> covered by test_matches_githubs_documented_negation_examples
    ['*.md','!README.md','README*']     -> covered by test_matches_githubs_documented_negation_examples
```

All twelve transcriptions are **character-exact**. The module as a whole covers
15/15 rows across three tests.

### The audit table

Status codes: **S** supported by primary source · **U** unsourced but not
contradicted · **X** contradicted / false.

| # | where | claim | primary source check | status |
|---|---|---|---|---|
| 1 | yml comment | "The matrix below has no per-path routing, so any entry here that fires redeploys all nine apps" | matrix has 9 apps, the only `if:` in the file is `if: always()` on the log upload; `paths1/2.md`: "If at least one path matches ... the **workflow** runs" | **S** |
| 2 | yml comment | "'Four' is how many images the fixture CHANGES ... the other five just rebuild identically" | `grep -rn 'static/example' tools/ --include=Dockerfile.modal` → af2, colabfold, esmfold, mpnn (4); 9−4=5 | **S** |
| 3 | excl. comment | "`paths-ignore`, which GitHub allows only one of" | paths1.md: "You cannot use both the `paths` and `paths-ignore` filters for the same event in a workflow." | **S** |
| 4 | excl. assert msg | "GitHub applies `paths` later-wins, so a negation moved above `tools/**` is re-included and stops excluding anything" | paths1.md NOTE + row 15 | **S** |
| 5 | excl. assert msg | "Four Dockerfiles COPY those fixtures ... `static/` is under no other entry here" | trigger list is `tools/**` + 3 negations + 3 static literals + the yml itself | **S** |
| 6 | excl. assert msg | "no `tools/**` negation can match a `static/` path, and each is a literal file path so it re-includes nothing" | all three negations start `!tools/`; the three positives are literal paths | **S** |
| 7 | mod docstring | "`test_matches_githubs_documented_filter_pattern_examples` replays **every row** of GitHub's published pattern table" | it replays 12 of 15; the other 3 are replayed by two sibling tests | **U** (imprecise — D-R4-5) |
| 8 | mod docstring | "those rows publish only MATCHES, never non-matches" | true of the 12 in `_DOC_ROWS`; the full table's row 14 does have a "_Does not match_" column | **S** as to `_DOC_ROWS` |
| 9 | mod docstring | M9 (`re.compile(".*")`) "still passes all twelve rows" | re-run by me — mutation MG, 0/12 rows fired | **S** |
| 10 | mod docstring | "Three rows (`*`, `*.js`, `docs/*`) catch nothing short of total inertness" | across my 17 mutations those three fired **only** under MM (translator matches nothing); `docs/**` is correctly *not* in the set (it fires under MD2) | **S — exactly right** |
| 11 | mod docstring | over-matching protection lives in `..._rules_exclude`, `..._star_from_globstar`, `..._negation_examples` | all three catch over-matching mutations (MA, MG); zero conformance rows do | **S** (criterion 5) |
| 12 | mod docstring | "This is what pins the regex **anchors**" (plural) | mutation ME (drop `^`) passes all 36 | **X — D-R4-2** |
| 13 | `_TRIGGER_PATHS` comment | `["paths"]` "raises a bare `KeyError: 'paths'` here and kills the whole module before any test can report why" | experiment C2 reproduced it verbatim: `KeyError: 'paths'` / `Interrupted: 1 error during collection` | **S** |
| 14 | metachars comment | cheat sheet "enumerates exactly six specials: `*`, `**`, `?`, `+`, `[]`, `!`" | raw cheat sheet has exactly those six bullets | **S** |
| 15 | metachars comment | `?` = "zero or one of the preceding character"; `+` = "one or more of the preceding character"; `[]` = one alphanumeric from the listed set/range | verbatim in the cheat sheet | **S** |
| 16 | metachars comment | "GitHub documents NO meaning for `\` ... `grep -i 'escape\|backslash'` over the whole 72 KB of workflow-syntax.md returns nothing" | `grep` exit 1, zero hits; file is 72 444 bytes | **S** |
| 17 | metachars comment + test docstring | "`re.escape` would turn `foo\*bar` into `^foo\\[^/]*bar$`, which matches only a path containing a real backslash" | computed: regex `'^foo\\\\[^/]*bar$'`; `foo*bar` False, `fooXbar` False, `foo\Xbar` True | **S** |
| 18 | `_to_regex` docstring | "`**/` matches ... INCLUDING zero ... This is GitHub's DOCUMENTED behaviour" | rows 8, 9, 10, 11, 12, 13 | **S** |
| 19 | `_to_regex` docstring | "**Five** rows of that table show a `**` consuming zero segments" | I count **six** — the five listed plus row 11 `'**/*src/**'` → `my-src/code/js/app.js` | **U** (undercount, conservative — D-R4-6) |
| 20 | `_to_regex` docstring | the five worked examples quoted | all five exact against rows 8, 9, 10, 12, 13 | **S** |
| 21 | `_to_regex` docstring | cheat-sheet prose says only `**` "Matches zero or more of any character", which read literally makes `**/README.md` need a leading `/` | verbatim; the inference is sound and does contradict row 10 | **S** |
| 22 | `_to_regex` docstring | "checked directly, docs.github.com's rendered page DOES carry the full table, root-`README.md` row included" | I fetched `https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax` (2 020 277 bytes) and extracted the row: `'**/README.md'` … `README.md` `js/README.md`. Also present: `migrate-10909`, `README.doc`, "Path patterns must match the whole path" ×2 | **S — the round-3 correction is correct** |
| 23 | `_to_regex` docstring | "Why the table was missed is not recorded here, because it is not known" | records no theory. Confirmed: no causal claim survives anywhere in the delta | **S** |
| 24 | `_triggers_deploy` docstring | "Later-wins, which is GitHub's rule" | paths1.md NOTE; row 15 | **S** |
| 25 | paths-ignore floor docstring | "GitHub forbids using both in one filter" | paths1.md | **S** |
| 26 | paths-ignore floor docstring | "this whole assertion **was dead code**: the sole state it fires on is the sole state that raises `KeyError` at import" | experiment C3: with BOTH keys present and the old `["paths"]` subscript, the floor fires normally (1 failed, no collection error) | **X — D-R4-3** |
| 27 | `_DOC_ROWS` comment | "**Every row** of GitHub's 'Patterns to match file paths' table, transcribed" | 12 of 15 | **U** (imprecise — D-R4-5) |
| 28 | `_DOC_ROWS` comment | "`**` NOT followed by `/` still crosses directory boundaries — three depths in one row" | row 5: `index.js`, `js/index.js`, `src/js/app.js` | **S** |
| 29 | `_DOC_NON_MATCHES` header | RULE W quote "Path patterns must match the whole path, and start from the repository's root." — "immediately above the table" | line 1556; table starts 1558 | **S, verbatim, location right** |
| 30 | `_DOC_NON_MATCHES` header | RULE S quote "The `*` wildcard matches any character, but does not match slash (`/`)." — "the `'*'` row of that table" | row 1's Description cell, first sentence | **S, verbatim, location right** |
| 31 | `_DOC_NON_MATCHES` header | "Rule W is what pins **BOTH** anchors" | mutation ME passes all 36 | **X — D-R4-2** |
| 32 | `_DOC_NON_MATCHES` header | "a dropped `$` ... `static/example/1HEW.pdb.bak` would read as covered" | without `$`, `^static/example/1HEW\.pdb` matches `...pdb.bak` | **S** |
| 33 | entry 3 inline | "RULE W — 'start from the repository's root'. **Kills a dropped `^`**." | mutation ME passes all 36 | **X — D-R4-2** |
| 34 | entry 4 inline | "the table says so by contrast: `'**.js'` lists `js/index.js` as a match while `'*.js'` lists only `app.js` and `index.js`" | rows 4 and 5 | **S** |
| 35 | entry 6 inline | "contrast again: `docs/**` lists `docs/mona/octocat.txt`, `docs/*` does not" | rows 6 and 7 | **S** |
| 36 | `/**` not-asserted note | "GitHub's table lists only `docs/README.md` and `docs/mona/octocat.txt` for `docs/**` ... no documented sentence settles the bare-directory case" | row 7 exact; no sentence in the file settles it | **S** |
| 37 | `/**` not-asserted note | "every path this module ever evaluates comes from `git ls-files` or from resolving a Dockerfile COPY against that same list" | the same commit adds ~35 hardcoded example paths that `_to_regex(...).match()` / `_triggers_deploy()` evaluate | **X (premise) / S (conclusion) — D-R4-4** |
| 38 | `/**` not-asserted note | "both yield FILES ... A bare directory path is never tested" | `git ls-files \| while read f; do [ -d "$f" ] && echo; done \| wc -l` → **0**; and all ~35 hardcoded paths are files | **S** |
| 39 | negation test | "GitHub: 'Patterns are checked sequentially. A pattern that negates a previous pattern will re-include file paths.'" | row 15, verbatim | **S** |
| 40 | negation test | `'*.md'`+`'!README.md'` matches `hello.md`, not `README.md` / `docs/hello.md` | row 14 including its "_Does not match_" column | **S** |
| 41 | refused-constructs test | "`'*.jsx?'` is in the same table, and GitHub defines its `?` as 'zero or one of the PRECEDING character' — which is why it matches BOTH `page.js` and `page.jsx`" | row 2 + cheat sheet | **S** |
| 42 | star_vs_globstar comment | the deleted assertion "selected on the literal substring `\"**/\"`, so `static/**`, `docs/**`, bare `**` and `**.js` all sailed past" | `git show ebe27fd:` line 297: `[p for p in _TRIGGER_PATHS if "**/" in p and not p.startswith("!")]` | **S** |
| 43 | test docstring parenthetical | "An earlier revision of this docstring called `\` 'GitHub's escape'. ... it outlived the comment fix" | `ebe27fd` line 247 and `616c828` line 263 both read ``foo\\*bar`` is GitHub's escape | **S** |
| 44 | `_to_regex` docstring | "An earlier QC round did not find this table and called the zero-segment reading unverified" | round-1 report line 173: "the `**/` zero-segment reading is asserted as fact but is unverified" | **S** |
| **45** | **PR #160 body, `## D1`** | **"`\` is GitHub's documented escape character"** and **"`foo\*bar` — GitHub's literal string `foo*bar`"** | contradicted by #16 above, and by the same section three paragraphs later | **X — D-R4-1** |
| 46 | PR body | "Both test modules compile clean under `-W error::SyntaxWarning`" | `python -W error::SyntaxWarning -m py_compile <both>` → exit 0 | **S** |
| 47 | PR body | "#159's body claimed six non-vacuity floors where there are eight (8 floors + 1 parametrized property check = 16 collected)" | base collected count is 16 | **S** |

**No live claim anywhere in the six changed files asserts `\` is a documented
escape.** `grep -in escape` over all three code files returns only the correct
"documents NO meaning" statements and the two historicising parentheticals.

---

## Criterion 3 — do the six `_DOC_NON_MATCHES` entries trace to their citations? **PASS**

| entry | cited rule | does it trace? |
|---|---|---|
| `*.js` !~ `app.js.map` | W | **Yes.** Whole-path: without `$` the pattern matches a *prefix* of the path. |
| `**/README.md` !~ `README.md.bak` | W | **Yes**, same, through the `**/` branch. (Wording nit: "does not license a **suffix** match" — the mechanism is a prefix match of the path. Reads coherently either way; cosmetic.) |
| `docs/*` !~ `src/docs/file.txt` | W | **Entry yes, stated mechanism no.** "start from the repository's root" genuinely excludes `src/docs/file.txt`, so the entry is a real documented non-match. But its comment says it "Kills a dropped `^`", and it does not — see D-R4-2. |
| `*.js` !~ `js/index.js` | S + contrast | **Yes, doubly.** Row 4's own Description states rule S *and* "Matches all `.js` files **at the root** of the repository". The claimed contrast against row 5 also holds (`'**.js'` lists `js/index.js`; `'*.js'` lists only `app.js`, `index.js`). |
| `*` !~ `docs/README.md` | S | **Yes, directly** — row 1 states rule S verbatim. |
| `docs/*` !~ `docs/mona/octocat.txt` | S + contrast | **Yes, doubly.** Row 6's Description says "All files within the root of the `docs` directory **only**". The claimed contrast against row 7 also holds. |

**Both claimed "table's own internal contrasts" hold**, and — importantly —
neither entry *depends* on the contrast: each also has a direct quoted-rule
citation that stands alone. The inference-from-absence is decorative, not
load-bearing. No entry needs a different justification.

---

## Criterion 4 — mutation set, built independently. **PASS**

Seventeen mutations, applied one at a time to
`tests/test_deploy_trigger_covers_dockerfile_copies.py`, each followed by
`pytest -q -p no:randomly` on that module, then restored. Driver:
`scratchpad/mutate.py` + `mutate2.py`. Baseline: **36 passed**.

"rows" = params of `test_matches_githubs_documented_filter_pattern_examples`;
"non-match" = params of `test_rejects_what_githubs_documented_rules_exclude`.

| # | mutation | rows caught | other tests caught | verdict |
|---|---|---|---|---|
| MA | `*` → `.*` (crosses `/`) | **0 / 12** | non-match ×3, star_vs_globstar, negation | caught (5 failed) |
| MB | `**/` → `.*/` (needs ≥1 leading segment) | 6 / 12 | star_vs_globstar | caught (7 failed) |
| MB2 | `**/` → `(?:[^/]*/)?` (≤1 leading segment) | 2 / 12 | star_vs_globstar | caught (3 failed) |
| MC | bare `**` → `[^/]*` (acts like `*`) | 2 / 12 (`**`, `**.js`) | — | caught (2 failed) |
| **MD (M11)** | trailing `/**` → `/.*` (**requires a child**) | 0 / 12 | *(none)* | **SURVIVES — 36 passed** |
| MD2 | trailing `/**` → `(?:/[^/]*)?` (one level only) | 3 / 12 (incl. `docs/**`) | star_vs_globstar | caught (4 failed) |
| **ME** | **drop leading `^` anchor** | 0 / 12 | *(none)* | **SURVIVES — 36 passed** |
| **MF (M5a)** | **drop trailing `$` anchor** | 0 / 12 | **non-match ×4** | **caught (4 failed)** |
| **MG (M9)** | `_to_regex` → `re.compile(".*")` | **0 / 12 — all twelve GREEN** | non-match ×6, star_vs_globstar, negation | caught (8 failed), **by no conformance row** |
| MH | first-wins instead of later-wins | 0 / 12 | star_vs_globstar, negation | caught (2 failed) |
| MI | negation handling dropped (`negated = False`) | 0 / 12 | star_vs_globstar, negation | caught (2 failed) |
| MJ | `\` removed from `_UNMODELLED_METACHARS` | 0 / 12 | `test_to_regex_refuses_rather_than_guesses` | caught (1 failed) |
| MK | `.match` → `.search` in `_triggers_deploy` | 0 / 12 | *(none)* | survives — **inert**, `^` still anchors |
| MK2 | `.match` → `.search` in the non-match assert | 0 / 12 | *(none)* | survives — **inert**, `^` still anchors |
| ML | `*` → `[^/]+` (requires ≥1 char) | 1 / 12 (`**/*src/**`) | — | caught (1 failed) |
| MM | `_to_regex` → matches nothing (`(?!)`) | **12 / 12** | copied_file ×4, star_vs_globstar, negation | caught (18 failed) |
| MN | drop **both** anchors | 0 / 12 | non-match ×4 (the `$` doing the work) | caught (4 failed) |

### The three claims under test — all three confirmed

* **M5a closed.** `MF` fails **exactly four** tests, all of them
  `_DOC_NON_MATCHES` params:
  `[*!~docs/README.md]`, `[**/README.md!~README.md.bak]`, `[*.js!~app.js.map]`,
  `[docs/*!~docs/mona/octocat.txt]`. **"caught by four negatives" is exact.**
* **M9 still uncaught by any conformance row.** `MG` fails 8 tests; **not one**
  is a `test_matches_githubs_documented_filter_pattern_examples` param. Confirmed.
* **M11 still surviving.** `MD` → `36 passed`. Confirmed, and correctly
  documented as a deliberate non-assertion rather than pinned by an invented
  rule.

### The claim ME disproves

The delta says three times that Rule W / the `_DOC_NON_MATCHES` entries pin the
leading `^`. They do not. `re.Pattern.match()` anchors at position 0 regardless
of `^`, so dropping it is a semantic no-op:

```
'docs/[^/]*$'          .match('src/docs/file.txt')      -> False
'[^/]*\.js$'           .match('app.js.map')             -> False
'(?:.*/)?README\.md$'  .match('README.md.bak')          -> False
'[^/]*\.js$'           .match('js/index.js')            -> False
'[^/]*$'               .match('docs/README.md')         -> False
'docs/[^/]*$'          .match('docs/mona/octocat.txt')  -> False
```

The `^` is not useless — MK/MK2 show a `.match` → `.search` swap is **inert
while `^` is present**, and would over-match without it. So the accurate
statement is "the `^` is what makes a `.search` swap harmless", not "these
entries kill a dropped `^`". See D-R4-2.

---

## Criterion 5 — is the one-directionality docstring accurate? **PASS**

It names three tests as where over-matching protection lives. Verified by
mutation that each genuinely catches over-matching (MA = `*` crosses `/`,
MG = translator matches everything, MF/MN = anchors dropped):

| named test | over-matching mutations it caught |
|---|---|
| `test_rejects_what_githubs_documented_rules_exclude` | MA, MF, MG, MN |
| `test_pattern_translation_distinguishes_star_from_globstar` | MA, MG (also MB, MB2, MD2, MH, MI) |
| `test_matches_githubs_documented_negation_examples` | MA, MG (also MH, MI) |

And "none of them the rows above" is confirmed: the 12 conformance rows caught
**zero** of MA / MF / MG / MN. The docstring points at the right tests.

The one inaccuracy in this section is the clause "This is what pins the regex
anchors" — plural. D-R4-2.

---

## Criterion 6 — nothing regressed from the two merges. **PASS**

```
$ git rev-list --parents -n1 f10a643
f10a643 f036906 41a0933
$ git rev-list --parents -n1 f036906
f036906 eb50332 2f0637c

$ git merge-tree --write-tree f036906 41a0933
605438446ebd4058e27c8ec590ecbbe61a904820
$ git rev-parse f10a643^{tree}
605438446ebd4058e27c8ec590ecbbe61a904820          MATCH

$ git merge-tree --write-tree eb50332 2f0637c
efe8edb5705d25ce880382f3af2a976237f90a3f
$ git rev-parse f036906^{tree}
efe8edb5705d25ce880382f3af2a976237f90a3f          MATCH
```

Both merges recompute byte-identically to the committed trees — no manual edit
smuggled into either merge commit.

```
$ git diff --stat eb50332 f10a643 -- <the six PR files>
(empty)
```

The six files are byte-identical to the previously-reviewed `eb50332`. The
remaining `eb50332..f10a643` diff is 12 files, all of them `#162`/`#163`
content (`shared/idempotency.py`, `shared/wallet.py`, the wallet migration and
their tests). No lost hunk, no contested file.

```
$ git diff --name-status 41a0933 f10a643
M  .github/workflows/deploy-modal.yml
A  docs/qc/deploy-trigger-guard-fixes-round2.md
A  docs/qc/deploy-trigger-guard-fixes-round3.md
A  docs/qc/deploy-trigger-static-example-round1.md
M  tests/test_deploy_paths_exclusions.py
M  tests/test_deploy_trigger_covers_dockerfile_copies.py
```

Six files, exactly as stated.

---

## Criterion 7 — the live guard still works. **PASS**

Driver `scratchpad/reverts.py`; both files restored afterwards, `git status`
clean.

### Revert A — strip the three `static/example` entries from the workflow

```
FAILED ...test_every_copied_file_is_on_a_deploy_trigger[af2]
FAILED ...test_every_copied_file_is_on_a_deploy_trigger[colabfold]
FAILED ...test_every_copied_file_is_on_a_deploy_trigger[esmfold]
FAILED ...test_every_copied_file_is_on_a_deploy_trigger[mpnn]
FAILED ...test_workflow_still_carries_every_trigger_entry_in_order
5 failed, 32 passed in 0.78s
```

**Exactly af2, colabfold, esmfold, mpnn** — no more, no fewer — each naming its
own fixture:

```
AssertionError: af2/Dockerfile.modal bakes ['static/example/BPTI.fasta'] into its
image, but no on.push.paths entry in .github/workflows/deploy-modal.yml matches
those paths. Editing one of them would change this image without redeploying it,
and nothing would say so. Either add the path to the trigger, or stop COPYing it.
```

Right mechanism (`on.push.paths` in the named workflow), right direction (image
changes, **no deploy**, nothing red). The exact-list pin fires alongside it.

### Revert B — revert `_EXPECTED_PATHS` to the negations-only list

```
FAILED tests/test_deploy_paths_exclusions.py::test_workflow_still_carries_every_trigger_entry_in_order
1 failed in 0.69s
```

```
AssertionError: deploy-modal.yml push.paths is [...8 entries...], expected [...5 entries...].
  Which entry moved decides which way this breaks.
    NEGATIONS (`!tools/**/...`) — ... Wasteful, but loud and harmless.
    POSITIVES (`static/example/...`) — the OPPOSITE failure. ... prod keeps serving
    the old layer and nothing is red. Silent staleness, which is the worse direction.
```

Both directions named correctly and separately. The D3 fix does what it claims.

### Extra experiment — the `.get` floor (not requested, run anyway)

| experiment | result |
|---|---|
| C: `paths:` → `paths-ignore:`, with the `.get` fix | floor **reports**: `deploy-modal.yml grew a paths-ignore filter. _triggers_deploy models only the paths allowlist, so it now over-reports coverage.` (7 failed, 30 passed) |
| C2: same, with the OLD `["paths"]` subscript | `KeyError: 'paths'` at line 96, `Interrupted: 1 error during collection` — the claimed dead-code state, reproduced exactly |
| C3: **both** `paths` and `paths-ignore`, OLD subscript | floor **fires normally**, 1 failed, no collection error — which disproves "the sole state it fires on is the sole state that raises `KeyError`". See D-R4-3 |

---

## Defects

| id | severity | blocks? | where | finding |
|---|---|---|---|---|
| **D-R4-1** | MEDIUM | **not the code — fix the PR description** | PR #160 body, `## D1` | The section opens by asserting the invented claim as fact: "`\` is GitHub's documented escape character", and "`foo\*bar` — GitHub's literal string `foo*bar`". Contradicted by primary source (`grep -i 'escape\|backslash'` over workflow-syntax.md returns nothing; the cheat sheet's six specials exclude `\`) **and by the same section three paragraphs later** ("I wrote that `\` is GitHub's documented escape character. It is not documented at all"). This is the fourth-round instance of the pattern: the fix corrected the code and left the same invention standing in the artefact it did not re-read. Fix = edit two sentences in the description. No commit, no re-run. |
| **D-R4-2** | LOW–MEDIUM | no | 3 places in `tests/test_deploy_trigger_covers_dockerfile_copies.py` | "Kills a dropped `^`" is false. Mutation ME passes all 36 tests; `re.Pattern.match()` anchors at position 0 regardless of `^`, so **nothing in the module can detect a dropped `^`**. Three copies: module docstring "This is what pins the regex **anchors**"; `_DOC_NON_MATCHES` header "Rule W is what pins **BOTH** anchors"; entry-3 inline "**Kills a dropped `^`**". The entry itself is a correct documented non-match correctly cited to Rule W — only the mechanism claim is wrong. Accurate replacement: the `^` is what makes a `.match`→`.search` swap inert (MK/MK2), and nothing pins the `^` itself. Cannot hide a stale image: dropping `^` is a semantic no-op. |
| **D-R4-3** | LOW | no | `test_the_trigger_is_a_paths_allowlist_with_no_paths_ignore` docstring; repeated in PR body `## D4` | "this whole assertion **was dead code**: the sole state it fires on is the sole state that raises `KeyError` at import". Experiment C3 disproves it — with **both** `paths` and `paths-ignore` present (YAML-legal, GitHub-illegal) and the old subscript, the floor fires normally. The `.get` change is still correct and still load-bearing for the replace-state (C2 reproduces the collection error); only the absolutism is wrong. |
| **D-R4-4** | LOW | no | the `/**` not-asserted note; repeated in PR body "Limits carried forward" | "every path this module ever evaluates comes from `git ls-files` or from resolving a Dockerfile COPY against that same list" — false as of this very commit, which adds ~35 hardcoded example paths that `_to_regex(...).match()` and `_triggers_deploy()` evaluate. The **conclusion** survives (all of them are files; `git ls-files` yields 0 bare directories), so M11 really is inert; only the premise over-claims. |
| **D-R4-5** | INFO | no | module docstring; `_DOC_ROWS` comment | "replays **every row** of GitHub's published pattern table" / "**Every row** of ... transcribed". The table has **15** rows; `_DOC_ROWS` carries **12**. The other three are replayed by two sibling tests, so the module does cover 15/15 — but "every row" is attached to a list of 12. Mildly in tension with "those rows publish only MATCHES", since row 14 has an explicit "_Does not match_" column. |
| **D-R4-6** | INFO | no | `_to_regex` docstring | "**Five** rows of that table show a `**` consuming zero segments" — I count **six**; row 11 `'**/*src/**'` → `my-src/code/js/app.js` also consumes zero leading segments. Undercount, conservative direction. |
| **D-R4-7** | INFO | no | pre-existing, **not in this delta** | `.github/workflows/deploy-modal.yml` header still reads "one job per app so a single-app failure does not abort **the other three**". Stale since the matrix reached nine. Untouched by this PR; noting so it does not get attributed here later. |

Nothing found that hides a stale image. Nothing found that contradicts primary
source **inside the six changed files**.

---

## Merge recommendation

**MERGE.**

Reason: every criterion passes on measured evidence. Both baselines are mine
(`5391 / 5411`, `+20`, zero failures), the delta reconciles exactly with the
guard module's `16 → 36`, both merges recompute byte-identically, both live
reverts fire correctly, and the three headline mutation claims are confirmed
first-hand. Round 3's correction about the rendered docs page — the third
invention — is itself correct: I fetched the page and the root-`README.md` row
is there. All twelve transcribed rows are character-exact against raw source.

The one **false** statement I found is in the PR description, not in the six
files. It should be corrected before merge because it is a two-sentence
description edit at zero risk and it is the exact invention this PR exists to
kill — but it is not a reason to hold the code, and it is not a reason for a
fifth code round.

D-R4-2 through D-R4-6 are the repo's signature "stated reason wrong, conclusion
right" class and are worth one small follow-up commit; if that commit is made,
D-R4-2's three sentences and D-R4-1's two are the ones actually worth changing.
D-R4-5 and D-R4-6 are word-count pedantry and I would leave them.

---

## What I could NOT verify — stated plainly as unverified

1. **GitHub's real filter engine was never executed.** All conformance here is
   against GitHub's *published worked examples*, not a live run. The engine
   could diverge from its own documentation and nothing here would know.
2. **That editing `static/example/1HEW.pdb` on `main` genuinely fires a
   deploy** is only provable by a real push. Not attempted.
3. **`modal` was never invoked**, per instruction — no image was built and no
   A100 was billed. "Four images contain the fixtures" is read off `COPY` lines
   in four `Dockerfile.modal` files, not off a built layer.
4. **Whether a trailing `/**` matches a bare directory in GitHub's engine.**
   The docs are genuinely silent (verified), the code takes the permissive
   reading as an explicit choice, and M11 confirms nothing pins it. Inert today
   because every path evaluated is a file — but that is verified by inspection
   of the current test inputs, not enforced by any assertion.
5. **The delivered images on Modal.** `/readyz` still exposes no build SHA, so
   "the deployed image matches this commit" is not verifiable from here for any
   of the nine apps. Pre-existing, repo-wide.
6. **Mutation coverage is a sample.** Seventeen mutations is broader than round
   3's fourteen and includes six the earlier rounds did not run (ME, MD2, MK,
   MK2, ML, MM, MN), but it is not exhaustive. A surviving mutation I did not
   think to write would not appear here.
7. **The two flaky node tests** did not fire in either full run, so I did not
   exercise the isolation-rerun path.

---

*Round 4, independent. Worktrees `qc160r4` and `qc160r4base` restored and
`git status` clean apart from this report; `qc160r4base` removed.*
