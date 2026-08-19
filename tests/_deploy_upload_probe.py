"""Ask Modal itself which local files a ``modal_app.py`` would upload.

Not a test module (leading underscore: pytest does not collect it).
``tests/test_deploy_paths_exclusions.py`` runs it as a subprocess, one per app,
with cwd = repo root — the same shape as the deploy workflow's one-job-per-app
``modal deploy tools/<app>/modal_app.py``.

Why a subprocess: ``import_file_or_module`` mutates ``sys.path`` and imports the
app module for real, and every app registers a global ``modal.App``. Running the
nine in one process would let them contaminate each other and would not mirror
CI. Why the *behaviour* and not another AST scan: the deploy-trigger exclusion
rests on Modal resolving these files in SCRIPT mode, which no amount of reading
repo source can confirm. If a future Modal client flips that, this is the only
check in the repo that can notice.

Run by hand for debugging::

    python tests/_deploy_upload_probe.py mpnn

Everything it reports is read off Modal's own objects:

* ``FunctionInfo._type`` / the defining module's ``__package__`` — script vs
  package mode.
* ``FunctionInfo.get_entrypoint_mount()`` and ``FunctionSpec.mounts`` — the
  implicit mount set (``modal/_functions.py``: ``all_mounts``).
* the whole image chain's ``_mount_layers`` **and** every ``_from_args``
  ``context_mount_function()`` closure — ``add_local_*``, ``from_dockerfile``
  and ``dockerfile_commands`` all land here, and reading only the entrypoint
  mount would miss every one of them.

File lists come from ``_MountEntry.get_files_to_upload()``, i.e. the actual
upload list, not a pattern guess.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import modal
from modal.cli.import_refs import ImportRef, import_file_or_module

# Marks the payload line so incidental stdout from Modal cannot be mistaken for it.
RESULT_PREFIX = "DEPLOY_UPLOAD_PROBE_JSON:"


def _rel(local_path, repo: Path) -> str:
    try:
        return str(Path(local_path).resolve().relative_to(repo)).replace("\\", "/")
    except ValueError:
        return "<outside-repo>:" + str(local_path).replace("\\", "/")


def _mount_files(mount, repo: Path) -> list[str]:
    try:
        entries = mount.entries
    except Exception:
        return []  # a non-local mount (e.g. the client mount) has no local files
    return [_rel(local, repo) for e in entries for local, _remote in e.get_files_to_upload()]


def _closure(obj):
    """Free variables of an ``_Object``'s loader closure, by name."""
    load = getattr(obj, "_load", None)
    if load is None or getattr(load, "__code__", None) is None:
        return {}
    return {
        name: cell.cell_contents
        for name, cell in zip(load.__code__.co_freevars, load.__closure__ or ())
    }


def _image_chain(image, seen: set):
    """Every ``_Image`` reachable through the ``_from_args`` base-image closures."""
    if image is None or id(image) in seen:
        return
    seen.add(id(image))
    yield image
    cells = _closure(image)
    for base in (cells.get("base_images") or {}).values():
        yield from _image_chain(base, seen)
    yield from _image_chain(cells.get("base_image"), seen)


def _image_files(image, repo: Path) -> list[str]:
    files, layers = [], 0
    for layer in _image_chain(image, set()):
        layers += 1
        cells = _closure(layer)
        for mount in getattr(layer, "_mount_layers", ()) or ():
            files += _mount_files(mount, repo)
        # from_dockerfile / dockerfile_commands / add_local_*(copy=True)
        cmf = cells.get("context_mount_function")
        if cmf is not None:
            mount = cmf()
            if mount is not None:
                files += _mount_files(mount, repo)
        # add_local_*(copy=False) keeps its mount as a plain freevar
        mount = cells.get("mount")
        if mount is not None:
            files += _mount_files(mount, repo)
    return sorted(set(files)), layers


def probe(slug: str) -> dict:
    repo = Path.cwd().resolve()
    module = import_file_or_module(
        ImportRef(f"tools/{slug}/modal_app.py", use_module_mode=False)
    )
    result = {
        "slug": slug,
        "module_package": getattr(module, "__package__", "<missing>"),
        "repo_root_on_sys_path": "" in sys.path or str(repo) in sys.path,
        "functions": [],
    }
    for app in [v for v in vars(module).values() if isinstance(v, modal.App)]:
        for name, fn in app.registered_functions.items():
            info = fn.info
            entrypoint = [
                f
                for mount in info.get_entrypoint_mount().values()
                for f in _mount_files(mount, repo)
            ]
            spec_mounts = [
                f for mount in (getattr(fn.spec, "mounts", ()) or ()) for f in _mount_files(mount, repo)
            ]
            image_files, layers = _image_files(fn.spec.image, repo)
            result["functions"].append(
                {
                    "app": app.name,
                    "name": name,
                    "info_type": info._type.name,
                    "entrypoint_mount": sorted(set(entrypoint)),
                    "spec_mounts": sorted(set(spec_mounts)),
                    "image_files": image_files,
                    "image_layers_walked": layers,
                }
            )
    return result


if __name__ == "__main__":
    print(RESULT_PREFIX + json.dumps(probe(sys.argv[1])))
