"""esmfold2_design's keepalive: a heartbeat at the start and every interval."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

from tools.esmfold2_design import run_pipeline as rp
from webhooks import modal as modal_webhook

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def test_keepalive_beats_on_entry_then_periodically_and_stops_on_exit(monkeypatch):
    beats = []
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: beats.append(a))
    with rp.keepalive("https://hub/webhooks/modal/j/t", "job-1", interval=0.01):
        time.sleep(0.2)
    after_exit = len(beats)
    time.sleep(0.1)
    assert after_exit >= 3
    assert len(beats) == after_exit
    assert beats[0] == ("https://hub/webhooks/modal/j/t", "job-1", "designing")


def test_first_beat_does_not_wait_an_interval(monkeypatch):
    beats = []
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: beats.append(a))
    with rp.keepalive("u", "job-1", interval=3600):
        deadline = time.time() + 5
        while not beats and time.time() < deadline:
            time.sleep(0.01)
    assert len(beats) == 1


def test_keepalive_stops_when_the_run_raises(monkeypatch):
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: None)
    ka = rp.keepalive("u", "job-1", interval=0.01)
    with pytest.raises(RuntimeError):
        with ka:
            raise RuntimeError
    assert not ka._thread.is_alive()


def test_main_runs_the_pipeline_inside_a_keepalive(monkeypatch):
    seen = []

    class FakeKeepalive:
        def __init__(self, *args, seed=""):
            seen.append(("open", args, seed))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            seen.append("close")

    monkeypatch.setenv("WEBHOOK_URL", "https://hub/webhooks/modal/job-9/tok")
    monkeypatch.setenv("JOB_ID", "job-9")
    monkeypatch.setenv("SEED_NAME", "seed2")
    monkeypatch.setattr(rp, "keepalive", FakeKeepalive)
    monkeypatch.setattr(rp, "_run", lambda: seen.append("run") or 0)
    monkeypatch.setattr(rp, "_archive_raw", lambda *a, **k: seen.append("archive"))
    assert rp.main() == 0
    assert seen == [
        ("open", ("https://hub/webhooks/modal/job-9/tok", "job-9"), "seed2"),
        "run", "close", "archive",
    ]


def _post_beat(job):
    posted = {}

    def fake_post(url, json, timeout):
        posted.update(url=url, body=json)
        return SimpleNamespace(status_code=200)

    with patch.object(rp.requests, "post", fake_post):
        rp.send_heartbeat("https://hub/webhooks/modal/job-1/tok", "job-1", "designing")
    assert posted["url"] == "https://hub/webhooks/heartbeat"

    app = Flask(__name__)
    modal_webhook.register_modal_webhooks(app)
    with patch.object(modal_webhook, "get_job", return_value=job), \
         patch.object(modal_webhook, "mark_running") as mark, \
         patch.object(modal_webhook, "_append_heartbeat_state") as append, \
         patch.object(modal_webhook, "_run_mid_run_monitor") as monitor:
        resp = app.test_client().post("/webhooks/heartbeat", json=posted["body"])
    assert resp.status_code == 200
    append.assert_called_once_with(job_id="job-1", stage="designing",
                                   designs_completed=0, designs_total=0,
                                   new_candidate=None, seed=None)
    return mark, monitor


def test_the_first_beat_stamps_started_at():
    job = SimpleNamespace(id="job-1", status="running", started_at=None,
                          inputs={}, job_token="tok")
    mark, _ = _post_beat(job)
    mark.assert_called_once_with("job-1", allowed_current=("pending", "running"))


def test_a_later_beat_meters_elapsed_gpu_seconds():
    started = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    job = SimpleNamespace(id="job-1", status="running", started_at=started,
                          inputs={"_progress": {}}, job_token="tok")
    mark, monitor = _post_beat(job)
    mark.assert_not_called()
    (job_id, seconds), _ = monitor.call_args
    assert job_id == "job-1" and seconds >= 90


def test_keepalive_names_its_seed_on_every_beat(monkeypatch):
    seeds = []
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: seeds.append(k.get("seed")))
    with rp.keepalive("u", "job-1", interval=0.01, seed="seed3"):
        time.sleep(0.1)
    assert len(seeds) >= 2 and set(seeds) == {"seed3"}


def test_each_child_env_names_its_seed():
    from tools.esmfold2_design import modal_app

    env = modal_app._build_run_env({"job_spec": {"pdb_prefix": "seed3_"}})
    assert env["SEED_NAME"] == "seed3"
    assert modal_app._build_run_env({"job_spec": {}})["SEED_NAME"] == ""


def _post_seed_beat(job_token, seed="seed3"):
    posted = {}

    def fake_post(url, json, timeout):
        posted.update(body=json)
        return SimpleNamespace(status_code=200)

    with patch.dict("os.environ", {"JOB_TOKEN": job_token}),          patch.object(rp.requests, "post", fake_post):
        rp.send_heartbeat("https://hub/webhooks/modal/job-1/tok", "job-1", "designing",
                          seed=seed)
    job = SimpleNamespace(id="job-1", status="running", started_at=None,
                          inputs={"_progress": {}}, job_token="tok")
    app = Flask(__name__)
    modal_webhook.register_modal_webhooks(app)
    with patch.object(modal_webhook, "get_job", return_value=job),          patch.object(modal_webhook, "_append_heartbeat_state") as append,          patch.object(modal_webhook, "_run_mid_run_monitor"):
        app.test_client().post("/webhooks/heartbeat", json=posted["body"])
    return append.call_args.kwargs["seed"]


def test_a_seed_beat_with_the_job_token_reaches_the_merge():
    assert _post_seed_beat("tok") == "seed3"


@pytest.mark.parametrize("token, seed", [("forged", "seed3"), ("tok", "seed3_x")])
def test_a_forged_or_malformed_seed_beat_is_dropped(token, seed):
    assert _post_seed_beat(token, seed) is None
