# QC round 2 — PR #160, fixes to the deploy-trigger guard's round-1 findings

**SHA reviewed: `ebe27fd6a6e0fb6b20f0aaed051129e32d78b071`**
(single commit; parent `c749480` = `origin/main` at review time. PR #160,
`fix/deploy-trigger-guard-qc-findings` -> `main`.)

Reviewed by an independent QC agent that did not write this change and did not
write round 1's report. Detached worktree, nothing committed, nothing pushed,
`modal` never invoked.

## Verdict

**FAIL as submitted — two prose defects must be fixed first; the code is correct
and should merge once they are.**

Nothing here weakens a guard or changes deploy behaviour. `_to_regex`'s actual
translation is unchanged and correct, the three mechanical fixes (D1, D3, D4) all
work and all survive mutation, and both suites measure identically. The problem
is what the PR *says*.

I resolved, from GitHub's own documentation source, the question round 1 declared
unresolvable — and it resolves **against** round 1. `**/` matching zero leading
segments is GitHub's documented behaviour, stated with four explicit root-level
example matches. #159's original docstring was **correct**. #160's headline "fix"
rewrites that correct, documented statement into "a CONVENTION, not a verified
GitHub fact" that "nobody on this branch has confirmed", and pins the new claim
with a new assertion whose message repeats it.

In the same commit, the D1 comment does the mirror-image thing: it states as
documented GitHub fact that `\` is the filter syntax's escape character. GitHub's
filter-pattern documentation lists no such character; the word "escape" does not
occur in the page at all.

So a PR whose entire purpose is removing docstrings that misstate what the code
pins has **downgraded one true documented fact to "unverified" and upgraded one
undocumented assumption to "documented"**, in the same file, in the same commit.

| # | Criterion | Verdict |
|---|---|---|
| 1 | Both baselines + collected counts, measured by me | **PASS** |
| 2 | Each fix fixes its finding, by mutation | **PASS** for D1, D3, D4; **FAIL** for D2 |
| 3 | Attack D1's fix | **PASS** on the fix, **FAIL** on its stated justification (R2-2) |
| 4 | Attack D2's new assertion | (a) PASS (b) PASS (c) PASS (d) **EVADABLE** (e) **SOUND** — but premise moot, see R2-1 |
| 5 | D4 regression risk | **PASS**, with one observation and one incompleteness (R2-8) |
| 6 | D3's new message is TRUE | **PASS** — both claims verified by execution |
| 7 | New overclaiming | **FAIL** — three false/stale claims (R2-3, R2-4, R2-5) |
| 8 | Were round 1's findings right? | D3/D4/D5/D6 right; **D2 wrong**; D1 right on the defect, wrong on the reason |
| 9 | What still has no guard | reported below |

---

## Criterion 1 — baselines. PASS

Interpreter `C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe`,
`-m pytest -q` from each worktree root, no path argument, redirected to a file
(never piped through `tail`). Run **sequentially**, not concurrently, to avoid
loading the two known-flaky node tests. Baseline worktree at `c749480` created by
me, not reused.

```
c749480 (origin/main) : 5372 passed, 21 skipped in 308.56s (0:05:08)   exit 0
ebe27fd (this branch) : 5372 passed, 21 skipped in 218.12s (0:03:38)   exit 0
```

`grep -cE "^(FAILED|ERROR)"` over both logs: `0` and `0`. No flakes, no reruns
needed.

**Delta = 0.** Equal is the correct result and I checked *why* rather than
accepting it: the delta adds assertions inside existing test functions and one
entry to an existing `for` loop. It adds no test function and no `parametrize`
case.

Collected-count claim verified independently on both sides:

```
$ pytest --collect-only -q tests/test_deploy_trigger_covers_dockerfile_copies.py
c749480 : 16 tests collected in 0.44s
ebe27fd : 16 tests collected in 0.45s
```

The module has 9 `def test_` functions: 8 floors + 1 parametrized property check
over 8 Dockerfiles = 16 nodes. The PR body's arithmetic prose "8 floors + 1
parametrized property check = 16 collected" reads as 8+1=16; the underlying count
is right.

## Criterion 2 — does each fix actually fix its finding? PASS for D1/D3/D4, FAIL for D2

### D1 — PASS

Original defect reproduced on `c749480`:

```
_to_regex('foo\*bar') -> ^foo\\[^/]*bar$
  matches 'foo*bar'?  False
  matches 'fooXbar'?  False
```

Gone on `ebe27fd` — the pattern is refused:

```
NotImplementedError: deploy trigger pattern 'foo\\*bar' uses GitHub filter
metacharacter(s) '\\', which this translator does not model. In a negation that
would under-model the exclusion and make this guard report coverage that does not
exist. Teach _to_regex the construct before using it.
```

Fix mutated (`frozenset("?+[]\\")` -> `frozenset("?+[]")`), assertion fires:

```
E           Failed: DID NOT RAISE <class 'NotImplementedError'>
tests\test_deploy_trigger_covers_dockerfile_copies.py:254: Failed
1 failed, 15 deselected in 0.56s
```

The PR body's claim about that exact failure text is accurate. The fix is
exercised, not merely written.

### D3 — PASS

Realistic regression induced on both sides (drop `static/example/1HEW.pdb` from
the workflow).

`c749480` — the wrong-direction message round 1 flagged:

```
FAILED tests/test_deploy_paths_exclusions.py::test_workflow_still_carries_every_negation_in_order
... GitHub applies `paths` later-wins: a negation moved above `tools/**` is
re-included and stops excluding anything, and a negation dropped altogether
resumes redeploying nine GPU images on web-tier-only edits.
```

`ebe27fd` — renamed, and the message now splits both directions:

```
FAILED tests/test_deploy_paths_exclusions.py::test_workflow_still_carries_every_trigger_entry_in_order
  Which entry moved decides which way this breaks.
    NEGATIONS (`!tools/**/...`) — ... Wasteful, but loud and harmless.
    POSITIVES (`static/example/...`) — the OPPOSITE failure. ... prod keeps
    serving the old layer and nothing is red. Silent staleness, which is the
    worse direction.
```

The dropped entry is a positive and the message now names the correct
consequence. Fixed.

### D4 — PASS

`c749480`, with `paths` replaced by `paths-ignore` (the only state that reaches
the floor):

```
E   KeyError: 'paths'
ERROR tests/test_deploy_trigger_covers_dockerfile_copies.py - KeyError: 'paths'
```

`ebe27fd`, same mutation — the module imports and the floor delivers its own
message, reported first:

```
E       AssertionError: deploy-modal.yml grew a paths-ignore filter.
        _triggers_deploy models only the paths allowlist, so it now over-reports
        coverage. Teach it the second channel before trusting this module again.
...
6 failed, 10 passed in 0.70s
```

Dead code is now live code. Fixed.

### D2 — FAIL

The mechanics work (see criterion 4). The content is wrong. See **R2-1**.

## Criterion 3 — attacking D1's fix. PASS on the fix, FAIL on its justification

**Is refusing the right call?** Yes. Rendering `\` through `re.escape` produces a
regex matching only a literal backslash, and no repo path contains one — the
pattern would silently subtract itself. Refusal is the conservative choice.

**Is the failure loud and actionable?** Yes, and better than D4's original shape.
`_TRIGGER_PATHS` is built at module scope without compiling, so a `\` pattern does
**not** kill collection. It produces one clean named failure from
`test_no_live_trigger_pattern_uses_an_unmodelled_metacharacter` plus 8
parametrized errors. The message names the pattern, the offending character, the
consequence, and the remedy.

**Can `\` reach `_to_regex` where refusal would be wrong?** No. Both call sites
(`_triggers_deploy` and the live-pattern floor) pass only **patterns**; paths are
handed to `.match()`, which never consults `_UNMODELLED_METACHARS`. Windows
separators cannot leak in either:

```
tracked files containing a backslash: []  count= 0
_triggers_deploy('tools\af2\modal_app.py') = False     # a path, not a pattern; no refusal
```

`_tracked_files()` uses `git ls-files -z` (POSIX separators even on Windows) and
`_copied_files` wraps each in `pathlib.PurePosixPath`.

**Can it fire spuriously on a legitimate POSIX pattern?** No. All eight live
patterns pass, as do spaces, unicode, `**/`, `*`, dots and dashes:

```
OK  'tools/**'      OK  '!tools/**/meta.py'   OK  'static/example/1HEW.pdb'
OK  '**/README.md'  OK  'src/**/*.py'         OK  'path with space/x.txt'
OK  'naïve/ünïcode.txt'                       OK  '.github/workflows/deploy-modal.yml'
```

**But the stated reason is unsupported — R2-2.** See defects.

## Criterion 4 — attacking D2's new assertion

**(a) Is the premise true of the current list? PASS**, by execution:

```
'tools/**'              negation=False  contains'**/'=False  -> ^tools(?:/.*)?$
'!tools/**/meta.py'     negation=True   contains'**/'=True   -> ^tools/(?:.*/)?meta\.py$
'!tools/**/example/**'  negation=True   contains'**/'=True   -> ^tools/(?:.*/)?example(?:/.*)?$
'!tools/**/__init__.py' negation=True   contains'**/'=True   -> ^tools/(?:.*/)?__init__\.py$
```

Every `**/` is a negation. The one positive using `**` (`tools/**`) is a trailing
`/**`, a different construct.

**(b) Does it fire on a positive `**/`? PASS.** Added `- '**/Dockerfile.modal'`
to `on.push.paths`:

```
E       AssertionError: deploy trigger grew positive `**/` pattern(s)
        ['**/Dockerfile.modal']. Every `**/` here was a negation, which is what
        makes _to_regex's unverified zero-segment reading safe: ...
tests\test_deploy_trigger_covers_dockerfile_copies.py:298: AssertionError
1 failed, 15 deselected in 0.62s
```

**(c) Spurious fires? PASS** — green on the live list in both full-suite runs.

**(d) Can it be evaded? YES — R2-7.** The filter is the literal substring
`"**/"`, so a positive using `**` in any other shape slips past. Demonstrated by
adding `- 'static/**'` to the trigger:

```
16 passed in 0.56s                                    # guard stays green
positive_globstars filter sees: []                    # assertion sees nothing
regex: ^static(?:/.*)?$
  matches bare 'static'?              True            # the zero-case over-match
  matches 'static/example/1HEW.pdb'?  True
```

`static/**`, `docs/**`, `static/example/**`, bare `**` and `**.py` are all
positives containing `**` that the assertion does not classify. Only trailing
`/**` actually diverges from GitHub's documented examples (GitHub's `docs/**` row
lists `docs/README.md` and `docs/mona/octocat.txt`, not bare `docs`), and the
divergence is the bare-directory string, which is never a changed-file path. So
the evasion is **real as a filter gap but inert in consequence** — which is more
than the `_to_regex` docstring's "the ambiguity is bounded rather than resolved,
and the bound is structural" implies, since the bound covers one of the four
modelled constructs, not all of them.

**(e) Is the safety argument sound? YES.** I did not take this on reasoning. I
re-implemented the translator with the *strict* reading (`**/` -> `(?:.*/)`,
requiring at least one segment) and diffed coverage over all 631 tracked files:

```
covered under PERMISSIVE but not STRICT (OVER-report = DANGEROUS): 0
covered under STRICT but not PERMISSIVE (UNDER-report = loud false alarm): 1
      tools/__init__.py
  -> SOUND
```

Zero over-reports. The permissive reading can only under-report, exactly as
claimed, and the single disagreement is `tools/__init__.py` — the very file
#157's comment hedges about. It is COPYed by no Dockerfile, so it produces no red
today either. Structurally this is guaranteed: under later-wins a negation match
only ever sets `hit = False`, so extra negation matches are monotone
non-increasing in coverage.

**The premise is moot.** All of (a)-(e) test an argument that only exists because
the PR believes `**/` semantics are unknown. They are documented. See R2-1.

## Criterion 5 — D4's regression risk. PASS, with an observation

Converting `push["paths"]` to `push.get("paths", [])` does turn a loud `KeyError`
into a silent `[]`. What happens downstream, measured:

* **Assertion order is correct.** `test_the_trigger_is_a_paths_allowlist_with_no_paths_ignore`
  is the first test in the file, and `assert "paths-ignore" not in push` precedes
  `assert _TRIGGER_PATHS`. pytest reports in file order, so the intended message
  wins and is the first thing the reader sees.
* **The exact-list assertion is in the other module**, not this one, so it does
  not compete for the reader's attention within the guard module.
* **Observation (not a defect):** the empty `_TRIGGER_PATHS` makes
  `_triggers_deploy` return `False` for everything, so four parametrized cases
  also go red with *"Either add the path to the trigger, or stop COPYing it"* —
  wrong advice for this scenario. Net still better than a bare collection error
  with no explanation, since the correct message appears first, but the commit
  message's "the floor fires with its own message instead of a collection error"
  understates it: it fires *alongside five other failures, four with misleading
  remediation*.
* **Incompleteness — R2-8.** The fix was applied to one of the two modules that
  subscript `["paths"]`.

## Criterion 6 — is D3's new message TRUE? PASS

Both factual claims verified by execution, not by reading.

Claim 1, *"no `tools/**` negation can match a `static/` path"*: each negation
regex cross-multiplied against all 25 tracked `static/` files.

```
negation x static-file matches: NONE  -> claim 1 TRUE
```

Claim 2, *"each is a literal file path so it re-includes nothing"* — i.e.
reordering the positives is inert. Brute-forced every permutation of the three
positives at every insertion point among the other five entries, comparing
`_triggers_deploy` over all 631 tracked files against the shipped order:

```
orderings tried=36  files each=631  differences=0
  -> claim 2 TRUE (order of the positives is inert)
```

This holds under GitHub's documented `!` rule too, not just under the model's
full later-wins: the positives are disjoint from every negation, so no
re-inclusion question arises.

Supporting facts in the message also check out: exactly four Dockerfiles COPY
those fixtures (`af2` -> BPTI.fasta, `colabfold` and `esmfold` -> ubiquitin.fasta,
`mpnn` -> 1HEW.pdb), and no non-`static/` trigger entry matches any `static/`
path.

## Criterion 7 — new overclaiming. FAIL

Three false or stale claims, all found by checking the text against the tree.

**The five-vs-six explanation is TRUE**, and I verified it rather than accepting
it: `gh pr view 159` shows #159's body now reads *"Eight non-vacuity floors,
because four of the eight Dockerfiles carry no COPY at all…"*. D6 was corrected in
#159 pre-merge and has nothing in code to fix, so "five" in the commit subject and
"six" in the PR title are both defensible.

The three that are not: **R2-3**, **R2-4**, **R2-5** below.

## Criterion 8 — were round 1's findings right?

**D2 — WRONG, and this is the important one.** Round 1 wrote *"I could not
resolve which is right"* after failing to retrieve GitHub's cheat sheet from the
rendered docs pages. The table is retrievable — it lives in the docs' markdown
source, which round 1 did not try. From
`github/docs@main:content/actions/reference/workflows-and-actions/workflow-syntax.md`,
section `### Patterns to match file paths` (line 1553ff), literal rows:

```
| `'**/README.md'`     | A README.md file anywhere in the repository. | `README.md`<br/><br/>`js/README.md` |
| `'**/docs/**'`       | Any files in a `docs` directory anywhere ... | `docs/hello.md`<br/><br/>`dir/docs/my-file.txt` ... |
| `'**/*-post.md'`     | A file with the suffix `-post.md` anywhere ... | `my-post.md`<br/><br/>`path/their-post.md` |
| `'**/migrate-*.sql'` | ...                                          | `migrate-10909.sql`<br/><br/>`db/migrate-v1.0.sql` ... |
```

Four rows, each listing a **root-level file with zero leading directories** as an
example match — including the exact `**/README.md` -> `README.md` case the
docstring uses. `**/` matching zero leading segments is GitHub's documented
behaviour. #159's original docstring was correct and its message-less assertion
pinned a true fact.

Round 1 was right that the assertion had no message, and right that a message is
worth adding. It was wrong that the fact was in doubt, and #160 built its
headline fix on that error.

Note the prose #160 quotes *is* real — line 1514, `**`: "Matches zero or more of
any character." But it is the one-line summary four lines above the table that
settles the question, and the table wins. Quoting the summary and omitting the
table is what makes #160's framing wrong.

**D1 — right about the defect, wrong about the reason.** The mis-compilation is
real and reproduced. But the justification ("GitHub's documented escape
character") is not supported: the cheat sheet enumerates exactly `*`, `**`, `?`,
`+`, `[]`, `!`, and `grep -i escape` over the whole workflow-syntax page returns
nothing. See R2-2.

**D3 — right.** Reproduced on `c749480`; the message really did name the opposite
consequence.

**D4 — right.** Reproduced: bare `KeyError: 'paths'` at collection on the base.

**D5 — right.** "Two … both" over three bullets, now "Three … all".

**D6 — right**, and already corrected in #159's body.

**One round-1 open item was already closed and round 1 missed it.** Round 1
listed `.dockerignore` as unguarded ("worth a note in the module"). The sibling
module already carries `test_no_dockerignore_narrows_what_modal_uploads`
(`tests/test_deploy_paths_exclusions.py:409`), which asserts none of the three
locations Modal's `find_dockerignore_file` checks exists. The scenario cannot
arise silently.

## Criterion 9 — what still has NO guard

* **GitHub's real engine is still unexercised.** The semantics are now
  *documented* (above), which is much stronger than round 1's "unknown", but
  documentation is not the engine. Nothing here proves a push touching
  `static/example/1HEW.pdb` queues the workflow.
* **`_triggers_deploy`'s re-inclusion rule is unverified.** The docstring claims
  "a positive after a matching negation re-includes … which is GitHub's rule".
  GitHub documents only the other direction ("a file … also matches a negative
  pattern defined later … will not be included"). Inert for the current list —
  proved by the 36-ordering brute force — but unasserted and unsourced.
* **Trailing `/**` positives are unpinned** (R2-7): no assertion covers the
  `tools/**` -> bare `tools` divergence, and no comment records why it is inert.
* **`ADD` is claimed to fail the build "outright"** in the module docstring. The
  parser half is verifiable; "the docker build fails outright" is a claim about
  docker that no test exercises. No Dockerfile uses `ADD` today.
* **`esmfold2_design` remains the invisible ninth** to any
  `tools/*/Dockerfile.modal` check, and its image pulls `binder_design.py` by
  network `curl` at a pinned SHA — not a repo path, so outside every trigger and
  both guards.
* **No build SHA on `/readyz`**, so no deploy is verifiable from outside.
  Pre-existing.

Not open: the `add_local_file` channel *is* guarded —
`test_modal_app_stays_self_contained` resolves every `add_local_file` argument and
asserts it equals `tools/{app}/run_pipeline.py`, so the guard module's scope-limit
prose is backed by an executable pin.

---

## Defects

| id | severity | blocks merge? | summary |
|---|---|---|---|
| **R2-1** | **MEDIUM** | **yes** | D2's "fix" replaces a TRUE, GitHub-documented statement with a false one. `**/` zero-segment matching is documented with four explicit root-level examples. The docstring now calls it "a CONVENTION, not a verified GitHub fact" that "nobody on this branch has confirmed"; the new assertion message calls it "_to_regex's unverified zero-segment reading". A new false docstring, pinned by an assertion, in the PR that exists to delete false docstrings. |
| **R2-2** | **LOW–MEDIUM** | **yes** | D1's justification asserts as documented GitHub fact that "`\` escapes the following character so it is matched literally". GitHub's filter-pattern cheat sheet lists `*`, `**`, `?`, `+`, `[]`, `!` and no escape character; "escape" appears nowhere on the page. The **fix is correct** — refusing an undocumented character is conservative — but the reason is an unverified assumption stated as fact. Mirror image of R2-1, same commit, same file. |
| R2-3 | LOW | no | PR body: "The old name survives only in `docs/qc/deploy-init-exclusion-round1.md`". It also survives at lines 281 and 411 of `docs/qc/deploy-trigger-static-example-round1.md` — a file **this PR adds**. |
| R2-4 | LOW | no | PR body: "the new docstring names the rename so a grep still lands". It does not. The docstring says "It was named for the negations alone" and never writes `test_workflow_still_carries_every_negation_in_order`; grepping the old name does not land there. |
| R2-5 | LOW | no | Commit message states "Suite re-measured: 5350 passed / 20 skipped" and "origin/main (3ec66b9) baseline remains 5334 / 20". This commit's parent is `c749480`, and both sides measure **5372 / 21**. The PR body is right; the commit message — the durable git artifact — is stale and wrong for the tree it describes. |
| R2-6 | COSMETIC | no | `tests/test_deploy_trigger_covers_dockerfile_copies.py`:76 says `_to_regex` "models none of the **four**"; line 122 still says "except the **three** metacharacters above". Adding `\` made a previously-defensible count wrong. Same miscount shape as D5, in the same file, introduced by the commit fixing D5. |
| R2-7 | LOW | no | D2's new assertion filters on the literal substring `"**/"`, so positive `**` patterns in other shapes evade it (`static/**`, `docs/**`, `**`, `**.py` — demonstrated: 16 passed with `static/**` in the trigger). Inert today, but the `_to_regex` docstring's "the bound is structural" claims more coverage than the assertion delivers. |
| R2-8 | LOW | no | D4's `.get("paths", [])` was applied to one of the two modules that subscript `["paths"]`. `tests/test_deploy_paths_exclusions.py:327` still raises a bare `KeyError: 'paths'` under the same mutation — in the very test whose message this PR rewrote. Verified: `1 failed, 40 passed`, `E KeyError: 'paths'`. |

### Recommended remedy for R2-1 / R2-2

Both are prose edits, no behaviour change:

1. Restore the `**/` docstring to a positive statement and **cite the source**:
   GitHub's "Patterns to match file paths" table, `'**/README.md'` -> `README.md`.
   Keep the assertion and keep a message — but the message should say the reading
   is documented, not unverified.
2. Either drop the `positive_globstars` assertion (its premise is unnecessary once
   `**/` is known-correct) or keep it and re-motivate it honestly as a
   conservatism pin, not as a safety bound on an unknown.
3. Reword the `\` comment to say what is true: `\` is **not** in GitHub's
   documented filter-pattern set, its behaviour is unknown, and `re.escape`-ing it
   produces a regex that matches nothing on a POSIX path — so it is refused.
4. One-word fix for R2-6: "three" -> "four" at line 122.

R2-3/R2-4/R2-5 are PR-body and commit-message corrections.

## What I could NOT verify — stated as unverified, not assumed fine

1. **GitHub's actual filter engine.** I verified GitHub's *documentation*, from
   its own source repository, and that is a real advance over round 1. I did not
   run the engine. Documentation and implementation can differ.
2. **That a push touching `static/example/1HEW.pdb` queues the workflow.** Only
   observable on a real push to `main`. Recorded environmental caveat: this repo
   has previously seen GitHub drop push events entirely.
3. **That Modal's build cache invalidates and the image is rebuilt.** `modal` was
   deliberately never invoked.
4. **That the redeployed image is what prod serves.** `/readyz` exposes no build
   SHA. Pre-existing open item.
5. **Whether GitHub's engine treats `\` as an escape.** I established only that
   GitHub does not *document* it. It may well behave that way; I make no claim
   either direction, which is exactly my objection to the comment that does.
6. **Whether `**` in a positive trailing `/**` diverges on any real path.** I
   showed the only divergence is the bare-directory string and that no changed-file
   path takes that form; I did not enumerate every conceivable future path shape.
7. **The two known-flaky node tests.** Both runs were green with 0 FAILED/ERROR
   lines, so no isolation rerun was needed; I did not probe their flakiness.

## Method notes

* Worktree `…/scratchpad/qc160` detached at `ebe27fd`; baseline worktree
  `…/scratchpad/qc160base` at `c749480`, created by me and removed after review.
  The `pr159`/`qc159` worktrees were read for round 1's report only.
* Every experimental edit reverted with `git checkout --` immediately after its
  run; `git status --porcelain` confirmed empty after each and at the end (both
  worktrees `dirty=0` apart from this report).
* GitHub docs fetched read-only over HTTPS and treated as data. Nothing
  committed, nothing pushed, PR not merged. `modal` never invoked.
