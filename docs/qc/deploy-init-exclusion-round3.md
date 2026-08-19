# QC round 3 — `!tools/**/__init__.py` deploy-trigger exclusion (PR #157)

**SHA reviewed: `339fb6f4b0f039b8d7125db0d4d5f344268e2d4d`** (branch `chore/skip-deploy-on-init-py`)
Trunk at review time: `origin/main` = `7fd180df35086cfc5da3710ff336024901d8e73b` (PR #158).
Reviewer: independent QC agent. Did not build this, did not run rounds 1 or 2.

## Verdict: **BLOCKED**

Round 2's two blockers are genuinely closed — I reproduced both and both now fail by name. The
six-door audit is real and I re-derived every entry by measurement. The recursion floor is
correctly scoped and I verified both halves.

But the same catastrophic shape is still reachable, one syntax step sideways:

> **`.dockerfile_commands([...], **{"context_files": {"/.x": "tools/mpnn/__init__.py"}})`
> bakes the adapter into the mpnn GPU image with the full suite green — 5299 passed,
> 20 skipped, byte-identical to clean head.**

`_scan` bans the kwarg by testing `kw.arg in _FORBIDDEN_KWARGS`. For a `**` expansion
`kw.arg is None`, so the ban never fires. The method (`dockerfile_commands`) is allowlisted, the
COPY pattern names only `/.x`, and the probe is structurally blind to `context_files`. All three
layers pass. Detail in §5.1.

A second, independent hole is in the deploy-step pin (§5.2): the line selector
`if "modal deploy" in ln` is itself a substring denylist, so `modal  deploy -m ...` — one extra
space — is never examined.

Both are the silent direction the exclusion exists to prevent, and both are small fixes. By the
standard the two prior rounds set — an adapter reaching a GPU image with the suite green — this
is a blocker.

---

## 1. Baselines — measured myself, on CURRENT trunk [RUN]

Command, from each worktree root, **no path argument** (no `pytest.ini` exists, so a path
argument silently changes collection scope):

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| state | commit | result | wall |
|---|---|---|---|
| **trunk (current)** | `7fd180d` | **5273 passed, 20 skipped** | 355.93s |
| **branch head** | `339fb6f` | **5299 passed, 20 skipped** | 365.74s |
| **merge(trunk, head)** — what actually lands | `7fd180d` + `339fb6f` | **5310 passed, 20 skipped** | 294.08s |
| old base (rounds 1/2 baselined here) | `48b4b71` | 5282 collected = 5262 + 20 skipped | collect-only |

All exit 0. The merge is clean, no conflict resolution needed
(`5 files changed, 1815 insertions(+), 5 deletions(-)`).

**Arithmetic closes exactly**: 5273 + 37 = **5310**. Trunk moved +11 under this branch
(5262 → 5273, PR #158); head is 5299 because it does not carry #158's tests. Nothing is lost in
the merge, and #158 does not interact with this branch.

### The +37 reconciled by node id, not by count [RUN]

```
pytest -q --collect-only    # 48b4b71: 5282 ids   |   339fb6f: 5319 ids
comm -13 <old> <new> | sed 's/::.*//' | sort | uniq -c
     37 tests/test_deploy_paths_exclusions.py
      1 tests/test_scout_anonymous_access.py      <- churn, not an addition
```

The lone `test_scout_anonymous_access.py` id is a parametrize id carrying a random UUID
(`.../download/555e6194-…`); one is dropped and one added on every collection. Net **+37, all in
`tests/test_deploy_paths_exclusions.py`** (34 → 37), nothing else. The claim is exact.

---

## 2. Claim 1 — round 2's two blockers, reproduced against THIS head [RUN]

Reproduced myself in a throwaway worktree. Each verified landed with `git diff --unified=0`
before the guard ran.

### Blocker A — `dockerfile_commands(..., context_files={...})` → **CLOSED**

Landed, `@@ -131,0 +132,4 @@ tools/mpnn/modal_app.py`:

```python
    .dockerfile_commands(
        ["COPY /.adapter.py /root/adapter.py"],
        context_files={"/.adapter.py": "tools/mpnn/__init__.py"},
    )
```

**CAUGHT** → `test_modal_app_stays_self_contained[mpnn]`
(`AssertionError: mpnn/modal_app.py line 130: passes context_files=`). 1 failed, 36 passed.

### Blocker B — deploy-step pin defeated by a leftover comment → **CLOSED**

Landed, `@@ -136 +136,2 @@ .github/workflows/deploy-modal.yml`:

```yaml
          # was: modal deploy "$app_file" | tee "deploy-${{ matrix.app }}.log"
          modal deploy -m "tools.${{ matrix.app }}.modal_app" | tee "deploy-${{ matrix.app }}.log"
```

**CAUGHT** → `test_deploy_step_still_passes_a_FILE_path_to_modal_deploy`. 1 failed, 36 passed.

### W3 re-run — the literal echoed from an unrelated step

Round 2 found this green. Landed `@@ -124,0 +125 @@`: `echo will run: modal deploy "$app_file"`
added to the *Verify Modal auth* step, real deploy line intact.

**Now CAUGHT** → `test_deploy_step_still_passes_a_FILE_path_to_modal_deploy`. Strictly a **false
red** (an `echo` deploys nothing), but it is the safe direction. Noted, not a defect.

---

## 3. Claim 2 — the six-door audit, re-derived by measurement [RUN]

I read no Modal docs. I built one image per method, unwrapped the synchronicity wrapper to
`modal.image._Image`, pulled the `dockerfile_function` free variable out of the `_from_args`
load closure, and called it for the real `DockerfileSpec`. modal **1.4.2**, builder `2024.10`.

| method | `DockerfileSpec.context_files` | ships local bytes? |
|---|---|---|
| `pip_install_from_requirements(p)` | `{"/.requirements.txt": p}` | **YES** |
| `poetry_install_from_file(a, b)` | `{"/.pyproject.toml": a, "/.poetry.lock": b}` | **YES** |
| `uv_sync(d)` | `{"/.pyproject.toml": …, "/.uv.lock": …}` | **YES** |
| `uv_pip_install(requirements=[p])` | `{"/.0_req.txt": p}` | **YES** |
| `micromamba_install(spec_file=p)` | `{"/spec.yaml": p}` | **YES** |
| `dockerfile_commands(context_files=…)` | whatever you pass | **YES** |
| `pip_install_from_pyproject(p)` | `{}` | no bytes — **canary leaked into `spec.commands`** |
| `pip_install_private_repos(...)` | `{}` | **no** |
| `run_function(...)` — all three `include_source` values | `{}` | **no**; carries a `build_function` |
| `from_registry` / `from_aws_ecr` / `from_gcp_artifact_registry` | only Modal's own `/modal_requirements.txt` | **no** |

**All six doors confirmed.** The audit is genuine and was measured, not read.

**Claim 4 — the builder's self-correction is right.** `pip_install_private_repos` really does
return `context_files={}`. Dropping it from the shipper list was correct; leaving it in would
have been a false claim in the over-cautious direction.

**The seventh door, measured.** The brief asked me to measure `run_function`, which the builder
reasoned about from the signature rather than measuring.

* `pip_install_from_pyproject` — **confirmed a real door of a different kind.** `context_files`
  is empty, but my canary package name from the local file appears verbatim in `spec.commands`.
  It inlines local file *content*. Correctly refused.
* `run_function(...)` — **measured, and the builder's reasoning happens to land on the right
  answer for the wrong reason.** `context_files` is `{}` for `include_source` True, False and
  default. What it actually carries is a `build_function` free variable whose `FunctionInfo`
  follows the *same* script/package rule as the entrypoint — in script mode I measured
  `info_type = FILE`, 1 entrypoint-mount file, no `__init__.py`. So `run_function` is not an
  independent door **while script mode holds**. It must still stay refused, for a reason the
  comment does not give: **the probe never walks `build_function`** (`_image_files` visits
  `_mount_layers`, `context_mount_function` and `mount` only). If `run_function` were ever
  allowlisted, its build function's mount would be invisible to the behavioural layer.
  Recommend the comment say that.

I found **no seventh `context_files` shipper**. The audit is complete for modal 1.4.2.

**One cosmetic inaccuracy.** The comment says `uv_pip_install` yields `/.0_requirements.txt`.
Measured, the key is `/.0_<basename-of-your-file>` — I got `/.0_req.txt` from `req.txt`. The
mechanism and the shipping are exactly as claimed; only the literal filename is wrong. Nit.

### Is the allowlist genuinely fail-closed?

**Yes, and I broke it to check.** `dir(modal.Image)` has 38 public names; 11 are allowed, 27
refused. Both rot directions fail loudly (§5, M9/M10). `Image.from_registry` — an unallowlisted
constructor a future app might reasonably want — goes red (M11). That is the intended
fail-closed behaviour, correctly implemented.

**But name-only matching produces real false reds, with an erosion path.** `_IMAGE_API` includes
generic names: `build`, `clone`, `client`, `cmd`, `deps`, `entrypoint`, `imports`, `shell`,
`hydrate`, `object_id`, `local_uuid`. I landed three ordinary non-Modal calls in
`mpnn/modal_app.py` (M12) —

```python
def _prep(cfg, argv):
    cfg = cfg.clone()
    return cfg.build(argv.imports())
```

— and `test_modal_app_stays_self_contained[mpnn]` goes red. The builder documents this and calls
it the safe direction, which it is. The concern is the *remedy the error message names*: "add it
to `_ALLOWED_IMAGE_CALLS`". A developer red-lit by `cfg.build()` is told to allowlist `build` —
which then permanently allows the real `Image.build` too. The false red pushes toward widening
the allowlist for reasons unrelated to images. Worth one clause in the message: allowlist only
after confirming the call is on an `Image`; otherwise rename the local call.

---

## 4. Claim 3 — the recursion floor, both halves verified [RUN]

**(a) The flat version really would have gone RED on clean head.** I ran the probe over all nine
apps on clean head and tabulated `image_layers_walked` per function:

```
mpnn            run_tool         3 layers,  2 image files
af2             run_tool         3          2
colabfold       run_tool         3          2
esmfold         run_tool         3          2
boltz2          run_tool         3          1
esmfold2_design _run_one_seed    8          1
esmfold2_design run_tool         1          0     <-- flat `>= 2` fails here
iggm            run_tool         3          1
proteina        run_tool         3          1
opendde         run_tool         3          1
```

Round 2's proposed flat `image_layers_walked >= 2` would have failed
`test_modal_really_uploads_no_adapter_package[esmfold2_design]` on a clean tree. The builder
measured before shipping and was right to scope it. **Confirmed.**

**(b) The scoped version still catches P2.** I re-landed round 2's P2 — replacing the two
recursion lines in `_image_chain` with `return` (`@@ -83,4 +83 @@`):

```
round 2 (flat floor absent):  34 passed   <- P2 was a HOLE
this head (scoped floor):      9 failed, 28 passed
                               all 9 test_modal_really_uploads_no_adapter_package[*]
```

**Confirmed.** The scoping is correct on both sides: it does not fire on the legitimately flat
function, and it does fire when the walk is truncated.

---

## 5. My own attack — 12 mutations, 9 novel [RUN]

Throwaway worktree `qc3mut`, discarded. **Nothing under `tools/` is in what I push.** Every
mutation proven landed with `git diff --unified=0` (or `git status --porcelain` for new files)
**before** any conclusion, then reverted to a verified-empty status.

**Harness safety proven first, not assumed.** My driver refuses on a non-unique anchor and on an
identity/empty edit. Both fired on demand: `!! ANCHOR NOT UNIQUE (count=0) -- NOT APPLIED` and
`!! anchor == replacement, identity edit refused`, worktree clean after each. So neither of this
repo's two historical silent-mutation failures could have produced a false "caught" below.

Guard command: `pytest -q tests/test_deploy_paths_exclusions.py -rf` (37 tests on clean head).

| # | mutation | novel | landed | caught | failing test(s) |
|---|---|---|---|---|---|
| **M1** | `dockerfile_commands([...], **{"context_files": {"/.adapter.py": "tools/mpnn/__init__.py"}})` | **yes** | yes | **NO — 37 passed** | — **BLOCKER, §5.1** |
| **M3** | same, on `esmfold2_design` (the app with **no Dockerfile**) | **yes** | yes | **NO — 37 passed** | — same hole, most exposed app |
| **M4** | workflow: keep the compliant line, add `MODAL_BIN=modal` + `$MODAL_BIN deploy -m …` | **yes** | yes | **NO — 37 passed** | — **HOLE, §5.2** |
| **M4b** | workflow: keep the compliant line, add `modal  deploy -m …` (one extra space) | **yes** | yes | **NO — 37 passed** | — same hole, accidental spelling |
| M2 | `COPY tools /opt/toolsrc` — bare directory COPY in mpnn's Dockerfile | yes | yes | yes | `test_dockerfile_copies_no_adapter_package[mpnn]`, `test_modal_really_uploads_no_adapter_package[mpnn]` |
| M5 | `modal.App("…", include_source=True)` | yes | yes | n/a | 37 passed — **correct green**: probe shows entrypoint still `['tools/mpnn/modal_app.py']`, no `__init__.py`. Not a door in script mode. |
| M6 | repo-root `.dockerignore` (`**/__init__.py`) + a real adapter `COPY` | yes | yes | yes | `test_dockerfile_copies_no_adapter_package[mpnn]` only — see §6 |
| M7 | image built in an imported root-level `image_defs.py`, `modal_app.py` reduced to `from image_defs import image` | yes | yes | yes | `test_modal_app_stays_self_contained[mpnn]` (no-`add_local_file` floor), `test_every_discovered_dockerfile_is_the_one_its_app_uses` |
| M8 | `dockerfile_commands(["COPY ${SRC} …"], build_args={"SRC": "tools/mpnn/__init__.py"})` | yes | n/a | n/a | **LOUD**: Modal raises `InvalidError: COPY command: ${SRC} using special flags/arguments/variables are not supported`, at spec time. Not a door. |
| M9 | FLOOR: `_IMAGE_API = set()` (simulate Modal renaming the API) | yes | yes | yes | `test_image_api_allowlist_is_live_and_refuses_the_context_files_family`, `test_scan_flags_every_forbidden_call_and_kwarg` |
| M10 | FLOOR: allowlist `pip_install_from_requirements`, `uv_sync`, `run_function` | yes | yes | yes | same two tests |
| M11 | swap `from_dockerfile` → `Image.from_registry(...)` | yes | yes | yes | `test_modal_app_stays_self_contained[mpnn]`, `test_every_discovered_dockerfile_is_the_one_its_app_uses` |
| M12 | FALSE-RED probe: benign `cfg.clone()` / `cfg.build()` / `argv.imports()` | yes | yes | yes (**false red**) | `test_modal_app_stays_self_contained[mpnn]` — see §3 |

Plus the three required re-runs from round 2: Blocker A **caught**, Blocker B **caught**, W3
**caught**, P2 **caught**.

**9 novel, 4 holes (two distinct mechanisms), 1 loud-by-Modal, 1 deliberate false red.**

### 5.1 BLOCKER — `**` expansion walks straight past `_FORBIDDEN_KWARGS`

`_scan` bans the kwarg by name:

```python
for kw in node.keywords:
    if kw.arg in _FORBIDDEN_KWARGS:
        out["violations"].append((node.lineno, f"passes {kw.arg}="))
```

For a `**` expansion Python sets `kw.arg = None`, so nothing matches. Measured directly on
`_scan`:

```
context_files=…            -> violations=[(1, 'passes context_files=')]
**{"context_files": …}     -> violations=[]        <-- bypass
_K = {...}; **_K           -> violations=[]        <-- bypass
**{"spec_file": …}         -> violations=[]        <-- bypass
getattr(image,"dockerfile_commands")(…, context_files=…) -> caught (kwarg check is name-agnostic)
```

Landed in `tools/mpnn/modal_app.py` (`@@ -131,0 +132,4 @@`):

```python
    .dockerfile_commands(
        ["COPY /.adapter.py /root/adapter.py"],
        **{"context_files": {"/.adapter.py": "tools/mpnn/__init__.py"}},
    )
```

**Why all three layers pass.** `dockerfile_commands` is allowlisted, so the method check is
satisfied. The kwarg ban never fires (`kw.arg is None`). The only COPY pattern is `/.adapter.py`,
which correctly matches no `tools/**/__init__.py`. And `context_files` is not a mount, so the
probe is structurally blind.

**It is real, not cosmetic.** Read back off the app's own image chain:

```
layer0 context_files = {'/.adapter.py': 'tools/mpnn/__init__.py'}
layer0 commands      = ['FROM base', 'COPY /.adapter.py /root/adapter.py']
```

Probe view of the same tree: `image_files = ['static/example/1HEW.pdb',
'tools/mpnn/run_pipeline.py']` — no `__init__.py` anywhere.

**Full suite, no path argument, mutation applied: 5299 passed, 20 skipped — identical to the
clean-head baseline.** Nothing in the repo notices.

**Consequence.** The edit touches `modal_app.py`, which is on the trigger, so it deploys once and
the adapter lands in the image. Every later edit to `tools/mpnn/__init__.py` matches
`!tools/**/__init__.py`, never redeploys, and the GPU image silently goes stale — the exact
failure the exclusion is supposed to make impossible.

**Realism.** No `tools/` file uses `**` expansion today (0 call sites), but it is ordinary style
in this codebase: **129 `**`-expansion call sites across 52 files** repo-wide. The natural way in
is conditional kwargs — `extra = {"context_files": ctx} if ctx else {}` … `**extra` — which is a
normal idiom, not an attack. Independently of likelihood, the workflow comment states these
kwargs "are refused outright", and that is currently false.

**Fix shape** (one condition): in the `_FORBIDDEN_KWARGS` loop also refuse `kw.arg is None` when
the call name is in `_IMAGE_API` — a `**` expansion into an Image method is unresolvable by an
AST scan, which is precisely the "not statically knowable → fail" rule `_resolve` already applies
everywhere else in this file. That keeps it consistent rather than adding a new concept.

### 5.2 Second hole — the deploy-step pin selects lines with a substring denylist

```python
invocations = [ln for ln in lines if "modal deploy" in ln]
...
not_a_file_path = [ln for ln in invocations if not ln.startswith('modal deploy "$app_file"')]
```

The builder correctly rewrote the *assertion* from denylist to allowlist ("every invocation must
be the file form"). But the *selector* that decides which lines are invocations is still a
substring denylist. Any spelling that avoids the exact bytes `modal deploy` is never examined at
all, and the retained compliant line keeps `assert invocations` satisfied.

Both of these landed and left the guard at **37 passed**:

```yaml
          modal deploy "$app_file" | tee "deploy-${{ matrix.app }}.log"   # satisfies both asserts
          MODAL_BIN=modal
          $MODAL_BIN deploy -m "tools.${{ matrix.app }}.modal_app"        # M4  — never examined
```
```yaml
          modal deploy "$app_file" | tee "deploy-${{ matrix.app }}.log"
          modal  deploy -m "tools.${{ matrix.app }}.modal_app"            # M4b — one extra space
```

The last deploy wins, so the package-mode app is what goes live. Round 2 established what that
means: `__package__ = 'tools.mpnn'`, `info_type = PACKAGE`, entrypoint mount 101 files including
22 `__init__.py`.

M4b matters more than M4: an extra space is a typo, not an attack, and it silently disarms the
only check in the repo that can see script-vs-package mode.

**Not caught but worth recording as safe:** aliasing *without* keeping the compliant line trips
`assert invocations` ("the deploy job runs no `modal deploy` at all"), so the builder's positive
framing does hold for the plain refactor case. It is only the keep-the-old-line pattern — the
same human behaviour round 2's W2 exploited — that gets through.

**Fix shape:** select invocation lines by token rather than substring (e.g. `shlex.split`, then
first token basename `modal` and second token `deploy`), or assert the deploy step's `run` block
equals an expected literal outright.

### 5.3 Closed off, for the record

I could not get in through any of these, each measured rather than reasoned:

* **Positional smuggling of a banned kwarg.** All four (`context_files`, `context_dir`,
  `spec_file`, `mounts`) are `KEYWORD_ONLY` in modal 1.4.2, so there is no positional spelling.
* **Directory COPY.** `COPY tools /opt/tools`, `COPY tools/`, `COPY ./tools`, `COPY tools/mpnn`,
  `COPY . /app` all resolve through Modal's own parser to patterns that match the adapters.
* **`build_args`.** Modal raises `InvalidError` at spec time on any `${VAR}` in a COPY.
* **A dotted `app_file`.** modal 1.4.2 refuses `modal deploy tools.mpnn.modal_app` with
  `InvalidError: Python module paths must be specified with the -m flag` — loud, not silent.
* **`ignore=` on `from_dockerfile` / `dockerfile_commands`.** Read Modal's `_create_context_mount`:
  the predicate is `not include_fn(source) or ignore_fn(source)`, so `ignore` can only ever
  narrow the COPY-matched set. It cannot widen, in any spelling including an inverted matcher.
* **`include_source`** on `modal.App` or `@app.function` — no effect in script mode (M5).

---

## 6. Claim 5 — the probe [RUN]

**Untouched this round: confirmed.** `git diff 3e664a1 339fb6f -- tests/_deploy_upload_probe.py`
is empty.

**Credential-free: confirmed myself.** With `MODAL_CONFIG_PATH` pointed at an empty file,
`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` blanked and `HOME` redirected to a non-existent dir:
**37 passed in 10.20s**.

**Unskipped: confirmed.** `grep -E "skip|xfail|importorskip|@pytest.mark"` over both files
returns one *comment* line ("not skippable: a skipped guard is not a guard") and nothing else
besides `@pytest.mark.parametrize`.

**Non-vacuous: confirmed by breaking it.** P2 (§4b) turns 9 tests red. It reaches real image
internals — M2's directory COPY is caught by the probe as well as the scan.

**New finding — the probe has a second blind spot of the P2 class.** A repo-root `.dockerignore`
silently removes files from the probe's view. Measured, same Dockerfile COPY of
`tools/mpnn/__init__.py` in both runs:

```
with root .dockerignore (**/__init__.py):  image_files = [static/example/1HEW.pdb, tools/mpnn/run_pipeline.py]
without it:                                image_files = [static/example/1HEW.pdb, tools/mpnn/__init__.py, tools/mpnn/run_pipeline.py]
```

This is **not** a hole for the deploy question — if `.dockerignore` excludes the file, Modal
genuinely does not ship it, so the syntactic scan going red (M6) is a false red in the safe
direction. But it is worth recording that the probe's unique value can be switched off by a file
nobody would think of as touching the deploy guard, with nothing red to say so. Round 2's floor
(`modal_app.py` + `run_pipeline.py` must appear) does not catch it, because `run_pipeline.py`
arrives via `add_local_file`, not the Dockerfile context mount.

### Risk 6 — `context_files` caught once, not twice: **structural, and that is the point**

The probe walks mounts; `context_files` is not a mount. No amount of probe work fixes that
without teaching the probe to call `dockerfile_function` and read `DockerfileSpec` — which is
exactly what I did in §3 and what the AST scan is standing in for. So single coverage is
**structural, and acceptable in principle**.

What is not acceptable is that the single copy has a hole in it (§5.1). Single coverage is only
tolerable when the one check is airtight; `**` expansion means it is not. That raises Risk 6 from
"disclosed and accepted" to "load-bearing and currently broken".

If you want the second layer cheaply, it is available: the probe already walks the image chain,
so pulling `dockerfile_function(builder_version).context_files` per layer and reporting any local
path under `tools/` would give a genuine behavioural check on the same channel — ~10 lines in
`_image_files`, and it would have caught M1 independently. Optional; fixing §5.1 is sufficient.

---

## 7. Residual-risk list — judged

I could not find the builder's verbatim seven-entry list: PR #157 has **0 comments and 0
reviews**, and its body is the round-1 text (still says "21 tests", "5283 passed"). I judged the
residual risks **as documented in the code** — the "Not covered on purpose" block in the test
docstring and the "Three known gaps" note in the workflow — plus the two the brief names. If the
builder's list has entries not present in either place, I did not see them and did not assess
them.

| # | risk | judgement |
|---|---|---|
| 1 | `run_pipeline.py` not covered | **Correct and trivially safe.** It is mounted and it IS on the deploy trigger. |
| 2 | `ADD` in a Dockerfile | **Correct, loud.** Modal's parser honours `COPY` only, so the file is in no context mount and the build fails on the very deploy the Dockerfile edit triggers. |
| 3 | `importlib.import_module("tools.x")` / `__import__` | **Correct, loud.** In-container `ImportError` on every run; the upload set is unchanged. |
| 4 | a local-path kwarg a FUTURE Modal adds to an allowlisted method | see below |
| 5 | script-vs-package mode invisible to both checks, pinned only by the shell scan | **Correct, and it is the weak point.** The probe hardcodes `use_module_mode=False`, so it reports FILE by construction. That makes the shell pin load-bearing and solitary — and §5.2 gets past it. |
| 6 | `context_files` caught once (probe blind) | **Structural, but currently load-bearing and holed** — §6. |
| 7 | Modal-version drift between the test and deploy jobs | **Real, pre-existing, correctly not blocking** — §8. |

### Risk 1 — is refusing to signature-pin the eleven methods the right trade?

**Yes, and I verified the premise it rests on rather than taking it.** I pulled
`inspect.signature` for all eleven allowlisted methods and checked for unbanned local-path
parameters:

```
add_local_file       local_path        -> resolved and compared against run_pipeline.py
dockerfile_commands  context_files     -> BANNED
                     context_dir       -> BANNED
                     ignore            -> narrows only (verified in _create_context_mount)
                     build_args        -> Modal raises InvalidError on ${VAR} in COPY
from_dockerfile      path              -> compared against the app's own Dockerfile.modal
                     context_dir       -> BANNED
                     ignore, build_args-> as above
micromamba_install   spec_file         -> BANNED
pip_install          find_links, index_url, extra_options -> string-only, never uploaded
run_commands         volumes           -> remote, not local
apt_install / env / workdir / debian_slim / micromamba -> no local path at all
```

**There is no live instance of Risk 1 today.** Every local-path kwarg on an allowlisted method is
either banned, resolved-and-compared, or provably incapable of widening the context. So the trade
is right on the merits: signature-pinning would buy protection against a hypothetical while
paying red CI on every cosmetic Modal kwarg addition against an unpinned `>=1.4,<2` that CI
reinstalls fresh. Correct call, correctly reasoned.

The caveat is that Risk 1 is stated as a *future* Modal problem, and §5.1 shows the ban on the
kwargs that **exist today** is already bypassable. Fix the present-tense hole first; the
future-kwarg trade-off stands as written.

### Missing from the list

* **The `**`-expansion bypass of `_FORBIDDEN_KWARGS`** (§5.1). Not disclosed anywhere. The
  workflow comment asserts the opposite: "mounts=, context_dir=, context_files= and spec_file=
  are refused outright."
* **The substring selector in the deploy-step pin** (§5.2). The comment says "every `modal
  deploy` line in this job, shell comments stripped, must be the `modal deploy "$app_file"`
  form" — true only for lines that contain those exact bytes.
* **`run_function` must stay refused because the probe cannot walk `build_function`** — a
  stronger reason than the `include_source` one the comment gives (§3).
* **A repo-root `.dockerignore` narrows the probe's view with nothing red** (§6).
* **The false-red remedy erodes the allowlist** (§3, M12).

---

## 8. The two "pre-existing on main, untouched" items — both confirmed [RUN]

Checked against **trunk `7fd180d`**, not against the branch.

**1. Modal-version drift between the test and deploy jobs — genuinely pre-existing.**
`pytest.yml` installs `-r requirements-dev.txt` → `requirements.txt:13: modal>=1.4,<2.0`;
`deploy-modal.yml:84` installs `modal>=1.4,<2`. Both are unpinned ranges resolved independently
at different times, so the client the guard tests against and the client that performs the deploy
can differ within the range. Present on trunk. `git diff 48b4b71 339fb6f --
.github/workflows/deploy-modal.yml | grep 'modal>='` returns **nothing** — this PR does not touch
the install line. **Out of scope, correctly not blocking.**

**2. `static/example/*` COPY'd into four GPU images while on no deploy trigger — genuinely
pre-existing.** On trunk, `COPY static/example/…` appears in `tools/{af2,colabfold,esmfold,mpnn}/
Dockerfile.modal`, and trunk's `push.paths` is `['tools/**', '!tools/**/meta.py',
'!tools/**/example/**', '.github/workflows/deploy-modal.yml']`. `static/example/` is not under
`tools/`, so it matches no trigger at all — editing a fixture changes four images and deploys
none. Note this is a *different* path from the `!tools/**/example/**` negation, which covers
`tools/<slug>/example/`. **Pre-existing, unrelated to this PR, out of scope.** Worth its own
ticket; it is the same silent-stale class this PR is trying to prevent, already live on main.

---

## 9. What would clear this — two small changes

1. **`_scan`**: in the `node.keywords` loop, also record a violation when `kw.arg is None` and the
   call name is in `_IMAGE_API`. Consistent with `_resolve`'s existing "not statically knowable →
   fail" rule. No app uses `**` under `tools/` today, so nothing goes red.
2. **`test_deploy_step_still_passes_a_FILE_path_to_modal_deploy`**: select invocation lines by
   token rather than by the substring `"modal deploy"` — `shlex.split(ln)`, then
   `Path(tok[0]).name == "modal" and tok[1] == "deploy"` — or assert the deploy step's `run`
   block against an expected literal.

Both are additive to a test file, neither touches `tools/`, and each should come with the
mutation that proves it: M1/M3 for the first, M4/M4b for the second.

Everything else in this PR I checked, I found accurate. The premise re-derivation, the six-door
audit, the allowlist inversion, the scoped recursion floor and the two round-2 fixes are all
genuine and all measured. This is a good change with two holes left in it.

---

## 10. Method notes

* All work in dedicated worktrees under this session's scratchpad (`qc3head`, `qc3trunk`,
  `qc3merge`, `qc3old`, `qc3mut`, `qc3full`), removed after. The main working tree was never
  touched; no `checkout`/`reset`/`stash` was run there.
* Nothing under `tools/` is modified in what I push — this file is the whole diff.
* **Verified empirically** (marked `[RUN]`): both baselines, the merge state, the node-id
  reconciliation, the six-door audit, `pip_install_from_pyproject`, `pip_install_private_repos`,
  `run_function` (all three `include_source` values), the three registry constructors, all eleven
  allowlisted signatures, `_create_context_mount`'s narrowing semantics, both round-2 blockers,
  W3, P2, the nine-app layer table, the credential-free run, all 12 of my mutations, and the two
  pre-existing items on trunk.
* **Reasoned but NOT run**: the real-world likelihood ranking of M1 vs M4 (judgement, not
  measurement); the claim that CI resolves a different Modal version than the test job (I
  confirmed both ranges are unpinned and can diverge, but did not observe an actual divergence);
  and the builder's seven-entry residual-risk list as *the builder worded it* — I could not
  locate it (PR has 0 comments, 0 reviews, stale body), so §7 judges the risks as documented in
  the code and says so.
* I did **not** run a full suite on M4/M4b — they are workflow-only edits whose effect is
  confined to one test, and the guard file was green at 37. M1 got the full-suite treatment
  because it is the blocker.
