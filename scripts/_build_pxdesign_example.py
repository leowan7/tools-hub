"""One-off: turn the campaign's 100-design PXDesign round into an example payload.

Kept in-tree so the payload can be re-derived and re-checked rather than
trusted. Reads the private campaign result, emits ONLY scores -- no
designed sequences, no structures, no target identifiers.
"""
import json
from pathlib import Path

SRC = Path.home() / "Documents/Claude_projects/boltzgen-workspace/aglyco-fc-vhh/results/cu43_pxdesign_round.json"
DEST = Path(__file__).resolve().parents[1] / "tools/pxdesign/example/result.json"

round_ = json.loads(SRC.read_text(encoding="utf-8"))
calls = round_["calls"]
# ONE call, not the pooled round: ``job.result`` is per submission, and a
# pilot-tier call returns ~25 designs. Pooling all four would render a
# payload shape no single job produces. The 63-residue call is the one
# carrying the round's best design.
CALL = "63"
call = next(c for c in calls if str(c["binder_len"]) == CALL)
designs = [d for d in round_["designs"] if d["call_key"] == call["key"]]

# The campaign ran a third filter (a target-specific clash check) that is
# meaningless off that target, so it is dropped here: a design passes if
# PXDesign's own AF2-IG re-fold agrees with the generator (ipTM >= 0.50 AND
# complex RMSD < 3.0 A) and the interface is big enough (BSA >= 514 A^2).
rows = []
for d in sorted(designs, key=lambda x: -(x.get("iptm") or 0)):
    passed = bool(d.get("pass_af2")) and bool(d.get("pass_geom"))
    rows.append({
        "rank": len(rows) + 1,
        "scores": {
            # Rounded to the precision the live tool emits, not finer.
            "ipTM": round(float(d["iptm"]), 2),
            "pLDDT": round(float(d["plddt"]), 1),
            "pAE": round(float(d["ipae"]), 2),
            "filter_status": "pass" if passed else "below threshold",
        },
    })

payload = {
    "total_designs": len(rows),
    "candidate_count": len(rows),
    "runtime_minutes": round(call["gpu_seconds"] / 60.0, 1),
    "candidates": rows,
}
DEST.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

n_pass = sum(1 for r in rows if r["scores"]["filter_status"] == "pass")
print("wrote %s (%.1f KB)" % (DEST.name, DEST.stat().st_size / 1024))
print("  designs: %d, pass: %d" % (len(rows), n_pass))
print("  this call: len=%s  $%.2f  %d gpu-seconds" % (
    call["binder_len"], call["cost"], call["gpu_seconds"]))
print("  whole round, for the narration only: %d designs, passes by length %s" % (
    len(round_["designs"]),
    ", ".join("%s:%d" % (c["binder_len"], sum(
        1 for d in round_["designs"]
        if d["call_key"] == c["key"] and d.get("pass_af2") and d.get("pass_geom")
    )) for c in calls)))
