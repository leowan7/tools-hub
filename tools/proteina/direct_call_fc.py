"""Invoke Proteina-Complexa directly on Modal and read designs from the return
value — no tools-hub web tier, no upload endpoint, no job_token.

WHY THIS EXISTS. The web tier cannot express a multi-chain target or
chain-prefixed hotspots, which is exactly what an IgG1 Fc campaign needs: two
protomers, eight hotspots each. RFdiffusion, PXDesign and BindCraft are already
driven this way. Proteina refused to run without an ``upload_urls_endpoint``
because its per-design loop called back out to tools-hub to mint a presigned
PUT per PDB; it now inlines the coordinates as ``pdb_content_b64`` instead.

    python tools/proteina/direct_call_fc.py --dry-run   # payload only, free
    python tools/proteina/direct_call_fc.py --validate  # free, but see below
    python tools/proteina/direct_call_fc.py --submit    # SPENDS GPU MONEY
    python tools/proteina/direct_call_fc.py --collect   # read the result back

WHAT ``--validate`` DOES NOT DO. ``run_validate`` returns before the
target-source invariant, before ``prepare_custom_target`` and before any
hotspot matching: it checks package import, config files and checkpoint
presence, and IGNORES ``target_chain``, the hotspots and the staged PDB
entirely. It is an environment check, NOT a preflight of this job spec — it
will report OK for a spec that would mis-aim on ``--submit``. The job-spec
preflight that matters is offline and free: see
``tests/test_proteina_delivery.py`` and the guards in
``run_pipeline.prepare_custom_target`` (``missing_hotspots`` /
``hotspots_outside_contig``).

CONTRACT QUIRKS THIS SCRIPT ENCODES (Proteina's job_spec vocabulary differs
from the other three tools, and every one of these is a silent-failure risk):
  * presets are protein_binder / ligand_binder / motif_ame / validate — there
    is no smoke or mini_pilot tier here.
  * ``binder_length`` is a [lo, hi] pair at job_spec TOP LEVEL. ``parameters``
    is never read at all.
  * ``target_source`` must be set to "custom" EXPLICITLY. It defaults to
    "curated", and the code deliberately refuses to infer a custom target from
    a URL being present — falling through to a repo-bundled benchmark target
    would design against the wrong structure and look successful.
  * custom targets are accepted on protein_binder ONLY (_CUSTOM_TARGET_PRESETS).
  * the target file arrives via ``input_presigned_url``.
  * hotspots are ``hotspot_spec`` natively; ``hotspot_residues`` is now
    accepted as an alias, as is a comma-separated ``target_chain``.
  * residue numbers are EU/author numbering from the uploaded file. Never
    pre-convert them — upstream matches f"{chain_id}{res_id}" against the file
    as given, and a token matching nothing is dropped SILENTLY, leaving the
    search unconstrained and the output indistinguishable from a correct run.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

TOOLS_HUB = Path(__file__).resolve().parents[2]
TARGET = Path(
    r"C:\Users\lab\Documents\Claude_projects\boltzgen-workspace"
    r"\aglyco-fc-vhh\inputs\3ave_target_AB.pdb"
)
APP = "ranomics-proteina-prod"
FN = "run_tool"
# EU/author numbering, both protomers. NOT pre-converted.
HOTSPOTS = [
    "A241", "A243", "A244", "A246", "A260", "A262", "A264", "A301",
    "B241", "B243", "B244", "B246", "B260", "B262", "B264", "B301",
]
# NOT inside the tracked tools/proteina/ tree: this is per-run scratch keyed to
# one Modal call id, and dropping it next to the source makes it a candidate
# for an accidental commit. Overridable for anyone who wants it elsewhere.
STATE = Path(
    os.environ.get("PROTEINA_DIRECT_STATE")
    or Path(__file__).resolve().parents[2] / ".proteina_direct_call_state.json"
)


def _load_env_and_path() -> None:
    """Put tools-hub on sys.path and load its .env (SUPABASE_* for storage)."""
    sys.path.insert(0, str(TOOLS_HUB))
    envf = TOOLS_HUB / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_job_spec(*, preset: str, nsamples: int, replicas: int) -> dict:
    return {
        "preset": preset,
        "config_name": "search_binder_local_pipeline",
        # Empty, and that is load-bearing: a custom run carrying a curated
        # task_name is refused pre-GPU as target_conflict.
        "task_name": "",
        "target_source": "custom",
        "target_chain": "A,B",
        "hotspot_residues": HOTSPOTS,
        "binder_length": [60, 120],
        "rf3_required": False,
        "nsamples": nsamples,
        "replicas": replicas,
    }


def build_payload(url: str, *, preset: str, nsamples: int, replicas: int,
                  job_id: str) -> dict:
    """NOTE the absence of upload_urls_endpoint and job_token. That absence is
    the whole point: it selects INLINE delivery in run_pipeline.main()."""
    return {
        "job_spec": build_job_spec(
            preset=preset, nsamples=nsamples, replicas=replicas),
        "input_presigned_url": url,
        "tier": preset,
        "job_tier": preset,
        "job_id": job_id,
    }


def _stage_target(job_id: str) -> str:
    from shared.storage import presigned_input_url, upload_input
    data = TARGET.read_bytes()
    path = upload_input(
        user_id="aglyco-fc-campaign", job_id=job_id,
        filename="3ave_target_AB.pdb", data=data,
        content_type="chemical/x-pdb",
    )
    url = presigned_input_url(path, expires_seconds=7200)
    print(f"[stage] {TARGET.name} ({len(data)} bytes) -> {path}")
    return url


def cmd_dry_run(args) -> int:
    payload = build_payload(
        "https://example/PRESIGNED", preset=args.preset,
        nsamples=args.nsamples, replicas=args.replicas, job_id="dry-run")
    print(json.dumps(payload, indent=2))
    print(f"\ndesigns/shard = nsamples*replicas = "
          f"{args.nsamples * args.replicas}")
    print("upload_urls_endpoint present:",
          "upload_urls_endpoint" in payload, "(must be False)")
    return 0


def cmd_validate(args) -> int:
    """The validate preset is FREE and short-circuits before any GPU work."""
    _load_env_and_path()
    import modal
    job_id = "proteina-direct-validate"
    payload = build_payload(
        _stage_target(job_id), preset="validate",
        nsamples=args.nsamples, replicas=args.replicas, job_id=job_id)
    fn = modal.Function.from_name(APP, FN)
    print(f"[validate] calling {APP}/{FN} (free, CPU-only)")
    out = fn.remote(payload)
    print(json.dumps(out, indent=2, default=str)[:4000])
    return 0


def cmd_submit(args) -> int:
    _load_env_and_path()
    import modal
    job_id = args.job_id
    payload = build_payload(
        _stage_target(job_id), preset=args.preset,
        nsamples=args.nsamples, replicas=args.replicas, job_id=job_id)
    fn = modal.Function.from_name(APP, FN)
    call = fn.spawn(payload)
    STATE.write_text(json.dumps(
        {"call_id": call.object_id, "job_id": job_id,
         "job_spec": payload["job_spec"]}, indent=2))
    print(f"[submit] {job_id} -> {call.object_id}")
    print(f"[submit] state saved to {STATE}; run --collect when it finishes")
    return 0


def cmd_collect(args) -> int:
    _load_env_and_path()
    import modal
    if not STATE.exists():
        print(f"no state at {STATE}; run --submit first", file=sys.stderr)
        return 2
    state = json.loads(STATE.read_text())
    call = modal.FunctionCall.from_id(state["call_id"])
    out = call.get(timeout=args.timeout)

    smoke = (out or {}).get("smoke_result") or {}
    cands = smoke.get("candidates") or []
    print(f"exit_code       : {(out or {}).get('exit_code')}")
    print(f"status          : {smoke.get('status')}")
    print(f"designs         : {smoke.get('designs_completed')}"
          f"/{smoke.get('designs_total')}")
    print(f"runtime_seconds : {smoke.get('runtime_seconds')}")
    print(f"raw archive     : {(out or {}).get('raw_tgz_volume_path')}")

    with_atoms = [c for c in cands if c.get("pdb_content_b64")]
    print(f"candidates      : {len(cands)} ({len(with_atoms)} with coordinates)")
    if not with_atoms:
        print("\nNO COORDINATES INLINE — the goal is NOT met.", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for c in with_atoms:
        pdb = base64.b64decode(c["pdb_content_b64"])
        dest = outdir / f"design_{c['rank']:03d}.pdb"
        dest.write_bytes(pdb)
        scores = c.get("scores") or {}
        print(f"  rank {c['rank']:>3}  reward={scores.get('total_reward')}  "
              f"plddt={scores.get('af2_plddt')}  {len(pdb)} bytes -> {dest}")
    (outdir / "smoke_result.json").write_text(json.dumps(smoke, indent=2))
    print(f"\nwrote {len(with_atoms)} PDB(s) + smoke_result.json to {outdir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--preset", default="protein_binder")
    ap.add_argument("--nsamples", type=int, default=4)
    ap.add_argument("--replicas", type=int, default=2)
    ap.add_argument("--job-id", default="proteina-direct-fc-01")
    ap.add_argument("--timeout", type=int, default=7200)
    ap.add_argument("--outdir", default="proteina_direct_out")
    args = ap.parse_args()

    if args.dry_run:
        return cmd_dry_run(args)
    if args.validate:
        return cmd_validate(args)
    if args.submit:
        return cmd_submit(args)
    if args.collect:
        return cmd_collect(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
