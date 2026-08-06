"""Offline unit tests for the Boltz-2 cofold atomic tool.

Currently covers ``run_pipeline.archive_raw_outputs`` only — the raw-output
capture that runs from a ``finally`` on every exit path. This file is the home
for further boltz2 offline tests; it is named for the ``test_<tool>_smoke.py``
convention the other tools follow, not because the coverage is broad yet.

It also carries the set's cross-tool tests. boltz2, opendde and proteina each
hold a copy of ``archive_raw_outputs`` — the opendde header calls it a "verbatim
contract from boltz2" — so ``TestRawArchiveResolutionPlacement``, which pins
where the ``dest`` resolution sits, is parametrized over all three modules here
rather than pasted into each tool's own file. It is one behavioural test plus
three structural ones; the behavioural one is the primary evidence.

Runs fully offline — no Modal, no Supabase, no GPU.
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import tarfile
import textwrap
from types import SimpleNamespace

import pytest

from tools.boltz2 import run_pipeline as rp
from tools.opendde import run_pipeline as opendde_rp
from tools.proteina import run_pipeline as proteina_rp


# ---------------------------------------------------------------------------
# 1 — archive_raw_outputs: the destination resolves on call, not at import
# ---------------------------------------------------------------------------


class TestRawArchiveDest:
    """``dest`` must be resolved when the function is called.

    ``def archive_raw_outputs(work_dir, dest=RAW_ARCHIVE_PATH)`` evaluates the
    constant once, at def time, and binds its VALUE into the function object.
    Reassigning the module constant afterwards is then silently ignored and the
    tar lands on the real ``/tmp`` path regardless.

    Not hypothetical: the proteina copy of this function carried exactly that
    default, and the test harness that set ``RAW_ARCHIVE_PATH`` to keep archives
    inside ``tmp_path`` had been writing to the real path the whole time. No
    boltz2 test set the constant before now, so here it was latent rather than
    live.
    """

    @staticmethod
    def _work_tree(tmp_path):
        src = tmp_path / "boltz2_work"
        src.mkdir()
        (src / "confidence.json").write_text('{"iptm": 0.78}')
        return src

    def test_default_follows_a_reassigned_constant(self, tmp_path, monkeypatch):
        src = self._work_tree(tmp_path)
        redirected = tmp_path / "redirected.tgz"
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(redirected))

        rp.archive_raw_outputs(str(src))

        assert redirected.is_file(), (
            "archive_raw_outputs ignored the reassigned RAW_ARCHIVE_PATH — its "
            "default is frozen at import, so the tar went to the real /tmp path")
        with tarfile.open(redirected) as tf:
            assert any(n.endswith("confidence.json") for n in tf.getnames())

    def test_explicit_dest_still_wins(self, tmp_path, monkeypatch):
        src = self._work_tree(tmp_path)
        constant = tmp_path / "constant.tgz"
        explicit = tmp_path / "explicit.tgz"
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(constant))

        rp.archive_raw_outputs(str(src), dest=str(explicit))

        assert explicit.is_file()
        assert not constant.exists(), "an explicit dest was overridden by the constant"

    def test_an_empty_dest_is_honoured_as_given(self, tmp_path, monkeypatch, caplog):
        """ONLY None resolves — the guard is identity, not truthiness.

        Every other test in this class passes just as happily with ``if not
        dest``, which would quietly swap a caller's explicit falsy dest for the
        module constant and land the tar on a path the caller never named. The
        empty string is the reachable falsy case: ``os.path.abspath("")`` is the
        cwd, a directory, so the write fails there — and it must fail THERE rather
        than divert to RAW_ARCHIVE_PATH. (Nothing is cleaned up on this path: the
        handler's os.remove() on the cwd raises and is swallowed, which is the
        correct outcome — a directory must not be unlinked.)
        chdir into tmp_path so the doomed write stays inside the tmp dir.

        The last two assertions are what stop this degrading to a vacuous test.
        The ``is None`` and ``not constant.exists()`` checks are both negative,
        and inaction satisfies them: put an early ``return`` at the top of
        archive_raw_outputs and the two behavioural tests in this class go red
        while this one stays green (the signature test stays green too — it only
        inspects the signature). Recording the path handed to tarfile.open pins
        that the cwd-directed write was actually ATTEMPTED, and the warning pins
        that it then failed there rather than being quietly skipped.
        """
        src = self._work_tree(tmp_path)
        constant = tmp_path / "constant.tgz"
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(constant))
        monkeypatch.chdir(tmp_path)
        # Exactly what the function computes from dest="". Read after the chdir,
        # and via abspath rather than str(tmp_path) so a symlinked tmp dir agrees.
        cwd_target = os.path.abspath("")

        attempted = []
        real_tar_open = rp.tarfile.open

        def recording_open(name, *args, **kwargs):
            # Record the path the resolution produced, then let the real open
            # fail on it exactly as it would unpatched (the errno is
            # platform-dependent, so do not assert on the exception itself).
            attempted.append(name)
            return real_tar_open(name, *args, **kwargs)

        monkeypatch.setattr(rp, "tarfile", SimpleNamespace(open=recording_open))

        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            assert rp.archive_raw_outputs(str(src), dest="") is None

        assert not constant.exists(), (
            'dest="" was replaced by RAW_ARCHIVE_PATH — the resolution is '
            "testing truthiness instead of identity, so an explicit falsy dest "
            "is silently overridden")
        assert attempted == [cwd_target], (
            f'dest="" never reached the write: expected one tar write aimed at the '
            f"cwd ({cwd_target}), got {attempted!r}")
        assert sum("raw capture failed" in r.getMessage() for r in caplog.records) == 1, (
            "the doomed cwd write was not reported as a capture failure; records "
            f"were {[r.getMessage() for r in caplog.records]!r}")

    def test_signature_default_is_not_a_baked_in_path(self):
        """Once nothing reassigns the constant the regression is behaviourally
        invisible, so pin the signature as well as the behaviour."""
        default = inspect.signature(rp.archive_raw_outputs).parameters["dest"].default
        assert default is None, (
            f"dest defaults to {default!r}; a module-constant default is bound at "
            "import and cannot follow a later reassignment of RAW_ARCHIVE_PATH")


# ---------------------------------------------------------------------------
# 2 — archive_raw_outputs: the documented "never raises" contract
# ---------------------------------------------------------------------------


class TestRawArchiveNeverRaises:
    """The cleanup handler must not raise on top of the failure it is cleaning up.

    Contract hardening, not a fix for anything observed in production: every
    real call site passes an absolute str, and ``os.path.isdir`` swallows
    OSError/ValueError for any str, so the window below is not reachable there.
    It is pinned because the function is documented "never raises" and is called
    from a ``finally`` in ``main()``, where an escape replaces whatever exit was
    already in flight. The handler deletes a partial tar at ``dest_abs``, but
    ``dest_abs`` used to be assigned partway through the ``try``: any failure
    before that line left it unbound, and the resulting UnboundLocalError is a
    NameError, which the inner ``except OSError`` does not catch.
    """

    def test_failure_before_dest_is_bound_does_not_escape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(tmp_path / "raw.tgz"))

        class _NotAPath:
            # os.path.isdir() swallows OSError and ValueError but NOT TypeError,
            # so this reaches the except block with dest_abs still unassigned —
            # the first statement in the try that can throw past the guard.
            def __fspath__(self):
                raise TypeError("simulated: work_dir is not a usable path")

        # Must return normally. Pre-fix this raised UnboundLocalError.
        assert rp.archive_raw_outputs(_NotAPath()) is None

    def test_partial_tar_is_still_removed_when_dest_is_bound(self, tmp_path, monkeypatch):
        """The None-guard must not disable the cleanup it guards.

        When the write fails AFTER dest_abs is bound, the truncated tar still has
        to go: modal_app parks whatever file exists, and a tar that reports
        success but cannot be read is worse than no tar at all.
        """
        src = tmp_path / "boltz2_work"
        src.mkdir()
        (src / "confidence.json").write_text('{"iptm": 0.78}')
        dest = tmp_path / "raw.tgz"

        def exploding_open(name, mode="r", *args, **kwargs):
            # Leave the truncated file a real mid-write ENOSPC would leave behind.
            with open(name, "wb") as fh:
                fh.write(b"not a readable tar")
            raise OSError(28, "No space left on device")

        # Patch the name inside the module's namespace, not the shared stdlib
        # module object, so nothing outside this call is affected.
        monkeypatch.setattr(rp, "tarfile", SimpleNamespace(open=exploding_open))

        rp.archive_raw_outputs(str(src), dest=str(dest))

        assert not dest.exists(), (
            "the partial tar survived a failed capture; the wrapper would park an "
            "archive that reports success and cannot be read")


# ---------------------------------------------------------------------------
# 3 — where the dest resolution SITS, in all three copies of the function
# ---------------------------------------------------------------------------


_ARCHIVE_MODULES = {
    "boltz2": rp,
    "opendde": opendde_rp,
    "proteina": proteina_rp,
}


def _resolves_dest_from_the_constant(node: ast.AST) -> bool:
    """True for the ``if dest is None: dest = RAW_ARCHIVE_PATH`` statement.

    Matching on ``ast.Is`` is the point, not incidental: truthiness would also
    fire for an explicit ``dest=""``, which is a caller's decision and must be
    left alone (``test_an_empty_dest_is_honoured_as_given`` pins the behaviour).
    """
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "dest"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Is)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ):
        return False
    return any(
        isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "dest" for t in stmt.targets)
        and isinstance(stmt.value, ast.Name)
        and stmt.value.id == "RAW_ARCHIVE_PATH"
        for stmt in node.body
    )


@pytest.mark.parametrize("tool", sorted(_ARCHIVE_MODULES))
class TestRawArchiveResolutionPlacement:
    """The resolution has to happen INSIDE the try, and the try has to be first.

    The primary evidence here is BEHAVIOURAL, not structural.
    ``test_a_deleted_constant_is_logged_not_raised`` hoists nothing and inspects
    nothing: it deletes RAW_ARCHIVE_PATH and calls the function. On the shipped
    code that is a logged warning and a None return; on the hoisted variant the
    NameError escapes a function documented "never raises" and called from a
    ``finally`` in ``main()``. The statement genuinely cannot throw for any dest
    a test can pass — ``is None`` is identity, so no dunder on dest runs at all —
    but what it CAN throw for is module state, and module state is reachable
    from a test, which is why that test exists.

    The three AST tests are the cheaper backstop the behavioural one cannot
    give. They pin WHERE code sits rather than what it does, so a rewrite that
    keeps the deleted-constant warning while dragging some other statement out
    from under the guard still fails. Division of labour: behaviour proves the
    consequence for the one statement it can reach, structure generalises the
    rule to every statement in the prologue and to the try's else/finally.

    Neither kind of test makes "never raises" a property you can read off the
    indentation. The ``except`` handler's own statements run outside every guard
    in all three copies — delete the module logger and its ``logger.warning``
    raises NameError out of the function.

    Parametrized over all three tools because all three carry a copy of the
    function and the contract is meant to be identical in each.
    """

    @staticmethod
    def _statements(tool):
        """The function's top-level statements, docstring stripped."""
        module = _ARCHIVE_MODULES[tool]
        tree = ast.parse(textwrap.dedent(inspect.getsource(module.archive_raw_outputs)))
        fn = tree.body[0]
        assert isinstance(fn, ast.FunctionDef)
        body = fn.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        return body

    def test_a_deleted_constant_is_logged_not_raised(
        self, tool, tmp_path, monkeypatch, caplog,
    ):
        """The behavioural half: the RAW_ARCHIVE_PATH read is under the guard.

        Deleting the constant is the reachable way to make that read fail — a
        dest argument cannot, because ``is None`` is identity. Hoist ``if dest is
        None: dest = RAW_ARCHIVE_PATH`` above the ``try`` and this call raises
        NameError instead of returning None.
        """
        mod = _ARCHIVE_MODULES[tool]
        src = tmp_path / "work"
        src.mkdir()
        (src / "kept.txt").write_text("would have been archived")
        # The only thing wrong with this call: the module constant is gone.
        monkeypatch.delattr(mod, "RAW_ARCHIVE_PATH")

        with caplog.at_level(logging.WARNING, logger=mod.logger.name):
            assert mod.archive_raw_outputs(str(src)) is None, (
                f"{tool}.archive_raw_outputs returned something other than None")

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "raw capture failed" in m and "RAW_ARCHIVE_PATH" in m for m in messages
        ), (
            f"{tool}: a missing RAW_ARCHIVE_PATH was swallowed silently instead of "
            f"being logged as a capture failure; records were {messages!r}")

    def test_nothing_that_can_raise_precedes_the_try(self, tool):
        """Only non-raising literal bindings may sit above the ``try``.

        Shape-based rather than exact-match on purpose. Dropping the ``str |
        None`` annotation, or adding a second ``name = <literal>`` pre-binding,
        is semantically identical and is accepted: a function-local annotation is
        never evaluated, so both compile to the same LOAD_CONST/STORE_FAST pair.
        What this rejects is any OTHER kind of statement above the try — not
        because each one necessarily raises, but because the whitelist is the
        only cheap way to know none of them can, and the guard below cannot catch
        what runs before it opens.
        """
        body = self._statements(tool)
        assert body and isinstance(body[-1], ast.Try), (
            f"{tool}.archive_raw_outputs does not end in the try, so there is code "
            "running after the never-raises guard has closed")

        pre_bound = {}
        for stmt in body[:-1]:
            targets = (
                [stmt.target] if isinstance(stmt, ast.AnnAssign)
                else stmt.targets if isinstance(stmt, ast.Assign)
                else []
            )
            assert (
                targets
                and all(isinstance(t, ast.Name) for t in targets)
                and isinstance(stmt.value, ast.Constant)
            ), (
                f"{tool}.archive_raw_outputs has a {type(stmt).__name__} above the "
                "try. Only ``name = <literal>`` bindings are allowed there — those "
                "compile to LOAD_CONST/STORE_FAST and provably cannot raise. "
                "Everything else belongs inside the try, whether or not this "
                "particular statement can raise")
            for target in targets:
                pre_bound[target.id] = stmt.value.value

        assert "dest_abs" in pre_bound and pre_bound["dest_abs"] is None, (
            f"{tool}: dest_abs must be pre-bound to None above the try so the "
            "handler's ``is not None`` check is meaningful; the pre-bindings found "
            f"were {pre_bound!r}")

    def test_the_try_has_no_else_or_finally_clause(self, tool):
        """``else:`` and ``finally:`` bodies are NOT covered by the except beside them.

        The other placement tests only look at ``try_node.body``, so a statement
        parked in an else or finally clause satisfies every one of them and still
        escapes: ``finally: _boom = {}["nope"]`` raises KeyError straight out of a
        function documented never to raise.
        """
        try_node = self._statements(tool)[-1]
        assert isinstance(try_node, ast.Try), f"{tool}: no try at the end of the body"
        assert not try_node.orelse and not try_node.finalbody, (
            f"{tool}.archive_raw_outputs's try carries an else/finally clause "
            f"(else={len(try_node.orelse)} stmts, finally={len(try_node.finalbody)} "
            "stmts). Neither body is covered by the except beside it, so anything "
            "put there raises out of a function documented never to raise")

    def test_the_dest_resolution_is_inside_the_try(self, tool):
        body = self._statements(tool)
        try_node = body[-1]
        assert isinstance(try_node, ast.Try), f"{tool}: no try at the end of the body"

        inside = [
            node
            for stmt in try_node.body
            for node in ast.walk(stmt)
            if _resolves_dest_from_the_constant(node)
        ]
        assert len(inside) == 1, (
            f"{tool}: expected exactly one ``if dest is None: dest = "
            f"RAW_ARCHIVE_PATH`` inside the try, found {len(inside)} — either it "
            "was hoisted out of the guard, or the identity test was replaced "
            "(``if not dest`` would swallow an explicit falsy dest)")

        outside = [
            node
            for stmt in body[:-1]
            for node in ast.walk(stmt)
            if _resolves_dest_from_the_constant(node)
        ]
        assert not outside, (
            f"{tool}: the dest resolution reads RAW_ARCHIVE_PATH before the try, "
            "so a missing constant is a NameError escaping a function called "
            "from a finally instead of a logged warning")
