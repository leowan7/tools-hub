"""Capture a real completed run's ``result`` as a tool's worked example.

Phase 3 of the tools-hub redesign shows a new visitor what actually
comes back from a tool, by replaying one past ``job.result`` through
that tool's OWN results partial (components/worked_example.html). The
machinery is done; what each tool still needs is the payload, and a
payload has to come from a run that really happened.

    python scripts/capture_example_result.py boltzgen --list
    python scripts/capture_example_result.py boltzgen --job-id <uuid>

Writes ``tools/<slug>/example/result.json`` and prints the figures the
narration in ``tools/<slug>/meta.py`` must quote, so the prose is
written FROM the payload rather than beside it.

Two things this does not do, on purpose:

* It does not write the narration. Every number in an EXAMPLE is a
  recorded fact about one run; a generated sentence would be a guess
  wearing a fact's clothes.
* It does not decide the run is a good example. A completed run can
  still be a FAILED one -- proteina's only captured shard scored
  af2_iptm 0.09 across all eight designs -- and publishing that teaches
  the tool backwards. Read the summary this prints before shipping it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv()

# Keys dropped wherever they appear in the payload tree. A worked
# example is a PUBLIC page: anything naming the customer, their target,
# our provider job, or a storage location has no business on it. The
# list is deliberately broad -- a false positive costs one field in a
# demo, a false negative publishes a customer's identifier.
SENSITIVE_KEYS = frozenset({
    "user_id", "user_email", "email", "owner", "owner_id",
    "workspace_id", "campaign_id", "target_id", "project_id",
    "provider_job_id", "modal_function_call_id", "modal_call_id",
    "storage_path", "storage_key", "bucket", "signed_url", "url",
    "input_pdb_path", "source_path", "upload_name", "original_filename",
})


def scrub(obj, _path=""):
    """Strip SENSITIVE_KEYS recursively. Returns (clean, removed_paths)."""
    removed = []
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            here = (_path + "." + key) if _path else key
            if key in SENSITIVE_KEYS:
                removed.append(here)
                continue
            sub, sub_removed = scrub(val, here)
            out[key] = sub
            removed.extend(sub_removed)
        return out, removed
    if isinstance(obj, list):
        out = []
        for i, val in enumerate(obj):
            sub, sub_removed = scrub(val, _path + "[" + str(i) + "]")
            out.append(sub)
            removed.extend(sub_removed)
        return out, removed
    return obj, removed


def trim_structures(result, inline_pdb):
    """Handle the embedded PDBs, which dominate payload size.

    ``candidate_table`` links a design's .pdb from ``pdb_key`` (a
    storage object, which an example has none of) and falls back to an
    inline ``pdb_content_b64`` data: URI when there is no key. So for
    the first ``inline_pdb`` candidates we drop the key and KEEP the
    blob -- those downloads then genuinely work on the example page --
    and drop the blob everywhere else, because eight of them is 3.5 MB.
    """
    kept = 0
    rows = (result.get("candidates") or []) + (result.get("designs") or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        if kept < inline_pdb and row.get("pdb_content_b64"):
            row.pop("pdb_key", None)
            kept += 1
        else:
            row.pop("pdb_content_b64", None)
    return kept


def summarise(slug, result):
    """Print what the narration has to be consistent with."""
    print("\n--- figures for tools/" + slug + "/meta.py EXAMPLE ---")
    for key in ("tier", "status", "runtime_seconds", "gpu_seconds",
                "designs_total", "designs_completed", "n_failures"):
        if result.get(key) is not None:
            print("  {}: {}".format(key, result[key]))
    rows = result.get("candidates") or result.get("designs") or []
    if rows:
        print("  candidates: {}".format(len(rows)))
        scores = (rows[0] or {}).get("scores") or {}
        for name in sorted(scores):
            nums = [
                (r.get("scores") or {}).get(name) for r in rows
                if isinstance(r, dict)
            ]
            nums = [v for v in nums if isinstance(v, (int, float))]
            if nums:
                print("    {}: min {:.3g}  max {:.3g}".format(
                    name, min(nums), max(nums)))
    seqs = result.get("sequences") or []
    if seqs:
        print("  sequences: {}, length {}".format(
            len(seqs), len(seqs[0].get("seq") or "")))
    print("  ^ READ THESE. A completed run can still be a failed one.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug", help="tool slug, e.g. boltzgen")
    ap.add_argument("--list", action="store_true",
                    help="show recent succeeded jobs and exit")
    ap.add_argument("--job-id", help="the run to capture")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--inline-pdb", type=int, default=1, metavar="N",
                    help="keep the inline PDB for the top N designs so "
                         "their download works on the example page "
                         "(default 1; 0 keeps none)")
    args = ap.parse_args()

    from shared.credits import get_service_client

    sb = get_service_client()

    if args.list or not args.job_id:
        rows = (
            sb.table("tool_jobs")
            .select("id,tool,status,created_at,completed_at,gpu_seconds_used")
            .eq("tool", args.slug).eq("status", "succeeded")
            .order("created_at", desc=True).limit(args.limit)
            .execute().data
        )
        if not rows:
            print("no succeeded " + args.slug + " jobs found", file=sys.stderr)
            return 1
        print("recent succeeded " + args.slug + " runs (newest first):")
        for r in rows:
            print("  {}  {}  gpu_s={}".format(
                r["id"], r.get("completed_at") or r["created_at"],
                r.get("gpu_seconds_used")))
        print("\nre-run with --job-id <id> to capture one")
        return 0

    row = (
        sb.table("tool_jobs").select("id,tool,status,result")
        .eq("id", args.job_id).single().execute().data
    )
    if row.get("status") != "succeeded":
        print("job is {}, not succeeded".format(row.get("status")),
              file=sys.stderr)
        return 1
    if row.get("tool") != args.slug:
        print("job is a {} run, not {}".format(row.get("tool"), args.slug),
              file=sys.stderr)
        return 1
    result = row.get("result")
    if not isinstance(result, dict):
        print("job has no dict result", file=sys.stderr)
        return 1

    result, removed = scrub(result)
    kept = trim_structures(result, args.inline_pdb)

    dest = REPO_ROOT / "tools" / args.slug.replace("-", "_") / "example"
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "result.json"
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")

    print("wrote {} ({:.0f} KB)".format(
        out.relative_to(REPO_ROOT), out.stat().st_size / 1024))
    print("scrubbed {} sensitive field(s){}".format(
        len(removed),
        (": " + str(sorted(set(removed))[:8])) if removed else ""))
    print("inline PDBs kept: {}".format(kept))
    summarise(args.slug, result)
    print("\nnext: write EXAMPLE in tools/" + args.slug + "/meta.py, then run")
    print("  pytest tests/test_worked_examples.py -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
