"""Run the REAL hotspot picker, from the REAL rendered form, under node.

``tests/test_hotspot_picker.py`` asserts that each form contains the string
``initHotspotPicker``. That cannot see whether a form passes ``chainPrefixed``,
because the flag lives inside an object literal — and ``chainPrefixed`` is the
whole difference between a picker that works on a multi-chain target and one
that is inert on it. Nothing in ``tests/`` mentions ``chainPrefixed`` today.

So this renders each tool form through its real Flask route, pulls the inline
``<script>`` the page actually ships, and executes it against the real
``static/js/hotspot_picker.js`` in a stubbed DOM (``tests/js/``). Assertions are
on emitted behaviour, not on source text.

Three bugs are pinned, all of them from
``docs/HANDOFF-2026-08-07-multichain-finish.md`` item 1a:

* on a multi-chain target the picker is INERT — ``_chains()`` returns the
  literal ``["A,B"]``, the NGL selection ``:A,B`` matches nothing and the chain
  gate throws away every click;
* a click is attributed to the wrong chain, so ``B264`` is recorded as if it
  were on chain A;
* a click OVERWRITES chain-prefixed hotspots the user typed, because
  ``parseHotspots`` runs ``parseInt("A296")``, gets NaN and silently drops the
  token — the user watches their hotspots disappear.

WHAT A CLICK IS, HERE. It is a pickingProxy handed to the handler the picker
itself registered on the viewer's ``clicked`` signal — not a direct call to
``_toggleResidue``. The harness used to do the latter and to apply its own copy
of the chain gate first, which meant the gate in ``hotspot_picker.js`` could be
deleted entirely with every test in this file still green. The harness now
knows none of the gate's rules and only reports whether the hotspot field
moved; ``test_a_click_outside_the_named_chains_is_thrown_away`` and its
bare-int sibling are what hold the gate up.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REPO_ROOT = Path(__file__).resolve().parent.parent
PICKER_JS = REPO_ROOT / "static" / "js" / "hotspot_picker.js"
# .cjs, not .js: the PARENT directory of this repo carries a package.json with
# "type": "module", which makes node treat a bare .js as ESM and reject
# require(). The extension is the fix node itself recommends, and it keeps the
# harness self-contained — adding a package.json here to opt out would be a new
# build file in a repo that deliberately has none.
HARNESS_JS = REPO_ROOT / "tests" / "js" / "hotspot_picker_harness.cjs"

# Inline <script> blocks only — the src= ones are separate elements and the
# non-greedy body match cannot span them.
_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)

# Which forms ask the picker for chain-prefixed tokens.
#
# rfantibody is OFF on purpose and must stay off: tools/rfantibody/__init__.py
# parses hotspots with a bare int() and rejects "A25" outright, and
# llm-proteinDesigner/docker/rfantibody/run_pipeline.py:1391 blindly prepends
# the chain, turning ["A25"] into "--hotspots AA25". It is also
# multi_chain_supported=False upstream (a VHH binds one chain).
#
# bindcraft is OFF pending verification, not on principle. Its container
# forwards the token verbatim (docker/bindcraft/run_pipeline.py:426) to a
# prebuilt image (kendrew-bindcraft:v7) whose parser is not vendored in either
# repo, and bindcraft is the one binder tool with no smoke tier — the only way
# to test it is a full paid pilot. It is already gated
# multi_chain_container_ready=False, so the flag buys it the least.
CHAIN_PREFIXED_FORMS = ("rfdiffusion", "pxdesign", "boltzgen", "proteina")
BARE_INT_FORMS = ("rfantibody", "bindcraft")
ALL_PICKER_FORMS = CHAIN_PREFIXED_FORMS + BARE_INT_FORMS

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)


@pytest.fixture(scope="module")
def flask_app():
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _form_page(app, slug: str) -> dict:
    """The rendered form page for ``slug``, and its inline picker ``<script>``.

    The PAGE goes to the harness as well as the script: ``getElementById``
    there answers only for ids the page really carries, so a form that mounts
    the picker on an element that does not exist fails instead of passing on an
    auto-created stub.
    """
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    ctx = SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com"
    )
    with patch("blueprints.tools.load_user_context", return_value=ctx), patch(
        "blueprints.tools.tool_enabled", return_value=True
    ), patch(
        "blueprints.tools.get_or_create_wallet",
        return_value={"balance_usd": "100", "wallet_frozen": False},
    ):
        resp = client.get(f"/tools/{slug}")
    assert resp.status_code == 200, f"/tools/{slug} -> {resp.status_code}"
    html = resp.get_data(as_text=True)
    blocks = [b for b in _SCRIPT_RE.findall(html) if "initHotspotPicker" in b]
    assert len(blocks) == 1, (
        f"{slug}: expected exactly one inline picker script, got {len(blocks)}"
    )
    return {"html": html, "script": blocks[0]}


def _drive(page: dict, *, chain="", hotspots="", clicks=()) -> dict:
    scenario = {
        "pickerJs": str(PICKER_JS),
        "pageHtml": page["html"],
        "formScript": page["script"],
        "chain": chain,
        "hotspots": hotspots,
        "clicks": list(clicks),
    }
    proc = subprocess.run(
        ["node", str(HARNESS_JS)],
        input=json.dumps(scenario),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.stdout, f"harness produced no stdout; stderr:\n{proc.stderr}"
    out = json.loads(proc.stdout)
    assert out["ok"], out.get("error")
    return out


@pytest.fixture(scope="module")
def scripts(flask_app):
    return {slug: _form_page(flask_app, slug) for slug in ALL_PICKER_FORMS}


# ---------------------------------------------------------------------------
# The harness itself has to be able to see a difference
# ---------------------------------------------------------------------------

def test_every_picker_call_site_is_covered_by_this_file():
    """A completeness check, not a behaviour one — the only thing a source
    scan is actually good for here.

    Every template that constructs a picker has to appear in one of the two
    lists above, so adding a form is a decision about ``chainPrefixed`` rather
    than an accident. There used to be a seventh call site,
    ``templates/components/hotspot_picker.html`` — a shared macro that nothing
    imported and that emitted the opts block WITHOUT the flag. It was deleted
    with this change: the first person to "de-duplicate the six forms onto the
    shared macro" would have silently reverted the flag on all of them, and no
    test could have caught it, because nothing rendered it.
    """
    templates = REPO_ROOT / "templates"
    call_sites = {
        p.stem.replace("_form", "")
        for p in templates.rglob("*.html")
        if "initHotspotPicker(" in p.read_text(encoding="utf-8")
    }
    assert call_sites == set(ALL_PICKER_FORMS), (
        f"picker call sites {sorted(call_sites)} do not match the forms this "
        f"file drives {sorted(ALL_PICKER_FORMS)} — a new form must be added to "
        f"CHAIN_PREFIXED_FORMS or BARE_INT_FORMS with a reason"
    )


@needs_node
def test_harness_reports_the_flag_each_form_actually_passes(scripts):
    """Guard the guard. If this ever reports the same value for every form,
    the harness has stopped reading the opts object and every assertion below
    is vacuous."""
    seen = {
        slug: _drive(scripts[slug], chain="A")["chainPrefixed"]
        for slug in ALL_PICKER_FORMS
    }
    assert seen == {
        **{s: True for s in CHAIN_PREFIXED_FORMS},
        **{s: False for s in BARE_INT_FORMS},
    }


@needs_node
@pytest.mark.parametrize("slug", ALL_PICKER_FORMS)
def test_every_form_builds_a_picker_with_the_expected_mount_points(slug, scripts):
    out = _drive(scripts[slug], chain="A")
    assert out["opts"]["hotspotInputId"] == "hotspot_residues"
    assert out["opts"]["chainInputId"] == "target_chain"
    assert out["opts"]["pdbInputId"] == "target_pdb"


@needs_node
@pytest.mark.parametrize("slug", ALL_PICKER_FORMS)
def test_every_form_mounts_a_picker_that_actually_comes_alive(slug, scripts):
    """The ids in ``opts`` have to NAME REAL ELEMENTS, which the assertion above
    cannot see: it reads the literal the form wrote, not the page it wrote it
    on.

    ``HotspotPicker.init`` returns at the first line on a null pdb input,
    hotspot input or viewer element, so a form pointing ``viewerId`` at an
    element that does not exist builds a picker that registers no listeners,
    never loads a structure, and silently accepts no clicks — every other
    assertion in this file still passes, because ``_chains``/``_chainSel`` are
    pure functions of the chain field. The observable difference is that the
    picker hands the viewer a click handler only if it mounted.
    """
    out = _drive(scripts[slug], chain="A")
    assert out["stagesBuilt"] == 1, (
        f"{slug}: the picker built no NGL stage — it mounted on nothing"
    )
    assert out["clickHandlers"] == 1, (
        f"{slug}: the picker registered {out['clickHandlers']} click handlers; "
        f"with none, no pick can ever reach the hotspot field"
    )


# ---------------------------------------------------------------------------
# Multi-chain targets: the picker must not be inert
# ---------------------------------------------------------------------------

@needs_node
@pytest.mark.parametrize("slug", CHAIN_PREFIXED_FORMS)
def test_multi_chain_target_is_selectable(slug, scripts):
    """``:A,B`` is not valid NGL and matches nothing, so with the flag off the
    viewer renders an empty target and every click is rejected."""
    out = _drive(scripts[slug], chain="A,B")
    assert out["chains"] == ["A", "B"]
    assert out["chainSel"] == "(:A or :B)"


@needs_node
@pytest.mark.parametrize("slug", CHAIN_PREFIXED_FORMS)
def test_a_click_on_the_second_chain_is_accepted_and_keeps_its_chain(
    slug, scripts
):
    """The chain-attribution assertion. A chain-blind picker records 264 with
    no chain, or attributes it to A — both indistinguishable from correct
    unless the click lands on the SECOND chain."""
    out = _drive(
        scripts[slug], chain="A,B", clicks=[{"resno": 264, "chain": "B"}]
    )
    assert out["ignoredClicks"] == []
    assert out["field"] == "B264"


@needs_node
@pytest.mark.parametrize("slug", CHAIN_PREFIXED_FORMS)
def test_clicks_across_both_chains_stay_apart(slug, scripts):
    """Identical residue numbers on two protomers — the Fc homodimer case,
    where a chain-blind implementation collapses both to one token."""
    out = _drive(scripts[slug], chain="A,B", clicks=[
        {"resno": 296, "chain": "A"},
        {"resno": 296, "chain": "B"},
    ])
    assert out["field"] == "A296,B296"


@needs_node
@pytest.mark.parametrize("slug", BARE_INT_FORMS)
def test_bare_int_forms_still_emit_bare_ints(slug, scripts):
    """rfantibody and bindcraft must keep posting what their adapters parse."""
    out = _drive(scripts[slug], chain="A", clicks=[{"resno": 54, "chain": "A"}])
    assert out["field"] == "54"


# ---------------------------------------------------------------------------
# The chain gate, in both directions
# ---------------------------------------------------------------------------
#
# The accept direction is above (``ignoredClicks == []``). Only the REJECT
# direction can fail if the gate is gone, and until this pair existed the gate
# could be deleted from hotspot_picker.js outright with 65 tests still green —
# because the harness carried its own copy of it and the tests were reading
# that copy back. The harness now registers the picker's real handler and only
# reports whether the hotspot field moved, so these two are the tests that hold
# the gate up.

@needs_node
@pytest.mark.parametrize("slug", CHAIN_PREFIXED_FORMS)
def test_a_click_outside_the_named_chains_is_thrown_away(slug, scripts):
    """A pick on a chain the target field does not name is not the user's
    target. Accepting it writes a hotspot upstream cannot match — proteina
    matches chain+resnum, so "C77" against a target staged as A and B simply
    never binds to anything — and it does it invisibly, because the token looks
    exactly like a good one in the field."""
    out = _drive(
        scripts[slug], chain="A,B", clicks=[{"resno": 77, "chain": "C"}]
    )
    assert out["field"] == "", (
        f"{slug}: a click on chain C was recorded against a target that names "
        f"only A and B"
    )
    assert out["ignoredClicks"] == [{"resno": 77, "chain": "C"}]


@needs_node
@pytest.mark.parametrize("slug", BARE_INT_FORMS)
def test_bare_int_forms_also_throw_away_a_click_off_the_target_chain(
    slug, scripts
):
    """The same gate, on the single-chain side. Here it matters MORE: the token
    these forms emit carries no chain at all, so a pick on chain B is recorded
    as a bare "77" and reads downstream as residue 77 of chain A."""
    out = _drive(scripts[slug], chain="A", clicks=[{"resno": 77, "chain": "B"}])
    assert out["field"] == "", (
        f"{slug}: a click on chain B became an unlabelled hotspot on chain A"
    )
    assert out["ignoredClicks"] == [{"resno": 77, "chain": "B"}]


# ---------------------------------------------------------------------------
# Typed hotspots must survive a click
# ---------------------------------------------------------------------------

@needs_node
@pytest.mark.parametrize("slug", CHAIN_PREFIXED_FORMS)
def test_a_click_does_not_destroy_typed_prefixed_hotspots(slug, scripts):
    """The silent-data-loss bug: ``parseHotspots`` runs ``parseInt("A296")``,
    gets NaN, drops the token, and rewrites the field from what survived. The
    user sees their hotspots vanish on an unrelated click."""
    out = _drive(
        scripts[slug], chain="A", hotspots="A296",
        clicks=[{"resno": 54, "chain": "A"}],
    )
    assert "A296" in out["field"], out["field"]
    assert out["field"] == "A54,A296"


@needs_node
@pytest.mark.parametrize("slug", CHAIN_PREFIXED_FORMS)
def test_clicking_a_typed_hotspot_removes_it_rather_than_duplicating(
    slug, scripts
):
    """Toggle semantics survive the prefix: a bare token already in the field
    matches a click on the default chain (hotspot_picker.js:334-338)."""
    out = _drive(
        scripts[slug], chain="A", hotspots="54",
        clicks=[{"resno": 54, "chain": "A"}],
    )
    assert out["field"] == ""


@needs_node
@pytest.mark.parametrize("slug", CHAIN_PREFIXED_FORMS)
def test_single_chain_typed_bare_ints_are_preserved_across_a_click(
    slug, scripts
):
    """Nothing the user typed is discarded. The picker does not rewrite bare
    tokens it did not create, so the field ends up MIXED — "54,56,A115"."""
    out = _drive(
        scripts[slug], chain="A", hotspots="54,56",
        clicks=[{"resno": 115, "chain": "A"}],
    )
    assert out["field"] == "54,56,A115"


# ---------------------------------------------------------------------------
# The seam: what the picker EMITS must be what the server PARSES
# ---------------------------------------------------------------------------
#
# tests/test_multichain_targets.py:234-237 records why this matters: every
# earlier test checked one side of a seam — the adapter emits "A,B" (true) and
# the shared parsers accept "A B" (also true) — and nothing asserted that the
# emitted form is an accepted form. The picker is the same shape of seam, one
# layer further out: it writes the string the browser posts.

@needs_node
@pytest.mark.parametrize("slug", CHAIN_PREFIXED_FORMS)
@pytest.mark.parametrize("chain,typed,clicks,expected", [
    # single chain, pure click: the historical case
    ("A", "", [{"resno": 54, "chain": "A"}], ["A54"]),
    # single chain, typed bare + click: mixed field, one meaning
    ("A", "54,56", [{"resno": 115, "chain": "A"}], ["A54", "A56", "A115"]),
    # two chains, one click each — the Fc case, same resnum on both protomers
    ("A,B", "", [{"resno": 296, "chain": "A"}, {"resno": 296, "chain": "B"}],
     ["A296", "B296"]),
    # two chains, typed prefixed survives a click on the other protomer
    ("A,B", "A296", [{"resno": 264, "chain": "B"}], ["A296", "B264"]),
])
def test_the_emitted_field_parses_to_the_residues_that_were_clicked(
    slug, scripts, chain, typed, clicks, expected
):
    from tools.base import parse_hotspot_residues, parse_target_chains

    out = _drive(scripts[slug], chain=chain, hotspots=typed, clicks=clicks)
    residues, err = parse_hotspot_residues(
        out["field"], parse_target_chains(chain)
    )
    assert err is None, f"{slug}: server refused the picker's own output: {err}"
    assert sorted(residues) == sorted(expected), (
        f"{slug}: picker wrote {out['field']!r}, server read it as {residues!r}"
    )
