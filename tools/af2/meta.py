"""Static reference metadata for the AF2 standalone (D2) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/mpnn/meta.py``.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
    paper_citation    — short inline citation.
    paper_url         — Nature permalink.
    github_url        — upstream ColabFold repository (which bundles AF2).
    comparison_one_liner — "pick AF2 when..." positioning string.
    example_output_id — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    # Standalone: user FASTA, MMseqs2 MSA + 3 recycles. MSA fetch
    # dominates for short sequences; fold time scales with length.
    "standalone": {"typical_minutes": "5 to 10"},
}

paper_citation: str = "Jumper et al., Nature 2021 (AF2); Mirdita et al., Nature Methods 2022 (ColabFold)"
paper_url: str = "https://www.nature.com/articles/s41586-021-03819-2"
# ColabFold is the packaging we actually ship — AF2 weights + MMseqs2
# MSA + a clean pip install. The upstream AlphaFold2 repo is linked
# from the ColabFold README.
github_url: str = "https://github.com/sokrypton/ColabFold"

seo_faq: list[dict] = [
    {
        "q": "Can I run AlphaFold2 multimer online without a local GPU?",
        "a": (
            "Yes. Ranomics Tools runs AlphaFold2 (via the ColabFold "
            "implementation) on a dedicated GPU through your browser, "
            "with full MSA search and template support. Results land on a "
            "job page with ipTM, pLDDT, and pAE plots."
        ),
    },
    {
        "q": "How is this different from running ColabFold yourself?",
        "a": (
            "Same underlying weights and pipeline, but you skip CUDA "
            "setup, MMseqs2 round-trip on your laptop, and the wait for "
            "the public Colab queue. You also get a persistent job page "
            "you can share or hand off into ProteinMPNN or BindCraft."
        ),
    },
    {
        "q": "How much does an AlphaFold2 multimer run cost?",
        "a": (
            "Billing is by the second of dedicated GPU time. A typical "
            "single-complex fold costs a few cents to a dollar from your "
            "wallet. New accounts start with $5 of credit, which covers "
            "many monomer folds or a handful of multimers."
        ),
    },
]

comparison_one_liner: str = (
    "Pick AF2 when you need the gold-standard structure prediction with "
    "calibrated pLDDT and PAE. For faster single-sequence folds use "
    "ESMFold (D4); for affinity-aware folds use Boltz-2 (D6)."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "AlphaFold2 (Jumper et al., <em>Nature</em> 2021) packaged "
        "via ColabFold (Mirdita et al., <em>Nature Methods</em> 2022). "
        "Standard MSA-backed structure prediction with calibrated "
        "pLDDT and PAE, monomer or multimer."
    ),
    "when_to_use": [
        "You need the gold-standard fold with full MSA and templates and calibrated confidence.",
        "Your target is monomeric or a small multimer (2 to 4 chains).",
        "You can wait roughly 5 to 10 min per run for MMseqs2 MSA fetch plus 3 recycles.",
    ],
    "prerequisites": [
        "Single-letter FASTA sequence(s). Multimers separated by <code>:</code> or pasted as multi-record FASTA.",
        "A stable target topology. AF2 underperforms on intrinsically disordered or flexible regions.",
    ],
    "inputs": [
        {
            "name": "Sequence",
            "explanation": (
                "Paste FASTA. Use <code>:</code> as a chain separator "
                "for multimers (e.g. <code>SEQ_A:SEQ_B</code>)."
            ),
        },
        {
            "name": "Recycles",
            "explanation": (
                "Number of model recycles. 3 is the AF2 default; lower "
                "is faster but trades a small amount of accuracy."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "standalone", "typical": "5 to 10 min"},
    ],
    "output_summary": (
        "Predicted PDB with per-residue pLDDT, pairwise PAE, and pTM "
        "or ipTM (for multimers). Download PDB or PAE matrix for "
        "downstream filtering and analysis."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# Sample sequences a first-time user can load in one click. The FASTA
# file's contents are read at prefill time and dropped into the form's
# ``fasta`` textarea (the field name AF2 expects).
examples: list[dict] = [
    {
        "id": "ubiquitin",
        "label": "Ubiquitin (76 aa)",
        "description": (
            "Tiny monomer benchmark. Quick MSA-backed AF2 fold; useful "
            "for confirming the pipeline end-to-end."
        ),
        "filename": "ubiquitin.fasta",
        "fasta_field": "fasta",
        "params": {
            "num_recycles": "3",
        },
    },
    {
        "id": "top7",
        "label": "Top7 de novo design (93 aa)",
        "description": (
            "The canonical de novo designed protein (Kuhlman et al. 2003). "
            "Shows AF2 on a designed fold rather than a natural sequence."
        ),
        "filename": "top7.fasta",
        "fasta_field": "fasta",
        "params": {
            "num_recycles": "3",
        },
    },
]
