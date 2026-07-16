"""Proteina-Complexa — de novo binder design via inference-time search.

Modal app: ``ranomics-proteina-prod``. GPU: A100-80GB. Campaign tool.

Proteina-Complexa (NVIDIA-BioNeMo, NVIDIA Open Model License) is a
flow-matching generator wrapped in an inference-time search that filters
candidates through an AF2 / RF3 / force-field reward stack. It designs de
novo binders against a **protein** target (PDB), a **small-molecule**
target (SDF), or an enzyme/motif scaffold (AME).

Runs as a fund-and-drain compute campaign of independent search shards
(see ``shared/compute_campaigns.py``), NOT a single giant job. One shard is
one seeded ``proteinfoundation.generate`` run on one A100-80GB; every shard
gets a distinct ``++seed`` (generate.py: ``seed = cfg.seed + job_id`` makes
them independent), and the hub does global cross-shard top-K + post-hoc
diversity. ``num_designs`` scales the SHARD COUNT, not the width/depth inside
a shard — the generation profile per shard is fixed (see ``_SHARD_*`` below)
so every shard deterministically yields ``_SHARD_DESIGNS`` designs, which is
exactly the campaign ``chunk_size`` (``_CHUNK_SIZE_OVERRIDE["proteina"]``).

The four presets are the **model variants**, not a pilot/full tier — each
maps to a checked-in pipeline config + checkpoint pair. ``preset`` becomes
``campaign.preset`` and selects the container's variant:

- ``protein_binder`` — binder vs a protein target
  (``search_binder_local_pipeline``, AF2 reward).
- ``ligand_binder``  — binder vs a small-molecule target (SDF)
  (``search_ligand_binder_local_pipeline``, RF3 reward; no AF2 fallback).
- ``motif_ame``      — motif scaffolding / enzyme active-site (AME)
  (``search_ame_local_pipeline``, RF3 reward; no AF2 fallback).
- ``validate``       — the free CPU-only ``complexa validate`` pre-flight
  gate (the staging smoke; no GPU, no wallet).

Target model: each variant runs a ``task_name`` (a repo-bundled benchmark
task whose target is baked into the config) OR a caller-uploaded custom
target (PDB for protein/motif, SDF for ligand), staged by the campaign
route. The curated tasks are the canary/demo path; bring-your-own targets
are the general path.

RF3 dependency (product decision): ``ligand_binder`` and ``motif_ame`` score
on RF3 only — AF2RewardModel has no ligand protocol, so there is no AF2
fallback. If the operator turns RF3 off (the ``PROTEINA_RF3`` kill-switch),
these two variants have no valid reward and are hard-blocked in the pipeline
preflight (``rf3_required`` below flags them for the container). RF3 ships on,
so this is a defensive degraded-path guard, not a normal-operation branch.

BUILD-TIME VERIFICATION (pinned commit + canary P-1/P-2/P-3): the exact
``task_name`` set per config, the SDF -> chain-A HETATM+CONECT PDB conversion
(done in-container via RDKit, which is not in the web tier), and the reward
score keys the results viewer surfaces.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Variant registry. preset slug -> Hydra --config-name (None = free validate).
# ---------------------------------------------------------------------------

_PRESET_CONFIG: dict[str, Optional[str]] = {
    "protein_binder": "search_binder_local_pipeline",
    "ligand_binder": "search_ligand_binder_local_pipeline",
    "motif_ame": "search_ame_local_pipeline",
    "validate": None,
}
_PRESETS = tuple(_PRESET_CONFIG.keys())

# Default curated benchmark task per design variant (the canary targets). A
# blank ``task_name`` field falls back to these so the demo path needs no
# upload. Verify each against the pinned repo's configs at build time.
_DEFAULT_TASK: dict[str, str] = {
    "protein_binder": "02_PDL1",
    "ligand_binder": "39_7V11_LIGAND",
    "motif_ame": "01_AME",
}

# Variants whose reward stack is RF3-only (no AF2 ligand protocol). The
# container hard-blocks these when PROTEINA_RF3 is off; surfaced here so the
# adapter can stamp the flag onto the job_spec.
_RF3_REQUIRED = {"ligand_binder", "motif_ame"}

# Variants that take a protein target chain (protein PDB / motif PDB). The
# ligand variant's target is an SDF, which has no chain.
_CHAIN_PRESETS = {"protein_binder", "motif_ame"}

# ---------------------------------------------------------------------------
# Fixed per-shard generation profile. Locked (not user-tunable) so every shard
# yields exactly _SHARD_DESIGNS designs == the campaign chunk_size; the user's
# scale knob is num_designs (the shard count). Exposing nsamples/replicas would
# desync the chunk math (chunk_size is pinned to 8 in _CHUNK_SIZE_OVERRIDE).
# Values are the upstream protein-variant defaults: designs/shard =
# nsamples x nrepeat_per_sample x best_of_n.replicas = 4 x 1 x 2 = 8.
# ---------------------------------------------------------------------------

_SHARD_NSAMPLES = 4      # generation.dataloader.dataset.nres.nsamples
_SHARD_REPLICAS = 2      # generation.search.best_of_n.replicas
_SHARD_NSTEPS = 400      # generation.args.nsteps (upstream default)
_SHARD_DESIGNS = _SHARD_NSAMPLES * _SHARD_REPLICAS  # == _CHUNK_SIZE_OVERRIDE

# task_name is a Hydra ++generation.task_name selector resolved against the
# repo configs in-container; bound it to a safe token here and let the pipeline
# preflight reject an unknown name before GPU spend.
_TASK_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _describe(preset: str, task_name: str, target_chain: str) -> str:
    if preset == "ligand_binder":
        return f"ligand binder vs {task_name or 'uploaded SDF'}"
    if preset == "motif_ame":
        return f"motif/enzyme scaffold ({task_name or 'uploaded motif'})"
    if preset == "validate":
        return "free validate dry-run"
    where = task_name or "uploaded target"
    chain = f" chain {target_chain}" if target_chain else ""
    return f"protein binder vs {where}{chain}"


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce the Proteina form into a shard job_spec.

    ``num_designs`` (the campaign scale) is NOT validated here — the campaign
    route injects a placeholder count and the driver sets the real per-chunk
    value. This validates the per-shard params only. The target file (PDB/SDF)
    is optional and handled by the campaign route (curated task vs custom
    upload); this only sanity-checks the task selector + target chain.
    """
    preset = (form.get("preset") or "protein_binder").strip()
    if preset not in _PRESET_CONFIG:
        return None, "Pick a design variant."

    task_name = (form.get("task_name") or "").strip() or _DEFAULT_TASK.get(preset, "")
    if preset != "validate":
        if not task_name:
            return None, "Choose a target task or upload a custom target."
        if not _TASK_RE.match(task_name):
            return None, (
                "Target task name may use letters, digits, underscore and "
                "hyphen (max 64 characters)."
            )

    target_chain = ""
    if preset in _CHAIN_PRESETS:
        target_chain = (form.get("target_chain") or "A").strip()
        if not target_chain or len(target_chain) > 4:
            return None, "Target chain ID is required (max 4 characters)."

    return (
        {
            "preset": preset,
            "config_name": _PRESET_CONFIG[preset],
            "task_name": task_name,
            "target_chain": target_chain,
            # RF3-only reward -> the container hard-blocks these when RF3 is off.
            "rf3_required": preset in _RF3_REQUIRED,
            # Locked generation profile (see _SHARD_*). run_pipeline reads these
            # for the Hydra overrides; keeping them here (not on the form) is what
            # guarantees designs/shard == chunk_size for the wallet math.
            "nsamples": _SHARD_NSAMPLES,
            "replicas": _SHARD_REPLICAS,
            "nsteps": _SHARD_NSTEPS,
            "designs_per_shard": _SHARD_DESIGNS,
            "target": _describe(preset, task_name, target_chain),
            "parameters": {"n_designs_total": _SHARD_DESIGNS},
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Proteina shard job_spec.

    A custom target's presigned URL is forwarded by the campaign dispatch path
    (from ``campaign.target_storage_path``), not embedded here — matches boltz2
    / boltzgen / iggm. A curated-task run carries no target file; the container
    resolves the target from the repo config for ``task_name``.
    """
    return {
        "preset": inputs["preset"],
        "config_name": inputs["config_name"],
        "task_name": inputs["task_name"],
        "target_chain": inputs["target_chain"],
        "rf3_required": inputs["rf3_required"],
        "nsamples": inputs["nsamples"],
        "replicas": inputs["replicas"],
        "nsteps": inputs["nsteps"],
        "parameters": inputs["parameters"],
    }


adapter = ToolAdapter(
    slug="proteina",
    label="Proteina-Complexa",
    blurb=(
        "De novo binder design against protein or small-molecule targets, "
        "run as an inference-time search filtered by an AF2 / RF3 / "
        "force-field reward stack. Fans out as a fund-and-drain campaign of "
        "independent search shards; the wallet balance is the only ceiling."
    ),
    presets=(
        Preset(
            slug="protein_binder",
            label="Protein binder (de novo, vs a protein target)",
            description=(
                "Design de novo binders against a protein target. Search is "
                "scored by AlphaFold2 confidence plus a force-field reward. "
                "Pick a curated target task or upload your own target PDB."
            ),
            requires_pdb=False,
            long_running=True,
        ),
        Preset(
            slug="ligand_binder",
            label="Ligand binder (de novo, vs a small molecule)",
            description=(
                "Design de novo binders against a small-molecule target "
                "supplied as an SDF. Scored by the RoseTTAFold3 reward "
                "(the force field does not support protein-ligand complexes). "
                "Pick a curated ligand task or upload your own SDF."
            ),
            requires_pdb=False,
            long_running=True,
        ),
        Preset(
            slug="motif_ame",
            label="Motif scaffolding / enzyme (AME)",
            description=(
                "Scaffold a functional motif or enzyme active site. Scored by "
                "the RoseTTAFold3 reward. Pick a curated AME task or upload "
                "your own motif."
            ),
            requires_pdb=False,
            long_running=True,
        ),
        Preset(
            slug="validate",
            label="Validate (free dry-run)",
            description=(
                "Free CPU-only pre-flight that checks your target + config "
                "load before you commit GPU to a paid search. No wallet charge."
            ),
            requires_pdb=False,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    # Target is OPTIONAL (curated task vs custom upload) and the SDF path is
    # ligand-only, so the campaign route handles staging, not the generic
    # requires_pdb upload plumbing.
    requires_pdb=False,
    form_template="tools/proteina_form.html",
    results_partial="tools/proteina_results.html",
)

register(adapter)
