"""The submit gate prices the tool's validated inputs, not the raw form.

``shared/wallet_guard.py::_priced_params``. Before it, ``requires_wallet``
priced every key the POST carried, so a key the tool never reads could set the
hold, and a key the tool recomputes could undercut it.
"""

from __future__ import annotations

import io
from decimal import Decimal
from unittest.mock import patch

import pytest
from flask import Flask, g, request

import app  # noqa: F401  -- importing the app is what registers the adapters
from shared.wallet import REASON_OK, PreflightResult
from shared.wallet_estimates import cushioned_hold_usd, estimated_cost_for_tool
from shared.wallet_guard import requires_wallet
from tools import base as tool_base

_WALLET_OK = {"balance_usd": 1000.0, "wallet_frozen": False}
_ALLOW = PreflightResult(
    allow=True, reason=REASON_OK, estimated_cost_usd=Decimal("1"),
    balance_usd=Decimal("1000"), deficit_usd=Decimal("0"),
    hard_cap_usd=Decimal("300"),
)


@pytest.fixture(autouse=True)
def offline_estimator():
    with patch("shared.credits.get_service_client", return_value=None):
        yield


def _post(tool_slug: str, data: dict, handler_body=None) -> dict:
    seen: dict = {}
    gate_app = Flask(__name__)
    gate_app.config["SECRET_KEY"] = "k"

    @requires_wallet(tool_slug=tool_slug)
    def handler():
        seen["estimate"] = g.wallet_estimate_usd
        seen["params"] = g.wallet_params
        if handler_body:
            seen["handler"] = handler_body()
        g.wallet_hold_consumed = True
        return "ok", 200

    gate_app.add_url_rule("/x", view_func=handler, methods=["POST"])
    with gate_app.test_client() as client, patch(
        "shared.wallet_guard.wallet_reserve_hold", return_value="tx-1"
    ) as reserve, patch(
        "shared.wallet_guard.open_live_run", return_value="tx-1"
    ), patch(
        "shared.wallet_guard.get_or_create_wallet", return_value=_WALLET_OK
    ), patch(
        "shared.wallet_guard.wallet_preflight", return_value=_ALLOW
    ):
        with client.session_transaction() as sess:
            sess["user_id"] = "u-1"
        resp = client.post("/x", data=data)
    assert resp.status_code == 200
    seen["held"] = reserve.call_args.args[3] if reserve.called else None
    return seen


def test_boltzgen_num_designs_is_not_priced():
    """The recorded case: boltzgen's form has no ``num_designs`` and its
    ``build_payload`` sends a fixed 200, yet the POST's 100 scaled the cap."""
    plain = _post("boltzgen", {"preset": "pilot"})
    crafted = _post("boltzgen", {"preset": "pilot", "num_designs": "100"})

    assert crafted["held"] == plain["held"]
    assert crafted["estimate"] == plain["estimate"]
    assert "num_designs" not in crafted["params"]
    # Not vacuous: priced as the raw form did, the same key moves the hold.
    assert cushioned_hold_usd(
        None, "boltzgen", {"preset": "pilot", "num_designs": 100}
    ) > plain["held"]


def test_af2_batch_prices_the_records_not_the_hidden_count():
    """``n_designs_total`` is a hidden field the page's JS fills; ``validate``
    counts the records itself, and that count is what runs and settles."""
    fasta = "\n".join(f">r{i}\nMKTAYIAKQRQISFVKSHFSRQ" for i in range(6))
    seen = _post(
        "af2", {"preset": "batch", "sequences": fasta, "n_designs_total": "1"}
    )

    six = estimated_cost_for_tool(
        None, "af2", {"preset": "batch", "parameters": {"n_designs_total": 6}}
    )
    one = estimated_cost_for_tool(
        None, "af2", {"preset": "batch", "n_designs_total": 1}
    )
    assert six > one
    assert seen["estimate"] == six
    assert seen["params"]["parameters"] == {"n_designs_total": 6}


def test_an_upload_is_rewound_for_the_handler():
    """The gate's ``validate`` reads af2's ``fasta_file``; the handler's own
    ``validate`` call must still find the whole file."""
    def handler_validate():
        return tool_base.get("af2").validate(request.form, request.files)

    seen = _post(
        "af2",
        {
            "preset": "standalone",
            "fasta_file": (io.BytesIO(b">a\nMKTAYIAKQRQISFVKSHFSRQ\n"), "a.fasta"),
        },
        handler_body=handler_validate,
    )

    inputs, err = seen["handler"]
    assert err is None, err
    assert inputs is not None
