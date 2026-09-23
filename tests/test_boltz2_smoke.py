"""Offline unit tests for the Boltz-2 cofold atomic tool.

Covers ``run_pipeline.archive_raw_outputs`` — the raw-output capture that runs
from a ``finally`` on every exit path — plus the zero-design and ``--no_kernels``
contracts, and the adapter's preset-aware binder cap. This file is the home for
further boltz2 offline tests; it is named for the ``test_<tool>_smoke.py``
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
import json
import logging
import os
import tarfile
import textwrap
from types import SimpleNamespace

import pytest

from shared.storage import _output_object_path
from tools import boltz2 as b2
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


# ---------------------------------------------------------------------------
# 4 — a run that folds nothing FAILS the job, it does not COMPLETE empty
# ---------------------------------------------------------------------------


class TestZeroDesignsFailsTheJob:
    """``main`` must not report COMPLETED when every design died.

    The torch/torchvision ABI break made every ``boltz predict`` exit non-zero.
    The pipeline logged a warning per design, dropped it, and still wrote
    COMPLETED with an empty ``designs[]`` — so the wrapper returned exit_code 0
    and the user saw a green job carrying no results.

    Two tests, and the second is the one that makes the first mean something:
    a guard that fired unconditionally would also pass the failure case.
    """

    def _arrange(self, tmp_path, monkeypatch, *, rc, n_binders=2,
                 upload_exc=None):
        """Stub every I/O edge of ``main`` and return the result-file path.

        ``rc`` is what ``run_boltz`` returns for every design: non-zero folds
        nothing, zero folds all of them. ``upload_exc``, when given, is raised
        by ``upload_pdb`` for every design - ``rc=0`` plus ``upload_exc`` is
        the shape Gate 1 Rung A and Rung B actually ran in, where every fold
        succeeded and the rigged endpoint refused every PUT.
        """
        result_file = tmp_path / "smoke_results.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))

        antigen = tmp_path / "antigen.pdb"
        antigen.write_text("ATOM\n")
        monkeypatch.setattr(rp, "download_antigen_pdb", lambda url, dest: antigen)
        monkeypatch.setattr(rp, "chain_seq", lambda path, chain="A": "GGGGSGGGGS")
        monkeypatch.setattr(rp, "archive_raw_outputs", lambda *a, **k: None)
        monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(rp, "run_boltz", lambda *a, **k: rc)

        # Only reached when run_boltz succeeds.
        predicted = tmp_path / "pred.pdb"
        predicted.write_text("ATOM\n")
        monkeypatch.setattr(
            rp, "collect_outputs", lambda out_dir: (predicted, {"iptm": 0.8}),
        )
        monkeypatch.setattr(
            rp,
            "hotspot_contacts",
            lambda *a, **k: {
                "n_contacted": 0,
                "n_hotspots": 0,
                "contacted": [],
                "antigen_chain": "A",
            },
        )
        monkeypatch.setattr(
            rp,
            "request_upload_urls",
            lambda endpoint, token, keys: {k: "https://example.invalid/put" for k in keys},
        )
        def _upload(url, data):
            if upload_exc is not None:
                raise upload_exc

        monkeypatch.setattr(rp, "upload_pdb", _upload)

        payload = {
            "tier": "standalone",
            "job_token": "tok",
            "upload_urls_endpoint": "https://example.invalid/upload-urls",
            "input_presigned_url": "https://example.invalid/antigen.pdb",
            "job_spec": {
                "antigen_chain": "A",
                "hotspot_residues": [],
                "binder_sequences": [
                    {"name": f"d{i}", "sequence": "EVQLVESGGG"}
                    for i in range(n_binders)
                ],
            },
        }
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps(payload))
        monkeypatch.setenv("JOB_TIER", "standalone")
        monkeypatch.setenv("JOB_ID", "job-zero-designs")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        return result_file

    def test_every_design_failing_is_a_failed_job(self, tmp_path, monkeypatch):
        result_file = self._arrange(tmp_path, monkeypatch, rc=1)

        with pytest.raises(SystemExit) as excinfo:
            rp.main()

        assert excinfo.value.code == 1, (
            "a run that folded zero designs must exit non-zero — the wrapper "
            "reports run_pipeline's returncode as the job's exit_code")
        result = json.loads(result_file.read_text())
        assert result["status"] == "FAILED", (
            f"expected FAILED, got {result['status']!r} — a zero-design run "
            "reported as COMPLETED is the silent failure this guards")
        assert result["error"]["check"] == "no_designs"
        assert isinstance(result.get("runtime_seconds"), int), (
            "a failed run must still report the GPU time it burned: "
            "gpu/modal_client.py::_interpret_pipeline_return reads "
            "runtime_seconds off the FAILED arm as gpu_seconds_used, and "
            "shared/jobs.py::_charge_workspace_for_completed_job skips the "
            "workspace compute debit when it is missing")

    def test_a_folded_design_still_completes(self, tmp_path, monkeypatch):
        """Positive control: the guard must not fire when designs survive."""
        result_file = self._arrange(tmp_path, monkeypatch, rc=0)

        rp.main()

        result = json.loads(result_file.read_text())
        assert result["status"] == "COMPLETED"
        assert result["designs_completed"] == 2
        assert result["designs_folded"] == 2, (
            "the healthy run must report folds too, or the count that "
            "separates the two failure shapes only exists on the failure path")

    def test_folded_but_unuploaded_is_not_reported_as_a_fold_failure(
        self, tmp_path, monkeypatch,
    ):
        """The whole point: an upload-only failure must not say the folds died.

        Every design folds, every PUT raises. Before this, the detail read
        "all 2 designs failed" - the verdict Gate 1 Rung A and Rung B both
        returned with 3 good folds on disk (docs/VALIDATION-LOG.md). The
        billing side is deliberately unchanged and is asserted below, because
        the value of this detail is that it does not cost a refund to get.
        """
        result_file = self._arrange(
            tmp_path, monkeypatch, rc=0,
            upload_exc=RuntimeError("upload failed: HTTP 503"),
        )

        with pytest.raises(SystemExit) as excinfo:
            rp.main()

        assert excinfo.value.code == 1
        result = json.loads(result_file.read_text())
        detail = result["error"]["detail"]
        assert "2 of 2 designs folded but 0 uploaded" in detail, (
            f"detail must separate the hops; got {detail!r}")
        assert "all 2 designs failed" not in detail, (
            f"the false claim this test exists to kill is back: {detail!r}")

        # Unchanged on purpose - see the comment above the guard in
        # run_pipeline.py. An empty designs_out still writes a refunded
        # failure carrying runtime_seconds.
        assert result["status"] == "FAILED"
        assert result["error"]["bucket"] == "pipeline"
        assert result["error"]["check"] == "no_designs"
        assert isinstance(result.get("runtime_seconds"), int)

    def test_a_run_that_folded_nothing_still_says_so(
        self, tmp_path, monkeypatch,
    ):
        """Negative control, and it is what makes the test above mean anything.

        A detail that named the upload hop unconditionally would pass that
        assertion while lying about this run, where no design ever folded.
        """
        result_file = self._arrange(tmp_path, monkeypatch, rc=1)

        with pytest.raises(SystemExit):
            rp.main()

        detail = json.loads(result_file.read_text())["error"]["detail"]
        assert "all 2 designs failed before producing a structure" in detail, (
            f"got {detail!r}")
        assert "uploaded" not in detail, (
            f"no upload was ever attempted on this run: {detail!r}")
# ---------------------------------------------------------------------------
# 5 - the image ships no cuequivariance, so the kernel path must stay off
# ---------------------------------------------------------------------------


class TestKernelsStayOff:
    """``run_boltz`` must keep passing ``--no_kernels``.

    tools/boltz2/Dockerfile.modal installs plain ``boltz``, not ``boltz[cuda]``,
    so cuequivariance is absent from the image. boltz imports it only inside
    ``kernel_triangular_mult`` / ``kernel_triangular_attn``, which run on the
    kernel path, so dropping this flag would fold nothing and the first sign
    would be a production failure. This test is what makes that Dockerfile
    comment a checked claim rather than an assertion.
    """

    def test_run_boltz_disables_kernels(self, tmp_path, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(rp.subprocess, "run", fake_run)
        rp.run_boltz(tmp_path / "in.yaml", tmp_path / "out", msa_server=False)

        assert "--no_kernels" in captured["cmd"], (
            "the boltz2 image installs plain boltz, so cuequivariance is not "
            "present; without --no_kernels the kernel path imports it and every "
            "fold dies")


# ---------------------------------------------------------------------------
# 5 — validate(): the binder cap is preset-aware
# ---------------------------------------------------------------------------


# Gate 1 Rung B, job gate1-msa_server-1790046491: 643 s of pipeline runtime for
# 3 designs. An AGGREGATE per-design figure over three 242-246 aa binders, not
# a marginal rate and not a measured 50-binder run — provenance and caveats in
# the runtime note in tools/boltz2/__init__.py.
MSA_SERVER_S_PER_DESIGN = 214.0

_MODAL_APP_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "boltz2", "modal_app.py",
)


def _modal_app_constant(name):
    """Read a module-level constant out of modal_app.py without importing it.

    Importing that module constructs a ``modal.App`` and resolves an Image from
    the Dockerfile, neither of which belongs in an offline test.
    """
    with open(_MODAL_APP_SRC, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(name + " is not a module-level assignment in "
                         + _MODAL_APP_SRC)


def _form(preset, n_binders):
    seq = "QVQLVESGGGLVQPGGSLRLSCAAS"  # 25 aa, all canonical
    return {
        "preset": preset,
        "target_chain": "A",
        "binder_sequences": "\n".join([seq] * n_binders),
    }


class TestPresetBinderCap:
    """``msa_server`` folds ~3x slower than ``standalone`` against one shared
    Modal timeout, so the two presets cannot share one binder ceiling.

    Without a preset-aware cap a user could submit 50 binders on msa_server,
    be billed for the full hour the run takes, and receive only the designs
    that finished before the timeout — the rest lost with no warning anywhere
    in the form, the validator or the estimate.
    """

    def test_standalone_still_takes_the_full_batch(self):
        inputs, err = b2.validate(_form("standalone", b2.MAX_BINDERS), {})
        assert err is None, err
        assert len(inputs["binder_sequences"]) == b2.MAX_BINDERS

    def test_msa_server_takes_its_own_ceiling(self):
        cap = b2.MAX_BINDERS_BY_PRESET["msa_server"]
        inputs, err = b2.validate(_form("msa_server", cap), {})
        assert err is None, err
        assert len(inputs["binder_sequences"]) == cap

    def test_msa_server_refuses_one_over(self):
        cap = b2.MAX_BINDERS_BY_PRESET["msa_server"]
        inputs, err = b2.validate(_form("msa_server", cap + 1), {})
        assert inputs is None
        assert str(cap) in err and str(cap + 1) in err
        # The refusal has to be actionable, or it just moves the surprise.
        assert "single-sequence" in err, err

    def test_the_batch_standalone_takes_is_refused_on_msa_server(self):
        inputs, err = b2.validate(_form("msa_server", b2.MAX_BINDERS), {})
        assert inputs is None, (
            "50 binders on msa_server extrapolate to ~10700 s against a "
            "3600 s timeout; accepting the batch is the silent truncation"
        )

    def test_the_cap_is_the_largest_batch_that_fits_the_timeout(self):
        """Pins the derivation, because 3600 lives in another file.

        Moving ``_MAX_SESSION_S`` or the measured rate without re-deriving the
        cap goes red here rather than silently re-opening the truncation.
        """
        ceiling = _modal_app_constant("_MAX_SESSION_S")
        cap = b2.MAX_BINDERS_BY_PRESET["msa_server"]
        assert cap * MSA_SERVER_S_PER_DESIGN <= ceiling, (
            "cap of %d extrapolates to %.0f s, past the %d s timeout"
            % (cap, cap * MSA_SERVER_S_PER_DESIGN, ceiling)
        )
        assert (cap + 1) * MSA_SERVER_S_PER_DESIGN > ceiling, (
            "cap of %d leaves room for another design inside %d s"
            % (cap, ceiling)
        )


# ---------------------------------------------------------------------------
# 6 - validate(): two binders may not share one storage object
# ---------------------------------------------------------------------------


_SEQ_A = "QVQLVESGGGLVQPGGSLRLSCAAS"  # 25 aa, all canonical
_SEQ_B = "EVQLLESGGGLVQPGGSLRLSCAAS"  # 25 aa, all canonical, not _SEQ_A


def _fasta_form(fasta):
    return {"preset": "standalone", "target_chain": "A", "binder_sequences": fasta}


class TestBinderNamesGetTheirOwnObject:
    """``main`` keys each design's upload on the binder's name alone.

    Each test runs ``run_pipeline.main`` and records the ``pdb_key``s it asks
    ``request_upload_urls`` for. The upload-URL endpoint (webhooks/uploads.py)
    mints each key's URL for the storage path
    ``shared/storage.py::_output_object_path`` gives it, so two designs whose
    keys map to one path leave one object for two structures.
    """

    @staticmethod
    def _requested_keys(tmp_path, monkeypatch, binders):
        """Run ``main`` over ``binders``; return the upload keys it requested."""
        TestZeroDesignsFailsTheJob()._arrange(tmp_path, monkeypatch, rc=0)
        payload = json.loads(os.environ["JOB_PAYLOAD"])
        payload["job_spec"]["binder_sequences"] = binders
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps(payload))
        requested = []

        def _record(endpoint, token, keys):
            requested.extend(keys)
            return {k: "https://example.invalid/put" for k in keys}

        monkeypatch.setattr(rp, "request_upload_urls", _record)
        rp.main()
        return requested

    def test_duplicate_headers_reach_the_pipeline_as_one_pdb_key(
        self, tmp_path, monkeypatch,
    ):
        fasta = f">VHH-12\n{_SEQ_A}\n>VHH-12\n{_SEQ_B}\n"
        # validate returns the parser's records as "binder_sequences" and
        # build_payload forwards that list (tools/boltz2/__init__.py), so these
        # are what main would be given had validate accepted.
        binders, _ = b2._parse_binder_text(fasta)

        keys = self._requested_keys(tmp_path, monkeypatch, binders)

        assert keys == ["VHH-12_complex.pdb", "VHH-12_complex.pdb"], keys
        inputs, err = b2.validate(_fasta_form(fasta), {})
        assert inputs is None, (
            f"validate accepted two binders that main uploads under one "
            f"pdb_key, {keys!r}")
        assert "'VHH-12'" in err and "Rename one" in err, err

    def test_names_that_differ_only_in_punctuation_share_an_object(
        self, tmp_path, monkeypatch,
    ):
        """The keys differ as strings; the storage path does not."""
        fasta = f">binder 1\n{_SEQ_A}\n>binder_1\n{_SEQ_B}\n"
        binders, _ = b2._parse_binder_text(fasta)

        keys = self._requested_keys(tmp_path, monkeypatch, binders)

        assert len(set(keys)) == 2, keys
        paths = {_output_object_path("u", "j", k) for k in keys}
        assert paths == {"u/j/designs/binder_1_complex.pdb"}, paths
        inputs, err = b2.validate(_fasta_form(fasta), {})
        assert inputs is None, (
            f"validate accepted two binders whose keys {keys!r} land on one "
            f"storage object, {paths!r}")
        for part in ("'binder 1'", "'binder_1'", "'binder_1_complex.pdb'"):
            assert part in err, err

    def test_distinct_names_are_accepted_and_get_two_objects(
        self, tmp_path, monkeypatch,
    ):
        """Positive control: the refusal must not fire on ordinary names.

        Feeds validate's output through build_payload into main.
        """
        inputs, err = b2.validate(
            _fasta_form(f">VHH-12\n{_SEQ_A}\n>VHH-13\n{_SEQ_B}\n"), {},
        )
        assert err is None, err

        keys = self._requested_keys(
            tmp_path, monkeypatch,
            b2.build_payload(inputs, "")["binder_sequences"],
        )

        assert len({_output_object_path("u", "j", k) for k in keys}) == 2, keys
