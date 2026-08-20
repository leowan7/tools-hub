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
# EVERY NUMBER BELOW IS A RECORDED FACT FROM THAT RUN. Provenance: the
# MDM2 peptide campaign, step 06 (Boltz-2 cofold of the design panel plus
# three literature reference peptides at three seeds each). ipTM is the
# binder:target pair value from each prediction's own confidence JSON, not
# the all-pairs figure; pLDDT is complex_plddt from the same file; the
# contact counts were measured against the 14 cleft residues. filter_status
# was not hand-assigned — it is run_pipeline.classify() called on those
# three numbers, so the label means exactly what it means on a live job.
#
# THE REFERENCE PEPTIDES ARE NOT ROWS IN THE PAYLOAD, deliberately. They
# carry no contact count, so the hotspot grid would have rendered them
# "0 / 14" — claiming the native p53 ligand touches none of the cleft it
# is defined by. Their numbers live in the narration instead, where they
# can be labelled. The band quoted below is the full min-to-max across
# all three seeds of each.
#
# NO DESIGNED SEQUENCES. The peptides are Ranomics campaign output; the
# page teaches the reader to read the scores without handing over the
# designs. The three references are published literature and are named.
#
# No cost_usd: this was campaign compute, not a wallet-billed hub job, so
# there is no per-run dollar figure that would mean anything to a reader.
# The estimate on the form is the live number for their own inputs.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "MDM2 &mdash; PDB <code>1YCR</code>, chain A, 85 residues. The "
        "pocket p53 binds."
    ),
    "why_this_target": (
        "Because this one can be marked. MDM2 is the most heavily "
        "characterised protein-protein interface in drug discovery: the "
        "natural ligand is known, several tighter binders have been "
        "published, and every one of them has a measured affinity. So we "
        "can fold designs and known binders through the identical path "
        "and read the designs against a real scale rather than against a "
        "threshold somebody picked."
    ),
    "inputs_used": [
        (
            "Antigen structure",
            "1YCR, chain A",
            "MDM2's p53-binding domain. Chain B in that file is the p53 "
            "peptide itself and was removed &mdash; leaving it in would "
            "have let the model copy the answer.",
        ),
        (
            "Binder sequences",
            "12 designed peptides, 12 to 20 residues",
            "Submitted in one batch. Boltz-2 designs nothing; it folds "
            "what you give it against the target and scores the "
            "interface, so a batch is just twelve independent verdicts.",
        ),
        (
            "Hotspot residues",
            "54, 57, 58, 61, 62, 67, 72, 75, 86, 91, 93, 96, 99, 100",
            "The 14 residues lining the cleft &mdash; L54, L57, G58, "
            "I61, M62, Y67, Q72, V75, F86, F91, V93, H96, I99, Y100 "
            "&mdash; where p53's Phe19, Trp23 and Leu26 insert. Naming "
            "them makes the run report whether a design lands in the "
            "pocket, instead of only whether it scores well somewhere.",
        ),
        (
            "Preset",
            "msa_server",
            "Builds an alignment for the antigen before folding. Worth "
            "it on a target with many known relatives.",
        ),
    ],
    "what_came_back": (
        "Twelve complexes, every one <code>strict_pass</code>. ipTM runs "
        "from 0.874 to 0.952, complex pLDDT from 91.3 to 97.1, and each "
        "design contacts 13 or 14 of the 14 cleft residues."
    ),
    "how_to_read_it": (
        "Read the numbers against the scale, not against zero. Folded "
        "through the identical path, three published MDM2 binders scored: "
        "the native p53 peptide 0.933&ndash;0.941, PDI 0.930&ndash;0.935, "
        "and PMI 0.905&ndash;0.912, each across three seeds. The designs "
        "sit inside that band and the better half sit above it &mdash; "
        "which is the useful reading of 0.94, rather than &ldquo;0.94 "
        "sounds high&rdquo;. "
        "Now the part worth carrying away: PMI is the tightest of the "
        "three at roughly 3 nM, an order of magnitude better than the "
        "native peptide, and it scored the LOWEST of the three. ipTM "
        "tells you the model is confident these two things form the "
        "complex you asked about. It does not rank affinity, and nothing "
        "on this page does. Treat a good score as a reason to make the "
        "molecule, never as a predicted K&#8321;."
    ),
    "what_we_did_next": (
        "Took the panel into a developability screen &mdash; several of "
        "the twelve were then rejected on protease liability, which the "
        "interface score says nothing about &mdash; and carried what "
        "survived toward synthesis. On your own candidates the same shape "
        "works: fold the batch, keep what clears the bar, and let the "
        "next filter be the one this score cannot see."
    ),
}
