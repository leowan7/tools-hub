"""Invoke Proteina-Complexa directly on Modal and read designs from the return
value — no tools-hub web tier, no upload endpoint, no job_token.

WHY THIS EXISTS. A direct caller has no tools-hub server to receive an upload
callback and no ``job_token`` to authenticate one. Proteina refused to run
without an ``upload_urls_endpoint`` because its per-design loop called back out
to tools-hub to mint a presigned PUT per PDB; it now inlines the coordinates as
``pdb_content_b64`` instead. RFdiffusion, PXDesign and BindCraft are already
driven this way.

NOT the reason, though this docstring used to say so: "the web tier cannot
express a multi-chain target or chain-prefixed hotspots". It can — ``tools/
proteina/validate`` accepts ``A236-443,B236-443`` and accepts ``A264 B264``
verbatim — and believing otherwise is what left the web form's bare-hotspot
promotion unguarded. See the note in ``run_pipeline.normalize_hotspots``.

    python tools/proteina/direct_call_fc.py --dry-run   # offline, genuinely free
    python tools/proteina/direct_call_fc.py --validate  # SPENDS GPU MONEY (see below)
    python tools/proteina/direct_call_fc.py --submit    # SPENDS GPU MONEY
    python tools/proteina/direct_call_fc.py --collect   # read the result back

WHAT ``--validate`` COSTS. Not nothing, though this docstring used to say
"free, CPU-only" and the banner used to print it. ``--validate`` calls the same
``ranomics-proteina-prod/run_tool`` as ``--submit``, and that is the ONLY
``@app.function`` in modal_app.py — declared ``gpu=_GPU`` with ``_GPU =
"A100-80GB"``, unconditionally, with no preset branch and no CPU-only sibling.
``run_validate`` short-circuits before GPU *work*, which is not the same as the
container being CPU-only: the accelerator is attached for the whole lifetime
and Modal bills wall-clock, so an operator pays A100-80GB seconds for the cold
image pull, three Volume mounts and the checks themselves. Cheap, not free, and
the inverted claim invited exactly the "just validate it first, it costs
nothing" habit it should have discouraged.

"Free validate" IS true one layer up and that is where the phrase belongs: the
tools-hub WALLET does not bill the validate preset (``tools/proteina/__init__``
describes it as a "free validate dry-run"). This direct path bypasses the
wallet entirely, so wallet-free says nothing about the infrastructure bill.

For a costless pre-submit check use ``--dry-run``, which never touches Modal,
plus the offline job-spec preflight named below.

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
import time
import uuid
from pathlib import Path

TOOLS_HUB = Path(__file__).resolve().parents[2]
# Overridable: the default is one operator's local campaign workspace, which
# exists on no other machine and in no CI runner. --target / PROTEINA_TARGET_PDB
# make this runnable elsewhere instead of silently FileNotFoundError-ing.
DEFAULT_TARGET = (
    r"C:\Users\lab\Documents\Claude_projects\boltzgen-workspace"
    r"\aglyco-fc-vhh\inputs\3ave_target_AB.pdb"
)
APP = "ranomics-proteina-prod"
FN = "run_tool"
# The accelerator EVERY command here that reaches Modal allocates — --validate
# included. modal_app.py declares exactly one @app.function and it is
# unconditionally `gpu=_GPU`; there is no preset branch, no `with_options`
# anywhere in the repo, and no second CPU-only function to route a cheap preset
# at. Modal bills container wall-clock, not utilisation, so a preset that skips
# GPU *work* still pays A100-80GB seconds for the cold image pull, the three
# Volume mounts and its own runtime.
#
# Kept as a constant next to APP/FN because it is a CLAIM THIS FILE MAKES TO
# THE OPERATOR, and a claim about money has to be checkable: the regression
# test resolves the real `gpu=` kwarg out of modal_app.py and fails if the two
# ever drift. Set this to None only when FN genuinely carries no gpu= — at
# which point the test will require the cost wording below to change too.
FN_GPU = "A100-80GB"
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


def _default_job_id() -> str:
    """A FRESH job id per invocation. Never a constant, and that matters twice.

    The job id is the only source of shard variation and the only key for the
    parked raw archive:

      * ``run_pipeline.shard_seed`` is ``sha256(job_id) % 1_000_000`` and
        nothing else, so re-running with the same id re-runs the same search
        with the same seed against the same staged structure — a second A100
        shard for bit-identical designs and zero new science. A shard is 8
        designs and a campaign needs far more, so "run --submit again" is the
        normal move, not an exotic one.
      * ``modal_app._raw_archive_name`` is a pure function of the job id, and
        ``_park_raw_archive`` does ``shutil.move`` onto it, so the second run
        destroys the first run's raw tree — the very fallback the inline cap
        relies on for coordinates it had to drop.

    Timestamp for readability (runs sort and are recognisable), uuid suffix so
    two submits in the same second still differ.
    """
    return f"proteina-direct-fc-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _resolve_job_id(args) -> str:
    """``--job-id`` if the operator named one, otherwise a fresh id."""
    return (getattr(args, "job_id", "") or "").strip() or _default_job_id()


def _read_state() -> dict | None:
    """The parked submit state, or None if absent/unreadable."""
    if not STATE.exists():
        return None
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None


def _mark_collected(state: dict) -> None:
    """Record that this call's result has been retrieved.

    WITHOUT THIS THE SUBMIT GUARD IS A DEAD END. ``cmd_collect`` used to leave
    STATE exactly as it found it, so the guard's "run --collect first" advice
    was unsatisfiable: every submit after the first refused, and the only way
    forward was ``--force`` — which also switches off the job-id reuse refusal
    beside it. A guard whose normal-path exit is the bypass trains the operator
    into the bypass, which is worse than no guard at all, because re-running
    ``--submit`` for another 8-design shard is the ordinary move.

    The call id is KEPT, not deleted: ``--collect`` stays repeatable (a
    completed Modal call can be fetched again, e.g. into a different --outdir),
    and only the "you would lose the handle to an unread run" claim is retired.
    Best effort — if this cannot be written the state stays "uncollected", so a
    failure here refuses the next submit rather than silently permitting one.
    """
    try:
        STATE.write_text(json.dumps({**state, "collected": True}, indent=2))
    except OSError as exc:  # pragma: no cover - defensive
        print(f"[collect] warning: could not update {STATE}: {exc}",
              file=sys.stderr)


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


def build_job_spec(*, preset: str, nsamples: int, replicas: int,
                   binder_length: tuple[int, int] = (60, 120)) -> dict:
    """``binder_length`` defaults to the SAME [60, 120] this file always sent,
    so every existing caller is unchanged.

    It is a parameter because omitting the field is NOT "let the model choose":
    ``run_pipeline.main()`` does ``job_spec.get("binder_length") or [60, 120]``
    and ``tools/proteina/__init__._parse_binder_length`` returns the identical
    default, so an unset value silently caps the design space at the CLI
    default. The validated range is 20-300 (``_BINDER_LEN_MIN`` /
    ``_BINDER_LEN_MAX``), and a length sweep has to reach outside [60, 120] to
    be a sweep at all.
    """
    return {
        "preset": preset,
        "config_name": "search_binder_local_pipeline",
        # Empty, and that is load-bearing: a custom run carrying a curated
        # task_name is refused pre-GPU as target_conflict.
        "task_name": "",
        "target_source": "custom",
        "target_chain": "A,B",
        "hotspot_residues": HOTSPOTS,
        "binder_length": [int(binder_length[0]), int(binder_length[1])],
        "rf3_required": False,
        "nsamples": nsamples,
        "replicas": replicas,
    }


def build_payload(url: str, *, preset: str, nsamples: int, replicas: int,
                  job_id: str,
                  binder_length: tuple[int, int] = (60, 120)) -> dict:
    """NOTE the absence of upload_urls_endpoint and job_token. That absence is
    the whole point: it selects INLINE delivery in run_pipeline.main()."""
    return {
        "job_spec": build_job_spec(
            preset=preset, nsamples=nsamples, replicas=replicas,
            binder_length=binder_length),
        "input_presigned_url": url,
        "tier": preset,
        "job_tier": preset,
        "job_id": job_id,
    }


def _resolve_target(args) -> Path:
    target = Path(
        args.target or os.environ.get("PROTEINA_TARGET_PDB") or DEFAULT_TARGET)
    if not target.is_file():
        raise SystemExit(
            f"target structure not found: {target}\n"
            "Pass --target /path/to/target.pdb or set PROTEINA_TARGET_PDB."
        )
    return target


def _stage_target(job_id: str, target: Path) -> str:
    from shared.storage import presigned_input_url, upload_input
    data = target.read_bytes()
    path = upload_input(
        user_id="aglyco-fc-campaign", job_id=job_id,
        filename=target.name, data=data,
        content_type="chemical/x-pdb",
    )
    url = presigned_input_url(path, expires_seconds=7200)
    print(f"[stage] {target.name} ({len(data)} bytes) -> {path}")
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
    """The validate preset short-circuits before GPU WORK — not before GPU
    BILLING. It runs inside the same unconditionally-``gpu=_GPU`` ``run_tool``
    as ``--submit`` (see FN_GPU), so this costs one A100-80GB container for its
    whole lifetime: cold image pull, three Volume mounts, then the checks.

    Cheap relative to a search shard, but not free, and not CPU-only. Use
    ``--dry-run`` for a check that genuinely costs nothing."""
    _load_env_and_path()
    import modal
    job_id = "proteina-direct-validate"
    payload = build_payload(
        _stage_target(job_id, _resolve_target(args)), preset="validate",
        nsamples=args.nsamples, replicas=args.replicas, job_id=job_id)
    fn = modal.Function.from_name(APP, FN)
    print(f"[validate] calling {APP}/{FN} — allocates a {FN_GPU} container "
          "(skips GPU work, still billed for container wall-clock)")
    out = fn.remote(payload)
    print(json.dumps(out, indent=2, default=str)[:4000])
    return 0


def cmd_submit(args) -> int:
    job_id = _resolve_job_id(args)

    # Refused BEFORE _load_env_and_path / modal / _stage_target, so nothing is
    # uploaded and no GPU is asked for. TWO DISTINCT LOSSES, and they have
    # different lifetimes, so they are two separate checks rather than one:
    #
    #   * REUSING A JOB ID re-runs the same seed and overwrites that run's raw
    #     archive (see _default_job_id). Neither is undone by collecting the
    #     result, so this refusal does not care whether the prior run was
    #     collected. It only reaches back one run, because STATE holds one
    #     slot — acceptable, since the default id is now unique and reuse
    #     therefore takes a deliberate --job-id.
    #   * OVERWRITING AN UNCOLLECTED STATE loses the prior call id, and without
    #     it that run can never be --collect'ed: its designs are unreachable.
    #     Collecting IS what retires this one, which is why _mark_collected
    #     exists — otherwise the advice below is unsatisfiable and every submit
    #     after the first needs --force.
    prior = _read_state()
    if prior and not args.force:
        if prior.get("job_id") == job_id:
            print(
                f"refusing to re-submit job id {job_id!r}: it is already in "
                f"{STATE}.\nThe seed is derived from the job id, so this would "
                "spend another A100 shard on bit-identical designs, and the "
                "raw archive is keyed on it too, so the previous run's tree "
                "would be overwritten.\nDrop --job-id to get a fresh one, or "
                "pass --force if you really mean it.",
                file=sys.stderr,
            )
            return 2
        if not prior.get("collected"):
            print(
                f"refusing to overwrite {STATE}: it still holds UNCOLLECTED "
                f"call {prior.get('call_id')} for job "
                f"{prior.get('job_id')!r}.\n"
                "Run --collect first (the call id is the only way to reach "
                "that run's designs), or pass --force to discard it.",
                file=sys.stderr,
            )
            return 2

    _load_env_and_path()
    import modal
    payload = build_payload(
        _stage_target(job_id, _resolve_target(args)), preset=args.preset,
        nsamples=args.nsamples, replicas=args.replicas, job_id=job_id)
    fn = modal.Function.from_name(APP, FN)
    call = fn.spawn(payload)
    STATE.write_text(json.dumps(
        {"call_id": call.object_id, "job_id": job_id,
         "job_spec": payload["job_spec"]}, indent=2))
    print(f"[submit] {job_id} -> {call.object_id}")
    print(f"[submit] state saved to {STATE}; run --collect when it finishes")
    return 0


def _plddt_text(value) -> str:
    """Render ``af2_plddt`` so the number cannot be read on the wrong scale.

    Proteina's reward CSV carries ``af2folding_plddt_log`` on [0,1] and
    ``parse_designs`` stores it unchanged, so this line prints ``0.86`` where
    every sibling generator prints ``86``: pxdesign, rfantibody and boltzgen
    each rescale pLDDT to the field-standard AlphaFold2 0-100 range inside the
    container, before it ever reaches a candidate's ``scores``
    (``pxdesign/run_pipeline.py`` ``if "pLDDT" in scores and 0.0 <=
    scores["pLDDT"] <= 1.0: ... * 100.0``, and the same in the other two).
    ``plddt=0.86`` next to the universal "pLDDT > 80 is confidently folded"
    gate reads as a catastrophically unfolded design, which is the opposite of
    what it says.

    THE VALUE ITSELF IS NOT RESCALED, and the annotation goes here rather than
    in ``run_pipeline.parse_designs`` for one specific reason: that ``scores``
    dict is built ONCE, by the parser, for both delivery modes. Rescaling
    there would silently move every number the production web tier has already
    stored and renders today — ``templates/tools/proteina_results.html`` reads
    ``candidates[*].scores.af2_plddt`` straight into its column — so the same
    design would report 0.86 in one job row and 86.0 in the next with nothing
    recording which scale a given row is on. Rescaling only when inlining is
    worse still: it would make a SCORE depend on the delivery mode, the thing
    ``test_scores_and_ranking_are_identical_between_the_two_modes`` exists to
    forbid, and would mix scales inside one campaign's pooled shards.

    So the printed line states both readings and the JSON keeps the one scale
    it has always had.

    THE SCALE WAS THE SECOND PROBLEM WITH THIS NUMBER, AND THE SMALLER ONE.
    Until the polarity fix, ``af2_plddt`` resolved to ``af2folding_plddt`` —
    the AfDesign LOSS term, ``1 - pLDDT`` — so this helper was confidently
    annotating an inverted value ("AF2 scale: 22.6/100" for a design whose
    real pLDDT was 77.4). Everything below about scale was correct and did
    nothing to catch it, which is the lesson: a careful note about how to READ
    a number is not a check on whether it is the RIGHT number.

    The [0,1] guard mirrors pxdesign's: the column aliases
    in ``_SCORE_COLUMNS`` include plain ``plddt``, which some CSVs write on
    0-100 already, and annotating THAT would create the error it prevents.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if 0.0 <= value <= 1.0:
        return f"{value} (AF2 scale: {value * 100:.1f}/100)"
    return str(value)


def cmd_collect(args) -> int:
    _load_env_and_path()
    import modal
    if not STATE.exists():
        print(f"no state at {STATE}; run --submit first", file=sys.stderr)
        return 2
    state = json.loads(STATE.read_text())
    call = modal.FunctionCall.from_id(state["call_id"])
    out = call.get(timeout=args.timeout)
    # The moment the result is IN HAND, not the moment it is written to disk:
    # everything below is presentation, and a run whose result was retrieved is
    # no longer a run whose call id must be protected. Deliberately before the
    # NO-COORDINATES return too — that run's atoms are in the raw archive, not
    # behind this call id, so holding the submit guard open buys nothing.
    _mark_collected(state)

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

    # PERSISTED BEFORE the no-coordinates return, not after it. This is the
    # only machine-readable copy of what a paid shard produced, and the
    # failure path is where it is wanted MOST: run_pipeline deliberately keeps
    # scores, ranks and the whole candidate list on a FAILED result (see the
    # inline-delivery verdict in its main()) precisely so the science survives
    # a delivery failure — and every pre-GPU `_fail` too (the #116 minimum
    # target size, the #118 empty contig, hotspot_chain_ambiguous,
    # hotspot_malformed, target_conflict) lands here with zero candidates and
    # its diagnosis in `error`.
    #
    # Writing it after the early return threw all of that away to terminal
    # scrollback. Re-running --collect did not recover it either: the call id
    # is kept, so the fetch succeeds, but it hits the identical early return
    # and still never writes the file. The loss was permanent, not deferred.
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "smoke_result.json").write_text(json.dumps(smoke, indent=2))

    if not with_atoms:
        print(f"\nwrote smoke_result.json to {outdir} "
              "(scores, ranks and error detail; atoms are in the raw archive)")
        print("\nNO COORDINATES INLINE — the goal is NOT met.", file=sys.stderr)
        return 1

    for c in with_atoms:
        pdb = base64.b64decode(c["pdb_content_b64"])
        dest = outdir / f"design_{c['rank']:03d}.pdb"
        dest.write_bytes(pdb)
        scores = c.get("scores") or {}
        print(f"  rank {c['rank']:>3}  reward={scores.get('total_reward')}  "
              f"plddt={_plddt_text(scores.get('af2_plddt'))}  "
              f"{len(pdb)} bytes -> {dest}")
    # smoke_result.json was written above, on the path EVERY outcome takes —
    # not repeated here. `smoke` is not mutated in between, so a second write
    # would only produce identical bytes and a second place to keep in sync.
    print(f"\nwrote {len(with_atoms)} PDB(s) + smoke_result.json to {outdir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Split out of main() so tests exercise the REAL defaults. A test that
    builds its own Namespace cannot catch a constant creeping back into
    add_argument, which is how --job-id came to default to a fixed string."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--preset", default="protein_binder")
    ap.add_argument("--nsamples", type=int, default=4)
    ap.add_argument("--replicas", type=int, default=2)
    ap.add_argument("--job-id", default="",
                    help="child job id (default: a fresh timestamped one). "
                         "Reusing one repeats the shard seed AND overwrites "
                         "that run's raw archive.")
    ap.add_argument("--force", action="store_true",
                    help="submit even though state for an uncollected call "
                         "exists; its call id is discarded.")
    ap.add_argument("--timeout", type=int, default=7200)
    ap.add_argument("--outdir", default="proteina_direct_out")
    ap.add_argument("--target", default="",
                    help="target PDB (default: PROTEINA_TARGET_PDB or the "
                         "campaign Fc structure)")
    return ap


def main() -> int:
    ap = build_parser()
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
