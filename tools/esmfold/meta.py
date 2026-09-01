"""Static reference metadata for the ESMFold (D4) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/colabfold/meta.py``.

Shapes
------
    PRESET_RUNTIME    - {preset_slug: {"typical_minutes": str}}.
    paper_citation    - short inline citation.
    paper_url         - Science / bioRxiv permalink.
    github_url        - upstream ESM repository.
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
    example_output_id - optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "standalone": {"typical_minutes": "0.5 to 1"},
}

paper_citation: str = "Lin et al., Science 2023"
paper_url: str = "https://www.science.org/doi/10.1126/science.ade2574"
github_url: str = "https://github.com/facebookresearch/esm"
comparison_one_liner: str = (
    "You have one protein sequence and want its 3D shape in about "
    "30 seconds. No search for relatives, so it works on designed "
    "or orphan sequences that have no natural family to align "
    "against. One chain only — for complexes use ColabFold or "
    "AlphaFold2."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Predicts the structure of one protein chain from its sequence "
        "alone, in about 30 seconds. It reads the sequence through a "
        "protein language model instead of searching for relatives, so "
        "it still works on designed or orphan sequences that have no "
        "natural family to align against. One chain only — it cannot "
        "fold a complex. ESMFold, Lin et al., <em>Science</em> 2023."
    ),
    "when_to_use": [
        (
            "You want a single-chain fold in well under a minute."
        ),
        (
            "Your sequence is designed, or has no known relatives, so a "
            "search for them would come back empty anyway."
        ),
        (
            "You are triaging a large batch of sequences and need "
            "throughput more than precision."
        ),
    ],
    "prerequisites": [
        "Single FASTA sequence (monomer only).",
        "Sequence length under ~600 residues for best accuracy.",
    ],
    "inputs": [
        {
            "name": "Sequence",
            "explanation": (
                "Single-chain FASTA. Multimers and non-canonical residues "
                "are not supported. Use ColabFold or AF2 instead."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "standalone", "typical": "~30 s"},
    ],
    "output_summary": (
        "Predicted PDB with per-residue pLDDT. No PAE (single-sequence "
        "prediction has no inter-domain signal). Use as a fast "
        "self-consistency check on designed sequences."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# ---------------------------------------------------------------------------
# No pilot. See templates/components/pilot_card.html — the card simply
# does not render.
# ---------------------------------------------------------------------------
# A single-sequence monomer fold finishes in well under a minute
# and has no scale parameter to start small on. There is nothing a
# pilot tier would reduce.
PILOT: dict | None = None


# ---------------------------------------------------------------------------
# EXAMPLE - one real past run, narrated, rendered by
# templates/components/worked_example.html. The output beside it is
# tools/esmfold/example/result.json replayed through this tool's OWN results
# partial, so the demo can never drift from the real job page.
#
# PROVENANCE. One `standalone` run on the same Modal app this form submits to,
# pulled from the jobs table by scripts/capture_example_result.py (job
# 800c4ddc, 32 GPU-seconds). Re-run that script against the same job to
# re-derive every figure below - it prints the pLDDT distribution the
# narration quotes.
#
# The folded sequence is human myelin basic protein, a PUBLISHED REFERENCE
# sequence, so it stays in the payload: it is the one field that lets a reader
# check this example against the literature. No designed sequence appears
# anywhere in it.
#
# pae_matrix_b64 was dropped at capture - 145 KB driving only the specialist
# pAE panel, which this partial already renders conditionally.
# plddt_per_residue is kept, because it IS this example.
#
# UNITS. This payload stores pLDDT on 0-1 (mean 0.39, max 0.659) because
# that is what ESMFold's HuggingFace head returns. The PAGE renders 0-100,
# because shared/metric_glossary.plddt_on_100 normalises at display time --
# every threshold, legend and tooltip on this site is written for 0-100.
# So the prose below quotes 39.00 / 65.9 / 50 / 70 and the JSON beside it
# holds 0.39 / 0.659. That is not a contradiction, it is the normaliser.
# Quote what the PAGE renders; tests/test_worked_examples.py asserts both
# halves so neither can drift.
#
# COST is compute_charge_usd(32, "A100-40GB") - what a reader would be
# charged, not the raw Modal cost. tests/test_worked_examples.py recomputes it
# from the rate card on every run, so a rate change fails a test instead of
# leaving a stale price in front of a customer.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "Human myelin basic protein &mdash; one chain, 304 residues, pasted "
        "as plain FASTA. A published reference sequence, not a design."
    ),
    "why_this_target": (
        "Myelin basic protein is one of the textbook <em>intrinsically "
        "disordered</em> proteins: on its own in solution it has no single "
        "fixed structure to predict. That makes it a useful example "
        "precisely because the answer is known in advance: it shows what "
        "something every user runs into sooner or later actually looks like "
        "&mdash; a low score coming back, and no obvious way to tell whether "
        "the tool failed or the protein did."
    ),
    "inputs_used": [
        (
            "Preset",
            "Standalone, one FASTA",
            "One chain, one fold. The other preset, batch, folds many "
            "sequences in a single submission.",
        ),
        (
            "FASTA (single chain)",
            "304 aa, one chain",
            "Pasted as FASTA. ESMFold reads the sequence alone &mdash; there "
            "is no MSA step and nothing else to configure, which is why it "
            "comes back in well under a minute.",
        ),
    ],
    "what_came_back": (
        "A complete structure, a mean pLDDT of <strong>39.00</strong> and a "
        "pTM of 0.119. Open <em>Per-residue pLDDT</em> under the result and "
        "the 304-residue strip is red almost end to end: <strong>278 of 304 "
        "residues sit below 50</strong>, 26 are amber above it, and nothing "
        "reaches green or blue anywhere. <strong>Not one residue reaches "
        "70</strong>; the highest in the chain is 65.9. The longest unbroken "
        "stretch that even reaches 50 is 10 residues."
    ),
    "how_to_read_it": (
        "<strong>That is the correct answer, not a failed run.</strong> The "
        "model is not confused about this protein; it is reporting that the "
        "protein has no one shape to report, which is what the disorder "
        "literature says about it too. A run that genuinely breaks returns an "
        "error, not a structure. "
        "What separates the two readings is the <em>shape</em> of the "
        "per-residue strip, not the mean. Here it is flat and low from end to "
        "end &mdash; no folded region anywhere. A low mean with a high "
        "plateau in the middle and red only at the edges would be the "
        "opposite finding: a real folded region with floppy ends, where the "
        "middle is trustworthy and only the termini are not. Similar mean, "
        "different result, and the mean alone cannot tell them apart. Open "
        "the strip."
    ),
    "what_we_did_next": (
        "Nothing, because there is nothing here to fix &mdash; no setting on "
        "this form would raise that number, and running it again returns the "
        "same structure. When a chain comes back like this the useful move is "
        "to change the question rather than the parameters: fold the fragment "
        "that is known to fold, or fold it together with the partner it binds, "
        "since many disordered proteins take a definite shape only in complex. "
        "If instead you are looking at a <em>designed</em> sequence scoring "
        "like this, that is a real failure and the design needs redoing."
    ),
    "cost_usd": "0.04",
    "runtime": "32 seconds",
}
