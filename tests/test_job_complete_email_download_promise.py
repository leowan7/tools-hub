"""The completion email must not promise a download it has not checked for.

``shared/email.py::_result_summary`` ends a successful composite-tool mail with
"N candidates returned with real scores and downloadable PDBs." The count is
read from ``candidate_records``. The download clause was read from nothing: it
was appended to every candidate list, whatever the rows carried.

WHAT THE PAGE ACTUALLY GATES ON. ``templates/components/candidate_table.html``
sets ``has_pdb = use_url or has_b64`` from ``pdb_key`` and
``pdb_content_b64``, and renders an em dash in the View-3D and .pdb columns
when neither is present. The mail's check is those two keys and no others, so
it can never promise where that column would abstain.

THIS IS A GUARD, NOT A REPAIR OF AN OBSERVED MAIL. The structureless row is a
real shape -- ``tools/proteina/run_pipeline.py`` pops ``pdb_key`` from an
inline-capped design in its ``n_inline_capped`` branch, and the one write of
``pdb_content_b64`` is the other leg of that same if/else -- but no in-repo
producer puts it in front of this code:

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

So the reachable residue is the PARTLY-capped result, which still says
"downloadable PDBs" and still overstates how many rows carry one. That is not
fixed here; see the test named for it. The five container-side tools
(pxdesign, rfdiffusion, bindcraft, boltzgen, rfantibody) have no
``run_pipeline.py`` in this repo, so their candidate shape is UNREAD -- whether
the guarded branch is ever live depends on them.

THE PREMISE IS RENDERED, NOT ASSERTED.
``test_the_page_offers_no_per_row_download_for_the_same_payload`` puts the very
payload the mail describes through the real macro in the app's real Jinja
environment and counts controls. Note its scope: it measures the PER-ROW
controls only. The export bar above the table carries an unconditional
"PDBs (ZIP)" link, which is a separate surface and is untouched here.

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
    above the worktree. One test uses this, so the per-test rebuild is free.
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
    (the cand-export-btns bar, including "PDBs (ZIP)") carry no such
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


def test_a_storage_backed_row_keeps_the_promise():
    """No over-correction: a result with real downloads still says so."""
    bodies = _mail({"candidates": [_url_row(i) for i in range(2)]})
    for part, body in bodies.items():
        assert "2 candidates returned with real scores and downloadable PDBs." in body, (
            part, body,
        )


def test_an_inline_only_row_keeps_the_promise():
    """The second leg of the page's ``or``: inline bytes, no pdb_key.

    A check written against ``pdb_key`` alone would strip the promise from
    every smoke/mini-pilot result, which does have downloads.
    """
    bodies = _mail({"candidates": [_inline_row(0)]})
    for part, body in bodies.items():
        assert "1 candidate returned with real scores and downloadable PDBs." in body, (
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
        assert "downloadable PDBs." in body, (part, body)

    capped = _mail({"designs": [_capped_row(i) for i in range(2)]}, tool="boltz2")
    for part, body in capped.items():
        assert PROMISE not in body, (part, body)


def test_a_partly_capped_result_still_overstates_and_that_residue_is_known():
    """Pins ANY, not all -- and this is the REACHABLE case, not a corner.

    Every proteina state that reaches a success mail has at least one row with
    a structure (the all-capped run is failed outright; see this file's
    docstring), so a partly-delivered result is the only shape where the
    sentence is still wrong. It says "downloadable PDBs" over N rows when
    fewer than N carry one. Narrowing the claim to the delivered subset needs a
    second count and was explicitly out of scope for this change.

    This test exists to make that residue visible and re-decidable, NOT to
    bless it: a later change that narrows the claim fails here and must delete
    or rewrite this test deliberately.
    """
    bodies = _mail({"candidates": [_url_row(0), _capped_row(1), _capped_row(2)]})
    for part, body in bodies.items():
        assert "3 candidates returned with real scores and downloadable PDBs." in body, (
            part, body,
        )


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
