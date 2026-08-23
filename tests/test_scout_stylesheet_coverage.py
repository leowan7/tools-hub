"""Scout's stylesheet must exist, must be loaded, and must still have teeth.

Scout's templates were ported into tools-hub from the standalone
``leowan7/epitope-scout`` repo; its stylesheet was not. ``static/style.css``
records the decision in its own header -- "trimmed to only what the
hub/auth/coming-soon pages need" -- and the result was that 92 Scout component
classes arrived with no rule anywhere (96 counting four applied at runtime via
``element.className``). Nothing failed. The page rendered, the tests passed,
and the defect was only visible by looking at it:

  * ``.upload-icon`` intends ``1.5rem`` -- 30px at the hub's 20px root -- and
    rendered at **1058 x 1058**, because an inline SVG with no width/height
    attribute fills its container and a square viewBox matches the height;
  * ``#pdb-file`` was never hidden, so the browser's native file button
    rendered against the dark theme;
  * ``.viewer-container`` had no height, and 3Dmol inherits its container's --
    the canvas was **2240 x 0** and the 3D viewer had never drawn a pixel.

An earlier version of this file checked only that each class *name appeared
somewhere in the CSS text*. That is not the same question, and independent QC
showed it: emptying ``.viewer-container``'s declarations, deleting the
``#pdb-file`` rule outright, or moving the height behind a 320px breakpoint all
left the suite green. Every one of those restores a defect above. So the file
now asks two separate questions --

  1. does every class the markup uses have *a rule* (cheap, broad, catches a
     missing or unlinked stylesheet);
  2. do the specific rules whose absence caused each defect still carry the
     *declaration* that fixes it (narrow, exact, catches a rule going hollow).

plus three containment checks: the templates really render the link, the file
does not restyle chrome ``style.css`` owns, and it defines no token the hub
already defines.

    pytest tests/test_scout_stylesheet_coverage.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCOUT_CSS = REPO / "static" / "scout.css"
HUB_SHEETS = [REPO / "static" / "style.css", REPO / "static" / "wallet.css"]
STYLESHEETS = HUB_SHEETS + [SCOUT_CSS]

# Globbed, not hardcoded: a third Scout template added later is covered without
# anyone remembering to list it here.
TEMPLATES = sorted((REPO / "templates" / "scout").glob("*.html"))

# Element and universal selectors. `scout.css` is linked only by the Scout
# templates, so a rule here cannot reach another page -- the risk is not
# site-wide leakage but Scout pages drifting from the rest of the site, which
# is the same risk the class half of this check covers. An element rule
# anchored to a Scout-owned class (`.data-table th`) is scoped and fine; one
# anchored to nothing, or to chrome only (`.panel-body > div`), is not. The
# port stripped the source stylesheet's html/body/main/footer rules and its
# universal reset for exactly this reason, and that decision had no guard.
LEAKY_ELEMENTS = {
    "*", "html", "body", "main", "footer", "header", "nav", "section",
    "article", "aside", "div", "span", "a", "p", "ul", "ol", "li", "button",
    "input", "select", "textarea", "label", "table", "tr", "td", "th", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "img", "svg",
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


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _rules(css: str):
    """Yield ``(media, selector, {prop: value})`` for every style rule.

    `media` is the enclosing at-rule condition, or None at top level -- the
    distinction matters, because a rule that only applies below 320px is not
    the same as one that applies everywhere, and the text-search this file used
    to do could not tell them apart.
    """
    css = _strip_comments(css)

    def walk(text: str, media):
        i, n = 0, len(text)
        while i < n:
            j, depth, quote = i, 0, None
            while j < n:
                c = text[j]
                if quote:
                    if c == "\\":
                        j += 2
                        continue
                    if c == quote:
                        quote = None
                elif c in "\"'":
                    quote = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                elif c == "{" and depth == 0:
                    break
                j += 1
            if j >= n:
                return
            prelude = " ".join(text[i:j].split())
            k, d, quote = j + 1, 1, None
            while k < n and d:
                c = text[k]
                if quote:
                    if c == "\\":
                        k += 2
                        continue
                    if c == quote:
                        quote = None
                elif c in "\"'":
                    quote = c
                elif c == "{":
                    d += 1
                elif c == "}":
                    d -= 1
                k += 1
            body = text[j + 1:k - 1]
            if prelude.startswith("@"):
                if re.match(r"@(media|supports|container|layer)\b", prelude):
                    yield from walk(body, prelude)
                # @keyframes/@font-face carry no selectors worth checking
            else:
                decls = {}
                for part in re.split(r";(?![^(]*\))", body):
                    if ":" in part:
                        prop, _, value = part.partition(":")
                        decls[" ".join(prop.split())] = " ".join(value.split())
                for sel in _split_selectors(prelude):
                    yield media, sel, decls
            i = k

    yield from walk(css, None)


def _split_selectors(prelude: str):
    """Split a selector list on top-level commas only.

    `:not(.a, .b)` and `[data-x="a,b"]` contain commas that are not list
    separators; splitting on them produces selectors that parse as something
    else entirely.
    """
    out, buf, depth, quote = [], "", 0, None
    for c in prelude:
        if quote:
            buf += c
            if c == quote:
                quote = None
            continue
        if c in "\"'":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == "," and depth == 0:
            out.append(buf.strip())
            buf = ""
        else:
            buf += c
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


def _subject(selector: str) -> str:
    """The rightmost compound selector -- what the rule actually styles.

    Splits on descendant/child/sibling combinators, but only at nesting depth
    zero: `.panel:nth-child(2n + 1)` and `.panel:not(.a > .b)` contain spaces
    and combinators inside parens, and naive splitting moves the shell class
    out of subject position -- which is a bypass, not a curiosity.
    """
    parts, buf, depth, quote = [], "", 0, None
    for c in selector:
        if quote:
            buf += c
            if c == quote:
                quote = None
            continue
        if c in "\"'":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if depth == 0 and (c.isspace() or c in ">+~"):
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
        else:
            buf += c
    if buf.strip():
        parts.append(buf.strip())
    return parts[-1] if parts else selector.strip()


def _subject_classes(selector: str) -> set[str]:
    """Classes in the subject, ignoring any nested inside :not()/:is()/etc.

    A class inside a functional pseudo is a *condition*, not the thing styled:
    `.viewer-bar:not(.panel)` styles `.viewer-bar`.
    """
    subject = _subject(selector)
    outer, depth, quote = "", 0, None
    for c in subject:
        if quote:
            if c == quote:
                quote = None
            continue
        if c in "\"'":
            quote = c
            continue
        if c in "([":
            depth += 1
            continue
        if c in ")]":
            depth -= 1
            continue
        if depth == 0:
            outer += c
    return set(re.findall(r"\.([A-Za-z][\w-]*)", outer))


def _subject_element(selector: str) -> str | None:
    """The bare tag or `*` the rule styles, if the subject is not class/id-qualified."""
    subject = _subject(selector)
    head = re.split(r"[.#:\[]", subject, maxsplit=1)[0].strip()
    return head.lower() if head else None


def _all_classes(selector: str) -> set[str]:
    """Every class anywhere in the selector, not just the subject."""
    return set(re.findall(r"\.([A-Za-z][\w-]*)", selector))


def _classes_used(html: str) -> set[str]:
    """Class tokens the markup applies, from markup and from JS.

    Covers `class="..."` attributes, `classList.add/remove/toggle`,
    `element.className = '...'`, and `setAttribute('class', '...')`. The last
    two matter: index.html applies `legend-item`, `legend-break`,
    `legend-chain` and `flag-ref-item` that way, and an attribute-only scan
    cannot see any of them -- they are the 3D-viewer legend, the same feature
    the missing stylesheet broke.

    Interpolated spans are removed rather than truncated at, so a class written
    *after* a substitution still counts. What is removed is the substitution
    itself: `'<span class="badge ' + cls + '">'` contributes `badge` and not
    `cls`, which is a JS variable.
    """
    found: set[str] = set()
    literals = re.findall(r'class\s*=\s*"([^"]*)"', html)
    literals += re.findall(r"class\s*=\s*'([^']*)'", html)
    literals += re.findall(r"className\s*=\s*['\"]([^'\"]*)['\"]", html)
    literals += re.findall(r"setAttribute\(\s*['\"]class['\"]\s*,\s*['\"]([^'\"]*)['\"]", html)
    for attr in literals:
        attr = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", attr)     # Jinja
        attr = re.sub(r"['\"]\s*\+.*?\+\s*['\"]", " ", attr)   # JS concatenation
        attr = re.sub(r"\$\{.*?\}", " ", attr)                 # template literal
        found |= {t for t in attr.split() if re.fullmatch(r"[a-zA-Z][\w-]*", t)}
    for token in re.findall(r"classList\.(?:add|remove|toggle)\('([-\w]+)'\)", html):
        found.add(token)
    return found


def _defines(css: str, cls: str) -> bool:
    """True if any selector in `css` targets `.cls` (and not `.cls-suffix`)."""
    return re.search(r"\." + re.escape(cls) + r"(?![\w-])", css) is not None


def _top_level_decls(css: str, selector: str) -> dict[str, str]:
    """Merged declarations for `selector` from rules outside any at-rule."""
    merged: dict[str, str] = {}
    for media, sel, decls in _rules(css):
        if media is None and sel == selector:
            merged.update(decls)
    return merged


_ABSOLUTE_LENGTH = re.compile(r"^(?!0+(?:\.0+)?(?:px|rem|em|vh|vw)?$)\d*\.?\d+(px|rem|em|vh|vw)$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scout_css() -> str:
    assert SCOUT_CSS.exists(), f"{SCOUT_CSS} is missing"
    return SCOUT_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def all_css() -> str:
    missing = [p.name for p in STYLESHEETS if not p.exists()]
    assert not missing, f"stylesheet(s) missing from static/: {missing}"
    joined = "\n".join(p.read_text(encoding="utf-8") for p in STYLESHEETS)
    # Comments are prose and name classes freely -- scout.css's own header
    # discusses `.btn-primary`. Leaving them in would let a class count as
    # styled because something wrote about it.
    return _strip_comments(joined)


@pytest.fixture(scope="module")
def hub_subject_classes() -> set[str]:
    """Classes the hub stylesheets style as a rule subject.

    Derived, not hardcoded. The previous version of this file pinned a
    hand-written list of 20, which omitted 13 real chrome classes -- `nav-link`
    and the whole `site-footer-*` family -- so `.site-footer { display: none }`
    in scout.css hid the footer on Scout pages with the suite green. Deriving
    it also makes the guard two-directional: it keeps working as `style.css`
    grows.
    """
    hub = "\n".join(p.read_text(encoding="utf-8") for p in HUB_SHEETS)
    return {c for _, sel, _ in _rules(hub) for c in _subject_classes(sel)}


# ---------------------------------------------------------------------------
# 1. Broad: every class has a rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_scout_class_has_a_rule(template: Path, all_css: str) -> None:
    """Catches a stylesheet that is missing, unlinked, or short a whole rule.

    This is a *name-presence* check by design -- broad and cheap. It cannot
    tell that a rule still carries the declaration that matters; that is what
    `test_the_defects_this_file_fixes_stay_fixed` is for. Do not describe this
    test as proving a class is styled.
    """
    html = template.read_text(encoding="utf-8")
    inline = "".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    available = all_css + _strip_comments(inline)

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


# ---------------------------------------------------------------------------
# 2. Narrow: the rules whose absence caused each defect still carry the fix
# ---------------------------------------------------------------------------

def test_the_defects_this_file_fixes_stay_fixed(scout_css: str) -> None:
    """One assertion per production symptom, against the declaration itself.

    Each check below corresponds to something that was actually broken on
    tools.ranomics.com. A rule going hollow, losing the one property that
    matters, or retreating behind a breakpoint reproduces the symptom while
    leaving the class name in the file -- so none of these are covered by the
    name-presence test above.
    """
    failures = []

    # The 3D viewer. 3Dmol sizes its canvas to the container, so a container
    # with no height (or a percentage height, which collapses against an
    # auto-height parent) yields a 0-height canvas and nothing renders.
    viewer = _top_level_decls(scout_css, ".viewer-container")
    height = viewer.get("height", "")
    if not _ABSOLUTE_LENGTH.match(height):
        failures.append(
            f".viewer-container needs a non-zero absolute height outside any "
            f"@media block (3Dmol inherits it; 0 means the canvas never "
            f"renders). Found: {height or 'nothing'}"
        )

    # The upload icon. An inline SVG with no width/height attribute fills its
    # container -- 1058px in production -- so the CSS must size it.
    icon = _top_level_decls(scout_css, ".upload-icon")
    for prop in ("width", "height"):
        if not _ABSOLUTE_LENGTH.match(icon.get(prop, "")):
            failures.append(
                f".upload-icon needs an absolute {prop}; the SVG carries no "
                f"{prop} attribute and fills its container without one. "
                f"Found: {icon.get(prop) or 'nothing'}"
            )

    # The native file input. Hidden behind the styled `.upload-label`; if the
    # rule goes, the browser's own grey file button renders on the dark theme.
    pdb_file = _top_level_decls(scout_css, "#pdb-file")
    hidden = pdb_file.get("display") == "none" or pdb_file.get("opacity") in {"0", "0.0"}
    if not hidden:
        failures.append(
            "#pdb-file must be hidden (opacity:0 or display:none) or the "
            f"native file button renders. Found: {pdb_file or 'no rule at all'}"
        )

    # The progress bar. Measured at height 0 in production, so the analysis ran
    # with no visible progress for its whole duration.
    track = _top_level_decls(scout_css, ".progress-track")
    if not _ABSOLUTE_LENGTH.match(track.get("height", "")):
        failures.append(
            f".progress-track needs a non-zero absolute height or the progress "
            f"bar is invisible. Found: {track.get('height') or 'nothing'}"
        )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# 3. Containment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_scout_templates_render_the_stylesheet_link(template: Path) -> None:
    """Render the template, do not grep it.

    A substring check passes when the link sits inside a `{# … #}` comment, a
    `{% if false %}`, or a block `base.html` never renders -- all of which ship
    a page with no stylesheet. Rendering is the question actually worth asking.
    """
    import os

    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    os.environ.setdefault("WEBHOOK_SWEEP_ENABLED", "0")
    from flask import render_template

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    with flask_app.test_request_context("/scout/"):
        html = render_template(f"scout/{template.name}", job_id="", epitope_id="")

    assert "static/scout.css" in html, (
        f"scout/{template.name} renders without a link to static/scout.css, so "
        f"every rule in that file is dead for this page."
    )


def test_scout_css_does_not_restyle_the_shared_chrome(
    scout_css: str, hub_subject_classes: set[str]
) -> None:
    """`scout.css` loads after `style.css`, so anything it says here wins.

    Two ways to drift Scout from the rest of the site: restyle a chrome class,
    or style an element without anchoring it to anything Scout owns. The port
    stripped the source stylesheet's `html`/`body`/`main`/`footer` rules and
    its universal reset for the second reason, and that half of the decision
    had no guard until now.
    """
    # Inline <style> in a Scout template is the same file by another route --
    # index.html carried one until this stylesheet existed, so it is a live
    # pattern, not a hypothetical one.
    sources = [("scout.css", scout_css)]
    for template in TEMPLATES:
        for block in re.findall(r"<style[^>]*>(.*?)</style>",
                                template.read_text(encoding="utf-8"), re.S):
            sources.append((f"inline <style> in {template.name}", block))

    class_offenders, element_offenders = [], []
    for origin, css in sources:
        for media, selector, _ in _rules(css):
            where = f"  [in {media}]" if media else ""
            if origin != "scout.css":
                where += f"  ({origin})"
            if selector in SHELL_STATE_EXCEPTIONS:
                continue
            if _subject_classes(selector) & hub_subject_classes:
                class_offenders.append(selector + where)
                continue
            if _subject_element(selector) in LEAKY_ELEMENTS:
                # Anchored to a class Scout owns (`.data-table th`) it is
                # scoped. Anchored to nothing, or to chrome alone
                # (`.panel-body > div`), it reaches markup this file has no
                # business restyling.
                if not (_all_classes(selector) - hub_subject_classes):
                    element_offenders.append(selector + where)

    assert not class_offenders, (
        f"scout.css restyles chrome that the hub stylesheets own: "
        f"{sorted(set(class_offenders))}. These apply on Scout pages only and "
        f"drift them from the rest of the site."
    )
    assert not element_offenders, (
        f"scout.css styles element selector(s) with no Scout class to anchor "
        f"them: {sorted(set(element_offenders))}. Qualify each with a class "
        f"this file owns, so it cannot reach the shared chrome's markup."
    )


def test_scout_css_defines_no_token_the_hub_already_owns(scout_css: str) -> None:
    """The recovered stylesheet carried a stale `:root`; it was stripped.

    Three tokens had drifted from the hub's current values (it darkened
    `--text-secondary` and `--text-tertiary`, and fixed a `'GeistMono'` typo),
    so redefining any of them here would quietly revert the hub on Scout pages.

    Checked across every selector, not just `:root`: `html { --accent: red }`
    is the same element at the same specificity and overrides just as
    effectively. New Scout-only tokens are fine -- only collisions are not.
    """
    hub_tokens = {
        prop
        for path in HUB_SHEETS
        for _, _, decls in _rules(path.read_text(encoding="utf-8"))
        for prop in decls
        if prop.startswith("--")
    }
    collisions = sorted(
        {
            f"{prop} (in `{selector}`)"
            for _, selector, decls in _rules(scout_css)
            for prop in decls
            if prop.startswith("--") and prop in hub_tokens
        }
    )
    assert not collisions, (
        f"scout.css redefines token(s) the hub already owns: {collisions}. "
        f"scout.css loads second, so these override the hub on Scout pages."
    )
