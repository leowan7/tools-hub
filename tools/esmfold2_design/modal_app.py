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

Raw capture: ``run_pipeline.py`` tars its COMPLETE work tree to
``/tmp/raw_archive.tgz`` before the container dies; ``_park_raw_archive``
moves it onto the ``ranomics-esmfold2-design-raw`` Volume under a
deterministic name and reports where it landed via the top-level
``raw_tgz_volume`` / ``raw_tgz_volume_path`` keys.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
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
# 60 min ceiling per H100 worker — covers the worst-case scfv preset
# with batch_size=6 plus weight-load tail latency on a cold container.
_MAX_SESSION_S = 60 * 60
# Orchestrator waits for the slowest child; 75 min gives a 15 min margin
# over the worker timeout to absorb spawn overhead + aggregation.
_ORCHESTRATOR_TIMEOUT_S = 75 * 60
_PYTHON = "python3"

# Raw run artifacts get their OWN Volume, never the weights Volume: a weights
# cache exists to make cold starts cheap and has no eviction path, so parking
# GB-scale run output in it bloats the very thing it is for and leaves no way to
# reap raw without touching weights.
#
# A Volume rather than an inline return or an upload — all three were checked.
# gpu/modal_client.py rejects a non-dict return outright and pipes dict values
# into the tool_jobs.result JSONB column (inline b64 has already broken that
# write, which is why shared/jobs.py carries the scar tissue for it), and
# Supabase Storage caps objects at 20 MB with no gzip/tar in its MIME allowlist.
# Naming the tar deterministically means nothing new has to travel through the
# DB at all — _interpret_pipeline_return ignores unknown top-level keys, so this
# needs zero client changes.
_RAW_MOUNT = "/raw"
_RAW_VOLUME = f"ranomics-{_TOOL}-raw"
# Fixed path run_pipeline.py tars its work tree to (RAW_ARCHIVE_PATH there).
_RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"


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

raw_volume = modal.Volume.from_name(_RAW_VOLUME, create_if_missing=True)


def _raw_stem(prefix: Any, job_id: Any) -> str:
    """Deterministic, filesystem-safe archive name for one container's tree.

    ``pdb_prefix`` is the mechanism this tool already uses to stop fan-out
    children colliding in the per-job Storage namespace (``seed{N}_``), so reuse
    it here for exactly the same reason: every child of one job shares a job_id,
    and without the prefix N seeds would race to write the same ``<job_id>.tgz``
    and N-1 trees would be lost silently. Single-seed runs get an empty prefix
    and land at ``<job_id>.tgz``.

    Deterministic on purpose — the caller reconstructs the path from the job id
    and seed, which is what lets the archive be found even when the return dict
    never made it home. No time-based fallback for that reason.
    """
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{prefix or ''}{job_id or ''}").strip("_")
    return stem or "unknown"


def _park_raw_archive(payload: Any) -> dict[str, str]:
    """Move run_pipeline.py's raw archive onto the raw Volume. Never raises.

    Returns the keys telling the caller where the tar landed, or ``{}`` if there
    was nothing to park. Capture must never fail the run: a tool that crashed
    before writing output is exactly when the tree matters most, so problems are
    printed, not raised.
    """
    try:
        if not os.path.isfile(_RAW_ARCHIVE_PATH):
            print(
                f"[raw] no archive at {_RAW_ARCHIVE_PATH} — pipeline died before "
                f"its finally ran (hard timeout / OOM kill?)",
                flush=True,
            )
            return {}
        spec = (payload or {}).get("job_spec", {}) or {}
        stem = _raw_stem(spec.get("pdb_prefix", ""), (payload or {}).get("job_id", ""))
        os.makedirs(_RAW_MOUNT, exist_ok=True)
        dest = os.path.join(_RAW_MOUNT, f"{stem}.tgz")
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
        # Top-level keys are ignored by _interpret_pipeline_return, so this needs
        # zero client changes; they exist so the caller can fetch the tar itself
        # rather than have a human run a `modal volume get` line, which has a
        # demonstrated ~0% follow-through rate.
        return {"raw_tgz_volume": _RAW_VOLUME, "raw_tgz_volume_path": dest}
    except Exception as exc:  # noqa: BLE001 — capture is best-effort by design
        print(
            f"[raw] parking failed (non-fatal): {type(exc).__name__}: {exc}",
            flush=True,
        )
        return {}

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

# Orchestrator runs CPU-only (no GPU, no weights mount) — its job is to
# spawn N child workers, wait, and aggregate. debian_slim is enough; the
# orchestrator only needs stdlib + the modal package (preinstalled by the
# Modal runtime).
orchestrator_image = modal.Image.debian_slim(python_version="3.12")

app = modal.App("ranomics-esmfold2-design-prod")


@app.function(
    image=image,
    gpu=_GPU,
    timeout=_MAX_SESSION_S,
    cpu=16,
    memory=10 * 1024,
    volumes={"/models": weights, _RAW_MOUNT: raw_volume},
)
def _run_one_seed(payload: Any) -> dict:
    """Worker — runs one ESMFold2-design gradient pass at one seed.

    Internal entry point used by the ``run_tool`` orchestrator. Body is
    the previous monolithic ``run_tool`` implementation, unchanged: read
    JOB_PAYLOAD env, subprocess into ``run_pipeline.py``, load the resulting
    ``/tmp/smoke_results.json``, commit the weights Volume, return.

    Subprocess stdout/stderr stream live to Modal's function logs so
    failures are visible via ``modal app logs ranomics-esmfold2-design-prod``
    without fetching the FunctionCall return.
    """
    import sys

    env = _merged_environment(payload)
    cmd = [_PYTHON, "-u", _RUN_PIPELINE_REMOTE]
    job_spec = (payload or {}).get("job_spec", {}) or {}

    print(f"[_run_one_seed] spawning: {' '.join(cmd)}", flush=True)
    print(
        f"[_run_one_seed] JOB_ID={env.get('JOB_ID')} TIER={env.get('JOB_TIER')} "
        f"seed={job_spec.get('seed')} pdb_prefix={job_spec.get('pdb_prefix', '')!r} "
        f"WEBHOOK={env.get('WEBHOOK_URL')}",
        flush=True,
    )

    # Clear any stale smoke_results.json from a prior invocation on a warm Modal
    # container. Without this, if this seed's run_pipeline.py dies before writing its
    # own file (early import error, OOM kill, SIGKILL, uncaught exception), the read
    # below picks up the PREVIOUS invocation's result and reports it as this seed's.
    # Sharper here than elsewhere: ``run_tool`` fans out N children per job, so the
    # container this seed inherits is most often one a SIBLING seed just ran on — a
    # dead seed would be aggregated as a real one, and for n_seeds == 1 the worker
    # dict is returned to the hub verbatim, where
    # ``gpu.modal_client._interpret_pipeline_return()`` marks the job succeeded off
    # smoke["status"] alone with no exit_code gate. Codex P1 (colabfold).
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(
            f"[_run_one_seed] could not remove stale smoke_results.json: {exc}",
            flush=True,
        )

    # Warm containers are reused: a leftover raw archive from a prior job would be parked
    # under THIS job's id. Clear it so we only ever park a tar this run actually wrote.
    try:
        os.remove(_RAW_ARCHIVE_PATH)
    except OSError:
        pass

    try:
        result = subprocess.run(
            cmd,
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr,
            timeout=max(60, _MAX_SESSION_S - 30),
        )
    finally:
        # Unconditional, and in a finally: not gated on exit_code, on
        # smoke_result, or on what got uploaded. subprocess.run raises
        # TimeoutExpired on a 60 min H100 run — the path where this function
        # never returns a dict at all, so the raw pointer never reaches the hub
        # and the deterministic name is the only way back to the tree. Parking
        # it there anyway is the difference between a recoverable timeout and a
        # re-paid H100 hour.
        raw_keys = _park_raw_archive(payload)

    print(f"[_run_one_seed] subprocess exited: {result.returncode}", flush=True)

    smoke_result: dict | None = None
    try:
        with open("/tmp/smoke_results.json") as fh:
            smoke_result = json.load(fh)
        print(
            f"[_run_one_seed] loaded smoke_results.json: "
            f"status={smoke_result.get('status')} "
            f"completed={smoke_result.get('designs_completed')}/"
            f"{smoke_result.get('designs_total')}",
            flush=True,
        )
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[_run_one_seed] failed to read smoke_results.json: {exc}", flush=True)

    # Persist the weights Volume so any newly-downloaded model files survive
    # the container teardown. Idempotent if nothing was written.
    try:
        weights.commit()
    except Exception as exc:
        print(f"[_run_one_seed] weights.commit() raised: {exc}", flush=True)

    return {
        "exit_code": result.returncode,
        "stdout_tail": "",
        "stderr_tail": "",
        "provider_job_id": payload.get("job_id", ""),
        "smoke_result": smoke_result,
        **raw_keys,
    }


@app.function(
    image=orchestrator_image,
    cpu=2,
    memory=1024,
    timeout=_ORCHESTRATOR_TIMEOUT_S,
)
def run_tool(payload: Any) -> dict:
    """Orchestrator entrypoint — fans out N parallel seeds, aggregates.

    Reads ``payload["job_spec"]["n_seeds"]`` (default 1). The seed sweep
    runs over ``[seed, seed + n_seeds)``. Each child gets its own
    ``pdb_prefix=seed{N}_`` so PDB filenames don't collide in the per-job
    Storage namespace. Children run in parallel on H100s; the orchestrator
    blocks on each call's ``.get()`` until all finish, then merges their
    ``candidates`` and ``designs`` arrays, globally re-ranks by ipTM, and
    returns one umbrella smoke_result. The shape matches what tools-hub's
    ``_interpret_pipeline_return`` already consumes — fan-out is invisible
    to the hub.

    For ``n_seeds == 1`` this is a thin pass-through with one child spawn
    (~5s orchestrator overhead vs going direct to ``_run_one_seed``).
    """
    job_spec = (payload or {}).get("job_spec", {}) or {}
    n_seeds = max(1, int(job_spec.get("n_seeds") or 1))
    start_seed = int(job_spec.get("seed") or 0)
    job_id = str(payload.get("job_id", "")) if isinstance(payload, dict) else ""

    print(
        f"[run_tool] fanout: n_seeds={n_seeds} start_seed={start_seed} "
        f"job_id={job_id}",
        flush=True,
    )

    if n_seeds == 1:
        cp = copy.deepcopy(payload) if isinstance(payload, dict) else payload
        if isinstance(cp, dict):
            cp.setdefault("job_spec", {})
            cp["job_spec"].setdefault("pdb_prefix", "")
            cp["job_spec"]["n_seeds"] = 1
        # ``.remote()`` is the synchronous equivalent of ``.spawn().get()``.
        # Returns the worker's full return dict; we pass it through unchanged.
        return _run_one_seed.remote(cp)

    children: list[tuple[int, Any]] = []
    for i in range(n_seeds):
        seed = start_seed + i
        cp = copy.deepcopy(payload) if isinstance(payload, dict) else {}
        cp.setdefault("job_spec", {})
        cp["job_spec"]["seed"] = seed
        cp["job_spec"]["pdb_prefix"] = f"seed{seed}_"
        cp["job_spec"]["n_seeds"] = 1
        call = _run_one_seed.spawn(cp)
        children.append((seed, call))
        print(
            f"[run_tool] spawned child seed={seed} fc_id="
            f"{getattr(call, 'object_id', '?')}",
            flush=True,
        )

    print(
        f"[run_tool] waiting for {len(children)} children to complete",
        flush=True,
    )

    successes: list[tuple[int, dict]] = []
    failures: list[tuple[int, str]] = []
    for seed, call in children:
        try:
            r = call.get()
            successes.append((seed, r))
            print(f"[run_tool] child seed={seed} completed", flush=True)
        except Exception as exc:
            failures.append((seed, str(exc)))
            print(f"[run_tool] child seed={seed} FAILED: {exc}", flush=True)

    print(
        f"[run_tool] aggregation: {len(successes)} succeeded, "
        f"{len(failures)} failed",
        flush=True,
    )
    return _aggregate(successes, failures, payload)


def _aggregate(
    successes: list[tuple[int, dict]],
    failures: list[tuple[int, str]],
    orig_payload: dict,
) -> dict:
    """Merge N child returns into one umbrella ``run_tool`` return dict.

    Concats ``candidates`` and ``designs`` across children, globally
    re-ranks by ipTM, tags each entry with its source seed so the CSV
    exporter and UI can attribute hits. Sums failure counts. If every
    child failed, returns a FAILED umbrella so tools-hub marks the job
    failed and surfaces the first few error strings to the user.

    Also forwards the children's raw-archive pointers. One container per seed
    means one archive per seed, so the umbrella carries the plural
    ``raw_tgz_volume_paths``: collapsing N trees into the scalar
    ``raw_tgz_volume_path`` that the single-seed path returns would silently
    point at one of them and drop the rest.
    """
    tier = ""
    if isinstance(orig_payload, dict):
        tier = str(orig_payload.get("tier") or "")
    umbrella_provider_id = ""
    if isinstance(orig_payload, dict):
        umbrella_provider_id = str(orig_payload.get("job_id") or "")

    if not successes:
        err = "; ".join(f"seed {s}: {e}" for s, e in failures[:5])
        return {
            "exit_code": 1,
            "stdout_tail": "",
            "stderr_tail": "",
            "provider_job_id": umbrella_provider_id,
            "smoke_result": {
                "status": "FAILED",
                "tier": tier,
                "error": f"All {len(failures)} seeds failed. {err}",
                "designs_total": 0,
                "designs_completed": 0,
                "n_failures": len(failures),
                "designs": [],
                "candidates": [],
                "runtime_seconds": 0,
                "n_seeds": len(failures),
                "seeds_succeeded": 0,
                "seeds_failed": len(failures),
                "failure_notes": [f"seed {s}: {e}" for s, e in failures],
                "provider_job_id": umbrella_provider_id,
            },
            "raw_tgz_volume": _RAW_VOLUME,
            # Every child raised, so none of their return dicts survived and no
            # pointer was confirmed. The names are deterministic, so hand over
            # the ones to look for: a child that parked its tree before dying is
            # recoverable, and one killed outright (OOM, preemption) simply will
            # not be there. Best-effort names beat nothing on the exact path
            # where the trees are worth the most.
            "raw_tgz_volume_paths": [
                f"{_RAW_MOUNT}/{_raw_stem(f'seed{s}_', umbrella_provider_id)}.tgz"
                for s, _ in failures
            ],
        }

    template = (successes[0][1].get("smoke_result") or {}) if isinstance(
        successes[0][1], dict
    ) else {}
    preset = template.get("preset", "")
    target_name = template.get("target_name")
    target_label = template.get("target_label", "")
    binder_name = template.get("binder_name", "")
    binder_label = template.get("binder_label", "")
    is_antibody = bool(template.get("is_antibody", False))
    use_scaling_critics = bool(template.get("use_scaling_critics", False))

    all_candidates: list[dict] = []
    all_designs: list[dict] = []
    raw_paths: list[str] = []
    # Each seed runs in its OWN container (see spawn loop in run_tool), so the
    # GPU time the account is billed for is the SUM of the children, not the
    # max. ``runtime_seconds`` feeds ``gpu_seconds_used`` at the billing seam
    # (gpu/modal_client.py), so it MUST be the sum or a multi-seed run is
    # under-charged by up to Nx. ``wall_clock_seconds`` keeps the parallel
    # elapsed time (~max) for honest UI display.
    total_gpu_seconds = 0
    wall_clock_seconds = 0
    designs_total = 0
    designs_completed = 0
    inner_failures = 0
    best_iptm: float | None = None
    best_seq: str | None = None

    for seed, child_ret in successes:
        smoke = (child_ret or {}).get("smoke_result") or {}
        raw_path = (child_ret or {}).get("raw_tgz_volume_path")
        if raw_path:
            raw_paths.append(str(raw_path))
        for cand in smoke.get("candidates", []) or []:
            tagged = dict(cand)
            tagged["seed"] = seed
            scores = dict(tagged.get("scores") or {})
            scores["seed"] = seed
            tagged["scores"] = scores
            all_candidates.append(tagged)
            iptm = scores.get("ipTM")
            if isinstance(iptm, (int, float)):
                if best_iptm is None or iptm > best_iptm:
                    best_iptm = float(iptm)
                    best_seq = tagged.get("sequence") or tagged.get(
                        "designed_sequence"
                    )
        for d in smoke.get("designs", []) or []:
            tagged = dict(d)
            tagged["seed"] = seed
            all_designs.append(tagged)
        designs_total += int(smoke.get("designs_total") or 0)
        designs_completed += int(smoke.get("designs_completed") or 0)
        inner_failures += int(smoke.get("n_failures") or 0)
        child_runtime = int(smoke.get("runtime_seconds") or 0)
        total_gpu_seconds += child_runtime
        wall_clock_seconds = max(wall_clock_seconds, child_runtime)

    def _cand_sort_key(c: dict) -> float:
        iptm = (c.get("scores") or {}).get("ipTM")
        return -1.0 if not isinstance(iptm, (int, float)) else -float(iptm)

    def _design_sort_key(d: dict) -> float:
        iptm = d.get("iptm")
        return -1.0 if not isinstance(iptm, (int, float)) else -float(iptm)

    all_candidates.sort(key=_cand_sort_key)
    all_designs.sort(key=_design_sort_key)

    for rank, c in enumerate(all_candidates):
        c["rank"] = rank
        c["name"] = f"seed{c.get('seed', 0)}_rank{rank}"
    for rank, d in enumerate(all_designs):
        d["rank"] = rank

    n_failures_total = inner_failures + len(failures)

    smoke_result = {
        "status": "COMPLETED" if designs_completed > 0 else "FAILED",
        "tier": tier,
        "preset": preset,
        "is_antibody": is_antibody,
        "target_name": target_name,
        "target_label": target_label,
        "binder_name": binder_name,
        "binder_label": binder_label,
        "designs_total": designs_total,
        "designs_completed": designs_completed,
        "n_failures": n_failures_total,
        "use_scaling_critics": use_scaling_critics,
        "best_sequence": best_seq,
        "designs": all_designs,
        "candidates": all_candidates,
        "runtime_seconds": total_gpu_seconds,
        "wall_clock_seconds": wall_clock_seconds,
        "n_seeds": len(successes) + len(failures),
        "seeds_succeeded": len(successes),
        "seeds_failed": len(failures),
        "failure_notes": [f"seed {s}: {e}" for s, e in failures],
        "provider_job_id": umbrella_provider_id,
    }
    if designs_completed == 0:
        smoke_result["error"] = "All seeds returned zero designs"

    out = {
        "exit_code": 0 if designs_completed > 0 else 1,
        "stdout_tail": "",
        "stderr_tail": "",
        "provider_job_id": umbrella_provider_id,
        "smoke_result": smoke_result,
    }
    if raw_paths:
        out["raw_tgz_volume"] = _RAW_VOLUME
        out["raw_tgz_volume_paths"] = raw_paths
    return out
