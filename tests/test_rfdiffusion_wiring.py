"""Wiring sanity tests for the RFdiffusion tool adapter.

Mirrors the structure of test_pxdesign_* and test_rfantibody_* tests:
verifies the adapter is registered, the preset list and credits match
``meta.preset_runtime_rows``, the Kendrew Modal app name resolves
correctly, and the validate / build_payload functions handle the
documented form shapes.

Run with::

    venv/Scripts/python.exe -m pytest tests/test_rfdiffusion_wiring.py -v
"""

from __future__ import annotations

import pytest

from tools import base as tools_base
from tools import rfdiffusion as adapter_mod
from tools.rfdiffusion import meta as adapter_meta


def test_adapter_registered():
    """Adapter self-registers on import."""
    a = tools_base.get("rfdiffusion")
    assert a is not None
    assert a.slug == "rfdiffusion"
    assert a is adapter_mod.adapter


def test_preset_slugs_and_count():
    """Three presets: smoke, mini_pilot, pilot."""
    a = adapter_mod.adapter
    slugs = [p.slug for p in a.presets]
    assert slugs == ["smoke", "mini_pilot", "pilot"]


def test_preset_credits_match_meta_table():
    """Credit costs in adapter must match meta.preset_runtime_rows."""
    by_slug = {p.slug: p for p in adapter_mod.adapter.presets}
    for row in adapter_meta.preset_runtime_rows:
        slug = row["slug"]
        expected_credits = int(row["credits"])
        assert by_slug[slug].credits_cost == expected_credits, (
            f"Mismatch for {slug}: adapter has {by_slug[slug].credits_cost}, "
            f"meta has {expected_credits}"
        )


def test_pilot_preset_marked_long_running_and_requires_pdb():
    """Pilot must trigger the email-on-complete UX and PDB upload field."""
    pilot = next(p for p in adapter_mod.adapter.presets if p.slug == "pilot")
    assert pilot.long_running is True
    assert pilot.requires_pdb is True


def test_modal_app_name_resolves_to_kendrew_default():
    """gpu.modal_client must dispatch to kendrew-rfdiffusion-prod by default."""
    from gpu import modal_client

    # The default resolver maps slug -> kendrew-<slug>-prod for composite
    # tools (BindCraft, BoltzGen, RFantibody, PXDesign, RFdiffusion).
    # Atomic tools (MPNN, AF2, COLABFOLD, ESMFOLD) override to
    # ranomics-<slug>-prod.
    app_name = modal_client.modal_app_name("rfdiffusion")
    assert app_name == "kendrew-rfdiffusion-prod"


def test_preset_caps_present_for_all_tiers():
    """gpu.modal_client.PRESET_CAPS must define caps for every tier
    the adapter exposes, so submit doesn't fall through to 0."""
    from gpu.modal_client import PRESET_CAPS

    for preset in adapter_mod.adapter.presets:
        cap = PRESET_CAPS.get(("rfdiffusion", preset.slug))
        assert cap and cap > 0, (
            f"Missing or zero PRESET_CAPS for ('rfdiffusion', {preset.slug!r})"
        )


# ---------------------------------------------------------------------------
# Validate / build_payload contract
# ---------------------------------------------------------------------------


def test_validate_rejects_missing_preset():
    inputs, err = adapter_mod.validate({}, {})
    assert inputs is None
    assert err is not None
    assert "preset" in err.lower()


def test_validate_smoke_returns_baked_target():
    inputs, err = adapter_mod.validate({"preset": "smoke"}, {})
    assert err is None
    assert inputs["preset"] == "smoke"
    assert "PD-L1" in inputs["target"]


def test_validate_mini_pilot_returns_baked_target():
    inputs, err = adapter_mod.validate({"preset": "mini_pilot"}, {})
    assert err is None
    assert inputs["preset"] == "mini_pilot"


def test_validate_pilot_requires_hotspots():
    form = {
        "preset": "pilot",
        "target_chain": "A",
        "binder_length_min": "55",
        "binder_length_max": "65",
        "num_designs": "2",
        "hotspot_residues": "",
    }
    inputs, err = adapter_mod.validate(form, {})
    assert inputs is None
    assert "hotspot" in err.lower()


def test_validate_pilot_parses_hotspots_as_ints():
    form = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "54, 56, 115",
        "binder_length_min": "55",
        "binder_length_max": "65",
        "num_designs": "2",
    }
    inputs, err = adapter_mod.validate(form, {})
    assert err is None
    assert inputs["hotspot_residues"] == [54, 56, 115]


def test_validate_pilot_rejects_non_integer_hotspot():
    form = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "54,notanint,115",
        "binder_length_min": "55",
        "binder_length_max": "65",
        "num_designs": "2",
    }
    inputs, err = adapter_mod.validate(form, {})
    assert inputs is None
    assert "integer" in err.lower()


def test_validate_pilot_rejects_bad_binder_length_range():
    form = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "54,56,115",
        "binder_length_min": "100",
        "binder_length_max": "60",
        "num_designs": "2",
    }
    inputs, err = adapter_mod.validate(form, {})
    assert inputs is None
    assert "min" in err.lower() and "max" in err.lower()


def test_validate_pilot_clamps_num_designs():
    """num_designs must be 1-5; 6 is rejected."""
    form = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "54,56,115",
        "binder_length_min": "55",
        "binder_length_max": "65",
        "num_designs": "6",
    }
    inputs, err = adapter_mod.validate(form, {})
    assert inputs is None
    assert "1 and 5" in err


# ---------------------------------------------------------------------------
# build_payload shape (matches Kendrew job_spec)
# ---------------------------------------------------------------------------


def test_build_payload_smoke_sets_skip_af2_true():
    inputs = {"preset": "smoke", "target": "(baked)"}
    payload = adapter_mod.build_payload(inputs, presigned_url="")
    assert payload["target_chain"] == "A"
    assert payload["parameters"]["skip_af2"] is True
    assert payload["parameters"]["num_designs"] == 1


def test_build_payload_mini_pilot_sets_skip_af2_false():
    inputs = {"preset": "mini_pilot", "target": "(baked)"}
    payload = adapter_mod.build_payload(inputs, presigned_url="")
    assert payload["parameters"]["skip_af2"] is False
    assert payload["parameters"]["num_designs"] == 2


def test_build_payload_pilot_forwards_caller_fields():
    inputs = {
        "preset": "pilot",
        "target_chain": "B",
        "hotspot_residues": [10, 20, 30],
        "binder_length": {"min": 60, "max": 80},
        "num_designs": 3,
        "target": "(uploaded)",
    }
    payload = adapter_mod.build_payload(inputs, presigned_url="https://x.test/upload.pdb")
    assert payload["target_chain"] == "B"
    assert payload["hotspot_residues"] == [10, 20, 30]
    assert payload["parameters"]["num_designs"] == 3
    assert payload["parameters"]["binder_length"] == {"min": 60, "max": 80}
    assert payload["parameters"]["skip_af2"] is False
