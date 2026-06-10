"""RFantibody tool adapter.

Modal app: ``ranomics-rfantibody-prod``. GPU: A100-40GB.

Pilot tier accepts a caller-uploaded target PDB plus hotspots and runs
on the webhook flow (~15-60 min on A100-40GB). Only VHH (single-domain
heavy-chain antibody) scaffolds are supported -- ProteinMPNN below
only redesigns heavy-chain CDRs.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the Kendrew RFantibody job_spec shape.

    The caller supplies the target PDB, chain, hotspots, and CDR
    lengths. Framework is fixed to VHH (single-domain heavy-chain
    antibody).
    """
    preset = (form.get("preset") or "pilot").strip() or "pilot"
    if preset != "pilot":
        return None, "Pick a preset."

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

    cdr_lengths = (form.get("cdr_lengths") or "H1:8,H2:7,H3:10-16").strip()

    raw_num_designs = (form.get("num_designs") or "4").strip()
    try:
        num_designs = int(raw_num_designs)
    except (TypeError, ValueError):
        return None, "Number of designs must be an integer."
    # Tier-collapse PR: raised the per-job cap from 24 to 1000 so users
    # can run real production campaigns self-serve. The wallet
    # per-tool hard cap (shared.wallet.PER_JOB_HARD_CAP_USD) remains
    # the durable spend ceiling -- a 1000-design rfantibody run will
    # be blocked by the $500 wallet cap before this validator passes
    # it through unless the user has topped up to cover it.
    if num_designs < 1 or num_designs > 1000:
        return None, "Number of designs must be between 1 and 1000."

    return (
        {
            "preset": preset,
            "target_chain": target_chain,
            "hotspot_residues": hotspot_residues,
            "cdr_lengths": cdr_lengths,
            "num_designs": num_designs,
            "target": f"Your uploaded PDB (chain {target_chain})",
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec RFantibody's run_pipeline.py expects.

    The presigned_url is forwarded by the generic submit route via
    ``_input_pdb_url`` -- this function does not embed it in the
    returned dict.
    """
    return {
        "target_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "parameters": {
            "framework": "VHH",
            "cdr_lengths": inputs["cdr_lengths"],
            "num_designs": inputs["num_designs"],
        },
    }


adapter = ToolAdapter(
    slug="rfantibody",
    label="RFantibody",
    blurb=(
        "Structure-based VHH (nanobody) binder design. Generates "
        "single-domain antibody candidates against a target epitope, "
        "then validates the fold with RoseTTAFold-2."
    ),
    presets=(
        Preset(
            slug="pilot",
            label="Your target, ~30 min start to first results",
            description=(
                "Real RFantibody design against your uploaded target PDB. "
                "Pick 1 to 1000 final VHH candidates. Start with a small "
                "batch (4 designs, ~30 to 60 min) to confirm your target "
                "and hotspots, then scale to 100+ once outputs look "
                "real. Results emailed when run completes; A100-80GB."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    form_template="tools/rfantibody_form.html",
    results_partial="tools/rfantibody_results.html",
)

register(adapter)
