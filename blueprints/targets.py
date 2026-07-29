"""Design target routes: upload a structure once, run many tools against it.

Phase 1 of the target-first rework. A target is created, listed, viewed, and
archived here; launching against one still goes through the existing run
create form (``/campaigns/new?target_id=``), which now skips staging and
inherits the target's already-staged path. The multi-tool launch screen is
Phase 2 and replaces the redirect in :func:`target_launch`.

Ownership: every route resolves its target through
``shared.targets.get_target(..., user_id=ctx.user_id)`` BEFORE touching a
storage path. ``copy_input`` / ``download_input`` take ``user_id`` as a path
component and perform no authorization of their own, so that owner-scoped
fetch is the entire tenancy boundary.
"""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)

from shared.auth import login_required
from shared.credits import load_user_context
from shared.idempotency import idempotent
from shared.pdb_intake import resolve_target_upload
from shared.storage import StorageError
from shared.targets import (
    archive_target,
    create_target,
    find_target_by_sha256,
    get_target,
    list_targets_for_user,
    unarchive_target,
)

logger = logging.getLogger(__name__)

targets_bp = Blueprint("targets", __name__)

# Generous: a real epitope is ~15-25 residues and a hotspot set smaller still.
# The cap only exists so a pasted spreadsheet column cannot become a 10k-element
# Postgres array.
_MAX_RESIDUES = 256

# Rows per section on the targets page. The page does not paginate, so this is
# also the point past which a target is reachable only by URL; the template
# says so when a section hits it rather than truncating in silence.
_LIST_LIMIT = 100


def _parse_residue_list(raw: str) -> "tuple[list, str | None]":
    """Parse "32, 45, 58" into ``[32, 45, 58]``.

    Returns ``(residues, error)``. Rejects rather than silently dropping a
    non-numeric entry: a typo'd hotspot that vanishes here would be a target
    that quietly aims somewhere else than the user asked for.
    """
    text = (raw or "").replace(";", ",").strip()
    if not text:
        return [], None
    out: list = []
    for piece in text.replace(",", " ").split():
        try:
            out.append(int(piece))
        except ValueError:
            return [], f"'{piece}' is not a residue number."
    if len(out) > _MAX_RESIDUES:
        return [], f"Too many residues (max {_MAX_RESIDUES})."
    return out, None


@targets_bp.route("/targets", methods=["GET"])
@login_required
def targets_list():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    # Two reads, not one mixed read: the live query is what migration 0039's
    # partial index covers, and putting archived rows in the same capped page
    # would let a user with many archived targets push their live ones off it.
    #
    # Both reads are capped and neither paginates, so a user past the cap has
    # targets this page cannot show. That is disclosed rather than hidden:
    # this section is otherwise the only route to an archived target, so a
    # silent truncation here is indistinguishable from a deleted target.
    #
    # Both reads ask for one row MORE than they render. A page holding exactly
    # _LIST_LIMIT rows is ambiguous -- it is either all of them or the first
    # of many -- so testing len() against the limit itself makes the banner
    # claim there are older targets when there are none. The extra row is the
    # only thing that distinguishes the two, and it is dropped before render.
    live = list_targets_for_user(ctx.user_id, limit=_LIST_LIMIT + 1)
    archived = list_targets_for_user(
        ctx.user_id, archived_only=True, limit=_LIST_LIMIT + 1
    )
    return render_template(
        "targets/list.html",
        targets=live[:_LIST_LIMIT],
        archived=archived[:_LIST_LIMIT],
        targets_capped=len(live) > _LIST_LIMIT,
        archived_capped=len(archived) > _LIST_LIMIT,
        list_limit=_LIST_LIMIT,
    )


@targets_bp.route("/targets/new", methods=["GET"])
@login_required
def target_new():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    return render_template("targets/new.html", pre_fill={})


@targets_bp.route("/targets", methods=["POST"])
@login_required
@idempotent()
def target_create():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    def _err(msg, code=400, duplicate=None):
        return render_template(
            "targets/new.html",
            error=msg,
            duplicate=duplicate,
            pre_fill=request.form.to_dict(),
        ), code

    target_chain = (request.form.get("target_chain") or "").strip()
    hotspots, hs_err = _parse_residue_list(request.form.get("hotspot_residues"))
    if hs_err:
        return _err(f"Hotspot residues: {hs_err}")
    epitope, ep_err = _parse_residue_list(request.form.get("epitope_residues"))
    if ep_err:
        return _err(f"Epitope residues: {ep_err}")

    uploaded = request.files.get("target_pdb")
    if uploaded is None or not uploaded.filename:
        return _err("Upload a target structure (.pdb / .cif).")

    upload, upload_err = resolve_target_upload(
        uploaded, target_chain=target_chain, kind="pdb",
    )
    if upload is None:
        return _err(upload_err or "That structure could not be read.")

    # Offer an existing target with the same content rather than splitting one
    # protein's results across two unlinked targets, which is exactly what the
    # combined table exists to prevent. The user can still insist: posting
    # allow_duplicate=1 creates the second target.
    if not request.form.get("allow_duplicate"):
        existing = find_target_by_sha256(ctx.user_id, upload.sha256)
        if existing is not None:
            return _err(
                "You have already uploaded this structure.", 400, existing,
            )

    try:
        target = create_target(
            user_id=ctx.user_id,
            upload=upload,
            name=request.form.get("name"),
            target_chain=target_chain,
            hotspot_residues=hotspots,
            epitope_residues=epitope,
            notes=request.form.get("notes"),
            source="upload",
        )
    except StorageError as exc:
        return _err(f"Upload failed: {exc}")
    if target is None:
        return _err("Could not save the target. Try again in a moment.")

    return redirect(url_for("targets.target_detail", target_id=target.id))


@targets_bp.route("/targets/<target_id>", methods=["GET"])
@login_required
def target_detail(target_id):
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    target = get_target(target_id, user_id=ctx.user_id)
    if target is None:
        return render_template("404.html"), 404

    from shared import compute_campaigns as cc  # noqa: PLC0415
    # Phase 1 shows this target's COMPUTE-CAMPAIGN runs. Standalone jobs that
    # carry target_id with campaign_id NULL (the `target:` reuse token, and
    # Phase 4's yardstick refolds) are not shown: reading both tables is
    # Phase 3's fan-in. Currently invisible rather than wrong, because no
    # template mints a `target:` token yet. The combined ranked table over all
    # of them is also Phase 3; until then each run links to its own results
    # page.
    #
    # One server-side read filtered on target_id. This previously fetched the
    # target's run ids and then intersected them with the user's 200 most
    # recent campaigns, which is capped over their ENTIRE campaign history: a
    # target whose runs all fell outside that window rendered the empty state,
    # telling the user nothing had ever been run against a target they had paid
    # to run against.
    runs = cc.list_campaigns_for_target(target.id, user_id=ctx.user_id)
    return render_template("targets/detail.html", target=target, runs=runs)


@targets_bp.route("/targets/<target_id>/launch", methods=["GET"])
@login_required
def target_launch(target_id):
    """Launch a run against this target.

    Phase 1 hands off to the existing single-tool create form, which now
    inherits the target's staged structure instead of asking for it again.
    Phase 2 replaces this with the multi-tool launch screen.
    """
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    target = get_target(target_id, user_id=ctx.user_id)
    if target is None:
        return render_template("404.html"), 404
    # An archived target is not launchable anywhere else (the run-create route
    # and the atomic form both reject it), and its structure is excluded from
    # the retention sweeper's protected set, so it may already be gone. Sending
    # the user to a form that will refuse the id is worse than saying so here.
    if target.is_archived:
        return redirect(url_for("targets.target_detail", target_id=target.id))
    return redirect(
        url_for("campaigns.compute_campaign_new", target_id=target.id)
    )


@targets_bp.route("/targets/<target_id>/archive", methods=["POST"])
@login_required
def target_archive(target_id):
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    # Archive only ever sets a timestamp. It must NOT delete the staged
    # structure: _dispatch_chunk re-mints a presigned URL from it on every
    # wave, so removing the object would break every chunk of every run still
    # in flight. See shared.targets.archive_target.
    archive_target(target_id, ctx.user_id)
    return redirect(url_for("targets.targets_list"))


@targets_bp.route("/targets/<target_id>/unarchive", methods=["POST"])
@login_required
def target_unarchive(target_id):
    """Restore an archived target.

    Owner scope is enforced inside ``unarchive_target``'s query (it filters on
    user_id), so an unowned id updates nothing and lands back on the list
    without confirming the id exists. A target that was already live takes the
    same path: nothing was restored, so nothing is claimed.

    ``return_to`` decides where a success goes, because the two callers want
    different things: from the detail page the user restored this target to
    use it, and from the list they are likely restoring several. It is matched
    against one literal and never used as a URL, so it cannot become an open
    redirect.
    """
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    if unarchive_target(target_id, ctx.user_id):
        if request.form.get("return_to") == "list":
            return redirect(url_for("targets.targets_list"))
        return redirect(url_for("targets.target_detail", target_id=target_id))
    return redirect(url_for("targets.targets_list"))
