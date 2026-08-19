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
    "You have a complex that is not all protein — protein with DNA, "
    "with RNA, or with a bound small molecule — and you want the "
    "whole thing folded together in one prediction. For a plain "
    "protein-protein or protein-peptide complex, Boltz-2 is faster "
    "and cheaper."
)
example_output_id: Optional[str] = None


about: dict = {
    "what_it_is": (
        "Folds a whole complex at once when the complex is not all "
        "protein — protein with DNA, protein with RNA, protein with a "
        "bound small molecule, or any mix of those written into a "
        "single specification. Every atom is modelled, not just the "
        "protein backbone. It is the multi-molecule counterpart to "
        "Boltz-2, which is faster but protein-only. OpenDDE, Aureka AI "
        "Research, Apache-2.0."
    ),
    "when_to_use": [
        (
            "Your complex has something in it other than protein: DNA, RNA, "
            "or a bound small molecule."
        ),
        (
            "You are modelling an antibody or nanobody together with its "
            "antigen (use the ABAG checkpoint)."
        ),
        (
            "You want an AlphaFold3-style all-atom prediction without "
            "standing up the pipeline yourself."
        ),
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


# ---------------------------------------------------------------------------
# No pilot. See templates/components/pilot_card.html — the card simply
# does not render.
# ---------------------------------------------------------------------------
# Deliberately None even though OpenDDE is neither fast nor cheap.
# It has no scaling parameter the estimator honours, so the
# smallest possible run is the only possible run (~$15) and a card
# headed "pilot" over that number would be a lie. Give it a PILOT
# if it ever gains a cheaper tier.
PILOT: dict | None = None


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
EXAMPLE: dict | None = None
