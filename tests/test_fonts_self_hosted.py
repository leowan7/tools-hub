"""Fonts must be served from this origin, not from Google's font hosts.

Mainland China is the hub's largest real audience (analytics, 28 days to
2026-11-24), and fonts.googleapis.com / fonts.gstatic.com are both blocked
there -- tested from Beijing, Shenzhen, Inner Mongolia, Heilongjiang and
Yunnan on 2026-11-24. A ``<link rel="stylesheet">`` to a blocked host is
render-blocking, so the page waited on a dead connection before first paint.

This guard is static: it scans every template, every stylesheet under
static/ and every first-party script there (static/vendor/ is excluded --
it is third-party code we do not author), so a re-added Google Fonts tag
fails here without needing a browser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")


def _sources() -> list[Path]:
    """Every template, stylesheet and first-party script -- a font link can
    be injected from any of them, not just from base.html."""
    static = ROOT / "static"
    return (
        sorted(ROOT.joinpath("templates").rglob("*.html"))
        + sorted(static.rglob("*.css"))
        + sorted(p for p in static.rglob("*.js") if "vendor" not in p.relative_to(static).parts)
    )


@pytest.mark.parametrize("host", BLOCKED_HOSTS)
def test_no_template_or_stylesheet_references_a_google_font_host(host: str) -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in _sources()
        if host in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"{host} is blocked in mainland China; still in {offenders}"


def test_every_font_face_src_in_style_css_resolves_to_a_shipped_file() -> None:
    css_path = ROOT / "static" / "style.css"
    css = css_path.read_text(encoding="utf-8")
    # src: url('fonts/<name>.woff2') -- relative to /static/, where style.css is served.
    refs = [
        line.split("url('", 1)[1].split("'", 1)[0]
        for line in css.splitlines()
        if "src:" in line and "url('" in line
    ]
    assert refs, "style.css declares no @font-face src"
    missing = [r for r in refs if not (ROOT / "static" / r).is_file()]
    assert not missing, f"@font-face points at files that are not in the repo: {missing}"
