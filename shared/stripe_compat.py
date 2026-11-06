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
    """
    if isinstance(obj, dict):
        return {k: as_dict(v) for k, v in obj.items()}
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return as_dict(to_dict())
    if isinstance(obj, (list, tuple)):
        return [as_dict(v) for v in obj]
    return obj
