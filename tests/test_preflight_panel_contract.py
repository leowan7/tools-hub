"""The contract between static/js/preflight.js, the proteina form, and the
``/tools/<slug>/preflight`` route that sizes what they send.

WHY THIS FILE EXISTS. The size envelope is supposed to size the CONTIG — the
chain/residue range the user types ("A236-300,B236-300"), which is what the
container actually designs against — rather than the whole uploaded structure.
``preflight_for_tool`` took a ``target_segments`` argument for exactly that, the
route passed ``preflight_target_segments(request.form)``, and
``shared/pdb_intake.py`` parsed ``target_input`` off the form with the adapter's
own parser. Every server-side piece was correct and tested.

The browser never sent the field. ``preflight.js`` posted ``target_pdb``,
``target_chain``, ``hotspot_residues`` and (boltz2 only) ``binder_length_max``,
so ``request.form`` carried no ``target_input``, ``preflight_target_segments``
returned None, and the envelope fell back to counting whole chains. Uploading
3S7G (830 aa) and typing ``A236-300,B236-300`` produced ``needs_fix`` at
``residue_count=415`` and ``setSubmitEnabled(!!v.ok)`` disabled the Run button,
for a selection that is 130 residues and comfortably inside the 140 cap. The
only way through was to hand-trim the PDB — the exact work the feature exists to
remove. A route comment asserted the opposite in so many words.

So the server half is NOT what these tests are about. Driving ``target_segments``
straight into ``preflight_for_tool`` passes with or without the fix and pins
nothing. The subject here is the SEAM: that the key the browser appends is the
key the server parses, that the field the browser looks for is the field the
form renders, and that a contig typed after the upload re-runs the panel.

HOW THE JS HALF IS SEARCHED. There is no JS runtime in this repo or in CI
(.github/workflows has no node), so the file is read as source — under the
discipline tests/test_candidate_table_js_contract.py arrived at over its rounds
20 and 21. Its ``_lex`` is imported rather than copied: a second lexer would
drift, and this file needs it more than that one does, because the fix ships
comments that name ``target_input`` and ``input[name="target_input"]`` in prose.
Against the raw file every assertion below would be satisfied by a comment.

And the tokens are not string-compared where a real artifact is reachable. The
posted key and the queried field NAME are EXTRACTED from the JS and then run
through the production parser and the live route, so a rename on either side of
the boundary fails here instead of shipping a silently dead panel.
"""

from __future__ import annotations

import io
import pathlib
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

# The house lexer, not a copy of it. See the module docstring.
from tests.test_candidate_table_js_contract import _lex

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_JS_PATH = _ROOT / "static" / "js" / "preflight.js"
_JS_SOURCE = _JS_PATH.read_text(encoding="utf-8")
_JS, _SLASHES = _lex(_JS_SOURCE)


# ---------------------------------------------------------------------------
# What the JS actually does, extracted rather than asserted
# ---------------------------------------------------------------------------

# `const contigInput = form.querySelector('input[name="target_input"]');`
# -> {"contigInput": ("input", "target_input")}. Anchored on `const` +
# `form.querySelector` so a mention in prose cannot answer for a binding (the
# comment stripping already removed prose, and this is the belt to that's
# braces).
#
# THE TAG IS CAPTURED, NOT ASSUMED. An earlier version of this file recorded
# only the name and the rendered-form test then asserted `name="target_input"`
# appeared ANYWHERE in the body. Changing the proteina form's
# `<input name="target_input">` to a `<textarea name="target_input">` therefore
# left the entire suite green while `querySelector('input[name=...]')` returned
# null and the panel died exactly the way F1 died — in the one test written to
# close that hole. A CSS type selector is a claim about the element, so both
# halves of it travel together from here on.
_FIELD_OF = dict(
    (var, (tag, field))
    for var, tag, field in re.findall(
        r"const\s+([A-Za-z][A-Za-z0-9]*)\s*=\s*"
        r"form\.querySelector\(\s*'([a-z]+)\[name=\"([a-z_]+)\"\]'\s*\)",
        _JS,
    )
)

# `fd.append("target_input", contigInput.value || "");`
# -> {"target_input": "contigInput"}. The KEY is what lands in request.form.
_APPENDED_FROM = dict(
    (key, var)
    for key, var in re.findall(
        r'fd\.append\(\s*"([a-z_]+)"\s*,\s*([A-Za-z][A-Za-z0-9]*)\.value',
        _JS,
    )
)


def _rerun_watchlist() -> list:
    """The identifiers whose `input` event re-runs preflight.

    Extracted from the `for (const inp of [...])` array literal rather than
    substring-searched, because every one of these identifiers also appears at
    its own declaration and at its append site. A membership test against the
    whole file would be satisfied by those and could never notice a variable
    dropped from this list — which is the difference between a panel that
    refreshes when the user narrows the contig and one that leaves a stale
    refusal on screen with the Run button still disabled.
    """
    m = re.search(r"for\s*\(\s*const\s+inp\s+of\s*\[([^\]]*)\]", _JS)
    assert m, "the re-run loop's array literal is no longer recognisable"
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def _ready_branch() -> str:
    """The `ready` arm of renderVerdict, comment-stripped and sliced out.

    Sliced rather than searched whole-file so that a reference living in some
    other arm cannot answer for this one. The ready arm is the only place the
    contradiction below was reachable: the needs_fix arm has always printed
    ``v.reason``, which is ``hard_fail_message`` and already embeds the count
    the gate used.
    """
    start = _JS.index('if (v.kind === "ready"')
    end = _JS.index('} else if (v.kind === "needs_fix")', start)
    return _JS[start:end]


def _guard_before(branch: str, marker: str) -> str:
    """The condition of the nearest enclosing `if (...)` above ``marker``.

    Reads the guard rather than assuming one. A token search over a branch
    proves the code was WRITTEN, never that it RUNS — `if (false) {` leaves
    every token in place — and that is the one dead-branch mutation a
    source-level test can still decide.
    """
    i = branch.index(marker)
    j = branch.rindex("if (", 0, i)
    k = branch.index(")", j)
    return branch[j + len("if ("):k].strip()


def test_the_comment_stripping_removed_the_prose_that_would_fake_a_pass():
    """Precondition, and NOT the one test_candidate_table_js_contract makes.

    That file proves its lexer safe by asserting no slash survives in code
    position, which works because candidate_table.js has no regex literal.
    preflight.js has two (`/\\s+/g`), so the empty-list proof is unavailable
    here and asserting it would be a permanent false alarm.

    Prove the property this file actually depends on instead. The failure mode
    that matters is stripping too LITTLE — a token left sitting in a comment
    answering for code. (Stripping too much fails in the safe direction: the
    token disappears and the tests below go red.)

    PROVED ON A FIXTURE, NOT ON THE PRODUCTION COMMENTS. This test used to
    assert that the stripped source held strictly fewer `target_input` mentions
    than the raw source, plus the literal presence of one comment sentence.
    Both went red on a pure comment reword with no behaviour change whatsoever,
    which is a false alarm on a precondition — and a precondition that cries
    wolf is one someone eventually deletes. The guarantee needed here is a
    property of `_lex`, not of any particular comment: if it provably removes
    `//` and `/* */` comments containing the token, then no comment in
    preflight.js can satisfy the searches below. That is decidable on a fixture
    and is coupled to nothing.
    """
    fixture = (
        '// contigInput reads target_input from the form\n'
        '/* fd.append("target_input", x) — prose, not code */\n'
        'const real = "target_input";\n'
    )
    stripped, _ = _lex(fixture)
    assert stripped.count("target_input") == 1, (
        f"_lex left {stripped.count('target_input')} of 3 mentions standing; "
        f"comments can now answer for code in every search below"
    )
    assert 'const real = "target_input";' in stripped
    # And the real file still has the token in CODE, or the searches below are
    # passing on nothing.
    assert _JS.count("target_input") > 0


def test_the_panel_posts_the_contig():
    """THE BUG. Without this key the panel sizes the upload, not the run."""
    assert "target_input" in _APPENDED_FROM, (
        "preflight.js never appends target_input, so /tools/<slug>/preflight "
        "receives no contig and the size envelope falls back to counting whole "
        "chains — refusing selections that are well inside the cap."
    )


def test_the_key_posted_is_the_field_read_from_the_form():
    """One rename, one failure. The variable the JS appends from must be the
    variable it bound to an input, and that input's NAME must be the same
    string as the posted key — otherwise the browser reads one field and posts
    another, which is indistinguishable from not posting at all."""
    var = _APPENDED_FROM["target_input"]
    assert var in _FIELD_OF, (
        f"{var} is appended as target_input but is not bound to any "
        f"form.querySelector('input[name=...]')"
    )
    assert _FIELD_OF[var] == ("input", "target_input")


def test_the_posted_key_is_the_key_the_server_parses():
    """Drive the extracted key through the PRODUCTION parser, not a copy.

    ``preflight_target_segments`` is what the route calls. Feeding it a form
    keyed by whatever the JS decided to send is what makes a rename on either
    side of this boundary fail here rather than in production.
    """
    from shared.pdb_intake import preflight_target_segments

    key = next(k for k, v in _APPENDED_FROM.items()
               if (_FIELD_OF.get(v) or ("", ""))[1] == "target_input")
    segments = preflight_target_segments({key: "A236-300,B236-300"})
    assert segments == [("A", 236, 300), ("B", 236, 300)], (
        f"the server does not parse a contig out of the {key!r} key the "
        f"browser posts"
    )


def test_typing_a_contig_re_runs_the_panel():
    """A contig is normally typed AFTER the file is attached, so the first
    verdict is always the whole-upload one. Without a re-run the user reads a
    refusal for a run they have since narrowed, and the Run button stays
    disabled at the value it was disabled on."""
    var = _APPENDED_FROM["target_input"]
    assert var in _rerun_watchlist(), (
        f"{var} is not in the re-run watchlist, so narrowing the contig after "
        f"upload leaves the stale whole-upload refusal on screen"
    )


def test_both_request_builders_send_the_same_target_fields():
    """The upload path and the AlphaFold path must not drift.

    They append the target fields from ONE helper for this reason: two copies
    of the same three lines is how a field ends up on one path and not the
    other, and a panel that sizes a different run than submit does is worse
    than no panel. Asserted structurally — each builder calls the helper — so
    adding a fourth field cannot land on only one of them.
    """
    assert re.search(r"function\s+appendTargetFields\s*\(", _JS), (
        "the shared target-field builder is gone; the two request builders "
        "can now drift"
    )
    # `(?<!function )` so the DEFINITION cannot answer for a call site — the
    # same superstring hole this repo's other JS contract test documents.
    calls = re.findall(r"(?<!function )\bappendTargetFields\(fd\)", _JS)
    assert len(calls) == 2, (
        f"expected both request builders (upload + alphafold) to call "
        f"appendTargetFields, found {len(calls)} call site(s)"
    )


# ---------------------------------------------------------------------------
# The rendered form: the field the JS queries has to actually be there
# ---------------------------------------------------------------------------


# EVERY form that mounts #preflight-panel, which is every consumer of
# preflight.js -- all eight, verified against the templates that load the
# script. An earlier version of this tuple listed six, so the "other requests
# are unchanged" guard below was 5 of 7 rather than 7 of 7; boltz2 and iggm
# were never rendered by it. Neither carries a contig, so nothing was broken,
# but the claim was wider than the coverage.
#
# `tool_enabled` is fail-closed on a missing env var, so without these flags
# the negative test would read 404 for every tool and pass by never rendering
# anything -- which is why each row asserts its 200 and its panel first.
_PANEL_TOOLS = (
    "proteina", "rfdiffusion", "bindcraft", "boltzgen", "rfantibody",
    "pxdesign", "boltz2", "iggm",
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    for slug in _PANEL_TOOLS:
        monkeypatch.setenv("FLAG_TOOL_" + slug.upper(), "on")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"


def _ctx():
    return SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com",
    )


def _get_form(client, slug):
    with patch("blueprints.tools.load_user_context", return_value=_ctx()):
        return client.get(f"/tools/{slug}")


def _squash(body: str) -> str:
    """The shared field macros render one attribute per line, so an assertion
    on `name="x" id="x"` against the raw body silently never matches."""
    return " ".join(body.split())


class _Elements(HTMLParser):
    """Every rendered element as ``(tag, attrs)``.

    Same device test_candidate_table_js_contract.py settled on in its round 20:
    an attribute NAME compared against a parsed attribute dict cannot be
    satisfied by a longer attribute that merely starts with it, and html.parser
    puts <style>/<script> into CDATA mode so nothing inside them is ever
    reported as an element. Here it buys the property a substring search cannot
    give at all — WHICH ELEMENT the attribute sits on.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def _elements(body: str) -> list:
    p = _Elements()
    p.feed(body)
    return p.elements


def test_the_proteina_form_renders_the_field_the_js_queries(client):
    """The RENDERED artifact, not the template source. A field that only
    exists in a Jinja branch the page never takes is a field the browser never
    sees, and `querySelector` would return null with no error anywhere.

    THE ELEMENT TYPE IS PART OF THE CONTRACT, and this test used to skip it.
    It asserted the NAME appeared somewhere in the body, so swapping the
    proteina form's `<input name="target_input">` for a
    `<textarea name="target_input">` kept the whole suite green while
    `querySelector('input[name=...]')` returned null and the panel went back to
    sizing the whole upload — the exact failure this file was written to close,
    passing through the file written to close it. Both halves of the selector
    now come out of the JS and are checked against parsed elements.
    """
    _login(client)
    resp = _get_form(client, "proteina")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    tag, field = _FIELD_OF[_APPENDED_FROM["target_input"]]
    matches = [
        (t, a) for t, a in _elements(body)
        if t == tag and a.get("name") == field
    ]
    assert matches, (
        f"preflight.js queries {tag}[name=\"{field}\"]; the rendered proteina "
        f"form has no <{tag}> with that name. Present on other elements: "
        + repr(sorted({t for t, a in _elements(body)
                       if a.get("name") == field}))
    )
    # And the panel is on this page at all — the script bails without it.
    assert 'id="preflight-panel"' in _squash(body)


def test_no_other_panel_form_carries_the_contig_field(client):
    """BLAST RADIUS, over all seven other panel forms rather than a subset.

    preflight.js is shared by eight tool forms. The contig is proteina's alone,
    so `querySelector` returns null everywhere else and a null appends nothing
    — their requests stay byte-identical. If another form ever grows a
    `target_input`, that tool starts sending a contig to a size envelope that
    would parse it with PROTEINA's parser, and this test is the thing that
    makes someone think about it first."""
    _login(client)
    # The NAME only, deliberately: for the negative half a substring search is
    # the stronger assertion, because a contig field would be a problem on any
    # element, not only on the one the JS currently queries.
    field = _FIELD_OF[_APPENDED_FROM["target_input"]][1]
    for slug in (s for s in _PANEL_TOOLS if s != "proteina"):
        resp = _get_form(client, slug)
        # A 404 here would make the assertion below vacuous, so the render is
        # asserted before what it rendered.
        assert resp.status_code == 200, f"{slug} did not render"
        body = _squash(resp.get_data(as_text=True))
        assert 'id="preflight-panel"' in body, (
            f"{slug} no longer mounts the panel, so this row is not testing "
            f"a preflight.js consumer at all"
        )
        assert f'name="{field}"' not in body, (
            f"{slug}'s form now renders {field}; preflight.js will start "
            f"posting a contig for it"
        )


# ---------------------------------------------------------------------------
# The seam, driven end to end through the real route
# ---------------------------------------------------------------------------

def _pdb(chains: dict) -> bytes:
    """Backbone-complete residues, the shape pipeline_normalize keeps."""
    lines = ["HEADER    SYNTHETIC\n"]
    serial = 0
    for chain_id, resnums in chains.items():
        for i, rn in enumerate(resnums):
            xb = float(i * 4.0)
            for nm, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 3.0)]:
                serial += 1
                elem = nm[0].rjust(2)
                lines.append(
                    f"ATOM  {serial:5d}  {nm:<3s} ALA "
                    f"{chain_id:1s}{rn:4d}    "
                    f"{xb + off:8.3f}{1.0:8.3f}{1.0:8.3f}"
                    f"{1.0:6.2f}{10.0:6.2f}          {elem}\n"
                )
    lines.append("END\n")
    return "".join(lines).encode()


# 3S7G's shape in miniature: a big two-chain upload whose CH2+CH3-style window
# is a small fraction of it. Whole = 400 aa (over proteina's 140 cap),
# A236-300,B236-300 = 130 (inside it, and the size the one paid GPU run used).
_BIG_UPLOAD = _pdb({
    "A": list(range(1, 201)),
    "B": list(range(101, 301)),
})
_CONTIG = "A100-164,B236-300"


def _post_preflight(client, **extra):
    data = {
        "target_chain": "A B",
        "hotspot_residues": "",
        "target_pdb": (io.BytesIO(_BIG_UPLOAD), "big.pdb"),
    }
    data.update(extra)
    with patch("blueprints.tools.load_user_context", return_value=_ctx()):
        return client.post(
            "/tools/proteina/preflight", data=data,
            content_type="multipart/form-data",
        )


def test_the_whole_upload_is_refused_without_a_contig(client):
    """Precondition for the test below. If this upload fitted anyway, the
    admission below would prove nothing about the contig."""
    _login(client)
    body = _post_preflight(client).get_json()
    assert body["ok"] is False
    assert body["kind"] == "needs_fix"
    assert body["size_envelope"]["residue_count"] == 400


def test_the_contig_the_browser_posts_sizes_the_selection(client):
    """END TO END, through the real route, on the real key.

    The same oversized upload plus the contig is a 130-residue run and has to
    be admitted — `ok` is what `setSubmitEnabled(!!v.ok)` reads, so this is
    literally whether the Run button is clickable. The form key is the one
    extracted from the JS above, so this cannot pass against a key the browser
    does not send.
    """
    _login(client)
    key = _FIELD_OF[_APPENDED_FROM["target_input"]][1]
    body = _post_preflight(client, **{key: _CONTIG}).get_json()
    assert body["ok"] is True, body.get("reason")
    # 130, not 400: the number the panel reports is the SELECTION's. Asserted
    # on the count rather than on a `size_basis` flag because the JSON block
    # ships neither `size_basis` nor `selection_label` (shared/pdb_intake.py
    # ::_verdict_to_json), and the count is the discriminator anyway — nothing
    # but the contig can move it from 400 to 130.
    assert body["size_envelope"]["residue_count"] == 130


# ---------------------------------------------------------------------------
# The panel must not print a number the gate did not judge
# ---------------------------------------------------------------------------
#
# Wiring the contig made a latent JSON divergence REACHABLE. The payload has
# always carried two different residue counts — ``residues_kept_on_target
# _chain`` (the whole named chains, i.e. the file) and
# ``size_envelope.residue_count`` (what the envelope actually judged) — and at
# 352de0a they were also 400 and 130 for this upload. No user could see it:
# without the contig in the request the verdict was needs_fix at 400 and the
# ready arm never rendered.
#
# With the contig posted, the sequence a real user walks is: upload -> refusal
# naming 400 against the 140 cap -> type the contig -> "Ready to run — 400
# residues." Nothing on screen reconciled those, and the ready arm rendered
# neither the cap nor the envelope. Not a money bug — the gate was right
# throughout — but it is collateral of this commit's own headline feature and
# it undermines the single job the panel has.
# ---------------------------------------------------------------------------

def test_the_verdict_says_which_number_the_gate_counted(client):
    """The discriminator has to be IN the payload before the panel can use it.

    ``size_basis`` and ``selection_label`` were computed by the envelope and
    dropped on the floor by ``_verdict_to_json``, so the browser had no way to
    tell a whole-chain count from a contig selection.
    """
    _login(client)
    key = _FIELD_OF[_APPENDED_FROM["target_input"]][1]
    body = _post_preflight(client, **{key: _CONTIG}).get_json()
    env = body["size_envelope"]
    assert env["size_basis"] == "selection"
    assert env["selection_label"] == _CONTIG
    # The two numbers the payload carries, and the fact that they DIFFER —
    # which is the precondition that makes rendering the wrong one visible.
    assert env["residue_count"] == 130
    assert body["residues_kept_on_target_chain"] == 400


def test_a_whole_chain_run_still_reports_the_chain_basis(client):
    """The other side of the discriminator. Without this, the assertion above
    is satisfied by hardcoding "selection"."""
    _login(client)
    body = _post_preflight(client).get_json()
    assert body["size_envelope"]["size_basis"] == "chains"
    assert body["size_envelope"]["selection_label"] is None


def test_the_ready_panel_renders_the_gates_number_and_the_cap():
    """THE FIX, on the arm where the contradiction was reachable.

    The ready arm must consume the discriminator and print the envelope's own
    count, not the file's. It must also print the cap: a bare residue count is
    not interpretable, and this is the screen on which the user decides whether
    to spend money.
    """
    branch = _ready_branch()
    assert "size_basis" in branch, (
        "the ready panel does not look at size_basis, so it cannot tell a "
        "contig selection from a whole-chain count and will print the file's "
        "number for a run sized on the selection"
    )
    assert "size_envelope.residue_count" in branch or "env.residue_count" in branch, (
        "the ready panel never renders the count the gate actually judged"
    )
    assert "hard_cap_target_aa" in branch, (
        "the ready panel prints a residue count with no cap beside it"
    )
    # A substring search cannot tell a live block from a dead one: rewriting
    # the guard to `if (false)` leaves every token above exactly where it was.
    # So the GUARD is read too, and it has to be the truthiness of the payload
    # field rather than a constant. This closes the one dead-branch mutation
    # reachable without a JS runtime; the others QC found (a condition false
    # only for proteina, the append moved after the POST) genuinely need one,
    # and are named as open in the commit message rather than papered over.
    guard = _guard_before(branch, "Size envelope:")
    assert guard == "v.size_envelope", (
        f"the size-envelope block is guarded on {guard!r}, not on the payload "
        f"field being present; a constant guard renders nothing while every "
        f"token this test looks for stays in the source"
    )


def test_the_two_panels_agree_on_what_they_show():
    """The AJAX panel and its server-rendered twin describe the same verdict.

    templates/components/preflight_panel.html has always rendered
    ``size_envelope.residue_count`` next to ``hard_cap_target_aa``; the AJAX
    panel rendered neither. Whichever one a user happens to hit, the number on
    screen has to be the number the gate used.
    """
    twin = (_ROOT / "templates" / "components" / "preflight_panel.html").read_text(
        encoding="utf-8")
    for token in ("size_envelope.residue_count", "size_envelope.hard_cap_target_aa"):
        assert token in twin, f"the server-rendered twin no longer shows {token}"
    branch = _ready_branch()
    assert "residue_count" in branch and "hard_cap_target_aa" in branch


# ---------------------------------------------------------------------------
# The panel must score the SAME hotspot value the submit gate will read
# ---------------------------------------------------------------------------
#
# blueprints/tools.py runs preflight twice: once for this panel, off the raw
# form, and once as the submit hard gate, off adapter.validate()'s
# inputs["hotspot_residues"]. Those two have to be the same value, or the panel
# is previewing a different run than the one the Run button launches.
#
# They diverged twice. Before the chain-prefix fix the panel parsed with a bare
# int() and silently dropped "A296", so it rendered a clean verdict for a field
# the gate then rejected. The fix routed the panel through
# tools.base.parse_hotspot_residues, which is right for the four binder tools
# and wrong for proteina, whose validate() keeps hotspot_residues BARE and
# carries the prefixed form separately under hotspot_spec — so the panel began
# applying a per-chain rule proteina's own gate does not.

_PANEL_HOTSPOT_FORMS = {
    "bindcraft":   {"preset": "pilot", "binder_length_min": "55",
                    "binder_length_max": "65", "num_designs": "2"},
    "boltzgen":    {"preset": "pilot", "binder_length_min": "55",
                    "binder_length_max": "65", "num_designs": "2"},
    "pxdesign":    {"preset": "pilot", "binder_length": "80",
                    "num_designs": "2"},
    "rfdiffusion": {"preset": "pilot", "binder_length_min": "55",
                    "binder_length_max": "65", "num_designs": "2"},
    "rfantibody":  {"preset": "pilot", "num_designs": "2"},
    "boltz2":      {"preset": "standalone",
                    "binder_sequences": "M" * 40},
    # proteina resolves its target chains from the CONTIG, not from
    # target_chain (tools/proteina/__init__.py:494-505), and validates hotspot
    # prefixes against that. Without target_input its chain set is empty and
    # every prefixed hotspot is refused — which is the same asymmetry that
    # made the panel block its own documented multi-chain flow, so the table
    # carries the contig rather than papering over it.
    "proteina":    {"preset": "protein_binder", "_has_custom_target": "1",
                    "target_input": "A1-80,B1-80",
                    "binder_length_min": "55", "binder_length_max": "65",
                    "num_designs": "2"},
}


def test_every_preflight_tool_is_covered_by_the_shape_table():
    """A new tool added to PREFLIGHT_TOOLS gets its panel/gate agreement
    checked, instead of inheriting whichever branch it happens to fall into."""
    from shared.pdb_preflight import PREFLIGHT_TOOLS

    assert set(_PANEL_HOTSPOT_FORMS) == set(PREFLIGHT_TOOLS), (
        "PREFLIGHT_TOOLS and the panel shape table have drifted: "
        f"{set(PREFLIGHT_TOOLS) ^ set(_PANEL_HOTSPOT_FORMS)}"
    )


@pytest.mark.parametrize("slug", sorted(_PANEL_HOTSPOT_FORMS))
def test_single_chain_bare_hotspots_stay_bare_for_every_tool(slug):
    """R1 across the whole table: the pre-multi-chain payload is bare ints for
    every adapter, declared chain-prefixed or not."""
    import importlib

    mod = importlib.import_module(f"tools.{slug}")
    form = dict(_PANEL_HOTSPOT_FORMS[slug])
    form.update({"target_chain": "A", "hotspot_residues": "5,7"})

    inputs, err = mod.validate(form, {})
    assert err is None, f"{slug}: {err}"
    assert inputs.get("hotspot_residues") == [5, 7], (
        f"{slug} emitted {inputs.get('hotspot_residues')!r} for a bare "
        f"single-chain field"
    )


def test_the_panel_does_not_block_proteinas_own_multichain_flow(client):
    """THE REGRESSION THIS PINS. templates/tools/proteina_form.html tells the
    user to leave target_chain at "A" and name several chains in the contig
    field instead ("For several chains, use the target region field below"),
    and gives "A113,C73" as the hotspot example.

    A panel that reads its chain set from target_chain alone calls C73 a
    hotspot on an untargeted chain, returns NEEDS_FIX, and
    preflight.js:setSubmitEnabled(!!v.ok) disables the Run button — for a
    submission the gate accepts. target_chain carries maxlength="4", so past
    two chains there is not even a value the user could type to escape it.
    """
    _login(client)
    pdb = _pdb({"A": list(range(1, 161)), "C": list(range(1, 161))})
    resp = _post_preflight(
        client,
        target_chain="A",
        target_input="A12-80,C12-80",
        hotspot_residues="A113,C73",
        target_pdb=(io.BytesIO(pdb), "ac.pdb"),
    )
    body = resp.get_json()

    # And the adapter itself accepts the very same field.
    from tools import proteina as proteina_mod
    inputs, err = proteina_mod.validate({
        "preset": "protein_binder", "_has_custom_target": "1",
        "target_chain": "A", "target_input": "A12-80,C12-80",
        "hotspot_residues": "A113,C73",
        "binder_length_min": "55", "binder_length_max": "65",
        "num_designs": "2",
    }, {})
    assert err is None, err

    assert body["ok"] is True, (
        f"panel refused a submit the adapter accepts: {body.get('reason')!r}"
    )


def test_the_panel_is_never_stricter_than_the_gate(client):
    """The invariant, stated as a property rather than a list of cases.

    A green panel over a submit the gate refuses costs a click. A RED panel
    over a submit the gate would have accepted costs the whole run: the button
    is disabled client-side and the stated reason was guessed by a function
    that does not run the adapter. So the panel may be wrong in one direction
    only, and the hotspot parse must never be the thing that flips it.
    """
    _login(client)
    pdb = _pdb({"A": list(range(1, 161)), "C": list(range(1, 161))})

    # Fields that are valid, half-typed, or malformed — the panel fires while
    # the user is still editing, so all three reach it in practice.
    for hotspots in ("A113,C73", "113", "A113", "A11", "A113,", "A113,Q",
                     "", "113,73", "A113;C73", "xyz"):
        body = _post_preflight(
            client,
            target_chain="A",
            target_input="A12-80,C12-80",
            hotspot_residues=hotspots,
            target_pdb=(io.BytesIO(pdb), "ac.pdb"),
        ).get_json()
        if body["ok"]:
            continue
        # A refusal is allowed, but it must not be one this function invented
        # about the hotspot FIELD. Size, gaps and chain checks are the gate's
        # own reasons and re-run identically at submit.
        reason = (body.get("reason") or "").lower()
        assert "does not name one of your target chains" not in reason, (
            f"hotspots={hotspots!r}: panel blocked Run on its own hotspot "
            f"parse: {body.get('reason')!r}"
        )


@pytest.mark.parametrize("slug", sorted(_PANEL_HOTSPOT_FORMS))
def test_what_each_adapter_does_with_a_chain_prefixed_hotspot(slug):
    """The landscape the panel has to live with, pinned so it cannot shift
    silently underneath it.

    Three behaviours, not two, which is why a single "parse it like the
    adapters do" rule kept failing:
      - the four binder tools accept the prefixed form and EMIT it
      - proteina accepts it but emits bare ints, carrying the prefixed form
        separately under hotspot_spec
      - rfantibody and boltz2 reject it outright
    """
    import importlib

    mod = importlib.import_module(f"tools.{slug}")
    form = dict(_PANEL_HOTSPOT_FORMS[slug])
    form.update({"target_chain": "A B", "hotspot_residues": "A5,B7"})
    inputs, err = mod.validate(form, {})

    if slug in {"rfantibody", "boltz2"}:
        assert err is not None, (
            f"{slug} now accepts chain-prefixed hotspots; the panel assumes "
            f"it does not"
        )
        return

    assert err is None, f"{slug}: {err}"
    emitted = inputs.get("hotspot_residues") or []
    if slug == "proteina":
        assert emitted == [5, 7], emitted
        assert inputs.get("hotspot_spec") == ["A5", "B7"], (
            "proteina moved the prefixed form off hotspot_spec"
        )
    else:
        assert emitted == ["A5", "B7"], f"{slug} emitted {emitted!r}"
