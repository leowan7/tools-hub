"""The two VRAM settings esmfold2-design needs must stay applied, and in order.

Both were documented and never wired, which is why the ``scfv`` preset burned a
full 80 GB H100 and returned zero designs at every ``batch_size`` above 1 --
including the default of 3, so this was the default web path, not an edge case.

Neither setting can be covered by a normal test: one is read inside the vendored
upstream ``binder_design.py`` (present only in the built image) and the other is
consumed by the CUDA allocator in a GPU container. So these are static
assertions over the source, which is what an offline suite can actually hold.

ORDER IS THE WHOLE POINT of the first test. ``ESMFold2Design.load()`` reads
``REUSE_ESMC`` as a module global at call time, so ``bd.REUSE_ESMC = True``
placed *after* ``designer.load(...)`` still assigns, still greps, still reviews
clean -- and does nothing at all. Asserting the assignment merely EXISTS would
pass on exactly that bug, so the assertion is on line order.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_TOOL_DIR = pathlib.Path(__file__).resolve().parent.parent / "tools" / "esmfold2_design"
_RUN_PIPELINE = _TOOL_DIR / "run_pipeline.py"
_MODAL_APP = _TOOL_DIR / "modal_app.py"


def _tree(path: pathlib.Path) -> ast.Module:
    assert path.is_file(), f"{path} is missing -- did the tool move or get renamed?"
    return ast.parse(path.read_text(encoding="utf-8"))


def _reuse_esmc_assign_lines(tree: ast.Module) -> list[int]:
    """Lines assigning a literal True to any ``<mod>.REUSE_ESMC``."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "REUSE_ESMC":
                if isinstance(node.value, ast.Constant) and node.value.value is True:
                    lines.append(node.lineno)
    return lines


def _designer_load_lines(tree: ast.Module) -> list[int]:
    """Lines calling ``<something>.load(...)`` on a name bound to the designer."""
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "load"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "designer"
        ):
            lines.append(node.lineno)
    return lines


def test_designer_load_is_discovered():
    """A finder that matches nothing would make the ordering test vacuous."""
    load_lines = _designer_load_lines(_tree(_RUN_PIPELINE))
    assert load_lines, (
        "no `designer.load(...)` call found in run_pipeline.py. The designer was "
        "renamed or restructured -- re-point this test before trusting it."
    )


def test_reuse_esmc_is_set_true_before_designer_load():
    tree = _tree(_RUN_PIPELINE)
    assign_lines = _reuse_esmc_assign_lines(tree)
    load_lines = _designer_load_lines(tree)

    assert assign_lines, (
        "run_pipeline.py never sets `bd.REUSE_ESMC = True`. Upstream ships it "
        "False, which costs ~51 GB VRAM instead of ~27 GB and cannot hold the "
        "1-6 batch_size tools/esmfold2_design/__init__.py advertises."
    )
    assert min(assign_lines) < min(load_lines), (
        f"`REUSE_ESMC = True` is on line {min(assign_lines)} but "
        f"`designer.load(...)` is on line {min(load_lines)}. load() reads the "
        "flag as a module global at call time, so assigning it afterwards is a "
        "no-op and the run OOMs exactly as if the line were absent."
    )


def _image_env_dicts(tree: ast.Module) -> list[ast.Dict]:
    """Every dict passed to a ``.env({...})`` call in the module."""
    dicts = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "env"
            and node.args
            and isinstance(node.args[0], ast.Dict)
        ):
            dicts.append(node.args[0])
    return dicts


def test_image_env_is_discovered():
    assert _image_env_dicts(_tree(_MODAL_APP)), (
        "no `.env({...})` call found in modal_app.py -- the image builder was "
        "restructured and this test can no longer see the container env."
    )


def test_expandable_segments_is_set_on_the_image():
    """The allocator setting both OOM messages recommended, applied.

    On the image rather than in ``_build_run_env`` on purpose: it is a container
    constant, and ``_merged_environment`` seeds from ``os.environ``, so the
    subprocess inherits it either way. Accept it in either place -- what must
    never happen again is it being in neither.
    """
    tree = _tree(_MODAL_APP)
    found = {}
    for env_dict in _image_env_dicts(tree):
        for key, value in zip(env_dict.keys, env_dict.values):
            if isinstance(key, ast.Constant) and key.value == "PYTORCH_CUDA_ALLOC_CONF":
                if isinstance(value, ast.Constant):
                    found[key.value] = value.value

    if not found:
        source = _MODAL_APP.read_text(encoding="utf-8")
        assert "PYTORCH_CUDA_ALLOC_CONF" in source, (
            "PYTORCH_CUDA_ALLOC_CONF is set nowhere in modal_app.py. At "
            "batch_size=2 this tool died needing 20-38 MiB while ~1.4 GB sat "
            "reserved-but-unallocated; expandable_segments:True reclaims it."
        )
        pytest.fail(
            "PYTORCH_CUDA_ALLOC_CONF appears in modal_app.py but not as a "
            "literal in an image .env({...}) dict -- confirm it still reaches "
            "the container, then update this test."
        )

    assert "expandable_segments:True" in found["PYTORCH_CUDA_ALLOC_CONF"], (
        f"PYTORCH_CUDA_ALLOC_CONF is {found['PYTORCH_CUDA_ALLOC_CONF']!r}, which "
        "does not enable expandable_segments -- the one thing both OOM messages "
        "explicitly recommended."
    )
