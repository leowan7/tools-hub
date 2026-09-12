"""Candidate serialization shared by per-job and campaign exports.

The per-job export routes (``/jobs/<id>/export.{csv,fasta,zip}``) and the new
campaign routes (``/campaigns/<id>/export.*``) produce the same three formats
over a list of candidate records — the only difference is that a campaign's
candidates come from many sub-jobs, so each carries a ``_source_job_id`` tag
(set by ``aggregate_campaign_candidates``) used to fetch its PDB and to
namespace it inside the ZIP. Keeping the serializers here means both paths stay
byte-for-byte identical and a bug is fixed once.

All three serializers take their leading columns, FASTA ids, and ZIP prefixes
from :func:`export_key`, so a row's identity is the same whichever format the
user downloads. That matters most once an export merges several tools over one
target: ``rank`` and ``pdb_key`` are no longer unique on their own there, since
every tool emits a rank 1 and a ``design_1.pdb``.

These are pure functions over candidate dicts; the ZIP builder takes a
``fetch_bytes(job_id, filename)`` callback so this module never imports the
Storage layer (and stays trivially testable).
"""

from __future__ import annotations

import base64
import csv
import io
import zipfile
from typing import Callable, Optional

from shared import metric_glossary as _metric_glossary
from shared import pdb_bfactors as _pdb_bfactors


def _dict_candidates(candidates) -> list:
    return [c for c in (candidates or []) if isinstance(c, dict)]


def _decode_b64(encoded) -> Optional[bytes]:
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except Exception:
        return None


def _safe_arcname(name: str, prefix: str = "") -> str:
    """A ZIP entry name with any traversal (``..``, absolute, backslash)
    stripped, legit sub-directories preserved, optionally namespaced.

    Only ``name`` is cleaned here. ``prefix`` is trusted and must already be
    built from :func:`_safe_component` segments — see :func:`candidates_to_zip`,
    the only caller that passes one.
    """
    cleaned = (name or "").replace("\\", "/").lstrip("/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".", "..")]
    safe = "/".join(parts) or "candidate.pdb"
    return f"{prefix}{safe}" if prefix else safe


def _safe_component(value, fallback: str = "unknown") -> str:
    """One ZIP path segment, with separators and traversal removed.

    A namespace prefix is interpolated into the arcname rather than passed
    through :func:`_safe_arcname`'s cleaner, so anything that reaches a prefix
    has to be made safe here. Today's inputs cannot contain a separator (a tool
    slug comes from the adapter registry and a job id is a uuid), so this is
    defence in depth rather than a fix: the traversal guard belongs with the
    function whose job is traversal safety, not with the caller that happens to
    supply clean values.
    """
    text = str(value or "").replace("\\", "/")
    segment = "".join(p for p in text.split("/") if p not in ("", ".", ".."))
    return segment or fallback


# Provenance the aggregator stamps onto every merged candidate, mapped to the
# column name it exports under. Ordered: these lead the CSV, ahead of pdb_key.
# A key absent from every candidate (a single-job export has no source job; a
# single-campaign export has no tool column) is omitted rather than exported
# blank, so each surface's CSV carries exactly the provenance it actually has.
_PROVENANCE_COLUMNS = (
    ("tool", "_source_tool"),
    ("campaign_id", "_source_campaign_id"),
    ("source_job", "_source_job_id"),
    ("source_chunk", "_source_chunk"),
)


def export_key(cand: dict, i: int) -> dict:
    """The provenance block for one exported row, at global index ``i``.

    ``rank`` is the row's 1-based index in the CANDIDATE LIST handed to the
    serializer. For the CSV that is also its position in the file. For the FASTA
    it is not: :func:`candidates_to_fasta` skips rows carrying no sequence but
    still numbers from the full list, so a target whose best design is a
    backbone with no sequence produces a file whose first record is ``rank2``.
    The numbers are monotonic and unique either way, which is what the ids need
    them for; they are not a count of the file's own records.

    It is NOT a cross-surface identifier. This docstring used to claim it
    "matches the on-screen order of the merged table", which holds only while
    the page is uncapped: the target page ranks with
    :data:`shared.ranking.DEFAULT_LIMIT` and these files rank the whole set, so
    a row the page numbers 298 can be rank 377 here whenever
    :func:`shared.ranking.select_under_cap`'s per-tool floor reserved it from
    beyond the cap, which is the very case the floor exists for. ``source_job``
    plus ``pdb_key`` identify a design across both surfaces; the rank does not.

    The tool's own rank is demoted to
    ``source_rank``: across a merged export those collide (every tool emits a
    rank 1), so using it as the export rank made the CSV look shuffled and made
    "row 7" ambiguous. ``pdb_key`` collides the same way (every tool emits
    ``design_1.pdb``), which is why the source job and chunk travel beside it.

    All three serializers derive from this one function so the CSV, the FASTA
    ids, and the ZIP entry names cannot disagree about where a row came from.
    """
    key: dict = {"rank": i + 1}
    for column, source in _PROVENANCE_COLUMNS:
        value = cand.get(source)
        if value is not None:
            key[column] = value
    key["pdb_key"] = cand.get("pdb_key", "")
    key["source_rank"] = cand.get("rank", i + 1)
    # Whether these numbers were measured at all. The smoke tier fabricates
    # deterministic scores when no model output exists, and stripping the
    # stored verdict field took the only marker saying so out of every CSV --
    # so the file handed over invented values with nothing to distinguish
    # them, while the page beside it said "scores fabricated". PER ROW, and
    # omitted entirely when nothing in the file is a stub, matching how every
    # other column here behaves.
    from shared.score_legends import is_fabricated  # noqa: PLC0415

    if is_fabricated(cand):
        key["provenance"] = "stub (smoke)"
    return key


def _export_keys(cands: list) -> tuple[list[dict], list[str]]:
    """Per-row provenance plus the leading column names actually present."""
    keys = [export_key(c, i) for i, c in enumerate(cands)]
    leading = ["rank"]
    for column, _ in _PROVENANCE_COLUMNS:
        if any(column in k for k in keys):
            leading.append(column)
    leading += ["pdb_key", "source_rank"]
    if any("provenance" in k for k in keys):
        leading.append("provenance")
    return keys, leading


def _basename(pdb_key: str, fallback: str) -> str:
    """Last path segment of a pdb_key. Keys arrive as ``designs/design_0.pdb``
    for most tools; a ``/`` inside a FASTA id terminates parsing in several
    downstream tools, so the prefix is stripped rather than escaped."""
    tail = (pdb_key or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    # Whitespace ends a FASTA id and turns the rest into a free-text
    # description, which would silently truncate the provenance we just added.
    return "_".join(tail.split()) or fallback


# Root-level keys that are never metrics: identity, provenance, bulk payloads.
# Everything else scalar at the root IS exported (see _metric_columns).
# The verdict a pipeline stamped, which is NOT a metric and is no longer true
# of anything. Excluded from BOTH loops in _metric_columns -- a CSV is the one
# copy of a result that leaves this site, and shipping "below threshold" beside
# the measurements would hand a customer a file contradicting the page they
# downloaded it from, on candidates whose bar has since moved.
#
# UNCONDITIONALLY. A first attempt kept the whole column whenever ANY row in
# the file was a smoke stub, so one fabricated design re-opened it for every
# real row beside it and shipped their stale verdict after all -- reachable in
# a cross-job export that happens to include a smoke sub-job. The fabrication
# marker travels as its own per-row provenance column instead (see
# ``export_key``), which is a column the stale word has no way into.
#
# ``passed`` is listed with it. No pipeline in either repo writes one today and
# the helpers that honoured it are gone; the entry is here so a pipeline that
# starts writing one cannot quietly reopen the door. It was dropped from this
# set once, silently, which is the reason for this paragraph.
_STALE_VERDICT_KEYS = frozenset({"filter_status", "passed"})


# Keys no pipeline has a SOURCE for. A column header is a claim that the number
# exists, and these carry that claim while being empty in every row.
#
# ``cluster_id`` is proteina's. It was dropped from the rendered column set on
# 2026-09-10 once it was measured null on 17,024 of 17,024 candidates (four
# length-sweep driver runs, not compute_campaigns rows) with no
# code anywhere that originates a value (see the note on
# ``_SCORE_COLUMNS["cluster_id"]`` in tools/proteina/run_pipeline.py). Removing
# it from the page did NOT remove it from the file: this function derives its
# header from the STORED payload's ``scores`` keys, not from
# ``shared.result_columns``, so /jobs/<id> export.csv, the campaign export and
# the target export all kept emitting an empty column the page had stopped
# showing -- one click apart, contradicting each other. Found by review, not by
# a test; `tests/test_proteina_promises_no_clustering.py` now pins it.
#
# THE FIX HAS TO LIVE HERE rather than in the pipeline. This header is built
# from what is STORED, and stored rows are expected to carry
# ``"cluster_id": null`` inside ``result`` (run_pipeline.py writes the key,
# webhooks/modal.py:549 copies it through) -- expected, not observed: the
# production jobs table has not been read from here.
#
# Scoped by NAME, across every tool, because no RENDERED column list declares
# ``cluster_id`` -- ``shared/result_columns.py`` and all 14
# ``{% set columns %}`` template lines are clean. Two OFFLINE operator exports
# still declare it and are deliberately untouched:
# ``tools/proteina/export_campaign.py``'s SCORE_COLUMNS and
# ``shard_driver.py``'s MANIFEST_COLUMNS.
_UNSOURCED_METRIC_KEYS = frozenset({"cluster_id"})


_NON_METRIC_ROOT_KEYS = frozenset({
    "pdb_key", "name", "rank", "scores",
    "sequence", "binder_sequence", "designed_sequence",
    "pdb_content_b64", "pdb_content", "cif_content_b64",
})


def _metric_columns(cands: list, leading: list[str]) -> list[str]:
    """Metric column names, in first-seen order.

    Reads ``scores`` AND the record root. The designs-shape pipelines (boltz2,
    af2, colabfold, esmfold, iggm, opendde) put every metric at the root and
    have no ``scores`` dict at all — their results templates reshape inline, so
    the screen looked fine while a scores-only export produced a file with the
    row count right and every metric missing.

    Root names are the pipeline's own (``iptm``, not ``ipTM``); mapping those
    onto canonical display names is a per-tool concern and belongs with the
    cross-tool aliasing work, not here. Exporting the real numbers under their
    real names beats exporting nothing.
    """
    # Suppress an unsourced key only WHILE it is unsourced. Keyed on the name
    # alone, a cluster_id that someone finally wires up would be deleted from
    # the file -- header and value -- with nothing failing; review caught that
    # a name-only filter drops a real 7 as readily as a null. Checking the rows
    # means the column comes back by itself the moment it means something.
    #
    # The test is ``_is_metric_value``, NOT ``is not None``, and that is the
    # second fix here: a list / dict / over-long string is not None, so one such
    # row re-opened the header while the writer below refused to print it --
    # an empty column for every row, which is the exact defect this suppression
    # exists to remove. Whatever cannot be printed cannot count as a source.
    def _is_printable_value(v: object) -> bool:
        # A source is a value the writer would actually PRINT.
        # ``_is_metric_value`` says yes to None (a null prints as an
        # empty cell), so calling it alone made every null count as a
        # source and suppressed nothing at all.
        return v is not None and _is_metric_value(v)

    def _scores_of(cand: object) -> dict:
        # ``scores`` is container-authored, and the loops below only ITERATE
        # it, so calling ``.get`` here introduced an AttributeError on a
        # non-dict that the function did not raise before. (It raised other
        # things -- measured on the pre-change code, a str or list `scores`
        # gives ValueError and an int gives TypeError -- so this restores the
        # previous behaviour exactly, it does not make non-dicts safe.)
        raw = cand.get("scores") if isinstance(cand, dict) else None
        return raw if isinstance(raw, dict) else {}

    unsourced = {
        k for k in _UNSOURCED_METRIC_KEYS
        if not any(_is_printable_value(_scores_of(c).get(k))
                   or (isinstance(c, dict) and _is_printable_value(c.get(k)))
                   for c in cands)
    }
    out: list[str] = []
    for cand in cands:
        for k in (cand.get("scores") or {}):
            if (k not in out and k not in leading
                    and k not in _STALE_VERDICT_KEYS
                    and k not in unsourced):
                out.append(k)
    for cand in cands:
        for k, v in cand.items():
            if k in _STALE_VERDICT_KEYS or k in unsourced:
                continue
            if k in out or k in leading or k in _NON_METRIC_ROOT_KEYS:
                continue
            if k.startswith("_"):          # provenance tags
                continue
            if not _is_metric_value(v):
                continue
            out.append(k)
    return out


# No metric is a long string. The cap is insurance against a pipeline putting a
# bulk payload (structure text, a base64 blob) under a key not in the denylist:
# without it one such key would inline megabytes into every CSV row.
_MAX_METRIC_STR = 512


def _is_metric_value(v) -> bool:
    """Scalar, and small enough to belong in a spreadsheet cell."""
    if v is None or isinstance(v, (int, float, bool)):
        return True
    if isinstance(v, str):
        return len(v) <= _MAX_METRIC_STR
    return False                           # lists/dicts (per-residue contacts)


def candidates_to_csv(candidates) -> str:
    """Provenance columns (:func:`export_key`) + every metric found."""
    cands = _dict_candidates(candidates)
    keys, leading = _export_keys(cands)
    all_score_keys = _metric_columns(cands, leading)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=leading + all_score_keys, extrasaction="ignore",
    )
    writer.writeheader()
    for cand, key in zip(cands, keys):
        # Root metrics first, then scores (which win, matching how
        # candidate_metric resolves), then provenance (which wins outright).
        # A key can be scalar on one candidate and a list on another, so the
        # value is re-checked here and not just at column-discovery time.
        row = {
            k: cand[k] for k in all_score_keys
            if k in cand and _is_metric_value(cand[k])
        }
        row.update(cand.get("scores") or {})
        row.update(key)
        # Same scale as the page this was downloaded from. Without this
        # the workflow "read the table, export the CSV, filter > 70"
        # returns an empty file for every tool that stores 0-1, with no
        # hint why -- the page says 88.5 and the CSV says 0.885.
        for col in _metric_glossary.PLDDT_COLUMNS & row.keys():
            scaled = _metric_glossary.plddt_on_100(row[col])
            # Rounded, because 0.8728 * 100 is 86.24000000000001 and the
            # page shows 86.24. Fixing a 100x disagreement and opening a
            # 1e-14 one is not fixing it.
            row[col] = scaled if scaled is None else round(scaled, 2)
        writer.writerow(row)
    return buf.getvalue()


def candidates_to_fasta(candidates, sequences=None) -> str:
    """FASTA body for a job/campaign. Binder-design tools carry a
    ``sequence`` / ``binder_sequence`` per candidate; MPNN's sequence-design
    output arrives as a separate ``sequences`` list (seq + score + recovery).
    Returns ``""`` when there is nothing to write (caller supplies the empty
    message so the download still names sensibly)."""
    lines: list[str] = []
    cands = _dict_candidates(candidates)
    for i, cand in enumerate(cands):
        seq = cand.get("sequence") or cand.get("binder_sequence") or ""
        if not seq:
            continue
        key = export_key(cand, i)
        # rank{global}_{tool}_{job8}_{basename}: unique across a merged export,
        # where the old rank+pdb_key pair was not (every tool emits a rank 1
        # and a design_1.pdb). Segments absent from this export are omitted.
        parts = [f"rank{key['rank']}"]
        if key.get("tool"):
            parts.append(str(key["tool"]))
        if key.get("source_job"):
            parts.append(str(key["source_job"])[:8])
        parts.append(_basename(key["pdb_key"], f"candidate_{i + 1}"))
        lines.append(">" + "_".join(parts))
        for start in range(0, len(seq), 80):
            lines.append(seq[start:start + 80])
    for i, seq_obj in enumerate(sequences or []):
        if not isinstance(seq_obj, dict):
            continue
        seq = seq_obj.get("seq") or ""
        if not seq:
            continue
        header_parts = [f">mpnn_rank{i + 1}"]
        score = seq_obj.get("score")
        recovery = seq_obj.get("recovery")
        if score is not None:
            header_parts.append(f"score={score}")
        if recovery is not None:
            header_parts.append(f"recovery={recovery}")
        lines.append(" ".join(header_parts))
        for start in range(0, len(seq), 80):
            lines.append(seq[start:start + 80])
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _missing_note(missing, written_count: int) -> str:
    """The body of the ``MISSING.txt`` a partial archive carries.

    Says only that the bytes did not arrive. An earlier draft asserted that
    "storage did not return" them, which this function cannot know: a row is
    recorded missing on the strength of the row alone (see the promise test in
    :func:`candidates_to_zip`), and a row whose inline ``pdb_content_b64`` is
    corrupt reaches here without a fetch ever being attempted.
    """
    total = written_count + len(missing)
    lines = [
        f"{len(missing)} of {total} designs could not be retrieved and are "
        f"NOT in this archive.",
        f"The other {written_count} are present.",
        "",
        "The designs below carry a structure in this run's results that could "
        "not be",
        "read when the archive was built.",
        "",
        "Missing:",
    ]
    lines.extend(f"  {name}" for name in missing)
    return "\n".join(lines) + "\n"


def zip_unresolved_message(missing) -> str:
    """Body for the refusal a ZIP route returns when nothing PROMISED resolved.

    Shared by the job, campaign and target routes so one wording covers the
    three buttons that reach this failure.

    STATES NO CAUSE, deliberately, and the history here is worth keeping. Draft
    one blamed Storage ("storage returned none of them"), which this code
    cannot know -- a corrupt inline ``pdb_content_b64`` reaches the refusal
    without a fetch being attempted. Draft two replaced that with a retention
    explanation ("structure files are deleted 30 days after a run"), which is
    false in its MECHANISM: ``cron/purge_old_storage.py`` is the only thing
    that would delete them, its sole non-test caller is the
    ``flask storage:purge-old`` CLI at ``app.py:1086`` (dry-run unless
    ``--apply``), the Procfile runs no cron, and no scheduled workflow invokes
    it. Nothing deletes these objects on a clock today. ``templates/legal/``
    is more careful than draft two was, saying outputs "may be" deleted after
    thirty days.

    Two false causes in two drafts is the argument for naming none. The route
    knows the bytes did not arrive; it does not know why, and a customer acts
    on the same advice either way.
    """
    return (
        f"None of the {len(missing)} structure files in this export could be "
        f"retrieved,\n"
        "so no archive was sent rather than sending you an empty one.\n"
        "\n"
        "The scores on the page and the CSV and FASTA exports do not depend on "
        "these\n"
        "files and are unaffected.\n"
        "\n"
        "Trying again is worth doing. If it keeps failing, the structure files "
        "for\n"
        "this run may no longer be available -- the run and its scores stay "
        "either\n"
        "way.\n"
    )


def candidates_to_zip(
    candidates,
    fetch_bytes: Callable[[str, str], Optional[bytes]],
    *,
    default_job_id: Optional[str] = None,
    namespace: bool = False,
    report: Optional[dict] = None,
) -> bytes:
    """Bundle candidate PDBs into a ZIP (bytes).

    Per candidate, try inline ``pdb_content_b64`` first, then
    ``fetch_bytes(source_job_id, pdb_key)`` (Storage). ``source_job_id`` is the
    candidate's ``_source_job_id`` (campaign merge) or ``default_job_id``
    (single job). Candidates that resolve via neither path are skipped rather
    than failing the archive.

    Skipping splits in two, and the caller needs the difference. A row that
    references no structure at all promised nothing, and an archive without it
    is a complete answer. A row that DOES carry a ``pdb_key`` or inline
    ``pdb_content_b64`` and still does not resolve is a design the surface
    offered and this file does not contain. Only the second kind is recorded
    as missing.

    ``report``, when passed, is filled with ``written`` and ``missing`` lists
    of arcnames so the route can answer a total failure with an error status
    instead of a 200 carrying an empty archive. A partial archive additionally
    names the absent designs in a ``MISSING.txt`` entry. The other channel a
    download has is the filename, which the routes already use for this class
    of caveat (``blueprints/targets.py`` marks a capped or partial export
    there); the two are complementary, since a filename cannot name each
    absent design.

    With ``namespace=True`` each entry is prefixed so identically-named designs
    from different sources do not collide. Which prefix depends on the
    provenance the rows actually carry:

    * ``<tool>/<job8>/`` when the row carries ``_source_tool``, which only the
      TARGET aggregate stamps. Chunk index is not enough there: every campaign
      starts at chunk 0, so a bindcraft and a boltzgen ``chunk000/design_1.pdb``
      would be one arcname and one design would silently overwrite the other.
      The job id rather than the chunk is the second segment because two
      campaigns of the SAME tool on one target both have a chunk 0 too.
    * ``chunk###/`` or ``<job8>/`` otherwise, which is every campaign and
      single-job export. Gating on ``_source_tool`` is what keeps those
      byte-identical to what they produced before that branch existed -- for
      a COMPLETE archive. A partial one gains a ``MISSING.txt`` member it did
      not have before, so the guarantee is now about the structure members
      rather than the whole file; each archived design is still byte-for-byte
      what it was.
    """
    buf = io.BytesIO()
    written = []
    missing = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, cand in enumerate(_dict_candidates(candidates)):
            key = export_key(cand, i)
            # Coerced ONCE, here, because this value has two consumers and both
            # of them break on a non-string. pdb_key is whatever the container
            # wrote into job.result, and its type "is not ours to guarantee" --
            # templates/components/candidate_table.html coerces it at its own
            # definition for that reason, and commit fadbe24 records the shape
            # 500-ing the results page.
            #   _safe_arcname  -> AttributeError on .replace (TypeError for
            #                     bytes, whose .replace wants bytes args)
            #   fetch_bytes    -> shared/storage.py::download_output computes
            #                     _output_object_path OUTSIDE its try, so
            #                     posixpath.basename raises TypeError, which
            #                     the routes' _fetch does not catch (it catches
            #                     StorageError) and the whole export 500s.
            # An earlier draft coerced only the arcname and left the fetch
            # reading key["pdb_key"] raw, so the Storage-backed row -- the one
            # the coercion was added for -- still 500'd.
            lookup_key = str(key["pdb_key"]) if key["pdb_key"] else ""
            pdb_key = lookup_key or f"candidate_{i + 1}.pdb"
            job_id = key.get("source_job") or default_job_id
            # The prefix is computed before the bytes are resolved because a
            # design that does NOT resolve is reported by the same arcname it
            # would have been archived under. In a merged target export the
            # bare pdb_key does not identify a row -- every tool emits a
            # design_1.pdb -- so an unprefixed name in MISSING.txt would not
            # say which design is absent.
            prefix = ""
            if namespace:
                tool = key.get("tool")
                chunk = key.get("source_chunk")
                if tool:
                    tool_seg = _safe_component(tool, "unknown-tool")
                    job_seg = _safe_component(str(job_id or "")[:8], "unknown-job")
                    prefix = f"{tool_seg}/{job_seg}/"
                elif chunk is not None:
                    prefix = f"chunk{int(chunk):03d}/"
                elif job_id:
                    prefix = f"{str(job_id)[:8]}/"
            arcname = _safe_arcname(pdb_key, prefix)
            data = _decode_b64(cand.get("pdb_content_b64"))
            if data is None and job_id and lookup_key:
                data = fetch_bytes(job_id, lookup_key)
            if data is None:
                # Read the row, not the archive: a structureless row is not a
                # miss, and the archive it produces is byte-identical to the
                # one a total failure produces. Two tests hold the two halves
                # of that apart, and they are not interchangeable:
                # tests/test_target_export.py::
                # test_an_owned_but_empty_target_exports_an_empty_file_not_a_404
                # covers ZERO rows (a target whose runs have not returned yet),
                # while tests/test_export_zip_unresolved.py::
                # test_a_structureless_row_is_not_a_failure_and_still_exports_200
                # covers a row that is present and references no structure.
                if cand.get("pdb_content_b64") or key["pdb_key"]:
                    missing.append(arcname)
                continue
            # One conversion covers the job, campaign and target ZIP
            # routes, which all come through here. Same whole-file gate
            # as every other download: a structure that is not a
            # fractional confidence is archived untouched.
            data = _pdb_bfactors.bfactors_on_100_bytes(data)
            zf.writestr(arcname, data)
            written.append(arcname)
        # Written only for a PARTIAL archive, and BOTH halves of that are
        # load-bearing. With nothing written the route refuses the download,
        # so a note would be the archive's only member; with nothing missing
        # there is nothing to say. Dropping either half is caught by
        # tests/test_export_zip_unresolved.py::
        # test_the_note_is_written_only_when_the_archive_is_really_partial.
        if missing and written:
            zf.writestr("MISSING.txt", _missing_note(missing, len(written)))
    if report is not None:
        report["written"] = written
        report["missing"] = missing
    buf.seek(0)
    return buf.read()
