"""Design targets: one uploaded structure, many runs against it.

A target is the parent object the target-first product hangs off. It is a
FREE organizing object — no money, no quota, no status FSM — deliberately
unlike the retired ``workspaces`` table it superficially resembles. Billing
stays where it is: the prepaid wallet, drained per sub-job hold.

Storage reuses the existing ``tool-inputs`` bucket under
``{user_id}/target-{target_id}/{filename}``. Every run created from a target
DENORMALIZES that path onto ``compute_campaigns.target_storage_path``, which
is what keeps this additive: the campaign driver keeps re-minting its
presigned URL from the campaign column every wave and never learns targets
exist.

Ownership model, mirroring ``shared/compute_campaigns.py``: every read takes
an optional ``user_id`` and every caller that will touch a storage path MUST
pass it. ``copy_input`` / ``download_input`` take ``user_id`` as a path
component, not an authorization check, so resolving a target id to a path
without an owner-scoped fetch is a cross-tenant read.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.credits import get_service_client
from shared.storage import StorageError, upload_input

logger = logging.getLogger(__name__)

_TABLE = "design_targets"

# Matches the campaign name cap so the two forms truncate identically.
_MAX_NAME_LEN = 120

# A target's runs are read back by id for the fan-in. Page rather than
# .limit(): PostgREST clamps both a bare select and .limit() to max_rows
# (1000 in supabase/config.toml), and a clamped read is indistinguishable
# from a complete one at the call site.
_PAGE_SIZE = 500
_MAX_PAGES = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _target_storage_key(target_id: str) -> str:
    """The ``target-{uuid}`` path segment ``upload_input`` interpolates.

    ``upload_input`` runs ``filename`` through ``secure_filename`` but
    interpolates ``user_id`` and ``job_id`` into the object key VERBATIM, and
    ``blueprints/campaigns.py`` already repurposes that slot as a free-text
    namespace. Parsing the id as a UUID here is what stops a
    ``target-../../<other user>`` key from writing outside the owner prefix.
    The id always comes from the database (``gen_random_uuid()``), so this can
    only fire on a caller bug — which is exactly when raising is wanted.
    """
    return f"target-{uuid.UUID(str(target_id))}"


def _clean_int_list(values) -> Optional[list]:
    """Coerce a residue list to ``list[int]``, or None when there is nothing.

    Postgres ``integer[]`` rejects a string element, and these arrive from
    form parsing, so normalize before the insert rather than discovering it
    as a 400 halfway through a launch.
    """
    if not values:
        return None
    out: list = []
    for v in values:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out or None


def _segments_label(segments) -> Optional[str]:  # noqa: ANN001
    """``[("A",236,300),("B",None,None)]`` -> ``"A236-300,B"``, or None."""
    parts = []
    for segment in segments or []:
        try:
            cid, lo, hi = segment
        except (TypeError, ValueError):
            return None
        parts.append(str(cid) if lo is None else f"{cid}{lo}-{hi}")
    return ",".join(parts) or None


def selection_residue_count(
    chain_summary, target_chain: str, segments,  # noqa: ANN001
) -> Optional[int]:
    """UPPER bound on the residues a run will design against, from the summary.

    Works off ``design_targets.chain_summary`` (and the identically-shaped
    ``TargetUpload.chain_summary``), so it costs no download — which is what
    makes a size gate possible on the two campaign routes at all.

    THE BOUND IS AN UPPER BOUND ON PURPOSE. The summary carries a per-chain
    residue COUNT plus min/max resnum, not the resnum list, so for an explicit
    range the true count cannot be recovered — a chain with internal gaps holds
    fewer residues than its span suggests. Every clamp below therefore rounds
    UP. Over-counting costs a user an error message on a run that would have
    fitted; under-counting bills for a run that does not. The in-container
    selection stays the authority, exactly as ``DesignTarget.segment_error``
    documents for its own check.

    ``segments`` empty or None means no contig was declared, i.e. the whole of
    the named chain(s) — the same reading ``_parse_target_input`` gives it.
    Returns None when there is no usable summary, so a caller can tell "fits"
    apart from "cannot say".
    """
    summary = chain_summary or {}
    rows = list(summary.get("chains") or [])
    if not rows:
        return None
    by_id = {r.get("chain_id"): r for r in rows}

    def _whole(cid: str) -> int:
        row = by_id.get(cid) or {}
        return int(row.get("standard_residue_count") or 0)

    if segments:
        total = 0
        for segment in segments:
            try:
                cid, lo, hi = segment
            except (TypeError, ValueError):
                # Unreadable segment: fall back to the whole named chains,
                # which is the larger number and therefore the safe one.
                return selection_residue_count(chain_summary, target_chain, None)
            cid = str(cid)
            if lo is None or hi is None:
                total += _whole(cid)
                continue
            row = by_id.get(cid) or {}
            cmin, cmax = row.get("min_resnum"), row.get("max_resnum")
            span = int(hi) - int(lo) + 1
            if cmin is not None and cmax is not None:
                span = min(int(hi), int(cmax)) - max(int(lo), int(cmin)) + 1
            total += max(0, min(span, _whole(cid)))
        return total

    tokens = [t for t in (target_chain or "").split() if t]
    if not tokens:
        # No chain named either: the whole structure is in play.
        total = summary.get("total_standard_residues")
        return int(total) if total is not None else None
    return sum(_whole(t) for t in tokens)


@dataclass
class DesignTarget:
    """A row of ``public.design_targets``."""

    id: str
    user_id: str
    kind: str = "pdb"
    name: Optional[str] = None
    storage_path: Optional[str] = None
    filename: Optional[str] = None
    content_type: Optional[str] = None
    sha256: Optional[str] = None
    byte_size: Optional[int] = None
    target_chain: Optional[str] = None
    hotspot_residues: list = field(default_factory=list)
    epitope_residues: list = field(default_factory=list)
    chain_summary: Optional[dict] = None
    uniprot_accession: Optional[str] = None
    source: Optional[str] = None
    notes: Optional[str] = None
    archived_at: Optional[str] = None
    created_at: Optional[str] = None
    last_used_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "DesignTarget":
        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            kind=row.get("kind") or "pdb",
            name=row.get("name"),
            storage_path=row.get("storage_path"),
            filename=row.get("filename"),
            content_type=row.get("content_type"),
            sha256=row.get("sha256"),
            byte_size=row.get("byte_size"),
            target_chain=row.get("target_chain"),
            hotspot_residues=list(row.get("hotspot_residues") or []),
            epitope_residues=list(row.get("epitope_residues") or []),
            chain_summary=row.get("chain_summary"),
            uniprot_accession=row.get("uniprot_accession"),
            source=row.get("source"),
            notes=row.get("notes"),
            archived_at=row.get("archived_at"),
            created_at=row.get("created_at"),
            last_used_at=row.get("last_used_at"),
        )

    @property
    def display_name(self) -> str:
        return self.name or self.filename or f"Target {self.id[:8]}"

    @property
    def is_archived(self) -> bool:
        return bool(self.archived_at)

    @property
    def chains(self) -> list:
        """Per-chain summary rows, or [] when the structure was never parsed."""
        summary = self.chain_summary or {}
        return list(summary.get("chains") or [])

    @property
    def residue_count(self) -> Optional[int]:
        summary = self.chain_summary or {}
        return summary.get("total_standard_residues")

    def _chain(self, chain_id: str) -> Optional[dict]:
        for c in self.chains:
            if c.get("chain_id") == chain_id:
                return c
        return None

    def chain_error(self, target_chain: str) -> Optional[str]:
        """Reject a chain that is not in this target, without a download.

        A run launched against a stored target never re-uploads the structure,
        so ``resolve_target_upload`` (and with it ``validate_target_chain``)
        never runs and a typo'd chain would reach the GPU. Rather than
        re-download and re-parse on every launch, this checks the
        ``chain_summary`` persisted at upload time — the same data the
        inspection produced, and enough for both the chain and hotspot-range
        checks.

        Returns None when the chain is fine or when none was given. Mirrors
        ``shared.pdb_inspect.validate_target_chain``, including accepting
        several whitespace-separated ids and being case-sensitive.

        A ``pdb`` target with NO persisted summary fails CLOSED. That state is
        unreachable through the current create route (``resolve_target_upload``
        always inspects a PDB), but ``create_target`` is a plain function that
        will accept any upload, and "no summary" silently meaning "no
        validation" is a trap: one future caller and a target reaches the GPU
        with nothing checked at all. An ``sdf`` target has no protein chains to
        name, so it is exempt.
        """
        if not (target_chain or "").strip():
            return None
        chain_ids = [c.get("chain_id") for c in self.chains]
        if not chain_ids:
            if self.kind == "pdb":
                return (
                    "This target's structure was never inspected, so its "
                    "chains cannot be checked. Re-upload it."
                )
            return None
        for cid in target_chain.split():
            chain = self._chain(cid)
            if chain is None:
                return (
                    f"Target chain '{cid}' is not in this target. "
                    f"Found chain(s): {', '.join(c for c in chain_ids if c)}."
                )
            if not chain.get("standard_residue_count"):
                return (
                    f"Chain '{cid}' has no standard protein residues in this "
                    f"target. It contains "
                    f"{len(chain.get('hetatm_resnames') or [])} ligand record(s)."
                )
        return None

    def hotspot_error(self, target_chain: str, hotspots) -> Optional[str]:  # noqa: ANN001
        """Reject hotspots outside the named chain(s)' residue ranges.

        Same rationale as :meth:`chain_error`: ranges come from the persisted
        summary rather than a re-parse. Returns None when there is nothing to
        check.

        A residue is in range if it falls inside ANY named chain, because
        ``target_chain`` may name several (``"A B"``, which ProteinMPNN-style
        multi-chain design submits and rfdiffusion's validator accepts). Note
        this deliberately does NOT reproduce
        ``shared.pdb_inspect.validate_hotspots``, which passes the whole string
        to ``report.chain()``, gets None for ``"A B"``, and therefore reports
        every hotspot out of range. That is a bug in the older path (filed as
        A18), not a contract worth mirroring — but it does mean the two paths
        disagree on multi-chain targets until it is fixed.
        """
        cids = [c for c in (target_chain or "").split() if c]
        ranges = []
        for cid in cids:
            chain = self._chain(cid)
            if chain is not None and chain.get("min_resnum") is not None:
                ranges.append((cid, chain["min_resnum"], chain["max_resnum"]))
        if not ranges:
            return None
        bad: list = []
        for h in hotspots or []:
            try:
                n = int(h)
            except (TypeError, ValueError):
                bad.append(h)
                continue
            if not any(lo <= n <= hi for _, lo, hi in ranges):
                bad.append(n)
        if not bad:
            return None
        spans = ", ".join(f"{cid} {lo}-{hi}" for cid, lo, hi in ranges)
        return (
            f"Hotspot residue(s) {', '.join(str(b) for b in bad)} are outside "
            f"this target's chain(s): {spans}."
        )

    def segment_error(self, segments) -> Optional[str]:  # noqa: ANN001
        """Reject chain/residue-range segments that this target cannot satisfy.

        Same persisted-summary rationale as :meth:`chain_error` and
        :meth:`hotspot_error`. ``segments`` is a sequence of
        ``(chain_id, lo, hi)``; ``lo``/``hi`` may be None meaning "the whole
        chain", which only needs the chain to exist.

        Deliberately generic rather than proteina-shaped: any adapter that
        declares residue ranges gets the check by returning ``_target_segments``
        from its validator. Returns None when there is nothing to check.

        This is a cheap pre-money filter, NOT the authority — it compares
        against a summary rather than the structure, so it cannot see internal
        gaps. The container re-derives the selection from the real file and
        refuses on an empty one; that is the check that decides correctness.
        """
        for segment in segments or []:
            try:
                cid, lo, hi = segment
            except (TypeError, ValueError):
                continue
            chain = self._chain(cid)
            if chain is None:
                chain_ids = [c.get("chain_id") for c in self.chains]
                return (
                    f"Target chain '{cid}' is not in this target. "
                    f"Found chain(s): {', '.join(c for c in chain_ids if c)}."
                )
            if lo is None or hi is None:
                continue
            cmin, cmax = chain.get("min_resnum"), chain.get("max_resnum")
            if cmin is None or cmax is None:
                continue
            if hi < cmin or lo > cmax:
                return (
                    f"Chain range {cid}{lo}-{hi} does not overlap this "
                    f"target's chain {cid} ({cmin}-{cmax})."
                )
        return None

    def size_error(
        self, tool: str, target_chain: str, segments,  # noqa: ANN001
        binder_max_aa=None,  # noqa: ANN001
    ) -> Optional[str]:
        """Reject a launch whose target is over the tool's size cap.

        THE GAP THIS CLOSES. The per-tool size envelope was enforced only in
        ``preflight_for_tool``, which only ``/tools/<slug>/submit`` calls — and
        that route refuses anything bigger than one container. So the cap never
        guarded a campaign, which is the shape that spends real money (proteina
        opens 4 concurrent shards at ~$12.58 each, inside a ~$15/shard hold
        that covers all of it). Both campaign routes land here.

        Size ONLY, deliberately. The full preflight also applies a min-residue
        floor, gap rules and hotspot rules; switching those on for campaigns
        would change behaviour far beyond the cost hole, and these routes
        cannot run them cheaply anyway — a target-bound launch never downloads
        the structure. Same family as :meth:`chain_error`, :meth:`hotspot_error`
        and :meth:`segment_error`, and it reads the same persisted summary.

        Returns None when there is no summary to judge: a target predating the
        summary column must not be blocked by a check that cannot see it.
        """
        count = selection_residue_count(
            self.chain_summary, target_chain, segments,
        )
        if count is None:
            return None
        from shared.pdb_preflight import size_only_refusal  # noqa: PLC0415
        return size_only_refusal(
            tool, count, binder_max_aa=binder_max_aa,
            selection_label=_segments_label(segments),
        )

    def to_dict(self) -> dict:
        """JSON-friendly view for templates and status endpoints."""
        return {
            "id": self.id,
            "name": self.display_name,
            "kind": self.kind,
            "filename": self.filename,
            "target_chain": self.target_chain,
            "hotspot_residues": list(self.hotspot_residues),
            "epitope_residues": list(self.epitope_residues),
            "residue_count": self.residue_count,
            "chains": [c.get("chain_id") for c in self.chains],
            "archived": self.is_archived,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_target(
    *,
    user_id: str,
    upload=None,  # noqa: ANN001 - shared.pdb_intake.TargetUpload
    name: Optional[str] = None,
    target_chain: Optional[str] = None,
    hotspot_residues=None,  # noqa: ANN001
    epitope_residues=None,  # noqa: ANN001
    uniprot_accession: Optional[str] = None,
    source: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[DesignTarget]:
    """Create a target and stage its structure.

    Insert first, stage second: the storage key needs the row's id, so there
    is no way to stage before the row exists. If staging then fails the row is
    DELETED before returning, so a target never exists without the structure
    it promises — a half-made target would render as a launchable card whose
    every run dies on an empty input URL.

    ``upload`` is a :class:`shared.pdb_intake.TargetUpload` (already inspected,
    chain-checked, and CIF-converted) or None for the curated-benchmark path
    that legitimately has no uploaded structure.

    Returns the target, or None when the insert itself failed. Raises
    :class:`shared.storage.StorageError` when staging failed, so the caller can
    tell "could not save" from "could not upload" and say which.
    """
    client = get_service_client()
    if client is None:
        logger.error("create_target: Supabase service client unavailable.")
        return None

    clean_name: Optional[str] = None
    if isinstance(name, str) and name.strip():
        clean_name = name.strip()[:_MAX_NAME_LEN]

    row = {
        "user_id": user_id,
        "name": clean_name,
        "kind": (getattr(upload, "kind", None) or "pdb"),
        "target_chain": (target_chain or "").strip() or None,
        "hotspot_residues": _clean_int_list(hotspot_residues),
        "epitope_residues": _clean_int_list(epitope_residues),
        "uniprot_accession": (uniprot_accession or "").strip() or None,
        "source": (source or "").strip() or None,
        "notes": (notes or "").strip() or None,
    }
    try:
        response = client.table(_TABLE).insert(row).execute()
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
        target = DesignTarget.from_row(rows[0])
    except Exception:
        logger.error("create_target: insert failed", exc_info=True)
        return None

    if upload is None:
        return target

    try:
        storage_path = upload_input(
            user_id=user_id,
            job_id=_target_storage_key(target.id),
            filename=upload.filename,
            data=upload.data,
            content_type=upload.content_type,
        )
    except (StorageError, ValueError):
        _delete_target_row(target.id)
        raise

    fields = {
        "storage_path": storage_path,
        "filename": upload.filename,
        "content_type": upload.content_type,
        "sha256": upload.sha256,
        "byte_size": len(upload.data),
        "chain_summary": upload.chain_summary,
    }
    if not _update_target(target.id, fields):
        # The bytes are staged but the row does not point at them, so the
        # target is unusable. Roll back rather than leave a card that cannot
        # launch. The orphaned object is swept by the retention cron.
        _delete_target_row(target.id)
        raise StorageError("Could not record the uploaded target. Try again.")

    for key, value in fields.items():
        setattr(target, key, value)
    return target


def get_target(
    target_id: str, *, user_id: Optional[str] = None
) -> Optional[DesignTarget]:
    """Fetch a target by id. Pass ``user_id`` to enforce owner scope.

    Every caller that will resolve this target to a storage path MUST pass
    ``user_id``: ``copy_input``/``download_input`` do no ownership check of
    their own, so an unscoped fetch here is the whole boundary and losing it
    is a cross-tenant structure read.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        query = client.table(_TABLE).select("*").eq("id", target_id)
        if user_id is not None:
            query = query.eq("user_id", user_id)
        response = query.single().execute()
    except Exception:
        # single() raises on zero rows — treat as not found.
        return None
    data = getattr(response, "data", None)
    if not data:
        return None
    return DesignTarget.from_row(data)


# Outcomes of :func:`read_target`. Three, for the reason ``shared/jobs.py`` gives
# beside its ``JOB_READ_*`` constants: ``get_target``'s ``None`` is two unrelated
# facts wearing one hat, and a caller that must act on the difference has no way
# to recover it.
TARGET_READ_OK = "ok"
# The read SUCCEEDED and matched no row: the target does not exist, or it exists
# and is not this caller's. One value, because they are one fact to a caller -- a
# permanent verdict -- and because telling them apart would require reading a row
# the owner scope exists to withhold.
TARGET_READ_ABSENT = "absent"
# We did not manage to look. No service client, or the query raised. Says NOTHING
# about whether the target exists.
TARGET_READ_UNAVAILABLE = "unavailable"

# Every value ``TargetRead.outcome`` may hold, so a typo reaches a raise at
# construction rather than a branch that silently never fires.
TARGET_READ_OUTCOMES = (
    TARGET_READ_OK, TARGET_READ_ABSENT, TARGET_READ_UNAVAILABLE,
)


@dataclass(frozen=True, eq=False)
class TargetRead:
    """A target lookup, plus WHY it came back the way it did.

    The shape of ``shared.jobs.JobRead``, deliberately, INCLUDING the two
    guards below -- both classes carry them and so does
    ``shared.compute_campaigns``'s ``CampaignRead``, because a three-outcome
    value that any one of them can be collapsed on is the same defect three
    times.

    NO TRUTHINESS OF ANY KIND, and that is enforced rather than asserted. This
    docstring used to claim "no ``__bool__``", which was true and was not the
    property it needed: the default ``__bool__`` made every instance
    unconditionally truthy, so ``if read:`` ran on an UNAVAILABLE read exactly
    as it ran on an OK one, and collapsing this back to one bit is the thing the
    class exists to prevent. ``__bool__`` now raises. Branch on ``.outcome`` or
    on ``.unavailable``.

    AND NO EQUALITY WITH AN OUTCOME STRING, for the reason
    ``tools/proteina/_canary_scoring.py::Verdict`` already paid for and wrote
    down: ``frozen=True`` GENERATES an ``__eq__``, so ``read == TARGET_READ_OK``
    returned False SILENTLY on a read that had succeeded -- a hole exactly the
    size of the one ``__bool__`` closes, and one that reads as a clean negative
    rather than as a mistake. ``eq=False`` above turns the generated one off;
    the one below raises on a string and still compares two reads as values.

    WHAT NEITHER GUARD CAN CATCH, stated because the obvious claim is stronger
    than the truth, and on two axes rather than one.
    (1) ``TARGET_READ_OK``, ``CAMPAIGN_READ_OK`` and ``JOB_READ_OK`` are all the
    string ``"ok"``, so ``TargetRead(t, JOB_READ_OK)`` is indistinguishable from
    the right constant at every check available here. The ``__eq__`` guard does
    catch the cross-family COMPARISON, which is the half that can be caught.
    (2) ``__hash__`` below hashes ``(payload id, outcome)`` and NOT the class,
    so ``hash(TargetRead(None, TARGET_READ_ABSENT))`` equals the JobRead and
    CampaignRead ones. Deliberate: equal hashes are not an equality claim,
    ``__eq__`` returns ``NotImplemented`` for a foreign class and Python falls
    back to identity, so a set holding two families keeps both members.
    ``tests/test_jobs.py::test_reads_of_different_families_collide_in_hash_only``
    pins both halves.
    """

    target: Optional[DesignTarget]
    outcome: str

    def __post_init__(self) -> None:
        # OK CARRIES A TARGET AND NOTHING ELSE DOES, checkable rather than
        # conventional. :func:`read_target` cannot violate it, but nothing
        # stopped a second constructor: ``TargetRead(None, TARGET_READ_OK)``
        # built fine and would have handed every caller that reads ``.target``
        # after checking ``.outcome`` a None it has no branch for.
        if self.outcome not in TARGET_READ_OUTCOMES:
            raise ValueError(f"unknown target read outcome {self.outcome!r}")
        if self.outcome == TARGET_READ_OK and self.target is None:
            raise ValueError("TargetRead is OK but carries no target")
        if self.outcome != TARGET_READ_OK and self.target is not None:
            raise ValueError(
                f"TargetRead is {self.outcome} and must carry no target"
            )

    def __bool__(self) -> bool:
        raise TypeError(
            "TargetRead has three outcomes and is not a boolean -- compare "
            ".outcome against TARGET_READ_OK / _ABSENT / _UNAVAILABLE, or read "
            ".unavailable"
        )

    def __eq__(self, other: Any) -> Any:
        if isinstance(other, str):
            raise TypeError(
                "TargetRead is not its outcome string -- write "
                "`read.outcome == TARGET_READ_OK`, not `read == ...`"
            )
        if not isinstance(other, TargetRead):
            return NotImplemented
        return (self.target, self.outcome) == (other.target, other.outcome)

    def __hash__(self) -> int:
        # Defining ``__eq__`` sets ``__hash__ = None``, which would make every
        # TargetRead unhashable on a frozen value type. The target itself is
        # deliberately NOT hashed: ``DesignTarget`` is a plain ``@dataclass``,
        # so its own ``__hash__`` is None and hashing it raises. Its id is what
        # identifies it, and equal reads necessarily agree on that and on the
        # outcome.
        #
        # KEPT RATHER THAN LEFT None: nothing in production puts a read in a set
        # or uses one as a dict key, so this exists for uniformity with the
        # other two read classes rather than for a call site. See
        # ``shared.jobs.JobRead.__hash__``, where the same choice is a change to
        # an existing class rather than a new one.
        return hash((getattr(self.target, "id", None), self.outcome))

    @property
    def unavailable(self) -> bool:
        """True iff the lookup did not complete. NOT "the target is missing".

        Accurate here as written, and NOT interchangeable with
        ``CampaignRead.unavailable``'s wording: this module has no
        ``_campaign_or_none`` equivalent, so :func:`read_target` reports
        UNAVAILABLE on exactly two grounds and both of them are a lookup that
        did not complete -- no service client, or the query raised.
        """
        return self.outcome == TARGET_READ_UNAVAILABLE


def read_target(target_id: str, *, user_id: Optional[str] = None) -> TargetRead:
    """Fetch a target and report which of the three outcomes occurred.

    Same query and same owner-scope semantics as :func:`get_target` -- including
    its rule that every caller which will resolve this target to a storage path
    MUST pass ``user_id``, since ``copy_input``/``download_input`` do no
    ownership check of their own. The whole difference is that a failure to read
    is distinguishable from a row that is not there. Use this wherever the two
    lead to different behaviour -- the lab handoff gate in
    ``blueprints/lab_projects.py`` returns the user to their targets in silence
    on one and refuses the submission with a reason on the other. ``get_target``
    remains the right call everywhere the only question is "do I have the
    target", which is most of this app.

    WHY THIS DOES NOT USE ``.single()``, AND WHY THAT IS THE ENTIRE POINT. The
    reasoning is ``shared.jobs.read_job``'s and is not restated here in different
    words, because a re-worded copy is how two functions that must behave
    identically drift: ``.single()`` RAISES on zero rows, so under it "no such
    target" and "PostgREST timed out" arrive as the same exception and the
    distinction this function exists to make is gone before the ``except`` runs.
    ``.limit(1)`` makes a completed-but-empty read observable as an empty list,
    so the boundary is drawn by whether the call returned at all rather than by
    inspecting an error code.

    ``get_target`` is deliberately NOT reimplemented on top of this, following
    the precedent ``read_job`` set beside ``get_job``: it is called from several
    blueprints, its ``.single()`` chain is what test fakes model, and a shared
    implementation would buy nothing but the risk of changing all of that at
    once.

    ``DesignTarget.from_row`` is called OUTSIDE the ``try``, exactly where
    ``get_target`` calls it and exactly where ``read_job`` calls its own. A row
    this app cannot parse therefore raises out of both functions alike; this
    module has no ``_campaign_or_none`` equivalent and adding one is a separate
    change. That leaves this function's three-outcome contract non-total where
    ``read_campaign``'s is total -- register item A97.
    """
    client = get_service_client()
    if client is None:
        return TargetRead(None, TARGET_READ_UNAVAILABLE)
    try:
        query = client.table(_TABLE).select("*").eq("id", target_id)
        if user_id is not None:
            query = query.eq("user_id", user_id)
        response = query.limit(1).execute()
    except Exception:
        logger.warning("read_target: lookup failed for %s", target_id, exc_info=True)
        return TargetRead(None, TARGET_READ_UNAVAILABLE)
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return TargetRead(None, TARGET_READ_ABSENT)
    return TargetRead(DesignTarget.from_row(rows[0]), TARGET_READ_OK)


def list_targets_for_user(
    user_id: str, *, limit: int = 100, include_archived: bool = False,
    archived_only: bool = False,
) -> list:
    """Return the user's targets, newest first -- by creation for the live
    and both-modes reads, by ARCHIVE time for ``archived_only``.

    ``limit`` is clamped to ``_PAGE_SIZE``. PostgREST would silently clamp a
    larger one to ``max_rows`` anyway, and a caller that thinks it asked for
    everything and got everything is exactly the failure mode this module's
    paged reads exist to avoid. A caller that genuinely needs more than one
    page should page, not raise the limit.

    Three modes, and the default is the one that matters: live only, which is
    the query migration 0039's partial index
    (``WHERE archived_at IS NULL``) was built for. ``archived_only`` is its
    complement and exists so the targets page can offer an un-archive control
    without the archived rows competing for the SAME capped read as the live
    ones -- with one mixed query a user holding many archived targets could
    push their live ones off the end of the page. ``include_archived`` returns
    both. ``archived_only`` wins if both flags are passed.

    Two costs the caller should know rather than discover:

    * The complement is exact as a PREDICATE, but neither list is exhaustive:
      both are capped, and there is no pagination on the targets page. A row
      past the cap of its own list is reachable only by URL. To DETECT that,
      ask for one more row than you intend to render and check whether you
      got it; a full page on its own proves nothing, since "exactly a page"
      and "the first of many" are the same length. Do not instead raise
      ``limit`` until it "covers" the user: this returns at most
      ``_PAGE_SIZE`` however large a limit you pass, so a caller comparing
      the row count against its own limit concludes "not truncated" at
      precisely the point truncation begins. This function has no offset, so
      the rows past the cap are not reachable through it at all.
    * Only the default mode uses the partial index. Both 0039 indexes carry
      ``WHERE archived_at IS NULL``, so ``archived_only`` is a scan and a
      sort, and the targets page pays it on EVERY load including for the
      majority of users who have archived nothing. Free on a small table;
      an index is the fix if it stops being one.
    """
    client = get_service_client()
    if client is None:
        return []
    try:
        query = client.table(_TABLE).select("*").eq("user_id", user_id)
        if archived_only:
            # Ordered by archive time, not creation time. This list exists to
            # undo an archive, so the one just archived has to be at the top;
            # ordering by created_at buries a freshly archived old structure
            # under targets archived months ago, and past the cap drops it
            # entirely, which reads as "archiving deleted it".
            query = query.not_.is_("archived_at", "null").order(
                "archived_at", desc=True
            )
        else:
            if not include_archived:
                query = query.is_("archived_at", "null")
            query = query.order("created_at", desc=True)
        response = query.limit(min(limit, _PAGE_SIZE)).execute()
    except Exception:
        logger.error("list_targets_for_user failed", exc_info=True)
        return []
    return [DesignTarget.from_row(r) for r in (getattr(response, "data", None) or [])]


def find_target_by_sha256(user_id: str, sha256: str) -> Optional[DesignTarget]:
    """The user's most recent live target with this content hash, if any.

    Two uploads of the same structure would otherwise create two unlinked
    targets and split one protein's results across both, which defeats the
    point of the combined table. The caller OFFERS the match; it never
    silently redirects an upload onto an existing target.
    """
    if not sha256:
        return None
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .eq("sha256", sha256)
            .is_("archived_at", "null")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:
        return None
    rows = list(getattr(response, "data", None) or [])
    return DesignTarget.from_row(rows[0]) if rows else None


def archive_target(target_id: str, user_id: str) -> bool:
    """Hide a target from the list. Does NOT touch storage.

    Deliberately not a delete:

    * ``_dispatch_chunk`` re-mints a presigned URL from the staged input on
      EVERY wave, with no cache and no refcount, so removing the object while
      any run is live breaks every chunk that has not dispatched yet. This is
      the reason that holds TODAY.
    * Phase 5 adds ``lab_campaigns.source_target_id``, which will have to be
      ON DELETE CASCADE (its shape CHECK requires the column NOT NULL), at
      which point a hard delete would also destroy paid CRO scoping requests.
      That column does not exist yet; do not cite it as a live constraint.

    Hard deletion happens only via account deletion, where cascading is the
    wanted behaviour.
    """
    client = get_service_client()
    if client is None:
        return False
    try:
        response = (
            client.table(_TABLE)
            .update({"archived_at": _now_iso()})
            .eq("id", target_id)
            .eq("user_id", user_id)
            .execute()
        )
    except Exception:
        logger.error("archive_target failed for %s", target_id, exc_info=True)
        return False
    return bool(getattr(response, "data", None))


def unarchive_target(target_id: str, user_id: str) -> bool:
    """Restore an archived target to the live list. Owner-scoped.

    The inverse of :func:`archive_target`, and it exists because archive was
    one-way from the UI with no route, no function and no control: a mis-click
    permanently removed a structure the user had paid to run against.

    Storage is untouched in both directions, so restoring is only a flag flip.
    One honest caveat, NOT a guarantee this function makes: an archived
    target's object is excluded from the retention sweeper's protected set
    (``cron/purge_old_storage.py::live_target_input_paths``), so a target
    archived long enough could in principle come back with its structure aged
    out. That sweeper is dry-run by default and is not scheduled anywhere
    today, so no live target has ever been swept -- but do not read this
    function as protecting against it. The caller cannot currently tell, and
    the failure would surface as a run that dies in Storage.

    Returns True only when a target that WAS archived is now live. Filtering
    on ``archived_at IS NOT NULL`` as well as ownership is what makes that
    true: without it a live id returns True and the caller reports a restore
    that restored nothing.
    """
    client = get_service_client()
    if client is None:
        return False
    try:
        response = (
            client.table(_TABLE)
            .update({"archived_at": None})
            .eq("id", target_id)
            .eq("user_id", user_id)
            .not_.is_("archived_at", "null")
            .execute()
        )
    except Exception:
        logger.error("unarchive_target failed for %s", target_id, exc_info=True)
        return False
    return bool(getattr(response, "data", None))


def touch_target(target_id: str) -> None:
    """Stamp ``last_used_at``. Best-effort: never fails a launch."""
    _update_target(target_id, {"last_used_at": _now_iso()})


def campaign_ids_for_target(
    target_id: str, *, user_id: Optional[str] = None,
) -> tuple[list, bool]:
    """The target's COMPUTE-CAMPAIGN ids, and whether that list is COMPLETE.

    Returns ``(ids, complete)``. ``complete`` is False when a page read raised
    or the page bound was hit, i.e. whenever the ids below are a prefix of the
    real set rather than the whole of it.

    THE FLAG IS THE POINT OF THE TUPLE. The one caller uses this for a
    membership test that decides whether a paid design is admitted to a wet-lab
    shortlist, and a short list rejects designs that are genuinely the user's.
    Returning the partial list bare -- which this did, from inside its own
    ``except`` -- makes a transient database fault indistinguishable from
    "that design does not belong to this target", and the difference is a
    silently narrowed order (register item A-7). A caller that cannot tell the
    two apart cannot make the safe choice, so the flag is not optional
    information.

    NOT chronological. Pages are ordered by ``id`` so page boundaries are
    stable, and ``compute_campaigns.id`` is ``gen_random_uuid()`` (0034), which
    has no relation to insert order. Sort by ``created_at`` at the caller if
    order matters; the membership test this exists for does not care.

    Not every run: this reads ``compute_campaigns`` only. Migration 0039 also
    puts ``target_id`` on ``tool_jobs``, and the ``target:`` reuse token on the
    atomic tool forms stamps it there, so a standalone run against this target
    exists as a ``tool_jobs`` row with ``campaign_id`` NULL and can never be
    returned here. Phase 3's fan-in has to read both.

    No caller in Phase 1 -- the target page uses
    ``compute_campaigns.list_campaigns_for_target``, which needs whole rows
    rather than ids. Kept for Phase 5's shortlist parentage check, which needs
    exactly this id set and nothing else.

    Owner-scoped when ``user_id`` is given. Paged for the same reason the
    campaign fan-in is: PostgREST clamps an unpaged read to max_rows and the
    truncation is invisible at the call site.
    """
    client = get_service_client()
    if client is None:
        # No client is "we could not look", not "this target has no runs".
        return [], False
    ids: list = []
    start = 0
    for _ in range(_MAX_PAGES):
        try:
            query = (
                client.table("compute_campaigns")
                .select("id")
                .eq("target_id", target_id)
            )
            if user_id is not None:
                query = query.eq("user_id", user_id)
            response = (
                query.order("id").range(start, start + _PAGE_SIZE - 1).execute()
            )
        except Exception:
            logger.error(
                "campaign_ids_for_target failed for %s", target_id, exc_info=True
            )
            return ids, False
        batch = list(getattr(response, "data", None) or [])
        ids += [str(r["id"]) for r in batch if r.get("id")]
        if len(batch) < _PAGE_SIZE:
            # A short page is the end of the data, so this is the ONLY exit
            # that saw the whole set.
            return ids, True
        start += _PAGE_SIZE
    logger.error(
        "campaign_ids_for_target: page bound hit for target %s; "
        "the run list may be incomplete", target_id,
    )
    return ids, False


def target_defaults_for_form(target: Optional[DesignTarget]) -> dict:
    """Prefill values for the run-create form, as form-field names.

    Chain and hotspots are DEFAULTS, not constraints: a multi-chain target may
    want a different epitope per run, so the form may override them and the
    override is persisted on the run.
    """
    if target is None:
        return {}
    out: dict = {"target_id": target.id}
    if target.target_chain:
        out["target_chain"] = target.target_chain
    if target.hotspot_residues:
        out["hotspot_residues"] = ",".join(str(r) for r in target.hotspot_residues)
    if target.epitope_residues:
        # Key is ``epitope``, not ``epitope_residues``: that is the form field
        # iggm reads (tools/iggm/__init__.py parses ``form["epitope"]``), and
        # a prefill dict keyed on anything else silently prefills nothing.
        out["epitope"] = ",".join(str(r) for r in target.epitope_residues)
    if target.display_name:
        out["target_name"] = target.display_name
    # proteina's chain/residue contig. Prefilled from the target's own chain
    # spans so a multi-chain target arrives as "A12-157,B12-157" rather than
    # making the user read the spans off the page and retype them. Still only a
    # default: the field is editable and the run persists what was submitted.
    if target.target_chain and target.chains:
        segments = []
        for cid in target.target_chain.split():
            chain = target._chain(cid)
            if chain and chain.get("min_resnum") is not None:
                segments.append(f"{cid}{chain['min_resnum']}-{chain['max_resnum']}")
        if segments:
            out["proteina__target_input"] = ",".join(segments)
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _update_target(target_id: str, fields: dict) -> bool:
    client = get_service_client()
    if client is None:
        return False
    try:
        response = (
            client.table(_TABLE).update(fields).eq("id", target_id).execute()
        )
    except Exception:
        logger.error("_update_target failed for %s", target_id, exc_info=True)
        return False
    return bool(getattr(response, "data", None))


def _delete_target_row(target_id: str) -> None:
    """Remove a target row that never became usable. Best-effort."""
    client = get_service_client()
    if client is None:
        return
    try:
        client.table(_TABLE).delete().eq("id", target_id).execute()
    except Exception:
        logger.error(
            "could not roll back half-created target %s", target_id, exc_info=True
        )
