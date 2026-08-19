"""Every file a Dockerfile bakes into an image must be on a deploy trigger.

``.github/workflows/deploy-modal.yml`` redeploys all nine Modal apps on push to
main, but only for pushes that touch a path in its ``on.push.paths`` filter. A
``Dockerfile.modal`` that COPYs a file living outside that filter is a silent
staleness bug of the worst kind: the file changes, the image content changes,
and no deploy runs — nothing fails, nothing is red, and prod keeps serving the
old layer indefinitely.

That is not hypothetical. ``static/example/`` fixtures are COPYed as in-image
smoke targets by four Dockerfiles (af2, colabfold, esmfold, mpnn) and, until the
three fixture entries were added to the trigger, matched no pattern at all
— ``static/`` is not under ``tools/``, and it is a different path from the
``!tools/**/example/**`` negation, which only covers ``tools/<slug>/example/``.

So this file asserts the GENERAL property rather than that one instance: resolve
what each Dockerfile's COPY patterns actually pull out of the repo, and require
every resolved file to survive the trigger filter. A new COPY of some other
un-triggered path fails here on the commit that introduces it.

Three deliberate scope limits, all safe in the conservative direction:

* Only the Dockerfile context channel. The other way a local file reaches an
  image is ``Image.add_local_file`` in a ``modal_app.py``; today every one of
  those names its own ``tools/<slug>/run_pipeline.py``, which ``tools/**``
  already covers.
* Only ``COPY``. Modal's parser ignores ``ADD``, and so does Modal's real
  context mount — an ``ADD <local path>`` therefore has no context file and the
  docker build fails outright. Loud, not stale, so nothing to guard.
* Only git-TRACKED files are candidates. A push event can only ever report
  changes to tracked files, so an untracked file is not something the trigger
  could match anyway.

What "conformance" here does and does not mean
----------------------------------------------
``test_matches_githubs_documented_filter_pattern_examples`` replays every row of
GitHub's published pattern table. Read the name carefully: those rows publish
only MATCHES, never non-matches, so on their own they prove conformance in ONE
direction. They catch a translator that matches too LITTLE. They cannot catch
one that matches too much — QC round 3 demonstrated this by replacing
``_to_regex`` with ``re.compile(".*")``, which makes every pattern match every
path and still passes all twelve rows. Three rows (``*``, ``*.js``, ``docs/*``)
catch nothing short of total inertness.

Over-matching is the direction that matters most, because it is the SILENT one:
a filter believed wider than it is makes this module report deploy coverage that
does not exist, and the image goes stale with nothing red. Protection against it
lives in three places, none of them the rows above:

* ``test_rejects_what_githubs_documented_rules_exclude`` — documented
  non-matches derived from GitHub's whole-path and ``*``-stops-at-``/`` rules.
  This is what pins the regex anchors;
* ``test_pattern_translation_distinguishes_star_from_globstar`` — hand-written
  cases over the LIVE trigger, including the ``*``-must-not-cross-``/`` case;
* ``test_matches_githubs_documented_negation_examples`` — later-wins ordering,
  including GitHub's own documented does-NOT-match entries.

Do not read a green conformance run as bidirectional proof. Round 3's mutation
table (``docs/qc/deploy-trigger-guard-fixes-round3.md``) is the evidence for
every claim in this section.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
import yaml
from modal._utils.docker_utils import extract_copy_command_patterns
from modal.file_pattern_matcher import FilePatternMatcher

_REPO = pathlib.Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO / ".github" / "workflows" / "deploy-modal.yml"
_DOCKERFILES = sorted((_REPO / "tools").glob("*/Dockerfile.modal"))


def _push_filter() -> dict:
    """The workflow's ``on.push`` block.

    ``on`` is a YAML 1.1 boolean, so ``yaml.safe_load`` hands back the key
    ``True``, not the string. Read both rather than assume either.
    """
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))
    return on["push"]


# `.get`, not `[...]`: this runs at COLLECTION time, and the one scenario the
# paths-ignore floor below exists to catch — `paths-ignore` having REPLACED
# `paths`, which GitHub allows only one of — is exactly the scenario where
# `["paths"]` raises a bare `KeyError: 'paths'` here and kills the whole module
# before any test can report why. Defaulting to [] keeps the module importable
# so that floor gets to run and name the problem.
_TRIGGER_PATHS = list(_push_filter().get("paths", []))


# GitHub filter patterns are NOT globs. Its "Filter pattern cheat sheet"
# (`github/docs`, content/actions/reference/workflows-and-actions/
# workflow-syntax.md) enumerates exactly six specials: `*`, `**`, `?`, `+`,
# `[]`, `!`. Three of those are regex quantifiers rather than wildcards — `?`
# is "zero or one of the PRECEDING character", `+` is "one or more of the
# preceding character", `[]` is a single alphanumeric from the listed set or
# range. `_to_regex` models none of the three, so it refuses them rather than
# guessing. Refusing matters most in a NEGATION: rendering `+`/`[]` as literals
# under-models the exclusion, the guard then believes a path is covered when the
# real filter excludes it, and that is silent staleness — the exact failure this
# module exists to catch. In a positive pattern the same mistake is merely
# noisy, and `?` is wrong in both directions.
#
# `\` is refused for the opposite reason: not because it means something this
# translator cannot model, but because GitHub documents NO meaning for it at
# all. `grep -i 'escape\|backslash'` over the whole 72 KB of workflow-syntax.md
# returns nothing, and the cheat sheet's six specials do not include it. So an
# escaping convention cannot be assumed, and neither can the literal reading:
# `re.escape` would turn `foo\*bar` into `^foo\\[^/]*bar$`, which matches only a
# path containing a real backslash — nothing on a POSIX repo path — so the
# pattern would read as "matches nothing" and quietly subtract itself from the
# filter. Undocumented plus silently-empty is the case to refuse, not to model.
# (A previous revision of this file asserted `\` WAS a documented escape. It is
# not. That claim was invented; this comment is what the source actually says.)
_UNMODELLED_METACHARS = frozenset("?+[]\\")


def _to_regex(pattern: str) -> re.Pattern:
    """GitHub filter-pattern -> anchored regex, for the subset modelled here.

    * ``**/`` matches any number of leading segments INCLUDING zero, so
      ``**/README.md`` hits the repo-root file too. This is GitHub's DOCUMENTED
      behaviour, not a glob convention borrowed from elsewhere — citation below;
    * a trailing ``/**`` matches the directory and everything under it;
    * a bare ``**`` matches across ``/``;
    * ``*`` matches within one segment, never across ``/``.

    Source for the first bullet, because it is the one worth being able to
    re-check: repo ``github/docs``, path
    ``content/actions/reference/workflows-and-actions/workflow-syntax.md``,
    section "Patterns to match file paths". SIX rows of that table list an
    example that is only reachable if ``**`` consumes zero segments, two of
    them decisive on their own:

    * ``'**/README.md'`` lists ``README.md`` — repo root, no leading directory
      — alongside ``js/README.md``;
    * ``'**/docs/**'`` lists ``docs/hello.md``, again with nothing before
      ``docs``;
    * and likewise ``'**/*-post.md'`` -> ``my-post.md``,
      ``'**/migrate-*.sql'`` -> ``migrate-10909.sql``, ``docs/**/*.md`` ->
      ``docs/README.md`` (zero segments consumed mid-pattern), and
      ``'**/*src/**'`` -> ``my-src/code/js/app.js``, where the leading ``**/``
      must consume nothing for ``*src`` to reach ``my-src``.

    Read the TABLE, not the cheat sheet's one-line prose. That prose says only
    ``**`` "Matches zero or more of any character", which taken literally makes
    ``**/README.md`` require a leading ``/`` and so NOT match ``README.md`` —
    contradicting the table's own worked example. The examples are what the
    engine does; the prose is a simplification. An earlier QC round did not
    find this table and called the zero-segment reading unverified, and this
    docstring was then rewritten to describe it as a mere convention. Why the
    table was missed is not recorded here, because it is not known — checked
    directly, docs.github.com's rendered page DOES carry the full table,
    root-``README.md`` row included, so "the rendered page omits it" is not the
    explanation. The reading is neither unverified nor conventional: it is
    documented, and
    ``test_matches_githubs_documented_filter_pattern_examples`` below now pins
    every row of the table so this cannot be re-litigated from memory again.

    Everything else is a literal, except the metacharacters in
    ``_UNMODELLED_METACHARS`` above, which raise. Written out rather than handed
    to ``fnmatch`` because ``fnmatch`` renders both ``*`` and ``**`` as ``.*``
    and would call ``tools/*/meta.py`` a match for ``tools/a/b/meta.py``.
    """
    unmodelled = "".join(sorted(_UNMODELLED_METACHARS & set(pattern)))
    if unmodelled:
        raise NotImplementedError(
            f"deploy trigger pattern {pattern!r} uses GitHub filter metacharacter(s) "
            f"{unmodelled!r}, which this translator does not model. In a negation that "
            "would under-model the exclusion and make this guard report coverage that "
            "does not exist. Teach _to_regex the construct before using it."
        )
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _triggers_deploy(path: str, patterns: list[str] | None = None) -> bool:
    """Would a push touching ``path`` run the deploy workflow?

    Later-wins, which is GitHub's rule: a negation after a matching positive
    excludes, and a positive after a matching negation re-includes. Evaluating
    the whole list in order — rather than short-circuiting on the first hit — is
    the only way to get that right.

    ``patterns`` defaults to the live trigger. It is overridable only so the
    conformance test below can run GitHub's own two documented negation examples
    through this exact function rather than a copy of it — a copy could drift
    from the real resolver and still look green.
    """
    hit = False
    for pattern in _TRIGGER_PATHS if patterns is None else patterns:
        negated = pattern.startswith("!")
        if _to_regex(pattern[1:] if negated else pattern).match(path):
            hit = not negated
    return hit


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


_TRACKED = _tracked_files()


def _copied_files(dockerfile_lines) -> list[str]:
    """Which tracked repo files these Dockerfile commands bake into the image.

    Modal's own two functions — ``extract_copy_command_patterns`` for the COPY
    sources and ``FilePatternMatcher`` over the context dir (the repo root) — so
    this cannot drift from real build behaviour the way a hand-rolled regex
    would. ``COPY --from=`` is dropped by the parser: it reads from an earlier
    build stage, not from the repo, so nothing in the repo can make it stale.
    """
    patterns = extract_copy_command_patterns(list(dockerfile_lines))
    if not patterns:
        return []  # no COPY at all -> Modal builds no context mount whatsoever
    match = FilePatternMatcher(*patterns)
    return [p for p in _TRACKED if match(pathlib.PurePosixPath(p))]


# ---------------------------------------------------------------------------
# Non-vacuity floors. Each check below is only as good as one of these.
# ---------------------------------------------------------------------------


def test_the_trigger_is_a_paths_allowlist_with_no_paths_ignore():
    """``paths-ignore`` would be a second, unmodelled exclusion channel.

    GitHub forbids using both in one filter, so its presence means ``paths`` is
    gone and ``_triggers_deploy`` is answering about a filter that no longer
    exists — it would report everything as covered.

    Fully reachable only because ``_TRIGGER_PATHS`` defaults a missing ``paths``
    to ``[]``. Be precise about which state that rescued, because there are two
    and the plain ``["paths"]`` subscript handled them differently:

    * BOTH keys present — ``paths-ignore`` ADDED alongside ``paths``. The
      subscript finds ``paths`` and this assertion fires normally. Never broken.
    * ``paths-ignore`` REPLACING ``paths`` — the shape GitHub actually forces,
      since it permits only one. Here the subscript raised
      ``KeyError: 'paths'`` at import, pytest reported a collection error, and
      this assertion's explanation never reached anyone. That state, and only
      that state, was unreachable.
    """
    push = _push_filter()
    assert "paths-ignore" not in push, (
        "deploy-modal.yml grew a paths-ignore filter. _triggers_deploy models "
        "only the paths allowlist, so it now over-reports coverage. Teach it "
        "the second channel before trusting this module again."
    )
    assert _TRIGGER_PATHS, "on.push.paths is empty"


def test_no_live_trigger_pattern_uses_an_unmodelled_metacharacter():
    """The live half of the ``_to_regex`` refusal.

    Without this, a pattern using ``?``/``+``/``[]``/``\\`` would only surface
    wherever ``_triggers_deploy`` happens to be called, as a raw
    NotImplementedError in some unrelated parametrized case. Here it names the
    offending pattern.
    """
    for pattern in _TRIGGER_PATHS:
        _to_regex(pattern[1:] if pattern.startswith("!") else pattern)


def test_to_regex_refuses_rather_than_guesses():
    """The floor under the refusal itself — and a record of what each means.

    ``*.jsx?`` is GitHub's own documented example: it matches ``page.js`` AND
    ``page.jsx``, because ``?`` quantifies the preceding ``x``. Modelling ``?``
    as a single-character wildcard, the glob reading, gets both wrong.

    ``foo\\*bar`` is refused because GitHub documents NO meaning for ``\\``. Its
    cheat sheet lists exactly six specials — ``*``, ``**``, ``?``, ``+``,
    ``[]``, ``!`` — and ``grep -i 'escape\\|backslash'`` over the whole of
    workflow-syntax.md returns nothing. So no escaping convention may be
    assumed, and the literal reading is no better: ``re.escape`` would give
    ``^foo\\\\[^/]*bar$``, which matches only a path containing a real
    backslash — nothing on a POSIX repo path — so the pattern would read as
    "matches nothing" and quietly subtract itself from the filter. Undocumented
    AND silently empty is the case to refuse.

    (An earlier revision of this docstring called ``\\`` "GitHub's escape". That
    was invented, and it outlived the comment fix that corrected the same claim
    beside ``_UNMODELLED_METACHARS``. Docstrings survive a squash merge; keep
    the two in step.)
    """
    for pattern in ["*.jsx?", "tools/**/*+.py", "tools/**/[abc]*.py", "foo\\*bar"]:
        with pytest.raises(NotImplementedError):
            _to_regex(pattern)
    # the modelled subset still compiles
    for pattern in ["tools/**", "tools/**/meta.py", "static/example/1HEW.pdb", "**/README.md"]:
        assert _to_regex(pattern)


# Twelve of the fifteen rows of GitHub's "Patterns to match file paths" table,
# transcribed from `github/docs`, content/actions/reference/workflows-and-actions/
# workflow-syntax.md. Pattern -> the example matches that row lists.
#
# The other three are covered, but not from here, because they are not
# single-pattern match rows: the two later-wins rows go to
# test_matches_githubs_documented_negation_examples (they need a pattern LIST,
# and they carry does-NOT-match entries), and `'*.jsx?'` goes to
# test_documented_rows_using_refused_constructs_are_refused (its `?` is refused,
# so there is no regex to compare). Between the three tests the table is covered
# in full; this list alone is not the whole table.
#
# This is the anchor the rest of the module's pattern reasoning hangs off. It
# exists because a previous revision argued about `**/` semantics from memory
# rather than from this table, and talked itself out of a correct
# implementation. Facts about an external system belong in an executable check
# against that system's own published examples, not in a docstring where the
# next reader has to take them on trust — which is the whole point of the rows
# below, and of not writing a prose theory about why the table went unread.
_DOC_ROWS = [
    ("*", ["README.md", "server.rb"]),
    ("**", ["all/the/files.md"]),
    ("*.js", ["app.js", "index.js"]),
    # `**` NOT followed by `/` still crosses directory boundaries — three depths
    # in one row, which is the row most likely to catch a translator that models
    # `**` as if it were `*`.
    ("**.js", ["index.js", "js/index.js", "src/js/app.js"]),
    ("docs/*", ["docs/README.md", "docs/file.txt"]),
    ("docs/**", ["docs/README.md", "docs/mona/octocat.txt"]),
    # zero segments consumed MID-pattern, not just leading
    ("docs/**/*.md", ["docs/README.md", "docs/mona/hello-world.md", "docs/a/markdown/file.md"]),
    ("**/docs/**", ["docs/hello.md", "dir/docs/my-file.txt", "space/docs/plan/space.doc"]),
    # the decisive zero-leading-segment row: `README.md` at the repo root
    ("**/README.md", ["README.md", "js/README.md"]),
    ("**/*src/**", ["a/src/app.js", "my-src/code/js/app.js"]),
    ("**/*-post.md", ["my-post.md", "path/their-post.md"]),
    ("**/migrate-*.sql", ["migrate-10909.sql", "db/migrate-v1.0.sql", "db/sept/migrate-v1.sql"]),
]


# The other direction. `_DOC_ROWS` publishes only MATCHES, so on its own it can
# detect a translator that matches too little and not one that matches too much
# — a mutation replacing `_to_regex` with `re.compile(".*")` passes every row
# above. These non-matches are DERIVED, each from one of two sentences GitHub
# states outright, cited per entry. Nothing here is invented; where the docs are
# silent (see the `/**` note below) there is deliberately no entry.
#
#   RULE W ("whole path"): "Path patterns must match the whole path, and start
#   from the repository's root." — workflow-syntax.md, immediately above the
#   "Patterns to match file paths" table.
#   RULE S ("* stops at /"): "The `*` wildcard matches any character, but does
#   not match slash (`/`)." — the `'*'` row of that table.
#
# Rule W is what pins the TRAILING anchor, and a dropped `$` is the mutation
# with a silent-staleness direction: without it `static/example/1HEW.pdb.bak`
# would read as covered when GitHub does not cover it, and this module would
# report coverage that does not exist. That is the exact failure it exists to
# prevent, which is why the whole-path entries come first.
#
# NOT the leading `^`, though the whole-path rule is what justifies writing it.
# Nothing in this module can detect its removal: every call site uses
# `re.Pattern.match()`, which anchors at position 0 on its own, so `^` is
# redundant to `.match` and a mutation deleting it passes all 36 tests. Do not
# read the `src/docs/file.txt` entry below as covering that — it fails for a
# path this module never takes. What the `^` does earn is making a
# `.match` -> `.search` swap inert, which IS caught (QC round 4 mutations
# MK/MK2). Keep it for that reason, not for a guard that does not exist.
_DOC_NON_MATCHES = [
    # RULE W — a suffix match is not a whole-path match. Kills a dropped `$`.
    ("*.js", "app.js.map", "W: pattern must match the WHOLE path, not a prefix of it"),
    # RULE W again, through the `**/` branch rather than the `[^/]*` one.
    ("**/README.md", "README.md.bak", "W: whole path; `**/` does not license a suffix match"),
    # RULE W — "start from the repository's root". Correct and correctly cited,
    # but it does NOT catch a dropped `^`: see the note above, `.match()` is
    # already anchored at 0. It is here because the rule is documented, not
    # because it guards a mutation.
    ("docs/*", "src/docs/file.txt", "W: patterns start at the repo root, so `docs/` cannot float"),
    # RULE S — and the table says so by contrast: `'**.js'` lists `js/index.js`
    # as a match while `'*.js'` lists only `app.js` and `index.js`.
    ("*.js", "js/index.js", "S: `*` does not cross `/` (contrast the `'**.js'` row, which does)"),
    ("*", "docs/README.md", "S: `*` does not cross `/`"),
    # RULE S — contrast again: `docs/**` lists `docs/mona/octocat.txt`, `docs/*` does not.
    ("docs/*", "docs/mona/octocat.txt", "S: `*` does not cross `/` (contrast the `docs/**` row)"),
]


@pytest.mark.parametrize(
    "pattern,path,why", _DOC_NON_MATCHES, ids=[f"{p}!~{q}" for p, q, _ in _DOC_NON_MATCHES]
)
def test_rejects_what_githubs_documented_rules_exclude(pattern, path, why):
    """The over-matching direction, derived from GitHub's stated rules."""
    rx = _to_regex(pattern)
    assert not rx.match(path), (
        f"_to_regex({pattern!r}) -> {rx.pattern!r} matches {path!r}, which GitHub's "
        f"documented rules exclude — {why}. A translator that matches too much makes "
        "this module report deploy coverage that does not exist, which is silent "
        "staleness: the file changes, the image changes, and no deploy runs."
    )


# NOT asserted, on purpose: whether a trailing `/**` matches the directory ITSELF
# as well as its contents (`docs/**` vs the bare path `docs`). GitHub's table
# lists only `docs/README.md` and `docs/mona/octocat.txt` for `docs/**` — both
# have a child — and no documented sentence settles the bare-directory case.
# `_to_regex` renders it `(?:/.*)?`, i.e. the permissive reading, and that is a
# CHOICE, not a documented fact. Writing an assertion either way would be
# inventing a rule, which is how two earlier revisions of this file went wrong.
#
# It is also inert here, which is why leaving it unpinned costs nothing. Paths
# reach this module from two places, and neither supplies a bare directory:
#
#   * `git ls-files`, directly or via a Dockerfile COPY resolved against it.
#     Git tracks blobs, so this yields FILES only — measured, not assumed: the
#     tracked list contains zero bare-directory entries.
#   * roughly 39 example paths hardcoded in this file — the `_DOC_ROWS`
#     examples, `_DOC_NON_MATCHES`, and the negation test's cases. Transcribed
#     from GitHub's table or derived from its rules, and every one is a file
#     path. (An earlier wording claimed EVERY path came from `git ls-files`;
#     that stopped being true in the commit that added these rows.)
#
# So the two readings cannot diverge on any input reachable from here. Add a
# bare-directory case to either source and this is the first thing to re-derive.


@pytest.mark.parametrize("pattern,examples", _DOC_ROWS, ids=[r[0] for r in _DOC_ROWS])
def test_matches_githubs_documented_filter_pattern_examples(pattern, examples):
    """Conformance, row by row, against GitHub's published worked examples.

    One direction only — see ``_DOC_NON_MATCHES`` for the other.
    """
    rx = _to_regex(pattern)
    missed = [e for e in examples if not rx.match(e)]
    assert not missed, (
        f"_to_regex({pattern!r}) -> {rx.pattern!r} fails to match {missed}, which "
        "GitHub's own 'Patterns to match file paths' table lists as matches for "
        "that pattern (github/docs, content/actions/reference/"
        "workflows-and-actions/workflow-syntax.md). The table is the spec; this "
        "translator is wrong, not the table."
    )


def test_matches_githubs_documented_negation_examples():
    """The two later-wins rows — the semantics the whole guard rests on.

    Run through ``_triggers_deploy`` itself, so a drift between the real
    resolver and these examples cannot hide behind a local reimplementation.
    """
    # '*.md' + '!README.md': matches hello.md; does NOT match README.md or docs/hello.md
    two = ["*.md", "!README.md"]
    assert _triggers_deploy("hello.md", two)
    assert not _triggers_deploy("README.md", two), "a later negation must exclude"
    assert not _triggers_deploy("docs/hello.md", two), "`*` must not cross `/`"
    # + 'README*': a positive after a negation RE-INCLUDES
    three = ["*.md", "!README.md", "README*"]
    for path in ("hello.md", "README.md", "README.doc"):
        assert _triggers_deploy(path, three), (
            f"{path!r} should be re-included by the trailing positive `README*`. "
            "GitHub: 'Patterns are checked sequentially. A pattern that negates a "
            "previous pattern will re-include file paths.'"
        )


def test_documented_rows_using_refused_constructs_are_refused():
    """The one documented row this translator declines rather than models.

    ``'*.jsx?'`` is in the same table, and GitHub defines its `?` as "zero or
    one of the PRECEDING character" — which is why it matches BOTH ``page.js``
    and ``page.jsx``. That is knowable and documented; modelling it is simply
    not worth it for a filter that has never used one. Refusing stays correct,
    but it is refused as UNMODELLED, not as unknown.
    """
    with pytest.raises(NotImplementedError):
        _to_regex("*.jsx?")


def test_tracked_file_list_is_populated():
    assert len(_TRACKED) > 100, f"git ls-files returned {len(_TRACKED)} paths — not a real checkout"


def test_pattern_translation_distinguishes_star_from_globstar():
    """The floor under ``_to_regex``. A too-permissive translation calls
    everything covered; a too-strict one fails honest paths."""
    assert _triggers_deploy("tools/af2/modal_app.py")
    assert _triggers_deploy("tools/af2/Dockerfile.modal")
    assert _triggers_deploy(".github/workflows/deploy-modal.yml")
    # negations, at both depths `tools/**/x` has to reach
    assert not _triggers_deploy("tools/af2/meta.py")
    assert not _triggers_deploy("tools/af2/example/demo.pdb")
    assert not _triggers_deploy("tools/af2/example/nested/demo.pdb")
    # single `*` must not cross a separator
    assert not _to_regex("tools/*/meta.py").match("tools/a/b/meta.py")
    assert _to_regex("tools/*/meta.py").match("tools/a/meta.py")
    # `**/` matches zero leading segments — GitHub's documented behaviour, per
    # the "Patterns to match file paths" table. Kept here as a spot-check; the
    # full table is pinned by
    # test_matches_githubs_documented_filter_pattern_examples below.
    #
    # There used to be a second assertion here requiring every live `**/` entry
    # to be a NEGATION, on the theory that the zero-segment reading was
    # unverified and safe only in that position. Deleted, for two independent
    # reasons. Its rationale was simply false — the reading is documented and
    # this translator matches the table row for row — so it was enforcing a
    # caveat that does not exist. And it did not even do that: it selected on
    # the literal substring "**/", so `static/**`, `docs/**`, bare `**` and
    # `**.js` all sailed past it while its own message claimed a "structural"
    # bound. A guard whose stated reason is wrong AND whose reach is narrower
    # than it claims is the thing this module exists to catch, not to contain.
    assert _to_regex("**/README.md").match("README.md"), (
        "`**/` stopped matching zero leading segments. That is not a tunable "
        "preference — it is what GitHub documents. See github/docs, "
        "content/actions/reference/workflows-and-actions/workflow-syntax.md, "
        "section 'Patterns to match file paths': the `'**/README.md'` row lists "
        "`README.md` at the repo root as a match, and the `'**/docs/**'` row "
        "lists `docs/hello.md`. Read that table rather than the cheat sheet's "
        "one-line prose, which is a simplification that contradicts it."
    )
    assert _to_regex("**/README.md").match("docs/a/README.md")
    # plainly-untriggered paths stay untriggered
    assert not _triggers_deploy("README.md")
    assert not _triggers_deploy("app.py")


def test_copy_scan_resolves_real_files_and_rejects_non_copies():
    """The floor under ``_copied_files``. Without it, a change in Modal's parser
    or matcher would leave every Dockerfile check green over an empty set."""
    fixture = "static/example/1HEW.pdb"
    assert fixture in _TRACKED, f"{fixture} is not tracked — pick another fixture for this floor"
    assert _copied_files(["FROM base", f"COPY {fixture} /opt/x.pdb"]) == [fixture]
    assert _copied_files(["FROM base", "COPY static/example/*.pdb /opt/"]), "glob COPY not detected"
    assert _copied_files(["FROM base", "COPY . /app"]), "whole-context COPY not detected"
    assert not _copied_files(["FROM base", "RUN echo no-copy-here"])
    assert not _copied_files(["FROM a AS b", "COPY --from=b /built /opt"])


def test_dockerfiles_are_discovered():
    # esmfold2_design builds via modal.Image.micromamba and has no Dockerfile,
    # so eight is the full set, not a short read.
    assert len(_DOCKERFILES) >= 8, (
        f"expected at least 8 tools/*/Dockerfile.modal, found {[str(p) for p in _DOCKERFILES]}"
    )


def test_some_dockerfile_actually_copies_something():
    """Four of the eight carry no COPY at all. If that ever became all eight,
    the parametrized check below would assert nothing, silently."""
    copying = [
        p.parent.name
        for p in _DOCKERFILES
        if _copied_files(p.read_text(encoding="utf-8").splitlines())
    ]
    assert copying, "no Dockerfile.modal COPYs anything — this whole module is now vacuous"


# ---------------------------------------------------------------------------
# The property.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _DOCKERFILES, ids=lambda p: p.parent.name)
def test_every_copied_file_is_on_a_deploy_trigger(path):
    uncovered = sorted(
        p
        for p in _copied_files(path.read_text(encoding="utf-8").splitlines())
        if not _triggers_deploy(p)
    )
    assert not uncovered, (
        f"{path.parent.name}/Dockerfile.modal bakes {uncovered} into its image, "
        "but no on.push.paths entry in .github/workflows/deploy-modal.yml "
        "matches those paths. Editing one of them would change this image "
        "without redeploying it, and nothing would say so. Either add the path "
        "to the trigger, or stop COPYing it."
    )
