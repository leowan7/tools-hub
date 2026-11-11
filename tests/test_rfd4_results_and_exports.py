"""rfd4's results page and its exports (RFD4-Proteina step 5).

HOW THE PARITY GATE IS EXERCISED. rfd4 is in the adapter registry as of
step 7A: ``tools/rfd4/__init__.py`` calls ``register(adapter)`` and app.py
imports the package. The shared parity driver in
``tests/test_export_page_parity.py`` resolves a tool's partial through
``tools.base.get`` (``tests/test_worked_examples.py:190``), which answers for
rfd4 now. These tests still reach the adapter through this file's own
``rfd4_registered`` fixture rather than that driver's ``TABLE_TOOLS`` tuple --
the fixture READS the live registry where it used to write to it, and folding
rfd4 into TABLE_TOOLS is a change to a guard this step does not otherwise
touch. The page-vs-CSV assertion below is still the same assertion the other
eleven tools get, now run against the adapter the app itself routes to.

The structure assertions read the committed rung-4 GPU run,
``docs/scoping/rung4-probe/gpu_result_20261103T114437.json`` (rc=0), so they are
pinned to real upstream output rather than a hand-written CIF.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_export_page_parity import JOB_ID, _num, _page_rows, _stored
from tests.test_worked_examples import _render_partial, tools_app  # noqa: F401

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "docs/scoping/rung4-probe/gpu_result_20261103T114437.json"


@pytest.fixture
def rfd4_registered():
    """The rfd4 adapter as the running app routes to it.

    Read through ``import app`` because that is how the running app fills
    ``tools.base._REGISTRY``. The registry is process-global and other test
    modules import tools.rfd4 directly, so this fixture cannot be relied on
    to notice a missing app.py import; the source-level guard
    tests/test_tool_categories.py::test_app_imports_every_registering_tool_package
    is what does, measured by deleting the line.
    """
    import app  # noqa: F401, PLC0415  (populates tools.base._REGISTRY)

    from tools.base import get

    adapter = get("rfd4")
    assert adapter is not None, (
        "rfd4 is not in the registry -- register(adapter) is missing from "
        "tools/rfd4/__init__.py"
    )
    return adapter


def _probe_cifs() -> dict[str, str]:
    """Every design CIF the committed rung-4 run wrote, keyed by file stem."""
    files = json.loads(PROBE.read_text())["files"]
    return {
        Path(path).stem: text
        for path, text in files.items()
        if path.endswith("_generated.cif")
    }


def _example() -> dict:
    return json.loads((REPO / "tools/rfd4/example/result.json").read_text())


def test_the_probe_still_holds_the_eight_design_cifs():
    """Otherwise every structure assertion below passes over an empty set."""
    cifs = _probe_cifs()
    assert len(cifs) == 8, sorted(cifs)


def test_csv_rows_are_the_page_rows_in_page_order(tools_app, rfd4_registered):
    """The step-5 export parity gate, run the way the eleven registered tools
    get it in tests/test_export_page_parity.py."""
    from shared.exports import candidates_to_csv
    from shared.jobs import page_ordered_records

    flask_app, _ = tools_app
    result = _example()
    page = _page_rows(_render_partial(
        flask_app, "rfd4", job_id=JOB_ID, example=False, result=result))
    assert page, "rfd4: the page rendered no candidate rows"

    stored = _stored(result)
    exported = page_ordered_records("rfd4", result)
    # Identity, not equality: two designs can share every value.
    export_refs = [next(i for i, s in enumerate(stored) if s is r) for r in exported]
    assert export_refs == [p["ref"] for p in page]

    csv_rows = list(csv.DictReader(io.StringIO(candidates_to_csv(exported))))
    assert len(csv_rows) == len(page)
    for n, (p, c) in enumerate(zip(page, csv_rows), start=1):
        assert c["rank"] == str(n)
        csv_nums = [x for x in map(_num, c.values()) if x is not None]
        for col, val in p["cells"].items():
            want = _num(val)
            if want is None:
                continue
            assert any(math.isclose(want, x, rel_tol=1e-3, abs_tol=1e-3)
                       for x in csv_nums), f"row {n}: {col}={val} not in CSV {c}"


def test_the_page_offers_the_cif_extension_not_pdb(tools_app, rfd4_registered):
    """rfd4 delivers mmCIF. The download cell derives its label from the key's
    own extension (templates/components/candidate_table.html), so a page that
    advertised ``.pdb`` would be mislabelling every file."""
    flask_app, _ = tools_app
    html = _render_partial(
        flask_app, "rfd4", job_id=JOB_ID, example=False, result=_example())
    labels = set(re.findall(
        r'class="btn-secondary cand-pdb-download"[^>]*>\.(\w+)<', html))
    assert labels == {"cif"}, labels


def _render_with_inputs(flask_app, adapter, *, result: dict, inputs: dict) -> str:
    """``_render_partial`` with the job's ``inputs`` filled in.

    The shared helper stubs ``inputs`` as ``{}`` (tests/test_worked_examples.py:133)
    and takes no override, and the two assertions below are precisely about the
    inputs fallbacks, so the stub is rebuilt here rather than widening a helper
    eleven other tools share. An object, not a dict, because
    shared/jobs.py::recovered_total reads ``job.inputs`` as an ATTRIBUTE and
    swallows the AttributeError a dict raises — the stub's shape decides
    whether this file tests the fallback or a silent zero.
    """
    job = SimpleNamespace(id=JOB_ID, status="succeeded", tool="rfd4",
                          inputs=inputs, result=result)
    with flask_app.test_request_context("/tools/rfd4"):
        return flask_app.jinja_env.get_template(adapter.results_partial).render(
            job=job, example=False, send_target_tools=[])


def _recovered_result(rows: int) -> dict:
    """What a recovered / wallet-stopped / cancel-kept rfd4 run stores.

    Every one of those writers replaces the result wholesale and keeps no root
    key of this tool's own: shared/job_recovery.py:480-484,
    shared/jobs.py:2093-2100 and shared/jobs.py:2299-2303.
    """
    designs = _example()["designs"][:rows]
    return {
        "candidates": [
            {"rank": d["rank"], "pdb_key": d["pdb_key"], "scores": {}} for d in designs
        ],
        "candidate_count": rows,
        "partial": True,
    }


def test_a_recovered_run_still_says_which_chain_is_the_binder(
        tools_app, rfd4_registered):
    """The chain line is the page's ONLY statement of which chain to keep, and
    test_the_designed_chain_is_not_the_longest_chain proves length cannot
    substitute for it. A recovered result carries no ``target_chain``, so it
    comes from the run's inputs instead.

    Mutation-proven 2026-11-04: with the partial's expression reverted to
    ``output.get('target_chain')`` alone, this test FAILS; restored, it passes.
    """
    flask_app, _ = tools_app
    html = _render_with_inputs(
        flask_app, rfd4_registered,
        result=_recovered_result(2),
        inputs={"target_chain": "A", "nsamples": 8,
                "_progress": {"designs_completed": 2, "designs_total": 8}})
    text = _page_text(html)
    assert "Target chain A" in text
    assert "the chain that is not A" in text


def _inputs(**extra) -> dict:
    """A job's inputs in the shape the adapter stores them.

    ``nsamples`` is tools/rfd4/__init__.py:200 and ``parameters.n_designs_total``
    is :209 — both the accepted design count, the latter the key the wallet
    scales the charge on. There is no ``num_designs`` key in inputs: :176 is
    the FORM field, read once and not kept under that name.
    """
    return {"target_chain": "A", "nsamples": 8,
            "parameters": {"n_designs_total": 8}} | extra


def test_a_lossy_run_says_how_many_designs_are_missing(tools_app, rfd4_registered):
    """M is the submitted count, so a shortfall shows however the designs were
    lost — including a generation shortfall, where the result root's
    ``designs_total`` is itself only ``len(design_cifs)``
    (tools/rfd4/run_pipeline.py:1290) and would render "6 / 6".

    ``requested_designs`` cannot supply it either: no _DESIGN_PARAM_KEY entry
    for rfd4, so it returns None and results_shell.html:49 drops the line.

    Mutation-proven 2026-11-04, this test failing on each revert and passing
    when restored: ``requested=requested_designs(job)`` fails at the first
    assertion (None drops the line outright), and dropping the ``submitted``
    term from the total fails at the second, rendering "6 / 6".
    """
    flask_app, _ = tools_app

    # Lost between CIF and delivery: eight generated, five uploaded.
    full = _example() | {"designs": _example()["designs"][:5]}
    html = _render_with_inputs(
        flask_app, rfd4_registered, result=full, inputs=_inputs())
    assert "5 / 8" in _page_text(html)

    # Generation shortfall: six CIFs written, all six delivered, billed for 8.
    short = _example() | {"designs": _example()["designs"][:6],
                          "designs_total": 6, "designs_completed": 6}
    html = _render_with_inputs(
        flask_app, rfd4_registered, result=short, inputs=_inputs())
    text = _page_text(html)
    assert "6 / 8" in text and "6 / 6" not in text, text[:400]

    # No root keys left at all.
    html = _render_with_inputs(
        flask_app, rfd4_registered,
        result=_recovered_result(2),
        inputs=_inputs(_progress={"designs_completed": 2, "designs_total": 8}))
    assert "2 / 8" in _page_text(html)


def _page_text(html: str) -> str:
    """``html`` with tags and runs of whitespace collapsed, so an assertion on
    "5 / 8" is not defeated by the markup inside the <strong>."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))


def test_the_cif_export_path_reads_both_chains(tools_app):
    """``structure_chain_sequences`` sniffs ``data_`` and parses mmCIF, so the
    sequence columns carry a chain for the target AND the binder on real
    upstream output."""
    from shared.exports import structure_chain_sequences

    for stem, text in sorted(_probe_cifs().items()):
        chains = structure_chain_sequences(text.encode("utf-8"))
        assert chains is not None, f"{stem}: did not parse"
        assert {cid for cid, _ in chains} == {"A", "B"}, f"{stem}: {chains}"
        assert all(len(seq) > 20 for _, seq in chains), f"{stem}: {chains}"


def test_the_designed_chain_is_not_the_longest_chain(tools_app):
    """A length rule would pick the wrong chain on real output, so nothing may
    reintroduce one: in this run design 4's binder is LONGER than the target.

    The target is chain A in all eight (115 aa, the uploaded PDL1 chain); the
    binder is chain B, which upstream's own ``per_example_metrics.csv`` names as
    ``chain_of_interest = B_1``. Selection belongs to ``target_chain`` at the
    result root — see templates/tools/rfd4_results.html.
    """
    from shared.exports import structure_chain_sequences

    lengths = {
        stem: {cid: len(seq) for cid, seq in
               structure_chain_sequences(text.encode("utf-8")) or []}
        for stem, text in _probe_cifs().items()
    }
    assert {d["A"] for d in lengths.values()} == {115}, lengths
    longest_is_target = [s for s, d in lengths.items() if d["A"] > d["B"]]
    assert len(longest_is_target) == 7, lengths
    assert lengths["pdl1_binder_4_generated"]["B"] > 115, lengths


def _probe_fetcher():
    """``fetch_bytes(job_id, pdb_key)`` serving the committed rung-4 CIFs.

    The example result stores a ``pdb_key`` and no inline bytes (that is what
    ``_upload_design`` writes), so every structure read in this file goes
    through a fetcher. Keys are the upload's ``{rank:03d}_{stem}.cif``
    (tools/rfd4/run_pipeline.py::_upload_design), so the stem is the key
    without its rank prefix and extension.
    """
    cifs = _probe_cifs()

    def fetch(job_id: str, key: str) -> bytes | None:
        stem = Path(key).stem.split("_", 1)[1]
        text = cifs.get(stem)
        return text.encode("utf-8") if text else None

    return fetch


def _csv_of(**kwargs) -> list[dict]:
    from shared.exports import candidates_to_csv
    from shared.jobs import page_ordered_records

    body = candidates_to_csv(
        page_ordered_records("rfd4", _example()),
        fetch_bytes=_probe_fetcher(), default_job_id=JOB_ID, **kwargs)
    return list(csv.DictReader(io.StringIO(body)))


def _protparam(seq: str) -> tuple[float, float]:
    from Bio.SeqUtils.ProtParam import ProteinAnalysis

    a = ProteinAnalysis(seq)
    return round(a.gravy(), 3), round(a.instability_index(), 2)


def test_the_developability_pair_is_the_designed_chain_not_the_target():
    """The two sequence terms §7C.6 item 4 keeps for release 1, computed for
    the binder only. Measured against Biopython directly on chain B of each
    probe CIF, and asserted DIFFERENT from chain A's, so a regression that
    scored the target instead would fail here rather than pass on a number
    that merely exists."""
    rows = _csv_of(target_chain="A")
    assert len(rows) == 8
    chains = {stem: dict(_chain_seqs(text))
              for stem, text in _probe_cifs().items()}
    for row in rows:
        stem = Path(row["pdb_key"]).stem.split("_", 1)[1]
        want = _protparam(chains[stem]["B"])
        other = _protparam(chains[stem]["A"])
        got = (float(row["gravy"]), float(row["instability_index"]))
        assert got == want, (stem, got, want)
        assert got != other, stem


def _chain_seqs(text: str):
    from shared.exports import structure_chain_sequences

    return structure_chain_sequences(text.encode("utf-8")) or []


def test_no_target_chain_means_no_developability_columns():
    """Cited by shared/exports.py::_add_chain_columns. Every other tool's
    export passes no target chain, so the pair must not appear without it.
    What this asserts is the two columns' ABSENCE and the chain columns'
    equality across the two calls, not byte-identity with the pre-change
    file: nothing here holds a copy of that file to compare against."""
    without = _csv_of()
    assert without, "no rows, so the comparison below proves nothing"
    for row in without:
        assert "gravy" not in row and "instability_index" not in row, row
    # The chain columns themselves are untouched by the argument.
    with_chain = _csv_of(target_chain="A")
    for a, b in zip(without, with_chain):
        assert a["sequence_chainA"] == b["sequence_chainA"]
        assert a["sequence_chainB"] == b["sequence_chainB"]


def test_the_developability_columns_are_not_a_pass_fail_bar():
    """Cited by shared/exports.py::_DEV_COLUMNS. §7C.6 warns against selling
    the two surviving sequence terms as "developability", and a name in
    GATE_COLUMNS would make a verdict out of one."""
    from shared import score_legends
    from shared.exports import _DEV_COLUMNS, _is_reserved_seq_name

    for tool, cols in score_legends.GATE_COLUMNS.items():
        assert not (set(cols) & set(_DEV_COLUMNS)), tool
    row = {"gravy": -0.9, "instability_index": 60.0}
    assert score_legends.judge("rfd4", row).verdict == "unjudged"
    # And no tool's own scores key can land in these columns.
    assert all(_is_reserved_seq_name(c) for c in _DEV_COLUMNS)


def test_the_fasta_marks_the_binder_chain():
    """Cited by shared/exports.py::candidates_to_fasta and
    :func:`_chain_records`. The page enables the FASTA button for rfd4 on
    ``pdb_key`` alone (templates/components/candidate_table.html:455), so the
    server derives both chains from the structure; unmarked, the file is two
    sequences per design with nothing saying which to order."""
    from shared.exports import candidates_to_fasta
    from shared.jobs import page_ordered_records

    rows = page_ordered_records("rfd4", _example())
    marked = candidates_to_fasta(
        rows, tool="rfd4", fetch_bytes=_probe_fetcher(),
        default_job_id=JOB_ID, target_chain="A")
    heads = [ln for ln in marked.splitlines() if ln.startswith(">")]
    assert len(heads) == 16, heads
    assert sum("_chainA [target chain]" in h for h in heads) == 8, heads
    assert sum("_chainB [designed chain]" in h for h in heads) == 8, heads

    plain = candidates_to_fasta(
        rows, tool="rfd4", fetch_bytes=_probe_fetcher(), default_job_id=JOB_ID)
    assert "[target chain]" not in plain and "[designed chain]" not in plain
    # Same records, same order, same sequences -- only the markers differ.
    assert [ln for ln in plain.splitlines() if not ln.startswith(">")] == \
           [ln for ln in marked.splitlines() if not ln.startswith(">")]
def _auth_relabelled_cifs() -> dict[str, str]:
    """The probe CIFs with ``auth_asym_id`` A/B rewritten to H/L and
    ``label_asym_id`` left alone, so the two ids DIFFER.

    What the real probe cannot show: in it the two ids agree (target A, binder
    B both ways), the degenerate case. RCSB and most cropping tools reassign
    label_asym_id from A while keeping the author letters, so a CIF upload can
    reach rfd4 with auth != label; the pipeline stores both
    (``target_chain`` = author, ``target_asym_id`` = label,
    tools/rfd4/run_pipeline.py:1294-1296) and the export has to match on the
    one the delivered file reads back as.
    """
    return {stem: _relabel_auth(text, {"A": "H", "B": "L"})
            for stem, text in _probe_cifs().items()}


def _relabel_auth(text: str, mapping: dict[str, str]) -> str:
    """One probe CIF with its ``auth_asym_id`` letters remapped and its
    ``label_asym_id`` left alone, so the two ids differ."""
    head = [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith("_atom_site.")]
    ia, il = (head.index("_atom_site.auth_asym_id"),
              head.index("_atom_site.label_asym_id"))
    assert ia != il, head
    lines = []
    for line in text.splitlines():
        if line.startswith(("ATOM ", "HETATM")):
            fields = line.split()
            fields[ia] = mapping.get(fields[ia], fields[ia])
            line = " ".join(fields)
        lines.append(line)
    return "\n".join(lines) + "\n"


def test_the_matching_key_is_the_author_chain_id_not_the_label_asym_id():
    """``target_chain`` is the AUTHOR id, and that is the id this export must
    match on, because Biopython's MMCIFParser keys chains by ``auth_asym_id``
    (measured here, not assumed: the relabelled file reads back as H/L while
    its label_asym_id is still A/B).

    Pins the choice against the plausible-looking swap to ``target_asym_id``
    (the label id, also on the result root): under that swap the author letter
    would match no delivered chain, two chains would look designed, and the
    CSV would silently lose the pair -- the ``len(designed) != 1`` branch of
    shared/exports.py::_add_chain_columns. The other developability tests here
    pass ``target_chain="A"`` against a probe whose two ids agree, so they
    cannot tell the two apart and this one is the only check that can.
    """
    from shared.exports import candidates_to_csv, structure_chain_sequences
    from shared.jobs import page_ordered_records

    cifs = _auth_relabelled_cifs()
    read_back = {stem: dict(structure_chain_sequences(t.encode("utf-8")) or [])
                 for stem, t in cifs.items()}
    assert all(set(c) == {"H", "L"} for c in read_back.values()), read_back

    def fetch(job_id: str, key: str) -> bytes | None:
        text = cifs.get(Path(key).stem.split("_", 1)[1])
        return text.encode("utf-8") if text else None

    def rows_for(target_chain: str) -> list[dict]:
        body = candidates_to_csv(
            page_ordered_records("rfd4", _example()), fetch_bytes=fetch,
            default_job_id=JOB_ID, target_chain=target_chain)
        return list(csv.DictReader(io.StringIO(body)))

    author = rows_for("H")
    assert len(author) == 8
    for row in author:
        stem = Path(row["pdb_key"]).stem.split("_", 1)[1]
        want = _protparam(read_back[stem]["L"])
        assert (float(row["gravy"]), float(row["instability_index"])) == want, stem

    # The label id names no chain of the delivered file, so nothing is scored.
    for row in rows_for("A"):
        assert "gravy" not in row and "instability_index" not in row, row
def test_the_fasta_marks_nothing_when_the_target_chain_is_absent():
    """Cited by shared/exports.py::_chain_records.

    The guard that keeps the FASTA from calling the operator's own upload a
    design. With the run's target chain matching NO chain of the delivered
    file, every chain would otherwise fall to the else branch and be stamped
    "[designed chain]". The CSV already fails safe in this state through its
    ``len(designed) != 1`` skip, and this asserts the two halves now agree:
    no marker in the FASTA, no developability pair in the CSV.

    Measured, not reasoned: the probe CIF is relabelled to C/D while the
    target chain stays "A", so "A" is genuinely absent. Deleting the
    ``present`` guard makes the two marker assertions below fail.
    """
    from shared.exports import candidates_to_csv, candidates_to_fasta
    from shared.jobs import page_ordered_records

    cifs = {stem: _relabel_auth(text, {"A": "C", "B": "D"})
            for stem, text in _probe_cifs().items()}

    def fetch(job_id: str, key: str) -> bytes | None:
        text = cifs.get(Path(key).stem.split("_", 1)[1])
        return text.encode("utf-8") if text else None

    rows = page_ordered_records("rfd4", _example())
    fasta = candidates_to_fasta(
        rows, tool="rfd4", fetch_bytes=fetch, default_job_id=JOB_ID,
        target_chain="A")
    heads = [ln for ln in fasta.splitlines() if ln.startswith(">")]
    assert len(heads) == 16, heads
    assert "_chainC" in fasta and "_chainD" in fasta, heads
    assert "[designed chain]" not in fasta, heads
    assert "[target chain]" not in fasta, heads

    body = candidates_to_csv(
        rows, fetch_bytes=fetch, default_job_id=JOB_ID, target_chain="A")
    for row in csv.DictReader(io.StringIO(body)):
        assert "gravy" not in row and "instability_index" not in row, row


def _only_chain(text: str, keep: str, as_id: str) -> str:
    """One probe CIF reduced to the atoms of author chain ``keep``, relabelled
    ``as_id``, so it reads back as exactly one chain under a different id."""
    head = [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith("_atom_site.")]
    ia = head.index("_atom_site.auth_asym_id")
    lines = []
    for line in text.splitlines():
        if line.startswith(("ATOM ", "HETATM")):
            fields = line.split()
            if fields[ia] != keep:
                continue
            fields[ia] = as_id
            line = " ".join(fields)
        lines.append(line)
    return "\n".join(lines) + "\n"


def test_a_lone_relabelled_target_is_not_scored_as_the_design():
    """Cited by shared/exports.py::_add_chain_columns, above its ``present``
    test.

    The hole the two-chain case above cannot reach. With the target absent,
    ``len(designed) != 1`` fails safe only because TWO chains read back and
    neither matched; a file reading back as exactly ONE chain under a
    different id leaves exactly one "designed" chain, and the pair is then
    computed from it. Here that one chain IS the operator's uploaded target
    (chain A alone, relabelled "C", with the run's ``target_chain`` still
    "A"), so without the guard the CSV prints the target's gravy and
    instability index under the design's name -- numbers a customer orders
    wet-lab work against.

    Measured, not reasoned. The three assertions below are what make this
    test non-vacuous rather than an absence that any bug could also produce:
    the structure really reads back as one chain, that chain really carries
    the target's sequence, and the pair really is computable from it -- so
    the empty columns are the guard's work and nothing else's. Deleting the
    ``present`` test in _add_chain_columns makes the last assertion fail.

    How a run reaches it is NOT settled by any committed artefact: it needs
    upstream to re-emit the target under a different letter AND to deliver a
    design file holding one chain, and the probe run
    (docs/scoping/rung4-probe/gpu_result_20261103T114437.json) does neither
    -- its eight files all read back as two chains, A and B. So this is a
    guard against a constructed input, pinned at the function's own level,
    not a fix for an observed run.
    """
    from shared.exports import (
        _sequence_developability, candidates_to_csv, structure_chain_sequences)
    from shared.jobs import page_ordered_records

    cifs = {stem: _only_chain(text, "A", "C")
            for stem, text in _probe_cifs().items()}
    assert cifs, "no probe CIFs, so nothing below proves anything"

    def fetch(job_id: str, key: str) -> bytes | None:
        text = cifs.get(Path(key).stem.split("_", 1)[1])
        return text.encode("utf-8") if text else None

    # The constructed file is one chain, it is the target's sequence, and the
    # pair is computable from it: all three hold, so the absence asserted
    # last is the guard and not a reduced file that scores nothing anyway.
    originals = {stem: dict(_chain_seqs(text))
                 for stem, text in _probe_cifs().items()}
    for stem, text in cifs.items():
        read_back = structure_chain_sequences(text.encode("utf-8"))
        assert [cid for cid, _ in read_back] == ["C"], (stem, read_back)
        assert read_back[0][1] == originals[stem]["A"], stem
        assert _sequence_developability(read_back[0][1]) is not None, stem

    body = candidates_to_csv(
        page_ordered_records("rfd4", _example()), fetch_bytes=fetch,
        default_job_id=JOB_ID, target_chain="A")
    rows = list(csv.DictReader(io.StringIO(body)))
    assert len(rows) == 8, len(rows)
    for row in rows:
        assert "gravy" not in row and "instability_index" not in row, row
        # The chain itself is still delivered, unmarked and unscored.
        assert row["sequence_chainC"], row


def test_the_runtime_line_renders_the_key_this_pipeline_writes(
        tools_app, rfd4_registered):
    """Cited by templates/tools/rfd4_results.html, above the results_panel call.

    components/results_shell.html:242 guards the runtime line on a truthy
    ``gpu_seconds``, and rfd4's pipeline stores the figure under
    ``runtime_seconds`` instead (tools/rfd4/run_pipeline.py:1300; the committed
    example result carries runtime_seconds and no gpu_seconds). So reading
    ``gpu_seconds`` in the partial dropped the line on every rfd4 job. The
    example result's 217 below is the value that must appear. Mutation-proven,
    not assumed: with the partial reading ``output.get('gpu_seconds')`` again,
    this file is 1 failed, 13 passed, and this is the failure.
    """
    flask_app, _ = tools_app
    result = _example()
    assert "gpu_seconds" not in result, sorted(result)
    html = _render_partial(
        flask_app, "rfd4", job_id=JOB_ID, example=False, result=result)
    assert f"GPU time: {result['runtime_seconds']} seconds." in html
