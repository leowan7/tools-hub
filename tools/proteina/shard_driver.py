"""Sequential multi-shard driver for a Proteina-Complexa binder-length sweep.

Runs an unattended campaign of many shards through the SAME production path
``direct_call_fc.py`` uses — ``ranomics-proteina-prod/run_tool``, no upload
endpoint, coordinates delivered inline — and lands every design in one
filterable manifest.

    python tools/proteina/shard_driver.py --dry-run          # free, plans only
    python tools/proteina/shard_driver.py --run --yes --budget 350

THE ONE FACT THE DESIGN FOLLOWS FROM. A Modal call id is the ONLY handle to a
paid run. Lose it and the designs it produced are unreachable: they are not in
Storage (there is no upload endpoint on this path) and re-running spends
another A100 on a different seed. So an ``intent`` record naming the job id is
written BEFORE the spawn, the ``submitted`` record naming the call id is
written BEFORE the driver waits, and a resume reconnects to uncollected calls
rather than resubmitting.

ONE GPU AT A TIME. Nothing downstream enforces this — Modal will happily
allocate a second A100 and bill it — so every route to a concurrent container
is guarded here. There are three, and an earlier revision claimed there were
two:
  * ``fn.spawn`` does not queue against a pool: each call gets its own
    container and ``run_tool`` is unconditionally ``gpu="A100-80GB"``. So the
    driver never spawns while a call of its own is unresolved — including the
    case where waiting on it TIMED OUT, which is not the same as it having
    finished (see ``_collect``).
  * A second driver process. The lock is GLOBAL (``default_lock_path``), not
    per-outdir: the invariant belongs to the account, not to a directory, and
    ``--outdir`` defaults to a RELATIVE path, so two shells in different
    working directories already evaded an outdir-scoped lock. Starting a fresh
    campaign is the obvious move on a run that looks stuck, which makes this
    the likely violation rather than the exotic one.
  * A ledger the loop cannot interpret. Skip-terminal / reconnect-submitted /
    else-spawn means every unrecognised state falls through to a spawn, and
    this module's own recovery advice tells operators to hand-edit that file.
    ``_refuse_unresumable`` stops on anything that is not terminal and not a
    ``submitted`` carrying a call id.

WHAT IT DELIBERATELY DOES NOT DO.
  * No automatic retry. A retry needs a FRESH job_id — the shard seed is
    ``sha256(job_id) % 1_000_000`` and the raw archive is keyed on it, so
    re-submitting the same id re-runs an identical search and destroys the
    first run's archive. A "retry" is therefore a new shard, which is a
    judgement call for an operator reading the log, not something to do
    automatically at 3am.
  * No resume of a shard that genuinely FAILED or whose results could not be
    harvested. Only ``submitted``-but-uncollected calls are reconnected,
    because those alone are paid work still reachable.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import socket
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.proteina.direct_call_fc import (  # noqa: E402
    DEFAULT_TARGET, APP, FN, _load_env_and_path, _stage_target, build_payload,
)
# IMPORTED rather than hardcoded, so a change to upstream's SOURCE cannot
# leave a stale copy here.
#
# WHAT THIS DOES NOT TRACK, and the earlier comment wrongly claimed it did:
# upstream's EFFECTIVE deadline is DESIGN_SUBPROCESS_TIMEOUT_S, computed from
# PROTEINA_DESIGN_TIMEOUT_S in the environment of whatever process reads it.
# That is the CONTAINER's environment, which this local process cannot see, so
# no import can mirror it. If that variable is set in the Modal environment
# below ~5000 s, _validate_plan will approve a 64-design shard the container
# then kills. Importing the *_DEFAULT_* symbol is the honest choice - it is the
# value that applies unless someone deliberately overrode it - but it is a
# source-drift guard, not an override guard.
from tools.proteina.run_pipeline import (  # noqa: E402
    DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S as DESIGN_SUBPROCESS_DEADLINE_S,
)

# --- campaign shape ---------------------------------------------------------
# DISJOINT bins over the locked 50-100 window. Inclusive [lo, hi] pairs, which
# is what run_pipeline hands to `complexa target add --binder-length lo hi`, so
# adjacent bins must not share an endpoint: (50,60),(60,70) would draw length
# 60 from two bins and give it double the campaign's weight, which is a broken
# stratification for a study whose whole point is comparing lengths.
# 51 integer lengths do not divide into 5 equal bins; the last one is 11 wide.
BINS: list[tuple[int, int]] = [(50, 59), (60, 69), (70, 79), (80, 89), (90, 100)]
# TIER 1: 4 shards x 64 = 256 designs per bin, 20 shards, ~1.2 days, ~$71.
# Deliberately a pilot. It resolves a ~11 percentage-point difference between
# bins at 80% power, which is enough to decide WHERE to spend the rest, and
# round-robin ordering means the comparison is valid even if it stops early.
# Raise this to continue the same campaign — the plan is append-only in
# SHARDS_PER_BIN, so existing shard indices keep their bins and the ledger
# stays valid (see build_plan / _verify_plan_matches_ledger).
SHARDS_PER_BIN = 4
# Consecutive shards delivering NO designs before the driver gives up.
#
# The 8-designs-to-64 extrapolation behind SECONDS_PER_DESIGN is UNVERIFIED, and
# if it is optimistic enough the pipeline overruns the subprocess deadline and
# every shard dies the same way. Without this the campaign would spend its
# entire budget reproducing one systemic failure — 20 shards at ~$4.21 for
# nothing. Three in a row is past coincidence: a one-off bad shard does not
# repeat, a wrong scaling assumption does.
MAX_CONSECUTIVE_BARREN = 3
# 64 designs/shard. nsamples draws LENGTHS (the upstream flag is
# generation.dataloader.dataset.nres.nsamples) and replicas gives independent
# designs at each drawn length — measured on job
# proteina-direct-fc-20260809-091702-68025f, where nsamples=4/replicas=2 gave
# exactly 4 lengths x 2 designs, the replicas sharing a length and differing in
# sequence.
#
# 16 draws over a ~10-length bin does NOT give "every length, 4 replicates
# each": the sampler's draw semantics are upstream in the vendored image and
# unverified here, and under replacement 16 draws cover ~8 of 10 lengths with
# an uneven replicate count. The reliable unit of analysis is therefore the
# BIN (n=1024 over 16 shards), with per-length counts read off the manifest's
# realised lengths as indicative only.
NSAMPLES = 16
REPLICAS = 4
DESIGNS_PER_SHARD = NSAMPLES * REPLICAS

# Validated bounds from tools/proteina/__init__.py. Checked here because the
# DIRECT path does no bounds validation at all — run_pipeline int-converts the
# pair and hands it to `complexa target add`, so a bad range reaches the GPU.
BINDER_LEN_MIN, BINDER_LEN_MAX = 20, 300
# How far outside its requested bin a realised length may sit before the driver
# calls it a mis-aimed shard. Slack, not tolerance of a wrong bin: upstream
# samples within [lo, hi] and the count is taken off chain C of the returned
# coordinates, so a correct shard should be inside the bin exactly.
LENGTH_SLACK_AA = 2

# --- cost model -------------------------------------------------------------
# Fitted to the one metered run: 8 designs, 673 s pipeline, $0.5528 charged.
# Stage split from that container's log (generate 343.6 + evaluate 274.6 scale
# with design count; filter 9.8 + analyze 31.1 + staging/parse are treated as
# fixed).
#
# TWO FREE PARAMETERS FITTED TO ONE OBSERVATION. That is not a fit, it is an
# assertion with a plausible shape, and the fixed/variable split in particular
# is assumed rather than measured — `analyze` is an aggregation over the design
# set and may well scale with it. A second shard at a different N is what would
# turn this into a measurement. Treat every projection as indicative.
#
# The RATE is inferred, not published. $0.5528 over a run whose pipeline alone
# took 673 s puts a HARD upper bound of $2.958/hr on the true rate; $2.50/hr
# leaves a 123 s container overhead (cold start, three volume mounts, archive
# tar, two volume commits), which is the plausible reading. The repo's own
# $3.70/hr card (shared/wallet.py:106) is arithmetically impossible here — it
# implies 538 billed seconds for a 673 s pipeline.
USD_PER_SECOND = 2.50 / 3600.0
# The BUDGET uses the upper bound, not the point estimate. A ceiling priced at
# the optimistic end of its own uncertainty cannot do its job: at $2.958/hr the
# shipped 80-shard plan bills $337 while a $300 "ceiling" never fires, so the
# operator who set it is billed 12% over a number they believed was hard.
USD_PER_SECOND_CEILING = 0.5528 / 673.0
SECONDS_PER_DESIGN = (343.6 + 274.6) / 8.0            # 77.28, scales with N
SECONDS_IN_PIPELINE_FIXED = 673.0 - (343.6 + 274.6)   # 54.8, filter+analyze+parse
# Billed but OUTSIDE the pipeline's own clock: cold start, three volume mounts,
# the archive tar and two volume commits. Named rather than inlined because the
# deadline check below needs to SUBTRACT it — the subprocess deadline applies
# to the pipeline, not to the container.
CONTAINER_OVERHEAD_S = 123.0
SECONDS_FIXED_PER_SHARD = SECONDS_IN_PIPELINE_FIXED + CONTAINER_OVERHEAD_S
# run_tool's container ceiling (modal_app._MAX_SESSION_S). --timeout must
# exceed it or the driver can give up on a container Modal is still running.
CONTAINER_CEILING_S = 7200

# States that mean an A100 was allocated and therefore billed. `intent` is
# included deliberately: it means the driver was about to spawn and may have
# succeeded before dying, so the conservative reading is that it cost money.
BILLED_STATES = {"intent", "submitted", "collected", "empty", "failed",
                 "harvest_error"}
# States that need no further work. `submitted` is NOT terminal — that is the
# reconnect case — and neither is `intent`, which is the ambiguous one.
TERMINAL_STATES = {"collected", "empty", "failed", "harvest_error"}


# ``None`` rather than ``DESIGNS_PER_SHARD`` as the default: a default
# expression is evaluated once at import, so the module constant would be
# frozen into the signature and any later change to NSAMPLES/REPLICAS would be
# silently ignored by exactly the guards that exist to catch it.
def shard_seconds(n_designs: int | None = None) -> float:
    n = DESIGNS_PER_SHARD if n_designs is None else n_designs
    return SECONDS_FIXED_PER_SHARD + SECONDS_PER_DESIGN * n


def shard_usd(n_designs: int | None = None) -> float:
    """Best-estimate cost. For projections."""
    return shard_seconds(n_designs) * USD_PER_SECOND


def shard_usd_ceiling(n_designs: int | None = None) -> float:
    """Worst-case cost consistent with the measurement. For the budget guard."""
    return shard_seconds(n_designs) * USD_PER_SECOND_CEILING


# --- plan -------------------------------------------------------------------

def build_plan(bins=None, per_bin=None) -> list[dict]:
    """Round-robin, NOT bin-by-bin.

    ``None`` defaults for the same reason ``shard_seconds`` uses one: written
    as ``bins=BINS`` the module constant is captured once at import, so
    changing BINS or SHARDS_PER_BIN would leave this function returning the
    old plan — including for _validate_plan, the guard meant to catch a bad
    BINS.

    Interleaving means an interrupted campaign still has equal n per bin, so
    the length comparison is valid at every moment rather than only at the end
    — and the even-versus-adaptive allocation choice can be deferred until
    there is data to make it with.

    ``index`` is the stable ledger key: it is a pure function of (bins,
    per_bin), so a resume with the same configuration reproduces the same
    numbering. Changing BINS or SHARDS_PER_BIN mid-campaign renumbers the plan
    and invalidates the ledger; _verify_plan_matches_ledger refuses that.
    """
    bins = BINS if bins is None else bins
    per_bin = SHARDS_PER_BIN if per_bin is None else per_bin
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
    seen: list[tuple[int, int]] = []
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
        if (lo, hi) not in seen:
            seen.append((lo, hi))
    # Overlapping strata double-weight the shared lengths and make a
    # length comparison unsound. Cheap to check, impossible to see by eye once
    # the list is long.
    for i, (lo_a, hi_a) in enumerate(seen):
        for lo_b, hi_b in seen[i + 1:]:
            if lo_a <= hi_b and lo_b <= hi_a:
                raise SystemExit(
                    f"bins [{lo_a}, {hi_a}] and [{lo_b}, {hi_b}] overlap; "
                    "the shared lengths would get double the campaign weight")


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
        try:
            key = int(rec["index"])
        except (KeyError, TypeError, ValueError):
            # The ledger is a file this module TELLS operators to hand-edit,
            # so a bad index is a foreseeable input, not an internal error.
            print(f"[ledger] skipping record with an unusable index: "
                  f"{line[:80]!r}", file=sys.stderr)
            continue
        latest[key] = {**latest.get(key, {}), **rec}
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


def _refuse_unresumable(state: dict[int, dict]) -> None:
    """Refuse ANY shard the loop cannot safely act on.

    The loop's rule is: skip TERMINAL_STATES, reconnect on ``submitted`` WITH a
    call id, otherwise spawn. That last "otherwise" is the danger, and an
    earlier revision guarded only ``intent`` against it — so a ``submitted``
    record whose call id was missing, or a state with a typo in it, fell
    straight through to a fresh spawn beside a container that may still be
    running. That is the two-A100 case, reached from the very file this
    module's own recovery message tells an operator to hand-edit at 3am.

    Two distinct hazards, both ending here:
      * ``intent`` with no call id — the one hole write-before-spawn cannot
        close. A container may exist and its id is gone either way.
      * anything else non-terminal — either a hand-edit that dropped the call
        id, or a state this code does not know about. Both mean "the loop
        would guess", and guessing costs an A100.
    """
    stuck = []
    for index, rec in sorted(state.items()):
        st = rec.get("state")
        if st in TERMINAL_STATES:
            continue
        if st == "submitted" and rec.get("call_id"):
            continue
        stuck.append((index, rec))
    if not stuck:
        return
    lines = "\n".join(
        f"  shard {i}: state={r.get('state')!r} job_id={r.get('job_id')!r} "
        f"call_id={r.get('call_id')!r}" for i, r in stuck)
    raise SystemExit(
        "refusing to resume: the ledger holds shards this driver cannot act "
        "on safely, so a container may be running that it can no longer "
        f"reach.\n{lines}\n"
        f"Check `modal app logs {APP}` for those job ids. Once you know, "
        'append EITHER {"index": N, "state": "failed"} to write the shard '
        'off, OR {"index": N, "state": "submitted", "call_id": "fc-..."} '
        "to reconnect — the call_id is required, a submitted record without "
        "one is refused for the same reason as an intent.")


# --- lock -------------------------------------------------------------------

def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - defensive
        return "?"


def default_lock_path() -> Path:
    """GLOBAL, not per-outdir.

    The invariant is "one A100 at a time", and that is a property of the
    ACCOUNT, not of a directory. An outdir-scoped lock let two drivers with
    two ``--outdir`` values both spawn — and since ``--outdir`` defaults to a
    RELATIVE path, the same command in two shells with different working
    directories already bypassed it. Starting a clean second campaign is also
    the obvious operator move on a run that looks stuck, so this is the likely
    violation, not the exotic one.

    Overridable so tests (and anyone who genuinely has a second GPU) can opt
    out deliberately rather than by accident.
    """
    override = os.environ.get("PROTEINA_DRIVER_LOCK")
    if override:
        return Path(override)
    return Path.home() / f".{APP}-shard-driver.lock"


class DriverLock:
    """Exclusive lock. Two drivers means two A100s — the exact thing the
    sequential design exists to prevent, and nothing downstream notices."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: int | None = None

    def __enter__(self) -> "DriverLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.path),
                              os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = ""
            try:
                held = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            raise SystemExit(
                f"another driver holds {self.path}\n  {held}\n"
                "Two drivers means two A100s. A lock left by a hard kill looks "
                "identical to a live one from here, so CHECK the pid above is "
                "gone before deleting the file.") from None
        os.write(self.fd, f"pid={os.getpid()} host={_hostname()} "
                          f"started={time.strftime('%F %T')}".encode())
        os.fsync(self.fd)
        return self

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except OSError:
            pass


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

    Chain C is the designed binder on this path: the target occupies A and B
    (``target_chain: "A,B"``) and the binder is appended after them. A zero
    here therefore means the layout changed, which is why _check_lengths
    treats zeros as mis-aimed rather than ignoring them.
    """
    n = 0
    for line in pdb_bytes.decode("ascii", "replace").splitlines():
        if line.startswith("ATOM") and line[21:22] == "C" \
                and line[12:16].strip() == "CA":
            n += 1
    return n


def _shard_dir(outdir: Path, index: int) -> Path:
    return outdir / f"shard_{index:03d}"


def _write_shard_rows(shard_dir: Path, rows: list[dict]) -> None:
    """Per-shard CSV, OVERWRITTEN not appended.

    This is what makes a re-harvest idempotent. The previous revision appended
    straight to one campaign-wide manifest, so a crash between the CSV write
    and the ledger write left the shard looking uncollected, and the resume
    appended all 64 rows a second time — silently double-counting a shard in
    the length study. Rewriting one shard's own file cannot do that.
    """
    tmp = shard_dir / "rows.csv.tmp"
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(shard_dir / "rows.csv")


def rebuild_manifest(outdir: Path) -> int:
    """Concatenate every shard's rows.csv into the campaign manifest.

    Derived, never appended to, so it is always consistent with the shard
    files and a re-harvest cannot duplicate rows into it.
    """
    manifest = outdir / "manifest.csv"
    tmp = outdir / "manifest.csv.tmp"
    total = 0
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        for shard in sorted(outdir.glob("shard_*/rows.csv")):
            with shard.open(encoding="utf-8", newline="") as src:
                for row in csv.DictReader(src):
                    writer.writerow(row)
                    total += 1
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(manifest)
    return total


def _harvest(out: dict, item: dict, job_id: str, call_id: str,
             outdir: Path) -> tuple[list[dict], int]:
    """Persist this shard's result, write its PDBs, return its manifest rows.

    ``smoke_result.json`` is written FIRST, before any candidate is parsed, so
    a paid shard's scores and error detail survive even if row-building then
    raises. run_pipeline deliberately keeps scores and ranks on a FAILED
    result, and every pre-GPU refusal lands there too, so that file is the
    diagnosis.

    A candidate with no ``pdb_content_b64`` (dropped by the inline size cap)
    still gets a row — its A100-computed scores are real and exist nowhere
    else. Only ``pdb_file`` is left empty.
    """
    smoke = (out or {}).get("smoke_result") or {}
    shard_dir = _shard_dir(outdir, item["index"])
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "smoke_result.json").write_text(
        json.dumps(smoke, indent=2), encoding="utf-8")

    rows, with_atoms = [], 0
    used_names: set[str] = set()
    for position, cand in enumerate(smoke.get("candidates") or [], start=1):
        scores = cand.get("scores") or {}
        blob = cand.get("pdb_content_b64")
        # `or position` not `.get("rank", ...)`: the key is present-and-None
        # on a malformed candidate, and dict.get returns that None rather than
        # the default, which then raises inside the format spec.
        rank = cand.get("rank") or position
        pdb_file, length = "", ""
        if blob:
            raw = base64.b64decode(blob)
            # Deduplicated. Mapping every malformed candidate onto one number
            # made two of them collide, and the SECOND silently overwrote the
            # first: one file on disk, two manifest rows pointing at it, and a
            # row claiming a 55 aa design beside 77 aa coordinates. A
            # mislabelled structure is worse than a missing one in a study
            # whose entire output is length versus quality.
            name = f"design_{int(rank):03d}.pdb"
            if name in used_names:
                name = f"design_{int(rank):03d}_dup{position:02d}.pdb"
            used_names.add(name)
            dest = shard_dir / name
            dest.write_bytes(raw)
            # posix separators: the manifest is read on other machines and a
            # backslash path is not portable.
            pdb_file = dest.relative_to(outdir).as_posix()
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
    _write_shard_rows(shard_dir, rows)
    return rows, with_atoms


def _check_lengths(rows: list[dict], item: dict) -> str:
    """Did the requested bin actually reach the model?

    The campaign's entire premise is that ``binder_length`` steers the design.
    If it silently does not — the field dropped, the pair mis-parsed, chain C
    not the binder — every shard still returns 64 plausible designs and the
    only symptom is lengths that ignore the bin. Unattended that is 4.7 days
    and $285 of designs at the wrong lengths, discoverable only by someone
    opening the manifest afterwards.

    Returns "" when the shard looks right, else a description.
    """
    lengths = [r["binder_length"] for r in rows if r["binder_length"] != ""]
    if not lengths:
        return "no design carried coordinates, so no length could be checked"
    lo, hi = item["bin"]
    outside = [n for n in lengths
               if not (lo - LENGTH_SLACK_AA <= n <= hi + LENGTH_SLACK_AA)]
    # ANY design outside the bin, not a majority. Upstream samples within
    # [lo, hi] and the count comes off the returned coordinates, so a correct
    # shard has none - a ">50%" rule let exactly half a shard sit 36 aa off
    # target and still report clean.
    if outside:
        return (f"{len(outside)}/{len(lengths)} designs fall outside the "
                f"requested bin [{lo}, {hi}] (+/-{LENGTH_SLACK_AA}); "
                f"median realised length {statistics.median(lengths):.0f}")
    return ""


# --- the loop ---------------------------------------------------------------

def _trailing_barren(plan: list[dict], state: dict[int, dict]) -> int:
    """How many shards in a row, most recent first, delivered no designs.

    Computed from the ledger rather than a loop counter so a resume inherits
    the streak: three failures then a restart is still three failures, and
    without this the operator's instinctive "just run it again" resets the only
    guard standing between a systemic fault and the whole budget.
    """
    streak = 0
    for item in reversed(plan):
        st = state.get(item["index"], {}).get("state")
        if st is None:
            continue                      # not attempted yet
        if st == "collected":
            break
        if st in TERMINAL_STATES:         # empty / failed / harvest_error
            streak += 1
        else:
            break
    return streak


def _spent_usd(state: dict[int, dict], *, ceiling: bool = False) -> float:
    """Every shard that reached ``intent`` may have been billed, and every one
    that reached ``submitted`` certainly was, whether or not it then produced
    designs. Counting only successes would let a run of failures walk straight
    through the budget ceiling."""
    per = shard_usd_ceiling() if ceiling else shard_usd()
    return sum(per for rec in state.values()
               if rec.get("state") in BILLED_STATES)


def _collect(call, item, job_id, call_id, outdir, ledger,
             timeout: int) -> str:
    """Block on one call, land its results, and record the outcome.

    Returns the new state, or ``"timeout"`` — which is NOT a state and NOT a
    failure. A timeout means the container may still be running and billing,
    so the ledger is deliberately left at ``submitted`` (a resume reconnects)
    and the caller must stop rather than spawn alongside it.
    """
    import modal.exception

    try:
        out = call.get(timeout=timeout)
    except (modal.exception.TimeoutError, TimeoutError) as exc:
        # NOTE modal.exception.TimeoutError does NOT subclass the builtin — its
        # bases are (modal.exception.Error, Exception) — so catching only the
        # builtin would silently miss every Modal timeout and fall through to
        # the failure branch below, writing off a live container.
        print(f"[shard {item['index']:03d}] TIMED OUT after {timeout}s: "
              f"{type(exc).__name__}. The container may still be RUNNING and "
              f"billing; call id {call_id} is kept in the ledger.",
              file=sys.stderr)
        return "timeout"
    except Exception as exc:  # noqa: BLE001 — one shard must not kill the run
        print(f"[shard {item['index']:03d}] collect FAILED: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        ledger_append(ledger, {"index": item["index"], "state": "failed",
                               "error": f"{type(exc).__name__}: {exc}"})
        return "failed"

    # Harvesting is guarded separately. It runs AFTER the money is spent, and
    # an exception here (a malformed candidate, a truncated blob, a full disk)
    # used to propagate out of run_campaign and end the campaign — leaving the
    # shard at `submitted` so the resume reconnected and re-crashed on the same
    # shard, forever, unattended.
    try:
        rows, with_atoms = _harvest(out, item, job_id, call_id, outdir)
    except Exception as exc:  # noqa: BLE001
        print(f"[shard {item['index']:03d}] HARVEST FAILED: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        ledger_append(ledger, {"index": item["index"], "state": "harvest_error",
                               "error": f"{type(exc).__name__}: {exc}"})
        return "harvest_error"

    smoke = (out or {}).get("smoke_result") or {}
    state = "collected" if with_atoms else "empty"
    # Called even with no coordinates: an all-cap-dropped shard is exactly
    # when nobody is looking, and _check_lengths says so rather than letting
    # the ledger record a silent clean pass.
    mismatch = _check_lengths(rows, item)
    ledger_append(ledger, {
        "index": item["index"], "state": state,
        "exit_code": (out or {}).get("exit_code"),
        "status": smoke.get("status"),
        "designs": len(rows), "with_atoms": with_atoms,
        "runtime_seconds": smoke.get("runtime_seconds"),
        "length_mismatch": mismatch or None,
    })
    # NOT allowed to end the campaign. The manifest is DERIVED from the shard
    # files, so a failure here loses nothing that is not rebuildable on the
    # next shard or at the end - whereas letting it propagate killed the run
    # on a routine operator action: os.replace over a destination another
    # process holds open raises PermissionError on Windows, so opening
    # manifest.csv in Excel to check progress on day 2 stopped the campaign,
    # and each restart advanced exactly one shard.
    try:
        rebuild_manifest(outdir)
    except OSError as exc:
        print(f"[shard {item['index']:03d}] manifest rebuild deferred "
              f"({type(exc).__name__}: {exc}); shard rows are safe on disk",
              file=sys.stderr)
    print(f"[shard {item['index']:03d}] {state}: {with_atoms}/{len(rows)} "
          f"with coordinates, {smoke.get('runtime_seconds')}s, "
          f"status={smoke.get('status')}")
    if mismatch:
        print(f"[shard {item['index']:03d}] LENGTH MISMATCH: {mismatch}",
              file=sys.stderr)
    return state


def run_campaign(args) -> int:
    # Checked HERE as well as in main(). A test drove main() with a real argv
    # and no fakes, relying entirely on main()'s gate; when a mutation removed
    # that gate the test spawned against the real Modal Function and wrote a
    # live ledger into the repo tree. A spend gate with exactly one enforcement
    # point is one refactor away from not existing.
    if not getattr(args, "yes", False):
        raise SystemExit("run_campaign requires args.yes")
    plan = build_plan()
    _validate_plan(plan)

    if args.timeout <= CONTAINER_CEILING_S:
        raise SystemExit(
            f"--timeout {args.timeout}s is at or below the {CONTAINER_CEILING_S}s "
            "container ceiling, so the driver could give up on a container "
            "Modal is still running. Use a larger value.")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    with DriverLock(default_lock_path()):
        return _run_locked(args, plan, outdir)


def _run_locked(args, plan: list[dict], outdir: Path) -> int:
    ledger = outdir / "ledger.jsonl"

    state = ledger_replay(ledger)
    _verify_plan_matches_ledger(plan, state)
    _refuse_unresumable(state)

    target = Path(args.target or os.environ.get("PROTEINA_TARGET_PDB")
                  or DEFAULT_TARGET)
    if not target.is_file():
        raise SystemExit(f"target structure not found: {target}")

    _load_env_and_path()
    import modal

    fn = modal.Function.from_name(APP, FN)
    done = {i for i, r in state.items() if r.get("state") in TERMINAL_STATES}
    print(f"[plan] {len(plan)} shards x {DESIGNS_PER_SHARD} designs = "
          f"{len(plan) * DESIGNS_PER_SHARD}; {len(done)} already finished")
    print(f"[budget] ${_spent_usd(state, ceiling=True):.2f} of "
          f"${args.budget:.2f} (priced at the ${USD_PER_SECOND_CEILING * 3600:.2f}/hr "
          "upper bound)")

    for item in plan:
        index = item["index"]
        prior = state.get(index, {})

        if prior.get("state") in TERMINAL_STATES:
            continue

        # PAID WORK ALREADY IN FLIGHT. Reconnect rather than resubmit: the
        # container is running (or has finished) and its call id is the only
        # way to reach the designs it produced. No budget check — that money
        # is already spent, and refusing here would strand it.
        if prior.get("state") == "submitted" and prior.get("call_id"):
            print(f"[shard {index:03d}] reconnecting to in-flight "
                  f"{prior['call_id']}")
            result = _collect(
                modal.FunctionCall.from_id(prior["call_id"]), item,
                prior.get("job_id", ""), prior["call_id"], outdir,
                ledger, args.timeout)
            if result == "timeout":
                return _stop_on_timeout(index)
            state = ledger_replay(ledger)
            continue

        # Checked BEFORE the spawn, not after the collect, so it also gates a
        # RESUME. Evaluated after the collect only, three failures followed by
        # the operator's instinctive "just run it again" would spend a fourth
        # shard before the guard noticed — and the reconnect branch above is
        # deliberately upstream of this, because that money is already spent.
        barren = _trailing_barren(plan, state)
        if barren >= MAX_CONSECUTIVE_BARREN:
            last = _shard_dir(outdir, index - 1) / "smoke_result.json"
            print(f"[stop] {barren} shards in a row delivered no designs. "
                  "That is a systemic fault, not bad luck — continuing would "
                  f"spend the rest of the budget reproducing it.\n"
                  f"       Read {last} for the diagnosis, then re-run to "
                  "resume once it is fixed.", file=sys.stderr)
            return 5

        spent = _spent_usd(state, ceiling=True)
        if spent + shard_usd_ceiling() > args.budget:
            print(f"[budget] STOP before shard {index}: ${spent:.2f} spent, "
                  f"next shard up to ${shard_usd_ceiling():.2f}, ceiling "
                  f"${args.budget:.2f}", file=sys.stderr)
            return 3

        lo, hi = item["bin"]
        # Fresh id per shard, always: the seed is derived from it, so a reused
        # id re-runs an identical search AND overwrites that run's raw archive.
        job_id = (f"proteina-sweep-{lo}-{hi}-r{item['round']:02d}-"
                  f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
        # Re-staged per shard. presigned_input_url expires in 7200 s and a
        # shard runs ~85 min, so one URL cannot safely cover the next shard.
        # DELIBERATELY BEFORE the intent record: this is an S3 upload, it is
        # fallible, and it runs 80 times over 4.7 days. Writing intent first
        # meant one transient upload failure left an orphan intent for a
        # container that was never created, wedging the resume into a hard
        # refusal whose only documented recovery is hand-editing the ledger.
        url = _stage_target(job_id, target)
        payload = build_payload(
            url, preset=args.preset, nsamples=NSAMPLES, replicas=REPLICAS,
            job_id=job_id, binder_length=(lo, hi))

        # Recorded in the LAST statement before the spawn, so the window it
        # covers is the spawn itself and nothing else. If the driver dies
        # between spawn returning and the call id being written, this is the
        # only trace that a container may exist; _refuse_unresumable turns it
        # into a stop rather than a silent double-charge.
        ledger_append(ledger, {
            "index": index, "round": item["round"], "bin": item["bin"],
            "state": "intent", "job_id": job_id,
        })
        call = fn.spawn(payload)
        # BEFORE waiting. See the module docstring.
        ledger_append(ledger, {
            "index": index, "state": "submitted", "job_id": job_id,
            "call_id": call.object_id,
        })
        print(f"[shard {index:03d}] bin {lo}-{hi} -> {call.object_id}")

        result = _collect(call, item, job_id, call.object_id, outdir,
                          ledger, args.timeout)
        if result == "timeout":
            return _stop_on_timeout(index)
        state = ledger_replay(ledger)

    final = ledger_replay(ledger)
    ok = sum(1 for r in final.values() if r.get("state") == "collected")
    try:
        rows = rebuild_manifest(outdir)
    except OSError as exc:
        rows = -1
        print(f"[done] manifest rebuild FAILED ({type(exc).__name__}: {exc}). "
              "Per-shard rows.csv files are intact; re-run to rebuild.",
              file=sys.stderr)
    print(f"\n[done] {ok}/{len(plan)} shards delivered designs; {rows} rows; "
          f"~${_spent_usd(final):.2f} spent (best estimate); "
          f"manifest at {outdir / 'manifest.csv'}")
    return 0


def _stop_on_timeout(index: int) -> int:
    """Stopping is the point.

    Spawning the next shard while a timed-out container may still hold the GPU
    is two A100s, which is what the one-GPU constraint forbids, and the failure
    compounds: each new shard queues behind the stale one, making the next
    timeout likelier. The shard stays `submitted`, so re-running the driver
    reconnects to it once it finishes.
    """
    print(f"[stop] shard {index} timed out and may still be running. NOT "
          "spawning the next shard — that would put two A100s in flight.\n"
          "       Re-run the driver to reconnect once it finishes, or check "
          "`modal app logs ranomics-proteina-prod`.", file=sys.stderr)
    return 4


def cmd_dry_run(args) -> int:
    """Never contacts Modal. Prints the plan, the projection and one payload."""
    plan = build_plan()
    _validate_plan(plan)
    total = len(plan) * DESIGNS_PER_SHARD
    secs = len(plan) * shard_seconds()
    print(f"bins            : {BINS}  (disjoint)")
    print(f"shards/bin      : {SHARDS_PER_BIN}")
    print(f"designs/shard   : {DESIGNS_PER_SHARD} "
          f"(nsamples {NSAMPLES} x replicas {REPLICAS})")
    print(f"shards          : {len(plan)}")
    print(f"designs         : {total}")
    print(f"per shard       : {shard_seconds() / 60:.0f} min, "
          f"${shard_usd():.2f} est / ${shard_usd_ceiling():.2f} max")
    print(f"projected total : {secs / 3600:.1f} h ({secs / 86400:.2f} d)")
    print(f"cost estimate   : ${secs * USD_PER_SECOND:.2f} at "
          f"${USD_PER_SECOND * 3600:.2f}/hr")
    print(f"cost ceiling    : ${secs * USD_PER_SECOND_CEILING:.2f} at "
          f"${USD_PER_SECOND_CEILING * 3600:.2f}/hr "
          "(the budget guard prices at this)")
    worst = secs * USD_PER_SECOND_CEILING
    print(f"budget ceiling  : ${args.budget:.2f}"
          + ("  <-- BELOW THE WORST CASE, the run will stop early"
             if args.budget < worst else "  (covers the worst case)"))
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
    ap.add_argument("--run", action="store_true",
                    help="SPENDS GPU MONEY; requires --yes")
    ap.add_argument("--yes", action="store_true",
                    help="confirm that --run may spend up to --budget")
    ap.add_argument("--budget", type=float, default=350.0,
                    help="hard USD ceiling, priced at the WORST rate the "
                         "measurement allows; refuses to start a shard that "
                         "would cross it. An estimate, not a Modal billing "
                         "limit (default 350)")
    ap.add_argument("--outdir", default="proteina_sweep_out")
    ap.add_argument("--preset", default="protein_binder")
    ap.add_argument("--target", default="")
    ap.add_argument("--timeout", type=int, default=9000,
                    help="seconds to wait on one shard; must exceed the "
                         f"{CONTAINER_CEILING_S}s container ceiling so a slow "
                         "Modal queue is not mistaken for a dead shard")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.dry_run:
        return cmd_dry_run(args)
    if args.run:
        if not args.yes:
            secs = len(build_plan()) * shard_seconds()
            raise SystemExit(
                f"--run would spend up to "
                f"${secs * USD_PER_SECOND_CEILING:.2f} over "
                f"{secs / 86400:.1f} days of A100 time. Re-run with --yes to "
                "confirm, or use --dry-run to see the plan.")
        return run_campaign(args)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
