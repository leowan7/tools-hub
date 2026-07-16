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
import subprocess
import sys
from typing import Any

import modal

_TOOL = "iggm"
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/IgGM/run_pipeline.py"
_GPU = "A100-40GB"
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

image = (
    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
)

app = modal.App("ranomics-iggm-prod")


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    volumes={"/opt/IgGM/checkpoints": checkpoints},
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
    result = subprocess.run(
        cmd, env=env, stdout=sys.stdout, stderr=sys.stderr,
        timeout=max(60, _MAX_SESSION_S - 30),
    )
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

    return {
        "exit_code": result.returncode,
        "stdout_tail": "",
        "stderr_tail": "",
        "provider_job_id": payload.get("job_id", ""),
        "smoke_result": smoke_result,
    }
