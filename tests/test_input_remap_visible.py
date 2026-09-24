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

    ``chains`` maps a chain id to a list of residue ids, each either an int, an
    ``(int, icode)`` pair, or an ``(int, icode, atom_names)`` triple. The
    insertion-code form is what makes two residues share a resSeq, the case
    where the renumber map's last-write-wins behaviour is observable at all.
    The triple form writes a partial backbone, which is how a residue gets
    dropped by ``drop_zero_backbone``.
    """
    lines = ["HEADER    SYNTHETIC\n"]
    serial = 0
    for chain_id, resids in chains.items():
        for i, rid in enumerate(resids):
            if not isinstance(rid, tuple):
                rid = (rid, " ")
            rn, icode = rid[0], rid[1]
            names = rid[2] if len(rid) > 2 else ("N", "CA", "C", "O")
            xb = float(i * 4.0)
            for nm, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 3.0)]:
                if nm not in names:
                    continue
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

# The case that decides whether the preview may be quoted to a user at all.
# Residue 141 has no O, so the filter loop drops it -- but the writer's
# selector keys on (chain, resnum) with the insertion code stripped, and 141A
# survives, so 141 is written anyway and the renumber pass counts it. A
# preview built from the filter loop's own decisions is one short, and every
# residue after 141 is then quoted one lower than the run will use.
_ICODE_RESURRECT = _pdb({"A": (
    list(range(100, 141))
    + [(141, " ", ("N", "CA", "C"))]
    + [(141, "A")]
    + list(range(142, 250))
)})


def _pdb_zero_atom_resurrection() -> bytes:
    """A resurrected residue that ends up written as no lines at all.

    Residue 102 is altloc'd A/B on every backbone atom, B at the higher
    occupancy, so the writer's chosen altloc for each of its atom names is B.
    A water shares chain, resSeq AND insertion code with it, so the water's
    single blank-altloc atom hits that same chosen-altloc lookup and loses it.
    The water is dropped by the water filter, but the writer's residue test
    strips the hetflag, so it is accepted as a residue and rejected atom by
    atom -- written as nothing, and therefore absent from the renumbered file.
    A preview that stops at the residue test counts it anyway.
    """
    lines = ["HEADER    SYNTHETIC\n"]
    serial = 0
    for i, rn in enumerate(range(100, 105)):
        xb = float(i * 4.0)
        alts = [("A", 0.4), ("B", 0.6)] if rn == 102 else [(" ", 1.0)]
        for nm, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 3.0)]:
            for alt, occ in alts:
                serial += 1
                lines.append(
                    f"ATOM  {serial:5d}  {nm:<3s}{alt:1s}ALA "
                    f"A{rn:4d}    "
                    f"{xb + off:8.3f}{1.0:8.3f}{1.0:8.3f}"
                    f"{occ:6.2f}{10.0:6.2f}          {nm[0]:>2s}\n"
                )
    serial += 1
    lines.append(
        f"HETATM{serial:5d}  O   HOH "
        f"A{102:4d}    "
        f"{99.0:8.3f}{99.0:8.3f}{99.0:8.3f}"
        f"{1.0:6.2f}{10.0:6.2f}           O\n"
    )
    lines.append("END\n")
    return "".join(lines).encode()


_ZERO_ATOM_RESURRECT = _pdb_zero_atom_resurrection()


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
    pytest.param(
        _ICODE_RESURRECT, "A",
        id="icode-sibling-resurrects-a-dropped-residue",
    ),
    pytest.param(
        _ZERO_ATOM_RESURRECT, "A",
        id="resurrected-residue-loses-every-atom",
    ),
])
def test_preview_renumber_map_equals_the_written_map(pdb_bytes, chain):
    """The panel prints the PREVIEW map; the container applies the WRITTEN one.

    If they can differ, the form is free to promise a number the run will not
    use — which is worse than saying nothing. Both insertion-code rows are
    cases where a plausible preview gets it wrong: the first because
    ``keep_residues`` is keyed on ``(chain, resnum)`` and so holds ONE entry
    for 140 and 140A (deriving the map from it yields first-wins where the
    writing pass yields last-wins), the second because that same stripped key
    lets a residue the filters dropped be written anyway.
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


def test_a_dropped_residue_its_icode_sibling_revives_still_takes_a_number():
    """Guards the resurrect row from becoming vacuous the same way.

    The fixture drops 141 (no O) and keeps 141A. If the preview counted only
    the residues the filter loop chose, 142 would be quoted as 43 while the
    run makes it 44, and every residue after it would be off by one too.
    """
    preview, written = _preview_and_written(_ICODE_RESURRECT, "A")
    # 100..140 is 41 residues, then the revived 141 at 42 and 141A at 43.
    assert preview[("A", 142)] == 44
    assert written[("A", 142)] == 44


def test_a_resurrected_residue_with_no_surviving_atom_takes_no_number():
    """Guards the zero-atom row from becoming vacuous.

    The water shares resSeq 102 with an altloc'd polymer residue and comes
    last in the file, so a preview that counted it would hand ``("A", 102)``
    the LAST index instead of the second. Asserting the value, not just
    equality, is what fails if the fixture stops reproducing the case.
    """
    text = _ZERO_ATOM_RESURRECT.decode()
    assert "HOH A 102" in text, "fixture no longer shares a resSeq with the water"
    assert "BALA A 102" in text, "fixture residue 102 is no longer altloc'd"
    preview, written = _preview_and_written(_ZERO_ATOM_RESURRECT, "A")
    # Five polymer residues, 100..104, and the water counted as none of them:
    # 102 is the third, not the sixth a residue-test-only walk would give it.
    assert len(written) == 5
    assert preview[("A", 102)] == 3
    assert written[("A", 102)] == 3


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
