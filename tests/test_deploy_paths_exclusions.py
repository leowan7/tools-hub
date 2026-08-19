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
  ``tools`` package. (Verified against modal 1.4.2 by driving
  ``modal.cli.import_refs.import_file_or_module`` on each of the nine and reading
  back ``FunctionInfo.get_entrypoint_mount()``.)  A ``from . import x`` or an
  ``import tools.<slug>`` in ``modal_app.py`` would not flip that classification
  — the repo root is on ``sys.path`` at deploy time, so it would resolve locally
  and then be MISSING in the container — but it is the shape that means the
  adapter package is now part of the app, so it fails here.
* the only local file added to any image is that app's own ``run_pipeline.py``,
  via ``.add_local_file(..., copy=True)``.
* Modal builds a Dockerfile's context mount from the COPY patterns in the
  Dockerfile ALONE (``modal._utils.docker_utils.extract_copy_command_patterns``
  -> ``FilePatternMatcher``). Today they name only ``static/example/`` fixtures,
  so no ``__init__.py`` is even uploaded, let alone baked in.

If any of that stops being true, the exclusion silently stops deploying real GPU
code changes — a much worse failure than an unnecessary rebuild. So this file
fails loudly instead.

Not covered on purpose: ``run_pipeline.py`` itself, which is mounted and IS on
the deploy trigger.
"""

from __future__ import annotations

import ast
import pathlib

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

# Any tools-side __init__.py must fail to match every COPY pattern. Both a
# top-level and a per-slug path, because `**` handling differs between them.
_INIT_PATHS = [pathlib.Path("tools/__init__.py")] + [
    pathlib.Path(f"tools/{a}/__init__.py") for a in _APPS
]


def test_dockerfiles_are_discovered():
    # esmfold2_design has no Dockerfile (it builds via modal.Image.micromamba),
    # so eight is the full set, not a short read.
    assert len(_DOCKERFILES) == len(_APPS) - 1, (
        f"expected {len(_APPS) - 1} tools/*/Dockerfile.modal, found "
        f"{[str(p) for p in _DOCKERFILES]}"
    )


@pytest.mark.parametrize("path", _DOCKERFILES, ids=lambda p: p.parent.name)
def test_dockerfile_copies_no_adapter_package(path):
    patterns = extract_copy_command_patterns(path.read_text(encoding="utf-8").splitlines())
    if not patterns:
        return  # no COPY at all -> Modal builds no context mount whatsoever
    match = FilePatternMatcher(*patterns)
    pulled = [str(p) for p in _INIT_PATHS if match(p)]
    assert not pulled, (
        f"{path.parent.name}/Dockerfile.modal COPY patterns {patterns!r} pull "
        f"{pulled} into the image build context. deploy-modal.yml excludes "
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
        "local_files": [],  # (lineno, first-arg literal or sentinel)
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
                if node.args and isinstance(node.args[0], ast.Constant):
                    out["local_files"].append((node.lineno, node.args[0].value))
                else:
                    # A name/f-string here is fine — resolve it from module-level
                    # string constants below rather than waving it through.
                    out["local_files"].append((node.lineno, node.args[0] if node.args else None))
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


def _resolve(arg, scan) -> str:
    """Best-effort literal value of an ``add_local_file`` first argument."""
    if isinstance(arg, str):
        return arg
    if isinstance(arg, ast.Name):
        if arg.id in scan["consts"]:
            return scan["consts"][arg.id]
        if arg.id in scan["fstring_consts"]:
            return _resolve(scan["fstring_consts"][arg.id], scan)
        return f"<unresolved name {arg.id}>"
    if isinstance(arg, ast.JoinedStr):
        parts = []
        for v in arg.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name):
                parts.append(str(scan["consts"].get(v.value.id, f"<{v.value.id}>")))
            else:
                parts.append(f"<non-literal {type(v).__name__}>")
        return "".join(parts)
    return f"<non-literal {type(arg).__name__}>"


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
        "exclusions are known to be safe around."
    )
