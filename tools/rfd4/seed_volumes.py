"""One-off seeder for the RFD4-Proteina Modal weights Volume.

Run ONCE, before the flag flips, so no paying job ever pays the cold pull::

    modal run tools/rfd4/seed_volumes.py

Populates ``rfd4-proteina-weights``. The Volume mounts at ``DATA_PATH`` (=/data,
set in ``tools/rfd4/Dockerfile.modal``) so that upstream's
``configs/inference.yaml`` resolves both checkpoints without an override:

  ``ckpt_path: ${oc.env:DATA_PATH}/flow_checkpoints``                    (:39)
  ``autoencoder_ckpt_path: ${oc.env:DATA_PATH}/ae_checkpoints/...ckpt``   (:46)

Source: HuggingFace ``nvidia/RFD4-Proteina`` at the revision upstream itself
pins (``configs/inference.yaml:30``), listed below. Two files out of a 82.57 GB
revision, named one by one — ``snapshot_download`` is NOT used here, and must not
be: ~71.4 GB of that revision is the AlphaFold2 and OpenFold3 oracle runtimes,
excluded on licence grounds. An allow-list that names its files cannot acquire a
new one when upstream adds one.

The two names are NOT symmetrical, and the asymmetry is load-bearing:

  flow  stays under its HuggingFace name, ``rfd4_proteina_flow-ema.ckpt``.
        ``rfproteina/utils/weights.py:20`` dispatches on the ``-ema.ckpt``
        SUFFIX; a file renamed away from it loads as a plain checkpoint and
        raises "has no SimpleEMA state, but inference requires EMA weights".
        An earlier seed renamed it to the dated name and the first run died
        exactly there (scope doc §5.7).
  AE    is renamed to the dated name ``rfd4-proteina-ae-release-2026-10-19.ckpt``
        so it lands on the literal path ``configs/inference.yaml:46`` reads.
        ``proteina.py:464`` loads it with a plain ``AutoEncoder.load_from_checkpoint``,
        so no suffix rule applies to it. This is the pair that §5.7's run proved.

The flow checkpoint and the autoencoder are a MATCHED PAIR (upstream's own
``release-checkpoints.json`` says so in its ``policy`` field); mixing releases
produces nonsense rather than an error. Both digests below come from that
manifest at this revision, and this script fails rather than committing a file
whose bytes do not match.

This downloads UNAUTHENTICATED. It worked that way for the §5.6 seed, but a
production re-seed should carry an ``HF_TOKEN`` secret so a rate limit or a
gating change cannot turn into a half-seeded Volume; this script does not read
one yet.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys

# The HF downloader writes tqdm progress bars (U+2588 and friends) through
# modal's log pump into THIS process's stdout. On a cp1252 Windows console that
# write raises UnicodeEncodeError and kills the local entrypoint while the
# container runs on, which loses the inventory below — the only readout of what
# actually landed. `backslashreplace` rather than `replace` so the operator can
# see WHICH character could not be rendered. Before `import modal`, and before
# anything that could print.
#
# ponytail: tools/proteina/seed_volumes.py carries a ~50-line hardening with a
# proxy-object fallback for streams that have no usable `reconfigure`. Every
# stream this script runs against is a CPython TextIOWrapper, which has had
# `reconfigure` since 3.7, so the two lines below are the whole of it. Lift that
# version in if this ever runs behind something that replaces sys.stdout.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            pass

import modal  # noqa: E402 — imported only after the console cannot kill us

_DATA_MOUNT = "/vol/data"

_HF_REPO = "nvidia/RFD4-Proteina"
# Upstream's own pin, read off configs/inference.yaml:30 at RFD4_SHA
# aaccdd2ad0720c180468df5f9cf037a713d403e5. Bump deliberately, never track main,
# and bump both checkpoints together — see the matched-pair note above.
_HF_REVISION = "a06e2509c7033b010fc43614e9fd840303588502"

# (path in the HF repo, path on the Volume, sha256, size in bytes). Both HF
# paths, both sizes and both digests were read back off the HuggingFace API at
# _HF_REVISION on 2026-11-02 — /api/models/nvidia/RFD4-Proteina/tree/<rev>/ for
# the paths and sizes, and each file's own `.sha256` sidecar for the digest.
#
# NOT seeded: flow_checkpoints/rfd4_proteina_flow_pretrain-ema.ckpt (the pretrain
# variant, a third 1.74 GB file), and the alphafold2/ and openfold3/ oracle
# runtimes (licence).
_FILES = (
    (
        "flow_checkpoints/rfd4_proteina_flow-ema.ckpt",
        "flow_checkpoints/rfd4_proteina_flow-ema.ckpt",
        "c1c8b7068e87c41a334ec5b77e7d7cdda30bd31ed4504909d93bd72f7742afe4",
        1751488703,
    ),
    (
        "ae_checkpoints/rfd4_proteina_ae-ema.ckpt",
        "ae_checkpoints/rfd4-proteina-ae-release-2026-10-19.ckpt",
        "24536372f3fc9a5083b6b813baa80630d2287a5d61fbf2f126c9735802e1d21a",
        1029340246,
    ),
)

weights = modal.Volume.from_name("rfd4-proteina-weights", create_if_missing=True)

# A light CPU image is enough for downloads: no GPU, no rfd4 package.
seed_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ca-certificates")
    .pip_install("huggingface_hub>=0.24", "hf_transfer>=0.1.6")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)

app = modal.App("ranomics-rfd4-seed")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@app.function(image=seed_image, volumes={_DATA_MOUNT: weights}, timeout=60 * 60 * 2)
def seed() -> dict:
    from huggingface_hub import hf_hub_download

    verified: list[str] = []
    for repo_path, vol_path, want_sha, want_size in _FILES:
        dest = os.path.join(_DATA_MOUNT, vol_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        # Idempotent, and on the DIGEST, not just the size. Size alone would skip
        # a file left over from an earlier release whose checkpoint happens to
        # serialise to the same length — and _HF_REVISION plus both digests get
        # bumped together, so the skip is exactly where a half-bumped Volume
        # would hide. Re-reading 2.59 GiB costs tens of seconds on a script that
        # runs once per Volume; a wrong pair costs a paid run that returns
        # nonsense (see the matched-pair note above).
        if (
            os.path.isfile(dest)
            and os.path.getsize(dest) == want_size
            and _sha256(dest) == want_sha
        ):
            print(f"[seed] {vol_path} already present, sha256 ok — skip", flush=True)
            verified.append(vol_path)
            continue

        print(f"[seed] {_HF_REPO}@{_HF_REVISION[:12]} :: {repo_path}", flush=True)
        src = hf_hub_download(
            repo_id=_HF_REPO,
            revision=_HF_REVISION,
            filename=repo_path,
            local_dir=os.path.join("/tmp", "rfd4-hf"),
        )

        size = os.path.getsize(src)
        if size != want_size:
            raise RuntimeError(f"{repo_path}: got {size} bytes, upstream manifest says {want_size}")
        got_sha = _sha256(src)
        if got_sha != want_sha:
            raise RuntimeError(f"{repo_path}: sha256 {got_sha}, upstream manifest says {want_sha}")

        # Verify first, then move onto the Volume, so a mismatch cannot leave a
        # bad checkpoint where a run would find it. shutil.move, not os.replace:
        # /tmp and the Volume mount are different filesystems, so a rename
        # across them raises EXDEV.
        shutil.move(src, dest)
        print(f"[seed] -> {vol_path} ({size} bytes, sha256 ok)", flush=True)
        verified.append(vol_path)

    weights.commit()

    inventory = []
    for dirpath, _dirs, files in os.walk(_DATA_MOUNT):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            inventory.append(f"{p} ({os.path.getsize(p)} bytes)")
    print("[seed] DONE. Inventory:", flush=True)
    for e in sorted(inventory):
        print(f"    {e}", flush=True)
    return {"verified": verified, "inventory": sorted(inventory)}


@app.local_entrypoint()
def main() -> None:
    result = seed.remote()
    print(
        f"Seeded rfd4-proteina-weights: {len(result['verified'])} checkpoints "
        f"verified, {len(result['inventory'])} files on the Volume."
    )
