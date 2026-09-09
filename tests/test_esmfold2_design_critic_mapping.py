"""Regression: the scFv gate must actually read a proxy value.

``_shape_designs`` used to select the distogram proxies with
``CRITIC_SCALING_PROXY in critic_name``, where CRITIC_SCALING_PROXY was
``"ESMFold2-Experimental-Fast-base"``. That string DOES match upstream names —
it is upstream's own scaling-critic detector, and the scaling checkpoints are
``f"ESMFold2-Experimental-Fast-base{size}-step{step}k"`` — but it matches
scaling rows and only scaling rows, and the scaling ensemble was off by
default. So on the default path no row matched, ``cdr_distogram_iptm_proxy``
stayed None, and ``_classify`` — which gates antibodies on that field alone —
returned ``drop`` for every scFv design ever run. Reproduced 2026-08-23 against
``ranomics-esmfold2-design-prod``: 13 designs on two targets, 13 dropped, real
iPTM 0.621-0.949.

The critic names and CDR proxy values below are the real rows from that run
(``results/raw/critic_results.json``, one representative design). Runs fully
offline: ``_shape_designs`` is a pure function once ``complex`` is None.
"""

from __future__ import annotations

import json
import logging
import re

from tools.esmfold2_design.run_pipeline import (
    CRITIC_REAL_IPTM,
    STRICT_CDR_IPTM_PROXY,
    _shape_designs,
)

SEQ = "TARGETSEQ|QVQLVQSGGGGGSGGGSGGGSGGGSDIQMTQ"

_LOGGER = "esmfold2_design_pipeline"


def _records(caplog):
    """Only this pipeline's records, so an unrelated logger cannot mask one."""
    return [r for r in caplog.records if r.name == _LOGGER]


_BLANK_RE = re.compile(r"value for (.+?) across")


def _blank_fields(caplog):
    """The blank-field list this record actually reported.

    NOT a bare substring test. ``"iptm"`` is a substring of both
    ``"distogram_iptm_proxy"`` and ``"cdr_distogram_iptm_proxy"``, so
    ``"iptm" in caplog.text`` is satisfied by a message over-claiming BOTH
    fields blank. Two such mutants walked through the text assertions this
    replaced.

    Two readings, because each survives churn the other does not:

    ``record.args[0]`` is the value as PASSED, so it is immune to any
    rewording of the format string -- and this file has already reworded these
    messages once. But an f-string logging call empties ``record.args`` to
    ``()`` (verified, not assumed), which would make this an IndexError rather
    than a result.

    So fall back to the rendered message, anchored between ``value for`` and
    `` across`` rather than searched bare. An over-claim renders
    ``value for iptm, distogram_iptm_proxy across``, whose ``, <other field>``
    falls INSIDE the capture, so the comparison fails in either ordering.
    The anchor is what does the work; it also tolerates a change to the
    joiner, which must not matter for a single-field message.
    """
    (record,) = _records(caplog)
    if record.args:
        return record.args[0]
    match = _BLANK_RE.search(record.getMessage())
    assert match, f"cannot read the blank-field list from {record.getMessage()!r}"
    return match.group(1)


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
    """A run with no rows at all is a different failure, reported by the caller.

    Asserting only ``CRITIC_REAL_IPTM not in caplog.text`` defanged this: the
    zero-designs branch never names the critic, so deleting the
    ``if critic_results`` guard left the test green while an empty run logged a
    spurious ERROR. Mutation-confirmed. Assert the silence itself.
    """
    with caplog.at_level(logging.ERROR, logger="esmfold2_design_pipeline"):
        assert _shape_designs([], is_antibody=True) == []
    assert CRITIC_REAL_IPTM not in caplog.text
    assert _records(caplog) == []


# ---------------------------------------------------------------------------
# The tripwire, and the flag whose removal it outlived.
# ---------------------------------------------------------------------------


def test_blank_proxy_is_logged_even_when_the_critic_is_named_correctly(caplog):
    """The case a critic-NAME check cannot see, i.e. the original bug.

    ``_warn_if_scores_missing`` replaced an ``_assert_critic_present`` that
    checked only whether CRITIC_REAL_IPTM appeared among the row names. Through
    all 13 prod drops that critic was present and correctly named; what was
    missing was the proxy, because it was being sourced off scaling rows. A
    name check stays silent here. Checking the shaped output does not.
    """
    no_proxy = [
        dict(row, cdr_distogram_iptm_proxy=None) for row in SCFV_ROWS
    ]
    with caplog.at_level(logging.ERROR, logger="esmfold2_design_pipeline"):
        (design,) = _shape_designs(no_proxy, is_antibody=True)
    assert design["iptm"] is not None, "iPTM still sourced; only the proxy went"
    assert design["filter_status"] == "drop"
    # The proxy alone. iPTM came through, so claiming it blank would be the
    # same over-claim in the other direction.
    assert _blank_fields(caplog) == "cdr_distogram_iptm_proxy"
    assert CRITIC_REAL_IPTM in caplog.text


def test_rows_that_shape_no_designs_are_logged(caplog):
    """Upstream renaming designed_sequence yields COMPLETED with 0 designs."""
    renamed_key = [
        {k: v for k, v in row.items() if k != "designed_sequence"}
        for row in SCFV_ROWS
    ]
    with caplog.at_level(logging.ERROR, logger="esmfold2_design_pipeline"):
        assert _shape_designs(renamed_key, is_antibody=True) == []
    assert "shaped 0 designs" in caplog.text


def test_a_minibinder_run_does_not_false_alarm_on_the_cdr_proxy(caplog):
    """Only the proxy the preset gates on is checked.

    Upstream emits the CDR proxy as NaN on minibinder runs, so it is
    legitimately None for every design there. Checking both proxies would log
    an error on every healthy minibinder run and teach everyone to ignore it.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": SEQ,
         "distogram_iptm_proxy": 0.62, "cdr_distogram_iptm_proxy": float("nan"),
         "iptm": 0.81, "final_loss": 0.29, "complex": None},
    ]
    with caplog.at_level(logging.ERROR, logger="esmfold2_design_pipeline"):
        designs = _shape_designs(rows, is_antibody=False)
    assert designs and designs[0]["cdr_distogram_iptm_proxy"] is None
    assert designs[0]["distogram_iptm_proxy"] == 0.62
    assert caplog.text == ""


def test_no_reachable_path_loads_the_scaling_ensemble():
    """The 15-checkpoint ensemble is never loaded, and cannot be switched on.

    It loads on the host with device="cpu" against a 10 GB ``memory=``, and
    since every score is read off a hero critic its rows are never consulted:
    ticking it bought an OOM risk and no change to any number. If someone
    reintroduces the toggle, ``memory=`` has to be raised in the same change.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pipeline = (root / "tools" / "esmfold2_design" / "run_pipeline.py").read_text(
        encoding="utf-8"
    )
    assert "designer.load(False)" in pipeline
    # Any argument other than the literal False means something can flip it.
    assert "designer.load(use_scaling_critics)" not in pipeline

    form = (
        root / "templates" / "tools" / "esmfold2_design_form.html"
    ).read_text(encoding="utf-8")
    assert 'name="use_scaling_critics"' not in form


def test_diverged_run_is_not_reported_as_a_missing_critic(caplog):
    """The alarm may not assert a cause it cannot distinguish.

    Every fold diverged (upstream emits NaN iPTM) but the proxies came through
    fine. The blank field is iptm and iptm only. An earlier wording said "every
    critic-sourced score is None" regardless of what was actually missing --
    false here, with proxies at 0.62 and 0.71 on the shaped designs -- and then
    named a critic that the very same line lists under "Critics seen". Pointing
    at a healthy critic sends the reader to look for a rename that did not
    happen, which is the confident misdiagnosis this guard exists to replace.
    """
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": seq,
         "iptm": float("nan"), "distogram_iptm_proxy": proxy,
         "cdr_distogram_iptm_proxy": float("nan"),
         "final_loss": float("nan"), "complex": None}
        for seq, proxy in (("T|AAAA", 0.62), ("T|CCCC", 0.71))
    ]
    with caplog.at_level(logging.ERROR, logger="esmfold2_design_pipeline"):
        designs = _shape_designs(rows, is_antibody=False)

    assert [d["distogram_iptm_proxy"] for d in designs] == [0.62, 0.71]
    assert all(d["iptm"] is None for d in designs)

    # EXACTLY iptm. Not "contains iptm" -- that is satisfied by the substring
    # inside "distogram_iptm_proxy", which is how an over-claiming mutant
    # survives a text assertion here.
    assert _blank_fields(caplog) == "iptm"
    # The proxies are populated, so no claim that everything is blank.
    assert "every critic-sourced score is None" not in caplog.text
    # Both readings offered, neither asserted.
    assert "diverged" in caplog.text
    assert "Either" in caplog.text


def test_null_sequences_are_not_reported_as_a_rename(caplog):
    """``seq is None`` catches failed folds too, not only a renamed key."""
    rows = [
        {"critic_name": CRITIC_REAL_IPTM, "designed_sequence": None,
         "iptm": 0.9, "distogram_iptm_proxy": 0.6,
         "cdr_distogram_iptm_proxy": float("nan"), "final_loss": 0.2,
         "complex": None},
    ]
    with caplog.at_level(logging.ERROR, logger="esmfold2_design_pipeline"):
        assert _shape_designs(rows, is_antibody=False) == []
    # One record, not one-or-more: the blank-score branch must not also fire.
    assert len(_records(caplog)) == 1
    assert "shaped 0 designs" in caplog.text
    assert "Either those folds failed" in caplog.text
    # "likely renamed" asserted a cause the skip cannot distinguish.
    assert "likely renamed" not in caplog.text
