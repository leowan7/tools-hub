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
# ONLY NARRATE COLUMNS THE PAGE ACTUALLY SHOWS. opendde has NO entry in
# shared/result_columns.py and none in shared/score_legends.py either, so
# there are no per-tool bars to quote and nothing to call a pass. The columns
# the partial renders are Ranking score, pTM, ipTM and pLDDT; all four are
# used below and no threshold is asserted for any of them, because this tool
# genuinely has none. The narration compares the four predictions to each
# other instead, which is what the run actually supports.
#
# WHY FOUR SEEDS AND ONE SAMPLE PER SEED. Samples-per-seed was tried first,
# at 4 samples x 2 seeds, and the eight predictions came back with only TWO
# distinct score sets -- one per seed, repeated across that seed's samples.
# The structures were all different (eight distinct md5s); the SCORES were
# shared, because _read_score_json in tools/opendde/run_pipeline.py falls
# back to the first *.json it finds beside the structure when no filename
# matches the sample stem, and OpenDDE writes one score file per seed
# directory. One sample per seed sidesteps it: each seed dir holds one
# structure and one score file, so the pairing cannot go wrong. The fallback
# is still live for any multi-sample run and is filed separately -- do not
# raise "Samples / seed" in this example until it is fixed.
EXAMPLE: dict | None = {
    "target": (
        "Bovine trypsin with benzamidine bound &mdash; chain A of "
        "<strong>PDB 3PTB</strong>, 220 residues, plus the ligand "
        "<code>CCD_BEN</code>."
    ),
    "why_this_target": (
        "OpenDDE is for complexes that are not all protein, so an all-protein "
        "example would be demonstrating the one case it is not for. This is "
        "the textbook protein-plus-small-molecule pair: benzamidine sitting "
        "in the trypsin S1 pocket, deposited in 1982 and used to check docking "
        "methods ever since. It is also a deliberate correction. The first "
        "run this page ever showed folded a single ubiquitin chain, which has "
        "no second entity at all &mdash; its ipTM of 0 was arithmetically "
        "correct and told you nothing, because there was no interface to "
        "score."
    ),
    "inputs_used": [
        (
            "Protein chains (FASTA)",
            "the 220-residue trypsin chain",
            "One record. The <code>&gt;A</code> header is optional &mdash; "
            "chain ids are auto-assigned when you leave them off.",
        ),
        (
            "Ligands (one per line)",
            "CCD_BEN",
            "Benzamidine, by its three-letter Chemical Component Dictionary "
            "code. A bare SMILES string works too, but a CCD code names one "
            "unambiguous molecule and needs no interpretation.",
        ),
        (
            "Samples / seed",
            "1",
            "One prediction per seed. All of the variation in this run comes "
            "from the seeds instead.",
        ),
        (
            "Seeds",
            "4",
            "Four independent starting points, so four predictions. This is "
            "the input that matters most here, and the results below are the "
            "argument for it: the spread across seeds is much wider than any "
            "single prediction would let you guess.",
        ),
        (
            "Diffusion steps",
            "200",
            "The upstream default, left alone. More steps trade compute for "
            "quality; nothing about this run suggested it was step-limited.",
        ),
        (
            "Recycles",
            "10",
            "Also the default. Worth raising on a complex that comes back "
            "geometrically incoherent, which this one did not.",
        ),
    ],
    "what_came_back": (
        "Four predictions in about three minutes. Ranking score "
        "runs <strong>0.45 to 0.63</strong>, ipTM 0.421 to 0.652, pTM "
        "0.524 to 0.557, and pLDDT 48.8 to 52.3."
    ),
    "how_to_read_it": (
        "Two of these columns move together and two do not. "
        "The ranking score is an ipTM sort in all but name. It reads 0.63, "
        "0.53, 0.53, 0.45 down the table &mdash; the middle two are a tie at "
        "the precision shown &mdash; and ipTM breaks that tie in the same "
        "order it sets the rest: 0.652, 0.536, 0.533, 0.421. What the model "
        "is ranking on is its "
        "confidence in the protein-ligand contact. pTM barely moves at all "
        "(0.524 to 0.557), which is what you would expect: the trypsin fold "
        "is not the uncertain part, only where the benzamidine sits. "
        "<strong>pLDDT is the column to be careful with.</strong> It sits "
        "between 48.8 and 52.3 for every prediction and it does <em>not</em> "
        "follow the ranking. The prediction with the highest pLDDT is the one "
        "ranked <em>last</em>: 52.3, with the worst ipTM of the four at "
        "0.421. Sort this table by pLDDT, take the top row, and you have "
        "picked the worst-docked of the four. Near 50 is low in absolute "
        "terms as well, and it stays near 50 whatever the seed, so treat it "
        "as a standing reason to look at the pose rather than as something "
        "that separates these four. "
        "<strong>Then the case for running more than one.</strong> The "
        "default is a single seed, seed 1 &mdash; and seed 1 is the top row "
        "here, ipTM 0.652. Run this once and you would have come away "
        "believing 0.652. The other three seeds say 0.536, 0.533 and "
        "0.421. "
        "The "
        "honest summary of this target is the spread, not its best member."
    ),
    "what_we_did_next": (
        "Took the spread as the result. Four predictions that disagree this "
        "much are telling you the pose is not settled, so the next move is "
        "not to pick the winner &mdash; it is to look at whether the top two "
        "or three put the ligand in the same pocket at all, which is a "
        "question for the 3D viewer rather than for any column here. If they "
        "agree on the pocket and disagree on the detail, the prediction is "
        "usable and worth more seeds. If they disagree on the pocket, no "
        "number in this table would have told you, and the run has still "
        "earned its cost by saying so."
    ),
    "cost_usd": "0.78",
    "runtime": "3 minutes",
    # Read by components/worked_example.html into the stub job's created_at so
    # a date-gated era notice knows when this ran. Job created_at, matching
    # the other examples' convention.
    "ran_on": "2026-09-09T15:52:57Z",
}
