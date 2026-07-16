"""Offline unit tests for the IgGM antibody/nanobody design atomic tool.

Runs fully offline — no Modal, no Supabase, no GPU. Covers:

1. Adapter registration (slug, 5 presets, requires_pdb, templates).
2. ``validate()`` accepts well-formed inputs per preset and rejects every
   known malformed case (missing >H, stray >A, no-mask design preset,
   maturation without / with mismatched wild-type, bad epitope, oversized
   chain, out-of-range num_samples).
3. ``build_payload()`` job_spec shape.
4. The correctness-critical epitope conversion + antigen extraction in
   ``run_pipeline`` against a synthetic PDB with NON-1-based numbering and
   an insertion code (the silent-wrong-output guard).
5. Pricing wiring: TOOL_SPECS + PER_JOB_HARD_CAP_USD + PRESET_CAPS, and the
   estimate scales with num_samples.
6. The two Jinja templates parse (syntax check).
7. GET /tools/iggm renders 200 through Flask with every input present, and
   404s with the flag off.

Section 6 only parses Jinja syntax, which does NOT resolve url_for(); that is
why a stale ``url_for('tool_submit')`` shipped and 500'd the live page. Section
7 renders the route for real to close that gap.
"""

from __future__ import annotations

import pytest

from tools import iggm as ig
from tools.base import get as get_adapter


# A valid heavy chain (>= ANTIBODY_LEN_MIN aa, canonical) reused across tests.
HEAVY = (
    "QVQLVESGGGLVQPGGSLRLSCAASGFTFSSYAMSWVRQAPGKGLEWVSAISGSGGSTYYADSVKG"
    "RFTISRDNSKNTLYLQMNSLRAEDTAVYYCAKDIVGYWGQGTLVTVSS"
)
LIGHT = (
    "DIQMTQSPSSLSASVGDRVTITCRASQSISSYLNWYQQKPGKAPKLLIYAASSLQSGVPSRFSGSG"
    "SGTDFTLTISSLQPEDFATYYCQQSYSTPLTFGGGTKVEIK"
)
HEAVY_MASKED = HEAVY[:-12] + "XXXXX" + HEAVY[-7:]


# ---------------------------------------------------------------------------
# 1. Registration
# ---------------------------------------------------------------------------


class TestAdapterRegistration:
    def test_registered(self):
        a = get_adapter("iggm")
        assert a is not None and a.slug == "iggm"

    def test_presets(self):
        a = get_adapter("iggm")
        assert [p.slug for p in a.presets] == [
            "complex_prediction", "cdr_design", "fr_design",
            "affinity_maturation", "inverse_design",
        ]

    def test_requires_pdb(self):
        a = get_adapter("iggm")
        assert a.requires_pdb is True
        assert all(p.requires_pdb for p in a.presets)

    def test_templates(self):
        a = get_adapter("iggm")
        assert a.form_template == "tools/iggm_form.html"
        assert a.results_partial == "tools/iggm_results.html"


# ---------------------------------------------------------------------------
# 2. validate()
# ---------------------------------------------------------------------------


class TestValidateAccept:
    def test_complex_prediction(self):
        inp, err = ig.validate(
            {"preset": "complex_prediction", "fasta": f">H\n{HEAVY}",
             "target_chain": "A", "epitope": "7 8 9 55"}, {})
        assert err is None
        assert inp["run_task"] == "design"
        assert inp["epitope_pdb_resnums"] == [7, 8, 9, 55]
        assert inp["num_samples"] == 1

    def test_nanobody_no_light(self):
        inp, err = ig.validate(
            {"preset": "complex_prediction", "fasta": f">H\n{HEAVY}",
             "target_chain": "B"}, {})
        assert err is None and "nanobody" in inp["target"]

    def test_cdr_design_with_mask(self):
        inp, err = ig.validate(
            {"preset": "cdr_design", "fasta": f">H\n{HEAVY_MASKED}",
             "target_chain": "A"}, {})
        assert err is None and inp["run_task"] == "design"

    def test_affinity_maturation(self):
        inp, err = ig.validate(
            {"preset": "affinity_maturation", "fasta": f">H\n{HEAVY_MASKED}",
             "fasta_origin": f">H\n{HEAVY}", "target_chain": "A",
             "num_samples": "10"}, {})
        assert err is None
        assert inp["run_task"] == "affinity_maturation"
        assert inp["num_samples"] == 10
        assert inp["fasta_origin"] is not None
        # HEAVY_MASKED has 5 masked positions; maturation expands per position,
        # so the true design count = 10 samples x 5 = 50 (not raw num_samples).
        assert inp["n_masked"] == 5
        assert inp["total_passes"] == 50
        assert inp["parameters"]["n_designs_total"] == 50

    def test_epitope_comma_and_space(self):
        inp, err = ig.validate(
            {"preset": "complex_prediction", "fasta": f">H\n{HEAVY}",
             "target_chain": "A", "epitope": "7,8, 9;55"}, {})
        assert err is None and inp["epitope_pdb_resnums"] == [7, 8, 9, 55]


class TestValidateReject:
    def _v(self, **form):
        form.setdefault("target_chain", "A")
        return ig.validate(form, {})

    def test_missing_heavy(self):
        _, err = self._v(preset="complex_prediction", fasta=f">L\n{LIGHT}")
        assert err and "heavy" in err.lower()

    def test_stray_antigen_record(self):
        _, err = self._v(preset="complex_prediction",
                         fasta=f">H\n{HEAVY}\n>A\nNLCPFDEVFNAT")
        assert err and ">A" in err

    def test_design_preset_without_mask(self):
        _, err = self._v(preset="cdr_design", fasta=f">H\n{HEAVY}")
        assert err and "X" in err

    def test_maturation_without_origin(self):
        _, err = self._v(preset="affinity_maturation",
                         fasta=f">H\n{HEAVY_MASKED}", num_samples="10")
        assert err is not None

    def test_maturation_without_mask(self):
        # No X in the design FASTA: maturation has nothing to diversify.
        _, err = self._v(preset="affinity_maturation", fasta=f">H\n{HEAVY}",
                         fasta_origin=f">H\n{HEAVY}", num_samples="10")
        assert err and "X" in err

    def test_maturation_over_pass_cap(self):
        # 5 masked positions x 25 samples = 125 passes > 100-per-run limit.
        # (25 <= NUM_SAMPLES_MAX, so only the product cap can reject this.)
        _, err = self._v(preset="affinity_maturation", fasta=f">H\n{HEAVY_MASKED}",
                         fasta_origin=f">H\n{HEAVY}", num_samples="25")
        assert err and "100" in err

    def test_maturation_huge_mask_advice(self):
        # 51 masked positions: even the 2-sample minimum = 102 > 100, so no
        # sample count works. The message must advise masking fewer positions,
        # not an impossible sample count.
        masked = "X" * 51 + HEAVY[51:]  # same length as HEAVY, 51 leading masks
        _, err = self._v(preset="affinity_maturation", fasta=f">H\n{masked}",
                         fasta_origin=f">H\n{HEAVY}", num_samples="2")
        assert err and "mask at most" in err

    def test_maturation_length_mismatch(self):
        _, err = self._v(preset="affinity_maturation", fasta=f">H\n{HEAVY_MASKED}",
                         fasta_origin=f">H\n{HEAVY[:-1]}", num_samples="10")
        assert err and "length" in err.lower()

    def test_maturation_origin_with_x(self):
        _, err = self._v(preset="affinity_maturation", fasta=f">H\n{HEAVY_MASKED}",
                         fasta_origin=f">H\n{HEAVY_MASKED}", num_samples="10")
        assert err and "X" in err

    def test_bad_epitope(self):
        _, err = self._v(preset="complex_prediction", fasta=f">H\n{HEAVY}",
                         epitope="7 8 foo")
        assert err and "integer" in err.lower()

    def test_num_samples_out_of_range(self):
        _, err = self._v(preset="complex_prediction", fasta=f">H\n{HEAVY}",
                         num_samples="500")
        assert err and "100" in err

    def test_noncanonical_residue(self):
        _, err = self._v(preset="complex_prediction", fasta=f">H\n{HEAVY[:-1]}B")
        assert err is not None

    def test_empty_chain_defaults_to_A(self):
        # Matches boltz2's ``(form.get("target_chain") or "A")`` idiom.
        inp, err = ig.validate({"preset": "complex_prediction",
                                "fasta": f">H\n{HEAVY}", "target_chain": ""}, {})
        assert err is None and inp["antigen_chain"] == "A"

    def test_chain_too_long(self):
        _, err = ig.validate({"preset": "complex_prediction",
                              "fasta": f">H\n{HEAVY}", "target_chain": "ABCDE"}, {})
        assert err and "chain" in err.lower()


# ---------------------------------------------------------------------------
# 3. build_payload
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_shape(self):
        inp, err = ig.validate(
            {"preset": "inverse_design", "fasta": f">H\n{HEAVY}\n>L\n{LIGHT}",
             "target_chain": "B"}, {})
        assert err is None
        bp = ig.build_payload(inp, "https://example/presigned")
        assert set(bp) == {
            "preset", "run_task", "antibody_fasta", "fasta_origin",
            "antigen_chain", "epitope_pdb_resnums", "max_antigen_size",
            "num_samples", "total_passes", "n_masked", "relax", "parameters",
        }
        assert bp["relax"] is False
        assert bp["parameters"]["n_designs_total"] == inp["num_samples"]


# ---------------------------------------------------------------------------
# 4. Epitope conversion + antigen extraction (correctness guard)
# ---------------------------------------------------------------------------


def _ca_line(serial, resname, chain, resseq, icode, x, y, z, elem="C"):
    return (
        "ATOM  " + f"{serial:>5}" + " " + " CA " + " " + f"{resname:>3}" + " "
        + chain + f"{resseq:>4}" + icode + "   "
        + f"{x:>8.3f}" + f"{y:>8.3f}" + f"{z:>8.3f}"
        + "  1.00  0.00" + "          " + f"{elem:>2}"
    )


class TestEpitopeConversion:
    """Synthetic antigen on chain G, numbering starting at 100 with an
    insertion code — the exact shape that breaks a naive index."""

    def _pdb(self, tmp_path):
        lines = [
            _ca_line(1, "ALA", "G", 100, " ", 0.0, 0.0, 0.0),   # pos 1
            _ca_line(2, "GLY", "G", 100, "A", 3.8, 0.0, 0.0),   # pos 2 (insertion)
            _ca_line(3, "SER", "G", 101, " ", 7.6, 0.0, 0.0),   # pos 3
            _ca_line(4, "LEU", "G", 102, " ", 11.4, 0.0, 0.0),  # pos 4
            # a decoy on a different chain — must be ignored
            _ca_line(5, "MET", "H", 1, " ", 0.0, 50.0, 0.0),
        ]
        p = tmp_path / "antigen.pdb"
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_extraction_and_mapping(self, tmp_path):
        rp = pytest.importorskip("tools.iggm.run_pipeline")
        info = rp.antigen_chain_info(self._pdb(tmp_path), "G")
        assert info["seq"] == "AGSL"
        assert info["n_res"] == 4
        # PDB residue numbers map to 1-based sequential positions.
        assert info["resnum_to_pos"][100] == 1
        assert info["resnum_to_pos"][101] == 3
        assert info["resnum_to_pos"][102] == 4

    def test_convert_epitope(self, tmp_path):
        rp = pytest.importorskip("tools.iggm.run_pipeline")
        info = rp.antigen_chain_info(self._pdb(tmp_path), "G")
        pos, missing = rp.convert_epitope([100, 102], info["resnum_to_pos"])
        assert pos == [1, 4] and missing == []

    def test_convert_epitope_missing(self, tmp_path):
        rp = pytest.importorskip("tools.iggm.run_pipeline")
        info = rp.antigen_chain_info(self._pdb(tmp_path), "G")
        pos, missing = rp.convert_epitope([100, 999], info["resnum_to_pos"])
        assert pos == [1] and missing == [999]

    def test_wrong_chain_empty(self, tmp_path):
        rp = pytest.importorskip("tools.iggm.run_pipeline")
        info = rp.antigen_chain_info(self._pdb(tmp_path), "Z")
        assert info["n_res"] == 0

    def test_epitope_contacts_identifies_antigen_by_length(self):
        """Output complex where IgGM RENAMED the antigen chain (Q, not the
        input G). Contacts must still be counted (antigen picked by closest
        length to antigen_length=4), not silently zeroed."""
        rp = pytest.importorskip("tools.iggm.run_pipeline")
        lines = [
            _ca_line(1, "ALA", "Q", 1, " ", 0.0, 0.0, 0.0),    # antigen pos 1
            _ca_line(2, "GLY", "Q", 2, " ", 3.8, 0.0, 0.0),    # pos 2
            _ca_line(3, "SER", "Q", 3, " ", 7.6, 0.0, 0.0),    # pos 3
            _ca_line(4, "LEU", "Q", 4, " ", 11.4, 0.0, 0.0),   # pos 4
            _ca_line(5, "TRP", "H", 1, " ", 2.0, 0.0, 0.0),    # antibody, near pos 1
        ]
        pdb_text = "\n".join(lines) + "\n"
        res = rp.epitope_contacts(pdb_text, 4, [1, 4])
        assert res["n_contacted"] == 1 and res["contacted"] == [1]


# ---------------------------------------------------------------------------
# 5. Pricing wiring
# ---------------------------------------------------------------------------


class TestPricingWiring:
    def test_tool_specs(self):
        from shared import wallet_estimates as we
        spec = we.TOOL_SPECS["iggm"]
        assert spec.gpu_class == "A100-40GB"
        assert spec.scaling_param == "num_samples"

    def test_hard_cap(self):
        from shared import wallet as w
        assert w.PER_JOB_HARD_CAP_USD["iggm"] == __import__("decimal").Decimal("75.00")

    def test_preset_caps(self):
        from gpu import modal_client as mc
        for p in ("complex_prediction", "cdr_design", "fr_design",
                  "affinity_maturation", "inverse_design"):
            assert mc.preset_gpu_seconds("iggm", p) > 0
        assert mc.modal_app_name("iggm") == "ranomics-iggm-prod"

    def test_estimate_scales(self):
        from shared import wallet_estimates as we
        e1 = we.estimated_cost_for_tool(None, "iggm", {"num_samples": 1})
        e100 = we.estimated_cost_for_tool(None, "iggm", {"num_samples": 100})
        assert e100 > e1
        # stays under the absolute cap
        assert e100 <= __import__("decimal").Decimal("75.00")

    def test_maturation_effective_scaling(self):
        # affinity_maturation expands per masked position, so a 10-sample run on
        # a 5-X FASTA must price like 50 passes (not 10) — otherwise the hold
        # under-covers the real compute by the mask-count factor.
        from shared import wallet_estimates as we
        mat = we.estimated_cost_for_tool(None, "iggm", {
            "preset": "affinity_maturation", "num_samples": 10,
            "fasta": ">H\nAAAAAXXXXXAAAAA",  # 5 X on a sequence line
        })
        plain50 = we.estimated_cost_for_tool(None, "iggm", {"num_samples": 50})
        plain10 = we.estimated_cost_for_tool(None, "iggm", {"num_samples": 10})
        assert mat == plain50
        assert mat > plain10
        # the hard cap expands the same way (both clamp identically)
        assert we.compute_hard_cap("iggm", {
            "preset": "affinity_maturation", "num_samples": 10,
            "fasta": ">H\nAAAAAXXXXXAAAAA",
        }) == we.compute_hard_cap("iggm", {"num_samples": 50})

    def test_maturation_scaling_lowercase_mask(self):
        # lowercase x masks are real design positions (validate uppercases them),
        # so the estimator must count them too or the hold under-covers.
        from shared import wallet_estimates as we
        lower = we.estimated_cost_for_tool(None, "iggm", {
            "preset": "affinity_maturation", "num_samples": 10,
            "fasta": ">H\naaaaaxxxxxaaaaa",  # 5 lowercase x
        })
        upper = we.estimated_cost_for_tool(None, "iggm", {
            "preset": "affinity_maturation", "num_samples": 10,
            "fasta": ">H\nAAAAAXXXXXAAAAA",  # 5 uppercase X
        })
        assert lower == upper

    def test_maturation_scaling_prefers_stored_total_passes(self):
        # At settle the params are the stored job_spec: they carry the
        # pre-computed total_passes but no raw `fasta` string, so the estimator
        # must use total_passes directly (not fall back to raw num_samples).
        from shared import wallet_estimates as we
        stored = we.estimated_cost_for_tool(None, "iggm", {
            "preset": "affinity_maturation", "num_samples": 10, "total_passes": 50,
        })
        plain50 = we.estimated_cost_for_tool(None, "iggm", {"num_samples": 50})
        assert stored == plain50


# ---------------------------------------------------------------------------
# 6. Templates parse (Jinja syntax)
# ---------------------------------------------------------------------------


class TestTemplatesParse:
    def test_templates_parse(self):
        from pathlib import Path
        from jinja2 import Environment
        env = Environment()
        base = Path(__file__).resolve().parents[1] / "templates" / "tools"
        for name in ("iggm_form.html", "iggm_results.html"):
            src = (base / name).read_text(encoding="utf-8")
            env.parse(src)  # raises TemplateSyntaxError on bad syntax


# ---------------------------------------------------------------------------
# 7. The form actually RENDERS through Flask
# ---------------------------------------------------------------------------
#
# TestTemplatesParse above only checks Jinja *syntax*: env.parse() never
# resolves url_for(), so it happily accepted the stale
# ``url_for('tool_submit')`` that 500'd /tools/iggm in production once the
# flag went on. Render the route for real. See also
# tests/test_template_endpoints.py, which statically catches the same bug
# class across every template — including iggm_results.html, which has no
# render test of its own (other tools render theirs against a fake job; see
# tests/test_esmfold_smoke.py).


@pytest.fixture
def app_with_iggm_flag(monkeypatch):
    """Boot the tools-hub Flask app with FLAG_TOOL_IGGM=on so the route
    resolves rather than 404s."""
    monkeypatch.setenv("FLAG_TOOL_IGGM", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _login_session(client, email="user@example.com"):
    """Set session cookie so ``@login_required`` routes pass."""
    with client.session_transaction() as sess:
        sess["user_email"] = email


def _patch_user_ctx(monkeypatch):
    """Stub every Supabase-backed call on the GET /tools/iggm render path.

    Three separate calls reach Supabase, and all fail closed — so the test
    passes either way and the network I/O is invisible unless you trace it.
    Leaving them live would make this file's "runs fully offline" contract a
    lie and add real HTTPS round-trips whenever SUPABASE_URL is set (app.py
    calls load_dotenv(), and the main checkout has a .env). Measured: with
    SUPABASE_URL pointed at an unroutable host this test took 7.3s vs 2.0s
    stubbed — that delta was real connection attempts.

      1+2. blueprints.tools.load_user_context / get_or_create_wallet
           (blueprints/tools.py:686 — wallet panel first paint).
      3.   app.load_user_context — the app-level inject_workspace_context
           context processor (app.py:400) fires on EVERY render_template and
           binds load_user_context in the *app* namespace, so patching the
           blueprints.tools binding does not reach it. With only user_email
           in the session it would call _resolve_user_id -> Supabase
           auth.admin.list_users(). Returning None short-circuits it at
           app.py:435 and the nav degrades gracefully.

    Note: seeding sess["user_id"] instead would make this WORSE — it skips
    list_users() but lets get_tier(), active_workspaces_count(), and the nav
    wallet query all reach Supabase.
    """
    from types import SimpleNamespace

    monkeypatch.setattr(
        "blueprints.tools.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    monkeypatch.setattr(
        "blueprints.tools.get_or_create_wallet",
        lambda user_id: {"balance_usd": "10.00"},
    )
    monkeypatch.setattr("app.load_user_context", lambda: None)


class TestFormRenders:
    def test_form_renders_when_flag_on(self, app_with_iggm_flag, monkeypatch):
        """GET /tools/iggm returns 200 with every IgGM input present."""
        _patch_user_ctx(monkeypatch)
        client = app_with_iggm_flag.test_client()
        _login_session(client)

        resp = client.get("/tools/iggm")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)

        # Posts to the blueprint-qualified endpoint, not the pre-refactor name.
        assert 'action="/tools/iggm/submit"' in body

        # All five run_task modes are offered.
        for preset in (
            "complex_prediction", "cdr_design", "fr_design",
            "affinity_maturation", "inverse_design",
        ):
            assert f'name="preset" value="{preset}"' in body

        # The inputs that map onto IgGM's design.py flags.
        for field in (
            "fasta", "target_pdb", "target_chain", "epitope",
            "fasta_origin", "num_samples", "max_antigen_size",
        ):
            assert f'name="{field}"' in body

    def test_form_404s_when_flag_off(self, app_with_iggm_flag, monkeypatch):
        """With the flag removed the route must 404 — launch-gate contract."""
        monkeypatch.delenv("FLAG_TOOL_IGGM", raising=False)
        _patch_user_ctx(monkeypatch)
        client = app_with_iggm_flag.test_client()
        _login_session(client)

        assert client.get("/tools/iggm").status_code == 404
        # The 404 must come from the flag, not from the adapter having
        # vanished from the registry — otherwise this passes for the wrong
        # reason and stops testing the launch gate at all.
        assert get_adapter("iggm") is not None
