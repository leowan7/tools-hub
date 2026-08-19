# QC round 4 — `!tools/**/__init__.py` deploy-trigger exclusion (PR #157)

**SHA reviewed: `74461f1c5e34b2624b0ba980d71b4315dd727be2`** (branch `chore/skip-deploy-on-init-py`)
Trunk at review time: `origin/main` = `7fd180df35086cfc5da3710ff336024901d8e73b`.
Merged tree actually tested: `3bd4c39ad07a9a6adac8efa473cfecb5c6e30f28` (clean merge, no conflicts).
Reviewer: independent QC agent. Did not build this; did not run rounds 1–3.

## Verdict: **BLOCKED**

The headline version claim is **fully confirmed** — I rebuilt the 1.5.4 venv and reproduced 9
failed/28 passed at `c9bb54b` against 37 passed on 1.4.2, and head is 41/41 on both. Round 3's two
blockers are closed. The `context_files` double coverage is real and I proved the probe stands alone
with the AST scan disabled.

But **the fix for the version problem introduced a new regression in the catastrophic direction**:

> `Secret = modal.Image.from_dockerfile(...)` + `importlib.import_module("tools.mpnn")` +
> `Secret.run_function(_adapter.build_payload)` mounts **every** `tools/**/__init__.py` into the mpnn
> GPU image with the full suite green — **5303 passed, 20 skipped**, byte-identical to clean head, on
> **both** modal versions.

The new receiver exemption keys on the receiver's *name*, so binding an Image **instance** to a
variable called `Secret` (or `Volume`, `Dict`, … any of 22–25 identifiers) exempts `run_function` —
the one refused method the behavioural probe is structurally blind to. Measured at `c9bb54b` the same
code was **caught**; at `74461f1` it is not. Detail in §3.

Everything marked [RUN] was executed; [REASONED] was not.

---

## 0. Method

Four private worktrees under this session's scratchpad (`r4trunk`, `r4head`, `r4pre`, `r4merge`,
`r4mut`), never the main working tree. Two interpreters:

| interpreter | modal | stands for |
|---|---|---|
| `C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe` | **1.4.2** | a laptop |
| `<scratchpad>/venv154/Scripts/python.exe` (built for this review) | **1.5.4** | **CI** (`pip install 'modal>=1.4,<2'`) |

Suite command, from each worktree root, **no path argument**:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

---

## 1. The headline claim — **CONFIRMED, reproduced independently** [RUN]

`pip index versions modal` today: `INSTALLED: 1.4.2`, `LATEST: 1.5.4`. CI's
`pip install 'modal>=1.4,<2'` resolves to **1.5.4**.

```
1.4.2:  'from_name' in dir(modal.Image)  ->  False
1.5.4:  'from_name' in dir(modal.Image)  ->  True
```

All nine apps call `modal.Volume.from_name(...)` (15 call sites). `_IMAGE_API` matches on the
attribute NAME alone, so on 1.5.4 every one of them became a refused Image method.

**Pre-existing at `c9bb54b`, measured in a worktree at that commit:**

| interpreter | `pytest -q tests/test_deploy_paths_exclusions.py` at `c9bb54b` |
|---|---|
| modal **1.4.2** | **37 passed** in 16.61s |
| modal **1.5.4** | **9 failed, 28 passed** in 14.69s |

The nine failures are exactly `test_modal_app_stays_self_contained[<app>]` for all nine apps, e.g.

```
AssertionError: opendde/modal_app.py line 92: calls .from_name(), which is not in
_ALLOWED_IMAGE_CALLS; line 93: calls .from_name(), which is not in _ALLOWED_IMAGE_CALLS
```

The builder's numbers are exact (9 failed/28 passed vs 37 passed). **This was a genuine, live,
100%-of-apps CI outage that was invisible to every local run**, and it was introduced by
`f096888`/`339fb6f` earlier on this same branch — i.e. it is pre-existing relative to `74461f1`
but **not** pre-existing relative to trunk. Trunk has no such file at all.

**At head `74461f1`, both versions are green:**

| interpreter | guard file at `74461f1` |
|---|---|
| modal **1.4.2** | **41 passed** in 16.59s |
| modal **1.5.4** | **41 passed** in 11.63s |

No divergence anywhere in the file across the two versions. That is the first time this file has
been run on the client CI actually installs.

### 1a. The fix keys on the receiver, not on a widened allowlist — **CONFIRMED** [RUN]

`_ALLOWED_IMAGE_CALLS` is byte-identical to `c9bb54b` (11 entries, no `from_name`). The fix is
`_MODAL_NON_IMAGE_CLASSES` + `_receiver_names_another_modal_class`. Driving `_scan` directly:

| spelling | 1.4.2 | 1.5.4 | wanted |
|---|---|---|---|
| `modal.Volume.from_name("x", create_if_missing=True)` | green | green | green |
| `modal.Secret.from_name("x")` | green | green | green |
| `modal.Image.from_name("x")` | green¹ | **CAUGHT** | red on 1.5+ |
| `image.from_name("x")` (unidentifiable receiver) | green¹ | **CAUGHT** | red on 1.5+ |

¹ vacuously green on 1.4.2 — `from_name` is not on `modal.Image` there at all. The guard's own
`test_non_image_receiver_exemption_is_narrow` handles this correctly: it picks the refused method
off `sorted(_IMAGE_API - _ALLOWED_IMAGE_CALLS - _FORBIDDEN_CALLS)[0]` rather than naming
`from_name`, so it is non-vacuous on both versions. That is the right construction and it is the
specific mistake that caused the outage.

**The genuine `Image.from_name` is still refused on 1.5.4, through both spellings.** Verified.

`"image" not in _MODAL_NON_IMAGE_CLASSES` and `"Image" not in _MODAL_NON_IMAGE_CLASSES` on both
versions — the `isinstance(..., type)` restriction does keep the lowercase `modal.image`
submodule out, so a receiver spelled `image` stays checked. Verified.

---

## 2. Baselines — measured, and reconciled by node id [RUN]

| state | commit | result | wall |
|---|---|---|---|
| **trunk** | `7fd180d` | **5273 passed, 20 skipped** | 301.46s |
| **branch head** | `74461f1` | **5303 passed, 20 skipped** | 264.67s |
| **merge(head, trunk)** — what lands | `3bd4c39` | _(see §2a)_ | |

All exit 0. Every claimed number matches.

Collection counts: trunk **5293** ids, head **5323** ids.

```
comm -13 ids_trunk ids_head | sed 's/::.*//' | uniq -c
     41 tests/test_deploy_paths_exclusions.py
      1 tests/test_scout_anonymous_access.py     <- UUID parametrize id, churns every run
comm -23 ids_trunk ids_head | sed 's/::.*//' | uniq -c
      1 tests/test_scout_anonymous_access.py     <- same id churning
     11 tests/test_scout_interface_competition.py
```

So head = trunk **+41** (all in the new guard file) **−11** (PR #158, on trunk but not on the
branch). 5273 + 41 − 11 = **5303** ✅. Merged predicts 5273 + 41 = **5314** ✅, matching the
builder's claim of 5314/20 for CI.

The `-11` explanation is correct and I verified the file name, not just the count.

---

## 3. THE BLOCKER — the new receiver exemption reopens `run_function` [RUN]

> **`importlib.import_module("tools.mpnn")` + `Secret = <an Image>` + `Secret.run_function(...)`
> mounts EVERY `tools/**/__init__.py` into the mpnn GPU image with the full suite green —
> 5303 passed, 20 skipped, byte-identical to clean head, on both modal versions.**

### 3.1 What the exemption actually exempts

`_receiver_names_another_modal_class` returns True whenever the receiver's **name** is in
`_MODAL_NON_IMAGE_CLASSES`, which is
`{n for n in dir(modal) if isinstance(getattr(modal, n), type)} - {"Image"}`:

| modal | size | members |
|---|---|---|
| 1.4.2 | 22 | App, Client, CloudBucketMount, Cls, Cron, Dict, Error, FilePatternMatcher, Function, FunctionCall, NetworkFileSystem, Period, Probe, Proxy, Queue, Retries, Sandbox, SandboxSnapshot, SchedulerPlacement, Secret, Tunnel, Volume |
| 1.5.4 | 25 | the same plus **Environment, Server, Workspace** |

The check reads the receiver's *name*, never its *value*. So **any local variable named after one of
those 22–25 identifiers exempts every non-allowlisted Image method called on it** — including the
six that move local bytes. Confirmed by driving `_scan` directly, identical on both versions:

| spelling | `_scan` verdict |
|---|---|
| `Volume = modal.Image` … `Volume.debian_slim().pip_install_from_requirements(a)` | **CAUGHT** (receiver is a `Call`, not a Name) |
| `Volume = modal.Image.from_dockerfile(...)` … `Volume.uv_sync(a)` | **MISSED** |
| `Secret = <Image>` … `Secret.pip_install_from_pyproject(a)` | **MISSED** |
| `Secret = <Image>` … `Secret.run_function(f)` | **MISSED** |
| `Dict = <Image>` … `Dict.uv_pip_install(requirements=[a])` | **MISSED** |
| `_h.Volume.pip_install_from_requirements(a)` (attribute chain) | **MISSED** |
| `_Secret = <Image>` … `_Secret.pip_install_from_requirements(a)` | CAUGHT (leading underscore is not in the set) |

The PR body's mutation row **N6** — "abuse the new exemption: `Volume = modal.Image`, then ship an
adapter through it" — tested the **first** row of that table, the one shape the exemption does *not*
reach. Binding an Image *instance* rather than the class is what walks through, and it is also the
more natural spelling.

### 3.2 Why that matters: it is `run_function` that gets through

Five of the six exemptable methods route through `DockerfileSpec.context_files`, and the probe reads
that channel, so they stay double-covered (see Claim 4). **`run_function` does not.** The module
docstring says so itself: it is refused precisely because `_image_files` never walks
`build_function`.

Before this PR's exemption, `run_function` was refused unconditionally. Measured on the two commits,
same source string:

```
c9bb54b (pre-exemption):  _scan violations: [(4, 'calls .run_function(), which is not in _ALLOWED_IMAGE_CALLS')]
74461f1 (this head):      _scan violations: []
```

**This PR's own fix is what opened it.** It is a regression introduced by `74461f1`, not a
pre-existing gap.

### 3.3 Composed with the documented `importlib` gap, the adapter really ships

`run_function(f)` alone is harmless, and I verified that rather than assuming it: with `_bake`
defined in `modal_app.py`, the guard is 41/41 green **and** the chain shows the build function is
script-mode, mounting only `modal_app.py`:

```
layer 1: !! build_function present: Function(_bake)
         info._type = FILE
         build_function entrypoint mount -> ['tools/mpnn/modal_app.py']
```

That is exactly the docstring's stated reason it is safe. But the docstring also accepts
`importlib.import_module("tools.x")` as a gap, on the grounds that it "does not pull the package into
the image (verified: the upload set is unchanged)". **That verification holds for importlib alone and
fails for the composition.** Landed on `tools/mpnn/modal_app.py`, `git diff --unified=0`:

```
+import importlib
+_adapter = importlib.import_module("tools.mpnn")
+Secret = modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
-    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
+    Secret.run_function(_adapter.build_payload)
```

Driving the real image chain (`tests/_deploy_upload_probe` internals, plus the `build_function`
freevar the probe does not walk):

```
layer 1: !! build_function present: Function(build_payload)
         info._type = PACKAGE
         build_function entrypoint mount -> ['tools/base.py', 'tools/__init__.py',
           'tools/af2/__init__.py', 'tools/boltz2/__init__.py', 'tools/colabfold/__init__.py',
           'tools/esmfold/__init__.py', 'tools/esmfold2_design/__init__.py', 'tools/iggm/__init__.py',
           'tools/mpnn/__init__.py', 'tools/opendde/__init__.py', 'tools/proteina/__init__.py',
           ... 104 files, the whole tools package ...]
```

`info._type` is **PACKAGE**, not FILE — `build_payload` is defined in `tools/mpnn/__init__.py`, whose
`__package__` is `tools`. Every adapter in the repo is now in the mpnn image.

Three independent things have to miss for this to land, and all three do:

1. `importlib.import_module` is not an `ast.Import`/`ast.ImportFrom`, so the package-import check
   never sees it. **Documented and accepted.**
2. `Secret.run_function(...)` is exempted by the new receiver rule. **New in this PR.**
3. `_deploy_upload_probe._image_files` walks `_mount_layers`, `context_mount_function`, `mount` and
   `context_files` — **not `build_function`**. **Documented and accepted.**

The probe's `module_package` / `info_type` assertions do not fire: they check the *entrypoint*
function's mode, which is still FILE. It is the *build* function that is PACKAGE.

### 3.4 Result

| check | modal 1.4.2 | modal 1.5.4 |
|---|---|---|
| `pytest -q tests/test_deploy_paths_exclusions.py` | **41 passed** | **41 passed** |
| `pytest -q` (whole suite, no path argument) | **5303 passed, 20 skipped** | — |

Clean head is also 5303/20. **Byte-identical.** Nothing anywhere in the repo goes red while nine
adapters sit in a GPU image that `!tools/**/__init__.py` guarantees will never be redeployed.

This is the exact failure the PR exists to prevent, so by the standard rounds 1–3 set, it blocks.

**It is a small fix.** Options, in the order I would try them:

* Move `run_function` and `pip_install_from_pyproject` into `_FORBIDDEN_CALLS`. That branch is
  checked **before** the exemption, so it is unreachable by receiver spelling. This alone closes the
  shipping path, and it is a two-line change.
* Narrow the exemption to a receiver that is *explicitly* a modal class — require an `ast.Attribute`
  whose `value.id == "modal"`, or a name bound by `from modal import <Class>` in the same file —
  rather than any bare name that happens to match. This closes the class rather than the instance.

Either change needs a floor test that goes red without it; the existing
`test_non_image_receiver_exemption_is_narrow` does not currently pin the instance-binding case.

---

## 4. Claim-by-claim

### Claim 1 — round 3's two blockers are closed: **CONFIRMED** [RUN]

| mutation | landed | 1.4.2 | 1.5.4 | failing test(s) |
|---|---|---|---|---|
| `dockerfile_commands(["COPY /.x /root/x.py"], **{"context_files": {"/.x": adapter}})` | yes | 2 failed, 39 passed | 2 failed, 39 passed | `test_modal_app_stays_self_contained[mpnn]` **and** `test_modal_really_uploads_no_adapter_package[mpnn]` |
| `modal  deploy -m "tools.${{ matrix.app }}.modal_app"` (one extra space) | yes | 1 failed, 40 passed | 1 failed, 40 passed | `test_deploy_step_still_passes_a_FILE_path_to_modal_deploy` |
| `$MODAL_BIN deploy -m "tools.${{ matrix.app }}.modal_app"` | yes | 1 failed, 40 passed | 1 failed, 40 passed | same |

Both closed, on both versions. The first is now **double-covered** — the AST scan *and* the probe
each fail it independently, which is the strongest single result in this PR.

### Claim 2 — the sibling audit is complete: **REFUTED (a sixth spelling exists), but not a blocker** [RUN]

The five listed spellings are all refused. I found more the parser does not read:

| # | spelling | AST scan | probe | ships adapter? |
|---|---|---|---|---|
| M6 | `_mc = operator.methodcaller("pip_install_from_requirements", a)` … `_mc(image)` | **MISSED** | CAUGHT | yes |
| M7 | `exec('image = image.pip_install_from_requirements("…")')` | **MISSED** | CAUGHT | yes |
| M8 | `_h = SimpleNamespace(Volume=<Image>)` … `_h.Volume.pip_install_from_requirements(a)` | **MISSED** | CAUGHT | yes |
| M4 | `Volume = <Image>` … `Volume.uv_sync(a)` | **MISSED** | CAUGHT | yes |
| M5 | `Dict = <Image>` … `Dict.uv_pip_install(requirements=[a])` | **MISSED** | CAUGHT | yes |

`operator.methodcaller` split over two statements is a genuine **sixth** spelling: the method name is
a *string argument* and the call site's callee is a bare `Name`, so neither the callee check nor the
bound-not-called check can see it. The inline form `operator.methodcaller(...)(image)` **is** caught,
because the outer callee is a `Call` — only the split form gets through.

None of these is a blocker on its own: every one routes through `context_files`, and the probe
catches all of them. That is the double coverage earning its keep. But **the claim that "the fix now
refuses anything the scan cannot statically read" is not accurate** — `exec`, `methodcaller` and an
attribute-chain receiver are all things it cannot read and does not refuse. Worth correcting in the
docstring, because a future reader could rely on it.

Spellings I tried that ARE caught: `functools.partial` over the bound method, a class attribute
holding the method, `vars(modal)["Image"]`, `type(image).uv_sync(...)`,
`image.__getattribute__("uv_sync")(...)`, `operator.attrgetter`, a comprehension walrus, a decorator,
`getattr(image, _N["a"])`, and module-level indirection through a repo-root helper.

### Claim 3 — zero false reds: **CONFIRMED today; the surface grows every Modal minor** [RUN]

All nine apps are green on both versions (41/41), so there is no false red today. The controls hold
on both: a `**` into a non-Image call, ordinary chaining, `modal.Volume.from_name` and
`modal.Secret.from_name` all stay green.

The forward-looking risk is real and now quantified. `_IMAGE_API` minus the allowlist is the set of
names that red-light any call sharing them, whatever the receiver:

| modal | `_IMAGE_API` | refused names that read like ordinary application methods |
|---|---|---|
| 1.4.2 | 38 | `build`, `clone`, `client`, `cmd`, `deps`, `entrypoint`, `from_id`, `hydrate`, `imports`, `is_hydrated`, `object_id`, `shell` |
| 1.5.4 | 41 | the same minus `clone`, **plus `from_name`, `logs`, `pipe`, `publish`** |

One minor bump added four names, one of which (`from_name`) collided with all nine apps. `logs`,
`pipe` and `publish` are exactly the sort of name an ordinary refactor introduces (`cfg.publish()`,
`job.logs()`), and any of them would red-light the **deploy** guard on a change that has nothing to
do with images. The remedy text correctly says "rename the local call, do not allowlist" — the right
instruction — but this is the class that has already caused one outage, and a repeat is likely rather
than hypothetical.

### Claim 4 — `context_files` has double coverage: **CONFIRMED, genuinely independent** [RUN]

Tested the way the task asked: **disable the AST scan and see whether the probe stands alone.**
Landed `out["violations"] = []` immediately before `_scan`'s `return out`, plus a `getattr`-callee
mutation shipping the adapter:

```
FAILED test_non_image_receiver_exemption_is_narrow          <- floor, correctly red
FAILED test_scan_flags_every_forbidden_call_and_kwarg       <- floor, correctly red
FAILED test_modal_really_uploads_no_adapter_package[mpnn]   <- THE PROBE, standing alone
3 failed, 38 passed
```

`AssertionError: Modal uploads ['tools/mpnn/__init__.py'] for mpnn.` The probe catches it with the
entire AST scan neutered, and the two non-vacuity floors correctly detected the sabotage. Wall cost
measured at 11–20s for the whole file including all nine probe subprocesses, on both versions;
credential-free (no `MODAL_TOKEN_*` in either environment).

### Claim 5 — the `.dockerignore` floor: **CONFIRMED against Modal's source, both versions** [RUN]

`modal/_utils/docker_utils.py::find_dockerignore_file` appends exactly three candidates, and the code
is identical in 1.4.2 (lines 68–98) and 1.5.4 (lines 77–107):

1. `dockerfile_path.parent / f"{dockerfile_path.name}.dockerignore"`
2. `dockerfile_path.parent / ".dockerignore"`
3. `context_directory / ".dockerignore"`

`test_no_dockerignore_narrows_what_modal_uploads` globs exactly those three
(`tools/*/Dockerfile.modal.dockerignore`, `tools/*/.dockerignore`, `<repo>/.dockerignore`) — the
Dockerfiles live at `tools/<slug>/Dockerfile.modal` and the context dir is the repo root, so the
mapping is exact, not approximate. Mutation M9 (a root `.dockerignore` of `**/__init__.py`) fails it
by name on both versions.

---

## 5. Full mutation table

Every mutation confirmed landed with `git diff --unified=0` (`git status --porcelain` for the
untracked one) **before** any conclusion, then reverted. The driver refuses on a non-unique anchor
and on an empty diff. All run in private worktrees (`r4mut`, `r4mut2`); nothing under `tools/` is
modified in what I push.

| # | mutation | landed | 1.4.2 | 1.5.4 | caught by |
|---|---|---|---|---|---|
| R3a | `dockerfile_commands(..., **{"context_files": adapter})` | yes | 2 failed | 2 failed | `test_modal_app_stays_self_contained[mpnn]` + `test_modal_really_uploads_no_adapter_package[mpnn]` |
| R3b | `modal  deploy -m` (one extra space) | yes | 1 failed | 1 failed | `test_deploy_step_still_passes_a_FILE_path_to_modal_deploy` |
| R3c | `$MODAL_BIN deploy -m` | yes | 1 failed | 1 failed | same |
| M1 | `_Secret = <Image>` … `.pip_install_from_requirements(adapter)` | yes | 2 failed | 2 failed | scan + probe (underscore name is not exempt) |
| M2 | `Secret = <Image>` … `.pip_install_from_pyproject(adapter)` | yes | 1 failed | 1 failed | probe, via `TomlDecodeError` — loud, not silent |
| M3 | `Secret = <Image>` … `.run_function(_bake)` (local fn) | yes | **41 passed** | **41 passed** | **nothing** — but adapter does NOT reach the image (build fn is FILE mode) |
| **M3b** | **M3 + `importlib.import_module("tools.mpnn")`** | **yes** | **41 passed / full suite 5303** | **41 passed** | **NOTHING — adapter DOES reach the image → BLOCKER** |
| M4 | `Volume = <Image>` … `.uv_sync("tools/mpnn")` | yes | 1 failed | 1 failed | probe only (AST scan exempted it) |
| M5 | `Dict = <Image>` … `.uv_pip_install(requirements=[adapter])` | yes | 1 failed | 1 failed | probe only |
| M6 | `operator.methodcaller` split over two statements | yes | 1 failed | 1 failed | probe only |
| M7 | `exec()` of a source string | yes | 1 failed | 1 failed | probe only |
| M8 | attribute-chain receiver `_h.Volume` | yes | 1 failed | 1 failed | probe only |
| M9 | root `.dockerignore` of `**/__init__.py` | yes (untracked) | 1 failed | 1 failed | `test_no_dockerignore_narrows_what_modal_uploads` |
| G1 | AST scan neutered + `getattr` callee shipping adapter | yes | 3 failed | — | probe alone + both non-vacuity floors |

Thirteen mutations beyond the three re-checks; ten of them tried by no prior round.

---

## 6. The eight residual risks

| # | builder's framing | my assessment |
|---|---|---|
| 1 | a future Modal channel that is neither mount nor `context_files` | Fair, and **already realised**: `build_function` is exactly such a channel, it exists today, and M3b reaches it. Not hypothetical. |
| 2 | a deploy command that does not exist as a written line | Agree. Deliberate obfuscation, out of scope. |
| 3 | script-vs-package mode pinned solely by the shell scan | Agree, correctly stated. Note M3b shows the pin covers the *entrypoint* only — a PACKAGE-mode **build** function is unpinned. |
| 4 | last-wins constant map | Agree; `add_local_file` builds a real mount so the probe catches it. Verified by reading, not run. [REASONED] |
| 5 | name-only matching false-reds on an unidentifiable receiver | **Agree, and I would raise it above "accepted".** §Claim 3 quantifies it: 4 new names in one minor, 1 collided with 9/9 apps. The exemption added to fix it is itself the blocker in §3. |
| 6 | `ADD` and `importlib` are loud, never silently stale | **Half wrong.** `ADD` — agree. `importlib` — the stated verification ("the upload set is unchanged") is true in isolation and **false in composition with `run_function`**, which is precisely M3b. This line should not be filed under "loud". |
| 7 | Modal-version drift | See below. |
| 8 | `static/example/*` on no deploy trigger | **Confirmed pre-existing and out of scope**, see below. |

### Should version drift block this PR? — **No, on its own.** [RUN + REASONED]

The drift is genuinely pre-existing: `pip install 'modal>=1.4,<2'` is on trunk in both
`deploy-modal.yml` and `pytest.yml`, and this PR touches neither line. What *is* new is that this PR
adds the first repo artifact whose correctness depends on `dir(modal.Image)` — so it converts a
latent, harmless range into a live one.

Three reasons it should not block by itself:

1. The demonstrated failure direction is a **false red** — CI goes red, merges stop, a human looks.
   That is the safe direction by this PR's own standard, and it is how the problem was found.
2. The guard is now verified green at **both ends of the range actually resolvable today** (1.4.2 and
   1.5.4), which is more than any prior round had.
3. Pinning `modal` in `requirements.txt` does not fix the deploy job (a separate install line), so
   the "obvious" fix is only half a fix and deserves its own change.

But it should be a **named follow-up with a trigger**, not an open-ended "worth considering": the
`_IMAGE_API` surface grew 38 → 41 in one minor and one of the new names took out all nine apps. My
suggestion is to pin `modal==` in `requirements.txt` (test side) and bump deliberately, leaving the
deploy job's range alone — that at least makes the local venv and the test CI agree, which is the gap
that hid the outage through three rounds.

### `static/example/*` — **confirmed pre-existing and out of scope** [RUN]

Four Dockerfiles COPY it, on trunk and on this branch identically:

```
tools/af2/Dockerfile.modal
tools/colabfold/Dockerfile.modal
tools/esmfold/Dockerfile.modal
tools/mpnn/Dockerfile.modal
```

Trunk's `push.paths` is `['tools/**', '!tools/**/meta.py', '!tools/**/example/**',
'.github/workflows/deploy-modal.yml']` — no `static/**` entry, so a `static/example/` edit changes
four images and triggers no deploy. Same silent-stale class this PR exists to prevent, entirely
independent of it. Correctly scoped out; worth its own ticket.

---

## 7. Is the PR body accurate? — **Mostly yes, with two corrections**

Everything I could check in it is accurate: the script-mode premise, the six-door `context_files`
audit, the three-layer structure, the round-2/3 hole table, the headline version story (including the
9-failed/28-passed and 37-passed numbers, both reproduced exactly), the 41/41-under-both claim, the
`ImageBuilderVersion` move from `modal.image` to `modal._image`, and every suite number including the
node-id reconciliation.

Two things need fixing:

1. **Mutation row N6 is misleading.** "abuse the new exemption: `Volume = modal.Image`, then ship an
   adapter through it → scan + probe" is true for that exact spelling, and it is the one spelling the
   exemption does not reach. Binding an Image *instance* (`Volume = modal.Image.from_dockerfile(...)`)
   is not covered and is the blocker in §3. The row currently reads as evidence the exemption was
   attacked, when the attack missed.
2. **Residual risk 6** files `importlib.import_module` under "both loud, never silently stale". M3b
   shows it is silent when composed with `run_function`. The parenthetical "verified: the upload set
   is unchanged" is true only in isolation and should say so.

The claim "None of the nine apps uses a dynamic callee, a `**`/`*` expansion or a bound Image
attribute, so this costs zero false reds" is correct — verified, 41/41 on both versions.

---

## 8. Verdict

**BLOCKED.**

The PR is close, and most of it is genuinely excellent — the double coverage in Claim 4 is the
strongest thing here, and the version-drift discovery is a real catch that three prior rounds and a
CI run missed. Rounds 3's two blockers are properly closed.

But the fix for the version problem introduced a regression that reopens the catastrophic direction:

> `Secret = modal.Image.from_dockerfile(...)` + `importlib.import_module("tools.mpnn")` +
> `Secret.run_function(_adapter.build_payload)` puts all nine `tools/**/__init__.py` into the mpnn GPU
> image with **5303 passed, 20 skipped** — byte-identical to clean head — on both modal versions.

Measured at `c9bb54b` the same code was caught; at `74461f1` it is not. That is a regression
introduced by the commit under review, in the exact failure class this PR exists to prevent, and the
safe error here is BLOCKED.

Two small, independent fixes each close the shipping path (§3.4). Neither needs new machinery, and
both want a floor test that goes red without them — `test_non_image_receiver_exemption_is_narrow`
currently pins the class-binding case and not the instance-binding one.

Not blocking, but worth folding in while the branch is open: the sixth spelling (Claim 2), the two PR-body
corrections (§7), and a named follow-up for the version pin (§6).
