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
SCFV_ROWS = [
    {"critic_name": "ESMFold2-Experimental-Fast", "designed_sequence": SEQ,
     "cdr_distogram_iptm_proxy": 0.7437, "iptm": None, "final_loss": None,
     "complex": None},
    {"critic_name": "ESMFold2-Experimental-Fast-Cutoff2025", "designed_sequence": SEQ,
     "cdr_distogram_iptm_proxy": 0.8460, "iptm": None, "final_loss": None,
     "complex": None},
    {"critic_name": "ESMFold2-Experimental", "designed_sequence": SEQ,
     "cdr_distogram_iptm_proxy": 0.8192, "iptm": None, "final_loss": None,
     "complex": None},
    {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": SEQ,
     "cdr_distogram_iptm_proxy": 0.7665, "iptm": 0.923, "final_loss": 0.31,
     "complex": None},
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
         "distogram_iptm_proxy": 0.51, "iptm": None, "final_loss": None,
         "complex": None},
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": SEQ,
         "distogram_iptm_proxy": 0.62, "iptm": 0.81, "final_loss": 0.29,
         "complex": None},
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
