"""Extended PXDesign metadata for UI panels.

Phase 2 frontend work exposes richer "About this tool" context on the
form page (and the underlying comparison/citation text referenced from
results). The core ``ToolAdapter`` in ``base.py`` is shared across all
tools and intentionally minimal, so per-tool narrative metadata lives
here and is rendered directly by the PXDesign templates.

Citations reflect what the pipeline code actually does, not what would
be plausible. PXDesign itself is a Ranomics-internal binder-design
pipeline (private repo at ``llm-proteinDesigner/backend/pipelines/``);
its scoring stage runs JAX AF2 in initial-guess mode per Bennett et al.
(2023), which is the citation a protein engineer would expect to see.
"""

from __future__ import annotations


# Underlying model — PXDesign is a Ranomics in-house pipeline that
# wraps an RFdiffusion-style backbone generator with JAX AF2 Initial
# Guess (AF2-IG) validation. The generator is private; the AF2-IG
# scoring stage is published.
paper_citation: str = (
    "Bennett, N. R., Coventry, B., Goreshnik, I., et al. "
    "\"Improving de novo protein binder design with deep learning.\" "
    "Nature Communications 14, 2625 (2023). "
    "Ranomics in-house pipeline; scoring stage uses AF2 Initial Guess."
)

paper_url: str = "https://www.nature.com/articles/s41467-023-38328-5"

# PXDesign source lives in the private Kendrew repo
# (llm-proteinDesigner/backend/pipelines/pxdesign.py). No public repo.
github_url: str = ""

# One-line decision helper shown in the "About" panel.
comparison_one_liner: str = (
    "You have a target and you want every single candidate to "
    "arrive with a real AlphaFold2 confidence score against that "
    "target, not a cheaper stand-in. This is the pipeline Ranomics "
    "runs for its own wet-lab campaigns. For design without that "
    "filtering step use BindCraft; for antibody formats use "
    "RFantibody or IgGM."
)

# Optional reference job id that the form page can link to as an
# example output. Populated when a showcase run exists.
example_output_id: str | None = None


# Runtime + cost reference rendered as a table on the form page.
# Values mirror the ``Preset`` tuples in ``__init__.py`` and the
# ``GPU_TIMEOUT`` map in ``gpu/modal_client.py``.
preset_runtime_rows: tuple[dict[str, str], ...] = (
    {
        "slug": "pilot",
        "label": "Pilot",
        "runtime": "30 to 60 min",
        "target": "Your uploaded target",
    },
)


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Designs binders against your target and then scores every one "
        "of them with AlphaFold2 run against that same target, so "
        "nothing reaches you on a cheaper stand-in number. The scoring "
        "uses AlphaFold2's initial-guess mode, which is far quicker "
        "than a full multimer prediction and is what makes "
        "per-candidate scoring affordable at all. This is the pipeline "
        "Ranomics runs for its own wet-lab campaigns. PXDesign is "
        "in-house; the initial-guess method is Bennett et al., "
        "<em>Nature Communications</em> 2023."
    ),
    "when_to_use": [
        (
            "You want an AlphaFold2 confidence number against your actual "
            "target on every candidate, not on a filtered subset."
        ),
        (
            "You want that scoring faster than a full AlphaFold2 multimer "
            "run on each design."
        ),
        (
            "You want the same pipeline Ranomics runs for its own wet-lab "
            "campaigns."
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
                "Comma-separated target-chain residues defining the "
                "epitope. Click in the 3D viewer to toggle."
            ),
        },
        {
            "name": "Binder length",
            "explanation": (
                "Residue count for the generated binder. ~40 residues "
                "is the validated default; PD-L1 published binders are "
                "in this range."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many candidates to score. Higher counts increase "
                "cost and runtime linearly."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "pilot", "typical": "30 to 60 min"},
    ],
    "output_summary": (
        "Ranked candidates with ipTM, pLDDT, pAE, and downloadable PDBs. "
        "Target ipTM &ge; 0.70 on 1 to 2 of 5 designs for a tractable "
        "epitope."
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
    "label": "Starter pilot: 4 designs",
    "goal": (
        "See whether your target and the face you picked give designs "
        "with usable AF2 confidence, before scaling."
    ),
    "you_need": (
        "A structure file for your target (.pdb or .cif), the chain ID, "
        "and at least one residue on the face you want bound."
    ),
    "params": {
        "preset": "pilot",
        "num_designs": "4",
    },
    "next_step": (
        "If any of the four comes back with good interface confidence, "
        "clone the run and raise the design count."
    ),
}


# ---------------------------------------------------------------------------
# EXAMPLE — one real past run, narrated, rendered by
# templates/components/worked_example.html. The output beside it is
# tools/pxdesign/example/result.json replayed through this tool's OWN results
# partial, so the demo can never drift from the real job page.
#
# PROVENANCE. One `pilot`-tier call against ranomics-pxdesign-prod — the same
# Modal app this form submits to. It was one of four calls in a 100-design
# round, one call per binder length; the payload here is that ONE call,
# because job.result is per submission and pooling all four would render a
# shape no single job produces. Rebuilt from the campaign's own result by
# scripts/_build_pxdesign_example.py; re-run it to re-derive every number
# here. Scores only: no designed sequences, no structures, target unnamed.
#
# ONE DELIBERATE EDIT. That campaign applied a third filter after these two —
# a clash check against a sugar the target carries — which is meaningless off
# that target and would identify it. Designs are marked pass/below-threshold
# here on the two general filters only, so the counts below are the two-filter
# counts and will not match the campaign's own reports.
#
# COST CORRECTED 2026-08-21. This first shipped at $1.42, which is the
# campaign's own record of the RAW Modal cost for the call. What a reader of
# this page would be charged is that figure through the wallet — see
# shared.wallet.WALLET_MARKUP — so the published price was 18% under what the
# run would actually settle at. It is now recomputed from the same 1380 GPU-
# seconds against this tool's rate-card entry, and
# tests/test_worked_examples.py recomputes it again on every run so a
# rate-card change fails a test instead of leaving a stale price in front of
# a customer.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "A two-chain human secreted protein, about 210 residues per chain, "
        "from a solved crystal structure. Six hotspot residues, three on "
        "each chain, across the two-fold interface where the chains meet."
    ),
    "why_this_target": (
        "The binding site sits on a symmetry axis, so a binder has to reach "
        "both chains at once rather than settle on either one. That is a "
        "harder ask than a single flat face, and it is why so much of this "
        "round fails in the specific way shown below."
    ),
    "inputs_used": [
        (
            "Target PDB",
            "the two-chain crystal structure",
            "Both chains staged together. Handing over one chain would let "
            "the model design against half a site that does not exist on "
            "its own.",
        ),
        (
            "Hotspot residues",
            "6 residues, 3 per chain",
            "The face we wanted engaged, given symmetrically so neither "
            "chain reads as the whole target.",
        ),
        (
            "Binder length",
            "63",
            "One of four calls we made at 50, 57, 63 and 70. The pilot "
            "tier draws a fresh seed per call, so fanning out over lengths "
            "buys seed diversity and length coverage from four calls.",
        ),
        (
            "Number of designs",
            "25",
            "Enough to see the shape of the failure, cheap enough to "
            "throw away. 100 across the four calls.",
        ),
    ],
    "what_came_back": (
        "25 designs, of which <strong>2 passed</strong>. The best scored "
        "ipTM 0.88 at pLDDT 88, with the re-folded complex landing 1.64 &Aring; "
        "from where the generator put it and 577 &Aring;&sup2; of surface "
        "buried against the target. "
        "The other 23 are the point of showing you this. The median pLDDT "
        "in this call is 91 &mdash; high &mdash; while the median ipTM is "
        "0.14. <strong>21 of the 25 designs fold beautifully and do not "
        "bind anything.</strong> All four calls looked like this; 8 of the "
        "full 100 passed."
    ),
    "how_to_read_it": (
        "Those two columns answer different questions and this round is what "
        "it looks like when you confuse them. pLDDT is the model's confidence "
        "in the shape of the binder considered alone; ipTM is its confidence "
        "that the binder and the target form the complex you asked for. A "
        "design can be a textbook helical bundle and dock nowhere near the "
        "site, and 21 of these are exactly that. Sort by ipTM, never by "
        "pLDDT. "
        "The filter behind the pass/below-threshold column is the tool "
        "re-folding each design from scratch and checking it lands where the "
        "generator claimed: ipTM at or above 0.50 and the re-folded complex "
        "within 3.0 &Aring;. That second half is what bites &mdash; across "
        "the round the median complex misses by 13.7 &Aring;, while all 8 "
        "survivors land "
        "inside 2.3 &Aring;. A design that clears the first and fails the "
        "second has folded correctly in the wrong place."
    ),
    "what_we_did_next": (
        "Kept the 8 and re-ran at longer binder lengths. Passes by length "
        "here were 0, 2, 2 and 4 out of 25 at 50, 57, 63 and 70 residues, "
        "which is 25 per bin &mdash; suggestive, nowhere near enough to call "
        "a length effect, and the reason the next round was a scale-up rather "
        "than a conclusion. If your own pilot returns a page of high pLDDT "
        "and low ipTM, that is not a broken run: it is this result, and the "
        "answer is more designs or a different site, not a different setting."
    ),
    "cost_usd": "1.68",
    "runtime": "23 minutes",
}
