"""BoltzGen tool adapter.

Modal app: ``ranomics-boltzgen-prod``. GPU: A100-40GB.

BoltzGen uses the Boltz-2 model to generate binder backbones against a
target, then scores each candidate for refolding RMSD, ipTM, and
pLDDT. The pilot tier accepts a caller-supplied target PDB, optional
hotspot residues, a configurable binder-length window, and a Boltz-2
design protocol (mini-protein, nanobody, antibody, or peptide). Runs
~15-60 min on A100-40GB and emails results on completion.
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


# Boltz-2 design protocols the wrapper forwards via ``--protocol``.
# Mirrors the upstream BoltzGen CLI; protein-small_molecule is omitted
# because the form does not yet collect a ligand input.
ALLOWED_PROTOCOLS: frozenset[str] = frozenset({
    "protein-anything",
    "nanobody-anything",
    "antibody-anything",
    "peptide-anything",
})


def _parse_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default`` on failure."""
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the Kendrew BoltzGen job_spec shape.

    Pilot tier requires caller-supplied target PDB + hotspots + binder
    length.
    """
    preset = (form.get("preset") or "pilot").strip() or "pilot"
    if preset != "pilot":
        return None, "Pick a preset."

    # target_chain may name one chain ("A") or several ("A,B" / "A B"):
    # BoltzGen's include: and binding_types: are per-chain LISTS upstream, and
    # the two-chain path is verified on GPU.
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

    raw_hotspots = (form.get("hotspot_residues") or "").strip()
    if raw_hotspots:
        # Parsing these as bare ints accepted "A,B" as a target and then
        # attributed EVERY hotspot to chain A — both protomers in the design
        # context, the epitope spec silently on one of them.
        hotspot_residues, err = parse_hotspot_residues(raw_hotspots, target_chains)
        if err:
            return None, err
    else:
        # BoltzGen accepts an empty hotspot list as "no hotspot constraint".
        hotspot_residues = []

    binder_length_min = _parse_int(form.get("binder_length_min"), 50)
    binder_length_max = _parse_int(form.get("binder_length_max"), 100)

    # Lower floor of 10 leaves headroom for peptide-anything (typical
    # 5-30 aa); upper bound of 200 covers nanobody and antibody-anything.
    if binder_length_min < 10 or binder_length_min > 200:
        return None, "binder_length_min must be between 10 and 200."
    if binder_length_max < 10 or binder_length_max > 200:
        return None, "binder_length_max must be between 10 and 200."
    if binder_length_min > binder_length_max:
        return None, "binder_length_min must be <= binder_length_max."

    budget = _parse_int(form.get("budget"), 4)
    # Cap is 50, not the candidate pool size of 200. budget is the
    # top N selected from the num_designs=200 pool that build_payload
    # sends. Capping budget at 50 preserves a 4x selectivity ratio
    # (200 generated, top 50 returned). Going closer to budget=200
    # collapses the filter to a no op and the user gets every
    # candidate regardless of score. Raising this requires a
    # coordinated bump to num_designs in build_payload and the 6600s
    # subprocess timeout in llm-proteinDesigner. The wallet $300 hard
    # cap on boltzgen still constrains actual spend.
    if budget < 1 or budget > 50:
        return None, "budget must be between 1 and 50."

    protocol = (form.get("protocol") or "protein-anything").strip()
    if protocol not in ALLOWED_PROTOCOLS:
        return None, (
            "Protocol must be one of: "
            + ", ".join(sorted(ALLOWED_PROTOCOLS))
            + "."
        )

    return (
        {
            "preset": preset,
            # Canonical comma form regardless of what the user typed:
            # both separators are accepted at this boundary, exactly one
            # is emitted, so no container has to guess.
            "target_chain": ",".join(target_chains),
            "hotspot_residues": hotspot_residues,
            "binder_length_min": binder_length_min,
            "binder_length_max": binder_length_max,
            "budget": budget,
            "protocol": protocol,
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec BoltzGen's run_pipeline.py expects.

    Caller target; presigned URL is forwarded by the generic submit
    route, not embedded here.
    """
    # job_tier is also set at the wrapper level by gpu/modal_client.py, but we
    # echo it inside job_spec so older run_pipeline.py builds (which read
    # job_spec.get("job_tier")) still resolve the tier correctly. This is what
    # gates the pilot fallback that emits top-N designs when none pass the
    # strict ipTM/pLDDT/RMSD thresholds.

    # Pilot tier. num_designs is the candidate population BoltzGen generates
    # and refolds (budget then selects the top-N to return). 1000 was the
    # original wave-2 default but ran past the 6600s subprocess timeout in
    # docker/boltzgen/run_pipeline.py:1407 on A100-40GB. 200 fits comfortably
    # within the "~15-60 min" pilot description and gives the filter a 4x
    # selectivity ratio against the validate-side budget cap of 50.
    return {
        "job_tier": "pilot",
        "target_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "parameters": {
            "binder_length": {
                "min": inputs["binder_length_min"],
                "max": inputs["binder_length_max"],
            },
            "num_designs": 200,
            "budget": inputs["budget"],
            "protocol": inputs["protocol"],
        },
    }


adapter = ToolAdapter(
    slug="boltzgen",
    label="BoltzGen",
    blurb=(
        "Boltz-2 binder design. Generates mini-protein, nanobody, "
        "antibody, or peptide backbones against a target, refolds each "
        "candidate, and scores affinity via ipTM and pLDDT."
    ),
    presets=(
        Preset(
            slug="pilot",
            label="Your target, ~30 min start to first results",
            description=(
                "Real BoltzGen run against your uploaded target. Pick "
                "1 to 50 final candidates with refolding RMSD and ipTM "
                "scores. Start with 4 designs (~15 to 30 min) to confirm "
                "your target and binder length, then scale up once the "
                "small batch looks reasonable. Results emailed when "
                "complete; A100-40GB."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    form_template="tools/boltzgen_form.html",
    results_partial="tools/boltzgen_results.html",
)

register(adapter)
