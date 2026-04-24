"""Modal app for D3 — ColabFold standalone (``ranomics-colabfold-prod``).

Deploy:
    modal deploy tools/colabfold/modal_app.py

Runtime: tools-hub's ``gpu.modal_client.ModalClient.submit`` resolves
this function via ``modal.Function.from_name("ranomics-colabfold-prod",
"run_tool")`` and calls ``.spawn(payload)``. The function body writes
the payload to env vars and runs the standalone ``run_pipeline.py``
subprocess, which writes results to ``/tmp/smoke_results.json``. The
wrapper reads that file and returns it inline via ``smoke_result`` so
the hub can poll the FunctionCall return value rather than wait on the
webhook — identical shape to the D1 MPNN app and the composite Kendrew
apps.

Self-contained rationale: Modal deploys only the single file you pass
to ``modal deploy`` plus modules it can auto-detect. The Kendrew apps
had portability bugs with sibling-module imports, so the same pattern
(fully self-contained) applies here.

GPU: A100-40GB per ATOMIC-TOOLS.md D3 section — ColabFold's AF2
multimer weights + JAX JIT need ~30GB at peak for ubiquitin-sized
inputs, so the 24GB A10G would OOM on anything beyond toy sequences.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import modal

_TOOL = "colabfold"
# Paths resolved relative to the repo root at ``modal deploy`` time.
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/run_pipeline.py"
_GPU = "A100-40GB"
_MAX_SESSION_S = 600  # 10 min per task spec — no-MSA ColabFold is fast.
_PYTHON = "python3"


def _build_run_env(payload: dict) -> dict[str, str]:
    """Translate a Modal payload into env vars for ``run_pipeline.py``.

    Mirrors the Kendrew / MPNN app env-var contract so
    ``run_pipeline.py`` stays provider-agnostic. The tier determines
    whether ``input_presigned_url`` is used (standalone tier) or
    ignored in favour of the baked smoke fixture.
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


image = (
    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
)

app = modal.App("ranomics-colabfold-prod")


@app.function(image=image, gpu=_GPU, timeout=_MAX_SESSION_S)
def run_tool(payload: Any) -> dict:
    """Run one ColabFold session (smoke or standalone).

    Subprocess stdout/stderr stream live to Modal's function logs so
    failures are visible via ``modal app logs ranomics-colabfold-prod``
    without fetching the FunctionCall return.

    ``payload`` is annotated ``Any`` rather than ``dict`` because the
    Modal CLI refuses to introspect bare ``dict`` / parameterised
    ``dict[str, ...]`` annotations (``unparseable annotation: dict``)
    when invoking via ``modal run tools/colabfold/modal_app.py::run_tool
    --payload '{...}'``. The webhook caller in
    ``gpu.modal_client.ModalClient.submit(...).spawn(payload)`` passes
    a dict either way; ``Any`` keeps both call paths alive. Lifted from
    the D1 MPNN fix (commit ``cdc9e3a``).
    """
    import sys

    env = _merged_environment(payload)
    cmd = [_PYTHON, "-u", _RUN_PIPELINE_REMOTE]

    # Clear any stale smoke_results.json from a prior invocation on a
    # warm Modal container. Without this, if the current run's
    # run_pipeline.py crashes before writing a fresh file (e.g. early
    # import error, OOM, sys.exit from preflight with a write failure),
    # this wrapper would read the previous job's result and
    # ``gpu.modal_client._interpret_kendrew_return()`` would mark the
    # new job succeeded with another run's output. Codex P1.
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[run_tool] could not remove stale smoke_results.json: {exc}", flush=True)

    print(f"[run_tool] spawning: {' '.join(cmd)}", flush=True)
    print(
        f"[run_tool] JOB_ID={env.get('JOB_ID')} TIER={env.get('JOB_TIER')} "
        f"WEBHOOK={env.get('WEBHOOK_URL')}",
        flush=True,
    )

    result = subprocess.run(
        cmd,
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
        # Keep a safety margin under the hard Modal timeout so the
        # wrapper still has time to read smoke_results.json.
        timeout=max(60, _MAX_SESSION_S - 30),
    )

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
    }
