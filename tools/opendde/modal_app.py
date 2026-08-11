"""Modal app for OpenDDE co-folding (``ranomics-opendde-prod``).

Deploy:
    modal deploy tools/opendde/modal_app.py

Runtime: tools-hub's ``gpu.modal_client.ModalClient.submit`` resolves this
function via ``modal.Function.from_name("ranomics-opendde-prod", "run_tool")``
and calls ``.spawn(payload)``. The function writes the payload to env vars and
runs ``run_pipeline.py`` as a subprocess (self-contained atomic pattern,
identical to boltz2 / iggm / proteina), which writes the OpenDDE spec to
input.json, runs ``opendde pred``, uploads each predicted structure via presigned
PUT URLs, emits heartbeats, and writes ``/tmp/smoke_results.json``. The wrapper
returns that file inline via ``smoke_result``.

GPU: H100. ``_MAX_SESSION_S = 3600`` (1 h) physically caps a single job: this is
the real bound on spend, so the wallet's fixed container-budget hold
(``scaling_param=None`` in wallet_estimates.py) can never under-hold.

Volumes:
- ``opendde-weights`` -> ``OPENDDE_ROOT_DIR`` : the two checkpoints
  (``checkpoint/opendde.pt`` + ``checkpoint/opendde_abag.pt``) and any runtime
  data, seeded once by ``tools/opendde/seed_volumes.py`` before the flag flips
  (a paying job never pays the cold pull). Apache-2.0, ungated HF weights.
- ``ranomics-opendde-raw`` -> ``/raw`` : the complete per-run output tree, keyed
  by job id (never the weights cache — raw output would bloat it with no eviction
  path).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any

import modal

_TOOL = "opendde"
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/run_pipeline.py"
_GPU = "H100"
# 1 h ceiling — the physical cap on a single co-folding job. The subprocess gets
# this minus a margin so it is killed (and billed at its held maximum, never
# above) before Modal reaps the whole container.
_MAX_SESSION_S = 3600
_PYTHON = "python3"

# Where run_pipeline.py tars its complete work tree, and where this wrapper parks
# it. Its OWN Volume, never the weights cache.
_RAW_ARCHIVE = "/tmp/raw_archive.tgz"
_RAW_VOLUME = f"ranomics-{_TOOL}-raw"
_RAW_MOUNT = "/raw"

# The weights Volume mounts at the OpenDDE runtime data root so
# ``checkpoint/opendde.pt`` resolves. Kept in lockstep with the Dockerfile ENV.
_OPENDDE_ROOT = "/opt/opendde_root"


def _build_run_env(payload: dict) -> dict[str, str]:
    """Translate a Modal payload into env vars for run_pipeline.py.

    Same contract as boltz2 / proteina. OpenDDE inputs are inline (no PDB), so
    there is no ``input_presigned_url`` to thread; the spec rides in ``job_spec``.
    ``upload_urls_endpoint`` is required for per-prediction streaming.
    """
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
        "JOB_TIER": str(payload.get("job_tier", payload.get("tier", "general"))),
    }


def _merged_environment(payload: dict) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(_build_run_env(payload))
    return merged


weights = modal.Volume.from_name("opendde-weights", create_if_missing=True)
raw = modal.Volume.from_name(_RAW_VOLUME, create_if_missing=True)

image = (
    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
)

app = modal.App("ranomics-opendde-prod")


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    volumes={_OPENDDE_ROOT: weights, _RAW_MOUNT: raw},
)
def run_tool(payload: Any) -> dict:
    """Run one OpenDDE co-folding job. stdout/stderr live-stream to Modal's
    function logs (``modal app logs ranomics-opendde-prod``)."""
    env = _merged_environment(payload)
    cmd = [_PYTHON, "-u", _RUN_PIPELINE_REMOTE]

    print(f"[run_tool] spawning: {' '.join(cmd)}", flush=True)
    print(
        f"[run_tool] JOB_ID={env.get('JOB_ID')} TIER={env.get('JOB_TIER')} "
        f"WEBHOOK={env.get('WEBHOOK_URL')}",
        flush=True,
    )

    # Clear any stale smoke_results.json from a prior invocation on a warm Modal
    # container. Without this, if this run's run_pipeline.py dies before writing its
    # own file (early import error, OOM kill, SIGKILL, uncaught exception), the read
    # below picks up the PREVIOUS job's result and
    # ``gpu.modal_client._interpret_pipeline_return()`` marks this job succeeded with
    # another run's structures — that branch keys off smoke["status"] alone and never
    # consults exit_code. Codex P1 (colabfold).
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[run_tool] could not remove stale smoke_results.json: {exc}", flush=True)

    # Warm containers are reused: a leftover raw archive from a prior job would be
    # parked under THIS job's id. Clear it so we only ever park a tar this run wrote.
    try:
        os.remove(_RAW_ARCHIVE)
    except OSError:
        pass

    result = subprocess.run(
        cmd,
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
        timeout=max(60, _MAX_SESSION_S - 30),
    )

    print(f"[run_tool] subprocess exited: {result.returncode}", flush=True)

    smoke_result: dict | None = None
    try:
        with open("/tmp/smoke_results.json") as fh:
            smoke_result = json.load(fh)
        print(
            f"[run_tool] loaded smoke_results.json: "
            f"status={smoke_result.get('status')} "
            f"completed={smoke_result.get('designs_completed')}/"
            f"{smoke_result.get('designs_total')}",
            flush=True,
        )
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[run_tool] failed to read smoke_results.json: {exc}", flush=True)

    # Park the COMPLETE raw work tree, unconditionally. The tar goes on a Volume
    # rather than inline in the return dict because a big base64 blob wedges the
    # job in "running" via the tool_jobs.result JSONB column. Best-effort.
    raw_info: dict[str, str] = {}
    try:
        if os.path.isfile(_RAW_ARCHIVE):
            os.makedirs(_RAW_MOUNT, exist_ok=True)
            job_id = str(payload.get("job_id") or "unknown")
            dest = os.path.join(_RAW_MOUNT, f"{os.path.basename(job_id)}.tgz")
            shutil.move(_RAW_ARCHIVE, dest)
            try:
                raw.commit()
            except Exception as exc:
                print(f"[run_tool] raw.commit() raised: {exc}", flush=True)
            raw_info = {"raw_tgz_volume": _RAW_VOLUME, "raw_tgz_volume_path": dest}
            print(
                f"[run_tool] raw tree parked at {dest} (volume {_RAW_VOLUME}, "
                f"{os.path.getsize(dest) / 1e6:.1f} MB)",
                flush=True,
            )
        else:
            print(f"[run_tool] no {_RAW_ARCHIVE} to park", flush=True)
    except Exception as exc:
        print(f"[run_tool] raw capture failed (non-fatal): {exc}", flush=True)

    # Persist any lazily-materialised weight downloads. Idempotent once seeded.
    try:
        weights.commit()
    except Exception as exc:
        print(f"[run_tool] weights.commit() raised: {exc}", flush=True)

    return {
        "exit_code": result.returncode,
        "stdout_tail": "",
        "stderr_tail": "",
        "provider_job_id": payload.get("job_id", ""),
        "smoke_result": smoke_result,
        **raw_info,
    }
