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

# ---------------------------------------------------------------------------
# The console, made incapable of killing the run
# ---------------------------------------------------------------------------
#
# BEFORE ``import modal``, AND BEFORE ANY OTHER STATEMENT THAT COULD PRINT.
# Container output reaches this process through modal's log pump, and the
# proteina container prints "  <check mark> ...", "  <round pushpin> ..." and
# box-drawing characters — this file additionally pipes the whole of `complexa
# design`'s stdout/stderr through it. On a Windows cp1252 console that write
# raises ``UnicodeEncodeError: 'charmap' codec can't encode character '✓'``
# and kills the LOCAL entrypoint, while the A100 carries on billing to
# completion or to _MAX_SESSION_S = 7200 s. That is what killed
# ``_hotspot_canary --phase 0`` on 2026-08-04; here it would throw away a real
# design shard AND the measurement it was bought for.
#
# ``_harden_stream`` mutates the stream's error handler IN PLACE and returns
# the SAME object, which is the point: modal's log pump, rich's renderer and
# the interpreter's own traceback printer each captured ``sys.stdout`` when they
# started, so REPLACING ``sys.stdout`` would leave every one of them writing to
# the strict original. Returning the same object also matters INSIDE the
# container, where ``run_design_canary`` hands ``sys.stdout`` straight to
# ``subprocess.run`` and needs a real file descriptor (the ``_SafeStream``
# fallback delegates ``fileno`` for the same reason). NOT
# ``PYTHONIOENCODING=utf-8``: that works, and an operator forgets it exactly
# once.
#
# DUPLICATED FROM ``_canary_scoring.py`` RATHER THAN IMPORTED, deliberately.
# It is a sibling file and ``_hotspot_canary`` loads it by path, but doing that
# here would make this harness depend on another harness's private module at
# module-import time, before ``modal`` is even imported; and importing it as
# ``tools.proteina._canary_scoring`` drags the web-tier adapter in through
# ``tools/proteina/__init__.py``. ~50 stateless stdlib lines is the cheaper
# cost. Canonical copy and its tests: ``_canary_scoring.py`` and
# ``tests/test_proteina_canary.py``.

# ``backslashreplace`` rather than ``replace``: the operator needs to be able to
# tell WHICH character could not be rendered.
CONSOLE_ERRORS = "backslashreplace"


def _safe_text(value, encoding=None, errors=CONSOLE_ERRORS):
    """``value`` rendered so that encoding it to ``encoding`` cannot raise.

    Lossless when the console can carry the text; ``None``/unknown encodings
    degrade to ASCII, which every console can take.
    """
    text = value if isinstance(value, str) else str(value)
    for candidate in (encoding, "ascii"):
        if not candidate:
            continue
        try:
            return text.encode(candidate, errors).decode(candidate, "replace")
        except (LookupError, UnicodeError, TypeError, ValueError):
            continue
    return text.encode("ascii", "backslashreplace").decode("ascii")


class _SafeStream:
    """Delegating proxy whose ``write`` cannot raise ``UnicodeEncodeError``.

    The FALLBACK only. Everything except ``write`` is delegated, so ``fileno``,
    ``encoding``, ``isatty`` and ``buffer`` keep working — ``run_design_canary``
    passes ``sys.stdout`` to ``subprocess.run``, which needs a real fd.
    """

    def __init__(self, stream, errors=CONSOLE_ERRORS):
        object.__setattr__(self, "_stream", stream)
        object.__setattr__(self, "_errors", errors)

    def write(self, text):
        stream = object.__getattribute__(self, "_stream")
        try:
            return stream.write(text)
        except UnicodeEncodeError:
            return stream.write(_safe_text(
                text, getattr(stream, "encoding", None),
                object.__getattribute__(self, "_errors")))

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_stream"), name)


def _harden_stream(stream, errors=CONSOLE_ERRORS):
    """The stream to use in place of ``stream``, unable to raise on an
    unencodable character.

    Returns the SAME object whenever it could be reconfigured. ``None`` for
    ``None``, and the original for a stream with no encoding to fail at
    (``io.StringIO``, pytest's capture) — wrapping something that cannot raise
    only adds a layer between the caller and a real file descriptor.
    """
    if stream is None:
        return None
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors=errors)
            return stream
        except (ValueError, OSError, TypeError, AttributeError, LookupError):
            pass
    if not getattr(stream, "encoding", None):
        return stream
    if isinstance(stream, _SafeStream):
        return stream
    return _SafeStream(stream, errors)


sys.stdout = _harden_stream(sys.stdout)
sys.stderr = _harden_stream(sys.stderr)


import modal  # noqa: E402 — imported only after the console cannot kill us

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
