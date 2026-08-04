"""The contract between static/js/candidate_table.js, the macro that renders
its DOM, and the server that parses what it posts.

REGISTER ITEM B-3. There is no JS test harness in this repo, so nothing
executes that file. Every identifier below crosses a boundary a rename can
break on one side only, and four such renames were confirmed to survive the
entire suite: `.cand-starred-export`, the submit listener, the posted key
shape, and `shortlist-hint-`. Each one shipped an empty CSV named `_starred`
at HTTP 200 with no error anywhere.

ROUND 20. The template half of every hook was a substring search over the
template SOURCE, and four of the thirteen hooks turned out to be held up by
something that is not the hook:

  `data-job`         by `data-job-id="{{ job_id }}"` on the action-bar div.
                     Renaming the star button's attribute passed the whole
                     suite while `starRef()` posted {"job_id": undefined} for
                     every design -- a header-only CSV at HTTP 200, which is
                     precisely the failure this file exists to catch.
  `star-btn`         by the `.star-btn { }` CSS rule in the macro's own
                     <style> block.
  `shortlist-review` by its CSS rule, with no backstop anywhere.
  `data-col`         by template COMMENTS, with no backstop anywhere.

So the template half now asserts on RENDERED HTML, parsed into elements. Jinja
strips its own comments before anything is emitted, html.parser treats <style>
and <script> as CDATA so no rule inside them can become an element, and an
attribute NAME comparison cannot be satisfied by a longer attribute that merely
starts with it. The macro renders under a bare Jinja environment, so the
artifact this repo's house rule asks for IS available here and the source-level
excuse only ever applied to the JS.

The JS half stays a source search -- there is still no runtime -- but the
tokens are anchored so a prefix cannot stand in for the whole: `dataset.job` is
a prefix of `dataset.jobId`, which is the same superstring hole from the other
side. Where a real artifact is reachable these tests use it: the ref shape is
not string-compared, it is extracted from the JS and driven through the
production parser, and the empty-selection case is asserted on a live response
in tests/test_target_export.py.

Adding a hook to the macro does not require adding it here. Adding one the JS
*reads* does. Deliberately NOT listed, because nothing reads them across THIS
boundary: `data-campaign-id` (emitted by the macro, and the JS header comment
says it "drives the modal payload", but no `dataset.campaignId` read exists);
`window.getShortlist` (exposed, called by nothing in templates/); the
`.starred` class (set by the JS, styled by the macro, never emitted by it); and
`window.initMolViewer` / `initMolViewerFromUrl`, which are a JS-to-JS contract
with mol_viewer.js rather than a hook in this macro's DOM.
"""

import pathlib
import re
from html.parser import HTMLParser

import pytest
from jinja2 import Environment, FileSystemLoader

from shared import metric_glossary, ranking, score_legends

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_JS = (_ROOT / "static" / "js" / "candidate_table.js").read_text(encoding="utf-8")
_TEMPLATES = _ROOT / "templates"


# ---------------------------------------------------------------------------
# The rendered artifact
# ---------------------------------------------------------------------------

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


def _rows():
    """Two candidates, chosen so both PDB branches render.

    The macro emits `data-pdb-url` for a row carrying `pdb_key` and
    `data-pdb64` for the legacy inline-base64 row that has none, and the JS
    reads both. One row can only ever exercise one of them.
    """
    return [
        {"scores": {"ipTM": 0.91}, "pdb_key": "designs/d1.pdb",
         "_source_tool": "bindcraft", "_source_job_id": "job-1",
         "_source_index": 0, "_rank_position": 1},
        {"scores": {"ipTM": 0.88}, "pdb_content_b64": "QVRPTQ==",
         "_source_tool": "bindcraft", "_source_job_id": "job-1",
         "_source_index": 1},
    ]


def _render(**kw) -> str:
    params = dict(
        candidates=_rows(), columns=["ipTM"], job_id="job-1",
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


class _Elements(HTMLParser):
    """Every rendered element as ``(tag, attrs)``.

    html.parser puts <style> and <script> into CDATA mode, so nothing inside
    them is ever reported as an element. That is what structurally retires the
    CSS rules that were propping up `.star-btn` and `.shortlist-review`; Jinja
    had already discarded the comments that were propping up `data-col`.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def _elements(html: str) -> list:
    p = _Elements()
    p.feed(html)
    return p.elements


# The three shapes this shared macro renders, named for the pages they ARE.
_MODES = {
    "job": {},
    "campaign": {"campaign_id": "c-1"},
    "target": {"target_id": "t-1"},
}
_ALL = ("job", "campaign", "target")

_DOM = {name: _elements(_render(**kw)) for name, kw in _MODES.items()}


def _el(*, tag=None, cls=None, attr=None, name=None, id_prefix=None, calls=None):
    """Rendered elements satisfying EVERY criterion given, on one element.

    ``attr`` compares an attribute NAME for equality against the parsed
    attribute dict, so `data-job-id` can no longer answer for `data-job`.
    ``id_prefix`` additionally requires a non-empty suffix, because the JS
    concatenates a scope onto it and a bare prefix would resolve to nothing.
    """
    def check(elements):
        out = []
        for tag_name, a in elements:
            if tag is not None and tag_name != tag:
                continue
            if cls is not None and cls not in (a.get("class") or "").split():
                continue
            if attr is not None and attr not in a:
                continue
            if name is not None and a.get("name") != name:
                continue
            if id_prefix is not None:
                el_id = a.get("id") or ""
                if not el_id.startswith(id_prefix) or el_id == id_prefix:
                    continue
            if calls is not None and not any(
                k.startswith("on") and re.search(rf"\b{calls}\s*\(", v or "")
                for k, v in a.items()
            ):
                continue
            out.append((tag_name, a))
        return out
    return check


# (token as the JS EXECUTES it, what the macro must EMIT, modes, DOM check).
#
# The JS side is a REGEX and deliberately the executable form -- `dataset.refIdx`,
# not the `data-ref-idx` its header comment also mentions. A comment match keeps
# a broken rename green, which is the failure mode this file exists to catch,
# and `\b` is what stops `dataset.job` matching the `dataset.jobId` two lines
# above it.
_HOOKS = [
    (r"'\.cand-starred-export'", "form.cand-starred-export", ("target",),
     _el(tag="form", cls="cand-starred-export")),
    (r"form\.dataset\.scope\b", "data-scope on that form", ("target",),
     _el(cls="cand-starred-export", attr="data-scope")),
    (r'\[name="refs"\]', 'the hidden input name="refs"', ("target",),
     _el(tag="input", name="refs")),

    (r"'\[data-cand-table-id\]'", "data-cand-table-id on the action bar",
     _ALL, _el(cls="cand-action-bar", attr="data-cand-table-id")),
    (r"dataset\.candTableId\b", "data-cand-table-id, read as a dataset key",
     _ALL, _el(attr="data-cand-table-id")),
    (r"wrapEl\.dataset\.scope\b", "data-scope on the action bar", _ALL,
     _el(cls="cand-action-bar", attr="data-scope")),
    (r"dataset\.jobId\b", "data-job-id, the scope fallback", _ALL,
     _el(cls="cand-action-bar", attr="data-job-id")),

    (r"'\.star-btn'", "button.star-btn", _ALL,
     _el(tag="button", cls="star-btn")),
    (r"dataset\.job\b", "data-job on the star button", _ALL,
     _el(cls="star-btn", attr="data-job")),
    (r"dataset\.refIdx\b", "data-ref-idx on the star button", _ALL,
     _el(cls="star-btn", attr="data-ref-idx")),
    (r"dataset\.idx\b", "data-idx on the 3D button", _ALL,
     _el(cls="view3d-btn", attr="data-idx")),

    (r"'shortlist-count-'", "id=shortlist-count-<scope>", _ALL,
     _el(id_prefix="shortlist-count-")),
    (r"'shortlist-hint-'", "id=shortlist-hint-<scope>", _ALL,
     _el(id_prefix="shortlist-hint-")),

    (r"'\.view3d-btn'", "button.view3d-btn", _ALL,
     _el(tag="button", cls="view3d-btn")),
    (r"'viewer-row-'", "id=viewer-row-<idx>", _ALL,
     _el(tag="tr", id_prefix="viewer-row-")),
    (r"'mol-viewer-'", "id=mol-viewer-<idx>", _ALL,
     _el(id_prefix="mol-viewer-")),
    (r"dataset\.pdbUrl\b", "data-pdb-url on the 3D button", _ALL,
     _el(cls="view3d-btn", attr="data-pdb-url")),
    (r"dataset\.pdb64\b", "data-pdb64 on the legacy inline 3D button", _ALL,
     _el(cls="view3d-btn", attr="data-pdb64")),

    (r"'th\[data-col\]'", "data-col on a sortable header", _ALL,
     _el(tag="th", attr="data-col")),
    (r"'\[data-col=\"'", "data-col on the value cells sortTable reads", _ALL,
     _el(tag="td", attr="data-col")),
    (r"dataset\.val\b", "data-val on those same cells", _ALL,
     _el(tag="td", attr="data-val")),
    # Read with classList.contains, not as a selector, so the token carries no
    # leading dot. These two are how sortTable tells a data row from the viewer
    # row beneath it; rename either and a column sort detaches every open 3D
    # panel from the design it belongs to.
    (r"contains\('cand-row'\)", "tr.cand-row, which sortTable moves", _ALL,
     _el(tag="tr", cls="cand-row")),
    (r"contains\('viewer-row'\)", "tr.viewer-row, which it moves with it",
     _ALL, _el(tag="tr", cls="viewer-row")),

    # Called from inline onclick in four places, which is the only reason the
    # shortlist button and the modal's three dismiss controls do anything.
    # Renaming either side left "Send shortlist to Ranomics lab" a dead button
    # with the whole suite green.
    (r"window\.openCampaignModal\b", "an onclick calling openCampaignModal(",
     _ALL, _el(calls="openCampaignModal")),
    (r"window\.closeCampaignModal\b", "an onclick calling closeCampaignModal(",
     _ALL, _el(calls="closeCampaignModal")),
    (r"'campaign-modal-'", "id=campaign-modal-<scope>", _ALL,
     _el(id_prefix="campaign-modal-")),
    (r"'\.shortlist-review'", "ul.shortlist-review inside the modal", _ALL,
     _el(cls="shortlist-review")),
    (r'\[name="candidate_refs"\]', 'the hidden input name="candidate_refs"',
     ("campaign", "target"), _el(tag="input", name="candidate_refs")),
    (r'\[name="candidate_indices"\]',
     'the hidden input name="candidate_indices"', ("job",),
     _el(tag="input", name="candidate_indices")),
]

_HOOK_IDS = [f"{h[1]} [{'+'.join(h[2])}]" for h in _HOOKS]


@pytest.mark.parametrize("js_token,emitted,modes,check", _HOOKS, ids=_HOOK_IDS)
def test_every_dom_hook_the_js_reads_is_emitted_by_the_template(
    js_token, emitted, modes, check,
):
    assert re.search(js_token, _JS), (
        f"{js_token} no longer matches anything in candidate_table.js")
    for mode in modes:
        assert check(_DOM[mode]), (
            f"candidate_table.js reads {js_token}, but the macro's {mode} mode "
            f"no longer renders {emitted}. The JS will silently find nothing.")


@pytest.mark.parametrize("mode", ["job", "campaign"])
def test_the_starred_export_is_target_mode_only(mode):
    """The pair for the three target-only rows above. Emitting the control
    everywhere would satisfy them while posting `refs` to the job and campaign
    export routes, which do not read it and would hand back the full file under
    a filename claiming otherwise."""
    assert _el(cls="cand-starred-export")(_DOM[mode]) == []
    assert _el(tag="input", name="refs")(_DOM[mode]) == []


@pytest.mark.parametrize("mode", _ALL)
def test_the_table_id_the_wrapper_advertises_is_a_table_that_exists(mode):
    """`initTable` does `getElementById(wrapEl.dataset.candTableId)` and
    RETURNS EARLY if that resolves to nothing, so the two ends of `table_id`
    are a contract even though neither is a rename of the other. Let them
    diverge and the star toggle, the column sort and the 3D expander are all
    dead at once, with the page rendering perfectly and the suite green: the
    hook rows above would each still find their attribute.
    """
    bars = _el(attr="data-cand-table-id")(_DOM[mode])
    assert len(bars) == 1, bars
    advertised = bars[0][1]["data-cand-table-id"]
    assert advertised, "the wrapper advertises an empty table id"

    tables = [e for e in _DOM[mode] if e[1].get("id") == advertised]
    assert len(tables) == 1, (advertised, [e[1].get("id") for e in _DOM[mode]])
    assert tables[0][0] == "table", tables[0]


def test_a_superstring_attribute_cannot_answer_for_the_hook_it_contains():
    """The mechanism, asserted directly rather than trusted.

    `data-job-id` on the action bar kept `data-job` alive for an entire round
    of QC. Both are still emitted, deliberately -- the JS reads each of them --
    so this pins that they are now told apart, which is the property the rows
    above depend on and the one a return to substring matching would lose.
    """
    bar = _el(cls="cand-action-bar", attr="data-job-id")(_DOM["target"])
    assert bar, "fixture assumption: the action bar still carries data-job-id"
    assert "data-job" not in bar[0][1], (
        "the action bar itself now carries data-job, so this file can no "
        "longer distinguish the two")
    assert _el(cls="star-btn", attr="data-job")(_DOM["target"])


def test_the_starred_export_form_registers_a_submit_handler():
    """The hidden `refs` field ships as `value="[]"` and is filled at submit
    time, because a value stamped at render time would be whatever was starred
    on the PREVIOUS page load. Drop the listener and the form still posts, still
    returns 200, and carries the render-time empty array.
    """
    block = _JS.split("'.cand-starred-export'", 1)
    assert len(block) == 2, "the starred-export block is gone entirely"
    # Bounded to the block that follows the selector, so a `submit` listener
    # somewhere else in the file cannot stand in for this one.
    following = block[1][:600]
    assert "addEventListener('submit'" in following, following[:200]


# ---------------------------------------------------------------------------
# The posted ref shape, at BOTH sites that build one
# ---------------------------------------------------------------------------

_REF_LITERAL = re.compile(
    r"return\s*\{\s*(\w+)\s*:\s*r\.j\s*,\s*(\w+)\s*:\s*r\.i\s*\}")

# Every place candidate_table.js turns a {j,i} sessionStorage entry into a wire
# ref, as (name, anchor, window). Searched inside a bounded block each, NOT as
# one file-wide findall: the round-19 version asserted `found` and
# `len(set(found)) == 1`, and both hold when only ONE literal survives. Changing
# the submit handler's copy to `return r;` therefore left both ref tests green
# while the starred export posted the raw {j,i} shape and `_parse_candidate_refs`
# dropped every ref of it -- a header-only CSV at HTTP 200, again.
#
# The anchor for the modal is the ASSIGNMENT, not the bare name: the name also
# appears in the file's own header comment, and splitting on that would bound
# the search to a block 200 lines above the code.
_REF_SITES = [
    ("the starred-export submit handler", "'.cand-starred-export'", 700),
    ("the lab-submit modal", "window.openCampaignModal = function", 700),
]
_REF_IDS = [s[0] for s in _REF_SITES]


def _ref_shape_at(anchor, window):
    parts = _JS.split(anchor, 1)
    assert len(parts) == 2, f"{anchor} is gone from candidate_table.js"
    match = _REF_LITERAL.search(parts[1][:window])
    assert match, (
        f"no {{job_id, index}} literal within {window} chars of {anchor}; "
        f"that block no longer puts a parseable ref on the wire")
    return match.groups()


@pytest.mark.parametrize("name,anchor,window", _REF_SITES, ids=_REF_IDS)
def test_each_site_that_posts_refs_uses_the_shape_the_server_parses(
    name, anchor, window,
):
    """Not a string comparison: the keys are lifted out of the JS and driven
    through the production parser. Emitting `{j, i}` -- the sessionStorage
    shape, one careless edit away -- makes this construct `{"j":..., "i":...}`,
    which `_parse_candidate_refs` drops entirely, so the assertion fails on the
    real consequence rather than on a diff.
    """
    import json

    from blueprints.lab_projects import _parse_candidate_refs

    job_key, idx_key = _ref_shape_at(anchor, window)
    payload = json.dumps([{job_key: "job-abc", idx_key: 3}])
    assert _parse_candidate_refs(payload) == [{"job_id": "job-abc", "index": 3}]


def test_the_two_ref_sites_do_not_drift_apart():
    """One shape in the file, not two. The server has a single parser, so a
    site that quietly grew its own key names would post something no route
    reads while the other site kept this file's other tests green."""
    shapes = {name: _ref_shape_at(anchor, window)
              for name, anchor, window in _REF_SITES}
    assert len(set(shapes.values())) == 1, shapes


def test_the_server_parser_really_would_drop_the_sessionstorage_shape():
    """The pair. If `_parse_candidate_refs` accepted anything, the tests above
    would pass under every mutation and prove nothing."""
    import json

    from blueprints.lab_projects import _parse_candidate_refs

    assert _parse_candidate_refs(json.dumps([{"j": "job-abc", "i": 3}])) == []
