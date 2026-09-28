"""Signup credit expiry, the expiry reminder and the idle balance reminder.

The ``expire_signup_credit`` RPC is simulated in Python by ``_Db._expire``,
mirroring supabase/migrations/0043_signup_credit_expiry.sql; the SQL itself is
not executed here.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared import wallet
from shared.wallet import (
    SIGNUP_CREDIT_EXPIRY_DAYS,
    SIGNUP_CREDIT_USD,
    unspent_signup_credit,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
UID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Fake Supabase
# ---------------------------------------------------------------------------


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.filters = db, table, []
        self.op, self.payload, self.rng = "select", None, None

    def select(self, *_a, **_k):
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def is_(self, col, val):
        assert val == "null"
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def lte(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r[col] <= val)
        return self

    def gt(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r[col] > val)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    def execute(self):
        rows = [r for r in self.db.tables[self.table] if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in rows:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in rows])
        if self.table == "wallet_transactions":
            rows.sort(key=lambda r: r["id"])
        if self.rng:
            rows = rows[self.rng[0]:self.rng[1] + 1]
        return SimpleNamespace(data=[dict(r) for r in rows])


class _Db:
    def __init__(self):
        self.tables = {"user_wallets": [], "wallet_transactions": [], "tool_jobs": []}
        self.next_id = 1
        self.before_rpc = None
        self.auth = None

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        assert name == "expire_signup_credit"
        if self.before_rpc:
            hook, self.before_rpc = self.before_rpc, None
            hook()
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self._expire(**params)))

    # -- helpers ------------------------------------------------------------

    def wallet(self, uid=UID):
        return next(w for w in self.tables["user_wallets"] if w["user_id"] == uid)

    def ledger(self, uid=UID):
        return [r for r in self.tables["wallet_transactions"] if r["user_id"] == uid]

    def add(self, kind, amount, uid=UID, parent=None, key=None):
        row = {"id": self.next_id, "user_id": uid, "kind": kind,
               "amount_usd": str(Decimal(str(amount))), "parent_tx_id": parent,
               "stripe_event_id": key}
        self.next_id += 1
        self.tables["wallet_transactions"].append(row)
        return row["id"]

    def new_wallet(self, uid=UID, expires_at=NOW - timedelta(minutes=1), **extra):
        self.tables["user_wallets"].append({
            "user_id": uid, "signup_credit_expires_at": expires_at.isoformat(),
            "signup_credit_expired_at": None, "signup_credit_reminder_sent_at": None,
            **extra,
        })
        self.add("signup_credit", SIGNUP_CREDIT_USD, uid, key=f"signup_credit:{uid}")

    def balance(self, uid=UID):
        return sum((Decimal(r["amount_usd"]) for r in self.ledger(uid)), Decimal("0"))

    def _expire(self, p_user_id, p_amount_usd, p_seen_last_tx_id):
        amount = Decimal(p_amount_usd)
        assert amount >= 0
        w = self.wallet(p_user_id)
        if w["signup_credit_expired_at"]:
            return "already_expired"
        if w["signup_credit_expires_at"] > NOW.isoformat():
            return "not_due"
        rows = self.ledger(p_user_id)
        if max((r["id"] for r in rows), default=None) != p_seen_last_tx_id:
            return "ledger_moved"
        parents = {r["parent_tx_id"] for r in rows}
        if any(r["kind"] == "hold" and r["id"] not in parents for r in rows):
            return "hold_open"
        grant = sum((Decimal(r["amount_usd"]) for r in rows if r["kind"] == "signup_credit"),
                    Decimal("0"))
        if amount > grant or amount > self.balance(p_user_id):
            return "amount_too_large"
        if amount > 0:
            key = f"signup_credit_expiry:{p_user_id}"
            assert all(r["stripe_event_id"] != key for r in rows), "unique key violated"
            self.add("signup_credit_expiry", -amount, p_user_id, key=key)
        w["signup_credit_expired_at"] = NOW.isoformat()
        return "expired"


@pytest.fixture
def db(monkeypatch):
    d = _Db()
    monkeypatch.setattr(wallet, "get_service_client", lambda: d)
    monkeypatch.setattr("shared.credits.get_service_client", lambda: d)
    return d


def _expiry_rows(db, uid=UID):
    return [r for r in db.ledger(uid) if r["kind"] == "signup_credit_expiry"]


# ---------------------------------------------------------------------------
# The rule: min(grant - spend since grant, balance), floored at 0
# ---------------------------------------------------------------------------


def _rows(*pairs):
    return [{"id": i + 1, "kind": k, "amount_usd": str(a), "parent_tx_id": p}
            for i, (k, a, p) in enumerate(pairs)]


def test_untouched_credit_is_all_unspent():
    rows = _rows(("signup_credit", 20, None))
    assert unspent_signup_credit(rows, Decimal("20")) == Decimal("20")


def test_partial_spend_leaves_the_rest():
    rows = _rows(("signup_credit", 20, None), ("hold", -8, None),
                 ("hold_release", 1.5, 2))
    assert unspent_signup_credit(rows, Decimal("13.5")) == Decimal("13.5")


def test_top_up_after_credit_is_never_counted_as_credit():
    rows = _rows(("signup_credit", 20, None), ("hold", -5, None),
                 ("hold_release", 0, 2), ("topup", 50, None))
    assert unspent_signup_credit(rows, Decimal("65")) == Decimal("15")


def test_spend_beyond_credit_leaves_nothing_even_after_top_up():
    rows = _rows(("signup_credit", 20, None), ("hold", -25, None),
                 ("charge", 0, 2), ("topup", 50, None))
    assert unspent_signup_credit(rows, Decimal("45")) == Decimal("0")


def test_never_more_than_balance():
    rows = _rows(("signup_credit", 20, None), ("dispute_freeze", 0, None))
    assert unspent_signup_credit(rows, Decimal("7")) == Decimal("7")


def test_no_grant_or_already_expired_is_zero():
    assert unspent_signup_credit(_rows(("topup", 50, None)), Decimal("50")) == 0
    rows = _rows(("signup_credit", 20, None), ("signup_credit_expiry", -20, None))
    assert unspent_signup_credit(rows, Decimal("0")) == 0


# ---------------------------------------------------------------------------
# expire_signup_credit
# ---------------------------------------------------------------------------


def test_expiry_after_partial_spend_removes_only_the_rest(db):
    db.new_wallet()
    hold = db.add("hold", -6)
    db.add("hold_release", 2, parent=hold)
    assert wallet.expire_signup_credit(UID) == "expired"
    assert [r["amount_usd"] for r in _expiry_rows(db)] == ["-16.0000"]
    assert db.balance() == 0


def test_expiry_leaves_top_up_untouched(db):
    db.new_wallet()
    hold = db.add("hold", -5)
    db.add("hold_release", 0, parent=hold)
    db.add("topup", 50)
    assert wallet.expire_signup_credit(UID) == "expired"
    assert db.balance() == Decimal("50")


def test_active_hold_skips_and_retries_later(db):
    db.new_wallet()
    hold = db.add("hold", -6)
    assert wallet.expire_signup_credit(UID) == "hold_open"
    assert not _expiry_rows(db) and db.wallet()["signup_credit_expired_at"] is None
    db.add("hold_release", 6, parent=hold)
    assert wallet.expire_signup_credit(UID) == "expired"
    assert db.balance() == 0


def test_hold_landing_between_read_and_write_defers(db):
    db.new_wallet()
    db.before_rpc = lambda: db.add("hold", -3)
    assert wallet.expire_signup_credit(UID) == "ledger_moved"
    assert not _expiry_rows(db)


def test_double_run_debits_once(db):
    db.new_wallet()
    assert wallet.expire_signup_credit(UID) == "expired"
    assert wallet.expire_signup_credit(UID) == "already_expired"
    assert len(_expiry_rows(db)) == 1 and db.balance() == 0


def test_concurrent_runs_from_the_same_snapshot_debit_once(db):
    db.new_wallet()
    # The second caller has already read the ledger; the first commits first.
    db.before_rpc = lambda: wallet.expire_signup_credit(UID)
    assert wallet.expire_signup_credit(UID) in ("already_expired", "ledger_moved")
    assert len(_expiry_rows(db)) == 1 and db.balance() == 0


def test_not_due_is_left_alone(db):
    db.new_wallet(expires_at=NOW + timedelta(days=3))
    assert wallet.expire_signup_credit(UID) == "not_due"
    assert db.balance() == SIGNUP_CREDIT_USD


def test_existing_user_grace_matches_the_constant():
    sql = (Path(__file__).resolve().parents[1]
           / "supabase/migrations/0043_signup_credit_expiry.sql").read_text()
    m = re.search(r"signup_credit_expires_at timestamptz\s+NOT NULL DEFAULT "
                  r"\(now\(\) \+ interval '(\d+) days'\)", sql)
    assert m and int(m.group(1)) == SIGNUP_CREDIT_EXPIRY_DAYS == 30


# ---------------------------------------------------------------------------
# cron.signup_credit.run: reminder once, expiry sweep
# ---------------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(**kw):
        calls.append(kw)
        return True

    monkeypatch.setattr("shared.email.send_signup_credit_expiring_email", fake_send)
    return calls


def test_reminder_sent_once(db, sent):
    from cron.signup_credit import run
    db.new_wallet(expires_at=NOW + timedelta(days=4))
    db.add("hold_release", 0)  # harmless row, keeps the ledger non-trivial
    assert run(now=NOW)["reminded"] == 1
    assert run(now=NOW + timedelta(hours=1))["reminded"] == 0
    assert len(sent) == 1
    assert sent[0]["remaining_usd"] == SIGNUP_CREDIT_USD
    assert db.balance() == SIGNUP_CREDIT_USD


def test_reminder_not_sent_when_credit_is_spent(db, sent):
    from cron.signup_credit import run
    db.new_wallet(expires_at=NOW + timedelta(days=4))
    hold = db.add("hold", -20)
    db.add("charge", 0, parent=hold)
    run(now=NOW)
    assert sent == []


def test_reminder_not_sent_outside_window(db, sent):
    from cron.signup_credit import run
    db.new_wallet(expires_at=NOW + timedelta(days=6))
    run(now=NOW)
    assert sent == []


def test_failed_reminder_is_retried(db, monkeypatch):
    from cron.signup_credit import run
    db.new_wallet(expires_at=NOW + timedelta(days=4))
    monkeypatch.setattr("shared.email.send_signup_credit_expiring_email", lambda **_: False)
    assert run(now=NOW)["errors"] == 1
    assert db.wallet()["signup_credit_reminder_sent_at"] is None


def test_reminder_claim_is_won_once():
    from cron.signup_credit import _claim_reminder
    d = _Db()
    d.new_wallet()
    assert _claim_reminder(d, UID, NOW.isoformat()) is True
    assert _claim_reminder(d, UID, NOW.isoformat()) is False


def test_run_expires_due_wallets(db, sent):
    from cron.signup_credit import run
    db.new_wallet()
    db.new_wallet(uid="u-later", expires_at=NOW + timedelta(days=20))
    summary = run(now=NOW)
    assert summary["expired"] == 1
    assert db.balance() == 0 and db.balance("u-later") == SIGNUP_CREDIT_USD
    assert run(now=NOW)["expired"] == 0


def test_expiring_email_wording(monkeypatch):
    from shared import email
    posted = {}
    monkeypatch.setattr(email, "_resolve_user_email", lambda _uid: "a@example.com")
    monkeypatch.setattr(email, "_post_resend", lambda **kw: posted.update(kw) or True)
    assert email.send_signup_credit_expiring_email(
        user_id=UID, remaining_usd=Decimal("12.3456"),
        expires_at=datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert posted["subject"] == "Your $12.34 free credit expires on October 28, 2026"
    assert "run\n    something" in posted["html_body"].replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# cron.reengagement: 14-day idle, never-ran users, 30-day cap, pagination
# ---------------------------------------------------------------------------


def _user(uid, created, sent_at=None):
    meta = {"reengagement_email_sent_at": sent_at} if sent_at else {}
    return SimpleNamespace(id=uid, email=f"{uid}@example.com",
                           user_metadata=meta, created_at=created)


class _Admin:
    def __init__(self, users):
        self.users, self.calls = users, []

    def list_users(self, page=None, per_page=None):
        self.calls.append((page, per_page))
        start = (page - 1) * per_page
        return self.users[start:start + per_page]


def _reengagement_db(users, jobs):
    d = _Db()
    for u in users:
        d.tables["user_wallets"].append({"user_id": u.id, "balance_usd": 5})
    d.tables["tool_jobs"] = jobs
    d.auth = SimpleNamespace(admin=_Admin(users))
    return d


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def test_idle_reminder_frequency_cap(monkeypatch):
    from cron import reengagement
    users = [
        _user("idle", _ago(60)),
        _user("recent-send", _ago(60), sent_at=_ago(10)),
        _user("old-send", _ago(60), sent_at=_ago(31)),
        _user("active", _ago(60)),
        _user("never-ran-old", _ago(20)),
        _user("never-ran-new", _ago(3)),
    ]
    jobs = [{"user_id": u, "tool": "mpnn", "created_at": _ago(d)}
            for u, d in [("idle", 15), ("recent-send", 20), ("old-send", 20), ("active", 2)]]
    d = _reengagement_db(users, jobs)
    monkeypatch.setattr("shared.credits.get_service_client", lambda: d)
    got = {c.user_id for c in reengagement.find_candidates(now=NOW)}
    assert got == {"idle", "old-send", "never-ran-old"}


def test_reengagement_pages_through_all_users(monkeypatch):
    from cron import reengagement
    monkeypatch.setattr(reengagement, "_USERS_PAGE", 2)
    users = [_user(f"u{i}", _ago(60)) for i in range(5)]
    d = _reengagement_db(users, [])
    monkeypatch.setattr("shared.credits.get_service_client", lambda: d)
    got = {c.user_id for c in reengagement.find_candidates(now=NOW)}
    assert got == {f"u{i}" for i in range(5)}
    assert [p for p, _ in d.auth.admin.calls] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Copy: every template mention of the credit amount states the expiry nearby
# ---------------------------------------------------------------------------

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
_NEAR = 3
_EXPIRY_WORDS = ("signup_credit_expiry_days", "expires")


def test_every_signup_credit_mention_states_the_expiry():
    missing = []
    for path in sorted(_TEMPLATES.rglob("*.html")):
        if path.parts[-2] == "email":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "signup_credit(" not in line or "btn-primary" in line:
                continue
            window = "\n".join(lines[max(0, i - _NEAR):i + _NEAR + 1])
            if not any(w in window for w in _EXPIRY_WORDS):
                missing.append(f"{path.relative_to(_TEMPLATES)}:{i + 1}")
    assert missing == []
