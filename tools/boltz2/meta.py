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
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
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
    "You already have a binder sequence and the target it should "
    "hit, and you want to know whether they actually stick together "
    "before you order DNA. Returns the predicted complex and a "
    "0-to-1 interface confidence score. Trained on antibody-antigen "
    "complexes, so it is a genuinely second opinion next to "
    "AlphaFold2."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Folds a binder and its target together and tells you how "
        "confident it is that they touch. It was trained on "
        "antibody-antigen complexes and works from sequence alone, "
        "which makes it a genuinely independent second opinion next to "
        "AlphaFold2 multimer: when the two agree the complex is "
        "probably real, and when they disagree that is worth knowing "
        "before you order DNA. Returns the folded complex plus ipTM "
        "(confidence in the interface), pTM (confidence in the whole "
        "complex) and per-residue confidence for every design. Boltz-2, "
        "Wohlwend et al., <em>bioRxiv</em> 2025."
    ),
    "when_to_use": [
        (
            "You designed binders with another tool here and need them "
            "scored against the antigen you actually care about."
        ),
        (
            "You have natural or near-natural antibody fragments (scFv or "
            "Fab), nanobodies or peptides and want a fast independent fold "
            "of each."
        ),
        (
            "AlphaFold2 interface confidence has topped out across your "
            "candidates and you want a second, differently trained opinion "
            "before committing to synthesis."
        ),
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
# EXAMPLE — one real past run, narrated, rendered by
# templates/components/worked_example.html. The output beside it is
# tools/boltz2/example/result.json replayed through this tool's OWN results
# partial, so the demo cannot drift from the real results page.
#
# EVERY NUMBER BELOW IS A RECORDED FACT FROM THAT RUN, not an estimate and
# not an illustration. Provenance: job 5e7c7574 (2026-06-02), captured with
# scripts/capture_example_result.py, which stripped provider_job_id; the
# payload carried nothing else identifying. Nothing may be added here that
# the archived payload does not support.
#
# No cost_usd: that run recorded credits_cost 0, so there is no dollar
# figure to quote and none is invented. The field is optional.
#
# No structure_file: the antigen was 1UBQ and static/example/ does not
# carry it. ubiquitin.fasta is there but is the SEQUENCE, not the structure
# this run was given, and offering it would misdescribe the input.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "Ubiquitin &mdash; PDB <code>1UBQ</code>, chain A, 76 residues."
    ),
    "why_this_target": (
        "This one has a known right answer. The binder submitted against "
        "it is the UBA1 domain of hHR23A, a domain whose actual job in the "
        "cell is to bind ubiquitin, and the residues we pointed it at are "
        "the surface it really uses. So the run is not asking &ldquo;is "
        "this a good binder&rdquo; &mdash; we already know it is. It is "
        "asking whether the model can recognise a real binder when it sees "
        "one, which is the only way to learn what a trustworthy score "
        "looks like before you spend one on a design of your own."
    ),
    "inputs_used": [
        (
            "Antigen structure",
            "1UBQ, chain A",
            "Ubiquitin, 76 residues. The whole chain; ubiquitin is small "
            "enough that there is nothing to trim.",
        ),
        (
            "Binder sequence",
            "hHR23A UBA1 domain",
            "Pasted as plain sequence. Boltz-2 folds it against the "
            "antigen &mdash; it does not design anything, so what you get "
            "back is a verdict on the sequence you brought.",
        ),
        (
            "Hotspot residues",
            "8, 44, 68, 70",
            "The hydrophobic patch centred on Ile44 &mdash; the face "
            "ubiquitin-binding domains dock onto. Naming it lets the run "
            "report whether the predicted complex actually lands there, "
            "rather than merely scoring well somewhere else.",
        ),
        (
            "Preset",
            "msa_server",
            "Builds a multiple-sequence alignment for the antigen before "
            "folding. Slower than the single-sequence path and worth it "
            "on a target with plenty of known relatives, which ubiquitin "
            "emphatically has.",
        ),
    ],
    "runtime": "2 minutes, 120 seconds of GPU time",
    "what_came_back": (
        "One complex, scored ipTM 0.894, pTM 0.916 and complex pLDDT 92.6, "
        "and flagged <code>strict_pass</code>. All four of the hotspots we "
        "named were contacted &mdash; 4 of 4, shown in the contact grid "
        "below the table."
    ),
    "how_to_read_it": (
        "ipTM is the model's confidence in the INTERFACE, as opposed to "
        "pTM, which covers the fold as a whole; a design can fold "
        "beautifully and still not touch the target, and comparing the two "
        "is how you catch that. The strict-pass bar on this page is "
        "complex pLDDT above 85, ipTM above 0.7 and at least four hotspot "
        "hits together, so 0.894 with 4 of 4 clears it on every count. "
        "Read this run as the calibration point: it is roughly what a "
        "genuine binder at a genuine epitope looks like. A number well "
        "below it on your own sequence is the useful result, not the "
        "disappointing one &mdash; it is the answer arriving in two "
        "minutes instead of after a month at the bench."
    ),
    "what_we_did_next": (
        "Nothing &mdash; this run existed to establish what a good score "
        "looks like on this page. On your own candidates the next step is "
        "to paste the rest of them and fold them in one submission, then "
        "take the few that clear the bar into the lab. A binder that "
        "scores well here has cleared a prediction, not an experiment."
    ),
}
