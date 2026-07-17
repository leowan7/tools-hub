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
import re
import shutil
import subprocess
from typing import Any

import modal

_TOOL = "colabfold"
# Paths resolved relative to the repo root at ``modal deploy`` time.
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/run_pipeline.py"
_GPU = "A100-40GB"
# Raw work-dir capture. ``run_pipeline.py`` tars its complete work dir to
# _RAW_ARCHIVE_PATH before the tree is destroyed; this wrapper parks it on a
# dedicated Volume under the job id. The split exists because the pipeline
# runs as a subprocess and cannot mount a Volume — only this wrapper can.
#
# A DEDICATED Volume, never a shared/weights one: raw run output is GB-scale
# and needs its own reap path, and parking it beside a weights cache bloats
# the thing that exists to make cold starts cheap.
_RAW_VOLUME = f"ranomics-{_TOOL}-raw"
_RAW_MOUNT = "/raw"
_RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"
# 4 h ceiling covers the standalone tier (~1-2 min warm, ~9 min cold)
# and the batch preset (up to 200 records at ~1-2 min/fold warm
# sequential). For a fully-cold container the JAX JIT recompile is paid
# once on the first fold and amortises across the rest of the batch.
_MAX_SESSION_S = 14400
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

app = modal.App("ranomics-colabfold-prod")

raw_volume = modal.Volume.from_name(_RAW_VOLUME, create_if_missing=True)


def _park_raw_archive(job_id: str) -> dict[str, str]:
    """Move the pipeline's raw work-dir tar onto the raw Volume.

    Returns the two keys a caller needs to fetch it, or ``{}`` when there is
    nothing to park. Never raises — capture must not fail the run.

    Volume rather than an inline return or an upload, all three checked:
    ``gpu/modal_client.py`` feeds this dict into the ``tool_jobs.result``
    JSONB column, and a b64 tar in there is what made the UPDATE throw and
    left jobs wedged in "running" (``shared/jobs.py``). Supabase Storage is
    out too: 20 MB object cap and no gzip/tar in the MIME allowlist. Naming
    the object after the job id means nothing new travels through the DB —
    and top-level keys are ignored by ``_interpret_pipeline_return``, so
    this needs zero client changes.
    """
    try:
        if not os.path.isfile(_RAW_ARCHIVE_PATH):
            print(
                f"[raw] no {_RAW_ARCHIVE_PATH} to park — pipeline died before "
                "it could tar, or the work dir was already gone",
                flush=True,
            )
            return {}
        # job_id reaches us straight from the payload; keep it from escaping
        # the mount via a slash or a "..".
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job_id)) or "unknown"
        os.makedirs(_RAW_MOUNT, exist_ok=True)
        dest = os.path.join(_RAW_MOUNT, f"{safe}.tgz")
        size = os.path.getsize(_RAW_ARCHIVE_PATH)
        shutil.move(_RAW_ARCHIVE_PATH, dest)
        try:
            raw_volume.commit()
        except Exception as exc:  # a commit race must not lose the run
            print(f"[raw] volume commit failed: {exc}", flush=True)
        print(
            f"[raw] parked {size / 1e6:.1f} MB at {dest} (volume {_RAW_VOLUME})",
            flush=True,
        )
        return {"raw_tgz_volume": _RAW_VOLUME, "raw_tgz_volume_path": dest}
    except Exception as exc:  # capture is best-effort by design
        print(f"[raw] parking failed (non-fatal): {exc}", flush=True)
        return {}


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    volumes={_RAW_MOUNT: raw_volume},
)
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
    # ``gpu.modal_client._interpret_pipeline_return()`` would mark the
    # new job succeeded with another run's output. Codex P1.
    #
    # raw_archive.tgz needs the identical treatment for a sharper reason: a
    # leftover tar would be parked under THIS job's id and look like this
    # run's evidence. Losing the tree is recoverable by re-running; silently
    # attributing another run's tree to this job is the exact failure this
    # capture exists to prevent.
    for _stale in ("/tmp/smoke_results.json", _RAW_ARCHIVE_PATH):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[run_tool] could not remove stale {_stale}: {exc}", flush=True)

    print(f"[run_tool] spawning: {' '.join(cmd)}", flush=True)
    print(
        f"[run_tool] JOB_ID={env.get('JOB_ID')} TIER={env.get('JOB_TIER')} "
        f"WEBHOOK={env.get('WEBHOOK_URL')}",
        flush=True,
    )

    # try/finally so the tar is parked even when subprocess.run raises
    # TimeoutExpired. The pipeline's own ceiling fires first (14000s batch /
    # 1740s single, vs 14370s here), so on that path it has usually already
    # written the tar — leaving it in a dying container's /tmp is a lost run.
    # The timeout still propagates: this only parks, it never swallows.
    raw_info: dict[str, str] = {}
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
        # Unconditional: not gated on exit_code, on smoke_result parsing, or
        # on anything having been uploaded. A non-zero exit or a
        # zero-candidate "success" is when the tree is worth the most.
        # The Volume object is named after the job id, so even on the raising
        # path — where this dict never reaches the caller — the archive is
        # still fetchable by job id alone.
        raw_info = _park_raw_archive(payload.get("job_id", ""))

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

    # raw_tgz_volume / raw_tgz_volume_path ride at the TOP level, alongside
    # exit_code — never inside smoke_result, which is the curated dict the
    # hub persists. _interpret_pipeline_return ignores unknown top-level
    # keys, so this is additive for every existing caller.
    return {
        "exit_code": result.returncode,
        "stdout_tail": "",
        "stderr_tail": "",
        "provider_job_id": payload.get("job_id", ""),
        "smoke_result": smoke_result,
        **raw_info,
    }
