"""Modal app for ESMFold2-design (``ranomics-esmfold2-design-prod``).

Deploy:
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \\
        modal deploy tools/esmfold2_design/modal_app.py

Runtime: tools-hub's ``gpu.modal_client.ModalClient.submit`` resolves
this function via ``modal.Function.from_name("ranomics-esmfold2-design-prod",
"run_tool")`` and calls ``.spawn(payload)``. The function body writes the
payload to env vars and runs ``run_pipeline.py`` as a subprocess, which
invokes the gradient-descent loop from the upstream
``binder_design.py`` (vendored into /opt/ by the Dockerfile). The
wrapper reads ``/tmp/smoke_results.json`` and returns it inline via
``smoke_result``.

GPU: H100. The 150-step gradient run takes ~10-15 min per design on a
warm container; weights pull is ~30 GB on a cold Volume. Memory is sized
for the default ``REUSE_ESMC=False`` path (27 GB VRAM); flip the
``use_scaling_critics`` env var to load the 15-checkpoint ensemble and
bump memory to 60 GB host RAM.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import modal

_TOOL = "esmfold2-design"
_RUN_PIPELINE_LOCAL = "tools/esmfold2_design/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/run_pipeline.py"
_GPU = "H100"

# Pinned upstream commit on evolutionaryscale/esm. binder_design.py at this
# SHA is cloned into /opt/ at image build time so run_pipeline.py can do
# ``import binder_design`` directly. Bump the SHA to bump the algorithm.
_ESM_GIT_SHA = "f652b471d29da828b31e9b7a9cf7d0a7803240f5"
# 60 min ceiling — covers the worst-case scfv preset with batch_size=6
# plus weight-load tail latency on a cold container.
_MAX_SESSION_S = 60 * 60
_PYTHON = "python3"


def _build_run_env(payload: dict) -> dict[str, str]:
    """Translate a Modal payload into env vars for ``run_pipeline.py``."""
    env: dict[str, str] = {
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
        "JOB_TIER": str(payload.get("job_tier", "minibinder")),
    }
    return env


def _merged_environment(payload: dict) -> dict[str, str]:
    """Merge run-specific env vars into the container's existing env."""
    merged = dict(os.environ)
    merged.update(_build_run_env(payload))
    return merged


# ESMFold2 + ESMC weights download is ~30-40 GB on first run. The Volume
# survives deploys so only the very first cold prod call pays the cost.
weights = modal.Volume.from_name(
    "ranomics-esmfold2-models", create_if_missing=True
)

# Mirrors the upstream cookbook image build (evolutionaryscale/esm
# cookbook/tutorials/binder_design.py, ~line 1150): micromamba base for
# the conda-only deps (anarci + hmmer from bioconda), then pip for esm
# itself. abnumber is the pythonic wrapper around anarci that
# binder_design imports. Switched from a hand-rolled Dockerfile after
# micromamba 2.x changed the shell-init CLI and broke the build.
image = (
    modal.Image.micromamba(python_version="3.12")
    .apt_install("git", "build-essential", "curl", "wget", "ca-certificates")
    .micromamba_install(
        "anarci>=2020.04.03",
        "hmmer=3.4",
        channels=["conda-forge", "bioconda"],
    )
    .pip_install(
        "abnumber",
        "biopython",
        "requests",
        "pandas",
        "py3Dmol",
        f"esm @ git+https://github.com/evolutionaryscale/esm.git@{_ESM_GIT_SHA}",
    )
    .run_commands(
        # Fetch the pinned binder_design.py via GitHub's archive endpoint
        # rather than git clone — we only need one file, and shallow git
        # fetch by SHA fails on GitHub (couldn't find remote ref).
        f"curl -fL https://github.com/evolutionaryscale/esm/archive/{_ESM_GIT_SHA}.tar.gz "
        f"  -o /tmp/esm.tar.gz "
        f"&& tar -xzf /tmp/esm.tar.gz -C /tmp/ "
        f"&& cp /tmp/esm-{_ESM_GIT_SHA}/cookbook/tutorials/binder_design.py "
        f"     /opt/binder_design.py "
        f"&& test -f /opt/binder_design.py "
        f"&& rm -rf /tmp/esm.tar.gz /tmp/esm-{_ESM_GIT_SHA}",
    )
    .env({
        "HF_HOME": "/models",
        "HF_XET_HIGH_PERFORMANCE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    .workdir("/opt")
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
)

app = modal.App("ranomics-esmfold2-design-prod")


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    cpu=16,
    memory=10 * 1024,
    volumes={"/models": weights},
)
def run_tool(payload: Any) -> dict:
    """Run one ESMFold2-design session.

    Subprocess stdout/stderr stream live to Modal's function logs so
    failures are visible via ``modal app logs ranomics-esmfold2-design-prod``
    without fetching the FunctionCall return.
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

    # Persist the weights Volume so any newly-downloaded model files survive
    # the container teardown. Idempotent if nothing was written.
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
    }
