"""P-2 / P-3 design-shard benchmark canary for Proteina-Complexa.

    modal run tools/proteina/_design_canary.py           # protein_binder / 02_PDL1
    modal run tools/proteina/_design_canary.py --preset ligand_binder \
        --config search_ligand_binder_local_pipeline --task 39_7V11_LIGAND

Runs ONE real search shard on a GPU using the SAME image + seeded Volumes as the
prod app, but bypasses the upload/webhook leg (which needs the tools-hub server).
It reuses run_pipeline.build_design_cmd + shard_seed so the invocation is
byte-identical to production, then inspects ./inference to report exactly what
the P-2/P-3 canary must measure:

  * GPU-seconds (wall time of `complexa design`) -> expected_gpu_seconds / PRESET_CAPS
  * peak VRAM (nvidia-smi poll) -> the 40-vs-80GB decision
  * design count + the reward-CSV column NAMES + a couple sample rows -> confirms
    the output contract (validates/repairs run_pipeline._SCORE_COLUMNS) and that
    the shard actually produced scored designs.

Nothing is uploaded, billed through the wallet, or written to prod state. This
is a diagnostic; delete before flag-on (it is not imported by the prod app).
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import modal

_TOOL = "proteina"
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/proteina/run_pipeline.py"
_GPU = "A100-80GB"
_MAX_SESSION_S = 7200

# Same Dockerfile => same image hash => reuses the already-built cached image (no
# rebuild). Same Volumes by name => the seeded weights/rewards.
image = (
    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
)
weights = modal.Volume.from_name("proteina-weights")
rewards = modal.Volume.from_name("proteina-rewards")

app = modal.App("ranomics-proteina-canary")


def _poll_vram(stop: threading.Event, out: dict) -> None:
    peak = 0
    while not stop.is_set():
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.strip().splitlines():
                peak = max(peak, int(line.strip()))
        except Exception:
            pass
        stop.wait(5)
    out["peak_vram_mb"] = peak


@app.function(
    image=image, gpu=_GPU, timeout=_MAX_SESSION_S,
    volumes={"/opt/proteina/ckpts": weights, "/opt/proteina/rewards": rewards},
)
def run_design_canary(preset: str, config_name: str, task_name: str,
                      nsamples: int, replicas: int) -> dict:
    sys.path.insert(0, "/opt/proteina")
    import importlib.util
    spec = importlib.util.spec_from_file_location("rp", _RUN_PIPELINE_REMOTE)
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)

    work_dir = Path("/opt/proteina")
    inference = work_dir / "inference"
    import shutil
    shutil.rmtree(inference, ignore_errors=True)

    seed = rp.shard_seed("canary-1")
    cmd = rp.build_design_cmd(
        config_name=config_name, task_name=task_name, seed=seed,
        nsamples=nsamples, replicas=replicas, nsteps=None,
        run_name="canary_1", rf3_on=rp._rf3_enabled(),
    )
    print(f"[canary] preset={preset} rf3_on={rp._rf3_enabled()}", flush=True)
    print(f"[canary] cmd: {' '.join(cmd)}", flush=True)

    vram: dict = {}
    stop = threading.Event()
    poller = threading.Thread(target=_poll_vram, args=(stop, vram), daemon=True)
    poller.start()

    t0 = time.time()
    try:
        rc = subprocess.run(
            cmd, cwd=str(work_dir), stdout=sys.stdout, stderr=sys.stderr,
            timeout=3600,  # bound a hang to ~1h (~$3.7) instead of the 2h Modal cap
        ).returncode
    except subprocess.TimeoutExpired:
        rc = 124
        print("[canary] TIMEOUT: `complexa design` exceeded 3600s — killed", flush=True)
    runtime_s = int(time.time() - t0)
    stop.set()
    poller.join(timeout=10)

    # Inspect the outputs (reward CSV columns + design count).
    csvs = {}
    for p in sorted(glob.glob(str(inference / "**/*.csv"), recursive=True)):
        try:
            import csv as _csv
            with open(p, newline="") as fh:
                reader = _csv.DictReader(fh)
                rows = list(reader)
            csvs[p] = {
                "columns": reader.fieldnames,
                "nrows": len(rows),
                "sample_rows": rows[:2],
            }
        except Exception as exc:
            csvs[p] = {"error": str(exc)}
    all_pdbs = [p for p in sorted(glob.glob(str(inference / "**/*.pdb"), recursive=True))
                if "filtered_out_samples" not in p and Path(p).name not in ("target.pdb",)]

    # Dump stage-log tails: each stage's real error goes to a per-stage log FILE
    # (generate.log / evaluate.log / ...), not to stdout. On a nonzero rc, surface
    # the tail of any stage log that mentions an error so we can see the actual
    # failure without re-running.
    if rc != 0:
        print("[canary] ===== stage log tails (rc != 0) =====", flush=True)
        for logp in sorted(glob.glob("/opt/proteina/logs/**/*.log", recursive=True)):
            try:
                with open(logp, errors="replace") as fh:
                    lines = fh.readlines()
                tail = "".join(lines[-45:])
                if any(k in tail for k in ("Error", "Traceback", "Exception", "error", "raise")):
                    print(f"[canary] --- {logp} (tail) ---\n{tail}\n", flush=True)
            except Exception:
                pass

    result = {
        "preset": preset, "task_name": task_name,
        "exit_code": rc, "runtime_s": runtime_s,
        "peak_vram_mb": vram.get("peak_vram_mb"),
        "designs_expected": nsamples * replicas,
        "n_pdbs": len(all_pdbs),
        "pdb_sample": [Path(p).name for p in all_pdbs[:5]],
        "csv_files": csvs,
    }
    print("[canary] RESULT:\n" + json.dumps(result, indent=2, default=str), flush=True)
    return result


@app.local_entrypoint()
def main(preset: str = "protein_binder",
         config: str = "search_binder_local_pipeline",
         task: str = "02_PDL1",
         nsamples: int = 4, replicas: int = 2) -> None:
    res = run_design_canary.remote(preset, config, task, nsamples, replicas)
    print("\n================ CANARY SUMMARY ================", flush=True)
    print(json.dumps({k: v for k, v in res.items() if k != "csv_files"}, indent=2, default=str))
    print("--- reward CSV columns ---")
    for path, info in (res.get("csv_files") or {}).items():
        print(f"  {path}")
        print(f"    columns: {info.get('columns')}")
        print(f"    nrows:   {info.get('nrows')}")
    if res.get("exit_code") != 0:
        print("[canary] SHARD FAILED (nonzero exit)")
        sys.exit(1)
