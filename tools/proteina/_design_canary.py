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


# One poll iteration is one nvidia-smi call capped at 10 s, and the poller
# always finishes an iteration after the stop flag is set, so the join must
# outlast it or the final sample is thrown away.
_VRAM_JOIN_TIMEOUT_S = 15


def _prealloc_disabled(env: dict | None) -> bool | None:
    """Was JAX preallocation OFF in the env the CHILD actually received?

    Derived, never asserted — see the twin in ``_hotspot_canary.py``.
    ``design_subprocess_env`` uses ``setdefault``, so an operator override puts
    preallocation back on while the code's intent is unchanged; a hardcoded
    flag would label that run as if the fix had been in force.
    """
    if not env:
        return None
    raw = env.get("XLA_PYTHON_CLIENT_PREALLOCATE")
    if raw is None:
        return None
    return str(raw).strip().lower() in ("false", "0", "no", "off")


def _poll_vram(
    stop: threading.Event, out: dict, child_env: dict | None = None,
) -> None:
    """Peak device VRAM across the design.

    SAMPLE FIRST, TEST THE STOP FLAG SECOND. `while not stop.is_set()` takes
    ZERO samples when the design finishes before this thread is first
    scheduled, and then reports peak 0 — which reads as "used no VRAM" rather
    than "was never measured". A shard that dies early is precisely the one
    whose memory you want to see. Same shape as ``_hotspot_canary._poll_vram``,
    which this file lagged behind on.
    """
    peak = 0
    while True:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.strip().splitlines():
                peak = max(peak, int(line.strip()))
        except Exception:
            pass
        if stop.is_set():
            break
        stop.wait(5)
    out["peak_vram_mb"] = peak
    out["vram_poll_interval_s"] = 5
    out["vram_prealloc_disabled"] = _prealloc_disabled(child_env)
    out["vram_poll_complete"] = True


def _scored_design_counts(rp, inference) -> dict:
    """``{n_scored_designs, n_reward_rows}`` from PRODUCTION's own parser.

    ``run_pipeline`` counts designs whose ``total_reward`` is not None and fails
    ONLY when a non-zero exit left that count at zero — and the reward CSV is
    written by the GENERATE stage, so a late evaluate/analyze crash routinely
    leaves a fully scored table behind. That is the whole reason this canary may
    not judge a shard on its exit code alone.

    ``rp.parse_designs`` is CALLED rather than re-derived: the number is
    compared against production's delivery rule, and a second implementation of
    that rule is a second thing to drift.

    Never raises. A diagnostic that can kill the shard it is describing would be
    the same defect wearing a different hat. On any error both counts are None,
    which the entrypoint reads as a failure — an unproven delivery is not a
    delivery. (Twin of ``_hotspot_canary._scored_design_counts``; duplicated
    rather than imported for the reason given at the top of this file.)
    """
    try:
        designs = rp.parse_designs(inference)
        return {
            "n_scored_designs": sum(
                1 for d in designs if d.get("total_reward") is not None),
            "n_reward_rows": len(designs),
        }
    except Exception as exc:  # noqa: BLE001 — never fail a shard over a count
        print(f"[canary] could not count scored designs: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return {"n_scored_designs": None, "n_reward_rows": None}


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
    # env=, and the SAME env the poller is told about. Without it the child
    # inherits JAX's default PREALLOCATE=true, reserves 0.75 x 81,920 =
    # 61,440 MB on its first JAX op regardless of target size, and every number
    # this harness reports is that constant — which is exactly how the two
    # existing ~67.5 GB readings came to be unusable. run_pipeline.run_streaming
    # was fixed for production; this file, whose docstring says its VRAM feeds
    # the 40-vs-80GB decision, was left on the old path and would have produced
    # the same discredited number today.
    child_env = rp.design_subprocess_env()
    poller = threading.Thread(
        target=_poll_vram, args=(stop, vram),
        kwargs={"child_env": child_env}, daemon=True,
    )
    poller.start()

    t0 = time.time()
    try:
        rc = subprocess.run(
            cmd, cwd=str(work_dir), stdout=sys.stdout, stderr=sys.stderr,
            timeout=3600,  # bound a hang to ~1h (~$3.7) instead of the 2h Modal cap
            env=child_env,
        ).returncode
    except subprocess.TimeoutExpired:
        rc = 124
        print("[canary] TIMEOUT: `complexa design` exceeded 3600s — killed", flush=True)
    runtime_s = int(time.time() - t0)
    stop.set()
    poller.join(timeout=_VRAM_JOIN_TIMEOUT_S)
    if not vram.get("vram_poll_complete"):
        print(
            f"[canary] WARNING: VRAM poller did not finish within "
            f"{_VRAM_JOIN_TIMEOUT_S}s; peak_vram_mb is incomplete.",
            flush=True,
        )

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
    # `_hub_input` carries the archive copies of the input — target.pdb AND
    # upload.pdb — so the basename check alone would count upload.pdb as a
    # design. Inert while this canary never creates that directory; mirrors
    # find_pdb_for's _hub_input clause specifically, NOT its whole exclusion
    # list (that one also drops the basename `target_input`, which cannot match
    # a *.pdb glob anyway).
    all_pdbs = [p for p in sorted(glob.glob(str(inference / "**/*.pdb"), recursive=True))
                if "filtered_out_samples" not in p
                and "_hub_input" not in Path(p).parts
                and Path(p).name not in ("target.pdb",)]

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
        # WOULD PRODUCTION HAVE DELIVERED THIS RUN? Same divergence this file's
        # sibling ``_hotspot_canary`` carried: the local entrypoint below judged
        # the shard on ``exit_code`` alone, which is stricter than production
        # and condemns runs production ships. See ``_scored_design_counts``.
        **_scored_design_counts(rp, inference),
        "peak_vram_mb": vram.get("peak_vram_mb"),
        # Provenance for the number above. Device-wide nvidia-smi cannot tell a
        # JAX reservation from demand, so a peak taken with preallocation ON is
        # not comparable to one taken with it OFF. Derived from the child's env.
        "vram_prealloc_disabled": vram.get("vram_prealloc_disabled"),
        "vram_poll_interval_s": vram.get("vram_poll_interval_s"),
        "vram_poll_complete": bool(vram.get("vram_poll_complete")),
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
    # THREE STATES, NOT TWO. ``exit_code != 0 -> FAILED`` was stricter than
    # production and would condemn a run production ships: 8 designs, 8 reward
    # rows, all scored, then a crash in evaluate. The same reading, in the
    # sibling harness, nearly cancelled a measurement campaign.
    #
    # CLEAN (exit 0) / DEGRADED (non-zero, but designs came back scored, so
    # production delivers) / FAILED (non-zero with nothing scored, or no count
    # to judge by). DEGRADED exits 0 because production would have shipped it —
    # and prints in full, because the non-zero exit is still a real defect that
    # needs its own diagnosis, just not a verdict on the run's output.
    #
    # The same THREE STATES as ``_canary_scoring.shard_delivery``, written out
    # rather than imported for the reason given at the top of this file: this
    # harness deliberately depends on no other harness's private module. Not the
    # byte-identical rule, and saying so matters more than the tidier sentence:
    # ``shard_delivery`` coerces the count with ``int()``, so a string "3" or a
    # float 2.5 would read there as a delivery and here as a failure. Both
    # counts are written by ``_scored_design_counts`` above, which emits an int
    # or None and nothing else, so the divergence is unreachable — but it is a
    # divergence, and "the same rule" would have been a claim nobody checked.
    rc = res.get("exit_code")
    scored = res.get("n_scored_designs")
    try:
        rc_int = int(rc)
    except (TypeError, ValueError):
        # A shard that reported no usable exit code did not report a success
        # either. `if rc == 0: return` alone would have fallen through to the
        # DELIVERED-DEGRADED line and printed "exited None".
        print(f"[canary] SHARD FAILED: no usable exit code ({rc!r}) — "
              "the shard cannot be interpreted at all")
        sys.exit(1)
    if rc_int == 0:
        return
    if not isinstance(scored, int) or isinstance(scored, bool) or scored <= 0:
        print(f"[canary] SHARD FAILED: exited {rc_int} with "
              f"{'no scored designs' if scored == 0 else f'no usable scored count ({scored!r})'}"
              " — production fails a run on exactly this reading")
        sys.exit(1)
    print(f"[canary] SHARD DELIVERED-DEGRADED: exited {rc_int}, but {scored} of "
          f"{res.get('n_reward_rows')} reward rows are fully scored. "
          "run_pipeline only fails when a non-zero exit left nothing scored, "
          "so production would have SHIPPED these designs. The non-zero exit "
          "is still a real defect — read the stage-log tails above.")
