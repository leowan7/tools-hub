"""Static reference metadata for the Proteina-Complexa tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so the "About" panel, citation block, license notices, and
cost previews import plain-data constants without touching the adapter
contract. Mirrors ``tools/boltzgen/meta.py``.
"""

from __future__ import annotations

from typing import Optional

PRESET_RUNTIME: dict[str, dict[str, object]] = {
    # Per-shard wall-clock. BOOTSTRAP ranges pending the P4/P5 canaries; the
    # campaign fans many shards out in parallel, so total time depends on the
    # requested design count and the launch concurrency (4).
    "protein_binder": {"typical_minutes": "30 to 120"},
    "ligand_binder": {"typical_minutes": "30 to 120"},
    "motif_ame": {"typical_minutes": "30 to 120"},
    "validate": {"typical_minutes": "1 to 3"},
}

paper_citation: str = "Geffner et al., NVIDIA (2025)"
paper_url: str = "https://research.nvidia.com/labs/genair/proteina-complexa/"
# The binder/ligand/AME search code, configs, reward stack, and weights live in
# the Proteina-Complexa repo (branch ``dev``), NOT the base ``proteina`` backbone
# generator — the module name ``proteinfoundation`` is shared between the two,
# which is an easy mix-up. Pinned commit: 916eaaedce5b07c205efb6ef32370c01d366591e.
github_url: str = "https://github.com/NVIDIA-Digital-Bio/proteina-complexa"

# NVIDIA Open Model License notice — surfaced verbatim on the tool page + repo.
model_license_notice: str = (
    "Licensed by NVIDIA Corporation under the NVIDIA Open Model License"
)

# Reward-model attributions surfaced alongside the results.
reward_attributions: list[str] = [
    "AlphaFold2 parameters (CC-BY-4.0, DeepMind) — protein-binder confidence.",
    "RoseTTAFold3 via RosettaCommons foundry (BSD) — ligand + motif reward.",
    "ESM2 (MIT, Meta AI) — sequence likelihood.",
    "Foldseek / MMseqs2 / DSSP — post-hoc diversity clustering.",
]

seo_faq: list[dict] = [
    {
        "q": "Can I run Proteina-Complexa online without a GPU cluster?",
        "a": (
            "Yes. Ranomics Tools runs Proteina-Complexa as a fund-and-drain "
            "campaign of independent search shards on dedicated A100-80GB "
            "GPUs. Pick a protein or small-molecule target, choose how many "
            "designs you want, and shards fan out automatically. You only pay "
            "for compute that runs, and the campaign pauses if your balance "
            "runs low."
        ),
    },
    {
        "q": "Can Proteina-Complexa design binders against a small molecule?",
        "a": (
            "Yes. The ligand-binder variant takes a small-molecule target as "
            "an SDF and designs de novo binders scored by the RoseTTAFold3 "
            "reward. The protein-binder variant targets a protein PDB and is "
            "scored by AlphaFold2 confidence."
        ),
    },
    {
        "q": "How are Proteina-Complexa designs scored and ranked?",
        "a": (
            "Each search shard filters candidates through an AF2 / RF3 / "
            "force-field reward stack, and the hub then selects a global "
            "top-K across all shards with post-hoc structural diversity "
            "clustering, so you get diverse high-reward designs rather than "
            "near-duplicates."
        ),
    },
]

comparison_one_liner: str = (
    "Pick Proteina-Complexa when you want de novo binders against a protein "
    "OR a small-molecule (ligand) target, scored by a full AF2 / RF3 / "
    "force-field reward stack, and you want to scale the search across many "
    "GPUs with the wallet as the only ceiling."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Proteina-Complexa (Geffner et al., NVIDIA 2025). A flow-matching "
        "generator wrapped in an inference-time search that filters designs "
        "through an AlphaFold2 / RoseTTAFold3 / force-field reward stack. It "
        "designs de novo binders against protein targets, small-molecule "
        "(ligand) targets, and enzyme / motif active sites. Runs here as a "
        "fund-and-drain campaign of independent seeded search shards, with "
        "global cross-shard top-K and post-hoc diversity clustering."
    ),
    "when_to_use": [
        "You want de novo binders against a small-molecule target, not just a protein.",
        "You want an inference-time search filtered by an AF2 / RF3 / force-field reward, not raw generation.",
        "You want to scale the search across many GPUs with the prepaid wallet as the only ceiling.",
        "You want diverse high-reward designs (global top-K + diversity clustering) rather than near-duplicates.",
    ],
    "prerequisites": [
        "A target: a curated benchmark task, or your own target "
        "(<code>.pdb</code> for protein / motif, <code>.sdf</code> for ligand).",
        "For a protein target, the chain ID.",
        "A funded wallet that covers at least the first wave of shards.",
    ],
    "inputs": [
        {
            "name": "Design variant",
            "explanation": (
                "<code>protein_binder</code> for a protein target (AF2 reward), "
                "<code>ligand_binder</code> for a small-molecule SDF target "
                "(RF3 reward), <code>motif_ame</code> for motif scaffolding / "
                "enzyme active sites, or <code>validate</code> for a free "
                "config check before spending GPU."
            ),
        },
        {
            "name": "Target task",
            "explanation": (
                "A curated benchmark task (target baked in) or your own "
                "uploaded target. Protein and motif targets are PDB; ligand "
                "targets are SDF."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many designs to search for. This scales the number of "
                "independent search shards; each shard runs on its own GPU and "
                "returns its survivors, and the hub picks the global top set."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "protein_binder", "typical": "30 to 120 min / shard"},
        {"preset": "ligand_binder", "typical": "30 to 120 min / shard"},
        {"preset": "motif_ame", "typical": "30 to 120 min / shard"},
        {"preset": "validate", "typical": "1 to 3 min (free)"},
    ],
    "output_summary": (
        "Ranked designs with reward scores (AF2 pLDDT / ipTM for protein, "
        "RF3 score for ligand / motif, force-field energy where applicable), "
        "a structural diversity cluster id, and downloadable structures. The "
        "ligand and motif variants score on RF3 only."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
    "model_license_notice": model_license_notice,
    "reward_attributions": reward_attributions,
}
