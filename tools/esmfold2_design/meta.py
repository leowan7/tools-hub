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
    comparison_one_liner — positioning string vs the rest of the toolkit.

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

paper_citation: str = "EvolutionaryScale, biohub.ai 2025"
paper_url: str = "https://biohub.ai/papers/esm_protein.pdf"
github_url: str = (
    "https://github.com/evolutionaryscale/esm/blob/main/cookbook/"
    "tutorials/binder_design.ipynb"
)
comparison_one_liner: str = (
    "Pick ESMFold2 design for scFv CDR design (the only catalog tool "
    "that does paired heavy + light scFvs) or as a gradient-based "
    "alternative to RFdiffusion's diffusion sampler for minibinders. "
    "Run alongside RFdiffusion, BindCraft, BoltzGen, or PXDesign for "
    "orthogonal candidate pools. For nanobody or VHH formats use "
    "RFantibody instead."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "ESMFold2 design (EvolutionaryScale, 2025). Inversion of the "
        "ESMFold2 structure prediction model: gradient descent on a "
        "soft sequence representation, backpropagated through the fold "
        "network, jointly optimizes sequence and predicted binding pose. "
        "The same architecture handles both de novo minibinders and "
        "framework-locked scFv CDR design. Wet-lab validated against "
        "PDGFRB, EGFR, PD-L1, CD45, and CTLA4 with nanomolar affinity "
        "and functional activity."
    ),
    "when_to_use": [
        "You need a paired heavy + light scFv with all six CDRs designed "
        "jointly against your target. No other catalog tool does this.",
        "You want a gradient-based minibinder alternative to RFdiffusion "
        "diffusion sampling. A different fold prior often surfaces "
        "different binders for a stuck target.",
        "You want to compare CDR designs across three validated humanized "
        "frameworks (trastuzumab, atezolizumab, ocankitug).",
        "You want to baseline your wet-lab setup against one of the five "
        "paper-validated targets (PDGFRB, EGFR, PD-L1, CD45, CTLA4).",
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
