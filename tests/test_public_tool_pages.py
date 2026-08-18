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

import re
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


class TestExplainerRendersInBothAuthStates:
    """The score legend is reference material, not a marketing banner.

    It shipped logged-out-only, which deleted "aim above roughly 0.7"
    for the one person who most needs it — the user who just submitted
    a run and is looking at the number. Both states, deliberately; this
    test is the thing that stops it regressing to anonymous-only again.
    """

    @pytest.mark.parametrize("slug", PUBLIC_TOOLS)
    @pytest.mark.parametrize("signed_in", [False, True])
    def test_score_legend_and_faq_render(self, client, slug, signed_in):
        with patch(
            "app.load_user_context", return_value=_ctx(),
        ), patch(
            "blueprints.tools.load_user_context", return_value=_ctx(),
        ), patch(
            "blueprints.tools.get_or_create_wallet",
            return_value={"balance_usd": 12.5, "wallet_frozen": False},
        ):
            if signed_in:
                _login(client)
            resp = client.get(f"/tools/{slug}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "What good looks like" in body
        assert "aim above" in body
        assert "ipTM" in body
        # ...and the form is still the primary thing: the explainer sits
        # in the right rail, after the form's own action attribute.
        assert body.index(f'action="/tools/{slug}/submit"') < body.index(
            "What good looks like"
        ), "explainer must not be hoisted above the form"

    @pytest.mark.parametrize("signed_in", [False, True])
    def test_hero_lede_is_anonymous_only(self, client, signed_in):
        """``seo_phrase`` has to be RENDERED, not just computed.

        It was still being built in _public_tool_context and passed into
        the template context after the preview shell was deleted, with
        no template reading it — dead code and the loss of the page's
        keyword-bearing first paragraph in one move.
        """
        with patch(
            "app.load_user_context", return_value=_ctx(),
        ), patch(
            "blueprints.tools.load_user_context", return_value=_ctx(),
        ), patch(
            "blueprints.tools.get_or_create_wallet",
            return_value={"balance_usd": 12.5, "wallet_frozen": False},
        ):
            if signed_in:
                _login(client)
            resp = client.get("/tools/mpnn")
        body = resp.get_data(as_text=True)
        lede = "through tools.ranomics.com on a dedicated GPU"
        assert (lede in body) is (not signed_in)

    def test_anonymous_page_offers_account_creation(self, client):
        resp = client.get("/tools/mpnn")
        body = resp.get_data(as_text=True)
        assert "Sign in to run this" in body
        assert "Create a free account" in body
        assert 'href="/signup"' in body


# ===========================================================================
# Fix 1's invariant, generalised.
# ===========================================================================
#
# "No <button type=submit> on an anonymous page" was the invariant the
# original change verified, and it was true while a live primary "Start
# campaign" button sat next to "Sign in to run this": type="button" plus
# a click handler calling form.submit(). An anonymous visitor uploaded a
# PDB, typed a large design count, clicked it, and lost the upload to a
# login redirect.
#
# The invariant these helpers encode instead: NO CONTROL THAT CAN POST
# THE TOOL FORM, BY ANY MECHANISM, IS REACHABLE ANONYMOUSLY.

_TAG_RE = re.compile(r"<(button|input)\b[^>]*>", re.I)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.I | re.S)
_SUBMIT_CALL_RE = re.compile(r"\.\s*(?:submit|requestSubmit)\s*\(")
_INLINE_HANDLER_RE = re.compile(
    r"\bon[a-z]+\s*=\s*\"[^\"]*\.\s*(?:submit|requestSubmit)\s*\(", re.I,
)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag, re.I)
    return m.group(1) if m else None


def _tool_form_html(body: str, slug: str) -> str:
    """The <form> block(s) that POST to this tool, contents only."""
    return "\n".join(
        m.group(1)
        for m in re.finditer(r"<form\b(.*?)</form>", body, re.S | re.I)
        if f"/tools/{slug}/" in m.group(1)
    )


def _submitting_controls(body: str, slug: str) -> list[str]:
    """Every control in the tool form that could POST it. Empty is the pass.

    Three mechanisms, because the shipped bug used the third:
      1. ``type="submit"`` / ``type="image"``.
      2. A bare ``<button>`` — HTML defaults it to type=submit.
      3. A ``<button>`` of any type whose id is named inside an inline
         <script> that also calls ``.submit()`` / ``.requestSubmit()``.
         This is the one that caught the shipped bug.
    Plus inline ``on*="....submit()"`` handlers, checked separately.

    ponytail: (3) is scoped to <button> because the campaign script also
    names #num_designs and #target_pdb — the fields it READS — and those
    are not controls a user can press. A file/text input with a
    scripted change-handler submit would slip through; widen to those
    input types if one ever appears (none does today).
    """
    form_html = _tool_form_html(body, slug)
    submitting_scripts = [
        s for s in _SCRIPT_RE.findall(body) if _SUBMIT_CALL_RE.search(s)
    ]
    offenders: list[str] = []
    for m in _TAG_RE.finditer(form_html):
        tag, kind = m.group(0), m.group(1).lower()
        typ = (_attr(tag, "type") or "").lower()
        if typ in {"submit", "image"}:
            offenders.append(f"type={typ}: {tag}")
            continue
        if kind == "button" and not typ:
            offenders.append(f"bare <button> defaults to submit: {tag}")
            continue
        el_id = _attr(tag, "id")
        if kind != "button":
            continue
        if el_id and any(el_id in s for s in submitting_scripts):
            offenders.append(f"script-wired #{el_id}: {tag}")
    if _INLINE_HANDLER_RE.search(form_html):
        offenders.append("inline on*= handler calling submit()")
    return offenders


class TestSubmittingControlDetector:
    """The detector itself, against the markup that actually shipped.

    Without this the invariant test below could pass by being blind.
    """

    def test_catches_the_campaign_button_as_it_shipped(self):
        body = (
            '<form action="/tools/rfdiffusion/submit" method="POST">'
            '<input type="text" name="hotspot_residues">'
            '<a href="/login">Sign in to run this</a>'
            '<button type="button" class="btn-primary" '
            'id="campaign-submit-btn" hidden>Start campaign</button>'
            "</form>"
            "<script>var campaignBtn = "
            "document.getElementById('campaign-submit-btn');"
            "campaignBtn.addEventListener('click', function () "
            "{ form.submit(); });</script>"
        )
        assert _submitting_controls(body, "rfdiffusion") == [
            'script-wired #campaign-submit-btn: <button type="button" '
            'class="btn-primary" id="campaign-submit-btn" hidden>'
        ]

    def test_catches_a_bare_button(self):
        body = (
            '<form action="/tools/mpnn/submit"><button>Go</button></form>'
        )
        assert len(_submitting_controls(body, "mpnn")) == 1

    def test_clean_anonymous_form_has_no_offenders(self):
        body = (
            '<form action="/tools/mpnn/submit" method="POST">'
            '<input type="text" name="x">'
            '<a class="btn-primary" href="/login?next=/tools/mpnn">'
            "Sign in to run this</a></form>"
        )
        assert _submitting_controls(body, "mpnn") == []


@pytest.fixture
def all_tools_app(monkeypatch):
    """Every registered adapter flagged on, not a remembered subset."""
    import app as app_module  # noqa: PLC0415  (populates tools.base registry)
    from shared.feature_flags import flag_name  # noqa: PLC0415
    from tools import base as tool_base  # noqa: PLC0415

    slugs = sorted(a.slug for a in tool_base.all_adapters())
    assert len(slugs) >= 14, (
        f"adapter registry holds {len(slugs)} tools; a registry that did "
        "not populate would make every assertion below vacuous"
    )
    for slug in slugs:
        monkeypatch.setenv(flag_name(slug), "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    return flask_app, slugs


class TestNoAnonymouslyReachableSubmitControl:

    def test_every_tool_form(self, all_tools_app):
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        broken = {}
        for slug in slugs:
            resp = client.get(f"/tools/{slug}")
            assert resp.status_code == 200, f"{slug} -> {resp.status_code}"
            body = resp.get_data(as_text=True)
            assert _tool_form_html(body, slug), (
                f"{slug}: no <form> posting to /tools/{slug}/ — the "
                "invariant below would be vacuously true"
            )
            offenders = _submitting_controls(body, slug)
            if offenders:
                broken[slug] = offenders
        assert not broken, f"anonymously reachable submit controls: {broken}"

    def test_start_campaign_button_is_signed_in_only(self, all_tools_app):
        """Positive control: it must still exist for a signed-in user.

        Otherwise the test above passes because the campaign path was
        deleted rather than gated.
        """
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        seen = []
        with patch(
            "app.load_user_context", return_value=_ctx(),
        ), patch(
            "blueprints.tools.load_user_context", return_value=_ctx(),
        ), patch(
            "blueprints.tools.get_or_create_wallet",
            return_value={"balance_usd": 12.5, "wallet_frozen": False},
        ):
            _login(client)
            for slug in slugs:
                resp = client.get(f"/tools/{slug}")
                if 'id="campaign-submit-btn"' in resp.get_data(as_text=True):
                    seen.append(slug)
        assert set(seen) == {
            "bindcraft", "proteina", "pxdesign", "rfantibody", "rfdiffusion",
        }, seen


# ===========================================================================
# Fix 4: Epitope Scout is a prerequisite, not a sibling.
# ===========================================================================


class TestEpitopeScoutPrerequisite:
    """"Which residues?" is the one question a bench biologist cannot
    answer from the hotspot field's own help, which documents syntax only.
    """

    HOTSPOT_TOOLS = (
        "rfdiffusion", "bindcraft", "pxdesign", "rfantibody",
        "boltzgen", "proteina", "iggm",
    )

    def test_prerequisite_panel_on_every_hotspot_tool(self, all_tools_app):
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        with_panel = {
            slug for slug in slugs
            if "Start here first" in client.get(
                f"/tools/{slug}"
            ).get_data(as_text=True)
        }
        assert with_panel == set(self.HOTSPOT_TOOLS)

    def test_panel_links_to_scout_and_is_not_a_sibling_entry(
        self, all_tools_app,
    ):
        flask_app, _ = all_tools_app
        client = flask_app.test_client()
        body = client.get("/tools/rfdiffusion").get_data(as_text=True)
        assert "Open Epitope Scout" in body
        # Not filed under the sibling heading, which reads "if you are
        # picking between X and a sibling algorithm" — Scout is not an
        # alternative to RFdiffusion, it is the step before it.
        siblings = body.split("Related tools on Ranomics", 1)
        assert len(siblings) == 2
        assert "Epitope Scout" not in siblings[1].split("</aside>", 1)[0]

    def test_hotspot_field_carries_an_inline_scout_link(self, all_tools_app):
        """At the field, not only in the rail — that is where the
        question occurs."""
        flask_app, _ = all_tools_app
        client = flask_app.test_client()
        for slug in self.HOTSPOT_TOOLS:
            body = client.get(f"/tools/{slug}").get_data(as_text=True)
            if slug == "iggm":
                # iggm's Epitope residues field already shipped its own
                # link, to the public scout host.
                assert "scout.ranomics.com" in body
                continue
            field = body.index('id="hotspot_residues"')
            after = body[field:field + 3000]
            assert "Epitope Scout" in after, slug
            assert "Not sure which residues?" in after, slug
