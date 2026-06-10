"""Static reference metadata for the ColabFold (D3) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/mpnn/meta.py``.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
    paper_citation    — short inline citation.
    paper_url         — Nature Methods / bioRxiv permalink.
    github_url        — upstream ColabFold repository.
    comparison_one_liner — "pick ColabFold when..." positioning string.
    example_output_id — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "standalone": {"typical_minutes": "1 to 2"},
}

paper_citation: str = "Mirdita et al., Nature Methods 2022"
paper_url: str = "https://www.nature.com/articles/s41592-022-01488-1"
github_url: str = "https://github.com/sokrypton/ColabFold"
comparison_one_liner: str = (
    "Pick ColabFold when you need a fast no-MSA fold. 1 to 2 min per "
    "run, no MMseqs2 round-trip. Pair with AF2 standalone (D2) when "
    "you want full MSA and templates, or with ESMFold (D4) for "
    "single-sequence monomers on an even smaller GPU."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "ColabFold (Mirdita et al., <em>Nature Methods</em> 2022) "
        "running AlphaFold2 weights without MMseqs2 MSA fetch. Faster "
        "than full AF2 at the cost of MSA-derived accuracy. Useful when "
        "you need a structure quickly and the target has a tractable fold."
    ),
    "when_to_use": [
        "You need a structure in 1 to 2 minutes and can tolerate slightly lower accuracy than full-MSA AF2.",
        "You're folding many sequences sequentially and need throughput.",
        "Your target is a well-folded monomer or small multimer with no exotic chemistry.",
    ],
    "prerequisites": [
        "Single-letter FASTA sequence(s).",
        "Targets with deep evolutionary signal. Multi-domain or low-information sequences underperform without MSA.",
    ],
    "inputs": [
        {
            "name": "Sequence",
            "explanation": (
                "Paste FASTA. Use <code>:</code> as a chain separator "
                "for multimers."
            ),
        },
        {
            "name": "Recycles",
            "explanation": (
                "Model recycles. ColabFold default is 3; reduce for "
                "speed if your target's fold is well-known."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "standalone", "typical": "1 to 2 min"},
    ],
    "output_summary": (
        "Predicted PDB with per-residue pLDDT and PAE. Download as "
        "PDB or PAE matrix for filtering."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# Sample sequences a first-time user can load in one click.
examples: list[dict] = [
    {
        "id": "ubiquitin",
        "label": "Ubiquitin (76 aa)",
        "description": (
            "Tiny monomer benchmark. ~1 min on the no-MSA ColabFold path; "
            "useful for confirming the pipeline end-to-end."
        ),
        "filename": "ubiquitin.fasta",
        "fasta_field": "fasta_text",
        "params": {
            "num_recycles": "1",
        },
    },
    {
        "id": "top7",
        "label": "Top7 de novo design (93 aa)",
        "description": (
            "Canonical de novo designed protein. Shows ColabFold's "
            "no-MSA path on a designed fold."
        ),
        "filename": "top7.fasta",
        "fasta_field": "fasta_text",
        "params": {
            "num_recycles": "1",
        },
    },
]
