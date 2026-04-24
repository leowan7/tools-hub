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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("esmfold_pipeline")


SMOKE_TARGET_FASTA = "/opt/smoke_target.fasta"
SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
ESMFOLD_MODEL_ID = "facebook/esmfold_v1"

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

    # 4. ESMFold weights present in HF cache
    hf_home = os.environ.get("HF_HOME", "/opt/hf_cache")
    hub_dir = Path(hf_home) / "hub"
    if not hub_dir.is_dir():
        _fail(
            "preflight",
            "weights",
            f"HF hub dir missing: {hub_dir}",
        )
    # The ``facebook/esmfold_v1`` snapshot lives under
    # ``hub/models--facebook--esmfold_v1/``. Its presence confirms the
    # build-time bake succeeded.
    esmfold_snapshots = list(hub_dir.glob("models--facebook--esmfold_v1"))
    if not esmfold_snapshots:
        _fail(
            "preflight",
            "weights",
            f"facebook/esmfold_v1 snapshot not found under {hub_dir}",
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


def run_esmfold(sequence: str) -> dict[str, Any]:
    """Load ESMFold-3B and fold the single-sequence input.

    Returns a dict with:
      pdb_text: str             # PDB ATOM records as text
      plddt_per_residue: list[float]  # floats in [0, 100]
      ptm: float | None         # pTM score if exposed by this checkpoint
      pae: list[list[float]] | None   # pAE matrix if exposed (often None)

    Uses the HuggingFace ``EsmForProteinFolding.infer_pdb`` helper which
    returns the model's output PDB string directly. Per-residue pLDDT is
    read from the model's forward-pass output (``output.plddt``), which
    is on 0-100 scale already.
    """
    import torch  # noqa: PLC0415
    from transformers import AutoTokenizer, EsmForProteinFolding  # noqa: PLC0415

    logger.info("loading ESMFold tokenizer + model from %s", ESMFOLD_MODEL_ID)
    tokenizer = AutoTokenizer.from_pretrained(ESMFOLD_MODEL_ID)
    model = EsmForProteinFolding.from_pretrained(
        ESMFOLD_MODEL_ID, low_cpu_mem_usage=True
    )
    # ``.eval()`` first to switch off dropout etc., then move to GPU.
    model = model.eval().cuda()
    # ESMFold's structure module benefits from fp16 on the ESM-2 trunk
    # for longer sequences. Leave default chunk size (None) for <= 400 aa.
    logger.info("ESMFold loaded; running forward on %d aa", len(sequence))

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


def main() -> None:
    start = time.time()
    payload = parse_payload()
    preflight(payload)

    tier = str(payload.get("tier") or "").lower() or "standalone"

    with tempfile.TemporaryDirectory(prefix="esmfold_", dir="/tmp") as _td:
        workdir = Path(_td)
        fasta_path = resolve_input_fasta(payload, workdir)
        fasta_summary = validate_fasta(fasta_path)
        raw_output = run_esmfold(fasta_summary["sequence"])

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


if __name__ == "__main__":
    main()
