"""BoltzGen's ipTM legend may not carry a bar its own metric cannot reach.

BoltzGen scores with Boltz-2 weights, so the boltz2 legend sitting directly
below it in shared/score_legends.py looks like the obvious thing to copy —
and that is what happened: ``good`` 0.7 / ``excellent`` 0.8, plus an
explanation asserting "Above 0.7 is a credible binder".

Those are CO-FOLD bars, and BoltzGen never cofolds. Its one refold folds the
binder on its own, so it has no interface to score (all five of its
``designfolding-*_iptm`` columns read 0.0 and its ``min_interaction_pae`` is
the 100000.25 "no interaction" sentinel). What reaches this legend is instead
the generator's own confidence head reading its own output:

    460 self-hosted designs, 2 unrelated targets, 3 modalities  ->  max 0.650
    the same designs re-scored on a real Boltz-2 cofold        ->  0.363-0.852

So the bar was not demanding, it was unreachable — every finished run read as
a failure to the user who paid for it, and 6 production runs / 65 candidates
came back uniformly "below threshold". The container-side half of this is
llm-proteinDesigner fix/boltzgen-unreachable-gate.

Pinned here because ``good``/``excellent`` are inert today (only
``explanation`` and ``caveat`` render), which is exactly what lets a wrong
value sit unnoticed until someone wires the field up to a colour.
"""
from __future__ import annotations

import re

from shared.score_legends import SCORE_LEGENDS, legend_text

BOLTZGEN_IPTM = ("boltzgen", "ipTM")
BOLTZ2_IPTM = ("boltz2", "ipTM")


def test_boltzgen_iptm_asserts_no_bar():
    legend = SCORE_LEGENDS[BOLTZGEN_IPTM]
    assert "good" not in legend and "excellent" not in legend, (
        f"boltzgen ipTM claims good={legend.get('good')} / "
        f"excellent={legend.get('excellent')}. Nothing pairs the in-run number "
        f"against a cofold on the same designs, so there is no bar to state. "
        f"Omit it rather than invent one."
    )


def test_boltzgen_iptm_does_not_inherit_the_boltz2_cofold_scale():
    """The specific copy that was made. boltz2 keeps its bars — it IS the
    calibrated cofold — so this compares against the live values rather than
    against a hardcoded 0.7, and keeps holding if boltz2 is ever recalibrated.
    """
    cofold = SCORE_LEGENDS[BOLTZ2_IPTM]
    assert cofold.get("good") and cofold.get("excellent"), (
        "boltz2 ipTM lost its bars, so this test compares against nothing; "
        "re-point it rather than leave it passing"
    )
    text = legend_text(SCORE_LEGENDS[BOLTZGEN_IPTM])
    for value in (cofold["good"], cofold["excellent"]):
        assert not re.search(
            rf"above\s+{re.escape(str(value))}", text, re.I
        ), (
            f"boltzgen ipTM text promises a value above {value}, the Boltz-2 "
            f"cofold bar, for a number that has never reached 0.65"
        )


def test_boltzgen_iptm_says_the_cofold_scale_does_not_apply():
    """Removing the false claim is not the same as telling the truth. A user
    reading 0.42 needs to know it is not the 0.7-scale number they know from
    every other tool here, or they will apply that scale themselves."""
    text = legend_text(SCORE_LEGENDS[BOLTZGEN_IPTM]).lower()
    assert "cofold" in text, text
    assert "0.7" in text, text


def test_the_legends_measured_on_the_refold_keep_their_bars():
    """The reason ipTM alone drops its bar is that ipTM alone has no reading
    of its own kind. pLDDT and refolding RMSD are measured on the design-only
    refold, so each is about the binder and nothing else — which is what 80
    and 1.5 A were calibrated on. Dropping their bars too would be
    over-correcting, and this pins that it did not happen."""
    for column in ("pLDDT", "refolding_rmsd"):
        legend = SCORE_LEGENDS[("boltzgen", column)]
        assert legend.get("good") is not None, column
        assert legend.get("excellent") is not None, column
