"""Proteina copy may not promise a small-molecule target the user supplies.

``tools/proteina/__init__.py::_CUSTOM_TARGET_PRESETS`` is ``{"protein_binder"}``
and ``validate`` refuses any other preset that arrives with a staged target, so
``ligand_binder`` only ever designs against a benchmark ligand bundled with the
upstream repo. The ``seo_faq`` entry shipped saying the opposite -- "the
ligand-binder variant takes a small-molecule target as an SDF and designs de
novo binders scored by the RoseTTAFold3 reward" -- and that answer renders
twice: as visible copy (templates/components/about_panel.html:353) and inside
the page's FAQPage JSON-LD (blueprints/tools.py:988-1004), so it was
rich-result eligible. Five other surfaces carried the same promise (the adapter
blurb, the signed-out hero lede, ``comparison_one_liner``, a ``when_to_use``
bullet, and the refusal in blueprints/targets.py).

THIS GUARD IS INVERTED, AND THE INVERSION IS THE POINT. It does not try to
recognise a promise. Three rounds of review proved a promise-shaped pattern
cannot be written: each round produced wording the previous pattern missed --
possession before the noun, then after it ("a molecule you supply"), then with
no second person at all ("Custom ligand uploads are supported"), then a plural
the trailing word boundary refused ("compounds"), then the promise split over
two sentences. Each fix widened the pattern and the next round walked around
it, because English has no finite list of ways to offer something.

So the scan is for the VOCABULARY alone -- ligand, molecule, compound, SDF,
.mol, small molecule, singular or plural -- with no notion of who supplies
what, and every string that matches must appear verbatim in ``_REVIEWED``.
There are 16, listed below, and they are the whole of this product's
molecule-adjacent copy. Consequences, all deliberate:

* A new sentence naming any of that vocabulary fails this test, whatever it
  says. The author adds it to ``_REVIEWED``, which is the moment someone
  checks it against ``_CUSTOM_TARGET_PRESETS``.
* An EDIT to a reviewed sentence fails too, because the entry stops matching.
  That is not friction to be engineered away; re-approval is the mechanism.
* An honest sentence gets no exemption. Several entries below refuse a custom
  molecule rather than promise one -- no lexical rule separates a promise from
  its negation, so the negations are reviewed strings like everything else.

This fails CLOSED. A "skip it when the sentence also says benchmark" shortcut
would keep ``_REVIEWED`` small and fail OPEN on the first promise that happens
to mention a benchmark, so it is not used.

The two halves are checked together on purpose. If the adapter ever grows a
custom-ligand path, ``test_the_enforcing_set_still_excludes_ligand_binder``
fails first, and the right repair is to restore the copy, not to delete this
file.

Surfaces come from ``test_proteina_promises_no_clustering._prose_sources``
(meta, adapter, hero lede) plus two that walk cannot see: the campaign form
template and ``blueprints/targets.py::_REFUSED_PRESETS``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_proteina_promises_no_clustering import _prose_sources

_REPO = Path(__file__).resolve().parents[1]

# The vocabulary, and nothing about who supplies it. Plurals included: round 3
# of review walked past a singular-only ``compound`` with "Custom compounds
# are supported".
_VOCAB = re.compile(
    r"small[-\s]molecules?|\bSDFs?\b|\.mol\b|\bcompounds?\b"
    r"|\bligands?\b|\bmolecules?\b",
    re.I,
)

_REVIEWED = {
    # tools/proteina/meta.py -- the about panel, the FAQ, the FAQPage
    # JSON-LD, the homepage card and /tools all read from here.
    (
        "The only variant that designs against a structure you "
        "upload; the others run curated ligand and motif "
        "benchmarks. It also settles the <code>rf3_score</code> "
        "column below, which is empty on every row: RF3 is a second "
        "scoring stack those other variants need, a protein binder "
        "run does not, and it is not free. That is a consequence of "
        "this choice rather than a switch of its own &mdash; there "
        "is no RF3 control on the form."
    ),
    (
        "Designs binders for the targets the standard tools find "
        "hard: a recessed pocket, or a site spanning two chains. "
        "Rather than generating candidates and hoping, it searches "
        "— it generates, re-folds every candidate and scores how "
        "well it grips your target, keeps what scores well and "
        "generates again from there. Which model does that scoring "
        "follows the target: a protein target is scored by an "
        "AlphaFold2 refold, a benchmark ligand or motif task by "
        "RoseTTAFold3, with a physics force field added where it "
        "applies. The run splits into independent shards across as "
        "many GPUs as your balance funds — each shard is a "
        "separately seeded search — and the designs are ranked "
        "globally across all of them into one pooled table. "
        "Proteina-Complexa, Didi et al., ICLR 2026."
    ),
    (
        "<code>protein_binder</code> for a protein target (AF2 "
        "reward) and the only variant that can take a target of "
        "yours, <code>ligand_binder</code> for a bundled benchmark "
        "ligand task (RF3 reward), <code>motif_ame</code> for motif "
        "scaffolding / enzyme active sites, or "
        "<code>validate</code> for a free config check before "
        "spending GPU."
    ),
    (
        "Your own structure, or a curated benchmark task whose "
        "target is baked in &mdash; the two are mutually exclusive. "
        "Uploading your own is available on the protein-binder "
        "variant; the ligand and motif variants run curated tasks "
        "(their tasks resolve from separate upstream registries)."
    ),
    (
        "Ranked designs with reward scores (AF2 pLDDT / ipTM for "
        "protein, RF3 score for ligand / motif, force-field energy "
        "where applicable), a self-consistency re-fold RMSD, and "
        "downloadable structures. The ligand and motif variants "
        "score on RF3 only."
    ),
    (
        "RoseTTAFold3 via RosettaCommons foundry (BSD) — ligand + "
        "motif reward."
    ),
    "Can Proteina-Complexa design binders against my own small molecule?",
    (
        "No. The only target you can supply is a protein structure "
        "(.pdb or .cif), on the protein-binder variant, scored by "
        "AlphaFold2 confidence. The ligand-binder and motif "
        "variants design against benchmark tasks bundled with the "
        "model rather than anything you upload, so a molecule of "
        "your own is refused before any GPU runs."
    ),
    (
        "Every candidate is re-folded and scored against your "
        "target as it is generated, and which model does that "
        "scoring follows the target: a protein target is scored by "
        "an AlphaFold2 refold, a benchmark ligand or motif task by "
        "RoseTTAFold3, with a physics force field added where it "
        "applies. Each shard keeps what scores well, and the hub "
        "then ranks across every shard at once, so one pooled table "
        "shows the best-scoring designs from the whole run rather "
        "than one table per GPU."
    ),
    # tools/proteina/__init__.py -- the blurb and the preset descriptions.
    "Ligand binder (de novo, vs a bundled benchmark ligand)",
    (
        "Design de novo binders against one of the small-molecule "
        "targets bundled with the model. Scored by the RoseTTAFold3 "
        "reward (the force field does not support protein-ligand "
        "complexes). Your own molecule cannot be used: the task "
        "resolves from a separate upstream registry, so this "
        "variant is limited to the curated ligand tasks."
    ),
    # the campaign form: option labels, field labels, hints.
    "Ligand binder (vs a bundled benchmark ligand)",
    (
        "Which Proteina-Complexa model variant to run. Only the "
        "protein binder designs against a target you supply; the "
        "ligand and motif variants run benchmark tasks bundled with "
        "the model."
    ),
    "Target molecule (.sdf)",
    (
        "Leave this blank — the ligand variant designs against a "
        "benchmark ligand bundled with the model, and an uploaded "
        "molecule is refused."
    ),
    # blueprints/targets.py -- the refusal shown when a
    # stored target is launched with a preset that cannot take it.
    (
        "the ligand variant can only design against a benchmark "
        "ligand bundled with the model, not a target of your own. "
        "Pick the protein binder variant instead."
    ),
}


def _offenders(strings):
    """The one predicate. Every check and every control calls it."""
    return [s for s in strings if _VOCAB.search(s) and s not in _REVIEWED]


# Inline tags that sit INSIDE a sentence. Round 3 of review split a promise
# with one -- "Upload your own <code>.sdf</code> molecule" -- and the node
# scan below returned it as three fragments, two of them under the length
# floor. Removing these before the scan keeps such a sentence whole.
_INLINE_TAGS = r"</?(?:code|strong|em|b|i|u|span|a|small|kbd|abbr|sup|sub)\b[^>]*>"

# Attributes a reader actually sees. Round 3: a promise in a ``placeholder``
# or ``title`` was invisible to a scan that only read node text.
_VISIBLE_ATTRS = r'(?:placeholder|title|aria-label|alt)="([^"]{8,})"'


def _visible_text(path):
    """Text a reader sees in a Jinja template: nodes plus visible attributes."""
    src = (_REPO / path).read_text(encoding="utf-8")
    # Script and style bodies are not prose, and they carry identifiers that
    # match the vocabulary.
    src = re.sub(r"<(script|style)\b.*?</\1>", " ", src, flags=re.S | re.I)
    # Jinja becomes a SPACE, not nothing and not a node boundary. A boundary
    # (round 1) silently dropped every node with an expression in the middle,
    # and this template is dense with them; deleting outright (round 2) glued
    # the last word of one branch onto the first word of the next.
    src = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", " ", src, flags=re.S)
    out = [re.sub(r"\s+", " ", a).strip()
           for a in re.findall(_VISIBLE_ATTRS, src, re.I)]
    src = re.sub(_INLINE_TAGS, "", src, flags=re.I)
    out += [re.sub(r"\s+", " ", t).strip()
            for t in re.findall(r">([^<>]{8,}?)<", src)]
    return [s for s in out if s]


def _all_surfaces():
    """Every surface this product's molecule vocabulary can reach.

    The option label at templates/runs/new.html read "Ligand binder (vs a
    small molecule)" and the .sdf hint read "Small-molecule target. Optional
    for curated ligand tasks." Neither lives in meta, the adapter or the hero
    lede, so the walk _prose_sources does would have stayed green with the
    promise fully intact on the campaign form -- which is how it survived
    there until 2026-09-24.
    """
    from blueprints.targets import _REFUSED_PRESETS

    sources = dict(_prose_sources())
    sources["templates/runs/new.html"] = _visible_text("templates/runs/new.html")
    sources["targets / _REFUSED_PRESETS"] = [
        msg for (tool, _preset), msg in _REFUSED_PRESETS.items()
        if tool == "proteina"
    ]
    return sources


def test_every_reviewed_string_is_still_a_live_surface():
    """No dead entries. A _REVIEWED entry matching no surface is a
    pre-authorised hole: paste that sentence back anywhere and the check
    below waves it through. It also catches the copy edit that left its old
    text sitting here."""
    live = {s for group in _all_surfaces().values() for s in group}
    orphaned = sorted(_REVIEWED - live)
    assert not orphaned, (
        f"these _REVIEWED entries match no current surface: {orphaned}. "
        "Delete them, or replace them with the copy that succeeded them."
    )


def test_the_enforcing_set_still_excludes_ligand_binder():
    """The premise. Both halves, so neither can rot silently."""
    from tools.proteina import _CUSTOM_TARGET_PRESETS, adapter

    assert "ligand_binder" not in _CUSTOM_TARGET_PRESETS, (
        "ligand_binder now accepts a custom target. Restore the small-molecule "
        "copy this file was written to remove, then delete this file."
    )
    _spec, err = adapter.validate(
        {"preset": "ligand_binder", "_has_custom_target": "1"}, {}
    )
    assert err is not None and "cannot design against your own target" in err


def test_no_proteina_surface_names_a_molecule_without_review():
    found = {}
    for name, strings in _all_surfaces().items():
        assert strings, f"surface {name!r} came back empty -- it asserts nothing"
        bad = _offenders(strings)
        if bad:
            found[name] = bad
    assert not found, (
        "proteina copy names a molecule in wording nobody has reviewed:\n"
        f"{found}\n\n"
        "_CUSTOM_TARGET_PRESETS holds protein_binder only -- validate() "
        "refuses a ligand_binder run carrying a staged target, so the ligand "
        "variant designs against a bundled benchmark ligand, never an upload. "
        "Check the string against that, then add it to _REVIEWED. If it "
        "promises a molecule the user supplies, the string is the bug, not "
        "this test."
    )


@pytest.mark.parametrize(
    "injected",
    [
        # The sentences this branch retired.
        "The ligand-binder variant takes a small-molecule target as an SDF.",
        "Upload your own ligand and the search designs against it.",
        "Your target is a small molecule rather than a protein.",
        "Ligand binder (vs a small molecule)",
        "Small-molecule target. Optional for curated ligand tasks.",
        "the ligand variant needs a small-molecule SDF, and this target is a "
        "protein structure.",
        # Round 1 of review: a fitted verb list held none of these.
        "Upload your own molecule and the search designs against it.",
        "The ligand variant accepts your SDF.",
        "Attach a .mol file and the ligand variant designs against it.",
        "Design against any small molecule you like.",
        "Give us your ligand and we design against it.",
        # Round 2: possession written AFTER the noun, and a noun the pattern
        # did not hold at all.
        "Design against a molecule you provide.",
        "The ligand variant designs against a ligand you supply.",
        "Bring your own compound and the search designs against it.",
        "Point the ligand variant at any molecule you like.",
        # Round 3: no second person anywhere, so the proximity rule that
        # replaced the verb list had nothing to fire on.
        "Custom ligand uploads are supported.",
        "Ligand targets can be uploaded on the campaign form.",
        "The ligand variant accepts an uploaded ligand.",
        "Any ligand can be used as the target.",
        "Send us a ligand and we design against it.",
        # Round 3: a plural the trailing word boundary refused.
        "Custom compounds are supported as targets.",
        # Round 3: the noun in one sentence, the person in the next.
        "Bring a ligand of any class. Whatever you upload is what we design "
        "against.",
        # Round 3: split by an inline tag, which the node scan returned as
        # three fragments, two below the length floor.
        "Upload your own .sdf molecule and the ligand variant designs "
        "against it.",
    ],
)
def test_the_scan_bites(injected):
    """Control: none of these is a reviewed string, so each one is caught.

    Every entry defeated some earlier version of this guard. They fail now not
    because the pattern grew to hold them but because the pattern stopped
    trying to judge wording: naming the vocabulary at all is what fails.
    """
    assert _offenders([injected]) == [injected]


def test_the_scan_is_not_vacuous():
    """Control the other way. The vocabulary is the trigger, so prose that
    does not name a molecule passes -- otherwise every string would fail and
    the check above would prove nothing.

    Note what is NOT claimed here: an honest sentence that names the
    vocabulary is not exempt. It fails until it is reviewed. That is the
    design, so there is no list of honest molecule sentences to pass here.
    """
    assert _offenders([
        "Upload a protein target, name the chain and the residues you want "
        "gripped, and set how many designs to fund.",
        "scores how well it grips your target",
        "Ranked designs with reward scores, re-folded as they are generated.",
    ]) == []
