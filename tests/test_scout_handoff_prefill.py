"""The Scout -> binder-form handoff, end to end, with nothing typed twice.

Prod said this path was dead: one ``scout_handoffs`` row had ever been
created (2026-05-05, never consumed) against 58 feasibility page views,
and no event recorded a handoff arriving on a form. Those three figures
come from a read-only query of the production tables on 2026-09-28 and
are NOT reproducible from this repository. The three faults behind
them, each pinned by a class here:

1. ``/scout/feasibility/analyze`` never returned the epitope's residue
   numbers. The page's handoff form posts them from a hidden input, so
   arriving from the results table -- where the page has an epitope id
   and no residue list of its own -- the input posted empty and the POST
   answered 400. ``TestAnalyzeReturnsTheResiduesTheHandoffPosts``.
2. Two tools whose forms take a target structure, a chain and the
   residues (rfdiffusion, iggm) were absent from
   ``VALID_HANDOFF_TOOLS`` and from the picker.
   ``TestThePickerAndTheGateAgree``.
3. iggm's residue field is named ``epitope``, not
   ``hotspot_residues``, so writing the prefill under the uniform key
   would have dropped the numbers silently -- form renders, numbers
   absent. ``TestEveryHandoffToolPrefillsItsOwnFields``.

    pytest tests/test_scout_handoff_prefill.py -v
"""

from __future__ import annotations

import csv
import pathlib
import re
import shutil
from pathlib import Path

import pytest

from scout.handoff import VALID_HANDOFF_TOOLS

pytestmark = pytest.mark.usefixtures("isolate_supabase")

TMP = Path("tmp")

# Distinct from any default so a prefilled value cannot be confused with
# the field's placeholder or its "A" fallback.
HANDOFF_CHAIN = "B"
HANDOFF_RESIDUES = [41, 42, 108]


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app  # also populates tools.base._REGISTRY
    from shared.feature_flags import flag_name

    # A flagged-off tool answers 404, which would make every prefill
    # assertion below pass for the wrong reason.
    for slug in VALID_HANDOFF_TOOLS:
        monkeypatch.setenv(flag_name(slug), "on")
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    from scout import ratelimit

    ratelimit.reset()
    yield app.test_client()
    ratelimit.reset()


def _upload(client) -> str:
    """A job dir owned by this session, which ``_resolve_job_dir`` requires.

    Two chains, each long enough to clear the pipeline's patch cap.
    """
    import io

    lines = []
    serial = 0
    for chain in ("A", "B"):
        for i in range(1, 41):
            serial += 1
            lines.append(
                f"ATOM  {serial:5d}  CA  ALA {chain}{i:4d}    "
                f"{i * 1.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 20.00           C"
            )
    pdb = ("\n".join(lines) + "\nEND\n").encode()
    resp = client.post(
        "/scout/upload",
        data={"file": (io.BytesIO(pdb), "target.pdb")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.data
    return resp.get_json()["job_id"]


def _login(client) -> None:
    with client.session_transaction() as sess:
        sess["user_email"] = "someone@example.com"
        sess["user_id"] = "u-handoff-test"


# ---------------------------------------------------------------------------
# 1. The residue list the page posts


@pytest.fixture
def reap_jobs():
    before = {p.name for p in TMP.iterdir()} if TMP.exists() else set()
    yield
    if not TMP.exists():
        return
    for entry in TMP.iterdir():
        if entry.name not in before and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


class TestAnalyzeReturnsTheResiduesTheHandoffPosts:
    """``epitope_residues``: bare integers, which is what the POST accepts.

    The route already returned ``residues``, but that column is
    resname+number ("TYR67"), and ``scout/routes.py::handoff_to_tool``
    parses its ``hotspot_residues`` field with ``int()``. So the field
    the page fills from the response had to be a separate, numeric one.
    """

    def _job_with_feasibility(self, client, monkeypatch, reap_jobs):
        from scout.pipeline import FEASIBILITY_CSV_COLUMNS

        # The feasibility pipeline cannot run here (freesasa is absent from
        # this venv) and the route reads its numeric columns straight back
        # out, so the stub has to write the whole declared column set.
        job_id = _upload(client)

        def _feas(pdb_path, chain_id, epitope_residues, progress_callback=None):
            row = dict.fromkeys(FEASIBILITY_CSV_COLUMNS, "0")
            row.update({
                "epitope_id": "1",
                "chain_id": chain_id,
                "residues": ",".join(f"ALA{r}" for r in HANDOFF_RESIDUES),
                "residue_count": str(len(HANDOFF_RESIDUES)),
                "tier": "Moderate",
            })
            out = Path(pdb_path).parent / "feasibility_results.csv"
            with out.open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FEASIBILITY_CSV_COLUMNS)
                w.writeheader()
                w.writerow(row)
            return out

        monkeypatch.setattr("scout.pipeline.run_feasibility_pipeline", _feas)
        return job_id

    def test_analyze_returns_bare_integer_residues(
        self, client, monkeypatch, reap_jobs
    ):
        _login(client)
        job_id = self._job_with_feasibility(client, monkeypatch, reap_jobs)

        resp = client.post(
            "/scout/feasibility/analyze",
            json={
                "job_id": job_id,
                "chain": "A",
                "epitope_residues": HANDOFF_RESIDUES,
            },
        )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body.get("epitope_residues") == HANDOFF_RESIDUES, (
            "the feasibility response carries no bare residue numbers, so "
            "the page's hidden hotspot input posts empty and every handoff "
            f"400s: {body}"
        )
        assert all(isinstance(r, int) for r in body["epitope_residues"]), (
            "handoff_to_tool parses these with int(); strings like 'TYR67' "
            "are rejected"
        )


# ---------------------------------------------------------------------------
# 2. The picker, the gate and the card


class TestThePickerAndTheGateAgree:
    """One set, three surfaces. A slug in one and not the others is a wall.

    The picker offers a tool; ``VALID_HANDOFF_TOOLS`` is what the POST
    checks; the pilot card claims the round trip. tests/
    test_pilot_recipes.py::TestHotspotDeflection owns the card.
    """

    @staticmethod
    def _picker_slugs() -> set[str]:
        html = Path("templates/scout/feasibility.html").read_text(
            encoding="utf-8"
        )
        select = re.search(
            r'<select[^>]*name="tool".*?</select>', html, re.S
        )
        assert select, "the handoff tool picker is gone from the page"
        return set(re.findall(r'<option value="([a-z0-9_-]+)"', select.group(0)))

    def test_every_offered_tool_passes_the_gate(self):
        offered = self._picker_slugs()
        assert offered, "the picker offers nothing"
        assert offered <= VALID_HANDOFF_TOOLS, (
            "the picker offers tools the POST refuses, so the button 400s: "
            f"{sorted(offered - VALID_HANDOFF_TOOLS)}"
        )

    def test_every_accepted_tool_is_offered(self):
        """The direction the dead rfdiffusion / iggm gap sat in.

        A tool the gate accepts but the picker hides is reachable only by
        hand-editing a URL, which is the same as not existing.
        """
        missing = VALID_HANDOFF_TOOLS - self._picker_slugs()
        assert not missing, (
            f"{sorted(missing)} accept a Scout handoff but Scout never "
            f"offers it"
        )

    def test_every_accepted_tool_really_takes_a_target_and_residues(self):
        """The membership rule in scout/handoff.py, checked against the forms.

        A slug in the set whose form has no structure banner, no chain
        field and no residue field would render a prefill into nothing.
        """
        from scout.handoff import VALID_HANDOFF_TOOLS

        for slug in sorted(VALID_HANDOFF_TOOLS):
            html = Path(f"templates/tools/{slug}_form.html").read_text(
                encoding="utf-8"
            )
            assert "pdb_source_banner(" in html, slug
            assert 'name="target_chain"' in html, slug
            assert (
                'name="hotspot_residues"' in html or 'name="epitope"' in html
            ), f"{slug}: no field for the residues Scout hands over"


# ---------------------------------------------------------------------------
# 3. The prefill actually lands, in the field each form uses


class _FakeHandoff:
    id = "h-1"
    user_id = "u-handoff-test"
    scout_job_id = "sj-1"
    tool = ""
    target_chain = HANDOFF_CHAIN
    hotspot_residues = HANDOFF_RESIDUES
    pdb_filename = "target.pdb"
    storage_path = "u-handoff-test/scout-handoff-h-1/target.pdb"
    consumed_at = None
    expires_at = None


class TestEveryHandoffToolPrefillsItsOwnFields:
    """Driven through the rendered form, so it fails at whichever layer drops it."""

    @staticmethod
    def _render(client, monkeypatch, slug: str) -> str:
        monkeypatch.setattr(
            "blueprints.tools.get_handoff", lambda hid, *, user_id: _FakeHandoff()
        )
        # The wallet/job context the signed-in branch loads is not what is
        # under test; a missing Supabase client makes those render empty
        # rather than fail, which isolate_supabase already arranges.
        resp = client.get(f"/tools/{slug}?handoff=h-1")
        assert resp.status_code == 200, (slug, resp.status_code)
        return resp.get_data(as_text=True)

    @staticmethod
    def _field_value(html: str, name: str) -> str | None:
        m = re.search(
            r'<input[^>]*name="' + re.escape(name) + r'"[^>]*>', html
        )
        if not m:
            return None
        v = re.search(r'value="([^"]*)"', m.group(0))
        return v.group(1) if v else ""

    @pytest.mark.parametrize("slug", sorted(VALID_HANDOFF_TOOLS))
    def test_chain_and_residues_arrive_in_the_form(
        self, client, monkeypatch, slug
    ):
        _login(client)
        html = self._render(client, monkeypatch, slug)

        assert self._field_value(html, "target_chain") == HANDOFF_CHAIN, (
            f"{slug}: the handed-over chain is not in the form"
        )
        # iggm names the same field ``epitope``; everything else
        # ``hotspot_residues``. Read whichever the form actually has, so
        # this checks the mapping rather than restating it.
        field = "epitope" if 'name="epitope"' in html else "hotspot_residues"
        got = self._field_value(html, field)
        assert got is not None, f"{slug}: no residue field rendered at all"
        assert [int(x) for x in got.split(",") if x] == HANDOFF_RESIDUES, (
            f"{slug}: the handed-over residues were dropped (field={field!r}, "
            f"value={got!r})"
        )

    def test_a_preset_is_selected_even_when_the_tool_has_no_pilot(
        self, client, monkeypatch
    ):
        """iggm's presets are complex_prediction / affinity_maturation / design.

        ``_prefill.html::pre_checked`` checks the first radio only when
        NO preset is pre-filled, so pre-filling "pilot" on a tool that has
        no such option leaves the whole group unchecked and the form posts
        no preset.
        """
        import app as _app  # noqa: F401  (populates tools.base._REGISTRY)
        from tools import base as tool_base

        _login(client)
        assert tool_base.get("iggm").preset_for("pilot") is None, (
            "iggm grew a pilot preset; this test no longer covers the case"
        )
        html = self._render(client, monkeypatch, "iggm")
        radios = re.findall(r'<input[^>]*name="preset"[^>]*>', html)
        assert radios, "iggm renders no preset radios"
        assert any("checked" in r for r in radios), (
            "no preset is selected on the handoff-prefilled iggm form"
        )


# ---------------------------------------------------------------------------
# 4. A failed handoff does not throw the scored page away


class TestAFailedHandoffReturnsToThePage:
    """The button is a plain <form> POST, so a JSON body IS the new page.

    Before this, a handoff the route refused replaced the whole scored
    feasibility report with one line of JSON, and the user's epitope was
    gone. The refusals someone can reach by clicking now redirect back
    with the reason.
    """

    def test_no_residues_redirects_back_with_the_reason(
        self, client, reap_jobs
    ):
        _login(client)
        job_id = _upload(client)
        resp = client.post(
            "/scout/handoff/tool",
            data={
                "tool": "bindcraft",
                "scout_job_id": job_id,
                "target_chain": "B",
                "hotspot_residues": "",
            },
        )
        assert resp.status_code == 302, resp.data[:300]
        loc = resp.headers["Location"]
        assert loc.startswith("/scout/feasibility"), loc
        assert "handoff_error=no_hotspots" in loc, loc
        # The page reads the chain from the query string and falls back to
        # "A", so a redirect that dropped it would re-score chain A and
        # present that as the epitope the user just analysed.
        assert "chain=B" in loc, loc

    def test_the_page_renders_the_reason(self, client):
        """Vacuity guard: the redirect is pointless if nothing shows it."""
        _login(client)
        resp = client.get("/scout/feasibility?handoff_error=no_hotspots")
        assert resp.status_code == 200, resp.status_code
        assert "No epitope residues to carry over" in resp.get_data(
            as_text=True
        ), "the feasibility page swallows handoff_error"

    def test_the_alert_is_outside_the_hidden_results_section(self, client):
        """In #results-section the message renders into display:none.

        That section keeps its ``hidden`` attribute until a successful
        render clears it, and the auto-load script returns early when the
        URL carries no epitope_id -- which is exactly the state a manually
        typed epitope leaves behind. The alert has to come first in the
        document.
        """
        _login(client)
        body = client.get(
            "/scout/feasibility?handoff_error=no_hotspots"
        ).get_data(as_text=True)
        assert body.index("No epitope residues to carry over") < body.index(
            'id="results-section"'
        ), "the alert sits inside the section that starts out hidden"

    def test_every_code_the_route_emits_has_wording(self):
        """The vocabulary is split across a producer and a consumer.

        scout/routes.py emits the key; the template holds the wording. A
        key with no entry renders NOTHING -- silently, and in exactly the
        state this whole class exists to stop. Bind the two sets.
        """
        route = pathlib.Path("scout/routes.py").read_text(encoding="utf-8")
        emitted = set(
            re.findall(r'_handoff_failed\(\s*"([a-z_]+)"', route)
        )
        assert emitted, "no _handoff_failed call sites found -- regex rotted"

        tpl = pathlib.Path(
            "templates/scout/feasibility.html"
        ).read_text(encoding="utf-8")
        block = tpl.split("HANDOFF_ERRORS = {", 1)[1].split("} %}", 1)[0]
        worded = set(re.findall(r'"([a-z_]+)":', block))

        assert emitted - worded == set(), (
            f"codes the route emits with no wording: {sorted(emitted - worded)}"
        )
        assert worded - emitted == set(), (
            f"wording for codes nothing emits: {sorted(worded - emitted)}"
        )

    def test_arbitrary_query_text_is_not_reflected(self, client):
        """handoff_error is a key, not prose.

        Anyone can send a user a link to this page, so reflecting the
        parameter verbatim would put a third party's words in a
        first-party Scout alert.
        """
        _login(client)
        body = client.get(
            "/scout/feasibility?handoff_error=Your+account+is+suspended"
        ).get_data(as_text=True)
        assert "Your account is suspended" not in body
        assert "Could not open the tool form" not in body
