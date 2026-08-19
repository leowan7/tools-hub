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
   Image methods are checked against an ALLOWLIST read off ``dir(modal.Image)``,
   not a denylist: most of Modal's image API moves local bytes through
   ``DockerfileSpec.context_files``, which is not a mount.

   A parser can always be out-spelled, and this one was, five times across three
   review rounds: ``**`` expansion (``kw.arg`` is ``None``), a ``getattr``
   callee, a dict-dispatch callee, a conditional callee, and an Image method
   bound to a variable instead of called. All five are now refused, and the
   shape of the fix is deliberately not "another name in a list": anything this
   scan cannot READ is refused, the same rule ``_resolve`` applies to arguments.
2. **Behaviourally** — ``tests/_deploy_upload_probe.py`` drives Modal's real
   loader per app in a subprocess and reads back the actual upload set
   (entrypoint mount, spec mounts, image ``_mount_layers``, every
   ``context_mount_function()``, and every layer's
   ``DockerfileSpec.context_files``). Costs ~1s per app. This is the only check
   that can see a change in *Modal* rather than in this repo: the whole
   exclusion rests on ``import_file_or_module`` resolving these files in script
   mode, and ``modal>=1.4,<2`` does not bound that — CI reinstalls the client
   fresh on every deploy. A syntactic scan structurally cannot notice.

   The ``context_files`` half of that walk exists because check 1 was the sole
   cover for that channel and kept being out-spelled. It asks each layer's own
   ``dockerfile_function`` what it will open, so it is indifferent to how the
   call was written — it catches all five spellings above independently.

Neither check can see script-vs-package mode, which is a property of the
*invocation* — the probe hardcodes ``use_module_mode=False``, so it reports FILE
by construction however the workflow is written. That is pinned separately, by
reading the deploy job's own shell lines
(``test_deploy_step_still_passes_a_FILE_path_to_modal_deploy``).

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
* a local-path keyword that a FUTURE modal adds to one of the eleven methods in
  ``_ALLOWED_IMAGE_CALLS`` and routes through ``context_files``. The allowlist
  covers a new *method*; it does not cover a new *kwarg* on an allowed one, and
  that is the one shape here that would be silent rather than loud. Pinning each
  allowed method's full signature would close it, at the price of a red CI on
  every cosmetic Modal kwarg addition — not worth it while the two kwargs that
  exist today (``context_files=``, ``spec_file=``) are banned by name, and while
  an unreadable keyword list (``**kwargs``) is refused outright.
* a deploy invocation this file cannot tokenise at all — a command assembled
  into a shell variable and run through ``eval``, or fetched from elsewhere.
  ``_invokes_deploy`` reads tokens, so it sees any *written* invocation however
  it is spaced or aliased, but not one that does not exist as a line.
* the SECOND assignment of a module-level constant. ``_scan``'s ``consts`` map
  is last-wins, so ``_P = adapter`` / ``add_local_file(_P)`` / ``_P =
  run_pipeline`` resolves to the innocent value. It is not silent, though:
  ``add_local_file`` builds a real mount, so check 2 sees the adapter and fails.

Two shapes that look like gaps and are not, both measured rather than reasoned:
``modal.Mount`` does not exist in modal 1.4.2 at all (an aliased
``from modal import Mount as M`` is an ImportError at deploy time, not a quiet
bypass — the ``Mount`` check here is legacy cover for a downgrade), and a
``*args`` expansion into ``dockerfile_commands`` resolves to ``None`` and trips
the ``opaque`` assertion.
"""

from __future__ import annotations

import ast
import json
import pathlib
import shlex
import subprocess
import sys

import modal
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


def _shell_lines(run_blocks) -> list[str]:
    """Executable lines of some ``run:`` blocks — comment-only lines dropped.

    Stripping matters: the check below is the ONLY thing in the repo that can
    see script-vs-package mode, and a substring match can only ever be
    satisfied, never contradicted. Rewriting the command while leaving the old
    one above it as ``# was: modal deploy "$app_file"`` is the most ordinary way
    a person edits a command line, and it would keep the literal in view while
    the real invocation had changed.
    """
    return [
        stripped
        for block in run_blocks
        for line in block.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]


def _deploy_job_shell_lines() -> list[str]:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return _shell_lines(s["run"] for s in doc["jobs"]["deploy"]["steps"] if "run" in s)


def _invokes_deploy(line: str) -> bool:
    """Does this shell line invoke ``modal deploy``? Decided on TOKENS.

    The assertion below is an allowlist ("every invocation must be the file
    form"), but an allowlist is only as wide as the SELECTOR that feeds it, and
    a substring test (``"modal deploy" in line``) is a denylist wearing a
    selector's clothes: a line it does not select is never asserted about at
    all. ``modal  deploy`` — one extra space, a typo rather than an attack — and
    an aliased ``$MODAL_BIN deploy`` both walk straight past it while the
    retained compliant line keeps ``assert invocations`` satisfied.

    So select on the bare token ``deploy`` instead, and say nothing about the
    binary: an alias, an absolute path, ``python -m modal deploy`` and any
    amount of whitespace all still land in the allowlist assertion. ``echo
    "Deploying $app_file"`` and ``tee "deploy-<app>.log"`` do not, because
    neither yields ``deploy`` as a token of its own.
    """
    try:
        tokens = shlex.split(line)
    except ValueError:
        # Unbalanced quoting: this scan cannot tell what the line runs, so audit
        # it rather than skip it. Same "not statically knowable -> fail" rule
        # `_resolve` applies to the AST side.
        return True
    return "deploy" in tokens


def test_shell_line_scan_ignores_commented_out_commands():
    """Non-vacuity floor for ``_shell_lines``."""
    lines = _shell_lines(['# was: modal deploy "$app_file" | tee x.log\nmodal deploy -m "tools.mpnn.modal_app"'])
    assert lines == ['modal deploy -m "tools.mpnn.modal_app"'], lines


def test_deploy_invocation_selector_is_tokenwise_not_a_substring():
    """Non-vacuity floor for ``_invokes_deploy``.

    Every spelling here reaches a real ``modal deploy``; a selector that misses
    one silently exempts it from the file-path allowlist below. The negatives
    matter just as much — a selector that fires on ``echo "Deploying ..."``
    would red-light the workflow as it stands and get loosened back.
    """
    for spelling in (
        'modal deploy "$app_file" | tee "deploy-${{ matrix.app }}.log"',
        'modal  deploy -m "tools.mpnn.modal_app"',  # one extra space
        '$MODAL_BIN deploy -m "tools.mpnn.modal_app"',  # aliased binary
        '/usr/local/bin/modal deploy -m "tools.mpnn.modal_app"',  # absolute path
        'python -m modal deploy -m "tools.mpnn.modal_app"',  # module invocation
        'modal deploy "unbalanced',  # unparseable -> audited, not skipped
    ):
        assert _invokes_deploy(spelling), f"selector missed: {spelling}"
    for benign in (
        'echo "Deploying $app_file"',
        'tee "deploy-${{ matrix.app }}.log"',
        'app_file="tools/${{ matrix.app }}/modal_app.py"',
    ):
        assert not _invokes_deploy(benign), f"selector cried wolf over: {benign}"


def test_deploy_step_still_passes_a_FILE_path_to_modal_deploy():
    """Script mode is a property of HOW the workflow invokes modal, not just of
    the source. ``modal deploy tools/<app>/modal_app.py`` loads by path
    (``FunctionInfoType.FILE``); ``modal deploy -m tools.<app>.modal_app`` would
    load the same source as a package, mount all of ``tools``, and make the
    ``!tools/**/__init__.py`` exclusion unsafe — with every other test here,
    probe included, still green, because they all assume the path form (the
    probe hardcodes ``use_module_mode=False``, so it reports FILE by
    construction no matter what the workflow does).

    Stated positively, and per invocation rather than over the joined text:
    EVERY ``modal deploy`` the job runs must be the file-path form. An
    enumeration of module spellings (``-m``, ``--module``, ...) would be a
    denylist that the next spelling walks past — and so would a substring
    SELECTOR, which is why ``_invokes_deploy`` tokenises.
    """
    lines = _deploy_job_shell_lines()
    runs = "\n".join(lines)
    assert 'app_file="tools/${{ matrix.app }}/modal_app.py"' in runs, (
        "the deploy step no longer builds a tools/<app>/modal_app.py path; "
        f"re-derive the script-mode premise. Steps run:\n{runs}"
    )
    invocations = [ln for ln in lines if _invokes_deploy(ln)]
    assert invocations, (
        "the deploy job runs no `modal deploy` at all — this pin is now "
        f"asserting nothing. Steps run:\n{runs}"
    )
    not_a_file_path = [ln for ln in invocations if not ln.startswith('modal deploy "$app_file"')]
    assert not not_a_file_path, (
        "the deploy job invokes modal deploy with something other than the "
        f"`modal deploy \"$app_file\"` file path: {not_a_file_path}. Anything "
        "module-shaped (`modal deploy -m ...`) loads in PACKAGE mode and mounts "
        "the whole tools package. An aliased or absolute-path binary "
        "(`$MODAL_BIN deploy ...`) is refused for the same reason: this pin "
        "cannot tell what it resolves to. Spell the line exactly as it is "
        f"written today. Steps run:\n{runs}"
    )


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


def test_no_dockerignore_narrows_what_modal_uploads():
    """A ``.dockerignore`` silently shrinks BOTH checks' field of view.

    Modal's ``_create_context_mount`` calls ``find_dockerignore_file``, which
    looks in exactly three places (read off that function, not the docs):
    ``<dir>/<dockerfile-name>.dockerignore``, ``<dir>/.dockerignore``, and
    ``.dockerignore`` in the context dir (the repo root). Whatever it finds
    becomes an ``ignore`` matcher over the Dockerfile context mount.

    Measured: a root ``.dockerignore`` of ``**/__init__.py`` drops
    ``tools/mpnn/__init__.py`` out of the probe's ``image_files`` for the very
    same Dockerfile COPY that puts it there without one. That is the safe
    direction *today* — an ignored file genuinely is not shipped — but it means
    the behavioural check quietly stops covering the Dockerfile context channel,
    with nothing red to say so, exactly like the truncated ``_image_chain`` walk
    the layer floor now catches. Round 2's floor does not notice, because the
    two files it requires arrive via ``add_local_file``, not the context mount.

    None of the three exist today, so this costs nothing. If one is ever wanted,
    delete this test deliberately and re-derive what the probe still proves.
    """
    found = [
        str(p.relative_to(_REPO)).replace("\\", "/")
        for p in [
            _REPO / ".dockerignore",
            *(_REPO / "tools").glob("*/.dockerignore"),
            *(_REPO / "tools").glob("*/Dockerfile.modal.dockerignore"),
        ]
        if p.exists()
    ]
    assert not found, (
        f"{found} exists. Modal applies it as an `ignore` matcher over the "
        "Dockerfile build context, so tests/_deploy_upload_probe.py stops "
        "seeing whatever it excludes and reports a SMALLER upload set with "
        "nothing failing. Re-derive what the behavioural check still covers "
        "before keeping it."
    )


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
    # Pre-1.0 spellings, gone from `dir(modal.Image)` and so invisible to the
    # allowlist below. Kept so a downgrade or a shim cannot resurrect them.
    "copy_local_dir",
    "copy_local_file",
    # These two are Image methods, so the allowlist below would already refuse
    # them -- but that branch is skipped for a receiver the modal-class
    # exemption accepts, and THIS branch is checked first. They are here rather
    # than only in `_ALLOWED_IMAGE_CALLS`'s complement because they are the two
    # refused methods `_deploy_upload_probe` cannot see: every other refused
    # method ships its bytes through `DockerfileSpec.context_files`, which the
    # probe reads back off each layer, so a spelling that evades this scan is
    # still caught there. `run_function` hides its mount in a `build_function`
    # free variable and `pip_install_from_pyproject` inlines the file's CONTENT
    # into `spec.commands`; neither channel is walked. For those two the scan is
    # the only check, so it must not be skippable by naming a receiver
    # `Secret`. MEASURED: `Secret = modal.Image.from_dockerfile(...)` plus
    # `Secret.run_function(_adapter.build_payload)` mounted all 104 files of the
    # `tools` package into the mpnn image with the whole suite green.
    "pip_install_from_pyproject",
    "run_function",
}

# Modal's Image API as the INSTALLED CLIENT reports it, not an enumeration. A
# denylist of dangerous methods goes stale silently the moment Modal adds one —
# which is exactly how `context_files=` got through. This inverts it: anything
# on the Image that is not on the reviewed-safe list below fails, including a
# method a future `modal>=1.4,<2` introduces.
_IMAGE_API = {n for n in dir(modal.Image) if not n.startswith("_")}

# Reviewed against modal 1.4.2 by BUILDING each candidate image and reading the
# `DockerfileSpec` its `dockerfile_function` returns — not by reading the docs.
# Every method here came back with `context_files` empty (or holding only
# Modal's own `/modal_requirements.txt`), and either takes no local path at all
# or has its path argument resolved and compared below (`add_local_file`,
# `from_dockerfile`) or its COPY patterns scanned (`dockerfile_commands`,
# `from_dockerfile`).
#
# What is deliberately NOT here is the `context_files` family: methods that read
# LOCAL BYTES and ship them straight into the build context without ever
# creating a mount (`modal/image.py`: `for filename, path in
# dockerfile.context_files.items(): open(path, "rb")`). Measured, one image each:
#
#   pip_install_from_requirements(p)   -> {"/.requirements.txt": p}
#   poetry_install_from_file(a, b)     -> {"/.pyproject.toml": a, "/.poetry.lock": b}
#   uv_sync(d)                         -> {"/.pyproject.toml": ..., "/.uv.lock": ...}
#   uv_pip_install(requirements=[p])   -> {"/.0_<basename of p>": p}
#   micromamba_install(spec_file=p)    -> {"/spec.yaml": p}      <- kwarg-banned
#   dockerfile_commands(context_files=)-> whatever you pass       <- kwarg-banned
#
# The COPY scan sees only the in-context filename (`/.requirements.txt`), never
# the local path it was read from. The probe now reads the same
# `DockerfileSpec.context_files` back off each layer, so this list is no longer
# the only thing standing between an adapter and an image — but it is still the
# check that names the line, so keep it accurate.
#
# Two more are refused for a different reason, so do not "fix" them by
# allowlisting:
#
#   `pip_install_from_pyproject(p)` ships no bytes but inlines p's CONTENT into
#   the RUN command (measured: a canary package name from the local file appears
#   in `spec.commands`).
#
#   `run_function(f)` returns `context_files={}` for every `include_source`
#   value, so the `include_source` framing is not the reason it is refused. The
#   reason is that it stores a `build_function` free variable whose
#   `FunctionInfo` carries its own mount, and `_deploy_upload_probe._image_files`
#   never walks it (it visits `_mount_layers`, `context_mount_function` and
#   `mount`, and nothing else). Allowlisting it would put a whole mount channel
#   out of the behavioural check's sight. Today it happens to be harmless only
#   because that FunctionInfo follows the same script-mode rule as the
#   entrypoint — i.e. it rests on the same premise this file exists to guard.
#
# `pip_install_private_repos` genuinely returns `context_files={}` and is off the
# list only because no app needs it.
_ALLOWED_IMAGE_CALLS = {
    "add_local_file",
    "apt_install",
    "debian_slim",
    "dockerfile_commands",
    "env",
    "from_dockerfile",
    "micromamba",
    "micromamba_install",
    "pip_install",
    "run_commands",
    "workdir",
}

# Public CLASSES on the modal namespace, read off the installed client, minus
# Image itself. `_IMAGE_API` matches on the attribute NAME alone, so when modal
# 1.5 added `Image.from_name`, every app's `modal.Volume.from_name(...)` handle
# started failing — a false red on nine apps, and one that only appears on the
# client CI resolves (`modal>=1.4,<2`), not the one a laptop happens to have.
# Widening `_ALLOWED_IMAGE_CALLS` would have been the wrong fix: it is global, so
# permitting `Volume.from_name` would permit the real `Image.from_name` forever.
#
# Restricted to `isinstance(..., type)` ON PURPOSE. `dir(modal)` also carries the
# lowercase SUBMODULES (`modal.image`, `modal.volume`, ...), and exempting a
# receiver spelled `image` would wave through the real
# `image.pip_install_from_requirements(...)` — the exact call this file exists to
# catch.
_MODAL_NON_IMAGE_CLASSES = {
    n for n in dir(modal) if not n.startswith("_") and isinstance(getattr(modal, n), type)
} - {"Image"}


def _modal_bound_names(tree) -> set:
    """Names this module bound with ``from modal import <a non-Image class>``.

    Only these bare names may be exempted below. Collected in their own pass
    because ``ast.walk`` gives no ordering guarantee between the import and the
    calls that rely on it.
    """
    return {
        (alias.asname or alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "") == "modal"
        and not node.level
        for alias in node.names
        if alias.name in _MODAL_NON_IMAGE_CLASSES
    }


def _receiver_is_another_modal_class(fn, modal_bound_names) -> bool:
    """Is this call's receiver EXPLICITLY a modal class that is not ``Image``?

    The one false-red family that is statically decidable: ``modal.Volume
    .from_name`` and ``modal.Secret.from_name`` say what they are, right there
    in the source. A receiver this scan cannot identify — any local variable —
    stays checked, so nothing is waved through on a guess.

    "Explicitly" has to mean the BINDING, not the spelling. An earlier version
    read the receiver's name and asked whether that name was a modal class,
    which exempted any local variable that happened to be spelled like one:
    ``Secret = modal.Image.from_dockerfile(...)`` then ``Secret.run_function(f)``
    passed, and shipped every adapter into a GPU image with the suite green. So
    a bare name counts only when this module actually imported it from modal,
    and a dotted receiver only when the chain is rooted at the ``modal`` module
    itself — ``_h.Volume.uv_sync(...)`` is somebody's helper, not modal's.
    """
    if not isinstance(fn, ast.Attribute):
        return False
    receiver = fn.value
    if isinstance(receiver, ast.Attribute):
        return (
            isinstance(receiver.value, ast.Name)
            and receiver.value.id == "modal"
            and receiver.attr in _MODAL_NON_IMAGE_CLASSES
        )
    return isinstance(receiver, ast.Name) and receiver.id in modal_bound_names


_FORBIDDEN_KWARGS = {
    "mounts",
    # Repoints Modal's build context away from cwd (the repo root), which is what
    # `_init_paths_pulled` matches COPY patterns against. Allowing it would let
    # the COPY scan above answer confidently and wrongly.
    "context_dir",
    # The `context_files` channel, reached through the two allowed methods that
    # expose it. `dockerfile_commands(context_files={"/.x": "tools/mpnn/__init__.py"})`
    # bakes the adapter into the image while the COPY it pairs with names only
    # `/.x`; `micromamba_install(spec_file=...)` does the same with one local
    # path. Neither is a mount, so neither layer of this file can see it.
    "context_files",
    "spec_file",
}


def _scan(source: str) -> dict:
    tree = ast.parse(source)
    modal_bound_names = _modal_bound_names(tree)
    # Attribute nodes that ARE a call's callee, so the "bound but not called"
    # check below does not double-report every ordinary `image.pip_install(...)`.
    called_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
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
            # A refused Image method BOUND rather than called —
            # `_f = image.pip_install_from_requirements` … `_f(adapter)`. The
            # call node's callee is then a bare Name, so the allowlist check
            # below never sees the method name. MEASURED to be real, not
            # theoretical: that pair ships `context_files={'/.requirements.txt':
            # 'tools/mpnn/__init__.py'}` on the resulting image, which no MOUNT
            # walk can see (the probe now reads that channel directly, so this
            # is double-covered — but this is the check that names the line).
            if (
                node.attr in _IMAGE_API
                and node.attr not in _ALLOWED_IMAGE_CALLS
                and id(node) not in called_funcs
            ):
                out["violations"].append(
                    (node.lineno, f"binds .{node.attr} without calling it")
                )
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                name = fn.attr
            elif isinstance(fn, ast.Name):
                name = fn.id
            else:
                # The callee is not a plain name or attribute:
                # `getattr(image, "uv_sync")(...)`, `_D["m"](...)`,
                # `(a if c else b)(...)`, `make_image().x(...)`'s outer call.
                # Every check in this branch keys off `name`, so an unresolvable
                # callee means none of them can run. Refuse instead of waving
                # it through — the same "not statically knowable -> fail" rule
                # `_resolve` applies to arguments. MEASURED: `getattr(image,
                # "pip_install_from_requirements")("tools/mpnn/__init__.py")`
                # really does ship the adapter as `context_files`.
                name = ""
                out["violations"].append(
                    (node.lineno, "calls a dynamically-resolved callee")
                )
            if name in _FORBIDDEN_CALLS:
                out["violations"].append((node.lineno, f"calls .{name}()"))
            elif (
                name in _IMAGE_API
                and name not in _ALLOWED_IMAGE_CALLS
                and not _receiver_is_another_modal_class(fn, modal_bound_names)
            ):
                # Matched on the attribute name alone, so an unrelated call that
                # happens to share a name with an Image method (`cfg.build()`,
                # `cfg.clone()`) lands here too. That is the safe direction: it
                # is loud. Narrowing it would need to know the receiver's TYPE,
                # which an AST scan cannot. The right fix for such a false red
                # is to RENAME the local call — not to allowlist the name, which
                # would permit the real Image method for everyone. The assertion
                # message spells that out, because "add it to
                # _ALLOWED_IMAGE_CALLS" is how an allowlist erodes.
                out["violations"].append(
                    (node.lineno, f"calls .{name}(), which is not in _ALLOWED_IMAGE_CALLS")
                )
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
                elif kw.arg is None and name in _IMAGE_API:
                    # `**` expansion: Python sets kw.arg to None, so the ban
                    # above matches nothing. The contents are not statically
                    # knowable — not for a dict literal either, since the same
                    # syntax accepts a name, a call or a merge — so THIS IS NOT
                    # FIXABLE BY WIDENING _ALLOWED_IMAGE_CALLS OR BY ADDING A
                    # NAME TO ANY LIST. An image-builder call whose keywords
                    # cannot be read cannot be audited, so it is refused
                    # outright: spell the keywords literally.
                    out["violations"].append(
                        (node.lineno, f"passes ** keyword expansion to .{name}()")
                    )
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


def test_image_api_allowlist_is_live_and_refuses_the_context_files_family():
    """Non-vacuity floor for the allowlist, plus the audit result as an assertion.

    ``_IMAGE_API`` is read off the installed client, so it has two ways to rot:
    it could come back empty (every call waved through), or Modal could rename an
    allowed method (the allowlist entry goes dead and the new name is refused
    without anyone noticing why). Both fail here.

    The second half pins the audit itself: the four ``context_files`` methods,
    the two package mounters and the two that reach local source another way
    must all be refused. If a future Modal renames one of them, this goes red
    and the rename gets re-reviewed rather than silently re-opening the channel.
    """
    assert {"add_local_file", "from_dockerfile", "dockerfile_commands"} <= _IMAGE_API, (
        f"dir(modal.Image) no longer looks like the Image API: {sorted(_IMAGE_API)}. "
        "The allowlist check in _scan() is now waving every method call through."
    )
    dead = _ALLOWED_IMAGE_CALLS - _IMAGE_API
    assert not dead, (
        f"allowlisted Image methods that modal {modal.__version__} does not have: "
        f"{sorted(dead)}. Modal renamed or removed them; re-review before editing "
        "this list, because whatever replaced them is currently refused."
    )
    must_refuse = {
        # measured: ships local bytes via DockerfileSpec.context_files, no mount
        "pip_install_from_requirements",
        "poetry_install_from_file",
        "uv_pip_install",
        "uv_sync",
        # mounts importable packages, i.e. every tools/**/__init__.py
        "add_local_dir",
        "add_local_python_source",
        # measured: inlines the local file's CONTENT into the RUN command
        "pip_install_from_pyproject",
        # carries a `build_function` whose mount `_image_files` never walks
        "run_function",
    }
    assert must_refuse <= (_IMAGE_API - _ALLOWED_IMAGE_CALLS), (
        "these Image methods move local bytes into an image and must stay "
        f"refused: {sorted(must_refuse & _ALLOWED_IMAGE_CALLS)} are allowlisted, "
        f"{sorted(must_refuse - _IMAGE_API)} are no longer on modal.Image."
    )


def test_non_image_receiver_exemption_is_narrow():
    """Floor for ``_receiver_is_another_modal_class`` — the only exemption here.

    It exists because ``_IMAGE_API`` matches on the attribute name alone and
    modal 1.5 added ``Image.from_name``, turning all nine apps'
    ``modal.Volume.from_name(...)`` red. Note this only reproduces on the client
    CI resolves from ``modal>=1.4,<2``: on modal 1.4.2 there is no
    ``Image.from_name`` and the false red does not exist at all. So the exemption
    cannot be trusted to a local run, and every edge below is pinned.

    An exemption is the one thing in this file that can make a check say
    nothing, so it must stay as narrow as its reason.
    """
    # The reason it exists: a different modal class, named explicitly.
    assert not _scan('vol = modal.Volume.from_name("x", create_if_missing=True)')["violations"]
    assert not _scan('s = modal.Secret.from_name("x")')["violations"]
    assert not _scan('from modal import Volume\nvol = Volume.from_name("x")')["violations"]

    # ...and everything it must NOT reach. The refused method is picked off the
    # INSTALLED client rather than named, because the one that caused this
    # (`Image.from_name`) exists on modal 1.5 and not on 1.4.2 — a literal here
    # would pass vacuously on one version and fail on the other, which is the
    # whole reason this false red reached CI unnoticed.
    refused = sorted(_IMAGE_API - _ALLOWED_IMAGE_CALLS - _FORBIDDEN_CALLS)[0]
    assert _scan(f'modal.Image.{refused}("x")')["violations"], "an Image receiver is still checked"
    assert not _scan(f'modal.Volume.{refused}("x")')["violations"], "a Volume receiver is exempt"
    assert _scan('modal.Image.from_registry("x")')["violations"], "an Image receiver is still checked"
    assert _scan('image.pip_install_from_requirements("tools/mpnn/__init__.py")')["violations"], (
        "a receiver this scan cannot identify must stay checked — waving through "
        "an unidentifiable receiver is how the exemption becomes the hole"
    )
    assert _scan('image.uv_sync("tools/mpnn")')["violations"]

    # A local variable SPELLED like a modal class is not a modal class. Every
    # source below was green while this exemption matched on the name alone, and
    # the first one really did mount all 104 files of the `tools` package into
    # the mpnn image with the full suite passing. `Volume = modal.Image` is not
    # here because it never passed: its receiver is a Call, not a Name, so it
    # was caught by accident — which is why binding the INSTANCE is the shape
    # that has to be pinned.
    _BIND = 'Secret = modal.Image.from_dockerfile("D")'
    for src in (
        _BIND + '\n' + 'Secret.run_function(build)',
        _BIND + '\n' + 'Secret.pip_install_from_pyproject("p")',
        _BIND.replace("Secret", "Volume") + '\n' + 'Volume.uv_sync("tools/mpnn")',
        _BIND.replace("Secret", "Dict")
        + '\n'
        + 'Dict.uv_pip_install(requirements=["tools/mpnn/__init__.py"])',
        # ...and a dotted receiver that is not rooted at the `modal` module.
        '_h.Volume.pip_install_from_requirements("tools/mpnn/__init__.py")',
    ):
        assert _scan(src)["violations"], (
            f"a receiver named after a modal class is exempted: {src!r}. The "
            "exemption must key on the BINDING, not on the spelling."
        )

    # Importing the class is the binding that earns the exemption, and it must
    # keep working through an alias — `from modal import Volume as V`.
    assert not _scan('from modal import Volume as V' + '\n' + 'V.from_name("x")')["violations"]
    # ...but importing that name from somewhere else does not.
    assert _scan(
        'from notmodal import Volume' + '\n' + 'Volume.uv_sync("tools/mpnn")'
    )["violations"]

    # `run_function` and `pip_install_from_pyproject` are the two refused
    # methods the behavioural probe cannot see, so they are additionally in
    # `_FORBIDDEN_CALLS`, which is checked BEFORE this exemption — no receiver
    # spelling reaches them, not even a genuinely imported modal class.
    assert _scan('from modal import Secret' + '\n' + 'Secret.run_function(build)')["violations"]
    assert {"run_function", "pip_install_from_pyproject"} <= _FORBIDDEN_CALLS

    # The lowercase SUBMODULES must never be exemptable: `modal.image` is a
    # module, and a receiver spelled `image` is the normal way an app names its
    # Image. If this set ever admitted them, the line above would go green.
    assert "image" not in _MODAL_NON_IMAGE_CLASSES
    assert "Image" not in _MODAL_NON_IMAGE_CLASSES
    assert {"Volume", "Secret"} <= _MODAL_NON_IMAGE_CLASSES, (
        f"modal {modal.__version__} no longer exposes Volume/Secret as classes: "
        f"{sorted(_MODAL_NON_IMAGE_CLASSES)}. The exemption is now dead and the "
        "nine apps' volume handles are about to go red."
    )


def test_scan_flags_every_forbidden_call_and_kwarg():
    """Non-vacuity floor for ``_scan``'s violation branch itself.

    No app uses any of these today, so ``test_modal_app_stays_self_contained``
    asserts ``not scan["violations"]`` over nine files that never populate it.
    Without this, an AST refactor could stop detecting them entirely and nine
    tests would stay green over nothing.
    """
    for src in (
        'image.pip_install_from_requirements("tools/mpnn/__init__.py")',
        'image.add_local_dir("tools", "/root")',
        'image.copy_local_file("tools/mpnn/__init__.py", "/root/x.py")',
        'image.uv_sync("tools/mpnn")',
        'image.dockerfile_commands(["COPY /.x /root/x.py"], context_files={"/.x": "tools/mpnn/__init__.py"})',
        'image.micromamba_install(spec_file="tools/mpnn/__init__.py")',
        'image.run_function(build)',
        # The same four kwargs, spelled so `kw.arg` is None. Python does not
        # resolve a `**` expansion at parse time, so nothing here can be read by
        # name; each of these was green before the `kw.arg is None` refusal, and
        # the first one was MEASURED to bake tools/mpnn/__init__.py into the
        # image with the whole suite still passing.
        'image.dockerfile_commands(["COPY /.x /r"], **{"context_files": {"/.x": "tools/mpnn/__init__.py"}})',
        '_K = {"context_files": {"/.x": "tools/mpnn/__init__.py"}}\nimage.dockerfile_commands(["COPY /.x /r"], **_K)',
        'image.micromamba_install(**{"spec_file": "tools/mpnn/__init__.py"})',
        'image.from_dockerfile("tools/mpnn/Dockerfile.modal", **{"context_dir": "/elsewhere"})',
        # Indirection: the callee is not a name this scan can read. Both
        # MEASURED to ship the adapter via DockerfileSpec.context_files, which
        # the behavioural probe cannot see.
        'getattr(image, "pip_install_from_requirements")("tools/mpnn/__init__.py")',
        'getattr(image, "add_local_dir")("tools", "/root")',
        '_D = {"m": image.uv_sync}\n_D["m"]("tools/mpnn")',
        '(image.uv_sync if x else image.pip_install)("tools/mpnn")',
        # Bound-not-called: the call site's callee is a bare local name.
        '_f = image.pip_install_from_requirements\n_f("tools/mpnn/__init__.py")',
    ):
        assert _scan(src)["violations"], f"_scan() saw nothing wrong with: {src}"
    # ...and does not cry wolf over what the nine apps really do.
    assert not _scan(
        'image.micromamba_install("cudatoolkit", channels=["conda-forge"])'
        '.add_local_file(_P, _R, copy=True)'
    )["violations"]
    # A `**` expansion into something that is NOT an Image method is ordinary
    # style (129 call sites across 52 files in this repo) and must stay green,
    # or the refusal above gets loosened for reasons unrelated to images.
    assert not _scan('subprocess.run(cmd, **kwargs)')["violations"]
    assert not _scan('modal.App("x", **app_kwargs)')["violations"]


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
        "into the image, which deploy-modal.yml's exclusion assumes never "
        "happens.\n"
        "REMEDY, in order:\n"
        "(a) `** keyword expansion` / `dynamically-resolved callee` / `binds "
        ".x without calling it`: NOT an allowlist question. This scan cannot "
        "read what the call does, and no list entry changes that. Spell the "
        "call and its keywords out literally.\n"
        "(b) the name is NOT a modal.Image method here — an ordinary "
        "`cfg.build()`, `cfg.clone()`, `argv.imports()` that merely collides "
        "with one. _IMAGE_API matches on the attribute name alone, so this is "
        "a false red. RENAME the local call or bind it through a different "
        "receiver. Do NOT allowlist it: `_ALLOWED_IMAGE_CALLS` is global, so "
        "silencing `cfg.build()` permanently permits the real `Image.build()` "
        "too.\n"
        "(c) it IS an Image method that provably ships no local bytes — check "
        "modal/image.py for a DockerfileSpec(context_files=...) built from one "
        "of its arguments, and for local file CONTENT inlined into "
        "spec.commands. Only then add it to _ALLOWED_IMAGE_CALLS, with that "
        "measurement as the reason."
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


def test_probe_sees_the_context_files_channel():
    """Non-vacuity floor for the probe's ``DockerfileSpec.context_files`` walk.

    That walk is the SECOND cover for the one channel this file's AST scan is
    solely responsible for, and three review rounds found five different
    spellings that got past the scan. It is worth exactly nothing unless it
    reports something: no app populates ``context_files`` today, so it returns
    ``[]`` for all nine and would go on returning ``[]`` if the closure name it
    reads were renamed — silently restoring single coverage.

    So build an image that really does ship a local file and make the probe
    name it.
    """
    from tests._deploy_upload_probe import context_file_paths

    shipper = modal.Image.debian_slim().pip_install_from_requirements(
        str(_REPO / "requirements.txt")
    )
    # The probe is handed Modal's internal `_Image` off `fn.spec.image`; an
    # image built here arrives wrapped by synchronicity, so unwrap it the same
    # way Modal does. If this stops finding an impl the assertion below fails,
    # which is the right direction: it means the probe's idea of what it walks
    # needs re-deriving.
    impl = next(
        (getattr(shipper, a) for a in vars(shipper) if a.startswith("_sync_original_")),
        shipper,
    )
    assert set(context_file_paths(impl, _REPO)) == {"requirements.txt"}, (
        "the probe no longer reads DockerfileSpec.context_files off an image "
        "layer, so tests/_deploy_upload_probe.py is blind to the channel that "
        "pip_install_from_requirements, uv_sync, micromamba_install(spec_file=) "
        "and dockerfile_commands(context_files=) all use. Fix it before "
        "trusting test_modal_really_uploads_no_adapter_package: the AST scan "
        "in this file is the only other thing covering it."
    )


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

    # Both of those live on the OUTERMOST layer, so the floor above survives a
    # walk that stops recursing into base images — and everything the deeper
    # layers bake in would vanish from view with nothing red. An image that
    # yielded a local file necessarily has a layer under it: `add_local_file(
    # copy=True)` builds `_from_args(base_images={"base": self}, ...)`. So one
    # walked layer means `_image_chain` stopped following the chain, not that the
    # image is flat.
    shallow = [f["name"] for f in fns if f["image_files"] and f["image_layers_walked"] < 2]
    assert not shallow, (
        f"{app}: {shallow} reported image files from a single walked layer. "
        "_deploy_upload_probe._image_chain has stopped recursing through "
        "base_images/base_image, so this test now certifies only the outermost "
        f"layer. Per-function layers walked: "
        f"{ {f['name']: f['image_layers_walked'] for f in fns} }"
    )

    adapters = [p for p in uploaded if p.endswith("__init__.py")]
    assert not adapters, (
        f"Modal uploads {adapters} for {app}. deploy-modal.yml excludes "
        "tools/**/__init__.py from the deploy trigger on the premise that no "
        "container sees it. That premise is now FALSE: every later edit to "
        "those files changes this image and never redeploys it. Full upload "
        f"set: {uploaded}"
    )
