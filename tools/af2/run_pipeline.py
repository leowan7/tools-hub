"""Modal entrypoint for D2 — AF2 standalone.

Reads job configuration from the ``JOB_PAYLOAD`` env var (same shape the
D1 MPNN pipeline uses), runs ColabFold (which wraps AF2 + MMseqs2 MSA),
parses the output PDB + JSON sidecars into the atomic-tool output
schema, and writes the result to ``/tmp/smoke_results.json``. The Modal
wrapper returns this file inline — see ``tools/af2/modal_app.py``.

Contract (per docs/ATOMIC-TOOLS.md):

- ``preflight()`` is called first and must complete in <= 60 s. On any
  failure it writes ``{"status":"FAILED","error":{...}}`` to
  ``/tmp/smoke_results.json`` and ``sys.exit(1)`` so the build-time
  Layer-1 checks are not duplicated at runtime.
- ``run()`` writes the input FASTA, invokes ``colabfold_batch``, then
  parses the output. Stub rejection: fails if the pLDDT array is
  all-identical, all-nan, or all-zero (the ColabFold failure modes
  where the model ran on a degraded path).

Environment variables (set by ``tools/af2/modal_app.py`` from the
payload):

    JOB_PAYLOAD     JSON string with job_spec + input_presigned_url + tier
    WEBHOOK_URL     URL to POST results to (ignored on smoke tier)
    JOB_ID          tool_jobs row id (used for log prefixing)
    JOB_TOKEN       Job-specific auth token for the webhook
    JOB_TIER        ``smoke`` | ``standalone``

Output shape (``/tmp/smoke_results.json``)::

    {
      "status": "COMPLETED",
      "tier": "standalone",
      "pdb_b64": "...",
      "plddt_per_residue": [92.1, 93.0, ...],
      "pae_matrix_b64": "<base64-encoded .npy>",
      "pae_shape": [L, L],
      "iptm": 0.82,
      "ptm": 0.79,
      "num_chains": 2,
      "total_aa": 248,
      "runtime_seconds": 420,
      "provider_job_id": "<job_id>"
    }
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("af2_pipeline")


COLABFOLD_CACHE_DIR = os.environ.get(
    "COLABFOLD_CACHE_DIR", "/opt/colabfold_weights"
)
SMOKE_TARGET_FASTA = "/opt/smoke_target.fasta"
SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"

# Bounds enforced on the two numeric job_spec params. Mirrored from the
# tools-hub adapter validate() but re-checked here because the pipeline
# may be invoked directly (e.g. ``modal run`` for staging validation).
RECYCLES_MIN = 1
RECYCLES_MAX = 5
MAX_TOTAL_AA = 1500


# ===========================================================================
# Result file writer
# ===========================================================================


def _write_result(payload: dict[str, Any]) -> None:
    """Write the canonical smoke-result JSON. Overwrites any prior file."""
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        # Last-ditch: log to stderr so Modal logs capture the reason.
        logger.error("Could not write %s: %s", SMOKE_RESULTS_PATH, exc)


def _fail(bucket: str, check: str, detail: str) -> None:
    """Write a FAILED result and exit 1. Matches the Kendrew + MPNN shape."""
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

    Asserts the things Layer-1 already checked, plus GPU availability
    and tmp-writable, which only exist at runtime. Failures write
    FAILED to ``/tmp/smoke_results.json`` and sys.exit(1).
    """
    # 1. payload shape — fasta_records required EXCEPT on smoke tier,
    # which uses the baked /opt/smoke_target.fasta fixture (mirrors
    # D1 MPNN's smoke contract). resolve_input_fasta() handles the
    # smoke fallback at line ~247.
    tier = str(payload.get("tier") or "").lower()
    job_spec = payload.get("job_spec") or {}
    if tier != "smoke":
        if "fasta_records" not in job_spec:
            _fail("preflight", "payload", "missing fasta_records in job_spec")
        records = job_spec.get("fasta_records") or []
        if not isinstance(records, list) or not records:
            _fail("preflight", "payload", "fasta_records must be a non-empty list")

    # 2. ColabFold binary on $PATH
    try:
        out = subprocess.run(
            ["colabfold_batch", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        _fail("preflight", "binary", "colabfold_batch not on PATH")
    except subprocess.TimeoutExpired:
        _fail("preflight", "binary", "colabfold_batch --help timed out")
    else:
        if out.returncode != 0:
            _fail(
                "preflight",
                "binary",
                (
                    f"colabfold_batch --help exit {out.returncode}: "
                    f"{(out.stderr or '')[-400:]}"
                ),
            )

    # 3. AF2 weights present. ColabFold downloads both monomer + multimer
    # at build time; we only assert the directory is populated.
    if not os.path.isdir(COLABFOLD_CACHE_DIR):
        _fail(
            "preflight",
            "weights",
            f"COLABFOLD_CACHE_DIR not found at {COLABFOLD_CACHE_DIR}",
        )
    # Must contain at least one params file
    try:
        contents = os.listdir(COLABFOLD_CACHE_DIR)
    except OSError as exc:
        _fail("preflight", "weights", f"cannot list {COLABFOLD_CACHE_DIR}: {exc}")
        contents = []  # unreachable
    if not any("params" in c.lower() for c in contents):
        _fail(
            "preflight",
            "weights",
            (
                f"No AF2 params files found in {COLABFOLD_CACHE_DIR} "
                f"(contents: {contents[:8]})"
            ),
        )

    # 4. /tmp writable
    try:
        probe = Path("/tmp") / ".af2_preflight_probe"
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
# Payload parsing + FASTA resolution
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
    """Either write the caller FASTA or copy the baked smoke target.

    Smoke tier uses the baked BPTI target. Standalone tier writes the
    inline ``fasta_records`` list out as a single FASTA file. ColabFold
    accepts multi-chain input two ways; we pick the simplest: one
    ``>header`` per chain, sequences joined into a multimer by
    ColabFold's own multi-chain heuristic when ``--model-type
    alphafold2_multimer_v3`` is passed.
    """
    tier = str(payload.get("tier") or "").lower()
    job_spec = payload.get("job_spec") or {}
    dest = workdir / "input.fasta"

    if tier == "smoke":
        if not os.path.isfile(SMOKE_TARGET_FASTA):
            _fail(
                "input",
                "smoke_fixture",
                f"baked smoke fasta missing at {SMOKE_TARGET_FASTA}",
            )
        # Normalise to our own file so colabfold_batch sees a writable dir.
        with open(SMOKE_TARGET_FASTA) as src, open(dest, "w") as out:
            out.write(src.read())
        logger.info("smoke tier: using baked fasta %s", SMOKE_TARGET_FASTA)
        return dest

    records = job_spec.get("fasta_records") or []
    if not records:
        _fail("input", "fasta", "fasta_records empty on non-smoke tier")

    total_aa = 0
    with open(dest, "w") as out:
        if len(records) == 1:
            rec = records[0]
            seq = (rec.get("sequence") or "").strip().upper()
            if not seq:
                _fail("input", "fasta", "single-chain record has empty sequence")
            total_aa = len(seq)
            header = (rec.get("header") or "chain1").replace("\n", " ").strip()
            out.write(f">{header}\n{seq}\n")
        else:
            # Multimer: ColabFold accepts chains joined with ":" on one
            # sequence line under a single header. This is the canonical
            # ColabFold multimer input shape, well-tested upstream.
            joined_header = "_".join(
                (r.get("header") or f"chain{i + 1}").strip()
                for i, r in enumerate(records)
            )[:80]
            chain_seqs: list[str] = []
            for i, rec in enumerate(records):
                seq = (rec.get("sequence") or "").strip().upper()
                if not seq:
                    _fail(
                        "input",
                        "fasta",
                        f"chain {i + 1} has empty sequence",
                    )
                chain_seqs.append(seq)
                total_aa += len(seq)
            out.write(f">{joined_header}\n{':'.join(chain_seqs)}\n")

    if total_aa > MAX_TOTAL_AA:
        _fail(
            "input",
            "length_cap",
            (
                f"total AA {total_aa} exceeds atomic cap {MAX_TOTAL_AA} — "
                "reduce payload before retry."
            ),
        )

    logger.info(
        "standalone tier: wrote %d chain FASTA (%d AA) to %s",
        len(records),
        total_aa,
        dest,
    )
    return dest


# ===========================================================================
# ColabFold invocation
# ===========================================================================


def run_colabfold(
    fasta: Path,
    *,
    model_preset: str,
    num_recycles: int,
    use_templates: bool,
    use_msa: bool,
    workdir: Path,
) -> Path:
    """Invoke ``colabfold_batch`` and return the output directory.

    Command pattern follows the ColabFold README "batch" invocation.
    For monomer runs we use ``alphafold2_ptm`` (AF2 with pTM head) so
    pTM is emitted. For multimer runs we use ``alphafold2_multimer_v3``
    (the standard AF2-multimer weights + ipTM). Both names match
    colabfold 1.5.5's argparse choices verbatim — older docs used the
    capitalised ``AlphaFold2-ptm`` form which 1.5.5 rejects.
    """
    out_dir = workdir / "af2_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    if model_preset == "multimer":
        model_type = "alphafold2_multimer_v3"
    else:
        model_type = "alphafold2_ptm"

    cmd = [
        "colabfold_batch",
        # Point at baked weights at /opt/colabfold_weights so
        # colabfold_batch does not fall back to its default
        # /root/.cache/colabfold and re-download the 3.5GB params on
        # every cold pod (Bug 6 — surfaced by the Bug 1 visibility fix).
        "--data", str(COLABFOLD_CACHE_DIR),
        "--num-recycle", str(num_recycles),
        "--num-models", "1",  # atomic tier: single model seat
        "--model-type", model_type,
    ]
    if not use_msa:
        # Smoke / no-MSA path. colabfold_batch flag name has changed
        # over versions -- 1.5.5 ships ``--msa-mode``.
        cmd += ["--msa-mode", "single_sequence"]
    if not use_templates:
        cmd += ["--templates"] if False else []  # explicit default: off
    else:
        cmd += ["--templates"]
    cmd += [str(fasta), str(out_dir)]

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
            # behind buffered stdout. The Modal wrapper already
            # inherits this stdout/stderr (modal_app.py uses the same
            # pattern), so colabfold output flows through to the Modal
            # function logs.
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


def _find_best_pdb(out_dir: Path) -> Path:
    """Return the rank_1 / unrelaxed PDB colabfold_batch wrote.

    ColabFold emits files like::

        <name>_unrelaxed_rank_001_alphafold2_multimer_v3_model_1_seed_000.pdb
        <name>_scores_rank_001_alphafold2_multimer_v3_model_1_seed_000.json

    We pick the lowest-rank ``.pdb`` as the best prediction. If relaxed
    output exists (``_relaxed_``) we prefer it over ``_unrelaxed_``,
    but with ``--amber`` off by default we will usually see unrelaxed.
    """
    pdbs = sorted(out_dir.glob("*_rank_*.pdb"))
    if not pdbs:
        # Fallback: any PDB
        pdbs = sorted(out_dir.glob("*.pdb"))
    if not pdbs:
        _fail("parser", "pdb_missing", f"no PDB file in {out_dir}")
    relaxed = [p for p in pdbs if "_relaxed_" in p.name]
    if relaxed:
        return relaxed[0]
    return pdbs[0]


def _find_best_scores_json(out_dir: Path) -> Path:
    """Return the rank_1 scores JSON that pairs with the best PDB."""
    jsons = sorted(out_dir.glob("*scores_rank_*.json"))
    if not jsons:
        jsons = sorted(out_dir.glob("*scores*.json"))
    if not jsons:
        _fail("parser", "scores_missing", f"no scores JSON in {out_dir}")
    return jsons[0]


def parse_af2_output(
    out_dir: Path, *, fasta: Path
) -> dict[str, Any]:
    """Parse colabfold_batch output into the atomic-tool output schema.

    The JSON sidecar ColabFold writes carries ``plddt`` (list of per-
    residue floats 0-100), ``pae`` (LxL matrix), ``ptm`` (float,
    present on every model), and ``iptm`` (float, multimer only).
    """
    pdb_path = _find_best_pdb(out_dir)
    scores_path = _find_best_scores_json(out_dir)

    try:
        pdb_bytes = pdb_path.read_bytes()
    except OSError as exc:
        _fail("parser", "pdb_read", f"could not read {pdb_path}: {exc}")
        pdb_bytes = b""  # unreachable

    pdb_b64 = base64.b64encode(pdb_bytes).decode("ascii")

    try:
        scores = json.loads(scores_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _fail(
            "parser",
            "scores_parse",
            f"could not parse {scores_path}: {exc}",
        )
        scores = {}  # unreachable

    plddt = scores.get("plddt")
    pae = scores.get("pae")
    ptm = scores.get("ptm")
    iptm = scores.get("iptm")

    if not isinstance(plddt, list) or not plddt:
        _fail(
            "parser",
            "plddt_missing",
            f"plddt array missing or empty in {scores_path.name}",
        )
    if not isinstance(pae, list) or not pae or not isinstance(pae[0], list):
        _fail(
            "parser",
            "pae_missing",
            f"pae matrix missing or malformed in {scores_path.name}",
        )

    # PAE matrix serialised as a base64 .npy blob so the wire format
    # stays binary-stable. Falls back to the JSON list if numpy is
    # unavailable at runtime (should never happen on the AF2 image).
    try:
        import numpy as np  # noqa: PLC0415

        pae_np = np.asarray(pae, dtype=np.float32)
        buf = io.BytesIO()
        np.save(buf, pae_np, allow_pickle=False)
        pae_matrix_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        pae_shape: list[int] = list(pae_np.shape)
    except Exception as exc:  # pragma: no cover
        logger.warning("numpy PAE encode failed (%s); falling back to JSON", exc)
        pae_matrix_b64 = base64.b64encode(
            json.dumps(pae).encode("utf-8")
        ).decode("ascii")
        pae_shape = [len(pae), len(pae[0]) if pae else 0]

    total_aa = _fasta_total_aa(fasta)
    num_chains = _fasta_num_chains(fasta)

    plddt_floats = [float(x) for x in plddt]
    mean_plddt = (
        round(sum(plddt_floats) / len(plddt_floats), 2)
        if plddt_floats
        else 0.0
    )

    return {
        "pdb_b64": pdb_b64,
        "plddt_per_residue": plddt_floats,
        "mean_plddt": mean_plddt,
        "plddt_mean": mean_plddt,  # alias — D3 emits mean_plddt; harness accepts either.
        "pae_matrix_b64": pae_matrix_b64,
        "pae_shape": pae_shape,
        "ptm": float(ptm) if ptm is not None else None,
        "iptm": float(iptm) if iptm is not None else None,
        "num_chains": num_chains,
        "total_aa": total_aa,
        "pdb_filename": pdb_path.name,
    }


def _fasta_total_aa(fasta: Path) -> int:
    aa = 0
    for line in fasta.read_text().splitlines():
        line = line.strip()
        if line.startswith(">") or not line:
            continue
        # Multimer form: "SEQ1:SEQ2" — count residues, not colons.
        aa += sum(len(part) for part in line.split(":"))
    return aa


def _fasta_num_chains(fasta: Path) -> int:
    headers = sum(1 for line in fasta.read_text().splitlines() if line.startswith(">"))
    # Also handle the one-header-with-colons multimer shape.
    for line in fasta.read_text().splitlines():
        line = line.strip()
        if line.startswith(">") or not line:
            continue
        if ":" in line:
            return line.count(":") + 1
    return max(1, headers)


def reject_stub(result: dict[str, Any]) -> None:
    """Stub-rejection guard. Per ATOMIC-TOOLS.md D2 section.

    AF2 / ColabFold silent-stub failure modes seen in practice:

    1. Every pLDDT value is identical (model never ran / wrong weights
       loaded). Hard fail.
    2. Every pLDDT is NaN (numerical blow-up, cuDNN mismatch — the
       PXDesign cautionary tale). Hard fail.
    3. Every pLDDT is zero or sits at the AF2 "I have no idea" baseline
       (< 5). Hard fail.
    4. pTM / ipTM at exact AF2 untrained defaults (both equal to the
       same number to 4 decimals across independent samples) — this
       is the PXDesign ipTM=0.08/pLDDT=0.96 shape. We do not have
       multiple samples on the atomic tier but we assert that ipTM
       and pTM are not both zero.
    """
    plddt = result.get("plddt_per_residue") or []
    if not plddt:
        _fail("parser", "stub", "plddt array empty after parse")

    # All-identical check. Allow tiny floating jitter: a real AF2 run
    # always shows > 0.1 spread across any non-trivial length.
    try:
        minv = min(plddt)
        maxv = max(plddt)
    except TypeError:
        _fail("parser", "stub", "plddt contains non-numeric values")
        return
    if maxv - minv < 0.1:
        _fail(
            "parser",
            "stub",
            (
                "plddt spread < 0.1 across the whole sequence — this is "
                f"the AF2 silent-stub failure mode. min={minv} max={maxv} "
                f"len={len(plddt)}"
            ),
        )

    # NaN check.
    nan_count = sum(1 for v in plddt if v != v)  # NaN != NaN
    if nan_count:
        _fail(
            "parser",
            "stub",
            f"plddt has {nan_count}/{len(plddt)} NaN entries",
        )

    # All-zero / near-zero baseline.
    if maxv < 5.0:
        _fail(
            "parser",
            "stub",
            f"plddt max {maxv} < 5 — model returned baseline garbage",
        )

    # pTM / ipTM sanity: both zero or both None is a stub signature.
    ptm = result.get("ptm")
    iptm = result.get("iptm")
    if (ptm is None or ptm == 0.0) and (iptm is None or iptm == 0.0):
        _fail(
            "parser",
            "stub",
            "both pTM and ipTM are zero/None — AF2 head outputs degenerate",
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

    parameters = job_spec.get("parameters") or {}
    model_preset = str(parameters.get("model_preset") or "monomer").lower()
    try:
        num_recycles = int(parameters.get("num_recycles", 3))
    except (TypeError, ValueError):
        num_recycles = 3
    use_templates = bool(parameters.get("use_templates", True))

    # Defensive clamping (adapter already validates, but belts-and-braces
    # because the pipeline may be invoked directly via ``modal run``).
    num_recycles = max(RECYCLES_MIN, min(RECYCLES_MAX, num_recycles))

    # Smoke tier forces the fastest possible preset regardless of caller.
    if tier == "smoke":
        num_recycles = 1
        use_templates = False
        use_msa = False
    else:
        use_msa = True

    with tempfile.TemporaryDirectory(prefix="af2_", dir="/tmp") as _td:
        workdir = Path(_td)
        fasta = resolve_input_fasta(payload, workdir)
        out_dir = run_colabfold(
            fasta=fasta,
            model_preset=model_preset,
            num_recycles=num_recycles,
            use_templates=use_templates,
            use_msa=use_msa,
            workdir=workdir,
        )
        parsed = parse_af2_output(out_dir, fasta=fasta)
        reject_stub(parsed)

    runtime_seconds = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": tier,
            **parsed,
            "num_recycles": num_recycles,
            "use_templates": use_templates,
            "use_msa": use_msa,
            "model_preset": model_preset,
            "runtime_seconds": runtime_seconds,
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    logger.info(
        "pipeline ok — plddt_len=%d, runtime=%ds",
        len(parsed.get("plddt_per_residue") or []),
        runtime_seconds,
    )


if __name__ == "__main__":
    main()
