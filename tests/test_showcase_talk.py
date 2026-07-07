"""Showcase content renders, and the retired /talk redirector is gone.

/showcase must render the real anonymized benchmark entries with no placeholder
banner and no crash from a bad tool slug in the per-entry JSON-LD (a typo'd slug
would raise inside ``url_for`` and 500 the page). The /talk/<campaign>
conference redirector was removed, so it must now 404.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


_SHOWCASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "content", "showcase",
)


def _showcase_files():
    return sorted(f for f in os.listdir(_SHOWCASE_DIR) if f.endswith(".md"))


def _frontmatter_tool(path):
    """Pull the ``tool:`` value out of a showcase markdown frontmatter block."""
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert lines and lines[0].strip() == "---"
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("tool:"):
            return line.partition(":")[2].strip()
    return None


def test_showcase_renders_without_placeholder_banner(client):
    resp = client.get("/showcase")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The pre-launch placeholder banner must be gone.
    assert "Replace with real customer anonymized data" not in body
    # Real entries render; the per-entry anchor id is the file slug.
    assert "01-boltzgen-vhh-immune-coreceptor" in body
    assert "03-rfdiffusion-platform-pilot" in body


def test_showcase_entries_use_registered_tool_slugs():
    """Every entry's tool must resolve, or the JSON-LD url_for 500s the page."""
    import app as _app  # noqa: F401 — importing app registers the tool adapters
    from tools import base as tool_base

    files = _showcase_files()
    assert files, "expected real showcase entries under content/showcase/"
    for name in files:
        slug = _frontmatter_tool(os.path.join(_SHOWCASE_DIR, name))
        assert slug, f"{name} has no tool slug"
        if slug == "scout":
            continue
        assert tool_base.get(slug) is not None, (
            f"{name} references unregistered tool slug {slug!r}"
        )


def test_talk_route_removed(client):
    resp = client.get("/talk/pegs-2026")
    assert resp.status_code == 404
