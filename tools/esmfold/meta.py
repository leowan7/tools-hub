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
    comparison_one_liner - "pick ESMFold when..." positioning string.
    example_output_id - optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "standalone": {"typical_minutes": "0.5-1"},
}

paper_citation: str = "Lin et al., Science 2023"
paper_url: str = "https://www.science.org/doi/10.1126/science.ade2574"
github_url: str = "https://github.com/facebookresearch/esm"
comparison_one_liner: str = (
    "Pick ESMFold when you need the fastest possible monomer fold - no "
    "MSA, no multimer, single-sequence ESM-2 language-model prediction. "
    "Pair with ColabFold (D3) for multimers or AF2 standalone (D2) for "
    "full MSA-backed accuracy."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "ESMFold (Lin et al., <em>Science</em> 2023). Single-sequence "
        "monomer structure prediction from the ESM-2 language model. "
        "No MSA, no multimer support &mdash; fastest fold available "
        "when an MSA is unavailable or unhelpful."
    ),
    "when_to_use": [
        "You need a monomer fold in well under a minute.",
        "Your sequence has no detectable homologs (orphan or designed).",
        "You're triaging a large set of sequences and need throughput.",
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
                "are not supported &mdash; use ColabFold or AF2 instead."
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


# Sample monomer sequences a first-time user can load in one click.
examples: list[dict] = [
    {
        "id": "ubiquitin",
        "label": "Ubiquitin (76 aa)",
        "description": (
            "Tiny monomer benchmark. ~30 s on the ESM-2 3B model; "
            "fastest possible feedback loop."
        ),
        "filename": "ubiquitin.fasta",
        "fasta_field": "fasta_text",
        "params": {},
    },
    {
        "id": "top7",
        "label": "Top7 de novo design (93 aa)",
        "description": (
            "Canonical de novo designed protein. Shows the ESM-2 "
            "single-sequence fold on a designed protein."
        ),
        "filename": "top7.fasta",
        "fasta_field": "fasta_text",
        "params": {},
    },
]
