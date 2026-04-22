"""Modal client contract — the interface every tool stream depends on.

The concrete implementation calls Modal's ``Function.spawn`` against the
per-tool apps (``kendrew-bindcraft-prod`` etc.). For Wave-0 we ship a stub
that returns a deterministic fake FunctionCall id and raises
``NotImplementedError`` from ``.poll()`` — enough for streams B/C/D to
wire their forms, templates, and webhook plumbing without depending on
Modal being reachable.

Contract (frozen for downstream streams):

    ModalClient.submit(
        tool: str,
        preset: str,
        inputs: dict,
    ) -> dict with keys:
        - function_call_id : str    Modal FunctionCall id
        - gpu_seconds_cap  : int    upper bound on billable GPU seconds

    ModalClient.poll(function_call_id: str) -> dict with keys (real impl):
        - status           : Literal["pending", "running", "success", "error", "timeout"]
        - result           : dict | None
        - gpu_seconds_used : int | None
        - error            : str | None

The GPU-seconds cap comes from the preset registry so the credits layer
can reserve the correct amount up-front. Do NOT change these keys without
bumping the contract version and coordinating via ORCH-LOG.md.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Dict

logger = logging.getLogger(__name__)


CONTRACT_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Preset registry
# ---------------------------------------------------------------------------
#
# GPU seconds caps per (tool, preset). Values are *upper bounds* used for
# credit pre-authorisation; the actual billed seconds come from Modal and
# drive any prorated refund. Numbers derive from PRODUCT-PLAN.md §Pricing.

PRESET_CAPS: Dict[tuple[str, str], int] = {
    # Atomic primitives.
    ("proteinmpnn", "standalone"): 360,
    ("colabfold", "fast"):         720,
    ("af2", "standard"):           720,
    ("esmfold", "fast"):           360,
    ("af2_ig", "standard"):        720,
    # Composite pipelines.
    ("rfdiffusion", "pilot"):      1800,
    ("rfdiffusion", "full"):       3600,
    ("rfantibody", "pilot"):       1800,
    ("rfantibody", "full"):        3600,
    ("boltzgen", "pilot"):         3600,
    ("boltzgen", "full"):          7200,
    ("bindcraft", "pilot"):        7200,
    ("bindcraft", "full"):         14400,
    ("pxdesign", "pilot"):         3600,
    # Wave-0 plumbing fixture.
    ("example-gpu", "smoke"):      60,
}


def preset_gpu_seconds(tool: str, preset: str) -> int:
    """Return the GPU-seconds cap for a (tool, preset) pair, or 0 if unknown."""
    return PRESET_CAPS.get((tool, preset), 0)


# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmitResult:
    """Return shape of ``ModalClient.submit``.

    Downstream code should treat this as a read-only record. Callers that
    want a plain dict can use ``dataclasses.asdict`` or ``to_dict()``.
    """

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

    Wave-0 implementation is a stub — it fabricates a FunctionCall id and
    leaves ``.poll()`` unimplemented. Stream E / Orchestrator will replace
    the body of ``submit`` and ``poll`` once the Modal staging environment
    is reachable from Railway.
    """

    def __init__(self, environment: str | None = None) -> None:
        """Create a client bound to a Modal environment ('staging'/'main')."""
        self.environment = environment or os.environ.get(
            "GPU_ENVIRONMENT", "staging"
        )

    def submit(
        self,
        tool: str,
        preset: str,
        inputs: dict[str, Any],
    ) -> Dict[str, Any]:
        """Submit a job to the Modal app for ``tool`` with ``preset``.

        Returns a dict (not the dataclass) so downstream consumers don't
        need to import from ``gpu.modal_client``.

        Raises:
            ValueError: if the (tool, preset) pair is not in the registry.
        """
        cap = preset_gpu_seconds(tool, preset)
        if cap == 0:
            raise ValueError(
                f"Unknown (tool, preset) = ({tool!r}, {preset!r}). "
                "Add an entry to PRESET_CAPS before submitting."
            )

        # Wave-0 stub: fake FunctionCall id. Real implementation will call
        # ``modal.Function.lookup(f'kendrew-{tool}-{env}', ...).spawn(...)``.
        fake_id = f"fc-stub-{tool}-{preset}-{secrets.token_hex(6)}"
        logger.info(
            "ModalClient.submit stub: tool=%s preset=%s env=%s id=%s "
            "inputs_keys=%s",
            tool,
            preset,
            self.environment,
            fake_id,
            sorted(inputs.keys()),
        )
        return SubmitResult(
            function_call_id=fake_id, gpu_seconds_cap=cap
        ).to_dict()

    def poll(self, function_call_id: str) -> Dict[str, Any]:
        """Poll a FunctionCall for status + result.

        Stream E will land the concrete implementation. Keeping this as
        ``NotImplementedError`` keeps downstream streams honest — they
        must not ship a tool that depends on polling until the real
        client lands.
        """
        raise NotImplementedError(
            "ModalClient.poll is not implemented yet. This is the Wave-0 "
            "stub. Stream E owns the concrete implementation."
        )
