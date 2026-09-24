"""Modal app for Boltz-2 cofold validation (``ranomics-boltz2-prod``).

Deploy:
    modal deploy tools/boltz2/modal_app.py

Runtime: tools-hub's ``gpu.modal_client.ModalClient.submit`` resolves
this function via ``modal.Function.from_name("ranomics-boltz2-prod",
"run_tool")`` and calls ``.spawn(payload)``. The function body writes
the payload to env vars and runs ``run_pipeline.py`` as a subprocess,
which folds each binder against the antigen, uploads per-design PDBs via
presigned PUT URLs requested from the hub, emits heartbeats with the
per-design ``new_candidate`` for the live status page, and writes a
final summary to ``/tmp/smoke_results.json``. The wrapper reads that file
and returns it inline via ``smoke_result`` so the hub can poll the
FunctionCall return value rather than wait on the webhook — identical
shape to the other atomic tools (MPNN, AF2, ColabFold, ESMFold).

Self-contained rationale: Modal deploys only the single file you pass to
``modal deploy`` plus modules it can auto-detect. The Kendrew apps had
portability bugs with sibling-module imports, so the same self-contained
pattern applies here.

GPU: A100-40GB. A single-sequence design measured ~69 s on this SKU, and
the first of a run ~13 s more — provenance and caveats in the runtime
note in ``tools/boltz2/__init__.py``. The "~15 s kernel plus a ~30 s cold
weight load" split those figures replace is not a per-design cost: that
run gave every design its own ``boltz predict`` process, so each one paid
start-up and a model load, and designs 2 and 3 took 68.5 s and 69.5 s.
Those are whole-process times, so they cannot test either term alone.
``standalone`` no longer folds that way — ``run_pipeline.py::main``
chunks designs into one process each ``FOLD_CHUNK``, which pays the
~60-70 s of start-up once a chunk instead of once a design. So ~69 s is
now a ceiling, and the rate under chunking has not been measured in
prod; see the batching note in ``tools/boltz2/__init__.py``.
The ``msa_server`` preset adds an MSA fetch from the public ColabFold
MMseqs2 endpoint and measures ~214 s/design aggregate, ~3x standalone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import modal

_TOOL = "boltz2"
# Paths resolved relative to the repo root at ``modal deploy`` time.
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/run_pipeline.py"
_GPU = "A100-40GB"
# 60 min ceiling. The "soft 10-binder limit" this comment used to lean on
# does not exist. The enforced ceilings live in ``tools/boltz2/__init__.py``
# and are both applied in its ``validate``: ``MAX_BINDERS = 50``, and
# ``MAX_BINDERS_BY_PRESET["msa_server"] = 16``, sized against THIS constant.
# ``tools/boltz2/meta.py`` advertises that pair to users.
#
# EXTRAPOLATING the PER-DESIGN rates, the two presets landed on opposite
# sides of this ceiling. standalone (81.9 s first design including model
# load, ~69 s marginal): 81.9 + 49 * 69 = ~3463 s, ~4% UNDER. msa_server
# (~214 s/design aggregate, measured 2026-09-21): 50 * 214 = ~10700 s,
# ~3x OVER — it crosses 3600 s at the 17th binder, so a 50-binder
# msa_server run CANNOT finish inside this timeout, which is why that
# preset is capped at 16 (~3424 s) rather than 50. Both are extrapolations
# from three folds at 242-246 aa, not measured 50-binder runs, and longer
# binders push both higher.
#
# CHUNKING HAS SINCE HAPPENED, on standalone only, so that preset's ~4% is
# historical: ``run_pipeline.py``'s ``FOLD_CHUNK`` pays start-up once per
# chunk, which extrapolates 50 standalone binders to ~1000-1525 s and ~58-72%
# under this ceiling. That is an extrapolation from 3- and 5-record probes,
# not a 50-binder run — see the batching note in ``tools/boltz2/__init__.py``
# for what was and was not measured. msa_server is NOT chunked, so its ~3x
# overrun and the cap of 16 stand exactly as above.
#
# Deliberately NOT raised here. An overrun is survivable: each design is
# PUT to its own presigned URL as it is collected, by
# ``tools/boltz2/run_pipeline.py::upload_pdb`` called from inside the
# per-design loop in ``tools/boltz2/run_pipeline.py::main``, so a timeout
# loses the tail of the batch rather than the run. On standalone that tail
# now rounds up to a chunk, since a design is collected only after its
# chunk's process exits — a smaller absolute risk on a run that is ~3x
# shorter, but a coarser one. Raising this ceiling or lifting the
# msa_server cap back to 50 still both belong with a real large-batch
# measurement, which does not exist yet.
_MAX_SESSION_S = 3600
_PYTHON = "python3"
# Where ``run_pipeline.py`` tars its complete work tree at teardown, and where
# this wrapper parks it. The archive gets its OWN Volume, never the weights
# cache: a weights volume exists to make cold starts cheap and has no eviction
# path, so parking GB-scale run output in it bloats the very thing it is for
# and leaves no way to reap raw without touching weights.
_RAW_ARCHIVE = "/tmp/raw_archive.tgz"
_RAW_VOLUME = "ranomics-boltz2-raw"
_RAW_MOUNT = "/raw"


def _build_run_env(payload: dict) -> dict[str, str]:
    """Translate a Modal payload into env vars for ``run_pipeline.py``.

    Mirrors the MPNN/AF2 env-var contract so ``run_pipeline.py`` stays
    provider-agnostic. Unlike MPNN, Boltz-2 also needs
    ``upload_urls_endpoint`` because each per-design PDB is uploaded
    individually as the fold completes — partial-results streaming.
    """
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
        "JOB_TIER": str(payload.get("job_tier", "standalone")),
    }
    return env


def _merged_environment(payload: dict) -> dict[str, str]:
    """Merge run-specific env vars into the container's existing env."""
    merged = dict(os.environ)
    merged.update(_build_run_env(payload))
    return merged


# Boltz-2's model cache, ~7.4 GiB. boltz 2.2.1's ``download_boltz2`` fetches
# ~5.7 GiB (boltz2_conf.ckpt 2.1 GiB, boltz2_aff.ckpt 1.9 GiB, mols.tar
# 1.7 GiB) and unpacks mols.tar into mols/, another 1.7 GiB in 45,227 files
# that the top-level listing shows as a 311.9 KiB dir. Checked 2026-09-23
# with ``modal volume ls boltz2-weights`` (add ``mols`` to size the dir).
# Scratch apps this repo does not track also mount this Volume, so re-list
# before trusting these figures.
#
# The cache has been populated since 2026-05-29 (the three files' dates), so as
# of 2026-09-23 prod cold starts download none of it. ``download_boltz2`` skips
# each step whose output is already in its cache dir, and that dir is this
# Volume: ``tools/boltz2/run_pipeline.py::run_boltz`` passes no ``--cache`` and
# nothing in this repo or the base image sets ``$BOLTZ_CACHE``, so boltz's
# ``get_cache_path`` falls back to ``~/.boltz``. That is ``/root/.boltz``, as
# neither ``tools/boltz2/Dockerfile.modal`` nor its base image sets USER or
# ``$HOME``, and ``run_tool`` mounts this Volume there. If the Volume is ever
# emptied, the next cold start fetches the ~5.7 GiB and unpacks mols/ again:
# untimed, and not in the ``_MAX_SESSION_S`` arithmetic above.
weights = modal.Volume.from_name("boltz2-weights", create_if_missing=True)

# Raw run artefacts, keyed by job id. Deterministic naming means nothing new has
# to travel through the DB: the caller already knows the job id, and top-level
# return keys are ignored by the hub's ``_interpret_pipeline_return``, so this
# needs zero client changes.
raw = modal.Volume.from_name(_RAW_VOLUME, create_if_missing=True)

image = (
    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
)

app = modal.App("ranomics-boltz2-prod")


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    volumes={"/root/.boltz": weights, _RAW_MOUNT: raw},
)
def run_tool(payload: Any) -> dict:
    """Run one Boltz-2 session.

    Subprocess stdout/stderr stream live to Modal's function logs so
    failures are visible via ``modal app logs ranomics-boltz2-prod``
    without fetching the FunctionCall return.

    ``payload`` is annotated ``Any`` for the same reason MPNN's is —
    Modal CLI dict annotation introspection is finicky.
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

    result = subprocess.run(
        cmd,
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
        # Safety margin under the hard Modal timeout so the wrapper still
        # has time to read smoke_results.json.
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

    # Park the COMPLETE raw work tree, unconditionally — not gated on success, on
    # candidates, or on anything having been uploaded. A zero-candidate run ships
    # nothing today and is exactly the run whose tree you need. The tar goes on a
    # Volume rather than inline in the return dict because a big base64 blob flows
    # into the ``tool_jobs.result`` JSONB column and wedges the job in "running".
    # Best-effort: capture must never fail the run.
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
                # A commit race must not discard the path we already know: the
                # tar is on the Volume either way, and a caller told where to
                # look can find out. Silence cannot.
                print(f"[run_tool] raw.commit() raised: {exc}", flush=True)
            raw_info = {
                "raw_tgz_volume": _RAW_VOLUME,
                "raw_tgz_volume_path": dest,
            }
            print(
                f"[run_tool] raw tree parked at {dest} (volume {_RAW_VOLUME}, "
                f"{os.path.getsize(dest) / 1e6:.1f} MB)",
                flush=True,
            )
        else:
            print(f"[run_tool] no {_RAW_ARCHIVE} to park", flush=True)
    except Exception as exc:
        print(f"[run_tool] raw capture failed (non-fatal): {exc}", flush=True)

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
        **raw_info,
    }
