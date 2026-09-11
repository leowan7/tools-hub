"""Regression: the completion email must not headline a design its bar rejects.

THE COMPLETION EMAIL -- named, not numbered. #241 fixed this class in three
writers on one tool; #248 fixed /jobs/compare and listed the completion email
among the surfaces it did not reach. Both of those are PULLED -- a customer
meets them by opening the site. This one is pushed: the webhook path
``webhooks/modal.py`` -> ``complete_job`` mails it when a run finishes, so it
reaches a customer who never asked to look. (Do not give this an ordinal. The
repo's two enumerations of the class disagree -- #248's reaches six, #241's
reaches seven, and a peer branch numbers four of them "Surfaces 3-6" -- so any
number here is checkable against nothing.)

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
        # NOT MERELY "above the call to action". The callout is the endorsement
        # frame ("Top design", a green rule) that the sentence exists to
        # correct, and an assertion that only excluded the footer left the whole
        # region between the callout's </div> and the CTA passing -- which is
        # exactly where "loose in the body" lives. Checked on the HTML source
        # below; here just pin the order, having first proved both needles are
        # present so a copy change fails the assertion rather than raising
        # ValueError from ``index``.
        assert "Meets" in body and "View results" in body, body
        assert body.index("Meets") < body.index("View results"), (
            f"the {part} body renders the judgement after the call to action"
        )

    # THE STRUCTURAL HALF, READ OFF THE TEMPLATE SOURCE rather than the render.
    # The rendered-HTML version of this check counted </div> between offsets,
    # which reduces to "at least one div closes between the judgement and the
    # CTA" -- true of a judgement in a div opened AFTER the callout closed, and
    # true even of one emitted outside the `if top_score_label` block entirely,
    # so it would render with no callout to qualify. Source order is exact and
    # needs no nesting arithmetic: the interpolation must sit between the
    # callout's opening div and its closing one.
    tpl = (Path(__file__).resolve().parents[1] / "templates" / "email"
           / "job_complete.html").read_text("utf-8")
    callout_open = tpl.index('<div style="margin:1rem 0;padding:12px 14px;')
    callout_close = tpl.index("</div>\n  {% endif %}", callout_open)
    verdict_at = tpl.index("{{ top_score_verdict }}")
    assert callout_open < verdict_at < callout_close, (
        "templates/email/job_complete.html renders top_score_verdict outside "
        "the callout it qualifies. The callout is the endorsement frame "
        "('Top design', a green rule); a judgement that sits below it reads as "
        "a separate remark, and one emitted outside the enclosing "
        "`if top_score_label` block renders with no callout at all."
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
        assert "Nothing in this run clears the bar" in body, (
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
    caller can fall back to the preset, and ``shared/job_recovery`` rebuilds a
    result as ``{"candidates", "candidate_count", "backfilled"}`` -- no
    ``is_antibody``. Without the fallback such a run is judged against no bar
    and mails the reject again.

    NOT "or written before the flag existed", which an earlier version claimed:
    ``is_antibody`` is in this tool's first add-commit, so no row predates it.
    The recovery path is the whole of the reachability argument.
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
    ``unusable`` clauses -- is unexercised, and a hand-join CONFINED TO THE
    "below" BRANCH passes. (That qualifier is load-bearing: a hand-join
    substituted for the whole of ``verdict_text`` renders "" on a ``meets``
    record, whose ``shortfalls`` is empty, and the sibling test asserting
    "Meets" catches it. The dangerous mutation is the one that keeps the meets
    branch and cheapens the rest.) Here pI is
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
            # BOTH legs present, deliberately, and NOT because the one-leg
            # fixture reached the wrong branch -- at ``preset="pilot"``
            # ``gate_columns`` is empty, so there is no leg for either fixture
            # to leave unmeasured and both are silent for the reason this test
            # names. It is about what happens UNDER MUTATION: give this tool a
            # default mode and a fully measured record judges "meets", so the
            # mail says "Meets pI 6 and ipTM 0.75" and the assertion below
            # fires. A record missing pI would judge ``unjudged`` with an
            # unmeasured leg under that same mutation, say "Not measured: pI",
            # and keep every assertion here green.
            "scores": {"ipTM": 0.91, "pI": 5.0},
        }],
    })))
    for part, body in bodies.items():
        assert "0.910" in body, f"the {part} body lost its top score"
        assert "Meets" not in body, (
            f"the {part} body asserts a bar that could not be resolved"
        )
        assert "clears the bar" not in body, body


@pytest.mark.parametrize("tool", ["af2", "colabfold", "esmfold"])
def test_an_unranked_designs_list_with_no_bar_gets_no_top_design_claim(tool):
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

    THE RULE IS THE SHAPE ALONE. Gating on the bar instead is wrong in both
    directions: bindcraft declares no bar yet stores a ranked ``candidates[]``,
    so a bar-only rule deletes a callout it has always had (the sibling test
    named ``..._ranked_candidates_list_still_renders_without_a_bar`` holds that
    side), and boltz2 declares one over an UNRANKED list, so a shape-or-bar rule
    crowns the wrong design (``..._abstains_even_when_the_tool_has_a_bar``).

    THE GATE IS TOOL-WIDE, not batch-specific. iggm and opendde also store
    ``designs[]`` and also abstain now. Neither had a callout on ``main``, so
    that is a missed opportunity rather than a regression, and nothing pins it.
    """
    bodies = _bodies(_sent(_job(tool=tool, preset="batch", result={"designs": [
        {"rank": 0, "pdb_key": "seqA.pdb", "ptm": 0.40},
        {"rank": 1, "pdb_key": "seqB.pdb", "ptm": 0.95},
    ]})))
    for part, body in bodies.items():
        assert "Top design" not in body, (
            f"the {part} body claims a top design over a list nothing ranked: "
            f"{body!r}"
        )
        assert "0.400" not in body and "seqA" not in body, body
        # The mail is still sent, and still counts the run.
        assert "2 candidates returned" in body, body
    # A POSITIVE NEEDLE ONLY THE REAL TEMPLATE PRODUCES. Every assertion above
    # is a negative, and ``send_job_complete_email`` catches a template failure
    # and falls back to an inline body that carries no callout at all -- so a
    # totally broken template satisfies this test while proving nothing.
    #
    # THE NEEDLE IS THE FOOTER'S PUNCTUATION, which is the only thing that
    # differs: job_complete.txt ends 'Ranomics Tools. <url>' and the inline
    # ``_render_text`` fallback ends 'Ranomics Tools - <url>' with an em dash.
    # 'preset <slug>' does NOT distinguish them -- both emit it, as does the
    # HTML template -- so an earlier needle on that string passed against a
    # deliberately broken template and closed nothing.
    assert "Ranomics Tools." in bodies["text"], (
        "the plain-text body did not come from job_complete.txt, so the "
        "absence of a callout above is not evidence about the gate"
    )


def test_an_unranked_designs_list_abstains_even_when_the_tool_has_a_bar():
    """A bar does not license a superlative over a list nothing ranked.

    The first version of this gate also admitted any list whose tool declares a
    bar, reasoning that ``headline_candidate`` could re-pick within it. It
    cannot: its docstring says "THIS DOES NOT RE-RANK" and its loop returns the
    FIRST record not shown to fall short, in stored order. A bar therefore only
    narrows WHICH arbitrary record gets crowned.

    boltz2 is the case that proves it -- submission-ordered ``designs[]`` AND a
    registered bar. Under the two-armed gate this payload mailed a top-design
    callout for d1 at 0.710 on a run holding 0.95. ``origin/main`` sent boltz2
    no callout at all, so that was a hole this change opened rather than one it
    inherited.

    The LABEL that gate mailed was ``iptm``, not ``ipTM``: boltz2 stores the
    lowercase spelling, the legend table is keyed on the canonical one, and the
    column chooser matches by raw key -- so the callout carried a bare storage
    key with no caption, two lines above a verdict naming ``ipTM``. That is a
    separate, older defect in the chooser, still live for any record whose
    scores use an alias spelling (a recovered row carrying ``i_pae``). It is not
    fixed here and no test covers it; abstaining simply stops this tool meeting
    it.
    """
    from shared.score_legends import gate_columns

    assert gate_columns("boltz2"), (
        "boltz2 has lost its bar; this test's premise -- an unranked list from "
        "a tool that DOES declare a bar -- no longer holds, so re-point it "
        "rather than leave it passing"
    )
    bodies = _bodies(_sent(_job(tool="boltz2", preset="pilot", result={
        "designs": [
            {"rank": 0, "pdb_key": "d1.pdb", "iptm": 0.71,
             "complex_plddt": 0.86, "n_hotspot_contacts": 13},
            {"rank": 1, "pdb_key": "d2.pdb", "iptm": 0.95,
             "complex_plddt": 0.97, "n_hotspot_contacts": 13},
        ],
    })))
    for part, body in bodies.items():
        assert "Top design" not in body, (
            f"the {part} body crowns the first-pasted design that clears the "
            f"bar while a better one sits in the same run: {body!r}"
        )
        assert "0.710" not in body, body
    # See the needle note in the af2 sibling: the footer's period is what
    # separates the real template from the inline fallback.
    assert "Ranomics Tools." in bodies["text"], (
        "the plain-text body did not come from job_complete.txt, so the "
        "absence of a callout above is not evidence about the gate"
    )


def test_the_mail_identifies_the_design_when_there_is_no_filename():
    """Say WHICH row, when there is no filename to say it with.

    Deriving the headline means the mail can show a design that is not the one
    the results page leads with -- and pxdesign, rfdiffusion and boltzgen
    candidates carry no ``pdb_key``, so before this the customer got a bare
    number, unidentifiable, that no longer matched row 1 on the page.
    ``headline_candidate``'s docstring puts the disclosure on the caller ("the
    caller is expected to say which row it picked when the two differ") and
    /jobs/compare does it; this is the same duty on the pushed surface.

    Fixture: record 0 wins on ipTM but fails the pLDDT leg, record 1 clears
    both -- so the pick genuinely differs from the stored first row.
    """
    bodies = _bodies(_sent(_job(tool="pxdesign", preset="pilot", result={
        "candidates": [
            {"rank": 1, "scores": {"ipTM": 0.88, "pLDDT": 68.0}},
            {"rank": 2, "scores": {"ipTM": 0.79, "pLDDT": 90.0}},
            {"rank": 3, "scores": {"ipTM": 0.60, "pLDDT": 55.0}},
        ],
    })))
    for part, body in bodies.items():
        # The pick, not the stored first row.
        assert "0.790" in body, f"the {part} body lost the derived headline"
        assert "0.880" not in body, (
            f"the {part} body leads with the row the tool's own bar fails"
        )
        # ...and it says which one, so the number can be matched to a design.
        assert "rank 2 of 3" in body, (
            f"the {part} body shows a design the customer cannot identify, on "
            f"a tool whose candidates carry no filename: {body!r}"
        )


def test_a_failed_run_gets_no_endorsement():
    """The tone gate, which nothing in this repo held.

    ``_top_candidate_summary`` returns five empty strings unless ``tone`` is
    "success". Delete that line and a job that died mid-run still mails the
    green "Top design" callout, with a legend saying the number is credible,
    under the headline "Your run failed" -- measured, all tests green.

    The example payload is reused deliberately: it HAS candidates that would
    render, so the silence here is the tone gate's doing and not an empty
    result's.
    """
    row = {
        "id": "2b917b54-0871-44af-a3d1-5d07ea5dcaeb",
        "user_id": str(uuid.uuid4()),
        "tool": "esmfold2-design",
        "preset": RUN_MODE,
        "status": "failed",
        "inputs": {},
        "result": _example_result(),
        "error": {"detail": "GPU OOM at design 3"},
        "modal_function_call_id": "fc-stub-x",
        "job_token": "t" * 64,
        "gpu_seconds_used": 120,
        "created_at": "2026-09-01T12:00:00Z",
        "started_at": "2026-09-01T12:00:01Z",
        "completed_at": "2026-09-01T12:30:00Z",
    }
    bodies = _bodies(_sent(ToolJob.from_row(row)))
    for part, body in bodies.items():
        assert "Top design" not in body, (
            f"the {part} body endorses a design in a mail about a run that "
            f"failed: {body!r}"
        )
        assert PASS_IPTM not in body and DROP_IPTM not in body, body
        assert "credible designed interface" not in body, body
        assert "Meets" not in body, body


def test_the_gate_reads_a_list_not_merely_a_present_key():
    """``isinstance(..., list)``, not ``is not None``.

    A row whose ``candidates`` is a non-list (a string, a dict) alongside a real
    ``designs[]`` would otherwise pass the shape test on the strength of the key
    alone, and render exactly the unranked callout the gate exists to suppress.
    """
    bodies = _bodies(_sent(_job(tool="af2", preset="batch", result={
        "candidates": "not-a-list",
        "designs": [{"rank": 0, "pdb_key": "seqA.pdb", "ptm": 0.40}],
    })))
    for part, body in bodies.items():
        assert "Top design" not in body, (
            f"the {part} body took a non-list 'candidates' key as evidence "
            f"that the designs list was ranked: {body!r}"
        )


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
