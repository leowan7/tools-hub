"""Regression: the completion email must not headline a design its bar rejects.

SURFACE THREE, AND THE ONE THAT ARRIVES UNPROMPTED. #241 fixed this class in
three writers on one tool; #248 fixed /jobs/compare and listed the completion
email among the surfaces it did not reach. Both of those are PULLED -- a
customer meets them by opening the site. This one is pushed:
``shared/jobs.complete_job`` mails it the moment a run finishes, so it reaches
a customer who never asked to look.

Real completed job ``2b917b54-0871-44af-a3d1-5d07ea5dcaeb`` (esmfold2-design,
PD-L1 minibinder, n_seeds=2), whose result ships verbatim as this tool's worked
example:

    seed0: ipTM 0.9556, pI 11.95 -> the pipeline drops it
    seed1: ipTM 0.9354, pI  5.67 -> clears the bar

``shared/email.py::_top_candidate_summary`` took ``result["candidates"][0]``
blind and captioned it from the first scored column with a registered legend,
so the mail sent "ipTM 0.956" above "0.75 or more is a credible designed
interface" -- about a poly-Leu/Arg scaffold at pI ~12 that is insoluble and
non-specific, and that the run itself dropped.

THE FIXTURE IS THE SHIPPED EXAMPLE PAYLOAD, read off disk, not a transcription
of its numbers. The class is about what a real container stored, and a copy is
one rewording away from agreeing with a fix that does not work on the real
shape. ``test_the_example_payload_still_carries_the_defect`` asserts the
premise, so an example that stops carrying it fails loudly here instead of
leaving every assertion below hollow.

EVERY CHECK GOES THROUGH ``send_job_complete_email`` WITH THE TRANSPORT
CAPTURED and reads the two bodies a customer receives. The defect lived in the
wiring, not in any one function: the record is chosen in ``shared.jobs``, judged
in ``shared.score_legends``, assembled in ``shared.email`` and formatted in two
templates. A function-level test of the chooser stays green while the context
builder keeps handing the templates the old value.
"""
from __future__ import annotations

import json
import uuid
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

from shared import email as email_mod
from shared.jobs import ToolJob
from shared.score_legends import judge

# The email path never touches Supabase, but ``shared.jobs`` imports the client
# module; blanking the env keeps that honest, as every sibling suite does.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

_EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "tools" / "esmfold2_design" / "example" / "result.json"
)

# As ``_top_candidate_summary`` formats them ("%.3f"), which is what a search of
# the delivered body has to look for. The reject and the design that clears the
# bar differ in the THIRD DECIMAL of the same column, so a shorter needle would
# match both and the assertions below would span the defect and its fix.
DROP_IPTM = "0.956"
PASS_IPTM = "0.935"
DROP_PDB = "seed0_design_0_complex.pdb"
PASS_PDB = "seed1_design_0_complex.pdb"

# The mode the run actually was, recorded on the RESULT as ``is_antibody:
# false``. Named because two tests below disagree with the job's stored preset
# on purpose.
RUN_MODE = "minibinder"


def _example_result() -> dict:
    return json.loads(_EXAMPLE.read_text("utf-8"))


def _job(*, preset: str = RUN_MODE, result: dict | None = None) -> ToolJob:
    return ToolJob.from_row({
        "id": "2b917b54-0871-44af-a3d1-5d07ea5dcaeb",
        "user_id": str(uuid.uuid4()),
        "tool": "esmfold2-design",
        "preset": preset,
        "status": "succeeded",
        "inputs": {},
        "result": _example_result() if result is None else result,
        "error": None,
        "modal_function_call_id": "fc-stub-x",
        "job_token": "t" * 64,
        "gpu_seconds_used": 120,
        "created_at": "2026-09-01T12:00:00Z",
        "started_at": "2026-09-01T12:00:01Z",
        "completed_at": "2026-09-01T12:30:00Z",
    })


def _sent(job: ToolJob) -> dict:
    """The exact payload the provider would receive."""
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
    source: ``select_autoescape`` covers .html and not .txt, so a sentence with
    an apostrophe reaches the HTML body as ``&#39;`` and a raw substring
    assertion would fail -- or pass a mutation for an escaping reason that has
    nothing to do with the claim under test.
    """
    parser = _Text()
    parser.feed(payload["html"])
    return {
        "html": " ".join("".join(parser.chunks).split()),
        "text": " ".join(payload["text"].split()),
    }


def test_the_example_payload_still_carries_the_defect():
    """The premise every assertion in this file rests on.

    Without it, an example edited to drop the high-pI design would leave the
    tests below passing while checking nothing: "the mail does not say 0.956"
    is trivially true of a payload that no longer contains 0.956.
    """
    result = _example_result()
    cands = result["candidates"]
    assert len(cands) == 2, cands
    first, second = (c["scores"] for c in cands)

    # The stored order is the container's ranking key, and it ranks the reject
    # FIRST. That is the whole reason a reader may not take candidates[0].
    assert first["ipTM"] > second["ipTM"], (first["ipTM"], second["ipTM"])
    assert first["pI"] > 6 >= second["pI"], (first["pI"], second["pI"])

    # And the derived bar agrees with the pipeline's own filter_status, so the
    # mail is not being asked to second-guess the tool that produced the run.
    assert first["filter_status"] == "drop"
    assert second["filter_status"] == "strict_pass"
    assert judge("esmfold2-design", cands[0], preset=RUN_MODE).verdict == "below"
    assert judge("esmfold2-design", cands[1], preset=RUN_MODE).verdict == "meets"

    # The mode is a fact ON THE RESULT. It is what lets this mail resolve a bar
    # at all for a tool deliberately absent from GATE_COLUMNS.
    assert result["is_antibody"] is False


def test_the_mail_leads_with_the_design_that_clears_the_bar():
    """0.956 is the number this mail used to send. It may not appear at all.

    Not merely "0.935 is present": the defect and the fix both put an ipTM in
    the callout, so only the ABSENCE of the reject's number tells them apart.
    """
    bodies = _bodies(_sent(_job()))
    for part, body in bodies.items():
        assert PASS_IPTM in body, (
            f"the {part} body does not carry the design that clears the bar"
        )
        assert DROP_IPTM not in body, (
            f"the {part} body still headlines the design the tool's own bar "
            f"drops on pI 11.95: {body!r}"
        )
        # The PDB filename travels with the number and is what the customer
        # downloads. Both halves have to move together or the mail interprets
        # one design and names another.
        assert PASS_PDB in body, f"the {part} body names no design file"
        assert DROP_PDB not in body, (
            f"the {part} body still points at the rejected design's PDB"
        )


def test_the_mail_states_the_bar_the_leading_design_meets():
    """The legend says what a good ipTM is; only the verdict says this IS one.

    Rendered through ``score_legends.verdict_text``, so the sentence names the
    bar it asserts rather than the bare word "Meets" -- which is what that
    renderer returns instead when a moded tool's mode goes missing.
    """
    bodies = _bodies(_sent(_job()))
    for part, body in bodies.items():
        assert "Meets" in body, f"the {part} body renders no judgement"
        # BOTH legs, so a bar that silently loses one is caught. pI is the leg
        # the leading design is here for.
        assert "pI 6" in body, f"the {part} body's bar drops its pI leg"
        assert "ipTM 0.75" in body, f"the {part} body's bar drops its ipTM leg"


def test_the_mode_comes_off_the_result_not_the_stored_preset():
    """A stored preset that disagrees with the run must not decide the bar.

    esmfold2-design's bar is keyed on (tool, mode) and "scfv" has no entry, so
    resolving the mode from ``job.preset`` alone leaves this run unjudged, the
    headline falls back to record 0, and the reject is mailed again. The result
    records what actually ran (``is_antibody: false``); the preset is a stored
    string. Same order ``blueprints/jobs.jobs_compare`` and
    ``templates/tools/esmfold2_design_results.html`` resolve it in.
    """
    bodies = _bodies(_sent(_job(preset="scfv")))
    for part, body in bodies.items():
        assert PASS_IPTM in body, (
            f"the {part} body read the stored preset instead of the result, so "
            f"the run was judged against no bar and record 0 headlined it"
        )
        assert DROP_IPTM not in body, body


def test_when_nothing_clears_the_bar_the_mail_says_so():
    """The chooser cannot rescue a run with no passing design -- the copy must.

    ``headline_candidate`` falls back to a cleanly measured shortfall when no
    record qualifies, so the callout then carries a design that FAILED, inside
    an endorsement frame ("Top design", a green rule), under a legend explaining
    what a good value means. Read together that is a recommendation.
    """
    result = _example_result()
    for cand in result["candidates"]:
        cand["scores"]["pI"] = 11.9
    bodies = _bodies(_sent(_job(result=result)))
    for part, body in bodies.items():
        assert "clears the bar" in body, (
            f"the {part} body captions a design that fails the bar without "
            f"saying it fails: {body!r}"
        )
        # The measurement, not just the framing: which leg fell short, and
        # against what. A bare "did not clear the bar" leaves nothing to act on.
        assert "above 6" in body, (
            f"the {part} body says a design fell short without naming the "
            f"measurement that decided it"
        )
        assert "Meets" not in body, (
            f"the {part} body claims a design meets the bar on a run where "
            f"none does"
        )


def test_an_unresolved_mode_asserts_no_bar_at_all():
    """The counterweight: silence when the bar cannot be named.

    A preset that matches no entry in ``MODE_GATE_COLUMNS`` on a result that
    records no mode leaves the run unjudged, and ``verdict_text`` returns "" --
    so the mail states nothing rather than the bare word "Meets", which is the
    shape a mis-keyed slug once put in every cell of this tool's results page.
    A sentence that appeared regardless of whether a bar exists would be
    furniture.
    """
    bodies = _bodies(_sent(_job(preset="pilot", result={
        "candidates": [{
            "name": "d0",
            "pdb_key": "d0.pdb",
            "scores": {"ipTM": 0.91},
        }],
    })))
    for part, body in bodies.items():
        assert "0.910" in body, f"the {part} body lost its top score"
        assert "Meets" not in body, (
            f"the {part} body asserts a bar that could not be resolved"
        )
        assert "clears the bar" not in body, body
