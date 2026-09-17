"""How many designs one Proteina shard holds.

tools/proteina/meta.py contradicted itself about this, and the contradiction
reached the number a user types. The pilot card said "Starter pilot: one
shard, 8 designs", and templates/tools/proteina_form.html says "One search
shard yields up to 8 designs on a single A100-80GB" under the field --
while the worked example on the same page said "One shard: 16 starting
samples with 4 replicas each. A shard is one container, so all 64 came back
from a single call." Both described something real: 8 is the shard the FORM
launches, 64 is the shard tools/proteina/shard_driver.py launches. What the
page never named was that second path, so the example read as a form result.
Typing 64 plans EIGHT shards.

WHERE THE NUMBER WAS ALREADY HELD, AND WHY THAT WAS NOT ENOUGH. Three
absolute literals predate this file:
  * tests/test_proteina_smoke.py:649  _CHUNK_SIZE_OVERRIDE["proteina"] == 8
  * tests/test_pdb_preflight.py:1696  rules.size.runtime_baseline_designs == 8
  * tests/test_compute_campaigns.py   _chunk_size_for("proteina") == 8
Each pins an integer, and none of them can see a SENTENCE -- which is how the
prose drifted 8x with the suite green. This file pins the prose.

It also closes a gap between those integers. The adapter's _SHARD_* constants
and compute_campaigns' override are INDEPENDENT literals with nothing deriving
one from the other, so editing the generation profile alone leaves all three
guards above green while a container starts returning a different number of
designs than the splitter budgeted for.

THE PROSE PINS ARE REGEXES, NOT SUBSTRINGS -- a repair, not a preference. An
earlier draft pinned four independent substrings ("8", "shards", the runtime,
the band). A row asserting the exact OPPOSITE ("you get one shard of 64
designs ... the 57 minutes sits comfortably inside the 9 to 15") satisfied all
four; so did the original defect's own sentence with a token appended, and so
did bare keyword salad with no sentence at all. A lone "8" is also satisfied
by "48 designs" and by "64 designs (8 shards)". Vocabulary is not meaning:
each pin below matches the SHAPE of the claim it protects. Re-measured after
the change: all five of those defeating rewrites now fail, as does the shipped
row judged against a shard width moved to 16.

WHAT THESE PINS STILL CANNOT DO. They require the right claims to be present;
they cannot forbid a wrong one from being ADDED. A row that states the split
correctly and then appends "...which is why the 9 to 15 min band does not
apply to shards at all" passes every assertion here. Nothing short of reading
the sentence catches that, and no guard in this file pretends otherwise.
"""
from __future__ import annotations

import json
import math
import pathlib
import re

import pytest

from shared.compute_campaigns import (
    _CHUNK_SIZE_OVERRIDE,
    _chunk_size_for,
    plan_chunks,
)
from tools.proteina import _SHARD_DESIGNS
from tools.proteina import meta

REPO = pathlib.Path(__file__).resolve().parent.parent


def _payload_designs() -> int:
    """How many designs the rendered example table has rows for."""
    payload = json.loads(
        (REPO / "tools" / "proteina" / "example" / "result.json")
        .read_text(encoding="utf-8"),
    )
    return len(payload["candidates"])


def _designs_row() -> str:
    """The worked example's explanation for the "Number of designs" field.

    Keyed on the field name, which is itself pinned elsewhere: ``inputs_used``
    field names must match the form's rendered <label> text verbatim
    (tests/test_worked_examples.py::test_input_field_names_exist_on_the_form),
    so this lookup cannot drift without that guard failing first.
    """
    for field, _value, why in meta.EXAMPLE["inputs_used"]:
        if field == "Number of designs":
            return why
    pytest.fail("the worked example no longer explains 'Number of designs'")


def test_adapter_shard_width_is_the_campaign_chunk_size():
    """Two independent literals, and this is the only thing tying them.

    tools/proteina/__init__.py:162-165 builds _SHARD_DESIGNS from
    _SHARD_NSAMPLES x _SHARD_REPLICAS. shared/compute_campaigns.py:514 sets
    _CHUNK_SIZE_OVERRIDE["proteina"] = 8 as a separate literal. Neither file
    reads the other, so editing the generation profile moves _SHARD_DESIGNS
    and leaves _chunk_size_for("proteina") at 8 -- measured: with
    _SHARD_NSAMPLES raised to 8, _chunk_size_for still returns 8.

    _SHARD_DESIGNS == _SHARD_NSAMPLES * _SHARD_REPLICAS is deliberately NOT
    asserted: __init__.py:165 defines it as exactly that product three lines
    below its operands, so asserting it restates the definition and can fail
    only if someone replaces the definition with a literal.
    """
    assert _chunk_size_for("proteina") == _SHARD_DESIGNS
    # Through the override entry specifically. _chunk_size_for falls back to a
    # derived rate when a tool has no override row, and for proteina that
    # fallback also lands on 8 -- so the assertion above survives deleting the
    # override entry outright, and this one does not.
    assert _CHUNK_SIZE_OVERRIDE["proteina"] == _SHARD_DESIGNS


@pytest.mark.parametrize("requested", [1, 8, 9, 16, 64, 65, 200])
def test_num_designs_buys_shards_never_a_wider_one(requested):
    """``num_designs`` is a shard COUNT; no value of it widens a container.

    The first assertion is the load-bearing one.

    HONEST LIMIT on the second: plan_chunks computes total_subjobs as
    ceil(requested / chunk_size) itself (compute_campaigns.py:654-655), and
    once chunk_size is fixed above, the assertion restates that arithmetic --
    brute-forced over requested = 1..4000 it never disagrees. It is kept
    because it still catches a change to that formula, and the NON-MULTIPLES
    (9, 65) are what make it do so: with only exact multiples of 8 in this
    list, swapping ceil for floor inside plan_chunks stayed green on every
    value except 1.
    """
    plan = plan_chunks("proteina", requested, "protein_binder")
    assert plan.chunk_size == _SHARD_DESIGNS
    assert plan.total_subjobs == math.ceil(requested / _SHARD_DESIGNS)


def test_pilot_card_quotes_the_real_shard_width():
    """The pilot card was the statement that was RIGHT, so it is pinned to the
    splitter rather than left to be "corrected" toward the example.

    Matched as phrases rather than as the digit: a bare "8" is satisfied by
    "48 designs is one shard on one GPU" and by "Starter pilot: one shard, 64
    designs (8 shards)", both of which advertise the wrong width, and both of
    which were demonstrated to pass a substring pin.
    """
    width = _SHARD_DESIGNS
    assert meta.PILOT["params"]["num_designs"] == str(width)
    assert re.search(rf"one shard, {width} designs\b", meta.PILOT["label"])
    assert re.search(rf"\b{width} designs is one shard\b",
                     meta.PILOT["next_step"])


def test_the_form_field_help_quotes_the_same_width():
    """The copy closest to the box the user types into. Nothing referenced it
    before: it sits in a template, so the guards that read meta.py could not
    see it, and it is the same defect class one file over.
    """
    html = (REPO / "templates" / "tools" / "proteina_form.html").read_text(
        encoding="utf-8")
    assert re.search(
        rf"One search shard yields up to {_SHARD_DESIGNS} designs", html)


def test_preflight_runtime_baseline_is_the_shard_width():
    """A copy that gets DIVIDED BY, not merely quoted.

    shared/pdb_preflight_rules.py:493 sets ``runtime_baseline_designs=8`` under
    the comment ``# _SHARD_DESIGNS`` -- naming a constant that file never
    imports. runtime_estimate_min divides by it (pdb_preflight_rules.py:563),
    so a width change rescales every preflight estimate shown before a run.

    Its only other guard is a second absolute ``== 8``
    (tests/test_pdb_preflight.py:1696), whose own docstring states the equality
    in prose ("proteina's shard IS 8 designs (_SHARD_DESIGNS)") while pinning
    nothing of the sort -- the same comment-only tie this file exists to close.
    """
    from shared.pdb_preflight_rules import TOOL_RULES

    assert TOOL_RULES["proteina"].size.runtime_baseline_designs == _SHARD_DESIGNS


def test_about_runtime_row_names_the_width_it_was_measured_at():
    """A per-shard runtime band is unreadable without the shard's width.

    The band is ~9 to 15 min while the example on the same page records 57
    minutes for ITS shard; the row said only "min / shard", so the two read as
    a contradiction rather than as two different widths.

    Matched structurally. A bare ``"8-design" in typical`` was satisfied by a
    row DENYING the claim ("a 8-design figure was never measured"). The width
    must also come AFTER the band, because
    test_proteina_smoke.py::test_about_panel_table_agrees_with_preset_runtime
    compares the row's LEADING pair of numerals against PRESET_RUNTIME -- a
    width in front would be read as the band.
    """
    row = {r["preset"]: r["typical"] for r in meta.about["runtime_table"]}
    typical = row["protein_binder"]
    assert re.search(rf"/ {_SHARD_DESIGNS}-design shard\b", typical)
    leading = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", typical)][:2]
    band = str(meta.PRESET_RUNTIME["protein_binder"]["typical_minutes"])
    assert leading == [float(x) for x in re.findall(r"\d+(?:\.\d+)?", band)]


def test_worked_example_says_its_shard_is_not_one_the_form_can_launch():
    """The example's payload is a 64-design shard; the form's is 8. That gap is
    allowed -- the failure mode the example teaches does not depend on how many
    designs shared a container -- but it may not be silent, because the number
    beside "Number of designs" is one a reader will type.
    """
    width, payload = _SHARD_DESIGNS, _payload_designs()
    why = _designs_row()

    # The premise. If a re-capture ever makes the payload an ordinary
    # form-width shard, the paragraph this guards is no longer needed and this
    # test should be deleted rather than satisfied.
    assert payload != width
    assert plan_chunks("proteina", payload, "protein_binder").total_subjobs > 1

    # The split, as a shape. This is what the inverted row fails.
    assert re.search(rf"splits? into \w+ shards of {width}\b", why)
    # ... and the single-container reading denied outright, so a row that
    # merely omits the correction cannot pass by silence.
    assert re.search(rf"not one shard of {payload}\b", why)

    # The runtime reconciliation, derived from the two figures it reconciles,
    # so moving either one fails here rather than leaving a 57-minute example
    # sitting under a 9-to-15-minute band.
    assert meta.EXAMPLE["runtime"] in why
    band = str(meta.PRESET_RUNTIME["protein_binder"]["typical_minutes"])
    lo, hi = re.findall(r"\d+(?:\.\d+)?", band)
    assert f"{lo} to {hi}" in why
