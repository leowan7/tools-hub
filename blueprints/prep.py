"""Free standalone target prep: load, check, trim, pick hotspots, hand off.

No wallet, no job row, no GPU. The only write is the handoff: the trimmed PDB
in ``tool-inputs`` plus a ``scout_handoffs`` row, the same pair Scout's handoff
writes, so the tool form's existing ``?handoff=`` prefill and submit path take
it.
"""

from __future__ import annotations

import io
import tempfile
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from scout.handoff import VALID_HANDOFF_TOOLS
from shared.auth import login_required
from shared.credits import load_user_context
from shared.feature_flags import tool_enabled
from shared.handoffs import create_handoff
from shared.pdb_inspect import (
    CifConversionError,
    convert_cif_to_pdb_bytes,
    inspect_pdb_bytes,
    split_hotspot,
    validate_target_chain,
)
from shared.pdb_preflight import PREFLIGHT_TOOLS, multi_chain_refusal
from shared.rcsb import PDB_ID_RE, EntryTooLarge, fetch_entry, search_entries
from shared.storage import MAX_UPLOAD_BYTES, StorageError, upload_input
from tools import base as tool_base

prep_bp = Blueprint("prep", __name__)


def handoff_tools() -> list:
    """Adapters the page can hand a target to.

    The Scout handoff gate's own set, narrowed to tools that run the preflight
    panel this page previews, and to what ``_require_tool`` in
    blueprints/tools.py would serve.
    """
    return [
        a for a in (tool_base.get(s)
                    for s in sorted(VALID_HANDOFF_TOOLS & PREFLIGHT_TOOLS))
        if a is not None and tool_enabled(a.slug)
    ]


@prep_bp.route("/prep")
def prep_page():
    return render_template("prep.html", tools=handoff_tools())


@prep_bp.route("/prep/search")
@login_required
def prep_search():
    q = (request.args.get("q") or "").strip()[:200]
    if not q:
        return jsonify({"results": []})
    results = search_entries(q)
    if results is None:
        return jsonify({"error": "RCSB search is unavailable. Try again, or load by PDB ID."}), 502
    return jsonify({"results": results})


@prep_bp.route("/prep/fetch/<pdb_id>")
@login_required
def prep_fetch(pdb_id: str):
    if not PDB_ID_RE.match(pdb_id):
        return jsonify({"error": "Enter a 4-character PDB ID."}), 400
    try:
        got = fetch_entry(pdb_id, MAX_UPLOAD_BYTES)
    except EntryTooLarge:
        return jsonify({"error": (
            f"{pdb_id.upper()} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. "
            "Download it, cut it down to the chains you need, and upload that."
        )}), 413
    if got is None:
        return jsonify({"error": f"{pdb_id.upper()} was not found on RCSB."}), 404
    data, filename = got
    return send_file(io.BytesIO(data), mimetype="chemical/x-pdb",
                     download_name=filename)


def _trim(form, files):
    """Crop the upload to the selected chains/range.

    Returns ``(pdb_bytes, filename, chains, kept, None)`` or a 5-tuple whose
    last item is the user-facing error.
    """
    from tools.proteina import _parse_target_input  # noqa: PLC0415
    from tools.proteina.run_pipeline import (  # noqa: PLC0415
        TargetCropError,
        pdb_ca_residues,
        selected_residue_keys,
        stage_cropped_target,
    )

    def fail(msg):
        return None, None, None, None, msg

    uploaded = files.get("target_pdb")
    if uploaded is None or not uploaded.filename:
        return fail("Load a target first.")
    raw = uploaded.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return fail(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    report = inspect_pdb_bytes(raw, filename=uploaded.filename)
    if not report.ok:
        return fail(report.error)

    range_text = (form.get("target_input") or "").strip()
    if range_text:
        segments, _contig, chains, err = _parse_target_input(range_text)
        if err:
            return fail(err)
    else:
        chains = tool_base.parse_target_chains(form.get("target_chain") or "")
        segments = [(c, None, None) for c in chains]
    if not chains:
        return fail("Name at least one chain to keep.")
    chain_err = validate_target_chain(report, " ".join(chains))
    if chain_err:
        return fail(chain_err)

    if uploaded.filename.lower().endswith((".cif", ".mmcif")):
        try:
            raw = convert_cif_to_pdb_bytes(raw, uploaded.filename)
        except CifConversionError as exc:
            return fail(str(exc))
    text = raw.decode("utf-8", errors="replace")

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdb"
        src.write_text(text)
        residues, _ = pdb_ca_residues(src)
        dest = Path(tmp) / "out.pdb"
        try:
            _n_staged, n_selected = stage_cropped_target(dest, text, residues, segments)
        except TargetCropError as exc:
            return fail(str(exc))
        if not n_selected:
            return fail("That selection keeps no residues. Check the chain and range.")
        trimmed = dest.read_bytes()
        kept = {(c, r) for c, r, _i in selected_residue_keys(residues, segments)}

    stem = secure_filename(uploaded.filename.rsplit(".", 1)[0]) or "target"
    return trimmed, f"{stem}_trimmed.pdb", chains, kept, None


@prep_bp.route("/prep/trim", methods=["POST"])
@login_required
def prep_trim():
    data, filename, _chains, _kept, err = _trim(request.form, request.files)
    if err:
        return jsonify({"error": err}), 400
    return send_file(io.BytesIO(data), mimetype="chemical/x-pdb",
                     as_attachment=True, download_name=filename)


@prep_bp.route("/prep/handoff", methods=["POST"])
@login_required
def prep_handoff():
    slug = (request.form.get("tool") or "").strip()
    if slug not in {a.slug for a in handoff_tools()}:
        return jsonify({"error": "Pick a tool to send this target to."}), 400
    ctx = load_user_context()
    if ctx is None:
        return jsonify({"error": "Sign in again to continue."}), 401

    data, filename, chains, kept, err = _trim(request.form, request.files)
    if err:
        return jsonify({"error": err}), 400
    target_chain = " ".join(chains)
    refusal = multi_chain_refusal(slug, target_chain)
    if refusal:
        return jsonify({"error": refusal}), 400

    hotspots: list[int] = []
    for tok in (request.form.get("hotspot_residues") or "").replace(",", " ").split():
        cid, resnum = split_hotspot(tok, chains)
        if resnum is None:
            return jsonify({"error": f"Hotspot {tok!r} is not a residue number."}), 400
        # scout_handoffs.hotspot_residues is integer[] (shared/handoffs.py
        # create_handoff), and tools/base.py parse_hotspot_residues reads a
        # bare integer as the FIRST target chain.
        if len(chains) > 1:
            return jsonify({"error": (
                "Hotspots on a multi-chain selection cannot be handed off yet: "
                "the tool form would read every hotspot as chain "
                f"{chains[0]}. Keep one chain, or send without hotspots and "
                "enter them on the tool form with their chain letters."
            )}), 400
        if (cid or chains[0], resnum) not in kept:
            return jsonify({"error": (
                f"Hotspot {tok} is outside the trimmed selection."
            )}), 400
        hotspots.append(resnum)

    try:
        path = upload_input(
            user_id=ctx.user_id,
            job_id=f"prep-handoff-{uuid.uuid4()}",
            filename=filename,
            data=data,
            content_type="chemical/x-pdb",
        )
    except StorageError:
        return jsonify({"error": "Could not stage the target. Try again."}), 502
    ho = create_handoff(
        user_id=ctx.user_id,
        pdb_storage_path=path,
        pdb_filename=filename,
        target_chain=target_chain,
        hotspot_residues=hotspots,
    )
    if ho is None:
        return jsonify({"error": "Could not hand the target off. Try again."}), 502
    return jsonify({"redirect": url_for("tools.tool_form", tool=slug, handoff=ho.id)})
