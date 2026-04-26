"""Modal entrypoint for D3 — ColabFold standalone (no-MSA fast fold).

Reads job configuration from the ``JOB_PAYLOAD`` env var (same RunPod-parity
shape the Kendrew pipelines + D1 MPNN use), runs ``colabfold_batch`` in
single-sequence mode (no MSA, no templates by default), writes the result
to ``/tmp/smoke_results.json``. For smoke / standalone tiers the wrapper
returns this file inline via the Modal function return value — see
``tools/colabfold/modal_app.py``.

Contract (per docs/ATOMIC-TOOLS.md):

- ``preflight()`` is called first and must complete in <= 60 s. On any
  failure it writes ``{"status":"FAILED","error":{...}}`` to
  ``/tmp/smoke_results.json`` and ``sys.exit(1)`` so the build-time
  Layer-1 checks are not duplicated at runtime.
- ``run()`` executes ``colabfold_batch --msa-mode single_sequence``, then
  parses the scores JSON and output PDB. Stub rejection: fails if the
  pLDDT array is all-identical or contains NaN (the AF2 silent-stub
  failure mode — see PXDesign's pLDDT=0.96 incident in VALIDATION-LOG).

Environment variables (set by ``tools/colabfold/modal_app.py`` from the
payload):

    JOB_PAYLOAD     JSON string with job_spec + input_presigned_url + tier
    WEBHOOK_URL     URL to POST results to (ignored on smoke tier)
    JOB_ID          tool_jobs row id (used for log prefixing)
    JOB_TOKEN       Job-specific auth token for the webhook
    JOB_TIER        ``smoke`` | ``standalone``

Output shape (``/tmp/smoke_results.json``) — parallels D2 AF2::

    {
      "status": "COMPLETED",
      "tier": "smoke",
      "pdb_b64": "...",              # base64 of the top-ranked PDB
      "plddt_per_residue": [...],    # floats, one per residue
      "pae_matrix_b64": "...",       # base64 of npz-packed PAE matrix
      "iptm": 0.82,                  # multimer only; None for monomer
      "ptm": 0.79,
      "mean_plddt": 87.4,
      "sequence": "MQIFV...",
      "chain_count": 1,
      "runtime_seconds": 47,
      "provider_job_id": "<job_id>"
    }
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from glob import glob
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("colabfold_pipeline")


SMOKE_TARGET_FASTA = "/opt/smoke_target.fasta"
SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
COLABFOLD_CACHE_DIR = os.environ.get(
    "COLABFOLD_CACHE_DIR", "/opt/colabfold_weights"
)

# Bounds enforced on the two numeric job_spec params. Mirrored from the
# tools-hub adapter validate() but re-checked here because the pipeline
# may be invoked directly (e.g. ``modal run`` for staging validation).
RECYCLES_MIN = 1
RECYCLES_MAX = 5
# Length caps: ColabFold single-sequence runs comfortably up to ~500 aa
# on A100-40GB. Above that, JAX memory + wall-clock blow the 10-min
# timeout. Reject server-side to save the user a wasted run.
SEQ_LEN_MAX = 600
SEQ_LEN_MIN = 10
# Canonical residue alphabet (20 aa + X for unknown). ColabFold parses
# but silently drops non-canonicals; flagging here gives the user a
# better error than "ValueError: shape mismatch" mid-run.
CANONICAL_AA = set("ACDEFGHIKLMNPQRSTVWYX")


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


def _preflight_jax_gpu(timeout: int = 60) -> None:
    """Validate JAX can init on GPU + run a tiny JIT in <30 s.

    Runs as a fresh subprocess so this process stays JAX-free (the
    parent must not import JAX — see the VRAM-hostage note in
    ``preflight()``). If JAX / cuDNN cannot init on this image, fails
    in seconds with a useful stderr instead of letting colabfold_batch
    silently hang for 18-29 min on cold A100 (Bug 8).
    """
    script = (
        "import time, sys; t0 = time.time(); "
        "import jax, jax.numpy as jnp; "
        "devs = jax.devices('gpu'); "
        "assert devs, 'no GPU devices found'; "
        "x = jnp.ones((128, 128)); "
        "y = jax.jit(lambda a: a @ a)(x).block_until_ready(); "
        "print(f'preflight ok jax={jax.__version__} dev={devs[0].device_kind} "
        "sum={float(y.sum()):.1f} elapsed={time.time()-t0:.1f}s')"
    )
    env = dict(os.environ)
    # Mirror the allocator flags applied to the colabfold_batch
    # subprocess — keeps preflight from preallocating most of the VRAM
    # and starving the fold that follows.
    env.setdefault("TF_FORCE_GPU_ALLOW_GROWTH", "true")
    env.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
    env.setdefault("XLA_PYTHON_CLIENT_ALLOCATOR", "platform")
    env.setdefault("XLA_PYTHON_CLIENT_MEM_FRACTION", "4.0")
    env.setdefault("TF_FORCE_UNIFIED_MEMORY", "1")
    if os.path.isdir("/opt/jax_cache"):
        env.setdefault("JAX_COMPILATION_CACHE_DIR", "/opt/jax_cache")
    else:
        env.setdefault("JAX_COMPILATION_CACHE_DIR", "/tmp/jax_cache")
    env.setdefault("JAX_PERSISTENT_CACHE_MIN_COMPILE_TIME_SECS", "0")
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        _fail(
            "preflight", "jax-gpu",
            f"JAX GPU preflight timed out after {timeout}s — "
            "JAX/cuDNN cannot init on this pod.",
        )
        return  # unreachable
    if result.returncode != 0:
        logger.error("JAX preflight FAILED — stderr:\n%s", result.stderr[-2000:])
        _fail(
            "preflight", "jax-gpu",
            f"JAX GPU preflight failed (exit {result.returncode}): "
            f"{result.stderr.strip()[-500:]}",
        )
    logger.info("JAX preflight: %s", result.stdout.strip())


def preflight(payload: dict[str, Any]) -> None:
    """Cheap runtime sanity check. Runs in well under 60 s.

    Asserts the things Layer-1 already checked, plus GPU availability and
    tmp-writable, which only exist at runtime. Failures write FAILED to
    ``/tmp/smoke_results.json`` and sys.exit(1) so the Modal wrapper
    surfaces them inline.
    """
    # 1. payload shape — job_spec exists (no required inner keys for
    #    ColabFold since the FASTA is delivered separately).
    if "job_spec" not in payload:
        _fail("preflight", "payload", "missing required key: job_spec")

    # 2. colabfold_batch on PATH with --help exit 0
    try:
        result = subprocess.run(
            ["colabfold_batch", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _fail("preflight", "binary", f"colabfold_batch --help failed: {exc}")
    if result.returncode != 0:
        _fail(
            "preflight",
            "binary",
            f"colabfold_batch --help exited {result.returncode}: "
            f"...{(result.stderr or '')[-500:]}",
        )

    # 3. AF2 weights present
    params_dir = Path(COLABFOLD_CACHE_DIR) / "params"
    if not params_dir.is_dir():
        _fail(
            "preflight",
            "weights",
            f"colabfold params dir missing: {params_dir}",
        )
    params_files = list(params_dir.iterdir())
    if not params_files:
        _fail(
            "preflight",
            "weights",
            f"colabfold params dir is empty: {params_dir}",
        )

    # 4. /tmp writable
    try:
        probe = Path("/tmp") / ".colabfold_preflight_probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        _fail("preflight", "tmp", f"/tmp is not writable: {exc}")

    # NOTE: We deliberately do NOT import jax in this process. Importing
    # jax here would initialise XLA in the parent and preallocate ~90%
    # of GPU VRAM, starving the colabfold_batch subprocess. Instead we
    # validate JAX can init on the GPU via a short subprocess
    # (_preflight_jax_gpu) so any cuDNN / driver mismatch fails fast
    # with a clear error rather than 18-29 min of silent JIT hang.
    _preflight_jax_gpu(timeout=60)

    logger.info("preflight ok")


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


def resolve_input_fasta(payload: dict[str, Any], workdir: Path) -> Path:
    """Write the caller FASTA (or copy the baked smoke fixture) to workdir.

    Priority:
      1. Smoke tier → baked fixture (no network hop).
      2. ``job_spec.fasta_text`` (inline text from the tools-hub form).
      3. ``input_presigned_url`` (user uploaded a .fasta file — same
         staging flow as MPNN's PDB upload).
    """
    tier = str(payload.get("tier") or "").lower()
    dest = workdir / "input.fasta"

    if tier == "smoke":
        if not os.path.isfile(SMOKE_TARGET_FASTA):
            _fail(
                "input",
                "smoke_fixture",
                f"baked smoke fixture missing at {SMOKE_TARGET_FASTA}",
            )
        shutil.copy(SMOKE_TARGET_FASTA, dest)
        logger.info("smoke tier: using baked fixture %s", SMOKE_TARGET_FASTA)
        return dest

    job_spec = payload.get("job_spec") or {}
    inline_fasta = str(job_spec.get("fasta_text") or "").strip()
    if inline_fasta:
        dest.write_text(inline_fasta)
        logger.info("standalone tier: inline fasta_text (%d bytes)", len(inline_fasta))
        return dest

    url = str(payload.get("input_presigned_url") or "").strip()
    if not url:
        _fail(
            "input",
            "fasta",
            "neither job_spec.fasta_text nor input_presigned_url supplied",
        )

    import requests  # noqa: PLC0415

    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=32768):
                    if chunk:
                        fh.write(chunk)
    except Exception as exc:
        _fail("input", "download", f"FASTA download failed: {exc}")
    if not dest.is_file() or dest.stat().st_size < 5:
        _fail("input", "download", "downloaded FASTA is empty or tiny")
    return dest


def validate_fasta(fasta_path: Path) -> dict[str, Any]:
    """Parse + sanity-check the FASTA. Returns a small summary dict.

    ColabFold encodes a complex as a SINGLE FASTA record whose sequence
    joins chains with ``:``. The adapter's ``validate()`` already
    normalises multimer inputs to that shape (Codex P1). Here we split
    each record on ``:`` so per-chain length + alphabet checks apply
    to individual chains, not the joined string (the raw joined seq
    would always trip the canonical-residue check because ``:`` is not
    an amino acid).

    Rejects:
      - empty file
      - zero records
      - more than one ``>`` record (would run as separate monomer jobs
        and silently drop all but the first — Codex P1 regression guard)
      - any chain outside the [SEQ_LEN_MIN, SEQ_LEN_MAX] bounds
      - any residue outside the canonical 20 aa + X alphabet
    """
    from Bio import SeqIO  # noqa: PLC0415

    try:
        records = list(SeqIO.parse(str(fasta_path), "fasta"))
    except Exception as exc:
        _fail("input", "fasta_parse", f"FASTA parse failed: {exc}")

    if not records:
        _fail("input", "fasta_empty", "FASTA contains zero records")

    if len(records) > 1:
        _fail(
            "input",
            "multi_record",
            (
                f"FASTA has {len(records)} records — ColabFold expects "
                "ONE record per fold with ':' between chains. Join your "
                "chains into a single record."
            ),
        )

    rec = records[0]
    joined = str(rec.seq).upper().strip()
    # ColabFold uses ':' to separate chains within a single record.
    chains = [chunk for chunk in joined.split(":") if chunk]
    if not chains:
        _fail("input", "fasta_empty", "record has no sequence")

    seqs: list[str] = []
    for idx, chain in enumerate(chains, start=1):
        if len(chain) < SEQ_LEN_MIN:
            _fail(
                "input",
                "seq_length",
                f"chain {idx} is {len(chain)} aa — min {SEQ_LEN_MIN}",
            )
        if len(chain) > SEQ_LEN_MAX:
            _fail(
                "input",
                "seq_length",
                f"chain {idx} is {len(chain)} aa — max {SEQ_LEN_MAX}",
            )
        non_canonical = set(chain) - CANONICAL_AA
        if non_canonical:
            _fail(
                "input",
                "seq_alphabet",
                f"chain {idx} contains non-canonical residues: "
                f"{sorted(non_canonical)}",
            )
        seqs.append(chain)

    total_len = sum(len(s) for s in seqs)
    if total_len > SEQ_LEN_MAX:
        _fail(
            "input",
            "total_length",
            f"total complex length {total_len} exceeds max {SEQ_LEN_MAX} aa "
            "for no-MSA fold on A100-40GB within the 10-min budget",
        )

    return {
        "chain_count": len(seqs),
        "total_length": total_len,
        "sequence": ":".join(seqs),
    }


# ===========================================================================
# ColabFold invocation
# ===========================================================================


def run_colabfold(
    fasta_path: Path,
    num_recycles: int,
    use_templates: bool,
    workdir: Path,
) -> Path:
    """Invoke ``colabfold_batch`` in no-MSA mode and return the output dir.

    Defaults mirror the RFdiffusion pipeline's ``stage_af2_validation``
    smoke path: single-sequence MSA, num-models=1, rank on ipTM (falls
    back to pLDDT for monomers).
    """
    out_dir = workdir / "colabfold_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "colabfold_batch",
        str(fasta_path),
        str(out_dir),
        # Point at the baked weights at /opt/colabfold_weights so
        # colabfold_batch does not fall back to its default
        # /root/.cache/colabfold and re-download the 3.5GB params on
        # every cold pod (Bug 6 — surfaced by the Bug 1 visibility fix).
        "--data", str(COLABFOLD_CACHE_DIR),
        "--msa-mode", "single_sequence",
        "--num-recycle", str(num_recycles),
        "--num-models", "1",
        "--rank", "iptm",
    ]
    if not use_templates:
        # Default: no templates. ColabFold 1.5 requires the explicit
        # flag to switch on templates (``--templates``); leaving it off
        # is the no-template path we want for the speed tier.
        pass
    else:
        cmd.append("--templates")

    logger.info("colabfold cmd: %s", " ".join(cmd))
    # Subprocess env. Inherits the container env (TF / XLA flags set in
    # Dockerfile) and adds the LocalColabFold-prescribed VRAM / allocator
    # flags as a runtime safety net in case the Dockerfile is older than
    # the runtime helper.
    env = dict(os.environ)
    if os.path.isdir("/opt/jax_cache"):
        env.setdefault("JAX_COMPILATION_CACHE_DIR", "/opt/jax_cache")
    else:
        env.setdefault("JAX_COMPILATION_CACHE_DIR", "/tmp/jax_cache")
    env.setdefault("JAX_PERSISTENT_CACHE_MIN_COMPILE_TIME_SECS", "0")
    env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    # LocalColabFold's prescribed env-var set for TF/JAX co-tenancy on a
    # single GPU. TF (pulled in for tf.data feature pipeline) defaults to
    # claiming nearly all VRAM at import time — JAX then can't allocate
    # and silently hangs during XLA JIT. These flags force both
    # frameworks into growth-allocation mode.
    env.setdefault("TF_FORCE_GPU_ALLOW_GROWTH", "true")
    env.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
    env.setdefault("XLA_PYTHON_CLIENT_ALLOCATOR", "platform")
    env.setdefault("XLA_PYTHON_CLIENT_MEM_FRACTION", "4.0")
    env.setdefault("TF_FORCE_UNIFIED_MEMORY", "1")
    # Silence the duplicate "oneDNN custom operations are on" log line
    # that appears twice in the same PID during AF2 multimer SavedModel
    # restore. With ONEDNN off, a single appearance means TF imported
    # once; a double appearance means it imported twice (Bug 8 H6 probe).
    env.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")

    try:
        result = subprocess.run(
            cmd,
            check=False,
            # Live-stream colabfold_batch output to Modal logs. The
            # earlier capture_output=True hid 18-29 min of "silent" hang
            # behind buffered stdout — without live streaming we could
            # not see WHERE colabfold_batch stalled. The Modal wrapper
            # already inherits this stdout/stderr (modal_app.py uses
            # the same pattern), so colabfold output flows through to
            # the Modal function logs.
            stdout=sys.stdout,
            stderr=sys.stderr,
            # 29 min of the 30 min app budget; leaves room for the
            # wrapper to read smoke_results.json.
            timeout=1740,
            env=env,
        )
    except subprocess.TimeoutExpired:
        # With live streaming, exc.stdout / exc.stderr are None — the
        # output already went to Modal's function logs. Reference those.
        logger.error("colabfold_batch TIMEOUT after 29 min — see Modal function logs for live output above.")
        _fail(
            "tool-invocation",
            "timeout",
            "colabfold_batch exceeded 29 min — see Modal function logs for live output.",
        )
        return out_dir  # unreachable

    if result.returncode != 0:
        # Live streaming already wrote to Modal logs above; just point
        # at them in the failure record.
        logger.error("colabfold_batch exit %d — see Modal function logs above.", result.returncode)
        _fail(
            "tool-invocation",
            "exit",
            f"colabfold_batch exited {result.returncode} — see Modal function logs.",
        )

    logger.info("colabfold exit 0")
    return out_dir


# ===========================================================================
# Output parser + stub rejection
# ===========================================================================


def parse_colabfold_output(out_dir: Path) -> dict[str, Any]:
    """Parse ColabFold's scores JSON + top-ranked PDB into the D3 output schema.

    ColabFold emits files like
    ``<stem>_scores_rank_001_alphafold2_multimer_v3_model_1_seed_000.json``
    and the matching ``_unrelaxed_rank_001_*.pdb``. We pick the
    rank-001 record for the scores + PDB.
    """
    # Prefer rank_001 if present; otherwise pick the lex-smallest
    # _scores_*.json so we always return a deterministic choice.
    score_files = sorted(glob(str(out_dir / "*rank_001*scores*.json")))
    if not score_files:
        score_files = sorted(glob(str(out_dir / "*scores*.json")))
    if not score_files:
        _fail(
            "parser",
            "scores_missing",
            f"no *scores*.json in colabfold output dir {out_dir}",
        )

    score_path = Path(score_files[0])
    try:
        with open(score_path) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        _fail("parser", "scores_parse", f"failed to read {score_path.name}: {exc}")

    plddt = data.get("plddt", []) or []
    ptm = data.get("ptm", None)
    iptm = data.get("iptm", None)
    pae_matrix = data.get("pae", []) or []

    if not isinstance(plddt, list) or len(plddt) == 0:
        _fail(
            "parser",
            "plddt_missing",
            f"scores JSON has empty or missing 'plddt': {score_path.name}",
        )

    try:
        plddt_floats = [float(x) for x in plddt]
    except (TypeError, ValueError) as exc:
        _fail("parser", "plddt_parse", f"plddt values not numeric: {exc}")

    mean_plddt = sum(plddt_floats) / len(plddt_floats)

    # Find the matching PDB. ColabFold encodes rank + model + seed in the
    # filename; strip "_scores_" → "_unrelaxed_".
    stem = score_path.name
    pdb_guess = out_dir / stem.replace("_scores_", "_unrelaxed_").replace(
        ".json", ".pdb"
    )
    if pdb_guess.is_file():
        pdb_path = pdb_guess
    else:
        pdb_candidates = sorted(glob(str(out_dir / "*rank_001*unrelaxed*.pdb")))
        if not pdb_candidates:
            pdb_candidates = sorted(glob(str(out_dir / "*unrelaxed*.pdb")))
        if not pdb_candidates:
            _fail(
                "parser",
                "pdb_missing",
                f"no unrelaxed rank-001 PDB in {out_dir}",
            )
        pdb_path = Path(pdb_candidates[0])

    try:
        pdb_bytes = pdb_path.read_bytes()
    except OSError as exc:
        _fail("parser", "pdb_read", f"could not read {pdb_path.name}: {exc}")

    if len(pdb_bytes) < 200:
        _fail(
            "parser",
            "pdb_tiny",
            f"{pdb_path.name} is only {len(pdb_bytes)} bytes — not a real PDB",
        )

    pdb_b64 = base64.b64encode(pdb_bytes).decode("ascii")

    # PAE packed as npz (float16) to keep the payload small. Fall back
    # to an empty b64 string if the matrix is missing (monomer-only
    # AF2 sometimes omits; we still succeed on plddt + ptm).
    pae_b64 = ""
    if pae_matrix:
        try:
            import numpy as np  # noqa: PLC0415

            arr = np.array(pae_matrix, dtype=np.float16)
            buf = io.BytesIO()
            np.savez_compressed(buf, pae=arr)
            pae_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:
            logger.warning("PAE packing failed (non-fatal): %s", exc)

    return {
        "pdb_b64": pdb_b64,
        "plddt_per_residue": plddt_floats,
        "pae_matrix_b64": pae_b64,
        "iptm": float(iptm) if iptm is not None else None,
        "ptm": float(ptm) if ptm is not None else None,
        "mean_plddt": round(mean_plddt, 2),
        "pae_matrix_raw": pae_matrix,  # used by stub rejection; popped before return
    }


def reject_stub(parsed: dict[str, Any]) -> None:
    """Stub-rejection guard. Per ATOMIC-TOOLS.md D2/D3 section.

    AF2 / ColabFold silent-stub failure modes seen in practice:

    1. pLDDT array is all-NaN — weights never loaded / wrong dtype.
    2. pLDDT array is all-identical — the model emitted a constant
       tensor. PXDesign's ipTM=0.08 / pLDDT=0.96 is the canonical
       example; see VALIDATION-LOG.md.
    3. mean pLDDT is implausible (<=0 or >100) — units got scrambled.
    4. iptm/ptm are exactly 0.0 across the whole run — the multimer
       head got bypassed.
    """
    plddt = parsed.get("plddt_per_residue") or []

    if not plddt:
        _fail("parser", "stub", "pLDDT array is empty after parse")

    # NaN / infinite check
    if any(math.isnan(x) or math.isinf(x) for x in plddt):
        _fail(
            "parser",
            "stub",
            "pLDDT array contains NaN or infinite — AF2 silent-stub signature",
        )

    # All-identical check (spread < 1e-6 means every residue got the
    # same value, which never happens in a real fold).
    spread = max(plddt) - min(plddt)
    if len(plddt) >= 5 and spread < 1e-6:
        _fail(
            "parser",
            "stub",
            (
                f"pLDDT is uniform across {len(plddt)} residues "
                f"(value={plddt[0]:.4f}) — AF2 silent-stub signature, "
                "see PXDesign pLDDT=0.96 incident"
            ),
        )

    mean_plddt = parsed.get("mean_plddt", 0.0)
    if mean_plddt <= 0 or mean_plddt > 100:
        _fail(
            "parser",
            "stub",
            f"mean pLDDT={mean_plddt} outside plausible [0, 100] range",
        )

    # If iptm is reported (multimer) and is exactly 0.0, that's the
    # multimer-head bypass stub. ptm==0.0 is the monomer equivalent —
    # Codex P2 found the original guard only checked iptm, so monomer
    # stubs with ptm==0.0 silently succeeded. Both paths now fail.
    iptm = parsed.get("iptm")
    if iptm is not None and iptm == 0.0:
        _fail(
            "parser",
            "stub",
            "iptm is exactly 0.0 — multimer-head bypass stub signature",
        )
    ptm = parsed.get("ptm")
    if ptm is not None and ptm == 0.0:
        _fail(
            "parser",
            "stub",
            "ptm is exactly 0.0 — AF2 head bypass stub signature",
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

    try:
        num_recycles = int(job_spec.get("parameters", {}).get("num_recycles", 1))
    except (TypeError, ValueError):
        num_recycles = 1
    use_templates = bool(
        job_spec.get("parameters", {}).get("use_templates", False)
    )

    # Defensive clamping (adapter already validates, but belts-and-braces
    # because the pipeline may be invoked directly via ``modal run``).
    num_recycles = max(RECYCLES_MIN, min(RECYCLES_MAX, num_recycles))

    # Smoke tier forces the fastest possible preset regardless of caller.
    if tier == "smoke":
        num_recycles = 1
        use_templates = False

    with tempfile.TemporaryDirectory(prefix="colabfold_", dir="/tmp") as _td:
        workdir = Path(_td)
        fasta_path = resolve_input_fasta(payload, workdir)
        fasta_summary = validate_fasta(fasta_path)
        out_dir = run_colabfold(
            fasta_path=fasta_path,
            num_recycles=num_recycles,
            use_templates=use_templates,
            workdir=workdir,
        )
        parsed = parse_colabfold_output(out_dir)
        reject_stub(parsed)

    # Drop the raw PAE matrix before writing — only the b64-packed copy
    # goes on the wire.
    parsed.pop("pae_matrix_raw", None)

    runtime_seconds = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": tier,
            **parsed,
            "sequence": fasta_summary["sequence"],
            "chain_count": fasta_summary["chain_count"],
            "total_length": fasta_summary["total_length"],
            "num_recycles": num_recycles,
            "use_templates": use_templates,
            "runtime_seconds": runtime_seconds,
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    logger.info(
        "pipeline ok — mean pLDDT=%.2f, length=%d, runtime=%ds",
        parsed.get("mean_plddt", 0.0),
        fasta_summary["total_length"],
        runtime_seconds,
    )


if __name__ == "__main__":
    main()
