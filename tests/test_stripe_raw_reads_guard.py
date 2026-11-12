"""Fail when non-test code reads a stripe object like a dict without ``as_dict``.

On stripe-python 15.x ``StripeObject`` is not a dict, so ``dict(x)``,
``x.get(...)``, ``x[...]``, ``isinstance(x, dict)`` and
``x.to_dict_recursive()`` on a raw SDK object raise or quietly go False
(see shared/stripe_compat.py). This walks every non-test module that imports
stripe, finds each name bound to the result of a ``stripe.*`` call (or a loop
over one), and fails on any dict-style read of that name, or of an attribute
chain off it, that is not wrapped in ``as_dict(...)``.

Its reach is the bound names in the module that made the call. A stripe object
passed into a helper as a parameter is not followed; ``billing.checkout._attr``
is one such helper and converts with ``as_dict`` itself. ``test_checker_*``
below pins what the walk does and does not flag.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_DICT_METHODS = {"get", "items", "keys", "values", "to_dict_recursive"}


def _is_stripe_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    while isinstance(f, ast.Attribute):
        f = f.value
    return isinstance(f, ast.Name) and f.id == "stripe"


def _is_as_dict(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "as_dict"
    )


def _has_unwrapped_stripe_call(node: ast.AST) -> bool:
    if _is_as_dict(node):
        return False
    if _is_stripe_call(node):
        return True
    return any(_has_unwrapped_stripe_call(c) for c in ast.iter_child_nodes(node))


def _root(node: ast.AST):
    """Name at the root of ``x.a.b`` / ``x[k]`` / ``getattr(x, ...)``; None past any other call."""
    while True:
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
        ):
            node = node.args[0]
        else:
            return node.id if isinstance(node, ast.Name) else None


def _names(target: ast.AST):
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]


def _scope_nodes(scope: ast.AST):
    """Nodes of one function (or the module) body, not descending into nested defs."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _scope_raw_reads(scope: ast.AST) -> list[str]:
    nodes = list(_scope_nodes(scope))
    bound: set[str] = set()
    # Twice, so a loop over a name bound later in walk order is still followed.
    for _ in range(2):
        for node in nodes:
            if isinstance(node, ast.Assign) and _has_unwrapped_stripe_call(node.value):
                for t in node.targets:
                    bound.update(_names(t))
            elif isinstance(node, ast.For) and (
                _has_unwrapped_stripe_call(node.iter) or _root(node.iter) in bound
            ):
                bound.update(_names(node.target))

    hits = []
    for node in nodes:
        subject = None
        if isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Slice):
            subject = node.value  # a slice is a list read, never a dict read
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _DICT_METHODS:
                subject = f.value
            elif isinstance(f, ast.Name) and f.id in ("dict", "isinstance") and node.args:
                subject = node.args[0]
        if subject is None:
            continue
        if _is_stripe_call(subject) or _root(subject) in bound:
            hits.append(f"line {node.lineno}: {ast.unparse(node)[:100]}")
    return hits


def raw_reads(source: str) -> list[str]:
    tree = ast.parse(source)
    scopes = [tree] + [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return [hit for scope in scopes for hit in _scope_raw_reads(scope)]


def _stripe_modules():
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if rel.parts[0] in ("tests", "venv", "node_modules") or rel.parts[0].startswith("."):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "import stripe" in text:
            yield rel, text


def test_scan_finds_the_known_stripe_modules():
    found = {str(rel).replace("\\", "/") for rel, _ in _stripe_modules()}
    assert {"billing/checkout.py", "webhooks/stripe.py"} <= found
    assert any(f.startswith("scripts/deploy/pass7_") for f in found)


@pytest.mark.parametrize(
    "rel,text", list(_stripe_modules()), ids=lambda v: str(v).replace("\\", "/")[:60]
)
def test_no_raw_dict_read_of_a_stripe_object(rel, text):
    assert raw_reads(text) == [], (
        f"{rel}: read a stripe object like a dict; wrap it in "
        "shared.stripe_compat.as_dict first"
    )


@pytest.mark.parametrize("src", [
    "x = stripe.Charge.retrieve('c')\ny = dict(x)",
    "x = stripe.Charge.retrieve('c')\nx.get('metadata')",
    "x = stripe.Charge.retrieve('c')\nx['metadata']",
    "x = stripe.Charge.retrieve('c')\nisinstance(x, dict)",
    "x = stripe.PaymentIntent.create()\nx.to_dict_recursive()",
    "a = stripe.Account.retrieve()\na.business_profile.get('name')",
    "s = stripe.tax.Settings.retrieve()\nd = getattr(s, 'defaults').get('k')",
    "dict(stripe.Charge.retrieve('c'))",
    "for p in stripe.Product.list().auto_paging_iter():\n    p.get('metadata')",
    "eps = list(stripe.WebhookEndpoint.list().auto_paging_iter())\nfor ep in eps:\n    ep['url']",
    "def f():\n    pi = stripe.PaymentIntent.retrieve('p')\n    return pi.get('metadata')",
])
def test_checker_flags_raw_reads(src):
    assert raw_reads(src), src


@pytest.mark.parametrize("src", [
    "x = stripe.Charge.retrieve('c')\nas_dict(x).get('metadata')",
    "x = as_dict(stripe.Charge.retrieve('c'))\nx.get('metadata')",
    "a = stripe.Account.retrieve()\nas_dict(a.business_profile).get('name')",
    "e = stripe.Webhook.construct_event(p, s, k)\nreturn_value = as_dict(e)",
    "ep = stripe.WebhookEndpoint.retrieve('we')\nset(ep.enabled_events or [])",
    "s = stripe.checkout.Session.create()\n_attr(s, 'id')",
    "evs = list(stripe.Event.list())\nfor e in evs[:10]:\n    print(e.id)",
    # Same name, different function: the plain-dict parameter is not the SDK object.
    "def a():\n    event = stripe.Webhook.construct_event(p, s, k)\n    return as_dict(event)\n"
    "def b(event):\n    return event.get('id')",
])
def test_checker_allows_converted_or_attribute_reads(src):
    assert raw_reads(src) == [], src
