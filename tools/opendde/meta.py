"""Static metadata for the OpenDDE tool (About panel, citation, runtime table).

Plain-data module — no adapter import. The About renderer and cost preview read
these constants. Runtime figures are conservative bootstraps; they are refit from
the O-1/O-2 benchmark before the flag flips on.
"""

from __future__ import annotations

from typing import Optional

# Keyed by preset slug. Both checkpoints share the same architecture, so runtime
# is driven by complex size + sampler settings, not by which checkpoint. Figures
# from the O-1/O-2/O-3 canaries: a single small-complex prediction is ~2-3 min
# (dominated by a ~1.5 min fixed CUDA/kernel init); more samples/seeds add ~15 s
# each. Kept conservative (overestimate) for larger inputs.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "general": {"typical_minutes": "~2 to 8"},
    "abag": {"typical_minutes": "~2 to 8"},
}

paper_citation: str = "Aureka AI Research, OpenDDE-Preview, arXiv 2026"
paper_url: str = "https://arxiv.org/abs/2607.03787"
github_url: str = "https://github.com/aurekaresearch/OpenDDE"
comparison_one_liner: str = (
    "Pick OpenDDE to co-fold a mixed complex — protein with DNA, RNA, or a bound "
    "ligand in one prediction. For a plain protein-protein or protein-peptide "
    "cofold, Boltz-2 is faster and cheaper."
)
example_output_id: Optional[str] = None


about: dict = {
    "what_it_is": (
        "OpenDDE is an AlphaFold3-class, all-atom co-folding foundation model "
        "(Aureka AI Research, Apache-2.0). It predicts the joint structure of an "
        "arbitrary mix of biomolecular entities &mdash; protein, DNA, RNA, and "
        "small molecules (ligands) &mdash; from a single specification."
    ),
    "when_to_use": [
        "You need a complex with more than just protein: protein plus DNA / RNA, "
        "or a bound small molecule.",
        "You are modelling an antibody or nanobody with its antigen (use the ABAG "
        "checkpoint).",
        "You want an AlphaFold3-style multi-modal prediction without standing up "
        "the pipeline yourself.",
    ],
    "prerequisites": [
        "Sequences for each polymer chain (protein / DNA / RNA).",
        "For ligands: a CCD code (e.g. <code>CCD_ATP</code>), a bare SMILES "
        "string, or a bundled <code>FILE_*.sdf</code> reference.",
    ],
    "inputs": [
        {
            "name": "Checkpoint",
            "explanation": (
                "<strong>General</strong> for any entity mix, or "
                "<strong>ABAG</strong> for antibody-antigen complexes."
            ),
        },
        {
            "name": "Entities (guided)",
            "explanation": (
                "One textarea per entity type. Proteins / DNA / RNA are entered as "
                "FASTA (<code>&gt;id</code> headers optional); ligands one per "
                "line. This adapter assembles the OpenDDE JSON for you."
            ),
        },
        {
            "name": "Entities (JSON)",
            "explanation": (
                "Or paste an exact OpenDDE spec. It is validated against the real "
                "schema and re-checked against the same size limits &mdash; the "
                "JSON mode is not a way around the ceilings."
            ),
        },
        {
            "name": "Samples / steps / recycles",
            "explanation": (
                "Sampler settings. Samples is how many structures per seed; steps "
                "and recycles trade compute for quality."
            ),
        },
        {
            "name": "Seeds",
            "explanation": (
                "How many seeds to run from the starting seed. Total predictions "
                "returned is seeds &times; samples."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "general", "typical": "~2 to 8 min"},
        {"preset": "abag", "typical": "~2 to 8 min"},
    ],
    "output_summary": (
        "A ranked set of predicted complexes (mmCIF/PDB) with the model's own "
        "confidence ranking score per prediction. Download each structure or view "
        "it in the browser."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}
