"""``tools/<slug>/__init__.py`` is web-tier only — keep it that way.

``.github/workflows/deploy-modal.yml`` excludes ``tools/**/__init__.py`` from the
``tools/**`` deploy trigger, alongside the ``meta.py`` and ``example/**``
exclusions. Those files hold the Flask-side adapter class; nothing about them
reaches a GPU container, so an edit there should not rebuild and redeploy nine
images.

That is a PREMISE, not a law. It holds today because, for all nine deployed apps:

* ``modal deploy tools/<slug>/modal_app.py`` loads that file in SCRIPT mode, so
  its ``__package__`` is ``""`` and Modal classifies it ``FunctionInfoType.FILE``
  — the implicit entrypoint mount is the single ``modal_app.py`` file, not the
  ``tools`` package.  A ``from . import x`` or an ``import tools.<slug>`` in
  ``modal_app.py`` would not flip that classification — the repo root is on
  ``sys.path`` at deploy time, so it would resolve locally and then be MISSING
  in the container — but it is the shape that means the adapter package is now
  part of the app, so it fails here.
* the only local file added to any image is that app's own ``run_pipeline.py``,
  via ``.add_local_file(..., copy=True)``.
* Modal builds a Dockerfile's context mount from the COPY patterns ALONE
  (``modal._utils.docker_utils.extract_copy_command_patterns`` ->
  ``FilePatternMatcher``), whether those commands come from a
  ``Dockerfile.modal`` or from ``Image.dockerfile_commands([...])``. Today they
  name only ``static/example/`` fixtures, so no ``__init__.py`` is even
  uploaded, let alone baked in.

If any of that stops being true, the exclusion silently stops deploying real GPU
code changes — a much worse failure than an unnecessary rebuild. So this file
fails loudly instead.

Checked two ways on purpose:

1. **Syntactically** — ``ast`` over each ``modal_app.py`` and Modal's own COPY
   parser over every ``Dockerfile.modal`` *and* every literal
   ``.dockerfile_commands([...])`` list. Fast, and it names the offending line.
2. **Behaviourally** — ``tests/_deploy_upload_probe.py`` drives Modal's real
   loader per app in a subprocess and reads back the actual upload set
   (entrypoint mount, spec mounts, image ``_mount_layers`` and every
   ``context_mount_function()``). Costs ~1s per app. This is the only check that
   can see a change in *Modal* rather than in this repo: the whole exclusion
   rests on ``import_file_or_module`` resolving these files in script mode, and
   ``modal>=1.4,<2`` does not bound that — CI reinstalls the client fresh on
   every deploy. A syntactic scan structurally cannot notice.

Not covered on purpose:

* ``run_pipeline.py`` itself, which is mounted and IS on the deploy trigger.
* ``ADD`` in a Dockerfile. ``extract_copy_command_patterns`` only honours
  ``COPY``, so an ``ADD tools/<slug>/__init__.py`` puts the file in no context
  mount and the docker build fails loudly on the very deploy the Dockerfile edit
  triggers — noisy, never silently stale. If a future Modal starts honouring
  ``ADD``, check 2 above notices, because it runs Modal's parser rather than
  this file's idea of it.
* ``importlib.import_module("tools.x")`` / ``__import__("tools.x")``. The static
  ``import tools.x`` spelling fails here; the dynamic ones do not. Neither pulls
  the package into the image (verified: the upload set is unchanged), so the
  failure mode is a loud in-container ``ImportError`` on every run, not a stale
  image.
"""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys

import pytest
import yaml
from modal._utils.docker_utils import extract_copy_command_patterns
from modal.file_pattern_matcher import FilePatternMatcher

_REPO = pathlib.Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO / ".github" / "workflows" / "deploy-modal.yml"

# Every negation the trigger must keep carrying, in the order they must appear
# AFTER the positive `tools/**`. GitHub resolves `paths` later-wins, so a
# negation hoisted above `tools/**` is re-included and silently does nothing.
_EXPECTED_PATHS = [
    "tools/**",
    "!tools/**/meta.py",
    "!tools/**/example/**",
    "!tools/**/__init__.py",
    ".github/workflows/deploy-modal.yml",
]


def _deploy_matrix_apps() -> list[str]:
    """The app slugs the deploy workflow actually deploys.

    Read off the workflow rather than hardcoded, so an app added to the matrix
    without being added here fails instead of quietly going unchecked.
    """
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return list(doc["jobs"]["deploy"]["strategy"]["matrix"]["app"])


_APPS = _deploy_matrix_apps()


def test_deploy_matrix_is_the_nine_known_apps():
    """Non-vacuity floor: every check below is parametrized over this list."""
    assert sorted(_APPS) == [
        "af2",
        "boltz2",
        "colabfold",
        "esmfold",
        "esmfold2_design",
        "iggm",
        "mpnn",
        "opendde",
        "proteina",
    ], f"deploy-modal.yml matrix changed: {_APPS!r}. Re-verify the premise in this module's docstring for the new app, then update this list."


def test_every_app_has_the_excluded_init_and_a_modal_app():
    """The exclusion is only meaningful for slugs that actually have both files."""
    missing = [
        a
        for a in _APPS
        if not (_REPO / "tools" / a / "__init__.py").is_file()
        or not (_REPO / "tools" / a / "modal_app.py").is_file()
    ]
    assert not missing, f"deployed apps missing __init__.py or modal_app.py: {missing}"


def test_workflow_still_carries_every_negation_in_order():
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1).
    trigger = doc[True] if True in doc else doc["on"]
    assert trigger["push"]["paths"] == _EXPECTED_PATHS, (
        f"deploy-modal.yml push.paths is {trigger['push']['paths']!r}, expected "
        f"{_EXPECTED_PATHS!r}. GitHub applies `paths` later-wins: a negation moved "
        "above `tools/**` is re-included and stops excluding anything, and a "
        "negation dropped altogether resumes redeploying nine GPU images on "
        "web-tier-only edits."
    )


# ---------------------------------------------------------------------------
# Premise 1: no Dockerfile pulls an __init__.py into its build context.
# ---------------------------------------------------------------------------

_DOCKERFILES = sorted((_REPO / "tools").glob("*/Dockerfile.modal"))

# EVERY tools-side __init__.py must fail to match every COPY pattern — globbed,
# not enumerated. A Dockerfile is free to COPY a *non-deployed* slug's adapter
# (`tools/pxdesign/__init__.py` into af2's image, say); that file is still under
# the `!tools/**/__init__.py` exclusion, so an enumeration of the nine deployed
# slugs would miss it entirely. The glob also covers the top-level
# `tools/__init__.py`, where `**` handling differs.
_INIT_PATHS = sorted(p.relative_to(_REPO) for p in (_REPO / "tools").glob("**/__init__.py"))


def _init_paths_pulled(commands) -> list[str]:
    """Which tools-side ``__init__.py`` files these Dockerfile commands upload.

    This is exactly what Modal does in ``_create_context_mount``
    (``modal/image.py``): COPY patterns out of the commands, then a
    ``FilePatternMatcher`` over the context dir, which defaults to the repo
    root. Same two functions, so it cannot drift from the real behaviour the way
    a hand-rolled regex would.
    """
    patterns = extract_copy_command_patterns(list(commands))
    if not patterns:
        return []  # no COPY at all -> Modal builds no context mount whatsoever
    match = FilePatternMatcher(*patterns)
    return [str(p).replace("\\", "/") for p in _INIT_PATHS if match(p)]


def test_every_tools_init_py_is_globbed():
    """Non-vacuity floor: the COPY checks are only as wide as this list."""
    found = {str(p).replace("\\", "/") for p in _INIT_PATHS}
    required = {"tools/__init__.py"} | {f"tools/{a}/__init__.py" for a in _APPS}
    assert required <= found, f"glob missed {sorted(required - found)}"
    assert len(found) > len(required), (
        f"_INIT_PATHS is {sorted(found)} — only the deployed slugs. It must be "
        "globbed, so that a Dockerfile COPYing a non-deployed tool's adapter is "
        "still caught."
    )


def test_copy_scan_actually_detects_an_adapter_copy():
    """Non-vacuity floor for ``_init_paths_pulled`` itself.

    Four of the eight Dockerfiles carry no COPY at all, so half of
    ``test_dockerfile_copies_no_adapter_package`` asserts nothing (correctly —
    Modal builds no context mount there), and no app uses
    ``dockerfile_commands`` today. Without this, a change in Modal's parser or
    matcher would leave every one of those checks green over nothing.
    """
    cross_slug = next(p for p in _INIT_PATHS if p.parent.name not in {*_APPS, "tools"})
    cross_slug = str(cross_slug).replace("\\", "/")
    assert _init_paths_pulled(["FROM base", f"COPY {cross_slug} /root/x.py"]) == [cross_slug]
    assert _init_paths_pulled(["FROM base", "COPY . /app"]), "whole-context COPY not detected"
    assert _init_paths_pulled(["FROM base", "COPY tools/*/__init__.py /app/"]), "glob COPY not detected"
    assert not _init_paths_pulled(["FROM base", "COPY static/example/1HEW.pdb /root/"])
    assert not _init_paths_pulled(["FROM base", "RUN echo no-copy-here"])


def test_dockerfiles_are_discovered():
    # esmfold2_design has no Dockerfile (it builds via modal.Image.micromamba),
    # so eight is the full set, not a short read.
    assert len(_DOCKERFILES) == len(_APPS) - 1, (
        f"expected {len(_APPS) - 1} tools/*/Dockerfile.modal, found "
        f"{[str(p) for p in _DOCKERFILES]}"
    )


@pytest.mark.parametrize("path", _DOCKERFILES, ids=lambda p: p.parent.name)
def test_dockerfile_copies_no_adapter_package(path):
    pulled = _init_paths_pulled(path.read_text(encoding="utf-8").splitlines())
    assert not pulled, (
        f"{path.parent.name}/Dockerfile.modal COPY patterns pull {pulled} into "
        "the image build context. deploy-modal.yml excludes "
        "tools/**/__init__.py from the deploy trigger on the premise that no "
        "container sees it — that premise is now false, so editing an adapter "
        "would change this image without ever redeploying it. Either COPY the "
        "file explicitly from somewhere else, or drop the exclusion."
    )


# ---------------------------------------------------------------------------
# Premise 2: no modal_app.py imports its own package or mounts local source.
# ---------------------------------------------------------------------------

_FORBIDDEN_CALLS = {
    "add_local_dir",  # a directory mount can sweep in __init__.py
    "add_local_python_source",  # mounts whole importable packages by name
    "copy_local_dir",
    "copy_local_file",
}
_FORBIDDEN_KWARGS = {"mounts"}


def _scan(source: str) -> dict:
    tree = ast.parse(source)
    out = {
        "imports": [],  # (lineno, root_module, level)
        "local_files": [],  # (lineno, first-arg node or literal)
        "docker_cmds": [],  # (lineno, arg node) — one per .dockerfile_commands() element
        "dockerfiles": [],  # (lineno, arg node) — from_dockerfile(path)
        "violations": [],  # (lineno, reason)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out["imports"].append((node.lineno, alias.name.split(".")[0], 0))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            out["imports"].append((node.lineno, root, node.level))
        elif isinstance(node, ast.Attribute):
            # `modal.Mount` / `Mount.from_local_dir` — the pre-1.0 mount API.
            if node.attr == "Mount" or (
                isinstance(node.value, ast.Name) and node.value.id == "Mount"
            ):
                out["violations"].append((node.lineno, "references modal.Mount"))
            # `sys.path.insert(...)` / `sys.path.append(...)`
            if (
                node.attr == "path"
                and isinstance(node.value, ast.Name)
                and node.value.id == "sys"
            ):
                out["violations"].append((node.lineno, "manipulates sys.path"))
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in _FORBIDDEN_CALLS:
                out["violations"].append((node.lineno, f"calls .{name}()"))
            if name == "add_local_file":
                # A name/f-string here is fine — resolve it from module-level
                # string constants below rather than waving it through.
                out["local_files"].append((node.lineno, node.args[0] if node.args else None))
            if name == "dockerfile_commands":
                # Injects raw Dockerfile directives, so a COPY here pulls local
                # files in exactly like a Dockerfile does. `_flatten_str_args`
                # accepts a list/tuple or bare varargs, so handle both.
                for arg in node.args:
                    elements = (
                        arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
                    )
                    out["docker_cmds"] += [(node.lineno, e) for e in elements]
            if name == "from_dockerfile":
                out["dockerfiles"].append((node.lineno, node.args[0] if node.args else None))
            for kw in node.keywords:
                if kw.arg in _FORBIDDEN_KWARGS:
                    out["violations"].append((node.lineno, f"passes {kw.arg}="))
    out["consts"] = {
        t.id: n.value.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    out["fstring_consts"] = {
        t.id: n.value
        for n in tree.body
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.JoinedStr)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    return out


def _resolve(arg, scan) -> str | None:
    """The literal string an argument node evaluates to, or ``None``.

    ``None`` means "not statically knowable", and every caller treats that as a
    failure rather than waving it through — an unresolvable path or Dockerfile
    command is exactly the shape that hides a local-source mount from an AST
    scan.
    """
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        if arg.id in scan["consts"]:
            return scan["consts"][arg.id]
        if arg.id in scan["fstring_consts"]:
            return _resolve(scan["fstring_consts"][arg.id], scan)
        return None
    if isinstance(arg, ast.JoinedStr):
        parts = []
        for v in arg.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                inner = _resolve(v.value, scan)
                if inner is None:
                    return None
                parts.append(inner)
            else:
                return None
        return "".join(parts)
    return None


@pytest.mark.parametrize("app", _APPS)
def test_modal_app_stays_self_contained(app):
    path = _REPO / "tools" / app / "modal_app.py"
    scan = _scan(path.read_text(encoding="utf-8"))

    # Non-vacuity: the walker must actually have seen this file's code.
    assert len(scan["imports"]) >= 5, (
        f"parsed only {len(scan['imports'])} imports from {app}/modal_app.py — "
        "the scanner has drifted and every check below would pass vacuously"
    )
    assert scan["local_files"], (
        f"parsed no .add_local_file() call in {app}/modal_app.py — the scanner "
        "has drifted and every check below would pass vacuously"
    )

    package_imports = [
        (n, mod, lvl) for n, mod, lvl in scan["imports"] if lvl > 0 or mod == "tools"
    ]
    assert not package_imports, (
        f"{app}/modal_app.py imports its own package at "
        + ", ".join(f"line {n}" for n, _, _ in package_imports)
        + ". Modal deploys this file in script mode: the import resolves locally "
        "(the repo root is on sys.path) and is then MISSING inside the container. "
        "It also means tools/**/__init__.py is now app code, which deploy-modal.yml "
        "excludes from its deploy trigger. Keep modal_app.py self-contained."
    )

    assert not scan["violations"], (
        f"{app}/modal_app.py "
        + "; ".join(f"line {n}: {why}" for n, why in scan["violations"])
        + ". These can pull local Python source (including tools/**/__init__.py) "
        "into the image, which deploy-modal.yml's exclusion assumes never happens."
    )

    resolved = [(n, _resolve(a, scan)) for n, a in scan["local_files"]]
    unexpected = [(n, v) for n, v in resolved if v != f"tools/{app}/run_pipeline.py"]
    assert not unexpected, (
        f"{app}/modal_app.py adds local files other than its own run_pipeline.py: "
        + ", ".join(f"line {n}: {v!r}" for n, v in unexpected)
        + ". run_pipeline.py is the only local file the deploy trigger's "
        "exclusions are known to be safe around. (None means the argument is not "
        "a module-level string constant, so this scan cannot tell what it is.)"
    )

    # `.dockerfile_commands([...])` injects raw Dockerfile directives, so a COPY
    # there pulls local files into the image exactly like a Dockerfile COPY —
    # and it is the natural reach for esmfold2_design, which has no Dockerfile
    # to edit. Run the same parser + matcher over them.
    cmds = [(n, _resolve(a, scan)) for n, a in scan["docker_cmds"]]
    opaque = [n for n, v in cmds if v is None]
    assert not opaque, (
        f"{app}/modal_app.py passes a non-literal to .dockerfile_commands() at "
        + ", ".join(f"line {n}" for n in opaque)
        + ". This scan cannot tell whether it contains a COPY, so it cannot "
        "certify that tools/**/__init__.py stays out of the image. Use literal "
        "strings, or module-level string constants."
    )
    pulled = _init_paths_pulled([v for _, v in cmds])
    assert not pulled, (
        f"{app}/modal_app.py .dockerfile_commands() COPYs {pulled} into the "
        "image build context. deploy-modal.yml excludes tools/**/__init__.py "
        "from the deploy trigger on the premise that no container sees it — "
        "that premise is now false, so every later edit to that adapter would "
        "change this image without ever redeploying it."
    )

    # from_dockerfile() must point at the app's own Dockerfile.modal, which is
    # what test_dockerfile_copies_no_adapter_package scans. Any other path (or a
    # non-literal one) is a Dockerfile no test in this file has ever read.
    dockerfiles = [(n, _resolve(a, scan)) for n, a in scan["dockerfiles"]]
    foreign = [(n, v) for n, v in dockerfiles if v != f"tools/{app}/Dockerfile.modal"]
    assert not foreign, (
        f"{app}/modal_app.py builds from a Dockerfile this file does not scan: "
        + ", ".join(f"line {n}: {v!r}" for n, v in foreign)
        + ". Only tools/*/Dockerfile.modal is globbed for COPY patterns, so a "
        "Dockerfile anywhere else could pull tools/**/__init__.py into the "
        "image with nothing to catch it."
    )


def test_every_discovered_dockerfile_is_the_one_its_app_uses():
    """Non-vacuity floor tying the two Dockerfile checks together.

    ``_DOCKERFILES`` is a glob of ``tools/*/Dockerfile.modal``; nothing else
    proves those are the files the apps actually build from. If an app switched
    to a Dockerfile somewhere else, the glob would still find eight files, still
    scan them, and still pass — over a Dockerfile no longer in use.
    """
    used = set()
    for app in _APPS:
        scan = _scan((_REPO / "tools" / app / "modal_app.py").read_text(encoding="utf-8"))
        used |= {v for _, v in ((n, _resolve(a, scan)) for n, a in scan["dockerfiles"])}
    globbed = {str(p.relative_to(_REPO)).replace("\\", "/") for p in _DOCKERFILES}
    assert used == globbed, (
        f"apps build from {sorted(used)} but this file scans {sorted(globbed)}. "
        "Every Dockerfile an app uses must be one the COPY scan reads."
    )


# ---------------------------------------------------------------------------
# Premise 3, checked behaviourally: ask Modal what it would actually upload.
#
# Everything above reads repo source. None of it can see a change in the MODAL
# CLIENT — and the entire exclusion rests on `import_file_or_module` resolving
# these files in script mode. If that ever flipped to package mode,
# `get_entrypoint_mount()` would return `_from_local_python_packages("tools")`,
# every adapter would ship in every image, and a purely syntactic guard would
# stay green. `modal>=1.4,<2` is not a bound: CI reinstalls it fresh each run.
#
# Costs ~1s per app (subprocess start + `import modal`), ~10s for the nine. Not
# marked slow and not skippable: a skipped guard is not a guard.
# ---------------------------------------------------------------------------

_PROBE = _REPO / "tests" / "_deploy_upload_probe.py"


def _run_probe(app: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_PROBE), app],
        cwd=_REPO,  # Modal's Dockerfile context dir defaults to cwd, as in CI
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"{_PROBE.name} failed for {app} (exit {proc.returncode}).\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    from tests._deploy_upload_probe import RESULT_PREFIX

    payload = [ln for ln in proc.stdout.splitlines() if ln.startswith(RESULT_PREFIX)]
    assert payload, f"{_PROBE.name} printed no result line for {app}:\n{proc.stdout}"
    return json.loads(payload[-1][len(RESULT_PREFIX):])


@pytest.mark.parametrize("app", _APPS)
def test_modal_really_uploads_no_adapter_package(app):
    got = _run_probe(app)

    assert got["module_package"] == "", (
        f"modal deploy tools/{app}/modal_app.py no longer loads in script mode "
        f"(__package__ is {got['module_package']!r}, not ''). Modal classifies "
        "such a function FunctionInfoType.PACKAGE and mounts the WHOLE `tools` "
        "package — every adapter __init__.py included. deploy-modal.yml's "
        "!tools/**/__init__.py exclusion is unsafe as of right now."
    )

    fns = got["functions"]
    # Non-vacuity: an empty function list would pass every check below.
    assert fns, f"probe found no registered modal functions in {app}/modal_app.py"

    package_mode = [f["name"] for f in fns if f["info_type"] != "FILE"]
    assert not package_mode, (
        f"{app}: {package_mode} resolved as {[f['info_type'] for f in fns]}, not "
        "FunctionInfoType.FILE. See above — package mode mounts all of `tools`."
    )

    uploaded = sorted(
        {p for f in fns for p in f["entrypoint_mount"] + f["spec_mounts"] + f["image_files"]}
    )

    # Non-vacuity: these two files ARE uploaded today, one via the entrypoint
    # mount and one via an image context mount. If the probe's walk broke and
    # returned nothing, the __init__.py assertion below would pass over an empty
    # set — so require the walk to still find both halves.
    for expected in (f"tools/{app}/modal_app.py", f"tools/{app}/run_pipeline.py"):
        assert expected in uploaded, (
            f"probe did not find {expected} in {app}'s upload set ({uploaded}). "
            "The probe's walk over Modal's mounts has drifted; fix it before "
            "trusting anything else in this test."
        )

    adapters = [p for p in uploaded if p.endswith("__init__.py")]
    assert not adapters, (
        f"Modal uploads {adapters} for {app}. deploy-modal.yml excludes "
        "tools/**/__init__.py from the deploy trigger on the premise that no "
        "container sees it. That premise is now FALSE: every later edit to "
        "those files changes this image and never redeploys it. Full upload "
        f"set: {uploaded}"
    )
