"""Static reference metadata for the ProteinMPNN (D1) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/bindcraft/meta.py`` etc.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
    paper_citation    — short inline citation.
    paper_url         — bioRxiv / Science permalink.
    github_url        — upstream ProteinMPNN repository.
    comparison_one_liner — "pick MPNN when..." positioning string.
    example_output_id — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "standalone": {"typical_minutes": "1"},
}

paper_citation: str = "Dauparas et al., Science 2022"
paper_url: str = "https://www.science.org/doi/10.1126/science.add2187"
github_url: str = "https://github.com/dauparas/ProteinMPNN"

seo_faq: list[dict] = [
    {
        "q": "Can I run ProteinMPNN online without a local GPU?",
        "a": (
            "Yes. Upload a backbone PDB and Ranomics Tools runs ProteinMPNN "
            "on a dedicated GPU in seconds. You get ranked sequence "
            "redesigns plus per-position recovery — no install, no CUDA."
        ),
    },
    {
        "q": "How much does one ProteinMPNN job cost?",
        "a": (
            "Billing is by the second. A typical ProteinMPNN job costs a "
            "fraction of a cent because the model finishes in under a "
            "minute on most backbones. New accounts start with a $5 "
            "wallet balance, which is enough for thousands of runs."
        ),
    },
    {
        "q": "Does ProteinMPNN need an MSA?",
        "a": (
            "No. ProteinMPNN is a structure-conditioned sequence model — "
            "it takes the backbone coordinates directly and never queries "
            "an MSA. That makes it the fastest route from a designed "
            "backbone to a sequence you can synthesise."
        ),
    },
]

comparison_one_liner: str = (
    "Pick ProteinMPNN when you already have a backbone and need candidate "
    "sequences. For de novo backbone generation, use RFantibody, BindCraft, "
    "or BoltzGen first and feed the output PDB here."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page. Field shape and
# rendering rules are documented at the top of about_panel.html.
about: dict = {
    "what_it_is": (
        "ProteinMPNN (Dauparas et al., <em>Science</em> 2022). A "
        "message-passing graph neural network that scores the 20 "
        "canonical residues at every backbone position, conditioned on "
        "C&alpha; / backbone coordinates. Sampling at "
        "<code>sampling_temp</code> produces candidate sequences that "
        "fold into the input geometry."
    ),
    "when_to_use": [
        "You already have a backbone and need candidate sequences for it.",
        "You want to redesign a binder produced by RFdiffusion, RFantibody, BindCraft, or BoltzGen.",
        "You want to thread alternative sequences through a curated PDB before ordering.",
    ],
    "prerequisites": [
        "Backbone PDB / mmCIF — only C&alpha; and backbone atoms are used.",
        "Chain ID(s) of the region(s) to redesign. Other chains stay fixed as context.",
    ],
    "inputs": [
        {
            "name": "Chains to design",
            "explanation": (
                "Which chains in the PDB MPNN should redesign "
                "(e.g. <code>A</code>, <code>A B</code>, <code>H L</code>). "
                "Other chains are held fixed as context."
            ),
        },
        {
            "name": "Number of sequences",
            "explanation": (
                "How many independent samples to draw (1 to 200). Each "
                "sample is independent; rank by score and ProteinMPNN "
                "recovery rate."
            ),
        },
        {
            "name": "Sampling temperature",
            "explanation": (
                "Lower = more conservative (closer to argmax); higher = "
                "more diverse. Defaults to 0.1 per the upstream README."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "standalone", "typical": "~1 min"},
    ],
    "output_summary": (
        "Ranked candidate sequences with per-position score and overall "
        "ProteinMPNN recovery, downloadable as FASTA. Pair downstream "
        "with AlphaFold2 / ColabFold to confirm the predicted fold."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# Sample backbones a first-time user can load in one click. Each entry's
# PDB lives at ``tools/mpnn/examples/<filename>``. ``params`` overrides
# form fields via the ``?example=<id>`` prefill path; the chosen PDB is
# fed in via the ``example:`` pdb_source token resolved at submit time.
examples: list[dict] = [
    {
        "id": "1ubq",
        "label": "Ubiquitin (1ubq)",
        "description": (
            "76-aa monomer benchmark. Fastest MPNN run, "
            "classic sequence-recovery test."
        ),
        "filename": "1ubq.pdb",
        "params": {
            "chains_to_design": "A",
            "num_seq_per_target": "20",
            "sampling_temp": "0.1",
        },
    },
    {
        "id": "4lzt",
        "label": "Hen egg-white lysozyme (4lzt)",
        "description": (
            "129-aa monomeric enzyme. Tests MPNN recovery on a "
            "functional active-site fold."
        ),
        "filename": "4lzt.pdb",
        "params": {
            "chains_to_design": "A",
            "num_seq_per_target": "20",
            "sampling_temp": "0.1",
        },
    },
]
