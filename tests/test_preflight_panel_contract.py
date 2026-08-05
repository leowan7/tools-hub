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
# -> {"contigInput": "target_input"}. Anchored on `const` + `form.querySelector`
# so a mention in prose cannot answer for a binding (the comment stripping
# already removed prose, and this is the belt to that's braces).
_FIELD_OF = dict(
    (var, field)
    for var, field in re.findall(
        r"const\s+([A-Za-z][A-Za-z0-9]*)\s*=\s*"
        r"form\.querySelector\(\s*'input\[name=\"([a-z_]+)\"\]'\s*\)",
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


def test_the_comment_stripping_removed_the_prose_that_would_fake_a_pass():
    """Precondition, and NOT the one test_candidate_table_js_contract makes.

    That file proves its lexer safe by asserting no slash survives in code
    position, which works because candidate_table.js has no regex literal.
    preflight.js has two (`/\\s+/g`), so the empty-list proof is unavailable
    here and asserting it would be a permanent false alarm.

    Prove the property this file actually depends on instead. The failure mode
    that matters is stripping too LITTLE — a token left sitting in a comment
    answering for code. (Stripping too much fails in the safe direction: the
    token disappears and the tests below go red.) The fix ships comments that
    say `target_input` and `input[name="target_input"]` in prose, so the raw
    file would satisfy the searches below whether or not the code was ever
    wired. Asserting that the stripped source holds strictly fewer of them
    than the raw source decides exactly that, on this file's real content.
    """
    raw_hits = _JS_SOURCE.count("target_input")
    code_hits = _JS.count("target_input")
    assert raw_hits > code_hits, (
        "comment stripping removed no `target_input` mention, so either _lex "
        "silently stopped stripping or the explanatory comments are gone. "
        "Either way the searches below can now be satisfied by prose."
    )
    assert code_hits > 0, "target_input survives only in comments"
    # A phrase that exists ONLY in a comment. If it is still here, the stripper
    # is not running over the region the tokens live in.
    assert "THE PANEL AND THE SUBMIT GATE MUST SIZE" not in _JS


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
    assert _FIELD_OF[var] == "target_input"


def test_the_posted_key_is_the_key_the_server_parses():
    """Drive the extracted key through the PRODUCTION parser, not a copy.

    ``preflight_target_segments`` is what the route calls. Feeding it a form
    keyed by whatever the JS decided to send is what makes a rename on either
    side of this boundary fail here rather than in production.
    """
    from shared.pdb_intake import preflight_target_segments

    key = next(k for k, v in _APPENDED_FROM.items()
               if _FIELD_OF.get(v) == "target_input")
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


# Every panel form this file GETs. `tool_enabled` is fail-closed on a missing
# env var, so without these the "no other form carries the field" test would
# read 404 for every tool and pass by never rendering anything.
_PANEL_TOOLS = (
    "proteina", "rfdiffusion", "bindcraft", "boltzgen", "rfantibody",
    "pxdesign",
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


def test_the_proteina_form_renders_the_field_the_js_queries(client):
    """The RENDERED artifact, not the template source. A field that only
    exists in a Jinja branch the page never takes is a field the browser never
    sees, and `querySelector` would return null with no error anywhere."""
    _login(client)
    resp = _get_form(client, "proteina")
    assert resp.status_code == 200
    body = _squash(resp.get_data(as_text=True))
    field = _FIELD_OF[_APPENDED_FROM["target_input"]]
    assert f'name="{field}"' in body, (
        f"preflight.js queries input[name=\"{field}\"] and the proteina form "
        f"does not render it"
    )
    # And the panel is on this page at all — the script bails without it.
    assert 'id="preflight-panel"' in body


def test_no_other_panel_form_carries_the_contig_field(client):
    """BLAST RADIUS. preflight.js is shared by eight tool forms. The contig is
    proteina's alone, so `querySelector` returns null everywhere else and a
    null appends nothing — their requests stay byte-identical. If another form
    ever grows a `target_input`, that tool starts sending a contig to a size
    envelope that will parse it with PROTEINA's parser, and this test is the
    thing that makes someone think about it first."""
    _login(client)
    field = _FIELD_OF[_APPENDED_FROM["target_input"]]
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
    key = _FIELD_OF[_APPENDED_FROM["target_input"]]
    body = _post_preflight(client, **{key: _CONTIG}).get_json()
    assert body["ok"] is True, body.get("reason")
    # 130, not 400: the number the panel reports is the SELECTION's. Asserted
    # on the count rather than on a `size_basis` flag because the JSON block
    # ships neither `size_basis` nor `selection_label` (shared/pdb_intake.py
    # ::_verdict_to_json), and the count is the discriminator anyway — nothing
    # but the contig can move it from 400 to 130.
    assert body["size_envelope"]["residue_count"] == 130
