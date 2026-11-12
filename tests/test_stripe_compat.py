"""Tests for :mod:`shared.stripe_compat`.

stripe-python 15 stopped making ``StripeObject`` a ``dict`` subclass.
Measured outside this suite, with the repo venv interpreter, on 12.5.1 (the
version installed in that venv, under the pre-bump ``stripe>=9.0,<13.0``) and
on 15.6.2 (what dependabot #47's floor resolves to):

    isinstance(obj, dict)   True  -> False
    dict(obj)               ok    -> TypeError
    obj.get("x")            ok    -> AttributeError
    obj.to_dict_recursive() ok    -> gone
    obj.to_dict()           ok    -> ok

:class:`StripeObject15` reproduces the 15.x shape (not a dict, no
``get``, ``to_dict`` only) so the suite can exercise the conversion
without either version installed.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from shared.stripe_compat import as_dict


class StripeObject15:
    """A stripe object with the 15.x surface: no dict base, no ``get``."""

    def __init__(self, data: dict) -> None:
        object.__setattr__(self, "_data", dict(data))

    def to_dict(self, recursive: bool = True, for_json: bool = False) -> dict:
        # 15.x holds decimal_string fields as Decimal; for_json=True gives str.
        if for_json:
            return {
                k: str(v) if isinstance(v, Decimal) else v
                for k, v in self._data.items()
            }
        return dict(self._data)

    def __getattr__(self, name: str):
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError:
            raise AttributeError(name) from None


class StripeObject12(dict):
    """A stripe object with the 12.x surface: IS a dict.

    ``to_dict`` raises so a test can prove ``as_dict`` never calls it on
    a dict -- on 12.5.1 calling it emits a DeprecationWarning.
    """

    def to_dict(self):  # pragma: no cover - must never be called
        raise AssertionError("as_dict called to_dict() on a dict")


def _wrap(value):
    """Recursively wrap dicts as 15.x stripe objects, as the SDK nests them."""
    if isinstance(value, dict):
        return StripeObject15({k: _wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


def test_non_dict_object_becomes_a_plain_dict():
    obj = StripeObject15({"id": "evt_1", "type": "checkout.session.completed"})
    out = as_dict(obj)
    assert type(out) is dict
    assert out == {"id": "evt_1", "type": "checkout.session.completed"}


def test_nested_objects_and_lists_are_converted():
    event = _wrap({
        "id": "evt_1",
        "data": {"object": {"metadata": {"user_id": "u1"}}},
        "items": [{"n": 1}, {"n": 2}],
    })
    out = as_dict(event)
    assert out["data"]["object"]["metadata"]["user_id"] == "u1"
    assert type(out["data"]["object"]["metadata"]) is dict
    assert out["items"] == [{"n": 1}, {"n": 2}]
    assert all(type(i) is dict for i in out["items"])


def test_dicts_are_recursed_without_calling_to_dict():
    """The 12.x branch: a dict subclass must convert via ``items()``."""
    out = as_dict(StripeObject12({"a": StripeObject12({"b": 1})}))
    assert out == {"a": {"b": 1}}
    assert type(out) is dict
    assert type(out["a"]) is dict


@pytest.mark.parametrize("value", ["s", 1, 1.5, None, True])
def test_scalars_pass_through(value):
    assert as_dict(value) is value


def test_the_double_really_has_the_15x_surface():
    """Guard the double itself: if it keeps dict behaviour it proves nothing."""
    obj = StripeObject15({"id": "x"})
    assert not isinstance(obj, dict)
    assert not hasattr(obj, "get")
    assert not hasattr(obj, "to_dict_recursive")
    with pytest.raises(TypeError):
        dict(obj)
    assert obj.id == "x"


class _FakeStripe:
    """Just enough of the module surface ``billing.checkout`` calls."""

    def __init__(self, returned):
        returned_ = returned

        class _PI:
            @staticmethod
            def create(**_kw):
                return returned_

        class _Session:
            @staticmethod
            def retrieve(_sid):
                return returned_

        class _Checkout:
            Session = _Session

        self.PaymentIntent = _PI
        self.checkout = _Checkout


def test_off_session_intent_on_15x_returns_a_plain_dict(monkeypatch):
    """The confirm=True charge has already happened; the return must not raise."""
    from billing import checkout

    pi = _wrap({"id": "pi_1", "status": "succeeded", "metadata": {"kind": "auto_reload"}})
    monkeypatch.setattr(checkout, "_stripe_client", lambda: _FakeStripe(pi))
    out = checkout.create_off_session_payment_intent(
        stripe_customer_id="cus_1", payment_method_id="pm_1", amount_usd="10.00",
    )
    assert type(out) is dict and type(out["metadata"]) is dict
    assert out["id"] == "pi_1"


def test_topup_session_metadata_on_15x_is_a_plain_dict(monkeypatch):
    """blueprints/wallet.py calls ``metadata.get`` on this with no try."""
    from billing import checkout

    sess = _wrap({"id": "cs_1", "url": "https://x", "metadata": {"user_id": "u1"}})
    monkeypatch.setattr(checkout, "_stripe_client", lambda: _FakeStripe(sess))
    out, err = checkout.retrieve_topup_session("cs_1")
    assert err is None
    assert type(out["metadata"]) is dict and out["metadata"].get("user_id") == "u1"
    assert out["status"] is None


def test_decimal_fields_come_back_json_serialisable():
    # stripe 15 coerces decimal_string fields (checkout session
    # currency_conversion.fx_rate) to Decimal. The webhook stores the whole
    # event as JSON in stripe_events.payload, and a Decimal there fails the
    # insert, which _insert_event_once logs as a replay.
    event = _wrap({
        "id": "evt_1",
        "data": {"object": {"currency_conversion": {"fx_rate": Decimal("0.92")}}},
    })
    out = as_dict(event)
    assert out["data"]["object"]["currency_conversion"]["fx_rate"] == "0.92"
    json.dumps(out)
