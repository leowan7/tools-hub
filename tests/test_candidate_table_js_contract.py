"""The contract between static/js/candidate_table.js, the macro that renders
its DOM, and the server that parses what it posts.

REGISTER ITEM B-3. There is no JS test harness in this repo, so nothing
executes that file. Every identifier below crosses a boundary a rename can
break on one side only, and four such renames were confirmed to survive the
entire suite: `.cand-starred-export`, the submit listener, the posted key
shape, and `shortlist-hint-`. Each one shipped an empty CSV named `_starred`
at HTTP 200 with no error anywhere.

These are SOURCE-level assertions, which this repo's house rule normally
rejects in favour of parsing real output. That rule assumes the output can be
produced; without a JS runtime it cannot, and the alternative to a source check
here is no check at all. Where a real artifact IS reachable these tests use it:
the ref shape below is not string-compared, it is extracted from the JS and
driven through the production parser, and the empty-selection case is asserted
on a live response in tests/test_target_export.py.

Adding a hook to the macro does not require adding it here. Adding one the JS
*reads* does.
"""

import pathlib
import re

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_JS = (_ROOT / "static" / "js" / "candidate_table.js").read_text(encoding="utf-8")
_TPL = (_ROOT / "templates" / "components" / "candidate_table.html").read_text(
    encoding="utf-8")


# (token as the JS EXECUTES it, token as the template EMITS it).
#
# The JS side is deliberately the executable form -- `dataset.refIdx`, not the
# `data-ref-idx` its header comment also mentions. A comment matching keeps a
# broken rename green, which is the failure mode this file exists to catch.
_HOOKS = [
    ("'.cand-starred-export'", "cand-starred-export"),
    ("'.star-btn'", "star-btn"),
    ("'.shortlist-review'", "shortlist-review"),
    ("'[data-cand-table-id]'", "data-cand-table-id"),
    ("'shortlist-count-'", "shortlist-count-"),
    ("'shortlist-hint-'", "shortlist-hint-"),
    ("dataset.scope", "data-scope"),
    ("dataset.job", "data-job"),
    ("dataset.refIdx", "data-ref-idx"),
    ("'th[data-col]'", "data-col"),
    ('[name="refs"]', 'name="refs"'),
    ('[name="candidate_refs"]', 'name="candidate_refs"'),
    ('[name="candidate_indices"]', 'name="candidate_indices"'),
]


@pytest.mark.parametrize("js_token,tpl_token", _HOOKS)
def test_every_dom_hook_the_js_reads_is_emitted_by_the_template(js_token, tpl_token):
    assert js_token in _JS, f"{js_token} vanished from candidate_table.js"
    assert tpl_token in _TPL, (
        f"candidate_table.js reads {js_token}, but the macro no longer emits "
        f"{tpl_token}. The JS will silently find nothing.")


def test_the_starred_export_form_registers_a_submit_handler():
    """The hidden `refs` field ships as `value="[]"` and is filled at submit
    time, because a value stamped at render time would be whatever was starred
    on the PREVIOUS page load. Drop the listener and the form still posts, still
    returns 200, and carries the render-time empty array.
    """
    block = _JS.split("'.cand-starred-export'", 1)
    assert len(block) == 2, "the starred-export block is gone entirely"
    # Bounded to the block that follows the selector, so a `submit` listener
    # somewhere else in the file cannot stand in for this one.
    following = block[1][:600]
    assert "addEventListener('submit'" in following, following[:200]


def _posted_ref_keys():
    """The two keys candidate_table.js actually puts on the wire.

    Both the starred export and the lab-submit modal build the same literal,
    so this asserts there is exactly one shape in the file rather than picking
    the first and letting the other drift.
    """
    found = re.findall(
        r"return\s*\{\s*(\w+)\s*:\s*r\.j\s*,\s*(\w+)\s*:\s*r\.i\s*\}", _JS)
    assert found, "no {job_id, index} literal found in candidate_table.js"
    assert len(set(found)) == 1, f"two different ref shapes on the wire: {found}"
    return found[0]


def test_the_ref_shape_the_js_posts_is_the_shape_the_server_parses():
    """Not a string comparison: the keys are lifted out of the JS and driven
    through the production parser. Emitting `{j, i}` -- the sessionStorage
    shape, one careless edit away -- makes this construct `{"j":..., "i":...}`,
    which `_parse_candidate_refs` drops entirely, so the assertion fails on the
    real consequence rather than on a diff.
    """
    import json

    from blueprints.lab_projects import _parse_candidate_refs

    job_key, idx_key = _posted_ref_keys()
    payload = json.dumps([{job_key: "job-abc", idx_key: 3}])
    assert _parse_candidate_refs(payload) == [{"job_id": "job-abc", "index": 3}]


def test_the_server_parser_really_would_drop_the_sessionstorage_shape():
    """The pair. If `_parse_candidate_refs` accepted anything, the test above
    would pass under every mutation and prove nothing."""
    import json

    from blueprints.lab_projects import _parse_candidate_refs

    assert _parse_candidate_refs(json.dumps([{"j": "job-abc", "i": 3}])) == []
