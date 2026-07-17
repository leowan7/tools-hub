"""Modal entrypoint for IgGM antibody / nanobody design + structure prediction.

Reads job configuration from ``JOB_PAYLOAD`` (same env-var contract as the
other atomic tools: boltz2 / af2 / mpnn), runs IgGM's ``design.py`` once,
uploads the designed complex PDBs (+ FASTA / CSV / plot artifacts) via
presigned PUT URLs, emits ``new_candidate`` heartbeats, and writes the
final summary to ``/tmp/smoke_results.json``. The Modal wrapper returns
that file inline (see ``tools/iggm/modal_app.py``).

Two things this pipeline is responsible for that the adapter cannot do
(it has no PDB at validate time), both correctness-critical:

1. **Antigen is single-source from the PDB.** The user pastes only the
   antibody chains (``>H`` [+ ``>L``]); this pipeline extracts the selected
   antigen chain's sequence from the uploaded PDB and appends it as the
   last FASTA record (IgGM reads the antigen as ``ids[-1]`` /
   ``sequences[-1]`` and loads it from ``--antigen`` by that chain id).
2. **Epitope PDB-number -> 1-based sequential-position conversion.** IgGM's
   ``--epitope`` indices are 1-based positions along the antigen chain
   *sequence*, not PDB author residue numbers. The structure picker returns
   PDB residue numbers; we map them to sequential positions (insertion-code
   aware) here. Passing raw PDB numbers through would silently produce the
   wrong epitope.

BUILD-TIME VERIFICATION (staging smoke I-1, pinned commit): the exact IgGM
output directory layout, per-design score columns, the checkpoint cache
dir, and whether ``design.py`` accepts a single ``--fasta`` file (the
README examples pass a single file). The I-1 smoke asserts the predicted
antibody contacts the requested epitope, which end-to-end validates items
1 + 2 above.
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
logger = logging.getLogger("iggm_pipeline")

SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
# Where the complete raw work tree is tarred for the Modal wrapper to park on
# the raw Volume. Fixed path (the wrapper looks here unconditionally) and
# deliberately OUTSIDE the /tmp/iggm_*/ work dir it archives.
RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"
# IgGM repo root inside the image (design.py imports the IgGM package, so we
# run from here). Confirmed against the Dockerfile.
IGGM_DIR = os.environ.get("IGGM_DIR", "/opt/IgGM")
CONTACT_CUTOFF_ANGSTROM = 5.0
ANTIGEN_LEN_HARD_CAP = int(os.environ.get("IGGM_ANTIGEN_LEN_HARD_CAP", "1000"))

_AA3TO1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V",
}


# ===========================================================================
# Result writer + fail-fast
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


# ===========================================================================
# Raw output capture
# ===========================================================================


def _ship_raw(work_dir: Path, dest: str = RAW_ARCHIVE_PATH) -> None:
    """Tar the COMPLETE work tree to ``dest`` before the work dir is destroyed.

    A container must never decide which fields are worth keeping. This pipeline
    keeps a handful of numbers per design (epitope contacts) and uploads the
    design PDBs; IgGM's own per-design score CSVs, plots, logs, the assembled
    FASTAs and the resolved inputs all die with the temp dir. Anything not
    shipped here is recoverable only by paying for the GPU again — that is how
    ``design_iptm`` was lost on 460 boltzgen designs. Decide LOCALLY, where
    re-parsing is free.

    Unconditional by design: not gated on candidates, on success, or on what
    was uploaded. A run that crashed or produced zero designs ships nothing
    today and is exactly the run whose tree you need.

    Best-effort: capture must never fail the run, so every problem is logged
    and swallowed. Never raises.
    """
    try:
        src = os.path.abspath(str(work_dir))
        if not os.path.isdir(src):
            logger.warning("[raw] no work dir to archive (nothing was created?): %s", src)
            return
        dst = os.path.abspath(dest)
        # The tar must never be written inside the tree it archives, or it tars
        # itself. /tmp/raw_archive.tgz is outside /tmp/iggm_*/ by construction;
        # this guard is here so a future change to either path fails loudly in
        # the log instead of silently producing a self-referential archive.
        if dst == src or dst.startswith(src + os.sep):
            logger.warning(
                "[raw] refusing to archive: dest %s is inside the tree it archives (%s)", dst, src
            )
            return
        # Stream to a file, never io.BytesIO — the latter costs ~3-4x peak RSS
        # on a multi-hundred-MB tree, this costs ~1x.
        with tarfile.open(dst, "w:gz") as tf:
            tf.add(src, arcname=os.path.basename(src.rstrip(os.sep)) or "work")
        logger.info("[raw] archived %s -> %s (%.1f MB)", src, dst, os.path.getsize(dst) / 1e6)
    except Exception as exc:
        logger.warning("[raw] capture failed (non-fatal): %s: %s", type(exc).__name__, exc)
        # A crash mid-write (e.g. ENOSPC) can leave a truncated but still-openable .tgz at
        # the destination; the wrapper parks whatever exists. Remove the partial so a failed
        # capture parks NOTHING rather than a tar that reports success but cannot be read.
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except OSError:
            pass


# ===========================================================================
# Heartbeat + upload helpers (contract identical to boltz2)
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


def upload_file(url: str, data: bytes, content_type: str) -> None:
    resp = requests.put(url, data=data, headers={"Content-Type": content_type}, timeout=120)
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"upload failed: HTTP {resp.status_code} {resp.text[:200]}")


# ===========================================================================
# Payload + antigen PDB
# ===========================================================================


def parse_payload() -> dict[str, Any]:
    raw = os.environ.get("JOB_PAYLOAD", "").strip()
    if not raw:
        _fail("preflight", "env", "JOB_PAYLOAD env var is empty")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail("preflight", "env", f"JOB_PAYLOAD is not valid JSON: {exc}")
    return {}


def download_antigen_pdb(url: str, dest: Path) -> Path:
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
# Antigen chain extraction + epitope conversion (insertion-code aware)
# ===========================================================================


def antigen_chain_info(pdb_path: Path, chain: str) -> dict:
    """Return the antigen chain's one-letter sequence in PDB order plus the
    PDB-residue-number -> 1-based sequential-position map used for the
    epitope conversion.

    Residue identity is ``(resSeq, iCode)`` so insertion codes (heavy in
    antibody/antigen numbering) don't collapse distinct residues. The
    ``resnum_to_pos`` map keys on the integer ``resSeq`` and points at the
    1-based sequential position of the FIRST residue with that number (the
    common case; genuine insertion-code ambiguity is rare for surface
    epitopes and is surfaced, not silently mis-mapped).
    """
    seq: list[str] = []
    seen: set[tuple[int, str]] = set()
    resnum_to_pos: dict[int, int] = {}
    with open(pdb_path) as fh:
        for line in fh:
            if not line.startswith("ATOM"):
                continue
            if line[12:16].strip() != "CA" or line[21] != chain:
                continue
            try:
                resseq = int(line[22:26])
            except ValueError:
                continue
            icode = line[26]
            key = (resseq, icode)
            if key in seen:
                continue
            seen.add(key)
            seq.append(_AA3TO1.get(line[17:20].strip(), "X"))
            pos = len(seq)  # 1-based sequential position
            if resseq not in resnum_to_pos:
                resnum_to_pos[resseq] = pos
    return {"seq": "".join(seq), "resnum_to_pos": resnum_to_pos, "n_res": len(seq)}


def convert_epitope(
    epitope_pdb_resnums: list[int], resnum_to_pos: dict[int, int]
) -> tuple[list[int], list[int]]:
    """Map picked PDB residue numbers to IgGM 1-based sequential positions.

    Returns ``(positions, missing)`` where ``missing`` are PDB numbers not
    present in the antigen chain (a preflight failure — the user picked a
    residue that isn't on the selected chain)."""
    positions: list[int] = []
    missing: list[int] = []
    for rn in epitope_pdb_resnums:
        pos = resnum_to_pos.get(rn)
        if pos is None:
            missing.append(rn)
        else:
            positions.append(pos)
    return positions, missing


# ---- contact counting (for the I-1 QC assertion; adapted from boltz2) ----


def _parse_pdb_chains(pdb_text: str) -> dict:
    # ATOM-only, matching ``antigen_chain_info`` so the antigen residue
    # ordering (and therefore the 1-based epitope positions) is consistent
    # across the input-PDB extraction and the output-complex contact check.
    chains: dict[str, list] = {}
    order: dict[str, list] = {}
    tmp: dict[str, dict] = {}
    for line in pdb_text.splitlines():
        if not line.startswith("ATOM"):
            continue
        elem = line[76:78].strip()
        name = line[12:16].strip()
        if elem == "H" or (not elem and name.startswith("H")):
            continue
        ch = line[21]
        try:
            resseq = int(line[22:26])
            xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError:
            continue
        key = (resseq, line[26])
        if ch not in tmp:
            tmp[ch] = {}
            order[ch] = []
        if key not in tmp[ch]:
            tmp[ch][key] = []
            order[ch].append(key)
        tmp[ch][key].append(xyz)
    for ch in tmp:
        chains[ch] = [(k, tmp[ch][k]) for k in order[ch]]
    return chains


def epitope_contacts(pdb_text: str, antigen_length: int, positions: list[int]) -> dict:
    """Count how many of the 1-based epitope positions on the antigen are
    contacted by any antibody atom (heavy atom within cutoff).

    The antigen chain in the OUTPUT complex is identified by closest length
    match to the input antigen length (not by chain id) so a chain-id rename
    in IgGM's output does not silently zero out every design's contacts
    (mirrors the boltz2 reference)."""
    chains = _parse_pdb_chains(pdb_text)
    if len(chains) < 2 or not positions:
        return {"n_contacted": 0, "n_epitope": len(positions), "contacted": []}
    ag = min(chains, key=lambda c: abs(len(chains[c]) - antigen_length))
    ag_res = chains[ag]
    other_atoms = [
        xyz for c, res in chains.items() if c != ag
        for _, atoms in res for xyz in atoms
    ]
    c2 = CONTACT_CUTOFF_ANGSTROM ** 2
    contacted: list[int] = []
    for p in positions:
        idx = p - 1
        if idx < 0 or idx >= len(ag_res):
            continue
        _, atoms = ag_res[idx]
        if any(
            (ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2 <= c2
            for (ax, ay, az) in atoms for (bx, by, bz) in other_atoms
        ):
            contacted.append(p)
    return {"n_contacted": len(contacted), "n_epitope": len(positions), "contacted": contacted}


# ===========================================================================
# FASTA assembly + IgGM invocation
# ===========================================================================


def write_fasta(records: list[dict[str, str]], antigen_header: str, antigen_seq: str, dest: Path) -> None:
    """Write the IgGM FASTA: antibody chains first, antigen LAST (IgGM reads
    ids[-1]/sequences[-1] as the antigen). The antigen header equals the PDB
    antigen chain id so IgGM loads the right chain from --antigen."""
    lines: list[str] = []
    for r in records:
        lines.append(f">{r['header']}")
        lines.append(r["sequence"])
    lines.append(f">{antigen_header}")
    lines.append(antigen_seq)
    dest.write_text("\n".join(lines) + "\n")


def run_iggm(
    fasta_path: Path,
    antigen_pdb: Path,
    out_dir: Path,
    run_task: str,
    epitope_positions: list[int],
    max_antigen_size: int,
    num_samples: int,
    fasta_origin_path: Path | None,
) -> int:
    """Run one ``design.py``. stdout/stderr live-stream to Modal logs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "python", "-u", "design.py",
        "--fasta", str(fasta_path),
        "--antigen", str(antigen_pdb),
        "--output", str(out_dir),
        "--max_antigen_size", str(max_antigen_size),
        "--num_samples", str(num_samples),
    ]
    # run_task "design" is the argparse default — omit it; pass the others.
    if run_task and run_task != "design":
        cmd += ["--run_task", run_task]
    if epitope_positions:
        cmd += ["--epitope"] + [str(p) for p in epitope_positions]
    if fasta_origin_path is not None:
        cmd += ["--fasta_origin", str(fasta_origin_path)]
    logger.info("iggm cmd: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, cwd=IGGM_DIR, stdout=sys.stdout, stderr=sys.stderr, check=False
    )
    return result.returncode


def collect_design_pdbs(out_dir: Path) -> list[Path]:
    return [Path(p) for p in sorted(glob.glob(f"{out_dir}/**/*.pdb", recursive=True))]


def collect_artifacts(out_dir: Path) -> list[Path]:
    arts: list[Path] = []
    for ext in ("*.fasta", "*.csv", "*.png"):
        arts += [Path(p) for p in sorted(glob.glob(f"{out_dir}/**/{ext}", recursive=True))]
    return arts


# ===========================================================================
# Main
# ===========================================================================


def main() -> None:
    start = time.time()
    payload = parse_payload()
    job_spec = payload.get("job_spec") or {}

    preset = str(job_spec.get("preset") or os.environ.get("JOB_TIER") or "complex_prediction")
    run_task = str(job_spec.get("run_task") or "design")
    antibody = job_spec.get("antibody_fasta") or []
    if not antibody:
        _fail("input", "antibody", "no antibody chains in job_spec")
    antigen_chain = str(job_spec.get("antigen_chain") or "A").strip() or "A"
    epitope_pdb = [int(x) for x in (job_spec.get("epitope_pdb_resnums") or [])]
    max_antigen_size = int(job_spec.get("max_antigen_size") or 2000)
    num_samples = int(job_spec.get("num_samples") or 1)
    # design.py gets raw num_samples on the CLI; total_passes is the true design
    # count IgGM produces (num_samples for every preset except affinity_maturation,
    # which expands one design per masked position per sample). Use it for the
    # progress heartbeats so the bar counts toward the real total, not num_samples.
    total_passes = int(job_spec.get("total_passes") or num_samples)
    fasta_origin_raw = job_spec.get("fasta_origin")

    webhook_url = os.environ.get("WEBHOOK_URL", "")
    job_id = os.environ.get("JOB_ID", "")
    job_token = payload.get("job_token", "") or os.environ.get("JOB_TOKEN", "")
    upload_endpoint = payload.get("upload_urls_endpoint", "")
    if not upload_endpoint:
        _fail("preflight", "upload_urls_endpoint", "upload_urls_endpoint missing from payload")

    send_heartbeat(webhook_url, job_id, stage="loading_model", designs_total=total_passes)

    with tempfile.TemporaryDirectory(prefix="iggm_", dir="/tmp") as _td:
        workdir = Path(_td)
        try:
            antigen_pdb = download_antigen_pdb(
                payload.get("input_presigned_url") or "", workdir / "antigen.pdb"
            )

            # ---- preflight: antigen extraction + epitope conversion + size gate ----
            info = antigen_chain_info(antigen_pdb, antigen_chain)
            antigen_seq, resnum_to_pos, n_res = info["seq"], info["resnum_to_pos"], info["n_res"]
            if n_res == 0:
                _fail("input", "antigen_chain",
                      f"chain {antigen_chain!r} produced 0 residues in the uploaded PDB")
            if n_res > ANTIGEN_LEN_HARD_CAP:
                _fail("input", "antigen_size",
                      f"antigen chain {antigen_chain} is {n_res} aa, over the "
                      f"{ANTIGEN_LEN_HARD_CAP} aa limit. Trim the antigen and resubmit.")

            epitope_positions, missing = convert_epitope(epitope_pdb, resnum_to_pos)
            if missing:
                _fail("input", "epitope",
                      f"epitope residue number(s) {missing} are not on antigen chain "
                      f"{antigen_chain}. Pick epitope residues on the antigen.")
            logger.info(
                "antigen %s: %d residues; epitope PDB %s -> positions %s",
                antigen_chain, n_res, epitope_pdb, epitope_positions,
            )

            # ---- build FASTA(s) ----
            design_fasta = workdir / "design.fasta"
            write_fasta(antibody, antigen_chain, antigen_seq, design_fasta)

            fasta_origin_path: Path | None = None
            if run_task == "affinity_maturation" and fasta_origin_raw:
                wt_records: list[dict[str, str]] = []
                hdr = None
                buf: list[str] = []
                for ln in str(fasta_origin_raw).splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    if ln.startswith(">"):
                        if hdr is not None and buf:
                            wt_records.append({"header": hdr, "sequence": "".join(buf).upper()})
                        hdr = ln[1:].strip()
                        buf = []
                    else:
                        buf.append(ln)
                if hdr is not None and buf:
                    wt_records.append({"header": hdr, "sequence": "".join(buf).upper()})
                fasta_origin_path = workdir / "origin.fasta"
                write_fasta(wt_records, antigen_chain, antigen_seq, fasta_origin_path)

            send_heartbeat(webhook_url, job_id, stage="designing", designs_total=total_passes)

            out_dir = workdir / "out"
            rc = run_iggm(
                design_fasta, antigen_pdb, out_dir, run_task, epitope_positions,
                max_antigen_size, num_samples, fasta_origin_path,
            )
            if rc != 0:
                _fail("run", "design.py", f"IgGM design.py exited with code {rc}")

            design_pdbs = collect_design_pdbs(out_dir)
            if not design_pdbs:
                _fail("run", "output", "IgGM produced no PDB outputs")

            # ---- upload designs + compute epitope-contact QC per design ----
            designs_out: list[dict] = []
            for i, pdb_path in enumerate(design_pdbs):
                pdb_text = pdb_path.read_text()
                contacts = epitope_contacts(pdb_text, n_res, epitope_positions)
                # Index-prefix the key so per-sample outputs that share a basename
                # (e.g. sample_0/pred.pdb, sample_1/pred.pdb) don't collide in storage.
                uniq = f"{i:03d}_{pdb_path.stem}"
                pdb_key = f"{uniq}.pdb"
                try:
                    urls = request_upload_urls(upload_endpoint, job_token, [pdb_key])
                    upload_file(urls[pdb_key], pdb_text.encode("utf-8"), "chemical/x-pdb")
                except Exception as exc:
                    logger.warning("design %s: upload failed (%s) — skipping", pdb_key, exc)
                    continue
                entry = {
                    "rank": i,
                    "name": uniq,
                    "pdb_key": pdb_key,
                    "n_epitope_contacts": contacts["n_contacted"],
                    "n_epitope": contacts["n_epitope"],
                    "contacted_positions": contacts["contacted"],
                    "antigen_chain": antigen_chain,
                }
                designs_out.append(entry)
                send_heartbeat(
                    webhook_url, job_id, stage="designing",
                    designs_completed=len(designs_out), designs_total=len(design_pdbs),
                    new_candidate=entry,
                )

            # ---- upload non-PDB artifacts (FASTA / CSV / plots) ----
            artifact_keys: list[str] = []
            for art in collect_artifacts(out_dir):
                key = f"artifacts/{art.name}"
                ctype = {
                    ".fasta": "text/plain", ".csv": "text/csv", ".png": "image/png",
                }.get(art.suffix.lower(), "application/octet-stream")
                try:
                    urls = request_upload_urls(upload_endpoint, job_token, [key])
                    upload_file(urls[key], art.read_bytes(), ctype)
                    artifact_keys.append(key)
                except Exception as exc:
                    logger.warning("artifact %s: upload failed (%s)", key, exc)

            # Deliver the COMPLETED result BEFORE the raw-capture tar runs. _ship_raw
            # is in the finally below; a run finishing near the subprocess timeout can
            # be SIGKILLed mid-tar, and if the result were written only after the tar
            # that kill would flip a real success into a lost job. Writing it here — the
            # last statement of the try body — guarantees result delivery precedes capture.
            runtime_seconds = int(time.time() - start)
            _write_result(
                {
                    "status": "COMPLETED",
                    "tier": preset,
                    "run_task": run_task,
                    "designs_total": len(design_pdbs),
                    "designs_completed": len(designs_out),
                    "designs": designs_out,
                    "artifact_keys": artifact_keys,
                    "antigen_chain": antigen_chain,
                    "antigen_length": n_res,
                    "epitope_positions": epitope_positions,
                    "epitope_pdb_resnums": epitope_pdb,
                    "runtime_seconds": runtime_seconds,
                    "provider_job_id": os.environ.get("JOB_ID", ""),
                }
            )
        finally:
            # Ship the COMPLETE tree home before TemporaryDirectory deletes it.
            # In a finally so it also covers the _fail() exits above (each is a
            # sys.exit -> SystemExit through this block) and any unexpected raise:
            # a crashed or zero-design run is precisely when the tree matters most.
            _ship_raw(workdir)

    send_heartbeat(
        webhook_url, job_id, stage="complete",
        designs_completed=len(designs_out), designs_total=len(design_pdbs),
    )
    logger.info(
        "pipeline ok — %d designs, runtime=%ds", len(designs_out), runtime_seconds
    )


if __name__ == "__main__":
    main()
