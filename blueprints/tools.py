"""Tool routes (blueprint refactor, Commit 7c -- last money-path step).

developability CPU tool, the retired library-planner redirects, the
/tools/<tool> preview/form, preflight, tool_submit (the @login_required
@idempotent() @requires_wallet money stack, unchanged), and the /tools
comparison matrix. Lifted verbatim
from ``create_app()``; only ``@flask_app.route`` -> ``@tools_bp.route``, the
factory-local modal_client -> current_app.modal_client, and self-refs ->
``tools.*``. The tool_form preview helpers move in with the routes.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from flask import (
    Blueprint,
    current_app,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from markupsafe import Markup

from scout.handoff import VALID_HANDOFF_TOOLS
from shared import resample as _resample
from shared import tool_chooser
from shared.auth import login_required
from shared.credits import load_user_context
from shared.feature_flags import tool_enabled
from shared.handoffs import get_handoff, mark_consumed
from shared.idempotency import idempotent
from shared.jobs import (
    create_job,
    get_job,
    list_jobs_paginated,
    mark_failed,
    set_modal_call,
    update_inputs,
)
from shared.pdb_inspect import (
    CifConversionError,
    convert_cif_to_pdb_bytes,
    hotspot_range_message,
    inspect_pdb_bytes,
    split_hotspot,
    summarize_for_log,
    validate_hotspots,
    validate_target_chain,
)
from shared.pdb_intake import (
    _fetch_alphafold_bytes,
    _parse_preflight_size_params,
    _verdict_to_json,
    _verify_reuse_pdb_bytes,
    preflight_target_segments,
)
from shared.pdb_preflight import (
    PREFLIGHT_TOOLS,
    preflight_for_tool,
    shipped_hotspots,
)
from shared.storage import (
    StorageError,
    copy_input,
    download_input,
    input_exists,
    presigned_input_url,
    upload_input,
)
from shared.tools_catalog import (
    _build_tools_catalog,
    _short_name_for_label,
    group_catalog,
)
from shared.wallet import get_or_create_wallet, release_hold as wallet_release_hold
from shared.tool_meta import meta_for
from shared.wallet_guard import requires_wallet
from tools import base as tool_base

logger = logging.getLogger(__name__)

tools_bp = Blueprint("tools", __name__)

# A submit-side guard flips into "panel present" mode for these tools even
# when no evaluator exists, because it guards a different thing (panel markup
# present in the template) than PREFLIGHT_TOOLS. The plain ``error`` string
# fallback in tool_submit is the defensive net.
_PREFLIGHT_PANEL_FORMS: frozenset = frozenset(
    {"rfantibody", "rfdiffusion", "bindcraft", "boltzgen", "pxdesign", "boltz2",
     "proteina"}
)


@tools_bp.route("/developability", methods=["GET"])
def developability():
    """Render the Binder Developability Scout input form.

    Open to anonymous visitors. Developability Scout is the only member of
    the "See if a binder will hold up in the lab" catalog band, so gating it
    made that whole band a login wall -- while its catalog card promises
    "see how it works". There is nothing to gate: scoring is a pure function
    over a pasted sequence with no GPU, no wallet charge, no storage write,
    and no user identity. The trust boundary lives in developability_score.
    """
    return render_template(
        "developability_form.html",
        error=None,
        sequence="",
        chain_type="VH",
    )

@tools_bp.route("/developability/score", methods=["POST"])
@idempotent()
def developability_score():
    """Validate input and render the developability results page.

    Anonymous like its GET: opening the form but redirecting the submit to
    login would be exactly the "promise you do not keep" this redesign
    exists to remove. Safe to open because the handler spends nothing and
    persists nothing -- it calls score_developability(), a pure function
    over the cleaned string, and renders a template.

    The trust boundary is enforced below and is unchanged by opening the
    route: FASTA headers stripped, length bounded to 10-2000 residues,
    canonical amino acids only, chain_type checked against an allowlist.
    That bounds the work any one anonymous request can ask for.

    Deliberately NOT adding a per-IP rate limiter here: the repo has no HTTP
    rate-limiting utility to reuse, and bolting a bespoke one onto this route
    while /api/wallet/estimate (already anonymous, and it does an uncached DB
    query) has none would be inconsistent. Request-rate control belongs at
    the edge/proxy, not in one view.
    """
    from tools.developability import score_developability  # noqa: PLC0415

    raw_sequence = request.form.get("sequence", "")
    chain_type = request.form.get("chain_type", "VH").strip() or "VH"

    # Strip FASTA headers (lines starting with '>') and whitespace.
    lines = [
        line.strip()
        for line in raw_sequence.splitlines()
        if line and not line.lstrip().startswith(">")
    ]
    cleaned_sequence = "".join(lines).replace(" ", "").upper()

    # Allowed chain types for the UI select; scoring accepts broader set.
    allowed_chains = {"VH", "VL", "VK", "SCFV", "VHH", "OTHER"}
    if chain_type.upper() not in allowed_chains:
        chain_type = "VH"
    chain_type = chain_type.upper()

    # Sequence validation.
    valid_aa = set("ACDEFGHIKLMNPQRSTVWY")
    error = None
    if not cleaned_sequence:
        error = "Paste a sequence before submitting."
    elif not (10 <= len(cleaned_sequence) <= 2000):
        error = (
            f"Sequence length must be between 10 and 2000 residues "
            f"(got {len(cleaned_sequence)})."
        )
    else:
        bad = sorted(set(cleaned_sequence) - valid_aa)
        if bad:
            error = (
                "Sequence contains non-canonical residues: "
                + ", ".join(bad)
                + ". Only the 20 standard amino acids are accepted."
            )

    if error:
        return render_template(
            "developability_form.html",
            error=error,
            sequence=raw_sequence,
            chain_type=chain_type,
        )

    try:
        result = score_developability(
            cleaned_sequence,
            chain_type=chain_type,
        )
    except ValueError as exc:
        return render_template(
            "developability_form.html",
            error=str(exc),
            sequence=raw_sequence,
            chain_type=chain_type,
        )

    return render_template(
        "developability_results.html",
        result=result,
    )

# ------------------------------------------------------------------
# Yeast Display Library Planner — RETIRED 2026-09-30 at Leo's request.
#
# Both endpoints stay registered and 301 to /tools. They are not 404ed
# because "try the tool" callouts in the ranomics.com marketing site link
# straight at https://tools.ranomics.com/library-planner, and that path
# answered 200 up to this commit (curl, 2026-09-30). A 301 hands those
# readers, and any search engine holding the URL, the catalog instead of
# an error. Treat the redirect as permanent, not a grace period.
#
# How many inbound links exist is not verifiable from this repo; their
# shape is. Measured read-only on 2026-09-30 in the local ranomics.com
# checkout, which is 93 commits behind its main (last fetched
# 2026-09-15), so re-grep their main before relying on it: four lines
# in three files point at this path -- the shared TryToolCallout
# component's tool-map entry (src/components/TryToolCallout.astro), a
# launchUrl in src/content/pages/tools/library-planner.mdx, and two
# hard-coded target="_blank" launch buttons in
# src/pages/technology/library-planner.astro. Only the first sits in
# that map, so one edit to the map does NOT drop the inbound links.
# src/components/layout/Footer.astro, which PageLayout renders on every
# page, and src/data/navigation.ts both link their own
# /technology/library-planner page, which is the funnel into those two
# buttons. A site-wide footer link is the slowest thing on a website to
# get cleaned up, so this redirect has to keep working indefinitely.
# The website lead reported 65 lines across 22 files on 2026-09-30;
# that count is secondhand and is not reproduced here.
#
# 301 and not 308 on the POST: a 308 preserves the method and would
# re-POST a stale bookmarked form to /tools, which is GET-only
# (tools_comparison, this file) and would answer 405. A 301 is
# downgraded to GET by browsers, which is the landing we want.
#
# The POST is also exempt from the app-wide CSRF guard
# (app.py::_csrf_request_is_exempt). Without that it answers 403, not
# 301: enforcement defaults ON, and although the planner form does carry
# csrf_input(), the form is no longer served -- the GET below 301s -- so
# no caller can obtain a token matching the current session. A replay
# from a cached copy presents a rotated token, or none at all if the
# reader has no session, and both are refused. Measured with
# CSRF_PROTECT=1 in
# tests/test_csrf_protection.py::test_retired_library_planner_post_is_exempt.
#
# tools/library_planner/ and templates/library_planner_*.html are left
# on disk on purpose: the package is matched by the 'tools/**' deploy
# trigger (.github/workflows/deploy-modal.yml:79, whose only negations
# are meta.py, example/** and __init__.py), and that workflow's matrix
# has no per-path routing, so deleting the package would redeploy all
# nine GPU Modal apps for a change that touches no GPU code.
#
# No historical job row is orphaned by this: planning never wrote one,
# and a census of public.tool_jobs on 2026-09-30 returned 0 rows for
# any 'librar*' tool slug. The table's total is not recorded here: it is
# live and grows, so any figure written down dates itself (it moved
# between two runs of the census on the same day). The 0 is the load-
# bearing half, and it is what justified dropping the dead
# library_planner branch from templates/job_detail.html.
# ------------------------------------------------------------------

def _retired_planner_redirect():
    """301 to /tools, carrying the query string across.

    The query string is carried for the same reason as in
    proteinmpnn_slug_redirect below, and this is its copy. Two reasons
    here. The inbound "try the tool" links are reported to be UTM-decorated
    at render by the marketing site's callout component, which would make a
    bare redirect land every one of them on /tools as direct traffic and
    hide the arrivals this redirect exists to serve; that is the website
    lead's report about another repository, unverified from here, and this
    comment does not establish it. The second reason needs no other
    repository: tools_comparison in this file reads asked, have and shape
    off request.args itself, so a parameterised link would be truncated on
    arrival whatever the marketing site does.

    QUERY_STRING arrives as raw bytes and decoding it strictly raises
    UnicodeDecodeError on a malformed one, which would 500 instead of
    redirecting, so substitute U+FFFD. Both halves are pinned by
    test_query_string_is_carried_across and
    test_malformed_query_string_still_redirects in
    tests/test_library_planner_retired.py.
    """
    target = url_for("tools.tools_comparison")
    qs = request.query_string.decode("utf-8", errors="replace")
    if qs:
        target = f"{target}?{qs}"
    return redirect(target, code=301)


@tools_bp.route("/library-planner", methods=["GET"])
def library_planner():
    """Permanently redirect the retired Library Planner form to /tools."""
    return _retired_planner_redirect()

@tools_bp.route("/library-planner/plan", methods=["POST"])
def library_planner_plan():
    """Permanently redirect the retired planner submit to /tools.

    @idempotent() was dropped with the handler body. The decorator is for
    routes where a replay costs real money or real work
    (shared/idempotency.py::idempotent); a redirect costs neither, and
    keeping it would have let a half-configured ledger 503 a 301.
    """
    return _retired_planner_redirect()


# ------------------------------------------------------------------
# GPU tool routes — one form/submit pair per registered adapter,
# plus shared jobs routes. FLAG_TOOL_<NAME>=off hides a tool at the
# route level so the UI can ship in one commit and the operator
# flips the flag after verifying an end-to-end production run.
# ------------------------------------------------------------------

def _require_tool(tool_slug: str):
    """Return (adapter, error_response). ``error_response`` is non-None on fail."""
    adapter = tool_base.get(tool_slug)
    if adapter is None:
        return None, (render_template("404.html"), 404)
    if not tool_enabled(tool_slug):
        return None, (render_template("coming_soon.html"), 404)
    return adapter, None

# ------------------------------------------------------------------
# B2 — public preview page helpers. Used by the logged-out branch
# of /tools/<slug> and only there.
# ------------------------------------------------------------------

# SEO phrase pairs per tool slug. Both land in ONE rendered sentence
# (templates/tools/_form_hero.html):
#
#     "{short_name} is a {seo_phrase} you can run through
#      tools.ranomics.com on a dedicated GPU. {seo_long}."
#
# Two rules follow from that frame and both were broken before:
#
#   1. ``seo_phrase`` must NOT contain the tool's own name. Every entry
#      used to start with it, so the lede rendered "RFdiffusion is a
#      RFdiffusion de novo binder design online you can run through...".
#      Byte-identical to the deleted _preview.html, faithfully restored,
#      and wrong on all 14 pages.
#   2. Every registered tool needs an entry. The old fallback
#      interpolated the raw slug — "ESMFold2 design is a free
#      esmfold2-design tool online" — so the tools added after the map
#      was written leaked a slug on an indexable page. The fallback
#      below no longer mentions the slug at all, and the map now covers
#      all 14 so it should never fire.
#
# ``seo_phrase`` is a compact noun phrase completing "is a ..." and
# must NOT end in a subordinate clause: "...against a target you
# upload" renders as "a target you upload you can run through",
# which parses as nonsense. Task detail belongs in ``seo_long``,
# a sentence WITHOUT a terminal period; the template adds it.
#
# "free" was dropped from these phrases deliberately. Reading the page
# is free; running the tool is billed against the wallet, and an
# indexable page is the wrong place to blur that. The same reasoning
# applies harder to ``_PREVIEW_TITLE_PHRASES`` below, which is what a
# search result shows.
#
# ``seo_long`` SELLS THE TASK; the adapter's ``blurb`` says what the
# form does. They render two paragraphs apart inside the same
# ``<div class="hero">`` (_form_hero.html), and the first draft of this
# map wrote both to the same brief without either knowing the other was
# adjacent, so ten of fourteen pages opened with the same sentence
# twice ("Upload your target, mark the residues you want gripped, and
# get back..." / "Upload your target, mark the residues you want the
# binder to touch, and get back..."). Each entry below therefore
# answers "why reach for THIS one" — the differentiator the blurb does
# not carry — rather than restating the mechanics one paragraph down.
_PREVIEW_SEO_PHRASES: dict[str, tuple[str, str]] = {
    "mpnn": (
        "no-install online sequence design tool",
        "The step between a designed shape and something you can "
        "actually order: give it any backbone, from any other tool, "
        "and it proposes the sequences most likely to fold into it"
    ),
    "af2": (
        "no-install online structure prediction tool",
        "The reference-standard fold, with the homolog search and "
        "templates that make it accurate on natural proteins, and a "
        "multimer mode that predicts several chains together rather "
        "than one at a time"
    ),
    "colabfold": (
        "fast no-install online structure prediction tool",
        "The quick pass while you are still iterating: no Colab "
        "notebook to babysit, no MMseqs2 round trip on your own "
        "laptop, and no queue to sit in between attempts"
    ),
    "esmfold": (
        "no-install online single-sequence structure prediction tool",
        "The one to reach for on a sequence you designed yourself: it "
        "reads the chain directly instead of hunting for natural "
        "relatives, which is exactly what a de novo binder does not "
        "have"
    ),
    "bindcraft": (
        "no-install online de novo binder design tool",
        "Design and in-silico filtering run in one pass, so what comes "
        "back is already a shortlist rather than raw output you have "
        "to triage yourself"
    ),
    "rfantibody": (
        "no-install online nanobody design tool",
        "Nanobodies are small enough to reach into a cleft a full "
        "antibody cannot and simple enough to express, and this designs "
        "them straight onto the patch you name with no RoseTTAFold or "
        "Rosetta to install"
    ),
    "rfdiffusion": (
        "no-install online de novo binder design tool",
        "The most widely used de novo binder generator, run end to "
        "end: every design it invents is re-folded with AlphaFold2 "
        "against your own target, so the confidence score you read is "
        "measured rather than the generator marking its own work"
    ),
    "boltzgen": (
        "no-install online multi-format binder design tool",
        "One model covers four binder formats against the same site, "
        "so you can weigh a mini-protein against a nanobody, an "
        "antibody or a peptide without changing tools — and it reads "
        "the sugars and modified residues on your target instead of "
        "ignoring them"
    ),
    "boltz2": (
        "no-install online cofold validation tool",
        "The cheap second opinion on a shortlist: it works from "
        "sequence alone, so a whole batch of designs from another tool "
        "can be re-checked for seconds each before any of them reach "
        "the bench"
    ),
    "pxdesign": (
        "no-install online AlphaFold2-scored binder design tool",
        "The same pipeline Ranomics runs for its own wet-lab "
        "projects: every candidate comes back already re-folded "
        "against your target, carrying its own confidence score for "
        "the contact rather than a number borrowed from the generator"
    ),
    "iggm": (
        "no-install online antibody and nanobody engineering tool",
        "One model for the antibody work that usually takes four "
        "separate ones — loop redesign, humanisation, affinity "
        "maturation and predicting the docked complex all run from the "
        "same upload"
    ),
    "esmfold2-design": (
        "no-install online antibody-fragment and minibinder design "
        "tool",
        "All six binding loops of the scFv are designed together in one "
        "pass against your target rather than one at a time, and the "
        "same run can make small de novo binders instead"
    ),
    "opendde": (
        "no-install online all-atom complex prediction tool",
        "DNA, RNA, cofactors and small molecules are first-class parts "
        "of the input rather than something to strip out before "
        "folding, so the complex is modelled as it actually exists"
    ),
    "proteina": (
        "no-install online hard-target binder design tool",
        # NOT "three independent scoring checks" — no variant runs all
        # three. tools/proteina/Dockerfile.modal:229-231: "Only
        # ligand_binder (RF3 is its sole reward) and motif_ame need it;
        # protein_binder scores on AF2 alone." See the note on
        # ``comparison_one_liner`` in tools/proteina/meta.py.
        #
        # ALSO THE ONLY IMPERATIVE OF THE FOURTEEN. It opened "Upload a
        # target the usual design tools struggle with", while the other
        # thirteen ledes are declaratives or noun phrases ("The
        # reference-standard fold…", "One model covers four binder
        # formats…"). Worse, proteina's own blurb two paragraphs up in the
        # same hero already opens "Upload a protein or small-molecule
        # target", so this was the last page where blurb and lede still
        # opened on the same verb — the stutter the rest of the rewrite
        # removed. Same content, declarative frame — and NOT "The one to
        # reach for", which is already how esmfold's lede opens.
        # AND NOT "or a small molecule rather than a protein". That clause
        # recruited visitors this form refuses: tools/proteina/__init__.py
        # ::_CUSTOM_TARGET_PRESETS is {"protein_binder"}, so a molecule of
        # your own has no path through validate().
        "Built for the protein targets the standard design tools stall on — a "
        "recessed pocket, or a site spanning two chains: every candidate the "
        "search generates is re-folded against your target before the search "
        "builds on it"
    ),
}

def _preview_seo_phrases(slug: str) -> tuple[str, str]:
    """Return (short, long) SEO phrase pair for a tool slug.

    Falls back to a generic pair so newly registered tools still get
    sensible copy without an explicit entry in the map.
    """
    return _PREVIEW_SEO_PHRASES.get(
        slug,
        (
            # No slug interpolation. This is rendered prose on an
            # indexable page; "a free esmfold2-design tool online" is
            # what the old f-string produced.
            "no-install online protein design tool",
            "Run it through your browser on a dedicated GPU with no "
            "install"
        ),
    )

# Title-only phrases. Kept separate from ``_PREVIEW_SEO_PHRASES`` so the
# body lede stays grammatical ("X is a <seo_phrase> you can run") while
# the <title> stays under the 65-char SERP cap.
#
# "Free" was dropped here for the same reason it was dropped from the
# ledes: running is billed against the wallet. A <title> is the most
# indexable string on the page and the one that shows in the search
# result, so the half-applied version had the word surviving in exactly
# the place it misleads most. mpnn was the only one of the fourteen
# carrying it.
_PREVIEW_TITLE_PHRASES: dict[str, str] = {
    "mpnn": "Sequence Design on a Backbone",
    "af2": "AF2 Multimer No-Install",
    "colabfold": "No Colab Required",
    "esmfold": "Single-Sequence Folding",
    "bindcraft": "De Novo Binder Design",
    "rfantibody": "Nanobody Design",
    "rfdiffusion": "De Novo Binder Design",
    "boltzgen": "Multi-Modal Binder Design",
    "boltz2": "Cofold Validation",
    "pxdesign": "AF2-IG Binder Design",
    "esmfold2-design": "scFv CDR Design",
    "iggm": "Antibody Design + Structure",
    # Added with the copy pass: these registered after the map was
    # written and were falling through to "GPU-Backed Protein Design",
    # which describes every tool on the hub and therefore none of them.
    # opendde's first draft read "Protein DNA RNA Ligand Co-Folding",
    # which rendered a 72-character <title> — over the 65-char SERP cap
    # this map exists to respect, and repeating "co-folding" from the
    # short name that precedes it.
    "opendde": "Protein, DNA, RNA, Ligand",
    "proteina": "Hard-Target Binder Design",
}

def _preview_title_phrase(slug: str) -> str:
    return _PREVIEW_TITLE_PHRASES.get(slug, "GPU-Backed Protein Design")

# Map tools-hub slug -> ranomics.com /technology/<slug> page slug.
# Used to emit a cross-site "Learn how X works" link on each public
# preview so the two co-owned sites reinforce each other for
# algorithm-name search intent.
_RANOMICS_TECHNOLOGY_SLUGS: dict[str, str] = {
    "mpnn": "proteinmpnn",
    "af2": "alphafold2",
    "colabfold": "colabfold",
    "esmfold": "esmfold",
    "rfdiffusion": "rfdiffusion",
    "rfantibody": "rfantibody",
    "bindcraft": "bindcraft",
    "boltzgen": "boltzgen",
    "pxdesign": "pxdesign",
}

# 2-3 related tools per slug, ordered by closest sibling first.
# Powers an internal-linking "Related tools" block on each preview
# page so a searcher comparing algorithms gets surfaced the next
# logical option from the same workflow stage.
_RELATED_TOOLS: dict[str, tuple[str, ...]] = {
    "rfdiffusion": ("bindcraft", "pxdesign", "boltzgen"),
    "bindcraft":   ("rfdiffusion", "boltzgen", "pxdesign"),
    "pxdesign":    ("rfdiffusion", "bindcraft", "boltzgen"),
    "boltzgen":    ("rfantibody", "iggm", "rfdiffusion"),
    "rfantibody":  ("boltzgen", "iggm", "rfdiffusion"),
    "iggm":        ("rfantibody", "boltzgen", "boltz2"),
    "mpnn":        ("af2", "colabfold", "esmfold"),
    "af2":         ("colabfold", "esmfold", "mpnn"),
    "colabfold":   ("af2", "esmfold", "mpnn"),
    "esmfold":     ("af2", "colabfold", "mpnn"),
    "boltz2":      ("af2", "colabfold", "boltzgen"),
}

# Tools whose form asks the user to name the residues a binder should
# engage. Every field on these forms is answerable by a bench biologist
# except this one: the helper text documents the SYNTAX (comma
# separated, original PDB numbering, prefix the chain) and never the
# METHOD — how to decide WHICH residues. Epitope Scout is the answer to
# that question and was linked from nowhere on these pages.
#
# Deliberately NOT folded into _RELATED_TOOLS: that block is headed "if
# you are picking between <tool> and a sibling algorithm", and Scout is
# not an alternative to RFdiffusion — it is the step before it. Mixing
# the two would file a prerequisite under a heading that denies it is
# one. Rendered as its own "start here first" panel instead.
#
# boltz2 also has a hotspot field and is excluded on purpose: its
# hotspots are scored after the fold, not steered toward, so choosing
# them well is not a precondition for the run being worth paying for.
_HOTSPOT_TOOLS: frozenset[str] = frozenset({
    "rfdiffusion", "bindcraft", "pxdesign", "rfantibody",
    "boltzgen", "proteina", "iggm",
})

def _prerequisite_tool(slug: str) -> dict | None:
    """Epitope Scout as a "run this first" step, or None.

    Copy is read from shared.tools_catalog rather than restated here —
    that hardcoded entry (Scout is not a registered adapter; it has no
    tools/<slug>/meta.py) is already the source of truth for the
    homepage tile and /tools, and a second hand-written tagline would
    drift from it.
    """
    if slug not in _HOTSPOT_TOOLS:
        return None
    from shared.tools_catalog import _HARDCODED_TOOLS  # noqa: PLC0415
    entry = next(
        (t for t in _HARDCODED_TOOLS if t["slug"] == "epitope-scout"), None
    )
    if entry is None:
        return None
    try:
        url = url_for(entry["endpoint"])
    except Exception:  # noqa: BLE001 — endpoint not registered in this app
        return None
    return {
        "name": entry["name"],
        "url": url,
        "tagline": entry["tagline"],
        "why": (
            "It scores your target's surface and ranks candidate epitopes, "
            "which is how you decide what to type into Hotspot residues. "
            "It is free."
        ),
    }

# Tools with a real, published run on /showcase. Rescued from the two
# per-tool preview overrides (templates/tools/{rfdiffusion,boltzgen}_preview.html)
# that the shared preview shell's removal took with it — that showcase
# link was the only content in either file. Phase 3 of the redesign
# replaces this with a per-tool worked example rendered through the
# tool's own results partial; until then, the link is what we have.
_SHOWCASE_NOTES: dict[str, dict[str, str]] = {
    "rfdiffusion": {
        "title": "A real RFdiffusion run",
        "body": (
            "The showcase has a real RFdiffusion run: 60 de novo binder "
            "backbones generated against a target across five parallel "
            "jobs, each fold shaped by diffusion rather than grafted onto "
            "a known scaffold. RFdiffusion returns backbone geometry, the "
            "starting fold you take into sequence design."
        ),
        "anchor": "03-rfdiffusion-platform-pilot",
        "cta": "See the RFdiffusion run in the showcase",
    },
    "boltzgen": {
        "title": "Real BoltzGen runs",
        "body": (
            "The showcase has two real BoltzGen runs with anonymized "
            "targets: a nanobody discovery project that narrowed 2000 "
            "designs to a validated panel of 12, scored by two independent "
            "structure predictors, and a de novo minibinder run that "
            "turned one interaction interface into 20,000 ranked designs "
            "with a top tier scoring ipTM 0.98."
        ),
        "anchor": "01-boltzgen-vhh-immune-coreceptor",
        "cta": "See real BoltzGen runs in the showcase",
    },
}

def _related_tool_cards(slug: str) -> list[dict]:
    """Build the related-tools card list for the preview page.

    Each card carries slug, short_name, one-line description, and
    the tool_form URL so the template stays declarative.
    """
    out: list[dict] = []
    for related_slug in _RELATED_TOOLS.get(slug, ()):
        related_adapter = tool_base.get(related_slug)
        if related_adapter is None or not tool_enabled(related_slug):
            continue
        blurb = related_adapter.blurb or ""
        one_liner = getattr(
            meta_for(related_slug), "comparison_one_liner", None,
        )
        if one_liner:
            blurb = one_liner
        out.append({
            "slug": related_slug,
            "short_name": _short_name_for_label(related_adapter.label),
            "blurb": blurb,
            "url": url_for("tools.tool_form", tool=related_slug),
        })
    return out

# (stored key, form field, label shown when the stored key is absent).
# iggm: tools/iggm/__init__.py:334-337 vs templates/tools/iggm_form.html:93,116,122.
_CLONE_RENAMES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "iggm": (
        ("antibody_fasta", "fasta", "Antibody FASTA"),
        ("antigen_chain", "target_chain", "Antigen chain"),
        ("epitope_pdb_resnums", "epitope", "Epitope residues"),
    ),
}

# OpenDDE spec entity key -> guided textarea (tools/opendde/__init__.py:193-234).
_OPENDDE_BOXES = {
    "proteinChain": "proteins",
    "dnaSequence": "dna",
    "rnaSequence": "rna",
    "ligand": "ligands",
}


def _upload_purged(stored_path: str) -> bool:
    """True only when storage confirms the staged input is gone.

    A job row keeps ``_pdb_storage_path`` after the object is deleted
    (cron/purge_old_storage.py:414 deletes objects and writes no job row).
    An unknown answer keeps the reuse offer; if the file is missing,
    download_input raises StorageError (shared/storage.py:146) and
    tool_submit marks the job failed and releases the hold (the
    ``except StorageError`` branch after staging).
    """
    return input_exists(stored_path) is False


def _form_takes_structure(adapter) -> bool:
    """Whether the tool's form renders the reuse-structure banner.

    proteina takes a structure but has requires_pdb=False
    (tools/proteina/__init__.py:824-868), so _needs_pdb cannot answer this.
    """
    env = current_app.jinja_env
    source, _, _ = env.loader.get_source(env, adapter.form_template)
    return "pdb_source_banner(" in source


def _clone_missing(adapter, prior_inputs: dict, pre_fill: dict, pdb_source) -> list[str]:
    """Labels of the cloned run's inputs the form could not refill."""
    missing = [
        label for stored, field, label in _CLONE_RENAMES.get(adapter.slug, ())
        if prior_inputs.get(stored) is None and not pre_fill.get(field)
    ]
    if adapter.slug == "opendde" and not prior_inputs.get("spec"):
        missing.append("Entities (proteins, DNA, RNA, ligands)")
    preset = next(
        (p for p in adapter.presets if p.slug == prior_inputs.get("preset")), None
    )
    if pdb_source is None and _needs_pdb(adapter, preset, prior_inputs):
        missing.append("Structure file (upload it again)")
    return missing


def _normalize_clone_pre_fill(slug: str, pre_fill: dict) -> None:
    """Map stored ``job.inputs`` keys onto the form's field names, in place.

    ``clone_from`` copies ``job.inputs`` straight into ``pre_fill`` and
    the form then looks each field up BY NAME. Where ``validate()``
    stored something under a different name, or nested, the lookup
    misses and the field silently falls back to its hard-coded default —
    so a user cloning a 400-design run to re-run it gets a different
    parameter set with no warning anywhere. That is a live bug
    independent of the redesign; it is fixed here, once, rather than in
    each form, because every clone for every tool passes through this
    one function.

    Four shapes, all measured against what ``validate()`` returns:

    * ANY list-valued input, handled last and generically by
      ``_as_form_text``: a stored list reaches Jinja unconverted and is
      rendered with ``repr``, so cloning a Boltz-2 run put
      ``[{'name': 'd0', 'sequence': ...}]`` into the FASTA textarea.
      ``hotspot_residues`` (a list on six adapters) falls out of the same
      rule rather than needing its own line, which is what it had.
    * ``binder_length`` is stored as ``{"min": .., "max": ..}`` by
      rfdiffusion and as ``[lo, hi]`` by proteina, while both forms ask
      for it as two number inputs. bindcraft and boltzgen store the two
      halves flat and are already fine; pxdesign stores a scalar under
      the same name its single field uses, so ``isinstance`` is what
      keeps this from corrupting it.
    * MPNN calls the field ``chains_to_design`` and stores it as
      ``target_chain``, so cloning an MPNN job silently reset the chain
      selection to "A".

    ``setdefault`` throughout: a key the form already reads under its
    own name always wins over a derived one.
    """
    bl = pre_fill.get("binder_length")
    if isinstance(bl, dict):
        lo, hi = bl.get("min"), bl.get("max")
    elif isinstance(bl, (list, tuple)) and len(bl) == 2:
        lo, hi = bl[0], bl[1]
    else:
        lo = hi = None
    if lo is not None:
        pre_fill.setdefault("binder_length_min", lo)
    if hi is not None:
        pre_fill.setdefault("binder_length_max", hi)

    if slug == "mpnn" and pre_fill.get("target_chain") is not None:
        pre_fill.setdefault("chains_to_design", pre_fill["target_chain"])

    # The batch folds store the parsed records under ``batch_records``
    # (tools/af2/__init__.py:347, tools/colabfold/__init__.py:335,
    # tools/esmfold/__init__.py:229) while all three textareas are named
    # ``sequences`` (templates/tools/af2_form.html:245,
    # colabfold_form.html:222, esmfold_form.html:221) and both parsers read
    # that name back (tools/colabfold/__init__.py:248,
    # tools/esmfold/__init__.py:169). Without this the box renders empty, so
    # cloning a batch fold silently dropped every record.
    if pre_fill.get("batch_records") is not None:
        pre_fill.setdefault("sequences", pre_fill["batch_records"])

    # Stored under another name than the field that reads it back.
    for stored, field, _label in _CLONE_RENAMES.get(slug, ()):
        if pre_fill.get(stored) is not None:
            pre_fill.setdefault(field, pre_fill[stored])
    # esmfold2-design stores the scFv framework as binder_name
    # (tools/esmfold2_design/__init__.py:166) and the minibinder preset stores
    # the literal "minibinder" there, which is no framework.
    if slug == "esmfold2-design" and pre_fill.get("is_antibody"):
        pre_fill.setdefault("binder_framework", pre_fill.get("binder_name"))
    # OpenDDE stores only the assembled spec (tools/opendde/__init__.py:454),
    # never the textareas it was built from
    # (templates/tools/opendde_form.html:144-167). Rebuild both modes from it.
    if slug == "opendde" and isinstance(pre_fill.get("spec"), list) and pre_fill["spec"]:
        pre_fill.setdefault("spec_json", json.dumps(pre_fill["spec"], indent=2))
        boxes: dict[str, list[str]] = {}
        for entity in pre_fill["spec"][0].get("sequences") or []:
            for key, box in _OPENDDE_BOXES.items():
                body = entity.get(key) if isinstance(entity, dict) else None
                if not isinstance(body, dict):
                    continue
                if key == "ligand":
                    boxes.setdefault(box, []).append(str(body.get("ligand", "")))
                else:
                    chain = (body.get("id") or [""])[0]
                    boxes.setdefault(box, []).append(f">{chain}\n{body.get('sequence', '')}")
        for box, lines in boxes.items():
            pre_fill.setdefault(box, "\n".join(lines))
        del pre_fill["spec"]

    # Every remaining list becomes text. LAST, so the shape-specific
    # rules above still see the structure they were written against.
    for key, value in pre_fill.items():
        if isinstance(value, (list, tuple)):
            pre_fill[key] = _as_form_text(value)


def _as_form_text(value) -> str:
    """Collapse a stored list into something a text control can render.

    ``pre_fill`` reaches the templates as-is and every field reads it by
    name, so anything that is not already a string is handed to Jinja and
    stringified with ``repr``. ``boltz2`` stores ``binder_sequences`` as
    ``[{"name": .., "sequence": ..}]`` and its textarea rendered exactly
    that — ``[{'name': 'd0', 'sequence': 'QVRL...'}]`` — into a box the
    user is asked to submit as FASTA. Cloning a Boltz-2 run therefore
    produced a form that could not be submitted at all.

    Fixed here rather than in boltz2_form.html because the defect is the
    shape, not the tool: ``hotspot_residues`` is a list on six adapters
    and was already being joined a few lines up, ``af2`` stores
    ``fasta_records`` in the same record shape and af2_form.html carries
    a hand-rolled Jinja loop to rebuild the FASTA. This is that logic,
    once, on the one function every clone for every tool passes through.

    Two shapes, both measured off the adapters' own ``validate()``:

    * a list of records carrying ``sequence`` -> FASTA, which is what the
      textareas ask for and what the parsers read back.
    * a list of scalars -> comma-separated, which is what the single-line
      fields (hotspots, epitopes, chains) ask for.

    Anything else is joined on commas too. That can still be wrong for a
    shape nobody has written yet, but it is never a ``repr`` in a form
    field, which is the failure this exists to stop.
    """
    items = list(value)
    if items and all(
        isinstance(i, dict) and i.get("sequence") for i in items
    ):
        lines = []
        for index, record in enumerate(items):
            name = record.get("name") or record.get("header") or f"seq_{index}"
            lines.append(f">{name}")
            lines.append(str(record["sequence"]))
        return "\n".join(lines)
    return ",".join(str(i) for i in items)


# Hotspot residues are the first hard stop a bench biologist hits: they
# know their target, they do not know which residues on it to name, and
# nothing on the pilot card said where to get them — the Epitope Scout
# deflection lived only in the form field's help text, further down the
# page than someone starting a pilot is looking.
#
# THE CARD HAS TWO SEPARABLE JOBS and they need two different predicates.
# Conflating them shipped both a false promise and a missing one:
#
#   (a) "this tool requires hotspots" -> shared.tool_chooser.needs_hotspots,
#       which reads TOOL_RULES[slug].hotspots_required — the flag
#       shared/pdb_preflight.py::preflight_for_tool branches on to refuse a
#       submit.
#   (b) "and Scout can hand them back" -> scout.handoff.VALID_HANDOFF_TOOLS.
#
# Two earlier cuts of (a) both got it wrong from a proxy. The first read
# which adapters' ``validate()`` refuses an empty hotspot field: that put
# rfdiffusion in, which at the time was not a Scout handoff target, so the
# card promised a round trip that dead-ended. (rfdiffusion has since been
# ADDED to VALID_HANDOFF_TOOLS, so the round trip is real now — the
# objection was never to the answer, it was to reading (b) off (a).)
# The second read the tool's own
# ``about["prerequisites"]`` bullets, matching "hotspot" without "option":
# that made a copy edit silently change a user-facing claim, and it put
# boltzgen in the REQUIRED set purely because its bullet was worded as a
# requirement while nothing enforced one.
#
# So (a) now reads the gate itself, and the card is raised for a tool that
# either requires hotspots OR can receive them from Scout — boltzgen is the
# second kind, and its Scout link no longer depends on how its bullet is
# phrased.


def _pilot_context(adapter, meta) -> dict | None:
    """The guided starter recipe for a tool, with its links derived.

    ``tools/<slug>/meta.py`` declares only the WORDS and the parameter
    set (``PILOT``). ``PILOT["params"]`` keys are FORM FIELD NAMES, so
    the same dict pre-fills the form (``?pilot=1``).

    Returns None when the tool declares ``PILOT = None`` (the fast
    predictors: a 40-second ESMFold run needs no pilot ceremony).
    """
    pilot = getattr(meta, "PILOT", None)
    if not pilot:
        return None
    return dict(
        pilot,
        params=dict(pilot.get("params") or {}),
        url=url_for("tools.tool_form", tool=adapter.slug, pilot=1),
        # Rendered by components/pilot_card.html as the "this tool asks
        # for these, and here is where to get them" line. Both flags are
        # derived, so one macro edit reaches every tool and neither can be
        # hand-edited into disagreeing with the surface it describes.
        hotspot_help_url=(
            url_for("scout.index")
            if (
                tool_chooser.needs_hotspots(adapter.slug)
                or adapter.slug in VALID_HANDOFF_TOOLS
            )
            else None
        ),
        # Whether ANYTHING refuses a submit that names none, not whether
        # the tool can use one. boltzgen is offered the deflection because
        # Scout can hand residues back to it, and runs unsteered without
        # them, so its paragraph must not say it needs one. iggm is the
        # other direction: its own validate refuses, which the preflight
        # flag alone does not see — tool_chooser.refuses_without_residues.
        hotspot_required=tool_chooser.refuses_without_residues(adapter.slug),
        # Only these tools can actually receive the residues back. Scout's
        # picker offers exactly this set; on anything else the user has to
        # copy the numbers across by hand, and the copy says so.
        hotspot_handoff=adapter.slug in VALID_HANDOFF_TOOLS,
    )


@lru_cache(maxsize=None)
def _example_result(slug: str) -> dict | None:
    """``tools/<slug>/example/result.json``, or None.

    A real ``job.result`` captured once from a completed run. Cached
    for the process lifetime because the file is static and this is
    read on every render of a public, crawlable page.
    """
    path = (
        Path(__file__).resolve().parent.parent
        / "tools" / slug.replace("-", "_") / "example" / "result.json"
    )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _example_teaser(example: dict) -> str:
    """The one-line trailer for the worked example, for the form hero.

    ``components/worked_example.html`` renders in the right rail below
    the form, so this line is what a visitor sees of it before deciding
    to scroll. Every word is taken from ``EXAMPLE``: the first sentence
    of ``target``. The example's recorded ``runtime`` and ``cost_usd``
    are left out: the tool page shows no price or time before a run.
    ``tests/test_worked_examples.py::TestTheTeaserComesFromTheExample``
    fails on a digit in the output that is absent from ``target``.

    ``Markup.striptags`` rather than splitting the raw string because
    ``target`` carries both markup and entities: it unescapes as it
    strips, so ``templates/tools/_form_hero.html`` renders the result
    WITHOUT ``|safe`` and an ``&mdash;`` still reaches the page as one
    character instead of six. Splitting plain text also means the cut
    cannot land inside a tag.

    Every field is read with ``.get``, matching the rest of
    ``_example_context``, which fails soft when the payload is missing
    rather than raising. This runs inside the tool-page render, so a
    ``KeyError`` here would 500 the form for both auth states, not just
    blank the rail panel.
    ``tests/test_worked_examples.py
    ::TestTheTeaserComesFromTheExample::test_a_targetless_example_does_not_500``
    renders a tool whose ``EXAMPLE`` has no ``target``.
    """
    lead, sentence_end, _rest = (
        Markup(example.get("target") or "").striptags().partition(". ")
    )
    if sentence_end:
        lead += "."
    return lead


# Runtime keys dropped from a tools/*/example/result.json before the results
# partial reads it, because the tool page shows no time before a run
# (tests/test_no_prerun_numbers.py).
_EXAMPLE_RUNTIME_KEYS = frozenset({
    "runtime_seconds", "runtime_minutes", "gpu_seconds", "wall_clock_seconds",
})


def _example_context(adapter, meta) -> dict | None:
    """The worked example for a tool: narration + the real payload.

    Returns None unless BOTH halves are present. Narration with no
    payload would render a description of results above an empty
    results panel, and a payload with no narration is an unlabelled
    table of numbers — either is worse than the tool simply not having
    an example yet.
    """
    example = getattr(meta, "EXAMPLE", None)
    if not example:
        return None
    result = _example_result(adapter.slug)
    if result is None:
        logger.warning(
            "EXAMPLE declared for %s but example/result.json is missing "
            "or unreadable; rendering no worked example", adapter.slug,
        )
        return None
    result = {k: v for k, v in result.items() if k not in _EXAMPLE_RUNTIME_KEYS}
    return dict(example, result=result, teaser=_example_teaser(example))

def _showcase_note(slug: str) -> dict | None:
    """The showcase card for a tool, with its URL resolved, or None."""
    note = _SHOWCASE_NOTES.get(slug)
    if not note:
        return None
    return dict(
        note,
        url=f"{url_for('public.showcase')}#{note['anchor']}",
    )

def _public_tool_context(adapter) -> dict:
    """SEO + explainer context the tool page needs in BOTH auth states.

    Memoised on ``flask.g`` — i.e. for the life of ONE request, and no
    longer. Two callers build this per render: ``tool_form`` passes it
    into the template, and the ``tool_public_context`` jinja global
    (app.py) rebuilds it inside ``about_panel.html`` because macros are
    imported without context.

    ``flask.g`` rather than ``lru_cache`` deliberately: the dict holds
    ``url_for(..., _external=True)`` breadcrumbs, so a process-lifetime
    cache would pin the first request's host into every later response.
    """
    cache = g.setdefault("_public_tool_ctx", {})
    hit = cache.get(adapter.slug)
    if hit is None:
        hit = cache[adapter.slug] = _build_public_tool_context(adapter)
    return hit


def _build_public_tool_context(adapter) -> dict:
    """Assemble the context. Call ``_public_tool_context`` instead."""
    tool_meta = meta_for(adapter.slug)

    short_name = _short_name_for_label(adapter.label)
    seo_phrase, seo_long = _preview_seo_phrases(adapter.slug)
    tech_slug = _RANOMICS_TECHNOLOGY_SLUGS.get(adapter.slug)

    # SoftwareApplication + FAQPage JSON-LD, built here rather than in
    # Jinja so base.html only has to dump it. Moved off the deleted
    # tools/_preview.html; /tools/<slug> is the indexable URL now.
    desc_src = (
        getattr(tool_meta, "comparison_one_liner", None)
        or adapter.blurb
        or ""
    )
    software: dict = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": short_name,
        "applicationCategory": "ScientificApplication",
        "operatingSystem": "Any (web-based)",
    }
    if desc_src:
        software["description"] = str(desc_src)[:250]
    if getattr(tool_meta, "paper_url", None):
        software["citation"] = tool_meta.paper_url
    if getattr(tool_meta, "github_url", None):
        software["codeRepository"] = tool_meta.github_url

    faq_items = getattr(tool_meta, "seo_faq", None) or ()
    faq_ld = None
    if faq_items:
        faq_ld = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": item["q"],
                    "acceptedAnswer": {
                        "@type": "Answer", "text": item["a"],
                    },
                }
                for item in faq_items
            ],
        }

    return {
        "meta": tool_meta,
        "seo_phrase": seo_phrase,
        "seo_long": seo_long,
        "title_phrase": _preview_title_phrase(adapter.slug),
        "short_name": short_name,
        "learn_more_url": (
            f"https://www.ranomics.com/technology/{tech_slug}"
            if tech_slug else None
        ),
        "related_tools": _related_tool_cards(adapter.slug),
        "pilot": _pilot_context(adapter, tool_meta),
        "example": _example_context(adapter, tool_meta),
        "prerequisite": _prerequisite_tool(adapter.slug),
        "showcase_note": _showcase_note(adapter.slug),
        "breadcrumbs": [
            {"name": "Home", "url": url_for("public.index", _external=True)},
            {"name": "Tools", "url": url_for(
                "tools.tools_comparison", _external=True
            )},
            {"name": short_name, "url": url_for(
                "tools.tool_form", tool=adapter.slug, _external=True
            )},
        ],
        "tool_seo": {"software": software, "faq": faq_ld},
    }

@tools_bp.route("/tools/proteinmpnn", methods=["GET"])
def proteinmpnn_slug_redirect():
    """301 the guessed URL to the catalog slug.

    The adapter's slug is ``mpnn``, so the real page is /tools/mpnn, but
    the tool is called ProteinMPNN everywhere else (including this
    module's own SEO phrase map at ``"mpnn": "proteinmpnn"``) and
    /tools/proteinmpnn is what a reader types. It used to 404.

    A static rule outranks the ``/tools/<tool>`` converter rule in
    Werkzeug's map, so this is reached rather than tool_form. The query
    string is carried across because tool_form pre-fills from it; grep
    ``request.args`` inside tool_form below for the seven parameters it
    reads (pilot, workspace_id, target_pdb_id, clone_from, from_job,
    handoff, resample_from). Nothing links here with those today, so
    this costs one line and keeps a future link that does from being
    silently truncated.
    """
    target = url_for("tools.tool_form", tool="mpnn")
    # QUERY_STRING arrives as raw bytes and decoding it strictly raises
    # UnicodeDecodeError on a malformed one, which would 500 instead of
    # redirecting, so substitute U+FFFD. Pinned by
    # test_malformed_query_string_still_redirects in
    # tests/test_tool_slug_redirects.py.
    qs = request.query_string.decode("utf-8", errors="replace")
    if qs:
        target = f"{target}?{qs}"
    return redirect(target, code=301)

@tools_bp.route("/tools/<tool>", methods=["GET"])
def tool_form(tool: str):
    """Render a GPU tool's submission form. Public GET; Submit is gated.

    Anonymous visitors get the SAME form template and the same URL as
    signed-in ones — they can read every field, see the live cost
    estimate, and read the explainer, but the Submit button is
    replaced by a sign-in CTA. The spend gate is unchanged and lives
    where it always did: POST /tools/<slug>/submit and
    /tools/<slug>/preflight are both @login_required, so a logged-out
    visitor still cannot spawn a job.

    Logged-in pre-fill sources (query params, owner-scoped):
      * ``clone_from=<job_id>`` — reuse all inputs of an earlier job.
        Same-tool only (exact parameter fidelity).
      * ``from_job=<job_id>`` — Phase 4 cross-tool handoff. Copies
        only the target fields (target PDB reuse token, target_chain,
        hotspot_residues) and defaults preset='pilot'. Works across
        tools so a user can refine RFantibody output with BindCraft,
        validate BoltzGen output with PXDesign, etc.
      * ``handoff=<handoff_id>`` — target PDB + chain + hotspots from
        Epitope Scout via ``public.scout_handoffs``.
      * ``workspace_id=<ws_id>&target_pdb_id=<storage_path>`` —
        Workspace-funded run. The detail page at
        /workspaces/<id> emits these together so the POST gate
        (``workspace_preflight``) can verify the run is funded by
        an active Workspace and bill the actual Modal cost back.
    """
    adapter, err = _require_tool(tool)
    if err:
        return err

    public_ctx = _public_tool_context(adapter)

    # NOTE: there is no ``login_next`` here any more. It was computed on
    # both render branches and passed into the template, but no template
    # ever read it — the sign-in link lives in
    # components/_signin_cta.html and gets its href from the
    # ``signin_url()`` jinja global (app.py), because that file is
    # reached through macros imported WITHOUT context and cannot see a
    # context variable. A dead duplicate of a live helper is worse than
    # nothing: the QC pass that found the dropped query string fixed it
    # here, where it changes no behaviour at all. The real fix is in
    # ``_signin_url``.

    # Fifth pre-fill source, and the only one that works logged OUT:
    # ``?pilot=1`` loads the tool's declared starter recipe. It is a
    # constant from meta.py rather than anything owner-scoped, so
    # unlike clone_from / from_job / handoff / resample_from it needs
    # no user context and is resolved before the anonymous branch.
    pilot_pre_fill: dict = {}
    if request.args.get("pilot") and public_ctx.get("pilot"):
        pilot_pre_fill = dict(public_ctx["pilot"]["params"])

    # Logged-out: same template, same URL, no user context loaded.
    # ``authenticated=False`` is what flips the shared macros —
    # submit_cta renders a sign-in link instead of a submit button,
    # about_panel renders the full public explainer, and the wallet
    # panel drops the balance rows (balance_usd is None).
    if not session.get("user_email"):
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error=None,
            pre_fill=pilot_pre_fill,
            pdb_source=None,
            workspace_ctx=None,
            wallet=None,
            single_container_ceiling=None,
            authenticated=False,
            # SEO title/description only on the anonymous render —
            # crawlers are always anonymous, and the signed-in page
            # keeps the plain "<Tool> — Ranomics Tools" title it has
            # always had.
            page_title=(
                f"{public_ctx['short_name']} Online | "
                f"{public_ctx['title_phrase']} | Ranomics"
            ),
            page_description=(
                f"Run {public_ctx['short_name']} online on a dedicated "
                f"GPU through your browser. No install, no local CUDA. "
                f"{public_ctx['seo_long']}."
            ),
            **public_ctx,
        )

    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    # Campaign-only tools have no single-job atomic form — every run is a
    # fund-and-drain campaign — so send a logged-in visitor straight to the
    # campaign create flow. The logged-out preview above stays indexable; this
    # also avoids rendering a form_template these tools do not ship. The set is
    # currently empty (proteina left it when it gained a form template); the
    # guard stays for the next tool that needs it.
    from shared import compute_campaigns as _cc  # noqa: PLC0415
    if tool in _cc.CAMPAIGN_ONLY_TOOLS:
        return redirect(url_for("campaigns.compute_campaign_new"))

    # Workspace context (Wave-2 launch). Forwarded as hidden form
    # inputs by templates/tools/_prefill.html::workspace_hidden_inputs
    # so the POST handler can re-read and gate.
    workspace_ctx: dict | None = None
    ws_id_q = (request.args.get("workspace_id") or "").strip()
    ws_target_q = (request.args.get("target_pdb_id") or "").strip()
    if ws_id_q and ws_target_q:
        workspace_ctx = {
            "workspace_id": ws_id_q,
            "target_pdb_id": ws_target_q,
        }

    pre_fill: dict = {}
    pdb_source = None  # dict describing a reusable PDB, or None
    clone_missing: list[str] = []

    clone_from = request.args.get("clone_from", "").strip()
    if clone_from:
        prior = get_job(clone_from, user_id=ctx.user_id)
        if prior is not None and prior.tool == adapter.slug:
            pre_fill = {
                k: v for k, v in (prior.inputs or {}).items()
                if not k.startswith("_")
            }
            _normalize_clone_pre_fill(adapter.slug, pre_fill)
            stored_path = (prior.inputs or {}).get("_pdb_storage_path")
            stored_name = (prior.inputs or {}).get("_pdb_filename")
            if stored_path and stored_name and not _upload_purged(stored_path):
                pdb_source = {
                    "label": f"PDB from job {prior.id[:8]} ({stored_name})",
                    "filename": stored_name,
                    "token": f"job:{prior.id}",
                }
            clone_missing = _clone_missing(
                adapter, prior.inputs or {}, pre_fill, pdb_source
            )
            from shared.scale_up import SCALE_UP  # noqa: PLC0415
            scale_to = request.args.get("scale_to", "").strip()
            if scale_to.isdigit() and adapter.slug in SCALE_UP:
                key, max_count = SCALE_UP[adapter.slug]
                pre_fill[key] = str(max(1, min(int(scale_to), max_count)))

    from_job = request.args.get("from_job", "").strip()
    if from_job and not pre_fill:
        # Cross-tool handoff: copy only target fields, default to pilot.
        # Unlike clone_from this works across tools — the binder /
        # parameter shape differs, but target_pdb + target_chain +
        # hotspots are shared across BindCraft / RFantibody /
        # BoltzGen / PXDesign.
        src = get_job(from_job, user_id=ctx.user_id)
        if src is not None:
            src_inputs = src.inputs or {}
            for key in ("target_chain", "hotspot_residues"):
                val = src_inputs.get(key)
                if val is None:
                    continue
                if isinstance(val, list):
                    val = ",".join(str(x) for x in val)
                pre_fill[key] = val
            pre_fill["preset"] = "pilot"
            stored_path = src_inputs.get("_pdb_storage_path")
            stored_name = src_inputs.get("_pdb_filename")
            if stored_path and stored_name and _upload_purged(stored_path):
                if _form_takes_structure(adapter):
                    clone_missing = ["Structure file (upload it again)"]
            elif stored_path and stored_name:
                pdb_source = {
                    "label": (
                        f"Target PDB from {src.tool} job {src.id[:8]} "
                        f"({stored_name})"
                    ),
                    "filename": stored_name,
                    "token": f"job:{src.id}",
                }

    handoff_id = request.args.get("handoff", "").strip()
    if handoff_id:
        ho = get_handoff(handoff_id, user_id=ctx.user_id)
        if ho is not None:
            pre_fill.setdefault("target_chain", ho.target_chain)
            # iggm's form names the same field ``epitope`` (its
            # ``name="epitope"`` input); every other handoff target names it
            # ``hotspot_residues``. Writing the wrong key drops the residues
            # silently — the form renders, the numbers are simply not in it.
            hotspot_field = "epitope" if adapter.slug == "iggm" else "hotspot_residues"
            pre_fill.setdefault(
                hotspot_field,
                ",".join(str(r) for r in ho.hotspot_residues),
            )
            # Only when the tool HAS a pilot preset. templates/tools/
            # _prefill.html::pre_checked falls back to checking the first
            # radio ONLY when no preset is pre-filled, so pre-filling a
            # slug that no option carries leaves the whole group unchecked
            # and the form posts no preset at all. iggm's presets are
            # complex_prediction / affinity_maturation / design.
            if adapter.preset_for("pilot") is not None:
                pre_fill["preset"] = "pilot"
            pdb_source = {
                "label": (
                    f"Target PDB from Epitope Scout ({ho.pdb_filename})"
                    if ho.scout_job_id
                    else f"Target PDB from target prep ({ho.pdb_filename})"
                ),
                "filename": ho.pdb_filename,
                "token": f"handoff:{ho.id}",
            }
            # The far end of the Scout funnel. scout_handoff_created fires
            # when Scout stages the target; this fires only when the row
            # resolves onto a form, so a handoff that is created and never
            # arrives (expired Scout run, wrong account) is visible as a
            # gap between the two counts.
            from shared.events import log_event  # noqa: PLC0415
            log_event(
                event_type="scout_handoff_opened",
                user_id=ctx.user_id,
                session_id=session.get("anon_session_id"),
                path=request.full_path,
                props={
                    "tool": adapter.slug,
                    "hotspot_count": len(ho.hotspot_residues),
                    "from_scout": bool(ho.scout_job_id),
                },
            )

    # AF2-resample chain: when the user lands on the MPNN form via a
    # "Resample with MPNN" button on an AF2 / ColabFold / ESMFold
    # result page, prefill the MPNN form with the source job's
    # predicted PDB and sensible diversification defaults
    # (sampling_temp=0.5, num_seq_per_target=16). The PDB itself is
    # not staged here — that happens at submit time when the
    # ``resample:<job_id>`` token is resolved (the submit-side
    # branch decodes pdb_b64 from the source job's result and
    # uploads it like a fresh PDB).
    resample_from = request.args.get("resample_from", "").strip()
    if (
        resample_from
        and adapter.slug == _resample.RESAMPLE_DESTINATION
        and not pre_fill
    ):
        src = get_job(resample_from, user_id=ctx.user_id)
        if (
            src is not None
            and _resample.can_resample(src.tool)
            and src.status == "succeeded"
            and ((src.result or {}).get("pdb_b64") or "").strip()
        ):
            for k, v in _resample.RESAMPLE_MPNN_DEFAULTS.items():
                pre_fill[k] = v
            pdb_source = {
                "label": (
                    f"Predicted PDB from {src.tool} job {src.id[:8]}"
                ),
                "filename": (
                    f"predicted-{src.tool}-{src.id[:8]}.pdb"
                ),
                "token": f"resample:{src.id}",
            }
            from shared.events import EVENTS, emit  # noqa: PLC0415
            emit(
                EVENTS.RESAMPLE_LOADED,
                user_id=ctx.user_id,
                properties={
                    "source_tool": src.tool,
                    "source_job_id": src.id,
                },
            )

    # Pilot recipe, applied LAST and only as a fallback: a job-derived
    # source (clone/handoff/resample) names a real earlier run and must
    # win over a generic starter recipe if both query params are
    # somehow present.
    if pilot_pre_fill and not pre_fill:
        pre_fill = dict(pilot_pre_fill)

    # The wallet estimate partial reads balance_usd for first paint
    # so the form lights up with the user's real balance even before
    # the /api/wallet/estimate call returns. Falls back to 0 if the
    # service client is misconfigured.
    wallet_for_form = get_or_create_wallet(ctx.user_id) or {}

    from shared import compute_campaigns as cc  # noqa: PLC0415
    # D1: single-container design ceiling. When a campaign-supported tool's
    # requested design count exceeds this, the form re-points the submit to
    # the campaign chunker (client-side) and the tool_submit backstop rejects
    # a doomed single job. None for tools without a campaign path.
    campaign_ceiling = (
        cc.single_container_ceiling(adapter.slug)
        if adapter.slug in cc.SUPPORTED_TOOLS
        else None
    )
    return render_template(
        adapter.form_template,
        adapter=adapter,
        error=None,
        pre_fill=pre_fill,
        pdb_source=pdb_source,
        clone_missing=clone_missing,
        workspace_ctx=workspace_ctx,
        wallet=wallet_for_form,
        single_container_ceiling=campaign_ceiling,
        authenticated=True,
        **public_ctx,
    )

@tools_bp.route("/tools/<tool>/preflight", methods=["POST"])
@login_required
def tool_preflight(tool: str):
    """Run the per-tool PDB preflight and return a JSON verdict.

    Fired by ``static/js/preflight.js`` when the user attaches a PDB
    (or clicks "Use AlphaFold model instead"). No wallet hold, no
    job row, no Modal call — this is purely a "would this work?"
    check. The same logic re-runs at submit time as the actual gate.

    Accepts EITHER:
      - ``target_pdb`` file upload (multipart) + form fields, OR
      - ``alphafold_accession`` form field with a UniProt id like
        ``P25779`` (we fetch the AF model and run preflight on it).

    Returns JSON; see ``_verdict_to_json`` for the shape.
    """
    adapter, err = _require_tool(tool)
    if err:
        return ({"error": "Unknown tool"}, 404)
    if adapter.slug not in PREFLIGHT_TOOLS:
        return ({
            "kind": "ready", "ok": True,
            "tool_slug": adapter.slug,
            "cleanup_items": [], "hotspots": {"surviving": [], "dropped": []},
            "alphafold": None,
        }, 200)

    target_chain = (request.form.get("target_chain") or "A").strip()
    raw_hotspots = (request.form.get("hotspot_residues") or "").strip()
    # THE PANEL PREVIEWS THE GATE. IT IS NOT THE GATE. Read the rule below
    # before making it stricter — three attempts have now broken it.
    #
    # `static/js/preflight.js` does `setSubmitEnabled(!!v.ok)`, so a NEEDS_FIX
    # here does not merely colour a box: it disables the Run button, and the
    # only re-enable path is the network-error catch. The two failure
    # directions are therefore not symmetric.
    #
    #   panel green, gate refuses -> the user clicks Run and gets the
    #                                adapter's own message. Mildly annoying.
    #   panel red,   gate accepts -> the user cannot submit AT ALL, and is
    #                                told why by a sentence the panel guessed.
    #
    # So this parse is deliberately TOLERANT and never returns a verdict of
    # its own. Trying to make it authoritative is what produced, in order: a
    # panel that green-lit fields submit refused (int()-only, the original
    # bug), then one that hard-blocked proteina's own documented multi-chain
    # flow, then one that rendered READY for rfantibody hotspots its validate()
    # rejects. Each fix moved the divergence rather than removing it, because
    # the panel is re-implementing seven adapters' parsers from the outside.
    # Closing it properly means the panel asking the adapter — a
    # `hotspot_preview(form)` hook, or posting the whole form so validate()
    # can run — and that is a change to every adapter, not to this function.

    # The chain set, resolved BEFORE the hotspot branch. It feeds cleanup, the
    # size envelope, the gap analysis and two user-facing sentences, so
    # computing it only when the hotspot box happens to be non-empty made the
    # verdict and its wording change with an unrelated field.
    #
    # proteina REPLACES target_chain with the contig's chains
    # (tools/proteina/__init__.py::validate) rather than adding to them, and its
    # form tells the user to leave target_chain at "A" and name the chains in
    # the contig. Reading target_chain alone called "C73" a hotspot on an
    # untargeted chain for the exact input the template prints as its example;
    # unioning instead of replacing then put the untyped "A" into the set, and
    # that string is user-visible — it reaches "Target chain 'A,H,L' isn't in
    # this PDB. Found chain(s): H, L.", a sentence that contradicts itself.
    _contig_chains: list = []
    for _seg in (preflight_target_segments(request.form) or []):
        # Segments are (chain, lo, hi) tuples; see pdb_intake.
        _seg_chain = _seg[0] if isinstance(_seg, (tuple, list)) else None
        if _seg_chain and _seg_chain not in _contig_chains:
            _contig_chains.append(_seg_chain)
    if _contig_chains:
        target_chain = " ".join(_contig_chains)
    _chains = list(tool_base.parse_target_chains(target_chain))

    hotspots: list = []
    if raw_hotspots:
        # Tokenize the way the adapters do —
        # tools/base.py::parse_hotspot_residues and proteina's _parse_hotspots
        # both fold separators into whitespace and split on it. Splitting on
        # commas alone made "A54 B56" one unparseable token, so the panel saw
        # zero hotspots, said "This tool needs at least one hotspot residue",
        # and disabled Run for a field the gate accepts. That direction is the
        # one the rule above forbids, and 8644c74 is what created it: before
        # that commit the gate refused the same input, so panel and gate
        # agreed by both being wrong.
        for _tok in raw_hotspots.replace(";", ",").replace(",", " ").split():
            _cid, _resnum = split_hotspot(_tok, _chains)
            if _resnum is None:
                # Skipped, not fatal — the adapter refuses it on submit, with
                # wording this function has no business inventing.
                continue
            hotspots.append(_resnum if _cid is None else f"{_cid}{_resnum}")

    # Source the bytes: file upload OR AlphaFold fetch.
    af_accession = (request.form.get("alphafold_accession") or "").strip()
    uploaded = request.files.get("target_pdb")
    pdb_bytes: Optional[bytes] = None
    source_label: str = ""
    needs_cif_convert = False

    if af_accession:
        fetched = _fetch_alphafold_bytes(af_accession)
        if fetched is None:
            return ({
                "kind": "needs_fix", "ok": False,
                "tool_slug": adapter.slug,
                "reason": (
                    f"Couldn't fetch AlphaFold model for {af_accession}. "
                    f"The AlphaFold-DB may not have this UniProt entry."
                ),
                "suggested_fix": (
                    "Pick a different target or upload a cleaned PDB manually."
                ),
                "cleanup_items": [],
                "hotspots": {"surviving": [], "dropped": []},
                "alphafold": None,
            }, 200)
        pdb_bytes = fetched
        source_label = f"AF-{af_accession}"
    elif uploaded and uploaded.filename:
        pdb_bytes = uploaded.read()
        source_label = uploaded.filename
        # DO NOT CONVERT HERE. The conversion now happens below the
        # inspection gate, and that order is the entire defect this
        # comment replaces.
        #
        # inspect_pdb_bytes picks its parser off the FILENAME EXTENSION
        # (shared/pdb_inspect.py). Converting first left source_label as
        # the user's own `.cif` name while pdb_bytes had already become
        # PDB text, so MMCIFParser was handed PDB and raised on the first
        # token. EVERY cif upload came back "Could not parse the uploaded
        # file as mmCIF. Error: ValueError." on all SEVEN tools in
        # PREFLIGHT_TOOLS. Eight forms mount this panel; iggm is not in
        # that set, so the route returns READY above without ever reading
        # the upload and its panel inspects nothing at all.
        #
        # Meanwhile tool_submit inspects raw and converts after, so it
        # accepted the same bytes and ran the job: the panel refused
        # targets the gate was happy to run.
        #
        # tool_submit (below) and pdb_intake.resolve_target_upload are the
        # two existing implementations of the correct order; this is the
        # third. convert_cif_to_pdb_bytes' own docstring states that it
        # runs "after inspect_pdb_bytes has already validated".
        needs_cif_convert = source_label.lower().endswith(
            (".cif", ".mmcif")
        )
    else:
        return ({
            "kind": "needs_fix", "ok": False,
            "tool_slug": adapter.slug,
            "reason": "No PDB uploaded.",
            "suggested_fix": "Attach a target PDB above.",
            "cleanup_items": [],
            "hotspots": {"surviving": [], "dropped": []},
            "alphafold": None,
        }, 200)

    # Cheap inspection first — catches "this isn't a PDB at all". Runs on
    # the bytes AS UPLOADED under the name they arrived with, so the parser
    # this picks always matches the content it is handed.
    inspection = inspect_pdb_bytes(pdb_bytes, filename=source_label)
    if not inspection.ok:
        return ({
            "kind": "needs_fix", "ok": False,
            "tool_slug": adapter.slug,
            "reason": inspection.error or "Couldn't parse upload as PDB.",
            "suggested_fix": (
                "Confirm the file is a PDB or mmCIF protein structure."
            ),
            "cleanup_items": [],
            "hotspots": {"surviving": [], "dropped": []},
            "alphafold": None,
        }, 200)

    # CONVERT ONLY NOW, AND ONLY BEFORE preflight_for_tool. That function
    # has no CIF handling whatsoever: it scans raw ATOM/HETATM columns and
    # says so in its own docstring.
    #
    # MEASURED, and the obvious guess is wrong in both directions --
    # nothing raises out of the route, and nothing passes cleanly either.
    # Handed mmCIF the gate refuses with a sentence that blames the user's
    # file for our own mistake: six of the seven report "Couldn't
    # pre-flight your upload (IndexError). The file may be malformed." and
    # boltz2 reports "Antigen chain 'A' has no protein residues in this
    # PDB." What every one of them has in common is a residue count of 0,
    # which is why the test pins the COUNT and not the verdict.
    # Conversion belongs between the inspection above and the
    # gate below, and nowhere else.
    if needs_cif_convert:
        try:
            pdb_bytes = convert_cif_to_pdb_bytes(pdb_bytes, source_label)
        except CifConversionError as exc:
            return ({
                "kind": "needs_fix", "ok": False,
                "tool_slug": adapter.slug,
                "reason": str(exc),
                "suggested_fix": (
                    "Save the structure as PDB format and re-upload."
                ),
                "cleanup_items": [],
                "hotspots": {"surviving": [], "dropped": []},
                "alphafold": None,
            }, 200)

    binder_max_aa, num_designs = _parse_preflight_size_params(request.form)
    # The form total is not what one container runs. Above
    # single_container_ceiling the form re-points at the campaign chunker
    # (templates/tools/_campaign_reroute.html) and _single_container_refusal
    # rejects a larger single job, so no container ever runs more than the
    # ceiling -- and the campaign route judges the ceiling PER CHUNK, passing
    # plan.designs_for_chunk(0) (blueprints/campaigns.py). Judging the form
    # total here refused runs that route funds: bindcraft at 115 aa / 100
    # designs is 15.6 h as one job and greys out Run, while the six-design
    # chunk it actually becomes is accepted. Clamped so the panel judges the
    # container the submit will create.
    if num_designs is not None:
        from shared import compute_campaigns as cc  # noqa: PLC0415

        if adapter.slug in cc.SUPPORTED_TOOLS:
            num_designs = min(num_designs, cc.single_container_ceiling(adapter.slug))
    verdict = preflight_for_tool(
        adapter.slug, pdb_bytes,
        target_chain=target_chain, hotspots=hotspots,
        binder_max_aa=binder_max_aa, num_designs=num_designs,
        offer_alphafold=request.form.get("no_alphafold") != "1",
        # Sizes the region the user typed, so the panel and the submit-time
        # gate below judge the same run. Without it the panel would size the
        # whole upload and refuse targets that submit then accepts.
        #
        # HALF OF THIS LIVES IN THE BROWSER. `preflight_target_segments` reads
        # `target_input` off the form, so the sentence above is only true while
        # static/js/preflight.js actually POSTS that field — and for its first
        # release it did not. The server was correct in isolation and the
        # feature was still dead: whole 3S7G plus contig `A236-300,B236-300`
        # arrived here with no contig at all, sized 415, and greyed out the Run
        # button for a 130-residue run. Both halves are pinned by
        # tests/test_preflight_panel_contract.py; do not read this comment as a
        # description of the system unless that file is still passing.
        target_segments=preflight_target_segments(request.form),
    )
    return (_verdict_to_json(verdict, source_label), 200)


def _run_validate(adapter, overrides=None):
    """Run ``adapter.validate`` on the posted form. Shared by submit and validate."""
    # Declare whether this run has a structure of its own, the same way both
    # campaign routes do. Assigned OVER the form dict so it cannot be forged by
    # posting the field directly. An adapter that ignores the key (every one but
    # proteina) is unaffected.
    form = dict(request.form.items())
    reuse = (request.form.get("reuse_pdb_token") or "").strip()
    upload = request.files.get("target_pdb") or request.files.get("target_sdf")
    form["_has_custom_target"] = (
        "1" if ((upload is not None and upload.filename) or reuse) else ""
    )
    form.update(overrides or {})
    return adapter.validate(form, request.files)


def _single_container_refusal(tool: str, inputs: dict):
    """Return ``(message, ceiling)`` when the design count needs a campaign, else None."""
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.scale_up import over_ceiling_count  # noqa: PLC0415
    requested_n = over_ceiling_count(tool, inputs, inputs.get("preset") or "pilot")
    if requested_n is None:
        return None
    ceiling = cc.single_container_ceiling(tool)
    return (
        f"{requested_n} designs is more than one GPU container "
        f"runs for {tool} (max {ceiling} per single job). "
        # No promise of "no per-job ceiling" here. Every piece is still one
        # container under the same pipeline timeout, so the campaign routes
        # run the same size gate this branch just failed
        # (blueprints/campaigns.py::compute_campaign_create and
        # blueprints/targets.py::_collect_launch_specs, both
        # calling size_error with plan.designs_for_chunk(0)). A large target
        # therefore caps the designs per piece there too, and a user sent over
        # by a promise of no ceiling would read a second refusal instead. What
        # a campaign lifts is the count one container runs, so that is all this
        # claims -- not that nothing caps the total. ``plan_chunks`` still
        # raises above MAX_SUBJOBS_PER_CAMPAIGN sub-jobs
        # (shared/compute_campaigns.py::MAX_SUBJOBS_PER_CAMPAIGN, 50000), which
        # no realistic ask reaches but which makes "not capped" false as
        # written.
        f"Start a full-size run instead: open /campaigns/new. It is "
        f"split into pieces that run on our GPUs and bill as they "
        f"finish, so you can ask for far more designs than one job "
        f"runs -- though each piece is still one container, so a large "
        f"target limits how many designs fit in a piece.",
        ceiling,
    )


def _needs_pdb(adapter, preset, inputs: dict) -> bool:
    # Per-preset PDB requirement: paid presets need an upload, smoke
    # and preview do not. Falls back to the adapter-level flag for
    # tools that require a PDB on every paid run (e.g. BindCraft).
    # An adapter whose target is OPTIONAL (proteina: curated benchmark task OR
    # your own structure) reports requires_pdb=False on every preset, so the
    # gate never fires for it — but a run that declared a custom target
    # and has no file is exactly as doomed as a missing mandatory upload, and
    # for the same reason: it would create a job row, dispatch a container, and
    # be refused there. Fold it into the same pre-create_job gate.
    return (
        bool(getattr(preset, "requires_pdb", False))
        or adapter.requires_pdb
        or inputs.get("target_source") == "custom"
    )


def _has_pdb_source() -> bool:
    uploaded = request.files.get("target_pdb")
    reuse_token = (request.form.get("reuse_pdb_token") or "").strip()
    return bool(
        (uploaded is not None and uploaded.filename)
        or reuse_token.startswith(
            ("job:", "handoff:", "resample:", "alphafold:", "target:")
        )
    )


@tools_bp.route("/tools/<tool>/validate", methods=["POST"])
@login_required
def tool_validate(tool: str):
    """Free "Check my settings": tool_submit's validate, preset, ceiling and missing-PDB checks, as JSON.

    The uploaded structure is not inspected here; ``tool_preflight`` does that.
    No @idempotent and no @requires_wallet; tests/test_tool_validate.py pins
    that a call creates no job and places no hold.
    """
    adapter, err = _require_tool(tool)
    if err:
        return ({"ok": False, "error": "Unknown tool"}, 404)
    overrides = None
    # _campaign is set by static/js/check_settings.js when the page is in campaign
    # mode; this mirrors blueprints/campaigns.py steps 0-2 (preset refusal, plan, validate at 1 design).
    campaign = request.form.get("_campaign") == "1"
    if campaign:
        from blueprints.campaigns import campaign_preset_refusal  # noqa: PLC0415
        from shared import compute_campaigns as cc  # noqa: PLC0415
        campaign_preset = (request.form.get("preset") or "pilot").strip() or "pilot"
        refusal = campaign_preset_refusal(tool, campaign_preset)
        if refusal:
            return {"ok": False, "error": refusal}
        try:
            plan = cc.plan_chunks(tool, request.form.get("requested_designs"), campaign_preset)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        overrides = {plan.design_param_key: "1"}
    inputs, error_msg = _run_validate(adapter, overrides)
    if inputs is None:
        return {"ok": False, "error": error_msg}
    preset = adapter.preset_for(inputs["preset"])
    if preset is None:
        return {"ok": False, "error": "Unknown preset."}
    refusal = None if campaign else _single_container_refusal(tool, inputs)
    if refusal is not None:
        return {"ok": False, "error": refusal[0]}
    if _needs_pdb(adapter, preset, inputs) and not _has_pdb_source():
        return {"ok": False, "error": "Upload a target PDB file."}
    return {"ok": True, "error": None}


@tools_bp.route("/tools/<tool>/submit", methods=["POST"])
@login_required
@idempotent()
@requires_wallet
def tool_submit(tool: str):
    """Validate, place a wallet hold, upload PDB, spawn Modal, redirect to job detail."""
    adapter, err = _require_tool(tool)
    if err:
        return err

    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    # Campaign-only tools never run as a single atomic job — a crafted submit is
    # redirected to the campaign create flow rather than spawning a doomed
    # one-container run (and rendering a form these tools do not ship). Mirrors
    # the tool_form guard. The set is currently empty (proteina left it when it
    # gained a form template); the guard stays for the next tool that needs it.
    from shared import compute_campaigns as _cc  # noqa: PLC0415
    if tool in _cc.CAMPAIGN_ONLY_TOOLS:
        return redirect(url_for("campaigns.compute_campaign_new"))

    # Workspace context (Wave-2). The /workspaces/<id> detail page
    # links to /tools/<slug>?workspace_id=...&target_pdb_id=... and
    # the form template forwards both as hidden inputs (see
    # ``workspace_hidden_inputs`` macro in
    # ``templates/tools/_prefill.html``). When present, the
    # workspace_preflight gate below rejects expired or
    # cap-exhausted workspaces BEFORE we create the job row, and the
    # IDs flow through to ``create_job`` so the completion-side
    # ``charge_for_job`` wiring (item #6) can bill the right cap.
    ws_id_form = (request.form.get("workspace_id") or "").strip()
    ws_target_form = (request.form.get("target_pdb_id") or "").strip()
    workspace_ctx: dict | None = None
    if ws_id_form and ws_target_form:
        workspace_ctx = {
            "workspace_id": ws_id_form,
            "target_pdb_id": ws_target_form,
        }

    inputs, error_msg = _run_validate(adapter)
    if inputs is None:
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error=error_msg,
            pre_fill=dict(request.form.items()),
            pdb_source=None,
            workspace_ctx=workspace_ctx,
        )

    preset = adapter.preset_for(inputs["preset"])
    if preset is None:
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error="Unknown preset.",
            pre_fill=inputs,
            pdb_source=None,
            workspace_ctx=workspace_ctx,
        )

    # D1 backstop: a count above one container's worth for a
    # campaign-supported tool must not run as a doomed single job. The
    # form re-points such submits to the campaign chunker client-side; this
    # catches the JS-off / reuse-token path. Returning here (before
    # create_job) leaves g.wallet_hold_consumed False, so requires_wallet
    # auto-releases the hold — no money-path change.
    refusal = _single_container_refusal(tool, inputs)
    if refusal is not None:
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error=f"{refusal[0]} Your wallet was not charged.",
            pre_fill=inputs,
            pdb_source=None,
            workspace_ctx=workspace_ctx,
            single_container_ceiling=refusal[1],
        )

    # Workspace gate (when context present). Rejects expired,
    # refunded, or cap-exhausted workspaces BEFORE the job row is
    # written, BEFORE PDB upload, BEFORE the Modal call. Submissions
    # without workspace context are gated by the wallet alone — the
    # requires_wallet decorator placed a hold before this handler ran.
    if workspace_ctx is not None:
        from shared.workspaces import workspace_preflight  # noqa: PLC0415
        preflight = workspace_preflight(
            ctx.user_id, workspace_ctx["target_pdb_id"]
        )
        if not preflight.allow:
            if preflight.reason == "no_workspace":
                return redirect(url_for("wallet.workspaces_new"))
            # cap_exceeded / expired: send the user to the workspace
            # detail so the cap meter + upgrade CTA explain why.
            return redirect(
                url_for(
                    "wallet.workspace_detail",
                    workspace_id=workspace_ctx["workspace_id"],
                )
            )
        # Sanity-check: the user's active workspace for this target
        # may differ from the one the form claims (e.g. if they
        # bought a second workspace mid-session). Trust the form ID
        # for charge attribution; preflight already confirmed an
        # active workspace exists for this user+target.
        workspace_ctx["workspace_id"] = preflight.workspace.id

    needs_pdb = _needs_pdb(adapter, preset, inputs)
    uploaded = request.files.get("target_pdb")
    reuse_token = (request.form.get("reuse_pdb_token") or "").strip()

    # Gate "no PDB attached" BEFORE create_job. Otherwise the row gets
    # written, the upload check fails further down, and the row sits
    # in 'pending' forever as an orphan with no Modal call and no
    # spend ledger entry. Production incident 2026-04-30: a pxdesign
    # pilot submit with no file attached created job d2d421ad which
    # showed PENDING for 2.5 hours until manually cancelled.
    if needs_pdb and not _has_pdb_source():
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error="Upload a target PDB file.",
            pre_fill=inputs,
            pdb_source=None,
            workspace_ctx=workspace_ctx,
        )

    # Resolve a ``target:`` reuse token HERE, before create_job. Two reasons:
    # the job row carries target_id (the only way a standalone run reaches its
    # target's combined table), and a bad id has to be rejected before a job
    # row exists — the same failure shape the missing-PDB gate above prevents.
    # The wallet hold is already placed by @requires_wallet at this point; what
    # matters is that returning here leaves ``g.wallet_hold_consumed`` False,
    # so the decorator releases it. Do not move this below the point where that
    # flag is set.
    #
    # The owner-scoped fetch is the ENTIRE tenancy boundary: copy_input takes
    # no source_user_id and download_input will read any object in the bucket,
    # so resolving this uuid to a storage path any other way is a cross-tenant
    # structure read.
    reuse_target = None
    if reuse_token.startswith("target:"):
        from shared.targets import get_target  # noqa: PLC0415
        reuse_target = get_target(
            reuse_token.split(":", 1)[1].strip(), user_id=ctx.user_id,
        )
        # Archived is rejected here as well as missing. An archived target is
        # excluded from the retention sweeper's protected set, so its structure
        # is deleted once it ages out — accepting one would create a job row,
        # copy nothing, and die in Storage. `/campaigns` already rejects the
        # same id; the two routes must not disagree about what is launchable.
        if (
            reuse_target is None
            or reuse_target.is_archived
            or not reuse_target.storage_path
        ):
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=(
                    "That target is archived."
                    if reuse_target is not None and reuse_target.is_archived
                    else "That target could not be found."
                ),
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )
        # An attached file OVERRIDES the token — that is the documented
        # behaviour for every reuse token (templates/tools/_prefill.html says
        # so verbatim). The target's structure is then never staged, so the
        # run must NOT be filed under it: a design produced from some other
        # structure appearing in that target's merged ranking is worse than
        # one that is merely unparented.
        if uploaded is not None and uploaded.filename:
            reuse_target = None

    # ---- PDB pre-flight inspection (Bug 9 follow-on) ----
    # Run a fast Biopython inspection on freshly-uploaded files so we
    # can reject obvious garbage (no protein, no ATOM records, malformed
    # parse) and validate user-typed target_chain + hotspots BEFORE
    # spinning up Modal. Reuse-token paths are not
    # re-inspected (the source job's PDB has already passed this gate).
    # Bytes are read here ONCE; we pass them through to upload_input
    # below so we don't need to seek the file pointer back.
    pdb_bytes: bytes | None = None
    inspection = None
    converted_filename: str | None = None
    if needs_pdb and uploaded is not None and uploaded.filename:
        pdb_bytes = uploaded.read()
        inspection = inspect_pdb_bytes(pdb_bytes, filename=uploaded.filename)
        logger.info(
            "pdb_inspect %s/%s: %s",
            adapter.slug, preset.slug, summarize_for_log(inspection),
        )
        if not inspection.ok:
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=inspection.error,
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )
        target_chain = (inputs.get("target_chain") or "").strip()
        if target_chain:
            chain_err = validate_target_chain(inspection, target_chain)
            if chain_err:
                return render_template(
                    adapter.form_template, adapter=adapter,
                    error=chain_err, pre_fill=inputs, pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )
            hotspots = inputs.get("hotspot_residues") or []
            # boltz2 hotspots are 1-indexed SEQUENCE positions, not
            # original PDB numbering; they are range-checked against the
            # antigen length in boltz2's own preflight, so skip the
            # original-numbering check here (it would false-reject an
            # antigen whose numbering does not start at 1).
            if hotspots and adapter.slug != "boltz2":
                in_range, out_of_range = validate_hotspots(
                    inspection, target_chain, hotspots,
                )
                if out_of_range:
                    return render_template(
                        adapter.form_template, adapter=adapter,
                        error=hotspot_range_message(
                            inspection, target_chain, out_of_range,
                        ),
                        pre_fill=inputs, pdb_source=None,
                        workspace_ctx=workspace_ctx,
                    )

        # ---- CIF -> PDB conversion (fleet-wide fix for
        # MPNN/RFdiff/BindCraft/RFantibody) ----
        # ProteinMPNN's parser and the rfdiffusion / bindcraft /
        # rfantibody docker pipelines are PDB-column-strict and
        # crash on CIF text (ValueError on byte-slice float
        # conversion). Convert here, before storage upload, so
        # Modal workers always see real PDB content. Pxdesign and
        # Boltzgen accept PDB just as well as CIF, so this is
        # universally safe across the tool set.
        fname_lower = uploaded.filename.lower()
        if fname_lower.endswith(".cif") or fname_lower.endswith(".mmcif"):
            try:
                pdb_bytes = convert_cif_to_pdb_bytes(
                    pdb_bytes, uploaded.filename,
                )
            except CifConversionError as exc:
                return render_template(
                    adapter.form_template, adapter=adapter,
                    error=str(exc), pre_fill=inputs, pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )
            converted_filename = (
                uploaded.filename.rsplit(".", 1)[0] + ".pdb"
            )
            logger.info(
                "cif_convert %s/%s: %s -> %s (%d bytes)",
                adapter.slug, preset.slug,
                uploaded.filename, converted_filename, len(pdb_bytes),
            )
        else:
            converted_filename = uploaded.filename

    # ---- AlphaFold reuse_token: fetch the AF model + use as PDB ----
    # When the user clicked "Use AlphaFold model instead" in the
    # preflight panel, the form replaces the file upload with
    # reuse_pdb_token="alphafold:<accession>". Fetch the model now,
    # treat the bytes as the upload for the rest of the submit path,
    # and let the hard-gate preflight below decide if the hotspots
    # still resolve on the AF model.
    af_accession_for_reuse: str | None = None
    if reuse_token.startswith("alphafold:"):
        af_accession_for_reuse = reuse_token.split(":", 1)[1].strip()
        af_bytes = _fetch_alphafold_bytes(af_accession_for_reuse)
        if af_bytes is None:
            if hold_tx_id_from_g := getattr(g, "wallet_hold_tx_id", None):
                try:
                    wallet_release_hold(
                        hold_tx_id_from_g, reason="alphafold_fetch_failed",
                    )
                except Exception:
                    logger.warning(
                        "tool_submit: release_hold after AF fetch fail "
                        "raised for hold=%s", hold_tx_id_from_g,
                        exc_info=True,
                    )
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=(
                    f"Couldn't fetch AlphaFold model AF-{af_accession_for_reuse}. "
                    f"Try uploading a target PDB directly."
                ),
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )
        pdb_bytes = af_bytes
        converted_filename = f"AF-{af_accession_for_reuse}.pdb"

    # ---- Hard-gate preflight (the rfantibody / hcruz fix) ----
    # For binder design tools, re-run the per-tool normalizer in
    # dry-run mode against the bytes we're about to ship to Modal
    # and BLOCK the submit on NEEDS_FIX. The exact same logic powers
    # the /tools/<tool>/preflight AJAX endpoint that drives the panel
    # above the Run button, so the user has already seen this verdict
    # before clicking. The gate here is the safety net for direct-POST
    # / curl / form-resubmit-without-JS paths.
    if (
        adapter.slug in PREFLIGHT_TOOLS
        and pdb_bytes is not None
    ):
        preflight_target_chain = (inputs.get("target_chain") or "").strip()
        # NOT ``inputs["hotspot_residues"]``. proteina emits that key as BARE
        # author numbers with the chain letter stripped, so on a multi-chain
        # contig it cannot distinguish a hotspot the user typed bare (which
        # ships onto the first chain) from one they chain-prefixed (which ships
        # onto the chain they named) — and the gate below wants opposite
        # verdicts for those. ``shipped_hotspots`` prefers the prefixed
        # ``hotspot_spec`` exactly as the container does, and is a no-op for
        # every other tool, whose ``hotspot_residues`` is already the shipped
        # token.
        #
        # THE AJAX PANEL THAT PREVIEWS THIS VERDICT DOES NOT SEND THE SAME
        # FORM, and the comment here used to claim it always had.
        # ``tool_preflight`` above only re-attaches a prefix the user already
        # typed — ``_resnum if _cid is None else f"{_cid}{_resnum}"`` — so a
        # bare token stays a bare int on that path while ``hotspot_spec``
        # prefixes it.
        #
        # The two still agree, and NOT by luck: preflight attributes a bare
        # token to the FIRST named chain, which is the same rule the adapter
        # applied when it built the prefix, so the bare token and the
        # ``hotspot_spec`` token derived from it name the same residue by
        # construction. Executed panel-vs-gate over 9 cases — bare and
        # prefixed, in range and out, single- and multi-chain: 0 disagreements
        # on verdict kind. What can still differ is the token ECHOED back in
        # the refusal text (``520`` vs ``A520``), which is cosmetic and, on the
        # gate's side, the more useful of the two.
        preflight_hotspots = shipped_hotspots(inputs)
        preflight_binder_max, preflight_num_designs = (
            _parse_preflight_size_params(inputs)
        )
        try:
            preflight_verdict = preflight_for_tool(
                adapter.slug, pdb_bytes,
                target_chain=preflight_target_chain,
                hotspots=preflight_hotspots,
                binder_max_aa=preflight_binder_max,
                num_designs=preflight_num_designs,
                # The validator already parsed the contig, so this is the
                # exact selection that will reach the model — no re-parse and
                # no chance of the gate sizing something the run will not use.
                target_segments=preflight_target_segments(inputs),
            )
        except Exception:
            # Defensive: a preflight crash must not block submit on
            # otherwise-valid uploads. Log and let the existing
            # server-side normalizer in the Modal pipeline handle it.
            logger.exception("preflight unexpected error tool=%s",
                             adapter.slug)
            preflight_verdict = None
        if preflight_verdict is not None and not preflight_verdict.ok:
            if hold_for_release := getattr(g, "wallet_hold_tx_id", None):
                try:
                    wallet_release_hold(
                        hold_for_release, reason="preflight_failed",
                    )
                except Exception:
                    logger.warning(
                        "tool_submit: release_hold on preflight "
                        "failure raised for hold=%s",
                        hold_for_release, exc_info=True,
                    )
            source_label = converted_filename or (
                uploaded.filename if uploaded is not None else None
            ) or ""
            if adapter.slug not in _PREFLIGHT_PANEL_FORMS:
                # pxdesign / boltz2 forms have no rich panel — surface a
                # plain actionable message so the rejection is visible.
                plain = (
                    preflight_verdict.reason
                    or "This target can't run as-is."
                )
                if preflight_verdict.suggested_fix:
                    plain = f"{plain} {preflight_verdict.suggested_fix}"
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=plain,
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=None,
                preflight_verdict=_verdict_to_json(
                    preflight_verdict, source_label,
                ),
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )
        # Verdict is OK — stash the JSON shape on inputs._preflight so
        # the /jobs/<id> page can replay the same panel ("we cleaned X,
        # Y, Z"; "swapped in AlphaFold AF-Pxxxxxx"). Persists with the
        # job row so the panel survives a page refresh and shows up on
        # completed jobs too — useful for "what did the cleanup change
        # before this design pool was generated?" provenance later.
        if preflight_verdict is not None and preflight_verdict.ok:
            ok_source_label = converted_filename or (
                uploaded.filename if uploaded is not None else None
            ) or (
                f"AF-{af_accession_for_reuse}.pdb"
                if af_accession_for_reuse else ""
            )
            inputs = dict(inputs)
            inputs["_preflight"] = _verdict_to_json(
                preflight_verdict, ok_source_label,
            )
            # Record explicitly when the user actually accepted the AF
            # swap (vs the verdict merely surfacing the suggestion).
            if af_accession_for_reuse:
                inputs["_preflight"]["used_alphafold"] = True
                inputs["_preflight"]["alphafold_accession_used"] = \
                    af_accession_for_reuse

    # Create the tool_jobs row so we have job_id + job_token for the
    # Modal payload and a persistent handle even if Modal submit
    # raises. Workspace IDs (when present) are stashed in inputs._workspace
    # so the completion-side ``charge_for_job`` (item #6) bills the
    # right cap.
    ws_target = workspace_ctx["target_pdb_id"] if workspace_ctx else None
    ws_id = workspace_ctx["workspace_id"] if workspace_ctx else None
    # Stash the wallet hold id (from the requires_wallet decorator)
    # on the job's inputs so the settle path in shared.jobs can
    # close it out on completion. None when the estimate was zero
    # (smoke runs); the settle hook short circuits in that case.
    hold_tx_id = getattr(g, "wallet_hold_tx_id", None)
    wallet_estimate = getattr(g, "wallet_estimate_usd", None)
    if hold_tx_id or wallet_estimate is not None:
        inputs = dict(inputs)
        wallet_ctx = dict(inputs.get("_wallet") or {})
        if hold_tx_id:
            wallet_ctx["hold_tx_id"] = hold_tx_id
        if wallet_estimate is not None:
            wallet_ctx["estimate_usd"] = str(wallet_estimate)
        if (hold_usd := getattr(g, "wallet_hold_usd", None)) is not None:
            wallet_ctx["hold_usd"] = str(hold_usd)
            wallet_ctx["balance_limited"] = bool(
                getattr(g, "wallet_balance_limited", False)
            )
        wallet_ctx["tool_slug"] = adapter.slug
        inputs["_wallet"] = wallet_ctx

    # C4 — free-form campaign label. Trimmed + length-capped in
    # create_job so power users running 50 variations of one target
    # see them grouped on /jobs instead of 50 flat rows.
    form_campaign_label = (request.form.get("campaign_label") or "").strip()

    job = create_job(
        user_id=ctx.user_id,
        tool=adapter.slug,
        preset=preset.slug,
        inputs=inputs,
        target_pdb_id=ws_target,
        workspace_id=ws_id,
        campaign_label=form_campaign_label or None,
        target_id=(reuse_target.id if reuse_target is not None else None),
    )
    if job is None:
        # Release the hold so we don't leave a stranded reservation.
        if hold_tx_id:
            try:
                wallet_release_hold(
                    hold_tx_id, reason="job_create_failed"
                )
            except Exception:
                logger.warning(
                    "tool_submit: release_hold after create_job "
                    "failure raised for hold=%s",
                    hold_tx_id, exc_info=True,
                )
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error=(
                "Could not create job record. Supabase is unreachable. "
                "Try again in a moment."
            ),
            pre_fill=inputs,
            pdb_source=None,
            workspace_ctx=workspace_ctx,
        )

    # create_job succeeded and the hold_tx_id is now stashed on
    # inputs._wallet. shared.jobs._settle_wallet_hold_for_completed_job
    # owns the hold lifecycle from here on. Tell the requires_wallet
    # decorator not to fire its auto-release: if the storage upload
    # or the Modal submit fails below, those paths release the hold
    # explicitly (release_hold is idempotent, so a follow-up
    # auto-release would no-op, but mark it consumed anyway for
    # clarity).
    g.wallet_hold_consumed = True

    presigned_url = ""
    staged_path = ""
    staged_filename = ""
    # Bytes resolved in-memory by a reuse token (resample:),
    # captured so the reuse verification below need not re-download them.
    reuse_resolved_bytes: bytes | None = None
    if needs_pdb:
        try:
            if uploaded is not None and uploaded.filename:
                # converted_filename is the original name with a .pdb
                # extension after CIF conversion (set above), or the
                # original .pdb filename unchanged. Storage + Modal
                # always see .pdb because pdb_bytes is always PDB by
                # the time we get here.
                staged_filename = converted_filename or uploaded.filename
                # pdb_bytes was read (and possibly converted) during
                # pre-flight; reuse instead of double-reading.
                file_data = pdb_bytes if pdb_bytes is not None else uploaded.read()
                staged_path = upload_input(
                    user_id=ctx.user_id,
                    job_id=job.id,
                    filename=staged_filename,
                    data=file_data,
                    content_type="chemical/x-pdb",
                )
            elif reuse_token.startswith("job:"):
                # Wave 3A clone: copy PDB from the original job's prefix.
                prior_job_id = reuse_token.split(":", 1)[1]
                prior = get_job(prior_job_id, user_id=ctx.user_id)
                if prior is None:
                    raise StorageError("source job not found")
                src_path = (prior.inputs or {}).get("_pdb_storage_path")
                src_name = (prior.inputs or {}).get("_pdb_filename")
                if not src_path or not src_name:
                    raise StorageError("source job has no stored PDB")
                staged_filename = src_name
                staged_path = copy_input(
                    source_path=src_path,
                    dest_user_id=ctx.user_id,
                    dest_job_id=job.id,
                    filename=src_name,
                )
            elif reuse_target is not None:
                # Target reuse: copy the target's staged structure into this
                # job's prefix so the RLS owner-prefix still holds, exactly as
                # the job:/handoff: paths do. Ownership was resolved before
                # create_job; do NOT re-derive the path from the token here.
                # The copied bytes are re-inspected by the reuse hard-gate
                # further down (it fires for every non-alphafold token), so a
                # chain that does not match this run is caught before Modal.
                staged_filename = reuse_target.filename or "target.pdb"
                staged_path = copy_input(
                    source_path=reuse_target.storage_path,
                    dest_user_id=ctx.user_id,
                    dest_job_id=job.id,
                    filename=staged_filename,
                )
            elif reuse_token.startswith("handoff:"):
                # Wave 3C Scout handoff: copy PDB staged by Scout.
                ho_id = reuse_token.split(":", 1)[1]
                ho = get_handoff(ho_id, user_id=ctx.user_id)
                if ho is None:
                    raise StorageError(
                        "handoff not found or already consumed"
                    )
                staged_filename = ho.pdb_filename
                staged_path = copy_input(
                    source_path=ho.pdb_storage_path,
                    dest_user_id=ctx.user_id,
                    dest_job_id=job.id,
                    filename=ho.pdb_filename,
                )
                mark_consumed(ho.id)
            elif reuse_token.startswith("alphafold:"):
                # AlphaFold fallback: pdb_bytes was already populated by
                # the AF fetch above the preflight gate (so the gate
                # could vote on the actual model). Stage those bytes as
                # if the user had uploaded them.
                if pdb_bytes is None:
                    raise StorageError("alphafold fetch produced no bytes")
                staged_filename = (
                    converted_filename or f"AF-{af_accession_for_reuse}.pdb"
                )
                staged_path = upload_input(
                    user_id=ctx.user_id,
                    job_id=job.id,
                    filename=staged_filename,
                    data=pdb_bytes,
                    content_type="chemical/x-pdb",
                )
            elif reuse_token.startswith("resample:"):
                # AF2-resample chain: decode the source fold job's
                # predicted PDB (stored as base64 in
                # ``result.pdb_b64`` across AF2/ColabFold/ESMFold)
                # and stage it as a fresh MPNN input PDB. The
                # source-tool gate prevents stuffing a non-fold
                # job id into the token to read its result blob.
                import base64  # noqa: PLC0415
                src_job_id = reuse_token.split(":", 1)[1]
                src = get_job(src_job_id, user_id=ctx.user_id)
                if src is None:
                    raise StorageError("source fold job not found")
                if not _resample.can_resample(src.tool):
                    raise StorageError(
                        "source job is not a fold predictor"
                    )
                src_pdb_b64 = (
                    (src.result or {}).get("pdb_b64") or ""
                ).strip()
                if not src_pdb_b64:
                    raise StorageError(
                        "source job has no predicted PDB"
                    )
                try:
                    src_pdb_bytes = base64.b64decode(
                        src_pdb_b64, validate=True
                    )
                except Exception as exc:
                    raise StorageError(
                        f"predicted PDB decode failed: {exc}"
                    )
                reuse_resolved_bytes = src_pdb_bytes
                staged_filename = (
                    f"predicted-{src.tool}-{src.id[:8]}.pdb"
                )
                staged_path = upload_input(
                    user_id=ctx.user_id,
                    job_id=job.id,
                    filename=staged_filename,
                    data=src_pdb_bytes,
                    content_type="chemical/x-pdb",
                )

            presigned_url = presigned_input_url(
                staged_path, expires_seconds=7200
            )
            # Persist the storage path + filename on the job row so a
            # future clone can re-use the file without re-uploading.
            update_inputs(
                job.id,
                {
                    **inputs,
                    "_pdb_storage_path": staged_path,
                    "_pdb_filename": staged_filename,
                },
            )
        except StorageError as exc:
            mark_failed(
                job.id,
                error={"bucket": "storage", "detail": str(exc)},
            )
            if hold_tx_id:
                try:
                    wallet_release_hold(
                        hold_tx_id, reason="storage_failure"
                    )
                except Exception:
                    logger.warning(
                        "tool_submit: release_hold on storage error "
                        "raised for hold=%s",
                        hold_tx_id, exc_info=True,
                    )
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=f"Upload failed: {exc}",
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

    # ---- Reuse-token inspection + hard-gate (gap 2) ----
    # Fresh uploads are inspected + gated at the boundary above, but the
    # reuse tokens (job:/handoff:/resample:) stage bytes that
    # skipped both. Re-check the RESOLVED bytes here before any Modal
    # call so a mismatch (wrong chain, oversized, corrupt predicted PDB
    # piped into MPNN) is flagged upfront. alphafold: already populated
    # pdb_bytes and ran the hard-gate above, so it is excluded.
    if (
        needs_pdb
        and pdb_bytes is None
        and reuse_token
        and not reuse_token.startswith("alphafold:")
        and staged_path
    ):
        check_bytes = reuse_resolved_bytes
        if check_bytes is None:
            # job: / handoff: copied storage-to-storage; read the staged
            # object back to verify it. Best-effort: a verification-only
            # download hiccup must not block an already-staged reuse.
            try:
                check_bytes = download_input(staged_path)
            except StorageError:
                logger.warning(
                    "tool_submit: could not download staged reuse PDB "
                    "for verification job=%s path=%s",
                    job.id, staged_path, exc_info=True,
                )
                check_bytes = None
        if check_bytes is not None:
            reuse_binder_max, reuse_num_designs = (
                _parse_preflight_size_params(inputs)
            )
            reuse_err = _verify_reuse_pdb_bytes(
                adapter, check_bytes,
                target_chain=(inputs.get("target_chain") or "").strip(),
                # Same rule as the fresh-upload gate above: the prefixed
                # ``hotspot_spec`` when the adapter emits one, because this
                # path runs both ``validate_hotspots`` and the full preflight
                # and both range-check by chain.
                hotspots=shipped_hotspots(inputs),
                filename=staged_filename or "input.pdb",
                binder_max_aa=reuse_binder_max,
                num_designs=reuse_num_designs,
                target_segments=preflight_target_segments(inputs),
            )
            if reuse_err:
                mark_failed(
                    job.id,
                    error={"bucket": "preflight", "detail": reuse_err},
                )
                if hold_tx_id:
                    try:
                        wallet_release_hold(
                            hold_tx_id, reason="reuse_preflight_failed",
                        )
                    except Exception:
                        logger.warning(
                            "tool_submit: release_hold on reuse preflight "
                            "failure raised for hold=%s",
                            hold_tx_id, exc_info=True,
                        )
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=reuse_err,
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )

    job_spec = adapter.build_payload(inputs, presigned_url)
    webhook_url = url_for(
        "modal_result",
        job_id=job.id,
        job_token=job.job_token,
        _external=True,
    )
    upload_urls_endpoint = url_for(
        "upload_urls",
        job_id=job.id,
        job_token=job.job_token,
        _external=True,
    )

    try:
        submit_result = current_app.modal_client.submit(
            adapter.slug,
            preset.slug,
            inputs={
                **job_spec,
                "_input_pdb_url": presigned_url,
                "_input_presigned_url": presigned_url,
                "_upload_urls_endpoint": upload_urls_endpoint,
            },
            job_id=job.id,
            job_token=job.job_token,
            webhook_url=webhook_url,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Modal submit failed for job %s", job.id)
        mark_failed(
            job.id,
            error={"bucket": "modal-submit", "detail": str(exc)},
        )
        if hold_tx_id:
            try:
                wallet_release_hold(
                    hold_tx_id, reason="modal_submit_failure"
                )
            except Exception:
                logger.warning(
                    "tool_submit: release_hold on modal submit "
                    "failure raised for hold=%s",
                    hold_tx_id, exc_info=True,
                )
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error=(
                "Could not submit to the GPU pool. Your wallet was "
                "not charged. Try again or contact support at "
                + (
                    os.environ.get("SUPPORT_EMAIL", "info@ranomics.com").strip()
                    or "info@ranomics.com"
                )
                + "."
            ),
            pre_fill=inputs,
            pdb_source=None,
            workspace_ctx=workspace_ctx,
        )

    set_modal_call(job.id, submit_result["function_call_id"])

    # D3 funnel fire. Distinguish the user's first-ever submission
    # from their nth so the dashboard can read activation rate
    # directly. list_jobs_paginated returns a count that already
    # includes the row we just created, so total == 1 -> first.
    # Best-effort: a count failure must not stall the redirect.
    try:
        _, total_jobs = list_jobs_paginated(
            ctx.user_id, page=1, page_size=1,
        )
        is_first = total_jobs == 1
    except Exception:
        is_first = False
    from shared.events import EVENTS, emit  # noqa: PLC0415
    emit(
        EVENTS.FIRST_JOB_SUBMITTED if is_first
        else EVENTS.NTH_JOB_SUBMITTED,
        user_id=ctx.user_id,
        properties={
            "tool": adapter.slug,
            "preset": preset.slug,
            "is_pilot": preset.slug == "pilot",
            "job_id": job.id,
        },
    )

    return redirect(url_for("jobs.job_detail", job_id=job.id))

# ------------------------------------------------------------------
# Public tool comparison matrix + campaign intake stub
# ------------------------------------------------------------------

@tools_bp.route("/tools", methods=["GET"])
def tools_comparison():
    """Public discovery hub for the full tool catalog.

    Renders the iteration-loop framing, a category-grouped tile
    grid, and the comparison matrix at the bottom for power users.
    Catalog includes both hardcoded tools (Epitope Scout, Binder
    Developability Scout) and flag-enabled GPU adapters. The Library
    Planner was delisted 2026-08-17 and is no longer in
    _HARDCODED_TOOLS; this docstring still named it, and a review
    traced a wrong count in a commit message back to here.
    """
    catalog = _build_tools_catalog()

    # Group catalog into workflow-stage sections in a stable order.
    # The order mirrors the iteration loop a scientist walks through:
    # scope → design → sequence → predict → QC.
    grouped = group_catalog(catalog)

    breadcrumbs = [
        {"name": "Home", "url": url_for("public.index", _external=True)},
        {"name": "All tools", "url": url_for(
            "tools.tools_comparison", _external=True
        )},
    ]

    # --- "Which of these is for me?" -----------------------------------
    # Answers arrive as query params from a plain GET form, so the
    # chooser works with JavaScript off and a filled-in answer is a
    # shareable URL. Unknown values are dropped rather than echoed: the
    # only thing reaching the template is a value from the tuples in
    # shared.tool_chooser.
    def _pick(param, choices):
        value = request.args.get(param, "")
        return value if value in {k for k, _ in choices} else None

    chooser_have = _pick("have", tool_chooser.HAVE_CHOICES)
    chooser_shape = _pick("shape", tool_chooser.SHAPE_CHOICES)
    chooser_chemistry = _pick("chemistry", tool_chooser.CHEMISTRY_CHOICES)
    chooser_asks_shape = chooser_have in tool_chooser.DESIGN_HAVES
    # The form was submitted but no usable answer came back: the radios
    # were left alone (an unchecked radio group sends no key at all, so
    # the form's hidden `asked` marker is the only thing that arrives),
    # or `have` came in empty or outside HAVE_CHOICES. Either way the
    # page used to re-render the bare form with no answer and no
    # explanation. A first visit sends neither key and gets no message.
    chooser_unanswered = (
        ("asked" in request.args or "have" in request.args)
        and chooser_have is None
    )
    # The same dead end one question later: `asked_shape` is rendered
    # only alongside the shape fieldset, so its arrival means the
    # visitor pressed the button with that fieldset in front of them and
    # picked nothing. Without it the press is indistinguishable from the
    # press that revealed the fieldset, and the page just came back
    # unchanged.
    chooser_shape_unanswered = (
        chooser_asks_shape
        and ("asked_shape" in request.args or "shape" in request.args)
        and chooser_shape is None
    )

    # Hold the answer back until the follow-up questions have been put,
    # otherwise a visitor who wants a nanobody sees every design tool in
    # shared/tool_chooser.py::_FACTS that takes a target structure, and
    # never learns the question that narrows them.
    chooser_picks = None
    chooser_prep = []
    if chooser_have and (not chooser_asks_shape or chooser_shape):
        answer = tool_chooser.recommend(
            chooser_have, shape=chooser_shape, chemistry=chooser_chemistry,
        )
        # A sequence-only visitor is offered the folding tools in the
        # same breath as the designers, and listing ESMFold under "I
        # want an scFv" reads as a claim that it designs one. Split
        # them: the folding tools answer a different question, which is
        # how to reach the structure the other tools need.
        if chooser_asks_shape:
            chooser_picks = [p for p in answer if p["is_designer"]]
            chooser_prep = [p for p in answer if not p["is_designer"]]
        else:
            chooser_picks = answer

    return render_template(
        "tools/comparison.html",
        tools=catalog,
        grouped=grouped,
        authenticated=bool(session.get("user_email")),
        breadcrumbs=breadcrumbs,
        have_choices=tool_chooser.HAVE_CHOICES,
        shape_choices=tool_chooser.SHAPE_CHOICES,
        chemistry_choices=tool_chooser.CHEMISTRY_CHOICES,
        chooser_have=chooser_have,
        chooser_shape=chooser_shape,
        chooser_chemistry=chooser_chemistry,
        chooser_asks_shape=chooser_asks_shape,
        chooser_unanswered=chooser_unanswered,
        chooser_shape_unanswered=chooser_shape_unanswered,
        chooser_picks=chooser_picks,
        chooser_prep=chooser_prep,
    )
