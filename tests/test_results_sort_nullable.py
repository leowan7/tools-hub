"""A results page must not 500 because one design is missing its sort key.

WHY THIS FILE EXISTS. Six per-tool results partials ordered candidate rows
with Jinja's ``sort(attribute=)`` on a key their adapter is allowed to leave
null. That raises ``TypeError``, and ``templates/job_detail.html`` includes
the partial through a bare ``{% include tool_results_partial %}``, so the
exception came straight out of ``render_template`` and 500d the whole page.
All six now route through the ``sort_by_number`` filter
(``shared.ranking.sort_by_number``), as does the seventh partial, which had
hand-rolled its own defaulted mirror.

THE TRAP, AND WHY EVERY FIXTURE HERE IS MIXED. Jinja's sort key is a LIST, and
list comparison short-circuits on ``==``. A column where EVERY row is null
therefore sorts fine, and only a column holding at least one real value AND at
least one null raises. A fixture that nulls every row passes against the
UNFIXED template and proves nothing -- a reviewer reported five of the six
affected partials for exactly that reason, having under-populated one tool's
source field. ``test_the_trap_only_a_mixed_column_raises`` pins both halves.

Do not assert on the exception message either: the operand order flips with
which row sorts first. Unfixed, af2 raised "'<' not supported between
instances of 'float' and 'NoneType'" while opendde, which sorts ascending,
said "'NoneType' and 'int'".

Every render here goes through the app's REAL Jinja environment. A test that
built its own ``Environment`` and registered the filter by hand could not
catch the filter going unregistered in ``app.create_app``.
"""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from shared.ranking import sort_by_number

pytestmark = pytest.mark.usefixtures("isolate_supabase")

RESULTS_PARTIALS = Path(__file__).resolve().parents[1] / "templates" / "tools"


# ---------------------------------------------------------------------------
# The seven partials, and the key each one orders candidate rows on
# ---------------------------------------------------------------------------

# (template, the RAW design field the partial reads, a real value for it).
# The sort attribute itself is the partial's own reshape of that field, so it
# is deliberately not repeated here: the point of the fixture is to drive each
# partial the way its adapter does.
PARTIALS: dict[str, tuple[str, str, object]] = {
    "af2": ("tools/af2_results.html", "mean_plddt", 85.1),
    "boltz2": ("tools/boltz2_results.html", "iptm", 0.82),
    "colabfold": ("tools/colabfold_results.html", "mean_plddt", 85.1),
    "esmfold": ("tools/esmfold_results.html", "mean_plddt", 85.1),
    "esmfold2_design": ("tools/esmfold2_design_results.html", "iptm", 0.82),
    "iggm": ("tools/iggm_results.html", "n_epitope_contacts", 7),
    "opendde": ("tools/opendde_results.html", "rank", 0),
}


@pytest.fixture(scope="module")
def env(isolate_supabase_module):
    """The app's REAL Jinja environment, inside a request context."""
    from app import create_app

    app = create_app()
    with app.test_request_context("/"):
        yield app.jinja_env


def _mixed_designs(field: str, value: object) -> list[dict]:
    """Two designs: one carrying ``field``, one carrying null for it.

    MIXED on purpose -- see this module's docstring. ``pdb_key`` is the marker
    the assertions read back, because it is what the candidate table actually
    renders; ``name`` does not reach the markup.
    """
    rows = [
        {"rank": 0, "name": "m", "pdb_key": "measured.pdb"},
        {"rank": 1, "name": "u", "pdb_key": "unmeasured.pdb"},
    ]
    rows[0][field] = value
    rows[1][field] = None
    return rows


def _render(env, template: str, designs: list[dict]) -> str:
    job = types.SimpleNamespace(
        id="job-1", inputs={}, preset=None, result={"designs": designs}
    )
    return env.get_template(template).render(job=job)


def _row_order(html: str) -> list[str]:
    """The ``pdb_key`` of each rendered candidate row, in document order."""
    order = []
    for row in re.findall(r'<tr class="cand-row.*?</tr>', html, re.S):
        hit = re.findall(r"\b(?:un)?measured\.pdb", row)
        order.append(hit[0] if hit else "?")
    return order


# ---------------------------------------------------------------------------
# The page renders
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(PARTIALS))
def test_a_mixed_column_renders_and_sinks_the_unmeasured_row(env, tool):
    """The 500, and the ordering that replaces it, in one assertion.

    Unfixed, this raised for all seven. The row order is asserted as well as
    the absence of the exception, because "it rendered" alone would also pass
    if a partial quietly dropped the rows it could not order.
    """
    template, field, value = PARTIALS[tool]
    html = _render(env, template, _mixed_designs(field, value))
    assert _row_order(html) == ["measured.pdb", "unmeasured.pdb"]


def test_the_trap_only_a_mixed_column_raises(env):
    """Why every fixture above mixes a real value with a null.

    This pins Jinja's own behaviour, not ours: it is the reason an all-null
    fixture is worthless here. If a future Jinja makes the all-null case raise
    too, this fails and the docstrings above need rewording -- they would no
    longer be describing a trap.
    """
    unfixed = env.from_string("{{ rows | sort(attribute='k', reverse=True) | list }}")

    unfixed.render(rows=[{"k": None}, {"k": None}])  # no raise: short-circuits

    with pytest.raises(TypeError):
        unfixed.render(rows=[{"k": 0.8}, {"k": None}])


# ---------------------------------------------------------------------------
# The filter's own contract
# ---------------------------------------------------------------------------

def _names(rows) -> list[str]:
    return [row["n"] for row in rows]


def test_unmeasured_sinks_when_sorting_descending():
    rows = [{"n": "null", "k": None}, {"n": "hi", "k": 0.9}, {"n": "lo", "k": 0.1}]
    assert _names(sort_by_number(rows, "k", reverse=True)) == ["hi", "lo", "null"]


def test_unmeasured_sinks_when_sorting_ascending():
    """The direction opendde sorts in.

    ``reverse=True`` over a (missing, value) tuple would lift the unmeasured
    rows to the top, which is why the descending case negates the value
    instead of passing ``reverse=`` through to ``sorted``.
    """
    rows = [{"n": "null", "k": None}, {"n": "hi", "k": 0.9}, {"n": "lo", "k": 0.1}]
    assert _names(sort_by_number(rows, "k", reverse=False)) == ["lo", "hi", "null"]


def test_a_negative_value_still_outranks_an_unmeasured_row():
    """The hazard in a fixed numeric sentinel.

    esmfold2_design_results.html defaulted its hand-rolled mirror to ``-1``,
    which is safe only while the metric cannot go that low. Nothing here
    depends on the metric's range.
    """
    rows = [{"n": "null", "k": None}, {"n": "neg", "k": -99.0}]
    assert _names(sort_by_number(rows, "k", reverse=True)) == ["neg", "null"]


@pytest.mark.parametrize("direction", [True, False])
def test_ties_keep_input_order(direction):
    """What boltz2_results.html's "designs with equal ipTM keep their
    submission order" relies on. Python's sort is stable in both directions.
    """
    rows = [
        {"n": "first", "k": 0.5},
        {"n": "second", "k": 0.5},
        {"n": "third", "k": 0.5},
    ]
    assert _names(sort_by_number(rows, "k", reverse=direction)) == [
        "first",
        "second",
        "third",
    ]


@pytest.mark.parametrize(
    "unsortable",
    [
        pytest.param(None, id="null"),
        pytest.param("0.99", id="numeric-string"),
        pytest.param(True, id="bool"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(10**400, id="int-too-wide-for-float"),
    ],
)
def test_a_value_that_is_not_a_finite_real_number_is_unmeasured(unsortable):
    """Each of these either cannot order against a float or orders wrongly.

    ``10 ** 400`` is the one that would still raise after a null guard:
    ``float()`` on it raises OverflowError, not TypeError.
    """
    rows = [{"n": "bad", "k": unsortable}, {"n": "good", "k": 0.5}]
    assert _names(sort_by_number(rows, "k", reverse=True)) == ["good", "bad"]


def test_an_absent_key_is_unmeasured_not_an_error():
    rows = [{"n": "absent"}, {"n": "good", "k": 0.5}]
    assert _names(sort_by_number(rows, "k", reverse=True)) == ["good", "absent"]


def test_a_dotted_path_reads_through_the_scores_dict():
    rows = [
        {"n": "lo", "scores": {"ipTM": 0.2}},
        {"n": "hi", "scores": {"ipTM": 0.9}},
        {"n": "none", "scores": {}},
    ]
    assert _names(sort_by_number(rows, "scores.ipTM", reverse=True)) == [
        "hi",
        "lo",
        "none",
    ]


def test_a_non_mapping_partway_down_the_path_is_unmeasured():
    """No getattr fallback: these partials build plain dicts."""
    rows = [{"n": "broken", "scores": None}, {"n": "good", "scores": {"ipTM": 0.5}}]
    assert _names(sort_by_number(rows, "scores.ipTM", reverse=True)) == [
        "good",
        "broken",
    ]


# ---------------------------------------------------------------------------
# No results partial goes back to the raw filter
# ---------------------------------------------------------------------------

def test_every_candidate_sort_in_a_results_partial_uses_the_filter():
    """Scoped to ``templates/tools/*_results.html`` -- the partials
    ``job_detail.html`` includes unguarded -- rather than to all of
    ``templates/``, so a safe ``sort(attribute=)`` over a list with no
    nullable key elsewhere on the site is not banned by this test.

    The second assertion makes the sweep self-checking: a sweep that read
    nothing would satisfy the first assertion on its own.
    """
    raw, filtered = [], []
    for path in sorted(RESULTS_PARTIALS.glob("*_results.html")):
        text = path.read_text(encoding="utf-8")
        if "sort(attribute=" in text:
            raw.append(path.name)
        if "sort_by_number(" in text:
            filtered.append(path.name)

    assert raw == []
    assert filtered == sorted(
        Path(template).name for template, _field, _value in PARTIALS.values()
    )
