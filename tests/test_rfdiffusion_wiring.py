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


from tools import base as tools_base
from tools import rfdiffusion as adapter_mod


def test_adapter_registered():
    """Adapter self-registers on import."""
    a = tools_base.get("rfdiffusion")
    assert a is not None
    assert a.slug == "rfdiffusion"
    assert a is adapter_mod.adapter


def test_preset_slugs_and_count():
    """One preset: pilot. Smoke + mini_pilot were removed 2026-05-29."""
    a = adapter_mod.adapter
    slugs = [p.slug for p in a.presets]
    assert slugs == ["pilot"]


def test_pilot_preset_marked_long_running_and_requires_pdb():
    """Pilot must trigger the email-on-complete UX and PDB upload field."""
    pilot = next(p for p in adapter_mod.adapter.presets if p.slug == "pilot")
    assert pilot.long_running is True
    assert pilot.requires_pdb is True


def test_modal_app_name_resolves_to_ranomics_default():
    """gpu.modal_client must dispatch to ranomics-rfdiffusion-prod by default."""
    from gpu import modal_client

    # The default resolver maps slug -> ranomics-<slug>-prod for every tool
    # (atomic + composite) post-Wave 1.
    app_name = modal_client.modal_app_name("rfdiffusion")
    assert app_name == "ranomics-rfdiffusion-prod"


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


def test_validate_rejects_empty_form():
    """No preset / no hotspots — pilot tier rejects the empty submit."""
    inputs, err = adapter_mod.validate({}, {})
    assert inputs is None
    assert err is not None


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
    """num_designs must be 1-1000: 1000 is accepted, 1001 is rejected.

    Tier-collapse PR raised the per-job cap from 200 to 1000 so users
    can run real production campaigns self-serve. The wallet $500
    hard cap remains the durable spend ceiling.
    """
    base = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "54,56,115",
        "binder_length_min": "55",
        "binder_length_max": "65",
    }
    inputs_ok, err_ok = adapter_mod.validate({**base, "num_designs": "1000"}, {})
    assert err_ok is None, err_ok
    assert inputs_ok["num_designs"] == 1000

    inputs, err = adapter_mod.validate({**base, "num_designs": "1001"}, {})
    assert inputs is None
    assert "1 and 1000" in err


# ---------------------------------------------------------------------------
# build_payload shape (matches Kendrew job_spec)
# ---------------------------------------------------------------------------


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
