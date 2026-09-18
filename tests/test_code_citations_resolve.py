"""Every in-repo ``<path>.py::<symbol>`` citation must name a real symbol.

PRs #294, #305 and #309 converted roughly 450 code citations from
``<path>.py:NNN`` to ``<path>.py::<symbol>`` so they survive an edit above
them. Nothing verified the symbol half, so a rename silently rotted every
reference to it -- the failure the sweep was meant to end. #309 found six
already-rotted LINE citations, two of which had drifted into a different
function.

The scan is repo-wide and derived from ``git ls-files``. A guard scoped to a
hand-built candidate list reports its blind spots as zeros, so there is no
candidate list here: every tracked ``.py``/``.html``/``.js`` file is read, and
a citation that cannot be resolved FAILS rather than being skipped. The only
exceptions are ``_OUTSIDE_THIS_REPO`` and lines marked ``not-a-citation``,
both enumerated below.

Four things the guard deliberately accepts, because tightening any of them
buys no protection against a rename and would churn prose instead:

  * A bare symbol may be defined at ANY depth -- a method, a nested ``def``,
    a closure. ``app.py::inject_workspace_context`` (a context processor
    defined inside ``create_app``) and ``shared/targets.py::hotspot_error``
    (a method) are both cited bare and are both findable by name. A rename
    still turns them red, which is the whole point. An IMPORT of the name
    is NOT a definition -- a symbol moved out from under a leftover
    re-export must still go red, so ``ast.Import``/``ast.ImportFrom`` are
    not collected. No citation in the tree resolves that way today.
  * A directory-less citation (``base.py::parse_hotspot_residues``) resolves
    against the citing file's own directory first, then against every tracked
    file with that basename. Where the basename is ambiguous -- nine tool
    directories each hold a ``run_pipeline.py`` -- the symbol need only exist
    in ONE candidate. That is weaker than an exact path but not inert: no
    ``run_pipeline.py`` defining ``_run_shard`` means red.
  * A ``tools-hub/``-prefixed path resolves by dropping the prefix. The two
    files in ``contracts/`` are byte-locked to the sibling llm-proteinDesigner
    repo, so they are written from the directory holding BOTH -- editing one
    here to suit this repo's vantage point fails
    ``.github/workflows/contracts-drift.yml``. Stripping keeps the symbol
    CHECKED, which exempting the path would not.
  * A template path is resolved the way Jinja resolves it, relative to the
    loader root: ``components/results_shell.html`` is
    ``templates/components/results_shell.html``. No ``::`` citation is
    written that way today, so this changes no verdict on its own. It is
    here because ``_candidates`` is also what the ``.html`` measurement
    below runs on, and both paths spelled that way are real files -- calling
    them out-of-repo made that measurement wrong, and would mis-handle the
    first citation someone writes beside an ``{% include %}``.

The DOTTED form is checked strictly: ``tests/test_data_retention.py::_FakeTable.is_``
requires ``is_`` inside ``_FakeTable``, not merely somewhere in the file.

The deliberate bad-citation example in ``blueprints/targets.py`` needs no
exemption: it is written in the LINE form (``lab_projects.py:521``) precisely
because it is showing what a line citation costs, and this guard only reads
the ``::`` form.

The LINE citations that point at an ``.html`` template stay in that form, and
#309 was right to leave them. Converting them wholesale would LOSE precision:
measured over all of them (57 when this landed), only 10 sit inside a narrow
macro -- ``candidate_table``, ``results_panel``, ``worked_example``,
``about_reference_card``, ``preflight_panel``. Every other one falls inside a
page-level ``block content``/``block og_image``, which is a whole page body
and far coarser than the line it would replace, or inside no named region at
all. This guard is still why that is not a gap: it resolves an ``.html``
target to its ``{% macro %}``/``{% block %}`` names and a ``.js`` target to
its declared functions already, so any that someone does convert are checked
from then on with no change here.

WHAT THIS DOES NOT COVER, stated rather than left as a zero. The token above
needs a PATH. Two abbreviations in this repo elide it, so they are outside
this guard and are a separate sweep, not a silent hole. Every count below is
measured over the tree MINUS this file: the examples spelled out here are
themselves instances, so a re-measurement that includes them runs high.

  * A continuation ``::symbol`` whose path is named a line or two above AND
    is already spoken for by a citation of its own -- in
    ``shared/exports.py:148-151`` the path carries ``TestNonStringPdbKey`` and
    three more tests follow on bare ``::`` lines, unreachable because the path
    was consumed by its own match. 17 of these exist, and every one is that
    shape. A path left BARE at the end of a line is a different shape and IS
    read, by the ``_GAP`` in the token above; none of the 17 is that. They cannot be resolved by inheriting the nearest path
    without also matching ``::after``, ``::ffff`` and GitHub Actions'
    ``::error``, which a scan of the tree finds 21 of -- a heuristic whose own
    misses would be silent, which is what this file refuses to ship.
  * A module shorthand with no extension, ``test_multichain_targets::
    test_split_hotspot``. 17 of these exist, across 3 files, naming 5
    modules that are all tracked. Widening the token to bare identifiers
    would match any ``a::b`` in any file, so these convert in prose or not
    at all.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent

# Citations whose target is genuinely not in this repository. Each is a real
# file in another tree, so no symbol here can confirm it.
_OUTSIDE_THIS_REPO = frozenset(
    {
        # The Modal SDK's own source. It is an installed dependency
        # (``requirements.txt`` pins ``modal>=1.4,<2.0``), never a file here.
        "modal/_utils/function_utils.py",
        # The container repo. The real token is the templated
        # ``llm-proteinDesigner/docker/<tool>/run_pipeline.py``; the ``<tool>``
        # placeholder is what truncates it to a leading slash here.
        "/run_pipeline.py",
    }
)

# A line carrying this marker holds a ``path::symbol`` token that is data, not
# a reference -- currently one fabricated pytest nodeid, in
# ``tests/test_supabase_client_guard.py``.
_NOT_A_CITATION = "not-a-citation"

# A line break either side of the ``::``. 79-column prose wraps at the nearest
# space, and a citation is mostly space-free, so the ``::`` boundary is where
# the wrapper lands: 25 citations across 18 files break there, both ways --
# ``blueprints/tools.py`` then ``::_normalize_clone_pre_fill``
# (``templates/tools/rfdiffusion_form.html:259``), and
# ``tests/test_malformed_candidate_row_render.py::`` then
# ``test_a_tuple_of_good_rows_is_not_blanked`` (``shared/jobs.py:245``).
# Without this they match nothing at all, which is a blind spot reported as a
# zero -- the failure this guard exists to prevent. Pinned by
# ``test_a_citation_wrapped_at_the_colons_is_still_found``.
_GAP = r"(?:\n[ \t]*(?:#|\*|//)?[ \t]*)?"

_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"([A-Za-z0-9_./-]+\.(?:py|html|js))" + _GAP + r"::" + _GAP + r"("
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
)

# Long snake_case names get wrapped by the 79-column prose in this repo, and a
# wrapper breaks them at an underscore. Rejoining a name that ENDS in one is
# unambiguous, and without it the guard reports the truncation as a rename:
# ``shared/pdb_preflight_rules.py`` cites
# ``tests/test_pdb_preflight.py::test_the_proteina_cap_is_
# traceable_to_three_post_prealloc_shards`` across two comment lines,
# breaking it straight after the ``is_``.
#
# Applied at the matched token's own end, NOT substituted across the whole file
# first. Joining first deletes a newline, so every later citation in that file
# is reported one line early per join -- which was wrong at 24 sites, by up to
# five lines in ``tools/proteina/run_pipeline.py``. Pinned by
# ``test_a_wrapped_symbol_rejoins_without_moving_the_line_number``.
#
# The mirror case -- a break just BEFORE the underscore -- is NOT rejoined,
# because the continuation cannot be told apart from an unrelated ``_NAME``
# opening the next line, which is what the bullet list in the module docstring
# of ``tests/test_proteina_shard_size.py`` actually holds. The single citation
# that wrapped that way was unwrapped in prose instead.
_WRAP_TAIL = re.compile(r"\n[ \t]*(?:#|\*|//)?[ \t]*([A-Za-z0-9_]+)")

_JINJA_DEF = re.compile(r"{%-?\s*(?:macro|block)\s+([A-Za-z_]\w*)")
_JS_DEF = re.compile(r"(?:function\s+|(?:const|let|var|class)\s+)([A-Za-z_$][\w$]*)")


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


_TRACKED = _tracked()
_TRACKED_SET = frozenset(_TRACKED)
_BY_BASENAME: dict[str, list[str]] = {}
for _p in _TRACKED:
    _BY_BASENAME.setdefault(_p.rsplit("/", 1)[-1], []).append(_p)


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8", errors="replace")


def _citations(text: str):
    """Yield ``(line, path, symbol)`` for every citation token in ``text``."""
    marked = {i + 1 for i, ln in enumerate(text.split("\n")) if _NOT_A_CITATION in ln}
    for m in _TOKEN.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        symbol = m.group(2)
        end = m.end()
        if symbol.endswith("_"):
            tail = _WRAP_TAIL.match(text, end)
            if tail is not None:
                symbol += tail.group(1)
                end = tail.end()
        # ANY line the token occupies, plus the one above it -- the line above
        # so a marker never has to be crammed onto an already-long line of
        # code, and the rest because _GAP and _WRAP_TAIL let a single token
        # straddle two lines. Checking only the first would ignore a marker
        # written on the continuation, which reads as the natural place to put
        # it. Pinned by the last assert in
        # test_a_citation_wrapped_at_the_colons_is_still_found.
        if any(n in marked for n in range(line - 1, text.count("\n", 0, end) + 2)):
            continue
        yield line, m.group(1), symbol


_NAMES: dict[str, tuple[frozenset[str], dict]] = {}


def _py_names(rel: str) -> tuple[frozenset[str], dict]:
    """``(every def/class/module-level name, the nested def/class tree)``."""
    if rel not in _NAMES:
        tree = ast.parse(_read(rel))
        flat = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }

        def bound(target):
            """Names an assignment target BINDS. A subscript/attribute binds none.

            Descending with ``ast.walk`` instead harvests the base object of
            ``os.environ["X"] = y`` and puts ``os`` -- an import -- into the
            name set, which is the thing this file refuses to accept as a
            definition. Measured over the tree, that descent pulled in 11
            names, 9 of which -- in 9 files -- nothing else in the file
            defines, so they resolved falsely. ``os`` in ``gunicorn.conf.py``
            is the only subscript; the other 8 are ATTRIBUTE targets --
            ``stripe`` in four ``scripts/deploy/pass7_*.py`` and ``sys`` in
            four ``tools/proteina/*.py`` -- so the attribute shape is the
            common one, not the exotic one. Those counts are a measurement and
            no test enforces them; one instance of each shape is pinned by
            ``test_a_subscript_or_attribute_target_does_not_define_its_base``.
            """
            if isinstance(target, ast.Name):
                yield target.id
            elif isinstance(target, (ast.Tuple, ast.List)):
                for element in target.elts:
                    yield from bound(element)
            elif isinstance(target, ast.Starred):
                yield from bound(target.value)

        def module_level(body) -> None:
            for n in body:
                if isinstance(n, ast.Assign):
                    for target in n.targets:
                        flat.update(bound(target))
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    flat.add(n.target.id)
                elif isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                    for field in ("body", "orelse", "finalbody"):
                        module_level(getattr(n, field, []) or [])
                    for handler in getattr(n, "handlers", []) or []:
                        module_level(handler.body)

        module_level(tree.body)

        def subtree(body) -> dict:
            return {
                n.name: subtree(n.body)
                for n in body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            }

        _NAMES[rel] = (frozenset(flat), subtree(tree.body))
    return _NAMES[rel]


def _symbol_exists(rel: str, symbol: str) -> bool:
    """Is ``symbol`` defined in the tracked file ``rel``?"""
    if rel.endswith(".html"):
        return symbol in set(_JINJA_DEF.findall(_read(rel)))
    if rel.endswith(".js"):
        return symbol in set(_JS_DEF.findall(_read(rel)))
    flat, tree = _py_names(rel)
    if "." not in symbol:
        return symbol in flat
    scope = tree
    for part in symbol.split("."):
        if part not in scope:
            return False
        scope = scope[part]
    return True


def _candidates(citing: str, path: str) -> list[str]:
    """Tracked files a citation's path may mean. Empty means unresolvable."""
    if path in _TRACKED_SET:
        return [path]
    # ``contracts/`` is byte-locked to the sibling llm-proteinDesigner repo --
    # ``.github/workflows/contracts-drift.yml`` fails the build when either
    # file drifts from ``CONTRACTS_SHA256.lock``. Those files are therefore
    # written from the directory holding BOTH repos, so a path there reads
    # ``tools-hub/gpu/...`` here and ``docker/<tool>/...`` in the sibling.
    # Resolved rather than exempted, so a rename still turns them red.
    from_parent = path.removeprefix("tools-hub/")
    if from_parent != path and from_parent in _TRACKED_SET:
        return [from_parent]
    # Jinja addresses a template relative to the loader root, so prose written
    # alongside ``{% include "components/results_shell.html" %}`` names the
    # same path the engine does, without the ``templates/`` prefix. Both such
    # paths in the tree are real files, so calling them out-of-repo would have
    # been wrong. Pinned by ``test_a_loader_relative_template_path_resolves``.
    from_templates = f"templates/{path}"
    if from_templates in _TRACKED_SET:
        return [from_templates]
    if "/" in path:
        return []
    sibling = f"{citing.rsplit('/', 1)[0]}/{path}" if "/" in citing else path
    if sibling in _TRACKED_SET:
        return [sibling]
    return _BY_BASENAME.get(path, [])


def _all_citations() -> list[tuple[str, int, str, str]]:
    found = []
    for rel in _TRACKED:
        if rel.endswith((".py", ".html", ".js")):
            found.extend((rel, *c) for c in _citations(_read(rel)))
    return found


_CITATIONS = _all_citations()


def test_every_code_citation_resolves_to_a_real_symbol():
    """The guard. One message listing every rotted citation, not the first."""
    rotted = []
    for citing, line, path, symbol in _CITATIONS:
        if path in _OUTSIDE_THIS_REPO:
            continue
        candidates = _candidates(citing, path)
        if not candidates:
            rotted.append(
                f"{citing}:{line} cites {path}::{symbol} -- no such file is "
                f"tracked (fix the path, or add it to _OUTSIDE_THIS_REPO)"
            )
        elif not any(_symbol_exists(c, symbol) for c in candidates):
            rotted.append(
                f"{citing}:{line} cites {path}::{symbol} -- {symbol} is not "
                f"defined in {', '.join(candidates)}"
            )
    assert not rotted, "\n".join(["rotted code citations:", *rotted])


def test_the_scan_still_reaches_the_citations():
    """Tripwire: a broken regex or walker would otherwise pass on an empty set.

    The tree carried 467 tokens when this landed. The floor is slack enough to
    delete a file's worth of prose and tight enough that a scan returning
    nothing is red.
    """
    assert len(_CITATIONS) >= 400, f"only {len(_CITATIONS)} citations found"


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("_symbol_exists", True),  # a real top-level def in this file
        ("_symbol_exists_RENAMED", False),  # the rename this guard exists to catch
        ("_TOKEN", True),  # a module-level assignment
        ("os.path.join", False),  # a dotted path with no such class here
        # ``ast`` is imported at the top of this file but defined in the
        # stdlib. An import must not satisfy a citation.
        ("ast", False),
    ],
)
def test_the_checker_can_fail(symbol, expected):
    """Negative control. A guard that cannot go red proves nothing when green."""
    assert _symbol_exists("tests/test_code_citations_resolve.py", symbol) is expected


def test_a_citation_is_found_and_a_marked_line_is_not():
    """Both halves of ``_citations``: the token scan and the opt-out marker."""
    assert list(_citations("see shared/jobs.py::complete_job for it")) == [
        (1, "shared/jobs.py", "complete_job")
    ]
    marked = f"x = 'shared/jobs.py::complete_job'  # {_NOT_A_CITATION}"
    assert list(_citations(marked)) == []
    above = f"# {_NOT_A_CITATION}\nx = 'shared/jobs.py::complete_job'"
    assert list(_citations(above)) == []


def test_a_sibling_repo_path_resolves_rather_than_being_exempted():
    """``contracts/`` is byte-locked, so its citations are written from the
    directory holding both repos. Dropping the prefix keeps the symbol checked.
    """
    assert _candidates("contracts/rpc.py", "tools-hub/gpu/modal_client.py") == [
        "gpu/modal_client.py"
    ]
    assert _symbol_exists("gpu/modal_client.py", "ModalClient._build_payload")
    # Still a real check, and still only for a path that exists.
    assert not _symbol_exists("gpu/modal_client.py", "ModalClient._build_payload_X")
    assert _candidates("contracts/rpc.py", "tools-hub/gpu/no_such_file.py") == []


def test_a_citation_wrapped_at_the_colons_is_still_found():
    """A break at the ``::`` must not make a citation vanish from the scan.

    Both directions occur in the tree, and before ``_GAP`` both matched
    nothing at all -- 25 citations that the guard silently never checked.
    The reported line is the PATH's, not the continuation's.
    """
    at_colons = "see blueprints/tools.py\n   ::_normalize_clone_pre_fill now"
    assert list(_citations(at_colons)) == [
        (1, "blueprints/tools.py", "_normalize_clone_pre_fill")
    ]
    after_colons = "pinned by shared/jobs.py::\n    # complete_job and more"
    assert list(_citations(after_colons)) == [(1, "shared/jobs.py", "complete_job")]
    # The gap is ONE line break. A citation cannot reach across a blank line.
    assert list(_citations("shared/jobs.py\n\n::complete_job")) == []
    # The marker works on EITHER line a wrapped citation occupies, not just
    # the first -- the continuation reads as the natural place to put it.
    on_second = f"x = 'shared/jobs.py::\n# complete_job'  # {_NOT_A_CITATION}"
    assert list(_citations(on_second)) == []


def test_a_subscript_or_attribute_target_does_not_define_its_base():
    """``os.environ["X"] = y`` must not put ``os`` into the name set.

    ``gunicorn.conf.py`` is the live subscript instance: ``import os`` on line
    13, then ``os.environ["PROMETHEUS_MULTIPROC_DIR"] = ...`` at module level
    on line 208. Nothing in that file defines ``os``.

    The ATTRIBUTE shape is the commoner one -- 8 of the 9 names the old
    ``ast.walk`` descent leaked -- so it gets its own assert.
    ``tools/proteina/_design_canary.py`` imports ``sys`` and assigns
    ``sys.stdout`` at module level on line 140; nothing there defines ``sys``.
    Both shapes are covered by the same fall-through, which yields nothing for
    any node kind this function does not name.

    These are asserted as line numbers rather than ``::`` citations because
    the whole point is that neither name is a definition -- a ``::`` citation
    to one would be a citation this file is built to reject.
    """
    assert not _symbol_exists("gunicorn.conf.py", "os")
    assert not _symbol_exists("tools/proteina/_design_canary.py", "sys")
    # Tuple unpacking still binds, or narrowing the descent would be a hole of
    # its own: ``SAMPLE_MIN, SAMPLE_MAX, SAMPLE_DEFAULT = 1, 4, 1``.
    assert _symbol_exists("tools/opendde/__init__.py", "SAMPLE_MAX")


def test_a_loader_relative_template_path_resolves():
    """Jinja names a template without the ``templates/`` prefix, and so does
    the prose written beside an ``{% include %}``. Both such paths are real.
    """
    assert _candidates(
        "templates/components/candidate_table.html", "components/results_shell.html"
    ) == ["templates/components/results_shell.html"]
    # Only for a template that exists -- this is resolution, not an exemption.
    assert _candidates("templates/x.html", "components/no_such_template.html") == []


def test_a_wrapped_symbol_rejoins_without_moving_the_line_number():
    """A rejoin must not shift the line the failure message points at.

    The earlier draft substituted the join across the whole text before
    matching, which cost a newline and reported every later citation in that
    file one line early. The second citation below is what catches it: it is
    on line 4, and the joining version called it line 3.
    """
    text = (
        "line one\n"
        # not-a-citation: input text for the scanner, not a reference.
        "# see a/fixture.py::wrapped_name_\n"
        "# here for it\n"
        "# and shared/jobs.py::complete_job after\n"
    )
    assert list(_citations(text)) == [
        (2, "a/fixture.py", "wrapped_name_here"),
        (4, "shared/jobs.py", "complete_job"),
    ]
