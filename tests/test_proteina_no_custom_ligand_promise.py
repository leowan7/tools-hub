"""Proteina copy may not promise a small-molecule target the user supplies.

``tools/proteina/__init__.py::_CUSTOM_TARGET_PRESETS`` is ``{"protein_binder"}``
and ``validate`` refuses any other preset that arrives with a staged target, so
``ligand_binder`` only ever designs against a benchmark ligand bundled with the
upstream repo. The ``seo_faq`` entry shipped saying the opposite — "the
ligand-binder variant takes a small-molecule target as an SDF and designs de
novo binders scored by the RoseTTAFold3 reward" — and that answer renders twice
on /tools/proteina, as visible FAQ copy and inside the page's FAQPage JSON-LD,
so it was rich-result eligible. Four other surfaces carried the same promise
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

import pytest

from tests.test_proteina_promises_no_clustering import _prose_sources


# A promise is a supply verb and a molecule noun in the SAME string. Either
# alone is fine: "bundled benchmark ligand" has the noun and no verb, "Upload a
# protein target" has the verb and no noun.
_SUPPLY = re.compile(
    r"\b(upload(?:ed|s|ing)?|supplied|supply|bring|takes?|your own"
    r"|you provide|your target is)\b",
    re.I,
)
_MOLECULE = re.compile(r"\bsmall[-\s]molecule\b|\bSDF\b|\bligand\b", re.I)

# Strings that pair the two while REFUSING, not promising. Each is here because
# saying "you cannot upload a molecule" needs both halves of the pattern.
_ALLOWED = {
    (
        "No. The only target you can supply is a protein structure "
        "(<code>.pdb</code>/<code>.cif</code>), on the protein-binder "
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
    (
        "<code>protein_binder</code> for a protein target (AF2 reward) "
        "and the only variant that can take a target of yours, "
        "<code>ligand_binder</code> for a bundled benchmark ligand task "
        "(RF3 reward), <code>motif_ame</code> for motif scaffolding / "
        "enzyme active sites, or <code>validate</code> for a free "
        "config check before spending GPU."
    ),
    (
        "Your own structure, or a curated benchmark task whose target "
        "is baked in &mdash; the two are mutually exclusive. Uploading "
        "your own is available on the protein-binder variant; the "
        "ligand and motif variants run curated tasks (their tasks "
        "resolve from separate upstream registries)."
    ),
    (
        "Ranked designs with reward scores (AF2 pLDDT / ipTM for protein, "
        "RF3 score for ligand / motif, force-field energy where applicable), "
        "a self-consistency re-fold RMSD, and downloadable structures. The "
        "ligand and motif variants score on RF3 only."
    ),
    "RoseTTAFold3 via RosettaCommons foundry (BSD) — ligand + motif reward.",
    # The pilot card's variant note. Names the upload and the ligand variant
    # in one breath precisely to say they are different variants.
    (
        "The only variant that designs against a structure you upload; the "
        "others run curated ligand and motif benchmarks. It also settles the "
        "<code>rf3_score</code> column below, which is empty on every row: "
        "RF3 is a second scoring stack those other variants need, a protein "
        "binder run does not, and it is not free. That is a consequence of "
        "this choice rather than a switch of its own &mdash; there is no RF3 "
        "control on the form."
    ),
}


def _offenders(strings):
    """The one predicate. The real check and the control both call it."""
    return [
        s for s in strings
        if _SUPPLY.search(s) and _MOLECULE.search(s) and s not in _ALLOWED
    ]


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
    for name, strings in _prose_sources().items():
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
        "The ligand-binder variant takes a small-molecule target as an SDF.",
        "Upload your own ligand and the search designs against it.",
        "Your target is a small molecule rather than a protein.",
    ],
)
def test_the_scan_bites(injected):
    """Control: the retired sentences, and the shape of them, are caught."""
    assert _offenders([injected]) == [injected]


def test_the_scan_passes_the_honest_versions():
    """Control the other way: the replacement copy must not trip the scan,
    or the check above would be green only because everything trips it."""
    assert _offenders([
        "Upload a protein target, name the chain and the residues you want "
        "gripped, and set how many designs to fund.",
        "the ligand and motif variants run benchmark tasks bundled with the "
        "model",
    ]) == []
