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

ROUND 21. The JS half had the template half's OTHER disease. The file's own
header comment names the three globals it exposes, so `window.getShortlist`,
`window.openCampaignModal` and `window.closeCampaignModal` each matched TWICE
-- once in code, once in prose -- where every other token matched code only.
`open` was saved incidentally by the ref-site anchor below; `close` was saved
by nothing, and renaming its definition to `window.dismissCampaignModal`
passed the entire suite while the macro's three dismiss controls (the times,
Cancel, and the overlay onclick) all threw ReferenceError: the modal became
undismissable and `document.body.style.overflow = 'hidden'` stuck. So the JS
is COMMENT-STRIPPED before anything is searched in it, which retires the class
instead of the instance -- the same move the template half made by switching
to parsed elements.

Adding a hook to the macro does not require adding it here. Adding one the JS
*reads* does. Deliberately NOT listed, because nothing reads them across THIS
boundary: `data-campaign-id` (emitted by the macro; the JS header comment used
to claim it "drives the modal payload", which was never true and has been
corrected -- no `dataset.campaignId` read exists anywhere);
`window.getShortlist` (exposed, called by nothing in templates/ or static/);
and the `.starred` class (set by the JS, styled by the macro, never emitted by
it).

`window.initMolViewer` / `initMolViewerFromUrl` USED to be excluded here as "a
JS-to-JS contract with mol_viewer.js rather than a hook in this macro's DOM".
That reasoning was wrong: the macro is what LOADS mol_viewer.js, in the pair of
`<script>` tags that closes it, and a `<script src>` is an element of the
macro's own DOM like any other. Deleting the tag survived the whole suite
while "View 3D" became a
silent no-op expanding to a blank 420px panel on all 15 pages this macro
reaches. (job_detail.html carries its own tag, which is part of why the hole
was invisible.) Pinned now by
test_the_macro_loads_both_scripts_its_dom_is_useless_without.
"""

import pathlib
import re
from html.parser import HTMLParser

import pytest
from jinja2 import Environment, FileSystemLoader

from shared import metric_glossary, ranking, score_legends
from shared import pdb_bfactors

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_JS_SOURCE = (_ROOT / "static" / "js" / "candidate_table.js").read_text(
    encoding="utf-8")
_TEMPLATES = _ROOT / "templates"


def _lex(src: str) -> tuple[str, list[int]]:
    """``(src with every comment removed, offsets of the / left in CODE)``.

    Not a JavaScript parser. It tracks the two comment forms and the three
    string quotes with backslash escapes, which is everything
    candidate_table.js contains. The one construct that would defeat it is a
    REGEX LITERAL, whose body can hold a quote or a `/*`.

    Hence the second return value. Once comments are gone, every remaining
    slash in code position is a division or the opening of a regex, so an
    empty list is a decidable proof that there was no regex literal here to be
    mislexed. It over-approximates -- a division trips it too -- and that is
    the safe direction for a guard on an assumption.
    """
    out: list[str] = []
    slashes: list[int] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"`":
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\":
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                i += 1
                if src[i - 1] == c:
                    break
            continue
        if c == "/" and src[i + 1:i + 2] == "/":
            nl = src.find("\n", i)
            i = n if nl == -1 else nl
            out.append(" ")
            continue
        if c == "/" and src[i + 1:i + 2] == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        if c == "/":
            slashes.append(i)
        out.append(c)
        i += 1
    return "".join(out), slashes


# EVERY search below runs against the comment-stripped source, never the file
# as read. See the ROUND 21 paragraph: the header comment answered for two of
# the three globals it advertises, and one of those had no other backstop.
_JS = _lex(_JS_SOURCE)[0]


# ---------------------------------------------------------------------------
# The rendered artifact
# ---------------------------------------------------------------------------

def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    env.globals["metric_glossary"] = metric_glossary.GLOSSARY
    env.globals["score_legends_for"] = score_legends.score_legends_for
    env.globals["format_metric_value"] = metric_glossary.format_value
    env.globals["score_legend_for"] = score_legends.get_legend
    env.globals["judge_design"] = score_legends.judge
    env.globals["verdict_text"] = score_legends.verdict_text
    env.globals["plddt_on_100"] = metric_glossary.plddt_on_100
    env.globals["gate_bar_text"] = score_legends.gate_bar_text
    env.globals["shortfall_bar_text"] = score_legends.shortfall_bar_text
    env.globals["tool_has_bar"] = score_legends.tool_has_bar
    env.globals["raw_metric"] = score_legends.raw_metric
    env.globals["pdb_b64_on_100"] = pdb_bfactors.bfactors_on_100_b64
    env.globals["legend_text"] = score_legends.legend_text
    env.globals["score_era_caveat"] = score_legends.score_era_caveat
    env.globals["ordinal"] = ranking.ordinal
    env.globals["csrf_input"] = lambda: ""
    env.globals["url_for"] = lambda _e, **kw: "/static/" + kw.get("filename", "")
    return env


def _rows(tools=("bindcraft", "bindcraft")):
    """Two candidates, chosen so both PDB branches render.

    The macro emits `data-pdb-url` for a row carrying `pdb_key` and
    `data-pdb64` for the legacy inline-base64 row that has none, and the JS
    reads both. One row can only ever exercise one of them.

    ``tools`` is what makes the grouped mode grouped: the macro gates its group
    headers on more than one DISTINCT ``_source_tool`` among the rows, so the
    default -- one tool twice -- is the ungrouped shape however ``sort_mode``
    is set.
    """
    return [
        {"scores": {"ipTM": 0.91}, "pdb_key": "designs/d1.pdb",
         "_source_tool": tools[0], "_source_job_id": "job-1",
         "_source_index": 0, "_rank_position": 1},
        {"scores": {"ipTM": 0.88}, "pdb_content_b64": "QVRPTQ==",
         "_source_tool": tools[1], "_source_job_id": "job-1",
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


# The shapes this shared macro renders, each named for a real caller and given
# that caller's arguments. `job` and `campaign` come from
# templates/components/results_shell.html:48; `target` and `target_grouped` are
# both templates/targets/detail.html:525, which passes `agg.multi_tool`,
# `sort_mode`, `agg.split_tools` and `agg.per_tool` -- so it renders the first
# shape when the target holds ONE tool and the second when it holds more.
#
# ROUND 20 had three entries under the label "the three shapes this shared
# macro renders, named for the pages they ARE", while `target` passed only
# `target_id`: `multi_tool` was therefore False, and the mode named for the
# target page was the shape that page takes only at one tool. The multi-tool
# branch -- Tool/Score/Pctile head, group header rows, a different colspan --
# was never parsed by this file at all, and three rows below asserted hooks in
# `target` mode that the real multi-tool page does not emit.
_MODES = {
    "job": {},
    "campaign": {"campaign_id": "c-1"},
    "target": {"target_id": "t-1", "sort_mode": "percentile"},
    "target_grouped": {
        "target_id": "t-1", "multi_tool": True, "sort_mode": "tool",
        "candidates": _rows(("bindcraft", "boltzgen")),
        "split_tools": (),
        "per_tool": {"bindcraft": {"total": 1, "shown": 1, "cohort_n": 1},
                     "boltzgen": {"total": 1, "shown": 1, "cohort_n": 1}},
    },
}
_ALL = ("job", "campaign", "target", "target_grouped")

# The modes that render the CALLER'S metric columns, and so the only ones with
# anything for the column sort to bind to. Multi-tool mode replaces those
# columns with Tool/Score/Pctile and deliberately omits `data-col` from all
# three; see test_the_multi_tool_target_page_emits_no_column_sort_hook.
_COLUMNAR = ("job", "campaign", "target")

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
# The JS side is a REGEX over the COMMENT-STRIPPED source, and deliberately the
# executable form -- `dataset.refIdx`, not the `data-ref-idx` its header comment
# also mentions. Two guards rather than one because they fail differently:
# stripping means prose can no longer answer for code at all, and `\b` is what
# stops `dataset.job` matching the `dataset.jobId` two lines above it.
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

    (r"'th\[data-col\]'", "data-col on a sortable header", _COLUMNAR,
     _el(tag="th", attr="data-col")),
    (r"'\[data-col=\"'", "data-col on the value cells sortTable reads",
     _COLUMNAR, _el(tag="td", attr="data-col")),
    (r"dataset\.val\b", "data-val on those same cells", _COLUMNAR,
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
    # THE PER-MODE MAPPING OF THESE TWO CHANGED UNDER A91, and the change is
    # what the mode tuples record. `candidate_refs` was campaign+target and is
    # now every mode: the job branch posts it as well, because it is the shape
    # both ref arms take and `campaigns_submit` prefers it there too.
    # `candidate_indices` stays job-only -- it is the shape a page served before
    # that change carries, which is the entire reason the job branch emits two
    # fields instead of one, and emitting it in a ref mode would hand the ref
    # arms a payload neither of them parses.
    #
    # `openCampaignModal` looks the two inputs up INDEPENDENTLY and fills each
    # one it finds, which is why a mode carrying both needs no JS change -- and
    # why these rows are per-mode rather than global. A mode that stops emitting
    # an input posts nothing for it, and the arm then sees only whatever other
    # shortlist field that mode still carries: for the ref modes there is none,
    # so the submit answers `?handoff=none` -- "you starred nothing" -- to a
    # user who starred designs.
    (r'\[name="candidate_refs"\]', 'the hidden input name="candidate_refs"',
     _ALL, _el(tag="input", name="candidate_refs")),
    (r'\[name="candidate_indices"\]',
     'the hidden input name="candidate_indices"', ("job",),
     _el(tag="input", name="candidate_indices")),
    # A NEW THING THE JS READS, and the reason this row exists at all rather
    # than being left to the render tests: the parent field used to be a
    # server-side detail this file had no interest in, and A91 made it the
    # switch `openCampaignModal` decides the review list's wording on. The JS
    # used to key that on `refsInput` -- present only in the ref modes -- and
    # A91 gave job mode a refs input of its own, so the old key stopped
    # identifying scope and was replaced by this lookup.
    #
    # Job-only, matching the macro: exactly one parent field is emitted per
    # render. test_no_ref_mode_carries_a_job_scope_field below is the other
    # direction, which is the one that actually bites -- see its docstring.
    (r'\[name="source_job_id"\]', 'the hidden input name="source_job_id"',
     ("job",), _el(tag="input", name="source_job_id")),
    # The metric tooltip stopped being a ::after and became an element this
    # file creates, so the icon is now a DOM hook like any other. Columnar
    # modes only: multi-tool replaces the caller's columns with
    # Tool/Score/Pctile, which carry plain `title` attributes and no icon.
    (r"'\.mtt\[data-tooltip\]'", "span.mtt carrying data-tooltip", _COLUMNAR,
     _el(tag="span", cls="mtt", attr="data-tooltip")),
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


_MACRO_SRC = (
    _TEMPLATES / "components" / "candidate_table.html"
).read_text(encoding="utf-8")


def _macro_css_rules(selector_substring: str) -> list[tuple[str, str]]:
    """``(selector, declarations)`` for each rule in the macro's ``<style>``
    whose selector mentions ``selector_substring``.

    Comments are stripped first so a ``}`` inside one cannot close a block
    early, and the flat ``selector{decls}`` scan is only exact while the block
    holds no nested at-rule -- which is asserted rather than assumed, because
    an ``@media`` override is one of the ways the position below could be
    changed without any guard here noticing.
    """
    src = re.sub(r"/\*.*?\*/", "", _MACRO_SRC, flags=re.S)
    assert "@media" not in src and "@supports" not in src, (
        "the macro grew a nested at-rule, so this flat CSS scan no longer "
        "sees every rule and the guards built on it would pass over an "
        "override rather than fail on it"
    )
    rules = []
    for sel, decls in re.findall(r"([^{}]*)\{([^{}]*)\}", src):
        lines = [ln.strip() for ln in sel.strip().splitlines() if ln.strip()]
        if lines and selector_substring in lines[-1]:
            rules.append((lines[-1], decls))
    return rules


def test_the_metric_tooltip_is_not_anchored_inside_the_scroller():
    """The tooltip has two clipping ancestors, not one: ``.cand-table-scroll``
    is ``overflow-x: auto`` (per CSS spec a non-visible overflow on one axis
    makes the other axis clip too) and ``.panel`` above it is ``overflow:
    hidden`` outright. As a ``::after`` on the icon the tooltip was cut at
    whichever edge it was anchored to, by however much room the table left
    below its own header -- so a short run lost most of the text and a long
    run lost none, and the defect hid on exactly the pages people read for
    longest. Anchoring to the other edge, which is what the attempt before
    this one did, only changes which end goes.

    An overflow ancestor clips a descendant only when it also contains that
    descendant's containing block, so the ANCHOR is what decides. That is what
    these assertions are about, and they fail differently: a revert to the
    pseudo-element, a re-parenting back into the table, and a change of
    positioning scheme are three separate ways to put the clip back.

    WHAT THIS CANNOT SEE, since there is no browser in this suite: that the
    element is laid out where it should be, or that an ancestor ``transform``
    / ``filter`` / ``contain`` on ``html`` or ``body`` has captured the fixed
    box's containing block. Those need a rendered page.
    """
    assert "attr(data-tooltip)" not in _MACRO_SRC, (
        "the tooltip is a ::after fed by attr(data-tooltip) again, which "
        "parents it on the icon and puts it back under both clips"
    )
    # COUNT, not just presence. Asserting only that the <body> call exists let
    # a second appendChild -- ``.cand-table-scroll.appendChild(popEl)`` on the
    # next line -- move the element straight back under the clip with this
    # test still green.
    assert _JS.count("appendChild(popEl)") == 1, (
        "candidate_table.js appends the tooltip element in more than one "
        "place, so the last one wins and this file can no longer say where "
        "the tooltip ends up"
    )
    assert "document.body.appendChild(popEl)" in _JS, (
        "candidate_table.js no longer parents the tooltip on <body>"
    )

    rules = _macro_css_rules(".mtt-pop")
    assert rules, "the .mtt-pop rules are gone from the macro"
    positioned = [(sel, d) for sel, d in rules if "position:" in d]
    assert positioned, (
        "no .mtt-pop rule declares a position, so the box is static and every "
        "coordinate placeTooltip writes is ignored"
    )
    # EVERY .mtt-pop rule, not the first one. Checking only the first block let
    # `.mtt-pop.is-open` -- the rule in force whenever the box is visible --
    # override the position with this test still green.
    for sel, decls in positioned:
        assert re.search(r"position:\s*fixed\s*;", decls), (
            "the rule `%s` gives the tooltip a position other than fixed. "
            "placeTooltip writes VIEWPORT coordinates; anything else resolves "
            "them against <body> (static/style.css makes it position: "
            "relative), so the box lands in the wrong place and then drifts "
            "away from its icon as the page scrolls." % sel
        )


def test_the_tooltip_listeners_are_actually_installed():
    """Every part of this feature -- hover, focus, Escape, reposition -- hangs
    off one call site, and nothing else in the suite touches it. Deleting the
    single ``initTooltips();`` line leaves the CSS, the markup, the selector
    and the handlers all present and correct, the tooltip never opens, and
    every other guard here stays green over a feature that does nothing."""
    assert _JS.count("function initTooltips(") == 1, (
        "initTooltips is defined more or less than once"
    )
    boot = _JS.split("addEventListener('DOMContentLoaded'", 1)
    assert len(boot) == 2, "candidate_table.js has no DOMContentLoaded boot"
    assert re.search(r"\binitTooltips\(\)\s*;", boot[1]), (
        "initTooltips() is defined but never called from the boot block, so "
        "no tooltip listener is ever bound and the icons do nothing"
    )


def test_the_focus_path_opens_the_tooltip_and_the_aria_wiring_survives():
    """The companion to test_the_tooltip_icon_is_reachable_without_a_mouse,
    which only proves a `focusin` listener EXISTS. Emptying that handler
    leaves the tabindex, the listener and this file's other assertions intact
    while focusing an icon shows nothing at all."""
    parts = _JS.split("addEventListener('focusin'", 1)
    assert len(parts) == 2, "candidate_table.js has no focusin listener"
    handler = parts[1].split("addEventListener(", 1)[0]
    assert "showTooltip(" in handler, (
        "the focusin handler no longer calls showTooltip, so the icon takes "
        "focus and nothing opens:\n" + handler
    )
    # The description has to be announced, and it has to be released again --
    # one element and one id are shared by every icon, so an icon that keeps
    # aria-describedby after another icon takes the box advertises the wrong
    # column's text.
    for token in (
        "setAttribute('role', 'tooltip')",
        "setAttribute('aria-describedby'",
        "removeAttribute('aria-describedby')",
    ):
        assert token in _JS, (
            "candidate_table.js no longer emits %s, so the tooltip is not "
            "wired to the control it describes" % token
        )


def test_the_tooltip_is_clamped_into_the_viewport_on_both_axes():
    """Escaping the scroller only to run off the screen is the same defect in
    a bigger box. Preferring below and flipping above covers two of three
    cases; the third -- too tall to fit below AND too tall to fit above -- is
    ordinary on a short viewport, and without a clamp the box stayed below and
    left the screen entirely. Both axes need the same two-sided treatment.

    A TEXT PROXY for a layout fact, deliberately: there is no browser here to
    measure a rendered box. It goes red if either clamp is deleted, which is
    the regression that actually happened, and it cannot tell you the
    arithmetic is right."""
    parts = _JS.split("function placeTooltip(", 1)
    assert len(parts) == 2, "placeTooltip is gone"
    place = re.sub(r"\s+", " ", parts[1].split("\n  function ", 1)[0])
    for var in ("left", "top"):
        assert re.search(r"if \(%s > \w+\) %s = \w+;" % (var, var), place), (
            "placeTooltip no longer clamps `%s` at its upper bound, so the "
            "tooltip can be placed past the far edge of the viewport" % var
        )
        assert re.search(r"if \(%s < \w+\) %s = \w+;" % (var, var), place), (
            "placeTooltip no longer clamps `%s` at its lower bound, so the "
            "tooltip can be placed off the near edge of the viewport" % var
        )


@pytest.mark.parametrize("mode", _COLUMNAR)
def test_the_tooltip_icon_is_reachable_without_a_mouse(mode):
    """The tooltip is the only place a column's bar, definition and citation
    are stated, and hover is the only thing that used to open it. The span
    carries tabindex so it takes focus; candidate_table.js opens on focusin
    as well as pointerover, and both halves have to be present for either to
    be worth anything."""
    icons = _el(tag="span", cls="mtt")(_DOM[mode])
    assert icons, f"the macro's {mode} mode renders no .mtt icon at all"
    for _tag, attrs in icons:
        assert attrs.get("tabindex") == "0", (
            f"a .mtt icon in {mode} mode has no tabindex, so the tooltip is "
            f"unreachable by keyboard"
        )
    assert re.search(r"addEventListener\('focusin'", _JS), (
        "the icon takes focus but candidate_table.js no longer opens the "
        "tooltip on focusin, so focusing it shows nothing"
    )


@pytest.mark.parametrize("mode", ["job", "campaign"])
def test_the_starred_export_is_target_mode_only(mode):
    """The pair for the three target-only rows above. Emitting the control
    everywhere would satisfy them while posting `refs` to the job and campaign
    export routes, which do not read it and would hand back the full file under
    a filename claiming otherwise."""
    assert _el(cls="cand-starred-export")(_DOM[mode]) == []
    assert _el(tag="input", name="refs")(_DOM[mode]) == []


@pytest.mark.parametrize("mode", ["campaign", "target", "target_grouped"])
def test_no_ref_mode_carries_a_job_scope_field(mode):
    """The pair for the two job-only rows above, and the direction that bites.

    `source_job_id` is not merely the job form's parent field any more. Since
    A91 it is the switch `openCampaignModal` reads --
    `!!modal.querySelector('[name="source_job_id"]')` -- to decide whether a
    reviewed candidate gets its `· sub-job <id>` suffix. The suffix is
    suppressed in job scope because a single-job table has exactly one job and
    calling it a sub-job of itself reads as a rendering fault. Emit the field
    in a ref mode and the suppression fires on the tables that INTERLEAVE
    several sub-jobs: the review list shows "Candidate 1, Candidate 1,
    Candidate 2" with nothing saying which sub-job any of them came from, on
    the last screen before a paid wet-lab order. The positive row above cannot
    see this -- it asserts job mode still emits the field, and every mode
    emitting it satisfies that.

    Routing is unaffected, deliberately not claimed otherwise: `campaigns_submit`
    tries `source_target_id`, then `source_campaign_id`, then `source_job_id`,
    so a ref form carrying an extra job id still reaches its own arm. The
    damage is entirely in what the user is shown.

    `candidate_indices` is asserted here for a different reason.
    tests/test_target_table_render.py calls it "the ONLY field this form has
    that the ref forms do not", which is the stated reason the job branch
    cannot yet be collapsed into the ref one; emitting it here makes that
    sentence false. No arm reads it off a ref form today -- both parse
    `candidate_refs` alone -- but what `openCampaignModal` would put in it is
    `sl.map(r => r.i)`, and `data-ref-idx` on a merged table is the row's index
    WITHIN ITS OWN sub-job, so the payload is a list of positions with the job
    that gives each one meaning stripped off.
    """
    assert _el(tag="input", name="source_job_id")(_DOM[mode]) == []
    assert _el(tag="input", name="candidate_indices")(_DOM[mode]) == []
    # ...and the mode is not vacuously fieldless: it does render the lab-submit
    # form, with its own parent field and the refs payload both ref arms parse.
    assert _el(tag="input", name="candidate_refs")(_DOM[mode])


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


@pytest.mark.parametrize("mode", _ALL)
def test_the_macro_loads_both_scripts_its_dom_is_useless_without(mode):
    """The macro's two `<script src>` tags, which are DOM like anything else.

    `mol_viewer.js` was excluded from the table above as "a JS-to-JS contract
    with mol_viewer.js rather than a hook in this macro's DOM". It is not:
    this macro is what loads it, and deleting that one tag survived the whole
    suite while every "View 3D" button expanded to a blank 420px panel --
    `window.initMolViewerFromUrl` is simply undefined, and the JS guards on it
    (`&& window.initMolViewerFromUrl`), so nothing throws and no console error
    appears either.

    Both tags, because losing the second is strictly worse: the star toggle,
    the column sort, the 3D expander and the lab-submit modal all stop at once
    and every hook row above stays green, since they assert what the macro
    EMITS and the macro would emit all of it.
    """
    assert re.search(r"window\.initMolViewer\b", _JS)
    assert re.search(r"window\.initMolViewerFromUrl\b", _JS)

    srcs = [a.get("src") or "" for tag, a in _DOM[mode] if tag == "script"]
    for filename in ("js/mol_viewer.js", "js/candidate_table.js"):
        assert any(s.endswith(filename) for s in srcs), (filename, srcs)


def test_the_multi_tool_target_page_emits_no_column_sort_hook():
    """The pair for the three `_COLUMNAR`-only rows, and the reason they are
    restricted rather than simply asserted everywhere.

    The multi-tool head prints Tool / Score / Pctile and leaves `data-col` off
    all three ON PURPOSE: the JS binds click-to-sort to `th[data-col]` and
    reorders rows numerically on `td[data-val]`, so leaving the attribute on
    would let a user sort ipTM 0.91 against reward 12.40 against ipAE 3.70 in
    the browser -- the exact cross-scale comparison this table exists to
    prevent. Narrowing three rows to `_COLUMNAR` without this would be
    indistinguishable from exempting a mode that had lost its hooks by
    accident.
    """
    dom = _DOM["target_grouped"]
    assert _el(tag="th", attr="data-col")(dom) == []
    assert _el(tag="td", attr="data-col")(dom) == []
    assert _el(tag="td", attr="data-val")(dom) == []

    # ...and the mode is not vacuously empty. A fixture that rendered no rows,
    # or rendered the ungrouped shape under a grouped name, would satisfy every
    # assertion above -- which is the defect that put this mode here.
    assert len(_el(tag="tr", cls="cand-row")(dom)) == 2, "no data rows"
    assert _el(tag="tr", cls="cand-group-row")(dom), "not actually grouped"


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


def test_the_comment_stripper_removes_comments_and_only_comments():
    """The mechanism the whole JS half now rests on, on a crafted input.

    A stripper that silently no-opped would report a green for every token it
    was supposed to disqualify, which is worse than not having one. The
    apostrophe case is not decoration: candidate_table.js says "the
    candidate's index" in a line comment, and anything that looked for quotes
    before it looked for comments would open a string there and swallow the
    next 60 lines of code.
    """
    src = "\n".join([
        "var a = 1; // the candidate's own index",
        "/* block",
        "   spanning lines */",
        "var url = 'http://x/y';",
        'var star = "/* not a comment */";',
        "var t = `a // b`;",
        "var esc = 'it\\'s fine'; // trailing",
        "var last = 2;",
    ])
    out, slashes = _lex(src)

    assert "candidate's own index" not in out
    assert "spanning lines" not in out
    assert "trailing" not in out
    # Code on the same line as a comment survives, and so does the line after
    # a comment -- a stripper that ate to the wrong terminator would pass every
    # assertion above and delete the file.
    assert "var a = 1;" in out
    assert "var last = 2;" in out
    # Slashes and comment openers inside string literals are content.
    assert "'http://x/y'" in out
    assert '"/* not a comment */"' in out
    assert "`a // b`" in out
    assert "it\\'s fine" in out
    assert slashes == [], slashes

    # The pair for that last one: the detector is not vacuous.
    assert len(_lex("var half = n / 2;")[1]) == 1


def test_no_comment_in_candidate_table_js_can_answer_for_a_hook():
    """ROUND 21, the instance behind the mechanism.

    The file's header comment advertises the three globals it exposes, so a
    plain search found each of them whether or not the definition still
    existed. `window.closeCampaignModal` had no other backstop: renaming its
    definition passed the entire suite while the modal's three dismiss
    controls threw ReferenceError.

    Counting is the assertion, not membership. One occurrence means the
    definition and nothing else; two means the comment is back in scope and
    every token this file searches for is soft again.

    `dropShortlistRefs` is in the list for the same reason its three siblings
    are, and its caller is off this macro entirely: templates/campaigns/
    detail.html loads this file solely to call it (register item A89), so a
    rename that the header comment answered for would leave a submitted
    shortlist un-touched with nothing here red. Its own cross-boundary pin lives
    in tests/test_lab_project_confirmation.py, which renders that page and, where
    `node` is on PATH, executes the function.
    """
    assert "Exposes:" in _JS_SOURCE, (
        "fixture assumption: candidate_table.js still carries the header "
        "comment that made this necessary")
    assert "Exposes:" not in _JS, "the stripper did not strip"

    for name in ("getShortlist", "openCampaignModal", "closeCampaignModal",
                 "dropShortlistRefs"):
        raw = _JS_SOURCE.count("window." + name)
        assert raw == 2, (name, raw)          # once in prose, once in code
        assert _JS.count("window." + name) == 1, name


def test_candidate_table_js_holds_no_regex_literal_the_stripper_could_mislex():
    """The stripper's one documented blind spot, held closed.

    A regex literal can carry a quote or a `/*` in its body, and this lexer
    would take either at face value. It cannot recognise one -- telling `/` as
    division from `/` as a regex needs the parse this file does not do -- so
    it reports every slash it leaves in code position instead, and the file is
    required to have none. Today every slash in candidate_table.js is inside
    a string literal (`'<li>'`, `'</li>'`).
    """
    slashes = _lex(_JS_SOURCE)[1]
    assert slashes == [], [_JS_SOURCE[max(0, p - 40):p + 40] for p in slashes]


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
# The anchor for the modal is the ASSIGNMENT rather than the bare name. That
# used to be load-bearing on its own -- the bare name also appears in the
# file's own header comment, and splitting on that would have bounded the
# search to a block 200 lines above the code. Round 21's comment-stripping now
# disqualifies the prose copy outright; the assignment stays because it is
# still the more precise anchor, and because it is what happened to keep
# `window.openCampaignModal` pinned while its sibling had nothing at all.
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
