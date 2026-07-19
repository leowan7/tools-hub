"""One-off seeder for the OpenDDE Modal weights Volume.

Run ONCE, before the flag flips, so no paying job ever pays the cold pull:

    modal run tools/opendde/seed_volumes.py

Populates ``opendde-weights`` (created by ``tools/opendde/modal_app.py``), which
mounts at ``OPENDDE_ROOT_DIR`` (=/opt/opendde_root). The two checkpoints must
land at ``checkpoint/opendde.pt`` and ``checkpoint/opendde_abag.pt`` so the
pipeline's default (general) and ``--load_checkpoint_path`` (abag) resolve.

Source: HuggingFace ``aurekaresearch/OpenDDE`` — Apache-2.0, UNGATED, pinned to
the verified revision below. Bump the revision deliberately; never track main.

BUILD-TIME-VERIFY at the O-1 canary: the exact in-repo path of each .pt file.
This snapshots the whole repo at the pinned revision and then GUARANTEES the
``checkpoint/`` layout by relocating the two .pt files from wherever they landed,
so a layout assumption cannot silently drop a checkpoint.
"""

from __future__ import annotations

import os
import shutil

import modal

_ROOT_MOUNT = "/vol/opendde_root"
_HF_REPO = "aurekaresearch/OpenDDE"
# Verified HF revision (from the official resolve URLs). Ungated.
_HF_REVISION = "eddd563ce96571f784012edd8f045181c8f8627d"
_CHECKPOINTS = ("opendde.pt", "opendde_abag.pt")

weights = modal.Volume.from_name("opendde-weights", create_if_missing=True)

seed_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ca-certificates")
    .pip_install("huggingface_hub>=0.24", "hf_transfer>=0.1.6")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)

app = modal.App("ranomics-opendde-seed")


def _find(root: str, filename: str) -> str | None:
    for dirpath, _dirs, files in os.walk(root):
        if filename in files:
            return os.path.join(dirpath, filename)
    return None


@app.function(image=seed_image, volumes={_ROOT_MOUNT: weights}, timeout=60 * 60 * 2)
def seed() -> dict:
    from huggingface_hub import snapshot_download

    print(f"[seed] HF snapshot {_HF_REPO}@{_HF_REVISION[:12]}", flush=True)
    snapshot_download(
        repo_id=_HF_REPO,
        revision=_HF_REVISION,
        local_dir=_ROOT_MOUNT,
        local_dir_use_symlinks=False,
    )

    # Guarantee the checkpoint/ layout regardless of where the .pt files landed.
    ckpt_dir = os.path.join(_ROOT_MOUNT, "checkpoint")
    os.makedirs(ckpt_dir, exist_ok=True)
    missing: list[str] = []
    for fn in _CHECKPOINTS:
        dest = os.path.join(ckpt_dir, fn)
        if os.path.isfile(dest):
            continue
        found = _find(_ROOT_MOUNT, fn)
        if found and os.path.abspath(found) != os.path.abspath(dest):
            print(f"[seed] relocating {found} -> {dest}", flush=True)
            shutil.move(found, dest)
        elif not found:
            missing.append(fn)

    weights.commit()

    def _tree(root: str) -> list[str]:
        out = []
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(dirpath, f)
                out.append(f"{p} ({os.path.getsize(p) // (1024 * 1024)} MB)")
        return sorted(out)

    inventory = _tree(_ROOT_MOUNT)
    print("[seed] DONE. Inventory:", flush=True)
    for e in inventory:
        print(f"    {e}", flush=True)
    if missing:
        print(f"[seed] WARNING: checkpoints not found in repo: {missing}", flush=True)
    return {"files": inventory, "missing": missing}


@app.local_entrypoint()
def main() -> None:
    result = seed.remote()
    print(f"Seeded opendde-weights ({len(result['files'])} files).")
    if result["missing"]:
        print(f"MISSING checkpoints (re-verify HF paths): {result['missing']}")
