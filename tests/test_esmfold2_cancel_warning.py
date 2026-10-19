"""The in-page cancel warning on a running esmfold2-design job (PR #440, item a).

Rendered through the real /jobs/<id> route. The warning is for esmfold2-design
only; every other tool keeps the old cancel button unchanged.
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_JID = str(uuid.uuid4())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app.test_client()


def _page(client, tool, inputs, status="running"):
    job = SimpleNamespace(
        id=_JID, tool=tool, preset="pilot", status=status,
        created_at="2026-01-01T00:00:00+00:00", inputs=inputs,
        result=None, error=None, gpu_seconds_used=None,
    )
    ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100, email="u@example.com")
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.jobs.load_user_context", return_value=ctx), \
            patch("blueprints.jobs.get_job", return_value=job):
        resp = client.get(f"/jobs/{_JID}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _delivered(html):
    m = re.search(r'id="cancel-confirm"[^>]*data-delivered="([^"]*)"', html)
    return m and m.group(1)


@pytest.mark.parametrize("inputs, want", [
    ({}, "0"),
    ({"_partial_candidates": "junk"}, "0"),
    ({"_partial_candidates": {"a": 1}}, "0"),
    ({"_partial_candidates": [{"rank": 1}, {"rank": 2}]}, "2"),
])
def test_esmfold2_running_page_carries_the_warning_and_its_count(client, inputs, want):
    html = _page(client, "esmfold2-design", inputs)
    assert _delivered(html) == want
    assert "keeps no designs, because designs arrive only when a seed's batch finishes" in html


@pytest.mark.parametrize("tool", ["esmfold", "bindcraft", "rfdiffusion"])
def test_other_tools_get_no_warning(client, tool):
    html = _page(client, tool, {})
    assert 'id="cancel-confirm"' not in html
    assert 'id="cancel-job-btn"' in html


def test_finished_esmfold2_page_has_no_warning(client):
    assert 'id="cancel-confirm"' not in _page(client, "esmfold2-design", {}, status="failed")


def test_the_warning_path_never_calls_window_confirm(client):
    """At 0 designs the click opens the in-page box and returns before
    window.confirm; "Cancel anyway" posts straight through doCancel."""
    html = _page(client, "esmfold2-design", {})
    click = html[html.index("btn.addEventListener('click'"):]
    assert click.index("confirmBox.hidden = false") < click.index("return;") < click.index("window.confirm")
    yes = html[html.index("getElementById('cancel-confirm-yes')"):]
    assert yes.index("doCancel()") < yes.index("});")
    do_cancel = html[html.index("function doCancel()"):]
    assert "window.confirm" not in do_cancel[:do_cancel.index("})();")]
    assert "'/cancel'" in do_cancel
