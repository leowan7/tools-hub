"""Finalize a stuck composite-tool job whose terminal webhook never landed.

A composite pilot (e.g. BoltzGen) can finish on Modal, upload its design CIFs
to tool-outputs Storage, and POST a COMPLETED webhook that returns 200 — yet
stay status="running" because the oversized result write threw before
shared.jobs._slim_result_for_persist bounded it. The structures are already in
Storage, so this script rebuilds result.candidates from the streamed
_partial_candidates (or a Storage listing) and finalizes the job through the
normal complete_job path.

Usage (from the tools-hub repo root, with SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
in the env or a local .env):

    python scripts/finalize_stuck_job.py --job-id <uuid>                         # dry run
    python scripts/finalize_stuck_job.py --job-id <uuid> --runtime-minutes 108.6 --commit

Dry run prints the reconstructed result; --commit persists status=succeeded and
runs the normal side effects (wallet settle against the GPU time implied by
--runtime-minutes, plus the completion email).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Allow `python scripts/finalize_stuck_job.py` from the repo root to import the
# shared package (running a script puts scripts/ on sys.path, not the root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from shared.jobs import complete_job, get_job
# The Storage reconstruction lives in shared.job_recovery so the stuck-job
# sweeper and this manual tool share one proven implementation.
from shared.job_recovery import reconstruct


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Finalize a stuck composite-tool job from Storage."
    )
    ap.add_argument("--job-id", required=True)
    ap.add_argument(
        "--runtime-minutes", type=float, default=None,
        help="Runtime to settle the wallet hold against (e.g. 108.6).",
    )
    ap.add_argument(
        "--commit", action="store_true",
        help="Persist the finalize (default: dry run).",
    )
    args = ap.parse_args()

    job = get_job(args.job_id)
    if job is None:
        print(f"Job {args.job_id} not found.", file=sys.stderr)
        return 1
    if job.status not in ("pending", "running"):
        print(
            f"Job {args.job_id} is already {job.status}; nothing to do.",
            file=sys.stderr,
        )
        return 1

    candidates = reconstruct(job)
    if not candidates:
        print("No design files found in Storage; cannot finalize.", file=sys.stderr)
        return 1

    result: dict = {
        "candidates": candidates,
        "candidate_count": len(candidates),
        "backfilled": True,
    }
    if args.runtime_minutes is not None:
        result["runtime_minutes"] = args.runtime_minutes

    print(json.dumps(result, indent=2))
    print(
        f"\n{len(candidates)} candidates reconstructed for job {job.id} "
        f"(user {job.user_id})."
    )
    if any("refolding_rmsd" not in c.get("scores", {}) for c in candidates):
        print(
            "NOTE: refolding_rmsd is unavailable (not in partials; metrics.csv "
            "never uploaded) and is omitted."
        )

    if not args.commit:
        print("\nDry run — re-run with --commit to persist.")
        return 0

    fresh = complete_job(job.id, terminal_status="succeeded", result=result)
    if fresh is None or fresh.status != "succeeded":
        print(
            f"complete_job did not finalize (status={getattr(fresh, 'status', None)}).",
            file=sys.stderr,
        )
        return 1
    print(
        f"\nJob {job.id} finalized: status={fresh.status}, "
        f"{len((fresh.result or {}).get('candidates', []))} candidates persisted."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
