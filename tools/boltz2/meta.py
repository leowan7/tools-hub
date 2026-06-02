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
    examples             — C2 "Load example" chip entries.
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
    "Single-sequence cofold + interface confidence (ipTM), antibody-trained "
    "and orthogonal to AF2-multimer. For sequence design, use ProteinMPNN; "
    "for de novo backbones, use RFantibody, BindCraft, or BoltzGen first."
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
        "informative. Returns a folded complex PDB + ipTM + pTM + "
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
        "Antigen PDB / mmCIF (single chain — the binder is added separately).",
        "One or more binder sequences (scFv, nanobody, peptide, anything "
        "that folds as a single protein chain), 20-400 aa each.",
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
                "independently against the antigen. 20-400 aa per binder, "
                "up to 50 binders per run."
            ),
        },
        {
            "name": "Preset",
            "explanation": (
                "<strong>Single-sequence</strong> (default) folds in "
                "<code>msa: empty</code> mode &mdash; the right choice "
                "for designed sequences. <strong>With MSA</strong> "
                "fetches MSAs from the public ColabFold MMseqs2 endpoint "
                "&mdash; slower but more accurate on natural sequences."
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


# Sample inputs for the C2 "Load example" chips. Each entry's PDB lives
# at ``tools/boltz2/examples/<filename>``. ``params`` overrides form
# fields via the ``?example=<id>`` prefill path; the bundled PDB is fed
# in via the ``example:`` pdb_source token resolved at submit time.
#
# The strict-pass scFv below is design index 1 from the
# ``S3_3B9K_yts156xE4_consistent_loops`` round-2 cell, picked because
# the prior research-tier validation showed all-7-hotspots contacted
# with complex_pLDDT=0.916, ipTM=0.870 (see
# scratch/scfv-designs/round2/validation/S3_3B9K_yts156xE4_consistent_loops_boltz_sseq.json
# entry index 1). Reproducing those numbers through the production
# pipeline is the canonical smoke test.
_STRICT_PASS_SCFV = (
    "QVRLQESGPGLVQPSQTLSLTCSVSGFSLTSDSVHWVRQPPGKGLEWMGGIWADGDTEYNSALKSRLSIS"
    "RDTSKSQGFLKMNSLQTDDTAIYFCTSNRGSYYFDYWGQGTMVTVSSGGGGSGGGGSGGGGSDIKMTQSP"
    "ASLSASLGDKVTITCKASQNIDKYMAWYQQKPGKAPRQLIHYTSTLVSGTPSRFSGSGSGRDYTFSISSV"
    "ESEDIASYYCLQYDNLYTFGAGTKLELK"
)

examples: list[dict] = [
    {
        "id": "cd8a-strict-pass",
        "label": "CD8a + strict-pass scFv",
        "description": (
            "117-aa CD8a antigen + a strict-pass scFv from the S3xE4 "
            "round-2 design campaign. Reproduces complex_pLDDT ~0.92, "
            "ipTM ~0.87, all 7 hotspot contacts in <1 min."
        ),
        "filename": "antigen_cd8a.pdb",
        "params": {
            "preset": "standalone",
            "target_chain": "A",
            "hotspot_residues": "55,56,57,71,72,73,74",
            "binder_sequences": f">strict_pass_S3xE4_1\n{_STRICT_PASS_SCFV}",
        },
    },
]
