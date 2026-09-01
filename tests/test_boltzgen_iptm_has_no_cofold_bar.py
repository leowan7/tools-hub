"""BoltzGen's ipTM legend may not carry a bar from a different measurement.

BoltzGen scores with Boltz-2 weights, so the boltz2 legend sitting directly
below it in shared/score_legends.py looks like the obvious thing to copy —
and that is what happened: ``good`` 0.7 / ``excellent`` 0.8, plus an
explanation asserting "Above 0.7 is a credible binder".

Those are CO-FOLD bars. On the audited run BoltzGen does not cofold: its one
refold folds the binder on its own, so it has no interface to score — all
seven of its numeric ``designfolding-*iptm`` columns read 0.0 and its
``designfolding-min_interaction_pae`` is the 100000.25 "no interaction"
sentinel. What reaches this legend is instead the generator's own confidence
head reading its own output. THAT is the objection: the wrong ruler.

THE REACH ARGUMENT IS SEPARATE, AND MUST BE SCOPED AND TAKEN OFF THE RIGHT
COLUMN. On the audited 100-design replicate:

    design_to_target_iptm  (what this legend describes)  0.084-0.583, 0/100
    bare iptm              (what "460 designs, max 0.650" is)  0.450-0.649

The second is an interface-pTM averaged over EVERY chain pair, so on a
homodimer target it carries the target's own crystal interface. Cite the
first. Quoting the 460 figure to justify this legend would reproduce, inside
the justification, the wrong-quantity error the legend exists to fix.

Even on the right column that is a property of those runs, not of the metric,
and the unscoped version ("it can never reach 0.7") is false twice over:

  * the same self-hosted pipeline on peptide-anything reaches 0.777, with
    16/36 over 0.70  (boltzgen-workspace/mdm2-peptide)
  * the HOSTED Boltz API clears 0.70 routinely, max 0.983 (42.5% over 0.70 on
    the matched 3AVE/miniprotein subset; 60.3% across all hosted designs). A
    different SERVICE, aggregated retrospectively from campaign manifests by
    feld1/12_engine_evidence.py -- not a control run by 11_engine_audit.py,
    which makes no HTTP call at all. Its records DO carry an `iptm` field, and
    whether that is the same quantity as ours is explicitly open
    (11_engine_audit.py says so). Keep the populations apart for that reason,
    not because we know they differ.

Pooling those populations is how a reviewer talks themselves into either
"the bar was fine" or "the premise collapsed". Neither follows. The bar comes
off because 0.70 is calibrated on a cofold and this is not one.

For contrast, the same campaign's designs re-scored on a real Boltz-2 cofold
span 0.166-0.806 (29 rows, 1 over 0.70) on `binder_to_target` — the
per-chain-pair column feld1/13_boltz_cofold.py exists to read, precisely
because it cannot pick up target-internal contacts. Its complex-wide sibling
`cofold_iptm` reads 0.263-0.852 and is the wrong column here for the same
reason the 460 figure is. Do not join either to the audit by spec id; they
are separate stochastic runs.

Pinned here because ``good``/``excellent`` are inert today (only
``explanation`` and ``caveat`` render), which is exactly what lets a wrong
value sit unnoticed until someone wires the field up to a colour.
"""
from __future__ import annotations

import re
from html import unescape
from pathlib import Path

from shared.score_legends import (
    SCORE_LEGENDS,
    get_legend,
    legend_text,
    score_legends_for,
)

BOLTZGEN_IPTM = ("boltzgen", "ipTM")
BOLTZ2_IPTM = ("boltz2", "ipTM")


def test_boltzgen_iptm_asserts_no_bar():
    legend = SCORE_LEGENDS[BOLTZGEN_IPTM]
    assert "good" not in legend and "excellent" not in legend, (
        f"boltzgen ipTM claims good={legend.get('good')} / "
        f"excellent={legend.get('excellent')}. Nothing pairs the in-run number "
        f"against a cofold on the same designs, so there is no bar to state. "
        f"Omit it rather than invent one."
    )


def test_boltzgen_iptm_does_not_inherit_the_boltz2_cofold_scale():
    """The specific copy that was made. boltz2 keeps its bars — it IS the
    calibrated cofold — so this compares against the live values rather than
    against a hardcoded 0.7, and keeps holding if boltz2 is ever recalibrated.
    """
    cofold = SCORE_LEGENDS[BOLTZ2_IPTM]
    assert cofold.get("good") and cofold.get("excellent"), (
        "boltz2 ipTM lost its bars, so this test compares against nothing; "
        "re-point it rather than leave it passing"
    )
    text = legend_text(SCORE_LEGENDS[BOLTZGEN_IPTM])
    for value in (cofold["good"], cofold["excellent"]):
        assert not re.search(
            rf"above\s+{re.escape(str(value))}", text, re.I
        ), (
            f"boltzgen ipTM text promises a value above {value}, the Boltz-2 "
            f"cofold bar, for a number measured on a different kind of fold"
        )


def test_boltzgen_iptm_says_the_cofold_scale_does_not_apply():
    """Removing the false claim is not the same as telling the truth. A user
    reading 0.42 needs to know it is not the 0.7-scale number they know from
    every other tool here, or they will apply that scale themselves."""
    text = legend_text(SCORE_LEGENDS[BOLTZGEN_IPTM]).lower()
    assert "cofold" in text, text
    assert "0.7" in text, text


def test_the_legends_measured_on_the_refold_keep_their_bars():
    """The reason ipTM alone drops its bar is that ipTM alone has no reading
    of its own kind. pLDDT and refolding RMSD are measured on the design-only
    refold, so each is about the binder and nothing else — which is what 80
    and 1.5 A were calibrated on. Dropping their bars too would be
    over-correcting, and this pins that it did not happen."""
    for column in ("pLDDT", "refolding_rmsd"):
        legend = SCORE_LEGENDS[("boltzgen", column)]
        assert legend.get("good") is not None, column
        assert legend.get("excellent") is not None, column


# ---------------------------------------------------------------------------
# The bar also has a GLOBAL home, and three tool-scoped surfaces render it
# ---------------------------------------------------------------------------
# Clearing ``good`` off the legend is not enough on its own. The same bar lives
# a second time in shared/metric_glossary.py as GLOSSARY["ipTM"]["good_range"]
# = "> 0.75 strong; > 0.65 acceptable", which is keyed by METRIC and is global,
# and three templates that DO know which tool they are rendering print it:
#
#   components/candidate_table.html  stacks it onto the per-tool legend, in one
#                                    tooltip, on the results table itself
#   components/about_panel.html      the form page, before the user pays
#   help/tool_guide.html             the page that teaches how to read the score
#
# So before this, a boltzgen user read "so 0.7 does not apply" and then, four
# words later in the same tooltip, "Range: > 0.75 strong; > 0.65 acceptable".
# The glossary entry is correct for every other tool and stays; the three
# tool-scoped surfaces suppress it when the tool's own legend declines a bar.
#
# The global bar is READ here, never retyped, so recalibrating the glossary
# cannot leave these passing against a number the product no longer uses.

import pytest  # noqa: E402
from jinja2 import Environment, FileSystemLoader  # noqa: E402

import app as _app  # noqa: E402,F401  (populates tools.base._REGISTRY)
from shared import metric_glossary, ranking  # noqa: E402
from tools import base as tool_base  # noqa: E402
from tools.boltzgen import meta as _bg_meta  # noqa: E402

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
_GLOBAL_IPTM_RANGE = metric_glossary.GLOSSARY["ipTM"]["good_range"]


@pytest.fixture
def _app_client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    for adapter in tool_base.all_adapters():
        monkeypatch.setenv(
            "FLAG_TOOL_" + adapter.slug.upper().replace("-", "_"), "on"
        )
    flask_app = _app.create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _column_tooltip(tool_slug: str) -> str:
    """The assembled ipTM header tooltip for one tool, as the page emits it."""
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    env.globals.update(
        metric_glossary=metric_glossary.GLOSSARY,
        score_legends_for=score_legends_for,
        format_metric_value=metric_glossary.format_value,
        score_legend_for=get_legend,
        legend_text=legend_text,
        ordinal=ranking.ordinal,
        csrf_input=lambda: "",
        url_for=lambda _endpoint, **kw: "/static/" + kw.get("filename", ""),
    )
    tmpl = env.from_string(
        '{% from "components/candidate_table.html" import candidate_table %}'
        "{{ candidate_table(candidates, columns, job_id, tool_slug, clone_url,"
        "  campaign_id, target_id, multi_tool, sort_mode, split_tools, per_tool) }}"
    )
    html = tmpl.render(
        candidates=[], columns=["ipTM"], job_id="j1", tool_slug=tool_slug,
        clone_url="", campaign_id="", target_id="", multi_tool=False,
        sort_mode="", split_tools=(), per_tool={},
    )
    match = re.search(r'data-tooltip="([^"]*)"', html)
    assert match, f"no ipTM tooltip rendered for {tool_slug}"
    return unescape(match.group(1))


def test_the_results_tooltip_does_not_re_add_the_bar_from_the_glossary():
    """The flat contradiction: one tooltip saying 0.7 does not apply and then
    quoting a 0.65-0.75 band. This is the surface that shows the numbers the
    bar judges, so it is the one that turned an ordinary run into a failure."""
    tooltip = _column_tooltip("boltzgen")
    assert _GLOBAL_IPTM_RANGE not in tooltip, (
        f"the global ipTM range {_GLOBAL_IPTM_RANGE!r} is stacked onto "
        f"boltzgen's own legend, which has just finished saying that scale "
        f"does not apply:\n\n{tooltip}"
    )


def test_the_tooltip_keeps_the_definition_and_the_citation():
    """Suppressing the BAR is not suppressing the glossary. The definition and
    the AlphaFold-Multimer citation are true of ipTM wherever it appears, and
    dropping the whole entry would be the over-correction."""
    tooltip = _column_tooltip("boltzgen")
    glossary = metric_glossary.GLOSSARY["ipTM"]
    assert glossary["definition"] in tooltip, tooltip
    assert glossary["citation"] in tooltip, tooltip
    # ...and the seam where the range was removed is not left ".." or " ."
    assert ".." not in tooltip and " ." not in tooltip, tooltip


def test_a_tool_that_states_a_bar_still_gets_the_global_range():
    """The control. boltz2 IS the calibrated cofold, so the band is true for
    it — a fix that stripped the range from every tool would pass the test
    above and quietly cost every other tool its answer to "what is good?"."""
    tooltip = _column_tooltip("boltz2")
    assert _GLOBAL_IPTM_RANGE in tooltip, tooltip


def test_boltzgen_is_the_only_legend_without_a_bar():
    """Pins the blast radius of the three template conditions. If a second
    legend ever drops its bar, that tool's surfaces change too — which may be
    right, but it should be a decision, not a surprise."""
    barless = {k for k, v in SCORE_LEGENDS.items() if "good" not in v}
    assert barless == {BOLTZGEN_IPTM}, barless


def test_the_form_page_does_not_quote_the_band_to_a_boltzgen_user(_app_client):
    """about_panel, on the page where the user decides whether to pay. It said
    tools sit "a little either side" of > 0.65 — a band from a cofold, which
    is not the measurement boltzgen's number comes from."""
    resp = _app_client.get("/tools/boltzgen")
    assert resp.status_code == 200, resp.status_code
    body = unescape(resp.get_data(as_text=True))
    assert _GLOBAL_IPTM_RANGE not in body, (
        f"the boltzgen form page quotes the global ipTM band "
        f"{_GLOBAL_IPTM_RANGE!r} as if boltzgen sat near it"
    )


def test_the_tool_guide_does_not_quote_the_band_to_a_boltzgen_user(_app_client):
    """help/tool_guide, the page that teaches people how to read the score."""
    resp = _app_client.get("/help/tools/boltzgen")
    assert resp.status_code == 200, resp.status_code
    body = unescape(resp.get_data(as_text=True))
    assert _GLOBAL_IPTM_RANGE not in body, (
        f"the boltzgen guide quotes the global ipTM band "
        f"{_GLOBAL_IPTM_RANGE!r} as this tool's acceptable range"
    )


def test_another_tool_s_guide_still_quotes_the_band(_app_client):
    """Control for the two above, for the same reason as the tooltip control:
    these pages are shared by every tool and must keep working for them."""
    resp = _app_client.get("/help/tools/boltz2")
    assert resp.status_code == 200, resp.status_code
    assert _GLOBAL_IPTM_RANGE in unescape(resp.get_data(as_text=True))


# ---------------------------------------------------------------------------
# The two guard holes an independent mutation pass found in this file
# ---------------------------------------------------------------------------
# 1. The tools/ copy fix had NO test. Reverting tools/boltzgen/__init__.py and
#    meta.py to their pre-fix state left the entire suite green (5852 passed,
#    identical to the branch), so the claim that candidates arrive "already
#    checked against the site you aimed at" could walk back in unnoticed.
# 2. The prose guards below pin PHRASES, not the claim. A mutation reading
#    "so 0.7 does not apply; treat 0.5 as a credible binder here" satisfied
#    every assertion in this file while inventing exactly the replacement bar
#    the legend's own comment forbids ("Omit rather than invent a
#    replacement"). Nothing asserted the ABSENCE of a bar.

# ---------------------------------------------------------------------------
# POLARITY. Both patterns below recognise the SHAPE of a claim, and the shape
# of "0.6 is credible" is identical to that of "there is no evidence that 0.6
# is credible". An independent mutation pass measured what that cost: writing
# "The refold does not demonstrate binding." into the live output_summary
# turned this file RED, while "The refold proves it binds." shipped GREEN. A
# guard that reddens the honest sentence and passes the false one is not weak,
# it is inverted -- it pushes the copy away from the truth.
#
# So a match only counts as a claim when the sentence around it neither negates
# nor RECOMMENDS a new fold. The recommendation case used to be protected by
# accident: "re-fold a shortlist against your target" -- the sentence the page
# is supposed to end on -- survived only because ``refold\w*`` cannot match the
# hyphen. Written without it, the sanctioned advice failed the suite. It is
# pinned in ``must_not_fire`` now rather than left to punctuation.
def _norm(s: str) -> str:
    return " ".join(s.split())


# THE ONLY EXEMPT SENTENCES, MATCHED EXACTLY (whitespace-normalised). Not a
# heuristic, and the reason is measured. The first attempt at this vetoed any
# match sitting inside a negated or advice-shaped sentence, which reads
# sensible and is catastrophic: an independent review found 26 real claims
# went silently green, among them the ONE mutation this file's header names as
# the reason _BAR_CLAIM exists ("so 0.7 does not apply; treat 0.5 as a credible
# binder here" -- it contains "not"), and the live output_summary turned into a
# shelter where NO overclaim could be seen, because its closing sentence
# carries both a negation and a recommendation.
#
# A heuristic that recognises the shape of honesty is a shape an overclaim can
# wear. So: honest sentences are ENUMERATED. Adding one is a deliberate act,
# and rewording one goes red until someone updates it here -- which is the
# point, because that is a human reading the sentence again.
_SANCTIONED = frozenset(
    _norm(s)
    for s in (
        # The next step the page is meant to end on, in both spellings. The
        # hyphenated form passed before only because ``refold\w*`` cannot match
        # "re-fold" -- an accident of punctuation, not a guard.
        "re-fold a shortlist against your target to check that.",
        "Refold a shortlist against your target to check that.",
        # Sentences that REFUSE a bar. The pattern cannot tell these from the
        # claim they deny, and they are the correct thing to say.
        "Other tools treat 0.7 as the bar; this one has none.",
        "There is no evidence that 0.6 is credible here.",
        "Nothing here tells you whether 0.5 is good.",
        # ...and the same for the refold pattern.
        "The refold does not demonstrate binding.",
        "Refolding RMSD under 1.5 &Aring; does not confirm binding.",
    )
)
# Sentence, not clause: "Other tools treat 0.7 as the bar; this one has none."
# carries its negation past a semicolon, so splitting there would re-open the
# hole. Splitting on [.!?] followed by SPACE leaves "1.5 A" intact -- a decimal
# point has no space after it. Same decimal problem the ``(?:[^.]|\.\d)`` span
# further down exists to solve. An abbreviation period ("vs. ") does split, and
# that is a known miss.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _sentence_around(text: str, index: int) -> str:
    start, end = 0, len(text)
    for m in _SENTENCE_BOUNDARY.finditer(text):
        if m.end() <= index:
            start = m.end()
        else:
            end = m.start()
            break
    return text[start:end]


def _asserted(pattern, text: str):
    """First match the copy actually ASSERTS, or None.

    A match is a claim unless the sentence holding it is one of the handful
    enumerated in ``_SANCTIONED``. Exact match, so any edit to a sanctioned
    sentence -- including appending an overclaim to it -- is a claim again.
    """
    for m in pattern.finditer(text):
        if _norm(_sentence_around(text, m.start())) in _SANCTIONED:
            continue
        return m
    return None


# ponytail: A PHRASE LIST WITH A MEASURED CEILING, not a claim detector. An
# independent mutation pass ran 66 adversarial bar statements past it and 51
# escaped. The gaps are whole classes, not a tail: negative polarity ("below
# 0.5 is weak"), percentages ("55% or better"), ranges ("0.5-0.7 is the
# credible band"), comparison symbols (">= 0.6" -- coverage here is zero; the
# few that looked caught were matching an unrelated clause), word-numbers
# ("point six"), leading dot (".55 or better"), and numbers split by markup
# ("<b>0.6</b>"). Those rates were measured on this pattern as ``_asserted``
# applies it -- the only exemption is the enumerated ``_SANCTIONED`` list, so
# they describe the guard as it actually runs.
#
# Kept because it does pin the phrasings that were actually wrong, and because
# widening it again is how this repo reached 37 guards that certify false. Read
# a pass as "the known-bad shapes are absent", NEVER as "no bar is stated". The
# durable fix is the one the legend's own comment names: do not restate
# thresholds in copy at all.
_BAR_CLAIM = re.compile(
    # "above 0.6", "aim for 0.55+", "0.5 or better", "treat 0.5 as"
    r"(above|over|aim\s+for|at\s+least|better\s+than|treat|from)\s+0\.\d+"
    r"|0\.\d+\s*\+"
    r"|0\.\d+\s+(or\s+better|or\s+higher|and\s+up(wards?)?|and\s+above"
    r"|upwards?|is\s+(good|strong|credible|acceptable))",
    re.I,
)


def test_the_explanation_states_no_bar_of_its_own():
    """The claim, not the phrasing. Dropping ``good`` from the data and then
    writing a bar into the sentence is the same false promise in a place no
    field name marks — and it is what the legend's own comment forbids.

    The one number allowed is the 0.7 being disclaimed, which is why this
    matches bar-SHAPED assertions rather than any digit.
    """
    text = legend_text(SCORE_LEGENDS[BOLTZGEN_IPTM])
    hit = _asserted(_BAR_CLAIM, text)
    assert hit is None, (
        f"boltzgen's ipTM explanation states a bar of its own: "
        f"{hit.group(0)!r} in {text!r}. There is nothing "
        f"pairing this number against a cofold on the same designs, so there "
        f"is no bar to state — omit rather than invent one."
    )


def test_a_tool_with_a_real_bar_is_still_allowed_to_say_so():
    """ANTI-VACUITY, despite what this docstring used to claim it was.

    It said "the regex must not be so broad that it forbids legends that
    legitimately carry a bar" — a breadth control — while the assertion is
    that the pattern DOES match. That is the opposite test. The real breadth
    control is the one below, which did not exist, and a review found three
    honest disclaimers going red with nothing to catch it.
    """
    text = legend_text(SCORE_LEGENDS[BOLTZ2_IPTM])
    assert _asserted(_BAR_CLAIM, text), (
        "the bar-claim pattern no longer matches boltz2's 'Above 0.7 is a "
        "credible binder', so the test above is asserting over nothing"
    )


def test_the_bar_pattern_does_not_fire_on_honest_disclaimers():
    """The breadth control ``_BAR_CLAIM`` never had.

    Every sentence here is a correct thing to say, and each one matched the
    unvetoed pattern — while four of five invented bars shipped green. If this
    list goes red the guard has started forcing the page to stop explaining
    itself, which is the failure that matters: the copy is what this whole
    change exists to keep true.
    """
    for text in (
        "Other tools treat 0.7 as the bar; this one has none.",
        "There is no evidence that 0.6 is credible here.",
        "Nothing here tells you whether 0.5 is good.",
    ):
        hit = _asserted(_BAR_CLAIM, text)
        assert hit is None, (
            f"the bar guard fires on an honest disclaimer "
            f"({hit.group(0)!r} in {text!r}), pushing the copy away from the "
            f"truth rather than toward it"
        )

    # ...and the exemption is EXACT. This is the half that matters: the first
    # attempt at this exempted a SHAPE (negated, or advice-worded), and an
    # overclaim appended to an honest sentence inherited its immunity. A
    # review demonstrated it on the live output_summary. Enumerating is what
    # makes appending visible again.
    for text in (
        "Other tools treat 0.7 as the bar; this one has none, so treat 0.5 "
        "as the bar instead.",
        "There is no evidence that 0.6 is credible here, but above 0.5 is a "
        "credible binder.",
    ):
        assert _asserted(_BAR_CLAIM, text), (
            f"a bar smuggled onto the end of a sanctioned disclaimer is "
            f"invisible: {text!r}. The exemption must be the exact sentence, "
            f"never its shape."
        )


# WHAT THE REFOLD MAY NOT BE CREDITED WITH. Two shapes, because the claim has
# now been found in three places wearing two different disguises:
#
#   (a) the refold joined to the target by a verb of verification --
#       "refolded and scored against that target", "already checked against
#       the site you aimed at".
#   (b) the refold or its RMSD used to infer BINDING -- "signals
#       self-consistent binding". This one names no target at all, which is
#       why a guard written for (a) walked straight past it.
#
# Saying "re-fold a shortlist against your target" is NOT either of these: it
# recommends a new cofold rather than crediting this one, and the must_not_fire
# cases below pin that distinction so the pattern cannot be widened until the
# page can no longer explain itself.
_REFOLD_OVERCLAIM = re.compile(
    r"refold\w*[^.]{0,80}?(against|checked|validated)[^.]{0,40}?"
    r"(target|site|epitope)"
    r"|(checked|validated|verified)[^.]{0,40}?against[^.]{0,30}?"
    r"(target|site you aimed)"
    r"|(refold\w*|rmsd)(?:[^.]|\.\d){0,60}?"
    r"(signals?|confirms?|shows?|demonstrates?|means|indicates?|establishes?)"
    r"(?:[^.]|\.\d){0,40}?(bind|engages?)",
    re.I,
)
# ``(?:[^.]|\.\d)`` NOT ``[^.]``: the span is a stand-in for "same sentence",
# and a DECIMAL POINT is a full stop to a character class. The sentence this
# guards is full of them, so "Refolding RMSD under 1.5 A confirms binding"
# slipped through while the identical sentence written "under two A" was
# caught. Allowing a dot that is followed by a digit keeps 1.5 inside the
# span and still stops at a real sentence end.
#
# ponytail: MEASURED CEILING. An independent mutation pass ran 49 ways of
# crediting the refold with binding past this pattern; 35 escaped. The holes
# are structural, not a tail of phrasings:
#   - the SUBJECT side is a two-word list (refold*|rmsd), so "the
#     self-consistency check confirms binding" walks straight past;
#   - the verb list omits verifies, validates, proves, tells you, is evidence
#     that -- and is inconsistent between legs (leg 2 has "verified", leg 3
#     does not);
#   - passive voice is uncovered ("binding is confirmed by the refold");
#   - the 60/40-char spans are a budget that a long clause exceeds;
#   - the span stops at a sentence end, so "It refolds the binder alone. It
#     confirms binding." is invisible.
# Kept because it pins the three sentences that actually shipped wrong. Read a
# pass as "those shapes are absent", never as "the copy is honest".


def test_the_adapter_copy_does_not_claim_the_refold_checks_the_target():
    """The tools/ half, which had no test at all.

    BoltzGen runs one refold and it folds the BINDER ALONE — every
    ``designfolding-*`` interface column reads 0.0 and its
    ``min_interaction_pae`` is the 100000.25 "no interaction" sentinel. So no
    surface may say the returned candidates have been checked, scored or
    validated AGAINST THE TARGET by that refold. Ranking on the generator's
    own interface number is a different claim and is fine.
    """
    from tools.boltzgen import adapter
    from tools.boltzgen import meta as bg_meta

    surfaces = {
        "blurb": adapter.blurb,
        "about.what_it_is": bg_meta.about.get("what_it_is", ""),
        "about.output_summary": bg_meta.about.get("output_summary", ""),
        # when_to_use is a LIST, and it renders on the same page as the three
        # above. It was never scanned: a review placed the loudest overclaim it
        # could write into it and the entire suite stayed green.
        "about.when_to_use": " ".join(bg_meta.about.get("when_to_use", ())),
    }
    for name, text in surfaces.items():
        # Non-empty FIRST: renaming the key or blanking the string would
        # otherwise satisfy every assertion below over "".
        assert text, (
            f"boltzgen {name} is missing or empty, so this guard is "
            f"asserting over nothing"
        )
        hit = _asserted(_REFOLD_OVERCLAIM, text)
        assert not hit, (
            f"boltzgen {name} credits the refold with something it cannot "
            f"show. It folds the binder alone, so it checks the design "
            f"against ITSELF: {hit.group(0)!r} in {text!r}"
        )


def test_that_refold_claim_check_can_actually_fire():
    """Anti-vacuity for the test above: the pattern must match the exact
    sentence that shipped, or it is guarding nothing."""
    must_fire = (
        # blurb, before #193
        "get back candidates each refolded and scored against that target",
        # about.what_it_is, before #193
        "refolds it inside the same model, so every candidate arrives already "
        "checked against the site you aimed at",
        # about.output_summary, which BOTH of those fixes walked past — and
        # which the first version of this guard also missed, because it looked
        # for the word "target" and this sentence never says it.
        "Refolding RMSD &lt; 2 &Aring; on the top design typically signals "
        "self-consistent binding.",
    )
    for text in must_fire:
        assert _asserted(_REFOLD_OVERCLAIM, text), (
            f"the guard does not fire on copy that actually shipped: {text!r}"
        )

    must_not_fire = (
        # The LIVE string, so a rewrite cannot drift out from under the guard.
        _bg_meta.about["output_summary"],
        # ...and a FROZEN exemplar beside it, because the live string alone is
        # not a pin: the test above already asserts over that same value, so
        # the two moved together on every edit and this entry asserted nothing
        # new. A review caught exactly that.
        "Refolding RMSD is the design against its own refold: at or under "
        "2 &Aring; it clears the RMSD leg of the pass bar. That says the "
        "binder folds as designed, not that it binds &mdash; re-fold a "
        "shortlist against your target to check that.",
        # The same sanctioned recommendation WITHOUT the hyphen. It passed
        # before only because ``refold\w*`` cannot match "re-fold"; the
        # recommendation veto is what allows it now, and this pins that rather
        # than trusting punctuation.
        "Refold a shortlist against your target to check that.",
        # Negated claims -- the correct things to say. The unvetoed pattern
        # turned every one of these red while passing "the refold proves it
        # binds".
        "The refold does not demonstrate binding.",
        "Refolding RMSD under 1.5 &Aring; does not confirm binding.",
        # when_to_use, which was correct all along and must stay allowed.
        "each design refolded on its own, so you can see whether it folds "
        "back to the shape it was designed as",
    )
    for text in must_not_fire:
        hit = _asserted(_REFOLD_OVERCLAIM, text)
        assert not hit, (
            f"the guard fires on honest copy ({hit.group(0)!r} in {text!r}), "
            f"which would force the page to stop explaining the metric"
        )


def test_the_refold_predicate_is_registered_and_is_the_real_set():
    """``is_refold_source`` gates the Second-opinion panel AND the all-failed
    banner's advice. Both fail QUIETLY if the global is missing from a jinja
    environment — ``x in Undefined`` renders False with no error, which is why
    the template calls it instead. This pins that the app registers it and that
    it is shared.refold.SOURCE_TOOLS rather than a copy that can drift.
    """
    from shared.refold import SOURCE_TOOLS

    flask_app = _app.create_app()
    pred = flask_app.jinja_env.globals.get("is_refold_source")
    assert pred is not None, (
        "is_refold_source is not registered, so every template that gates on "
        "it renders the gated block away silently"
    )
    for slug in SOURCE_TOOLS:
        assert pred(slug), slug
    assert not pred("definitely-not-a-tool")
    # ...and it is the module's set BY IDENTITY, not a second collection
    # someone must remember to update. The template used to carry five slugs
    # under a comment asking the next reader to keep them in lockstep, which
    # is the arrangement this replaced.
    assert pred.__self__ is SOURCE_TOOLS, (
        "is_refold_source is bound to something other than "
        "shared.refold.SOURCE_TOOLS, so the two can drift apart again"
    )
