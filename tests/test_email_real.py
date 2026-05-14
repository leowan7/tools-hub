"""Wave 2 wallet email senders: real Resend calls (Resend mocked).

These tests exercise the senders in ``shared.email`` that Wave 2 Agent G
wired up. The Resend HTTP layer is mocked, so the suite asserts:

* Each sender renders its template without exceptions.
* Each sender invokes Resend with the right from/to and a non-empty
  subject + html body.
* ``alert_sales_slack`` / ``alert_ops_slack`` log without raising when
  their webhook env var is unset.
* Each sender's rendered HTML body is dash free (no em dashes, no en
  dashes, no connector hyphens in user-facing strings). Identifiers,
  slugs, and URL paths are explicitly allowed to keep hyphens.

The user-id to email resolver is also mocked. Tests do not touch the
Supabase service-role client.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from shared import email as email_mod


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


TEST_USER_ID = "00000000-0000-0000-0000-0000000000aa"
TEST_USER_EMAIL = "leo@example.com"

EM_DASH = "—"
EN_DASH = "–"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Set the minimum env needed for the senders to attempt a send."""
    monkeypatch.setenv("RESEND_API_KEY", "test-key-xxx")
    monkeypatch.setenv("EMAIL_FROM", "Ranomics Tools <noreply@tools.ranomics.com>")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tools.ranomics.com")
    monkeypatch.setenv("WALLET_SIGNUP_CREDIT_USD", "5")
    monkeypatch.setenv("WALLET_DEFAULT_DAILY_CAP_USD", "200")
    monkeypatch.setenv("SUPPORT_EMAIL", "support@ranomics.com")
    # Clear Slack webhooks by default so the Slack tests can flip per-test.
    monkeypatch.delenv("SLACK_SALES_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SLACK_OPS_WEBHOOK_URL", raising=False)
    return monkeypatch


@pytest.fixture
def resolve_email():
    """Mock the user-id to email resolver so tests do not hit Supabase."""
    with patch.object(
        email_mod, "_resolve_user_email", return_value=TEST_USER_EMAIL
    ) as m:
        yield m


@pytest.fixture
def mock_resend():
    """Mock the Resend HTTP layer; capture the last POST payload."""
    captured: dict = {}

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "re_test_id"}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResp()

    with patch.object(email_mod.requests, "post", side_effect=_fake_post):
        yield captured


def _assert_resend_call_shape(captured: dict, log_label: str) -> None:
    """Common assertions on the Resend HTTP envelope."""
    assert captured.get("url") == email_mod.RESEND_ENDPOINT, log_label
    body = captured.get("json") or {}
    assert body.get("from") and "@" in body["from"], log_label
    assert body.get("to") == [TEST_USER_EMAIL], log_label
    assert isinstance(body.get("subject"), str) and body["subject"].strip(), log_label
    assert isinstance(body.get("html"), str) and len(body["html"]) > 100, log_label
    headers = captured.get("headers") or {}
    assert headers.get("Authorization", "").startswith("Bearer "), log_label
    assert headers.get("Content-Type") == "application/json", log_label


# ---------------------------------------------------------------------------
# Dash-freeness check (user-facing body strings only)
# ---------------------------------------------------------------------------


def _visible_text(html: str) -> str:
    """Strip jinja comments, HTML tags, URLs, and code-token contents.

    What is left is the prose the user actually reads. URLs are stripped
    because connector hyphens inside URL slugs are explicitly allowed.
    Same for the dispute id reference in <code>...</code>.
    """
    # Jinja comments are never rendered.
    txt = re.sub(r"\{#.*?#\}", "", html, flags=re.S)
    # Strip the inside of href="..." / src="..." / style="..." attributes
    # because they contain CSS identifiers (font-family, etc.) and URLs.
    txt = re.sub(r'\b(?:href|src|style)\s*=\s*"[^"]*"', "", txt, flags=re.I)
    txt = re.sub(r"\b(?:href|src|style)\s*=\s*'[^']*'", "", txt, flags=re.I)
    # Strip <code>...</code> blocks (treated as identifiers).
    txt = re.sub(r"(?is)<code>.*?</code>", "", txt)
    # Drop all remaining tags.
    txt = re.sub(r"<[^>]+>", " ", txt)
    # Drop full URLs that might appear as visible text.
    txt = re.sub(r"https?://\S+", "", txt)
    # Drop email addresses (slug-style identifiers).
    txt = re.sub(r"[\w.+-]+@[\w.-]+", "", txt)
    txt = re.sub(r"&nbsp;|&amp;", " ", txt)
    return txt


def _assert_dash_free(body_html: str, what: str) -> None:
    """Fail if the rendered body has em/en dashes or connector hyphens in prose."""
    text = _visible_text(body_html)
    assert EM_DASH not in text, f"{what}: em dash in user-facing text"
    assert EN_DASH not in text, f"{what}: en dash in user-facing text"
    matches = re.findall(r"[A-Za-z]+-[A-Za-z]+", text)
    assert not matches, (
        f"{what}: connector hyphen(s) found in user-facing text: {matches!r}"
    )


# ===========================================================================
# Per-sender renders and Resend invocations
# ===========================================================================


class TestSignupCredit:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_signup_credit_email(user_id=TEST_USER_ID)
        assert ok is True
        _assert_resend_call_shape(mock_resend, "signup_credit")
        body = mock_resend["json"]
        # Subject should include the $5 dollar amount.
        assert "$5" in body["subject"]
        assert "compute credit" in body["subject"].lower()
        # Body must contain the credit amount and the catalog URL.
        assert "$5" in body["html"]
        assert "tools.ranomics.com" in body["html"]
        _assert_dash_free(body["html"], "signup_credit")

    def test_no_email_returns_false(self, env, mock_resend):
        with patch.object(email_mod, "_resolve_user_email", return_value=None):
            ok = email_mod.send_signup_credit_email(user_id=TEST_USER_ID)
        assert ok is False
        # Resend should not have been called.
        assert mock_resend == {}


class TestTopupConfirmation:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_topup_confirmation_email(
            user_id=TEST_USER_ID,
            amount_usd=20,
            new_balance_usd=25,
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "topup_confirmation")
        body = mock_resend["json"]
        assert "$20" in body["subject"]
        assert "$20" in body["html"]
        assert "$25" in body["html"]
        _assert_dash_free(body["html"], "topup_confirmation")

    def test_handles_missing_new_balance(self, env, resolve_email, mock_resend):
        ok = email_mod.send_topup_confirmation_email(
            user_id=TEST_USER_ID,
            amount_usd=20,
        )
        assert ok is True


class TestAutoReloadCharged:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_auto_reload_charged_email(
            user_id=TEST_USER_ID,
            amount_usd=50,
            new_balance_usd=60,
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "auto_reload_charged")
        body = mock_resend["json"]
        assert "$50" in body["subject"]
        assert "$50" in body["html"]
        assert "$60" in body["html"]
        _assert_dash_free(body["html"], "auto_reload_charged")


class TestAutoReloadFailed:
    def test_renders_known_reason(self, env, resolve_email, mock_resend):
        ok = email_mod.send_auto_reload_failed_email(
            user_id=TEST_USER_ID,
            reason="no_payment_method",
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "auto_reload_failed")
        body = mock_resend["json"]
        # Reason gets humanised by the sender.
        assert "no saved card" in body["html"].lower()
        _assert_dash_free(body["html"], "auto_reload_failed")

    def test_renders_unknown_reason(self, env, resolve_email, mock_resend):
        ok = email_mod.send_auto_reload_failed_email(
            user_id=TEST_USER_ID,
            reason="weird_unmapped_code",
        )
        assert ok is True


class TestAutoReloadRateLimited:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_auto_reload_rate_limited_email(
            user_id=TEST_USER_ID
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "auto_reload_rate_limited")
        body = mock_resend["json"]
        assert "skipped" in body["subject"].lower()
        _assert_dash_free(body["html"], "auto_reload_rate_limited")


class TestAutoReloadMonthlyCap:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_auto_reload_monthly_cap_email(
            user_id=TEST_USER_ID,
            total_usd=850,
            cap_usd=1000,
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "auto_reload_monthly_cap")
        body = mock_resend["json"]
        assert "$850" in body["html"]
        assert "$1000" in body["html"]
        _assert_dash_free(body["html"], "auto_reload_monthly_cap")


class TestLowBalance:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_low_balance_email(
            user_id=TEST_USER_ID,
            balance_usd="3.45",
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "low_balance")
        body = mock_resend["json"]
        assert "$3.45" in body["html"]
        _assert_dash_free(body["html"], "low_balance")


class TestJobCapped:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_job_capped_email(
            user_id=TEST_USER_ID,
            tool_slug="bindcraft",
            attempted_usd=120,
            cap_usd=100,
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "job_capped")
        body = mock_resend["json"]
        # The human-readable tool label should appear in the subject.
        assert "BindCraft" in body["subject"]
        assert "BindCraft" in body["html"]
        assert "$120" in body["html"]
        assert "$100" in body["html"]
        _assert_dash_free(body["html"], "job_capped")

    def test_unknown_slug_falls_back(self, env, resolve_email, mock_resend):
        ok = email_mod.send_job_capped_email(
            user_id=TEST_USER_ID,
            tool_slug="future-tool",
            attempted_usd=1,
            cap_usd=0,
        )
        assert ok is True


class TestDailyCap:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_daily_cap_email(
            user_id=TEST_USER_ID,
            cap_usd=200,
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "daily_cap")
        body = mock_resend["json"]
        assert "$200" in body["html"]
        _assert_dash_free(body["html"], "daily_cap")


class TestPilotIntro:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_pilot_intro_email(
            user_id=TEST_USER_ID,
            spent_30d_usd=1234.50,
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "pilot_intro")
        body = mock_resend["json"]
        assert "Binder Pilot" in body["subject"]
        assert "$1234.50" in body["html"]
        assert "binder-pilot" in body["html"]  # URL slug ok with hyphen
        _assert_dash_free(body["html"], "pilot_intro")


class TestWalletFrozen:
    def test_renders_and_sends(self, env, resolve_email, mock_resend):
        ok = email_mod.send_wallet_frozen_email(
            user_id=TEST_USER_ID,
            dispute_id="dp_test_123",
        )
        assert ok is True
        _assert_resend_call_shape(mock_resend, "wallet_frozen")
        body = mock_resend["json"]
        assert "frozen" in body["subject"].lower()
        # Dispute id is rendered inside <code> which is treated as identifier
        # in the dash check, but should still appear in the raw HTML.
        assert "dp_test_123" in body["html"]
        assert "support@ranomics.com" in body["html"]
        _assert_dash_free(body["html"], "wallet_frozen")


# ===========================================================================
# Resend transport contract
# ===========================================================================


class TestResendContract:
    """Cross-sender contract: every sender must hit Resend with the right
    from/to/subject/html shape."""

    SENDER_CALLS = [
        ("send_signup_credit_email",
         {"user_id": TEST_USER_ID}),
        ("send_topup_confirmation_email",
         {"user_id": TEST_USER_ID, "amount_usd": 20, "new_balance_usd": 25}),
        ("send_auto_reload_charged_email",
         {"user_id": TEST_USER_ID, "amount_usd": 50, "new_balance_usd": 60}),
        ("send_auto_reload_failed_email",
         {"user_id": TEST_USER_ID, "reason": "card_declined"}),
        ("send_auto_reload_rate_limited_email",
         {"user_id": TEST_USER_ID}),
        ("send_auto_reload_monthly_cap_email",
         {"user_id": TEST_USER_ID, "total_usd": 500, "cap_usd": 1000}),
        ("send_low_balance_email",
         {"user_id": TEST_USER_ID, "balance_usd": 2}),
        ("send_job_capped_email",
         {"user_id": TEST_USER_ID, "tool_slug": "boltzgen",
          "attempted_usd": 200, "cap_usd": 150}),
        ("send_daily_cap_email",
         {"user_id": TEST_USER_ID, "cap_usd": 200}),
        ("send_pilot_intro_email",
         {"user_id": TEST_USER_ID, "spent_30d_usd": 1200}),
        ("send_wallet_frozen_email",
         {"user_id": TEST_USER_ID, "dispute_id": "dp_x"}),
    ]

    @pytest.mark.parametrize("name,kwargs", SENDER_CALLS)
    def test_sender_invokes_resend(
        self, env, resolve_email, mock_resend, name, kwargs
    ):
        sender = getattr(email_mod, name)
        ok = sender(**kwargs)
        assert ok is True, f"{name} did not return True"
        _assert_resend_call_shape(mock_resend, name)

    @pytest.mark.parametrize("name,kwargs", SENDER_CALLS)
    def test_sender_html_is_dash_free(
        self, env, resolve_email, mock_resend, name, kwargs
    ):
        sender = getattr(email_mod, name)
        sender(**kwargs)
        body = mock_resend["json"]
        _assert_dash_free(body["html"], name)
        # Subjects must also be dash free.
        subj = body["subject"]
        assert EM_DASH not in subj, f"{name}: em dash in subject"
        assert EN_DASH not in subj, f"{name}: en dash in subject"
        subj_compact = re.sub(r"https?://\S+", "", subj)
        subj_matches = re.findall(r"[A-Za-z]+-[A-Za-z]+", subj_compact)
        assert not subj_matches, (
            f"{name}: connector hyphen in subject: {subj_matches!r}"
        )

    @pytest.mark.parametrize("name,kwargs", SENDER_CALLS)
    def test_sender_swallows_resend_failure(
        self, env, resolve_email, name, kwargs
    ):
        """When Resend POST raises, the sender returns False without raising."""
        with patch.object(
            email_mod.requests, "post", side_effect=RuntimeError("boom")
        ):
            sender = getattr(email_mod, name)
            ok = sender(**kwargs)
        assert ok is False

    @pytest.mark.parametrize("name,kwargs", SENDER_CALLS)
    def test_sender_skips_when_no_api_key(
        self, monkeypatch, resolve_email, mock_resend, name, kwargs
    ):
        """Without RESEND_API_KEY the sender logs and returns False."""
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://tools.ranomics.com")
        sender = getattr(email_mod, name)
        ok = sender(**kwargs)
        assert ok is False
        # No HTTP call made.
        assert mock_resend == {}


# ===========================================================================
# Slack alerters
# ===========================================================================


class TestSalesSlack:
    def test_no_webhook_logs_and_returns_false(self, env, resolve_email, caplog):
        """When the Slack webhook env is unset, log without raising."""
        with caplog.at_level("INFO", logger="shared.email"):
            ok = email_mod.alert_sales_slack(
                user_id=TEST_USER_ID, spent_30d_usd=5000
            )
        assert ok is False
        assert any(
            "no webhook URL" in r.message for r in caplog.records
        )

    def test_with_webhook_posts(self, env, resolve_email, monkeypatch):
        """When the webhook env is set, POST a Slack payload."""
        monkeypatch.setenv("SLACK_SALES_WEBHOOK_URL", "https://hooks.slack.com/test")
        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""

        def _fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResp()

        with patch.object(email_mod.requests, "post", side_effect=_fake_post):
            ok = email_mod.alert_sales_slack(
                user_id=TEST_USER_ID, spent_30d_usd=5000
            )
        assert ok is True
        assert captured["url"] == "https://hooks.slack.com/test"
        text = captured["json"]["text"]
        assert "$5000" in text
        assert TEST_USER_ID in text

    def test_falls_back_to_funnel_alert_env_var(
        self, env, resolve_email, monkeypatch
    ):
        """The plan's Railway table uses WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL.

        Either env name should route to the same handler.
        """
        monkeypatch.setenv(
            "WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL",
            "https://hooks.slack.com/funnel",
        )
        with patch.object(
            email_mod.requests, "post"
        ) as fake_post:
            fake_post.return_value.status_code = 200
            ok = email_mod.alert_sales_slack(
                user_id=TEST_USER_ID, spent_30d_usd=5000
            )
        assert ok is True
        assert fake_post.called
        called_url = fake_post.call_args[0][0]
        assert called_url == "https://hooks.slack.com/funnel"

    def test_does_not_raise_on_transport_error(
        self, env, resolve_email, monkeypatch
    ):
        monkeypatch.setenv("SLACK_SALES_WEBHOOK_URL", "https://hooks.slack.com/test")
        with patch.object(
            email_mod.requests, "post", side_effect=RuntimeError("boom")
        ):
            ok = email_mod.alert_sales_slack(
                user_id=TEST_USER_ID, spent_30d_usd=5000
            )
        assert ok is False


class TestSalesSlackHigh:
    def test_no_webhook_logs_and_returns_false(self, env, resolve_email):
        ok = email_mod.alert_sales_slack_high(
            user_id=TEST_USER_ID, spent_30d_usd=10500
        )
        assert ok is False


class TestOpsSlack:
    def test_no_webhook_logs_and_returns_false(self, env, caplog):
        with caplog.at_level("INFO", logger="shared.email"):
            ok = email_mod.alert_ops_slack(
                event="wallet_frozen",
                user_id=TEST_USER_ID,
                dispute_id="dp_test",
            )
        assert ok is False
        assert any(
            "no webhook URL" in r.message for r in caplog.records
        )

    def test_with_webhook_posts(self, env, monkeypatch):
        monkeypatch.setenv("SLACK_OPS_WEBHOOK_URL", "https://hooks.slack.com/ops")
        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""

        def _fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResp()

        with patch.object(email_mod.requests, "post", side_effect=_fake_post):
            ok = email_mod.alert_ops_slack(
                event="wallet_frozen",
                user_id=TEST_USER_ID,
                dispute_id="dp_test_99",
            )
        assert ok is True
        text = captured["json"]["text"]
        assert "wallet_frozen" in text
        assert "dp_test_99" in text


# ===========================================================================
# Backward compat: existing call sites must keep working.
# ===========================================================================


class TestBackwardCompat:
    """The wallet code calls these by name through ``_send_email_safe``,
    which forwards arbitrary kwargs. The new signatures accept ``**_extra``
    so a forward-compatible extra kwarg should not raise."""

    def test_extra_kwargs_do_not_raise(self, env, resolve_email, mock_resend):
        ok = email_mod.send_low_balance_email(
            user_id=TEST_USER_ID,
            balance_usd=2,
            # Future fields the wallet might pass:
            wallet_id="w_x",
            future_field=True,
        )
        assert ok is True

    def test_decimal_amounts_accepted(self, env, resolve_email, mock_resend):
        """The wallet passes Decimal values; sender must format them OK."""
        from decimal import Decimal as D  # noqa: PLC0415

        ok = email_mod.send_low_balance_email(
            user_id=TEST_USER_ID,
            balance_usd=D("3.456789"),
        )
        assert ok is True
        body = mock_resend["json"]
        # Two decimal rendering for non-integer values.
        assert "$3.46" in body["html"] or "$3.45" in body["html"]
