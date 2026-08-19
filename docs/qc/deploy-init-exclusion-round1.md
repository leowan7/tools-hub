# QC round 1 — `!tools/**/__init__.py` deploy-trigger exclusion (PR #157)

## Verdict: **BLOCKED**

Not because the exclusion is wrong. It is right, and I re-derived that from Modal's own
machinery rather than from the builder's table. It is blocked because the guard that is
supposed to keep it right has a hole that produces **exactly the failure this PR exists to
prevent** — a real GPU code change that silently never deploys — and I demonstrated that hole
end to end, on the app the builder itself calls the most exposed. Fix is 2-4 lines. See §7.

Reviewer: independent QC agent; did not build this.
Reviewed SHA: `5b9f9bc40cbaccc2a16af00ce575563906ad457a` (`chore/skip-deploy-on-init-py`)
Base: `origin/main` = `48b4b71eedd2f791142ee4d020ee977a6961a6be`, confirmed as the merge-base
after `git fetch origin`. **Trunk had not moved.**
Modal in the review venv: **1.4.2**. `requirements.txt` pins `modal>=1.4,<2.0`, the same bound
CI installs.

Every claim below is tagged **[RUN]** (I executed it, output reproduced) or **[REASONED]**
(argued from source I read but could not execute here). Nothing is reported as verified that I
could not run.

---

## 1. Baselines — measured myself, both match the claim [RUN]

Exact command, from each worktree root, **no path argument**:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| tree | SHA | result |
|---|---|---|
| base | `48b4b71` | **5262 passed, 20 skipped** in 338.47s |
| head | `5b9f9bc` | **5283 passed, 20 skipped** in 336.19s |

Delta **+21 passed, +0 skipped**. `pytest tests/test_deploy_paths_exclusions.py --collect-only -q`
collects **21 tests**. The whole delta is the new file; nothing else moved. Claim **CONFIRMED**.

---

## 2. The safety premise — re-derived independently, and it HOLDS [RUN]

I did not check the builder's table. I drove Modal's own resolution in a **separate subprocess
per app** (mirroring CI's one-job-per-app), with `cwd` = repo root, via
`modal.cli.import_refs.import_file_or_module`, then walked, for every registered function:
`FunctionInfo._type`, `get_entrypoint_mount()`, `spec.mounts`, and the whole image chain's
`_mount_layers` plus every `context_mount_function()` reached through the `_Image._from_args`
closure. Files enumerated with `_MountEntry.get_files_to_upload()` — i.e. the actual upload list,
not a pattern guess.

| app | `__package__` | fns | FunctionInfo | entrypoint mount | image mount layers + context mounts |
|---|---|---|---|---|---|
| af2 | `''` | 1 | FILE | `tools/af2/modal_app.py` | `tools/af2/run_pipeline.py`, `static/example/BPTI.fasta` |
| boltz2 | `''` | 1 | FILE | `tools/boltz2/modal_app.py` | `tools/boltz2/run_pipeline.py` |
| colabfold | `''` | 1 | FILE | `tools/colabfold/modal_app.py` | `tools/colabfold/run_pipeline.py`, `static/example/ubiquitin.fasta` |
| esmfold | `''` | 1 | FILE | `tools/esmfold/modal_app.py` | `tools/esmfold/run_pipeline.py`, `static/example/ubiquitin.fasta` |
| **esmfold2_design** | `''` | **2** | FILE (both) | `tools/esmfold2_design/modal_app.py` (both) | `_run_one_seed`: `tools/esmfold2_design/run_pipeline.py`; `run_tool` (debian_slim orchestrator): **nothing** |
| iggm | `''` | 1 | FILE | `tools/iggm/modal_app.py` | `tools/iggm/run_pipeline.py` |
| mpnn | `''` | 1 | FILE | `tools/mpnn/modal_app.py` | `tools/mpnn/run_pipeline.py`, `static/example/1HEW.pdb` |
| opendde | `''` | 1 | FILE | `tools/opendde/modal_app.py` | `tools/opendde/run_pipeline.py` |
| proteina | `''` | 1 | FILE | `tools/proteina/modal_app.py` | `tools/proteina/run_pipeline.py` |

**No `__init__.py` appears anywhere in any app's resolved upload set.** Both of
`esmfold2_design`'s functions were checked, not just the entrypoint; the orchestrator uploads no
local file at all. The premise is **true at `5b9f9bc`**.

Supporting checks I ran rather than took on faith:

* **Dockerfile COPY surface.** Only 4 of the 8 Dockerfiles contain any `COPY`, and each names one
  static fixture: `af2` → `static/example/BPTI.fasta`, `colabfold`/`esmfold` →
  `static/example/ubiquitin.fasta`, `mpnn` → `static/example/1HEW.pdb`. `boltz2`, `iggm`,
  `opendde`, `proteina` have **no COPY at all**. **Zero `ADD` lines anywhere.** I ran Modal's
  `extract_copy_command_patterns` + `FilePatternMatcher` over all 8 against **all 23**
  tools-side `__init__.py` paths (not the guard's narrower 10): **zero hits**. [RUN]
* **Import surface.** AST scan of all nine `modal_app.py` **and** all nine `run_pipeline.py`:
  zero relative imports, zero `tools.*` imports, in all 18 files. The only dynamic imports in the
  whole set are `proteina/run_pipeline.py:3830-3831`, and they import `proteinfoundation.*` —
  a package vendored into the container, not repo source. [RUN]
* **No automount.** `grep -rin automount` and `grep -rn AUTOMOUNT` over the installed modal
  1.4.2 tree: **zero hits**, including no `MODAL_AUTOMOUNT` config key. [RUN]

### Other upload paths the audit did not name — checked

* **`get_entrypoint_mount()` is not the whole story, and it is right that it isn't.** In
  `modal/_functions.py:727`, `all_mounts = [_get_client_mount(), *entrypoint_mount.values()]` —
  that is the *implicit* set only. Local files also reach a container through the **Image**:
  `_mount_layers` (from `add_local_*`) and `context_mount_function()` (from
  `from_dockerfile` / `dockerfile_commands` / `add_local_file(copy=True)`). My probe walked
  **all three** and the base-image chain, which is why the `static/example/*` fixtures and the
  `run_pipeline.py` copies show up above. Reading only `get_entrypoint_mount()` would have
  missed them. [RUN]
* **Dockerfile build context.** `modal/image.py:278-303`: `_create_context_mount` returns `None`
  outright when there are no COPY patterns, otherwise mounts `context_dir` filtered by
  `FilePatternMatcher(*copy_patterns)`. `context_dir` defaults to `Path.cwd()` — **the repo
  root**. So the whole repo is the candidate context, narrowed only by the COPY patterns. The
  guard's use of the same two functions is therefore a faithful mirror of Modal's real behaviour,
  not an approximation. [RUN + REASONED]
* **Secrets / Volumes / network file systems.** Remote objects resolved by name at load time
  (`_Secret`, `_Volume` handles in `_FunctionSpec`); they carry no local path. Nothing in the nine
  passes a local file to one. [REASONED, from `_FunctionSpec` + the probe's `volumes`/`n_secrets`]
* **`.env` / `modal.toml`.** Modal reads `~/.modal.toml` for *client config* (tokens,
  environment); it is not part of any mount or image layer. CI supplies credentials via
  `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` env vars, no file. [REASONED]
* **Nothing reads `__init__.py` at build or deploy time.** `grep` over `.github/workflows/` and
  all 8 Dockerfiles: the only hits are the new comment lines in `deploy-modal.yml` itself. The
  deploy step runs exactly `modal deploy tools/<app>/modal_app.py`. [RUN]

### The `sys.path` claim — confirmed, and it is worse-shaped than stated

`import_file_or_module` (`modal/cli/import_refs.py:61-87`) inserts **both** `""` (the cwd, i.e.
the repo root) **and** `str(full_path.parent)` (i.e. `tools/<slug>/`) onto `sys.path`. My probe
reports `sys.path[0:2] == ['<repo>/tools/mpnn', '<CWD/repo-root>']` and
`repo_root_importable == True`. So a future `import tools.<slug>` in a `modal_app.py` **does**
resolve at deploy time and fails only inside the container. Builder's claim **CONFIRMED**. [RUN]

Two refinements the builder's write-up does not make, both of which I verified:

1. Such an import does **not** flip `FunctionInfo` to `PACKAGE`. Classification
   (`function_utils.py:180`) keys off the *defining* module's `__package__`, which stays `''`
   no matter what that module imports. So the failure mode is not a silent stale image — it is a
   **loud in-container `ImportError` on every run**. I confirmed this empirically: with
   `importlib.import_module('tools.mpnn')` spliced into `mpnn/modal_app.py`, Modal's upload set is
   still exactly `{modal_app.py, run_pipeline.py, static/example/1HEW.pdb}` — the package does
   **not** follow it into the image. [RUN]
2. The guard **does** catch the static spelling (`import tools.mpnn`, `from . import meta`) but
   **not** the dynamic ones. See M6/M7 in §5. Because of (1), that miss cannot cause silent
   staleness, so I rate it low — but the guard's docstring implies coverage it does not have.

---

## 3. Every `__init__.py` under `tools/` — enumerated and judged [RUN]

22 tracked files. None reaches any container (§2), so **none needs to trigger a deploy**.

| path | deployed GPU app? | reaches a container? | verdict |
|---|---|---|---|
| `tools/__init__.py` | n/a (top level) | no | 6-line docstring, no code. Safe to exclude; see §6 for whether it even *is* excluded. |
| `tools/{af2,boltz2,colabfold,esmfold,esmfold2_design,iggm,mpnn,opendde,proteina}/__init__.py` | **yes, all 9** | **no** (proved in §2) | Flask adapter classes, 241-848 lines each. Safe to exclude. |
| `tools/{bindcraft,boltzgen,pxdesign,rfantibody,rfdiffusion}/__init__.py` | no | no | Web-tier-only tools with no `modal_app.py`. Cannot affect a GPU image. Safe. |
| `tools/platform_api/__init__.py` | no | no | Web tier. Safe. |
| `tools/developability/__init__.py` + `data/`, `dimensions/` | no | no | Scout-side package. Safe. |
| `tools/library_planner/__init__.py` + `data/`, `tests/` | no | no | Delisted tool, web tier. Safe. |

I grepped all nine deployed adapters for anything container-relevant (`modal`, `Dockerfile`,
`image`, `digest`, `sha256`, `volume`, `gpu`, `app_name`, `lookup`). Every hit is a prose comment
or a docstring cross-reference (e.g. `iggm`: zero hits at all). No adapter carries an image tag,
a digest, or a Dockerfile path that a build step consumes. [RUN]

---

## 4. The pattern shape — the builder called this untested; it is not [RUN]

The builder states it did not empirically test GitHub's matcher on `'!tools/**/__init__.py'` and
argues by analogy to `'!tools/**/meta.py'`. I went looking for the analogy's evidence and **found
it on main**.

`!tools/**/meta.py` and `!tools/**/example/**` landed in `f20b7b4` (merged as `95abcd6`,
2026-08-18T18:39), which is an ancestor of `origin/main`. Since that merge, main advanced through
five commits. Cross-referencing each commit's `tools/` file list against
`gh run list --workflow="Deploy Modal apps" --branch main`:

| commit | `tools/` files touched | all excluded? | deploy run? |
|---|---|---|---|
| `4b7af64` | 3 × `meta.py` | yes | **none** ✔ |
| `1bfce94` | 15 × `meta.py` + `tools/mpnn/example/result.json` | yes | **none** ✔ |
| `48b4b71` (main HEAD) | `tools/boltzgen/meta.py` | yes | **none** ✔ |
| `5e4fa66`, `66388af` | (none) | n/a | none, as expected |
| `71cf4cf`, `95abcd6` (before/at the exclusion) | Dockerfiles, `modal_app.py` | no | **ran** ✔ |

Three consecutive real pushes to main matched only `!tools/**/<literal>.py` /
`!tools/**/example/**` and produced **zero** deploy runs, while every push carrying a
non-excluded `tools/` file did run. `'!tools/**/__init__.py'` is character-for-character the same
shape with a different literal basename. **The shape argument is sound and now has empirical
backing, not just analogy.**

Caveat, stated honestly: this repo has a documented history of GitHub silently dropping push
events, so "no run" is in principle confoundable. Three-for-three with a clean complement (every
non-excluded push *did* run) makes a coincidental triple-drop implausible, but I could not
inspect GitHub's path-filter decision directly — only its observable effect. **[RUN, with that
caveat.]**

---

## 5. Mutation table — 22 mutations, all verified landed on disk [RUN]

Every mutation was applied in a **throwaway worktree** (`qc-init-mut`, discarded; nothing under
`tools/` is in what I push), proven present with `git diff --unified=0` + `git status --porcelain`
**before** any conclusion, then attributed by failing test **name**. `landed=False` count across
the whole battery: **0**. Guard file run: `pytest -q tests/test_deploy_paths_exclusions.py -rf`.

| # | mutation | landed | caught | failing test |
|---|---|---|---|---|
| M1 | `COPY . /app` (whole-context) | yes | **yes** | `test_dockerfile_copies_no_adapter_package[mpnn]` |
| M2 | `COPY --chown=root:root tools/mpnn/__init__.py /app/x.py` | yes | **yes** | `test_dockerfile_copies_no_adapter_package[mpnn]` |
| M3 | multi-stage `FROM … AS builder` + `COPY . /src` + `COPY --from=builder` | yes | **yes** | `test_dockerfile_copies_no_adapter_package[mpnn]` |
| M4 | `ADD tools/mpnn/__init__.py /app/x.py` | yes | **no** | — (harmless, see below) |
| M5 | `COPY tools/pxdesign/__init__.py` into **af2**'s Dockerfile | yes | **NO** | — **HOLE** |
| M5b | `COPY tools/*/__init__.py /app/` | yes | **yes** | `test_dockerfile_copies_no_adapter_package[mpnn]` |
| M6 | `importlib.import_module('tools.mpnn')` | yes | **no** | — (bounded, see below) |
| M7 | `__import__('tools.mpnn')` | yes | **no** | — (bounded) |
| M8 | deferred `import tools.mpnn` **inside a function body** | yes | **yes** | `test_modal_app_stays_self_contained[mpnn]` |
| M9 | `from . import meta` | yes | **yes** | `test_modal_app_stays_self_contained[mpnn]` |
| M10 | `add_local_file(f"tools/{_TOOL}/__init__.py", …)` (f-string) | yes | **yes** | `test_modal_app_stays_self_contained[mpnn]` |
| M11 | `_SNEAK = "…/__init__.py"` + `add_local_file(_SNEAK, …)` (variable) | yes | **yes** | `test_modal_app_stays_self_contained[mpnn]` |
| M12 | `.dockerfile_commands(["COPY tools/mpnn/__init__.py /root/x.py"])` | yes | **NO** | — **HOLE** |
| M12b | `add_local_dir("tools/mpnn", …)` | yes | **yes** | `test_modal_app_stays_self_contained[mpnn]` |
| M12c | `add_local_python_source("tools")` | yes | **yes** | `test_modal_app_stays_self_contained[mpnn]` |
| M13 | tenth app `newtool` added to the deploy matrix | yes | **yes** | `test_deploy_matrix_is_the_nine_known_apps`, `test_dockerfiles_are_discovered`, `test_every_app_has_the_excluded_init_and_a_modal_app`, `test_modal_app_stays_self_contained[newtool]` |
| M14 | hoist `!tools/**/__init__.py` **above** `tools/**` | yes | **yes** | `test_workflow_still_carries_every_negation_in_order` |
| M15 | drop the negation entirely | yes | **yes** | `test_workflow_still_carries_every_negation_in_order` |
| M16 | FLOOR: hide one Dockerfile (glob finds 7) | yes | **yes** | `test_dockerfiles_are_discovered` |
| M17 | FLOOR: strip `mpnn/modal_app.py` to 2 imports | yes | **yes** | `test_modal_app_stays_self_contained[mpnn]` |
| M18 | FLOOR: delete the only `add_local_file` call | yes | **yes** | `test_modal_app_stays_self_contained[mpnn]` |
| M19 | FLOOR: shrink the matrix to one app | yes | **yes** | `test_deploy_matrix_is_the_nine_known_apps`, `test_dockerfiles_are_discovered` |

**17 caught, 5 missed.** Building on the builder's 8, this adds 14 shapes it did not try
(M2-M5b, M6-M8, M10-M12c, M16-M19).

### Non-vacuity floors — each starved, each fired [RUN]

The brief flags that this repo has shipped a guard-over-zero-items before. All four floors are
real:

* **Matrix floor.** M19 cut the matrix to one app. Collection dropped **21 → 13 tests** (exactly
  the vacuity mode: the two parametrized families collapse), and
  `test_deploy_matrix_is_the_nine_known_apps` + `test_dockerfiles_are_discovered` both went red.
  The app list is genuinely read off the workflow (`yaml.safe_load` → `jobs.deploy.strategy.matrix.app`),
  not hardcoded, so M13's tenth app also failed four tests including a *new* parametrized case.
* **Dockerfile floor.** M16 renamed one Dockerfile; `test_dockerfiles_are_discovered` went red on
  7 ≠ 8. The `len(_APPS) - 1` phrasing means it tracks the matrix rather than a magic number.
* **≥5 imports floor.** M17 fired. Real margin check: the tightest apps parse 8 imports
  (`boltz2`, `mpnn`, `opendde`), so the floor has 3 of headroom — it is a scanner-drift detector,
  not a tight bound. That is what it claims to be.
* **≥1 `add_local_file` floor.** M18 fired.

One structural note, not a defect: `test_dockerfile_copies_no_adapter_package` short-circuits with
`if not patterns: return` — so for `boltz2`, `iggm`, `opendde`, `proteina` (no COPY at all) **4 of
the 8 parametrized cases assert nothing**. That is *correct*, because `_create_context_mount`
returns `None` when there are no patterns (`image.py:291`), so there is genuinely no context to
check. But it does mean half that family is a pass-by-vacuity, and it is not labelled as such.

---

## 6. The two holes, proved end to end [RUN]

I did not stop at "the guard stayed green". For each miss I re-ran the §2 probe on the mutated
tree and asked **Modal itself** whether the file now reaches the image.

| mutation | guard | does `__init__.py` actually reach the image? | severity |
|---|---|---|---|
| M4 `ADD …` | green | **no** — upload set unchanged (`{modal_app.py, run_pipeline.py, 1HEW.pdb}`). `extract_copy_command_patterns` only matches `COPY`, so the file is not in the context and the **docker build fails loudly** on the very deploy the Dockerfile edit triggers. | none — premise holds |
| M6 `importlib.import_module` | green | **no** — upload set unchanged. Loud in-container `ImportError`, not stale code. | low (scope gap only) |
| M7 `__import__` | green | **no** — upload set unchanged. Same as M6. | low (scope gap only) |
| M5 `COPY tools/pxdesign/__init__.py` into af2 | green | **YES** — Modal's af2 context mount becomes `{BPTI.fasta, run_pipeline.py, **tools/pxdesign/__init__.py**}` | **real** |
| M12 `.dockerfile_commands(["COPY tools/mpnn/__init__.py …"])` | green | **YES** — Modal's mpnn context mount becomes `{1HEW.pdb, run_pipeline.py, **tools/mpnn/__init__.py**}` | **BLOCKING** |

### M12 is the blocker, and it lands hardest on the app the builder calls most exposed

`esmfold2_design` has **no Dockerfile** — its image is built entirely in Python
(`Image.micromamba(...).micromamba_install(...).add_local_file(...)`). It is therefore precisely
the app where a maintainer needing to add a file reaches for `.dockerfile_commands([...])`, since
there is no Dockerfile to edit. I spliced exactly one line into `tools/esmfold2_design/modal_app.py`:

```python
.dockerfile_commands(["COPY tools/esmfold2_design/__init__.py /root/x.py"])
```

Landed (`git diff --unified=0` → `@@ -250,0 +251 @@`). Result:

```
GUARD:  21 passed in 0.56s
MODAL:  context_mount_files = ["tools/esmfold2_design/__init__.py",
                               "tools/esmfold2_design/run_pipeline.py"]
```

The adapter is now baked into the GPU image. That first edit touches `modal_app.py`, so it *does*
deploy once. **Every subsequent edit to `tools/esmfold2_design/__init__.py` matches
`!tools/**/__init__.py` and silently never redeploys** — the image keeps serving the old adapter,
with a green CI and no signal anywhere. That is the exact catastrophic mode the brief says to
weigh above an unnecessary rebuild.

`dockerfile_commands` is not an exotic escape hatch. It is the first-class Modal API for adding
Dockerfile instructions to an image, and it is the *only* remaining one that pulls arbitrary local
files in. The guard already forbids its four siblings (`add_local_dir`,
`add_local_python_source`, `copy_local_dir`, `copy_local_file`), plus `mounts=`, `modal.Mount`,
and `sys.path` manipulation. Omitting `dockerfile_commands` from that list is a gap in an
otherwise careful enumeration, not a judgement call.

### The comment in the workflow is currently false

`deploy-modal.yml:40-42` tells the next maintainer:

> `tests/test_deploy_paths_exclusions.py` fails if any app starts importing its package **or
> mounting local source**, which is what would make this exclusion unsafe.

M12 mounts local source and the test does not fail. Whatever else changes, that sentence must
either become true or stop claiming it.

---

## 7. What would clear this (all small)

1. **Add `dockerfile_commands` to the guard.** Cheapest correct form: treat it like a Dockerfile —
   when `_scan` sees `.dockerfile_commands([...])` with literal string elements, run those strings
   through the same `extract_copy_command_patterns` + `FilePatternMatcher` the Dockerfile test
   already uses, and fail on any `__init__.py` match. Falling back to a flat ban
   (add the name to `_FORBIDDEN_CALLS`) is also acceptable and is one line — no app uses it today,
   so nothing goes red.
2. **Widen `_INIT_PATHS`.** Replace the hand-built list of 9 slugs + top level with
   `sorted((_REPO/"tools").glob("**/__init__.py"))` (23 paths today). Closes M5 and stops the set
   drifting as tools are added. One line.
3. **Optional, and it retires the one risk the builder left unguarded** (§8): make the premise
   itself a test instead of a comment. My probe is ~40 lines — per app, in a subprocess, assert
   `FunctionInfo._type is FunctionInfoType.FILE`, `module.__package__ == ""`, and that the union
   of `get_entrypoint_mount()` + image `_mount_layers` + every `context_mount_function()` contains
   no `__init__.py`. That is behavioural rather than syntactic, so it survives *any* future spelling
   — including the Modal-version risk that AST scanning structurally cannot see. It costs a few
   seconds of subprocess time.
4. Consider noting that `test_dockerfile_copies_no_adapter_package` asserts nothing for the four
   COPY-less Dockerfiles, so a green run is not read as 8/8 coverage.

None of these touch the workflow change itself, which is correct as written.

---

## 8. The seven-risk list, and the `modal>=1.4,<2` bound

The builder lists one unguarded risk: Modal changing script-mode resolution so `modal_app.py`
loads as a package, `FunctionInfo` flips to `PACKAGE`, and
`get_entrypoint_mount()` returns `_Mount._from_local_python_packages("tools")` — i.e. **the whole
`tools` package**, every `__init__.py` included (`function_utils.py:330-336`). The bound offered is
the `modal>=1.4,<2` pin.

**That bound is not adequate**, for three reasons I can point at in source:

* The behaviour relied on is `spec_from_file_location` leaving `__package__ == ""`, consumed by
  `if getattr(module, "__package__", None)` at `function_utils.py:180`. That is an **implementation
  detail of `import_file_or_module`**, not a documented public contract. Nothing stops it moving in
  a 1.x **minor**, which `>=1.4,<2` accepts.
* CI runs `pip install 'modal>=1.4,<2'` **fresh on every deploy**, so the client can change under
  the repo with no commit and no review. The review venv happens to resolve 1.4.2 and
  `requirements.txt` carries the same `<2.0` bound, so guard and deploy at least share a
  constraint — but neither pins a version, and the resolutions are taken at different times.
* Most importantly: **the guard cannot see this class of change at all.** Every one of its 21
  tests is either YAML parsing or `ast`/regex over *repo source*. Not one test executes Modal.
  If Modal flipped script-mode to package-mode tomorrow, all 21 stay green, the exclusion becomes
  unsafe, and nothing anywhere goes red. Fix 3 in §7 is the only thing that closes it, and it is
  the same code I already ran.

### Belongs on the list and is not there

* **`.dockerfile_commands()` with a COPY** — §6. Currently unguarded *and* not disclosed.
* **A Dockerfile COPYing a non-deployed tool's `__init__.py`** (M5) — unguarded and not disclosed.
* **`ADD`** — Modal's parser ignores it, so an `ADD` of an `__init__.py` breaks the build rather
  than shipping the file. Safe today, but it is a divergence between "what the Dockerfile says" and
  "what Modal uploads" that is worth one line of comment, because the *next* Modal release could
  start honouring `ADD` and turn it into an M12-shaped hole silently.
* **Mitigation worth recording on the other side:** `workflow_dispatch:` is present on the
  workflow, so a missed deploy is always recoverable by hand once noticed. That does not make a
  silent miss acceptable, but it bounds the blast radius.

### The `**`-matches-empty question

The builder flags uncertainty over whether `tools/**/__init__.py` also excludes the top-level
`tools/__init__.py`, and judges it harmless either way. **I agree, and I can now say why
concretely rather than by assumption:** `tools/__init__.py` is a 6-line module containing only a
docstring — no code, no constants — and my probe confirms it is in **no** app's upload set. If it
*is* excluded, editing it cannot change a container, so nothing is lost. If it is *not* excluded,
editing it rebuilds nine images unnecessarily — wasteful, never wrong. Genuinely harmless in both
directions. I could not test GitHub's matcher on the empty-`**` case and am not claiming to have.

The guard's `_INIT_PATHS` already tests both a top-level and a per-slug path against the
Dockerfile matcher, with a comment saying `**` handling differs between them — that instinct is
right; fix 2 in §7 generalises it.

---

## 9. Summary

| claim under review | status |
|---|---|
| base 5262/20, head 5283/20, +21 = exactly the new file | **CONFIRMED [RUN]** |
| all nine: `__package__ == ''`, `FunctionInfoType.FILE`, entrypoint mount = one own file | **CONFIRMED [RUN]**, independently, incl. both `esmfold2_design` functions |
| no Dockerfile COPY pattern matches any `__init__.py` | **CONFIRMED [RUN]**, against all 23 rather than 10 |
| zero local-package imports in all nine `modal_app.py` + nine `run_pipeline.py` | **CONFIRMED [RUN]** |
| modal 1.4.2 has no automount | **CONFIRMED [RUN]** |
| repo root is on `sys.path` at deploy time | **CONFIRMED [RUN]** — and so is `tools/<slug>/` |
| the exclusion is safe **as of `5b9f9bc`** | **CONFIRMED [RUN]** |
| the pattern shape works | **CONFIRMED [RUN]** — 3 real meta.py-only pushes to main, 0 deploy runs; builder undersold its own evidence |
| the guard "fails if any app starts … mounting local source" | **FALSE [RUN]** — M12 / M5 |
| `modal>=1.4,<2` adequately bounds the one unguarded risk | **NO [REASONED]** — and no test executes Modal at all |

The workflow change is right. The guard around it is 2-4 lines short of matching what the workflow
comment promises, and the shortfall is on the silent-stale-deploy side. Close those and this is a
clear merge.
