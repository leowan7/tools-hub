"""Fold to MPNN-diversification resample helpers.

When a user folds a sequence with AF2 / ColabFold / ESMFold and the
confidence comes back borderline (pLDDT 70-85, ipTM 0.65-0.80), the
useful next move is not "throw it away" but "rescue it with sequence
diversification": feed the *predicted structure* back into
ProteinMPNN at a higher sampling temperature, get a handful of
alternative sequences that fit the same fold target, and re-fold the
best of them.

This module names the source tool set and centralizes the resample
defaults so the form-GET prefill, the resample-token resolver, and
the button-rendering check all read from one place.

Implementation note: the predicted PDB lives at ``job.result.pdb_b64``
(base64-encoded) across all three source tools. The resample-token
resolver decodes those bytes and uploads them as a fresh MPNN input
PDB, so MPNN sees a normal staged upload at submit time.
"""
from __future__ import annotations

# Fold predictors whose top predicted structure can be fed into MPNN
# for sequence diversification. All three persist the predicted PDB
# under the same ``result.pdb_b64`` key, which the resample token
# resolver decodes at submit time.
RESAMPLE_SOURCES: frozenset[str] = frozenset({
    "af2",
    "colabfold",
    "esmfold",
})

# The only destination MPNN-diversification makes sense for is MPNN
# itself. Kept as a constant for parity with refold.DESTINATION_TOOLS
# in case future sequence-design tools join the lineup.
RESAMPLE_DESTINATION: str = "mpnn"


# Defaults applied to the MPNN form when the user arrives via a
# resample handoff. Bumps sampling_temp up from MPNN's normal 0.1
# (conservative, near-argmax) to 0.5 (meaningfully diverse, still
# targeting the same fold). Drops num_seq_per_target from 50 to 16
# because re-folding the output candidates downstream is the
# expensive step and 16 is enough variety to pick a top 3-5.
RESAMPLE_MPNN_DEFAULTS: dict[str, str] = {
    "sampling_temp": "0.5",
    "num_seq_per_target": "16",
    "chains_to_design": "A",
}


def can_resample(source_tool: str) -> bool:
    """Return True iff ``source_tool`` is a supported fold predictor."""
    return source_tool in RESAMPLE_SOURCES
