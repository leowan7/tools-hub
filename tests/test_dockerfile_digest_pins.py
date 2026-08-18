"""Every GPU base image must be pinned by DIGEST, not by tag.

A tag is a mutable pointer. Upstream can overwrite it in place, and the next
``modal deploy`` then builds a different image with nothing in this repo having
changed — no diff, no review, no signal. The deploy workflow rebuilds every app
on any ``tools/**`` push, so that window is open on every merge, not just on a
deliberate image bump.

This is not hypothetical here: an unpinned transitive dep (dm-haiku) silently
moved under the proteina image and broke every design run on the next rebuild.
Tag pins are the same hazard one level up.

To pin or bump a base image::

    docker buildx imagetools inspect <image>:<tag>

and write ``FROM <image>:<tag>@sha256:...`` — keep the tag for readability, the
digest is what actually resolves. Re-verify the image before bumping; a digest
bump is a build change and belongs in its own commit.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "tools"
_DOCKERFILES = sorted(_TOOLS_DIR.glob("*/Dockerfile.modal"))

# `FROM ref [AS stage]`. Deliberately anchored at column 0 and case-SENSITIVE:
# these Dockerfiles embed Python in `RUN python3 -c "..."` continuations, and a
# lenient pattern reads `from transformers import ...` as an instruction. Docker
# does accept lowercase `from`, so the miss direction is guarded by the
# "has no FROM instruction" assertion below rather than by this regex.
_FROM_RE = re.compile(r"^FROM\s+(\S+)")
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


def test_dockerfiles_are_discovered():
    """A glob that silently matches nothing would make every test below vacuous."""
    assert len(_DOCKERFILES) >= 8, (
        f"expected at least 8 tools/*/Dockerfile.modal, found {len(_DOCKERFILES)}: "
        f"{[str(p) for p in _DOCKERFILES]}"
    )


@pytest.mark.parametrize("path", _DOCKERFILES, ids=lambda p: p.parent.name)
def test_base_image_is_digest_pinned(path):
    refs = [
        (n, m.group(1))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if (m := _FROM_RE.match(line))
    ]
    assert refs, f"{path} has no FROM instruction"

    unpinned = [(n, ref) for n, ref in refs if not _DIGEST_RE.search(ref)]
    assert not unpinned, (
        f"{path.parent.name}/Dockerfile.modal pins its base image by tag at "
        f"{', '.join(f'line {n}: {ref!r}' for n, ref in unpinned)}. A tag can be "
        "retagged upstream, which changes this image on the next rebuild with no "
        "diff in this repo. Resolve with `docker buildx imagetools inspect <ref>` "
        "and pin `<image>:<tag>@sha256:...`."
    )


# ---------------------------------------------------------------------------
# esmfold2_design has NO Dockerfile — it builds via modal.Image.micromamba in
# modal_app.py, so the glob above cannot see it. It is nonetheless the ninth app
# in the same deploy-modal.yml matrix, on the same `tools/**` rebuild trigger.
# Without this, a green "base images are digest-pinned" run reads as covering the
# whole fleet while the most exposed app of the nine sits outside it.
# ---------------------------------------------------------------------------

_E2D_MODAL_APP = _TOOLS_DIR / "esmfold2_design" / "modal_app.py"

_INSTALL_CALLS = {"pip_install", "micromamba_install"}

# The spec must END in an exact release: `==` followed by a version that runs to
# the end of the spec. Matching structurally rather than by substring is what
# makes this a pin. A lower bound (`pkg>=1.2`) is the drift hazard, not a defence
# against it — dm-haiku was `>=`-shaped — and the compatible-release operator
# `pkg~=1.2.3` and the wildcard `pkg==1.2.*` float in exactly the same way, so
# neither is accepted. Conda's single `=` is a fuzzy "startswith" match, so conda
# specs must use `==` too; `[^,;*]` admits the trailing `=<build>` of
# `==<version>=<build>` (the build string is not REQUIRED here — a bare conda
# `==<version>` still floats across build numbers, which is a live gap this
# guard does not close; the file's own comment block asks for the build string).
_EXACT_VERSION_RE = re.compile(r"==\s*[0-9][^,;*]*$")

# A `git+` direct reference is pinned only if it names an IMMUTABLE ref. The URL
# alone is not a pin: `...@main` (or a tag) re-resolves on every rebuild — the
# exact hazard modal_app.py documents for its `transformers @ git+...@main`
# transitive. Anchored to the end so the check reads the ref, not the host, and
# so a URL carrying no `@ref` at all fails.
_GIT_SHA_RE = re.compile(r"^git\+.*@[0-9a-f]{40}$")


def _install_specs(source: str) -> list[tuple[int, str]]:
    """``[(lineno, spec)]`` for every argument of a ``.pip_install()`` /
    ``.micromamba_install()`` call.

    Uses ``ast`` rather than scanning text. A hand-rolled paren counter got this
    wrong in three separate ways — it dropped specs sharing a line with the call,
    it matched the operator against the whole source line (so a trailing
    ``# was ==0.4.4`` comment or a ``?q=`` in a URL faked a pin), and an
    unbalanced paren inside a string literal made it walk into ``.run_commands()``
    and report ``curl`` as a dependency. Reading literal argument VALUES off the
    parse tree removes that entire class.

    Keyword arguments (``channels=``, ``extra_index_url=``) are not in
    ``node.args`` and so are ignored for free. Non-literal arguments are returned
    as sentinels rather than skipped, so ``.pip_install(*DEPS)`` fails loudly
    instead of silently reducing the guard to nothing.
    """
    tree = ast.parse(source)
    # Module-level ``NAME = "literal"`` bindings, so that an f-string spec such as
    # ``f"esm @ git+...@{_ESM_GIT_SHA}"`` resolves to the ref it ACTUALLY installs.
    # Keeping only the literal segments would leave a bare trailing ``@`` that no
    # ref check could inspect, which is how a repoint to a branch slipped through.
    consts = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in _INSTALL_CALLS):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append((arg.lineno, arg.value))
            elif isinstance(arg, ast.JoinedStr):
                # f-string: literal parts verbatim, `{NAME}` substituted from the
                # module constants above. Anything else (a call, an attribute, a
                # name bound non-literally) becomes a sentinel so it fails loudly.
                parts = []
                for v in arg.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        parts.append(v.value)
                    elif (
                        isinstance(v, ast.FormattedValue)
                        and isinstance(v.value, ast.Name)
                        and v.value.id in consts
                    ):
                        parts.append(consts[v.value.id])
                    else:
                        parts.append(f"<non-literal {type(v).__name__}>")
                out.append((arg.lineno, "".join(parts)))
            else:
                out.append((arg.lineno, f"<non-literal {type(arg).__name__}>"))
    return out


def test_esmfold2_design_install_specs_are_version_constrained():
    specs = _install_specs(_E2D_MODAL_APP.read_text(encoding="utf-8"))
    assert len(specs) >= 9, (
        "parsed too few install specs from esmfold2_design/modal_app.py "
        f"({len(specs)}) — the parser has drifted from the file and this test "
        f"would pass vacuously. Parsed: {specs!r}"
    )

    floating = []
    for lineno, spec in specs:
        # Strip any environment marker first: `pkg; python_version>="3.10"` has a
        # `>=` that says nothing about which version of `pkg` gets installed.
        bare = spec.split(";", 1)[0].strip()
        if "git+" in bare:
            # A direct reference is pinned by its REF, not by being a URL. Drop
            # any `#egg=`/`#subdirectory=` fragment, then require a 40-hex commit.
            url = bare[bare.index("git+"):].split("#", 1)[0].strip()
            if not _GIT_SHA_RE.match(url):
                floating.append((lineno, spec))
        elif not _EXACT_VERSION_RE.search(bare):
            floating.append((lineno, spec))

    assert not floating, (
        "esmfold2_design/modal_app.py installs dependencies that are not pinned to "
        "an exact, immutable version at "
        + ", ".join(f"line {n}: {spec!r}" for n, spec in floating)
        + ". This app has no Dockerfile and so is not covered by the base-image "
        "digest guard, but it rebuilds on the same tools/** push as the other "
        "eight — an open spec drifts on an unrelated merge. Use `==<version>` "
        "running to the end of the spec (a `>=` lower bound is the hazard, not a "
        "fix; `~=` and `==1.2.*` float the same way; conda's single `=` is a fuzzy "
        "prefix match), and pin any `git+` reference to a full 40-char commit SHA "
        "— a branch or tag re-resolves on every rebuild. Pin the version actually "
        "running — read it out of the built image, do not use 'latest'."
    )


def test_esmfold2_design_esm_is_pinned_to_a_full_sha():
    source = _E2D_MODAL_APP.read_text(encoding="utf-8")
    m = re.search(r'^_ESM_GIT_SHA\s*=\s*["\']([0-9a-f]+)["\']', source, re.MULTILINE)
    assert m, "_ESM_GIT_SHA not found in esmfold2_design/modal_app.py"
    assert len(m.group(1)) == 40, (
        f"_ESM_GIT_SHA is {len(m.group(1))} chars, not a full 40-char SHA; short "
        "SHAs can become ambiguous as upstream grows."
    )
