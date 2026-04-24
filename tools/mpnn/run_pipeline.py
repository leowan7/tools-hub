"""Modal entrypoint for D1 — ProteinMPNN standalone.

Reads job configuration from the ``JOB_PAYLOAD`` env var (same RunPod-parity
shape the Kendrew pipelines use), runs ProteinMPNN, writes the result to
``/tmp/smoke_results.json``. For smoke / standalone tiers the wrapper
returns this file inline via the Modal function return value — see
``tools/mpnn/modal_app.py``.

Contract (per docs/ATOMIC-TOOLS.md):

- ``preflight()`` is called first and must complete in <= 60 s. On any
  failure it writes ``{"status":"FAILED","error":{...}}`` to
  ``/tmp/smoke_results.json`` and ``sys.exit(1)`` so the build-time
  Layer-1 checks are not duplicated at runtime.
- ``run()`` executes ``protein_mpnn_run.py`` on the target PDB, then
  parses the FASTA output. Stub rejection: fails if every returned
  sequence is identical (MPNN's silent-stub failure mode).

Environment variables (set by ``tools/mpnn/modal_app.py`` from the
payload):

    JOB_PAYLOAD     JSON string with job_spec + input_presigned_url + tier
    WEBHOOK_URL     URL to POST results to (ignored on smoke tier)
    JOB_ID          tool_jobs row id (used for log prefixing)
    JOB_TOKEN       Job-specific auth token for the webhook
    JOB_TIER        ``smoke`` | ``standalone``

Output shape (``/tmp/smoke_results.json``)::

    {
      "status": "COMPLETED",
      "tier": "smoke",
      "sequences": [
        {"seq": "MDPLR...", "score": 1.23, "recovery": 0.52, "chain": "A"},
        ...
      ],
      "runtime_seconds": 47,
      "provider_job_id": "<job_id>"
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mpnn_pipeline")


PROTEINMPNN_DIR = os.environ.get("PROTEINMPNN_DIR", "/opt/ProteinMPNN")
PROTEINMPNN_WEIGHTS = os.environ.get(
    "PROTEINMPNN_WEIGHTS", f"{PROTEINMPNN_DIR}/vanilla_model_weights"
)
PROTEINMPNN_SCRIPT = f"{PROTEINMPNN_DIR}/protein_mpnn_run.py"
SMOKE_TARGET_PDB = "/opt/smoke_target.pdb"
SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"

# Bounds enforced on the two numeric job_spec params. Mirrored from the
# tools-hub adapter validate() but re-checked here because the pipeline
# may be invoked directly (e.g. ``modal run`` for staging validation).
NUM_SEQ_MIN = 1
NUM_SEQ_MAX = 20
TEMP_MIN = 0.01
TEMP_MAX = 1.0


# ===========================================================================
# Result file writer
# ===========================================================================


def _write_result(payload: dict[str, Any]) -> None:
    """Write the canonical smoke-result JSON. Overwrites any prior file."""
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        # Last-ditch: log to stderr so Modal logs capture the reason. The
        # wrapper's ``read_smoke_results`` will return None and the run
        # will be reported as FAILED via exit_code.
        logger.error("Could not write %s: %s", SMOKE_RESULTS_PATH, exc)


def _fail(bucket: str, check: str, detail: str) -> None:
    """Write a FAILED result and exit 1. Matches the Kendrew shape."""
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
# Preflight
# ===========================================================================


def preflight(payload: dict[str, Any]) -> None:
    """Cheap runtime sanity check. Runs in well under 60 s.

    Asserts the things Layer-1 already checked, plus GPU availability and
    tmp-writable, which only exist at runtime. Failures write FAILED to
    ``/tmp/smoke_results.json`` and sys.exit(1) so the Modal wrapper
    surfaces them inline.
    """
    # 1. payload shape
    for key in ("target_chain",):
        if key not in payload.get("job_spec", {}):
            _fail(
                "preflight",
                "payload",
                f"missing required key in job_spec: {key}",
            )

    # 2. MPNN binary on disk + executable via python
    if not os.path.isfile(PROTEINMPNN_SCRIPT):
        _fail(
            "preflight",
            "binary",
            f"protein_mpnn_run.py not found at {PROTEINMPNN_SCRIPT}",
        )

    # 3. Weights present
    weight_file = f"{PROTEINMPNN_WEIGHTS}/v_48_020.pt"
    if not os.path.isfile(weight_file):
        _fail("preflight", "weights", f"vanilla weights missing: {weight_file}")

    # 4. Torch + CUDA available (the A10G-24GB SKU must report a device)
    try:
        import torch  # noqa: PLC0415
    except Exception as exc:
        _fail("preflight", "torch", f"torch import failed: {exc}")
    if not torch.cuda.is_available():
        _fail(
            "preflight",
            "cuda",
            "torch.cuda.is_available() is False — no GPU visible",
        )

    # 5. MPNN module imports (uses torch — must succeed after step 4)
    # ProteinMPNN ships a protein_mpnn_utils.py next to protein_mpnn_run.py;
    # we import the former because the latter has side-effects at import.
    mpnn_utils = Path(PROTEINMPNN_DIR) / "protein_mpnn_utils.py"
    if not mpnn_utils.is_file():
        _fail(
            "preflight",
            "module",
            f"protein_mpnn_utils.py not found at {mpnn_utils}",
        )

    # 6. /tmp writable
    try:
        probe = Path("/tmp") / ".mpnn_preflight_probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        _fail("preflight", "tmp", f"/tmp is not writable: {exc}")

    logger.info("preflight ok — GPU=%s", torch.cuda.get_device_name(0))


# ===========================================================================
# Payload parsing / fetch
# ===========================================================================


def parse_payload() -> dict[str, Any]:
    """Read and parse the JOB_PAYLOAD env var."""
    raw = os.environ.get("JOB_PAYLOAD", "").strip()
    if not raw:
        _fail("preflight", "env", "JOB_PAYLOAD env var is empty")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail("preflight", "env", f"JOB_PAYLOAD is not valid JSON: {exc}")
    return {}  # unreachable; _fail exits


def resolve_input_pdb(payload: dict[str, Any], workdir: Path) -> Path:
    """Either download the caller PDB or copy the baked smoke target.

    Smoke tier uses the baked target to avoid a network hop on the smoke
    path; standalone tier downloads the user's upload from the presigned
    URL in the payload.
    """
    tier = str(payload.get("tier") or "").lower()
    if tier == "smoke":
        if not os.path.isfile(SMOKE_TARGET_PDB):
            _fail(
                "input",
                "smoke_fixture",
                f"baked smoke target missing at {SMOKE_TARGET_PDB}",
            )
        dest = workdir / "target.pdb"
        shutil.copy(SMOKE_TARGET_PDB, dest)
        logger.info("smoke tier: using baked target %s", SMOKE_TARGET_PDB)
        return dest

    url = str(payload.get("input_presigned_url") or "").strip()
    if not url:
        _fail("input", "url", "input_presigned_url missing on non-smoke tier")

    import requests  # noqa: PLC0415

    dest = workdir / "target.pdb"
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=32768):
                    if chunk:
                        fh.write(chunk)
    except Exception as exc:
        _fail("input", "download", f"PDB download failed: {exc}")
    if not dest.is_file() or dest.stat().st_size < 100:
        _fail("input", "download", "downloaded PDB is empty or tiny")
    return dest


# ===========================================================================
# MPNN invocation
# ===========================================================================


def run_mpnn(
    target_pdb: Path,
    chains_to_design: str,
    num_seq_per_target: int,
    sampling_temp: float,
    workdir: Path,
) -> Path:
    """Invoke ``protein_mpnn_run.py`` and return the output directory.

    Command matches the vanilla MPNN README usage. Output goes under
    ``workdir/mpnn_out/`` with ``seqs/<pdb_stem>.fa`` as the FASTA.
    """
    out_dir = workdir / "mpnn_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # MPNN's path input is a directory of PDBs. Stage our single PDB.
    pdb_stage = workdir / "pdb_in"
    pdb_stage.mkdir(parents=True, exist_ok=True)
    staged = pdb_stage / target_pdb.name
    if staged.resolve() != target_pdb.resolve():
        shutil.copy(target_pdb, staged)

    cmd = [
        "python3",
        PROTEINMPNN_SCRIPT,
        "--pdb_path", str(staged),
        "--pdb_path_chains", chains_to_design,
        "--out_folder", str(out_dir),
        "--num_seq_per_target", str(num_seq_per_target),
        "--sampling_temp", str(sampling_temp),
        "--seed", "37",
        "--batch_size", "1",
    ]
    logger.info("mpnn cmd: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=540,  # 9 min; ATOMIC spec caps runtime at 10 min
        )
    except subprocess.TimeoutExpired as exc:
        _fail("tool-invocation", "timeout", f"MPNN exceeded 9 min: {exc}")
        return out_dir  # unreachable

    if result.returncode != 0:
        tail = (result.stderr or "")[-1500:]
        _fail(
            "tool-invocation",
            "exit",
            f"MPNN exited {result.returncode}: ...{tail}",
        )

    logger.info("mpnn exit 0")
    return out_dir


# ===========================================================================
# Output parser + stub rejection
# ===========================================================================


def parse_mpnn_output(out_dir: Path, pdb_stem: str) -> list[dict[str, Any]]:
    """Parse MPNN's FASTA output into the atomic-tool sequence schema.

    ProteinMPNN writes ``<out_dir>/seqs/<pdb_stem>.fa`` where every other
    line is a FASTA header carrying score + sample metadata, followed by
    the sequence. The first record is the original (native) sequence.
    Return the non-native samples.
    """
    fa_path = out_dir / "seqs" / f"{pdb_stem}.fa"
    if not fa_path.is_file():
        _fail(
            "parser",
            "fasta_missing",
            f"expected MPNN FASTA at {fa_path}",
        )

    sequences: list[dict[str, Any]] = []
    header: str | None = None
    for line in fa_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            header = line[1:]
            continue
        if header is None:
            continue
        seq = line
        # Skip the first record (original native) which MPNN always emits.
        if header.startswith(f"{pdb_stem}"):
            # Native record: no "sample=" metadata.
            if "sample=" not in header:
                header = None
                continue

        score = _extract_metadata(header, "global_score")
        if score is None:
            score = _extract_metadata(header, "score")
        recovery = _extract_metadata(header, "seq_recovery")
        sample = _extract_metadata(header, "sample")

        sequences.append(
            {
                "seq": seq,
                "score": float(score) if score is not None else None,
                "recovery": float(recovery) if recovery is not None else None,
                "sample": int(sample) if sample is not None else None,
                "chain": "",  # MPNN's FASTA does not break chains out
            }
        )
        header = None

    if not sequences:
        _fail(
            "parser",
            "empty",
            f"parsed zero sample sequences from {fa_path.name}",
        )

    return sequences


def _extract_metadata(header: str, key: str) -> str | None:
    """Pull a key=value metadatum from an MPNN FASTA header."""
    match = re.search(rf"{re.escape(key)}\s*=\s*([^,\s]+)", header)
    if not match:
        return None
    return match.group(1)


def reject_stub(sequences: list[dict[str, Any]]) -> None:
    """Stub-rejection guard. Per ATOMIC-TOOLS.md D1 section.

    MPNN's silent-stub failure modes seen in practice:

    1. Every returned sequence is identical (model never ran / wrong
       weights loaded). Hard fail.
    2. Every sequence shares identical score + recovery floats to the
       bit (same random seed, no sampling). Hard fail.
    3. "Degenerate mode" — sequences differ by only 1-2 residues and
       score/recovery spreads are tiny (< 0.01). The model technically
       ran but collapsed; results are not usable. Hard fail so we don't
       bill a user for useless output.
    """
    seqs = [s.get("seq") or "" for s in sequences]
    if len(seqs) >= 2 and len(set(seqs)) == 1:
        _fail(
            "parser",
            "stub",
            (
                "all returned sequences are identical — "
                "this is the ProteinMPNN silent-stub failure mode. "
                f"n={len(seqs)}, length={len(seqs[0])}"
            ),
        )

    # Defence-in-depth: exact equality of score + recovery across the
    # set. Catches the naive replay-the-same-tensor stub.
    recoveries = [s.get("recovery") for s in sequences if s.get("recovery") is not None]
    scores = [s.get("score") for s in sequences if s.get("score") is not None]
    if (
        len(seqs) >= 3
        and len(set(recoveries)) == 1
        and len(set(scores)) == 1
        and recoveries
    ):
        _fail(
            "parser",
            "stub",
            (
                "all returned sequences share identical score + recovery "
                f"(score={scores[0]}, recovery={recoveries[0]}) — stub suspect."
            ),
        )

    # Near-clone detection: pairwise Hamming distance. If every pair of
    # sequences differs by <= 2 residues, the model has collapsed. Codex
    # P2 — the previous guards only tripped on bit-exact matches, which
    # missed this real ProteinMPNN degenerate mode.
    if len(seqs) >= 3 and all(len(s) == len(seqs[0]) and s for s in seqs):
        max_pairwise_hamming = 0
        for i, s1 in enumerate(seqs):
            for s2 in seqs[i + 1:]:
                d = sum(1 for a, b in zip(s1, s2) if a != b)
                if d > max_pairwise_hamming:
                    max_pairwise_hamming = d
        if max_pairwise_hamming <= 2:
            _fail(
                "parser",
                "stub",
                (
                    "returned sequences are near-clones (max pairwise "
                    f"Hamming={max_pairwise_hamming} over n={len(seqs)} "
                    f"samples of length {len(seqs[0])}) — ProteinMPNN "
                    "degenerate mode."
                ),
            )

    # Near-clone detection on score/recovery: tight cluster (spread <
    # 0.01) on both score AND recovery across >=3 samples. Covers the
    # failure mode where sampling injects residue diversity but the
    # probability landscape is collapsed.
    if len(seqs) >= 3 and len(scores) >= 3 and len(recoveries) >= 3:
        score_spread = max(scores) - min(scores)
        recovery_spread = max(recoveries) - min(recoveries)
        if score_spread < 0.01 and recovery_spread < 0.01:
            _fail(
                "parser",
                "stub",
                (
                    "score+recovery cluster is suspiciously tight "
                    f"(score spread={score_spread:.4f}, "
                    f"recovery spread={recovery_spread:.4f} over n={len(seqs)}) — "
                    "ProteinMPNN degenerate mode."
                ),
            )


# ===========================================================================
# Main
# ===========================================================================


def main() -> None:
    start = time.time()
    payload = parse_payload()
    preflight(payload)

    job_spec = payload.get("job_spec") or {}
    tier = str(payload.get("tier") or "").lower() or "standalone"

    chains_to_design = str(job_spec.get("target_chain") or "A").strip()
    try:
        num_seq_per_target = int(
            job_spec.get("parameters", {}).get("num_seq_per_target", 5)
        )
    except (TypeError, ValueError):
        num_seq_per_target = 5
    try:
        sampling_temp = float(
            job_spec.get("parameters", {}).get("sampling_temp", 0.1)
        )
    except (TypeError, ValueError):
        sampling_temp = 0.1

    # Defensive clamping (adapter already validates, but belts-and-braces
    # because the pipeline may be invoked directly via ``modal run``).
    num_seq_per_target = max(NUM_SEQ_MIN, min(NUM_SEQ_MAX, num_seq_per_target))
    sampling_temp = max(TEMP_MIN, min(TEMP_MAX, sampling_temp))

    # Smoke tier forces the fastest possible preset regardless of caller.
    if tier == "smoke":
        num_seq_per_target = 2
        sampling_temp = 0.1

    with tempfile.TemporaryDirectory(prefix="mpnn_", dir="/tmp") as _td:
        workdir = Path(_td)
        target_pdb = resolve_input_pdb(payload, workdir)
        out_dir = run_mpnn(
            target_pdb=target_pdb,
            chains_to_design=chains_to_design,
            num_seq_per_target=num_seq_per_target,
            sampling_temp=sampling_temp,
            workdir=workdir,
        )
        sequences = parse_mpnn_output(out_dir, pdb_stem=target_pdb.stem)
        reject_stub(sequences)

    runtime_seconds = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": tier,
            "sequences": sequences,
            "num_sequences": len(sequences),
            "sampling_temp": sampling_temp,
            "chains_designed": chains_to_design,
            "runtime_seconds": runtime_seconds,
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    logger.info(
        "pipeline ok — %d sequences, runtime=%ds",
        len(sequences),
        runtime_seconds,
    )


if __name__ == "__main__":
    main()
