"""Modal client — submit + poll external Modal app GPU pipeline functions.

The contract is the interface every tool package depends on. Wave-0 was
a stub; this is the real implementation for Wave-2 launch (Stream C).

Contract (frozen; bump CONTRACT_VERSION for breaking changes and log it
in ORCH-LOG.md):

    ModalClient.submit(tool, preset, inputs, *, job_id, job_token, webhook_url)
        -> dict:
            function_call_id : str    Modal FunctionCall id
            gpu_seconds_cap  : int    upper bound on billable GPU seconds

    ModalClient.poll(function_call_id) -> dict:
        status           : Literal["pending", "running", "succeeded",
                                   "failed", "timeout", "error"]
        result           : dict | None    inline GPU pipeline smoke_result payload
        gpu_seconds_used : int | None
        error            : str | None

Behaviour
---------
Submit calls ``modal.Function.from_name("ranomics-<tool>-prod",
"run_tool").spawn(payload)`` with the GPU pipeline webhook-roundtrip payload
shape. Atomic tools return results inline via a ``smoke_result`` key, so
tools-hub can poll the FunctionCall rather than wait for the webhook.
For pilot and full tiers the Modal
function POSTs to ``webhook_url`` — poll() still reports "running" but
the webhook handler updates tool_jobs independently.

Poll uses a non-blocking ``FunctionCall.get(timeout=0)``. TimeoutError
means "still running"; anything else propagates as an error dict.

Offline degradation
-------------------
When the ``modal`` package is not importable (local dev without the
external Modal environment), submit returns a stub FunctionCall id and poll
returns a deterministic "running" forever. This matches the Wave-0
behaviour so unit tests and contributors without Modal access still
work.

Environment
-----------
    GPU_ENVIRONMENT (optional) — Modal environment name. Defaults to
        "main" in production. Set to "staging" for a staging pool.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional

from contracts.rpc import ToolPayload

logger = logging.getLogger(__name__)


CONTRACT_VERSION = "2.0.0"


# ---------------------------------------------------------------------------
# Preset registry
# ---------------------------------------------------------------------------
# GPU-seconds caps per (tool, preset). Values are upper bounds used for
# credit pre-authorisation; the actual billed seconds come from Modal and
# drive any prorated refund. Numbers derived from
# docs/VALIDATION-LOG.md real observations + docs/PRODUCT-PLAN.md pricing.

PRESET_CAPS: Dict[tuple[str, str], int] = {
    # Atomic primitives.
    # D1 MPNN: slug "mpnn" matches the tools-hub adapter; the Modal app
    # is named ``ranomics-mpnn-prod`` per the ``ranomics-<slug>-prod``
    # convention in ``modal_app_name``.
    ("mpnn", "smoke"):             120,
    ("mpnn", "standalone"):        360,
    ("proteinmpnn", "standalone"): 360,  # legacy alias — pre-D1 planning
    # D2 AF2 standalone: slug "af2" → ``ranomics-af2-prod``. Smoke runs
    # the baked BPTI fixture at 1 recycle / no MSA (~1-2 min cold,
    # <30 s warm). Standalone runs user FASTA with MSA + 3 recycles,
    # capped at 1500 AA total on the atomic tier; real runs observed
    # in the 5-10 min window, cap at 20 min to match the Modal
    # timeout.
    ("af2", "smoke"):              180,
    ("af2", "standalone"):         1200,
    ("af2", "standard"):           720,   # legacy alias — pre-D2 planning
    # AF2 batch: sequential per-fold inside one warm A100-80GB container.
    # Cap matches tools/af2/modal_app.py:_MAX_SESSION_S (14400 s = 4 h)
    # which covers a 50-record run at ~5 min/fold warm with cold-start
    # headroom on the first fold.
    ("af2", "batch"):              14400,
    # D3 ColabFold: slug "colabfold" → ``ranomics-colabfold-prod``.
    # Smoke fits in ~120 s post-JIT (first run includes ~3 min JAX
    # compile on a cold container). Standalone budgets 420 s — no-MSA
    # ColabFold on <=600 aa completes in 1-2 min once cached.
    ("colabfold", "smoke"):        120,
    ("colabfold", "standalone"):   420,
    ("colabfold", "fast"):         720,  # legacy alias — pre-D3 planning
    # ColabFold batch: sequential per-fold inside one warm A100-40GB
    # container. Cap matches tools/colabfold/modal_app.py:_MAX_SESSION_S
    # (14400 s = 4 h) — supports up to 200 records at ~1-2 min/fold warm
    # with cold-start headroom on the first fold.
    ("colabfold", "batch"):        14400,
    # D4 ESMFold: slug "esmfold" → ``ranomics-esmfold-prod``. Smoke
    # folds the baked 76 aa ubiquitin fixture on ESMFold-3B in ~30 s
    # once warm (~60-90 s cold including model load). Standalone caps
    # at 360 s for monomers up to 400 aa.
    ("esmfold", "smoke"):          90,
    ("esmfold", "standalone"):     360,
    ("esmfold", "fast"):           360,   # legacy alias — pre-D4 planning
    # ESMFold batch: sequential per-fold inside one warm A100-40GB
    # container. Cap matches tools/esmfold/modal_app.py:_MAX_SESSION_S
    # (3600 s) so a 500-record run that hits the modal session ceiling
    # surfaces a clean timeout instead of silently truncating designs.
    ("esmfold", "batch"):          3600,
    # Boltz-2 cofold: ``standalone`` = single-sequence (~60 s/design); cap
    # at 1200 s covers a 10-binder run with weight-load headroom.
    # ``msa_server`` = --use_msa_server (~3 min/design including MSA
    # fetch); cap at 3600 s covers a 10-binder run including the
    # public-server tail latency. Modal hard timeout is 3600 s.
    ("boltz2", "standalone"):      1200,
    ("boltz2", "msa_server"):      3600,
    # ESMFold2-design: gradient-based inversion of ESMFold2 on H100.
    # ~150 steps per design at 4-6 s/step gives ~10-15 min per gradient
    # run. Cap at 2400 s (40 min) per preset — headroom over upstream's
    # 60-min Modal timeout, room for batch_size up to 6 and weight-load
    # latency on a cold container. Tune downward once we have real
    # observed wall-clock distributions from the first prod batches.
    ("esmfold2-design", "minibinder"): 2400,
    ("esmfold2-design", "scfv"):       2400,
    # IgGM antibody/nanobody design (diffusion) on A100-40GB. Canary-measured:
    # ~24 s per diffusion pass + ~35 s model load. Every preset is bounded to
    # MAX_TOTAL_PASSES=100 inference passes (tools.iggm), so the realistic max
    # wall-clock is ~35 + 100*24 ≈ 2435 s + heartbeat/upload overhead ≈ 2600 s.
    # affinity_maturation runs one pass PER masked position PER sample, so its
    # 100-pass ceiling is the num_samples*n_masked product (not raw samples).
    # Uniform 3000 s ceiling covers that with margin, under the 3570 s Modal
    # subprocess timeout. Historical p90 supersedes once >=20 runs land.
    ("iggm", "complex_prediction"):  3000,
    ("iggm", "cdr_design"):          3000,
    ("iggm", "fr_design"):           3000,
    ("iggm", "affinity_maturation"): 3000,
    ("iggm", "inverse_design"):      3000,
    # Composite pipelines. smoke + mini_pilot tiers were removed
    # 2026-05-29; pilot is the only user-facing tier and full is reserved
    # for AI Binder Sprint runs that go through the webhook flow.
    ("bindcraft", "pilot"):         7200,
    ("bindcraft", "full"):          14400,
    ("rfantibody", "pilot"):        1800,
    ("rfantibody", "full"):         3600,
    ("boltzgen", "pilot"):          3600,
    ("boltzgen", "full"):           7200,
    ("pxdesign", "pilot"):          3600,
    ("rfdiffusion", "pilot"):       1800,
    ("rfdiffusion", "full"):        3600,
}


def preset_gpu_seconds(tool: str, preset: str) -> int:
    """Return the GPU-seconds cap for a (tool, preset) pair, or 0 if unknown."""
    return PRESET_CAPS.get((tool, preset), 0)


# ---------------------------------------------------------------------------
# Modal app names
# ---------------------------------------------------------------------------
# All Modal apps follow the ``ranomics-<tool>-prod`` naming convention,
# including both composite pipelines (BindCraft, BoltzGen, RFantibody,
# PXDesign, RFdiffusion) and atomic primitives (D1..D9 per ATOMIC-TOOLS.md).


def modal_app_name(tool: str) -> str:
    """Return the Modal app name to resolve for a given tool slug."""
    return f"ranomics-{tool}-prod"


@dataclass(frozen=True)
class SubmitResult:
    """Return shape of ``ModalClient.submit``."""

    function_call_id: str
    gpu_seconds_cap: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "function_call_id": self.function_call_id,
            "gpu_seconds_cap": self.gpu_seconds_cap,
        }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ModalClient:
    """Thin abstraction over Modal ``Function.spawn`` + ``FunctionCall.get``.

    Degrades to a deterministic stub when the ``modal`` package is not
    importable, so local contributors and unit tests run offline.
    """

    def __init__(self, environment: Optional[str] = None) -> None:
        self.environment = environment or os.environ.get(
            "GPU_ENVIRONMENT", "main"
        )

    # -- submit -----------------------------------------------------------

    def submit(
        self,
        tool: str,
        preset: str,
        inputs: Dict[str, Any],
        *,
        job_id: str,
        job_token: str,
        webhook_url: str = "",
    ) -> Dict[str, Any]:
        """Submit a GPU job to the external Modal app for ``tool``.

        ``inputs`` is the tool-specific payload (e.g. target_chain,
        parameters) that maps onto the GPU pipeline ``job_spec`` shape. The
        caller is responsible for pre-uploading any large input files
        (PDB, FASTA, etc.) and passing a reachable URL — this client
        does not stage file uploads.

        Raises:
            ValueError: unknown (tool, preset) pair.
            RuntimeError: Modal call failed at submit time.
        """
        cap = preset_gpu_seconds(tool, preset)
        if cap == 0:
            raise ValueError(
                f"Unknown (tool, preset)=({tool!r}, {preset!r}). Add an "
                "entry to PRESET_CAPS before submitting."
            )

        payload = self._build_payload(
            tool=tool,
            preset=preset,
            inputs=inputs,
            job_id=job_id,
            job_token=job_token,
            webhook_url=webhook_url,
        )

        modal = _import_modal()
        if modal is None:
            # Offline stub — predictable FunctionCall id so poll() behaves.
            fake_id = f"fc-stub-{tool}-{preset}-{secrets.token_hex(6)}"
            logger.info(
                "ModalClient.submit offline stub: tool=%s preset=%s id=%s",
                tool,
                preset,
                fake_id,
            )
            return SubmitResult(
                function_call_id=fake_id, gpu_seconds_cap=cap
            ).to_dict()

        try:
            fn = modal.Function.from_name(
                modal_app_name(tool),
                "run_tool",
                environment_name=self.environment,
            )
            function_call = fn.spawn(payload)
            fc_id = getattr(function_call, "object_id", None) or str(function_call)
        except Exception as exc:  # pragma: no cover — exercised live only
            logger.exception("Modal submit failed for tool=%s", tool)
            raise RuntimeError(f"Modal submit failed: {exc}") from exc

        logger.info(
            "ModalClient.submit: tool=%s preset=%s env=%s fc_id=%s",
            tool,
            preset,
            self.environment,
            fc_id,
        )
        return SubmitResult(
            function_call_id=fc_id, gpu_seconds_cap=cap
        ).to_dict()

    # -- poll -------------------------------------------------------------

    def poll(self, function_call_id: str) -> Dict[str, Any]:
        """Poll a FunctionCall non-blockingly.

        Returns:
            dict with ``status`` in
            ``{"running","succeeded","failed","error"}``, plus ``result``
            (the inline GPU pipeline return dict when succeeded) and
            ``error`` (string on error).
        """
        if function_call_id.startswith("fc-stub-"):
            # Offline stub path — never advances.
            return {
                "status": "running",
                "result": None,
                "gpu_seconds_used": None,
                "error": None,
            }

        modal = _import_modal()
        if modal is None:
            return {
                "status": "error",
                "result": None,
                "gpu_seconds_used": None,
                "error": "modal package not available",
            }

        try:
            fc = modal.FunctionCall.from_id(function_call_id)
            try:
                # Non-blocking poll. timeout=0 raises TimeoutError when
                # the function has not yet returned.
                raw_result = fc.get(timeout=0)
            except TimeoutError:
                return {
                    "status": "running",
                    "result": None,
                    "gpu_seconds_used": None,
                    "error": None,
                }
        except Exception as exc:  # pragma: no cover — exercised live only
            logger.warning(
                "Modal poll failed for fc=%s", function_call_id, exc_info=True
            )
            return {
                "status": "error",
                "result": None,
                "gpu_seconds_used": None,
                "error": str(exc),
            }

        # GPU pipeline apps return a dict with "smoke_result" (inline payload on
        # smoke/mini_pilot tiers) + "exit_code" + "provider_job_id".
        return _interpret_pipeline_return(raw_result)

    # -- cancel -----------------------------------------------------------

    def cancel(self, function_call_id: str) -> Dict[str, Any]:
        """Best-effort cancel of a running FunctionCall.

        Returns a dict with ``ok`` (bool) and ``error`` (str | None).
        Offline stubs and missing-modal environments return ``ok=True``
        so tests and local dev do not block the tools-hub cancel flow;
        the authoritative state lives in the tool_jobs row regardless.
        """
        if function_call_id.startswith("fc-stub-"):
            return {"ok": True, "error": None}

        modal = _import_modal()
        if modal is None:
            return {"ok": True, "error": "modal package not available"}

        try:
            fc = modal.FunctionCall.from_id(function_call_id)
            fc.cancel()
        except Exception as exc:  # pragma: no cover — exercised live only
            logger.warning(
                "Modal cancel failed for fc=%s", function_call_id, exc_info=True
            )
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "error": None}

    # -- internals --------------------------------------------------------

    def _build_payload(
        self,
        *,
        tool: str,
        preset: str,
        inputs: Dict[str, Any],
        job_id: str,
        job_token: str,
        webhook_url: str,
    ) -> Dict[str, Any]:
        """Assemble the dict passed to ``run_tool.spawn``.

        Mirrors the webhook-roundtrip shape the GPU pipeline's run_pipeline.py
        expects. Keys not used by a given tier are simply ignored on the
        GPU pipeline side, so one shape fits all presets.

        The dict is constructed via ``ToolPayload`` from the shared
        contracts module so both sides validate against the same schema.
        """
        payload = ToolPayload(
            job_id=job_id,
            job_token=job_token,
            job_tier=preset,
            tier=preset,
            job_spec=inputs,
            webhook_url=webhook_url,
            input_presigned_url=inputs.get("_input_presigned_url", ""),
            input_pdb_url=inputs.get("_input_pdb_url", ""),
            upload_urls_endpoint=inputs.get("_upload_urls_endpoint", ""),
            total_budget_hours=inputs.get("_total_budget_hours", 4),
        )
        return payload.model_dump()


def _import_modal():
    """Import the ``modal`` package lazily; return None if unavailable."""
    try:
        import modal  # noqa: PLC0415
        return modal
    except Exception:
        return None


def _interpret_pipeline_return(raw_result: Any) -> Dict[str, Any]:
    """Translate an external Modal app function return into poll() shape.

    GPU pipeline apps return::
        {
            "exit_code": int,
            "smoke_result": dict | None,
            "provider_job_id": str,
            "webhook_outcome": dict | None,  # {"delivered": bool, "detail": str}
            ...
        }

    Modal calls are synchronous: by the time the FunctionCall returns,
    the entire subprocess (including post_webhook for the webhook tier)
    has finished. So ``smoke_result is None`` here means the pipeline
    took the webhook path AND the webhook was already attempted. If the
    webhook had succeeded the job would already be terminal via the
    ``/webhooks/modal`` handler, not via this poll. So smoke_result
    missing here is treated as a webhook-delivery failure.
    """
    if not isinstance(raw_result, dict):
        return {
            "status": "error",
            "result": None,
            "gpu_seconds_used": None,
            "error": f"unexpected Modal return type: {type(raw_result).__name__}",
        }

    exit_code = int(raw_result.get("exit_code") or 0)
    smoke = raw_result.get("smoke_result")

    if isinstance(smoke, dict):
        status_raw = str(smoke.get("status") or "").upper()
        if status_raw == "COMPLETED":
            # Two shapes land here:
            #   - Composite tools (mini_pilot legacy) nest results under
            #     ``smoke["output"]`` so the webhook path and the inline
            #     path agree on a shared ``payload["output"]`` contract.
            #   - Atomic tools (MPNN / AF2 / ColabFold / ESMFold /
            #     Boltz-2) emit a FLAT ``smoke_results.json`` whose
            #     domain keys (sequences, designs, ...) sit at the top
            #     level next to status / tier / runtime_seconds.
            # Unwrap accordingly so job.result always carries the
            # tool-specific keys without silently dropping any of them.
            raw_output = smoke.get("output")
            if isinstance(raw_output, dict):
                output = dict(raw_output)
                # Composite shape — merge tier + timing from the wrapper
                # so templates that read them off the top level still see
                # them when they live one level up.
                for key in ("tier", "gpu_seconds", "runtime_seconds"):
                    if key in smoke and key not in output:
                        output[key] = smoke[key]
            else:
                # Flat atomic-tool shape — take everything except
                # ``status`` (already used to branch above). Preserves
                # designs / sequences / antigen_length / contacted_residues
                # / ... without per-tool key allowlists.
                output = {k: v for k, v in smoke.items() if k != "status"}
            return {
                "status": "succeeded",
                "result": output,
                "gpu_seconds_used": (
                    smoke.get("runtime_seconds")
                    if smoke.get("runtime_seconds") is not None
                    else smoke.get("gpu_seconds")
                ),
                "exit_code": exit_code,
                "error": None,
            }
        if status_raw == "FAILED":
            return {
                "status": "failed",
                "result": None,
                "gpu_seconds_used": smoke.get("runtime_seconds"),
                "exit_code": exit_code,
                "error": _stringify_error(smoke.get("error")),
            }
        # Unknown status string — treat as error so we do not silently
        # succeed on a malformed result.
        return {
            "status": "error",
            "result": smoke,
            "gpu_seconds_used": None,
            "error": f"unexpected smoke_result.status: {status_raw!r}",
        }

    # smoke_result missing: pipeline used the webhook path. Modal is
    # synchronous, so the webhook was already attempted by the time we
    # see this return. If it had succeeded the job would already be
    # terminal — so missing-result here means webhook delivery failed.
    webhook_outcome = raw_result.get("webhook_outcome") or {}
    detail = webhook_outcome.get("detail") or "no webhook_outcome reported by pipeline"
    if exit_code == 0:
        # Pipeline process exited cleanly but tools-hub never confirmed the
        # terminal webhook. The run itself succeeded; the payload was
        # delivered out-of-band. ``exit_code`` lets the stuck-job recovery
        # path distinguish this (recoverable) case from a genuine crash.
        return {
            "status": "failed",
            "result": None,
            "gpu_seconds_used": None,
            "exit_code": 0,
            "error": f"webhook delivery failed (pipeline exited 0): {detail}",
        }
    return {
        "status": "failed",
        "result": None,
        "gpu_seconds_used": None,
        "exit_code": exit_code,
        "error": f"run_pipeline exited {exit_code} with no smoke_result; webhook detail: {detail}",
    }


def _stringify_error(err: Any) -> str:
    """Best-effort flattening of the GPU pipeline error dict into a string."""
    if isinstance(err, dict):
        bucket = err.get("bucket", "unknown")
        check = err.get("check", "")
        detail = err.get("detail", "")
        return f"{bucket}:{check} — {detail}" if check else f"{bucket} — {detail}"
    return str(err) if err else ""
