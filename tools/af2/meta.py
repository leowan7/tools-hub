"""Static reference metadata for the AF2 standalone (D2) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/mpnn/meta.py``.

Shapes
------
    paper_citation   — short inline citation.
    paper_url         — Nature permalink.
    github_url        — upstream ColabFold repository (which bundles AF2).
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
    example_output_id — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

from shared.wallet import MIN_TOPUP_USD

# The minimum top-up is quoted in SEO copy that reaches JSON-LD structured
# data, so it is read from the constant rather than retyped.
_MIN_TOPUP: str = f"${MIN_TOPUP_USD:.0f}"

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
            "You pay for the GPU time your run uses, from your "
            f"wallet. You can top up from {_MIN_TOPUP}, which is enough "
            "to start a run."
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
            "You can wait for the homolog search and three refinement "
            "passes each run makes."
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
    # The ipTM figures are af2's own bar: shared/score_legends.py
    # ("af2", "iptm") sets good 0.6 and excellent 0.75. Stated
    # INCLUSIVELY, which that legend's own ``explanation`` ("Above 0.6")
    # is not: judge gates on ``seen >= good`` (shared/score_legends.py::judge),
    # shared/metric_glossary.py tells readers a value exactly on a bar
    # counts as meeting it, and score_legends.py made the same
    # correction once already, in its ("esmfold2-design", "ipTM") entry.
    # The ipTM entry on the form's "What good looks like" panel and the
    # one in this tool's guide each end a SENTENCE by sending the reader
    # here for the number -- a multi-chain caveat follows it
    # (about_panel.html:275, tool_guide.html:211) -- so a summary stating
    # none makes that pointer a dead end;
    # tests/test_about_panel_iptm_bar_default.py fails if it goes missing.
    "output_summary": (
        "Predicted PDB with per-residue pLDDT, pairwise PAE, and pTM "
        "(or ipTM on a multimer). An ipTM of 0.6 or more is a plausible "
        "interface and 0.75 or more is strong. On a run of your own the "
        "structures download from the results page. The PAE matrix "
        "downloads only from a standalone run &mdash; a batch returns "
        "no PAE file."
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
# PROVENANCE. One `standalone` run on Leo's own account, pulled from the
# jobs table by scripts/capture_example_result.py (job df0b5d35, 73
# GPU-seconds). Re-run that script against the same job to re-derive every
# figure below. It replaces an earlier batch example whose source job was
# not on an internal account.
#
# The folded sequence is human ubiquitin, a published reference sequence
# (static/example/ubiquitin.fasta), not a design. pdb_b64 is kept, so the
# results partial draws its own viewer.
#
# Figures quoted below, all from result.json: mean pLDDT 95.4, pTM 0.83,
# per-residue range 53.1 to 98.5 (the two numbers the page prints). Of 76
# residues, 71 are above 90; the five at or below 90 are positions 72-76,
# with the minimum at residue 76.
#
# COST is compute_charge_usd(73, "A100-80GB") - the charge, not the raw
# Modal cost. tests/test_worked_examples.py recomputes it from the rate card.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "The target is human ubiquitin, a single chain of 76 residues, and "
        "it is the example FASTA this form ships with. The sequence is a "
        "published reference sequence rather than a design."
    ),
    "why_this_target": (
        "Ubiquitin is one of the most studied small folds there is, with "
        "crystal structures going back decades, so the right answer is known "
        "before the run starts. That makes it the cleanest way to see what a "
        "confident AlphaFold2 result looks like, and what the one honestly "
        "uncertain part of a confident result looks like too."
    ),
    "inputs_used": [
        (
            "Preset",
            "Standalone, one FASTA",
            "We used the standalone preset with one FASTA, which folds a "
            "single chain. The other preset, batch, folds many sequences in "
            "a single submission.",
        ),
        (
            "FASTA",
            "1 record, 76 aa",
            "The FASTA held one record of 76 amino acids, so the sequence "
            "folds as a monomer. That is why there is no ipTM row on the "
            "result: with no second chain there is no interface to score.",
        ),
        (
            "Number of recycles",
            "3",
            "We left the number of recycles at the default of 3. Each pass "
            "lets the model refine its own answer, and three is the standard "
            "setting behind published AF2 numbers.",
        ),
        (
            "Use PDB templates",
            "on",
            "We ran with PDB templates on, so known structures are allowed as "
            "scaffolding, and ubiquitin has plenty of them in the PDB.",
        ),
    ],
    "what_came_back": (
        "The run returned a complete structure with a mean pLDDT of "
        "<strong>95.4</strong> and a pTM of <strong>0.83</strong>. The "
        "per-residue range runs from <strong>53.1 to 98.5</strong>, and "
        "71 of the 76 residues sit above 90, while the five that do not are "
        "the last five, at the C-terminal end."
    ),
    "how_to_read_it": (
        "<strong>The mean and the minimum are both right.</strong> A mean of "
        "95.4 says the fold is predicted with very high confidence, and it "
        "is: the compact core of the protein is placed almost exactly. The "
        "53.1 at the bottom of the range is not a flaw in that core. It is "
        "the final residue of a short tail that sticks out from the folded "
        "body, and the viewer shows it as the one stretch that fades from "
        "blue toward red. That tail is known to be flexible in the real "
        "protein, so a lower score there is the model being accurate about "
        "what it cannot pin down, not the model failing. "
        "Read a wide pLDDT range by asking WHERE the low residues are, "
        "because a few at a chain end is normal while a low patch in the "
        "middle of a domain is worth worrying about. Compare the ESMFold "
        "example on this site, where the whole chain is low end to end."
    ),
    "what_we_did_next": (
        "We did nothing further, because the run was a check that the tool "
        "returns a known answer, and it did. On a protein of your own the "
        "useful next step depends on where any low residues fall: trim a "
        "floppy end before you use the structure as a design target, or fold "
        "it again with the partner it binds if the low region is an "
        "interface."
    ),
    "cost_usd": "0.13",
    "runtime": "73 seconds",
}
