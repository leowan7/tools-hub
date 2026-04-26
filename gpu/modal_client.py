"""Modal client — submit + poll Kendrew GPU pipeline functions.

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
        result           : dict | None    inline Kendrew smoke_result payload
        gpu_seconds_used : int | None
        error            : str | None

Behaviour
---------
Submit calls ``modal.Function.from_name("kendrew-<tool>-prod",
"run_tool").spawn(payload)`` with the Kendrew webhook-roundtrip payload
shape. For smoke and mini_pilot tiers the Modal function returns results
inline via a ``smoke_result`` key, so tools-hub can poll the FunctionCall
rather than wait for the webhook. For pilot and full tiers the Modal
function POSTs to ``webhook_url`` — poll() still reports "running" but
the webhook handler updates tool_jobs independently.

Poll uses a non-blocking ``FunctionCall.get(timeout=0)``. TimeoutError
means "still running"; anything else propagates as an error dict.

Offline degradation
-------------------
When the ``modal`` package is not importable (local dev without the
Kendrew environment), submit returns a stub FunctionCall id and poll
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
    # lives at ``ranomics-mpnn-prod`` (see ``APP_NAME_OVERRIDES`` below).
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
    # D3 ColabFold: slug "colabfold" → ``ranomics-colabfold-prod``.
    # Smoke fits in ~120 s post-JIT (first run includes ~3 min JAX
    # compile on a cold container). Standalone budgets 420 s — no-MSA
    # ColabFold on <=600 aa completes in 1-2 min once cached.
    ("colabfold", "smoke"):        120,
    ("colabfold", "standalone"):   420,
    ("colabfold", "fast"):         720,  # legacy alias — pre-D3 planning
    # D4 ESMFold: slug "esmfold" → ``ranomics-esmfold-prod``. Smoke
    # folds the baked 76 aa ubiquitin fixture on ESMFold-3B in ~30 s
    # once warm (~60-90 s cold including model load). Standalone caps
    # at 360 s for monomers up to 400 aa.
    ("esmfold", "smoke"):          90,
    ("esmfold", "standalone"):     360,
    ("esmfold", "fast"):           360,   # legacy alias — pre-D4 planning
    ("af2_ig", "standard"):        720,
    # Composite pipelines — smoke tier (inline return, small preset).
    ("bindcraft", "smoke"):         600,    # ~5-10 min on A100-80GB
    ("bindcraft", "mini_pilot"):    1800,
    ("bindcraft", "pilot"):         7200,
    ("bindcraft", "full"):          14400,
    ("rfantibody", "smoke"):        600,
    ("rfantibody", "mini_pilot"):   900,
    ("rfantibody", "pilot"):        1800,
    ("rfantibody", "full"):         3600,
    ("boltzgen", "smoke"):          900,
    ("boltzgen", "mini_pilot"):     1200,
    ("boltzgen", "pilot"):          3600,
    ("boltzgen", "full"):           7200,
    ("pxdesign", "smoke"):          1800,
    ("pxdesign", "mini_pilot"):     5400,   # observed 35-40 min; 1800 was tight against outer timeout
    ("pxdesign", "pilot"):          3600,
    ("rfdiffusion", "smoke"):       600,
    ("rfdiffusion", "mini_pilot"):  1800,
    ("rfdiffusion", "pilot"):       1800,
    ("rfdiffusion", "full"):        3600,
    # Wave-0 plumbing fixture.
    ("example-gpu", "smoke"):       60,
}


def preset_gpu_seconds(tool: str, preset: str) -> int:
    """Return the GPU-seconds cap for a (tool, preset) pair, or 0 if unknown."""
    return PRESET_CAPS.get((tool, preset), 0)


# ---------------------------------------------------------------------------
# Modal app-name overrides
# ---------------------------------------------------------------------------
# Default is ``kendrew-<tool>-prod`` (composite pipelines — BindCraft,
# BoltzGen, RFantibody, PXDesign) because those apps live in the Kendrew
# Modal project. Atomic primitives (D1..D9 per ATOMIC-TOOLS.md) deploy
# under the ``ranomics-<tool>-prod`` namespace because they are
# standalone. Keep this table tiny — one row per atomic tool.

APP_NAME_OVERRIDES: Dict[str, str] = {
    "mpnn":      "ranomics-mpnn-prod",
    "af2":       "ranomics-af2-prod",
    "colabfold": "ranomics-colabfold-prod",
    "esmfold":   "ranomics-esmfold-prod",
}


def modal_app_name(tool: str) -> str:
    """Return the Modal app name to resolve for a given tool slug."""
    return APP_NAME_OVERRIDES.get(tool, f"kendrew-{tool}-prod")


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
        """Submit a GPU job to the Kendrew Modal app for ``tool``.

        ``inputs`` is the tool-specific payload (e.g. target_chain,
        parameters) that maps onto the Kendrew ``job_spec`` shape. The
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
            (the inline Kendrew return dict when succeeded) and
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

        # Kendrew apps return a dict with "smoke_result" (inline payload on
        # smoke/mini_pilot tiers) + "exit_code" + "provider_job_id".
        return _interpret_kendrew_return(raw_result)

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

        Mirrors the webhook-roundtrip shape Kendrew's run_pipeline.py
        expects. Keys not used by a given tier are simply ignored on the
        Kendrew side, so one shape fits all presets.
        """
        return {
            "job_id": job_id,
            "job_token": job_token,
            "job_tier": preset,
            "tier": preset,
            "job_spec": inputs,
            "webhook_url": webhook_url,
            "input_presigned_url": inputs.get("_input_presigned_url", ""),
            "input_pdb_url": inputs.get("_input_pdb_url", ""),
            "upload_urls_endpoint": inputs.get("_upload_urls_endpoint", ""),
            "total_budget_hours": inputs.get("_total_budget_hours", 4),
        }


def _import_modal():
    """Import the ``modal`` package lazily; return None if unavailable."""
    try:
        import modal  # noqa: PLC0415
        return modal
    except Exception:
        return None


def _interpret_kendrew_return(raw_result: Any) -> Dict[str, Any]:
    """Translate a Kendrew Modal function return into poll() shape.

    Kendrew apps return::
        {
            "exit_code": int,
            "smoke_result": dict | None,
            "provider_job_id": str,
            ...
        }

    When ``smoke_result`` is present and carries ``status=="COMPLETED"``
    we succeed; when it carries ``status=="FAILED"`` we fail with the
    ``error`` bucket. When ``smoke_result`` is None we treat exit_code
    == 0 as succeeded with an empty result payload (pipeline used the
    webhook path; the actual result lands via the Modal callback
    webhook on tools-hub).
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
            return {
                "status": "succeeded",
                "result": smoke,
                "gpu_seconds_used": smoke.get("runtime_seconds"),
                "error": None,
            }
        if status_raw == "FAILED":
            return {
                "status": "failed",
                "result": None,
                "gpu_seconds_used": smoke.get("runtime_seconds"),
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

    # smoke_result missing: pipeline must be using the webhook path.
    if exit_code == 0:
        return {
            "status": "running",
            "result": None,
            "gpu_seconds_used": None,
            "error": None,
        }
    return {
        "status": "failed",
        "result": None,
        "gpu_seconds_used": None,
        "error": f"run_pipeline exited {exit_code} with no smoke_result",
    }


def _stringify_error(err: Any) -> str:
    """Best-effort flattening of the Kendrew error dict into a string."""
    if isinstance(err, dict):
        bucket = err.get("bucket", "unknown")
        check = err.get("check", "")
        detail = err.get("detail", "")
        return f"{bucket}:{check} — {detail}" if check else f"{bucket} — {detail}"
    return str(err) if err else ""
