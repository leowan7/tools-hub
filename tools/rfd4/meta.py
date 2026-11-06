"""Static reference metadata for the RFD4-Proteina tool (slug ``rfd4``).

Plain data only. The About panel, the citation block, the pilot card and
the worked example import these constants; the :class:`ToolAdapter` and
its registration contract live in ``__init__.py`` and nothing here
imports it.

Shapes
------
    paper_citation       — short inline citation.
    paper_url            — the preprint PDF on the authors' own site.
    github_url           — upstream RosettaCommons repository.
    comparison_one_liner — what you have / what you get, plus which
                           sibling tool to reach for instead. Rendered on
                           the About panel and read by
                           shared/tool_chooser.py.
    example_output_id    — optional job_id of a public demo run to link
                           to. None: no rfd4 job exists in this
                           platform's jobs table. The run narrated in
                           ``EXAMPLE`` below was a container-validation
                           probe driven by a standalone Modal app
                           (docs/scoping/rung4-probe/rung4_app.py), not a
                           job, so it has no id to link to.
"""

from __future__ import annotations

from typing import Optional

# THE PAPER IS A PREPRINT WITH NO DOI. Read first-hand off the upstream
# README (lines 222-235 of RosettaCommons/RFD4-Proteina at
# aaccdd2ad0720c180468df5f9cf037a713d403e5), whose BibTeX entry gives
# first author Corley, year 2026, note "Preprint", and the URL below.
#
# ``REQUIRED_URL_TOKENS["rfd4"]`` in
# tests/test_citations_name_the_right_model.py pins this link on the path
# "assets/paper/RFD4-Proteina.pdf". That is the first entry in that map
# whose paper token is a URL PATH rather than a persistent identifier:
# every other entry pins a DOI or a bioRxiv id, which survives the
# publisher moving the file, and this one does not. The site is GitHub
# Pages (the PDF answered 200 with Server: GitHub.com on 2026-11-05), so
# an upstream repository reshuffle breaks it silently. Replace both the
# URL and that token with the DOI if this preprint ever gets one.
paper_citation: str = "Corley et al., preprint 2026"
paper_url: str = "https://rfd4-proteina.org/assets/paper/RFD4-Proteina.pdf"
github_url: str = "https://github.com/RosettaCommons/RFD4-Proteina"

# The one-model claim is the Hugging Face model card for nvidia/RFD4-Proteina,
# quoted in docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md §1: "generates
# protein sequences and all-atom structures together". The two sibling
# clauses are the Output row of that document's §1.1 table -- our
# rfdiffusion returns a backbone with MPNN and AF2 run in its container,
# our proteina returns a backbone plus its own downstream designability
# stack.
#
# NO COMPARATIVE PERFORMANCE CLAIM BELONGS HERE. The upstream benchmark
# does rank this model above the generators we already sell (§3 of the
# same document), and Leo's decision of 2026-11-05 was that no surface
# may say so: the figures are the authors' own, and rfd4 has produced no
# binder for a customer here.
comparison_one_liner: str = (
    "You have a target structure and want a binder that arrives whole — "
    "RFD4-Proteina writes the sequence and the all-atom structure in a "
    "single model, so there is no separate sequence-design step to run "
    "afterwards. It returns no confidence number: judging the complex is "
    "a fold run of its own. For a route that sequences and refolds the "
    "backbone for you, so each candidate arrives with an AlphaFold2 "
    "confidence score, use RFdiffusion; for a backbone with its own "
    "designability screen use Proteina; for an antibody or nanobody "
    "format use RFantibody or IgGM."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page and by
# templates/tool_guide.html.
#
# ``output_summary`` STATES NO PASS BAR, and that is the shape rather
# than an omission: rfd4 has no entry in shared/score_legends.py
# ::GATE_COLUMNS and none in shared/result_columns.py, so there is no
# legend for a figure here to agree with
# (tests/test_guide_bar_matches_legend.py is what ties the two together
# for the tools that do have one).
about: dict = {
    "what_it_is": (
        "Designs de-novo protein binders against a patch of your target "
        "structure, and writes each binder's sequence and its full "
        "atomic structure in one model — so a design comes back complete "
        "rather than as a backbone waiting for a sequence step. "
        "RFD4-Proteina, Corley et al., preprint 2026."
    ),
    "when_to_use": [
        (
            "You want a small de-novo binder against a protein target, "
            "not an antibody format."
        ),
        (
            "You would rather have sequence and structure out of one "
            "model than run a separate sequence-design step."
        ),
        (
            "You have your own way of judging a complex — this tool "
            "hands back structures and no score."
        ),
    ],
    "prerequisites": [
        "Target structure (<code>.pdb</code> / <code>.cif</code>).",
        "Chain ID of the target. One chain only.",
        "At least one hotspot residue on that chain.",
    ],
    "inputs": [
        {
            "name": "Hotspot residues",
            "explanation": (
                "Residues on the target chain the binder should grip, in "
                "the uploaded file's own numbering. At least one is "
                "required."
            ),
        },
        {
            "name": "Binder length (residues)",
            "explanation": (
                "The length range to sample a binder from. Anywhere "
                "between 20 and 300 residues; the default range is 60 to "
                "120."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many binders to generate. Each one is an "
                "independent sample, and each is delivered as a "
                "structure file holding your target chain plus the "
                "binder."
            ),
        },
    ],
    "output_summary": (
        "One structure per design, each holding your target chain and "
        "the de-novo binder beside it, downloadable from a run of your "
        "own. There are no score columns: nothing in this run measures "
        "how well a binder holds, so take the structures into a "
        "predictor before choosing between them."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# ---------------------------------------------------------------------------
# PILOT — the guided starter recipe rendered by
# templates/components/pilot_card.html.
#
# NO PRICE AND NO RUNTIME STRING BELONGS IN THIS DICT.
#
# ``params`` keys are FORM FIELD NAMES. The same dict pre-fills the form
# via ?pilot=1 and feeds the estimator, so only keys the form honours
# through pre_value()/pre_checked() belong here --
# tests/test_pilot_recipes.py::TestPilotPrefillActuallyLands fails on a
# key no field reads. ``num_designs`` is read through pre_value in
# templates/tools/rfd4_form.html; ``preset`` is deliberately absent,
# because the form posts the only preset this tool accepts as a hidden
# input and pre-filling it would change nothing.
# ---------------------------------------------------------------------------
PILOT: dict | None = {
    "label": "Trial run: 2 binders",
    "goal": (
        "Find out whether the face you picked can be gripped at all, "
        "before paying for a full sweep."
    ),
    "you_need": (
        "A structure file for your target (.pdb or .cif), the chain ID of "
        "the chain you want bound, and at least one residue on it."
    ),
    # 2, against the form's default of 8. The charge scales on the design
    # count -- shared/wallet_estimates.py TOOL_SPECS["rfd4"] has
    # scaling_param="n_designs_total" and designs_per_run_baseline=8 --
    # so a quarter of the default count is the cheapest honest look at
    # the target that still returns more than one sample to compare.
    "params": {
        "num_designs": "2",
    },
    "next_step": (
        "Two binders tell you the run completes on your target and what "
        "shape of thing comes back. They cannot tell you which one binds: "
        "no number here ranks them. Open both, check the binder sits on "
        "the residues you named, then clone the run at a larger count and "
        "fold the complexes before reading anything into them."
    ),
}


# ---------------------------------------------------------------------------
# EXAMPLE — one real past run, rendered by
# templates/components/worked_example.html against the payload in
# example/result.json.
#
# WHAT THIS RUN WAS, stated because the narration below would otherwise
# read as a customer job. It was rung 4 of the validation ladder in
# docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md §6: a real GPU run of
# our own container image, driven by a standalone Modal app rather than
# by this form, on the PD-L1 binder condition spec that ships inside the
# upstream image. The console artefact is
# docs/scoping/rung4-probe/gpu_result_20261103T114437.json -- rc 0,
# wall_s 217.4, eight CIFs under inference_samples/pdl1_binder_{0..7}/,
# which are the eight design stems in example/result.json. No customer
# was charged for it and no rfd4 job row exists.
#
# ONLY NARRATE WHAT THE PAGE ACTUALLY SHOWS. The partial renders rank,
# name and the structure download, and nothing else: every design in the
# payload carries exactly those three keys. The run's own
# per_example_metrics.csv holds inline metrics, and none of them reaches
# this page, so none is quoted below. Its header row is also misaligned
# against its data row where the quoted metrics_tags field spans commas,
# which is a second reason not to lift a figure out of it.
#
# NO CONFIDENCE FIGURE AND NO RANKING LANGUAGE. ``ranked=false`` in
# templates/tools/rfd4_results.html, because the pipeline's rank is
# completion order within a sweep rather than a quality order
# (tools/rfd4/run_pipeline.py:1053-1055).
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "Human PD-L1, taken as chain A of <strong>PDB 4ZQK</strong> "
        "&mdash; the solved PD-1/PD-L1 complex. One hotspot residue, 56."
    ),
    "why_this_target": (
        "This run was ours but it was not a customer's: it is the "
        "validation run that proved the container works, driven straight "
        "from the binder condition spec that ships inside the upstream "
        "image rather than from the form above. So read it for the "
        "<em>shape</em> of what comes back, eight complete structures and "
        "no scores, and not as a result on a target of your own."
    ),
    "inputs_used": [
        (
            "Target PDB",
            "PD-L1 chain A, 115 residues",
            "Supplied to the container as the upstream spec's own "
            "annotated structure rather than as an upload, because this "
            "run predates the form. A run of your own uploads a .pdb or "
            ".cif here and the normalizer does the rest.",
        ),
        (
            "Target chain",
            "A",
            "One chain, and only one: the run designs against the chain "
            "you name here and refuses a request that names more than "
            "one.",
        ),
        (
            "Hotspot residues",
            "56",
            "A single residue on the PD-1 face. One is enough to point "
            "the binder at a patch; more narrows it further.",
        ),
        (
            "Binder length (residues)",
            "60-120",
            "The default range. Each design samples a length inside it, "
            "so the eight structures are not all the same size &mdash; "
            "and a binder can come back longer than the target, which is "
            "why the results page names the target chain instead of "
            "telling you to take the shorter one.",
        ),
        (
            "Number of designs",
            "8",
            "Eight independent samples. Unlike the antibody tools there "
            "is no multiplier hiding here: eight designs is eight "
            "structures and eight rows.",
        ),
    ],
    "what_came_back": (
        "<strong>Eight structures, all eight of the eight asked for.</strong> "
        "Each is one file holding chain A and the de-novo binder beside "
        "it. The table has no score columns, because there are no scores "
        "to put in them: every delivered design carries its position, its "
        "name and its structure, and nothing more."
    ),
    "how_to_read_it": (
        "The reading is structural. Open each file, find the chain "
        "that is not A, and check it actually sits against residue 56 "
        "rather than somewhere else on the surface. Eight samples at one "
        "setting will not all do that."
    ),
    "what_we_did_next": (
        "For this run, nothing: it existed to prove the container, and it "
        "did. On a target of your own the next step is the one this page "
        "cannot do for you &mdash; fold each complex through a predictor, "
        "let that give you the confidence number, and only then decide "
        "which handful is worth a larger sweep or a bench. Eight "
        "unscored structures are a starting point, not a shortlist."
    ),
    # 217 GPU-seconds on A100-80GB, the gpu_class in
    # shared/wallet_estimates.py TOOL_SPECS["rfd4"], is what
    # shared.wallet.compute_charge_usd settles at $0.38 for.
    # tests/test_worked_examples.py::
    # test_recorded_cost_is_what_this_tool_would_charge carries the 217 in
    # its ``_EXAMPLE_GPU_SECONDS`` table and recomputes this, so it fails
    # rather than going stale if the rate card or the tool's GPU class
    # moves. That guard walks the slugs the registry hands the app, so it
    # will start reading this entry at step 7A and not before. It is the charge a
    # customer would have paid, not what this validation run cost us.
    "cost_usd": "0.38",
    "runtime": "4 minutes",
    # Read by components/worked_example.html:92 into the stub job's
    # created_at, where the only consumer is the date-gated era notice
    # (components/score_era_notice.html). Taken from the probe artefact's
    # own filename stamp, gpu_result_20261103T114437.json. That stamp is
    # LOCAL time, not UTC: the scope document records the artefact as
    # "written 11:44:37 local" with local = UTC-5
    # (docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md:627), which also
    # explains the artefact's own 16:42:01 internal log line. So the
    # trailing Z here is the house format of the sibling meta modules and
    # was wrong by five hours; the value below is the converted instant,
    # 11:44:37 - (-5) = 16:44:37Z, and the DATE is unchanged either way.
    # Only the date is load-bearing, and rfd4 has no era caveat to gate (no rfd4 key in
    # shared/score_legends.py), so the clock is not customer-visible.
    "ran_on": "2026-11-03T16:44:37Z",
}
