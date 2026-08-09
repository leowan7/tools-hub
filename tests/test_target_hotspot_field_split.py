"""ONE shared hotspot field cannot carry a chain prefix, so proteina has its own.

The shape being pinned here:

    hotspot_residues   the SHARED launch field, posted to every selected tool.
                       PLAIN INTEGERS ONLY, forever. rfdiffusion, bindcraft,
                       boltzgen and pxdesign refuse a token naming a chain the
                       run does not target; tools/rfantibody parses it with a
                       bare ``int(tok)`` and refuses a prefix on ANY chain. The
                       launch route is all-or-nothing, so one prefixed token in
                       this field kills the whole launch.

    chain_hotspots     proteina's OWN field (``proteina__chain_hotspots`` on the
                       multi-tool launch screen, ``chain_hotspots`` on the
                       campaign form). The only input that carries a protomer.
                       Preferred by the adapter, with the shared field as a
                       fallback so a single-chain co-launch driven entirely from
                       the shared field is unchanged.

    hotspot_spec       design_targets' text[] column (migration 0041). Written
                       ONLY by shared.targets.enrich_target_hotspot_spec, from a
                       run that named its chains, and only as a strictly-more-
                       specific restatement of what the target already stores.

Executed evidence for the first paragraph is
``test_every_campaign_tool_launches_against_a_target_carrying_hotspot_spec`` in
tests/test_target_multi_launch_routes.py, which drives the real
``_collect_launch_specs`` for all six tools.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.targets import DesignTarget, target_defaults_for_form


def _enrich(*args):
    """``shared.targets.enrich_target_hotspot_spec``, imported at CALL time.

    Local so that a tree without the function fails each enrichment test on its
    own line rather than failing to collect the whole module — which would hide
    what every other test here is actually asserting.
    """
    from shared.targets import enrich_target_hotspot_spec  # noqa: PLC0415

    return enrich_target_hotspot_spec(*args)

_REPO = Path(__file__).resolve().parents[1]


def _fc(**kw):
    """An IgG1 Fc homodimer: BOTH protomers numbered 234-444.

    The case the integer column cannot express. ``hotspot_residues`` holds
    ``[241, 241]`` and no range check anywhere can tell which 241 the user
    meant, because both are real residues.
    """
    base = dict(
        id=str(uuid.uuid4()), user_id="u-1", kind="pdb", name="Fc",
        filename="fc.pdb", storage_path="u-1/t/fc.pdb",
        target_chain="A B",
        hotspot_residues=[241, 241],
        chain_summary={
            # 260 aa total: inside proteina's 500 cap and inside its combined
            # target+binder envelope with a wide margin, so the route tests
            # below exercise the hotspot path and never the size gate.
            "total_standard_residues": 260,
            "chains": [
                {"chain_id": "A", "standard_residue_count": 130,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 234, "max_resnum": 444},
                {"chain_id": "B", "standard_residue_count": 130,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 234, "max_resnum": 444},
            ],
        },
    )
    base.update(kw)
    return DesignTarget(**base)


# ---------------------------------------------------------------------------
# (b) The prefill splits into two fields with two shapes
# ---------------------------------------------------------------------------


def test_the_shared_prefill_stays_plain_integers_on_a_chain_qualified_target():
    """THE P0, ON THE PREFILL SIDE.

    ``target_defaults_for_form`` feeds ``hotspot_residues``, which is the one
    field every selected tool reads. Emitting ``effective_hotspots`` here put
    "A241,B241" in front of rfantibody's bare ``int(tok)`` and in front of four
    parsers that refuse a chain the run does not target — for a target the user
    never edited, on a screen that never mentioned the prefix.
    """
    out = target_defaults_for_form(_fc(hotspot_spec=["A241", "B241"]))
    assert out["hotspot_residues"] == "241,241"


def test_proteinas_own_prefill_carries_the_protomer():
    """The other half: the information is not thrown away, it is routed."""
    out = target_defaults_for_form(_fc(hotspot_spec=["A241", "B241"]))
    assert out["chain_hotspots"] == "A241,B241"


def test_both_prefills_agree_on_a_target_that_stores_no_chain():
    """The ordinary single-chain target, which is most of them. Both fields get
    the same plain numbers, so proteina reading its own field first changes
    nothing for anyone who never used a prefix."""
    plain = DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", target_chain="A",
        hotspot_residues=[42, 88],
    )
    out = target_defaults_for_form(plain)
    assert out["hotspot_residues"] == "42,88"
    assert out["chain_hotspots"] == "42,88"


def test_a_target_with_no_hotspots_prefills_neither_field():
    out = target_defaults_for_form(
        DesignTarget(id=str(uuid.uuid4()), user_id="u-1", target_chain="A")
    )
    assert "hotspot_residues" not in out
    assert "chain_hotspots" not in out


# ---------------------------------------------------------------------------
# (a) The adapter prefers its own field, and falls back
# ---------------------------------------------------------------------------


def _validate(**form):
    from tools import proteina
    base = {
        "preset": "protein_binder", "_has_custom_target": "1",
        "target_chain": "A", "num_designs": "1",
    }
    base.update(form)
    return proteina.validate(base, {})


def test_proteina_prefers_its_own_hotspot_field():
    """Both fields post on the launch screen — the shared one for the other
    tools, proteina's for proteina — so which one wins has to be decided, and
    the chain-qualified one is the only one that can express a protomer."""
    inp, err = _validate(
        target_input="A234-444,B234-444",
        chain_hotspots="A241,B241",
        hotspot_residues="241,241",
    )
    assert err is None, err
    assert inp["hotspot_spec"] == ["A241", "B241"]


def test_proteina_falls_back_to_the_shared_field_when_its_own_is_blank():
    """What keeps a single-chain co-launch working with no new typing: the user
    fills the shared field once and proteina reads it, exactly as before."""
    inp, err = _validate(chain_hotspots="", hotspot_residues="42,88")
    assert err is None, err
    assert inp["hotspot_spec"] == ["A42", "A88"]
    assert inp["hotspot_residues"] == [42, 88]


def test_a_missing_proteina_field_is_the_same_as_a_blank_one():
    """The campaign form omits the key entirely when the tool is not proteina,
    and ``/tools/proteina/submit`` has never had it at all."""
    inp, err = _validate(hotspot_residues="42,88")
    assert err is None, err
    assert inp["hotspot_spec"] == ["A42", "A88"]


def test_proteinas_field_cannot_be_used_to_clear_the_shared_one():
    """Stated because it is a real limitation of ``or`` and not an accident: an
    empty proteina field means "fall back", not "no hotspots". Clearing the
    shared field is how a user asks for an unconstrained search."""
    inp, err = _validate(chain_hotspots="   ", hotspot_residues="42")
    assert err is None, err
    assert inp["hotspot_spec"] == ["A42"]

    inp, err = _validate(chain_hotspots="", hotspot_residues="")
    assert err is None, err
    assert inp["hotspot_spec"] == []


# ---------------------------------------------------------------------------
# (a) The two screens render the field
# ---------------------------------------------------------------------------


def _template(name):
    return (_REPO / "templates" / name).read_text(encoding="utf-8")


def _squash(text):
    return " ".join(text.split())


def test_the_launch_screen_renders_proteinas_field_disabled():
    """Every control inside a tool panel is server-rendered ``disabled`` and
    enabled by syncPanels() when the box is ticked (templates/targets/launch.html
    :90-95). A field that shipped enabled would post proteina's chain-qualified
    hotspots on a launch that never selected proteina — where ``_tool_form``
    would hand them to nothing, but the request would still carry them."""
    body = _squash(_template("targets/launch.html"))
    assert 'name="proteina__chain_hotspots" id="proteina__chain_hotspots" ' \
           'class="field-input" style="max-width: 320px;" disabled' in body


def test_the_launch_screen_prefills_proteinas_field_from_the_target():
    """Two-level lookup, the same one ``iggm__epitope`` uses: the namespaced key
    on a validation re-render, the target's own ``chain_hotspots`` otherwise.
    Reading only the namespaced key prefills nothing on a first GET, silently."""
    body = _squash(_template("targets/launch.html"))
    assert "pre_fill.get('proteina__chain_hotspots', " \
           "pre_fill.get('chain_hotspots', ''))" in body


def test_the_launch_screens_shared_field_no_longer_teaches_the_prefix():
    """The copy that caused this. The shared field is read by five tools that
    cannot parse a prefix, so its helper must not tell anyone to type one."""
    body = _template("targets/launch.html")
    i = body.find('field_text("hotspot_residues"')
    assert i != -1, "the shared hotspot field is gone from the launch screen"
    helper = _squash(body[i:body.find("placeholder=", i)])
    assert "Plain numbers only" in helper
    assert "prefix the chain instead" not in helper


def test_the_campaign_form_carries_both_fields_and_swaps_them():
    """One tool runs per submission here, so exactly one hotspot input may be
    live at a time — and the hidden one must be DISABLED, not merely hidden. A
    hidden-but-enabled shared field still posts, and proteina falls back to it,
    so a user who cleared proteina's field would silently get the shared value
    anyway."""
    body = _template("runs/new.html")
    flat = _squash(body)
    assert 'id="chain_hotspots" name="chain_hotspots"' in flat
    assert "hotspotInput.disabled = !sharedHotspots" in flat
    assert "chainHotspots.disabled = !custom" in flat


def test_the_campaign_form_keeps_the_proteina_rule_out_of_the_shared_hint():
    """#hotspot-hint is now LIVE server-rendered copy (nothing rewrites it) and
    is only ever shown to tools that take plain numbers."""
    body = _template("runs/new.html")
    i = body.find('<div class="hint" id="hotspot-hint">')
    assert i != -1
    shared_hint = _squash(body[i:body.find("</div>", i)])
    assert "more than one chain" not in shared_hint
    assert "prefix" not in shared_hint.lower()


def test_the_campaign_form_states_the_prefix_rule_under_proteinas_field():
    """And it has to be under the field it governs, not somewhere in the page."""
    body = _template("runs/new.html")
    i = body.find('<div class="hint" id="chain-hotspot-hint">')
    assert i != -1, "proteina's hotspot hint is gone from the campaign form"
    hint = _squash(body[i:body.find("</div>", i)])
    assert "more than one chain it is refused" in hint
    assert "prefix the chain instead" in hint


def test_no_template_writes_the_dead_hotspot_hint_ternary():
    """refreshTool() used to rewrite #hotspot-hint per tool because ONE field
    served every tool. Proteina has its own field now, so a proteina arm there
    would be copy no user can ever see."""
    body = _template("runs/new.html")
    assert not re.search(
        r"getElementById\('hotspot-hint'\)\.textContent\s*=", body
    ), "runs/new.html still rewrites the shared hint per tool"


# ---------------------------------------------------------------------------
# (c) The write path: ENRICH-ONLY
# ---------------------------------------------------------------------------


class _Update:
    """Records the UPDATE a write attempt would issue, and returns ``rows``."""

    def __init__(self, store, rows):
        self.store = store
        self._rows = rows

    def update(self, fields):
        self.store["fields"] = dict(fields)
        return self

    def eq(self, col, val):
        self.store.setdefault("eq", []).append((col, val))
        return self

    def is_(self, col, val):
        self.store.setdefault("is_", []).append((col, val))
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._rows))


class _Client:
    def __init__(self, store, rows):
        self.store = store
        self._rows = list(rows)

    def table(self, name):
        self.store["table"] = name
        return _Update(self.store, self._rows)


def _attempt(target, tokens, rows=({"id": "row"},)):
    """Run the enrichment and return ``(wrote, recorded_update_or_None)``."""
    store: dict = {}
    with patch("shared.targets.get_service_client",
               return_value=_Client(store, rows)):
        wrote = _enrich(target, tokens)
    return wrote, (store or None)


def test_a_run_that_named_the_protomers_fills_an_empty_hotspot_spec():
    """The whole point of the column: the target stores [241, 241] and cannot
    say which protomer either one is on. The run does."""
    wrote, update = _attempt(_fc(), ["A241", "B241"])
    assert wrote is True
    assert update["fields"] == {"hotspot_spec": ["A241", "B241"]}


def test_the_enrichment_never_writes_hotspot_residues():
    """CONSTRAINT 3. The integer column is what the shared launch field is
    prefilled from, and five of the six tools break on a prefixed token, so a
    write there is the P0 all over again — from a route the user reached to
    launch a run, not to edit a target."""
    _wrote, update = _attempt(_fc(), ["A241", "B241"])
    assert set(update["fields"]) == {"hotspot_spec"}


def test_the_enrichment_declines_when_hotspot_spec_is_already_set():
    """CONSTRAINT 1. A target that already says which protomer it means is
    never re-decided by a later run."""
    already = _fc(hotspot_spec=["A241", "A241"])
    wrote, update = _attempt(already, ["A241", "B241"])
    assert wrote is False
    assert update is None, "an UPDATE was issued for an already-qualified target"


def test_the_enrichment_declines_when_the_run_changed_the_hotspots():
    """CONSTRAINT 2, AND THE REASON THE WHOLE THING IS SAFE. Hotspots on the
    run form are a per-RUN override. Someone who aimed one run at 300 has not
    asked to change what every LATER run against this target starts from."""
    wrote, update = _attempt(_fc(), ["A300", "B300"])
    assert wrote is False
    assert update is None


def test_the_enrichment_declines_when_the_run_added_a_hotspot():
    """Same constraint, the subset case: [241, 241] stored, three tokens run."""
    wrote, update = _attempt(_fc(), ["A241", "B241", "A300"])
    assert wrote is False
    assert update is None


def test_the_enrichment_declines_on_a_reordered_list():
    """SAME VALUES, SAME ORDER. ``hotspot_spec`` is stored positionally beside
    ``hotspot_residues``, so ["B241", "A241"] against [241, 241] is a claim
    this function cannot check — both orders reduce to the same integers, and
    picking one would be a guess written into the database."""
    fc = _fc(hotspot_residues=[241, 300])
    assert _attempt(fc, ["A300", "B241"])[0] is False
    assert _attempt(fc, ["A241", "B300"])[0] is True


def test_the_enrichment_declines_when_the_run_named_no_chain():
    """Bare tokens carry nothing the integer column does not already hold, so
    there is nothing to enrich and no row to touch."""
    wrote, update = _attempt(_fc(), ["241", "241"])
    assert wrote is False
    assert update is None


def test_the_enrichment_declines_on_an_empty_token_list():
    for tokens in ([], None, ()):
        wrote, update = _attempt(_fc(), tokens)
        assert wrote is False, tokens
        assert update is None, tokens


def test_the_enrichment_declines_for_a_target_with_no_stored_hotspots():
    """There is nothing to qualify. Writing the run's tokens here would invent
    a target default the user never saved."""
    bare = _fc(hotspot_residues=[])
    wrote, update = _attempt(bare, ["A241", "B241"])
    assert wrote is False
    assert update is None


def test_the_update_is_owner_scoped_and_refuses_a_row_that_is_already_set():
    """The two in-memory conditions are RE-STATED in the WHERE clause, because
    the decision was made from a row read earlier in the request. Without the
    IS NULL filter a concurrent writer is clobbered; without user_id this is a
    content write addressed by id alone."""
    target = _fc()
    _wrote, update = _attempt(target, ["A241", "B241"])
    assert update["table"] == "design_targets"
    assert update["eq"] == [("id", target.id), ("user_id", "u-1")]
    assert update["is_"] == [("hotspot_spec", "null")]


def test_an_update_that_matched_nothing_reports_false():
    """PostgREST returns the affected rows, so an empty list means the WHERE
    clause refused it — another writer got there first."""
    wrote, update = _attempt(_fc(), ["A241", "B241"], rows=())
    assert wrote is False
    # It still ISSUED the update; what it must not do is report success.
    assert update["fields"] == {"hotspot_spec": ["A241", "B241"]}


def test_a_database_failure_is_swallowed():
    """MIGRATION 0041 MAY NOT HAVE LANDED. An UPDATE naming a column this
    database does not have fails every time, and the caller is a money route
    that has already created and funded runs. Nothing about the launch depends
    on this write."""
    class _Boom:
        def table(self, _name):
            raise RuntimeError("column design_targets.hotspot_spec does not exist")

    with patch("shared.targets.get_service_client", return_value=_Boom()):
        assert _enrich(_fc(), ["A241", "B241"]) is False


def test_a_malformed_chain_summary_cannot_reach_the_caller():
    """THE TRY COVERS THE REDUCTIONS, NOT JUST THE WRITE. ``chain_summary`` is
    JSON off a database row, so its shape is not guaranteed by anything in this
    process, and ``_hotspot_chain_ids`` calls ``.get("chains")`` on it. Both
    callers are money routes PAST their commit point — an escaping exception
    there is a 500 on a request whose runs are already funded and billing."""
    wrote, update = _attempt(_fc(chain_summary=["not", "a", "dict"]),
                             ["A241", "B241"])
    assert wrote is False
    assert update is None or "fields" not in update


def test_no_client_is_not_an_error():
    with patch("shared.targets.get_service_client", return_value=None):
        assert _enrich(_fc(), ["A241", "B241"]) is False


def test_a_none_target_is_not_an_error():
    assert _enrich(None, ["A241"]) is False


def test_a_multi_character_chain_is_read_against_the_targets_own_chains():
    """``split_hotspot`` matches the LONGEST chain id that fits, and it only
    knows the ids it is given. On a target whose chains are A2/B2, "A2296" is
    residue 296 on chain A2 — the reduction has to use the target's chain list
    or it reads 2296 and declines a correct enrichment."""
    t = _fc(
        target_chain="A2 B2",
        hotspot_residues=[296, 296],
        chain_summary={
            "total_standard_residues": 400,
            "chains": [
                {"chain_id": "A2", "standard_residue_count": 200,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 1, "max_resnum": 300},
                {"chain_id": "B2", "standard_residue_count": 200,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 1, "max_resnum": 300},
            ],
        },
    )
    wrote, update = _attempt(t, ["A2296", "B2296"])
    assert wrote is True
    assert update["fields"] == {"hotspot_spec": ["A2296", "B2296"]}


# ---------------------------------------------------------------------------
# (c) The write path is WIRED, on BOTH routes that run a tool at a saved target
#
# The helper being correct and the helper being CALLED are different claims,
# and only the second one makes the column reachable. These drive the real
# routes with the real `enrich_target_hotspot_spec` against a recording client,
# so a deleted call site is red here even though every unit test above stays
# green.
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = "u-1"
            sess["user_email"] = "u@example.com"
        yield c


def _ctx():
    return SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com")


def _preauth_ok():
    from decimal import Decimal
    return SimpleNamespace(
        ok=True, reason=None, balance_usd=Decimal("1000"),
        budget_usd=Decimal("50"), required_usd=Decimal("1"),
    )


def _launch_form(**kw):
    data = {
        "tools": ["proteina"],
        "pace": "burst",
        "target_chain": "A B",
        "hotspot_residues": "241,241",
        "proteina__designs": "8",
        "proteina__preset": "protein_binder",
        "proteina__target_input": "A234-444,B234-444",
        "proteina__chain_hotspots": "A241,B241",
    }
    data.update(kw)
    return data


def _post_launch(client, target, form=None):
    """POST the multi-tool launch with money mocked and the enrichment REAL."""
    store: dict = {}
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=target), \
            patch("blueprints.targets.touch_target"), \
            patch("shared.targets.get_service_client",
                  return_value=_Client(store, [{"id": target.id}])), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth_ok()), \
            patch("shared.compute_campaigns.create_campaign",
                  side_effect=lambda **kw: SimpleNamespace(
                      id="c-1", tool=kw["tool"], status="draft")), \
            patch("shared.compute_campaigns.fund_campaign", return_value=True), \
            patch("shared.compute_campaigns.drive_campaign_async"), \
            patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp = client.post(
            f"/targets/{target.id}/launch",
            data=form if form is not None else _launch_form(),
        )
    return resp, store


def test_a_proteina_launch_records_the_protomer_on_the_target(client):
    """END TO END, through the real route: a run that named its chains fills
    the target's empty `hotspot_spec`. This is what makes the column and
    migration 0041 reachable at all now that target creation is integers-only."""
    target = _fc()
    resp, store = _post_launch(client, target)
    assert resp.status_code == 302, resp.get_data(as_text=True)[-400:]
    assert store.get("fields") == {"hotspot_spec": ["A241", "B241"]}
    assert store["eq"] == [("id", target.id), ("user_id", "u-1")]


def test_a_launch_that_changed_the_hotspots_leaves_the_target_alone(client):
    """The constraint that makes the write path safe to have on a money route:
    a per-run override is not an edit of the saved target."""
    target = _fc()
    resp, store = _post_launch(client, target, form=_launch_form(
        proteina__chain_hotspots="A300,B300"))
    assert resp.status_code == 302, resp.get_data(as_text=True)[-400:]
    assert store == {}, "launching a run rewrote the target's saved hotspots"


def test_a_launch_with_no_proteina_run_writes_nothing(client):
    """Only proteina emits `hotspot_spec`. An rfdiffusion launch must not touch
    design_targets beyond the `last_used_at` stamp."""
    target = _fc()
    resp, store = _post_launch(client, target, form={
        "tools": ["rfdiffusion"], "pace": "burst",
        "target_chain": "A B", "hotspot_residues": "241,241",
        "rfdiffusion__designs": "8",
        "rfdiffusion__binder_length_min": "55",
        "rfdiffusion__binder_length_max": "65",
    })
    assert resp.status_code == 302, resp.get_data(as_text=True)[-400:]
    assert store == {}


def test_a_refused_launch_writes_nothing(client):
    """The call sits after every `create_campaign`, so a launch that produced no
    run cannot enrich. A400 here means the specs never validated."""
    target = _fc()
    resp, store = _post_launch(client, target, form=_launch_form(
        proteina__chain_hotspots="C241"))
    assert resp.status_code == 400
    assert store == {}


def _campaign_form(target, **kw):
    data = {
        "tool": "proteina",
        "preset": "protein_binder",
        "requested_designs": "8",
        "target_id": target.id,
        "target_chain": "A B",
        "target_input": "A234-444,B234-444",
        "hotspot_residues": "241,241",
        "chain_hotspots": "A241,B241",
    }
    data.update(kw)
    return data


def test_the_campaign_route_enriches_too(client):
    """The OTHER route that runs a tool against a saved target. A target
    enriched by one screen and not the other would depend on which form the
    user happened to open."""
    target = _fc()
    store: dict = {}
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.targets.get_target", return_value=target), \
            patch("shared.targets.touch_target"), \
            patch("shared.targets.get_service_client",
                  return_value=_Client(store, [{"id": target.id}])), \
            patch("shared.compute_campaigns.campaign_preauth",
                  return_value=_preauth_ok()), \
            patch("shared.compute_campaigns.create_campaign",
                  return_value=SimpleNamespace(id="c-1")), \
            patch("shared.compute_campaigns.fund_campaign", return_value=True), \
            patch("shared.compute_campaigns.drive_campaign_async"), \
            patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp = client.post("/campaigns", data=_campaign_form(target))
    assert resp.status_code == 302, resp.get_data(as_text=True)[-400:]
    assert store.get("fields") == {"hotspot_spec": ["A241", "B241"]}
