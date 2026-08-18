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
