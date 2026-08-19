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


def _hero_text(body: str) -> str:
    """Visible text of the page hero, tags stripped and entities resolved."""
    import html as _html

    block = re.search(r'<div class="hero">(.*?)</div>', body, re.S)
    assert block, "no hero block rendered"
    return re.sub(
        r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", block.group(1)))
    ).strip()


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


class TestRenderedLedeRules:
    """One test per stated rule, each named after the rule it holds."""

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
            phrase = _lede_phrase(hero).lower()
            head = hero.split(_LEDE_FRAME, 1)[0]
            short_name = head[:list(re.finditer(r"\bis an? ", head))[-1].start()]
            short_name = short_name.rsplit(". ", 1)[-1].strip().lower()
            assert short_name, slug
            if short_name in phrase:
                offenders[slug] = (short_name, phrase)
        assert not offenders, f"lede repeats the tool's own name: {offenders}"

    def test_lede_phrase_does_not_end_in_a_subordinate_clause(
        self, all_tools_app,
    ):
        """The builder's own first draft rendered "a target you upload you
        can run through" -- a second-person clause colliding with the
        frame's own "you can run through".

        Counted on the phrase AS RENDERED: the frame supplies the only
        "you" the sentence is allowed to contain, so any in the phrase is
        the collision.
        """
        offenders = {}
        for slug, hero in self._ledes(all_tools_app).items():
            phrase = _lede_phrase(hero)
            hits = re.findall(r"\byou\b|\byour\b", phrase, re.I)
            if hits:
                offenders[slug] = (hits, phrase)
        assert not offenders, (
            "seo_phrase carries a second-person clause into a frame that "
            "already ends '... you can run through': "
            f"{offenders}"
        )

    def test_lede_phrase_carries_no_relative_clause_marker(
        self, all_tools_app,
    ):
        """The same rule's non-pronoun half: a compact noun phrase
        completing "is a ..." has no relative clause hanging off it."""
        markers = (" that ", " which ", " where ", " when ", " so that ")
        offenders = {}
        for slug, hero in self._ledes(all_tools_app).items():
            phrase = f" {_lede_phrase(hero).lower()} "
            hit = [m.strip() for m in markers if m in phrase]
            if hit:
                offenders[slug] = (hit, phrase.strip())
        assert not offenders, f"seo_phrase opens a clause: {offenders}"

    def test_no_lede_phrase_leaks_a_raw_slug(self, all_tools_app):
        """The old fallback was ``f"free {slug} tool online"``, so every
        tool registered after the map was written rendered "a free
        esmfold2-design tool online" on an indexable page.

        Scoped to the PHRASE, not the whole hero: eleven of the fourteen
        slugs are the tool's display name lowercased, so "boltzgen" in
        the hero is the ``<h1>``, not a leak.
        """
        offenders = {
            slug: _lede_phrase(hero)
            for slug, hero in self._ledes(all_tools_app).items()
            if slug.lower() in _lede_phrase(hero).lower()
        }
        assert not offenders, (
            f"rendered lede phrase contains the raw slug: {offenders}"
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
            phrase = _lede_phrase(_hero_text(body))
            for where, text in (("title", title.group(1)), ("lede", phrase)):
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

    # WHITESPACE-INSENSITIVE ON PURPOSE. The first version of this guard
    # matched the literal "aim above roughly" against the raw body and
    # CERTIFIED FALSE: restoring the exact deleted sentence to
    # help/tool_guide.html left it green, because the template wraps the
    # phrase across two source lines ("aim above\n roughly 0.7") and the
    # substring never appears contiguously. Both of the instances this
    # PR removed were line-wrapped that way, so the guard would have
    # missed the very defect it was written for.
    STALE = re.compile(r"aim\s+above\s+roughly", re.I)

    def _pages(self, all_tools_app):
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        out = {}
        for path in ["/help/faq"] + [f"/help/tools/{s}" for s in slugs]:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} -> {resp.status_code}"
            out[path] = resp.get_data(as_text=True)
        for slug in slugs:
            resp = client.get(f"/tools/{slug}")
            assert resp.status_code == 200, f"{slug} -> {resp.status_code}"
            out[f"/tools/{slug}"] = resp.get_data(as_text=True)
        assert len(out) == 2 * len(slugs) + 1
        return out

    def test_the_sourceless_threshold_is_gone_everywhere(
        self, all_tools_app,
    ):
        offenders = {}
        for path, body in self._pages(all_tools_app).items():
            m = self.STALE.search(body)
            if m:
                offenders[path] = body[
                    max(0, m.start() - 80):m.end() + 80
                ]
        assert not offenders, (
            f"'{self.STALE.pattern}' is a threshold with no source: "
            f"{offenders}"
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
        data; the fix is ``~`` concatenation."""
        flask_app, _ = all_tools_app
        body = flask_app.test_client().get("/help/faq").get_data(as_text=True)
        blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.S
        )
        for raw in blocks:
            assert "{{" not in raw and "{%" not in raw, raw[:400]
