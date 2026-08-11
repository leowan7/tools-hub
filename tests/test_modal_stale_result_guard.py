"""Every Modal wrapper that READS ``/tmp/smoke_results.json`` must UNCONDITIONALLY
REMOVE it BEFORE it spawns the pipeline.

THE BUG THIS FILE EXISTS TO PIN. Modal reuses warm containers. ``run_pipeline.py``
writes its verdict to a fixed path in ``/tmp``, and the wrapper reads that path
back after the subprocess returns. If the pipeline dies before writing its own
file — early import error, OOM kill, SIGKILL, any uncaught exception — a wrapper
that did not clear the path first reads the PREVIOUS job's file.
``gpu/modal_client.py``'s ``_interpret_pipeline_return()`` then branches on
``smoke["status"] == "COMPLETED"`` with **no exit_code gate** and copies that
dict's designs / sequences / candidates through as this job's result. The
customer is handed another run's output under a ``succeeded`` job.

HOW IT IS CHECKED: TEMPLATE MATCHING, NOT ANALYSIS. There are exactly nine
``tools/*/modal_app.py`` files, all in this repo, all written by us, and between
them they use exactly TWO spellings of the clear. So this file does not reason
about what a wrapper's code means. For each TOP-LEVEL statement of a reading
function it produces one canonical string (``_normalise``) and compares it, byte
for byte, against a short list of accepted forms (``_TEMPLATES``). A statement
that is not character-identical to one of those forms is NOT a clear. Nothing is
inferred, and nothing else counts. The previous version tried to be general
instead — resolving module constants, tracking rebindings, walking reachability,
peeling wrappers, allow-listing ancestors — and four review rounds each found a
NEW way to slip an unsound ACCEPT through that machinery while its measured
mutation kill rate FELL from 79% to 59% and it grew from 180 to 1987 lines. Nine
known files with two known shapes do not need an analyser; they need a list.

ORDERING IS ON STATEMENT INDEX, NOT LINE NUMBER. Clear, spawn and read are each
identified by the INDEX of the top-level statement of the reading function that
contains them, and the rule is ``clear_index < spawn_index < read_index``. Line
numbers used to be the anchor, and a clear tucked into the ``finally:`` of the
spawn's OWN ``try:`` compared as "before the spawn" because the ``try:`` header's
line precedes the ``subprocess.run`` line. On statement indices that comparison
cannot be written: the clear and the spawn are then the same statement.

WHAT THIS DELIBERATELY NO LONGER ACCEPTS. Shrinking the accepted set is the
point. Every spelling below was understood by the old analyser, is now a LOUD
rejection, and is used by NONE of the nine wrappers: ``os.unlink``, ``from os
import remove``, ``Path(...).unlink()``, ``io.open`` / ``codecs.open`` /
``os.open`` readers, paths built with f-strings, ``+`` or ``os.path.join``, and
module constants (``_SMOKE = "/tmp/..."`` then ``os.remove(_SMOKE)``). The trade
is explicit: FALSE REJECTIONS GO UP — several correct wrappers would now be told
to change — and what is bought is narrow enough to be worth stating exactly. NO
STATEMENT IS ACCEPTED AS THE CLEAR UNLESS ITS NORMALISED TEXT IS IDENTICAL TO A
FORM READ OFF A REAL WRAPPER AND CHECKED BY HAND. That is the whole of it. An
earlier revision of this paragraph went one step further and said false
ACCEPTANCES GO TO ZERO for the clear; that is FALSE, and the rest of this
docstring refutes it twenty lines down. Textual identity is a property of the
text, not of what runs. Normalisation ERASES executable code, so the erased part
can start the pipeline; a display tail that RAISES means the removal never runs
at all; an ``@lru_cache`` above the ``def`` stops the whole body after the first
call; and a statement is accepted as the clear even when an unrecognised start on
an EARLIER statement has already launched the pipeline — measured for ``os.popen``
before it entered ``_SPAWN_CALLS``: ACCEPTED, zero failures. Each of those is
written out below and, where it is still live, pinned in ``_DISCLOSED_GAPS``.
``_UNSUPPORTED_SHAPES`` holds the cost of the rejections, visible and executable.

AN UNRECOGNISED PROCESS START IS ONLY LOUD WHEN IT IS THE ONLY ONE. The
symmetric claim used to be made for spawns — ``import subprocess as sp``,
``_RUN = subprocess.run``, ``os.system``, ``os.exec*``, ``os.spawn*``,
``multiprocessing`` — and it is not true. ``_order_failure``'s own wording is
the honest one: the rule it enforces is "the clear precedes every process start
THIS FILE RECOGNISES here". An unrecognised start reaches the loud "makes no
call this file recognises as starting a process" only when it is the SOLE start
in its function. Put one before the clear and leave any recognised
``subprocess.run`` after it, and the anchors read clear@1 spawn@2 read@3 — a
pass, with the pipeline already launched at statement 0. Measured, and pinned as
``unrecognised_start_before_the_clear_beside_a_recognised_one``. It is worse
inside text that normalisation erases: the guard that stops a statement holding a
spawn from counting as the clear keys on ``_is_spawn``, so an UNRECOGNISED start
parked in a ``for``-display tail — evaluated before iteration 1 — started the
pipeline while the same statement was accepted as the clear
(``unrecognised_start_in_the_erased_display_tail``). ``os.system``,
``multiprocessing.Process``, ``subprocess.getoutput``,
``subprocess.getstatusoutput`` and ``os.popen`` were moved into ``_SPAWN_CALLS``
for those two reasons. WHAT IS LEFT OVER IS AN OPEN SET, NOT A LIST, and writing
it as though it were a list is what made omitting ``subprocess.getoutput`` — and
then ``os.popen`` a round later — look free. Unrecognised, and silent in that
combination: ``os.exec*``, ``os.spawn*``, ``os.posix_spawn``,
``asyncio.create_subprocess_exec`` / ``create_subprocess_shell`` (named here as a
DECISION rather than left to be found as the next oversight — see ``_SPAWN_CALLS``
for why the trade was declined), any aliased or
rebound ``subprocess`` call, a spawn hoisted into a HELPER (``def _go():
subprocess.run(...)`` then ``_go()`` — ``_owner`` charges the call to ``_go``, so
the caller never sees it; measured ACCEPTED, and pinned in ``_DISCLOSED_GAPS``),
and every spelling nobody has thought of yet. ADDING A NAME THERE IS A TRADE, NOT
A TIGHTENING, and an earlier revision of this file claimed otherwise: recognising
a call can SUPPLY a spawn anchor where a function had none, moving it out of the
loud no-recognised-start backstop into a clean ``clear < spawn < read`` pass.
Measured both directions for every name in the set; the arithmetic is written out
at ``_SPAWN_CALLS`` and must be re-measured by anyone adding another.

WHAT THIS FILE STILL CANNOT SEE. It matches the TEXT ``os.remove`` and
``subprocess.run``; it does not resolve names, so a module that rebound ``os`` or
``subprocess`` would defeat it. It also cannot follow a clear hoisted into a
helper — proving a helper CONTAINS a removal is not proving that CALLING it
removes anything (a once-guard, an ``@lru_cache``, or a post-``def`` rebinding
each make the call a no-op on every warm container after the first, which is
exactly the bug). Helper-based clears are rejected with a message that says that,
rather than claiming the removal is not there.

Four more, none of them rejectable by a matcher this size:

* A STALE FILE PUT BACK AFTER THE CLEAR. The clear is textbook — it matches a
  template, it is first, it runs — and a later statement RESTORES the results
  file before the pipeline is spawned::

      try: os.remove('/tmp/smoke_results.json')   # 0 matches the template
      except FileNotFoundError: pass
      shutil.unpack_archive(payload["bundle"], "/tmp")   # 1 puts one back
      result = subprocess.run(cmd)                       # 2
      with open('/tmp/smoke_results.json') as fh: ...    # 3

  Anchors clear@0 spawn@2 read@3, so it passes; measured as written above,
  through the same ``_analyze`` the nine wrappers go through: ACCEPTED, zero
  failures. It reproduces the production bug in full — the read returns a file
  this job did not write, on a warm container and a cold one alike. Rejecting it
  would mean deciding which of the statements between the clear and the spawn
  can create a file, which is exactly the general analysis this file is a
  deliberate retreat from: ``shutil.unpack_archive``, ``shutil.copy``,
  ``tarfile``, ``zipfile``, a ``download()`` helper, a mounted volume, and any
  of them behind a name this file cannot resolve. The cheapest instance is not
  even exotic — ``open('/tmp/smoke_results.json', 'w')``, which names the exact
  literal this file matches on, is accepted too, though what the recreated file
  CONTAINS is what decides whether that acceptance becomes a leak: the case as
  written puts ``{}`` there and surfaces as an error, while a COMPLETED dict
  there leaks in full. Disclosed, not closed, and both spellings now run in
  ``_DISCLOSED_GAPS`` so the word "measured" here stays true rather than becoming
  a thing someone once ran — see that dict's header for which of its cases were
  executed into a leak and which were not.
* A DECORATOR ON THE READING FUNCTION ITSELF. Only ``func.body`` is read, so a
  ``@lru_cache`` above the ``def`` is invisible, and it reproduces the bug in
  full without touching a single statement this file inspects: executed, the
  body ran ONCE for two calls and the second call was handed the first call's
  dict. The clear is there, it is inline, it is first, and it stops running.
  Same mechanism as the once-guard named above for helpers, moved up one level.
* A DISPLAY TAIL THAT RAISES. Rewrite 3 truncates a display to its first
  element; the tail still evaluates, and evaluates FIRST. A tail element that
  raises means iteration 1 — the removal — never runs at all (executed: ZERO
  iterations for ``('/tmp/smoke_results.json', 1 // 0)``). A RECOGNISED spawn or
  read in the tail is caught, because ``_analyze`` looks at the whole statement.
  Recognised is the load-bearing word and it was missing here for a round: the
  guard keys on ``_is_spawn``, so an unrecognised start in the tail was invisible
  — that was live for ``subprocess.getoutput`` until it was added to
  ``_SPAWN_CALLS``, and it is still live for every start that set does not name.
  A tail that merely RAISES is not catchable at all.
* WHETHER ANY OF IT RUNS. There is no reachability analysis: an early
  ``return``, a ``sys.exit`` or a raise above the clear leaves the clear
  matching and the ordering satisfied.

WHY SOURCE-LEVEL, AND WHY THE SYNTHETIC CASES. ``run_tool`` is wrapped in
``@app.function(...)``, so it is a ``modal.Function`` object that cannot be
called in-process, and its body shells out to a GPU pipeline; the AST is the only
honest way to reach it. And a pattern-matcher that stops matching fails OPEN — it
reports green — so ``_analyze()`` is applied to the inline synthetic modules at
the bottom of this file as well as to the nine real wrappers. Those cases are the
only thing standing between a future "simplification" and a silent return to
green.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import NamedTuple

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_RESULTS_PATH = "/tmp/smoke_results.json"

# Every string constant that is NOT the results path is rewritten to this before
# comparison, so a log message's wording is not part of a template. A wrapper
# that literally contains "<elided>" is rewritten to "<elided>" as well, so no
# spelling survives normalisation into a position the templates read as the path.
_ELIDED = "<elided>"

# Pinned by hand. Every one of these nine spawns a pipeline subprocess and reads
# its verdict back. This is a FILE-SET pin, not a text search: see
# ``test_the_wrapper_file_set_is_exactly_the_pinned_nine``.
_EXPECTED_TOOLS = frozenset(
    {
        "af2",
        "boltz2",
        "colabfold",
        "esmfold",
        "esmfold2_design",
        "iggm",
        "mpnn",
        "opendde",
        "proteina",
    }
)


# --------------------------------------------------------------------------- #
# Normalisation: one statement in, one canonical string out.
# --------------------------------------------------------------------------- #


class _Canonicalise(ast.NodeTransformer):
    """The only three rewrites this file performs. Each is purely syntactic.

    1. An f-string becomes the placeholder. This does not merely narrow — it is
       what makes the wrappers match AT ALL. All nine log their OSError arm with
       an f-string, so collapsing it turns a NON-match into a match for every one
       of them; measured, deleting this method leaves all nine with no recognised
       clear and turns 22 tests red (22 failed, 82 passed, the whole file — the
       22 being all nine wrappers on BOTH per-reader checks, plus the three
       template-shaped good cases and the template/spawn equivalence test,
       re-measured and re-counted by name). Either number moves whenever a test
       is added or removed — the pass count has, four times, most recently 80 to
       82 — and the
       load-bearing half of the claim is the other one, that all nine stop
       matching, so re-measure rather than trusting the number. The direction
       that matters is
       therefore the opposite of narrowing, and the cost is that this rewrite
       DELETES EXECUTABLE CODE: everything inside ``{...}`` disappears from the
       compared text and still runs. That is the channel a spawn or a read hides
       in, which is why ``_analyze`` refuses to treat a statement containing
       either as a clear no matter how it normalises.
    2. A string constant that is not EXACTLY the results path becomes the
       placeholder, so a log message's wording is not part of a template. The
       results path survives untouched — that is the whole of the "fold the path
       expression to the literal" step, because these nine wrappers write the
       path as that literal everywhere it matters.
    3. A ``for`` whose iterable is a TUPLE OR LIST DISPLAY beginning with the
       results-path literal is truncated to that first element, because only
       iteration 1 can be reasoned about from a display. The tail is NOT inert.
       The whole display is built before iteration 1, so an element AFTER the
       first runs BEFORE the loop body (executed: a call in the tail logged
       ahead of the body), and one that raises means iteration 1 never runs at
       all (executed: ``for x in ('/tmp/smoke_results.json', 1 // 0)`` performs
       ZERO iterations). This rewrite deletes executable code exactly as rewrite
       1 does; ``_analyze`` covers the part of that it can see — a recognised
       spawn or read in the tail — and a tail that merely RAISES is a residual
       gap, listed under WHAT THIS FILE STILL CANNOT SEE. Templates that use
       this form fix the loop body exactly, so iteration 1 provably reaches the
       removal — an accepted body has no ``break``, ``continue``, ``raise`` or
       ``return`` in it. A set display is NOT truncated: its iteration order is
       not its source order, so "the first element" would mean nothing.
    """

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        return ast.Constant(value=_ELIDED)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str) and node.value != _RESULTS_PATH:
            return ast.Constant(value=_ELIDED)
        return node

    def visit_For(self, node: ast.For) -> ast.AST:
        self.generic_visit(node)
        iterable = node.iter
        if isinstance(iterable, (ast.Tuple, ast.List)) and iterable.elts:
            first = iterable.elts[0]
            if isinstance(first, ast.Constant) and first.value == _RESULTS_PATH:
                node.iter = ast.Tuple(elts=[first], ctx=ast.Load())
        return node


def _normalise(stmt: ast.stmt) -> str:
    """The canonical text of one statement, for byte-for-byte template comparison."""
    clone = _Canonicalise().visit(copy.deepcopy(stmt))
    return ast.unparse(ast.fix_missing_locations(clone))


# --------------------------------------------------------------------------- #
# The accepted forms. THIS LIST IS THE WHOLE MATCHER.
#
# Each entry was read off a real wrapper (or, where noted, is a strict
# simplification of one) and checked BY HAND to unconditionally ATTEMPT the
# removal of the results path on every call. Attempt, not outcome: template A
# swallows a non-FileNotFoundError OSError, so a path that exists and cannot be
# deleted leaves the stale file in place and lets the job continue. That mirrors
# what all nine wrappers ship and is deliberate; an undeletable /tmp is not the
# failure this file exists to catch. They are literal strings rather than snippets fed back
# through ``_normalise`` on purpose: if the normaliser drifts, the nine real
# wrappers stop matching and the suite goes red, instead of the templates
# silently drifting along with it.
#
# ``ast.unparse`` formatting is part of the contract — 4-space indent, single
# quotes. A Python upgrade that changes it breaks all nine at once, which is the
# safe direction to fail in.
# --------------------------------------------------------------------------- #

_TEMPLATES: dict[str, str] = {
    # af2, boltz2, esmfold, esmfold2_design, iggm, mpnn, opendde, proteina —
    # eight of the nine, character for character after normalisation.
    "try/remove/except-FileNotFoundError/except-OSError-print": (
        "try:\n"
        "    os.remove('/tmp/smoke_results.json')\n"
        "except FileNotFoundError:\n"
        "    pass\n"
        "except OSError as exc:\n"
        "    print('<elided>', flush=True)"
    ),
    # colabfold, which clears the results file and the raw archive in one loop.
    # Rewrite 3 truncates the display, so what is pinned is: results path FIRST,
    # and a body that provably completes iteration 1.
    "for-over-a-display/try/remove/except-FileNotFoundError/except-OSError-print": (
        "for _stale in ('/tmp/smoke_results.json',):\n"
        "    try:\n"
        "        os.remove(_stale)\n"
        "    except FileNotFoundError:\n"
        "        pass\n"
        "    except OSError as exc:\n"
        "        print('<elided>', flush=True)"
    ),
    # NOT taken from any wrapper: the first template with the diagnostic arm
    # removed. Listed because dropping a ``print`` is the most likely benign edit
    # to the form eight wrappers use, and because it is unconditional by
    # inspection — the removal is still ATTEMPTED on every call and a missing
    # file is still swallowed. Verified by hand, not by machine.
    "try/remove/except-FileNotFoundError-only": (
        "try:\n"
        "    os.remove('/tmp/smoke_results.json')\n"
        "except FileNotFoundError:\n"
        "    pass"
    ),
}


def _match_template(text: str) -> str | None:
    """The name of the template ``text`` IS, or None. Exact equality, nothing else."""
    return next((name for name, t in _TEMPLATES.items() if t == text), None)


# --------------------------------------------------------------------------- #
# Recognised reads and recognised spawns.
#
# Both sets are deliberately tiny. AN EARLIER REVISION OF THIS HEADER CLAIMED
# "neither can turn into a silent pass". BOTH HALVES OF THAT ARE FALSE, and the
# heading 250 lines above already says so — AN UNRECOGNISED PROCESS START IS ONLY
# LOUD WHEN IT IS THE ONLY ONE. Every other passage was corrected; this one was
# missed. What is actually true:
#
#   An unrecognised SPAWN is loud — "makes no call this file recognises as
#   starting a process" — only when it is the SOLE start in its function. Beside
#   any recognised start it is invisible, and invisible is the production shape:
#   the pipeline goes out through the unrecognised call at statement 0 while the
#   anchors read clear@1 spawn@2 read@3, a clean pass.
#
#   An unrecognised READ is loud — via ``test_every_wrapper_has_a_recognised_
#   reader`` — only when NO function in the whole wrapper is a recognised reader,
#   or when the function naming the path is one ``_functions_naming_the_path``
#   can see. A SECOND reader in an already-covered wrapper, spelled some other
#   way, is silent: zero ``_Reader``s and therefore zero tests, while the first
#   function keeps the tool off the blind list. That is why that check resolves
#   module constants as well as the literal, and it still does not see a
#   concatenation or an imported name.
#
# Both are disclosed, both are exercised by cases below, and neither is closed.
# --------------------------------------------------------------------------- #

_SPAWN_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        # WIDENING THIS SET IS A TRADE, NOT A TIGHTENING. Read this before adding
        # a name. An earlier revision of this comment claimed the four names
        # below could only ever cause MORE rejections. That is false, and it was
        # measured false by running the same sources through the analyser with
        # and without them:
        #
        #   os.system BEFORE the clear, recognised subprocess.run after
        #                                     without: ACCEPT   with: REJECT
        #   aliased sp.run start, benign os.system AFTER the clear
        #                                     without: REJECT   with: ACCEPT
        #   aliased sp.run start, benign multiprocessing.Process after the clear
        #                                     without: REJECT   with: ACCEPT
        #
        # BOTH DIRECTIONS COME FROM THE SAME MECHANISM. A function whose only
        # process start is unrecognised has spawn_index None, and that hits the
        # loud "makes no call this file recognises as starting a process"
        # backstop in ``_order_failure``. Recognising one more name can SUPPLY an
        # anchor where there was none — and if the newly recognised call happens
        # to sit after the clear, the function moves out of that backstop into a
        # clean clear < spawn < read pass. So each name here:
        #
        #   CLOSES  an unrecognised start placed BEFORE the clear beside a
        #           recognised one (silent accept; the pipeline is already
        #           running at statement 0 while the anchors read 1/2/3), and
        #   OPENS   a function started by a still-unrecognised call that has a
        #           benign occurrence of the newly recognised name AFTER the
        #           clear (was a loud reject via the backstop, now silent).
        #
        # The trade was taken because the first case is the production bug and
        # the second needs an unrecognised start to exist in the first place.
        # None of these names appears in any of the nine wrappers, so the whole
        # cost is borne by hypothetical code either way. If you add another name,
        # you are making this trade again — MEASURE IT IN BOTH DIRECTIONS, write
        # the result here, and do not describe it as a tightening.
        #
        # os.system / multiprocessing.Process: the two commonest non-subprocess
        # starts. What is matched for multiprocessing is the CONSTRUCTOR, not the
        # ``.start()``: ``_dotted`` cannot name a method call on a call. That is
        # the conservative direction — it rejects a Process that is built and
        # never started, and never accepts one that is started.
        "os.system",
        "multiprocessing.Process",
        # subprocess.getoutput / getstatusoutput: unaliased, dotted, public
        # ``subprocess`` API that starts a process, and they were in neither the
        # recognised set nor the list of deliberately unrecognised spellings —
        # recognising the other five ``subprocess.*`` names and not these two was
        # an oversight, not a decision. Besides the before-the-clear case above,
        # they defeated the erasure guard: ``_analyze`` refuses to count a
        # statement as a clear when it contains a recognised spawn or read, so an
        # UNRECOGNISED start hidden in text that normalisation erases was
        # invisible. A ``for``-display tail is evaluated before iteration 1, so
        # ``for _stale in ("/tmp/smoke_results.json", subprocess.getoutput(...)):``
        # launched the pipeline before ``os.remove`` ran and the whole statement
        # still counted as the clear. Pinned as
        # ``unrecognised_start_in_the_erased_display_tail``.
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        # os.popen: the exact twin of subprocess.getoutput one module over. It is
        # unaliased, dotted, public, and it starts a process; it was in neither
        # the recognised set nor the list of deliberately unrecognised spellings,
        # so it is the same oversight getoutput was, one round later. That is the
        # cost of writing "what remains unrecognised" as a list that reads
        # complete — the criterion above applies to os.popen word for word.
        #
        # THE TRADE, re-measured for this name by running the same sources
        # through _analyze with and without it in this set:
        #
        #   os.popen BEFORE the clear, recognised subprocess.run after
        #                                     without: ACCEPT   with: REJECT
        #   os.popen in a for-display TAIL — evaluated before iteration 1 — with
        #   the statement still normalising onto template B
        #                                     without: ACCEPT   with: REJECT
        #   aliased sp.run start, benign os.popen AFTER the clear
        #                                     without: REJECT   with: ACCEPT
        #
        # Same arithmetic as the four above and taken for the same reason: two
        # silent accepts closed, one loud reject turned silent, and the opened
        # case needs a start this file still cannot read to exist at all.
        "os.popen",
        # DELIBERATELY NOT ADDED — and written here so it is a DECISION rather
        # than the oversight `subprocess.getoutput` and then `os.popen` each
        # were: ``asyncio.create_subprocess_exec`` and
        # ``asyncio.create_subprocess_shell``. They meet the criterion used to
        # justify `os.popen` word for word — unaliased, dotted, public API that
        # starts a process, in neither list — and ``_analyze`` already models
        # ``AsyncFunctionDef``, so the pinning case would work today. What is
        # missing is a reason to make the trade above a third time: NONE of the
        # nine wrappers is async or so much as mentions asyncio (checked), and
        # the direction a new name OPENS needs a still-unrecognised start to
        # exist before it costs anything. So the trade would buy a hypothetical
        # and pay in a hypothetical. If a wrapper ever goes async, add both
        # names, measure the trade in both directions and write the arithmetic
        # here — ``test_every_recognised_spawn_name_is_load_bearing`` then pins
        # them for free. Until then they are unrecognised, and an async wrapper
        # whose ONLY start is one of them is loud, not silent.
    }
)


# The same names again, pinned BY HAND, exactly as ``_EXPECTED_TOOLS`` pins the
# nine wrapper directories. It is duplication on purpose.
#
# WHY IT IS NOT DERIVED FROM ``_SPAWN_CALLS``. Deriving it was written first and
# is VACUOUS, which was caught by running the mutation it exists to kill. The
# mutation is "drop a name from _SPAWN_CALLS", and a parametrisation that reads
# _SPAWN_CALLS loses the case in the very edit that loses the name: measured,
# dropping each of the four previously unpinned names from a derived
# parametrisation left the file GREEN at 100 passed — one fewer test, no
# failures, the same silence the derived version was written to end. A pin that
# reads the thing it pins cannot fail. That argument is enforced, not merely
# argued: ``test_the_pinned_spawn_names_are_written_out_and_not_derived`` reads
# this file's own source and fails if the right-hand side below stops being a
# literal. Without it the whole paragraph is a comment, and reverting to
# ``tuple(sorted(_SPAWN_CALLS))`` is a one-line edit nothing goes red for.
#
# Held to EQUALITY by ``test_the_pinned_spawn_names_are_exactly__SPAWN_CALLS``,
# which catches a ONE-SIDED edit in either direction: a name dropped from
# _SPAWN_CALLS goes red here AND in its own case, and a name added to
# _SPAWN_CALLS alone goes red here. WHAT IT DOES NOT CATCH is a name added to
# BOTH lists — that generates its own case and the case passes, which is the
# intended shape, since the case is then what proves the name load-bearing. Nor
# does anything catch a name DROPPED from both lists in the same edit — the
# equality still holds and the case goes with the name. Measured residual,
# disclosed and not closed.
_PINNED_SPAWN_NAMES = (
    "multiprocessing.Process",
    "os.popen",
    "os.system",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "subprocess.run",
)


def _dotted(node: ast.AST) -> str | None:
    """``"a.b.c"`` for a Name/Attribute chain, ``None`` for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_read(node: ast.AST) -> bool:
    """``open('/tmp/smoke_results.json')`` — that literal, one arg, no keywords.

    All nine wrappers spell the read exactly that way. A mode argument, an
    ``encoding=`` keyword, ``io.open`` / ``codecs.open`` / ``os.open`` and
    ``Path(...).read_text()`` are all unrecognised — which is also why
    ``open(path, 'w')``, truncating instead of removing, is correctly not
    counted as a read.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _RESULTS_PATH
    )


def _is_spawn(node: ast.AST) -> bool:
    """A call whose dotted name is literally one of ``_SPAWN_CALLS``.

    Textual, like everything else here: ``_dotted`` reads the Name/Attribute
    chain as written, so an alias or a rebinding is not recognised. See
    ``_SPAWN_CALLS`` for why widening that set is a trade in both directions.
    """
    return isinstance(node, ast.Call) and _dotted(node.func) in _SPAWN_CALLS


def _is_removal_attempt(stmt: ast.stmt, text: str) -> bool:
    """Does this statement LOOK like an attempt to clear the file?

    Diagnostics only — it never grants acceptance. It decides what gets quoted
    back at an author whose clear matched no template, so the rejection can show
    the normalised text of the thing that nearly counted.
    """
    if _RESULTS_PATH in text:
        return True
    for node in ast.walk(stmt):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name) else None
        )
        if name in {"remove", "unlink"}:
            return True
    return False


# --------------------------------------------------------------------------- #
# Whole-module analysis: which statement index clears, spawns, reads.
# --------------------------------------------------------------------------- #


class _Reader(NamedTuple):
    """One function that reads the results file, anchored on statement indices."""

    label: str  # repo-relative path, or a synthetic case name
    tool: str
    func: str
    read_index: int  # EARLIEST reading statement
    spawn_index: int | None  # EARLIEST spawning statement
    spawn_count: int
    clear_index: int | None  # FIRST statement matching a template
    clear_template: str | None
    near_misses: tuple[tuple[int, str], ...]  # (index, normalised text)


def _owner(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The INNERMOST enclosing function of ``node``, or None at module level.

    Applied to reads AND to spawns, identically. Attributing a nested function's
    read to its parent charged one function's anchors against another's;
    attributing a nested function's SPAWN to its parent failed correct code,
    because a helper ``def`` sitting above the clear made the parent look like it
    had already shelled out.
    """
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
    return None


def _analyze(source: str, label: str, tool: str) -> list[_Reader]:
    """Every reading function in one module, with its clear/spawn/read indices."""
    tree = ast.parse(source, filename=label)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    out: list[_Reader] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        reads: list[int] = []
        spawns: list[int] = []
        clear_index: int | None = None
        clear_template: str | None = None
        near_misses: list[tuple[int, str]] = []

        # ``enumerate(func.body)`` — so ``reads`` and ``spawns`` are built in
        # strictly ascending index order, and every index recorded is a TOP-LEVEL
        # statement of this function.
        for index, stmt in enumerate(func.body):
            owned = [n for n in ast.walk(stmt) if _owner(n, parents) is func]
            is_reader = any(_is_read(n) for n in owned)
            if is_reader:
                reads.append(index)
            spawns_here = [n for n in owned if _is_spawn(n)]
            spawns.extend(index for _ in spawns_here)

            text = _normalise(stmt)
            match = _match_template(text)
            # NORMALISATION ERASES EXECUTABLE CODE, so matching a template is not
            # on its own evidence that the statement only removes a file.
            # ``visit_JoinedStr`` deletes everything inside ``{...}`` and
            # ``visit_For`` deletes display elements 2..n, and the deleted text
            # still RUNS — a display's elements are all evaluated before
            # iteration 1, so a spawn parked in the tail starts the pipeline
            # before ``os.remove`` is reached at all. A statement whose subtree
            # contains a recognised spawn or a recognised read is therefore not a
            # clear, however it normalises. This is also what makes ``clear <
            # spawn`` and ``clear <= spawn`` the same rule: with it,
            # ``clear_index == spawn_index`` is unreachable. Pinned by
            # ``test_no_accepted_template_can_contain_a_spawn_or_a_read``.
            if match is not None and (is_reader or spawns_here):
                match = None
            if match is not None and clear_index is None:
                clear_index, clear_template = index, match
            elif match is None and not is_reader and _is_removal_attempt(stmt, text):
                near_misses.append((index, text))

        if not reads:
            continue
        out.append(
            _Reader(
                label=label,
                tool=tool,
                func=func.name,
                read_index=min(reads),
                spawn_index=min(spawns) if spawns else None,
                spawn_count=len(spawns),
                clear_index=clear_index,
                clear_template=clear_template,
                near_misses=tuple(near_misses),
            )
        )
    out.sort(key=lambda r: (r.label, r.func))
    return out


def _names_assigned_the_path(tree: ast.Module) -> frozenset[str]:
    """Names assigned the results-path LITERAL — ``_SMOKE = "/tmp/..."``.

    ``Assign`` AND ``AnnAssign`` (``_SMOKE: str = "/tmp/..."``), found by walking
    the whole module rather than reading ``tree.body``.

    THE GAP THAT BOUGHT BOTH WIDENINGS. This used to accept only a bare
    module-level ``ast.Assign``, so adding ``: str`` to a constant — or tucking
    one inside a module-level ``if:`` / ``try:``, which is how an optional import
    gets a fallback — put its reader straight back into the blind spot the
    constant resolution exists to close. Silently: a second reader that is
    neither modelled by ``_is_read`` nor listed here gets ZERO tests, and zero
    tests is reported as a pass. Both spellings are pinned by
    ``test_a_second_reader_spelled_with_a_module_constant_is_not_invisible``.

    STILL NOT SEEN, and deliberately not written as a closed list: a TUPLE target
    (``_A, _B = path, path``), whose value node is a ``Tuple`` and not the
    literal, and a walrus (``(_SMOKE := "/tmp/...")``), which is a ``NamedExpr``
    and not an assignment statement at all.

    ``ast.walk`` does not stop at module level, so a name bound INSIDE a function
    is treated as an alias everywhere. That over-widens on purpose, and it is the
    safe direction — the same argument that covers not tracking a later
    rebinding: an extra name here can only ever put a function ON the list that
    makes a test FAIL, which is loud and gets resolved by hand. Measured over the
    nine real wrappers, the widening adds nothing at all — none of them binds the
    literal to a name in any spelling. Missing a name is the silent direction,
    and is the only direction this exists to move.
    """
    names: set[str] = set()
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if isinstance(value, ast.Constant) and value.value == _RESULTS_PATH:
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return frozenset(names)


def _functions_naming_the_path(source: str, label: str) -> list[str]:
    """Functions that name the results path as their own — as the LITERAL, or as
    a module-level constant assigned that literal.

    Attribution is identical to ``_analyze``'s — innermost function wins, via the
    same ``_owner`` — so a nested function's mention is not charged to its
    parent.

    Used ONLY to ask "is every function that clearly touches this path modelled?"
    — never to grant acceptance to anything.

    WHY THIS RESOLVES A CONSTANT WHEN ``_is_read`` REFUSES TO. The asymmetry is
    deliberate, not a slip, and it is the whole reason this widening is not the
    general analyser this file retreated from. ``_is_read`` grants a function a
    MODEL — anchors that then decide a pass — so a name it resolved wrongly would
    hand out an ACCEPT. This function grants nothing, and NO consumer of it can:
    ``_blind_functions``, where everything on the returned list is a FAILURE, and
    the equality assertions on it in the synthetic second-reader cases, each an
    ``==`` in a test and so able only to fail. (An earlier revision of this
    paragraph said "its only consumer is ``_blind_functions``"; the revision after
    it said "TWO consumers". Both were counts of call sites, and both went stale
    the next time one was added — the asymmetry argument is what survives, not the
    count.) Widening
    a check that is incapable of accepting anything moves in one direction only.

    IT WAS MEASURED SILENT WITHOUT THIS, which is what bought the change. Append
    to any already-covered wrapper a second reader spelled ``_P =
    "/tmp/smoke_results.json"`` then ``open(_P)``, with NO clear, and it got zero
    tests: not a ``_Reader``, because ``_is_read`` wants the literal, and not on
    the blind list either, because this function wanted the literal too — while
    the original ``run_tool`` kept the tool off the per-TOOL list. Both halves of
    the check missed the same function. Pinned by
    ``test_a_second_reader_spelled_with_a_module_constant_is_not_invisible``.

    STILL NOT SEEN, and this is deliberately not written as a closed list: a path
    built by concatenation (``"/tmp/" + "smoke" + "_results.json"``), an f-string,
    ``os.path.join``, an attribute (``paths.SMOKE``), a name imported from
    another module, or a name bound by a spelling ``_names_assigned_the_path``
    does not read — a tuple target or a walrus; see there. The corpus in
    ``test_discovery_is_the_glob_and_not_a_text_search`` is exactly the
    concatenation case; it is caught only by the per-TOOL half, which is why that
    half stays.
    """
    tree = ast.parse(source, filename=label)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    aliases = _names_assigned_the_path(tree)

    def names_it(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant):
            return node.value == _RESULTS_PATH
        return isinstance(node, ast.Name) and node.id in aliases

    out: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            names_it(node) and _owner(node, parents) is func
            for node in ast.walk(func)
        ):
            out.append(func.name)
    return sorted(out)


def _blind_functions(
    sources: list[tuple[str, Path | None, str]], readers: list[_Reader]
) -> list[str]:
    """``tool:func`` for every path-naming function that is NOT a modelled reader.

    Factored out of ``test_every_wrapper_has_a_recognised_reader`` so a synthetic
    module can be pushed through the same code the nine real wrappers are. Inline
    it had no executable claim at all: replacing it with ``[]``
    left the whole suite green, because no module the suite looked at has a
    second path-naming function. Same defect as the one round 7 fixed for
    ``_order_failure`` — a comment arguing for itself with nothing running it.

    PAIRED BY COUNT, NOT BY NAME. This used to ask ``any(r.func == name)``, which
    filtered on the bare NAME, so a second path-naming function spelled the same
    as a modelled reader was hidden by it — a method, a nested def, a conditional
    redefinition::

        def run_tool(payload): ...           # guarded, modelled
        class _Legacy:
            def run_tool(self, payload):     # NO CLEAR, unmodelled read
                subprocess.run(cmd)
                with open(_SMOKE) as fh: ...

    Measured: zero per-reader failures, ``blind_funcs == []``, accepted SILENT —
    and loud again the moment the method was renamed, so the collision was the
    whole of it. Every modelled reader necessarily names the path (``_is_read``
    wants the literal, which ``_functions_naming_the_path`` also sees), so one
    modelled reader claims exactly ONE path-naming function of that name and any
    further one is blind. Pinned by
    ``test_a_same_named_second_reader_is_not_shadowed_by_the_first``.
    """
    out: list[str] = []
    for tool, _path, src in sources:
        unclaimed = [r.func for r in readers if r.tool == tool]
        for name in _functions_naming_the_path(src, tool):
            if name in unclaimed:
                unclaimed.remove(name)
            else:
                out.append(f"{tool}:{name}")
    return sorted(out)


# --------------------------------------------------------------------------- #
# The two assertions, factored so the synthetic cases exercise the same code.
# --------------------------------------------------------------------------- #

_HELPER_CAVEAT = (
    "If you hoisted the removal into a helper: this test requires it INLINE and "
    "cannot prove that CALLING a helper removes anything — a once-guard (`if "
    "_DONE: return`), an @lru_cache, or a later rebinding of the name each make "
    "the call a no-op on every warm container after the first, which is precisely "
    "the bug. Inline it here; a comment can point at the helper."
)


def _clear_failure(reader: _Reader) -> str | None:
    if reader.clear_index is not None:
        return None
    if reader.near_misses:
        detail = "\n".join(
            f"  --- statement {i}, normalised ---\n{text}"
            for i, text in reader.near_misses
        )
        found = f"These statements look like an attempt, and none matches:\n{detail}"
    else:
        found = "No statement here even looks like an attempt to remove that file."
    forms = "\n\n".join(f"  # {name}\n{text}" for name, text in _TEMPLATES.items())
    return (
        f"{reader.label}:{reader.func} reads {_RESULTS_PATH} at top-level statement "
        f"{reader.read_index} without unconditionally removing it first.\n"
        f"{found}\n"
        f"This test recognises {len(_TEMPLATES)} exact forms; yours is not one of "
        f"them — either use one of them, or extend _TEMPLATES deliberately after "
        f"checking by hand that your form unconditionally ATTEMPTS the removal on "
        f"every call. The recognised forms are:\n{forms}\n"
        f"Matching is textual and exact, after f-strings and non-path string "
        f"literals are replaced by '{_ELIDED}', and the removal must be a "
        f"TOP-LEVEL statement of this function.\n"
        f"{_HELPER_CAVEAT}\n"
        f"WHY IT MATTERS: on a warm Modal container a pipeline that dies before "
        f"writing its own file leaves the PREVIOUS job's verdict in place, and "
        f"_interpret_pipeline_return() reports THIS job succeeded with THAT job's "
        f"results — it branches on smoke['status'] alone, with no exit_code gate."
    )


def _order_failure(reader: _Reader) -> str | None:
    if reader.clear_index is None:
        # Deliberately NOT silent. The ordering invariant is violated when there
        # is nothing to order, and an unguarded wrapper should light up both
        # checks — one failure per tool would understate a class-wide regression
        # that a reviewer counts rather than reads. The detail lives next door.
        where = (
            f"its spawn at statement {reader.spawn_index}"
            if reader.spawn_index is not None
            else "anything (this function makes no recognised process start either)"
        )
        return (
            f"{reader.label}:{reader.func} has no recognised removal of "
            f"{_RESULTS_PATH} to order against {where}; nothing is proven to clear "
            f"the file before the pipeline starts. test_stale_result_file_is_cleared "
            f"reports what was found."
        )
    if reader.spawn_index is None:
        return (
            f"{reader.label}:{reader.func} reads {_RESULTS_PATH} but makes no call "
            f"this file recognises as starting a process. Recognised, and only "
            f"these, written exactly this way: {', '.join(sorted(_SPAWN_CALLS))}. "
            f"EVERYTHING ELSE IS UNRECOGNISED — an open set, not a list, and the "
            f"examples are only examples: an alias (`import subprocess as sp`, "
            f"`_RUN = subprocess.run`), os.exec*, os.spawn*, os.posix_spawn, "
            f"asyncio.create_subprocess_exec / create_subprocess_shell, a "
            f"spawn hoisted into a helper. They land "
            f"here — but ONLY when they are the sole start in this function; "
            f"beside a recognised one they are invisible, not loud. This file's "
            f"model of the wrapper no longer holds: "
            f"either the spawn moved into a helper — in which case the clear must "
            f"move to wherever that helper is CALLED from, and this function is no "
            f"longer where the ordering can be checked — or the wrapper stopped "
            f"shelling out, or it spawns in a way this file does not read. Resolve "
            f"it rather than letting the ordering check become a no-op."
        )
    clear, spawn, read = reader.clear_index, reader.spawn_index, reader.read_index
    if clear < spawn < read:
        return None
    return (
        f"{reader.label}:{reader.func} orders the guard wrong. Top-level statement "
        f"indices: clear@{clear} ({reader.clear_template}) first-spawn@{spawn} "
        f"first-read@{read}, over {reader.spawn_count} recognised process start(s). "
        f"Required: clear < spawn < read.\n"
        f"  Indices, not line numbers: a clear nested inside the SAME statement as "
        f"the spawn — its `finally:`, its `else:`, its own `try:` body — is not "
        f"before it, however the line numbers fall.\n"
        f"  The anchors are the EARLIEST spawn and the EARLIEST read, so the rule is "
        f"'the clear precedes every process start this file recognises here'. This "
        f"file cannot tell which subprocess is the pipeline, so shelling out at all "
        f"before clearing counts. A removal after the spawn deletes the file the "
        f"pipeline just wrote; a removal after the read protects nothing."
    )


# --------------------------------------------------------------------------- #
# Discovery: the FILE set is pinned, and every pinned file must be modelled.
# --------------------------------------------------------------------------- #


def _wrapper_paths() -> list[Path]:
    """Every ``tools/*/modal_app.py``, discovered not enumerated."""
    return sorted(_ROOT.glob("tools/*/modal_app.py"))


_ALL_WRAPPERS = _wrapper_paths()
if not _ALL_WRAPPERS:
    # Import-time, so a glob that stopped matching is a collection ERROR rather
    # than a file full of tests that quietly parametrise over nothing.
    raise RuntimeError(
        f"no tools/*/modal_app.py under {_ROOT} — the stale-result guard cannot "
        f"check wrappers it cannot find"
    )

# Explicit encoding, so a non-UTF-8 wrapper raises UnicodeDecodeError here rather
# than decoding differently on a different machine's locale. Parsed eagerly for
# symmetry, though ``_analyze`` below would raise the same SyntaxError anyway.
_SOURCES: list[tuple[str, Path, str]] = []
for _path in _ALL_WRAPPERS:
    _src = _path.read_text(encoding="utf-8")
    ast.parse(_src, filename=str(_path))
    _SOURCES.append((_path.parent.name, _path, _src))

_READERS: list[_Reader] = []
for _tool, _path, _src in _SOURCES:
    _READERS.extend(
        _analyze(_src, str(_path.relative_to(_ROOT)).replace("\\", "/"), _tool)
    )
_IDS = [f"{r.tool}:{r.func}" for r in _READERS]


def _file_set_drift(found: set[str]) -> str | None:
    """None if ``found`` is EXACTLY the pinned set, else the whole failure text.

    Factored out of the test below so the EQUALITY can itself be pinned. Inline,
    the assertion carried a written instruction — "Do NOT relax this to a minimum
    count" — that nothing enforced: rewriting it as
    ``len(found) >= len(_EXPECTED_TOOLS)`` left the suite green, and so did
    ``found >= set(_EXPECTED_TOOLS)``. Under the first, renaming ``tools/af2/``
    to ``tools/af2_v2/`` still passed — nine wrappers before, nine after, and the
    one that was being checked gone.
    ``test_the_file_set_pin_is_an_equality_not_a_minimum`` kills both.
    """
    if found == set(_EXPECTED_TOOLS):
        return None
    return (
        f"the set of tools/*/modal_app.py files drifted.\n"
        f"  expected: {sorted(_EXPECTED_TOOLS)}\n"
        f"  found:    {sorted(found)}\n"
        f"  added:    {sorted(found - _EXPECTED_TOOLS)}\n"
        f"  missing:  {sorted(set(_EXPECTED_TOOLS) - found)}\n"
        f"WHAT TO DO. If you ADDED a Modal wrapper, add its directory name to "
        f"_EXPECTED_TOOLS at the top of this file. That registration is not a "
        f"rubber stamp and it is not a coverage claim: it subjects the wrapper to "
        f"exactly three checks — that some function in it is recognised as reading "
        f"{_RESULTS_PATH}, that each such function removes that file inline before "
        f"any process start, and that the order is clear < spawn < read. Nothing "
        f"here verifies that your wrapper writes the file, that the pipeline is "
        f"what it spawns, or that anything else about it is correct. If you REMOVED "
        f"or renamed a wrapper, drop or rename it there too. Do NOT relax this to a "
        f"minimum count: an equality is the only form that also catches a wrapper "
        f"silently dropping OUT of coverage, which is the worse direction."
    )


def test_the_wrapper_file_set_is_exactly_the_pinned_nine():
    """Guard the guard: a new, moved or deleted wrapper must fail loudly.

    THE DEFECT THIS REPLACES. Discovery used to key on wrappers that MENTION the
    literal basename ``smoke_results.json``. Two ordinary refactors made a
    wrapper invisible — and because the per-reader assertions are parametrised
    over discovery, invisible renders as zero tests, which reads as green:
    ``_SMOKE = "/tmp/" + "smoke" + "_results.json"``, and ``from
    tools.common.paths import SMOKE_PATH``. A brand new wrapper carrying the
    production bug and spelling the path either way produced a clean run.

    Pinning the FILE set removes the text search from the loop entirely: a tenth
    ``tools/*/modal_app.py`` fails here no matter how it spells anything, or
    whether it mentions the path at all, and registering it forces it through the
    reader check below.
    """
    drift = _file_set_drift({tool for tool, _p, _s in _SOURCES})
    assert drift is None, drift


def test_discovery_is_the_glob_and_not_a_text_search(tmp_path, monkeypatch):
    """The file-set pin is only spelling-independent if DISCOVERY is.

    THE MUTATION THIS EXISTS TO KILL, which survives everything else here.
    Rewrite ``_wrapper_paths`` back to the text search it replaced — keep only
    files whose source mentions ``smoke_results.json`` — and the whole suite
    stays green, because all nine real wrappers do mention it. The test above
    compares the tools it FOUND against _EXPECTED_TOOLS and finds nine either
    way, so it cannot see the difference. Nothing else can: every per-reader
    assertion is parametrised over discovery, and a wrapper that never enters
    discovery contributes zero tests, which reads as green.

    The cost lands on the NEXT wrapper. One spelling the path as ``"/tmp/" +
    "smoke" + "_results.json"`` — an ordinary constant fold, and one of the two
    refactors that produced the original blind spot — would be dropped by the
    text search before anything looked at it. So point ``_wrapper_paths`` at a
    corpus containing exactly that file. The glob finds it; the text search
    returns nothing and this fails.

    Found is not the same as covered, and the second half says so: the corpus
    wrapper carries the production bug and is recognised as reading nothing,
    because ``_is_read`` wants the literal. Being found is what routes it to
    ``test_every_wrapper_has_a_recognised_reader`` to be failed there, by name.
    """
    hidden = tmp_path / "tools" / "tenth" / "modal_app.py"
    hidden.parent.mkdir(parents=True)
    hidden.write_text(
        "import json\n"
        "import subprocess\n"
        "\n"
        '_SMOKE = "/tmp/" + "smoke" + "_results.json"\n'
        "\n"
        "\n"
        "def run_tool(payload):\n"
        '    subprocess.run(["python3", "run_pipeline.py"])\n'
        "    with open(_SMOKE) as fh:\n"
        "        return json.load(fh)\n",
        encoding="utf-8",
    )
    assert "smoke_results.json" not in hidden.read_text(encoding="utf-8"), (
        "the corpus wrapper must NOT spell the basename, or a text-search "
        "_wrapper_paths would find it too and this test would prove nothing"
    )

    monkeypatch.setitem(globals(), "_ROOT", tmp_path)
    assert _wrapper_paths() == [hidden], (
        f"_wrapper_paths() no longer discovers every tools/*/modal_app.py by "
        f"path alone. It returned {_wrapper_paths()} for a corpus whose one "
        f"wrapper spells the results path as a concatenation. Discovery must not "
        f"depend on what a file SAYS: a wrapper it skips is a wrapper with zero "
        f"tests, and zero tests is reported as a pass."
    )

    source = hidden.read_text(encoding="utf-8")
    assert _analyze(source, "tools/tenth/modal_app.py", "tenth") == [], (
        "expected the corpus wrapper to be recognised as reading nothing — it is "
        "the blind case, and being discovered is what lets "
        "test_every_wrapper_has_a_recognised_reader fail it by name"
    )


def test_every_wrapper_has_a_recognised_reader():
    """A wrapper the matcher cannot model must FAIL, never silently drop out.

    The per-reader assertions below are parametrised over ``_READERS``, so a
    wrapper this file cannot model contributes zero tests — which pytest reports
    as green. Asking the question from the other side converts that blind spot
    into a failure. Not parametrised, for the same reason as the test above.

    TWO QUESTIONS, BOTH NEEDED. The first is per-TOOL and was for a long time the
    only one: does this wrapper have ANY recognised reader? It is what catches a
    wrapper that never spells the literal at all — the concatenation corpus in
    ``test_discovery_is_the_glob_and_not_a_text_search`` is exactly that, and it
    is failed here, by name. But per-tool alone is satisfied by the FIRST reading
    function: a SECOND function in an already-covered wrapper, spelling its read
    in any unrecognised way, contributes zero ``_Reader``s and therefore zero
    per-reader tests, while the first function keeps the wrapper off the blind
    list. So the second question is per-FUNCTION: every function that names the
    results-path literal must be one this file models.

    Latent as measured today, and the measurement is narrower than an earlier
    revision of this docstring claimed. All nine wrappers have exactly ONE
    path-naming function each, and in all nine that one function is a recognised
    reader. So a wrapper with two path-touching functions is NOT "the normal
    shape here" — nothing in this repo has one. What ``esmfold2_design``
    establishes is only the weaker fact that the reader is not always called
    ``run_tool``: it reads from ``_run_one_seed``. The per-FUNCTION question is
    kept anyway, because it costs nothing and because the second reader is the
    shape that goes UNTESTED rather than red — silence is the failure mode this
    whole test exists to convert into a failure.
    """
    blind_tools = [
        tool for tool, _p, _s in _SOURCES if not any(r.tool == tool for r in _READERS)
    ]
    blind_funcs = _blind_functions(_SOURCES, _READERS)
    assert not (blind_tools or blind_funcs), (
        f"this file's model of a wrapper no longer holds, so the clear/spawn/read "
        f"ordering is going unchecked somewhere that handles another customer's "
        f"results. Unchecked is reported as zero tests, which reads as green — "
        f"which is why this is an assertion and not an absence.\n"
        f"  wrappers with NO recognised reader at all: {sorted(blind_tools)}\n"
        f"  functions that name {_RESULTS_PATH} and are NOT recognised readers: "
        f"{sorted(blind_funcs)}\n"
        f"The second list is the one that can be non-empty while the wrapper looks "
        f"covered: another function in the same file already satisfies the first "
        f"check, and this one is carrying an unmodelled read. What puts a function "
        f"on it is naming the path as the LITERAL or as a module-level constant "
        f"assigned that literal; a path built by concatenation, an f-string, "
        f"os.path.join or an import is seen by NEITHER list, and is the residual "
        f"blind spot.\n"
        f"The ONE recognised read is `open('{_RESULTS_PATH}')` — that exact literal, "
        f"one positional argument, no keywords. A mode argument, an encoding= "
        f"keyword, io.open / codecs.open / os.open, Path(...).read_text(), and any "
        f"path built from a constant, an f-string, a concatenation or os.path.join "
        f"are unrecognised on purpose. Either restore that spelling, or teach "
        f"_is_read() the new one deliberately, or establish that the function never "
        f"reads the file — a function that only CLEARS it, or only logs its name, "
        f"lands on the second list too and is a real finding to resolve, not "
        f"something to suppress. A wrapper that reads it nowhere comes off "
        f"_EXPECTED_TOOLS."
    )


@pytest.mark.parametrize("reader", _READERS, ids=_IDS)
def test_stale_result_file_is_cleared(reader: _Reader):
    """The reading function unconditionally deletes the results file, inline.

    Unconditionally is the operative word. ``if payload.get("mode") ==
    "smoke":``, an ``except OSError:`` handler that never fires, a helper that is
    defined but never called, and a removal sharing a ``try:`` with an earlier
    one that raises all contain a textbook-perfect ``os.remove`` and none of them
    protects a single customer job. None of them is textually identical to a
    template either, which is how they are rejected here.
    """
    failure = _clear_failure(reader)
    assert failure is None, failure


@pytest.mark.parametrize("reader", _READERS, ids=_IDS)
def test_clear_precedes_the_pipeline_spawn(reader: _Reader):
    """Ordering is the whole point: clear, then spawn, then read.

    The anchor is the EARLIEST recognised process start, so the rule reads "the
    clear precedes EVERY recognised process start in this function". That is the
    only form this file can justify: it cannot tell which subprocess is the
    pipeline, so shelling out at all before the clear counts.

    ``min(spawns)`` is belt and braces, not a fix. ``spawns`` is built by
    ``enumerate(func.body)``, so it is already ascending and ``min(spawns)`` is
    provably ``spawns[0]``. It is kept because it states the intended anchor at
    the point of use and survives an edit that stops building the list in order.
    """
    failure = _order_failure(reader)
    assert failure is None, failure


# --------------------------------------------------------------------------- #
# Executable cases for the matcher itself.
#
# Everything above is one pattern-matcher, and a pattern-matcher that stops
# matching fails OPEN. Each case below is a module the matcher must judge
# correctly, run through the SAME ``_analyze()`` as the nine real wrappers.
#
# A case that does not itself define ``run_tool`` is the BODY of one: it is
# wrapped in ``def run_tool(payload):`` and given ``_TAIL`` — a recognised spawn
# and a recognised read — so the case shows only the shape under test. Cases that
# need to place the spawn or the read themselves spell the whole module out.
# --------------------------------------------------------------------------- #

_PROLOGUE = """\
import functools
import json
import multiprocessing
import os
import shutil
import subprocess
from pathlib import Path

_SMOKE = "/tmp/smoke_results.json"
_RAW = "/tmp/raw_archive.tgz"
cmd = ["python3", "run_pipeline.py"]
"""

_TAIL = """\
    result = subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
"""


def _module(case: str) -> str:
    if "def run_tool" in case:
        return _PROLOGUE + case
    return _PROLOGUE + "\ndef run_tool(payload):\n" + case + _TAIL


def _guard_failures(case: str) -> list[str]:
    """Every message the two real tests would emit for a synthetic module."""
    readers = _analyze(_module(case), "<case>", "synthetic")
    # A case that produces no reader would pass every "must be rejected"
    # assertion vacuously. It is an error in the case, not a result.
    assert readers, "case defines no recognised reader at all"
    out = []
    for reader in readers:
        out.extend(m for m in (_clear_failure(reader), _order_failure(reader)) if m)
    return out


_BAD_SHAPES: dict[str, str] = {
    # --- the production bug itself, and clears that never run ---------------- #
    "no_clear_at_all_the_real_pre_fix_code": "    pass\n",
    "clear_only_for_smoke_runs_not_customers": '''
    if payload.get("mode") == "smoke":
        try:
            os.remove("/tmp/smoke_results.json")
        except FileNotFoundError:
            pass
''',
    "clear_behind_an_env_var_inside_a_full_try": '''
    if os.environ.get("CLEAR_STALE") == "1":
        try:
            os.remove("/tmp/smoke_results.json")
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"could not remove: {exc}", flush=True)
''',
    "clear_in_an_except_handler_that_normally_never_fires": '''
    try:
        os.stat("/tmp/marker")
    except OSError:
        os.remove("/tmp/smoke_results.json")
''',
    "clear_defined_in_a_helper_that_is_never_called": '''
def _clear_stale():
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass


def run_tool(payload):
''' + _TAIL,
    # A once-guard is why helper resolution was dropped: the old analyser proved
    # the helper CONTAINS a removal, which says nothing about what CALLING it
    # does. @lru_cache and `_clear_stale = lambda p: None` after the def are the
    # same defect wearing different clothes.
    "clear_delegated_to_a_helper_with_a_once_guard": '''
_DONE = False


def _clear_stale(path):
    global _DONE
    if _DONE:
        return
    _DONE = True
    os.remove(path)


def run_tool(payload):
    _clear_stale("/tmp/smoke_results.json")
''' + _TAIL,
    # --- clears in the wrong place ------------------------------------------- #
    "clear_after_the_spawn": '''
def run_tool(payload):
    result = subprocess.run(cmd)
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
''',
    "clear_after_the_read": '''
def run_tool(payload):
    result = subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        smoke = json.load(fh)
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    return smoke
''',
    "clear_in_the_finally_of_the_spawns_own_try": '''
def run_tool(payload):
    try:
        result = subprocess.run(cmd)
    finally:
        try:
            os.remove("/tmp/smoke_results.json")
        except FileNotFoundError:
            pass
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
''',
    "clear_in_the_else_of_the_spawns_own_try": '''
def run_tool(payload):
    try:
        result = subprocess.run(cmd)
    except OSError:
        raise
    else:
        try:
            os.remove("/tmp/smoke_results.json")
        except FileNotFoundError:
            pass
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
''',
    "clear_above_the_spawn_but_inside_the_spawns_own_try": '''
def run_tool(payload):
    try:
        try:
            os.remove("/tmp/smoke_results.json")
        except FileNotFoundError:
            pass
        result = subprocess.run(cmd)
    finally:
        pass
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
''',
    # This file cannot tell which subprocess is the pipeline, so the anchor is
    # the EARLIEST one. A probe run before the clear is rejected for that reason
    # — not because nvidia-smi leaks results.
    "a_benign_spawn_before_the_clear": '''
def run_tool(payload):
    subprocess.run(["nvidia-smi"])
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
''' + _TAIL,
    # --- statements that normalise ONTO a template but still execute --------- #
    # Normalisation deletes code: ``visit_JoinedStr`` drops everything inside
    # ``{...}``, ``visit_For`` drops display elements 2..n. Each of the three
    # below is character-identical to a template AFTER normalisation while the
    # deleted text still runs. ``_analyze`` refuses to count a statement holding
    # a recognised spawn or read as a clear, which is what rejects them; without
    # that refusal the first two are rejected only by the strict ``<`` in
    # ``_order_failure`` landing on clear_index == spawn_index, and both pass
    # under ``<=`` — the comparison the file's own prose calls equivalent.
    "template_b_normalising_over_a_spawn_in_the_display": '''
    for _stale in ("/tmp/smoke_results.json", subprocess.check_output(cmd, text=True).strip()):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"could not remove {_stale}: {exc}", flush=True)
''',
    "template_a_normalising_over_a_spawn_in_the_f_string": '''
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"{subprocess.run(cmd)}", flush=True)
''',
    "template_a_normalising_over_a_read_in_the_f_string": '''
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"{open('/tmp/smoke_results.json')}", flush=True)
''',
    # --- an unrecognised process start hiding behind a recognised one -------- #
    # `os.system` and `multiprocessing.Process` are in _SPAWN_CALLS for this
    # case alone. With only `subprocess.*` recognised, an unrecognised start is
    # loud ONLY when it is the sole start in the function: put one before the
    # clear and leave any recognised `subprocess.run` after it, and the anchors
    # become clear@1 spawn@2 read@3, which passes — while the pipeline was
    # already launched at statement 0. Measured, before that entry was added:
    # ACCEPTED, zero failures.
    "unrecognised_start_before_the_clear_beside_a_recognised_one": '''
def run_tool(payload):
    os.system("python3 run_pipeline.py")
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    result = subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
''',
    # The erasure guard — ``_analyze`` refusing to count a statement that holds a
    # recognised spawn or read as a clear — keys on ``_is_spawn``, so an
    # UNRECOGNISED start hidden in text that normalisation erases was invisible
    # to it. This is that hole, and it is worse than the case above: the display
    # tail is evaluated in full BEFORE iteration 1, so the pipeline starts before
    # ``os.remove`` runs, and the statement still normalises onto template B and
    # counts as the clear. Measured before ``subprocess.getoutput`` entered
    # _SPAWN_CALLS: ACCEPTED, zero failures, on this and on the
    # ``getstatusoutput`` spelling. Now the start is recognised, the statement is
    # refused as a clear, and both checks fire. If a future edit drops those
    # names from _SPAWN_CALLS this case goes green-to-red, which is the point.
    # It pins ``getoutput`` only, though: the ``getstatusoutput`` half of that
    # sentence had NO case for four rounds, and dropping the name left the file
    # green. Every name in the set, that one included, is now pinned by
    # ``test_every_recognised_spawn_name_is_load_bearing``.
    "unrecognised_start_in_the_erased_display_tail": '''
    for _stale in ("/tmp/smoke_results.json", subprocess.getoutput("python3 run_pipeline.py")):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"could not remove {_stale}: {exc}", flush=True)
''',
    # ONE CASE PER NAME, or the name is not pinned. `os.popen` is the exact twin
    # of `subprocess.getoutput` above and was missed for exactly as long — and
    # adding it to _SPAWN_CALLS with nothing exercising it would have been the
    # same defect one more time: measured, dropping `"os.popen"` back out of the
    # set left the whole file green until this case existed. Both live shapes
    # were measured ACCEPTED before the name was added — this one, where the
    # display tail starts the pipeline before iteration 1 and the statement is
    # still counted as the clear, and the plainer `os.popen(...)` at statement 0
    # beside a recognised `subprocess.run` after it. The stronger shape is
    # pinned here; both go green-to-red if the name leaves the set.
    #
    # The rule stated above was applied to `os.popen` and to nothing else, which
    # left four of the ten names in _SPAWN_CALLS unexercised. It is now enforced
    # over the whole set by `test_every_recognised_spawn_name_is_load_bearing`,
    # in the plainer before-the-clear shape; this hand-written case stays because
    # the display-tail shape is the stronger one and that test does not cover it.
    "os_popen_in_the_erased_display_tail": '''
    for _stale in ("/tmp/smoke_results.json", os.popen("python3 run_pipeline.py").read()):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"could not remove {_stale}: {exc}", flush=True)
''',
    "os_popen_before_the_clear_beside_a_recognised_one": '''
def run_tool(payload):
    os.popen("python3 run_pipeline.py").read()
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    result = subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
''',
    # --- a sibling removal that swallows this one ---------------------------- #
    "merged_try_where_the_earlier_removal_usually_raises": '''
    try:
        os.remove(_RAW)
        os.remove("/tmp/smoke_results.json")
    except OSError as exc:
        print(f"could not remove: {exc}", flush=True)
''',
    "merged_suppress_where_the_earlier_removal_usually_raises": '''
    import contextlib
    with contextlib.suppress(OSError):
        os.remove(_RAW)
        os.remove("/tmp/smoke_results.json")
''',
    # --- loop escapes that skip the results path ----------------------------- #
    # In each of these the results path is NOT first in the display, so nothing
    # about the iteration that would clear it can be asserted — and the escape is
    # what turns "unprovable" into a live bug: the raw archive is removed or
    # missing, the loop leaves, and the results file survives into the read.
    "loop_break_in_the_handler_before_the_results_path": '''
    for _stale in (_RAW, "/tmp/smoke_results.json"):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            break
''',
    "loop_break_in_the_else_before_the_results_path": '''
    for _stale in (_RAW, "/tmp/smoke_results.json"):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        else:
            break
''',
    "loop_break_in_the_finally_before_the_results_path": '''
    for _stale in (_RAW, "/tmp/smoke_results.json"):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        finally:
            break
''',
    "loop_break_as_a_sibling_of_the_try_before_the_results_path": '''
    for _stale in (_RAW, "/tmp/smoke_results.json"):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        if not payload.get("clear_all"):
            break
''',
    "loop_raise_in_the_handler_before_the_results_path": '''
    for _stale in (_RAW, "/tmp/smoke_results.json"):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            raise RuntimeError("nothing to clean")
''',
    "loop_over_a_name_instead_of_a_display": '''
_STALE_PATHS = ("/tmp/smoke_results.json", _RAW)


def run_tool(payload):
    for _stale in _STALE_PATHS:
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"could not remove {_stale}: {exc}", flush=True)
''' + _TAIL,
    # --- the removal does not denote the results path ------------------------ #
    "removal_of_a_name_rebound_between_the_removal_and_the_read": '''
    p = _RAW
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
    p = _SMOKE
''',
    "removes_the_raw_archive_only": '''
    try:
        os.remove("/tmp/raw_archive.tgz")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"could not remove: {exc}", flush=True)
''',
    "truncate_instead_of_remove": '''
    with open("/tmp/smoke_results.json", "w") as fh:
        fh.write("")
''',
    # --- the clear is in the wrong function ---------------------------------- #
    "clear_in_the_caller_read_in_a_nested_function": '''
def run_tool(payload):
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass

    def _load():
        result = subprocess.run(cmd)
        with open("/tmp/smoke_results.json") as fh:
            return json.load(fh)

    return _load()
''',
}


_GOOD_SHAPES: dict[str, str] = {
    # The form eight of the nine wrappers use, verbatim.
    "template_a_the_eight_wrapper_form": '''
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[run_tool] could not remove stale smoke_results.json: {exc}", flush=True)
''',
    # Same form, different log wording — esmfold2_design says "[_run_one_seed]"
    # where the rest say "[run_tool]". Pins that the message is normalised away
    # and the eight really are ONE template.
    "template_a_with_a_different_log_message": '''
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print("could not remove it, oh well", flush=True)
''',
    # colabfold's loop, verbatim.
    "template_b_the_colabfold_loop": '''
    for _stale in ("/tmp/smoke_results.json", _RAW):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[run_tool] could not remove stale {_stale}: {exc}", flush=True)
''',
    # A list with a longer tail: only the FIRST element is load-bearing.
    "template_b_as_a_list_with_a_longer_tail": '''
    for _stale in ["/tmp/smoke_results.json", _RAW, "/tmp/other.json"]:
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"could not remove {_stale}: {exc}", flush=True)
''',
    "template_c_without_the_oserror_arm": '''
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
''',
    # Unrelated work between the clear and the spawn: the rule is `<`, not
    # "immediately precedes" and not "at statement 0".
    "clear_first_then_unrelated_work_then_spawn": '''
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    env = dict(os.environ)
    print("about to spawn", flush=True)
''',
    # Clearing again after the read is belt-and-braces, not a violation: the
    # anchor is the FIRST proven clear.
    "a_second_redundant_clear_after_the_read": '''
def run_tool(payload):
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    result = subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        smoke = json.load(fh)
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    return smoke
''',
    # Popen is a recognised process start too, so the ordering is still checked.
    "spawn_via_subprocess_popen": '''
def run_tool(payload):
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    subprocess.Popen(cmd).wait()
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
''',
    # A nested helper `def` above the clear must not be charged to the parent as
    # an early spawn, and its read must not be charged to the parent either.
    "nested_helper_def_above_the_clear": '''
    def _park():
        subprocess.run(["tar", "-cf", "/tmp/x.tar", "/tmp/out"])

    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
''',
}


_UNSUPPORTED_SHAPES: dict[str, str] = {
    # Every case here is rejected FOR ITS SPELLING, not for its behaviour: the
    # rejection is never a claim that the code is wrong. That is the price of
    # template matching, paid deliberately — acceptance requires textual identity
    # with a form read off a real wrapper, so a spelling nobody uses is a
    # spelling nobody has checked.
    #
    # All but one of these are correct code, and the exception is named as such
    # at the case (``bare_os_remove_raises_on_a_cold_container``). "Correct" was
    # asserted of the whole bucket for four rounds while two entries crashed on
    # every call; both were found by running them, which is the only way this
    # claim is worth anything.
    "os_unlink_instead_of_os_remove": '''
    try:
        os.unlink("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
''',
    "pathlib_unlink_missing_ok": '    Path("/tmp/smoke_results.json").unlink(missing_ok=True)\n',
    # NOT correct code, and kept anyway: a naked ``os.remove`` is the first thing
    # an author reaches for, so the bucket should show what happens to it. It
    # raises FileNotFoundError on every cold container — the file it removes is
    # the one a PREVIOUS invocation left behind, and on the first invocation
    # there isn't one. It is rejected here for its spelling; it would deserve
    # rejecting anyway.
    "bare_os_remove_raises_on_a_cold_container": '    os.remove("/tmp/smoke_results.json")\n',
    "path_spelled_as_a_module_constant": '''
    try:
        os.remove(_SMOKE)
    except FileNotFoundError:
        pass
''',
    "handler_binds_a_differently_named_exception": '''
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    except OSError as err:
        print(f"could not remove: {err}", flush=True)
''',
    # The results path IS first, so iteration 1 clears it and this is correct. It
    # is rejected because an accepted loop body is fixed exactly — no break,
    # continue, raise or return — and that exactness is what makes truncating the
    # display sound. Pins the body rule independently of the first-element rule.
    "for_loop_with_a_break_in_the_body": '''
    for _stale in ("/tmp/smoke_results.json", _RAW):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError:
            break
''',
    "for_loop_with_a_continue_in_the_handler": '''
    for _stale in ("/tmp/smoke_results.json", _RAW):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"could not remove {_stale}: {exc}", flush=True)
''',
    # Correct — iteration 2 clears it — but only iteration 1 is reasoned about,
    # so a display that does not START with the results path proves nothing.
    # Pins the first-element rule independently of the body rule.
    "results_path_second_in_the_display": '''
    for _stale in (_RAW, "/tmp/smoke_results.json"):
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"could not remove {_stale}: {exc}", flush=True)
''',
    # A set is not truncated: its iteration order is not its source order, so
    # "first element" is not a property a set display has.
    "set_display_instead_of_a_tuple": '''
    for _stale in {"/tmp/smoke_results.json", _RAW}:
        try:
            os.remove(_stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"could not remove {_stale}: {exc}", flush=True)
''',
    # The clear is genuinely first, but the spawn is spelled in a way this file
    # does not recognise, so the ordering cannot be checked and is reported as
    # unchecked rather than assumed.
    "spawn_through_a_module_alias": '''
def run_tool(payload):
    import subprocess as sp

    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    sp.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
''',
    # Correct: the clear runs first. Rejected because the spawn and the read sit
    # in the SAME top-level statement, so `spawn < read` cannot hold and the
    # anchors no longer describe the wrapper.
    "spawn_and_read_in_the_same_statement": '''
def run_tool(payload):
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    try:
        result = subprocess.run(cmd)
    finally:
        with open("/tmp/smoke_results.json") as fh:
            smoke = json.load(fh)
    return smoke
''',
    # Correct: the clear runs first. Rejected because a read BEFORE the spawn
    # means the anchors are ambiguous — this file's model is that the read is
    # what returns the pipeline's verdict.
    #
    # The extra read is guarded, and has to be. Written bare it opened the file
    # the statement above had just deleted, so it raised FileNotFoundError on
    # EVERY call — four rounds of "these are correct wrappers" over code that
    # could not complete once.
    "an_extra_read_before_the_spawn": '''
def run_tool(payload):
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    try:
        with open("/tmp/smoke_results.json") as fh:
            previous = json.load(fh)
    except FileNotFoundError:
        previous = None
    result = subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
''',
    "hoisted_helper_taking_the_path": '''
def _clear_stale(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def run_tool(payload):
    _clear_stale("/tmp/smoke_results.json")
''' + _TAIL,
}


_DISCLOSED_GAPS: dict[str, str] = {
    # EVERY CASE HERE IS ACCEPTED, AND THE ACCEPTANCE IS THE DISCLOSURE — not the
    # leak. An earlier revision of this header said instead that every case
    # "CARRIES THE PRODUCTION BUG", and that is FALSE. Checked by executing all
    # three on a warm container already holding a prior job's
    # ``{"status": "COMPLETED", "designs": ["OTHER_CUSTOMER"]}``, with the
    # pipeline killed before it could write its own:
    #
    #   a_stale_file_put_back_by_unpack_archive_before_the_spawn
    #       STALE LEAK. The previous job's designs are returned as this job's
    #       result under a `succeeded` job. The claim was true of this one.
    #   the_results_file_recreated_between_the_clear_and_the_spawn
    #       NO LEAK as written — returns {}. See the case.
    #   helper_hosted_spawn_before_the_clear_beside_a_recognised_one
    #       NO LEAK, structurally — FileNotFoundError. See the case.
    #
    # WHAT IS PINNED HERE IS MATCHER BLINDNESS, NOT DAMAGE. All three are shapes
    # this file ACCEPTS while something it cannot see happens between the clear
    # and the read; whether that ends in another customer's results depends on
    # the rest of the wrapper, which is precisely what this file does not model.
    # All three are worth keeping for that reason alone. Each needs the general
    # analysis this file is a deliberate retreat from, and each is written out in
    # the module docstring. What this dict does is make those disclosures
    # EXECUTABLE, so the word "measured" up there stays true instead of decaying
    # into a thing somebody once ran. The pre-existing precedent is round 7's fix
    # to ``_order_failure``: a comment arguing for itself with no executable
    # claim.
    #
    # If a case here starts being REJECTED, nothing is broken — the disclosure
    # above it has become wrong. Update the docstring and move the case to
    # _BAD_SHAPES.
    #
    # A STALE FILE PUT BACK AFTER THE CLEAR. The clear matches a template, it is
    # first, it runs — and a later statement restores the results file before the
    # pipeline is spawned. Anchors clear@0 spawn@2 read@3. THIS IS THE ONE THAT
    # LEAKS: executed on a warm container as described above, the read returned
    # the previous job's dict and the job reported success.
    "a_stale_file_put_back_by_unpack_archive_before_the_spawn": '''
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    shutil.unpack_archive(payload["bundle"], "/tmp")
''',
    # The same class, and the cheapest possible instance of it: this one names
    # the exact literal the file already matches on, and is still accepted.
    # `open(path, "w")` is not a recognised read — deliberately, it is what makes
    # truncate-instead-of-remove report as "no clear" — so it is not even a
    # near-miss here. Rejecting it would mean deciding which statements between
    # the clear and the spawn can CREATE a file.
    #
    # IT DOES NOT LEAK AS WRITTEN, and that is a fact about the CONTENT, not
    # about the matcher. It writes `{}`, so `_interpret_pipeline_return()`
    # computes `status_raw = ""`, misses both the COMPLETED and the FAILED arm
    # and returns `"status": "error"` — a visible failure, not another
    # customer's designs. Put a COMPLETED dict in that `write` instead, or
    # `shutil.copy` a previous job's file over it, and the same accepted shape
    # leaks in full by the mechanism the case above already demonstrates. No
    # fourth case is added for either: this dict pins what the MATCHER accepts,
    # `a_stale_file_put_back_by_unpack_archive_before_the_spawn` already executes
    # the leak, and a second instance of a proven leak pins nothing new.
    "the_results_file_recreated_between_the_clear_and_the_spawn": '''
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    with open("/tmp/smoke_results.json", "w") as fh:
        fh.write("{}")
''',
    # A SPAWN HOISTED INTO A HELPER. `_owner` charges the `subprocess.run` to
    # `_go`, so the caller never sees it: the pipeline goes out at statement 1
    # while the anchors read clear@2 spawn@3 read@4. Same mechanism as
    # `unrecognised_start_before_the_clear_beside_a_recognised_one`, one spelling
    # over — and NOT closable by adding a name to _SPAWN_CALLS, because the name
    # is already in it. Attribution is what hides it, and attributing a nested
    # def's spawn to its parent is the thing `_owner` exists to stop (it failed
    # correct code; see `nested_helper_def_above_the_clear`).
    #
    # IT DOES NOT LEAK, and cannot, at THIS ordering: `_go()` is statement 1 and
    # the clear is statement 2, so the clear wipes whatever the helper's pipeline
    # left behind and the read at statement 4 gets FileNotFoundError — executed
    # on a warm container holding a previous job's COMPLETED dict, that is what
    # came back. The blindness is real and is what is pinned: a process this file
    # cannot see is started before a clear this file calls first. Move the helper
    # call BELOW the clear, or have it write anything the later statements do not
    # overwrite, and the same accepted anchors stop protecting anything.
    "helper_hosted_spawn_before_the_clear_beside_a_recognised_one": '''
    def _go():
        subprocess.run(["python3", "run_pipeline.py"])

    _go()
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
''',
}


@pytest.mark.parametrize("case", sorted(_BAD_SHAPES), ids=sorted(_BAD_SHAPES))
def test_matcher_rejects_known_bad_shapes(case: str):
    """Each of these carries the stale-result bug, or a hole that produced it."""
    failures = _guard_failures(_BAD_SHAPES[case])
    assert failures, (
        f"the synthetic wrapper `{case}` carries the stale-result bug and the "
        f"matcher passed it. This file is failing open — the nine real wrappers "
        f"are no longer being checked for this shape.\n{_module(_BAD_SHAPES[case])}"
    )


@pytest.mark.parametrize("case", sorted(_GOOD_SHAPES), ids=sorted(_GOOD_SHAPES))
def test_matcher_accepts_known_good_shapes(case: str):
    """The forms the nine wrappers use must not cost their authors a red suite."""
    failures = _guard_failures(_GOOD_SHAPES[case])
    assert not failures, (
        f"the synthetic wrapper `{case}` is CORRECT and the matcher rejected it:\n"
        + "\n".join(failures)
        + f"\n{_module(_GOOD_SHAPES[case])}"
    )


@pytest.mark.parametrize(
    "case", sorted(_UNSUPPORTED_SHAPES), ids=sorted(_UNSUPPORTED_SHAPES)
)
def test_matcher_rejects_shapes_it_cannot_prove(case: str):
    """Pin the measured cost of template matching, so it stays visible.

    These are rejected because they are not textually one of the recognised
    forms — for their SPELLING, never as a verdict on their behaviour. All but
    one are correct wrappers; the exception is marked at the case. If you are
    here because you added a template: check by hand that the form
    unconditionally ATTEMPTS the removal on every call, then move the case to
    _GOOD_SHAPES. Do not widen a template into a pattern.
    """
    failures = _guard_failures(_UNSUPPORTED_SHAPES[case])
    assert failures, (
        f"`{case}` is now accepted. If that was deliberate, move it to "
        f"_GOOD_SHAPES; if not, a template has been widened.\n"
        f"{_module(_UNSUPPORTED_SHAPES[case])}"
    )


def test_the_case_dicts_are_not_empty():
    """An empty case dict is ZERO tests, and zero tests is reported as GREEN.

    That is the exact failure mode the whole bottom half of this file exists to
    convert into a failure, and it was live against this file itself. Measured on
    the revision before this test, by emptying each dict in turn and running the
    whole file:

        _DISCLOSED_GAPS = {}      85 passed, 1 skipped   — fully green
        _GOOD_SHAPES    = {}      79 passed, 1 skipped   — fully green
        _BAD_SHAPES     = {}       3 failed, 54 passed, 1 skipped
        _UNSUPPORTED_SHAPES = {}   1 failed, 74 passed, 1 skipped

    The newest construct in the file could be deleted without a single red mark,
    taking every executable disclosure with it. The two that did go red went red
    INCIDENTALLY — via the handful of other tests that index them by case name,
    not because anything asserted a case dict holds a case. An empty
    parametrisation is one SKIP, which is why none of this shows up as a failure.

    NON-EMPTINESS ONLY, DELIBERATELY. A count would pin today's number, and a
    pinned count is the stale measurement this file has already had to delete
    once — see ``test_an_unguarded_function_fails_BOTH_checks_not_just_one``.
    Deleting all but one case from a dict is therefore still silent. The bar is
    closing the cheap gap, not closing every gap.
    """
    for name, cases in (
        ("_BAD_SHAPES", _BAD_SHAPES),
        ("_GOOD_SHAPES", _GOOD_SHAPES),
        ("_UNSUPPORTED_SHAPES", _UNSUPPORTED_SHAPES),
        ("_DISCLOSED_GAPS", _DISCLOSED_GAPS),
    ):
        assert cases, (
            f"{name} is empty, so the test parametrised over it contributes ZERO "
            f"cases. pytest reports an empty parametrisation as one SKIP, not as a "
            f"failure, so the construct can be deleted and the file stays green — "
            f"which is the shape every case in it exists to catch. Restore the "
            f"cases, or remove the construct AND its test deliberately."
        )


def test_the_pinned_spawn_names_are_written_out_and_not_derived():
    """The pin must be a LITERAL in this file's source, not a computation.

    THE DEFECT THIS CLOSES. The comment above ``_PINNED_SPAWN_NAMES`` argues at
    length that deriving the pin is vacuous, and nothing ran that argument:
    ``_PINNED_SPAWN_NAMES = tuple(sorted(_SPAWN_CALLS))`` is a one-line edit that
    satisfies the equality test below BY CONSTRUCTION, and it restores exactly the
    silence the by-hand pin was written to end. Measured, on a copy of this file
    with THIS test deleted: 103 passed; derive the pin and drop ``subprocess.call``
    from ``_SPAWN_CALLS`` and it is 102 passed, ZERO failures — one fewer test and
    no red, which pytest reports as green. Same two numbers for
    ``subprocess.check_call``, ``multiprocessing.Process`` and
    ``subprocess.getstatusoutput``. A prose argument with no executable claim is
    the defect this file already names twice — in ``_blind_functions``' docstring
    and in the ``_DISCLOSED_GAPS`` header, both pointing back at round 7's fix to
    ``_order_failure``.

    So this reads THIS FILE's own source. Any right-hand side that is not a tuple
    display of string literals fails, whatever it evaluates to — a call, a
    comprehension, a name, a concatenation.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=__file__)
    values = [
        stmt.value
        for stmt in ast.walk(tree)
        if isinstance(stmt, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_PINNED_SPAWN_NAMES"
            for t in stmt.targets
        )
    ]
    assert len(values) == 1, (
        f"expected exactly ONE assignment to _PINNED_SPAWN_NAMES in this file, "
        f"found {len(values)}. A second one would decide the pin at import time "
        f"while this test looked at the other."
    )
    assert isinstance(values[0], ast.Tuple) and all(
        isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        for elt in values[0].elts
    ), (
        f"_PINNED_SPAWN_NAMES is now `{ast.unparse(values[0])}`, which is not a "
        f"tuple of string literals. A pin computed from _SPAWN_CALLS cannot fail: "
        f"it agrees with test_the_pinned_spawn_names_are_exactly__SPAWN_CALLS by "
        f"construction, and a name dropped from _SPAWN_CALLS then takes its own "
        f"parametrised case away with it — one fewer test, no failures, green. "
        f"Write the names out by hand; the duplication is the point."
    )


def test_the_pinned_spawn_names_are_exactly__SPAWN_CALLS():
    """``_PINNED_SPAWN_NAMES`` is the parametrisation, so it must not drift.

    It is deliberately a second copy of ``_SPAWN_CALLS`` — see the comment there
    — and a second copy is only worth anything while it is held to EQUALITY. This
    is the half that catches a name ADDED to _SPAWN_CALLS without a case: what
    ``os.popen`` was until a case was written for it, and what
    ``subprocess.getstatusoutput``, ``subprocess.call``, ``subprocess.check_call``
    and ``multiprocessing.Process`` all still were when this test was written —
    measured, dropping any of the four left the whole file green. The other half
    — a name dropped — is caught by that name's own case.

    Equality, not a subset, for the same reason
    ``test_the_file_set_pin_is_an_equality_not_a_minimum`` gives.
    """
    assert set(_PINNED_SPAWN_NAMES) == set(_SPAWN_CALLS), (
        f"_PINNED_SPAWN_NAMES has drifted from _SPAWN_CALLS.\n"
        f"  recognised but NOT pinned: {sorted(set(_SPAWN_CALLS) - set(_PINNED_SPAWN_NAMES))}\n"
        f"  pinned but NOT recognised: {sorted(set(_PINNED_SPAWN_NAMES) - set(_SPAWN_CALLS))}\n"
        f"The first list is a name this file will treat as a process start with "
        f"NOTHING exercising it — the defect that let os.popen and "
        f"subprocess.getstatusoutput sit unpinned. Adding a name is the TRADE "
        f"written out at _SPAWN_CALLS: measure it in both directions, write the "
        f"arithmetic there, then add it here. The second list means a name left "
        f"_SPAWN_CALLS and its case went with it; if that was deliberate, drop it "
        f"here too."
    )
    assert len(_PINNED_SPAWN_NAMES) == len(set(_PINNED_SPAWN_NAMES)), (
        f"_PINNED_SPAWN_NAMES has a duplicate, which would hide a missing name "
        f"behind the set comparison above: {sorted(_PINNED_SPAWN_NAMES)}"
    )


@pytest.mark.parametrize("name", _PINNED_SPAWN_NAMES, ids=_PINNED_SPAWN_NAMES)
def test_every_recognised_spawn_name_is_load_bearing(name: str, monkeypatch):
    """ONE CASE PER NAME — enforced over the whole set, not case by case.

    THE DEFECT THIS CLOSES. That rule is stated at ``os_popen_in_the_erased_
    display_tail`` and was then applied to ``os.popen`` alone. Measured on the
    revision before this test, by deleting each name from ``_SPAWN_CALLS`` in
    turn and running the whole file, FOUR of the ten were dead weight — the file
    stayed at 88 passed without them:

        subprocess.call            88 passed        subprocess.check_call   88 passed
        subprocess.getstatusoutput 88 passed        multiprocessing.Process 88 passed

    Four names carrying the exact defect that let ``os.popen`` slip for a whole
    round. ``subprocess.getstatusoutput`` was worse than merely unpinned: the
    comment at ``unrecognised_start_in_the_erased_display_tail`` asserts it was
    measured ACCEPTED before it entered the set, while only the ``getoutput``
    spelling ever had a case.

    THE SHAPE IS THE PRODUCTION ONE: an unrecognised start at statement 0 beside
    a recognised one after the clear, so the anchors read clear@1 spawn@2 read@3
    — a clean pass with the pipeline already running. Recognising ``name`` drags
    the spawn anchor back to 0 and the ordering fails. BOTH DIRECTIONS are
    asserted, because "it is rejected" alone would be satisfied by any other
    reason for rejecting it; dropping the name must hand the case back its pass,
    or this parametrisation is vacuous for that name.

    A name is always exercised against a DIFFERENT recognised name in the tail,
    never against itself. With ``subprocess.run`` in both positions, dropping it
    would leave the function with no recognised start at all and the case would
    be rejected by the loud no-recognised-start backstop instead of accepted —
    true, and proof of nothing about the name. The tail is ``subprocess.run``
    (or ``subprocess.Popen`` when ``subprocess.run`` is the name under test)
    rather than "any other pinned name", so that dropping one name cannot cascade
    into every other name's case and bury the one real failure.

    The call ARGUMENTS are not meaningful. These modules are parsed and never
    executed; the only property under test is that ``_dotted`` reads the dotted
    name as written.
    """
    other = "subprocess.Popen" if name == "subprocess.run" else "subprocess.run"
    case = (
        "\ndef run_tool(payload):\n"
        f"    {name}(cmd)\n"
        "    try:\n"
        '        os.remove("/tmp/smoke_results.json")\n'
        "    except FileNotFoundError:\n"
        "        pass\n"
        f"    {other}(cmd)\n"
        '    with open("/tmp/smoke_results.json") as fh:\n'
        "        return json.load(fh)\n"
    )
    assert _guard_failures(case), (
        f"`{name}` is in _SPAWN_CALLS, so a call to it BEFORE the clear must pull "
        f"the spawn anchor to statement 0 and fail the ordering. It did not, so "
        f"_is_spawn no longer reads this spelling and the name is decorative.\n"
        f"{_module(case)}"
    )

    monkeypatch.setitem(globals(), "_SPAWN_CALLS", _SPAWN_CALLS - {name})
    assert not _guard_failures(case), (
        f"dropping `{name}` from _SPAWN_CALLS left this case REJECTED anyway, so "
        f"the name is not what rejects it and this case pins nothing about it. "
        f"Re-derive the case — see the docstring on why the tail must use a "
        f"different recognised name.\n" + "\n".join(_guard_failures(case))
    )


def test_template_matching_is_exact_not_fuzzy():
    """Acceptance is ``==`` on the normalised text. Not ``in``, not whitespace-blind.

    Every loosening of this comparison is a loosening of the whole file, and the
    tolerant-looking ones are the tempting ones: ``.strip()`` and "ignore
    whitespace" both look like tidying and both turn an exact list into a
    pattern language.
    """
    for name, text in _TEMPLATES.items():
        assert _match_template(text) == name
        assert _match_template(text + "\n") is None, f"trailing newline accepted: {name}"
        assert _match_template(" " + text) is None, f"leading space accepted: {name}"
        assert _match_template(text.replace("\n", " ")) is None, f"unindented: {name}"
        assert _match_template("if x:\n    " + text.replace("\n", "\n    ")) is None


def test_no_accepted_template_can_contain_a_spawn_or_a_read():
    """Why ``clear < spawn`` and ``clear <= spawn`` are the same rule.

    The claim is about the STATEMENTS that get accepted, and walking the
    template STRINGS does not establish it — which is all this test used to do,
    so it proved nothing about the matcher. What is compared is a statement
    AFTER normalisation, and normalisation deletes an f-string's interior and a
    display's tail; a statement can therefore be character-identical to a
    template and still spawn the pipeline or read the results file. Three such
    statements are in _BAD_SHAPES, and measured on the pre-fix file two of them
    were rejected only because the strict ``<`` happened to land on clear_index
    == spawn_index — both PASSED under ``<=``.

    What makes the equivalence true is ``_analyze`` declining to count such a
    statement as a clear at all, so ``clear_index`` is None and the rejection no
    longer depends on the comparison. The string walk below is kept because it
    is cheap and true; the statement checks after it are the proof.
    """
    for name, text in _TEMPLATES.items():
        nodes = list(ast.walk(ast.parse(text)))
        assert not any(_is_spawn(n) for n in nodes), f"{name} contains a spawn"
        assert not any(_is_read(n) for n in nodes), f"{name} contains a read"

    for case in (
        "template_a_normalising_over_a_spawn_in_the_f_string",
        "template_a_normalising_over_a_read_in_the_f_string",
        "template_b_normalising_over_a_spawn_in_the_display",
    ):
        module = _module(_BAD_SHAPES[case])
        func = next(
            n
            for n in ast.walk(ast.parse(module))
            if isinstance(n, ast.FunctionDef) and n.name == "run_tool"
        )
        # Non-vacuity: the case only exercises the hazard while it really does
        # normalise onto a template AND really does still spawn or read. If a
        # future normaliser stops erasing, this is where the case stops being
        # about anything, rather than quietly passing.
        hazards = [
            stmt
            for stmt in func.body
            if _match_template(_normalise(stmt)) is not None
            and any(_is_spawn(n) or _is_read(n) for n in ast.walk(stmt))
        ]
        assert hazards, (
            f"`{case}` no longer has a statement that both matches a template and "
            f"contains a recognised spawn or read, so it no longer tests anything. "
            f"Re-derive it from what _normalise erases today, or drop it."
        )
        for reader in _analyze(module, "<case>", "synthetic"):
            assert reader.clear_index is None, (
                f"`{case}` was counted as cleared by "
                f"{reader.clear_template!r} at statement {reader.clear_index}, but "
                f"that statement still contains a recognised spawn or read. "
                f"clear_index == spawn_index is reachable again, `clear < spawn` "
                f"and `clear <= spawn` have stopped being the same rule, and a "
                f"statement that starts the pipeline is being read as the clear."
            )


def test_only_a_bare_one_argument_open_of_the_results_path_is_a_read():
    """Truncating is not reading, and neither is opening some other file.

    ``open(path, "w")`` must not become a read anchor — that is what makes
    truncate-instead-of-remove report as "no clear" rather than as a read before
    the spawn — and widening the first argument would make every ``open()`` in a
    wrapper a results-file read.
    """

    def call(src: str) -> ast.AST:
        return ast.parse(src).body[0].value

    assert _is_read(call('open("/tmp/smoke_results.json")'))
    assert not _is_read(call('open("/tmp/smoke_results.json", "w")'))
    assert not _is_read(call('open("/tmp/smoke_results.json", encoding="utf-8")'))
    assert not _is_read(call('open("/tmp/other.json")'))
    assert not _is_read(call("open(path)"))


def test_a_case_with_no_recognised_reader_is_an_error_not_a_pass():
    """``_guard_failures`` must not let a case pass "reject" checks vacuously.

    Every _BAD_SHAPES assertion is "this produced at least one failure". A case
    the matcher does not see as a reader produces zero readers and zero failures,
    which would satisfy nothing while looking green.
    """
    with pytest.raises(AssertionError, match="defines no recognised reader"):
        _guard_failures("\ndef run_tool(payload):\n    return subprocess.run(cmd)\n")


def test_an_unguarded_function_fails_BOTH_checks_not_just_one():
    """``_order_failure``'s ``clear_index is None`` branch must stay LOUD.

    That branch is a deliberate double-report: with nothing to order, the
    ordering invariant is violated too, so an unguarded wrapper lights up
    ``test_stale_result_file_is_cleared`` AND
    ``test_clear_precedes_the_pipeline_spawn``. It is what makes reverting the
    fix on N wrappers show as 2N failures rather than N — the arithmetic a
    reviewer counts rather than reads, and the arithmetic this whole guard is
    certified on.

    NOTHING PINNED IT. Replacing the entire branch with ``return None`` — the
    obvious "tidy up the duplicate message" edit, and the obvious way to silence
    half the evidence — left the suite fully green: measured on the revision
    BEFORE this test existed, every test in the file passed with the branch dead.
    (The bare count that used to stand here has been deliberately removed: it was
    the file's test total at that moment, it has moved twice since, and a stale
    number reads as a false measurement.) The branch has a five-line comment
    arguing for itself and, until this test, had no executable claim.

    Both arms are checked, because the branch has two: one for a function that
    does spawn, one for a function that does not.
    """
    with_spawn = _guard_failures(_BAD_SHAPES["no_clear_at_all_the_real_pre_fix_code"])
    assert len(with_spawn) == 2, (
        f"an unguarded reading function must produce a message from BOTH checks, "
        f"got {len(with_spawn)}:\n" + "\n\n".join(with_spawn)
    )
    assert any("nothing is proven to clear" in m for m in with_spawn), (
        "the ordering check went silent on a function with no recognised clear. "
        "One failure per wrapper understates a class-wide regression by half.\n"
        + "\n\n".join(with_spawn)
    )

    # The other arm: no recognised process start either, so the branch has no
    # spawn index to name and says so instead of naming one.
    readers = _analyze(
        _module(
            '''
def run_tool(payload):
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
'''
        ),
        "<case>",
        "synthetic",
    )
    assert [r.spawn_index for r in readers] == [None], (
        f"expected one reader with no recognised spawn, got {readers}"
    )
    no_spawn = _order_failure(readers[0])
    assert no_spawn is not None and "no recognised process start either" in no_spawn, (
        f"the no-spawn arm of the clear_index-is-None branch went silent: "
        f"{no_spawn!r}"
    )


def test_the_rejection_is_actionable():
    """The failure text must carry the recognised forms AND what it saw instead.

    The whole cost of template matching is paid by authors of correct code that
    is spelled differently. They are owed the list they have to match, and the
    normalised text of their own statement, without going and reading this file.
    """
    text = "\n".join(_guard_failures(_BAD_SHAPES["clear_only_for_smoke_runs_not_customers"]))
    assert f"This test recognises {len(_TEMPLATES)} exact forms" in text
    for template in _TEMPLATES.values():
        assert template in text, f"template missing from the failure text:\n{template}"
    # A near miss with no removal call in it at all is still quoted back, so an
    # author who truncated instead of removing sees their own statement.
    truncate = "\n".join(_guard_failures(_BAD_SHAPES["truncate_instead_of_remove"]))
    assert "open('/tmp/smoke_results.json', '<elided>')" in truncate, truncate


def test_helper_based_clear_fails_with_an_honest_message():
    """The rejection must say what is true, not "there is no removal".

    The original complaint against this file was a factually false message. The
    remedy is an honest one, not a matcher that accepts more shapes.
    """
    failures = _guard_failures(_UNSUPPORTED_SHAPES["hoisted_helper_taking_the_path"])
    assert any(
        "cannot prove that CALLING a helper removes anything" in f for f in failures
    ), "expected the helper caveat in the failure text, got:\n" + "\n".join(failures)


def test_nested_reader_is_attributed_to_the_innermost_function_only():
    """A nested reader must not also be charged to its parent.

    Walking every ``FunctionDef`` and then walking into it again flagged the
    parent as a reader too, and the parent inherited the CHILD's anchors —
    a failure with a true verdict and nonsense reasoning. Attribution is
    innermost-only, so exactly one function is charged and its indices are its
    own.
    """
    readers = _analyze(
        _module(
            '''
def run_tool(payload):
    result = subprocess.run(cmd)

    def _load():
        with open("/tmp/smoke_results.json") as fh:
            return json.load(fh)

    return _load()
'''
        ),
        "<case>",
        "synthetic",
    )
    assert [r.func for r in readers] == ["_load"], (
        f"expected the read to be charged to `_load` alone, got "
        f"{[r.func for r in readers]}"
    )
    assert _clear_failure(readers[0]) is not None, (
        "the nested reader clears nothing and must still fail"
    )


@pytest.mark.parametrize(
    "case", sorted(_DISCLOSED_GAPS), ids=sorted(_DISCLOSED_GAPS)
)
def test_the_disclosed_gaps_are_still_exactly_as_disclosed(case: str):
    """These are ACCEPTED. The acceptance is the disclosure, executed.

    What is asserted is acceptance, not damage — see the header on
    ``_DISCLOSED_GAPS``, which used to claim all three leak and now records which
    one does. A shape this file waves through while something it cannot see
    happens between the clear and the read is worth pinning either way.

    The module docstring says of the first one "measured as written above,
    through the same ``_analyze`` the nine wrappers go through: ACCEPTED, zero
    failures", and for several rounds nothing re-ran it. A prose claim ABOUT a
    measurement is the first thing to go stale, in either direction — the
    docstring has been wrong about a gap being open and wrong about one being
    closed. This test does not ask for these to stay broken; it asks for the
    docstring and the matcher to keep saying the same thing.
    """
    failures = _guard_failures(_DISCLOSED_GAPS[case])
    assert not failures, (
        f"`{case}` is now REJECTED. That is an improvement to the matcher and it "
        f"makes the prose above it FALSE — this case is written into the module "
        f"docstring as a known accepted gap. Update that disclosure and move the "
        f"case to _BAD_SHAPES.\n" + "\n".join(failures)
    )


def test_the_file_set_pin_is_an_equality_not_a_minimum():
    """``==``, not ``>=``, and not a count. Both relaxations left the suite green.

    ``_file_set_drift`` carries a written instruction not to relax the comparison
    to a minimum count, and until this test nothing enforced it. Measured on the
    pre-fix file: rewriting it as ``len(found) >= len(_EXPECTED_TOOLS)`` passed
    the whole file, and so did ``found >= set(_EXPECTED_TOOLS)``. Under the first,
    renaming ``tools/af2/`` to ``tools/af2_v2/`` still passed — nine wrappers
    before, nine after, and the one being checked gone.

    One case per relaxation, plus the direction the message itself calls worse.
    """
    expected = set(_EXPECTED_TOOLS)
    assert _file_set_drift(expected) is None, (
        "the pinned set must not read as drift against itself"
    )

    # RENAMED: same count, different set. A minimum-count check passes this.
    assert _file_set_drift((expected - {"af2"}) | {"af2_v2"}) is not None, (
        "a renamed wrapper is drift. The count is unchanged, so a count check "
        "cannot see it, and af2's guard is no longer checked by anything."
    )
    # ADDED: a strict superset. A `found >= expected` check passes this.
    assert _file_set_drift(expected | {"tenth"}) is not None, (
        "a tenth wrapper must be registered deliberately, not absorbed silently"
    )
    # DROPPED OUT of coverage — the direction the failure text calls worse.
    assert _file_set_drift(expected - {"af2"}) is not None, (
        "a wrapper leaving coverage must be loud"
    )


def test_a_second_reading_function_is_modelled_and_not_shadowed_by_the_first():
    """Two readers in one module means two sets of anchors and two verdicts.

    NOTHING PINNED THIS. All nine real wrappers have exactly one path-naming
    function each, so no module the suite ever looked at had a second reading
    function at all, and three separate mutations rode through green: ``_analyze``
    returning ``out[:1]``, ``_functions_naming_the_path`` returning ``[]``, and
    the blind-function comprehension returning ``[]``. The first is the one that
    matters — it silently drops every reader after the first, which is precisely
    the shape the per-function half of
    ``test_every_wrapper_has_a_recognised_reader`` was added for.

    It runs through the SAME ``_analyze`` the nine wrappers do. A case that
    checked the shape some other way would be pinning a second implementation.
    """
    source = _module(
        '''
def run_tool(payload):
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    result = subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)


def _run_one_seed(seed):
    subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)
'''
    )
    readers = _analyze(source, "<case>", "synthetic")
    assert [r.func for r in readers] == ["_run_one_seed", "run_tool"], (
        f"both reading functions must be modelled, got "
        f"{[r.func for r in readers]}. A reader this file drops has zero anchors "
        f"and zero tests, and zero tests is reported as a pass."
    )
    by_name = {r.func: r for r in readers}
    assert _clear_failure(by_name["run_tool"]) is None, (
        "the guarded function must still pass on its own anchors"
    )
    assert _clear_failure(by_name["_run_one_seed"]) is not None, (
        "the second function clears nothing and must fail on ITS anchors — it "
        "does not inherit the first function's pass"
    )
    assert _functions_naming_the_path(source, "synthetic") == [
        "_run_one_seed",
        "run_tool",
    ], "both functions name the literal and both must be listed"


@pytest.mark.parametrize(
    "binding",
    [
        '_SMOKE_PATH = "/tmp/smoke_results.json"',
        '_SMOKE_PATH: str = "/tmp/smoke_results.json"',
        'try:\n    _SMOKE_PATH = "/tmp/smoke_results.json"\nexcept NameError:\n    raise',
    ],
    ids=["plain-assign", "annotated-assign", "inside-a-module-level-try"],
)
def test_a_second_reader_spelled_with_a_module_constant_is_not_invisible(binding: str):
    """A second reader that does not spell the literal must not get ZERO tests.

    MEASURED SILENT before the fix. Append to any already-covered wrapper::

        _SMOKE_PATH = "/tmp/smoke_results.json"

        @app.function(gpu="A100")
        def run_tool_v2(payload):
            subprocess.run(["python3", "run_pipeline.py"])   # NO CLEAR
            with open(_SMOKE_PATH) as fh:
                return json.load(fh)

    and BOTH halves of the check missed it: not a ``_Reader``, because
    ``_is_read`` wants the literal, and not on the blind list either, because
    ``_functions_naming_the_path`` wanted the literal too — while the original
    ``run_tool`` kept the tool off the per-TOOL list. Zero tests, green suite.
    The same second reader spelled with the literal IS caught, which is what made
    it look covered.

    THE FIX IS ON THE DISCOVERY SIDE ONLY. ``_functions_naming_the_path``
    resolves a constant assigned the literal; ``_is_read`` was NOT widened, and
    must not be. ``_is_read`` grants a model, so a name it resolved wrongly hands
    out a PASS; this list can only ever produce a FAILURE. The first assertion
    below pins that asymmetry, not just the second.

    ONE PARAMETER PER SPELLING ``_names_assigned_the_path`` CLAIMS TO READ.
    Resolution used to be a bare module-level ``ast.Assign``, and only that
    spelling had a case — so annotating this file's own example constant as
    ``_SMOKE_PATH: str = ...`` re-opened the whole gap, with nothing red, and
    ``AnnAssign`` was not even on the "STILL NOT SEEN" list. The module-level
    ``try:`` is the other shape ``tree.body`` could not reach. A spelling the
    resolver claims and this parametrisation does not cover is a spelling nobody
    has run.
    """
    source = _PROLOGUE + f'''
{binding}


def run_tool(payload):
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    result = subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)


def run_tool_v2(payload):
    subprocess.run(cmd)
    with open(_SMOKE_PATH) as fh:
        return json.load(fh)
'''
    readers = _analyze(source, "<case>", "synthetic")
    assert [r.func for r in readers] == ["run_tool"], (
        f"only the literal-spelled reader may be MODELLED — `_is_read` must not "
        f"resolve constants, because its output decides a pass. Got "
        f"{[r.func for r in readers]}."
    )
    assert _blind_functions([("synthetic", None, source)], readers) == [
        "synthetic:run_tool_v2"
    ], (
        f"the second reader spells its path as a module constant, is modelled by "
        f"nothing, and must appear on the blind list. Otherwise it has zero tests "
        f"while run_tool keeps the wrapper looking covered. Got "
        f"{_blind_functions([('synthetic', None, source)], readers)}."
    )


def test_a_same_named_second_reader_is_not_shadowed_by_the_first():
    """The NAME of the second reader must not be what decides whether it is seen.

    MEASURED SILENT before the fix. It is the case above — a guarded reader beside
    a second one that spells the path as a module constant — differing in the one
    thing that should not matter: the second reader is named ``run_tool``, not
    ``run_tool_v2``. It sits on a class so both can carry the same name::

        def run_tool(payload): ...            # guarded, modelled
        class _Legacy:
            def run_tool(self, payload):      # NO CLEAR — the production bug
                subprocess.run(cmd)
                with open(_SMOKE) as fh:
                    return json.load(fh)

    ``_blind_functions`` filtered each path-naming function against
    ``any(r.func == name)``, so the modelled ``run_tool`` covered for the method
    of the same name: zero per-reader failures, ``blind_funcs == []``, ACCEPTED
    SILENT. Renaming it was all it took to be loud, which is the whole of the
    defect — the residual this file discloses is a path SPELLING it cannot read,
    and this one names the path as a module constant it reads fine.

    A method is only the cheapest instance. A nested def and a conditional
    redefinition collide the same way, which is why the fix pairs by COUNT rather
    than by qualified name: one modelled reader claims one path-naming function of
    that name, and any further one is blind whatever scope it sits in.
    """
    source = _PROLOGUE + '''

def run_tool(payload):
    try:
        os.remove("/tmp/smoke_results.json")
    except FileNotFoundError:
        pass
    result = subprocess.run(cmd)
    with open("/tmp/smoke_results.json") as fh:
        return json.load(fh)


class _Legacy:
    def run_tool(self, payload):
        subprocess.run(cmd)
        with open(_SMOKE) as fh:
            return json.load(fh)
'''
    readers = _analyze(source, "<case>", "synthetic")
    assert [r.func for r in readers] == ["run_tool"], (
        f"only the literal-spelled reader may be MODELLED, got "
        f"{[r.func for r in readers]}."
    )
    assert _functions_naming_the_path(source, "synthetic") == ["run_tool", "run_tool"], (
        f"both functions name the path — one as the literal, one as the module "
        f"constant — and both must be listed, or the count below proves nothing. "
        f"Got {_functions_naming_the_path(source, 'synthetic')}."
    )
    assert _blind_functions([("synthetic", None, source)], readers) == [
        "synthetic:run_tool"
    ], (
        f"the method reads the results file, is modelled by nothing, and shares "
        f"its name with a function that IS modelled. It must still reach the blind "
        f"list: one modelled reader may claim one path-naming function, not every "
        f"function spelled the same. Got "
        f"{_blind_functions([('synthetic', None, source)], readers)}."
    )
