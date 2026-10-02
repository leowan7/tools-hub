"""No price or time is shown before a paid run (Leo, 2026-10-01).

Sweeps every public page a visitor reads before running anything: each tool
page and its help guide rendered anonymously, the homepage, the /tools
chooser, /showcase, /pricing and the help pages.
Text a visitor can read, including the FAQPage JSON-LD, must carry no dollar
figure and no duration. Two dollar figures are allowed because they are not
run prices: the signup credit and the smallest top-up. One duration is
allowed because it is a billing rule: the auto-reload "24 hours" limit.
"""

from __future__ import annotations

import html
import re

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_PAGES = ("/", "/tools", "/showcase", "/pricing", "/help", "/help/faq",
          "/help/getting-started", "/help/troubleshooting")

_MONEY = re.compile(r"\$\s?\d")
# A bare "s" or "h" needs a space before it, so "the 40s on pLDDT" is not a
# duration and "388 s" is.
_TIME = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:-|to|–)?\s*\d*"
    r"(?:\s+[sh]|\s*(?:sec|secs|seconds?|min|mins|minutes?|hrs?|hours?))\b",
    re.IGNORECASE,
)
# The auto-reload rate limit on /pricing is a billing rule, not a run time.
_ALLOWED_TIME = ("every 24 hours", "per 24 hours", "24 hours")
_SCRIPT = re.compile(
    r"<script(?![^>]*application/ld\+json)[^>]*>.*?</script>|<style[^>]*>.*?</style>",
    re.DOTALL | re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")


def _visible(page: str) -> str:
    # Unescaped, so "3.6&nbsp;min" and "&#36;8" read as a visitor sees them.
    text = html.unescape(_TAG.sub(" ", _SCRIPT.sub(" ", page)))
    return re.sub(r"\s+", " ", text)


def _allowed_money(flask_app) -> tuple[str, ...]:
    from shared.wallet import MIN_TOPUP_USD  # noqa: PLC0415

    credit = flask_app.jinja_env.globals["signup_credit"]
    # Longest first, so "$20.00" is not left as ".00" by "$20".
    return (f"${credit(True)}", f"${credit()}",
            f"${MIN_TOPUP_USD}", f"${MIN_TOPUP_USD:.0f}")


def _hits(flask_app, text: str) -> list[str]:
    for allowed in _allowed_money(flask_app) + _ALLOWED_TIME:
        text = text.replace(allowed, "")
    found = []
    for rx in (_MONEY, _TIME):
        for m in rx.finditer(text):
            found.append(text[max(0, m.start() - 60):m.end() + 20])
    return found


def _render(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    return _visible(resp.get_data(as_text=True))


def test_every_tool_page_shows_no_price_or_time(all_tools_app):
    flask_app, slugs = all_tools_app
    client = flask_app.test_client()
    bad = {}
    for slug in slugs:
        for path in (f"/tools/{slug}", f"/help/tools/{slug}"):
            hits = _hits(flask_app, _render(client, path))
            if hits:
                bad[path] = hits
    assert not bad, bad


@pytest.mark.parametrize("path", _PAGES)
def test_public_pages_show_no_price_or_time(all_tools_app, path):
    flask_app, _slugs = all_tools_app
    hits = _hits(flask_app, _render(flask_app.test_client(), path))
    assert not hits, hits
