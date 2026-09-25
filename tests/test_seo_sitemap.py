"""Sitemap entries resolve, and the free tool pages carry WebApplication JSON-LD."""
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urlparse

import pytest

from shared.feature_flags import flag_name
from tools import base as tool_base

pytestmark = pytest.mark.usefixtures("isolate_supabase")

FREE_PAGES = ("/prep", "/scout/", "/developability", "/library-planner")


@pytest.fixture
def client(monkeypatch):
    import app as _app  # noqa: F401  (populates tools.base._REGISTRY)
    for adapter in tool_base.all_adapters():
        monkeypatch.setenv(flag_name(adapter.slug), "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _locs(client):
    body = client.get("/sitemap.xml").get_data(as_text=True)
    return [urlparse(u).path for u in re.findall(r"<loc>([^<]*)</loc>", body)]


def test_free_pages_are_listed(client):
    assert set(FREE_PAGES) <= set(_locs(client))


def test_every_loc_is_a_200(client):
    paths = _locs(client)
    assert any(p.startswith("/tools/") for p in paths)
    bad = {p: client.get(p).status_code for p in paths}
    assert {p: s for p, s in bad.items() if s != 200} == {}


@pytest.mark.parametrize("path", FREE_PAGES)
def test_free_page_has_webapplication_ld(client, path):
    html = client.get(path).get_data(as_text=True)
    blocks = [json.loads(b) for b in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]
    app_ld = [b for b in blocks if b.get("@type") == "WebApplication"]
    assert len(app_ld) == 1
    desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
    assert app_ld[0]["description"] == unescape(desc)
    assert app_ld[0]["url"].endswith(path)
