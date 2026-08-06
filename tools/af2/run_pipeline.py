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
      "pae_matrix_b64": "<base64-encoded .npz, key 'pae'>",
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
from urllib.parse import urlparse, urlunparse

import requests

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
    """Fire-and-forget heartbeat. Never raises — a flaky webhook hop must
    not abort an in-progress batch fold. Schema mirrors the Boltz-2 +
    ESMFold batch contract.
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
            f"upload_urls request failed: HTTP {resp.status_code} "
            f"{resp.text[:200]}"
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
# Raw output capture
# ===========================================================================


RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"


def archive_raw(work_dir: Path | str, tag: str) -> None:
    """Tar the COMPLETE work dir to ``RAW_ARCHIVE_PATH`` before teardown.

    ``tools/af2/modal_app.py`` moves the archive onto the
    ``ranomics-af2-raw`` Volume once this process exits. Everything the
    parsers above do not keep -- the a3m MSAs, every model but rank_001,
    the PAE / pLDDT of the ranks we discard, config.json, the colabfold
    log -- otherwise dies with the container and is recoverable only by
    paying for the GPU again. A container must never decide which fields
    are worth keeping: that is how boltzgen's ``iptm`` (interface-pTM
    averaged over EVERY chain pair) got shipped where ``design_iptm``
    (binder -> target) was meant, and 460 designs across two campaigns
    were scored on a number that read ~2x high. Ship the tree; decide
    locally, where re-parsing is free.

    Unconditional by design -- not gated on exit code, on candidates, or
    on what got uploaded. A batch whose records all fail pre-validation
    uploads nothing and returns a zero-design "success"; that is exactly
    the run whose tree you want. Callers invoke it from a ``finally`` so
    it also fires on the ``_fail()`` / ``sys.exit(1)`` paths, which is
    when the diagnostics matter most.

    Best-effort: capture must never fail the run. Problems are logged,
    never raised.
    """
    # Contract hardening, not a report of an observed failure. The handler at
    # the bottom deletes ``dest``, but the try does not assign it until several
    # statements in; reaching the handler before that point would raise
    # UnboundLocalError, and UnboundLocalError is a NameError, which the inner
    # ``except OSError`` does not catch -- so it would escape a function
    # documented never to raise, out of a ``finally``, discarding whatever exit
    # was already in flight. Binding it here is what makes the handler's
    # ``dest is not None`` guard mean anything. Both call sites pass a live
    # Path and cannot open that window; the point is that the contract should
    # not depend on that staying true.
    dest: str | None = None
    try:
        import tarfile  # noqa: PLC0415

        src = os.path.abspath(str(work_dir))
        if not os.path.isdir(src):
            logger.warning("[raw] no work dir at %s -- nothing to archive", src)
            return
        dest = os.path.abspath(RAW_ARCHIVE_PATH)
        # The tar must not be written inside the tree it archives, or it
        # tars itself. /tmp/raw_archive.tgz sits outside a /tmp/af2_*/
        # work dir, but assert that rather than trust it.
        if dest == src or dest.startswith(src + os.sep):
            logger.warning(
                "[raw] refusing to write %s inside its own source tree %s",
                dest, src,
            )
            return
        # Stream to a file, never io.BytesIO -- the latter costs ~3-4x
        # peak RSS on a multi-hundred-MB batch output tree.
        with tarfile.open(dest, "w:gz") as tf:
            tf.add(src, arcname=tag)
        logger.info(
            "[raw] archived %s -> %s (%.1f MB)",
            src, dest, os.path.getsize(dest) / 1e6,
        )
    except Exception as exc:  # noqa: BLE001 -- capture is best-effort
        logger.warning(
            "[raw] capture failed (non-fatal): %s: %s",
            type(exc).__name__, exc,
        )
        # A crash mid-write (e.g. ENOSPC) can leave a truncated but still-openable .tgz at
        # the destination; the wrapper parks whatever exists. Remove the partial so a failed
        # capture parks NOTHING rather than a tar that reports success but cannot be read.
        try:
            if dest is not None and os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass


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
    # 1. payload shape. Smoke tier uses the baked /opt/smoke_target.fasta
    # fixture. Standalone tier needs ``fasta_records`` (legacy single-fold
    # key). Batch preset sends ``batch_records`` instead and is dispatched
    # by main() before reaching the single-fold path — accept either.
    tier = str(payload.get("tier") or "").lower()
    job_spec = payload.get("job_spec") or {}
    if tier != "smoke":
        batch_records = job_spec.get("batch_records")
        if isinstance(batch_records, list) and batch_records:
            # Batch path — main() will dispatch to _run_batch().
            pass
        else:
            if "fasta_records" not in job_spec:
                _fail(
                    "preflight",
                    "payload",
                    "missing fasta_records / batch_records in job_spec",
                )
            records = job_spec.get("fasta_records") or []
            if not isinstance(records, list) or not records:
                _fail(
                    "preflight",
                    "payload",
                    "fasta_records must be a non-empty list",
                )

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

        # PAE packed as a compressed npz (float16) to keep the inline
        # payload small: an uncompressed float32 .npy is ~12 MB at the
        # 1500 aa cap, which overflows the result jsonb write. Mirrors the
        # ColabFold/ESMFold encoding; consumers load np.load(...)["pae"].
        pae_np = np.asarray(pae, dtype=np.float16)
        buf = io.BytesIO()
        np.savez_compressed(buf, pae=pae_np)
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


def _hhsearch_available() -> bool:
    """True if hhsearch is on $PATH.

    The AF2 image does not currently ship hhsuite, so colabfold_batch
    crashes with ``FileNotFoundError: 'hhsearch'`` whenever
    ``--templates`` is passed. We probe once and downgrade
    use_templates to False if the binary is missing, instead of letting
    every record crash deep inside colabfold's MSA pipeline. Image-level
    fix (install hhsuite in the Dockerfile) is the proper long-term
    answer.
    """
    from shutil import which  # noqa: PLC0415
    return which("hhsearch") is not None


def _classify_record(sequence: str) -> tuple[str, int, int, list[str]]:
    """Split a batch record sequence on ``:`` chain breaks and return
    ``(model_preset, num_chains, total_aa, chains)``.

    Used by the consolidated batch path to pre-validate every record
    before colabfold_batch sees them, and to surface per-record
    classifications back to the result file.
    """
    chains = [c.strip().upper() for c in (sequence or "").split(":") if c.strip()]
    if not chains:
        raise ValueError("record parsed to zero chains")
    total_aa = sum(len(c) for c in chains)
    preset = "monomer" if len(chains) == 1 else "multimer"
    return preset, len(chains), total_aa, chains


def _write_consolidated_fasta(
    records: list[dict[str, Any]],
    dest: Path,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Write all valid batch records to a single multi-record FASTA.

    Each record gets a synthetic ``>d{idx:04d}`` header so output files
    can be unambiguously dispatched back to their input record (user
    display names may collide after regex normalisation). The returned
    map is keyed by safe_name; values carry the user-facing name + the
    pre-classified preset / chains / total_aa, plus a ``design_start``
    timestamp stamped lazily when the record is first parsed.

    Returns ``(record_map, n_validation_failures)``.
    """
    record_map: dict[str, dict[str, Any]] = {}
    n_failures = 0
    with open(dest, "w") as out:
        for i, rec in enumerate(records):
            name = str(rec.get("name") or f"fold_{i}").strip() or f"fold_{i}"
            sequence = rec.get("sequence") or ""
            try:
                preset, n_chains, total_aa, chains = _classify_record(sequence)
            except ValueError as exc:
                n_failures += 1
                logger.warning("design %s: bad input — %s", name, exc)
                continue
            if total_aa > MAX_TOTAL_AA:
                n_failures += 1
                logger.warning(
                    "design %s: %d aa exceeds per-record cap %d — skipping",
                    name, total_aa, MAX_TOTAL_AA,
                )
                continue
            safe_name = f"d{i:04d}"
            joined = ":".join(chains)
            out.write(f">{safe_name}\n{joined}\n")
            record_map[safe_name] = {
                "index": i,
                "name": name,
                "model_preset": preset,
                "num_chains": n_chains,
                "total_aa": total_aa,
            }
    return record_map, n_failures


def _run_colabfold_consolidated(
    fasta_path: Path,
    *,
    num_recycles: int,
    use_templates: bool,
    use_msa: bool,
    workdir: Path,
    timeout: int,
) -> tuple[subprocess.Popen, Path]:
    """Spawn colabfold_batch in the background over a multi-record FASTA.

    Mirrors :func:`run_colabfold` but uses ``--model-type auto`` so
    colabfold dispatches monomer vs multimer per record without the
    caller having to group, and returns a live ``Popen`` so the caller
    can poll the output directory for streamed per-record results.
    """
    out_dir = workdir / "af2_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "colabfold_batch",
        "--data", str(COLABFOLD_CACHE_DIR),
        "--num-recycle", str(num_recycles),
        "--num-models", "1",
        # auto = monomer when single chain in record, multimer when ':'
        # joins multiple chains. Lets one process handle a mixed batch.
        "--model-type", "auto",
    ]
    if not use_msa:
        cmd += ["--msa-mode", "single_sequence"]
    if use_templates:
        cmd += ["--templates"]
    cmd += [str(fasta_path), str(out_dir)]

    logger.info("colabfold consolidated cmd: %s", " ".join(cmd))

    env = dict(os.environ)
    if os.path.isdir("/opt/jax_cache"):
        env.setdefault("JAX_COMPILATION_CACHE_DIR", "/opt/jax_cache")
    else:
        env.setdefault("JAX_COMPILATION_CACHE_DIR", "/tmp/jax_cache")
    env.setdefault("JAX_PERSISTENT_CACHE_MIN_COMPILE_TIME_SECS", "0")
    env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    env.setdefault("TF_FORCE_GPU_ALLOW_GROWTH", "true")
    env.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
    env.setdefault("XLA_PYTHON_CLIENT_ALLOCATOR", "platform")
    env.setdefault("XLA_PYTHON_CLIENT_MEM_FRACTION", "4.0")
    env.setdefault("TF_FORCE_UNIFIED_MEMORY", "1")
    env.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")

    # Live-stream stdout/stderr to Modal logs (same as run_colabfold).
    process = subprocess.Popen(
        cmd,
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    return process, out_dir


def _stream_consolidated_results(
    process: subprocess.Popen,
    out_dir: Path,
    record_map: dict[str, dict[str, Any]],
    on_design_ready: Any,
    timeout: float,
    poll_interval: float = 5.0,
) -> int:
    """Poll ``out_dir`` while ``process`` runs.

    Calls ``on_design_ready(safe_name, scores_path, pdb_path)`` exactly
    once per record as soon as both its rank_001 scores JSON and PDB
    are present. Returns the process exit code. Raises
    ``subprocess.TimeoutExpired`` if ``timeout`` elapses before the
    process exits.
    """
    start = time.time()
    seen: set[str] = set()
    safe_name_pat = re.compile(r"^(d\d{4})_scores_rank_001")
    while True:
        rc = process.poll()
        # Sweep for newly-completed records.
        for json_path in sorted(out_dir.glob("*_scores_rank_001*.json")):
            match = safe_name_pat.match(json_path.name)
            if not match:
                continue
            safe_name = match.group(1)
            if safe_name in seen or safe_name not in record_map:
                continue
            # Find matching PDB (prefer relaxed if present).
            relaxed = sorted(out_dir.glob(f"{safe_name}_*relaxed_rank_001*.pdb"))
            relaxed = [p for p in relaxed if "_unrelaxed_" not in p.name]
            unrelaxed = sorted(out_dir.glob(f"{safe_name}_*unrelaxed_rank_001*.pdb"))
            pdb_candidates = relaxed or unrelaxed
            if not pdb_candidates:
                # JSON written, PDB still being flushed — wait next poll.
                continue
            seen.add(safe_name)
            try:
                on_design_ready(safe_name, json_path, pdb_candidates[0])
            except Exception as exc:
                logger.warning(
                    "design %s: streaming dispatch failed — %s", safe_name, exc
                )
        if rc is not None:
            return rc
        if time.time() - start > timeout:
            process.kill()
            try:
                process.wait(timeout=10)
            except Exception:
                pass
            raise subprocess.TimeoutExpired(args=process.args, timeout=timeout)
        time.sleep(poll_interval)


def _run_single(payload: dict[str, Any], start: float) -> None:
    """Existing single-fold path."""
    job_spec = payload.get("job_spec") or {}
    tier = str(payload.get("tier") or "").lower() or "standalone"

    parameters = job_spec.get("parameters") or {}
    model_preset = str(parameters.get("model_preset") or "monomer").lower()
    try:
        num_recycles = int(parameters.get("num_recycles", 3))
    except (TypeError, ValueError):
        num_recycles = 3
    use_templates = bool(parameters.get("use_templates", True))

    num_recycles = max(RECYCLES_MIN, min(RECYCLES_MAX, num_recycles))

    if tier == "smoke":
        num_recycles = 1
        use_templates = False
        use_msa = False
    else:
        use_msa = True

    # Image-level guard: drop --templates when hhsearch is absent so we
    # surface a single warning instead of a per-fold colabfold crash.
    if use_templates and not _hhsearch_available():
        logger.warning(
            "use_templates=True but hhsearch is not on PATH on this image — "
            "forcing use_templates=False to avoid colabfold MSA crash"
        )
        use_templates = False

    with tempfile.TemporaryDirectory(prefix="af2_", dir="/tmp") as _td:
        workdir = Path(_td)
        try:
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
        finally:
            # Ship the whole tree home before TemporaryDirectory
            # destroys it -- including on the _fail() SystemExit
            # paths, which are exactly the runs worth inspecting.
            archive_raw(workdir, "af2_single")

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


def _run_batch(
    payload: dict[str, Any], records: list[dict[str, Any]], start: float
) -> None:
    """Consolidated batch fold across N records with per-design streaming.

    All records are written to a single multi-record FASTA and folded by
    one ``colabfold_batch`` invocation with ``--model-type auto``.
    ColabFold loads the AF2 weights + pays the JAX JIT compile cost
    exactly once for the entire batch, then dispatches monomer vs
    multimer per record. Per-design heartbeats are emitted live by
    polling the output dir for each record's ``_scores_rank_001*.json``
    + ``_*rank_001*.pdb`` pair as colabfold writes them.

    Compared to the original per-record subprocess loop this skips
    N×(~10 s Python+JAX import overhead) on top of the JIT win — which
    on small batches is the dominant cost.
    """
    job_id = os.environ.get("JOB_ID", "")
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    job_token = payload.get("job_token", "") or os.environ.get("JOB_TOKEN", "")
    upload_endpoint = payload.get("upload_urls_endpoint", "")
    if not upload_endpoint:
        _fail(
            "preflight",
            "upload_urls_endpoint",
            "upload_urls_endpoint missing from payload — batch preset "
            "requires the web flow to populate it",
        )

    job_spec = payload.get("job_spec") or {}
    parameters = job_spec.get("parameters") or {}
    try:
        num_recycles = int(parameters.get("num_recycles", 3))
    except (TypeError, ValueError):
        num_recycles = 3
    num_recycles = max(RECYCLES_MIN, min(RECYCLES_MAX, num_recycles))
    use_templates = bool(parameters.get("use_templates", True))

    # Image-level guard (see _hhsearch_available docstring).
    if use_templates and not _hhsearch_available():
        logger.warning(
            "use_templates=True but hhsearch is not on PATH on this image — "
            "forcing use_templates=False to avoid colabfold MSA crash"
        )
        use_templates = False

    designs_total = len(records)
    logger.info("af2 batch starting: designs=%d (consolidated)", designs_total)
    send_heartbeat(
        webhook_url, job_id,
        stage="loading_model",
        designs_completed=0,
        designs_total=designs_total,
    )

    designs_out: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="af2_batch_", dir="/tmp") as _td:
        workdir = Path(_td)
        try:
            fasta_path = workdir / "batch.fasta"
            record_map, n_failures = _write_consolidated_fasta(records, fasta_path)
            if not record_map:
                _write_result(
                    {
                        "status": "COMPLETED",
                        "tier": "batch",
                        "designs_total": designs_total,
                        "designs_completed": 0,
                        "n_failures": n_failures,
                        "designs": [],
                        "num_recycles": num_recycles,
                        "use_templates": use_templates,
                        "runtime_seconds": int(time.time() - start),
                        "provider_job_id": os.environ.get("JOB_ID", ""),
                    }
                )
                send_heartbeat(
                    webhook_url, job_id,
                    stage="complete",
                    designs_completed=0,
                    designs_total=designs_total,
                )
                logger.info(
                    "batch pipeline ok — 0/%d designs folded "
                    "(all records failed pre-validation), %d failures",
                    designs_total, n_failures,
                )
                return

            send_heartbeat(
                webhook_url, job_id,
                stage="folding",
                designs_completed=0,
                designs_total=designs_total,
            )
            fold_start = time.time()

            # Bound the consolidated subprocess by the Modal session
            # ceiling minus a margin for post-processing.
            max_session_s = int(os.environ.get("MAX_BATCH_TIMEOUT_S", "14000"))

            process, out_dir = _run_colabfold_consolidated(
                fasta_path=fasta_path,
                num_recycles=num_recycles,
                use_templates=use_templates,
                use_msa=True,
                workdir=workdir,
                timeout=max_session_s,
            )

            def _dispatch(safe_name: str, scores_path: Path, pdb_path: Path) -> None:
                """Per-record post-processing: parse scores, upload PDB, emit heartbeat."""
                rec_info = record_map[safe_name]
                try:
                    with open(scores_path) as fh:
                        scores = json.load(fh)
                except Exception as exc:
                    logger.warning(
                        "design %s: scores parse failed — %s",
                        rec_info["name"], exc,
                    )
                    rec_info["failed"] = True
                    return

                plddt_raw = scores.get("plddt") or []
                try:
                    plddt = [float(x) for x in plddt_raw]
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "design %s: plddt cast failed — %s",
                        rec_info["name"], exc,
                    )
                    rec_info["failed"] = True
                    return
                if not plddt:
                    logger.warning("design %s: empty plddt", rec_info["name"])
                    rec_info["failed"] = True
                    return

                try:
                    pdb_bytes = pdb_path.read_bytes()
                except OSError as exc:
                    logger.warning(
                        "design %s: PDB read failed — %s",
                        rec_info["name"], exc,
                    )
                    rec_info["failed"] = True
                    return

                user_name = rec_info["name"]
                pdb_key = (
                    f"{re.sub(r'[^A-Za-z0-9_-]+', '_', user_name)[:60] or 'fold'}.pdb"
                )
                try:
                    urls = request_upload_urls(upload_endpoint, job_token, [pdb_key])
                    upload_pdb(urls[pdb_key], pdb_bytes)
                except Exception as exc:
                    logger.warning(
                        "design %s: upload failed — %s",
                        user_name, exc,
                    )
                    rec_info["failed"] = True
                    return

                mean_plddt = round(sum(plddt) / len(plddt), 2)
                ptm = scores.get("ptm")
                iptm = scores.get("iptm")
                design_entry = {
                    "rank": rec_info["index"],
                    "name": user_name,
                    "pdb_key": pdb_key,
                    "mean_plddt": mean_plddt,
                    "iptm": float(iptm) if iptm is not None else None,
                    "ptm": float(ptm) if ptm is not None else None,
                    "total_aa": rec_info["total_aa"],
                    "num_chains": rec_info["num_chains"],
                    "model_preset": rec_info["model_preset"],
                    "runtime_seconds": int(time.time() - fold_start),
                }
                designs_out.append(design_entry)
                rec_info["done"] = True
                send_heartbeat(
                    webhook_url, job_id,
                    stage="folding",
                    designs_completed=len(designs_out),
                    designs_total=designs_total,
                    new_candidate=design_entry,
                )
                logger.info(
                    "  -> %s mean_pLDDT=%s ipTM=%s pTM=%s len=%d",
                    user_name, mean_plddt,
                    design_entry["iptm"], design_entry["ptm"],
                    design_entry["total_aa"],
                )

            # Stream results while colabfold_batch runs.
            try:
                exit_code = _stream_consolidated_results(
                    process=process,
                    out_dir=out_dir,
                    record_map=record_map,
                    on_design_ready=_dispatch,
                    timeout=max_session_s,
                )
            except subprocess.TimeoutExpired:
                logger.error(
                    "colabfold consolidated batch TIMEOUT after %ds — "
                    "%d/%d designs streamed before kill",
                    max_session_s, len(designs_out), designs_total,
                )
                _fail(
                    "tool-invocation",
                    "timeout",
                    f"colabfold_batch consolidated exceeded {max_session_s}s.",
                )
                return  # unreachable

            if exit_code != 0:
                logger.error(
                    "colabfold consolidated exit %d after %d/%d designs streamed",
                    exit_code, len(designs_out), designs_total,
                )
                # If we streamed at least one design we treat the batch as
                # partially-completed (consistent with the original loop's
                # SystemExit catch); otherwise fail hard.
                if not designs_out:
                    _fail(
                        "tool-invocation",
                        "exit",
                        f"colabfold_batch consolidated exited {exit_code} "
                        "with zero completed designs.",
                    )

            # Account for any records that colabfold skipped silently
            # (no output emitted): treat as failures so the counts add up.
            n_failures += sum(
                1
                for safe_name, info in record_map.items()
                if not info.get("done") and not info.get("failed")
            )
            n_failures += sum(
                1 for info in record_map.values() if info.get("failed")
            )
        finally:
            # Ship the whole tree home before TemporaryDirectory
            # destroys it -- including on the _fail() SystemExit
            # paths, which are exactly the runs worth inspecting.
            archive_raw(workdir, "af2_batch")

    runtime_seconds = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": "batch",
            "designs_total": designs_total,
            "designs_completed": len(designs_out),
            "n_failures": n_failures,
            "designs": designs_out,
            "num_recycles": num_recycles,
            "use_templates": use_templates,
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
        "batch pipeline ok — %d/%d designs folded, %d failures, runtime=%ds",
        len(designs_out), designs_total, n_failures, runtime_seconds,
    )


def main() -> None:
    start = time.time()
    payload = parse_payload()
    preflight(payload)

    job_spec = payload.get("job_spec") or {}
    batch_records = (
        job_spec.get("batch_records") if isinstance(job_spec, dict) else None
    )
    if batch_records:
        _run_batch(payload, list(batch_records), start)
    else:
        _run_single(payload, start)


if __name__ == "__main__":
    main()
