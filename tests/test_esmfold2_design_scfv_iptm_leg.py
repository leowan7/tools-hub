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


def _render_one(flask_app, proxy, iptm) -> str:
    """One scFv candidate, rendered.

    No stored ``filter_status``: the panel derives its own word, which is the
    copy under test.
    """
    result = {
        "status": "COMPLETED",
        "preset": "scfv",
        "is_antibody": True,
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
                "scores": {
                    "ipTM": iptm,
                    "iPTM_proxy": proxy,
                    "final_loss": 1.0,
                    "pI": None,
                },
            }
        ],
    }
    job = SimpleNamespace(id="scfv-leg", status="succeeded", result=result)
    with flask_app.test_request_context():
        return flask_app.jinja_env.get_template(
            "tools/esmfold2_design_results.html"
        ).render(job=job, send_target_tools=None)


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
