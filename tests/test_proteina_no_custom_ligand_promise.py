"""Proteina copy may not promise a small-molecule target the user supplies.

``tools/proteina/__init__.py::_CUSTOM_TARGET_PRESETS`` is ``{"protein_binder"}``
and ``validate`` refuses any other preset that arrives with a staged target, so
``ligand_binder`` only ever designs against a benchmark ligand bundled with the
upstream repo. The ``seo_faq`` entry shipped saying the opposite — "the
ligand-binder variant takes a small-molecule target as an SDF and designs de
novo binders scored by the RoseTTAFold3 reward" — and that answer renders
twice: as visible copy (templates/components/about_panel.html:353) and inside
the page's FAQPage JSON-LD (blueprints/tools.py:988-1004), so it was
rich-result eligible. Four other surfaces carried the same promise
(the adapter blurb, the signed-out hero lede, ``comparison_one_liner`` and a
``when_to_use`` bullet).

The two halves are checked together on purpose. If the adapter ever grows a
custom-ligand path, ``test_the_enforcing_set_still_excludes_ligand_binder``
fails first, and the right repair is to restore the copy, not to delete this
file.

Surfaces come from ``test_proteina_promises_no_clustering._prose_sources`` —
the same three-source walk (meta, adapter, hero lede), so a new surface is
added in one place.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_proteina_promises_no_clustering import _prose_sources

_REPO = Path(__file__).resolve().parents[1]

# ONE PATTERN, NOT A VERB/NOUN PAIR. The first version of this guard required a
# supply verb AND a molecule noun in the same string, with the verb list fitted
# to the three sentences being retired. Review drove it on plausible future
# copy and it missed every one: "Upload your own molecule", "The ligand variant
# accepts your SDF", "Attach a .mol file", "Design against any small molecule
# you like". A verb list is open-ended and a fitted one only proves it matches
# its own training set.
#
# So the noun carries the check on its own. Naming a small molecule, an SDF or
# a .mol file at all is the offence, because this product has no path for one:
# _CUSTOM_TARGET_PRESETS is {"protein_binder"}. ``ligand`` alone is NOT in here
# -- it is the honest word for what the bundled benchmark tasks hold and for
# the RF3 reward, and scanning it would flag every scoring sentence. ``your
# ligand``/``your molecule`` is, because possession is the promise.
#
# ``.mol`` is live vocabulary, not hypothetical: templates/runs/new.html:150
# is ``accept=".sdf,.mol"``.
_PROMISE = re.compile(
    r"small[-\s]molecule|\bSDF\b|\.mol\b|\byour (?:own )?(?:ligand|molecule)\b",
    re.I,
)

# Strings that pair the two while REFUSING, not promising. Each is here because
# saying "you cannot upload a molecule" needs both halves of the pattern.
# Strings that NAME a small molecule while refusing one, or that name the
# .sdf field itself. Each needs a reason, because the pattern above is the
# whole check -- an unexplained entry here is a hole.
_ALLOWED = {
    # The FAQ question. It has to contain the phrase a visitor would search
    # for; the answer two lines below it is "No".
    "Can Proteina-Complexa design binders against my own small molecule?",
    # The FAQ answer, the ligand preset description, and the .sdf field label
    # on the campaign form -- all three say the molecule is the model's, not
    # yours, or name a field whose hint says to leave it blank.
    (
        "No. The only target you can supply is a protein structure "
        "(.pdb or .cif), on the protein-binder "
        "variant, scored by AlphaFold2 confidence. The ligand-binder and "
        "motif variants design against benchmark tasks bundled with the "
        "model rather than anything you upload, so a molecule of your own "
        "is refused before any GPU runs."
    ),
    (
        "Design de novo binders against one of the small-molecule "
        "targets bundled with the model. Scored by the RoseTTAFold3 "
        "reward (the force field does not support protein-ligand "
        "complexes). Your own molecule cannot be used: the task "
        "resolves from a separate upstream registry, so this variant "
        "is limited to the curated ligand tasks."
    ),
    # templates/runs/new.html: the file input's own label. Its hint, one node
    # later, says to leave it blank and why.
    "Target molecule (.sdf)",
}


def _offenders(strings):
    """The one predicate. Every check and both controls call it."""
    return [s for s in strings if _PROMISE.search(s) and s not in _ALLOWED]


def _template_text(path):
    """Visible text nodes from a Jinja template.

    ``>text<`` runs with no tag or Jinja delimiter inside, which is what an
    ``<option>`` label and a ``<div class="hint">`` are. Crude on purpose: it
    only has to reach the strings a user reads, and a false positive lands in
    _ALLOWED with its reason.
    """
    src = (_REPO / path).read_text(encoding="utf-8")
    return [t.strip() for t in re.findall(r">([^<>{}]{8,}?)<", src)]


def _all_surfaces():
    """The three Python prose sources, plus the two this commit had to fix
    that the Python walk cannot see.

    The option label at templates/runs/new.html read "Ligand binder (vs a
    small molecule)" and the .sdf hint read "Small-molecule target. Optional
    for curated ligand tasks." Neither lives in meta, the adapter or the hero
    lede, so the walk _prose_sources does would have stayed green with the
    promise fully intact on the campaign form -- which is how it survived
    there until 2026-09-24.
    """
    from blueprints.targets import _REFUSED_PRESETS

    sources = dict(_prose_sources())
    sources["templates/runs/new.html"] = _template_text("templates/runs/new.html")
    sources["targets / _REFUSED_PRESETS"] = [
        msg for (tool, _preset), msg in _REFUSED_PRESETS.items()
        if tool == "proteina"
    ]
    return sources


def test_every_allowlisted_string_is_still_a_live_surface():
    """No dead entries. An allowlist entry that no longer matches any surface
    is a pre-authorised hole: paste that sentence back anywhere and the check
    above waves it through."""
    live = {s for group in _all_surfaces().values() for s in group}
    orphaned = sorted(_ALLOWED - live)
    assert not orphaned, (
        f"these _ALLOWED entries match no current surface: {orphaned}. "
        "Delete them, or re-point them at the copy that replaced them."
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


def test_no_proteina_prose_source_promises_a_custom_small_molecule():
    found = {}
    for name, strings in _all_surfaces().items():
        assert strings, f"surface {name!r} came back empty — it asserts nothing"
        bad = _offenders(strings)
        if bad:
            found[name] = bad
    assert not found, (
        "proteina copy offers a small molecule the user supplies:\n"
        f"{found}\n\n"
        "_CUSTOM_TARGET_PRESETS is {'protein_binder'} — validate() refuses a "
        "ligand_binder run carrying a staged target, so the ligand variant "
        "designs against a bundled benchmark ligand. Say that, or add the "
        "string to _ALLOWED with the reason it refuses rather than promises."
    )


@pytest.mark.parametrize(
    "injected",
    [
        # The three sentences this commit retired.
        "The ligand-binder variant takes a small-molecule target as an SDF.",
        "Upload your own ligand and the search designs against it.",
        "Your target is a small molecule rather than a protein.",
        # Rephrasings the first version of this guard MISSED. Review produced
        # them by driving _offenders directly; each one is the same promise in
        # wording the fitted verb list did not hold.
        "Upload your own molecule and the search designs against it.",
        "The ligand variant accepts your SDF.",
        "Attach a .mol file and the ligand variant designs against it.",
        "Design against any small molecule you like.",
        "Give us your ligand and we design against it.",
        "Ligand binder (vs a small molecule)",
        "Small-molecule target. Optional for curated ligand tasks.",
        "the ligand variant needs a small-molecule SDF, and this target is a "
        "protein structure.",
    ],
)
def test_the_scan_bites(injected):
    """Control: the retired sentences, and rephrasings of them, are caught."""
    assert _offenders([injected]) == [injected]


def test_the_scan_passes_the_honest_versions():
    """Control the other way: the replacement copy must not trip the scan,
    or the check above would be green only because everything trips it."""
    assert _offenders([
        "Upload a protein target, name the chain and the residues you want "
        "gripped, and set how many designs to fund.",
        "the ligand and motif variants run benchmark tasks bundled with the "
        "model",
        # ``ligand`` on its own must stay invisible to the scan, or every
        # scoring sentence would have to be allowlisted and the allowlist
        # would stop meaning anything.
        "a benchmark ligand or motif task is scored by RoseTTAFold3",
        "RF3 score for ligand / motif, force-field energy where applicable",
        # Possession without a molecule noun is not this defect.
        "scores how well it grips your target",
    ]) == []
