"""Extended RFdiffusion metadata for UI panels.

The core ``ToolAdapter`` in ``tools/base.py`` is intentionally minimal,
so per-tool narrative metadata lives here and is rendered directly by
the RFdiffusion templates.

Citations reflect what the pipeline code actually does. RFdiffusion is
the public Watson et al. (2023) diffusion-based backbone generator;
the wrapper run on Kendrew composes it with ProteinMPNN sequence
design and JAX AF2 multimer validation, so candidates carry real
ipTM / pLDDT / i_pAE statistics from the AF2 model.
"""

from __future__ import annotations

from shared.wallet import SIGNUP_CREDIT_USD

# The signup credit is quoted in SEO copy that reaches JSON-LD structured
# data, so it is read from the grant rather than retyped. It was hardcoded
# as "$5" here and stayed that way when the grant went to $15.
_SIGNUP_CREDIT: str = f"${SIGNUP_CREDIT_USD:.0f}"


# Underlying model — RFdiffusion is the public diffusion-based backbone
# generator from the Baker lab. Composite Kendrew pipeline pairs it
# with ProteinMPNN sequence design and AF2 multimer scoring.
paper_citation: str = (
    "Watson, J. L., Juergens, D., Bennett, N. R., et al. "
    "\"De novo design of protein structure and function with RFdiffusion.\" "
    "Nature 620, 1089 to 1100 (2023). "
    "Composite pipeline: RFdiffusion backbones, ProteinMPNN sequences, "
    "and AF2 multimer validation."
)

paper_url: str = "https://www.nature.com/articles/s41586-023-06415-8"

github_url: str = "https://github.com/RosettaCommons/RFdiffusion"


seo_faq: list[dict] = [
    {
        "q": "Can I run RFdiffusion online without installing it locally?",
        "a": (
            "Yes. Ranomics Tools runs the full RFdiffusion + ProteinMPNN + "
            "AF2 multimer pipeline on a dedicated GPU through your browser. "
            "You upload a target PDB, set hotspots and binder length, and "
            "candidates come back with real ipTM, pLDDT, and i_pAE scores."
        ),
    },
    {
        "q": "How much does one RFdiffusion run cost?",
        "a": (
            "Billing is by the second of dedicated GPU time. A pilot run "
            "(~25 minutes on an A100 for four designs) typically clears for "
            "under a few dollars from your wallet. New accounts start with a "
            f"{_SIGNUP_CREDIT} balance, which covers a first "
            "small-target run."
        ),
    },
    {
        "q": "Do I need to choose hotspots before running RFdiffusion?",
        "a": (
            "Hotspots are strongly recommended. They tell RFdiffusion "
            "which residues on the target the binder should contact. If you "
            "do not have a hotspot guess, run Epitope Scout first to score "
            "the target surface, then hand off the picked residues into "
            "the RFdiffusion form."
        ),
    },
]


# One-line decision helper shown in the "About" panel.
comparison_one_liner: str = (
    "You have a target structure and a patch of its surface you "
    "want gripped, and you want brand-new binders of whatever shape "
    "works — the general-purpose starting point for de novo design. "
    "Every candidate comes back with a real AlphaFold2 confidence "
    "score against your own target. For antibody or nanobody "
    "formats use RFantibody or IgGM instead."
)

# Optional reference job id linked from the form page as an example.
example_output_id: str | None = None


# Runtime + cost reference rendered as a table on the form page.
# Values mirror the ``Preset`` tuples in ``__init__.py`` and the
# ``PRESET_CAPS`` map in ``gpu/modal_client.py``.
# MEASURED, not estimated, and re-measured after the September 2026 container
# update roughly TRIPLED it. Job 25471e07 (4ZQK chain A, 8 designs) ran 2220
# GPU-seconds / 37 wall-clock minutes; the same shape of job before that update
# took 804. The AlphaFold re-score now fetches a real MSA for the target
# instead of folding it single-sequence, and that is where the time goes.
#
# Wall-clock and GPU-seconds are ~1:1 here (one GPU, one job), and the run
# splits into a fixed ~700 s of diffusion + MPNN plus ~190 s per design in AF2.
# So four designs is ~1460 s (~25 min) and eight is ~2220 s (~37 min), which is
# the band below.
#
# THREE PLACES ON ONE PAGE QUOTE THIS and they must agree -- this row, the
# "runtime_table" entry in the about-panel below, and the FAQ answer above.
# tools/boltzgen/meta.py carries the same warning because that page once
# quoted three different runtimes for one run.
preset_runtime_rows: tuple[dict[str, str], ...] = (
    {
        "slug": "pilot",
        "label": "Pilot",
        "runtime": "25 to 40 min (4 to 8 designs)",
        "target": "Your uploaded target",
    },
)


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Generates brand-new protein backbones — 3D shapes with no "
        "sequence yet — by starting from noise and denoising toward "
        "something that fits the patch of your target you named. "
        "Ranomics then puts a sequence on each backbone with "
        "ProteinMPNN and refolds the pair with AlphaFold2 multimer, so "
        "every candidate that reaches you carries a confidence score "
        "measured against your own target rather than an idealised one. "
        "This is the general-purpose starting point for de novo binder "
        "design. RFdiffusion, Watson et al., <em>Nature</em> 2023."
    ),
    "when_to_use": [
        (
            "You want general-purpose de novo binders, with confidence "
            "numbers that came from your actual target."
        ),
        (
            "Your target is an ordinary protein epitope with no sugars or "
            "modified residues."
        ),
        (
            "You want the binder's length and shape left open rather than "
            "locked to an antibody scaffold."
        ),
    ],
    "prerequisites": [
        "Target structure (<code>.pdb</code> / <code>.cif</code>).",
        "Chain ID of the target.",
        "At least one hotspot residue.",
    ],
    "inputs": [
        {
            "name": "Hotspot residues",
            "explanation": (
                "Comma-separated target-chain residues the binder "
                "should contact during diffusion."
            ),
        },
        {
            "name": "Binder length (min/max)",
            "explanation": (
                "How long the new binder should be. Each design draws "
                "its own length from this window. 55 to 65 is a compact "
                "single-domain binder and the right first try on a "
                "small, fairly flat patch; 90 to 150 suits a broad face "
                "or one sitting in a groove a short binder cannot reach "
                "across. Longer binders are harder to express at the "
                "bench, so lengthen only if nothing scores."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many candidates to generate. Each passes "
                "ProteinMPNN sequence design and AF2 multimer scoring."
            ),
        },
    ],
    "runtime_table": [
        # Must match preset_runtime_rows above and the cost FAQ. See the
        # measurement note there.
        {"preset": "pilot", "typical": "25 to 40 min (4 to 8 designs)"},
    ],
    "output_summary": (
        "Ranked candidates with ipTM, pLDDT, i_pAE, and downloadable "
        "PDBs. Aim for at least 1 in 5 with ipTM &ge; 0.65 on a "
        "tractable target before committing to a full pilot."
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
    "label": "Starter pilot: 8 binders",
    "goal": (
        "Find out whether your target and the face you picked are "
        "workable, before paying for a large run."
    ),
    "you_need": (
        "A structure file for your target (.pdb or .cif), the chain ID "
        "it sits on, and at least one residue on the face you want the "
        "binder to touch."
    ),
    "params": {
        "preset": "pilot",
        "num_designs": "8",
    },
    "next_step": (
        "Roughly 1 in 5 designs clears the confidence bar on a "
        "tractable target, so 8 is enough to tell. If one or two score "
        "well, clone the run and raise the design count. If none do, "
        "move the hotspots rather than scaling."
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
# Captured from job 25471e07 (2026-09-04). Every figure below is a recorded
# fact about THAT run -- read back off the payload and the wallet ledger, not
# estimated and not rounded to a number someone liked. The payload lives in
# example/result.json and is merged in by blueprints/tools._example_context.
#
# The target is a PUBLISHED structure and is named for that reason: 4ZQK is
# the human PD-1/PD-L1 complex and chain A is PD-L1. The publishing rule for
# these pages is scores and published references only, which is why the
# candidates carry rank and scores and nothing else -- the container also
# returns a designed `sequence` and a `pdb_key` per candidate, and both were
# dropped on capture.
#
# WHY THIS RUN AND NOT AN EARLIER ONE. Every RFdiffusion run before the
# September 2026 container update scored its designs against a target
# AlphaFold had rebuilt without an MSA, so their numbers are not measurements
# -- see RFDIFFUSION_SCORE_ERA_BOUNDARY in shared/score_legends.py. Publishing
# one of those would teach the tool backwards, which is the failure mode
# scripts/capture_example_result.py warns about in its own docstring. This is
# the first run on the fixed container.
EXAMPLE: dict | None = {
    "target": (
        "Human PD-L1, the IgV domain, taken as chain A of "
        "<strong>PDB 4ZQK</strong> &mdash; the solved PD-1/PD-L1 complex. "
        "Hotspots Ile54, Tyr56 and Met115."
    ),
    "why_this_target": (
        "Chain B of the same file is PD-1, the natural partner, so the three "
        "hotspots are not a guess &mdash; they are residues a real binding "
        "protein is known to cover. It is also the target the BoltzGen "
        "worked example uses, so the two pages can be read against each "
        "other: the same epitope, two different generators."
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
            "file is PD-1, and the run ignores it &mdash; otherwise the "
            "model would be designing into an occupied site.",
        ),
        (
            "Hotspot residues",
            "54, 56, 115",
            "Ile54, Tyr56 and Met115 in the file's own numbering. Three is a "
            "normal number: enough to aim the binder at one patch, few "
            "enough that you have not drawn the interface yourself.",
        ),
        (
            "Binder length (residues)",
            "55 to 65",
            "A window rather than a fixed number &mdash; RFdiffusion samples "
            "a length inside it. The default window, left alone.",
        ),
        (
            "Number of designs",
            "8",
            "A pilot-sized batch. Enough to tell whether the target and "
            "hotspots are workable before committing to 100+, which is what "
            "this preset is for.",
        ),
    ],
    "what_came_back": (
        "Eight designs, <strong>two of them above the bar</strong>. The best "
        "scores ipTM 0.88 with an interface pAE of 3.65 &Aring; and pLDDT "
        "95.5; the second is ipTM 0.82. The other six fall away from 0.46 to "
        "0.19."
    ),
    "how_to_read_it": (
        "Sort by ipTM: 0.65 or more is a credible binder, 0.75 or more is "
        "strong. Then read i_pAE beside it, because the two agreeing is what "
        "makes either believable &mdash; here the two passing designs sit "
        "under 4.3 &Aring; while everything below ipTM 0.5 is above 13 "
        "&Aring;, and that gap is the real separation in this table. "
        "<strong>Look at the spread, not only the top row.</strong> A run "
        "whose eight designs all score within a whisker of each other is "
        "usually telling you something went wrong upstream rather than that "
        "you have eight equally good binders; a run that fans out from 0.88 "
        "to 0.19 is one where the scoring actually discriminated."
    ),
    "what_we_did_next": (
        "Kept the two passing designs and dropped the other six. For those "
        "two the next step is an independent re-fold against the same target "
        "&mdash; a different model, so it is a real second opinion rather "
        "than the same one twice &mdash; and then SPR or BLI if they survive "
        "it. One pilot is a screen, not a result: two hits out of eight is "
        "roughly the yield this tool is scoped for, and the point of the "
        "pilot is to earn the bigger run."
    ),
    "cost_usd": "2.69",
    "runtime": "37 minutes",
    # Read by components/worked_example.html into the stub job's created_at,
    # so an era notice gated on the run date knows this run postdates the
    # container fix and stays silent. Without it the example warns about
    # itself.
    "ran_on": "2026-09-04T18:33:54Z",
}
