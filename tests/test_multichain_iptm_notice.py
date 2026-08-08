"""ipTM must be marked not-comparable on a multi-chain target.

The defect: ipTM is computed over the interfaces of the whole complex, not the
binder-to-target pair alone, so on a multi-chain target the target's own
chain-chain interface holds the number up almost independently of binder
quality. It is both the displayed value AND the ranking key
(``shared/result_columns.py``), so a mediocre binder can rank first carrying a
plausible-looking number.

Stated at that level on purpose. An earlier version of this docstring, and of
the banner, said "a MAX over residues" and quoted "~0.9 for a real crystal
dimer" — and four pipeline files in this repo describe ipTM as interface-pTM
"averaged over EVERY chain pair" instead (tools/af2/run_pipeline.py:202 and
three siblings). The conclusion holds under either reduction; the figure does
not. See the comment above MULTICHAIN_IPTM_UNRELIABLE_TOOLS in
shared/score_legends.py.

Every test here asserts BOTH directions. A presence-only test passes against a
banner that renders unconditionally, which would put a scary caveat on every
single-chain run — the far more common case — and train users to ignore it.
"""
from __future__ import annotations

import os
import re
import uuid
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.score_legends import (
    MULTICHAIN_IPTM_UNRELIABLE_TOOLS,
    multichain_iptm_unreliable,
)
from shared.targets import TARGET_READ_OK, DesignTarget, TargetRead

pytestmark = pytest.mark.usefixtures("isolate_supabase")

NOTICE_MARKER = "data-multichain-iptm-notice"

# boltzgen is deliberately not here any more: llm-proteinDesigner#18 deployed,
# so its container reports the binder-to-target interface and the banner's
# claim is false of any new run. The caveat its PRE-deploy runs still need
# moved to the ipTM legend, which is per tool and per row. The reasoning, and
# the two discriminators that were checked — one absent, one merely not
# projected — are in the comment above MULTICHAIN_IPTM_UNRELIABLE_TOOLS in
# shared/score_legends.py.
BANNER_TOOLS = ("rfdiffusion", "pxdesign", "bindcraft")

# The load-bearing half of the mechanism sentence: the number is computed over
# interfaces that INCLUDE the target's own chain-chain contact. A shape, not a
# phrasing, and dash-agnostic — the copy uses an en dash and the extracted text
# carries it through convert_charrefs.
_INCLUDES_TARGET_INTERFACE = re.compile(
    r"includ\w+ the target'?s own chain.chain", re.I
)

# --- the two halves of "the banner may not describe the page" -------------
#
# WHY THIS IS NO LONGER A WORD LIST. Every previous version of this guard was
# a denylist of banned words, and a denylist has now been walked around twice:
# once by a phrase it had never heard of ("columns in this table"), and once,
# in an independent review, by swapping "below" for "beneath" -- which put the
# ORIGINAL defect back, verbatim, with the whole suite green.
#
# So the column/control half is now read OFF THE RENDERED PAGES (see
# ``_column_labels`` and ``_page_labels`` below): the banner may not quote a
# label that any surface it renders on does not have. That is the actual
# property and it needs no maintenance when a column is added.
#
# THE VERSION OF THAT CLAIM THAT SAID "beneath buys a mutation nothing,
# because what it catches is the LABEL, not the preposition" WAS TOO STRONG,
# and an independent review defeated the cross-check with one punctuation
# character: the panel is labelled "Second-opinion fold", the copy
# "the second opinion fold" restored the original round-1 defect verbatim, and
# a byte-exact matcher never saw it. Same for "shape-complementarity" against
# the `<th>` "Shape complementarity (SC)". So the match is now on a NORMALISED
# form (``_norm``): case, hyphens, underscores, slashes, dashes and runs of
# whitespace are all one thing on both sides, and a trailing parenthetical is
# stripped as an extra variant.
#
# What a cross-check cannot catch is copy that quotes NO label at all: "the
# order of this table", "these designs", "Only the top 300 designs are shown
# here", "Star the ones you want". No page has a `<th>` reading "table", and
# nothing on any page is labelled "the designs you are comparing" -- yet each
# of those is false on the zero-candidate page. Three regexes are what is left,
# and they are grammars rather than synonym lists:

# A deictic pointed at page furniture: a determiner, up to two modifiers, and
# a noun naming something that is either on the page or not. "these designs"
# and "this table" are claims about what the reader is looking at; the macro
# has no parameter that could tell it, and on a zero-candidate job page both
# are false.
#
# TWO ALTERNATIONS, and the split is deliberate. A POINTING word (this, these,
# that, those) makes anything it points at a claim about the page, including
# the designs. A definite article does not: "the design with the highest ipTM"
# is generic English and honest copy, while "the table" and "the column" have
# no non-page meaning here. Banning ``the design`` would be the round-3 NIT-7
# mistake -- a guard that rejects true copy -- committed by this fix instead.
#
# THE NOUN LIST GREW IN ROUND 5, and the additions are the ones an independent
# review walked through: "with the re-fold FORM" and "the SHORTLIST you
# starred" both named a piece of page that two surfaces do not have, and
# neither ``fold``, ``form`` nor ``shortlist`` was here. It is still a list,
# which is a real weakness -- but it is a list of KINDS of page part, not of
# phrasings, so a synonym for "below" or a missing hyphen does not defeat it.
_FURNITURE_NOUN = (
    r"table|tables|column|columns|row|rows|panel|panels|list|lists|"
    r"page|pages|button|buttons|control|controls|menu|menus|widget|widgets|"
    r"fold|folds|form|forms|field|fields|link|links|tab|tabs|"
    r"section|sections|selector|selectors|toggle|toggles|chart|charts|"
    r"graph|graphs|badge|badges|banner|banners|notice|notices|box|boxes|"
    r"card|cards|view|views|screen|screens|shortlist|shortlists|"
    r"checkbox|checkboxes|dropdown|dropdowns|tooltip|tooltips|"
    r"header|headers|footer|footers|star|stars"
)
_DEICTIC_FURNITURE = re.compile(
    r"\b(?:this|these|that|those)\s+(?:\w[\w-]*[\s-]+){0,2}"
    r"(?:" + _FURNITURE_NOUN + r"|design|designs|candidate|candidates|"
    r"result|results)\b"
    r"|\bthe\s+(?:\w[\w-]*[\s-]+){0,2}(?:" + _FURNITURE_NOUN + r")\b",
    re.I,
)

# THE CLASS THE LABEL CROSS-REFERENCE STRUCTURALLY CANNOT CATCH: a claim that
# the page HAS content, or that the reader has already done something to it.
# Six mutations in the round-4 review quoted no label and named no furniture,
# and every one of them was false on the zero-candidate job page --
# "The designs you are comparing…", "Scroll down to the ranked designs",
# "Only the top 300 designs are shown here", "Star the ones you want",
# "re-fold the shortlist you starred".
#
# THREE GRAMMARS, and the first is the one worth reading twice:
#
#   * A DEFINITE PLURAL presupposes a particular set. "Designs are ranked by
#     it" is generic English about the tool and is the banner's own copy; "the
#     designs", "the ranked designs", "the top 300 designs" all assert that a
#     set of them is in front of the reader. The bare plural stays writable and
#     so does the singular "the design with the highest ipTM" -- banning either
#     would be the round-3 NIT-7 mistake (a guard that rejects true copy).
#   * CONTENT THE READER ACTED ON -- "designs you are comparing", "the ones you
#     want", "the shortlist you starred". The macro takes a tool slug and a
#     chain; it cannot see the reader's history with the page.
#   * PRESENCE ADVERBS -- shown / listed / displayed / visible / here. Same
#     family as the locatives below, but about existence rather than position.
_ASSERTS_PAGE_CONTENT = re.compile(
    r"\bthe\s+(?:\w[\w-]*[\s-]+){0,2}"
    r"(?:designs|candidates|results|rows|entries|hits|ones)\b"
    r"|\b(?:designs?|candidates?|results?|rows?|ones|shortlists?|list)\b"
    r"[^.]{0,24}\byou\b"
    r"|\b(?:shown|listed|displayed|visible)\b|\bhere\b",
    re.I,
)

# An instruction to OPERATE the page. Deliberately not a list of every verb:
# these are the ones that only make sense if a particular control or a
# particular row is present. "Do not choose between designs on ipTM alone" and
# "confirm a shortlist with an independent re-fold" are judgements about the
# metric and stay writable; "Scroll down to…" and "Star the ones you want" are
# not.
_PAGE_ACTION = re.compile(
    r"\b(?:scroll|click|tap|hover|drag|expand|collapse|re-?sort|"
    r"star|starred|unstar|tick|untick)\b"
    r"|\bsort by\b|\bopen the\b|\bswitch to\b|\bselect the\b",
    re.I,
)

# A locative. ANCHORED, not banned outright: the previous version rejected
# ``\babove\b`` unconditionally, so a threshold sentence -- "treat a value
# above 0.8 with the same suspicion" -- could not be written even though it
# names no furniture at all. A locative "above"/"below" is not followed by a
# number; a threshold one always is.
_POINTS_AT_FURNITURE = re.compile(
    r"\b(?:below|above|beneath|underneath|overleaf)\b"
    r"(?!\s+(?:roughly|about|around|approximately|only|just)?\s*\d)"
    r"|\bopposite\b|\bfurther (?:up|down)\b|\bon this page\b"
    r"|\bat the (?:top|bottom|side)\b|\bto the (?:left|right)\b"
    r"|\bnext to (?:this|it)\b|\bre-?fold\b[^.]{0,40}\bBoltz",
    re.I,
)


@pytest.fixture(scope="module")
def flask_app():
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


# ---------------------------------------------------------------------------
# The decision, in Python
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(MULTICHAIN_IPTM_UNRELIABLE_TOOLS))
@pytest.mark.parametrize("chain", ["A,B", "A B", "A, B", "A,B,C"])
def test_multi_chain_targets_are_flagged(tool, chain):
    assert multichain_iptm_unreliable(tool, chain) is True


@pytest.mark.parametrize("tool", sorted(MULTICHAIN_IPTM_UNRELIABLE_TOOLS))
@pytest.mark.parametrize("chain", ["A", " A ", "", None, "A,A", "B B"])
def test_single_chain_targets_are_not_flagged(tool, chain):
    """``"A,A"`` matters: the field de-duplicates everywhere else
    (tools/base.parse_target_chains), so a repeated chain is ONE chain and must
    not trip a caveat about cross-chain interfaces that do not exist."""
    assert multichain_iptm_unreliable(tool, chain) is False


@pytest.mark.parametrize(
    "tool", ["proteina", "rfantibody", "boltz2", "boltzgen", "", None]
)
def test_unaffected_tools_are_never_flagged(tool):
    """proteina reports af2_iptm from a different scoring path, rfantibody
    cannot take a multi-chain target at all, and boltzgen's container now
    reports the binder-to-target interface. Warning on any of them would be
    noise, and noise is what makes a real warning ignorable."""
    assert multichain_iptm_unreliable(tool, "A,B") is False


def test_a_pooled_table_is_flagged_if_any_tool_is_affected():
    """The target page pools several tools into one table."""
    assert multichain_iptm_unreliable(["proteina", "rfdiffusion"], "A,B") is True
    assert multichain_iptm_unreliable(["proteina"], "A,B") is False
    assert multichain_iptm_unreliable([], "A,B") is False


def test_boltzgen_left_the_banner_set_and_its_caveat_did_not_vanish():
    """The two halves of the B11 decision, pinned together on purpose.

    llm-proteinDesigner#18 is merged (311c29f) and deployed, so the container
    reports the binder-to-target interface and the banner would be telling
    every new boltzgen user something untrue about their run. It is out.

    What must NOT come with that is the silent loss of the caveat the
    PRE-deploy runs still need: a results page renders whatever the job
    stored, at least one multi-chain boltzgen run predates the deploy, there
    is no per-record marker saying which IPTM_KEYS entry produced the number,
    and the pooled rows the sixth call site renders carry no date because
    neither pooled read projects created_at (both checked; see the comment
    above MULTICHAIN_IPTM_UNRELIABLE_TOOLS, which is careful about the
    difference between absent and unprojected). So the caveat moved to the
    ipTM legend,
    which renders per tool and per row, and this test refuses to let one half
    of the trade happen without the other.
    """
    from shared.score_legends import get_legend, legend_text

    assert "boltzgen" not in MULTICHAIN_IPTM_UNRELIABLE_TOOLS
    # ``legend_text``, not ``["explanation"]``: the caveat lives in the
    # legend's optional ``caveat`` field, because ``explanation`` is a
    # one-line slot shared by 32 legends and the era note is 380 characters
    # that only one of them needs. NOT because the caveat is false in the
    # email — that reasoning shipped for a round and was wrong; the mail is
    # sent about stored results too, and shared/score_legends.email_caption
    # now carries the caveat there on a multi-chain job. What has to survive
    # is what a READER OF THE TABLE sees, which is both halves.
    shown = legend_text(get_legend("boltzgen", "ipTM"))
    assert "chain-chain" in shown, (
        "boltzgen left the banner set without the legend picking up the "
        "pre-deploy caveat — the old runs now carry no warning anywhere"
    )


# ---------------------------------------------------------------------------
# The banner, in the rendered page
# ---------------------------------------------------------------------------

class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list = []

    def handle_data(self, data):
        self.chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self.chunks).split())


class _BannerText(HTMLParser):
    """Visible text INSIDE the notice element only.

    Scoped deliberately. Asserting on the whole page would let
    components/results_shell.html's own "Re-fold with Boltz-2 (cofold)" copy
    satisfy — or trip — a check about what the BANNER says, which is the
    unrelated-copy failure mode tests/test_multichain_form_affordances.py
    already paid for once.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self.chunks: list = []

    def handle_starttag(self, tag, attrs):
        if self._depth:
            self._depth += 1
        elif any(k == "data-multichain-iptm-notice" for k, _ in attrs):
            self._depth = 1

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self.chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self.chunks).split())


def _banner_text(html: str) -> str:
    parser = _BannerText()
    parser.feed(html)
    return parser.text


class _Headers(HTMLParser):
    """The visible label of every ``<th>`` on the page, in order.

    Used to pin the PREMISE of the furniture check rather than restating it:
    the copy may not name a column, and this is what says which columns the
    page has. Reading them off the rendered page means the premise fails
    loudly if candidate_table ever grows the metric columns back in
    multi-cohort mode, instead of the check quietly guarding nothing.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._chunks: list = []
        self.headers: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "th":
            self._depth = 1
            self._chunks = []
        elif self._depth:
            self._depth += 1

    def handle_endtag(self, tag):
        if not self._depth:
            return
        self._depth -= 1
        if self._depth == 0:
            self.headers.append(" ".join("".join(self._chunks).split()))

    def handle_data(self, data):
        if self._depth:
            self._chunks.append(data)


def _table_headers(html: str) -> list:
    parser = _Headers()
    parser.feed(html)
    return parser.headers


# --- labels, read off the render, for the positive cross-check ------------

_VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta source track wbr".split()
)


class _Labels(HTMLParser):
    """Every multi-word LABEL on the page, from outside the banner.

    A label is the complete visible text of an element that has no child
    element contributing text of its own -- a button, a table header, a panel
    title, a nav link -- plus ``title``/``aria-label``/``placeholder``
    attribute values, which is how several of this app's controls name
    themselves. Capped at eight words, because past that it is prose.

    MULTI-WORD ONLY, and that is a real limitation rather than a convenience:
    single-word labels ("Score", "Filter", "Designs") collide with ordinary
    English, so banning them here would reject honest copy. The single-word
    case that actually matters is the column, and ``_column_labels`` covers it
    exactly, from the `<th>` elements themselves.

    The banner's own text is excluded -- otherwise the banner would name
    furniture "present on the page" by the act of naming it.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._stack: list = []
        self.labels: set = set()

    def _add(self, raw: str) -> None:
        norm = " ".join(raw.split())
        if (
            len(norm) >= 3
            and 2 <= len(norm.split()) <= 8
            and re.search(r"[A-Za-z]", norm)
        ):
            self.labels.add(norm)

    def _attr_labels(self, attrs) -> None:
        for key, value in attrs:
            if key in ("title", "aria-label", "placeholder") and value:
                self._add(value)

    def handle_starttag(self, tag, attrs):
        if self._skip:
            if tag not in _VOID_TAGS:
                self._skip += 1
            return
        if any(k == "data-multichain-iptm-notice" for k, _ in attrs):
            self._skip = 1
            return
        self._attr_labels(attrs)
        if tag not in _VOID_TAGS:
            self._stack.append([tag, [], False])

    def handle_startendtag(self, tag, attrs):
        if not self._skip:
            self._attr_labels(attrs)

    def handle_endtag(self, tag):
        if self._skip:
            self._skip -= 1
            return
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                frame = self._stack.pop(i)
                del self._stack[i:]  # discard anything left unclosed inside
                break
        else:
            return
        text = "".join(frame[1])
        if not frame[2] and frame[0] not in ("script", "style"):
            self._add(text)
        if self._stack:
            self._stack[-1][1].append(text)
            if text.strip():
                self._stack[-1][2] = True

    def handle_data(self, data):
        if not self._skip and self._stack:
            self._stack[-1][1].append(data)


def _page_labels(html: str) -> set:
    parser = _Labels()
    parser.feed(html)
    return parser.labels


def _column_labels(html: str) -> set:
    """The `<th>` labels of a page, in the forms copy might write them in.

    ``_table_headers`` returns the header cell's whole visible text, which
    includes the "?" of the tooltip affordance and any unit parenthetical --
    "RMSD (Å) ?". Copy would write "RMSD", so the parenthetical is split off
    and both halves are registered. Without that, a banner naming "i_pAE"
    would sail past a forbidden set holding only "i_pAE (Å)".
    """
    out: set = set()
    for raw in _table_headers(html):
        label = re.sub(r"\s*\?\s*$", "", raw).strip()
        head, sep, tail = label.partition("(")
        for part in (label, head, tail.rstrip(") ")) if sep else (label,):
            part = part.strip()
            if len(part) >= 2 and re.search(r"[A-Za-z]", part):
                out.add(part)
    return out


def _norm(text: str) -> str:
    """Case-folded, with every separator reduced to one space.

    Hyphen, en/em dash, underscore, slash and runs of whitespace all become
    the same thing, so "Second-opinion fold", "second opinion fold" and
    "second_opinion  fold" are ONE string. That is not cosmetic: the byte-exact
    version of this matcher was defeated by dropping a single hyphen, which put
    the round-1 defect back verbatim with the suite green, and again by
    "shape-complementarity" against the `<th>` "Shape complementarity (SC)".
    """
    return " ".join(re.sub(r"[-_/‐-―]+", " ", text.lower()).split())


def _label_forms(label: str) -> list:
    """The normalised strings copy might write ``label`` as.

    The label itself, plus the same label with a trailing parenthetical
    dropped -- "Re-fold with Boltz-2 (cofold)" is written "the re-fold with
    Boltz-2" as often as not. ``_column_labels`` does the equivalent split for
    `<th>` text; this covers the labels that come from anywhere else.
    """
    forms = [_norm(label)]
    stripped = _norm(re.sub(r"\s*\([^)]*\)\s*$", "", label))
    if stripped and stripped != forms[0] and len(stripped) >= 3:
        forms.append(stripped)
    return forms


def _quoted_in(text: str, phrases) -> list:
    """Which of ``phrases`` the copy quotes, matched on whole words.

    Both sides go through ``_norm`` first, so punctuation and case cannot
    hide a quote.
    """
    hay = _norm(text)
    out = []
    for phrase in phrases:
        for form in _label_forms(phrase):
            if re.search(
                r"(?<![0-9a-z])" + re.escape(form) + r"(?![0-9a-z])", hay,
            ):
                out.append(phrase)
                break
    return sorted(out)


def _metrics_no_surface_displays(surfaces) -> set:
    """Metric names that appear on NO page the banner renders on.

    The `<th>` cross-check can only forbid what some surface actually draws,
    and the seven surfaces between them draw the columns of three tools. A
    banner that named ``ipAE`` (rfantibody's spelling) or ``total_reward``
    (proteina's ranking key) would be false on ALL SEVEN, and the cross-check
    would not notice — those two were covered by the old denylist and the
    rewrite dropped them.

    DERIVED, from the two registries that define what a metric is called:
    ``shared.score_legends.SCORE_LEGENDS`` keys and
    ``shared.result_columns.columns_for`` for every adapter in the registry.
    So a new tool's metrics join the forbidden set without an edit here.

    ipTM is exempt for the same reason it is exempt from the column half: it is
    what the banner is about. Anything a surface DOES display is removed, since
    the column half already forbids it in the form the page writes it in.
    """
    from shared.result_columns import columns_for
    from shared.score_legends import SCORE_LEGENDS
    from tools import base as tool_base

    names = {col for (_tool, col) in SCORE_LEGENDS}
    for adapter in tool_base.all_adapters():
        names |= set(columns_for(adapter.slug))

    displayed = set()
    for html in surfaces.values():
        displayed |= {_norm(c) for c in _column_labels(html)}
    return {
        n for n in names
        if _norm(n) not in displayed and _norm(n) != "iptm"
    }


@pytest.mark.parametrize("copy,label", [
    # The exact walk-around an independent review used: the panel is labelled
    # "Second-opinion fold" and dropping the hyphen restored the round-1 defect
    # verbatim with the whole suite green.
    ("confirm a shortlist with the second opinion fold against the same "
     "target", "Second-opinion fold"),
    ("confirm a shortlist with the Second-Opinion  Fold", "Second-opinion fold"),
    # Same hole on the column half: the `<th>` reads "Shape complementarity
    # (SC)" and "shape-complementarity" sailed past.
    ("Rank designs by their shape-complementarity instead",
     "Shape complementarity (SC)"),
    ("Rank designs by their shape complementarity instead",
     "Shape complementarity"),
])
def test_the_label_cross_check_is_not_defeated_by_punctuation(copy, label):
    """The guard's own unit, so a byte-exact matcher fails HERE.

    Without it the only thing pinning the normalisation is a template mutation
    nobody runs, and a return to ``re.escape(label.lower())`` would show up as
    a survivor rather than as a failure.
    """
    assert _quoted_in(copy, {label}) == [label]


def test_a_label_is_not_matched_inside_a_longer_word():
    """The other direction: normalising separators must not make the matcher
    fire on a substring. ``_norm`` collapses "-" to " ", so a word boundary is
    still a boundary."""
    assert _quoted_in("the shape complementarity index", {"Complementarity"}) \
        == ["Complementarity"]
    assert _quoted_in("supercomplementarity is not a word", {"Complementarity"}) \
        == []


def _render_results(flask_app, tool: str, target_chain: str) -> str:
    job = SimpleNamespace(
        id="job-1",
        tool=tool,
        status="succeeded",
        inputs={"target_chain": target_chain, "hotspot_residues": ["A296"]},
        result={
            "candidates": [
                {
                    "design_name": "d0",
                    "sequence": "AAAA",
                    "scores": {"ipTM": 0.91, "pLDDT": 88.0, "filter_status": "pass"},
                }
            ],
            "tier": "pilot",
        },
    )
    from flask import render_template

    with flask_app.test_request_context(f"/jobs/{job.id}"):
        return render_template(
            f"tools/{tool}_results.html", job=job, send_target_tools=None
        )


@pytest.mark.parametrize("tool", BANNER_TOOLS)
def test_banner_appears_on_a_multi_chain_job(tool, flask_app):
    html = _render_results(flask_app, tool, "A,B")
    assert NOTICE_MARKER in html, f"{tool}: no notice on a multi-chain job"
    text = _Text()
    text.feed(html)
    body = text.text
    # The ranking consequence is the half that matters. A caveat that
    # disclaims only the VALUES while the ORDER silently persists is the
    # half-measure this exists to avoid.
    assert "ranked by it" in body, f"{tool}: notice never mentions ranking"
    # And the mechanism, at the level this repo can stand behind. This used to
    # assert "maximum over residues"; four pipeline files here call ipTM
    # interface-pTM "averaged over EVERY chain pair", so that assertion pinned
    # a claim the repo contradicts. What has to survive is WHY the number is
    # not about the binder: it is computed over interfaces that include the
    # target's own chain-chain contact. True under either reduction, and the
    # reason the warning exists at all.
    assert _INCLUDES_TARGET_INTERFACE.search(body), (
        f"{tool}: the notice no longer says the number includes the target's "
        f"own chain-chain interface, which is the whole claim. body={body!r}"
    )


@pytest.mark.parametrize("tool", BANNER_TOOLS)
def test_banner_is_absent_on_a_single_chain_job(tool, flask_app):
    html = _render_results(flask_app, tool, "A")
    assert NOTICE_MARKER not in html, (
        f"{tool}: notice rendered on a SINGLE-chain job — the common case. "
        f"A caveat shown to everyone is a caveat nobody reads."
    )


@pytest.mark.parametrize("tool", BANNER_TOOLS)
def test_banner_is_absent_when_the_job_has_no_inputs(tool, flask_app):
    """Older job rows predate the inputs column. A missing target_chain must
    read as "not known to be multi-chain", not raise."""
    from flask import render_template

    job = SimpleNamespace(
        id="job-1", tool=tool, status="succeeded", inputs=None,
        result={"candidates": [], "tier": "pilot"},
    )
    with flask_app.test_request_context("/jobs/job-1"):
        html = render_template(
            f"tools/{tool}_results.html", job=job, send_target_tools=None
        )
    assert NOTICE_MARKER not in html


def test_proteina_results_never_carry_the_banner(flask_app):
    """proteina's column is af2_iptm from a separate scoring path, which this
    change did not trace. Not warning is the honest position until it is."""
    html = _render_results(flask_app, "proteina", "A,B")
    assert NOTICE_MARKER not in html


# ---------------------------------------------------------------------------
# The other two call sites: the campaign page and the pooled target page
# ---------------------------------------------------------------------------
#
# WHY THESE ARE RENDERED AND NOT GREPPED. Both used to be "covered" by opening
# the template and asserting the source contains ``multichain_iptm_notice(``.
# That says the call is WRITTEN, and nothing about what it is called WITH or
# whether the branch it sits in is ever taken. Both arguments were mutated with
# the call text left intact --
#
#   runs/detail.html     ``.get('target_chain')``  -> ``.get('target_chains')``
#   targets/detail.html  ``target.target_chain``   -> ``target.chain``
#
# -- and the entire suite stayed byte-identical, while the banner became
# permanently dead on the two views a user actually compares runs in. Both
# mutations resolve to None/Undefined, the macro's guard reads that as "not
# known to be multi-chain", and it renders nothing at all: a silent failure
# with no error to notice.
#
# So these go through the REAL routes, against records shaped like the ones
# production stores, and assert on the rendered ``data-multichain-iptm-notice``
# in BOTH directions -- the same shape as the four job-page call sites above.

_CAMPAIGN_ID = str(uuid.uuid4())
_TARGET_ID = str(uuid.uuid4())


def _ctx():
    return SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com",
    )


def _candidates():
    """One design, because the notice on BOTH pages sits inside the block that
    draws the candidate table. With an empty pooled read neither page renders a
    banner in either direction, and the absent half of each pair would pass for
    a reason that has nothing to do with the chain."""
    return [{
        "pdb_key": "designs/design_0.pdb",
        "sequence": "MKTAY",
        "scores": {"ipTM": 0.91, "pLDDT": 88.0, "filter_status": "pass"},
        "_source_tool": "rfdiffusion",
        "_source_job_id": "job-aaaaaaaa",
        "_source_campaign_id": _CAMPAIGN_ID,
        "_source_chunk": 0,
        "_source_index": 0,
    }]


_COLUMNS = ["ipTM", "pLDDT", "filter_status"]


def _campaign(tool: str, target_chain: str):
    """The minimal campaign ``runs/detail.html`` reads.

    ``params`` is the sanitized submit payload the row actually carries:
    ``shared/compute_campaigns.create_campaign`` stores
    ``sanitize_shared_params(tool, params)``, which drops only
    underscore-prefixed wiring keys, so ``target_chain`` reaches the template
    under the name the form posted it under. That is the fact the grepped
    version could not check and the mutation above exploited.
    """
    return SimpleNamespace(
        id=_CAMPAIGN_ID,
        name="Fc dimer",
        tool=tool,
        status="completed",
        requested_designs=24,
        total_subjobs=6,
        target_name="Fc dimer",
        budget_usd=Decimal("12.00"),
        params={
            "target_chain": target_chain,
            "hotspot_residues": "A296",
            "num_designs": 24,
        },
    )


def _render_run_page(flask_app, tool: str, target_chain: str) -> str:
    """GET /campaigns/<id> — the real route, the real template."""
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    counts = {
        "pending": 0, "running": 0, "succeeded": 6, "failed": 0,
        "timeout": 0, "cancelled": 0, "total": 6,
    }
    agg = {
        "candidates": _candidates(), "columns": _COLUMNS,
        "total": 1, "capped": False,
    }
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.compute_campaigns.get_campaign",
                  return_value=_campaign(tool, target_chain)), \
            patch("shared.compute_campaigns.get_progress_counts",
                  return_value=counts), \
            patch("shared.compute_campaigns.aggregate_campaign_candidates",
                  return_value=agg):
        resp = client.get(f"/campaigns/{_CAMPAIGN_ID}")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def _target(target_chain: str):
    return DesignTarget(
        id=_TARGET_ID,
        user_id="u-1",
        name="Fc dimer",
        filename="fc.pdb",
        storage_path="u-1/target-x/fc.pdb",
        target_chain=target_chain,
        chain_summary={
            "total_standard_residues": 440,
            "chains": [
                {"chain_id": "A", "standard_residue_count": 220,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 1, "max_resnum": 220},
                {"chain_id": "B", "standard_residue_count": 220,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 1, "max_resnum": 220},
            ],
        },
    )


def _render_target_page(flask_app, tools, target_chain: str) -> str:
    """GET /targets/<id> — the real route, the real template.

    ``list_campaigns_for_target`` is patched explicitly rather than left to the
    blanked Supabase env: the route calls it on the empty-runs path this
    envelope produces, and a test whose isolation depends on a client happening
    to be unavailable is not isolated.
    """
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    agg = {
        "ok": True, "partial": False, "candidates": _candidates(), "total": 1,
        "shown": 1, "unranked": 0, "capped": False, "columns": _COLUMNS,
        "tools": list(tools), "per_tool": {}, "campaigns": [],
        "standalone_jobs": 0, "refold_jobs": 0, "passed_total": 1,
        "provisional": False, "sort_mode": "percentile",
        "multi_tool": len(tools) > 1, "limit": 300, "split_tools": [],
    }
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(_target(target_chain),
                                          TARGET_READ_OK)), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=agg), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=[]):
        resp = client.get(f"/targets/{_TARGET_ID}")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


@pytest.mark.parametrize("tool,chain,expected", [
    ("rfdiffusion", "A,B", True),
    ("rfdiffusion", "A", False),
    # The tool half of the same call, so the page cannot be "fixed" into
    # warning about every run.
    ("proteina", "A,B", False),
])
def test_the_campaign_page_notice_follows_the_run_it_describes(
    flask_app, tool, chain, expected
):
    """``runs/detail.html`` reads the chain out of ``campaign.params``, which is
    a dict on the row rather than a column, so a wrong key is not an error —
    it is silence."""
    html = _render_run_page(flask_app, tool, chain)
    assert (NOTICE_MARKER in html) is expected, (
        f"campaign page, tool={tool} chain={chain!r}: "
        f"notice {'missing' if expected else 'rendered'}"
    )


@pytest.mark.parametrize("tools,chain,expected", [
    (["rfdiffusion"], "A,B", True),
    (["rfdiffusion"], "A", False),
    # ``agg.tools`` is a LIST of slugs on this page, and the macro's contract is
    # that the caveat applies if ANY pooled tool ranks on a complex-wide ipTM.
    # Both halves are asserted, so neither the pooling nor the chain can be
    # broken without a failure here.
    (["proteina", "rfdiffusion"], "A,B", True),
    (["proteina"], "A,B", False),
])
def test_the_pooled_target_page_notice_follows_the_target_it_describes(
    flask_app, tools, chain, expected
):
    """``targets/detail.html`` reads ``target.target_chain`` off the row. A
    misspelled attribute is a Jinja ``Undefined``, which the macro's guard
    reads as "not known to be multi-chain" — so the banner disappears with no
    error anywhere."""
    html = _render_target_page(flask_app, tools, chain)
    assert (NOTICE_MARKER in html) is expected, (
        f"target page, tools={tools} chain={chain!r}: "
        f"notice {'missing' if expected else 'rendered'}"
    )


def _render_empty_job(flask_app, tool: str = "rfdiffusion") -> str:
    """A succeeded run that returned nothing.

    Not a corner case: it is where every failed-filter RFdiffusion / PXDesign
    / BindCraft run lands, common enough that
    ``templates/tools/rfdiffusion_results.html`` carries bespoke copy for it
    ("Zero candidates returned."). The notice is called OUTSIDE
    ``results_shell``, so it renders here while the table, the columns and the
    re-fold panel -- all of which live inside ``{% if candidates %}`` -- do
    not.
    """
    from flask import render_template

    job = SimpleNamespace(
        id="job-1", tool=tool, status="succeeded",
        inputs={"target_chain": "A,B"},
        result={"candidates": [], "tier": "pilot"},
    )
    with flask_app.test_request_context("/jobs/job-1"):
        return render_template(
            f"tools/{tool}_results.html", job=job, send_target_tools=None,
        )


@pytest.fixture(scope="module")
def banner_surfaces(flask_app) -> dict:
    """EVERY page the banner renders on, rendered.

    Round 3's regression came from checking the copy against a subset: the
    guard ran on the single-tool target page only, so a sentence that was
    false on the zero-candidate page passed. The fix is not another remembered
    surface, it is a named set that the checks iterate over, so adding a
    surface to it extends every check at once.
    """
    surfaces = {
        f"job {tool}, one candidate": _render_results(flask_app, tool, "A,B")
        for tool in BANNER_TOOLS
    }
    surfaces["job rfdiffusion, ZERO candidates"] = _render_empty_job(flask_app)
    surfaces["campaign"] = _render_run_page(flask_app, "rfdiffusion", "A,B")
    surfaces["target, single-tool"] = _render_target_page(
        flask_app, ["rfdiffusion"], "A,B",
    )
    # Reached by a SINGLE tool at two presets as well as by two tools
    # (shared/target_results.py sets multi_cohort on either), so this is the
    # ordinary multi-chain case and not a corner.
    surfaces["target, multi-cohort"] = _render_target_page(
        flask_app, ["proteina", "rfdiffusion"], "A,B",
    )
    return surfaces


def test_the_banner_does_not_point_at_page_furniture_it_cannot_see(
    banner_surfaces,
):
    """The copy is page-independent, so it may not describe a page.

    The macro takes ``(tool_slug, target_chain)`` and nothing that says which
    of its call sites it is on. FURNITURE IS A WIDGET, A COLUMN, AND A DESIGN,
    and this test has now been through one of each:

      * it used to end "re-fold the top candidates with Boltz-2 below", which
        describes the Second-opinion fold panel components/results_shell.html
        draws — a panel two call sites never draw:
        templates/targets/detail.html calls candidate_table directly and has
        no re-fold control anywhere on the page, and a job page whose run
        returned zero candidates renders the notice while results_shell draws
        the panel only inside its non-empty branch;
      * its replacement then said "Compare designs on pLDDT and the other
        columns in this table", and in MULTI-COHORT mode the pooled target
        page has no metric columns at all;
      * and the fix for THAT kept "the order of this table" and "these
        designs", on a written premise that "every call site draws a candidate
        table directly beneath it". The zero-candidate page refutes it: banner
        present, ``<table>`` count 0, zero ``<th>``.

    THE CHECK IS NOW A CROSS-REFERENCE, NOT A WORD LIST. Every surface is
    rendered; the labels are read off those renders; the banner may not quote
    one. A denylist was defeated twice — once by a phrase it had not heard of,
    once by "beneath" for "below" — and both times the copy shipped false.
    """
    banners = {
        name: _banner_text(html) for name, html in banner_surfaces.items()
    }
    for name, banner in banners.items():
        assert NOTICE_MARKER in banner_surfaces[name], f"{name}: no banner"
        assert banner, f"{name}: the notice rendered with no text in it"
    # One copy everywhere is the property that makes a single set of checks
    # enough. If it ever stops holding, every check below is checking one page.
    assert len(set(banners.values())) == 1, (
        f"the banner is no longer one string across its call sites: "
        f"{ {n: b[:60] for n, b in banners.items()} }"
    )
    banner = next(iter(banners.values()))

    # --- premises, read off the renders rather than restated ---------------
    #
    # These are what make the cross-checks below mean something: if the
    # surfaces stopped differing, "the banner names nothing absent" would be
    # trivially true.
    with_control = {
        n for n, h in banner_surfaces.items() if 'name="dest_tool"' in h
    }
    assert with_control, "no surface has a re-fold control; premise gone"
    assert with_control != set(banner_surfaces), (
        "every surface now has a re-fold control, so the banner could name "
        "it; this test's premise no longer holds and the copy decision "
        "should be revisited"
    )
    empty = banner_surfaces["job rfdiffusion, ZERO candidates"]
    assert _table_headers(empty) == [], (
        "the zero-candidate page has grown a table; the premise that the "
        "banner renders where there is no table no longer holds"
    )
    pooled_headers = _table_headers(banner_surfaces["target, multi-cohort"])
    assert "Score" in pooled_headers and "Pctile" in pooled_headers, (
        f"the multi-cohort table is not in pooled mode; this test is then "
        f"checking the same page twice. headers={pooled_headers!r}"
    )

    # --- the cross-check, in two halves ------------------------------------
    #
    # COLUMNS, from the `<th>` elements. ipTM is exempt: it is the metric the
    # banner is ABOUT, and a warning that cannot name its own subject is
    # useless. Everything else a page calls a column is off limits, and the
    # set updates itself when a column is added or renamed.
    columns = set()
    for html in banner_surfaces.values():
        columns |= _column_labels(html)
    assert {"pLDDT", "Filter"} <= columns, (
        f"the header extractor stopped seeing known columns, so this check "
        f"is guarding nothing: {sorted(columns)!r}"
    )
    forbidden_columns = {c for c in columns if _norm(c) != "iptm"}
    named = _quoted_in(banner, forbidden_columns)
    assert not named, (
        f"the banner names column(s) {named!r}. A column is furniture: the "
        f"multi-cohort pooled table has only Tool/Score/Pctile and the "
        f"zero-candidate page has no columns at all, so naming one is false "
        f"somewhere. banner={banner!r}"
    )

    # AND THE METRICS NO SURFACE DRAWS AT ALL. The `<th>` half can only forbid
    # what some page displays; ``ipAE`` and ``total_reward`` belong to tools
    # that never draw this banner, so naming either is false on all seven.
    off_surface = _metrics_no_surface_displays(banner_surfaces)
    assert {"ipAE", "total_reward"} <= off_surface, (
        f"the derived off-surface metric set no longer holds the two names "
        f"this check was added for; it is deriving something else: "
        f"{sorted(off_surface)!r}"
    )
    named = _quoted_in(banner, off_surface)
    assert not named, (
        f"the banner names metric(s) {named!r}, which no page it renders on "
        f"displays — they belong to tools that never draw this banner. "
        f"banner={banner!r}"
    )

    # CONTROLS AND EVERYTHING ELSE THE PAGES LABEL. Any multi-word label that
    # is not on EVERY surface is off limits. In practice that is nearly all of
    # them, because the zero-candidate page is almost bare — which is the
    # correct conclusion for a macro that cannot tell which page it is on.
    per_surface = {n: _page_labels(h) for n, h in banner_surfaces.items()}
    everywhere = set.intersection(*per_surface.values())
    somewhere = set().union(*per_surface.values())
    # THE DEGENERACY, STATED. Measured, the intersection is EMPTY: the four job
    # pages are rendered as their results PARTIAL (no shell) while the campaign
    # and target pages are full routes, so no label survives all seven. That
    # makes the rule below "quote no multi-word label at all" — strict, and
    # deliberately so, but only by accident of the render set. Asserted so it
    # is a stated property: if a future change gives all seven a common shell,
    # ``everywhere`` becomes non-empty, the forbidden set silently shrinks, and
    # nothing else here would fail.
    assert everywhere == set(), (
        f"the banner surfaces now share label(s) {sorted(everywhere)!r}. That "
        f"is not a failure in itself — it means the copy may name them — but "
        f"it shrinks the forbidden set below, so decide it on purpose rather "
        f"than discovering it"
    )
    not_universal = {
        lab for lab in somewhere - everywhere if len(lab.split()) >= 2
    }
    assert "Second-opinion fold" in not_universal, (
        "the re-fold panel's own label is no longer extracted as a label, so "
        "this check would not catch the defect it exists for"
    )
    named = _quoted_in(banner, not_universal)
    assert not named, (
        f"the banner names {named!r}, which is on some of the pages it "
        f"renders on and not others. It takes no parameter that could tell "
        f"the difference. banner={banner!r}"
    )

    # --- and the things a cross-reference cannot see ------------------------
    assert not _POINTS_AT_FURNITURE.search(banner), (
        f"the banner uses a locative, so it promises something at a place on "
        f"the page: {banner!r}"
    )
    deictic = _DEICTIC_FURNITURE.search(banner)
    assert not deictic, (
        f"the banner says {deictic.group(0)!r} — a deictic pointed at page "
        f"furniture. On the zero-candidate page there is no table, no column "
        f"and no design for it to point at. banner={banner!r}"
    )
    content = _ASSERTS_PAGE_CONTENT.search(banner)
    assert not content, (
        f"the banner says {content.group(0)!r} — a claim that content is on "
        f"the page, or that the reader has already acted on it. The macro "
        f"cannot see either, and on the zero-candidate page both are false. "
        f"banner={banner!r}"
    )
    action = _PAGE_ACTION.search(banner)
    assert not action, (
        f"the banner says {action.group(0)!r} — an instruction to operate a "
        f"control it cannot know is there. banner={banner!r}"
    )


def test_the_banner_is_true_on_a_job_page_with_zero_candidates(
    flask_app, banner_surfaces,
):
    """The surface round 3 wrote a comment excusing instead of rendering.

    The comment said "``of this table`` is deliberately NOT matched … because
    every call site draws a candidate table directly beneath it". Rendered,
    this page carries the banner with no table, no columns, no designs and no
    re-fold panel — and the copy at the time claimed all four.

    This is the strictest surface the macro has, so it gets its own test
    rather than only membership in the set above: everything the banner could
    point at is absent here, which makes it the one page where a page-shaped
    claim cannot hide.
    """
    empty = banner_surfaces["job rfdiffusion, ZERO candidates"]
    assert NOTICE_MARKER in empty, (
        "the notice no longer renders with zero candidates. That is a "
        "defensible product change, but it is the premise of this test and "
        "of point 3 in the macro's comment"
    )

    # What is actually on the page — asserted, not assumed.
    body = _Text()
    body.feed(empty)
    visible = body.text
    assert "Zero candidates returned" in visible
    assert empty.count("<table") == 0, "there is a table after all"
    assert _table_headers(empty) == [], "there are columns after all"
    assert 'name="dest_tool"' not in empty, "there is a re-fold control"
    assert "Second-opinion" not in visible, "the re-fold panel is on the page"

    banner = _banner_text(empty)
    assert banner, "the notice rendered with no text in it"

    # So the banner may not claim any of them. Same checks as the guard test,
    # but scoped to THIS page's furniture, which is empty — so the only copy
    # that passes is copy that describes the metric and the remedy.
    assert not _quoted_in(banner, _page_labels(empty) | _column_labels(empty))
    assert not _DEICTIC_FURNITURE.search(banner), (
        f"the banner points at page furniture on a page that has none: "
        f"{banner!r}"
    )
    assert not _POINTS_AT_FURNITURE.search(banner), banner
    assert not _ASSERTS_PAGE_CONTENT.search(banner), (
        f"the banner claims content is on a page whose run returned none, or "
        f"that the reader has acted on it: {banner!r}"
    )
    assert not _PAGE_ACTION.search(banner), (
        f"the banner tells the reader to operate a control on a page that has "
        f"none: {banner!r}"
    )
    # The specific three clauses the round-3 review caught here, named so a
    # revert reads as a revert rather than as an anonymous regex failure.
    for clause in ("this table", "these designs", "second-opinion fold"):
        assert clause not in banner.lower(), (
            f"the banner says {clause!r} on a page with no table, no designs "
            f"and no Second-opinion fold panel: {banner!r}"
        )


# ---------------------------------------------------------------------------
# The macro is wired everywhere a candidate table is drawn
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", [
    "tools/rfdiffusion_results.html",
    "tools/pxdesign_results.html",
    "tools/bindcraft_results.html",
    "tools/boltzgen_results.html",
])
def test_every_candidate_table_page_calls_the_notice(template, flask_app):
    """A completeness check over the four job-result partials, whose rendered
    behaviour in both directions is pinned above.

    boltzgen is still here although it no longer trips the notice. The call is
    the page asking a shared decision function, not the page deciding; leaving
    it means a future change to MULTICHAIN_IPTM_UNRELIABLE_TOOLS reaches every
    results view at once instead of one that quietly lost its wiring.

    ``runs/detail.html`` and ``targets/detail.html`` used to be in this list and
    are deliberately no longer. A source grep was the ONLY thing covering them,
    and it could see neither argument; they are rendered through their real
    routes above instead."""
    path = flask_app.jinja_env.get_or_select_template(template).filename
    body = open(path, encoding="utf-8").read()
    assert "multichain_iptm_notice(" in body, (
        f"{template} renders a candidate table but never calls the notice"
    )


def test_the_boltzgen_legend_describes_both_sides_of_the_deploy(flask_app):
    """The tooltip is now the only place the era distinction is made, so it
    has to make it — in both directions.

    An earlier draft of this file asserted the legend must NOT say
    binder-to-target, because the deploy had not happened. It has, so that
    assertion would now pin a false claim, which is worse than no test. What
    replaces it is not the mirror image: the legend has to say what the
    number IS today AND what an older multi-chain run stored, because a
    results page shows whatever the job saved and nothing in the record says
    which container produced it.
    """
    from shared.score_legends import get_legend, legend_text

    legend = get_legend("boltzgen", "ipTM")
    # ASSERTED ON WHAT THE TABLE SHOWS, which is ``explanation`` plus the
    # optional ``caveat``. The era distinction sits in ``caveat`` and not in
    # ``explanation`` because ``explanation`` is a one-line slot shared by 32
    # legends and is handed to the job-completion email for every tool — NOT
    # because the email is exempt from caveats. It is not: complete_job also
    # runs from the stuck-job sweeper, the inline poll and
    # scripts/finalize_stuck_job.py, so that mail can be about a result read
    # back out of Storage, and shared/score_legends.email_caption gives it the
    # caveat when the job's target names more than one chain. The tooltip half
    # of the split is pinned in tests/test_target_table_render.py, the email
    # half in tests/test_job_complete_email_caption.py; this asserts the
    # CONTENT, so that neither half can be satisfied by a legend that says
    # less.
    explanation = legend_text(legend)
    assert "binder-to-target" in explanation, (
        "the legend still describes a value the deployed container no longer "
        "emits"
    )
    assert "chain-chain" in explanation, (
        "the legend drops the caveat that a pre-deploy multi-chain run stored "
        "the complex-wide number"
    )
    assert "older run" in explanation, (
        "the legend states the current meaning without saying older runs "
        "differ, which reads as if every stored value were binder-to-target"
    )
    # BOTH HALVES OF THE MOVED CAVEAT, pinned together so they cannot separate
    # again. The banner said "these designs are ALSO RANKED BY IT, so both the
    # values and the ORDER of this table should be treated as indicative only",
    # and the first move brought the value half only — the words "rank" and
    # "order" then appeared nowhere on a pre-deploy boltzgen results page.
    # boltzgen ranks on ipTM (shared/result_columns.py) and the pooled reads
    # sort then truncate at limit=300, so the order decides which designs are
    # visible at all. Asserted as two words rather than a phrase so the
    # sentence can be reworded.
    assert "ranked" in explanation, (
        "the legend disclaims the VALUE but never says the designs are "
        "ranked on it, which is the half that decides what the user sees"
    )
    assert "order" in explanation, (
        "the legend never says the ORDER is affected; past limit=300 the "
        "ipTM order decides which designs appear at all"
    )
    # Thresholds were calibrated on single-chain runs where the two keys nearly
    # coincide, so they remain the best available anchor and must not drift
    # silently alongside a wording change.
    assert legend["good"] == 0.7
    assert legend["excellent"] == 0.8


# ---------------------------------------------------------------------------
# The general pages that describe ipTM for every tool at once
# ---------------------------------------------------------------------------

# The claim no page may make unqualified. ipTM's INTENT is the binder-to-target
# pair, and stating it as fact is false today for rfdiffusion, pxdesign and
# bindcraft on a multi-chain target, and for any boltzgen run predating the
# August 2026 container update. Dash-agnostic and space-tolerant, so a
# rewording between "binder to target" and "binder-to-target" does not slip
# past.
_CLAIMS_THE_BINDER_PAIR = re.compile(r"binder.to.target interface", re.I)

# The qualifier that makes it honest, IN TWO PARTS, because one part is what
# the round-3 review walked through. The old check asked only for the string
# "multi-chain" within 400 characters of the claim, so restoring the banned
# sentence verbatim and adding "Multi-chain targets are supported by most
# tools here." beside it turned the test green with the defect back on the
# page. Proximity to the WORD is not qualification.
#
# A real qualifier states the condition AND the consequence: on a multi-chain
# target, the number covers something other than the binder pair. Either half
# alone is satisfiable by copy that qualifies nothing.
_QUALIFIES_MULTI_CHAIN = re.compile(r"multi.chain", re.I)
_NAMES_THE_CONSEQUENCE = re.compile(
    r"chain.chain|whole complex|complex.wide|target'?s own|"
    r"more than the binder|not (?:only|just) the binder",
    re.I,
)

# Tags that end a run of visible text. The check runs per BLOCK rather than
# over a character window: a `<dd>` is the unit in which a definition either
# is or is not qualified, and a window is a guess about layout.
_BLOCK_TAGS = frozenset(
    "address article aside blockquote br dd details div dl dt fieldset "
    "figcaption figure footer form h1 h2 h3 h4 h5 h6 header hr legend li "
    "main nav ol option p pre script section style table tbody td tfoot th "
    "thead tr ul".split()
)


class _TextBlocks(HTMLParser):
    """Visible text, split at every block boundary.

    Inline markup (``<strong>``, ``<a>``) does NOT split, so a sentence
    wrapped in emphasis stays with its neighbours; a new ``<dd>`` or ``<p>``
    does, so an unrelated paragraph cannot qualify the one before it.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._current: list = []
        self.blocks: list = []

    def _flush(self):
        text = " ".join("".join(self._current).split())
        if text:
            self.blocks.append(text)
        self._current = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        self._current.append(data)

    def close(self):
        super().close()
        self._flush()


def _blocks(html: str) -> list:
    parser = _TextBlocks()
    parser.feed(html)
    parser.close()
    return parser.blocks


def _visible(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return parser.text


def _reachable_pages(flask_app) -> dict:
    """Every page a logged-out visitor can GET, rendered.

    NOT A LIST OF PATHS. The check this feeds used to name
    ``/help/tools/rfdiffusion`` and ``/tools/rfdiffusion``, and the commit that
    wrote that list had just learned the lesson against it — an independent
    review found two surfaces carrying the claim and there were three. A
    fourth is found the same way: not at all.

    So the routes come from ``url_map``. Every GET rule with no arguments,
    plus the two per-tool families, for every adapter in the registry rather
    than for a slug someone remembered. Non-200s are skipped: most are the
    login redirect, and a page a signed-out visitor cannot reach is not a page
    this check is about. The floor assertion in the test is what stops that
    skip from quietly emptying the sweep.
    """
    from tools import base as tool_base

    client = flask_app.test_client()
    slugs = sorted(a.slug for a in tool_base.all_adapters())
    rules = sorted({
        rule.rule
        for rule in flask_app.url_map.iter_rules()
        if "GET" in (rule.methods or set())
        and not rule.arguments
        and not rule.rule.startswith("/static")
    })
    rules += [f"/help/tools/{s}" for s in slugs]
    # Requested with NO session, which is what selects the preview shell
    # rather than the form. ``tool_enabled`` is patched because the flag is
    # off in a bare test env and the route answers 404 — the flag is not what
    # is under test here.
    rules += [f"/tools/{s}" for s in slugs]

    pages = {}
    with patch("blueprints.tools.tool_enabled", return_value=True):
        for rule in rules:
            try:
                resp = client.get(rule)
            except Exception:  # noqa: BLE001, S110
                continue  # a route that errors is a different test's business
            if resp.status_code == 200:
                pages[rule] = resp.get_data(as_text=True)
    return pages


def test_no_general_page_states_iptm_as_the_binder_pair(flask_app):
    """A page that cannot know the tool must not make the per-tool claim.

    ipTM's INTENT is the binder-to-target pair. Stating it as fact is false
    today for rfdiffusion, pxdesign and bindcraft on a multi-chain target, and
    for any boltzgen run predating the August 2026 container update. The
    per-tool legend and the multi-chain banner exist because of that; a
    general page repeating it as fact undoes them one click away.

    SWEPT, NOT LISTED, and QUALIFIED, NOT MERELY NEARBY — the two things the
    round-3 review took off this check. It is applied to every page a
    logged-out visitor can reach, and the qualifier has to name the
    consequence and not only the words "multi-chain".

    ASSERTED ON RENDERED TEXT, NOT SOURCE. The fix left explanatory comments
    in both templates that quote the banned phrase in order to ban it, so a
    source grep would fail on the fix itself. HTMLParser routes comments to
    handle_comment, which _Text ignores.
    """
    pages = _reachable_pages(flask_app)
    describes_iptm = {
        path for path, html in pages.items() if "ipTM" in _visible(html)
    }
    # THE FLOOR. Skipping non-200s could otherwise empty this sweep without a
    # failure, and these two are the surfaces the claim was actually found on.
    assert {"/help/tools/rfdiffusion", "/tools/rfdiffusion"} <= describes_iptm, (
        f"the sweep no longer reaches the two pages this check was written "
        f"for; it is covering something other than what it claims. reached "
        f"{len(pages)} pages, {sorted(describes_iptm)!r} mention ipTM"
    )

    offenders = {}
    for path, html in pages.items():
        for block in _blocks(html):
            if not _CLAIMS_THE_BINDER_PAIR.search(block):
                continue
            if _QUALIFIES_MULTI_CHAIN.search(block) and \
                    _NAMES_THE_CONSEQUENCE.search(block):
                continue
            offenders[path] = block
    assert not offenders, (
        "page(s) state ipTM as the binder-to-target interface without saying, "
        "in the same block, that on a MULTI-CHAIN target the number covers "
        f"the target's own chain-chain interface too: {offenders!r}"
    )


class _Tooltips(HTMLParser):
    """Every ``data-tooltip`` on the page. HTMLParser unescapes attribute
    values, so the assertions see the string the user's browser shows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tooltips: list = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "data-tooltip" and value:
                self.tooltips.append(value)


def _iptm_tooltip(html: str) -> str:
    parser = _Tooltips()
    parser.feed(html)
    hits = [t for t in parser.tooltips if "Interface pTM" in t]
    assert len(hits) == 1, (
        f"expected exactly one ipTM tooltip, got {len(hits)}: {hits!r}"
    )
    return hits[0]


def test_the_boltzgen_iptm_tooltip_does_not_contradict_itself(flask_app):
    """One string, one meaning.

    components/candidate_table.html CONCATENATES the per-tool legend with the
    global metric_glossary entry into a single ``data-tooltip``. The legend now
    carries the pre-deploy caveat — "an older run stored a complex-wide value
    instead" — and the glossary used to end four words later with "Measures
    structural confidence at the binder–target interface SPECIFICALLY". Two
    statements on one screen, one of them false, is exactly the failure the
    legend rewrite existed to fix; putting them inside a single tooltip is that
    failure at its smallest possible scale.

    The glossary is global — it is shown for every tool's ipTM column — so it
    cannot be the surface that says which interface the number covers. The
    legend can, and does.

    Both halves are asserted present first, because a tooltip that stopped
    stacking them would satisfy the contradiction check by saying nothing.
    """
    tooltip = _iptm_tooltip(_render_results(flask_app, "boltzgen", "A,B"))
    assert "complex-wide" in tooltip, (
        f"the legend half is gone from the tooltip: {tooltip!r}"
    )
    assert "Template Modeling" in tooltip, (
        f"the glossary half is gone from the tooltip: {tooltip!r}"
    )
    assert not re.search(
        r"binder.target interface\s+(specifically|alone|only)", tooltip, re.I,
    ), (
        f"the tooltip disclaims the value as possibly complex-wide and then "
        f"asserts it is the binder-target pair and nothing else: {tooltip!r}"
    )


def test_boltzgen_results_no_longer_carry_the_banner(flask_app):
    """The decision, at the seam a user actually sees.

    The frozenset test above is the unit; this is the page. boltzgen still
    CALLS the macro — templates/tools/boltzgen_results.html is in the
    completeness check below — so the wiring stays and only the shared
    decision function changes. That is deliberate: the page asks, one place
    answers.
    """
    html = _render_results(flask_app, "boltzgen", "A,B")
    assert NOTICE_MARKER not in html, (
        "a boltzgen run gets the banner again; its container reports the "
        "binder-to-target interface, so the banner's mechanism sentence is "
        "false about it"
    )
