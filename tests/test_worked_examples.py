"""``EXAMPLE`` — a real past run replayed on the public tool page.

``templates/components/worked_example.html`` renders a canned
``job.result`` through the tool's OWN results partial, which is what
stops the demo drifting from the real results page. The cost of that is
that the partial does not know it is a demo: it emits a clone link keyed
on ``job.id``, artifact downloads keyed on storage paths, and on some
tools whole POST forms. With ``job.id = "example"`` every one of those
404s, and nothing about the page would look wrong.

THIS FILE IS THAT REGRESSION. It renders every tool that declares an
EXAMPLE and fails on any surviving job-scoped href or form action. It is
written to generalise: only mpnn ships an EXAMPLE today, and only
mpnn_results.html has been given the ``example`` guard, so the day
another tool gains one this test fails until its partial is guarded too.
That is the intended behaviour, not a gap — a speculative guard in
fourteen partials for payloads that do not exist is code nobody has ever
executed.

``TestTheDetectorIsNotBlind`` is the positive control: it renders an
UNGUARDED partial through the same stub and asserts the scan fires.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from shared.tool_meta import meta_for

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REPO = Path(__file__).resolve().parent.parent

# A URL is dead in an example if it is scoped to a job, a campaign or a
# storage artifact that the stub does not have behind it.
DEAD_URL_PATTERNS = (
    "/jobs/",
    "clone_from=",
    "resample_from=",
    "campaign_id=",
    "/lab-projects/",
    "/export.",
)


@pytest.fixture(scope="module")
def tools_app():
    import os

    import app as app_module
    from shared.feature_flags import flag_name
    from tools import base as tool_base

    slugs = sorted(a.slug for a in tool_base.all_adapters())
    assert len(slugs) >= 14, f"adapter registry holds {len(slugs)} tools"
    prior = {}
    for slug in slugs:
        prior[flag_name(slug)] = os.environ.get(flag_name(slug))
        os.environ[flag_name(slug)] = "on"
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    yield flask_app, slugs
    for key, val in prior.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _examples(slugs):
    return {s: getattr(meta_for(s), "EXAMPLE", None) for s in slugs}


def _dead_urls(html: str) -> list[str]:
    """Every href/action in ``html`` that only a real job could satisfy."""
    urls = re.findall(r'\b(?:href|action)\s*=\s*"([^"]*)"', html)
    return [u for u in urls if any(p in u for p in DEAD_URL_PATTERNS)]


def _stub_job(slug: str, result: dict) -> dict:
    """The same mapping worked_example.html builds. A dict, not a model:
    the partials only ever read .id / .status / .result / .inputs."""
    return {
        "id": "example", "status": "succeeded", "tool": slug,
        "inputs": {}, "result": result,
    }


class TestExampleDeclaration:

    def test_every_tool_declares_example_explicitly(self, tools_app):
        """None is a decision — thirteen of fourteen have no captured
        payload — and each meta.py records which. A missing attribute is
        an oversight."""
        _, slugs = tools_app
        missing = [s for s in slugs if not hasattr(meta_for(s), "EXAMPLE")]
        assert not missing, f"meta.py declares no EXAMPLE for: {missing}"

    def test_at_least_one_example_ships(self, tools_app):
        """Otherwise every assertion below passes over an empty set."""
        _, slugs = tools_app
        assert any(_examples(slugs).values()), (
            "no tool ships a worked example; the link regression below "
            "would be vacuously green"
        )

    def test_narration_and_payload_travel_together(self, tools_app):
        """A declared EXAMPLE must have a real result.json beside it.

        Narration alone would render a description of results above an
        empty results panel.
        """
        _, slugs = tools_app
        for slug, example in _examples(slugs).items():
            if not example:
                continue
            path = (
                REPO / "tools" / slug.replace("-", "_")
                / "example" / "result.json"
            )
            assert path.is_file(), f"{slug}: EXAMPLE declared, {path} missing"
            assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_structure_file_exists(self, tools_app):
        """The offered download must be a file that is actually served."""
        _, slugs = tools_app
        for slug, example in _examples(slugs).items():
            if not example or not example.get("structure_file"):
                continue
            path = REPO / "static" / "example" / example["structure_file"]
            assert path.is_file(), f"{slug}: {path} is not on disk"


class TestNoDeadLinkInsideAnExample:
    """The regression that would otherwise reach production silently."""

    def test_rendered_tool_page_has_no_job_scoped_url(self, tools_app):
        flask_app, slugs = tools_app
        client = flask_app.test_client()
        broken = {}
        for slug, example in _examples(slugs).items():
            if not example:
                continue
            resp = client.get(f"/tools/{slug}")
            assert resp.status_code == 200, f"{slug} -> {resp.status_code}"
            html = resp.get_data(as_text=True)
            assert "A run we actually did" in html, (
                f"{slug} declares an EXAMPLE but the page does not render "
                "it — the scan below would be vacuously clean"
            )
            dead = _dead_urls(html)
            if dead:
                broken[slug] = dead
        assert not broken, f"dead links inside a worked example: {broken}"

    def test_partial_rendered_in_isolation_is_also_clean(self, tools_app):
        """Same check one layer down, so a page-level wrapper cannot mask it."""
        flask_app, slugs = tools_app
        from tools import base as tool_base

        for slug, example in _examples(slugs).items():
            if not example:
                continue
            adapter = tool_base.get(slug)
            result = json.loads(
                (
                    REPO / "tools" / slug.replace("-", "_")
                    / "example" / "result.json"
                ).read_text(encoding="utf-8"),
            )
            with flask_app.test_request_context(f"/tools/{slug}"):
                html = flask_app.jinja_env.get_template(
                    adapter.results_partial,
                ).render(job=_stub_job(slug, result), example=True)
            assert not _dead_urls(html), f"{slug}: {_dead_urls(html)}"


class TestTheDetectorIsNotBlind:
    """Without this, the tests above could pass by scanning for nothing."""

    def test_an_unguarded_partial_trips_the_scan(self, tools_app):
        """rfdiffusion ships no EXAMPLE and its partial has no guard.

        Rendering it through the same stub must produce dead links. If it
        ever stops doing so, either the partial was guarded (fine, update
        this control) or DEAD_URL_PATTERNS has gone blind (not fine).
        """
        flask_app, _ = tools_app
        from tools import base as tool_base

        adapter = tool_base.get("rfdiffusion")
        payload = {
            "tier": "pilot",
            "candidates": [
                {
                    "rank": 1, "name": "d1", "pdb_key": "design_001.pdb",
                    "scores": {
                        "ipTM": 0.81, "pLDDT": 0.94, "i_pAE": 4.8,
                        "filter_status": "pass",
                    },
                },
            ],
        }
        with flask_app.test_request_context("/tools/rfdiffusion"):
            html = flask_app.jinja_env.get_template(
                adapter.results_partial,
            ).render(job=_stub_job("rfdiffusion", payload), example=True)
        assert _dead_urls(html), (
            "an unguarded results partial produced no job-scoped URL — "
            "DEAD_URL_PATTERNS is no longer detecting anything"
        )

    def test_the_guard_is_what_makes_mpnn_clean(self, tools_app):
        """Drop the flag and mpnn's partial must emit its links again."""
        flask_app, _ = tools_app
        from tools import base as tool_base

        adapter = tool_base.get("mpnn")
        result = json.loads(
            (REPO / "tools" / "mpnn" / "example" / "result.json")
            .read_text(encoding="utf-8"),
        )
        with flask_app.test_request_context("/tools/mpnn"):
            html = flask_app.jinja_env.get_template(
                adapter.results_partial,
            ).render(job=_stub_job("mpnn", result), example=False)
        assert _dead_urls(html), (
            "mpnn_results.html emitted no job-scoped URL even unguarded, "
            "so the example=true guard is not what is keeping it clean"
        )


class TestExampleNumbersComeFromThePayload:
    """The narration must not drift off the data it sits beside.

    Not a general check — it cannot be — but every figure quoted in
    mpnn's narration is derivable from its result.json, and this pins
    the ones a copy edit is most likely to round away.
    """

    def test_mpnn_narration_matches_its_result_json(self, tools_app):
        _, slugs = tools_app
        example = _examples(slugs)["mpnn"]
        result = json.loads(
            (REPO / "tools" / "mpnn" / "example" / "result.json")
            .read_text(encoding="utf-8"),
        )
        seqs = result["sequences"]
        assert len(seqs) == 2
        # "Two sequences, 129 residues each, recovering 53% and 50% ...
        #  at scores of 0.76."
        assert {len(s["seq"]) for s in seqs} == {129}
        assert [round(s["recovery"] * 100) for s in seqs] == [53, 50]
        assert {round(s["score"], 2) for s in seqs} == {0.76}
        assert "129 residues each" in example["what_came_back"]
        assert "53% and 50%" in example["what_came_back"]
        assert "0.76" in example["what_came_back"]
        assert "2" == example["inputs_used"][2][1]
