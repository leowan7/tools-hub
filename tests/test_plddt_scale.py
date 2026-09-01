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
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest

from shared.metric_glossary import PLDDT_COLUMNS, plddt_on_100

pytestmark = pytest.mark.usefixtures("isolate_supabase")


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

    def test_it_is_idempotent(self):
        """The property that lets a pipeline be fixed later without this
        double-scaling. An unconditional ``* 100`` -- which is what
        boltz2's template used to do -- does not have it."""
        for value in (0.39, 0.659, 1.0, 0.0, 75.93, 100.0):
            once = plddt_on_100(value)
            assert plddt_on_100(once) == pytest.approx(once)

    def test_junk_is_not_invented_into_a_number(self):
        assert plddt_on_100(None) is None
        assert plddt_on_100("nonsense") is None
        assert plddt_on_100(float("nan")) is None
        # Negative is broken data. It stays visibly broken rather than
        # being scaled into something that looks deliberate.
        assert plddt_on_100(-3.0) == pytest.approx(-3.0)

    def test_a_string_number_still_works(self):
        """Scores arrive from JSON and have been strings before now."""
        assert plddt_on_100("0.5") == pytest.approx(50.0)


class TestEveryRenderedPLDDTIsOnOneScale:
    """The regression itself, asserted on rendered HTML."""

    def _pages(self, flask_app, slugs):
        from shared.tool_meta import meta_for

        out = {}
        with flask_app.test_client() as client:
            for slug in slugs:
                if not getattr(meta_for(slug), "EXAMPLE", None):
                    continue
                resp = client.get("/tools/" + slug)
                assert resp.status_code == 200, slug
                out[slug] = resp.get_data(as_text=True)
        return out

    def test_no_tooltip_shows_a_zero_to_one_plddt(self, tools_app):
        flask_app, slugs = tools_app
        pages = self._pages(flask_app, slugs)
        assert pages, "no example pages rendered; this test would be vacuous"
        seen = 0
        for slug, html in pages.items():
            values = [float(v) for v in re.findall(r"pLDDT=([\d.]+)", html)]
            seen += len(values)
            low = [v for v in values if 0 < v <= 1]
            assert not low, (
                slug + " renders " + str(len(low)) + " per-residue pLDDT "
                "tooltips at or below 1.0 (e.g. " + str(low[:5]) + ") under "
                "a legend written for 0-100"
            )
        assert seen > 300, (
            "only " + str(seen) + " tooltips scanned; the esmfold example "
            "alone has 304, so the scan has gone blind"
        )

    def test_no_table_cell_shows_a_zero_to_one_plddt(self, tools_app):
        flask_app, slugs = tools_app
        pages = self._pages(flask_app, slugs)
        cols = "|".join(sorted(PLDDT_COLUMNS))
        pattern = 'data-col="(' + cols + ')"[^>]*data-val="([^"]+)"'
        seen = 0
        for slug, html in pages.items():
            for col, val in re.findall(pattern, html):
                try:
                    num = float(val)
                except ValueError:
                    continue
                seen += 1
                assert not 0 < num <= 1, (
                    slug + " renders " + col + "=" + val + ", a 0-1 pLDDT "
                    "in a column the legend reads on 0-100"
                )
        assert seen >= 20, "only " + str(seen) + " pLDDT cells scanned"

    def test_the_esmfold_example_shows_the_numbers_its_prose_quotes(
        self, tools_app,
    ):
        """The page the defect was found on. Its narration quotes 39.00
        and 65.9; without the normaliser the page says 0.39 and 0.7 while
        the prose keeps its figures."""
        flask_app, slugs = tools_app
        html = self._pages(flask_app, slugs)["esmfold"]
        values = [float(v) for v in re.findall(r"pLDDT=([\d.]+)", html)]
        assert len(values) == 304
        assert round(max(values), 1) == 65.9
        assert round(min(values), 1) == 21.5
        assert "39.00" in html


class TestTheDetectorCanFail:
    """Positive control. The scans above must be able to SEE a 0-1
    value, or their passing means nothing."""

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


def test_every_plddt_column_a_results_template_uses_is_registered():
    """PLDDT_COLUMNS drives the table's normalisation, so a column key a
    template renders but this set omits is silently unnormalised."""
    repo = pathlib.Path(__file__).resolve().parent.parent
    used = set()
    for path in (repo / "templates" / "tools").glob("*_results.html"):
        body = path.read_text(encoding="utf-8")
        for block in re.findall(r"columns\s*=\s*\[([^\]]*)\]", body):
            for token in re.findall(r"'([^']+)'", block):
                if "plddt" in token.lower():
                    used.add(token)
    assert used, "no pLDDT columns found in any results template"
    missing = used - set(PLDDT_COLUMNS)
    assert not missing, (
        "results templates render " + str(sorted(missing)) + " but "
        "PLDDT_COLUMNS does not list them, so those cells are never "
        "normalised"
    )


class TestTheCompletionEmailUsesTheSameScale:
    """The surface a template sweep misses.

    ``shared/email._result_summary`` quotes the mean into the job-
    completion email. It read ``mean_plddt`` raw, so an esmfold run
    mailed "mean pLDDT 0.4" while every threshold the reader has been
    given is on 0-100. Nothing on the page could correct it, because the
    email is the only thing some users read.
    """

    @staticmethod
    def _summary(mean_plddt):
        import types

        from shared.email import _result_summary

        job = types.SimpleNamespace()
        job.result = {"pdb_b64": "x", "mean_plddt": mean_plddt}
        job.status = "succeeded"
        job.tool = "esmfold"
        return _result_summary(job, tone="ok")

    def test_a_zero_to_one_mean_is_mailed_on_the_shared_scale(self):
        assert "mean pLDDT 39.0" in self._summary(0.39)

    def test_a_zero_to_one_hundred_mean_is_unchanged(self):
        assert "mean pLDDT 61.0" in self._summary(61.05)

    def test_the_old_wording_is_gone(self):
        """0.4 was what an esmfold run actually mailed."""
        assert "0.4)" not in self._summary(0.39)
