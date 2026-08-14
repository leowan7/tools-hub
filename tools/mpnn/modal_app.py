"""Modal app for D1 — ProteinMPNN standalone (``ranomics-mpnn-prod``).

Deploy:
    modal deploy tools/mpnn/modal_app.py

Runtime: tools-hub's ``gpu.modal_client.ModalClient.submit`` resolves
this function via ``modal.Function.from_name("ranomics-mpnn-prod",
"run_tool")`` and calls ``.spawn(payload)``. The function body writes
the payload to env vars and runs the standalone ``run_pipeline.py``
subprocess, which writes results to ``/tmp/smoke_results.json``. The
wrapper reads that file and returns it inline via ``smoke_result`` so
the hub can poll the FunctionCall return value rather than wait on the
webhook — identical shape to the composite Kendrew apps.

Self-contained rationale: Modal deploys only the single file you pass
to ``modal deploy`` plus modules it can auto-detect. The Kendrew apps
had portability bugs with sibling-module imports, so the same pattern
(fully self-contained) applies here.

GPU: A10G-24GB per ATOMIC-TOOLS.md D1 section — MPNN does not need an
A100 seat.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import modal

_TOOL = "mpnn"
# Paths resolved relative to the repo root at ``modal deploy`` time.
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/run_pipeline.py"
_GPU = "A10G"
_MAX_SESSION_S = 600  # 10 min per ATOMIC-TOOLS.md D1 timeout.
_PYTHON = "python3"

# Raw run artifacts get their OWN Volume — never a weights/cache volume, which
# has no eviction path and exists to keep cold starts cheap.
#
# A Volume, rather than an upload or an inline return, because all three were
# checked: gpu/modal_client.py rejects a non-dict return; webhooks/modal.py
# NULLs a non-dict result; a big b64 inside the returned dict flows into the
# tool_jobs.result JSONB column and throws on UPDATE, wedging the job in
# "running" (shared/jobs.py already carries the scar tissue for that); and
# Supabase Storage caps objects at 20 MB with no gzip/tar in its MIME
# allowlist. Naming the tar after the job id means nothing new has to travel
# through the DB at all — _interpret_pipeline_return ignores unknown top-level
# keys, so this needs zero client changes.
_RAW_VOLUME = f"ranomics-{_TOOL}-raw"
_RAW_MOUNT = "/raw"
_RAW_ARCHIVE = "/tmp/raw_archive.tgz"  # written by run_pipeline.py's _archive_raw
raw_volume = modal.Volume.from_name(_RAW_VOLUME, create_if_missing=True)


def _build_run_env(payload: dict) -> dict[str, str]:
    """Translate a Modal payload into env vars for ``run_pipeline.py``.

    Mirrors the Kendrew app env-var contract so ``run_pipeline.py``
    stays provider-agnostic. The tier determines whether
    ``input_presigned_url`` is used (standalone tier) or ignored in
    favour of the baked smoke target.
    """
    env: dict[str, str] = {
        "JOB_PAYLOAD": json.dumps(
            {
                "job_spec": payload.get("job_spec", {}),
                "input_presigned_url": payload.get("input_presigned_url", ""),
                "job_token": payload.get("job_token", ""),
                "tier": payload.get("tier", ""),
            }
        ),
        "WEBHOOK_URL": str(payload.get("webhook_url", "")),
        "JOB_ID": str(payload.get("job_id", "")),
        "JOB_TOKEN": str(payload.get("job_token", "")),
        "JOB_TIER": str(payload.get("job_tier", "standalone")),
    }
    return env


def _merged_environment(payload: dict) -> dict[str, str]:
    """Merge run-specific env vars into the container's existing env."""
    merged = dict(os.environ)
    merged.update(_build_run_env(payload))
    return merged


def _ship_raw(job_id: str) -> dict[str, str]:
    """Park run_pipeline.py's raw archive on the raw Volume. Never raises.

    Returns the two keys that tell the caller where the tar landed, or ``{}``
    if there was nothing to park. Capture must never fail the run: a tool that
    crashed before writing output is exactly when the tree matters most, so
    problems are printed, not raised.
    """
    try:
        if not os.path.isfile(_RAW_ARCHIVE):
            print(
                f"[raw] no archive at {_RAW_ARCHIVE} — pipeline died before its "
                f"finally ran (hard timeout / OOM kill?)",
                flush=True,
            )
            return {}
        os.makedirs(_RAW_MOUNT, exist_ok=True)
        # basename() so a malformed job_id can never escape the mount.
        name = os.path.basename(str(job_id).strip()) or "unknown"
        dest = os.path.join(_RAW_MOUNT, f"{name}.tgz")
        shutil.move(_RAW_ARCHIVE, dest)
        size = os.path.getsize(dest)
        try:
            raw_volume.commit()
        except Exception as exc:  # noqa: BLE001 — a commit race must not lose the run
            print(f"[raw] volume commit failed (non-fatal): {exc}", flush=True)
        print(
            f"[raw] parked {size / 1e6:.1f} MB at {dest} (volume {_RAW_VOLUME})",
            flush=True,
        )
        return {"raw_tgz_volume": _RAW_VOLUME, "raw_tgz_volume_path": dest}
    except Exception as exc:  # noqa: BLE001 — capture is best-effort by design
        print(f"[raw] capture failed (non-fatal): {type(exc).__name__}: {exc}", flush=True)
        return {}


image = (
    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
)

app = modal.App("ranomics-mpnn-prod")


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    volumes={_RAW_MOUNT: raw_volume},
)
def run_tool(payload: Any) -> dict:
    """Run one MPNN session (smoke or standalone).

    Subprocess stdout/stderr stream live to Modal's function logs so
    failures are visible via ``modal app logs ranomics-mpnn-prod``
    without fetching the FunctionCall return.

    ``payload`` is annotated ``Any`` rather than ``dict`` because the
    Modal CLI refuses to introspect bare ``dict`` / parameterised
    ``dict[str, ...]`` annotations (``unparseable annotation: dict``)
    when invoking via ``modal run tools/mpnn/modal_app.py::run_tool
    --payload '{...}'``. The webhook caller in
    ``gpu.modal_client.ModalClient.submit(...).spawn(payload)`` passes
    a dict either way; ``Any`` keeps both call paths alive.
    """
    import sys

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
    # another run's sequences — that branch keys off smoke["status"] alone and never
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

    try:
        result = subprocess.run(
            cmd,
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr,
            # Keep a safety margin under the hard Modal timeout so the
            # wrapper still has time to read smoke_results.json.
            timeout=max(60, _MAX_SESSION_S - 30),
        )
    finally:
        # Ship on both exit paths: subprocess.run RAISES TimeoutExpired rather
        # than returning, and a timed-out run is one worth having the tree for.
        # (A SIGKILLed pipeline never reaches its own finally, so there may be
        # nothing to park — _ship_raw says so and returns {}.)
        raw_info = _ship_raw(payload.get("job_id", ""))

    print(f"[run_tool] subprocess exited: {result.returncode}", flush=True)

    smoke_result: dict | None = None
    try:
        with open("/tmp/smoke_results.json") as fh:
            smoke_result = json.load(fh)
        print(
            f"[run_tool] loaded smoke_results.json: "
            f"status={smoke_result.get('status')}",
            flush=True,
        )
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[run_tool] failed to read smoke_results.json: {exc}", flush=True)

    return {
        "exit_code": result.returncode,
        "stdout_tail": "",
        "stderr_tail": "",
        "provider_job_id": payload.get("job_id", ""),
        "smoke_result": smoke_result,
        # raw_tgz_volume / raw_tgz_volume_path, at the TOP level next to
        # exit_code. _interpret_pipeline_return ignores top-level keys it does
        # not recognise, so pointing at the tar costs no client change and
        # nothing large travels through tool_jobs.result.
        **raw_info,
    }
