"""pxdesign's advertised runtime band must bracket the runs that measured it.

The catalog advertised a "30 to 60 min" pilot run in EIGHT places across
tools/pxdesign/meta.py, tools/pxdesign/__init__.py and
templates/tools/pxdesign_form.html. Every pxdesign pilot run on record
finished BELOW the bottom of it:

    designs   GPU-s   wallclock   source
       2       504      8.4 min   job 816fc4a9, docs/VALIDATION-LOG.md
       5       462      7.7 min   job 79228f03, docs/VALIDATION-LOG.md
      25      1380     23.0 min   ``runtime_minutes`` in
                                  tools/pxdesign/example/result.json

Every one of them cleared the 30-min floor outright: the slowest by 7.0 min
and the fastest by a factor of 3.9. The band is now "8 to 25 min".

FINDING THE EIGHT NEEDS A REGEX, which is why this file re-derives them
from the source text rather than listing line numbers. Six read "30 to
60"; the module docstring of tools/pxdesign/__init__.py used a real en
dash; and the preset table at the top of the form wrote that dash as a
literal backslash-u2013 escape -- six characters -- so it survived even a
search for the en dash itself. A NINTH carried the same claim in a form no
"30.*60" regex can see: the preset label read "~45 min", the midpoint of
the band, and it is covered here too.

WHAT THE BAND IS. The span of the three runs, rounded OUTWARD: 25 rounds
23.0 up, and 8 rounds 8.4 down and 7.7 UP. That last one is the only place
the band is tighter than a measurement, by 0.3 min, and it is disclosed
here rather than widened away -- a floor of 7 would advertise a speed no
run has ever reached, the fastest being 7.7. Rounding the TOP up is the load-bearing direction:
an estimate that reads low is the one that strands a caller.

NO ENDPOINT IS ATTRIBUTED TO A DESIGN COUNT. The record does not order
that way: 5 designs ran FASTER than 2 (7.7 against 8.4), and the four
smoke successes in the same log took 16.5 to 17.5 min on a SINGLE design,
about twice the 2-design pilot. Smoke is a different tier on a baked
target, so it is not in the derivation, but it refutes a per-count reading
of the band; all four land inside 8 to 25 anyway. Count and target size
are confounded besides -- the 25-design run is the largest target too --
so no rate can be separated out of three points.

WHAT IT DOES NOT BOUND. Failures: docs/VALIDATION-LOG.md carries a
mini_pilot FAIL that ran 75.3 min before dying at 78.5%, and a band fitted
to runs that finished cannot describe one that does not. The rest of the
input domain: the form takes 1 to 1000 designs and the largest run on
record is 25, and target size is unmeasured past the ~420 aa example
against a 600 aa cap. Every surface states the range it was measured over
for that reason.

NOT PINNED HERE. The preflight estimator in shared/pdb_preflight_rules.py
is a different number with a different job -- a per-request cost gate, not
an advertised band -- fitted in its own ``_PXDESIGN`` envelope against its
own test. The two are allowed to disagree; this file pins only the catalog
copy.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from tools import pxdesign
from tools.pxdesign import meta as pxmeta

_ROOT = Path(__file__).resolve().parents[1]

#: The band as every surface must spell it.
BAND = "8 to 25 min"

#: Its endpoints, in minutes.
BAND_LO, BAND_HI = 8.0, 25.0

#: The three files that carried the eight occurrences.
SURFACES = (
    "tools/pxdesign/meta.py",
    "tools/pxdesign/__init__.py",
    "templates/tools/pxdesign_form.html",
)

#: The band this replaced, in the three encodings it was written in: an
#: ASCII hyphen, a real en dash, and the six-character escape of that en
#: dash. Spelling all three is the point -- a literal "30 to 60" search
#: found only six of the eight.
_EN_DASH = "–"
_ESCAPED_EN_DASH = "\\u2013"
OLD_BAND = re.compile(
    "30" + r"\s*(?:to|-|" + _EN_DASH + "|" + re.escape(_ESCAPED_EN_DASH) + r")\s*" + "60"
)

#: The midpoint form, which no OLD_BAND search can see.
OLD_MIDPOINT = re.compile(r"~\s*45\s*min")


def _read(rel: str) -> str:
    return io.open(_ROOT / rel, encoding="utf-8").read()


def _measurements() -> dict:
    """The three runs, read from their sources rather than restated here.

    The 25-design figure is machine-readable and is read. The other two are
    prose in docs/VALIDATION-LOG.md and are matched against their job ids,
    so correcting either row fails this test and forces the band to be
    re-derived rather than silently drifting off its evidence.
    """
    example = json.loads(_read("tools/pxdesign/example/result.json"))
    log = _read("docs/VALIDATION-LOG.md")

    runs = {}
    for job, designs in (("816fc4a9", 2), ("79228f03", 5)):
        # Anchored on the row's OWN "Job `<id>" declaration, not a bare
        # substring: the 816fc4a9 row also names 79228f03 (it supersedes
        # that run's FLAG), and a substring match handed both jobs that one
        # row and read its 8.4 min twice.
        marker = "Job `" + job
        rows = [ln for ln in log.splitlines() if marker in ln and "pxdesign" in ln]
        assert len(rows) == 1, f"{len(rows)} pxdesign rows declare job {job}"
        row = rows[0]
        found = re.search(r"~([0-9]+\.[0-9]+) min wallclock", row)
        assert found is not None, f"job {job} states no wallclock"
        runs[job] = (designs, float(found.group(1)))

    runs["example"] = (
        int(example["total_designs"]),
        float(example["runtime_minutes"]),
    )
    return runs


def test_band_brackets_every_measured_run():
    """8 and 25 are the measurements rounded outward, not a guess."""
    runs = _measurements()
    assert len(runs) == 3

    minutes = sorted(m for _, m in runs.values())
    assert minutes == [7.7, 8.4, 23.0], minutes

    # The top covers the slowest success outright.
    assert BAND_HI >= max(minutes)

    # The floor is the fastest success rounded to the minute. 7.7 -> 8 is
    # the one place the band is tighter than a run; the size of that gap is
    # asserted so that widening it silently is a failure.
    assert BAND_LO == float(round(min(minutes)))
    assert BAND_LO - min(minutes) == pytest.approx(0.3, abs=0.01)

    # And rounding the floor down instead would undershoot every run:
    # no pxdesign pilot has ever finished in 7 min.
    assert min(minutes) > BAND_LO - 1.0

    # The band this replaced contained NONE of them.
    assert all(m < 30.0 for m in minutes)


def test_design_counts_span_the_range_the_band_is_quoted_over():
    """The endpoints' design counts are the ones actually measured."""
    runs = _measurements()
    assert sorted(d for d, _ in runs.values()) == [2, 5, 25]

    # Each file states the two endpoint measurements it reasons from, so a
    # surface cannot keep the band after its evidence has been edited out.
    for rel in SURFACES:
        text = _read(rel)
        assert "8.4" in text and "23.0" in text, rel


@pytest.mark.parametrize("rel", SURFACES)
def test_no_surface_still_advertises_the_old_band(rel: str):
    """Any "30.*60" left in these files must be historical, not advertised.

    Matched on the source text in all three encodings, so a re-introduced
    en dash or ``\\u2013`` escape fails here too. A comment is exempt only
    where it RECORDS the replacement; an advertised value can never be one,
    because none of the eight sat in a comment.
    """
    for lineno, line in enumerate(_read(rel).splitlines(), start=1):
        if not OLD_BAND.search(line) and not OLD_MIDPOINT.search(line):
            continue
        stripped = line.lstrip()
        assert stripped.startswith("#") or stripped.startswith("{#"), (
            f"{rel}:{lineno} still advertises the old band: {line}"
        )
        assert "replaced" in line.lower(), (
            f"{rel}:{lineno} names the old band without recording that it "
            f"was replaced: {line}"
        )


def test_every_python_surface_carries_the_band():
    """The six values the catalog serves out of Python.

    Read off the live objects, not the file text: these are what the form
    route and the tool catalog actually render.
    """
    values = {
        "module docstring": pxdesign.__doc__,
        "preset_runtime_rows": pxmeta.preset_runtime_rows[0]["runtime"],
        "about runtime_table": pxmeta.about["runtime_table"][0]["typical"],
        "adapter blurb": pxdesign.adapter.blurb,
        "preset label": pxdesign.adapter.presets[0].label,
        "preset description": pxdesign.adapter.presets[0].description,
    }
    for name, value in values.items():
        assert BAND in value, f"{name} does not carry the band: {value!r}"
        assert not OLD_BAND.search(value), f"{name} still carries the old band"
        assert not OLD_MIDPOINT.search(value), f"{name} still carries ~45 min"


def test_every_template_surface_carries_the_band():
    """The three the form renders, one of which is the Jinja mirror.

    The mirror is a literal copy of ``preset_runtime_rows`` by design -- the
    generic tool_form route injects no per-tool meta -- so it is checked
    against the Python value rather than a second hardcoded string. That is
    the pair that drifted.
    """
    text = _read("templates/tools/pxdesign_form.html")
    assert text.count(BAND) == 3, text.count(BAND)
    assert '{"slug": "pilot", "runtime": "' + BAND + '"' in text
    assert pxmeta.preset_runtime_rows[0]["runtime"] == BAND


def test_no_surface_attributes_an_endpoint_to_a_design_count():
    """The measurements do not order by count, so no surface may imply it.

    5 designs ran faster than 2. Anything of the form "8 at two designs"
    reads a trend into three points that do not carry one.
    """
    by_count = {d: m for d, m in _measurements().values()}
    assert by_count[5] < by_count[2], by_count

    # "twelve times" bans the rate sentence this change deleted from the
    # form panel and the "Number of designs" explanation ("twelve times
    # the designs costs under three times the ..."), which the other
    # three phrases do not reach.
    banned = ("at two designs", "at twenty-five", "fixed setup",
              "twelve times")
    live = [pxdesign.__doc__, pxdesign.adapter.blurb,
            pxdesign.adapter.presets[0].label,
            pxdesign.adapter.presets[0].description,
            pxmeta.about["runtime_table"][0]["typical"]]
    live += [_read(rel) for rel in SURFACES]
    for text in live:
        for phrase in banned:
            assert phrase not in text.lower(), phrase


def test_no_surface_claims_runtime_scales_linearly_with_designs():
    """It does not: 12.5x the designs cost 2.7x the time.

    The "Number of designs" explanation said cost and runtime both rise
    "linearly". The same three runs refute the runtime half -- 2 designs
    took 8.4 min and 25 took 23.0, where linear would be 105 -- because
    most of a run is fixed setup.
    """
    by_count = {d: m for d, m in _measurements().values()}
    linear = by_count[2] * (25 / 2)
    assert by_count[25] < linear / 3

    designs = next(
        item
        for item in pxmeta.about["inputs"]
        if item["name"] == "Number of designs"
    )
    assert "linear" not in designs["explanation"].lower()
    assert "8.4" in designs["explanation"]
    assert "23.0" in designs["explanation"]

    # And it states the ceiling of what was measured, because the form
    # accepts far more designs than any run on record used.
    assert "twenty-five" in designs["explanation"]
