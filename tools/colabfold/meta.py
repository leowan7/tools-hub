"""Static reference metadata for the ColabFold (D3) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/mpnn/meta.py``.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
    paper_citation    — short inline citation.
    paper_url         — Nature Methods / bioRxiv permalink.
    github_url        — upstream ColabFold repository.
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
    example_output_id — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "standalone": {"typical_minutes": "1 to 2"},
}

paper_citation: str = "Mirdita et al., Nature Methods 2022"
paper_url: str = "https://www.nature.com/articles/s41592-022-01488-1"
github_url: str = "https://github.com/sokrypton/ColabFold"
comparison_one_liner: str = (
    "You have a sequence and want its 3D shape in a minute or two, "
    "trading a little accuracy for speed. It skips the search for "
    "related natural sequences that full AlphaFold2 runs. Use it to "
    "triage a batch; use AlphaFold2 when the answer has to be "
    "right."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Predicts a structure from a sequence in one to two minutes by "
        "skipping the search for related natural sequences that full "
        "AlphaFold2 runs. Same AlphaFold2 weights, less evidence to "
        "work from — so a little less accurate and a lot faster. Useful "
        "for triaging a batch of sequences or getting a quick look at a "
        "well-behaved fold. ColabFold, Mirdita et al., <em>Nature "
        "Methods</em> 2022."
    ),
    "when_to_use": [
        (
            "You want a structure in a couple of minutes and can live with "
            "slightly less accuracy than full AlphaFold2."
        ),
        (
            "You are folding a batch of sequences one after another and "
            "throughput matters more than the last few points of "
            "confidence."
        ),
        (
            "Your target is a well-behaved single chain or small complex "
            "with no unusual chemistry."
        ),
    ],
    "prerequisites": [
        "Single-letter FASTA sequence(s).",
        "Targets with deep evolutionary signal. Multi-domain or low-information sequences underperform without MSA.",
    ],
    "inputs": [
        {
            "name": "Sequence",
            "explanation": (
                "Paste FASTA. Use <code>:</code> as a chain separator "
                "for multimers."
            ),
        },
        {
            "name": "Recycles",
            "explanation": (
                "Model recycles. ColabFold default is 3; reduce for "
                "speed if your target's fold is well-known."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "standalone", "typical": "1 to 2 min"},
    ],
    "output_summary": (
        "Predicted PDB with per-residue pLDDT and PAE. Download as "
        "PDB or PAE matrix for filtering."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# ---------------------------------------------------------------------------
# No pilot. See templates/components/pilot_card.html — the card simply
# does not render.
# ---------------------------------------------------------------------------
# A 1 to 2 minute no-MSA fold with no scale parameter to start
# small on. Nothing for a pilot tier to reduce.
PILOT: dict | None = None


# ---------------------------------------------------------------------------
# EXAMPLE - one real past run, narrated, rendered by
# templates/components/worked_example.html. The output beside it is
# tools/colabfold/example/result.json replayed through this tool's OWN results
# partial, so the demo can never drift from the real job page.
#
# PROVENANCE. One `standalone` run on the same Modal app this form submits to,
# pulled from the jobs table by scripts/capture_example_result.py (job
# 9beb0103, 55 GPU-seconds). Re-run that script against the same job to
# re-derive every figure below.
#
# The folded sequence was a ProteinMPNN DESIGN, so it was dropped at capture
# (--drop-sequence) under the no-designed-sequences rule. Every score it
# earned is kept; the narration describes the sequence without publishing it.
# pae_matrix_b64 went too - 22 KB for the specialist pAE panel, which this
# partial renders conditionally. plddt_per_residue is kept, because it IS this
# example.
#
# UNITS. ColabFold reports pLDDT on 0-100 and this partial prints mean_plddt
# raw, so the page shows 61.05. The sibling ESMFold page reports the same
# metric on 0-1. Quote what THIS page renders.
#
# COST is compute_charge_usd(55, "A100-40GB") - the charge, not the raw Modal
# cost. tests/test_worked_examples.py recomputes it from the rate card.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "Not a natural protein &mdash; a 101-residue sequence ProteinMPNN had "
        "just written for a backbone, brought straight here to be checked."
    ),
    "why_this_target": (
        "This is the step that catches design failures before they cost "
        "anything at the bench. ProteinMPNN scores its own output, but those "
        "numbers only say the sequence suits the backbone it was handed "
        "&mdash; they are not a prediction that it folds. Re-folding it here, "
        "with a model that never saw that backbone, is an independent check. "
        "This run is one that did not come back clean."
    ),
    "inputs_used": [
        (
            "Preset",
            "Standalone, one FASTA",
            "One design, folded on its own. The batch preset takes a whole "
            "set of MPNN outputs in a single submission, which is the usual "
            "way to run this once the loop is worth having.",
        ),
        (
            "FASTA",
            "101 aa, one chain",
            "The top-ranked MPNN sequence for that backbone, pasted as FASTA.",
        ),
        (
            "Number of recycles",
            "2",
            "How many times the model refines its own answer. More recycles "
            "let a borderline fold settle and cost proportionally more; 2 is "
            "enough to see whether a design is in trouble.",
        ),
        (
            "Use PDB templates",
            "off",
            "Left off on purpose. Handing the model the backbone the design "
            "was built for would be marking its own homework &mdash; the "
            "point is an independent opinion.",
        ),
    ],
    "what_came_back": (
        "A mean pLDDT of <strong>61.05</strong> and a pTM of 0.62 &mdash; "
        "under any threshold you would set, a fail. "
        "But the mean is the wrong summary here, and opening "
        "<em>Per-residue pLDDT</em> shows why. The first "
        "<strong>22 residues</strong> are red, every one below 50. The other "
        "79 average 67.3 and climb to <strong>89.9</strong> at residue 50, "
        "with 27 residues at or above 70. <strong>This is not a design that "
        "failed everywhere. It is a folded core with a 22-residue tail "
        "hanging off the front.</strong>"
    ),
    "how_to_read_it": (
        "One number for a whole chain averages together the parts the model "
        "is sure of and the parts it is not, and the two failures that hides "
        "need opposite responses. Uniformly low across the whole strip means "
        "the sequence does not fold and the design is dead. Low at one end "
        "and high through the middle &mdash; this run &mdash; means most of "
        "the design is fine and a specific, locatable piece of it is not. "
        "<strong>The mean cannot tell those apart; the strip can.</strong> "
        "Read the trace before deciding the fate of a design, because the "
        "difference between them is a redesign versus a 22-residue edit. "
        "It is still a fail as submitted: a floppy 22-residue terminus is a "
        "protease target and it will not behave in an assay. But you now know "
        "which 22 residues, which is a different morning's work."
    ),
    "what_we_did_next": (
        "The move a result like this points to is to trim the tail and re-fold. "
        "If the remaining 79 residues hold up alone, the core was real and the "
        "terminus was the whole problem. If the core collapses without it, the "
        "fold depended on that tail after all and it is the backbone that "
        "needs revisiting, not the sequence. Either way it is one more cheap "
        "fold before anything gets ordered, which is the reason this step is "
        "in the loop at all."
    ),
    "cost_usd": "0.07",
    "runtime": "55 seconds",
}
