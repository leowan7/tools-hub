"""Modal entrypoint for ESMFold2 design (gradient inversion).

Reads job configuration from the ``JOB_PAYLOAD`` env var (same shape as
the MPNN / AF2 / ColabFold / ESMFold / Boltz-2 pipelines), runs the
upstream ``ESMFold2Design.design`` gradient-descent loop, and writes
the per-design summary to ``/tmp/smoke_results.json``. The Modal wrapper
returns this file inline via the function return value — see
``tools/esmfold2_design/modal_app.py``.

Environment variables (set by ``modal_app.py``):

    JOB_PAYLOAD     JSON: job_spec + input_presigned_url + upload_urls_endpoint + tier
    WEBHOOK_URL     URL to POST results to (unused at launch; see TODO)
    JOB_ID          tool_jobs row id (used for log prefixing)
    JOB_TOKEN       Job-specific auth token
    JOB_TIER        ``minibinder`` | ``scfv``

job_spec keys:

    preset                str   minibinder | scfv
    target_name           str?  one of TARGET_PRESETS or null
    target_sequence       str?  pasted target if target_name is null
    binder_name           str   minibinder | <framework>_framework_vhvl
    is_antibody           bool
    seed                  int
    batch_size            int   1-6

Output shape (``/tmp/smoke_results.json``)::

    {
      "status": "COMPLETED",
      "tier": "minibinder",
      "preset": "minibinder",
      "is_antibody": false,
      "target_name": "ctla4",
      "target_label": "CTLA4",
      "binder_name": "minibinder",
      "binder_label": "minibinder",
      "designs_total": 1,
      "designs_completed": 1,
      "n_failures": 0,
      "trajectory_steps": 150,
      "best_sequence": "AEKV...",
      "designs": [
        {
          "rank": 0,
          "name": "design_0",
          "pdb_key": "design_0_complex.pdb",
          "designed_sequence": "MAEK...|AEKV...",
          "sequence": "AEKV...",
          "iptm": 0.74,
          "distogram_iptm_proxy": 0.62,
          "cdr_distogram_iptm_proxy": null,
          "final_loss": 0.31,
          "isoelectric_point": 5.4,
          "filter_status": "strict_pass",
          "scores": {"iptm": 0.74, "...": "the six keys above, nested"}
        }
      ],
      "candidates": [
        {
          "rank": 0,
          "name": "design_0",
          "pdb_key": "design_0_complex.pdb",
          "designed_sequence": "MAEK...|AEKV...",
          "sequence": "AEKV...",
          "scores": {
            "ipTM": 0.74,
            "iPTM_proxy": 0.62,
            "final_loss": 0.31,
            "pI": 5.4,
            "filter_status": "strict_pass"
          }
        }
      ],
      "runtime_seconds": 612,
      "provider_job_id": "<job_id>"
    }

``designed_sequence`` is ``target|binder`` concatenated; ``sequence`` is the
binder alone, and it is what ``best_sequence``, export.fasta and the results
panel's order-form block all mean. ``candidates[]`` is the capitalized,
nested-``scores`` view the web tier reads; ``designs[]`` keeps the flat
lowercase keys. Both are emitted, and the results template consumes whichever
is present.

Raw capture: the summary above is a *view*, not the record. The complete
work tree (``/tmp/results``, including the untouched critic rows dumped
under ``results/raw/``) is tarred to ``/tmp/raw_archive.tgz`` on every
exit path, and ``modal_app.py`` parks that archive on the
``ranomics-esmfold2-design-raw`` Volume. See ``_archive_raw`` for why.

TODO (post first prod run, separate PR):
  - Send heartbeats with ``new_candidate`` events for the live UI.
  - Calibrate STRICT_IPTM / STRICT_CDR_IPTM_PROXY thresholds against
    the first 8-seed PD-L1 sweep instead of the conservative defaults.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import sys
import tarfile
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Optional

import requests

# /opt is the cookbook tutorial path planted by Dockerfile.modal.
sys.path.insert(0, "/opt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("esmfold2_design_pipeline")


SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
PDB_OUTPUT_DIR = Path("/tmp/results")
# Fixed path the Modal wrapper collects the raw tree from. MUST sit outside
# the tree it archives, or the tar ends up inside its own source — see
# _archive_raw, which re-checks rather than trusting this constant.
RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"
# The untouched critic rows land here — inside the work tree, so the archive
# carries them home. Written before _shape_designs collapses them.
RAW_CRITIC_RESULTS_PATH = PDB_OUTPUT_DIR / "raw" / "critic_results.json"

# Strict-pass thresholds. Open thread: tune against real PD-L1 sweep.
# Raised from 0.55 to 0.75 on 2026-06-03 after three test runs returned
# iPTM 0.83 / 0.83 / 0.95 — the prior gate was admitting noise. Real
# paper-ordered binders sit at iPTM >= ~0.7, so 0.75 is the conservative
# pass-through that rejects mid-band noise without hiding marginal hits.
STRICT_IPTM = 0.75
STRICT_CDR_IPTM_PROXY = 0.50
STRICT_PI = 6.0  # minibinder only: pI < 6 for downstream displayability

# Critic name string used by upstream binder_design.py. Every scored field —
# the real iPTM and BOTH distogram proxies — is read off this one critic's row,
# so every number in a results row comes from the same model.
#
# There was a second constant here, CRITIC_SCALING_PROXY =
# "ESMFold2-Experimental-Fast-base", matched by substring for the proxies.
# That string is NOT dead upstream -- it is upstream's own test for whether a
# critic is a scaling critic (``is_scaling_critic = "ESMFold2-Experimental-
# Fast-base" in critic_name``), and the 15 scaling checkpoints are named
# f"ESMFold2-Experimental-Fast-base{size}-step{step}k". Copying that substring
# as a *source selector* is what broke: it matches scaling rows and only
# scaling rows, and the scaling ensemble was off by default, so on the default
# path no matching row exists, both proxies stayed None, and since _classify
# gates scFvs on the CDR proxy alone EVERY antibody design came back ``drop``
# regardless of quality (13/13 on a 2026-08-23 prod run). Loading the ensemble
# did populate it -- which is why the tool looked fine to whoever tested that
# way.
#
# The proxies were never actually missing. Upstream calls
# compute_distogram_iptm_proxy once per critic and spreads the result into
# EVERY row, hero critics included, so the value was always sitting on the row
# this file already read the iPTM off. Sourcing it there removes the second
# name to keep in sync and makes every number in a results row one model's
# opinion. It also left the scaling ensemble reading nowhere, which is why the
# ``use_scaling_critics`` toggle that used to sit on this job spec was removed
# outright rather than left as a paid no-op: see the ``designer.load(False)``
# call in _run.
# _warn_if_scores_missing makes a future upstream rename loud in the logs
# instead of silently zeroing the gate again.
CRITIC_REAL_IPTM = "ESMFold2-Experimental-Cutoff2025"


def _write_result(payload: dict[str, Any]) -> None:
    """Write the canonical smoke-result JSON. Overwrites any prior file."""
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, default=str)
        logger.info("Wrote %s", SMOKE_RESULTS_PATH)
    except OSError as exc:
        logger.error("Failed to write %s: %s", SMOKE_RESULTS_PATH, exc)


def _parse_job_payload() -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    """Read JOB_PAYLOAD env, return (job_spec, job_id, tier, full_payload)."""
    raw = os.environ.get("JOB_PAYLOAD", "")
    if not raw:
        raise RuntimeError("JOB_PAYLOAD env var is empty.")
    payload = json.loads(raw)
    return (
        payload.get("job_spec", {}),
        os.environ.get("JOB_ID", ""),
        os.environ.get("JOB_TIER", "minibinder"),
        payload,
    )


# ===========================================================================
# Upload helpers (mirror tools/boltz2/run_pipeline.py exactly)
# ===========================================================================


def request_upload_urls(
    upload_endpoint: str, job_token: str, filenames: list[str]
) -> dict[str, str]:
    """Ask the hub for presigned PUT URLs keyed by filename."""
    resp = requests.post(
        upload_endpoint,
        json={"filenames": filenames},
        headers={"Authorization": f"Bearer {job_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"upload_urls request failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    return resp.json()["urls"]


def upload_pdb(url: str, pdb_bytes: bytes) -> None:
    """PUT the PDB bytes to a presigned URL with chemical/x-pdb."""
    resp = requests.put(
        url,
        data=pdb_bytes,
        headers={"Content-Type": "chemical/x-pdb"},
        timeout=120,
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"upload failed: HTTP {resp.status_code} {resp.text[:200]}"
        )


def _target_label(target_name: Optional[str], target_sequence: Optional[str]) -> str:
    if target_name:
        return target_name.upper()
    if target_sequence:
        return f"pasted target ({len(target_sequence)} aa)"
    return "target"


def _binder_label(binder_name: str) -> str:
    if binder_name == "minibinder":
        return "minibinder"
    # trastuzumab_framework_vhvl -> "Trastuzumab scFv"
    head = binder_name.replace("_framework_vhvl", "")
    return f"{head.title()} scFv"


def _extract_binder_sequence(designed_sequence: str) -> str:
    """The upstream concatenates ``target|binder`` with a ``|`` separator.

    Mirrors the cookbook's selector cell::

        df_result["binder_sequence"] = df_result.designed_sequence.str.split(r"\\|").str[1]
    """
    if "|" in designed_sequence:
        return designed_sequence.split("|", 1)[1]
    return designed_sequence


def _isoelectric_point(seq: str) -> Optional[float]:
    try:
        from Bio.SeqUtils.ProtParam import ProteinAnalysis
        return float(ProteinAnalysis(seq).isoelectric_point())
    except Exception as exc:
        logger.warning("Failed to compute pI for %s aa sequence: %s", len(seq), exc)
        return None


def _classify(
    is_antibody: bool,
    iptm: Optional[float],
    distogram_iptm_proxy: Optional[float],
    cdr_distogram_iptm_proxy: Optional[float],
    pi: Optional[float],
) -> str:
    """Return ``strict_pass`` | ``borderline`` | ``drop``."""
    if is_antibody:
        proxy = cdr_distogram_iptm_proxy
        if proxy is not None and proxy >= STRICT_CDR_IPTM_PROXY:
            return "strict_pass"
        if proxy is not None and proxy >= STRICT_CDR_IPTM_PROXY - 0.1:
            return "borderline"
        return "drop"
    # minibinder mode. pI is a hard gate: an undisplayable scaffold is a
    # drop regardless of iPTM, so check pI before the iPTM bands.
    if iptm is None:
        return "drop"
    pi_ok = pi is not None and pi < STRICT_PI
    if not pi_ok:
        return "drop"
    if iptm >= STRICT_IPTM:
        return "strict_pass"
    if iptm >= STRICT_IPTM - 0.05:
        return "borderline"
    return "drop"


def _save_complex_pdb(
    complex_obj: Any,
    name: str,
    upload_endpoint: str = "",
    job_token: str = "",
) -> Optional[str]:
    """Write a ProteinComplex out as a PDB file under PDB_OUTPUT_DIR and,
    if ``upload_endpoint`` is set, also stream the PDB bytes to the hub
    via a presigned PUT URL (mirrors tools/boltz2/run_pipeline.py).

    Returns the relative pdb_key for the smoke-results manifest. The key
    matches the Storage path the hub serves at
    ``/api/jobs/<job_id>/pdb/<pdb_key>``.
    """
    if complex_obj is None:
        return None
    PDB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    key = f"{name}_complex.pdb"
    path = PDB_OUTPUT_DIR / key
    try:
        pdb_text = complex_obj.to_pdb_string()
        path.write_text(pdb_text)
    except Exception as exc:
        logger.warning("Failed to write %s: %s", path, exc)
        return None

    if upload_endpoint:
        try:
            urls = request_upload_urls(upload_endpoint, job_token, [key])
            upload_pdb(urls[key], pdb_text.encode("utf-8"))
            logger.info("Uploaded %s to hub via presigned URL", key)
        except Exception as exc:
            logger.warning(
                "Upload of %s failed (%s) — inline /tmp copy preserved", key, exc,
            )

    return key


def _finite(value: Any) -> Optional[float]:
    """None for anything that is not a finite number.

    Upstream sets ``cdr_distogram_iptm_proxy`` to ``float("nan")`` for every
    non-antibody design (``compute_distogram_iptm_proxy``: "otherwise the CDR
    score is NaN"). That NaN never surfaced while the proxy branch was dead,
    so un-deadening it is what makes this reachable. ``json.dump`` writes NaN
    as a bare ``NaN`` literal, which is not valid JSON and which the results
    page's ``JSON.parse`` rejects outright; NaN also compares False against
    every threshold, so it would read as a confident drop rather than as the
    absent measurement it is.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _warn_if_scores_missing(
    designs: list[dict], is_antibody: bool, critic_results: list[dict],
) -> None:
    """Log loudly when a whole run came back with no critic-sourced scores.

    Every critic-sourced field (iPTM, both proxies, final_loss, the complex) is
    read off CRITIC_REAL_IPTM; only the pI is computed locally. Three upstream
    changes reach this: renaming that critic (blanks all of them), dropping a
    scored key from its row (blanks that one), or moving the proxies onto other
    critics (blanks the proxy — exactly how the scFv gate stayed dead).

    This checks the SHAPED OUTPUT rather than the critic names, because a name
    check sees only the first of those three. The bug this file exists to fix
    left CRITIC_REAL_IPTM present and correctly named the whole time, so a name
    check would have stayed silent through all 13 of the drops it caused.
    Checking the output sees the first and third, and the second whenever the
    dropped key is iptm or the proxy upstream populates for this preset; a
    dropped final_loss or complex still passes, neither being worth a false
    alarm.

    Neither message may assert a cause it cannot distinguish. An all-blank
    iPTM is equally a run where every fold diverged and a critic that stopped
    carrying the field, and zero shaped designs is equally failed folds and a
    renamed key. Naming one is a confident misdiagnosis, which sends the next
    reader somewhere there is nothing to find -- the same failure mode as the
    ``drop`` this file exists to fix, one layer up.

    This cannot repair the run, only put the cause in the Modal logs instead of
    making it cost another H100 to find. The logs are the only place it lands:
    modal_app.py returns stderr_tail="", so nothing surfaces on the job page.
    """
    names = sorted({str(row.get("critic_name", "")) for row in critic_results})
    if not designs:
        if critic_results:
            # Rows arrived but every one hit the ``seq is None`` skip above.
            # Two causes reach that skip and this cannot tell them apart: a
            # renamed key, or folds that failed and carry designed_sequence
            # None. Either way the job reports COMPLETED with zero designs,
            # which reads as "the target is undesignable".
            logger.error(
                "%d critic rows shaped 0 designs: every row had a null "
                "designed_sequence. Either those folds failed, or upstream "
                "renamed the key. Critics seen: %s",
                len(critic_results),
                names,
            )
        return
    # The proxy upstream POPULATES for this preset, which is not the same as
    # the one the preset gates on: _classify reads the CDR proxy alone for
    # antibodies, but minibinders gate on iptm and pI and never read their
    # proxy at all. The selection is about which one carries a number --
    # upstream emits the other as NaN (compute_distogram_iptm_proxy:
    # "otherwise the CDR score is NaN"), which _finite turns to None, so
    # checking both would false-alarm on every healthy run.
    proxy_key = (
        "cdr_distogram_iptm_proxy" if is_antibody else "distogram_iptm_proxy"
    )
    blank = [
        key for key in ("iptm", proxy_key)
        if all(design.get(key) is None for design in designs)
    ]
    if blank:
        # Name only the fields actually blank, and offer both readings. The
        # earlier wording said "every critic-sourced score is None" whatever
        # was missing, which is false on a diverged run whose proxies came
        # through, and it accused a critic the same line lists as present.
        logger.error(
            "no design carries a value for %s across all %d shaped design(s). "
            "Either every fold diverged (upstream emits NaN, dropped here), or "
            "critic %r -- which every one of these fields is read off -- "
            "stopped carrying them. Critics seen: %s",
            ", ".join(blank),
            len(designs),
            CRITIC_REAL_IPTM,
            names,
        )


def _shape_designs(
    critic_results: list[dict],
    is_antibody: bool,
    upload_endpoint: str = "",
    job_token: str = "",
    pdb_prefix: str = "",
) -> list[dict]:
    """Collapse the per-critic results into one row per design.

    The upstream critic_results is a list of dicts with keys including
    critic_name, iptm, distogram_iptm_proxy, cdr_distogram_iptm_proxy,
    final_loss, designed_sequence, complex. Several critics emit a row
    for the same design, each with its own proxy value — we group on
    designed_sequence and take every score from the CRITIC_REAL_IPTM
    row, so the iPTM and the proxy a user compares side by side are
    one model's opinion rather than two models'.

    ``pdb_prefix`` namespaces PDB filenames so multi-seed fan-out jobs
    (where the orchestrator spawns N children, each running this
    pipeline with a different seed) don't collide in the per-job
    Storage namespace. The orchestrator sets it to ``seed{N}_`` per
    child; single-seed runs leave it empty.
    """
    by_sequence: dict[str, dict] = {}
    for row in critic_results:
        seq = row.get("designed_sequence")
        if seq is None:
            continue
        bucket = by_sequence.setdefault(
            seq,
            {
                "designed_sequence": seq,
                "_scored": False,
                "iptm": None,
                "distogram_iptm_proxy": None,
                "cdr_distogram_iptm_proxy": None,
                "final_loss": None,
                "complex": None,
            },
        )
        critic_name = str(row.get("critic_name", ""))
        if critic_name == CRITIC_REAL_IPTM and not bucket["_scored"]:
            # The FIRST scoring row wins every field. Scores used to be
            # assigned unconditionally (last row won) while complex and
            # final_loss were first-wins, so two rows sharing a sequence --
            # which batch_size > 1 makes possible, and which the REUSE_ESMC
            # half of this change is what finally allows -- handed the user
            # one row's PDB underneath another row's numbers.
            bucket["_scored"] = True
            bucket["iptm"] = _finite(row.get("iptm"))
            bucket["distogram_iptm_proxy"] = _finite(
                row.get("distogram_iptm_proxy")
            )
            bucket["cdr_distogram_iptm_proxy"] = _finite(
                row.get("cdr_distogram_iptm_proxy")
            )
            bucket["complex"] = row.get("complex")
            bucket["final_loss"] = _finite(row.get("final_loss"))

    designs: list[dict] = []
    for rank, (seq, bucket) in enumerate(by_sequence.items()):
        name = f"{pdb_prefix}design_{rank}"
        binder_seq = _extract_binder_sequence(seq)
        pi = None if is_antibody else _isoelectric_point(binder_seq)
        pdb_key = _save_complex_pdb(
            bucket["complex"], name, upload_endpoint, job_token,
        )
        filter_status = _classify(
            is_antibody,
            bucket["iptm"],
            bucket["distogram_iptm_proxy"],
            bucket["cdr_distogram_iptm_proxy"],
            pi,
        )
        # Build a single ``scores`` dict so the generic /jobs/<id>/export.csv
        # exporter in app.py picks up every numeric/categorical column. The
        # flat copies are kept because the results template and the strict-
        # pass classifier in this file read them by short name.
        # ``sequence`` (binder only) is what /jobs/<id>/export.fasta reads;
        # ``designed_sequence`` (target|binder) is the UI's primary field.
        scores = {
            "iptm": bucket["iptm"],
            "distogram_iptm_proxy": bucket["distogram_iptm_proxy"],
            "cdr_distogram_iptm_proxy": bucket["cdr_distogram_iptm_proxy"],
            "final_loss": bucket["final_loss"],
            "isoelectric_point": pi,
            "filter_status": filter_status,
        }
        designs.append(
            {
                "rank": rank,
                "name": name,
                "pdb_key": pdb_key,
                "designed_sequence": seq,
                "sequence": binder_seq,
                "scores": scores,
                **scores,
            }
        )

    # Sort by iPTM desc with None at the bottom. The sentinel has to be
    # +inf, not -1: iPTM lives in [0, 1], so -1 is the negation of a PERFECT
    # score and sorted an unmeasured design above every measured one, which
    # is what this comment always claimed it did not do.
    designs.sort(
        key=lambda d: (float("inf") if d["iptm"] is None else -d["iptm"])
    )
    for rank, d in enumerate(designs):
        d["rank"] = rank
        d["name"] = f"{pdb_prefix}design_{rank}"
    # After shaping, not before: the check reads the values that actually
    # landed on the designs, which is the only view that catches a proxy
    # sourced off the wrong critic.
    _warn_if_scores_missing(designs, is_antibody, critic_results)
    return designs


# Filter tiers in preference order. Anything else ("drop", missing) is only
# reached when no tier above it has a single design.
_FILTER_TIERS = ("strict_pass", "borderline")


def _pick_best(designs: list[dict]) -> Optional[dict]:
    """Best design by filter tier first, iPTM second.

    ``designs`` is already sorted by iPTM desc, so the first design of the
    best non-empty tier wins. Ranking on iPTM alone hands the user a record
    the tool's own filter rejected: a pI ~12 poly-Arg hallucination can
    outscore every clean design, and best_sequence is what the results page
    offers up for peptide synthesis.

    Tier first, iPTM second -- in BOTH modes. In scFv mode ``_classify``
    decides the tier on the CDR distogram proxy while this ordering stays
    iPTM, so the winner is the highest-iPTM design among those the proxy let
    through, not the highest-proxy one. Deliberate: iPTM is the calibrated
    quantity and the proxy is a gate.
    """
    for tier in _FILTER_TIERS:
        for d in designs:
            if d.get("filter_status") == tier:
                return d
    return designs[0] if designs else None


# ===========================================================================
# Raw capture
# ===========================================================================


def _dump_raw_critic_results(critic_results: Any) -> None:
    """Dump the untouched critic rows into the work tree. Best-effort; never raises.

    This is where the field-dropping bug lives in this tool. ``_shape_designs``
    reads four keys off each critic row (``iptm``, one of the two distogram
    proxies, ``final_loss``, ``complex``), groups them on ``designed_sequence``,
    and lets every other key — and every row from a critic name it does not
    recognise — die with the container. Unlike the CSV-based pipelines there is
    no file on disk to fall back to: ``critic_results`` exists only in this
    process's memory, so if it is not written here it is recoverable only by
    paying for the H100 again.

    ``default=str`` mirrors _write_result: ``complex`` is a ProteinComplex and is
    not JSON-serialisable. Its coordinates are not lost — _save_complex_pdb
    writes them into this same tree as PDBs.

    A copy, taken before the parser runs. Nothing here feeds scoring.
    """
    try:
        RAW_CRITIC_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RAW_CRITIC_RESULTS_PATH, "w") as fh:
            json.dump(critic_results, fh, default=str)
        logger.info(
            "raw capture: wrote %d untouched critic rows to %s",
            len(critic_results or []),
            RAW_CRITIC_RESULTS_PATH,
        )
    except Exception as exc:  # noqa: BLE001 — capture is best-effort by design
        logger.warning(
            "raw capture: critic_results dump failed (non-fatal): %s: %s",
            type(exc).__name__,
            exc,
        )


def _archive_raw(srcs: list[str], dest: str | None = None) -> None:
    """Tar the COMPLETE work tree to ``dest``. Best-effort; never raises.

    A container must not decide which fields are worth keeping. That is exactly
    how ``design_iptm`` (the real binder->target interface) was lost on 460
    BoltzGen designs: the container kept three numbers out of ~190 columns, and
    the one it kept was the wrong one — ``iptm``, averaged over every chain pair
    and so dominated by the target's own crystal interface. It read ~2x high,
    two campaigns were concluded on it, and it was unrecoverable without
    re-paying for the GPU. Decide LOCALLY, where re-parsing is free.

    Not gated on designs, on status, or on ``upload_urls_endpoint``: a run that
    parsed to zero designs requests no upload URLs and ships nothing today, and
    that is precisely the run whose tree you need. Called from a ``finally`` so a
    crash keeps the evidence for the failure it is reporting.

    ``dest`` defaults to None and resolves to ``RAW_ARCHIVE_PATH`` on call, not
    at import: a ``dest: str = RAW_ARCHIVE_PATH`` default binds the constant's
    value once at def time, so any later reassignment of the module constant is
    silently ignored and the tar lands on the original path regardless.
    """
    # ``stage`` is what the finally removes, and mkdtemp does not run until well
    # into the try. Bound here so the cleanup cannot raise NameError on top of
    # the failure it is cleaning up after — the same floor the sibling pipelines
    # put under their dest variable, for a function called from a ``finally``
    # and contracted never to raise.
    stage = None
    try:
        # Inside the guard, and ``is None`` rather than ``or``: the docstring
        # promises only None is replaced, and ``or`` would silently swallow an
        # explicit dest="" too.
        if dest is None:
            dest = RAW_ARCHIVE_PATH
        present = [s for s in srcs if os.path.exists(s)]
        if not present:
            logger.warning(
                "raw capture: nothing to archive (tool wrote no output?): %s", srcs
            )
            return
        dest_abs = os.path.abspath(dest)
        # The tar must never be written inside a tree it archives, or it tars
        # itself. /tmp/raw_archive.tgz is outside /tmp/results by construction,
        # but check rather than trust the layout — a future move of
        # PDB_OUTPUT_DIR or RAW_ARCHIVE_PATH would otherwise silently make this
        # recursive.
        for src in present:
            src_abs = os.path.abspath(src)
            if (
                os.path.isdir(src_abs)
                and os.path.commonpath([src_abs, dest_abs]) == src_abs
            ):
                logger.error(
                    "raw capture: archive path %s is inside source tree %s — "
                    "skipping so the tar does not archive itself",
                    dest_abs,
                    src_abs,
                )
                return
        # Stage in a FRESH mkdtemp, pinned with ``dir=`` to dest's OWN directory.
        #
        # Moving into place at the end is what keeps a half-written tar out of
        # dest for the wrapper to park — but only while the move is a RENAME.
        # shutil.move falls back to copy2 across filesystems, and an interrupted
        # copy2 CAN leave a truncated file at dest: exactly the partial this
        # design exists to avoid, and unlike the sibling pipelines there is no
        # cleanup handler here to remove it. An unpinned mkdtemp hands that
        # guarantee to TMPDIR/TEMP/TMP, which an image change or a caller's
        # environment can point at another filesystem without touching this
        # file.
        #
        # Dest's own directory is the same filesystem as dest, which is what the
        # rename needs — on the Linux container this ships in, where POSIX
        # rename replaces an existing dest atomically. Not a universal: measured
        # on Windows/CPython 3.13, os.rename onto an EXISTING dest raises
        # FileExistsError (WinError 183) even within one directory, and
        # shutil.move then takes its copy2 fallback and truncates dest anyway.
        # The pin buys the guarantee where this runs; it does not buy it "by
        # construction".
        #
        # What the pin COSTS is self-containment. The loop above is purely
        # LEXICAL — commonpath over strings — so it cannot see a symlink,
        # junction or bind mount that makes dest's directory an alias of a
        # source tree. Before the pin, staging lived in the system temp dir and
        # such an alias was harmless. With it, the staging dir is created inside
        # the aliased source and the tar archives itself: measured, srcs=[<dir>]
        # with dest=<junction-to-that-dir>/raw_archive.tgz tarred ['results',
        # 'results/a.txt', 'results/rawtar_.../raw_archive.tgz'], where the same
        # case with an unpinned mkdtemp tarred ['results', 'results/a.txt'].
        # Taken knowingly: the sole production call site passes literal
        # constants from this module (PDB_OUTPUT_DIR /tmp/results and
        # SMOKE_RESULTS_PATH /tmp/smoke_results.json, dest /tmp/raw_archive.tgz),
        # so there is no alias to walk through, while the truncated dest the pin
        # prevents is the failure with no cleanup handler behind it.
        #
        # Fresh, so it cannot pick up a sibling file; streamed to a file, never
        # io.BytesIO — ~1x peak RSS instead of ~3-4x.
        #
        # If that directory will not take a tempdir (read-only, or not there
        # yet) fall back to the default location. The move may then degrade to
        # copy2, but a best-effort capture that tries and might truncate beats
        # one that gives up and captures nothing.
        try:
            stage = tempfile.mkdtemp(
                prefix="rawtar_", dir=os.path.dirname(dest_abs) or None
            )
        except OSError:
            stage = tempfile.mkdtemp(prefix="rawtar_")
        staged = os.path.join(stage, "raw_archive.tgz")
        with tarfile.open(staged, "w:gz") as tf:
            for src in present:
                src_abs = os.path.abspath(src)
                tf.add(
                    src_abs,
                    arcname=os.path.basename(src_abs.rstrip(os.sep)) or "work",
                )
        shutil.move(staged, dest_abs)
        logger.info(
            "raw capture: archived %s -> %s (%.1f MB)",
            present,
            dest_abs,
            os.path.getsize(dest_abs) / 1e6,
        )
    except Exception as exc:  # noqa: BLE001 — capture is best-effort by design
        logger.warning(
            "raw capture failed (non-fatal): %s: %s", type(exc).__name__, exc
        )
    finally:
        # Deliberately NOT wrapped in try/except OSError. An earlier revision
        # wrapped it "the way all five siblings wrap their os.remove", but the
        # analogy is false: their ``os.remove`` genuinely raises OSError, and
        # neither call here can. ``stage`` is None or the str mkdtemp returned.
        # ``os.path.isdir`` returns False rather than raising for any str —
        # posix genericpath catches (OSError, ValueError), nt._path_isdir
        # likewise returns False, checked against empty, NUL-bearing, over-long
        # and absent paths. ``shutil.rmtree(..., ignore_errors=True)`` routes
        # every OSError to a no-op onexc, checked against a tree held
        # undeletable by an open read-only handle. The one exception that does
        # escape rmtree is ValueError on a NUL in the path, which ``except
        # OSError`` would not have caught and which isdir screens out first.
        # So the wrap caught nothing reachable, no test could go red on its
        # removal, and it advertised a hazard that is not there.
        #
        # What IS load-bearing is ``stage = None`` above and the ``if stage``
        # here: mkdtemp does not run until well into the try, so without them a
        # failure before it turns this cleanup into an UnboundLocalError — a
        # NameError escaping a function contracted never to raise, out of a
        # ``finally`` and into the caller's. That one is reachable and pinned,
        # by test_failure_before_dest_is_bound_does_not_escape.
        if stage and os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)


# ===========================================================================
# Main
# ===========================================================================


def _run() -> int:
    start = time.time()
    try:
        job_spec, job_id, tier, payload = _parse_job_payload()
    except Exception as exc:
        logger.error("Failed to parse job payload: %s", exc)
        _write_result(
            {
                "status": "FAILED",
                "tier": "",
                "error": f"Failed to parse JOB_PAYLOAD: {exc}",
                "designs_total": 0,
                "designs_completed": 0,
                "n_failures": 1,
                "designs": [],
                "runtime_seconds": int(time.time() - start),
                "provider_job_id": os.environ.get("JOB_ID", ""),
            }
        )
        return 1

    upload_endpoint = payload.get("upload_urls_endpoint", "")
    job_token = payload.get("job_token", "") or os.environ.get("JOB_TOKEN", "")
    if not upload_endpoint:
        logger.warning(
            "upload_urls_endpoint missing from payload — per-design PDBs "
            "will only land in the inline smoke_result, not the hub Storage. "
            "Pilot tier requires the web flow to populate this."
        )

    preset = job_spec.get("preset", "minibinder")
    target_name = job_spec.get("target_name")
    target_sequence = job_spec.get("target_sequence")
    binder_name = job_spec.get("binder_name", "minibinder")
    is_antibody = bool(job_spec.get("is_antibody", preset == "scfv"))
    seed = int(job_spec.get("seed", 0))
    batch_size = int(job_spec.get("batch_size", 1))
    # pdb_prefix is set by the multi-seed orchestrator in modal_app.py
    # so each child run uses a unique Storage key for its PDB output;
    # single-seed runs receive an empty string and behave as before.
    pdb_prefix = str(job_spec.get("pdb_prefix", "") or "")

    logger.info(
        "ESMFold2 design start: job=%s tier=%s preset=%s target=%s "
        "binder=%s seed=%d batch_size=%d",
        job_id,
        tier,
        preset,
        target_name or "(pasted)",
        binder_name,
        seed,
        batch_size,
    )

    # Import the upstream module. /opt is on sys.path via the top of
    # this file; binder_design was copied in by the Dockerfile.
    try:
        import binder_design as bd  # type: ignore
    except Exception as exc:
        logger.error("Failed to import binder_design: %s\n%s", exc, traceback.format_exc())
        _write_result(
            {
                "status": "FAILED",
                "tier": tier,
                "error": f"binder_design import failed: {exc}",
                "designs_total": batch_size,
                "designs_completed": 0,
                "n_failures": 1,
                "designs": [],
                "runtime_seconds": int(time.time() - start),
                "provider_job_id": job_id,
            }
        )
        return 1

    # Direct (non-Modal-wrapped) instantiation. We are already inside the
    # Modal container, so we use ESMFold2Design rather than the
    # ESMFold2DesignModal wrapper class.
    try:
        # Share the one ESM-C 6B trunk the inversion models already hold
        # instead of loading a second fp32 copy for the LM head (~24 GB).
        # Upstream ships this False and measures 51 GB -> 27 GB VRAM with it
        # True, on exactly the scfv config this tool exposes, noting it is
        # what "enables increasing batch size up to 6". The 1-6 bound in
        # __init__.py is that same figure -- so shipping False under it took
        # the cap from one branch and the setting from the other, and every
        # batch_size > 1 scfv run OOMed an 80 GB H100 for zero designs
        # (default batch_size is 3, so that was the default web path).
        # CAVEAT: the shared trunk is not the fp32 one this replaces, so the
        # LM-loss term is not bit-identical to prior runs. Upstream flags the
        # same thing ("testing this setting in silico").
        # load() reads it as a module global, so it must be set before it.
        bd.REUSE_ESMC = True
        designer = bd.ESMFold2Design()
        # Hero critics only, always. The 15-checkpoint scaling ensemble this
        # argument used to switch on loads with device="cpu" and
        # cache_esmc=False -- 15 more checkpoints, 5 of them 6B, each carrying
        # its own ESMC copy, against the 10 GB ``memory=`` in modal_app.py.
        # Upstream recommends 60 GB host RAM for it. Since _shape_designs reads
        # every score off CRITIC_REAL_IPTM, a hero critic, those rows are never
        # read and the ensemble changed no reported number.
        #
        # Deliberately not calling that an OOM: a bare integer ``memory=`` is
        # a Modal soft limit ("How much memory to request, in MiB. This is a
        # soft limit." -- modal/image.py), so the ensemble may have been
        # throttled rather than killed. Untested either way, because the path
        # never ran in prod. What IS certain is the trade: six times the
        # documented host-RAM requirement, for no change to any number.
        designer.load(False)
    except Exception as exc:
        logger.error("Failed to load ESMFold2Design: %s\n%s", exc, traceback.format_exc())
        _write_result(
            {
                "status": "FAILED",
                "tier": tier,
                "error": f"ESMFold2Design load failed: {exc}",
                "designs_total": batch_size,
                "designs_completed": 0,
                "n_failures": 1,
                "designs": [],
                "runtime_seconds": int(time.time() - start),
                "provider_job_id": job_id,
            }
        )
        return 1

    try:
        best_seq, trajectory, critic_results = designer.design(
            target_name=target_name,
            target_sequence=target_sequence,
            binder_name=binder_name,
            binder_sequence=None,
            is_antibody=is_antibody,
            seed=seed,
            batch_size=batch_size,
        )
    except Exception as exc:
        logger.error("design() raised: %s\n%s", exc, traceback.format_exc())
        _write_result(
            {
                "status": "FAILED",
                "tier": tier,
                "preset": preset,
                "is_antibody": is_antibody,
                "target_name": target_name,
                "target_label": _target_label(target_name, target_sequence),
                "binder_name": binder_name,
                "binder_label": _binder_label(binder_name),
                "error": f"design() raised: {exc}",
                "designs_total": batch_size,
                "designs_completed": 0,
                "n_failures": 1,
                "designs": [],
                "runtime_seconds": int(time.time() - start),
                "provider_job_id": job_id,
            }
        )
        return 1

    # Copy the raw critic rows out before _shape_designs collapses them to four
    # keys per design; _archive_raw carries the file home with the tree.
    _dump_raw_critic_results(critic_results)

    designs = _shape_designs(
        critic_results or [],
        is_antibody,
        upload_endpoint,
        job_token,
        pdb_prefix=pdb_prefix,
    )
    best_design = _pick_best(designs)
    runtime = int(time.time() - start)

    # ``job.result["candidates"]`` with ``scores`` as a nested dict and
    # capitalized keys (ipTM, iPTM_proxy, pI) is what the web tier reads:
    # shared/exports.py (CSV + FASTA), shared/email.py::_top_candidate_summary
    # and blueprints/jobs.py::_top_score_for_share. Build that canonical view
    # alongside the ``designs`` shape the results template already consumes,
    # so we don't have to rewrite either. (This comment used to name a
    # ``summarize_top_score``, which has never existed in this repo.)
    candidates = [
        {
            "rank": d["rank"],
            "name": d["name"],
            "pdb_key": d["pdb_key"],
            "designed_sequence": d["designed_sequence"],
            "sequence": d.get("sequence", ""),
            "scores": {
                "ipTM": d["iptm"],
                "iPTM_proxy": (
                    d["cdr_distogram_iptm_proxy"] if is_antibody
                    else d["distogram_iptm_proxy"]
                ),
                "final_loss": d["final_loss"],
                "pI": None if is_antibody else d["isoelectric_point"],
                "filter_status": d["filter_status"],
            },
        }
        for d in designs
    ]

    summary = {
        "status": "COMPLETED",
        "tier": tier,
        "preset": preset,
        "is_antibody": is_antibody,
        "target_name": target_name,
        "target_label": _target_label(target_name, target_sequence),
        "binder_name": binder_name,
        "binder_label": _binder_label(binder_name),
        "designs_total": batch_size,
        "designs_completed": len(designs),
        "n_failures": max(0, batch_size - len(designs)),
        "trajectory_steps": len(trajectory) if trajectory is not None else None,
        # NOT ``best_seq`` from design(): upstream returns its own top pick
        # with no knowledge of our strict-pass gate. It survives only as the
        # fallback for a run that shaped nothing to choose from -- or whose
        # chosen design has an empty binder half, which would otherwise make
        # the results panel vanish rather than degrade.
        "best_sequence": (
            (best_design or {}).get("sequence")
            or (_extract_binder_sequence(best_seq) if best_seq else None)
        ),
        "designs": designs,
        "candidates": candidates,
        "runtime_seconds": runtime,
        "provider_job_id": job_id,
    }
    _write_result(summary)
    logger.info(
        "ESMFold2 design complete: %d/%d designs in %ds (%s strict_pass)",
        len(designs),
        batch_size,
        runtime,
        sum(1 for d in designs if d.get("filter_status") == "strict_pass"),
    )
    return 0


def main() -> int:
    """Run the pipeline, then archive the work tree on EVERY exit path.

    ``_run`` has five returns — payload parse failure, binder_design import
    failure, model load failure, design() raising, success — plus anything it
    raises unexpectedly. A ``finally`` here covers all six without re-indenting
    the body. There is no rmtree to race: this tool's work tree is a fixed
    /tmp path that simply dies with the container the moment this process
    exits, so "before teardown" means "before main returns".
    """
    try:
        return _run()
    finally:
        # smoke_results.json rides along even though it is normally returned
        # inline: on a hard timeout the wrapper's return dict never reaches the
        # hub at all, and then the copy inside this tar is the only one left.
        _archive_raw([str(PDB_OUTPUT_DIR), SMOKE_RESULTS_PATH])


if __name__ == "__main__":
    sys.exit(main())
