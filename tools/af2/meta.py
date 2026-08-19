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
# EXAMPLE — one real past run, rendered by
# templates/components/worked_example.html. None here, deliberately:
# The one archived payload (.deploy-logs/af2-smoke-bug8-attempt3-run1
# .log) is a deliberately degraded smoke fixture: 58-residue BPTI, MSA
# off, one recycle, mean pLDDT 54. Real, but it measures the fixture
# rather than the tool, and AF2's whole case is MSA-backed accuracy.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = None
