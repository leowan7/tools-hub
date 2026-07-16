"""Modal entrypoint for one Proteina-Complexa search SHARD.

One invocation == one independent, seeded inference-time search on one
A100-80GB, returning up to ``_SHARD_DESIGNS`` (8) designs. The campaign engine
(``shared/compute_campaigns.py``) fans ``num_designs`` out across many of these
containers; the hub does the global cross-shard top-K + diversity. This script
never runs multi-GPU / multi-shard itself.

Contract (identical to boltz2 / iggm; set by ``tools/proteina/modal_app.py``):

    JOB_PAYLOAD   JSON: job_spec + input_presigned_url + upload_urls_endpoint
                  + job_token + tier
    WEBHOOK_URL   heartbeats derive /webhooks/heartbeat from it
    JOB_ID        tool_jobs row id (log prefix + heartbeat body + seed source)
    JOB_TOKEN     per-job auth token (new_candidate heartbeat gate)
    JOB_TIER      the preset (protein_binder | ligand_binder | motif_ame | validate)
    PROTEINA_RF3  on (default) | off — the RF3 reward kill-switch (Dockerfile ENV)

job_spec (from ``tools/proteina/__init__.py`` build_payload):
    preset, config_name, task_name, target_chain, rf3_required,
    nsamples, replicas, nsteps, parameters

Output (``/tmp/smoke_results.json`` == the persisted ``job.result``): both a
flat ``designs`` list and a ``candidates`` list whose nested ``scores`` dict is
keyed to the results columns the viewer renders
(total_reward / af2_iptm / af2_plddt / rf3_score / binder_scrmsd / cluster_id).

CONFIRMED upstream facts (Proteina-Complexa @ dev 916eaaed, source-verified
against the pinned checkout 2026-07-16):
  * generate seed = cfg.seed + job_id (generate.py:74). gen_njobs=1 forces
    job_id=0 (split_by_job zeroes any job_id>=1), so cross-shard independence
    comes from a distinct ++seed derived from JOB_ID, never from job_id.
  * model checkpoints resolve via RELATIVE config keys (ckpt_path: ./ckpts,
    ckpt_name, autoencoder_ckpt_path: ./ckpts/<v>_ae.ckpt), so we run from
    cwd=/opt/proteina with the weights Volume mounted at ./ckpts. `complexa` is
    the console script (pyproject [project.scripts]); `design` + `validate` are
    real subcommands.
  * results-CSV early-exit (generate.py:584-589) keyed on (config_name, job_id)
    at CWD-relative ./inference/results_<config>_<job_id>.csv. cwd is now the
    shared repo root (not a per-shard temp dir), so main() wipes ./inference at
    shard start to stop a warm container re-emitting a prior shard's designs.
  * filter keeps all samples with
    ++generation.filter.delete_non_top_n_samples=false and a high
    ++generation.filter.filter_samples_limit.
  * reward channels are config-gated, NOT flag-gated: protein_binder scores on
    AF2 only (rf3folding commented out in binder_generate.yaml); ligand_binder
    scores on RF3 only (its sole active reward); motif_ame's reward block is
    commented out upstream (the least-verified variant).

BUILD-TIME-VERIFY (only a P2 seed / P4-P5 canary can pin these — the output
layer, not the launch recipe):
  * the reward/results CSV path + exact column names (mapped tolerantly below);
  * the per-design PDB glob under ./inference/;
  * whether the AF2 binder reward tolerates the absent dssp / sc binaries (the
    public image ships without them; DSSP_EXEC/SC_EXEC are left unset);
  * the custom-target (bring-your-own PDB/SDF) registration mechanism.
"""

from __future__ import annotations

import csv
import glob
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
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
logger = logging.getLogger("proteina_pipeline")

SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"

PROTEINA_HOME = os.environ.get("PROTEINA_HOME", "/opt/proteina")
CONFIG_DIR = os.environ.get("PROTEINA_CONFIG_DIR", f"{PROTEINA_HOME}/configs")
# The generator checkpoints live in the repo-root ckpts/ dir (the weights Volume
# mounts here); the configs reference them via the RELATIVE key `ckpt_path:
# ./ckpts`, so the validate tier globs this exact dir for a *.ckpt.
WEIGHTS_DIR = os.environ.get("WEIGHTS_DIR", f"{PROTEINA_HOME}/ckpts")
COMPLEXA_BIN = os.environ.get("COMPLEXA_BIN", "complexa")

# The three paid design-variant configs (validate exercises all of them so it
# is not a protein-only false green).
_ALL_CONFIGS = (
    "search_binder_local_pipeline",
    "search_ligand_binder_local_pipeline",
    "search_ame_local_pipeline",
)

# RF3 kill-switch. Off-values mirror the tools-hub CSRF_PROTECT=0 pattern.
_RF3_OFF = {"off", "false", "0", "no"}

# Results columns the viewer renders (proteina_results.html). Each maps to one
# or more tolerant upstream column-name candidates (BUILD-TIME-VERIFY the exact
# spellings against the reward CSV at canary; unmatched -> None -> hidden).
_SCORE_COLUMNS: dict[str, tuple[str, ...]] = {
    "total_reward": ("total_reward", "reward", "score", "total_score"),
    "af2_iptm": ("af2_iptm", "iptm", "af2_ iptm", "af2_ipTM", "ipTM"),
    "af2_plddt": ("af2_plddt", "plddt", "af2_pLDDT", "pLDDT"),
    "rf3_score": ("rf3_score", "rf3", "rf3_reward", "rf3folding", "rf3_plddt"),
    "binder_scrmsd": ("binder_scrmsd", "scrmsd", "sc_rmsd", "binder_rmsd", "self_consistency_rmsd"),
    "cluster_id": ("cluster_id", "cluster", "cluster_idx", "diversity_cluster"),
}
# Candidate columns for a PDB path/name in the reward CSV (tolerant).
_PDB_PATH_COLUMNS = ("pdb_path", "path", "sample_path", "structure_path", "filepath", "file")
_PDB_NAME_COLUMNS = ("sample", "name", "sample_name", "design_id", "sample_id", "id", "tag", "metadata_tag")


# ===========================================================================
# Result writer + fast-fail
# ===========================================================================


def _write_result(payload: dict[str, Any]) -> None:
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except OSError as exc:
        logger.error("Could not write %s: %s", SMOKE_RESULTS_PATH, exc)


def _fail(bucket: str, check: str, detail: str) -> None:
    """Write a FAILED result and exit 1 (no GPU spent past this point)."""
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


# ===========================================================================
# Heartbeat + upload helpers (identical contract to boltz2 / iggm)
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
    """Fire-and-forget heartbeat. Never raises — a long shard must keep beating
    so the stale-job sweeper does not reap it as dead."""
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


def upload_pdb(url: str, pdb_bytes: bytes) -> None:
    resp = requests.put(
        url, data=pdb_bytes, headers={"Content-Type": "chemical/x-pdb"}, timeout=120
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"upload failed: HTTP {resp.status_code} {resp.text[:200]}")


# ===========================================================================
# Payload parsing + input download
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


def download_target(url: str, dest: Path) -> Path:
    """Stream a custom target file (PDB / SDF) from the presigned GET URL."""
    try:
        with requests.get(url, stream=True, timeout=180) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=32768):
                    if chunk:
                        fh.write(chunk)
    except Exception as exc:
        _fail("input", "download", f"custom target download failed: {exc}")
    if not dest.is_file() or dest.stat().st_size < 32:
        _fail("input", "download", "downloaded target is empty or tiny")
    return dest


def sdf_to_pdb(sdf_path: Path, dest: Path) -> Path:
    """Convert a small-molecule SDF into a HETATM PDB via RDKit (present in this
    image, absent in the tools-hub web tier — hence the conversion happens
    here). Keeps the first valid molecule, ensures a 3D conformer (embeds one if
    the SDF was 2D-only), adds hydrogens, and writes a PDB. Scaffolding for the
    bring-your-own-ligand fast-follow — the custom-target path is hard-blocked
    until a canary wires the upstream registration (see main)."""
    try:
        from rdkit import Chem  # noqa: PLC0415
        from rdkit.Chem import AllChem  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - image guarantees RDKit
        _fail("input", "rdkit", f"RDKit import failed in container: {exc}")

    supplier = Chem.SDMolSupplier(str(sdf_path), removeHs=False, sanitize=True)
    mol = next((m for m in supplier if m is not None), None)
    if mol is None:
        _fail("input", "sdf", "no valid molecule parsed from the uploaded SDF")
    # A 2D-only SDF has no usable geometry; embed a 3D conformer before AddHs.
    if mol.GetNumConformers() == 0:
        mol = Chem.AddHs(mol)
        if AllChem.EmbedMolecule(mol, randomSeed=0xf00d) != 0:
            _fail("input", "sdf", "could not embed a 3D conformer for the ligand")
        AllChem.MMFFOptimizeMolecule(mol)
    else:
        mol = Chem.AddHs(mol, addCoords=True)
    try:
        # flavor=4 -> write ATOM/HETATM by residue info; the ligand carries no
        # peptide residue info so RDKit emits HETATM records.
        Chem.MolToPDBFile(mol, str(dest), flavor=4)
    except Exception as exc:
        _fail("input", "sdf", f"SDF -> PDB write failed: {exc}")
    if not dest.is_file() or dest.stat().st_size < 32:
        _fail("input", "sdf", "SDF -> PDB conversion produced no output")
    return dest


# ===========================================================================
# Seed derivation (cross-shard independence)
# ===========================================================================


def shard_seed(job_id: str) -> int:
    """Derive a stable, distinct, bounded seed from the job id. Distinct child
    job ids -> distinct seeds -> independent shards (avoids the seed+job_id
    collision caveat by keeping job_id=0 and varying only the seed)."""
    if not job_id:
        return 42
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % 1_000_000


# ===========================================================================
# CLI construction
# ===========================================================================


def _rf3_enabled() -> bool:
    return os.environ.get("PROTEINA_RF3", "on").strip().lower() not in _RF3_OFF


def build_design_cmd(
    *,
    config_name: str,
    task_name: str,
    seed: int,
    nsamples: int | None,
    replicas: int | None,
    nsteps: int | None,
    run_name: str,
    rf3_on: bool,
) -> list[str]:
    """Assemble the `complexa design` invocation for one shard.

    Runs the full generate -> filter -> evaluate -> analyze pipeline (evaluate
    is what writes the reward CSV we parse). ++job_id=0 with a distinct ++seed;
    filter keeps every sample so all designs survive; a fresh run_name + cwd
    isolate the results-CSV early-exit.

    nsamples / replicas are pinned as explicit Hydra overrides. The keys are
    VERIFIED against configs/pipeline/binder/binder_generate.yaml @ 916eaaed:
    nsamples lives at generation.dataloader.dataset.nres.nsamples (default 4) and
    replicas at generation.search.best_of_n.replicas (default 2, algorithm
    best-of-n). Pinning them makes every variant yield exactly nsamples*replicas
    designs == the campaign chunk_size, regardless of the per-variant default.

    gen_njobs is pinned to 1 (one GPU per shard). That FORCES job_id=0: with
    njobs=1, split_by_job() gives any job_id>=1 zero samples, so cross-shard
    independence comes from a distinct ++seed (cfg.seed = cfg.seed + job_id, so
    seed+0 == our derived seed), never from job_id.

    The config path is passed RELATIVE (configs/<name>.yaml) because the caller
    runs from cwd=/opt/proteina — the same invocation upstream documents in each
    config header — so ./ckpts (weights), ./assets (target PDBs) and Hydra's
    config search path all resolve from the repo root.
    """
    config_path = f"configs/{config_name}.yaml"
    cmd = [
        COMPLEXA_BIN, "design", config_path,
        "++job_id=0",
        f"++base_config_name={config_name}",
        f"++seed={seed}",
        "++gen_njobs=1",
        f"++generation.task_name={task_name}",
        f"++run_name={run_name}",
        "++generation.filter.delete_non_top_n_samples=false",
        "++generation.filter.filter_samples_limit=1000",
    ]
    if nsamples:
        cmd.append(f"++generation.dataloader.dataset.nres.nsamples={nsamples}")
    if replicas:
        cmd.append(f"++generation.search.best_of_n.replicas={replicas}")
    if nsteps:
        cmd.append(f"++generation.args.nsteps={nsteps}")
    # No RF3 toggle is emitted: RF3 is enabled/disabled by whether `rf3folding`
    # is present in the config's reward_models block, not by a flag. The upstream
    # protein_binder config is AF2-only (no RF3 block), while the RF3-only
    # variants (ligand_binder / motif_ame) are hard-blocked pre-GPU in main()
    # when PROTEINA_RF3=off. So there is nothing to override here; rf3_on is kept
    # in the signature for the call-site contract + the main() hard-block.
    _ = rf3_on
    return cmd


def run_streaming(cmd: list[str], cwd: Path) -> int:
    """Run a subprocess, live-streaming stdout/stderr to Modal logs (never
    capture_output for long GPU work, per the Modal-subprocess memory)."""
    logger.info("cmd (cwd=%s): %s", cwd, " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(cwd), stdout=sys.stdout, stderr=sys.stderr, check=False)
    return result.returncode


# ===========================================================================
# Output parsing (tolerant — BUILD-TIME-VERIFY exact names at canary)
# ===========================================================================


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _pick(row: dict, names: tuple[str, ...]) -> Any:
    lowered = {k.lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return None


def find_reward_csv(run_dir: Path) -> Path | None:
    """Locate the per-design reward/results CSV under the shard run dir."""
    patterns = ("**/rewards_*.csv", "**/results_*.csv", "**/*reward*.csv", "**/*.csv")
    for pat in patterns:
        hits = sorted(glob.glob(str(run_dir / pat), recursive=True))
        if hits:
            return Path(hits[0])
    return None


def find_pdb_for(row: dict, run_dir: Path, idx: int, total_rows: int) -> Path | None:
    """Resolve the design PDB for a CSV row: an explicit path column first,
    else match a name column against the PDB glob, else fall back by index
    (only when the design-PDB count matches the row count, so the positional
    pairing is unambiguous)."""
    explicit = _pick(row, _PDB_PATH_COLUMNS)
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = run_dir / explicit
        if p.is_file():
            return p
    # Design PDBs only: exclude the (now-blocked) staged target input and the
    # filter's rejected-sample bucket so an index fallback can never mis-pair a
    # row's scores onto a non-design structure.
    all_pdbs = [
        p for p in sorted(glob.glob(str(run_dir / "**/*.pdb"), recursive=True))
        if "filtered_out_samples" not in p and Path(p).name not in ("target.pdb", "target_input")
    ]
    name = _pick(row, _PDB_NAME_COLUMNS)
    if name:
        stem = str(name)
        for p in all_pdbs:
            if stem in Path(p).name:
                return Path(p)
    # Index fallback only when the design count matches the row count (so the
    # positional pairing is meaningful); otherwise skip rather than mis-pair.
    if len(all_pdbs) == total_rows and idx < len(all_pdbs):
        logger.warning("row %d: matched PDB by index fallback (name match failed)", idx)
        return Path(all_pdbs[idx])
    return None


def parse_designs(run_dir: Path) -> list[dict]:
    """Parse the reward CSV into ranked design rows with a nested ``scores``
    dict. Returns [] when no CSV is found (caller treats as zero survivors)."""
    csv_path = find_reward_csv(run_dir)
    if csv_path is None:
        logger.warning("no reward CSV found under %s", run_dir)
        return []
    logger.info("parsing reward CSV: %s", csv_path)
    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    parsed: list[dict] = []
    for i, row in enumerate(rows):
        scores = {col: _num(_pick(row, names)) for col, names in _SCORE_COLUMNS.items()}
        # cluster_id is an int id, not a measurement.
        if scores.get("cluster_id") is not None:
            scores["cluster_id"] = int(scores["cluster_id"])
        parsed.append(
            {
                "_row_index": i,
                "name": str(_pick(row, _PDB_NAME_COLUMNS) or f"design_{i}")[:64],
                "total_reward": scores.get("total_reward"),
                "scores": scores,
            }
        )
    # Rank by total_reward desc (None at the bottom), mirroring filter.py.
    parsed.sort(key=lambda d: (d["total_reward"] is not None, d["total_reward"] or 0.0), reverse=True)
    for rank, d in enumerate(parsed):
        d["rank"] = rank
    return parsed


# ===========================================================================
# validate tier (free, CPU dry-run — the staging gate)
# ===========================================================================


def run_validate(config_dir: str, preset: str, task_name: str) -> None:
    """Free pre-flight / staging gate. Variant-agnostic checks, no GPU compute:
    (1) the proteinfoundation package imports (src-layout sane), (2) all three
    variant config files are present, (3) at least one model checkpoint is
    present on the mounted weights Volume (catches an unseeded / wrong-path
    weights mount — the HIGH-2 failure mode — before any paid search). A full
    reward-model load needs a GPU and is the P-1/P-2 canary's job, not this
    tier. Writes a PASS/FAIL smoke result and exits."""
    start = time.time()
    problems: list[str] = []
    try:
        import importlib  # noqa: PLC0415
        importlib.import_module("proteinfoundation.generate")
        importlib.import_module("proteinfoundation.filter")
    except Exception as exc:
        problems.append(f"package import failed: {exc}")
    for name in _ALL_CONFIGS:
        if not os.path.isfile(f"{config_dir}/{name}.yaml"):
            problems.append(f"missing config {name}.yaml under {config_dir}")
    if not glob.glob(f"{WEIGHTS_DIR}/**/*.ckpt", recursive=True):
        problems.append(f"no model checkpoint (*.ckpt) found under {WEIGHTS_DIR}")

    if problems:
        _write_result(
            {
                "status": "FAILED",
                "tier": "validate",
                "error": {"bucket": "validate", "check": "preflight", "detail": "; ".join(problems)},
                "provider_job_id": os.environ.get("JOB_ID", ""),
            }
        )
        logger.error("validate FAILED: %s", "; ".join(problems))
        sys.exit(1)

    _write_result(
        {
            "status": "COMPLETED",
            "tier": "validate",
            "preset": preset,
            "task_name": task_name,
            "designs_total": 0,
            "designs_completed": 0,
            "n_failures": 0,
            "designs": [],
            "candidates": [],
            "validate_ok": True,
            "runtime_seconds": int(time.time() - start),
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    logger.info("validate OK in %ds", int(time.time() - start))


# ===========================================================================
# Main
# ===========================================================================


def main() -> None:
    start = time.time()
    payload = parse_payload()

    job_spec = payload.get("job_spec") or {}
    preset = str(payload.get("tier") or os.environ.get("JOB_TIER") or "protein_binder").strip()
    config_name = str(job_spec.get("config_name") or "")
    task_name = str(job_spec.get("task_name") or "")
    rf3_required = bool(job_spec.get("rf3_required"))
    nsamples = int(job_spec.get("nsamples") or 4)
    replicas = int(job_spec.get("replicas") or 2)
    nsteps = job_spec.get("nsteps")

    webhook_url = os.environ.get("WEBHOOK_URL", "")
    job_id = os.environ.get("JOB_ID", "")
    job_token = payload.get("job_token", "") or os.environ.get("JOB_TOKEN", "")
    upload_endpoint = payload.get("upload_urls_endpoint", "")
    input_url = payload.get("input_presigned_url", "")

    rf3_on = _rf3_enabled()
    logger.info(
        "proteina shard: preset=%s config=%s task=%s rf3_required=%s rf3_on=%s",
        preset, config_name, task_name, rf3_required, rf3_on,
    )

    # --- validate tier: free CPU dry-run, no wallet, no GPU compute ----------
    if preset == "validate":
        run_validate(CONFIG_DIR, preset, task_name)
        return

    # --- RF3 kill-switch hard-block (pre-GPU) --------------------------------
    # ligand_binder / motif_ame score on RF3 only (no AF2 ligand protocol), so
    # with RF3 off they have no valid reward. Fail before spending any GPU.
    if rf3_required and not rf3_on:
        _fail(
            "preflight",
            "rf3",
            f"the {preset} variant scores on RoseTTAFold3, which is currently "
            "disabled (PROTEINA_RF3=off). No AlphaFold2 fallback exists for "
            "this variant. Re-enable RF3 or choose the protein_binder variant.",
        )

    if not config_name:
        _fail("preflight", "config", "job_spec.config_name is empty")
    if not upload_endpoint:
        _fail("preflight", "upload_urls_endpoint", "upload_urls_endpoint missing from payload")

    # Designs/shard is derived from the (overridden) generation profile so it
    # tracks the actual Hydra overrides, not a hardcoded 8.
    designs_total = nsamples * replicas

    # --- custom target (bring-your-own) HARD-BLOCK (pre-GPU) -----------------
    # The route stages an uploaded PDB/SDF, but the upstream custom-target
    # registration (`complexa target` add) is NOT yet wired into the shard CLI:
    # build_design_cmd selects the target only via ++generation.task_name, which
    # resolves a REPO-BUNDLED benchmark target. So a bring-your-own run would be
    # SILENTLY designed against the curated default task (billed GPU, wrong
    # science). Refuse before any GPU spend until a canary wires + verifies the
    # registration. Curated benchmark tasks are the launch path; download_target
    # / sdf_to_pdb stay below as the scaffolding for that fast-follow.
    if input_url:
        _fail(
            "input",
            "custom_target",
            "Bring-your-own targets are not enabled yet for Proteina-Complexa. "
            "Pick a curated benchmark task instead — the custom PDB/SDF target "
            "path lands after the initial canary verifies the registration.",
        )

    send_heartbeat(webhook_url, job_id, stage="loading_model", designs_total=designs_total)

    # Run from the repo root so the configs' RELATIVE paths resolve exactly as
    # upstream's own `complexa design configs/...` invocation: ./ckpts (weights
    # Volume), ./assets/target_data (bundled benchmark target PDBs), ./configs
    # (Hydra search path), ./inference (outputs). generate.py uses @hydra.main;
    # modern Hydra's job.chdir defaults off, so cwd stays here across all stages.
    work_dir = Path(PROTEINA_HOME)
    run_dir = work_dir / "inference"
    # Defeat the warm-container results-CSV early-exit: generate.py exits early if
    # ./inference/results_<config>_<job_id>.csv exists, and that name is keyed on
    # (config_name, job_id=0) — identical across same-variant shards. A reused
    # warm container would then re-emit the prior shard's designs. Modal runs one
    # input per container at a time (no concurrent inputs on this function), so
    # wiping ./inference at shard start is safe and isolates every shard.
    shutil.rmtree(run_dir, ignore_errors=True)

    seed = shard_seed(job_id)
    run_name = f"shard_{(job_id or 'x')[:12]}"
    cmd = build_design_cmd(
        config_name=config_name, task_name=task_name, seed=seed,
        nsamples=nsamples, replicas=replicas, nsteps=nsteps,
        run_name=run_name, rf3_on=rf3_on,
    )
    send_heartbeat(webhook_url, job_id, stage="searching", designs_total=designs_total)

    try:
        rc = run_streaming(cmd, work_dir)
    except FileNotFoundError:
        _fail("search", "complexa", f"`{COMPLEXA_BIN}` binary not found on PATH")
    if rc != 0:
        _fail("search", "complexa", f"`complexa design` exited {rc}")

    designs = parse_designs(run_dir)
    if not designs:
        # A shard that legitimately produced no survivors still COMPLETES
        # with zero candidates — the campaign pools survivors across shards,
        # and delivered-only billing releases this shard's hold.
        runtime = int(time.time() - start)
        _write_result(
            {
                "status": "COMPLETED",
                "tier": preset,
                "designs_total": designs_total,
                "designs_completed": 0,
                "n_failures": 0,
                "designs": [],
                "candidates": [],
                "runtime_seconds": runtime,
                "provider_job_id": job_id,
            }
        )
        send_heartbeat(webhook_url, job_id, stage="complete", designs_total=designs_total)
        logger.info("shard produced 0 survivors in %ds", runtime)
        return

    # --- upload + stream each design -----------------------------------------
    out_designs: list[dict] = []
    out_candidates: list[dict] = []
    n_failures = 0
    n_rows = len(designs)
    for d in designs:
        rank = d["rank"]
        pdb_path = find_pdb_for(d, run_dir, d["_row_index"], n_rows)
        if pdb_path is None:
            n_failures += 1
            logger.warning("design rank %d: no PDB file matched — skipping", rank)
            continue
        basename = f"design_{rank:03d}.pdb"
        pdb_key = f"designs/{basename}"
        try:
            pdb_bytes = pdb_path.read_bytes()
            urls = request_upload_urls(upload_endpoint, job_token, [basename])
            upload_pdb(urls[basename], pdb_bytes)
        except Exception as exc:
            n_failures += 1
            logger.warning("design rank %d: upload failed (%s) — skipping", rank, exc)
            continue

        scores = d["scores"]
        design_entry = {
            "rank": rank,
            "name": d["name"],
            "pdb_key": pdb_key,
            # flat copies for the results template + classifiers
            "total_reward": scores.get("total_reward"),
            "af2_iptm": scores.get("af2_iptm"),
            "af2_plddt": scores.get("af2_plddt"),
            "rf3_score": scores.get("rf3_score"),
            "binder_scrmsd": scores.get("binder_scrmsd"),
            "cluster_id": scores.get("cluster_id"),
        }
        out_designs.append(design_entry)
        out_candidates.append(
            {"rank": rank, "name": d["name"], "pdb_key": pdb_key, "scores": scores}
        )
        # Heartbeat new_candidate keys match webhook _sanitize_candidate.
        send_heartbeat(
            webhook_url, job_id, stage="searching",
            designs_completed=len(out_designs), designs_total=designs_total,
            new_candidate={
                "rank": rank,
                "name": d["name"],
                "pdb_key": pdb_key,
                "total_reward": scores.get("total_reward"),
                "af2_iptm": scores.get("af2_iptm"),
                "af2_plddt": scores.get("af2_plddt"),
                "rf3_score": scores.get("rf3_score"),
                "binder_scrmsd": scores.get("binder_scrmsd"),
                "cluster_id": scores.get("cluster_id"),
            },
        )
        logger.info("  -> rank %d reward=%s pdb=%s", rank, scores.get("total_reward"), pdb_key)

    runtime = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": preset,
            "designs_total": designs_total,
            "designs_completed": len(out_designs),
            "n_failures": n_failures,
            "designs": out_designs,
            "candidates": out_candidates,
            "runtime_seconds": runtime,
            "provider_job_id": job_id,
        }
    )
    send_heartbeat(
        webhook_url, job_id, stage="complete",
        designs_completed=len(out_designs), designs_total=designs_total,
    )
    logger.info(
        "shard complete — %d/%d designs, %d failures, runtime=%ds",
        len(out_designs), designs_total, n_failures, runtime,
    )


if __name__ == "__main__":
    main()
