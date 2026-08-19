"""``EXAMPLE`` — a real past run replayed on the public tool page.

``templates/components/worked_example.html`` renders a canned
``job.result`` through the tool's OWN results partial, which is what
stops the demo drifting from the real results page. The cost of that is
that the partial does not know it is a demo: it emits a clone link keyed
on ``job.id``, artifact downloads keyed on storage paths, and on some
tools whole POST forms. With ``job.id = "example"`` every one of those
404s, and nothing about the page would look wrong.

THIS FILE IS THAT REGRESSION. It renders every tool that declares an
EXAMPLE and fails on any surviving job-scoped href or form action.

The guard itself is NOT per-partial. Every job-scoped URL in the app
comes from two shared macros — components/results_shell.html (the
refold POSTs, the clone link) and components/candidate_table.html (the
exports, the per-design .pdb, the lab-submit modal) — and thirteen of
the fourteen partials route through one or both. Both macros suppress
those controls when ``job_id`` is the "example" sentinel that
components/worked_example.html passes, so every tool is example-safe
before it has an EXAMPLE rather than after. mpnn is the fourteenth: it
deliberately skips both macros (its schema is ``sequences``, not
``candidates``) and carries its own inline guard keyed on ``example``.

``TestEveryPartialIsExampleSafe`` verifies that across all fourteen,
including the thirteen with no payload yet, so the guard is proven
before it is depended on. ``TestTheDetectorIsNotBlind`` is the positive
control: the same renders with a REAL job id must still emit the links,
otherwise the scan has gone blind and everything above it is vacuous.
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
    # Into the SAME prior dict the flags use, so the teardown loop
    # below restores it. setdefault() reads as safe and is not: it
    # leaks the key into every test that runs after this module.
    prior["SESSION_SECRET_KEY"] = os.environ.get("SESSION_SECRET_KEY")
    os.environ["SESSION_SECRET_KEY"] = "test-secret"
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


def _stub_job(slug: str, result: dict, job_id: str = "example") -> dict:
    """The same mapping worked_example.html builds. A dict, not a model:
    the partials only ever read .id / .status / .result / .inputs.

    ``job_id`` is overridable for the positive control only: passing a
    realistic id renders exactly what a signed-in user sees on
    /jobs/<id>, which must still be full of job-scoped links.
    """
    return {
        "id": job_id, "status": "succeeded", "tool": slug,
        "inputs": {}, "result": result,
    }


# A payload wide enough to populate any of the fourteen partials: the
# composite tools read ``candidates``, boltz2 reads ``designs``, mpnn
# reads ``sequences``, and the score keys are the union of every
# ``columns`` list. Deliberately not per-tool — the point is to drive
# each partial far enough down its own happy path to emit its links.
_GENERIC_RESULT = {
    "tier": "pilot",
    "candidates": [{
        "rank": 1, "name": "d1", "pdb_key": "design_001.pdb", "seq": "MKV",
        "scores": {
            "ipTM": 0.81, "pTM": 0.80, "pLDDT": 0.94, "i_pAE": 4.8,
            "refolding_rmsd": 1.2, "n_hotspot_contacts": 5,
            "af2_iptm": 0.81, "af2_plddt": 0.90, "binder_scrmsd": 1.1,
            "total_reward": 0.4, "rf3_score": 0.5, "cluster_id": 1,
            "filter_status": "pass",
        },
    }],
    "sequences": [{"seq": "MKV", "score": 0.76, "recovery": 0.53, "sample": 1}],
    "designs_total": 1, "designs_completed": 1, "n_failures": 0,
    "runtime_seconds": 100, "gpu_seconds": 100,
    "hotspots_requested": [], "antigen_length": 100,
}
_GENERIC_RESULT["designs"] = _GENERIC_RESULT["candidates"]

# One payload is not enough. A partial's job-scoped links are spread
# across its success branch, its empty branch and blocks gated on
# optional payload keys, so a single rich payload leaves whole branches
# unrendered and their links unscanned. Three shapes were the minimum
# that exposed real leaks: the empty shape caught the clone links af2,
# colabfold and esmfold emit outside the shared macros, and the b64
# shape caught af2's download_pdb / download_pae / resample_from block,
# which is gated on ``pdb_b64`` and invisible without it.
_EMPTY_RESULT = {
    "tier": "pilot", "candidates": [], "designs": [], "sequences": [],
    "runtime_seconds": 10,
}
_B64_RESULT = dict(
    _GENERIC_RESULT,
    pdb_b64="UERC", pae_matrix_b64="UEFF",
    pdb_content_b64="UERC",
)
_PAYLOAD_SHAPES = {
    "rich": _GENERIC_RESULT,
    "empty": _EMPTY_RESULT,
    "with-b64-artifacts": _B64_RESULT,
}


def _render_partial(
    flask_app, slug: str, *, job_id: str, example: bool, result=None,
) -> str:
    from tools import base as tool_base

    adapter = tool_base.get(slug)
    with flask_app.test_request_context(f"/tools/{slug}"):
        return flask_app.jinja_env.get_template(adapter.results_partial).render(
            job=_stub_job(
                slug, _GENERIC_RESULT if result is None else result,
                job_id=job_id,
            ),
            example=example,
            send_target_tools=[],
        )


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

    def test_a_real_job_still_emits_job_scoped_urls(self, tools_app):
        """The same fourteen renders, with a real job id.

        This is what stops TestEveryPartialIsExampleSafe passing for the
        wrong reason. If a partial emitted no job-scoped URL even for a
        real job, its clean example would prove nothing — the guard
        would be sitting in front of a door that was never open.

        It replaces an earlier control that rendered rfdiffusion as the
        specimen "with no guard". Once the guard moved into the two
        shared macros, no unguarded partial was left to point at; keying
        the control on the job id instead means it can never be
        invalidated by guarding one more tool.

        Asserted per TOOL, not per (tool, payload shape): a branch may
        legitimately carry no job-scoped link at all. proteina is the
        real case — its partial never passes ``clone_url``, so its
        empty-candidates branch has nothing to suppress. Demanding a
        link from every shape would assert something false about that
        template and force a guard where there is nothing to guard.
        """
        flask_app, slugs = tools_app
        silent = [
            s for s in slugs
            if not any(
                _dead_urls(_render_partial(
                    flask_app, s, job_id="real-job-123", example=False,
                    result=result,
                ))
                for result in _PAYLOAD_SHAPES.values()
            )
        ]
        assert not silent, (
            f"{silent} emitted no job-scoped URL under ANY payload shape "
            "even for a real job, so their worked examples would be clean "
            "for the wrong reason"
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


class TestEveryPartialIsExampleSafe:
    """Thirteen of these tools have no EXAMPLE yet. That is the point.

    The guard lives in two shared macros, so it can be verified for a
    tool BEFORE that tool gains a payload — which is the difference
    between shipping the fourteenth example safely and discovering on
    the public page that its partial was the one nobody guarded.
    """

    def test_no_partial_emits_a_job_scoped_url_under_the_sentinel(
        self, tools_app,
    ):
        flask_app, slugs = tools_app
        broken = {}
        for slug in slugs:
            for shape, result in _PAYLOAD_SHAPES.items():
                dead = _dead_urls(_render_partial(
                    flask_app, slug, job_id="example", example=True,
                    result=result,
                ))
                if dead:
                    broken[f"{slug}/{shape}"] = dead
        assert not broken, f"job-scoped URLs survive the example guard: {broken}"

    def test_the_sentinel_alone_is_enough(self, tools_app):
        """``example=False`` but the sentinel id: still clean.

        worked_example.html sets both, but an imported macro cannot see
        the ``example`` flag (Jinja needs ``with context``), so for the
        thirteen macro-driven tools the job id is the ONLY signal that
        reaches the guard. If this ever fails, the guard silently
        depends on a variable the macros do not actually receive.
        """
        flask_app, slugs = tools_app
        broken = {}
        for slug in slugs:
            if slug == "mpnn":  # guards itself on ``example``; see above
                continue
            for shape, result in _PAYLOAD_SHAPES.items():
                dead = _dead_urls(_render_partial(
                    flask_app, slug, job_id="example", example=False,
                    result=result,
                ))
                if dead:
                    broken[f"{slug}/{shape}"] = dead
        assert not broken, f"guard depends on a flag the macros never see: {broken}"


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


class TestCaptureScrubsBeforeItPublishes:
    """scripts/capture_example_result.py pulls from the PRODUCTION jobs
    table and writes into a page anyone can read. The scrub is the only
    thing between a customer's run and a public URL, so it is tested
    rather than eyeballed."""

    def test_sensitive_keys_are_stripped_at_every_depth(self):
        from scripts.capture_example_result import scrub

        payload = {
            "tier": "pilot",
            "user_email": "someone@example.com",
            "candidates": [
                {"rank": 1, "storage_path": "s3://bucket/x.pdb", "score": 0.8},
                {"rank": 2, "nested": {"workspace_id": "ws_1", "keep": "yes"}},
            ],
        }
        clean, removed = scrub(payload)
        flat = json.dumps(clean)
        for leaked in ("someone@example.com", "s3://bucket", "ws_1"):
            assert leaked not in flat, f"{leaked} survived the scrub"
        assert clean["tier"] == "pilot"
        assert clean["candidates"][0]["score"] == 0.8
        assert clean["candidates"][1]["nested"]["keep"] == "yes"
        assert set(removed) == {
            "user_email",
            "candidates[0].storage_path",
            "candidates[1].nested.workspace_id",
        }

    def test_inline_pdb_is_kept_only_for_the_top_designs(self):
        """Keeping the blob AND dropping the key is what makes the
        example's own .pdb download work: candidate_table falls back to
        a self-contained data: URI when there is no storage key. Every
        other design drops the blob, because eight of them is 3.5 MB."""
        from scripts.capture_example_result import trim_structures

        result = {"candidates": [
            {"rank": i, "pdb_key": f"d{i}.pdb", "pdb_content_b64": "UERC"}
            for i in range(4)
        ]}
        assert trim_structures(result, 1) == 1
        top, rest = result["candidates"][0], result["candidates"][1:]
        assert top["pdb_content_b64"] == "UERC" and "pdb_key" not in top
        for row in rest:
            assert "pdb_content_b64" not in row
            assert row["pdb_key"]

    def test_inline_pdb_zero_keeps_none(self):
        from scripts.capture_example_result import trim_structures

        result = {"candidates": [{"pdb_key": "d.pdb", "pdb_content_b64": "X"}]}
        assert trim_structures(result, 0) == 0
        assert "pdb_content_b64" not in result["candidates"][0]
