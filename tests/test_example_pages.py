"""Public /examples pages, behind EXAMPLE_PAGES_ENABLED (off by default)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_no_prerun_numbers import _hits, _visible

pytestmark = pytest.mark.usefixtures("isolate_supabase")

ROOT = Path(__file__).resolve().parents[1]


def _example_slugs(client) -> list[str]:
    page = client.get("/examples").get_data(as_text=True)
    return re.findall(r'href="/examples/([a-z0-9_-]+)"', page)


def test_flag_off_hides_routes_and_links(all_tools_app, monkeypatch):
    flask_app, _slugs = all_tools_app
    monkeypatch.delenv("EXAMPLE_PAGES_ENABLED", raising=False)
    client = flask_app.test_client()
    assert client.get("/examples").status_code == 404
    assert client.get("/examples/rfd4").status_code == 404
    assert "/examples/" not in client.get("/tools/rfd4").get_data(as_text=True)


def test_flag_on_renders_every_example_without_job_links_or_prerun_numbers(all_tools_app, monkeypatch):
    flask_app, _slugs = all_tools_app
    monkeypatch.setenv("EXAMPLE_PAGES_ENABLED", "1")
    client = flask_app.test_client()
    slugs = _example_slugs(client)
    assert len(slugs) >= 14, slugs
    bad = {}
    for slug in slugs:
        resp = client.get(f"/examples/{slug}")
        assert resp.status_code == 200, slug
        page = resp.get_data(as_text=True)
        job_links = re.findall(r'href="[^"]*(?:/jobs/|/api/jobs/)[^"]*"', page)
        hits = _hits(flask_app, _visible(page))
        if job_links or hits:
            bad[slug] = (job_links, hits)
        assert f'href="/examples/{slug}"' in client.get(f"/tools/{slug}").get_data(as_text=True)
    assert not bad, bad


def test_unknown_tool_404s(all_tools_app, monkeypatch):
    flask_app, _slugs = all_tools_app
    monkeypatch.setenv("EXAMPLE_PAGES_ENABLED", "1")
    assert flask_app.test_client().get("/examples/not-a-tool").status_code == 404


def test_structures_are_viewable_and_never_downloadable(all_tools_app, monkeypatch):
    """The viewer stays, the download goes.

    Leo, 2026-11-27: "we don't let people download the structure". Each
    committed file must still be reachable by the viewer, which loads it
    by URL, and no download control may name it. The file itself is still
    served from /static/example/, so this asserts the page makes no
    offer, not that the bytes are unreachable.
    """
    flask_app, _slugs = all_tools_app
    monkeypatch.setenv("EXAMPLE_PAGES_ENABLED", "1")
    page = flask_app.test_client().get("/examples/rfd4").get_data(as_text=True)
    files = sorted(p.name for p in (ROOT / "static" / "example" / "rfd4").glob("*.cif"))
    assert files
    for name in files:
        assert f'data-example-view="/static/example/rfd4/{name}"' in page
    assert "js/mol_viewer.js" in page
    assert "data-example-download" not in page
    assert "download>" not in page and "download " not in page.split("<body")[-1].lower().replace("downloaded", "")


def test_every_example_structure_names_a_design_in_its_payload():
    """A committed file must belong to that tool's example run."""
    import json  # noqa: PLC0415

    for folder in (ROOT / "static" / "example").iterdir():
        if not folder.is_dir():
            continue
        payload = (ROOT / "tools" / folder.name.replace("-", "_") / "example" / "result.json").read_text(encoding="utf-8")
        data = json.loads(payload)
        rows = data.get("candidates") or data.get("designs") or []
        keys = {Path(r["pdb_key"]).name for r in rows if isinstance(r, dict) and r.get("pdb_key")}
        for f in folder.iterdir():
            if f.suffix in (".pdb", ".cif") and keys:
                assert f.name in keys, (folder.name, f.name)


def test_new_event_types_are_registered():
    from shared.events import USER_EVENT_TYPES  # noqa: PLC0415

    js = (ROOT / "static" / "js" / "track.js").read_text(encoding="utf-8")
    for event in ("example_view",):
        assert event in USER_EVENT_TYPES
        assert f'"{event}"' in js


def test_inline_structure_example_gets_one_viewer_and_no_download(all_tools_app, monkeypatch):
    """AF2's payload carries its own structure, so its partial renders the
    only viewer and the structures panel does not render at all."""
    flask_app, _slugs = all_tools_app
    monkeypatch.setenv("EXAMPLE_PAGES_ENABLED", "1")
    page = flask_app.test_client().get("/examples/af2").get_data(as_text=True)
    assert page.count('id="af2-ngl-viewer"') == 1
    assert 'id="example-viewer-0"' not in page
    assert "ubiquitin_human_unrelaxed_rank_001_alphafold2_ptm_model_1_seed_000.pdb" not in page
    assert "data-example-download" not in page
