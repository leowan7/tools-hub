"""Top-N second-opinion fold helpers (C3 of the growth plan).

When a user runs RFdiffusion / BindCraft / RFantibody / PXDesign /
BoltzGen, they get back a list of candidate binder designs ranked by an
AlphaFold2 multimer score. To check whether that ranking holds up under
an *orthogonal* predictor (ColabFold's no-MSA path, or ESMFold's
single-sequence path), we let them spawn N independent fold jobs in one
click and route them to the existing /jobs/compare view.

This module extracts the sequences and provides the source-of-truth list
of which destination tools support the second-opinion-fold handoff.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Tools whose results justify a second-opinion fold. These are the
# tools whose results.candidates carry a designed binder sequence that
# can be re-folded by an orthogonal predictor. RFdiffusion is included
# because its pipeline bundles MPNN sequence design + AF2 multimer
# scoring (so each candidate has a sequence even though MPNN was upstream).
SOURCE_TOOLS: frozenset[str] = frozenset({
    "rfdiffusion",
    "bindcraft",
    "rfantibody",
    "pxdesign",
    "boltzgen",
})

# Destination tools that accept a single binder and produce a per-design
# fold for the comparison view. ColabFold (no-MSA monomer) and ESMFold
# (single-sequence monomer) are the cheapest orthogonal checks; both run
# < 2 min on tools-hub. Boltz-2 is the antibody-trained cofold against
# the SOURCE job's original antigen — same target, different predictor —
# so it is the strongest orthogonal signal for binder-design results.
# Each candidate spawns its own Boltz-2 job to match the per-spawned-job
# row layout of /jobs/compare.
DESTINATION_TOOLS: frozenset[str] = frozenset({
    "colabfold",
    "esmfold",
    "boltz2",
})


# Conservative refold cap. The /jobs/compare view today handles up to 6
# columns; we lift the cap to 10 alongside this so a "top 10 refold" can
# land in one comparison page.
MAX_REFOLD_N: int = 10
DEFAULT_REFOLD_N: int = 5


@dataclass(frozen=True)
class CandidateSeq:
    """A single candidate's sequence with provenance for FASTA headers."""

    rank: int
    pdb_key: str
    sequence: str

    @property
    def fasta_header(self) -> str:
        return f"rank{self.rank}_{self.pdb_key}"


def _candidate_sequence(cand: dict) -> Optional[str]:
    """Best-effort sequence extraction from a single candidate dict.

    Binder design tools expose the designed binder sequence under one of
    a few keys depending on the adapter generation:
      * ``sequence``         — BindCraft, BoltzGen, PXDesign.
      * ``binder_sequence``  — RFantibody, some RFdiffusion variants.
    """
    seq = cand.get("sequence") or cand.get("binder_sequence") or ""
    return seq.strip() if isinstance(seq, str) and seq.strip() else None


def candidate_seq_from_record(cand: dict, idx: int) -> Optional[CandidateSeq]:
    """A :class:`CandidateSeq` for one candidate record, or None when it
    carries no designed sequence. ``idx`` seeds the rank/pdb_key fallbacks."""
    if not isinstance(cand, dict):
        return None
    seq = _candidate_sequence(cand)
    if seq is None:
        return None
    rank = cand.get("rank", idx + 1)
    pdb_key = cand.get("pdb_key", f"design_{idx + 1}")
    return CandidateSeq(rank=int(rank), pdb_key=str(pdb_key), sequence=seq)


def extract_top_n_sequences(
    job_result: dict, n: int
) -> list[CandidateSeq]:
    """Return the top ``n`` candidate sequences from a completed binder
    design job. Caller is responsible for clamping ``n`` to
    ``MAX_REFOLD_N``.

    Candidates already arrive ranked from the source pipeline; we trust
    their order. Candidates that have no sequence field are skipped (so
    the returned list may be shorter than ``n`` if the source job has
    sparse output).
    """
    out: list[CandidateSeq] = []
    candidates: Iterable[dict] = (job_result or {}).get("candidates", [])
    for idx, cand in enumerate(candidates):
        if len(out) >= n:
            break
        cs = candidate_seq_from_record(cand, idx)
        if cs is not None:
            out.append(cs)
    return out


def can_refold(source_tool: str, dest_tool: str) -> bool:
    """Gate for the refold button: source must produce binder
    sequences, destination must accept a single-monomer FASTA.
    """
    return (
        source_tool in SOURCE_TOOLS and dest_tool in DESTINATION_TOOLS
    )


def build_fasta(seqs: Iterable[CandidateSeq]) -> str:
    """Compose a multi-FASTA string from extracted candidates. Used by
    the refold endpoint to build per-job input payloads (one FASTA
    record per spawned ColabFold/ESMFold job).
    """
    blocks: list[str] = []
    for seq in seqs:
        wrapped = "\n".join(
            seq.sequence[i : i + 60]
            for i in range(0, len(seq.sequence), 60)
        )
        blocks.append(f">{seq.fasta_header}\n{wrapped}")
    return "\n".join(blocks) + "\n"
