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

2. A validate job cannot be in a campaign. ``blueprints/campaigns.py`` refuses
   the preset at both entry points (:196 and :337, "The validate tier is a
   free pre-flight, not a campaign"), so there is no campaign page to check
   and no sibling shard to pool with.

3. The campaign sentence is also wrong for a STANDALONE paid run. proteina is
   deliberately not campaign-only (``"proteina" not in CAMPAIGN_ONLY_TOOLS``,
   pinned by tests/test_proteina_smoke.py::test_not_campaign_only), so a
   single ``protein_binder`` job that genuinely filters everything out is the
   ordinary case, and it has no siblings either.

The campaign wording is correct, and stays, for the one case it was written
for: a paid shard that is actually part of a campaign.

WHY THESE TESTS RENDER THE PAGE. The defect is what the operator reads, and
this partial is never rendered alone - ``job_detail.html`` includes it, and
only under ``status == 'succeeded' and job.result``. A source-string
assertion would pass on a template whose branch never fires. Each case here
drives the real Flask route and reads the resulting paragraphs, so a branch
that is present in the file but unreachable on the page fails.

WHAT THESE TESTS DO NOT DO. They pin which SENTENCE each case gets, not that
the sentence is true. The truth of the validate copy rests on the three
checks cited above; if ``run_validate`` gains or loses one, these stay green
and the page goes stale. ``test_the_validate_copy_does_not_claim_the_target``
is the one guard against the specific overclaim that was available to be
made here, and it is a wording check like the rest.
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

# The sentence that may only appear for a paid shard inside a campaign.
_CAMPAIGN_SENTENCE = "pooled across the whole campaign"
_CAMPAIGN_POINTER = "check the campaign page"
# The claim that is false whenever nothing was designed.
_FILTER_CLAIM = "cleared the reward filters"


class _Paragraphs(HTMLParser):
    """Visible text of each <p>, whitespace collapsed."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._buf: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self._depth += 1

    def handle_endtag(self, tag):
        if tag == "p" and self._depth:
            self._depth -= 1
            if not self._depth:
                text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                if text:
                    self.paragraphs.append(text)
                self._buf = []

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


@pytest.fixture
def app(monkeypatch):
    # proteina is flag-gated (compute_campaigns.FLAG_GATED_CAMPAIGN_TOOLS);
    # without the flag the adapter lookup that picks this results partial can
    # fall through to the default one and the page under test never renders.
    monkeypatch.setenv("FLAG_TOOL_PROTEINA", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


def _job(preset: str, campaign_id: str | None):
    """A SUCCEEDED proteina job carrying the zero-candidate result shape both
    writers emit: ``run_validate`` (run_pipeline.py:3920-3935) and the paid
    no-survivors branch (:4343-4352). Only ``preset`` and ``campaign_id``
    differ between the cases, which is what makes the branch the variable
    under test rather than the payload.
    """
    return SimpleNamespace(
        id=_JID,
        tool="proteina",
        preset=preset,
        status="succeeded",
        created_at="2026-09-11T00:00:00+00:00",
        inputs={},
        result={
            "status": "COMPLETED",
            "tier": preset,
            "designs_total": 0,
            "designs_completed": 0,
            "n_failures": 0,
            "designs": [],
            "candidates": [],
            "gpu_seconds": 27,
        },
        error=None,
        gpu_seconds_used=27,
        campaign_id=campaign_id,
    )


def _render(app, preset: str, campaign_id: str | None = None) -> list[str]:
    """GET /jobs/<id> for one job shape and return its paragraphs.

    ``get_job`` is patched where ``blueprints.jobs`` bound it. The succeeded
    branch of the route additionally resolves user metadata for the share
    button through a late ``from shared.jobs import ...``, so that one is
    patched at its source module rather than on the blueprint.
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
    parser = _Paragraphs()
    parser.feed(resp.get_data(as_text=True))
    # The partial only renders under `status == 'succeeded' and job.result`.
    # Without this the whole suite could pass on a page that rendered none of
    # the three branches.
    joined = " ".join(parser.paragraphs)
    assert "Candidates (0)" in resp.get_data(as_text=True), (
        "the results panel did not render; nothing below asserts anything")
    assert joined, "no paragraphs on the page"
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
    """blueprints/campaigns.py:196 and :337 refuse the preset, so the page a
    validate run was being sent to cannot exist."""
    body = _text(app, "validate")
    for claim in (_CAMPAIGN_SENTENCE, _CAMPAIGN_POINTER):
        assert claim not in body, (
            f"validate run still renders {claim!r}, but a validate job can "
            f"never carry a campaign_id:\n  " + body
        )


def test_the_validate_copy_does_not_claim_the_target(app):
    """``run_validate`` never sees the target.

    The adapter's own preset description says the tier "checks your target +
    config load" (tools/proteina/__init__.py:848-850), and the target half of
    that is not what the container does: the validate branch returns at
    run_pipeline.py:4028 and ``download_target`` is not reached until :4240.
    Repeating the adapter's phrasing on the results page would have replaced
    one false sentence with another, so the copy names the three checks that
    actually run and says the target is not among them.
    """
    body = _text(app, "validate")
    assert "not your target" in body, (
        "the validate copy does not tell the operator the target was not "
        "checked, which is the one overclaim available here:\n  " + body
    )


# ---------------------------------------------------------------------------
# 2. A paid shard that IS part of a campaign — the wording was written for it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "preset", ["protein_binder", "ligand_binder", "motif_ame"])
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


def test_a_standalone_paid_run_is_not_told_to_check_a_campaign_page(app):
    """proteina is not campaign-only, so this job shape is ordinary rather
    than hypothetical (tests/test_proteina_smoke.py::test_not_campaign_only).
    It has no campaign and therefore no sibling shards."""
    body = _text(app, "protein_binder", campaign_id=None)
    for claim in (_CAMPAIGN_SENTENCE, _CAMPAIGN_POINTER):
        assert claim not in body, (
            f"a standalone run still renders {claim!r}:\n  " + body)


def test_a_standalone_paid_run_still_reports_the_filtering(app):
    """Dropping the campaign sentence must not drop the actual news: unlike
    validate, this run DID design and everything was culled."""
    body = _text(app, "protein_binder", campaign_id=None)
    assert _FILTER_CLAIM in body, (
        "a standalone paid run no longer says its designs were filtered "
        "out:\n  " + body
    )


# ---------------------------------------------------------------------------
# 4. The three arms are actually distinct
# ---------------------------------------------------------------------------


def test_the_three_cases_render_three_different_messages(app):
    """Guards against a refactor that collapses two arms into one and leaves
    every assertion above still satisfied by a single generic sentence."""
    seen = {
        "validate": _text(app, "validate"),
        "campaign": _text(app, "protein_binder", campaign_id=_CID),
        "standalone": _text(app, "protein_binder", campaign_id=None),
    }
    assert len(set(seen.values())) == 3, (
        "two of the three zero-candidate cases render identical copy: "
        + repr({k: v[:120] for k, v in seen.items()})
    )
