"""Static reference metadata for IgGM antibody / nanobody design.

Kept separate from ``__init__.py`` (adapter registration) so About panels,
citation blocks, and cost previews import plain-data constants without
touching the adapter contract. Parallel to ``tools/boltz2/meta.py``.
"""

from __future__ import annotations

from typing import Optional


PRESET_RUNTIME: dict[str, dict[str, object]] = {
    # Advisory only; refit from the canary I-* runs before flag-on.
    "complex_prediction": {"typical_minutes": "~2"},
    "cdr_design": {"typical_minutes": "~3"},
    "fr_design": {"typical_minutes": "~3"},
    "affinity_maturation": {"typical_minutes": "scales with samples x masked positions"},
    "inverse_design": {"typical_minutes": "~2"},
}

paper_citation: str = "Wang et al., ICLR 2025"
paper_url: str = "https://www.biorxiv.org/content/10.1101/2024.09.19.613838v1"
github_url: str = "https://github.com/TencentAI4S/IgGM"
comparison_one_liner: str = (
    "You have an antibody or nanobody and an antigen, and you want "
    "to redesign its binding loops, humanise its framework, raise "
    "its affinity, or just see how the two dock — one model does "
    "all of it, aimed at the epitope you name. For a nanobody from "
    "scratch use RFantibody; for a paired heavy and light antibody "
    "fragment use ESMFold2 design."
)
example_output_id: Optional[str] = None


about: dict = {
    "what_it_is": (
        "Takes an antibody or nanobody and an antigen and does whatever "
        "you need to the pair: redesign the binding loops (the CDRs), "
        "rebuild or humanise the framework around them, raise affinity "
        "from a wild-type starting point, recover a sequence from a "
        "structure, or simply predict how the two dock. All of it is "
        "aimed at the epitope you name, and all of it comes out of one "
        "model rather than a chain of them. IgGM, Wang et al., "
        "<em>ICLR</em> 2025."
    ),
    "when_to_use": [
        (
            "You already have an antibody or nanobody and want its binding "
            "loops, or its framework, rebuilt against a specific antigen "
            "and epitope."
        ),
        (
            "You want to humanise a framework, or push affinity up from a "
            "wild-type reference."
        ),
        (
            "You want to see how an antibody sits on its antigen before "
            "committing to a wet-lab campaign."
        ),
    ],
    "prerequisites": [
        "Antigen structure as PDB or mmCIF (the antigen sequence is read "
        "from this file — you do not type it).",
        "Antibody heavy chain sequence (>H); light chain (>L) optional "
        "(omit it for a nanobody / VHH). Mark positions to design with X.",
        "Optional: an epitope — click antigen residues on the structure and "
        "IgGM guides design toward them.",
    ],
    "inputs": [
        {
            "name": "Antibody FASTA",
            "explanation": (
                "Paste the heavy chain as <code>&gt;H</code> and, for a "
                "conventional antibody, the light chain as <code>&gt;L</code>. "
                "Mark residues to design with <code>X</code>. Omit "
                "<code>&gt;L</code> for a nanobody / VHH. Do not include an "
                "antigen record — it comes from the uploaded PDB."
            ),
        },
        {
            "name": "Antigen PDB",
            "explanation": (
                "Upload the target as .pdb / .cif. The antigen sequence is "
                "extracted from the chain you select, so the structure is the "
                "single source of truth."
            ),
        },
        {
            "name": "Antigen chain",
            "explanation": (
                "The chain ID in the uploaded PDB that IgGM should treat as "
                "the antigen (e.g. <code>A</code>)."
            ),
        },
        {
            "name": "Epitope",
            "explanation": (
                "Optional. Click residues on the antigen structure; IgGM "
                "guides design toward them. Positions are handled correctly "
                "regardless of the PDB's residue numbering."
            ),
        },
        {
            "name": "Mode",
            "explanation": (
                "<strong>Complex prediction</strong> folds the complex; "
                "<strong>CDR design</strong> / <strong>framework redesign</strong> "
                "redesign masked (X) positions; <strong>affinity maturation</strong> "
                "generates improved variants from a wild-type reference; "
                "<strong>inverse design</strong> recovers sequence from the backbone."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "complex_prediction", "typical": "~2 min"},
        {"preset": "cdr_design", "typical": "~3 min"},
        {"preset": "fr_design", "typical": "~3 min"},
        {"preset": "affinity_maturation", "typical": "scales with samples x masked positions"},
        {"preset": "inverse_design", "typical": "~2 min"},
    ],
    "output_summary": (
        "Per design: the predicted antibody-antigen complex PDB, the designed "
        "sequence, and an epitope-contact count (how many of your chosen "
        "epitope residues the designed antibody engages). Sequence statistics "
        "and amino-acid distribution plots are attached as artifacts."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# Load Example chips are retired platform-wide (Leo, 2026-07: no one should
# spend GPU on an example). The IgGM form does not render an examples chip.
# Kept as an empty list only so the generic tool_form route's
# ``getattr(meta, "examples", [])`` stays well-defined. Canary inputs (the
# IgGM-bundled 8iv5 / 8hpu complexes) are exercised Modal-side from the cloned
# repo, not bundled here.
examples: list[dict] = []


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
    "label": "A guided first run",
    "goal": (
        "Before designing anything, check that IgGM can place your "
        "existing antibody on your antigen. A single sample in "
        "prediction mode is already the smallest run IgGM offers, so "
        "these are the form&rsquo;s own defaults &mdash; a guided first "
        "run at the tool&rsquo;s normal cost, not a cheaper trial."
    ),
    "you_need": (
        "Your antigen structure file, and your antibody heavy chain "
        "sequence. The light chain is optional &mdash; omit it for a "
        "nanobody."
    ),
    # Identical to the form's defaults, and measured to be unavoidable.
    # complex_prediction is the first preset (so already checked) and
    # the only single-pass one; num_samples=1 is the field minimum. The
    # whole run is $0.08, which is the floor for this tool. Nothing on
    # this form buys a cheaper or smaller first run.
    "params": {
        "preset": "complex_prediction",
        "num_samples": "1",
    },
    "next_step": (
        "If the predicted complex looks right, switch the mode to CDR "
        "design and mark the positions you want redesigned with X."
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
# ONLY NARRATE COLUMNS THE PAGE ACTUALLY SHOWS. shared/result_columns.py
# gives iggm exactly one scored column, ["epitope_contacts"], so there is no
# second metric to cross-check a design against and a leaderboard reading of
# this run is nearly worthless. The narration below therefore leans on the
# two things the partial renders BESIDE that column: the distribution across
# all 40 designs, and the per-position contact grid
# (templates/tools/iggm_results.html, "Epitope contacts (top N designs)").
#
# THE GRID SHOWS ONLY THE TOP 12. Positions 63, 64 and 66 are empty for all
# 40 designs, and they are also empty in every one of the 12 rows the grid
# draws, so the claim below is visible on the page and not just true in the
# payload. It was checked both ways before being written.
EXAMPLE: dict | None = {
    "target": (
        "Hen egg-white lysozyme, chain A of <strong>PDB 1HEW</strong>, 129 "
        "residues &mdash; with a nine-residue epitope named on its surface: "
        "63 to 68 and 73 to 75."
    ),
    "why_this_target": (
        "IgGM is not a de novo binder tool, so a worked example needs a real "
        "antibody as well as a real antigen. It takes an antibody you already "
        "have and rewrites its CDR loops; there is nothing for it to do "
        "without one. Lysozyme is the textbook antigen for exactly this work "
        "&mdash; small, rigid and thoroughly characterised &mdash; and the "
        "nine residues are one contiguous surface patch, which is the shape "
        "of thing you would type if you had mapped an epitope and wanted the "
        "loops aimed at it."
    ),
    "inputs_used": [
        (
            "Mode",
            "CDR design",
            "Redesigns the loops and leaves the framework alone. The other "
            "modes rebuild the framework instead, or only predict the complex "
            "without designing anything.",
        ),
        (
            "Antibody FASTA",
            "two records, H (114 aa) and L (107 aa)",
            "The heavy chain carries five X characters, and X is the mask: "
            "those are the positions IgGM is free to rewrite. A design mode "
            "given an input with no X in it has nothing to design.",
        ),
        (
            "Antigen PDB",
            "the 1HEW file",
            "Chain A is the lysozyme, numbered 1-129 as it downloads.",
        ),
        (
            "Antigen chain",
            "A",
            "Names the chain the epitope numbers below refer to.",
        ),
        (
            "Epitope residues (required)",
            "63 64 65 66 67 68 73 74 75",
            "Space separated, in the file's own numbering: Trp63, Cys64, "
            "Asn65, Asp66, Gly67 and Arg68, then Arg73, Asn74 and Leu75. "
            "Worth knowing before you read the result &mdash; this is a "
            "request, not a constraint. Nothing forces a design to land here.",
        ),
        (
            "Samples",
            "40",
            "Forty independent designs. Worth this many because the outcome "
            "per design is close to all-or-nothing, so the useful answer is "
            "the spread across a batch rather than any one design.",
        ),
    ],
    "what_came_back": (
        "40 designs in 12 minutes, each scored by how many of the nine "
        "requested epitope residues it actually contacts. "
        "<strong>Nineteen of the forty reach no part of the "
        "epitope.</strong> Fourteen reach exactly one residue. The rest "
        "go further &mdash; three touch two, one "
        "touches four, two touch five, and the best design touches six of "
        "nine."
    ),
    "how_to_read_it": (
        "There is one score column here, and reading it as a leaderboard "
        "throws away most of what this run told you. Two other things matter "
        "more. "
        "<strong>First, the shape.</strong> Nearly half the designs miss the "
        "epitope completely. That is an ordinary IgGM result, and it is why "
        "forty samples is the right order of magnitude rather than four: a "
        "run of four could easily have come back four zeros and told you "
        "nothing about your antibody or your epitope. "
        "<strong>Second, the contact grid below the table</strong>, which is "
        "the part worth the most and the part with no number attached. Its "
        "columns are your nine requested positions, and three of them "
        "&mdash; <strong>63, 64 and 66</strong> &mdash; are empty for every "
        "single design. Not rarely hit: never hit. Meanwhile position 73 is "
        "contacted by seventeen of the forty, and is the <em>only</em> "
        "contact five of them make. So the run is not sampling your "
        "nine-residue patch evenly. It is repeatedly finding one reachable "
        "sub-patch &mdash; 65, 67, 68, 73, 74 and 75 &mdash; while three "
        "residues sit somewhere a CDR loop on this framework does not get "
        "to. That is a finding about your epitope definition rather than "
        "about any design, and no ranked column would ever have shown it to "
        "you."
    ),
    "what_we_did_next": (
        "Took the empty columns as the actionable result, which is not where "
        "you would look if you had gone straight to the top row. The next run "
        "narrows the epitope request to the residues that proved reachable, "
        "so the sampling concentrates there instead of spending part of every "
        "design on positions that never come back. Folding the best design "
        "independently and taking it to expression is the step after that, "
        "not before it."
    ),
    "cost_usd": "0.87",
    "runtime": "12 minutes",
    # Read by components/worked_example.html into the stub job's created_at so
    # a date-gated era notice knows when this ran. Job created_at, matching
    # the rfdiffusion and bindcraft examples' convention.
    "ran_on": "2026-09-08T21:20:26Z",
}
