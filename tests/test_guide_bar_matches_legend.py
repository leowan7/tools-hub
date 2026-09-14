"""A tool's guide prints one bar, and it is the bar its results are judged at.

``about["output_summary"]`` is the sentence rendered under "How to read the
results" on ``/help/tools/<tool>`` (templates/help/tool_guide.html:149) and
again in the tool form's About panel (templates/components/about_panel.html:120).
Both surfaces end their ipTM entry by sending the reader to that sentence for
the tool's own pass bar:

    tool_guide.html     "this guide's own results summary above states
                         this tool's."
    about_panel.html    "the guide for the tool you ran states its one."

Cited without line numbers: both sentences are pinned by phrase in
tests/test_about_panel_iptm_bar_default.py, and both moved when the templates
were edited above them.

So whatever number that sentence prints is read as this tool's bar, while every
verdict and colour on the results table is computed from
``shared/score_legends.py``. pxdesign printed 0.70 and was judged at 0.75, so a
design at ipTM 0.72 met the guide's stated target and read as below the bar in
the cell beside it.

WHY 0.70 WAS THERE, because it is not a typo and the next reader will want to
put it back: it is the CONTAINER's own constant, ``IPTM_THRESHOLD = 0.70`` at
llm-proteinDesigner/docker/pxdesign/run_pipeline.py -- no line number, that
repo moves on its own. ``CONTAINER_GATES`` in tests/test_derived_verdicts.py
names that constant and reads its value out of the sibling checkout, skipping
when that repo is absent, so the 0.70 written just above is a cross-repo
reading and not a number this repo pins. This site
deliberately does not judge on it. The comment above ``GATE_COLUMNS`` in
shared/score_legends.py records that decision -- the bar is the legend's
``good`` value so that a column's tooltip and the verdict beside it can never
quote different numbers -- and lists pxdesign/ipTM as one of five legs where
this site is deliberately not its container. tests/test_derived_verdicts.py
pins that half, legend against container constant. This file pins the half
nothing held: PROSE against legend.

SCOPE, stated exactly, because a guard read as covering more than it does is
worse than none. It checks numbers a tool's About prose states about A METRIC
THAT TOOL HAS A LEGEND FOR, against the values that legend publishes, matching
the legend's metric token on a word boundary and ignoring case (load-bearing:
esmfold2-design writes "iptm"). Four consequences worth naming rather than
discovering later:

  * The metric is matched by its legend KEY with the comparator immediately
    after it. Prose that spells the metric differently, or puts a clause
    between metric and comparator, is not matched -- boltzgen misses on both:
    "Refolding RMSD is the design against its own refold: at or under 2
    angstroms it clears the RMSD leg", against the key ``refolding_rmsd``.
    Adjacency is the binding half: measured across all 14 summaries (not
    pinned), aliasing ``_`` to a space AND adding "at or under"/"under"/
    "below" to _CMP matches nothing extra, and only relaxing the adjacency
    rule reaches it -- the one thing _CMP's comment below rejects. That 2
    angstroms is the container's RMSD_THRESHOLD against a legend gating at
    1.5, so it IS a live prose-vs-legend divergence this file does NOT catch.
    Deliberate: the repair is to the boltzgen sentence, not to this pattern.
  * A tool with no legend for a metric is not judged here. A tool is allowed
    to have no bar.
  * ``good`` OR ``excellent`` is accepted, because a summary may legitimately
    point at the stronger band -- rfantibody's "filter at ipAE <= 6 for
    downstream wet-lab work" is its legend's ``excellent``, against a ``good``
    of 10.0. What this rejects is a number the legend does not publish at all,
    which is what 0.70 was.
  * Only top-level string values of ``about`` are read, so a threshold stated
    inside a nested field is not compared. esmfold2-design's
    ``inputs[0]["explanation"]`` is the repo's only one today; it says "pI < 6"
    and agrees with that legend's ``good`` of 6.
"""

from __future__ import annotations

import html
import re

import pytest

import app as _app_module  # noqa: F401  (import populates the adapter registry)
from shared.score_legends import SCORE_LEGENDS
from shared.tool_meta import meta_for
from tools import base as tool_base

# A superset of the comparator spellings today's summaries use: the nine
# thresholds they state are written with >=, <=, >, < and "at or above" only,
# and the word forms are here for prose not yet written. Entities are decoded
# before matching, so ``&ge;`` arrives here as the character. What keeps a word
# form from reading ordinary prose as a threshold is the ``\s*`` in the pattern
# below: the comparator has to sit immediately after the metric token, so
# "ipTM scored over 5 designs" cannot match. "ipTM over 5 designs" would, and
# no summary is worded that way today -- one that was would still have to state
# a number its legend publishes or fail here. "under"/"below" are left out
# because adding them matches nothing extra across all 14 summaries.
_CMP = r"(?:>=|<=|>|<|at least|at or above|above|over)"
_NUM = r"(\d+(?:\.\d+)?)"

#: U+2265 / U+2264, built with ``chr`` so this file stays pure ASCII and no
#: escape in it can be flattened by an editor into something that still parses.
_GE, _LE = chr(0x2265), chr(0x2264)

#: Adapter slugs, resolved once. ``import app`` above is what populates the
#: registry; without it this is empty and every assertion below is vacuous.
_SLUGS = sorted(a.slug for a in tool_base.all_adapters())

#: The (tool, metric) pairs whose About prose states a threshold today. A pair
#: that silently leaves this set means the guard stopped covering it, which is
#: how a threshold test comes to pin nothing at all.
_EXPECTED_COVERAGE = {
    ("boltz2", "ipTM"),
    ("boltz2", "n_hotspot_contacts"),
    ("boltzgen", "pLDDT"),
    ("esmfold2-design", "ipTM"),
    ("esmfold2-design", "pI"),
    ("pxdesign", "ipTM"),
    ("rfantibody", "ipAE"),
    ("rfantibody", "pAE"),
    ("rfdiffusion", "ipTM"),
}


def _visible(markup: str) -> str:
    """Tag-stripped, entity-decoded prose, as a reader sees it.

    The two comparator characters are folded to their ASCII spellings so the
    patterns below need only one form of each: ``&ge;`` and ``>=`` are the
    same claim to a reader and must be to this guard.
    """
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return text.replace(_GE, ">=").replace(_LE, "<=")


def _stated_thresholds(slug: str) -> list[tuple[str, str, float]]:
    """Every ``<metric> <comparator> <number>`` this tool's About prose states.

    Driven by the tool's OWN legend metrics, so ``\\b`` does the
    disambiguation the rest of the repo already relies on: ``pAE`` cannot
    match inside ``i_pAE`` or ``ipAE``, and ``pLDDT`` cannot match inside
    ``complex_pLDDT``, because ``_`` and the letters either side are all word
    characters and no boundary falls there.
    """
    meta = meta_for(slug)
    about = getattr(meta, "about", None) or {}
    metrics = sorted({m for (t, m) in SCORE_LEGENDS if t == slug})
    found: list[tuple[str, str, float]] = []
    for key, value in about.items():
        if not isinstance(value, str):
            continue
        text = _visible(value)
        for metric in metrics:
            pattern = r"\b" + re.escape(metric) + r"\b\s*" + _CMP + r"\s*" + _NUM
            for match in re.finditer(pattern, text, re.I):
                found.append((key, metric, float(match.group(1))))
    return found


def _published(slug: str, metric: str) -> set[float]:
    """The threshold values this tool's legend publishes for one metric."""
    legend = SCORE_LEGENDS.get((slug, metric)) or {}
    return {
        float(legend[band])
        for band in ("good", "excellent")
        if legend.get(band) is not None
    }


def test_the_adapter_registry_populated():
    """Without this the parametrised test below runs over nothing."""
    assert len(_SLUGS) >= 14, (
        f"adapter registry holds {len(_SLUGS)} tools; a registry that did "
        "not populate makes every assertion in this file vacuous"
    )


def test_the_scan_reaches_the_prose_it_claims_to_check():
    """Anti-vacuity, and it is the half that actually rots.

    The parametrised test passes for a tool whose prose states nothing, so on
    its own it cannot tell "no threshold stated" from "the scan stopped
    finding them". Re-wording a summary past the comparator list, or renaming
    a legend key, would do exactly that silently.
    """
    seen = {
        (slug, metric)
        for slug in _SLUGS
        for _key, metric, _value in _stated_thresholds(slug)
    }
    missing = _EXPECTED_COVERAGE - seen
    assert not missing, (
        f"the scan no longer sees a threshold it used to: {sorted(missing)}. "
        "Either the prose stopped stating one (update _EXPECTED_COVERAGE) or "
        "the extraction stopped matching it (fix the extraction)."
    )


@pytest.mark.parametrize("slug", _SLUGS)
def test_stated_threshold_is_a_number_its_own_legend_publishes(slug):
    """The invariant: guide prose and the legend cannot quote different bars."""
    wrong = []
    for key, metric, stated in _stated_thresholds(slug):
        published = _published(slug, metric)
        if published and stated not in published:
            wrong.append(
                f"about[{key!r}] states {metric} {stated} but "
                f"shared/score_legends.py publishes {sorted(published)} for "
                f"({slug}, {metric})"
            )
    assert not wrong, (
        f"{slug}: the guide states a bar the legend that judges its results "
        f"does not publish:\n  " + "\n  ".join(wrong)
    )


def test_pxdesign_states_the_bar_its_designs_are_judged_at():
    """The specific pair this file was written for.

    Pinned to the legend's ``good`` rather than to a literal, because
    agreement is the invariant and freezing 0.75 would only move the drift
    one file over. The number today is 0.75; the tempting wrong answer is the
    container's 0.70 (see the module docstring).
    """
    bar = SCORE_LEGENDS[("pxdesign", "ipTM")]["good"]
    stated = [
        value
        for _key, metric, value in _stated_thresholds("pxdesign")
        if metric == "ipTM"
    ]
    assert stated == [bar], (
        f"pxdesign's About prose states ipTM {stated} while its results are "
        f"judged at {bar}; both pointers send the reader to that prose for "
        "this tool's pass bar"
    )


def test_the_guide_page_shows_that_bar_to_a_reader(monkeypatch):
    """The number reaching a rendered page, not just a dict.

    Both defects this file exists for were in prose that renders, so a check
    that never leaves Python would not have seen either.
    """
    from shared.feature_flags import flag_name  # noqa: PLC0415

    for slug in _SLUGS:
        monkeypatch.setenv(flag_name(slug), "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    flask_app = _app_module.create_app()
    flask_app.config["TESTING"] = True

    resp = flask_app.test_client().get("/help/tools/pxdesign")
    assert resp.status_code == 200, f"guide page -> {resp.status_code}"
    text = _visible(resp.get_data(as_text=True))

    bar = SCORE_LEGENDS[("pxdesign", "ipTM")]["good"]
    assert re.search(rf"ipTM\s*>=\s*{re.escape(str(bar))}", text), (
        f"/help/tools/pxdesign does not show 'ipTM >= {bar}' to a reader"
    )
