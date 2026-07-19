"""Modal entrypoint for OpenDDE all-atom co-folding.

Reads job configuration from the ``JOB_PAYLOAD`` env var (same contract as the
boltz2 / iggm / proteina pipelines), writes the assembled OpenDDE spec to
``input.json``, runs ``opendde pred`` live-streaming its logs, uploads each
predicted structure via presigned PUT URLs requested from the hub, emits
per-prediction ``new_candidate`` heartbeats, and writes the final summary to
``/tmp/smoke_results.json`` (returned inline by ``tools/opendde/modal_app.py``).

OpenDDE inputs are INLINE — there is no PDB upload, so the whole spec travels
inside ``job_spec['spec']`` and this pipeline never downloads an input file.

Environment variables (set by ``tools/opendde/modal_app.py``):

    JOB_PAYLOAD     JSON: job_spec + upload_urls_endpoint + tier
    WEBHOOK_URL     URL to POST heartbeats to (derives /webhooks/heartbeat)
    JOB_ID          tool_jobs row id (log prefix + heartbeat body)
    JOB_TOKEN       job-specific auth token (heartbeat new_candidate gate)
    JOB_TIER        ``general`` | ``abag``
    OPENDDE_ROOT_DIR  runtime data root (checkpoint/ lives here, on the Volume)

The output structure format (mmCIF vs PDB) and the exact confidence-score file
are NOT documented upstream, so collection is deliberately defensive: it globs
both formats, best-effort converts mmCIF to PDB for the viewer, and best-effort
reads a ranking score from any co-located JSON. Per the standing "never parse in
the container" rule, the COMPLETE output tree is tarred and parked on the raw
Volume regardless of what this light-touch parse recovers.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("opendde_pipeline")


SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"

OPENDDE_BIN = os.environ.get("OPENDDE_BIN", "opendde")
OPENDDE_ROOT = os.environ.get("OPENDDE_ROOT_DIR", "/opt/opendde_root")
MODEL_NAME = "opendde_v1"
CHECKPOINTS = {
    "general": "opendde.pt",
    "abag": "opendde_abag.pt",
}
# Keys we try, in order, when scraping a confidence/ranking JSON. Undocumented
# upstream — best-effort only; a missing score renders as "—", never fails.
_RANKING_KEYS = ("ranking_score", "ranking", "confidence", "score")
_PTM_KEYS = ("ptm", "pTM", "complex_ptm")
_IPTM_KEYS = ("iptm", "ipTM", "complex_iptm")
_PLDDT_KEYS = ("plddt", "pLDDT", "mean_plddt", "complex_plddt")


# ===========================================================================
# Result writer  (verbatim contract from boltz2)
# ===========================================================================


def _write_result(payload: dict[str, Any]) -> None:
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except OSError as exc:
        logger.error("Could not write %s: %s", SMOKE_RESULTS_PATH, exc)


def _fail(bucket: str, check: str, detail: str) -> None:
    logger.error("pipeline FAILED at %s/%s: %s", bucket, check, detail)
    _write_result(
        {
            "status": "FAILED",
            "error": {"bucket": bucket, "check": check, "detail": detail},
            "tier": os.environ.get("JOB_TIER", ""),
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    sys.exit(1)


def _num(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Heartbeat + upload helpers  (verbatim contract from boltz2)
# ===========================================================================


def _heartbeat_url(webhook_url: str) -> str:
    parsed = urlparse(webhook_url)
    return urlunparse(parsed._replace(path="/webhooks/heartbeat"))


def send_heartbeat(
    webhook_url: str,
    job_id: str,
    stage: str,
    designs_completed: int = 0,
    designs_total: int = 0,
    new_candidate: dict | None = None,
) -> None:
    """Fire-and-forget heartbeat. Never raises."""
    if not webhook_url:
        return
    body = {
        "job_id": job_id,
        "stage": stage,
        "designs_completed": int(designs_completed),
        "designs_total": int(designs_total),
    }
    if isinstance(new_candidate, dict):
        body["new_candidate"] = new_candidate
        body["job_token"] = os.environ.get("JOB_TOKEN", "")
    try:
        resp = requests.post(_heartbeat_url(webhook_url), json=body, timeout=10)
        logger.debug("Heartbeat sent: %s (HTTP %d)", stage, resp.status_code)
    except Exception as exc:
        logger.warning("Heartbeat failed (%s): %s", stage, exc)


def request_upload_urls(
    upload_endpoint: str, job_token: str, filenames: list[str]
) -> dict[str, str]:
    resp = requests.post(
        upload_endpoint,
        json={"filenames": filenames},
        headers={"Authorization": f"Bearer {job_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"upload_urls request failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    return resp.json()["urls"]


def upload_structure(url: str, data: bytes, content_type: str) -> None:
    resp = requests.put(url, data=data, headers={"Content-Type": content_type}, timeout=120)
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"upload failed: HTTP {resp.status_code} {resp.text[:200]}")


# ===========================================================================
# Payload parsing
# ===========================================================================


def parse_payload() -> dict[str, Any]:
    raw = os.environ.get("JOB_PAYLOAD", "").strip()
    if not raw:
        _fail("preflight", "env", "JOB_PAYLOAD env var is empty")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail("preflight", "env", f"JOB_PAYLOAD is not valid JSON: {exc}")
    return {}  # unreachable


# ===========================================================================
# Output collection (defensive — format is undocumented upstream)
# ===========================================================================


def _cif_to_pdb(cif_bytes: bytes) -> bytes | None:
    """Best-effort mmCIF -> PDB for the browser viewer. None on any failure."""
    try:
        import gemmi

        st = gemmi.read_structure_string(cif_bytes.decode("utf-8", "replace"))
        st.setup_entities()
        return st.make_pdb_string().encode("utf-8")
    except Exception as exc:  # noqa: BLE001 — conversion is optional
        logger.warning("cif->pdb conversion failed (%s); serving native file", exc)
        return None


def _read_score_json(structure_path: Path) -> dict:
    """Scrape a ranking/confidence JSON co-located with a structure. Best-effort."""
    cand: dict = {}
    search_dirs = [structure_path.parent, structure_path.parent.parent]
    json_paths: list[str] = []
    for d in search_dirs:
        json_paths += glob.glob(f"{d}/*.json")
    stem = structure_path.stem
    # Prefer a JSON that shares the structure's stem, else the first available.
    json_paths.sort(key=lambda p: (stem not in Path(p).stem, p))
    for jp in json_paths[:8]:
        try:
            with open(jp) as fh:
                data = json.load(fh)
        except Exception:
            continue
        if isinstance(data, dict):
            cand = data
            break
    return cand


def _first(d: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        if k in d and d[k] is not None:
            v = _num(d[k])
            if v is not None:
                return v
    return None


def collect_structures(out_dir: Path) -> list[Path]:
    """Glob predicted structures. Prefer mmCIF (AF3-class default); fall back to PDB.

    Picking one format per run avoids double-counting when both are emitted.
    """
    cif = sorted(glob.glob(f"{out_dir}/**/*.cif", recursive=True)) + sorted(
        glob.glob(f"{out_dir}/**/*.mmcif", recursive=True)
    )
    if cif:
        return [Path(p) for p in cif]
    pdb = sorted(glob.glob(f"{out_dir}/**/*.pdb", recursive=True))
    return [Path(p) for p in pdb]


# ===========================================================================
# Raw output capture  (verbatim contract from boltz2)
# ===========================================================================


def archive_raw_outputs(work_dir: str, dest: str = RAW_ARCHIVE_PATH) -> None:
    """Tar the ENTIRE work tree to ``dest`` before teardown destroys it.

    A container must never decide which fields are worth keeping. The confidence
    file format and score names are undocumented for OpenDDE, so the local re-parse
    of this tar is the source of truth — the container keeps only a best-effort
    ranking scalar. Unconditional + best-effort by design: it runs on every exit
    path (a run that folded nothing is exactly the tree you need) and never raises.
    """
    try:
        if not os.path.isdir(work_dir):
            logger.warning("raw capture: %s is not a directory — nothing to archive", work_dir)
            return
        root = os.path.abspath(work_dir)
        dest_abs = os.path.abspath(dest)
        if os.path.commonpath([dest_abs, root]) == root:
            logger.error(
                "raw capture: refusing to write %s inside the tree it archives (%s)",
                dest_abs, root,
            )
            return
        with tarfile.open(dest_abs, "w:gz") as tf:
            tf.add(root, arcname=os.path.basename(root) or "work")
        logger.info(
            "raw capture: archived %s -> %s (%.1f MB)",
            root, dest_abs, os.path.getsize(dest_abs) / 1e6,
        )
    except Exception as exc:
        logger.warning("raw capture failed (non-fatal): %s: %s", type(exc).__name__, exc)
        try:
            if os.path.exists(dest_abs):
                os.remove(dest_abs)
        except OSError:
            pass


# ===========================================================================
# OpenDDE invocation
# ===========================================================================


def run_opendde(
    input_json: Path, out_dir: Path, preset: str, sample: int, step: int, cycle: int
) -> int:
    """Run one ``opendde pred``. stdout/stderr live-stream to Modal logs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        OPENDDE_BIN, "pred",
        "-i", str(input_json),
        "-o", str(out_dir),
        "-n", MODEL_NAME,
        "--use_msa", "false",
        "--use_template", "false",
        "--use_rna_msa", "false",
        "--sample", str(sample),
        "--step", str(step),
        "--cycle", str(cycle),
    ]
    # The ABAG checkpoint is selected by an explicit path override; the general
    # checkpoint resolves from OPENDDE_ROOT_DIR/checkpoint/opendde.pt by default.
    if preset == "abag":
        cmd += ["--load_checkpoint_path", os.path.join(OPENDDE_ROOT, "checkpoint", CHECKPOINTS["abag"])]
    logger.info("opendde cmd: %s", " ".join(cmd))
    # Live-stream — never capture_output for long-running GPU subprocesses.
    result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr, check=False)
    return result.returncode


# ===========================================================================
# Main
# ===========================================================================


def main() -> None:
    start = time.time()
    payload = parse_payload()

    job_spec = payload.get("job_spec") or {}
    preset = str(job_spec.get("preset") or payload.get("tier") or "general").lower()
    if preset not in CHECKPOINTS:
        preset = "general"

    spec = job_spec.get("spec")
    if not isinstance(spec, list) or not spec:
        _fail("input", "spec", "job_spec['spec'] is missing or not a non-empty list")

    sample = int(job_spec.get("sample") or 1)
    step = int(job_spec.get("step") or 200)
    cycle = int(job_spec.get("cycle") or 10)
    designs_total = int(job_spec.get("n_designs_total") or sample)

    webhook_url = os.environ.get("WEBHOOK_URL", "")
    job_id = os.environ.get("JOB_ID", "")
    job_token = payload.get("job_token", "") or os.environ.get("JOB_TOKEN", "")
    upload_endpoint = payload.get("upload_urls_endpoint", "")
    if not upload_endpoint:
        _fail("preflight", "upload_urls_endpoint", "upload_urls_endpoint missing from payload")

    # Preflight: the checkpoint must be present on the Volume BEFORE any GPU spend
    # so a paying job never races a cold / missing weight download.
    ckpt = os.path.join(OPENDDE_ROOT, "checkpoint", CHECKPOINTS[preset])
    if not os.path.isfile(ckpt):
        _fail("preflight", "weights", f"checkpoint not found: {ckpt} (seed the opendde-weights Volume)")

    logger.info(
        "opendde starting: preset=%s sample=%d step=%d cycle=%d designs_total=%d",
        preset, sample, step, cycle, designs_total,
    )
    send_heartbeat(webhook_url, job_id, stage="loading_model", designs_completed=0, designs_total=designs_total)

    with tempfile.TemporaryDirectory(prefix="opendde_", dir="/tmp") as _td:
        workdir = Path(_td)
        designs_out: list[dict] = []
        n_failures = 0
        try:
            input_json = workdir / "input.json"
            input_json.write_text(json.dumps(spec, indent=2))
            out_dir = workdir / "output"

            send_heartbeat(webhook_url, job_id, stage="folding", designs_completed=0, designs_total=designs_total)

            rc = run_opendde(input_json, out_dir, preset, sample, step, cycle)
            if rc != 0:
                _fail("run", "opendde", f"opendde pred exited with code {rc}")

            structures = collect_structures(out_dir)
            if not structures:
                _fail("output", "structures", "opendde produced no predicted structures")

            # Score + rank. Missing scores sort last; rank is assigned after sorting.
            scored: list[tuple[Path, dict]] = []
            for sp in structures:
                score_json = _read_score_json(sp)
                scored.append((sp, score_json))
            scored.sort(
                key=lambda t: (
                    _first(t[1], _RANKING_KEYS) is None,
                    -(_first(t[1], _RANKING_KEYS) or 0.0),
                )
            )

            for rank, (sp, score_json) in enumerate(scored):
                design_start = time.time()
                # Key on the path RELATIVE to out_dir (not the bare stem): OpenDDE
                # writes one predictions/ dir PER seed, and the per-sample
                # filenames repeat across seeds, so a stem-only key would collide
                # and overwrite structures for any n_seeds > 1 run.
                try:
                    rel = sp.relative_to(out_dir).with_suffix("")
                    name = "_".join(rel.parts) or f"prediction_{rank}"
                except ValueError:
                    name = sp.stem or f"prediction_{rank}"
                name = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
                raw_bytes = sp.read_bytes()
                is_cif = sp.suffix.lower() in (".cif", ".mmcif")

                if is_cif:
                    pdb_bytes = _cif_to_pdb(raw_bytes)
                    if pdb_bytes is not None:
                        struct_key, struct_bytes, ctype = f"{name}.pdb", pdb_bytes, "chemical/x-pdb"
                    else:
                        struct_key, struct_bytes, ctype = f"{name}.cif", raw_bytes, "chemical/x-cif"
                else:
                    struct_key, struct_bytes, ctype = f"{name}.pdb", raw_bytes, "chemical/x-pdb"

                try:
                    urls = request_upload_urls(upload_endpoint, job_token, [struct_key])
                    upload_structure(urls[struct_key], struct_bytes, ctype)
                except Exception as exc:
                    n_failures += 1
                    logger.warning("prediction %s: upload failed (%s) — skipping", name, exc)
                    continue

                ranking = _first(score_json, _RANKING_KEYS)
                design_entry = {
                    "rank": rank,
                    "name": name,
                    "pdb_key": struct_key,
                    "ranking_score": ranking,
                    "ptm": _first(score_json, _PTM_KEYS),
                    "iptm": _first(score_json, _IPTM_KEYS),
                    "plddt": _first(score_json, _PLDDT_KEYS),
                    "filter_status": "scored" if ranking is not None else "no_score",
                    "runtime_seconds": int(time.time() - design_start),
                }
                designs_out.append(design_entry)
                send_heartbeat(
                    webhook_url, job_id,
                    stage="folding",
                    designs_completed=len(designs_out),
                    designs_total=designs_total,
                    new_candidate=design_entry,
                )
                logger.info("  -> %s ranking=%s (%s)", name, ranking, struct_key)

        finally:
            # Ship the COMPLETE tree home BEFORE TemporaryDirectory deletes it —
            # a _fail sys.exit, an unexpected raise, or a zero-output run is a path
            # whose tree is worth more, not less (and OpenDDE's score format is
            # undocumented, so the local re-parse depends on this tar).
            archive_raw_outputs(str(workdir))

    runtime_seconds = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": preset,
            "designs_total": designs_total,
            "designs_completed": len(designs_out),
            "n_failures": n_failures,
            "designs": designs_out,
            "sample": sample,
            "step": step,
            "cycle": cycle,
            "runtime_seconds": runtime_seconds,
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    send_heartbeat(
        webhook_url, job_id,
        stage="complete",
        designs_completed=len(designs_out),
        designs_total=designs_total,
    )
    logger.info(
        "pipeline ok — %d predictions uploaded, %d failures, runtime=%ds",
        len(designs_out), n_failures, runtime_seconds,
    )


if __name__ == "__main__":
    main()
