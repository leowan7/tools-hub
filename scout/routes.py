"""Epitope Scout Flask Blueprint mounted under ``/scout``.

Ported from ``epitope-scout/app.py`` as part of the Scout-into-tools-hub
consolidation. Auth, signup, password-reset, and the upgrade page are
owned by tools-hub (``shared.auth`` + ``/pricing``). Everything left in
this module is Scout-specific: PDB upload, structural scoring, SSE
progress, feasibility, and the tool handoff.

The free-tier paywall (``scout.quota``) still works because the
tools-hub Supabase project is the same one Scout always used.
"""

from __future__ import annotations

import csv as csv_module
import json
import logging
import re
import shutil
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from shared.auth import login_required

from scout.jobs import cleanup_old_jobs, create_job_dir
from scout.parser import parse_pdb
from scout.quota import (
    FREE_TIER_RUN_CAP,
    quota_status,
    record_scout_run,
    requires_scout_quota,
)

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdb", ".cif"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

scout_bp = Blueprint(
    "scout",
    __name__,
    url_prefix="/scout",
    template_folder="../templates/scout",
)


def _extract_structure_title(path: Path, suffix: str) -> str:
    """Extract a human-readable protein name from a PDB or mmCIF header."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""

    if suffix == ".pdb":
        compnd_lines = [
            line[10:].strip()
            for line in text.splitlines()
            if line.startswith("COMPND")
        ]
        compnd_blob = " ".join(compnd_lines)
        match = re.search(r"MOLECULE:\s*([^;]+)", compnd_blob, re.IGNORECASE)
        if match:
            return match.group(1).strip().title()
        return ""

    if suffix == ".cif":
        match = re.search(r"_struct\.title\s+'([^']+)'", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(r"_struct\.title\s+\"([^\"]+)\"", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(r"_struct\.title\s+(\S[^\n]+)", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return ""

    return ""


def _find_input_file(job_dir: Path) -> "Path | None":
    for ext in (".pdb", ".cif"):
        candidate = job_dir / f"input{ext}"
        if candidate.exists():
            return candidate
    return None


def _get_binder_overlaps(job_dir: Path, epitope_residues: list[int]) -> list[dict]:
    cache_path = job_dir / "analyze_cache.json"
    if not cache_path.exists():
        return []

    try:
        with cache_path.open() as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    epitope_set = set(epitope_residues)
    overlaps = []
    for binder in cache.get("known_binders", []):
        contacts = set(binder.get("contact_residues", []))
        overlap = epitope_set & contacts
        if overlap:
            overlaps.append({
                "pdb_id": binder.get("pdb_id", ""),
                "binder_type": binder.get("binder_type", ""),
                "species": binder.get("species", ""),
                "resolution": binder.get("resolution"),
                "affinity": binder.get("affinity", ""),
                "overlap_count": len(overlap),
                "overlap_residues": sorted(overlap),
                "total_contacts": len(contacts),
            })
    return overlaps


# ---------------------------------------------------------------------------
# Landing + quota
# ---------------------------------------------------------------------------

@scout_bp.route("/", methods=["GET"])
@login_required
def index():
    return render_template("scout/index.html"), 200


@scout_bp.route("/quota", methods=["GET"])
def quota_json():
    email = session.get("user_email", "")
    if not email:
        return jsonify({
            "tier": "anon",
            "runs_used": 0,
            "runs_cap": FREE_TIER_RUN_CAP,
            "runs_remaining": FREE_TIER_RUN_CAP,
            "unlimited": False,
        }), 200
    return jsonify(quota_status(email)), 200


# ---------------------------------------------------------------------------
# Upload + fetch + example
# ---------------------------------------------------------------------------

@scout_bp.route("/upload", methods=["POST"])
@login_required
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file submitted."}), 400

    uploaded_file = request.files["file"]
    if not uploaded_file.filename:
        return jsonify({"error": "No file selected."}), 400

    suffix = Path(uploaded_file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return jsonify({
            "error": (
                f'Unsupported file type "{suffix}". '
                "Please upload a .pdb or .cif file."
            )
        }), 400

    cleanup_old_jobs()
    job_id, job_dir = create_job_dir()

    save_path = job_dir / f"input{suffix}"
    uploaded_file.save(str(save_path))

    result = parse_pdb(save_path)
    if result.error:
        return jsonify({"error": result.error}), 422

    structure_title = _extract_structure_title(save_path, suffix)

    return jsonify({
        "job_id": job_id,
        "filename": uploaded_file.filename,
        "chains": [
            {"id": chain.id, "residue_count": chain.residue_count, "name": chain.name}
            for chain in result.chains
        ],
        "structure_title": structure_title,
    }), 200


@scout_bp.route("/fetch-pdb", methods=["POST"])
@login_required
def fetch_pdb():
    import requests as http_requests  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    pdb_id = data.get("pdb_id", "").strip().upper()

    if not pdb_id or not re.match(r"^[A-Z0-9]{4}$", pdb_id):
        return jsonify({"error": "Please enter a valid 4-character PDB ID."}), 400

    download_urls = [
        (f"https://files.rcsb.org/download/{pdb_id}.pdb", ".pdb"),
        (f"https://files.rcsb.org/download/{pdb_id}.cif", ".cif"),
    ]

    content = None
    suffix = ".pdb"
    for url, ext in download_urls:
        try:
            resp = http_requests.get(url, timeout=30)
            if resp.status_code == 200 and len(resp.content) > 100:
                content = resp.content
                suffix = ext
                break
        except Exception:
            continue

    if content is None:
        return jsonify({
            "error": f"PDB ID \"{pdb_id}\" not found on RCSB. Check the ID and try again."
        }), 404

    cleanup_old_jobs()
    job_id, job_dir = create_job_dir()
    save_path = job_dir / f"input{suffix}"
    save_path.write_bytes(content)

    result = parse_pdb(save_path)
    if result.error:
        return jsonify({"error": result.error}), 422

    structure_title = _extract_structure_title(save_path, suffix)

    return jsonify({
        "job_id": job_id,
        "filename": f"{pdb_id}{suffix}",
        "chains": [
            {"id": chain.id, "residue_count": chain.residue_count, "name": chain.name}
            for chain in result.chains
        ],
        "structure_title": structure_title,
    }), 200


@scout_bp.route("/example", methods=["GET"])
@login_required
def example():
    example_src = Path(current_app.root_path) / "static" / "example" / "1HEW.pdb"
    if not example_src.exists():
        return jsonify({"error": "Example protein file not found on server."}), 500

    cleanup_old_jobs()
    job_id, job_dir = create_job_dir()
    dest = job_dir / "input.pdb"
    shutil.copy2(str(example_src), str(dest))

    result = parse_pdb(dest)
    if result.error:
        return jsonify({"error": result.error}), 422

    structure_title = _extract_structure_title(dest, ".pdb")

    return jsonify({
        "job_id": job_id,
        "filename": "1HEW.pdb",
        "chains": [
            {"id": chain.id, "residue_count": chain.residue_count, "name": chain.name}
            for chain in result.chains
        ],
        "structure_title": structure_title,
    }), 200


# ---------------------------------------------------------------------------
# Analyze + progress SSE
# ---------------------------------------------------------------------------

@scout_bp.route("/analyze", methods=["POST"])
@login_required
@requires_scout_quota
def analyze():
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id", "").strip()
    chain_id = data.get("chain", "").strip()

    if not job_id or not chain_id:
        return jsonify({"error": "job_id and chain are required."}), 400

    job_dir = Path("tmp") / job_id
    pdb_path = _find_input_file(job_dir)
    if pdb_path is None:
        return jsonify({"error": "Job not found or expired. Please re-upload your file."}), 404

    known_binders = []
    ppi_interfaces = []
    try:
        csv_path_prelim = Path("tmp") / job_id / "results.csv"
        if not csv_path_prelim.exists():
            from scout.pipeline import run_pipeline  # noqa: PLC0415
            run_pipeline(pdb_path, chain_id)

        known_binders = []
        uniprot_id = ""
        uniprot_name = ""
        uniprot_identity_pct = "unknown"
        from scout.epitope_db import fetch_known_binders, resolve_uniprot_id  # noqa: PLC0415
        uniprot_result = resolve_uniprot_id(pdb_path, chain_id)
        uniprot_id = uniprot_result["uniprot_id"]
        uniprot_name = uniprot_result["protein_name"]
        uniprot_identity_pct = uniprot_result["identity_pct"]
        logger.warning(
            "UniProt resolution: id=%s name=%s identity=%s",
            uniprot_id or "(empty)", uniprot_name or "(none)", uniprot_identity_pct,
        )
        if uniprot_id:
            try:
                known_binders = fetch_known_binders(uniprot_id)
                logger.warning(
                    "Known binder lookup for %s: %d binders found",
                    uniprot_id, len(known_binders),
                )
            except Exception:
                logger.exception("Known binder lookup failed for %s", uniprot_id)
                known_binders = []

        from scout.interfaces import detect_interfaces  # noqa: PLC0415
        ppi_interfaces = detect_interfaces(pdb_path, chain_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception:
        logger.exception("Pipeline error for job %s", job_id)
        return jsonify({"error": "Analysis failed. Check that the PDB is valid and try again."}), 500

    _MIN_COMPOSITE = 0.40
    _MIN_RESI_COUNT = 5
    _MAX_PATCH_FRACTION = 0.30

    _chain_total = None
    try:
        _pr = parse_pdb(pdb_path)
        for _ch in _pr.chains:
            if _ch.id == chain_id:
                _chain_total = _ch.residue_count
                break
    except Exception:
        pass

    all_rows = []
    all_epitopes = []
    csv_path = Path("tmp") / job_id / "results.csv"
    if csv_path.exists():
        with csv_path.open(newline="") as csv_file:
            reader = csv_module.DictReader(csv_file)
            fieldnames = reader.fieldnames or []
            for row in reader:
                resi_nums = sorted(int(n) for n in re.findall(r'\d+', row.get("residues", "")))
                if len(resi_nums) > 1:
                    filtered = []
                    for k, num in enumerate(resi_nums):
                        prev_gap = abs(num - resi_nums[k - 1]) if k > 0 else 999
                        next_gap = abs(resi_nums[k + 1] - num) if k + 1 < len(resi_nums) else 999
                        if min(prev_gap, next_gap) <= 20:
                            filtered.append(num)
                    resi_nums = filtered if filtered else resi_nums
                filled_nums = []
                for k, num in enumerate(resi_nums):
                    filled_nums.append(num)
                    if k + 1 < len(resi_nums):
                        gap = resi_nums[k + 1] - num
                        if 1 < gap <= 4:
                            filled_nums.extend(range(num + 1, resi_nums[k + 1]))
                composite = float(row.get("composite_score", 0))
                rcount = int(row.get("residue_count", 0))
                all_rows.append(dict(row))
                all_epitopes.append({
                    "epitope_id": int(row.get("epitope_id", row.get("patch_id", 0))),
                    "residues": row.get("residues", ""),
                    "residue_numbers": filled_nums,
                    "residue_count": rcount,
                    "composite_score": composite,
                    "mean_rsa": float(row.get("mean_rsa", 0)),
                    "secondary_structure": row.get("secondary_structure", "loop"),
                    "centroid_x": float(row.get("centroid_x", 0)),
                    "centroid_y": float(row.get("centroid_y", 0)),
                    "centroid_z": float(row.get("centroid_z", 0)),
                    "_row": dict(row),
                })

    _max_resi = (
        int(_chain_total * _MAX_PATCH_FRACTION)
        if _chain_total is not None
        else None
    )
    _MIN_CENTROID_DIST = 15.0

    ranked_candidates = sorted(
        [e for e in all_epitopes
         if e["composite_score"] >= _MIN_COMPOSITE
         and e["residue_count"] >= _MIN_RESI_COUNT
         and (_max_resi is None or e["residue_count"] <= _max_resi)],
        key=lambda e: e["composite_score"],
        reverse=True,
    )

    top3 = []
    for candidate in ranked_candidates:
        cx = candidate.get("centroid_x", 0)
        cy = candidate.get("centroid_y", 0)
        cz = candidate.get("centroid_z", 0)
        too_close = False
        for selected in top3:
            sx = selected.get("centroid_x", 0)
            sy = selected.get("centroid_y", 0)
            sz = selected.get("centroid_z", 0)
            dist = ((cx - sx) ** 2 + (cy - sy) ** 2 + (cz - sz) ** 2) ** 0.5
            if dist < _MIN_CENTROID_DIST:
                too_close = True
                break
        if not too_close:
            top3.append(candidate)
        if len(top3) >= 3:
            break

    from scout.flags import compute_quality_flags, CSV_COLUMNS_ANNOTATED  # noqa: PLC0415

    _is_plddt = any(
        row.get("is_plddt", "0") == "1"
        for row in all_rows
    ) if all_rows else False

    _flag_chain_length = _chain_total or 0

    for e in all_epitopes:
        row = e["_row"]
        e["quality_flags"] = compute_quality_flags(
            secondary_structure=row.get("secondary_structure", "loop"),
            hydrophobicity=float(row.get("hydrophobicity", 0)),
            burial_raw=float(row.get("burial_raw", 0)),
            bfactor_score=float(row.get("bfactor_score", 0)),
            is_functional_site=False,
            residues_str=row.get("residues", ""),
            chain_length=_flag_chain_length,
            is_plddt=_is_plddt,
        )

    epitopes_annotated_path = Path("tmp") / job_id / "epitopes_annotated.csv"
    if top3:
        with epitopes_annotated_path.open("w", newline="") as csv_file:
            writer = csv_module.DictWriter(csv_file, fieldnames=CSV_COLUMNS_ANNOTATED)
            writer.writeheader()
            for rank, epitope in enumerate(top3, start=1):
                row = epitope["_row"].copy()
                row["epitope_id"] = rank
                row["quality_flags"] = epitope["quality_flags"]
                writer.writerow(row)

    results_annotated_path = Path("tmp") / job_id / "results_annotated.csv"
    with results_annotated_path.open("w", newline="") as csv_file:
        writer = csv_module.DictWriter(csv_file, fieldnames=CSV_COLUMNS_ANNOTATED)
        writer.writeheader()
        for e in all_epitopes:
            row = e["_row"].copy()
            row["quality_flags"] = e["quality_flags"]
            writer.writerow(row)

    epitopes_csv_path = Path("tmp") / job_id / "epitopes.csv"
    if top3 and fieldnames:
        with epitopes_csv_path.open("w", newline="") as csv_file:
            writer = csv_module.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            for rank, epitope in enumerate(top3, start=1):
                row = epitope["_row"].copy()
                row["epitope_id"] = rank
                writer.writerow(row)

    for e in top3:
        e.pop("_row", None)
        e.pop("centroid_x", None)
        e.pop("centroid_y", None)
        e.pop("centroid_z", None)

    pdb_format = pdb_path.suffix.lstrip(".") if pdb_path else "pdb"

    analyze_cache = {
        "epitopes": top3,
        "known_binders": known_binders,
        "ppi_interfaces": ppi_interfaces,
        "chain": chain_id,
    }
    analyze_cache_path = job_dir / "analyze_cache.json"
    with analyze_cache_path.open("w") as _cf:
        json.dump(analyze_cache, _cf)

    _email = session.get("user_email", "")
    if _email:
        record_scout_run(
            _email,
            metadata={
                "job_id": job_id,
                "chain": chain_id,
                "uniprot_id": uniprot_id or None,
            },
        )

    return jsonify({
        "download_url": url_for("scout.download", job_id=job_id),
        "download_url_full": url_for("scout.download", job_id=job_id) + "?full=1",
        "pdb_url": url_for("scout.serve_pdb", job_id=job_id),
        "pdb_format": pdb_format,
        "chain": chain_id,
        "epitopes": top3,
        "known_binders": known_binders,
        "ppi_interfaces": ppi_interfaces,
        "uniprot_id": uniprot_id,
        "uniprot_name": uniprot_name,
        "sequence_identity_pct": uniprot_identity_pct,
    }), 200


@scout_bp.route("/pdb/<job_id>", methods=["GET"])
@login_required
def serve_pdb(job_id):
    input_path = _find_input_file(Path("tmp") / job_id)
    if input_path is None:
        return jsonify({"error": "Structure file not found. Please re-upload your file."}), 404
    return send_file(str(input_path), mimetype="chemical/x-pdb")


@scout_bp.route("/download/<job_id>", methods=["GET"])
@login_required
def download(job_id):
    full = request.args.get("full", "0") == "1"
    if full:
        csv_path = Path("tmp") / job_id / "results_annotated.csv"
        fallback_path = Path("tmp") / job_id / "results.csv"
        download_name = "all_patches.csv"
    else:
        csv_path = Path("tmp") / job_id / "epitopes_annotated.csv"
        fallback_path = Path("tmp") / job_id / "epitopes.csv"
        download_name = "top3_epitopes.csv"

    if not csv_path.exists():
        csv_path = fallback_path
    if not csv_path.exists():
        return jsonify({"error": "Results not found. Please run analysis first."}), 404
    return send_file(str(csv_path.resolve()), as_attachment=True, download_name=download_name)


@scout_bp.route("/progress", methods=["GET"])
@login_required
@requires_scout_quota
def progress():
    from flask import stream_with_context  # noqa: PLC0415

    job_id = request.args.get("job_id", "").strip()
    chain_id = request.args.get("chain", "").strip()

    job_dir = Path("tmp") / job_id
    pdb_path = _find_input_file(job_dir) if job_id else None

    if not job_id or not chain_id or pdb_path is None:
        def _error_stream():
            msg = (
                "job_id and chain are required."
                if not job_id or not chain_id
                else "Job not found or expired. Please re-upload your file."
            )
            yield f"data: {json.dumps({'stage': 'error', 'msg': msg})}\n\n"

        return current_app.response_class(
            _error_stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _generate():
        try:
            import gevent  # noqa: PLC0415,F401
            import gevent.queue as gqueue  # noqa: PLC0415
            _use_gevent = True
        except ImportError:
            _use_gevent = False

        def _run_worker(q):
            def callback(stage, pct):
                stage_labels = {
                    "parsing": "Parsing structure\u2026",
                    "sasa": "Calculating solvent accessibility\u2026",
                    "patches": "Clustering surface patches\u2026",
                    "scoring": "Scoring epitope geometry\u2026",
                    "ranking": "Finalising results\u2026",
                }
                q.put({"stage": stage, "pct": pct, "msg": stage_labels.get(stage, stage)})

            try:
                from scout.pipeline import run_pipeline  # noqa: PLC0415
                run_pipeline(pdb_path, chain_id, progress_callback=callback)

                q.put({"stage": "done", "pct": 100, "result": {
                    "download_url": url_for("scout.download", job_id=job_id),
                    "download_url_full": url_for("scout.download", job_id=job_id) + "?full=1",
                    "pdb_url": url_for("scout.serve_pdb", job_id=job_id),
                    "pdb_format": pdb_path.suffix.lstrip("."),
                    "chain": chain_id,
                }})
            except Exception as exc:
                logger.exception("SSE pipeline error for job %s", job_id)
                q.put({"stage": "error", "msg": str(exc)})

        if _use_gevent:
            import gevent  # noqa: PLC0415
            import gevent.queue as gqueue  # noqa: PLC0415
            q = gqueue.Queue()
            gevent.spawn(_run_worker, q)
            while True:
                try:
                    event = q.get(timeout=15)
                except gqueue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break
        else:
            import queue as stdqueue  # noqa: PLC0415
            q = stdqueue.Queue()
            _run_worker(q)
            while not q.empty():
                event = q.get_nowait()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break

    return current_app.response_class(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Feasibility
# ---------------------------------------------------------------------------

@scout_bp.route("/feasibility", methods=["GET"])
@login_required
def feasibility_page():
    job_id = request.args.get("job_id", "")
    epitope_id = request.args.get("epitope_id", "")
    return render_template("scout/feasibility.html", job_id=job_id, epitope_id=epitope_id)


@scout_bp.route("/feasibility/analyze", methods=["POST"])
@login_required
def feasibility_analyze():
    from scout.feasibility import generate_recommendations  # noqa: PLC0415
    from scout.pipeline import run_feasibility_pipeline  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id", "").strip()
    chain_id = data.get("chain", "").strip()

    if not job_id or not chain_id:
        return jsonify({"error": "job_id and chain are required."}), 400

    job_dir = Path("tmp") / job_id
    pdb_path = _find_input_file(job_dir) if job_id else None
    if pdb_path is None:
        return jsonify({"error": "Job not found or expired. Please re-upload."}), 404

    epitope_residues = data.get("epitope_residues", [])
    epitope_id = data.get("epitope_id")

    if not epitope_residues and epitope_id is not None:
        results_csv = job_dir / "results.csv"
        if not results_csv.exists():
            return jsonify({"error": "No Epitope Scout results found for this job. Run epitope analysis first."}), 404

        try:
            epitope_id = int(epitope_id)
            with results_csv.open() as f:
                reader = csv_module.DictReader(f)
                for row in reader:
                    if int(row.get("epitope_id", 0)) == epitope_id:
                        residues_str = row.get("residues", "")
                        for token in residues_str.split(","):
                            token = token.strip()
                            num = re.sub(r"[^0-9\-]", "", token)
                            if num:
                                epitope_residues.append(int(num))
                        break
        except (ValueError, KeyError) as exc:
            return jsonify({"error": f"Failed to parse epitope from results: {exc}"}), 400

    if not epitope_residues:
        return jsonify({"error": "epitope_residues or epitope_id is required."}), 400

    try:
        feasibility_csv = run_feasibility_pipeline(
            pdb_path, chain_id, epitope_residues,
        )
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 422

    result_row = {}
    with feasibility_csv.open() as f:
        reader = csv_module.DictReader(f)
        for row in reader:
            result_row = row
            break

    dimensions = {
        "surface_topology": float(result_row.get("surface_topology", 0)),
        "epitope_rigidity": float(result_row.get("epitope_rigidity", 0)),
        "geometric_access": float(result_row.get("geometric_access", 0)),
        "glycan_risk": float(result_row.get("glycan_risk", 0)),
        "interface_competition": float(result_row.get("interface_competition", 0)),
    }

    composite = float(result_row.get("composite_feasibility", 0))
    tier = result_row.get("tier", "Unknown")
    result = generate_recommendations(dimensions, composite, tier, len(epitope_residues))

    return jsonify({
        "composite_feasibility": composite,
        "tier": result.tier,
        "tier_color": result.tier_color,
        "dimensions": dimensions,
        "dimension_descriptions": result.dimension_descriptions,
        "recommended_approach": result.recommended_approach,
        "recommended_scaffold": result.recommended_scaffold,
        "design_scale_min": result.design_scale_min,
        "design_scale_max": result.design_scale_max,
        "expected_hit_rate": result.expected_hit_rate,
        "hit_rate_citation": result.hit_rate_citation,
        "risk_factors": result.risk_factors,
        "residues": result_row.get("residues", ""),
        "residue_count": int(result_row.get("residue_count", 0)),
        "download_url": url_for("scout.feasibility_download", job_id=job_id),
        "pdb_url": url_for("scout.serve_pdb", job_id=job_id),
        "pdb_format": pdb_path.suffix.lstrip("."),
        "chain": chain_id,
        "known_binder_overlaps": _get_binder_overlaps(job_dir, epitope_residues),
    }), 200


@scout_bp.route("/feasibility/progress", methods=["GET"])
@login_required
def feasibility_progress():
    from flask import stream_with_context  # noqa: PLC0415

    job_id = request.args.get("job_id", "").strip()
    chain_id = request.args.get("chain", "").strip()
    epitope_str = request.args.get("epitope_residues", "").strip()
    epitope_id = request.args.get("epitope_id", "").strip()

    job_dir = Path("tmp") / job_id
    pdb_path = _find_input_file(job_dir) if job_id else None

    if not job_id or not chain_id or pdb_path is None:
        def _error_stream():
            msg = "job_id and chain are required." if not job_id or not chain_id else "Job not found or expired."
            yield f"data: {json.dumps({'stage': 'error', 'msg': msg})}\n\n"
        return current_app.response_class(
            _error_stream(), mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    epitope_residues: list[int] = []
    if epitope_str:
        epitope_residues = [int(x.strip()) for x in epitope_str.split(",") if x.strip().lstrip("-").isdigit()]
    elif epitope_id:
        results_csv = job_dir / "results.csv"
        if results_csv.exists():
            try:
                eid = int(epitope_id)
                with results_csv.open() as f:
                    reader = csv_module.DictReader(f)
                    for row in reader:
                        if int(row.get("epitope_id", 0)) == eid:
                            for token in row.get("residues", "").split(","):
                                num = re.sub(r"[^0-9\-]", "", token.strip())
                                if num:
                                    epitope_residues.append(int(num))
                            break
            except (ValueError, KeyError):
                pass

    if not epitope_residues:
        def _err():
            yield f"data: {json.dumps({'stage': 'error', 'msg': 'No epitope residues specified.'})}\n\n"
        return current_app.response_class(
            _err(), mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _generate():
        try:
            import gevent  # noqa: PLC0415,F401
            import gevent.queue as gqueue  # noqa: PLC0415
            _use_gevent = True
        except ImportError:
            _use_gevent = False

        def _run_worker(q):
            def callback(stage, pct):
                stage_labels = {
                    "parsing": "Parsing structure\u2026",
                    "sasa": "Calculating solvent accessibility\u2026",
                    "bfactor": "Computing rigidity scores\u2026",
                    "topology": "Analyzing surface topology\u2026",
                    "accessibility": "Evaluating geometric accessibility\u2026",
                    "glycan": "Detecting glycosylation sites\u2026",
                    "interfaces": "Detecting protein interfaces\u2026",
                    "scoring": "Computing feasibility score\u2026",
                }
                q.put({"stage": stage, "pct": pct, "msg": stage_labels.get(stage, stage)})

            try:
                from scout.pipeline import run_feasibility_pipeline  # noqa: PLC0415
                run_feasibility_pipeline(pdb_path, chain_id, epitope_residues, progress_callback=callback)
                q.put({"stage": "done", "pct": 100, "result": {
                    "job_id": job_id,
                    "chain": chain_id,
                }})
            except Exception as exc:
                logger.exception("Feasibility SSE error for job %s", job_id)
                q.put({"stage": "error", "msg": str(exc)})

        if _use_gevent:
            import gevent  # noqa: PLC0415
            import gevent.queue as gqueue  # noqa: PLC0415
            q = gqueue.Queue()
            gevent.spawn(_run_worker, q)
            while True:
                try:
                    event = q.get(timeout=15)
                except gqueue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break
        else:
            import queue as stdqueue  # noqa: PLC0415
            q = stdqueue.Queue()
            _run_worker(q)
            while not q.empty():
                event = q.get_nowait()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break

    return current_app.response_class(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@scout_bp.route("/feasibility/download/<job_id>", methods=["GET"])
@login_required
def feasibility_download(job_id):
    csv_path = Path("tmp") / job_id / "feasibility_results.csv"
    if not csv_path.exists():
        return jsonify({"error": "Feasibility results not found. Run analysis first."}), 404

    return send_file(
        str(csv_path.resolve()),
        as_attachment=True,
        download_name=f"feasibility_{job_id[:8]}.csv",
        mimetype="text/csv",
    )


# ---------------------------------------------------------------------------
# Scout -> Tools-hub handoff
# ---------------------------------------------------------------------------

VALID_HANDOFF_TOOLS = {"rfantibody", "bindcraft", "pxdesign", "boltzgen"}


@scout_bp.route("/handoff/tool", methods=["POST"])
@login_required
def handoff_to_tool():
    from scout.handoff import create_handoff, handoff_redirect_url  # noqa: PLC0415

    tool = (request.form.get("tool") or "").strip().lower()
    scout_job_id = (request.form.get("scout_job_id") or "").strip()
    target_chain = (request.form.get("target_chain") or "A").strip() or "A"
    hotspots_raw = (request.form.get("hotspot_residues") or "").strip()
    scout_epitope_id = (request.form.get("scout_epitope_id") or "").strip() or None

    if tool not in VALID_HANDOFF_TOOLS:
        return jsonify({"error": f"Unknown tool: {tool}"}), 400
    if not scout_job_id:
        return jsonify({"error": "scout_job_id is required"}), 400

    hotspots: list[int] = []
    if hotspots_raw:
        try:
            hotspots = [
                int(tok.strip())
                for tok in hotspots_raw.split(",")
                if tok.strip()
            ]
        except ValueError:
            return jsonify({"error": "hotspot_residues must be integers"}), 400
    if not hotspots:
        return jsonify({"error": "At least one hotspot residue is required"}), 400

    email = session.get("user_email", "")
    handoff_id = create_handoff(
        user_email=email,
        scout_job_id=scout_job_id,
        target_chain=target_chain,
        hotspot_residues=hotspots,
        scout_epitope_id=scout_epitope_id,
    )
    if not handoff_id:
        return (
            jsonify({
                "error": (
                    "Could not stage handoff. Make sure the Scout run "
                    "still has its PDB, and try again."
                )
            }),
            500,
        )
    return redirect(handoff_redirect_url(tool, handoff_id))
