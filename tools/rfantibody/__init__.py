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


# RFantibody designs a VHH (single heavy chain), so only H1/H2/H3 are
# valid CDRs. Lengths feed RFdiffusion's contig builder, which asserts on
# malformed / out-of-envelope values mid-run, so reject them at submit.
_CDR_SPEC_EXAMPLE = "H1:8,H2:7,H3:10-16"
_CDR_BOUNDS: dict = {"H1": (1, 20), "H2": (1, 20), "H3": (5, 20)}


def _validate_cdr_lengths(spec: str) -> tuple[Optional[str], Optional[str]]:
    """Validate and canonicalize the CDR length spec.

    Returns ``(canonical_spec, None)`` when valid, else ``(None, message)``.
    The canonical form (uppercase keys, ASCII-digit values, single-hyphen
    ranges, no spaces) is what gets forwarded to the GPU, so leniency in
    parsing (whitespace, case) never lets an off-format string reach the
    pipeline. Accepts comma-separated ``KEY:VALUE`` entries where KEY is
    H1/H2/H3 and VALUE is a whole number or a ``lo-hi`` range. The hyphen
    in ``10-16`` is required input syntax; error prose renders numeric
    ranges as "X to Y".
    """
    text = (spec or "").strip()
    if not text:
        return "", None  # caller defaults this; empty means "use the default"
    seen: set = set()
    canonical: list = []
    for raw_entry in text.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            return None, (
                f'CDR spec entry "{entry}" must look like KEY:LENGTH. '
                f'Example: "{_CDR_SPEC_EXAMPLE}".'
            )
        key, _, value = entry.partition(":")
        key = key.strip().upper()
        value = value.strip()
        if key not in _CDR_BOUNDS:
            return None, (
                f'Unknown CDR "{key}". Use H1, H2, or H3 only '
                f'(RFantibody designs a VHH heavy chain). '
                f'Example: "{_CDR_SPEC_EXAMPLE}".'
            )
        if key in seen:
            return None, f'CDR "{key}" is specified more than once.'
        seen.add(key)
        lo_s, sep, hi_s = value.partition("-")
        lo_s, hi_s = lo_s.strip(), hi_s.strip()
        if sep and not hi_s:
            return None, (
                f'CDR {key} range "{value}" is missing its upper bound '
                f'(write it low to high, e.g. "10-16").'
            )
        # Strict ASCII-digit tokens only: rejects "+8", "1_0", embedded
        # spaces, unicode digits, and negatives that int() would otherwise
        # silently accept and forward off-format to the GPU.
        for tok in ((lo_s, hi_s) if sep else (lo_s,)):
            if not (tok.isascii() and tok.isdigit()):
                return None, (
                    f'CDR {key} length "{value}" must be a whole number or a '
                    f'range written low to high (e.g. "10-16").'
                )
        lo = int(lo_s)
        hi = int(hi_s) if sep else lo
        if lo > hi:
            return None, (
                f"CDR {key} range is backwards: {lo} to {hi}. "
                f"Write it low to high."
            )
        floor, ceil = _CDR_BOUNDS[key]
        if lo < floor or hi > ceil:
            return None, (
                f"CDR {key} length must be between {floor} and {ceil} "
                f"(got {value})."
            )
        canonical.append(f"{key}:{lo}" if not sep else f"{key}:{lo}-{hi}")
    if not canonical:
        return None, f'CDR spec is empty. Example: "{_CDR_SPEC_EXAMPLE}".'
    return ",".join(canonical), None


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
    canonical_cdr, cdr_err = _validate_cdr_lengths(cdr_lengths)
    if cdr_err:
        return None, cdr_err
    # Forward the canonical form so whitespace / case in user input never
    # reaches the GPU contig builder.
    cdr_lengths = canonical_cdr or cdr_lengths

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
        "Upload your target structure, mark the patch you want gripped, "
        "and get back nanobody (single-domain antibody) candidates, "
        "each refolded and scored against the target."
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
