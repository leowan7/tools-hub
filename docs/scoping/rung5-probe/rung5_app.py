"""Rung 5: does one RFD4 design's ipTM reproduce across three seeded folds?

The question, in Leo's words (2026-11-03): "I find it weird that the design
scores from 0.26 to 0.83 on the same designs, I don't believe that." The
0.263-0.830 band is real but is seven draws of one DECOY; the true cognate
reproduced to 0.0026 over the same runs (section 7C.8 of the scope doc). So the
open question is whether per-design spread is TIGHT for confident designs and
WIDE only for weak ones. This probe measures that on real RFD4 output: rung 4's
eight designs, each folded three times with distinct fixed seeds.

Three ``boltz predict`` processes, one per seed, eight records each.
``seed_everything`` is process-global, so replicates cannot be records in one
chunk - they have to be separate processes, which is why the fixed per-process
cost is paid three times.

Scope: ONE approved paid run, hard cap $1.00 total, no retry without a new
approval (Leo via the hub lead, 2026-11-03). ``timeout`` below is the only
thing that enforces the cap, so it is set from the cap and not from the
estimate. Every mode is dispatched explicitly: rung 4's probe recorded that a
fall-through ``--mode`` default turned a typo into an allocated A100 and a
billed run.

Reuses production ``tools/boltz2/run_pipeline.py`` for the YAML, the record ids
and the output globbing, so what folds here is what the hub would fold. The one
difference is deliberate and is the point of the probe: ``--seed`` is appended
to the ``boltz predict`` argv. Production ``run_boltz``
(``tools/boltz2/run_pipeline.py:444-473``) does not pass it.

Usage (repo venv by absolute path):
    modal run docs/scoping/rung5-probe/rung5_app.py --mode cli
    modal run docs/scoping/rung5-probe/rung5_app.py --mode gpu --inputs <json>
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import modal

_HERE = Path(__file__).resolve().parent

# Upward search for the repo's boltz2 tool dir, so this runs from any cwd.
# This module is re-imported INSIDE the container, where the script is mounted at
# /root with no repo above it, so the search must fall back to a path that is
# never dereferenced there rather than raise - a raise here crash-looped every
# container on 2026-11-03 with SystemExit before any function ran. The image
# builders below are only dereferenced locally, at build time.
_REPO_BOLTZ2 = next(
    (p / "tools" / "boltz2" for p in _HERE.parents
     if (p / "tools" / "boltz2" / "Dockerfile.modal").is_file()),
    _HERE / "tools" / "boltz2",
)

_SEEDS = (1, 2, 3)  # replicate k uses seed 1+k: fixed and derived, never random
_RUN_PIPELINE_REMOTE = "/opt/run_pipeline.py"

# The cap, not the estimate. A100-40GB bills $0.000714 raw GPU-second
# (``shared/wallet_estimates.py:73``), so 1150 s is $0.82 raw - inside the
# $1.00 approval with room for the container's own start-up seconds. The
# estimate is 516-774 s ($0.37-0.55) from the batching anchors in
# ``tools/boltz2/__init__.py``; if the real run needs more than 1150 s it is
# over budget and SHOULD die rather than keep billing.
_TIMEOUT_S = 1150
_GPU = "A100-40GB"  # same class as production boltz2 (tools/boltz2/modal_app.py:54)

image = (
    modal.Image.from_dockerfile(str(_REPO_BOLTZ2 / "Dockerfile.modal"), add_python=None)
    .add_local_file(str(_REPO_BOLTZ2 / "run_pipeline.py"), _RUN_PIPELINE_REMOTE, copy=True)
)
weights = modal.Volume.from_name("boltz2-weights", create_if_missing=False)

app = modal.App("ranomics-rfd4-rung5-probe")


def _load_pipeline():
    """Import production run_pipeline.py from the image, by path."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("rp", _RUN_PIPELINE_REMOTE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@app.function(image=image, timeout=600)
def cli_probe() -> dict:
    """$0 check that ``boltz predict`` at our pin accepts ``--seed``. No GPU."""
    rp = _load_pipeline()
    out = subprocess.run([rp.BOLTZ_BIN, "predict", "--help"],
                         capture_output=True, text=True, check=False)
    help_text = out.stdout + out.stderr
    ver = subprocess.run([sys.executable, "-c",
                          "import boltz, sys; sys.stdout.write(boltz.__version__)"],
                         capture_output=True, text=True, check=False)
    return {
        "rc": out.returncode,
        "boltz_version": ver.stdout.strip() or ver.stderr.strip(),
        "has_seed_flag": "--seed" in help_text,
        "seed_help_line": next(
            (line.strip() for line in help_text.splitlines() if "--seed" in line), None),
        "fold_chunk": rp.FOLD_CHUNK,
    }


@app.function(image=image, gpu=_GPU, timeout=_TIMEOUT_S,
              volumes={"/root/.boltz": weights})
def gpu_run(inputs: dict) -> dict:
    """The one approved paid run: 8 designs x 3 seeds, 3 processes.

    Seeds run one after another and each seed's rows are printed as they
    finish, so a timeout that kills the container still leaves the completed
    seeds in ``modal app logs``.
    """
    rp = _load_pipeline()
    target = inputs["target_seq"]
    designs = inputs["designs"]
    ids = {rp._record_id(i): d["name"] for i, d in enumerate(designs)}

    work = Path("/tmp/rung5")
    results: dict[str, dict] = {d["name"]: {} for d in designs}
    timings = []

    for seed in _SEEDS:
        data_dir = work / f"seed{seed}" / "in"
        out_dir = work / f"seed{seed}" / "out"
        data_dir.mkdir(parents=True, exist_ok=True)
        for i, d in enumerate(designs):
            (data_dir / f"{rp._record_id(i)}.yaml").write_text(
                rp.make_yaml_sseq(d["binder_seq"], target))

        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [rp.BOLTZ_BIN, "predict", str(data_dir), "--out_dir", str(out_dir),
               "--no_kernels", "--output_format", "pdb", "--override",
               "--seed", str(seed)]
        print(f"[rung5] seed {seed}: {' '.join(cmd)}", flush=True)
        t0 = time.time()
        rc = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr,
                            check=False).returncode
        wall = round(time.time() - t0, 1)
        timings.append({"seed": seed, "rc": rc, "wall_s": wall})
        print(f"[rung5] seed {seed}: rc={rc} wall={wall}s", flush=True)

        # rc covers the whole process, so decide each record on its own files.
        row = {}
        for rid, name in ids.items():
            _pdb, conf = rp.collect_outputs(out_dir, rid)
            results[name][f"seed{seed}"] = {
                "iptm": rp._num(conf.get("iptm") or conf.get("complex_iptm")),
                "plddt": rp._num(conf.get("complex_plddt") or conf.get("plddt")),
            }
            row[name] = results[name][f"seed{seed}"]["iptm"]
        print(f"[rung5] seed {seed} iptm: {json.dumps(row)}", flush=True)

    summary = []
    for d in designs:
        name = d["name"]
        vals = [results[name][f"seed{s}"]["iptm"] for s in _SEEDS]
        got = [v for v in vals if v is not None]
        summary.append({
            "design": name,
            "binder_len": d["binder_len"],
            "iptm": vals,
            "plddt": [results[name][f"seed{s}"]["plddt"] for s in _SEEDS],
            "n_folded": len(got),
            "median": round(statistics.median(got), 4) if got else None,
            "spread": round(max(got) - min(got), 4) if len(got) > 1 else None,
        })
    return {"seeds": list(_SEEDS), "timings": timings, "summary": summary,
            "gpu": _GPU, "timeout_s": _TIMEOUT_S}


@app.local_entrypoint()
def main(mode: str = "", inputs: str = "", out_dir: str = "") -> None:
    """Explicit dispatch. No fall-through default: an unrecognised mode exits."""
    if mode not in {"cli", "gpu"}:
        raise SystemExit(f"--mode must be 'cli' or 'gpu' (got {mode!r})")

    dest = Path(out_dir) if out_dir else _HERE
    result: dict

    if mode == "cli":
        result = cli_probe.remote()
    else:
        if not inputs:
            raise SystemExit("--mode gpu needs --inputs <rung5_inputs.json>")
        payload = json.loads(Path(inputs).read_text())
        # Pre-flight, before any remote call: everything the GPU needs, checked
        # here where a mistake costs nothing.
        assert len(payload["designs"]) == 8, "expected 8 designs"
        assert payload["target_len"] == len(payload["target_seq"])
        for d in payload["designs"]:
            assert d["binder_len"] == len(d["binder_seq"]), d["name"]
            assert d["binder_seq"].isalpha() and d["binder_seq"].isupper(), d["name"]
            assert d["binder_seq"] != payload["target_seq"], f"{d['name']} is the target"
        n_rec = len(payload["designs"]) * len(_SEEDS)
        print(f"[rung5] {len(payload['designs'])} designs x {len(_SEEDS)} seeds "
              f"= {n_rec} records in {len(_SEEDS)} processes; "
              f"timeout {_TIMEOUT_S}s caps raw GPU spend at "
              f"${_TIMEOUT_S * 0.000714:.2f}", flush=True)
        result = gpu_run.remote(payload)

    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = dest / f"{mode}_result_{stamp}.json"  # never overwritten
    path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"[rung5] wrote {path}")
