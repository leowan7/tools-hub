"""Contract for the left rail's data source (``shared/sidebar_nav.py``).

Two things here are logic rather than markup, so two things are tested:
the longest-match rule that decides which single row is marked current,
and the promise that the tool headings ARE the catalog's own intent bands
rather than a second taxonomy that can drift from it.
"""

from __future__ import annotations

import pytest

from shared.sidebar_nav import sidebar_active_href, sidebar_groups

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


_GROUPS = [
    {"title": "Overview", "items": [{"label": "Home", "href": "/"}]},
    {
        "title": "Manage",
        "items": [
            {"label": "My jobs", "href": "/jobs"},
            {"label": "Account", "href": "/account"},
            {"label": "API keys", "href": "/account/api-keys"},
        ],
    },
]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "/"),
        # Home is "/" and every path starts with it; a plain prefix test
        # would light Home up on every page in the app.
        ("/jobs", "/jobs"),
        ("/jobs/abc123", "/jobs"),
        # The rail's hrefs nest. /account/api-keys is covered by BOTH
        # /account and itself, and only the longer one may win.
        ("/account", "/account"),
        ("/account/api-keys", "/account/api-keys"),
        ("/account/api-keys/", "/account/api-keys"),
        # A page no rail row covers marks nothing, rather than falling
        # back to the shortest match.
        ("/pricing", ""),
        ("", ""),
    ],
)
def test_active_href_is_the_longest_row_covering_the_path(path, expected):
    assert sidebar_active_href(_GROUPS, path) == expected


def test_jobs_does_not_match_a_sibling_with_the_same_prefix():
    assert sidebar_active_href(_GROUPS, "/jobs-archive") == ""


def test_tool_headings_are_the_catalog_bands(app):
    from shared.tools_catalog import CATEGORY_ORDER

    with app.test_request_context("/"):
        titles = [g["title"] for g in sidebar_groups()]

    assert titles[0] == "Overview"
    assert titles[-1] == "Manage"
    # Every heading between them comes from the catalog, in catalog order:
    # renaming a band in shared/tools_catalog.py moves the rail heading
    # with it instead of leaving the rail asserting a stale taxonomy.
    tool_titles = titles[1:-1]
    assert tool_titles, "no tool bands rendered"
    assert set(tool_titles) <= set(CATEGORY_ORDER)
    assert tool_titles == sorted(tool_titles, key=CATEGORY_ORDER.index)


def test_every_row_has_a_usable_href(app):
    with app.test_request_context("/"):
        groups = sidebar_groups()

    for group in groups:
        assert group["items"], f"{group['title']} rendered with no rows"
        for item in group["items"]:
            assert item["label"]
            assert item["href"].startswith("/")
