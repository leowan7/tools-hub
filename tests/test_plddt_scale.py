"""pLDDT reaches the templates on two scales; the page must show one.

esmfold and proteina store 0-1. af2, colabfold and pxdesign store 0-100.
boltz2 stores 0-1. opendde stores whichever of four upstream keys its
scorer wrote, so the SAME adapter yields either -- which is why the
normaliser keys off the VALUE and not the tool.

Every legend, threshold and tooltip on this site is written for 0-100:
``blue > 90 . green > 70 . amber > 50 . red <= 50``. Before this guard,
/tools/esmfold rendered a mean of 0.39 and per-residue tooltips of
0.2-0.7 under exactly that legend, so a real user could not tell a
catastrophic fold from a nearly-fine one. The spark bar's COLOURS were
already normalised, which is what kept it from being obvious: the bars
were right and the numbers beside them were not.

Normalising happens at DISPLAY, not in a pipeline, because the jobs
table already holds completed 0-1 runs that no pipeline change can
reach, and because a pipeline fix would then make those historical rows
render as catastrophic.

WHY THIS FILE IS SHAPED THE WAY IT IS. Its first version asserted
``"39.00" in html`` for the esmfold page. That string also occurs in the
page's own NARRATION, so restoring the exact original defect in the
summary panel left every test green while the panel showed 0.39 again.
A guard that a defect's own prose can satisfy is not a guard. Every
assertion below therefore anchors on the RENDERED ELEMENT, and the
negative form (``"0.39" not in html``) carries as much weight as the
positive one.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest

from shared.metric_glossary import PLDDT_COLUMNS, plddt_on_100

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REPO = pathlib.Path(__file__).resolve().parent.parent

# Tools whose stored payload is on 0-1, so their pages are the ones that
# would REGRESS. A scan that never reaches one of these is vacuous no
# matter how many values it counted on the tools that were always fine.
STORES_ZERO_TO_ONE = {"esmfold", "proteina", "boltz2"}


def _without_jinja_comments(body: str) -> str:
    """Template source with ``{# ... #}`` removed.

    A guard that reads raw source matches the comment describing the
    thing it forbids, which is how the first version of the boltz2 check
    failed on the sentence explaining why the code was removed.
    """
    return re.sub(r"\{#.*?#\}", "", body, flags=re.S)


def _stub_job(tool: str, scores: dict):
    """A real ToolJob, because these consumers read one -- not a mapping.

    ``_top_candidate_summary`` takes the first scored column that has a
    registered legend, so the scores dict here holds exactly the metric
    under test.
    """
    import uuid

    from shared.jobs import ToolJob

    return ToolJob.from_row({
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": tool,
        "preset": "pilot",
        "status": "succeeded",
        "inputs": {"target_chain": "A"},
        "result": {
            "tier": "pilot",
            "candidates": [
                {"design_name": "d0", "pdb_key": "d.pdb", "scores": scores},
            ],
        },
        "error": None,
        "modal_function_call_id": "fc-stub",
        "job_token": "t" * 64,
        "gpu_seconds_used": 10,
        "created_at": "2026-08-08T12:00:00Z",
        "started_at": "2026-08-08T12:00:01Z",
        "completed_at": "2026-08-08T12:30:00Z",
    })


@pytest.fixture(scope="module")
def tools_app():
    import app as app_module
    from shared.feature_flags import flag_name
    from tools import base as tool_base

    slugs = sorted(a.slug for a in tool_base.all_adapters())
    assert len(slugs) >= 14, f"adapter registry holds {len(slugs)} tools"
    prior = {}
    for slug in slugs:
        prior[flag_name(slug)] = os.environ.get(flag_name(slug))
        os.environ[flag_name(slug)] = "on"
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


@pytest.fixture(scope="module")
def example_pages(tools_app):
    from shared.tool_meta import meta_for

    flask_app, slugs = tools_app
    pages = {}
    with flask_app.test_client() as client:
        for slug in slugs:
            if not getattr(meta_for(slug), "EXAMPLE", None):
                continue
            resp = client.get("/tools/" + slug)
            assert resp.status_code == 200, slug
            pages[slug] = resp.get_data(as_text=True)
    assert pages, "no example pages rendered; every scan here would be vacuous"
    return pages


class TestTheNormaliser:
    def test_zero_to_one_is_scaled(self):
        assert plddt_on_100(0.39) == pytest.approx(39.0)
        assert plddt_on_100(0.659) == pytest.approx(65.9)
        assert plddt_on_100(1.0) == pytest.approx(100.0)
        assert plddt_on_100(0.0) == pytest.approx(0.0)

    def test_zero_to_one_hundred_passes_through(self):
        assert plddt_on_100(39.0) == pytest.approx(39.0)
        assert plddt_on_100(75.93) == pytest.approx(75.93)
        assert plddt_on_100(100.0) == pytest.approx(100.0)

    def test_one_application_to_either_scale_lands_on_0_100(self):
        """The property the callers actually need, and the only one
        claimed. A later pipeline fix that starts emitting 0-100 is safe
        because that value passes through."""
        for stored_0_1, stored_0_100 in ((0.39, 39.0), (0.659, 65.9)):
            assert plddt_on_100(stored_0_1) == pytest.approx(
                plddt_on_100(stored_0_100)
            )

    def test_it_is_NOT_idempotent_below_a_hundredth(self):
        """Pinned so nobody claims idempotency again -- an earlier
        docstring did, and boltz2's template was applying the rule twice
        on the strength of it. Harmless there only because boltz2's real
        range is 0.91-0.97; below 0.01 the second application scales a
        value that is already on 0-100."""
        assert plddt_on_100(0.005) == pytest.approx(0.5)
        assert plddt_on_100(plddt_on_100(0.005)) == pytest.approx(50.0)
        # ...and above that boundary a second application is a no-op,
        # which is why the bug hid.
        assert plddt_on_100(plddt_on_100(0.39)) == pytest.approx(39.0)

    def test_junk_returns_none_for_the_caller_to_render_as_missing(self):
        assert plddt_on_100(None) is None
        assert plddt_on_100("nonsense") is None
        assert plddt_on_100("") is None
        assert plddt_on_100(float("nan")) is None
        # Negative is broken data. It stays visibly broken rather than
        # being scaled into something that looks deliberate.
        assert plddt_on_100(-3.0) == pytest.approx(-3.0)

    def test_an_already_scaled_int_stays_an_int(self):
        """The email and the share card format ints and floats
        differently, so coercing here turned a stored ``88`` into
        "pLDDT 88.000". Scale is the only thing this changes."""
        assert plddt_on_100(88) == 88
        assert isinstance(plddt_on_100(88), int)
        assert isinstance(plddt_on_100(0.88), float)

    def test_a_string_number_still_works(self):
        """Scores arrive from JSON and have been strings before now."""
        assert plddt_on_100("0.5") == pytest.approx(50.0)


class TestEveryRenderedPLDDTIsOnOneScale:
    """The regression itself, asserted on rendered HTML."""

    def test_no_tooltip_shows_a_zero_to_one_plddt(self, example_pages):
        scanned = {}
        for slug, html in example_pages.items():
            values = [float(v) for v in re.findall(r"pLDDT=([\d.]+)", html)]
            scanned[slug] = len(values)
            low = [v for v in values if 0 < v <= 1]
            assert not low, (
                slug + " renders " + str(len(low)) + " per-residue pLDDT "
                "tooltips at or below 1.0 (e.g. " + str(low[:5]) + ") under "
                "a legend written for 0-100"
            )
        # Non-vacuity, keyed on the page that would actually regress
        # rather than on a total that colabfold's 101 always-fine values
        # could satisfy on their own.
        assert scanned.get("esmfold", 0) == 304, (
            "the esmfold strip is the only 0-1 tooltip source on the site; "
            "scanned " + str(scanned.get("esmfold", 0)) + " of 304"
        )

    def test_no_table_cell_shows_a_zero_to_one_plddt(self, example_pages):
        cols = "|".join(sorted(PLDDT_COLUMNS))
        pattern = 'data-col="(' + cols + ')"[^>]*data-val="([^"]+)"'
        per_tool = {}
        for slug, html in example_pages.items():
            for col, val in re.findall(pattern, html):
                try:
                    num = float(val)
                except ValueError:
                    continue
                per_tool[slug] = per_tool.get(slug, 0) + 1
                assert not 0 < num <= 1, (
                    slug + " renders " + col + "=" + val + ", a 0-1 pLDDT "
                    "in a column the legend reads on 0-100"
                )
        # The floor must be met by a tool that STORES 0-1. pxdesign alone
        # satisfied the old ``>= 20`` while contributing nothing that
        # could ever fail.
        regressable = {s: n for s, n in per_tool.items()
                       if s in STORES_ZERO_TO_ONE}
        assert regressable, (
            "no pLDDT cells scanned on any tool that stores 0-1 "
            "(saw: " + str(per_tool) + "); this scan cannot fail"
        )
        assert sum(regressable.values()) >= 60, regressable


class TestTheEsmfoldPanelSpecifically:
    """The page the defect was found on, asserted on the ELEMENT.

    ``"39.00" in html`` was the original assertion and it is satisfied by
    the narration alone, so the defect could be restored with the suite
    green. These anchor on the rendered panel and on the absence of the
    old value.
    """

    def test_the_summary_panel_renders_the_normalised_mean(
        self, example_pages,
    ):
        html = example_pages["esmfold"]
        panel = re.search(
            r"mean pLDDT</div>\s*<div[^>]*>\s*([0-9.]+)", html
        )
        assert panel, "esmfold's mean pLDDT panel did not render at all"
        assert panel.group(1) == "39.00", (
            "the panel shows " + panel.group(1) + "; the payload stores "
            "0.39 and the page must show it on the 0-100 scale its own "
            "legend uses"
        )

    def test_the_raw_zero_to_one_mean_appears_nowhere_on_the_page(
        self, example_pages,
    ):
        """The negative form, which is what actually catches a revert."""
        html = example_pages["esmfold"]
        assert "0.39" not in html, (
            "the 0-1 mean is still rendered somewhere on the page"
        )

    def test_the_tooltips_span_the_normalised_range(self, example_pages):
        values = [
            float(v)
            for v in re.findall(r"pLDDT=([\d.]+)", example_pages["esmfold"])
        ]
        assert len(values) == 304
        assert round(max(values), 1) == 65.9
        assert round(min(values), 1) == 21.5


class TestPrecisionDidNotChange:
    """A scale fix does not get to change precision as a side effect.

    Widening the table's FORMAT branch to every pLDDT column moved
    ``mean_pLDDT`` from two decimals to one, which rewrote af2's table
    (75.93 -> 75.9) and falsified the sentence on that page quoting a
    1.77 spread between top and bottom row.
    """

    def test_af2_batch_table_keeps_two_decimals(self, example_pages):
        cells = re.findall(
            r'data-col="mean_pLDDT"[^>]*>([0-9.]+)<',
            example_pages["af2"],
        )
        assert cells, "af2's mean_pLDDT column did not render"
        assert "75.93" in cells, cells[:5]

    def test_the_af2_narration_still_matches_its_table(self, example_pages):
        html = example_pages["af2"]
        assert "74.16 to 75.93" in html
        assert "1.77 pLDDT points" in html


class TestNoSiteAppliesTheRuleTwice:
    def test_boltz2_renders_exactly_one_application(self, example_pages):
        """boltz2's template normalised and then handed the result to
        candidate_table, which normalised the 'pLDDT' key again."""
        import json

        payload = json.loads(
            (REPO / "tools" / "boltz2" / "example" / "result.json")
            .read_text(encoding="utf-8")
        )
        raw = [
            d["complex_plddt"] for d in payload["designs"]
            if d.get("complex_plddt") is not None
        ]
        assert raw, "boltz2's example carries no complex_plddt"
        expected = {round(plddt_on_100(v), 2) for v in raw}
        rendered = {
            round(float(v), 2)
            for _c, v in re.findall(
                r'data-col="(pLDDT)"[^>]*data-val="([^"]+)"',
                example_pages["boltz2"],
            )
        }
        assert rendered <= expected, (
            "boltz2 renders values that are not one application of the "
            "rule to its payload: " + str(sorted(rendered - expected))
        )

    def test_boltz2_leaves_the_scaling_to_the_table(self):
        """The assertion above CANNOT catch a re-added double
        application: boltz2's payload runs 0.91-0.97, one application
        lands on 91-97, and a second is a no-op above 1.0. The hazard is
        real but invisible in this data, so it is guarded structurally --
        candidate_table normalises the 'pLDDT' key, therefore the
        template must hand it the raw value and not pre-scale it."""
        # Strip {# ... #} first. Matching raw source made this fail on
        # the COMMENT explaining the fix, which is the same defect shape
        # as guarding template source instead of rendered output.
        body = _without_jinja_comments(
            (REPO / "templates/tools/boltz2_results.html")
            .read_text(encoding="utf-8")
        )
        assert "plddt_on_100" not in body, (
            "boltz2_results.html normalises complex_plddt AND stores it "
            "under 'pLDDT', which candidate_table normalises again"
        )
        assert "_plddt * 100" not in body, (
            "the old unconditional scaling is back; it renders 9609 for a "
            "payload that already arrives on 0-100"
        )


class TestAnUnparseablePLDDTDoesNotCrashThePage:
    """``plddt_on_100`` returns None for junk and formatting None raises.

    The ``raw is none`` guard above the branch catches nulls but not ""
    or "n/a", so a scorer placeholder in a pLDDT-keyed score was a 500
    where the old ``raw | float`` quietly showed 0.00 -- a fake number,
    but not an outage. This is the regression the round-1 fix closed and
    the one thing that went in without a test.
    """

    @staticmethod
    def _render(flask_app, value):
        tmpl = flask_app.jinja_env.from_string(
            "{% from 'components/candidate_table.html' import"
            " candidate_table %}"
            "{{ candidate_table(cands, ['pLDDT'], 'example', 'esmfold') }}"
        )
        # A request context: the macro builds URLs with url_for.
        with flask_app.test_request_context("/"):
            return tmpl.render(
                cands=[{"rank": 1, "scores": {"pLDDT": value}}]
            )

    @pytest.mark.parametrize("junk", ["", "n/a", "TBD", [], {}])
    def test_junk_renders_as_missing_rather_than_raising(
        self, tools_app, junk,
    ):
        flask_app, _slugs = tools_app
        html = self._render(flask_app, junk)
        assert 'data-col="pLDDT"' in html
        assert "—" in html, html[:400]

    def test_a_real_value_still_renders_beside_it(self, tools_app):
        """Non-vacuity: the harness must be able to show a number, or
        the test above passes on a macro that renders nothing."""
        flask_app, _slugs = tools_app
        html = self._render(flask_app, 0.88)
        assert "88.0" in html, html[:400]


def test_no_glossary_range_points_at_other_text_on_the_page():
    """A ``good_range`` has to stand on its own wherever it is printed.

    ``candidate_table.html`` appends this string to the metric tooltip
    only when the tool states no bar of its own -- precisely the case
    where there is nothing else in the tooltip to point AT. ipTM's read
    "0.65 to 0.75 depending on the tool; the bar that applies is quoted
    above", which is true on pxdesign (its legend names 0.75) and
    dangling on af2, colabfold, rfantibody and proteina, all of which
    render an ipTM column with no ipTM legend. Four live public pages
    told the reader to look above at nothing.

    The glossary is metric-scoped and a bar is tool-scoped; a sentence
    here cannot assume what a per-tool legend happens to say beside it.
    """
    from shared.metric_glossary import GLOSSARY

    deictic = ("above", "below this", "quoted", "beside", "shown here")
    offenders = {
        metric: [w for w in deictic if w in (entry.get("good_range") or "").lower()]
        for metric, entry in GLOSSARY.items()
    }
    offenders = {m: w for m, w in offenders.items() if w}
    assert not offenders, (
        "these good_range strings refer to text that may not be there: "
        f"{offenders}. State the range on its own terms."
    )


def test_every_plddt_key_the_exporter_can_meet_is_registered():
    """PLDDT_COLUMNS has TWO jobs and the registry guard only checks one.

    ``shared/exports.candidates_to_csv`` matches the set against raw
    PAYLOAD keys, while the registry guard checks DISPLAY column keys.
    ``complex_plddt`` is never a display column -- boltz2 remaps it to
    'pLDDT' -- so deleting it from the set left every test green while
    silently reverting the boltz2 CSV to two scales in one row.
    """
    import json

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                yield key, val
                yield from walk(val)
        elif isinstance(node, list):
            for val in node:
                yield from walk(val)

    scalar_keys = set()
    for path in (REPO / "tools").glob("*/example/result.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key, val in walk(payload):
            # Scalars only. ``plddt_per_residue`` is an array and never
            # becomes a CSV column (``_is_metric_value`` drops lists), so
            # registering it would be wrong.
            if "plddt" in key.lower() and isinstance(val, (int, float)):
                scalar_keys.add(key)

    assert scalar_keys, "no example payload carries a scalar pLDDT"

    # ...and example payloads are the WRONG source on their own. Six
    # tools ship no example, and more importantly the CSV's root keys are
    # not written by a tool at all -- webhooks/modal.py persists a fixed
    # dict for every composite pipeline. Deriving from it covers all
    # fourteen. This is what catches ``plddt``, which no example payload
    # carries as a scalar root key and which is not a display column
    # either, so both earlier guards were blind to it.
    persisted = set()
    tree = ast.parse((REPO / "webhooks" / "modal.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if (isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and "plddt" in key.value.lower()):
                    persisted.add(key.value)
    assert persisted, "the webhook writer scan found no pLDDT key"
    scalar_keys |= persisted
    missing = scalar_keys - set(PLDDT_COLUMNS)
    assert not missing, (
        "these pLDDT keys exist in a real payload but are not in "
        "PLDDT_COLUMNS, so the CSV export leaves them on the stored "
        "scale while the page shows 0-100: " + str(sorted(missing))
    )


class TestTheDetectorCanFail:
    """Positive controls. The scans must be able to SEE a 0-1 value."""

    def test_a_zero_to_one_tooltip_would_be_caught(self):
        page = '<span title="residue 1: pLDDT=0.7"></span>'
        values = [float(v) for v in re.findall(r"pLDDT=([\d.]+)", page)]
        assert [v for v in values if 0 < v <= 1] == [0.7]

    def test_a_zero_to_one_cell_would_be_caught(self):
        cols = "|".join(sorted(PLDDT_COLUMNS))
        pattern = 'data-col="(' + cols + ')"[^>]*data-val="([^"]+)"'
        page = '<td data-col="af2_plddt" data-val="0.885">0.9</td>'
        found = re.findall(pattern, page)
        assert found == [("af2_plddt", "0.885")]
        assert 0 < float(found[0][1]) <= 1

    def test_the_panel_regex_would_see_an_un_normalised_mean(self):
        page = '<div>mean pLDDT</div>\n<div class="x">  0.39\n</div>'
        found = re.search(r"mean pLDDT</div>\s*<div[^>]*>\s*([0-9.]+)", page)
        assert found and found.group(1) == "0.39"


class TestTheOtherSurfaces:
    """Everything that shows a pLDDT and is not a tool results page.

    These were found one at a time, each after the previous sweep was
    called complete: the completion email, then the second caption in the
    same file, then the CSV export, then the public share card.
    """

    def test_the_completion_email_uses_the_shared_scale(self):
        import types

        from shared.email import _result_summary

        job = types.SimpleNamespace()
        job.result = {"pdb_b64": "x", "mean_plddt": 0.39}
        job.status = "succeeded"
        job.tool = "esmfold"
        summary = _result_summary(job, tone="ok")
        assert "mean pLDDT 39.0" in summary
        assert "0.4)" not in summary

    def test_the_top_candidate_email_caption_uses_the_shared_scale(self):
        """This one quotes the 80/90 band in the very next sentence, so
        it mailed 'pLDDT 0.830' directly above 'Above 80 is confidently
        folded'."""
        from shared.email import _top_candidate_summary

        label, value, caption, _pdb = _top_candidate_summary(
            job=_stub_job("boltzgen", {"pLDDT": 0.83}), tone="success",
        )
        assert label == "pLDDT"
        assert value == "83.000", value
        # The caption underneath is the reason this matters: it quotes the
        # band, so the number above it has to be on the same scale. The NUMBER
        # is what this test is about -- it asserted the phrase "Above 80" and
        # went red when the legends were reworded to "80 or more", which is
        # the same band said the way `judge` actually compares it.
        assert "80" in caption and "0.8" not in caption, caption

    def test_the_csv_export_matches_the_page(self):
        """Read the table, download the CSV, filter > 70 -- that returned
        an empty file for every tool storing 0-1, with no hint why."""
        from shared.exports import candidates_to_csv

        csv_text = candidates_to_csv(
            [{"rank": 1, "scores": {"af2_plddt": 0.885, "ipTM": 0.89}}]
        )
        assert "88.5" in csv_text, csv_text
        assert "0.885" not in csv_text, csv_text
        # a metric that is NOT a pLDDT must be untouched
        assert "0.89" in csv_text, csv_text

    def test_the_csv_export_uses_the_pages_precision(self):
        """0.885 multiplies cleanly and hides the defect. 0.8624 does
        not: unrounded it exports 86.24000000000001 for a cell the page
        shows as 86.24, so the fix for a 100x disagreement opened a
        1e-14 one and a ragged column in Excel."""
        from shared.exports import candidates_to_csv

        csv_text = candidates_to_csv(
            [{"rank": 1, "scores": {"af2_plddt": 0.8624}}]
        )
        assert "86.24" in csv_text, csv_text
        assert "86.24000000000001" not in csv_text, csv_text

    def test_the_public_share_card_uses_the_shared_scale(self):
        from blueprints.jobs import _top_score_for_share

        assert _top_score_for_share(
            _stub_job("esmfold", {"pLDDT": 0.39})
        ) == "pLDDT 39.000"


class TestEveryKnownDisplaySiteStillCallsTheRule:
    """A text-level deletion guard.

    Three sites cannot be reached by the rendered-page scans above -- the
    cross-tool compare table and job_detail's live table both need job
    fixtures, and job_detail's is built in the browser. Un-normalising
    any of them left the whole suite green. This is a weaker guard than a
    render, and it is here because a weak guard on a real site beats a
    strong guard on a site that cannot regress.
    """

    # (needle, how many times it must appear). COUNTS, because a bare
    # presence check is satisfied by a decoy in the same file: af2 calls
    # the rule three times and colabfold twice, so removing one call
    # left the needle matching and the guard green.
    SITES = {
        "templates/jobs_compare.html": ("plddt_on_100(scores.get(", 1),
        "templates/job_detail.html": ("plddtOn100(c.plddt)", 1),
        "templates/components/candidate_table.html": ("plddt_on_100(raw)", 1),
        "templates/tools/esmfold_results.html": ("plddt_on_100(", 3),
        "templates/tools/colabfold_results.html": ("plddt_on_100(", 3),
        # mean, min and max, none of which any render scan reaches:
        # af2's example is batch-shaped and carries no plddt_per_residue.
        "templates/tools/af2_results.html": ("plddt_on_100(", 3),
    }

    def test_each_site_applies_it(self):
        missing = []
        for path, (needle, expected) in self.SITES.items():
            # Comments stripped: a note *about* the call would satisfy
            # a raw-source match while the call itself was gone.
            body = _without_jinja_comments(
                (REPO / path).read_text(encoding="utf-8")
            )
            seen = body.count(needle)
            if seen != expected:
                missing.append(
                    path + ": " + needle + " appears " + str(seen)
                    + "x, expected " + str(expected)
                )
        assert not missing, "pLDDT display sites drifted: " + str(missing)

    def test_the_js_mirror_still_defines_the_rule(self):
        """job_detail builds its live table in the browser, so the jinja
        global cannot reach it and the rule is hand-copied there."""
        body = (REPO / "templates/job_detail.html").read_text(encoding="utf-8")
        assert "function plddtOn100" in body
        assert "n <= 1" in body and "n * 100" in body


def test_every_plddt_column_any_registry_uses_is_registered():
    """PLDDT_COLUMNS drives normalisation, so a column key rendered
    somewhere but missing from the set is silently unnormalised.

    Scans BOTH registries. The first version globbed only
    templates/tools/*_results.html with a regex that stopped at the first
    ``]``, so it saw nothing at all in opendde_results.html -- which
    builds its columns by concatenation, and which is the one tool the
    whole value-keyed design rests on.
    """
    used = set()
    from_templates = set()

    for path in (REPO / "templates" / "tools").glob("*_results.html"):
        body = path.read_text(encoding="utf-8")
        for block in re.findall(r"columns\s*=\s*(.+?)%\}", body, re.S):
            for token in re.findall(r"'([^']+)'", block):
                if "plddt" in token.lower():
                    from_templates.add(token)
    used |= from_templates

    # ast, not a regex. The regex here was
    # r"'([^']+)'|\"([^\"]+)\"" and it found NOTHING in this file: the
    # module opens with a triple-quoted docstring, so the first \" of
    # \"\"\" starts a match that swallows the docstring body and
    # desynchronises quote pairing for everything after it. What it
    # actually captured were the separators -- "': ['" and "', '".
    from_columns_py = set()
    tree = ast.parse((REPO / "shared" / "result_columns.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "plddt" in node.value.lower():
                from_columns_py.add(node.value)
    used |= from_columns_py

    # PER-SOURCE floors. A single combined assertion was satisfied by the
    # template half alone, so the Python half sat inert while the
    # docstring claimed both -- the same shape as the defect this guard
    # was widened to close.
    assert from_templates, "the results-template scan found no pLDDT column"
    assert from_columns_py, "the result_columns.py scan found no pLDDT column"
    missing = used - set(PLDDT_COLUMNS)
    assert not missing, (
        "these pLDDT column keys are rendered but not in PLDDT_COLUMNS, "
        "so those cells are never normalised: " + str(sorted(missing))
    )
