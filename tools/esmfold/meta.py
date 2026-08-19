"""Static reference metadata for the ESMFold (D4) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/colabfold/meta.py``.

Shapes
------
    PRESET_RUNTIME    - {preset_slug: {"typical_minutes": str}}.
    paper_citation    - short inline citation.
    paper_url         - Science / bioRxiv permalink.
    github_url        - upstream ESM repository.
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
    example_output_id - optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "standalone": {"typical_minutes": "0.5 to 1"},
}

paper_citation: str = "Lin et al., Science 2023"
paper_url: str = "https://www.science.org/doi/10.1126/science.ade2574"
github_url: str = "https://github.com/facebookresearch/esm"
comparison_one_liner: str = (
    "You have one protein sequence and want its 3D shape in about "
    "30 seconds. No search for relatives, so it works on designed "
    "or orphan sequences that have no natural family to align "
    "against. One chain only — for complexes use ColabFold or "
    "AlphaFold2."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Predicts the structure of one protein chain from its sequence "
        "alone, in about 30 seconds. It reads the sequence through a "
        "protein language model instead of searching for relatives, so "
        "it still works on designed or orphan sequences that have no "
        "natural family to align against. One chain only — it cannot "
        "fold a complex. ESMFold, Lin et al., <em>Science</em> 2023."
    ),
    "when_to_use": [
        (
            "You want a single-chain fold in well under a minute."
        ),
        (
            "Your sequence is designed, or has no known relatives, so a "
            "search for them would come back empty anyway."
        ),
        (
            "You are triaging a large batch of sequences and need "
            "throughput more than precision."
        ),
    ],
    "prerequisites": [
        "Single FASTA sequence (monomer only).",
        "Sequence length under ~600 residues for best accuracy.",
    ],
    "inputs": [
        {
            "name": "Sequence",
            "explanation": (
                "Single-chain FASTA. Multimers and non-canonical residues "
                "are not supported. Use ColabFold or AF2 instead."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "standalone", "typical": "~30 s"},
    ],
    "output_summary": (
        "Predicted PDB with per-residue pLDDT. No PAE (single-sequence "
        "prediction has no inter-domain signal). Use as a fast "
        "self-consistency check on designed sequences."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# ---------------------------------------------------------------------------
# No pilot. See templates/components/pilot_card.html — the card simply
# does not render.
# ---------------------------------------------------------------------------
# A single-sequence monomer fold finishes in well under a minute
# and has no scale parameter to start small on. There is nothing a
# pilot tier would reduce.
PILOT: dict | None = None


# ---------------------------------------------------------------------------
# EXAMPLE — one real past run, rendered by
# templates/components/worked_example.html. None here, deliberately:
# No real completed-run payload for this tool exists anywhere on disk
# (searched 2026-08-18: every .json in the tree, .deploy-logs/, scratch/,
# runs/, tmp/). The fixtures in tests/ are synthetic and the stage JSONs
# under runs/ are pipeline-stage outputs, not job results. Capture one from
# a real run and this becomes a two-file change: example/result.json plus
# the narration below. scripts/capture_example_result.py pulls a succeeded
# run out of the jobs table, scrubs the customer-identifying fields, and
# prints the figures the narration has to match. The results partial is
# already example-safe — the guard lives in the two shared macros, not
# here — so nothing else needs touching.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = None
