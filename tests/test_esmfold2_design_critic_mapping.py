"""Regression: the scFv gate must actually read a proxy value.

``_shape_designs`` used to select the distogram proxies with
``CRITIC_SCALING_PROXY in critic_name``, where CRITIC_SCALING_PROXY was
``"ESMFold2-Experimental-Fast-base"``. That string is not a substring of any
critic name the pinned upstream emits, so the branch never fired,
``cdr_distogram_iptm_proxy`` stayed None, and ``_classify`` — which gates
antibodies on that field alone — returned ``drop`` for every scFv design ever
run. Reproduced 2026-08-23 against ``ranomics-esmfold2-design-prod``: 13
designs on two targets, 13 dropped, real iPTM 0.621-0.949.

The critic names and CDR proxy values below are the real rows from that run
(``results/raw/critic_results.json``, one representative design). Runs fully
offline: ``_shape_designs`` is a pure function once ``complex`` is None.
"""

from __future__ import annotations

import json
import logging

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
