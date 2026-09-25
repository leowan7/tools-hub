"""P5 "Check my settings" (POST /tools/<tool>/validate) and P6 form sections.

The validate route must be free: no job row, no wallet hold. The anonymous
side of the gate is in tests/test_public_tool_pages.py
(TestSubmitGateStillHolds, parametrised over "validate").
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

ROOT = Path(__file__).resolve().parent.parent
UBIQUITIN = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"

_RFDIFFUSION_FORM = {
    "preset": "pilot",
    "target_chain": "A",
    "hotspot_residues": "10,12",
    "binder_length_min": "60",
    "binder_length_max": "80",
    "num_designs": "4",
}


def _ctx():
    return SimpleNamespace(user_id="u-1", tier="free", balance=100, email="u@example.com")


def _login(client, user_id=None):
    with client.session_transaction() as sess:
        sess["user_email"] = "u@example.com"
        if user_id:
            sess["user_id"] = user_id


@pytest.fixture
def paid_path_spies():
    """Every call into job creation or a wallet hold fails the test.

    The wallet guard is armed (funded wallet, paid estimate) so that
    @requires_wallet on the route would reach reserve_hold rather than pass
    through on a missing wallet. Tests using this also log in with a user_id.
    """
    g = "shared.wallet_guard."
    with patch("blueprints.tools.create_job", side_effect=AssertionError("job created")) as job, \
            patch(g + "wallet_reserve_hold", side_effect=AssertionError("wallet hold placed")) as hold, \
            patch(g + "estimated_cost_for_tool", return_value=Decimal("5")), \
            patch(g + "get_or_create_wallet", return_value={"balance_usd": 100}), \
            patch(g + "wallet_preflight", return_value=SimpleNamespace(allow=True)), \
            patch(g + "cushioned_hold_usd", return_value=Decimal("6")):
        yield job, hold


def _validate(client, slug, data):
    resp = client.post(f"/tools/{slug}/validate", data=data)
    assert resp.status_code == 200, f"{slug} -> {resp.status_code}"
    return resp.get_json()


class TestValidateIsFree:
    def test_every_tool_answers_without_a_job_or_hold(self, all_tools_app, paid_path_spies):
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        _login(client, user_id="u-1")
        for slug in slugs:
            verdict = _validate(client, slug, {"preset": "pilot"})
            assert set(verdict) == {"ok", "error"}, (slug, verdict)
        job, hold = paid_path_spies
        job.assert_not_called()
        hold.assert_not_called()

    def test_valid_settings_say_ok(self, all_tools_app, paid_path_spies):
        client = all_tools_app[0].test_client()
        _login(client)
        verdict = _validate(client, "esmfold", {"preset": "standalone", "fasta_text": f">q\n{UBIQUITIN}"})
        assert verdict == {"ok": True, "error": None}

    def test_bad_settings_carry_the_validator_message(self, all_tools_app, paid_path_spies):
        client = all_tools_app[0].test_client()
        _login(client)
        verdict = _validate(client, "esmfold", {"preset": "standalone"})
        assert verdict["ok"] is False and verdict["error"]

    def test_missing_structure_is_reported(self, all_tools_app, paid_path_spies):
        client = all_tools_app[0].test_client()
        _login(client)
        verdict = _validate(client, "rfdiffusion", _RFDIFFUSION_FORM)
        assert verdict == {"ok": False, "error": "Upload a target PDB file."}

    def test_single_container_ceiling_is_reported(self, all_tools_app, paid_path_spies):
        client = all_tools_app[0].test_client()
        _login(client)
        from shared.compute_campaigns import single_container_ceiling
        over = str(single_container_ceiling("rfdiffusion") + 1)
        verdict = _validate(client, "rfdiffusion", {**_RFDIFFUSION_FORM, "num_designs": over})
        assert verdict["ok"] is False and "campaign" in verdict["error"], verdict
        # In campaign mode the page submits to campaigns, which has no such ceiling.
        verdict = _validate(client, "rfdiffusion",
                            {**_RFDIFFUSION_FORM, "num_designs": over, "_campaign": "1"})
        assert verdict == {"ok": False, "error": "Upload a target PDB file."}, verdict


class TestCheckButton:
    def test_script_never_submits_the_form(self):
        js = (ROOT / "static/js/check_settings.js").read_text(encoding="utf-8")
        assert not re.search(r"\.\s*(?:submit|requestSubmit)\s*\(", js)
        assert "/validate" in js

    def test_signed_in_only_on_every_tool_form(self, all_tools_app):
        flask_app, slugs = all_tools_app
        client = flask_app.test_client()
        for slug in slugs:
            assert "data-check-settings" not in client.get(f"/tools/{slug}").get_data(as_text=True), slug
        _login(client)
        with patch("app.load_user_context", return_value=_ctx()), \
                patch("blueprints.tools.load_user_context", return_value=_ctx()), \
                patch("blueprints.tools.get_or_create_wallet",
                      return_value={"balance_usd": 12.5, "wallet_frozen": False}):
            for slug in slugs:
                body = client.get(f"/tools/{slug}").get_data(as_text=True)
                assert body.count("<button type=\"button\" class=\"btn-secondary\" data-check-settings>") == 1, slug


class TestFormSections:
    SECTIONED = ("rfdiffusion", "bindcraft", "boltzgen", "pxdesign", "rfantibody")

    def test_every_summary_placeholder_names_a_field(self, all_tools_app):
        """A typo in a data-summary template would print an empty value forever."""
        client = all_tools_app[0].test_client()
        for slug in self.SECTIONED:
            body = client.get(f"/tools/{slug}").get_data(as_text=True)
            templates = re.findall(r'<details class="form-section" data-summary="([^"]*)"', body)
            assert len(templates) == 2, (slug, templates)
            for name in re.findall(r"\{(\w+)(?:#\w+)?\}", " ".join(templates)):
                assert f'name="{name}"' in body, (slug, name)
