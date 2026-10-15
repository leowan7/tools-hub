"""cron/daily_digest.py: internal accounts, money, pricing viewers, commercial flag."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

import shared.credits
from cron import daily_digest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

NOW = datetime.now(timezone.utc)
RECENT = (NOW - timedelta(hours=1)).isoformat()
OLD = (NOW - timedelta(days=60)).isoformat()
LAPSED = (NOW - timedelta(days=10)).isoformat()


class _Query:
    def __init__(self, rows, broken):
        self._rows = [dict(r) for r in rows]
        self._broken = broken

    def select(self, *_a, **_k):
        return self

    def gte(self, col, value):
        self._rows = [r for r in self._rows if (r.get(col) or "") >= value]
        return self

    def lt(self, col, value):
        self._rows = [r for r in self._rows if (r.get(col) or "") < value]
        return self

    def eq(self, col, value):
        self._rows = [r for r in self._rows if r.get(col) == value]
        return self

    def in_(self, col, values):
        self._rows = [r for r in self._rows if r.get(col) in values]
        return self

    def order(self, col, desc=False):
        self._rows.sort(key=lambda r: r.get(col), reverse=desc)
        return self

    def range(self, start, end):
        self._rows = self._rows[start:end + 1]
        return self

    def execute(self):
        if self._broken:
            raise RuntimeError("table unreadable")
        return SimpleNamespace(data=self._rows)


class _Client:
    def __init__(self, tables, broken=()):
        self.tables = tables
        self.broken = set(broken)

    def table(self, name):
        return _Query(self.tables.get(name, []), name in self.broken)


AUTH_USERS = [
    {"id": "u-int", "email": "LeoWan7+test@gmail.com", "created_at": RECENT},
    {"id": "u-staff", "email": "leo@ranomics.com", "created_at": RECENT},
    {"id": "u-a", "email": "jane@acmebio.com", "created_at": RECENT},
    {"id": "u-b", "email": "bob@gmail.com", "created_at": OLD},
    {"id": "u-c", "email": "carol@gmail.com", "created_at": OLD},
]


def _ledger(i, uid, kind, amount):
    return {"id": i, "user_id": uid, "kind": kind, "amount_usd": amount, "created_at": RECENT}


def _tables():
    return {
        "user_profiles": [
            {"user_id": "u-int", "created_at": RECENT, "signup_quality": "business",
             "purpose": "drug target test"},
            {"user_id": "u-a", "created_at": RECENT, "signup_quality": "business",
             "domain_class": "business",
             "purpose": "Binders against a therapeutic target for our company"},
        ],
        "signup_rejections": [
            {"email": "qa@ranomics.com", "reason": "disposable", "created_at": RECENT},
            {"email": "x@mailinator.com", "reason": "disposable", "created_at": RECENT},
        ],
        "tool_jobs": [
            *({"user_id": "u-a", "tool": "boltz2", "status": "succeeded",
               "created_at": RECENT} for _ in range(3)),
            *({"user_id": "u-int", "tool": "boltz2", "status": "failed",
               "created_at": RECENT} for _ in range(5)),
            {"user_id": "u-int", "created_at": LAPSED},
        ],
        "user_events": [
            # Signed in: two views by one person count once.
            {"user_id": "u-a", "session_id": "s-a", "event_type": "pricing_view",
             "path": "/pricing", "created_at": RECENT},
            {"user_id": "u-a", "session_id": "s-a", "event_type": "pricing_view",
             "path": "/pricing", "created_at": RECENT},
            {"user_id": "u-b", "session_id": "s-b", "event_type": "pricing_view",
             "path": "/pricing", "created_at": RECENT},
            # u-b before signing in on the same browser: still u-b.
            {"user_id": None, "session_id": "s-b", "event_type": "pricing_view",
             "path": "/pricing", "created_at": RECENT},
            # Anonymous: counted by session, once.
            {"user_id": None, "session_id": "s-anon", "event_type": "pricing_view",
             "path": "/pricing", "created_at": RECENT},
            {"user_id": None, "session_id": "s-anon", "event_type": "pricing_view",
             "path": "/pricing", "created_at": RECENT},
            # Internal, signed in and then logged out on the same browser.
            {"user_id": "u-int", "session_id": "s-int", "event_type": "pricing_view",
             "path": "/pricing", "created_at": RECENT},
            {"user_id": None, "session_id": "s-int", "event_type": "pricing_view",
             "path": "/pricing", "created_at": RECENT},
            {"user_id": "u-staff", "session_id": "s-staff", "event_type": "credit_exhausted",
             "path": "/tools/boltz2", "created_at": RECENT},
            {"event_type": "signup_failed", "created_at": RECENT,
             "props": {"reason": "existing_account", "email": "leowan7@gmail.com"}},
            {"event_type": "signup_failed", "created_at": RECENT,
             "props": {"reason": "existing_account", "email": "bob@gmail.com"}},
            {"user_id": "u-c", "session_id": "s-c", "event_type": "page_view",
             "created_at": LAPSED},
            {"user_id": "u-staff", "session_id": "s-staff", "event_type": "page_view",
             "created_at": LAPSED},
        ],
        "wallet_transactions": [
            _ledger(1, "u-a", "topup", "50.00"),
            _ledger(2, "u-a", "auto_reload", "20.00"),
            _ledger(3, "u-a", "hold", "-10.00"),
            _ledger(4, "u-a", "hold_release", "4.00"),
            _ledger(5, "u-a", "charge", "-2.00"),
            _ledger(6, "u-b", "run_debit", "-3.50"),
            _ledger(7, "u-b", "signup_credit", "5.00"),
            _ledger(8, "u-int", "topup", "500.00"),
            _ledger(9, "u-int", "run_debit", "-40.00"),
            _ledger(10, "u-staff", "auto_reload", "25.00"),
            {"id": 11, "user_id": "u-b", "kind": "topup", "amount_usd": "99.00",
             "created_at": OLD},
        ],
    }


def _payload(monkeypatch, tables=None, broken=(), auth=AUTH_USERS):
    # Pinned: an earlier `import app` loads the repo .env into os.environ.
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tools.ranomics.com")
    client = _Client(tables if tables is not None else _tables(), broken)
    monkeypatch.setattr(shared.credits, "get_service_client", lambda: client)

    def _list(_client):
        if auth is None:
            raise RuntimeError("admin api down")
        return auth

    monkeypatch.setattr(shared.credits, "list_all_auth_users", _list)
    return daily_digest.build_payload(window_hours=24)


def test_internal_accounts_left_out_everywhere(monkeypatch):
    p = _payload(monkeypatch)

    assert p.internal_filter_ok is True
    assert [s["email"] for s in p.new_signups] == ["jane@acmebio.com"]
    assert p.signups_total == 1
    assert p.rejections_total == 1
    assert p.rejections[0]["samples"] == ["x@mailinator.com"]
    assert [r["email"] for r in p.existing_email_watch] == ["bob@gmail.com"]
    assert p.runs_total == 3
    assert p.active_users == 2
    assert {h["email"] for h in p.hot_users} == {"jane@acmebio.com", "bob@gmail.com"}
    assert [u["email"] for u in p.lapsed_users] == ["carol@gmail.com"]
    assert [u["email"] for u in p.paid_users] == ["jane@acmebio.com"]


def test_pricing_counts_distinct_people_not_events(monkeypatch):
    p = _payload(monkeypatch)
    # u-a (twice), u-b (signed in + logged out on s-b), anonymous s-anon
    # (twice); internal s-int dropped.
    assert p.pricing_viewers == 3


def test_money_section(monkeypatch):
    p = _payload(monkeypatch)

    assert p.money_ok is True
    assert p.payments_count == 2
    assert p.payments_users == 1
    assert p.payments_usd == 70.0
    assert p.payments_by_kind == {"topup": 1, "auto_reload": 1}
    # u-a: 10 held - 4 released + 2 charged = 8; u-b: 3.50 run_debit.
    assert p.spend_usd == 11.5
    assert p.spend_users == 2
    assert p.paid_users == [{
        "user_id": "u-a",
        "email": "jane@acmebio.com",
        "paid_usd": 70.0,
        "payments": 2,
        "spend_usd": 8.0,
        "url": "https://tools.ranomics.com/admin/users/u-a",
    }]


def test_ledger_is_paged(monkeypatch):
    monkeypatch.setattr(daily_digest, "_LEDGER_PAGE", 2)
    tables = _tables()
    tables["wallet_transactions"] = [
        _ledger(i, "u-a", "topup", "10.00") for i in range(1, 6)
    ]
    p = _payload(monkeypatch, tables=tables)
    assert p.payments_count == 5
    assert p.payments_usd == 50.0


def test_unreadable_ledger_says_so(monkeypatch):
    p = _payload(monkeypatch, broken=("wallet_transactions",))
    assert p.money_ok is False
    assert p.payments_count == 0
    assert p.runs_total == 3


def test_auth_listing_failure_is_flagged(monkeypatch):
    p = _payload(monkeypatch, auth=None)
    assert p.internal_filter_ok is False


def test_commercial_signals():
    assert daily_digest._commercial_signals(
        "jane@acmebio.com", "Binders against a Therapeutic target for our company"
    ) == ["company domain", "therapeutic", "target", "company"]
    assert daily_digest._commercial_signals("bob@gmail.com", "thesis project") == []
    assert daily_digest._commercial_signals("li@qq.com", "抗原表位") == ["抗原"]
    # Word boundary on the left only: "targets" and "drugs" still match.
    assert daily_digest._commercial_signals("bob@gmail.com", "drugs and targets") == [
        "drug", "target",
    ]


@pytest.mark.parametrize("email, internal", [
    ("leo@ranomics.com", True),
    ("Someone@Ranomics.com", True),
    ("leowan7@gmail.com", True),
    ("leowan7+qa3@gmail.com", True),
    ("leowan@gmail.com", False),
    ("x@notranomics.com", False),
    ("leowan7@outlook.com", False),
    (None, False),
])
def test_is_internal(email, internal):
    assert daily_digest._is_internal(email) is internal


def test_template_renders_new_sections(monkeypatch):
    p = _payload(monkeypatch)
    p.internal_filter_ok = False
    app = Flask(__name__, template_folder=str(Path(daily_digest.__file__).parents[1] / "templates"))
    with app.app_context():
        html = daily_digest.render_digest_html(p)

    assert "People who viewed pricing" in html
    assert "Money" in html
    assert "$70.00" in html
    assert "2 payments from 1 user" in html
    assert "$11.50" in html
    assert "commercial? company domain · therapeutic · target · company" in html
    assert "Could not list accounts" in html


def test_template_renders_unreadable_ledger(monkeypatch):
    p = _payload(monkeypatch, broken=("wallet_transactions",))
    app = Flask(__name__, template_folder=str(Path(daily_digest.__file__).parents[1] / "templates"))
    with app.app_context():
        html = daily_digest.render_digest_html(p)
    assert "wallet ledger could not be read" in html
    assert "Could not list accounts" not in html
