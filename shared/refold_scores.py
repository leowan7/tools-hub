"""Per-design Boltz-2 cofold scores, read back onto the source job's table.

A binder-design run that carries no score of its own (rfd4) gets one by the
customer spawning a Boltz-2 cofold per design from the second-opinion panel
(``blueprints/jobs.py::_spawn_refold_job``). Those scores used to live only on
the spawned jobs and in /jobs/compare; this module is what carries them back.

THE JOIN KEY IS THE FASTA HEADER, and that is not a parse.
``_spawn_refold_job`` writes the boltz2 job's binder as
``inputs["binder_sequences"][0]["name"] = seq.fasta_header``
(``blueprints/jobs.py::_spawn_refold_job``), and
``shared.refold.CandidateSeq.fasta_header``
builds that string from the source candidate's ``rank`` and ``pdb_key``
alone. Both of those are on the source row, so :func:`design_key`
below rebuilds the same string with the same property, on both sides of the
join. No new column is written and no stored string is taken apart, which is
also why this works for refolds spawned before this module existed.

ONE QUERY PER PAGE, not one per row: the jsonb filter on
``inputs->>_refold_of_job_id`` returns every refold of a source job in a single
round trip (same operator form as the ``inputs->_wallet->>live`` filter in
``shared/jobs.py::meter_live_runs``).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from shared.credits import get_service_client
from shared.refold import candidate_seq_from_record

logger = logging.getLogger(__name__)

_TABLE = "tool_jobs"

# How many spawned refolds one source job's page will read back. The spawn
# itself is capped at shared.refold.MAX_REFOLD_N per click, but a customer may
# click more than once, so this is not that cap restated -- it is a bound on
# the read. Past it the page shows the NEWEST _MAX_REFOLDS_READ refolds and
# the rest of the rows read as un-folded; the table states no total and claims
# no completeness.
_MAX_REFOLDS_READ = 200


def design_key(cand: Any, idx: int = 0) -> Optional[str]:
    """The join key for one source design row, or None when it has none.

    Calls the SAME function the spawn side calls --
    :func:`shared.refold.candidate_seq_from_record`, which
    ``blueprints/jobs.py::_spawn_refold_job`` reaches through
    ``extract_top_n_sequences`` -- so the two sides of the join cannot drift
    apart, including in the ``idx``-seeded fallbacks for a row that carries no
    ``rank`` or ``pdb_key``. Returns None for a row with no designed sequence,
    which is also a row no refold could have been spawned from.
    """
    if not isinstance(cand, dict):
        return None
    try:
        seq = candidate_seq_from_record(cand, idx)
    except (TypeError, ValueError):
        return None
    return seq.fasta_header if seq is not None else None


def _iptm_of(result: Any) -> Optional[float]:
    """The ipTM of a finished one-design cofold, or None.

    A refold job is submitted with ``parameters.n_designs_total = 1``
    (``blueprints/jobs.py::_spawn_refold_job``), so its result holds exactly one
    record. None
    rather than 0.0 for every other shape: a cofold that finished without
    writing a number has no number, and the caller renders that differently
    from a measured one.

    BOTH candidate shapes are read, because a refold can arrive in either.
    A boltz2 job that finished normally writes ``designs`` with the metrics
    inline at the record root; one whose webhook was lost and was rebuilt from
    Storage by shared/job_recovery.py::_candidate_from_partial writes
    ``candidates`` with the score nested as ``scores["ipTM"]``, and
    shared/jobs.py::candidate_records PREFERS ``candidates``. Reading only the
    root would make every recovered refold report no score while that same
    job's own page (templates/tools/boltz2_results.html) showed the number.
    """
    from shared.jobs import candidate_records  # noqa: PLC0415

    recs = candidate_records(result if isinstance(result, dict) else None)
    if not recs:
        return None
    first = recs[0]
    if not isinstance(first, dict):
        return None
    raw = first.get("iptm", first.get("ipTM"))
    if raw is None:
        nested = first.get("scores")
        if isinstance(nested, dict):
            raw = nested.get("ipTM", nested.get("iptm"))
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    # NaN is not a measurement; it formats as "nan" and would read as one.
    return None if val != val else val


# Job status -> the state this module reports. Unmapped statuses are reported
# as "running": the only ones VALID_STATUSES holds beyond these are the
# non-terminal pair, and a state this module has not been taught is closer to
# "not finished" than to any claim about a score.
_STATE_BY_STATUS = {
    "succeeded": "succeeded",
    "failed": "failed",
    "timeout": "failed",
    "cancelled": "cancelled",
}


def cofold_scores_for_source(
    user_id: str, source_job_id: str
) -> dict[str, dict]:
    """Map :func:`design_key` -> ``{"state": str, "iptm": float|None}`` for the
    Boltz-2 refolds of ``source_job_id`` owned by ``user_id``.

    ``state`` is one of ``succeeded`` / ``running`` / ``failed`` /
    ``cancelled``. A ``succeeded`` entry may still carry ``iptm: None`` -- a
    cofold that finished without a number -- and the caller must not render
    that as a measurement.

    Returns ``{}`` on any failure, including no database client. An empty map
    means "no refold scores to show", which renders as the un-refolded state;
    it never renders as a bad score.

    WHICH ATTEMPT A DESIGN SHOWS, when it has been re-folded more than once:
    the newest attempt that is either still running or actually measured a
    number. If no attempt qualifies, the newest one wins and the cell reports
    how it ended.

    That rule exists because plain newest-wins deletes measurements. Re-folding
    a design and having the new attempt fail, be cancelled, or raise at the
    Modal submit -- which leaves the row it had already created marked failed
    (``blueprints/jobs.py::_spawn_refold_job``) -- would turn a measured 0.82
    into "fold failed" and take the only number the customer paid for off the
    table. A still-running attempt does win over an older measurement: a fold
    the customer just started is the more useful thing to tell them, and the
    measurement comes back by this same rule if that attempt ends without one.
    Two re-folds of one design in flight at once, the newer ending while the
    older still runs, is why the choice is made over ALL of a design's attempts
    rather than against the previous winner alone. Held by
    ``test_a_failed_re_fold_does_not_erase_an_earlier_measurement`` in
    tests/test_target_table_render.py.
    """
    client = get_service_client()
    if client is None or not user_id or not source_job_id:
        return {}
    try:
        rows = (
            client.table(_TABLE)
            .select("id,status,inputs,result,created_at")
            .eq("user_id", user_id)
            .eq("tool", "boltz2")
            .eq("inputs->>_refold_of_job_id", str(source_job_id))
            .order("created_at", desc=True)
            .limit(_MAX_REFOLDS_READ)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning(
            "cofold_scores_for_source failed for job %s", source_job_id,
            exc_info=True,
        )
        return {}

    out: dict[str, dict] = {}
    settled: set[str] = set()
    # Newest-first off the wire (see the .order above) and walked in that
    # order, so the first qualifying attempt met for a design is its newest.
    for row in rows:
        inputs = row.get("inputs")
        # ``tool_jobs.inputs`` is jsonb NOT NULL, but a bare jsonb scalar is a
        # legal value of that column and ``"legacy".get`` is an AttributeError
        # -- the same row shape shared/target_results.py::_is_refold guards.
        if not isinstance(inputs, dict):
            continue
        seqs = inputs.get("binder_sequences")
        if not isinstance(seqs, list) or not seqs:
            continue
        first = seqs[0]
        key = (first.get("name") or "").strip() if isinstance(first, dict) else ""
        if not key:
            continue
        status = str(row.get("status") or "")
        state = _STATE_BY_STATUS.get(status, "running")
        entry = {
            "state": state,
            "iptm": _iptm_of(row.get("result")) if state == "succeeded" else None,
            "job_id": str(row.get("id") or ""),
        }
        # See WHICH ATTEMPT A DESIGN SHOWS in the docstring. Running or
        # measured settles the design, because rows arrive newest-first.
        # Anything else is only a fallback, held until something qualifies.
        if key in settled:
            continue
        if entry["state"] == "running" or entry["iptm"] is not None:
            out[key] = entry
            settled.add(key)
        elif key not in out:
            out[key] = entry
    return out
