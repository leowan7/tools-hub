"""Modal entrypoint for ESMFold2 design (gradient inversion).

Reads job configuration from the ``JOB_PAYLOAD`` env var (same shape as
the MPNN / AF2 / ColabFold / ESMFold / Boltz-2 pipelines), runs the
upstream ``ESMFold2Design.design`` gradient-descent loop, and writes
the per-design summary to ``/tmp/smoke_results.json``. The Modal wrapper
returns this file inline via the function return value — see
``tools/esmfold2_design/modal_app.py``.

Environment variables (set by ``modal_app.py``):

    JOB_PAYLOAD     JSON: job_spec + input_presigned_url + upload_urls_endpoint + tier
    WEBHOOK_URL     URL to POST results to (unused at launch; see TODO)
    JOB_ID          tool_jobs row id (used for log prefixing)
    JOB_TOKEN       Job-specific auth token
    JOB_TIER        ``minibinder`` | ``scfv``

job_spec keys:

    preset                str   minibinder | scfv
    target_name           str?  one of TARGET_PRESETS or null
    target_sequence       str?  pasted target if target_name is null
    binder_name           str   minibinder | <framework>_framework_vhvl
    is_antibody           bool
    seed                  int
    batch_size            int   1-6
    use_scaling_critics   bool

Output shape (``/tmp/smoke_results.json``)::

    {
      "status": "COMPLETED",
      "tier": "minibinder",
      "preset": "minibinder",
      "is_antibody": false,
      "target_name": "ctla4",
      "target_label": "CTLA4",
      "binder_name": "minibinder",
      "binder_label": "minibinder",
      "designs_total": 1,
      "designs_completed": 1,
      "n_failures": 0,
      "use_scaling_critics": false,
      "trajectory_steps": 150,
      "best_sequence": "MAEK...",
      "designs": [
        {
          "rank": 0,
          "name": "design_0",
          "pdb_key": "design_0_complex.pdb",
          "designed_sequence": "MAEK...",
          "iptm": 0.74,
          "distogram_iptm_proxy": 0.62,
          "cdr_distogram_iptm_proxy": null,
          "final_loss": 0.31,
          "isoelectric_point": 5.4,
          "filter_status": "strict_pass"
        }
      ],
      "runtime_seconds": 612,
      "provider_job_id": "<job_id>"
    }

TODO (post first prod run, separate PR):
  - Send heartbeats with ``new_candidate`` events for the live UI.
  - Calibrate STRICT_IPTM / STRICT_CDR_IPTM_PROXY thresholds against
    the first 8-seed PD-L1 sweep instead of the conservative defaults.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

import requests

# /opt is the cookbook tutorial path planted by Dockerfile.modal.
sys.path.insert(0, "/opt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("esmfold2_design_pipeline")


SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
PDB_OUTPUT_DIR = Path("/tmp/results")

# Strict-pass thresholds. Open thread: tune against real PD-L1 sweep.
STRICT_IPTM = 0.55
STRICT_CDR_IPTM_PROXY = 0.50
STRICT_PI = 6.0  # minibinder only: pI < 6 for downstream displayability

# Critic name strings used by upstream binder_design.py.
CRITIC_REAL_IPTM = "ESMFold2-Experimental-Cutoff2025"
CRITIC_SCALING_PROXY = "ESMFold2-Experimental-Fast-base"


def _write_result(payload: dict[str, Any]) -> None:
    """Write the canonical smoke-result JSON. Overwrites any prior file."""
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, default=str)
        logger.info("Wrote %s", SMOKE_RESULTS_PATH)
    except OSError as exc:
        logger.error("Failed to write %s: %s", SMOKE_RESULTS_PATH, exc)


def _parse_job_payload() -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    """Read JOB_PAYLOAD env, return (job_spec, job_id, tier, full_payload)."""
    raw = os.environ.get("JOB_PAYLOAD", "")
    if not raw:
        raise RuntimeError("JOB_PAYLOAD env var is empty.")
    payload = json.loads(raw)
    return (
        payload.get("job_spec", {}),
        os.environ.get("JOB_ID", ""),
        os.environ.get("JOB_TIER", "minibinder"),
        payload,
    )


# ===========================================================================
# Upload helpers (mirror tools/boltz2/run_pipeline.py exactly)
# ===========================================================================


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


def _target_label(target_name: Optional[str], target_sequence: Optional[str]) -> str:
    if target_name:
        return target_name.upper()
    if target_sequence:
        return f"pasted target ({len(target_sequence)} aa)"
    return "target"


def _binder_label(binder_name: str) -> str:
    if binder_name == "minibinder":
        return "minibinder"
    # trastuzumab_framework_vhvl -> "Trastuzumab scFv"
    head = binder_name.replace("_framework_vhvl", "")
    return f"{head.title()} scFv"


def _extract_binder_sequence(designed_sequence: str) -> str:
    """The upstream concatenates ``target|binder`` with a ``|`` separator.

    Mirrors the cookbook's selector cell::

        df_result["binder_sequence"] = df_result.designed_sequence.str.split(r"\\|").str[1]
    """
    if "|" in designed_sequence:
        return designed_sequence.split("|", 1)[1]
    return designed_sequence


def _isoelectric_point(seq: str) -> Optional[float]:
    try:
        from Bio.SeqUtils.ProtParam import ProteinAnalysis
        return float(ProteinAnalysis(seq).isoelectric_point())
    except Exception as exc:
        logger.warning("Failed to compute pI for %s aa sequence: %s", len(seq), exc)
        return None


def _classify(
    is_antibody: bool,
    iptm: Optional[float],
    distogram_iptm_proxy: Optional[float],
    cdr_distogram_iptm_proxy: Optional[float],
    pi: Optional[float],
) -> str:
    """Return ``strict_pass`` | ``borderline`` | ``drop``."""
    if is_antibody:
        proxy = cdr_distogram_iptm_proxy
        if proxy is not None and proxy >= STRICT_CDR_IPTM_PROXY:
            return "strict_pass"
        if proxy is not None and proxy >= STRICT_CDR_IPTM_PROXY - 0.1:
            return "borderline"
        return "drop"
    # minibinder mode. pI is a hard gate: an undisplayable scaffold is a
    # drop regardless of iPTM, so check pI before the iPTM bands.
    if iptm is None:
        return "drop"
    pi_ok = pi is not None and pi < STRICT_PI
    if not pi_ok:
        return "drop"
    if iptm >= STRICT_IPTM:
        return "strict_pass"
    if iptm >= STRICT_IPTM - 0.05:
        return "borderline"
    return "drop"


def _save_complex_pdb(
    complex_obj: Any,
    name: str,
    upload_endpoint: str = "",
    job_token: str = "",
) -> Optional[str]:
    """Write a ProteinComplex out as a PDB file under PDB_OUTPUT_DIR and,
    if ``upload_endpoint`` is set, also stream the PDB bytes to the hub
    via a presigned PUT URL (mirrors tools/boltz2/run_pipeline.py).

    Returns the relative pdb_key for the smoke-results manifest. The key
    matches the Storage path the hub serves at
    ``/api/jobs/<job_id>/pdb/<pdb_key>``.
    """
    if complex_obj is None:
        return None
    PDB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    key = f"{name}_complex.pdb"
    path = PDB_OUTPUT_DIR / key
    try:
        pdb_text = complex_obj.to_pdb_string()
        path.write_text(pdb_text)
    except Exception as exc:
        logger.warning("Failed to write %s: %s", path, exc)
        return None

    if upload_endpoint:
        try:
            urls = request_upload_urls(upload_endpoint, job_token, [key])
            upload_pdb(urls[key], pdb_text.encode("utf-8"))
            logger.info("Uploaded %s to hub via presigned URL", key)
        except Exception as exc:
            logger.warning(
                "Upload of %s failed (%s) — inline /tmp copy preserved", key, exc,
            )

    return key


def _shape_designs(
    critic_results: list[dict],
    is_antibody: bool,
    upload_endpoint: str = "",
    job_token: str = "",
) -> list[dict]:
    """Collapse the per-critic results into one row per design.

    The upstream critic_results is a list of dicts with keys including
    critic_name, iptm, distogram_iptm_proxy, cdr_distogram_iptm_proxy,
    final_loss, designed_sequence, complex. Multiple critics emit rows
    for the same design — we group on designed_sequence and pull the
    real iPTM from the Cutoff2025 critic, the proxy from any Fast-base
    critic.
    """
    by_sequence: dict[str, dict] = {}
    for row in critic_results:
        seq = row.get("designed_sequence")
        if seq is None:
            continue
        bucket = by_sequence.setdefault(
            seq,
            {
                "designed_sequence": seq,
                "iptm": None,
                "distogram_iptm_proxy": None,
                "cdr_distogram_iptm_proxy": None,
                "final_loss": None,
                "complex": None,
            },
        )
        critic_name = str(row.get("critic_name", ""))
        if critic_name == CRITIC_REAL_IPTM:
            bucket["iptm"] = row.get("iptm")
            if bucket["complex"] is None:
                bucket["complex"] = row.get("complex")
            if bucket["final_loss"] is None:
                bucket["final_loss"] = row.get("final_loss")
        elif CRITIC_SCALING_PROXY in critic_name:
            if is_antibody:
                bucket["cdr_distogram_iptm_proxy"] = row.get(
                    "cdr_distogram_iptm_proxy"
                )
            else:
                bucket["distogram_iptm_proxy"] = row.get("distogram_iptm_proxy")

    designs: list[dict] = []
    for rank, (seq, bucket) in enumerate(by_sequence.items()):
        name = f"design_{rank}"
        binder_seq = _extract_binder_sequence(seq)
        pi = None if is_antibody else _isoelectric_point(binder_seq)
        pdb_key = _save_complex_pdb(
            bucket["complex"], name, upload_endpoint, job_token,
        )
        filter_status = _classify(
            is_antibody,
            bucket["iptm"],
            bucket["distogram_iptm_proxy"],
            bucket["cdr_distogram_iptm_proxy"],
            pi,
        )
        # Build a single ``scores`` dict so the generic /jobs/<id>/export.csv
        # exporter in app.py picks up every numeric/categorical column. The
        # flat copies are kept because the results template and the strict-
        # pass classifier in this file read them by short name.
        # ``sequence`` (binder only) is what /jobs/<id>/export.fasta reads;
        # ``designed_sequence`` (target|binder) is the UI's primary field.
        scores = {
            "iptm": bucket["iptm"],
            "distogram_iptm_proxy": bucket["distogram_iptm_proxy"],
            "cdr_distogram_iptm_proxy": bucket["cdr_distogram_iptm_proxy"],
            "final_loss": bucket["final_loss"],
            "isoelectric_point": pi,
            "filter_status": filter_status,
        }
        designs.append(
            {
                "rank": rank,
                "name": name,
                "pdb_key": pdb_key,
                "designed_sequence": seq,
                "sequence": binder_seq,
                "scores": scores,
                **scores,
            }
        )

    # Sort by iPTM desc with None at the bottom.
    designs.sort(key=lambda d: (-1 if d["iptm"] is None else -d["iptm"]))
    for rank, d in enumerate(designs):
        d["rank"] = rank
        d["name"] = f"design_{rank}"
    return designs


def main() -> int:
    start = time.time()
    try:
        job_spec, job_id, tier, payload = _parse_job_payload()
    except Exception as exc:
        logger.error("Failed to parse job payload: %s", exc)
        _write_result(
            {
                "status": "FAILED",
                "tier": "",
                "error": f"Failed to parse JOB_PAYLOAD: {exc}",
                "designs_total": 0,
                "designs_completed": 0,
                "n_failures": 1,
                "designs": [],
                "runtime_seconds": int(time.time() - start),
                "provider_job_id": os.environ.get("JOB_ID", ""),
            }
        )
        return 1

    upload_endpoint = payload.get("upload_urls_endpoint", "")
    job_token = payload.get("job_token", "") or os.environ.get("JOB_TOKEN", "")
    if not upload_endpoint:
        logger.warning(
            "upload_urls_endpoint missing from payload — per-design PDBs "
            "will only land in the inline smoke_result, not the hub Storage. "
            "Pilot tier requires the web flow to populate this."
        )

    preset = job_spec.get("preset", "minibinder")
    target_name = job_spec.get("target_name")
    target_sequence = job_spec.get("target_sequence")
    binder_name = job_spec.get("binder_name", "minibinder")
    is_antibody = bool(job_spec.get("is_antibody", preset == "scfv"))
    seed = int(job_spec.get("seed", 0))
    batch_size = int(job_spec.get("batch_size", 1))
    use_scaling_critics = bool(job_spec.get("use_scaling_critics", False))

    logger.info(
        "ESMFold2 design start: job=%s tier=%s preset=%s target=%s "
        "binder=%s seed=%d batch_size=%d scaling=%s",
        job_id,
        tier,
        preset,
        target_name or "(pasted)",
        binder_name,
        seed,
        batch_size,
        use_scaling_critics,
    )

    # Import the upstream module. /opt is on sys.path via the top of
    # this file; binder_design was copied in by the Dockerfile.
    try:
        import binder_design as bd  # type: ignore
    except Exception as exc:
        logger.error("Failed to import binder_design: %s\n%s", exc, traceback.format_exc())
        _write_result(
            {
                "status": "FAILED",
                "tier": tier,
                "error": f"binder_design import failed: {exc}",
                "designs_total": batch_size,
                "designs_completed": 0,
                "n_failures": 1,
                "designs": [],
                "runtime_seconds": int(time.time() - start),
                "provider_job_id": job_id,
            }
        )
        return 1

    # Direct (non-Modal-wrapped) instantiation. We are already inside the
    # Modal container, so we use ESMFold2Design rather than the
    # ESMFold2DesignModal wrapper class.
    try:
        designer = bd.ESMFold2Design()
        designer.load(use_scaling_critics)
    except Exception as exc:
        logger.error("Failed to load ESMFold2Design: %s\n%s", exc, traceback.format_exc())
        _write_result(
            {
                "status": "FAILED",
                "tier": tier,
                "error": f"ESMFold2Design load failed: {exc}",
                "designs_total": batch_size,
                "designs_completed": 0,
                "n_failures": 1,
                "designs": [],
                "runtime_seconds": int(time.time() - start),
                "provider_job_id": job_id,
            }
        )
        return 1

    try:
        best_seq, trajectory, critic_results = designer.design(
            target_name=target_name,
            target_sequence=target_sequence,
            binder_name=binder_name,
            binder_sequence=None,
            is_antibody=is_antibody,
            seed=seed,
            batch_size=batch_size,
        )
    except Exception as exc:
        logger.error("design() raised: %s\n%s", exc, traceback.format_exc())
        _write_result(
            {
                "status": "FAILED",
                "tier": tier,
                "preset": preset,
                "is_antibody": is_antibody,
                "target_name": target_name,
                "target_label": _target_label(target_name, target_sequence),
                "binder_name": binder_name,
                "binder_label": _binder_label(binder_name),
                "error": f"design() raised: {exc}",
                "designs_total": batch_size,
                "designs_completed": 0,
                "n_failures": 1,
                "designs": [],
                "use_scaling_critics": use_scaling_critics,
                "runtime_seconds": int(time.time() - start),
                "provider_job_id": job_id,
            }
        )
        return 1

    designs = _shape_designs(
        critic_results or [], is_antibody, upload_endpoint, job_token,
    )
    runtime = int(time.time() - start)

    summary = {
        "status": "COMPLETED",
        "tier": tier,
        "preset": preset,
        "is_antibody": is_antibody,
        "target_name": target_name,
        "target_label": _target_label(target_name, target_sequence),
        "binder_name": binder_name,
        "binder_label": _binder_label(binder_name),
        "designs_total": batch_size,
        "designs_completed": len(designs),
        "n_failures": max(0, batch_size - len(designs)),
        "use_scaling_critics": use_scaling_critics,
        "trajectory_steps": len(trajectory) if trajectory is not None else None,
        "best_sequence": _extract_binder_sequence(best_seq) if best_seq else None,
        "designs": designs,
        "runtime_seconds": runtime,
        "provider_job_id": job_id,
    }
    _write_result(summary)
    logger.info(
        "ESMFold2 design complete: %d/%d designs in %ds (%s strict_pass)",
        len(designs),
        batch_size,
        runtime,
        sum(1 for d in designs if d.get("filter_status") == "strict_pass"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
