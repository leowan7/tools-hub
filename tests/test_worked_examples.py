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
import statistics
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

    def test_no_html_entity_in_an_escaped_narration_field(self, tools_app):
        """``inputs_used`` renders as ``{{ field }}`` / ``{{ value }}``,
        without ``|safe`` — unlike every prose field beside it. So an
        ``&ndash;`` written there reaches the page as the six literal
        characters, which is exactly what happened. Prose fields may
        keep their entities; these two may not."""
        _, slugs = tools_app
        bad = {}
        for slug, example in _examples(slugs).items():
            if not example:
                continue
            for field, value, _why in example["inputs_used"]:
                for cell in (field, value):
                    if re.search(r"&[a-zA-Z]+;|&#\d+;", cell):
                        bad.setdefault(slug, []).append(cell)
        assert not bad, (
            f"HTML entities in a field rendered without |safe: {bad}"
        )

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

    # Copy that names a control the example suppresses. Not cosmetic: the
    # wet-lab panel told the reader the designs "above" were theirs to
    # download and pointed at a shortlist button, on a page carrying
    # neither. A dead link is caught by the scan above; a sentence
    # describing a button that is not there is not, so it is pinned here.
    PROMISES_A_SUPPRESSED_CONTROL = (
        "shortlist button above",
        "designs above are yours to download",
    )

    def test_example_copy_does_not_promise_controls_it_hides(self, tools_app):
        flask_app, slugs = tools_app
        offenders = {}
        for slug, example in _examples(slugs).items():
            if not example:
                continue
            html = flask_app.test_client().get(f"/tools/{slug}").get_data(
                as_text=True,
            )
            found = [p for p in self.PROMISES_A_SUPPRESSED_CONTROL if p in html]
            if found:
                offenders[slug] = found
        assert not offenders, f"example page promises absent controls: {offenders}"

    def test_a_real_results_page_still_makes_those_promises(self, tools_app):
        """The control. If the phrases vanished from the real page too,
        the test above would pass by scanning for text nobody writes."""
        flask_app, _ = tools_app
        html = _render_partial(
            flask_app, "boltz2", job_id="real-job-1", example=False,
        )
        assert "shortlist button above" in html, (
            "the phrase is gone from the real results page as well, so the "
            "example-side assertion no longer proves anything"
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

    def test_boltz2_narration_matches_its_result_json(self, tools_app):
        """Same pin for boltz2. Its scores sit FLAT on the design rather
        than nested under ``scores``, which is the shape difference that
        made the capture script print nothing at all until it handled
        both — so the figures here are worth holding down."""
        _, slugs = tools_app
        example = _examples(slugs)["boltz2"]
        result = json.loads(
            (REPO / "tools" / "boltz2" / "example" / "result.json")
            .read_text(encoding="utf-8"),
        )
        designs = result["designs"]
        assert len(designs) == 12
        assert result["antigen_length"] == 85
        assert result["hotspots_requested"] == [
            54, 57, 58, 61, 62, 67, 72, 75, 86, 91, 93, 96, 99, 100,
        ]
        iptm = [d["iptm"] for d in designs]
        plddt = [d["complex_plddt"] * 100 for d in designs]
        hits = {d["n_hotspot_contacts"] for d in designs}
        # "every one strict_pass ... 0.874 to 0.952 ... 91.3 to 97.1 ...
        #  13 or 14 of the 14 cleft residues"
        assert all(d["filter_status"] == "strict_pass" for d in designs)
        assert (round(min(iptm), 3), round(max(iptm), 3)) == (0.874, 0.952)
        assert (round(min(plddt), 1), round(max(plddt), 1)) == (91.3, 97.1)
        assert hits == {13, 14}
        assert {d["n_hotspots"] for d in designs} == {14}
        blurb = example["what_came_back"]
        for figure in ("0.874", "0.952", "91.3", "97.1", "strict_pass",
                       "13 or 14"):
            assert figure in blurb, f"{figure} missing from what_came_back"
        assert "1YCR" in example["target"] and "85 residues" in example["target"]
        assert example["inputs_used"][2][1] == (
            "54, 57, 58, 61, 62, 67, 72, 75, 86, 91, 93, 96, 99, 100"
        )

        # The reference band is quoted in prose because those folds are
        # NOT rows in the payload — see the comment in meta.py. Nothing
        # in this file can re-derive them, so this pins the shape of the
        # claim instead: three named binders, each with a range, and the
        # affinity caveat that is the whole point of quoting them.
        reading = example["how_to_read_it"]
        for ref in ("p53", "PDI", "PMI"):
            assert ref in reading, f"{ref} missing from how_to_read_it"
        assert "0.905" in reading and "0.941" in reading
        assert "does not rank affinity" in reading

        # No invented price: campaign compute, not a wallet-billed job.
        assert not example.get("cost_usd")
        # No structure_file: static/example/ carries no 1YCR.
        assert not example.get("structure_file")


    def test_pxdesign_narration_matches_its_result_json(self, tools_app):
        """Third pin. This example's whole lesson is a contrast between two
        columns, so if either median drifts the page argues for something
        the table no longer shows."""
        _, slugs = tools_app
        example = _examples(slugs)["pxdesign"]
        result = json.loads(
            (REPO / "tools" / "pxdesign" / "example" / "result.json")
            .read_text(encoding="utf-8"),
        )
        cands = result["candidates"]
        scores = [c["scores"] for c in cands]
        iptm = [s["ipTM"] for s in scores]
        plddt = [s["pLDDT"] for s in scores]

        assert len(cands) == 25 == result["total_designs"]
        assert sum(1 for s in scores if s["filter_status"] == "pass") == 2
        assert max(iptm) == 0.88
        assert statistics.median(iptm) == 0.14
        assert statistics.median(plddt) == 91
        # The headline claim, re-counted rather than trusted.
        assert sum(
            1 for s in scores if s["pLDDT"] >= 85 and s["ipTM"] < 0.3
        ) == 21
        # Rank 1 is the design the narration describes.
        assert scores[0]["ipTM"] == 0.88 and scores[0]["pLDDT"] == 88.0

        blurb = example["what_came_back"]
        for figure in ("25 designs", "2 passed", "0.88", "88", "91",
                       "0.14", "21 of the 25"):
            assert figure in blurb, f"{figure} missing from what_came_back"

        # Only ipTM / pLDDT / pAE / filter_status: the four keys the live
        # tool emits. An example that carried complex RMSD or buried area
        # would render a payload shape pxdesign never returns, so those two
        # figures stay in prose and this asserts they cannot creep in.
        assert all(
            set(s) == {"ipTM", "pLDDT", "pAE", "filter_status"} for s in scores
        )
        reading = example["how_to_read_it"]
        assert "0.50" in reading and "3.0" in reading
        assert "Sort by ipTM, never by pLDDT." in reading

        # No sequences, no structures, no target identity anywhere.
        blob = json.dumps(result)
        for leaked in ("sequence", "pdb_key", "pdb_content_b64", "struct_path"):
            assert leaked not in blob, f"{leaked} leaked into the payload"
        assert not example.get("structure_file")

        # The length counts are 25 per bin and the prose must keep saying so
        # rather than reporting 0/2/2/4 as a trend.
        nxt = example["what_we_did_next"]
        assert "0, 2, 2 and 4" in nxt and "25 per bin" in nxt

    def test_proteina_narration_matches_its_result_json(self, tools_app):
        """Proteina's example exists to name one failure mode: the
        generator handing the target's own sequence back as the binder.
        That claim rests on three numbers — how many rows do it, what
        their ``binder_scrmsd`` is, and what their ``af2_plddt`` is — and
        the third is the load-bearing one, because the whole point is
        that pLDDT does NOT flag them. Re-derived here so a copy edit
        cannot soften any of the three."""
        _, slugs = tools_app
        example = _examples(slugs)["proteina"]
        result = json.loads(
            (REPO / "tools" / "proteina" / "example" / "result.json")
            .read_text(encoding="utf-8"),
        )
        cands = result["candidates"]
        assert len(cands) == 64
        assert result["gpu_seconds"] == 3447

        def col(key, rows=None):
            return [c["scores"][key] for c in (rows if rows is not None else cands)]

        # Ranks are the pipeline's own order: total_reward descending.
        assert [c["rank"] for c in cands] == list(range(1, 65))
        rewards = col("total_reward")
        assert rewards == sorted(rewards, reverse=True)

        # "12 passed — ipTM at or above 0.80 with the re-folded complex
        #  landing within 5 A ... ranks 1 to 11 and 13"
        passed = [
            c for c in cands
            if c["scores"]["af2_iptm"] >= 0.80
            and c["scores"]["binder_scrmsd"] < 5.0
        ]
        assert len(passed) == 12
        assert [c["rank"] for c in passed] == list(range(1, 12)) + [13]
        assert "12 passed" in example["what_came_back"]
        assert "ranks 1 to 11 and 13" in example["what_came_back"]

        # "The best scored ipTM 0.89 at pLDDT 0.89, re-folding 1.32 A"
        top = cands[0]["scores"]
        assert round(top["af2_iptm"], 2) == 0.89
        assert round(top["af2_plddt"], 2) == 0.89
        assert round(top["binder_scrmsd"], 2) == 1.32
        assert "1.32 &Aring;" in example["what_came_back"]

        # "Of the 52 that did not pass, 30 failed on the re-fold."
        assert len(cands) - len(passed) == 52
        failed_refold = [c for c in cands if c["scores"]["binder_scrmsd"] >= 5.0]
        assert len(failed_refold) == 30
        assert "52 that did not pass, 30 failed" in example["what_came_back"]

        reading = example["how_to_read_it"]

        # THE SHORTLIST, AND WHY IT IS NOT THE ANSWER. The score profile
        # the prose names catches THIRTEEN rows; only twelve of them are
        # copies. Which twelve is a sequence-vs-target measurement, and
        # this payload deliberately carries no sequences, so the test pins
        # the shortlist and the one member that is not a copy rather than
        # pretending the scores settle it. An earlier draft asserted 12
        # here and was wrong by exactly this row.
        band = [
            c for c in cands
            if c["scores"]["binder_scrmsd"] >= 30.0
            and c["scores"]["af2_iptm"] < 0.10
        ]
        assert len(band) == 13
        assert "Thirteen rows here" in reading
        assert "twelve of them are copies" in reading
        assert "Twelve of those 30" in example["what_came_back"]

        odd = [c for c in band if c["scores"]["af2_plddt"] < 0.70]
        assert [c["rank"] for c in odd] == [55]
        assert round(odd[0]["scores"]["af2_plddt"], 2) == 0.58
        assert "rank 55" in reading and "0.58" in reading
        copies = [c for c in band if c not in odd]
        assert len(copies) == 12

        # pLDDT does NOT flag them: 0.71-0.76, inside the shard's own
        # spread. If a re-capture ever made these look broken on pLDDT,
        # the paragraph would be arguing for a check nobody needs.
        cp = col("af2_plddt", copies)
        assert 0.70 <= min(cp) and max(cp) < 0.77
        assert "0.71 to 0.76" in reading
        cs = col("binder_scrmsd", copies)
        assert 32.0 <= min(cs) and max(cs) <= 44.5
        assert "32 to 44 &Aring;" in reading
        ci = col("af2_iptm", copies)
        assert 0.086 <= min(ci) and max(ci) <= 0.098
        assert "0.086 to 0.098" in reading

        # ... against the rest of the shard.
        rest = [c for c in cands if c not in copies]
        assert round(statistics.median(col("binder_scrmsd", rest)), 1) == 2.0
        assert "median of 2.0 &Aring;" in reading
        assert round(statistics.median(col("af2_iptm", rest)), 2) == 0.67
        assert "0.67 median" in reading

        # "total_reward sends all twelve to ranks 51 to 64"
        assert min(c["rank"] for c in copies) >= 51
        assert "ranks 51 to 64" in reading
        assert "binder_scrmsd" in reading and "af2_plddt" in reading

        # THE HOTSPOT NOTE IS PINNED TO THE LIVE GATE, not to memory. The
        # first draft of it described the PRE-#130 behaviour — a bare
        # token silently promoted onto chain A — which #130 replaced with
        # a refusal. Prose about a fixed bug reads as current advice, so
        # the claim is asserted against the validator that enforces it.
        from tools import proteina as proteina_adapter

        hotspot_why = example["inputs_used"][1][2]
        assert "refuses a bare" in hotspot_why
        spec, err = proteina_adapter.validate(
            {
                "preset": "protein_binder",
                "target_input": "A1-40,B500-539",
                "hotspot_residues": "520",
                "binder_length_min": "60",
                "binder_length_max": "80",
                "_has_custom_target": "1",
            },
            {},
        )
        assert err and spec is None, (
            "a bare hotspot on a two-chain proteina run is accepted again — "
            "the worked example tells the reader it is refused"
        )

        # THE pLDDT POLARITY CHECK the previous EXAMPLE = None note asked
        # for. Pre-#129 payloads stored 1 - pLDDT, which flips both signs.
        # This is the assertion a re-capture from an old run cannot pass.
        plddt, iptm = col("af2_plddt"), col("af2_iptm")
        assert statistics.correlation(plddt, iptm) > 0.5
        assert statistics.correlation(plddt, rewards) > 0.5
        assert (statistics.median(col("af2_plddt", cands[:12]))
                > statistics.median(col("af2_plddt", cands[-12:])))

        # rf3_score and cluster_id are ABSENT, not zero. The narration
        # tells the reader that column is empty because RF3 was off, and a
        # stub value of 0 would render a confident "0.00" instead of the
        # em dash — see templates/components/candidate_table.html.
        for c in cands:
            assert set(c["scores"]) == {
                "total_reward", "af2_iptm", "af2_plddt", "binder_scrmsd",
            }
        assert "rf3_score" in example["inputs_used"][4][2]

        blob = json.dumps(result)
        for leaked in ("sequence", "pdb_key", "pdb_content_b64", "struct_path"):
            assert leaked not in blob, f"{leaked} leaked into the payload"
        assert not example.get("structure_file")

        # The campaign-scale claim, which is NOT derivable from this
        # payload and so is pinned as prose only.
        nxt = example["what_we_did_next"]
        assert "17,024 designs" in nxt and "29%" in nxt

    # GPU-seconds behind each example's recorded cost. proteina carries it
    # in the payload; pxdesign's payload records runtime_minutes instead.
    _EXAMPLE_GPU_SECONDS = {"proteina": 3447.0, "pxdesign": 1380.0}

    def test_recorded_cost_is_what_this_tool_would_charge(self, tools_app):
        """``cost_usd`` tells a reader what a run of this tool costs, on a
        page they can read without signing in, so it has to be the
        CUSTOMER-facing charge rather than the raw Modal cost. The two
        differ by shared.wallet.WALLET_MARKUP, and the first pxdesign
        example quoted the raw figure — 18% under what the wallet would
        actually settle at.

        Recomputing it here also ties the page to the rate card: change a
        tool's ``gpu_class`` in shared/wallet_estimates.py and this fails
        rather than leaving a stale price in front of a customer."""
        from decimal import Decimal

        from shared.wallet import compute_charge_usd
        from shared.wallet_estimates import TOOL_SPECS

        _, slugs = tools_app
        for slug, example in _examples(slugs).items():
            gpu_seconds = self._EXAMPLE_GPU_SECONDS.get(slug)
            if gpu_seconds is None:
                continue
            spec = TOOL_SPECS[slug]
            expected = compute_charge_usd(gpu_seconds, spec.gpu_class)
            assert Decimal(example["cost_usd"]) == expected.quantize(
                Decimal("0.01"),
            ), (
                f"{slug} example says ${example['cost_usd']} but "
                f"{gpu_seconds:.0f} GPU-s on {spec.gpu_class} settles at "
                f"${expected}"
            )


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
