"""Gate 1 -- Boltz-2 smoke. PLAN-v2-new-scaffold-sweep.md section 4.

RESULT, Rung A, 2026-09-19: it ran, and 3 of 3 folds succeeded -- the first
successful Boltz-2 folds this repo has produced. 229 s wall, $0.16 of the
$0.79 ceiling; job gate1-standalone-1789842854, call
fc-01M2XF557NA259FRD6CJMJFDFJ, on ranomics-boltz2-prod v13 (cab729e).
  (i)   YES. The f49f5cf (PR #285) repair holds -- no torchvision import
        death, which is how both tiers died on 2026-09-16.
  (ii)  ~69 s/design marginal: 81.9 s for the first, which carries the model
        load, then 68.5 s and 69.5 s. $0.049/design at RATE below.
  (iii) NOT MSA-blind. The parked tree's msa/ dirs are empty and Boltz warned
        the prediction would be suboptimal without an MSA, yet the antigen
        chain reproduced its own crystal coordinates at CA-RMSD 0.65 / 0.72 /
        0.73 A (antigen pLDDT 86.6 / 82.9 / 91.7).
Measured but not asked for: ipTM separated the cognate (0.953) from both
decoys (0.719, 0.619), preserving the ESM prior's rank order (0.949 / 0.621 /
0.102) -- but the cd45 decoy rose from 0.102 to 0.619, so a 0.70 ipTM gate
would PASS the IL4 decoy.

RUNG B, RUN 2026-09-21 on its own per-job approval: the msa_server tier, and
the answer is that it makes decoy discrimination WORSE. 661 s wall / $0.47 of
an authorised $0.65, 3 folded .pdb of 3, job gate1-msa_server-1790046491. ipTM
cd45_decoy 0.801, IL4_decoy 0.675, TIGIT_cognate 0.948: the cognate holds
(0.953 -> 0.948) while the WORST decoy by the ESM prior climbs 0.619 -> 0.801,
so the cognate-to-best-decoy margin collapses 0.234 -> 0.147 and the two
decoys swap rank. Not a thin-MSA artifact -- ~17.5-18.5k UniRef and ~2.4-2.8k
paired sequences per design, against a Rung A archive carrying no MSA files at
all. Keep standalone as the default: cheaper AND better separation. Full
write-up, including the framework-homology hypothesis for WHY, is in
docs/VALIDATION-LOG.md under "## Boltz-2".

That run also priced the tier's runtime: 643 s / 3 = 214 s/design against the
"~3 min / design" in tools/boltz2/__init__.py, ~19% optimistic. Better than
that file's OTHER figure -- "~15 s / design" for standalone, measured ~69 s
above, 4.6x out -- but short in the same direction, which is why DEADLINES
below was NOT sized on it. Sizing on it would have given 553 s and cancelled
this run at 84% complete.

THE THREE QUESTIONS, as they stood before Rung A. Boltz-2 had never run in
this repo, and docs/VALIDATION-LOG.md carried no boltz2 rows at all -- only
the placeholder line under the combined "AF2-IG, Boltz-2, LigandMPNN,
RF2-standalone, RFdiff-standalone" heading. #320 closed that gap on 2026-09-21: it added the
"## Boltz-2" section with the Rung A row and dropped Boltz-2 from the
placeholder. This commit adds the Rung B row above it. What the runs bought before any design GPU is spent:
  (i)   does the deployed container run at all;
  (ii)  real seconds/design on a ~350-residue scFv + antigen complex, the only
        number that lets Gate 3 be priced honestly;
  (iii) whether the standalone (single-sequence) tier is MSA-blind on a
        NATURAL antigen the way ColabFold was -- the antigen chain's pLDDT
        inside the complex is the read. Rung A runs standalone alone, so
        (iii) has no paired msa_server arm -- but it does not need one. The
        antigen is a NATURAL chain whose crystal coordinates are the input
        file, so the ground truth is already on disk: CA-RMSD of the
        predicted antigen chain against staged antigen.pdb is a stronger
        control than msa_server would have been, and it is free.

ANTIGEN. RCSB 3RQ3 chain A, fetched by the container straight from
files.rcsb.org. 107 aa, zero unknown residues, and an exact substring of the
109 aa TIGIT target sequence the archived designs were optimised against.
That check ran on 2026-09-21 against a fresh files.rcsb.org download, which
is NOT in this repo -- reproducing it means re-fetching 3RQ3.pdb and matching
its chain A into the seed3_demo-b2-tigit-s1 target_seq in gate1_binders.json,
which is the only half of the comparison the repo carries. Serving it from
RCSB sidesteps both options PLAN-v2 section 6 flagged: no write to production
Supabase, nothing published.

BINDERS. Three archived designs spanning ESM iPTM 0.102 / 0.621 / 0.949.
Against this antigen the TIGIT design is a COGNATE pair and the cd45 and IL-4
designs are DECOYS. n=1 per class is not Rule 0 -- it is a sanity read, and a
cognate scoring below both decoys is an early warning that costs nothing
extra.

UPLOADS ARE EXPECTED TO FAIL, BY DESIGN. run_pipeline.py's `main` hard-fails
preflight on an empty upload_urls_endpoint (its `if not upload_endpoint`
guard), and that endpoint only exists in the web flow, which writes to
production Supabase. A dummy endpoint satisfies preflight; each design then
takes the `except` wrapping the request_upload_urls / upload_pdb pair in
`main`, which increments n_failures, logs a warning beginning
"design %s: upload failed", and `continue`s before designs_out.append.
Scores are recovered instead from the raw work tree, which the wrapper parks
on the ranomics-boltz2-raw Volume unconditionally -- "not gated on success,
on candidates, or on anything having been uploaded", the raw-archive block
in run_tool, tools/boltz2/modal_app.py, parked as <job_id>.tgz under
_RAW_MOUNT. Zero production writes.

CONSEQUENCE, and it is the reason the abort gate reads a tar rather than the
return value. Every design taking that branch leaves designs_out empty, and
since f49f5cf (PR #285) an empty designs_out is
_fail("pipeline", "no_designs") in `main`, which exits 1. So every run of
THIS driver returns status FAILED with designs_completed 0 whether all three
folds succeeded or none did -- the two outcomes are indistinguishable in the
returned dict. Only the parked tree separates them, and it is written by
archive_raw_outputs(str(workdir)) in `main`'s finally and therefore BEFORE
that _fail.

That last point is true of cab729e and is the state both Rung A and Rung B
ran against. It is being fixed: `main` now counts folds in n_folded, decided
before the upload, and the no_designs detail reads "N of M designs folded but
0 uploaded" instead of "all M designs failed". NOTHING BELOW CHANGES. The
abort gate still reads the tar, because what made designs_completed useless
here is unchanged -- it is 0 on every run of this driver by construction, the
uploads being rigged to fail -- and a detail string is prose, while
folds_in_raw() counts files the container actually wrote. The next person to
run this against a rebuilt image should expect the new detail and still
believe the tar.

This docstring cites symbols, not line numbers: they resolve
against tools/boltz2/ at cab729e, deployed as ranomics-boltz2-prod v13
(checked with `modal app history ranomics-boltz2-prod`; that table records
every deploy of this app against a dirty tree, so the image is not provably
byte-exact to its commit). run_pipeline.py is the only file under
tools/boltz2/ that cab729e changed relative to f49f5cf (PR #285), and its
AST with docstrings stripped is unchanged, so every symbol below denotes
the same code in both.

SPEND. Two independent bounds, neither of them a promise in prose:
  1. Per job: Modal kills the container at _MAX_SESSION_S = 3600 s,
     _MAX_SESSION_S in tools/boltz2/modal_app.py.
  2. This driver: each job is spawned, its FunctionCall id written to disk and
     fsynced BEFORE anything blocks (a write that fails tears the container
     down, and prints the id if that teardown fails too, rather than
     leave it running unrecorded), and cancelled with
     terminate_containers=True at its own deadline. The deadlines sum to at
     most BUDGET_S, which is CEILING_USD at the A100-40GB raw rate in
     GPU_USD_PER_SECOND, shared/wallet_estimates.py. Modal-direct calls
     bypass the wallet, so the raw rate -- not the 1.70x marked-up one -- is
     the real dollars.
     Teardown is not instantaneous, so treat $0.64 as the target and ~$0.68
     as the true worst case. Should this client die before it can cancel,
     the only bound left is Modal's own _MAX_SESSION_S (modal_app.py), and
     at that cap one container is $2.57.
"""
import json
import os
import time

import modal
from modal.exception import FunctionTimeoutError
from modal.exception import TimeoutError as ModalPollTimeout

# The abort gate's success signal. Its own module so it can be exercised
# without spawning a GPU job -- `python scripts/gate1_raw.py` self-checks it
# against the two known-zero runs of 2026-09-16. The import resolves because
# Python puts a script's own directory on sys.path[0]; this file is a script
# and is never imported (the guard below enforces that).
from gate1_raw import folds_in_raw

if __name__ != "__main__":
    raise RuntimeError(
        "gate1_boltz2_smoke is a script, not a module: every top-level "
        "statement below spawns a billed A100 job. Run it as "
        "`python scripts/gate1_boltz2_smoke.py`."
    )

RATE = 0.000714          # GPU_USD_PER_SECOND["A100-40GB"] $/s,
                         # shared/wallet_estimates.py
CEILING_USD = 0.65       # Gate 1 Rung B ceiling: msa_server only
BUDGET_S = int(CEILING_USD / RATE)

ANTIGEN_URL = "https://files.rcsb.org/download/3RQ3.pdb"
ANTIGEN_CHAIN = "A"
# Exercises hotspot_contacts (run_pipeline.py) on a real complex. NOT a
# biological claim about the TIGIT interface -- Gate 1 reads iptm and plddt,
# never filter_status.
HOTSPOTS = [40, 42, 44, 46, 48]
# Instant ECONNREFUSED inside the container, so the 30 s POST timeout in
# request_upload_urls (run_pipeline.py) is never paid.
UPLOAD_ENDPOINT = "http://127.0.0.1:1/upload"

# RUNG B: msa_server only. Rung A bought standalone on 2026-09-19 (3/3 folds,
# $0.16), so scheduling it again would pay a second time for a measured answer.
#
# 900 s is a SPEND CEILING, not an estimate, and deliberately not derived from
# the "~3 min / design" figure -- the docstring says why that figure is not
# trustworthy. At RATE it is $0.64. Refusing to size on that figure is what
# saved the run: it took 661 s, and the 553 s that "~3 min / design" implies
# would have cancelled it at 84% complete. The cancel path stayed the
# fallback and did not fire -- had it, folds_in_raw() still counts whatever
# reached the tar, so a per-design rate survives a cancelled run.
#
# `modal volume ls boltz2-weights` on 2026-09-19 listed boltz2_conf.ckpt,
# boltz2_aff.ckpt and mols.tar, 5.7 GiB between them, all three stamped
# 2026-05-29, plus a mols/ directory older still at 2025-02-18, and Rung A
# wrote nothing to it. That contradicts the "~1 GB of model weights" comment
# beside the Volume in modal_app.py, still present there at this commit. The
# same listing immediately before and after the 2026-09-21 Rung B run gave
# the same four entries at the same timestamps and sizes, so Rung B wrote
# nothing to it either and no weights download came out of the 900 s. That
# says nothing about container cold start, which is a separate cost and was
# not separately measured; nor was the split between MSA-server fetch and GPU
# compute, so 214 s/design is an aggregate.
DEADLINES = [("msa_server", 900)]
assert sum(d for _, d in DEADLINES) <= BUDGET_S, "deadlines exceed the ceiling"

PICKS = [
    # (run, pdb_key, role against a TIGIT antigen)
    ("verify242-bs6-1789054528", "design_1_complex.pdb", "decoy"),       # ESM 0.102, cd45
    ("seed1_demo-b2-il4-s0", "seed1_design_0_complex.pdb", "decoy"),     # ESM 0.621, IL-4
    ("seed3_demo-b2-tigit-s1", "seed3_design_0_complex.pdb", "cognate"), # ESM 0.949, TIGIT
]

# Both land in the CWD, not beside this file: the call log IS the reattach
# state, so it belongs to the run you are doing, not to the checkout. Both,
# and the raw_gate1/ tree folds_in_raw fills, are gitignored -- committing a
# call log would make the next run reattach to a call that is long dead.
CALL_LOG = "gate1_calls.jsonl"
RESULT_LOG = "gate1_results.json"

# The three PICKS rows, extracted verbatim from the 25-row phase_b_records.json
# this driver was developed against, so it is self-contained. Same keys, same
# values, and PICKS selects by (run, pdb_key) -- never by index. Resolved
# beside this file rather than in the CWD so the script runs from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
rows = {
    (r["run"], r["pdb_key"]): r
    for r in json.load(open(os.path.join(_HERE, "gate1_binders.json")))["rows"]
}
binders = []
manifest = []
for run, key, role in PICKS:
    r = rows[(run, key)]
    name = f"{r['target'].replace('-', '')}_{role}_{r['esm_iptm']:.3f}".replace(".", "p")
    binders.append({"name": name, "sequence": r["binder_seq"]})
    manifest.append(
        {
            "name": name,
            "run": run,
            "pdb_key": key,
            "role": role,
            "esm_target": r["target"],
            "esm_iptm": r["esm_iptm"],
            "esm_cdr_proxy": r["esm_cdr_proxy"],
            "binder_len": len(r["binder_seq"]),
        }
    )

print("binders:")
for m in manifest:
    print(f"  {m['name']:32s} {m['role']:8s} esm_iptm={m['esm_iptm']} len={m['binder_len']}")

fn = modal.Function.from_name("ranomics-boltz2-prod", "run_tool")

out_all = {"manifest": manifest, "antigen_url": ANTIGEN_URL, "jobs": []}
spent_s = 0.0

# Never clobber a previous run's ledger -- it records real money. Resolved
# once, before the loop, so every tier of THIS run appends to one file.
result_log = RESULT_LOG
_n = 1
while os.path.exists(result_log):
    result_log = f"{os.path.splitext(RESULT_LOG)[0]}.{_n}.json"
    _n += 1

for tier, deadline in DEADLINES:
    job_id = f"gate1-{tier}-{int(time.time())}"
    payload = {
        "job_spec": {
            "preset": tier,
            "antigen_chain": ANTIGEN_CHAIN,
            "hotspot_residues": HOTSPOTS,
            "binder_sequences": binders,
            "parameters": {},
        },
        "input_presigned_url": ANTIGEN_URL,
        "upload_urls_endpoint": UPLOAD_ENDPOINT,
        "job_token": "gate1-no-upload",
        "tier": tier,
        "job_tier": tier,
        "job_id": job_id,
        "webhook_url": "",
    }

    # RESUME BY REATTACH. A call already recorded for this tier is reattached,
    # never respawned: killing the local client does NOT stop the container
    # (2026-09-15 esmfold2 orphan, 330 s / $0.34 lost), so a rerun that spawned
    # again would pay for the same fold twice. The id is on disk because it is
    # written and fsynced before anything blocks.
    prior = None
    logged = []
    if os.path.exists(CALL_LOG):
        for line in open(CALL_LOG):
            r = json.loads(line)
            logged.append(r)
            if r["tier"] == tier:
                prior = r
    if prior is not None and time.time() - prior["spawned_at"] > prior["deadline_s"]:
        # Past its own deadline, so reattaching is wrong twice over. The
        # accounting: `wall` below counts from spawned_at, so a day-old record
        # would report tens of dollars for a run that spent nothing. The
        # container: modal's cap is _MAX_SESSION_S (3600 s), _MAX_SESSION_S in
        # tools/boltz2/modal_app.py, NOT this driver's deadline, so between
        # those two ages the call may still be LIVE -- which is why this
        # cancels rather than merely skipping. The cancel is unconditional
        # rather than conditioned on the call's state: the `except` below
        # absorbs a call that has already finished, so the unconditional form
        # is safe whether or not the container is still up.
        age = time.time() - prior["spawned_at"]
        print(
            f"\n[{tier}] call log entry is {age:.0f}s old, past its "
            f"{prior['deadline_s']}s deadline -- cancelling every recorded "
            f"call and stopping. Move {CALL_LOG} aside to start a fresh run.",
            flush=True,
        )
        # EVERY record, not just this tier's. Tiers run in order, so on a
        # multi-tier resume it is the EARLIER tier whose record ages past its
        # deadline while the LATER tier holds the live container. Cancelling
        # only the tripped tier would walk away from that live A100 -- and the
        # remedy printed above then destroys the only record of its call id.
        for r in logged:
            try:
                modal.FunctionCall.from_id(r["call_id"]).cancel(terminate_containers=True)
                print(f"[{tier}]   cancelled {r['tier']} call={r['call_id']}", flush=True)
            except Exception as exc:  # noqa: BLE001 -- already gone is the common case
                print(f"[{tier}]   cancel {r['call_id']}: {exc!r}", flush=True)
        raise SystemExit(1)
    if prior is not None:
        rec = prior
        job_id = rec["job_id"]
        fc = modal.FunctionCall.from_id(rec["call_id"])
        elapsed_before = time.time() - rec["spawned_at"]
        print(
            f"\n[{tier}] REATTACH call={rec['call_id']} job_id={job_id} "
            f"already {elapsed_before:.0f}s old, deadline={deadline}s",
            flush=True,
        )
    else:
        fc = fn.spawn(payload)
        rec = {
            "tier": tier,
            "job_id": job_id,
            "call_id": fc.object_id,
            "deadline_s": deadline,
            "spawned_at": time.time(),
        }
        elapsed_before = 0.0
        # Printed BEFORE the write rather than after it: if the write
        # fails, this line is the only place the id exists, and the
        # operator needs it for `modal app history` should the cancel
        # below fail too.
        print(f"\n[{tier}] spawned call={fc.object_id} job_id={job_id} deadline={deadline}s", flush=True)
        # The container is live from the spawn above, and nothing can
        # cancel it until its id reaches disk. If that write fails the id is
        # gone from disk, so tear the container down rather than leave it
        # running with no record of how to stop it. This window sits BEFORE
        # the poll loop, so the loop's own `finally` does not cover it.
        try:
            with open(CALL_LOG, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:  # noqa: BLE001 -- KeyboardInterrupt counts too
            try:
                fc.cancel(terminate_containers=True)
            except Exception as exc:  # noqa: BLE001 -- teardown must not mask the write failure
                print(
                    f"[{tier}] cancel failed after an unlogged spawn -- kill "
                    f"call={fc.object_id} by hand, `modal app history`: {exc!r}",
                    flush=True,
                )
            raise

    # Billed seconds run from the spawn, not from this process starting, so the
    # deadline and the spend total both have to count the pre-reattach life.
    t0 = time.time() - elapsed_before
    out, err, killed = None, None, False
    # Set only where the container is known dead: modal returned, modal killed
    # it at its own timeout, or this driver cancelled it. Anything else leaves
    # the call live and is cleaned up in the `finally`.
    settled = False
    try:
        while True:
            left = deadline - (time.time() - t0)
            if left <= 0:
                killed = True
                print(f"[{tier}] DEADLINE {deadline}s -- cancel(terminate_containers=True)", flush=True)
                fc.cancel(terminate_containers=True)
                settled = True
                break
            try:
                out = fc.get(timeout=min(30.0, left))
                settled = True
                break
            except FunctionTimeoutError as exc:
                # The function hit its own timeout, so modal has already torn
                # the container down -- nothing left to cancel.
                err = f"modal killed the container at its own timeout: {exc!r}"
                settled = True
                break
            except (ModalPollTimeout, TimeoutError):
                # modal 1.4.2 raises the BUILTIN TimeoutError from
                # FunctionCall.get(timeout=), not modal.exception.TimeoutError.
                # Catching only the modal class treated the first poll window as
                # a fatal error and orphaned a live container on 2026-09-16.
                el = time.time() - t0
                print(f"[{tier}]   ... {el:5.0f}s  ${(spent_s + el) * RATE:.2f} cumulative", flush=True)
            except Exception as exc:  # noqa: BLE001 -- record and move on, never hang on the budget
                err = repr(exc)
                break
    finally:
        # The two exits that leave a live container: the generic `except`
        # above, and KeyboardInterrupt, which is not an Exception and unwinds
        # straight through the loop. On either, the deadline never fires and
        # the only remaining bound is _MAX_SESSION_S (3600 s) -- $2.57 at RATE,
        # against the $0.65 ceiling. That bound is what SPEND item 2 in the
        # docstring promises, so it has to hold on every path out of the loop,
        # not just the deadline one.
        if not settled:
            print(f"[{tier}] exiting with the call live -- cancel(terminate_containers=True)", flush=True)
            try:
                fc.cancel(terminate_containers=True)
            except Exception as exc:  # noqa: BLE001 -- teardown must not mask the original failure
                print(f"[{tier}] cancel failed, check `modal app history`: {exc!r}", flush=True)

    wall = time.time() - t0
    spent_s += wall
    n_pdb, n_out_files, raw_local = folds_in_raw(job_id)
    job = {
        **rec,
        "wall_s": round(wall, 1),
        "killed_at_deadline": killed,
        "error": err,
        "result": out,
        "usd_at_raw_rate": round(wall * RATE, 4),
        "folded_pdbs": n_pdb,
        "files_under_out": n_out_files,
        "raw_local": raw_local,
    }
    out_all["jobs"].append(job)
    with open(result_log, "w") as fh:
        json.dump(out_all, fh, indent=2)
    print(
        f"[{tier}] done wall={wall:.0f}s ${wall * RATE:.2f} killed={killed} err={err}",
        flush=True,
    )
    print(
        f"[{tier}] raw tree: {n_pdb} folded .pdb of {len(PICKS)}, "
        f"{n_out_files} files under out/ -> {raw_local}",
        flush=True,
    )
    if out is not None:
        print(f"[{tier}] return: {json.dumps(out)[:600]}", flush=True)

    # ABORT POINT, PLAN-v2 section 4: do not pay for a later tier if this one
    # produced no fold. Deliberately NOT keyed on a tier name. It read
    # `tier == "standalone"`, which went silently inert the moment DEADLINES
    # was rescheduled to msa_server alone -- taking the "produced no fold"
    # line with it, so a failed run would have exited quiet. Caught by
    # test_generic_exception_cancels_the_container, which asserts that line
    # is printed. The rationale below is why it reads the tar rather than the
    # return value.
    #
    # The test reads the parked tar, NOT the return value, and not
    # designs_completed. Two failures drove that:
    #   - 2026-09-16, the pre-#285 image returned status=COMPLETED,
    #     exit_code=0, err=None, killed=False and zero folds (every fold died
    #     on a torchvision import), so an err-or-killed test let msa_server
    #     launch into the same wall. Cost $0.02 to learn.
    #   - designs_completed is 0 on EVERY run of this driver by construction,
    #     including a perfect one, because uploads are rigged to fail and the
    #     failure branch continues before the append. Gating on it would abort
    #     before msa_server no matter what the GPU did.
    # folds_in_raw() reads what the container actually wrote to disk, which is
    # the only place the two cases differ.
    if err or killed or not n_pdb:
        print(f"\n{tier} produced no fold -- stopping.", flush=True)
        break

print(f"\nTOTAL {spent_s:.0f}s = ${spent_s * RATE:.2f} of ${CEILING_USD:.2f}")
print(f"call ids in {CALL_LOG}; results in {result_log}")
print("raw trees: modal volume get ranomics-boltz2-raw <job_id>.tgz")
