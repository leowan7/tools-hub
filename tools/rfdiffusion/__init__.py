"""RFdiffusion tool adapter.

Kendrew Modal app: ``kendrew-rfdiffusion-prod``. GPU: A100-40GB.

Composite pipeline: RFdiffusion backbone generation + ProteinMPNN
sequence design + JAX AF2 multimer validation. Candidates carry real
ipTM / pLDDT / i_pAE statistics from the AF2 model.

Known-good on commit ``d83335c`` (Bug 8 unblock). Mini_pilot wallclock
~6 min on warm cache, ~17 min cold.

Smoke / mini_pilot tiers use the baked ``/opt/smoke_target.pdb`` (PD-L1
IgV) and ignore caller-supplied PDBs -- the form presents this as a
"reference-target demo" with no PDB upload. Pilot tier accepts a
caller-supplied target PDB plus hotspots and runs on the webhook flow
(~15-30 min on A100-40GB).

Note: at smoke tier the Kendrew pipeline sets ``skip_af2=True`` and
returns stub scores (``filter_status="stub (smoke)"``) -- the algorithm
runs end-to-end through RFdiffusion + ProteinMPNN but no real AF2
statistics are produced. The ``mini_pilot`` and ``pilot`` tiers run
the full composite with real AF2 multimer scoring.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register
from tools.rfdiffusion import meta as _meta  # noqa: F401 -- re-export for templates

# Re-export so callers can do ``from tools.rfdiffusion import paper_citation``.
paper_citation = _meta.paper_citation
paper_url = _meta.paper_url
github_url = _meta.github_url
comparison_one_liner = _meta.comparison_one_liner
example_output_id = _meta.example_output_id
preset_runtime_rows = _meta.preset_runtime_rows


def _parse_binder_length(form: Mapping[str, Any]) -> tuple[Optional[dict], Optional[str]]:
    """Coerce binder_length_min / binder_length_max into a {min, max} dict.

    Defaults to {min: 55, max: 65} -- mirrors the Kendrew mini_pilot_preset.
    Range bounds: 30-150 residues, min <= max.
    """
    raw_min = (form.get("binder_length_min") or "55").strip()
    raw_max = (form.get("binder_length_max") or "65").strip()
    try:
        bmin = int(raw_min)
        bmax = int(raw_max)
    except (TypeError, ValueError):
        return None, "Binder length must be integers."
    if bmin < 30 or bmax > 150:
        return None, "Binder length must be between 30 and 150 residues."
    if bmin > bmax:
        return None, "Binder length min must be <= max."
    return {"min": bmin, "max": bmax}, None


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the Kendrew RFdiffusion job_spec shape.

    Smoke / mini_pilot tiers ignore the caller's job_spec at the pipeline
    level (the Kendrew ``smoke_preset`` / ``mini_pilot_preset`` overrides
    everything), but the form still accepts ``binder_length`` so the
    cosmetic field appears in results metadata. Pilot tier requires the
    caller PDB, target chain, and hotspot residues.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"smoke", "mini_pilot", "pilot"}:
        return None, "Pick a preset."

    if preset in {"smoke", "mini_pilot"}:
        return (
            {
                "preset": preset,
                "target": "PD-L1 IgV (residues 18-132, chain A)",
            },
            None,
        )

    # pilot tier -- real target supplied by caller
    target_chain = (form.get("target_chain") or "A").strip()
    if not target_chain:
        return None, "Target chain is required."
    if len(target_chain) > 4:
        return None, "Target chain must be at most 4 characters."

    raw_hotspots = (form.get("hotspot_residues") or "").strip()
    if not raw_hotspots:
        return None, "At least one hotspot residue is required."
    try:
        hotspot_residues = [
            int(tok.strip()) for tok in raw_hotspots.split(",") if tok.strip()
        ]
    except ValueError:
        return None, "Hotspot residues must be comma-separated integers (e.g. 54,56,115)."
    if not hotspot_residues:
        return None, "At least one hotspot residue is required."

    binder_length, err = _parse_binder_length(form)
    if err:
        return None, err

    raw_num_designs = (form.get("num_designs") or "2").strip()
    try:
        num_designs = int(raw_num_designs)
    except (TypeError, ValueError):
        return None, "Number of designs must be an integer."
    if num_designs < 1 or num_designs > 5:
        return None, "Number of designs must be between 1 and 5."

    return (
        {
            "preset": preset,
            "target_chain": target_chain,
            "hotspot_residues": hotspot_residues,
            "binder_length": binder_length,
            "num_designs": num_designs,
            "target": f"Your uploaded PDB (chain {target_chain})",
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec that RFdiffusion's run_pipeline.py expects.

    Smoke / mini_pilot use the baked PD-L1 fixture; the pipeline ignores
    these caller fields and applies its own preset. The shape is sent
    anyway for forward-compat with future tier-aware handling. Pilot
    tier sends the caller target chain, hotspots, binder length range,
    and num_designs -- the presigned URL is forwarded separately by the
    generic submit route via ``input_pdb_url`` on the top-level payload.
    """
    preset = inputs["preset"]

    if preset in {"smoke", "mini_pilot"}:
        return {
            "target_chain": "A",
            "hotspot_residues": [],
            "parameters": {
                "num_designs": 1 if preset == "smoke" else 2,
                "diffusion_steps": 50,
                "skip_af2": preset == "smoke",
                "binder_length": {"min": 55, "max": 65},
            },
        }

    # pilot tier
    return {
        "target_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "parameters": {
            "num_designs": inputs["num_designs"],
            "diffusion_steps": 50,
            "skip_af2": False,
            "binder_length": inputs["binder_length"],
        },
    }


adapter = ToolAdapter(
    slug="rfdiffusion",
    label="RFdiffusion -- de novo binder design",
    blurb=(
        "Composite binder design: RFdiffusion backbones + ProteinMPNN "
        "sequences + AF2 multimer validation. Candidates carry real "
        "ipTM / pLDDT / i_pAE scores. GPU: A100-40GB. Mini_pilot ~7 min "
        "warm; pilot ~15-30 min on caller targets."
    ),
    presets=(
        Preset(
            slug="smoke",
            label="Smoke -- 1 design, 2 credits",
            credits_cost=2,
            description=(
                "Pipeline-shape check (~2 min). 1 candidate against PD-L1 "
                "reference, AF2 stubbed -- proves RFdiffusion + MPNN run, "
                "scores are placeholders."
            ),
        ),
        Preset(
            slug="mini_pilot",
            label="Preview -- 2 designs, 8 credits",
            credits_cost=8,
            description=(
                "Two ranked candidates against PD-L1 reference (~7 min). "
                "Full composite with real AF2 multimer scoring "
                "(ipTM / pLDDT / i_pAE)."
            ),
        ),
        Preset(
            slug="pilot",
            label="Pilot -- your target, ~30 min",
            credits_cost=15,
            description=(
                "Real RFdiffusion run against your uploaded target PDB "
                "with AF2 multimer validation. 1-5 candidates with real "
                "scores; results emailed when complete (~15-30 min on "
                "A100-40GB)."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/rfdiffusion_form.html",
    results_partial="tools/rfdiffusion_results.html",
)

register(adapter)
