"""An upload-only failure must not be reported as a fold failure.

af2, colabfold and esmfold each count a design only after its PDB has been
uploaded, so ``designs_out`` was the sole record that anything folded: a run
where every fold succeeded and every PUT failed left it empty, which is
indistinguishable from a run that folded nothing. Unlike boltz2 -- whose guard
turns that into a FAILED "all N designs failed", pinned by
``tests/test_boltz2_smoke.py::TestZeroDesignsFailsTheJob`` -- af2 and
colabfold complete green with zero designs on that run when the fold step
exits 0, so what is pinned for them is the count and the message rather than
the exit status. esmfold fails a zero-design run, with bucket ``storage`` when
something folded and ``no_yield`` when nothing did
(``tests/test_zero_design_runs_fail.py``); what is pinned for it here is that
the failure names the upload hop only when something folded.

The negative controls (nothing folded) are what make the rest mean anything: a
count or a message that named the upload hop unconditionally would satisfy the
positive assertions while lying about a run where no structure ever existed.

Billing is deliberately unchanged by the fix under test, so every case below
also asserts ``status`` and, on a COMPLETED result, ``designs_completed`` and
``n_failures``.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from tools.af2 import run_pipeline as af2_rp
from tools.colabfold import run_pipeline as colabfold_rp
from tools.esmfold import run_pipeline as esmfold_rp

# af2 and colabfold share the consolidated-subprocess shape: one
# colabfold_batch over a multi-record FASTA, with _stream_consolidated_results
# calling _dispatch per finished design. esmfold's loop is sequential and
# in-process, so it is driven separately below.
CONSOLIDATED = [pytest.param(af2_rp, id="af2"), pytest.param(colabfold_rp, id="colabfold")]

_SEQ = "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQ"
_RECORDS = [{"name": "alpha", "sequence": _SEQ}, {"name": "beta", "sequence": _SEQ}]
_PAYLOAD = {"job_token": "tok", "upload_urls_endpoint": "https://example.invalid/urls"}

# colabfold's _dispatch rejects a PDB under 200 bytes as a stub, and the
# folded-count is incremented after that branch, so the fixture has to be a
# structure that clears it.
_PDB = "".join(
    f"ATOM  {i:5d}  CA  ALA A{i:4d}      {i:6.3f}   0.000   0.000  1.00 90.00           C\n"
    for i in range(1, 13)
) + "END\n"

_UPLOAD_DIED = RuntimeError("upload failed: HTTP 503")


def _arrange_consolidated(
    rp, tmp_path, monkeypatch, *, upload_exc=None, exit_code=0, folds=True,
):
    """Stub every I/O edge of ``_run_batch`` and return the result-file path.

    ``folds`` decides whether the streamer hands _dispatch a finished design at
    all; ``upload_exc``, when given, is raised by every ``upload_pdb``.
    ``folds=True`` plus ``upload_exc`` is the storage-outage shape.
    """
    result_file = tmp_path / "smoke_results.json"
    monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        rp,
        "request_upload_urls",
        lambda endpoint, token, keys: {k: "https://example.invalid/put" for k in keys},
    )

    def _upload(url, data):
        if upload_exc is not None:
            raise upload_exc

    monkeypatch.setattr(rp, "upload_pdb", _upload)
    # af2 tars the work dir, colabfold tars the TemporaryDirectory; both from
    # the same finally, and neither is under test here.
    for name in ("archive_raw", "archive_work_dir"):
        if hasattr(rp, name):
            monkeypatch.setattr(rp, name, lambda *a, **k: None)
    if hasattr(rp, "_hhsearch_available"):
        monkeypatch.setattr(rp, "_hhsearch_available", lambda: True)
    monkeypatch.setattr(
        rp, "_run_colabfold_consolidated", lambda **kw: (object(), kw["workdir"] / "out")
    )

    def _stream(*, process, out_dir, record_map, on_design_ready, timeout=None):
        if folds:
            out_dir.mkdir(parents=True, exist_ok=True)
            for safe_name in record_map:
                scores = out_dir / f"{safe_name}_scores.json"
                scores.write_text(
                    json.dumps({"plddt": [90.0] * 12, "ptm": 0.8, "iptm": 0.7})
                )
                pdb = out_dir / f"{safe_name}.pdb"
                pdb.write_text(_PDB)
                on_design_ready(safe_name, scores, pdb)
        return exit_code

    monkeypatch.setattr(rp, "_stream_consolidated_results", _stream)
    return result_file


@pytest.mark.parametrize("rp", CONSOLIDATED)
class TestConsolidatedBatch:
    def test_folded_but_unuploaded_is_not_reported_as_a_fold_failure(
        self, rp, tmp_path, monkeypatch,
    ):
        """The point: every fold worked, every PUT died, nothing was delivered.

        Before the fix ``designs_completed`` 0 was the only count and the
        closing log line called it "designs folded", so a storage outage read
        as the model failing on all of them.
        """
        result_file = _arrange_consolidated(
            rp, tmp_path, monkeypatch, upload_exc=_UPLOAD_DIED
        )

        rp._run_batch(dict(_PAYLOAD), list(_RECORDS), time.time())

        result = json.loads(result_file.read_text())
        assert result["designs_folded"] == 2, (
            "both designs folded; only the delivery hop failed")
        assert result["designs_completed"] == 0

        # Unchanged on purpose. This path has no zero-design abort, so the run
        # still completes and still bills; the fix is to the counts, not to
        # the verdict.
        assert result["status"] == "COMPLETED"
        assert result["n_failures"] == 2

    def test_a_run_that_folded_nothing_still_says_so(self, rp, tmp_path, monkeypatch):
        """Negative control for the count.

        A ``designs_folded`` that stood in for "records we were handed" would
        pass the test above while lying about this run.
        """
        result_file = _arrange_consolidated(rp, tmp_path, monkeypatch, folds=False)

        rp._run_batch(dict(_PAYLOAD), list(_RECORDS), time.time())

        result = json.loads(result_file.read_text())
        assert result["designs_folded"] == 0
        assert result["designs_completed"] == 0
        assert result["status"] == "COMPLETED"
        assert result["n_failures"] == 2

    def test_a_healthy_run_reports_both_counts(self, rp, tmp_path, monkeypatch):
        """The count that separates the two failure shapes must exist on the
        success path too, not only where it is convenient."""
        result_file = _arrange_consolidated(rp, tmp_path, monkeypatch)

        rp._run_batch(dict(_PAYLOAD), list(_RECORDS), time.time())

        result = json.loads(result_file.read_text())
        assert result["designs_folded"] == 2
        assert result["designs_completed"] == 2
        assert result["status"] == "COMPLETED"
        assert result["n_failures"] == 0

    def test_nonzero_exit_detail_names_the_delivery_hop(
        self, rp, tmp_path, monkeypatch,
    ):
        """The one terminal message these two tools have.

        colabfold_batch exiting non-zero with nothing delivered is the only
        path here that reaches _fail, and its detail is the channel that
        survives to the user (gpu/modal_client.py::_interpret_pipeline_return
        drops the result on the FAILED arm and keeps the stringified error).
        """
        result_file = _arrange_consolidated(
            rp, tmp_path, monkeypatch, upload_exc=_UPLOAD_DIED, exit_code=1
        )

        with pytest.raises(SystemExit) as excinfo:
            rp._run_batch(dict(_PAYLOAD), list(_RECORDS), time.time())

        assert excinfo.value.code == 1
        result = json.loads(result_file.read_text())
        detail = result["error"]["detail"]
        assert "2 of 2 designs folded but 0 uploaded" in detail, (
            f"detail must separate the hops; got {detail!r}")
        assert "with zero completed designs" not in detail, (
            f"the false claim this test exists to kill is back: {detail!r}")
        assert result["status"] == "FAILED"
        assert result["error"]["check"] == "exit"

    def test_nonzero_exit_that_folded_nothing_keeps_the_plain_detail(
        self, rp, tmp_path, monkeypatch,
    ):
        """Negative control for the message, and the reason the split exists.

        Nothing folded here, so naming the upload hop would be the new lie.
        """
        result_file = _arrange_consolidated(
            rp, tmp_path, monkeypatch, exit_code=1, folds=False
        )

        with pytest.raises(SystemExit):
            rp._run_batch(dict(_PAYLOAD), list(_RECORDS), time.time())

        detail = json.loads(result_file.read_text())["error"]["detail"]
        assert "with zero completed designs" in detail, f"got {detail!r}"
        assert "folded but 0 uploaded" not in detail, (
            f"no design ever folded on this run: {detail!r}")


def _arrange_esmfold(tmp_path, monkeypatch, *, upload_exc=None, folds=True):
    """Same arrangement for esmfold's sequential loop.

    ``_fold_record`` stands in for the model: it returns only after
    ``reject_stub`` has passed, which is why the fix counts a fold there.
    """
    rp = esmfold_rp
    result_file = tmp_path / "smoke_results.json"
    monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(rp, "load_esmfold", lambda: (None, None))
    monkeypatch.setattr(
        rp,
        "request_upload_urls",
        lambda endpoint, token, keys: {k: "https://example.invalid/put" for k in keys},
    )

    def _upload(url, data):
        if upload_exc is not None:
            raise upload_exc

    monkeypatch.setattr(rp, "upload_pdb", _upload)

    def _fold(tokenizer, model, name, seq, raw_dir=None, raw_stem=""):
        if not folds:
            raise RuntimeError("CUDA OOM")
        return {
            "pdb_b64": base64.b64encode(_PDB.encode("utf-8")).decode("ascii"),
            "mean_plddt": 90.0,
            "ptm": 0.8,
            "total_length": 12,
        }

    monkeypatch.setattr(rp, "_fold_record", _fold)
    return result_file


class TestEsmfoldBatch:
    def test_folded_but_unuploaded_is_not_reported_as_a_fold_failure(
        self, tmp_path, monkeypatch,
    ):
        result_file = _arrange_esmfold(tmp_path, monkeypatch, upload_exc=_UPLOAD_DIED)

        with pytest.raises(SystemExit):
            esmfold_rp._run_batch_folds(
                dict(_PAYLOAD), list(_RECORDS), time.time(), tmp_path
            )

        result = json.loads(result_file.read_text())
        assert result["status"] == "FAILED"
        assert result["error"]["bucket"] == "storage"
        assert "runtime_seconds" not in result, result
        detail = result["error"]["detail"]
        assert "2 of 2 designs folded but 0 uploaded" in detail, detail

    def test_a_run_that_folded_nothing_still_says_so(self, tmp_path, monkeypatch):
        result_file = _arrange_esmfold(tmp_path, monkeypatch, folds=False)

        with pytest.raises(SystemExit):
            esmfold_rp._run_batch_folds(
                dict(_PAYLOAD), list(_RECORDS), time.time(), tmp_path
            )

        result = json.loads(result_file.read_text())
        assert result["status"] == "FAILED"
        assert result["error"]["bucket"] == "no_yield"
        detail = result["error"]["detail"]
        assert "none of 2 designs folded (2 failures)" in detail, detail
        assert "folded but 0 uploaded" not in detail, detail

    def test_a_healthy_run_reports_both_counts(self, tmp_path, monkeypatch):
        result_file = _arrange_esmfold(tmp_path, monkeypatch)

        esmfold_rp._run_batch_folds(
            dict(_PAYLOAD), list(_RECORDS), time.time(), tmp_path
        )

        result = json.loads(result_file.read_text())
        assert result["designs_folded"] == 2
        assert result["designs_completed"] == 2
        assert result["status"] == "COMPLETED"
        assert result["n_failures"] == 0
