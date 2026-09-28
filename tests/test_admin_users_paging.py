"""/admin/users reads every row past PostgREST's 1000-row response cap,
and shows the funnel and reminder-email blocks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

STAFF = "leo@ranomics.com"
NOW = datetime.now(timezone.utc)


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


class _Query:
    """A select that honours ``.range()`` and never returns more than 1000
    rows, like PostgREST."""

    def __init__(self, rows, calls):
        self.rows, self.calls, self.window, self.single = rows, calls, (0, 999), False

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def range(self, start, end):
        self.window = (start, end)
        return self

    def maybe_single(self):
        self.single = True
        return self

    def execute(self):
        if self.single:
            return SimpleNamespace(data=self.rows[0] if self.rows else None)
        start, end = self.window
        self.calls.append(self.window)
        return SimpleNamespace(data=self.rows[start:min(end + 1, start + 1000)])


def _client(tables, users):
    calls: dict = {}
    admin = SimpleNamespace(
        list_users=lambda page, per_page: users[(page - 1) * per_page:page * per_page],
    )
    return SimpleNamespace(
        auth=SimpleNamespace(admin=admin),
        table=lambda t: _Query(tables.get(t, []), calls.setdefault(t, [])),
        calls=calls,
    )


def _user(uid, days_ago, meta=None):
    return SimpleNamespace(id=uid, email=f"{uid}@example.com", user_metadata=meta or {},
                           created_at=NOW - timedelta(days=days_ago))


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _get(app, monkeypatch, client, path="/admin/users"):
    monkeypatch.setattr("shared.credits.get_service_client", lambda: client)
    captured = {}

    def fake_render(name, **ctx):
        captured.update(ctx, template=name)
        return "ok"

    monkeypatch.setattr("blueprints.admin.render_template", fake_render)
    tc = app.test_client()
    with tc.session_transaction() as s:
        s["user_email"] = STAFF
    assert tc.get(path).status_code == 200
    return captured


def test_counts_span_pages(app, monkeypatch):
    # 2500 events for one user, newest on the last page; 1200 runs across two.
    events = [{"id": f"e{i:05d}", "user_id": "a", "event_type": "page_view",
               "created_at": _ago(days=20, seconds=-i)} for i in range(2500)]
    events[-1]["event_type"] = "scale_up_click"
    runs = [{"id": f"j{i:05d}", "user_id": "b", "campaign_id": None,
             "created_at": _ago(days=10, seconds=-i)} for i in range(1200)]
    c = _client({"user_events": events, "tool_jobs": runs},
                [_user("a", 5), _user("b", 5)])
    ctx = _get(app, monkeypatch, c)

    by_id = {u["user_id"]: u for u in ctx["users"]}
    assert by_id["a"]["events_30d"] == 2500
    assert by_id["a"]["last_activity"] == events[-1]["created_at"][:19]
    assert by_id["b"]["runs_30d"] == 1200
    assert c.calls["user_events"] == [(0, 999), (1000, 1999), (2000, 2999)]
    f30 = ctx["funnel"][1]
    assert f30["scale_up_click"] == 1 and f30["ran_2"] == 1


def test_funnel_and_reminders(app, monkeypatch):
    users = [
        _user("new", 3, {"reengagement_email_sent_at": _ago(days=2)}),
        _user("old", 20),
        _user("ancient", 90),
    ]
    runs = [
        # A campaign's two children count as one run.
        {"id": "j1", "user_id": "new", "campaign_id": "c1", "created_at": _ago(days=1)},
        {"id": "j2", "user_id": "new", "campaign_id": "c1", "created_at": _ago(days=1)},
        {"id": "j3", "user_id": "old", "campaign_id": None, "created_at": _ago(days=15)},
        {"id": "j4", "user_id": "old", "campaign_id": None, "created_at": _ago(days=14)},
    ]
    topups = [{"id": "t1", "user_id": "old", "amount_usd": 25, "created_at": _ago(days=10)}]
    wallets = [{"user_id": "old", "balance_usd": 5,
                "signup_credit_reminder_sent_at": _ago(days=1),
                "signup_credit_expired_at": None}]
    c = _client({"tool_jobs": runs, "wallet_transactions": topups, "user_wallets": wallets},
                users)
    ctx = _get(app, monkeypatch, c)

    f7, f30 = ctx["funnel"]
    assert (f7["signups"], f7["ran_1"], f7["ran_2"], f7["topups"]) == (1, 1, 0, 0)
    assert (f30["signups"], f30["ran_1"], f30["ran_2"]) == (2, 2, 1)
    assert (f30["topups"], f30["topup_users"], f30["topup_usd"]) == (1, 1, 25.0)
    r = ctx["reminders"]
    assert r["total"] == {"reengagement": 1, "credit_reminder": 1, "credit_expired": 0}
    assert r["last"]["credit_expired"] == ""
    assert sum(d["reengagement"] + d["credit_reminder"] for _, d in r["days"]) == 2


def test_pages_render(app, monkeypatch):
    u = _user("a", 5, {"reengagement_email_sent_at": _ago(days=1)})
    c = _client({"user_wallets": [{"user_id": "a", "balance_usd": 5,
                                   "signup_credit_expires_at": _ago(days=-20)}]}, [u])
    c.auth.admin.get_user_by_id = lambda uid: SimpleNamespace(user=u)
    monkeypatch.setattr("shared.credits.get_service_client", lambda: c)
    tc = app.test_client()
    with tc.session_transaction() as s:
        s["user_email"] = STAFF
    html = tc.get("/admin/users").get_data(as_text=True)
    assert "Funnel" in html and "Reminder emails" in html and "never sent" in html
    html = tc.get("/admin/users/a").get_data(as_text=True)
    assert "credit reminder: never sent" in html and "signup credit expires" in html


def test_rejected_signups_span_pages(app, monkeypatch):
    rows = [{"id": f"r{i:05d}", "reason": "honeypot", "email": "x@y",
             "created_at": _ago(days=1)} for i in range(1300)]
    ctx = _get(app, monkeypatch, _client({"signup_rejections": rows}, []),
               "/admin/signups/rejected")
    assert ctx["total"] == 1300


def test_user_detail_shows_reminder_stamps(app, monkeypatch):
    u = _user("a", 5, {"reengagement_email_sent_at": "2026-09-01T14:00:00+00:00"})
    wallet = {"balance_usd": 5, "signup_credit_expires_at": "2026-10-28T00:00:00+00:00",
              "signup_credit_reminder_sent_at": None, "signup_credit_expired_at": None}
    c = _client({"user_wallets": [wallet]}, [u])
    c.auth.admin.get_user_by_id = lambda uid: SimpleNamespace(user=u)
    ctx = _get(app, monkeypatch, c, "/admin/users/a")
    t = ctx["target"]
    assert t["reengagement_sent_at"] == "2026-09-01T14:00:00+00:00"
    assert t["credit_reminder_sent_at"] == ""
    assert t["credit_expires_at"].startswith("2026-10-28")
