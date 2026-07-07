"""Scout feasibility page — reflected job_id/epitope_id are JS-context safe.

``templates/scout/feasibility.html`` reflects the request-supplied
``job_id`` and ``epitope_id`` (from ``request.args`` in
``scout.routes.feasibility_page``) into a ``<script>`` block:

    var currentJobId = {{ job_id | tojson }} || '';
    var currentEpitopeId = {{ epitope_id | tojson }} || '';

Jinja's HTML autoescaping is the wrong tool for a JS string context: inside
a ``<script>`` raw-text block HTML entities are not decoded, so autoescaping
a quote to ``&#39;`` yields broken JS (a literal ``&#39;``) rather than a
usable value, and relying on it is context-blind. The ``tojson`` filter is
the correct, context-safe way to embed a server value in a ``<script>``
block: it serialises the value as a JSON literal (quoting + ``\\uXXXX``
escaping of ``'``, ``"`` and ``<``). This is defense-in-depth and
correctness, not a fix for a live breakout (autoescaping already neutralised
the raw quote). These tests lock the encoded form: a hostile ``job_id`` must
appear JSON-encoded, never as a raw breakout.

Route tests drive the Flask app the same way ``tests/test_404_route.py``
does — ``create_app`` with ``SESSION_SECRET_KEY`` set, login faked via
``session_transaction``, and ``load_user_context`` mocked so the render
never touches Supabase.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _login_session(client, email="leowan7@gmail.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email


def _ctx(email="leowan7@gmail.com"):
    return SimpleNamespace(
        user_id="u-feasibility-xss",
        tier="free",
        balance=0,
        email=email,
    )


def _render(app, monkeypatch, **query):
    # Mock the user-context load so the render is hermetic (no Supabase).
    monkeypatch.setattr("app.load_user_context", lambda: _ctx())
    client = app.test_client()
    _login_session(client)
    resp = client.get("/scout/feasibility", query_string=query)
    return resp, resp.get_data(as_text=True)


# The classic JS-string breakout: a single quote closes the literal, then
# ``-alert(1)-`` runs, then the trailing quote re-opens it.
HOSTILE_JOB_ID = "x'-alert(1)-'"


def test_hostile_job_id_is_not_reflected_unescaped(app, monkeypatch):
    resp, body = _render(app, monkeypatch, job_id=HOSTILE_JOB_ID)
    assert resp.status_code == 200
    # The raw breakout string must never survive verbatim into the page.
    assert HOSTILE_JOB_ID not in body
    # The unescaped breakout inside the JS literal would look like this.
    assert "currentJobId = 'x'-alert(1)-''" not in body
    # tojson emits the single quotes as ', so the payload is inert.
    assert 'var currentJobId = "x\\u0027-alert(1)-\\u0027"' in body


def test_hostile_epitope_id_script_close_is_escaped(app, monkeypatch):
    # A </script> in epitope_id must not close the inline script block.
    resp, body = _render(app, monkeypatch, epitope_id='y"</script><img src=x>')
    assert resp.status_code == 200
    assert "</script><img" not in body
    # tojson unicode-escapes the '<' so the closing tag can't form.
    assert "\\u003c/script\\u003e" in body


def test_benign_job_id_still_usable(app, monkeypatch):
    # A normal UUID-ish job_id round-trips as a quoted JS string literal.
    resp, body = _render(app, monkeypatch, job_id="abc123-def")
    assert resp.status_code == 200
    assert 'var currentJobId = "abc123-def" || \'\';' in body


def test_empty_params_render_empty_string_literals(app, monkeypatch):
    # No query params: both reflect as empty JSON strings, still valid JS.
    resp, body = _render(app, monkeypatch)
    assert resp.status_code == 200
    assert 'var currentJobId = "" || \'\';' in body
    assert 'var currentEpitopeId = "" || \'\';' in body
