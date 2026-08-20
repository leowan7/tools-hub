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

Every Modal gRPC round trip runs inside ``_bounded_modal_call``, because the
SDK applies no deadline of its own and these are called from request handlers.
Each of submit / poll / cancel makes exactly ONE bounded call covering both of
its hops, so a method's worst case is one budget rather than two. See
``_MODAL_CALL_TIMEOUT_SEC`` for the number and for why stacking is not safe.

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
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, TypeVar

from contracts.rpc import ToolPayload

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Wall-clock ceiling on any single Modal gRPC round trip made from a request
# handler.
#
# Modal's client sets NO deadline of its own. ``Retry()`` defaults both
# ``attempt_timeout`` and ``total_timeout`` to None (modal 1.4.2,
# ``_utils/grpc_utils.py``), ``_grpc_client.py`` applies that as the default
# retry policy, and the timeout the RPC finally receives is therefore
# ``None``. grpclib's keepalive is off by default and the channel options set
# only HTTP/2 window sizes, so a half-open connection is never detected
# either. Only the initial handshake is bounded. A stale channel means the
# handshake succeeds, the request is written, and the response never arrives:
# the call blocks forever.
#
# This repo has already had that outage — see the note in
# ``shared/supabase_client.py`` for 2026-06-10, same cause (Railway egress,
# idle HTTP/2 goes stale). What saved it was gunicorn's sync worker being
# killed at ``timeout``, which is a process-level backstop, not a fix: it
# takes out every other in-flight request on that worker too. Bound the call
# instead, so the failure costs one request rather than one worker.
#
# Why 90 s, and not the "generous" 30 s this started at.
#
# 30 s was set from how long a healthy control-plane call takes (well under a
# second) rather than from how long the SDK is entitled to take, and it lands
# BELOW Modal's own retry budget. In modal 1.4.2,
# ``_utils/grpc_utils.py:231``:
#
#     @retry(n_attempts=18, base_delay=0.1, attempt_timeout=10.0,
#            max_delay=5.0, total_timeout=63.0)
#     async def connect_channel(channel): ...
#
# Establishing the channel is allowed **63 s** across 18 attempts, by design,
# for exactly the transient blips this deadline exists to survive. Any of
# these calls can be the one that triggers a connect. Capping below 63 s turns
# a blip the SDK would have ridden out into a hard failure — on ``submit``
# that means a released wallet hold and an error the user did not need to see.
#
# So the cap is bracketed on both sides:
#
#     63 s  <  _MODAL_CALL_TIMEOUT_SEC  <  gunicorn `timeout` (120 s default)
#
# Above 63 s so we never preempt the SDK's own retry; below the worker
# watchdog so a wedged channel costs one request rather than the worker and
# every other request on it. 90 s sits in the middle with 30 s of headroom
# under the watchdog for the rest of the request.
#
# That bracket is why each public method makes exactly ONE bounded call.
# ``submit`` used to bound ``from_name`` and ``spawn`` separately, which at
# 90 s each would be a 180 s worst case — straight through the watchdog — and
# which also opened a real orphan window: ``from_name`` inside its budget,
# ``spawn`` cut at its own, and a ``spawn`` that lands a second after we gave
# up is a **billed GPU job with no job row tracking it**. One call, one
# budget, per method.
#
# Coupling to record: ``gunicorn.conf.py:164`` floors the watchdog at 60 s
# (`max(60, GUNICORN_TIMEOUT)`). Setting GUNICORN_TIMEOUT below 90 puts the
# watchdog back underneath this cap and restores the old take-out-the-worker
# behaviour. Do not lower it without lowering this too.
_MODAL_CALL_TIMEOUT_SEC = 90.0


class ModalCallTimeout(TimeoutError):
    """A Modal gRPC call exceeded ``_MODAL_CALL_TIMEOUT_SEC``."""


def _bounded_modal_call(what: str, fn: Callable[[], _T]) -> _T:
    """Run ``fn`` with a hard wall-clock cap and surface a timeout as an error.

    The Modal SDK exposes no per-call deadline (see
    ``_MODAL_CALL_TIMEOUT_SEC``), so the cap has to come from outside the
    call. Same shape as ``shared.webhooks._resolve_addrinfo_bounded``, and
    for the same reason: a daemon worker joined for a bounded time, with no
    process-wide state touched.

    ponytail: on timeout the worker thread is orphaned and stays blocked on
    the dead channel for the life of the process — one thread per timed-out
    call, which is the price of Python having no way to interrupt a blocking
    C-level read. That is strictly better than the caller blocking forever,
    and it is bounded by how often Modal can time out. Revisit only if Modal
    ever exposes a real deadline.
    """
    box: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller
            box["error"] = exc

    worker = threading.Thread(
        target=_worker, name=f"modal-{what}", daemon=True
    )
    worker.start()
    worker.join(_MODAL_CALL_TIMEOUT_SEC)
    if worker.is_alive():
        raise ModalCallTimeout(
            f"Modal {what} did not return within {_MODAL_CALL_TIMEOUT_SEC:g}s"
        )
    if "error" in box:
        raise box["error"]
    return box["value"]


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
    # Proteina-Complexa de novo binder search on A100-80GB, run as a
    # fund-and-drain campaign of one-shard-per-container jobs. The preset IS
    # the model variant (not a "pilot"/"full" tier): each design variant is
    # capped at the 7200 s (2 h) container that _MAX_SESSION_S enforces, the
    # physical bound on a single shard's spend (~$12.6 marked-up at A100-80GB).
    # BOOTSTRAP until the P4/P5 canaries measure real per-shard wall-clock;
    # historical p90 supersedes at >=20 runs. `validate` is the free CPU-only
    # complexa-validate pre-flight gate (no GPU); its cap is nominal.
    ("proteina", "protein_binder"): 7200,
    ("proteina", "ligand_binder"):  7200,
    ("proteina", "motif_ame"):      7200,
    ("proteina", "validate"):        900,
    # OpenDDE all-atom co-folding on H100. Atomic tool: one container per job,
    # both checkpoints share the same architecture so the cap is size/sampler
    # driven, not preset driven. 3600 s matches tools/opendde/modal_app.py
    # _MAX_SESSION_S — the physical bound on a single job's spend (~$14.79
    # marked-up at H100). BOOTSTRAP until the O-1/O-2 canaries measure real
    # per-prediction wall-clock; historical p90 supersedes at >=20 runs.
    ("opendde", "general"):         3600,
    ("opendde", "abag"):            3600,
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

        def _lookup_and_spawn() -> Any:
            # ONE bounded call, not two. Two budgets would stack past the
            # gunicorn watchdog, and a `spawn` cut on its own deadline can
            # still land — a billed GPU job with no job row. See
            # ``_MODAL_CALL_TIMEOUT_SEC``.
            fn = modal.Function.from_name(
                modal_app_name(tool),
                "run_tool",
                environment_name=self.environment,
            )
            return fn.spawn(payload)

        try:
            function_call = _bounded_modal_call("submit", _lookup_and_spawn)
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

        def _fetch() -> Any:
            # Non-blocking poll. timeout=0 raises TimeoutError when the
            # function has not yet returned. One bounded call covers both
            # hops — see ``_MODAL_CALL_TIMEOUT_SEC``.
            fc = modal.FunctionCall.from_id(function_call_id)
            return fc.get(timeout=0)

        try:
            try:
                raw_result = _bounded_modal_call("poll", _fetch)
            except ModalCallTimeout:
                # Our own wall-clock cap on a wedged channel, NOT Modal's
                # "not finished yet" signal. Both leave the job row alone and
                # both are retried by the next poll, but only one of them is
                # visible in the logs — so do not let it fall into the
                # `except TimeoutError` below and be reported as healthy.
                raise
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

        def _lookup_and_cancel() -> None:
            # One bounded call — see ``_MODAL_CALL_TIMEOUT_SEC``.
            modal.FunctionCall.from_id(function_call_id).cancel()

        try:
            _bounded_modal_call("cancel", _lookup_and_cancel)
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
