"""proteina's search keepalive: a heartbeat every interval while a chunk runs."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

from tests.test_proteina_delivery import _drive_design_loop
from tools.proteina import run_pipeline as rp
from webhooks import modal as modal_webhook

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def test_keepalive_beats_while_open_and_stops_on_exit(monkeypatch):
    beats = []
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: beats.append((a, k)))
    with rp.keepalive("https://hub/webhooks/modal/j/t", "job-1", 3, 8, interval=0.01):
        time.sleep(0.2)
    after_exit = len(beats)
    time.sleep(0.1)
    assert after_exit >= 2
    assert len(beats) == after_exit
    assert beats[0] == (
        ("https://hub/webhooks/modal/j/t", "job-1"),
        {"stage": "searching", "designs_completed": 3, "designs_total": 8},
    )


def test_keepalive_stops_when_the_search_raises(monkeypatch):
    beats = []
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: beats.append(k))
    ka = rp.keepalive("u", "job-1", 0, 8, interval=0.01)
    with pytest.raises(TimeoutError):
        with ka:
            raise TimeoutError
    assert not ka._thread.is_alive()


def test_the_search_runs_inside_a_keepalive(tmp_path, monkeypatch):
    seen = []

    class FakeKeepalive:
        def __init__(self, *args):
            seen.append(("open", args))

        def __enter__(self):
            inner = rp.run_streaming
            self._inner = inner
            rp.run_streaming = lambda *a, **k: seen.append("search") or inner(*a, **k)

        def __exit__(self, *exc):
            rp.run_streaming = self._inner
            seen.append("close")

    monkeypatch.setattr(rp, "keepalive", FakeKeepalive)
    _drive_design_loop(tmp_path, monkeypatch, endpoint="https://hub/upload",
                       webhook_url="https://hub/webhooks/modal/job-deliver/tok")
    assert seen == [
        ("open", ("https://hub/webhooks/modal/job-deliver/tok", "job-deliver", 0, 8)),
        "search", "close",
    ]


def test_the_hub_meters_the_body_the_keepalive_posts():
    posted = {}

    def fake_post(url, json, timeout):
        posted.update(url=url, body=json)
        return SimpleNamespace(status_code=200)

    with patch.object(rp.requests, "post", fake_post):
        rp.send_heartbeat("https://hub/webhooks/modal/job-1/tok", "job-1",
                          stage="searching", designs_completed=2, designs_total=8)
    assert posted["url"] == "https://hub/webhooks/heartbeat"

    started = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    job = SimpleNamespace(id="job-1", status="running", started_at=started,
                          inputs={"_progress": {}}, job_token="tok")
    app = Flask(__name__)
    modal_webhook.register_modal_webhooks(app)
    with patch.object(modal_webhook, "get_job", return_value=job), \
         patch.object(modal_webhook, "_append_heartbeat_state") as append, \
         patch.object(modal_webhook, "_run_mid_run_monitor") as monitor:
        resp = app.test_client().post("/webhooks/heartbeat", json=posted["body"])
    assert resp.status_code == 200
    append.assert_called_once_with(job_id="job-1", stage="searching",
                                   designs_completed=2, designs_total=8,
                                   new_candidate=None)
    (job_id, seconds), _ = monitor.call_args
    assert job_id == "job-1" and seconds >= 90
