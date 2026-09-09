"""Regression: the scFv gate must actually read a proxy value.

``_shape_designs`` used to select the distogram proxies with
``CRITIC_SCALING_PROXY in critic_name``, where CRITIC_SCALING_PROXY was
``"ESMFold2-Experimental-Fast-base"``. That string DOES match upstream names —
it is upstream's own scaling-critic detector, and the scaling checkpoints are
built as ``f"ESMFold2-Experimental-Fast-base{size}-step{step}k"`` — but it
matches scaling rows and ONLY scaling rows, and ``use_scaling_critics``
defaults to False. So on the default path no row matched,
``cdr_distogram_iptm_proxy`` stayed None, and ``_classify`` — which gates
antibodies on that field alone — returned ``drop`` for every scFv design ever
run. Reproduced 2026-08-23 against ``ranomics-esmfold2-design-prod``: 13
designs on two targets, 13 dropped, real iPTM 0.621-0.949.

(The earlier wording here claimed that string matched nothing upstream. It is
contradicted by SCFV_ROWS below, whose last row is a real scaling-critic name,
and by run_pipeline.py's own comment on CRITIC_REAL_IPTM.)

The critic names and CDR proxy values below are the real rows from that run
(``results/raw/critic_results.json``, one representative design).

Runs fully offline. ``_shape_designs`` is a pure function while ``complex`` is
None; the tests that supply a real complex — to prove a structure follows its
own row — take the ``stub_pdb`` fixture, because ``_save_complex_pdb`` mkdir()s
PDB_OUTPUT_DIR before its try block and would touch the host filesystem.
"""

from __future__ import annotations

import json
import logging

import pytest

from tools.esmfold2_design import run_pipeline
from tools.esmfold2_design.run_pipeline import (
    CRITIC_REAL_IPTM,
    STRICT_CDR_IPTM_PROXY,
    _shape_designs,
)

SEQ = "TARGETSEQ|QVQLVQSGGGGGSGGGSGGGSGGGSDIQMTQ"

# Every critic emits its own proxy for the same design, hence the bug report's
# warning that a fix must NAME the critic it reads rather than take whichever
# row happens to sort first.
# CRITIC_REAL_IPTM sits in the MIDDLE deliberately. With it last, "take every
# row, last wins" passes; with it first, "first wins" passes. Only a row picked
# by name yields 0.7665 from this ordering.
# The trailing row is a real scaling-critic name (upstream builds them as
# f"ESMFold2-Experimental-Fast-base{size}-step{step}k"), which is what the
# deleted CRITIC_SCALING_PROXY substring actually matched. It is present so a
# revert to substring matching produces 0.1111 and fails loudly.
SCFV_ROWS = [
    {"critic_name": "ESMFold2-Experimental-Fast", "designed_sequence": SEQ,
     "cdr_distogram_iptm_proxy": 0.7437, "iptm": None, "final_loss": None,
     "complex": None},
    {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": SEQ,
     "cdr_distogram_iptm_proxy": 0.7665, "iptm": 0.923, "final_loss": 0.31,
     "complex": None},
    {"critic_name": "ESMFold2-Experimental-Fast-Cutoff2025", "designed_sequence": SEQ,
     "cdr_distogram_iptm_proxy": 0.8460, "iptm": None, "final_loss": None,
     "complex": None},
    {"critic_name": "ESMFold2-Experimental", "designed_sequence": SEQ,
     "cdr_distogram_iptm_proxy": 0.8192, "iptm": None, "final_loss": None,
     "complex": None},
    {"critic_name": "ESMFold2-Experimental-Fast-base600m-step10k",
     "designed_sequence": SEQ, "cdr_distogram_iptm_proxy": 0.1111,
     "iptm": None, "final_loss": None, "complex": None},
]


def test_scfv_proxy_comes_from_the_iptm_critic_and_passes():
    (design,) = _shape_designs(SCFV_ROWS, is_antibody=True)

    # 0.7665 is the Cutoff2025 row specifically, not 0.8460 (highest) and not
    # 0.7437 (first). Pin the source, not just "some number arrived".
    assert design["cdr_distogram_iptm_proxy"] == 0.7665
    assert design["iptm"] == 0.923
    assert design["scores"]["cdr_distogram_iptm_proxy"] == 0.7665
    assert design["cdr_distogram_iptm_proxy"] > STRICT_CDR_IPTM_PROXY
    assert design["filter_status"] == "strict_pass"


def test_minibinder_proxy_is_populated_too():
    """The same dead branch also blanked the minibinder proxy column."""
    rows = [
        {"critic_name": "ESMFold2-Experimental-Fast", "designed_sequence": SEQ,
         "distogram_iptm_proxy": 0.51, "cdr_distogram_iptm_proxy": float("nan"),
         "iptm": None, "final_loss": None, "complex": None},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": SEQ,
         "distogram_iptm_proxy": 0.62, "cdr_distogram_iptm_proxy": float("nan"),
         "iptm": 0.81, "final_loss": 0.29, "complex": None},
    ]
    (design,) = _shape_designs(rows, is_antibody=False)
    assert design["distogram_iptm_proxy"] == 0.62


def test_missing_critic_is_logged_not_silent(caplog):
    """An upstream rename must be readable in the logs, not a silent all-drop."""
    renamed = [dict(row, critic_name=row["critic_name"] + "-v2") for row in SCFV_ROWS]
    with caplog.at_level(logging.ERROR, logger="esmfold2_design_pipeline"):
        (design,) = _shape_designs(renamed, is_antibody=True)
    assert design["filter_status"] == "drop"
    assert CRITIC_REAL_IPTM in caplog.text
    assert "ESMFold2-Experimental-Fast-v2" in caplog.text


def test_minibinder_nan_cdr_proxy_never_reaches_the_result_json():
    """Upstream emits NaN, not None, for a non-antibody's CDR proxy.

    ``compute_distogram_iptm_proxy`` returns ``float("nan")`` whenever
    ``is_antibody`` is false, and ``design_binder`` spreads it into every
    critic row. While the proxy branch was dead this never surfaced; reading
    the proxy off the scoring row is exactly what makes it reachable.

    NaN is not valid JSON. httpx serialises request bodies with
    ``allow_nan=False``, so the persist write raises, ``_cas_update`` swallows
    it and reports no transition, and the job never leaves ``running`` -- a
    full H100 charged for a result that is never stored. Asserting on
    ``allow_nan=False`` here reproduces that encoder, not a proxy for it.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": SEQ,
         "distogram_iptm_proxy": 0.62, "cdr_distogram_iptm_proxy": float("nan"),
         "iptm": 0.81, "final_loss": 0.29, "complex": None},
    ]
    (design,) = _shape_designs(rows, is_antibody=False)

    assert design["cdr_distogram_iptm_proxy"] is None
    assert design["scores"]["cdr_distogram_iptm_proxy"] is None
    json.dumps(design["scores"], allow_nan=False)


def test_duplicate_sequence_takes_every_field_from_one_row():
    """batch_size > 1 can converge two seeds onto the same sequence.

    Scores were assigned last-wins while complex/final_loss were first-wins,
    so the user got one row's structure underneath another row's numbers.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": SEQ,
         "cdr_distogram_iptm_proxy": 0.80, "iptm": 0.90, "final_loss": 0.10,
         "complex": None},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": SEQ,
         "cdr_distogram_iptm_proxy": 0.20, "iptm": 0.40, "final_loss": 1.10,
         "complex": None},
    ]
    (design,) = _shape_designs(rows, is_antibody=True)

    assert (design["iptm"], design["cdr_distogram_iptm_proxy"],
            design["final_loss"]) == (0.90, 0.80, 0.10)


def test_unscored_designs_rank_below_scored_ones():
    """The old key returned -1 for None, which sorts below every real -iptm."""
    rows = [
        {"critic_name": "ESMFold2-Experimental-Fast", "designed_sequence": "T|AAAA",
         "cdr_distogram_iptm_proxy": 0.9, "iptm": None, "final_loss": None,
         "complex": None},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": "T|CCCC",
         "cdr_distogram_iptm_proxy": 0.8, "iptm": 0.91, "final_loss": 0.2,
         "complex": None},
    ]
    designs = _shape_designs(rows, is_antibody=True)

    assert [d["iptm"] for d in designs] == [0.91, None]
    assert designs[0]["rank"] == 0


def test_empty_critic_results_is_not_reported_as_a_rename(caplog):
    with caplog.at_level(logging.ERROR, logger="esmfold2_design_pipeline"):
        assert _shape_designs([], is_antibody=True) == []
    assert CRITIC_REAL_IPTM not in caplog.text



# ---------------------------------------------------------------------------
# The bucket must be claimed by a row that actually carries a score.
#
# #242 made the FIRST CRITIC_REAL_IPTM row win outright, holes included, to
# stop one row's PDB appearing under another row's numbers. The holes are the
# problem: a blank row claims the bucket and the real fold behind it is thrown
# away. Reachable on any locked-framework scFv run, where only the CDRs vary,
# so two batch elements converging on one designed_sequence is ordinary, and a
# diverged fold emitting NaN is ordinary too.
#
# Users RANK these designs -- the ordering and the numbers are the product;
# ``filter_status`` is a label on top of them.
# ---------------------------------------------------------------------------

CONVERGED = "T|QVQLVQSGGGGGSGGGSGGGSGGGSDIQMTQ"
WORSE = "T|EVQLVESGGGGGSGGGSGGGSGGGSDIQMTQ"


@pytest.fixture
def stub_pdb(monkeypatch):
    """Keep _save_complex_pdb off the filesystem.

    It calls ``PDB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)`` before its
    try block, so any row carrying a non-None complex creates /tmp/results for
    real and then logs a write failure. Every test that supplies a complex
    takes this fixture; the rest pass ``complex: None``, which returns early.

    The key is derived from the COMPLEX, not from the design name the real
    function uses. That is deliberate: keyed by name, no assertion anywhere
    can tell "the winning row's structure" from "some other row's structure",
    because every row on a sequence yields the same design name. Tests that
    give their complexes distinct string values can then assert which row's
    structure actually survived. Tests that only need "a PDB exists" assert
    ``is not None`` and are unaffected by the naming.
    """
    monkeypatch.setattr(
        run_pipeline,
        "_save_complex_pdb",
        lambda complex_obj, name, *a, **kw: (
            None if complex_obj is None else f"{complex_obj}_complex.pdb"
        ),
    )


def _converged_rows():
    """Two batch elements on one sequence, plus a genuinely worse design.

    Row 1 diverged: upstream emits NaN, not None, for a fold that blew up.
    Row 2 is the same sequence folded successfully at 0.91. Row 3 is a
    different, real, WORSE design at 0.40 -- it is what makes this a ranking
    assertion rather than a "some number arrived" one.
    """
    return [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": float("nan"), "cdr_distogram_iptm_proxy": float("nan"),
         "final_loss": float("nan"), "complex": None},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": 0.91, "cdr_distogram_iptm_proxy": 0.80,
         "final_loss": 0.20, "complex": object()},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": WORSE,
         "iptm": 0.40, "cdr_distogram_iptm_proxy": 0.55,
         "final_loss": 0.90, "complex": object()},
    ]


def test_a_blank_row_must_not_discard_a_real_score_and_invert_the_ranking(
    stub_pdb,
):
    """The regression for the #242 ``_scored`` rule.

    Before: iptm order [0.40, None] -- the best design blank and ranked LAST,
    behind a design that really is worse. After: [0.91, 0.40].
    """
    designs = _shape_designs(_converged_rows(), is_antibody=True)

    assert [d["iptm"] for d in designs] == [0.91, 0.40]
    assert designs[0]["designed_sequence"] == CONVERGED
    assert designs[0]["rank"] == 0
    # The whole row is salvaged, not just the ranking key.
    assert designs[0]["cdr_distogram_iptm_proxy"] == 0.80
    assert designs[0]["scores"]["iptm"] == 0.91


def test_the_salvaged_row_keeps_its_complex_and_final_loss(stub_pdb):
    """``complex`` and ``final_loss`` are dropped by the same rule.

    No complex means ``_save_complex_pdb`` returns None, which means no PDB
    written, nothing to download and no NGL viewer for the run's best design.
    Stubbed rather than written: the real function targets /tmp/results and
    this assertion is about which row the complex came from, not about file IO.
    """
    designs = _shape_designs(_converged_rows(), is_antibody=True)

    assert designs[0]["final_loss"] == 0.20
    # Presence, not the exact name: a concurrent fix is changing how the name
    # that goes into this key is chosen, and this test is not about that.
    assert designs[0]["pdb_key"] is not None
    assert all(d["pdb_key"] is not None for d in designs)


def test_the_first_scored_row_still_wins_every_field(stub_pdb):
    """#242's field-coherence property, kept.

    Row 1 has the iPTM, so it is a scored row and claims the bucket outright.
    Every other field is a hole, and row 2 has all of them. Filling any one of
    them per-field would put two different folds in one line of the results
    table -- a 0.91 iPTM beside a proxy, a loss, or a STRUCTURE belonging to
    the 0.30 fold.

    All three holes are here on purpose. With the hole only in the proxy, a
    per-field fill of ``complex`` or ``final_loss`` goes unnoticed, and those
    are the fields where taking the wrong row is worst: ``complex`` is the PDB
    the customer downloads.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": 0.91, "cdr_distogram_iptm_proxy": None, "final_loss": None,
         "complex": None},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": 0.30, "cdr_distogram_iptm_proxy": 0.20, "final_loss": 0.90,
         "complex": object()},
    ]
    (design,) = _shape_designs(rows, is_antibody=True)

    assert design["iptm"] == 0.91
    assert design["cdr_distogram_iptm_proxy"] is None
    assert design["final_loss"] is None
    # The winning row had no complex, so there is no PDB to offer. Taking
    # row 2's would hand the customer the 0.30 fold's structure under the
    # 0.91 design's name.
    assert design["pdb_key"] is None







# ---------------------------------------------------------------------------
# A scoreless row is still a usable row.
#
# The first cut of this fix refused to assign from any row without a finite
# iPTM. That looked like the same thing and was not: it threw away the
# structure, final_loss and proxy of any design whose only row diverged, and
# in scFv mode it dropped designs the tool's own filter passes. Both are
# pinned below.
# ---------------------------------------------------------------------------


def test_a_diverged_iptm_must_not_drop_a_passing_scfv():
    """_classify gates antibodies on the CDR proxy ALONE -- never on iPTM.

    So a NaN-iPTM row carrying a real 0.85 CDR proxy is a strict_pass, and
    discarding it for having no iPTM hands ``_pick_best`` a design the tool's
    own filter rejects. That is the #241 defect reached by a new route, and it
    needs only ONE diverged fold.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": "T|GOODCDRS",
         "iptm": float("nan"), "cdr_distogram_iptm_proxy": 0.85,
         "final_loss": 0.20, "complex": None},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": "T|WEAKCDRS",
         "iptm": 0.60, "cdr_distogram_iptm_proxy": 0.30,
         "final_loss": 0.90, "complex": None},
    ]
    designs = _shape_designs(rows, is_antibody=True)
    good = next(d for d in designs if d["designed_sequence"] == "T|GOODCDRS")

    assert good["cdr_distogram_iptm_proxy"] == 0.85
    assert good["filter_status"] == "strict_pass"
    best = run_pipeline._pick_best(designs)
    assert best["designed_sequence"] == "T|GOODCDRS"
    assert best["filter_status"] == "strict_pass"


def test_a_scoreless_row_still_supplies_the_structure_it_carries(stub_pdb):
    """A design whose ONLY row diverged keeps its complex, loss and proxy.

    Refusing to assign from it leaves the design with nothing at all -- no
    complex, so no PDB written, no download and no viewer -- which is the very
    harm this change cites as its reason to exist. One diverged fold reaches
    this; the converged-duplicate bug needs two rows on one sequence.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": "T|DIVERGED",
         "iptm": float("nan"), "distogram_iptm_proxy": 0.62,
         "final_loss": 3.69, "complex": object()},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": "T|CLEANFOLD",
         "iptm": 0.93, "distogram_iptm_proxy": 0.71, "final_loss": 0.20,
         "complex": object()},
    ]
    designs = _shape_designs(rows, is_antibody=False)
    diverged = next(d for d in designs if d["designed_sequence"] == "T|DIVERGED")

    assert diverged["iptm"] is None            # it really did diverge
    assert diverged["distogram_iptm_proxy"] == 0.62
    assert diverged["final_loss"] == 3.69
    assert diverged["pdb_key"] is not None     # ...and is still downloadable
    assert diverged["rank"] == 1               # ranked last, not discarded


def test_a_scored_row_replaces_a_scoreless_incumbent_WHOLESALE(stub_pdb):
    """The replacement takes every field, so no line mixes two folds.

    The incumbent carries NO score -- both its iPTM and its proxy diverged --
    but it does carry a structure and a loss. Wholesale replacement discards
    those along with everything else, and that is the point: salvaging them
    field by field would put the diverged fold's PDB and loss beside the good
    fold's 0.91, which is exactly the mixing #242 set out to stop.

    The incumbent holds the only complex here on purpose. With both rows at
    ``complex: None`` a salvage-the-incumbent's-complex bug is invisible, and
    complex is the field where taking the wrong row costs most: it is the PDB
    the customer downloads.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": float("nan"), "cdr_distogram_iptm_proxy": float("nan"),
         "final_loss": 9.90, "complex": "divergedfold"},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": 0.91, "cdr_distogram_iptm_proxy": 0.80, "final_loss": 0.20,
         "complex": None},
    ]
    (design,) = _shape_designs(rows, is_antibody=True)

    assert (design["iptm"], design["cdr_distogram_iptm_proxy"],
            design["final_loss"]) == (0.91, 0.80, 0.20)
    # The winner had no complex, so there is no PDB. The incumbent's
    # "divergedfold" is gone with the rest of its row, NOT salvaged underneath
    # the winner's numbers. Named rather than object() on purpose: the real
    # _save_complex_pdb returns None for any complex it cannot serialise, so
    # with a bare object() this assertion passes whether or not the salvage
    # happened, and the stub keys the pdb_key off the complex so it cannot.
    assert design["pdb_key"] is None


def test_only_the_first_scored_row_wins__a_second_one_does_not_replace_it():
    """Replacement happens once, when the incumbent has no score.

    Otherwise the last scored row would win and #242's determinism would be
    back to front.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": 0.91, "cdr_distogram_iptm_proxy": 0.80, "final_loss": 0.20,
         "complex": None},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": 0.30, "cdr_distogram_iptm_proxy": 0.20, "final_loss": 0.90,
         "complex": None},
    ]
    (design,) = _shape_designs(rows, is_antibody=True)

    assert (design["iptm"], design["cdr_distogram_iptm_proxy"],
            design["final_loss"]) == (0.91, 0.80, 0.20)



def test_a_scored_row_must_not_evict_a_scoreless_row_that_PASSES(stub_pdb):
    """The election runs on "carries any score", not on the iPTM.

    This is the case that makes the distinction load-bearing. Row 1 diverged
    (no iPTM) but carries a real 0.85 CDR proxy, which in scFv mode is a
    ``strict_pass`` -- ``_classify`` gates antibodies on that field ALONE and
    never reads iptm. Row 2 has an iPTM and a failing 0.10 proxy.

    Electing on the iPTM lets row 2 evict row 1, turning a strict_pass into a
    drop and flipping ``_pick_best`` onto a design the tool's own filter
    rejects. That is the #241 defect by a new route, and it is WORSE than
    origin/main, which keeps row 1. An earlier cut of this fix shipped exactly
    that.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": float("nan"), "cdr_distogram_iptm_proxy": 0.85,
         "final_loss": 0.20, "complex": "goodfold"},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": 0.30, "cdr_distogram_iptm_proxy": 0.10,
         "final_loss": 0.90, "complex": "badfold"},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": WORSE,
         "iptm": 0.20, "cdr_distogram_iptm_proxy": 0.42,
         "final_loss": 0.80, "complex": "otherfold"},
    ]
    designs = _shape_designs(rows, is_antibody=True)
    conv = next(d for d in designs if d["designed_sequence"] == CONVERGED)

    assert conv["cdr_distogram_iptm_proxy"] == 0.85
    assert conv["filter_status"] == "strict_pass"
    assert conv["pdb_key"] == "goodfold_complex.pdb"
    best = run_pipeline._pick_best(designs)
    assert best["designed_sequence"] == CONVERGED
    assert best["filter_status"] == "strict_pass"


def test_a_scoreless_row_is_replaced_by_the_first_row_carrying_ANY_score(
    stub_pdb,
):
    """Two diverged rows where only the second carries a usable number.

    Neither has an iPTM, so an iPTM-based election leaves the design blank --
    origin/main and the first cut both do. The 0.85 proxy is a real
    measurement and a passing one.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": float("nan"), "cdr_distogram_iptm_proxy": float("nan"),
         "final_loss": float("nan"), "complex": None},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": float("nan"), "cdr_distogram_iptm_proxy": 0.85,
         "final_loss": 0.20, "complex": "goodfold"},
    ]
    (design,) = _shape_designs(rows, is_antibody=True)

    assert design["iptm"] is None                       # it really did diverge
    assert design["cdr_distogram_iptm_proxy"] == 0.85
    assert design["filter_status"] == "strict_pass"
    assert design["pdb_key"] == "goodfold_complex.pdb"


def test_between_two_scoreless_rows_the_FIRST_one_holds_the_slot(stub_pdb):
    """Neither row carries a score, so nothing outranks anything: order wins.

    Without this, "last scoreless row wins" is indistinguishable from the
    intended rule, and a design's structure would depend on critic ordering.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": float("nan"), "cdr_distogram_iptm_proxy": float("nan"),
         "final_loss": 1.10, "complex": "firstfold"},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": float("nan"), "cdr_distogram_iptm_proxy": float("nan"),
         "final_loss": 2.20, "complex": "secondfold"},
    ]
    (design,) = _shape_designs(rows, is_antibody=True)

    assert design["pdb_key"] == "firstfold_complex.pdb"
    assert design["final_loss"] == 1.10



def test_the_winner_s_blank_final_loss_is_not_filled_from_the_loser(stub_pdb):
    """Wholesale replacement, pinned for final_loss specifically.

    The incumbent carries a real 9.90 loss and no score; the winner carries
    scores and a NaN loss. Salvaging the loss field-by-field puts the diverged
    fold's 9.90 beside the good fold's 0.91 -- the mixing #242 exists to stop.
    No other test supplies a WINNING row whose final_loss is blank, so without
    this the salvage branch is never executed.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": float("nan"), "cdr_distogram_iptm_proxy": float("nan"),
         "final_loss": 9.90, "complex": "divergedfold"},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": CONVERGED,
         "iptm": 0.91, "cdr_distogram_iptm_proxy": 0.80,
         "final_loss": float("nan"), "complex": "goodfold"},
    ]
    (design,) = _shape_designs(rows, is_antibody=True)

    assert design["iptm"] == 0.91
    assert design["final_loss"] is None          # NOT 9.90
    assert design["pdb_key"] == "goodfold_complex.pdb"
