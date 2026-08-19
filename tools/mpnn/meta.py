"""Static reference metadata for the ProteinMPNN (D1) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/bindcraft/meta.py`` etc.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
    paper_citation    — short inline citation.
    paper_url         — bioRxiv / Science permalink.
    github_url        — upstream ProteinMPNN repository.
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
    example_output_id — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

from shared.wallet import SIGNUP_CREDIT_USD

# The signup credit is quoted in SEO copy that reaches JSON-LD structured
# data, so it is read from the grant rather than retyped. It was hardcoded
# as "$5" here and stayed that way when the grant went to $15.
_SIGNUP_CREDIT: str = f"${SIGNUP_CREDIT_USD:.0f}"

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "standalone": {"typical_minutes": "1"},
}

paper_citation: str = "Dauparas et al., Science 2022"
paper_url: str = "https://www.science.org/doi/10.1126/science.add2187"
github_url: str = "https://github.com/dauparas/ProteinMPNN"

seo_faq: list[dict] = [
    {
        "q": "Can I run ProteinMPNN online without a local GPU?",
        "a": (
            "Yes. Upload a backbone PDB and Ranomics Tools runs ProteinMPNN "
            "on a dedicated GPU in seconds. You get ranked sequence "
            "redesigns plus per-position recovery, with no install and no "
            "CUDA setup."
        ),
    },
    {
        "q": "How much does one ProteinMPNN job cost?",
        "a": (
            "Billing is by the second. A typical ProteinMPNN job costs a "
            "fraction of a cent because the model finishes in under a "
            "minute on most backbones. New accounts start with a "
            f"{_SIGNUP_CREDIT} wallet balance, which is enough for "
            "thousands of runs."
        ),
    },
    {
        "q": "Does ProteinMPNN need an MSA?",
        "a": (
            "No. ProteinMPNN is a structure-conditioned sequence model. "
            "It takes the backbone coordinates directly and never queries "
            "an MSA. That makes it the fastest route from a designed "
            "backbone to a sequence you can synthesise."
        ),
    },
]

comparison_one_liner: str = (
    "You have a backbone — a 3D shape with no sequence decided yet "
    "— and need amino-acid sequences that will fold into it. Ranked "
    "candidates come back in about 30 seconds. To generate the "
    "backbone in the first place, run a binder design tool and feed "
    "its PDB in here."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page. Field shape and
# rendering rules are documented at the top of about_panel.html.
about: dict = {
    "what_it_is": (
        "You give it a backbone — a 3D protein shape with no sequence "
        "decided yet — and it proposes an amino acid for every "
        "position, chosen so the sequence should fold back into that "
        "exact shape. It reads only the backbone atoms, so side chains "
        "in your file are ignored. Each candidate comes back with a "
        "score and, on a natural backbone, the fraction of the real "
        "sequence it recovered. ProteinMPNN, Dauparas et al., "
        "<em>Science</em> 2022."
    ),
    "when_to_use": [
        (
            "You have a backbone and need sequences for it."
        ),
        (
            "You want to re-sequence a binder another tool here designed, "
            "before folding or ordering it."
        ),
        (
            "You want several alternative sequences threaded through a "
            "curated structure so you can choose between them."
        ),
    ],
    "prerequisites": [
        "Backbone PDB or mmCIF (only C&alpha; and backbone atoms are used).",
        "Chain ID(s) of the region(s) to redesign. Other chains stay fixed as context.",
    ],
    "inputs": [
        {
            "name": "Chains to design",
            "explanation": (
                "Which chains in the PDB MPNN should redesign "
                "(e.g. <code>A</code>, <code>A B</code>, <code>H L</code>). "
                "Other chains are held fixed as context."
            ),
        },
        {
            "name": "Number of sequences",
            "explanation": (
                "How many independent samples to draw (1 to 1000). Each "
                "sample is independent; rank by score and ProteinMPNN "
                "recovery rate."
            ),
        },
        {
            "name": "Sampling temperature",
            "explanation": (
                "Lower means more conservative (closer to argmax); higher "
                "means more diverse. Defaults to 0.1 per the upstream "
                "README."
            ),
        },
        {
            "name": "Fixed positions (optional)",
            "explanation": (
                "Positions to hold <strong>fixed</strong> inside a designed "
                "chain; everything else in that chain is redesigned. Written "
                "as <code>CHAIN:list</code> groups with single positions or "
                "ranges &mdash; <code>A:1-44,46-66 B:5,7</code>. Positions are "
                "1-indexed <em>within their chain</em>, not author residue "
                "numbers, so the chain must be numbered from 1 with no gaps "
                "or insertion codes. Use this to redesign a liability patch "
                "while leaving a binding interface untouched: list the "
                "complement of the patch. Leave blank to redesign the whole "
                "chain."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "standalone", "typical": "~1 min"},
    ],
    "output_summary": (
        "Ranked candidate sequences with per-position score and overall "
        "ProteinMPNN recovery, downloadable as FASTA. Pair downstream "
        "with AlphaFold2 or ColabFold to confirm the predicted fold."
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
    "label": "Starter check: 8 sequences",
    "goal": (
        "Confirm your backbone file parses and you named the right "
        "chains, before sampling a full batch."
    ),
    "you_need": (
        "A backbone structure file (.pdb or .cif) and the chain ID(s) "
        "you want new sequences for. Every other chain stays fixed as "
        "context."
    ),
    "params": {
        "preset": "standalone",
        "num_seq_per_target": "8",
        "sampling_temp": "0.1",
    },
    "next_step": (
        "Raise the sequence count to 50 or more. Raise the sampling "
        "temperature too if the first eight came back near-identical to "
        "each other."
    ),
}


# ---------------------------------------------------------------------------
# EXAMPLE — one real past run, narrated, rendered by
# templates/components/worked_example.html. The output beside it is
# tools/mpnn/example/result.json replayed through this tool's OWN results
# partial, so the demo cannot drift from the real results page.
#
# EVERY NUMBER BELOW IS A RECORDED FACT FROM THAT RUN, not an estimate and
# not an illustration. Provenance: job `smoke-1777396479`, 2026-04-28, the
# baked 1HEW smoke fixture; the same scores are logged against job
# `smoke-1777047396` in docs/VALIDATION-LOG.md. Nothing may be added here
# that the archived payload does not support — an invented recovery figure
# on a public page is worse than no example at all.
#
# No cost_usd: that run was smoke tier at zero credits, so there is no
# dollar figure to quote and none is invented. The field is optional.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "Hen egg-white lysozyme &mdash; PDB <code>1HEW</code>, chain A, "
        "129 residues."
    ),
    "why_this_target": (
        "Its real sequence is known, so every sequence the model writes "
        "can be scored against the one nature uses. A de-novo backbone "
        "has nothing to compare against, which makes a solved structure "
        "the only way to see whether the model is behaving."
    ),
    "inputs_used": [
        (
            "Backbone",
            "1HEW.pdb",
            "The crystal structure, downloadable below. Only the backbone "
            "atoms are read &mdash; the sequence in the file is discarded "
            "before design.",
        ),
        (
            "Chain(s) to design",
            "A",
            "The single protein chain. 1HEW's other chain is a sugar "
            "ligand, not protein.",
        ),
        (
            "Sequences to sample",
            "2",
            "Deliberately tiny. This run existed to prove the pipeline "
            "worked end to end, not to produce a design set.",
        ),
    ],
    "runtime": "24 seconds end to end, cold container included",
    "what_came_back": (
        "Two sequences, 129 residues each, recovering 53% and 50% of the "
        "native lysozyme sequence at scores of 0.76. The archived payload "
        "pre-dates the fields that record the designed chain and the "
        "sampling temperature back into the result, so those two read as "
        "em-dashes in the table below; everything else is as it was "
        "returned."
    ),
    "how_to_read_it": (
        "Recovery is the fraction of positions where the model picked the "
        "residue nature actually uses. The score legend on this page puts "
        "the usable line at 0.4 and excellent at 0.6, so about 0.5 on a "
        "real backbone is healthy &mdash; you would be suspicious of 0.95, "
        "which usually means the native sequence leaked into the run. "
        "Score runs the other way: it is the model's negative "
        "log-likelihood per residue, so lower is more confident, and below "
        "1.0 is excellent."
    ),
    "what_we_did_next": (
        "Nothing &mdash; this run existed to prove the pipeline. On a real "
        "backbone the next step is to raise the sequence count to 50 or "
        "more, then fold the best few with ESMFold or ColabFold to see "
        "which of them actually adopt the shape you designed them for."
    ),
    "structure_file": "1HEW.pdb",
}
