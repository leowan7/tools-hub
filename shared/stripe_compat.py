"""Plain-dict conversion for stripe-python objects.

stripe-python 15 stopped making ``StripeObject`` a ``dict`` subclass, so
``dict(obj)``, ``obj.get(k)`` and ``isinstance(obj, dict)`` all break on the
objects returned by ``stripe.*.retrieve`` / ``construct_event``.  Both
surfaces are exercised by the doubles in tests/test_stripe_compat.py; the
version numbers behind them were measured outside the suite and are recorded
in this commit's message.

Call ``as_dict`` once at the boundary where a stripe object enters our code;
everything downstream then reads a plain dict.
"""

from __future__ import annotations


def as_dict(obj):
    """Return ``obj`` with any stripe object converted to plain dicts/lists.

    Dicts are recursed into before ``to_dict()`` is considered, so on
    stripe 12.x -- where ``StripeObject`` IS a dict subclass -- we never call
    the deprecated ``to_dict``; on stripe 15.x ``to_dict()`` runs and the
    recursion converts nested objects too.

    ``for_json=True`` makes 15.x return ``decimal_string`` fields (which it
    otherwise coerces to ``Decimal``) as ``str``, so the result can be stored
    as JSON -- webhooks/stripe.py ``_insert_event_once`` writes the whole event
    to ``stripe_events.payload``. tests/test_stripe_compat.py
    ``test_decimal_fields_come_back_json_serialisable`` covers it.
    """
    if isinstance(obj, dict):
        return {k: as_dict(v) for k, v in obj.items()}
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return as_dict(to_dict(for_json=True))
    if isinstance(obj, (list, tuple)):
        return [as_dict(v) for v in obj]
    return obj
