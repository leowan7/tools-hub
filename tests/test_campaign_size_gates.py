"""The size cap on ``POST /campaigns``, on both of its branches, for every tool.

WHAT THIS FILE IS FOR. ``blueprints/campaigns.py`` grew two size gates — one on
the target-bound branch (``DesignTarget.size_error``) and one on the
fresh-upload branch (``size_only_refusal``) — and the whole suite stayed green
with either ``if size_err:`` rewritten to ``if False:``. The gates were correct
and completely unguarded. Six further mutations survived with them:
``preflight_target_segments`` returning None, ``_segments_label`` returning
None, the chain-span clamp deleted, a malformed segment counted as 0 instead of
falling back to whole chains, the combined-budget cap left unarmed, and the
soft-warn "untested" copy branch switched off. Every one is pinned below.

WHY THE ORDER IS ASSERTED AND NOT JUST THE STATUS. A 400 proves the user saw an
error; it does not prove nothing was bought first. ``POST /campaigns`` reaches
``campaign_preauth`` -> ``upload_input`` -> ``create_campaign`` ->
``fund_campaign`` -> ``drive_campaign_async``, and proteina opens a 4-shard
first wave at ~$12.58 a shard inside a ~$15/shard hold that covers all of it, so
a refusal that lands one line late is a refusal that costs ~$50. Every test here
asserts the spy recorded NOTHING, the same property
``test_proteina_oversized_target_is_refused_before_any_run_is_funded`` asserts
for ``/targets/<id>/launch``.

THE FIVE OTHER TOOLS ARE IN SCOPE ON PURPOSE, NOT BY ACCIDENT. ``size_error``
resolves its rules with ``TOOL_RULES.get(tool)``, so these gates apply to every
tool in that dict: rfdiffusion, bindcraft, boltzgen, pxdesign and rfantibody all
gained a hard size refusal on both campaign branches, where they previously had
NONE. That is a real behaviour change shipped by a commit whose subject says
"fix(proteina)", and it is kept deliberately — the caps are those tools' own
literature-backed numbers, the campaign routes genuinely had no size protection
at all, and the failure direction is a free refusal rather than a funded OOM.
Kept, therefore declared and tested: see the non-proteina section at the bottom,
which also pins that those tools get their OWN cap_basis wording rather than
proteina's "untested" copy. (iggm is unaffected — it has no TOOL_RULES entry, so
``size_error`` returns None for it and no gate exists to test.)
"""

from __future__ import annotations

import html
import io
import uuid
from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Real routes through a real create_app(); app.py calls load_dotenv() at import,
# so without this every read would reach the PRODUCTION Supabase project.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.targets import DesignTarget


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("FLAG_TOOL_PROTEINA", "on")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _ctx():
    return SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com",
    )


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"


def _visible(resp) -> str:
    """The body as the user reads it. Jinja autoescapes, so a raw substring
    search fails on any sentence containing an apostrophe or a '>'."""
    return html.unescape(resp.get_data(as_text=True))


def _summary(total: int, chains: dict) -> dict:
    """``chains`` maps chain_id -> (residue_count, min_resnum, max_resnum)."""
    return {
        "total_standard_residues": total,
        "chains": [
            {"chain_id": cid, "standard_residue_count": n,
             "hetatm_resnames": [], "water_count": 0,
             "min_resnum": lo, "max_resnum": hi}
            for cid, (n, lo, hi) in chains.items()
        ],
    }


def _target(chain_summary=None, **kw) -> DesignTarget:
    base = dict(
        id=str(uuid.uuid4()), user_id="u-1", kind="pdb", name="T",
        filename="t.pdb", storage_path="u-1/t/t.pdb",
        target_chain="A", hotspot_residues=[42, 88], epitope_residues=[],
        chain_summary=chain_summary or _summary(130, {"A": (130, 1, 130)}),
    )
    base.update(kw)
    return DesignTarget(**base)


class _Spy:
    """Every money-moving or state-writing call, in the order it happened.

    One log, not five counters: "nothing was bought" is a statement about the
    whole sequence, and a per-call count cannot distinguish a refusal that came
    first from one that came after the preauth.
    """

    def __init__(self):
        self.calls: list = []

    def preauth(self, *a, **kw):
        self.calls.append("preauth")
        return SimpleNamespace(
            ok=True, reason=None, balance_usd=Decimal("1000"),
            required_usd=Decimal("1"), needs_verification=False,
        )

    def upload(self, *a, **kw):
        self.calls.append("upload_input")
        return "u-1/campaign/target.pdb"

    def create(self, **kw):
        self.calls.append(("create", kw.get("tool")))
        return SimpleNamespace(id="c-1", tool=kw.get("tool"), status="draft")

    def fund(self, campaign_id):
        self.calls.append("fund")
        return True

    def drive(self, campaign_id):
        self.calls.append("drive")


_PDB = (
    "ATOM      1  N   MET A  42      11.104  13.207  10.000  1.00 20.00           N\n"
    "ATOM      2  CA  MET A  42      12.560  13.207  10.000  1.00 20.00           C\n"
    "END\n"
)


def _post(client, form, target=None, upload_summary=None):
    """POST /campaigns with every money seam spied and nothing real touched.

    ``target`` takes the stored-target branch; ``upload_summary`` takes the
    fresh-upload branch by giving the resolved upload a chain_summary (the
    gate abstains on an upload that has none, which is what makes it possible
    to test "gate ran and passed" apart from "gate never ran").
    """
    spy = _Spy()
    upload = SimpleNamespace(
        filename="target.pdb", data=_PDB.encode(),
        content_type="chemical/x-pdb", kind="pdb",
        chain_summary=upload_summary,
    )
    patches = [
        patch("blueprints.campaigns.load_user_context", return_value=_ctx()),
        patch("shared.targets.get_target", return_value=target),
        patch("shared.targets.touch_target"),
        patch("shared.compute_campaigns.campaign_preauth", side_effect=spy.preauth),
        patch("shared.compute_campaigns.create_campaign", side_effect=spy.create),
        patch("shared.compute_campaigns.fund_campaign", side_effect=spy.fund),
        patch("shared.compute_campaigns.drive_campaign_async", side_effect=spy.drive),
        patch("blueprints.campaigns.upload_input", side_effect=spy.upload),
        patch("blueprints.campaigns.resolve_target_upload",
              return_value=(upload, None)),
        patch("shared.wallet.get_or_create_wallet",
              return_value={"balance_usd": "1000", "wallet_frozen": False}),
    ]
    data = dict(form)
    if target is None:
        data["target_pdb"] = (io.BytesIO(_PDB.encode()), "target.pdb")
    else:
        # The route only looks a target up when the form names one, and an
        # attached file would OVERRIDE it and drop the link — so the two
        # branches are selected by the payload, exactly as the browser does.
        data["target_id"] = target.id
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        resp = client.post("/campaigns", data=data)
    return resp, spy


def _proteina_form(**kw):
    data = {
        "tool": "proteina", "requested_designs": "8",
        "preset": "protein_binder", "target_chain": "A",
        "hotspot_residues": "42,88",
        "binder_length_min": "60", "binder_length_max": "120",
    }
    data.update(kw)
    return data


# ---------------------------------------------------------------------------
# F2 — the gates themselves, on both branches
# ---------------------------------------------------------------------------

def test_target_bound_branch_refuses_over_cap(client):
    """THE TARGET-BOUND GATE. 415 aa is over proteina's 140 cap.

    Rewriting this branch's `if size_err:` to `if False:` left the entire
    suite green before this test existed.
    """
    _login(client)
    t = _target(chain_summary=_summary(415, {"A": (415, 1, 415)}))
    resp, spy = _post(client, _proteina_form(), target=t)
    assert resp.status_code == 400, _visible(resp)[-500:]
    assert spy.calls == [], (
        f"refusal landed AFTER {spy.calls} — the gate must precede every one "
        f"of preauth / upload_input / create / fund / drive"
    )
    body = _visible(resp)
    assert "415" in body and "140" in body


def test_target_bound_branch_admits_under_cap(client):
    """The other half. Without this the test above would also pass against a
    gate that refuses everything."""
    _login(client)
    resp, spy = _post(client, _proteina_form(), target=_target())
    assert resp.status_code in (302, 303), _visible(resp)[-500:]
    assert ("create", "proteina") in spy.calls


def test_fresh_upload_branch_refuses_over_cap(client):
    """THE FRESH-UPLOAD GATE, a genuinely separate path — it calls
    ``size_only_refusal`` with different kwargs, so a fix to the target-bound
    branch does not reach it. Same mutation survived here too."""
    _login(client)
    resp, spy = _post(
        client, _proteina_form(),
        upload_summary=_summary(415, {"A": (415, 1, 415)}),
    )
    assert resp.status_code == 400, _visible(resp)[-500:]
    assert spy.calls == []
    assert "415" in _visible(resp)


def test_fresh_upload_branch_admits_under_cap(client):
    _login(client)
    resp, spy = _post(
        client, _proteina_form(), upload_summary=_summary(130, {"A": (130, 1, 130)}),
    )
    assert resp.status_code in (302, 303), _visible(resp)[-500:]
    assert ("create", "proteina") in spy.calls


# ---------------------------------------------------------------------------
# F2 — the contig wiring the gates depend on
# ---------------------------------------------------------------------------

def test_a_contig_narrows_an_oversized_target_into_the_cap(client):
    """THE WHOLE POINT OF SIZING THE SELECTION. An 830 aa upload whose contig
    names 130 residues is a 130-residue run and must be admitted; sizing the
    file would refuse a run the container would happily do.

    Dies when ``preflight_target_segments`` is stubbed to None, when
    ``_target_segments`` stops reaching the route, and when the chain-span
    clamp in ``selection_residue_count`` is removed (that clamp is what turns
    the two 65-residue windows into 130 rather than the whole chains' 830).
    """
    _login(client)
    t = _target(chain_summary=_summary(
        830, {"A": (415, 1, 415), "B": (415, 1, 415)},
    ))
    resp, spy = _post(client, _proteina_form(
        target_chain="A B", target_input="A236-300,B236-300",
        hotspot_residues="A250,B250",
    ), target=t)
    assert resp.status_code in (302, 303), _visible(resp)[-500:]
    assert ("create", "proteina") in spy.calls


def test_a_contig_that_is_still_too_big_is_still_refused(client):
    """The contig is not an escape hatch — it re-sizes, it does not disable."""
    _login(client)
    t = _target(chain_summary=_summary(
        830, {"A": (415, 1, 415), "B": (415, 1, 415)},
    ))
    resp, spy = _post(client, _proteina_form(
        target_chain="A B", target_input="A1-200,B1-200",
        hotspot_residues="A100,B100",
    ), target=t)
    assert resp.status_code == 400, _visible(resp)[-500:]
    assert spy.calls == []
    assert "400" in _visible(resp)


def test_the_refusal_names_the_selection_not_the_file(client):
    """``_segments_label`` is what puts the contig in the sentence. Stubbing it
    to None survived the suite, and the message silently degraded to "The
    target chain(s) have N residues" — which names a number the user cannot
    map back to anything they typed."""
    _login(client)
    t = _target(chain_summary=_summary(
        830, {"A": (415, 1, 415), "B": (415, 1, 415)},
    ))
    resp, _ = _post(client, _proteina_form(
        target_chain="A B", target_input="A1-200,B1-200",
        hotspot_residues="A100,B100",
    ), target=t)
    body = _visible(resp)
    assert "A1-200,B1-200" in body, body[-500:]
    assert "The region you selected" in body


_FC_NUMBERED_FROM_236 = _summary(415, {"A": (415, 236, 650)})


def test_a_range_that_overshoots_the_chain_numbering_counts_what_exists():
    """THE CHAIN-SPAN CLAMP, and it is not decoration.

    Real Fc constructs are numbered from 236 (3S7G's CH2+CH3 is exactly this),
    so a user who wants "the first bit of chain A" reasonably types A1-300 and
    means residues 236-300 — the only ones that exist. The clamp intersects the
    typed range with the chain's real min/max resnum and counts 65. Without it
    the span is a bare 300-1+1 and `min(span, whole_chain)` picks 300.

    Both numbers are "safe" in the over-count sense, which is why deleting the
    clamp survived the whole suite. It is still wrong in the direction this
    commit exists to fix: 65 is under the 140 cap and 300 is not, so dropping
    the clamp refuses a run the container would have completed, and the user's
    only recourse is to guess the numbering. The cap must refuse big runs, not
    unusual numbering.
    """
    from shared.targets import selection_residue_count

    assert selection_residue_count(
        _FC_NUMBERED_FROM_236, "A", [("A", 1, 300)],
    ) == 65

    # And the clamp cannot manufacture residues either: a range that overshoots
    # both ends is still just the chain.
    assert selection_residue_count(
        _FC_NUMBERED_FROM_236, "A", [("A", 1, 1000)],
    ) == 415


def test_an_overshooting_range_is_admitted_at_its_real_size(client):
    """The consequence of the clamp, on the route that spends the money."""
    _login(client)
    t = _target(chain_summary=_FC_NUMBERED_FROM_236)
    resp, spy = _post(client, _proteina_form(
        target_input="A1-300", hotspot_residues="A250",
    ), target=t)
    assert resp.status_code in (302, 303), _visible(resp)[-500:]
    assert ("create", "proteina") in spy.calls


def test_an_unreadable_segment_falls_back_to_the_whole_chains(client):
    """THE ONE MUTATION THAT BILLS MONEY. A segment the counter cannot unpack
    must fall back to counting whole chains — the LARGER number. Counting it
    as 0 is an UNDER-count, and under-counting is the direction that admits an
    oversized run and funds it.

    Driven through ``selection_residue_count`` directly because no HTTP form
    can produce a malformed tuple: the adapter's parser rejects bad contigs
    long before this, so the fallback exists for a caller passing segments
    programmatically, and this is the seam where it is decidable.
    """
    from shared.targets import selection_residue_count

    summary = _summary(830, {"A": (415, 1, 415), "B": (415, 1, 415)})
    count = selection_residue_count(summary, "A B", [("A", 1, 100), "junk"])
    assert count == 830, (
        f"a malformed segment produced {count}; anything less than the whole "
        f"named chains under-counts, and under-counting funds the run this "
        f"gate exists to refuse"
    )


# ---------------------------------------------------------------------------
# F3 — the combined-budget cap, which no money route ever armed
# ---------------------------------------------------------------------------
#
# hard_cap_combined_aa fires on (target_aa + binder_max_aa). No caller passed
# binder_max_aa, so the whole half was dead on every route that spends money: a
# 140 aa target with a 300 aa max binder is 440 against proteina's 260 budget,
# refused by /tools/proteina/submit and funded by both branches here.
# ---------------------------------------------------------------------------

_OVER_COMBINED = dict(binder_length_min="60", binder_length_max="300")


def test_combined_cap_fires_on_the_target_bound_branch(client):
    _login(client)
    resp, spy = _post(client, _proteina_form(**_OVER_COMBINED), target=_target())
    assert resp.status_code == 400, _visible(resp)[-500:]
    assert spy.calls == []
    body = _visible(resp)
    assert "combined budget" in body and "260" in body


def test_combined_cap_fires_on_the_fresh_upload_branch(client):
    _login(client)
    resp, spy = _post(
        client, _proteina_form(**_OVER_COMBINED),
        upload_summary=_summary(130, {"A": (130, 1, 130)}),
    )
    assert resp.status_code == 400, _visible(resp)[-500:]
    assert spy.calls == []
    assert "combined budget" in _visible(resp)


def test_a_binder_inside_the_budget_still_runs(client):
    """130 + 120 = 250, under the 260 budget. Guards against the combined cap
    being armed with something that refuses every campaign."""
    _login(client)
    resp, spy = _post(client, _proteina_form(), target=_target())
    assert resp.status_code in (302, 303), _visible(resp)[-500:]
    assert ("create", "proteina") in spy.calls


# ---------------------------------------------------------------------------
# F13 — the soft-warn copy branch may not predict an OOM it cannot predict
# ---------------------------------------------------------------------------

def test_the_untested_soft_warn_copy_does_not_predict_an_oom():
    """proteina's cap_basis is "untested": the largest target ever run is 130
    residues, so nothing above that is known-to-fail, only unmeasured. The
    generic soft-warn copy this falls through to says "a higher chance of
    out-of-memory", which is exactly the claim the untested branch exists to
    avoid making — and deleting that branch left the suite green while the
    hard-cap twin was caught by one test.
    """
    from shared.pdb_preflight import _check_size_envelope
    from shared.pdb_preflight_rules import TOOL_RULES

    rules = TOOL_RULES["proteina"]
    assert rules.size.cap_basis == "untested"   # precondition for the branch
    status = _check_size_envelope(
        rules, rules.size.soft_warn_target_aa + 1,
        binder_max_aa=None, num_designs=None,
    )
    assert status.over_soft_warn
    msg = status.warn_message or ""
    assert "out-of-memory" not in msg, msg
    assert "has not been measured" in msg


def test_a_literature_backed_tool_keeps_the_generic_soft_warn():
    """The other side of the branch. Without this, the assertion above is
    satisfied by deleting the generic copy instead of by keeping both."""
    from shared.pdb_preflight import _check_size_envelope
    from shared.pdb_preflight_rules import TOOL_RULES

    rules = TOOL_RULES["rfdiffusion"]
    assert rules.size.cap_basis != "untested"
    status = _check_size_envelope(
        rules, rules.size.soft_warn_target_aa + 1,
        binder_max_aa=None, num_designs=None,
    )
    assert "out-of-memory" in (status.warn_message or "")


# ---------------------------------------------------------------------------
# F4 — the undeclared collateral, now declared
# ---------------------------------------------------------------------------
#
# These five tools had NO size gate on either campaign branch before this
# change and have a hard one now. Kept on purpose; pinned here so it is a
# tested behaviour rather than a side effect nobody wrote down.
# ---------------------------------------------------------------------------

_OTHER_TOOLS = {
    # slug: (form, over-cap aa, under-cap aa)
    "rfdiffusion": (
        {"tool": "rfdiffusion", "requested_designs": "24", "target_chain": "A",
         "hotspot_residues": "42,88", "binder_length_min": "55",
         "binder_length_max": "65"},
        700, 200,
    ),
    "boltzgen": (
        {"tool": "boltzgen", "requested_designs": "24", "target_chain": "A",
         "hotspot_residues": "42,88", "binder_length_min": "50",
         "binder_length_max": "100"},
        800, 200,
    ),
}


@pytest.mark.parametrize("slug", sorted(_OTHER_TOOLS))
@pytest.mark.parametrize("branch", ["target", "upload"])
def test_other_tools_are_refused_above_their_own_cap(client, slug, branch):
    """Both branches, and the cap is the TOOL'S own number, not proteina's."""
    from shared.pdb_preflight_rules import TOOL_RULES

    form, over_aa, _ = _OTHER_TOOLS[slug]
    cap = TOOL_RULES[slug].size.hard_cap_target_aa
    assert over_aa > cap, f"{slug} fixture is not over its own {cap} cap"

    _login(client)
    summary = _summary(over_aa, {"A": (over_aa, 1, over_aa)})
    if branch == "target":
        resp, spy = _post(client, form, target=_target(chain_summary=summary))
    else:
        resp, spy = _post(client, form, upload_summary=summary)
    assert resp.status_code == 400, _visible(resp)[-500:]
    assert spy.calls == [], f"{slug}/{branch} refused only after {spy.calls}"
    body = _visible(resp)
    assert str(over_aa) in body and str(cap) in body
    # Their copy is the literature-backed one, NOT proteina's untested wording.
    assert "tops out around" in body, body[-400:]
    assert "precaution rather than a measured failure point" not in body


@pytest.mark.parametrize("slug", sorted(_OTHER_TOOLS))
@pytest.mark.parametrize("branch", ["target", "upload"])
def test_other_tools_still_run_below_their_cap(client, slug, branch):
    """The gate is a size gate, not a new blanket refusal for these five."""
    form, _, under_aa = _OTHER_TOOLS[slug]
    _login(client)
    summary = _summary(under_aa, {"A": (under_aa, 1, under_aa)})
    if branch == "target":
        resp, spy = _post(client, form, target=_target(chain_summary=summary))
    else:
        resp, spy = _post(client, form, upload_summary=summary)
    assert resp.status_code in (302, 303), _visible(resp)[-500:]
    assert ("create", slug) in spy.calls


def test_iggm_has_no_rules_entry_so_no_gate_applies():
    """The one campaign tool the collateral does NOT reach. Pinned so that
    adding a TOOL_RULES entry for iggm is a decision someone makes rather than
    a size refusal that appears on a route nobody was thinking about."""
    from shared.pdb_preflight import size_only_refusal
    from shared.pdb_preflight_rules import TOOL_RULES

    assert "iggm" not in TOOL_RULES
    assert size_only_refusal("iggm", 5000, binder_max_aa=5000) is None
