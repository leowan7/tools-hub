"""A GPU run that delivers zero designs must FAIL, not COMPLETE green.

Three pipelines used to write ``"status": "COMPLETED"`` with ``designs: []``
when every single design failed to fold or upload, so the user saw a green job
with no results. Each now guards the terminal write with ``_fail``, and every
guard is refunded:

* Designs were produced but none uploaded: bucket ``"storage"``, which maps
  to ``infra_crash``.
* esmfold folded no design at all: bucket ``"pipeline"``, which maps to
  ``tool_error``, the bucket boltz2's zero-design guard uses
  (``main`` in ``tools/boltz2/run_pipeline.py``).
* IgGM and OpenDDE produced no structure at all: ``main`` in each fails the
  empty output directory under bucket ``"run"`` / ``"output"``, neither in
  ``_ERROR_BUCKET_TO_FAILURE_CLASS``, so both classify ``unclassified``.

Every class above is in ``_REFUNDED_FAILURE_CLASSES`` (``shared/jobs.py``),
whose wallet hold ``_settle_wallet_hold_for_completed_job`` releases in full.

Every uploads-failed case here is a PAIR — the zero-design run must FAIL, and
a run with one surviving design must still COMPLETE. Without the positive
control a guard that fired unconditionally would pass the failure half just as
well.

A refunded run still debits the Workspace GPU-time cap for the seconds it used
(Leo, 2026-09-24), so every guard here passes ``runtime_seconds``.
``_interpret_pipeline_return`` (``gpu/modal_client.py``) reads that key off
the FAILED arm as the job's ``gpu_seconds_used`` — ``result`` is ``None`` on
that arm, so the payload-scan fallback in ``shared/jobs.py::complete_job``
cannot recover it — and ``_charge_workspace_for_completed_job``
(``shared/jobs.py``) charges any failed job that carries it, whatever its
failure class, and returns without charging when it is absent or zero.
``test_zero_design_run_refunds_the_wallet_and_debits_the_cap`` drives each
guard's payload through both halves.

``_assert_failed`` reads the bucket back off each guard's own payload rather
than restating it, so a guard that moved to another bucket fails here.

That classifier is only reachable because the poll path carries the bucket.
All three tools return their terminal payload inline as ``smoke_result``
(``tools/<tool>/modal_app.py``) and POST no terminal webhook, so a single
job's poll, ``/jobs/<id>/status.json`` (``blueprints/jobs.py::job_status``),
is what terminalises it — and that route used to rebuild the error dict with a literal
``"bucket": "pipeline"``, which classifies as ``tool_error`` and releases the
hold in full. ``test_guard_bucket_survives_the_poll_route_to_the_wallet``
drives a real guard payload through ``_interpret_pipeline_return`` and that
route instead of calling the classifier directly, so a bucket dropped anywhere
in between fails here.
"""

from __future__ import annotations

import base64
import itertools
import json
from types import SimpleNamespace

import pytest

from tools.esmfold import run_pipeline as esm_rp
from tools.iggm import run_pipeline as iggm_rp
from tools.opendde import run_pipeline as odd_rp

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _capture(monkeypatch, module) -> list[dict]:
    """Trap the terminal payload instead of writing /tmp/smoke_results.json."""
    written: list[dict] = []
    monkeypatch.setattr(module, "_write_result", written.append)
    return written


def _clock(monkeypatch, module) -> None:
    """Advance the module's clock 60 s per read so ``main``'s runtime is > 0."""
    monkeypatch.setattr(
        module, "time", SimpleNamespace(time=itertools.count(1000, 60).__next__)
    )


def _assert_failed(
    written: list[dict], failure_class: str, check: str = "no_designs"
) -> None:
    assert len(written) == 1, f"expected exactly one terminal write, got {written}"
    payload = written[0]
    assert payload["status"] == "FAILED", payload
    assert payload["error"]["check"] == check, payload["error"]
    runtime = payload.get("runtime_seconds")
    assert isinstance(runtime, int) and runtime > 0, (
        f"runtime_seconds={runtime!r}: a zero-design run must still report "
        "the GPU time it burned, or the Workspace cap is not debited for it"
    )
    # The bucket comes from the guard, not from this file.
    from shared.jobs import classify_terminal_state, is_billed_failure_class

    got = classify_terminal_state(status="failed", error=payload["error"])
    assert got == failure_class, (
        f"bucket {payload['error']['bucket']!r} classifies as {got!r}, "
        f"not {failure_class!r}"
    )
    assert not is_billed_failure_class(got), got


def _assert_completed(written: list[dict], n_designs: int) -> None:
    assert len(written) == 1, f"expected exactly one terminal write, got {written}"
    payload = written[0]
    assert payload["status"] == "COMPLETED", payload
    assert len(payload["designs"]) == n_designs, payload["designs"]


# ---------------------------------------------------------------------------
# ESMFold — batch fold path
# ---------------------------------------------------------------------------


_SEQ = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"


def _esmfold_batch(monkeypatch, *, fold_ok: bool, upload_ok: bool = True):
    """Drive ``_run_batch_folds`` with the model + uploads stubbed out."""
    written = _capture(monkeypatch, esm_rp)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)  # silences send_heartbeat
    monkeypatch.setattr(esm_rp, "load_esmfold", lambda: (None, None))

    def _fold(tokenizer, model, name, seq, raw_dir=None, raw_stem=""):
        if not fold_ok:
            raise RuntimeError("fold blew up on the GPU")
        return {
            "pdb_b64": base64.b64encode(b"ATOM      1  N   MET A   1\n").decode(),
            "mean_plddt": 88.1,
            "ptm": 0.8,
            "total_length": len(seq),
        }

    monkeypatch.setattr(esm_rp, "_fold_record", _fold)
    monkeypatch.setattr(
        esm_rp,
        "request_upload_urls",
        lambda ep, tok, keys: {k: "https://up" for k in keys},
    )
    def _upload(url, data):
        if not upload_ok:
            raise RuntimeError("storage PUT refused")

    monkeypatch.setattr(esm_rp, "upload_pdb", _upload)

    payload = {"job_token": "t", "upload_urls_endpoint": "https://example/upload"}
    records = [{"name": "d0", "sequence": _SEQ}]
    return written, payload, records


def test_esmfold_batch_with_zero_folded_designs_fails_refunded(monkeypatch, tmp_path):
    written, payload, records = _esmfold_batch(monkeypatch, fold_ok=False)
    with pytest.raises(SystemExit):
        esm_rp._run_batch_folds(payload, records, 0.0, tmp_path)
    _assert_failed(written, "tool_error")


def test_esmfold_batch_with_folded_but_unuploaded_designs_fails_refunded(
    monkeypatch, tmp_path
):
    written, payload, records = _esmfold_batch(
        monkeypatch, fold_ok=True, upload_ok=False
    )
    with pytest.raises(SystemExit):
        esm_rp._run_batch_folds(payload, records, 0.0, tmp_path)
    _assert_failed(written, "infra_crash")


def test_esmfold_batch_with_a_surviving_design_still_completes(monkeypatch, tmp_path):
    written, payload, records = _esmfold_batch(monkeypatch, fold_ok=True)
    esm_rp._run_batch_folds(payload, records, 0.0, tmp_path)
    _assert_completed(written, 1)


# ---------------------------------------------------------------------------
# OpenDDE — every predicted structure fails to upload
# ---------------------------------------------------------------------------


def _opendde_main(monkeypatch, tmp_path, *, upload_ok: bool, produced: bool = True):
    written = _capture(monkeypatch, odd_rp)
    _clock(monkeypatch, odd_rp)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)

    # Satisfy the real weights preflight without a Modal Volume.
    ckpt_dir = tmp_path / "root" / "checkpoint"
    ckpt_dir.mkdir(parents=True)
    (ckpt_dir / odd_rp.CHECKPOINTS["general"]).write_bytes(b"weights")
    monkeypatch.setattr(odd_rp, "OPENDDE_ROOT", str(tmp_path / "root"))

    monkeypatch.setattr(
        odd_rp,
        "parse_payload",
        lambda: {
            "job_token": "t",
            "upload_urls_endpoint": "https://example/upload",
            "job_spec": {"preset": "general", "spec": [{"id": "A"}], "sample": 1},
        },
    )
    monkeypatch.setattr(odd_rp, "run_opendde", lambda *a, **k: 0)
    monkeypatch.setattr(odd_rp, "archive_raw_outputs", lambda *a, **k: None)

    def _collect(out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        if not produced:
            return []
        path = out_dir / "pred_0.pdb"
        path.write_bytes(b"ATOM      1  N   MET A   1\n")
        return [path]

    monkeypatch.setattr(odd_rp, "collect_structures", _collect)
    monkeypatch.setattr(odd_rp, "_read_score_json", lambda sp: {"ranking_score": 0.9})

    def _urls(ep, tok, keys):
        if not upload_ok:
            raise RuntimeError("presign endpoint refused")
        return {k: "https://up" for k in keys}

    monkeypatch.setattr(odd_rp, "request_upload_urls", _urls)
    monkeypatch.setattr(odd_rp, "upload_structure", lambda url, data, ctype: None)
    return written


def test_opendde_with_zero_uploaded_predictions_fails_refunded(monkeypatch, tmp_path):
    written = _opendde_main(monkeypatch, tmp_path, upload_ok=False)
    with pytest.raises(SystemExit):
        odd_rp.main()
    _assert_failed(written, "infra_crash")


def test_opendde_with_no_predicted_structures_fails_refunded(monkeypatch, tmp_path):
    written = _opendde_main(monkeypatch, tmp_path, upload_ok=True, produced=False)
    with pytest.raises(SystemExit):
        odd_rp.main()
    _assert_failed(written, "unclassified", check="structures")


def test_opendde_with_a_surviving_prediction_still_completes(monkeypatch, tmp_path):
    written = _opendde_main(monkeypatch, tmp_path, upload_ok=True)
    odd_rp.main()
    _assert_completed(written, 1)


# ---------------------------------------------------------------------------
# IgGM — every design PDB fails to upload
# ---------------------------------------------------------------------------


def _iggm_main(monkeypatch, *, upload_ok: bool, produced: bool = True):
    written = _capture(monkeypatch, iggm_rp)
    _clock(monkeypatch, iggm_rp)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)

    monkeypatch.setattr(
        iggm_rp,
        "parse_payload",
        lambda: {
            "job_token": "t",
            "upload_urls_endpoint": "https://example/upload",
            "input_presigned_url": "https://example/antigen.pdb",
            "job_spec": {
                "preset": "complex_prediction",
                "run_task": "design",
                "antibody_fasta": [{"header": "H", "sequence": "QVQLV"}],
                "antigen_chain": "A",
                "num_samples": 1,
            },
        },
    )
    monkeypatch.setattr(iggm_rp, "download_antigen_pdb", lambda url, dest: dest)
    monkeypatch.setattr(
        iggm_rp,
        "antigen_chain_info",
        lambda path, chain: {"seq": "MQIFV", "resnum_to_pos": {}, "n_res": 5},
    )
    monkeypatch.setattr(iggm_rp, "write_fasta", lambda *a, **k: None)
    monkeypatch.setattr(iggm_rp, "run_iggm", lambda *a, **k: 0)
    monkeypatch.setattr(iggm_rp, "collect_artifacts", lambda out_dir: [])
    monkeypatch.setattr(iggm_rp, "_ship_raw", lambda *a, **k: None)
    monkeypatch.setattr(
        iggm_rp,
        "epitope_contacts",
        lambda text, n, pos: {"n_contacted": 0, "n_epitope": 0, "contacted": []},
    )

    def _collect(out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        if not produced:
            return []
        path = out_dir / "sample_0.pdb"
        path.write_text("ATOM      1  N   MET A   1\n")
        return [path]

    monkeypatch.setattr(iggm_rp, "collect_design_pdbs", _collect)

    def _urls(ep, tok, keys):
        if not upload_ok:
            raise RuntimeError("presign endpoint refused")
        return {k: "https://up" for k in keys}

    monkeypatch.setattr(iggm_rp, "request_upload_urls", _urls)
    monkeypatch.setattr(iggm_rp, "upload_file", lambda url, data, ctype: None)
    return written


def test_iggm_with_zero_uploaded_designs_fails_refunded(monkeypatch):
    written = _iggm_main(monkeypatch, upload_ok=False)
    with pytest.raises(SystemExit):
        iggm_rp.main()
    _assert_failed(written, "infra_crash")


def test_iggm_uploads_no_artifacts_when_every_design_upload_failed(monkeypatch):
    written = _iggm_main(monkeypatch, upload_ok=True)
    requested: list[str] = []

    def _urls(ep, tok, keys):
        requested.extend(keys)
        if any(k.endswith(".pdb") for k in keys):
            raise RuntimeError("presign endpoint refused")
        return {k: "https://up" for k in keys}

    monkeypatch.setattr(iggm_rp, "request_upload_urls", _urls)
    monkeypatch.setattr(
        iggm_rp, "collect_artifacts",
        lambda out_dir: [out_dir / "designs.fasta"],
    )
    with pytest.raises(SystemExit):
        iggm_rp.main()
    _assert_failed(written, "infra_crash")
    assert not [k for k in requested if k.startswith("artifacts/")], requested


def test_iggm_with_no_design_pdbs_fails_refunded(monkeypatch):
    written = _iggm_main(monkeypatch, upload_ok=True, produced=False)
    with pytest.raises(SystemExit):
        iggm_rp.main()
    _assert_failed(written, "unclassified", check="output")


def test_iggm_with_a_surviving_design_still_completes(monkeypatch):
    written = _iggm_main(monkeypatch, upload_ok=True)
    iggm_rp.main()
    _assert_completed(written, 1)


# ---------------------------------------------------------------------------
# The billing wiring the guards depend on
# ---------------------------------------------------------------------------


def test_fail_omits_runtime_seconds_when_no_gpu_time_was_burned(monkeypatch):
    """Pre-run failures must NOT invent a runtime, or they would bill for one."""
    written = _capture(monkeypatch, esm_rp)
    with pytest.raises(SystemExit):
        esm_rp._fail("preflight", "weights", "checkpoint missing")
    assert "runtime_seconds" not in written[0], written[0]


def _zero_design_payload(case: str, monkeypatch, tmp_path) -> dict:
    """Run the tool's own guard and hand back the terminal payload it wrote."""
    if case.startswith("esmfold"):
        written, payload, records = _esmfold_batch(
            monkeypatch,
            fold_ok=case == "esmfold-upload",
            upload_ok=case != "esmfold-upload",
        )
        with pytest.raises(SystemExit):
            esm_rp._run_batch_folds(payload, records, 0.0, tmp_path)
    elif case.startswith("opendde"):
        written = _opendde_main(
            monkeypatch, tmp_path,
            upload_ok=False, produced=case != "opendde-empty",
        )
        with pytest.raises(SystemExit):
            odd_rp.main()
    else:
        written = _iggm_main(
            monkeypatch, upload_ok=False, produced=case != "iggm-empty"
        )
        with pytest.raises(SystemExit):
            iggm_rp.main()
    return written[0]


def _drive_poll_route(monkeypatch, poll: dict) -> dict:
    """GET /jobs/<id>/status.json against ``poll`` and trap complete_job's args."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    captured: dict = {}
    row = SimpleNamespace(
        id="job-1",
        status="running",
        tool="esmfold",
        preset="standard",
        inputs={},
        result=None,
        error=None,
        gpu_seconds_used=None,
        started_at=None,
        modal_function_call_id="fc-abc123",
    )

    def _complete_job(job_id, *, terminal_status, **kw):
        captured.update(kw, terminal_status=terminal_status)
        row.status = terminal_status
        return row

    monkeypatch.setattr(
        "blueprints.jobs.get_job", lambda job_id, user_id=None, **_kw: row
    )
    monkeypatch.setattr("blueprints.jobs.complete_job", _complete_job)
    monkeypatch.setattr(
        "blueprints.jobs.load_user_context",
        lambda: SimpleNamespace(
            user_id="u-1", tier="free", balance=10, email="user@example.com"
        ),
    )

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.modal_client = SimpleNamespace(poll=lambda _fc: poll)
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_email"] = "user@example.com"
    resp = client.get("/jobs/job-1/status.json")
    assert resp.status_code == 200, resp.data
    return captured


_CASES = [
    ("esmfold-nofold", "tool_error"),
    ("esmfold-upload", "infra_crash"),
    ("opendde", "infra_crash"),
    ("opendde-empty", "unclassified"),
    ("iggm", "infra_crash"),
    ("iggm-empty", "unclassified"),
]


@pytest.mark.parametrize(("case", "failure_class"), _CASES)
def test_guard_bucket_survives_the_poll_route_to_the_wallet(
    case, failure_class, monkeypatch, tmp_path
):
    """End to end: guard payload -> modal_client -> status route -> classifier."""
    from gpu.modal_client import _interpret_pipeline_return
    from shared.jobs import classify_terminal_state

    smoke = json.loads(json.dumps(_zero_design_payload(case, monkeypatch, tmp_path)))
    poll = _interpret_pipeline_return({"exit_code": 1, "smoke_result": smoke})
    captured = _drive_poll_route(monkeypatch, poll)

    assert captured["terminal_status"] == "failed"
    assert captured["gpu_seconds_used"] == smoke["runtime_seconds"] > 0
    got = classify_terminal_state(status="failed", error=captured["error"])
    assert got == failure_class, (
        f"the route handed the wallet {captured['error']!r} -> {got!r}; "
        f"the guard reported bucket {smoke['error']['bucket']!r}, so the bucket "
        "was dropped or changed between the pipeline and the billing decision"
    )


def test_poll_route_still_falls_back_to_pipeline_without_a_bucket(monkeypatch):
    """A failed poll carrying no payload keeps the old literal bucket.

    ``_interpret_pipeline_return`` reports ``error_bucket`` as ``None`` when the
    FunctionCall returned no ``smoke_result`` at all (webhook-delivery failure,
    or a nonzero exit with nothing written) -- that case must keep classifying
    as ``tool_error`` rather than falling through to ``unclassified``.
    """
    from gpu.modal_client import _interpret_pipeline_return
    from shared.jobs import classify_terminal_state

    poll = _interpret_pipeline_return({"exit_code": 137, "smoke_result": None})
    assert poll["status"] == "failed"
    assert poll.get("error_bucket") is None
    captured = _drive_poll_route(monkeypatch, poll)
    assert captured["error"]["bucket"] == "pipeline"
    assert classify_terminal_state(status="failed", error=captured["error"]) == (
        "tool_error"
    )


@pytest.mark.parametrize(("case", "failure_class"), _CASES)
def test_zero_design_run_refunds_the_wallet_and_debits_the_cap(
    case, failure_class, monkeypatch, tmp_path
):
    """Guard payload -> modal_client -> real complete_job: both money halves.

    Only the database row, the workspace store and the wallet RPCs are faked;
    ``mark_failed``, ``classify_terminal_state``,
    ``_charge_workspace_for_completed_job`` and
    ``_settle_wallet_hold_for_completed_job`` all run for real.
    """
    from gpu.modal_client import _interpret_pipeline_return
    from shared import jobs as jobs_mod
    from shared import wallet as wallet_mod
    from shared import workspaces as ws_mod

    smoke = json.loads(json.dumps(_zero_design_payload(case, monkeypatch, tmp_path)))
    poll = _interpret_pipeline_return({"exit_code": 1, "smoke_result": smoke})
    assert poll["status"] == "failed"

    row = {
        "id": "job-1",
        "user_id": "u-1",
        "tool": "esmfold",
        "preset": "standard",
        "status": "running",
        "job_token": "t" * 64,
        "inputs": {
            "_workspace": {"target_pdb_id": "1ABC"},
            "_wallet": {"hold_tx_id": "tx-hold"},
        },
    }

    def _cas(job_id, payload, *, allowed_current):
        if row["status"] not in allowed_current:
            return False
        row.update(payload)
        return True

    monkeypatch.setattr(jobs_mod, "_cas_update", _cas)
    monkeypatch.setattr(
        jobs_mod, "get_job", lambda jid, **kw: jobs_mod.ToolJob.from_row(row)
    )
    monkeypatch.setattr(jobs_mod, "_send_completion_email", lambda job: None)

    ws = SimpleNamespace(id="ws-1", modal_spent_usd=0, modal_cap_usd=100)
    charges: list[dict] = []
    monkeypatch.setattr(ws_mod, "get_active_workspace", lambda uid, pdb: ws)
    monkeypatch.setattr(
        ws_mod,
        "charge_for_job",
        lambda uid, pdb, **kw: charges.append(kw) or ws,
    )
    monkeypatch.setattr(ws_mod, "crossed_warn_threshold", lambda *a: False)

    released: list[str] = []
    settled: list[str] = []
    monkeypatch.setattr(
        wallet_mod, "release_hold", lambda tx, reason="": released.append(tx) or True
    )
    monkeypatch.setattr(
        wallet_mod, "settle_hold", lambda tx, **kw: settled.append(tx) or True
    )

    jobs_mod.complete_job(
        "job-1",
        terminal_status="failed",
        error={"bucket": poll["error_bucket"], "detail": poll["error"]},
        gpu_seconds_used=poll["gpu_seconds_used"],
    )

    assert row["failure_class"] == failure_class
    assert released == ["tx-hold"] and settled == [], (
        f"wallet: released={released} settled={settled}; a refunded class "
        "must release the hold in full and never settle it"
    )
    assert [c["gpu_seconds"] for c in charges] == [smoke["runtime_seconds"]], (
        f"Workspace cap charges {charges}; the run's GPU time "
        f"({smoke['runtime_seconds']}s) must be debited exactly once"
    )
