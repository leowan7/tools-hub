"""A zero-candidate proteina job page must not describe a search that never ran.

WHAT WAS SHIPPING. ``templates/tools/proteina_results.html`` passed one
unconditional empty-state paragraph to ``results_panel``:

    This Proteina-Complexa shard returned no candidates that cleared the
    reward filters. Survivors are pooled across the whole campaign, so other
    shards may still have produced hits - check the campaign page.

Both sentences are false on a SUCCEEDED free ``validate`` run, observed live
2026-09-11 on main @ ca36d70 (job fc9d14d7, status succeeded, 27 GPU-seconds).

1. ``validate`` designs nothing. ``run_pipeline.run_validate`` checks three
   things - the ``proteinfoundation`` package imports, every variant config
   file is present, and at least one ``*.ckpt`` is on the weights Volume -
   then writes ``"candidates": []`` and exits (run_pipeline.py:3886-3936).
   There were never candidates for a reward filter to cull, so "returned no
   candidates that cleared the reward filters" reports a failed search to an
   operator whose run passed.

2. A validate job cannot be in a campaign. The only writer of
   ``tool_jobs.campaign_id`` is ``shared/compute_campaigns.py:2020-2033``,
   which takes its preset from a campaign, and both routes that create a
   campaign refuse this preset first (``blueprints/campaigns.py:337``,
   ``blueprints/targets.py:243``). Nothing UPDATEs ``campaign_id`` later. So
   there is no campaign page to check and no sibling shard to pool with.

3. The campaign sentence is also wrong for a STANDALONE paid run. proteina is
   deliberately not campaign-only (``"proteina" not in CAMPAIGN_ONLY_TOOLS``,
   pinned by tests/test_proteina_smoke.py::TestAdapterRegistration::
   test_not_campaign_only), so a single ``protein_binder`` job that genuinely
   filters everything out is the ordinary case, and it has no siblings either.

The campaign wording is correct, and stays, for the one case it was written
for: a paid shard that is actually part of a campaign.

WHY THESE TESTS RENDER THE PAGE. The defect is what the operator reads, and
this partial is not rendered alone: ``templates/job_detail.html:283`` includes
it under ``status == 'succeeded' and job.result``, and
``templates/components/worked_example.html:88`` includes it again on the
public tool page. A source-string assertion would pass on a template whose
branch never fires. Each case here drives the real Flask route and reads the
resulting paragraphs, so a branch present in the file but unreachable on the
page fails.

WHY DISTINCTNESS IS MEASURED ON THE PANEL, NOT THE PAGE. An earlier version of
``test_the_three_cases_render_three_different_messages`` joined every ``<p>``
on the page. That made it certify a guard it did not have: with the validate
arm disabled, validate and standalone rendered word-for-word identical
empty-state copy and the test still passed, because the hero prints
``Preset validate`` vs ``Preset protein_binder`` (job_detail.html:117) and
that unrelated line differed for free. ``_panel_paragraphs`` reads only
``<p>`` inside a ``div.panel-body``, which excludes the hero, so the
comparison is over the copy actually under test.

WHAT THESE TESTS DO NOT DO. They pin which SENTENCE each case gets, not that
the sentence is true. The truth of the validate copy rests on the three
checks cited above; if ``run_validate`` gains or loses one, these stay green
and the page goes stale.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_JID = "fc9d14d7-ada8-40f3-995c-56a67e4087a2"
_CID = "c0ffee00-0000-4000-8000-000000000001"

_PAID_PRESETS = ["protein_binder", "ligand_binder", "motif_ame"]

# The sentence that may only appear for a paid shard inside a campaign.
_CAMPAIGN_SENTENCE = "pooled across the whole campaign"
_CAMPAIGN_POINTER = "check the campaign page"
# The claim that is false whenever nothing was designed.
_FILTER_CLAIM = "cleared the reward filters"

# Parameter advice the standalone arm may not give. Each is refused or
# ignored on at least one run shape that reaches that arm: a curated run
# rejects hotspots and contigs (tools/proteina/__init__.py:666) and
# ligand_binder / motif_ame can never be custom (:151, :607); binder_length is
# consumed only under target_source == "custom" (run_pipeline.py:4245, inside
# the :4234 gate); and a shard is pinned at 8 designs (__init__.py:162), so
# raising the count makes it a campaign instead.
_PARAMETER_ADVICE = ("binder length", "hotspot", "more designs")


class _PanelParagraphs(HTMLParser):
    """Visible text of each ``<p>`` inside a ``div.panel-body``.

    Scoped to the panel on purpose: the hero's ``Preset <x> · submitted ...``
    line varies with the preset under test, so a page-wide collection makes
    any two cases look distinct no matter what the copy says.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._div_depth = 0
        self._panel_at: int | None = None
        self._p_depth = 0
        self._buf: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            self._div_depth += 1
            if self._panel_at is None:
                classes = dict(attrs).get("class") or ""
                if "panel-body" in classes.split():
                    self._panel_at = self._div_depth
        elif tag == "p" and self._panel_at is not None:
            self._p_depth += 1

    def handle_endtag(self, tag):
        if tag == "p" and self._p_depth:
            self._p_depth -= 1
            if not self._p_depth:
                text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                if text:
                    self.paragraphs.append(text)
                self._buf = []
        elif tag == "div":
            if self._panel_at is not None and self._div_depth == self._panel_at:
                self._panel_at = None
            self._div_depth -= 1

    def handle_data(self, data):
        if self._p_depth:
            self._buf.append(data)


@pytest.fixture
def app(monkeypatch):
    # FLAG_TOOL_PROTEINA does NOT gate this partial, despite the obvious
    # reading: tools/base.get() is a bare registry lookup (tools/base.py:188)
    # with no flag check, and the page renders identically without it -- the
    # flag gates campaign create/estimate (FLAG_GATED_CAMPAIGN_TOOLS) and the
    # send_target_tools loop (blueprints/jobs.py:303). It is set so the
    # fixture matches how the tool is configured in production, not because
    # anything here needs it.
    monkeypatch.setenv("FLAG_TOOL_PROTEINA", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


def _result(preset: str) -> dict:
    """The zero-candidate payload the container actually writes.

    Two writers, two shapes, and the difference matters to the tests below:
    ``run_validate`` (run_pipeline.py:3920-3935) reports ``designs_total: 0``
    plus ``validate_ok``, because it designed nothing; the paid no-survivors
    branch (:4342-4353) reports the real generated count and an
    ``output_census``, because it designed and then filtered everything out.

    Neither emits ``gpu_seconds`` -- both write ``runtime_seconds``. An
    earlier fixture put ``gpu_seconds`` in here, which no writer produces and
    which made the panel render a second "GPU time: 27 seconds." line under
    the header's genuine "Completed in 27 GPU-seconds".
    """
    common = {
        "status": "COMPLETED",
        "tier": preset,
        "designs_completed": 0,
        "n_failures": 0,
        "designs": [],
        "candidates": [],
        "runtime_seconds": 27,
        "provider_job_id": "fc-1",
    }
    if preset == "validate":
        return {**common, "designs_total": 0, "preset": preset,
                "task_name": "02_PDL1", "validate_ok": True}
    return {**common, "designs_total": 8, "output_census": {}}


def _job(preset: str, campaign_id: str | None):
    """A SUCCEEDED proteina job. Only ``preset`` and ``campaign_id`` vary
    between cases, which keeps the branch the variable under test.

    ``gpu_seconds_used`` is a tool_jobs column, not a result key: it is what
    job_detail.html:245 prints as "Completed in 27 GPU-seconds", the line the
    live bug report quoted.
    """
    return SimpleNamespace(
        id=_JID,
        tool="proteina",
        preset=preset,
        status="succeeded",
        created_at="2026-09-11T00:00:00+00:00",
        inputs={},
        result=_result(preset),
        error=None,
        gpu_seconds_used=27,
        campaign_id=campaign_id,
    )


def _render(app, preset: str, campaign_id: str | None = None) -> list[str]:
    """GET /jobs/<id> for one job shape; return its panel paragraphs.

    ``get_job`` is patched where ``blueprints.jobs`` bound it (module-level,
    blueprints/jobs.py:41). The succeeded branch additionally resolves user
    metadata through a function-local ``from shared.jobs import ...`` at :318,
    so that one must be patched on the source module -- patching
    ``blueprints.jobs.resolve_user_email_and_meta`` would raise AttributeError.
    """
    client = app.test_client()
    ctx = SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com")
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.jobs.load_user_context", return_value=ctx), \
            patch("blueprints.jobs.get_job",
                  return_value=_job(preset, campaign_id)), \
            patch("shared.jobs.resolve_user_email_and_meta",
                  return_value=("u@example.com", {})):
        resp = client.get(f"/jobs/{_JID}")
    assert resp.status_code == 200, resp.status_code
    raw = resp.get_data(as_text=True)
    # Liveness. "Candidates (0)" comes only from
    # templates/components/results_shell.html:33, so this fails if the route
    # fell through to tools/_default_results.html (a raw JSON <pre>), and it
    # confirms the ZERO-candidate branch specifically. It does not by itself
    # prove the caller block rendered rather than the macro's own fallback --
    # the copy assertions below are what pin that.
    assert "Candidates (0)" in raw, (
        "the results panel did not render; nothing below asserts anything")
    parser = _PanelParagraphs()
    parser.feed(raw)
    assert parser.paragraphs, "no panel paragraphs on the page"
    return parser.paragraphs


def _text(app, preset: str, campaign_id: str | None = None) -> str:
    return " ".join(_render(app, preset, campaign_id))


# ---------------------------------------------------------------------------
# 1. The free validate dry-run
# ---------------------------------------------------------------------------


def test_a_succeeded_validate_run_is_not_reported_as_a_failed_search(app):
    """The headline defect: a green run told the operator it found nothing."""
    body = _text(app, "validate")
    assert _FILTER_CLAIM not in body, (
        "a validate run designs nothing, so it cannot have candidates that "
        "failed to clear a reward filter:\n  " + body
    )
    assert "Pre-flight passed" in body, (
        "a succeeded validate run says nothing about having passed:\n  " + body
    )


def test_a_validate_run_does_not_point_at_a_campaign(app):
    """Both campaign-creating routes refuse this preset, so the page a
    validate run was being sent to cannot exist."""
    body = _text(app, "validate")
    for claim in (_CAMPAIGN_SENTENCE, _CAMPAIGN_POINTER):
        assert claim not in body, (
            f"validate run still renders {claim!r}, but a validate job can "
            f"never carry a campaign_id:\n  " + body
        )


def test_the_validate_copy_does_not_claim_the_target(app):
    """``run_validate`` never inspects the target.

    The adapter's own preset description says the tier "checks your target +
    config load" (tools/proteina/__init__.py:848-849), and the target half of
    that is not what the container does: the validate branch returns at
    run_pipeline.py:4030, and the sole route to ``download_target`` (:2864) is
    the ``prepare_custom_target`` call at :4239. Repeating the adapter's
    phrasing would have replaced one false sentence with another.

    The copy says "nothing here inspects the target itself" rather than
    "before any target is staged": the HUB does upload an attached file even
    for a validate run (__init__.py:607 exempts validate from the
    custom-target gate), so a staging claim could be read as "my upload never
    left my machine", which is false.
    """
    body = _text(app, "validate")
    assert "not your target" in body, (
        "the validate copy does not tell the operator the target was not "
        "checked, which is the one overclaim available here:\n  " + body
    )


def test_a_validate_run_beats_a_campaign_id_if_one_ever_appears(app):
    """Pins branch PRECEDENCE, which the arms alone do not.

    Reordering the chain so ``campaign_id`` is tested first passed every
    other test in this file, because no case combines the two. The state is
    unreachable today (see the module docstring), but the ordering is a
    deliberate choice: "this run designed nothing" outranks "your other
    shards may have hits", since the latter is advice about a search this
    run never performed.
    """
    body = _text(app, "validate", campaign_id=_CID)
    assert "Pre-flight passed" in body, (
        "a validate job carrying a campaign_id took the campaign arm:\n  "
        + body
    )
    assert _CAMPAIGN_SENTENCE not in body, body


# ---------------------------------------------------------------------------
# 2. A paid shard that IS part of a campaign - the wording was written for it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", _PAID_PRESETS)
def test_a_paid_campaign_shard_keeps_the_pooling_message(app, preset):
    """This is the case the original sentence was correct for. A fix that
    simply deleted it would lose a true and useful pointer."""
    body = _text(app, preset, campaign_id=_CID)
    assert _FILTER_CLAIM in body, body
    assert _CAMPAIGN_SENTENCE in body, (
        f"a {preset} shard inside a campaign lost the pooling message:\n  "
        + body
    )
    assert _CAMPAIGN_POINTER in body, body


# ---------------------------------------------------------------------------
# 3. A paid run launched OUTSIDE a campaign
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", _PAID_PRESETS)
def test_a_standalone_paid_run_is_not_told_to_check_a_campaign_page(
        app, preset):
    """proteina is not campaign-only, so this job shape is ordinary rather
    than hypothetical. It has no campaign and therefore no sibling shards.

    Parametrized across all three paid presets because this is the arm whose
    copy used to name tool parameters, and the presets differ in which
    parameters they accept at all.
    """
    body = _text(app, preset, campaign_id=None)
    for claim in (_CAMPAIGN_SENTENCE, _CAMPAIGN_POINTER):
        assert claim not in body, (
            f"a standalone {preset} run still renders {claim!r}:\n  " + body)


def test_a_standalone_paid_run_still_reports_the_filtering(app):
    """Dropping the campaign sentence must not drop the actual news: unlike
    validate, this run DID design (designs_total 8) and everything was
    culled."""
    body = _text(app, "protein_binder", campaign_id=None)
    assert _FILTER_CLAIM in body, (
        "a standalone paid run no longer says its designs were filtered "
        "out:\n  " + body
    )


@pytest.mark.parametrize("preset", _PAID_PRESETS)
def test_the_standalone_arm_gives_no_parameter_advice(app, preset):
    """The regression this arm already shipped once.

    Its first version closed with "Widening the binder length range, refining
    your hotspots, or funding more designs are the levers that move it", and
    every clause is false for at least one run shape that lands here -- see
    ``_PARAMETER_ADVICE``. Advice naming a knob has to be gated on a run
    shape that actually exposes that knob, and this arm knows neither the
    target source nor the preset's custom-target capability.
    """
    body = _text(app, preset, campaign_id=None).lower()
    offenders = [a for a in _PARAMETER_ADVICE if a in body]
    assert not offenders, (
        f"the standalone {preset} arm names {offenders} as a lever, but it "
        f"is refused or ignored on at least one run that reaches here:\n  "
        + body
    )


# ---------------------------------------------------------------------------
# 4. The three arms are actually distinct
# ---------------------------------------------------------------------------


def test_the_three_cases_render_three_different_messages(app):
    """Guards against a refactor that collapses two arms into one and leaves
    every assertion above still satisfied by a single generic sentence.

    Measured over panel paragraphs only -- see the module docstring for why a
    page-wide comparison made this pass while two arms were identical.
    """
    seen = {
        "validate": _text(app, "validate"),
        "campaign": _text(app, "protein_binder", campaign_id=_CID),
        "standalone": _text(app, "protein_binder", campaign_id=None),
    }
    assert len(set(seen.values())) == 3, (
        "two of the three zero-candidate cases render identical copy: "
        + repr({k: v[:120] for k, v in seen.items()})
    )
