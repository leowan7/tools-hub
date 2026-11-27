"""Static metadata for the OpenDDE tool (About panel, citation).

Plain-data module — no adapter import. The About renderer and cost preview read
these constants.
"""

from __future__ import annotations

from typing import Optional

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
    "output_summary": (
        "A ranked set of predicted complexes (mmCIF/PDB) with the model's own "
        "confidence ranking score per prediction. On a run of your own every "
        "row carries its structure, to download or to open in the viewer."
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
# EXAMPLE — one real past run of this tool, rendered by
# templates/components/worked_example.html above the tool's own results
# partial. Captured from job 4a3e3203 on 2026-09-09; example/result.json
# is that job's payload as scripts/capture_example_result.py wrote it:
# the script scrubs its SENSITIVE_KEYS list, provider_job_id among
# them, and trims inline structure blobs. Neither touched these rows:
# opendde emits no SENSITIVE_KEYS member per design and no inline blob
# (the rows carry a pdb_key reference), so the scrub had nothing to take.
# Checked against the shipped payload, not asserted.
# ---------------------------------------------------------------------------
# ONLY NARRATE COLUMNS THE PAGE ACTUALLY SHOWS. opendde has NO entry in
# shared/result_columns.py and none in shared/score_legends.py either, so
# there are no per-tool bars to quote and nothing to call a pass. The columns
# the partial renders are Ranking score, pTM, ipTM and pLDDT; all four are
# used below and no threshold is asserted for any of them. Numbers are quoted
# at the precision the TABLE prints (ranking 2dp, ipTM/pTM 3dp, pLDDT 1dp),
# so every figure in the prose can be checked against the row above it.
#
# THIS IS THE SECOND CAPTURE, AND THE FIRST ONE TAUGHT A FALSE LESSON.
# The first run used a 220-residue sequence that was NOT 3PTB chain A: three
# residues were missing at PDB positions 184A, 188A and 221A -- every
# insertion-coded position in the chain and nothing else -- which is what
# a sequence extracted while ignoring insertion codes looks like. Verified
# against the deposited file: chain A is 223 residues in both SEQRES and
# ATOM, those three are its only insertion codes, and collapsing them
# leaves exactly 220. An earlier version of this comment named 187 as the
# third and called two of the three insertion-coded; both were wrong.
# On that corrupted input pTM barely moved,
# pLDDT sat flat near 50 and ANTI-correlated with the ranking, and seed 1 was
# the best of four. Every one of those reverses on the real chain: pTM ranges
# 0.433-0.625, pLDDT ranges 42.8-52.9 and tracks the ranking, and seed 1 is
# the WORST of the four. Do not restore the old narration.
#
# WHY FOUR SEEDS AND ONE SAMPLE PER SEED. Samples-per-seed was tried at
# 4 samples x 2 seeds and the eight predictions came back with only TWO
# distinct score sets, one per seed. The structures were all different (eight
# distinct md5s); the SCORES were shared, because _read_score_json in
# tools/opendde/run_pipeline.py falls back to the first *.json beside the
# structure when no filename matches the sample stem, and OpenDDE writes one
# score file per seed directory. One sample per seed sidesteps it.
# FIXED SINCE THIS RUN: #245 (main 30f6116) replaced that glob with an
# exact per-sample lookup that records no score rather than borrowing a
# neighbour's. This example still runs one sample per seed because that is
# what was captured, not because the bug is live.
#
# Do not read that as "the figures below avoided the fallback". They did
# not. The stem preference could never match at all: upstream interleaves
# "_summary_confidence" between the base name and the sample index, so
# "opendde_job_sample_0" is not a substring of
# "opendde_job_summary_confidence_sample_0" and every read fell through to
# the alphabetical pick. These figures are sound because that pick landed
# on the right file anyway, not because it was bypassed. What this repo
# can show: run_pipeline.py builds a fixed command and passes no flag
# requesting an atom-level "full_data" dump. What it CANNOT show:
# upstream's own default for writing one, since OpenDDE is invoked as a
# binary and is not vendored here -- an earlier version of this comment
# called that half provable and it is not. Had such a file been written
# it sorts ahead of the confidence one and carries no ranking_score, so
# the same shape would have returned nothing. #245 removes the dependence
# either way.
EXAMPLE: dict | None = {
    "target": (
        "The target is bovine trypsin, chain A of <strong>PDB 3PTB</strong> "
        "(UniProt P00760) at 223 residues, with benzamidine (CCD_BEN) "
        "bound."
    ),
    "inputs_used": [
        (
            "Protein chains (FASTA)",
            "the 223-residue trypsin chain",
            "We pasted the trypsin chain as a single record. It is worth a "
            "moment if you are preparing this yourself, because 3PTB is "
            "numbered in the chymotrypsin convention, so the chain carries "
            "three insertion-coded positions, 184A, 188A and 221A. A "
            "script that reads residue numbers and ignores the insertion "
            "letter silently drops all three. Our first attempt at this "
            "example came back 220 residues instead of 223 for exactly "
            "that reason, and handed the model a different protein. Count "
            "what you extract against the deposited length before you "
            "submit it.",
        ),
        (
            "Ligands (one per line)",
            "CCD_BEN",
            "We named benzamidine by its three-letter Chemical Component "
            "Dictionary code. A bare SMILES string works too, but a CCD "
            "code names one unambiguous molecule and needs no "
            "interpretation.",
        ),
        (
            "Samples / seed",
            "1",
            "We asked for one prediction per seed. All of the variation in "
            "this run comes from the seeds instead.",
        ),
        (
            "Seeds",
            "4",
            "We ran four independent starting points, so four predictions "
            "came back. This is the input that matters most here, and the "
            "results below are the argument for it.",
        ),
        (
            "Diffusion steps",
            "200",
            "We left this at the upstream default of 200. More steps trade "
            "compute for quality, and nothing about this run suggested it "
            "was step-limited.",
        ),
        (
            "Recycles",
            "10",
            "We left this at the default of 10 as well. It is worth raising on a "
            "complex that comes back geometrically incoherent, which this "
            "one did not.",
        ),
    ],
    "what_came_back": (
        "Four predictions came back in about three and a half minutes, and "
        "they split cleanly <strong>two and two</strong>. The top pair "
        "scores 0.67 and 0.66 on the ranking, and the bottom pair scores "
        "0.48 and 0.47. ipTM runs 0.471 "
        "to 0.689, pTM 0.433 to 0.625, and pLDDT 42.8 to 52.9."
    ),
    "how_to_read_it": (
        "All four columns put the same two predictions on top, at ipTM "
        "0.689 and 0.670 against 0.493 and 0.471. Read that as a ranking "
        "among these four only, because every pLDDT sits in the 40s or "
        "low 50s. The form&rsquo;s default single seed is the bottom row "
        "here."
    ),
    "cost_usd": "0.86",
    "runtime": "3.5 minutes",
    # Read by components/worked_example.html into the stub job's created_at so
    # a date-gated era notice knows when this ran. Job created_at, matching
    # the other examples' convention.
    "ran_on": "2026-09-09T17:40:13Z",
}
