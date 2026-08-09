"""Sequential multi-shard driver for a Proteina-Complexa binder-length sweep.

Runs an unattended campaign of many shards through the SAME production path
``direct_call_fc.py`` uses — ``ranomics-proteina-prod/run_tool``, no upload
endpoint, coordinates delivered inline — and lands every design in one
filterable manifest.

    python tools/proteina/shard_driver.py --dry-run        # free, plans only
    python tools/proteina/shard_driver.py --run --budget 300

THE ONE FACT THE DESIGN FOLLOWS FROM. A Modal call id is the ONLY handle to a
paid run. Lose it and the designs it produced are unreachable: they are not in
Storage (there is no upload endpoint on this path) and re-running spends
another A100 for a different seed. So the ledger line naming a call id is
written BEFORE the driver starts waiting on it, never after. Every other
crash-safety property here is a consequence of that ordering.

WHY IT IS STRICTLY SEQUENTIAL, and why that is not a limitation to be lifted
by raising a constant. ``fn.spawn`` does not queue against a fixed pool — each
spawned call gets its OWN container, and ``run_tool`` is unconditionally
``gpu="A100-80GB"``. Two in flight is therefore two A100s and twice the burn
rate, which is exactly the thing the "one GPU" constraint forbids. The limit is
enforced here rather than left as a tunable because nothing downstream would
catch the violation — Modal would happily allocate the second GPU and bill it.
If more GPUs genuinely become available, the change is a bounded in-flight
window around ``_run_shard``'s spawn/collect split, not a constant.

WHAT IT DELIBERATELY DOES NOT DO.
  * No automatic retry. A retry needs a FRESH job_id — the shard seed is
    ``sha256(job_id) % 1_000_000`` and the raw archive is keyed on it, so
    re-submitting the same id re-runs an identical search and destroys the
    first run's archive. A "retry" is therefore a new shard, which is a
    judgement call for an operator reading the log, not something to do
    automatically at 3am.
  * No resume of a shard that FAILED. Only ``submitted``-but-uncollected calls
    are reconnected, because those are paid work still in flight.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.proteina.direct_call_fc import (  # noqa: E402
    DEFAULT_TARGET, APP, FN, _load_env_and_path, _stage_target, build_payload,
)

# --- campaign shape ---------------------------------------------------------
# Five 10-aa bins over the locked 50-100 window. Round-robin across them (see
# build_plan) so the sample stays BALANCED at every point in the run.
BINS: list[tuple[int, int]] = [(50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]
SHARDS_PER_BIN = 16
# 64 designs/shard. nsamples draws LENGTHS (the upstream flag is
# generation.dataloader.dataset.nres.nsamples) and replicas gives independent
# designs at each drawn length — measured on job
# proteina-direct-fc-20260809-091702-68025f, where nsamples=4/replicas=2 gave
# exactly 4 lengths x 2 designs. 16x4 covers a 10-aa bin's integer lengths with
# 4 replicates each, so a per-LENGTH hit rate is estimable, not just per-bin.
NSAMPLES = 16
REPLICAS = 4
DESIGNS_PER_SHARD = NSAMPLES * REPLICAS

# Validated bounds from tools/proteina/__init__.py. Checked here because the
# DIRECT path does no bounds validation at all — run_pipeline int-converts the
# pair and hands it to `complexa target add`, so a bad range reaches the GPU.
BINDER_LEN_MIN, BINDER_LEN_MAX = 20, 300

# --- cost model -------------------------------------------------------------
# Fitted to the one metered run: 8 designs, 673 s pipeline, $0.5528 charged.
# Stage split from that container's log (generate 343.6 + evaluate 274.6 scale
# with design count; filter 9.8 + analyze 31.1 + staging/parse do not).
#
# The RATE is inferred, not published: $0.5528 over a run whose pipeline alone
# took 673 s puts an upper bound of $2.96/hr on the true rate, and $2.50/hr
# leaves a 123 s container overhead (cold start, three volume mounts, archive
# tar, two volume commits) which is the plausible figure. The repo's own
# $3.70/hr card (shared/wallet.py) is arithmetically impossible here — it
# implies 538 billed seconds for a 673 s pipeline.
USD_PER_SECOND = 2.50 / 3600.0
SECONDS_PER_DESIGN = (343.6 + 274.6) / 8.0            # 77.28, scales with N
SECONDS_IN_PIPELINE_FIXED = 673.0 - (343.6 + 274.6)   # 54.8, filter+analyze+parse
# Billed but OUTSIDE the pipeline's own clock: cold start, three volume mounts,
# the archive tar and two volume commits. Named rather than inlined because the
# deadline check below needs to SUBTRACT it — the 6780s subprocess deadline
# applies to the pipeline, not to the container.
CONTAINER_OVERHEAD_S = 123.0
SECONDS_FIXED_PER_SHARD = SECONDS_IN_PIPELINE_FIXED + CONTAINER_OVERHEAD_S
# run_tool's own ceiling. A shard whose pipeline would exceed the subprocess
# deadline is refused at plan time rather than discovered 113 minutes in.
DESIGN_SUBPROCESS_DEADLINE_S = 6780


# ``None`` rather than ``DESIGNS_PER_SHARD`` as the default: a default
# expression is evaluated once at import, so the module constant would be
# frozen into the signature and any later change to NSAMPLES/REPLICAS would be
# silently ignored by exactly the guard that exists to catch it.
def shard_seconds(n_designs: int | None = None) -> float:
    n = DESIGNS_PER_SHARD if n_designs is None else n_designs
    return SECONDS_FIXED_PER_SHARD + SECONDS_PER_DESIGN * n


def shard_usd(n_designs: int | None = None) -> float:
    return shard_seconds(n_designs) * USD_PER_SECOND


# --- plan -------------------------------------------------------------------

def build_plan(bins=BINS, per_bin=SHARDS_PER_BIN) -> list[dict]:
    """Round-robin, NOT bin-by-bin.

    Interleaving means an interrupted campaign still has equal n per bin, so
    the length comparison is valid at every moment rather than only at the end
    — and the even-versus-adaptive allocation choice can be deferred until
    there is data to make it with.

    ``index`` is the stable ledger key: it is a pure function of (bins,
    per_bin), so a resume with the same configuration reproduces the same
    numbering. Changing BINS or SHARDS_PER_BIN mid-campaign renumbers the plan
    and invalidates the ledger; _verify_plan_matches_ledger refuses that.
    """
    plan = []
    for round_index in range(per_bin):
        for lo, hi in bins:
            plan.append({
                "index": len(plan),
                "round": round_index,
                "bin": [int(lo), int(hi)],
            })
    return plan


def _validate_plan(plan: list[dict]) -> None:
    """Fail before spending, not during."""
    if not plan:
        raise SystemExit("empty plan: check BINS / SHARDS_PER_BIN")
    pipeline_s = shard_seconds(DESIGNS_PER_SHARD) - CONTAINER_OVERHEAD_S
    if pipeline_s >= DESIGN_SUBPROCESS_DEADLINE_S:
        raise SystemExit(
            f"{DESIGNS_PER_SHARD} designs/shard projects to {pipeline_s:.0f}s "
            f"of pipeline, at or past the {DESIGN_SUBPROCESS_DEADLINE_S}s "
            "subprocess deadline. Lower NSAMPLES*REPLICAS.")
    for item in plan:
        lo, hi = item["bin"]
        if not (isinstance(lo, int) and isinstance(hi, int)):
            raise SystemExit(f"bin {item['bin']} is not a pair of ints")
        if lo > hi:
            raise SystemExit(f"bin {item['bin']} has lo > hi")
        if lo < BINDER_LEN_MIN or hi > BINDER_LEN_MAX:
            raise SystemExit(
                f"bin {item['bin']} is outside the validated "
                f"{BINDER_LEN_MIN}-{BINDER_LEN_MAX} residue range")


# --- ledger -----------------------------------------------------------------
# Append-only JSONL, one line per state transition. Append-only rather than a
# rewritten state document because a partial write during a crash must not be
# able to corrupt what came before it.

def ledger_append(path: Path, record: dict) -> None:
    record = {**record, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def ledger_replay(path: Path) -> dict[int, dict]:
    """Latest record per shard index. A truncated final line (killed
    mid-write) is skipped rather than fatal — that is the whole reason the
    format is one self-contained JSON object per line."""
    latest: dict[int, dict] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            print(f"[ledger] skipping unparsable line: {line[:80]!r}",
                  file=sys.stderr)
            continue
        if "index" in rec:
            latest[int(rec["index"])] = {**latest.get(int(rec["index"]), {}),
                                         **rec}
    return latest


def _verify_plan_matches_ledger(plan: list[dict], state: dict[int, dict]) -> None:
    """A resume whose BINS changed would attach old call ids to new bins and
    silently mislabel every design that came back. Refuse instead."""
    for index, rec in state.items():
        if index >= len(plan):
            raise SystemExit(
                f"ledger holds shard {index} but the current plan has only "
                f"{len(plan)} shards. BINS/SHARDS_PER_BIN changed since this "
                "campaign started; use a fresh --outdir.")
        want = plan[index]["bin"]
        got = rec.get("bin")
        if got is not None and list(got) != list(want):
            raise SystemExit(
                f"shard {index} was run with bin {got} but the current plan "
                f"says {want}. BINS changed; use a fresh --outdir.")


# --- results ----------------------------------------------------------------

MANIFEST_COLUMNS = [
    "shard_index", "round", "bin_lo", "bin_hi", "job_id", "call_id",
    "rank", "name", "binder_length", "total_reward", "af2_iptm", "af2_plddt",
    "binder_scrmsd", "cluster_id", "pdb_file",
]


def _binder_length(pdb_bytes: bytes) -> int:
    """CA count of chain C — the ACTUAL sampled length.

    The bin is what was REQUESTED; upstream samples a length per design inside
    it. A length study needs the realised value, so it is measured off the
    coordinates rather than assumed from the bin.
    """
    n = 0
    for line in pdb_bytes.decode("ascii", "replace").splitlines():
        if line.startswith("ATOM") and line[21:22] == "C" \
                and line[12:16].strip() == "CA":
            n += 1
    return n


def _write_manifest_rows(manifest: Path, rows: list[dict]) -> None:
    """Appended per shard so a crash keeps every row already earned."""
    new = not manifest.exists()
    with manifest.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerows(rows)


def _harvest(out: dict, item: dict, job_id: str, call_id: str,
             outdir: Path) -> tuple[list[dict], int]:
    """Write this shard's PDBs and return its manifest rows.

    A candidate with no ``pdb_content_b64`` (dropped by the inline size cap)
    still gets a row — its A100-computed scores are real and the run is the
    only place they exist. Only ``pdb_file`` is left empty.
    """
    smoke = (out or {}).get("smoke_result") or {}
    shard_dir = outdir / f"shard_{item['index']:03d}"
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "smoke_result.json").write_text(
        json.dumps(smoke, indent=2), encoding="utf-8")

    rows, with_atoms = [], 0
    for cand in (smoke.get("candidates") or []):
        scores = cand.get("scores") or {}
        blob = cand.get("pdb_content_b64")
        pdb_file, length = "", ""
        if blob:
            raw = base64.b64decode(blob)
            dest = shard_dir / f"design_{cand.get('rank', 0):03d}.pdb"
            dest.write_bytes(raw)
            pdb_file = str(dest.relative_to(outdir))
            length = _binder_length(raw)
            with_atoms += 1
        rows.append({
            "shard_index": item["index"], "round": item["round"],
            "bin_lo": item["bin"][0], "bin_hi": item["bin"][1],
            "job_id": job_id, "call_id": call_id,
            "rank": cand.get("rank"), "name": cand.get("name"),
            "binder_length": length,
            "total_reward": scores.get("total_reward"),
            "af2_iptm": scores.get("af2_iptm"),
            "af2_plddt": scores.get("af2_plddt"),
            "binder_scrmsd": scores.get("binder_scrmsd"),
            "cluster_id": scores.get("cluster_id"),
            "pdb_file": pdb_file,
        })
    return rows, with_atoms


# --- the loop ---------------------------------------------------------------

def _spent_usd(state: dict[int, dict]) -> float:
    """Every shard that reached ``submitted`` was billed, whether or not it
    then produced designs. Counting only successes would let a run of failures
    walk straight through the budget ceiling."""
    return sum(shard_usd() for rec in state.values()
               if rec.get("state") in {"submitted", "collected", "failed",
                                       "empty"})


def _collect(call, item, job_id, call_id, outdir, manifest, ledger,
             timeout: int) -> str:
    """Block on one call, land its results, and record the outcome."""
    try:
        out = call.get(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — one shard must not kill the run
        print(f"[shard {item['index']:03d}] collect FAILED: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        ledger_append(ledger, {"index": item["index"], "state": "failed",
                               "error": f"{type(exc).__name__}: {exc}"})
        return "failed"

    rows, with_atoms = _harvest(out, item, job_id, call_id, outdir)
    if rows:
        _write_manifest_rows(manifest, rows)
    smoke = (out or {}).get("smoke_result") or {}
    state = "collected" if with_atoms else "empty"
    ledger_append(ledger, {
        "index": item["index"], "state": state,
        "exit_code": (out or {}).get("exit_code"),
        "status": smoke.get("status"),
        "designs": len(rows), "with_atoms": with_atoms,
        "runtime_seconds": smoke.get("runtime_seconds"),
    })
    print(f"[shard {item['index']:03d}] {state}: {with_atoms}/{len(rows)} "
          f"with coordinates, {smoke.get('runtime_seconds')}s, "
          f"status={smoke.get('status')}")
    return state


def run_campaign(args) -> int:
    plan = build_plan()
    _validate_plan(plan)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ledger = outdir / "ledger.jsonl"
    manifest = outdir / "manifest.csv"

    state = ledger_replay(ledger)
    _verify_plan_matches_ledger(plan, state)

    target = Path(args.target or os.environ.get("PROTEINA_TARGET_PDB")
                  or DEFAULT_TARGET)
    if not target.is_file():
        raise SystemExit(f"target structure not found: {target}")

    _load_env_and_path()
    import modal

    fn = modal.Function.from_name(APP, FN)
    done = {i for i, r in state.items()
            if r.get("state") in {"collected", "empty", "failed"}}
    print(f"[plan] {len(plan)} shards x {DESIGNS_PER_SHARD} designs = "
          f"{len(plan) * DESIGNS_PER_SHARD}; {len(done)} already finished")
    print(f"[budget] ${_spent_usd(state):.2f} spent, ceiling ${args.budget:.2f}")

    for item in plan:
        index = item["index"]
        prior = state.get(index, {})

        if prior.get("state") in {"collected", "empty", "failed"}:
            continue

        # PAID WORK ALREADY IN FLIGHT. Reconnect rather than resubmit: the
        # container is running (or has finished) and its call id is the only
        # way to reach the designs it produced.
        if prior.get("state") == "submitted" and prior.get("call_id"):
            print(f"[shard {index:03d}] reconnecting to in-flight "
                  f"{prior['call_id']}")
            _collect(modal.FunctionCall.from_id(prior["call_id"]), item,
                     prior.get("job_id", ""), prior["call_id"], outdir,
                     manifest, ledger, args.timeout)
            state = ledger_replay(ledger)
            continue

        spent = _spent_usd(state)
        if spent + shard_usd() > args.budget:
            print(f"[budget] STOP before shard {index}: ${spent:.2f} spent, "
                  f"next shard ~${shard_usd():.2f}, ceiling "
                  f"${args.budget:.2f}", file=sys.stderr)
            return 3

        lo, hi = item["bin"]
        # Fresh id per shard, always: the seed is derived from it, so a reused
        # id re-runs an identical search AND overwrites that run's raw archive.
        job_id = (f"proteina-sweep-{lo}-{hi}-r{item['round']:02d}-"
                  f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
        # Re-staged per shard. presigned_input_url expires in 7200 s and a
        # shard runs ~85 min, so one URL cannot safely cover the next shard.
        url = _stage_target(job_id, target)
        payload = build_payload(
            url, preset=args.preset, nsamples=NSAMPLES, replicas=REPLICAS,
            job_id=job_id, binder_length=(lo, hi))

        call = fn.spawn(payload)
        # BEFORE waiting. See the module docstring.
        ledger_append(ledger, {
            "index": index, "round": item["round"], "bin": item["bin"],
            "state": "submitted", "job_id": job_id,
            "call_id": call.object_id,
        })
        print(f"[shard {index:03d}] bin {lo}-{hi} -> {call.object_id}")

        _collect(call, item, job_id, call.object_id, outdir, manifest,
                 ledger, args.timeout)
        state = ledger_replay(ledger)

    final = ledger_replay(ledger)
    ok = sum(1 for r in final.values() if r.get("state") == "collected")
    print(f"\n[done] {ok}/{len(plan)} shards delivered designs; "
          f"~${_spent_usd(final):.2f} spent; manifest at {manifest}")
    return 0


def cmd_dry_run(args) -> int:
    """Never contacts Modal. Prints the plan, the projection and one payload."""
    plan = build_plan()
    _validate_plan(plan)
    total = len(plan) * DESIGNS_PER_SHARD
    secs = len(plan) * shard_seconds()
    print(f"bins            : {BINS}")
    print(f"shards/bin      : {SHARDS_PER_BIN}")
    print(f"designs/shard   : {DESIGNS_PER_SHARD} "
          f"(nsamples {NSAMPLES} x replicas {REPLICAS})")
    print(f"shards          : {len(plan)}")
    print(f"designs         : {total}")
    print(f"per shard       : {shard_seconds() / 60:.0f} min, "
          f"${shard_usd():.2f}")
    print(f"projected total : {secs / 3600:.1f} h ({secs / 86400:.2f} d), "
          f"${secs * USD_PER_SECOND:.2f}")
    print(f"budget ceiling  : ${args.budget:.2f}"
          + ("  <-- BELOW PROJECTION, the run will stop early"
             if args.budget < secs * USD_PER_SECOND else ""))
    print("\nfirst 6 shards (round-robin keeps bins balanced):")
    for item in plan[:6]:
        print(f"  {item['index']:03d}  round {item['round']}  "
              f"bin {item['bin'][0]}-{item['bin'][1]}")
    lo, hi = plan[0]["bin"]
    payload = build_payload(
        "https://example/PRESIGNED", preset=args.preset, nsamples=NSAMPLES,
        replicas=REPLICAS, job_id="dry-run", binder_length=(lo, hi))
    print("\npayload for shard 000:")
    print(json.dumps(payload["job_spec"], indent=2))
    print("upload_urls_endpoint present:",
          "upload_urls_endpoint" in payload, "(must be False)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="plan and cost only; never contacts Modal")
    ap.add_argument("--run", action="store_true", help="SPENDS GPU MONEY")
    ap.add_argument("--budget", type=float, default=300.0,
                    help="hard USD ceiling; refuses to start a shard that "
                         "would cross it (default 300)")
    ap.add_argument("--outdir", default="proteina_sweep_out")
    ap.add_argument("--preset", default="protein_binder")
    ap.add_argument("--target", default="")
    ap.add_argument("--timeout", type=int, default=7500,
                    help="seconds to wait on one shard; must exceed the "
                         "7200s container ceiling")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.dry_run:
        return cmd_dry_run(args)
    if args.run:
        return run_campaign(args)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
