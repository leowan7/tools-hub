"""ProteinMPNN standalone (D1) — atomic primitive.

Modal app: ``ranomics-mpnn-prod``. GPU: A10G-24GB.
Pattern setter per ``docs/ATOMIC-TOOLS.md`` D1 section.

The user uploads a backbone PDB + picks chain(s) to design, and receives
``num_seq_per_target`` candidate sequences with MPNN scores and per-
sequence recovery. 1-credit loss leader on the pilot tier.

D1 exposes a single ``standalone`` tier: caller-supplied PDB, up to 1000
candidate sequences. It runs on the Modal function
(``ranomics-mpnn-prod::run_tool``) and ``run_pipeline.py``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds (also enforced on the pipeline side for direct ``modal run`` use).
# ---------------------------------------------------------------------------

NUM_SEQ_MIN = 1
# Single-container ceiling: MPNN sampling is O(n) and the pipeline's diversity
# filter is O(n^2) pairwise Hamming, so ~1000 seqs finish well inside the
# ranomics-mpnn-prod 600s timeout. Keep in sync with run_pipeline.NUM_SEQ_MAX
# (the pipeline re-clamps standalone and must not silently cap below this).
NUM_SEQ_MAX = 1000
TEMP_MIN = 0.01
TEMP_MAX = 1.0


def _parse_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default`` on failure."""
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value: Any, default: float) -> float:
    """Coerce ``value`` to float, falling back to ``default`` on failure."""
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# One entry of a fixed-positions group: "12" or "1-44".
_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")

# Ceiling on expansion, applied BOTH per token and to the running per-chain
# total. Per token alone is not a bound: a group holds unlimited comma-separated
# tokens, so "A:1-10000,10001-20000,..." spells the same billion-position request
# in a few KB and walks straight past a per-token check. The expanded set is
# persisted verbatim into tool_jobs.inputs (jsonb) and shipped to Modal, and this
# repo already carries the scar of a multi-MB blob wedging a job — see the
# _RAW_VOLUME rationale in tools/mpnn/modal_app.py. No protein chain comes near
# this, and normalise_fixed_positions refuses anything past the real chain length
# anyway; this only has to stop the allocation from happening first.
_MAX_FIXED_POSITIONS = 10_000


def _parse_fixed_positions(
    raw: Any, designed_chains: list[str]
) -> tuple[Optional[dict], Optional[str]]:
    """Parse the fixed-positions field into ``{chain: [1-indexed ints]}``.

    Syntax is whitespace-separated per-chain groups, each ``CHAIN:list``, where
    list items are single positions or inclusive ranges:
    ``A:1-44,46-66,68-88,90-113 B:5,7``. A bare list with no chain prefix is
    accepted only when exactly one chain is being designed, mirroring the
    bare-integer rule in ``base.parse_hotspot_residues``.

    SENSE IS PROTEINMPNN'S: the positions listed are the ones held FIXED, which
    is what ``run_pipeline.normalise_fixed_positions`` expects. Ranges exist so
    that stating the complement of a small redesign patch stays short — freezing
    everything but residues 45, 67 and 89 of a 113-mer is four tokens, not 110.

    Only syntax and chain membership are checked here. Bounds, contiguity and
    author-numbering are re-checked in the pipeline against the real parsed PDB,
    which is the only place those are knowable.
    """
    text = str(raw or "").strip()
    if not text:
        return {}, None
    if not designed_chains:
        return None, "Chains to design is required before fixing positions."

    out: dict[str, list[int]] = {}
    for group in text.split():
        chain, _, body = group.rpartition(":")
        if not chain:
            if len(designed_chains) != 1:
                return None, (
                    f"Fixed positions {group!r} must name its chain when more "
                    f"than one chain is being designed (e.g. "
                    f"{designed_chains[0]}:1-44,46)."
                )
            chain = designed_chains[0]
        if chain not in designed_chains:
            return None, (
                f"Fixed positions name chain {chain!r}, which is not among the "
                f"chains to design ({', '.join(designed_chains)}). Freezing "
                "positions on a chain MPNN was not asked to design does nothing."
            )
        # Rejected rather than merged, matching normalise_fixed_positions: two
        # groups for one chain reads as additive but the second would win.
        if chain in out:
            return None, (
                f"Chain {chain!r} appears more than once in fixed positions; "
                "merge it into a single group."
            )

        positions: set[int] = set()
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            match = _RANGE_RE.match(item)
            if not match:
                return None, (
                    f"Fixed position {item!r} is not a position or a range "
                    f"(e.g. 46 or 1-44)."
                )
            lo = int(match.group(1))
            hi = int(match.group(2) or lo)
            if hi < lo:
                return None, f"Fixed position range {item!r} runs backwards."
            # Positions are 1-indexed. 0 is refused HERE and not left to the
            # pipeline because upstream ProteinMPNN turns it into
            # np.array([0]) - 1 == -1 and silently freezes the LAST residue —
            # one of only two silent failure modes in the whole feature (see
            # verify_fixed_positions' docstring). An off-by-one caller is the
            # likeliest way anyone reaches it.
            if lo < 1:
                return None, (
                    f"Fixed position {item!r} is below 1. Positions are "
                    "1-indexed within their chain; position 1 is the first "
                    "residue."
                )
            if hi - lo >= _MAX_FIXED_POSITIONS:
                return None, (
                    f"Fixed position range {item!r} spans more than "
                    f"{_MAX_FIXED_POSITIONS} positions."
                )
            positions.update(range(lo, hi + 1))
            # Checked inside the loop, so a long comma list stops accumulating
            # rather than being measured after it has already been built.
            if len(positions) > _MAX_FIXED_POSITIONS:
                return None, (
                    f"Fixed positions for chain {chain!r} exceed "
                    f"{_MAX_FIXED_POSITIONS} positions."
                )

        # An empty list is a whole-chain redesign wearing the shape of a freeze.
        # normalise_fixed_positions refuses it too; refusing here names the typo
        # before the job is submitted.
        if not positions:
            return None, (
                f"Fixed positions for chain {chain!r} are empty. Drop the chain "
                "if you meant to redesign all of it."
            )
        out[chain] = sorted(positions)
    return out, None


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the MPNN job_spec shape.

    The single ``standalone`` tier takes a caller-supplied PDB, the
    user-picked chain(s), ``num_seq_per_target``, and ``sampling_temp``.
    A missing or blank preset is treated as ``standalone`` so the form's
    hidden preset field is robust.

    The shape returned is consumed by ``build_payload`` below and is
    also the ``inputs`` blob persisted on the ``tool_jobs`` row.
    """
    preset = (form.get("preset") or "standalone").strip() or "standalone"
    if preset != "standalone":
        return None, "Pick a preset."

    # standalone tier — caller target.
    chains_to_design = (form.get("chains_to_design") or "A").strip()
    if not chains_to_design:
        return None, "chains_to_design is required."
    # Accept "A", "AB", "A B", "A,B" — normalize to space-separated.
    normalized_chains = " ".join(
        tok.strip()
        for tok in chains_to_design.replace(",", " ").split()
        if tok.strip()
    )
    if not normalized_chains:
        return None, "chains_to_design must contain at least one chain ID."
    if len(normalized_chains) > 24:
        return None, "chains_to_design too long (max 24 characters)."
    for chain in normalized_chains.split():
        if len(chain) > 4:
            return None, f"chain ID {chain!r} is too long (max 4 characters)."

    num_seq_per_target = _parse_int(form.get("num_seq_per_target"), 50)
    if num_seq_per_target < NUM_SEQ_MIN or num_seq_per_target > NUM_SEQ_MAX:
        return (
            None,
            f"num_seq_per_target must be between {NUM_SEQ_MIN} and {NUM_SEQ_MAX}.",
        )

    sampling_temp = _parse_float(form.get("sampling_temp"), 0.1)
    if sampling_temp < TEMP_MIN or sampling_temp > TEMP_MAX:
        return (
            None,
            f"sampling_temp must be between {TEMP_MIN} and {TEMP_MAX}.",
        )

    fixed_raw = str(form.get("fixed_positions") or "").strip()
    fixed_positions, fixed_err = _parse_fixed_positions(
        fixed_raw, normalized_chains.split()
    )
    if fixed_err:
        return None, fixed_err

    return (
        {
            "preset": preset,
            "target_chain": normalized_chains,
            "num_seq_per_target": num_seq_per_target,
            "sampling_temp": sampling_temp,
            # Stored as typed so ``clone_from`` refills the field verbatim; the
            # parsed form rides under an underscore key, which the clone route
            # strips from pre_fill (same convention as _pdb_storage_path).
            "fixed_positions": fixed_raw,
            "_fixed_positions": fixed_positions,
            "target": f"Your uploaded PDB (chain(s) {normalized_chains})",
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the MPNN job_spec shape ``run_pipeline.py`` expects.

    The presigned URL is forwarded by the generic submit route via
    ``_input_presigned_url`` — this function does not embed it in the
    dict.
    """
    parameters: dict[str, Any] = {
        "num_seq_per_target": inputs["num_seq_per_target"],
        "sampling_temp": inputs["sampling_temp"],
    }
    # Omitted entirely when nothing was frozen, so a plain redesign submits the
    # byte-identical payload it did before this field existed.
    # ``inputs.get`` because jobs persisted before this field have no such key.
    fixed_positions = inputs.get("_fixed_positions")
    if fixed_positions:
        parameters["fixed_positions"] = fixed_positions
    return {
        "target_chain": inputs["target_chain"],
        "parameters": parameters,
    }


adapter = ToolAdapter(
    slug="mpnn",
    label="ProteinMPNN",
    blurb=(
        "Sequence design from a backbone. Upload a backbone PDB, get N "
        "candidate sequences with MPNN scores and per-sequence recovery. "
        "~30 s per run."
    ),
    presets=(
        Preset(
            slug="standalone",
            label="Standalone with your backbone",
            description=(
                "Upload a backbone PDB, pick chain(s) to redesign, get "
                "up to 1000 candidate sequences. ~30 to 60 s on A10G-24GB."
            ),
            requires_pdb=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    form_template="tools/mpnn_form.html",
    results_partial="tools/mpnn_results.html",
)

register(adapter)
