"""``PILOT`` — the guided starter recipe, and the two ways it can lie.

The recipe lives in ``tools/<slug>/meta.py`` and is rendered by
``templates/components/pilot_card.html``. Two failure modes are worth a
test, and neither is visible by reading the diff:

1. **A parameter the form does not honour.** ``PILOT["params"]`` keys
   are form field names. A key no field reads through
   ``pre_value()``/``pre_checked()`` makes ``?pilot=1`` a link that
   appears to load settings and silently loads nothing. Two forms
   (rfdiffusion, proteina) hard-coded ``value="4"``/``value="8"`` on
   the design count and ignored ``pre_fill`` entirely, so this is not
   hypothetical.

2. **A second rate card.** The whole point of deriving the price from
   ``estimated_cost_for_tool`` is that a hand-written price in meta.py
   drifts off the real one. The test below fails if a price or a
   runtime string is ever typed into a PILOT dict.
"""

from __future__ import annotations

import re

import pytest

from shared.tool_meta import meta_for

pytestmark = pytest.mark.usefixtures("isolate_supabase")

PARAM_KEYS = ("label", "goal", "you_need", "params", "next_step")


@pytest.fixture(scope="module")
def tools_app():
    """Every registered adapter, flagged on, rendered anonymously."""
    import os

    import app as app_module
    from shared.feature_flags import flag_name
    from tools import base as tool_base

    slugs = sorted(a.slug for a in tool_base.all_adapters())
    # An empty registry would make every assertion below vacuous —
    # tools.base._REGISTRY is only populated as a side effect of the
    # ``import app`` above.
    assert len(slugs) >= 14, f"adapter registry holds {len(slugs)} tools"
    prior = {}
    for slug in slugs:
        prior[flag_name(slug)] = os.environ.get(flag_name(slug))
        os.environ[flag_name(slug)] = "on"
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    yield flask_app, slugs
    for key, val in prior.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _pilots(slugs):
    return {s: getattr(meta_for(s), "PILOT", None) for s in slugs}


def _estimate(slug: str, params: dict):
    from shared.wallet_estimates import estimated_cost_for_tool

    return estimated_cost_for_tool(
        None, slug, {k: v for k, v in params.items() if v is not None},
    )


def _posted_value(html: str, name: str) -> str | None:
    """What the browser would POST for ``name``, read off the markup.

    Covers the three controls the 14 forms use: a text/number input, a
    checked radio, and a selected <option>. Returns None when the field
    is absent or renders no value at all.
    """
    for tag in re.findall(r"<input\b[^>]*>", html):
        if re.search(rf'\bname="{re.escape(name)}"', tag) is None:
            continue
        kind = (re.search(r'\btype="([^"]*)"', tag) or [None, ""])[1] \
            if re.search(r'\btype="([^"]*)"', tag) else ""
        if kind in {"radio", "checkbox"} and "checked" not in tag:
            continue
        val = re.search(r'\bvalue="([^"]*)"', tag)
        if val:
            return val.group(1)
    sel = re.search(
        rf'<select\b[^>]*\bname="{re.escape(name)}"[^>]*>(.*?)</select>',
        html, re.S,
    )
    if sel:
        for opt in re.findall(r"<option\b[^>]*>", sel.group(1)):
            if "selected" in opt:
                v = re.search(r'\bvalue="([^"]*)"', opt)
                return v.group(1) if v else None
    return None


class TestPilotDeclaration:

    def test_every_tool_declares_pilot_explicitly(self, tools_app):
        """None is a decision; a missing attribute is an oversight."""
        _, slugs = tools_app
        missing = [
            s for s in slugs
            if not hasattr(meta_for(s), "PILOT")
        ]
        assert not missing, f"meta.py declares no PILOT for: {missing}"

    def test_at_least_one_tool_actually_has_one(self, tools_app):
        """Otherwise every assertion below passes over an empty set."""
        _, slugs = tools_app
        assert sum(1 for p in _pilots(slugs).values() if p) >= 5

    def test_shape(self, tools_app):
        _, slugs = tools_app
        for slug, pilot in _pilots(slugs).items():
            if not pilot:
                continue
            assert set(pilot) == set(PARAM_KEYS), slug
            assert pilot["params"], f"{slug}: a pilot with no parameters"

    def test_no_hand_written_price_or_runtime(self, tools_app):
        """The whole point is that both are derived. See the module docstring."""
        _, slugs = tools_app
        bad = []
        for slug, pilot in _pilots(slugs).items():
            if not pilot:
                continue
            for key in ("label", "goal", "you_need", "next_step"):
                text = pilot[key]
                if re.search(r"\$\s?\d", text):
                    bad.append(f"{slug}.{key} quotes a price: {text!r}")
                if re.search(r"\b\d+\s*(min|minutes|hours|hrs)\b", text):
                    bad.append(f"{slug}.{key} quotes a runtime: {text!r}")
        assert not bad, bad


class TestPilotPrefillActuallyLands:

    def test_every_param_reaches_the_form(self, tools_app):
        flask_app, slugs = tools_app
        client = flask_app.test_client()
        broken = []
        for slug, pilot in _pilots(slugs).items():
            if not pilot:
                continue
            resp = client.get(f"/tools/{slug}?pilot=1")
            assert resp.status_code == 200, f"{slug} -> {resp.status_code}"
            html = resp.get_data(as_text=True)
            for key, want in pilot["params"].items():
                got = _posted_value(html, key)
                if got != want:
                    broken.append(
                        f"{slug}: ?pilot=1 sets {key}={want!r} but the form "
                        f"renders {got!r}"
                    )
        assert not broken, broken

    def test_without_the_param_the_form_is_untouched(self, tools_app):
        """A pilot value that is also the default proves nothing.

        At least one tool must render differently with and without
        ``?pilot=1``, or the test above could pass on a link that does
        nothing at all.
        """
        flask_app, slugs = tools_app
        client = flask_app.test_client()
        differs = [
            slug for slug, pilot in _pilots(slugs).items() if pilot
            and client.get(f"/tools/{slug}").get_data(as_text=True)
            != client.get(f"/tools/{slug}?pilot=1").get_data(as_text=True)
        ]
        assert differs, "?pilot=1 changed no tool form at all"


class TestPilotCardPriceIsDerived:

    def test_card_price_equals_the_estimator(self, tools_app):
        """The card and /api/wallet/estimate must not disagree.

        Both go through ``estimated_cost_for_tool`` over the same
        params — the card server-side, the form's JS over the values
        those params rendered into the fields.
        """
        from shared.compute_campaigns import display_cost_usd
        from shared.wallet_estimates import estimated_cost_for_tool

        flask_app, slugs = tools_app
        client = flask_app.test_client()
        for slug, pilot in _pilots(slugs).items():
            if not pilot:
                continue
            html = client.get(f"/tools/{slug}?pilot=1").get_data(as_text=True)
            shown = re.search(r"About <strong>\$([0-9.]+)</strong>", html)
            assert shown, f"{slug}: pilot card rendered no price"
            expected = display_cost_usd(
                estimated_cost_for_tool(None, slug, pilot["params"]),
            )
            assert shown.group(1) == str(expected), slug

    def test_no_card_where_there_is_no_pilot(self, tools_app):
        flask_app, slugs = tools_app
        client = flask_app.test_client()
        for slug, pilot in _pilots(slugs).items():
            if pilot:
                continue
            html = client.get(f"/tools/{slug}").get_data(as_text=True)
            assert "Load these settings" not in html, slug


class TestPilotDoesNotOutrankARealJob:

    def test_clone_from_wins(self, tools_app):
        """?pilot=1 is the fallback, never an override.

        A user who arrives with both params is re-running a real job;
        a generic starter recipe must not quietly replace its inputs.
        """
        from types import SimpleNamespace
        from unittest.mock import patch

        flask_app, _ = tools_app
        client = flask_app.test_client()
        prior = SimpleNamespace(
            id="job-1234abcd", tool="rfdiffusion", status="succeeded",
            inputs={"num_designs": "250", "target_chain": "B"},
        )
        ctx = SimpleNamespace(
            user_id="u-1", tier="free", balance=100, email="u@example.com",
        )
        with client.session_transaction() as sess:
            sess["user_email"] = "u@example.com"
        with patch("blueprints.tools.load_user_context", return_value=ctx), \
                patch("blueprints.tools.get_job", return_value=prior), \
                patch(
                    "blueprints.tools.get_or_create_wallet",
                    return_value={"balance_usd": 50, "wallet_frozen": False},
                ):
            html = client.get(
                "/tools/rfdiffusion?pilot=1&clone_from=job-1234abcd",
            ).get_data(as_text=True)
        assert _posted_value(html, "num_designs") == "250"
        assert _posted_value(html, "target_chain") == "B"


def _pilot_card_html(client, slug: str) -> str:
    """Just the pilot card's <aside>, isolated from the rest of the page."""
    html = client.get(f"/tools/{slug}").get_data(as_text=True)
    start = html.find("<h3>What you need</h3>")
    assert start != -1, f"{slug}: no pilot card rendered"
    open_tag = html.rfind("<aside", 0, start)
    close = html.find("</aside>", start)
    assert open_tag != -1 and close != -1, slug
    return html[open_tag:close]


class TestPilotCardRendersMarkupNotEntities:
    """The card's prose is author-written and contains markup on purpose.

    ``goal``, ``you_need`` and ``next_step`` hold ``&mdash;`` on four
    tools and a ``<code>`` span on proteina. Rendered without ``|safe``
    — as they shipped — a visitor reads a literal ``&amp;mdash;`` and a
    literal ``<code>A1-150</code>`` on a page that is now publicly
    indexable. Every sibling explainer macro already marks the same
    class of prose safe; this asserts the card matches.

    Only the three prose fields get the filter. ``url`` is an href, and
    ``cost_usd`` and ``label`` have no reason to carry markup, so a
    future field reading user input must not join that list — see the
    header comment in components/pilot_card.html.
    """

    def test_no_double_escaped_entity_reaches_the_page(self, tools_app):
        flask_app, slugs = tools_app
        client = flask_app.test_client()
        bad = []
        checked = 0
        for slug, pilot in _pilots(slugs).items():
            if not pilot:
                continue
            checked += 1
            card = _pilot_card_html(client, slug)
            # An escaped ampersand-entity anywhere in the card body is
            # text the visitor reads as source code.
            for m in re.findall(r"&amp;[a-zA-Z]+;", card):
                bad.append(f"{slug}: card shows a literal {m}")
        assert checked >= 5, f"only {checked} pilot cards rendered"
        assert not bad, bad

    def test_markup_in_the_prose_renders_as_markup(self, tools_app):
        """proteina's ``<code>A1-150</code>`` must be a tag, not text."""
        flask_app, _ = tools_app
        card = _pilot_card_html(flask_app.test_client(), "proteina")
        assert "&lt;code&gt;" not in card, \
            "proteina's pilot card shows a literal <code> tag"
        assert "<code>A1-150</code>" in card

    def test_at_least_one_pilot_actually_carries_markup(self, tools_app):
        """Otherwise both assertions above pass over plain text forever."""
        _, slugs = tools_app
        carriers = [
            slug for slug, pilot in _pilots(slugs).items() if pilot
            and any(
                "&" in pilot[k] or "<" in pilot[k]
                for k in ("goal", "you_need", "next_step")
            )
        ]
        assert carriers, "no PILOT prose contains markup at all"


class TestNoPilotIsANoOp:
    """"Load these settings" must not promise a change it does not make.

    Six of the ten pilots shipped with params identical to the form's
    own defaults, so the button loaded settings that were already
    loaded. Two of those (bindcraft, rfantibody) had a genuinely cheaper
    first run available and were retuned to it; proteina gained an
    explicit binder-length window.

    The remaining three (boltzgen, esmfold2-design, iggm) are MEASURED
    to have no cheaper configuration reachable from their form — the
    default already is the tool's floor — so this asserts the honest
    rule rather than an allow-list.

    THE RULE, stated as what is actually computed: if a pilot costs the
    same as the form's own defaults, then driving the scaling parameter
    to its minimum must not make it cheaper. A pilot that bills exactly
    what the default bills, on a tool where a smaller run is reachable,
    fails.

    Keyed on PRICE and not on "do the params differ from the defaults".
    That earlier shape was defeated with a decoy: it skipped any pilot
    that differed from the defaults in ANY key, so adding one
    price-irrelevant key — pxdesign's ``binder_length``, which the
    estimator does not read — exempted the tool from the check entirely
    while the pilot still billed the full default price.
    ``test_a_cosmetic_param_cannot_buy_the_exemption`` is that decoy,
    kept as a control.
    """

    @staticmethod
    def _form_defaults(client, slug, keys):
        html = client.get(f"/tools/{slug}").get_data(as_text=True)
        return {k: _posted_value(html, k) for k in keys}

    @classmethod
    def _no_op_violation(cls, client, slug, params) -> str | None:
        """The rule itself, so the sweep and the control share one body."""
        from shared.wallet_estimates import (
            estimated_cost_for_tool,
            get_tool_spec,
        )

        defaults = cls._form_defaults(client, slug, params)
        here = estimated_cost_for_tool(None, slug, params)
        as_shipped = estimated_cost_for_tool(
            None, slug, {k: v for k, v in defaults.items() if v is not None},
        )
        if here != as_shipped:
            return None  # the pilot moves the bill; it is not a no-op

        spec = get_tool_spec(slug)
        floor = estimated_cost_for_tool(
            None, slug,
            dict(params, **({spec.scaling_param: "1"}
                            if spec and spec.scaling_param else {})),
        )
        if floor < here:
            return (
                f"{slug}: the pilot bills exactly what the form's own "
                f"defaults bill (${here}), but a cheaper run is reachable "
                f"(${floor}) — retune the pilot instead of restating the "
                f"default"
            )
        return None

    def test_a_pilot_is_never_more_expensive_than_the_defaults(
        self, tools_app
    ):
        from shared.wallet_estimates import estimated_cost_for_tool

        flask_app, slugs = tools_app
        client = flask_app.test_client()
        bad = []
        for slug, pilot in _pilots(slugs).items():
            if not pilot:
                continue
            params = pilot["params"]
            defaults = self._form_defaults(client, slug, params)
            pilot_cost = estimated_cost_for_tool(None, slug, params)
            default_cost = estimated_cost_for_tool(
                None, slug,
                {k: v for k, v in defaults.items() if v is not None},
            )
            if pilot_cost > default_cost:
                bad.append(
                    f"{slug}: the pilot costs ${pilot_cost} but the form's "
                    f"own defaults cost ${default_cost} — a 'starter' run "
                    f"must not be the expensive one"
                )
        assert not bad, bad

    def test_a_no_op_pilot_is_only_allowed_when_nothing_cheaper_exists(
        self, tools_app
    ):
        flask_app, slugs = tools_app
        client = flask_app.test_client()
        pilots = {s: p for s, p in _pilots(slugs).items() if p}
        # Five of the ten currently bill exactly what their defaults
        # bill. If that ever reaches zero the sweep still passes but has
        # stopped exercising the floor branch, which is the whole rule.
        priced_like_the_default = [
            slug for slug, pilot in pilots.items()
            if self._form_defaults(client, slug, pilot["params"])
            and _estimate(slug, pilot["params"])
            == _estimate(slug, self._form_defaults(
                client, slug, pilot["params"]))
        ]
        assert priced_like_the_default, (
            "no pilot bills what its form defaults bill, so the floor "
            "check below never runs"
        )
        bad = [
            msg for slug, pilot in pilots.items()
            if (msg := self._no_op_violation(client, slug, pilot["params"]))
        ]
        assert not bad, bad

    def test_a_cosmetic_param_cannot_buy_the_exemption(self, tools_app):
        """The control. A price-irrelevant key must not silence the rule.

        pxdesign is the tool a no-op pilot would actually hurt: a
        smaller run IS reachable (num_designs down to 1 halves the
        bill), so a pilot restating its defaults is exactly what the
        rule exists to catch. ``binder_length`` is a real, rendered,
        submitted field that the estimator does not read — the decoy the
        previous shape of this test was defeated with.
        """
        flask_app, _ = tools_app
        client = flask_app.test_client()
        keys = ("preset", "num_designs", "binder_length")
        defaults = self._form_defaults(client, "pxdesign", keys)
        assert all(v is not None for v in defaults.values()), defaults

        # A hypothetical no-op pilot. The rule must flag it.
        assert self._no_op_violation(client, "pxdesign", dict(defaults)), (
            "a pilot restating pxdesign's defaults was NOT flagged, even "
            "though num_designs=1 costs half — the rule is not enforcing "
            "what its docstring claims"
        )

        # The same pilot plus one key that moves the markup and nothing
        # else. It must still be flagged.
        decoy = dict(defaults, binder_length=str(
            int(defaults["binder_length"]) + 1))
        assert _estimate("pxdesign", decoy) == _estimate("pxdesign", defaults), (
            "binder_length now moves the estimate; pick another "
            "price-irrelevant field for this control"
        )
        assert decoy != defaults
        assert self._no_op_violation(client, "pxdesign", decoy), (
            "one price-irrelevant key exempted pxdesign from the no-op "
            "rule — the decoy that defeated the earlier shape of this "
            "test still works"
        )

    def test_the_retuned_pilots_really_are_cheaper(self, tools_app):
        """Pins the two retuned by hand, so a revert is loud.

        These are the only two tools whose form exposes a knob that both
        shrinks the run and lowers the bill, and both shipped restating
        the default.
        """
        from shared.wallet_estimates import estimated_cost_for_tool

        flask_app, slugs = tools_app
        client = flask_app.test_client()
        for slug in ("bindcraft", "rfantibody"):
            pilot = _pilots(slugs)[slug]
            params = pilot["params"]
            defaults = self._form_defaults(client, slug, params)
            assert estimated_cost_for_tool(None, slug, params) \
                < estimated_cost_for_tool(None, slug, defaults), slug
