"""Modal app for Proteina-Complexa binder design (``ranomics-proteina-prod``).

Deploy:
    modal deploy tools/proteina/modal_app.py

Runtime: tools-hub's ``gpu.modal_client.ModalClient.submit`` resolves this
function via ``modal.Function.from_name("ranomics-proteina-prod", "run_tool")``
and calls ``.spawn(payload)``. The function writes the payload to env vars and
runs ``run_pipeline.py`` as a subprocess (self-contained atomic pattern,
identical to boltz2 / iggm), which resolves the target (a curated benchmark
task baked into the config, or a caller-uploaded PDB/SDF from the presigned
GET URL), runs one seeded ``proteinfoundation.generate`` search shard plus the
reward filter, uploads per-design PDBs via presigned PUT URLs, emits heartbeats,
and writes ``/tmp/smoke_results.json``. The wrapper returns that file inline via
``smoke_result``.

One CONTAINER == one search SHARD. The campaign engine
(``shared/compute_campaigns.py``) fans ``num_designs`` out across many of these
containers, each with a distinct ``++seed`` (generate.py: ``seed = cfg.seed +
job_id`` makes them independent), and the hub does the global cross-shard top-K
+ diversity clustering. This app never runs multi-GPU / multi-shard itself.

GPU: A100-80GB (AF2 + RF3 co-resident with the flow-matching generator is
heavy; 40GB risks OOM). ``_MAX_SESSION_S = 7200`` (2 h) physically caps a
runaway shard: with the A100-80GB rate + markup that ceiling is ~$12.58, which
is why the per-shard wallet hold ($15) can never under-hold.

Volumes (seeded once by ``tools/proteina/seed_volumes.py`` before the flag
flips; a paying job never pays the cold pull):

- ``proteina-weights``  -> ``/opt/proteina/ckpts``   : the 3 model variants'
  checkpoints (complexa{,_ligand,_ame}.ckpt + _ae siblings). The configs resolve
  them via the RELATIVE key ``ckpt_path: ./ckpts``, so the mount point and the
  ``complexa design`` cwd (=/opt/proteina, set in run_pipeline) are load-bearing.
  (NVIDIA Open Model License, HF mirror ``nvidia/NV-Proteina-Complexa-*``).
- ``proteina-rewards``  -> ``/opt/proteina/rewards``  : the reward stack
  weight artifacts (AF2 params, the RF3 checkpoint, ESM2). No sequence/structure
  reference DBs are needed — Foldseek / MMseqs2 run all-vs-all self-comparison
  over the generated designs, and their binaries live in the image, not here.
  The Dockerfile ``ENV RF3_CKPT_PATH`` / ``AF2_DIR`` point into this mount.

RF3 kill-switch: ``PROTEINA_RF3`` (default ``on`` from the Dockerfile ENV) is
inherited by the subprocess through ``os.environ``. Flip it off (redeploy with
``--build-arg`` or a Modal env override) only to disable the RF3 reward channel;
``run_pipeline`` then hard-blocks the ligand / motif variants (no AF2 ligand
fallback) before any GPU spend. Per Leo, RF3 stays on; this is the degraded-path
guard, not a normal branch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any

import modal

_TOOL = "proteina"
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/proteina/run_pipeline.py"
_GPU = "A100-80GB"
# 2 h ceiling — the physical cap on a single search shard. The subprocess gets
# this minus a small margin so it is killed (and the shard billed at its held
# maximum, never above) before Modal reaps the whole container.
_MAX_SESSION_S = 7200

# Volume mount points. The weights mount must be /opt/proteina/ckpts because the
# configs locate the generator checkpoint via the RELATIVE key `ckpt_path: ./ckpts`
# (resolved against the `complexa design` cwd=/opt/proteina). The reward stack is
# located by absolute env vars (RF3_CKPT_PATH / AF2_DIR / ESM_DIR) under the
# rewards mount. Kept in lockstep with the Dockerfile ENV.
_WEIGHTS_MOUNT = "/opt/proteina/ckpts"
_REWARDS_MOUNT = "/opt/proteina/rewards"

# Raw shard-output capture. run_pipeline.py tars its COMPLETE ./inference tree to
# _RAW_ARCHIVE_PATH on every exit path; this wrapper parks it on a Volume under
# the job id. The job id is a name both sides already know, so the tar itself
# never travels through the return dict (and therefore never through the
# tool_jobs.result JSONB column, where an inline blob has already broken an
# UPDATE and stranded jobs in "running").
_RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"
_RAW_VOLUME = f"ranomics-{_TOOL}-raw"
_RAW_MOUNT = "/raw"


def _build_run_env(payload: dict) -> dict[str, str]:
    """Translate a Modal payload into env vars for run_pipeline.py (identical
    contract to boltz2 / iggm; proteina also needs upload_urls_endpoint for
    per-design streaming and input_presigned_url for a custom target)."""
    return {
        "JOB_PAYLOAD": json.dumps(
            {
                "job_spec": payload.get("job_spec", {}),
                "input_presigned_url": payload.get("input_presigned_url", ""),
                "upload_urls_endpoint": payload.get("upload_urls_endpoint", ""),
                "job_token": payload.get("job_token", ""),
                "tier": payload.get("tier", ""),
            }
        ),
        "WEBHOOK_URL": str(payload.get("webhook_url", "")),
        "JOB_ID": str(payload.get("job_id", "")),
        "JOB_TOKEN": str(payload.get("job_token", "")),
        "JOB_TIER": str(payload.get("job_tier", payload.get("tier", "protein_binder"))),
    }


def _merged_environment(payload: dict) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(_build_run_env(payload))
    return merged


# Weight + reward Volumes cache across cold starts. Seeded once before flag-on
# (see tools/proteina/seed_volumes.py); the first prod run then hits the cache
# instead of paying a tens-of-GB cold pull.
weights = modal.Volume.from_name("proteina-weights", create_if_missing=True)
rewards = modal.Volume.from_name("proteina-rewards", create_if_missing=True)
# Raw run artifacts get their OWN Volume, never the weights / rewards caches:
# those exist to make cold starts cheap and have no eviction path, so parking
# GB-scale per-run output in them bloats the very thing they are for and leaves
# no way to reap raw without touching the weights a paying job depends on.
raw = modal.Volume.from_name(_RAW_VOLUME, create_if_missing=True)

image = (
    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
)

app = modal.App("ranomics-proteina-prod")


def _raw_archive_name(job_id: str) -> str:
    """Filesystem-safe stem for the parked tar. The job id is deterministic and
    already known to whoever wants the tree, so nothing extra has to be carried
    back through the result to locate it."""
    cleaned = "".join(c for c in str(job_id) if c.isalnum() or c in "-_.")
    return cleaned or f"job_{int(time.time())}"


def _park_raw_archive(job_id: str) -> str | None:
    """Move run_pipeline's raw output tar onto the raw Volume. Never raises.

    Capture failing must never fail the run — the runs worth capturing are
    disproportionately the ones that already went wrong — so every problem here
    is printed and swallowed. Returns the Volume path, or None if there was
    nothing to park (e.g. the pipeline was killed before it could tar)."""
    try:
        if not os.path.isfile(_RAW_ARCHIVE_PATH):
            print(f"[run_tool] no raw archive at {_RAW_ARCHIVE_PATH} (nothing captured)", flush=True)
            return None
        size = os.path.getsize(_RAW_ARCHIVE_PATH)
        os.makedirs(_RAW_MOUNT, exist_ok=True)
        dest = f"{_RAW_MOUNT}/{_raw_archive_name(job_id)}.tgz"
        shutil.move(_RAW_ARCHIVE_PATH, dest)
        try:
            raw.commit()
        except Exception as exc:  # noqa: BLE001 — a commit race must not lose the run
            print(f"[run_tool] raw.commit() raised: {exc}", flush=True)
        print(
            f"[run_tool] raw archive parked: {dest} ({size / 1e6:.1f} MB, volume {_RAW_VOLUME})",
            flush=True,
        )
        return dest
    except Exception as exc:  # noqa: BLE001 — capture is best-effort by design
        print(
            f"[run_tool] raw archive capture failed (non-fatal): {type(exc).__name__}: {exc}",
            flush=True,
        )
        return None


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    volumes={_WEIGHTS_MOUNT: weights, _REWARDS_MOUNT: rewards, _RAW_MOUNT: raw},
)
def run_tool(payload: Any) -> dict:
    """Run one Proteina-Complexa search shard. stdout/stderr live-stream to
    Modal's function logs (``modal app logs ranomics-proteina-prod``)."""
    env = _merged_environment(payload)
    cmd = ["python3", "-u", _RUN_PIPELINE_REMOTE]
    print(f"[run_tool] spawning: {' '.join(cmd)}", flush=True)
    print(
        f"[run_tool] JOB_ID={env.get('JOB_ID')} TIER={env.get('JOB_TIER')} "
        f"RF3={env.get('PROTEINA_RF3', 'on')} WEBHOOK={env.get('WEBHOOK_URL')}",
        flush=True,
    )
    # Warm containers are reused: a leftover raw archive from a prior job would be parked
    # under THIS job's id. Clear it so we only ever park a tar this run actually wrote.
    try:
        os.remove(_RAW_ARCHIVE_PATH)
    except OSError:
        pass
    try:
        result = subprocess.run(
            cmd, env=env, stdout=sys.stdout, stderr=sys.stderr,
            timeout=max(60, _MAX_SESSION_S - 120),
        )
        print(f"[run_tool] subprocess exited: {result.returncode}", flush=True)
    finally:
        # In a finally so the _MAX_SESSION_S timeout kill — where subprocess.run
        # raises TimeoutExpired and this function never reaches its return — still
        # ships whatever the pipeline managed to tar. The exception is not
        # swallowed: a timeout must still fail the job exactly as it does today.
        raw_tgz_path = _park_raw_archive(str(payload.get("job_id", "")))

    smoke_result: dict | None = None
    try:
        with open("/tmp/smoke_results.json") as fh:
            smoke_result = json.load(fh)
        print(
            f"[run_tool] loaded smoke_results.json: status={smoke_result.get('status')} "
            f"completed={smoke_result.get('designs_completed')}/"
            f"{smoke_result.get('designs_total')}",
            flush=True,
        )
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[run_tool] failed to read smoke_results.json: {exc}", flush=True)

    # Commit any lazily-materialized weight/reward downloads so the next cold
    # start sees them (no-op once the seed run has populated the Volumes).
    for vol, name in ((weights, "weights"), (rewards, "rewards")):
        try:
            vol.commit()
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[run_tool] {name}.commit() raised: {exc}", flush=True)

    out = {
        "exit_code": result.returncode,
        "stdout_tail": "",
        "stderr_tail": "",
        "provider_job_id": payload.get("job_id", ""),
        "smoke_result": smoke_result,
    }
    # Top-level keys are ignored by the hub's _interpret_pipeline_return, so this
    # is a pure addition — no client change, and only a pointer travels, never the
    # tar. Absent == nothing was parked.
    if raw_tgz_path:
        out["raw_tgz_volume"] = _RAW_VOLUME
        out["raw_tgz_volume_path"] = raw_tgz_path
    return out
