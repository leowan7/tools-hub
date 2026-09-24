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
    """Agree with ``blueprints/tools.py::tool_submit``, which reads BOTH flags.

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


@pytest.mark.parametrize("have", sorted(tool_chooser.DESIGN_HAVES))
def test_multi_chain_answer_offers_only_tools_that_accept_two_chains(have):
    """End to end: nothing in that answer refuses a two-chain input.

    Both design buckets are asked, not just ``target-structure``: the
    ``target-sequence`` bucket is the one that carries the folding tools
    as a "get a structure first" step, and preflight never sees those --
    they take no upload, so their refusal lives in the adapter.
    """
    picks = tool_chooser.recommend(
        have, shape="unsure", chemistry="multi-chain",
    )
    assert picks, f"the multi-chain answer must not be empty for {have}"
    for pick in picks:
        assert pick["slug"] not in tool_chooser._ADAPTER_MULTI_CHAIN_REFUSALS, (
            f"{pick['slug']} is offered for a site spanning two chains but "
            "its own validate refuses a two-chain input"
        )
        if not tool_chooser.needs_structure(pick["slug"]):
            continue
        verdict = preflight_for_tool(
            pick["slug"], TWO_CHAIN, target_chain="A,B", hotspots=["A10", "B10"],
        )
        assert verdict.kind is VerdictKind.READY, (
            f"{pick['slug']} is offered for a multi-chain site but preflight "
            f"refuses one: {verdict.reason}"
        )


# The form field each folding adapter reads its FASTA from. They differ:
# af2 reads "fasta" (tools/af2/__init__.py::validate), esmfold and
# colabfold read "fasta_text". A single generic probe cannot derive this,
# which is why _ADAPTER_MULTI_CHAIN_REFUSALS is typed and pinned here.
_FOLDER_FASTA_FIELD = {
    "af2": "fasta",
    "colabfold": "fasta_text",
    "esmfold": "fasta_text",
}
_FOLD_SEQ = "MKTAYIAKQRQISFVKSHFSRQ" * 2


def test_only_esmfold_refuses_a_two_chain_fasta():
    """Drive the real validate of all three folding adapters.

    Fails if ESMFold ever accepts a multimer (the set is then stale) or
    if AlphaFold2 / ColabFold ever start refusing one (the set is then
    incomplete), so the two cannot drift apart silently.
    """
    adapters = {a.slug: a for a in tool_base.all_adapters()}
    one = f">a\n{_FOLD_SEQ}\n"
    two = f">a\n{_FOLD_SEQ}\n>b\n{_FOLD_SEQ}\n"
    for slug, field in _FOLDER_FASTA_FIELD.items():
        assert adapters[slug].validate({field: one}, {})[1] is None, (
            f"{slug} refused a single-chain FASTA, so the two-chain result "
            "below would prove nothing"
        )
        refused = adapters[slug].validate({field: two}, {})[1] is not None
        expected = slug in tool_chooser._ADAPTER_MULTI_CHAIN_REFUSALS
        assert refused is expected, (
            f"{slug}: validate {'refuses' if refused else 'accepts'} a "
            f"two-chain FASTA but _ADAPTER_MULTI_CHAIN_REFUSALS says "
            f"{'refuses' if expected else 'accepts'}"
        )


def test_every_adapter_multi_chain_refusal_slug_is_a_real_tool():
    slugs = {a.slug for a in tool_base.all_adapters()}
    assert tool_chooser._ADAPTER_MULTI_CHAIN_REFUSALS <= slugs


def test_a_backbone_tool_does_not_call_the_structure_a_target():
    """mpnn's upload is the customer's own backbone, not a target.

    The other structure-taking tools must keep saying "your target", so
    this cannot pass by renaming the noun everywhere.
    """
    assert "your backbone" in tool_chooser.prerequisite_line("mpnn")
    assert "your target" not in tool_chooser.prerequisite_line("mpnn")
    others = [
        s
        for s in tool_chooser._FACTS
        if s != "mpnn" and tool_chooser.needs_structure(s)
    ]
    assert others, "no other structure-taking tool left to contrast with"
    for slug in others:
        assert "your target" in tool_chooser.prerequisite_line(slug)


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
    env var (shared/feature_flags.py::tool_enabled), so clearing the flag must
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
    # Every chemistry too, not just "plain": the radios share one GET form,
    # so a chemistry answer from a previous submit arrives with whatever
    # `have` is picked next, including the buckets that never ask it.
    for have, _label in tool_chooser.HAVE_CHOICES:
        for chem, _clabel in tool_chooser.CHEMISTRY_CHOICES:
            assert tool_chooser.recommend(have, shape="unsure", chemistry=chem) or (
                have in tool_chooser.DESIGN_HAVES
            ), f"no tool answers have={have} with a stale chemistry={chem}"
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

    def test_a_no_match_answer_still_says_nothing_matched(self):
        """The prep step is not an answer to the question that was asked.

        ``?have=target-sequence&shape=nanobody`` matches no designer, but
        the folding tools still qualify as a prep step; the note must not
        be suppressed by their presence, and must not point at a list of
        tools "above" that is not there.
        """
        body = self._get("?have=target-sequence&shape=nanobody")
        assert "No tool in the catalog covers that combination" in body
        assert "which is what the tools above need" not in body

    def test_a_matching_answer_does_not_say_nothing_matched(self):
        body = self._get("?have=target-structure&shape=nanobody")
        assert "No tool in the catalog covers that combination" not in body


# ---------------------------------------------------------------------
# Adapter-level residue refusals
#
# The preflight hotspot gate only reaches tools with a TOOL_RULES entry.
# iggm has none, so ``needs_hotspots("iggm")`` is False and the
# prerequisite line would have named only the structure — while
# ``tools/iggm/__init__.py::validate`` refuses a residue-less submit
# outright. These drive that validate in both directions.
# ---------------------------------------------------------------------

# 128 aa each: inside the 80-400 aa window ``_parse_antibody_fasta``
# enforces, so validate reaches the epitope check, which is last.
_IGGM_H = "QVQLVESGGGLVQPGG" * 8
_IGGM_L = "DIQMTQSPSSLSASVG" * 8


def _iggm_form(epitope: str) -> dict:
    return {
        "preset": "complex_prediction",
        "fasta": "\n".join([">H", _IGGM_H, ">L", _IGGM_L, ""]),
        "target_chain": "A",
        "epitope": epitope,
    }


def _iggm_adapter():
    adapter = {a.slug: a for a in tool_base.all_adapters()}.get("iggm")
    assert adapter is not None, "iggm is not in the adapter registry"
    return adapter


def test_iggm_validate_refuses_a_submit_with_no_epitope():
    """The refusal the prerequisite line has to mention actually happens."""
    adapter = _iggm_adapter()
    spec, err = adapter.validate(_iggm_form(""), {})
    assert spec is None
    assert "epitope" in (err or "").lower()

    ok_spec, ok_err = adapter.validate(_iggm_form("45 46 47"), {})
    assert ok_err is None, f"the same form with an epitope was refused: {ok_err}"
    assert ok_spec is not None


def test_iggm_prerequisite_line_names_the_residues_the_adapter_demands():
    """Looser than the gate is the failure this whole module exists to stop."""
    adapter = _iggm_adapter()
    _spec, err = adapter.validate(_iggm_form(""), {})
    assert err is not None

    line = tool_chooser.prerequisite_line("iggm")
    assert "residue" in line.lower(), line
    assert "epitope" in line.lower(), line


def test_every_adapter_residue_refusal_slug_lacks_tool_rules():
    """Two mechanisms, never both — or the sentence states it twice.

    ``prerequisite_line`` appends the adapter clause unconditionally, so
    a slug that gained a TOOL_RULES entry with hotspots_required=True
    would produce "at least one residue ... and the antigen residues ...".
    """
    for slug in tool_chooser._ADAPTER_RESIDUE_REFUSALS:
        assert slug not in TOOL_RULES or not TOOL_RULES[slug].hotspots_required, (
            f"{slug} is now gated by preflight too; drop its entry from "
            "_ADAPTER_RESIDUE_REFUSALS or the prerequisite line doubles up"
        )


def test_every_adapter_residue_refusal_slug_is_a_real_tool():
    slugs = {a.slug for a in tool_base.all_adapters()}
    assert set(tool_chooser._ADAPTER_RESIDUE_REFUSALS) <= slugs


def test_a_stale_chemistry_answer_does_not_empty_a_bucket_that_never_asked():
    """Question 3 is only PUT in the two design buckets.

    The have/shape/chemistry radios share one GET form, so switching
    question 1 to a single-answer bucket resubmits the old chemistry.
    Every tool in those buckets is a non-designer, so a chemistry filter
    that is not scoped to DESIGN_HAVES wipes the answer out entirely.
    """
    singles = [
        have
        for have, _label in tool_chooser.HAVE_CHOICES
        if have not in tool_chooser.DESIGN_HAVES
    ]
    assert singles, "HAVE_CHOICES has no non-design bucket to test"
    for have in singles:
        baseline = {p["slug"] for p in tool_chooser.recommend(have)}
        assert baseline, f"have={have} answers nothing even with no chemistry"
        for chem, _clabel in tool_chooser.CHEMISTRY_CHOICES:
            stale = {
                p["slug"]
                for p in tool_chooser.recommend(
                    have, shape="mini-protein", chemistry=chem
                )
            }
            assert stale == baseline, (
                f"have={have} lost {sorted(baseline - stale)} to a stale "
                f"chemistry={chem} that this bucket never asked for"
            )


def test_multi_chain_does_not_drop_a_tool_that_takes_no_upload():
    """_multi_chain_block only runs on a STAGED target.

    esmfold2-design needs no PDB, so nothing refuses it for a two-chain
    target; dropping it emptied the only scFv answer in the catalog.
    """
    assert not tool_chooser.needs_structure("esmfold2-design")
    assert "esmfold2-design" not in TOOL_RULES

    picks = {
        p["slug"]
        for p in tool_chooser.recommend(
            "target-sequence", shape="scfv", chemistry="multi-chain"
        )
    }
    assert "esmfold2-design" in picks, sorted(picks)

    # Vacuity: the filter still bites a tool that DOES take an upload.
    structured = {
        p["slug"]
        for p in tool_chooser.recommend(
            "target-structure", shape="unsure", chemistry="multi-chain"
        )
    }
    assert "rfantibody" not in structured, sorted(structured)


def test_supports_multi_chain_reads_both_flags_the_gate_ands():
    """_multi_chain_block ANDs supported AND container_ready."""
    for slug, rules in TOOL_RULES.items():
        expected = bool(
            rules.multi_chain_supported and rules.multi_chain_container_ready
        )
        assert tool_chooser.supports_multi_chain(slug) is expected, slug


def test_proteina_is_not_offered_to_a_visitor_with_no_structure():
    """Its no-upload path runs a benchmark target, not the visitor's.

    ``tools/proteina/__init__.py`` module docstring: a curated task is
    "a repo-bundled benchmark task whose target is baked into the
    config". requires_pdb is False on every preset, so
    ``prerequisite_line`` has no clause to warn them with — the only
    honest fix is to leave it out of the sequence-only bucket.
    """
    assert tool_chooser.prerequisite_line("proteina") == ""
    seq = {
        p["slug"]
        for p in tool_chooser.recommend("target-sequence", shape="mini-protein")
    }
    assert "proteina" not in seq, sorted(seq)

    struct = {
        p["slug"]
        for p in tool_chooser.recommend("target-structure", shape="mini-protein")
    }
    assert "proteina" in struct, sorted(struct)


def test_a_small_molecule_target_is_not_offered_the_folding_tools():
    """Nothing to fold: a small molecule has no protein sequence.

    Guards the branch in ``recommend`` that drops non-designers for this
    answer. The vacuity check is the companion assertion: the same query
    with a protein target does return them.
    """
    folders = {"af2", "colabfold", "esmfold"}
    small = {
        p["slug"]
        for p in tool_chooser.recommend(
            "target-sequence", shape="unsure", chemistry="small-molecule"
        )
    }
    assert not (small & folders), sorted(small & folders)

    plain = {
        p["slug"]
        for p in tool_chooser.recommend(
            "target-sequence", shape="unsure", chemistry="plain"
        )
    }
    assert folders <= plain, sorted(folders - plain)
