"""The completion email for a succeeded proteina ``validate`` dry-run.

``validate`` designs nothing by construction: ``run_validate``
(tools/proteina/run_pipeline.py::run_validate) checks that the package imports,
that every variant config is present and that a checkpoint is mounted, writes
``"candidates": []`` and exits. Until 2026-09-11 ``shared.email`` classified
that payload with ``_is_empty_result``, which reads only the result SHAPE, so
a passing pre-flight was mailed out with the copy written for a paid run whose
every design was filtered away.

These tests assert on the RENDERED email -- the HTML part and the text part,
through the same ``_render_template`` calls ``send_job_complete_email`` makes
-- because the defect is what the operator reads, not what the source says.
Both renderers are covered: ``templates/email/job_complete.{html,txt}`` on the
normal path, and the inline ``_render_html`` / ``_render_text`` pair that
``send_job_complete_email`` falls back to when a template render raises.

The email-side twin of the page fix in be8e410 + a7845a1. The copy asserted
here is worded to match that fix's ``job.preset == 'validate'`` arm in
templates/tools/proteina_results.html -- which is NOT on this branch, since
both of those commits were unmerged when this was written. Nothing here reads
that template, so these tests pass either way; the consistency is editorial,
and a reader grepping this branch for that arm will not find it yet.
"""

from __future__ import annotations

import ast
import inspect
import uuid

import pytest

from shared import email as email_mod
from shared.jobs import ToolJob

pytestmark = pytest.mark.usefixtures("isolate_supabase")

JOB_URL = "https://tools.ranomics.com/jobs/x"
BASE_URL = "https://tools.ranomics.com"

# The green of a run that went fine, and the grey of one that did not.
GREEN = "#1f9d55"
GREY = "#525252"

# Every tone the classifier can return. Hardcoded on purpose: the point is to
# have a second copy that can DISAGREE with _result_tone, which is what
# test_tones_is_every_tone_result_tone_returns checks.
TONES = ("success", "preflight", "empty", "failed")


def _tones_result_tone_can_return() -> set:
    """The string literals ``_result_tone`` returns, read off its own AST."""
    tree = ast.parse(inspect.getsource(email_mod._result_tone))
    return {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }

# Every clause the old empty-tone copy put in front of a validate operator.
# Each is false for this preset; the reasons are in the commit message and in
# _result_summary's preflight branch.
FALSE_FOR_A_PREFLIGHT = (
    "no passing candidates",
    "difficult targets",
    "design budget",
    "per-design scores",
    "binder length",
    "hotspot",
    "number of designs",
)


def _job(**over) -> ToolJob:
    """A succeeded free validate run, shaped like run_validate's own payload."""
    base = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": "proteina",
        "preset": "validate",
        "status": "succeeded",
        "inputs": {},
        "result": {
            "status": "COMPLETED",
            "tier": "validate",
            "preset": "validate",
            "task_name": "",
            "designs_total": 0,
            "designs_completed": 0,
            "n_failures": 0,
            "designs": [],
            "candidates": [],
            "validate_ok": True,
            "runtime_seconds": 27,
        },
        "error": None,
        "modal_function_call_id": "fc-stub-x",
        "job_token": "t" * 64,
        "gpu_seconds_used": 27,
        "created_at": "2026-09-11T12:00:00Z",
        "started_at": "2026-09-11T12:00:01Z",
        "completed_at": "2026-09-11T12:00:28Z",
    }
    base.update(over)
    return ToolJob.from_row(base)


def _rendered(job) -> tuple[str, str]:
    """Render both parts the way ``send_job_complete_email`` does.

    Derives the tone rather than passing one in: a test that hand-picked
    "preflight" here would still pass with the classifier broken.
    """
    tone = email_mod._result_tone(job)
    ctx = email_mod._job_complete_template_context(
        job=job,
        base_url=BASE_URL,
        job_url=JOB_URL,
        tone=tone,
        tool=email_mod._tool_label(job.tool),
    )
    return (
        email_mod._render_template("job_complete.html", **ctx),
        email_mod._render_template("job_complete.txt", **ctx),
    )


class TestTone:
    def test_a_succeeded_validate_run_is_preflight_not_empty(self):
        assert email_mod._result_tone(_job()) == "preflight"

    def test_a_failed_validate_run_is_still_failed(self):
        """A pre-flight that FAILED its checks must keep the failed tone.

        run_validate's problem path writes a ``"status": "FAILED"`` result
        payload (tools/proteina/run_pipeline.py::run_validate) and only then
        exits non-zero -- the written payload is the mechanism, the
        sys.exit is a second signal. What this test pins is the single
        hop it can actually reach: _result_tone sends any job row whose
        status is not "succeeded" to the failed tone BEFORE either the
        preflight or the empty check runs. A pre-flight that found a
        missing config is the one case where "your run failed" is true.
        """
        job = _job(status="failed", result=None,
                   error={"bucket": "validate", "check": "preflight",
                          "detail": "no model checkpoint (*.ckpt) found"})
        assert email_mod._result_tone(job) == "failed"

    def test_the_preflight_check_runs_before_the_empty_check(self):
        """Both match a validate payload, so ORDER is the whole contract.

        Reordering the two checks in _result_tone reverts the defect while
        leaving every other test in this file's non-regression class green.
        """
        job = _job()
        assert email_mod._is_preflight_result(job) is True
        assert email_mod._is_empty_result(job) is True
        assert email_mod._result_tone(job) == "preflight"


class TestRenderedEmail:
    """What the operator actually reads, in both MIME parts."""

    def test_headline_does_not_report_a_failed_search(self):
        html, text = _rendered(_job())
        for part in (html, text):
            assert "pre-flight passed" in part.lower()
            assert "no candidates" not in part
            assert "finished with no candidates" not in part

    def test_body_names_the_three_checks_that_actually_ran(self):
        html, text = _rendered(_job())
        for part in (html, text):
            low = part.lower()
            assert "package imports" in low
            assert "variant config" in low
            assert "checkpoint" in low

    def test_body_says_zero_candidates_is_expected(self):
        html, text = _rendered(_job())
        for part in (html, text):
            assert "not a failed search" in part

    def test_body_names_no_lever_and_no_cause(self):
        """The empty-tone copy's every remediation clause, none of them true.

        Checked case-insensitively over the WHOLE rendered part, not over
        _result_summary's return value: the phrases have to be absent from
        what is sent, wherever a future edit might reintroduce them.
        """
        html, text = _rendered(_job())
        for part in (html, text):
            low = part.lower()
            for phrase in FALSE_FOR_A_PREFLIGHT:
                assert phrase not in low, f"{phrase!r} is false for a validate run"

    def test_body_does_not_claim_the_target_was_checked(self):
        """The adapter's preset description says "checks your target + config
        load" (tools/proteina/__init__.py:848-849). The target half is false --
        run_validate reads no target, and run_pipeline.py::_run_shard returns
        from its validate arm before the only call that fetches one. The mail
        must not inherit that claim while fixing the rest.
        """
        html, text = _rendered(_job())
        for part in (html, text):
            low = part.lower()
            assert "checks your target" not in low
            assert "your target + config" not in low
            # and it says the true thing instead
            assert "nothing in this run inspects the target" in low

    def test_no_phantom_top_design_block(self):
        """A run with no designs must not render the "Top design" callout."""
        html, text = _rendered(_job())
        assert "Top design" not in html
        assert "Top design" not in text

    def test_no_next_step_handoff(self):
        """"Validate the top design with an orthogonal predictor" needs a top
        design. There is none."""
        html, text = _rendered(_job())
        for part in (html, text):
            assert "Natural next step" not in part


class TestCallToAction:
    """Two slots, two different conditions -- see the template comment."""

    def test_button_is_the_green_of_a_run_that_went_fine(self):
        html, _ = _rendered(_job())
        assert f"background:{GREEN}" in html
        assert f"background:{GREY}" not in html

    def test_button_is_not_labelled_view_results(self):
        """Green, but there are no results to view."""
        html, text = _rendered(_job())
        for part in (html, text):
            assert "View job details" in part
            assert "View results" not in part


class TestFallbackRenderers:
    """``send_job_complete_email`` falls back to these when a template render
    raises, and each keys its headline off a ``[tone]`` dict lookup.

    A tone the dicts do not carry raises KeyError on the path taken precisely
    when something has already gone wrong -- and ``_send_completion_email``
    (shared/jobs.py::_send_completion_email) wraps the whole send in ``except
    Exception`` and logs a warning, so the operator-visible result is not a
    crash but NO
    EMAIL AT ALL, for a job that completed fine.
    """

    def test_inline_html_renders_preflight_headline_and_cta(self):
        html = email_mod._render_html(job=_job(), job_url=JOB_URL,
                                      tone="preflight")
        assert "pre-flight passed" in html.lower()
        assert "no candidates" not in html
        assert f"background:{GREEN}" in html
        assert "View job details" in html
        assert "View results" not in html

    def test_inline_text_renders_preflight_headline_and_cta(self):
        text = email_mod._render_text(job=_job(), job_url=JOB_URL,
                                      tone="preflight")
        assert "pre-flight passed" in text.lower()
        assert "no candidates" not in text
        assert "View job details" in text

    @pytest.mark.parametrize("tone", TONES)
    def test_every_tone_the_classifier_can_return_renders(self, tone):
        """The three headline dicts and _result_summary must agree on the tone
        vocabulary: a tone any of them lacks raises KeyError right here.

        This walks TONES, a hardcoded tuple, so on its own it renders four
        tones and would never notice a fifth.
        test_tones_is_every_tone_result_tone_returns is what makes that case
        fail.
        """
        job = _job()
        assert email_mod._render_html(job=job, job_url=JOB_URL, tone=tone)
        assert email_mod._render_text(job=job, job_url=JOB_URL, tone=tone)
        assert email_mod._result_summary(job, tone=tone)

    def test_tones_is_every_tone_result_tone_returns(self):
        """A tone added to _result_tone alone fails HERE, not above.

        Mutation-verified 2026-09-15: inserting a fifth ``return "fifth"``
        into _result_tone leaves all 23 other cases in this file green and
        fails only this one.
        """
        assert _tones_result_tone_can_return() == set(TONES)


class TestNoRegressionForRealEmptyRuns:
    """A paid run that genuinely filtered everything out keeps its old email.

    The point of a third tone rather than a reworded shared one: these are the
    cases the original copy was written for, and it is still right for them.
    """

    def test_a_paid_proteina_run_with_no_survivors_is_still_empty(self):
        job = _job(preset="protein_binder",
                   result={"candidates": [], "designs_total": 8})
        assert email_mod._result_tone(job) == "empty"
        summary = email_mod._result_summary(job, tone="empty")
        assert "no passing candidates" in summary

    def test_another_tool_with_no_candidates_is_still_empty(self):
        job = _job(tool="rfdiffusion", preset="pilot",
                   result={"candidates": []})
        assert email_mod._result_tone(job) == "empty"

    def test_another_tool_with_no_sequences_is_still_empty(self):
        job = _job(tool="mpnn", preset="standalone",
                   result={"sequences": []})
        assert email_mod._result_tone(job) == "empty"

    def test_a_proteina_run_with_candidates_is_still_success(self):
        job = _job(preset="protein_binder",
                   result={"candidates": [{"rank": 1, "scores": {}}]})
        assert email_mod._result_tone(job) == "success"

    def test_the_gate_is_keyed_on_tool_as_well_as_preset(self):
        """`validate` is today the only preset slug of that name across
        tools/*/__init__.py, but the copy names proteina's three specific
        checks, so a future tool adopting the slug must not inherit it.
        """
        job = _job(tool="boltzgen")
        assert email_mod._is_preflight_result(job) is False
        assert email_mod._result_tone(job) == "empty"
