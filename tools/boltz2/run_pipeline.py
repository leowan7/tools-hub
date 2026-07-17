"""Modal entrypoint for Boltz-2 cofold validation (antibody-trained, single-sequence).

Reads job configuration from the ``JOB_PAYLOAD`` env var (same shape as
the MPNN / AF2 / ColabFold / ESMFold pipelines), folds each binder
against the supplied antigen, uploads per-design PDBs via presigned PUT
URLs requested from the hub, and emits heartbeats with the per-design
``new_candidate`` for the live status page. Writes the final summary to
``/tmp/smoke_results.json``. The Modal wrapper returns this file inline
via the function return value — see ``tools/boltz2/modal_app.py``.

Two presets:

- ``standalone`` (default) — single-sequence cofold (YAML ``msa: empty``
  per chain). The right choice for designed sequences (MPNN, RFantibody,
  BindCraft, etc.) where there is no informative MSA. ~15 s / design on
  A100-40GB once warm.
- ``msa_server`` — Boltz fetches MSAs from the public ColabFold MMseqs2
  endpoint via ``--use_msa_server``. ~3 min / design. Better for natural
  / near-native sequences; for designed sequences the MSA is usually
  dominated by the closest natural homologues and the result barely
  differs from ``standalone``.

Environment variables (set by ``tools/boltz2/modal_app.py``):

    JOB_PAYLOAD     JSON: job_spec + input_presigned_url + upload_urls_endpoint + tier
    WEBHOOK_URL     URL to POST results to (heartbeats derive /webhooks/heartbeat from it)
    JOB_ID          tool_jobs row id (used for log prefixing + heartbeat body)
    JOB_TOKEN       Job-specific auth token (heartbeat new_candidate gate)
    JOB_TIER        ``standalone`` | ``msa_server``

Output shape (``/tmp/smoke_results.json``)::

    {
      "status": "COMPLETED",
      "tier": "standalone",
      "designs_total": N,
      "designs_completed": N,
      "n_failures": 0,
      "designs": [
        {
          "rank": 0,
          "name": "design_0",
          "pdb_key": "design_0_complex.pdb",
          "iptm": 0.78,
          "ptm": 0.74,
          "complex_plddt": 0.87,
          "complex_iplddt": 0.82,
          "n_hotspot_contacts": 5,
          "n_hotspots": 7,
          "contacted_residues": [55, 56, 57, 71, 72],
          "antigen_chain": "B",
          "filter_status": "strict_pass",
          "runtime_seconds": 67
        },
        ...
      ],
      "antigen_length": 117,
      "hotspots_requested": [55, 56, 57, 71, 72, 73, 74],
      "runtime_seconds": 412,
      "provider_job_id": "<job_id>"
    }
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
logger = logging.getLogger("boltz2_pipeline")


SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
# The COMPLETE work tree, tarred at teardown for the Modal wrapper to park on the
# raw Volume. Fixed path, outside the work dir, so the wrapper needs no coordination.
RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"
HOTSPOT_CUTOFF_ANGSTROM = 5.0
BOLTZ_BIN = os.environ.get("BOLTZ_BIN", "boltz")

# Acceptance bar for the strict_pass classification (mirrors the
# verification gate in the plan).
STRICT_PLDDT = 0.85
STRICT_IPTM = 0.7
STRICT_HOTSPOT_CONTACTS_MIN = 4  # > 4 contacts == strict_pass
SOFT_IPTM = 0.5


# ===========================================================================
# Three-letter -> one-letter table (PDB residue parsing)
# ===========================================================================

_AA3TO1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V",
}


# ===========================================================================
# Result writer
# ===========================================================================


def _write_result(payload: dict[str, Any]) -> None:
    """Write the canonical smoke-result JSON. Overwrites any prior file."""
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except OSError as exc:
        logger.error("Could not write %s: %s", SMOKE_RESULTS_PATH, exc)


def _fail(bucket: str, check: str, detail: str) -> None:
    """Write a FAILED result and exit 1."""
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
# Heartbeat + upload helpers (mirror the composite-pipeline contract)
# ===========================================================================


def _heartbeat_url(webhook_url: str) -> str:
    """Derive the /webhooks/heartbeat URL from the main webhook URL."""
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
    """Fire-and-forget heartbeat. Never raises — the run survives a flaky
    webhook hop. Schema matches ``llm-proteinDesigner/docker/bindcraft/run_pipeline.py``.
    """
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
    """Ask the hub for presigned PUT URLs keyed by filename."""
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
    """PUT the PDB bytes to a presigned URL with chemical/x-pdb."""
    resp = requests.put(
        url,
        data=pdb_bytes,
        headers={"Content-Type": "chemical/x-pdb"},
        timeout=120,
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"upload failed: HTTP {resp.status_code} {resp.text[:200]}"
        )


# ===========================================================================
# Payload parsing + input PDB resolution
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


def download_antigen_pdb(url: str, dest: Path) -> Path:
    """Stream the antigen PDB from the presigned GET URL."""
    if not url:
        _fail("input", "url", "input_presigned_url missing")
    try:
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=32768):
                    if chunk:
                        fh.write(chunk)
    except Exception as exc:
        _fail("input", "download", f"antigen PDB download failed: {exc}")
    if not dest.is_file() or dest.stat().st_size < 100:
        _fail("input", "download", "downloaded antigen PDB is empty or tiny")
    return dest


# ===========================================================================
# PDB parsing helpers (lifted verbatim from scratch/boltz_modal/app.py
# so we keep one canonical implementation of contact-counting logic)
# ===========================================================================


def chain_seq(pdb_path: Path, chain: str = "A") -> str:
    """Extract the one-letter sequence for a given chain from a PDB file."""
    seq: list[str] = []
    seen: set[int] = set()
    with open(pdb_path) as fh:
        for line in fh:
            if (
                line.startswith("ATOM")
                and line[12:16].strip() == "CA"
                and line[21] == chain
            ):
                try:
                    rs = int(line[22:26])
                except ValueError:
                    continue
                if rs not in seen:
                    seen.add(rs)
                    seq.append(_AA3TO1.get(line[17:20].strip(), "X"))
    return "".join(seq)


def _parse_pdb_chains(pdb_text: str) -> dict:
    chains: dict[str, dict[int, list[tuple[float, float, float]]]] = {}
    order: dict[str, list[int]] = {}
    for line in pdb_text.splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        elem = line[76:78].strip()
        name = line[12:16].strip()
        if elem == "H" or (not elem and name.startswith("H")):
            continue
        ch = line[21]
        try:
            rs = int(line[22:26])
            xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError:
            continue
        if ch not in chains:
            chains[ch] = {}
            order[ch] = []
        if rs not in chains[ch]:
            chains[ch][rs] = []
            order[ch].append(rs)
        chains[ch][rs].append(xyz)
    return {ch: [(rs, chains[ch][rs]) for rs in order[ch]] for ch in chains}


def hotspot_contacts(
    pdb_text: str,
    hotspot_positions: list[int],
    antigen_len: int,
    cutoff: float = HOTSPOT_CUTOFF_ANGSTROM,
) -> dict:
    """Identify the antigen chain by closest length match, then count which of
    the requested hotspot positions (1-indexed on the antigen) have at least
    one heavy atom within ``cutoff`` of a binder atom.
    """
    chains = _parse_pdb_chains(pdb_text)
    if len(chains) < 2:
        return {
            "error": f"expected 2 chains, got {list(chains)}",
            "antigen_chain": "",
            "antigen_res": 0,
            "contacted": [],
            "n_contacted": 0,
            "n_hotspots": len(hotspot_positions),
        }
    ag = min(chains, key=lambda c: abs(len(chains[c]) - antigen_len))
    binder_atoms = [
        xyz
        for c, residues in chains.items()
        if c != ag
        for _, atoms in residues
        for xyz in atoms
    ]
    ag_res = chains[ag]
    c2 = cutoff * cutoff
    contacted: list[int] = []
    for p in hotspot_positions:
        idx = p - 1
        if idx < 0 or idx >= len(ag_res):
            continue
        _, atoms = ag_res[idx]
        hit = any(
            (ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2 <= c2
            for (ax, ay, az) in atoms
            for (bx, by, bz) in binder_atoms
        )
        if hit:
            contacted.append(p)
    return {
        "antigen_chain": ag,
        "antigen_res": len(ag_res),
        "contacted": contacted,
        "n_contacted": len(contacted),
        "n_hotspots": len(hotspot_positions),
    }


# ===========================================================================
# Boltz invocation
# ===========================================================================


def make_yaml_sseq(binder_seq: str, antigen_seq: str) -> str:
    """Single-sequence YAML: ``msa: empty`` per chain."""
    return (
        "version: 1\n"
        "sequences:\n"
        "  - protein:\n"
        "      id: A\n"
        f"      sequence: {binder_seq}\n"
        "      msa: empty\n"
        "  - protein:\n"
        "      id: B\n"
        f"      sequence: {antigen_seq}\n"
        "      msa: empty\n"
    )


def make_yaml_msa_server(binder_seq: str, antigen_seq: str) -> str:
    """MSA-server YAML: omit ``msa`` per chain so Boltz fetches MSAs at
    runtime when paired with ``--use_msa_server``."""
    return (
        "version: 1\n"
        "sequences:\n"
        "  - protein:\n"
        "      id: A\n"
        f"      sequence: {binder_seq}\n"
        "  - protein:\n"
        "      id: B\n"
        f"      sequence: {antigen_seq}\n"
    )


def run_boltz(yaml_path: Path, out_dir: Path, msa_server: bool) -> int:
    """Run one ``boltz predict``. stdout/stderr live-stream to Modal logs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        BOLTZ_BIN,
        "predict",
        str(yaml_path),
        "--out_dir", str(out_dir),
        "--no_kernels",
        "--output_format", "pdb",
        "--override",
    ]
    if msa_server:
        cmd.append("--use_msa_server")
    logger.info("boltz cmd: %s", " ".join(cmd))
    # Live-stream — never capture_output for long-running GPU subprocesses
    # (per the Modal subprocess debugging memory).
    result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr, check=False)
    return result.returncode


def collect_outputs(out_dir: Path) -> tuple[Path | None, dict]:
    """Glob the Boltz output directory for the predicted PDB + confidence JSON."""
    pdb_files = sorted(glob.glob(f"{out_dir}/**/*.pdb", recursive=True))
    conf_files = sorted(glob.glob(f"{out_dir}/**/confidence*.json", recursive=True))
    pdb_path = Path(pdb_files[0]) if pdb_files else None
    conf: dict = {}
    if conf_files:
        try:
            with open(conf_files[0]) as fh:
                conf = json.load(fh)
        except Exception as exc:
            logger.warning("could not parse confidence json: %s", exc)
    return pdb_path, conf


# ===========================================================================
# Classification
# ===========================================================================


def classify(iptm: float | None, plddt: float | None, n_contacts: int) -> str:
    """strict_pass | soft_pass | fail.

    Mirrors the acceptance bar in the verification plan: a strict-pass
    Boltz-2 fold is one where all three confidence channels agree the
    complex is real.
    """
    if iptm is None or plddt is None:
        return "fail"
    if (
        iptm >= STRICT_IPTM
        and plddt >= STRICT_PLDDT
        and n_contacts >= STRICT_HOTSPOT_CONTACTS_MIN
    ):
        return "strict_pass"
    if iptm >= SOFT_IPTM:
        return "soft_pass"
    return "fail"


def _num(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Raw output capture
# ===========================================================================


def archive_raw_outputs(work_dir: str, dest: str = RAW_ARCHIVE_PATH) -> None:
    """Tar the ENTIRE work tree to ``dest`` before teardown destroys it.

    A container must never decide which fields are worth keeping. This pipeline
    keeps 4 confidence scalars out of Boltz's ``confidence*.json`` and drops the
    rest of the tree — the per-design YAML, the PA(E)/PDE npz, the extra ranked
    models, the MSA, and every artefact of a fold that failed before it produced
    a PDB. Everything not archived here dies with the container and is
    recoverable only by re-paying for the GPU. That is how ``design_iptm`` was
    lost on 460 designs. Re-parsing a tar locally is free; re-running is not.

    Unconditional by design: it is not gated on success or on candidates. A run
    that folded nothing uploads nothing and is exactly the run whose tree you
    need. Best-effort by design: capture must never fail the run, so problems
    are logged and never raised — a crash before output is written is precisely
    when the diagnostics matter most.
    """
    try:
        if not os.path.isdir(work_dir):
            logger.warning(
                "raw capture: %s is not a directory — nothing to archive", work_dir,
            )
            return
        root = os.path.abspath(work_dir)
        dest_abs = os.path.abspath(dest)
        # The tar must never be written inside the tree it archives, or it tars
        # itself. RAW_ARCHIVE_PATH sits in /tmp while work_dir is /tmp/boltz2_*/,
        # so this cannot trip today — it is here so a future caller passing a dest
        # under work_dir gets a log line instead of a self-referential archive.
        if os.path.commonpath([dest_abs, root]) == root:
            logger.error(
                "raw capture: refusing to write %s inside the tree it archives (%s)",
                dest_abs, root,
            )
            return
        # Stream to a file, never io.BytesIO: ~1x peak RSS instead of ~3-4x.
        with tarfile.open(dest_abs, "w:gz") as tf:
            tf.add(root, arcname=os.path.basename(root) or "work")
        logger.info(
            "raw capture: archived %s -> %s (%.1f MB)",
            root, dest_abs, os.path.getsize(dest_abs) / 1e6,
        )
    except Exception as exc:
        logger.warning(
            "raw capture failed (non-fatal): %s: %s", type(exc).__name__, exc,
        )
        # A crash mid-write (e.g. ENOSPC) can leave a truncated but still-openable .tgz at
        # the destination; the wrapper parks whatever exists. Remove the partial so a failed
        # capture parks NOTHING rather than a tar that reports success but cannot be read.
        try:
            if os.path.exists(dest_abs):
                os.remove(dest_abs)
        except OSError:
            pass


# ===========================================================================
# Main
# ===========================================================================


def main() -> None:
    start = time.time()
    payload = parse_payload()

    job_spec = payload.get("job_spec") or {}
    tier = str(payload.get("tier") or "standalone").lower()
    if tier not in {"standalone", "msa_server"}:
        # Defensive — adapter validate() already restricts the tier, but
        # the pipeline may be invoked directly (e.g. modal run).
        tier = "standalone"
    msa_server = tier == "msa_server"

    binders = job_spec.get("binder_sequences") or []
    if not binders:
        _fail("input", "binders", "no binder sequences in job_spec")

    antigen_chain = str(job_spec.get("antigen_chain") or "A").strip() or "A"
    hotspots = [int(x) for x in (job_spec.get("hotspot_residues") or [])]

    webhook_url = os.environ.get("WEBHOOK_URL", "")
    job_id = os.environ.get("JOB_ID", "")
    job_token = payload.get("job_token", "") or os.environ.get("JOB_TOKEN", "")
    upload_endpoint = payload.get("upload_urls_endpoint", "")
    if not upload_endpoint:
        _fail(
            "preflight",
            "upload_urls_endpoint",
            "upload_urls_endpoint missing from payload — pilot tier requires "
            "the web flow to populate it",
        )

    designs_total = len(binders)
    logger.info(
        "boltz2 starting: tier=%s designs=%d hotspots=%s antigen_chain=%s",
        tier, designs_total, hotspots, antigen_chain,
    )
    send_heartbeat(
        webhook_url, job_id,
        stage="loading_model",
        designs_completed=0,
        designs_total=designs_total,
    )

    with tempfile.TemporaryDirectory(prefix="boltz2_", dir="/tmp") as _td:
        workdir = Path(_td)
        try:
            antigen_pdb = download_antigen_pdb(
                payload.get("input_presigned_url") or "",
                workdir / "antigen.pdb",
            )
            antigen_seq = chain_seq(antigen_pdb, chain=antigen_chain)
            if not antigen_seq:
                _fail(
                    "input",
                    "antigen_chain",
                    f"chain {antigen_chain!r} produced 0 residues in the uploaded PDB",
                )
            antigen_length = len(antigen_seq)
            logger.info(
                "antigen %s: %d residues parsed from uploaded PDB",
                antigen_chain, antigen_length,
            )

            send_heartbeat(
                webhook_url, job_id,
                stage="folding",
                designs_completed=0,
                designs_total=designs_total,
            )

            yaml_factory = make_yaml_msa_server if msa_server else make_yaml_sseq

            designs_out: list[dict] = []
            n_failures = 0

            for i, binder in enumerate(binders):
                name = str(binder.get("name") or f"design_{i}").strip() or f"design_{i}"
                sequence = (binder.get("sequence") or "").strip().upper()
                if not sequence:
                    n_failures += 1
                    logger.warning("design %d (%s): empty sequence — skipping", i, name)
                    continue

                design_start = time.time()
                design_workdir = workdir / f"d_{i:03d}"
                design_workdir.mkdir(parents=True, exist_ok=True)
                yaml_path = design_workdir / f"{name}.yaml"
                yaml_path.write_text(yaml_factory(sequence, antigen_seq))
                out_dir = design_workdir / "out"

                logger.info(
                    "=== folding %d/%d %s (binder=%d aa, antigen=%d aa, msa=%s) ===",
                    i + 1, designs_total, name, len(sequence), antigen_length, msa_server,
                )
                rc = run_boltz(yaml_path, out_dir, msa_server=msa_server)
                if rc != 0:
                    n_failures += 1
                    logger.warning("design %s: boltz exited %d", name, rc)
                    continue

                pdb_path, conf = collect_outputs(out_dir)
                if pdb_path is None:
                    n_failures += 1
                    logger.warning("design %s: no PDB emitted", name)
                    continue

                pdb_text = pdb_path.read_text()
                contacts = hotspot_contacts(
                    pdb_text, hotspots, antigen_len=antigen_length,
                )

                iptm = _num(conf.get("iptm") or conf.get("complex_iptm"))
                ptm = _num(conf.get("ptm") or conf.get("complex_ptm"))
                plddt = _num(conf.get("complex_plddt") or conf.get("plddt"))
                iplddt = _num(conf.get("complex_iplddt"))
                filter_status = classify(iptm, plddt, contacts["n_contacted"])

                pdb_key = f"{name}_complex.pdb"
                try:
                    urls = request_upload_urls(upload_endpoint, job_token, [pdb_key])
                    upload_pdb(urls[pdb_key], pdb_text.encode("utf-8"))
                except Exception as exc:
                    n_failures += 1
                    logger.warning(
                        "design %s: upload failed (%s) — skipping", name, exc,
                    )
                    continue

                design_entry = {
                    "rank": i,
                    "name": name,
                    "pdb_key": pdb_key,
                    "iptm": iptm,
                    "ptm": ptm,
                    "complex_plddt": plddt,
                    "complex_iplddt": iplddt,
                    "n_hotspot_contacts": contacts["n_contacted"],
                    "n_hotspots": contacts["n_hotspots"],
                    "contacted_residues": contacts["contacted"],
                    "antigen_chain": contacts["antigen_chain"],
                    "filter_status": filter_status,
                    "runtime_seconds": int(time.time() - design_start),
                }
                designs_out.append(design_entry)

                send_heartbeat(
                    webhook_url, job_id,
                    stage="folding",
                    designs_completed=i + 1,
                    designs_total=designs_total,
                    new_candidate=design_entry,
                )
                logger.info(
                    "  -> %s iptm=%s plddt=%s contacts=%d/%d %s",
                    name, iptm, plddt,
                    contacts["n_contacted"], contacts["n_hotspots"],
                    filter_status,
                )

        finally:
            # Ship the COMPLETE tree home BEFORE TemporaryDirectory deletes it.
            # In the ``finally`` because every path that skips the normal exit —
            # a ``_fail`` sys.exit, an unexpected raise, a zero-design run — is a
            # path whose tree is worth more, not less.
            archive_raw_outputs(str(workdir))

    runtime_seconds = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": tier,
            "designs_total": designs_total,
            "designs_completed": len(designs_out),
            "n_failures": n_failures,
            "designs": designs_out,
            "antigen_length": antigen_length,
            "hotspots_requested": hotspots,
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
        "pipeline ok — %d/%d designs folded, %d failures, runtime=%ds",
        len(designs_out), designs_total, n_failures, runtime_seconds,
    )


if __name__ == "__main__":
    main()
