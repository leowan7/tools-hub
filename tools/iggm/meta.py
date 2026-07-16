"""Static reference metadata for IgGM antibody / nanobody design.

Kept separate from ``__init__.py`` (adapter registration) so About panels,
citation blocks, and cost previews import plain-data constants without
touching the adapter contract. Parallel to ``tools/boltz2/meta.py``.
"""

from __future__ import annotations

from typing import Optional


PRESET_RUNTIME: dict[str, dict[str, object]] = {
    # Advisory only; refit from the canary I-* runs before flag-on.
    "complex_prediction": {"typical_minutes": "~2"},
    "cdr_design": {"typical_minutes": "~3"},
    "fr_design": {"typical_minutes": "~3"},
    "affinity_maturation": {"typical_minutes": "scales with samples x masked positions"},
    "inverse_design": {"typical_minutes": "~2"},
}

paper_citation: str = "Wang et al., ICLR 2025"
paper_url: str = "https://arxiv.org/abs/2504.09248"
github_url: str = "https://github.com/TencentAI4S/IgGM"
comparison_one_liner: str = (
    "Pick IgGM to design or humanize an antibody / nanobody against your "
    "antigen, or to predict the antibody-antigen complex, all in one model. "
    "For VHH backbones use RFantibody; for paired scFv CDRs use ESMFold2 "
    "design; to validate a designed binder's fold use Boltz-2."
)
example_output_id: Optional[str] = None


about: dict = {
    "what_it_is": (
        "IgGM (Wang et al., <em>ICLR</em> 2025). A generative diffusion "
        "foundation model for antibody and nanobody engineering. One model "
        "covers antibody-antigen complex structure prediction, CDR design, "
        "framework redesign / humanization, affinity maturation, and inverse "
        "(sequence-from-structure) design, all epitope-guided against the "
        "antigen you upload."
    ),
    "when_to_use": [
        "You have an antibody or nanobody sequence and want to redesign its "
        "CDRs (or framework) against a specific antigen and epitope.",
        "You want to humanize a framework or mature affinity from a "
        "wild-type reference.",
        "You want a fast antibody-antigen complex structure prediction "
        "before committing to a wet-lab campaign.",
    ],
    "prerequisites": [
        "Antigen structure as PDB or mmCIF (the antigen sequence is read "
        "from this file — you do not type it).",
        "Antibody heavy chain sequence (>H); light chain (>L) optional "
        "(omit it for a nanobody / VHH). Mark positions to design with X.",
        "Optional: an epitope — click antigen residues on the structure and "
        "IgGM guides design toward them.",
    ],
    "inputs": [
        {
            "name": "Antibody FASTA",
            "explanation": (
                "Paste the heavy chain as <code>&gt;H</code> and, for a "
                "conventional antibody, the light chain as <code>&gt;L</code>. "
                "Mark residues to design with <code>X</code>. Omit "
                "<code>&gt;L</code> for a nanobody / VHH. Do not include an "
                "antigen record — it comes from the uploaded PDB."
            ),
        },
        {
            "name": "Antigen PDB",
            "explanation": (
                "Upload the target as .pdb / .cif. The antigen sequence is "
                "extracted from the chain you select, so the structure is the "
                "single source of truth."
            ),
        },
        {
            "name": "Antigen chain",
            "explanation": (
                "The chain ID in the uploaded PDB that IgGM should treat as "
                "the antigen (e.g. <code>A</code>)."
            ),
        },
        {
            "name": "Epitope",
            "explanation": (
                "Optional. Click residues on the antigen structure; IgGM "
                "guides design toward them. Positions are handled correctly "
                "regardless of the PDB's residue numbering."
            ),
        },
        {
            "name": "Mode",
            "explanation": (
                "<strong>Complex prediction</strong> folds the complex; "
                "<strong>CDR design</strong> / <strong>framework redesign</strong> "
                "redesign masked (X) positions; <strong>affinity maturation</strong> "
                "generates improved variants from a wild-type reference; "
                "<strong>inverse design</strong> recovers sequence from the backbone."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "complex_prediction", "typical": "~2 min"},
        {"preset": "cdr_design", "typical": "~3 min"},
        {"preset": "fr_design", "typical": "~3 min"},
        {"preset": "affinity_maturation", "typical": "scales with samples x masked positions"},
        {"preset": "inverse_design", "typical": "~2 min"},
    ],
    "output_summary": (
        "Per design: the predicted antibody-antigen complex PDB, the designed "
        "sequence, and an epitope-contact count (how many of your chosen "
        "epitope residues the designed antibody engages). Sequence statistics "
        "and amino-acid distribution plots are attached as artifacts."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# Load Example chips are retired platform-wide (Leo, 2026-07: no one should
# spend GPU on an example). The IgGM form does not render an examples chip.
# Kept as an empty list only so the generic tool_form route's
# ``getattr(meta, "examples", [])`` stays well-defined. Canary inputs (the
# IgGM-bundled 8iv5 / 8hpu complexes) are exercised Modal-side from the cloned
# repo, not bundled here.
examples: list[dict] = []
