"""A cloned form re-submits the run it was cloned from, or says what it lost.

QA 2026-09-29 F1: "Try again with these settings" on a failed IgGM job opened
/tools/iggm?clone_from=<id> with the antibody FASTA, epitope and antigen PDB
empty and no message. iggm stores those under other names than its fields
(antibody_fasta / epitope_pdb_resnums / antigen_chain vs fasta / epitope /
target_chain), esmfold2-design stores the scFv framework as binder_name and
OpenDDE stores only the assembled spec.

    pytest tests/test_clone_prefill_restore.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_clone_roundtrip import (  # noqa: F401 (fixture)
    _FORM,
    _field_names,
    _posted_value,
    _stored_inputs,
    tools_app,
)

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _clone(flask_app, slug, inputs, stored=None, query="clone_from"):
    """``stored`` is what storage says about the staged input (None: unknown)."""
    client = flask_app.test_client()
    prior = SimpleNamespace(
        id="job-1234abcd", tool=slug, status="failed", inputs=inputs,
    )
    ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100, email="u@example.com")
    with client.session_transaction() as sess:
        sess["user_email"] = "u@example.com"
    with patch("blueprints.tools.load_user_context", return_value=ctx), \
            patch("blueprints.tools.get_job", return_value=prior), \
            patch("blueprints.tools.input_exists", return_value=stored, create=True), \
            patch("blueprints.tools.get_or_create_wallet",
                  return_value={"balance_usd": 50, "wallet_frozen": False}):
        resp = client.get(f"/tools/{slug}?{query}=job-1234abcd")
    assert resp.status_code == 200, f"{slug} -> {resp.status_code}"
    return resp.get_data(as_text=True)


def _resubmit(adapter, html):
    """What validate() makes of the cloned form, as the browser would post it."""
    form = {n: _posted_value(html, n) for n in _field_names(html)}
    form = {k: v for k, v in form.items() if v is not None}
    form["_has_custom_target"] = "1"  # injected by the route, never a field
    inputs, err = adapter.validate(form, {})
    assert err is None, f"{adapter.slug}: cloned form refused: {err}"
    return {k: v for k, v in inputs.items() if not k.startswith("_")}


def _public(inputs):
    return {k: v for k, v in inputs.items() if not k.startswith("_")}


def _adapter(adapters, slug):
    return next(a for a in adapters if a.slug == slug)


def test_every_tool_clone_resubmits_the_same_inputs(tools_app):
    flask_app, adapters = tools_app
    for adapter in adapters:
        inputs = _stored_inputs(adapter)
        html = _clone(flask_app, adapter.slug, inputs)
        assert _resubmit(adapter, html) == _public(inputs), adapter.slug


@pytest.mark.parametrize("slug,form", [
    ("esmfold2-design", {"preset": "scfv"}),
    ("opendde", {"spec_mode": "json",
                 "spec_json": '{"sequences": [{"proteinChain": {"sequence": "MKV", '
                              '"count": 2, "id": ["A", "B"]}}]}'}),
    ("opendde", {"spec_mode": "guided", "dna": ">D\nACGT", "rna": ">R\nACGU",
                 "ligands": "CCD_ATP"}),
])
def test_preset_specific_shapes_round_trip(tools_app, slug, form):
    flask_app, adapters = tools_app
    adapter = _adapter(adapters, slug)
    inputs, err = adapter.validate(dict(_FORM, **form), {})
    assert err is None, err
    html = _clone(flask_app, slug, inputs)
    assert _resubmit(adapter, html) == _public(inputs)


def test_iggm_restores_the_qa_fields(tools_app):
    flask_app, adapters = tools_app
    inputs = _stored_inputs(_adapter(adapters, "iggm"))
    html = _clone(flask_app, "iggm", inputs)
    assert _posted_value(html, "fasta").startswith(">H\nQVQLVESGGG")
    assert _posted_value(html, "epitope") == "12,13,14"
    assert _posted_value(html, "target_chain") == "B"


def _with_pdb(inputs):
    return dict(inputs, _pdb_storage_path="u-1/job-1234abcd/t.pdb", _pdb_filename="t.pdb")


@pytest.mark.parametrize("stored", [True, None])
def test_stored_upload_is_reused_and_nothing_is_flagged(tools_app, stored):
    flask_app, adapters = tools_app
    inputs = _with_pdb(_stored_inputs(_adapter(adapters, "iggm")))
    html = _clone(flask_app, "iggm", inputs, stored=stored)
    assert _posted_value(html, "reuse_pdb_token") == "job:job-1234abcd"
    assert "data-clone-missing" not in html


@pytest.mark.parametrize("query", ["clone_from", "from_job"])
def test_deleted_upload_is_not_offered_and_is_flagged(tools_app, query):
    flask_app, adapters = tools_app
    inputs = _with_pdb(_stored_inputs(_adapter(adapters, "iggm")))
    html = _clone(flask_app, "iggm", inputs, stored=False, query=query)
    assert _posted_value(html, "reuse_pdb_token") is None
    assert "data-clone-missing" in html
    assert "Structure file (upload it again)" in html


def test_row_without_the_stored_keys_lists_them(tools_app):
    flask_app, adapters = tools_app
    inputs = {"preset": "complex_prediction", "num_samples": 40}
    html = _clone(flask_app, "iggm", inputs)
    notice = html[html.index("data-clone-missing"):]
    notice = notice[:notice.index("</div>\n            </div>")]
    for label in ("Antibody FASTA", "Antigen chain", "Epitope residues",
                  "Structure file (upload it again)"):
        assert label in notice, label


def test_tool_without_a_structure_has_no_notice(tools_app):
    flask_app, adapters = tools_app
    inputs = _stored_inputs(_adapter(adapters, "esmfold"))
    assert "data-clone-missing" not in _clone(flask_app, "esmfold", inputs)


@pytest.mark.parametrize("slug,flagged", [
    ("esmfold", False),  # no structure field
    ("proteina", True),  # optional structure, requires_pdb=False
])
def test_handoff_flags_a_deleted_structure_only_where_the_form_takes_one(
    tools_app, slug, flagged,
):
    flask_app, adapters = tools_app
    inputs = _with_pdb(_stored_inputs(_adapter(adapters, "bindcraft")))
    html = _clone(flask_app, slug, inputs, stored=False, query="from_job")
    assert ("data-clone-missing" in html) is flagged
