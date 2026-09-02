"""A tool must not be cited as a related-but-different model.

The defect keeps recurring because the neighbours are genuinely close: Boltz
/ Boltz-2 / BoltzGen, Proteina / Proteina-Complexa, PXDesign / RFantibody
(both Bennett et al.). Found so far:

  * ``tools/boltzgen/meta.py`` cited Boltz (Wohlwend et al., jwohlwend/boltz).
    BoltzGen is Stark et al.
  * ``tools/boltz2/meta.py`` credited Boltz-2 to Wohlwend et al. -- again
    Boltz-1's first author. Boltz-2 is Passaro et al.
  * ``shared/metric_glossary.py`` credited BindCraft to Bennett et al.
    BindCraft is Pacesa et al.; Bennett et al., Nat Commun 14, 2625 (2023)
    is a different paper.
  * ``tools/proteina/meta.py`` cited Geffner et al. (Proteina, ICLR 2025) on
    the Proteina-COMPLEXA tool, whose paper is Didi et al., ICLR 2026 --
    the base model, not the tool. The first version of this file PINNED that
    error, which is the worst thing a guard can do: it made the fix red.
  * ``tools/iggm/meta.py`` linked arXiv 2504.09248, a control-theory paper
    on homomorphic encryption. IgGM has no arXiv version.

Two of those five were in a URL, not in the citation string, so the URLs are
asserted here too. Five of the eight files the first sweep corrected live
outside any ``tools/<slug>/`` directory, so the leak scan is repo-wide.

Every value below was checked against a PRIMARY source -- publisher Crossref
metadata, the bioRxiv details API, arXiv's API, or the upstream repository's
own README/BibTeX -- and never against another citation in this repo. When
one genuinely changes, re-check it the same way; do not reconcile it against
a neighbouring string here.
"""
from __future__ import annotations

import importlib
import pathlib
import re

import pytest

from shared.metric_glossary import GLOSSARY

REPO = pathlib.Path(__file__).resolve().parents[1]

# tool slug -> a surname its paper_citation must contain (or the organisation,
# where the work is not credited to a first author).
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
    # NOT Geffner. The tool is Proteina-Complexa (didi2026scaling, ICLR 2026);
    # geffner2025proteina is the base backbone generator, which the upstream
    # README lists separately as prior work.
    "proteina": "Didi",
    "pxdesign": "Bennett",
    "rfantibody": "Bennett",
    "rfdiffusion": "Watson",
}

# A surname is not enough on its own where two tools share one. These are the
# strings that tell the pair apart.
BARRED_IN_CITATION: dict[str, tuple[str, ...]] = {
    # Both are Bennett et al. Swapping one citation for the other passes a
    # surname check, which is exactly the bug class this file exists for.
    "pxdesign": ("RFantibody",),
    "rfantibody": ("Improving de novo protein binder design",),
}

# slug -> (token required in paper_url, token required in github_url or None
# where the tool declares no repository). A DOI or an owner/repo pair, because
# those are what actually identify a work -- a citation string can name the
# right author beside a link to something else entirely, which is how the
# IgGM defect survived.
REQUIRED_URL_TOKENS: dict[str, tuple[str, str | None]] = {
    "af2": ("s41586-021-03819-2", "sokrypton/ColabFold"),
    "bindcraft": ("2024.09.30.615802", "martinpacesa/BindCraft"),
    # v1 SPECIFICALLY. The field pointed at ...659707v2, which bioRxiv does
    # not have -- it redirects to biorxiv.org/node/ and the citation rendered
    # as a dead link. If a real v2 is ever posted, check it resolves and then
    # change this token deliberately.
    "boltz2": ("2025.06.14.659707v1", "jwohlwend/boltz"),
    "boltzgen": ("2025.11.20.689494", "HannesStark/boltzgen"),
    "colabfold": ("s41592-022-01488-1", "sokrypton/ColabFold"),
    "esmfold": ("science.ade2574", "facebookresearch/esm"),
    "esmfold2_design": ("biohub.ai", "evolutionaryscale/esm"),
    "iggm": ("2024.09.19.613838", "TencentAI4S/IgGM"),
    "mpnn": ("science.add2187", "dauparas/ProteinMPNN"),
    "opendde": ("2607.03787", "aurekaresearch/OpenDDE"),
    # Both must say complexa. The base model lives at .../genair/proteina/ and
    # in a different repository.
    "proteina": ("proteina-complexa", "proteina-complexa"),
    "pxdesign": ("s41467-023-38328-5", None),
    "rfantibody": ("2024.03.14.585103", "RosettaCommons/RFantibody"),
    "rfdiffusion": ("s41586-023-06415-8", "RosettaCommons/RFdiffusion"),
}

# Model label as it appears in a GLOSSARY citation's parenthetical -> the first
# author that label requires. The original glossary defect was exactly this
# disagreement: "Bennett et al., Nat Commun 2023 (BindCraft)".
MODEL_LABEL_AUTHOR: dict[str, str] = {
    "AlphaFold-Multimer": "Evans",
    "AlphaFold2": "Jumper",
    "AF2": "Jumper",
    "BindCraft": "Pacesa",
    "Boltz-2": "Passaro",
    "BoltzGen": "Stark",
    "ColabFold": "Mirdita",
    "ProteinMPNN": "Dauparas",
    "RFantibody": "Bennett",
    "RFdiffusion": "Watson",
}

# A line naming the KEY must not also carry one of its VALUES: those belong to
# a different model. Applied repo-wide, because the leak has appeared in a
# score legend, a runtime table, a product doc and a module docstring -- none
# of them under tools/<slug>/.
CONTEXT_BARS: dict[str, tuple[str, ...]] = {
    "BoltzGen": ("Boltz-1", "Wohlwend", "jwohlwend"),
}

# Names of models this platform does not serve. Every appearance of Boltz-1
# here has been BoltzGen or Boltz-2 miscalled, and a line-scoped check misses
# the ones that do not also say "BoltzGen" -- the runtime-anchor comment in
# shared/pdb_preflight_rules.py named it with no nearby tool name at all. A
# deliberate future mention has to be allowlisted by editing this tuple, which
# is the intended cost.
NEVER_NAMED: tuple[str, ...] = ("Boltz-1",)

# Boltz-2 predicts structure and affinity. It does not GENERATE, and every
# leak so far has credited it with generation. These are the exact phrasings
# that have appeared -- in a module docstring, a tool page's About copy, a
# product doc, and a user-facing <option> in the Scout handoff picker.
#
# LIMIT, stated because a guard that hides its blind spot is worse than none:
# this is a denylist of observed strings, not an understanding. "Boltz-2 makes
# the backbones" is the same error and would pass. Naming Boltz-2 beside
# "refold" stays legal and correct, which is why this cannot simply bar the
# name near BoltzGen.
BARRED_PHRASES: tuple[str, ...] = (
    "Boltz-2 design protocol",
    "Boltz-2 backbone",
    "Boltz-2 model to generate",
)

# tool slug -> names belonging to a DIFFERENT model that must not appear
# anywhere under tools/<slug>/.
FOREIGN_SIGNATURES: dict[str, tuple[str, ...]] = {
    # BoltzGen is not Boltz and is not distilled from Boltz-1. It does invoke
    # Boltz-2 at the fold/refold step, which is how the mix-up happened, so
    # "Boltz-2" stays legal in prose about that step; the AUTHOR and the Boltz
    # REPO do not.
    "boltzgen": ("Wohlwend", "jwohlwend", "Boltz-1"),
    # Boltz-2 genuinely lives in jwohlwend/boltz, so only the author string is
    # barred: "Wohlwend et al" is the Boltz-1 citation.
    "boltz2": ("Wohlwend et al",),
}

# A DENYLIST, not an allowlist. The first version listed the suffixes to scan
# and silently skipped .json -- the second most common type under tools/ and
# the format of every worked-example fixture rendered to users.
_SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".ico", ".svg", ".pdf", ".zip", ".gz", ".woff", ".woff2", ".ttf", ".eot",
    ".pdb", ".cif", ".npz", ".pt", ".bin",
}
_SKIP_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "node_modules", "vendor",
    "graphify-out", "runs", "tmp", ".claude", "content-migration",
    # Tool caches, and .pytest_cache in particular: it stores this file's own
    # parametrised node ids, so the barred strings appear there verbatim and
    # the scan reports itself.
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "htmlcov", "dist", "build",
}


def _meta(slug: str):
    return importlib.import_module("tools.%s.meta" % slug)


def _text_files():
    """Every text file in the repo except this test's own directory.

    ``tests/`` is excluded because this file necessarily contains the barred
    strings it is looking for.
    """
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        rel = path.relative_to(REPO)
        if rel.parts and rel.parts[0] == "tests":
            continue
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        yield path, rel


def test_every_tool_meta_is_covered_here() -> None:
    """A new tool must be added to the maps, not silently skipped."""
    on_disk = {p.parent.name for p in (REPO / "tools").glob("*/meta.py")}
    assert on_disk, "found no tools/*/meta.py -- this guard would vacuously pass"
    for name, mapping in (
        ("FIRST_AUTHOR", FIRST_AUTHOR),
        ("REQUIRED_URL_TOKENS", REQUIRED_URL_TOKENS),
    ):
        assert on_disk == set(mapping), (
            "tools/*/meta.py and %s disagree: only in repo=%s, only in map=%s"
            % (name, sorted(on_disk - set(mapping)),
               sorted(set(mapping) - on_disk))
        )


@pytest.mark.parametrize("slug,expected", sorted(FIRST_AUTHOR.items()))
def test_paper_citation_names_this_tools_own_paper(slug: str, expected: str) -> None:
    cited = _meta(slug).paper_citation
    # WORD BOUNDARIES. A bare substring test passed "Passarotti et al." for
    # boltz2, and the short keys ("Lin", "Wang") were weaker still.
    assert re.search(r"\b%s\b" % re.escape(expected), cited), (
        "tools/%s/meta.py paper_citation is %r, which does not name %s. If the "
        "upstream paper genuinely changed, update FIRST_AUTHOR against a "
        "PRIMARY source (journal page, bioRxiv, arXiv, or the upstream repo's "
        "own BibTeX) -- never against another citation in this repo."
        % (slug, cited, expected)
    )
    for barred in BARRED_IN_CITATION.get(slug, ()):
        assert barred not in cited, (
            "tools/%s/meta.py paper_citation names %r, which belongs to the "
            "other tool that shares this first author: %r"
            % (slug, barred, cited)
        )


@pytest.mark.parametrize("slug,tokens", sorted(REQUIRED_URL_TOKENS.items()))
def test_the_links_point_at_this_tools_own_work(
    slug: str, tokens: tuple[str, str | None]
) -> None:
    """The citation string and the links are separate attribution surfaces and
    they have failed separately. Both are rendered as live links on the tool
    page and emitted into its JSON-LD as schema:citation and codeRepository
    (blueprints/tools.py), so a wrong URL is machine-readable misattribution.
    """
    meta = _meta(slug)
    paper_token, repo_token = tokens

    assert meta.paper_url.startswith("https://"), (
        "tools/%s/meta.py paper_url is not an https URL: %r"
        % (slug, meta.paper_url)
    )
    assert paper_token in meta.paper_url, (
        "tools/%s/meta.py paper_url is %r, which does not contain %r. That "
        "token is the DOI or identifier of this tool's OWN paper; a link to a "
        "neighbouring model -- or, as happened here, to an unrelated field -- "
        "is invisible to the citation-string check above."
        % (slug, meta.paper_url, paper_token)
    )

    if repo_token is None:
        assert not meta.github_url, (
            "tools/%s/meta.py now declares a github_url; add its owner/repo to "
            "REQUIRED_URL_TOKENS so it is pinned like every other tool's"
            % slug
        )
        return
    assert repo_token in meta.github_url, (
        "tools/%s/meta.py github_url is %r, which does not contain %r"
        % (slug, meta.github_url, repo_token)
    )


def test_a_glossary_citation_names_the_author_of_the_model_it_labels() -> None:
    """The third defect, and the one nothing guarded until now: reverting
    ``shared/metric_glossary.py`` to its original wrong citation left the whole
    suite green. The entries carry a parenthetical model label beside an author,
    and "Bennett et al., Nat Commun 2023 (BindCraft)" is the two disagreeing.
    """
    assert GLOSSARY, "the glossary is empty -- this guard would vacuously pass"
    checked = 0
    for key, entry in GLOSSARY.items():
        cited = entry.get("citation") or ""
        for label, author in MODEL_LABEL_AUTHOR.items():
            if "(%s)" % label not in cited:
                continue
            checked += 1
            assert re.search(r"\b%s\b" % re.escape(author), cited), (
                "GLOSSARY[%r] is cited as %r. It labels the work %s, whose "
                "first author is %s -- the label and the author name two "
                "different papers."
                % (key, cited, label, author)
            )
    assert checked, (
        "no glossary citation carries a recognised model label, so this guard "
        "checked nothing. Add the label to MODEL_LABEL_AUTHOR, or this test is "
        "passing over the entries it exists to read."
    )


@pytest.mark.parametrize("subject,barred", sorted(CONTEXT_BARS.items()))
def test_no_line_naming_one_model_carries_another_models_signature(
    subject: str, barred: tuple[str, ...]
) -> None:
    """Repo-wide, because the leak is not confined to tool directories: of the
    eight files the first sweep corrected, five were a score legend, a runtime
    table, a product doc, a module docstring and the glossary. Scoping this to
    ``tools/<slug>/`` let every one of those be reverted with the suite green.
    """
    hits: list[str] = []
    scanned = 0
    for path, rel in _text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if subject not in line:
                continue
            for name in barred:
                if name in line:
                    hits.append(
                        "%s:%d names %s and %r together: %s"
                        % (rel.as_posix(), lineno, subject, name, line.strip())
                    )
    assert scanned, "no files scanned -- this guard would vacuously pass"
    assert not hits, (
        "a different model's signature sits on a line about %s:\n  %s"
        % (subject, "\n  ".join(hits))
    )


@pytest.mark.parametrize(
    "needle", [*NEVER_NAMED, *BARRED_PHRASES]
)
def test_a_model_this_platform_does_not_run_is_not_named_anywhere(
    needle: str,
) -> None:
    """The companion to the line-scoped check above, for the leaks that carry
    no nearby tool name to key on. ``shared/pdb_preflight_rules.py`` named
    Boltz-1 in a runtime table with the word BoltzGen nowhere on the line, and
    ``templates/scout/feasibility.html`` shipped "BoltzGen: Boltz-2 backbone"
    as an <option> label in the Scout handoff picker -- a user-facing string
    that three independent reviews of the first sweep all read past.
    """
    hits: list[str] = []
    scanned = 0
    for path, rel in _text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if needle in line:
                hits.append("%s:%d %s" % (rel.as_posix(), lineno, line.strip()))
    assert scanned, "no files scanned -- this guard would vacuously pass"
    assert not hits, (
        "%r names a model this platform does not run, or credits Boltz-2 with "
        "generation. If the mention is deliberate, allowlist it in "
        "NEVER_NAMED / BARRED_PHRASES rather than deleting this test:\n  %s"
        % (needle, "\n  ".join(hits))
    )


@pytest.mark.parametrize("slug,barred", sorted(FOREIGN_SIGNATURES.items()))
def test_no_neighbouring_models_name_leaks_into_a_tool(
    slug: str, barred: tuple[str, ...]
) -> None:
    files = [
        p
        for p in (REPO / "tools" / slug).rglob("*")
        if p.is_file() and p.suffix.lower() not in _SKIP_SUFFIXES
    ]
    assert files, "no files scanned for %s -- this guard would vacuously pass" % slug

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
