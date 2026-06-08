"""Poll the 5 Week-2 calibration jobs and emit a status snapshot.

Reads dispatched.json, queries Supabase for terminal status + error,
writes results.json with classified outcomes. Idempotent — safe to
re-run as jobs progress.

Classification (per CALIBRATION-WEEK2.md):
    OOM           — "CUDA out of memory" / "Killed" in error
    TIMEOUT       — "subprocess.TimeoutExpired" or wall-clock at cap
    ASSERTION     — early AssertionError (e.g. contig builder)
    SLOW_SUCCESS  — status=succeeded, wall_s near cap
    FAST_SUCCESS  — status=succeeded, wall_s well under cap
    OTHER         — surfaces an unrelated bug to investigate separately
    PENDING       — not yet terminal
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv()
from shared.credits import get_service_client
DISPATCHED = REPO_ROOT / "tmp" / "calibration" / "dispatched.json"
RESULTS = REPO_ROOT / "tmp" / "calibration" / "results.json"


# Pilot-tier subprocess caps from gpu/modal_client.PRESET_CAPS.
PILOT_CAP_S = {
    "rfantibody":  1800,
    "rfdiffusion": 1800,
    "bindcraft":   7200,
    "boltzgen":    3600,
}


def _classify(row: dict, dispatched_meta: dict) -> tuple[str, str | None]:
    """Return (mode, summary) for one tool_jobs row."""
    status = row.get("status")
    if status in (None, "pending", "running", "submitted"):
        return "PENDING", None

    err_obj = row.get("error") or {}
    err_msg = (
        err_obj.get("message", "")
        if isinstance(err_obj, dict)
        else str(err_obj)
    )
    started = row.get("started_at")
    ended = row.get("completed_at")
    wall_s = None
    if started and ended:
        try:
            wall_s = int(
                (datetime.fromisoformat(ended.replace("Z", "+00:00"))
                 - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
            )
        except Exception:
            pass

    if status == "succeeded":
        cap = PILOT_CAP_S.get(row.get("tool"), 0)
        if wall_s and cap and wall_s >= 0.8 * cap:
            return "SLOW_SUCCESS", f"wall={wall_s}s near cap={cap}s"
        return "FAST_SUCCESS", f"wall={wall_s}s under cap={PILOT_CAP_S.get(row.get('tool'))}s"

    # Failed paths — classify by error string.
    if "out of memory" in err_msg.lower() or "killed" in err_msg.lower():
        return "OOM", f"OOM after wall={wall_s}s"
    if "TimeoutExpired" in err_msg or "timeout" in err_msg.lower():
        return "TIMEOUT", f"timeout after wall={wall_s}s"
    if "AssertionError" in err_msg or "assert " in err_msg.lower():
        # Try to extract the first AssertionError line.
        for line in err_msg.split("\n"):
            if "AssertionError" in line:
                return "ASSERTION", line.strip()[:200]
        return "ASSERTION", "AssertionError in pipeline"
    return "OTHER", err_msg.split("\n", 1)[0][:200]


def main() -> int:
    if not DISPATCHED.exists():
        print(f"missing {DISPATCHED} — run dispatch first", file=sys.stderr)
        return 1
    dispatched = json.loads(DISPATCHED.read_text())
    ids = [j["job_id"] for j in dispatched["jobs"]]

    sb = get_service_client()
    rows = sb.table("tool_jobs").select(
        "id,tool,status,started_at,completed_at,error,"
        "gpu_seconds_used,credits_cost,modal_function_call_id"
    ).in_("id", ids).execute().data
    by_id = {r["id"]: r for r in rows}

    snapshot = {
        "polled_at": datetime.utcnow().isoformat() + "Z",
        "jobs": [],
    }
    terminal_count = 0
    for j in dispatched["jobs"]:
        row = by_id.get(j["job_id"])
        if not row:
            snapshot["jobs"].append({**j, "status": "NOT_FOUND"})
            continue
        mode, summary = _classify(row, j)
        if mode != "PENDING":
            terminal_count += 1
        snapshot["jobs"].append({
            "n": j["n"],
            "tool": j["tool"],
            "fixture": j["fixture"],
            "job_id": j["job_id"],
            "status": row["status"],
            "mode": mode,
            "summary": summary,
            "wall_s": (
                int((datetime.fromisoformat(row["completed_at"].replace("Z","+00:00"))
                     - datetime.fromisoformat(row["started_at"].replace("Z","+00:00"))).total_seconds())
                if row.get("started_at") and row.get("completed_at") else None
            ),
            "gpu_seconds_used": row.get("gpu_seconds_used"),
            "credits_cost": row.get("credits_cost"),
            "expected": j.get("expected"),
        })
    snapshot["terminal_count"] = terminal_count
    snapshot["total"] = len(dispatched["jobs"])

    RESULTS.write_text(json.dumps(snapshot, indent=2))
    # Pretty-print per-job line to stdout.
    print(f"{snapshot['polled_at']}  terminal={terminal_count}/{snapshot['total']}")
    for j in snapshot["jobs"]:
        wall = f"{j['wall_s']}s" if j["wall_s"] else "(running)"
        print(f"  [{j['tool']:12s}] {j['fixture'][:20]:20s} status={j['status']:10s} "
              f"mode={j['mode']:14s} wall={wall:10s} {j['summary'] or ''}")
    return 0 if terminal_count == snapshot["total"] else 2


if __name__ == "__main__":
    sys.exit(main())
