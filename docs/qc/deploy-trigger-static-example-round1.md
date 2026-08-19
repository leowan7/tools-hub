# QC round 1 — PR #159, `static/example` deploy trigger + Dockerfile-COPY guard

**SHA reviewed: `984d23bd247afcf0b9ac9f53a938a7560f1ee84f`**
(merge; parents `f7f7b61282f90b8aaa9a34dcc9b4cc34c94a5d1a` = PR #159 work, `3ec66b92e2a19ebab2f20145be640c3a29de31d9` = `origin/main`. Merge-base `7fd180d`.)

Reviewed by an independent QC agent that did not write the change. Reviewed in a
dedicated detached worktree; nothing committed, nothing pushed, `modal` never invoked.

## Verdict

**PASS — recommend merge.** The bug is real, the fix is right, and the guard is
general rather than fitted to this instance. Six defects found, all documentation
or message-quality, **none blocking**. The one that matters most (D2) is a claim
about GitHub's `**/` semantics that I could not verify from here; its risk
direction under the current trigger list is loud-not-silent, so it cannot cause
the staleness this guard exists to prevent.

| # | Criterion | Verdict |
|---|---|---|
| 1 | Both baselines measured first-hand | PASS |
| 2 | Merge resolution audited for lost content | PASS |
| 3 | Trigger-filter model attacked | PASS, 2 divergences (D1, D2) |
| 4 | Guard proven general, not fitted | PASS |
| 5 | Assertion messages read, not just pass/fail | PASS for #159's guard; D3 against the #157 test this merge edited |
| 6 | Third channel to a container image | PASS — none found; D4 found while looking |
| 7 | Goal chain to production | PASS with steps explicitly unverifiable from here |
| 8 | PR body / docstring overclaim audit | PASS on substance; D5, D6 cosmetic |

---

## Criterion 1 — baselines. PASS

Interpreter `C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe`,
run as `-m pytest -q` from each worktree root, no path argument, output redirected
to a file (never piped through `tail`). Run sequentially, not concurrently, to
avoid loading the two known-flaky node tests.

```
origin/main 3ec66b9 : 5334 passed, 20 skipped in 340.90s (0:05:40)   exit 0
merged      984d23b : 5350 passed, 20 skipped in 246.80s (0:04:06)   exit 0
```

`grep -cE "^(FAILED|ERROR)"` over both logs: `0` and `0`. No flakes, no reruns needed.

**Delta = +16**, exactly the new module's 16 test cases (8 floor tests + 8
parametrized Dockerfile cases). The worker's reported numbers match my measurement
exactly, in both directions.

Module in isolation: `16 passed in 0.57s`. Both guard modules together:
`57 passed in 11.32s` — the PR body's "57 tests" claim is exact.

## Criterion 2 — merge audit. PASS

Two independent lines of evidence, the second decisive.

**(a) Nothing from `main` was lost.** `git diff 3ec66b9 984d23b` touches 3 files,
**324 insertions, 0 deletions**. A merge that deletes nothing relative to a parent
cannot have dropped that parent's content.

**(b) Nothing anywhere else was lost.** I recomputed the merge with git's own
resolver and compared trees:

```
$ git merge-tree --write-tree f7f7b61 3ec66b9   -> aebbf8b171baf10a60b0a2eecd76f7388adf430a
$ git rev-parse 984d23b^{tree}                  -> 640f71c2eecf7ace38dc6cc1245580a1c098d21b
$ git diff --stat aebbf8b1 984d23b^{tree}
 .github/workflows/deploy-modal.yml                    | 12 ++++++++----
 tests/test_deploy_paths_exclusions.py                 | 13 +++++++++++++
 tests/test_deploy_trigger_covers_dockerfile_copies.py |  2 +-
 3 files changed, 22 insertions(+), 5 deletions(-)
```

Every other file in the repo — all 54 that differ from parent 1, including
`blueprints/tools.py`, the 14 `meta.py`/`__init__.py` pairs, the templates and the
`docs/qc/` set — is **byte-identical to git's mechanical auto-merge**. The recorded
"clean automerge silently dropped a function" failure mode is excluded by
construction: a dropped hunk would show up here as a difference from the auto-merge,
and there is none.

The 3 differences are all deliberate hand edits, and the only conflict was one hunk:

* `deploy-modal.yml` — the conflict was `<<<<<<< f7f7b61` (#159's three statics +
  comment) vs `>>>>>>> 3ec66b9` (#157's `!tools/**/__init__.py`). The resolution
  **keeps both sides**, in the correct order, plus a new paragraph explaining why
  the positives are order-free. Both rationales survive: #157's `__init__.py`
  block is present in full (lines 28–76), #159's `static/example` block in full
  (lines 83–103). No third thing was dropped.
* `tests/test_deploy_paths_exclusions.py` — `_EXPECTED_PATHS` grew the three
  statics plus a 10-line explanatory comment. Pure insertion.
* `tests/test_deploy_trigger_covers_dockerfile_copies.py` — one line, the docstring
  drift correction (`static/example/**` → `three fixture entries`). **The correction
  is right**: the trigger does list three literal files, not a glob. It is also
  complete — that string appeared nowhere else in the module.

Merged `on.push.paths` order confirmed, read back through PyYAML, exactly as reported:
`tools/**`, `!tools/**/meta.py`, `!tools/**/example/**`, `!tools/**/__init__.py`,
the three `static/example/...` literals, `.github/workflows/deploy-modal.yml`.

## Criterion 3 — the trigger-filter model. PASS, with two divergences

This is the load-bearing piece, so I drove `_to_regex` directly rather than through
the tests. Rendered regexes for the live list:

```
'tools/**'                           -> ^tools(?:/.*)?$
'!tools/**/meta.py'                  -> ^tools/(?:.*/)?meta\.py$
'!tools/**/example/**'               -> ^tools/(?:.*/)?example(?:/.*)?$
'!tools/**/__init__.py'              -> ^tools/(?:.*/)?__init__\.py$
'static/example/BPTI.fasta'          -> ^static/example/BPTI\.fasta$
'static/example/ubiquitin.fasta'     -> ^static/example/ubiquitin\.fasta$
'static/example/1HEW.pdb'            -> ^static/example/1HEW\.pdb$
'.github/workflows/deploy-modal.yml' -> ^\.github/workflows/deploy\-modal\.yml$
```

Everything the criterion asked for, verified by execution:

| construct | result | correct? |
|---|---|---|
| `*` not crossing `/` | `tools/*/meta.py` vs `tools/a/b/meta.py` → False; vs `tools/a/meta.py` → True | yes — and strictly better than `fnmatch`, which the docstring correctly calls out |
| `**` crossing `/` | `tools/**.py` matches `tools/x.py` and `tools/a/b/x.py` | yes |
| leading `**/` | `**/README.md` matches `README.md`, `docs/README.md`, `a/b/README.md` | **see D2** |
| trailing `/**` | `tools/**` matches `tools`, `tools/`, `tools/a`; not `toolsX` | matches bare `tools` — divergent but inert (no changed-file path is ever a bare directory) |
| `**` in the middle | `a/**/b/**/c` → `^a/(?:.*/)?b/(?:.*/)?c$` | yes |
| literal, no wildcard | exact match only; rejects `…1HEW.pdbX`, `Xstatic/…`, `…1HEWZpdb` | yes — `.` is escaped, so no accidental wildcard |
| later-wins over several matches | `tools/af2/modal_app.py` → True; `tools/af2/meta.py` → False; `tools/af2/example/nested/x` → False; `static/example/1HEW.pdb` → True; `static/example/other.pdb` → False; `app.py`, `README.md` → False | yes — evaluates the whole list, does not short-circuit |
| `?` `+` `[]` | `*.jsx?`, `tools/**/*+.py`, `tools/**/[abc]*.py`, `a?b`, `a+b`, `a[0-9]b` all → `NotImplementedError` | yes |

**The refusal is reachable, not dead code.** Two live paths reach it:
`test_no_live_trigger_pattern_uses_an_unmodelled_metacharacter` walks the real
trigger list, and `_triggers_deploy` calls `_to_regex` on every pattern for every
path check. The `NotImplementedError` message names the offending pattern and the
offending metacharacters. Good.

I also confirmed the guard mirrors Modal's real behaviour rather than approximating
it. From installed `modal 1.4.2`, `modal.image._create_context_mount`:

```python
copy_patterns = extract_copy_command_patterns(docker_commands)
if not copy_patterns:
    return None  # no mount needed
include_fn = FilePatternMatcher(*copy_patterns)
```

`_copied_files` is the same two calls with the same early-exit. And
`_create_context_mount_function` sets `context_dir = Path.cwd() if context_dir is
None else …` — every app calls `from_dockerfile(_DOCKERFILE)` with no `context_dir`,
and CI runs `modal deploy tools/<app>/modal_app.py` from the repo root, so
**context dir = repo root**, which is exactly the frame the guard resolves patterns in.

Parser claims spot-checked directly against Modal 1.4.2, all correct:
`ADD` → `[]`; `COPY --from=s` → `[]`; lowercase `copy` → resolved; multi-source
COPY → both sources; line continuations → resolved; `COPY . /app` → `['./**']`.
(`COPY --chown=…` raises Modal's own `InvalidError` — the guard would error rather
than fail cleanly, but Modal refuses that Dockerfile too, so it is loud either way.)

### D1 — `\` is neither modelled nor refused. LOW, non-blocking

GitHub's filter syntax documents `\` as the escape character for literal
metacharacters. `_UNMODELLED_METACHARS = frozenset("?+[]")` omits it, and the
literal branch runs it through `re.escape`:

```
_to_regex("foo\\*bar") -> ^foo\\[^/]*bar$
   matches 'fooZZZbar'? False    matches 'foo*bar'? False
```

GitHub would read that pattern as the literal string `foo*bar`. The guard matches
neither that nor anything else useful — it silently mis-models rather than refusing,
which is precisely what the module's own rationale says it will not do. No live
pattern uses `\`, so nothing is wrong today. **Fix is one character**: add `\\` to
`_UNMODELLED_METACHARS`.

### D2 — the `**/` zero-segment reading is asserted as fact but is unverified. LOW–MEDIUM, non-blocking, latent

`_to_regex` renders `**/` as `(?:.*/)?` — zero or more leading segments, *including
zero*. The docstring states this as settled GitHub behaviour:

> `**/` matches any number of leading segments INCLUDING zero, so `**/README.md`
> hits the repo-root file too

and `test_pattern_translation_distinguishes_star_from_globstar` **enforces** it:

```python
assert _to_regex("**/README.md").match("README.md")   # no assertion message
```

That is the glob/gitignore convention. GitHub's own cheat sheet instead defines `**`
as "matches zero or more of any character", which read literally makes
`**/README.md` require the `/` and therefore *not* match a root-level `README.md`.
**I could not resolve which is right.** I attempted to fetch the Filter pattern cheat
sheet table from `docs.github.com` (current and Enterprise Server 3.3 URLs) and via
search; the table did not come back in the retrieved content in any attempt. GitHub's
real filter engine is not runnable from here.

This is the classic shape this repo keeps producing — a docstring asserting an
invariant, and a test pinning it — on a pattern (`**/README.md`) that appears
**nowhere in the live trigger list**. Notably, #157's own comment in
`test_deploy_paths_exclusions.py` already hedges the same point: "the top-level
`tools/__init__.py`, where `**` handling differs". So the repo already treats this
as uncertain; #159 states it as fact.

**Why it does not block.** All three live uses of `**/` are **negations**
(`!tools/**/meta.py`, `!tools/**/example/**`, `!tools/**/__init__.py`). If the
zero-segment reading is too permissive, those negations exclude *more* than GitHub
does, so `_triggers_deploy` under-reports coverage and the guard goes **red on a
file that is really covered** — a false alarm, loud, never silent staleness. Every
**positive** in the live list is either `tools/**` (trailing `/**`, agrees with
GitHub on every real file path) or a wildcard-free literal. So for this list the
guard cannot over-report.

The exposure is purely forward-looking: a future *positive* using a leading `**/`
(say `**/Dockerfile.modal`) would be reported as covering a root-level file that
GitHub might not match — the dangerous direction. Concretely today, the model says
`tools/__init__.py` is excluded; under the literal GitHub reading it would still be
covered by `tools/**`. That file **does** exist and is tracked, so the divergence is
live, just harmless in this direction.

**Recommendation (follow-up, not a merge blocker):** soften the docstring from an
assertion to a stated assumption, and either give the `**/README.md` assertion a
message naming it as an assumed convention, or drop it until it can be confirmed
against a real workflow run.

## Criterion 4 — generality. PASS

I constructed my own counterexamples rather than re-running the worker's. All in a
throwaway worktree at the same SHA, each reverted immediately after.

| experiment | expected | actual |
|---|---|---|
| A. `COPY app.py /opt/app.py` into **boltz2** (tracked, on no trigger) | red, names `app.py` | red — `boltz2/Dockerfile.modal bakes ['app.py']…`, `1 failed, 15 passed` |
| B. `COPY tools/boltz2/run_pipeline.py` into boltz2 (covered by `tools/**`) | green | `16 passed` |
| C. `COPY tools/mpnn/example/ /opt/ex/` into mpnn (real tracked file, negated out) | red, names the real file | red — `bakes ['tools/mpnn/example/result.json']`, `1 failed, 15 passed` |
| D. bake a **fourth** fixture (`static/example/3s7g_fc_ab.pdb`) into iggm | red until the trigger grows | red — `bakes ['static/example/3s7g_fc_ab.pdb']` |
| E. D + add that path to `on.push.paths` **and** `_EXPECTED_PATHS` | green | `57 passed` |

E is the self-maintaining claim proven in both directions: the tight file-by-file
list cannot silently fall behind what the Dockerfiles bake in. C also confirms the
PR body's "subsumes the two existing negation premises" — a COPY of a negated-out
`tools/<slug>/example/*` path fails here.

**Vacuity, specifically.** Four of the eight Dockerfiles (boltz2, iggm, opendde,
proteina) carry no `COPY` at all, so their parametrized cases pass over an empty
set. The module handles this honestly rather than hiding it:
`test_some_dockerfile_actually_copies_something` is the floor against *all* eight
going empty, and its docstring says so plainly. More importantly, experiments A and
D deliberately targeted two of the four **no-COPY** Dockerfiles and both went red —
so the per-Dockerfile check is dormant, not dead: it activates the instant a COPY
appears. Confirmed independently: exactly 4 Dockerfiles carry a COPY, all four are
the `static/example` smoke targets, and there is no `ADD` anywhere.

Non-vacuity of the whole module, reproduced: stripping the three statics from the
trigger gives

```
FAILED …::test_every_copied_file_is_on_a_deploy_trigger[af2]
FAILED …::test_every_copied_file_is_on_a_deploy_trigger[colabfold]
FAILED …::test_every_copied_file_is_on_a_deploy_trigger[esmfold]
FAILED …::test_every_copied_file_is_on_a_deploy_trigger[mpnn]
4 failed, 12 passed in 1.21s
```

— exactly the four claimed, each naming its own fixture. Reverting `_EXPECTED_PATHS`
to main's version while the workflow keeps the statics gives `1 failed, 40 passed`,
also as claimed.

## Criterion 5 — assertion messages. PASS for #159; D3 against the file it edited

#159's own property message is the best kind: it names the Dockerfile, the exact
files, the workflow, the real mechanism, and both valid remedies.

> `mpnn/Dockerfile.modal bakes ['static/example/1HEW.pdb'] into its image, but no
> on.push.paths entry in .github/workflows/deploy-modal.yml matches those paths.
> Editing one of them would change this image without redeploying it, and nothing
> would say so. Either add the path to the trigger, or stop COPYing it.`

Nothing misleading in any of the five failures I induced from this module.

### D3 — the #157 test this merge edited now names the wrong mechanism. LOW–MEDIUM, non-blocking

The merge added three **positives** to `_EXPECTED_PATHS` but left
`test_workflow_still_carries_every_negation_in_order` — its name and its assertion
message — talking only about negations. I induced the realistic regression (someone
deletes `static/example/1HEW.pdb` from the trigger):

> `…GitHub applies` `paths` `later-wins: a negation moved above` `tools/**` `is
> re-included and stops excluding anything, and a negation dropped altogether
> resumes redeploying nine GPU images on web-tier-only edits.`

Nothing was negated, and the consequence is the **opposite** of what the message
states: an image goes *stale*, it does not start over-deploying. A developer reading
only this message is pointed at the wrong failure. The test *name* is likewise now
inaccurate — it pins three positives.

**Mitigated, which is why it does not block:** #159's own guard fires on the same
edit, simultaneously, with the correct message (both appear above). The merge also
added a clear 10-line comment above `_EXPECTED_PATHS` explaining the positives — but
a comment is not what a developer reads when a test goes red. **Fix:** widen the
message to cover a dropped positive, and rename to something like
`test_workflow_still_carries_every_trigger_entry_in_order`.

## Criterion 6 — is there a third channel? PASS, none found

Swept the repo for every way a repo file can reach an image: `add_local_file`,
`add_local_dir`, `add_local_python_source`, `from_dockerfile`, `dockerfile_commands`,
`context_files=`, `context_dir=`, `mounts=`, `Mount.`, `pip_install_from_requirements`,
`poetry_install_from_file`, `uv_pip_install(requirements=)`, `uv_sync`,
`micromamba_install(spec_file=)`, `spec_file=`.

Result: **no third channel.** Findings:

* Every `add_local_file` in all nine `modal_app.py` names its own
  `tools/<slug>/run_pipeline.py` (`_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"`),
  covered by `tools/**`. The docstring's scope-limit claim is accurate.
* `esmfold2_design`'s `micromamba_install` passes **inline package specs only**, no
  `spec_file=`. No repo path. No `pip_install_from_requirements` anywhere.
* `tools/proteina/_hotspot_canary.py` has a second `add_local_file`
  (`tools/proteina/_canary_scoring.py`) — under `tools/**`, and that file is a canary
  script the workflow never deploys.
* No `.dockerignore` is tracked or present. The guard does not model
  `_create_context_mount`'s `ignore_fn`, which is currently a no-op for exactly that
  reason. **Open item:** if a `.dockerignore` (or a `Dockerfile.modal.dockerignore`,
  which `find_dockerignore_file` also honours) is ever added, the guard would list
  files that no longer reach the image — over-strict, so red-and-loud, never silently
  permissive. Worth a note in the module rather than a code change.

Answering the criterion's real question — is there a channel that is *also* outside
`tools/` and therefore off every trigger? No. The only repo paths outside `tools/`
reaching any image are the three `static/example` fixtures this PR fixes. Between
them, #157's guard (Modal's real loader over the upload set) and #159's guard (the
Dockerfile COPY channel) cover the tracked-file-to-image question completely.

### D4 — a floor test that can never fire. LOW, non-blocking

`test_the_trigger_is_a_paths_allowlist_with_no_paths_ignore` asserts
`"paths-ignore" not in push` with a carefully written message. That assertion is
**unreachable**. GitHub forbids both keys in one filter, so the only way to get
`paths-ignore` is to *replace* `paths` — and module scope does
`_TRIGGER_PATHS = list(_push_filter()["paths"])` at line 62. I simulated it:

```
tests\test_deploy_trigger_covers_dockerfile_copies.py:62: in <module>
    _TRIGGER_PATHS = list(_push_filter()["paths"])
E   KeyError: 'paths'
ERROR tests/test_deploy_trigger_covers_dockerfile_copies.py - KeyError: 'paths'
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

The module dies at collection; the floor test never runs and its message is never
shown. The outcome is still **red**, so this cannot produce silent staleness — but
it is dead code presented as a live floor, and a maintainer gets a bare `KeyError`
instead of the explanation that was written for them. **Fix:** make `_push_filter`
tolerate a missing `paths` (e.g. `on["push"]` returned whole, `.get("paths", [])`
at line 62) so the floor test can actually deliver its message. The
`assert _TRIGGER_PATHS, "on.push.paths is empty"` half of that test is reachable and fine.

## Criterion 7 — will editing `static/example/1HEW.pdb` rebuild the mpnn image in prod?

**Most likely yes, and every step I could check is sound — but the first and last
links in the chain are not verifiable from this machine, and I am not claiming them.**

| # | step | status |
|---|---|---|
| 1 | Push to `main` touching `static/example/1HEW.pdb` matches `on.push.paths` | **NOT VERIFIED — not verifiable here.** GitHub's filter engine cannot be exercised from this environment; I could not even retrieve the pattern cheat sheet. Confidence is nonetheless high: the entry is a **wildcard-free literal path**, the least ambiguous form the filter has, with no glob semantics to get wrong. |
| 2 | No negation cancels it | **Verified in model, high confidence in reality.** All three negations are rooted at `tools/`; under any plausible reading a `static/` path cannot match one. `_triggers_deploy("static/example/1HEW.pdb")` → `True`. |
| 3 | Workflow runs the 9-job matrix | **Verified by reading the workflow.** Note: there is no per-app path filtering, so this redeploys **all nine apps**, not only mpnn. Accepted design, but it means the honest answer is "yes, along with eight others" — 4 of 9 need it, 5 are waste. |
| 4 | `modal deploy tools/mpnn/modal_app.py` resolves the COPY against the repo root | **Verified from Modal 1.4.2 source.** `context_dir = Path.cwd()` when unset; the app passes no `context_dir`; CI runs from the repo root. |
| 5 | The changed file enters the build context, changing the image layer | **Verified for the pattern/matcher half.** `extract_copy_command_patterns` + `FilePatternMatcher` resolve `COPY static/example/1HEW.pdb` to exactly that file, executed locally. |
| 6 | Modal's build cache invalidates on that context change and actually rebuilds | **NOT VERIFIED.** Requires a real build; `modal` was never run (billed A100). |
| 7 | The deployed app serves the new image | **NOT VERIFIED.** Recorded open item: `/readyz` exposes no build SHA, so no deploy is verifiable from outside. |

Two recorded environmental caveats that sit outside this PR but bear on the outcome:
GitHub has previously **dropped push events** in this repo (9 pushes, no runs), so a
correct trigger is necessary but historically not sufficient; and step 7 has never
been independently checkable here.

## Criterion 8 — overclaim audit. PASS on substance

Everything load-bearing in the PR body checks out, including the two claims most
worth doubting: `_create_context_mount` really does use those two functions in that
order (source quoted above), and the four Dockerfiles/lines named in the bug table
are exactly right (`af2:81`, `colabfold:62`, `esmfold:104`, `mpnn:85`). The
"other two files in that directory are docs-only" claim is correct — `static/example/`
holds exactly five tracked files, and the two unlisted ones
(`3ave_igg1_fc_dimer.pdb`, `3s7g_fc_ab.pdb`) are COPYed by nothing.

The docstring correction the worker reported making is **right and complete**: the
trigger does carry three literal entries, not a `static/example/**` glob, and the
stale string appeared nowhere else in the module.

Two cosmetic counting errors, neither affecting behaviour:

* **D5 (cosmetic).** Module docstring: "**Two** deliberate scope limits, **both**
  safe in the conservative direction:" — followed by **three** bullets (Dockerfile
  channel only; `COPY` only; tracked files only). All three bullets are individually
  accurate; only the count and the word "both" are wrong.
* **D6 (cosmetic).** PR body: "**Six** non-vacuity floors" — there are **eight**
  floor test functions before the `# The property.` banner. An undercount, so it
  understates the work; the +16 arithmetic in the body is still right (8 floors +
  8 parametrized).

I found no case of the module claiming a property it does not have.

---

## Defects

| id | severity | blocks merge? | summary |
|---|---|---|---|
| D1 | LOW | no | `\` (GitHub's documented escape char) neither modelled nor refused by `_to_regex`; silently mis-modelled. One-char fix: add it to `_UNMODELLED_METACHARS`. |
| D2 | LOW–MEDIUM | no | `**/` zero-segment reading asserted as GitHub fact in a docstring and pinned by a message-less assertion; unverifiable from here, and possibly the glob convention rather than GitHub's. Safe direction for the current list (negations only); latent risk for a future leading-`**/` positive. |
| D3 | LOW–MEDIUM | no | `test_workflow_still_carries_every_negation_in_order` now pins three positives but its name and message speak only of negations, describing the opposite consequence. Mitigated by #159's guard firing alongside with a correct message. |
| D4 | LOW | no | `"paths-ignore" not in push` is unreachable — the scenario that would trip it kills the module at collection with a bare `KeyError: 'paths'`. Loud either way, but the written explanation never reaches anyone. |
| D5 | COSMETIC | no | Docstring says "Two … both" over three bullets. |
| D6 | COSMETIC | no | PR body says "Six non-vacuity floors"; there are eight. |

None of these can produce the silent staleness the guard exists to catch. D1–D4 are
worth a small follow-up commit; D5/D6 are one-line edits.

## What I could NOT verify — stated as unverified, not assumed fine

1. **GitHub's actual `paths` filter semantics.** The engine is not runnable here. I
   could not retrieve the Filter pattern cheat sheet table from `docs.github.com`
   (tried the current and Enterprise Server 3.3 URLs and search; the table was not in
   any retrieved content). Everything in criterion 3 tests `_to_regex` against its own
   stated model and against *my* reading of GitHub's documented syntax — **not**
   against GitHub. Specifically unresolved: whether `**` can match zero characters
   (D2), and whether `tools/**` matches a bare `tools`.
2. **That a push touching `static/example/1HEW.pdb` actually queues the workflow.**
   Only observable on a real push to `main`. The strongest statement I can make is
   that the entry is a wildcard-free literal and no negation can reach it.
3. **That Modal's build cache invalidates and the image is really rebuilt.** Requires
   a real build; `modal` was deliberately never invoked.
4. **That the redeployed image is what prod serves.** No build SHA is exposed by
   `/readyz`; this is a pre-existing recorded open item, not something #159 changed.
5. **Behaviour under a `.dockerignore`.** None exists, so the guard's omission of
   `ignore_fn` is currently exact. I did not test the divergence, only reasoned that
   it is over-strict (loud) rather than permissive.
6. **`esmfold2_design`.** It has no Dockerfile, so `_DOCKERFILES` is 8, not 9, and the
   ninth app is invisible to this module. Correct here — it has no COPY channel at all,
   and its one local file is covered by `tools/**` and by #157's loader check — but it
   remains "the invisible ninth" to any `tools/*/Dockerfile.modal`-shaped check. Its
   image content also comes partly from a network `curl` of `binder_design.py` at a
   pinned SHA inside `run_commands`, which is not a repo path and therefore outside
   what any trigger or either guard can see. Out of scope for #159; recorded as an
   open item.

## Method notes

* Worktree `…/scratchpad/qc159` detached at `984d23b`; baseline worktree at `3ec66b9`
  and a throwaway experiment worktree at `984d23b` created by me for the mutation
  tests, so the measured suites were never perturbed by an edit.
* All experimental edits reverted with `git checkout --` immediately after each run;
  `git status --porcelain` confirmed empty after every experiment and at the end.
* Nothing committed, nothing pushed, PR not merged. `modal` never invoked.
