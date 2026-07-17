"""Modal app for D2 — AF2 standalone (``ranomics-af2-prod``).

Deploy:
    modal deploy tools/af2/modal_app.py

Runtime: tools-hub's ``gpu.modal_client.ModalClient.submit`` resolves
this function via ``modal.Function.from_name("ranomics-af2-prod",
"run_tool")`` and calls ``.spawn(payload)``. The function body writes
the payload to env vars and runs the standalone ``run_pipeline.py``
subprocess, which writes results to ``/tmp/smoke_results.json``. The
wrapper reads that file and returns it inline via ``smoke_result`` so
the hub can poll the FunctionCall return value rather than wait on the
webhook — identical shape to the D1 MPNN and composite Kendrew apps.

Self-contained rationale: Modal deploys only the single file you pass
to ``modal deploy`` plus modules it can auto-detect. The Kendrew apps
had portability bugs with sibling-module imports, so the same pattern
(fully self-contained) applies here.

GPU: A100-80GB per ATOMIC-TOOLS.md D2 section — AF2-multimer on
sequences > ~400 AA needs the 80 GB seat. Timeout 20 minutes per the
user-spec (MSA fetch can take 3-5 min cold on the MMseqs2 public
server; fold is usually <5 min at ≤1500 AA).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

import modal

_TOOL = "af2"
# Paths resolved relative to the repo root at ``modal deploy`` time.
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/run_pipeline.py"
_GPU = "A100-80GB"
# 4 h ceiling covers the standalone tier (~5-10 min warm, ~30 min cold)
# and the batch preset (up to 50 records at ~5 min/fold warm
# sequential). Cold-pod JAX JIT compile + fold takes >18 min for monomer
# on cold A100 (Bug 8b); keep headroom so a 50-record batch that lands
# on a cold container can still complete.
_MAX_SESSION_S = 14400
_PYTHON = "python3"

# Raw run artifacts get their OWN Volume, never a weights/cache volume: a
# weights cache exists to make cold starts cheap and has no eviction path,
# so parking GB-scale run output in it bloats the very thing it is for and
# leaves no way to reap raw without touching weights.
_RAW_MOUNT = "/raw"
_RAW_VOLUME = f"ranomics-{_TOOL}-raw"
# Fixed path run_pipeline.py tars its work dir to (RAW_ARCHIVE_PATH there).
_RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"


def _build_run_env(payload: dict) -> dict[str, str]:
    """Translate a Modal payload into env vars for ``run_pipeline.py``.

    Mirrors the D1 MPNN + Kendrew app env-var contract so
    ``run_pipeline.py`` stays provider-agnostic. AF2 does not use
    ``input_presigned_url`` — the FASTA ships inline in ``job_spec``
    because it is small.
    """
    env: dict[str, str] = {
        "JOB_PAYLOAD": json.dumps(
            {
                "job_spec": payload.get("job_spec", {}),
                "input_presigned_url": payload.get("input_presigned_url", ""),
                # Required for the batch preset's partial-results streaming.
                # The standalone tier ignores it (results inline via the
                # function return value). Matches the Boltz-2 contract.
                "upload_urls_endpoint": payload.get("upload_urls_endpoint", ""),
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
# NOTE: Option A (image.run_function JAX cache bake) was attempted and
# rolled back — colabfold_batch consistently exceeds 25 min on Modal
# A100 even during build, so we cannot bake the JIT cache via that
# pattern. Root cause not yet identified. Until then, runtime relies on
# B+ generous timeouts.

app = modal.App("ranomics-af2-prod")

raw_volume = modal.Volume.from_name(_RAW_VOLUME, create_if_missing=True)


def _park_raw_archive(job_id: str) -> dict[str, str]:
    """Move the pipeline's raw archive onto the raw Volume.

    ``run_pipeline.py`` tars its COMPLETE work dir to
    ``/tmp/raw_archive.tgz`` before teardown; this parks it under a
    deterministic name so it can be fetched later without anything new
    travelling through the DB.

    A Volume rather than an inline return or an upload, all three of
    which were checked: ``gpu/modal_client.py`` rejects a non-dict return
    outright and pipes dict values into the ``tool_jobs.result`` JSONB
    column (inline b64 has already broken that write, which is why
    ``shared/jobs.py`` has to defend it), and Supabase Storage caps
    objects at 20 MB with no gzip/tar in its MIME allowlist.

    Best-effort: capture must never fail the run. Returns the keys to
    merge into the return dict, or ``{}`` if there is nothing to park.
    """
    try:
        if not os.path.isfile(_RAW_ARCHIVE_PATH):
            print("[raw] pipeline wrote no archive — nothing to park", flush=True)
            return {}
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job_id or "")).strip("_")
        if not name:
            name = f"unknown_{int(time.time())}"
        os.makedirs(_RAW_MOUNT, exist_ok=True)
        dest = os.path.join(_RAW_MOUNT, f"{name}.tgz")
        size = os.path.getsize(_RAW_ARCHIVE_PATH)
        shutil.move(_RAW_ARCHIVE_PATH, dest)
        try:
            raw_volume.commit()
        except Exception as exc:  # noqa: BLE001 — a commit race must not lose the run
            print(f"[raw] volume commit failed (non-fatal): {exc}", flush=True)
        print(
            f"[raw] parked {size / 1e6:.1f} MB at {dest} (volume {_RAW_VOLUME})",
            flush=True,
        )
        # Top-level keys are ignored by _interpret_pipeline_return, so this
        # needs zero client changes; they exist to tell the caller where to
        # fetch from rather than print a `modal volume get` line for a human
        # to run, which has a demonstrated ~0% follow-through rate.
        return {"raw_tgz_volume": _RAW_VOLUME, "raw_tgz_volume_path": dest}
    except Exception as exc:  # noqa: BLE001 — capture is best-effort by design
        print(
            f"[raw] parking failed (non-fatal): {type(exc).__name__}: {exc}",
            flush=True,
        )
        return {}


@app.function(image=image, gpu=_GPU, timeout=_MAX_SESSION_S,
              volumes={_RAW_MOUNT: raw_volume})
def run_tool(payload: Any) -> dict:
    """Run one AF2 session (smoke or standalone).

    Subprocess stdout/stderr stream live to Modal's function logs so
    failures are visible via ``modal app logs ranomics-af2-prod``
    without fetching the FunctionCall return.

    ``payload`` is annotated ``Any`` rather than ``dict`` because the
    Modal CLI refuses to introspect bare ``dict`` / parameterised
    ``dict[str, ...]`` annotations (``unparseable annotation: dict``)
    when invoking via ``modal run tools/af2/modal_app.py::run_tool
    --payload '{...}'`` — lifted from the D1 MPNN modal_app after
    Codex P2 there. The webhook caller passes a dict either way;
    ``Any`` keeps both call paths alive.
    """
    import sys

    env = _merged_environment(payload)
    cmd = [_PYTHON, "-u", _RUN_PIPELINE_REMOTE]

    # Clear any stale smoke_results.json from a prior invocation on a
    # warm Modal container. Without this, if the current run's
    # run_pipeline.py crashes before writing a fresh file (e.g. early
    # import error, OOM, sys.exit from preflight with a write failure),
    # this wrapper would read the previous job's result and
    # ``gpu.modal_client._interpret_pipeline_return()`` would mark the
    # new job succeeded with another run's output. Mirrors D3 ColabFold
    # modal_app.py:123-128 (Codex P1 fix; AF2 was missing it).
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[run_tool] could not remove stale smoke_results.json: {exc}", flush=True)

    # Same warm-container hazard, same fix: a stale raw archive left by a
    # prior invocation would be parked under THIS job's id and read back as
    # this job's output tree.
    try:
        os.remove(_RAW_ARCHIVE_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[run_tool] could not remove stale raw archive: {exc}", flush=True)

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

    # Unconditional: not gated on exit_code, on smoke_result, or on what got
    # uploaded. A run that crashed before writing output is exactly when the
    # tree matters most.
    raw_keys = _park_raw_archive(payload.get("job_id", ""))

    return {
        "exit_code": result.returncode,
        "stdout_tail": "",
        "stderr_tail": "",
        "provider_job_id": payload.get("job_id", ""),
        "smoke_result": smoke_result,
        **raw_keys,
    }
