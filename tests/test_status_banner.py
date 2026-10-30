"""The disruption banner follows the cached UptimeRobot flag, not a live read."""

from __future__ import annotations

import pytest

from shared import status_banner

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.delenv("STATUS_BANNER", raising=False)
    # Empty, not deleted: load_dotenv does not override a set variable.
    monkeypatch.setenv("UPTIMEROBOT_READONLY_KEY", "")
    monkeypatch.setattr(status_banner, "_down", False)
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def test_down_monitor_shows_banner_and_up_monitor_clears_it(client, monkeypatch):
    assert b"data-status-banner" not in client.get("/").data

    monkeypatch.setattr(status_banner, "_uptimerobot_down", lambda: True)
    status_banner.refresh()
    page = client.get("/").data.decode()
    assert 'role="status" data-status-banner' in page
    assert (  # Leo's wording, verbatim
        "Service disruption: all functionalities are not currently available. "
        "We are working on it"
    ) in page

    monkeypatch.setattr(status_banner, "_uptimerobot_down", lambda: False)
    status_banner.refresh()
    assert b"data-status-banner" not in client.get("/").data


def test_unreachable_uptimerobot_fails_closed_to_no_banner(monkeypatch):
    def boom():
        raise TimeoutError("read timed out")

    monkeypatch.setattr(status_banner, "_down", True)
    monkeypatch.setattr(status_banner, "_uptimerobot_down", boom)
    status_banner.refresh()
    assert status_banner.banner_message() is None


def test_only_down_statuses_count(monkeypatch):
    class Resp:
        def __init__(self, statuses):
            self.statuses = statuses

        def raise_for_status(self):
            pass

        def json(self):
            return {"stat": "ok",
                    "monitors": [{"status": s} for s in self.statuses]}

    import httpx

    for statuses, expected in [([2, 0, 1], False), ([2, 8], True), ([9], True)]:
        monkeypatch.setattr(httpx, "post", lambda *a, _s=statuses, **k: Resp(_s))
        assert status_banner._uptimerobot_down() is expected


def test_env_override_forces_banner(client, monkeypatch):
    monkeypatch.setenv("STATUS_BANNER", "Planned maintenance at 18:00 UTC.")
    assert b"Planned maintenance at 18:00 UTC." in client.get("/login").data


def test_a_hanging_check_does_not_block_the_page(client, monkeypatch):
    import threading
    import time

    release = threading.Event()
    started = threading.Event()

    def hang():
        started.set()
        release.wait(10)
        return False  # the thread may finish after teardown; leave the flag clean

    monkeypatch.setenv("UPTIMEROBOT_READONLY_KEY", "ur-test")
    monkeypatch.setattr(status_banner, "_uptimerobot_down", hang)
    monkeypatch.setattr(status_banner, "_last_started", float("-inf"))
    monkeypatch.setattr(status_banner, "_in_flight", False)
    try:
        t0 = time.monotonic()
        assert client.get("/").status_code == 200
        assert time.monotonic() - t0 < 5  # the check hangs for 10 s
        assert started.wait(5)
    finally:
        release.set()
