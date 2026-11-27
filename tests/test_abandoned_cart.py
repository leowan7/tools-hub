"""Abandoned-cart email: selection rules, flag gate, English template."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cron import abandoned_cart as ac
from shared import email

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _ev(uid="u1", kind="tool_form_open", ago=timedelta(hours=2), tool="boltz2", path=None):
    return {"user_id": uid, "event_type": kind, "created_at": (NOW - ago).isoformat(),
            "props": {"tool": tool} if tool else {}, "path": path or f"/tools/{tool}"}


USER = {"email": "bench@lab.org", "user_metadata": {}}


def _select(events, *, balances=None, topups=(), jobs=(), users=None):
    users = {"u1": USER} if users is None else users
    return ac.select_candidates(
        events=events, balances=balances or {}, topups=list(topups), jobs=list(jobs),
        get_user=users.get, now=NOW,
        tool_label=lambda s: {"boltz2": "Boltz-2", "bindcraft": "BindCraft"}.get(s, ""),
    )


def test_zero_balance_user_who_opened_a_form_two_hours_ago_qualifies():
    [c] = _select([_ev()])
    assert (c.user_id, c.email, c.tool, c.tool_label) == ("u1", "bench@lab.org", "boltz2", "Boltz-2")


@pytest.mark.parametrize("event", [
    _ev(kind="credit_exhausted"),
    _ev(kind="page_view", tool=None, path="/account/wallet/topup?tool=bindcraft"),
])
def test_pay_gate_and_topup_page_also_trigger(event):
    [c] = _select([event])
    assert c.user_id == "u1"


def test_topup_page_names_the_tool_from_its_query():
    [c] = _select([_ev(kind="page_view", tool=None, path="/account/wallet/topup?tool=bindcraft")])
    assert c.tool == "bindcraft"


def test_form_then_bare_topup_visit_keeps_the_tool_in_the_subject(monkeypatch):
    [c] = _select([_ev(ago=timedelta(hours=3)),
                   _ev(kind="page_view", tool=None, path="/account/wallet/topup")])
    assert (c.tool, c.tool_label) == ("boltz2", "Boltz-2")
    sent = {}
    monkeypatch.setenv("SESSION_SECRET_KEY", "k")
    monkeypatch.setattr(email, "_post_resend", lambda **kw: sent.update(kw) or True)
    assert email.send_abandoned_cart_email(candidate=c, base_url="https://t.example")
    assert sent["subject"] == "Your Boltz-2 run is waiting"


def test_other_page_views_do_not_trigger():
    assert _select([_ev(kind="page_view", tool=None, path="/pricing")]) == []


@pytest.mark.parametrize("ago", [timedelta(minutes=30), timedelta(hours=25)])
def test_last_event_must_be_one_to_24_hours_old(ago):
    assert _select([_ev(ago=ago)]) == []


def test_a_newer_event_inside_the_hour_waits():
    assert _select([_ev(ago=timedelta(hours=3)), _ev(ago=timedelta(minutes=10))]) == []


def test_positive_balance_is_skipped_and_missing_wallet_counts_as_zero():
    assert _select([_ev()], balances={"u1": 0.01}) == []
    assert len(_select([_ev()], balances={})) == 1


def test_topup_or_job_after_the_event_skips():
    assert _select([_ev()], topups=[{"user_id": "u1", "created_at": _ago(minutes=90)}]) == []
    assert _select([_ev()], jobs=[{"user_id": "u1", "created_at": _ago(minutes=90)}]) == []


def test_topup_before_the_event_does_not_skip():
    assert len(_select([_ev()], topups=[{"user_id": "u1", "created_at": _ago(hours=5)}])) == 1


def test_unsubscribed_user_is_skipped():
    users = {"u1": {"email": "bench@lab.org",
                    "user_metadata": {"email_preferences": {"marketing_email": False}}}}
    assert _select([_ev()], users=users) == []


@pytest.mark.parametrize("addr", ["leo@ranomics.com", "leowan7+x@gmail.com"])
def test_internal_accounts_are_skipped(addr):
    assert _select([_ev()], users={"u1": {"email": addr, "user_metadata": {}}}) == []


@pytest.mark.parametrize("meta, qualifies", [
    ({ac.STAMP_KEY: _ago(days=3), ac.COUNT_KEY: 1}, False),   # inside 7-day cooldown
    ({ac.STAMP_KEY: _ago(days=8), ac.COUNT_KEY: 1}, True),    # cooldown over
    ({ac.STAMP_KEY: _ago(days=30), ac.COUNT_KEY: 2}, False),  # two ever
])
def test_cooldown_and_lifetime_cap(meta, qualifies):
    users = {"u1": {"email": "bench@lab.org", "user_metadata": meta}}
    assert bool(_select([_ev()], users=users)) is qualifies


def test_tool_not_live_falls_back_to_no_tool():
    [c] = _select([_ev(tool="secret-tool")])
    assert (c.tool, c.tool_label) == ("", "")


def test_user_lookup_only_for_users_past_the_row_rules():
    asked = []
    ac.select_candidates(
        events=[_ev("u1"), _ev("u2")], balances={"u2": 5.0}, topups=[], jobs=[],
        get_user=lambda uid: asked.append(uid) or USER, now=NOW,
    )
    assert asked == ["u1"]


def test_flag_off_sends_nothing(monkeypatch):
    monkeypatch.delenv("ABANDONED_CART_EMAIL_ENABLED", raising=False)
    monkeypatch.setattr(ac, "find_candidates", lambda now: pytest.fail("swept with flag off"))
    assert ac.send_abandoned_cart(now=NOW) == {"enabled": False, "qualified": 0,
                                                "sent": 0, "errors": 0}


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("ABANDONED_CART_EMAIL_ENABLED", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    cands = [ac.Candidate("u1", "a@lab.org"), ac.Candidate("u2", "b@lab.org")]
    monkeypatch.setattr(ac, "find_candidates", lambda now: cands)
    log = []
    monkeypatch.setattr(ac, "_unclaim", lambda uid, before: log.append(("unclaim", uid)))
    return log


def test_flag_on_claims_before_sending_and_skips_blocked(monkeypatch, flag_on):
    monkeypatch.setattr(ac, "_claim",
                        lambda uid, now: flag_on.append(("claim", uid)) or ({} if uid == "u1" else None))
    monkeypatch.setattr(email, "send_abandoned_cart_email",
                        lambda candidate, base_url: flag_on.append(("send", candidate.user_id)) or True)
    assert ac.send_abandoned_cart(now=NOW)["sent"] == 1
    assert flag_on == [("claim", "u1"), ("send", "u1"), ("claim", "u2")]


def test_failed_send_puts_the_stamp_back_and_claim_errors_count(monkeypatch, flag_on):
    def claim(uid, now):
        if uid == "u2":
            raise RuntimeError("gotrue down")
        return {}
    monkeypatch.setattr(ac, "_claim", claim)
    monkeypatch.setattr(email, "send_abandoned_cart_email", lambda candidate, base_url: False)
    assert ac.send_abandoned_cart(now=NOW)["errors"] == 2
    assert flag_on == [("unclaim", "u1")]


@pytest.mark.parametrize("unset", ["RESEND_API_KEY", "SESSION_SECRET_KEY"])
def test_missing_mail_config_stamps_nobody(monkeypatch, flag_on, unset):
    monkeypatch.delenv(unset)
    monkeypatch.setattr(ac, "_claim", lambda uid, now: pytest.fail("stamped without mail config"))
    assert ac.send_abandoned_cart(now=NOW)["errors"] == 1


def _render(monkeypatch, cand, *, wechat):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WECHAT_PAY_ENABLED", "1" if wechat else "0")
    sent = {}
    monkeypatch.setattr(email, "_post_resend", lambda **kw: sent.update(kw) or True)
    assert email.send_abandoned_cart_email(candidate=cand, base_url="https://t.example")
    return sent


def test_email_names_the_tool_with_links_in_english_only(monkeypatch):
    sent = _render(monkeypatch, ac.Candidate("u1", "a@lab.org", "boltz2", "Boltz-2"), wechat=False)
    for body in (sent["html_body"], sent["text_body"]):
        assert "Your Boltz-2 run is waiting" in body
        assert "https://t.example/tools/boltz2?utm_source=email&" in body.replace("&amp;", "&")
        assert "/account/wallet/topup?tool=boltz2&utm_source=email&utm_medium=abandoned_cart" \
            in body.replace("&amp;", "&")
        assert "it is not charged" in body
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in body)
        assert "WeChat" not in body
        assert "$" not in body
    assert sent["subject"] == "Your Boltz-2 run is waiting" and sent["unsubscribe_link"]
    assert "/email/unsubscribe/" in sent["html_body"]


def test_email_mentions_wechat_only_when_the_flag_is_on(monkeypatch):
    sent = _render(monkeypatch, ac.Candidate("u1", "a@lab.org"), wechat=True)
    for body in (sent["html_body"], sent["text_body"]):
        assert "WeChat Pay" in body
        assert "https://t.example/tools?utm_source=email" in body
        assert "Your run is waiting" in body
    assert sent["subject"] == "Your Ranomics Tools run is waiting"


class _Q:
    def __init__(self, rows):
        self.rows, self.neg = [dict(r) for r in rows], False

    def _keep(self, ok):
        self.rows = [r for r in self.rows if ok(r) != self.neg]
        self.neg = False
        return self

    @property
    def not_(self):
        self.neg = True
        return self

    def select(self, *_):
        return self

    def gte(self, c, v):
        return self._keep(lambda r: str(r.get(c) or "") >= v)

    def eq(self, c, v):
        return self._keep(lambda r: r.get(c) == v)

    def in_(self, c, vs):
        return self._keep(lambda r: r.get(c) in vs)

    def is_(self, c, _null):
        return self._keep(lambda r: r.get(c) is None)

    def like(self, c, pat):
        return self._keep(lambda r: str(r.get(c) or "").startswith(pat.rstrip("%")))

    def order(self, *_):
        return self

    def range(self, a, b):
        self.rows = self.rows[a:b + 1]
        return self

    def execute(self):
        from types import SimpleNamespace
        return SimpleNamespace(data=self.rows)


def test_find_candidates_skips_frozen_wallets_and_runs_in_flight(monkeypatch):
    import shared.credits
    from types import SimpleNamespace

    tables = {
        "user_events": [_ev("u1"), _ev("u2"), _ev("u3"), _ev(None),
                        _ev("u4", kind="page_view", tool=None, path="/account/wallet/topup")],
        "user_wallets": [{"user_id": "u2", "balance_usd": 0, "wallet_frozen": True}],
        "tool_jobs": [{"user_id": "u3", "status": "running", "created_at": _ago(days=2)}],
        "wallet_transactions": [],
    }
    auth = SimpleNamespace(admin=SimpleNamespace(
        get_user_by_id=lambda uid: SimpleNamespace(user=SimpleNamespace(
            email=f"{uid}@lab.org", user_metadata={}))))
    client = SimpleNamespace(table=lambda name: _Q(tables[name]), auth=auth)
    monkeypatch.setattr(shared.credits, "get_service_client", lambda: client)
    monkeypatch.setattr(ac, "_live_tool_label", lambda slug: "Boltz-2" if slug == "boltz2" else "")
    assert sorted(c.user_id for c in ac.find_candidates(now=NOW)) == ["u1", "u4"]


def test_unclaim_clears_a_first_stamp_and_keeps_a_new_unsubscribe(monkeypatch):
    import shared.credits
    from types import SimpleNamespace

    now_meta = {ac.STAMP_KEY: NOW.isoformat(), ac.COUNT_KEY: 1,
                "email_preferences": {"marketing_email": False}}
    written = []
    admin = SimpleNamespace(
        get_user_by_id=lambda uid: SimpleNamespace(user=SimpleNamespace(
            email="a@lab.org", user_metadata=dict(now_meta))),
        update_user_by_id=lambda uid, body: written.append(body["user_metadata"]))
    monkeypatch.setattr(shared.credits, "get_service_client",
                        lambda: SimpleNamespace(auth=SimpleNamespace(admin=admin)))
    ac._unclaim("u1", before={})
    [meta] = written
    assert meta[ac.STAMP_KEY] is None and meta[ac.COUNT_KEY] is None
    assert meta["email_preferences"] == {"marketing_email": False}
    assert not ac._blocked_by_history(meta, NOW)
