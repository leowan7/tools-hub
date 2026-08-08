"""The binder forms must let a user TYPE a multi-chain target and understand it.

Two defects from ``docs/HANDOFF-2026-08-07-multichain-finish.md`` items 1b/1c:

1b. ``maxlength="4"`` on ``target_chain``. ``"A,B"`` is 3 characters and fits;
    ``"A,B,C"`` is 5 and **cannot be typed at all**. The backend supports N
    chains — ``tests/test_multichain_targets.py::test_three_chain_target_accepted``
    proves it — so the input was the only thing capping the product at two
    protomers.

1c. The hotspot help said "residue indices from the target chain", singular,
    with the example ``54,56,115``. Nothing told the user the ``A296,B264``
    form existed, so the one field that can express a multi-chain epitope read
    as if it could not.

The forms are grouped by BEHAVIOUR, not by family resemblance. That is not
bookkeeping: bindcraft sat in ``MULTI_CHAIN_FORMS`` while its preflight refuses
a multi-chain target and its picker deletes a chain-prefixed hotspot on the
first click, and the grouping alone is what put the sibling forms' copy on it.
Each tuple below carries the behaviour that earns membership.

Assertions are on PARSED ATTRIBUTES and RENDERED TEXT, never on template
source. ``tests/test_candidate_table_js_contract.py:11-31`` is a catalogue of
what source-substring assertions cost here: four of thirteen hooks were held
up by CSS rules and template comments rather than by the code they claimed to
pin, so the tests passed while the feature was broken. A Jinja comment
containing the right words satisfies a grep and ships nothing.
"""
from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

# Every tool whose target may be an oligomer AND whose image can run one. The
# chain field has to hold at least "A,B,C"; 32 matches proteina's own
# server-side _MAX_CHAIN_FIELD.
#
# bindcraft is NOT here, and the grouping is the point. It was, and that alone
# is what put the sibling forms' multi-chain copy on a form whose preflight
# refuses a multi-chain target and whose picker deletes a chain-prefixed
# hotspot on the first click. A tool joins this tuple when its behaviour joins
# it, not because its form looks like the others. See GATED_FORMS below.
MULTI_CHAIN_FORMS = ("rfdiffusion", "pxdesign", "boltzgen", "proteina")

# rfantibody builds a VHH against ONE chain (multi_chain_supported=False), and
# tools/rfantibody/__init__.py caps the whole field at 4 characters. Raising
# the input would let a user type "A,B", pass the 4-char check, and reach
# llm-proteinDesigner/docker/rfantibody/run_pipeline.py:1387 where chain="A,B"
# builds "--hotspots A,B25". The 4 here is load-bearing, not an oversight.
SINGLE_CHAIN_FORMS = ("rfantibody",)

# Neither of the above. bindcraft's ADAPTER parses "A,B" and "A296,B264" fine,
# so the field is not capped at 4 — but multi_chain_container_ready=False means
# preflight refuses the run, and the picker is not chainPrefixed, so the COPY
# must not advertise either form. Asserted explicitly, in both directions,
# rather than by omission.
GATED_FORMS = ("bindcraft",)

# The forms whose hotspot copy this change rewrote. proteina already documented
# both token forms in its own words and is asserted more loosely below.
REWRITTEN_COPY_FORMS = ("rfdiffusion", "pxdesign", "boltzgen")

WIDEST_TYPEABLE_TARGET = "A,B,C"

# "A296,B264" — two chain-prefixed tokens. Deliberately a shape, not a literal,
# so the copy can be reworded without the test pinning one phrasing.
_PREFIXED_EXAMPLE = re.compile(r"[A-Z]\s*\d+\s*,\s*[A-Z]\s*\d+")


class _Doc(HTMLParser):
    """Collects input tags by name, and the page's visible text.

    HTMLParser routes ``<!-- ... -->`` to handle_comment, which this ignores —
    so copy that only exists inside a Jinja/HTML comment cannot satisfy any
    assertion here. That is the entire point.
    """

    _FIELDS = ("input", "select", "textarea")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inputs: list = []
        self._text: list = []
        self._skip = 0
        # Text that follows a given field, up to the next field. That is the
        # help block for it, and scoping to it matters: asserting on the whole
        # page let unrelated copy elsewhere satisfy a help-text assertion, so
        # three of four forms passed a check none of them actually met.
        self._after: dict = {}
        self._current: str = ""

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "input":
            self.inputs.append(d)
        if tag in self._FIELDS:
            self._current = d.get("name") or ""
            self._after.setdefault(self._current, [])
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            return
        self._text.append(data)
        if self._current:
            self._after[self._current].append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._text))

    def help_after(self, name: str) -> str:
        """Visible text between the named field and the next field."""
        return re.sub(r"\s+", " ", "".join(self._after.get(name, []))).strip()

    def input_named(self, name: str) -> dict:
        found = [i for i in self.inputs if i.get("name") == name]
        assert len(found) == 1, f"expected 1 input[name={name}], got {len(found)}"
        return found[0]


@pytest.fixture(scope="module")
def flask_app():
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture(scope="module")
def pages(flask_app):
    ctx = SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com"
    )
    out = {}
    for slug in MULTI_CHAIN_FORMS + SINGLE_CHAIN_FORMS + GATED_FORMS:
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "u-1"
            sess["user_email"] = "u@example.com"
        with patch(
            "blueprints.tools.load_user_context", return_value=ctx
        ), patch("blueprints.tools.tool_enabled", return_value=True), patch(
            "blueprints.tools.get_or_create_wallet",
            return_value={"balance_usd": "100", "wallet_frozen": False},
        ):
            resp = client.get(f"/tools/{slug}")
        assert resp.status_code == 200, f"/tools/{slug} -> {resp.status_code}"
        doc = _Doc()
        doc.feed(resp.get_data(as_text=True))
        out[slug] = doc
    return out


# ---------------------------------------------------------------------------
# 1b — the field has to hold more than two chains
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", MULTI_CHAIN_FORMS)
def test_target_chain_accepts_at_least_three_chains(slug, pages):
    """The requirement, stated as the requirement: "A,B,C" must be typeable."""
    maxlength = pages[slug].input_named("target_chain").get("maxlength")
    assert maxlength is not None, f"{slug}: target_chain has no maxlength"
    assert int(maxlength) >= len(WIDEST_TYPEABLE_TARGET), (
        f"{slug}: maxlength={maxlength} cannot hold "
        f"{WIDEST_TYPEABLE_TARGET!r} ({len(WIDEST_TYPEABLE_TARGET)} chars)"
    )


@pytest.mark.parametrize("slug", MULTI_CHAIN_FORMS)
def test_target_chain_maxlength_matches_the_server_cap(slug, pages):
    """32 is proteina's own _MAX_CHAIN_FIELD. The other four validators impose
    no whole-string cap at all (only per-token <=4, enforced in validate()), so
    this is an input affordance rather than a mirror of a server rule — but it
    must not be tighter than the one server rule that does exist."""
    from tools.proteina import _MAX_CHAIN_FIELD

    assert pages[slug].input_named("target_chain")["maxlength"] == str(
        _MAX_CHAIN_FIELD
    )


@pytest.mark.parametrize("slug", SINGLE_CHAIN_FORMS)
def test_single_chain_tools_keep_the_tight_cap(slug, pages):
    """rfantibody's 4 is load-bearing — see the module docstring."""
    assert pages[slug].input_named("target_chain")["maxlength"] == "4"


# ---------------------------------------------------------------------------
# 1c — the copy has to mention the form that exists
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", MULTI_CHAIN_FORMS)
def test_hotspot_copy_shows_a_chain_prefixed_example(slug, pages):
    help_text = pages[slug].help_after("hotspot_residues")
    assert _PREFIXED_EXAMPLE.search(help_text), (
        f"{slug}: no chain-prefixed hotspot example in the hotspot help; "
        f"a user has no way to learn the A296,B264 form exists. "
        f"help={help_text!r}"
    )


@pytest.mark.parametrize("slug", REWRITTEN_COPY_FORMS)
def test_hotspot_copy_says_a_bare_number_means_the_first_chain(slug, pages):
    """The ambiguity that actually bites: on an "A,B" target, is "296" chain A
    or chain B? tools/base.py:108 attributes it to the FIRST target chain, and
    that is silent unless the form says so."""
    help_text = pages[slug].help_after("hotspot_residues").lower()
    assert "first" in help_text, (
        f"{slug}: hotspot help never says a bare number means the first "
        f"target chain. help={help_text!r}"
    )


@pytest.mark.parametrize("slug", REWRITTEN_COPY_FORMS)
def test_hotspot_copy_no_longer_claims_a_single_target_chain(slug, pages):
    help_text = pages[slug].help_after("hotspot_residues").lower()
    assert "from the target chain" not in help_text, (
        f"{slug}: copy still describes the target as one chain"
    )


@pytest.mark.parametrize("slug", REWRITTEN_COPY_FORMS)
def test_target_chain_field_says_several_chains_are_allowed(slug, pages):
    """Raising maxlength is invisible. The field has to say what it now takes,
    or a user with a dimer still types one letter."""
    help_text = pages[slug].help_after("target_chain").lower()
    assert "chain" in help_text and re.search(r"a\s*,\s*b", help_text), (
        f"{slug}: target chain field never mentions a multi-chain value. "
        f"help={help_text!r}"
    )


def test_proteina_points_at_its_own_multi_chain_route(pages):
    """proteina is excluded from the assertion above on purpose. It expresses a
    multi-chain target through a separate contig field (``target_input``,
    "A12-157,B12-157"), not by listing chains in ``target_chain``, so telling
    users to type "A,B" there would send them down the wrong path. The field
    still gets the 32-char cap, because its own validator allows 32
    (tools/proteina/__init__.py:508) and the form contradicted it at 4."""
    help_text = pages["proteina"].help_after("target_chain").lower()
    assert "several chains" in help_text and "region" in help_text, (
        f"proteina: target chain help no longer points at the contig field. "
        f"help={help_text!r}"
    )


@pytest.mark.parametrize("slug", SINGLE_CHAIN_FORMS)
def test_single_chain_tools_do_not_advertise_the_prefixed_form(slug, pages):
    """rfantibody's adapter rejects "A25" with a bare int() parse. Showing the
    prefixed example there would document a form the tool refuses."""
    help_text = pages[slug].help_after("hotspot_residues")
    assert not _PREFIXED_EXAMPLE.search(help_text), (
        f"{slug}: advertises a hotspot form its own validator rejects"
    )


# ---------------------------------------------------------------------------
# The gated form: parses it, refuses to run it, must not advertise it
# ---------------------------------------------------------------------------
#
# These are the negative half of 1b/1c, and they exist because the positive
# half was applied to bindcraft by GROUPING rather than by behaviour. Both
# assertions below failed against that copy.

@pytest.mark.parametrize("slug", GATED_FORMS)
def test_gated_forms_do_not_advertise_the_prefixed_hotspot_form(slug, pages):
    """The picker destroys it, so the copy must not teach it.

    bindcraft is in BARE_INT_FORMS in tests/test_hotspot_picker_runtime.py:
    ``chainPrefixed`` is deliberately not set (the container forwards tokens
    verbatim to a prebuilt image whose parser is vendored in neither repo, and
    bindcraft has no smoke tier). So typing "A296" and then clicking once in
    the 3D viewer rewrites the whole field as bare ints — the prefix is gone
    with no message. Copy that teaches a form the page itself deletes is worse
    than no copy.
    """
    help_text = pages[slug].help_after("hotspot_residues")
    assert not _PREFIXED_EXAMPLE.search(help_text), (
        f"{slug}: hotspot help shows a chain-prefixed example, but this "
        f"form's picker is not chainPrefixed — one click in the viewer "
        f"silently destroys it. help={help_text!r}"
    )


@pytest.mark.parametrize("slug", GATED_FORMS)
def test_gated_forms_do_not_promise_a_multi_chain_target(slug, pages):
    """The gate refuses it, so the copy must not offer it.

    ``multi_chain_container_ready=False`` (shared/pdb_preflight_rules.py) makes
    preflight_for_tool return needs_fix for any target naming more than one
    chain — pinned end-to-end by tests/test_multichain_targets.py::
    test_preflight_refuses_multichain_for_the_unverified_image. A field whose
    help says "several for an oligomeric target (A,B)" walks the user into that
    refusal.
    """
    help_text = pages[slug].help_after("target_chain").lower()
    assert not re.search(r"a\s*,\s*b", help_text), (
        f"{slug}: target chain help offers a multi-chain value that preflight "
        f"refuses at submit. help={help_text!r}"
    )
    # Not vacuous: the field must still say what it DOES take, or removing the
    # over-promise would pass by saying nothing at all.
    assert "one chain" in help_text, (
        f"{slug}: target chain help no longer states the single-chain "
        f"requirement. help={help_text!r}"
    )


@pytest.mark.parametrize("slug", GATED_FORMS)
def test_gated_forms_still_post_what_was_typed(slug, pages):
    """The cap stays wide even though the copy says one chain.

    A 4-char field does not enforce single-chain — "A,B" is 3 characters and
    fits. All it does is truncate "A,B,C" to "A,B," so the server sees two of
    the three chains the user named and the refusal describes the wrong input.
    The tool's own validator caps chain ids per TOKEN, so the honest field is
    one wide enough to post what was typed and let the submit-time refusal
    speak.
    """
    from tools.proteina import _MAX_CHAIN_FIELD

    assert pages[slug].input_named("target_chain")["maxlength"] == str(
        _MAX_CHAIN_FIELD
    )


# ---------------------------------------------------------------------------
# The design-target path, which feeds the same adapters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", ["targets/new.html", "targets/launch.html"])
def test_design_target_forms_are_not_capped_at_two_chains(template, flask_app):
    """A target saved here pre-fills every later run (shared/targets.py:979), so
    a 4-char cap on this path re-imposes the two-chain limit on exactly the
    users who run the same target repeatedly."""
    from tools.proteina import _MAX_CHAIN_FIELD

    src = (flask_app.jinja_env.get_or_select_template(template).filename)
    body = open(src, encoding="utf-8").read()
    start = re.search(r'field_text\(\s*"target_chain"', body)
    assert start, f"{template}: no field_text(\"target_chain\") call found"
    # Bounded by the NEXT macro call rather than the next ")": the helper
    # strings contain parentheses of their own ("One chain (A)"), so a
    # non-greedy match to ")" stops inside the prose and silently reads no
    # max_length at all.
    rest = body[start.start():]
    end = re.search(r"\{\{\s*field_", rest[1:])
    call = rest[: end.start() + 1] if end else rest
    m = re.search(r"max_length\s*=\s*(\d+)", call)
    assert m, f"{template}: target_chain field declares no max_length"
    assert int(m.group(1)) == _MAX_CHAIN_FIELD, (
        f"{template}: max_length={m.group(1)}, cannot hold "
        f"{WIDEST_TYPEABLE_TARGET!r}"
    )


# ---------------------------------------------------------------------------
# The copy on a page must be parseable by THAT page's parser
# ---------------------------------------------------------------------------
#
# The two target pages do NOT share a residue parser, and that is the whole
# defect this section exists to hold shut:
#
#   POST /targets        (targets/new.html)     -> targets.py::_parse_residue_list
#                                                  bare int() per token; answers
#                                                  "'A296' is not a residue
#                                                  number."
#   POST /targets/<id>/launch (targets/launch.html)
#                                               -> the TOOL ADAPTER's validate(),
#                                                  i.e. tools/base.py::
#                                                  parse_hotspot_residues, which
#                                                  accepts "A45".
#
# So launch.html's "for a multi-chain Proteina target, prefix the chain
# (A45, C73)" is true on its own route, and the same sentence on new.html was
# not: it told users to type a token the create route rejects outright. It is
# the COPY that moves, not the parser -- _parse_residue_list feeds the shared
# target record, which is read back by every tool including iggm and
# rfantibody, whose adapters must never see a prefixed token.

# An example residue as it appears in copy: an optional chain prefix then
# digits. Deliberately permissive about the prefix so a re-introduced "A296"
# is SEEN by the extractor rather than skipped as prose.
_RESIDUE_EXAMPLE = re.compile(r"\b([A-Za-z]{0,2}\d+)\b")

# The two fields POST /targets parses with _parse_residue_list.
_RESIDUE_FIELDS = ("hotspot_residues", "epitope_residues")


@pytest.fixture(scope="module")
def targets_new_page(flask_app):
    """GET /targets/new through the real route."""
    ctx = SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com"
    )
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.targets.load_user_context", return_value=ctx):
        resp = client.get("/targets/new")
    assert resp.status_code == 200, resp.status_code
    doc = _Doc()
    doc.feed(resp.get_data(as_text=True))
    return doc


@pytest.mark.parametrize("field", _RESIDUE_FIELDS)
def test_targets_new_residue_examples_parse_on_its_own_route(
    field, targets_new_page
):
    """Every residue example this page shows must survive this page's parser.

    Stated as the property rather than as "the copy does not say A296", so it
    keeps holding through a rewording: whatever example the field offers, the
    route it posts to has to accept it.
    """
    from blueprints.targets import _parse_residue_list

    inp = targets_new_page.input_named(field)
    shown = f"{inp.get('placeholder') or ''} {targets_new_page.help_after(field)}"
    examples = _RESIDUE_EXAMPLE.findall(shown)
    assert examples, (
        f"{field}: no residue example anywhere in the placeholder or help, so "
        f"this check has nothing to compare against. shown={shown!r}"
    )
    for token in examples:
        _, err = _parse_residue_list(token)
        assert err is None, (
            f"targets/new.html offers {token!r} for {field}, but the route it "
            f"posts to answers {err!r}. Fix the COPY: this parser feeds the "
            f"shared target record that iggm and rfantibody read back."
        )


def test_the_residue_example_extractor_can_see_a_chain_prefix():
    """Guard the guard, and the only thing it pins is the EXTRACTOR.

    The check above is worth exactly what its regex can see. If a prefixed
    token were invisible to it, the copy could re-acquire "A296, B264" and the
    check would go on passing while saying nothing -- which is how the
    contradiction shipped in the first place.
    """
    assert _RESIDUE_EXAMPLE.findall(
        "A plain number is read as the first target chain; prefix the chain "
        "to name another (A296, B264)."
    ) == ["A296", "B264"]


def test_field_text_actually_renders_max_length_as_maxlength(flask_app):
    """Pins the indirection the previous test relies on: max_length is a macro
    KEYWORD, and the assertion above reads it from source rather than from a
    rendered page. This proves the keyword still reaches the attribute."""
    with flask_app.test_request_context("/"):
        html = flask_app.jinja_env.from_string(
            '{% from "components/field_group.html" import field_text %}'
            '{{ field_text("target_chain", "Target chain", "", max_length=32) }}'
        ).render()
    doc = _Doc()
    doc.feed(html)
    assert doc.input_named("target_chain")["maxlength"] == "32"
