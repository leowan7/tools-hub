"""Proteina may not promise diversity clustering, and may not render a
``cluster_id`` column, because this pipeline delivers neither.

WHAT WAS SHIPPING. The tool page said the run "clusters the winners, so you get
a spread of different high-scoring designs rather than near-duplicates"
(``seo_faq[2]``, which also feeds the page's FAQPage JSON-LD via
blueprints/tools.py) and "ranks globally across all of them and clusters the
winners" (``about["what_it_is"]``); a ``when_to_use`` bullet offered "a spread of
different good designs" as a reason to pick the tool;
``about["output_summary"]`` listed "a structural diversity cluster id" among the
outputs; ``reward_attributions`` credited "Foldseek / MMseqs2 / DSSP —
post-hoc diversity clustering". Meanwhile every proteina results table carried a
``cluster_id`` column, which the renderer prints as an em dash for a null
(templates/components/candidate_table.html:761 — :789 is the branch for a
non-null unparseable value, a distinction that file draws at :785).

WHAT IS ACTUALLY THERE, measured 2026-09-10.

1. No cluster id has ever been MEASURED here. 17,024 of 17,024 candidates across
   the four length-sweep driver runs (``~/proteina-sweep-tier{1,2,3a,3b}/shard_*/
   smoke_result.json`` — the container's own output) carry
   ``"cluster_id": null``. Those runs went through ``shard_driver.py`` straight
   to Modal, so they were never RENDERED; what is measured is the value, not the
   em dash. Jobs stored in the production database were not read — that needs
   credentials — and point 2 is why it does not change the answer.

2. No code in this repo ORIGINATES a value. The only ingest is
   ``_pick`` over ``_SCORE_COLUMNS.items()`` at run_pipeline.py:3559, whose
   ``cluster_id`` entry (:390) lists one candidate column name that appears in
   neither reward-CSV header
   pinned in
   tests/test_proteina_smoke.py::TestRewardParse. No captured reward CSV is
   committed anywhere in this repo — every header it records is a hand-written
   test literal, and those two are not the only such literals. Writes of the key
   DO exist (a coercion, plus several name-only column declarations), so "every
   occurrence is a read" would be wrong; the supportable form is that nothing
   ORIGINATES a value. ``webhooks/modal.py`` only range-clamps what the
   container sent, so it cannot invent one either.

3. The hub ranks; it does not cluster.
   ``shared.compute_campaigns.aggregate_campaign_candidates`` pools every
   sub-job's candidates and sorts by ``(passed, missing, primary metric)``
   (compute_campaigns.py:1483-1495). No diversity step there. (The repo does
   contain MPNN *sequence* diversification (``shared/resample.py``, which
   raises sampling temperature to spread sequences over one fold) — a different
   thing on a different tool. "No diversity anywhere in the repo" would be
   false; this is the claim.)

4. Upstream clusters, into a place nothing here reads. That upstream RUNS it is
   what ``binder_analyze.yaml``'s header comment says ("diversity: enabled by
   default" — a comment, not a config key) plus the fact that the binaries
   ship; no container log was inspected. At 916eaaed
   ``result_analysis/compute_diversity.py`` returns one row per GROUP (column
   ``_res_diversity_foldseek_*``, an aggregate) after dropping ``pdb_path``. The
   per-design artifacts — ``cluster_assignments*.csv`` (header
   ``cluster_index,sample_index,path_name``), ``res_cluster*.tsv`` and
   ``original_pdb_paths*.txt`` — all land in a ``clusters_*`` side directory.
   None of them is the reward CSV, and the column is ``cluster_index``, not
   ``cluster_id``. Those ids are per-invocation, renumbered from 0 on every
   ``diversity_foldseek`` call (metrics/diversity.py:207), so they could not
   carry the cross-shard claim the page was making even if they were read.

WHY THIS FILE IS SHAPED THE WAY IT IS. Its first version scanned only
``tools/proteina/meta.py`` and regex-parsed the template's column line. Review
found seven ways it stayed green while the defect was back, four of them
executed:

  * the retired sentence pasted into ``adapter.blurb`` or into
    ``blueprints.tools._PREVIEW_SEO_PHRASES["proteina"]`` — the hero renders
    the second to signed-OUT visitors, i.e. the indexed page — was invisible,
    because neither lives in ``meta``;
  * ``"cluster_id"`` double-quoted inside the otherwise single-quoted column
    list defeated ``re.findall(r"'([^']+)'")``;
  * a second ``{% set columns = columns + ['cluster_id'] %}`` line was never
    examined, because the parse took the first match and stopped;
  * dropping ``about["when_to_use"]`` — the key that carried one of the five
    retired promises — left 83 strings against a ``> 60`` floor, so the
    liveness check said nothing. (83 of the 88 strings THAT walk collected;
    the current one also walks dict keys and all three sources, so the same
    counts today are 144 of 150 for meta and 155 of 161 overall.)

So: the columns are read through Jinja's own parser rather than a regex, the
prose scan covers three sources and pins an anchor in each SECTION, and the
whole thing sits under a RENDER of the real page in both auth states, which is
the layer that does not care where a sentence came from.

WHAT THIS FILE STILL CANNOT DO. It pins wording, not meaning. ``_MENTION``
carries the five phrasings that shipped plus nine synonyms review found; the
authoritative list is the pattern itself, and ``_BRANCH_PROBES`` pins one live
example per branch. So the obvious one-word swaps are covered. A promise built
on none of those fourteen roots still passes the source scan. The render tests narrow that to
"not on the tool page", which is the surface that matters, but nothing here can
police a promise nobody thought of.

Treat a green run as "the known wordings are gone", not as "the page is honest".
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jinja2
import pytest
from jinja2 import nodes

from shared.result_columns import columns_for

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_REPO = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO / "templates" / "tools" / "proteina_results.html"


# A mention of clustering/diversity is only allowed if it is one of these, and
# each is here for a stated reason that is NOT "the tool clusters your designs".
_ALLOWED_MENTIONS: dict[str, str] = {
    "Can I run Proteina-Complexa online without a GPU cluster?":
        "a GPU cluster — compute hardware, not design clustering",
    "Foldseek / MMseqs2 — clustering binaries shipped for the upstream "
    "analysis stage; no cluster assignment is surfaced in results.":
        "an attribution for binaries the image really ships "
        "(Dockerfile.modal:80-87), and it says outright that no assignment "
        "reaches the result",
}

# WIDER THAN "cluster", and deliberately so. ``diversit`` alone misses
# "diverse"; the retired payoff clauses were phrased three ways — "a spread
# of different designs", "rather than near-duplicates", "many copies of one"
# — and none of them contains either root. Review then showed that
# one-token edits of the retired copy ("clusters" -> "groups", "diversity
# cluster id" -> "similarity group id") slipped past a pattern built only from
# the historical phrasings, so the common synonyms are here too. This is a
# tripwire for known wordings, NOT a semantic check — see the docstring.
#
# ``variety of`` rather than ``variety``: proteina's own length-window copy
# (templates/tools/proteina_form.html:236-239) says "a wide window buys variety
# and a narrow one buys consistency", an honest statement about sampling and not
# a promise. In the rendered page that sentence wraps -- the gap between "buys"
# and "variety" is a newline plus indentation, which is why the branches below
# use a whitespace class and not a space. Near-duplicates of that sentence live in boltzgen_form.html and
# rfdiffusion_form.html; nothing is shared, and an earlier draft of this comment
# said the proteina page rendered boltzgen's copy, which it does not — that file
# is included nowhere. "a variety of different good designs" — the one-word swap
# of the retired bullet — still matches.
_MENTION = re.compile(
    # Every inter-word gap is a WHITESPACE CLASS, never a literal space. The
    # render layer scans raw HTML, where template copy wraps across lines and
    # indents (see the ``variety of`` note above for the measured case), so a
    # literal space would miss a promise written into a template -- the one
    # route the render layer exists to cover. ``near[-\s]*`` rather than
    # ``near.?``: ``.`` does not match a newline, so the ``.?`` spelling missed
    # "near" + newline + indent + "duplicate" while claiming to cover it.
    r"cluster"
    r"|divers"
    r"|spread\s+of"
    r"|\bnear[-\s]*duplicate"
    r"|\bnear[-\s]*identical"
    r"|copies\s+of\s+one"
    r"|redundan"
    r"|\bde[-\s]*duplicat"
    r"|group(s|ed|ing)?\s+(the\s+)?(winner|design|result|survivor)"
    r"|similarity"
    r"|family\s+id"
    r"|variety\s+of"
    r"|distinct"
    r"|no\s+two\s+designs",
    re.I,
)

# One probe per alternation. ``_RETIRED`` exercises five of the fourteen, so
# the nine added by review were pinned by nothing — a typo in any of them
# (``redundna``) would have shipped green.
_BRANCH_PROBES = {
    "cluster": "the hub clusters the winners",
    "divers": "a structural diversity id",
    "spread of": "you get a spread of different designs",
    "near-duplicate": "rather than near-duplicates",
    "near-identical": "discards near-identical winners",
    "copies of one": "many copies of one fold",
    "redundan": "redundancy across shards is removed",
    "de-duplicat": "we de-duplicate the winners",
    "groups the winners": "Foldseek groups the winners by shape",
    "similarity": "a structural similarity id",
    "family id": "a structural family id per candidate",
    "variety of": "a variety of different good designs",
    "distinct": "every design is structurally distinct",
    "no two designs": "no two designs in your results are alike",
}

# The retired sentences. The positive control below feeds these THROUGH the
# real scan path, not just through the regex — the first version of this file
# checked them as bare literals, which proved the pattern and nothing about the
# walk.
_RETIRED = (
    "Each shard keeps what scores well, and the hub then ranks across every "
    "shard at once and clusters the winners, so you get a spread of different "
    "high-scoring designs rather than near-duplicates.",
    "then ranks globally across all of them and clusters the winners, so you "
    "get a spread of different designs rather than many copies of one.",
    "a structural diversity cluster id, and downloadable structures.",
    "Foldseek / MMseqs2 / DSSP — post-hoc diversity clustering.",
    "You want a spread of different good designs rather than many "
    "variations on one.",
)


def _walk(node: object, out: list[str]) -> None:
    """Collect every string reachable through str/dict/list/tuple.

    Dict KEYS are walked as well as values: a promise can be a label.
    Sets and frozensets ARE walked. Shapes this does not reach (generators,
    objects with ``__str__``, functions returning copy) are a known limit — the render tests are what
    covers prose that arrives by a route this walk does not model.
    """
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            _walk(key, out)
            _walk(value, out)
    elif isinstance(node, (list, tuple, set, frozenset)):
        for value in node:
            _walk(value, out)


def _prose_sources() -> dict[str, list[str]]:
    """Every proteina prose surface, keyed by source so a loss names itself.

    THREE sources, not one. ``meta`` is what the about panel and the FAQ read;
    ``adapter`` is the blurb the hero shows a signed-IN visitor and the help
    guide shows everyone; ``seo_phrases`` is the lede the hero shows a
    signed-OUT visitor, which is the indexed version of the page.
    """
    from blueprints.tools import _PREVIEW_SEO_PHRASES
    from tools.proteina import adapter, meta

    meta_strings: list[str] = []
    for name in dir(meta):
        if not name.startswith("_"):
            _walk(getattr(meta, name), meta_strings)

    adapter_strings: list[str] = []
    _walk(adapter.blurb, adapter_strings)
    for preset in adapter.presets:
        _walk(getattr(preset, "label", ""), adapter_strings)
        _walk(getattr(preset, "description", ""), adapter_strings)

    seo_strings: list[str] = []
    _walk(_PREVIEW_SEO_PHRASES.get("proteina"), seo_strings)

    return {
        "meta": meta_strings,
        "adapter": adapter_strings,
        "seo_phrases": seo_strings,
    }


def _offenders(strings: list[str]) -> list[str]:
    """The one predicate. Both the real check and the injected control use it,
    so the control exercises the code path the check actually runs."""
    return [s for s in strings
            if _MENTION.search(s) and s not in _ALLOWED_MENTIONS]


def _template_column_assignments() -> list[object]:
    """Every ``{% set columns = ... %}`` in the results partial, via Jinja's own
    parser.

    NOT a regex over the source. ``re.findall(r"'([^']+)'")`` on the first
    matching line missed a double-quoted entry mixed into a single-quoted list,
    and missed a second assignment entirely — both executed, both left the
    column rendering with these tests green. A non-constant right-hand side
    (``columns + [...]``) comes back as the sentinel rather than a list, because
    it cannot be proved safe by reading.
    """
    tree = jinja2.Environment().parse(_TEMPLATE.read_text(encoding="utf-8"))
    found: list[object] = []
    for node in tree.find_all(nodes.Assign):
        target = node.target
        if isinstance(target, nodes.Name) and target.name == "columns":
            try:
                found.append(node.node.as_const())
            except nodes.Impossible:
                found.append("NON-CONSTANT")
    return found


def _template_columns_arguments() -> list[str]:
    """What each call site actually PASSES as ``columns=``.

    Pinning the declaration is not enough: the partial declares the constant
    list and then hands it to ``results_panel`` at ``columns=columns``. Change
    that one call to ``columns=columns + ['cluster_id']`` and the check above
    still sees the canonical five, while the page renders the extra header
    (measured on /tools/proteina: ``<th`` count 15 -> 16, ``cluster_id``
    present in the body). The render consumes the ARGUMENT, so the argument is
    what has to be a bare name.

    THIS IS A CHEAP SPECIFIC DUPLICATE, NOT THE ONLY THING STOPPING THAT.
    ``test_the_rendered_tool_page_promises_no_clustering`` already fails on the
    same mutation (measured: 66 offenders), because ``cluster`` has been in
    ``_MENTION`` since the first version of this file. An earlier draft of this
    docstring claimed the call-site append passed "with all tests green" and
    quoted a ``<th>`` delta as executed when the figure had been taken from a
    review report rather than run. Both were wrong; the numbers above were
    measured on the page.

    It does not cover every rebind. Measured: wrapping the call in
    ``{% with columns = columns + ["cluster_id"] %}`` leaves BOTH AST checks
    green — ``nodes.With`` is not a ``nodes.Assign`` (nor is
    ``nodes.AssignBlock``, checked the same way), and the argument is still a
    bare ``Name('columns')`` — while the page renders the extra column and the
    render test flags it (66 offenders). So the AST layer is narrower than it
    reads, and the render test is what actually closes that shape. A positional
    call is fail-closed: the keyword scan returns ``[]`` and the assert fails.
    """
    tree = jinja2.Environment().parse(_TEMPLATE.read_text(encoding="utf-8"))
    passed: list[str] = []
    for call in tree.find_all(nodes.Call):
        for kwarg in call.kwargs:
            if kwarg.key == "columns":
                value = kwarg.value
                passed.append(
                    value.name if isinstance(value, nodes.Name)
                    else type(value).__name__)
    return passed


# ---------------------------------------------------------------------------
# Layer 0 — is the scan looking at anything?
# ---------------------------------------------------------------------------

# One fragment per SECTION, not per source. A ``> N strings`` floor cannot see
# a single key go missing: dropping ``about["when_to_use"]`` left every coarse
# anchor intact and all but six of the strings collected (83 of 88 on the
# meta-only walk that shipped then; 155 of 161 on the current one).
_SECTION_ANCHORS = {
    "meta / about.what_it_is": "Designs binders for the targets",
    "meta / about.when_to_use": "You want to aim a binder at one specific patch",
    "meta / about.output_summary": "Ranked designs with reward scores",
    "meta / seo_faq": "How are Proteina-Complexa designs scored and ranked?",
    "meta / reward_attributions": "AlphaFold2 parameters",
    "meta / comparison_one_liner": "You have a hard target",
    "meta / EXAMPLE": "A two-chain human secreted protein",
    "adapter / blurb": "Upload a protein or small-molecule target",
    # The LEDE is element [1] — the paragraph the signed-out hero renders.
    # This anchor was "hard-target binder design tool" until review pointed out
    # that lives in element [0], the short noun phrase, so deleting the lede
    # left this check green: the one source it names, it did not pin.
    "seo_phrases / lede": "Built for the targets the standard design tools",
    "seo_phrases / noun phrase": "hard-target binder design tool",
}


def test_every_prose_source_is_reached():
    sources = _prose_sources()
    for name, strings in sources.items():
        assert strings, f"prose source {name!r} came back empty — it asserts nothing"
    blob = " ".join(s for group in sources.values() for s in group)
    missing = [name for name, anchor in _SECTION_ANCHORS.items()
               if anchor not in blob]
    assert not missing, (
        "these sections are no longer reached by the scan, so nothing polices "
        f"them: {missing}. Re-point the anchor if the copy was legitimately "
        "rewritten; do not delete the entry."
    )


# ---------------------------------------------------------------------------
# Layer 1 — the source scan
# ---------------------------------------------------------------------------


def test_no_prose_source_promises_clustering_or_diversity():
    found: dict[str, list[str]] = {}
    for name, strings in _prose_sources().items():
        bad = _offenders(strings)
        if bad:
            found[name] = bad
    assert not found, (
        "proteina copy mentions clustering or diversity in a string that is "
        f"not allowlisted:\n{found}\n\n"
        "No proteina run has produced a cluster assignment and no step in this "
        "repo computes one. If this is a new benign mention, add it to "
        "_ALLOWED_MENTIONS with the reason it is not a promise."
    )


def test_none_of_the_retired_sentences_came_back():
    """Substring, not equality: the shape that shipped was these clauses
    embedded in longer paragraphs, so equality would miss a re-paste."""
    blob = " ".join(s for group in _prose_sources().values() for s in group)
    for sentence in _RETIRED:
        assert sentence not in blob, f"retired copy is back: {sentence!r}"


# ---------------------------------------------------------------------------
# Layer 1 controls — does the scan actually bite?
# ---------------------------------------------------------------------------


# Words that END in a branch's opening root. The gap classes ``[-\s]*`` are
# zero-width-capable, so without a left word boundary these match: "code
# DUPLICATION" and "provide DUPLICATE" hit ``de[-\s]*duplicat``, and "a
# coliNEAR DUPLICATE chain" hits ``near[-\s]*duplicate``. The render layer
# scans ~225 KB of whole-page HTML including shared chrome, so a false positive
# there is a failure with no proteina cause -- the kind that trains the next
# person to widen the allowlist instead of reading it.
_NOT_PROMISES = (
    "we avoid code duplication here",
    "provide duplicate targets",
    "made duplicates of the file",
    "a colinear duplicate chain",
)


def test_the_matcher_does_not_fire_on_unrelated_words():
    fired = [s for s in _NOT_PROMISES if _MENTION.search(s)]
    assert not fired, (
        f"_MENTION matches ordinary prose: {fired}. A branch has lost its left "
        "word boundary, so it is matching the tail of an unrelated word.")


def test_every_branch_of_the_matcher_is_live():
    """One probe per alternation, because ``_RETIRED`` only reaches five of
    fourteen. Without this, a typo in any branch review added (``redundna``)
    ships green and the synonym it was meant to catch walks straight through."""
    dead = [name for name, probe in _BRANCH_PROBES.items()
            if not _offenders([probe])]
    assert not dead, f"these _MENTION branches match nothing: {dead}"


def test_every_multi_word_branch_tolerates_wrapped_whitespace():
    r"""Template copy wraps. The render layer scans raw HTML, so a promise
    broken across a newline and an indent has to match the same as one on a
    single line.

    EVERY branch whose probe spans words, not one. NINE of the fourteen
    alternations carry a whitespace-capable gap -- six did before this
    file respelled ``near.?``/``de-?`` as ``[-\s]*``, which is the kind of
    count an edit makes stale in the same breath. A version of this test
    that wrapped only ``spread of`` left the rest unpinned, and a mutation
    putting a plain space back into ``variety of`` passed the whole file.
    """
    literal = []
    for name, probe in _BRANCH_PROBES.items():
        for sep in (" ", "-"):
            if sep not in probe:
                continue
            # HYPHENS TOO. ``near-duplicate`` / ``near-identical`` spell their
            # gap ``[-\s]*``; wrapping only at spaces left the hyphen position
            # untested, and the ``near.?`` spelling it replaced could not match
            # a newline there at all.
            wrapped = probe.replace(sep, "\n              ")
            if not _offenders([wrapped]):
                literal.append(f"{name} (at {sep!r})")
    assert not literal, (
        f"these _MENTION branches only match on a single line: {literal}. "
        "Put a whitespace class between the words — the render layer reads "
        "wrapped HTML.")


def test_the_retired_sentences_all_trip_the_matcher():
    for sentence in _RETIRED:
        assert _offenders([sentence]) == [sentence], (
            f"the matcher does not flag {sentence!r} — it is decoration")


@pytest.mark.parametrize("source_key", ["meta", "adapter", "seo_phrases"])
def test_a_promise_injected_into_a_REAL_source_is_caught(source_key, monkeypatch):
    """END TO END, through the walk, one source at a time.

    The wiring between ``_prose_sources`` and ``_offenders`` is the part that
    was never exercised: the first version of this file checked the retired
    sentences as bare literals, so a walk that returned ``[]`` would have left
    every promise test green. Injecting into the live object and asserting the
    offender is attributed to the RIGHT source is what closes that.
    """
    import dataclasses

    import tools.proteina as proteina_pkg
    from blueprints.tools import _PREVIEW_SEO_PHRASES
    from tools.proteina import meta

    promise = "and clusters the winners, so you get a spread of different designs"
    if source_key == "meta":
        monkeypatch.setitem(meta.about, "output_summary", promise)
    elif source_key == "adapter":
        # ToolAdapter is a FROZEN dataclass, so the obvious
        # ``setattr(adapter, "blurb", ...)`` raises FrozenInstanceError — which
        # is how this control first failed, correctly. Replace the module
        # attribute instead; ``_prose_sources`` re-imports it per call.
        monkeypatch.setattr(
            proteina_pkg, "adapter",
            dataclasses.replace(proteina_pkg.adapter, blurb=promise),
        )
    else:
        monkeypatch.setitem(_PREVIEW_SEO_PHRASES, "proteina", ("x", promise))

    sources = _prose_sources()
    assert promise in _offenders(sources[source_key]), (
        f"a promise planted in {source_key} was not caught")


# ---------------------------------------------------------------------------
# Layer 2 — the rendered page, which does not care where a sentence came from
# ---------------------------------------------------------------------------

# Substrings allowed to appear in the RENDERED page. "GPU cluster" is the FAQ
# question, which renders twice: once visibly and once inside the FAQPage
# JSON-LD. Scanning raw HTML rather than visible text is deliberate — the
# structured data is exactly where a false claim would be machine-readable.
_ALLOWED_RENDERED = ("without a GPU cluster",)


def _rendered_offenders(body: str) -> list[str]:
    """Promise-shaped matches in a rendered page, allowlisted phrases removed.

    THE ALLOWLISTED TEXT IS EXCISED, not used as a proximity window. The window
    version skipped a match whenever the whole 21-character phrase fitted
    inside [match-60, match+60], which masked anything written just after the
    benign one: measured on that logic, a promise at a gap of 0 or 20
    characters produced ZERO offenders and the same sentence at 40 produced
    one. So the masked span was roughly 39 characters past the phrase -- twice
    over, since the phrase renders both visibly and inside the FAQPage JSON-LD.

    The allowlisted phrase is ``seo_faq[0]``, which is about GPU hardware, NOT
    the entry that carried the retired promise (``seo_faq[2]``). An earlier
    draft of this docstring called it "the FAQ entry about clustering", which
    inverts the very distinction ``_ALLOWED_MENTIONS`` exists to draw.

    TWO THINGS THE EXCISION HAS TO GET RIGHT, both found by review after a
    first version got them wrong:

      * ``(?![A-Za-z])`` -- the phrase ends in the root ``cluster``, so a plain
        ``str.replace`` also ate the root of a promise spelled as its
        CONTINUATION ("without a GPU clustering of the winners"), which then
        rendered on the page with zero offenders reported.
      * a ``\x00`` sentinel rather than a space -- joining the two sides with
        whitespace MANUFACTURED matches the body never held ("you get a
        spread" + phrase + "of different designs" became "spread of").

    Neither "accounts for itself and nothing else" nor "at any distance" is
    quite true even now; what is true is that no match is skipped for being
    near an allowed phrase, only for being inside one.
    """
    for allowed in _ALLOWED_RENDERED:
        body = re.sub(re.escape(allowed) + r"(?![A-Za-z])", "\x00", body)
    return [body[max(0, m.start() - 60):m.end() + 60].replace("\n", " ")
            for m in _MENTION.finditer(body)]


@pytest.fixture
def proteina_app(monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_PROTEINA", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def test_an_allowlisted_phrase_does_not_shield_a_promise_beside_it():
    """The allowlist must account for itself and nothing else.

    The benign phrase is the FAQ question about GPU clusters -- which is the
    FAQ entry about clustering, so it is exactly where a returning promise
    would be written. A proximity window made that the one blind spot on the
    page.
    """
    benign = "Can I run Proteina-Complexa online without a GPU cluster?"
    assert _rendered_offenders(benign) == [], "the benign phrase must not flag"
    for label, body in (
        # Just after the phrase: what the proximity window masked.
        ("adjacent", benign + " Yes. It clusters the winners for you."),
        # Spelled as a CONTINUATION of the phrase: what a plain str.replace
        # masked, by eating the ``cluster`` root the promise shares with it.
        # This is the discriminating case -- the "200 characters away" variant
        # it replaced passed under BOTH broken versions.
        ("suffix", "Can I run it without a GPU clustering of the winners?"),
    ):
        assert _rendered_offenders(body), (
            f"[{label}] a real promise was masked by the allowlisted phrase")

    # And excision must not INVENT a match by closing the gap it leaves.
    phrase = "without a GPU cluster"
    for label, body in (
        ("spread/of", "you get a spread " + phrase + " of different designs"),
        ("variety/of", "the window buys variety " + phrase + " of folds"),
        ("near/duplicates", "we drop near " + phrase + " duplicates"),
    ):
        assert _rendered_offenders(body) == [], (
            f"[{label}] excision manufactured a match the page never carried")


@pytest.mark.parametrize("signed_in", [False, True])
def test_the_rendered_tool_page_promises_no_clustering(proteina_app, signed_in):
    """BOTH auth states, because the hero swaps its paragraph.

    templates/tools/_form_hero.html:41-48 renders the SEO lede to a signed-out
    visitor and ``adapter.blurb`` to a signed-in one, so a single-state test
    reads only half the copy — and the half it skips is the indexed one.
    """
    client = proteina_app.test_client()
    # The signed-in branch needs a user context and a wallet, or the route
    # 302s instead of rendering — the same three patches
    # tests/test_public_tool_pages.py uses for its signed-in cases. Without
    # them this test read a redirect body and found no promises in it, which
    # is a pass for the wrong reason.
    ctx = SimpleNamespace(
        user_id="u-public", tier="free", balance=100, email="u@example.com")
    with patch("app.load_user_context", return_value=ctx), \
            patch("blueprints.tools.load_user_context", return_value=ctx), \
            patch("blueprints.tools.get_or_create_wallet",
                  return_value={"balance_usd": 12.5, "wallet_frozen": False}):
        if signed_in:
            with client.session_transaction() as sess:
                sess["user_email"] = "u@example.com"
        resp = client.get("/tools/proteina")
    assert resp.status_code == 200, resp.status_code
    body = resp.data.decode("utf-8", "replace")
    assert "Proteina" in body, "did not render the proteina page"

    offenders = _rendered_offenders(body)
    assert not offenders, (
        f"the rendered tool page (signed_in={signed_in}) promises clustering "
        f"or diversity:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Layer 3 — the column, in both places that declare it, and in the export
# ---------------------------------------------------------------------------


def test_cluster_id_is_not_a_campaign_column():
    columns = columns_for("proteina")
    assert columns, "columns_for('proteina') is empty — this asserts nothing"
    assert "cluster_id" not in columns, (
        "cluster_id is back in the merged campaign table; it renders an em "
        "dash on every row because nothing writes it")


def test_the_results_partial_declares_exactly_one_constant_column_list():
    found = _template_column_assignments()
    assert found == [columns_for("proteina")], (
        f"{_TEMPLATE.name} does not declare exactly one constant `columns` "
        f"list equal to columns_for('proteina'). Parsed: {found!r}. A second "
        "assignment, or one built from an expression, cannot be checked by "
        "reading — which is how `columns + ['cluster_id']` got past the "
        "regex this replaced."
    )


def test_the_results_partial_passes_that_list_through_unmodified():
    """The declaration and the argument are different things — see
    ``_template_columns_arguments``. Without this, appending at the call site
    restored the column with every other test in this file green."""
    passed = _template_columns_arguments()
    assert passed == ["columns"], (
        f"{_TEMPLATE.name} passes something other than the bare declared list "
        f"as `columns=`: {passed!r}. The rendered table uses the argument, so "
        "an expression here puts a column back that the declaration check "
        "cannot see."
    )


def test_cluster_id_never_reaches_a_downloadable_csv():
    """The page and the file have to agree.

    Dropping the column from the render lists did NOT drop it from the export:
    ``shared.exports._metric_columns`` builds its header from the STORED
    payload's ``scores`` keys, so /jobs/<id> export.csv kept shipping an empty
    column one click from the page that had stopped showing it. Stored rows are
    expected to carry the key (run_pipeline writes it, webhooks/modal.py:549
    copies it through) — expected, not observed: the production jobs table has
    not been read. Either way the fix belongs at the export layer, since no
    pipeline change can rewrite a row that is already stored.
    """
    from shared.exports import candidates_to_csv
    stored = {
        "rank": 1, "pdb_key": "design_001.pdb",
        "scores": {"total_reward": -0.18, "af2_iptm": 0.89, "af2_plddt": 0.885,
                   "rf3_score": None, "binder_scrmsd": 1.32, "cluster_id": None},
    }
    header = candidates_to_csv([stored]).splitlines()[0]
    assert "af2_iptm" in header, f"export produced no metrics: {header!r}"
    assert "cluster_id" not in header, (
        f"the downloadable CSV still carries the column the page dropped: {header!r}")

    # BOTH loops of _metric_columns, not just the scores one. The nested shape
    # above exercises the first; the designs-shape pipelines put every metric
    # at the record ROOT with no scores dict, and that is the second. With only
    # the nested case pinned, removing the guard from the root loop left this
    # test green while the column came back on a root-shaped payload.
    root_shaped = {"rank": 1, "pdb_key": "d.pdb", "iptm": 0.8, "cluster_id": None}
    root_header = candidates_to_csv([root_shaped]).splitlines()[0]
    assert "iptm" in root_header, f"export produced no metrics: {root_header!r}"
    assert "cluster_id" not in root_header, (
        f"a root-shaped payload still carries the column: {root_header!r}")


def test_a_value_the_export_cannot_PRINT_does_not_count_as_a_source():
    """"Sourced" has to mean "a value that reaches the file".

    ``_is_metric_value`` rejects lists, dicts and over-long strings, so the
    writer prints nothing for them. Treating one as a source — which a bare
    ``is not None`` does — re-opens the header on the strength of a value that
    is then blank in every row, which is the exact defect the suppression
    exists to remove.
    """
    from shared.exports import candidates_to_csv
    for label, value in (("list", ["x", "y"]), ("dict", {"a": 1}),
                         ("over-long string", "x" * 600)):
        cands = [
            {"rank": 1, "pdb_key": "a.pdb", "cluster_id": value,
             "scores": {"af2_iptm": 0.9}},
            {"rank": 2, "pdb_key": "b.pdb",
             "scores": {"af2_iptm": 0.8, "cluster_id": None}},
        ]
        header = candidates_to_csv(cands).splitlines()[0]
        assert "cluster_id" not in header.split(","), (
            f"[{label}] an unprintable value re-opened the column, so every "
            f"row carries it blank: {header!r}")


def test_a_cluster_id_that_ever_MEANS_something_is_not_deleted():
    """The suppression is scoped to a column with no source, not to a name.

    Keyed on the name alone it deleted a real value — header and number — with
    nothing failing, which would make wiring the field up a silent data loss
    rather than a visible change.
    """
    from shared.exports import candidates_to_csv

    # ZERO, not 7. Truthiness and ``is not None`` agree on 7 and disagree on 0,
    # so a 7 fixture left the filter swappable to ``if v:`` with every test
    # green -- while deleting cluster 0, the most likely first real value:
    # point 4 above records upstream renumbering cluster indices FROM 0.
    #
    # BOTH SHAPES, because the filter checks ``scores`` and the record root
    # separately. Pinning only the nested one left the root half deletable.
    for label, cand in (
        ("nested", {"rank": 1, "pdb_key": "d.pdb",
                    "scores": {"af2_iptm": 0.89, "cluster_id": 0}}),
        ("root", {"rank": 1, "pdb_key": "d.pdb", "iptm": 0.8, "cluster_id": 0}),
    ):
        header, row = candidates_to_csv([cand]).splitlines()[:2]
        columns = header.split(",")
        assert "cluster_id" in columns, (
            f"[{label}] a populated cluster_id was dropped from the export; the "
            f"filter is suppressing a NAME rather than an empty column: {header!r}")
        # By COLUMN, not by substring: `"0" in row` passes off any other
        # field's digits, which is how this file's own register of
        # false-certifying guards keeps filling up.
        assert row.split(",")[columns.index("cluster_id")] == "0", (
            f"[{label}] the column survived but the value did not: {row!r}")
