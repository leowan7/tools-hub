"""Every class the Scout templates use must have a rule behind it.

Scout's templates were ported into tools-hub from the standalone
``leowan7/epitope-scout`` repo; its stylesheet was not. ``static/style.css``
records the decision in its own header -- "trimmed to only what the
hub/auth/coming-soon pages need" -- and the result was that 93 Scout component
classes arrived with no rule anywhere. Nothing failed. The page rendered, the
tests passed, and the defect was only visible by looking at it: a 1058px upload
icon (``.upload-icon`` intends 1.5rem), a bare native file input where
``#pdb-file`` should be hidden, and a 3D viewer that mounted into a zero-height
``.viewer-container`` and never drew a pixel in production.

That is a whole class of defect no existing test could see, because every check
here asks Python questions and this one is a question about CSS. These tests ask
it directly:

  * every class in the Scout markup resolves to a rule in a stylesheet the page
    actually loads;
  * both Scout templates load ``scout.css``;
  * ``scout.css`` does not redefine what ``style.css`` owns -- it is loaded
    second, so anything it says about ``.navbar``, ``.panel`` or a shared design
    token would win and drift Scout pages from the rest of the site.

    pytest tests/test_scout_stylesheet_coverage.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = [
    REPO / "templates" / "scout" / "index.html",
    REPO / "templates" / "scout" / "feasibility.html",
]
# In load order. `scout.css` is last, which is what makes the leak tests below
# matter rather than being pedantry.
STYLESHEETS = [
    REPO / "static" / "style.css",
    REPO / "static" / "wallet.css",
    REPO / "static" / "scout.css",
]

# Shell classes `style.css` owns. Scout pages get them from the shared
# `base.html` chrome, so `scout.css` must not have an opinion about them.
SHELL_CLASSES = {
    "app-container", "brand", "brand-divider", "brand-logo-img", "brand-logo-link",
    "brand-tool-link", "btn-logout", "btn-primary", "btn-secondary", "hero",
    "navbar", "navbar-inner", "navbar-right", "panel", "panel-badge", "panel-body",
    "panel-header", "panel-title", "section-title", "subtitle",
}

# `scout.css` may add states the hub defines nowhere. These are additive: they
# apply only while the attribute is set, and Scout disables its Analyze button
# for the duration of a run.
SHELL_STATE_EXCEPTIONS = {".btn-primary:disabled", ".btn-secondary:disabled"}

# Classes that are markup hooks by design and correctly have no rule. Keep this
# list short and justified -- it is the escape hatch that could hide the very
# defect these tests exist to catch.
NO_RULE_BY_DESIGN = {
    # `<div class="panel feasibility-report">` -- a modifier that carries no
    # visual of its own; the panel styling comes from `.panel`.
    "feasibility-report",
    # `<p class="feasibility-note-text" style="font-size:0.7rem;...">` -- the
    # element already carries the full style inline.
    "feasibility-note-text",
}


def _classes_used(html: str) -> set[str]:
    """Class tokens the markup applies.

    Deliberately loose about where it looks: `class="..."` attributes, the
    `classList.add('x')` calls the inline JS makes, and the class strings JS
    concatenates into `innerHTML`. Tokens that are not literal class names
    (Jinja tags, JS operators, interpolated expressions) fail the fullmatch and
    drop out -- which also means a class assembled at runtime from server data,
    like `"interface-badge " + iface.interface_area`, contributes only its
    literal half. Those modifiers are covered by the leak tests, not this one.
    """
    found: set[str] = set()
    for attr in re.findall(r'class="([^"]+)"', html):
        # A JS template such as `'<span class="badge ' + cls + '">'` leaves the
        # regex capturing `badge ' + cls + '`. Only the run before the first
        # quote/operator/Jinja tag is literal, so `cls` is a JS variable, not a
        # class. Truncate there rather than trusting every whitespace token.
        literal = re.split(r"['\"+{<]", attr, maxsplit=1)[0]
        found |= {t for t in literal.split() if re.fullmatch(r"[a-zA-Z][\w-]*", t)}
    for token in re.findall(r"classList\.(?:add|remove|toggle)\('([-\w]+)'\)", html):
        found.add(token)
    return found


def _defines(css: str, cls: str) -> bool:
    """True if any selector in `css` targets `.cls` (and not `.cls-suffix`)."""
    return re.search(r"\." + re.escape(cls) + r"(?![\w-])", css) is not None


@pytest.fixture(scope="module")
def css() -> str:
    missing = [p.name for p in STYLESHEETS if not p.exists()]
    assert not missing, f"stylesheet(s) missing from static/: {missing}"
    joined = "\n".join(p.read_text(encoding="utf-8") for p in STYLESHEETS)
    # Comments are prose and name classes freely -- scout.css's own header
    # discusses `.btn-primary`. Leaving them in would let a class count as
    # styled because something wrote about it.
    return re.sub(r"/\*.*?\*/", "", joined, flags=re.S)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_scout_class_has_a_rule(template: Path, css: str) -> None:
    """The check that would have caught the missing stylesheet."""
    html = template.read_text(encoding="utf-8")
    # A template may still carry an inline <style>; count it as coverage.
    inline = "".join(re.findall(r"<style>(.*?)</style>", html, re.S))
    available = css + inline

    unstyled = sorted(
        c
        for c in _classes_used(html)
        if c not in NO_RULE_BY_DESIGN and not _defines(available, c)
    )
    assert not unstyled, (
        f"{template.name} uses {len(unstyled)} class(es) with no CSS rule in "
        f"{[p.name for p in STYLESHEETS]}: {unstyled}. Either style them, or -- "
        f"if a class is a markup hook that needs no rule -- add it to "
        f"NO_RULE_BY_DESIGN with a reason."
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_scout_templates_link_the_scout_stylesheet(template: Path) -> None:
    """Coverage above is worthless if the page never loads the file."""
    html = template.read_text(encoding="utf-8")
    linked = "filename='scout.css'" in html or 'filename="scout.css"' in html
    assert linked, (
        f"{template.name} does not link static/scout.css, so every rule in it "
        f"is dead for this page."
    )


def test_scout_css_does_not_restyle_the_shared_chrome() -> None:
    """`scout.css` loads after `style.css`, so a shell rule here would win.

    Scoped to the rule *subject* (the rightmost compound selector): a rule like
    `.panel-body .data-table` is a Scout rule that happens to mention a shell
    class, and is fine. `.panel-body` on its own is not.
    """
    source = (REPO / "static" / "scout.css").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", source, flags=re.S)

    offenders = []
    for prelude in re.findall(r"([^{}]+)\{", body):
        for selector in prelude.split(","):
            selector = " ".join(selector.split())
            if not selector or selector.startswith("@") or selector.endswith("%"):
                continue
            if selector in SHELL_STATE_EXCEPTIONS:
                continue
            subject = re.split(r"\s*[>+~]\s*|\s+", selector)[-1]
            if any(c in SHELL_CLASSES for c in re.findall(r"\.([A-Za-z][\w-]*)", subject)):
                offenders.append(selector)

    assert not offenders, (
        f"scout.css restyles chrome that style.css owns: {sorted(set(offenders))}. "
        f"These would apply on Scout pages only and drift them from the rest of "
        f"the site."
    )


def test_scout_css_defines_no_design_tokens_the_hub_already_owns() -> None:
    """The recovered stylesheet carried a stale `:root`; only `--info` survives.

    Three tokens had drifted from the hub's current values (it darkened
    `--text-secondary` and `--text-tertiary`, and fixed a `'GeistMono'` typo),
    so redefining them here would quietly revert the hub on Scout pages.
    """

    def tokens(path: Path) -> dict[str, str]:
        text = path.read_text(encoding="utf-8")
        return {
            name: value.strip()
            for block in re.findall(r":root\s*\{(.*?)\}", text, re.S)
            for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)
        }

    hub = tokens(REPO / "static" / "style.css")
    scout = tokens(REPO / "static" / "scout.css")

    clashes = sorted(set(scout) & set(hub))
    assert not clashes, (
        f"scout.css redefines token(s) style.css already owns: {clashes}. "
        f"scout.css loads second, so these override the hub on Scout pages."
    )
    assert set(scout) == {"--info"}, (
        f"scout.css should carry exactly one token the hub lacks (--info); "
        f"found {sorted(scout)}"
    )
