"""``?clone_from=`` must re-run the SAME job, not a differently-parameterised one.

Cloning is the documented way to scale a run up: start small, look at the
output, clone, raise the count. That contract is broken the moment a
stored input does not reach the field it came from — the user gets a form
that LOOKS like their earlier run, submits it, and pays for something
else. Nothing anywhere reports the difference.

Three instances were found by three different people before this test
existed, which is the argument for testing the property rather than the
instances:

* ``rfdiffusion`` and ``proteina`` hard-coded ``value="4"`` / ``value="8"``
  on the design count and ignored ``pre_fill`` entirely.
* ``rfantibody`` hard-coded ``value="H1:8,H2:7,H3:10-16"`` on
  ``cdr_lengths``, which it stores flat, so ``clone_from`` carried the key
  and the template discarded it.
* ``rfdiffusion`` hard-coded the binder-length window, which it stores
  NESTED as ``binder_length={min,max}`` — so a name lookup could not have
  found it even with ``pre_value()`` in place. That half is fixed in
  ``blueprints/tools.py::_normalize_clone_pre_fill``.

The inputs are not hand-written here. Each tool's own ``validate()`` is
called on a filled-in form and its return value IS ``job.inputs`` — that
is what ``tool_submit`` persists — so the fixture cannot drift away from
the real stored shape the way a literal dict would.

SCOPE, stated plainly. This asserts that every stored key which NAMES A
FORM FIELD reaches that field. It does not assert anything about stored
keys that name no field, and there are many: iggm stores the antibody
FASTA as ``antibody_fasta`` while the field is ``fasta``, af2 stores
parsed ``fasta_records`` rather than the pasted text, opendde stores a
built ``spec``. Cloning those tools loses those values too, but fixing it
means re-serialising parsed structures back into textareas — a different
and larger change than making a name lookup work. See the D3 findings in
the PR description.
"""

from __future__ import annotations

import html as _html
import os
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")


# One superset form covering every field name across the 14 templates.
# Values are deliberately NOT any tool's default, so a field that ignores
# pre_fill renders its default and the comparison fails.
_FORM: dict[str, str] = {
    "target_chain": "B",
    "chains_to_design": "B,C",
    "hotspot_residues": "54,56,115",
    "chain_hotspots": "B54,B56",
    "binder_length_min": "70",
    "binder_length_max": "90",
    "binder_length": "95",
    "num_designs": "37",
    "budget": "9",
    "protocol": "nanobody-anything",
    "cdr_lengths": "H1:9,H2:8,H3:12-15",
    "num_seq_per_target": "23",
    "sampling_temp": "0.35",
    "fixed_positions": "B:7,9",
    "num_recycles": "5",
    "use_templates": "on",
    "fasta": ">H\n" + "QVQLVESGGG" * 9 + "XXXXX" + "\n>L\n"
             + "DIQMTQSPSS" * 9 + "FGGGTKVEIK",
    "fasta_origin": "",
    "fasta_text": ">p\nMKWVTFISLLFLFSSAYS",
    "sequences": ">d0\nMKWVTFISLLFLFSSAYS",
    "binder_sequences": ">d0\nQVRLQESGPGLVQPSQTLSLTCMKWVTFISLLFLFSSAYS",
    "target_mode": "paste",
    "target_sequence": "MHVAQPAVVLASSRGIASFVCEYASPGKATEVRVTVLRQ"
                       "ADSQVTEVCAATYMHVAQPAVVLASSRGIASFVCEYASPGKA",
    "binder_framework": "atezolizumab_framework_vhvl",
    "target_name": "egfr",
    "seed": "7",
    "n_seeds": "2",
    "batch_size": "2",
    "use_scaling_critics": "on",
    "spec_mode": "guided",
    "proteins": ">A\nMQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQ"
                "QRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG",
    "dna": "",
    "rna": "",
    "ligands": "",
    "spec_json": "",
    "sample": "3",
    "step": "4",
    "cycle": "2",
    "epitope": "12,13,14",
    "max_antigen_size": "1500",
    "num_samples": "2",
    "task_name": "",
    "target_input": "B1-150",
    # Not a rendered field — proteina's validate() branches on it to tell
    # a bring-your-own-target run from a curated benchmark task.
    "_has_custom_target": "1",
}


@pytest.fixture(scope="module")
def tools_app():
    """Every registered adapter, flagged on."""
    import app as app_module
    from shared.feature_flags import flag_name
    from tools import base as tool_base

    adapters = sorted(tool_base.all_adapters(), key=lambda a: a.slug)
    # tools.base._REGISTRY is populated only as a side effect of importing
    # app; an empty registry would make every assertion below vacuous.
    assert len(adapters) >= 14, f"adapter registry holds {len(adapters)}"
    prior = {}
    for a in adapters:
        prior[flag_name(a.slug)] = os.environ.get(flag_name(a.slug))
        os.environ[flag_name(a.slug)] = "on"
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    yield flask_app, adapters
    for key, val in prior.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _stored_inputs(adapter) -> dict:
    """What ``tool_submit`` would persist as ``job.inputs`` for this tool.

    Walks the presets until one validates — a few tools reject the
    superset form on some presets (proteina's curated variants refuse a
    custom target), and any accepted preset exercises the same
    pre_fill path.
    """
    for preset in adapter.presets:
        form = dict(_FORM, preset=preset.slug)
        if adapter.slug in ("af2", "colabfold", "esmfold"):
            # These branch on preset: the batch field and the single-FASTA
            # field are mutually exclusive and validate() rejects both.
            form.pop("sequences" if preset.slug != "batch" else "fasta", None)
            form.pop("sequences" if preset.slug != "batch" else "fasta_text", None)
        inputs, err = adapter.validate(form, {})
        if inputs:
            return inputs
    raise AssertionError(f"{adapter.slug}: no preset validated the fixture form")


def _posted_value(html: str, name: str) -> str | None:
    """What the browser would POST for ``name``, read off the markup.

    Covers the four controls the 14 forms use: text/number input, checked
    radio or checkbox, selected <option>, and textarea body. Returns None
    when the field is absent.
    """
    for tag in re.findall(r"<input\b[^>]*>", html):
        if re.search(rf'\bname="{re.escape(name)}"', tag) is None:
            continue
        kind = re.search(r'\btype="([^"]*)"', tag)
        if (kind.group(1) if kind else "") in {"radio", "checkbox"} \
                and "checked" not in tag:
            continue
        val = re.search(r'\bvalue="([^"]*)"', tag)
        if val:
            return val.group(1)
    sel = re.search(
        rf'<select\b[^>]*\bname="{re.escape(name)}"[^>]*>(.*?)</select>',
        html, re.S,
    )
    if sel:
        opts = re.findall(r"<option\b[^>]*>", sel.group(1))
        for opt in opts:
            if "selected" in opt:
                v = re.search(r'\bvalue="([^"]*)"', opt)
                return v.group(1) if v else None
        if opts:  # no option marked: the browser posts the first
            v = re.search(r'\bvalue="([^"]*)"', opts[0])
            return v.group(1) if v else None
    # Attribute-aware rather than ``[^>]*``: several placeholders hold a
    # literal ">" (FASTA headers), which ends the tag early for a naive
    # pattern and makes the placeholder look like the textarea's body.
    for m in re.finditer(r'<textarea\b((?:[^>"]|"[^"]*")*)>(.*?)</textarea>',
                         html, re.S):
        if re.search(rf'\bname="{re.escape(name)}"', m.group(1)):
            # The body is HTML-escaped on the way out; compare source text.
            return _html.unescape(m.group(2))
    return None


def _field_names(html: str) -> set[str]:
    return set(re.findall(r'<(?:input|select|textarea)\b[^>]*\bname="([^"]+)"',
                          html))


def _clone_html(flask_app, adapter, inputs):
    client = flask_app.test_client()
    prior = SimpleNamespace(
        id="job-1234abcd", tool=adapter.slug, status="succeeded",
        inputs=inputs,
    )
    ctx = SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com",
    )
    with client.session_transaction() as sess:
        sess["user_email"] = "u@example.com"
    with patch("blueprints.tools.load_user_context", return_value=ctx), \
            patch("blueprints.tools.get_job", return_value=prior), \
            patch(
                "blueprints.tools.get_or_create_wallet",
                return_value={"balance_usd": 50, "wallet_frozen": False},
            ):
        resp = client.get(f"/tools/{adapter.slug}?clone_from=job-1234abcd")
    assert resp.status_code == 200, f"{adapter.slug} -> {resp.status_code}"
    return resp.get_data(as_text=True)


class TestCloneRoundTrip:

    def test_every_stored_input_named_by_a_field_reaches_it(self, tools_app):
        """The property. See the module docstring for what is out of scope."""
        flask_app, adapters = tools_app
        broken: list[str] = []
        checked = 0
        for adapter in adapters:
            inputs = _stored_inputs(adapter)
            html = _clone_html(flask_app, adapter, inputs)
            fields = _field_names(html)
            for key, want in inputs.items():
                if key.startswith("_") or key not in fields or want is None:
                    continue
                if isinstance(want, (dict, list, tuple)):
                    continue  # asserted by name below, not by identity
                checked += 1
                got = _posted_value(html, key)
                # A checkbox posts its literal ``value`` ("on") when set and
                # nothing at all when clear, so the round trip is about the
                # BOX being ticked, not about the string matching.
                ok = (
                    (got is not None) == bool(want)
                    if isinstance(want, bool) else got == str(want)
                )
                if not ok:
                    broken.append(
                        f"{adapter.slug}: stored {key}={want!r} but the "
                        f"cloned form renders {got!r}"
                    )
        assert checked >= 40, f"only {checked} fields compared; fixture is thin"
        assert not broken, broken

    def test_rfantibody_cdr_lengths(self, tools_app):
        """Stored flat; the template used to throw it away. D3, instance 1."""
        flask_app, adapters = tools_app
        adapter = next(a for a in adapters if a.slug == "rfantibody")
        inputs = _stored_inputs(adapter)
        assert inputs["cdr_lengths"] == "H1:9,H2:8,H3:12-15"
        html = _clone_html(flask_app, adapter, inputs)
        assert _posted_value(html, "cdr_lengths") == "H1:9,H2:8,H3:12-15"

    def test_rfdiffusion_nested_binder_length(self, tools_app):
        """Stored as {min,max}; two fields read it. D3, instance 2.

        This is the one a bare ``pre_value()`` could not have fixed —
        the key the form asks for does not exist in job.inputs at all.
        """
        flask_app, adapters = tools_app
        adapter = next(a for a in adapters if a.slug == "rfdiffusion")
        inputs = _stored_inputs(adapter)
        assert inputs["binder_length"] == {"min": 70, "max": 90}
        html = _clone_html(flask_app, adapter, inputs)
        assert _posted_value(html, "binder_length_min") == "70"
        assert _posted_value(html, "binder_length_max") == "90"

    def test_proteina_listed_binder_length(self, tools_app):
        """Same defect, different container: proteina stores ``[lo, hi]``."""
        flask_app, adapters = tools_app
        adapter = next(a for a in adapters if a.slug == "proteina")
        inputs = _stored_inputs(adapter)
        assert list(inputs["binder_length"]) == [70, 90]
        html = _clone_html(flask_app, adapter, inputs)
        assert _posted_value(html, "binder_length_min") == "70"
        assert _posted_value(html, "binder_length_max") == "90"

    def test_pxdesign_scalar_binder_length_is_not_corrupted(self, tools_app):
        """The normaliser must leave a scalar under the same name alone.

        pxdesign has ONE ``binder_length`` field and stores a plain int
        there. Exploding it into min/max unconditionally would have
        broken the tool that was already correct.
        """
        flask_app, adapters = tools_app
        adapter = next(a for a in adapters if a.slug == "pxdesign")
        inputs = _stored_inputs(adapter)
        html = _clone_html(flask_app, adapter, inputs)
        assert _posted_value(html, "binder_length") == "95"

    def test_mpnn_chain_selection_survives(self, tools_app):
        """MPNN's field is ``chains_to_design``; it stores ``target_chain``.

        Found by the sweep, not reported: a cloned MPNN job silently
        reset the designed chains to "A".
        """
        flask_app, adapters = tools_app
        adapter = next(a for a in adapters if a.slug == "mpnn")
        inputs = _stored_inputs(adapter)
        assert "chains_to_design" not in inputs
        html = _clone_html(flask_app, adapter, inputs)
        assert _posted_value(html, "chains_to_design") == str(
            inputs["target_chain"]
        )
