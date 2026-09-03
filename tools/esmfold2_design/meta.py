"""Static reference metadata for ESMFold2 design.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/boltz2/meta.py`` etc.

Shapes
------
    PRESET_RUNTIME       — {preset_slug: {"typical_minutes": str}}.
    paper_citation       — short inline citation.
    paper_url            — paper PDF / preprint URL.
    github_url           — upstream repo.
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.

Open thread
-----------
    Strict-pass thresholds (minibinder ``iptm > 0.75``, scfv
    ``cdr_distogram_iptm_proxy > 0.5``) are conservative starting points,
    not paper-derived. Tune after the first 8-seed sweep against PD-L1
    surfaces real ipTM distributions on each preset. Minibinder iPTM
    was raised from 0.55 on 2026-06-03 after early runs showed real
    designs sitting at 0.83-0.95 with the gate admitting too much noise.
"""

from __future__ import annotations

from typing import Optional


PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "minibinder": {"typical_minutes": "~10"},
    "scfv": {"typical_minutes": "~12"},
}

paper_citation: str = "Chan Zuckerberg Biohub, 2026"
paper_url: str = "https://biohub.ai/papers/esm_protein.pdf"
github_url: str = (
    "https://github.com/evolutionaryscale/esm/blob/main/cookbook/"
    "tutorials/binder_design.ipynb"
)
comparison_one_liner: str = (
    "You want a paired heavy + light scFv — a single-chain antibody "
    "fragment — with all six binding loops designed against your "
    "target at once, which no other tool here does. It also builds "
    "small de novo binders by a different route to RFdiffusion, "
    "worth a run when a target has gone quiet. For single-domain "
    "nanobodies use RFantibody."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Designs a binder by running a structure predictor backwards: "
        "it starts from a soft, blurred sequence and nudges it one "
        "gradient step at a time until the fold network believes the "
        "result binds your target. The same machinery does two jobs — "
        "small de novo binders, and a paired heavy + light scFv (a "
        "single-chain antibody fragment) with all six binding loops "
        "designed together, which no other tool here does. Designs from "
        "this method have been taken to the bench against PDGFRB, EGFR, "
        "PD-L1, CD45 and CTLA4, reaching nanomolar affinity and "
        "functional activity. ESMFold2 design, Chan Zuckerberg Biohub "
        "2026, built on the ESMC protein language model."
    ),
    "when_to_use": [
        (
            "You want a paired heavy + light scFv with all six binding "
            "loops designed jointly against your target. No other tool here "
            "does this."
        ),
        (
            "Your target has gone quiet under RFdiffusion and you want a "
            "method that searches differently — a different prior often "
            "surfaces different binders."
        ),
        (
            "You want to compare designed loops across three humanised "
            "frameworks that have been to the bench (trastuzumab, "
            "atezolizumab, ocankitug)."
        ),
        (
            "You want to calibrate your own wet-lab setup against one of "
            "the five targets from the paper before spending on a novel "
            "one."
        ),
    ],
    "prerequisites": [
        "Target sequence: pick one of five paper-validated presets or "
        "paste a single chain (30 to 800 aa).",
        "Pick minibinder mode or scFv mode. For scFv, pick a framework "
        "(trastuzumab, atezolizumab, or ocankitug).",
        "No PDB required. The gradient loop is sequence-only.",
    ],
    "inputs": [
        {
            "name": "Preset",
            "explanation": (
                "<strong>De novo minibinder</strong> generates a free "
                "60 to 200 aa scaffold with an isoelectric-point filter "
                "(pI &lt; 6). <strong>scFv</strong> designs all six CDRs "
                "on a locked humanized framework. Same model, different "
                "binder factory."
            ),
        },
        {
            "name": "Target",
            "explanation": (
                "Pick one of five paper-validated presets (CD45, CTLA4, "
                "EGFR, PD-L1, or PDGFR, with sequences from UniProt "
                "cropped to the relevant ectodomain) or paste your own "
                "protein sequence (30 to 800 aa, canonical amino acids "
                "only)."
            ),
        },
        {
            "name": "Binder framework",
            "explanation": (
                "scFv mode only. Locks the framework backbone and "
                "sequence; the gradient loop only mutates the six CDR "
                "regions. <strong>Trastuzumab</strong> (anti-HER2 IgG1, "
                "humanized), <strong>atezolizumab</strong> (anti-PD-L1 "
                "IgG1, humanized), or <strong>ocankitug</strong> "
                "(humanized IgG1). All three are clinically validated "
                "frameworks."
            ),
        },
        {
            "name": "Starting seed",
            "explanation": (
                "Integer seed for the soft-sequence initialization. "
                "Different seeds yield different designs. When "
                "<strong>Seeds to run</strong> is greater than 1 this "
                "is the first seed in the sweep; the orchestrator runs "
                "<code>[seed, seed + n)</code> in parallel."
            ),
        },
        {
            "name": "Seeds to run",
            "explanation": (
                "Number of parallel seeds to sweep (1 to 64). Each seed "
                "gets its own H100 worker, all run in parallel, so a "
                "16-seed sweep finishes in the same wall-clock as one "
                "seed (~10 to 15 min). Results from every seed merge "
                "into one globally-ranked table. Use this when you need "
                "to build a candidate library against a target. Cost "
                "scales linearly with seeds x batch size."
            ),
        },
        {
            "name": "Batch size",
            "explanation": (
                "Designs produced per gradient run (1 to 6). All designs "
                "share one ~10 min H100 pass, so a higher batch "
                "multiplies candidates without multiplying wall-clock. "
                "<strong>Default 3.</strong> Single-design runs often "
                "return <code>drop</code> after the iPTM and pI gates. "
                "Bump to 6 for first-pass exploration; drop to 1 only "
                "when you already know the target gives clean hits."
            ),
        },
        {
            "name": "Use scaling critics",
            "explanation": (
                "Optional. Loads the 15-checkpoint ESMFold2 scaling "
                "ensemble for stricter ranking. Adds the distogram "
                "iPTM proxy alongside the real iPTM. Roughly doubles "
                "host memory; off by default."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "minibinder", "typical": "~10 min/design"},
        {"preset": "scfv", "typical": "~12 min/design"},
    ],
    "output_summary": (
        "Per-design table with designed sequence, iPTM, distogram iPTM "
        "proxy (or CDR distogram iPTM proxy for scFvs), final loss, "
        "isoelectric point, source seed, and predicted complex PDB. "
        "Strict-pass classification surfaces designs worth ordering "
        "(minibinder: <code>iptm &gt; 0.75</code> AND "
        "<code>pI &lt; 6</code>; scfv: "
        "<code>cdr_distogram_iptm_proxy &gt; 0.5</code>). Sweep mode "
        "(<strong>Seeds to run</strong> &gt; 1) merges every seed's "
        "designs into one globally-ranked table."
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
    "label": "A guided first run",
    "goal": (
        "See what a single gradient-design run produces before "
        "committing to a parallel sweep. One seed is already the "
        "smallest run this tool offers, so these are the form&rsquo;s "
        "own defaults &mdash; a guided first run at the tool&rsquo;s "
        "normal cost, not a cheaper trial."
    ),
    "you_need": (
        "A target sequence &mdash; one of the bundled presets, or a "
        "single chain of 30 to 800 residues pasted in. No structure "
        "file required."
    ),
    # Identical to the form's defaults, and measured to be unavoidable.
    # Cost scales on n_seeds (one H100 container per seed) and n_seeds=1
    # is both the form default and the field minimum, so $9.87 is the
    # floor. batch_size does not move the price at all — 1, 2 and 3 all
    # cost $9.87 — so dropping it would return fewer designs for the
    # same money. Nothing on this form buys a cheaper first run.
    "params": {
        "preset": "minibinder",
        "n_seeds": "1",
    },
    "next_step": (
        "Raise the seed count. Every seed gets its own GPU, so a sweep "
        "finishes in about the same wall-clock time as one seed and "
        "costs proportionally more."
    ),
}


# ---------------------------------------------------------------------------
# EXAMPLE - one real past run, narrated, rendered by
# templates/components/worked_example.html. The output beside it is
# tools/esmfold2_design/example/result.json replayed through this tool's OWN
# results partial, so the demo can never drift from the real job page.
#
# PROVENANCE. One `minibinder` run at n_seeds=2 on the same Modal app this
# form submits to, pulled from the jobs table by
# scripts/capture_example_result.py (job 2b917b54, 306 GPU-seconds).
#
# This supersedes the note that stood here refusing an example. That refusal
# was right about its own payload: a single design scored ipTM 0.956 while
# being a junk poly-leucine bundle, and publishing 0.956 beside it would have
# taught a reader to trust the number. This run has TWO designs - the same
# artifact at 0.956, and a real one at 0.935 that the filter passed - and with
# both on screen the number teaches the opposite lesson, which is the right
# one.
#
# Every designed sequence was dropped at capture (--drop-sequence) under the
# no-designed-sequences rule: 9 fields across designs[] and candidates[] plus
# the top-level best_sequence. The scores that decide the run are all kept.
# The target is PD-L1, which is safe to name because it is one of this form's
# built-in preset targets - public by construction, not a customer's.
#
# COST is compute_charge_usd(306, "H100") - the charge, not the raw Modal
# cost. tests/test_worked_examples.py recomputes it from the rate card.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "PD-L1, the checkpoint protein &mdash; picked from this form's "
        "built-in target list rather than uploaded, so there was no structure "
        "to prepare."
    ),
    "why_this_target": (
        "PD-L1 is about as well-trodden as a binder target gets, which makes "
        "it the right place to show what this tool's scores do and do not "
        "settle. Two seeds is the smallest run that could show it at all, and "
        "this one happens to show it clearly."
    ),
    "inputs_used": [
        (
            "Target preset",
            "PD-L1 (Q9NZQ7, 17 to 132), immune checkpoint",
            "A built-in target, so there was no structure to upload or "
            "prepare &mdash; usually the slowest part of a first run. The "
            "preset covers the IgV domain, which is the face a binder needs.",
        ),
        (
            "Binder mode",
            "De novo minibinder (60 to 200 aa)",
            "A small de novo binder rather than an antibody. The tool judges "
            "antibodies on different criteria, so this choice changes which "
            "columns come back &mdash; the pI column below is a minibinder "
            "column.",
        ),
        (
            "Seeds to run",
            "2",
            "Two independent starting points. Each seed explores separately, "
            "so two seeds is two genuinely different attempts rather than two "
            "variations on one &mdash; which is what makes the comparison "
            "below meaningful.",
        ),
    ],
    "what_came_back": (
        "Two designs, both scoring far above the ipTM band that would have "
        "decided them if ipTM had been the deciding column. "
        "Seed 0 came back at <strong>ipTM 0.956</strong>, the better of the "
        "two. Seed 1 came back at 0.935. "
        "<strong>The tool passed seed 1 and dropped seed 0.</strong> "
        "The reason is in the <code>pI</code> column: seed 1 has an isoelectric "
        "point of 5.67, and seed 0 has one of <strong>11.95</strong>."
    ),
    "how_to_read_it": (
        "<code>pI</code> is a hard gate and it is checked <em>first</em>: a "
        "design with pI 6 or above is dropped whatever its ipTM. Only for "
        "the designs that clear it do the ipTM bands apply &mdash; "
        "<code>&ge; 0.75</code> passes strictly, <code>&ge; 0.70</code> comes "
        "back <code>borderline</code>, and below that it drops. So seed 0 was "
        "not out-scored by seed 1. It was disqualified before its 0.956 "
        "counted for anything. "
        "A pI near 12 means a strongly positively charged peptide at "
        "the pH of every buffer you own. Those stick to things &mdash; "
        "membranes, columns, the wrong protein &mdash; and they read as "
        "binders in an assay for reasons that have nothing to do with the "
        "site you aimed at. You do not need to see the sequence to act on "
        "that; the <code>pI</code> column has already told you. "
        "<strong>The higher score is the one you must not order.</strong> "
        "A 0.02 difference in ipTM is noise; the gap between pI 5.67 and 11.95 "
        "is the entire decision. "
        "So read <code>pI</code> first and treat the score column as a "
        "tie-breaker among the designs that survive it &mdash; never the other "
        "way round. Sorting this table by ipTM puts the worst design on top. "
        "There is deliberately no pass/fail column here to read instead. This "
        "tool&rsquo;s gate changes shape with the mode &mdash; an scFv is decided "
        "on a CDR proxy, a minibinder on ipTM <em>and</em> pI &mdash; and a "
        "single column claiming to summarise both would have printed "
        "&ldquo;meets&rdquo; over seed 0, the design this whole example exists "
        "to tell you not to order."
    ),
    "what_we_did_next": (
        "Seed 1 is the one worth anything here, and a two-seed run that yields "
        "one usable design is a signal to widen rather than to proceed: the "
        "same settings at more seeds give a set to choose among instead of a "
        "single survivor. Seed 0 is worth keeping in view as a reminder of "
        "what a top score can look like &mdash; the next junk design will "
        "score just as well, and the pI column will be the thing that catches "
        "it again."
    ),
    "cost_usd": "1.26",
    "runtime": "5 minutes",
}
