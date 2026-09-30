"""A logged-out POST to a POST-only route sends next= to a page that answers GET.

QA 2026-09-29 P1-5: a session-expired POST /tools/<slug>/submit redirected to
/login?next=/tools/<slug>/submit, and after sign-in that GET is a 405.

    pytest tests/test_login_next_post_only.py -v
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.routing import RequestRedirect

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _next(resp) -> str:
    assert resp.status_code == 302, resp.status_code
    loc = urlsplit(resp.headers["Location"])
    assert loc.path == "/login", resp.headers["Location"]
    return parse_qs(loc.query)["next"][0]


def _answers_get(app, path) -> bool:
    adapter = app.url_map.bind("localhost")
    try:
        adapter.match(path, method="GET")
    except RequestRedirect:
        return True
    except (MethodNotAllowed, NotFound):
        return False
    return True


def _gated_post_only_rules(app):
    """Every rule without GET whose view is wrapped by shared/auth.py."""
    rules = []
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods:
            continue
        fn = app.view_functions[rule.endpoint]
        while fn is not None:
            code = getattr(fn, "__code__", None)
            if code and code.co_filename.replace("\\", "/").endswith("shared/auth.py"):
                rules.append(rule)
                break
            fn = getattr(fn, "__wrapped__", None)
    return rules


def test_tool_submit_returns_to_the_tool_form(app):
    resp = app.test_client().post("/tools/rfdiffusion/submit", data={})
    assert _next(resp) == "/tools/rfdiffusion"


def test_get_route_keeps_its_own_path(app):
    assert _next(app.test_client().get("/jobs")) == "/jobs"


def test_every_gated_post_only_route_gets_a_get_target(app):
    rules = _gated_post_only_rules(app)
    assert len(rules) >= 20, [r.rule for r in rules]
    client = app.test_client()
    for rule in rules:
        path = re.sub(r"<(?:[^:>]+:)?([^>]+)>", "x1", rule.rule)
        method = sorted(rule.methods - {"HEAD", "OPTIONS"})[0]
        nxt = _next(client.open(path, method=method))
        assert nxt == "/" or _answers_get(app, nxt), (rule.rule, nxt)
        assert path.startswith(nxt.rstrip("/")), (rule.rule, nxt)
