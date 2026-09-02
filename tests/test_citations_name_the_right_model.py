"""A tool must not be cited as a related-but-different model.

Three citations in this repo named the wrong model, and all three shipped
green, because ``test_tool_categories.py`` only asserts ``paper_citation
!= "—"`` — it passes with any author at all:

  * ``tools/boltzgen/meta.py`` cited Boltz (Wohlwend et al., jwohlwend/boltz)
    on the BoltzGen page. BoltzGen is Stark et al.; Wohlwend is Boltz-1's
    first author and sits 36th on BoltzGen's own author list.
  * ``tools/boltz2/meta.py`` credited Boltz-2 to Wohlwend et al. — again
    Boltz-1's first author. Boltz-2 is Passaro et al.
  * ``shared/metric_glossary.py`` credited BindCraft to Bennett et al.
    BindCraft is Pacesa et al.; Bennett et al., Nat Commun 14, 2625 (2023)
    is a different paper, and it was cited for a threshold neither paper
    states.

First authors below were checked against primary sources on 2026-09-01
(the bioRxiv details API, PMC full text, Europe PMC, and each upstream
repo's own README/BibTeX) — not against another citation in this repo.

The second test is the one that matters. The first would drift with the
constants if someone edited both; the second tests the actual bug class,
a neighbouring model's signature name leaking into a tool that is not
that model, which is how all three defects above presented.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# tool slug -> a string its paper_citation must contain (surname, or the
# organisation where the work is not credited to a first author).
FIRST_AUTHOR: dict[str, str] = {
    "af2": "Jumper",
    "bindcraft": "Pacesa",
    "boltz2": "Passaro",
    "boltzgen": "Stark",
    "colabfold": "Mirdita",
    "esmfold": "Lin",
    "esmfold2_design": "Biohub",
    "iggm": "Wang",
    "mpnn": "Dauparas",
    "opendde": "Aureka",
    "proteina": "Geffner",
    "pxdesign": "Bennett",
    "rfantibody": "Bennett",
    "rfdiffusion": "Watson",
}

# tool slug -> names belonging to a DIFFERENT model that must not appear
# anywhere under tools/<slug>/. These are the near-neighbours that have
# actually been confused here, not a generic blocklist.
FOREIGN_SIGNATURES: dict[str, tuple[str, ...]] = {
    # BoltzGen is not Boltz and is not distilled from Boltz-1. It does call
    # Boltz-2 for its refold stages, which is how the mix-up happened, so
    # "Boltz-2" stays legal in prose about that step; the AUTHOR and the
    # Boltz REPO do not.
    "boltzgen": ("Wohlwend", "jwohlwend", "Boltz-1"),
    # Boltz-2 genuinely lives in jwohlwend/boltz, so only the author string
    # is barred: "Wohlwend et al." is the Boltz-1 citation.
    "boltz2": ("Wohlwend et al",),
}

_SCANNED_SUFFIXES = {".py", ".modal", ".md", ".txt", ""}


def _meta(slug: str):
    return importlib.import_module("tools.%s.meta" % slug)


def test_every_tool_meta_is_covered_here() -> None:
    """A new tool must be added to FIRST_AUTHOR, not silently skipped."""
    on_disk = {p.parent.name for p in (REPO / "tools").glob("*/meta.py")}
    assert on_disk, "found no tools/*/meta.py — this guard would vacuously pass"
    assert on_disk == set(FIRST_AUTHOR), (
        "tools/*/meta.py and FIRST_AUTHOR disagree: only in repo=%s, only in map=%s"
        % (sorted(on_disk - set(FIRST_AUTHOR)), sorted(set(FIRST_AUTHOR) - on_disk))
    )


@pytest.mark.parametrize("slug,expected", sorted(FIRST_AUTHOR.items()))
def test_paper_citation_names_this_tools_own_paper(slug: str, expected: str) -> None:
    cited = _meta(slug).paper_citation
    assert expected in cited, (
        "tools/%s/meta.py paper_citation is %r, which does not name %s. If the "
        "upstream paper genuinely changed, update FIRST_AUTHOR against a PRIMARY "
        "source (journal page, bioRxiv, or the upstream repo's own BibTeX) — "
        "never against another citation in this repo." % (slug, cited, expected)
    )


@pytest.mark.parametrize("slug,barred", sorted(FOREIGN_SIGNATURES.items()))
def test_no_neighbouring_models_name_leaks_into_a_tool(
    slug: str, barred: tuple[str, ...]
) -> None:
    files = [
        p
        for p in (REPO / "tools" / slug).rglob("*")
        if p.is_file() and p.suffix in _SCANNED_SUFFIXES
    ]
    assert files, "no files scanned for %s — this guard would vacuously pass" % slug

    hits: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name in barred:
                if name in line:
                    hits.append(
                        "%s:%d names %r: %s"
                        % (
                            path.relative_to(REPO).as_posix(),
                            lineno,
                            name,
                            line.strip(),
                        )
                    )
    assert not hits, "a different model's citation leaked into tools/%s/:\n  %s" % (
        slug,
        "\n  ".join(hits),
    )
