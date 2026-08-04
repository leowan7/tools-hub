"""The results action bar is SHARED, so Phase 5.2's button hierarchy landed on
every page that renders it, not only on the target page it was designed for.

REGISTER ITEM B-11. ``components/candidate_table.html`` and
``components/results_shell.html`` are imported by 14 pages: ``runs/detail.html``
(the compute-campaign results screen) and 13 ``tools/*_results.html`` pages.
Nothing parsed those action bars, so a later edit could reintroduce a second
primary CTA on all of them at once and the suite would stay green.

Both invariants below are claims the templates make about themselves, in
comments, in the imperative:

  "DOWNLOAD IS THE PRIMARY OUTCOME, and there is exactly one btn-primary in
   this bar."                                        candidate_table.html

  "It used to open with 'Take it further' and carry a `btn-primary`, which
   made it the loudest control on a page whose primary outcome is the
   download ... Both links are secondary now."       results_shell.html

Under this repo's house rule a comment is a claim, so each one needs something
that fails when it stops being true.
"""

from __future__ import annotations

import pathlib
from html.parser import HTMLParser

import pytest
from jinja2 import Environment, FileSystemLoader

from shared import metric_glossary, ranking, score_legends

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "templates"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    env.globals["metric_glossary"] = metric_glossary.GLOSSARY
    env.globals["score_legends_for"] = score_legends.score_legends_for
    env.globals["format_metric_value"] = metric_glossary.format_value
    env.globals["score_legend_for"] = score_legends.get_legend
    env.globals["ordinal"] = ranking.ordinal
    env.globals["csrf_input"] = lambda: ""
    env.globals["url_for"] = lambda _e, **kw: "/static/" + kw.get("filename", "")
    return env


class _Subtree(HTMLParser):
    """Collect the buttons and links inside the first element carrying
    ``wanted`` in its class, tracking div depth so the modal further down the
    macro (which legitimately has its own primary) is never counted."""

    def __init__(self, wanted: str):
        super().__init__(convert_charrefs=True)
        self.wanted = wanted
        self.depth = 0
        self.inside = False
        self._text: list[str] = []
        self.controls: list[tuple[str, str]] = []   # (classes, label)
        self._open: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = a.get("class") or ""
        if tag == "div":
            if not self.inside and self.wanted in classes.split():
                self.inside = True
                self.depth = 0
            elif self.inside:
                self.depth += 1
        if self.inside and tag in ("a", "button"):
            self._open = classes.split()
            self._text = []

    def handle_data(self, data):
        if self._open is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if self.inside and tag in ("a", "button") and self._open is not None:
            self.controls.append((" ".join(self._open),
                                  " ".join("".join(self._text).split())))
            self._open = None
        elif tag == "div" and self.inside:
            if self.depth == 0:
                self.inside = False
            else:
                self.depth -= 1


def _controls(html: str, wanted: str = "cand-action-bar"):
    p = _Subtree(wanted)
    p.feed(html)
    return p.controls


def _primaries(controls):
    return [label for classes, label in controls if "btn-primary" in classes.split()]


def _row():
    return {
        "scores": {"ipTM": 0.9}, "pdb_key": "designs/d.pdb", "sequence": "MKTAY",
        "_source_tool": "bindcraft", "_source_job_id": "job-1",
        "_source_index": 0, "_metric_key": "ipTM", "_metric_value": 0.9,
        "_rank_percentile": 90, "_ranked": True, "_rank_position": 1,
    }


def _render_table(**kw):
    params = dict(
        candidates=[_row()], columns=["ipTM"], job_id="job-1",
        tool_slug="bindcraft", clone_url="", campaign_id="", target_id="",
        multi_tool=False, sort_mode="", split_tools=(), per_tool={},
    )
    params.update(kw)
    tmpl = _env().from_string(
        '{% from "components/candidate_table.html" import candidate_table %}'
        "{{ candidate_table(candidates, columns, job_id, tool_slug, clone_url,"
        "                   campaign_id, target_id, multi_tool, sort_mode,"
        "                   split_tools, per_tool) }}"
    )
    return tmpl.render(**params)


# The three shapes this macro renders, named for the page each one IS. Only the
# last was in Phase 5.2's scope; the other two are the escape.
_MODES = [
    ("single job (13 tools/*_results.html pages)", {}),
    ("compute campaign (runs/detail.html)", {"campaign_id": "c-1"}),
    ("design target (targets/detail.html)", {"target_id": "t-1"}),
]


@pytest.mark.parametrize("name,kw", _MODES, ids=[m[0] for m in _MODES])
def test_the_action_bar_has_exactly_one_primary_in_every_mode(name, kw):
    controls = _controls(_render_table(**kw))
    assert controls, f"{name}: no action bar parsed at all"
    primaries = _primaries(controls)
    assert len(primaries) == 1, (name, primaries, controls)


@pytest.mark.parametrize("name,kw", _MODES, ids=[m[0] for m in _MODES])
def test_the_one_primary_is_the_download_not_the_sales_handoff(name, kw):
    """WHICH control is primary is the whole point of 5.2. It used to be "Send
    shortlist to Ranomics lab", which framed the CRO handoff as the goal of the
    page rather than as one option after the download. Counting primaries alone
    would stay green if the primary simply moved back."""
    primaries = _primaries(_controls(_render_table(**kw)))
    assert primaries == ["Download CSV"], (name, primaries)


def test_the_shortlist_and_export_controls_are_all_secondary():
    """The pair for the count test: a bar whose every control is primary has
    exactly one primary only by accident of counting, so name the rest."""
    controls = _controls(_render_table(target_id="t-1"))
    labels = {label: classes for classes, label in controls}
    for label in ("FASTA", "PDBs (ZIP)", "Starred only (CSV)",
                  "Send shortlist to Ranomics lab"):
        assert label in labels, (label, sorted(labels))
        assert "btn-secondary" in labels[label].split(), (label, labels[label])


def test_the_wet_lab_panel_carries_no_primary_button():
    """`results_shell.html`'s outbound sales panel. Two of a results page's
    three loudest buttons pointed at ranomics.com before 5.2 demoted these.

    Parsed from the whole rendered panel rather than a subtree, because the
    claim is about the macro's own markup and it has exactly one primary to
    lose: the table's Download CSV.
    """
    tmpl = _env().from_string(
        '{% from "components/results_shell.html" import results_panel %}'
        '{{ results_panel(candidates, ["ipTM"], "bindcraft", "job-1") }}'
    )
    html = tmpl.render(candidates=[_row()])
    assert "Validating in the lab is optional" in html, "panel did not render"
    assert "Binder Pilot" in html and "AI Binder Sprint" in html
    for link in ("Binder Pilot", "AI Binder Sprint"):
        idx = html.index(">" + link + "<")
        tag_start = html.rindex("<a ", 0, idx)
        assert "btn-secondary" in html[tag_start:idx], link
        assert "btn-primary" not in html[tag_start:idx], link


def test_the_blast_radius_this_file_claims_is_real():
    """The docstring above says 14 pages. If that stops being true the reason
    for the parametrisation stops being true with it.

    A lower bound, not an equality: adding a fifteenth tool only widens the
    surface these invariants protect, and failing a build for that would be
    noise.
    """
    consumers = sorted(
        p.relative_to(_TEMPLATES).as_posix()
        for p in _TEMPLATES.rglob("*.html")
        if "import results_panel" in p.read_text(encoding="utf-8")
    )
    assert len(consumers) >= 14, consumers
    assert "runs/detail.html" in consumers, consumers
    assert sum(1 for c in consumers if c.startswith("tools/")) >= 13, consumers
