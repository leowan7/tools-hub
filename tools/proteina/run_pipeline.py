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

import csv
import glob
import hashlib
import json
import logging
import os
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
    convert to an int. Columns 22:26 overflow at residue numbers >= 10000, and
    a silently-skipped residue there could make a legitimate hotspot look
    missing — so the count is surfaced in the failure message rather than
    swallowed.
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
    """
    return [(c, lo, hi) for c, lo, hi in segments if lo < 0 or hi < 0]


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
        segments = []
        for chain, lo, hi in raw_segments:
            if lo is None:
                nums = [r[1] for r in residues if r[0] == chain]
                if not nums:
                    _fail(
                        "input", "target_input",
                        f"chain {chain} is not present in the uploaded target. "
                        f"It contains: {spans}.",
                    )
                segments.append((chain, min(nums), max(nums)))
            else:
                segments.append((chain, lo, hi))
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
    for chain, lo, hi in segments:
        picked = select_residues(residues, [(chain, lo, hi)])
        if not picked:
            _fail(
                "input", "target_input",
                f"chain {chain} residues {lo}-{hi} select 0 residues in the "
                f"uploaded target. It contains: {spans}.",
            )

    selected = select_residues(residues, segments)
    logger.info(
        "custom target: selected %d of %d residues (%s); chains present: %s",
        len(selected), len(residues), format_contig(segments), spans,
    )
    if len(selected) < 20:
        _fail(
            "input", "target_input",
            f"the selected target region has only {len(selected)} residues, "
            "which is too small to design a binder against. Widen the chain "
            f"range. The target contains: {spans}.",
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
        _fail(
            "input", "hotspot_missing",
            f"hotspot residue(s) {', '.join(missing)} are not in the selected "
            f"region of the uploaded target ({format_contig(segments)}). The "
            f"target contains: {spans}. Hotspots are chain-prefixed and "
            "case-sensitive, in original PDB numbering (e.g. A45)."
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
    incoming.replace(staged)
    record["target_path"] = str(staged)

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
# The real working set was ~6.1 GB, and the two runs agreed to within 24 MB
# because a CONSTANT dominated the reading — not because the workload was flat
# in target size. Any envelope derived from those numbers is arithmetic on an
# allocator policy. See shared/pdb_preflight_rules.py::_PROTEINA.
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
    target_chain = str(job_spec.get("target_chain") or "")
    target_input = str(job_spec.get("target_input") or "")
    hotspot_spec = [str(h) for h in (job_spec.get("hotspot_spec") or [])]
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
    if not upload_endpoint:
        _fail("preflight", "upload_urls_endpoint", "upload_urls_endpoint missing from payload")

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

        # --- upload + stream each design -------------------------------------
        out_designs: list[dict] = []
        out_candidates: list[dict] = []
        n_failures = 0
        n_rows = len(designs)
        for d in designs:
            rank = d["rank"]
            pdb_path = find_pdb_for(d, run_dir, d["_row_index"], n_rows)
            if pdb_path is None:
                n_failures += 1
                logger.warning("design rank %d: no PDB file matched — skipping", rank)
                continue
            basename = f"design_{rank:03d}.pdb"
            pdb_key = f"designs/{basename}"
            try:
                pdb_bytes = pdb_path.read_bytes()
                urls = request_upload_urls(upload_endpoint, job_token, [basename])
                upload_pdb(urls[basename], pdb_bytes)
            except Exception as exc:
                n_failures += 1
                logger.warning("design rank %d: upload failed (%s) — skipping", rank, exc)
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
            out_designs.append(design_entry)
            out_candidates.append(
                {"rank": rank, "name": d["name"], "pdb_key": pdb_key, "scores": scores}
            )
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
        logger.info(
            "shard complete — %d/%d designs, %d failures, runtime=%ds",
            len(out_designs), designs_total, n_failures, runtime,
        )
    finally:
        # NOT gated on rc, on survivors, or on what got uploaded. A shard whose
        # reward CSV went missing returns [] from parse_designs and completes as a
        # silent zero-candidate "success" having shipped nothing — that is
        # precisely the run whose tree you need to read afterwards.
        archive_raw_outputs(run_dir)


if __name__ == "__main__":
    main()
