"""pLDDT in the B-factor column, converted on the way out.

A predicted structure carries per-residue confidence in the B-factor
column, and AlphaFold DB, the ESM Atlas and every colouring recipe in
the field read it on 0-100. ESMFold's head returns 0-1, so a downloaded
ESMFold PDB ran 0.10-0.77 while the page above the download button read
21.5-65.9, and ``spectrum b, blue_white_red, minimum=50, maximum=90``
painted the whole chain one flat colour.

THE GATE IS WHOLE-FILE, AND THAT IS THE LOAD-BEARING PART. A real
crystallographic B-factor can be below 1: ``static/example/1HEW.pdb``
runs 0.01 to 150.80. A per-atom rule would scale that one 0.01 atom to
1.00 and leave the 150.80 alone, corrupting a file it had no business
touching. Most of this file exists to pin that.
"""

from __future__ import annotations

import base64
import os
import pathlib
import re

import pytest

from shared.pdb_bfactors import (
    _coordinate_bfactor,
    _looks_like_cif,
    bfactors,
    bfactors_on_100,
    bfactors_on_100_b64,
    bfactors_on_100_bytes,
    is_fractional,
)

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def tools_app():
    """The real app, so the macro and the routes run with the globals
    and the blueprints they actually ship with."""
    import app as app_module
    from shared.feature_flags import flag_name
    from tools import base as tool_base

    slugs = sorted(a.slug for a in tool_base.all_adapters())
    prior = {}
    for slug in slugs:
        prior[flag_name(slug)] = os.environ.get(flag_name(slug))
        os.environ[flag_name(slug)] = "on"
    prior["SESSION_SECRET_KEY"] = os.environ.get("SESSION_SECRET_KEY")
    os.environ["SESSION_SECRET_KEY"] = "test-secret"
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    yield flask_app, slugs
    for key, val in prior.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _atom(serial: int, bfactor: float) -> str:
    """One ATOM record with ``bfactor`` in columns 61-66."""
    return (
        f"ATOM  {serial:5d}  CA  LEU A{serial:4d}    "
        f"   0.000   0.000   0.000  1.00{bfactor:6.2f}           C\n"
    )


def _bfactors(text: str) -> list[float]:
    return [
        float(line[60:66])
        for line in text.splitlines()
        if line.startswith("ATOM") and len(line) >= 66
    ]


class TestTheWholeFileGate:
    def test_an_all_fractional_file_is_scaled(self):
        pdb = _atom(1, 0.21) + _atom(2, 0.66)
        assert is_fractional(pdb)
        assert _bfactors(bfactors_on_100(pdb)) == [21.0, 66.0]

    def test_one_atom_above_one_protects_the_whole_file(self):
        """The 1HEW shape, minimised. A per-atom rule scales the 0.01
        and leaves the 150.80, which is data corruption."""
        pdb = _atom(1, 0.01) + _atom(2, 150.80)
        assert not is_fractional(pdb)
        assert bfactors_on_100(pdb) == pdb

    def test_the_real_crystal_file_is_untouched_byte_for_byte(self):
        """Not a synthetic case: this file ships in the repo and mpnn's
        worked example hands it to the reader."""
        crystal = (REPO / "static" / "example" / "1HEW.pdb").read_text(
            encoding="utf-8",
        )
        values = _bfactors(crystal)
        assert min(values) < 1.0 < max(values), (
            "1HEW is the fixture BECAUSE it straddles the boundary; if "
            "that stopped being true this test proves nothing"
        )
        assert bfactors_on_100(crystal) == crystal

    def test_every_static_example_structure_is_left_alone(self):
        """All three shipped fixtures are real depositions."""
        seen = 0
        for path in (REPO / "static" / "example").glob("*.pdb"):
            text = path.read_text(encoding="utf-8")
            if not _bfactors(text):
                continue
            seen += 1
            assert bfactors_on_100(text) == text, path.name
        assert seen >= 3, f"only {seen} fixture structures scanned"

    def test_a_file_with_no_coordinates_is_not_fractional(self):
        assert not is_fractional("HEADER    NOTHING HERE\n")
        assert not is_fractional("")


class TestTheRecordIsNotDamaged:
    def test_column_widths_survive(self):
        """A PDB is a column format. Widening the B-factor field by one
        character shifts the element symbol and every parser breaks."""
        pdb = _atom(1, 0.21) + _atom(2, 0.66)
        converted = bfactors_on_100(pdb)
        assert [len(line) for line in converted.splitlines()] == [
            len(line) for line in pdb.splitlines()
        ]
        for line in converted.splitlines():
            assert line[76:78].strip() == "C", repr(line[70:])

    def test_a_full_scale_value_still_fits_the_column(self):
        """1.0 -> 100.00 is six characters, exactly the field width."""
        converted = bfactors_on_100(_atom(1, 1.0))
        assert converted[60:66] == "100.00"
        assert len(converted.splitlines()[0]) == len(
            _atom(1, 1.0).splitlines()[0]
        )

    def test_non_coordinate_lines_pass_through(self):
        pdb = "HEADER    TEST\n" + _atom(1, 0.21) + "END\n"
        converted = bfactors_on_100(pdb)
        assert converted.startswith("HEADER    TEST\n")
        assert converted.endswith("END\n")

    def test_it_is_not_idempotent_and_does_not_claim_to_be(self):
        """Same weakness as metric_glossary.plddt_on_100, and the
        column's own precision fixes exactly where it bites: a
        B-factor is written to two decimals, so the smallest non-zero
        value the file can hold is 0.01, and 0.01 scales to exactly
        1.00 -- still inside the fractional window. A second pass takes
        it to 100. Apply once. Pinned so nobody claims idempotency."""
        pdb = _atom(1, 0.01)
        once = bfactors_on_100(pdb)
        assert _bfactors(once) == [1.0]
        assert is_fractional(once), (
            "1.00 is still within 0-1, which is why a second pass is "
            "not a no-op here"
        )
        assert _bfactors(bfactors_on_100(once)) == [100.0]

    def test_a_realistic_prediction_survives_a_second_pass(self):
        """The range real payloads live in: esmfold's example bottoms
        out at 0.10, so one pass lands it above the window and a second
        is a genuine no-op. That is why the hazard above stayed hidden."""
        once = bfactors_on_100(_atom(1, 0.10) + _atom(2, 0.77))
        assert not is_fractional(once)
        assert bfactors_on_100(once) == once


class TestTheBase64Wrapper:
    def test_it_round_trips_a_fractional_structure(self):
        pdb = _atom(1, 0.21) + _atom(2, 0.66)
        raw = base64.b64encode(pdb.encode()).decode()
        out = base64.b64decode(bfactors_on_100_b64(raw)).decode()
        assert _bfactors(out) == [21.0, 66.0]

    @pytest.mark.parametrize(
        "junk", ["", "not base64!!", "bm8gYXRvbXMgaGVyZQ=="],
    )
    def test_junk_comes_back_untouched(self, junk):
        """A download rendering the wrong scale is a bug; a download
        rendering nothing is an outage."""
        assert bfactors_on_100_b64(junk) == junk

    def test_a_non_fractional_structure_returns_the_ORIGINAL_string(self):
        """Not a re-encoded equivalent. Re-encoding would churn the
        bytes of every crystal structure the site serves."""
        crystal = (REPO / "static" / "example" / "1HEW.pdb").read_bytes()
        raw = base64.b64encode(crystal).decode()
        assert bfactors_on_100_b64(raw) is raw


class TestTheEsmfoldDownloadAgreesWithItsPage:
    """End to end, on the page the defect was found on."""

    @pytest.fixture(scope="class")
    def page(self):
        import app as app_module
        from shared.feature_flags import flag_name
        from tools import base as tool_base

        slugs = sorted(a.slug for a in tool_base.all_adapters())
        prior = {}
        for slug in slugs:
            prior[flag_name(slug)] = os.environ.get(flag_name(slug))
            os.environ[flag_name(slug)] = "on"
        prior["SESSION_SECRET_KEY"] = os.environ.get("SESSION_SECRET_KEY")
        os.environ["SESSION_SECRET_KEY"] = "test-secret"
        flask_app = app_module.create_app()
        flask_app.config["TESTING"] = True
        with flask_app.test_client() as client:
            html = client.get("/tools/esmfold").get_data(as_text=True)
        for key, val in prior.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        return html

    def test_the_download_link_carries_a_0_100_structure(self, page):
        match = re.search(
            r"data:chemical/x-pdb;base64,([A-Za-z0-9+/=]+)", page,
        )
        assert match, "esmfold's PDB download link did not render"
        values = _bfactors(
            base64.b64decode(match.group(1)).decode("utf-8", "replace")
        )
        assert values, "the downloaded file has no ATOM records"
        assert max(values) > 1.0, (
            f"the downloaded structure is still fractional "
            f"({min(values)}..{max(values)}) while the page reads 0-100"
        )
        assert 1.0 < max(values) <= 100.0

    def test_the_page_and_the_file_are_on_the_same_scale(self, page):
        """The contradiction that started this: tooltips on one scale,
        the file the button hands you on another."""
        tooltips = [float(v) for v in re.findall(r"pLDDT=([\d.]+)", page)]
        match = re.search(
            r"data:chemical/x-pdb;base64,([A-Za-z0-9+/=]+)", page,
        )
        values = _bfactors(
            base64.b64decode(match.group(1)).decode("utf-8", "replace")
        )
        assert tooltips and values
        # Not equal -- a B-factor is per ATOM and a tooltip is per
        # RESIDUE -- but they must land in the same order of magnitude.
        assert max(tooltips) / max(values) < 2.0
        assert max(values) / max(tooltips) < 2.0


class TestTheDefectsQCFound:
    """Each of these shipped in the first draft and was caught in review."""

    def test_a_short_record_is_not_welded_to_the_next_one(self):
        """The reader used splitlines() and the writer
        splitlines(keepends=True), both testing length against 66. A
        65-character record plus its newline measures 66, so the writer
        rewrote a line the reader had never inspected -- and
        ``line[66:]`` was then "", which ate the terminator and welded
        two ATOM records into one. Three records in, two out."""
        full = _atom(1, 0.21).rstrip("\n")
        short = full[:65]
        pdb = full + "\n" + short + "\n" + full + "\n"
        out = bfactors_on_100(pdb)
        assert len(out.splitlines()) == 3, out
        assert short in out.splitlines()[1], (
            "the short record was rewritten by a writer the gate never "
            "let inspect it"
        )

    def test_crlf_terminators_survive(self):
        pdb = _atom(1, 0.21).rstrip("\n") + "\r\n"
        out = bfactors_on_100(pdb)
        assert out.endswith("\r\n")
        assert bfactors(out) == [21.0]

    def test_an_mmcif_atom_site_row_is_not_touched(self):
        """``ATOM  `` also prefixes an mmCIF row, which is
        whitespace-DELIMITED. Rewriting fixed offsets in one eats a
        separator and yields a row with a field missing. The predicate
        requires the whole fixed-column layout to parse, which a CIF
        row does not."""
        cif = (
            "ATOM   1    N N   . MET A 1 1   ? 12.345 -3.210  8.765  "
            "1.00 0.85 ? 1   MET A N   1\n"
            "ATOM   2    C CA  . MET A 1 1   ? 13.000 -3.000  9.000  "
            "1.00 0.90 ? 1   MET A CA  1\n"
        )
        assert bfactors(cif) == [], "a CIF row was read as a PDB record"
        assert not is_fractional(cif)
        assert bfactors_on_100(cif) is cif

    def test_non_utf8_bytes_in_a_declined_file_are_preserved(self):
        """The byte path decoded and re-encoded unconditionally, which
        destroyed every non-UTF-8 byte in a file the gate had already
        declined -- breaking the byte-for-byte promise on exactly the
        files it exists to protect."""
        raw = (
            b"REMARK   1 AUTHOR  Jos\xe9 Garc\xeda\n"
            + _atom(1, 30.48).encode()
        )
        assert bfactors_on_100_bytes(raw) is raw

    def test_a_declined_utf8_file_comes_back_as_the_SAME_object(self):
        """Two different properties, and the non-UTF-8 test above only
        covers one. That input fails the decode and returns early, so it
        never reaches the re-encode; a perfectly valid UTF-8 crystal
        structure does. Without the identity check every declined file
        is decoded and re-encoded on every download for nothing."""
        crystal = (REPO / "static" / "example" / "1HEW.pdb").read_bytes()
        assert bfactors_on_100_bytes(crystal) is crystal

    def test_the_byte_path_still_converts_a_fractional_file(self):
        raw = _atom(1, 0.21).encode()
        assert bfactors(bfactors_on_100_bytes(raw).decode()) == [21.0]


class TestEveryDownloadPathConverts:
    """The first draft converted the paths a user rarely takes.

    ``candidate_table`` sets ``use_url = raw_pdb_key and not is_example``
    and ``_slim_result_for_persist`` drops the inline copy for any
    ``designs/`` key -- so every modern job resolves through Storage and
    the template's converted value is never reached. Converting only
    there was, in practice, an ESMFold-worked-example-only fix.
    """

    def test_the_zip_export_converts(self):
        """One helper behind the job, campaign and target ZIP routes."""
        import io
        import zipfile

        from shared.exports import candidates_to_zip

        blob = base64.b64encode(_atom(1, 0.21).encode()).decode()
        archive = candidates_to_zip(
            [{"rank": 1, "pdb_key": "d.pdb", "pdb_content_b64": blob}],
            lambda _job, _key: None,
        )
        inner = zipfile.ZipFile(io.BytesIO(archive)).read("d.pdb").decode()
        assert bfactors(inner) == [21.0], inner

    def test_the_zip_export_leaves_a_crystal_structure_alone(self):
        import io
        import zipfile

        from shared.exports import candidates_to_zip

        crystal = (REPO / "static" / "example" / "1HEW.pdb").read_bytes()
        blob = base64.b64encode(crystal).decode()
        archive = candidates_to_zip(
            [{"rank": 1, "pdb_key": "x.pdb", "pdb_content_b64": blob}],
            lambda _job, _key: None,
        )
        assert zipfile.ZipFile(io.BytesIO(archive)).read("x.pdb") == crystal

    # (payload B-factors, what every route must serve).
    #
    # The 0.01 row pins SINGLE APPLICATION on the customer surface.
    # ``bfactors_on_100`` is documented as not idempotent -- 0.01 scales
    # to 1.00, which is still inside the fractional window, so a second
    # pass takes it to 100.00 -- and wrapping the route's call in itself
    # passed 403 of 403 tests across every file that touches these
    # routes. The staging path had an exactly-once test; the primary
    # download, which serves far more people, had none.
    @pytest.mark.parametrize(
        "payload, expected",
        [((0.21, 0.66), [21.0, 66.0]), ((0.01, 0.01), [1.0, 1.0])],
    )
    def test_every_structure_route_in_jobs_converts(
        self, tools_app, payload, expected,
    ):
        """Behavioural, not counted.

        The counted version asserted three occurrences of the call in
        blueprints/jobs.py. A count is satisfied by any three
        occurrences anywhere: deleting the Storage-path call -- the
        primary download and the headline of this change -- and
        duplicating the af2 one kept the count at 3 and the whole file
        green. This drives the routes instead.
        """
        import types

        flask_app, _slugs = tools_app
        fractional = _atom(1, payload[0]) + _atom(2, payload[1])

        job = types.SimpleNamespace()
        job.id = "job-1"
        job.tool = "af2"
        job.status = "succeeded"
        job.result = {
            "pdb_b64": base64.b64encode(fractional.encode()).decode(),
            "candidates": [{
                "rank": 1,
                "pdb_key": "designs/d.pdb",
                "pdb_content_b64": base64.b64encode(
                    fractional.encode()
                ).decode(),
            }],
        }
        ctx = types.SimpleNamespace(user_id="u-1", email="u@example.com")

        import blueprints.jobs as jobs_bp

        served = {}

        def _fake_ctx():
            return ctx

        def _fake_job(job_id, user_id=None, **_kw):
            return job

        originals = (
            jobs_bp.load_user_context, jobs_bp.get_job,
            jobs_bp.output_exists, jobs_bp.download_output,
        )
        jobs_bp.load_user_context = _fake_ctx
        jobs_bp.get_job = _fake_job
        try:
            with flask_app.test_client() as client:
                # @login_required reads the SESSION, not the patched
                # context loader, and redirects before the route runs.
                with client.session_transaction() as sess:
                    sess["user_email"] = "u@example.com"
                # 1) Storage path -- what every modern job takes.
                jobs_bp.output_exists = lambda **_kw: True
                jobs_bp.download_output = (
                    lambda **_kw: fractional.encode()
                )
                served["storage"] = client.get(
                    "/api/jobs/job-1/pdb/designs%2Fd.pdb"
                ).get_data(as_text=True)

                # 2) Same route, inline fallback.
                jobs_bp.output_exists = lambda **_kw: False
                served["inline"] = client.get(
                    "/api/jobs/job-1/pdb/designs%2Fd.pdb"
                ).get_data(as_text=True)

                # 3) The af2 download.
                served["af2"] = client.get(
                    "/jobs/job-1/af2.pdb"
                ).get_data(as_text=True)
        finally:
            (jobs_bp.load_user_context, jobs_bp.get_job,
             jobs_bp.output_exists, jobs_bp.download_output) = originals

        for name, body in served.items():
            values = _bfactors(body)
            assert values == expected, (
                f"{name} route served {values} for a {payload} payload; "
                f"expected {expected}. A doubled conversion reads 100.00 "
                "where the design was 0.01 -- confident, and wrong."
            )

    def test_each_template_download_site_converts(self, tools_app):
        """Behavioural, not counted.

        The counted version asserted the call text appeared once per
        template. Rewriting the candidate table's condition to
        ``has_b64 and use_url and not use_url`` left the text present,
        the count at 1, and the legacy row serving raw 0-1 -- green.
        This renders the macro and reads what it emits.
        """
        flask_app, _slugs = tools_app
        fractional = _atom(1, 0.50) + _atom(2, 0.50)
        blob = base64.b64encode(fractional.encode()).decode()

        tmpl = flask_app.jinja_env.from_string(
            "{% from 'components/candidate_table.html' import"
            " candidate_table %}"
            "{{ candidate_table(cands, ['pLDDT'], 'job-1', 'esmfold') }}"
        )
        # A LEGACY row: inline b64 and NO pdb_key, so use_url is false
        # and both the viewer and the download read the converted value.
        with flask_app.test_request_context("/"):
            html = tmpl.render(cands=[{
                "rank": 1,
                "pdb_content_b64": blob,
                "scores": {"pLDDT": 0.5},
            }])

        found = re.findall(
            r"data:chemical/x-pdb;base64,([A-Za-z0-9+/=]+)", html
        )
        assert found, "the legacy row emitted no PDB download"
        served = base64.b64decode(found[0]).decode()
        assert _bfactors(served) == [50.0, 50.0], (
            f"the candidate table served {_bfactors(served)} for a 0.50 "
            "payload"
        )

    def test_the_colabfold_download_converts(self, tools_app):
        """Renders the partial, because the substring version of this
        was hollow: moving the call text into a Jinja comment and
        pointing the live link at the raw payload left it green. It did
        not even strip comments, so it was WEAKER than the counted
        guard it replaced. colabfold's own example carries no pdb_b64
        (it was dropped when a designed sequence turned out to be
        recoverable from it), so the payload is stubbed here.
        """
        import types

        flask_app, _slugs = tools_app
        blob = base64.b64encode(
            (_atom(1, 0.21) + _atom(2, 0.66)).encode()
        ).decode()
        job = types.SimpleNamespace()
        job.id = "example"
        job.status = "succeeded"
        job.tool = "colabfold"
        job.inputs = {}
        job.result = {
            "pdb_b64": blob,
            "mean_plddt": 61.05,
            "plddt_per_residue": [61.0, 62.0],
            "total_length": 2,
        }
        tmpl = flask_app.jinja_env.get_template(
            "tools/colabfold_results.html"
        )
        with flask_app.test_request_context("/"):
            html = tmpl.render(job=job, example=True)

        found = re.findall(
            r"data:chemical/x-pdb;base64,([A-Za-z0-9+/=]+)", html
        )
        assert found, "colabfold rendered no PDB download link"
        served = base64.b64decode(found[0]).decode()
        assert _bfactors(served) == [21.0, 66.0], (
            f"colabfold served {_bfactors(served)} for a 0.21/0.66 payload"
        )


class TestAmbiguityFailsClosed:
    """The hole the STRICTER predicate opened.

    Requiring the whole column layout made the reader refuse lines the
    old one accepted -- and ``is_fractional`` then SKIPPED them rather
    than counting them. Skipping is not disqualifying: a B-factor of 49
    on such a line could no longer veto the conversion, so a stitched
    complex came out with two scales in one file. If this module cannot
    read a line it can see, it does not get to judge the file.
    """

    # A target chain carried over from a deposition, blank occupancy.
    _UNPARSEABLE = (
        "ATOM      2  CA  LEU B   2       0.000   0.000   0.000"
        "        49.00           C\n"
    )

    def test_a_line_it_cannot_read_disqualifies_the_file(self):
        pdb = _atom(1, 0.11) + self._UNPARSEABLE
        assert not is_fractional(pdb), (
            "a 49.00 the reader cannot parse must still veto; skipping "
            "it converts the binder and leaves the target alone"
        )
        assert bfactors_on_100(pdb) is pdb

    def test_the_old_behaviour_would_have_corrupted_this_file(self):
        """Documents what fail-open produced, so the fix is not
        mistaken for belt-and-braces: 11.00 on one chain, 49.0 on the
        other, in one downloaded structure."""
        pdb = _atom(1, 0.11) + self._UNPARSEABLE
        readable = bfactors(pdb)
        assert readable == [0.11], readable
        assert all(0.0 <= v <= 1.0 for v in readable), (
            "the readable subset looks fractional on its own, which is "
            "exactly why deciding from it alone was unsafe"
        )

    def test_a_clean_file_is_still_converted(self):
        """Non-vacuity: fail-closed must not refuse everything."""
        assert is_fractional(_atom(1, 0.11) + _atom(2, 0.66))


class TestMmcifIsRefusedByFormatToo:
    def test_a_cif_header_declines_before_any_column_guessing(self):
        """The layout check refused every real CIF writer I could find,
        but it is a heuristic over offsets and a hand-rolled row can
        align by chance. opendde stores a .cif under ``pdb_key`` when
        its CIF-to-PDB conversion fails, so one genuinely reaches these
        routes."""
        # The row is deliberately COLUMN-ALIGNED, so the layout
        # check accepts it and only the header marker can decline
        # the file. A CIF whose rows do NOT align is refused by the
        # layout check anyway, and a test built on one proves
        # nothing about this guard -- the first version of this
        # test was exactly that, and deleting the marker check left
        # it green.
        aligned = (
            "ATOM      1  CA  LEU A   1       1.000   2.000   3.000"
            "  1.00  0.62           C\n"
        )
        assert _coordinate_bfactor(aligned.rstrip("\n")) == 0.62, (
            "the fixture must parse as a PDB record or this test is "
            "back to proving nothing"
        )
        cif = "data_XYZ\nloop_\n_atom_site.group_PDB\n" + aligned
        assert not is_fractional(cif)
        assert bfactors_on_100(cif) is cif

    def test_a_pdb_that_merely_mentions_loop_is_still_converted(self):
        """The marker check is line-anchored, so a REMARK containing the
        word does not disqualify a real structure."""
        pdb = "REMARK   1 REFINED IN A loop_ OF FOUR CYCLES\n" + _atom(1, 0.21)
        assert is_fractional(pdb)
        assert bfactors(bfactors_on_100(pdb)) == [21.0]


class TestAPartialBFieldAlsoDisqualifies:
    """The same hole as the blank-occupancy one, a column narrower.

    A prefix-matching record truncated inside the B field (61-65 chars)
    was skipped by both reader and writer, so a 31.0 sitting in a
    narrow field could not veto a conversion either. A record shorter
    than column 61 has no B field at all and is correctly ignored --
    that is a TER, or a genuinely truncated line, not a disagreement.
    """

    def test_a_truncated_b_field_vetoes_the_file(self):
        narrow = (
            "ATOM      2  CA  LEU B   2       0.000   0.000   0.000"
            "  1.00 31.0"
        )
        assert 60 <= len(narrow) < 66, len(narrow)
        pdb = _atom(1, 0.11) + narrow + "\n"
        assert bfactors(pdb) == [0.11], (
            "the readable subset looks fractional, which is why "
            "deciding from it alone was unsafe"
        )
        assert not is_fractional(pdb)
        assert bfactors_on_100(pdb) is pdb

    def test_a_non_coordinate_record_is_still_ignored(self):
        """Non-vacuity in the other direction. TER, REMARK and the rest
        do not start with a coordinate prefix, so they never reach the
        rule at all -- refusing every file that has a TER record would
        refuse almost every file."""
        pdb = (
            "REMARK   1 ANYTHING\n"
            + _atom(1, 0.11)
            + "TER    1234      LEU A   1\n"
            + "END\n"
        )
        assert is_fractional(pdb)
        assert bfactors(bfactors_on_100(pdb)) == [11.0]


class TestTheSeparatorHalfOfTheWeld:
    """F1. ``splitlines`` breaks on eight separators beyond CR/LF; the
    reader stripped only CR/LF. A 65-character record terminated by one
    of the other eight measured 66, ``float()`` took the separator as
    trailing whitespace so the B field parsed, and the writer overwrote
    all six columns -- eating the terminator and welding two records.
    The module docstring claims this weld was fixed; the LENGTH half
    was, the SEPARATOR half was not."""

    # 65 characters: the B value occupies columns 61-65, one short of
    # the six-wide field, so the record is unreadable ON ITS OWN.
    def _short(self, serial: int, b: float) -> str:
        return (
            f"ATOM  {serial:5d}  CA  LEU A{serial:4d}    "
            f"   0.000   0.000   0.000  1.00{b:5.2f}"
        )

    # THE FIVE THAT ACTUALLY WELDED. ``float()`` takes each of these as
    # trailing whitespace, so the truncated B field parsed and the
    # writer overwrote all six columns. Verified against the parent
    # commit: each produced a single 144-character line.
    WELDED = ["\x0b", "\x0c", "\x85", "\u2028", "\u2029"]

    # THE THREE THAT DID NOT. ``float()`` rejects the file separators,
    # so ``_coordinate_bfactor`` already returned None and the file
    # already declined. Kept as regression cover -- they are the same
    # defect class and a future ``float()`` change would move them into
    # the list above -- but listed apart, because a parameter that
    # cannot fail on the defect its message names is decoration, and
    # this file has been burned by exactly that before.
    ALREADY_DECLINED = ["\x1c", "\x1d", "\x1e"]

    @pytest.mark.parametrize("sep", WELDED + ALREADY_DECLINED)
    def test_no_record_is_welded_to_the_next(self, sep):
        assert len(self._short(1, 0.21)) == 65, "fixture is not 65 chars"
        pdb = self._short(1, 0.21) + sep + _atom(2, 0.66)
        assert len(pdb.splitlines()) == 2

        out = bfactors_on_100(pdb)

        assert len(out.splitlines()) == 2, (
            f"{sep!r} welded two records into "
            f"{len(out.splitlines())} line(s)"
        )
        # And the right answer is to DECLINE: a record this module
        # cannot read is exactly what the fail-closed rule is for.
        assert out is pdb


class TestLeadingNoiseCannotSlipPastTheGate:
    """F2, closed as a CLASS rather than at one byte offset.

    A BOM, a stray space, a tab or a NUL pad in front of a coordinate
    record is not a coordinate-record prefix -- and a BOM is not even
    whitespace to Python -- so the record failed ``startswith`` and was
    passed over as though it were a REMARK. Invisible to the gate and to
    the writer alike, which is not "declined", it is HALF CONVERTED.

    The first fix stripped a BOM at the FILE HEAD, which closed byte 0
    and left every other offset open. Two reviewers found that
    independently, and ``cat binder.pdb target.pdb`` where the second
    was saved with a BOM puts the mark exactly at the chain seam -- the
    stitched-complex case this module's fail-closed rule exists for.
    """

    # Deliberately spans BOTH families, because the first version of
    # the fix was a four-character list and a no-break space, a
    # zero-width space, a word joiner and an ideographic space all still
    # walked through it. Anything invisible has to disqualify, so the
    # fixture has to test more than the four somebody happened to name.
    NOISE = [
        "\ufeff",    # byte-order mark
        " ",        # plain space
        "\t",        # tab
        "\x00",      # NUL pad
        "\xa0",      # no-break space   -- whitespace, but not ASCII
        "\u200b",    # zero-width space -- invisible and not whitespace
        "\u2060",    # word joiner
        "\u3000",    # ideographic space
    ]

    @pytest.mark.parametrize("noise", NOISE)
    @pytest.mark.parametrize("where", [0, 1, 2])
    def test_noise_anywhere_declines_the_whole_file(self, noise, where):
        """Not half-converted, and not converted-with-the-mark-eaten:
        DECLINED, byte for byte, wherever the noise sits."""
        records = [_atom(1, 0.50), _atom(2, 0.10), _atom(3, 0.77)]
        records[where] = noise + records[where]
        pdb = "".join(records)

        assert not is_fractional(pdb)
        assert bfactors_on_100(pdb) is pdb

    @pytest.mark.parametrize("noise", NOISE)
    def test_noise_cannot_hide_a_disqualifying_value(self, noise):
        """The direction that actually corrupts. A real 88.50 carried on
        a noisy record used to be skipped, so it could not veto -- and
        the file was judged fractional on its OTHER records and scaled.
        Reproduced before the fix as 10.00 sitting beside 88.50."""
        pdb = _atom(1, 0.10) + noise + _atom(2, 88.50)

        assert not is_fractional(pdb), (
            f"{noise!r} before an 88.50 record hid it from the gate"
        )
        # Identity, not a re-read: stripping the noise back out to line
        # the columns up would also strip every real space in the file.
        # A declined file comes back as the SAME object, which says
        # everything a value comparison would and cannot be fooled.
        assert bfactors_on_100(pdb) is pdb

    def test_a_bom_does_not_let_a_cif_through(self):
        """Which mechanism fires, asserted explicitly.

        ``lstrip()`` does not strip a BOM, so the CIF sniffer genuinely
        MISSES a BOM'd ``data_`` marker -- and teaching it to see one
        would be another list of characters, which is the mistake this
        module has already made twice. It does not need to: the noise
        rule declines the file first, on that same line, for the more
        general reason. Pinning both verdicts stops the two mechanisms
        silently swapping which one carries the case.

        The earlier version of this test was HOLLOW. Its fixture was
        ``BOM + "data_XYZ\nloop_\n_atom_site.group_PDB\n"``, and lines
        two and three carry un-BOM'd markers -- so the verdict came from
        them and it passed with BOM handling removed entirely. Both
        reviewers caught it. The marker has to be the only one in the
        file AND the one wearing the mark.
        """
        cif = "\ufeff" + "data_XYZ\n" + _atom(1, 0.21)

        assert not _looks_like_cif(cif), (
            "the sniffer is not expected to see a BOM'd marker; if it "
            "now does, this test is no longer about the noise rule"
        )
        # Declined anyway, and the trailing ATOM record is what makes
        # that non-vacuous: drop the noise rule and this file is a
        # perfectly good fractional PDB with an odd first line.
        assert not is_fractional(cif)
        assert bfactors_on_100(cif) is cif

    def test_no_codepoint_anywhere_can_hide_a_record(self):
        """The guard the two earlier attempts could not have had.

        Both of them were LISTS -- four characters, then a Unicode
        block -- and a list is only correct if it is complete, which no
        example-based test can show. This sweeps every codepoint Python
        knows and asserts the count of leaks is zero, so it fails the
        moment the rule stops being exhaustive rather than the moment
        somebody thinks of the right character. The first list leaked 5
        of these; the second leaked 213.

        Excludes the ten separators ``str.splitlines`` breaks on: those
        end the line rather than sitting inside it, and they are covered
        by TestTheSeparatorHalfOfTheWeld.
        """
        splitters = set("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
        clean = _atom(1, 0.10)
        disqualifying = _atom(2, 88.50)

        leaks = []
        for cp in range(0x110000):
            ch = chr(cp)
            if ch in splitters:
                continue
            # Only invisibles can hide a record; a visible character
            # would make the line genuinely unrecognisable, which every
            # reader agrees about.
            if ch.isprintable() and not ch.isspace():
                continue
            if is_fractional(clean + ch + disqualifying):
                leaks.append(hex(cp))

        assert leaks == [], (
            f"{len(leaks)} codepoint(s) still hide a coordinate record "
            f"from the gate, e.g. {leaks[:8]} -- the file is judged "
            "fractional on its other records and half converted"
        )

    def test_a_clean_file_still_converts(self):
        """Non-vacuity: the rule above must not decline everything."""
        assert _bfactors(bfactors_on_100(_atom(1, 0.21) + _atom(2, 0.66))) == [
            21.0, 66.0,
        ]


class TestTheStaffCopyAgreesWithTheCustomerCopy:
    """The invariant the campaign-staging change exists for, which was
    stated in three prose comments and guarded by none.

    Both surfaces convert today, but nothing pinned that they AGREE. If
    the conversion is later dropped from either side they diverge again
    with a green suite -- and divergence is the whole defect: one design,
    two people, two scales.
    """

    def test_one_design_reaches_both_readers_identically(self, tools_app):
        import types
        from unittest.mock import MagicMock, patch

        from shared import storage as storage_mod

        flask_app, _slugs = tools_app
        fractional = (_atom(1, 0.21) + _atom(2, 0.66)).encode()

        # --- the CUSTOMER's copy, through the real download route.
        job = types.SimpleNamespace()
        job.id = "job-1"
        job.tool = "esmfold"
        job.status = "succeeded"
        job.result = {"candidates": [{"rank": 1, "pdb_key": "designs/d.pdb"}]}
        ctx = types.SimpleNamespace(user_id="u-1", email="u@example.com")

        import blueprints.jobs as jobs_bp

        originals = (
            jobs_bp.load_user_context, jobs_bp.get_job,
            jobs_bp.output_exists, jobs_bp.download_output,
        )
        jobs_bp.load_user_context = lambda: ctx
        jobs_bp.get_job = lambda job_id, user_id=None, **_kw: job
        jobs_bp.output_exists = lambda **_kw: True
        jobs_bp.download_output = lambda **_kw: fractional
        try:
            with flask_app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user_email"] = "u@example.com"
                customer = client.get(
                    "/api/jobs/job-1/pdb/designs%2Fd.pdb"
                ).get_data()
        finally:
            (jobs_bp.load_user_context, jobs_bp.get_job,
             jobs_bp.output_exists, jobs_bp.download_output) = originals

        # --- the STAFF copy, through the real campaign-staging path.
        client_mock = MagicMock()
        with patch.object(
            storage_mod, "get_service_client", lambda: client_mock
        ), patch.object(
            storage_mod, "download_output", return_value=fractional
        ):
            storage_mod.stage_campaign_candidates(
                campaign_id="camp-1",
                candidates=[{"rank": 1, "pdb_key": "designs/d.pdb"}],
                indices=[0],
                user_id="u-1",
                job_id="job-1",
            )
        staff = client_mock.storage.from_.return_value.upload.call_args.kwargs[
            "file"
        ]

        assert staff == customer, (
            "the shortlist Ranomics opens and the file the customer "
            "downloads are the same design and must carry the same scale"
        )
        # ...and BOTH converted. Without this, deleting the conversion
        # from both sides would leave them equal and this test green.
        assert _bfactors(customer.decode()) == [21.0, 66.0]
