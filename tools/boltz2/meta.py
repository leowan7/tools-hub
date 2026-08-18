"""Static reference metadata for Boltz-2 cofold validation.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/mpnn/meta.py`` etc.

Shapes
------
    PRESET_RUNTIME       — {preset_slug: {"typical_minutes": str}}.
    paper_citation       — short inline citation.
    paper_url            — bioRxiv permalink.
    github_url           — upstream Boltz repository.
    comparison_one_liner — positioning string vs the rest of the toolkit.
    example_output_id    — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional


# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "standalone": {"typical_minutes": "<1"},
    "msa_server": {"typical_minutes": "~3"},
}

paper_citation: str = "Wohlwend et al., bioRxiv 2025"
paper_url: str = "https://www.biorxiv.org/content/10.1101/2025.06.14.659707v2"
github_url: str = "https://github.com/jwohlwend/boltz"
comparison_one_liner: str = (
    "Pick Boltz-2 to validate a designed binder against your antigen. "
    "Single-sequence cofold with interface confidence (ipTM), "
    "antibody-trained and orthogonal to AF2-multimer. For sequence "
    "design, use ProteinMPNN; for de novo backbones, use RFantibody, "
    "BindCraft, or BoltzGen first."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Boltz-2 (Wohlwend et al., <em>bioRxiv</em> 2025). An open-weights "
        "structure prediction model trained on antibody-antigen complexes "
        "with a calibrated confidence head. Single-sequence mode is "
        "orthogonal to AF2-multimer: when both agree, the predicted "
        "complex is real; when they disagree, the disagreement itself is "
        "informative. Returns a folded complex PDB plus ipTM, pTM, and "
        "complex_pLDDT per design."
    ),
    "when_to_use": [
        "You designed binders with MPNN, RFantibody, BindCraft, BoltzGen, "
        "RFdiffusion, or PXDesign and need to score them against the "
        "intended antigen.",
        "You have native or near-native scFv / Fab / nanobody / peptide "
        "sequences you want a fast independent fold for.",
        "AF2-multimer ipTM is saturated and you want a second confidence "
        "channel from a different architecture before ordering DNA.",
    ],
    "prerequisites": [
        "Antigen PDB or mmCIF (single chain; the binder is added separately).",
        "One or more binder sequences (scFv, nanobody, peptide, anything "
        "that folds as a single protein chain), 20 to 400 aa each.",
        "Optional: a list of antigen hotspot residue numbers to count "
        "contacts against (1-indexed on the chosen antigen chain).",
    ],
    "inputs": [
        {
            "name": "Antigen PDB",
            "explanation": (
                "Upload the target structure as .pdb, .cif, or .mmcif. "
                "CIF inputs are converted to PDB server-side."
            ),
        },
        {
            "name": "Antigen chain",
            "explanation": (
                "Single chain ID (e.g. <code>A</code>) that Boltz-2 should "
                "treat as the antigen. The binder folds as a separate chain."
            ),
        },
        {
            "name": "Hotspot residues",
            "explanation": (
                "Optional. Comma-separated 1-indexed positions on the "
                "antigen chain (e.g. <code>55,56,57,71,72,73,74</code>). "
                "The pipeline reports how many of these residues the "
                "binder contacts (heavy atom within 5&nbsp;&Aring;)."
            ),
        },
        {
            "name": "Binder sequences",
            "explanation": (
                "Paste one sequence per line, or upload as FASTA "
                "(<code>&gt;name</code> headers). Each sequence folds "
                "independently against the antigen. 20 to 400 aa per "
                "binder, up to 50 binders per run."
            ),
        },
        {
            "name": "Preset",
            "explanation": (
                "<strong>Single-sequence</strong> (default) folds in "
                "<code>msa: empty</code> mode, the right choice "
                "for designed sequences. <strong>With MSA</strong> "
                "fetches MSAs from the public ColabFold MMseqs2 endpoint "
                "and is slower but more accurate on natural sequences."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "standalone", "typical": "<1 min/design"},
        {"preset": "msa_server", "typical": "~3 min/design"},
    ],
    "output_summary": (
        "Per-design folded complex PDB + ipTM, pTM, complex_pLDDT, "
        "complex_iplddt, and hotspot contact count. Strict-pass "
        "classification (<code>complex_pLDDT &gt; 0.85</code>, "
        "<code>ipTM &gt; 0.7</code>, "
        "<code>n_hotspot_contacts &gt; 4</code>) surfaces which designs "
        "are worth ordering."
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
    "label": "Starter check: one binder",
    "goal": (
        "See whether a binder sequence you already have is predicted to "
        "fold against your antigen."
    ),
    "you_need": (
        "Your antigen structure (single chain) and one binder sequence "
        "&mdash; an scFv, a nanobody or a peptide, 20 to 400 residues."
    ),
    "params": {
        "preset": "msa_server",
    },
    "next_step": (
        "Paste the rest of your candidate sequences and run them "
        "together; the cost scales with how many you submit at once."
    ),
}


# ---------------------------------------------------------------------------
# EXAMPLE — one real past run, rendered by
# templates/components/worked_example.html. None here, deliberately:
# No real completed-run payload for this tool exists anywhere on disk
# (searched 2026-08-18: every .json in the tree, .deploy-logs/, scratch/,
# runs/, tmp/). The fixtures in tests/ are synthetic and the stage JSONs
# under runs/ are pipeline-stage outputs, not job results. Capture one from
# a real run and this becomes a two-file change: example/result.json plus
# the narration below.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = None
