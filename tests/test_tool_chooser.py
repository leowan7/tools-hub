"""The chooser's stated prerequisites must match what actually refuses a run.

The chooser tells a visitor "you will need a structure" or "you will
need at least one residue for the binder to touch" BEFORE they pick a
tool. If that sentence is stricter than the gate, we turn people away
from a run that would have worked; if it is looser, they arrive at the
form and are refused after choosing. Either way the chooser has lied.

So every clause of that sentence is asserted here against the code that
enforces it — the preflight gate and the submit gate — not against a
restatement of the same table.
"""

from __future__ import annotations

import pytest

import app  # noqa: F401  — populates tools.base._REGISTRY
from shared import tool_chooser
from shared.pdb_preflight import VerdictKind, preflight_for_tool
from shared.pdb_preflight_rules import TOOL_RULES
from shared.pdb_preflight import _multi_chain_block
from tools import base as tool_base


@pytest.fixture(autouse=True)
def _all_tools_flagged_on(monkeypatch):
    """Every adapter on, so the catalog is not empty.

    ``shared.feature_flags.tool_enabled`` is fail-closed on a missing env
    var, and nothing in a test process sets FLAG_TOOL_*, so without this
    ``_build_tools_catalog()`` returns only the two hardcoded entries and
    every assertion about a GPU tool would pass vacuously. Function
    scoped, so ``test_a_disabled_tool_is_never_recommended`` can turn one
    back off through the same monkeypatch afterwards.
    """
    from shared.feature_flags import flag_name

    slugs = sorted(a.slug for a in tool_base.all_adapters())
    assert len(slugs) >= 14, (
        f"adapter registry holds {len(slugs)} tools; it is empty until "
        "`import app` populates it"
    )
    for slug in slugs:
        monkeypatch.setenv(flag_name(slug), "on")


def _adapters() -> dict:
    return {a.slug: a for a in tool_base.all_adapters()}


# ---------------------------------------------------------------------------
# A target big enough to clear the size floor, so the only thing a verdict
# can object to is the input under test.
# ---------------------------------------------------------------------------

def _pdb(chains: str = "A", residues: int = 120) -> bytes:
    lines = ["HEADER    SYNTHETIC"]
    serial = 1
    for chain in chains:
        for i in range(residues):
            resi = i + 1
            x = float(i)
            for name, elem in (("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")):
                lines.append(
                    f"ATOM  {serial:>5}  {name:<3} ALA {chain}{resi:>4}    "
                    f"{x:>8.3f}{0.0:>8.3f}{0.0:>8.3f}  1.00 10.00"
                    f"          {elem:>2}"
                )
                serial += 1
    lines.append("END")
    return ("\n".join(lines) + "\n").encode()


ONE_CHAIN = _pdb("A")
TWO_CHAIN = _pdb("AB")


# ---------------------------------------------------------------------------
# 1. "You will need at least one residue for the binder to touch."
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", sorted(TOOL_RULES))
def test_hotspot_clause_matches_what_preflight_refuses(slug):
    """Say hotspots are needed only when submitting none is refused.

    Drives the real gate rather than re-reading ``hotspots_required``:
    the same target, once with a hotspot and once without.
    """
    without = preflight_for_tool(
        slug, ONE_CHAIN, target_chain="A", hotspots=[],
    )
    withone = preflight_for_tool(
        slug, ONE_CHAIN, target_chain="A", hotspots=["A10"],
    )

    # The fixture itself must be acceptable, or "refused" would prove
    # nothing about hotspots.
    assert withone.kind is VerdictKind.READY, withone.reason

    refused_without_hotspots = without.kind is not VerdictKind.READY
    assert tool_chooser.needs_hotspots(slug) == refused_without_hotspots


def test_hotspot_clause_is_stated_for_at_least_one_tool():
    """Guards the test above from passing vacuously.

    If every tool dropped its hotspot requirement the parametrised test
    would still pass with both sides False, and a chooser that never
    mentions hotspots would look verified.
    """
    assert any(tool_chooser.needs_hotspots(s) for s in TOOL_RULES)


def test_no_hotspot_clause_for_tools_outside_the_gate():
    """A tool preflight does not gate must not claim a hotspot is required."""
    for slug in tool_chooser._FACTS:
        if slug not in TOOL_RULES:
            assert tool_chooser.needs_hotspots(slug) is False


# ---------------------------------------------------------------------------
# 2. "You will need a structure of your target."
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", sorted(tool_chooser._FACTS))
def test_structure_clause_matches_the_submit_gate(slug):
    """Agree with ``blueprints/tools.py:1719``, which reads BOTH flags.

    That gate is per-SELECTED-preset (``preset.requires_pdb or
    adapter.requires_pdb``) while the chooser speaks before a preset is
    chosen, so it reads every preset. The two can only agree while no
    tool has presets that disagree with each other — asserted here, so
    a future mixed-preset tool fails this test instead of shipping a
    prerequisite line that is wrong for one of its presets.
    """
    adapter = _adapters().get(slug)
    if adapter is None:  # epitope-scout / developability: no GPU adapter
        assert tool_chooser.needs_structure(slug) is False
        return

    per_preset = {
        bool(getattr(p, "requires_pdb", False)) or bool(adapter.requires_pdb)
        for p in adapter.presets
    }
    assert len(per_preset) == 1, (
        f"{slug} has presets that disagree on requires_pdb; the chooser "
        f"cannot state one prerequisite for all of them"
    )
    assert tool_chooser.needs_structure(slug) is per_preset.pop()


def test_structure_clause_splits_the_catalog():
    """Both answers occur, so neither side of the clause is vacuous."""
    answers = {tool_chooser.needs_structure(s) for s in tool_chooser._FACTS}
    assert answers == {True, False}


# ---------------------------------------------------------------------------
# 3. The multi-chain answer must only offer tools that accept two chains.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", sorted(TOOL_RULES))
def test_multi_chain_support_matches_the_block(slug):
    """Agree with ``_multi_chain_block``, the function that refuses."""
    blocked = _multi_chain_block(TOOL_RULES[slug], ["A", "B"], slug) is not None
    assert tool_chooser.supports_multi_chain(slug) is not blocked


def test_multi_chain_answer_offers_only_tools_that_accept_two_chains():
    """End to end: nothing in that answer is refused a two-chain target."""
    picks = tool_chooser.recommend(
        "target-structure", shape="unsure", chemistry="multi-chain",
    )
    assert picks, "the multi-chain answer must not be empty"
    for pick in picks:
        verdict = preflight_for_tool(
            pick["slug"], TWO_CHAIN, target_chain="A,B", hotspots=["A10", "B10"],
        )
        assert verdict.kind is VerdictKind.READY, (
            f"{pick['slug']} is offered for a multi-chain site but preflight "
            f"refuses one: {verdict.reason}"
        )


def test_multi_chain_answer_excludes_the_tools_that_are_refused():
    """The exclusion is real, not an empty filter.

    rfantibody and bindcraft read ``multi_chain_container_ready=False``
    and are offered for a single-chain site, so they are the proof that
    the chemistry filter removes something.
    """
    plain = {
        p["slug"]
        for p in tool_chooser.recommend(
            "target-structure", shape="unsure", chemistry="plain",
        )
    }
    multi = {
        p["slug"]
        for p in tool_chooser.recommend(
            "target-structure", shape="unsure", chemistry="multi-chain",
        )
    }
    dropped = plain - multi
    assert dropped, "the multi-chain filter dropped nothing"
    for slug in dropped:
        if slug in TOOL_RULES:
            assert not tool_chooser.supports_multi_chain(slug)


# ---------------------------------------------------------------------------
# 4. The chooser can only ever offer what the catalog offers.
# ---------------------------------------------------------------------------

def test_every_fact_slug_is_a_real_catalog_slug():
    """A typo'd slug would silently make an answer unreachable."""
    from shared.tools_catalog import _build_tools_catalog

    catalog = {e["slug"] for e in _build_tools_catalog()}
    unknown = set(tool_chooser._FACTS) - catalog
    assert not unknown, f"chooser names slugs the catalog does not have: {unknown}"


def test_a_disabled_tool_is_never_recommended(monkeypatch):
    """The flag gate is inherited from ``_build_tools_catalog``.

    ``shared.feature_flags.tool_enabled`` is fail-closed on a missing
    env var (shared/feature_flags.py:41-44), so clearing the flag must
    remove the tool from every answer.
    """
    from shared.feature_flags import flag_name

    before = {
        p["slug"]
        for p in tool_chooser.recommend("target-structure", shape="unsure")
    }
    assert "bindcraft" in before, "fixture assumes bindcraft is enabled"

    monkeypatch.setenv(flag_name("bindcraft"), "off")
    after = {
        p["slug"]
        for p in tool_chooser.recommend("target-structure", shape="unsure")
    }
    assert "bindcraft" not in after


def test_every_answer_reaches_at_least_one_tool():
    """No question combination may dead-end on an empty page."""
    for have, _label in tool_chooser.HAVE_CHOICES:
        assert tool_chooser.recommend(have, shape="unsure", chemistry="plain"), (
            f"no tool answers have={have}"
        )
    for shape, _label in tool_chooser.SHAPE_CHOICES:
        assert tool_chooser.recommend(
            "target-structure", shape=shape, chemistry="plain",
        ) or tool_chooser.recommend(
            "target-sequence", shape=shape, chemistry="plain",
        ), f"no tool answers shape={shape}"
    for chem, _label in tool_chooser.CHEMISTRY_CHOICES:
        assert tool_chooser.recommend(
            "target-structure", shape="unsure", chemistry=chem,
        ), f"no tool answers chemistry={chem}"


def test_worked_example_flag_matches_the_files_on_disk():
    """``has_example`` must not point at an anchor that never renders."""
    for slug in tool_chooser._FACTS:
        from blueprints.tools import _example_result
        from shared.tool_meta import meta_for

        meta = meta_for(slug)
        expected = bool(
            meta is not None
            and getattr(meta, "EXAMPLE", None)
            and _example_result(slug) is not None
        )
        assert tool_chooser.has_example(slug) is expected, slug


# ---------------------------------------------------------------------------
# 5. The rendered page: the deep link must land on something.
# ---------------------------------------------------------------------------

def test_worked_example_anchor_exists_in_the_component():
    """The chooser links to ``#worked-example``; the macro must carry it.

    A fragment with no matching id is silently a no-op in every browser,
    so nothing at runtime would report the link as broken.
    """
    from pathlib import Path

    macro = (
        Path(__file__).resolve().parents[1]
        / "templates" / "components" / "worked_example.html"
    ).read_text(encoding="utf-8")
    assert 'id="worked-example"' in macro


def test_homepage_points_at_the_chooser():
    """One line on the homepage reaches it, per the brief."""
    from pathlib import Path

    index = (
        Path(__file__).resolve().parents[1] / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert "tools.tools_comparison') }}#chooser" in index


class TestChooserRendering:
    """The /tools page with the chooser answered."""

    @pytest.fixture(autouse=True)
    def _client(self, all_tools_app):
        flask_app, _slugs = all_tools_app
        self.client = flask_app.test_client()

    def _get(self, query=""):
        resp = self.client.get("/tools" + query)
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def test_unanswered_page_shows_the_questions_and_no_answer(self):
        body = self._get()
        assert 'id="chooser"' in body
        assert "What do you have right now?" in body
        assert 'class="chooser-pick-name"' not in body

    def test_an_answer_names_tools(self):
        body = self._get("?have=target-structure&shape=nanobody")
        assert "RFantibody" in body
        assert 'class="chooser-pick-name"' in body

    def test_shape_question_is_withheld_until_the_first_answer(self):
        assert "What shape of binder" not in self._get()
        assert "What shape of binder" in self._get("?have=target-structure")

    def test_a_junk_answer_is_dropped_not_echoed(self):
        body = self._get("?have=<script>x</script>&shape=../../etc")
        assert "<script>x</script>" not in body
        assert 'class="chooser-pick-name"' not in body

    def test_deep_link_only_for_tools_that_render_an_example(self):
        body = self._get("?have=nothing")
        # epitope-scout has no meta.py and so no worked example.
        assert not tool_chooser.has_example("epitope-scout")
        assert "#worked-example" not in body

    def test_deep_link_present_for_a_tool_that_has_one(self):
        body = self._get("?have=backbone")
        assert tool_chooser.has_example("mpnn")
        assert "#worked-example" in body
