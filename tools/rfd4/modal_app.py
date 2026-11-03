"""Modal app for RFD4-Proteina de-novo binder generation (``ranomics-rfd4-prod``).

NOT the same model line as ``tools/proteina/`` (NVIDIA Proteina-Complexa):
separate slug, separate image, separate weights Volume.

Deploy:
    modal deploy tools/rfd4/modal_app.py

Not deployed by anything yet. ``rfd4`` is absent from the deploy matrix in
``.github/workflows/deploy-modal.yml``, and scope doc §8 adds it at step 6;
until then this file is built by no workflow and reachable by no customer.

Runtime: tools-hub's ``gpu.modal_client.ModalClient.submit`` resolves this
function via ``modal.Function.from_name("ranomics-rfd4-prod", "run_tool")``
and calls ``.spawn(payload)``. The function writes the payload to env vars and
runs ``run_pipeline.py`` as a subprocess (the self-contained atomic pattern of
tools/iggm and tools/boltz2), which builds the condition spec, runs
``rfd4-generate``, uploads each design via a presigned PUT as it lands, emits
heartbeats, and writes ``/tmp/smoke_results.json``. The wrapper returns that
file inline via ``smoke_result``.

GPU: A100-80GB. Measured, not chosen here: scope doc §5.5 (driver, kernel
launch, tmol compiled operators) and §5.8's 8-design run, both on an
A100-SXM4-80GB. ``memory=65536`` and the weights Volume at ``/data`` are the
same two settings §5.8's run used (``docs/scoping/rung4-probe/rung4_app.py``).
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

_TOOL = "rfd4"
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
# Both local files land in ONE remote directory, and that is what puts
# numbering.py on the subprocess's import path: CPython prepends the script's
# own directory to sys.path, so run_pipeline.py's ``import numbering`` resolves
# to the copy beside it. Modal deploys the single file passed to ``modal
# deploy`` plus auto-detected modules, so run_pipeline.py cannot import
# ``shared/`` and numbering.py has to travel with it.
_PIPELINE_DIR = "/opt/rfd4-pipeline"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = f"{_PIPELINE_DIR}/run_pipeline.py"
_NUMBERING_LOCAL = f"tools/{_TOOL}/numbering.py"
_NUMBERING_REMOTE = f"{_PIPELINE_DIR}/numbering.py"
_GPU = "A100-80GB"
# cu13 is the only accelerator variant proven here, and the Dockerfile's ARG has
# no default on purpose (tools/rfd4/Dockerfile.modal:36-49): its install layer
# exits 2 on an unset value, so this build arg is what makes the image buildable
# at all. cu12 resolves a different package set with no BioIR backend.
_ACCELERATOR = "cu13"
# Raw run archives get their OWN Volume, never the weights Volume: a weights
# Volume has no eviction path, so parking GB-scale run output in it bloats the
# one thing cold starts depend on.
_RAW_VOLUME = f"ranomics-{_TOOL}-raw"
_RAW_MOUNT = "/raw"
# Must match RAW_ARCHIVE_PATH in run_pipeline.py — the subprocess cannot mount a
# Volume, so it tars to this fixed path and the wrapper moves it onto /raw.
_RAW_ARCHIVE = "/tmp/raw_archive.tgz"
# 45 min. §5.8's 8-design run took 217.4 s wall end to end on a 211-residue
# target, so this is ~12x that measured figure; the headroom is for a target at
# §4.3's hard_cap_combined_aa and for a cold container paging 2.59 GiB of
# checkpoints off the Volume. A ceiling, not a budget: the wallet's own runtime
# ceiling is step 4's and is not set here.
_MAX_SESSION_S = 2700


def _build_run_env(payload: dict) -> dict[str, str]:
    """Translate a Modal payload into env vars for run_pipeline.py (the same
    env-var contract as tools/iggm, including upload_urls_endpoint for
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
        "JOB_TIER": str(payload.get("job_tier", payload.get("tier", "binder_design"))),
    }


def _merged_environment(payload: dict) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(_build_run_env(payload))
    return merged


def _start_beat(env: dict) -> None:
    """POST a ``starting`` heartbeat to the hub as the GPU function begins.

    The hub stamps ``started_at`` on a job's first heartbeat
    (webhooks/modal.py::_handle_heartbeat), and a cancel bills from
    ``started_at`` once a heartbeat has arrived (shared/jobs.py::cancel_job).
    A failed POST is printed and the run goes on.
    """
    import urllib.parse
    import urllib.request

    url, job_id = env.get("WEBHOOK_URL", ""), env.get("JOB_ID", "")
    if not (url and job_id):
        return
    body = {"job_id": job_id, "stage": "starting"}
    if env.get("SEED_NAME"):
        body.update(seed=env["SEED_NAME"], job_token=env.get("JOB_TOKEN", ""))
    try:
        beat_url = urllib.parse.urlunparse(
            urllib.parse.urlparse(url)._replace(path="/webhooks/heartbeat"))
        req = urllib.request.Request(
            beat_url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as exc:  # noqa: BLE001
        print(f"[start_beat] heartbeat failed: {exc}", flush=True)


# The two checkpoints, seeded once by tools/rfd4/seed_volumes.py.
# ``create_if_missing`` is deliberately absent: an empty Volume would let a run
# start and die inside upstream's weight loader, and the seeder is what creates
# this Volume.
weights = modal.Volume.from_name("rfd4-proteina-weights")
raw = modal.Volume.from_name(_RAW_VOLUME, create_if_missing=True)


def _stash_raw(job_id: str) -> dict[str, str]:
    """Park the pipeline's raw work-tree archive on the raw Volume.

    Returns the top-level keys the caller merges into its return dict, or ``{}``
    if there was nothing to park. Best-effort: never raises — a run that died
    before writing an archive is exactly when the rest of the result matters.

    A Volume rather than an upload or an inline return, for the reasons
    ``tools/iggm/modal_app.py::_stash_raw`` records: ``gpu/modal_client.py``
    flows the returned dict into the ``tool_jobs.result`` JSONB column, and
    Supabase Storage caps objects at 20 MB with no tar in its MIME allowlist.
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
    modal.Image.from_dockerfile(
        _DOCKERFILE, add_python=None, build_args={"RFD4_ACCELERATOR": _ACCELERATOR}
    )
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
    .add_local_file(_NUMBERING_LOCAL, _NUMBERING_REMOTE, copy=True)
)

app = modal.App("ranomics-rfd4-prod")


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    # /data is where upstream's configs/inference.yaml:39 and :46 resolve the
    # flow and autoencoder checkpoints, via DATA_PATH in the Dockerfile.
    volumes={"/data": weights, _RAW_MOUNT: raw},
    memory=65536,
)
def run_tool(payload: Any) -> dict:
    """Run one RFD4-Proteina binder-generation session. stdout/stderr
    live-stream to Modal's function logs (``modal app logs ranomics-rfd4-prod``)."""
    env = _merged_environment(payload)
    _start_beat(env)
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
