"""webhooks/resend.py: Svix signature check, idempotent storage, send tags."""

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from flask import Flask

from shared import email as email_mod
from webhooks import resend as resend_webhook

pytestmark = pytest.mark.usefixtures("isolate_supabase")

KEY = b"0123456789abcdef0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(KEY).decode()
USER = "00000000-0000-0000-0000-000000000aaa"


def _sign(msg_id, ts, body, key=KEY):
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def _event(kind="abandoned_cart", user=USER, type_="email.delivered"):
    return {
        "type": type_,
        "created_at": "2026-11-27T10:00:00.000Z",
        "data": {"email_id": "em-1", "to": ["li@qq.com"],
                 "tags": {"kind": kind, "user_id": user}},
    }


class _Store:
    """Fake email_events: a dict keyed on svix_id, first write wins."""

    def __init__(self):
        self.rows = {}
        self.fail = False
        self._row = None

    def table(self, name):
        assert name == "email_events"
        return self

    def upsert(self, row, on_conflict, ignore_duplicates):
        assert (on_conflict, ignore_duplicates) == ("svix_id", True)
        self._row = row
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("db down")
        self.rows.setdefault(self._row["svix_id"], self._row)
        return SimpleNamespace(data=[])


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", SECRET)
    store = _Store()
    monkeypatch.setattr(resend_webhook, "get_service_client", lambda: store)
    app = Flask(__name__)
    resend_webhook.register_resend_webhook(app)
    c = app.test_client()
    c.store = store
    return c


def _post(c, event, msg_id="msg_1", ts=None, sig=None):
    body = json.dumps(event).encode()
    ts = str(int(time.time())) if ts is None else ts
    headers = {"svix-id": msg_id, "svix-timestamp": ts,
               "svix-signature": sig if sig is not None else _sign(msg_id, ts, body)}
    return c.post("/webhooks/resend", data=body, headers=headers)


def test_good_signature_stores_row(client):
    r = _post(client, _event())
    assert r.status_code == 200
    assert client.store.rows["msg_1"] == {
        "svix_id": "msg_1", "resend_email_id": "em-1", "event_type": "delivered",
        "recipient": "li@qq.com", "email_kind": "abandoned_cart", "user_id": USER,
        "event_at": "2026-11-27T10:00:00.000Z",
    }


@pytest.mark.parametrize("sig", ["", "v1,AAAA", "v1," + "A" * 44])
def test_bad_or_missing_signature_rejected(client, sig):
    assert _post(client, _event(), sig=sig).status_code == 400
    assert client.store.rows == {}


def test_wrong_key_rejected(client):
    body = json.dumps(_event()).encode()
    ts = str(int(time.time()))
    sig = _sign("msg_1", ts, body, key=b"some-other-key")
    r = client.post("/webhooks/resend", data=body, headers={
        "svix-id": "msg_1", "svix-timestamp": ts, "svix-signature": sig})
    assert r.status_code == 400


def test_tampered_body_rejected(client):
    ts = str(int(time.time()))
    sig = _sign("msg_1", ts, json.dumps(_event()).encode())
    body = json.dumps(_event(type_="email.clicked")).encode()
    r = client.post("/webhooks/resend", data=body, headers={
        "svix-id": "msg_1", "svix-timestamp": ts, "svix-signature": sig})
    assert r.status_code == 400
    assert client.store.rows == {}


def test_signature_bound_to_svix_id(client):
    body = json.dumps(_event()).encode()
    ts = str(int(time.time()))
    r = client.post("/webhooks/resend", data=body, headers={
        "svix-id": "msg_other", "svix-timestamp": ts,
        "svix-signature": _sign("msg_1", ts, body)})
    assert r.status_code == 400


def test_non_ascii_signature_rejected_not_500(client):
    assert _post(client, _event(), sig="v1,é").status_code == 400


def test_stale_timestamp_rejected(client):
    assert _post(client, _event(), ts=str(int(time.time()) - 600)).status_code == 400


def test_second_signature_in_header_accepted():
    body = json.dumps(_event()).encode()
    ts = str(int(time.time()))
    header = "v1,bm90LWl0 " + _sign("msg_1", ts, body)
    assert resend_webhook.verify_signature(SECRET, "msg_1", ts, header, body)


def test_unset_secret_refuses(client, monkeypatch):
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "")
    assert _post(client, _event()).status_code == 503
    assert client.store.rows == {}


def test_redelivery_keeps_one_row(client):
    assert _post(client, _event()).status_code == 200
    # Same svix-id redelivered: first row stays, no second row.
    assert _post(client, _event(type_="email.opened")).status_code == 200
    assert list(client.store.rows) == ["msg_1"]
    assert client.store.rows["msg_1"]["event_type"] == "delivered"


def test_store_failure_asks_for_retry(client):
    client.store.fail = True
    assert _post(client, _event()).status_code == 503


def test_unknown_type_ignored_and_bad_user_tag_dropped(client):
    assert _post(client, _event(type_="contact.created")).get_json() == {"status": "ignored"}
    assert client.store.rows == {}
    assert _post(client, _event(user="?"), msg_id="msg_2").status_code == 200
    assert client.store.rows["msg_2"]["user_id"] is None


@pytest.mark.parametrize("log_tag, tags", [
    (f"abandoned_cart user={USER}", [{"name": "kind", "value": "abandoned_cart"},
                                     {"name": "user_id", "value": USER}]),
    ("reengagement user=?", [{"name": "kind", "value": "reengagement"}]),
    ("daily_digest", [{"name": "kind", "value": "daily_digest"}]),
])
def test_resend_tags(log_tag, tags):
    assert email_mod._resend_tags(log_tag) == tags


def test_post_resend_sends_tags(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    sent = {}

    def _fake_post(url, json, headers, timeout):
        sent.update(json)
        return SimpleNamespace(status_code=200, json=lambda: {"id": "em-1"})

    monkeypatch.setattr(email_mod.requests, "post", _fake_post)
    assert email_mod._post_resend(to_email="a@b.com", subject="s", html_body="<p>x</p>",
                                  log_tag=f"welcome user={USER}")
    assert sent["tags"] == [{"name": "kind", "value": "welcome"},
                            {"name": "user_id", "value": USER}]
