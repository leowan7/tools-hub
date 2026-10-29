"""Store chain sequences on the rows of jobs that finished before they were
stored at ingest (shared.jobs._with_chain_sequences).

A row that has them exports its sequences without a structure read, so the
CSV/FASTA ``_MAX_STRUCTURE_READS`` cap no longer reaches it. This script reads
each remaining row's structure from Storage once and writes the chains into
``tool_jobs.result``. It runs as its own process, not in the web tier.

Usage (repo root, SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in the env or .env):

    python scripts/backfill_chain_sequences.py                      # dry run
    python scripts/backfill_chain_sequences.py --tool rfantibody    # dry run, one tool
    python scripts/backfill_chain_sequences.py --apply              # write
    python scripts/backfill_chain_sequences.py --apply --after <id> # resume
    python scripts/backfill_chain_sequences.py --apply --ids <id>,<id>  # just these

Dry run reads the structures and prints what it would fill; it writes
nothing. Jobs are walked in id order, ``--batch`` ids at a time, each job's
row read on its own, and every batch of a paged run prints the ``--after``
value to resume from. Rerunning is safe: a row that already stores chains, or stores a
sequence, is skipped, and a job with nothing to fill is not written. Only
terminal jobs completed more than ``--min-age-minutes`` ago are touched, so a
terminal write still in flight is not raced; the write itself is conditional
on the job's status not having changed since it was read.

Storage downloads run one at a time with ``--pause`` seconds after each, and
the same pause between jobs. A 429, 5xx, network error or statement timeout
(57014) is retried ``--retries`` times with doubling backoff. A job whose
write, or a Storage read that failed transiently, still fails is listed for a
``--ids`` rerun; a read that misses for good (404, empty) is only printed. A
page or row read that still fails ends the run with exit status 2, the
resume point and that list.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from shared.credits import get_service_client  # noqa: E402
from shared.exports import STORED_CHAINS  # noqa: E402
from shared.jobs import (  # noqa: E402
    TERMINAL_STATUSES, _cas_update, _with_chain_sequences, candidate_records,
)
from shared.storage import StorageError, download_output  # noqa: E402

BACKOFF_SECONDS = 2.0
_TRANSIENT_CODES = {"429", "57014", "DatabaseTimeout", "too_many_connections"}


def _transient(exc: BaseException) -> bool:
    """Whether ``exc``, or an exception it was raised from, is worth a retry:
    a network error, HTTP 429 or 5xx, Postgres statement timeout 57014, or a
    Storage DatabaseTimeout / too_many_connections."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, httpx.TransportError):
            return True
        resp = getattr(exc, "response", None)
        for value in (getattr(exc, "status", None), getattr(exc, "code", None),
                      getattr(resp, "status_code", None)):
            code = str(value)
            if code in _TRANSIENT_CODES or (len(code) == 3 and code[0] == "5"
                                            and code.isdigit()):
                return True
        exc = exc.__cause__ or exc.__context__
    return False


def _retry(fn, what: str, retries: int):
    """``fn()``, retried up to ``retries`` more times on a transient error
    with ``BACKOFF_SECONDS`` doubling each time. Anything else, or the last
    transient error, is raised."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= retries or not _transient(exc):
                raise
            wait = BACKOFF_SECONDS * 2 ** attempt
            print(f"  {what}: {str(exc)[:200]}; retry in {wait:g}s", file=sys.stderr)
            time.sleep(wait)


def _stored_count(result) -> int:
    return sum(isinstance(c, dict) and STORED_CHAINS in c
               for c in candidate_records(result))


def _fetcher(row_id: str, user_id: str, *, pause: float, retries: int,
             failed: set):
    # ponytail: the lock makes the exports' read pool download one at a time;
    # _STRUCTURE_READ_THREADS (16) at once got 429 too_many_connections from
    # Storage in the 2026-10-29 production run (runner's report).
    lock = threading.Lock()

    def fetch(job_id: str, filename: str):
        with lock:
            try:
                return _retry(lambda: download_output(
                    user_id=user_id, job_id=job_id, filename=filename),
                    f"download {job_id}/{filename}", retries)
            except StorageError as exc:
                print(f"  storage miss {job_id}/{filename}: {exc}", file=sys.stderr)
                if _transient(exc):
                    failed.add(row_id)
                return None
            finally:
                time.sleep(pause)
    return fetch


def backfill_job(row: dict, *, apply: bool, pause: float = 0.0, retries: int = 0,
                 failed: set | None = None) -> int:
    """Rows filled (or, dry run, that would be) for one ``tool_jobs`` row.
    A job whose write, or a transient Storage read, still fails after retries
    goes in ``failed``."""
    failed = set() if failed is None else failed
    result = row.get("result")
    if not isinstance(result, dict):
        return 0
    new = _with_chain_sequences(
        result, default_job_id=row["id"],
        fetch_bytes=_fetcher(row["id"], row["user_id"], pause=pause, retries=retries,
                             failed=failed))
    filled = _stored_count(new) - _stored_count(result)
    if not (filled and apply):
        return filled
    for attempt in range(retries + 1):
        if _cas_update(row["id"], {"result": new}, allowed_current=(row["status"],)):
            return filled
        if attempt < retries:
            wait = BACKOFF_SECONDS * 2 ** attempt
            print(f"  {row['id']}: write failed or status changed; retry in {wait:g}s",
                  file=sys.stderr)
            time.sleep(wait)
    print(f"  {row['id']}: not written (status changed since read, or the write "
          f"failed: see log)")
    failed.add(row["id"])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--after", default="", help="resume after this job id")
    ap.add_argument("--ids", default="",
                    help="comma-separated job ids: only these, no paging")
    ap.add_argument("--tool", help="only this tool slug")
    ap.add_argument("--batch", type=int, default=20, help="job ids per page")
    ap.add_argument("--limit", type=int, default=0, help="stop after N jobs (0 = all)")
    ap.add_argument("--min-age-minutes", type=int, default=60)
    ap.add_argument("--pause", type=float, default=0.25,
                    help="seconds after each Storage download and each job")
    ap.add_argument("--retries", type=int, default=5,
                    help="retries on a transient error, backoff doubling from "
                         f"{BACKOFF_SECONDS:g}s")
    a = ap.parse_args(argv)
    if a.retries < 0:
        ap.error("--retries must be 0 or more")
    if a.ids and a.after:
        ap.error("--ids and --after do not combine")

    client = get_service_client()
    if client is None:
        print("no Supabase service client (SUPABASE_URL / key unset)", file=sys.stderr)
        return 1
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=a.min_age_minutes)).isoformat()

    def query(columns: str):
        q = (client.table("tool_jobs").select(columns)
             .in_("status", sorted(TERMINAL_STATUSES)).lt("completed_at", cutoff))
        return q.eq("tool", a.tool) if a.tool else q

    def page_ids(cursor: str) -> list:
        if a.ids:
            return [] if cursor else [i.strip() for i in a.ids.split(",") if i.strip()]
        q = query("id")
        if cursor:
            q = q.gt("id", cursor)
        return [r["id"] for r in q.order("id").limit(a.batch).execute().data or []]

    def read_row(job_id: str):
        data = query("id,user_id,tool,status,result").eq("id", job_id).execute().data
        return data[0] if data else None

    cursor, jobs, rows_filled, failed = a.after, 0, 0, set()
    while True:
        try:
            ids = _retry(lambda: page_ids(cursor), "page query", a.retries)
        except Exception as exc:
            return _stop(exc, a.ids and f"--ids {a.ids}" or cursor and f"--after {cursor}",
                         failed)
        if not ids:
            break
        for job_id in ids:
            try:
                row = _retry(lambda: read_row(job_id), f"read {job_id}", a.retries)
            except Exception as exc:
                return _stop(exc, a.ids and f"--ids {a.ids}" or cursor and f"--after {cursor}",
                         failed)
            if row is None:
                print(f"{job_id}: not found, not terminal, or too recent; skipped")
                n = 0
            else:
                n = backfill_job(row, apply=a.apply, pause=a.pause,
                                 retries=a.retries, failed=failed)
                if n:
                    print(f"{row['id']} {row['tool']} {row['status']}: "
                          f"{'filled' if a.apply else 'would fill'} {n} row(s)")
            rows_filled += n
            jobs += 1
            if not a.ids:
                cursor = job_id
            if a.limit and jobs >= a.limit:
                break
            time.sleep(a.pause)
        if a.ids:
            break
        print(f"-- {jobs} job(s) scanned; resume with --after {cursor}")
        if a.limit and jobs >= a.limit:
            break
    print(f"done: {jobs} job(s), {rows_filled} row(s) "
          f"{'filled' if a.apply else 'would be filled (dry run)'}")
    _print_failed(failed)
    return 0


def _print_failed(failed: set) -> None:
    if failed:
        print(f"incomplete after retries, rerun with --ids {','.join(sorted(failed))}")


def _stop(exc: BaseException, resume: str, failed: set) -> int:
    print(f"stopped: {str(exc)[:300]}", file=sys.stderr)
    print(f"resume with {resume}" if resume else "resume from the start",
          file=sys.stderr)
    _print_failed(failed)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
