"""Rung-4 probe app for RFD4-Proteina. Scratchpad only -- not product code.

Two entry points:
  cpu_probe  -- CPU only. Builds the condition spec into an annotated CIF with the
                upstream validator, checks where the hotspot flags landed, dumps
                the resolved Hydra config, and reports which Metrics the chosen
                metrics_tags actually select. No GPU, no weights.
  gpu_run    -- the one approved A100-80GB run.
"""
import json
import os
import pathlib
import re
import sys
import time

import modal

_HERE = pathlib.Path(__file__).parent
# This module is re-imported INSIDE the container, where the script is mounted at
# /root and has no repo above it. So search upward for the real tools/rfd4 and fall
# back to a path that is never dereferenced there: from_dockerfile reads the file
# lazily, at local declare time only. Indexing parents[] directly crash-looped
# every container on 2026-11-03 with IndexError.
_REPO_RFD4 = next(
    (p / "tools" / "rfd4" for p in _HERE.parents
     if (p / "tools" / "rfd4" / "Dockerfile.modal").is_file()),
    _HERE / "tools" / "rfd4",
)

weights = modal.Volume.from_name("rfd4-proteina-weights")

image = modal.Image.from_dockerfile(
    str(_REPO_RFD4 / "Dockerfile.modal"),
    build_args={"RFD4_ACCELERATOR": "cu13"},
)

app = modal.App("ranomics-rfd4-rung4-probe")

_IN_DIR = "/tmp/rfd4_in"
# EU/author numbering -> label_seq_id is auth - 224 for both chains of this file.
_HOTSPOT_AUTH = [241, 243, 244, 246, 260, 262, 264, 301]
_TAGS = ["design", "interchain_interactions", "hotspot_conditioning"]


# The spec the paid run uses. V1 (condition_spec_fc.yaml, both chains indexed)
# does NOT get past upstream prevalidation -- see the resume note in docs/scoping.
_PAID_SPEC = "spec_v4_chainA.yaml"


def _stage(files: dict, spec: str = _PAID_SPEC) -> str:
    os.makedirs(_IN_DIR, exist_ok=True)
    for name, blob in files.items():
        with open(os.path.join(_IN_DIR, name), "wb") as fh:
            fh.write(blob)
    # An absolute spec is one that already ships inside the image (upstream's own
    # examples under /opt/rfd4), so there is nothing of ours to stage for it.
    return spec if spec.startswith("/") else os.path.join(_IN_DIR, spec)


def _run(cmd: list[str], cwd: str = "/workspace") -> tuple[int, str]:
    import subprocess

    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    out = f"$ {' '.join(cmd)}\n[exit {p.returncode}, {time.time()-t0:.1f}s]\n{p.stdout}\n{p.stderr}"
    return p.returncode, out


@app.function(image=image, cpu=4, memory=16384, timeout=60 * 30)
def cpu_probe(files: dict, spec_name: str = _PAID_SPEC) -> dict:
    spec = _stage(files, spec_name)
    report: dict = {}

    rc, out = _run([sys.executable, "/opt/rfd4/scripts/write_condition_spec_cifs.py", spec,
                    "--out-dir", "/tmp/cifs", "--verbose"])
    report["write_cifs_rc"] = rc
    report["write_cifs_log"] = out[-20000:]

    built = sorted(str(p) for p in pathlib.Path("/tmp/cifs").rglob("*.cif*")) if os.path.isdir("/tmp/cifs") else []
    report["built_cifs"] = built

    # Where did the condition flags actually land? Read the annotated CIF back and
    # report, per condition column, the residues it marks -- in both numberings.
    for _cif in built:
        import gemmi

        doc = gemmi.cif.read(_cif)
        blk = doc.sole_block()
        names = [t.strip() for t in blk.get_mmcif_category_names()]
        cols = [c for c in blk.find_mmcif_category("_atom_site.").tags]
        report.setdefault("atom_site_tags", {})[os.path.basename(_cif)] = cols
        cond_cols = [c for c in cols if "condition" in c.lower()]
        keys = ["_atom_site.label_asym_id", "_atom_site.label_seq_id",
                "_atom_site.auth_seq_id", "_atom_site.label_comp_id",
                "_atom_site.label_atom_id"]
        want = [k for k in keys if k in cols] + cond_cols
        tab = blk.find("", want)
        per_col: dict = {}
        for row in tab:
            rec = {w: row[i] for i, w in enumerate(want)}
            res = (rec.get("_atom_site.label_asym_id"), rec.get("_atom_site.label_seq_id"),
                   rec.get("_atom_site.auth_seq_id"), rec.get("_atom_site.label_comp_id"))
            for c in cond_cols:
                v = rec.get(c)
                if v in (".", "?", "", None):
                    continue
                d = per_col.setdefault(c, {})
                d.setdefault(v, set()).add("/".join(str(x) for x in res))
        report.setdefault("condition_columns", {})[os.path.basename(_cif)] = {
            c: {v: (sorted(rs) if len(rs) <= 40 else [f"<{len(rs)} residues>", *sorted(rs)[:8]])
                for v, rs in sorted(vals.items())}
            for c, vals in per_col.items()
        }
        report.setdefault("mmcif_categories", {})[os.path.basename(_cif)] = names

    # Resolved Hydra config for the production configuration we intend to run.
    rc, out = _run([
        "rfd4-generate",
        "inference_experiment=inference_on_user_inputs",
        "hf_ckpt_path=null",
        "ckpt_name=rfd4_proteina_flow-ema.ckpt",
        f"inputs={spec}",
        "num_replicates=4",
        f"metrics_tags=[{','.join(_TAGS)}]",
        "delete_failing_pre_oracle=False",
        "out_dir=/tmp/rfd4_run",
        "--cfg", "job", "--resolve",
    ])
    report["cfg_rc"] = rc
    report["cfg_dump"] = out[-40000:]

    # Which Metrics does this tag set actually select, and does any oracle survive?
    code = f'''
import hydra, json
from hydra import initialize_config_dir, compose
tags = set({_TAGS!r})
with initialize_config_dir(version_base=None, config_dir="/opt/rfd4/configs"):
    cfg = compose(config_name="inference", overrides=[
        "inference_experiment=inference_on_user_inputs",
        "hf_ckpt_path=null",
        "metrics_tags=[{','.join(_TAGS)}]",
    ])
from omegaconf import OmegaConf
m = OmegaConf.to_container(cfg.metrics, resolve=False)
def applies(spec):
    if not isinstance(spec, dict):
        return None
    req_all = set(spec.get("required_tags_all") or [])
    req_any = set(spec.get("required_tags_any") or [])
    pro = set(spec.get("prohibited_tags") or [])
    return (not req_all or tags >= req_all) and (not req_any or bool(tags & req_any)) and not (tags & pro)
res = {{}}
for group, entries in m.items():
    for name, spec in (entries or {{}}).items():
        a = applies(spec)
        if a is not None:
            res[f"{{group}}.{{name}}"] = a
print(json.dumps({{"selected": sorted(k for k,v in res.items() if v),
                   "skipped": sorted(k for k,v in res.items() if not v)}}, indent=1))
'''
    pathlib.Path("/tmp/tagcheck.py").write_text(code)
    rc, out = _run([sys.executable, "/tmp/tagcheck.py"])
    report["tagcheck_rc"] = rc
    report["tagcheck"] = out[-20000:]
    return report


@app.function(image=image, cpu=4, memory=16384, timeout=60 * 30)
def variant_probe(files: dict, specs: list[str]) -> dict:
    """Free CPU sweep: which spec variant gets past upstream prevalidation?

    Also prints the atomworks molecule_iid derivation and the per-chain
    molecule_iid of the assembled array, so the index-group merge is measured
    rather than guessed.
    """
    _stage(files)
    report: dict = {"variants": {}}

    for name in specs:
        spec = os.path.join(_IN_DIR, name)
        rc, out = _run([sys.executable, "/opt/rfd4/scripts/write_condition_spec_cifs.py", spec,
                        "--out-dir", f"/tmp/cifs_{name}", "--verbose"])
        d = pathlib.Path(f"/tmp/cifs_{name}")
        report["variants"][name] = {
            "rc": rc,
            "log_tail": out[-6000:],
            "built": sorted(str(q) for q in d.rglob("*.cif*")) if d.is_dir() else [],
        }

    code = r"""
import inspect, json, sys
out = {}
try:
    from atomworks.io.utils.selection import add_molecule_iid_annotation as f
except Exception as exc:
    f = None
    out["import_selection"] = repr(exc)
if f is None:
    import importlib, pkgutil, atomworks
    for m in pkgutil.walk_packages(atomworks.__path__, "atomworks."):
        try:
            mod = importlib.import_module(m.name)
        except Exception:
            continue
        if hasattr(mod, "add_molecule_iid_annotation"):
            f = getattr(mod, "add_molecule_iid_annotation")
            out["found_in"] = m.name
            break
out["source"] = inspect.getsource(f) if f is not None else None
print(json.dumps(out, indent=1))
"""
    pathlib.Path("/tmp/molsrc.py").write_text(code)
    rc, out = _run([sys.executable, "/tmp/molsrc.py"])
    report["molsrc_rc"] = rc
    report["molsrc"] = out[-20000:]

    # Per-chain molecule_iid for the two-chain file, as atomworks derives it.
    code2 = r"""
import json
import numpy as np
from atomworks.io.parser import parse
from atomworks.io.utils.selection import (
    add_id_and_entity_annotations, add_molecule_iid_annotation, ensure_annotations,
)
res = {}
for path in ["/tmp/rfd4_in/3ave_fc_AB.cif", "/tmp/rfd4_in/3ave_fc_A.cif"]:
    try:
        parsed = parse(path)
        aa = parsed["assemblies"]["1"] if isinstance(parsed, dict) and "assemblies" in parsed else parsed
        if isinstance(aa, dict):
            aa = list(aa.values())[0]
        aa = ensure_annotations(aa, "is_polymer")
        aa = add_id_and_entity_annotations(aa)
        if "transformation_id" not in aa.get_annotation_categories():
            aa.set_annotation("transformation_id", np.full(aa.array_length(), "1"))
        aa = add_molecule_iid_annotation(aa)
        pairs = sorted(set(zip(aa.chain_id.tolist(), aa.molecule_iid.tolist())))
        res[path] = {"chain_molecule_iid": [list(map(str, t)) for t in pairs]}
    except Exception as exc:
        res[path] = {"error": repr(exc)}
print(json.dumps(res, indent=1))
"""
    pathlib.Path("/tmp/moliid.py").write_text(code2)
    rc, out = _run([sys.executable, "/tmp/moliid.py"])
    report["moliid_rc"] = rc
    report["moliid"] = out[-20000:]
    return report


@app.function(image=image, gpu="A100-80GB", volumes={"/data": weights},
              timeout=1020, memory=65536)
def gpu_run(files: dict, num_replicates: int = 4, spec_name: str = _PAID_SPEC) -> dict:
    """The one approved paid run. timeout=1020s bounds the spend to ~$1.78 customer."""
    import subprocess
    import threading

    spec = _stage(files, spec_name)
    out_dir = "/tmp/rfd4_run"
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.time()
    sightings: list[tuple[float, str]] = []
    stop = threading.Event()

    def watch() -> None:
        seen: set[str] = set()
        while not stop.wait(0.5):
            for p in pathlib.Path(out_dir).rglob("*"):
                s = str(p)
                if ".generated-cif-" in s or s in seen or p.is_dir():
                    continue
                seen.add(s)
                sightings.append((round(time.time() - t0, 1), s))

    th = threading.Thread(target=watch, daemon=True)
    th.start()

    cmd = [
        "rfd4-generate",
        "inference_experiment=inference_on_user_inputs",
        "hf_ckpt_path=null",
        "ckpt_name=rfd4_proteina_flow-ema.ckpt",
        f"inputs={spec}",
        f"num_replicates={num_replicates}",
        f"metrics_tags=[{','.join(_TAGS)}]",
        "delete_failing_pre_oracle=False",
        f"out_dir={out_dir}",
    ]
    timed_out = False
    try:
        p = subprocess.run(cmd, cwd="/workspace", capture_output=True, text=True,
                           errors="replace", timeout=870)
        rc, so, se = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = None
        so = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        se = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
    wall = time.time() - t0
    stop.set()
    th.join(timeout=5)

    files_out: dict[str, str] = {}
    for q in sorted(pathlib.Path(out_dir).rglob("*")):
        if q.is_file() and (q.suffix in (".cif", ".csv", ".json", ".yaml", ".txt") or q.name.endswith(".cif.zst")):
            try:
                files_out[str(q)] = q.read_bytes().hex() if q.suffix == ".zst" else q.read_text(errors="replace")
            except Exception as exc:  # noqa: BLE001
                files_out[str(q)] = f"<unreadable: {exc}>"

    return {
        "cmd": cmd,
        "rc": rc,
        "timed_out": timed_out,
        "wall_s": round(wall, 1),
        "stdout": so[-200000:],
        "stderr": se[-200000:],
        "sightings": sightings,
        "tree": sorted(str(q) for q in pathlib.Path(out_dir).rglob("*")),
        "files": files_out,
    }


@app.local_entrypoint()
def main(mode: str = "cpu", num_replicates: int = 4, spec_name: str = _PAID_SPEC) -> None:
    payload = {
        q.name: q.read_bytes()
        for q in sorted(_HERE.glob("3ave_fc_*.cif")) + sorted(_HERE.glob("*.yaml"))
    }
    specs = [s.strip() for s in spec_name.split(",") if s.strip()]
    if mode == "variants" and spec_name == _PAID_SPEC:
        specs = ["spec_v5_nocrdgrp.yaml", "spec_v7_unindex_b.yaml"]

    # Every spec about to run must have its spec file AND the CIFs it names staged,
    # or the container allocates (on `gpu`, an A100) and then fails on a missing
    # input. Checked here, before any remote call.
    for s in specs:
        if s.startswith("/"):
            continue  # ships in the image; nothing local to check
        if s not in payload:
            raise SystemExit(f"spec {s} is not in {_HERE}")
        for ref in re.findall(rf"{re.escape(_IN_DIR)}/(\S+)", (_HERE / s).read_text(encoding="utf-8")):
            if ref not in payload:
                raise SystemExit(f"{s} names {ref}, which is not in {_HERE}")

    if mode == "cpu":
        out = cpu_probe.remote(payload, specs[0])
    elif mode == "variants":
        out = variant_probe.remote(payload, specs)
    else:
        out = gpu_run.remote(payload, num_replicates, specs[0])
    # Never overwrite a result: rung 3 lost its metrics CSV once already, and on a
    # paid run this file is the only copy of what the money bought.
    dest = _HERE / f"{mode}_result_{time.strftime('%Y%m%dT%H%M%S')}.json"
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {dest}")
