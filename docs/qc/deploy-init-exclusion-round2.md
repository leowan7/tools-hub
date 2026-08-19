# QC round 2 — `!tools/**/__init__.py` deploy-trigger exclusion (PR #157)

## Verdict: **BLOCKED**

The workflow change is still right, and I re-derived that independently rather than
inheriting round 1's answer. Round 1's two blockers are genuinely closed, each caught twice
exactly as claimed. It is blocked because I found **two more ways to bake
`tools/<slug>/__init__.py` into a GPU image with the entire suite green** — the same
silent-stale-deploy failure mode round 1 blocked on, reached through two doors the new guard
leaves open. Both fixes are one line each. See §8.

Reviewer: independent QC agent; did not build this, did not run round 1.
Reviewed SHA: `96d62d03745c15f8e735f9959c7a6a7a61cb1f42` (`chore/skip-deploy-on-init-py`).
Base: `origin/main` = `48b4b71eedd2f791142ee4d020ee977a6961a6be`. Trunk had not moved.
Modal in the review venv: **1.4.2**. `requirements.txt` pins `modal>=1.4,<2.0`.

Every claim is tagged **[RUN]** (executed, output reproduced) or **[REASONED]** (argued from
source I read but could not execute). Nothing is reported as verified that I could not run.

---

## 1. Baselines — measured myself, both match the claim exactly [RUN]

Exact command, from each worktree root, **no path argument**:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| tree | SHA | result | exit |
|---|---|---|---|
| base | `48b4b71` | **5262 passed, 20 skipped** in 223.89s | 0 |
| head | `96d62d0` | **5296 passed, 20 skipped** in 198.80s | 0 |

Delta **+34 passed, +0 skipped**. Claim **CONFIRMED**.

### Verified by node id, not just by count [RUN]

`pytest --collect-only -q` on each tree, sorted, `comm`'d:

* base collects **5282** ids, head **5316**.
* **35 head-only**, **1 base-only**.
* The single base-only id and one of the head-only ids are the *same test* with a different
  random UUID baked into its parametrize id:
  `tests/test_scout_anonymous_access.py::TestStillGated::test_feasibility_get_requires_login[/scout/feasibility/download/<uuid>]`.
  That is a wash, not a moved test.
* The remaining **34 head-only ids are all in `tests/test_deploy_paths_exclusions.py`**, which
  `--collect-only` on that file confirms collects exactly 34.

So the delta is the whole of the new file and nothing else moved. The 21 → 34 within-file
growth is also confirmed (round 1 measured 21 at `5b9f9bc`).

---

## 2. Round-1's two blockers — reproduced, and both are CLOSED [RUN]

Both applied in a throwaway worktree, proven present with `git diff --unified=0` **before**
any conclusion, then attributed by failing test name. The builder's "caught twice" claim holds
for both: once syntactically, once by the behavioural probe.

| round-1 blocker | mutation | landed | failing tests |
|---|---|---|---|
| (a) `.dockerfile_commands([...COPY...])` | `.dockerfile_commands(["COPY tools/esmfold2_design/__init__.py /root/x.py"])` into `esmfold2_design/modal_app.py` (`@@ -250,0 +251 @@`) | yes | `test_modal_app_stays_self_contained[esmfold2_design]` **and** `test_modal_really_uploads_no_adapter_package[esmfold2_design]` |
| (b) `_INIT_PATHS` enumerated 10 | `COPY tools/pxdesign/__init__.py /root/x.py` into **af2**'s `Dockerfile.modal` (`@@ -96,0 +97,2 @@`) | yes | `test_dockerfile_copies_no_adapter_package[af2]` **and** `test_modal_really_uploads_no_adapter_package[af2]` |

`_INIT_PATHS` is now `(_REPO/"tools").glob("**/__init__.py")` — 22 paths, not 10 — and
`test_every_tools_init_py_is_globbed` asserts `len(found) > len(required)`, which is a real
floor: narrowing it back to the enumeration fails **two** tests (§5, NF-A).

---

## 3. The premise itself — re-derived independently, and it still HOLDS [RUN]

I did not take round 1's table. I re-ran the probe across all nine apps at `96d62d0`. No
`__init__.py` appears in any app's resolved upload set. Spot values (mpnn):

```
entrypoint_mount : ['tools/mpnn/modal_app.py']
spec_mounts      : ['tools/mpnn/modal_app.py']
image_files      : ['static/example/1HEW.pdb', 'tools/mpnn/run_pipeline.py']
image_layers     : 4
```

Modal-source checks I ran rather than assumed:

* `_create_context_mount` (`modal/image.py:278-303`): patterns from
  `extract_copy_command_patterns`; **`None` if there are none**. The `ignore_fn` is applied as
  `if not include_fn(source) or ignore_fn(source): return True` — it can only ever **narrow**.
  So a `.dockerignore` or an `ignore=` **structurally cannot add a file**, and the guard's
  pattern-only match is conservative (over-inclusive), i.e. it errs toward a false red, never a
  false green. [RUN, source read + empirically: §5 N10.]
* `extract_copy_command_patterns` (`modal/_utils/docker_utils.py`): `COPY` only, case-insensitive;
  handles line continuations; **skips `COPY --from=`**; and **raises `InvalidError` on a `$` in a
  source**, so a build-arg `COPY ${VAR}` is loud on both sides (§5 N4).

---

## 4. Two REAL holes: `__init__.py` into a GPU image, full suite green

These are the blockers. Both are the catastrophic direction — silent staleness, not an
unnecessary rebuild.

### 4.1 HOLE A — `dockerfile_commands(..., context_files=...)` [RUN]

`context_files` is a documented kwarg of `Image.dockerfile_commands`
(`modal/image.py:1640`). It is **not a mount**. At `image.py:591` Modal does:

```python
for filename, path in dockerfile.context_files.items():
    with open(path, "rb") as f:
        context_file_pb2s.append(api_pb2.ImageContextFile(filename=filename, data=f.read()))
```

— it reads arbitrary local bytes and ships them into the build context, where a `COPY` names
them by their in-context filename. This is Modal's own mechanism for `/modal_requirements.txt`,
not an exotic path.

Mutation, landed (`git diff --unified=0` → `@@ -131,0 +132,4 @@ tools/mpnn/modal_app.py`):

```python
    .dockerfile_commands(
        ["COPY /.adapter.py /root/adapter.py"],
        context_files={"/.adapter.py": "tools/mpnn/__init__.py"},
    )
```

Result:

```
GUARD FILE : 34 passed in 13.27s
FULL SUITE : 5296 passed, 20 skipped        (exit 0)   <- see §4.3
PROBE      : image_files = ['static/example/1HEW.pdb', 'tools/mpnn/run_pipeline.py']
             (no __init__.py anywhere)
REALITY    : DockerfileSpec layer0
             context_files = {'/.adapter.py': 'tools/mpnn/__init__.py'}
             commands      = ['FROM base', 'COPY /.adapter.py /root/adapter.py']
```

I obtained the `DockerfileSpec` by calling the `dockerfile_function` free variable of the
image's `_from_args` closure directly — i.e. Modal's own build description, not my model of it.

**Why both checks miss it.** The AST scan bans `context_dir=` but not `context_files=`
(`_FORBIDDEN_KWARGS = {"mounts", "context_dir"}`), and the COPY pattern it does scan is
`/.adapter.py`, which correctly matches no `tools/**/__init__.py`. The probe walks
`_mount_layers`, `context_mount_function()` and the `mount` freevar — `context_files` is none of
those, so it is **structurally invisible** to it.

**Severity: blocking.** The first edit touches `modal_app.py`, so it deploys once and the
adapter lands in the image. Every subsequent edit to `tools/mpnn/__init__.py` matches
`!tools/**/__init__.py` and never redeploys. Green CI, no signal, stale GPU image. This is
round-1 blocker (a) reached through the sibling kwarg of the very same API.

**Fix: one line** — add `"context_files"` to `_FORBIDDEN_KWARGS`. No app uses it today, so
nothing goes red.

### 4.2 HOLE B — the deploy-step pin is defeated by a leftover comment [RUN]

The pin (`test_deploy_step_still_passes_a_FILE_path_to_modal_deploy`) joins the `run` blocks of
**every step in the deploy job** and substring-matches `modal deploy "$app_file"`. It does not
strip shell comments and does not check the deploy command itself.

| # | mutation to `.github/workflows/deploy-modal.yml` | landed | result |
|---|---|---|---|
| W1 | swap the deploy line to `modal deploy -m "tools.${{ matrix.app }}.modal_app"` | yes | **CAUGHT** — `test_deploy_step_still_passes_a_FILE_path_to_modal_deploy` |
| W2 | same swap, but leave `# was: modal deploy "$app_file" \| tee ...` on the line above | yes | **34 passed — HOLE** |
| W3 | echo the literal in the "Verify Modal auth" step | yes | 34 passed (shows the substring is satisfiable from any step in the job) |

W2 is the single most ordinary way a person edits a command line: comment out the old, add the
new. The pin passes; the deploy is now module mode.

**What module mode actually does — verified independently, as the brief asked** [RUN]:

```
import_file_or_module(ImportRef("tools.mpnn.modal_app", use_module_mode=True))
  __package__      = 'tools.mpnn'
  info_type        = PACKAGE
  entrypoint mount = 101 files, of which 22 are __init__.py
                     (tools/__init__.py, all nine apps, bindcraft, boltzgen,
                      developability/{,data,dimensions}, library_planner, platform_api, ...)
```

So the builder's claim 3(a) is **exactly right**, and worse than "the probe would still report
FILE": the probe hardcodes `use_module_mode=False`, so it reports FILE *by construction* no
matter what the workflow does. The literal-substring pin is genuinely the only guard, and W2
walks past it.

**Is the substring pin the right mechanism?** The idea is right — script-vs-package mode is a
property of the *invocation*, and nothing else in the repo can see it. The implementation is
brittle in the one direction that matters: it can only ever be satisfied, never contradicted, so
any leftover text that contains the literal disarms it. **Fix: one line** — either strip
`#`-comment lines from `runs` before matching, or add
`assert " -m " not in runs and "--module" not in runs`. The second also catches W3.

### 4.3 Full-suite confirmation of Hole A [RUN]

Guard-file-only greens can hide a failure elsewhere in the suite. With the §4.1 mutation applied
I ran the **full** suite, no path argument: **5296 passed, 20 skipped, exit 0** — byte-identical
to the clean head baseline. Nothing anywhere in the repo notices.

---

## 5. Mutation table — 24 mutations, every one verified landed before any conclusion [RUN]

All in a throwaway worktree (`r2mut`, since discarded); nothing under `tools/` is in what I push.
Each was proven present with `git diff --unified=0` **plus** `git status --porcelain` before the
guard was run, and reverted after. Guard command:
`pytest -q tests/test_deploy_paths_exclusions.py -rf`.

**"Novel" = not in round 1's table and not in the builder's set.** 15 of the 24 are novel.

| # | mutation | novel | landed | caught | failing test(s) |
|---|---|---|---|---|---|
| **M20** | `.dockerfile_commands(["COPY /.adapter.py …"], context_files={"/.adapter.py": "tools/mpnn/__init__.py"})` | **yes** | yes | **NO** | — **HOLE A** |
| **W2** | deploy step → `-m`, old line left as a `#` comment | **yes** | yes | **NO** | — **HOLE B** |
| W1 | deploy step → `-m`, no leftover | yes | yes | yes | `test_deploy_step_still_passes_a_FILE_path_to_modal_deploy` |
| W3 | deploy literal echoed in another step | yes | yes | n/a | 34 passed (substring satisfiable job-wide) |
| **P2** | probe: kill **only** the base-image recursion in `_image_chain` | **yes** | yes | **NO** | — floor gap, §6 |
| P1 | probe: `_closure()` → `{}` (the builder's named Modal-rename risk) | no | yes | yes | all 9 `test_modal_really_uploads_no_adapter_package[*]` |
| P3 | probe: `_mount_files()` → `[]` | yes | yes | yes | all 9 `test_modal_really_uploads_no_adapter_package[*]` |
| R1a | `.dockerfile_commands(["COPY tools/esmfold2_design/__init__.py …"])` | no | yes | yes | `test_modal_app_stays_self_contained[esmfold2_design]`, `test_modal_really_uploads_no_adapter_package[esmfold2_design]` |
| R1b | cross-slug `COPY tools/pxdesign/__init__.py` into af2's Dockerfile | no | yes | yes | `test_dockerfile_copies_no_adapter_package[af2]`, `test_modal_really_uploads_no_adapter_package[af2]` |
| N1 | `dockerfile_commands(_CMDS)` where `_CMDS` is a module-level **list** constant | yes | yes | yes | `test_modal_app_stays_self_contained[mpnn]` (opaque-arg branch), `test_modal_really_uploads_no_adapter_package[mpnn]` |
| N2 | `add_local_file(local_path="tools/mpnn/__init__.py", remote_path=…)` — **kwargs only, `node.args` empty** | yes | yes | yes | `test_modal_app_stays_self_contained[mpnn]`, `test_modal_really_uploads_no_adapter_package[mpnn]` |
| N3 | `getattr(image, "add_local_dir")("tools/mpnn", …)` — evades AST name matching | yes | yes | yes | **only** `test_modal_really_uploads_no_adapter_package[mpnn]` — the probe earns its keep |
| N4 | Dockerfile `ARG SRC=tools/mpnn/__init__.py` + `COPY ${SRC} /root/x.py` | yes | yes | yes | `test_dockerfile_copies_no_adapter_package[mpnn]`, `test_modal_really_uploads_no_adapter_package[mpnn]` (Modal raises `InvalidError` on `$`) |
| N5 | multi-stage: `FROM scratch AS adapter` + `COPY tools/mpnn/__init__.py` + `COPY --from=adapter` | yes | yes | yes | `test_dockerfile_copies_no_adapter_package[mpnn]`, `test_modal_really_uploads_no_adapter_package[mpnn]` |
| N6 | `ADD tools/mpnn/__init__.py /root/x.py` | no | yes | no | — harmless, re-derived §7 |
| N7 | `importlib.import_module("tools.mpnn")` | no | yes | no | — harmless, re-derived §7 |
| N8 | tenth app (`bindcraft`, **no Dockerfile**) added to the matrix | yes | yes | yes | 6 tests: `test_deploy_matrix_is_the_nine_known_apps`, `test_dockerfiles_are_discovered`, `test_every_app_has_the_excluded_init_and_a_modal_app`, `test_every_discovered_dockerfile_is_the_one_its_app_uses`, `test_modal_app_stays_self_contained[bindcraft]`, `test_modal_really_uploads_no_adapter_package[bindcraft]` |
| N9 | symlink `run_pipeline.py` → `__init__.py` | yes | **NOT RUN** | — | `WinError 1314` (no symlink privilege). §7 |
| N10 | `Dockerfile.modal.dockerignore` excluding `**/__init__.py`, alongside a real adapter COPY | yes | yes | yes | `test_dockerfile_copies_no_adapter_package[mpnn]`, `test_modal_really_uploads_no_adapter_package[mpnn]` — ignore did **not** narrow the upload set; no hole |
| F1 | FLOOR: matrix shrunk to one app | no | yes | yes | `test_deploy_matrix_is_the_nine_known_apps`, `test_dockerfiles_are_discovered`, `test_every_discovered_dockerfile_is_the_one_its_app_uses` (collection 34 → 18) |
| F2 | FLOOR: rename `tools/iggm/Dockerfile.modal` away | no | yes | yes | `test_dockerfiles_are_discovered`, `test_every_discovered_dockerfile_is_the_one_its_app_uses`, `test_modal_really_uploads_no_adapter_package[iggm]` |
| F3c | FLOOR: strip `mpnn/modal_app.py` to 2 imports | no | yes | yes | `test_modal_app_stays_self_contained[mpnn]` |
| F4b | FLOOR: delete the only `add_local_file` call | no | yes | yes | `test_modal_app_stays_self_contained[mpnn]`, `test_modal_really_uploads_no_adapter_package[mpnn]` |
| NF-A | FLOOR: narrow `_INIT_PATHS` back to the 10-path enumeration | yes | yes | yes | `test_every_tools_init_py_is_globbed`, `test_copy_scan_actually_detects_an_adapter_copy` |
| NF-B | FLOOR: make `_init_paths_pulled()` always return `[]` | yes | yes | yes | `test_copy_scan_actually_detects_an_adapter_copy` |
| NF-C | FLOOR: probe reports zero registered functions | yes | yes | yes | all 9 `test_modal_really_uploads_no_adapter_package[*]` |

**21 caught, 2 holes, 1 not runnable.**

### Mutation-harness safety — verified, it does work [RUN]

My driver asserts the anchor occurs **exactly once** before writing, and separately refuses to
proceed if `git diff --unified=0` comes back empty. Both fired during this round:

* `count=0` on a deliberately absent anchor → `!! ANCHOR NOT UNIQUE (count=0) -- NOT APPLIED`,
  file untouched (twice).
* an accidental identity replacement → `landed=False … !! diff empty, aborting`.

So neither of this repo's two historical silent-mutation failures (missed sed pattern; Windows
em-dash encoding mismatch) could have produced a false "caught" in this table.

---

## 6. The behavioural probe — genuine, but its floor has a blind spot [RUN]

**It is genuine.** The probe does reach image internals, and the floor is not decorative:

* **P1** — `_closure()` → `{}`, the builder's own named risk (Modal renaming free variables):
  **all 9 behavioural tests go red.** The floor fires. Claimed and true.
* **P3** — `_mount_files()` → `[]`: all 9 red.
* **NF-C** — zero registered functions: all 9 red.
* **N3** — a `getattr()`-obfuscated `add_local_dir` that the AST scan cannot see is caught by
  the probe **alone**. The two-layer design pays for itself.

**Runs without credentials** [RUN]. This machine has a `~/.modal.toml`, so a plain run proves
nothing. With `MODAL_CONFIG_PATH` pointed at an empty file and `MODAL_TOKEN_ID` /
`MODAL_TOKEN_SECRET` blanked: **34 passed in 8.26s**, and the standalone probe still emits a
full payload. CI (`pytest.yml`) wires in no secrets and says so explicitly. Confirmed.

**Runtime, measured myself** [RUN]: the 9 probe tests take **8.34s** total, 0.89-1.07s each.
Matches the "~1s per app, ~10s for the nine" claim.

**Not skipped, xfailed or conditionally disabled** [RUN]. `grep` over both new files finds only
`@pytest.mark.parametrize` — no `skip`, `skipif`, `xfail`, or `importorskip`. There is no
autouse conftest fixture that could skip them. The guard cannot be silently turned off.

### The blind spot: partial narrowing does not trip the floor

The floor requires `tools/<app>/modal_app.py` and `tools/<app>/run_pipeline.py` to appear in the
upload set. Both live on the **outermost** image layer / entrypoint mount. So breaking only the
*recursion* into base images leaves the floor satisfied while the walk goes blind to everything
underneath:

**P2** — replace the two `base_images` / `base_image` recursion lines in `_image_chain` with
`return` (landed, `@@ -84,3 +84 @@`):

```
guard: 34 passed        <- floor does NOT fire
probe: image_layers_walked  4 -> 1
       image_files ['static/example/1HEW.pdb','tools/…/run_pipeline.py'] -> ['tools/…/run_pipeline.py']
```

Combined with a real hole (P2 + R1b's cross-slug Dockerfile COPY), the probe reports
`image_layers_walked: 1` and the smuggled `tools/pxdesign/__init__.py` vanishes from its view —
**only the syntactic Dockerfile test still fires** (1 failure instead of 2). Defence-in-depth
held in that particular case, but the probe's unique value (seeing a *Modal-side* change no AST
scan can) had silently evaporated with nothing red.

Not a blocker on its own — it needs Modal to rename those two specific free variables, and the
more likely wholesale rename **is** caught by P1. Worth one line: assert
`image_layers_walked >= 2`, or require a known deep-layer file (`static/example/*` is on a deeper
layer for af2, colabfold, esmfold, mpnn).

---

## 7. `ADD` and dynamic imports — re-derived, and the low rating is correct [RUN]

I drove the probe myself rather than trusting the builder's note. Upload sets, mpnn:

```
CLEAN                          : ['static/example/1HEW.pdb', 'tools/mpnn/modal_app.py', 'tools/mpnn/run_pipeline.py']
+ ADD tools/mpnn/__init__.py   : identical   (UNCHANGED = True)
+ importlib.import_module(...) : identical   (UNCHANGED = True)
```

Byte-identical in both cases, so neither can produce a stale image — the premise the rating rests
on is true.

* **`ADD`**: the file is in no context mount, so the `ADD` refers to a path that is not in the
  build context and the docker build fails — **on the very deploy the Dockerfile edit triggers**,
  since `Dockerfile.modal` is not excluded. The "upload set unchanged" half is [RUN]; the "build
  therefore errors" half is standard Docker semantics, [REASONED] — I did not run a real Modal
  build (needs credentials and spend).
* **`importlib` / `__import__`**: package not uploaded → loud in-container `ImportError` on every
  run. [RUN for the upload set.]

**Judgement: leaving them open is right, and naming them in the workflow comment is the correct
treatment** — they are documented, they are loud, and guarding them would add AST machinery for
zero safety. My one caveat is the count, not the content: the comment says "**Two** known gaps,
both LOUD" (§9), and after §4 that number is wrong and the "both LOUD" reassurance no longer
covers the set.

**N9 (symlink) — NOT RUN.** `os.symlink` fails with `WinError 1314` on this box. [REASONED
only]: `_MountEntry.get_files_to_upload()` yields the path it walked, so a `run_pipeline.py`
symlinked at `__init__.py` would report as `run_pipeline.py` and both layers would stay green
while the shipped bytes are the adapter's. I rate it negligible: creating the symlink is itself a
mode change to `run_pipeline.py` (100644 → 120000) that triggers a deploy, and no maintainer does
this by accident. Flagging it as unverified, not as a finding.

---

## 8. What would clear this — two lines

1. **Ban `context_files=`.** Add `"context_files"` to `_FORBIDDEN_KWARGS` in
   `tests/test_deploy_paths_exclusions.py`. It ships arbitrary local bytes and is not a mount, so
   neither existing layer can see it. No app uses it, so nothing goes red. *(A stricter variant —
   resolving its literal values through `_resolve` and matching them against `_INIT_PATHS` —
   is also fine, but the flat ban is one line and loses nothing today.)*
2. **Make the deploy-step pin contradictable.** In
   `test_deploy_step_still_passes_a_FILE_path_to_modal_deploy`, drop `#`-comment lines from
   `runs` before the substring match, **or** add
   `assert " -m " not in runs and "--module" not in runs`. Either closes W2; the second also
   closes W3.

Optional, and cheap:

3. Assert `image_layers_walked >= 2` (or require a deep-layer file) so §6's partial narrowing
   trips the floor rather than passing silently.
4. Correct the two false clauses in the workflow comment (§9).

None of these touch the workflow change itself, which is correct as written.

---

## 9. The workflow comment — mostly accurate, two clauses false [RUN]

Round 1 found it promised coverage the test lacked. It is much better now. Sentence by sentence:

| claim in the comment | status |
|---|---|
| "meta.py, example/ and `__init__.py` are WEB-TIER ONLY … an edit there cannot change a container" | **TRUE [RUN]** — probe over all nine, no `__init__.py` in any upload set |
| "(each only ships its own run_pipeline.py)" | **imprecise [RUN]** — four apps also ship `static/example/*` (see §10) |
| "The meta.py importers are `tools/pxdesign/__init__.py` and `tools/rfdiffusion/__init__.py`" | **TRUE [RUN]** — grep over all `tools/*/__init__.py` returns exactly those two |
| "loads that file in SCRIPT mode, so its `__package__` is `""` … (FunctionInfoType.FILE)" | **TRUE [RUN]** — all nine |
| "runs Modal's own loader over each of the nine apps in a subprocess and fails if the resulting upload set … contains any `__init__.py`" | **TRUE [RUN]** |
| "**so it catches a local-source mount however it is spelled**" | **FALSE [RUN]** — §4.1. `context_files=` ships local source without being a mount at all. This is the same overclaim, in the same sentence position, that round 1 blocked on. |
| "fails statically on a package import, on an `add_local_*`/`mounts=` of anything but the app's own run_pipeline.py, and on a COPY of any tools-side `__init__.py` from a `Dockerfile.modal` or from `.dockerfile_commands([...])`" | **TRUE [RUN]** — R1a, R1b, N1, N2, N4, N5 |
| "It also **pins** the Deploy step below to a FILE path" | **overstated [RUN]** — §4.2. W1 is caught; W2 is not. |
| "`modal deploy -m tools.<app>.modal_app` would load the same source in PACKAGE mode and mount the whole tools package" | **TRUE [RUN]** — 101 files, 22 of them `__init__.py` |
| "**Two** known gaps, both LOUD rather than silent, so neither can leave a stale image behind" | **content TRUE, count now FALSE [RUN]** — the two named gaps really are loud (§7), but §4 adds two silent ones |

---

## 10. Residual risk — the builder's list, judged, plus what is missing

### The Modal-version gap: real, but should **NOT** block

The builder names it: test and deploy are separate jobs that each install `modal>=1.4,<2`
independently, so a Modal release landing between a green CI run and a later deploy is
unbounded, and only an exact pin in both closes it. I verified the mechanics [RUN]:
`pytest.yml` installs `requirements-dev.txt` (→ `requirements.txt`'s `modal>=1.4,<2.0`);
`deploy-modal.yml` runs `pip install 'modal>=1.4,<2'` in its own job. Two independent
resolutions at two different times. The analysis is correct.

I judge it **not blocking**, for a reason that cuts the other way from round 1's framing:

* This exact risk **already exists on main**. `!tools/**/meta.py` and `!tools/**/example/**`
  shipped in `95abcd6` under the *same* script-mode premise, with **no** behavioural check at
  all. Nothing in the repo could detect a Modal-side flip today.
* This PR adds the first test in the repo that can. On this axis it is a strict improvement over
  trunk, not a new exposure. Blocking it for a pre-existing gap it partially closes would be
  backwards.
* An exact pin is a real change with its own cost (someone must bump it), and belongs in its own
  PR against both workflows — not smuggled into this one.

### Missing from the residual-risk list

* **`context_files=`** (§4.1) — silent, unguarded, undisclosed.
* **The pin's defeat by a leftover comment** (§4.2) — silent, undisclosed.
* **The probe's partial-narrowing blind spot** (§6) — undisclosed.
* **Non-`tools/` files that are baked into images and are on no trigger at all** [RUN].
  `static/example/{BPTI.fasta,ubiquitin.fasta,1HEW.pdb}` are `COPY`d by af2, colabfold, esmfold
  and mpnn and appear in those apps' real upload sets. The deploy trigger is `tools/**` plus the
  workflow file, so **editing them already changes four GPU images and never deploys**. This is
  **pre-existing on `48b4b71`, not introduced here**, so it cannot block this PR — but it is the
  identical failure class, it is not on any list, and `tests/test_deploy_paths_exclusions.py` is
  now the obvious home for it.

---

## 11. Operational note — merging redeploys all nine once: **TRUE**, and one-time [RUN]

Verified empirically rather than by reading the trigger:

* `.github/workflows/deploy-modal.yml` is on its own `push.paths` list, and this branch modifies
  it.
* Precedent: `95abcd6` (merge of #144) touched `.github/workflows/deploy-modal.yml` and **no**
  `tools/**` file (`git diff --name-only 95abcd6^1 95abcd6`). Its Deploy run has **9 matrix
  jobs**, all `success`. So a workflow-file-only change does trigger the full nine.
* **One-time, not recurring** [REASONED from the trigger + confirmed by the run history]: after
  the merge, the workflow file stops changing, and later pushes match only on non-excluded
  `tools/**` paths. The run list also shows the complement holding — `48b4b71` (main HEAD,
  `tools/boltzgen/meta.py` only) produced **no** deploy run.

Cost: one redundant build of nine images, once. Correctly characterised.

---

## 12. Summary

| claim under review | status |
|---|---|
| base 5262/20, head 5296/20, +34 = exactly the new file | **CONFIRMED [RUN]** — by count *and* node id |
| round-1 blocker (a) `.dockerfile_commands` COPY now caught | **CONFIRMED [RUN]** — caught twice |
| round-1 blocker (b) `_INIT_PATHS` now globbed | **CONFIRMED [RUN]** — caught twice, floor real |
| the probe reaches image internals, is not vacuous | **CONFIRMED [RUN]** — P1/P3/NF-C fire; N3 caught by probe alone |
| the probe runs without Modal credentials | **CONFIRMED [RUN]** — 34 passed with an empty config |
| probe runtime ~1s/app | **CONFIRMED [RUN]** — 8.34s for nine |
| probe never skipped/xfailed/disabled | **CONFIRMED [RUN]** |
| the probe's floor catches a narrowed walk | **PARTLY FALSE [RUN]** — total break yes, recursion-only break no |
| claim 3(a): `-m` loads in PACKAGE mode and mounts all of `tools` | **CONFIRMED [RUN]** — 101 files, 22 `__init__.py` |
| claim 3(b): `context_dir=` is banned | **CONFIRMED [RUN]** |
| the deploy-step pin is the right mechanism | **right idea, brittle implementation [RUN]** — W1 caught, W2 not |
| `ADD` + dynamic imports are low severity | **CONFIRMED [RUN]** — upload sets byte-identical |
| the workflow comment is accurate | **TWO CLAUSES FALSE [RUN]** — §9 |
| the exclusion is safe **as of `96d62d0`** | **CONFIRMED [RUN]** |
| the guard "catches a local-source mount however it is spelled" | **FALSE [RUN]** — `context_files=`, §4.1 |
| merging redeploys nine images once | **CONFIRMED [RUN]** — 9-job precedent at `95abcd6` |
| Modal-version drift should block | **NO [RUN + REASONED]** — pre-existing on main; this PR narrows it |

The workflow change is right, and the guard is substantially stronger than at round 1 — the two
blockers are properly closed and the behavioural probe is real, not decoration. It is still two
lines short, and both shortfalls are on the silent-stale-deploy side: an adapter can be baked
into a GPU image through `context_files=`, and the script-mode pin can be walked past by leaving
a comment behind. Close those and this is a clear merge.
