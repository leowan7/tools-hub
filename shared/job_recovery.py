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


# The proteina measurement fields of webhooks/modal.py::_sanitize_candidate. The
# heartbeat (tools/proteina/run_pipeline.py) streams the first five; the rest
# are copied only if a partial ever carries them.
_PROTEINA_SCORE_KEYS = (
    "total_reward", "af2_iptm", "af2_plddt", "rf3_score", "binder_scrmsd",
    "ligand_scrmsd", "ff_energy", "rmsd", "min_ipae",
)

# Other measurement fields of webhooks/modal.py::_sanitize_candidate, kept under
# the streamed name, which is the name the finished run's rows use: boltz2 and
# af2 stream their design_entry as new_candidate (tools/*/run_pipeline.py).
# Without them a kept boltz2 design's CSV had ipTM as its only score
# (tests/test_cancel_keeps_designs.py::test_a_kept_design_keeps_every_streamed_score).
_STREAMED_SCORE_KEYS = (
    "ptm", "complex_plddt", "complex_iplddt", "n_hotspot_contacts",
    # opendde's first column, under the name templates/tools/
    # opendde_results.html:60 reads. Dropped here, so a kept OpenDDE
    # prediction showed a dash where its ranking score belongs
    # (tests/test_streamed_scores_survive_the_gates.py).
    "ranking_score",
)


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
    if part.get("mean_plddt") is not None:
        # Renamed to the display spelling, like the two above and for the same
        # reason: it is the first column of templates/tools/af2_results.html
        # (:68), colabfold (:58) and esmfold (:69), which all spell it
        # ``mean_pLDDT``, so the rebuilt row's column reads the same as a
        # finished run's. Unlike ``i_pae`` below there is only one spelling to
        # pick. af2/colabfold/esmfold stream it as ``mean_plddt``.
        scores["mean_pLDDT"] = part["mean_plddt"]
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
    # proteina's partial names each score as its final result's ``scores`` does
    # (tools/proteina/run_pipeline.py's new_candidate heartbeat), the names the
    # ``columns`` of templates/tools/proteina_results.html read. Without them a
    # kept proteina design showed every score as a dash (seen on prod job
    # 580d793b, 2026-10-19).
    for key in _PROTEINA_SCORE_KEYS + _STREAMED_SCORE_KEYS:
        if part.get(key) is not None:
            scores[key] = part[key]
    # NO ``name`` KEY, DELIBERATELY. ``_sanitize_candidate`` allowlists one
    # (webhooks/modal.py), so a recovered row drops a name the heartbeat carried
    # — but every reader falls back to the pdb_key's basename
    # (blueprints/admin.py:331-332, templates/jobs_compare.html:292), and ``name``
    # is not an export column (it is in ``_NON_METRIC_ROOT_KEYS``,
    # shared/exports.py). For rfd4 the two are the same string anyway: its
    # pipeline sets ``name = pdb_key`` minus the ``.cif``
    # (tools/rfd4/run_pipeline.py::_upload_design). Carrying it would be
    # harmless; not carrying it costs nothing that has been found.
    candidate = {
        "rank": part.get("rank"),
        "pdb_key": f"designs/{basename}",
        "scores": scores,
    }
    # At the root, where templates/tools/proteina_results.html reads it; the
    # partial's value is already allowlisted by
    # webhooks/modal.py::_sanitize_candidate.
    if part.get("target_numbering") is not None:
        candidate["target_numbering"] = part["target_numbering"]
    # Also at the root, not in ``scores``: boltz2's against-bar reads
    # ``c.n_hotspots`` as the denominator of its contacts cell
    # (templates/tools/boltz2_results.html:191), and it is not a score column
    # anywhere. Without it a kept design counted its contacts against the
    # hotspots the operator ASKED for, which is a different number.
    if part.get("n_hotspots") is not None:
        candidate["n_hotspots"] = part["n_hotspots"]
    return candidate


# storage3's list() defaults to ``{"limit": 100}``
# (``storage3._sync.file_api.DEFAULT_SEARCH_OPTIONS``), so every call below
# asks for its page explicitly. _LIST_MAX bounds the walk; a prefix holding
# more than that reconstructs from its first _LIST_MAX objects.
# ponytail: fixed ceiling, make it configurable if a tool ever exceeds it.
_LIST_PAGE = 100
_LIST_MAX = 10_000


def _list_prefix_names(user_id: str, job_id: str) -> list[str]:
    """Every object name under the job's designs prefix, or [] on any error.

    Returning [] rather than the pages that did load keeps the conservative
    direction: callers read a missing name as a design that did not survive,
    so an unreadable listing refunds the job instead of finalising a silently
    truncated result as a success.
    """
    from shared.credits import get_service_client  # noqa: PLC0415
    from shared.storage import OUTPUT_BUCKET  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return []
    prefix = f"{user_id}/{job_id}/designs"
    names: list[str] = []
    for offset in range(0, _LIST_MAX, _LIST_PAGE):
        try:
            listing = client.storage.from_(OUTPUT_BUCKET).list(
                path=prefix,
                options={"limit": _LIST_PAGE, "offset": offset},
            )
        except Exception:
            return []
        rows = listing or []
        names.extend(
            item["name"]
            for item in rows
            if isinstance(item, dict) and item.get("name")
        )
        # Page on the RAW row count, never the filtered one: a short page is
        # the only end-of-listing signal, and a page that is full but holds a
        # row we skip would otherwise end the walk and drop every page after
        # it -- the same silent truncation this pagination exists to close.
        if len(rows) < _LIST_PAGE:
            break
    return names


def _list_design_files(user_id: str, job_id: str) -> list[str]:
    """Fallback: list the design filenames present under the job's prefix."""
    return sorted(
        name
        for name in _list_prefix_names(user_id, job_id)
        if name.lower().endswith((".cif", ".pdb", ".mmcif"))
    )


def reconstruct(job) -> list[dict]:  # noqa: ANN001 — ToolJob, avoid import cycle
    """Rebuild result.candidates, keeping only designs that exist in Storage.

    Prefers the streamed ``inputs._partial_candidates`` (which carry per-design
    scores) and drops any whose structure is not actually in Storage. When no
    partials survive, lists the Storage prefix directly (no scores recoverable).
    Returns ``[]`` when the job produced nothing recoverable.

    Existence is tested against ONE paginated listing instead of a per-design
    ``output_exists`` round trip. ``_partial_candidates`` runs to 1000
    (``webhooks/modal.py::_hb_merge_inputs``) and this is now reached from
    inside the status request (``blueprints/jobs.py::job_status``), where a
    Storage call per design would hold the worker past gunicorn's 120 s
    watchdog (``gunicorn.conf.py::timeout``). Names are compared after
    ``_safe_filename``, which is both what ``output_exists`` matched on
    (``shared/storage.py::output_exists``) and what wrote the object in the
    first place (``_output_object_path``,
    ``shared/storage.py::_output_object_path``).
    """
    from shared.storage import _safe_filename  # noqa: PLC0415

    present = set(_list_prefix_names(job.user_id, job.id))
    partials = (job.inputs or {}).get("_partial_candidates") or []
    candidates = []
    for part in partials:
        if not isinstance(part, dict):
            continue
        basename = posixpath.basename(str(part.get("pdb_key") or ""))
        if not basename or _safe_filename(basename) not in present:
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
      - ``"failed_unconfirmed"`` — ``poll`` returned a dict but no usable
        answer for this call, and gave no nonzero exit code to confirm that
        the pipeline itself crashed. Reached by ``status="failed"`` with no
        integer exit code, and by ``status="error"``, which
        ``gpu/modal_client.py`` returns from four places, not one: the
        ``modal`` package missing, ``poll``'s own outer ``except Exception``,
        a non-dict pipeline return, and an unrecognised
        ``smoke_result.status``. The second of those is the broadest: it
        fires both when ``FunctionCall.get`` re-raised the remote function's
        own exception AND when our own ``ModalCallTimeout`` fired on a wedged
        channel, which ``poll`` deliberately re-raises into that handler. So
        this value covers "the run died", "Modal reported a failure it could
        not pin down", "our poll channel wedged" and "we could not even ask"
        alike — it does NOT mean Modal necessarily answered. Every branch
        below treats it exactly as
        ``"unknown"``; only the money differs, on the safe side — see
        ``recover_stuck_job_result``'s docstring.
      - ``"still_running"`` — ``status="running"``: Modal says this call
        has not finished. Unlike ``"unknown"`` this is evidence, and it points
        the other way — a caller about to terminalise the job is ending a
        live run. Every branch below treats it as ``"unknown"``; only
        ``timeout_stuck_job`` reads it, to keep designs without billing
        (tests/test_sweep_stuck_recovery.py::TestGenuinelyDeadJobStillTimesOut
        ::test_a_run_modal_still_calls_running_keeps_designs_but_is_not_billed).
      - ``"unknown"``     — no ground truth about how the run ended. Either
        nothing was asked or parsed (the id is an offline stub, the ``poll``
        call itself raised, the payload was not a dict) or Modal answered
        with a status that settles nothing about the pipeline's own fate
        — including ``status="timeout"``, the container hitting its OWN
        limit, which is parsed and is the one shape that is unambiguously a
        time-out rather than a failure. Note that an unreachable Modal splits
        across both values — it lands here when ``poll`` raises, and in
        ``"failed_unconfirmed"`` when ``poll`` swallows the fault and reports
        ``status="error"``. No ground truth either way; the caller must find
        completion evidence elsewhere.
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
        # Failed poll with no exit code (older payload): a reported failure
        # with its evidence missing, which is not the same as no failure.
        return None, "failed_unconfirmed"

    if status == "error":
        # ``poll`` could not get a result out of the call at all. See the
        # "failed_unconfirmed" bullet above for why that is not "unknown".
        return None, "failed_unconfirmed"

    if status == "running":
        # Modal says the call has not finished. That is not an absence of
        # evidence like the tail below: it is positive evidence that the run
        # was still alive, so whoever terminalises it now is us.
        return None, "still_running"

    # timeout / anything else — no ground truth. A container that hit its
    # OWN timeout lands here (status="timeout",
    # gpu/modal_client.py::ModalClient.poll) and is the one shape that is
    # unambiguously a time-out rather than a failure.
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


def recover_stuck_job_result(
    job, *, probe_modal: bool = True, verdict_out: Optional[dict] = None
) -> Optional[dict]:  # noqa: ANN001
    """Return a finalizable ``succeeded`` result for a stuck job, or None.

    ``None`` means the work is not recoverable and the caller should time the
    job out. A non-None return is a result dict ready to hand to
    ``complete_job(terminal_status="succeeded", ...)``.

    ``None`` on its own no longer implies a full refund: since Leo's
    2026-11-04 decision a timed-out run that delivered designs is billed
    (``timeout_with_delivery`` in shared/jobs.py::_BILLED_FAILURE_CLASSES).
    The two reasons for ``None`` below therefore need different money, and
    only the Modal probe can tell them apart. Pass a dict as ``verdict_out``
    to receive that ground truth under ``"exit_verdict"``: ``"failed"`` is a
    confirmed crash and ``"failed_unconfirmed"`` is a fault we could not pin
    down — which, per ``_probe_modal``'s bullet, includes our own poll channel
    wedging. Neither may be billed off designs rebuilt from Storage, and ``timeout_stuck_job``
    skips the keep for both
    (tests/test_sweep_stuck_recovery.py::TestGenuinelyDeadJobStillTimesOut::
    test_crashed_pipeline_with_partial_designs_is_not_billed and
    ::test_an_unconfirmed_modal_failure_is_not_billed).

    Recovery only fires on positive evidence the run genuinely finished, so a
    run that streamed a few designs and then FAILED (whose FAILED webhook was
    also lost) is timed out, not billed as a success:

    1. Modal holds the finished payload inline -> recover it.
    2. Modal reports a NONZERO pipeline exit -> genuine crash -> refuse.
    3. Storage is trusted only with positive completion evidence: a clean
       pipeline exit (exit 0, webhook merely lost) OR a heartbeat progress
       snapshot showing every design finished. An explicitly INCOMPLETE
       progress snapshot vetoes even a clean exit.

    ``probe_modal=False`` skips step 1/2's Modal round trip for a caller that
    already holds a terminal Modal verdict for this FunctionCall; recovery
    then rests on step 3 alone. See the comment at the call site.
    """
    if probe_modal:
        inline, exit_verdict = _probe_modal(job)
    else:
        # ``probe_modal=False``: the caller already polled Modal this request
        # and holds a TERMINAL verdict. Re-polling the same FunctionCall can
        # only raise the same FunctionTimeoutError, which _probe_modal's tail
        # maps to "unknown" regardless -- so the probe buys nothing and costs a
        # second 90 s _bounded_modal_call inside one request. Two stacked hops
        # is 180 s, past the 120 s gunicorn watchdog that kills the worker and
        # every other request on it (gunicorn.conf.py::timeout,
        # tests/test_modal_call_deadline.py::
        # test_it_is_below_the_gunicorn_worker_watchdog). Recovery then rests
        # on the heartbeat + Storage evidence below, which is the only thing
        # that could have recovered this job anyway.
        inline, exit_verdict = None, "unknown"

    if verdict_out is not None:
        # The caller needs step 2's ground truth even when we return None,
        # because the two None reasons differ in money (see the docstring).
        verdict_out["exit_verdict"] = exit_verdict

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
