"""One-off seeder for the Proteina-Complexa Modal Volumes.

Run ONCE, before the flag flips, so no paying job ever pays the cold pull:

    modal run tools/proteina/seed_volumes.py

Populates two Volumes (created by ``tools/proteina/modal_app.py``):

  proteina-weights  (~9 GB)  the 3 model variants (checkpoint + autoencoder),
                             from the UNGATED HuggingFace mirrors (no NGC auth):
                               nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1
                               nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1
                               nvidia/NV-Proteina-Complexa-AME-160M-v1
                             (NVIDIA Open Model License, commercial OK.) The *.ckpt
                             files land at the Volume root; it mounts at
                             /opt/proteina/ckpts so the configs' `ckpt_path:
                             ./ckpts` (cwd=/opt/proteina) resolves.

  proteina-rewards  (~18 GB) the reward stack artifacts (NO reference DBs — the
                             stack does all-vs-all self-comparison), laid out to
                             match the Dockerfile ENV (verified against upstream
                             download_startup.sh):
                               ckpts/AF2/          AF2 params (CC-BY-4.0)   ~5 GB
                                                   (npz flat, no params/ subdir)
                               ckpts/ESM2/         facebook/esm2_t33_650M   ~2.6 GB
                                                   (HF cache layout, models--*/)
                               ckpts/RF3/          RF3 foundry ckpt (BSD)   ~10 GB

All sources are ungated direct downloads (verified 2026-07-16). The RF3 CODE
(rc-foundry, BSD-3) is installed in the image; only its CHECKPOINT is seeded here.

BUILD-TIME-VERIFY at run: the exact ckpt filenames inside the Ligand-Target and
AME HF repos (assumed complexa_ligand{,_ae}.ckpt / complexa_ame{,_ae}.ckpt by
analogy with the byte-verified Protein-Target repo, which has complexa.ckpt +
complexa_ae.ckpt). ``snapshot_download`` below just mirrors whatever the repos
contain, so a filename guess cannot silently drop a file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile

# ---------------------------------------------------------------------------
# The console, made incapable of killing the run
# ---------------------------------------------------------------------------
#
# BEFORE ``import modal``, AND BEFORE ANY OTHER STATEMENT THAT COULD PRINT.
# Container output reaches this process through modal's log pump: here that is
# wget's progress bars (box-drawing characters), huggingface_hub's tqdm bars
# and whatever filenames the three HF repos happen to contain. On a Windows
# cp1252 console such a write raises ``UnicodeEncodeError: 'charmap' codec
# can't encode character '✓'`` (or U+2588, or U+2501) and kills the LOCAL
# entrypoint, while the container carries on for up to its 3-hour timeout. The
# same class of raise killed ``_hotspot_canary --phase 0`` on 2026-08-04. The
# seed job is not GPU-priced, but a local death here loses the INVENTORY —
# the only readout saying which weights actually landed on the Volumes — and
# an unnoticed half-seeded Volume is paid for later, by a design run.
#
# ``_harden_stream`` mutates the stream's error handler IN PLACE and returns
# the SAME object, which is the point: modal's log pump, rich's renderer and
# the interpreter's own traceback printer each captured ``sys.stdout`` when they
# started, so REPLACING ``sys.stdout`` would leave every one of them writing to
# the strict original. Wrapping is only the fallback for a stream with no usable
# ``reconfigure``. NOT ``PYTHONIOENCODING=utf-8``: that works, and an operator
# forgets it exactly once.
#
# DUPLICATED FROM ``_canary_scoring.py`` RATHER THAN IMPORTED, deliberately.
# That module is reachable (it is a sibling file) but it is canary-lifetime
# code — ``_hotspot_canary.py`` and ``_design_canary.py`` both say "delete
# before flag-on" — while this seeder is a permanent operational script that
# must still run the day the canaries are gone. Importing it as
# ``tools.proteina._canary_scoring`` is worse still: that drags the web-tier
# adapter in through ``tools/proteina/__init__.py``. ~50 stateless stdlib lines
# is the cheaper cost. Canonical copy and its tests: ``_canary_scoring.py`` and
# ``tests/test_proteina_canary.py``.

# ``backslashreplace`` rather than ``replace``: the operator needs to be able to
# tell WHICH character could not be rendered.
CONSOLE_ERRORS = "backslashreplace"


def _safe_text(value, encoding=None, errors=CONSOLE_ERRORS):
    """``value`` rendered so that encoding it to ``encoding`` cannot raise.

    Lossless when the console can carry the text; ``None``/unknown encodings
    degrade to ASCII, which every console can take.
    """
    text = value if isinstance(value, str) else str(value)
    for candidate in (encoding, "ascii"):
        if not candidate:
            continue
        try:
            return text.encode(candidate, errors).decode(candidate, "replace")
        except (LookupError, UnicodeError, TypeError, ValueError):
            continue
    return text.encode("ascii", "backslashreplace").decode("ascii")


class _SafeStream:
    """Delegating proxy whose ``write`` cannot raise ``UnicodeEncodeError``.

    The FALLBACK only. Everything except ``write`` is delegated, so ``fileno``,
    ``encoding``, ``isatty`` and ``buffer`` keep working for anything that hands
    the stream to a subprocess.
    """

    def __init__(self, stream, errors=CONSOLE_ERRORS):
        object.__setattr__(self, "_stream", stream)
        object.__setattr__(self, "_errors", errors)

    def write(self, text):
        stream = object.__getattribute__(self, "_stream")
        try:
            return stream.write(text)
        except UnicodeEncodeError:
            return stream.write(_safe_text(
                text, getattr(stream, "encoding", None),
                object.__getattribute__(self, "_errors")))

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_stream"), name)


def _harden_stream(stream, errors=CONSOLE_ERRORS):
    """The stream to use in place of ``stream``, unable to raise on an
    unencodable character.

    Returns the SAME object whenever it could be reconfigured. ``None`` for
    ``None``, and the original for a stream with no encoding to fail at
    (``io.StringIO``, pytest's capture) — wrapping something that cannot raise
    only adds a layer between the caller and a real file descriptor.
    """
    if stream is None:
        return None
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors=errors)
            return stream
        except (ValueError, OSError, TypeError, AttributeError, LookupError):
            pass
    if not getattr(stream, "encoding", None):
        return stream
    if isinstance(stream, _SafeStream):
        return stream
    return _SafeStream(stream, errors)


sys.stdout = _harden_stream(sys.stdout)
sys.stderr = _harden_stream(sys.stderr)


import modal  # noqa: E402 — imported only after the console cannot kill us

_WEIGHTS_MOUNT = "/vol/weights"
_REWARDS_MOUNT = "/vol/rewards"

_HF_WEIGHT_REPOS = (
    "nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1",
    "nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1",
    "nvidia/NV-Proteina-Complexa-AME-160M-v1",
)
_ESM2_REPO = "facebook/esm2_t33_650M_UR50D"
_AF2_TAR_URL = "https://storage.googleapis.com/alphafold/alphafold_params_2022-12-06.tar"
_RF3_CKPT_URL = "https://files.ipd.uw.edu/pub/rf3/rf3_foundry_01_24_latest_remapped.ckpt"

weights = modal.Volume.from_name("proteina-weights", create_if_missing=True)
rewards = modal.Volume.from_name("proteina-rewards", create_if_missing=True)

# A light CPU image is enough for downloads (no GPU, no reward stack).
seed_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("wget", "ca-certificates")
    .pip_install("huggingface_hub>=0.24", "hf_transfer>=0.1.6", "requests")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)

app = modal.App("ranomics-proteina-seed")


def _download(url: str, dest: str) -> None:
    print(f"[seed] downloading {url} -> {dest}", flush=True)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        print(f"[seed] already present ({os.path.getsize(dest)} bytes) — skip", flush=True)
        return
    # wget for large files (resume + progress); it is in the image.
    subprocess.run(["wget", "-q", "--show-progress", "-O", dest, url], check=True)


@app.function(
    image=seed_image,
    volumes={_WEIGHTS_MOUNT: weights, _REWARDS_MOUNT: rewards},
    timeout=60 * 60 * 3,  # tens of GB over the network
)
def seed() -> dict:
    from huggingface_hub import snapshot_download

    # --- weights: 3 ungated HF mirrors, flattened into the mount ------------
    # Only the *.ckpt weights (distinct basenames per variant: complexa.ckpt,
    # complexa_ligand.ckpt, complexa_ame.ckpt + _ae siblings) so flattening 3
    # repos into one dir cannot clobber a shared aux filename (config.json /
    # pipeline yaml). BUILD-TIME-VERIFY at the P-1 canary whether any variant
    # needs an aux file alongside its ckpt — if so, seed that variant into its
    # own subdir instead.
    for repo in _HF_WEIGHT_REPOS:
        print(f"[seed] HF snapshot {repo}", flush=True)
        snapshot_download(
            repo_id=repo,
            local_dir=_WEIGHTS_MOUNT,
            local_dir_use_symlinks=False,
            allow_patterns=["*.ckpt"],
        )
    weights.commit()

    # --- rewards: AF2 params -------------------------------------------------
    # The npz params extract DIRECTLY into AF2_DIR (flat, NO params/ subdir): the
    # code reads AF2_DIR/params_model_*.npz. This mirrors upstream
    # download_startup.sh (tar -C community_models/ckpts/AF2, then verifies
    # params_model_5_ptm.npz sits directly in that dir).
    af2_dir = f"{_REWARDS_MOUNT}/ckpts/AF2"
    af2_tar = f"{af2_dir}/alphafold_params_2022-12-06.tar"
    if not os.path.isfile(f"{af2_dir}/params_model_5_ptm.npz"):
        os.makedirs(af2_dir, exist_ok=True)
        _download(_AF2_TAR_URL, af2_tar)
        print("[seed] extracting AF2 params (flat into AF2_DIR, no params/ subdir)", flush=True)
        with tarfile.open(af2_tar) as tf:
            tf.extractall(af2_dir)
        os.remove(af2_tar)

    # --- rewards: ESM2 -------------------------------------------------------
    # esm_eval.get_esm_model() calls from_pretrained(model_name, cache_dir=ESM_DIR,
    # local_files_only=True), so ESM_DIR must be an HF *cache* dir (containing the
    # models--facebook--esm2_t33_650M_UR50D/ tree), NOT a flattened local_dir. Use
    # cache_dir= so the on-disk layout is exactly what from_pretrained expects.
    print(f"[seed] HF snapshot {_ESM2_REPO} (HF cache layout under ESM_DIR)", flush=True)
    snapshot_download(
        repo_id=_ESM2_REPO,
        cache_dir=f"{_REWARDS_MOUNT}/ckpts/ESM2",
    )

    # --- rewards: RF3 checkpoint --------------------------------------------
    _download(_RF3_CKPT_URL, f"{_REWARDS_MOUNT}/ckpts/RF3/rf3_foundry_01_24_latest_remapped.ckpt")

    rewards.commit()

    # Inventory for the log / return value.
    def _tree(root: str) -> list[str]:
        out = []
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(dirpath, f)
                out.append(f"{p} ({os.path.getsize(p) // (1024*1024)} MB)")
        return sorted(out)

    inventory = {"weights": _tree(_WEIGHTS_MOUNT), "rewards": _tree(_REWARDS_MOUNT)}
    print("[seed] DONE. Inventory:", flush=True)
    for section, entries in inventory.items():
        print(f"  [{section}]", flush=True)
        for e in entries:
            print(f"    {e}", flush=True)
    return inventory


@app.local_entrypoint()
def main() -> None:
    result = seed.remote()
    print(
        f"Seeded proteina-weights ({len(result['weights'])} files) + "
        f"proteina-rewards ({len(result['rewards'])} files)."
    )
