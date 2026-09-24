"""Every numbering change the run makes to a user's upload has to be on screen.

WHY THIS FILE EXISTS. The recurring defect class in this repo is numbering: a
bare hotspot steering one protomer of several, a contig that has to cover the
chain, a residue the user named that no longer exists under that number after
cleanup. In each case the user had no way to see what we did to their file.

Two transformations were invisible by construction:

  1. ``normalize_for_boltzgen`` / ``normalize_for_pxdesign`` renumber the target
     1..N per chain, in-container. The preflight preview computed no
     ``renumber_map`` at all (``shared/pipeline_normalize.py``, dry-run arm), so
     no surface could have printed it even if one wanted to.
  2. A hotspot typed without a chain letter is attributed to the FIRST named
     chain by ``tools/base.py::parse_hotspot_residues`` and by
     ``shared/pdb_preflight.py::_check_hotspots``. On a multi-chain target that
     silently picks one protomer.

And boltz2's hotspots are 1-based sequence positions, not author residue
numbers, which reads as a numbering change to anyone who typed a number off
their own PDB.

The tests below fail if a note stops rendering OR names the wrong before/after.
The fixtures deliberately have a non-1 start, an insertion code, and multiple
chains, because every one of those is a case where "before" and "after" differ.
"""

from __future__ import annotations

import io
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.pdb_preflight import preflight_for_tool
from shared.pipeline_normalize import normalize_for_boltzgen


# ---------------------------------------------------------------------------
# Fixtures: real PDB text, backbone-complete, so the normalizer keeps it
# ---------------------------------------------------------------------------

def _pdb(chains: dict) -> bytes:
    """Backbone-complete ALA residues per chain.

    ``chains`` maps a chain id to a list of residue ids, each either an int or
    an ``(int, icode)`` pair. The insertion-code form is what makes two
    residues share a resSeq, the case where the renumber map's last-write-wins
    behaviour is observable at all.
    """
    lines = ["HEADER    SYNTHETIC\n"]
    serial = 0
    for chain_id, resids in chains.items():
        for i, rid in enumerate(resids):
            rn, icode = rid if isinstance(rid, tuple) else (rid, " ")
            xb = float(i * 4.0)
            for nm, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 3.0)]:
                serial += 1
                elem = nm[0].rjust(2)
                lines.append(
                    f"ATOM  {serial:5d}  {nm:<3s} ALA "
                    f"{chain_id:1s}{rn:4d}{icode:1s}   "
                    f"{xb + off:8.3f}{1.0:8.3f}{1.0:8.3f}"
                    f"{1.0:6.2f}{10.0:6.2f}          {elem}\n"
                )
    lines.append("END\n")
    return "".join(lines).encode()


# Non-1 start on both chains, and two different numbering ranges, so a note
# that quoted the wrong chain's endpoints would fail here. 150 residues each:
# above every tool's min_target_aa (30) and 300 total, inside boltzgen's cap.
_A = list(range(100, 250))
_B = list(range(500, 650))
_TWO_CHAIN = _pdb({"A": _A, "B": _B})

# One chain, an insertion code in the middle: 140, 140A, 141... Two residues
# share resSeq 140, so the renumber map can only hold one of them and which
# one it holds is a real behavioural detail, not a formality.
_ICODE = _pdb({"A": (
    list(range(100, 141)) + [(140, "A")] + list(range(141, 250))
)})


# ---------------------------------------------------------------------------
# 1. The preview map has to BE the map the run will apply
# ---------------------------------------------------------------------------

def _preview_and_written(pdb_bytes: bytes, chain: str) -> tuple[dict, dict]:
    src = tempfile.NamedTemporaryFile(suffix=".pdb", delete=False)
    out = tempfile.NamedTemporaryFile(suffix=".pdb", delete=False)
    try:
        src.write(pdb_bytes)
        src.close()
        out.close()
        preview = normalize_for_boltzgen(src.name, None, target_chain=chain)
        written = normalize_for_boltzgen(src.name, out.name, target_chain=chain)
        return dict(preview.renumber_map), dict(written.renumber_map)
    finally:
        for f in (src.name, out.name):
            try:
                os.unlink(f)
            except OSError:
                pass


@pytest.mark.parametrize("pdb_bytes,chain", [
    pytest.param(_TWO_CHAIN, "A B", id="two-chains-non-1-start"),
    pytest.param(_ICODE, "A", id="insertion-code"),
])
def test_preview_renumber_map_equals_the_written_map(pdb_bytes, chain):
    """The panel prints the PREVIEW map; the container applies the WRITTEN one.

    If they can differ, the form is free to promise a number the run will not
    use — which is worse than saying nothing. The insertion-code row is the
    case that forced the preview to walk an ordered list rather than the
    ``keep_residues`` dict: keyed on ``(chain, resnum)`` that dict holds ONE
    entry for 140 and 140A, so deriving the map from it yields first-wins
    where the writing pass yields last-wins.
    """
    preview, written = _preview_and_written(pdb_bytes, chain)
    assert written, "the written pass produced no map; fixture is not renumbered"
    assert preview == written


def test_the_insertion_code_pair_is_the_reason_the_two_could_differ():
    """Guards the row above from becoming vacuous.

    If the fixture ever stops carrying two residues at one resSeq, the
    parametrised case still passes while testing nothing about ordering.
    """
    preview, _ = _preview_and_written(_ICODE, "A")
    # 151 residues written, 150 distinct resSeq values -> the map is one
    # shorter than the chain, and 140 resolves to the LATER of the pair.
    assert len(preview) == 150
    assert preview[("A", 140)] == 42  # 100..139 is 40, then 140, then 140A


# ---------------------------------------------------------------------------
# 2. The form's panel, through the real route
# ---------------------------------------------------------------------------

_PANEL_TOOLS = ("boltzgen", "proteina", "boltz2")


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


def _preflight(client, slug, pdb_bytes, **extra):
    data = {
        "target_chain": "A",
        "hotspot_residues": "",
        "target_pdb": (io.BytesIO(pdb_bytes), "t.pdb"),
    }
    data.update(extra)
    _login(client)
    with patch("blueprints.tools.load_user_context", return_value=_ctx()):
        resp = client.post(
            f"/tools/{slug}/preflight", data=data,
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    return resp.get_json()


def test_a_renumbering_tool_states_the_before_and_after_per_chain(client):
    body = _preflight(
        client, "boltzgen", _TWO_CHAIN, target_chain="A B",
    )
    assert body["ok"] is True, body.get("reason")
    notes = body["remap"]["notes"]
    assert len(notes) == 2, notes
    joined = " ".join(notes)
    # The real endpoints of each chain, not a generic "we renumber" sentence.
    assert "your A100 becomes residue 1" in joined
    assert "your A249 becomes residue 150" in joined
    assert "your B500 becomes residue 1" in joined
    assert "your B649 becomes residue 150" in joined


def test_a_bare_hotspot_is_shown_resolved_to_the_chain_it_lands_on(client):
    """The case that has burned us: 150 is chain A's 150, not chain B's.

    Both halves are asserted — the chain the token was attributed to AND the
    number it becomes — because either one alone is the defect.
    """
    body = _preflight(
        client, "boltzgen", _TWO_CHAIN,
        target_chain="A B", hotspot_residues="150, B520",
    )
    assert body["ok"] is True, body.get("reason")
    pairs = {h["typed"]: h for h in body["remap"]["hotspots"]}
    assert set(pairs) == {"150", "B520"}
    # 150 is the 51st residue of chain A (100..249).
    assert pairs["150"]["means"] == "A51"
    assert "first of the chains you named" in pairs["150"]["why"]
    # 520 is the 21st residue of chain B (500..649), and B was NOT assumed.
    assert pairs["B520"]["means"] == "B21"
    assert "first of the chains" not in pairs["B520"]["why"]


def test_a_tool_that_preserves_numbering_discloses_only_the_attribution(client):
    """proteina keeps the upload's numbering (rfdiffusion/rfantibody too).

    So there is no renumbering to disclose and a note claiming one would be
    false. The bare-hotspot attribution still applies and still has to show.
    """
    body = _preflight(
        client, "proteina", _TWO_CHAIN,
        target_chain="A B", hotspot_residues="150",
        target_input="A100-249,B500-649",
    )
    assert body["ok"] is True, body.get("reason")
    assert body["remap"]["notes"] == []
    (h,) = body["remap"]["hotspots"]
    # Same number, explicit chain: nothing was renumbered.
    assert h["means"] == "A150"
    assert "first of the chains you named" in h["why"]


def test_nothing_is_disclosed_when_nothing_changes(client):
    """A single-chain target with a chain-prefixed hotspot on a
    numbering-preserving tool has no remap, and the panel must not invent one
    — a block that always renders teaches users to ignore it."""
    body = _preflight(
        client, "proteina", _pdb({"A": _A}),
        target_chain="A", hotspot_residues="A150",
    )
    assert body["ok"] is True, body.get("reason")
    assert body["remap"] is None


def test_boltz2_names_the_residue_a_typed_position_lands_on(client):
    """boltz2 hotspots are positions counted from 1, not author numbers.

    Chain A starts at 100, so position 12 is residue 111 — and a user who
    typed 12 off their own PDB numbering meant something else entirely.
    """
    body = _preflight(
        client, "boltz2", _pdb({"A": _A}),
        target_chain="A", hotspot_residues="12",
    )
    assert body["ok"] is True, body.get("reason")
    (h,) = body["remap"]["hotspots"]
    assert h["typed"] == "12"
    assert h["means"] == "A111"
    assert "1-150" in " ".join(body["remap"]["notes"])


# ---------------------------------------------------------------------------
# 3. The panel actually prints it — both panels
# ---------------------------------------------------------------------------

def test_the_server_rendered_panel_prints_the_remap(app):
    """``shared/pdb_intake.py::_verdict_to_json`` can carry the block and the
    route can return it while the template drops it on the floor. The job
    detail page renders the stored verdict through this same macro
    (templates/job_detail.html), so this covers the after-payment surface too.
    """
    verdict = preflight_for_tool(
        "boltzgen", _TWO_CHAIN, target_chain="A B", hotspots=[150],
    )
    from shared.pdb_intake import _verdict_to_json
    payload = _verdict_to_json(verdict, "t.pdb")
    assert payload["remap"] is not None
    with app.test_request_context("/"):
        from flask import render_template_string
        html = render_template_string(
            '{% from "components/preflight_panel.html" import preflight_panel %}'
            "{{ preflight_panel(v, tool_slug='boltzgen', summary=True) }}",
            v=payload,
        )
    assert "Numbering in this run" in html
    assert "your A100 becomes residue 1" in html
    # The typed token and what it resolves to, side by side.
    assert ">150<" in html and ">A51<" in html


def _iggm_html(app, *, positions, pdb_resnums) -> str:
    job = SimpleNamespace(id="job-1", inputs={}, preset=None, result={
        "antigen_chain": "A",
        "epitope_positions": positions,
        "epitope_pdb_resnums": pdb_resnums,
        "designs": [{
            "rank": 0, "name": "d0", "pdb_key": "d0.pdb",
            "n_epitope_contacts": 2, "n_epitope": len(positions),
            "contacted_positions": positions[:1],
        }],
    })
    with app.test_request_context("/"):
        return app.jinja_env.get_template("tools/iggm_results.html").render(
            job=job,
        )


def test_the_iggm_epitope_table_shows_the_numbers_the_user_typed(app):
    """The one transformation that happens in-container, not at preflight.

    ``tools/iggm/run_pipeline.py::convert_epitope`` turns each epitope residue
    number into a 1-based position along the antigen chain, and the result
    already carries both forms — so this is a rendering fix, not a rebuild.
    Before it, the column header printed the converted position alone: a user
    who asked for 241 saw a column called 118.
    """
    html = _iggm_html(app, positions=[42, 118], pdb_resnums=[141, 241])
    assert "A141" in html and "A241" in html
    assert "pos 42" in html and "pos 118" in html


def test_the_iggm_table_falls_back_when_the_two_lists_disagree(app):
    """A result written by a build that stored a different number of author
    numbers than positions cannot be index-aligned, and labelling the columns
    from it would mislabel every one. The guard drops to the positions."""
    html = _iggm_html(app, positions=[42, 118], pdb_resnums=[241])
    assert "A241" not in html
    assert "pos 42" not in html


def test_the_ajax_panel_renders_the_same_two_fields():
    """static/js/preflight.js builds the panel client-side on upload, so a
    block added only to the Jinja twin is invisible on the form — which is the
    surface this package is about. Read as source because the repo carries no
    JS runner; the keys are the ones _verdict_to_json ships.
    """
    from tests.test_candidate_table_js_contract import _lex
    import pathlib
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "static" / "js" / "preflight.js"
    ).read_text(encoding="utf-8")
    js, _ = _lex(src)  # comments stripped: prose cannot satisfy this
    assert "v.remap" in js
    assert "h.typed" in js and "h.means" in js
    assert "remap.notes" in js
