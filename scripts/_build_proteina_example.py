"""Re-derive tools/proteina/example/result.json from the private sweep shard.

Kept in the repo for the same reason as ``_build_pxdesign_example.py``: the
numbers narrated in ``tools/proteina/meta.py`` are checkable rather than
trusted. The source campaign is not public, so this does not run in CI — it
is the record of how the payload was produced.

SOURCE. One shard of the tier-2 length sweep: 64 designs from a single
``ranomics-proteina-prod`` call (job ``proteina-sweep-60-69-r14-...``, 3447
GPU-seconds). One shard is one job, so the payload is the shape a single
submission returns — not an aggregate across the campaign's 46 shards.

WHAT IS DROPPED. Sequences, PDB paths and the campaign's job/call ids. The
results table renders none of them, and the published rule for these examples
is scores only.

WHAT IS KEPT AS-IS. ``rf3_score`` and ``cluster_id`` are absent from every
row because the run set ``rf3_required: False`` — the template renders an
absent metric as an em dash, which is the honest output of this preset, not a
gap in the capture.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

SWEEP = Path.home() / "proteina-sweep-tier2"
SHARD = "28"
OUT = Path(__file__).resolve().parent.parent / "tools" / "proteina" / "example" / "result.json"

# Columns templates/tools/proteina_results.html renders that this preset
# actually produces. rf3_score / cluster_id are deliberately not written.
SCORE_COLUMNS = ("total_reward", "af2_iptm", "af2_plddt", "binder_scrmsd")


def _num(raw: str) -> float | None:
    return float(raw) if raw not in ("", "NA", "None") else None


def main() -> None:
    rows = [
        r
        for r in csv.DictReader((SWEEP / "export" / "designs.csv").open())
        if r["shard_index"] == SHARD
    ]
    # The pipeline sorts by total_reward descending (run_pipeline.py) — rank in
    # the payload has to be the rank an operator would actually see.
    rows.sort(key=lambda r: _num(r["total_reward"]) or 0.0, reverse=True)

    candidates = [
        {
            "rank": i + 1,
            "name": r["name"],
            "scores": {c: _num(r[c]) for c in SCORE_COLUMNS},
        }
        for i, r in enumerate(rows)
    ]
    payload = {"candidates": candidates, "gpu_seconds": 3447}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")

    # Figures the narration quotes, printed so a reviewer can diff them
    # against meta.py without re-reading the campaign.
    passed = [r for r in rows if r["passes"] == "1"]
    self_copy = [r for r in rows if r["self_copy"] == "1"]
    print(f"designs      {len(rows)}")
    print(f"passed       {len(passed)}")
    print(f"refold <5A   {sum(1 for r in rows if (_num(r['binder_scrmsd']) or 0) < 5.0)}")
    print(f"self-copies  {len(self_copy)}  ranks {[i+1 for i, r in enumerate(rows) if r['self_copy']=='1']}")
    print(f"self scrmsd  {min(_num(r['binder_scrmsd']) for r in self_copy):.1f} - "
          f"{max(_num(r['binder_scrmsd']) for r in self_copy):.1f} A")
    print(f"top          iptm {_num(rows[0]['af2_iptm']):.4f} plddt {_num(rows[0]['af2_plddt']):.4f} "
          f"scrmsd {_num(rows[0]['binder_scrmsd']):.4f} reward {_num(rows[0]['total_reward']):.4f}")
    print(f"bytes        {OUT.stat().st_size}")


if __name__ == "__main__":
    main()
