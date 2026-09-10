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
interface" -- about the design the run's own ``filter_status`` marks ``drop``,
on a pI the tool's legend calls "usually an insoluble, non-specific scaffold".
(That legend sentence is generic; nothing in the repo measures solubility for
this design, and its sequence does not survive example capture.)

THE FIXTURE IS THE SHIPPED EXAMPLE PAYLOAD, read off disk, not a transcription
of its numbers. The class is about what a real container stored, and a copy is
one rewording away from agreeing with a fix that does not work on the real
shape. ``test_the_example_payload_still_carries_the_defect`` asserts the
premise, so an example that stops carrying it fails loudly here instead of
leaving every assertion below hollow.

EVERY BEHAVIOURAL CHECK GOES THROUGH ``send_job_complete_email`` WITH THE
TRANSPORT CAPTURED and reads the two bodies a customer receives. (The premise
test is the exception: its subject is the fixture, so it reads the JSON off disk
and calls ``judge`` directly.) The defect lived in the
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
# module; blanking the env keeps that honest. NOT a repo-wide convention: 51 of
# 177 test files take this fixture, and test_jobs_compare_headline.py -- the
# #248 suite for this same defect class -- does not.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

_EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "tools" / "esmfold2_design" / "example" / "result.json"
)

# As ``_top_candidate_summary`` formats them ("%.3f"), which is what a search of
# the delivered body has to look for. Same column, same run, parting at the
# SECOND decimal (0.9556 / 0.9354), so these are transcriptions of the fixture
# rather than facts about it -- and the premise test pins both renderings, or a
# re-captured example would silently make every negative assertion vacuous.
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


def _job(*, preset: str = RUN_MODE, result: dict | None = None,
         tool: str = "esmfold2-design") -> ToolJob:
    return ToolJob.from_row({
        "id": "2b917b54-0871-44af-a3d1-5d07ea5dcaeb",
        "user_id": str(uuid.uuid4()),
        "tool": tool,
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

    # THE NEEDLES ARE TRANSCRIPTIONS, so pin them here or the negative
    # assertions elsewhere go vacuous in silence. An example re-captured on a
    # newer container -- defect intact, seed0 merely rescored to 0.9700 -- leaves
    # `DROP_IPTM not in body` trivially true, and nothing else in the file
    # notices.
    assert "%.3f" % first["ipTM"] == DROP_IPTM, first["ipTM"]
    assert "%.3f" % second["ipTM"] == PASS_IPTM, second["ipTM"]
    assert cands[0]["pdb_key"] == DROP_PDB, cands[0]["pdb_key"]
    assert cands[1]["pdb_key"] == PASS_PDB, cands[1]["pdb_key"]

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
        # AND NOT THE SHORTFALL FRAMING. Without this, weakening the guard in
        # shared/email.py from `verdict.verdict == "below"` to a bare truthiness
        # test leaves this whole file green while the mail says "Nothing in this
        # run clears the bar — Meets pI 6 and ipTM 0.75" in one sentence.
        assert "clears the bar" not in body, (
            f"the {part} body tells a customer whose design MEETS the bar that "
            f"nothing in the run clears it: {body!r}"
        )
        # The judgement belongs in the callout it qualifies, not loose in the
        # footer: the callout is the endorsement frame ("Top design", a green
        # rule) that the sentence exists to correct.
        assert body.index("Meets") < body.index("View results"), (
            f"the {part} body renders the judgement after the call to action, "
            f"outside the callout whose framing it is there to qualify"
        )


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


def test_the_mode_falls_back_to_the_preset_when_the_result_omits_it():
    """The OTHER half of ``result_mode(...) or job.preset``, which nothing else
    here reaches.

    Every other fixture in this file either records the mode on the result or
    carries a preset that resolves to no bar, and
    ``gate_columns("esmfold2-design", None) == gate_columns(..., "pilot") ==
    ()`` -- so the two branches are observationally identical across the whole
    suite and deleting ``or getattr(job, "preset", None)`` leaves it green.

    It is not a hypothetical branch. ``result_mode``'s own docstring says it
    "Returns None rather than guessing when the flag is absent", precisely so a
    caller can fall back to the preset; a row rebuilt by ``shared/job_recovery``
    or written before the flag existed is exactly that shape. Without the
    fallback such a run is judged against no bar and mails the reject again.
    """
    result = _example_result()
    del result["is_antibody"]
    bodies = _bodies(_sent(_job(preset=RUN_MODE, result=result)))
    for part, body in bodies.items():
        assert PASS_IPTM in body, (
            f"the {part} body lost the preset fallback, so a result that does "
            f"not record its mode is judged against no bar"
        )
        assert DROP_IPTM not in body, body
        assert "Meets" in body, f"the {part} body resolved no bar"


def test_an_unmeasured_leg_is_disclosed_not_dropped():
    """Why ``verdict_text`` and not ``"; ".join(verdict.shortfalls)``.

    On this fixture the two are byte-identical, so the docstring's stated reason
    for using the single renderer -- that it also carries the ``unmeasured`` and
    ``unusable`` clauses -- is unexercised, and a hand-join passes. Here pI is
    absent, which makes the record ``unjudged`` with an ``unmeasured`` leg: the
    headline is then the ipTM 0.956 reject (an unjudged record is eligible, by
    design), and the ONLY thing qualifying it is the clause a hand-join drops.
    """
    result = _example_result()
    for cand in result["candidates"]:
        cand["scores"].pop("pI")
    bodies = _bodies(_sent(_job(result=result)))
    for part, body in bodies.items():
        assert "Not measured: pI" in body, (
            f"the {part} body headlines a design whose pI was never measured "
            f"and says nothing about it: {body!r}"
        )
        assert "Meets" not in body, body


def test_an_unresolved_mode_asserts_no_bar_at_all():
    """The counterweight: silence when the bar cannot be named.

    A preset matching no entry in ``MODE_GATE_COLUMNS``, on a result recording
    no mode, leaves ``gate_columns`` empty -- so ``judge`` returns ``unjudged``
    and ``verdict_text`` falls through to its final ``return ""``. The mail
    states nothing, and a sentence that appeared regardless of whether a bar
    exists would be furniture.

    NOT the "refuses to render a bare Meets" branch, which an earlier version of
    this docstring claimed. That branch is unreachable from here: ``judge`` and
    ``verdict_text`` are handed the SAME ``mode``, and with no gate columns
    ``judge`` cannot return ``meets`` in the first place. It defends against a
    caller that judges under one preset and renders under another, which
    ``_top_candidate_summary`` never does.
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


def test_an_unranked_designs_list_with_no_bar_gets_no_top_design_claim():
    """The regression this fix nearly shipped, caught in review.

    Reading ``candidate_records`` instead of ``result["candidates"]`` also picks
    up ``designs[]`` -- and af2, colabfold and esmfold store their ``batch``
    preset there, one record per INDEPENDENTLY SUBMITTED target, in submission
    order (``"rank": rec_info["index"]``). None of the three declares a bar, so
    ``headline_candidate`` cannot re-pick and ``verdict_text`` renders nothing.
    The callout therefore named whichever sequence the customer pasted first as
    the "Top design" and captioned it with what a GOOD value looks like -- this
    module's own defect, recreated on three tools, in the pushed surface.

    Measured before the gate: an af2 batch holding ptm 0.40 and 0.95 mailed
    "Top design: ptm 0.400 (seqA.pdb)" above "Above 0.7 is a credible model".

    The rule is the SHAPE or the BAR, and gating on the bar alone is wrong:
    bindcraft declares no bar yet stores a ranked ``candidates[]``, so that rule
    would delete a callout it has always had. ``test_a_ranked_candidates_list_
    still_renders_without_a_bar`` holds that other side.
    """
    bodies = _bodies(_sent(_job(tool="af2", preset="batch", result={"designs": [
        {"rank": 0, "pdb_key": "seqA.pdb", "ptm": 0.40},
        {"rank": 1, "pdb_key": "seqB.pdb", "ptm": 0.95},
    ]})))
    for part, body in bodies.items():
        assert "Top design" not in body, (
            f"the {part} body claims a top design over a list nothing ranked "
            f"and no bar can re-pick within: {body!r}"
        )
        assert "0.400" not in body and "seqA" not in body, body
        # The mail is still sent, and still counts the run.
        assert "2 candidates returned" in body, body


def test_a_ranked_candidates_list_still_renders_without_a_bar():
    """The other side of that gate, so it cannot be tightened into a deletion.

    bindcraft stores a container-ranked ``candidates[]`` and declares no bar
    (``gate_columns("bindcraft") == ()``). Its callout predates this change and
    must survive it -- a gate keyed on "has a bar" alone would silently remove
    it, which is why the gate asks about the SHAPE first.
    """
    from shared.score_legends import gate_columns

    assert gate_columns("bindcraft") == (), (
        "bindcraft has grown a bar; this test's premise -- a ranked list with "
        "no bar -- no longer holds, so re-point it rather than leave it passing"
    )
    bodies = _bodies(_sent(_job(tool="bindcraft", preset="pilot", result={
        "candidates": [
            {"rank": 0, "pdb_key": "bc_0.pdb", "scores": {"ipTM": 0.88}},
        ],
    })))
    for part, body in bodies.items():
        assert "Top design" in body, (
            f"the {part} body dropped the callout for a ranked candidate list"
        )
        assert "0.880" in body, body
