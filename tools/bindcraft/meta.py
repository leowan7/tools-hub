"""Static reference metadata for the BindCraft tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so Phase 2 "About" panels, citation blocks, and cost
previews can import plain-data constants without touching the adapter
contract. Parallel to ``tools/rfantibody/meta.py``.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
                         ``typical_minutes`` is a human-readable range (e.g.
                         ``"45"``) pulled straight from adapter copy.
    paper_citation    — short inline citation.
    paper_url         — bioRxiv permalink for the BindCraft paper.
    github_url        — upstream repository.
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
                         rendered on the About panel.
    example_output_id — optional job_id of a public demo run to link to
                         from the About panel. Phase 3 will populate this;
                         today it is None.
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. BindCraft ships only the ``pilot``
# preset; the pipeline cost floor is ~45 min on A100-80GB.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "pilot": {"typical_minutes": "45"},
}

paper_citation: str = "Pacesa et al., bioRxiv 2024"
paper_url: str = "https://www.biorxiv.org/content/10.1101/2024.09.30.615802v1"
github_url: str = "https://github.com/martinpacesa/BindCraft"

seo_faq: list[dict] = [
    {
        "q": "Can I run BindCraft online without installing it?",
        "a": (
            "Yes. Ranomics Tools runs the full BindCraft hallucination "
            "loop on a dedicated GPU through your browser. Upload a target "
            "PDB, pick hotspots and binder length, and candidates come "
            "back with AF2 ipTM and pLDDT on every hit."
        ),
    },
    {
        "q": "BindCraft vs RFdiffusion: which should I pick?",
        "a": (
            "Both design de novo binders against your target. BindCraft "
            "hallucinates sequences and backbones jointly inside AF2; "
            "RFdiffusion samples a backbone first then drops sequences in "
            "with ProteinMPNN. Run both on the same hub and compare ipTM "
            "before committing to wet-lab."
        ),
    },
    {
        "q": "How long does a BindCraft pilot take?",
        "a": (
            "Typical pilot runs finish in roughly 20 to 60 minutes on a "
            "dedicated A100, depending on target size and how many "
            "candidates pass the internal ipTM filter. Billing is by the "
            "second so a faster preset costs less."
        ),
    },
]

comparison_one_liner: str = (
    "You have a target structure and know roughly which patch of "
    "its surface you want gripped, and you want brand-new "
    "mini-proteins of 60 to 150 residues built to grip it. Every "
    "candidate is refolded and filtered before you see it, so what "
    "comes back is already a shortlist."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Designs brand-new mini-proteins that grip a patch of your "
        "target. It runs AlphaFold2 multimer backwards — pushing a "
        "random starting sequence toward one the model believes will "
        "bind the residues you named — then assigns a real sequence "
        "with ProteinMPNN and refolds every candidate to check the "
        "answer survives. What reaches you has already been filtered on "
        "interface confidence and fold quality. BindCraft, Pacesa et "
        "al., bioRxiv 2024."
    ),
    "when_to_use": [
        (
            "You have a target structure and at least one residue on its "
            "surface you want the binder to touch."
        ),
        (
            "You want a small de novo protein of 50 to 150 residues, not an "
            "antibody."
        ),
        (
            "You can wait about 45 minutes for a first run, and you would "
            "rather see a filtered shortlist than every candidate the run "
            "generated."
        ),
    ],
    "prerequisites": [
        "Target structure as <code>.pdb</code>, <code>.cif</code>, or <code>.mmcif</code>.",
        "Chain ID of the target within that structure.",
        "At least one hotspot residue index on the target chain.",
    ],
    "inputs": [
        {
            "name": "Hotspot residues",
            "explanation": (
                "Comma-separated target-chain residue indices the binder "
                "should contact (e.g. <code>54,56,115</code>). These "
                "bias AF2 backpropagation toward the intended epitope. "
                "Click residues in the 3D viewer to toggle them."
            ),
        },
        {
            "name": "Binder length (min/max)",
            "explanation": (
                "Residue-count window for the generated binder chain "
                "(50 to 150). Shorter binders are easier to validate "
                "in yeast display; longer ones can target larger interfaces."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many final filtered designs to return (1 to 5). "
                "Each passes AF2 re-prediction with ipTM and pLDDT above "
                "the BindCraft default thresholds. Pipeline cost floor "
                "is ~45 min regardless of count."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "pilot", "typical": "~45 min"},
    ],
    "output_summary": (
        "Filtered candidate binders with ipTM, pLDDT, shape complementarity, "
        "and downloadable PDBs. Hand off promising designs to the Ranomics "
        "yeast display CRO for in vitro validation."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
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
    "label": "Starter pilot: 2 trajectories",
    "goal": (
        "Check that your target parses, your hotspots resolve against "
        "it, and the pipeline returns scored designs &mdash; before "
        "committing to a run that hunts for hits."
    ),
    "you_need": (
        "A structure file for your target (.pdb, .cif or .mmcif), the "
        "chain ID it sits on, and at least one residue on the face you "
        "want the binder to touch."
    ),
    # 2, not the form's default of 4. BindCraft prices in whole
    # containers of two trajectories (wallet_estimates designs_per_run_
    # baseline=2), so 2 is one container — the smallest complete unit of
    # work — and exactly half the price of the default. A pilot that
    # restated the default was a button promising a change it did not
    # make.
    "params": {
        "preset": "pilot",
        "num_designs": "2",
    },
    "next_step": (
        "Two trajectories tell you the setup works, not whether the "
        "target is bindable &mdash; BindCraft burns more GPU per design "
        "than anything else here, which is why the first step is this "
        "small. Once it comes back clean, clone the run and raise the "
        "count."
    ),
}


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
# ---------------------------------------------------------------------------
# Worked example
# ---------------------------------------------------------------------------
# Captured from job 1c4d5803 (2026-05-28) with
# scripts/capture_example_result.py. Every figure is read back off that run:
# the scores from the payload in example/result.json, the runtime from the
# job row (1170 GPU-seconds, 19m57s wall clock), and the cost from the wallet
# ledger -- a $4.3697 hold with $2.325 released as surplus, so $2.0447 was
# actually charged. That reconciles with the rate card exactly:
# 1170 s * $0.001028 (A100-80GB) * 1.70 markup = $2.0447.
#
# The target is a PUBLISHED structure and is named for that reason: 4ZQK is
# the human PD-1/PD-L1 complex and chain A is PD-L1. This is the same target,
# chain and hotspots as the RFdiffusion and BoltzGen examples, deliberately.
# The publishing rule for these pages is scores and published references
# only, which is why the candidates carry rank and scores and nothing else.
#
# ONLY NARRATE COLUMNS THE PAGE ACTUALLY SHOWS. shared/result_columns.py
# gives bindcraft ["ipTM", "pLDDT", "RMSD", "shape_complementarity", "SAP"],
# so Target_RMSD, Hotspot_RMSD, pTM and i_pAE are in the payload and NOT on
# the page. A first draft of this narration leaned on Target_RMSD 0.38 as the
# did-the-target-refold sanity check -- which is the single most diagnostic
# number here, given that scoring against a misfolded target is exactly what
# was wrong with RFdiffusion before the September 2026 fix -- and pointed at
# an "interface pAE column" that does not exist. Both sent the reader hunting
# for a column that is not rendered. tests/test_worked_examples.py has that
# guard for INPUT field names and no equivalent for score columns.
#
# SAP IS NOT NARRATED, ON PURPOSE. The payload stores it as 0.29 and 0.30
# while this tool's legend calls "< 5 favourable", so the column reads
# favourable whatever the design does. That is the shape of the pxdesign pAE
# note in shared/score_legends.py -- a bar on one scale reading a value on
# another -- and it is why bindcraft has no GATE_COLUMNS entry to be wrong
# about. Teaching a reader to trust that column would be teaching them the
# bug, so the narration below stays off it. Fixing the scale is its own
# change; it is a threshold question, not a copy question.
EXAMPLE: dict | None = {
    "target": (
        "Human PD-L1, the IgV domain, taken as chain A of "
        "<strong>PDB 4ZQK</strong> &mdash; the solved PD-1/PD-L1 complex. "
        "Hotspots Ile54, Tyr56 and Met115."
    ),
    "why_this_target": (
        "The same target, chain and hotspots as the RFdiffusion and BoltzGen "
        "examples on this site, and that is the point &mdash; three different "
        "generators aimed at one epitope, so the three pages can be read "
        "against each other. Chain B of the same file is PD-1, the natural "
        "partner, so the hotspots are residues a real binding protein is "
        "known to cover rather than a guess."
    ),
    "inputs_used": [
        (
            "Target PDB",
            "the 4ZQK file, uploaded whole",
            "Both chains, exactly as it downloads from the PDB. No trimming "
            "and no renumbering, so chain A keeps its crystal numbering "
            "18-132 &mdash; which is the numbering the hotspot field "
            "expects.",
        ),
        (
            "Target chain",
            "A",
            "This is what restricts the design to PD-L1. Chain B in the same "
            "file is PD-1, and the run ignores it &mdash; otherwise the model "
            "would be designing into an already occupied site.",
        ),
        (
            "Hotspot residues",
            "54, 56, 115",
            "Ile54, Tyr56 and Met115 in the file's own numbering. Three is a "
            "normal number: enough to aim the binder at one patch, few enough "
            "that you have not drawn the interface yourself.",
        ),
        (
            "Binder length (min)",
            "50",
            "The bottom of the default window. BindCraft picks a length "
            "inside the range rather than being told one.",
        ),
        (
            "Binder length (max)",
            "100",
            "The top of the default window, left alone.",
        ),
        (
            "Number of designs",
            "2",
            "Small even for a pilot, and that is this tool rather than "
            "impatience: BindCraft optimises each design individually instead "
            "of sampling a batch, so two of them cost about what eight cost "
            "on the diffusion tools. It is also why this run took 20 minutes "
            "against the ~45 the runtime table above quotes &mdash; that row "
            "is for the form's default of four.",
        ),
    ],
    "what_came_back": (
        "Two designs, landing in almost the same place: ipTM "
        "<strong>0.75</strong> and <strong>0.76</strong>, pLDDT 81 for both, "
        "shape complementarity 0.64 and 0.60, and a refolding RMSD of "
        "3.04 and 2.96 &Aring;."
    ),
    "how_to_read_it": (
        "Read ipTM first: 0.75 is this tool's bar for a credible binder, so "
        "both designs sit <em>on</em> that bar rather than above it, and "
        "pLDDT 81 clears its own bar of 80 by about as little. Then read the "
        "two columns that stop you over-reading those. Shape complementarity "
        "wants 0.65 for an antibody-grade fit and both designs are under it; "
        "refolding RMSD wants to be inside 1.5 &Aring; and both are close to "
        "3. Put together: <strong>the interface is plausible, the fit is "
        "loose, and neither design rebuilds quite the backbone it was drawn "
        "as</strong>. That is a normal pilot result and it is worth more to "
        "you than a single headline number would be. "
        "<strong>Do not read the ranking as an ipTM sort.</strong> Design 2 "
        "has the marginally higher ipTM and still ranks second, because "
        "BindCraft ranks on a composite that includes the fit &mdash; and "
        "design 1 wins there, 0.64 against 0.60."
    ),
    "what_we_did_next": (
        "Treated this as a screen that came back amber rather than green. Two "
        "designs sitting exactly on the bar with a sub-par fit is a reason to "
        "run more of them, not a reason to order peptides. The next step is "
        "the same target and the same hotspots at a higher design count, then "
        "an independent re-fold on whatever clears the bar properly, and SPR "
        "or BLI only after that. A two-design pilot can tell you the target "
        "and hotspots are workable; it cannot tell you which binder to make."
    ),
    "cost_usd": "2.04",
    "runtime": "20 minutes",
    # Read by components/worked_example.html into the stub job's created_at,
    # so a date-gated era notice knows when this ran. This is the job's
    # created_at, matching the rfdiffusion example's convention.
    "ran_on": "2026-05-28T18:30:54Z",
}
