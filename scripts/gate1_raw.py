"""Read the fold count out of a boltz2 run's parked work tree.

Separate module so the Gate 1 abort gate can be tested without spawning a
GPU job: `python scripts/gate1_raw.py` runs the self-check at the bottom.
"""
import os
import tarfile

import modal

RAW_VOLUME = "ranomics-boltz2-raw"   # _RAW_VOLUME, tools/boltz2/modal_app.py
RAW_DIR = "raw_gate1"


def folds_in_raw(job_id, raw_dir=RAW_DIR):
    """Count folded structures in one run's parked work tree.

    Counts `.pdb` files under an `out/` directory because that is precisely
    what the pipeline itself accepts as a fold: collect_outputs globs
    `{out_dir}/**/*.pdb` (run_pipeline.py) and a design whose glob comes back
    empty is logged "design %s: no PDB emitted" by `main` and dropped. out_dir
    is `workdir/d_{i:03d}/out`, built in `main`, and the tar is of workdir.

    Returns (n_pdb, n_files_under_out, local_path). A missing or unreadable
    tar returns (0, 0, None): absence of evidence is scored as no fold, never
    as a pass, and never raises mid-budget.
    """
    os.makedirs(raw_dir, exist_ok=True)
    local = os.path.join(raw_dir, f"{job_id}.tgz")
    part = local + ".part"
    try:
        vol = modal.Volume.from_name(RAW_VOLUME)
        # Download to a sidecar and rename only once the whole archive is on
        # disk AND parses. `open(local, "wb")` truncates before the first byte
        # arrives, so a read that failed mid-stream would destroy a good
        # archive an earlier call had already fetched -- and this tar is the
        # only thing that separates "folds" from "no folds".
        with open(part, "wb") as fh:
            for chunk in vol.read_file(f"{job_id}.tgz"):
                fh.write(chunk)
        with tarfile.open(part, "r:gz") as tf:
            under_out = [n for n in tf.getnames() if "/out/" in n]
        os.replace(part, local)
    except Exception as exc:  # noqa: BLE001 -- a 0, never a crash on the budget
        print(f"  raw tar unavailable for {job_id}: {exc!r}")
        return 0, 0, None
    return sum(n.endswith(".pdb") for n in under_out), len(under_out), local


if __name__ == "__main__":
    # Self-check against the two runs of 2026-09-16, which are the only boltz2
    # jobs whose fold count is independently known: both were on prod v11
    # (the pre-#285 image) and every design died on a torchvision import, so
    # the correct answer for both is 0 folds. Verified independently at the
    # time with `tar tzf <archive> | grep -c "out/.*\."` over all four
    # archives then on the Volume, which returned 0 for each.
    #
    # This exercises the whole path -- Volume.read_file, the gzip parse, the
    # member filter -- for $0, because reading a Volume costs no GPU.
    KNOWN_ZERO = [
        "gate1-standalone-1789568688",
        "gate1-msa_server-1789568770",
    ]
    for jid in KNOWN_ZERO:
        n_pdb, n_out, local = folds_in_raw(jid, raw_dir="raw_selfcheck")
        print(f"{jid}: {n_pdb} pdb, {n_out} files under out/, {local}")
        assert local is not None, f"{jid}: tar missing, cannot self-check"
        assert n_pdb == 0, f"{jid}: expected 0 folds on the v11 image, got {n_pdb}"

    # A job id that cannot exist must score 0 and not raise -- the branch the
    # abort gate depends on when a run dies before parking anything.
    n_pdb, n_out, local = folds_in_raw("gate1-no-such-job", raw_dir="raw_selfcheck")
    assert (n_pdb, n_out, local) == (0, 0, None), (n_pdb, n_out, local)

    print("self-check OK: known-zero runs read as 0, missing tar reads as 0")
