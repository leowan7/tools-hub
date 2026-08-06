"""RFdiffusion tool adapter.

Modal app: ``ranomics-rfdiffusion-prod``. GPU: A100-40GB.

Composite pipeline: RFdiffusion backbone generation + ProteinMPNN
sequence design + JAX AF2 multimer validation. Candidates carry real
ipTM / pLDDT / i_pAE statistics from the AF2 model.

Known-good on commit ``d83335c`` (Bug 8 unblock). Pilot tier accepts a
caller-supplied target PDB plus hotspots and runs on the webhook flow
(~15-30 min on A100-40GB).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import (
    Preset,
    ToolAdapter,
    parse_hotspot_residues,
    parse_target_chains,
    register,
)
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

    Defaults to {min: 55, max: 65}. Range bounds: 30-150 residues,
    min <= max.
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

    Pilot tier requires the caller PDB, target chain, and hotspot
    residues.
    """
    preset = (form.get("preset") or "pilot").strip() or "pilot"
    if preset != "pilot":
        return None, "Pick a preset."

    # target_chain may name one chain ("A") or several ("A,B" / "A B"): a
    # multi-chain fixed target becomes a "/0 "-separated contig downstream.
    target_chain = (form.get("target_chain") or "A").strip()
    if not target_chain:
        return None, "Target chain is required."

    target_chains = parse_target_chains(target_chain)
    if not target_chains:
        return None, "Target chain is required."
    # Per TOKEN, not per string: a whole-string cap of 4 admitted "A,B" but
    # rejected "A,B,C", silently capping every target at two chains.
    for cid in target_chains:
        if len(cid) > 4:
            return None, f"Chain id {cid!r} is too long (max 4 characters)."

    hotspot_residues, err = parse_hotspot_residues(
        form.get("hotspot_residues") or "", target_chains
    )
    if err:
        return None, err

    binder_length, err = _parse_binder_length(form)
    if err:
        return None, err

    raw_num_designs = (form.get("num_designs") or "4").strip()
    try:
        num_designs = int(raw_num_designs)
    except (TypeError, ValueError):
        return None, "Number of designs must be an integer."
    # Tier-collapse PR: raised the per-job cap from 200 to 1000. The
    # wallet per-tool hard cap ($500 for rfdiffusion) blocks excessive
    # spend; this validator just enforces a sanity ceiling.
    if num_designs < 1 or num_designs > 1000:
        return None, "Number of designs must be between 1 and 1000."

    return (
        {
            "preset": preset,
            # Canonical comma form regardless of what the user typed:
            # both separators are accepted at this boundary, exactly one
            # is emitted, so no container has to guess.
            "target_chain": ",".join(target_chains),
            "hotspot_residues": hotspot_residues,
            "binder_length": binder_length,
            "num_designs": num_designs,
            "target": f"Your uploaded PDB (chain {target_chain})",
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec that RFdiffusion's run_pipeline.py expects.

    Pilot tier sends the caller target chain, hotspots, binder length
    range, and num_designs -- the presigned URL is forwarded separately
    by the generic submit route via ``input_pdb_url`` on the top-level
    payload.
    """
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
    label="RFdiffusion",
    blurb=(
        "De novo binder design. Composite pipeline combining RFdiffusion "
        "backbones, ProteinMPNN sequences, and AF2 multimer validation. "
        "Candidates carry real ipTM, pLDDT, and i_pAE scores. Pilot runs "
        "in roughly 15 to 30 min on caller targets."
    ),
    presets=(
        Preset(
            slug="pilot",
            label="Your target, ~30 min start to first results",
            description=(
                "Real RFdiffusion run against your uploaded target PDB "
                "with AF2 multimer validation. Pick 1 to 1000 candidates. "
                "Start with a small batch (4 designs, ~30 min) to "
                "confirm your target and hotspots, then scale to 100+ "
                "once the small batch looks reasonable. Results emailed "
                "when complete; A100-80GB."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    form_template="tools/rfdiffusion_form.html",
    results_partial="tools/rfdiffusion_results.html",
)

register(adapter)
