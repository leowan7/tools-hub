"""The BoltzGen antibody and peptide protocols are gone, and stay gone.

``antibody-anything`` and ``peptide-anything`` were offered by the form
and accepted by ``tools/boltzgen/__init__.py::validate`` with no record
on trunk of either having been run through this app --
``docs/SESSION-HANDOFF-2026-05-30.md`` files the live nanobody / antibody
/ peptide proof as deferred and names the multi-chain antibody output
and very short peptide lengths as the untested risks, and no later doc on trunk records the proof. Both were
removed on 2026-11-13, leaving ``protein-anything`` and
``nanobody-anything``.

Four things are pinned here, because removing an ``<option>`` alone
would leave the others intact:

1. The options are not in the rendered form.
2. A POST still naming either one is REFUSED, not coerced to another
   protocol. This is the one that costs money if it regresses: a silent
   coercion bills a GPU run the caller did not ask for.
3. Cloning a run stored with a removed protocol is reported to the user
   rather than silently re-pointed at ``protein-anything`` -- the same
   money failure, reached through the browser instead of the server.
4. The customer-facing copy no longer advertises either format.

The nanobody option is deliberately still offered, so every assertion
below names the removed wire values rather than sweeping the whole
antibody or peptide vocabulary out of the tool.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from shared.tool_meta import meta_for

pytestmark = pytest.mark.usefixtures("isolate_supabase")

GONE = ("antibody-anything", "peptide-anything")
KEPT = ("protein-anything", "nanobody-anything")


def _valid_form(**overrides) -> dict:
    """The smallest form ``validate`` accepts, before overrides."""
    form = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "",
        "binder_length_min": "50",
        "binder_length_max": "100",
        "budget": "4",
        "protocol": "protein-anything",
    }
    form.update(overrides)
    return form


def test_the_baseline_form_still_validates():
    """Guards the tests below: if this ever stopped validating, a
    refusal there would prove nothing about the protocol field."""
    from tools import boltzgen

    inputs, err = boltzgen.validate(_valid_form(), {})
    assert err is None, err
    assert inputs is not None and inputs["protocol"] == "protein-anything"


@pytest.mark.parametrize("removed", GONE)
def test_validate_refuses_a_removed_protocol(removed):
    from tools import boltzgen

    inputs, err = boltzgen.validate(_valid_form(protocol=removed), {})
    # Refused outright. NOT coerced: an inputs dict carrying any
    # protocol at all would mean the submit went on to build a payload.
    assert inputs is None
    assert err and removed not in err
    # The message names what IS accepted rather than only saying no.
    for kept in KEPT:
        assert kept in err, f"refusal message does not offer {kept}: {err}"


def test_the_allowed_set_is_the_two_kept_protocols():
    from tools import boltzgen

    assert boltzgen.ALLOWED_PROTOCOLS == frozenset(KEPT)


@pytest.mark.parametrize("removed", GONE)
def test_cloning_a_retired_protocol_is_reported_as_missing(
    all_tools_app, removed
):
    """A stored protocol the form no longer offers is surfaced.

    ``clone_from`` copies ``job.inputs`` into ``pre_fill`` and the
    ``<select>`` looks the value up by name (blueprints/tools.py, the
    ``clone_from`` branch of the tool form view). With the matching
    ``<option>`` deleted no option carries ``selected``, so the browser
    falls back to the first -- ``protein-anything`` -- and the form posts
    a protocol the user never chose, which ``validate`` then accepts.
    """
    from blueprints.tools import _clone_missing
    from tools import base as tool_base

    flask_app, _slugs = all_tools_app
    adapter = {a.slug: a for a in tool_base.all_adapters()}["boltzgen"]
    prior = {"preset": "pilot", "protocol": removed}

    with flask_app.app_context():
        # A non-None pdb_source short-circuits the structure check, which
        # is not what this test is about.
        missing = _clone_missing(adapter, prior, {}, {"label": "stored pdb"})
        kept_ok = _clone_missing(
            adapter,
            {"preset": "pilot", "protocol": "protein-anything"},
            {},
            {"label": "stored pdb"},
        )

    assert any(m.startswith("Protocol (") for m in missing), missing
    # ...and a protocol still offered is NOT reported, so the guard is
    # answering the stored value and not firing on every boltzgen clone.
    assert not any(m.startswith("Protocol (") for m in kept_ok), kept_ok


@pytest.fixture(scope="module")
def boltzgen_form_html(all_tools_app):
    flask_app, _slugs = all_tools_app
    with flask_app.test_client() as client:
        resp = client.get("/tools/boltzgen")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def test_the_rendered_form_offers_exactly_the_kept_options(boltzgen_form_html):
    options = re.findall(
        r'<select[^>]*name="protocol"[^>]*>(.*?)</select>',
        boltzgen_form_html,
        flags=re.S,
    )
    assert len(options) == 1, f"{len(options)} protocol selects on the page"
    values = re.findall(r'value="([^"]*)"', options[0])
    assert values == list(KEPT), values


def test_the_page_never_mentions_a_removed_protocol(boltzgen_form_html):
    """Covers the help copy and the about panel, not just the select."""
    for removed in GONE:
        assert removed not in boltzgen_form_html, removed


def test_the_form_template_claims_no_antibody_framework():
    """The copy that was false, read off the template itself.

    Not off the rendered page: /tools/boltzgen also renders the
    site-wide tool FAQ, where RFantibody says "hybridoma"
    (tools/rfantibody/meta.py) and IgGM says "framework"
    (tools/iggm/meta.py) about themselves.

    ``tools/boltzgen/__init__.py::validate`` reads preset, target_chain,
    hotspot_residues, binder_length_min, binder_length_max, budget and
    protocol -- nothing naming a framework or a starting sequence -- and
    ``build_payload`` sends the binder as a length window only. That is
    what the replacement copy claims, and all it claims: what BoltzGen
    does inside the container is not asserted, because the wrapper lives
    in llm-proteinDesigner and nothing in this repo could enforce it.
    """
    src = pathlib.Path("templates/tools/boltzgen_form.html").read_text(
        encoding="utf-8"
    )
    # Jinja comments carry the rationale for the removal and name the
    # words on purpose; it is the RENDERED copy that must not claim one.
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S).lower()
    # The removed sentence, by its load-bearing phrases. The bare word
    # "framework" is NOT one of them: the replacement copy uses it to say
    # the form collects no framework, which is the opposite claim.
    for claim in ("antibody framework", "hybridoma", "igg format"):
        assert claim not in src, claim
    # ...and the replacement says so positively, so a later edit cannot
    # drop the correction and still pass the checks above.
    assert "takes a framework or a starting sequence" in src


def test_the_about_panel_lists_exactly_the_allowed_protocols():
    """The about panel enumerates protocol values by hand.

    The form's own ``<option>`` labels name the same wire values and are
    checked above against the rendered select; this panel is prose and
    is not, so a protocol added to or removed from ``ALLOWED_PROTOCOLS``
    without touching it would document a set the server does not accept.
    """
    from tools import boltzgen

    explanation = next(
        i["explanation"]
        for i in meta_for("boltzgen").about["inputs"]
        if i["name"] == "Protocol"
    )
    named = set(re.findall(r"<code>([a-z_-]+-anything)</code>", explanation))
    assert named == set(boltzgen.ALLOWED_PROTOCOLS)


def test_the_customer_copy_advertises_neither_removed_format():
    """The BoltzGen strings the catalog, /tools and the about panel render.

    Not every string in meta.py: this list is the label, the blurb, the
    comparison one-liner, the about panel, the SEO FAQ, and the PILOT
    and EXAMPLE blocks.
    """
    from tools import base as tool_base

    meta = meta_for("boltzgen")
    adapter = {a.slug: a for a in tool_base.all_adapters()}["boltzgen"]

    blobs: list[str] = [adapter.label, adapter.blurb, meta.comparison_one_liner]
    blobs += [str(v) for v in meta.about.values()]
    blobs += [f["q"] + " " + f["a"] for f in meta.seo_faq]
    blobs += [str(v) for v in (meta.PILOT or {}).values()]
    blobs += [str(v) for v in (getattr(meta, "EXAMPLE", None) or {}).values()]

    for blob in blobs:
        low = blob.lower()
        assert "antibod" not in low, blob
        assert "peptide" not in low, blob
