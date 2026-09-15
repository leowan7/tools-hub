"""Regression: the scFv gate must read iPTM, not the CDR proxy alone.

``_classify`` gated antibodies on ``cdr_distogram_iptm_proxy`` and never
looked at ``iptm``, while the minibinder branch beside it required
``iptm >= STRICT_IPTM``. The two modes therefore applied different rigour
under one word, and ``strict_pass`` is what the results page's order panel
offers for peptide synthesis.

MEASURED, not constructed. Every row below is from a production run of
``ranomics-esmfold2-design-prod`` recorded in docs/VALIDATION-LOG.md under
"ESMFold2 binder design" -- preset ``scfv``, target ``cd45``, binder
``trastuzumab_framework_vhvl``, ``batch_size=6``, ``seed=0``. The row this
exists for is job ``verify242-bs6-1789054528``: proxy 0.618, ipTM **0.436**,
classified ``strict_pass``.

TWO COPIES OF THE ARITHMETIC ARE PINNED HERE, because run_pipeline.py is
copied into the GPU image and cannot import shared/, so the results template
carries its own. The template's header says that copy is held to the pipeline
by no test. For these twelve rows it now is.
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

import pytest

from tools.esmfold2_design.run_pipeline import (
    STRICT_CDR_IPTM_PROXY,
    STRICT_IPTM,
    _classify,
    _pick_best,
)

# Job verify242-bs6-1789054528, 2026-09-10 15:45 UTC. The log pairs these
# explicitly ("proxy 0.618 with ipTM 0.436" and so on). The 0.396/0.400 pair
# is the one place the pairing is ambiguous in the source text, and it does
# not matter: both proxies are below the bar, so both rows drop on the proxy
# leg whichever ipTM belongs to which.
RUN_2 = [
    # (cdr proxy, iptm, tier BEFORE this change, tier AFTER).
    # ``before`` is DOCUMENTATION -- the parametrisation binds it to ``_before``
    # and asserts only ``after``. Pinning it would mean importing origin/main's
    # classifier into the suite, which is not worth the coupling; it is
    # recorded so a reader can see what moved and what did not.
    (0.3960, 0.117, "drop", "drop"),
    (0.3999, 0.102, "drop", "drop"),
    (0.618, 0.436, "strict_pass", "drop"),
    (0.706, 0.716, "strict_pass", "borderline"),
    (0.799, 0.844, "strict_pass", "strict_pass"),
    (0.821, 0.898, "strict_pass", "strict_pass"),
]

# Job verify242-bs6-1789012528, 2026-09-10 03:55 UTC. The log gives the two
# lists without saying which proxy belongs to which design, so this zip is an
# assumption -- and an irrelevant one: every proxy clears 0.50 and every ipTM
# clears 0.75, so all 36 cross-product pairings AND all 720 permutations give
# ``strict_pass``. (An earlier version of this note said the log records both
# lists sorted. The ipTM list is ascending; the proxy list is not -- 0.8639
# precedes 0.8599 -- which is mild evidence that the log preserved per-design
# order and the zip is real rather than assumed. Either way the row's point
# stands: the new leg changes nothing on this run.)
RUN_1 = [
    (0.7337, 0.7936),
    (0.7844, 0.8297),
    (0.7884, 0.8397),
    (0.8639, 0.8856),
    (0.8599, 0.8959),
    (0.8728, 0.9119),
]


def _tier(proxy, iptm) -> str:
    """scFv classification. pI is None by construction in this mode."""
    return _classify(True, iptm, None, proxy, None)


class TestClassifier:
    @pytest.mark.parametrize("proxy,iptm,_before,after", RUN_2)
    def test_run_2_rows(self, proxy, iptm, _before, after):
        assert _tier(proxy, iptm) == after

    def test_the_row_this_exists_for(self):
        """Stated on its own so a failure names the defect, not a row index.

        ipTM 0.436 is below the number the minibinder branch rejects at
        (STRICT_IPTM - 0.05 = 0.70), so consistency between the two modes
        makes it a drop.

        Not a demotion to ``borderline``, and the distinction is not the one
        an earlier version of this docstring drew. It claimed borderline
        "would still leave it in the panel's fallback pool" -- true, but so
        does ``drop``: ``_pick_best`` returns ``designs[0]`` at any tier, so
        neither word removes a design from consideration. What separates them
        is evidence. 0.436 is a MEASURED interface that failed; the cap to
        ``borderline`` in
        ``test_an_unmeasured_iptm_caps_the_tier_but_does_not_fail_it`` is for
        an interface that was never measured at all. A measured failure is a
        drop in both modes.
        """
        assert _tier(0.618, 0.436) == "drop"

    @pytest.mark.parametrize("proxy,iptm", RUN_1)
    def test_run_1_is_untouched(self, proxy, iptm):
        assert _tier(proxy, iptm) == "strict_pass"

    def test_both_legs_are_load_bearing(self):
        """Neither leg alone reproduces the gate.

        Written against the CONSTANTS rather than literals: this asserts the
        shape of the conjunction, and test_derived_verdicts holds the numbers
        themselves to score_legends.
        """
        assert _tier(STRICT_CDR_IPTM_PROXY, STRICT_IPTM) == "strict_pass"
        # Proxy clears, iPTM does not.
        assert _tier(STRICT_CDR_IPTM_PROXY, STRICT_IPTM - 0.2) == "drop"
        # iPTM clears, proxy does not -- the pre-existing leg, still biting.
        assert _tier(STRICT_CDR_IPTM_PROXY - 0.2, STRICT_IPTM) == "drop"

    def test_an_unmeasured_iptm_caps_the_tier_but_does_not_fail_it(self):
        """An absent iPTM is not a failed iPTM, and the difference is not
        cosmetic.

        The first cut of this leg returned ``drop`` for both, which is what
        QC caught: ``_pick_best`` falls through to ``designs[0]`` when no tier
        is occupied, and that list is iPTM-sorted with the UNMEASURED last, so
        calling an unmeasured design ``drop`` handed the order panel the
        best of the designs the gate had actually rejected. Measured then: an
        scFv at proxy 0.90 with no iPTM lost the pick to one at proxy 0.10 /
        iPTM 0.20. ``test_the_unmeasured_design_still_wins_the_pick`` below is
        the behavioural guard; this one pins the tier word it rests on.

        A missing PROXY is still a flat drop -- it is the antibody-side
        precondition, and nothing else in the row speaks to it.
        """
        assert _tier(0.9, None) == "borderline"   # capped, not failed
        assert _tier(0.45, None) == "drop"        # proxy under its own bar
        assert _tier(None, 0.9) == "drop"         # no CDR evidence at all
        # Still not a strict pass: that is the whole point of the leg.
        assert _tier(0.9, None) != "strict_pass"

    def test_the_unmeasured_design_still_wins_the_pick(self):
        """The regression the cap exists to prevent, asserted end to end.

        A design with a strong CDR proxy and no measured iPTM must still beat
        one that WAS measured and failed. This is ``_pick_best``'s tier order,
        not its untiered fallback -- the fallback sorts on iPTM and puts the
        unmeasured design last, which is exactly the trap.
        """
        unmeasured = {"sequence": "GOOD", "iptm": None,
                      "filter_status": _tier(0.90, None)}
        measured_bad = {"sequence": "BAD", "iptm": 0.20,
                        "filter_status": _tier(0.10, 0.20)}
        # iPTM-sorted the way _shape_designs emits: unmeasured LAST.
        designs = [measured_bad, unmeasured]
        assert _pick_best(designs)["sequence"] == "GOOD"

    def test_the_minibinder_branch_did_not_move(self):
        """The whole change is meant to leave minibinder mode alone."""
        assert _classify(False, STRICT_IPTM, None, None, 5.0) == "strict_pass"
        assert _classify(False, STRICT_IPTM, None, None, 6.0) == "drop"
        assert _classify(False, 0.72, None, None, 5.0) == "borderline"
        assert _classify(False, 0.60, None, None, 5.0) == "drop"
        assert _classify(False, None, None, None, 5.0) == "drop"


# --- the template's second copy of the same arithmetic --------------------


@pytest.fixture
def flask_app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    return create_app()


SEQ = "SEEDLTKAQNLIDEAKKLNDAQAPKG"


def _render(flask_app, *, is_antibody, scores) -> str:
    """One candidate, rendered, on whichever mode is asked for.

    No stored ``filter_status``: the panel derives its own word, which is the
    copy under test.
    """
    result = {
        "status": "COMPLETED",
        "preset": "scfv" if is_antibody else "minibinder",
        "is_antibody": is_antibody,
        "designs_total": 1,
        "designs_completed": 1,
        "n_seeds": 1,
        "best_sequence": SEQ,
        "designs": [],
        "candidates": [
            {
                "rank": 0,
                "name": "design_0",
                "pdb_key": "design_0_complex.pdb",
                "sequence": SEQ,
                "designed_sequence": "TARGET|" + SEQ,
                "scores": scores,
            }
        ],
    }
    job = SimpleNamespace(id="scfv-leg", status="succeeded", result=result)
    with flask_app.test_request_context():
        return flask_app.jinja_env.get_template(
            "tools/esmfold2_design_results.html"
        ).render(job=job, send_target_tools=None)


def _render_one(flask_app, proxy, iptm) -> str:
    """One scFv candidate, rendered.

    The proxy goes in under the LEGACY ``iPTM_proxy`` spelling: that is what
    every scFv run stored before the 2026-09-14 column split and what the GPU
    image keeps writing until it is redeployed, so these rows exercise the
    template's fallback read as well as its arithmetic.
    """
    return _render(
        flask_app, is_antibody=True,
        scores={"ipTM": iptm, "iPTM_proxy": proxy, "final_loss": 1.0,
                "pI": None},
    )


def _read_text(path: str) -> str:
    """A repo file as text, resolved from this file rather than the cwd."""
    return (pathlib.Path(__file__).resolve().parent.parent / path).read_text(
        encoding="utf-8",
    )


def _table_columns(html: str) -> list[str]:
    """The candidate table's column keys, in header order.

    ``data-col`` on the header cell (templates/components/candidate_table.html
    :520) -- the key, beside the label the cell renders. The table is emitted
    twice per page (wide and narrow), so the list is de-duplicated while
    keeping first-seen order.
    """
    seen = []
    for column in re.findall(r'data-col="([^"]+)"', html):
        if column not in seen:
            seen.append(column)
    return seen


def _render_minibinder(flask_app, proxy, iptm, pi) -> str:
    """The same page on the OTHER mode, for the containment assertions."""
    return _render(
        flask_app, is_antibody=False,
        scores={"ipTM": iptm, "iPTM_proxy": proxy, "final_loss": 1.0,
                "pI": pi},
    )


def _badge(html: str):
    """The tier word in the best-design panel's badge, or None.

    Scoped after the panel title: the run's tier renders in an identical
    ``panel-badge`` span higher up the page.
    """
    flat = re.sub(r"\s+", " ", html)
    start = flat.find("Best design")
    assert start != -1, "best-design panel did not render"
    match = re.search(
        r'<span class="panel-badge"[^>]*>\s*([a-z ]+?)\s*</span>', flat[start:]
    )
    return match.group(1) if match else None


# What the template renders for each word the pipeline can return. A strict
# pass carries no badge at all -- AND NEITHER DOES AN UNJUDGED DESIGN, which
# is why the badge alone is not a sufficient anchor and every case below also
# asserts on the panel's prose. Without that, a render that silently became
# "unmeasured" would satisfy the strict_pass expectation of None.
_BADGE_FOR = {
    "strict_pass": None,
    "borderline": "borderline",
    "drop": "rejected",
}

# Rendered above the copy-paste box when the offered design fell short, and
# the different sentence used when it was never measured against the bar.
_CAUTION = "No design in this run clears the bar stated above"
_UNCHECKED = "has not been checked against the bar"


# SYNTHETIC BOUNDARY ROWS, and they are here because the measured ones do not
# reach every literal. Mutation-checked: widening the template's ``_iptm_band``
# from 0.05 to 0.15 left all 12 production rows green, because none of them has
# an iPTM in [0.60, 0.70) where that band bites. These do. One pair straddles
# each threshold in each leg, plus the unmeasured-iPTM cap.
BOUNDARY = [
    (0.50, 0.75),    # both exactly on the bar -> strict_pass (>= is inclusive)
    (0.50, 0.7499),  # iPTM a hair under -> borderline
    (0.50, 0.70),    # exactly on the borderline floor
    (0.50, 0.6999),  # a hair under it -> drop.  PINS _iptm_band.
    (0.4999, 0.90),  # proxy a hair under its bar, iPTM strong -> borderline
    (0.40, 0.90),    # proxy exactly on its own floor
    (0.3999, 0.90),  # proxy under the floor -> drop.  PINS _band.
    (0.90, None),    # unmeasured iPTM with a passing proxy -> capped
    (0.45, None),    # unmeasured iPTM, proxy under its bar -> drop
]


@pytest.mark.parametrize(
    "proxy,iptm", [(p, i) for p, i, _b, _a in RUN_2] + RUN_1 + BOUNDARY
)
def test_the_template_agrees_with_the_pipeline(flask_app, proxy, iptm):
    """The page's derived tier word and ``_classify`` must not drift apart.

    They are two hand-maintained copies of one rule and nothing else holds
    them together -- the drift guard in tests/test_derived_verdicts.py reads
    run_pipeline.py and shared/score_legends.py, never this template.
    """
    tier = _tier(proxy, iptm)
    html = _render_one(flask_app, proxy, iptm)
    flat = re.sub(r"\s+", " ", html)
    assert _badge(html) == _BADGE_FOR[tier], (
        "proxy=%s ipTM=%s: pipeline says %r" % (proxy, iptm, tier)
    )
    # The badge cannot separate strict_pass from unjudged -- both render none
    # -- so pin the prose too, or half this parametrisation asserts nothing.
    assert _UNCHECKED not in flat, (
        "proxy=%s ipTM=%s: page says it was never measured, pipeline judged "
        "it %r" % (proxy, iptm, tier)
    )
    assert (_CAUTION in flat) == (tier != "strict_pass"), (
        "proxy=%s ipTM=%s: caution prose disagrees with tier %r"
        % (proxy, iptm, tier)
    )


# The copy-paste box itself, not "somewhere on the page" -- the same anchor
# tests/test_esmfold2_design_best_sequence.py uses, and for the same reason:
# an earlier version of that file anchored on a Jinja COMMENT that never
# renders, so its "block" was the whole document.
_SEQ_BOX = re.compile(
    r"word-break: break-all;[^>]*>\s*([A-Za-z]+)\s*</div>", re.S
)


def test_the_page_no_longer_offers_the_0_436_design_unqualified(flask_app):
    """The customer-visible half of the defect: this design was rendered in
    the copy-paste synthesis box with no caution and no badge.

    Asserts on the BOX, not just the page, because the box is what gets
    pasted into a synthesis order -- and asserts the caution precedes it,
    because a warning below the thing being copied is not a warning.
    """
    html = _render_one(flask_app, 0.618, 0.436)
    assert _badge(html) == "rejected"
    flat = re.sub(r"\s+", " ", html)
    assert _CAUTION in flat
    box = _SEQ_BOX.search(html)
    assert box, "no sequence box rendered; this assertion would be hollow"
    assert box.group(1) == SEQ
    assert flat.index(_CAUTION) < flat.index(SEQ)


# --- where the new column is allowed to appear ----------------------------
#
# ``CDR_iPTM_proxy`` is a MODE-SCOPED column: run_pipeline.py emits it on an
# scFv run and ``iPTM_proxy`` on a minibinder one, so a surface that lists it
# unconditionally would name a CDR quantity over a whole-interface number.
# Three comments elsewhere cite "the containment is pinned by this file"
# (shared/score_legends.py, templates/jobs_compare.html and
# templates/tools/esmfold2_design_results.html); these are what they cite.


def test_the_cdr_column_is_a_leg_of_the_scfv_bar_alone():
    """MODE_GATE_COLUMNS, scfv only -- and not GATE_COLUMNS at all.

    A tool-wide entry would hold every MINIBINDER design to a column that is
    absent by construction on that mode, leaving them all unjudged: the same
    failure the pI leg would cause in the other direction, and the reason
    esmfold2-design is absent from GATE_COLUMNS entirely.
    """
    from shared.score_legends import GATE_COLUMNS, MODE_GATE_COLUMNS

    assert "esmfold2-design" not in GATE_COLUMNS
    modes = MODE_GATE_COLUMNS["esmfold2-design"]
    assert modes["scfv"] == ("CDR_iPTM_proxy", "ipTM")
    assert "CDR_iPTM_proxy" not in modes["minibinder"]
    for tool, columns in GATE_COLUMNS.items():
        assert "CDR_iPTM_proxy" not in columns, tool


def test_the_cdr_column_is_on_the_antibody_branch_of_the_results_page_alone(
    flask_app,
):
    """The results template picks the key by mode, the way the payload does.

    Rendered both ways and read off the table headers, because the column
    list is built in Jinja and the header is where it becomes visible. The
    scFv row here stores the LEGACY ``iPTM_proxy`` spelling -- what every run
    delivered before the 2026-09-14 split holds, and what the GPU image keeps
    writing until it is redeployed -- so this also shows the template's
    fallback read reaching it.
    """
    antibody = _render_one(flask_app, 0.618, 0.436)
    assert _table_columns(antibody) == ["ipTM", "CDR_iPTM_proxy", "final_loss"]
    assert "CDR distogram proxy" in antibody, "the header shows the label"

    minibinder = _render_minibinder(flask_app, 0.70, 0.90, 5.6)
    assert _table_columns(minibinder) == [
        "ipTM", "iPTM_proxy", "final_loss", "pI",
    ]
    assert "CDR_iPTM_proxy" not in minibinder
    assert "CDR distogram proxy" not in minibinder


def test_the_cdr_column_stays_out_of_the_compare_priority_keys():
    """jobs_compare.html must NOT list it, and the reason is the alias.

    ``raw_metric`` resolves ``_COLUMN_ALIASES``, whose tuple for this column
    ends in the mode-blind legacy spelling ``iPTM_proxy`` -- that entry is
    what lets pre-split scFv rows judge at all. The cost is that asking a
    MINIBINDER record for ``CDR_iPTM_proxy`` answers with its whole-interface
    proxy rather than None, so a compare column headed "CDR distogram proxy"
    would fill with numbers that are not CDR proxies. Asserted below, not
    assumed.
    """
    from shared.score_legends import _COLUMN_ALIASES, raw_metric

    source = _read_text("templates/jobs_compare.html")
    keys = re.search(r"set priority_keys = \[([^\]]*)\]", source)
    assert keys, "priority_keys is no longer a literal list; re-read this test"
    assert "CDR_iPTM_proxy" not in keys.group(1)

    assert _COLUMN_ALIASES["CDR_iPTM_proxy"][-1] == "iPTM_proxy"
    minibinder = {"scores": {"ipTM": 0.90, "iPTM_proxy": 0.70}}
    assert raw_metric(minibinder, "CDR_iPTM_proxy") == 0.70


def test_the_share_chain_can_reach_exactly_three_renamed_columns():
    """WHICH cards moved when the clause started printing glossary names.

    An enumeration, not a rendering check -- the rendering is
    ``test_the_share_card_quotes_the_cdr_proxy_by_name``. This one exists so
    that a future tool whose gate leg is an underscored storage key trips a
    test instead of quietly putting that key in an og:title.

    The reachable set is enumerated by DRIVING ``_share_headline_metric``
    itself rather than re-deriving its arms: every candidate column is made
    readable, the chain is asked what it would quote, that column and its
    alias spellings are removed, and the ask repeats until it goes silent.
    Three of the ten columns it can reach have a label that differs from
    their key; the other seven are ipTM/pLDDT spellings whose label IS the
    key, which is why nothing before this change looked wrong.
    """
    import blueprints.jobs as jobs_bp
    from shared import metric_glossary, result_columns
    from shared.score_legends import (
        _COLUMN_ALIASES, GATE_COLUMNS, MODE_GATE_COLUMNS,
    )

    candidates = set(jobs_bp._PLDDT_PREFERENCE)
    for columns in GATE_COLUMNS.values():
        candidates.update(columns)
    for modes in MODE_GATE_COLUMNS.values():
        for columns in modes.values():
            candidates.update(columns)
    for tool in result_columns._TOOL_PRIMARY_METRIC:
        key, _direction = result_columns.primary_metric_for(tool)
        if key:
            candidates.add(key)

    tools = (set(GATE_COLUMNS) | set(MODE_GATE_COLUMNS)
             | set(result_columns._TOOL_PRIMARY_METRIC)
             | set(result_columns._TOOL_RESULT_COLUMNS))
    reachable = set()
    for tool in sorted(tools):
        for mode in list(MODE_GATE_COLUMNS.get(tool, {})) or [None]:
            scores = {column: 7.0 for column in candidates}
            while True:
                chosen = jobs_bp._share_headline_metric(
                    tool, mode, {"scores": scores},
                )
                if chosen is None:
                    break
                reachable.add(chosen[0])
                for spelling in _COLUMN_ALIASES.get(
                    chosen[0], (chosen[0],),
                ):
                    scores.pop(spelling, None)

    renamed = {
        column for column in reachable
        if metric_glossary.get(column).get("label") != column
    }
    assert renamed == {
        "CDR_iPTM_proxy", "epitope_contacts", "n_hotspot_contacts",
    }, sorted(reachable)


def test_no_lower_is_better_leg_can_be_the_quoted_column():
    """Which coarse legs the share clause can actually contradict.

    ``_share_headline_metric`` prints at a fixed .3f while ``judge`` decides
    at the glossary format, so a leg declared coarser than .3f can be
    published on the far side of the bar the clause says it met. Seven leg
    columns are coarser, and an earlier draft of the comment at
    blueprints/jobs.py:433 treated all seven as exposed and offered "pI 5.995
    under a 6.0 bar" as the example. It is not exposed: the gate arm takes a
    leg only when ``_higher_is_better``, so no lower-is-better leg is ever the
    quoted column, and pI is one.

    Driven the same way as
    ``test_the_share_chain_can_reach_exactly_three_renamed_columns`` rather
    than by re-deriving the arms.
    """
    import blueprints.jobs as jobs_bp
    from shared import metric_glossary, result_columns
    from shared.score_legends import (
        _COLUMN_ALIASES, GATE_COLUMNS, MODE_GATE_COLUMNS, get_legend,
    )

    candidates = set(jobs_bp._PLDDT_PREFERENCE)
    for columns in GATE_COLUMNS.values():
        candidates.update(columns)
    for modes in MODE_GATE_COLUMNS.values():
        for columns in modes.values():
            candidates.update(columns)
    for tool in result_columns._TOOL_PRIMARY_METRIC:
        key, _direction = result_columns.primary_metric_for(tool)
        if key:
            candidates.add(key)

    tools = (set(GATE_COLUMNS) | set(MODE_GATE_COLUMNS)
             | set(result_columns._TOOL_PRIMARY_METRIC)
             | set(result_columns._TOOL_RESULT_COLUMNS))
    reachable = set()
    for tool in sorted(tools):
        for mode in list(MODE_GATE_COLUMNS.get(tool, {})) or [None]:
            scores = {column: 7.0 for column in candidates}
            while True:
                chosen = jobs_bp._share_headline_metric(
                    tool, mode, {"scores": scores},
                )
                if chosen is None:
                    break
                reachable.add(chosen[0])
                for spelling in _COLUMN_ALIASES.get(
                    chosen[0], (chosen[0],),
                ):
                    scores.pop(spelling, None)

    lower = set()
    for tool, columns in GATE_COLUMNS.items():
        for column in columns:
            if (get_legend(tool, column) or {}).get(
                "direction",
            ) == "lower_is_better":
                lower.add(column)
    for tool, modes in MODE_GATE_COLUMNS.items():
        for _mode, columns in modes.items():
            for column in columns:
                if (get_legend(tool, column) or {}).get(
                    "direction",
                ) == "lower_is_better":
                    lower.add(column)

    assert "pI" in lower, sorted(lower)
    assert not (lower & reachable), sorted(lower & reachable)

    # What IS exposed, so a new coarse leg has to come here and say so.
    coarse = {
        column: metric_glossary._FORMAT[column] for column in reachable
        if metric_glossary._FORMAT.get(column, ".3f") != ".3f"
    }
    assert coarse == {
        "pLDDT": ".1f", "n_hotspot_contacts": ".0f", "epitope_contacts": ".0f",
    }, coarse


def test_the_share_card_quotes_the_cdr_proxy_by_name():
    """End to end on the column this fix added, against the measured pair.

    ipTM 0.844 with proxy 0.799 is the design job verify242-bs6-1789054528
    kept. The clause names the first leg of the scFv bar in reading order,
    under the glossary name rather than the storage key -- which is the same
    name the results table heads that column with, asserted over there in
    ``test_the_cdr_column_is_on_the_antibody_branch_of_the_results_page_alone``.

    The job's stored ``preset`` says minibinder on purpose: the mode comes
    off the RESULT, and a clause carrying a CDR name cannot be produced
    under the other mode.
    """
    from blueprints.jobs import _top_score_for_share

    job = SimpleNamespace(
        id="scfv-share", tool="esmfold2-design", preset="minibinder",
        status="succeeded",
        result={
            "preset": "scfv", "is_antibody": True,
            "candidates": [{
                "rank": 0, "name": "design_0", "pdb_key": "design_0.pdb",
                "sequence": SEQ,
                "scores": {"ipTM": 0.844, "CDR_iPTM_proxy": 0.799,
                           "pI": None},
            }],
        },
    )
    assert _top_score_for_share(job) == "CDR distogram proxy 0.799"


# --- the sixth wired surface: the completion email ------------------------
#
# Every surface above waits to be opened. This one is PUSHED:
# shared/jobs.py::complete_job sends it at shared/jobs.py:1349 the moment the
# run finishes. It needed no edit of its own -- shared/email.py already asks
# score_legends for the design and the verdict -- which is exactly why it
# needs a test: nothing in that file mentions this tool or this mode, so the
# coupling is invisible from either end.


def _email_scfv_cand(name, iptm, proxy, rank, filter_status):
    """A candidate in the key order run_pipeline.py stores.

    The ORDER matters to this surface and to no other:
    shared/email.py:367-370 leads with the first stored column that has a
    registered legend, and this change gave ``CDR_iPTM_proxy`` its first one. ipTM is written first
    (tools/esmfold2_design/run_pipeline.py:1181), so the headline column does
    not move -- asserted below, because the two files have no other link.
    """
    return {
        "rank": rank,
        "name": name,
        "pdb_key": f"{name}.pdb",
        "sequence": SEQ,
        "scores": {
            "ipTM": iptm,
            "CDR_iPTM_proxy": proxy,
            "final_loss": 1.0,
            "pI": None,
            "filter_status": filter_status,
        },
    }


def _email_scfv_job(candidates):
    return SimpleNamespace(
        id="scfv-mail", tool="esmfold2-design", preset="scfv",
        status="succeeded",
        result={"preset": "scfv", "is_antibody": True,
                "candidates": candidates},
    )


def test_the_completion_email_judges_an_scfv_run_against_its_own_bar():
    """The mail that reaches a customer who never opens the results page.

    The mode gaining a MODE_GATE_COLUMNS entry changes TWO things here, and
    neither is in shared/email.py. ``headline_candidate`` picks the design,
    and with nothing judgeable it falls back to the stored-first record --
    which on this run is the reject. The first assertion below is what
    catches that: with the scfv entry removed from MODE_GATE_COLUMNS it read
    0.436 rather than 0.844 (measured 2026-09-14 by deleting the entry). And
    the fifth return value is ``score_legends.verdict_text``, which was blank
    for this mode because ``judge`` answered "unjudged".

    So until 2026-09-14 this mail led with the dropped design's 0.436
    directly above a caption reading "0.75 or more is a credible designed
    interface", and said nothing to contradict it.
    """
    from shared.email import _top_candidate_summary

    keeper = _email_scfv_cand("keep", 0.844, 0.799, 1, "strict_pass")
    reject = _email_scfv_cand("drop", 0.436, 0.618, 0, "drop")

    label, value, _caption, pdb_key, judgement, _pos = _top_candidate_summary(
        job=_email_scfv_job([reject, keeper]), tone="success",
    )
    # The headline column is unchanged by the new legend, and the design is
    # the keeper rather than the stored-first reject.
    assert (label, value, pdb_key) == ("ipTM", "0.844", "keep.pdb")
    assert judgement == "Meets CDR distogram proxy 0.5 and ipTM 0.75"

    _l, value, _c, _k, judgement, _p = _top_candidate_summary(
        job=_email_scfv_job([reject]), tone="success",
    )
    assert value == "0.436"
    assert "Nothing in this run clears the bar" in judgement, judgement
    assert "ipTM 0.436, below 0.75" in judgement, judgement
def _share_job(proxy, iptm):
    return SimpleNamespace(
        id="scfv-precision", tool="esmfold2-design", preset="scfv",
        status="succeeded",
        result={
            "preset": "scfv", "is_antibody": True,
            "candidates": [{
                "rank": 0, "name": "design_0", "pdb_key": "design_0.pdb",
                "sequence": SEQ,
                "scores": {"ipTM": iptm, "CDR_iPTM_proxy": proxy,
                           "pI": None},
            }],
        },
    )


def test_a_gate_leg_renders_at_the_precision_the_share_card_prints():
    """A published clause must not show a number below the bar it claims.

    ``score_legends.shown_value`` judges a leg at the precision the GLOSSARY
    renders, while the share card prints ``.3f`` (blueprints/jobs.py:458).
    A leg declared coarser than that is therefore judged on a rounded-up
    figure and printed as the raw one. This column was ".2f" until review:
    raw 0.4951 rounded to "0.50", cleared the 0.50 bar, and published as
    "CDR distogram proxy 0.495" -- measured 2026-09-14 by setting the entry
    back to ".2f" and driving the chain below, and recorded at
    shared/metric_glossary.py:337.

    Both legs of this bar are ".3f", so the card prints the number it judged,
    and templates/components/candidate_table.html:882 gives the results cell
    the same width so the table does not contradict the verdict beside it.
    The repo's other seven gate legs are coarser and predate this change;
    this test does not assert anything about them.
    """
    from shared import metric_glossary
    from shared.score_legends import gate_columns
    from blueprints.jobs import _top_score_for_share

    for column in gate_columns("esmfold2-design", "scfv"):
        assert metric_glossary._FORMAT[column] == ".3f", column

    # Straddling the proxy bar, three decimals apart, ipTM clear of its own.
    assert _top_score_for_share(_share_job(0.4951, 0.80)) is None
    assert _top_score_for_share(_share_job(0.5010, 0.80)) == (
        "CDR distogram proxy 0.501"
    )


def test_the_results_cell_shows_the_proxy_at_the_width_the_verdict_judged(
    flask_app,
):
    """The number in the column and the number in the sentence beside it.

    ``score_legends._reading`` writes the verdict at the glossary format, so
    a results cell rendered coarser than that prints one figure while the
    verdict quotes another. A raw 0.4949 showed "0.49" in the cell under a
    verdict reading "CDR distogram proxy 0.495, below 0.5" until this column
    joined ipTM in the ``.3f`` branch at
    templates/components/candidate_table.html:882.

    THE PAGE PRINTS THE PROXY TWICE. The best-design header above the
    sequence carries its own literal formats
    (templates/tools/esmfold2_design_results.html:505), so moving the cell
    alone left the header at .2f and the page showed 0.50 above 0.495 --
    measured 2026-09-14, and the reason this test reads both. The header
    branches on the MODE: a pre-split scFv row stores the legacy
    ``iPTM_proxy`` spelling and the table still renders it in the
    CDR_iPTM_proxy column, so both spellings are driven below.
    """
    from shared.score_legends import judge, verdict_text

    for scores in (
        {"ipTM": 0.90, "CDR_iPTM_proxy": 0.4949, "final_loss": 1.0,
         "pI": None},
        {"ipTM": 0.90, "iPTM_proxy": 0.4949, "final_loss": 1.0, "pI": None},
    ):
        html = _render(flask_app, is_antibody=True, scores=scores)
        cells = re.findall(
            r'data-col="CDR_iPTM_proxy" data-val="[^"]*">([^<]*)<', html,
        )
        assert cells and set(cells) == {"0.495"}, (scores, cells)
        assert "proxy 0.495" in html, (scores, "header disagrees with cell")

    verdict = judge(
        "esmfold2-design", {"scores": {"ipTM": 0.90,
                                       "CDR_iPTM_proxy": 0.4949}}, "scfv",
    )
    assert verdict_text("esmfold2-design", verdict, "scfv") == (
        "CDR distogram proxy 0.495, below 0.5"
    )

    # The minibinder header keeps .2f, because its column keeps the table's
    # .2f else-branch (templates/components/candidate_table.html:904).
    minibinder = _render(
        flask_app, is_antibody=False,
        scores={"ipTM": 0.90, "iPTM_proxy": 0.4949, "final_loss": 1.0,
                "pI": 5.6},
    )
    assert "proxy 0.49" in minibinder and "proxy 0.495" not in minibinder
