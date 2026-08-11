"""Modal entrypoint for one Proteina-Complexa search SHARD.

One invocation == one independent, seeded inference-time search on one
A100-80GB, returning up to ``_SHARD_DESIGNS`` (8) designs. The campaign engine
(``shared/compute_campaigns.py``) fans ``num_designs`` out across many of these
containers; the hub does the global cross-shard top-K + diversity. This script
never runs multi-GPU / multi-shard itself.

Contract (identical to boltz2 / iggm; set by ``tools/proteina/modal_app.py``):

    JOB_PAYLOAD   JSON: job_spec + input_presigned_url + upload_urls_endpoint
                  + job_token + tier
    WEBHOOK_URL   heartbeats derive /webhooks/heartbeat from it
    JOB_ID        tool_jobs row id (log prefix + heartbeat body + seed source)
    JOB_TOKEN     per-job auth token (new_candidate heartbeat gate)
    JOB_TIER      the preset (protein_binder | ligand_binder | motif_ame | validate)
    PROTEINA_RF3  on (default) | off — the RF3 reward kill-switch (Dockerfile ENV)

job_spec (from ``tools/proteina/__init__.py`` build_payload):
    preset, config_name, task_name, target_source, target_chain, target_input,
    hotspot_residues, hotspot_spec, binder_length, rf3_required, nsamples,
    replicas, nsteps, parameters

TARGET SOURCE. A shard designs against EITHER a curated ``task_name`` baked
into the repo configs OR a caller-uploaded PDB, never both, and which one is
declared explicitly as ``target_source`` rather than inferred from whether a
URL happens to be present. A custom target is staged, verified against the
real structure, and registered with ``complexa target add`` (which appends to
configs/targets/targets_dict.yaml, the dict binder_generate.yaml composes into
``target_dict_cfg``); ``task_name`` is then the registered key, so the design
invocation is byte-identical to a curated run. Bring-your-own is protein_binder
only — see ``_CUSTOM_TARGET_PRESETS``.

Output (``/tmp/smoke_results.json`` == the persisted ``job.result``): both a
flat ``designs`` list and a ``candidates`` list whose nested ``scores`` dict is
keyed to the results columns the viewer renders
(total_reward / af2_iptm / af2_plddt / rf3_score / binder_scrmsd / cluster_id).

CONFIRMED upstream facts (Proteina-Complexa @ dev 916eaaed, source-verified
against the pinned checkout 2026-07-16):
  * generate seed = cfg.seed + job_id (generate.py:74). gen_njobs=1 forces
    job_id=0 (split_by_job zeroes any job_id>=1), so cross-shard independence
    comes from a distinct ++seed derived from JOB_ID, never from job_id.
  * model checkpoints resolve via RELATIVE config keys (ckpt_path: ./ckpts,
    ckpt_name, autoencoder_ckpt_path: ./ckpts/<v>_ae.ckpt), so we run from
    cwd=/opt/proteina with the weights Volume mounted at ./ckpts. `complexa` is
    the console script (pyproject [project.scripts]); `design` + `validate` are
    real subcommands.
  * results-CSV early-exit (generate.py:584-589) keyed on (config_name, job_id)
    at CWD-relative ./inference/results_<config>_<job_id>.csv. cwd is now the
    shared repo root (not a per-shard temp dir), so main() wipes ./inference at
    shard start to stop a warm container re-emitting a prior shard's designs.
  * filter keeps all samples with
    ++generation.filter.delete_non_top_n_samples=false and a high
    ++generation.filter.filter_samples_limit.
  * reward channels are config-gated, NOT flag-gated: protein_binder scores on
    AF2 only (rf3folding commented out in binder_generate.yaml); ligand_binder
    scores on RF3 only (its sole active reward); motif_ame's reward block is
    commented out upstream (the least-verified variant).

BUILD-TIME-VERIFY (only a P2 seed / P4-P5 canary can pin these — the output
layer, not the launch recipe):
  * the reward/results CSV path + exact column names (mapped tolerantly below);
  * the per-design PDB glob under ./inference/, and whether those PDBs are
    binder-only or binder+target complexes (the hotspot canary's phase 1
    answers this — it decides how hotspot occupancy can be measured);
  * whether the AF2 binder reward tolerates the absent dssp / sc binaries (the
    public image ships without them; DSSP_EXEC/SC_EXEC are left unset).

HOTSPOTS ARE SILENTLY DROPPED UPSTREAM. load_target_from_pdb builds a
zero-initialised mask and sets ``mask[idx] = True`` where
``f"{atom.chain_id}{atom.res_id}"`` is in the requested list. A token that
matches nothing — a typo, a wrong chain letter, a residue absent from the file
— is ignored without warning, and the search then runs UNCONSTRAINED while
emitting output identical in shape to a correct run. That is why every hotspot
is re-checked here against the uploaded structure before the model loads
(``prepare_custom_target``), rather than trusted to fail loudly downstream.
"""

from __future__ import annotations

import base64
import csv
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("proteina_pipeline")

SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
# The COMPLETE, unparsed shard output tree, tarred here on every exit path (see
# archive_raw_outputs). modal_app.py moves it onto the raw Volume keyed by job
# id; nothing about it travels through the job result. Fixed path == the wrapper
# needs no coordination with this script beyond the constant.
RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"

# --- delivery mode for design coordinates ----------------------------------
# Two ways a design's atoms reach the caller, chosen by whether the payload
# carries an upload endpoint. NOT a preference: it is a statement about what
# the caller can actually receive.
#
#   UPLOAD  (upload_urls_endpoint present) — the tools-hub web path. Each PDB
#           is PUT to a presigned URL and the entry carries a `pdb_key`
#           pointer. Unchanged, and it stays the only behaviour a real job
#           sees, because a real job always supplies the endpoint.
#   INLINE  (endpoint absent) — a direct `modal.Function.from_name(...)` call.
#           There is no tools-hub server to call back to and no job_token to
#           authenticate with, so the atoms travel in the return value as
#           base64 under `pdb_content_b64` — the same field name and encoding
#           PXDesign and BindCraft already emit, so a cross-generator consumer
#           needs no per-tool special-casing.
#
# NOT the reason, despite what this comment used to claim: "the web tier cannot
# express a multi-chain target or chain-prefixed hotspots". It can, and that
# claim was wrong in a way that cost real safety. `tools/proteina/validate`
# accepts `A236-443,B236-443` (target_chain becomes "A B") and accepts
# `A264 B264` verbatim. The absent upload endpoint and job_token are the whole
# justification for INLINE; multi-chain has nothing to do with it.
#
# That false premise is why `normalize_hotspots`' bare-integer refusal below
# was placed on the container's direct-call entry ONLY. The web tier is a
# first-class multi-chain path, and IT IS NOW GUARDED IN ITS OWN TIER:
# `_parse_hotspots` in tools/proteina/__init__.py REFUSES a bare token when the
# run names more than one chain — named by the contig (`A236-443,B236-443`) or
# by target_chain alone (`A B`); both refuse — instead of promoting it onto the
# first chain. So the refusal below still cannot fire on a web payload, but for
# the opposite reason to the one this comment used to give: not because the
# ambiguous token arrives pre-promoted, but because it never becomes a job_spec
# at all. What the guard below still covers is the direct
# `modal.Function.from_name(...)` call, which has no adapter in front of it:
# `{"target_chain": "A B", "hotspot_residues": [264]}` raises here today.
#
# `hotspot_residues` DOES still carry the bare number either way — that half of
# the old claim stands. It is the chain-letter-stripped copy of `hotspot_spec`
# on every run, single- or multi-chain, and it is LOSSY: `[264]` cannot say
# whether the operator typed `264` or `B264`. Nothing that spends money reads
# it any more. `shared.pdb_preflight.shipped_hotspots` prefers `hotspot_spec`
# and all four money gates call it, which is the same precedence THIS file
# already applies (`_hotspot_tokens(job_spec["hotspot_spec"])` first, falling
# back to `hotspot_residues` only when the spec yields nothing). A promoted
# `A45` and a typed `A45` do normalize to the same list here (`["A45"]` either
# way), so this file cannot tell them apart and must not try — which is exactly
# why the ambiguity has to be refused in the adapter, above, while it is still
# visible.
#
# Do not "fix" it here by refusing when the hotspots address fewer chains than
# the contig names — designing against one epitope of a multi-chain complex,
# with the other chains present as steric context, is ordinary and correct, and
# both this file and the adapter accept it today. That guard would refuse
# legitimate campaigns to catch a case it cannot even see. Pinned by
# test_hotspots_on_a_SUBSET_of_the_contig_chains_are_legitimate in
# tests/test_proteina_delivery.py, so the reasoning is checkable rather than
# asserted.
#
# Nothing here is executable, which is exactly how the paragraph this replaced
# survived the change that falsified it. So the claims above about the web
# tier's refusal (from either chain source) and about the hotspot_residues
# shape are each pinned by a test that runs:
# test_a_bare_hotspot_is_refused_when_target_chain_
# names_two_chains, test_the_homodimer_case_is_refused_even_though_the_residue_
# is_real, test_the_token_the_gate_judges_is_the_token_the_payload_ships and
# test_hotspot_residues_stays_bare_ints_on_a_single_chain_run, all in
# tests/test_proteina_hotspot_chain_semantics.py. The direct-call refusal below:
# TestJobSpecAliases::test_bare_ints_on_a_MULTI_chain_target_are_refused in
# tests/test_proteina_delivery.py.
#
# The two are EXCLUSIVE, and that is a deliberate correction rather than an
# accident of the gate. Inlining alongside an upload would put a second copy of
# every structure in the Modal return value for no gain — the uploaded one
# already resolves by pdb_key — and reconcile_campaign_children pulls each
# child's full return into web-tier memory from inside a user-facing request.
# Exclusivity is what lets "the web path is unchanged" mean the whole payload
# and not merely the upload calls.
#
# Modal imposes no hard ceiling on a return value — _utils/blob_utils.py
# format_blob_data() blob-uploads anything over MAX_OBJECT_SIZE_BYTES (2 MiB)
# transparently, and the container's return path (container_io_manager.py
# package_output) goes through it. So inlining cannot fail on size. It can
# still be WASTEFUL: a 419-residue target plus binder is ~340 KB of PDB, ~450 KB
# once base64'd, and nsamples*replicas of those runs to multiple MB. The cap
# below bounds the total; designs past it keep their scores and lose only their
# coordinates, which are still recoverable from the raw archive.
INLINE_PDB_DEFAULT_CAP_BYTES = 64 * 1024 * 1024


def _inline_cap_bytes() -> int:
    """Parse the cap defensively.

    A bare ``int(os.environ[...])`` at module scope raises ValueError on a
    typo like ``64MB`` BEFORE ``_fail`` can write /tmp/smoke_results.json, so
    the container dies with no result file and the hub reports it as a webhook
    delivery failure — a misleading error for a mistyped env var, on a GPU
    container that is already allocated and billing.
    """
    raw = (os.environ.get("PROTEINA_INLINE_PDB_CAP_BYTES") or "").strip()
    if not raw:
        return INLINE_PDB_DEFAULT_CAP_BYTES
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "PROTEINA_INLINE_PDB_CAP_BYTES=%r is not an integer; using the "
            "%d byte default", raw, INLINE_PDB_DEFAULT_CAP_BYTES,
        )
        return INLINE_PDB_DEFAULT_CAP_BYTES


INLINE_PDB_TOTAL_CAP_BYTES = _inline_cap_bytes()
# A DEGENERATE-cap floor, and deliberately nothing more. It catches 0 and 1 —
# caps that cannot admit a single atom — before the GPU, because that check is
# free and those are the values a bug produces.
#
# It is NOT "the smallest cap worth starting a run for", which is what this
# constant used to claim and could not deliver. A single design's PDB is target
# dependent and UNKNOWABLE before the run (the Fc target this work exists to
# serve is ~340 KB, ~34x this floor; a small-target campaign is a fraction of
# it), so no pre-GPU threshold can separate "admits some designs" from "admits
# none". Raising the number would only move the false negative and would start
# refusing legitimate small-target runs. The guarantee lives POST-loop instead,
# where the real sizes are known: see the inline-delivery verdict in main(),
# which fails a shard that inlined nothing while the cap dropped designs.
INLINE_PDB_MIN_USEFUL_CAP_BYTES = 10 * 1024

# Hard off-switch for inline delivery. It is only observable when there is NO
# upload endpoint, because inlining never happens when there is one — with an
# endpoint the atoms are already in Storage and this flag changes nothing. Its
# one real effect is to turn a direct, endpoint-less call into a pre-GPU
# refusal, which is what you want if such a call was made by mistake.
_INLINE_OFF = {"off", "false", "0", "no"}

PROTEINA_HOME = os.environ.get("PROTEINA_HOME", "/opt/proteina")
CONFIG_DIR = os.environ.get("PROTEINA_CONFIG_DIR", f"{PROTEINA_HOME}/configs")
# The generator checkpoints live in the repo-root ckpts/ dir (the weights Volume
# mounts here); the configs reference them via the RELATIVE key `ckpt_path:
# ./ckpts`, so the validate tier globs this exact dir for a *.ckpt.
WEIGHTS_DIR = os.environ.get("WEIGHTS_DIR", f"{PROTEINA_HOME}/ckpts")
COMPLEXA_BIN = os.environ.get("COMPLEXA_BIN", "complexa")

# The three paid design-variant configs (validate exercises all of them so it
# is not a protein-only false green).
_ALL_CONFIGS = (
    "search_binder_local_pipeline",
    "search_ligand_binder_local_pipeline",
    "search_ame_local_pipeline",
)

# RF3 kill-switch. Off-values mirror the tools-hub CSRF_PROTECT=0 pattern.
_RF3_OFF = {"off", "false", "0", "no"}

# Variants that can design against a caller-supplied target. `complexa target
# add` writes configs/targets/targets_dict.yaml, which ONLY the binder pipeline
# composes into target_dict_cfg; ligand_binder and motif_ame index separate
# registries (ligand_targets_dict / design_tasks/ame_dict_v2 -> motif_target_
# dict_cfg). Kept in lockstep with _CUSTOM_TARGET_PRESETS in __init__.py.
_CUSTOM_TARGET_PRESETS = {"protein_binder"}

# Results columns the viewer renders (proteina_results.html). Column names are
# VERIFIED against the P-2 (protein: af2folding_*) and P-3 (ligand: rf3folding_*)
# canary reward CSVs @916eaaed. NOTE WHAT THAT VERIFICATION COVERED: that each
# named column EXISTS. It did not check what any of them MEANS, which is how
# af2_plddt shipped reading a loss term for its metric — see below. A column
# name matching is not evidence the value is the quantity the display key
# claims. Each display key lists the real upstream columns
# for BOTH variants; the tolerant _pick takes the first that exists (unmatched ->
# None -> hidden by the renderer).
#   protein_binder reward = af2folding_* (AF2 refold); total_reward == -i_pae.
#   ligand_binder  reward = rf3folding_* (RF3 fold);   ranking_score is the summary.
_SCORE_COLUMNS: dict[str, tuple[str, ...]] = {
    "total_reward": ("total_reward",),
    # interface pTM: raw for ligand (rf3folding_ipTM), log-only for protein.
    "af2_iptm": ("rf3folding_ipTM", "af2folding_i_ptm_log", "af2_iptm", "iptm"),
    # pLDDT (0-1). The protein variant MUST read the ``_log`` column. The bare
    # ``af2folding_plddt`` is the AfDesign LOSS term — ``1 - pLDDT`` — and the
    # two sum to 1.000000 in every row of a real reward CSV. Reading the loss
    # delivered an INVERTED confidence, and inverted monotonically, so any
    # sort, filter or threshold on pLDDT picked the WORST designs.
    #
    # Measured on job proteina-direct-fc-20260809-091702-68025f (prod v20 @
    # 1302d47): rank 1 scored ipTM 0.7625 and scRMSD 1.22 A while reporting
    # pLDDT 0.226 — impossible together. Its ``_log`` value is 0.774. The
    # ``_log`` suffix is NOT a logarithm: ``af2folding_rmsd`` and
    # ``af2folding_rmsd_log`` are identical in every row, which is what proves
    # ``_log`` is the reported-metric dict rather than a transform.
    #
    # ``af2folding_plddt`` is deliberately NOT retained as a fallback. It is
    # not a worse-but-usable source, it is the complement; falling back to it
    # would silently restore the inversion on any CSV lacking the _log column.
    # Better to deliver None, which the renderer hides.
    #
    # ``af2_iptm`` above is right only by accident — there is no non-log
    # ``af2folding_i_ptm`` column at all, so it was forced onto the metric.
    # pLDDT is the one field where both spellings exist.
    #
    # ``rf3folding_plddt`` (ligand) is UNVERIFIED: the run that proved this was
    # protein_binder and says nothing about the RF3 variant. Left exactly where
    # it was rather than speculatively reordered on an assumed symmetry.
    "af2_plddt": ("af2folding_plddt_log", "rf3folding_plddt", "af2_plddt", "plddt"),
    # RF3 summary (ligand only): the fold ranking score (0-1, higher better).
    "rf3_score": ("rf3folding_ranking_score", "rf3_score"),
    # self-consistency RMSD (protein AF2 refold; absent for the ligand variant).
    "binder_scrmsd": ("af2folding_rmsd", "binder_scrmsd", "scrmsd"),
    # cross-shard diversity is assigned at the hub, not in the per-shard CSV.
    "cluster_id": ("cluster_id",),
}
# Candidate columns for a PDB path/name in the reward CSV (tolerant).
_PDB_PATH_COLUMNS = ("pdb_path", "path", "sample_path", "structure_path", "filepath", "file")
_PDB_NAME_COLUMNS = ("sample", "name", "sample_name", "design_id", "sample_id", "id", "tag", "metadata_tag")


# ===========================================================================
# Result writer + fast-fail
# ===========================================================================


# Has this process already written a result file? ``main()``'s catch-all reads
# it so a crash AFTER the shard wrote its result cannot REPLACE that result
# with a traceback stub. The window is small but real — everything from
# ``send_heartbeat`` to the final log line runs after ``_write_result`` — and
# overwriting a COMPLETED result carrying every design would destroy the run
# rather than diagnose it. A flag rather than ``os.path.isfile`` on purpose:
# containers are reused warm and ``modal_app.run_tool`` does NOT delete
# /tmp/smoke_results.json between jobs, so a stale file from the previous shard
# would read as "this run already reported" and suppress the only diagnosis
# this run was going to produce. Reset at the top of ``main()``.
#
# THAT IS ONLY THE IN-PROCESS HALF, and the comment above used to read as
# though it were the whole thing. The flag stops a stale file being MISREAD by
# this process; it cannot stop the stale file itself being handed to the caller
# when this process dies without writing. ``_reset_result_file`` below closes
# that half, and is why only a REAL report may set this flag.
_RESULT_WRITTEN = False


def _dump_result(payload: dict[str, Any]) -> bool:
    """Serialise ``payload`` over SMOKE_RESULTS_PATH; return whether it landed.

    The file write on its own, with NO ``_RESULT_WRITTEN`` bookkeeping, so the
    startup placeholder can reuse the same OSError handling without claiming
    that this run has reported anything.
    """
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        return True
    except OSError as exc:
        logger.error("Could not write %s: %s", SMOKE_RESULTS_PATH, exc)
        return False


def _write_result(payload: dict[str, Any]) -> None:
    """Write THIS run's result and record that it reported."""
    global _RESULT_WRITTEN
    if _dump_result(payload):
        _RESULT_WRITTEN = True


def _reset_result_file() -> None:
    """Clear any result file a PREVIOUS shard left, then leave a placeholder.

    Proteina was the only one of the six generators that never did this, and
    /tmp/smoke_results.json is its ONLY reporting channel — this script posts
    no terminal webhook. Modal reuses containers warm and
    ``modal_app.run_tool`` reads the path unconditionally; it clears the raw
    archive and the staged-target dir for exactly this reason but not this
    path. So a shard that died without writing handed the caller the PREVIOUS
    shard's COMPLETED result — its provider_job_id, its candidates, its atoms —
    and the hub scored it ``succeeded``. Reproduced by driving this ``main()``
    with ``_run_shard`` replaced by ``os._exit(137)``.

    WHAT IS ACTUALLY REACHABLE, so this is not sold as more than it is. The
    container-level timeout does NOT leak: ``modal_app.py`` passes ``timeout=``
    to ``subprocess.run``, so that kill raises ``TimeoutExpired`` out of
    ``run_tool`` and the results file is never read. The leak needs
    ``subprocess.run`` to RETURN with the child dead — an OOM-kill or fatal
    signal, which skips every ``except`` and ``finally`` and so also skips
    ``main()``'s catch-all — or the OSError arm of ``_dump_result`` swallowing
    the write and a later ``sys.exit(1)`` leaving no file. Inline delivery
    makes the OOM arm likelier on this path specifically: up to
    ``INLINE_PDB_TOTAL_CAP_BYTES`` of raw PDB, its ~4/3 base64 expansion and
    the ``json.dump`` buffer are all live in this process at once.

    THE UNLINK IS THE LOAD-BEARING HALF; the placeholder is the courtesy.
    ``open(..., "w")`` truncates, but only once it has opened — remove first
    and a placeholder write that FAILS leaves no file at all, which the hub
    reports as a failure, instead of the previous shard's designs.

    IT DELIBERATELY DOES NOT GO THROUGH ``_write_result``. That would set
    ``_RESULT_WRITTEN`` before the shard had reported anything, and
    ``main()``'s catch-all — whose whole job is to guarantee a structured
    failure — would then decline to write the real diagnosis because it
    believed this run had already reported. That trades a rare stale-result
    leak for losing EVERY crash diagnosis, which is strictly worse than the bug
    being fixed. Pinned by
    ``test_the_startup_placeholder_does_not_suppress_the_catch_all``.
    """
    try:
        os.remove(SMOKE_RESULTS_PATH)
    except OSError:
        pass
    _dump_result(
        {
            "status": "FAILED",
            "error": {
                "bucket": "internal",
                "check": "did_not_complete",
                # Says what is KNOWN — this text survived, so no later write
                # replaced it — and lists the causes rather than asserting one.
                # Two different things produce it: the process died without
                # running any `except` or `finally` (OOM-kill, fatal signal), or
                # the run finished and BOTH its result write and the catch-all's
                # hit the OSError arm of _dump_result, which /tmp filling up
                # mid-run would do and which co-occurs with the first cause.
                # Naming only the kill would misreport the second as a crash.
                "detail": (
                    "run_pipeline.py left no result of its own, so this "
                    "placeholder from container startup is what survived. "
                    "Either the process was killed without running any "
                    "`except` or `finally` — the kernel OOM-killer or a fatal "
                    "signal, and there is no traceback in that case — or it "
                    "could not write to /tmp. Check the Modal function logs "
                    "for this job id; anything the run produced is in the raw "
                    "archive."
                ),
            },
            "tier": os.environ.get("JOB_TIER", ""),
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )


def _fail(bucket: str, check: str, detail: str) -> None:
    """Write a FAILED result and exit 1 (no GPU spent past this point)."""
    logger.error("pipeline FAILED at %s/%s: %s", bucket, check, detail)
    _write_result(
        {
            "status": "FAILED",
            "error": {"bucket": bucket, "check": check, "detail": detail},
            "tier": os.environ.get("JOB_TIER", ""),
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    sys.exit(1)


# ===========================================================================
# Heartbeat + upload helpers (identical contract to boltz2 / iggm)
# ===========================================================================


def _heartbeat_url(webhook_url: str) -> str:
    parsed = urlparse(webhook_url)
    return urlunparse(parsed._replace(path="/webhooks/heartbeat"))


def send_heartbeat(
    webhook_url: str,
    job_id: str,
    stage: str,
    designs_completed: int = 0,
    designs_total: int = 0,
    new_candidate: dict | None = None,
) -> None:
    """Fire-and-forget heartbeat. Never raises — a long shard must keep beating
    so the stale-job sweeper does not reap it as dead."""
    if not webhook_url:
        return
    body = {
        "job_id": job_id,
        "stage": stage,
        "designs_completed": int(designs_completed),
        "designs_total": int(designs_total),
    }
    if isinstance(new_candidate, dict):
        body["new_candidate"] = new_candidate
        body["job_token"] = os.environ.get("JOB_TOKEN", "")
    try:
        resp = requests.post(_heartbeat_url(webhook_url), json=body, timeout=10)
        logger.debug("Heartbeat sent: %s (HTTP %d)", stage, resp.status_code)
    except Exception as exc:
        logger.warning("Heartbeat failed (%s): %s", stage, exc)


def request_upload_urls(
    upload_endpoint: str, job_token: str, filenames: list[str]
) -> dict[str, str]:
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
    resp = requests.put(
        url, data=pdb_bytes, headers={"Content-Type": "chemical/x-pdb"}, timeout=120
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"upload failed: HTTP {resp.status_code} {resp.text[:200]}")


# ===========================================================================
# Payload parsing + input download
# ===========================================================================


def parse_payload() -> dict[str, Any]:
    raw = os.environ.get("JOB_PAYLOAD", "").strip()
    if not raw:
        _fail("preflight", "env", "JOB_PAYLOAD env var is empty")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail("preflight", "env", f"JOB_PAYLOAD is not valid JSON: {exc}")
    return {}  # unreachable


def download_target(url: str, dest: Path) -> Path:
    """Stream a custom target file (PDB / SDF) from the presigned GET URL."""
    try:
        with requests.get(url, stream=True, timeout=180) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=32768):
                    if chunk:
                        fh.write(chunk)
    except Exception as exc:
        _fail("input", "download", f"custom target download failed: {exc}")
    if not dest.is_file() or dest.stat().st_size < 32:
        _fail("input", "download", "downloaded target is empty or tiny")
    return dest


def sdf_to_pdb(sdf_path: Path, dest: Path) -> Path:
    """Convert a small-molecule SDF into a HETATM PDB via RDKit (present in this
    image, absent in the tools-hub web tier — hence the conversion happens
    here). Keeps the first valid molecule, ensures a 3D conformer (embeds one if
    the SDF was 2D-only), adds hydrogens, and writes a PDB. Scaffolding for the
    bring-your-own-ligand fast-follow — the custom-target path is hard-blocked
    until a canary wires the upstream registration (see main)."""
    try:
        from rdkit import Chem  # noqa: PLC0415
        from rdkit.Chem import AllChem  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - image guarantees RDKit
        _fail("input", "rdkit", f"RDKit import failed in container: {exc}")

    supplier = Chem.SDMolSupplier(str(sdf_path), removeHs=False, sanitize=True)
    mol = next((m for m in supplier if m is not None), None)
    if mol is None:
        _fail("input", "sdf", "no valid molecule parsed from the uploaded SDF")
    # A 2D-only SDF has no usable geometry; embed a 3D conformer before AddHs.
    if mol.GetNumConformers() == 0:
        mol = Chem.AddHs(mol)
        if AllChem.EmbedMolecule(mol, randomSeed=0xf00d) != 0:
            _fail("input", "sdf", "could not embed a 3D conformer for the ligand")
        AllChem.MMFFOptimizeMolecule(mol)
    else:
        mol = Chem.AddHs(mol, addCoords=True)
    try:
        # flavor=4 -> write ATOM/HETATM by residue info; the ligand carries no
        # peptide residue info so RDKit emits HETATM records.
        Chem.MolToPDBFile(mol, str(dest), flavor=4)
    except Exception as exc:
        _fail("input", "sdf", f"SDF -> PDB write failed: {exc}")
    if not dest.is_file() or dest.stat().st_size < 32:
        _fail("input", "sdf", "SDF -> PDB conversion produced no output")
    return dest


# ===========================================================================
# Custom-target structure parsing + verification (pre-GPU)
#
# This container is STANDALONE: modal_app.py copies exactly this one file, so
# nothing under shared/ is importable and there is no Biopython. Everything
# below is stdlib and pure, which is also what makes it unit-testable offline.
# The shape follows tools/iggm/run_pipeline.py's antigen_chain_info.
#
# Why any of this exists: upstream's load_target_from_pdb matches hotspots with
#
#     if f"{atom.chain_id}{atom.res_id}" in target_hotspots: mask[idx] = True
#
# against a zero-initialised mask. A token that matches nothing is SILENTLY
# dropped — no warning, no error — and the search then runs unconstrained while
# producing output identical in shape to a correct run. Re-deriving that exact
# match here, before the GPU is touched, is the only way a typo'd chain or a
# residue that isn't in the file becomes a refusal instead of a wrong answer
# the user pays for.
# ===========================================================================

# Modified residues biotite/atomworks treat as protein when building the CA
# structure upstream. An ATOM-only parser would report a legitimate hotspot on
# one of these as missing and refuse a valid run; false-refusal is the safe
# direction in general but it is avoidable here, so avoid it.
_MODRES_EQUIV = frozenset({
    "MSE", "CME", "CSO", "SEP", "TPO", "PTR", "KCX", "HYP", "LLP",
    "CSD", "OCS", "MLY", "M3L", "CAS", "CSS", "CSX", "PCA", "SAC",
})


def pdb_ca_residues(pdb_path: Path) -> tuple[list[tuple[str, int, str]], int]:
    """Parse (chain, resseq, icode) for every CA residue, first model only.

    Returns ``(residues, n_unparsable)``. Deliberately mirrors what upstream's
    CA structure contains:

    * ``ATOM`` CA records, plus ``HETATM`` CA records for the modified residues
      in ``_MODRES_EQUIV`` (biotite treats those as protein).
    * first model only — parsing stops at the first ``ENDMDL``, matching
      shared/pdb_inspect.py's single-model rule for NMR ensembles.
    * altloc duplicates collapsed on ``(chain, resseq, icode)``.

    ``n_unparsable`` counts CA lines whose residue-sequence columns would not
    convert to an int, so a residue this parser could not place is surfaced in
    the failure message rather than swallowed.

    THIS DOCSTRING USED TO CLAIM MORE THAN THE CODE DOES, and the correction is
    the point rather than a tidy-up. It said "columns 22:26 overflow at residue
    numbers >= 10000, and a silently-skipped residue there could make a
    legitimate hotspot look missing". Measured, that is false in both halves. A
    residue numbered 10000 occupies columns 23-27 in the file, so ``line[22:26]``
    reads "1000" — an int, no ValueError, nothing counted and nothing skipped.
    Residues 9995-10009 parse as ``[9995..9999, 1000 x 10]``, with
    ``n_unparsable == 0``. So the >= 10000 case is a SILENT MISPARSE onto a
    wrong residue number, which ``n_unparsable`` cannot report and which no
    caller of this function can currently detect. The fifth digit lands in the
    INSERTION-CODE column, so those ten residues are additionally told apart
    only by an "insertion code" of "0".."9" — which is why they do not collapse
    onto one key, and why they would all answer to the hotspot token ``A1000``.

    Left as-is deliberately: the misparse predates this parser's current callers
    and fixing it means deciding what a 5-column resSeq means (PDB has no legal
    answer; the hybrid-36 and mmCIF conventions disagree), which is a change to
    what counts as a residue rather than a docstring correction. The hazard is
    real but bounded — the crop keys on the same misparsed number the count
    keys on, so the two stay consistent with each other, and a hotspot on such a
    residue would be refused pre-GPU rather than silently dropped. Anything
    beyond that is unverified.
    """
    residues: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str]] = set()
    n_unparsable = 0
    with open(pdb_path, "r", errors="replace") as fh:
        for line in fh:
            record = line[:6]
            if record.startswith("ENDMDL"):
                break
            if record not in ("ATOM  ", "HETATM"):
                continue
            if line[12:16].strip() != "CA":
                continue
            resname = line[17:20].strip().upper()
            if record == "HETATM" and resname not in _MODRES_EQUIV:
                continue
            chain = line[21:22].strip()
            try:
                resseq = int(line[22:26])
            except ValueError:
                n_unparsable += 1
                continue
            icode = line[26:27].strip()
            key = (chain, resseq, icode)
            if key in seen:
                continue
            seen.add(key)
            residues.append(key)
    return residues, n_unparsable


def normalize_target_chain(raw: str) -> str:
    """Accept both chain separators, emit the whitespace form this file parses.

    ``target_chain`` is consumed by ``derive_segments`` via a bare ``.split()``,
    so only whitespace ever separated chains here. The campaign side and the
    other three generators standardised on the comma form (see
    llm-proteinDesigner/docs/MULTI-CHAIN-TARGETS.md: ``"A,B"``, ``"A B"`` and
    ``"A, B"`` are equivalent). Parsing only one of them is how a multi-chain
    request gets accepted at the form and rejected — or worse, silently
    narrowed — at every gate behind it.

    ``"A,B"`` alone was already a LOUD failure: it splits to the single token
    ``"A,B"``, no chain matches, ``derive_segments`` returns [] and the caller
    is told the chain is absent. The quiet case is a mixed string like
    ``"A B,C"``, which yields ``["A", "B,C"]`` — chain A resolves, ``B,C`` is
    dropped by ``derive_segments``' ``continue``, and the run designs against
    one protomer of a dimer while looking entirely successful.

    Order is significant (it drives contig segment order) and duplicates are
    removed, both per that contract.
    """
    tokens = [t for t in re.split(r"[,\s]+", raw.strip()) if t]
    seen: set[str] = set()
    ordered: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    return " ".join(ordered)


def _hotspot_element(value: object, field: str) -> str:
    """Render ONE element of a hotspot list as the token this file parses.

    A WHOLE-NUMBER FLOAT IS AN INTEGER RESIDUE NUMBER, not a token of its own.
    ``str(296.0)`` is ``"296.0"``, which the bare-integer regex below does not
    match, so such a hotspot was neither refused as ambiguous on a multi-chain
    target nor attributed to the chain on a single-chain one: it travelled on
    as the literal ``"296.0"``, and upstream matches ``f"{chain_id}{res_id}"``,
    so it addresses nothing that can exist. That is contained on the
    custom-target path — ``missing_hotspots`` refuses it pre-GPU — but the
    refusal then blames "not in the selected region" for what is really a
    number format, and on the multi-chain case it means the ambiguity guard,
    the one refusal standing between a caller and a silently half-aimed dimer,
    is simply skipped. ``shared/pdb_inspect.split_hotspot`` already truncates a
    float for exactly this reason: main's commit 0cbfea6 restored it because
    "a JSON body sending a whole number as a float is the shape that reaches
    it". This is the same shape crossing the container boundary.

    A FRACTIONAL float is REFUSED rather than truncated, and that is a
    deliberate divergence from ``split_hotspot``, which truncates. No residue
    is numbered 296.7, so resolving it means guessing which residue was meant,
    and guessing on the hotspot field is the whole failure class this function
    exists to stop. The refusal costs nothing and stops nothing real: the web
    adapter's ``_parse_hotspots`` yields ints from a regex and can never emit
    a float, and today such a token is refused anyway — by ``missing_hotspots``
    on the custom path, with a worse message, and dropped unread on the curated
    path. ``bool`` is refused for the same reason ``split_hotspot`` refuses it:
    it subclasses int, so ``True`` would otherwise read as residue 1.
    """
    if isinstance(value, bool):
        raise TypeError(
            f"job_spec.{field} contains {value!r}, which is a boolean, not a "
            "residue number. Send the residue number itself (e.g. 264 or "
            '"A264").'
        )
    if isinstance(value, float):
        if not value.is_integer():
            raise TypeError(
                f"job_spec.{field} contains {value!r}, which is not a whole "
                "residue number. Residues are numbered with integers, and "
                "there is no residue to round it to without guessing. Send "
                f"{int(value)} or {int(value) + 1} explicitly."
            )
        return str(int(value))
    return str(value).strip()


def _hotspot_tokens(raw: object, field: str) -> list[str]:
    """Tokenise ONE hotspot field. Empty -> []; a wrong TYPE -> TypeError.

    Split out of ``normalize_hotspots`` so "is this field empty?" is answered by
    the tokens it actually yields rather than by comparing the raw value against
    a hand-listed set of empty shapes. The old predicate was
    ``raw is None or raw == []``, which recognises exactly two of them; ``""``,
    ``" "``, ``[""]``, ``("",)``, ``()`` and ``","`` all read as "hotspots were
    supplied and there are none", so the ``hotspot_residues`` alias beside them
    was never consulted and every hotspot it carried was discarded into a fully
    unconstrained search that completes successfully.

    A value of the wrong TYPE is REFUSED here rather than coerced, and both
    ways it used to go were bad. A scalar (``264``, ``True``, ``3.5``) reached
    ``for h in raw`` and raised ``TypeError: 'int' object is not iterable``,
    which ``main()`` did not catch, so the container died with no
    /tmp/smoke_results.json and the caller saw an opaque delivery failure for a
    mistyped field. A dict did not crash at all — ``for h in {"a": 1}``
    iterates KEYS, so it yielded the hotspot ``"a"`` out of nothing. Neither is
    salvaged: ``main()`` turns this into a clean pre-GPU ``_fail`` naming the
    field, and shape-guessing on the hotspot field is the same class of
    helpfulness that produced the silent mis-aim the refusal below exists to
    stop.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t for t in re.split(r"[,\s]+", raw.strip()) if t]
    if isinstance(raw, (list, tuple)):
        # Per element via _hotspot_element, not a bare str(): a whole-number
        # float is a residue number and must be seen as one by the bare-integer
        # checks below, not carried through as the token "296.0".
        return [t for t in (_hotspot_element(h, field) for h in raw) if t]
    raise TypeError(
        f"job_spec.{field} is a {type(raw).__name__} ({raw!r}), which is "
        "neither a list of hotspot tokens nor a comma/space-separated string. "
        "Send a list like [\"A264\", \"B264\"] or a string like \"A264 B264\" "
        "— never a bare value, not even for a single hotspot."
    )


def normalize_hotspots(job_spec: dict) -> list[str]:
    """Resolve hotspot tokens from either this tool's name or the shared one.

    Proteina's native key is ``hotspot_spec``; the campaign side and the other
    three generators send ``hotspot_residues``. Native wins when both are
    present so nothing already in flight changes meaning — where "present"
    means "yields at least one token", so an empty native key in ANY shape
    (``[]``, ``""``, ``[""]``, ``{}``) falls through to the alias instead of
    silently discarding everything the alias carries. A plain string is
    accepted for either key and split on commas/whitespace, because that is
    what a caller who typed the chain list as a string tends to send for the
    hotspots too.

    Bare integers are attributed to the single target chain, per the shared
    contract. Upstream matches hotspots as ``f"{chain_id}{res_id}"``, so a bare
    ``264`` addresses nothing at all; attributing it is what makes the
    single-chain shorthand mean what its sender intended.

    A BARE INTEGER IS REFUSED WHEN THE TARGET HAS MORE THAN ONE CHAIN, and that
    refusal is the whole reason this function is allowed to rewrite a token at
    all. "Attribute to the first chain" is only unambiguous for a single-chain
    target. On a homodimer it is actively dangerous: ``264`` becomes ``A264``,
    ``missing_hotspots`` is a set-membership test and a real dimer genuinely
    contains ``A264``, so the guard passes, the log reports every hotspot
    matched, and the run designs against protomer A with B completely
    unconstrained — indistinguishable from a correct run, which is precisely
    the failure this file exists to prevent. On a symmetric Fc set the two
    protomers' numbers are identical, so 16 tokens silently collapse to 8.
    ValueError here reaches ``main()`` as a pre-GPU ``_fail``.

    When no chain is known at all the token is passed through untouched rather
    than guessed at — the pre-GPU ``missing_hotspots`` guard then refuses it.
    """
    # Emptiness is decided by the TOKENS a field yields, not by the shape of
    # the raw value — see _hotspot_tokens. "Native wins when both are present"
    # is unchanged: a native key that yields any token still wins outright.
    items = _hotspot_tokens(job_spec.get("hotspot_spec"), "hotspot_spec")
    if not items:
        items = _hotspot_tokens(
            job_spec.get("hotspot_residues"), "hotspot_residues")
    if not items:
        return []

    # The chains the DESIGN will actually contain — which is the only question
    # that decides whether a bare number is ambiguous — read from whichever
    # field prepare_custom_target itself reads.
    #
    # A CONTIG REPLACES target_chain, it does not add to it. prepare_custom_target
    # derives its segments from target_input when that is present and never
    # looks at target_chain again (``requested_chains`` is used in the else
    # branch and nowhere else), so with a contig the contig's chains ARE the
    # design. Counting target_chain as well invents a protomer that does not
    # exist: {"target_chain": "A", "target_input": "C1-200"} — proteina's
    # shipped default "A" over a structure whose chain is C, the normal shape
    # because the form tells the user to leave that field alone and name their
    # chains in the contig — has exactly one chain, so a bare 264 is
    # unambiguous, yet the union counted two and refused the run while
    # suggesting A264, a residue on a chain the upload does not contain.
    # shared/pdb_inspect made the same replace-not-union correction on main
    # (commit 0cbfea6) for this same input shape.
    #
    # The union's own motivating case is untouched, because it never needed the
    # union: {"target_chain": "A", "target_input": "A1-200,B1-200"} has two
    # chains read from the contig ALONE, so bare hotspots are still refused
    # rather than silently promoted to A with the second protomer
    # unconstrained.
    chains = normalize_target_chain(str(job_spec.get("target_chain") or "")).split()
    target_input = str(job_spec.get("target_input") or "").strip()
    if target_input:
        try:
            contig_chains: list[str] = []
            for chain, _lo, _hi in parse_target_input(target_input):
                if chain not in contig_chains:
                    contig_chains.append(chain)
            # Only when the contig actually named something. A contig that
            # parses to nothing (``","``) is refused downstream on its own
            # terms; until then target_chain stays the best available answer,
            # so this cannot silently drop the count to zero and let a bare
            # token through unchecked.
            if contig_chains:
                chains = contig_chains
        except ValueError:
            # Malformed contig: leave it to parse_target_input's own pre-GPU
            # refusal in prepare_custom_target, which reports it properly.
            pass
    bare = [t for t in items if re.fullmatch(r"-?\d+", t)]
    if bare and len(chains) > 1:
        raise ValueError(
            f"hotspots {bare} carry no chain prefix, but this run targets "
            f"{len(chains)} chains ({' '.join(chains)}). A bare residue number "
            "cannot say which protomer it means, and guessing the first one "
            "would leave every other chain unconstrained while still reporting "
            "a full hotspot match. Prefix each hotspot with its chain, e.g. "
            f"{chains[0]}{bare[0]} or {chains[1]}{bare[0]}."
        )
    out: list[str] = []
    for tok in items:
        if chains and re.fullmatch(r"-?\d+", tok):
            out.append(f"{chains[0]}{tok}")
        else:
            out.append(tok)
    return out


# ---------------------------------------------------------------------------
# Putting the DELIVERED design back into the operator's residue numbering
# ---------------------------------------------------------------------------
#
# THE DEFECT, MEASURED. Upstream renumbers every chain of a design to 1..N. On
# 8 of 8 designs of a completed Fc shard, input chains A 234-444 (211 residues)
# and B 237-444 (208) came back as A 1-211 and B 1-208 — contiguous, chain
# labels preserved, residue order preserved, 100.0% positional sequence identity
# on both chains across 3,352 correspondences. Only the keys changed.
#
# THIS IS NOT A CORRECTNESS BUG IN PRODUCTION, and saying so precisely matters:
# every hotspot check runs PRE-GPU against the uploaded file, so nothing is
# designed against the wrong residue. What breaks is the DELIVERABLE. An
# operator who asked for hotspot A241 gets back a structure that has no residue
# 241 in it and cannot cross-reference the result against the numbering they
# typed.
#
# The restore is fail-closed: it proves the correspondence by sequence before
# using it, and when it cannot, the design is uploaded byte-for-byte as upstream
# wrote it — today's behaviour. A design is never lost to this step.

# The three answers to "which numbering does the delivered file carry?":
# ``input`` (the operator's uploaded numbering was restored, so their own
# hotspot labels resolve against the download), ``upstream`` (they gave us a
# numbering and the file carries 1..N instead), ``n/a`` (there was no operator
# numbering at all — a curated benchmark run uploads no file). Where each is
# chosen, and why the third is not a synonym for the second, is at
# ``not_restored`` in main().
#
# NOTHING READS THIS TUPLE AT RUNTIME, which is worth saying rather than
# leaving it to look like a gate: the pipeline emits the strings as literals
# and ``webhooks/modal.py::_sanitize_candidate`` allowlists the same three a
# third time. What it is for is giving those two lists one place to be
# compared — tests/test_proteina_smoke.py reads this tuple and the webhook's
# own allowlist and fails when they drift, which is the drift that would make
# one design report one numbering while it streams and another once it
# finalised.
_TARGET_NUMBERING_VALUES = ("input", "upstream", "n/a")

_UNKNOWN_RESNAMES = frozenset({"UNK", "UNX", "XAA", "X"})

# The parent amino acid of each modified residue in ``_MODRES_EQUIV``. Used ONLY
# when comparing a design output's residue names against the input's: refold and
# relax steps routinely write MSE back as MET, and counting that as a MISMATCH
# pushes a perfectly good selenomethionine target below the identity floor, so
# the restore declines and the operator silently receives 1..N. Never used for
# selection. Kept in lockstep with the canary's MODRES_PARENT by hand — see the
# note on _RENUMBER_MIN_IDENTITY for why this file cannot import it.
_MODRES_PARENT = {
    "MSE": "MET", "CME": "CYS", "CSO": "CYS", "SEP": "SER", "TPO": "THR",
    "PTR": "TYR", "KCX": "LYS", "HYP": "PRO", "LLP": "LYS", "CSD": "CYS",
    "OCS": "CYS", "MLY": "LYS", "M3L": "LYS", "CAS": "CYS", "CSS": "CYS",
    "CSX": "CYS", "PCA": "GLU", "SAC": "SER",
}

# How far a chain may drift from the input before we refuse to call it the same
# chain, and how much of that chain must carry actual evidence. Same values as
# the canary's TARGET_MIN_SEQUENCE_IDENTITY / TARGET_MIN_INFORMATIVE_RESIDUES /
# TARGET_MIN_INFORMATIVE_FRACTION. DUPLICATED, NOT IMPORTED, and that is forced:
# modal_app.py copies run_pipeline.py into the image and nothing else, so
# _canary_scoring.py does not exist in production. Keep the two in step by hand.
_RENUMBER_MIN_IDENTITY = 0.9
_RENUMBER_MIN_INFORMATIVE = 10
# THE ABSOLUTE FLOOR ALONE IS NOT ENOUGH, and the canary has carried this second
# half since the day the first one was written. ``max(1, min(10, ref_informative))``
# is capped at what the reference offers so a genuinely tiny target still works —
# but a 200-residue reference of which 198 are UNK caps the bar at 2, and two
# coincidental matches then certify a map over all 200 residues. A FRACTION of
# the chain has to be evidence, not just a count of it.
_RENUMBER_MIN_INFORMATIVE_FRACTION = 0.5

# Records whose residue-sequence number lives in columns 23-26 (and whose
# insertion code lives in column 27), i.e. the ones a resseq rewrite must touch.
# TER is handled separately — see restore_design_numbering.
_RESSEQ_COORD_RECORDS = ("ATOM  ", "HETATM", "ANISOU", "SIGATM", "SIGUIJ")

# Records that ALSO carry residue numbers, at other column positions. Upstream's
# design output contains none of them (measured on the 8 archived Fc designs: a
# real design is MODEL / ATOM / TER / ENDMDL / END and nothing else). If one ever
# appears, renumbering only the coordinate section would leave the file
# self-inconsistent, so the restore declines instead — the direction that ships
# upstream's file unchanged.
#
# ``REMARK`` is NOT in this tuple and must not be: every real PDB carries
# REMARKs and refusing on all of them would disable the restore entirely. Only
# REMARK 465 (MISSING RESIDUES) carries residue numbers, and it is matched by
# its own two-field test in _annotation_refusal.
_RESSEQ_ANNOTATION_RECORDS = (
    "HELIX ", "SHEET ", "SSBOND", "LINK  ", "CISPEP", "SITE  ", "MODRES",
    "SEQADV", "DBREF ", "HET   ",
)


def pdb_ca_sequence(pdb_text: str) -> dict[str, list[tuple[int, str, str]]]:
    """``chain -> [(resseq, icode, resname), ...]`` ascending, over every CA.

    The sibling of ``pdb_ca_residues``, which returns ``(chain, resseq, icode)``
    and no residue NAME. A positional map has to be checked by sequence, so the
    names are the entire point, and a second parser is cheaper than changing a
    return shape that several callers depend on.

    Same model-1 / altloc / modified-residue rules as ``pdb_ca_residues``, so
    the two never disagree about what counts as a residue.

    THE INSERTION CODE IS PART OF THE RESIDUE ID, NOT DECORATION. This function
    used to de-dupe on ``(chain, resseq, icode)`` but STORE only
    ``(resseq, resname)``, which threw the icode away one line after computing
    it. Input residues ``A100``, ``A100A`` and ``A100B`` then became three
    entries all keyed 100, the renumber map's values collided, and the rewrite
    stamped three different design residues with residue number 100 and a blank
    icode — measured: a 200-residue chain with 3 insertion codes scored identity
    0.985, cleared the 0.9 floor, applied, and delivered a file containing
    ``{('A', 100, ' '): 3}``. That is a WORSE deliverable than the 1..N it
    replaced, and Kabat/Chothia-numbered antibodies — the target market — always
    carry insertion codes.

    THE SORT KEY IS ``(resseq, icode)``, NOT THE WHOLE TUPLE, for the same
    reason. Sorting ``(resseq, resname)`` broke ties on the RESIDUE NAME: file
    order TRP(100) / ALA(100A) / GLY(100B) parsed back as ALA / GLY / TRP,
    scrambling the positional correspondence before anything could check it.
    Blank icode sorts first because ``"" < "A"``, which is the PDB convention.

    KNOWN LIMIT, NOT FIXED: FIVE-DIGIT RESIDUE NUMBERS. PDB gives resSeq four
    columns, and writers that exceed it spill the fifth digit into the iCode
    column, so ``10000`` parses as ``(1000, "0")``. A target numbered ENTIRELY
    at or above 10000 still round-trips — the misparse is identical on both
    sides and preserves order, measured ``10000..10004 -> (1000,"0")..
    (1000,"4")`` and applied. A target that CROSSES the boundary does not:
    ``9998, 9999, 10000`` parse as ``(9998,""), (9999,""), (1000,"0")`` and
    sort with the last one FIRST, which scrambles the positional
    correspondence. That is fail-closed — measured identity 0.0, refused — but
    the refusal blames chain order rather than naming the real cause.
    """
    by_chain: dict[str, list[tuple[int, str, str]]] = {}
    seen: set[tuple[str, int, str]] = set()
    for line in pdb_text.split("\n"):
        record = line[:6]
        if record.startswith("ENDMDL"):
            break
        if record not in ("ATOM  ", "HETATM"):
            continue
        if line[12:16].strip() != "CA":
            continue
        resname = line[17:20].strip().upper()
        if record == "HETATM" and resname not in _MODRES_EQUIV:
            continue
        chain = line[21:22].strip()
        try:
            resseq = int(line[22:26])
        except ValueError:
            continue
        icode = line[26:27].strip()
        key = (chain, resseq, icode)
        if key in seen:
            continue
        seen.add(key)
        by_chain.setdefault(chain, []).append((resseq, icode, resname))
    for chain in by_chain:
        by_chain[chain].sort(key=lambda e: (e[0], e[1]))
    return by_chain


def _is_unknown_resname(name: str) -> bool:
    """``UNK`` / ``UNX`` / ``XAA`` / ``X`` — "I do not know what this is"."""
    return str(name).strip().upper() in _UNKNOWN_RESNAMES


def _same_resname(a: str, b: str) -> bool:
    """Do two residue names denote the same residue for identity purposes?

    Equal, or equal after folding a modified residue to its parent. MSE and MET
    are the same residue; an upstream refold that writes selenomethionine back
    as methionine has not changed the protein, and calling that a mismatch is
    how a real target drops below the 0.9 floor and silently ships in 1..N.

    NO UNKNOWN WILDCARD HERE, unlike the canary's ``same_residue``. Every caller
    restricts the comparison to INFORMATIVE pairs first — both names known — so
    an unknown can never reach this function, and adding a wildcard it does not
    need would only invite someone to aggregate it into a fraction later.
    """
    a, b = str(a).strip().upper(), str(b).strip().upper()
    return _MODRES_PARENT.get(a, a) == _MODRES_PARENT.get(b, b)


def chain_renumber_map(observed: list[tuple[int, str, str]],
                       reference: list[tuple[int, str, str]],
                       *,
                       min_identity: float = _RENUMBER_MIN_IDENTITY,
                       min_informative: int = _RENUMBER_MIN_INFORMATIVE,
                       min_informative_fraction: float = (
                           _RENUMBER_MIN_INFORMATIVE_FRACTION),
                       ) -> dict:
    """Position-for-position map from a design chain's numbering to the input's.

    Both lists are ``(resseq, icode, resname)`` ASCENDING BY ``(resseq, icode)``.
    The i-th residue of the design chain is taken to be the i-th residue of the
    input chain — and that assumption is then CHECKED against the residue names
    rather than trusted, because assuming it is how a rewrite ends up keyed to
    the wrong chain.

    Returns ``{"ok", "reason", "map", "n", "n_informative", "identity"}``, where
    ``map`` is ``{(design_resseq, design_icode): (input_resseq, input_icode)}``.
    ``ok`` is False on any doubt and ``map`` is then empty.

    WHAT IT REFUSES, AND WHY EACH REFUSAL EARNS ITS PLACE. A binder relabelled
    onto a target's chain id fails on length. A binder that happens to match on
    length fails on sequence, because a de-novo binder is not the target. A chain
    of nothing but UNK fails the informative floor — unknown residues compare
    equal to anything, so without that floor an all-UNK chain scores a perfect
    identity against any reference at all and would certify a map that renumbers
    the wrong chain onto the operator's keys. A chain that is MOSTLY unknown
    fails the informative FRACTION for the same reason at a smaller scale.
    A map that is not one-to-one fails injectivity, because collapsing two
    design residues onto one input residue id emits a file with duplicate
    residues in it — strictly worse than the 1..N it replaced.

    Residue-name comparison folds modified residues to their parent
    (``_same_resname``). It used to be exact ``==``, on the argument that only a
    false MATCH can do damage — true as far as it goes, but it ignored the cost
    of the other direction. A selenomethionine target comes back from an
    upstream refold with MSE written as MET; exact comparison scores every one
    of those a mismatch, a target with enough of them drops below 0.9, and the
    operator receives 1..N with no indication that anything was declined. That
    is not a free refusal, it is the defect this function exists to fix, firing
    on a correct input.
    """
    n_obs, n_ref = len(observed), len(reference)
    if n_obs != n_ref:
        return {"ok": False, "n": n_obs, "n_informative": 0, "identity": None,
                "map": {},
                "reason": (f"length differs: {n_obs} residues in the design, "
                           f"{n_ref} in the input chain — a positional map needs "
                           f"one residue per residue")}
    if not n_obs:
        return {"ok": False, "n": 0, "n_informative": 0, "identity": None,
                "map": {}, "reason": "the chain carries no CA residue"}

    pairs = [(o[-1], r[-1]) for o, r in zip(observed, reference)]
    informative = [(a, b) for a, b in pairs
                   if not _is_unknown_resname(a) and not _is_unknown_resname(b)]
    identical = sum(1 for a, b in informative if _same_resname(a, b))
    identity = (identical / len(informative)) if informative else None
    ref_informative = sum(1 for entry in reference
                          if not _is_unknown_resname(entry[-1]))
    floor = max(1, min(int(min_informative), int(ref_informative)))
    fraction = len(informative) / n_obs

    if len(informative) < floor:
        return {"ok": False, "n": n_obs, "n_informative": len(informative),
                "identity": identity, "map": {},
                "reason": (f"only {len(informative)} informative residue pair(s), "
                           f"below the {floor} required — an all-unknown chain "
                           f"matches anything and must not certify a map")}
    if fraction < min_informative_fraction:
        return {"ok": False, "n": n_obs, "n_informative": len(informative),
                "identity": identity, "map": {},
                "reason": (f"only {len(informative)} of the {n_obs} residues "
                           f"carry a known name on both sides "
                           f"({fraction:.0%}, need "
                           f"{min_informative_fraction:.0%}) — a predominantly "
                           f"sequence-free chain is mostly wildcard matches, "
                           f"not evidence")}
    if identity is None or identity < min_identity:
        return {"ok": False, "n": n_obs, "n_informative": len(informative),
                "identity": identity, "map": {},
                "reason": (f"sequence identity {identity!r} over "
                           f"{len(informative)} informative pair(s) is below "
                           f"{min_identity} — these are not the same chain in "
                           f"the same order")}

    mapping = {(o[0], o[1]): (r[0], r[1]) for o, r in zip(observed, reference)}
    # INJECTIVITY, as a backstop rather than a live case. ``pdb_ca_sequence``
    # de-dupes on ``(chain, resseq, icode)``, so a reference parsed by it cannot
    # offer two identical residue ids and this can only fire on a hand-built
    # list. It stays because the version of this file that shipped WITHOUT it
    # was one dropped field away from emitting duplicate residues, and the
    # symptom — a valid-looking file the operator cannot key into — is exactly
    # the one the whole function exists to prevent. A map that is not one-to-one
    # must never be applied to anything.
    if len(set(mapping.values())) != len(mapping):
        return {"ok": False, "n": n_obs, "n_informative": len(informative),
                "identity": (None if identity is None else round(identity, 4)),
                "map": {},
                "reason": ("the map is not one-to-one: two design residues "
                           "would be given the same input residue id, which "
                           "would emit a file with duplicate residues in it")}
    return {"ok": True, "n": n_obs, "n_informative": len(informative),
            "identity": round(identity, 4),
            "map": mapping,
            "reason": ""}


def _annotation_refusal(line: str) -> str | None:
    """The record type on ``line`` that this rewrite cannot maintain, or None.

    ``REMARK 465`` (MISSING RESIDUES) is matched on its two leading fields
    rather than by membership in ``_RESSEQ_ANNOTATION_RECORDS``: every real PDB
    carries REMARKs, so refusing on the record name alone would disable the
    restore on any file that has one, while 465 specifically tabulates residue
    numbers that renumbering the coordinates would silently invalidate.
    """
    record = line[:6]
    if record in _RESSEQ_ANNOTATION_RECORDS:
        return record.strip()
    if record == "REMARK" and line[7:10].strip() == "465":
        return "REMARK 465"
    return None


def restore_design_numbering(pdb_text: str,
                             target_chains: list[str],
                             reference: dict[str, list[tuple[int, str, str]]],
                             *,
                             min_identity: float = _RENUMBER_MIN_IDENTITY,
                             min_informative: int = _RENUMBER_MIN_INFORMATIVE,
                             min_informative_fraction: float = (
                                 _RENUMBER_MIN_INFORMATIVE_FRACTION),
                             ) -> tuple[str, dict]:
    """Rewrite a design's TARGET chains back into the input's residue numbering.

    Returns ``(text, report)``. ``text`` is the rewritten file when
    ``report["applied"]`` and the input string unchanged otherwise. The binder
    chain is never touched: it is a de-novo chain with no input to be numbered
    against, and 1..N is the only numbering it has ever had.

    ALL TARGET CHAINS MAP OR NONE IS APPLIED. A partial rewrite would leave one
    target chain keyed to the operator's input and another to upstream's 1..N,
    which is harder to reason about than either alone.

    AND THEIR COORDINATE RECORDS THAT THE MAP HAS NO KEY FOR — but only where
    leaving one where it is would actually collide. The map is built from CA
    atoms, so a residue modelled without a CA, a HETATM ion or ligand on a
    target chain, or a second MODEL numbered differently from the first yields
    coordinate records that are not keys. Such a record keeps the number it
    has, and that is a problem in exactly one case: when some OTHER residue is
    being renumbered ONTO that number, so the delivered file would carry two
    different residues on one residue id. Those are counted and the whole file
    is refused. A record nothing is moving onto stays where it is and the
    restore still applies — refusing it would cost the shard its numbering for
    a file that is fine.

    ONLY MODEL 1 DEFINES THE MAP, WHILE THE REWRITE WALKS THE WHOLE FILE.
    ``pdb_ca_sequence`` stops at the first ``ENDMDL``, so a multi-model file's
    later models are rewritten with model 1's map. That is correct exactly when
    the models share a numbering, which is the normal case and the only one
    upstream emits (the 8 archived designs are single-model). A later model
    numbered DIFFERENTLY is caught only where its numbers land on ids model 1
    is being renumbered onto; where they do not, that model keeps its own
    numbers and the file goes out with two models in two numberings. Nothing
    upstream writes reaches that state — it is named here rather than claimed
    closed.

    WHAT IS GUARANTEED ABOUT DUPLICATE RESIDUE IDS, PRECISELY: this rewrite
    never CREATES one. It cannot do so through the map, which is checked
    one-to-one, so two records reach one destination only if they already
    shared a source id; the unmapped-record refusal covers the only other way.
    It does not REMOVE one either — a design that arrives with a ligand sitting
    on a real residue's number is delivered with both moved together, because
    refusing hands the operator the same two residues on the same id and takes
    their numbering away as well.

    BOTH THE resSeq COLUMNS AND THE iCode COLUMN ARE WRITTEN. A residue id in
    PDB is ``(chain, resSeq, iCode)``, and an earlier version of this function
    wrote only ``resSeq`` — which mapped ``A100 / A100A / A100B`` onto three
    residues all numbered 100 with a blank icode. Writing four columns and
    leaving the fifth alone is not a partial fix, it is a corrupted file.

    TER IS SET FROM THE LAST COORDINATE RECORD SEEN FOR THAT CHAIN SO FAR IN THE
    FILE, NOT THROUGH THE MAP, and that is measured rather than stylistic.
    Upstream's TER records already carry a cumulative index that is not in the
    chain's own numbering space: in a real design, chain B's final ATOM is
    ``SER B 208`` while its TER reads ``SER B 419``, and chain C's TER names a
    different residue than the atom it follows. Those numbers are not keys in
    the map and a map lookup would skip them — which would be fine for B and C,
    but chain A's TER *does* agree with its atoms, so skipping it would leave
    ``TER ... A 211`` beside atoms renumbered to 234-444 and break something
    that was correct. "So far in the file" is the honest statement of the rule
    and differs from "the chain's last coordinate record" for a file whose
    chains are INTERLEAVED; upstream writes each chain contiguously, so on the
    measured input the two coincide.

    NEVER RAISES. A design that reaches this function has already been paid for;
    losing it over a numbering nicety would be a far worse outcome than shipping
    upstream's keys. Any failure returns the input text with a reason.

    KNOWN LIMIT, NOT FIXED: a BARE three-character ``TER`` refuses the whole
    restore when a target chain has a BLANK chain id. ``line[21:22]`` is ``""``
    on such a line, a blank chain id is a legal key in ``remap``, and
    ``_splice_resid`` cannot write a residue id into a 3-character line — so it
    returns None and the file is declined. Fail-closed and rare (upstream pads
    its TER records to 80 columns), and over-refusing costs only the numbering,
    never the design.
    """
    report: dict = {"applied": False, "already_input_numbering": False,
                    "chains": {}, "reason": ""}
    try:
        # NOT ``if c``: a blank chain id is a legal PDB chain and a legitimate
        # key in ``reference``. Filtering it out here dropped it from ``wanted``
        # while leaving the rest of the file rewritten, which quietly broke the
        # all-or-none promise for exactly the input least likely to be noticed.
        wanted = sorted(set(target_chains))
        if not wanted:
            report["reason"] = "no target chains were named"
            return pdb_text, report

        observed = pdb_ca_sequence(pdb_text)
        chains: dict[str, dict] = {}
        for chain in wanted:
            if chain not in observed:
                chains[chain] = {"ok": False, "n": 0, "n_informative": 0,
                                 "identity": None, "map": {},
                                 "reason": "chain absent from the design output"}
            elif chain not in reference:
                chains[chain] = {"ok": False, "n": len(observed[chain]),
                                 "n_informative": 0, "identity": None, "map": {},
                                 "reason": "chain absent from the input target"}
            else:
                chains[chain] = chain_renumber_map(
                    observed[chain], reference[chain],
                    min_identity=min_identity, min_informative=min_informative,
                    min_informative_fraction=min_informative_fraction)
        report["chains"] = chains

        # THE SEQUENCE CHECK GATES "already", it does not bypass it. Matching
        # residue ids alone are not evidence that the design's chain A is the
        # input's chain A: a target numbered from 1 (an AlphaFold model, say)
        # against a design where upstream emitted the BINDER as chain A gives
        # identical key lists and 5% sequence identity, and this used to return
        # before the ok check and report ``target_numbering: "input"`` for it.
        # Requiring ok first means "already" can only be claimed for a chain the
        # code has actually recognised; when the keys match but the sequence
        # does not, the refusal below names the real problem.
        already = (
            all(chains[c]["ok"] for c in wanted)
            and all([(k, i) for k, i, _ in observed.get(c, [])]
                    == [(k, i) for k, i, _ in reference.get(c, [])]
                    for c in wanted))
        report["already_input_numbering"] = already
        if already:
            report["reason"] = "the design already carries the input numbering"
            return pdb_text, report
        if not all(c["ok"] for c in chains.values()):
            report["reason"] = "; ".join(
                f"chain {c}: {v['reason']}"
                for c, v in sorted(chains.items()) if not v["ok"])
            return pdb_text, report

        remap = {c: chains[c]["map"] for c in wanted}
        # Columns 23-26 hold resSeq and column 27 holds the insertion code — four
        # characters and one. A value that does not fit would shift every column
        # to its right, so refuse the whole file rather than emit a corrupted
        # one. The icode half is not hypothetical padding: it is the field the
        # first version of this rewrite dropped.
        for chain, mapping in remap.items():
            for value, icode in mapping.values():
                if len(f"{value:d}") > 4:
                    report["reason"] = (
                        f"chain {chain}: input residue number {value} does not "
                        f"fit the four columns PDB gives resSeq")
                    return pdb_text, report
                if len(icode) > 1:
                    report["reason"] = (
                        f"chain {chain}: insertion code {icode!r} does not fit "
                        f"the single column PDB gives iCode")
                    return pdb_text, report

        lines = pdb_text.split("\n")
        for line in lines:
            found = _annotation_refusal(line)
            if found:
                report["reason"] = (
                    f"the design carries {found} records, whose residue numbers "
                    f"this rewrite does not maintain — renumbering only the "
                    f"coordinates would leave the file self-inconsistent")
                return pdb_text, report

        out: list[str] = []
        last_seen: dict[str, tuple[int, str]] = {}
        # A COORDINATE RECORD THE MAP HAS NO KEY FOR IS ONLY A PROBLEM WHEN
        # SOMETHING IS MOVING ONTO IT. The injectivity refusal above proves the
        # MAP is one-to-one and says nothing about such records.
        # ``pdb_ca_sequence`` builds the map from CA atoms only, so a residue
        # modelled without a CA, a HETATM ligand or ion sitting on a target
        # chain, or a second model whose numbering differs from the first all
        # produce them — and this loop used to hand them straight through,
        # keeping upstream's number while every neighbour moved. Measured:
        # ``[A100..A109, A105 ZN]`` against a 100..109 reference delivered
        # duplicate residue ids with ``applied`` True and the payload claiming
        # the operator's numbering was restored. That is the exact outcome the
        # one-to-one refusal exists to prevent, reached around the side.
        #
        # THE FIRST VERSION OF THIS GUARD REFUSED ON ALL OF THEM, and gave that
        # collision as its reason for every one. Measured false: a ``HETATM ZN``
        # at ``A9000`` against a 234-253 reference collides with nothing, and
        # refusing it shipped the WHOLE shard in upstream's 1..N — both target
        # chains, every design, the operator's hotspot labels no longer
        # resolving and the results page raising its warning banner. One benign
        # heteroatom for the entire feature, on a stated reason that was false
        # about that file. The test is therefore the collision itself: the
        # record's own ``(resseq, icode)`` being a value of the map, i.e. an id
        # this rewrite is renumbering some other residue onto.
        #
        # COUNTED BY RESIDUE, NOT BY RECORD. One CA-less tryptophan is 8 atoms
        # plus their 8 ``ANISOU`` lines; "16 coordinate record(s)" with the same
        # residue named three times in the sample sends an operator looking for
        # sixteen problems. Keying on the residue id is also what bounds this:
        # the keys are a subset of the map's values, so it cannot outgrow the
        # map however many records a pathological file piles onto one residue.
        #
        # No archived design can trigger this: all 8 are ATOM-only, one model,
        # every residue has a CA, and ``crop_pdb_to_contig`` strips ligands,
        # ions and waters out of the staged input. Verified 8/8 still applied.
        destinations = {c: set(m.values()) for c, m in remap.items()}
        collisions: dict[tuple[str, int, str], None] = {}
        for line in lines:
            record = line[:6]
            chain = line[21:22].strip() if len(line) > 21 else ""
            if record in _RESSEQ_COORD_RECORDS and chain in remap:
                icode = line[26:27].strip()
                try:
                    old = int(line[22:26])
                except ValueError:
                    # A residue number that is not a number cannot be a
                    # destination either: every id this rewrite writes comes
                    # out of ``f"{n:4d}"`` and ``int`` reads every one of those
                    # back, so nothing can be renumbered onto this record. It
                    # keeps its own field, exactly as it arrived.
                    out.append(line)
                    continue
                new = remap[chain].get((old, icode))
                if new is None:
                    if (old, icode) in destinations[chain]:
                        collisions[(chain, old, icode)] = None
                    out.append(line)
                    continue
                spliced = _splice_resid(line, new)
                if spliced is None:
                    report["reason"] = (
                        f"chain {chain}: a coordinate record is too short at "
                        f"{len(line)} characters to carry the residue id "
                        f"{new[0]}{new[1]} without changing the line's width")
                    return pdb_text, report
                last_seen[chain] = new
                out.append(spliced)
                continue
            if record.startswith("TER") and chain in remap and chain in last_seen:
                spliced = _splice_resid(line, last_seen[chain])
                if spliced is None:
                    report["reason"] = (
                        f"chain {chain}: a TER record is too short at "
                        f"{len(line)} characters to carry the residue id "
                        f"{last_seen[chain][0]}{last_seen[chain][1]} without "
                        f"changing the line's width")
                    return pdb_text, report
                out.append(spliced)
                continue
            out.append(line)

        if collisions:
            shown = ", ".join(f"chain {c} residue {r}{i}"
                              for c, r, i in list(collisions)[:3])
            report["reason"] = (
                f"{len(collisions)} residue(s) on a chain being renumbered are "
                f"not in the map and sit on residue ids this rewrite is moving "
                f"other residues onto ({shown}"
                f"{', ...' if len(collisions) > 3 else ''}) — leaving them "
                f"there would emit a file with two different residues sharing "
                f"one residue id")
            return pdb_text, report

        report["applied"] = True
        return "\n".join(out), report
    except Exception as exc:  # noqa: BLE001 — never lose a paid design to this
        report["applied"] = False
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return pdb_text, report


def _splice_resid(line: str, resid: tuple[int, str]) -> str | None:
    """``line`` with columns 23-26 set to ``resSeq`` and column 27 to ``iCode``.

    ALWAYS LENGTH-PRESERVING, or ``None``. Returning a longer line would shift
    every column to its right in a fixed-width format, and the caller refuses
    the whole file on ``None`` rather than emit one. Three cases:

    * 27 characters or more — the iCode column exists; splice both fields.
    * exactly 26 — resSeq is complete but the file ends before iCode. Writing a
      BLANK icode into a column that does not exist is a no-op, so the line is
      rewritten without one; a REAL icode has nowhere to go and returns None.
      Real files do this: the archived input target's TER records are 26
      characters plus a trailing space, and a 26-character TER is valid PDB.
    * fewer than 26 — the resSeq field itself is truncated; nothing can be
      written into it safely.
    """
    seq, icode = resid
    if len(line) < 26:
        return None
    if len(line) < 27:
        return None if icode else f"{line[:22]}{seq:>4d}"
    return f"{line[:22]}{seq:>4d}{(icode or ' '):1s}{line[27:]}"


def parse_target_input(spec: str) -> list[tuple[str, Optional[int], Optional[int]]]:
    """Parse a contig such as ``A1-150`` or ``A12-157,B12-157,C12-157``.

    A bare chain id yields ``(chain, None, None)`` meaning "the whole chain".
    The adapter already validated the syntax; this re-parses in-container
    because the container must never trust a value it did not check itself.
    Raises ValueError on anything unparsable.
    """
    out: list[tuple[str, Optional[int], Optional[int]]] = []
    for token in (t.strip() for t in (spec or "").replace(";", ",").split(",")):
        if not token:
            continue
        if len(token) == 1 and token.isalpha():
            out.append((token, None, None))
            continue
        chain, rest = token[0], token[1:]
        if not chain.isalpha() or "-" not in rest[1:]:
            raise ValueError(f"unparsable target_input segment {token!r}")
        # rsplit so a negative lower bound (e.g. "A-5-20") still splits right.
        lo_text, hi_text = rest.rsplit("-", 1)
        try:
            lo, hi = int(lo_text), int(hi_text)
        except ValueError:
            raise ValueError(f"unparsable target_input segment {token!r}") from None
        out.append((chain, lo, hi))
    return out


def derive_segments(
    residues: list[tuple[str, int, str]], chain_ids: list[str]
) -> list[tuple[str, int, int]]:
    """Full observed residue span per requested chain.

    Used when the caller gave a target chain but no explicit contig. Upstream's
    ``target_input`` defaults to ``"A1-100"`` when omitted, which would silently
    truncate a 250-residue target to its first 100 residues, so the contig is
    ALWAYS written explicitly — this is what it is computed from.
    """
    out: list[tuple[str, int, int]] = []
    for chain in chain_ids:
        nums = [r[1] for r in residues if r[0] == chain]
        if not nums:
            continue
        out.append((chain, min(nums), max(nums)))
    return out


def expand_bare_chains(
    residues: list[tuple[str, int, str]],
    segments: list[tuple[str, Optional[int], Optional[int]]],
) -> list[tuple[str, Optional[int], Optional[int]]]:
    """``(chain, None, None)`` — "the whole chain" — as an explicit span.

    ``parse_target_input`` yields a bare chain id with no bounds, and every
    check downstream of it compares numbers: ``unrenderable_segments`` reads
    ``lo < 0``, ``format_contig`` renders ``lo``-``hi``, and upstream's contig
    regex needs digits. Resolving the bounds FIRST is what makes those checks
    apply to ``--contig A`` at all.

    IT IS EXTRACTED BECAUSE THE CANARY HAD NO EXPANSION AND THEREFORE NO
    GUARDS. ``_hotspot_canary`` filtered unexpanded segments OUT before asking
    ``unrenderable_segments``, so ``--contig A`` on a construct numbered from
    -5 skipped the negative-numbering refusal entirely and spawned ~$4 (phase
    1) or ~$12 (phase 2) to die in ``from_contig``. Production does not refuse
    that input either — it EXPANDS it to ``A-5-240`` and then refuses it for
    the right reason. A canary that refused the bare id itself would be
    over-refusing, which on this branch is its own defect class: it stops runs
    production would have accepted.

    A chain absent from the upload has no span to expand to and is returned
    UNCHANGED, still carrying its ``None``s. That is deliberate: it is not this
    function's business to decide what an unresolvable chain means, and both
    callers already have a refusal for it — ``prepare_custom_target`` names it
    ("chain Z is not present"), the canary reaches it through
    ``empty_segments``. Anything downstream that compares bounds must therefore
    tolerate a ``None`` lower bound; ``unrenderable_segments`` does.
    """
    out: list[tuple[str, Optional[int], Optional[int]]] = []
    for chain, lo, hi in segments:
        if lo is None:
            nums = [r[1] for r in residues if r[0] == chain]
            out.append((chain, min(nums), max(nums)) if nums else (chain, lo, hi))
        else:
            out.append((chain, lo, hi))
    return out


def select_residues(
    residues: list[tuple[str, int, str]],
    segments: list[tuple[str, Optional[int], Optional[int]]],
) -> list[tuple[str, int]]:
    """Residues selected by the contig, as (chain, resseq) in file order."""
    out: list[tuple[str, int]] = []
    for chain, lo, hi in segments:
        for c, resseq, _icode in residues:
            if c != chain:
                continue
            if lo is not None and not (lo <= resseq <= hi):
                continue
            out.append((c, resseq))
    return out


def selected_residue_keys(
    residues: list[tuple[str, int, str]],
    segments: list[tuple[str, Optional[int], Optional[int]]],
) -> set[tuple[str, int, str]]:
    """``select_residues`` as a SET of full residue keys, insertion code kept.

    ``select_residues`` returns a list of ``(chain, resseq)`` in file order and
    REPEATS a residue named by two overlapping segments. That is right for
    hotspot matching, which set-ifies through ``hotspot_keys`` before it
    compares anything, and it is right for cropping only after this function
    has de-duplicated it — the question there is "which residue lines survive".

    IT IS WRONG FOR ANYTHING THAT COUNTS, and this docstring used to claim the
    opposite ("right for the size gate"). The size gate believed it: measured
    on a 60-residue chain, ``--contig A10-20`` counted 11 and was refused, while
    ``--contig A10-20,A10-20`` counted 22 for the same 11 residues and was not.
    One comma bought a run of exactly the input the floor exists to stop. On the
    WEB route production was shielded from that by the adapter, which at the
    time rejected a chain named twice at all; it now refuses only OVERLAPPING
    ranges on one chain (``tools/proteina/__init__.py::_parse_target_input``),
    because ``A1-50,A60-240`` is the correct — and until then un-typable —
    contig for a chain with a disordered loop, and the flat rule made it
    unsayable. ``A10-20,A10-20`` still fails there. What matters here is that
    the shield was never the defence: ``prepare_custom_target`` is reached with
    contigs the adapter never saw and the canary does not go through the adapter
    at all, so THIS de-duplicated count is what holds the floor.
    ``target_too_small`` counts it, which is also what the crop stages.

    The insertion code is carried because ``A100`` and ``A100A`` are two
    residues with two CA atoms — upstream counts both, so the crop has to keep
    both.
    """
    keep: set[tuple[str, int, str]] = set()
    for chain, lo, hi in segments:
        for c, resseq, icode in residues:
            if c != chain:
                continue
            if lo is not None and not (lo <= resseq <= hi):
                continue
            keep.add((c, resseq, icode))
    return keep


# The only records the crop carries over. ANISOU is deliberately NOT here: to
# drop it alongside a rejected ligand it would have to be matched back to its
# parent atom, nothing downstream reads an anisotropic B-factor, and a dangling
# ANISOU is worse than no ANISOU.
_CROP_COORD_RECORDS = ("ATOM  ", "HETATM")


def crop_pdb_to_contig(
    text: str, keep: set[tuple[str, int, str]]
) -> str:
    """The uploaded PDB reduced to exactly the residues the contig selects.

    WHY THIS EXISTS. Upstream's evaluate stage asserts a COUNT, in
    ``proteinfoundation/metrics/metric_utils.py``::

        assert (np.isin(gen_pdb.chain_id, gen_pdb_target_chain)).sum() == len(target_seq)

    The left side counts CA atoms of the target chains in the GENERATED complex,
    which contains only the contig's selection — ``pdb_utils`` masks the target
    through ``AtomSelectionStack.from_contig`` before the model sees it. The
    right side is ``len(target_seq)``, built in ``binder_eval_utils`` from the
    STAGED file restricted to the chains the contig NAMES: that chain set is
    ``sorted(set(x[0] for x in target_input.split(",")))``, the letters only,
    with the ranges thrown away. Nothing crops the file on that path.

    So upstream silently requires the contig to select every CA residue present
    in each chain it names. 42 of its own 44 curated targets happen to satisfy
    that. A sub-range does not, and the run dies in ``evaluate`` after the GPU
    has generated and scored every design — the most expensive place to learn
    it. Cropping here makes the invariant true by construction, and the contig
    stays exactly as the user wrote it (``--target-input`` is never omitted:
    upstream defaults it to ``"A1-100"``, which would silently truncate).

    WHAT SURVIVES, AND WHY IT IS THIS AND NOT MORE:

    * ``ATOM`` lines, and ``HETATM`` lines for a modified residue in
      ``_MODRES_EQUIV``, whose ``(chain, resseq, icode)`` is in ``keep`` —
      verbatim, byte for byte, so the AUTHOR NUMBERING is
      untouched. Hotspot matching upstream is the literal concatenation
      ``f"{chain_id}{res_id}"``, and this wrapper's own preflight and
      ``missing_hotspots`` are built on the same string, so a crop that
      renumbered would silently move every hotspot. Renumbering is the one
      thing this function must never do.
    * one ``TER`` per chain, synthesised from that chain's last kept atom.
      Chains are unambiguous from column 22 alone, but a reader that infers
      polymer breaks from ``TER`` should not see two chains fused.
    * a final ``END``.

    Everything else is dropped, and the dropped records are the point rather
    than an oversight:

    * Residues OUTSIDE the ranges, and every residue of a chain the contig does
      not name. That second one is the whole reason a 4-chain deposit works: the
      right-hand side counts every CA in the named chains, so chains C and D may
      stay, but nothing of A or B outside the range may.
    * EVERY water, ion and ligand, whatever it is numbered. In the real campaign
      input the 20 waters sit at resid 1-30, outside every range, so the range
      rule alone would have removed those — but a ligand numbered INSIDE a range
      would have ridden along, and that is the case that matters. A ``HETATM``
      is kept only when its residue name is in ``_MODRES_EQUIV``, i.e. only when
      ``pdb_ca_residues`` COUNTED it; the file that comes out therefore holds
      exactly the residues this wrapper counted and nothing else. That is what
      makes the left-hand side of upstream's assertion unable to exceed our
      count: a modified residue outside ``_MODRES_EQUIV`` that biotite happens
      to call protein cannot inflate a file it is no longer in. (The converse —
      a residue we count that biotite does NOT — is still open; see the module
      note on ``_MODRES_EQUIV``.) The custom-target path is ``protein_binder``
      only (``_CUSTOM_TARGET_PRESETS``), which does not model a co-factor, so
      nothing that was being used is lost.
    * ``SEQRES``, ``CONECT``, ``SSBOND``, ``HELIX``, ``SHEET``, ``LINK``,
      ``SITE`` and the rest of the annotation block. Each describes residues or
      bonds that the crop has removed. ``SEQRES`` is the load-bearing one: it
      declares the FULL chain sequence, so any code deriving a target sequence
      from it rather than from coordinates would read the uncropped length back
      and the assertion would fire anyway. Dropping it forces coordinates.
    * Everything after the first ``ENDMDL``. ``pdb_ca_residues`` counts model 1
      only, so on an NMR ensemble the count and the file would otherwise
      disagree by a factor of however many models were deposited.

    Idempotent: cropping an already-cropped file returns the same residue set.
    """
    by_chain: dict[str, list[str]] = {}
    for raw in text.splitlines():
        record = raw[:6]
        if record.startswith("ENDMDL"):
            break
        if record not in _CROP_COORD_RECORDS:
            continue
        if record == "HETATM" and raw[17:20].strip().upper() not in _MODRES_EQUIV:
            # The same protein test ``pdb_ca_residues`` applies, moved from the
            # CA atom to every atom of the residue. Water, ions and ligands go
            # even when they are numbered inside a range.
            continue
        chain = raw[21:22].strip()
        try:
            resseq = int(raw[22:26])
        except ValueError:
            # The same columns, read the same way, as ``pdb_ca_residues``: a
            # line this cannot place is a line that cannot be proven to be
            # inside the contig, so it goes. Note what that does NOT cover — a
            # resSeq >= 10000 overruns into column 27 and reads back as a
            # DIFFERENT number rather than raising here. That misparse is
            # unfixed (see pdb_ca_residues), and the reason it does not break
            # the crop is that both sides make it identically: the keep key and
            # the count are built from the same wrong number, so they agree.
            continue
        if (chain, resseq, raw[26:27].strip()) not in keep:
            continue
        # Bucketed by chain rather than streamed, so an input that interleaves
        # chains still comes out with each chain contiguous and ONE TER. Within
        # a chain the original line order — and therefore the original residue
        # order — is preserved exactly.
        by_chain.setdefault(chain, []).append(raw.rstrip("\r\n"))

    out: list[str] = []
    for chain, lines in by_chain.items():
        out.extend(lines)
        # Cols 1-6 record, 7-11 serial, 12-17 blank, 18-27 resName/chain/resSeq
        # /iCode lifted straight off the last atom so the TER names a residue
        # that is actually still in the file. The serial is 0: PDB readers key
        # chains off column 22, TER serials are not referenced by anything the
        # crop emits (CONECT is dropped), and inventing a plausible-looking one
        # would be the more misleading choice.
        out.append(f"TER   {0:5d}      {lines[-1][17:27]}")
    out.append("END")
    return "\n".join(out) + "\n"


class TargetCropError(ValueError):
    """The staged file does not satisfy the count upstream will assert.

    A distinct type, not a bare ValueError, because the two callers of
    ``stage_cropped_target`` must convert it differently and neither may
    swallow it: production turns it into ``_fail("input", "target_crop", ...)``
    and exits, the canary turns it into a refusal record and returns. Sharing
    the raise but not the handling is the only way both get the same verdict
    from the same code.
    """


def stage_cropped_target(
    dest: Path,
    pdb_text: str,
    residues: list[tuple[str, int, str]],
    segments: list[tuple[str, Optional[int], Optional[int]]],
) -> tuple[int, int]:
    """THE staging step: write ``pdb_text`` cropped to ``segments`` at ``dest``.

    ONE FUNCTION, TWO CALLERS, BY CONSTRUCTION. ``prepare_custom_target`` calls
    it and so does ``_hotspot_canary._stage``, and that is the whole point of
    its existing separately from either.

    WHAT IT COST TO LEARN THIS. The crop was written inline in
    ``prepare_custom_target``. ``_stage`` — whose docstring said "Stage the
    target EXACTLY the way ``prepare_custom_target`` does", under a
    block-capital "THE CANARY MUST NOT EXERCISE A PATH PRODUCTION NEVER RUNS" —
    kept doing ``p.write_text(pdb_text)``. That claim was true when it was
    written and the crop commit made it false, in the one file whose entire job
    is fidelity to production. A paid A100 phase-1 shard then staged the
    uncropped file and reproduced the exact assertion the crop prevents
    (``metric_utils.py:217``, contig ``A236-300,B236-300`` on 3S7G). Production
    was correct throughout; the harness that exists to prove it was not.

    ``_stage_dir`` already had the right idea for the PATH — "Derived, not
    copied, so the two cannot drift: if prod's staging directory moves, the
    canary follows it in the same commit or not at all." This applies it to the
    BYTES. A canary that re-implements the crop can drift again; a canary that
    calls this cannot.

    THE SELF-CHECK IS INSIDE, deliberately, and it is the half that pays for
    itself. It is upstream's own comparison made locally: CA residues of the
    written file restricted to the chains the contig NAMES (ranges discarded,
    exactly as ``binder_eval_utils`` does it) against the residues the contig
    SELECTS. Had it been shared from the start, the canary's uncropped staging
    would have raised here — before the GPU — instead of after it.

    Returns ``(n_staged, n_selected)``, the two numbers upstream compares.
    Raises ``TargetCropError`` when they disagree and ``OSError`` if the write
    fails; neither is caught here, because what to do about them is the
    caller's to decide and the two callers decide differently.
    """
    keep = selected_residue_keys(residues, segments)
    dest.write_text(crop_pdb_to_contig(pdb_text, keep))
    staged_residues, _ = pdb_ca_residues(dest)
    named_chains = {chain for chain, _lo, _hi in segments}
    n_staged = sum(1 for c, _r, _i in staged_residues if c in named_chains)
    if n_staged != len(keep):
        # Not ``format_contig``: a bare chain id parses to (chain, None, None)
        # — legal input to the crop, and "ANone-None" in a refusal message an
        # operator is meant to act on.
        shown = ",".join(
            f"{c}{lo}-{hi}" if lo is not None else c for c, lo, hi in segments)
        raise TargetCropError(
            f"cropping the uploaded target to {shown} left "
            f"{n_staged} residue(s) in chain(s) {'/'.join(sorted(named_chains))} "
            f"but the range selects {len(keep)}. The design engine compares "
            "exactly these two numbers and would have failed after the GPU work "
            "was already paid for."
        )
    return n_staged, len(keep)


def hotspot_keys(selected: list[tuple[str, int]]) -> set[str]:
    """The exact key set upstream matches against: chain id + author number,
    concatenated, no separator, case preserved."""
    return {f"{chain}{resseq}" for chain, resseq in selected}


def missing_hotspots(
    selected: list[tuple[str, int]], spec: list[str]
) -> list[str]:
    """Hotspot tokens that match NO selected residue.

    Case-sensitive and literal, exactly like upstream — a lowercase chain
    ``a45`` against an ``A45`` residue is a miss there and must be a miss here,
    or we would wave through the run that upstream then silently unconstrains.
    """
    available = hotspot_keys(selected)
    return [token for token in spec if token not in available]


def hotspots_outside_contig(
    residues: list[tuple[str, int, str]],
    selected: list[tuple[str, int]],
    spec: list[str],
) -> list[str]:
    """Unmatched hotspots that EXIST in the upload, just not inside the contig.

    The refusal these feed already fired before the crop landed — ``missing_
    hotspots`` has always been evaluated against the contig's selection, not
    against the whole file — so no behaviour changes here. What changes is that
    the residue is now genuinely absent from the file handed to the design
    engine rather than merely unselected by it, and "A250 is not in the selected
    region" reads identically whether the user mistyped a residue that does not
    exist or picked a real one outside the range they asked for. Those have
    different fixes (correct the hotspot / widen the contig) and the message
    could not tell them apart.
    """
    in_file = hotspot_keys([(chain, resseq) for chain, resseq, _icode in residues])
    return [token for token in missing_hotspots(selected, spec) if token in in_file]


def format_contig(segments: list[tuple[str, int, int]]) -> str:
    return ",".join(f"{chain}{lo}-{hi}" for chain, lo, hi in segments)


# The most contiguous runs a normalised contig may carry before the run is
# refused outright.
#
# UNCALIBRATED, and a POLICY choice rather than a measured limit — the same
# provenance convention ``MIN_SELECTED_RESIDUES`` and ``SizeEnvelope.cap_basis``
# use ("untested" = no run has ever approached the number, so the copy must
# claim a precaution and not a predicted failure point). Nothing upstream is
# known to break at any particular run count, and nothing here has measured one.
#
# WHAT IT IS NOT. It is not an upstream cost bound. ``AtomSelectionStack.
# from_contig`` expands EVERY range into one ``AtomSelection`` per integer, so a
# 240-residue chain already costs 240 selections whether it arrives as one run
# or as forty; splitting on gaps barely moves that number and can only lower it.
#
# WHAT IT IS. A pathological-input stop. A structure whose author numbering is
# non-contiguous by construction — a C-alpha trace numbered every tenth residue,
# a model built from sparse density — turns nearly every residue into its own
# run, and a 400-residue contig would render as a 400-token string that no
# operator can read and no reviewer can check. Refusing is the honest answer
# there; silently truncating to the first N runs would design against a target
# nobody asked for, which is the failure class this whole file exists to stop.
#
# THE NUMBER'S ONLY BASIS is a ratio to the adapter's typed-contig ceiling:
# ``tools/proteina/__init__.py::_MAX_SEGMENTS`` is 8, so 64 lets every one of
# the 8 segments a user may legally type shatter into 8 runs — 7 disordered
# loops each — and still register. It is also the same order as that file's
# ``_MAX_HOTSPOTS``. Real crystal structures carry single-digit gap counts per
# chain, so this sits far above ordinary input on purpose.
MAX_CONTIG_RUNS = 64


def contig_runs(
    residues: list[tuple[str, int, str]],
    segments: list[tuple[str, Optional[int], Optional[int]]],
) -> list[tuple[str, int, int]]:
    """Each segment as the CONTIGUOUS RUNS of residues that really exist in it.

    ``A1-240`` on a chain missing 51-59 becomes ``[("A", 1, 50), ("A", 60,
    240)]``. A segment with NO gap comes back unchanged — that is the property
    that makes this safe to apply to every contig rather than only to the ones
    suspected of being gappy.

    WHY THIS EXISTS, AND WHY IT IS A REWRITE RATHER THAN A REFUSAL. Upstream's
    ``load_target_from_pdb`` (``proteinfoundation/utils/pdb_utils.py``
    @916eaaed) resolves the contig with::

        select = AtomSelectionStack.from_contig(target_spec)
        mask = select.get_mask(struct)

    and atomworks' ``from_contig`` (``src/atomworks/io/utils/selection.py``)
    expands a range into ONE ``AtomSelection`` PER INTEGER::

        for i in range(int(start), int(stop) + 1):
            selections.append(AtomSelection(chain_id=chain_id, res_id=i))

    ``AtomSelectionStack.get_mask`` is then a bare list comprehension with no
    try/except, over a per-selection ``get_mask`` that RAISES on an empty
    match::

        if not np.any(mask):
            raise ValueError(f"No atoms found for selection: {atom_selection}")

    So every integer between the two endpoints must be a real residue, not just
    the endpoints. A disordered loop — which most crystal structures have — kills
    the run inside ``complexa design``, on a billed A100, after the checkpoints
    have loaded. Comma-separated segments are UNIONED and repeating a chain is
    legal upstream, so ``A1-50,A60-240`` succeeds exactly where ``A1-240`` dies.

    THIS IS THE ONLY GUARD ON THIS PATH THAT REWRITES INSTEAD OF REFUSING, and
    the asymmetry is deliberate. ``missing_endpoints`` refuses a bad ENDPOINT
    because an endpoint the user typed and the file does not hold is a mistake
    only the user can settle — ``A1-500`` on a 240-residue chain might mean
    ``A1-240`` or might mean the wrong file was uploaded. An interior gap is not
    a mistake at all: the operator asked for "chain A from 1 to 240", the file
    answers "these 231 residues", and the two agree about which residues are
    wanted. Nothing is added or dropped by the rewrite — see
    ``selected_residue_keys``, which selects the identical set either way — so
    there is no decision left for a human to make.

    IT MUST THEREFORE RUN LAST, after every existing refusal. Normalising first
    would silently narrow ``A1-500`` to the real last residue and swallow the
    refusal ``missing_endpoints`` exists to raise.

    DETAILS THAT ARE LOAD-BEARING:

    * ``lo is None`` (a bare chain id, "the whole chain") selects every residue
      of the chain, matching ``select_residues`` and ``selected_residue_keys``.
      Callers expand bare ids first; tolerating one here means a caller that
      forgets gets the right answer instead of a TypeError.
    * INSERTION CODES DO NOT SPLIT A RUN. Runs are computed over the DISTINCT
      ``resseq`` values, so ``A100``/``A100A`` is one number and one residue for
      this purpose — the same reading ``missing_endpoints`` takes, and the only
      one a contig can express, since a range endpoint is a bare integer with
      nowhere to put a code.
    * SEGMENT ORDER IS PRESERVED and runs within a segment ascend. Overlapping
      segments are NOT merged across the segment boundary: upstream ORs the
      masks, so a residue named twice is selected once either way, and merging
      would make this function's output stop corresponding one-to-one with the
      input the guards judged.
    * A segment that selects nothing contributes NO run. Unreachable from
      ``prepare_custom_target`` — ``empty_segments`` refuses that segment, and a
      wholly empty ``segments`` list is refused by ``target_too_small`` at a
      count of 0 — so the caller never has to handle a shrunken list.
    """
    out: list[tuple[str, int, int]] = []
    for chain, lo, hi in segments:
        nums = sorted({
            resseq for c, resseq, _icode in residues
            if c == chain and (lo is None or lo <= resseq <= hi)
        })
        if not nums:
            continue
        start = prev = nums[0]
        for num in nums[1:]:
            if num != prev + 1:
                out.append((chain, start, prev))
                start = num
            prev = num
        out.append((chain, start, prev))
    return out


def unrenderable_segments(
    segments: list[tuple[str, int, int]]
) -> list[tuple[str, int, int]]:
    """Segments whose contig text upstream's parser cannot read back.

    Verified against atomworks ``AtomSelectionStack.from_contig``
    (``src/atomworks/io/utils/selection.py``)::

        CONTIG_REGEX = re.compile(r"([A-Za-z]+)(\\d+)-(\\d+)")
        match = CONTIG_REGEX.match(selection)
        if not match:
            raise ValueError(f"Invalid contig string: {selection}")

    ``(\\d+)`` carries no sign, so a negative author residue number — routine
    on constructs that keep an expression tag, e.g. CA residues -5..240 —
    renders as ``A-5-240`` and raises. Nothing before the GPU catches it:
    the selection is non-empty, the registry write succeeds and the read-back
    matches, so the shard boots, loads checkpoints and only then dies inside
    ``complexa design``. Refusing here converts a full-price crash into a free
    message. ``0`` is fine (``A0-240`` matches), so the bound is ``< 0``.

    A segment still carrying ``lo is None`` is NOT unrenderable, it is
    unresolved: ``expand_bare_chains`` leaves a bare chain id that is absent
    from the upload exactly as it found it, and "chain Z is not in this file"
    is a different refusal with a different fix. Skipping it here is what lets
    both callers hand this function a parsed contig unfiltered — the canary
    used to strip unexpanded segments itself, which silently stripped the
    negative-numbering guard along with them (``--contig A`` on a tagged
    construct). Unreachable inside ``prepare_custom_target``, which refuses an
    absent chain before it gets here; live in the canary, which does not.
    """
    return [(c, lo, hi) for c, lo, hi in segments
            if lo is not None and (lo < 0 or hi < 0)]


def empty_segments(
    residues: list[tuple[str, int, str]],
    segments: list[tuple[str, Optional[int], Optional[int]]],
) -> list[tuple[str, Optional[int], Optional[int]]]:
    """Segments of the contig that select no residue of the upload at all.

    PER SEGMENT, WHICH IS THE WHOLE POINT: the aggregate selection can be
    healthy while one segment is dead, and a dead segment is a request upstream
    cannot honour. ``prepare_custom_target`` has always refused this; the canary
    checked only that the TOTAL selection was non-empty, so ``--contig
    A1-300,Z1-50`` on a file of chains A and B selected 300 residues, cleared
    every aggregate check, and spawned one A100 in phase 1 (~$4) or three in
    phase 2 (~$12) for a request production refuses for free. PR #109 made
    multi-segment contigs the ordinary input shape, which is what turned a
    latent hole into a reachable one.

    Also catches a bare chain id ``expand_bare_chains`` could not resolve — the
    chain is not in the file, so it selects nothing — and that case is why the
    message this feeds must not say "widen the range". Widening cannot conjure
    a chain the upload does not contain.

    ``select_residues`` is what decides, one segment at a time, so this asks the
    same question of each segment that the aggregate selection answers for all
    of them together.
    """
    return [seg for seg in segments if not select_residues(residues, [seg])]


# The smallest DISTINCT selection worth starting a GPU for.
#
# UNCALIBRATED — the same provenance convention ``SizeEnvelope.cap_basis`` uses
# for the other end of this range ("untested" = no run has ever approached the
# number here, so the copy must claim it as a precaution and not as a predicted
# failure point). Nothing has measured this one either. It entered the codebase
# as a bare uncommented ``20`` inside ``prepare_custom_target``; no A100 run has
# ever been made at, above or below it; and the only property any test asserts
# of the number itself is ``>= 10``. The stated rationale — that there is not
# enough surface in fewer than 20 residues to place a 60-120 residue binder — is
# plausible and is NOT evidence. Treat it as the floor production happens to
# enforce, which is exactly what makes it worth mirroring: the canary's job is
# to agree with production, not to be right about biophysics.
#
# NOT ``shared/pdb_preflight.py::MIN_TARGET_RESIDUES``, which is 30 and is a
# different quantity: that one is ``min(r.min_target_aa ...)`` over the binder
# tools and bounds the WHOLE named chain, before any contig is applied, on the
# ``/tools/<slug>/submit`` route. This one bounds the contig's SELECTION. A
# 400-residue chain cropped to 15 residues clears theirs and fails this. The
# campaign route (``shared/targets.py::size_error``) runs size-only and
# deliberately does not apply a minimum at all, so on that route this floor,
# inside the container, is the only one that ever fires.
MIN_SELECTED_RESIDUES = 20


def n_selected_residues(
    residues: list[tuple[str, int, str]],
    segments: list[tuple[str, Optional[int], Optional[int]]],
) -> int:
    """How many DISTINCT residues the contig selects — what the crop stages."""
    return len(selected_residue_keys(residues, segments))


def target_too_small(
    residues: list[tuple[str, int, str]],
    segments: list[tuple[str, Optional[int], Optional[int]]],
) -> bool:
    """True when the contig selects too little target to design a binder against.

    THE NUMBER AND THE COMPARISON BOTH LIVE HERE BECAUSE TWO CALLERS READ THEM.
    This was a bare ``if len(selected) < 20`` inside ``prepare_custom_target``,
    which is the shape that has now cost three separate rounds on this branch:
    production grows a pre-GPU refusal, ``_hotspot_canary`` has no equivalent,
    and the harness whose entire job is fidelity to production spends real money
    to discover what a comparison knows for free. ``--contig A10-20`` would
    spawn one A100 in phase 1 (~$4) or three in phase 2 (~$12).

    The two already closed the same way — ``stage_cropped_target`` for the crop
    and ``unrenderable_segments`` for negative residue numbers — and the rule
    both established is the one applied here: the canary CALLS this, it does not
    restate it. A restated threshold is a threshold that drifts on the next
    commit that moves this one, silently, in the direction of spending money.

    IT TAKES ``(residues, segments)`` RATHER THAN A SELECTION, AND THAT
    SIGNATURE IS THE FIX. It first took whatever list a caller handed it and
    measured ``len``, and both callers handed it ``select_residues``, which
    appends per segment and never de-duplicates. On a 60-residue chain A:
    ``A10-20`` counted 11 and was refused; ``A10-20,A10-20`` counted 22 for the
    same 11 residues and was not; ``A1-7,A1-7,A1-7`` counted 21 for 7. One comma
    defeated the floor, and the contig it defeated it with is the exact one this
    round was opened to stop. Counting is now this function's job, on the same
    de-duplicated key set the crop stages, and there is no longer a collection a
    caller could pass that would give the wrong answer.
    """
    return n_selected_residues(residues, segments) < MIN_SELECTED_RESIDUES


def missing_endpoints(
    residues: list[tuple[str, int, str]],
    segments: list[tuple[str, Optional[int], Optional[int]]],
) -> list[tuple[str, int]]:
    """Contig endpoints that name no residue of their chain, as (chain, number).

    THE SIBLING OF ``unrenderable_segments``, for the same family of bug: our
    selection logic accepts something upstream's selector rejects, and the
    disagreement is only discovered on a paid GPU. That one is about the contig
    TEXT; this one is about what the text RESOLVES to.

    WHAT IT COST TO LEARN THIS. The Fc target 3S7G has chain A spanning 236-443
    and chain B spanning 236-**442**. A campaign was launched with
    ``A236-443,B236-443`` and upstream died::

        ValueError('No atoms found for selection: B/*/443')

    ~60 s of billed A100, zero designs. Every guard in ``prepare_custom_target``
    passed it, and passed it for a good reason rather than an oversight:
    ``select_residues`` filters with ``lo <= resseq <= hi``, so on chain B the
    segment picked out the 207 residues that really are there. The COUNT was
    correct. Step 4 ("every segment must select something") therefore saw 207,
    not 0; the 20-residue floor passed; ``unrenderable_segments`` passed (no
    negative numbers); the hotspots passed; and ``stage_cropped_target``'s
    self-check passed too, at (415, 415) — it compares the staged file against
    the same selection, and both sides simply ignore the residue that is not
    there. Nothing anywhere asked whether ``lo`` and ``hi`` are THEMSELVES
    residues on that chain. Upstream's ``AtomSelectionStack.from_contig`` does,
    and it does it after the checkpoints are loaded.

    BOTH ENDPOINTS ARE CHECKED. The failure was on ``hi``; ``lo`` has the
    identical exposure and is the one that closes the residue-0 hole (see
    below). An endpoint inside a DISORDERED GAP is caught by the same test —
    ``A320-443`` on a chain missing 301-349 names a residue the file does not
    contain just as surely as one past the end.

    IT ALSO CLOSES THE RESIDUE-0 HOLE, for free rather than by special case.
    The adapter's ``_parse_target_input`` refuses ``lo < 0``, and 0 is not < 0,
    so ``A0-100`` has always been accepted. On a chain numbered from 1 that
    selects residues 1-100 — a non-empty, above-floor selection that every
    other guard waves through — while upstream is recorded as resolving residue
    0 by selecting the whole chain, silently designing against a different
    target than the operator asked for. Residue 0 does not exist, so it is
    already a missing endpoint here; there is no rule about zero in this
    function and there should not be one.

    ONLY BOUNDED SEGMENTS ARE CHECKED. A bare chain id parses to
    ``(chain, None, None)`` and has no endpoints to check, so it is skipped —
    handled here rather than left to the caller, because a caller that forgets
    would get a TypeError instead of the right answer. The derived path
    (``derive_segments``) is safe by construction: it builds spans from
    ``min(nums)``/``max(nums)`` of residues it just read out of the file, so
    both endpoints exist by definition and this returns ``[]``.

    INSERTION CODES: AN ENDPOINT MATCHING ANY INSERTION CODE COUNTS AS EXISTING.
    ``pdb_ca_residues`` returns ``(chain, resseq, icode)``, and ``A100`` and
    ``A100A`` are two residues with two CA atoms — but a contig endpoint is a
    bare number with nowhere to put a code, so a choice is forced. Existence is
    tested on ``resseq`` ALONE, for three reasons, in order of weight:

    * It is what the rest of this file already does. ``select_residues`` and
      ``selected_residue_keys`` both filter on ``lo <= resseq <= hi`` with the
      code ignored, so on a chain whose residue 200 exists only as ``B200A``
      the contig ``B200-201`` genuinely selects it and the crop genuinely keeps
      it. Refusing an endpoint the selection then honours would make this
      function disagree with the code it is guarding.
    * It matches how the structure libraries model a residue. biotite keeps the
      insertion code in a field of its own, separate from the number — the fact
      ``ambiguous_insertion_codes`` already records — so a numeric endpoint is a
      question about the number field alone.

      THIS BULLET IS THE WEAK ONE, AND IT IS THE EXPENSIVE DIRECTION. It is not
      verified against ``AtomSelectionStack.from_contig``: atomworks is not
      vendored here and its contig grammar is unread. Do not read the failure
      message ``No atoms found for selection: B/*/443`` as agreement — that
      wildcard sits in the MIDDLE field, and a three-field selection puts the
      residue number, and any code with it, in the third. If upstream does
      discriminate on the code, this rule is a false negative and the run dies
      on a billed A100, whereas the strict rule would only have cost a free
      refusal. Revisit here first if a paid shard ever dies on an
      insertion-coded endpoint.
    * Antibody-numbered targets carry insertion codes as a matter of routine, so
      treating ``A100A`` as failing to satisfy the endpoint ``100`` would refuse
      runs THIS FILE'S OWN SELECTION accepts (see the first bullet — that part is
      executed, not inferred). ``ambiguous_insertion_codes`` settles the same
      trade-off the same way: warned about, never fatal.

    Returned in segment order, ``lo`` before ``hi``, so a segment with two bad
    endpoints contributes two entries and the message can name both. Deduped on
    ``(chain, endpoint)``, so ``B443-443`` — both ends the same absent residue —
    names it once rather than twice.
    """
    out: list[tuple[str, int]] = []
    for chain, lo, hi in segments:
        if lo is None or hi is None:
            continue
        present = {resseq for c, resseq, _icode in residues if c == chain}
        for endpoint in (lo, hi):
            if endpoint not in present and (chain, endpoint) not in out:
                out.append((chain, endpoint))
    return out


def chain_span_summary(residues: list[tuple[str, int, str]]) -> str:
    """`A1-115, B3-97` — for failure messages, so a user whose hotspot missed
    can see what the file actually contains without re-uploading it."""
    spans: list[str] = []
    for chain in sorted({r[0] for r in residues}):
        nums = [r[1] for r in residues if r[0] == chain]
        spans.append(f"{chain}{min(nums)}-{max(nums)}")
    return ", ".join(spans)


def ambiguous_insertion_codes(residues: list[tuple[str, int, str]]) -> list[str]:
    """Keys where an insertion code makes `chain+resnum` non-unique.

    Upstream's match key carries no insertion code (biotite keeps ``ins_code``
    in a separate field), so ``A100`` and ``A100A`` collapse to the same token
    and a hotspot on one also lands on the other. Warned about, never fatal:
    the constraint still lands in the right neighbourhood, and refusing would
    block legitimate antibody-numbered targets outright.
    """
    counts: dict[str, int] = {}
    for chain, resseq, _icode in residues:
        key = f"{chain}{resseq}"
        counts[key] = counts.get(key, 0) + 1
    return sorted(k for k, n in counts.items() if n > 1)


# ===========================================================================
# Seed derivation (cross-shard independence)
# ===========================================================================


def shard_seed(job_id: str) -> int:
    """Derive a stable, distinct, bounded seed from the job id. Distinct child
    job ids -> distinct seeds -> independent shards (avoids the seed+job_id
    collision caveat by keeping job_id=0 and varying only the seed)."""
    if not job_id:
        return 42
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % 1_000_000


# ===========================================================================
# CLI construction
# ===========================================================================


def _rf3_enabled() -> bool:
    return os.environ.get("PROTEINA_RF3", "on").strip().lower() not in _RF3_OFF


def _inline_enabled() -> bool:
    """Whether design coordinates may travel in the return value.

    Defaults ON: a caller with no upload endpoint has no other way to receive
    atoms, and that caller is the whole reason this exists.

    Observable ONLY when there is no upload endpoint. Inlining is exclusive
    with uploading, so with an endpoint this flag changes nothing — there is no
    "scores inline but not the atoms" mode, because with an endpoint the atoms
    were never inline to begin with. Turning it off makes an endpoint-less call
    a pre-GPU refusal instead of an inline delivery.
    """
    return os.environ.get("PROTEINA_INLINE_PDBS", "on").strip().lower() not in _INLINE_OFF


# ===========================================================================
# Custom-target registration (`complexa target add`)
#
# VERIFIED against Proteina-Complexa @ dev 916eaaed:
#   * pyproject [project.scripts] exposes `complexa-target`, and `target` is
#     also a nested subcommand of `complexa` with add/list/show.
#   * `add` writes configs/targets/targets_dict.yaml. binder_generate.yaml
#     composes it (`defaults: - /targets/targets_dict@_here_`) into
#     target_dict_cfg, and ++generation.task_name indexes that dict — which is
#     what makes a registered key selectable exactly like a curated one.
#   * a record is {source, target_filename, target_path, target_input,
#     hotspot_residues, binder_length, pdb_id}; hotspot_residues is a list of
#     chain-prefixed strings and binder_length a [lo, hi] int pair.
#   * `target` is NOT in _INIT_EXEMPT_COMMANDS, so it needs COMPLEXA_INIT —
#     the Dockerfile sets COMPLEXA_INIT=docker.
# ===========================================================================

# Marks records this wrapper wrote. Load-bearing: it is how a key collision
# with a curated benchmark target is detected at runtime rather than by hand-
# auditing the shipped YAML on every upstream bump.
_HUB_SOURCE = "tools_hub_upload"
_TARGETS_DICT = f"{PROTEINA_HOME}/configs/targets/targets_dict.yaml"
# Staged outside ./inference: that tree is wiped at shard start and archived at
# shard end, and the registry holds an absolute path to this file for the whole
# `complexa design` run.
_HUB_TARGET_DIR = f"{PROTEINA_HOME}/hub_targets"


def custom_target_key(job_id: str, pdb_sha256: str, record: dict) -> str:
    """Deterministic registry key for an uploaded target.

    Distinct job ids give distinct keys, so two shards sharing a warm container
    never overwrite each other's record; an identical re-registration gives an
    identical key, so a retry is idempotent under --force. The ``hub_`` prefix
    plus 16 hex chars satisfies the adapter's _TASK_RE, and a collision with a
    curated key is checked against the YAML at registration time rather than
    assumed away.
    """
    blob = json.dumps({"job": job_id, "sha": pdb_sha256, **record}, sort_keys=True, default=str)
    return "hub_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_target_add_cmd(
    *,
    key: str,
    pdb_path: str,
    filename_stem: str,
    contig: str,
    hotspot_spec: list[str],
    binder_length: list[int],
    dict_path: str = _TARGETS_DICT,
) -> list[str]:
    """Assemble the `complexa target add` invocation.

    Three details are load-bearing and each has a test:

    ``--dict`` is passed EXPLICITLY. Upstream's get_default_dict_path() walks up
    from the cwd and silently falls back to a legacy configs/generation/ path;
    naming the file removes a whole class of wrote-the-wrong-registry failure.

    ``--hotspot-residues`` and ``--binder-length`` are argparse ``nargs="+"``,
    so their values must be SEPARATE argv elements. Joining them into one
    string is the single most likely silent bug in this path: argparse would
    take "A45 A67" as one token, it would match no residue, and upstream would
    drop it to an all-zero mask without complaint.

    ``--force`` is mandatory. Without it an existing key prompts
    ``input("Overwrite? (y/N): ")``, which EOFErrors on a container's closed
    stdin and returns False — a registration that did not happen, reported as
    if the user had declined it.
    """
    cmd = [
        COMPLEXA_BIN, "target", "add", key,
        "--dict", str(dict_path),
        "--source", _HUB_SOURCE,
        "--target-filename", filename_stem,
        "--target-path", str(pdb_path),
        # NEVER omitted: upstream defaults target_input to "A1-100", which would
        # silently crop a larger target to its first 100 residues.
        "--target-input", contig,
        "--binder-length", str(binder_length[0]), str(binder_length[1]),
        "--force",
    ]
    if hotspot_spec:
        cmd.append("--hotspot-residues")
        cmd.extend(hotspot_spec)
    return cmd


def read_targets_dict(path: str) -> dict:
    """Load the targets registry, returning the TARGET RECORDS.

    The file nests every record one level down, under a top-level
    ``target_dict_cfg:`` key (verified against the pinned commit: the file opens
    with ``target_dict_cfg:`` and each target sits at 2-space indent beneath it).
    Upstream's own ``target_manager`` compensates with ``data.get(
    "target_dict_cfg", data)`` before indexing by target name, and so must we —
    reading the outer mapping makes every ``registry[key]`` lookup miss, which
    turns a SUCCESSFUL registration into "target was not written to the
    registry" and fails every custom-target shard.

    Falls back to the raw mapping when the wrapper is absent, matching upstream
    and keeping the legacy ``configs/generation/`` layout readable.

    PyYAML rides in with OmegaConf in the image but is not a tools-hub
    dependency, so the import is lazy and local — this module must stay
    importable in the offline test suite.
    """
    import yaml  # noqa: PLC0415

    with open(path, "r", errors="replace") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return {}
    inner = data.get("target_dict_cfg")
    return inner if isinstance(inner, dict) else data


def registration_mismatch(record: Any, expected: dict) -> Optional[str]:
    """Compare a written record against what we asked for; None means it took.

    Pure (no YAML, no filesystem) so it unit-tests offline. This is the check
    that makes the CLI's exit code irrelevant: add_target_cli can return False
    without failing the process, so the artifact is the only trustworthy
    evidence that the registration actually landed.
    """
    if not isinstance(record, dict):
        return "target was not written to the registry"
    for field in ("source", "target_path", "target_input"):
        want = expected[field]
        got = record.get(field)
        if str(got) != str(want):
            return f"{field} is {got!r} in the registry, expected {want!r}"
    got_hot = [str(h) for h in (record.get("hotspot_residues") or [])]
    want_hot = [str(h) for h in expected["hotspot_residues"]]
    if got_hot != want_hot:
        return f"hotspot_residues are {got_hot} in the registry, expected {want_hot}"
    got_len = [int(v) for v in (record.get("binder_length") or [])]
    want_len = [int(v) for v in expected["binder_length"]]
    if got_len != want_len:
        return f"binder_length is {got_len} in the registry, expected {want_len}"
    return None


def build_design_cmd(
    *,
    config_name: str,
    task_name: str,
    seed: int,
    nsamples: int | None,
    replicas: int | None,
    nsteps: int | None,
    run_name: str,
    rf3_on: bool,
) -> list[str]:
    """Assemble the `complexa design` invocation for one shard.

    Runs the full generate -> filter -> evaluate -> analyze pipeline (evaluate
    is what writes the reward CSV we parse). ++job_id=0 with a distinct ++seed;
    filter keeps every sample so all designs survive; a fresh run_name + cwd
    isolate the results-CSV early-exit.

    nsamples / replicas are pinned as explicit Hydra overrides. The keys are
    VERIFIED against configs/pipeline/binder/binder_generate.yaml @ 916eaaed:
    nsamples lives at generation.dataloader.dataset.nres.nsamples (default 4) and
    replicas at generation.search.best_of_n.replicas (default 2, algorithm
    best-of-n). Pinning them makes every variant yield exactly nsamples*replicas
    designs == the campaign chunk_size, regardless of the per-variant default.

    gen_njobs is pinned to 1 (one GPU per shard). That FORCES job_id=0: with
    njobs=1, split_by_job() gives any job_id>=1 zero samples, so cross-shard
    independence comes from a distinct ++seed (cfg.seed = cfg.seed + job_id, so
    seed+0 == our derived seed), never from job_id.

    The config path is passed RELATIVE (configs/<name>.yaml) because the caller
    runs from cwd=/opt/proteina — the same invocation upstream documents in each
    config header — so ./ckpts (weights), ./assets (target PDBs) and Hydra's
    config search path all resolve from the repo root.
    """
    config_path = f"configs/{config_name}.yaml"
    cmd = [
        COMPLEXA_BIN, "design", config_path,
        "++job_id=0",
        f"++base_config_name={config_name}",
        f"++seed={seed}",
        "++gen_njobs=1",
        f"++generation.task_name={task_name}",
        f"++run_name={run_name}",
        "++generation.filter.delete_non_top_n_samples=false",
        "++generation.filter.filter_samples_limit=1000",
    ]
    if nsamples:
        cmd.append(f"++generation.dataloader.dataset.nres.nsamples={nsamples}")
    if replicas:
        cmd.append(f"++generation.search.best_of_n.replicas={replicas}")
    if nsteps:
        cmd.append(f"++generation.args.nsteps={nsteps}")
    # No RF3 toggle is emitted: RF3 is enabled/disabled by whether `rf3folding`
    # is present in the config's reward_models block, not by a flag. The upstream
    # protein_binder config is AF2-only (no RF3 block), while the RF3-only
    # variants (ligand_binder / motif_ame) are hard-blocked pre-GPU in main()
    # when PROTEINA_RF3=off. So there is nothing to override here; rf3_on is kept
    # in the signature for the call-site contract + the main() hard-block.
    _ = rf3_on
    return cmd


def prepare_custom_target(
    *,
    input_url: str,
    job_id: str,
    target_chain: str,
    target_input: str,
    hotspot_spec: list[str],
    binder_length: list[int],
    run_dir: Path,
) -> str:
    """Stage, verify and register a bring-your-own target. Returns its key.

    Every failure path here is a ``_fail`` BEFORE the model is loaded, which is
    the point: the checks that matter (does this hotspot exist, does this chain
    range select anything) are exactly the ones upstream performs silently and
    wrongly, so they have to be settled while the answer is still free.
    """
    target_dir = Path(_HUB_TARGET_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. stage ---------------------------------------------------------
    incoming = target_dir / "incoming.pdb"
    download_target(input_url, incoming)
    pdb_sha = hashlib.sha256(incoming.read_bytes()).hexdigest()

    # --- 2. parse ---------------------------------------------------------
    residues, n_unparsable = pdb_ca_residues(incoming)
    if not residues:
        _fail(
            "input", "pdb_parse",
            "no protein residues (CA atoms) could be read from the uploaded "
            f"target{f'; {n_unparsable} residue lines were unparsable' if n_unparsable else ''}.",
        )
    spans = chain_span_summary(residues)

    # --- 3. resolve the contig -------------------------------------------
    requested_chains = target_chain.split()
    if target_input:
        try:
            raw_segments = parse_target_input(target_input)
        except ValueError as exc:
            _fail("input", "target_input", str(exc))
        # ``expand_bare_chains``, not an inline loop: the canary calls the same
        # expansion, and until it did, ``--contig A`` reached it unexpanded and
        # was filtered out of the negative-numbering guard. A chain the upload
        # does not contain comes back still carrying its ``None``s, which is
        # what this refusal names.
        segments = expand_bare_chains(residues, raw_segments)
        for chain, lo, _hi in segments:
            if lo is None:
                _fail(
                    "input", "target_input",
                    f"chain {chain} is not present in the uploaded target. "
                    f"It contains: {spans}.",
                )
    else:
        # PER CHAIN, not in aggregate. ``derive_segments`` ``continue``s past a
        # chain it finds no residues for, so a request for two chains against a
        # structure that holds one produces a perfectly healthy one-chain
        # ``segments`` and nothing downstream ever mentions the chain that
        # vanished: every later guard (3b, 4, 4b, target_too_small,
        # missing_hotspots) reads the ALREADY-PRUNED list, and hotspots on a
        # strict subset of the contig's chains are legitimate, so a request
        # aimed at one protomer clears step 5 with "all N hotspot(s) matched"
        # while the other protomer is absent from the staged file entirely. An
        # A100 is billed and the binders are designed against a monomer, in a
        # run indistinguishable from a correct one.
        #
        # An aggregate check cannot see this — it only fires when ALL the
        # chains are missing — and the ``target_input`` branch immediately
        # above has always refused per chain, so this closes an asymmetry
        # rather than adding a new class of refusal. The comma spelling
        # ``"A,B"`` is why it matters now: ``normalize_target_chain`` turns it
        # into two real chain tokens (it used to be one unmatchable token that
        # emptied ``segments`` and tripped the aggregate guard), so the exact
        # literal ``direct_call_fc.build_job_spec`` sends now takes this path.
        present = {r[0] for r in residues}
        absent = [c for c in requested_chains if c not in present]
        if absent:
            noun, verb = ("chains", "are") if len(absent) > 1 else ("chain", "is")
            _fail(
                "input", "target_chain",
                f"{noun} {' '.join(absent)} {verb} not present in the uploaded "
                f"target. It contains: {spans}.",
            )
        segments = derive_segments(residues, requested_chains)
        if not segments:
            # Reachable only when NO chain was requested at all (an empty
            # target_chain with no contig): ``absent`` is empty because there
            # was nothing to look for, and nothing was selected either.
            _fail(
                "input", "target_chain",
                f"chain {target_chain!r} is not present in the uploaded target. "
                f"It contains: {spans}.",
            )

    # --- 3b. the contig must survive a round-trip through upstream --------
    # See unrenderable_segments(): a negative author residue number renders as
    # "A-5-240", which atomworks' CONTIG_REGEX cannot match. Every other guard
    # in this function passes on such a target, so without this the refusal
    # happens on a billed A100 instead of here.
    bad = unrenderable_segments(segments)
    if bad:
        hints = []
        for chain, lo, hi in bad:
            nonneg = [r[1] for r in residues if r[0] == chain and r[1] >= 0]
            hints.append(
                f"{chain}{min(nonneg)}-{max(nonneg)}" if nonneg else
                f"(chain {chain} has no residue numbered 0 or above)"
            )
        _fail(
            "input", "target_input_negative",
            "the target chain range "
            f"{format_contig(bad)} uses negative residue numbers, which the "
            "design engine's contig format cannot express — it accepts digits "
            "only. Structures carrying an expression tag are usually numbered "
            "this way. Set an explicit target chain range that starts at 0 or "
            f"above, e.g. {','.join(hints)}. The target contains: {spans}.",
        )

    # --- 4. every segment must select something ---------------------------
    # Upstream hands the contig to atomworks' AtomSelectionStack.from_contig,
    # whose behaviour on an unresolvable chain is unverified. We never depend
    # on it: the selection is computed here and an empty one is a refusal.
    # ``empty_segments``, not an inline loop, for the same reason as every other
    # predicate here — the canary calls this one too, and checked only the
    # AGGREGATE until it did, so one dead segment hid behind a healthy one.
    for chain, lo, hi in empty_segments(residues, segments):
        _fail(
            "input", "target_input",
            f"chain {chain} residues {lo}-{hi} select 0 residues in the "
            f"uploaded target. It contains: {spans}.",
        )

    # --- 4b. and both of its endpoints must be real residues --------------
    # See missing_endpoints(). Step 4 asks whether a segment selects ANYTHING,
    # which is a different question: on 3S7G, B236-443 selects the 207 residues
    # of chain B that do exist and says nothing about the 443 that does not.
    # Upstream resolves each endpoint and dies -- "No atoms found for
    # selection: B/*/443" -- ~60 s into a billed A100. This is that question,
    # asked while the answer is still free.
    absent = missing_endpoints(residues, segments)
    if absent:
        bad_chains = {chain for chain, _endpoint in absent}
        fixes = []
        for chain, lo, hi in segments:
            if chain not in bad_chains:
                continue
            nums = sorted({r[1] for r in residues if r[0] == chain})
            if not nums:
                # Unreachable via step 4 (a chain with no residues selects
                # nothing and is refused above), but the hint must not index
                # an empty list if that ever stops being true.
                continue
            # The nearest residue that EXISTS, moving inwards: the smallest at
            # or above ``lo`` and the largest at or below ``hi``. Handles a
            # disordered gap as well as an over-run bound.
            at_or_above = [n for n in nums if n >= lo] or [nums[-1]]
            at_or_below = [n for n in nums if n <= hi] or [nums[0]]
            # THEN THROUGH ``contig_runs``, WHICH IS THE FIX. Two endpoints that
            # exist still bracket a span that can straddle a disordered gap, and
            # upstream resolves every integer between them — so ``f"{chain}{at_
            # or_above[0]}-{at_or_below[-1]}"`` on its own told the user to
            # retype a range that dies in exactly the way they had just been
            # refused for. Splitting the hint at each gap makes the advice
            # something they can paste. ``[]`` when the two bounds cross (no
            # residue at or above ``lo``, none at or below ``hi``): the sentence
            # drops its "e.g." clause rather than printing a backwards range.
            fixed = contig_runs(residues, [(chain, at_or_above[0], at_or_below[-1])])
            if fixed:
                fixes.append(format_contig(fixed))
        named = ", ".join(f"residue {endpoint} on chain {chain}"
                          for chain, endpoint in absent)
        advice = f", e.g. {','.join(fixes)}" if fixes else ""
        _fail(
            "input", "target_input_endpoint",
            f"the target chain range names {named}, which the uploaded target "
            "does not contain. The design engine resolves each end of the "
            "range against the structure and would have failed with "
            f'"No atoms found for selection: {absent[0][0]}/*/{absent[0][1]}" '
            "after the GPU work was already paid for. Set an explicit target "
            f"chain range whose ends are real residues{advice}. "
            f"The chains present run {spans} — a run is first-to-last and can "
            "have gaps inside it, which is why the range suggested above is "
            "built from residues that really exist rather than from those ends.",
        )

    selected = select_residues(residues, segments)
    n_distinct = n_selected_residues(residues, segments)
    logger.info(
        "custom target: selected %d of %d residues (%s); chains present: %s",
        n_distinct, len(residues), format_contig(segments), spans,
    )
    # ``target_too_small``, not an inline comparison: the canary calls the same
    # predicate, and a second copy of the number is a second thing to move. It
    # is handed ``(residues, segments)`` rather than ``selected`` because
    # ``select_residues`` repeats a residue two segments both name — ``A10-20,
    # A10-20`` used to count 22 of the same 11 and clear a floor of 20 — and the
    # count the message quotes is the one the gate used and the crop stages.
    if target_too_small(residues, segments):
        _fail(
            "input", "target_input",
            f"the selected target region has only {n_distinct} residues, "
            f"fewer than the {MIN_SELECTED_RESIDUES} needed to design a binder "
            f"against it. Widen the chain range. The target contains: {spans}.",
        )

    ambiguous = ambiguous_insertion_codes(residues)
    if ambiguous:
        logger.warning(
            "custom target: %d residue id(s) are ambiguous because of insertion "
            "codes (%s). Upstream matches hotspots on chain+number only, so a "
            "hotspot on one of these also constrains its insertion-coded twin.",
            len(ambiguous), ", ".join(ambiguous[:10]),
        )

    # --- 5. THE guard: every hotspot must exist ---------------------------
    missing = missing_hotspots(selected, hotspot_spec)
    if missing:
        outside = hotspots_outside_contig(residues, selected, hotspot_spec)
        _fail(
            "input", "hotspot_missing",
            f"hotspot residue(s) {', '.join(missing)} are not in the selected "
            f"region of the uploaded target ({format_contig(segments)}). The "
            f"target contains: {spans}. Hotspots are chain-prefixed and "
            "case-sensitive, in original PDB numbering (e.g. A45)."
            + (
                f" {', '.join(outside)} do exist in the upload but fall outside "
                "that range, and the target is cropped to the range before the "
                "design engine sees it — widen the chain range to include them, "
                "or move the hotspot inside it."
                if outside else ""
            )
            + (f" {n_unparsable} residue lines were unparsable." if n_unparsable else ""),
        )
    if hotspot_spec:
        logger.info(
            "custom target: all %d hotspot(s) matched: %s",
            len(hotspot_spec), " ".join(hotspot_spec),
        )

    # --- 6. name it, then refuse to shadow a curated target ---------------
    # THE CONTIG IS NORMALISED HERE AND NOWHERE EARLIER. See ``contig_runs``:
    # upstream resolves EVERY integer between a range's endpoints and raises on
    # the first one the file does not hold, so ``A1-240`` on a chain with a
    # disordered loop dies inside `complexa design` where ``A1-50,A60-240``
    # succeeds. Every guard above ran against the segments the operator asked
    # for, unrewritten, which is the whole reason this sits below them rather
    # than above: normalising first would quietly narrow ``A1-500`` to the real
    # last residue and swallow the ``missing_endpoints`` refusal at step 4b.
    #
    # This is the LAST place the contig exists as segments — from here it is a
    # string, written into the record, compared on read-back and passed as
    # ``--target-input``. All three must be the same string, which is why one
    # variable feeds all three.
    runs = contig_runs(residues, segments)
    requested = format_contig(segments)
    if len(runs) > MAX_CONTIG_RUNS:
        # NOT truncated to the first MAX_CONTIG_RUNS. A shortened contig is a
        # different target, and designing against one the operator did not ask
        # for is the exact failure class every other guard here exists to stop.
        _fail(
            "input", "target_input_runs",
            f"the target chain range {requested} covers {len(runs)} separate "
            "runs of residues once the gaps in the uploaded structure are "
            f"taken out, more than the {MAX_CONTIG_RUNS} this service will "
            "register. The design engine resolves every residue number in a "
            "range, so a range spanning a gap has to be split at each one, and "
            "a structure that needs this many splits is usually numbered "
            "non-contiguously by design (a C-alpha trace, or a model built from "
            "sparse density). Narrow the target chain range to a well-ordered "
            f"region. The target contains: {spans}.",
        )
    contig = format_contig(runs)
    if contig != requested:
        logger.info(
            "custom target: the chain range %s spans gaps in the uploaded "
            "structure and was shipped to the design engine as %s (%d "
            "contiguous run(s)); the same residues are selected either way, but "
            "the engine resolves every number in a range and raises on the "
            "first one the file does not contain",
            requested, contig, len(runs),
        )
    record = {
        "source": _HUB_SOURCE,
        "target_input": contig,
        "hotspot_residues": list(hotspot_spec),
        "binder_length": [int(binder_length[0]), int(binder_length[1])],
    }
    key = custom_target_key(job_id, pdb_sha, record)
    staged = target_dir / f"{key}.pdb"

    # --- 6b. CROP THE STAGED FILE TO THE CONTIG ---------------------------
    # ``pdb_sha`` is deliberately still the SHA of what the user uploaded — it
    # is the identity of their input, and the registry key derives from it.
    #
    # The staging itself is ``stage_cropped_target``, which is ALSO what the
    # canary calls. See its docstring: writing those four lines here instead is
    # exactly what let the canary stage uncropped bytes and reproduce, on a paid
    # A100, the failure this crop exists to prevent.
    try:
        n_staged, n_selected = stage_cropped_target(
            staged, incoming.read_text(errors="replace"), residues, segments)
    except TargetCropError as exc:
        _fail("input", "target_crop", str(exc))
    except OSError as exc:
        _fail("input", "target_crop", f"could not write the cropped target: {exc}")
    incoming.unlink(missing_ok=True)
    record["target_path"] = str(staged)
    if n_staged != len(residues):
        logger.info(
            "custom target: cropped %d of %d residues to %s (%d selected); "
            "%d chain(s) in the upload were dropped entirely",
            n_staged, len(residues), contig, n_selected,
            len({r[0] for r in residues})
            - len({chain for chain, _lo, _hi in segments}),
        )

    try:
        existing = read_targets_dict(_TARGETS_DICT)
    except Exception as exc:
        _fail("input", "target_registry", f"could not read the targets registry: {exc}")
    prior = existing.get(key)
    if isinstance(prior, dict) and str(prior.get("source")) != _HUB_SOURCE:
        _fail(
            "input", "target_key_collision",
            f"registry key {key} already exists and was not written by this "
            "service. Refusing rather than overwriting a benchmark target.",
        )

    # Keep the exact bytes that were designed against with the run's archive.
    # The basename target.pdb is on find_pdb_for's exclusion list, so it can
    # never be mistaken for a design.
    try:
        hub_input = run_dir / "_hub_input"
        hub_input.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, hub_input / "target.pdb")
    except OSError as exc:
        logger.warning("could not copy the staged target into the run dir: %s", exc)

    # --- 7. register ------------------------------------------------------
    cmd = build_target_add_cmd(
        key=key,
        pdb_path=str(staged),
        filename_stem=staged.stem,
        contig=contig,
        hotspot_spec=hotspot_spec,
        binder_length=record["binder_length"],
    )
    try:
        rc = run_streaming(cmd, Path(PROTEINA_HOME))
    except FileNotFoundError:
        _fail("input", "complexa", f"`{COMPLEXA_BIN}` binary not found on PATH")

    # --- 8. verify the ARTIFACT, not the exit code ------------------------
    # add_target_cli returns False (not a nonzero exit) when its overwrite
    # prompt hits a closed stdin, so a clean rc proves nothing on its own.
    try:
        written = read_targets_dict(_TARGETS_DICT)
    except Exception as exc:
        _fail("input", "target_registration", f"could not re-read the targets registry: {exc}")
    problem = registration_mismatch(written.get(key), record)
    if problem or rc != 0:
        _fail(
            "input", "target_registration",
            f"registering the uploaded target failed (`complexa target add` "
            f"exited {rc}): {problem or 'the record was written but the command failed'}.",
        )

    n_hub = sum(
        1 for v in written.values()
        if isinstance(v, dict) and str(v.get("source")) == _HUB_SOURCE
    )
    if n_hub > 200:
        logger.warning(
            "targets registry holds %d uploaded targets in this container; "
            "Hydra composes the whole file on every run", n_hub,
        )
    logger.info("custom target registered as %s (%s)", key, contig)
    return key


# GPU allocator flags for every subprocess this module launches.
#
# ``proteinfoundation.generate`` imports colabdesign, which imports JAX (the
# image pins colabdesign 1.1.1 / jax 0.4.29 — Dockerfile.modal:17). JAX's
# DEFAULT is XLA_PYTHON_CLIENT_PREALLOCATE=true at MEM_FRACTION=0.75, so the
# first JAX op reserves 0.75 x 81,920 = 61,440 MB on an A100-80GB regardless of
# how big the target is, and holds it for the life of the process.
#
# That default did more damage than wasted VRAM: it invalidated the only two
# size measurements this tool has. Both canary shards reported ~67.5 GB peak
# from a device-wide nvidia-smi poll, of which 61,440 MB was this reservation.
# Subtract it and ~6.1 GB is left over — but that residual is NOT "the real
# working set", and calling it that would be the same species of invented
# confidence this whole block exists to remove. With preallocation on, JAX
# serves its own allocations FROM the 61,440 MB pool, so they never appear in a
# device-wide reading at all. The ~6.1 GB is only the NON-JAX half (the torch
# generator plus the CUDA context); the JAX/AF2 half is invisible and could be
# anywhere from close to nothing up to the full 61,440 MB. Which makes the
# conclusion stronger, not weaker: the two runs agreed to within 24 MB because
# a CONSTANT dominated the reading, not because the workload is flat in target
# size, and the part that would actually scale with the target is precisely the
# part the reading could not see. Any envelope derived from those numbers is
# arithmetic on an allocator policy. See shared/pdb_preflight_rules.py
# ::_PROTEINA, which states this the same way.
#
# af2 and colabfold already set exactly these — tools/af2/run_pipeline.py:584
# and tools/colabfold/run_pipeline.py:301, "keeps preflight from preallocating
# most of the VRAM". proteina set none of them, and ``run_streaming`` passed no
# ``env=`` at all, so the design subprocess inherited the bare JAX default.
#
# DELIBERATE DIVERGENCE from those two: they also set TF_FORCE_UNIFIED_MEMORY=1
# and this does not. Unified memory lets an oversized job spill to host RAM and
# thrash instead of dying, and thrashing is the EXPENSIVE failure here — it
# bills on to _MAX_SESSION_S = 7200 s (~$12.58 per shard) while a clean OOM
# dies in seconds for cents. For a tool whose open risk is uncapped spend on
# oversized targets, failing fast is worth more than finishing slowly. With
# PREALLOCATE=false and ALLOCATOR=platform, allocation goes through the CUDA
# driver on demand and OOMs at the true device limit; MEM_FRACTION is then
# effectively inert, and is kept only to match the two files above rather than
# to have an effect.
_ALLOCATOR_ENV = {
    "TF_FORCE_GPU_ALLOW_GROWTH": "true",
    "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
    "XLA_PYTHON_CLIENT_ALLOCATOR": "platform",
    "XLA_PYTHON_CLIENT_MEM_FRACTION": "4.0",
}


def design_subprocess_env() -> dict:
    """``os.environ`` plus ``_ALLOCATOR_ENV``, for any GPU subprocess.

    Exported, not private, so the canary launches its design under the SAME
    allocator policy production uses. The canary cannot reach these through
    ``run_streaming`` — it needs its own Popen for the timeout and the VRAM
    poller — and a canary that measures a different allocator than production
    measures nothing production can act on. That is not hypothetical: it is
    exactly how the two existing measurements came to be unusable.

    ``setdefault``, so an operator can still override any of them per-run
    without editing this file.
    """
    env = dict(os.environ)
    for key, value in _ALLOCATOR_ENV.items():
        env.setdefault(key, value)
    return env


# THE DESIGN SUBPROCESS OWNS ITS OWN DEADLINE.
#
# ``modal_app.py`` caps the container at ``_MAX_SESSION_S = 7200`` and gives
# run_pipeline.py itself ``max(60, 7200 - 120) = 7080`` s. Without a timeout of
# our own, a hung ``complexa design`` runs until ONE of those two fires — and
# both land on run_pipeline.py, not on the child: subprocess.run is killed with
# the parent, main() never reaches _write_result, /tmp/smoke_results.json is
# never written, and ``run_tool`` hands the caller ``smoke_result: None`` with
# an empty ``stdout_tail``. A 2 h A100 is billed for a result that says nothing
# at all, and any design the search had ALREADY scored and written to the reward
# CSV dies with it.
#
# BindCraft owns the same deadline for the same reason (``run_command(cmd,
# timeout=timeout_s)``, then a ``timeout`` run_status recorded beside the
# designs it banked) and returns a structured result on the timeout path. This
# is that, sized to leave the shard time to parse, upload/inline, write its
# result and tar the raw tree inside the wrapper's 7080 s.
#
# SIZED AT THE HEADROOM FLOOR, DELIBERATELY. This deadline is NEW, so every
# value below 7080 takes jobs that used to finish and kills them instead: a
# search running between this number and 7080 completed before and does not
# now. Nobody has measured real Fc runtimes against it, so the honest choice
# is the largest value that still leaves the shard the 300 s it needs to
# parse, deliver, write and tar — which is exactly 7080 - 300 = 6780. That
# keeps the newly-fatal band as small as the headroom rule allows (480 s ->
# 300 s) instead of picking a rounder number that forfeits three minutes of
# other people's compute. The test above asserts the 300 s floor, so this
# sits ON it: raising the container ceiling gives this room, lowering it
# fails the test rather than silently inverting the ordering.
DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S = 6780
# The exit code a timed-out search reports into ``result["search"]``. 124 is
# what coreutils `timeout(1)` uses, so it does not collide with a real
# `complexa design` exit status, and ``rc != 0`` already routes it through the
# existing partial-delivery arithmetic.
SEARCH_TIMEOUT_RC = 124


def _design_timeout_s() -> float:
    """Parse the override defensively, exactly as ``_inline_cap_bytes`` does.

    Module scope again: a typo like ``2h`` raising ValueError here would kill
    the container before ``_fail`` can write a result file, which is the very
    outcome the timeout exists to prevent.
    """
    raw = (os.environ.get("PROTEINA_DESIGN_TIMEOUT_S") or "").strip()
    if not raw:
        return float(DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S)
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "PROTEINA_DESIGN_TIMEOUT_S=%r is not a number; using the %d s "
            "default", raw, DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S,
        )
        return float(DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S)
    if value <= 0:
        logger.warning(
            "PROTEINA_DESIGN_TIMEOUT_S=%r is not positive; using the %d s "
            "default", raw, DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S,
        )
        return float(DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S)
    return value


DESIGN_SUBPROCESS_TIMEOUT_S = _design_timeout_s()


def run_streaming(cmd: list[str], cwd: Path, timeout: float | None = None) -> int:
    """Run a subprocess, live-streaming stdout/stderr to Modal logs (never
    capture_output for long GPU work, per the Modal-subprocess memory).

    Raises ``subprocess.TimeoutExpired`` when the child outlives ``timeout``
    (default ``DESIGN_SUBPROCESS_TIMEOUT_S``). Deliberately not swallowed here:
    the caller is the only place that knows whether anything was banked before
    the hang, and main() turns it into a structured result either way.
    """
    if timeout is None:
        timeout = DESIGN_SUBPROCESS_TIMEOUT_S
    logger.info("cmd (cwd=%s, timeout=%ss): %s", cwd, timeout, " ".join(cmd))
    result = subprocess.run(
        cmd, cwd=str(cwd), stdout=sys.stdout, stderr=sys.stderr, check=False,
        env=design_subprocess_env(), timeout=timeout,
    )
    return result.returncode


# ===========================================================================
# Output parsing (tolerant — BUILD-TIME-VERIFY exact names at canary)
# ===========================================================================


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _pick(row: dict, names: tuple[str, ...]) -> Any:
    lowered = {k.lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return None


def find_reward_csv(run_dir: Path) -> Path | None:
    """Locate the per-design reward/results CSV under the shard run dir."""
    patterns = ("**/rewards_*.csv", "**/results_*.csv", "**/*reward*.csv", "**/*.csv")
    for pat in patterns:
        hits = sorted(glob.glob(str(run_dir / pat), recursive=True))
        if hits:
            return Path(hits[0])
    return None


def find_pdb_for(row: dict, run_dir: Path, idx: int, total_rows: int) -> Path | None:
    """Resolve the design PDB for a CSV row: an explicit path column first,
    else match a name column against the PDB glob, else fall back by index
    (only when the design-PDB count matches the row count, so the positional
    pairing is unambiguous)."""
    explicit = _pick(row, _PDB_PATH_COLUMNS)
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = run_dir / explicit
        if p.is_file():
            return p
    # Design PDBs only: exclude the (now-blocked) staged target input and the
    # filter's rejected-sample bucket so an index fallback can never mis-pair a
    # row's scores onto a non-design structure.
    all_pdbs = [
        p for p in sorted(glob.glob(str(run_dir / "**/*.pdb"), recursive=True))
        if "filtered_out_samples" not in p and Path(p).name not in ("target.pdb", "target_input")
    ]
    name = _pick(row, _PDB_NAME_COLUMNS)
    if name:
        stem = str(name)
        for p in all_pdbs:
            if stem in Path(p).name:
                return Path(p)
    # Index fallback only when the design count matches the row count (so the
    # positional pairing is meaningful); otherwise skip rather than mis-pair.
    if len(all_pdbs) == total_rows and idx < len(all_pdbs):
        logger.warning("row %d: matched PDB by index fallback (name match failed)", idx)
        return Path(all_pdbs[idx])
    return None


def parse_designs(run_dir: Path) -> list[dict]:
    """Parse the reward CSV into ranked design rows with a nested ``scores``
    dict. Returns [] when no CSV is found (caller treats as zero survivors)."""
    csv_path = find_reward_csv(run_dir)
    if csv_path is None:
        logger.warning("no reward CSV found under %s", run_dir)
        return []
    logger.info("parsing reward CSV: %s", csv_path)
    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    parsed: list[dict] = []
    for i, row in enumerate(rows):
        scores = {col: _num(_pick(row, names)) for col, names in _SCORE_COLUMNS.items()}
        # cluster_id is an int id, not a measurement.
        if scores.get("cluster_id") is not None:
            scores["cluster_id"] = int(scores["cluster_id"])
        parsed.append(
            {
                "_row_index": i,
                "name": str(_pick(row, _PDB_NAME_COLUMNS) or f"design_{i}")[:64],
                "total_reward": scores.get("total_reward"),
                "scores": scores,
            }
        )
    # Rank by total_reward desc (None at the bottom), mirroring filter.py.
    parsed.sort(key=lambda d: (d["total_reward"] is not None, d["total_reward"] or 0.0), reverse=True)
    # ``rank`` HERE IS THE REWARD-SORT POSITION, 0-BASED, AND IT IS NOT THE
    # NUMBER ANY CALLER EVER SEES. It says "this row sorted Nth"; it does not
    # say "this is delivered design N", because whether a row is delivered at
    # all is decided later — main()'s loop drops a row whose PDB never
    # materialised, whose upload raised, or whose file reads as zero bytes.
    # Feeding this index straight into the candidate would therefore hand a
    # direct caller a rank 0 (every sibling generator's best design is rank 1)
    # and, after any drop, a gapped set. main() counts the DELIVERED rank
    # itself, densely and from 1 — see ``emitted_rank`` there, and boltzgen /
    # rfantibody, which carry the same split for the same reason.
    for sort_position, d in enumerate(parsed):
        d["rank"] = sort_position
    return parsed


# ===========================================================================
# What the shard actually delivered: census + ONE verdict
# ===========================================================================


def census_output_tree(run_dir: Path) -> dict:
    """Count what ``complexa design`` actually wrote under the shard run dir.

    A zero-design result is only interpretable next to this. "The search never
    produced a reward CSV" (the tool broke, exit code notwithstanding) and "the
    filter culled every sample" (the search worked, nothing survived) are the
    same three lines of JSON without it — empty ``designs``, empty
    ``candidates``, status COMPLETED — and they call for opposite responses:
    the first is a bug to fix, the second is a campaign to widen. BindCraft
    carries the identical thing as ``diagnostics["output_tree"]`` and puts it
    in the failure detail for exactly this reason.

    ``filtered_out_pdbs`` is the discriminator that matters most: it is the
    bucket ``find_pdb_for`` deliberately skips, so those structures are
    invisible everywhere else in the result, and a run with 8 of them and 0
    design PDBs is a filter outcome, not a broken tool.

    A DIAGNOSTIC MUST NOT FAIL A RUN. Everything here is best-effort and every
    exception is captured into the census itself, because this is called on
    the paths where something has already gone wrong.
    """
    census: dict[str, Any] = {"run_dir": str(run_dir)}
    try:
        census["exists"] = os.path.isdir(str(run_dir))
        pdbs = glob.glob(str(run_dir / "**/*.pdb"), recursive=True)
        census["design_pdbs"] = len(
            [p for p in pdbs if "filtered_out_samples" not in p])
        census["filtered_out_pdbs"] = len(
            [p for p in pdbs if "filtered_out_samples" in p])
        csvs = sorted(glob.glob(str(run_dir / "**/*.csv"), recursive=True))
        census["csvs"] = [os.path.basename(p) for p in csvs][:40]
        reward_csv = find_reward_csv(run_dir)
        census["reward_csv"] = (
            os.path.basename(str(reward_csv)) if reward_csv else None)
        census["subdirs"] = sorted(
            p.name for p in run_dir.iterdir() if p.is_dir()
        )[:40] if os.path.isdir(str(run_dir)) else []
    except Exception as exc:  # noqa: BLE001 — a diagnostic must never raise
        census["error"] = f"{type(exc).__name__}: {exc}"
    return census


def delivery_verdict(
    *,
    n_parsed: int,
    n_delivered: int,
    n_structures: int,
    n_scored_delivered: int,
    n_inline_capped: int,
    n_failures: int,
    inline_pdbs: bool,
    census: dict | None = None,
) -> dict | None:
    """ONE verdict on what this shard actually delivered. ``None`` == honest.

    Written once and consulted once, because the four ways this shard could
    return ``status: COMPLETED``, exit 0 and no ``error`` key while having
    delivered nothing usable are not four different bugs — they are one
    missing question, asked in four places or nowhere:

        did the caller end up with coordinates AND scores?

    The predecessor asked a narrower one — "did the inline size cap drop
    every design?" — which is true on exactly one of the paths. A shard whose
    PDBs never matched a reward row, whose files could not be read, or whose
    files read as zero bytes reaches the identical outcome (candidates with no
    atoms behind them, or no candidates at all) and used to certify it as a
    clean run: the exact spend-then-look-fine failure the delivery accounting
    exists to prevent. BindCraft returns a structured FAILED for all of them.

    ``n_structures`` is the honest count of DELIVERED COORDINATES and it is
    mode-dependent on purpose: inline, a candidate carries atoms only if it
    was actually inlined (``n_inlined``), because a cap-dropped candidate has
    scores and a pdb_key and no bytes; on the upload path a candidate exists
    only after its PUT returned, so the candidate itself is the evidence.

    NOT a failure, deliberately:
      * ``n_parsed == 0`` — nothing was produced, so nothing was undelivered.
        The campaign pools survivors across shards and delivered-only billing
        releases the hold; a legitimately culled shard is a COMPLETED shard.
        It gets a census instead, so it can be told apart from a broken one.
      * a PARTIAL loss — some designs delivered, some dropped. That is what
        ``n_failures`` and ``inline_delivery`` report, and throwing away the
        survivors would cost more than the diagnosis is worth.

    The ``check`` is reason-specific while the code path is single: a caller
    branching on ``error.check`` gets told which of the four it hit, and
    ``inline_cap_admitted_nothing`` keeps its original spelling because the
    cap is a distinct, operator-fixable cause (raise the env var) rather than
    a symptom of a broken search.
    """
    if n_parsed <= 0:
        return None
    note = f" Output tree: {census}." if census else ""

    if n_structures == 0:
        if inline_pdbs and n_inline_capped > 0:
            return {
                "bucket": "delivery",
                "check": "inline_cap_admitted_nothing",
                "detail": (
                    f"the inline PDB cap ({INLINE_PDB_TOTAL_CAP_BYTES} bytes) "
                    f"admitted none of the {n_inline_capped} design(s) this "
                    "shard produced, so the run delivered no coordinates at "
                    "all. Scores and ranks are in this result and the atoms "
                    "are in the raw archive. Raise "
                    "PROTEINA_INLINE_PDB_CAP_BYTES above one design's PDB and "
                    "re-run." + note
                ),
            }
        return {
            "bucket": "delivery",
            "check": "no_coordinates_delivered",
            "detail": (
                f"the search scored {n_parsed} design(s) but this shard "
                f"delivered no coordinates at all ({n_delivered} candidate(s) "
                f"survived, {n_failures} dropped: no PDB matched the "
                "reward-CSV row, the file could not be read, or it read as "
                "zero bytes). An A100 was billed and no structure came back. "
                "Whatever the search did write is in the raw archive." + note
            ),
        }

    if n_delivered > 0 and n_scored_delivered == 0:
        return {
            "bucket": "delivery",
            "check": "no_scores_delivered",
            "detail": (
                f"all {n_delivered} delivered design(s) carry coordinates but "
                "not one carries a total_reward, so nothing in this result can "
                "be ranked or triaged and the rank order is arbitrary. The "
                "reward CSV was found and read, so this is a column/scoring "
                "failure rather than a missing file. The structures are in "
                "this result and the full output tree is in the raw "
                "archive." + note
            ),
        }
    return None


# ===========================================================================
# Raw output capture (the counterpart to the parser above)
# ===========================================================================


def archive_raw_outputs(out_dir: Path, dest: str = RAW_ARCHIVE_PATH) -> None:
    """Tar the COMPLETE shard output tree to ``dest``. Best-effort: never raises.

    A container must not decide which fields are worth keeping. Everything above
    this line throws work away: ``_SCORE_COLUMNS`` maps 6 display keys out of the
    reward CSV and drops every other column; ``find_reward_csv`` reads the FIRST
    matching CSV and ignores the rest; ``find_pdb_for`` skips the
    filtered_out_samples bucket; and only PDBs that matched a scored row are
    uploaded. The Hydra resolved config, the analyze artifacts and every unmapped
    column then die with the container, recoverable only by re-paying for the
    A100. That is exactly how ``design_iptm`` (the real binder->target interface)
    was lost behind ``iptm`` (an average over every chain pair, ~2x high) on 460
    designs across two campaigns. Decide LOCALLY, where re-parsing is free.

    Note this archives ``run_dir`` (./inference), NOT the ``work_dir`` the shard
    runs from: work_dir is /opt/proteina, the repo root, and the weights and
    rewards Volumes are mounted INSIDE it (./ckpts, ./rewards). Tarring the work
    dir would archive tens of GB of model checkpoints on every run. ./inference is
    the whole of what this shard produced.

    Failure to archive must never break the run: a shard that crashed before
    writing output is exactly when the diagnostics matter most, so problems are
    logged, never raised.
    """
    try:
        src = os.path.abspath(str(out_dir))
        if not os.path.isdir(src):
            logger.warning("raw capture: nothing to archive, no dir at %s", src)
            return
        dest_abs = os.path.abspath(dest)
        # The tar must never be written inside the tree it archives, or it tars
        # itself. /tmp is outside /opt/proteina/inference, but assert it rather
        # than trust it — this is cheap and the failure mode is silent.
        if os.path.commonpath([dest_abs, src]) == src:
            logger.error("raw capture: refusing to write %s inside its own source %s", dest_abs, src)
            return
        # Stream to a file, never io.BytesIO: ~1x peak RSS instead of ~3-4x, which
        # matters on a tree carrying every sample PDB the search emitted.
        with tarfile.open(dest_abs, "w:gz") as tf:
            tf.add(src, arcname=os.path.basename(src) or "inference")
        logger.info(
            "raw capture: archived %s -> %s (%.1f MB)",
            src, dest_abs, os.path.getsize(dest_abs) / 1e6,
        )
    except Exception as exc:
        logger.warning("raw capture failed (non-fatal): %s: %s", type(exc).__name__, exc)
        # A crash mid-write (e.g. ENOSPC) can leave a truncated but still-openable .tgz at
        # the destination; the wrapper parks whatever exists. Remove the partial so a failed
        # capture parks NOTHING rather than a tar that reports success but cannot be read.
        try:
            if os.path.exists(dest_abs):
                os.remove(dest_abs)
        except OSError:
            pass


# ===========================================================================
# validate tier (wallet-free staging gate — NOT a CPU-only container)
# ===========================================================================
# "free, CPU dry-run" is what this header used to say, and half of it was
# false in the direction that costs money. WALLET-free is true: tools-hub does
# not bill the validate preset. CPU-only is not: modal_app.py declares exactly
# one @app.function and it is unconditionally `gpu="A100-80GB"`, so this tier
# runs on an A100 container for its whole lifetime and Modal bills wall-clock
# rather than utilisation. Skipping GPU *work* is not the same as not
# allocating a GPU. See FN_GPU in tools/proteina/direct_call_fc.py, whose
# direct path bypasses the wallet and therefore pays the infrastructure bill
# with nothing free about it.


def run_validate(config_dir: str, preset: str, task_name: str) -> None:
    """Pre-flight / staging gate. Variant-agnostic checks, no GPU compute:
    (1) the proteinfoundation package imports (src-layout sane), (2) all three
    variant config files are present, (3) at least one model checkpoint is
    present on the mounted weights Volume (catches an unseeded / wrong-path
    weights mount — the HIGH-2 failure mode — before any paid search). A full
    reward-model load needs a GPU and is the P-1/P-2 canary's job, not this
    tier. Writes a PASS/FAIL smoke result and exits."""
    start = time.time()
    problems: list[str] = []
    try:
        import importlib  # noqa: PLC0415
        importlib.import_module("proteinfoundation.generate")
        importlib.import_module("proteinfoundation.filter")
    except Exception as exc:
        problems.append(f"package import failed: {exc}")
    for name in _ALL_CONFIGS:
        if not os.path.isfile(f"{config_dir}/{name}.yaml"):
            problems.append(f"missing config {name}.yaml under {config_dir}")
    if not glob.glob(f"{WEIGHTS_DIR}/**/*.ckpt", recursive=True):
        problems.append(f"no model checkpoint (*.ckpt) found under {WEIGHTS_DIR}")

    if problems:
        _write_result(
            {
                "status": "FAILED",
                "tier": "validate",
                "error": {"bucket": "validate", "check": "preflight", "detail": "; ".join(problems)},
                "provider_job_id": os.environ.get("JOB_ID", ""),
            }
        )
        logger.error("validate FAILED: %s", "; ".join(problems))
        sys.exit(1)

    _write_result(
        {
            "status": "COMPLETED",
            "tier": "validate",
            "preset": preset,
            "task_name": task_name,
            "designs_total": 0,
            "designs_completed": 0,
            "n_failures": 0,
            "designs": [],
            "candidates": [],
            "validate_ok": True,
            "runtime_seconds": int(time.time() - start),
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    logger.info("validate OK in %ds", int(time.time() - start))


# ===========================================================================
# Main
# ===========================================================================


def _run_shard() -> None:
    """The shard itself. Always entered through ``main()``, never directly —
    ``main()`` is what guarantees a result file exists on every exit path."""
    start = time.time()
    payload = parse_payload()

    job_spec = payload.get("job_spec") or {}
    preset = str(payload.get("tier") or os.environ.get("JOB_TIER") or "protein_binder").strip()
    config_name = str(job_spec.get("config_name") or "")
    task_name = str(job_spec.get("task_name") or "")
    rf3_required = bool(job_spec.get("rf3_required"))
    nsamples = int(job_spec.get("nsamples") or 4)
    replicas = int(job_spec.get("replicas") or 2)
    nsteps = job_spec.get("nsteps")
    # Custom-target fields. Defaults reproduce the curated behaviour exactly, so
    # a campaign created before these existed keeps draining unchanged.
    target_source = str(job_spec.get("target_source") or "curated")
    # Both accept the shared cross-tool spelling as well as this file's native
    # one; see normalize_target_chain / normalize_hotspots.
    #
    # ACCEPTED WEB-PATH CHANGE, recorded here rather than discovered later.
    # Routing this field through normalize_target_chain also DE-DUPLICATES it,
    # and the web adapter does not (tools/proteina/__init__.py joins the form
    # field on whitespace and stops). So a form entry of "A A" or "A,A" —
    # which validate() accepts — used to reach ``derive_segments`` as two
    # tokens and register ``--target-input A1-200,A1-200``; it now registers
    # ``A1-200``. Measured across the adapter's whole accepted input space,
    # that is the ONLY divergence this change introduces on the web path.
    #
    # It is accepted because the deduplicated form is the one whose meaning is
    # known. A repeated segment is not a harmless spelling here: counting one
    # is what let ``A10-20,A10-20`` report 22 residues for 11 and walk through
    # the minimum-size floor (see selected_residue_keys — that particular hole
    # is closed, by counting a de-duplicated key set, but it is what the shape
    # costs when something downstream forgets). And upstream's own contig
    # grammar is unread: atomworks is not vendored here, so what
    # ``AtomSelectionStack.from_contig`` does with the same range named twice
    # is not something this file may assume. Sending it once removes the
    # question. Pinned end to end by TestAcceptedWebPathChanges in
    # tests/test_proteina_delivery.py.
    target_chain = normalize_target_chain(str(job_spec.get("target_chain") or ""))
    target_input = str(job_spec.get("target_input") or "")
    try:
        hotspot_spec = normalize_hotspots(job_spec)
    except ValueError as exc:
        # Ambiguous bare-int hotspots on a multi-chain target. Pre-GPU, and it
        # has to be: the guess it refuses to make would pass every downstream
        # check and produce a run that looks correct.
        _fail("input", "hotspot_chain_ambiguous", str(exc))
    except TypeError as exc:
        # A hotspot field of the wrong TYPE. Caught for the same reason
        # _inline_cap_bytes parses defensively: an uncaught exception here
        # escapes main() before _fail can write /tmp/smoke_results.json, so
        # modal_app's json.load finds nothing, `smoke_result` comes back None,
        # and a mistyped field is reported as a webhook delivery failure on a
        # container that is already allocated and billing.
        _fail("input", "hotspot_malformed", str(exc))
    binder_length = [int(v) for v in (job_spec.get("binder_length") or [60, 120])]

    webhook_url = os.environ.get("WEBHOOK_URL", "")
    job_id = os.environ.get("JOB_ID", "")
    job_token = payload.get("job_token", "") or os.environ.get("JOB_TOKEN", "")
    upload_endpoint = payload.get("upload_urls_endpoint", "")
    input_url = payload.get("input_presigned_url", "")

    rf3_on = _rf3_enabled()
    logger.info(
        "proteina shard: preset=%s config=%s task=%s target_source=%s "
        "chain=%s contig=%s hotspots=%s rf3_required=%s rf3_on=%s",
        preset, config_name, task_name or "-", target_source,
        target_chain or "-", target_input or "-",
        " ".join(hotspot_spec) or "-", rf3_required, rf3_on,
    )

    # --- validate tier: no wallet charge, no GPU compute — but the container
    # --- is still the A100 run_tool always allocates. See the section header.
    if preset == "validate":
        run_validate(CONFIG_DIR, preset, task_name)
        return

    # --- RF3 kill-switch hard-block (pre-GPU) --------------------------------
    # ligand_binder / motif_ame score on RF3 only (no AF2 ligand protocol), so
    # with RF3 off they have no valid reward. Fail before spending any GPU.
    if rf3_required and not rf3_on:
        _fail(
            "preflight",
            "rf3",
            f"the {preset} variant scores on RoseTTAFold3, which is currently "
            "disabled (PROTEINA_RF3=off). No AlphaFold2 fallback exists for "
            "this variant. Re-enable RF3 or choose the protein_binder variant.",
        )

    if not config_name:
        _fail("preflight", "config", "job_spec.config_name is empty")

    # Delivery mode, decided pre-GPU so the choice is visible in the logs of a
    # run that later returns no coordinates. This used to be a hard _fail:
    # without an endpoint the per-design loop had nowhere to put a PDB, so
    # refusing before spending money was correct. It is no longer the only
    # option — INLINE carries the atoms in the return value instead — so the
    # refusal is now only about the case where inlining was explicitly
    # disabled AND there is no endpoint, which really does deliver nothing.
    # Inline ONLY when there is no endpoint. Deliberately not "always, as a
    # bonus": with an endpoint the atoms are already in Storage and resolve by
    # pdb_key, so a second copy in the Modal return value buys nothing and is
    # not free. shared/compute_campaigns.py reconcile_campaign_children pulls
    # each finished child's FULL return into web-tier memory (max_poll=64)
    # from inside a user-facing request; at 8 designs/shard an Fc-sized target
    # is ~3.6 MB of base64 per child, so "harmless extra field" is ~230 MB
    # through one worker. Gating here is what makes the claim that the web
    # path is unchanged actually true, rather than true only of its upload
    # mechanics.
    inline_pdbs = _inline_enabled() and not upload_endpoint
    cap_ok = INLINE_PDB_TOTAL_CAP_BYTES >= INLINE_PDB_MIN_USEFUL_CAP_BYTES

    # A HUB-SHAPED PAYLOAD THAT LOST ITS ENDPOINT STILL FAILS FREE.
    #
    # Dropping the unconditional pre-GPU refusal is what buys parity with the
    # five siblings — every one of them does a bare
    # ``payload.get("upload_urls_endpoint", "")`` and never refuses, so an
    # endpoint-less DIRECT call must run and deliver inline. But the same
    # deletion also un-refused a case that is not a direct call at all: a
    # tools-hub web submission whose endpoint went missing. That is a bug in
    # the web tier, and it used to cost nothing (FAILED, pre-GPU, before the
    # container did any work). Silently running it INLINE instead spends a full
    # A100 shard and then persists multi-MB of base64 into job.result, where
    # the caller that was supposed to receive presigned-upload pointers has no
    # idea what to do with it.
    #
    # ``job_token`` / ``WEBHOOK_URL`` is the discriminator, and it is exact
    # rather than heuristic. ``ModalClient.submit`` (gpu/modal_client.py) takes
    # ``job_token`` and ``webhook_url`` as REQUIRED keyword arguments, so every
    # web submission carries both; ``modal_app._build_run_env`` copies them
    # straight into JOB_TOKEN / WEBHOOK_URL for this process. A direct call
    # sets neither — ``direct_call_fc.py`` documents the deliberate absence of
    # both the endpoint and the token, and passes no webhook — so the two
    # populations do not overlap.
    #
    # Same ``check`` name the original refusal used (``upload_urls_endpoint``),
    # because anything already branching on it is looking for exactly this;
    # only the detail changes, to name the real cause instead of implying the
    # operator forgot a field.
    if (job_token or webhook_url) and not upload_endpoint:
        _fail(
            "preflight",
            "upload_urls_endpoint",
            "this payload carries "
            + " and ".join(
                [n for n, v in (("a job_token", job_token),
                                ("a WEBHOOK_URL", webhook_url)) if v])
            + ", so it was submitted by the tools-hub web tier, but it has no "
            "upload_urls_endpoint. The endpoint is required on that path: the "
            "hub expects each design in Storage behind a pdb_key and there is "
            "no direct caller here to hand inline coordinates to. Refusing "
            "before any GPU spend — this is a submission bug in the web tier, "
            "not a bad request. (A genuine direct "
            "`modal.Function.from_name(...)` call carries neither a job_token "
            "nor a webhook and is delivered inline instead.)",
        )

    if not upload_endpoint and not (inline_pdbs and cap_ok):
        # Every way a finished design could end up with nowhere to put its
        # coordinates, refused before the GPU rather than after. A cap of 0 is
        # the sharp edge here: "0" is truthy so it does not fall back to the
        # default, _inline_enabled() is still True, and without this clause the
        # run would spend an A100 and return COMPLETED with zero structures.
        reason = (
            "inline PDBs are disabled (PROTEINA_INLINE_PDBS=off)" if not _inline_enabled()
            else f"the inline PDB cap is {INLINE_PDB_TOTAL_CAP_BYTES} bytes, "
                 f"below the {INLINE_PDB_MIN_USEFUL_CAP_BYTES} byte floor "
                 "(PROTEINA_INLINE_PDB_CAP_BYTES)"
        )
        _fail(
            "preflight",
            "upload_urls_endpoint",
            f"no upload_urls_endpoint in the payload and {reason}, so a "
            "completed design would have nowhere to put its coordinates. "
            "Supply the endpoint, re-enable inline delivery, or raise the cap.",
        )
    logger.info(
        "design delivery: upload=%s inline=%s (cap %.0f MB)",
        "on" if upload_endpoint else "off",
        "on" if inline_pdbs else "off",
        INLINE_PDB_TOTAL_CAP_BYTES / 1e6,
    )

    # Designs/shard is derived from the (overridden) generation profile so it
    # tracks the actual Hydra overrides, not a hardcoded 8.
    designs_total = nsamples * replicas

    # --- target-source invariant (pre-GPU) -----------------------------------
    # EXACTLY ONE target source per shard, declared explicitly rather than
    # inferred. The declaration is made once, by the route, at campaign
    # creation — the only place that knew whether a structure actually exists —
    # and rides campaign.params into every chunk. This block re-checks it here
    # because the container must be able to refuse on its own: it is the last
    # gate before money turns into GPU.
    #
    # The `elif input_url` branch is the original bring-your-own hard block,
    # narrowed rather than deleted. Its safety intent is unchanged and is the
    # whole reason it existed: a staged structure arriving on a run that did NOT
    # declare a custom target must never fall through to ++generation.task_name,
    # which resolves a REPO-BUNDLED benchmark target — that would design against
    # the wrong structure on billed GPU and look completely successful.
    if target_source == "custom":
        if not input_url:
            _fail(
                "input", "target_missing",
                "this run declared a custom target but no target structure was "
                "staged for it. Refusing rather than falling back to a benchmark "
                "target.",
            )
        if task_name:
            _fail(
                "input", "target_conflict",
                "this run declared a custom target but also carries the curated "
                f"benchmark task {task_name!r}. Refusing rather than designing "
                "against the wrong structure.",
            )
        if preset not in _CUSTOM_TARGET_PRESETS:
            _fail(
                "input", "custom_target_variant",
                f"bring-your-own targets are not available for the {preset} "
                "variant: upstream resolves its task from a separate registry "
                "that `complexa target add` does not write. Pick a curated task.",
            )
    elif input_url:
        _fail(
            "input", "target_conflict",
            "a target structure was staged for this run but it was not declared "
            "as a custom-target run, so the search would silently use a curated "
            "benchmark target instead. Refusing before any GPU spend.",
        )
    elif not task_name:
        _fail(
            "input", "target_missing",
            "no curated benchmark task and no custom target — nothing to design "
            "against.",
        )

    send_heartbeat(webhook_url, job_id, stage="loading_model", designs_total=designs_total)

    # Run from the repo root so the configs' RELATIVE paths resolve exactly as
    # upstream's own `complexa design configs/...` invocation: ./ckpts (weights
    # Volume), ./assets/target_data (bundled benchmark target PDBs), ./configs
    # (Hydra search path), ./inference (outputs). generate.py uses @hydra.main;
    # modern Hydra's job.chdir defaults off, so cwd stays here across all stages.
    work_dir = Path(PROTEINA_HOME)
    run_dir = work_dir / "inference"
    # Defeat the warm-container results-CSV early-exit: generate.py exits early if
    # ./inference/results_<config>_<job_id>.csv exists, and that name is keyed on
    # (config_name, job_id=0) — identical across same-variant shards. A reused
    # warm container would then re-emit the prior shard's designs. Modal runs one
    # input per container at a time (no concurrent inputs on this function), so
    # wiping ./inference at shard start is safe and isolates every shard.
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Everything from here to the end of the shard runs under a try/finally so the
    # complete output tree is archived on EVERY exit path: success, zero
    # survivors, _fail()'s sys.exit (SystemExit still runs a finally), or an
    # uncaught exception. The try opens AFTER the wipe above on purpose — on a
    # warm container the preflight _fail()s can leave the PREVIOUS shard's
    # ./inference standing, and archiving that would file another shard's tree
    # under this job id. Everything inside was written by this shard alone.
    #
    # Custom-target staging sits INSIDE the try for the same reason: it copies
    # the exact input bytes into run_dir/_hub_input, and a run that dies on a
    # missing hotspot is precisely the one whose input you want to inspect
    # afterwards without re-paying for the A100.
    try:
        if target_source == "custom":
            send_heartbeat(
                webhook_url, job_id, stage="preparing_target",
                designs_total=designs_total,
            )
            task_name = prepare_custom_target(
                input_url=input_url,
                job_id=job_id,
                target_chain=target_chain,
                target_input=target_input,
                hotspot_spec=hotspot_spec,
                binder_length=binder_length,
                run_dir=run_dir,
            )

        seed = shard_seed(job_id)
        run_name = f"shard_{(job_id or 'x')[:12]}"
        cmd = build_design_cmd(
            config_name=config_name, task_name=task_name, seed=seed,
            nsamples=nsamples, replicas=replicas, nsteps=nsteps,
            run_name=run_name, rf3_on=rf3_on,
        )
        send_heartbeat(webhook_url, job_id, stage="searching", designs_total=designs_total)

        # A HUNG SEARCH IS A RESULT, NOT A DEAD CONTAINER. See
        # DESIGN_SUBPROCESS_TIMEOUT_S: without this except the kill arrives
        # from Modal or from the wrapper, lands on THIS process, and the shard
        # returns no result file at all — no scores, no coordinates, no
        # diagnosis, on a fully billed A100. Catching it here keeps the rest of
        # the function running, so anything ``complexa design`` had already
        # written to the reward CSV before it hung is still parsed, still
        # delivered, and still ranked. That is BindCraft's behaviour on its own
        # timeout, and the only reason it can bank partial work.
        search_timeout_s: float | None = None
        try:
            rc = run_streaming(cmd, work_dir)
        except FileNotFoundError:
            _fail("search", "complexa", f"`{COMPLEXA_BIN}` binary not found on PATH")
        except subprocess.TimeoutExpired as exc:
            rc = SEARCH_TIMEOUT_RC
            search_timeout_s = float(exc.timeout or DESIGN_SUBPROCESS_TIMEOUT_S)
            logger.error(
                "`complexa design` exceeded its %.0fs timeout and was killed; "
                "delivering whatever it had already written", search_timeout_s,
            )

        designs = parse_designs(run_dir)
        # `complexa design` chains generate -> filter -> evaluate -> analyze. A late
        # stage can exit nonzero AFTER the reward CSV (with complete scores) is
        # already written — observed on the ligand path (P-3 canary: 8 designs fully
        # RF3-scored, then exit 1). Cross-shard diversity is assigned at the hub, so
        # we still DELIVER designs that were fully scored; only fail when the nonzero
        # exit left nothing scored to deliver (a genuine early failure).
        n_scored = sum(1 for d in designs if d.get("total_reward") is not None)
        if rc != 0:
            if n_scored == 0 and search_timeout_s is not None:
                # Its OWN check name. "exited 124" is a number nobody can act
                # on; "the search ran out of time" says which knob moves
                # (PROTEINA_DESIGN_TIMEOUT_S, or a smaller target) and rules
                # out the crash reading entirely.
                _fail(
                    "search", "timeout",
                    f"`complexa design` exceeded its {search_timeout_s:.0f}s "
                    "timeout and was killed before it scored a single design. "
                    "Nothing was banked, so there is nothing to deliver. The "
                    "partial output tree is in the raw archive. Raise "
                    "PROTEINA_DESIGN_TIMEOUT_S (it must stay under the "
                    "container's own ceiling) or reduce the target size / "
                    "nsamples.",
                )
            if n_scored == 0:
                _fail("search", "complexa", f"`complexa design` exited {rc} with no scored designs")
            logger.warning(
                "complexa design exited %d but %d/%d designs are fully scored — delivering "
                "(late analyze/eval failure is non-fatal; hub does cross-shard diversity)",
                rc, n_scored, len(designs),
            )

        if not designs:
            # A shard that legitimately produced no survivors still COMPLETES
            # with zero candidates — the campaign pools survivors across shards,
            # and delivered-only billing releases this shard's hold. NOT a
            # delivery failure either: nothing was produced, so nothing was
            # undelivered. The counters ride along at zero purely so
            # `inline_delivery` is present on EVERY inline result and a caller
            # can read it without first testing for its existence.
            #
            # WITH A CENSUS, THOUGH. "COMPLETED, 0 designs, 0 failures" is the
            # same three lines whether the filter culled every sample or
            # `complexa design` never wrote a reward CSV at all, and those call
            # for opposite responses. Note this branch is reachable only with
            # rc == 0: the guard above already _fail()s on a nonzero exit with
            # nothing scored, which is what a zero-design shard always is. So a
            # census here is describing a run that claims to have succeeded.
            runtime = int(time.time() - start)
            empty_result = {
                "status": "COMPLETED",
                "tier": preset,
                "designs_total": designs_total,
                "designs_completed": 0,
                "n_failures": 0,
                "designs": [],
                "candidates": [],
                "output_census": census_output_tree(run_dir),
                "runtime_seconds": runtime,
                "provider_job_id": job_id,
            }
            if inline_pdbs:
                empty_result["inline_delivery"] = {
                    "n_inlined": 0,
                    "n_inline_capped": 0,
                    "inline_bytes_used": 0,
                    "cap_bytes": INLINE_PDB_TOTAL_CAP_BYTES,
                }
                # Empty for the same reason the counters are zero: present on
                # EVERY inline result so a caller can read it without first
                # testing for its existence. Nothing was parsed here, so
                # nothing could have been dropped.
                empty_result["undelivered"] = []
            _write_result(empty_result)
            send_heartbeat(webhook_url, job_id, stage="complete", designs_total=designs_total)
            logger.info("shard produced 0 survivors in %ds", runtime)
            return

        # --- upload and/or inline each design --------------------------------
        # Parsed ONCE, not per design: the reference is the STAGED CROPPED file,
        # which is what upstream was given and therefore the only thing the
        # design's target chains can be positionally compared against. The
        # uploaded original is the wrong reference — the crop is what defines
        # which residues exist.
        renumber_reference: dict[str, list[tuple[int, str, str]]] = {}
        if target_source == "custom" and task_name:
            # ``except Exception``, not ``except OSError``. This block sits
            # inside main()'s outer try/finally, which has no except clause of
            # its own, so anything this raises that is not an OSError kills a
            # shard that has ALREADY PAID for its GPU and loses all 8 designs.
            # Nothing about restoring a residue number is worth that.
            #
            # latin-1, not utf-8: PDB is a fixed-COLUMN format, and a multi-byte
            # utf-8 sequence would shift every column after it. latin-1 maps one
            # byte to one character for all 256 values, so column 23 is byte 23.
            #
            # KNOWN ASYMMETRY, NOT FIXED, and measured rather than reasoned
            # about. Three reads of one upload disagree about encoding:
            # ``prepare_custom_target`` reads it with
            # ``incoming.read_text(errors="replace")`` (platform default),
            # ``stage_cropped_target`` WRITES the crop with
            # ``dest.write_text(...)`` (platform default again), and this line
            # reads that back as latin-1. All three agree byte for byte on the
            # ASCII a coordinate section is made of, which is every real target.
            #
            # A non-ASCII byte does NOT reach here through an annotation
            # record: ``crop_pdb_to_contig`` emits COORDINATE lines — ``ATOM``,
            # and ``HETATM`` for a modified residue in ``_MODRES_EQUIV`` — plus
            # one ``TER`` per chain and a final ``END``, and no annotation
            # record at all, so no REMARK, HEADER or SEQRES survives to carry
            # one. Where it CAN land is a coordinate line the crop keeps,
            # and there it moves both of the things an earlier version of this
            # note said it moved neither of. Under a UTF-8 default — what the
            # container runs — ``errors="replace"`` destroys the byte before
            # anything is written, the U+FFFD goes out as three bytes, this
            # latin-1 read finds that line two columns wider, its resSeq field
            # no longer parses and the residue drops out of the reference
            # entirely: a chain one residue short, refused as "length differs".
            # Under a single-byte default the widths hold and the residue NAME
            # changes instead, refused as "sequence identity". Fail-closed both
            # ways — a clean apply becomes a refusal, never a wrong file — and
            # ``stage_cropped_target``'s own count self-check cannot see either,
            # because it reads the file back with the same default encoding it
            # was written with. See TestTheStagedReferenceEncoding.
            try:
                renumber_reference = pdb_ca_sequence(
                    (Path(_HUB_TARGET_DIR) / f"{task_name}.pdb")
                    .read_text(encoding="latin-1"))
            except Exception as exc:  # noqa: BLE001 — see above
                logger.warning(
                    "renumbering: staged target unreadable (%s) — designs ship "
                    "in upstream's 1..N numbering", exc)
        renumber_chains = sorted(renumber_reference)
        # WHAT GETS RECORDED WHEN NOTHING WAS RESTORED, and why it is not always
        # "upstream". Every value here reaches the results page, which turns
        # "upstream" into a WARNING: "not the numbering in the file you
        # uploaded ... hotspot labels such as A241 will not resolve". That is
        # true of a custom run whose restore declined and false of a curated
        # benchmark run, where there is no uploaded file to be at odds with.
        # Computed once, outside the loop, because it is a property of the RUN
        # and not of any design.
        not_restored = "upstream" if target_source == "custom" else "n/a"
        # Warn once per shard, not once per design. A flag rather than a test on
        # ``rank``: rank comes from ``enumerate(parsed)`` and therefore starts at
        # ZERO, so "rank == 1" would announce the second design and stay silent
        # on a one-design shard.
        #
        # LOG-ONLY, and that is a limit rather than a design: this line reaches
        # whoever reads the container log, not the operator. What the OPERATOR
        # gets is ``target_numbering`` on every candidate and the sentence the
        # results page renders from it. The log line carries the ``reason``,
        # which the page does not.
        renumber_warned = False

        out_designs: list[dict] = []
        out_candidates: list[dict] = []
        n_failures = 0
        n_rows = len(designs)
        inline_bytes_used = 0
        n_inlined = 0
        n_inline_capped = 0
        # THE SCORES OF A DROPPED DESIGN SURVIVE THE DROP. INLINE ONLY.
        #
        # Each of the three ``continue``s below throws away a row that an A100
        # already scored: the reward, the AF2/RF3 confidences and the cluster
        # id are computed BEFORE this loop runs, and losing the PDB does not
        # make them wrong. They used to vanish with the design — nothing in the
        # returned result recorded that the row existed, let alone what it
        # scored — and the raw archive is a tarball in a Modal Volume that
        # neither ``--collect`` nor the hub ever opens, so "it is in the
        # archive" is not the same as "the caller has it". A shard that scored
        # 8 and matched 6 PDBs handed back six designs and no way to learn that
        # the best-scoring row was one of the two that went missing.
        #
        # WHY NOT AS A CANDIDATE, which is what BindCraft does (it keeps the
        # entry and just omits ``pdb_content_b64`` when the read raises). Two
        # reasons, both specific to this file rather than to taste:
        #
        #   * The upload path MUST keep dropping — it is the production web
        #     tier and its failure arithmetic is fixed. Keeping the design on
        #     the inline path only would make ``designs_completed`` and
        #     ``n_failures`` depend on the delivery mode, which is exactly what
        #     ``test_a_read_failure_is_counted_the_same_way_on_both_paths``
        #     exists to forbid.
        #   * ``delivery_verdict`` reads ``n_scored_delivered`` off the
        #     candidate list to answer "did the caller get coordinates AND
        #     scores?". Admitting score-only candidates would let a run in
        #     which no single candidate has both pass as COMPLETED — the
        #     no_scores_delivered check would see a scored candidate and stop
        #     asking. ``candidates`` means "delivered, with atoms" here, and
        #     the verdict is built on that meaning.
        #
        # So the scores ride in their own list, clearly marked as NOT
        # delivered, and every consumer that resolves structures
        # (shared/storage.py, shared/exports.py, the candidate table) keeps
        # reading ``candidates`` and sees exactly what it saw.
        undelivered: list[dict] = []
        # Basenames whose PUT raised and whose atoms therefore travel inline
        # instead. UPLOAD PATH ONLY, and only ever non-empty on a run where
        # something was already broken. The name is not local shorthand: it is
        # the key ``shared/jobs.py::_slim_result_for_persist`` reads to decide
        # that a ``designs/``-prefixed candidate's inline copy is the ONLY copy
        # and must survive persistence, and rfdiffusion already emits it with
        # exactly this meaning. Added to the result only when non-empty, so a
        # healthy web result grows no key.
        failed_uploads: list[str] = []
        # THE DELIVERED RANK IS COUNTED HERE, not read off the parsed row.
        #
        # ``parse_designs`` numbers rows by reward-sort position, 0-based, over
        # rows that have not yet faced any of the three drops below (no PDB
        # matched, upload/read raised, zero-byte file). Using it as the
        # candidate rank produced BOTH defects the other five generators
        # already fixed: a best design at rank 0 / design_000.pdb where every
        # sibling emits rank 1 / design_001.pdb, and — once anything was
        # dropped — surviving ranks like 2,3,4 with no rank 1 in the result at
        # all. boltzgen's comment on the identical bug: "designs without a
        # matching structure file get skipped — so we can't use rank_idx for
        # the candidate rank or we end up with gaps"; rfantibody mirrors it.
        #
        # It matters beyond cosmetics. ``shared/exports.py`` states the
        # cross-tool invariant that "every tool emits a rank 1 and a
        # ``design_1.pdb``" and copies this number into ``source_rank``, so a
        # merged campaign export listed proteina's rows off by one against
        # every other generator; ``direct_call_fc`` names the operator's output
        # files from it, so a collected shard began at design_002.pdb with
        # nothing to distinguish "two were dropped" from "the numbering is
        # offset"; and the candidate table renders it verbatim, showing "0" for
        # the top design.
        #
        # ``emitted_rank`` advances ONLY when a design actually joins the
        # candidate list, which is why ``rank`` below is provisional until the
        # append: proteina — unlike the siblings, which batch their uploads
        # after the loop — can still drop a design AFTER the name is needed for
        # the upload request. A dropped design must not burn its number, or the
        # gap simply moves to a later drop path. Reusing the basename is
        # correct as well as dense: the drop means nothing of that design was
        # delivered, so no candidate points at the object it would have written.
        emitted_rank = 0

        def keep_scores(design: dict, reason: str) -> None:
            """Bank a dropped design's scores. INLINE ONLY — see ``undelivered``.

            Gated on ``inline_pdbs`` rather than on "there is no endpoint" so a
            web-path result can never grow this key, which is the same gate
            ``inline_delivery`` uses and the same reason.
            """
            if not inline_pdbs:
                return
            undelivered.append({
                "name": design["name"],
                "reason": reason,
                "scores": design["scores"],
            })

        for d in designs:
            pdb_path = find_pdb_for(d, run_dir, d["_row_index"], n_rows)
            if pdb_path is None:
                n_failures += 1
                keep_scores(d, "no_pdb_matched")
                logger.warning(
                    "design %s (reward CSV row %d): no PDB file matched — skipping",
                    d["name"], d["_row_index"],
                )
                continue
            rank = emitted_rank + 1
            basename = f"design_{rank:03d}.pdb"
            # The `designs/` prefix is a claim that the bytes are in Storage —
            # the web service's resolver reads it as {user}/{job}/designs/<name>
            # and shared/jobs.py _slim_result_for_persist strips the inline copy
            # from any candidate carrying it, on the stated grounds that the
            # structure "resolves from Storage". Nothing was uploaded on the
            # inline path, so claiming the prefix there would let slimming
            # delete the ONLY copy and leave a pointer at an object that was
            # never written — scores intact, every structure gone, no error
            # (shared/storage.py skips a candidate that resolves via neither).
            # A bare filename is the convention jobs.py already documents for
            # candidates that are not Storage-backed.
            pdb_key = f"designs/{basename}" if upload_endpoint else basename
            numbering = not_restored
            # THE READ AND THE UPLOAD ARE NOW TWO DIFFERENT FAILURES, because
            # they leave the shard in two different states. A read that raises
            # leaves NO BYTES — there is nothing to deliver by any route, so it
            # drops and counts, identically on both paths (that symmetry is
            # what ``test_a_read_failure_is_counted_the_same_way_on_both_paths``
            # forbids drifting). An upload that raises leaves the bytes sitting
            # right here, already read, already correct.
            try:
                pdb_bytes = pdb_path.read_bytes()
            except Exception as exc:
                n_failures += 1
                keep_scores(d, "pdb_read_failed")
                # Named by DESIGN, not by rank: this one is being dropped, so
                # it never gets a delivered rank and the number it was about to
                # take goes to the next survivor instead.
                logger.warning(
                    "design %s: PDB read failed (%s) — skipping", d["name"], exc)
                continue

            # PLACED BETWEEN THE READ AND THE UPLOAD, which is stronger than
            # what it inherited. The comment below describes escaping the
            # upload's try; on this branch the read and the upload are two
            # separate failures with two separate handlers, so this sits
            # outside BOTH. A numbering failure cannot cost a design here by
            # any route, and the bytes it hands on are the ones the upload
            # (or the inline rescue) will use.
            #
            # GENUINELY OUTSIDE THE UPLOAD'S FAILURE ACCOUNTING. The previous
            # version of this block carried that claim in a comment while
            # sitting INSIDE the upload's try, whose ``except Exception``
            # increments n_failures and drops the design — so a numbering
            # failure could cost a design that had already been paid for, which
            # is the exact opposite of what the comment promised. It now has its
            # own try, because ``restore_design_numbering`` never raises but the
            # decode/encode around it are outside that guarantee. Every path
            # here leaves ``pdb_bytes`` uploadable.
            if renumber_chains:
                try:
                    restored, rep = restore_design_numbering(
                        pdb_bytes.decode("latin-1"),
                        renumber_chains, renumber_reference)
                    if rep["applied"]:
                        # latin-1 round-trips all 256 byte values, so a design
                        # this step declines to change is uploaded byte-for-byte.
                        pdb_bytes = restored.encode("latin-1")
                        numbering = "input"
                    elif rep["already_input_numbering"]:
                        numbering = "input"
                    elif not renumber_warned:
                        renumber_warned = True
                        logger.warning(
                            "renumbering: designs ship in upstream's 1..N "
                            "numbering — %s", rep["reason"])
                except Exception as exc:  # noqa: BLE001 — never lose a design
                    # ``not_restored``, not the literal "upstream": this branch
                    # is inside ``if renumber_chains:`` and a reference only
                    # exists on a custom run, so the two are provably the same
                    # value here — spelling it once is what stops them drifting
                    # if that ever stops being true.
                    numbering = not_restored
                    if not renumber_warned:
                        renumber_warned = True
                        logger.warning(
                            "renumbering: failed (%s) — designs ship in "
                            "upstream's 1..N numbering", exc)

            upload_failed = False
            if upload_endpoint:
                try:
                    urls = request_upload_urls(upload_endpoint, job_token, [basename])
                    upload_pdb(urls[basename], pdb_bytes)
                except Exception as exc:
                    upload_failed = True
                    logger.warning(
                        "design %s: upload failed (%s) — falling back to "
                        "inline delivery", d["name"], exc,
                    )

            # AN UPLOAD FAILURE NO LONGER DESTROYS THE DESIGN.
            #
            # It used to share the read's ``except`` and take the same
            # count-and-skip, so a present-but-BROKEN endpoint — HTTP 401/404,
            # a revoked presigned URL, or an empty ``job_token`` (rfdiffusion
            # guards that explicitly; this file never did) — returned nothing
            # at all: every PUT raised, every design was dropped, and the
            # billed shard handed back an empty candidate list. The atoms were
            # in this process's memory the entire time.
            #
            # Every sibling ships them anyway. rfdiffusion keeps the candidate
            # when its PUT raises, appends the filename to ``failed_uploads``
            # and inlines ``pdb_content_b64``; the hub is already built for it,
            # and this is the ONE case where a ``designs/`` pdb_key and an
            # inline copy are both correct at once —
            # ``shared/jobs.py::_slim_result_for_persist`` strips the inline
            # copy from a Storage-backed candidate EXCEPT when its basename is
            # listed in ``failed_uploads``, precisely because then Storage does
            # not have it. Keeping the key is what makes the rescue reach the
            # browser: ``candidate_table.html`` takes the URL branch whenever a
            # pdb_key exists, and ``/api/jobs/<id>/pdb/<file>`` misses in
            # Storage and then falls back to the inline copy by basename.
            #
            # It is NOT "inline as a bonus". Nothing changes on a healthy web
            # job: no upload raised, so ``upload_failed`` is never True, no
            # candidate grows a b64 field, and ``failed_uploads`` is absent
            # from the result. The ~3.6 MB/child that reconcile_campaign_children
            # would pull is only ever paid by a run that would otherwise have
            # returned zero structures.
            #
            # The rescue is bounded by the SAME total budget inline mode uses,
            # so a broken endpoint cannot turn into an unbounded return value;
            # a design the budget cannot fit (or one that read as zero bytes)
            # falls back to the original drop-and-count, which is also what
            # keeps the delivered ranks dense across it.
            rescue_inline = False
            if upload_failed:
                if pdb_bytes and (inline_bytes_used + len(pdb_bytes)
                                  <= INLINE_PDB_TOTAL_CAP_BYTES):
                    rescue_inline = True
                else:
                    n_failures += 1
                    keep_scores(d, "upload_failed")
                    logger.warning(
                        "design %s: upload failed and its %d bytes do not fit "
                        "the inline rescue budget (used %d of %d) — skipping",
                        d["name"], len(pdb_bytes), inline_bytes_used,
                        INLINE_PDB_TOTAL_CAP_BYTES,
                    )
                    continue

            # A PDB THAT READS AS ZERO BYTES IS A FAILED DESIGN, not a delivered
            # one. ``read_bytes()`` on a truncated or not-yet-written file
            # returns b"" and raises nothing, so it slid past the except above;
            # the cap test ``inline_bytes_used + 0 <= CAP`` is then trivially
            # true, an EMPTY base64 string is attached, and ``n_inlined``
            # increments for a blob carrying no atoms. With every design empty
            # the shard reported COMPLETED, 8 designs, 0 failures, "8 inlined
            # (0.0 MB)" — a delivery of nothing, certified by the very counters
            # a caller reads instead of parsing logs, and with no `error` key to
            # contradict it. n_inlined=8 alongside inline_bytes_used=0 is an
            # impossible pair the post-loop verdict could not catch, because it
            # only fires when the CAP dropped designs.
            #
            # Routed to the same count-and-skip an unreadable PDB takes, because
            # it is the same thing: this design has no atoms. INLINE ONLY. On
            # the upload path the identical weakness is pre-existing (empty
            # bytes are PUT and counted) and fixing it here would change what
            # the production web tier reports — designs_completed and n_failures
            # both — for a shape no one has observed. That belongs in its own
            # change, deliberately made, not as a side effect of this one.
            if inline_pdbs and not pdb_bytes:
                n_failures += 1
                keep_scores(d, "pdb_empty")
                logger.warning(
                    "design %s: %s is empty (0 bytes), so there are no "
                    "coordinates to inline — skipping", d["name"], pdb_path,
                )
                continue

            scores = d["scores"]
            design_entry = {
                "rank": rank,
                "name": d["name"],
                "pdb_key": pdb_key,
                # "input" when the delivered file carries the residue numbers
                # the operator typed, "upstream" when they gave us a numbering
                # and it carries 1..N instead, "n/a" when there was no operator
                # numbering at all (a curated benchmark run). Recorded per
                # design so the answer is in the result rather than in a log
                # line nobody reads. See _TARGET_NUMBERING_VALUES.
                "target_numbering": numbering,
                # flat copies for the results template + classifiers
                "total_reward": scores.get("total_reward"),
                "af2_iptm": scores.get("af2_iptm"),
                "af2_plddt": scores.get("af2_plddt"),
                "rf3_score": scores.get("rf3_score"),
                "binder_scrmsd": scores.get("binder_scrmsd"),
                "cluster_id": scores.get("cluster_id"),
            }
            candidate_entry = {
                "rank": rank, "name": d["name"], "pdb_key": pdb_key,
                # ON THE CANDIDATE, NOT ONLY ON THE DESIGN. shared/jobs.py's
                # candidate_records prefers ``candidates`` over ``designs`` and
                # templates/tools/proteina_results.html reads ``candidates``
                # only, so a field written into out_designs alone is data no
                # operator can ever see. It was.
                "target_numbering": numbering,
                "scores": scores,
            }
            # Coordinates inline, under the SAME key PXDesign and BindCraft
            # emit, so a cross-generator consumer reads one field for all four
            # tools. The extension is already carried by pdb_key (".pdb"),
            # which is how PXDesign records it too — no extra field is invented.
            #
            # Reached only when there is no upload endpoint (see the delivery
            # gate above), so this is the design's ONLY copy, never a duplicate
            # of something already in Storage.
            #
            # ON `candidates` ONLY, and that placement is load-bearing rather
            # than stylistic. /tmp/smoke_results.json IS the persisted
            # job.result, and shared/jobs.py _slim_result_for_persist walks
            # result["candidates"] and nothing else. A copy parked on
            # result["designs"] would escape every size control the hub has and
            # put multi-MB of base64 through the single PostgREST UPDATE in
            # _cas_update, documented there to throw and strand the job in
            # "running" after a webhook that already returned 200.
            # shared/exports.py and shared/storage.py also read the inline copy
            # off candidates only.
            if inline_pdbs:
                if inline_bytes_used + len(pdb_bytes) <= INLINE_PDB_TOTAL_CAP_BYTES:
                    candidate_entry["pdb_content_b64"] = base64.b64encode(
                        pdb_bytes
                    ).decode("ascii")
                    inline_bytes_used += len(pdb_bytes)
                    n_inlined += 1
                else:
                    # NOT a failure: the design is real and its scores are
                    # delivered. It loses only its atoms, which are still in
                    # the raw archive. Counted and logged below rather than
                    # dropped quietly — a truncated result set that looks
                    # complete is the failure mode this whole file guards.
                    n_inline_capped += 1
                    # AND IT LOSES ITS pdb_key WITH THEM. INLINE ONLY.
                    #
                    # The candidate table's "does this row have a structure?"
                    # test is ``pdb_key or pdb_content_b64``
                    # (templates/components/candidate_table.html), and with a
                    # pdb_key present it takes the URL branch: a live View-3D
                    # button and a .pdb download link pointing at
                    # /api/jobs/<job>/pdb/<key>. In INLINE mode there is no
                    # Storage object behind that key — nothing was uploaded —
                    # and this candidate has no inline copy either, so the
                    # route's Storage lookup misses, its b64 fallback finds an
                    # empty field, and both controls resolve to a 404. The key
                    # is a promise nothing can keep, and an em-dash ("no
                    # structure for this design") is the honest rendering.
                    #
                    # Dropped from the designs row too so the pair cannot
                    # disagree: every other consumer of a proteina result
                    # reads one list or the other, and a dangling pointer left
                    # in ``designs`` would be the same lie in a second place.
                    #
                    # The upload path never reaches here (``inline_pdbs`` is
                    # False whenever an endpoint exists), and there the key IS
                    # backed by a real object that the PUT already wrote — so
                    # it must keep it, untouched.
                    candidate_entry.pop("pdb_key", None)
                    design_entry.pop("pdb_key", None)
            elif rescue_inline:
                # ``elif``, and it can only ever be the other branch: the
                # rescue needs an upload endpoint and ``inline_pdbs`` is False
                # whenever one exists.
                #
                # The ``designs/`` pdb_key STAYS, unlike the cap branch above.
                # There the key promised bytes that exist nowhere; here it is
                # the key ``failed_uploads`` is matched on, and dropping it
                # would make _slim_result_for_persist treat the candidate as
                # not-Storage-backed, the basename lookup in the PDB route miss
                # its target, and the export lose the pointer — for a
                # candidate that IS carrying its atoms.
                candidate_entry["pdb_content_b64"] = base64.b64encode(
                    pdb_bytes
                ).decode("ascii")
                inline_bytes_used += len(pdb_bytes)
                failed_uploads.append(basename)
            # The design survived every drop, so the number it was assigned is
            # now spent. Committing here rather than at the top of the loop is
            # what keeps the delivered ranks contiguous no matter WHICH drop
            # fired — a design that lost its atoms to the cap is still
            # delivered (it keeps its rank, its name and its full scores; only
            # the atoms and the pointer that promised them are gone), so it
            # rightly consumes a rank; the three ``continue``s above do not.
            emitted_rank = rank
            out_designs.append(design_entry)
            out_candidates.append(candidate_entry)
            # Heartbeat new_candidate keys match webhook _sanitize_candidate.
            #
            # pdb_key is read off the CANDIDATE, not off the local, so a design
            # the inline cap stripped it from cannot announce it here either:
            # the live status page renders its View-3D control straight from
            # the heartbeat, before any result exists, so announcing a key the
            # result will not carry just moves the dead control earlier in the
            # run. ``_sanitize_candidate`` already does ``cand.get("pdb_key")``
            # and stores None, which the results renderer hides.
            #
            # A no-op on the upload path: ``inline_pdbs`` is False there, the
            # pop never fires, and this reads back exactly ``pdb_key``.
            #
            # KNOWN GAP, NOT FIXED: the streamed copy of ``target_numbering``
            # has no reader today. ``_sanitize_candidate`` preserves it, but
            # ``shared/job_recovery.py::_candidate_from_partial`` — the path a
            # job finalised from streamed heartbeats goes through — does not
            # carry it, so such a job renders NO numbering line rather than a
            # wrong one. Silent, not false, which is the right failure
            # direction; it costs the operator a sentence, never a guarantee.
            send_heartbeat(
                webhook_url, job_id, stage="searching",
                designs_completed=len(out_designs), designs_total=designs_total,
                new_candidate={
                    "rank": rank,
                    "name": d["name"],
                    "pdb_key": candidate_entry.get("pdb_key"),
                    "target_numbering": numbering,
                    "total_reward": scores.get("total_reward"),
                    "af2_iptm": scores.get("af2_iptm"),
                    "af2_plddt": scores.get("af2_plddt"),
                    "rf3_score": scores.get("rf3_score"),
                    "binder_scrmsd": scores.get("binder_scrmsd"),
                    "cluster_id": scores.get("cluster_id"),
                },
            )
            logger.info("  -> rank %d reward=%s pdb=%s", rank, scores.get("total_reward"),
                        candidate_entry.get("pdb_key") or "(capped, no atoms)")

        runtime = int(time.time() - start)
        result = {
            "status": "COMPLETED",
            "tier": preset,
            "designs_total": designs_total,
            "designs_completed": len(out_designs),
            "n_failures": n_failures,
            "designs": out_designs,
            "candidates": out_candidates,
            "runtime_seconds": runtime,
            "provider_job_id": job_id,
        }
        # Present only when a PUT actually raised. Its absence is the normal
        # web result and the only shape a healthy job has ever returned; its
        # presence is what tells the hub not to slim the rescued atoms away.
        if failed_uploads:
            result["failed_uploads"] = failed_uploads

        # --- inline accounting (post-loop, sizes finally known) --------------
        # ADDED ONLY IN INLINE MODE, so the web path's result dict is exactly
        # what it was: same keys, same values, no `pdb_content_b64`, candidates
        # still {rank, name, pdb_key, scores}. Inline mode is unreachable when
        # an upload endpoint is present, so nothing a real web job returns can
        # grow a field here.
        #
        # These counters existed only inside logger calls, which no caller can
        # read: a shard that delivered ZERO of the coordinates it was asked for
        # still returned status COMPLETED with a full designs_completed count
        # and candidates whose bare-filename pdb_key is backed by nothing at
        # all. That is the billed-and-empty outcome
        # INLINE_PDB_MIN_USEFUL_CAP_BYTES claims to prevent and demonstrably
        # cannot: any cap between that floor and one real PDB clears it.
        if inline_pdbs:
            result["inline_delivery"] = {
                "n_inlined": n_inlined,
                "n_inline_capped": n_inline_capped,
                "inline_bytes_used": inline_bytes_used,
                "cap_bytes": INLINE_PDB_TOTAL_CAP_BYTES,
            }
            # THE BANKED SCORES, ACTUALLY HANDED BACK. ``keep_scores`` above
            # collects every dropped design's A100-computed scores, and until
            # this line that list was built and then discarded when main()
            # returned — the local went out of scope, the result never grew a
            # key, and the drop destroyed the scores exactly as before. A
            # shard that scored 8 and matched 6 PDBs still handed back six
            # designs and no way to learn that the best-scoring row was one of
            # the two that went missing, which is the whole defect the helper
            # was written for.
            #
            # It is its own list rather than score-only candidates on purpose;
            # see ``undelivered``'s declaration for why (the upload path's
            # failure arithmetic, and delivery_verdict's reading of
            # ``candidates`` as "delivered, WITH atoms").
            #
            # Always present in inline mode, like ``inline_delivery``: an
            # empty list is the readable statement that nothing was dropped,
            # where a missing key is indistinguishable from an older shard.
            result["undelivered"] = undelivered

        # --- THE DELIVERY VERDICT (one question, asked once) -----------------
        # This used to live inline above and ask only "did the size cap drop
        # every design?", which is one of at least four ways this shard can
        # deliver nothing and say COMPLETED. See ``delivery_verdict`` for the
        # question it asks instead and for what is deliberately NOT a failure.
        #
        # ``n_structures`` is the count of coordinates that actually left the
        # container, and it is mode-dependent because the evidence is: inline,
        # only an INLINED candidate carries atoms (a cap-dropped one has scores
        # and a pdb_key and no bytes); on the upload path a candidate exists
        # only after its PUT returned, so the candidate is the evidence.
        #
        # ``n_scored_delivered`` counts the DELIVERED candidates, not the
        # parsed rows, because this is a verdict on delivery: a run whose only
        # scored rows were all dropped hands the caller an unrankable set even
        # though ``n_scored`` above is nonzero.
        n_structures = n_inlined if inline_pdbs else len(out_candidates)
        n_scored_delivered = sum(
            1 for c in out_candidates
            if c["scores"].get("total_reward") is not None
        )
        census = census_output_tree(run_dir)
        error = delivery_verdict(
            n_parsed=len(designs),
            n_delivered=len(out_candidates),
            n_structures=n_structures,
            n_scored_delivered=n_scored_delivered,
            n_inline_capped=n_inline_capped,
            n_failures=n_failures,
            inline_pdbs=inline_pdbs,
            census=census,
        )

        # THE SEARCH'S EXIT CODE BELONGS IN THE RESULT, not only in a log line.
        # `complexa design` chains generate -> filter -> evaluate -> analyze and
        # a late stage can die AFTER a complete reward CSV is written (P-3
        # canary: 8 designs fully RF3-scored, then exit 1). Delivering those is
        # right — see the guard above — but the crash was then recorded in a
        # logger.warning only, and container logs are not part of what a direct
        # caller or the hub receives. A half-length shard was therefore
        # indistinguishable from one the filter had legitimately culled down.
        # BindCraft states the same thing as ``partial`` beside a subprocess
        # status block; this mirrors it. Both keys appear ONLY when rc != 0, so
        # a clean run's result — on either path — is unchanged.
        if rc != 0:
            result["partial"] = True
            result["search"] = {
                "exit_code": rc,
                "n_parsed": len(designs),
                "n_scored": n_scored,
            }
            if search_timeout_s is not None:
                # BindCraft records the same pair (``status: "timeout"`` beside
                # ``timeout_s``). A bare exit code of 124 is indistinguishable
                # from a search that genuinely exited 124, and the two call for
                # different responses.
                result["search"]["status"] = "timeout"
                result["search"]["timeout_s"] = search_timeout_s

        # The census rides along whenever something is off — a nonzero exit or
        # a failed verdict — and never on a clean run, which has nothing to
        # diagnose and whose result shape is load-bearing on the web path.
        delivery_failed = error is not None
        if delivery_failed or rc != 0:
            result["output_census"] = census
        if delivery_failed:
            # Scores, ranks and the candidate list are all KEPT on the failed
            # result — the science survives, and the atoms are in the raw
            # archive — so this reports a delivery failure without destroying
            # what the run did produce.
            logger.error(
                "pipeline FAILED at %s/%s: %s",
                error["bucket"], error["check"], error["detail"],
            )
            result["status"] = "FAILED"
            result["error"] = error

        _write_result(result)
        send_heartbeat(
            webhook_url, job_id, stage="complete",
            designs_completed=len(out_designs), designs_total=designs_total,
        )
        if n_inline_capped:
            logger.warning(
                "inline PDB cap reached: %d design(s) carry scores but NO "
                "coordinates (cap %d bytes, used %d). Their atoms are in the "
                "raw archive. Raise PROTEINA_INLINE_PDB_CAP_BYTES to inline them.",
                n_inline_capped, INLINE_PDB_TOTAL_CAP_BYTES, inline_bytes_used,
            )
        if failed_uploads:
            logger.warning(
                "%d design(s) could not be uploaded to Storage and are being "
                "returned INLINE instead (%s). The upload endpoint or the "
                "job_token is broken; the coordinates are still in this "
                "result and _slim_result_for_persist will keep them.",
                len(failed_uploads), " ".join(failed_uploads),
            )
        logger.info(
            "shard complete — %d/%d designs, %d failures, %d inlined "
            "(%.1f MB b64), %d over cap, runtime=%ds",
            len(out_designs), designs_total, n_failures, n_inlined,
            inline_bytes_used * 4 / 3 / 1e6, n_inline_capped, runtime,
        )
        if delivery_failed:
            # After the result is written and the logs are out, matching
            # _fail's exit code so a delivery failure is not the one failure
            # mode that returns 0. The `finally` below still tars the raw
            # outputs — which is where the undelivered atoms are.
            sys.exit(1)
    finally:
        # NOT gated on rc, on survivors, or on what got uploaded. A shard whose
        # reward CSV went missing returns [] from parse_designs and completes as a
        # silent zero-candidate "success" having shipped nothing — that is
        # precisely the run whose tree you need to read afterwards.
        archive_raw_outputs(run_dir)


def main() -> None:
    """EVERY EXIT PATH WRITES A RESULT FILE. That is this function's whole job.

    ``_run_shard`` had no ``except`` arm of its own, so anything it did not
    anticipate — the wrong TYPE in a job_spec field, an OSError from the run
    dir, a KeyError in a parser — left the interpreter with a bare traceback on
    stderr and NO /tmp/smoke_results.json. ``modal_app.run_tool`` then hits
    FileNotFoundError, passes, and returns ``smoke_result: None`` with an empty
    ``stdout_tail`` and an empty ``stderr_tail``, which is the entire diagnosis
    a direct caller receives for a fully billed A100. It is not hypothetical:
    ``job_spec.binder_length = 90`` — a scalar where ``direct_call_fc`` documents
    the ``[lo, hi]`` pair — raises TypeError out of the list comprehension near
    the top and produced exactly that, silently.

    It also guarantees the result file belongs to THIS run: the first thing it
    does is unlink whatever a previous shard left on this warm container and
    drop a ``did_not_complete`` placeholder in its place, so a kill that skips
    every ``except`` cannot hand the caller someone else's designs. See
    ``_reset_result_file``, including why that placeholder must not go through
    ``_write_result``.

    BindCraft guarantees a structured failure on every path and this mirrors it.
    Three things it deliberately does NOT do:

      * It does not swallow ``SystemExit``. ``_fail`` and the delivery-failure
        exit have already written their result and chosen exit code 1;
        re-wrapping them would only lose that.
      * It does not overwrite a result that was already written. The tail of a
        successful shard (heartbeat, log lines) runs after ``_write_result``,
        and replacing a COMPLETED result carrying every design with an
        ``unhandled_exception`` stub would destroy the run rather than report
        it. ``_RESULT_WRITTEN`` is what makes that decidable.
      * It does not exit 0. ``run_tool`` copies ``result.returncode`` straight
        into what the caller receives, so a crash that exited 0 would be a
        failed run reported as a clean one to anything branching on it.
    """
    global _RESULT_WRITTEN
    # Reset rather than relying on the module default: one process runs one
    # shard, but the test suite drives main() many times in a single
    # interpreter, and a flag left True by an earlier run would suppress the
    # very write this function exists to guarantee.
    _RESULT_WRITTEN = False
    # The on-disk half the flag cannot reach: a result file left by a PREVIOUS
    # shard on this warm container. Must run before _run_shard, and must not
    # set _RESULT_WRITTEN — see _reset_result_file.
    _reset_result_file()
    try:
        _run_shard()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — the point is that it is total
        logger.exception("UNHANDLED exception in the proteina shard")
        if not _RESULT_WRITTEN:
            _write_result({
                "status": "FAILED",
                "error": {
                    "bucket": "internal",
                    "check": "unhandled_exception",
                    "detail": (
                        f"the shard crashed with an unhandled "
                        f"{type(exc).__name__}: {exc}. This is a bug in "
                        "run_pipeline.py, not a bad request — check the "
                        "traceback below and the container logs. Any output "
                        "the run produced before the crash is in the raw "
                        "archive."
                    ),
                    # Tail, not head: the frames nearest the raise are the ones
                    # that name the failing line, and the result is persisted
                    # into a JSONB column that must not grow without bound.
                    "traceback": traceback.format_exc()[-4000:],
                },
                "tier": os.environ.get("JOB_TIER", ""),
                "provider_job_id": os.environ.get("JOB_ID", ""),
            })
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
