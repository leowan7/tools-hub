"""Every GPU tool's runtime band reads as ONE range, on every surface.

Production rendered "~9 to 15 min to 1 to 3 min" (proteina) and "~2 min to
scales with samples x masked positions min" (iggm): the band joined the
first and last presets' free text with " to ". It is now built from each
preset's numeric ``minutes`` by shared/tool_meta.py::runtime_band.
"""

import re
import types

import pytest

from shared.feature_flags import flag_name
from shared.tool_meta import meta_for, runtime_band
from tools import base as tool_base

pytestmark = pytest.mark.usefixtures("isolate_supabase")

# "~2 to 8 min", "1 min", "~1 to 15 min (some presets vary)".
_BAND = re.compile(
    r"~?\d+(\.\d+)? (to \d+(\.\d+)? )?min( \(some presets vary\))?"
)


@pytest.fixture
def all_flags_app(monkeypatch):
    import app as app_module

    slugs = {a.slug for a in tool_base.all_adapters()}
    for slug in slugs:
        monkeypatch.setenv(flag_name(slug), "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    return app_module.create_app(), slugs


def test_every_band_is_one_numeric_range(all_flags_app):
    from blueprints.tools import _runtime_band_for_adapter
    from shared.tools_catalog import _build_tools_catalog

    flask_app, slugs = all_flags_app
    with flask_app.test_request_context("/"):
        catalog = {e["slug"]: e["runtime_band"] for e in _build_tools_catalog()}
    assert slugs <= set(catalog), sorted(slugs - set(catalog))

    bad = []
    for adapter in tool_base.all_adapters():
        catalog_band = catalog[adapter.slug]
        detail_band = _runtime_band_for_adapter(adapter, meta_for(adapter.slug))
        for surface, band in (("catalog", catalog_band), ("detail", detail_band)):
            if band.count(" to ") > 1 or not _BAND.fullmatch(band):
                bad.append((adapter.slug, surface, band))
    assert not bad, f"bands that are not one numeric range: {bad}"


def test_minutes_agree_with_the_text_beside_them():
    """``minutes`` repeats numbers already in the preset's own text."""
    drift = []
    for adapter in tool_base.all_adapters():
        meta = meta_for(adapter.slug)
        rows = [
            (slug, e.get("typical_minutes"), e["minutes"])
            for slug, e in (getattr(meta, "PRESET_RUNTIME", None) or {}).items()
            if "minutes" in e
        ] + [
            (r.get("slug"), r.get("runtime"), r["minutes"])
            for r in getattr(meta, "preset_runtime_rows", None) or ()
            if "minutes" in r
        ]
        for slug, text, (low, high) in rows:
            numbers = re.findall(r"\d+(?:\.\d+)?", str(text))
            if f"{low:g}" not in numbers or f"{high:g}" not in numbers:
                drift.append((adapter.slug, slug, text, (low, high)))
    assert not drift, drift


@pytest.mark.parametrize("minutes", [None, "9 to 15", (9,), (15, 9), (0, 3), (True, 3)])
def test_unusable_minutes_fall_back_to_one_presets_own_text(minutes):
    meta = types.SimpleNamespace(PRESET_RUNTIME={
        "a": {"typical_minutes": "~9 to 15", "minutes": minutes},
        "b": {"typical_minutes": "1 to 3", "minutes": minutes},
    })
    assert runtime_band(meta, ["a", "b"]) == "~9 to 15 min"


def test_a_preset_with_no_runtime_entry_earns_the_caveat():
    meta = types.SimpleNamespace(PRESET_RUNTIME={
        "standalone": {"typical_minutes": "5 to 10", "minutes": (5, 10)},
    })
    assert runtime_band(meta, ["standalone", "batch"]) == "5 to 10 min (some presets vary)"
    assert runtime_band(meta, ["standalone"]) == "5 to 10 min"
