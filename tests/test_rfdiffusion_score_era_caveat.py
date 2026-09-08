"""The RFdiffusion era caveat: what it says, when it applies, where it shows.

WHY THIS FILE EXISTS. RFdiffusion's container scored every design against a
target AlphaFold had rebuilt without an MSA, so the stored ipTM, pLDDT and
i_pAE are not readings of the design. llm-proteinDesigner#23 (squash-merged as
e976f32) fixed it. The fix reached this repo's Modal app when that commit's
"Deploy rfdiffusion to main" job finished at 2026-09-04T16:30:46Z -- NOT when
the registry image build finished at 16:45:37Z, which serves the separate
RunPod path; see RFDIFFUSION_SCORE_ERA_BOUNDARY. tools-hub renders
whatever a job STORED, so every earlier run still shows those numbers, and #216
now DERIVES a visible pass/fail verdict from exactly the three of them.

THREE THINGS ARE PINNED HERE, and the first two replace guards that a review
proved hollow:

  1. The caveat applies on a DATE, not always. The first draft used a bool
     (`caveat_always`) and three independent reviewers called it the same
     defect the chain gate exists to prevent -- every future customer mailed a
     note about a defect their run does not have. The test that was supposed to
     cover the flag passed a multi-chain target, so the pre-existing chain gate
     satisfied it and it never exercised the flag at all.
  2. The caveat is one string, not three. The first version asserted `is`
     identity, which CPython satisfies by folding equal literals in one code
     object -- a reviewer pasted three copies and it stayed green. Equality of
     the RENDERED surfaces is what actually catches drift.
  3. It reaches a reader who never hovers anything. The caveat alone lives in
     a tooltip; the banner is the surface a person actually reads.
"""

import html as _html
import os
import re

import pytest

from shared import score_legends
from shared.score_legends import (
    GATE_COLUMNS,
    RFDIFFUSION_SCORE_ERA_BOUNDARY,
    SCORE_LEGENDS,
    _RFDIFFUSION_SCORE_ERA_CAVEAT,
    caveat_applies,
    email_caption,
    get_legend,
    legend_text,
)

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture(scope="module")
def flask_app():
    """Same shape as tests/test_multichain_iptm_notice.py's.

    Local rather than shared, because that file's is module-scoped and moving
    it to conftest would change its lifetime for every test that uses it.
    """
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app

# Comfortably either side of the container boundary, so neither depends on
# clock skew or on how the boundary string is spelled.
BEFORE = "2026-07-11T09:00:00Z"
AFTER = "2026-09-05T09:00:00Z"


def _rfd_legend(column="ipTM"):
    legend = get_legend("rfdiffusion", column)
    assert legend is not None, f"no rfdiffusion {column} legend"
    return legend


# ---------------------------------------------------------------------------
# 1. The gate is the date the caveat names
# ---------------------------------------------------------------------------

def test_a_run_from_before_the_container_fix_gets_the_caveat():
    """The load-bearing case, and a SINGLE-chain one.

    Single-chain is the ordinary RFdiffusion run, and the chain gate that
    serves BoltzGen would withhold the caveat from all of them.
    """
    caption = email_caption(_rfd_legend(), "A", BEFORE)
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT in caption


def test_a_run_from_after_the_container_fix_does_not():
    """The half the first draft got wrong, on every tool's most common run.

    A post-boundary run is on the fixed image, so the caveat's own first clause
    is false for it. Mailing it anyway is the defect the chain gate exists to
    prevent, moved to a different axis.
    """
    caption = email_caption(_rfd_legend(), "A", AFTER)
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT not in caption
    assert caption == _rfd_legend()["explanation"]


def test_a_post_boundary_multi_chain_run_does_not_get_it_either():
    """The chain count must not smuggle the era caveat back in.

    If `caveat_applies` fell through to the chain gate for a dated caveat, a
    multi-chain post-fix run would carry a note about a defect it does not
    have -- and every other test here would still pass.
    """
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT not in email_caption(
        _rfd_legend(), "A,B", AFTER,
    )


@pytest.mark.parametrize("unknown", [None, "", "not-a-date", "2026-13-45"])
def test_an_unknown_date_over_warns(unknown):
    """Unknown means "cannot tell which container ran", and the two errors
    are not equal: a sentence too many costs a sentence, a sentence too few
    lets a non-measurement render as a measurement."""
    assert caveat_applies(_rfd_legend(), "A", unknown) is True


def test_the_boundary_is_the_modal_deploy_not_the_registry_build():
    """Pin the literal, because nothing else can.

    The suite passing is not evidence for this value -- every gate test here
    works off BEFORE/AFTER dates chosen relative to whatever it is set to, so
    a wrong instant is invisible to all of them. This assert exists so that
    changing it is a deliberate act with the provenance in front of you.

    Provenance: llm-proteinDesigner Actions run 33895498649 ("Deploy Modal
    apps", commit e976f32), job "Deploy rfdiffusion to main", completed_at
    2026-09-04T16:30:46Z. NOT run 33895498838 ("Build and Push RFdiffusion
    Docker", 16:45:37Z) -- that image is pulled by RunPod, and tools-hub calls
    the Modal app.
    """
    assert RFDIFFUSION_SCORE_ERA_BOUNDARY == "2026-09-04T16:30:46Z", (
        "the era boundary changed. It is the completion of the 'Deploy "
        "rfdiffusion to main' job for llm-proteinDesigner e976f32, because "
        "that app copies run_pipeline.py in at deploy time. If you are "
        "setting it to 16:45:37Z you are reading the registry image build, "
        "which serves RunPod and not this repo -- that mistake has been made "
        "and corrected once already."
    )


def test_the_boundary_itself_is_exclusive():
    """A run at the exact instant the image finished building used it."""
    assert caveat_applies(_rfd_legend(), "A",
                          RFDIFFUSION_SCORE_ERA_BOUNDARY) is False


@pytest.mark.parametrize("shape", [
    "2026-07-11T09:00:00Z",      # the Supabase shape
    "2026-07-11T09:00:00+00:00",  # the same instant, spelled out
    "2026-07-11T09:00:00",        # naive; read as UTC
    "2026-07-11T11:00:00+02:00",  # an offset that still lands before
])
def test_the_date_is_read_in_every_shape_the_app_stores(shape):
    assert caveat_applies(_rfd_legend(), "A", shape) is True


# ---------------------------------------------------------------------------
# 2. The gate stayed per-caveat: BoltzGen is untouched
# ---------------------------------------------------------------------------

def test_boltzgens_caveat_is_still_gated_on_chains_and_not_on_dates():
    """The mutation guard on the other side.

    Making the gate unconditional, or making it purely temporal, would each
    pass the RFdiffusion tests above and re-introduce the defect the chain
    gate was added to fix: BoltzGen's multi-chain sentence in single-chain
    mail.
    """
    legend = get_legend("boltzgen", "ipTM")
    caveat = legend.get("caveat")
    assert caveat, "the BoltzGen ipTM caveat has gone; re-point this test"
    assert "caveat_before" not in legend, (
        "BoltzGen's caveat is about chains, not about an era; giving it a date "
        "would silently change which BoltzGen runs are warned"
    )
    # Chain count decides it, and the date does not enter.
    assert caveat not in email_caption(legend, "A", BEFORE)
    assert caveat not in email_caption(legend, "A", AFTER)
    assert caveat in email_caption(legend, "A,B", BEFORE)
    assert caveat in email_caption(legend, "A,B", AFTER)


# ---------------------------------------------------------------------------
# 3. Every column the verdict is built from carries it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("column", sorted(GATE_COLUMNS["rfdiffusion"]))
def test_every_rfdiffusion_gate_column_carries_the_era_caveat(column):
    """Derived from GATE_COLUMNS, not from a list written out here.

    Those columns are exactly the conjunction `judge` evaluates into the
    verdict #216 renders. A reader who checks only the caveated column would
    otherwise still be handed a verdict computed partly from uncaveated ones.
    """
    legend = get_legend("rfdiffusion", column)
    assert legend is not None, f"no legend for rfdiffusion {column}"
    assert legend.get("caveat") == _RFDIFFUSION_SCORE_ERA_CAVEAT
    assert legend.get("caveat_before") == RFDIFFUSION_SCORE_ERA_BOUNDARY, (
        f"rfdiffusion {column} carries the caveat but no era boundary, so the "
        f"email cannot tell which runs it is about"
    )
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT in legend_text(legend)


def test_the_gate_columns_are_not_silently_empty():
    """An empty parametrize skips instead of failing, so pin the premise."""
    assert len(GATE_COLUMNS["rfdiffusion"]) == 3


def test_no_rfdiffusion_legend_carries_a_caveat_without_a_boundary():
    """`caveat_before` is what stops the note going to everyone forever."""
    for (tool, col), legend in SCORE_LEGENDS.items():
        if tool != "rfdiffusion" or not legend.get("caveat"):
            continue
        assert legend.get("caveat_before"), (
            f"rfdiffusion {col} has a caveat with no boundary"
        )


def test_the_caveat_is_one_string_across_every_column_that_carries_it():
    """Equality, not `is`.

    The identity form of this test was hollow: CPython folds equal string
    literals within a code object, so three pasted copies of the same text
    still satisfy `is`. A reviewer proved it by pasting them. What actually
    catches drift is that the columns agree.
    """
    texts = {
        col: legend["caveat"]
        for (tool, col), legend in SCORE_LEGENDS.items()
        if tool == "rfdiffusion" and legend.get("caveat")
    }
    assert len(texts) == 3, texts
    assert len(set(texts.values())) == 1, (
        f"rfdiffusion columns disagree about the era caveat: {texts!r}"
    )


# ---------------------------------------------------------------------------
# 4. What it says
# ---------------------------------------------------------------------------

def test_the_caveat_names_the_cause_the_scale_change_and_the_ordering():
    """Content, so a future trim cannot leave a caveat that warns of nothing.

    Each clause is here because leaving it out was a real defect:
      * the MSA is the cause, and without it the note asserts without evidence;
      * pLDDT CHANGED SCALE at the same commit -- pre-fix it was the whole
        complex mean, which the legend's own explanation contradicts;
      * ordering is derived from ipTM and truncated at a limit, so disclaiming
        the values while the order silently persists is a half-measure this
        repo has shipped twice.
    """
    text = _RFDIFFUSION_SCORE_ERA_CAVEAT
    assert "MSA" in text
    assert "whole-complex mean" in text
    assert "ordered on ipTM" in text or "ordering" in text


def test_the_caveat_does_not_read_as_its_own_opposite():
    """A draft ended "nor is any pass judgement computed from them", which
    parses naturally as "no verdict is computed" -- the reassurance opposite to
    the warning, on a page where #216 computes exactly that verdict."""
    assert "nor is any pass judgement computed" not in _RFDIFFUSION_SCORE_ERA_CAVEAT


# ---------------------------------------------------------------------------
# 5. It reaches somebody who never hovers
# ---------------------------------------------------------------------------

_NOTICE_TPL = (
    '{% from "components/score_era_notice.html" import score_era_notice %}'
    "{{ score_era_notice(tool, created) }}"
)


def _notice(flask_app, tool, created):
    """The rendered banner, with HTML entities decoded.

    Decoded because the caveat contains apostrophes and Jinja escapes them to
    &#39; -- correct output, but it means a raw substring check against the
    source string silently fails on markup that is fine. Compare what the
    browser shows.
    """
    with flask_app.app_context():
        return _html.unescape(
            flask_app.jinja_env.from_string(_NOTICE_TPL).render(
                tool=tool, created=created,
            )
        )


def test_the_banner_renders_for_a_pre_fix_run(flask_app):
    """The whole point of the banner: BODY text, not a tooltip.

    results_shell tells the reader in plain prose that "the pipeline ran
    cleanly" and #216 renders "pLDDT 64.2, below 80" as flat text. A claim
    that contradicts those has to appear in the same register.
    """
    html = _notice(flask_app, "rfdiffusion", BEFORE)
    assert "data-score-era-notice" in html
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT in html
    # Visible text, not an attribute value.
    visible = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    assert "predate the fix" in visible


def test_the_banner_is_absent_for_a_post_fix_run(flask_app):
    assert "data-score-era-notice" not in _notice(flask_app, "rfdiffusion", AFTER)


def test_the_banner_never_fires_for_a_tool_with_no_era_caveat(flask_app):
    assert "data-score-era-notice" not in _notice(flask_app, "boltzgen", BEFORE)
    assert "data-score-era-notice" not in _notice(flask_app, "mpnn", BEFORE)


def test_the_banner_says_the_same_thing_as_the_tooltip(flask_app):
    """One string, two surfaces. Two wordings is how this area has drifted."""
    html = _notice(flask_app, "rfdiffusion", BEFORE)
    assert score_legends.score_era_caveat("rfdiffusion", BEFORE) in html
    assert score_legends.score_era_caveat("rfdiffusion", BEFORE) == (
        _RFDIFFUSION_SCORE_ERA_CAVEAT
    )


def test_the_job_page_calls_the_banner_with_the_jobs_own_date(flask_app):
    """Wiring, not just the macro. A macro nobody calls warns nobody.

    COMMENTS ARE STRIPPED FIRST, and the version of this test that did not
    strip them was hollow -- proven by mutation. Replacing the call with
    ``{# score_era_notice removed #}`` left the substring "score_era_notice" in
    the file and the test stayed green, and the check for the date argument was
    satisfied by the prose of the explanatory comment sitting directly above
    the call, which names ``job.created_at``. A guard that its own neighbouring
    comment can satisfy is not a guard.
    """
    src = (flask_app.jinja_env.get_or_select_template(
        "tools/rfdiffusion_results.html").filename)
    body = re.sub(r"\{#.*?#\}", "", open(src, encoding="utf-8").read(),
                  flags=re.S)

    call = re.search(r"\{\{\s*score_era_notice\((?P<args>[^)]*)\)", body)
    assert call, (
        "the RFdiffusion results partial no longer CALLS the era banner "
        "(an import or a mention in a comment is not a call)"
    )
    assert "job.created_at" in call.group("args"), (
        f"the banner is called as score_era_notice({call.group('args')}), "
        f"without the job's own date -- so it cannot gate on the era and will "
        f"warn on every run forever, which is the defect this replaced"
    )


# ---------------------------------------------------------------------------
# 6. The rendered table, not just the pure functions
# ---------------------------------------------------------------------------
#
# The first version of this file tested `email_caption` and `legend_text` in
# isolation and every one of its assertions passed while the caveat was, on the
# page a customer actually opens, a hover tooltip on three metric columns and
# absent from the one cell that states a conclusion. Pure-function tests could
# not see that. These render.

def _rendered_table():
    """One pre-fix RFdiffusion job's results table, as the job page draws it."""
    from shared import ranking, result_columns  # noqa: PLC0415
    from tests.test_target_table_render import _render, _row  # noqa: PLC0415

    rows = [_row("rfdiffusion", "ipTM", 0.06 + 0.001 * i, job="j-rd", index=i)
            for i in range(25)]
    ranked = ranking.rank_candidates(rows, limit=None)
    return _html.unescape(_render(
        candidates=ranked["rows"],
        columns=list(result_columns.columns_for("rfdiffusion")),
        job_id="j-rd", tool_slug="rfdiffusion", multi_tool=False,
    ))


def test_the_derived_verdict_carries_the_caveat_it_is_derived_from():
    """#216 computes this verdict from the three caveated columns.

    Leaving it bare made the single most confident string on the page the one
    with no caveat on it -- "pLDDT 64.2, below 80" reads as a measurement that
    fell short, when the measurement is void. Two reviewers found this
    independently.
    """
    cells = re.findall(r'<td data-col="against_bar".*?</td>',
                       _rendered_table(), re.S)
    assert cells, (
        "no verdict cell rendered, so this test is measuring nothing; "
        "against_bar is RFdiffusion's fourth column in shared/result_columns"
    )
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT in cells[0], (
        "the derived verdict states a conclusion with no era caveat attached"
    )


def test_the_metric_columns_still_carry_it_too():
    """The tooltip half, on the surface rather than through legend_text."""
    assert _RFDIFFUSION_SCORE_ERA_CAVEAT in _rendered_table()
