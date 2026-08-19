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

Two deliberate scope limits, both safe in the conservative direction:

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


_TRIGGER_PATHS = list(_push_filter()["paths"])


# GitHub filter patterns are NOT globs. Per its documented syntax, `?` means
# "zero or one of the PRECEDING character" and `+` means "one or more of the
# preceding character" — regex quantifiers, not wildcards — and `[]` is an
# alphanumeric class. `_to_regex` models none of the three, so it refuses them
# rather than guessing. Refusing matters most in a NEGATION: rendering `+`/`[]`
# as literals under-models the exclusion, the guard then believes a path is
# covered when the real filter excludes it, and that is silent staleness — the
# exact failure this module exists to catch. In a positive pattern the same
# mistake is merely noisy, and `?` is wrong in both directions.
_UNMODELLED_METACHARS = frozenset("?+[]")


def _to_regex(pattern: str) -> re.Pattern:
    """GitHub filter-pattern -> anchored regex, for the subset modelled here.

    * ``**/`` matches any number of leading segments INCLUDING zero, so
      ``**/README.md`` hits the repo-root file too;
    * a trailing ``/**`` matches the directory and everything under it;
    * a bare ``**`` matches across ``/``;
    * ``*`` matches within one segment, never across ``/``.

    Everything else is a literal, except the three metacharacters above, which
    raise. Written out rather than handed to ``fnmatch`` because ``fnmatch``
    renders both ``*`` and ``**`` as ``.*`` and would call ``tools/*/meta.py`` a
    match for ``tools/a/b/meta.py``.
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


def _triggers_deploy(path: str) -> bool:
    """Would a push touching ``path`` run the deploy workflow?

    Later-wins, which is GitHub's rule: a negation after a matching positive
    excludes, and a positive after a matching negation re-includes. Evaluating
    the whole list in order — rather than short-circuiting on the first hit — is
    the only way to get that right.
    """
    hit = False
    for pattern in _TRIGGER_PATHS:
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

    Without this, a pattern using ``?``/``+``/``[]`` would only surface wherever
    ``_triggers_deploy`` happens to be called, as a raw NotImplementedError in
    some unrelated parametrized case. Here it names the offending pattern.
    """
    for pattern in _TRIGGER_PATHS:
        _to_regex(pattern[1:] if pattern.startswith("!") else pattern)


def test_to_regex_refuses_rather_than_guesses():
    """The floor under the refusal itself — and a record of what each means.

    ``*.jsx?`` is GitHub's own documented example: it matches ``page.js`` AND
    ``page.jsx``, because ``?`` quantifies the preceding ``x``. Modelling ``?``
    as a single-character wildcard, the glob reading, gets both wrong.
    """
    for pattern in ["*.jsx?", "tools/**/*+.py", "tools/**/[abc]*.py"]:
        with pytest.raises(NotImplementedError):
            _to_regex(pattern)
    # the modelled subset still compiles
    for pattern in ["tools/**", "tools/**/meta.py", "static/example/1HEW.pdb", "**/README.md"]:
        assert _to_regex(pattern)


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
    # `**/` matches zero leading segments
    assert _to_regex("**/README.md").match("README.md")
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
