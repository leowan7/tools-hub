"""Marketing-email unsubscribe: signed link, List-Unsubscribe header, the
opt-out route, the crons honouring it, and paged auth user listing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from shared import email

pytestmark = pytest.mark.usefixtures("isolate_supabase")

UID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"id": "resend-1"}


@pytest.fixture
def posted(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return _Resp()

    monkeypatch.setattr(email.requests, "post", fake_post)
    return calls


class _Admin:
    def __init__(self, users):
        self.users = {u.id: u for u in users}
        self.updates = []

    def list_users(self, page=None, per_page=None):
        rows = list(self.users.values())
        return rows[(page - 1) * per_page:page * per_page]

    def get_user_by_id(self, uid):
        return SimpleNamespace(user=self.users[uid])

    def update_user_by_id(self, uid, attrs):
        self.updates.append((uid, attrs))
        self.users[uid].user_metadata = attrs["user_metadata"]


def _client_with(users):
    return SimpleNamespace(auth=SimpleNamespace(admin=_Admin(users)))


def _user(uid, meta=None):
    return SimpleNamespace(id=uid, email=f"{uid}@example.com",
                           user_metadata=meta or {}, created_at="2026-01-01T00:00:00+00:00")


# --- token -----------------------------------------------------------------


def test_token_round_trips_and_rejects_tampering():
    url = email.unsubscribe_url(UID)
    token = url.rsplit("/", 1)[1]
    assert email.read_unsubscribe_token(token) == UID
    assert email.read_unsubscribe_token(token[:-2] + "xx") is None
    assert email.read_unsubscribe_token("garbage") is None


def test_no_secret_means_no_link_and_no_marketing_send(monkeypatch, posted):
    monkeypatch.delenv("SESSION_SECRET_KEY")
    assert email.unsubscribe_url(UID) is None
    monkeypatch.setattr(email, "_resolve_user_email", lambda _uid: "a@example.com")
    assert not email.send_signup_credit_expiring_email(
        user_id=UID, remaining_usd=5, expires_at=datetime(2026, 10, 28))
    assert posted == []


# --- transport + footer -----------------------------------------------------


def test_marketing_email_carries_header_footer_and_text_link(monkeypatch, posted):
    monkeypatch.setattr(email, "_resolve_user_email", lambda _uid: "a@example.com")
    assert email.send_signup_credit_expiring_email(
        user_id=UID, remaining_usd=5, expires_at=datetime(2026, 10, 28))
    (payload,) = posted
    link = email.unsubscribe_url(UID)
    assert payload["headers"] == {
        "List-Unsubscribe": f"<{link}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    assert f'href="{link}"' in payload["html"]
    assert payload["text"].endswith(f"Unsubscribe: {link}")


def test_reengagement_email_carries_header_and_footer(posted):
    cand = SimpleNamespace(user_id=UID, balance_usd=5, last_job_at="",
                           suggestions=[{"slug": "mpnn", "label": "MPNN",
                                         "blurb": "b", "url": "https://x/tools/mpnn"}])
    assert email.send_reengagement_email(user_email="a@example.com", candidate=cand,
                                         base_url="https://x")
    (payload,) = posted
    link = email.unsubscribe_url(UID)
    assert payload["headers"]["List-Unsubscribe"] == f"<{link}>"
    assert f'href="{link}"' in payload["html"]
    assert f"Unsubscribe: {link}" in payload["text"]


def test_transactional_email_has_no_unsubscribe(monkeypatch, posted):
    monkeypatch.setattr(email, "_resolve_user_email", lambda _uid: "a@example.com")
    assert email.send_topup_confirmation_email(user_id=UID, amount_usd=10)
    (payload,) = posted
    assert "headers" not in payload
    assert "nsubscribe" not in payload["html"] + payload["text"]


# --- route ------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("CSRF_PROTECT", "1")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def test_route_get_confirms_post_records(app, monkeypatch):
    c = _client_with([_user(UID, {"email_preferences": {"job_complete_email": False},
                                  "other": 1})])
    monkeypatch.setattr("shared.credits.get_service_client", lambda: c)
    path = "/email/unsubscribe/" + email.unsubscribe_url(UID).rsplit("/", 1)[1]
    client = app.test_client()

    resp = client.get(path)
    assert resp.status_code == 200 and b"<form" in resp.data
    assert c.auth.admin.updates == []

    # RFC 8058 one-click: a mail client POSTs with no session and no CSRF token.
    resp = client.post(path, data={"List-Unsubscribe": "One-Click"})
    assert resp.status_code == 200
    assert c.auth.admin.updates == [(UID, {"user_metadata": {
        "email_preferences": {"job_complete_email": False, "marketing_email": False},
        "other": 1,
    }})]


def test_route_rejects_bad_token(app, monkeypatch):
    c = _client_with([_user(UID)])
    monkeypatch.setattr("shared.credits.get_service_client", lambda: c)
    resp = app.test_client().post("/email/unsubscribe/not-a-token")
    assert resp.status_code == 400
    assert c.auth.admin.updates == []


# --- crons skip opted-out users --------------------------------------------

_OUT = {"email_preferences": {"marketing_email": False}}


def test_reengagement_skips_opted_out(monkeypatch):
    from cron import reengagement

    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    users = [_user("in"), _user("out", _OUT)]
    rows = {"user_wallets": [{"user_id": u.id, "balance_usd": 5} for u in users],
            "tool_jobs": []}

    class _Q:
        def __init__(self, t):
            self.t = t

        def __getattr__(self, _name):
            return lambda *a, **k: self

        def execute(self):
            return SimpleNamespace(data=rows[self.t])

    c = _client_with(users)
    c.table = _Q
    monkeypatch.setattr("shared.credits.get_service_client", lambda: c)
    got = {x.user_id for x in reengagement.find_candidates(now=now)}
    assert got == {"in"}


def test_signup_credit_reminder_skips_opted_out(monkeypatch):
    from cron import signup_credit
    from shared import wallet

    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    w = {"user_id": UID, "signup_credit_expires_at": (now + timedelta(days=3)).isoformat(),
         "signup_credit_expired_at": None, "signup_credit_reminder_sent_at": None}
    c = _client_with([_user(UID, _OUT)])
    monkeypatch.setattr("shared.credits.get_service_client", lambda: c)
    monkeypatch.setattr("shared.jobs.get_service_client", lambda: c)
    monkeypatch.setattr(signup_credit, "_due_wallets", lambda *_: [w])
    monkeypatch.setattr(wallet, "signup_credit_status",
                        lambda *_a, **_k: {"remaining_usd": 5, "expires_at": now})
    claims, sends = [], []
    monkeypatch.setattr(signup_credit, "_claim_reminder", lambda *a: claims.append(a) or True)
    monkeypatch.setattr("shared.email.send_signup_credit_expiring_email",
                        lambda **kw: sends.append(kw) or True)
    summary = signup_credit.run(now=now)
    assert summary["skipped"] == 1 and summary["reminded"] == 0
    assert claims == [] and sends == []


# --- paging -----------------------------------------------------------------


def test_list_all_auth_users_reads_every_page(monkeypatch):
    from shared import credits

    monkeypatch.setattr(credits, "_AUTH_USERS_PAGE", 2)
    users = [_user(f"u{i}") for i in range(5)]
    c = _client_with(users)
    assert [u.id for u in credits.list_all_auth_users(c)] == [u.id for u in users]
    monkeypatch.setattr(credits, "get_service_client", lambda: c)
    assert credits._resolve_user_id("u4@example.com") == "u4"


def test_list_all_auth_users_stops_on_empty_response_object():
    """A response object whose ``.users`` is empty ends the walk (a truthy
    object with an empty list must not be iterated as the batch)."""
    from shared import credits

    class _Page(list):
        def __init__(self, users):
            super().__init__(["not-a-user"])
            self.users = users

    pages = {1: _Page([_user("a")]), 2: _Page([])}
    admin = SimpleNamespace(list_users=lambda page, per_page: pages[page])
    got = credits.list_all_auth_users(SimpleNamespace(auth=SimpleNamespace(admin=admin)))
    assert [u.id for u in got] == ["a"]
