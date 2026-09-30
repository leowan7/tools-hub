"""reengagement:send exit code: a refused address is counted, not a crash;
a systemic Resend failure still exits 1."""

from __future__ import annotations

import pytest

from cron import reengagement
from shared import email

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REFUSED = "user@example.com"
MESSAGE_422 = ('Invalid `to` field. Please use our testing email address '
               'instead of domains like `example.com`.')


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    cands = [reengagement.Candidate(user_id=f"u{i}", email=addr)
             for i, addr in enumerate(["a@lab.org", REFUSED, "b@lab.org"])]
    monkeypatch.setattr(reengagement, "find_candidates", lambda now=None: cands)
    monkeypatch.setattr(reengagement, "_suggested_tools_for",
                        lambda c, base_url: [{"name": "t", "url": "u"}])
    stamped = []
    monkeypatch.setattr(reengagement, "_stamp_reengagement",
                        lambda uid, when: stamped.append(uid) or True)
    from app import create_app

    flask_app = create_app()
    flask_app.stamped = stamped
    return flask_app


def _resend(monkeypatch, refused_resp):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        if json["to"] == [REFUSED]:
            return refused_resp
        sent.append(json["to"][0])
        return _Resp(200, {"id": "resend-1"})

    monkeypatch.setattr(email.requests, "post", fake_post)
    return sent


def test_refused_address_exits_zero_and_the_rest_still_send(app, monkeypatch):
    sent = _resend(monkeypatch, _Resp(422, {"name": "validation_error",
                                            "message": MESSAGE_422}))
    result = app.test_cli_runner().invoke(args=["reengagement:send"])
    assert result.exit_code == 0, result.output
    assert "sent=2 skipped_no_suggestions=0 invalid_recipients=1 errors=0" in result.output
    assert sent == ["a@lab.org", "b@lab.org"]
    assert app.stamped == ["u0", "u2"]


@pytest.mark.parametrize("resp", [
    _Resp(500, {"message": "internal"}),
    _Resp(429, {"message": "rate limited"}),
    _Resp(422, {"name": "validation_error", "message": "Invalid `from` field."}),
])
def test_systemic_failure_still_exits_one(app, monkeypatch, resp):
    _resend(monkeypatch, resp)
    result = app.test_cli_runner().invoke(args=["reengagement:send"])
    assert result.exit_code == 1, result.output
    assert "invalid_recipients=0 errors=1" in result.output


@pytest.mark.parametrize("n, code", [(1, 0), (2, 1)])
def test_every_address_refused(app, monkeypatch, n, code):
    monkeypatch.setattr(reengagement, "find_candidates", lambda now=None: [
        reengagement.Candidate(user_id=f"u{i}", email=REFUSED) for i in range(n)])
    _resend(monkeypatch, _Resp(422, {"name": "validation_error",
                                     "message": MESSAGE_422}))
    result = app.test_cli_runner().invoke(args=["reengagement:send"])
    assert result.exit_code == code, result.output
    assert f"sent=0 skipped_no_suggestions=0 invalid_recipients={n} errors={code}" in result.output
