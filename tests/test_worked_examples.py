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

import base64
import json
import statistics
import re
from pathlib import Path

import pytest

from shared.metric_glossary import plddt_on_100
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



def _form_labels(html: str) -> set[str]:
    """Every <label> on a rendered form page, whitespace-normalised.

    The set the reader can actually see. Anything ``inputs_used`` names
    has to be in here verbatim.
    """
    out = set()
    for raw in re.findall(r"<label[^>]*>(.*?)</label>", html, re.S | re.I):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
        if text:
            out.add(text)
    return out

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
        # The PROSE quotes what the page renders (58.38 on the 0-100
        # scale); the PAYLOAD stores 0.5838. Both are asserted, because
        # normalising the column without rescaling this sentence is what
        # made the page contradict its own table.
        assert "rank 55" in reading and "58.38" in reading
        copies = [c for c in band if c not in odd]
        assert len(copies) == 12

        # pLDDT does NOT flag them: 0.71-0.76, inside the shard's own
        # spread. If a re-capture ever made these look broken on pLDDT,
        # the paragraph would be arguing for a check nobody needs.
        cp = col("af2_plddt", copies)
        assert 0.70 <= min(cp) and max(cp) < 0.77
        # Payload 0-1, prose on the 0-100 scale the table renders.
        assert (round(plddt_on_100(min(cp)), 2),
                round(plddt_on_100(max(cp)), 2)) == (70.79, 75.65)
        assert "70.79 to 75.65" in reading
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
    # The four predictors record it as runtime_seconds, which for these
    # jobs equals the gpu_seconds_used the wallet settled on.
    _EXAMPLE_GPU_SECONDS = {
        "proteina": 3447.0,
        "pxdesign": 1380.0,
        "esmfold": 32.0,
        "colabfold": 55.0,
        "af2": 388.0,
        "esmfold2-design": 306.0,
    }

    def test_recorded_cost_is_what_this_tool_would_charge(self, tools_app):
        """``cost_usd`` tells a reader what a run of this tool costs, on a
        page they can read without signing in, so it has to be the
        CUSTOMER-facing charge rather than the raw Modal cost. The two
        differ by shared.wallet.WALLET_MARKUP, and the first pxdesign
        example quoted the raw figure — 41% under what the wallet would
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


    # ---------------- the four predictors ----------------
    #
    # Each of these examples is built on ONE reading of its payload, and
    # the reading is the whole value of the page: a mean that hides a
    # shape, a spread too narrow to rank, a top score that must not be
    # ordered. A copy edit that rounds a figure away takes the lesson
    # with it, so every number the prose quotes is recomputed here from
    # the JSON beside it.

    @staticmethod
    def _plddt_pct(result):
        """Per-residue pLDDT on a 0-100 scale.

        ESMFold reports 0-1 and ColabFold reports 0-100 for the same
        metric. Each page prints its own payload raw, so the NARRATION
        differs by tool on purpose -- but a distribution has to be
        compared on one scale, which is this.
        """
        vals = result["plddt_per_residue"]
        scale = 100.0 if max(vals) <= 1.0 else 1.0
        return [v * scale for v in vals]

    def test_esmfold_narration_matches_its_result_json(self, tools_app):
        """The disordered-protein reading: low EVERYWHERE, which is the
        correct answer rather than a failure. The claim that carries it
        is that no residue reaches 70 -- one residue at 71 would make
        the page wrong."""
        _, slugs = tools_app
        example = _examples(slugs)["esmfold"]
        result = json.loads(
            (REPO / "tools" / "esmfold" / "example" / "result.json")
            .read_text(encoding="utf-8"),
        )
        assert result["total_length"] == 304
        assert result["mean_plddt"] == 0.39
        assert round(result["ptm"], 3) == 0.119

        # TWO scales, asserted as two halves that must agree.
        #
        # The PAYLOAD stores 0-1, because that is what ESMFold's head
        # returns and the stored job is not rewritten. The PAGE renders
        # 0-100, because plddt_on_100 normalises at display time. The
        # prose describes the PAGE. Asserting only one half is how the
        # original defect shipped: the old version of this test
        # normalised the payload to whatever scale the prose used, so it
        # certified the prose against the data while the reader was shown
        # something else.
        from shared.metric_glossary import plddt_on_100

        raw = result["plddt_per_residue"]
        assert len(raw) == 304
        assert max(raw) <= 1.0, "the stored payload is on 0-1"

        shown = [plddt_on_100(v) for v in raw]
        assert max(shown) > 1.0, "the displayed value is on 0-100"
        assert sum(1 for v in shown if v >= 70) == 0, (
            "the example's central claim is that NOTHING reaches 70"
        )
        assert round(max(shown), 1) == 65.9
        # The strip's own colour buckets, because "the whole strip is red"
        # was the claim before this and 26 residues are not red.
        assert sum(1 for v in shown if v < 50) == 278
        assert sum(1 for v in shown if 50 <= v < 70) == 26
        assert sum(1 for v in shown if v >= 90) == 0

        longest = run = 0
        for v in shown:
            run = run + 1 if v >= 50 else 0
            longest = max(longest, run)
        assert longest == 10

        came = example["what_came_back"]
        assert "39.00" in came and "0.119" in came
        assert "65.9" in came and "278 of 304" in came
        assert "26 are amber" in came
        assert "10 residues" in came
        assert "<strong>Not one residue reaches 70</strong>" in came
        # The page no longer needs a units caveat, and must not carry a
        # stale one. Both were true of this page at different times.
        assert "0-to-1 scale" not in came
        # The reading, not just the numbers.
        assert "correct answer, not a failed run" in example["how_to_read_it"]

    def test_colabfold_narration_matches_its_result_json(self, tools_app):
        """The opposite reading on a similar-looking number: a mean that
        hides a folded core plus a floppy tail. The 22-residue boundary
        is load-bearing -- it is what turns 'redesign it' into 'trim
        22 residues'."""
        _, slugs = tools_app
        example = _examples(slugs)["colabfold"]
        result = json.loads(
            (REPO / "tools" / "colabfold" / "example" / "result.json")
            .read_text(encoding="utf-8"),
        )
        assert result["total_length"] == 101
        assert result["mean_plddt"] == 61.05
        assert result["ptm"] == 0.62
        assert result["num_recycles"] == 2
        assert result["use_templates"] is False

        pct = self._plddt_pct(result)
        lead = 0
        while lead < len(pct) and pct[lead] < 50:
            lead += 1
        assert lead == 22, "the N-terminal disordered run is the finding"
        rest = pct[lead:]
        assert len(rest) == 79
        assert round(sum(rest) / len(rest), 1) == 67.3
        assert round(max(pct), 1) == 89.9
        assert sum(1 for v in rest if v >= 70) == 27

        came = example["what_came_back"]
        for figure in ("61.05", "0.62", "22 residues", "67.3", "89.9",
                       "27 residues"):
            assert figure in came, figure
        assert "The mean cannot tell those apart" in example["how_to_read_it"]

    def test_af2_narration_matches_its_result_json(self, tools_app):
        """Ten designs that agree. The spread figures are the point: if
        a future capture widened them the 'this is not a ranking'
        reading would stop being true, and this fails rather than
        letting the page keep asserting it."""
        _, slugs = tools_app
        example = _examples(slugs)["af2"]
        result = json.loads(
            (REPO / "tools" / "af2" / "example" / "result.json")
            .read_text(encoding="utf-8"),
        )
        designs = result["designs"]
        assert len(designs) == 10
        assert result["designs_completed"] == 10 and result["n_failures"] == 0
        assert {d["total_aa"] for d in designs} == {333}
        assert {d["model_preset"] for d in designs} == {"monomer"}
        assert all(d["iptm"] is None for d in designs), (
            "the narration tells the reader a blank ipTM column is a "
            "monomer run, not a low score"
        )

        plddt = [d["mean_plddt"] for d in designs]
        ptm = [d["ptm"] for d in designs]
        assert min(plddt) == 74.16 and max(plddt) == 75.93
        assert round(max(plddt) - min(plddt), 2) == 1.77
        assert min(ptm) == 0.71 and max(ptm) == 0.73
        assert round(max(ptm) - min(ptm), 2) == 0.02

        came = example["what_came_back"]
        assert "74.16 to 75.93" in came
        assert "0.71 to 0.73" in came
        assert "1.77 pLDDT points" in came and "spans 0.02" in came

        read = example["how_to_read_it"]
        assert "sort order, not a" in read
        # This passage sends the reader to the ESMFold example holding a
        # threshold in mind. Both pages render 0-100 now, so it states one
        # number -- and must not reacquire a scale caveat that would only
        # be true if the normaliser were removed.
        assert "70" in read
        assert "0.70" not in read and "0 to 1" not in read

        # NOT derivable from the payload: the ten submitted sequences are
        # designs and are not published, so the identity range is a
        # capture-time fact. Pinned as PROSE, the way proteina's
        # non-derivable figures are, so a copy edit cannot drift it and
        # nobody goes looking for it in result.json.
        assert not list(_published_sequences(result)), (
            "af2's payload must stay sequence-free; if that changes, the "
            "identity range below should be derived rather than pinned"
        )
        assert "72% to 79%" in example["target"]
        assert "a fifth to a little over a quarter" in read, (
            "72-79% identity is 21-28% divergence; 28% is more than a "
            "quarter, so 'a fifth to a quarter' understates it"
        )

    def test_esmfold2_design_narration_matches_its_result_json(
        self, tools_app,
    ):
        """The top-scoring design is the rejected one. That inversion is
        the entire example, so it is asserted as a RELATION between the
        two rows rather than as two remembered numbers -- swap the
        payload for one where the best score also passes and this
        fails, as it should."""
        _, slugs = tools_app
        example = _examples(slugs)["esmfold2-design"]
        result = json.loads(
            (REPO / "tools" / "esmfold2_design" / "example" / "result.json")
            .read_text(encoding="utf-8"),
        )
        designs = result["designs"]
        assert len(designs) == 2
        assert result["target_name"] == "pd-l1"
        assert result["is_antibody"] is False

        best = max(designs, key=lambda d: d["iptm"])
        worst = min(designs, key=lambda d: d["iptm"])
        assert best["filter_status"] == "drop", (
            "the example teaches that the HIGHEST ipTM was rejected"
        )
        assert worst["filter_status"] == "strict_pass"
        assert best["isoelectric_point"] > 6 > worst["isoelectric_point"]

        assert round(best["iptm"], 3) == 0.956
        assert round(worst["iptm"], 3) == 0.935
        assert round(best["isoelectric_point"], 2) == 11.95
        assert round(worst["isoelectric_point"], 2) == 5.67
        # ipTM separates them by noise; pI separates them by a mile.
        assert round(best["iptm"] - worst["iptm"], 2) == 0.02

        came = example["what_came_back"]
        for figure in ("0.956", "0.935", "5.67", "11.95"):
            assert figure in came, figure
        read = example["how_to_read_it"]
        assert "The higher score is the one you must not order." in read
        assert "0.02 difference in ipTM is noise" in read

    def test_input_field_names_exist_on_the_form(self, tools_app):
        """``inputs_used`` names a form field and the value put in it, and
        it renders directly below that form. A name the form does not use
        sends the reader looking for a control that is not there.

        Caught four of these by hand on the predictors: the pages said
        "Mode / Single sequence", "Recycles" and "Use templates" where
        the forms say "Preset / Standalone, one FASTA", "Number of
        recycles" and "Use PDB templates" -- and one named a "Model"
        field that does not exist at all, because AF2 derives monomer vs
        multimer from the record rather than asking. Prose is the surface
        with no compiler, so it gets this instead.
        """
        app, slugs = tools_app

        # Match against the RENDERED <label> text, exactly. Scanning the
        # template SOURCE for a substring is the obvious version and it
        # is hollow three ways over: it matches Jinja comments, which are
        # stripped before anyone sees them; it matches CSS inside a
        # <style> block, so "grid-template-columns" passes; and being a
        # substring it accepts a truncation of a real label, which sends
        # the reader hunting for a control whose name does not quite
        # match. Four of those were live here.
        wrong = {}
        with app.test_client() as client:
            for slug, example in _examples(slugs).items():
                if not example:
                    continue
                html = client.get(f"/tools/{slug}").get_data(as_text=True)
                labels = _form_labels(html)
                assert labels, f"{slug}: no form labels rendered at all"
                for field, _value, _why in example["inputs_used"]:
                    if field not in labels:
                        near = [l for l in labels if l.startswith(field)]
                        wrong.setdefault(slug, []).append(
                            f"{field!r}" + (f" (label is {near[0]!r})"
                                            if near else " (no such label)")
                        )
        assert not wrong, (
            "worked-example input names that are not a form label, "
            f"exactly as rendered: {wrong}"
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


# ── The publishing rule ──────────────────────────────────────────────
#
# Written down here because a test enforcing an unwritten rule drifts
# the moment two people read it differently:
#
#   No CUSTOMER or CAMPAIGN designed sequence may reach a public page.
#
# Scores are always fine. So is the sequence of a PUBLISHED reference
# protein — that is what lets a reader check the example against the
# literature instead of taking our word for it. So is one of our own
# demo designs on a published reference backbone: there is no customer
# target behind it. Everything else is somebody's IP on an index,follow
# URL.
#
# Every sequence allowed to ship is named below with the reason it is
# allowed. A new one fails until someone writes its reason down, and
# that is the point: the question is not "does this look like a design"
# — the detector already answers that — it is "whose design is it",
# which only a human knows.
MIN_PUBLISHED_SEQUENCE = 25

PUBLISHABLE_SEQUENCES = {
    ("mpnn", "sequences[0].seq"): (
        "our own ProteinMPNN demo redesign of hen egg-white lysozyme, "
        "PDB 1HEW chain A: a published backbone, no customer target"
    ),
    ("mpnn", "sequences[1].seq"): (
        "second sample from the same 1HEW demo run"
    ),
    ("esmfold", "sequence"): (
        "human myelin basic protein, a published reference sequence -- it "
        "is what lets a reader check this example against the literature"
    ),
    ("esmfold", "pdb_b64"): (
        "the folded structure OF that published reference; the sequence it "
        "encodes is the cleared one above"
    ),
}

_AA1 = set("ACDEFGHIKLMNPQRSTVWY")
_AA3 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "MSE": "M",
}


def _ca_sequence(pdb_text: str) -> str:
    """The one-letter sequence a reader can recover from CA records."""
    return "".join(
        _AA3.get(line[17:20].strip(), "X")
        for line in pdb_text.splitlines()
        if line.startswith("ATOM") and line[12:16].strip() == "CA"
    )


def _sequence_in(value):
    """``(kind, sequence)`` if ``value`` publishes a protein sequence.

    TWO ways, because guarding only the first is how the earlier
    version of this check passed while a design shipped. A bare string
    is the obvious one. A base64 structure is the one that got through:
    dropping the ``sequence`` KEY and keeping ``pdb_b64`` leaves the
    whole sequence on the page, recoverable from the CA records by
    anyone who clicks Download PDB.
    """
    if not isinstance(value, str) or len(value) < MIN_PUBLISHED_SEQUENCE:
        return None
    if set(value.upper()) <= _AA1:
        return ("bare sequence", value)
    if len(value) >= 200:
        try:
            text = base64.b64decode(value, validate=True).decode(
                "utf-8", "replace"
            )
        except Exception:
            return None
        if "ATOM" in text:
            seq = _ca_sequence(text)
            if len(seq) >= MIN_PUBLISHED_SEQUENCE:
                return ("structure", seq)
    return None


def _published_sequences(obj, path=""):
    """Every publishable sequence in ``obj``, at any depth."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _published_sequences(val, child)
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            yield from _published_sequences(val, f"{path}[{i}]")
    else:
        found = _sequence_in(obj)
        if found:
            yield (path, *found)


def _example_payloads(slugs):
    """``{slug: payload}`` for every tool that actually ships one."""
    out = {}
    for slug, example in _examples(slugs).items():
        if not example:
            continue
        path = REPO / "tools" / slug.replace("-", "_") / "example"
        out[slug] = json.loads((path / "result.json").read_text("utf-8"))
    return out


class TestNoUnclearedSequenceReachesAPublicPage:
    """The publishing rule, enforced on VALUES rather than key names.

    The obvious shape for this guard is a list of the key names a
    designed sequence has been seen under. That is a proxy for the
    rule, not the rule: it cannot see a design stored under a key
    nobody thought of, and it cannot see one encoded inside a
    structure blob at all.
    """

    def test_every_published_sequence_has_been_cleared(self, tools_app):
        _app, slugs = tools_app
        uncleared = []
        for slug, payload in _example_payloads(slugs).items():
            for where, kind, seq in _published_sequences(payload):
                if (slug, where) not in PUBLISHABLE_SEQUENCES:
                    uncleared.append(
                        f"{slug}: {len(seq)} residues as a {kind} at "
                        f"{where} -> {seq[:40]}..."
                    )
        assert not uncleared, (
            "a sequence reaches a public page with no recorded reason.\n"
            + "\n".join(uncleared)
            + "\n\nIf it is a published reference or one of our own demo "
            "designs on a published backbone, add it to "
            "PUBLISHABLE_SEQUENCES with the reason. If it belongs to a "
            "customer or a campaign it must not ship: drop the key, and "
            "drop any structure blob that encodes it."
        )

    def test_the_detector_is_not_blind(self, tools_app):
        """Positive control. Every cleared entry must still be FOUND,
        otherwise the test above passes by detecting nothing at all —
        which is the failure mode it exists to replace."""
        _app, slugs = tools_app
        seen = set()
        for slug, payload in _example_payloads(slugs).items():
            for where, _kind, _seq in _published_sequences(payload):
                seen.add((slug, where))
        assert seen >= set(PUBLISHABLE_SEQUENCES), (
            "the detector no longer finds sequences it has cleared, so "
            "it would not find an uncleared one either. Missing: "
            f"{sorted(set(PUBLISHABLE_SEQUENCES) - seen)}"
        )

    def test_a_structure_blob_cannot_smuggle_a_sequence(self):
        """The leak a key-name check cannot see, in isolation."""
        pdb = "\n".join(
            f"ATOM  {i:5d}  CA  LEU A{i:4d}      0.000   0.000   0.000"
            for i in range(1, 41)
        )
        blob = base64.b64encode(pdb.encode()).decode()
        found = list(_published_sequences({"pdb_b64": blob}))
        assert found == [("pdb_b64", "structure", "L" * 40)]
