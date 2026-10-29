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

Dry run reads the structures and prints what it would fill; it writes
nothing. Jobs are walked in id order, ``--batch`` at a time, and every batch
prints the ``--after`` value to resume from. Rerunning is safe: a row that
already stores chains, or stores a sequence, is skipped, and a job with
nothing to fill is not written. Only terminal jobs completed more than
``--min-age-minutes`` ago are touched, so a terminal write still in flight is
not raced; the write itself is conditional on the job's status not having
changed since it was read.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

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


def _stored_count(result) -> int:
    return sum(isinstance(c, dict) and STORED_CHAINS in c
               for c in candidate_records(result))


def _fetcher(user_id: str):
    def fetch(job_id: str, filename: str):
        try:
            return download_output(user_id=user_id, job_id=job_id, filename=filename)
        except StorageError as exc:
            print(f"  storage miss {job_id}/{filename}: {exc}", file=sys.stderr)
            return None
    return fetch


def backfill_job(row: dict, *, apply: bool) -> int:
    """Rows filled (or, dry run, that would be) for one ``tool_jobs`` row."""
    result = row.get("result")
    if not isinstance(result, dict):
        return 0
    new = _with_chain_sequences(
        result, fetch_bytes=_fetcher(row["user_id"]), default_job_id=row["id"])
    filled = _stored_count(new) - _stored_count(result)
    if filled and apply and not _cas_update(
            row["id"], {"result": new}, allowed_current=(row["status"],)):
        print(f"  {row['id']}: not written (status changed since read, or the write failed: see log)")
        return 0
    return filled


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--after", default="", help="resume after this job id")
    ap.add_argument("--tool", help="only this tool slug")
    ap.add_argument("--batch", type=int, default=20, help="jobs per page")
    ap.add_argument("--limit", type=int, default=0, help="stop after N jobs (0 = all)")
    ap.add_argument("--min-age-minutes", type=int, default=60)
    a = ap.parse_args(argv)

    client = get_service_client()
    if client is None:
        print("no Supabase service client (SUPABASE_URL / key unset)", file=sys.stderr)
        return 1
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=a.min_age_minutes)).isoformat()
    cursor, jobs, rows_filled = a.after, 0, 0
    while True:
        q = (client.table("tool_jobs").select("id,user_id,tool,status,result")
             .in_("status", sorted(TERMINAL_STATUSES)).lt("completed_at", cutoff))
        if a.tool:
            q = q.eq("tool", a.tool)
        if cursor:
            q = q.gt("id", cursor)
        page = q.order("id").limit(a.batch).execute().data or []
        if not page:
            break
        for row in page:
            n = backfill_job(row, apply=a.apply)
            if n:
                print(f"{row['id']} {row['tool']} {row['status']}: "
                      f"{'filled' if a.apply else 'would fill'} {n} row(s)")
            rows_filled += n
            jobs += 1
            cursor = row["id"]
            if a.limit and jobs >= a.limit:
                break
        print(f"-- {jobs} job(s) scanned; resume with --after {cursor}")
        if a.limit and jobs >= a.limit:
            break
    print(f"done: {jobs} job(s), {rows_filled} row(s) "
          f"{'filled' if a.apply else 'would be filled (dry run)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
