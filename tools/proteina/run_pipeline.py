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
#   INLINE  (endpoint absent) — a direct `modal.Function.from_name(...)` call,
#           which is the only way to pass a multi-chain target and
#           chain-prefixed hotspots (the web tier cannot express either). There
#           is no tools-hub server to call back to and no job_token to
#           authenticate with, so the atoms travel in the return value as
#           base64 under `pdb_content_b64` — the same field name and encoding
#           PXDesign and BindCraft already emit, so a cross-generator consumer
#           needs no per-tool special-casing.
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
# Smallest cap worth starting a run for. `> 0` was too weak a test: a cap of 1
# byte passes it, admits no design, and still spends the A100 to return
# COMPLETED with zero structures — the same failure as a cap of 0, with a
# narrower trigger. A single-design PDB is tens of KB at minimum.
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
# canary reward CSVs @916eaaed. Each display key lists the real upstream columns
# for BOTH variants; the tolerant _pick takes the first that exists (unmatched ->
# None -> hidden by the renderer).
#   protein_binder reward = af2folding_* (AF2 refold); total_reward == -i_pae.
#   ligand_binder  reward = rf3folding_* (RF3 fold);   ranking_score is the summary.
_SCORE_COLUMNS: dict[str, tuple[str, ...]] = {
    "total_reward": ("total_reward",),
    # interface pTM: raw for ligand (rf3folding_ipTM), log-only for protein.
    "af2_iptm": ("rf3folding_ipTM", "af2folding_i_ptm_log", "af2_iptm", "iptm"),
    # pLDDT (0-1): af2folding_plddt (protein) / rf3folding_plddt (ligand).
    "af2_plddt": ("af2folding_plddt", "rf3folding_plddt", "af2_plddt", "plddt"),
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


def _write_result(payload: dict[str, Any]) -> None:
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except OSError as exc:
        logger.error("Could not write %s: %s", SMOKE_RESULTS_PATH, exc)


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


def normalize_hotspots(job_spec: dict) -> list[str]:
    """Resolve hotspot tokens from either this tool's name or the shared one.

    Proteina's native key is ``hotspot_spec``; the campaign side and the other
    three generators send ``hotspot_residues``. Native wins when both are
    present so nothing already in flight changes meaning. A plain string is
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
    raw = job_spec.get("hotspot_spec")
    if raw is None or raw == []:
        raw = job_spec.get("hotspot_residues")
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [t for t in re.split(r"[,\s]+", raw.strip()) if t]
    else:
        items = [str(h).strip() for h in raw if str(h).strip()]

    # Chains come from the UNION of both fields that can name one, because
    # either can be the one that actually decides. prepare_custom_target
    # derives its segments from target_input when that is present and ignores
    # target_chain entirely, so counting target_chain alone lets
    # {"target_chain": "A", "target_input": "A1-200,B1-200"} through: one
    # declared chain, two real ones, bare hotspots silently promoted to A and
    # the second protomer unconstrained. Reachable by exactly the direct
    # callers this delivery mode exists to serve.
    chains = normalize_target_chain(str(job_spec.get("target_chain") or "")).split()
    target_input = str(job_spec.get("target_input") or "").strip()
    if target_input:
        try:
            for chain, _lo, _hi in parse_target_input(target_input):
                if chain not in chains:
                    chains.append(chain)
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
    WEB route production was shielded from that by the adapter, which rejects a
    chain named twice ("Chain A appears more than once in the target chain
    range", ``tools/proteina/__init__.py``); ``prepare_custom_target`` itself
    was not, and the canary does not go through the adapter at all.
    ``target_too_small`` now counts THIS, which is also what the crop stages.

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
        segments = derive_segments(residues, requested_chains)
        if not segments:
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
            # disordered gap as well as an over-run bound, and always renders a
            # range whose two endpoints are really in the file.
            at_or_above = [n for n in nums if n >= lo] or [nums[-1]]
            at_or_below = [n for n in nums if n <= hi] or [nums[0]]
            fixes.append(f"{chain}{at_or_above[0]}-{at_or_below[-1]}")
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
    contig = format_contig(segments)
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


def run_streaming(cmd: list[str], cwd: Path) -> int:
    """Run a subprocess, live-streaming stdout/stderr to Modal logs (never
    capture_output for long GPU work, per the Modal-subprocess memory)."""
    logger.info("cmd (cwd=%s): %s", cwd, " ".join(cmd))
    result = subprocess.run(
        cmd, cwd=str(cwd), stdout=sys.stdout, stderr=sys.stderr, check=False,
        env=design_subprocess_env(),
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
    for rank, d in enumerate(parsed):
        d["rank"] = rank
    return parsed


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
# validate tier (free, CPU dry-run — the staging gate)
# ===========================================================================


def run_validate(config_dir: str, preset: str, task_name: str) -> None:
    """Free pre-flight / staging gate. Variant-agnostic checks, no GPU compute:
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


def main() -> None:
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
    target_chain = normalize_target_chain(str(job_spec.get("target_chain") or ""))
    target_input = str(job_spec.get("target_input") or "")
    try:
        hotspot_spec = normalize_hotspots(job_spec)
    except ValueError as exc:
        # Ambiguous bare-int hotspots on a multi-chain target. Pre-GPU, and it
        # has to be: the guess it refuses to make would pass every downstream
        # check and produce a run that looks correct.
        _fail("input", "hotspot_chain_ambiguous", str(exc))
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

    # --- validate tier: free CPU dry-run, no wallet, no GPU compute ----------
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
    if not upload_endpoint and not (inline_pdbs and cap_ok):
        # Every way a finished design could end up with nowhere to put its
        # coordinates, refused before the GPU rather than after. A cap of 0 is
        # the sharp edge here: "0" is truthy so it does not fall back to the
        # default, _inline_enabled() is still True, and without this clause the
        # run would spend an A100 and return COMPLETED with zero structures.
        reason = (
            "inline PDBs are disabled (PROTEINA_INLINE_PDBS=off)" if not _inline_enabled()
            else f"the inline PDB cap is {INLINE_PDB_TOTAL_CAP_BYTES} bytes, "
                 f"below the {INLINE_PDB_MIN_USEFUL_CAP_BYTES} needed to admit "
                 "even one design (PROTEINA_INLINE_PDB_CAP_BYTES)"
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

        try:
            rc = run_streaming(cmd, work_dir)
        except FileNotFoundError:
            _fail("search", "complexa", f"`{COMPLEXA_BIN}` binary not found on PATH")

        designs = parse_designs(run_dir)
        # `complexa design` chains generate -> filter -> evaluate -> analyze. A late
        # stage can exit nonzero AFTER the reward CSV (with complete scores) is
        # already written — observed on the ligand path (P-3 canary: 8 designs fully
        # RF3-scored, then exit 1). Cross-shard diversity is assigned at the hub, so
        # we still DELIVER designs that were fully scored; only fail when the nonzero
        # exit left nothing scored to deliver (a genuine early failure).
        n_scored = sum(1 for d in designs if d.get("total_reward") is not None)
        if rc != 0:
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
            # and delivered-only billing releases this shard's hold.
            runtime = int(time.time() - start)
            _write_result(
                {
                    "status": "COMPLETED",
                    "tier": preset,
                    "designs_total": designs_total,
                    "designs_completed": 0,
                    "n_failures": 0,
                    "designs": [],
                    "candidates": [],
                    "runtime_seconds": runtime,
                    "provider_job_id": job_id,
                }
            )
            send_heartbeat(webhook_url, job_id, stage="complete", designs_total=designs_total)
            logger.info("shard produced 0 survivors in %ds", runtime)
            return

        # --- upload and/or inline each design --------------------------------
        out_designs: list[dict] = []
        out_candidates: list[dict] = []
        n_failures = 0
        n_rows = len(designs)
        inline_bytes_used = 0
        n_inlined = 0
        n_inline_capped = 0
        for d in designs:
            rank = d["rank"]
            pdb_path = find_pdb_for(d, run_dir, d["_row_index"], n_rows)
            if pdb_path is None:
                n_failures += 1
                logger.warning("design rank %d: no PDB file matched — skipping", rank)
                continue
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
            try:
                pdb_bytes = pdb_path.read_bytes()
                # Guarded, not removed. With an endpoint this is the original
                # call pair on the original bytes inside the original try, so
                # the web path's behaviour — including counting an upload
                # failure and skipping the design — is untouched.
                if upload_endpoint:
                    urls = request_upload_urls(upload_endpoint, job_token, [basename])
                    upload_pdb(urls[basename], pdb_bytes)
            except Exception as exc:
                n_failures += 1
                logger.warning(
                    "design rank %d: %s failed (%s) — skipping",
                    rank, "upload" if upload_endpoint else "PDB read", exc,
                )
                continue

            scores = d["scores"]
            design_entry = {
                "rank": rank,
                "name": d["name"],
                "pdb_key": pdb_key,
                # flat copies for the results template + classifiers
                "total_reward": scores.get("total_reward"),
                "af2_iptm": scores.get("af2_iptm"),
                "af2_plddt": scores.get("af2_plddt"),
                "rf3_score": scores.get("rf3_score"),
                "binder_scrmsd": scores.get("binder_scrmsd"),
                "cluster_id": scores.get("cluster_id"),
            }
            candidate_entry = {
                "rank": rank, "name": d["name"], "pdb_key": pdb_key, "scores": scores,
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
            out_designs.append(design_entry)
            out_candidates.append(candidate_entry)
            # Heartbeat new_candidate keys match webhook _sanitize_candidate.
            send_heartbeat(
                webhook_url, job_id, stage="searching",
                designs_completed=len(out_designs), designs_total=designs_total,
                new_candidate={
                    "rank": rank,
                    "name": d["name"],
                    "pdb_key": pdb_key,
                    "total_reward": scores.get("total_reward"),
                    "af2_iptm": scores.get("af2_iptm"),
                    "af2_plddt": scores.get("af2_plddt"),
                    "rf3_score": scores.get("rf3_score"),
                    "binder_scrmsd": scores.get("binder_scrmsd"),
                    "cluster_id": scores.get("cluster_id"),
                },
            )
            logger.info("  -> rank %d reward=%s pdb=%s", rank, scores.get("total_reward"), pdb_key)

        runtime = int(time.time() - start)
        _write_result(
            {
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
        )
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
        logger.info(
            "shard complete — %d/%d designs, %d failures, %d inlined "
            "(%.1f MB b64), %d over cap, runtime=%ds",
            len(out_designs), designs_total, n_failures, n_inlined,
            inline_bytes_used * 4 / 3 / 1e6, n_inline_capped, runtime,
        )
    finally:
        # NOT gated on rc, on survivors, or on what got uploaded. A shard whose
        # reward CSV went missing returns [] from parse_designs and completes as a
        # silent zero-candidate "success" having shipped nothing — that is
        # precisely the run whose tree you need to read afterwards.
        archive_raw_outputs(run_dir)


if __name__ == "__main__":
    main()
