"""Static reference metadata for the RFantibody tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so Phase 2 "About" panels, citation blocks, and cost
previews can import plain-data constants without touching the adapter
contract. Other tools (BindCraft, BoltzGen, PXDesign) will grow their
own ``meta.py`` alongside this one.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
                         ``typical_minutes`` is a human-readable range (e.g.
                         ``"15-60"``) pulled straight from adapter copy.
    paper_citation    — short inline citation.
    paper_url         — Nature permalink for the RFantibody paper.
    github_url        — upstream RosettaCommons repo.
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
                         rendered on the About panel.
    example_output_id — optional job_id of a public demo run to link to
                         from the About panel. Phase 3 will populate this;
                         today it is None.
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "pilot": {"typical_minutes": "15 to 60"},
}

# THE PREPRINT THIS ONCE CITED IS NOW A JOURNAL ARTICLE, and the publisher
# is what ties the two together rather than a title match: the Crossref
# record for Nature 649, 183-193 (DOI 10.1038/s41586-025-09721-5) carries a
# ``has-preprint`` relation naming the exact bioRxiv DOI this field used to
# hold, and the preprint's own record carries the matching ``is-preprint-of``
# back, so this pair resolves from either end. BindCraft's does not resolve
# from either -- see its meta.py before relying on the relation for a sweep.
# Both fields moved to the journal version in one commit.
#
# THE ISSUE YEAR AND THE ONLINE YEAR DISAGREE HERE, which is why the number
# below is worth a note rather than a glance. nature.com's own "Cite this
# article" block reads "Nature 649, 183-193 (2026)"; the DOI's CSL rendering
# dates it to the online-first posting instead, one year earlier. The
# publisher's own instruction is what this follows. Do not realign it to a
# reference manager's output -- PAPER_YEAR records the same tiebreak, so
# changing one of them alone turns the suite red.
paper_citation: str = "Bennett et al., Nature 2026"
paper_url: str = "https://www.nature.com/articles/s41586-025-09721-5"
github_url: str = "https://github.com/RosettaCommons/RFantibody"
comparison_one_liner: str = (
    "You have a target structure and want nanobodies — "
    "single-domain antibodies you can carry straight into yeast "
    "display, mammalian display or a hybridoma workflow. Each "
    "candidate is refolded and scored against the target before you "
    "see it. For non-antibody mini-proteins use RFdiffusion or "
    "BindCraft; for sugar-coated targets use BoltzGen."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Designs nanobodies — single-domain antibodies, the VHH format "
        "— against a patch of your target, then refolds each one with "
        "AlphaFold2 and reports how confident it is about the contact. "
        "Nanobodies are the format that carries most easily into yeast "
        "display, mammalian display and hybridoma workflows, which is "
        "the usual reason to pick this over a de novo mini-protein. "
        "RFantibody, Bennett et al., Nature 2026."
    ),
    "when_to_use": [
        (
            "You want a nanobody scaffold rather than a de novo "
            "mini-protein."
        ),
        (
            "Your downstream work is yeast display, mammalian display or a "
            "hybridoma workflow."
        ),
        (
            "Your target is an ordinary protein epitope without heavy sugar "
            "coverage."
        ),
    ],
    "prerequisites": [
        "Target structure (<code>.pdb</code> / <code>.cif</code>).",
        "Chain ID of the target.",
        "At least one hotspot residue defining the epitope face.",
    ],
    "inputs": [
        {
            "name": "Hotspot residues",
            "explanation": (
                "Comma-separated target-chain residues defining the "
                "epitope the CDRs should target."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many backbones to generate. Each gets five "
                "ProteinMPNN sequences, and every sequence is "
                "re-folded and filtered by RoseTTAFold-2 on pAE, "
                "ipAE and pLDDT -- so 4 here returns 20 candidates."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "pilot", "typical": "15 to 60 min"},
    ],
    "output_summary": (
        "Ranked VHH candidates with pAE, pLDDT, ipAE, and "
        "PDBs downloadable from a run of your own. Filter at pAE &le; 5 / ipAE &le; 6 for "
        "downstream wet-lab work."
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
    "label": "Starter pilot: 2 nanobodies",
    "goal": (
        "Check that a VHH scaffold can be placed on the face you "
        "picked at all, before scaling up."
    ),
    "you_need": (
        "A structure file for your target (.pdb or .cif), the chain ID, "
        "and at least one residue defining the face you want bound."
    ),
    # 2, not the form's default of 4. RFantibody prices in whole
    # containers of two designs (wallet_estimates designs_per_run_
    # baseline=2), so 2 is one container and half the default's price,
    # while 1 costs the same as 2. A pilot equal to the default was a
    # button that promised a change it did not make.
    "params": {
        "preset": "pilot",
        "num_designs": "2",
    },
    "next_step": (
        "Two designs answer whether the epitope is reachable by a VHH "
        "at all, not whether it is a good one. Roughly 1 in 5 clears "
        "the confidence bar on a tractable target, so once these come "
        "back scored, clone the run and raise the count to 10 or more "
        "before reading anything into the numbers."
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
# gives rfantibody ["ipAE", "pLDDT", "pAE", "against_bar"], and all four are
# used below. framework, cdr_lengths, rf2_scored and next_steps are in the
# payload and NOT columns, so where the narration leans on them it says so in
# words rather than pointing at a column.
#
# THE 4 -> 20 IS REAL AND IS EXPLAINED ON PURPOSE. The form field is a
# BACKBONE count: stage 1 diffuses num_designs backbones, then stage 2 runs
# ProteinMPNN at seqs_per_backbone=5 (docker/rfantibody/run_pipeline.py), so
# four backbones score twenty candidates. A reader who takes the field at its
# old wording ("how many candidates to design") would under-estimate both the
# table length and the bill by five times, so the inputs_used note below
# carries it and the form help has been corrected to match.
EXAMPLE: dict | None = {
    "target": (
        "Human PD-L1, the IgV domain, taken as chain A of "
        "<strong>PDB 4ZQK</strong> &mdash; the solved PD-1/PD-L1 complex. "
        "Hotspots Ile54, Tyr56 and Met115."
    ),
    "why_this_target": (
        "The same target, chain and hotspots as the RFdiffusion, BoltzGen and "
        "BindCraft examples on this site. That makes four generators aimed at "
        "one epitope, and this is the only one of the four that answers with "
        "an <em>antibody</em> &mdash; a VHH nanobody, framework fixed, with "
        "only the three heavy-chain CDR loops designed. So the comparison "
        "worth making across those four pages is not which tool scores "
        "higher. It is what shape of molecule you get back for the same "
        "request."
    ),
    "inputs_used": [
        (
            "Target PDB",
            "the 4ZQK file, uploaded whole",
            "Both chains, exactly as it downloads from the PDB. Chain A keeps "
            "its crystal numbering 18-132, which is the numbering the hotspot "
            "field expects.",
        ),
        (
            "Target chain",
            "A",
            "Restricts the design to PD-L1. Chain B in the same file is PD-1, "
            "the natural partner; leaving it in the upload is harmless "
            "because the run only reads the chain you name here.",
        ),
        (
            "Hotspot residues",
            "54, 56, 115",
            "Ile54, Tyr56 and Met115 in the file's own numbering &mdash; the "
            "face PD-1 covers.",
        ),
        (
            "CDR lengths",
            "H1:8, H2:7, H3:10-16",
            "The three heavy-chain loops. H1 and H2 are pinned at their "
            "common lengths; H3 is given a range because it is the loop that "
            "does most of the binding, so its length is the thing worth "
            "sampling rather than fixing.",
        ),
        (
            "Number of designs",
            "4",
            "Four backbones &mdash; and this is the input to understand "
            "before you spend. The field sets the backbone count, not the "
            "candidate count: ProteinMPNN then designs five sequences on each "
            "one, so four here returned <strong>20</strong> scored candidates "
            "and twenty rows in the table below. Whatever you type, expect "
            "five times as many rows and roughly five times the work.",
        ),
    ],
    "what_came_back": (
        "20 candidates in 14 minutes. <strong>Five meet every bar; fifteen "
        "do not.</strong> Interface pAE runs 3.72 to 17.48 &Aring;, global "
        "pAE 2.68 to 9.84 &Aring;, and pLDDT 87 to 91."
    ),
    "how_to_read_it": (
        "Start with the column that does <em>not</em> help. pLDDT is 87 to 91 "
        "across all twenty, every failing candidate included, so all twenty "
        "clear the 80 bar and the column separates nothing at all. That is "
        "the normal case for an antibody run rather than a fault: the "
        "framework is a real nanobody scaffold, so of course it folds. "
        "Confidently folded is not bound. "
        "<strong>The column that separates is ipAE</strong>, and it splits "
        "cleanly instead of tailing off &mdash; five candidates between 3.72 "
        "and 7.69 &Aring;, then nothing whatsoever until 12.56, then fifteen "
        "more out to 17.48. A gap that wide is the real signal here. It says "
        "the five are a different outcome from the fifteen, not merely the "
        "top of a continuum, which is a much safer thing to act on than a "
        "ranking. Global pAE agrees exactly: under 5 &Aring; for all five, "
        "above 7 for every other one. The <em>vs. quality bar</em> column "
        "spells it out per row &mdash; every rejection names ipAE and pAE, "
        "and not one of them names pLDDT."
    ),
    "what_we_did_next": (
        "Treated the five as the only rows carrying information, and the size "
        "of the ipAE gap as the reason to trust that split rather than "
        "hedging down the list. On a real target the next run is more "
        "backbones at these same settings &mdash; remembering the five-to-one "
        "again, so the bill climbs faster than the number you type &mdash; "
        "then an independent re-fold of the few that clear the bar, then "
        "yeast display, and SPR or BLI only after that. Twenty candidates off "
        "four backbones is a screen, not a shortlist."
    ),
    "cost_usd": "1.01",
    "runtime": "14 minutes",
    # Read by components/worked_example.html into the stub job's created_at so
    # a date-gated era notice knows when this ran. Job created_at, matching
    # the rfdiffusion and bindcraft examples' convention.
    "ran_on": "2026-09-08T21:33:59Z",
}
