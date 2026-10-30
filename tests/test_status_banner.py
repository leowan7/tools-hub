"""The disruption banner follows the cached health flag, not a live read."""

from __future__ import annotations

import pytest

from shared import status_banner

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.delenv("STATUS_BANNER", raising=False)
    monkeypatch.setattr(status_banner, "_db_down", False)
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def test_failed_check_shows_banner_and_good_check_clears_it(client, monkeypatch):
    assert b"data-status-banner" not in client.get("/").data

    def boom():
        raise TimeoutError("read timed out")

    monkeypatch.setattr(status_banner, "_db_ok", boom)
    status_banner.refresh()
    page = client.get("/").data.decode()
    assert 'role="status" data-status-banner' in page
    assert status_banner.DISRUPTION_MESSAGE in page

    monkeypatch.setattr(status_banner, "_db_ok", lambda: True)
    status_banner.refresh()
    assert b"data-status-banner" not in client.get("/").data


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
        return True  # the thread may finish after teardown; leave the flag clean

    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setattr(status_banner, "_db_ok", hang)
    monkeypatch.setattr(status_banner, "_last_started", float("-inf"))
    monkeypatch.setattr(status_banner, "_in_flight", False)
    try:
        t0 = time.monotonic()
        assert client.get("/").status_code == 200
        assert time.monotonic() - t0 < 5  # the check hangs for 10 s
        assert started.wait(5)
    finally:
        release.set()
