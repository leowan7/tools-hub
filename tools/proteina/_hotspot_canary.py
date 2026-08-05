"""Custom-target + hotspot canary for Proteina-Complexa.

    modal run tools/proteina/_hotspot_canary.py --phase 0
    modal run tools/proteina/_hotspot_canary.py --phase 1 --target-pdb <path>
    modal run tools/proteina/_hotspot_canary.py --phase 2 --target-pdb <path>

WHY THIS EXISTS. Upstream's ``load_target_from_pdb`` matches hotspots with

    if f"{atom.chain_id}{atom.res_id}" in target_hotspots: mask[idx] = True

against a zero-initialised mask. A token matching nothing is dropped SILENTLY:
the search then runs unconstrained and its output — design count, reward CSV,
PDBs, exit code — is indistinguishable from a run that honoured every hotspot.
No amount of "it completed successfully" is evidence the feature works. Only a
measurement of WHERE the binders landed is, which is what phase 2 does.

Runs against the SAME image and seeded Volumes as the prod app and calls
``build_target_add_cmd`` / ``build_design_cmd`` from the deployed
``run_pipeline.py`` rather than re-implementing them, so what is measured is
what production runs. Nothing is uploaded, billed through the wallet, or
written to prod state. (The image adds one extra inert file over prod's —
``_canary_scoring.py`` — so the Dockerfile layers are still shared and cached,
but the final image hash is not byte-identical to prod's.)

WHERE THE LOGIC LIVES. Everything that decides anything — geometry, scoring,
negative-patch selection, thresholds, verdicts — is in ``_canary_scoring.py``,
which imports no third-party package at all and is covered by
``tests/test_proteina_canary.py``. This file keeps only the Modal app, the
container-side function bodies and the entrypoint. That split exists because
this module imports ``modal`` at top level, so in an environment without the
``modal`` package NOT ONE of its lines is reachable by pytest — and for a
harness whose entire job is to be the last gate before real GPU spend, "never
tested" is not an acceptable property.

PHASES, cheapest first — stop at the first failure.

  0  FREE, CPU-only, no GPU compute. (a) a deliberately wrong hotspot must be
     refused pre-GPU; (b) registering twice in one container must work, proving
     --force clears the interactive overwrite prompt that EOFs on closed stdin.
     Phase 0 is the direct regression test for the silent-drop failure and it
     costs nothing, so it runs first and always.

  1  ~$4. One real shard against a custom target. Answers the two things the
     source could not settle — whether per-design PDBs are binder-only or
     binder+target complexes, and WHICH CHAIN LABEL upstream gives each side —
     and asserts the resolved Hydra config actually SELECTED our registered key
     (``generation.task_name``) and composed OUR hotspots. That assertion is
     geometry-independent and is the cheapest real proof the wiring works.

  2  ~$12. Three shards, same PDB, same seed, hotspots the ONLY variable:
     positive (a known-designable patch), negative (a patch >=25 A from the
     nearest positive hotspot), null (no hotspots at all). The null control is
     the one that catches "we passed it and upstream ignored it" — if a
     no-hotspot run scores the same as a hotspot run, the argument is a no-op
     and the feature is a lie.

EVERY SPEC IS PROVED RESOLVABLE LOCALLY BEFORE A SHARD IS SPAWNED. Phases 1 and
2 run ``pdb_ca_residues`` / ``select_residues`` / ``missing_hotspots`` — pure,
local, the same functions the container uses — over the positive AND negative
specs first. A token that matches no residue is a free refusal; discovering it
in-container costs three A100 startups to learn what a membership test knew.

THE OUTPUT'S CHAIN LABELLING IS NEVER ASSUMED. Because phase 1 exists to
DISCOVER the output convention, phase 2 cannot presume it: every design is
matched against the input target's residues before any geometry is scored, and
one that does not match is UNSCORABLE. Scoring it anyway measures the binder
against itself and reports a perfect hotspot recall — a fabricated PASS.

VERDICTS ARE THREE-VALUED. PASS (exit 0), FAIL (exit 1), INCONCLUSIVE (exit 3).
"Could not measure it" is a distinct answer from "measured it and it is wrong":
reporting the first as the second condemns a feature that may work fine, and
reporting it as a PASS blesses one nobody measured. Whether the per-design
outputs are complexes at all is exactly what phase 1 exists to discover, so
phase 2 must be able to say "unmeasurable" out loud.

Delete before flag-on; it is not imported by the prod app.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_TOOL = "proteina"
_DOCKERFILE = f"tools/{_TOOL}/Dockerfile.modal"
_RUN_PIPELINE_LOCAL = f"tools/{_TOOL}/run_pipeline.py"
_RUN_PIPELINE_REMOTE = "/opt/proteina/run_pipeline.py"
_SCORING_LOCAL = f"tools/{_TOOL}/_canary_scoring.py"
_SCORING_REMOTE = "/opt/proteina/_canary_scoring.py"
_GPU = "A100-80GB"
_MAX_SESSION_S = 7200
# Slack on top of the function timeout so a shard that is being reaped is still
# collected as a timeout rather than as a local wait that gave up first.
_COLLECT_TIMEOUT_S = _MAX_SESSION_S + 600

# HOW MANY DESIGNS EACH SHARD ORDERS. Named here, in one place, because the
# product is BOTH the Hydra override we send and the denominator the verdicts
# are owed: ``run_shard`` passes these to ``build_design_cmd`` and reports
# ``_NSAMPLES * _REPLICAS`` as ``n_designs_expected``, so the two cannot drift.
# Written as a constant rather than inline for exactly that reason — the shipped
# code passed ``nsamples=4, replicas=2`` inline and nothing downstream knew the
# number, so a shard that returned ONE post-filter file reported "1/1 designs
# on the patch (needed 1)" and PASSED. See ``cs.designs_expected``.
#
# This is the same product production derives (``run_pipeline.designs_total =
# nsamples * replicas``); changing either constant moves the request and the
# floor together.
_NSAMPLES = 4
_REPLICAS = 2


def _load_by_path(name: str, path: str):
    """importlib's documented load-from-path recipe, INCLUDING the sys.modules
    registration.

    Registering before ``exec_module`` is not optional housekeeping. Anything in
    the loaded module that introspects its own module at class-creation time —
    ``@dataclass`` is the common one, via ``sys.modules[cls.__module__]`` — hits
    ``None`` and raises ``AttributeError: 'NoneType' object has no attribute
    '__dict__'``. Skipping it works right up until the loaded module grows its
    first dataclass, and then it fails in the container, on the GPU, with the
    money already committed.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _load_scoring():
    """Import the pure scoring module BY PATH, so the same code runs on both
    sides without depending on Modal's source-inclusion rules or on
    ``tools/__init__.py`` being importable.

    Sibling first (the local entrypoint), then the image path (the container,
    where ``add_local_file(copy=True)`` has baked it in at ``/opt/proteina``).
    """
    candidates = [str(Path(__file__).resolve().parent / "_canary_scoring.py"),
                  _SCORING_REMOTE]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return _load_by_path("_canary_scoring", candidate)
    raise RuntimeError(f"_canary_scoring.py not found; looked in {candidates}")


cs = _load_scoring()


# ===========================================================================
# The console, made incapable of killing the run
# ===========================================================================
#
# BEFORE ``import modal``, AND BEFORE ANY OTHER STATEMENT THAT COULD PRINT.
# Upstream's ``complexa target add`` prints "  <check mark> Updated target
# '<key>'" and "  <round pushpin> Saved to: ...". ``run_shard`` runs it INSIDE
# the container, i.e. after the A100s are already billing; modal streams that
# output to the local console; and on a Windows cp1252 console the write raises
# ``UnicodeEncodeError: 'charmap' codec can't encode character '✓'``. That
# killed ``--phase 0`` on 2026-08-04. In phase 2 the same raise arrives between
# ``spawn`` and ``get`` with three A100s running, and a local entrypoint that
# dies there leaves them billing to completion or to _MAX_SESSION_S = 7200 s:
# ~$12 to $38 bought and thrown away.
#
# The raise happens inside code this repo does not own, so there is no call
# site here to wrap. ``harden_stream`` mutates the stream's error handler IN
# PLACE, which fixes modal's log pump, rich's renderer and the interpreter's
# own traceback printer at once — every one of them holds its own reference to
# this object. ``_emit`` below is the second layer, for the strings this module
# formats itself; neither layer makes the other redundant. See the section
# comment in ``_canary_scoring.py`` for the full argument.
#
# NOT ``PYTHONIOENCODING=utf-8``: that works, and an operator forgets it
# exactly once.
sys.stdout = cs.harden_stream(sys.stdout)
sys.stderr = cs.harden_stream(sys.stderr)


def _emit(message: object = "", *, flush: bool = False) -> None:
    """``print`` that CANNOT raise. Every print in this module goes through it.

    Two things kill a canary run at the console, and both happen after the
    money is committed: an unencodable character (upstream's tick, a container
    error string quoting it, a traceback carrying it) and a stream that has
    gone away. Neither is a reason to abandon three running A100s, so the text
    is made encodable first and anything left over is swallowed — a failed
    print is not a verdict.

    The swallow is deliberate and is the whole point: ``_cancel_outstanding``
    prints per shard, and a print that raises there stops the remaining
    containers from being terminated. Losing a line of output costs nothing;
    losing the cancel costs the rest of the session's GPU time.
    """
    stream = sys.stdout
    try:
        stream.write(cs.safe_text(message, getattr(stream, "encoding", None))
                     + "\n")
        if flush:
            stream.flush()
        return
    except Exception:  # noqa: BLE001 — see the docstring; nothing may escape
        pass
    try:
        # stdout is unusable, not merely picky. One ASCII-only attempt at the
        # other stream, so a silent canary is not the first symptom.
        sys.stderr.write(cs.safe_text(message, "ascii") + "\n")
    except Exception:  # noqa: BLE001
        pass


import modal  # noqa: E402 — imported only after the console cannot kill us

# Same Dockerfile => the expensive layers stay cached. Same Volume names => the
# already-seeded weights/rewards. _canary_scoring.py rides along so the
# container scores designs with the SAME code the offline tests cover.
image = (
    modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)
    .add_local_file(_RUN_PIPELINE_LOCAL, _RUN_PIPELINE_REMOTE, copy=True)
    .add_local_file(_SCORING_LOCAL, _SCORING_REMOTE, copy=True)
)
weights = modal.Volume.from_name("proteina-weights")
rewards = modal.Volume.from_name("proteina-rewards")

app = modal.App("ranomics-proteina-hotspot-canary")


# ===========================================================================
# Remote helpers
# ===========================================================================


def _load_rp():
    sys.path.insert(0, "/opt/proteina")
    return _load_by_path("rp", _RUN_PIPELINE_REMOTE)


def _poll_vram(stop: threading.Event, out: dict) -> None:
    """Device-wide peak VRAM, not per-process — the shard is the only tenant."""
    peak = 0
    while not stop.is_set():
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.strip().splitlines():
                peak = max(peak, int(line.strip()))
        except Exception:
            pass
        stop.wait(5)
    out["peak_vram_mb"] = peak


def _stage_dir(rp) -> Path:
    """WHERE PRODUCTION STAGES, read off ``run_pipeline`` rather than restated.

    Derived, not copied, so the two cannot drift: if prod's staging directory
    moves, the canary follows it in the same commit or not at all.
    """
    return Path(rp._HUB_TARGET_DIR)


def _stage(rp, pdb_text: str, key: str) -> Path:
    """Stage the target EXACTLY the way ``prepare_custom_target`` does.

    THE CANARY MUST NOT EXERCISE A PATH PRODUCTION NEVER RUNS. It used to write
    ``/tmp/canary_targets/<label>.pdb`` — a directory prod never touches, under
    a stem ("phase1", "positive", "null") that is not the registry key — while
    ``prepare_custom_target`` writes ``$PROTEINA_HOME/hub_targets/<key>.pdb``
    and passes ``filename_stem=staged.stem``, i.e. the KEY. The single question
    this harness exists to answer is whether a target registered by this repo
    is really the one upstream designs against, and upstream matches on the
    literal strings in that record; testing it with a different directory and a
    different stem tests a request prod never makes.

    ``$PROTEINA_HOME/hub_targets`` is in the image's writable layer, NOT on a
    mounted Volume — the Dockerfile pre-creates only ``ckpts``, ``rewards`` and
    ``.cache``, and the first two are the Volume mount points — so this is a
    plain mkdir exactly as it is in prod, and nothing here is written to a
    Volume that would then persist between runs.
    """
    target_dir = _stage_dir(rp)
    target_dir.mkdir(parents=True, exist_ok=True)
    p = target_dir / f"{key}.pdb"
    p.write_text(pdb_text)
    return p


def _prune_staged(rp) -> list[str]:
    """Delete the PDBs this harness staged, and only those. Never raises.

    Was ``shutil.rmtree(_STAGE_DIR)``, which was safe only while the canary
    owned that directory outright. It now stages where PRODUCTION stages, so a
    blanket delete would remove files this harness did not write; the prune is
    prefix-based instead, for the same reason and with the same safety argument
    as ``prune_canary_records`` (``cs.canary_staged_pdbs`` carries it).
    """
    try:
        target_dir = _stage_dir(rp)
        if not target_dir.is_dir():
            return []
        removed = []
        for name in cs.canary_staged_pdbs(os.listdir(target_dir)):
            try:
                (target_dir / name).unlink()
            except OSError:
                continue
            removed.append(name)
        return removed
    except Exception as exc:  # noqa: BLE001 — hygiene must not fail the run
        _emit(f"[canary] could not prune the staged targets: {exc}", flush=True)
        return []


def _prune_registry(rp) -> list[str]:
    """Drop this harness's own records and staged PDBs from the container.

    Modal reuses warm containers and Hydra composes the whole registry on every
    run, so a canary that only ever appends leaves a growing file and a stale
    absolute target_path readable to the next tenant of the container. Mirrors
    ``modal_app.py::_clear_hub_targets``, narrowed to ``hub_canary*`` keys so it
    can never remove a curated benchmark target — and ``run_pipeline``'s own
    ``custom_target_key`` emits ``hub_`` + HEX, which can never begin with
    "canary", so a prod record can never match the prefix either. Never raises:
    hygiene must not fail the run.
    """
    _prune_staged(rp)
    try:
        if not os.path.isfile(rp._TARGETS_DICT):
            return []
        import yaml  # noqa: PLC0415 — rides in with OmegaConf, not a hub dep

        with open(rp._TARGETS_DICT, "r", errors="replace") as fh:
            data = yaml.safe_load(fh) or {}
        stale = cs.prune_canary_records(data)
        if not stale:
            return []
        with open(rp._TARGETS_DICT, "w") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
        _emit(f"[canary] pruned {len(stale)} canary record(s) from the registry",
              flush=True)
        return stale
    except Exception as exc:  # noqa: BLE001 — hygiene must not fail the run
        _emit(f"[canary] could not prune the targets registry: {exc}", flush=True)
        return []


# ===========================================================================
# Phase 0 — free, CPU only
# ===========================================================================


@app.function(image=image, timeout=900,
              volumes={"/opt/proteina/ckpts": weights, "/opt/proteina/rewards": rewards})
def phase0(pdb_text: str) -> dict:
    """No GPU. Two controls that need no model at all."""
    rp = _load_rp()
    _prune_registry(rp)
    results: dict = {}

    # The key FIRST, because the staged filename is derived from it — prod
    # writes ``hub_targets/<key>.pdb`` and registers ``--target-filename
    # <key>``, so the stem and the key are one thing, not two.
    key = cs.canary_task_key("phase0", 0)

    # (a) A hotspot that is not in the structure must be refused, and nothing
    #     may be executed — not the registration, not the design.
    staged = _stage(rp, pdb_text, key)
    residues, _ = rp.pdb_ca_residues(staged)
    chains = sorted({r[0] for r in residues})
    contig = rp.format_contig(rp.derive_segments(residues, chains))
    selected = rp.select_residues(residues, rp.parse_target_input(contig))
    missing = rp.missing_hotspots(selected, ["A99999"])
    results["typo_control"] = {
        "pass": missing == ["A99999"],
        "missing": missing,
        "detail": "a hotspot absent from the structure must be detected",
    }

    # (b) Register the same target twice in ONE container. Without --force the
    #     second add hits input("Overwrite? (y/N): "), EOFs on closed stdin and
    #     silently returns False. Warm containers make this the normal case.
    #
    #     The hotspot is taken from the SELECTED residues, not from
    #     residues[0] paired with chains[0] — those two need not describe the
    #     same residue (they do not when the first record in the file is not on
    #     the alphabetically-first chain), and a control that registers a
    #     hotspot which does not exist tests less than it looks like it does.
    if not selected:
        results["warm_container_control"] = {
            "pass": False,
            "detail": "the contig selected no residue, so no real hotspot exists",
        }
    else:
        hotspots = [f"{selected[0][0]}{selected[0][1]}"]
        cmd = rp.build_target_add_cmd(
            key=key, pdb_path=str(staged), filename_stem=staged.stem,
            contig=contig, hotspot_spec=hotspots, binder_length=[60, 120],
        )
        rcs, records = [], []
        for _ in range(2):
            rcs.append(rp.run_streaming(cmd, Path(rp.PROTEINA_HOME)))
            try:
                written = rp.read_targets_dict(rp._TARGETS_DICT)
            except Exception as exc:
                records.append({"error": str(exc)})
                continue
            records.append(written.get(key))
        expected = {
            "source": rp._HUB_SOURCE, "target_path": str(staged),
            "target_input": contig, "hotspot_residues": hotspots,
            "binder_length": [60, 120],
        }
        mismatches = [rp.registration_mismatch(r, expected) for r in records]
        results["warm_container_control"] = {
            "pass": rcs == [0, 0] and mismatches == [None, None],
            "exit_codes": rcs,
            "mismatches": mismatches,
            "hotspot": hotspots,
            "detail": "--force must clear the interactive overwrite prompt",
        }

    # Named controls, not "every dict in the result": all() over an empty or
    # renamed set is vacuously True, i.e. a green phase 0 that never ran.
    results["pass"] = cs.phase0_pass(results)
    _prune_registry(rp)
    _emit("[canary/phase0] " + json.dumps(results, indent=2, default=str), flush=True)
    return results


# ===========================================================================
# Phases 1 and 2 — one real shard each
# ===========================================================================


def _read_hydra_assertion(work_dir: Path, key: str, hotspot_spec: list[str],
                          contig: str) -> dict | None:
    """The geometry-independent proof, evaluated STRUCTURALLY.

    The original asked ``if key in json.dumps(resolved_config)``. That cannot
    distinguish "the search selected our target" from "our target is merely
    present in the file" — and ``binder_generate.yaml`` composes the ENTIRE
    targets registry into ``target_dict_cfg``, so our key is in the blob the
    instant it is registered, whether or not it was ever selected. The
    substring test therefore passes for a run that designed against a
    completely different target: a false positive on the one assertion phase 1
    spends $4 to make.
    """
    import yaml  # noqa: PLC0415

    paths = sorted(
        glob.glob(str(work_dir / "**/.hydra/config.yaml"), recursive=True),
        key=lambda p: os.path.getmtime(p), reverse=True,
    )
    best: dict | None = None
    for path in paths:
        try:
            data = yaml.safe_load(Path(path).read_text(errors="replace")) or {}
        except Exception:
            continue
        result = cs.hydra_assertion(data, key, hotspot_spec, contig)
        result["config_path"] = path
        result["n_configs_scanned"] = len(paths)
        if result.get("task_name_selected"):
            return result
        if best is None:
            best = result
    return best


def _mtime(path: str) -> float:
    """``getmtime`` that answers instead of raising. A file that vanished between
    the glob and the stat is simply the oldest candidate, not an exception on
    the diagnostics path."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _collect_run_logs(work_dir: Path) -> dict:
    """The tail of the log files UPSTREAM wrote, read IN THE CONTAINER.

    THE FILES ONLY EXIST IN HERE, which is the whole reason this is not done on
    the operator's side: the same mistake — shipping container paths back and
    re-opening them locally — is what cost phase 2 two of its three verdicts
    after the money was spent. Bytes come back, never paths to bytes.

    ``complexa design`` shells out to ``python -m proteinfoundation.generate``
    with its stdout and stderr redirected into
    ``logs/design_pipeline_<key>_<run>_<stamp>/generate.log``. Modal forwards
    this function's streams and nothing else, so that file is where the entire
    explanation of two failed live runs has been sitting. The sibling
    ``logs/design_pipeline_..._<stamp>.log`` is the other file upstream's own
    error message points at ("Check log for details"), so both are read.

    NEWEST FIRST, ALWAYS. Modal reuses warm containers and each run leaves its
    own directory behind, so "whatever glob returned first" is a coin flip
    between this run's log and a previous one's — and a stale log presented as
    the current one is worse than no log, because nothing in it says so.

    EVERY DECISION IS IN ``cs``. This function stats, globs and reads; which
    directory is newest, how much of each file survives, and what the total
    budget is are all in the covered module. Nothing here can fail the shard
    either: it runs after the GPU time is spent, and a diagnostics collector
    that raises turns an informative failure into an uninformative one.
    """
    try:
        matched: list[str] = []
        for pattern in cs.LOG_GLOBS:
            matched.extend(glob.glob(str(work_dir / pattern), recursive=True))
        matched = sorted(set(matched))

        newest_dir = cs.newest_path(
            (p, _mtime(p)) for p in matched if os.path.isdir(p))
        if newest_dir is not None:
            # THE SIBLING OF THE SELECTED RUN, derived from its name, not
            # "the newest file in logs/". Upstream writes ``<rundir>.log``
            # beside ``<rundir>/``, so the right one is knowable exactly — and
            # picking by mtime instead can pair this run's generate.log with a
            # PREVIOUS run's pipeline log whenever the two disagree by a
            # second, which is the same stale-evidence failure the newest-dir
            # selection exists to prevent, reintroduced one line later.
            sibling = f"{newest_dir}.log"
        else:
            # No run directory at all. Fall back to the newest matching file,
            # so a future upstream that stops creating the directory but still
            # writes the log is not silently unreadable.
            sibling = cs.newest_path(
                (p, _mtime(p)) for p in matched if os.path.isfile(p))

        # Budget order: the stage that failed twice, then the file upstream's
        # own message tells the operator to read, then the rest.
        wanted: list[str] = []
        if newest_dir is not None:
            wanted.append(os.path.join(str(newest_dir), cs.STAGE_LOG_NAMES[0]))
        if sibling is not None:
            wanted.append(str(sibling))
        if newest_dir is not None:
            # The later stages only if they exist — `generate` failing means
            # they never ran, and three FileNotFoundErrors are noise. The two
            # above are reported even when missing, because THAT is a finding.
            wanted.extend(
                p for p in (os.path.join(str(newest_dir), name)
                            for name in cs.STAGE_LOG_NAMES[1:])
                if os.path.isfile(p))

        files: list[tuple[str, object]] = []
        seen: set[str] = set()
        for path in wanted:
            if path in seen:
                continue
            seen.add(path)
            try:
                files.append((path, Path(path).read_bytes()))
            except OSError as exc:
                files.append((path, exc))

        return cs.build_log_report(
            globs=[str(work_dir / p) for p in cs.LOG_GLOBS],
            matched=matched, selected=newest_dir, files=files)
    except Exception as exc:  # noqa: BLE001 — diagnostics may never fail a shard
        return {"error": f"{type(exc).__name__}: {exc}"}


def _collect_tree(work_dir: Path) -> list[str]:
    """What upstream actually produced, across the WHOLE work directory.

    This used to glob ``inference/**/*`` and nothing else, so a run that died
    inside ``generate`` — before that directory ever had contents — reported
    ``tree: []``. That is literally true and completely uninformative, and it
    was the entire file-level evidence from two failed live runs. Upstream's
    logs, its Hydra output directory and everything else it writes all sit
    outside ``inference/``.

    The mounted Volumes are excluded and the cap is applied by rank rather than
    alphabetically; both decisions are in ``cs.select_tree_entries``, and both
    exist so the cap cannot evict the subtrees the listing is for.
    """
    try:
        found: list[str] = []
        for pattern in cs.TREE_GLOBS:
            for path in glob.glob(str(work_dir / pattern), recursive=True):
                try:
                    found.append(str(Path(path).relative_to(work_dir)))
                except ValueError:
                    found.append(path)
        return cs.select_tree_entries(found)
    except Exception as exc:  # noqa: BLE001 — diagnostics may never fail a shard
        return [f"<the file listing could not be built: {type(exc).__name__}: {exc}>"]


@app.function(image=image, gpu=_GPU, timeout=_MAX_SESSION_S,
              volumes={"/opt/proteina/ckpts": weights, "/opt/proteina/rewards": rewards})
def run_shard(pdb_text: str, label: str, hotspot_spec: list[str],
              contig: str, seed: int, binder_length: list[int],
              dump_tree: bool, cross_reference_spec: list[str] | None = None) -> dict:
    """One real custom-target shard. Registers, designs, then reports.

    ``cross_reference_spec`` is the POSITIVE patch, and every shard is scored
    against it in addition to its own hotspots. That cross score is what the
    negative and null verdicts are made of, and it is computed HERE, in the
    container, because the per-design PDBs only exist here: the original
    shipped container paths back to the local entrypoint and re-opened them
    there, which raises FileNotFoundError on the operator's laptop AFTER the
    ~$12 has already been spent, killing two of the three phase-2 verdicts.

    Nothing in the returned dict is a path the caller may open. The per-design
    key is named ``container_path`` for exactly that reason.

    WHICH CHAINS ARE THE TARGET IS VERIFIED, NOT ASSUMED. This function used to
    treat every chain of the INPUT PDB as target and every other chain in a
    design output as binder — with nothing checking that the output preserved
    the labelling, which the module docstring says outright is unknown (it is
    why phase 1 exists). A design that emitted the binder as the target's chain
    id inverted the roles silently and scored a perfect ``hotspot_recall`` off
    the binder's own contacts. Every design is now matched against the input
    target's residues by ``cs.score_design_file`` before any geometry is
    computed, and a design that does not match is returned UNSCORED with
    ``target_verified: False``, which reaches the verdicts as INCONCLUSIVE and
    can never become a PASS or a FAIL.

    ``chains`` below is the CONTIG's selection, not the input file's chain
    list, so it cannot disagree with the identity ``reference`` built from the
    same selection. It used to, and a ``--contig`` naming a subset of the
    input's chains then made every design unscorable (or, for a 2-chain input
    with a 1-chain contig, not even a complex) for ~$12.
    """
    rp = _load_rp()
    work_dir = Path(rp.PROTEINA_HOME)
    inference = work_dir / "inference"
    shutil.rmtree(inference, ignore_errors=True)
    inference.mkdir(parents=True, exist_ok=True)
    _prune_registry(rp)

    # The key is the staged file's STEM, so it is computed before the staging
    # rather than after it: that is the coupling production has
    # (``staged = target_dir / f"{key}.pdb"`` then ``filename_stem=staged.stem``)
    # and reproducing it is the point of the exercise.
    key = cs.canary_task_key(label, seed)
    staged = _stage(rp, pdb_text, key)
    residues, _ = rp.pdb_ca_residues(staged)
    input_chains = sorted({r[0] for r in residues})
    contig = contig or rp.format_contig(
        rp.derive_segments(residues, input_chains))
    cross_spec = list(cross_reference_spec or [])

    # Refuse here too rather than discovering it in the output. The predicate
    # is in the covered module; this only supplies the two missing-token lists.
    selected = rp.select_residues(residues, rp.parse_target_input(contig))
    refusal = cs.shard_spec_refusal(
        label,
        rp.missing_hotspots(selected, hotspot_spec),
        rp.missing_hotspots(selected, cross_spec))
    if refusal is not None:
        return refusal

    # THE CHAINS WE MAY CALL TARGET COME FROM THE CONTIG'S SELECTION, not from
    # every chain in the input file. The identity ``reference`` below is built
    # from ``selected``, and when the two were derived from different things a
    # ``--contig`` naming a subset of the input's chains made them disagree: a
    # design chain carrying an EXCLUDED input chain id had no reference entry,
    # so coverage fell under the floor and every design came back unscorable
    # (~$12 for an INCONCLUSIVE), and a 2-chain input with a contig on chain A
    # only turned an A+B design into "not a complex" because every present
    # chain was wanted.
    chains = cs.target_chains_from_selection(selected)

    add_cmd = rp.build_target_add_cmd(
        key=key, pdb_path=str(staged), filename_stem=staged.stem,
        contig=contig, hotspot_spec=hotspot_spec, binder_length=binder_length,
    )
    add_rc = rp.run_streaming(add_cmd, work_dir)
    written = rp.read_targets_dict(rp._TARGETS_DICT)
    mismatch = rp.registration_mismatch(written.get(key), {
        "source": rp._HUB_SOURCE, "target_path": str(staged),
        "target_input": contig, "hotspot_residues": list(hotspot_spec),
        "binder_length": [int(binder_length[0]), int(binder_length[1])],
    })
    if add_rc != 0 or mismatch:
        return {"label": label, "key": key,
                "error": f"registration failed (rc={add_rc}): {mismatch}"}

    cmd = rp.build_design_cmd(
        config_name="search_binder_local_pipeline", task_name=key, seed=seed,
        nsamples=_NSAMPLES, replicas=_REPLICAS, nsteps=None,
        run_name=f"canary_{label}", rf3_on=rp._rf3_enabled(),
    )
    _emit(f"[canary/{label}] {' '.join(cmd)}", flush=True)

    vram: dict = {}
    stop = threading.Event()
    poller = threading.Thread(target=_poll_vram, args=(stop, vram), daemon=True)
    poller.start()
    t0 = time.time()
    try:
        rc = subprocess.run(cmd, cwd=str(work_dir), stdout=sys.stdout,
                            stderr=sys.stderr, timeout=3600).returncode
    except subprocess.TimeoutExpired:
        rc = 124
    runtime_s = int(time.time() - t0)
    stop.set()
    poller.join(timeout=10)

    out: dict = {
        "label": label, "key": key, "contig": contig,
        "requested_hotspots": list(hotspot_spec),
        "cross_reference_hotspots": cross_spec,
        # THE DENOMINATOR THE VERDICTS ARE OWED, reported by the only code that
        # knows it. ``designs`` below is what SURVIVED: the glob skips
        # ``filtered_out_samples`` (upstream's own filter bucket) and drops
        # unreadable files, so counting it answers "how many were left", never
        # "how many did we order". Without this key a shard whose eight samples
        # upstream filtered to one returned ``1/1 designs on the patch (needed
        # 1)`` — a PASS, exit 0, and a green light for FLAG_TOOL_PROTEINA off
        # one design. Derived from the SAME constants the design command was
        # built with, immediately above, so it cannot describe a different run.
        "n_designs_expected": _NSAMPLES * _REPLICAS,
        "exit_code": rc, "runtime_s": runtime_s,
        "peak_vram_mb": vram.get("peak_vram_mb"),
        # The chains scored as target (the contig's) and every chain the input
        # file carried. They differ exactly when --contig names a subset, and
        # printing both is what makes that visible instead of inferable.
        "target_chains": chains,
        "input_chains": input_chains,
    }
    out["hydra"] = _read_hydra_assertion(work_dir, key, list(hotspot_spec), contig)

    # The residue IDENTITY of the target we uploaded, restricted to the contig's
    # selection. This is the reference every design's putative-target chain is
    # matched against; without it "target_chains" is an assumption about
    # upstream's output convention, and a wrong one fabricates a perfect score.
    #
    # ``selected`` rather than every residue in the file, because the contig is
    # what upstream is told to use. The asymmetry that follows is deliberate: a
    # design that CROPS the selection still verifies (its keys are all in the
    # reference), while one that adds residues we did not upload does not — the
    # contact set and the recall denominator would absorb geometry we cannot
    # account for. When ``contig`` is not supplied it is the full observed span,
    # so this is the whole target; when the operator narrows it by hand and the
    # output turns out to carry the uncropped chain, the designs come back
    # UNSCORABLE with the coverage number attached, which is the safe direction
    # and is visible in phase 1 for $4.
    selected_keys = set(selected)
    reference = {
        k: v for k, v in cs.ca_resnames(cs.heavy_atoms(pdb_text)).items()
        if k in selected_keys
    }
    out["n_reference_residues"] = len(reference)

    # Per-design geometry. Which files are complexes is what phase 1 discovers.
    designs = []
    for p in sorted(glob.glob(str(inference / "**/*.pdb"), recursive=True)):
        if "filtered_out_samples" in p:
            continue
        name = Path(p).name
        if name in ("target.pdb", "target_input"):
            continue
        try:
            text = Path(p).read_text(errors="replace")
        except OSError:
            continue
        entry = {
            # Named for the container on purpose: nothing on the local side may
            # ever open this. The whole reason phase 2 could not produce a
            # verdict was a local read of a path that only exists in here.
            "container_path": p, "name": name,
        }
        # Every decision — is this a complex, is the target really the target,
        # where did the binder land — is made by the covered pure module. This
        # loop only supplies bytes and collects the answer.
        entry.update(cs.score_design_file(
            text, set(chains), list(hotspot_spec), cross_spec, reference))
        designs.append(entry)
    out["designs"] = designs
    out["n_complexes"] = sum(1 for d in designs if d.get("is_complex"))
    out["n_target_verified"] = sum(1 for d in designs if d.get("target_verified"))
    out["n_target_unverified"] = len(cs.unverified_designs({"designs": designs}))
    # Medians over designs the SAME predicate the verdicts use accepts, so a
    # design excluded from `n_scorable` can never still be inside a median.
    for field in ("hotspot_recall", "centroid_distance",
                  "cross_hotspot_recall", "cross_centroid_distance"):
        out[f"{field}_median"] = cs.median(
            d.get(field)
            for d in cs.scorable_designs({"designs": designs}, field))

    # ---- the diagnostics for a run that would otherwise say nothing --------
    #
    # BOTH blind cases, not just the loud one. A non-zero exit is obvious; the
    # second is a command that exits 0 and produces no complex, where every
    # number in the report is None and the verdict tells the operator to go and
    # read a run tree that was globbed under `inference/` and is empty. Two live
    # runs hit the first, and the phase-1 NOTE below has always assumed the
    # second is survivable without a log. Neither is.
    #
    # A run that produced complexes pays nothing: no glob, no read, no extra
    # key, and the console is byte-for-byte what it was.
    blind = cs.should_collect_logs(rc, out["n_complexes"])
    if blind:
        out["log_diagnostics"] = _collect_run_logs(work_dir)

    # The listing is what phase 1 asks for explicitly (`dump_tree`) AND what a
    # blind shard needs whether or not it asked — phase 2 spawns with
    # dump_tree=False, so gating it on the flag alone is what leaves a failing
    # $12 shard with no file-level evidence at all.
    if dump_tree or blind:
        out["tree"] = _collect_tree(work_dir)

    # ALWAYS, not only under ``dump_tree``. These row counts used to be a phase-1
    # curiosity, and phase 2 spawns with ``dump_tree=False`` — so the one number
    # in the payload that upstream writes from its OWN sample list, rather than
    # from the file layout the harness globs, was collected precisely nowhere
    # that spends money. ``cs.design_count_disagreement`` reads it to check the
    # produced count against something the file layout cannot move; without it
    # the check is dead in the only phase that has a $12 verdict to protect.
    # Reading a handful of small CSVs costs milliseconds of container time.
    csvs = {}
    for p in sorted(glob.glob(str(inference / "**/*.csv"), recursive=True)):
        try:
            import csv as _csv
            with open(p, newline="") as fh:
                reader = _csv.DictReader(fh)
                rows = list(reader)
            csvs[p] = {"columns": reader.fieldnames, "nrows": len(rows)}
        except Exception as exc:
            csvs[p] = {"error": str(exc)}
    # Outside the loop: "no CSV was written at all" is itself a finding, and
    # setting the key only from inside the loop makes it indistinguishable
    # from "we did not look".
    out["csv_files"] = csvs

    _prune_registry(rp)
    # ``log_diagnostics`` is excluded from the JSON for the same reason
    # ``designs`` and ``tree`` are, and one more: json.dumps escapes every
    # newline, so a 6 KB traceback renders as a single unreadable line of
    # ``\n``-separated text. The local tails print it properly, through the same
    # ``_emit`` that sanitises upstream's characters.
    _emit(f"[canary/{label}] " + json.dumps(
        {k: v for k, v in out.items()
         if k not in ("designs", "tree", "log_diagnostics")},
        indent=2, default=str), flush=True)
    return out


# ===========================================================================
# Local entrypoint
# ===========================================================================


def _print_verdict(v) -> None:
    """The verdict, and — when it is not a PASS — the counts behind it.

    The reason alone cannot separate a near-miss from a catastrophe: "the
    negative control could not be measured" reads the same whether one design
    of eight survived or none did, and the operator's next move ($4 phase 1
    versus re-reading the chain map versus stopping) depends on exactly that.
    Every number was already in ``Verdict.metrics`` and nothing printed it.
    The rendering is in ``cs`` so the offline suite can assert on the lines
    themselves rather than on the presence of a print.
    """
    _emit(f"  {v.name:<9} {v.outcome:<13} {v.reason}")
    for line in cs.verdict_diagnostics(v):
        _emit(line)


def _finish(outcome: str, message: str) -> None:
    """Exit 0 / 1 / 3 for PASS / FAIL / INCONCLUSIVE.

    INCONCLUSIVE is non-zero because it is not a green light, and distinct from
    FAIL because it is not a condemnation either — the operator must be able to
    tell "we could not measure this" from "this is broken" from the exit status
    alone, not just from prose that scrolled past.
    """
    _emit(f"\n[canary] {outcome}: {message}")
    code = cs.EXIT_CODES[outcome]
    if code:
        raise SystemExit(code)


def _load_rp_local():
    """``run_pipeline`` on the OPERATOR's machine, for the pre-spawn refusal.

    Same by-path recipe as ``_load_scoring``: sibling first, image path second.
    It has to be the real ``run_pipeline`` and not a lookalike, because the
    whole value of checking locally is that the answer is identical to what the
    container would decide — ``missing_hotspots`` is a literal, case-sensitive
    membership test, and a re-implementation would drift from it exactly when
    it mattered.
    """
    candidates = [str(Path(__file__).resolve().parent / "run_pipeline.py"),
                  _RUN_PIPELINE_REMOTE]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return _load_by_path("rp_local", candidate)
    raise RuntimeError(f"run_pipeline.py not found; looked in {candidates}")


def _refuse_unresolvable_hotspots(target_pdb: str, contig: str,
                                  specs: list) -> str:
    """Refuse a malformed request BEFORE any shard is spawned. Returns the contig.

    WHY THIS IS NOT REDUNDANT WITH THE IN-CONTAINER CHECK. ``run_shard`` already
    refuses a spec whose tokens match no residue — but it does so INSIDE the
    container, after an A100 has started, and phase 2 starts three of them at
    once. The negative path made that worse: supplying ``--negative`` skipped
    ``pick_far_patch`` entirely, so the ONLY local code that touched the
    positive spec never ran, three shards spawned, and each returned
    ``{"error": ...}`` from a check that ``missing_hotspots`` — pure, local,
    already imported here — could have made for free.

    Upstream drops an unmatched hotspot SILENTLY and then designs
    unconstrained, so "these tokens resolve to nothing" is never a warning; it
    is a refusal, and it must be issued where it costs nothing.

    THE DECISIONS ARE IN ``cs``, THE INGREDIENTS ARE HERE. This function reads
    a file and calls ``run_pipeline``'s own matcher, neither of which the
    offline suite can do for a container; the two ``raise``s are in
    ``_canary_scoring`` where the suite executes them. Both refusals previously
    survived deletion — the tests could only see that a call existed — while
    ``--hotspots A99999`` went on spawning three A100s.
    """
    rp_local = _load_rp_local()
    residues, n_unparsable = rp_local.pdb_ca_residues(Path(target_pdb))
    cs.refuse_structureless_target(target_pdb, len(residues), n_unparsable)
    chains = sorted({r[0] for r in residues})
    resolved = contig or rp_local.format_contig(
        rp_local.derive_segments(residues, chains))
    selected = rp_local.select_residues(
        residues, rp_local.parse_target_input(resolved))
    cs.refuse_unresolvable_hotspots(
        target_pdb, resolved, len(selected),
        [(label, rp_local.missing_hotspots(selected, list(spec or [])))
         for label, spec in specs])
    return resolved


def _cancel_outstanding(handles: list, settled: set) -> None:
    """Kill every shard we launched that is not known to have finished.

    Without this, a Ctrl-C between ``spawn`` and ``get`` leaves three A100
    containers running to completion with nobody reading the result — the worst
    possible outcome, ~$12 spent to produce nothing.

    ``settled`` IS "NO LONGER BILLING", NOT "WE WROTE SOMETHING DOWN". It used
    to be the ``results`` dict, and the collect loop puts an ``{"error": ...}``
    entry in there for a ``get`` that RAISED — including a timeout.
    ``FunctionCall.get(timeout=)`` maps to ``poll_function`` and does not
    terminate anything, so that shard is still running on an A100 while its
    label reads as collected, and this function then skipped it forever. Only a
    ``get`` that RETURNED marks a label settled.

    A LABEL IS SETTLED ONLY BY A CANCEL THAT RETURNED. This used to mark the
    label BEFORE the attempt, which contradicts the paragraph above in its own
    docstring: a ``cancel`` that raised a transient gRPC error wrote the label
    down as settled, and the outer ``finally`` — which exists precisely to be
    the second chance — then skipped it forever. One Ctrl-C plus one flaky RPC
    and an A100 bills on to ``_MAX_SESSION_S`` (~$12.58) with the console
    already claiming it was handled. A successful cancel is recorded and never
    repeated; a failed one is retried by the next caller, which is what the
    interrupt handler and the ``finally`` are two of.

    The stated reason for marking early was that a retry must not raise a fresh
    exception out of a ``finally`` and mask the one already propagating. It
    bought nothing: that guarantee is held independently, by the ``except
    Exception`` swallow below and by the ``sys.exc_info()`` check on the only
    re-raise. Both still hold with the mark moved, and both are exercised.

    Marking on success also closes the narrower hole rather than opening one. A
    ``BaseException`` arriving between the mark and the ``try`` used to leave a
    label permanently unreachable; the remaining window — between a cancel that
    RETURNED and its ``settled.add`` — can only cost a redundant cancel of a
    call that is already terminated, which is a wasted round trip rather than a
    running A100.

    ``terminate_containers=True`` IS THE WHOLE POINT AND IS NOT THE DEFAULT.
    ``modal.FunctionCall.cancel(terminate_containers: bool = False)`` with the
    default marks the inputs TERMINATED but leaves the container that is
    running them alive — which for an A100 shard means the billing continues
    exactly as before and the cancel bought nothing. Verified against the
    pinned modal 1.4.2 signature; if that default ever changes meaning, this
    call still says what it wants explicitly.

    Best-effort per handle: one that refuses must not stop the others being
    killed, and a failure says so loudly enough to send the operator to the
    dashboard.

    EVERY CANCEL IS ATTEMPTED BEFORE ANYTHING IS PRINTED, and the two loops are
    separate for exactly that reason. Reporting inside the cancel loop puts a
    console write between one A100 and the next: the console is the thing that
    just failed (a container line carrying upstream's tick killed a run on
    2026-08-04), and a write that raises here abandons every shard after the
    first — the precise outcome this function exists to prevent. ``_emit``
    already cannot raise; this ordering means the guarantee does not DEPEND on
    that, so neither half has to be trusted alone.

    A ``BaseException`` from ``cancel`` — a second Ctrl-C landing mid-loop — is
    likewise not allowed to abandon the remaining containers. It is remembered,
    the loop finishes, and it is re-raised afterwards, because an interruption
    is still an interruption once the money is safe. It is re-raised ONLY when
    nothing else is already unwinding: this runs from a ``finally``, and
    raising there replaces the exception that sent us here — turning, say, a
    SystemExit(3) carrying the INCONCLUSIVE verdict into an unrelated
    KeyboardInterrupt. The money is safe either way by that point; the original
    diagnosis is not, so it wins.
    """
    outcomes: list[tuple[str, BaseException | None]] = []
    interrupted: BaseException | None = None
    for label, handle in handles:
        if label in settled:
            continue
        try:
            handle.cancel(terminate_containers=True)
        except Exception as exc:  # noqa: BLE001
            # NOT settled. ``settled`` means "no longer billing", and a cancel
            # that raised has established the opposite. Leaving it unmarked is
            # what makes the outer ``finally`` a real second attempt.
            outcomes.append((label, exc))
        except BaseException as exc:
            if interrupted is None:
                interrupted = exc
            outcomes.append((label, exc))
        else:
            # The ONLY place a label becomes settled: the cancel returned.
            settled.add(label)
            outcomes.append((label, None))

    for label, exc in outcomes:
        if exc is None:
            _emit(f"[canary] cancelled the {label} shard and terminated its "
                  "container")
        else:
            _emit(f"[canary] could not cancel the {label} shard "
                  f"({type(exc).__name__}: {exc}) — CHECK modal.com FOR A "
                  "RUNNING CONTAINER, it is still billing")
    if interrupted is not None and sys.exc_info()[0] is None:
        raise interrupted


@app.local_entrypoint()
def main(phase: int = 0, target_pdb: str = "", seed: int = 1234,
         hotspots: str = "A37 A39 A49 A98", contig: str = "",
         binder_min: int = 60, binder_max: int = 120,
         negative: str = "") -> None:
    # An unrecognised phase used to FALL THROUGH the `phase == 0` and
    # `phase == 1` branches into the ~$12 three-shard phase, so `--phase 3`
    # spent the most money available to answer a question nobody asked. Both
    # refusals below raise from the covered module, not from here.
    phase = cs.refuse_unknown_phase(phase)
    if phase in (1, 2) and not target_pdb:
        raise SystemExit("--target-pdb is required for phases 1 and 2")
    if phase in (1, 2):
        # `--hotspots ""` is the vacuous-pass path: phase 1 would compare no
        # hotspots against no hotspots and report PASS for $4, and phase 2
        # (with --negative, which skips pick_far_patch) would spawn three
        # shards for a guaranteed INCONCLUSIVE.
        positive = cs.refuse_empty_hotspot_spec(hotspots.split(), phase)
    else:
        positive = hotspots.split()
    pdb_text = Path(target_pdb).read_text() if target_pdb else _FALLBACK_PDB
    blen = [binder_min, binder_max]

    if phase == 0:
        res = phase0.remote(pdb_text)
        _emit("\n=========== PHASE 0 (free) ===========")
        _emit(json.dumps(res, indent=2, default=str))
        if not cs.phase0_pass(res):
            _finish(cs.FAIL, "PHASE 0 — a control did not pass, see above")
        _finish(cs.PASS, "PHASE 0 — a wrong hotspot is refused pre-GPU, "
                         "and --force survives a warm container.")
        return

    if phase == 1:
        # $4 is still money: prove the request is answerable before spending it.
        _refuse_unresolvable_hotspots(target_pdb, contig, [("positive", positive)])
        # SPAWNED, NOT AWAITED, SO THERE IS SOMETHING TO CANCEL. `.remote()`
        # blocks and never yields a `modal.FunctionCall`, so phase 1 held no
        # handle at all: every piece of cancellation machinery below — the
        # `finally`, `_cancel_outstanding`, `terminate_containers=True` — had
        # nothing to act on, and any local death left an A100 billing to
        # `_MAX_SESSION_S` (7200 s, ~$12.58) with nobody reading the result.
        # That is not hypothetical: a cp1252 `UnicodeEncodeError` killed a local
        # entrypoint mid-run on 2026-08-04, which is exactly this shape.
        #
        # ONE shard, so no collection loop and no `results` dict — but the same
        # `handles`/`settled` pair and the same `_cancel_outstanding`, because a
        # second cancellation path would be a second thing to get wrong and
        # would not inherit the fix that makes a FAILED cancel retryable.
        handles: list[tuple[str, object]] = []
        settled: set[str] = set()
        try:
            handle = run_shard.spawn(pdb_text, "phase1", positive, contig, seed,
                                     blen, True, positive)
            handles.append(("phase1", handle))
            res = handle.get(timeout=_COLLECT_TIMEOUT_S)
            # ONLY a `get` that RETURNED settles the label — same rule as phase
            # 2, and for the same reason: `FunctionCall.get(timeout=)` maps to
            # `poll_function` and terminates nothing, so a `get` that raised has
            # left the shard running.
            settled.add("phase1")
            _emit("\n=========== PHASE 1 (~$4) ===========")
            # `log_diagnostics` is held back from the JSON and printed below
            # instead: json.dumps escapes every newline, so a traceback comes out as
            # one unreadable line. It is the thing the operator most needs to read.
            _emit(json.dumps({k: v for k, v in res.items()
                              if k not in ("designs", "log_diagnostics")},
                             indent=2, default=str))
            _emit("\n--- per-design shape ---")
            for d in res.get("designs", []):
                _emit(f"  {d.get('name', '?'):40} chains={d.get('chains')} "
                      f"complex={d.get('is_complex')} "
                      f"target_verified={d.get('target_verified')} "
                      f"contacts={d.get('contacts')}")
                hints = (d.get("target_identity") or {}).get("chain_hints")
                if d.get("is_complex") and not d.get("target_verified"):
                    _emit(f"      UNSCORABLE: {d.get('unscorable_reason')}")
                    _emit(f"      chain map: {json.dumps(hints, default=str)}")
            # Upstream's own text, from inside the container, BEFORE the verdict —
            # the verdict on a failed shard is "the design command exited 1", which
            # is what the last two runs already said and could not act on. Every
            # line goes through `_emit`, which is what makes upstream's non-ASCII
            # (its ✓, its 📍, a traceback quoting either) survivable on a cp1252
            # console; a bare print here would reintroduce the exact defect
            # `harden_stream` exists for, on text this repo does not author.
            for line in cs.format_log_diagnostics(res):
                _emit(line)
            verdict = cs.phase1_verdict(res)
            _emit("\n--- verdict ---")
            _print_verdict(verdict)
            _emit(f"\nDesigns that are binder+target complexes: {res.get('n_complexes')}")
            _emit(f"Complexes whose target chain IS the input target: "
                  f"{res.get('n_target_verified')} "
                  f"(unverified: {res.get('n_target_unverified')})")
            _emit(f"Peak VRAM: {res.get('peak_vram_mb')} MB   Runtime: {res.get('runtime_s')} s")
            _emit("\nSET shared/pdb_preflight_rules.py::_PROTEINA SizeEnvelope."
                  "hard_cap_target_aa FROM THIS RUN before flag-on.")
            # HOW MANY OF THE ORDERED DESIGNS UPSTREAM ACTUALLY KEPT. Phase 1
            # does not fail on a thin yield — it asserts wiring — but it is the
            # fact that decides whether the ~$12 run is worth starting, and
            # without it the operator would learn it from three INCONCLUSIVE
            # verdicts afterwards. Rendered in ``cs`` so the offline suite can
            # assert on the line rather than on the presence of a print.
            for line in cs.designs_yield_note(res):
                _emit(line)
            if not res.get("n_complexes"):
                _emit("\n[canary] NOTE: no per-design file contains both target and "
                      "binder chains. Phase 2 cannot measure occupancy from these; "
                      "look for the AF2/RF3 refold artifact in the run tree. This is "
                      "an observation, not a failure — but running phase 2 before "
                      "resolving it will spend ~$12 to return INCONCLUSIVE.")
            elif not res.get("n_target_verified"):
                _emit("\n[canary] NOTE: the per-design files DO contain two chains, "
                      "but the chains matching the input PDB's ids do not carry the "
                      "input target's residues — the output uses a different chain "
                      "convention. Read the chain map above: it names the design "
                      "chain that does look like the input target. Phase 2 would "
                      "spend ~$12 to return INCONCLUSIVE until this is resolved. "
                      "Do NOT 'fix' it by scoring the chains as-is: that measures "
                      "the BINDER's own contacts and reports a perfect recall.")
            _finish(verdict.outcome, f"PHASE 1 — {verdict.reason}")
        finally:
            # EVERY local exit path the phase-1 branch has, `_finish`'s
            # SystemExit included — which is the NORMAL exit for FAIL and
            # INCONCLUSIVE, not an edge case — plus a raise anywhere in the
            # tail above (formatting the per-design block, the log render,
            # the verdict). A no-op once the `get` has returned and the
            # label is settled.
            _cancel_outstanding(handles, settled)
        return

    # ---- phase 2 ---------------------------------------------------------
    if negative.strip():
        negative_spec = negative.split()
        patch_info = {"source": "operator-supplied via --negative"}
    else:
        try:
            negative_spec, patch_info = cs.pick_far_patch(pdb_text, positive)
        except ValueError as exc:
            # Before any GPU is touched, so nothing has been spent. This is a
            # setup problem, not a verdict on the feature.
            raise SystemExit(
                f"[canary] cannot build a negative control (no GPU time was "
                f"used): {exc}") from None
    # BOTH specs, on BOTH paths, before the spawn loop. --negative used to skip
    # pick_far_patch entirely, which was the only local code touching the
    # positive spec — so a malformed positive spec bought three A100 startups
    # and three {"error": ...} returns.
    resolved_contig = _refuse_unresolvable_hotspots(
        target_pdb, contig,
        [("positive", positive), ("negative", negative_spec)])
    _emit(f"[canary] every hotspot token resolves against {target_pdb} "
          f"(contig {resolved_contig})")
    _emit(f"[canary] negative patch (>= {cs.NEGATIVE_MIN_SEPARATION_A} A from the "
          f"nearest positive hotspot): {' '.join(negative_spec)}")
    _emit("[canary] negative patch diagnostics: "
          + json.dumps(patch_info, indent=2, default=str), flush=True)

    # .spawn() returns immediately, so the three shards run CONCURRENTLY and the
    # wall clock is one shard, not three. Three blocking .remote() calls made
    # the worst case 3 * timeout = 6 h against a 2 h per-shard cap. Same idiom
    # the hub itself uses (gpu/modal_client.py: fn.spawn(payload) then
    # FunctionCall.get(timeout=...)). Collected in a loop rather than with
    # FunctionCall.gather so one failing shard is attributed to its label
    # instead of aborting the other two results.
    plan = [("positive", positive), ("negative", negative_spec), ("null", [])]
    results: dict[str, dict] = {}
    handles: list[tuple[str, object]] = []
    # Labels that are provably NOT billing any more: a `get` that RETURNED, or
    # a shard `_cancel_outstanding` has already dealt with. Deliberately not
    # `results`, which also holds an entry for a `get` that RAISED — and a
    # `get` that raised has terminated nothing (`FunctionCall.get(timeout=)`
    # maps to `poll_function`), so treating it as collected is how a shard
    # billed on to _MAX_SESSION_S with its label reading as done.
    settled: set[str] = set()
    try:
        try:
            for label, spec in plan:
                try:
                    handles.append((label, run_shard.spawn(
                        pdb_text, label, spec, contig, seed, blen, False,
                        positive)))
                except Exception as exc:  # noqa: BLE001
                    # A failed spawn must not abandon the shards already
                    # running: they are billing right now, and collecting their
                    # results is the only way that money buys anything.
                    results[label] = {
                        "label": label,
                        "error": f"spawn failed: {type(exc).__name__}: {exc}"}
            for label, handle in handles:
                try:
                    results[label] = handle.get(timeout=_COLLECT_TIMEOUT_S)
                except Exception as exc:  # noqa: BLE001 — one dead shard still reports
                    results[label] = {"label": label,
                                      "error": f"{type(exc).__name__}: {exc}"}
                else:
                    settled.add(label)
        except BaseException:
            # Ctrl-C / SIGTERM between spawn and collect. `except Exception`
            # does not cover either, and without a cancel the three A100
            # containers run to completion with nobody reading the answer — the
            # one way to spend the full ~$12 and receive literally nothing.
            # Re-raised: this is not an outcome, it is an interruption.
            #
            # THE NOTICE IS IN A ``try``, THE CANCEL IN ITS ``finally``, so the
            # cancel does not sit downstream of a console write. What brings us
            # here may BE a console write — a container line carrying upstream's
            # tick, on a cp1252 console — and "we could not print the word
            # interrupted, so the three A100s kept billing" is not a trade
            # anyone would make. ``_emit`` cannot raise; this makes the cancel
            # independent of that rather than reliant on it.
            #
            # The outer ``finally`` would cancel these too. This handler stays
            # because it is the only place that can say WHY, and because the
            # cancel is idempotent, so saying it twice costs nothing.
            try:
                _emit("\n[canary] interrupted — cancelling any shard still "
                      "running")
            finally:
                _cancel_outstanding(handles, settled)
            raise

        def _result(label: str) -> dict:
            return results.get(label, {"label": label,
                                       "error": "the shard was never launched"})

        pos, neg, null = _result("positive"), _result("negative"), _result("null")

        _emit("\n=========== PHASE 2 (~$12) ===========")
        for r in (pos, neg, null):
            _emit(f"\n[{r.get('label')}] rc={r.get('exit_code')} "
                  f"complexes={r.get('n_complexes')} "
                  f"target_verified={r.get('n_target_verified')} "
                  f"own_recall_median={r.get('hotspot_recall_median')} "
                  f"own_centroid_median={r.get('centroid_distance_median')} "
                  f"cross_recall_median={r.get('cross_hotspot_recall_median')}")
            if r.get("error"):
                _emit(f"    ERROR: {r['error']}")
            if r.get("n_complexes") and not r.get("n_target_verified"):
                _emit("    UNSCORABLE: the chains treated as target do not "
                      "carry the input target's residues — the design output "
                      "relabelled the chains. Scoring them anyway would measure "
                      "the BINDER against itself and report a perfect recall. "
                      "Re-run phase 1 for the chain map.")
            # PER SHARD, and the loop is what makes it per shard: a phase-2 run
            # where only the null control dies is the interesting case, and one
            # combined block at the end could not attribute the log to a label.
            # Shards spawn with dump_tree=False, so this and `tree` are the only
            # file-level evidence a failing $12 shard has. Through `_emit` for
            # the console reason: this is upstream's text, not ours.
            for line in cs.format_log_diagnostics(r):
                _emit(line)

        report = cs.phase2_report(pos, neg, null)
        _emit("\n--- verdicts ---")
        for verdict in report["verdicts"]:
            _print_verdict(verdict)

        if report["overall"] == cs.FAIL:
            _finish(cs.FAIL, "PHASE 2 — do NOT enable FLAG_TOOL_PROTEINA. A "
                             "failing null control specifically means the "
                             "hotspots were passed but had no effect.")
        elif report["overall"] == cs.INCONCLUSIVE:
            _finish(cs.INCONCLUSIVE,
                    "PHASE 2 — the run completed but the evidence needed to "
                    "decide was not measurable. This is NOT a failure and NOT a "
                    "pass: do not enable FLAG_TOOL_PROTEINA, and do not conclude "
                    "the feature is broken. Fix the measurement (see the verdict "
                    "reasons) and re-run.")
        else:
            _finish(cs.PASS, "PHASE 2 — hotspots demonstrably steer the "
                             "interface.")
    finally:
        # EVERY LOCAL EXIT PATH, not just the interrupt. Between the spawn and
        # here sit three A100s at ~$12.58 each against a 7200 s ceiling, and
        # anything that leaves this function without cancelling them buys that
        # time and throws it away: a raise inside the scoring, an AttributeError
        # while formatting a shard's output, a SystemExit from `_finish` (which
        # is the NORMAL exit for FAIL and INCONCLUSIVE) — none of them is a
        # KeyboardInterrupt, and none of them was covered.
        #
        # It is a no-op on the healthy path, where every `get` returned and
        # every label is settled, and it is idempotent, so the handler above
        # can also have run. Nothing raised in here can mask the exception that
        # brought us here: `_cancel_outstanding` swallows per-handle failures
        # and only re-raises when nothing else is unwinding.
        _cancel_outstanding(handles, settled)


# A 2-chain toy used only by phase 0 when no --target-pdb is given. Phase 0
# never runs the model, so it needs geometry, not biology.
_FALLBACK_PDB = "".join(
    f"ATOM  {i:5d}  CA  ALA {ch}{r:4d}    "
    f"{i * 1.5:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C\n"
    for i, (ch, r) in enumerate(
        [("A", n) for n in range(1, 41)] + [("B", n) for n in range(1, 21)], start=1
    )
)
