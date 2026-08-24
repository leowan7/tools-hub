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

import atexit
import os
import re
import shutil
import sys
import tempfile
import textwrap
import uuid
from contextlib import ExitStack
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
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
#
# WHAT THESE REGEXES ARE, STATED SO NOBODY READS THEM AS MORE. They are a
# HEURISTIC BACKSTOP, not a proof. They do not decide whether copy is true;
# they catch the handful of English constructions that have made it false here
# before. Five review rounds have each closed the previous round's survivors
# and found new ones, and the last round's survivors were defeated by a
# two-letter prefix ("second-opinion REfold"), a possessive determiner ("YOUR
# top designs"), a noun outside a seven-item list ("the top ten"), a verb
# outside a verb list ("Download the best few") and a metric's long name. That
# is an arms race against English and it does not terminate. It also has a
# cost that is not hypothetical: twice now a term added to close a survivor
# has gone on to refuse honest copy, and the second time the word was "fold",
# in a protein-design app.
#
# So the rule for anyone tempted to add a word here: DON'T. The defence that
# actually scales is
# ``test_the_banner_is_true_on_a_job_page_with_zero_candidates``, which PRINTS
# the rendered banner against the emptiest surface the macro has. Ten seconds
# of reading it catches the whole class these regexes chase, including every
# survivor listed above. Run the suite with ``-s`` and read the copy.

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
# neither ``form`` nor ``shortlist`` was here. It is still a list, which is a
# real weakness -- but it is a list of KINDS of page part, not of phrasings, so
# a synonym for "below" or a missing hyphen does not defeat it.
#
# ``fold|folds`` WAS ALSO ADDED, AND IS NOW REMOVED, because it carried no
# coverage and cost honest copy. It was added for the re-fold panel, whose
# label is "Second-opinion fold" -- and the LABEL CROSS-CHECK already catches
# that, from the rendered page rather than from a list. Measured: with
# ``fold`` deleted, both walk-arounds an independent review used ("the second
# opinion fold", "the second-opinion fold") stay REFUSED, by
# ``label:['Second-opinion fold']``; "the re-fold form" stays refused by
# ``form``; and all eight of round 4's survivors stay refused. What changes is
# that "Designs that share the same fold can still differ at the interface"
# becomes writable again. In a protein-design app "fold" is the central noun
# of the domain -- "the same fold", "the native fold", "the correct fold" --
# and none of them names anything on a page. This is the round-3 NIT-7 mistake
# (a guard that rejects true copy) on a new axis, and the cheapest correct fix
# for a denylist term that is also ordinary domain vocabulary is to delete it
# and let the check that reads the actual page do the work.
# ``test_the_guard_permits_honest_copy_about_the_metric`` pins it.
_FURNITURE_NOUN = (
    r"table|tables|column|columns|row|rows|panel|panels|list|lists|"
    r"page|pages|button|buttons|control|controls|menu|menus|widget|widgets|"
    r"form|forms|field|fields|link|links|tab|tabs|"
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
#   * PRESENCE ADVERBS -- shown / listed / displayed / visible -- ANCHORED to
#     a place, plus a bare ``here``.
#
#     THE ANCHOR IS NEW AND IT IS A NARROWING. These four were banned
#     unconditionally, which refused "A value SHOWN for a multi-chain target is
#     not comparable to one for a single-chain target" and "The inflation is
#     not VISIBLE in the number itself" -- two sentences that describe the
#     metric, name no page part, and are exactly what this banner is for.
#     A bare passive is not a claim about the page; what makes it one is an
#     anchor, and the anchor is a place. Measured against all eight of round
#     4's survivors, the unanchored form carried ZERO coverage: "Only the top
#     300 designs are shown here" is refused by the definite plural "the top
#     300 designs" AND by ``here``, and every other survivor is refused by a
#     different grammar entirely. So the anchor loses nothing that was ever
#     caught and buys back copy a careful writer wants.
#
#     ``here`` stays bare and is now load-bearing: with the adverbs anchored
#     it is the only thing left that refuses "shown here" in the absence of a
#     plural. What DOES become writable is a claim with neither -- "Only the
#     top 300 are shown." Named rather than papered over; see the note above
#     these regexes about why the answer to that is the printed banner and not
#     a ninth alternation.
_ASSERTS_PAGE_CONTENT = re.compile(
    r"\bthe\s+(?:\w[\w-]*[\s-]+){0,2}"
    r"(?:designs|candidates|results|rows|entries|hits|ones)\b"
    r"|\b(?:designs?|candidates?|results?|rows?|ones|shortlists?|list)\b"
    r"[^.]{0,24}\byou\b"
    r"|\b(?:shown|listed|displayed|visible)\s+(?:here|below|above|on this)\b"
    r"|\bhere\b",
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


def _guard_hits(copy: str) -> list[str]:
    """Which of the four grammars refuse ``copy``, and on what.

    The shipped regex objects, not a replication of them, so this cannot drift
    from what the two banner tests run.
    """
    hits = []
    for name, rx in (
        ("deictic", _DEICTIC_FURNITURE),
        ("page-content", _ASSERTS_PAGE_CONTENT),
        ("page-action", _PAGE_ACTION),
        ("locative", _POINTS_AT_FURNITURE),
    ):
        found = rx.search(copy)
        if found:
            hits.append(f"{name}:{found.group(0)!r}")
    return hits


# Copy a careful writer would want, which the guard must NOT refuse. Every
# entry describes the METRIC or the REMEDY and names no page part, so it is
# true on all seven surfaces including the zero-candidate one.
#
# THIS TEST EXISTS BECAUSE THE GUARD HAS TWICE STARTED REFUSING TRUE COPY, and
# each time the term added to close a survivor was also ordinary vocabulary.
# A denylist that only gets checked in the false direction ratchets in one
# direction forever; this is the ratchet's other pawl.
_HONEST_COPY = [
    # Round 5 put ``fold`` in _FURNITURE_NOUN, in an app whose entire subject
    # is protein folds. Refused as "deictic 'the same fold'".
    "Do not choose between designs on ipTM alone. Designs that share the same "
    "fold can still differ at the interface.",
    # ...and banned ``shown`` unconditionally. Refused as "page-content
    # 'shown'", although the sentence is about a NUMBER, not about the page.
    "A value shown for a multi-chain target is not comparable to one for a "
    "single-chain target.",
    # Same construction, same cause, on ``visible``.
    "The inflation is not visible in the number itself.",
    # Round 3's NIT-7 controls. Kept here so this test also pins THAT fix: an
    # unanchored ``above`` refused the first, and banning ``the design`` would
    # have refused the second.
    "Treat a value above 0.8 with the same suspicion.",
    "Do not pick the design with the highest ipTM.",
    # The banner's own generic-English constructions, which round 4 blessed
    # deliberately: a BARE plural is about the tool, not about this page.
    "Designs are ranked by it, so a mediocre binder can rank first.",
]


@pytest.mark.parametrize("copy", _HONEST_COPY, ids=range(len(_HONEST_COPY)))
def test_the_guard_permits_honest_copy_about_the_metric(copy):
    assert _guard_hits(copy) == [], (
        f"the guard refuses copy that names no page furniture and is true on "
        f"every surface, including the zero-candidate one: {copy!r}. A "
        f"denylist term that is also ordinary domain vocabulary costs more "
        f"than it catches -- narrow or delete it rather than reword the copy."
    )


# The other direction, so the narrowing above is pinned as a narrowing and not
# as a hole. Every one of these is a round-4 survivor that round 5 closed, and
# each must still be refused by a GRAMMAR here.
#
# "the second opinion fold" is deliberately NOT in this list: it is refused by
# the LABEL cross-check reading "Second-opinion fold" off the rendered page,
# which is unit-tested two tests above, and that is precisely why ``fold``
# could come out of _FURNITURE_NOUN.
_PAGE_SHAPED_COPY = [
    "The designs you are comparing were ranked on it.",
    "Scroll down to the ranked designs.",
    "Only the top 300 designs are shown here.",
    "Re-run the top designs with the re-fold form.",
    "Star the ones you want and re-fold them.",
    "Re-fold the shortlist you starred.",
    "The value is shown below.",
]


@pytest.mark.parametrize("copy", _PAGE_SHAPED_COPY,
                         ids=range(len(_PAGE_SHAPED_COPY)))
def test_the_guard_still_refuses_page_shaped_copy(copy):
    assert _guard_hits(copy), (
        f"the guard permits copy that claims page content the macro cannot "
        f"see, and which is false on the zero-candidate page: {copy!r}"
    )


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

    AND IT PRINTS THE COPY, EVERY RUN. That is the point of this test, more
    than the four regexes below it. Five review rounds of regexes have each
    closed the previous round's survivors and found new ones, because the
    class is "a sentence in English that is false on an empty page" and no
    denylist enumerates it. A human reading the actual rendered banner against
    the emptiest surface the macro has catches all of them in about ten
    seconds — including every survivor those rounds turned up, none of which
    quotes a label or names a noun any list could hold.

    So: ``pytest -s tests/test_multichain_iptm_notice.py -k zero_candidates``
    prints the banner and the emptiness facts. If you are changing this copy,
    that is the review. pytest also shows the block on failure without ``-s``,
    which is when it is needed most.
    """
    empty = banner_surfaces["job rfdiffusion, ZERO candidates"]
    assert NOTICE_MARKER in empty, (
        "the notice no longer renders with zero candidates. That is a "
        "defensible product change, but it is the premise of this test and "
        "of point 3 in the macro's comment"
    )

    # What is actually on the page — measured first so the block below can
    # print it, then asserted. Same facts, same assertions; the difference is
    # that they are now legible rather than only checked.
    body = _Text()
    body.feed(empty)
    visible = body.text
    banner = _banner_text(empty)
    facts = [
        ('page says "Zero candidates returned"',
         "Zero candidates returned" in visible),
        ("<table> elements on the page", empty.count("<table")),
        ("column headers on the page", _table_headers(empty)),
        ("re-fold control (name=\"dest_tool\")", 'name="dest_tool"' in empty),
        ('"Second-opinion" anywhere in the visible text',
         "Second-opinion" in visible),
        ("multi-word labels the page does carry",
         sorted(_page_labels(empty))[:6] or "none"),
    ]
    print("\n" + "=" * 72)
    print("THE MULTI-CHAIN ipTM BANNER, RENDERED ON THE EMPTIEST PAGE IT HAS")
    print("(job rfdiffusion, zero candidates — read this against the facts "
          "below it)")
    print("=" * 72)
    for line in textwrap.wrap(banner or "<EMPTY>", width=72):
        print("  " + line)
    print("-" * 72)
    for label, value in facts:
        print(f"  {label:<45} {value!r}")
    print("=" * 72)
    print("  Every claim the banner makes must be true with all of that "
          "absent.")
    print("=" * 72)

    assert facts[0][1]
    assert facts[1][1] == 0, "there is a table after all"
    assert facts[2][1] == [], "there are columns after all"
    assert not facts[3][1], "there is a re-fold control"
    assert not facts[4][1], "the re-fold panel is on the page"

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
    # THE THRESHOLDS ARE GONE, AND THE COMMENT THAT JUSTIFIED THEM WAS WRONG.
    # It read: "calibrated on single-chain runs where the two keys nearly
    # coincide, so they remain the best available anchor". The two keys do
    # coincide on a single-chain target, and on the audited replicate of that
    # protocol the coincident value spans 0.084-0.583, 0/100 over 0.70.
    # (Six single-chain production runs, 65 candidates, max 0.659 — but read
    # the column note below before pooling those two figures.)
    # 0.7/0.8 were not calibrated on anything, they were copied from the
    # ("boltz2", "ipTM") entry directly below, which IS the calibrated cofold
    # and is a different measurement. The same designs re-scored on a real
    # Boltz-2 cofold, read on the CLEAN per-chain-pair column, span
    # 0.166-0.806 with 1/29 over 0.70.
    #
    # QUOTE THE RIGHT COLUMN. The widely-cited "460 designs, max 0.650" is
    # BoltzGen's bare `iptm`, an interface-pTM averaged over EVERY chain pair,
    # so on the Fel d 1 homodimer it carries the target's own A:B crystal
    # interface — the very contamination this banner exists to warn about.
    # boltzgen-workspace/aglyco-fc-vhh/modal_design.py records that it "read
    # ~2x high" and that all 460 were concluded on it; 13_boltz_cofold.py adds
    # that the binder-interface column was dropped inside the container for
    # those runs and is unrecoverable. On the audited n100 the two sit side by
    # side: bare `iptm` 0.450-0.649, `design_to_target_iptm` 0.084-0.583. The
    # legend describes the SECOND one, so cite that.
    #
    # So the anchor is absent rather than corrected: nothing pairs the in-run
    # number against a cofold on the same designs, so there is no bar to state.
    # Pinned in full by tests/test_boltzgen_iptm_has_no_cofold_bar.py; asserted
    # here too because this is the test a wording change runs, and a bar that
    # says one thing while the wording says another is how the two drift.
    assert "good" not in legend and "excellent" not in legend, (
        f"boltzgen ipTM claims good={legend.get('good')} / "
        f"excellent={legend.get('excellent')} again — 0.7 is the Boltz-2 "
        f"cofold bar, measured on a fold this run does not perform"
    )
    assert "0.7 does not apply" in explanation, (
        "the legend dropped the bar from its data but no longer tells the "
        "reader the 0.7 scale they know from every other tool here is the "
        "wrong one to apply"
    )


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
#
# ``target'?s own`` USED TO BE ONE OF THE CONSEQUENCE ALTERNATIVES AND IS GONE,
# because it is not a consequence -- it is two ordinary words that this app
# says constantly. The round-4 review restored the banned flat claim and put
#
#   "Multi-chain targets are supported by most tools here, and hotspots are
#    always given in the target's own numbering."
#
# in the same `<dd>`: "Multi-chain" satisfied one half, "the target's own
# numbering" the other, and the defect was back on the page with the suite
# green. Hotspots in the target's own numbering is REAL COPY here (see
# templates/tools/*_form.html), so the phrase had to go, not the sentence. What
# is left names the mechanism itself.
_QUALIFIES_MULTI_CHAIN = re.compile(r"multi.chain", re.I)
_NAMES_THE_CONSEQUENCE = re.compile(
    r"chain.chain|whole complex|complex.wide|"
    r"more than the binder|not (?:only|just) the binder|"
    r"as well as the binder",
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


def _sentences(block: str) -> list:
    """A block split at sentence ends.

    A `<dd>` was the unit until the round-4 review showed that is far too
    coarse: two unrelated true sentences elsewhere in the same definition
    qualified a flat claim at the top of it. A decimal threshold ("above
    roughly 0.7 on a tractable target") is safe because the split needs
    whitespace after the stop.
    """
    return [s for s in re.split(r"(?<=[.!?])\s+", block) if s.strip()]


def _visible(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return parser.text


def _sweep_rules(flask_app) -> list:
    """The paths the sweep requests, from ``url_map`` rather than from memory.

    NOT A LIST OF PATHS. The check this feeds used to name
    ``/help/tools/rfdiffusion`` and ``/tools/rfdiffusion``, and the commit that
    wrote that list had just learned the lesson against it — an independent
    review found two surfaces carrying the claim and there were three. A
    fourth is found the same way: not at all.

    Every GET rule with no arguments, plus the two per-tool families for every
    adapter in the registry rather than for a slug someone remembered.
    """
    from tools import base as tool_base

    slugs = sorted(a.slug for a in tool_base.all_adapters())
    rules = sorted({
        rule.rule
        for rule in flask_app.url_map.iter_rules()
        if "GET" in (rule.methods or set())
        and not rule.arguments
        and not rule.rule.startswith("/static")
    })
    rules += [f"/help/tools/{s}" for s in slugs]
    rules += [f"/tools/{s}" for s in slugs]
    return rules


_REPO_TMP = Path(__file__).resolve().parents[1] / "tmp"


def _tmp_entries() -> dict:
    """Every path under the repo's ``tmp/``, recursively, with file sizes.

    NAMES AT ONE LEVEL WAS HALF A CHECK, and the missing half is where the
    incident actually is. ``tmp/calibration/dispatched.json`` and
    ``results.json`` are TRACKED files, one level DOWN. ``cleanup_old_jobs``
    happened to rmtree the whole directory, so the name vanished and a
    one-level name set caught it — but a route that deleted, truncated or
    rewrote either file while leaving ``tmp/calibration/`` standing left that
    set byte-identical, and the test below claimed to close the class.
    Recursive, and with sizes, so a rewrite in place fails here too.

    Cheap: the repo's ``tmp/`` holds two files in one directory.
    """
    if not _REPO_TMP.exists():
        return {}
    return {
        str(p.relative_to(_REPO_TMP)):
            (p.stat().st_size if p.is_file() else "dir")
        for p in _REPO_TMP.rglob("*")
    }


# ONE scratch job dir for the whole process, removed at exit. It used to be a
# fresh ``mkdtemp`` per swept scout route with no cleanup, and
# ``_reachable_pages`` is a plain function called by two tests, so a full run
# left several behind in the system temp dir. Outside the repo, so
# ``git status`` never saw them — litter rather than a defect, but free to stop.
_SCRATCH_JOB_DIR: list = []


def _scratch_job_dir():
    """A job dir OUTSIDE the repo, for the one swept route that creates one."""
    if not _SCRATCH_JOB_DIR:
        made = Path(tempfile.mkdtemp(prefix="iptm-sweep-"))
        _SCRATCH_JOB_DIR.append(made)
        atexit.register(shutil.rmtree, made, True)
    path = _SCRATCH_JOB_DIR[0]
    return path.name, path


def _reachable_pages(flask_app) -> dict:
    """Every page the sweep can render, SIGNED OUT AND SIGNED IN.

    THE SIGNED-IN PASS IS NOT AN EXTRA. It used to be load-bearing for a
    different reason: being signed out selected ``templates/tools/_preview.html``
    over ``templates/tools/<slug>_form.html``, so a signed-out-only sweep
    structurally could not see the FORM. That preview shell is gone —
    ``/tools/<slug>`` renders the real form in both auth states now — but the
    signed-in pass still reaches strictly more: the ~37 rules that answer a
    login redirect when signed out, and the wallet/balance rows the form only
    renders when a wallet is in play. Measured before the change, the
    signed-out pass reached 46 pages and the signed-in pass 65 of the same 83
    rules.

    ``load_user_context`` is patched in every ``blueprints.*`` module that
    imported it, discovered by walking ``sys.modules`` rather than listed, so a
    new blueprint joins the sweep without an edit here — which is the same
    reason the routes come from ``url_map``. ``tool_enabled`` is patched
    because the flag is off in a bare test env and the route would answer 404,
    and ``get_or_create_wallet`` because the form's first-paint balance reads
    it; neither flag is what is under test.

    WHAT THIS STILL DOES NOT COVER, stated so nobody reads it as total:

      * PARAMETRISED ROUTES, excluded by ``not rule.arguments`` — ``/jobs/<id>``,
        ``/campaigns/<id>``, ``/targets/<id>``. Those are the RESULTS surfaces,
        and they are rendered through their real routes by ``_render_run_page``
        and ``_render_target_page`` and directly by ``_render_results``
        elsewhere in this file; what is not covered is a parametrised route
        carrying general ipTM prose that is not a results view.
      * The ~18 rules that answer non-200 even signed in (admin pages, billing
        redirects, ``/healthz``). Skipped rather than asserted, which is what
        the floor assertion in the test exists to stop from emptying the sweep.
      * POST-only surfaces, and anything behind a role the patched context does
        not carry.

    AND THE SCOUT REAPER IS STUBBED, which was found by doing rather than by
    anticipating. ``/scout/example`` is a no-arg GET behind ``@login_required``,
    so the signed-in pass reached it for the first time — and it calls
    ``scout.jobs.cleanup_old_jobs``, which AT THE TIME deleted EVERY
    subdirectory of ``tmp/`` older than an hour. ``tmp/calibration/`` is two
    files this repo TRACKS; the first signed-in run deleted them both. A sweep
    that asks pages what they SAY has no business running a directory reaper,
    so the reaper and the job-dir creation it precedes are stubbed here, and
    ``test_the_sweep_leaves_the_repo_alone`` fails on the next route that
    mutates the tree instead of a reviewer noticing files missing from
    ``git status``.

    The reaper itself has since been fixed — it now skips any directory whose
    name is not a UUID it minted (``scout.jobs.cleanup_old_jobs``, guarded by
    ``TestCleanupOldJobsScope`` in ``tests/test_scout_access_control.py``), so
    it can no longer reach ``tmp/calibration/``. The stubs stay: the sweep must
    not depend on the reaper being well behaved, and the job-dir creation stub
    beside it is doing separate work.
    """
    ctx = SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com",
    )
    rules = _sweep_rules(flask_app)
    pages = {}
    for signed_in in (False, True):
        client = flask_app.test_client()
        if signed_in:
            with client.session_transaction() as sess:
                sess["user_id"] = "u-1"
                sess["user_email"] = "u@example.com"
        tag = "signed in" if signed_in else "signed out"
        with ExitStack() as stack:
            stack.enter_context(
                patch("blueprints.tools.tool_enabled", return_value=True)
            )
            stack.enter_context(
                patch("scout.routes.cleanup_old_jobs", return_value=0)
            )
            stack.enter_context(patch(
                "scout.routes.create_job_dir",
                side_effect=lambda *a, **k: _scratch_job_dir(),
            ))
            if signed_in:
                for name, module in list(sys.modules.items()):
                    if name.startswith("blueprints.") and hasattr(
                        module, "load_user_context"
                    ):
                        stack.enter_context(
                            patch(f"{name}.load_user_context", return_value=ctx)
                        )
                stack.enter_context(patch(
                    "blueprints.tools.get_or_create_wallet",
                    return_value={"balance_usd": 10},
                ))
            for rule in rules:
                try:
                    resp = client.get(rule)
                except Exception:  # noqa: BLE001, S110
                    continue  # a route that errors is another test's business
                if resp.status_code == 200:
                    pages[f"{rule} [{tag}]"] = resp.get_data(as_text=True)
    return pages


def test_the_sweep_leaves_the_repo_alone(flask_app):
    """A sweep that asks pages what they SAY may not change the tree.

    Not hypothetical. Signing the sweep in reached ``/scout/example`` — a
    no-arg GET behind ``@login_required``, invisible to the signed-out sweep —
    which calls ``scout.jobs.cleanup_old_jobs``, which AT THE TIME rmtree'd
    every subdirectory of ``tmp/`` older than an hour. ``tmp/calibration/`` is
    tracked, and the first run deleted both files in it. (The reaper now filters
    on the UUID shape it mints, so it can no longer reach a sibling tenant —
    but this test does not assume that, and should not.)

    The stubs in ``_reachable_pages`` close that one. This closes the class:
    the next swept route that writes, deletes or rewrites anything under
    ``tmp/`` fails here rather than showing up as an unexplained deletion in
    someone's ``git status``.

    "The class" is a claim that was half true until ``_tmp_entries`` went
    recursive — it compared directory NAMES one level down, and the two files
    the incident destroyed live one level below that. See its docstring.

    AND ``_reachable_pages`` MUST NOT BE CACHED, although a module-scoped
    fixture would halve the suite's 166 requests: this test measures the
    sweep's SIDE EFFECTS, so it has to be the sweep that runs between the two
    snapshots. Memoise it and the second caller gets a dict back without
    touching a route, and this test passes while checking nothing.
    """
    before = _tmp_entries()
    _reachable_pages(flask_app)
    after = _tmp_entries()
    assert after == before, (
        f"the sweep changed the repo's tmp/. Added or changed: "
        f"{sorted(set(after.items()) - set(before.items()))!r}. Removed or "
        f"changed: {sorted(set(before.items()) - set(after.items()))!r}. A "
        f"swept route has a filesystem side effect; stub it in "
        f"_reachable_pages the way the scout reaper is"
    )


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
    # failure. The first two are the surfaces the claim was actually found on;
    # the third is the one a signed-out sweep structurally cannot reach.
    floor = {
        "/help/tools/rfdiffusion [signed out]",
        "/tools/rfdiffusion [signed out]",
        "/tools/rfdiffusion [signed in]",
    }
    assert floor <= describes_iptm, (
        f"the sweep no longer reaches the pages this check was written for; "
        f"it is covering something other than what it claims. reached "
        f"{len(pages)} pages, missing {sorted(floor - describes_iptm)!r}"
    )
    # AND THE TWO VARIANTS ARE STILL DIFFERENT PAGES. This guard used to read
    # "the form renders signed in and NOT signed out", which was true while a
    # separate preview shell stood in for logged-out visitors. /tools/<slug>
    # is public now — both passes render the FORM, deliberately — so the
    # discriminator moves to the thing that genuinely differs: the wallet.
    # An anonymous visitor has no balance, so the balance row and the top-up
    # gate must be absent, and a signed-out pass that grew them would mean the
    # sweep had somehow acquired a session.
    signed_in = pages["/tools/rfdiffusion [signed in]"]
    signed_out = pages["/tools/rfdiffusion [signed out]"]
    assert 'name="target_chain"' in signed_in, (
        "the signed-in sweep is not reaching the tool FORM"
    )
    assert 'name="target_chain"' in signed_out, (
        "the signed-out sweep is not reaching the tool FORM; /tools/<slug> "
        "is a public GET since the Phase 1 redesign"
    )
    assert '<div class="wallet-topup-gate"' in signed_in, (
        "the signed-in sweep is not rendering the wallet gate, so the two "
        "passes are no longer distinguishable"
    )
    assert '<div class="wallet-topup-gate"' not in signed_out, (
        "the signed-out sweep is rendering a wallet gate, so it is not "
        "actually signed out"
    )
    assert "Sign in to run this" in signed_out, (
        "the signed-out form must gate Submit behind a sign-in link"
    )

    offenders = {}
    for path, html in pages.items():
        for block in _blocks(html):
            if not _CLAIMS_THE_BINDER_PAIR.search(block):
                continue
            sentences = _sentences(block)
            for i, sentence in enumerate(sentences):
                if not _CLAIMS_THE_BINDER_PAIR.search(sentence):
                    continue
                # SCOPED TO THE SENTENCE AND ITS NEIGHBOURS, not to the block.
                # The block was the unit until an independent review put two
                # decoy clauses at the far end of the same `<dd>` and turned
                # this green with the flat claim restored. A window of one
                # sentence either side is what honest copy needs -- "…the
                # binder-to-target interface. On a multi-chain target the
                # number may cover the target's own chain–chain interface as
                # well." qualifies from the NEXT sentence, and rejecting that
                # would be the round-3 NIT-7 mistake.
                #
                # BOTH HALVES IN ONE SENTENCE, not merely both in the window: a
                # real qualification states the condition and the consequence
                # together. Split across two sentences they are two unrelated
                # true statements, which is exactly what the decoy was.
                window = sentences[max(0, i - 1):i + 2]
                if any(
                    _QUALIFIES_MULTI_CHAIN.search(s)
                    and _NAMES_THE_CONSEQUENCE.search(s)
                    for s in window
                ):
                    continue
                offenders[path] = sentence
    assert not offenders, (
        "page(s) state ipTM as the binder-to-target interface without saying, "
        "in that sentence or the one on either side of it, that on a "
        "MULTI-CHAIN target the number covers the target's own chain-chain "
        f"interface too: {offenders!r}"
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
