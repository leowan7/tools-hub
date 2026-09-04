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
  * ``tools/esmfold2_design/meta.py`` was credited to an organisation,
    "Chan Zuckerberg Biohub, 2026", where the paper has a first author:
    Candido et al., bioRxiv 2026. Worse, ``esmfold2_design_form.html``
    credited the five preset targets to "the EvolutionaryScale 2025
    paper" -- a different organisation and a different year -- twenty-two
    lines above the <option> list it describes. And this file PINNED the
    organisation form, so the correct citation was red: the SECOND time
    the guard did the one thing a guard must never do.

Some of those were in a URL and not in the citation string at all, so the
URLs are asserted here too. Four of the eight files the first sweep corrected
live outside any ``tools/<slug>/`` directory, so the leak scan is repo-wide.

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

# tool slug -> a surname its paper_citation must contain.
#
# The organisation may stand in ONLY where a primary source shows the work
# genuinely has no first author. That clause is how "Biohub" sat here for
# esmfold2_design while Crossref recorded Candido, S. with sequence="first"
# -- so before using it, look the authors up, do not assume from the
# citation string already in the repo.
FIRST_AUTHOR: dict[str, str] = {
    "af2": "Jumper",
    "bindcraft": "Pacesa",
    "boltz2": "Passaro",
    "boltzgen": "Stark",
    "colabfold": "Mirdita",
    "esmfold": "Lin",
    # NOT "Biohub". Crossref gives Candido, S. as sequence="first" on
    # 10.64898/2026.06.03.729735; Biohub is the affiliation, not a
    # substitute for the author.
    "esmfold2_design": "Candido",
    "iggm": "Wang",
    "mpnn": "Dauparas",
    # Checked, because this is the one live use of the organisation clause
    # above and it has the exact shape that was wrong for Biohub. It is NOT:
    # arXiv 2607.03787 carries a single citation_author, "project, Aureka AI
    # OpenDDE" -- the work has no person as first author.
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
    # The DOI, not the publisher's "biohub.ai/papers/esm_protein.pdf"
    # path: that 301s to this DOI today, but the token "biohub.ai"
    # identifies no particular paper, so it would pass a link to any
    # other one they publish. The repo moved orgs, evolutionaryscale ->
    # Biohub. A transfer, not a rename: the two orgs have distinct GitHub ids
    # (131310367 from 2023, 262686015 from 2026) and a rename keeps the id.
    "esmfold2_design": ("2026.06.03.729735", "Biohub/esm"),
    "iggm": ("2024.09.19.613838", "TencentAI4S/IgGM"),
    "mpnn": ("science.add2187", "dauparas/ProteinMPNN"),
    "opendde": ("2607.03787", "aurekaresearch/OpenDDE"),
    # Both must say complexa. The base model lives at .../genair/proteina/ and
    # in a different repository.
    #
    # These two tokens are WEAKER than the rest of this map -- neither is a
    # DOI nor an owner/repo pair, so a GitHub link pasted into paper_url would
    # pass, as would any fork of the repo name.
    #
    # KNOWN TRAP, left in place rather than half-fixed. The upstream repo
    # moved: NVIDIA-Digital-Bio/proteina-complexa 301s to
    # NVIDIA-BioNeMo/Proteina-Complexa -- new owner AND new casing. The
    # assertion below is a case-sensitive substring test, so this lowercase
    # token already goes RED the day someone corrects meta.py to the canonical
    # URL, and pinning the owner would do the same. It is the only github_url
    # in this repo that redirects. Fix meta.py and this token in one change;
    # touching either alone turns a correct value into a failure.
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
# a different model.
#
# Repo-wide, not scoped to tools/<slug>/, because this pattern has appeared on
# both sides of that boundary: shared/score_legends.py had a section header
# pairing "BoltzGen" with "Boltz-1", and tools/boltzgen/meta.py:76 had
# "BoltzGen (Wohlwend et al., MIT 2024)" in its user-facing About copy. Both
# were corrected in 879b5ea. A tool-scoped scan sees only the second.
#
# The second one also shows this matcher's BLIND SPOT, which is worth more
# than the history: 3ec66b9, a pure copy pass, reflowed that sentence so the
# two names landed on separate lines. Still misattributed, now invisible to a
# line-scoped check -- and nothing about that commit was aiming at this. A
# file-scoped check like FOREIGN_SIGNATURES keeps seeing it; this one does not.
#
# This matcher has therefore never caught anything: CONTEXT_BARS first appears
# in 819bf13, and 879b5ea and 3ec66b9 are both ancestors of it. It is a
# regression guard, not a record of finds.
#
# No count and no per-guard attribution is given here, on purpose: every
# version of that history so far has been falsified by replaying the matcher
# over the blobs. Do that rather than trust a sentence about it, this one
# included.
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
    # The previous owner of the upstream repo: github.com/evolutionaryscale
    # /esm 301s to github.com/Biohub/esm, so a stale link still resolves and
    # nothing breaks to signal it. The misattributing form of the same name,
    # "the EvolutionaryScale 2025 paper", named the wrong organisation AND
    # the wrong year on the form's own target picker. If a mention ever
    # becomes deliberate, drop that one string from this tuple rather than
    # deleting the entry.
    "esmfold2_design": ("EvolutionaryScale", "evolutionaryscale"),
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
    """The repo's text files, minus this test's own directory and the
    skip lists.

    ``tests/`` is excluded because this file necessarily contains the barred
    strings it is looking for. ``_SKIP_SUFFIXES`` and ``_SKIP_DIRS`` take out
    binaries, vendored trees and caches -- and also ``.svg``, ``.pdb`` and
    ``.cif``, which are plain text. So this is NOT every text file: a leak
    parked under ``vendor/``, ``runs/`` or ``content-migration/``, or written
    into an SVG label, is not scanned.
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


def test_the_leak_maps_are_not_empty() -> None:
    """Emptying a MAP switches its guard off silently.

    The per-entry floors below catch an emptied TUPLE, which fails loudly.
    Nothing caught an emptied MAP: it yields an empty ``parametrize``, and
    pytest's default ``empty_parameter_set_mark`` is SKIP. This repo ships no
    pytest ini (no pytest.ini / setup.cfg / tox.ini / pyproject.toml, and
    tests/conftest.py sets no ini options), so the default applies and the
    guard would report SKIPPED rather than failing.
    """
    assert CONTEXT_BARS, (
        "CONTEXT_BARS is empty, so test_no_line_naming_one_model_carries_"
        "another_models_signature now SKIPs instead of running"
    )
    assert FOREIGN_SIGNATURES, (
        "FOREIGN_SIGNATURES is empty, so test_no_neighbouring_models_name_"
        "leaks_into_a_tool now SKIPs instead of running"
    )
    assert NEVER_NAMED or BARRED_PHRASES, (
        "NEVER_NAMED and BARRED_PHRASES are both empty, so the repo-wide scan "
        "test_a_model_this_platform_does_not_run_is_not_named_anywhere now "
        "SKIPs instead of running"
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
    eight files the first sweep (879b5ea) corrected, four sit outside any
    ``tools/<slug>/`` directory -- a score legend, a runtime table, a product
    doc and the glossary. A scan scoped to tool directories cannot see any of
    them.

    This docstring used to add a claim about what stayed green when those were
    reverted. It was true of the suite as it stood at that sweep and false of
    the suite now, so it is gone rather than re-derived.
    """
    # Same floor as its sibling below: emptying a tuple rather than deleting
    # the entry leaves ``scanned`` non-zero and ``hits`` empty, so the test
    # reports PASSED having looked for nothing.
    assert barred, (
        "CONTEXT_BARS[%r] is empty, so this guard scans every file and "
        "matches nothing. Remove the whole entry if it is obsolete -- and see "
        "test_the_leak_maps_are_not_empty before removing the last one."
        % subject
    )

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
    # An emptied tuple leaves this test reporting PASSED over nothing: the walk
    # below still runs and still finds files, it just matches no string. The
    # floors already here (assert files / assert templates) prove the SCAN
    # happened, which is a different vacuity from having nothing to scan FOR.
    # Reaching it takes emptying an entry rather than deleting it -- one step
    # past the allowlisting edit the esmfold2_design comment above describes.
    assert barred, (
        "FOREIGN_SIGNATURES[%r] is empty, so this guard scans every file "
        "and matches nothing. Remove the whole entry if it is obsolete -- and "
        "see test_the_leak_maps_are_not_empty before removing the last one."
        % slug
    )

    files = [
        p
        for p in (REPO / "tools" / slug).rglob("*")
        if p.is_file() and p.suffix.lower() not in _SKIP_SUFFIXES
    ]
    assert files, "no files scanned for %s -- this guard would vacuously pass" % slug

    # The tool's own templates too. Scoping this to tools/<slug>/ is why "the
    # EvolutionaryScale 2025 paper" survived: it was an <option> description on
    # the form itself, and no check here could see the file it lived in.
    #
    # LIMIT, stated because a guard that hides its blind spot is worse than
    # none: this adds the tool's OWN two templates, not templates/ generally.
    # The same string moved to another tool's form, to a shared component, or
    # to templates/help/ still slips. It cannot simply go repo-wide, because
    # these tuples are scoped BY CONSTRUCTION -- boltzgen bars "jwohlwend",
    # and tools/boltz2/meta.py links jwohlwend/boltz legitimately, so a
    # repo-wide sweep of these tuples fails on correct code. Widening this
    # means per-tool name sets, not a wider walk.
    templates = sorted((REPO / "templates" / "tools").glob("%s_*.html" % slug))
    assert templates, (
        "no templates/tools/%s_*.html matched. Every tool here has a form and "
        "a results template, so an empty glob means the naming convention "
        "moved and this half of the scan covers nothing. It floors at zero "
        "only -- losing one of a tool's two templates still passes."
        % slug
    )
    files += templates

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
