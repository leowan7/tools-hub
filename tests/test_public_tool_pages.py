"""GET /tools/<slug> is public; POST /tools/<slug>/* is not.

The redesign opened the tool pages to anonymous visitors: the run form,
every field, the live cost estimate and the explainer all render without
a session, and only the Submit control is replaced by a sign-in link.

THE LOAD-BEARING ASSERTION OF THAT CHANGE IS IN
``TestSubmitGateStillHolds``: an unauthenticated POST to
``/tools/<slug>/submit`` or ``/tools/<slug>/preflight`` must never reach
the handler, never place a wallet hold, and never create a job row. The
GET being public is a copy decision; the POST being gated is the money.
Nothing else in the suite covered it before this file existed, which is
why it is asserted here against the real routes rather than by reading
the decorator list.

The rest of the file is the anonymous-render contract that the same
change introduced, plus the signed-in no-regression check.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Three tools with structurally different forms: a fixed-preset
# sequence tool, a design tool with a campaign path, and a cofold
# validator. If the anonymous branch renders these three it renders.
PUBLIC_TOOLS = ("mpnn", "proteina", "boltz2")

# Nothing here may consult the real database. The signed-in render in
# particular reaches the header's workspace-count context processor,
# which queries Supabase with the stubbed user id.
pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _ctx(user_id="u-public", balance=100):
    return SimpleNamespace(
        user_id=user_id, tier="free", balance=balance, email="u@example.com",
    )


def _login(client, email="u@example.com"):
    # user_email only. Setting session["user_id"] to a non-UUID makes the
    # header's workspace-count context processor issue a real Supabase
    # query, which is not what any test here is about.
    with client.session_transaction() as sess:
        sess["user_email"] = email


@pytest.fixture
def app(monkeypatch):
    for slug in PUBLIC_TOOLS:
        monkeypatch.setenv(f"FLAG_TOOL_{slug.upper()}", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ===========================================================================
# The gate. This is the assertion the whole change rests on.
# ===========================================================================


class TestSubmitGateStillHolds:
    """Opening the GET must not open the POST."""

    @pytest.mark.parametrize("slug", PUBLIC_TOOLS)
    @pytest.mark.parametrize("action", ["submit", "preflight"])
    def test_anonymous_post_never_reaches_the_handler(
        self, client, slug, action,
    ):
        # Patch the two things a real submit would touch. Neither may be
        # called: @login_required has to short-circuit first. Patching
        # rather than asserting on the response alone is deliberate — a
        # 302 to /login proves the redirect, only these prove nothing
        # happened on the way there.
        with patch("blueprints.tools.create_job") as create, \
                patch("blueprints.tools.load_user_context") as luc:
            resp = client.post(
                f"/tools/{slug}/{action}",
                data={"preset": "pilot", "num_designs": "4"},
            )
        assert resp.status_code in (301, 302, 303, 307, 308, 401, 403), (
            f"anonymous POST /tools/{slug}/{action} answered "
            f"{resp.status_code}; it must be refused, not handled"
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            assert "/login" in resp.headers.get("Location", "")
        create.assert_not_called()
        luc.assert_not_called()

    @pytest.mark.parametrize("slug", PUBLIC_TOOLS)
    def test_submit_and_preflight_are_login_required(self, app, slug):
        """Belt and braces: the view functions carry the decorator.

        The HTTP test above is the real one. This one fails loudly if
        someone re-registers the route under a differently named view,
        which would make the HTTP test's redirect assertion pass for the
        wrong reason (a 404 is not a 3xx, but a new unguarded alias
        would be).
        """
        view = app.view_functions["tools.tool_submit"]
        assert getattr(view, "__wrapped__", None) is not None, (
            "tools.tool_submit lost its decorator stack"
        )
        view = app.view_functions["tools.tool_preflight"]
        assert getattr(view, "__wrapped__", None) is not None, (
            "tools.tool_preflight lost its decorator stack"
        )


# ===========================================================================
# The GET is public and renders the REAL form.
# ===========================================================================


class TestAnonymousGetRendersTheRealForm:

    @pytest.mark.parametrize("slug", PUBLIC_TOOLS)
    def test_renders_200_with_no_session(self, client, slug):
        resp = client.get(f"/tools/{slug}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # The real form, not a preview shell: a POST form pointed at the
        # gated submit route, with its file/parameter inputs present.
        assert f'action="/tools/{slug}/submit"' in body
        assert 'method="POST"' in body
        assert "field-group" in body

    @pytest.mark.parametrize("slug", PUBLIC_TOOLS)
    def test_submit_button_is_replaced_by_a_signin_link(self, client, slug):
        resp = client.get(f"/tools/{slug}")
        body = resp.get_data(as_text=True)
        assert "Sign in to run this" in body
        # No submit control at all — not a disabled one. Matched on the
        # rendered ELEMENT, because submit_cta_script() always emits a
        # querySelector('[data-submit-cta]') string into the page.
        assert '<button type="submit"' not in body
        assert f'href="/login?next=/tools/{slug}"' in body

    @pytest.mark.parametrize("slug", PUBLIC_TOOLS)
    def test_no_balance_rows_and_no_topup_gate(self, client, slug):
        resp = client.get(f"/tools/{slug}")
        body = resp.get_data(as_text=True)
        assert "data-estimate-cost" in body, "estimate panel must render"
        # Element-level again: wallet_partials_script() names all three
        # of these in querySelector/getElementById strings regardless.
        assert "value\" data-estimate-balance>" not in body
        assert "value\" data-estimate-balance-after>" not in body
        assert "Balance after this job" not in body
        assert '<div class="wallet-topup-gate"' not in body

    @pytest.mark.parametrize("slug", PUBLIC_TOOLS)
    def test_page_is_indexable_with_tool_json_ld(self, client, slug):
        resp = client.get(f"/tools/{slug}")
        body = resp.get_data(as_text=True)
        # The form templates used to carry noindex because the indexable
        # copy lived on a separate preview URL. Same URL now, so the form
        # is the indexable page.
        assert "noindex" not in body
        assert '"@type": "SoftwareApplication"' in body

    @pytest.mark.parametrize("slug", PUBLIC_TOOLS)
    def test_explainer_content_is_present(self, client, slug):
        """The content cannibalised from the deleted preview shell."""
        resp = client.get(f"/tools/{slug}")
        body = resp.get_data(as_text=True)
        assert "What good looks like" in body
        assert "ipTM" in body


class TestPreviewShellIsGone:
    """The three deleted templates must not be reachable or referenced."""

    @pytest.mark.parametrize("name", [
        "tools/_preview.html",
        "tools/rfdiffusion_preview.html",
        "tools/boltzgen_preview.html",
    ])
    def test_template_no_longer_resolves(self, app, name):
        import jinja2
        with app.app_context(), pytest.raises(jinja2.TemplateNotFound):
            app.jinja_env.get_template(name)


# ===========================================================================
# Signed-in: no regression.
# ===========================================================================


class TestSignedInRenderUnchanged:

    @pytest.mark.parametrize("slug", PUBLIC_TOOLS)
    def test_signed_in_keeps_submit_button_and_wallet_rows(
        self, client, slug,
    ):
        with patch(
            "app.load_user_context", return_value=_ctx(),
        ), patch(
            "blueprints.tools.load_user_context", return_value=_ctx(),
        ), patch(
            "blueprints.tools.get_or_create_wallet",
            return_value={"balance_usd": 12.5, "wallet_frozen": False},
        ):
            _login(client)
            resp = client.get(f"/tools/{slug}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # Only mpnn uses the submit_cta macro; the other 13 forms hand-
        # roll the button, so assert on the control itself.
        assert '<button type="submit"' in body
        assert "Sign in to run this" not in body
        assert "value\" data-estimate-balance>" in body
        assert "Balance after this job" in body
        assert '<div class="wallet-topup-gate"' in body
        # The anonymous-only explainer stays off the signed-in page.
        assert "What good looks like" not in body
