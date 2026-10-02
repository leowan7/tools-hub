"""pxdesign may not tell a caller that runtime rises with the design count.

It does not. Three pilot runs are on record:

    designs   GPU-s   wallclock   source
       2       504      8.4 min   job 816fc4a9, docs/VALIDATION-LOG.md
       5       462      7.7 min   job 79228f03, docs/VALIDATION-LOG.md
      25      1380     23.0 min   ``runtime_minutes`` in
                                  tools/pxdesign/example/result.json

The "Number of designs" explanation said cost and runtime both rise
"linearly". These three refute the runtime half twice over: 12.5x the
designs cost 2.7x the wallclock, and 5 designs ran FASTER than 2, so the
record does not even order by count. The four smoke successes in the same
log took 16.5 to 17.5 min on a SINGLE design -- a different tier on a baked
target, so not in the derivation, but refuting a per-count reading again.
Count and target size are confounded besides (the 25-design run is the
largest target too), so no rate can be separated out of three points.

WHAT IS NOT BOUNDED by any of this. Failures: docs/VALIDATION-LOG.md
carries a mini_pilot FAIL that ran 75.3 min before dying at 78.5%. The rest
of the input domain: the form takes 1 to 1000 designs and the largest run on
record is 25, and target size is unmeasured past the ~420 aa example against
a 600 aa cap -- which is why the explanation also states the ceiling it was
measured to.

The three runs are READ from their sources here rather than restated, so
correcting one fails this file and forces the claim to be re-checked against
its evidence rather than drifting off it.

NOT PINNED HERE. The preflight estimator in shared/pdb_preflight_rules.py is
a different number with a different job -- a per-request cost gate -- fitted
in its own ``_PXDESIGN`` envelope against its own test.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

from tools.pxdesign import meta as pxmeta

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return io.open(_ROOT / rel, encoding="utf-8").read()


def _measurements() -> dict:
    """The three runs, read from their sources rather than restated here.

    The 25-design figure is machine-readable and is read. The other two are
    prose in docs/VALIDATION-LOG.md and are matched against their job ids.
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


def test_the_record_refutes_a_per_count_runtime_claim():
    """Both halves of the refutation, from the runs themselves."""
    runs = _measurements()
    assert len(runs) == 3

    by_count = {d: m for d, m in runs.values()}
    assert sorted(by_count) == [2, 5, 25], by_count

    # Not linear: 12.5x the designs came in under a third of linear.
    assert by_count[25] < (by_count[2] * (25 / 2)) / 3

    # Not even ordered by count.
    assert by_count[5] < by_count[2], by_count


def test_the_designs_field_makes_no_rate_claim_and_states_its_ceiling():
    """The explanation a caller reads beside the box they type into."""
    designs = next(
        item
        for item in pxmeta.about["inputs"]
        if item["name"] == "Number of designs"
    )
    explanation = designs["explanation"].lower()

    assert "linear" not in explanation

    # "at two designs" / "at twenty-five" would attribute a figure to a
    # count; "twelve times" is the rate claim the other two do not reach
    # ("twelve times the designs costs under three times the wait"). No
    # shipped surface ever carried that last one -- it was written into an
    # early draft and dropped before merge -- so this entry is preventive.
    for phrase in ("at two designs", "at twenty-five", "twelve times"):
        assert phrase not in explanation, phrase

    # The ceiling of what was measured, because the form accepts far more
    # designs than any run on record used.
    assert "twenty-five" in explanation
