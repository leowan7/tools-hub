"""Smoke tests for the Wave-4 3D hotspot picker on the BoltzGen form.

These tests exercise the parts that can run offline:

1. The boltzgen form template renders, loads the vendored NGL viewer
   script, loads the hotspot picker JS, and exposes the hotspot input
   + viewer container the client-side script binds to.
2. The BoltzGen validator keeps round-tripping the classic comma-
   separated integer string that the picker writes — i.e. the picker is
   a pure UI sugar layer, the server-side contract is unchanged.

The full interactive flow (file upload → NGL.parse → click → toggle)
requires a headless browser and is out of scope for the pytest suite.
Run this file with:

    venv/Scripts/python.exe -m pytest tests/test_hotspot_picker.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools import boltzgen as boltzgen_adapter


REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_JS = REPO_ROOT / "static" / "vendor" / "ngl.min.js"
PICKER_JS = REPO_ROOT / "static" / "js" / "hotspot_picker.js"
FORM_TEMPLATE = REPO_ROOT / "templates" / "tools" / "boltzgen_form.html"

# All four binder-design tool forms that should ship the 3D hotspot picker.
# Each takes a target PDB + hotspot residues, so the same picker pattern
# applies verbatim.
PICKER_FORM_TEMPLATES = [
    REPO_ROOT / "templates" / "tools" / "boltzgen_form.html",
    REPO_ROOT / "templates" / "tools" / "rfantibody_form.html",
    REPO_ROOT / "templates" / "tools" / "bindcraft_form.html",
    REPO_ROOT / "templates" / "tools" / "pxdesign_form.html",
]


# ---------------------------------------------------------------------------
# Static asset + template presence (cheap, no Flask test client required)
# ---------------------------------------------------------------------------


def test_ngl_is_vendored_in_static():
    """NGL ships as a vendored file so production has no CDN dependency."""
    assert VENDOR_JS.exists(), f"Expected vendored NGL at {VENDOR_JS}"
    # Sanity — file is non-trivial, looks like the real bundle.
    size = VENDOR_JS.stat().st_size
    assert size > 100_000, f"NGL bundle looks truncated ({size} bytes)"
    head = VENDOR_JS.read_bytes()[:512].decode("utf-8", errors="ignore")
    assert "NGL" in head


def test_hotspot_picker_js_exists():
    """The picker helper lives at a stable path the template references."""
    assert PICKER_JS.exists()
    body = PICKER_JS.read_text(encoding="utf-8")
    # Contract surface the form template depends on.
    assert "initHotspotPicker" in body
    assert "parseHotspots" in body
    assert "formatHotspots" in body


def test_boltzgen_form_loads_picker_assets():
    """Form template references both the vendored NGL JS and the picker JS."""
    html = FORM_TEMPLATE.read_text(encoding="utf-8")
    assert "vendor/ngl.min.js" in html
    assert "js/hotspot_picker.js" in html
    # The picker mount points must match what the JS expects.
    assert 'id="hotspot-viewer"' in html
    assert 'id="hotspot_residues"' in html
    assert 'id="target_pdb"' in html
    assert 'id="target_chain"' in html
    # Init call references every required option.
    assert "initHotspotPicker" in html


@pytest.mark.parametrize(
    "template_path",
    PICKER_FORM_TEMPLATES,
    ids=lambda p: p.stem,
)
def test_all_binder_forms_ship_picker(template_path):
    """Every binder-design form (boltzgen, rfantibody, bindcraft, pxdesign)
    must ship the 3D hotspot picker block, vendored NGL, and the picker
    JS so the pattern is consistent across tools. Regressions on any one
    form should trip this test.
    """
    assert template_path.exists(), f"Missing template: {template_path}"
    html = template_path.read_text(encoding="utf-8")

    # Vendored scripts — no CDN in production.
    assert "vendor/ngl.min.js" in html, (
        f"{template_path.name} missing vendored NGL script tag"
    )
    assert "js/hotspot_picker.js" in html, (
        f"{template_path.name} missing hotspot_picker.js script tag"
    )

    # Picker block + mount points the JS binds to.
    assert "hotspot-picker" in html, (
        f"{template_path.name} missing .hotspot-picker container"
    )
    assert 'id="hotspot-viewer"' in html
    assert 'id="hotspot-empty"' in html
    assert 'id="hotspot-surface-toggle"' in html
    assert 'id="hotspot-clear-btn"' in html

    # Form fields the picker wires up.
    assert 'id="hotspot_residues"' in html
    assert 'id="target_pdb"' in html
    assert 'id="target_chain"' in html

    # Init call wires the picker to the form's input IDs.
    assert "initHotspotPicker" in html


# ---------------------------------------------------------------------------
# Backend contract unchanged — hotspots still round-trip as "int,int,int"
# ---------------------------------------------------------------------------


def _pilot_form(**over):
    """Build a minimum valid pilot-tier form payload."""
    base = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "54,56,115",
        "binder_length_min": "50",
        "binder_length_max": "70",
        "budget": "5",
    }
    base.update(over)
    return base


def test_hotspot_input_round_trips_comma_separated_ints():
    """The picker writes "int,int,int" and the validator accepts it
    unchanged — i.e. adding the picker does not touch the server
    contract.
    """
    inputs, err = boltzgen_adapter.validate(_pilot_form(), files={})
    assert err is None, err
    assert inputs is not None
    assert inputs["hotspot_residues"] == [54, 56, 115]


def test_hotspot_input_accepts_empty_string():
    """Empty hotspot field stays valid (unconstrained run)."""
    inputs, err = boltzgen_adapter.validate(
        _pilot_form(hotspot_residues=""), files={}
    )
    assert err is None, err
    assert inputs["hotspot_residues"] == []


def test_hotspot_input_rejects_non_integer():
    """Typed non-integer tokens still fail validation (guards
    against a future picker bug writing garbage into the input).
    """
    inputs, err = boltzgen_adapter.validate(
        _pilot_form(hotspot_residues="54,notanint,115"), files={}
    )
    assert inputs is None
    assert err is not None
    assert "integer" in err.lower()


def test_hotspot_input_tolerates_whitespace_and_trailing_comma():
    """Matches the output shape of the picker's ``formatHotspots``
    (sorted, deduped, comma-joined) plus whatever the user types by
    hand.
    """
    inputs, err = boltzgen_adapter.validate(
        _pilot_form(hotspot_residues=" 54 , 56 ,115,"), files={}
    )
    assert err is None, err
    assert inputs["hotspot_residues"] == [54, 56, 115]
