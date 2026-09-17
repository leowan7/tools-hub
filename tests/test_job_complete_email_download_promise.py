"""The completion email must not promise a download it has not checked for.

``shared/email.py::_result_summary`` ends a successful composite-tool mail with
"N candidates returned with real scores and downloadable structures." The count
is read from ``candidate_records``. The download clause was read from nothing:
it was appended to every candidate list, whatever the rows carried. #261 made
the clause conditional. This file now also pins its NUMBER, because a result
where only SOME rows carry a structure kept the clause and kept overstating
how many rows it covered.

WHAT THE PAGE ACTUALLY GATES ON. ``templates/components/candidate_table.html``
sets ``has_pdb`` from ``use_url or has_b64`` -- i.e. from ``pdb_key`` and
``pdb_content_b64``, and renders an em dash in the View-3D and .pdb columns
when neither is present. The mail's check is those two keys and no others, so
where a page hands the macro its stored DICT rows unchanged, and is not the
worked example, the mail can never promise where that column abstains and its
count IS that column's population. Three carve-outs sit behind those
qualifiers. NONE OF THEM IS A KNOWN LIVE DIVERGENCE -- which is not the same
as saying no mail reaches those tools; jobs on the third one's six tools
do send mail, and simply cannot carry the shape that would diverge:

  * the worked example sets ``is_example``, which suppresses the pdb_key leg
    of the gate. It has no tool_jobs row, so no mail describes it;
  * a NON-DICT row is counted by the mail (which guards with isinstance) and
    raises in the macro. The whole render aborts -- the results page 500s
    rather than showing a shorter table -- so there is no column to compare
    against. Nothing in the repo is known to produce such a row;
  * six results templates (templates/tools/{af2,boltz2,colabfold,esmfold,
    iggm,opendde}_results.html) rebuild each row and drop pdb_content_b64 on
    the way, so there the column is gated on pdb_key alone. Latent rather
    than live -- nothing writes that key onto a ``designs`` row -- and
    recorded in the comment in ``shared/email.py::_result_summary``. NOT
    esmfold2_design: its reshape looks like theirs but sits in the
    ``{% else %}`` of ``{% if output_candidates %}``, and the current
    pipeline writes ``candidates`` 1:1 with ``designs``, so no ROW it
    produces today is reshaped. (A zero-design payload does reach that
    branch; it reshapes nothing there.) The branch is for stored payloads
    predating the candidates contract, which CAN be non-empty.

This file renders the stored rows through the macro directly, so it models
none of the three.

NEITHER BRANCH IS A REPAIR OF AN OBSERVED MAIL. The structureless row is a
real shape -- ``tools/proteina/run_pipeline.py`` pops ``pdb_key`` from an
inline-capped design in its ``n_inline_capped`` branch, and neither of the two
writes of ``pdb_content_b64`` in that file runs on that path (one is the other
leg of the same if/else, one is the ``rescue_inline`` branch, which needs an
upload endpoint) -- but no in-repo producer puts it in front of this code:

  * that branch needs ``inline_pdbs``, which is ``_inline_enabled() and not
    upload_endpoint``, and the hub sets ``_upload_urls_endpoint``
    unconditionally (blueprints/tools.py and shared/compute_campaigns.py).
    A hub-shaped payload without one is refused before the GPU, which is a
    failed job, not a success;
  * a run where the cap admits NOTHING is failed outright -- ``n_structures ==
    0`` yields ``inline_cap_admitted_nothing`` and sets
    ``result["status"] = "FAILED"`` -- so it takes the failed branch of
    _result_summary, not this one;
  * boltz2, iggm and opendde all ``continue`` past a failed upload rather
    than emit a keyless row.

So the residue #261 left open was the PARTLY-capped result, which kept saying
"downloadable structures" over all N rows while the table served fewer. THAT
IS WHAT THIS CHANGE FIXES: the clause now carries its own count, read off the
same two keys, and
``test_the_mails_structure_count_is_what_the_page_will_serve`` checks that
count against what the shipped macro rendered rather than against a literal.

AND IT IS A GUARD TOO. #261 called the partial "the REACHABLE residue" and an
earlier draft of this file repeated it. Reviewing the fix did not bear that
out: inside proteina's inline mode a partial cap is the natural outcome and
the all-capped run is failed outright, but inline mode needs ``not
upload_endpoint`` and every path that can submit a proteina job sets one. On
the upload path a failed upload EITHER takes ``rescue_inline``, which keeps
``pdb_key`` and adds ``pdb_content_b64``, OR -- when the bytes are empty or
will not fit the remaining inline budget -- ``continue``s and emits no row
at all. Both keys or no row; never a keyless one. So the partial sits in the
same bucket as the
no-structure branch: wrong in principle, with no in-repo producer named for
either. The five container-side tools
(pxdesign, rfdiffusion, bindcraft, boltzgen, rfantibody) have no
``run_pipeline.py`` in this repo, so their candidate shape is UNREAD -- whether
either branch is ever live depends on them.

THE PREMISE IS RENDERED, NOT ASSERTED.
``test_the_page_offers_no_per_row_download_for_the_same_payload`` puts the very
payload the mail describes through the real macro in the app's real Jinja
environment and counts controls. Note its scope: it measures the PER-ROW
controls only. The export bar above the table carries a "Structures (ZIP)"
link (#252 renamed it from "PDBs (ZIP)"), gated on ``not is_example`` rather
than on any row's structure. Separate surface, untouched here.

NO OUTBOUND MAIL IS SENT. ``send_job_complete_email`` posts through
``requests.post`` in its own body (it does NOT use ``_post_resend``, which is a
different sender), and ``requests`` is the only network import in that module.
Every sender test
below patches the module attribute all of its call sites resolve through.
"""

from __future__ import annotations

import base64
import re
import uuid
from html.parser import HTMLParser
from unittest.mock import patch

import pytest

from shared import email as email_mod
from shared.jobs import ToolJob

pytestmark = pytest.mark.usefixtures("isolate_supabase")

# One word of the clause, not the clause: the negative assertions want to fail
# on any rewording that still offers a download, not only on the exact
# sentence. The positive assertions below pin the full string instead.
PROMISE = "downloadable"

# One real ATOM record, so the inline-b64 render path has something
# ``bfactors_on_100_b64`` can parse rather than a fixture that only proves the
# template survives garbage.
_PDB_TEXT = (
    "ATOM      1  N   MET A   1      11.104  13.207  10.000"
    "  1.00 88.31           N"
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("ascii")).decode("ascii")


def _scores(iptm: float) -> dict:
    return {
        "total_reward": 0.81,
        "af2_iptm": iptm,
        "af2_plddt": 88.0,
        "binder_scrmsd": 1.42,
    }


def _capped_row(rank: int) -> dict:
    """A proteina design the inline byte cap dropped the atoms of.

    Its TOP-LEVEL keys are the ``candidate_entry`` that file builds
    (the ``candidate_entry`` dict) minus the two its ``n_inline_capped``
    branch removes or never writes -- leaving rank, name, target_numbering,
    scores. The nested ``scores`` is a SUBSET of the real one (that carries six
    columns, including rf3_score and cluster_id); nothing here reads those, and
    the fixture does not claim to reproduce them.
    """
    return {
        "rank": rank,
        "name": f"design_{rank}",
        "target_numbering": "input",
        "scores": _scores(0.70 + rank / 100),
    }


def _url_row(rank: int) -> dict:
    """A delivered design: Storage-backed, reached by pdb_key."""
    row = _capped_row(rank)
    row["pdb_key"] = f"designs/design_{rank}.pdb"
    return row


def _inline_row(rank: int) -> dict:
    """A delivered design on the OTHER leg: inline bytes, no key.

    Separate from ``_url_row`` because the page's gate is a two-term ``or`` and
    a check written against ``pdb_key`` alone would pass every test that only
    ever supplies the first term.
    """
    row = _capped_row(rank)
    row["pdb_content_b64"] = _b64(_PDB_TEXT)
    return row


def _job(result: dict, *, tool: str = "proteina") -> ToolJob:
    return ToolJob.from_row({
        "id": "6f2d7c10-9d6b-4a2e-9f41-0c9a3b7e5d22",
        "user_id": str(uuid.uuid4()),
        "tool": tool,
        "preset": "pilot",
        "status": "succeeded",
        "inputs": {},
        "result": result,
        "error": None,
        "modal_function_call_id": "fc-stub-x",
        "job_token": "t" * 64,
        "gpu_seconds_used": 900,
        "created_at": "2026-09-01T12:00:00Z",
        "started_at": "2026-09-01T12:00:01Z",
        "completed_at": "2026-09-01T12:30:00Z",
    })


def _sent(job: ToolJob) -> dict:
    """The exact payload the provider would receive; nothing leaves the process."""
    captured: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "resend-stub"}

    def _post(url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return _Resp()

    with patch.dict("os.environ", {"RESEND_API_KEY": "test-key"}), \
            patch("shared.email.requests.post", side_effect=_post):
        assert email_mod.send_job_complete_email(
            user_email="u@example.com", job=job,
        ) is True
    assert captured, "nothing was sent"
    return captured


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data):
        self.chunks.append(data)


def _bodies(payload: dict) -> dict:
    """What the user READS, per part.

    The HTML part is un-escaped through the parser rather than searched as
    source: ``select_autoescape`` covers .html and not .txt (shared/email.py),
    so a raw substring assertion over the two parts would not be testing the
    same string twice.
    """
    parser = _Text()
    parser.feed(payload["html"])
    return {
        "html": " ".join("".join(parser.chunks).split()),
        "text": " ".join(payload["text"].split()),
    }


def _mail(result: dict, *, tool: str = "proteina") -> dict:
    return _bodies(_sent(_job(result, tool=tool)))


# ---------------------------------------------------------------------------
# The premise: the page really does refuse these rows a per-row download
# ---------------------------------------------------------------------------

@pytest.fixture
def env(isolate_supabase):
    """The app's REAL Jinja environment, inside a request context.

    Not a hand-built Environment: the claim being checked is about what ships.

    FUNCTION-SCOPED, AND TAKING ``isolate_supabase`` BY NAME. As a module-scoped
    fixture it was instantiated BEFORE the function-scoped mark at the top of
    this file could blank the environment, because pytest fills higher scopes
    first -- so ``create_app()`` ran against whatever ``load_dotenv()`` found.
    ``DOTENV_PATH`` does not protect it: that name appears nowhere in this repo,
    and app.py calls bare ``load_dotenv()``, which walks up to the real .env
    above the worktree. Three test functions take it, one of them parametrized
    to two cases, so create_app() runs four times for this file -- few enough
    that the per-test rebuild is not worth widening the scope for.
    """
    from app import create_app

    app = create_app()
    with app.test_request_context("/"):
        yield app.jinja_env


def _render_table(env, candidates):
    tmpl = env.from_string(
        "{% from 'components/candidate_table.html' import candidate_table %}"
        "{{ candidate_table(candidates, columns, 'job-1', 'proteina') }}"
    )
    return tmpl.render(
        candidates=candidates,
        columns=["af2_iptm", "af2_plddt", "binder_scrmsd"],
    )


def _per_row_controls(html: str) -> dict:
    """How many PER-ROW structure controls the table rendered.

    ``download=`` counts only the per-row anchors: the three export-bar links
    (the cand-export-btns bar, including "Structures (ZIP)") carry no such
    attribute, so they cannot inflate this. That is a deliberate limit, not an
    oversight -- the bulk export is a separate surface this file does not test.
    """
    return {
        "view3d": len(re.findall(r"view3d-btn", html)),
        "download": len(re.findall(r"<a[^>]*\sdownload=", html)),
    }


def test_the_page_offers_no_per_row_download_for_the_same_payload(env):
    """Renders the mail's own payload through the shipped macro.

    Two controls, both counted, because the page's gate gives a row either both
    or neither and a test watching one of them would miss half a regression.
    """
    capped = _per_row_controls(_render_table(env, [_capped_row(i) for i in range(3)]))
    assert capped == {"view3d": 0, "download": 0}, (
        "the premise no longer holds: the candidate table now serves a "
        f"per-row structure for rows carrying neither key: {capped}"
    )

    # The control. Without it, a macro that rendered NO controls for any input
    # -- a broken import, an exception swallowed by Jinja -- would satisfy the
    # assertion above while measuring nothing.
    delivered = _per_row_controls(_render_table(env, [_url_row(i) for i in range(3)]))
    assert delivered == {"view3d": 3, "download": 3}, delivered

    inline = _per_row_controls(_render_table(env, [_inline_row(i) for i in range(2)]))
    assert inline == {"view3d": 2, "download": 2}, inline


def test_the_page_ignores_a_bare_pdb_b64_row(env):
    """Why the mail's check is two keys and not three.

    ``pdb_b64`` is a ROOT-level field on the structure-prediction tools (af2,
    colabfold, esmfold), consumed earlier in _result_summary. The table never
    reads it per row, so accepting it there would promise a download this
    column abstains from -- reintroducing the defect through a wider read.
    """
    only_b64 = [{"rank": 0, "name": "d0", "scores": _scores(0.8),
                 "pdb_b64": _b64(_PDB_TEXT)}]
    assert _per_row_controls(_render_table(env, only_b64)) == {
        "view3d": 0, "download": 0,
    }


# ---------------------------------------------------------------------------
# The email
# ---------------------------------------------------------------------------

def test_no_structure_anywhere_drops_the_download_promise():
    """The defect: the clause used to be unconditional."""
    bodies = _mail({"candidates": [_capped_row(i) for i in range(3)]})
    for part, body in bodies.items():
        assert PROMISE not in body, (
            f"the {part} body promises a download for a result whose rows "
            f"carry no structure: {body!r}"
        )


def test_the_count_survives_when_the_promise_is_dropped():
    """Only the download clause is conditional; the run is still reported.

    A fix that returned a bare "see the job page" would pass the test above and
    silently stop telling the customer how many designs they got.
    """
    bodies = _mail({"candidates": [_capped_row(i) for i in range(3)]})
    for part, body in bodies.items():
        assert "3 candidates returned with real scores" in body, (part, body)


def test_a_bare_pdb_b64_row_is_not_promised_a_download():
    """The email half of ``test_the_page_ignores_a_bare_pdb_b64_row``.

    Pins the two-key read. A third term here would pass every other test in
    this file while promising a download the page renders as an em dash.
    """
    bodies = _mail({"candidates": [{"rank": 0, "name": "d0",
                                    "scores": _scores(0.8),
                                    "pdb_b64": _b64(_PDB_TEXT)}]})
    for part, body in bodies.items():
        assert "1 candidate returned with real scores" in body, (part, body)
        assert PROMISE not in body, (part, body)


def test_the_zero_count_branch_above_this_one_still_owns_its_case():
    """#252's branch, pinned because THIS change now sits directly below it.

    ``_result_summary`` returns "Your run finished. The results are on the job
    page." when ``candidate_records`` reads no rows, which happens on a
    succeeded job whose payload has an unrecognised shape -- ``_is_empty_result``
    defaults such a shape to success rather than calling it empty.

    Nothing in the repo asserted that sentence: deleting the branch left the
    file green, because execution then falls into the code below and produces
    a plausible "0 candidates returned ..." line. Harmless today, but the two
    blocks are now adjacent, so an edit to the lower one could silently eat
    the upper one. This is the boundary marker.
    """
    bodies = _mail({"an_unrecognised_shape": [1, 2, 3]})
    for part, body in bodies.items():
        assert "Your run finished. The results are on the job page." in body, (
            part, body,
        )
        assert "0 candidate" not in body, (part, body)


def test_a_storage_backed_row_keeps_the_promise():
    """No over-correction: a result with real downloads still says so."""
    bodies = _mail({"candidates": [_url_row(i) for i in range(2)]})
    for part, body in bodies.items():
        assert "2 candidates returned with real scores and downloadable structures." in body, (
            part, body,
        )


def test_an_inline_only_row_keeps_the_promise():
    """The second leg of the page's ``or``: inline bytes, no pdb_key.

    A check written against ``pdb_key`` alone would strip the promise from
    every smoke/mini-pilot result, which does have downloads.
    """
    bodies = _mail({"candidates": [_inline_row(0)]})
    for part, body in bodies.items():
        assert "1 candidate returned with real scores and downloadable structures." in body, (
            part, body,
        )


def test_the_designs_shape_is_read_too():
    """boltz2 and iggm store ``designs[]`` and carry no ``candidates`` key.

    ``candidate_records`` reads both keys, so a structure check that looked
    at ``result["candidates"]`` directly would strip the promise from those
    tools' mails -- they set pdb_key on every emitted row. Not
    esmfold2_design, whose docstring in shared/jobs.py records that it emits
    BOTH lists, so it is reached either way.
    """
    bodies = _mail({"designs": [_url_row(i) for i in range(2)]}, tool="boltz2")
    for part, body in bodies.items():
        assert "downloadable structures." in body, (part, body)

    capped = _mail({"designs": [_capped_row(i) for i in range(2)]}, tool="boltz2")
    for part, body in capped.items():
        assert PROMISE not in body, (part, body)


@pytest.mark.parametrize(("rows", "sentence"), [
    pytest.param(
        [_url_row(0), _capped_row(1), _inline_row(2), _capped_row(3)],
        "4 candidates returned with real scores; "
        "{m} with downloadable structures.",
        id="both-legs-delivered",
    ),
    pytest.param(
        [_url_row(0), _capped_row(1), _capped_row(2)],
        "3 candidates returned with real scores; "
        "{m} with a downloadable structure.",
        id="one-delivered",
    ),
])
def test_the_mails_structure_count_is_what_the_page_will_serve(env, rows, sentence):
    """THE NUMBER IS MEASURED OFF THE PAGE, not written down here.

    Replaces the test named ``..._still_overstates_and_that_residue_is_known``,
    which pinned the ANY-not-all residue #261 left open and said in its own
    docstring that a later change narrowing the claim must rewrite it. This is
    that change, so the old test is gone rather than skipped.

    ``{m}`` is filled from the per-row download controls the shipped macro
    rendered for THIS payload, so the test fails in both directions: if the mail
    counts rows the table refuses, and if the table starts serving rows the mail
    does not count. The wording around ``{m}`` is a literal on purpose --
    deriving that too would re-implement the production expression and assert
    nothing about it. The two cases differ in the noun, which is why both are
    here: a fix that always wrote the plural would read "1 with downloadable
    structures" for a row that has exactly one.

    BOTH OF THE PAGE'S TWO KEYS appear in the first payload, one per row --
    _url_row carries ``pdb_key``, _inline_row carries ``pdb_content_b64``, and
    no fixture in this file carries both -- so a count written against
    ``pdb_key`` alone undercounts here rather than passing.
    """
    html = _render_table(env, rows)
    offered = _per_row_controls(html)
    assert offered["view3d"] == offered["download"], (
        "the page's two structure controls disagree, so neither of them is the "
        f"number this mail should be matching: {offered}"
    )

    # THE SENTENCE CARRIES TWO NUMBERS AND BOTH ARE THE PAGE'S. The one
    # below is the structure count; this is the row count, and without it a
    # table that quietly stopped rendering structureless rows would leave
    # "N candidates returned" unguarded while every assertion here passed.
    # Not hypothetical: the macro already renders a "{shown} of {total}
    # shown" group count, and shared/ranking.py::build_tool_stats defines
    # ``shown`` as "rows of this tool in the selected (capped) set" against a
    # ``total`` before any cap -- so a table showing fewer rows than the
    # payload holds is a shape this macro already models for merged tables.
    # Anchored on the row's own class, which the macro writes once per
    # candidate as ``cand-row``/``cand-row cand-row-top`` -- ``cand-group-row``
    # and ``viewer-row`` do not match, and job_detail.html's
    # ``live-cand-row`` is a JS className, not a <tr class=" opener. The
    # trailing [ "] pins the class boundary: without it a future
    # ``cand-row-empty`` placeholder row would be counted as a candidate.
    # The macro's own <style> block is part of the rendered string and does
    # contain ``.cand-table .cand-row``, but no CSS text matches the <tr
    # class=" prefix -- a zero-row render scores 0.
    rendered_rows = len(re.findall(r'<tr class="cand-row[ "]', html))
    assert rendered_rows == len(rows), (
        f"the table rendered {rendered_rows} of {len(rows)} candidate rows, "
        f"so the mail's '{len(rows)} candidates returned' is no longer what "
        "the page shows"
    )

    m = offered["download"]
    # The payload has to be PARTIAL for any of this to be about the partial
    # case. A macro that served every row -- or none -- would leave the
    # assertions below pinning a case other tests in this file already cover.
    assert 0 < m < len(rows), (
        f"not a partial delivery: the table served {m} of {len(rows)} rows",
        offered,
    )

    bodies = _mail({"candidates": rows})
    overstated = (
        f"{len(rows)} candidates returned with real scores and "
        "downloadable structures"
    )
    for part, body in bodies.items():
        assert sentence.format(m=m) in body, (part, body)
        # The defect itself: the clause that covered every row.
        assert overstated not in body, (part, body)


def test_a_non_dict_row_cannot_crash_the_mail():
    """The structure check runs over whatever the result holds.

    ``candidate_records`` returns the stored list unfiltered,
    so a malformed row reaches this code, and ``_result_summary`` is called from
    ``_job_complete_template_context`` OUTSIDE the template try/except -- an
    AttributeError here is a job whose customer is never told it finished.
    """
    bodies = _mail({"candidates": ["not-a-dict", None, _capped_row(2)]})
    for part, body in bodies.items():
        assert "3 candidates returned with real scores" in body, (part, body)
        assert PROMISE not in body, (part, body)
