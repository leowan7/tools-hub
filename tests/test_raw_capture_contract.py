"""The shared contract every container-side raw-capture function must honour.

Each tool's ``run_pipeline.py`` carries its own near-identical copy of "tar the
work tree to RAW_ARCHIVE_PATH before teardown". They were written by copying,
and two defects propagated with the copy — which is the reason this file is
table-driven rather than nine near-identical blocks in nine per-tool smoke
files. A new tool that clones the pattern gets tested the moment it is added to
``CAPTURES``, and both defects below were invisible to every existing test.

1. THE DESTINATION MUST RESOLVE ON CALL. ``def f(..., dest=RAW_ARCHIVE_PATH)``
   evaluates the constant once, at def time, and binds its VALUE into the
   function object; reassigning the module constant afterwards is then silently
   ignored and the tar lands on the real ``/tmp`` path. This was live in
   proteina — its test harness set RAW_ARCHIVE_PATH to keep archives inside
   tmp_path and had been writing to the real path the whole time.

2. THE CLEANUP HANDLER MUST NOT RAISE. These functions are called from a
   ``finally`` in main(), so an exception escaping one replaces whatever exit
   was already in flight — on exactly the crashed run whose diagnostics matter
   most. The handler deletes a partial tar at its dest variable, but that
   variable is assigned partway through the ``try``: any failure before that
   line left it unbound, and the resulting UnboundLocalError is a NameError,
   which the inner ``except OSError`` does not catch.

All nine copies are in ``CAPTURES``. boltz2, opendde and proteina were listed in
``_NOT_YET_COVERED`` while their fix was still in flight; it landed in #136, and
they moved here rather than into three more per-tool blocks.

That list stays, empty, because it is the mechanism that makes the NEXT copy of
this pattern impossible to overlook: the registry test below fails on any copy
in the tree that is in neither place. It is asserted empty by
``test_the_not_yet_covered_list_is_empty``, because the registry test can only
check that a reason STRING exists, not that it is true — and a stale reason is
exactly what it failed to catch once already. An entry re-added there now has to
survive a test that says the list should be empty, which is a decision rather
than a note nobody re-reads.
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from tools.af2 import run_pipeline as af2_rp
from tools.boltz2 import run_pipeline as boltz2_rp
from tools.colabfold import run_pipeline as colabfold_rp
from tools.esmfold import run_pipeline as esmfold_rp
from tools.esmfold2_design import run_pipeline as esmfold2_rp
from tools.iggm import run_pipeline as iggm_rp
from tools.mpnn import run_pipeline as mpnn_rp
from tools.opendde import run_pipeline as opendde_rp
from tools.proteina import run_pipeline as proteina_rp


class _PoisonPath:
    """An argument that fails every way a work dir can fail to be a path.

    ``os.path.isdir`` / ``os.path.exists`` swallow OSError and ValueError but
    NOT TypeError, so ``__fspath__`` is what gets past the copies that hand the
    raw argument straight to isdir. The copies that do ``abspath(str(work_dir))``
    first are tripped by ``__str__``. One object covers both shapes.

    ``__repr__`` is deliberately left alone so a failing assertion can still be
    rendered by pytest.
    """

    def __str__(self):
        raise RuntimeError("simulated: work_dir is not stringifiable")

    def __fspath__(self):
        raise TypeError("simulated: work_dir is not a usable path")


@dataclass(frozen=True)
class Capture:
    tool: str
    module: object
    func_name: str
    # How to invoke the function for a normal capture, and with a poisoned arg.
    #
    # ``call`` should mirror the PRODUCTION call site's argument shape and types
    # rather than merely something the function accepts: a row that passes a str
    # where production passes a Path stops exercising what ships. That one is
    # HAND-MAINTAINED and unchecked, deliberately. Checking it automatically
    # would mean re-deriving each caller's argument expression from its source,
    # and the only tractable form of that is a second hand-written copy of the
    # call sites — the duplicated state this table exists to avoid. What is
    # provided instead is reviewability: every row below names the production
    # call site it mirrors, on the line above the lambda.
    #
    # ``poison_call`` IS checked, by test_poison_call_delivers_the_poison. A row
    # that quietly stops delivering the _PoisonPath vacates
    # test_failure_before_dest_is_bound_does_not_escape for that tool while
    # leaving it green, so that one cannot be left to review.
    call: Callable
    poison_call: Callable
    # Only the copies that expose a `dest` parameter can have a frozen default.
    has_dest_param: bool
    # True for the copies that write the tar straight to dest and therefore need
    # an `os.remove(dest)` in their error handler. esmfold2_design is the one
    # that does not: it tars into a mkdtemp and moves the finished file into
    # place, so a failed WRITE leaves nothing at dest to clean up. That is a
    # narrower claim than "nothing can ever be left at dest" — the move only
    # avoids a partial while it is a rename, and shutil.move falls back to a
    # truncating copy2 across filesystems. Its run_pipeline pins the staging
    # dir to dest's own directory to keep the rename; see the comment there.
    has_dest_cleanup: bool
    # A literal fragment of THIS copy's "nothing to archive" warning; the nine
    # word it differently (boltz2 and opendde share one, and it carries an EM
    # DASH that is matched literally — retyping it as a hyphen reddens this
    # row). Its job is to pin that wording, so the caplog assertion in
    # test_missing_work_dir_does_not_escape cannot silently stop matching.
    #
    # It is NOT what tells the early return apart from "crashed, then cleaned
    # up after itself": every copy logs this line BEFORE it returns, so
    # the crash path emits exactly the same line. Only "tarfile.open was never
    # reached" separates them, and that is what the test asserts.
    missing_src_log: str

    @property
    def func(self):
        return getattr(self.module, self.func_name)


# esmfold2_design is the only copy that takes a LIST of sources, and production
# hands it one directory plus one plain FILE
# (``[str(PDB_OUTPUT_DIR), SMOKE_RESULTS_PATH]``). Keep both shapes in the
# fixture: that copy's self-containment check branches on ``os.path.isdir``, so
# a dir-only fixture never exercises the file arm.
_SIDECAR_NAME = "smoke_results.json"


def _sidecar(src) -> Path:
    return Path(src).with_name(_SIDECAR_NAME)


CAPTURES = [
    Capture(
        "af2", af2_rp, "archive_raw",
        # Production passes a Path (``workdir = Path(_td)``), not a str.
        call=lambda fn, src: fn(Path(src), "af2_batch"),
        poison_call=lambda fn, bad: fn(bad, "af2_batch"),
        has_dest_param=False,
        has_dest_cleanup=True,
        missing_src_log="[raw] no work dir at ",
    ),
    Capture(
        "colabfold", colabfold_rp, "archive_work_dir",
        # Production passes the raw TemporaryDirectory str (``_td``).
        call=lambda fn, src: fn(str(src)),
        poison_call=lambda fn, bad: fn(bad),
        has_dest_param=True,
        has_dest_cleanup=True,
        missing_src_log="[raw] no work dir to archive: ",
    ),
    Capture(
        "esmfold", esmfold_rp, "_archive_raw",
        # Production passes ``str(workdir)``.
        call=lambda fn, src: fn(str(src)),
        poison_call=lambda fn, bad: fn(bad),
        has_dest_param=False,
        has_dest_cleanup=True,
        missing_src_log=" is not a directory - nothing to archive",
    ),
    Capture(
        "mpnn", mpnn_rp, "_archive_raw",
        # Production passes a Path.
        call=lambda fn, src: fn(Path(src)),
        poison_call=lambda fn, bad: fn(bad),
        has_dest_param=False,
        has_dest_cleanup=True,
        missing_src_log="raw capture: no work dir at ",
    ),
    Capture(
        "iggm", iggm_rp, "_ship_raw",
        # Production passes a Path.
        call=lambda fn, src: fn(Path(src)),
        poison_call=lambda fn, bad: fn(bad),
        has_dest_param=True,
        has_dest_cleanup=True,
        missing_src_log="[raw] no work dir to archive (nothing was created?)",
    ),
    Capture(
        "boltz2", boltz2_rp, "archive_raw_outputs",
        # Production passes ``str(workdir)``.
        call=lambda fn, src: fn(str(src)),
        poison_call=lambda fn, bad: fn(bad),
        has_dest_param=True,
        has_dest_cleanup=True,
        # NB the em dash: this fragment is matched literally, so a copy that
        # "tidies" it to a hyphen must fail here rather than silently stop
        # being checked.
        missing_src_log=" is not a directory — nothing to archive",
    ),
    Capture(
        "opendde", opendde_rp, "archive_raw_outputs",
        # Production passes ``str(workdir)`` — that call site IS byte-identical
        # to boltz2's. The function is not: same executable code (equal ASTs
        # with docstrings stripped), different docstring, comments and wrapping.
        call=lambda fn, src: fn(str(src)),
        poison_call=lambda fn, bad: fn(bad),
        has_dest_param=True,
        has_dest_cleanup=True,
        missing_src_log=" is not a directory — nothing to archive",
    ),
    Capture(
        "proteina", proteina_rp, "archive_raw_outputs",
        # Production passes a Path (``archive_raw_outputs(run_dir)``), and this
        # copy is the one that has since diverged: it carries a tar filter and
        # the _hub_input_written gate that no other copy has. The shared
        # contract still applies to it — the filter only ever decides members
        # UNDER _hub_input, which _work_tree never creates — and pinning it here
        # is what keeps the divergence from drifting off the common contract.
        call=lambda fn, src: fn(Path(src)),
        poison_call=lambda fn, bad: fn(bad),
        has_dest_param=True,
        has_dest_cleanup=True,
        missing_src_log="raw capture: nothing to archive, no dir at ",
    ),
    Capture(
        "esmfold2_design", esmfold2_rp, "_archive_raw",
        # Production passes TWO sources, one of them a file, not a directory.
        call=lambda fn, src: fn([str(src), str(_sidecar(src))]),
        poison_call=lambda fn, bad: fn([bad]),
        has_dest_param=True,
        has_dest_cleanup=False,
        missing_src_log="raw capture: nothing to archive (tool wrote no output?)",
    ),
]

# Copies that exist in the tree and are knowingly NOT in CAPTURES. Being on this
# list is a declaration, not an excuse: test_every_raw_capture_copy_is_covered_
# or_listed fails on a copy that is on neither, so the next tool to clone the
# pattern has to be dealt with rather than merely overlooked.
_NOT_YET_COVERED: dict[str, tuple[str, str]] = {
    # tool: (function name, why it is not in CAPTURES)
    #
    # Empty, and test_the_not_yet_covered_list_is_empty keeps it that way. It
    # held boltz2/opendde/proteina until #136 landed their fix; they are now in
    # CAPTURES above. Left in place because it is the escape hatch the registry
    # test below needs to be able to name a copy that genuinely cannot be
    # tabled yet — deleting it would leave that test with no way to distinguish
    # "overlooked" from "deliberately deferred".
}

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

_IDS = [c.tool for c in CAPTURES]
_BY_TOOL = {c.tool: c for c in CAPTURES}
_WITH_DEST = [c for c in CAPTURES if c.has_dest_param]
_WITH_DEST_IDS = [c.tool for c in _WITH_DEST]
_WITH_CLEANUP = [c for c in CAPTURES if c.has_dest_cleanup]
_WITH_CLEANUP_IDS = [c.tool for c in _WITH_CLEANUP]


def _work_tree(tmp_path):
    src = tmp_path / "work"
    src.mkdir()
    (src / "result.txt").write_text("payload the container must not throw away")
    # Only esmfold2_design reads this one; harmless for the rest, which are
    # handed the directory alone.
    _sidecar(src).write_text('{"smoke": "the copy the wrapper may never return"}')
    return src


# ---------------------------------------------------------------------------
# 0 — the table describes the real functions
#
# Every boolean here duplicates something the source already states. Duplicated
# state drifts, and a wrong flag silently DESELECTS a tool from the test that
# would have caught its defect, so each flag is pinned to the thing it mirrors.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", CAPTURES, ids=_IDS)
def test_has_dest_param_matches_the_signature(cap):
    exposes_dest = "dest" in inspect.signature(cap.func).parameters
    assert exposes_dest == cap.has_dest_param, (
        f"CAPTURES says {cap.tool}.{cap.func_name} has_dest_param="
        f"{cap.has_dest_param}, but its signature says {exposes_dest}")


@pytest.mark.parametrize("cap", CAPTURES, ids=_IDS)
def test_has_dest_cleanup_matches_the_source(cap):
    """The flag must track the presence of a real ``os.remove`` handler.

    A copy that writes straight to dest needs one; a copy that stages elsewhere
    has nothing at dest to remove. Getting this wrong would quietly drop a tool
    from test_partial_tar_is_removed_when_the_write_fails.
    """
    removes_dest = "os.remove(" in inspect.getsource(cap.func)
    assert removes_dest == cap.has_dest_cleanup, (
        f"CAPTURES says {cap.tool}.{cap.func_name} has_dest_cleanup="
        f"{cap.has_dest_cleanup}, but its source "
        f"{'does' if removes_dest else 'does not'} call os.remove")


@pytest.mark.parametrize("cap", CAPTURES, ids=_IDS)
def test_missing_src_log_matches_the_source(cap):
    assert cap.missing_src_log in inspect.getsource(cap.func), (
        f"CAPTURES expects {cap.tool}.{cap.func_name} to log "
        f"{cap.missing_src_log!r} when there is nothing to archive, but that "
        "text is not in the function any more — the wording drifted, and the "
        "caplog assertion in test_missing_work_dir_does_not_escape would stop "
        "matching anything real")


@pytest.mark.parametrize("cap", CAPTURES, ids=_IDS)
def test_poison_call_delivers_the_poison(cap):
    """``poison_call`` must actually hand the _PoisonPath to the function.

    A row whose lambda drops the poison — ``lambda fn, bad: fn([])`` — silently
    VACATES test_failure_before_dest_is_bound_does_not_escape for that tool: the
    function is called with something harmless, returns None, and the assertion
    passes without ever reaching the window it exists to test. Nothing about the
    green run says so. ``call`` and ``poison_call`` were the only fields in this
    table with no consistency test; this is the half that can go wrong invisibly.

    The function under test is replaced by a spy, so this asserts a property of
    the TABLE and does not depend on what any copy does with the argument.
    """
    poison = _PoisonPath()
    seen: list[tuple] = []

    def spy(*args, **kwargs):
        seen.append(args + tuple(kwargs.values()))
        return None

    cap.poison_call(spy, poison)

    assert seen, (
        f"CAPTURES[{cap.tool}].poison_call never called the function it was "
        "handed, so the never-raises test it feeds passes vacuously")
    passed = seen[0]
    # Identity only. _PoisonPath raises from __str__ and __fspath__, so an
    # equality or ``in`` test would drag one of them into play.
    delivered = any(
        arg is poison
        or (isinstance(arg, (list, tuple, set)) and any(x is poison for x in arg))
        for arg in passed
    )
    assert delivered, (
        f"CAPTURES[{cap.tool}].poison_call did not pass the _PoisonPath to "
        f"{cap.func_name} — it called it with {len(passed)} argument(s), none "
        "of which is the poison, nor a list/tuple/set containing it. "
        "test_failure_before_dest_is_bound_does_not_escape is exercising a "
        f"harmless call for {cap.tool} and cannot fail")


def _raw_capture_functions(path: Path):
    """``({func name: lineno}, [stray linenos])`` for one ``run_pipeline.py``.

    ``None`` if the module never binds the name RAW_ARCHIVE_PATH by assignment,
    i.e. it is not one of the copies. The binding is looked for ANYWHERE in the
    tree rather than only in ``tree.body``: scanning only the top level meant an
    assignment nested in a module-level ``try:`` or ``if:`` hid the whole file
    from the coverage check.

    Every READ of the constant is attributed to its nearest enclosing ``def``,
    so a default argument (``dest=RAW_ARCHIVE_PATH``, the shape every copy had
    before the fix and none has now) counts for the function it defaults for, and a
    read at module level is reported as a stray rather than quietly dropped.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    binds = any(
        isinstance(node, ast.Name)
        and node.id == "RAW_ARCHIVE_PATH"
        and isinstance(node.ctx, ast.Store)
        for node in ast.walk(tree)
    )
    if not binds:
        return None

    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    found: dict[str, int] = {}
    stray: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id != "RAW_ARCHIVE_PATH":
            continue
        if isinstance(node.ctx, ast.Store):
            continue
        owner = parents.get(node)
        while owner is not None and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            owner = parents.get(owner)
        if owner is None:
            stray.append(node.lineno)
        else:
            found.setdefault(owner.name, owner.lineno)
    return found, stray


def test_every_raw_capture_copy_is_covered_or_listed():
    """Every copy the scan can SEE must be in CAPTURES or _NOT_YET_COVERED.

    Precisely what it enforces: for every ``tools/**/run_pipeline.py`` that
    assigns the name ``RAW_ARCHIVE_PATH`` somewhere, each function that READS
    that name must be in one of the two lists. Nothing wider.

    That is a heuristic, and the useful thing to record about a heuristic is
    where it stops. It was probed with deliberate evasions; these three got past
    it, each leaving the suite green, and are NOT closed:

      * a copy whose constant is spelled anything else — the scan keys on the
        literal name;
      * a copy that IMPORTS the constant instead of assigning it, since
        ``from ... import RAW_ARCHIVE_PATH`` binds no ``ast.Name`` Store;
      * a capture in a file not named ``run_pipeline.py``.

    Two more were closed rather than written down, because they were cheap: the
    assignment is now looked for anywhere in the tree instead of only in
    ``tree.body``, so nesting it in a module-level ``try:``/``if:`` no longer
    hides the file; and the glob is recursive, so ``tools/<t>/<sub>/
    run_pipeline.py`` is seen too (reported under the id ``<t>/<sub>``).

    So this does not prove the family is covered. It proves nobody added another
    copy in the OBVIOUS shape without declaring it — which is the failure that
    actually happened: three of the nine copies were covered by neither this
    file nor any per-tool test, and the suite was fully green. The consistency
    tests above check that the ROWS describe the source; this is the only one
    that checks the source has no rows MISSING, and it also runs the other way —
    a _NOT_YET_COVERED entry the scan can no longer find, or one that has since
    been promoted into CAPTURES, fails here too.
    """
    covered = {(c.tool, c.func_name) for c in CAPTURES}
    listed = {(tool, fn) for tool, (fn, _why) in _NOT_YET_COVERED.items()}

    scanned: set[tuple[str, str]] = set()
    uncovered: list[str] = []
    strays: list[str] = []
    for path in sorted(_TOOLS_DIR.rglob("run_pipeline.py")):
        result = _raw_capture_functions(path)
        if result is None:
            continue
        found, stray = result
        # Relative to tools/, so a copy one level down reports as "<t>/<sub>"
        # rather than colliding with, or masquerading as, a top-level tool.
        tool = path.relative_to(_TOOLS_DIR).parent.as_posix()
        assert found, (
            f"{tool}/run_pipeline.py defines RAW_ARCHIVE_PATH but no function "
            "reads it — either the constant is dead or the capture moved out of "
            "this scan's reach, and this test would then be vouching for "
            "coverage it never actually checked")
        strays += [f"{tool}/run_pipeline.py:{ln}" for ln in stray]
        for name, lineno in found.items():
            scanned.add((tool, name))
            if (tool, name) not in covered and (tool, name) not in listed:
                uncovered.append(f"{tool}.{name} ({tool}/run_pipeline.py:{lineno})")

    assert scanned, (
        f"no raw-capture copies found under {_TOOLS_DIR} at all; the glob or "
        "the detection broke, and an empty scan passes every check below")
    assert not strays, (
        f"RAW_ARCHIVE_PATH is read outside any function at {strays}. That read "
        "cannot be attributed to a capture function, so the coverage check "
        "below no longer covers everything that writes the archive")
    assert not uncovered, (
        f"raw-capture copies in neither CAPTURES nor _NOT_YET_COVERED: "
        f"{uncovered}. Add the tool to CAPTURES, or to _NOT_YET_COVERED with "
        "the reason it cannot be tested yet. Being in neither is how the family "
        "drifted in the first place")

    stale = sorted(listed - scanned)
    assert not stale, (
        f"_NOT_YET_COVERED names {stale}, which the scan no longer finds — "
        "renamed or removed, so the entry is excusing nothing and hides the "
        "real name if it comes back")
    promoted = sorted(
        tool for tool, (fn, _why) in _NOT_YET_COVERED.items() if (tool, fn) in covered
    )
    assert not promoted, (
        f"{promoted} is in CAPTURES and still on the skip list; drop the "
        "_NOT_YET_COVERED entry so the list keeps meaning 'untested'")
    unexplained = sorted(
        tool for tool, (_fn, why) in _NOT_YET_COVERED.items() if not why.strip()
    )
    assert not unexplained, (
        f"{unexplained} is skipped with no reason given; a bare name on the "
        "skip list is indistinguishable from an oversight")


def test_the_not_yet_covered_list_is_empty():
    """Every known copy is in CAPTURES, and re-listing one is a decision.

    The test above can check that a skipped tool carries a reason STRING. It
    cannot check the reason is TRUE, and that gap has already cost something:
    boltz2, opendde and proteina sat here reading "the fix for these three is
    not in this branch" for a while after the fix had landed in #136, with the
    whole suite green. Prose cannot be asserted; emptiness can.

    So this is not a second coverage check — it is the thing that makes the
    skip list expire. Adding an entry back now costs a deliberate edit to a
    test that says there should be none, instead of a comment nobody re-reads.
    If a copy genuinely cannot be tabled yet, delete this test in the same
    commit that lists it and say why; that is the conversation this is for.

    KNOWN, AND INTENDED: while this passes, it makes part of its neighbour
    unable to fail. test_every_raw_capture_copy_is_covered_or_listed's `stale`,
    `promoted` and `unexplained` assertions all quantify over _NOT_YET_COVERED,
    and its `not in listed` branch never fires against an empty dict. Those
    checks are not dead — they are what guards the list the moment anything is
    put back on it, which is exactly when they are needed. Recorded here so the
    next reader does not "discover" four vacuous assertions and delete them.
    """
    assert _NOT_YET_COVERED == {}, (
        f"{sorted(_NOT_YET_COVERED)} is on the skip list. If its fix has "
        "landed, move it into CAPTURES. If it genuinely cannot be tabled, this "
        "test is what you have to argue with — which is the point")


# ---------------------------------------------------------------------------
# 1 — the destination resolves on call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", CAPTURES, ids=_IDS)
def test_archive_follows_a_reassigned_constant(cap, tmp_path, monkeypatch):
    """Reassigning RAW_ARCHIVE_PATH must redirect the archive.

    True by construction for the copies that read the constant inside the
    function body; only true for the ones with a ``dest`` parameter once that
    default stops being the constant itself.
    """
    src = _work_tree(tmp_path)
    redirected = tmp_path / "redirected.tgz"
    monkeypatch.setattr(cap.module, "RAW_ARCHIVE_PATH", str(redirected))

    cap.call(cap.func, src)

    assert redirected.is_file(), (
        f"{cap.tool}.{cap.func_name} ignored the reassigned RAW_ARCHIVE_PATH — "
        "its default is frozen at import, so the tar went to the real /tmp path")
    with tarfile.open(redirected) as tf:
        assert any(n.endswith("result.txt") for n in tf.getnames())


@pytest.mark.parametrize("cap", _WITH_DEST, ids=_WITH_DEST_IDS)
def test_dest_default_is_not_a_baked_in_path(cap):
    """Once nothing reassigns the constant the regression is behaviourally
    invisible, so pin the signature as well as the behaviour."""
    default = inspect.signature(cap.func).parameters["dest"].default
    assert default is None, (
        f"{cap.tool}.{cap.func_name} defaults dest to {default!r}; a "
        "module-constant default is bound at import and cannot follow a later "
        "reassignment of RAW_ARCHIVE_PATH")


@pytest.mark.parametrize("cap", _WITH_DEST, ids=_WITH_DEST_IDS)
def test_explicit_dest_still_wins(cap, tmp_path, monkeypatch):
    src = _work_tree(tmp_path)
    constant = tmp_path / "constant.tgz"
    explicit = tmp_path / "explicit.tgz"
    monkeypatch.setattr(cap.module, "RAW_ARCHIVE_PATH", str(constant))

    cap.call(lambda *a, **kw: cap.func(*a, dest=str(explicit), **kw), src)

    assert explicit.is_file()
    assert not constant.exists(), (
        f"{cap.tool}.{cap.func_name} overrode an explicit dest with the constant")


# What each copy is MEASURED to leave in the cwd for dest="". The shared
# postcondition — RAW_ARCHIVE_PATH is not written — is the point of the test
# below, but what happens at the empty path itself cannot be one assertion for
# all of them: ``os.path.abspath("")`` is the cwd, which is a DIRECTORY, and the
# copies meet a directory differently. colabfold and iggm hand it straight to
# ``tarfile.open``, which fails, and their handler's ``os.remove`` of a
# directory fails too and is swallowed — nothing is left. esmfold2_design tars
# into a staging dir and ``shutil.move``s the finished file AT a directory,
# which moves it INSIDE, so it succeeds and leaves the archive in the cwd.
# boltz2, opendde and proteina behave as colabfold and iggm do, and it was
# measured rather than reasoned from their shape: each was run with dest="" and
# observed to raise nothing, write nothing to the constant, and leave nothing
# new in the cwd. Their commonpath self-containment guard does NOT fire on the
# way — abspath("") is the cwd, which is the PARENT of the work tree, not the
# work tree itself — so what stops them is the same tarfile.open on a directory
# that stops the other two.
_EMPTY_DEST_LEAVES_IN_CWD = {
    "colabfold": None,
    "iggm": None,
    "esmfold2_design": "raw_archive.tgz",
    "boltz2": None,
    "opendde": None,
    "proteina": None,
}


@pytest.mark.parametrize("cap", _WITH_DEST, ids=_WITH_DEST_IDS)
def test_an_empty_dest_is_honoured_as_given(cap, tmp_path, monkeypatch):
    """ONLY None resolves — the guard is identity, not truthiness.

    Every other test in this file passes just as happily with ``if not dest``,
    which would quietly swap a caller's explicit falsy dest for the module
    constant and land the tar on a path the caller never named. Measured on the
    three copies this was written for (colabfold, iggm, esmfold2_design):
    flipping them to ``if not dest`` reddens only this test.

    That is NO LONGER the whole story, and the difference is worth stating
    rather than leaving for the next person to trip over. boltz2, opendde and
    proteina also have per-tool twins of this assertion in their own smoke
    files, so flipping THOSE three reddens 9 tests, 6 of them outside this
    file. This test is still the only cross-tool statement of the rule; it is
    no longer the only thing that fails when the rule is broken.

    The empty string is the reachable falsy case. ``os.path.abspath("")`` is the
    cwd, so chdir into tmp_path first and the doomed write stays inside the tmp
    dir instead of landing in the repo.
    """
    assert set(_EMPTY_DEST_LEAVES_IN_CWD) == {c.tool for c in _WITH_DEST}, (
        "a copy gained or lost its dest parameter — record what it actually "
        "does with dest='' in _EMPTY_DEST_LEAVES_IN_CWD rather than assuming "
        "it behaves like one of the others; they do not agree")

    src = _work_tree(tmp_path)
    constant = tmp_path / "constant.tgz"
    monkeypatch.setattr(cap.module, "RAW_ARCHIVE_PATH", str(constant))
    monkeypatch.chdir(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}

    assert cap.call(lambda *a, **kw: cap.func(*a, dest="", **kw), src) is None, (
        f"{cap.tool}.{cap.func_name} must return None on every path")

    assert not constant.exists(), (
        f'{cap.tool}.{cap.func_name} replaced dest="" with RAW_ARCHIVE_PATH — '
        "the resolution is testing truthiness instead of identity, so an "
        "explicit falsy dest is silently overridden and the tar lands on a "
        "path the caller never named")

    left = sorted({p.name for p in tmp_path.iterdir()} - before)
    expected = _EMPTY_DEST_LEAVES_IN_CWD[cap.tool]
    assert left == ([] if expected is None else [expected]), (
        f"{cap.tool}.{cap.func_name} left {left} in the cwd for dest=''; "
        f"_EMPTY_DEST_LEAVES_IN_CWD records {expected!r}. The empty dest is "
        "still being honoured (the assertion above passed), but what it does "
        "at that path changed")


# ---------------------------------------------------------------------------
# 2 — the documented "never raises" contract, on the error paths too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", CAPTURES, ids=_IDS)
def test_failure_before_dest_is_bound_does_not_escape(cap, tmp_path, monkeypatch):
    """Pre-fix this raised UnboundLocalError out of the cleanup handler."""
    monkeypatch.setattr(cap.module, "RAW_ARCHIVE_PATH", str(tmp_path / "raw.tgz"))

    assert cap.poison_call(cap.func, _PoisonPath()) is None, (
        f"{cap.tool}.{cap.func_name} must return None on every path")


@pytest.mark.parametrize("cap", CAPTURES, ids=_IDS)
def test_missing_work_dir_does_not_escape(cap, tmp_path, monkeypatch, caplog):
    """The ordinary early-return path: nothing to archive, no exception, no tar.

    Neither postcondition discriminates on its own, and neither does the log.
    Every copy logs its "nothing to archive" warning BEFORE it returns, so the
    crash path emits the identical line; and delete the bare ``return`` while
    keeping that log and most copies still leave no tar, because ``tarfile.open``
    creates dest, ``tf.add`` raises on the missing source and the cleanup
    handler removes the partial. Measured, not assumed, and RE-measured when the
    table went to nine: dropping only the ``return`` leaves af2, colabfold,
    esmfold, mpnn and iggm green, and reddens exactly this test for boltz2,
    opendde and proteina — nothing else in the suite. For those three it is the
    only guard there is, which is the clearest single reason the table earns
    its three new rows.

    What actually separates "returned early, as designed" from "entered the
    archive path and was tidied up after" is that the archive path was never
    entered — so assert ``tarfile.open`` is never called. The caplog assertion
    stays for its other job: pinning ``missing_src_log`` to the real wording.
    """
    monkeypatch.setattr(cap.module, "RAW_ARCHIVE_PATH", str(tmp_path / "raw.tgz"))

    real_open = tarfile.open
    opened: list[str] = []

    def recording_open(name, mode="r", *args, **kwargs):
        opened.append(os.path.abspath(str(name)))
        # Delegate, so the copies behave exactly as they would in production and
        # the postconditions below stay honest about the unmutated code.
        return real_open(name, mode, *args, **kwargs)

    # colabfold / af2 / esmfold import tarfile lazily INSIDE the function, so
    # patching their module namespace would not be seen. Patch the attribute on
    # the shared tarfile module instead; monkeypatch restores it either way.
    monkeypatch.setattr(tarfile, "open", recording_open)

    with caplog.at_level(logging.WARNING):
        cap.call(cap.func, tmp_path / "does_not_exist")

    assert not opened, (
        f"{cap.tool}.{cap.func_name} opened a tar at {opened} for a work dir "
        "that does not exist — it did not take the early return, it entered the "
        "archive path and something downstream covered for it")
    assert not (tmp_path / "raw.tgz").exists()
    assert any(cap.missing_src_log in r.getMessage() for r in caplog.records), (
        f"{cap.tool}.{cap.func_name} never logged {cap.missing_src_log!r}; the "
        "operator gets no record that the tree was missing, and the wording "
        f"CAPTURES pins has drifted. Logged instead: "
        f"{[r.getMessage() for r in caplog.records]}")


@pytest.mark.parametrize("cap", _WITH_CLEANUP, ids=_WITH_CLEANUP_IDS)
def test_partial_tar_is_removed_when_the_write_fails(cap, tmp_path, monkeypatch):
    """The None-guard must not disable the cleanup it guards.

    When the write fails AFTER the dest variable is bound, the truncated tar
    still has to go: modal_app parks whatever file exists, and an archive that
    reports success but cannot be read is worse than no archive at all.

    Restricted to the copies that HAVE such a cleanup. esmfold2_design stages
    the tar elsewhere and moves it in, so on this fixture it writes nothing to
    dest and an empty dest would prove the absence of a write rather than the
    presence of a cleanup — an assertion nothing could fail. (Its staging keeps
    dest clean only while the move is a rename; across filesystems shutil.move
    degrades to copy2, which can truncate at dest, which is why its
    run_pipeline pins the staging dir to dest's own directory.) Its actual
    contract is asserted separately, below.
    """
    src = _work_tree(tmp_path)
    dest = tmp_path / "raw.tgz"
    monkeypatch.setattr(cap.module, "RAW_ARCHIVE_PATH", str(dest))

    opened: list[str] = []

    def exploding_open(name, mode="r", *args, **kwargs):
        opened.append(os.path.abspath(str(name)))
        # Leave behind the truncated file a real mid-write ENOSPC would.
        with open(name, "wb") as fh:
            fh.write(b"not a readable tar")
        raise OSError(28, "No space left on device")

    # colabfold / af2 / esmfold import tarfile lazily INSIDE the function, so
    # patching the module's namespace would not be seen. Patch the attribute on
    # the shared tarfile module instead; monkeypatch restores it either way.
    monkeypatch.setattr(tarfile, "open", exploding_open)

    cap.call(cap.func, src)

    assert opened, (
        f"{cap.tool}.{cap.func_name} never opened a tar at all — neuter it to a "
        "bare `return` and 'dest is empty' is satisfied by a function that "
        "simply never wrote, so the cleanup assertion below would prove nothing")
    assert not dest.exists(), (
        f"{cap.tool}.{cap.func_name} left a partial tar behind after a failed "
        "capture; the wrapper would park an unreadable archive")


def test_esmfold2_design_writes_the_tar_somewhere_other_than_dest(
    tmp_path, monkeypatch
):
    """esmfold2_design's answer to the partial-tar problem is staging.

    It has no cleanup handler because the staging makes one unnecessary FOR THE
    FAILED WRITE: the tar is built in a fresh mkdtemp and only moved to dest
    once it is complete. That is narrower than "nothing can ever be left at
    dest" — shutil.move degrades to copy2 across filesystems and an interrupted
    copy2 can leave a truncated file at dest, so the property holds only while
    the staging dir shares dest's filesystem, which its run_pipeline arranges by
    pinning ``dir=`` to dest's own directory.

    The staged write is a positive claim, so assert it positively — record every
    path handed to ``tarfile.open`` and require that dest is not among them.
    "dest is empty" alone would pass for a function that simply never ran.
    """
    cap = _BY_TOOL["esmfold2_design"]
    src = _work_tree(tmp_path)
    dest = tmp_path / "raw.tgz"
    monkeypatch.setattr(cap.module, "RAW_ARCHIVE_PATH", str(dest))

    opened: list[str] = []

    def recording_open(name, mode="r", *args, **kwargs):
        opened.append(os.path.abspath(str(name)))
        # Fail mid-write the way ENOSPC would, leaving a truncated file behind
        # at whatever path the function chose.
        with open(name, "wb") as fh:
            fh.write(b"not a readable tar")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tarfile, "open", recording_open)

    cap.call(cap.func, src)

    assert opened, (
        "esmfold2_design._archive_raw never opened a tar at all — this test "
        "asserts where it writes, so it proves nothing if it did not write")
    assert os.path.abspath(dest) not in opened, (
        "esmfold2_design._archive_raw wrote the tar directly to dest "
        f"({dest}); it must stage the write elsewhere and move the finished "
        "file into place, because it has no handler to remove a partial")
    assert not dest.exists(), (
        "esmfold2_design._archive_raw left something at dest after a failed "
        "write; the wrapper would park it as if it were a whole archive")
    assert not any(os.path.exists(p) for p in opened), (
        "the truncated staging tar outlived the call; the finally must rmtree "
        f"the staging dir. Still present: {[p for p in opened if os.path.exists(p)]}")


def test_esmfold2_design_stages_inside_dests_own_directory(tmp_path, monkeypatch):
    """WHERE it stages, not just that it stages elsewhere than dest.

    The test above is satisfied by any staging location, so deleting
    ``dir=os.path.dirname(dest_abs) or None`` from the mkdtemp call left the
    whole suite green — measured. What that argument buys is that ``shutil.move``
    is a rename rather than a cross-filesystem copy2 that an interruption can
    truncate AT dest, and esmfold2_design is the one copy with no handler to
    remove such a partial. Filesystem identity is not directly assertable, so
    assert the mechanism the code uses to obtain it: the tar is opened inside a
    directory that is a child of dest's own directory.

    ``tempfile.tempdir`` is pointed somewhere else first, so an unpinned mkdtemp
    lands there and this fails. Set on the module rather than through
    TMPDIR/TEMP/TMP, which ``gettempdir()`` caches after its first read.
    """
    cap = _BY_TOOL["esmfold2_design"]
    src = _work_tree(tmp_path)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    dest = dest_dir / "raw.tgz"
    monkeypatch.setattr(cap.module, "RAW_ARCHIVE_PATH", str(dest))

    ambient = tmp_path / "ambient_tmp"
    ambient.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(ambient))

    real_open = tarfile.open
    opened: list[str] = []

    def recording_open(name, mode="r", *args, **kwargs):
        opened.append(os.path.abspath(str(name)))
        # Delegate: the capture must actually complete, or where it staged
        # proves nothing about the move that follows.
        return real_open(name, mode, *args, **kwargs)

    monkeypatch.setattr(tarfile, "open", recording_open)

    cap.call(cap.func, src)

    assert dest.is_file(), (
        "esmfold2_design._archive_raw did not deliver the archive at all, so "
        "this test has nothing to say about where it staged it")
    assert opened, "no tar was opened; nothing to locate"
    want = os.path.dirname(os.path.abspath(str(dest)))
    for staged in opened:
        staged_dir = os.path.dirname(staged)
        assert os.path.dirname(staged_dir) == want, (
            f"esmfold2_design._archive_raw staged its tar in {staged_dir}, "
            f"which is not a child of dest's own directory ({want}). Without "
            "the ``dir=`` pin the staging follows TMPDIR/TEMP/TMP — here "
            f"{ambient} — and shutil.move can then degrade to a copy2 that "
            "truncates at dest, which this copy has no handler to clean up")
        assert not os.path.exists(staged_dir), (
            f"the staging dir {staged_dir} outlived the call; the finally must "
            "rmtree it, and it now sits in the same directory as the archive")
