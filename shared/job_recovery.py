"""Recover a genuinely-succeeded job whose terminal webhook was lost.

A compute job normally reaches ``succeeded`` when Modal POSTs its
completion webhook to ``/webhooks/modal/<job_id>/<token>``. If that
webhook is PERMANENTLY lost (app restart mid-deploy, a transient 5xx, a
Supabase HTTP/2 read-hang) the job stays non-terminal until the stuck-job
sweeper (``cron/sweep_stuck_jobs.py``) times it out. Timing it out is
money-safe (the hold is released) but throws away a result the work
already produced: Modal may still hold the payload, and for composite
pilots the design structures are already in tool-outputs Storage.

This module lets the sweeper recover that result instead of discarding it.
``recover_stuck_job_result`` returns a finalizable result dict when the
work is provably present, or ``None`` when Modal genuinely has nothing
left to recover (a true stall -> the sweeper should time the job out).

Recovery only fires on POSITIVE evidence the run genuinely finished, so a
run that streamed a few designs and then FAILED (whose FAILED webhook was
also lost) is timed out and refunded, never billed as a success:

  1. **Modal inline result.** ``ModalClient.poll`` does a non-blocking
     ``FunctionCall.get(timeout=0)``. Atomic tools return their payload
     inline, so a lost webhook is fully recoverable here.

  2. **Pipeline exit code (ground truth).** Composite pilots take the
     webhook path and carry no inline payload, so ``poll`` reports
     ``failed`` for them even when the work succeeded. We do NOT trust that
     status; instead we read the pipeline PROCESS exit code the poll now
     surfaces: exit 0 means the run finished and only the callback was lost
     (recoverable); a nonzero exit means a genuine crash (refuse -- its
     partial Storage designs must not be resurrected as a success).

  3. **Storage reconstruction**, gated on completion evidence. Rebuilds
     ``result.candidates`` from the streamed ``inputs._partial_candidates``
     (keeping only designs that exist in Storage) or a direct Storage
     listing. Trusted only behind a clean pipeline exit OR a heartbeat
     ``_progress`` snapshot showing every design finished. It is the same
     reconstruction the manual ``scripts/finalize_stuck_job.py`` tool proves
     out; both call in here.

The caller (``timeout_stuck_job``) finalizes the returned result through
the SAME ``complete_job`` terminal/settle path the webhook uses, so
billing settles correctly (charge actual GPU consumed, release the
surplus). No parallel billing branch is introduced.
"""

from __future__ import annotations

import logging
import posixpath
from typing import Optional

logger = logging.getLogger(__name__)


def _candidate_from_partial(part: dict) -> dict:
    """Rebuild a result.candidate (nested scores) from a streamed partial.

    MEASUREMENTS ONLY. This carried the partial's ``filter_status`` across too,
    and that one line was a whole class of defect: a partial is streamed DURING
    the run, so it holds the verdict but not the metrics the run produces at
    the end — boltzgen's refolding RMSD arrives after the refold. The rebuilt
    candidate therefore froze a verdict about two measurements while carrying
    only one of them, on 50 stored candidates.

    Dropping it loses nothing, because no reader wants the word. The bar is
    applied to whatever measurements ARE here, at render time
    (shared/score_legends.judge), and a recovered candidate missing an
    end-of-run metric comes back UNJUDGED rather than passed or failed — which
    is the true statement about it.
    """
    basename = posixpath.basename(str(part.get("pdb_key") or ""))
    scores: dict = {}
    if part.get("iptm") is not None:
        scores["ipTM"] = part["iptm"]
    if part.get("plddt") is not None:
        scores["pLDDT"] = part["plddt"]
    if part.get("i_pae") is not None:
        # Kept under the streamed name. It is one quantity -- interface PAE --
        # that three containers spell three ways, and the partial schema has a
        # single field for it (webhooks/modal._sanitize_candidate), so the
        # partial cannot say which spelling its tool uses.
        # shared.score_legends resolves "i_pae" for both the rfdiffusion
        # (i_pAE) and rfantibody (ipAE) bars, so writing the raw name is what
        # keeps a recovered job judgeable. Dropping it made every recovered
        # rfdiffusion job unjudged forever on a leg it had measured.
        scores["i_pae"] = part["i_pae"]
    return {
        "rank": part.get("rank"),
        "pdb_key": f"designs/{basename}",
        "scores": scores,
    }


def _list_design_files(user_id: str, job_id: str) -> list[str]:
    """Fallback: list the design filenames present under the job's prefix."""
    from shared.credits import get_service_client  # noqa: PLC0415
    from shared.storage import OUTPUT_BUCKET  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return []
    prefix = f"{user_id}/{job_id}/designs"
    try:
        listing = client.storage.from_(OUTPUT_BUCKET).list(path=prefix)
    except Exception:
        return []
    names = []
    for item in listing or []:
        name = item.get("name") if isinstance(item, dict) else None
        if name and name.lower().endswith((".cif", ".pdb", ".mmcif")):
            names.append(name)
    return sorted(names)


def reconstruct(job) -> list[dict]:  # noqa: ANN001 — ToolJob, avoid import cycle
    """Rebuild result.candidates, keeping only designs that exist in Storage.

    Prefers the streamed ``inputs._partial_candidates`` (which carry per-design
    scores) and drops any whose structure is not actually in Storage. When no
    partials survive, lists the Storage prefix directly (no scores recoverable).
    Returns ``[]`` when the job produced nothing recoverable.
    """
    from shared.storage import output_exists  # noqa: PLC0415

    partials = (job.inputs or {}).get("_partial_candidates") or []
    candidates = []
    for part in partials:
        if not isinstance(part, dict):
            continue
        basename = posixpath.basename(str(part.get("pdb_key") or ""))
        if not basename:
            continue
        if not output_exists(
            user_id=job.user_id, job_id=job.id, filename=basename
        ):
            continue
        candidates.append(_candidate_from_partial(part))
    if candidates:
        return candidates
    # Partials missing/empty — list Storage directly (no scores recoverable).
    files = _list_design_files(job.user_id, job.id)
    return [
        {"rank": i + 1, "pdb_key": f"designs/{name}", "scores": {}}
        for i, name in enumerate(files)
    ]


def _probe_modal(job) -> tuple[Optional[dict], str]:  # noqa: ANN001
    """Probe Modal for ground truth on a stuck job's FunctionCall.

    Returns ``(inline_result, exit_verdict)`` where:

    * ``inline_result`` is the result dict when Modal already holds the
      finished payload inline (atomic tools, or any pipeline that returns
      its payload). ``None`` otherwise.
    * ``exit_verdict`` is one of:

      - ``"clean_exit"``  — the pipeline process exited 0. Either it
        returned its payload inline (``inline_result`` set) OR it took the
        webhook path and merely lost the callback. Positive evidence the
        run finished, so Storage reconstruction is trustworthy.
      - ``"failed"``      — the pipeline process exited NONZERO: a genuine
        crash. Partial designs left in Storage must NOT be resurrected as a
        success.
      - ``"unknown"``     — Modal is unreachable, the call is still running,
        the id is an offline stub, or the exit code was not reported. No
        ground truth; the caller must find completion evidence elsewhere.
    """
    fc_id = getattr(job, "modal_function_call_id", None)
    if not fc_id or str(fc_id).startswith("fc-stub-"):
        return None, "unknown"
    try:
        from gpu.modal_client import ModalClient  # noqa: PLC0415

        poll = ModalClient().poll(str(fc_id))
    except Exception:
        logger.warning(
            "recover: Modal poll raised for job %s", job.id, exc_info=True
        )
        return None, "unknown"
    if not isinstance(poll, dict):
        return None, "unknown"

    status = poll.get("status")
    if status == "succeeded":
        result = poll.get("result")
        if not isinstance(result, dict):
            return None, "unknown"
        out = dict(result)
        # Carry the poll-reported runtime so complete_job settles against
        # actual GPU time rather than the heartbeat snapshot.
        gpu_used = poll.get("gpu_seconds_used")
        if gpu_used is not None and not any(
            k in out for k in ("gpu_seconds", "runtime_seconds", "runtime_minutes")
        ):
            out["gpu_seconds"] = gpu_used
        return out, "clean_exit"

    if status == "failed":
        exit_code = poll.get("exit_code")
        if exit_code == 0:
            # Webhook path: pipeline exited clean, only the callback was lost.
            return None, "clean_exit"
        if isinstance(exit_code, int) and exit_code != 0:
            return None, "failed"
        # Failed poll with no exit code (older payload) — inconclusive.
        return None, "unknown"

    # running / error / anything else — no ground truth.
    return None, "unknown"


def _completion_signal(job) -> str:  # noqa: ANN001
    """Read the heartbeat progress snapshot as a completion verdict.

    ``inputs._progress`` (last-writer-wins from heartbeats) carries
    ``designs_completed`` / ``designs_total``. Returns:

    * ``"complete"``   — total is known (> 0) and every design finished.
    * ``"incomplete"`` — total is known (> 0) and some designs are missing.
    * ``"unknown"``    — no usable progress snapshot (the tool never
      reported a total).
    """
    progress = (job.inputs or {}).get("_progress")
    if not isinstance(progress, dict):
        return "unknown"
    try:
        completed = int(progress.get("designs_completed"))
        total = int(progress.get("designs_total"))
    except (TypeError, ValueError):
        return "unknown"
    if total <= 0:
        return "unknown"
    return "complete" if completed >= total else "incomplete"


def recover_stuck_job_result(job) -> Optional[dict]:  # noqa: ANN001
    """Return a finalizable ``succeeded`` result for a stuck job, or None.

    ``None`` means the work is not recoverable and the caller should time the
    job out (full refund) as before. A non-None return is a result dict ready
    to hand to ``complete_job(terminal_status="succeeded", ...)``.

    Recovery only fires on positive evidence the run genuinely finished, so a
    run that streamed a few designs and then FAILED (whose FAILED webhook was
    also lost) is timed out, not billed as a success:

    1. Modal holds the finished payload inline -> recover it.
    2. Modal reports a NONZERO pipeline exit -> genuine crash -> refuse.
    3. Storage is trusted only with positive completion evidence: a clean
       pipeline exit (exit 0, webhook merely lost) OR a heartbeat progress
       snapshot showing every design finished. An explicitly INCOMPLETE
       progress snapshot vetoes even a clean exit.
    """
    inline, exit_verdict = _probe_modal(job)

    # 1. Inline result already in hand (atomic tools / payload-returning runs).
    if inline is not None:
        return inline

    # 2. Ground-truth veto: the pipeline process crashed. Do not resurrect
    #    partial Storage designs from a genuinely failed run.
    if exit_verdict == "failed":
        return None

    # 3. Gate Storage reconstruction on positive completion evidence.
    progress = _completion_signal(job)
    if progress == "incomplete":
        # A clean exit contradicted by an incomplete design count is
        # suspicious; refuse rather than bill a partial run.
        return None
    if exit_verdict != "clean_exit" and progress != "complete":
        # Neither a clean pipeline exit nor a full-completion heartbeat —
        # we cannot confirm the run finished. Leave it to the timeout path.
        return None

    try:
        candidates = reconstruct(job)
    except Exception:
        logger.warning(
            "recover: Storage reconstruct raised for job %s",
            job.id, exc_info=True,
        )
        candidates = []
    if candidates:
        return {
            "candidates": candidates,
            "candidate_count": len(candidates),
            "backfilled": True,
        }

    return None
