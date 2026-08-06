"""Static reference metadata for the Proteina-Complexa tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so the "About" panel, citation block, license notices, and
cost previews import plain-data constants without touching the adapter
contract. Mirrors ``tools/boltzgen/meta.py``.
"""

from __future__ import annotations

from typing import Optional

PRESET_RUNTIME: dict[str, dict[str, object]] = {
    # Per-shard wall-clock. The campaign fans many shards out in parallel, so
    # total campaign time depends on the requested design count and the launch
    # concurrency (4), not on this number alone.
    #
    # protein_binder is MEASURED, at three sizes. Paid A100-80GB canary shards
    # returned 8 designs in 576 s (9.6 min) at 130 aa, 645 s (10.8 min) at
    # 260 aa and 874 s (14.6 min) at 415 aa. The band below spans them, and the
    # top of it also covers the 500-aa cap in shared/pdb_preflight_rules.py
    # (_PROTEINA): runtime scales as (aa/120)^0.34, so a target at the cap
    # comes out at ~14.6 min too — the curve is nearly flat in target size, and
    # Modal cold-start and GPU contention matter more than the extra residues.
    #
    # THIS COPY HAS BEEN WRONG TWICE, both times in the direction a user plans
    # against. It shipped as "30 to 120" for all three variants, a placeholder
    # never re-set, which overstated a real shard by 5-20x. It was then
    # corrected to "~6" from a 359 s shard — but that shard died before its
    # AF2/ESM stack loaded, so it timed an incomplete run. A COMPLETE run at
    # that same 130 aa takes 576 s. Both times the number here was also
    # load-bearing: shared/pdb_preflight_rules.py anchors its runtime estimator
    # to this measurement, so an error in a docs constant reaches the preflight
    # panel looking calibrated.
    #
    # ligand_binder and motif_ame have NEVER been timed. Their band is bounded,
    # not measured: the floor is the smallest complete protein_binder
    # measurement and the ceiling is the physical _MAX_SESSION_S = 7200 s
    # (120 min) session wall in modal_app.py, past which the shard is killed.
    # Re-set each from its own canary.
    #
    # AND THE FLOOR IS WEAKER THAN IT LOOKS. This used to read "same container,
    # same reward stack". The container is the same; the reward stack is NOT.
    # protein_binder scores on AF2 alone, while RF3 is the SOLE reward for
    # ligand_binder and is what motif_ame needs too — Dockerfile.modal:219-222
    # says so outright ("Only ligand_binder (RF3 is its sole reward) and
    # motif_ame need it; protein_binder scores on AF2 alone"), and
    # ``reward_attributions`` below splits them the same way. So the floor is
    # not evidence transferred from a comparable run; it is a lower bound
    # borrowed from a DIFFERENT scoring path, and there is no reason to think
    # RF3 scoring is as fast as AF2 scoring. Treat 10 as "cannot plausibly be
    # quicker than the fastest thing we timed", not as a measurement.
    "protein_binder": {"typical_minutes": "~10 to 15"},
    "ligand_binder": {"typical_minutes": "10 to 120 (not yet measured)"},
    "motif_ame": {"typical_minutes": "10 to 120 (not yet measured)"},
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
        "You want to aim a de novo binder at a specific epitope, including a "
        "recessed or occluded one, using hotspot residues.",
        "Your target is multi-chain and the site you care about spans more than "
        "one chain.",
        "You want de novo binders against a small-molecule target, not just a protein.",
        "You want an inference-time search filtered by an AF2 / RF3 / force-field reward, not raw generation.",
        "You want to scale the search across many GPUs with the prepaid wallet as the only ceiling.",
        "You want diverse high-reward designs (global top-K + diversity clustering) rather than near-duplicates.",
    ],
    "prerequisites": [
        "A target: your own structure (<code>.pdb</code>/<code>.cif</code>) for "
        "the protein-binder variant, or a curated benchmark task for any variant.",
        "For your own target, the chain ID &mdash; or a chain/residue range such "
        "as <code>A1-150</code>, or <code>A12-157,B12-157,C12-157</code> for a "
        "multi-chain target.",
        "Optionally, hotspot residues to aim the binder at a specific epitope.",
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
            "name": "Target",
            "explanation": (
                "Your own structure, or a curated benchmark task whose target "
                "is baked in &mdash; the two are mutually exclusive. Uploading "
                "your own is available on the protein-binder variant; the "
                "ligand and motif variants run curated tasks (their tasks "
                "resolve from separate upstream registries)."
            ),
        },
        {
            "name": "Target region",
            "explanation": (
                "Which chains and residues to design against, e.g. "
                "<code>A1-150</code>, or <code>A12-157,B12-157,C12-157</code> "
                "for a multi-chain target. Blank uses the whole target chain."
            ),
        },
        {
            "name": "Hotspot residues",
            "explanation": (
                "Optional. Residues the binder should contact, in original PDB "
                "numbering &mdash; plain numbers use the target chain, or "
                "prefix the chain (<code>A113 C73</code>) for a multi-chain "
                "region. Every hotspot is checked against your structure "
                "before any GPU runs, so a residue that is not there is "
                "refused rather than quietly ignored."
            ),
        },
        {
            "name": "Binder length",
            "explanation": (
                "The range each design's length is drawn from. Defaults to "
                "60-120 residues."
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
    # Kept in lockstep with PRESET_RUNTIME above — see the provenance note
    # there for what is measured (protein_binder, at 130 / 260 / 415-residue
    # targets) and what is only bounded by the 7200 s session wall.
    "runtime_table": [
        {"preset": "protein_binder",
         "typical": "~10 to 15 min / shard (measured at 130-415 residues)"},
        {"preset": "ligand_binder", "typical": "not yet measured (under 120 min / shard)"},
        {"preset": "motif_ame", "typical": "not yet measured (under 120 min / shard)"},
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
