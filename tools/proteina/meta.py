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
    # 260 aa and 874 s (14.6 min) at 415 aa. The band below has to CONTAIN the
    # span it cites, and "~10 to 15" did not — the 130 aa shard, the smallest
    # and fastest of the three, sits at 9.6 and fell outside its own claimed
    # range. "~9 to 15" spans all three; the top of it also covers the 500-aa
    # cap in shared/pdb_preflight_rules.py (_PROTEINA): runtime scales as
    # (aa/120)^0.34, so a target at the cap comes out at ~14.6 min too — the
    # curve is nearly flat in target size, and Modal cold-start and GPU
    # contention matter more than the extra residues.
    #
    # THIS COPY HAS BEEN WRONG TWICE, both times in the direction a user plans
    # against. It shipped as "30 to 120" for all three variants, a placeholder
    # never re-set, which overstated a real shard by 5-20x. It was then
    # corrected to "~6" from a 359 s reading at 130 aa. TWO readings exist at
    # that size and they disagree by ~60%: 359 s and 576 s. Both are recorded
    # as completed 8-design protein_binder shards at 130 aa; what separates
    # them is the JAX ALLOCATOR REGIME. The 359 s wall-clock belongs to the
    # preallocation-ON shard that read 67,570 MB; the three shards this band is
    # drawn from all ran with preallocation disabled.
    # shared/pdb_preflight_rules.py::_PROTEINA documents those two regimes as
    # non-comparable and names allocate-on-demand as a candidate for the gap,
    # not a diagnosis of it. The 576 s reading is the one taken under the
    # allocator settings production runs today, so it is the one that describes
    # what a user's shard will do; the 359 s figure is not used for anything.
    # Both times the number here was also load-bearing:
    # shared/pdb_preflight_rules.py anchors its runtime estimator to this
    # measurement, so an error in a docs constant reaches the preflight panel
    # looking calibrated.
    #
    # ligand_binder and motif_ame have NEVER been timed. Their band is bounded,
    # not measured: the floor is the smallest complete protein_binder
    # measurement (9.6 min) and the ceiling is the physical
    # _MAX_SESSION_S = 7200 s (120 min) session wall in modal_app.py, past
    # which the shard is killed. Re-set each from its own canary.
    #
    # THAT FLOOR ROUNDS DOWN, TO 9, for the same reason protein_binder's does.
    # It read 10 while protein_binder's read 9 — the same 9.6 min rounded two
    # different ways in adjacent lines, which had the never-measured presets
    # claiming a HIGHER floor than the only preset anyone has timed, and
    # overstated the sentence below by 0.4 min. Rounding down keeps a lower
    # bound a lower bound; rounding up turns it into a claim.
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
    # RF3 scoring is as fast as AF2 scoring. Treat 9 as "cannot plausibly be
    # quicker than the fastest thing we timed", not as a measurement.
    "protein_binder": {"typical_minutes": "~9 to 15"},
    "ligand_binder": {"typical_minutes": "9 to 120 (not yet measured)"},
    "motif_ame": {"typical_minutes": "9 to 120 (not yet measured)"},
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
         "typical": "~9 to 15 min / shard (measured at 130-415 residues)"},
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


# ---------------------------------------------------------------------------
# PILOT — the guided starter recipe rendered by
# templates/components/pilot_card.html.
#
# NO PRICE AND NO RUNTIME STRING BELONGS IN THIS DICT. Both are derived
# at render time (blueprints/tools.py::_pilot_context) from
# shared.wallet_estimates.estimated_cost_for_tool over ``params`` and
# from the preset runtime map above. A hand-written second rate card
# drifts off the real one within a month.
#
# ``params`` keys are FORM FIELD NAMES. The same dict pre-fills the
# form via ?pilot=1 and feeds the estimator, and the form posts those
# same names to /api/wallet/estimate — so the card's price and the
# form's live price cannot disagree. Only include keys the form
# actually honours through pre_value()/pre_checked(); a key no field
# reads is a pre-fill that silently does nothing.
# ---------------------------------------------------------------------------
PILOT: dict | None = {
    "label": "Starter pilot: one shard, 8 designs",
    "goal": (
        "Run the smallest complete unit of a Proteina search against "
        "your own target and see what the reward stack returns."
    ),
    "you_need": (
        "A structure file for your target and its chain ID &mdash; or a "
        "chain and residue range such as <code>A1-150</code>. Hotspot "
        "residues are optional, and are what aims the binder at one "
        "specific face."
    ),
    "params": {
        "preset": "protein_binder",
        "num_designs": "8",
    },
    "next_step": (
        "8 designs is one shard on one GPU. Raise the count and the run "
        "fans out across GPUs as a campaign bounded by your wallet, "
        "with a single ranked list pooled across every shard."
    ),
}


# ---------------------------------------------------------------------------
# EXAMPLE — one real past run, rendered by
# templates/components/worked_example.html. None here, deliberately:
# A real payload exists on disk (proteina_direct_out/smoke_result.json,
# 2026-08-06) and is NOT shippable: it pre-dates the pLDDT polarity fix
# in #129 (merged 9fbe547, 2026-08-09), so its af2_plddt column holds
# 1 - pLDDT on every candidate. Publishing an inverted confidence column
# as a worked example would teach the metric backwards. Its scores are
# also degenerate (af2_iptm 0.09 to 0.10 across all 8, binder_scrmsd 34
# to 41 A), so it is not a picture of a working run either. Capture a
# post-fix delivered shard and this becomes a one-file change.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = None
