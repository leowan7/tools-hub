"""The public ColabFold MSA server timing out is named as such, not as a parser fault.

colabfold_batch logs "Could not get MSA/templates" and exits 0 when the server
times out (docs/qa/AF2-FAILURES-2026-10-13.md). The log lines below are from
the archived log.txt of af2 job d8aa9b50, with the record name replaced.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from shared.jobs import (
    _FAILURE_RULES,
    _REFUNDED_FAILURE_CLASSES,
    classify_terminal_state,
    failure_advice,
    is_billed_failure_class,
)
from tests.test_failed_run_page import _job, _poll_error
from tools.af2 import run_pipeline as af2_rp
from tools.colabfold import run_pipeline as colabfold_rp

_OUTAGE_LOG = (
    "2026-10-13 05:38:48,847 Error: HTTPSConnectionPool(host='api.colabfold.com', "
    "port=443): Read timed out.\n"
    "2026-10-13 05:38:53,849 Could not get MSA/templates for query_1: "
    "HTTPSConnectionPool(host='api.colabfold.com', port=443): Read timed out.\n"
    "2026-10-13 05:38:53,859 Done\n"
)
_DETAIL = ("Could not get MSA/templates for query_1: HTTPSConnectionPool("
           "host='api.colabfold.com', port=443): Read timed out.")


class _Failed(Exception):
    pass


def _run(module, tmp_path, monkeypatch, log: str):
    """Call the module's run_colabfold with a fake colabfold_batch that exits 0."""
    def fake_run(cmd, **_kw):
        out_dir = next(a for a in cmd if str(a).endswith("_out"))
        (tmp_path / out_dir).joinpath("log.txt").write_text(log)
        return SimpleNamespace(returncode=0)

    def fake_fail(bucket, check, detail, runtime_seconds=None):
        raise _Failed(bucket, check, detail)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_fail", fake_fail)
    fasta = tmp_path / "in.fasta"
    if module is af2_rp:
        return module.run_colabfold(fasta=fasta, model_preset="monomer",
                                    num_recycles=1, use_templates=True,
                                    use_msa=True, workdir=tmp_path)
    return module.run_colabfold(fasta_path=fasta, num_recycles=1,
                                use_templates=False, workdir=tmp_path)


@pytest.mark.parametrize("module", [af2_rp, colabfold_rp], ids=["af2", "colabfold"])
class TestDetection:
    def test_outage_fails_as_msa_server(self, module, tmp_path, monkeypatch):
        with pytest.raises(_Failed) as exc:
            _run(module, tmp_path, monkeypatch, _OUTAGE_LOG)
        assert exc.value.args == ("tool-invocation", "msa_server", _DETAIL)

    def test_clean_log_returns_out_dir(self, module, tmp_path, monkeypatch):
        out = _run(module, tmp_path, monkeypatch, "2026-10-13 08:37:00,000 Done\n")
        assert out.name.endswith("_out")

    def test_non_server_exception_keeps_the_parser_path(self, module, tmp_path, monkeypatch):
        log = "Could not get MSA/templates for query_1: hhsearch not found\n"
        assert _run(module, tmp_path, monkeypatch, log).name.endswith("_out")


class TestAdviceAndBilling:
    def test_advice_beats_the_timeout_rule(self):
        error = _poll_error("tool-invocation", "msa_server", _DETAIL)
        timeout_rule = next(p for k, p, _c, _f in _FAILURE_RULES if k == "timeout")
        # The detail does match the timeout rule, so only the order saves it.
        assert timeout_rule.search(error["detail"])
        advice = failure_advice(_job(tool="af2", error=error))
        assert advice["kind"] == "msa_server"
        assert "sequence-search service" in advice["cause"]
        assert "crop the target" not in advice["fix"]
        assert advice["retry_unchanged"] is False

    def test_webhook_shape_gets_the_same_advice(self):
        error = {"bucket": "tool-invocation", "check": "msa_server", "detail": _DETAIL}
        assert failure_advice(_job(tool="af2", error=error))["kind"] == "msa_server"

    def test_still_refunded(self):
        # classify_terminal_state reads only the bucket; tool-invocation is not in its table.
        cls = classify_terminal_state(
            status="failed",
            error=_poll_error("tool-invocation", "msa_server", _DETAIL))
        assert cls in _REFUNDED_FAILURE_CLASSES
        assert not is_billed_failure_class(cls)
