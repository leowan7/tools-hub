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

import re

import pytest

import app  # noqa: F401  — populates tools.base._REGISTRY
from shared import tool_chooser
from shared.pdb_preflight import VerdictKind, preflight_for_tool
from shared.pdb_preflight_rules import TOOL_RULES
from shared.tool_meta import meta_for
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


def test_has_example_agrees_with_the_renderer_for_every_tool():
    """The link is offered exactly when the section actually renders.

    ``has_example`` used to test only that ``example/result.json`` exists,
    while ``_example_context`` also has to parse it -- so an unparseable
    file would have advertised "See a worked example" and linked to an
    anchor the macro never rendered.
    """
    from blueprints.tools import _example_context

    checked = 0
    for adapter in tool_base.all_adapters():
        meta = meta_for(adapter.slug)
        if meta is None:
            continue
        checked += 1
        assert tool_chooser.has_example(adapter.slug) is (
            _example_context(adapter, meta) is not None
        ), f"{adapter.slug}: chooser and renderer disagree about the example"
    assert checked >= 14, f"only {checked} tools carried metadata"


def test_a_damaged_example_withholds_the_link(monkeypatch):
    """Proof by mutation: unparseable JSON must drop the link.

    Pins the parse itself, which a file-exists check would pass.
    """
    import blueprints.tools as bt

    slug = next(
        s for s in tool_chooser._FACTS if tool_chooser.has_example(s)
    )
    monkeypatch.setattr(
        bt, "_example_result", lambda s: None if s == slug else {"ok": 1}
    )
    assert tool_chooser.has_example(slug) is False


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
        picks = tool_chooser.recommend(
            "target-structure", shape="unsure", chemistry=chem,
        )
        assert picks, f"no tool answers chemistry={chem}"


def test_the_chooser_asks_no_small_molecule_question():
    """The option was removed (Leo, 2026-09-24: "we don't do any small
    molecules"), and the reason it could never be answered is driven on the
    real adapter: proteina's ligand variant refuses the visitor's own target.

    If proteina ever accepts a custom ligand target, the refusal assertion
    fails and the question can be put back.
    """
    assert "small-molecule" not in dict(tool_chooser.CHEMISTRY_CHOICES)

    proteina = _adapters()["proteina"]
    _spec, err = proteina.validate(
        {
            "preset": "ligand_binder",
            "_has_custom_target": "1",
            "target_input": "A1-150",
        },
        {},
    )
    assert err is not None and "cannot design against your own target" in err
    assert not any(
        "small-molecule" in f.chemistries for f in tool_chooser._FACTS.values()
    )


def test_a_curated_default_target_tool_tells_you_to_attach_the_file():
    """A no-upload submit must not look free of prerequisites.

    proteina accepts a submit with no file and silently designs against a
    bundled benchmark target, so its card has to say to attach one. Driven
    on the real adapter: if a future version refuses the empty submit, the
    warning is no longer needed and this fails.
    """
    proteina = _adapters()["proteina"]
    spec, err = proteina.validate({"preset": "protein_binder"}, {})
    assert err is None, "proteina no longer accepts a submit with no upload"
    assert spec.get("target_source") == "curated"
    assert spec.get("task_name"), "no default target, so nothing to warn about"

    line = tool_chooser.prerequisite_line("proteina")
    assert "must attach it" in line
    assert "benchmark target" in line


def test_the_multi_chain_filter_consults_every_tool_preflight_gates():
    """The filter must key on TOOL_RULES, not on needs_structure.

    proteina is the case that separates them: requires_pdb is False on it
    and every preset, so needs_structure is False, yet it has TOOL_RULES
    and does reach ``_multi_chain_block``. Keyed on needs_structure the
    question was skipped for it entirely.
    """
    assert tool_chooser.needs_structure("proteina") is False
    assert "proteina" in TOOL_RULES

    offered = {
        p["slug"]
        for p in tool_chooser.recommend(
            "target-structure", shape="unsure", chemistry="multi-chain",
        )
    }
    for slug in offered & set(TOOL_RULES):
        assert tool_chooser.supports_multi_chain(slug), (
            f"{slug} is offered for a two-chain site but preflight refuses one"
        )


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

    def test_an_empty_have_says_to_pick_one(self):
        """P1-3: `?have=` rendered the bare form with no explanation."""
        assert "Pick what you have from the first question" in self._get(
            "?have="
        )

    def test_a_bogus_have_says_to_pick_one(self):
        assert "Pick what you have from the first question" in self._get(
            "?have=banana"
        )

    def test_submitting_the_form_with_no_radio_picked_says_to_pick_one(self):
        """The reachable case: an unchecked radio group sends no key.

        Only the form's hidden marker arrives, which is why the message
        cannot be keyed on `have` alone.
        """
        body = self._get()
        assert 'name="asked" value="1"' in body, "marker missing from the form"
        assert "Pick what you have from the first question" in self._get(
            "?asked=1"
        )

    def test_pressing_on_with_no_shape_picked_says_to_pick_one(self):
        """The same dead end one question later.

        The shape radios send nothing when untouched, so the second
        press was indistinguishable from the first and the page came
        back unchanged.
        """
        first = self._get("?asked=1&have=target-structure")
        assert 'name="asked_shape" value="1"' in first
        assert "Pick a shape from the second question" not in first

        second = self._get("?asked=1&asked_shape=1&have=target-structure")
        assert "Pick a shape from the second question" in second

    def test_each_prompt_names_the_button_that_is_on_the_page(self):
        """The button is relabelled between the two questions.

        `comparison.html` renders "Next question" while a shape is
        still owed and "Show me the tools" otherwise, so a prompt
        naming the other label sends the visitor looking for a control
        that is not there.
        """
        for query in ("?asked=1", "?asked=1&asked_shape=1&have=target-structure"):
            body = self._get(query)
            labels = re.findall(
                r'<button type="submit"[^>]*>\s*(.*?)\s*</button>', body, re.S
            )
            assert len(labels) == 1, f"{query}: {labels}"
            quoted = "&ldquo;" + labels[0] + "&rdquo;"
            notes = [
                n
                for n in re.findall(
                    r'<p class="chooser-note">(.*?)</p>', body, re.S
                )
                if n.lstrip().startswith("Pick ")
            ]
            assert len(notes) == 1, (query, notes)
            assert quoted in notes[0], (query, labels[0], notes[0])

    def test_the_shape_prompt_is_not_shown_where_the_question_is_not_put(self):
        body = self._get("?asked=1&asked_shape=1&have=backbone")
        assert "Pick a shape from the second question" not in body
        assert 'class="chooser-pick-name"' in body

    def test_a_first_visit_is_not_told_off(self):
        assert "Pick what you have from the first question" not in self._get()

    def test_structure_plus_scfv_names_the_scfv_tool_and_its_limits(self):
        body = self._get("?have=target-structure&shape=scfv")
        assert "ESMFold2" in body
        assert "takes no structure file" in body


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


def test_having_no_preflight_rules_is_not_itself_a_refusal():
    """_multi_chain_block only runs on a STAGED target.

    esmfold2-design needs no PDB and has no TOOL_RULES entry, and that
    absence must not drop it: it is the only scFv designer in the
    target-sequence bucket (boltzgen also designs scFvs, but only from a
    structure), so dropping it emptied that answer. It IS dropped for a
    multi-chain patch, by the _ADAPTER_SINGLE_CHAIN_TARGETS check in
    recommend, whose cited reason its own validate is driven for in
    test_a_single_sequence_target_tool_is_withheld_for_a_multi_chain_patch.
    """
    assert not tool_chooser.needs_structure("esmfold2-design")
    assert "esmfold2-design" not in TOOL_RULES

    picks = {
        p["slug"]
        for p in tool_chooser.recommend(
            "target-sequence", shape="scfv", chemistry="plain"
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
    config". Aiming it at the visitor's own target is the
    ``target_source == "custom"`` path, which is a .pdb/.cif upload — so
    a visitor holding only a sequence cannot use it on their target at
    all, whatever its card says.
    """
    # The card does warn, but the warning names a file this visitor does
    # not have; being told to upload what you lack is not an answer.
    assert "must attach it" in tool_chooser.prerequisite_line("proteina")
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


# ---------------------------------------------------------------------
# The one tool offered outside its own HAVE bucket (P1-3).
# ---------------------------------------------------------------------

def _shapes(have, shape, chemistry=None):
    return {
        p["slug"]
        for p in tool_chooser.recommend(have, shape=shape, chemistry=chemistry)
    }


def test_the_scfv_designer_is_offered_to_a_visitor_holding_a_structure():
    """P1-3: structure + scFv returned BoltzGen alone.

    Its card claims the paired-scFv run is something "No other tool here
    does", so the answer that asks for exactly that must list it.
    """
    assert "esmfold2-design" in _shapes("target-structure", "scfv")
    assert "esmfold2-design" in _shapes("target-structure", "unsure")
    assert "esmfold2-design" in _shapes("target-structure", None)


def test_the_cross_bucket_offer_is_scoped_to_that_one_shape():
    """mini-protein and nanobody are answered by tools that use the upload."""
    assert "esmfold2-design" not in _shapes("target-structure", "mini-protein")
    assert "esmfold2-design" not in _shapes("target-structure", "nanobody")
    assert "esmfold2-design" not in _shapes("target-structure", "peptide")


def test_a_single_sequence_target_tool_is_withheld_for_a_multi_chain_patch():
    """One pasted sequence cannot span two chains, in EITHER bucket.

    ``tools/esmfold2_design/__init__.py::_check_protein_sequence`` refuses
    a ':' separator as a non-canonical residue, driven below. The
    target-sequence bucket offered it for a multi-chain patch before
    this: the generic filter is keyed on TOOL_RULES membership, and this
    tool, taking no upload, has no preflight rules.
    """
    assert "esmfold2-design" not in _shapes(
        "target-structure", "scfv", chemistry="multi-chain"
    )
    assert "esmfold2-design" not in _shapes(
        "target-sequence", "scfv", chemistry="multi-chain"
    )
    adapter = _adapters()["esmfold2-design"]
    spec, err = adapter.validate(
        {
            "preset": "scfv",
            "target_mode": "paste",
            "target_sequence": "A" * 60 + ":" + "A" * 60,
            "binder_framework": "trastuzumab_framework_vhvl",
        },
        {},
    )
    assert spec is None and err and "non-canonical" in err, (spec, err)


def test_its_own_bucket_still_answers_the_same_way():
    assert "esmfold2-design" in _shapes("target-sequence", "scfv")
    assert "esmfold2-design" in _shapes("target-sequence", "mini-protein")


def test_the_input_limits_are_stated_in_every_bucket_the_tool_appears_in():
    """They are facts about the adapter, not about the answer given.

    The tool's own bucket showed no prerequisite at all, so a visitor
    with a 900-residue target met TARGET_SEQ_MAX at the form instead.
    """
    own = {
        p["slug"]: p["prerequisite"]
        for p in tool_chooser.recommend("target-sequence", shape="scfv")
    }["esmfold2-design"]
    cross = {
        p["slug"]: p["prerequisite"]
        for p in tool_chooser.recommend("target-structure", shape="scfv")
    }["esmfold2-design"]
    for line in (own, cross):
        assert "30 to 800 amino acids" in line
        assert "five built-in targets" in line
        assert "three built-in humanised frameworks" in line
    # Only the cross-bucket card carries the extra sentence, which
    # answers a question only a structure holder asked.
    assert "takes no structure file" in cross
    assert "takes no structure file" not in own


def test_the_cross_bucket_line_states_the_limits_the_adapter_enforces():
    """Both clauses of the offer's prerequisite are driven, not read.

    Clause 1, "takes no structure file": requires_pdb is False on the
    adapter and on every preset, and a paste-mode submit with no files
    at all is accepted.
    Clause 2, three built-in humanised frameworks: FRAMEWORK_NAMES holds
    three, and validate refuses anything else.
    Clause 3, 30 to 800 amino acids, and five built-in targets: driven
    at both ends and against the preset list.
    """
    picks = {
        p["slug"]: p
        for p in tool_chooser.recommend("target-structure", shape="scfv")
    }
    line = picks["esmfold2-design"]["prerequisite"]
    assert "takes no structure file" in line
    assert "30 to 800 amino acids" in line
    assert "five built-in targets" in line
    assert "three built-in humanised frameworks" in line

    adapter = _adapters()["esmfold2-design"]
    assert adapter.requires_pdb is False
    assert not any(p.requires_pdb for p in adapter.presets)

    def _scfv(**over):
        form = {
            "preset": "scfv",
            "target_mode": "paste",
            "target_sequence": "A" * 60,
            "binder_framework": "trastuzumab_framework_vhvl",
        }
        form.update(over)
        return adapter.validate(form, {})

    spec, err = _scfv()
    assert err is None and spec is not None, err  # no file field needed

    from tools.esmfold2_design import FRAMEWORK_NAMES, TARGET_PRESET_NAMES

    assert len(FRAMEWORK_NAMES) == 3, FRAMEWORK_NAMES
    assert len(TARGET_PRESET_NAMES) == 5, TARGET_PRESET_NAMES
    assert _scfv(binder_framework="my_own_framework")[0] is None
    assert _scfv(target_sequence="A" * 29)[0] is None
    assert _scfv(target_sequence="A" * 801)[0] is None
    # Both ends of the range the line states are accepted, so the
    # sentence is not stricter than the refusal either.
    assert _scfv(target_sequence="A" * 30)[1] is None
    assert _scfv(target_sequence="A" * 800)[1] is None
    assert _scfv(target_mode="preset", target_name="not-a-target")[0] is None
    assert _scfv(target_mode="preset", target_name="egfr")[1] is None
