"""/prep: the free target-prep page (blueprints/prep.py, templates/prep.html)."""

from __future__ import annotations

import io
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_AA = ["ALA", "GLY", "SER", "LEU", "LYS"]


def _pdb(chains: dict[str, int]) -> bytes:
    """Backbone-only PDB, residues 1..n per chain."""
    lines, serial = [], 1
    for chain, n in chains.items():
        for i in range(1, n + 1):
            for j, atom in enumerate(("N", "CA", "C", "O")):
                x = 3.8 * i + 0.5 * j
                lines.append(
                    f"ATOM  {serial:5d}  {atom:<3} {_AA[i % 5]} {chain}{i:4d}    "
                    f"{x:8.3f}{0.0:8.3f}{(ord(chain) - 65) * 20.0:8.3f}"
                    f"  1.00 20.00           {atom[0]}"
                )
                serial += 1
        lines.append("TER")
    lines.append("END")
    return ("\n".join(lines) + "\n").encode()


PDB_AB = _pdb({"A": 30, "B": 20})


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app
    from shared.feature_flags import flag_name
    from tools import base as tool_base

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    for a in tool_base.all_adapters():
        monkeypatch.setenv(flag_name(a.slug), "on")
    return flask_app


@pytest.fixture
def anon(app):
    return app.test_client()


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_email"] = "prep@example.com"
    return c


def _derived():
    from scout.handoff import VALID_HANDOFF_TOOLS
    from shared.pdb_preflight import PREFLIGHT_TOOLS

    return sorted(VALID_HANDOFF_TOOLS & PREFLIGHT_TOOLS)


def _multi_chain_tool():
    from shared.pdb_preflight import multi_chain_refusal

    ok = [s for s in _derived() if not multi_chain_refusal(s, "A B")]
    if not ok:
        pytest.skip("no derived tool accepts multi-chain targets")
    return ok[0]


def _post(client, url, pdb=PDB_AB, name="1abc.pdb", **form):
    data = dict(form)
    data["target_pdb"] = (io.BytesIO(pdb), name)
    return client.post(url, data=data, content_type="multipart/form-data")


def _ca(pdb_text: str) -> list[tuple[str, int]]:
    return [(ln[21], int(ln[22:26])) for ln in pdb_text.splitlines()
            if ln.startswith("ATOM") and ln[12:16].strip() == "CA"]


# --- route renders -------------------------------------------------------

def test_signed_in_page_offers_every_derived_tool_and_no_submit(client):
    html = client.get("/prep").get_data(as_text=True)
    start = html.index('id="prep-form"')
    form = html[start:html.index("</form>", start)]
    assert re.findall(r'<option value="([^"]+)"', form) == _derived()
    assert 'type="submit"' not in form
    assert 'data-tool="%s"' % _derived()[0] in form


def test_derived_tool_list_is_not_empty():
    # An empty intersection would render a page that can hand off nowhere.
    assert _derived()


def test_disabled_tool_is_not_offered(client, monkeypatch):
    from shared.feature_flags import flag_name

    off = _derived()[0]
    monkeypatch.setenv(flag_name(off), "off")
    html = client.get("/prep").get_data(as_text=True)
    assert f'value="{off}"' not in html
    r = _post(client, "/prep/handoff", tool=off, target_chain="A")
    assert r.status_code == 400


# --- anonymous access ----------------------------------------------------

def test_anonymous_page_has_no_control_that_can_post(anon):
    r = anon.get("/prep")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Target prep" in html
    for needle in ('id="prep-form"', "/prep/trim", "/prep/handoff",
                   "/prep/fetch", "/prep/search", "preflight.js"):
        assert needle not in html, needle
    assert not re.search(r'method=["\']?post', html, re.I)
    assert not re.search(r'type=["\']?submit', html, re.I)


@pytest.mark.parametrize(("method", "url"), [
    ("get", "/prep/search?q=pdl1"),
    ("get", "/prep/fetch/1ABC"),
    ("post", "/prep/trim"),
    ("post", "/prep/handoff"),
])
def test_anonymous_api_redirects_to_login(anon, method, url):
    with patch("shared.rcsb.requests") as req:
        r = getattr(anon, method)(url)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    req.post.assert_not_called()
    req.get.assert_not_called()


# --- keyword search ------------------------------------------------------

def _resp(status=200, payload=None):
    m = MagicMock(status_code=status)
    m.json.return_value = payload or {}
    m.raise_for_status.side_effect = None if status < 400 else Exception(status)
    return m


def test_search_returns_ids_with_titles(client):
    search = _resp(payload={"result_set": [{"identifier": "3BIK"}, {"identifier": "5J89"}]})
    titles = _resp(payload={"data": {"entries": [
        {"rcsb_id": "3BIK", "struct": {"title": "PD-1/PD-L1 complex"}},
    ]}})
    with patch("shared.rcsb.requests.post", side_effect=[search, titles]) as post:
        r = client.get("/prep/search?q=PD-L1")
    assert r.get_json() == {"results": [
        {"id": "3BIK", "title": "PD-1/PD-L1 complex"},
        {"id": "5J89", "title": ""},
    ]}
    body = post.call_args_list[0].kwargs["json"]
    assert body["query"]["service"] == "full_text"
    assert body["query"]["parameters"]["value"] == "PD-L1"


def test_search_no_hits_is_empty_not_error(client):
    with patch("shared.rcsb.requests.post", return_value=_resp(204)):
        assert client.get("/prep/search?q=zzzz").get_json() == {"results": []}


def test_search_outage_is_502(client):
    with patch("shared.rcsb.requests.post", side_effect=ConnectionError):
        r = client.get("/prep/search?q=pdl1")
    assert r.status_code == 502 and "unavailable" in r.get_json()["error"]


# --- fetch by id ---------------------------------------------------------

def _stream(status, body=b""):
    m = MagicMock(status_code=status)
    m.iter_content.return_value = [body[i:i + 100] for i in range(0, len(body), 100)]
    m.__enter__.return_value = m
    return m


def test_fetch_falls_back_to_cif(client):
    with patch("shared.rcsb.requests.get",
               side_effect=[_stream(404), _stream(200, b"data_1ABC\n" * 20)]):
        r = client.get("/prep/fetch/1abc")
    assert r.status_code == 200
    assert "1ABC.cif" in r.headers["Content-Disposition"]


def test_fetch_rejects_bad_id_without_calling_rcsb(client):
    with patch("shared.rcsb.requests.get") as get:
        assert client.get("/prep/fetch/abc").status_code == 400
    get.assert_not_called()


def test_fetch_over_cap_is_413(client, monkeypatch):
    monkeypatch.setattr("blueprints.prep.MAX_UPLOAD_BYTES", 150)
    with patch("shared.rcsb.requests.get", return_value=_stream(200, b"x" * 400)):
        assert client.get("/prep/fetch/1abc").status_code == 413


# --- trim download -------------------------------------------------------

def test_trim_keeps_only_selected_chain(client):
    r = _post(client, "/prep/trim", target_chain="B")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert 'filename=1abc_trimmed.pdb' in r.headers["Content-Disposition"]
    ca = _ca(r.get_data(as_text=True))
    assert ca == [("B", i) for i in range(1, 21)]


def test_trim_range_overrides_chain_list(client):
    r = _post(client, "/prep/trim", target_chain="B", target_input="A5-10")
    assert _ca(r.get_data(as_text=True)) == [("A", i) for i in range(5, 11)]


@pytest.mark.parametrize(("form", "needle"), [
    ({"target_chain": "Z"}, "Z"),
    ({"target_chain": ""}, "at least one chain"),
    ({"target_chain": "A", "target_input": "A200-300"}, "no residues"),
])
def test_trim_refusals(client, form, needle):
    r = _post(client, "/prep/trim", **form)
    assert r.status_code == 400
    assert needle in r.get_json()["error"]


def test_trim_without_a_file(client):
    r = client.post("/prep/trim", data={"target_chain": "A"})
    assert r.status_code == 400


# --- handoff -------------------------------------------------------------

@pytest.fixture
def staged():
    ctx = SimpleNamespace(user_id="u-1")
    with patch("blueprints.prep.load_user_context", return_value=ctx), \
         patch("blueprints.prep.upload_input", return_value="u-1/prep-handoff-x/1abc_trimmed.pdb") as up, \
         patch("blueprints.prep.create_handoff",
               return_value=SimpleNamespace(id="ho-9")) as ch:
        yield up, ch


@pytest.mark.parametrize("slug", _derived())
def test_handoff_payload_per_tool(client, staged, slug):
    up, ch = staged
    r = _post(client, "/prep/handoff", tool=slug, target_chain="A",
              target_input="A3-20", hotspot_residues="A5,A12")
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"redirect": f"/tools/{slug}?handoff=ho-9"}
    staged_bytes = up.call_args.kwargs["data"]
    assert _ca(staged_bytes.decode()) == [("A", i) for i in range(3, 21)]
    assert up.call_args.kwargs["user_id"] == "u-1"
    assert up.call_args.kwargs["job_id"].startswith("prep-handoff-")
    kw = ch.call_args.kwargs
    assert kw["target_chain"] == "A"
    assert kw["hotspot_residues"] == [5, 12]
    assert kw["pdb_storage_path"] == "u-1/prep-handoff-x/1abc_trimmed.pdb"
    assert kw["pdb_filename"] == "1abc_trimmed.pdb"


def test_handoff_refuses_tool_outside_derived_set(client, staged):
    up, _ = staged
    r = _post(client, "/prep/handoff", tool="mpnn", target_chain="A")
    assert r.status_code == 400
    up.assert_not_called()


def test_handoff_refuses_hotspots_on_multichain_selection(client, staged):
    up, ch = staged
    slug = _multi_chain_tool()
    r = _post(client, "/prep/handoff", tool=slug, target_chain="A B",
              hotspot_residues="B5")
    assert r.status_code == 400
    assert "cannot be handed off yet" in r.get_json()["error"]
    up.assert_not_called()
    ch.assert_not_called()


def test_handoff_multichain_without_hotspots_passes_both_chains(client, staged):
    _, ch = staged
    slug = _multi_chain_tool()
    r = _post(client, "/prep/handoff", tool=slug, target_chain="A B")
    assert r.status_code == 200, r.get_json()
    assert ch.call_args.kwargs["target_chain"] == "A B"
    assert ch.call_args.kwargs["hotspot_residues"] == []


def test_handoff_refuses_hotspot_outside_trim(client, staged):
    up, _ = staged
    r = _post(client, "/prep/handoff", tool=_derived()[0], target_chain="A",
              target_input="A3-20", hotspot_residues="A25")
    assert r.status_code == 400 and "outside" in r.get_json()["error"]
    up.assert_not_called()


def test_handoff_honours_multi_chain_capability(client, staged):
    from shared.pdb_preflight import multi_chain_refusal

    blocked = [s for s in _derived() if multi_chain_refusal(s, "A B")]
    if not blocked:
        pytest.skip("every derived tool accepts multi-chain targets")
    r = _post(client, "/prep/handoff", tool=blocked[0], target_chain="A B")
    assert r.status_code == 400
    assert r.get_json()["error"] == multi_chain_refusal(blocked[0], "A B")


# --- rail and /tools -----------------------------------------------------

def test_rail_links_target_prep(app):
    from shared.sidebar_nav import sidebar_groups

    with app.test_request_context("/"):
        overview = sidebar_groups()[0]["items"]
    assert {"label": "Target prep", "href": "/prep"} in overview


def test_tools_page_links_target_prep(anon):
    assert 'href="/prep"' in anon.get("/tools").get_data(as_text=True)


# --- AlphaFold swap ------------------------------------------------------

def test_prep_panel_opts_out_of_the_alphafold_swap(client):
    """The swap empties the file input and parks the model in
    reuse_pdb_token, which /prep never reads: trim and handoff would then
    refuse with "Load a target first". Source check, like
    test_input_remap_visible.py's, because the repo has no runner for
    preflight.js."""
    import pathlib

    from tests.test_candidate_table_js_contract import _lex

    html = client.get("/prep").get_data(as_text=True)
    assert 'data-no-alphafold="1"' in html
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "static" / "js" / "preflight.js").read_text(encoding="utf-8")
    js, _ = _lex(src)
    assert "if (panel.dataset.noAlphafold) v = Object.assign({}, v, { alphafold: null });" in js
    assert "reuse_pdb_token" not in (pathlib.Path(__file__).resolve().parents[1]
                                     / "blueprints" / "prep.py").read_text(encoding="utf-8")
