"""Guessed tool URLs redirect instead of 404ing.

The catalog slug is ``mpnn``, so /tools/mpnn is the real page, while the
tool is called ProteinMPNN everywhere a reader meets it. /tools/proteinmpnn
404ed; curl against production on 2026-09-15 returned 404 there and 200 on
/tools/mpnn.

Per the marketing repo's docs/seo/STRATEGY-2026-09-11.md (section E item 5,
sourced from Search Console), the query "proteinmpnn online" already earns
the site impressions at an 8.3% click-through rate, so the guessed URL is
one those clicks can land on.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_proteinmpnn_redirects_permanently(client):
    resp = client.get("/tools/proteinmpnn")
    assert resp.status_code == 301, (
        f"/tools/proteinmpnn returned {resp.status_code}; a 404 spends the "
        "inbound search click"
    )
    assert resp.headers["Location"].endswith("/tools/mpnn")


def test_query_string_survives(client):
    """tool_form pre-fills from query params, so dropping them loses state.

    ``clone_from`` is one tool_form really reads from ``request.args``.
    """
    resp = client.get("/tools/proteinmpnn?clone_from=job-123")
    assert resp.headers["Location"].endswith("/tools/mpnn?clone_from=job-123")


def test_malformed_query_string_still_redirects(client):
    """A non-UTF-8 byte in the query string must not 500.

    WSGI hands QUERY_STRING over as a latin-1 str, so a raw 0xFF byte in
    the request line reaches the redirect undecoded.
    """
    resp = client.get(
        "/tools/proteinmpnn",
        environ_overrides={"QUERY_STRING": "clone_from=\xff"},
    )

    assert resp.status_code == 301
    assert resp.headers["Location"].startswith("/tools/mpnn?clone_from=")
