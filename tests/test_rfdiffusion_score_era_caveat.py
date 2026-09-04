"""The RFdiffusion era caveat, and the gate that lets it reach a 1-chain mail.

WHY THIS FILE EXISTS. ``Legend.caveat`` already carried "an older run recorded
this differently" for BoltzGen, and ``email_caption`` gated it on the job's
target naming more than one chain — correct there, because that caveat's own
first clause is "On a multi-chain target …".

RFdiffusion's caveat has a different antecedent. llm-proteinDesigner#23
(squash-merged as e976f32) fixed the AF2 re-score, which had been folding the
natural target chain with no MSA, and the diffusion noise, which had been left
at the stock 1.0. Neither depends on how many chains the target names, and
RFdiffusion is used mostly on ONE — so the chain gate would have withheld the
note from exactly the runs that need it. Hence ``caveat_always``.

The three assertions that carry this file, in the order they would break:

  1. the caveat reaches a SINGLE-CHAIN rfdiffusion completion mail;
  2. it still does NOT reach a single-chain BoltzGen one — the gate was made
     per-caveat, not simply removed;
  3. every column in ``GATE_COLUMNS["rfdiffusion"]`` carries it, derived from
     that tuple rather than listed here, because those three are exactly what
     ``judge`` conjoins into a pass/fail verdict.
"""

import pytest

from shared.score_legends import (
    GATE_COLUMNS,
    SCORE_LEGENDS,
    _RFDIFFUSION_SCORE_ERA_CAVEAT,
    get_legend,
    legend_text,
)

from tests.test_job_complete_email_caption import _caption_of, _job

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def test_the_era_caveat_reaches_a_single_chain_rfdiffusion_email():
    """The whole point. A one-chain run is the common RFdiffusion run.

    Under the old chain-only gate this caption is the explanation alone, so
    this test fails if ``caveat_always`` stops being read.
    """
    caption = _caption_of(_job(target_chain="A", tool="rfdiffusion"))
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT in caption, (
        "a single-chain RFdiffusion completion mail carries no era caveat, so "
        "it describes a pre-#23 ipTM as if it measured the designed interface"
    )


def test_it_reaches_a_multi_chain_rfdiffusion_email_too():
    """``caveat_always`` must not accidentally become "single-chain only"."""
    caption = _caption_of(_job(target_chain="A,B", tool="rfdiffusion"))
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT in caption


def test_the_gate_became_per_caveat_and_was_not_simply_removed():
    """BoltzGen's caveat opens "On a multi-chain target" — still gated.

    This is the mutation guard on the other side: making ``email_caption``
    unconditional would pass the two tests above and re-introduce the defect
    the chain gate was added to fix (a multi-chain sentence in single-chain
    mail).
    """
    caption = _caption_of(_job(target_chain="A", tool="boltzgen"))
    boltzgen_caveat = get_legend("boltzgen", "ipTM").get("caveat")
    assert boltzgen_caveat, "the BoltzGen ipTM caveat has gone; re-point this"
    assert boltzgen_caveat not in caption, (
        "BoltzGen's multi-chain caveat now reaches a single-chain mail — the "
        "gate was widened for everyone instead of made per-caveat"
    )
    # And it still arrives when its own antecedent holds.
    assert boltzgen_caveat in _caption_of(_job(target_chain="A,B",
                                               tool="boltzgen"))


@pytest.mark.parametrize("column", sorted(GATE_COLUMNS["rfdiffusion"]))
def test_every_rfdiffusion_gate_column_carries_the_era_caveat(column):
    """Derived from GATE_COLUMNS, not from a list written out here.

    Those columns are exactly the conjunction ``judge`` evaluates. A reader
    who checks only the caveated column would otherwise still be handed a
    pass/fail verdict computed partly from uncaveated ones.
    """
    legend = get_legend("rfdiffusion", column)
    assert legend is not None, f"no legend for rfdiffusion {column}"
    assert legend.get("caveat") == _RFDIFFUSION_SCORE_ERA_CAVEAT, (
        f"rfdiffusion {column} is a gate leg with no era caveat"
    )
    assert legend.get("caveat_always") is True, (
        f"rfdiffusion {column} carries the caveat but the email will only "
        f"send it on a multi-chain job"
    )
    # The results-table surface, which is ungated and reads the same field.
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT in legend_text(legend)


def test_the_era_caveat_is_one_string_not_three_copies():
    """Three pasted copies of one claim is how the last defects here started."""
    caveats = [
        legend.get("caveat")
        for (tool, _col), legend in SCORE_LEGENDS.items()
        if tool == "rfdiffusion" and legend.get("caveat")
    ]
    assert caveats, "no rfdiffusion legend carries a caveat"
    assert all(c is _RFDIFFUSION_SCORE_ERA_CAVEAT for c in caveats), (
        "an rfdiffusion caveat is a separate string object, so the three can "
        "drift apart"
    )


def test_the_caveat_names_both_things_that_changed():
    """Content, so a future trim cannot leave a caveat that warns of nothing.

    Both halves of llm-proteinDesigner#23 are load-bearing for "not
    comparable": the re-score changed what the numbers MEAN, the noise change
    changed what the backbones ARE.
    """
    text = _RFDIFFUSION_SCORE_ERA_CAVEAT
    assert "MSA" in text, "the caveat no longer says why the scores are wrong"
    assert "noise" in text, "the caveat no longer says the designs changed too"
    assert "not comparable" in text, (
        "the caveat no longer states the consequence a reader acts on"
    )
