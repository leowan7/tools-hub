"""Launch-cutover URL redirects (compute -> /campaigns, wet-lab -> /lab-projects).

Full protection: the vacated GET paths (/runs/*, /admin/campaigns/*) 301 to
their new homes so already-sent email links and bookmarks keep working, and the
old wet-lab /campaigns/<id> link (now the compute detail route) forwards to
/lab-projects/<id> when the id is a wet-lab campaign.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

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


def _ctx(user_id="u-1"):
    return SimpleNamespace(user_id=user_id, tier="free", balance=100, email="u@example.com")


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"


# -- Vacated compute paths 301 to /campaigns/* ------------------------------

def test_legacy_runs_list_redirects(client):
    resp = client.get("/runs")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/campaigns")


def test_legacy_runs_new_redirects(client):
    resp = client.get("/runs/new")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/campaigns/new")


def test_legacy_runs_detail_redirects(client):
    resp = client.get("/runs/camp-xyz")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/campaigns/camp-xyz")


def test_legacy_estimate_redirects_and_preserves_query(client):
    resp = client.get("/api/runs/estimate?tool=rfdiffusion&requested_designs=24")
    assert resp.status_code == 301
    loc = resp.headers["Location"]
    assert "/api/campaigns/estimate" in loc
    assert "tool=rfdiffusion" in loc
    assert "requested_designs=24" in loc


# -- Vacated admin paths 301 to /admin/lab-projects/* -----------------------

def test_legacy_admin_list_redirects_and_preserves_query(client):
    resp = client.get("/admin/campaigns?status=quoted")
    assert resp.status_code == 301
    loc = resp.headers["Location"]
    assert "/admin/lab-projects" in loc
    assert "status=quoted" in loc


def test_legacy_admin_detail_redirects(client):
    resp = client.get("/admin/campaigns/exp-1")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/admin/lab-projects/exp-1")


# -- Wet-lab email-link collision on /campaigns/<id> ------------------------

def test_wetlab_email_link_forwards_to_lab_projects(client):
    """/campaigns/<id> is now the compute detail route. A compute miss that IS
    one of the user's wet-lab campaigns must 301 to /lab-projects/<id> so old
    wet-lab email links still land right.

    THE COMPUTE READ HERE IS THE TWO-OUTCOME ``get_campaign`` AND STAYS THAT
    WAY (register items A90 and A94): this fallback is what an unreadable run
    also gets, so an id that is neither compute nor wet-lab ends on the runs
    list with a 200 whether the row is absent or the database is down. The
    target arm resolves its own parent through the three-outcome ``read_target``
    because ITS absent answer is a 404; see tests/test_target_routes.py.
    """
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
         patch("shared.compute_campaigns.get_campaign", return_value=None), \
         patch("shared.campaigns.get_campaign", return_value=object()):
        resp = client.get("/campaigns/wetlab-1")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/lab-projects/wetlab-1")


def test_compute_miss_that_is_not_wetlab_falls_back_to_list(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
         patch("shared.compute_campaigns.get_campaign", return_value=None), \
         patch("shared.campaigns.get_campaign", return_value=None):
        resp = client.get("/campaigns/missing")
    assert resp.status_code in (301, 302)
    assert resp.headers["Location"].endswith("/campaigns")
