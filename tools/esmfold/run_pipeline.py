"""Modal entrypoint for D4 - ESMFold standalone (single-sequence fold).

Reads job configuration from the ``JOB_PAYLOAD`` env var (same RunPod-parity
shape the Kendrew pipelines + D1 MPNN + D3 ColabFold use), runs HuggingFace
``EsmForProteinFolding`` (the ``facebook/esmfold_v1`` 3B model), writes the
result to ``/tmp/smoke_results.json``. For smoke / standalone tiers the
wrapper returns this file inline via the Modal function return value - see
``tools/esmfold/modal_app.py``.

Contract (per docs/ATOMIC-TOOLS.md):

- ``preflight()`` is called first and must complete in <= 60 s. On any
  failure it writes ``{"status":"FAILED","error":{...}}`` to
  ``/tmp/smoke_results.json`` and ``sys.exit(1)`` so the build-time
  Layer-1 checks are not duplicated at runtime.
- ``run()`` loads the ESMFold checkpoint + tokenizer, runs a single
  forward pass on the caller's sequence, extracts the predicted PDB +
  per-residue pLDDT + (optional) pTM. Stub rejection: fails if the
  pLDDT array is all-identical, all-NaN, or mean-out-of-range.

ESMFold v1 specifics vs ColabFold (D3):
  - Monomer-only: no chain separator, no multimer head, no iptm output.
  - pAE / pTM may or may not be returned depending on whether the
    ``EsmForProteinFolding.infer_pdb()`` helper exposes them in the
    transformers version we pin. We handle both cases.
  - Outputs are produced directly from the model via its
    ``atom37_to_pdb`` / ``output_to_pdb`` helpers - no ``colabfold_batch``
    CLI to parse.

Environment variables (set by ``tools/esmfold/modal_app.py`` from the
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
      "pdb_b64": "...",              # base64 of the predicted PDB
      "plddt_per_residue": [...],    # floats in [0, 100], one per residue
      "mean_plddt": 87.4,
      "ptm": 0.79,                   # may be None if not exposed by checkpoint
      "sequence": "MQIFV...",
      "chain_count": 1,
      "total_length": 76,
      "runtime_seconds": 47,
      "provider_job_id": "<job_id>"
    }
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import shutil
import sys
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
logger = logging.getLogger("esmfold_pipeline")


SMOKE_TARGET_FASTA = "/opt/smoke_target.fasta"
SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
ESMFOLD_MODEL_ID = "facebook/esmfold_v1"
# Fixed drop point for the complete raw work-tree archive. This pipeline runs as
# a subprocess and so cannot mount a Modal Volume itself; the wrapper
# (``tools/esmfold/modal_app.py``) moves this file onto the
# ``ranomics-esmfold-raw`` Volume once the subprocess returns.
RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"

# Bounds enforced on the sequence. Mirrored from the tools-hub adapter
# validate() but re-checked here because the pipeline may be invoked
# directly (e.g. ``modal run`` for staging validation).
SEQ_LEN_MAX = 400
SEQ_LEN_MIN = 10
# Canonical residue alphabet (20 aa + X for unknown). ESMFold's tokenizer
# accepts only the canonical ESM-2 alphabet; flagging here gives the
# user a better error than the tokenizer's cryptic "unknown token".
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
# Raw-output capture
#
# A container must never decide which fields are worth keeping. ESMFold has no
# on-disk tool output tree the way a CLI-driven tool does: ``fold_with_loaded``
# hands back tensors and everything downstream is a projection of them.
# ``shape_output`` keeps pdb/pLDDT/pTM and float16-packs pAE; ``_fold_record``
# then drops pAE entirely; ``_run_batch``'s ``design_entry`` drops the
# per-residue pLDDT array too. Whatever is not written to the work dir here
# dies with the container and is recoverable only by paying for the GPU again.
# That is how ``design_iptm`` (the real binder:target interface) was lost on 460
# boltzgen designs, which were scored on ``iptm`` - a different quantity, ~2x
# high - and concluded on. Write the raw values; decide LOCALLY, where
# re-parsing is free.
#
# Nothing below gates on success, on candidate count or on what got uploaded: a
# zero-candidate run ships nothing today and is exactly the run whose tree you
# want. Nothing below may raise, either - a job that crashed before writing
# output is when the diagnostics matter most.
# ===========================================================================


def _safe_stem(name: str) -> str:
    """Filesystem-safe stem for a user-supplied (FASTA header) record name."""
    stem = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name or "")
    return stem.strip("._")[:80] or "design"


def _dump_raw_fold(raw_dir: Path, stem: str, raw: dict[str, Any]) -> None:
    """Persist ONE fold's untouched model output into the work dir.

    Writes the PDB verbatim plus a JSON sidecar carrying the FULL per-residue
    pLDDT array, pTM and the pAE matrix at the precision the model produced
    them - not the float16 npz ``shape_output`` packs, and not the subset
    ``_fold_record`` returns.

    Best-effort: never raises. Losing a dump must not cost the fold.
    """
    try:
        base = _safe_stem(stem)
        pdb_text = raw.get("pdb_text") or ""
        if pdb_text:
            (raw_dir / f"{base}.pdb").write_text(pdb_text)
        with open(raw_dir / f"{base}.raw.json", "w") as fh:
            json.dump(
                {
                    "plddt_per_residue": raw.get("plddt_per_residue"),
                    "ptm": raw.get("ptm"),
                    "pae": raw.get("pae"),
                },
                fh,
            )
    except Exception as exc:
        logger.warning("raw capture: dump of %r failed (non-fatal): %s", stem, exc)


def _archive_raw(work_dir: str) -> None:
    """Tar the COMPLETE work dir to ``RAW_ARCHIVE_PATH``. Never raises.

    Callers put this in a ``finally``, immediately before the teardown that
    destroys the tree, so it also runs on ``_fail()``'s ``sys.exit`` and on an
    unhandled raise.
    """
    # ``dest`` is bound here because the handler at the bottom deletes it and
    # the try does not assign it until after the isdir check. Reaching the
    # handler before then would raise UnboundLocalError; that is a NameError,
    # so the inner ``except OSError`` does not stop it, and it would leave a
    # function whose entire contract is "never raises" — while running inside a
    # ``finally``, where a raise overwrites the exit already in progress. Both
    # call sites pass ``str()`` of a live tempdir Path and cannot trigger it;
    # this keeps the contract true for callers that have not been audited.
    dest: str | None = None
    try:
        # The lazy import lives inside the guard, matching af2, so that even an
        # ImportError on this path is logged rather than raised.
        import tarfile  # noqa: PLC0415

        if not os.path.isdir(work_dir):
            logger.warning(
                "raw capture: %s is not a directory - nothing to archive", work_dir
            )
            return
        root = os.path.abspath(work_dir)
        dest = os.path.abspath(RAW_ARCHIVE_PATH)
        # The tar must never be written inside the tree it archives or it tars
        # itself. /tmp/raw_archive.tgz already sits outside a /tmp/<workdir>/,
        # but assert that rather than trust it.
        if dest == root or dest.startswith(root + os.sep):
            logger.error(
                "raw capture: refusing to write archive %s inside its own source %s",
                dest,
                root,
            )
            return
        # Stream to a file; never io.BytesIO, which costs ~3-4x peak RSS.
        with tarfile.open(dest, "w:gz") as tf:
            tf.add(root, arcname=os.path.basename(root) or "work")
        logger.info(
            "raw capture: archived %s -> %s (%d bytes)",
            root,
            dest,
            os.path.getsize(dest),
        )
    except Exception as exc:
        logger.warning("raw capture: archive failed (non-fatal): %s", exc)
        # A crash mid-write (e.g. ENOSPC) can leave a truncated but still-openable .tgz at
        # the destination; the wrapper parks whatever exists. Remove the partial so a failed
        # capture parks NOTHING rather than a tar that reports success but cannot be read.
        try:
            if dest is not None and os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass


# ===========================================================================
# Heartbeat + upload helpers (batch preset only).
# Identical contract to tools/boltz2/run_pipeline.py so the hub-side
# webhook ingestion handles both tools without per-tool branches.
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
    """Fire-and-forget heartbeat. Never raises — a flaky webhook hop
    must not abort an in-progress batch fold.
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
    # 1. payload shape - job_spec exists (no required inner keys for
    #    ESMFold since the FASTA is delivered separately).
    if "job_spec" not in payload:
        _fail("preflight", "payload", "missing required key: job_spec")

    # 2. transformers + torch importable
    try:
        import torch  # noqa: PLC0415
    except Exception as exc:
        _fail("preflight", "torch", f"torch import failed: {exc}")
    try:
        import transformers  # noqa: F401, PLC0415
    except Exception as exc:
        _fail("preflight", "transformers", f"transformers import failed: {exc}")

    # 3. CUDA visible
    try:
        if not torch.cuda.is_available():
            _fail(
                "preflight",
                "cuda",
                "torch.cuda.is_available() returned False - no GPU visible",
            )
        device_count = torch.cuda.device_count()
        if device_count < 1:
            _fail(
                "preflight",
                "cuda",
                f"torch.cuda.device_count() returned {device_count}",
            )
        device_name = torch.cuda.get_device_name(0)
        logger.info("preflight: CUDA ok, device=%s", device_name)
    except Exception as exc:
        _fail("preflight", "cuda", f"CUDA probe failed: {exc}")

    # 4. ESMFold weights present in HF cache. With both HF_HOME and
    # TRANSFORMERS_CACHE set in the Dockerfile, transformers writes
    # directly under TRANSFORMERS_CACHE (no /hub/ subdir). Check both
    # layouts so we are robust to env-var changes.
    hf_home = os.environ.get("HF_HOME", "/opt/hf_cache")
    transformers_cache = os.environ.get("TRANSFORMERS_CACHE", hf_home)
    candidate_dirs = [
        Path(hf_home) / "hub",                # standard hf_hub layout
        Path(transformers_cache),             # flat layout when TRANSFORMERS_CACHE set
        Path(hf_home),                        # fallback
    ]
    hub_dir = next((d for d in candidate_dirs if d.is_dir()), None)
    if hub_dir is None:
        _fail(
            "preflight",
            "weights",
            f"no HF cache dir found among {[str(d) for d in candidate_dirs]}",
        )
    # The ``facebook/esmfold_v1`` snapshot lives under
    # ``models--facebook--esmfold_v1/`` regardless of layout.
    esmfold_snapshots = list(hub_dir.glob("models--facebook--esmfold_v1"))
    if not esmfold_snapshots:
        _fail(
            "preflight",
            "weights",
            f"facebook/esmfold_v1 snapshot not found under {hub_dir} "
            f"(checked: {[str(d) for d in candidate_dirs]})",
        )

    # 5. /tmp writable
    try:
        probe = Path("/tmp") / ".esmfold_preflight_probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        _fail("preflight", "tmp", f"/tmp is not writable: {exc}")

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
      1. Smoke tier -> baked fixture (no network hop).
      2. ``job_spec.fasta_text`` (inline text from the tools-hub form).
      3. ``input_presigned_url`` (user uploaded a .fasta file - same
         staging flow as MPNN's PDB upload; not used today but kept for
         parity with D3).
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
    """Parse + sanity-check the FASTA. Returns ``{"sequence": str, ...}``.

    ESMFold v1 is monomer-only. We reject multi-record FASTA and any
    ``:`` chain separator inside a single record.

    Rejects:
      - empty file
      - zero records
      - more than one ``>`` record (monomer-only per ATOMIC-TOOLS.md D4)
      - sequence outside [SEQ_LEN_MIN, SEQ_LEN_MAX]
      - any residue outside the canonical 20 aa + X alphabet
      - any ':' separator (ColabFold multimer convention)
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
            "multimer",
            (
                f"FASTA has {len(records)} records - ESMFold v1 is "
                "monomer-only. Use ColabFold (D3) or AF2 (D2) for multimers."
            ),
        )

    rec = records[0]
    seq = str(rec.seq).upper().strip()
    if not seq:
        _fail("input", "fasta_empty", "record has no sequence")

    if ":" in seq:
        _fail(
            "input",
            "multimer",
            (
                "sequence contains ':' (ColabFold multimer separator) - "
                "ESMFold v1 is monomer-only"
            ),
        )

    if len(seq) < SEQ_LEN_MIN:
        _fail(
            "input",
            "seq_length",
            f"sequence is {len(seq)} aa - min {SEQ_LEN_MIN}",
        )
    if len(seq) > SEQ_LEN_MAX:
        _fail(
            "input",
            "seq_length",
            f"sequence is {len(seq)} aa - max {SEQ_LEN_MAX} "
            "for ESMFold-3B on A100-40GB within the 10-min budget",
        )
    non_canonical = set(seq) - CANONICAL_AA
    if non_canonical:
        _fail(
            "input",
            "seq_alphabet",
            f"sequence contains non-canonical residues: "
            f"{sorted(non_canonical)}",
        )

    return {
        "sequence": seq,
        "total_length": len(seq),
        "chain_count": 1,
    }


# ===========================================================================
# ESMFold invocation
# ===========================================================================


def load_esmfold() -> tuple[Any, Any]:
    """Load the ESMFold-3B tokenizer + model once and return both.

    Hoisted out of ``run_esmfold`` so the batch path can reuse a single
    loaded model across N folds — model + tokenizer load is ~30 s cold
    and dominates per-fold wall-clock at small sequence lengths.
    """
    import torch  # noqa: PLC0415, F401  (CUDA bind imported lazily)
    from transformers import AutoTokenizer, EsmForProteinFolding  # noqa: PLC0415

    logger.info("loading ESMFold tokenizer + model from %s", ESMFOLD_MODEL_ID)
    tokenizer = AutoTokenizer.from_pretrained(ESMFOLD_MODEL_ID)
    model = EsmForProteinFolding.from_pretrained(
        ESMFOLD_MODEL_ID, low_cpu_mem_usage=True
    )
    # ``.eval()`` first to switch off dropout etc., then move to GPU.
    model = model.eval().cuda()
    return tokenizer, model


def fold_with_loaded(tokenizer: Any, model: Any, sequence: str) -> dict[str, Any]:
    """Run one forward pass against an already-loaded ESMFold model.

    Returns the same shape ``run_esmfold`` did pre-split: pdb_text,
    plddt_per_residue, ptm, pae. Pure inference — no model construction
    cost. The caller owns the model lifetime.
    """
    import torch  # noqa: PLC0415

    # ESMFold's structure module benefits from fp16 on the ESM-2 trunk
    # for longer sequences. Leave default chunk size (None) for <= 400 aa.
    logger.info("ESMFold running forward on %d aa", len(sequence))

    tokenized = tokenizer(
        [sequence],
        return_tensors="pt",
        add_special_tokens=False,
    )
    input_ids = tokenized["input_ids"].cuda()

    with torch.no_grad():
        output = model(input_ids)

    # pLDDT extraction. HuggingFace ``EsmForProteinFolding`` returns
    # ``output.plddt`` on a 0-100 scale. Shape varies by transformers
    # minor version: 4.35.x returns ``(batch, seq_len, 37)`` per-atom
    # confidence; older branches returned ``(batch, seq_len)``. Collapse
    # to per-residue by atom-masked mean when the atom axis is present.
    plddt_tensor = output.plddt
    if plddt_tensor.dim() == 3:
        # Average over atom37 axis, ignoring missing atoms. The atom
        # presence mask is exposed on the output dict. Fall back to a
        # simple mean if the mask isn't available on this transformers
        # version (both paths yield numerically close per-residue
        # confidence for the HF output_to_pdb writer's sake).
        atom_mask = None
        for attr in ("atom37_atom_exists", "atom37_mask"):
            atom_mask = getattr(output, attr, None)
            if atom_mask is not None:
                break
        if atom_mask is not None:
            masked = plddt_tensor * atom_mask
            denom = atom_mask.sum(dim=-1).clamp(min=1)
            per_residue = masked.sum(dim=-1) / denom
        else:
            per_residue = plddt_tensor.mean(dim=-1)
    else:
        per_residue = plddt_tensor
    plddt_list = per_residue[0].detach().cpu().float().tolist()

    # Generate PDB text using the model's output_to_pdb helper.
    try:
        pdb_texts = model.output_to_pdb(output)
        pdb_text = pdb_texts[0] if pdb_texts else ""
    except Exception as exc:
        _fail("tool-invocation", "output_to_pdb", f"output_to_pdb failed: {exc}")

    if not pdb_text or len(pdb_text) < 200:
        _fail(
            "tool-invocation",
            "pdb_empty",
            f"output_to_pdb returned {len(pdb_text or '')} bytes - not a real PDB",
        )

    # pTM: transformers 4.35.x exposes ptm on the output when the
    # checkpoint has the PAE head. Handle the None case cleanly.
    ptm_val: float | None = None
    ptm_attr = getattr(output, "ptm", None)
    if ptm_attr is not None:
        try:
            ptm_val = float(ptm_attr.detach().cpu().item())
        except Exception as exc:
            logger.warning("could not extract ptm (non-fatal): %s", exc)
            ptm_val = None

    # pAE: some checkpoints include it, most don't. Handle the None case
    # cleanly - per ATOMIC-TOOLS.md gotchas, some ESMFold implementations
    # return pae as None and the results template must not render a broken
    # PAE panel.
    pae_list: list[list[float]] | None = None
    pae_attr = getattr(output, "predicted_aligned_error", None)
    if pae_attr is not None:
        try:
            pae_arr = pae_attr[0].detach().cpu().float()
            pae_list = pae_arr.tolist()
        except Exception as exc:
            logger.warning("could not extract pae (non-fatal): %s", exc)
            pae_list = None

    return {
        "pdb_text": pdb_text,
        "plddt_per_residue": [float(x) for x in plddt_list],
        "ptm": ptm_val,
        "pae": pae_list,
    }


def run_esmfold(sequence: str) -> dict[str, Any]:
    """Single-shot fold — load model + fold + tear down.

    Kept for the standalone path's backward compatibility. The batch
    path calls ``load_esmfold`` once and ``fold_with_loaded`` per
    record to skip the ~30 s reload tax.
    """
    tokenizer, model = load_esmfold()
    return fold_with_loaded(tokenizer, model, sequence)


# ===========================================================================
# Output parser / shaping + stub rejection
# ===========================================================================


def shape_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape the raw ESMFold output into the D4 JSON wire schema.

    PDB text -> b64. pAE (optional) -> b64 npz-packed float16 matching
    the D3 ColabFold shape so the results template can share display
    logic.
    """
    import io  # noqa: PLC0415

    pdb_bytes = raw["pdb_text"].encode("utf-8")
    pdb_b64 = base64.b64encode(pdb_bytes).decode("ascii")

    pae_b64 = ""
    pae_raw = raw.get("pae")
    if pae_raw:
        try:
            import numpy as np  # noqa: PLC0415

            arr = np.array(pae_raw, dtype=np.float16)
            buf = io.BytesIO()
            np.savez_compressed(buf, pae=arr)
            pae_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:
            logger.warning("PAE packing failed (non-fatal): %s", exc)

    plddt = raw["plddt_per_residue"]
    mean_plddt = sum(plddt) / len(plddt) if plddt else 0.0

    return {
        "pdb_b64": pdb_b64,
        "plddt_per_residue": plddt,
        "pae_matrix_b64": pae_b64,
        "ptm": raw.get("ptm"),
        "mean_plddt": round(mean_plddt, 2),
    }


def reject_stub(parsed: dict[str, Any]) -> None:
    """Stub-rejection guard. Per ATOMIC-TOOLS.md D4 section.

    ESMFold silent-stub failure modes:

    1. pLDDT array is all-NaN - weights never loaded / wrong dtype.
    2. pLDDT array is all-identical - the model emitted a constant
       tensor. AF2's pLDDT=0.96 bug (PXDesign) is the cautionary tale;
       the same pattern can occur with ESM-2 if the folding head is
       skipped.
    3. mean pLDDT is implausible (<=0 or >100) - units got scrambled.
    4. Empty PDB - output_to_pdb returned the wrong thing.

    Note: unlike ColabFold D3, we do NOT check iptm/ptm == 0.0 because
    ESMFold v1's pTM head is optional (may legitimately be None).
    """
    plddt = parsed.get("plddt_per_residue") or []

    if not plddt:
        _fail("parser", "stub", "pLDDT array is empty after parse")

    # NaN / infinite check
    if any(math.isnan(x) or math.isinf(x) for x in plddt):
        _fail(
            "parser",
            "stub",
            "pLDDT array contains NaN or infinite - ESMFold silent-stub signature",
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
                f"(value={plddt[0]:.4f}) - ESMFold silent-stub signature, "
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

    pdb_b64 = parsed.get("pdb_b64") or ""
    if len(pdb_b64) < 100:
        _fail(
            "parser",
            "stub",
            f"pdb_b64 is {len(pdb_b64)} chars - not a real PDB payload",
        )

    # Decode + sanity-check the PDB text. Must contain at least one ATOM
    # record and all zeros would indicate degenerate coordinates (e.g.
    # the model returned the identity output).
    try:
        pdb_text = base64.b64decode(pdb_b64).decode("utf-8", errors="ignore")
    except Exception as exc:
        _fail("parser", "stub", f"pdb_b64 failed to decode: {exc}")

    if "ATOM" not in pdb_text:
        _fail("parser", "stub", "PDB contains no ATOM records")

    # Check for all-zero coordinates across the first handful of ATOM
    # records (sufficient signal without scanning the whole structure).
    atom_lines = [ln for ln in pdb_text.splitlines() if ln.startswith("ATOM")][:20]
    if atom_lines:
        nonzero = 0
        for ln in atom_lines:
            # Column slice per PDB spec: x=31:38, y=39:46, z=47:54.
            try:
                x = float(ln[30:38].strip() or "0")
                y = float(ln[38:46].strip() or "0")
                z = float(ln[46:54].strip() or "0")
            except ValueError:
                continue
            if abs(x) + abs(y) + abs(z) > 1e-3:
                nonzero += 1
        if nonzero == 0:
            _fail(
                "parser",
                "stub",
                "all ATOM coordinates are zero - degenerate PDB output",
            )


# ===========================================================================
# Main
# ===========================================================================


def _validate_record_inline(name: str, seq: str) -> str:
    """Mirror of ``validate_fasta`` for an already-parsed batch record.

    Skips the file-IO + BioPython parse path; returns a clean uppercase
    sequence. Raises ``ValueError`` with a per-record explanation on
    failure so the batch loop can mark the record as failed without
    aborting the whole job.
    """
    seq = (seq or "").strip().upper()
    if not seq:
        raise ValueError(f"record {name!r} has no sequence")
    if ":" in seq:
        raise ValueError(
            f"record {name!r} contains ':' (multimer separator) — ESMFold v1 "
            "is monomer-only"
        )
    if len(seq) < SEQ_LEN_MIN:
        raise ValueError(f"record {name!r} is {len(seq)} aa — min {SEQ_LEN_MIN}")
    if len(seq) > SEQ_LEN_MAX:
        raise ValueError(
            f"record {name!r} is {len(seq)} aa — max {SEQ_LEN_MAX} for "
            "ESMFold-3B on A100-40GB within the per-fold budget"
        )
    non_canonical = set(seq) - CANONICAL_AA
    if non_canonical:
        raise ValueError(
            f"record {name!r} contains non-canonical residues: "
            f"{sorted(non_canonical)}"
        )
    return seq


def _fold_record(
    tokenizer: Any,
    model: Any,
    name: str,
    seq: str,
    raw_dir: Path | None = None,
    raw_stem: str = "",
) -> dict[str, Any]:
    """Fold one record against a pre-loaded ESMFold model.

    Returns the candidate-table-friendly shape (``rank`` and ``pdb_key``
    are set by the caller after the upload). Raises ``SystemExit`` via
    ``reject_stub``/``_fail`` if the output is degenerate, so the batch
    loop can mark this record as a failure and move on without aborting
    the run.

    ``raw_dir``/``raw_stem`` are capture-only: when set, the untouched model
    output is dumped there BEFORE ``reject_stub`` can kill the fold, because a
    stub-rejected fold is precisely the one whose numbers you need to see.
    """
    raw_output = fold_with_loaded(tokenizer, model, seq)
    if raw_dir is not None:
        _dump_raw_fold(raw_dir, raw_stem or name, raw_output)
    parsed = shape_output(raw_output)
    reject_stub(parsed)
    plddt = parsed.get("plddt_per_residue") or []
    return {
        "name": name,
        "sequence": seq,
        "total_length": len(seq),
        "mean_plddt": parsed.get("mean_plddt"),
        "ptm": parsed.get("ptm"),
        "pdb_b64": parsed.get("pdb_b64"),
        "plddt_per_residue": plddt,
    }


def _run_single(payload: dict[str, Any], start: float) -> None:
    """Existing single-fold path. Writes inline result; no streaming."""
    tier = str(payload.get("tier") or "").lower() or "standalone"

    with tempfile.TemporaryDirectory(prefix="esmfold_", dir="/tmp") as _td:
        workdir = Path(_td)
        try:
            fasta_path = resolve_input_fasta(payload, workdir)
            fasta_summary = validate_fasta(fasta_path)
            raw_output = run_esmfold(fasta_summary["sequence"])
            _dump_raw_fold(workdir, "fold", raw_output)
        finally:
            # Tar INSIDE the with-block: ``TemporaryDirectory.__exit__`` is the
            # teardown here, and ``_fail()``'s sys.exit unwinds straight through
            # it, so an input-resolution or fold crash would otherwise take the
            # tree with it.
            _archive_raw(str(workdir))

    parsed = shape_output(raw_output)
    reject_stub(parsed)

    runtime_seconds = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": tier,
            **parsed,
            "sequence": fasta_summary["sequence"],
            "chain_count": fasta_summary["chain_count"],
            "total_length": fasta_summary["total_length"],
            "runtime_seconds": runtime_seconds,
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    logger.info(
        "pipeline ok - mean pLDDT=%.2f, length=%d, runtime=%ds",
        parsed.get("mean_plddt", 0.0),
        fasta_summary["total_length"],
        runtime_seconds,
    )


def _run_batch(
    payload: dict[str, Any], records: list[dict[str, Any]], start: float
) -> None:
    """Own the batch work dir + raw capture around ``_run_batch_folds``.

    Unlike ``_run_single`` this path has no work dir of its own - records
    arrive inline in the payload and every fold happens in memory - so one is
    made here purely to give the raw per-fold output somewhere to land.
    """
    raw_dir = Path(tempfile.mkdtemp(prefix="esmfold_batch_", dir="/tmp"))
    try:
        _run_batch_folds(payload, records, start, raw_dir)
    finally:
        # Tar immediately before the rmtree that destroys the tree, on every
        # exit path: clean return, ``_fail()``'s SystemExit, unhandled raise.
        _archive_raw(str(raw_dir))
        shutil.rmtree(raw_dir, ignore_errors=True)


def _run_batch_folds(
    payload: dict[str, Any],
    records: list[dict[str, Any]],
    start: float,
    raw_dir: Path,
) -> None:
    """Sequential batch fold across N records with per-design streaming.

    One container loads ESMFold-3B once and folds each record in turn.
    Each successful fold uploads its PDB via the presigned PUT URL and
    fires a ``new_candidate`` heartbeat so the live job page updates as
    folds complete. Failures (bad input, stub rejection, upload error)
    increment ``n_failures`` and do not abort the rest of the batch.
    """
    # The batch input arrives inline in the payload rather than as a file, so
    # write it down: this is the path's equivalent of _run_single's input.fasta.
    # First thing in the function so even the upload_urls_endpoint _fail below
    # still leaves the inputs in the archive.
    try:
        (raw_dir / "batch_records.json").write_text(json.dumps(records, indent=2))
    except Exception as exc:
        logger.warning(
            "raw capture: could not write batch_records.json (non-fatal): %s", exc
        )

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

    designs_total = len(records)
    logger.info("esmfold batch starting: designs=%d", designs_total)
    send_heartbeat(
        webhook_url, job_id,
        stage="loading_model",
        designs_completed=0,
        designs_total=designs_total,
    )

    # Load ESMFold-3B ONCE for the whole batch. Without this hoist, the
    # per-record fold path would re-read weights + tokenizer (~30 s
    # each) which dominates wall-clock at the small per-fold inference
    # cost and blows the Modal session budget on large batches.
    tokenizer, model = load_esmfold()

    designs_out: list[dict] = []
    n_failures = 0
    send_heartbeat(
        webhook_url, job_id,
        stage="folding",
        designs_completed=0,
        designs_total=designs_total,
    )

    for i, rec in enumerate(records):
        name = str(rec.get("name") or f"design_{i}").strip() or f"design_{i}"
        try:
            seq = _validate_record_inline(name, rec.get("sequence") or "")
        except ValueError as exc:
            n_failures += 1
            logger.warning("design %s: validation failed — %s", name, exc)
            continue

        design_start = time.time()
        logger.info(
            "=== folding %d/%d %s (%d aa) ===",
            i + 1, designs_total, name, len(seq),
        )
        try:
            folded = _fold_record(
                tokenizer, model, name, seq, raw_dir, f"{i:04d}_{name}"
            )
        except SystemExit:
            # reject_stub() calls _fail() which writes a FAILED smoke
            # result + sys.exit(1). For batch we want to keep going.
            n_failures += 1
            logger.warning("design %s: stub-rejection killed the fold", name)
            continue
        except Exception as exc:
            n_failures += 1
            logger.warning("design %s: fold raised — %s", name, exc)
            continue

        pdb_key = f"{name}.pdb"
        try:
            pdb_text = base64.b64decode(folded["pdb_b64"]).decode("utf-8")
            urls = request_upload_urls(upload_endpoint, job_token, [pdb_key])
            upload_pdb(urls[pdb_key], pdb_text.encode("utf-8"))
        except Exception as exc:
            n_failures += 1
            logger.warning("design %s: upload failed (%s)", name, exc)
            continue

        design_entry = {
            "rank": i,
            "name": name,
            "pdb_key": pdb_key,
            "mean_plddt": folded.get("mean_plddt"),
            "ptm": folded.get("ptm"),
            "total_length": folded.get("total_length"),
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
            "  -> %s mean_pLDDT=%s ptm=%s len=%d",
            name,
            design_entry["mean_plddt"],
            design_entry["ptm"],
            design_entry["total_length"],
        )

    runtime_seconds = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": "batch",
            "designs_total": designs_total,
            "designs_completed": len(designs_out),
            "n_failures": n_failures,
            "designs": designs_out,
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
    batch_records = job_spec.get("batch_records") if isinstance(job_spec, dict) else None
    if batch_records:
        _run_batch(payload, list(batch_records), start)
    else:
        _run_single(payload, start)


if __name__ == "__main__":
    main()
