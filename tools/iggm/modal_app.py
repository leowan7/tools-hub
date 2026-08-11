"""Modal app for IgGM antibody / nanobody design (``ranomics-iggm-prod``).

Deploy:
    modal deploy tools/iggm/modal_app.py

Runtime: tools-hub's ``gpu.modal_client.ModalClient.submit`` resolves this
function via ``modal.Function.from_name("ranomics-iggm-prod", "run_tool")``
and calls ``.spawn(payload)``. The function writes the payload to env vars
and runs ``run_pipeline.py`` as a subprocess (self-contained atomic pattern,
identical to boltz2), which builds the IgGM FASTA (antigen from the uploaded
PDB), runs ``design.py``, uploads designs via presigned PUT URLs, emits
heartbeats, and writes ``/tmp/smoke_results.json``. The wrapper returns that
file inline via ``smoke_result``.

GPU: A100-40GB. Weights (ESM-PPI-650M-Ab + the per-task design trunk +
IGSO3 buffer) auto-download from Zenodo record 16909543 on first run; the
``iggm-checkpoints`` Volume caches them so only the first prod run pays the
download. If the largest antibody+antigen complex OOMs on 40GB, bump
``_GPU`` and the wallet ``gpu_class`` to A100-80GB together.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any

import modal

_TOOL = "iggm"
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/IgGM/run_pipeline.py"
_GPU = "A100-40GB"
# Raw run archives get their OWN Volume, never the checkpoints cache: a weights
# cache exists to make cold starts cheap and has no eviction path, so parking
# GB-scale run output in it bloats the very thing it is for and leaves no way to
# reap raw without touching weights.
_RAW_VOLUME = f"ranomics-{_TOOL}-raw"
_RAW_MOUNT = "/raw"
# Must match RAW_ARCHIVE_PATH in run_pipeline.py — the subprocess cannot mount a
# Volume, so it tars to this fixed path and the wrapper moves it onto /raw.
_RAW_ARCHIVE = "/tmp/raw_archive.tgz"
# 60 min ceiling — covers a 100-sample affinity-maturation run plus the
# cold Zenodo weight pull on the first run (subsequent runs hit the Volume).
_MAX_SESSION_S = 3600


def _build_run_env(payload: dict) -> dict[str, str]:
    """Translate a Modal payload into env vars for run_pipeline.py (mirrors
    the boltz2 env-var contract; IgGM also needs upload_urls_endpoint for
    per-design streaming)."""
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
        "JOB_TIER": str(payload.get("job_tier", payload.get("tier", "complex_prediction"))),
    }


def _merged_environment(payload: dict) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(_build_run_env(payload))
    return merged


# IgGM checkpoints (Zenodo 16909543) cache here across cold starts. VERIFIED:
# IgGM saves each checkpoint to `{os.getcwd()}/checkpoints/<model>.pth`, and
# run_pipeline runs design.py with cwd=/opt/IgGM, so downloads land in
# /opt/IgGM/checkpoints == this mount and persist after the first prod run.
checkpoints = modal.Volume.from_name("iggm-checkpoints", create_if_missing=True)
raw = modal.Volume.from_name(_RAW_VOLUME, create_if_missing=True)


def _stash_raw(job_id: str) -> dict[str, str]:
    """Park the pipeline's raw work-tree archive on the raw Volume.

    Returns the top-level keys the caller merges into its return dict, or ``{}``
    if there was nothing to park. Best-effort: never raises — a run that died
    before writing an archive is exactly when the rest of the result matters.

    A Volume rather than an upload or an inline return, all three checked:
    ``gpu/modal_client.py`` rejects a non-dict return and flows the dict into the
    ``tool_jobs.result`` JSONB column (a big inline b64 already broke that once,
    see ``shared/jobs.py``); ``webhooks/modal.py`` nulls a non-dict result; and
    Supabase Storage caps objects at 20 MB with no gzip/tar in its MIME
    allowlist. A deterministic name means nothing new travels through the DB.
    """
    try:
        if not os.path.isfile(_RAW_ARCHIVE):
            print(f"[raw] no archive at {_RAW_ARCHIVE} (pipeline died before tarring?)", flush=True)
            return {}
        # The job id is the deterministic handle the caller already knows; keep
        # it to path-safe characters so it can never escape the mount.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(job_id or "")).strip("._") or "unknown"
        os.makedirs(_RAW_MOUNT, exist_ok=True)
        dest = os.path.join(_RAW_MOUNT, f"{safe}.tgz")
        size = os.path.getsize(_RAW_ARCHIVE)
        shutil.move(_RAW_ARCHIVE, dest)
        try:
            raw.commit()
        except Exception as exc:
            # Report the path anyway: a commit race must not lose the pointer.
            print(f"[raw] raw.commit() raised: {exc}", flush=True)
        print(f"[raw] parked {size / 1e6:.1f} MB at {dest} (volume {_RAW_VOLUME})", flush=True)
        return {"raw_tgz_volume": _RAW_VOLUME, "raw_tgz_volume_path": dest}
    except Exception as exc:
        print(f"[raw] stash failed (non-fatal): {type(exc).__name__}: {exc}", flush=True)
        return {}


image = (
    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
)

app = modal.App("ranomics-iggm-prod")


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    volumes={"/opt/IgGM/checkpoints": checkpoints, _RAW_MOUNT: raw},
)
def run_tool(payload: Any) -> dict:
    """Run one IgGM design/prediction session. stdout/stderr live-stream to
    Modal's function logs (``modal app logs ranomics-iggm-prod``)."""
    env = _merged_environment(payload)
    cmd = ["python3", "-u", _RUN_PIPELINE_REMOTE]
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
    # another run's designs — that branch keys off smoke["status"] alone and never
    # consults exit_code. Codex P1 (colabfold).
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[run_tool] could not remove stale smoke_results.json: {exc}", flush=True)
    # Warm containers are reused: a leftover raw archive from a prior job would be parked
    # under THIS job's id. Clear it so we only ever park a tar this run actually wrote.
    try:
        os.remove(_RAW_ARCHIVE)
    except OSError:
        pass

    raw_info: dict[str, str] = {}
    try:
        result = subprocess.run(
            cmd, env=env, stdout=sys.stdout, stderr=sys.stderr,
            timeout=max(60, _MAX_SESSION_S - 30),
        )
    finally:
        # In a finally so a subprocess timeout still parks whatever the pipeline
        # managed to tar. The archive lands on the Volume under the job id, so it
        # is retrievable even on the paths where this function never returns.
        raw_info = _stash_raw(str(payload.get("job_id", "")))
    print(f"[run_tool] subprocess exited: {result.returncode}", flush=True)

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

    try:
        checkpoints.commit()
    except Exception as exc:
        print(f"[run_tool] checkpoints.commit() raised: {exc}", flush=True)

    # raw_tgz_volume / raw_tgz_volume_path ride at the TOP LEVEL: the client's
    # _interpret_pipeline_return ignores unknown top-level keys, so this needs
    # zero client changes and nothing large travels through the DB.
    return {
        "exit_code": result.returncode,
        "stdout_tail": "",
        "stderr_tail": "",
        "provider_job_id": payload.get("job_id", ""),
        "smoke_result": smoke_result,
        **raw_info,
    }
