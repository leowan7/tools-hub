"""Static reference metadata for the AF2 standalone (D2) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/mpnn/meta.py``.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
    paper_citation    — short inline citation.
    paper_url         — Nature permalink.
    github_url        — upstream ColabFold repository (which bundles AF2).
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
    # Standalone: user FASTA, MMseqs2 MSA + 3 recycles. MSA fetch
    # dominates for short sequences; fold time scales with length.
    "standalone": {"typical_minutes": "5 to 10"},
}

paper_citation: str = "Jumper et al., Nature 2021 (AF2); Mirdita et al., Nature Methods 2022 (ColabFold)"
paper_url: str = "https://www.nature.com/articles/s41586-021-03819-2"
# ColabFold is the packaging we actually ship — AF2 weights + MMseqs2
# MSA + a clean pip install. The upstream AlphaFold2 repo is linked
# from the ColabFold README.
github_url: str = "https://github.com/sokrypton/ColabFold"

seo_faq: list[dict] = [
    {
        "q": "Can I run AlphaFold2 multimer online without a local GPU?",
        "a": (
            "Yes. Ranomics Tools runs AlphaFold2 (via the ColabFold "
            "implementation) on a dedicated GPU through your browser, "
            "with full MSA search and template support. Results land on a "
            "job page with ipTM, pLDDT, and pAE plots."
        ),
    },
    {
        "q": "How is this different from running ColabFold yourself?",
        "a": (
            "Same underlying weights and pipeline, but you skip CUDA "
            "setup, MMseqs2 round-trip on your laptop, and the wait for "
            "the public Colab queue. You also get a persistent job page "
            "you can share or hand off into ProteinMPNN or BindCraft."
        ),
    },
    {
        "q": "How much does an AlphaFold2 multimer run cost?",
        "a": (
            "Billing is by the second of dedicated GPU time. A typical "
            "single-complex fold costs a few cents to a dollar from your "
            "wallet. New accounts start with "
            f"{_SIGNUP_CREDIT} of credit, which covers many monomer "
            "folds or a handful of multimers."
        ),
    },
]

comparison_one_liner: str = (
    "You have a sequence and want the most trusted 3D prediction of "
    "it, with per-residue confidence you can act on. It searches "
    "for related natural sequences first, which is where the "
    "accuracy comes from and where the time goes. For a faster "
    "answer use ColabFold or ESMFold; to score a binder against its "
    "target use Boltz-2."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Predicts the 3D structure of a protein from its sequence — one "
        "chain, or several chains folded together as a complex. It "
        "searches public databases for related natural sequences first, "
        "which is where most of its accuracy comes from and most of its "
        "runtime goes, then returns a structure with per-residue "
        "confidence (pLDDT) and an estimate of the error between any "
        "two residues (PAE). Both are calibrated, meaning the numbers "
        "mean what they say. AlphaFold2, Jumper et al., <em>Nature</em> "
        "2021, packaged via ColabFold (Mirdita et al., <em>Nature "
        "Methods</em> 2022)."
    ),
    "when_to_use": [
        (
            "You have a sequence and want the most trusted structure "
            "prediction available, with confidence numbers you can act on."
        ),
        (
            "Your target is a single chain, or a small complex of two to "
            "four chains."
        ),
        (
            "You can wait 5 to 10 minutes per run for the homolog search "
            "and three refinement passes."
        ),
    ],
    "prerequisites": [
        "Single-letter FASTA sequence(s). Multimers separated by <code>:</code> or pasted as multi-record FASTA.",
        "A stable target topology. AF2 underperforms on intrinsically disordered or flexible regions.",
    ],
    "inputs": [
        {
            "name": "Sequence",
            "explanation": (
                "Paste FASTA. Use <code>:</code> as a chain separator "
                "for multimers (e.g. <code>SEQ_A:SEQ_B</code>)."
            ),
        },
        {
            "name": "Recycles",
            "explanation": (
                "Number of model recycles. 3 is the AF2 default; lower "
                "is faster but trades a small amount of accuracy."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "standalone", "typical": "5 to 10 min"},
    ],
    "output_summary": (
        "Predicted PDB with per-residue pLDDT, pairwise PAE, and pTM "
        "or ipTM (for multimers). Download PDB or PAE matrix for "
        "downstream filtering and analysis."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# ---------------------------------------------------------------------------
# No pilot. See templates/components/pilot_card.html — the card simply
# does not render.
# ---------------------------------------------------------------------------
# A 5 to 10 minute fold. Its only scale parameter is how many
# sequences you paste, which the user is already choosing
# directly on the form.
PILOT: dict | None = None


# ---------------------------------------------------------------------------
# EXAMPLE - one real past run, narrated, rendered by
# templates/components/worked_example.html. The output beside it is
# tools/af2/example/result.json replayed through this tool's OWN results
# partial, so the demo can never drift from the real job page.
#
# PROVENANCE. One `batch` run on the same Modal app this form submits to,
# pulled from the jobs table by scripts/capture_example_result.py (job
# dd44f46f, 388 GPU-seconds). Re-run that script against the same job to
# re-derive every figure below.
#
# This supersedes the note that stood here refusing an example: the only
# payload on disk then was a deliberately degraded smoke fixture (58-residue
# BPTI, MSA off, one recycle) that measured the fixture rather than the tool.
# This is a real MSA-backed batch.
#
# The ten folded sequences were DESIGNS and are not published. They live in
# job.inputs, which a worked example never renders - the payload here is
# job.result, which for a batch carries scores only and no sequence at all.
# Nothing had to be stripped.
#
# The per-design `runtime_seconds` in this payload are CUMULATIVE elapsed
# time, not per-design durations: they climb 101 -> 358 and sum to far more
# than the job's own 388. Do not quote them as "this design took N seconds".
#
# COST is compute_charge_usd(388, "A100-80GB") - the charge, not the raw
# Modal cost. tests/test_worked_examples.py recomputes it from the rate card.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "Ten designed variants of a microbial rhodopsin &mdash; the "
        "seven-transmembrane-helix light-driven channel family &mdash; at 333 "
        "residues each, folded in one submission. They came off a generative "
        "model as ten samples on one scaffold, and each is 72% to 79% "
        "identical to the first."
    ),
    "why_this_target": (
        "A membrane protein is a hard case for a structure predictor, and ten "
        "near-siblings is the shape a real design round actually arrives in. "
        "The interesting part is not whether they fold &mdash; it is what you "
        "are entitled to conclude from a table of ten scores, which is less "
        "than the table's own layout suggests."
    ),
    "inputs_used": [
        (
            "Preset",
            "Batch for many fold targets",
            "Ten sequences in one submission rather than ten submissions. "
            "Same cost per sequence, one job to watch.",
        ),
        (
            "Fold targets (FASTA or one per line)",
            "10 records, one chain each",
            "Whether a record folds as a monomer or a multimer is not a "
            "setting &mdash; it follows from the record itself, and joining "
            "chains with a colon is what makes one a multimer. These are "
            "single chains, so all ten folded as monomers, which is why the "
            "ipTM column stays blank throughout: there is no second chain, so "
            "there is no interface to score. Blank is not a low score.",
        ),
        (
            "Number of recycles",
            "3",
            "The default. Each pass lets the model refine its own answer; "
            "three is the standard setting behind published AF2 numbers.",
        ),
        (
            "Use PDB templates",
            "on",
            "Known structures are allowed as scaffolding. The rhodopsin fold "
            "is well represented in the PDB, so there is real signal to use.",
        ),
    ],
    "what_came_back": (
        "All ten folded, no failures. Mean pLDDT runs from "
        "<strong>74.16 to 75.93</strong> and pTM from 0.71 to 0.73. "
        "The table sorts by mean pLDDT and highlights the top row &mdash; but "
        "the top row beats the bottom row by <strong>1.77 pLDDT points, and "
        "the whole pTM column spans 0.02</strong>. "
        "Every one of the ten landed in the same place."
    ),
    "how_to_read_it": (
        "<strong>The highlight on the top row is a sort order, not a "
        "verdict.</strong> A ranked table implies the first row is better "
        "than the last, and across a spread this narrow it simply is not: "
        "re-run the same ten sequences with a different seed and the order "
        "would reshuffle. Nothing here distinguishes these designs, and "
        "picking the top one to carry forward is picking at random while "
        "feeling informed. "
        "The result that <em>is</em> real is the agreement. Ten variants "
        "differing at a fifth to a quarter of their residues all folding to "
        "the same "
        "confidence says the scaffold tolerates that much variation &mdash; "
        "which is a genuine finding about the design round, and the opposite "
        "of what you would conclude by reading row one. "
        "For scale: 70 is the usual line for a confidently folded structure, "
        "and all ten clear it. Compare with the ESMFold example on this site, "
        "where a genuinely disordered protein has no residue reaching 70 "
        "anywhere in the chain."
    ),
    "what_we_did_next": (
        "A batch this tight does not narrow itself, so the choice has to come "
        "from somewhere other than these scores &mdash; a property the table "
        "does not carry, or an experiment. The runs worth doing next are the "
        "ones that can separate the ten: fold them against the partner or "
        "ligand that matters if there is one, which turns a blank ipTM column "
        "into a real interface number, or take several forward together "
        "rather than betting on row one. When a screen comes back this "
        "uniform, that is the screen telling you it has finished its job."
    ),
    "cost_usd": "0.68",
    "runtime": "6 minutes",
}
