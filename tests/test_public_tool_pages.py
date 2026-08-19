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

from markupsafe import escape as _escape

from shared import metric_glossary as _mg

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

    It shipped logged-out-only, which deleted the "what counts as a
    good ipTM" legend for the one person who most needs it — the user
    who just submitted a run and is looking at the number. Both states,
    deliberately; this test is the thing that stops it regressing to
    anonymous-only again.
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
        assert "ipTM" in body
        # The legend's ipTM cut-off must be the one in
        # shared/metric_glossary.py, not a number typed into the
        # template. The panel used to say "aim above roughly 0.7" while
        # a runtime card ~40 lines below on the SAME page carried a
        # tool's own "ipTM >= 0.65" — a page contradicting itself
        # inside one scroll. Asserting on the glossary string rather
        # than a literal is what makes that unrepeatable: hardcode a
        # threshold back into the template and this goes red.
        # ``escape`` because the band contains ">" and jinja
        # autoescapes it to "&gt;" on the way into the page.
        assert str(_escape(_mg.GLOSSARY["ipTM"]["good_range"])) in body
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


class TestPublicContextIsBuiltOncePerRequest:
    """One page render, one build — and one ``tool_jobs_p90`` SELECT.

    The bundle is needed twice per render: ``tool_form`` passes it into
    the template, and the ``tool_public_context`` jinja global rebuilds
    it inside ``components/about_panel.html``, because macros are
    imported without context and cannot see a context variable. Each
    build prices the pilot through ``estimated_cost_for_tool`` ->
    ``_historical_p90_seconds``, which is an UNCACHED Supabase SELECT.

    /tools/<slug> is publicly indexable now, so that was two network
    round trips per crawler hit, on fourteen pages.
    """

    # Uses the module's ``all_tools_app`` fixture rather than setting
    # FLAG_TOOL_* by hand: the hand-rolled version set fourteen env vars
    # plus SESSION_SECRET_KEY and never restored them, leaking flag state
    # into whatever test ran next.
    @staticmethod
    def _client(all_tools_app):
        flask_app, _slugs = all_tools_app
        return flask_app.test_client()

    def _counted(self, client, paths):
        from unittest.mock import patch

        import blueprints.tools as bt

        calls: list[str] = []
        real = bt._build_public_tool_context

        def counting(adapter):
            calls.append(adapter.slug)
            return real(adapter)

        with patch.object(bt, "_build_public_tool_context", counting):
            for path in paths:
                assert client.get(path).status_code == 200, path
        return calls

    def test_one_render_builds_it_once(self, all_tools_app):
        calls = self._counted(self._client(all_tools_app), ["/tools/mpnn"])
        assert calls == ["mpnn"], (
            f"built {len(calls)} times in one render: {calls}"
        )

    def test_the_cache_does_not_outlive_the_request(self, all_tools_app):
        """It memoises on ``flask.g``, so it must rebuild next request.

        A process-lifetime cache here would pin the first request's host
        into the ``_external=True`` breadcrumb URLs of every later
        response and freeze the derived price against p90 drift.
        """
        client = self._client(all_tools_app)
        calls = self._counted(client, ["/tools/mpnn", "/tools/mpnn"])
        assert calls == ["mpnn", "mpnn"], calls


# ===========================================================================
# Wave C copy pass: the rules the copy PR established, held by tests.
# ===========================================================================
#
# THE COPY PR STATED THREE RULES AND GUARDED NONE OF THEM. QC restored the
# original defect ("RFdiffusion is a RFdiffusion de novo binder design
# online...") and the full suite stayed green at 5262/20. It restored the
# raw-slug fallback and stayed green too. Everything below asserts on the
# RENDERED PAGE, never on the map literal in blueprints/tools.py: the
# failure mode is what the page says, and the map looked fine each time it
# shipped a broken sentence.
#
# The frame is templates/tools/_form_hero.html:
#
#     "{short_name} is a {seo_phrase} you can run through
#      tools.ranomics.com on a dedicated GPU. {seo_long}."
#
# It renders for ANONYMOUS visitors only (a signed-in user is past the
# pitch), so every test here drives the client with no session.

_LEDE_FRAME = " you can run through tools.ranomics.com on a dedicated GPU."


def _visible_text(markup: str) -> str:
    """What a reader sees: tags stripped, entities resolved, spaces collapsed.

    THE ONE PLACE THIS NORMALISATION IS DEFINED, because every guard in
    this file that matched raw markup instead has certified false at
    least once:

    * the first ipTM guard matched the literal ``"aim above roughly"``
      against ``resp.get_data()`` and stayed green on the exact sentence
      it was written to delete, because the template wrapped it across
      two source lines;
    * ``\\s+`` fixed the wrap and QC then defeated it again with
      ``aim above&nbsp;roughly`` — one entity, visibly identical on the
      page, invisible to the regex;
    * the first jargon sweep matched CSS class names and JSON-LD keys
      because it never stripped ``<script>``/``<style>``.

    Order is load-bearing: strip tags FIRST, then unescape. Unescaping
    first would turn a literal ``&lt;div&gt;`` in the copy into a tag
    the stripper then eats. ``<script>`` and ``<style>`` bodies are
    dropped whole rather than de-tagged, so JS string literals and CSS
    selectors cannot be read as page copy.

    ``\\s`` is Unicode-aware on ``str`` patterns, so the ``\\xa0`` that
    ``&nbsp;`` unescapes to collapses like any other space. That is what
    closes the entity hole for every caller at once.
    """
    import html as _html

    stripped = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", markup, flags=re.S | re.I
    )
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return re.sub(r"\s+", " ", _html.unescape(stripped)).strip()


_JSONLD_BLOCK = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.S
)


def _jsonld_text(markup: str) -> str:
    """The OTHER half of the page: every string inside every JSON-LD block.

    ``_visible_text`` drops ``<script>`` bodies whole, which is right for
    JS and CSS and catastrophic for structured data: it makes every
    content guard in this file blind to the copy Google actually indexes.
    QC planted the proteina reward-stack claim in ``/help/faq``'s
    ``faq_items`` — FAQPage JSON-LD only — and the full suite stayed
    green (M-P2).

    That page is the reason this is not a theoretical gap. ``faq.html``
    builds its FAQPage from a ``{% set faq_items = [...] %}`` list at the
    top and renders its VISIBLE answers from separate hardcoded ``<dd>``
    blocks ninety lines below: two independent copies of the same text,
    only one of which any guard could see.

    Values are extracted by PARSING, not by regex over the raw block, so
    ``\\u2014`` escapes and ``tojson``'s ``\\u003e`` come back as the
    characters a reader would see. A block that does not parse is
    returned raw rather than skipped — silently dropping it would be a
    hole of exactly the kind this function exists to close.
    ``TestEveryJsonLdBlockIsClean`` is what fails on the malformed JSON
    itself.
    """
    import json

    out: list[str] = []
    for raw in _JSONLD_BLOCK.findall(markup):
        try:
            doc = json.loads(raw)
        except ValueError:
            out.append(raw)
            continue
        stack = [doc]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, str):
                out.append(node)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _page_views(markup: str) -> dict[str, str]:
    """Both views of one page, keyed by the name a failure should print.

    Every content sweep runs over BOTH. A claim is just as false in a
    rich result as it is on the page.
    """
    return {"visible": _visible_text(markup), "json-ld": _jsonld_text(markup)}


#: Paths that must be in any honest enumeration of the public site. Not
#: the sweep list -- the FLOOR under it, so a discovery that silently
#: stops finding pages fails instead of passing over nothing.
_MUST_REACH = ("/", "/tools", "/help", "/help/faq", "/help/getting-started")

#: 14 tool pages + 14 guides + the five above + /pricing, /showcase,
#: /developability, /privacy, /terms, /login, /signup and the rest. The
#: measured set is 50; 40 leaves room for a flag change without leaving
#: room for the sweep to quietly collapse.
_MIN_PUBLIC_PATHS = 40


def _public_get_paths(flask_app, slugs) -> list[str]:
    """Every anonymously reachable 200 GET, discovered from the url_map.

    ENUMERATION IS THE BUG. ``test_no_rendered_page_claims_the_three_model
    _stack`` hardcoded 32 paths; QC planted the claim on ``/help`` -- a
    public page that renders the word "Proteina" and was not on the list
    -- and the suite stayed green (M-P3). Thirteen further public 200
    pages were outside it.

    Single-argument rules are expanded by SUBSTITUTING EACH REAL SLUG AND
    KEEPING WHAT ANSWERS 200, not by matching the argument's name: an
    earlier version keyed on ``{"slug"}`` and silently skipped
    ``/help/tools/<tool>``. A route wanting a job id simply does not
    answer 200 for "rfdiffusion" and drops out on its own.
    """
    client = flask_app.test_client()
    paths = set()
    for rule in flask_app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()):
            continue
        if not rule.arguments:
            candidates = [rule.rule]
        elif len(rule.arguments) == 1:
            arg = next(iter(rule.arguments))
            candidates = [rule.rule.replace(f"<{arg}>", s) for s in slugs]
        else:
            continue
        for candidate in candidates:
            if "<" in candidate:  # a converter like <int:id>
                continue
            try:
                if client.get(candidate).status_code == 200:
                    paths.add(candidate)
            except Exception:  # noqa: BLE001, PERF203
                continue
    # NON-VACUITY, asserted rather than assumed. Both halves matter: the
    # floor catches a discovery that collapsed to a handful, the named
    # paths catch one that stayed large while losing the pages the
    # claims actually live on.
    missing = [p for p in _MUST_REACH if p not in paths]
    assert not missing, (
        f"page discovery did not reach {missing}; every sweep built on "
        f"it would be blind there. Found {len(paths)}: {sorted(paths)}"
    )
    for slug in slugs:
        for path in (f"/tools/{slug}", f"/help/tools/{slug}"):
            assert path in paths, f"discovery missed {path}"
    assert len(paths) >= _MIN_PUBLIC_PATHS, (
        f"only {len(paths)} public GET paths discovered, expected at "
        f"least {_MIN_PUBLIC_PATHS}: {sorted(paths)}"
    )
    return sorted(paths)


def _marker_haystack(phrase: str) -> str:
    """Lowercased, space-padded, with every non-alphanumeric run as a space.

    QC's M-P5: ``CLAUSE_MARKERS`` already contains "which", and

        "a binder design tool&mdash;which labs run daily"

    walked past it, because membership was tested as ``f" {m} " in
    padded`` and an em dash leaves no space before the marker. The list
    was right; the matching was not. ``&mdash;`` is the house
    punctuation mark in these very strings, so this is the realistic
    next draft rather than an adversarial one.

    Normalising the INPUT rather than each marker fixes it for both
    marker classes at once and for any marker added later.
    """
    return f" {re.sub(r'[^0-9a-z]+', ' ', phrase.lower()).strip()} "


def _sentences(text: str) -> list[str]:
    """Split visible copy into sentences. Deliberately naive."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _hero_text(body: str) -> str:
    """Visible text of the page hero, tags stripped and entities resolved."""
    block = re.search(r'<div class="hero">(.*?)</div>', body, re.S)
    assert block, "no hero block rendered"
    return _visible_text(block.group(1))


def _blurb_for(slug: str) -> str:
    """The adapter's own blurb, read from the registry.

    Not a copy kept in this file: a fixture copy would drift out of the
    template it is asserted against, and then the guard passes because
    both sides changed rather than because the page is right.
    """
    import app as app_module  # noqa: F401,PLC0415  (populates the registry)
    from tools import base as tool_base  # noqa: PLC0415

    for adapter in tool_base.all_adapters():
        if adapter.slug == slug:
            return adapter.blurb or ""
    raise AssertionError(f"no adapter registered for slug {slug!r}")


def _lede_phrase(hero: str) -> str:
    """The ``seo_phrase`` as it actually reached the page.

    Sliced out of the rendered sentence rather than read from the map, so
    a template change that stops rendering it fails here too.

    The LAST "is a" before the frame, not the first: the same hero also
    carries the adapter blurb two paragraphs up, and several blurbs
    contain "is a" of their own.
    """
    head = hero.split(_LEDE_FRAME, 1)[0]
    starts = list(re.finditer(r"\bis an? ", head))
    assert starts, f"lede did not render the expected frame: {hero[:200]!r}"
    return head[starts[-1].end():].strip()


def _lede_long(hero: str) -> str:
    """``seo_long``: everything AFTER the frame. QC's M-E.

    Every rule in this class used to stop at ``_lede_phrase``, which is
    ``hero.split(_LEDE_FRAME, 1)[0]`` — the half BEFORE "you can run
    through …". QC violated all five rules in ``seo_long`` instead and
    the full suite stayed green while the page rendered

        "… Free rfdiffusion runs that you can start now — RFdiffusion is
         the rfdiffusion tool you want, which is free."

    That half is where round 1's real findings lived (pxdesign's three
    unglossed acronyms, af2's trailing "/jobs", proteina's false
    three-checks claim), so the guards covered the half that was never
    the problem.
    """
    parts = hero.split(_LEDE_FRAME, 1)
    assert len(parts) == 2, f"lede frame did not render: {hero[:200]!r}"
    return parts[1].strip()


class TestRenderedLedeRules:
    """One test per stated rule, each named after the rule it holds.

    WHICH RULES APPLY TO WHICH HALF, and why it is not "all five to
    both". The sentence renders as

        "{short_name} is a {seo_phrase} you can run through
         tools.ranomics.com on a dedicated GPU. {seo_long}."

    ``seo_phrase`` completes "is a …", so it is a NOUN PHRASE and the
    shape rules apply to it: no second person (the frame supplies the
    only "you"), no relative clause, no length that could only be a
    clause. ``seo_long`` is a free-standing sentence of prose, where
    "so you can weigh a mini-protein against a nanobody" (boltzgen) and
    "templates that make it accurate" (af2) are correct English. Running
    the shape rules over it would fail six of the fourteen tools on
    copy that is right — measured, not assumed.

    The three CONTENT rules — own name, raw slug, "free" — are true of
    both halves and all fourteen tools today, so those are the three
    extended. That is what catches M-E: its mutation violates all three.
    """

    @staticmethod
    def _ledes(all_tools_app):
        flask_app, slugs = all_tools_app
        # Not "at least 14": the two assertions the brief names, spelled
        # out, so a registry that half-populated or a flag left off cannot
        # make the loop below pass over three tools.
        assert len(slugs) == 14, f"expected 14 adapters, got {slugs}"
        client = flask_app.test_client()
        out = {}
        served = 0
        for slug in slugs:
            resp = client.get(f"/tools/{slug}")
            assert resp.status_code == 200, f"{slug} -> {resp.status_code}"
            served += 1
            hero = _hero_text(resp.get_data(as_text=True))
            assert _LEDE_FRAME in hero, (
                f"{slug}: the anonymous lede did not render, so every "
                "assertion about its wording would be vacuous"
            )
            out[slug] = hero
        assert served == 14, f"only {served} tool pages returned 200"
        return out

    def test_lede_phrase_never_repeats_the_tools_own_name(
        self, all_tools_app,
    ):
        """Defect A. It shipped as "RFdiffusion is a RFdiffusion ...".

        Only the page's OWN name: pxdesign's phrase legitimately names
        AlphaFold2, which is a different tool.
        """
        offenders = {}
        for slug, hero in self._ledes(all_tools_app).items():
            head = hero.split(_LEDE_FRAME, 1)[0]
            short_name = head[:list(re.finditer(r"\bis an? ", head))[-1].start()]
            short_name = short_name.rsplit(". ", 1)[-1].strip().lower()
            assert short_name, slug
            # BOTH HALVES (M-E). The mutation that stayed green put
            # "RFdiffusion is the rfdiffusion tool you want" in seo_long.
            for half, text in (
                ("seo_phrase", _lede_phrase(hero)),
                ("seo_long", _lede_long(hero)),
            ):
                if short_name in text.lower():
                    offenders[f"{slug}/{half}"] = (short_name, text)
        assert not offenders, f"lede repeats the tool's own name: {offenders}"

    # ── the subordinate-clause proxy, after QC walked through it ────────
    #
    # WHAT THE RULE ACTUALLY IS: ``seo_phrase`` completes "{name} is a
    # ___" and must stay a NOUN PHRASE. Any finite verb in it starts a
    # second clause, and the frame already ends "... you can run
    # through", so the result reads "a target you upload you can run
    # through" — the defect that shipped.
    #
    # WHY IT IS STILL A PROXY: deciding "is there a finite verb here"
    # properly needs a parser, and a parser is not worth a dependency to
    # police fourteen strings. So it is three cheap checks over closed
    # word classes, plus one structural check that does not depend on
    # vocabulary at all.
    #
    # THE FIRST VERSION WAS ONE CHECK — ``\byou\b|\byour\b`` — and QC
    # defeated it by swapping one word:
    #
    #   "…binder design tool ANYONE CAN RUN against a target uploaded
    #    from the bench"
    #
    # which renders round 1's defect verbatim in shape and stayed green.
    # A writer told "no second person in the phrase" reaches for exactly
    # that substitution, so this was the realistic next draft, not an
    # adversarial one.
    #
    # WHAT IT STILL CANNOT CATCH, stated plainly rather than left to be
    # rediscovered: a finite verb in the bare present tense with a
    # non-pronoun subject and no modal — "a binder design tool LABS RUN
    # daily". No word in it belongs to a closed class, so only the
    # length cap can see it, and a short enough one slips under. That is
    # the known ceiling; it is narrower than the hole QC found, and
    # closing it properly means a parser.

    #: Closed-class clause introducers. The first version stopped at
    #: five (that/which/where/when/so that), so whose, who, while,
    #: after, because and the rest all walked through.
    CLAUSE_MARKERS = (
        "that", "which", "where", "when", "who", "whom", "whose",
        "while", "after", "before", "once", "since", "because",
        "unless", "until", "although", "though", "if", "whether",
    )

    #: Modals and auxiliaries — the other closed class. "anyone CAN
    #: run", "a tool labs WILL use", "a target you HAVE uploaded". A
    #: noun phrase completing "is a ___" contains none of these.
    VERB_MARKERS = (
        "can", "could", "will", "would", "may", "might", "must",
        "shall", "should", "do", "does", "did", "is", "are", "was",
        "were", "has", "have", "had", "lets", "let",
    )

    #: The structural backstop, vocabulary-free. The fourteen real
    #: phrases run 5-7 words ("no-install online de novo binder design
    #: tool" is the longest at 7); M-D's is 18.
    #:
    #: WAS 12, AND 12 WAS THE ONLY THING BETWEEN THE PAGE AND M-P6:
    #:
    #:   "a binder design tool to run against a target uploaded from
    #:    the bench"
    #:
    #: which renders round 1's defect A almost verbatim ("...uploaded
    #: from the bench you can run through..."). ``to run`` is an
    #: infinitive, so it is in neither closed class, and the phrase is
    #: EXACTLY twelve words -- ``not > 12``. Eight keeps a full word of
    #: margin over the longest real phrase and puts the boundary well
    #: below the shortest clause anyone would write here.
    MAX_PHRASE_WORDS = 8

    def test_lede_phrase_does_not_end_in_a_subordinate_clause(
        self, all_tools_app,
    ):
        """Defect A's shape, and QC's M-D which evaded the first version.

        Second person, then two closed word classes, then a length cap
        that needs no vocabulary at all. Reported together because they
        are one rule: "this must still be a noun phrase".
        """
        offenders = {}
        for slug, hero in self._ledes(all_tools_app).items():
            phrase = _lede_phrase(hero)
            padded = _marker_haystack(phrase)
            reasons = []
            hits = re.findall(r"\byou\b|\byour\b|\byours\b", phrase, re.I)
            if hits:
                reasons.append(f"second person {hits}")
            verbs = [w for w in self.VERB_MARKERS if f" {w} " in padded]
            if verbs:
                reasons.append(f"finite-verb marker {verbs}")
            words = len(phrase.split())
            if words > self.MAX_PHRASE_WORDS:
                reasons.append(
                    f"{words} words > {self.MAX_PHRASE_WORDS}: too long to "
                    "be the noun phrase completing 'is a ...'"
                )
            if reasons:
                offenders[slug] = (reasons, phrase)
        assert not offenders, (
            "seo_phrase must stay a noun phrase completing '<name> is a "
            "...'; the frame already ends '... you can run through', so a "
            f"clause here collides with it: {offenders}"
        )

    def test_lede_phrase_carries_no_relative_clause_marker(
        self, all_tools_app,
    ):
        """The same rule's non-pronoun half, on the widened marker list.

        Kept as its own test so a failure names WHICH half broke.

        Matched through ``_marker_haystack``, which is where M-P5 lived:
        the list already contained "which" and an em dash defeated it.
        """
        offenders = {}
        for slug, hero in self._ledes(all_tools_app).items():
            phrase = _lede_phrase(hero)
            padded = _marker_haystack(phrase)
            hit = [m for m in self.CLAUSE_MARKERS if f" {m} " in padded]
            if hit:
                offenders[slug] = (hit, phrase)
        assert not offenders, f"seo_phrase opens a clause: {offenders}"

    def test_no_lede_phrase_leaks_a_raw_slug(self, all_tools_app):
        """The old fallback was ``f"free {slug} tool online"``, so every
        tool registered after the map was written rendered "a free
        esmfold2-design tool online" on an indexable page.

        Scoped to the PHRASE, not the whole hero: eleven of the fourteen
        slugs are the tool's display name lowercased, so "boltzgen" in
        the hero is the ``<h1>``, not a leak.
        """
        offenders = {}
        for slug, hero in self._ledes(all_tools_app).items():
            # BOTH HALVES (M-E). Scoped to phrase + long rather than to
            # the whole hero: eleven of the fourteen slugs are the tool's
            # display name lowercased, so "boltzgen" in the hero is the
            # <h1>, not a leak.
            for half, text in (
                ("seo_phrase", _lede_phrase(hero)),
                ("seo_long", _lede_long(hero)),
            ):
                if slug.lower() in text.lower():
                    offenders[f"{slug}/{half}"] = text
        assert not offenders, (
            f"rendered lede contains the raw slug: {offenders}"
        )

    def test_every_registered_tool_has_its_own_phrase(self, all_tools_app):
        """QC's M4, closed: it deleted the ``proteina`` key so the
        fallback fired, and the whole suite stayed green.

        The fallback is a safety net, not copy. It describes every tool
        on the hub and therefore none of them, and the map has already
        been four tools out of date once — which is how "a free
        esmfold2-design tool online" reached an indexable page. Read off
        the RENDERED phrase, so a tool that quietly falls through is
        caught on the page rather than in the dict.
        """
        import blueprints.tools as bt

        fallback = bt._preview_seo_phrases("__definitely-not-a-tool__")[0]
        fell_through = {
            slug: hero
            for slug, hero in self._ledes(all_tools_app).items()
            if _lede_phrase(hero) == fallback
        }
        assert not fell_through, (
            f"these tools render the generic fallback {fallback!r} "
            f"instead of their own phrase: {sorted(fell_through)}"
        )

    def test_the_fallback_itself_leaks_no_slug(self, all_tools_app):
        """Forces the unmapped path on all 14 and reads what renders.

        Without this the rule holds only while the map is complete, which
        is exactly the state that was true when the fallback was written
        and false four tools later. QC's M4 mutation, turned into a guard.
        """
        import blueprints.tools as bt

        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        with patch.dict(bt._PREVIEW_SEO_PHRASES, {}, clear=True):
            for slug in slugs:
                hero = _hero_text(
                    client.get(f"/tools/{slug}").get_data(as_text=True)
                )
                assert _LEDE_FRAME in hero, slug
                phrase = _lede_phrase(hero)
                assert slug.lower() not in phrase.lower(), (
                    f"fallback lede interpolates the slug: {phrase!r}"
                )

    def test_no_lede_or_title_advertises_the_run_as_free(
        self, all_tools_app,
    ):
        """Decision 1, applied to the lede AND the <title>.

        Reading the page is free; running is billed against the wallet.
        "Free Sequence Design" survived in ``_PREVIEW_TITLE_PHRASES`` --
        the most indexable string on the page, and the one a search
        result shows -- after the ledes had already dropped the word.
        """
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        offenders = {}
        for slug in slugs:
            body = client.get(f"/tools/{slug}").get_data(as_text=True)
            title = re.search(r"<title>(.*?)</title>", body, re.S)
            assert title, slug
            hero = _hero_text(body)
            # seo_long included (M-E): the mutation that stayed green put
            # "Free rfdiffusion runs …, which is free" in the second half.
            for where, text in (
                ("title", title.group(1)),
                ("seo_phrase", _lede_phrase(hero)),
                ("seo_long", _lede_long(hero)),
            ):
                if re.search(r"\bfree\b", text, re.I):
                    offenders[f"{slug}/{where}"] = text.strip()
        assert not offenders, (
            f"'free' on a page whose Submit button is billed: {offenders}"
        )


class TestCatalogLoopStepTwoCountIsDerived:
    """B1: /tools step 2 named a count the same page falsifies.

    It read "Five tools here do this" while the band one screen below it
    rendered eight. Asserted against the band AS RENDERED, so the two
    cannot disagree even when the catalog or the feature flags change.
    """

    DESIGN_BAND = "Make new binders for my target"

    @staticmethod
    def _band_slugs(body: str, band: str) -> list[str]:
        parts = body.split('<span class="catalog-section-title">')
        for chunk in parts[1:]:
            if chunk.split("</span>", 1)[0].strip() == band:
                return sorted(
                    set(re.findall(r'href="/tools/([a-z0-9-]+)"', chunk))
                )
        return []

    def test_step_two_count_equals_the_rendered_band(self, all_tools_app):
        flask_app, _ = all_tools_app
        body = flask_app.test_client().get("/tools").get_data(as_text=True)
        members = self._band_slugs(body, self.DESIGN_BAND)
        assert len(members) >= 2, (
            f"the design band rendered {members}; with fewer than two "
            "members the count assertion below would prove nothing"
        )
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
        assert f"{len(members)} tools below do this" in text, (
            f"step 2 does not name {len(members)}, the number of tools the "
            f"'{self.DESIGN_BAND}' band actually renders ({members}). "
            "Derive it from ``grouped``; do not type a literal."
        )

    def test_homepage_hero_count_equals_the_same_band(self, all_tools_app):
        """The homepage promises binders and then counts. Its first draft
        counted the whole registry ("Fourteen models sit behind that"),
        six of which predict structures or design sequences instead."""
        flask_app, _ = all_tools_app
        client = flask_app.test_client()
        home = client.get("/").get_data(as_text=True)
        members = self._band_slugs(
            client.get("/tools").get_data(as_text=True), self.DESIGN_BAND
        )
        assert len(members) >= 2, members
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", home))
        assert f"{len(members)} different design tools sit behind" in text, (
            f"the homepage hero does not name {len(members)}, the size of "
            f"the '{self.DESIGN_BAND}' band ({members})"
        )

    @pytest.mark.parametrize("path", ["/", "/tools"])
    def test_no_spelled_out_count_survives(self, all_tools_app, path):
        """The literals that shipped were words, not digits, so the two
        tests above would not have caught them on their own."""
        flask_app, _ = all_tools_app
        body = flask_app.test_client().get(path).get_data(as_text=True)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
        words = (
            "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
            "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
        )
        for word in words:
            for noun in ("tools", "models"):
                assert f"{word} {noun}" not in text, (
                    f"'{word} {noun}' is a literal count on {path}; it "
                    "goes stale the next time a tool is registered"
                )


class TestIptmThresholdHasOneSource:
    """B3: 0.7 was deleted from one template and left in two others.

    ``shared/metric_glossary.py`` is the source for the GENERAL band.
    Per-tool pass bars are not sourced from it and must not be --
    ``shared/score_legends.py`` puts rfdiffusion's ipTM "good" at 0.65,
    bindcraft's at 0.75 and boltz2's at 0.7, and each tool page states
    its own. What may not survive anywhere is the general "aim above
    roughly 0.7", which had no source at all and was still being emitted
    as FAQPage structured data after the PR claimed it was gone.
    """

    # MATCHED ON VISIBLE TEXT, NEVER ON MARKUP. This guard has now
    # certified false twice, both times on whitespace-shaped input that
    # a reader cannot see:
    #
    #   1. v1 matched the literal "aim above roughly" against the raw
    #      body. Restoring the exact deleted sentence to
    #      help/tool_guide.html left it GREEN, because the template
    #      wraps it across two source lines and the substring never
    #      appears contiguously.
    #   2. v2 relaxed the spaces to ``\s+``, which fixes a line wrap and
    #      nothing else. QC restored "aim above&nbsp;roughly 0.7", the
    #      page rendered it visibly on /help/tools/rfdiffusion, and the
    #      suite stayed green: ``&nbsp;`` is not ``\s`` in markup.
    #
    # Relaxing the pattern a third time would have been the third
    # version of the same mistake. The fix is on the INPUT side —
    # ``_visible_text`` unescapes entities and collapses everything
    # ``\s`` matches in Unicode (``\xa0`` included), so an entity, a
    # line wrap, a tag in the middle of the phrase and a thin space are
    # all the same string by the time the regex sees it.
    #
    # The pattern is also no longer pinned to the one word "aim": F1
    # found the identical defect four entries below the fixed one,
    # reading "well calibrated above roughly 0.4", and the "aim"-only
    # pattern missed it. It now matches the hedge itself. The guard that
    # does not depend on guessing the wording at all is
    # ``test_the_general_metric_legend_states_no_unsourced_number``
    # below; this one stays as the cheap named check for the exact
    # phrasing that shipped twice.
    STALE = re.compile(r"\babove\s+roughly\s+\d*\.?\d+", re.I)

    def _pages(self, all_tools_app):
        """Every public page, DISCOVERED. Was 29 hardcoded paths.

        The hardcoded list covered the tool pages and their guides and
        nothing else, so a stale threshold restored to ``/help`` or
        ``/pricing`` was outside it -- the same enumeration hole QC
        measured on the proteina sweep as M-P3.
        """
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        out = {}
        for path in _public_get_paths(flask_app, slugs):
            resp = client.get(path)
            if resp.status_code == 200:
                out[path] = resp.get_data(as_text=True)
        assert len(out) >= 2 * len(slugs) + 1, sorted(out)
        return out

    def test_the_sourceless_threshold_is_gone_everywhere(
        self, all_tools_app,
    ):
        """Both views. The threshold was still being emitted as FAQPage
        structured data after the PR claimed it was gone, and JSON-LD is
        exactly what ``_visible_text`` cannot see (M-P2)."""
        offenders = {}
        for path, body in self._pages(all_tools_app).items():
            for view, text in _page_views(body).items():
                m = self.STALE.search(text)
                if m:
                    offenders[f"{path} ({view})"] = text[
                        max(0, m.start() - 80):m.end() + 80
                    ]
        assert not offenders, (
            f"'{self.STALE.pattern}' is a threshold with no source: "
            f"{offenders}"
        )

    # The <aside> that renders the general "what good looks like" legend
    # on all 14 tool pages, identically. Anchored on its own panel title
    # rather than on a position, because there are four <dl>s on a tool
    # page and the other three are tool-specific.
    LEGEND_TITLE = "What good looks like"

    def _legend_entries(self, body: str) -> list[tuple[str, str]]:
        """The legend's ``<dt>``/``<dd>`` pairs, as visible text.

        Per ENTRY, because the metric a number describes is the whole
        point -- see the test below.
        """
        block = re.search(
            r'<aside class="panel about-panel">\s*<div class="panel-header">'
            r'\s*<span class="panel-title">\s*' + self.LEGEND_TITLE +
            r'\s*</span>(.*?)</aside>',
            body, re.S,
        )
        assert block, f"the '{self.LEGEND_TITLE}' panel did not render"
        pairs = re.findall(
            r"<dt>(.*?)</dt>\s*(?:<!--.*?-->\s*)*<dd>(.*?)</dd>",
            block.group(1), re.S,
        )
        entries = [(_visible_text(dt), _visible_text(dd)) for dt, dd in pairs]
        assert len(entries) >= 4, (
            f"the legend parsed into {len(entries)} dt/dd pairs; the "
            f"per-metric check below would barely be exercised: {entries}"
        )
        return entries

    #: What a THRESHOLD looks like when it is typed into a template. A
    #: decimal, or an integer percentage. Bare integers are exempt on
    #: purpose: "on a 0 to 1 scale" is a range description and a bare
    #: 0 or 1 cannot be a bar.
    #:
    #: THE PERCENTAGE IS QC's M-P4. "well calibrated above 40% on
    #: diverse folds" restored F1 in the one wording that evaded the
    #: phrase guard (which needs the word "roughly") AND the decimal
    #: scan (a percentage has no decimal point) at the same time. It
    #: rendered on all 14 tool pages with the suite green.
    THRESHOLD_NUM = re.compile(r"\d+\.\d+|\d+\s*%")

    @classmethod
    def _numbers(cls, text: str) -> set[str]:
        return {
            re.sub(r"\s+", "", n) for n in cls.THRESHOLD_NUM.findall(text)
        }

    @staticmethod
    def _glossary_keys_for(label: str) -> list[str]:
        """Which glossary entries a ``<dt>`` label is describing.

        Resolved by word-boundary match on the key or its label, so
        "ProteinMPNN recovery" finds ``recovery`` and "i_pAE and pAE"
        finds both. ``\\b`` is what keeps "pAE" out of "i_pAE" and "pTM"
        out of "ipTM": ``_`` and the letters either side are all word
        characters, so no boundary falls there.
        """
        keys = []
        for key, entry in _mg.GLOSSARY.items():
            for name in (key, str(entry.get("label", ""))):
                if name and re.search(
                    rf"\b{re.escape(name)}\b", label, re.I
                ):
                    keys.append(key)
                    break
        return keys

    def test_the_general_metric_legend_states_no_unsourced_number(
        self, all_tools_app,
    ):
        """The rule the phrase-matching guard above is only a proxy for.

        STRUCTURAL, so it does not care how the next threshold is
        worded. The "what good looks like" panel is GENERAL guidance —
        byte-identical on all 14 tool pages, so it cannot state a
        tool-specific bar — and every number in it must therefore come
        out of ``shared/metric_glossary.py``. Any other number is one
        typed into a template, which is what both defects were: "aim
        above roughly 0.7" (fixed by this PR) and "well calibrated above
        roughly 0.4" (F1, which the phrase guard missed by one word, and
        which was live on 14 indexable pages).

        VALIDATED AGAINST THE METRIC'S OWN ENTRY, NOT THE UNION, and it
        took QC's M-P9 to show why. The first version built ``sourced``
        from every ``good_range`` in the glossary at once, so

            pLDDT — ... treat anything above 0.75 as reliable.

        passed on all 14 pages: 0.75 is in the union because **ipTM**'s
        band contains it. pLDDT is a 0-100 scale in the same file
        ("> 80 very high confidence"), so that sentence is not merely
        unsourced, it is NONSENSE — and the guard written to be
        structural rather than phrase-guessing waved it through. A
        number is only sourced by the metric it is stated about.

        A ``<dt>`` naming no glossary metric at all fails too: an entry
        the glossary does not describe can state whatever it likes and
        this check would have nothing to compare it against.
        """
        assert any(
            self._numbers(str(e.get("good_range", "")))
            for e in _mg.GLOSSARY.values()
        ), "no glossary good_range states a number at all; vacuous"
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        offenders = {}
        for slug in slugs:
            body = client.get(f"/tools/{slug}").get_data(as_text=True)
            for label, definition in self._legend_entries(body):
                keys = self._glossary_keys_for(label)
                if not keys:
                    offenders[f"{slug} ~ {label}"] = (
                        "names no glossary metric, so any number in it is "
                        "unsourceable", definition[:200],
                    )
                    continue
                sourced = set()
                for key in keys:
                    sourced |= self._numbers(
                        str(_mg.GLOSSARY[key].get("good_range", ""))
                    )
                unsourced = sorted(self._numbers(definition) - sourced)
                if unsourced:
                    offenders[f"{slug} ~ {label}"] = (
                        f"{unsourced} not in {sorted(keys)}'s own "
                        f"good_range {sorted(sourced)}", definition[:200],
                    )
        assert not offenders, (
            "the general metric legend states numbers that its own "
            "metric's glossary good_range does not, so they are "
            f"thresholds typed into a template with no source: {offenders}"
        )

    def test_every_general_legend_reads_the_glossary(self, all_tools_app):
        """The three surfaces that state the general band must render the
        glossary's own string, not a number typed next to it."""
        band = str(_escape(_mg.GLOSSARY["ipTM"]["good_range"]))
        pages = self._pages(all_tools_app)
        for path in ("/help/faq", "/help/tools/rfdiffusion", "/tools/mpnn"):
            assert band in pages[path], (
                f"{path} does not render the glossary ipTM band {band!r}"
            )

    def test_the_faq_emits_the_glossary_band_as_structured_data(
        self, all_tools_app,
    ):
        """/help/faq publishes its answers as JSON-LD FAQPage, so the
        threshold is eligible for a Google rich result. ``tojson`` escapes
        ">", which is why this matches on the parsed object rather than
        on the raw page text."""
        import json

        flask_app, _ = all_tools_app
        body = flask_app.test_client().get("/help/faq").get_data(as_text=True)
        blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.S
        )
        answers = [
            q["acceptedAnswer"]["text"]
            for raw in blocks
            for q in json.loads(raw).get("mainEntity", [])
        ]
        scored = [a for a in answers if "ipTM" in a]
        assert scored, "no ipTM answer in the FAQPage structured data"
        for answer in scored:
            assert _mg.GLOSSARY["ipTM"]["good_range"] in answer, answer
            assert not self.STALE.search(answer), answer

    def test_the_faq_structured_data_carries_no_template_syntax(
        self, all_tools_app,
    ):
        """``{% set %}`` is one Jinja statement, so ``{{ ... }}`` inside
        its string literals is never interpolated. The signup-credit
        answer shipped the raw braces straight into Google's structured
        data; the fix is ``~`` concatenation.

        Kept as the named regression test for the block that actually
        shipped broken. ``TestEveryJsonLdBlockIsClean`` below is the one
        that generalises it — this test fetched only ``/help/faq``, so
        QC moved the identical defect into ``help/tool_guide.html`` and
        it shipped into ``SoftwareApplication`` structured data on all
        14 guide pages with the suite green (M-I).
        """
        flask_app, _ = all_tools_app
        body = flask_app.test_client().get("/help/faq").get_data(as_text=True)
        blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.S
        )
        for raw in blocks:
            assert "{{" not in raw and "{%" not in raw, raw[:400]


class TestProteinaScoringClaimIsConsistent:
    """The claim that has now been "removed everywhere" three times.

    "Every candidate is filtered through an AF2 / RF3 / force-field
    reward stack" is false: ``tools/proteina/Dockerfile.modal:229-231``
    says "Only ligand_binder (RF3 is its sole reward) and motif_ame need
    it; protein_binder scores on AF2 alone", and no variant runs all
    three. The sweep for it was declared complete at three surfaces,
    then at four, then a fifth was found in ``seo_faq[2]`` — rendering
    visibly AND inside FAQPage JSON-LD, which is why it is worth a test
    rather than another grep.

    Asserted on the RENDERED page across all 33 public pages, because
    each round of the sweep was done by grepping source and each time
    the source grep missed a copy that reached a reader.
    """

    #: A REGRESSION-LOCK ON FOUR STRINGS, NOT A CLAIM CHECK. Say so,
    #: because it has read as more than it is: these are the shapes the
    #: composite has actually shipped in, and QC paraphrased straight
    #: past all four (M-P1) with
    #:
    #:   "Each search shard filters EVERY candidate through all three
    #:    scoring models — an AlphaFold2 refold, a RoseTTAFold3 score
    #:    and a physics force field — before the hub ranks across
    #:    shards."
    #:
    #: which rendered visibly on /tools/proteina with the suite green.
    #: That is how a writer FIXES a flagged phrase: keep the meaning,
    #: change the words. The check that does not depend on guessing the
    #: wording is ``test_every_multi_model_sentence_states_the_mapping``
    #: below; this stays as the cheap named lock on the four literals.
    CLAIMS = (
        r"AF2\s*/\s*RF3",
        r"three independent scoring checks",
        r"filters candidates through",
        r"force[-\s]field reward stack",
    )

    #: The two scoring models the composite conflates, by every name
    #: the copy uses. ``RoseTTAFold-2`` is rfantibody's refolder and has
    #: nothing to do with this claim, so the ``3`` is anchored.
    MODEL_NAMES = {
        "AF2": r"\bAlphaFold\s?2\b|\bAF2\b",
        "RF3": r"\bRoseTTAFold[\s-]?3\b|\bRF3\b",
    }

    #: The target types the mapping is keyed on. A sentence that names
    #: both models and no target type is describing a stack.
    PROTEIN_TARGET = r"\bprotein\b"
    LIGAND_TARGET = r"\bligands?\b|\bsmall[-\s]molecule\b|\bmotifs?\b"

    # DISCLOSED CEILING, so it is not rediscovered as a finding. The
    # rule triggers on AF2 **and** RF3 together, which is the composite
    # this whole sweep exists for: one model per target type, conflated
    # into one stack. It deliberately does NOT trigger on a sentence
    # naming one model and the force field, and there is a live one:
    #
    #   tools/proteina/__init__.py:811, the protein_binder preset --
    #   "Search is scored by AlphaFold2 confidence plus a force-field
    #   reward" -- against Dockerfile.modal:229-231's "protein_binder
    #   scores on AF2 alone".
    #
    # That is PRE-EXISTING (commit c03aa1d, the tool's first commit),
    # untouched by this PR, and it is the same soft spot QC round 3
    # logged as §4c caveat 1: "with a physics force field added where it
    # applies" is a hedge the two "only" statements leave nowhere to
    # apply. Which is right -- the preset copy or the Dockerfile note --
    # is a question about what the container actually runs, not about
    # wording, so it is not settleable from this file and a guard that
    # forced one of the two answers would be asserting a fact nobody
    # here has checked. Resolve it at the source and widen this to
    # ``len(named) >= 2``; the code already reads as a set.
    def _claim_offenders(self, path: str, views: dict[str, str]) -> dict:
        """Both checks over one page's two views. Returns offenders."""
        found = {}
        for view, text in views.items():
            for pat in self.CLAIMS:
                m = re.search(pat, text, re.I)
                if m:
                    found[f"{path} ({view}) ~ {pat}"] = text[
                        max(0, m.start() - 100):m.end() + 140
                    ]
            for sentence in _sentences(text):
                named = [
                    fam for fam, pat in self.MODEL_NAMES.items()
                    if re.search(pat, sentence, re.I)
                ]
                if len(named) < len(self.MODEL_NAMES):
                    continue
                missing = [
                    t for t, pat in (
                        ("a protein target", self.PROTEIN_TARGET),
                        ("a ligand/motif target", self.LIGAND_TARGET),
                    ) if not re.search(pat, sentence, re.I)
                ]
                if missing:
                    found[f"{path} ({view}) ~ {named} without {missing}"] = (
                        sentence[:400]
                    )
        return found

    def test_no_rendered_page_claims_the_three_model_stack(
        self, all_tools_app,
    ):
        """Every public page, discovered, in both views.

        WAS 32 HARDCODED PATHS, and QC planted the claim on ``/help`` --
        public, 200, renders the word "Proteina", not on the list -- for
        a green suite (M-P3). Thirteen further public pages were outside
        it. It also read VISIBLE TEXT ONLY, so the same claim planted in
        ``/help/faq``'s ``faq_items`` shipped into the FAQPage JSON-LD
        Google indexes, invisible to every guard in this file (M-P2).
        Both are closed by discovering the pages and reading both views;
        neither needed a new claim pattern.
        """
        flask_app, slugs = all_tools_app
        assert len(slugs) == 14, f"expected 14 adapters, got {slugs}"
        client = flask_app.test_client()
        offenders = {}
        seen_proteina = 0
        for path in _public_get_paths(flask_app, slugs):
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} -> {resp.status_code}"
            views = _page_views(resp.get_data(as_text=True))
            if "Proteina" in views["visible"]:
                seen_proteina += 1
            offenders.update(self._claim_offenders(path, views))
        # Vacuity guard. Counted, not a bool: "Proteina" renders on /,
        # /tools and the guide too, so one hit could be a catalog card
        # while /tools/proteina renders nothing at all.
        assert seen_proteina >= 4, (
            f"only {seen_proteina} pages mentioned Proteina; the sweep "
            "would be describing pages that do not discuss it"
        )
        assert not offenders, (
            "a rendered page states the AF2 / RF3 / force-field reward "
            "stack. No variant runs all three, and it contradicts "
            "proteina's own output_summary ('The ligand and motif "
            f"variants score on RF3 only') on the same page: {offenders}"
        )

    def test_every_multi_model_sentence_states_the_mapping(self):
        """The claim check the four regexes above are not.

        WHY NOT A LONGER LIST. This claim has now escaped detection five
        times, each time in a new wording, and each round's answer was
        another literal. A list of false phrasings can only ever catch
        the drafts somebody already thought of.

        SO IT IS KEYED ON THE TRUTH INSTEAD. The truth is a MAPPING —
        ``tools/proteina/Dockerfile.modal:229-231``, "Only ligand_binder
        (RF3 is its sole reward) and motif_ame need it; protein_binder
        scores on AF2 alone" — and ``meta.py`` states the rule for
        writers in one line: *say which model scores which target, or
        say nothing*. So the invariant is that rule, asserted: a
        sentence naming two or more scoring models must also name both
        sides of the mapping. Every true sentence in this copy does,
        because that is what makes it true.

        WHAT THIS IS NOT. An earlier version of this docstring claimed
        the false composite "never can" name both sides, because a stack
        has no target types to name. That is wrong, and QC round 4
        produced four counterexamples that pass, one of which renders:

            "Whether the target is a protein or a small molecule, every
            Proteina candidate is put through AlphaFold2, RoseTTAFold3
            and a physics force field together."

        Naming both target types is in fact the natural way to write
        "whatever your target is". So this is a BAR-RAISER, not an
        invariant: it kills every wording that reached production so
        far, and it makes the surviving lie harder to write by accident
        — but a determined draft can still clear it. Do not read a green
        run here as proof the page is honest.

        Scoped to sentences naming TWO models on purpose. "/tools/af2
        runs AlphaFold2 on a dedicated GPU" names one and is fine; it is
        the conjunction that is the lie.

        This is the direct unit exercise of the matcher, so a change to
        it fails here rather than only in a page sweep that might have
        stopped reaching the page. The page sweep runs it for real.
        """
        # The mapping, in the four shapes this copy actually states it.
        # Read off the rendered page by QC, not invented here.
        true_copy = [
            "a protein target is scored by an AlphaFold2 refold, a "
            "small-molecule or motif target by RoseTTAFold3, with a "
            "physics force field added where it applies.",
            "Ranked designs with reward scores (AF2 pLDDT / ipTM for "
            "protein, RF3 score for ligand / motif, force-field energy "
            "where applicable).",
            "The protein-binder variant targets a protein PDB and is "
            "scored by AlphaFold2 confidence, while a small-molecule "
            "target is scored by RoseTTAFold3.",
            "Ranomics Tools runs AlphaFold2 on a dedicated GPU.",
        ]
        for sentence in true_copy:
            assert not self._claim_offenders("t", {"v": sentence}), sentence

        # POSITIVE CONTROLS. Every one of these is false, none shares a
        # wording with another, and none contains any of the four
        # CLAIMS literals except the first.
        false_copy = [
            # the string that shipped, four times
            "Each search shard filters candidates through an AF2 / RF3 "
            "/ force-field reward stack.",
            # QC's M-P1: the paraphrase that walked past all four
            "Each search shard filters every candidate through all "
            "three scoring models — an AlphaFold2 refold, a "
            "RoseTTAFold3 score and a physics force field — before the "
            "hub ranks across shards.",
            # no aggregation word at all, just two models conjoined
            "Every design is re-folded by AlphaFold2 and rescored by "
            "RoseTTAFold3 before it is ranked.",
            # the two-model version with the force field dropped
            "Candidates are scored by AlphaFold2 and RoseTTAFold3.",
        ]
        for sentence in false_copy:
            assert self._claim_offenders("t", {"v": sentence}), (
                f"the mapping check passed a composite claim: {sentence!r}"
            )

        # THE DISCLOSED CEILING, ASSERTED RATHER THAN DESCRIBED, so the
        # comment above cannot quietly stop being true. This is the live
        # protein_binder preset sentence: one model plus the force
        # field, no RF3, so the rule does not reach it. If somebody
        # widens the trigger, this line fails and points at the
        # disclosure that needs deleting.
        assert not self._claim_offenders("t", {"v": (
            "Search is scored by AlphaFold2 confidence plus a "
            "force-field reward."
        )}), "the AF2-plus-force-field ceiling is closed; update the note"

    def test_the_scoring_answer_says_which_model_scores_which_target(
        self, all_tools_app,
    ):
        """Positive control for the test above.

        Deleting the answer satisfies "states no false claim" exactly as
        well as fixing it does, and this is the answer Google indexes —
        so the true mapping has to be asserted present, not just the
        false one absent.

        SCOPED TO ONE QUESTION, and it took a mutation to learn why. The
        first version of this test asked "does ANY answer containing
        'scored' name both models" and checked two phrases against the
        whole page. Mutation M-L deleted the mapping from this very
        answer and the test stayed GREEN, because:

        * ``seo_faq[1]`` ("Can Proteina-Complexa design binders against a
          small molecule?") already names AlphaFold2 AND RoseTTAFold3, so
          it satisfied an any-answer check on its own; and
        * ``about["what_it_is"]`` carries the same two phrases verbatim —
          deliberately, so the page speaks in one voice — so a
          whole-page substring check passes off a different block.

        Two other strings were silently answering for the one under
        test. It now resolves the answer BY ITS QUESTION and requires
        the visible copy to carry that same answer, which is also the
        real invariant: structured data and page must not diverge.
        """
        import json

        flask_app, _ = all_tools_app
        body = flask_app.test_client().get(
            "/tools/proteina"
        ).get_data(as_text=True)
        qa = {
            q["name"]: q["acceptedAnswer"]["text"]
            for raw in re.findall(
                r'<script type="application/ld\+json">(.*?)</script>',
                body, re.S,
            )
            for q in json.loads(raw).get("mainEntity", [])
        }
        assert qa, "proteina renders no FAQPage structured data at all"
        keys = [q for q in qa if re.search(r"scored and ranked", q, re.I)]
        assert len(keys) == 1, (
            f"expected exactly one 'scored and ranked' question, got {keys}; "
            f"all questions: {sorted(qa)}"
        )
        answer = qa[keys[0]]
        missing = [
            m for m in ("AlphaFold2", "RoseTTAFold3") if m not in answer
        ]
        assert not missing, (
            f"proteina's scoring answer no longer says which model scores "
            f"which target (missing {missing}): {answer!r}"
        )
        assert _visible_text(body).count(answer) >= 1, (
            "the FAQPage answer is not in the visible copy, so the page and "
            f"the structured data Google reads disagree: {answer!r}"
        )


class TestEveryJsonLdBlockIsClean:
    """M-I: the same defect, anywhere it can happen, discovered not listed.

    The guard it replaces named one URL. Naming four instead would fail
    the same way the day a fifth template emits JSON-LD, so nothing here
    is enumerated:

    * the TEMPLATES that emit JSON-LD are discovered by reading
      ``templates/`` off disk;
    * the PAGES are discovered from the app's own url_map, through the
      shared ``_public_get_paths`` -- this class's private copy of that
      crawl was the only one in the file, which is how the proteina
      sweep next door was still hardcoding 32 paths (M-P3);
    * ``flask.template_rendered`` records which templates the crawl
      actually exercised, and the test FAILS if a discovered JSON-LD
      template was never rendered — that is the completeness argument,
      and without it "all clean" could just mean "never looked".
    """

    @staticmethod
    def _jsonld_templates() -> set[str]:
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "templates"
        assert root.is_dir(), root
        found = {
            p.relative_to(root).as_posix()
            for p in root.rglob("*.html")
            if "application/ld+json" in p.read_text(encoding="utf-8")
        }
        assert found, "no template emits JSON-LD; the crawl below is vacuous"
        return found

    @staticmethod
    def _with_ancestors(flask_app, rendered: set[str]) -> set[str]:
        """``rendered`` plus every template it extends/includes/imports.

        ``flask.template_rendered`` fires once per ``render_template``
        call and names only the top-level template, so ``base.html`` —
        which carries the Organization JSON-LD on EVERY page — never
        appeared and would have been reported as unreachable. Resolved
        with jinja's own parser rather than a regex over the source,
        because a regex over template source is how several guards in
        this repo have certified false.
        """
        from jinja2 import nodes as _nodes

        env = flask_app.jinja_env
        seen: set[str] = set()
        queue = list(rendered)
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            try:
                source = env.loader.get_source(env, name)[0]
                tree = env.parse(source, name=name)
            except Exception:  # noqa: BLE001  (a template we cannot load)
                continue
            for node in tree.find_all(
                (_nodes.Extends, _nodes.Include, _nodes.Import,
                 _nodes.FromImport)
            ):
                target = getattr(node, "template", None)
                if isinstance(target, _nodes.Const) and isinstance(
                    target.value, str
                ):
                    queue.append(target.value)
        return seen

    def test_no_page_ships_raw_template_syntax_in_json_ld(
        self, all_tools_app,
    ):
        import json

        from flask import template_rendered

        flask_app, slugs = all_tools_app
        assert len(slugs) == 14, f"expected 14 adapters, got {slugs}"
        expected = self._jsonld_templates()

        rendered: set[str] = set()

        def _record(sender, template, **extra):  # noqa: ARG001
            if template.name:
                rendered.add(template.name)

        client = flask_app.test_client()
        template_rendered.connect(_record, flask_app)
        try:
            blocks: dict[str, list[str]] = {}
            for path in _public_get_paths(flask_app, slugs):
                try:
                    resp = client.get(path)
                except Exception:  # noqa: BLE001  (a route needing real data)
                    continue
                if resp.status_code != 200:
                    continue
                body = resp.get_data(as_text=True)
                found = re.findall(
                    r'<script type="application/ld\+json">(.*?)</script>',
                    body, re.S,
                )
                if found:
                    blocks[path] = found
        finally:
            template_rendered.disconnect(_record, flask_app)

        # COMPLETENESS FIRST. If the crawl never rendered a JSON-LD
        # template, a clean result below says nothing about it.
        covered = self._with_ancestors(flask_app, rendered)
        missed = sorted(expected - covered)
        assert not missed, (
            f"these templates emit JSON-LD but the crawl never rendered "
            f"them, so they are unchecked: {missed}. Covered: "
            f"{sorted(covered & expected)}"
        )
        assert len(blocks) >= 14, (
            f"only {len(blocks)} pages carried a JSON-LD block; the "
            "assertions below would barely be exercised"
        )

        offenders = {}
        for path, found in blocks.items():
            for raw in found:
                if "{{" in raw or "{%" in raw:
                    offenders[f"{path} (template syntax)"] = raw[:300]
                    continue
                try:
                    json.loads(raw)
                except ValueError as exc:
                    offenders[f"{path} (invalid JSON)"] = f"{exc}: {raw[:300]}"
        assert not offenders, (
            "raw Jinja, or unparseable JSON, in structured data Google "
            f"indexes: {offenders}"
        )


# ===========================================================================
# The hero carries ONE intro, not two.
# ===========================================================================


class TestHeroCarriesOneIntroNotTwo:
    """``blurb`` and the SEO lede are alternatives, never a stack.

    Rendering both put the tool's own name in front of an anonymous
    reader three times inside three lines -- once in the ``<h1>``, again
    inside the blurb, and a third time opening the lede -- against a
    house rule that asks for no model name in a first sentence at all.

    Both directions are asserted because the obvious wrong fix passes
    half of it: deleting the blurb from the template outright would
    satisfy the anonymous case and quietly strip every signed-in page of
    its subtitle. The signed-in test is that positive control, and it is
    the reason this is two tests rather than one.
    """

    def test_anonymous_hero_shows_the_lede_and_not_the_blurb(
        self, all_tools_app,
    ):
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        offenders = {}
        for slug in slugs:
            resp = client.get(f"/tools/{slug}")
            assert resp.status_code == 200, f"{slug} -> {resp.status_code}"
            hero = _hero_text(resp.get_data(as_text=True))
            blurb = _visible_text(_blurb_for(slug))
            if _LEDE_FRAME not in hero:
                offenders[slug] = "lede missing from anonymous hero"
            elif blurb and blurb in hero:
                offenders[slug] = "blurb still stacked above the lede"
        assert not offenders, (
            f"anonymous hero is not carrying exactly one intro: {offenders}"
        )

    def test_signed_in_hero_still_shows_the_blurb(self, all_tools_app):
        """Positive control -- the subtitle survives for everyone else.

        Without this, dropping ``adapter.blurb`` from the template
        altogether passes the test above.
        """
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        offenders = {}
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
                assert resp.status_code == 200, f"{slug} -> {resp.status_code}"
                hero = _hero_text(resp.get_data(as_text=True))
                blurb = _visible_text(_blurb_for(slug))
                if blurb and blurb not in hero:
                    offenders[slug] = "signed-in hero lost its subtitle"
                elif _LEDE_FRAME in hero:
                    offenders[slug] = "anonymous SEO lede leaked to a session"
        assert not offenders, (
            f"signed-in hero is wrong: {offenders}"
        )
