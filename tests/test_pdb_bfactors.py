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
    bfactors_on_100,
    bfactors_on_100_b64,
    is_fractional,
)

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REPO = pathlib.Path(__file__).resolve().parent.parent


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
