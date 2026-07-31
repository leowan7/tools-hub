"""Shared static guard: a template must not derive money figures itself.

Both estimate-backed pages (``templates/targets/launch.html`` and
``templates/runs/new.html``) render money that sits directly above a consent
checkbox. Rounding for those figures lives in Decimal on the server, where a
cost rounds UP and a balance rounds DOWN. A page that re-derives 2dp from the
exact 4dp value rounds to NEAREST, which prints a hold BELOW the amount about
to be reserved.

This lives in one module rather than being copied into each test file because
the first version of the guard was too narrow and a reviewer walked straight
through it; two copies of a guard drift, and the weaker copy is the one that
decides whether a regression ships.

**What this cannot do.** It is a syntactic guard over a script no test executes
(A47). It closes the KNOWN routes to a wrong figure; it cannot close all of
them, and it must not be read as saying the right figure is printed.

**This is a lint, not a proof, and previous versions of this docstring claimed
otherwise four rounds running.** Each version enumerated the ways past it and
declared the rest closed; each was falsified by the next reviewer, and each
false sentence was written in the very commit that made it false. The routes
found so far, in order: ``toPrecision(3)`` (a rounding call the list did not
name), ``d['balance' + '_usd']`` (a computed key), ``money(d.a_display - 0.01)``
(arithmetic in the argument), ``money(x)`` with ``x`` assigned above,
``money(fw_display)`` with a local named for the suffix, and
``d.fw_display = d.a_display - 0.01; money(d.fw_display)`` with a *member*
assigned above.

That last one is the point at which the enumeration should stop. **A pattern
matcher cannot establish where a value came from.** A member is as easy to
assign as a local; beyond it lie ``money (v)`` with a space, ``window.w_display``,
mutating the ``_display`` field in place, and a ``defineProperty`` getter. Each
of those is one line, and closing any one of them does not make the next
unreachable.

So read this guard as catching the ACCIDENTAL mistake -- an author reaching for
``toFixed`` out of habit, or doing arithmetic inline because the number looked
like a number. It does not stop a determined author and no wording of it will.
The thing that actually keeps these figures right is that the server computes
every displayed string in Decimal and the page only interpolates them; this file
exists so that arrangement is not undone by accident.

Do not add a route count back to this docstring. If you tighten the grammar,
tighten it and say what the new rule is, without claiming what remains.

Known false positives, deliberately not fixed: ``d['first_wave_usd_display']``
(a literal string key), ``d.rows[i].x_display`` (a variable index), ``??``, and
template literals are rejected while printing the correct figure. None is used
by either template. **A guard that fails closed on a valid-but-unused form costs
one rewrite; a guard that fails open costs money on a consent screen.** Widening
the grammar is how two of the holes above were opened.
"""

from __future__ import annotations

import re

# Every money key the estimate endpoints put on the wire in exact 4dp form.
# Each also ships a ``<key>_display`` sibling, and only the sibling may be
# rendered.
MONEY_FIELDS = ("budget_usd", "first_wave_usd", "balance_usd", "per_chunk_usd")

# Anything that turns a display string back into a number, or re-rounds it.
# Once a 2dp string becomes a Number the server's chosen direction is gone, and
# whatever is printed next was computed on the page. ``toFixed`` was the only
# one of these the first version knew, so ``Number(x).toPrecision(3)`` rendered
# a $573.68 hold as "$574" with the suite green.
NUMERIC_COERCIONS = (
    "toFixed", "toPrecision", "toExponential",
    "parseFloat", "parseInt", "Number(",
    "Math.round", "Math.floor", "Math.ceil", "Math.abs",
)


# The only shape money() may be called on: a MEMBER ACCESS ending in
# ``_display``, optionally with a string-literal fallback. Anything else --
# arithmetic, a cast, a computed key, or a local whose value cannot be read from
# the call site -- is rejected.
#
# The final ``\.`` is load-bearing and is not decoration. Without it the root
# token backtracks and a BARE LOCAL satisfies the whole pattern: ``fw_display``
# matched, so ``var fw_display = d.a_usd_display - 0.01; money(fw_display)``
# passed on both consent pages with 100 tests green. Accept/reject then turns on
# what the author happens to name the variable, which is the defect the previous
# version claimed to have fixed. Requiring a dot means the value's origin is
# always readable at the call site.
_MONEY_ARG = re.compile(
    r"[A-Za-z_$][\w$]*"                       # root object
    r"(?:\.[A-Za-z_$][\w$]*|\[[0-9]+\])*"     # .field or [0]
    r"\.[A-Za-z_$][\w$]*_display"             # FINAL hop must be a member access
    r"""(?:\s*\|\|\s*'[^']*'|\s*\|\|\s*"[^"]*")?"""   # optional '0.00' fallback
)


def assert_money_slots_are_not_crossed(path: str, slots: dict) -> None:
    """Fail if a money figure is rendered into the WRONG slot.

    ``slots`` maps element id -> the ``*_display`` key that belongs in it.

    Separate from the provenance question, and unlike provenance this IS
    statically decidable: the assignment names both the destination element and
    the source key on one line. It was simply never checked. Putting
    ``budget_usd_display`` into the "Held to start" slot, or
    ``first_wave_usd_display`` into the Balance slot, left 292 tests green on
    both consent pages -- a user would read the wrong number under the right
    label, directly above the line consenting to it.

    The slot that matters most is the first wave: it is the figure the consent
    sentence refers to, and the budget is always the larger number, so crossing
    those two overstates what is about to be held.
    """
    src = open(path, encoding="utf-8").read()
    for element_id, expected_key in slots.items():
        m = re.search(
            rf"getElementById\(['\"]{re.escape(element_id)}['\"]\)"
            rf"\s*\.textContent\s*=\s*money\(\s*([^)]*?)\s*\)",
            src,
            re.S,
        )
        assert m, (
            f"{path}: no money() assignment found for #{element_id}. Either the "
            f"slot was renamed or it is now filled some other way; update this "
            f"map deliberately rather than deleting the entry."
        )
        assert m.group(1).endswith(expected_key), (
            f"{path}: #{element_id} is filled from {m.group(1)!r}, expected a "
            f"field ending in {expected_key!r}. A figure under the wrong label "
            f"is a wrong figure."
        )


def assert_template_prints_no_raw_money(path: str) -> None:
    """Fail if ``path`` can print a money figure it derived or took raw."""
    src = open(path, encoding="utf-8").read()

    # 1. No numeric coercion or rounding of money, anywhere in the file.
    #    Matching the names means a COMMENT naming one fails too. That is
    #    deliberate: both templates describe the method beside ``money()``
    #    instead of spelling it, and say why. It is a blunt rule -- these
    #    templates have no other use for arithmetic, and if one ever does, the
    #    right move is to narrow the rule deliberately rather than to widen an
    #    exception.
    found = [name for name in NUMERIC_COERCIONS if name in src]
    assert not found, (
        f"{path} coerces or rounds a value with {found}. Money rounding "
        f"direction is chosen on the server; doing arithmetic here can only "
        f"undo it."
    )

    # 2. No exact 4dp field reaches the renderer under its own name. Checking
    #    only the arguments of money() was not enough: `'$' + d.balance_usd`
    #    bypasses money(), contains no rounding call, and renders "$573.6736".
    bare = re.findall("|".join(f"{f}(?!_display)" for f in MONEY_FIELDS), src)
    assert not bare, (
        f"{path} references the exact 4dp field(s) {sorted(set(bare))}. Only "
        f"the *_display strings may be rendered."
    )

    # 3. And money() is handed a *_display FIELD REFERENCE and nothing else.
    #    A full match against a grammar, not a substring test, because JS
    #    coerces implicitly for -, *, / and unary +:
    #    ``money(d.first_wave_usd_display - 0.01)`` contains "_display",
    #    names no coercion, and hides its 4dp field behind the suffix check 2
    #    looks past. Under the old substring form it printed "$9.18" above
    #    "the amount above will be held" against a column summing to $9.19.
    #
    #    NOT redundant with check 2: it is the only check that catches a field
    #    reached by a computed key (``money(d['balance' + '_usd'])``).
    #
    #    The grammar admits a dotted/indexed path ending in ``_display`` plus an
    #    optional string-literal fallback, so ``money(d.rows[0].x_display)`` and
    #    ``money(d.x_display || '0.00')`` are legal. It rejects a BARE LOCAL,
    #    including the ``x`` the definition uses. An earlier version allowed
    #    ``x`` outright to accommodate ``function money(x)``, which let any call
    #    site launder anything through a local: ``var x = d.a_display - 0.01;
    #    money(x)`` passed while printing a cent under the hold. The definition
    #    is excluded by matching only CALLS, via the lookbehind below.
    # ``money\s*\(`` because ``money (v)`` is a legal call and ``money\(`` does
    # not see it at all -- one space made every check below inapplicable.
    for call in re.findall(r"(?<!function )money\s*\(([^)]*)\)", src):
        assert _MONEY_ARG.fullmatch(call.strip()), (
            f"{path}: money() called on {call!r}. It takes a *_display field "
            f"reference (optionally with a string fallback) and nothing else "
            f"-- no arithmetic, no computed key, no cast, no bare local."
        )
