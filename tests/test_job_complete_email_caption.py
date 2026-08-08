"""The job-completion email's top-score caption.

THE SECOND CONSUMER. ``shared/score_legends.py`` exists to fill a column
tooltip, and every change to it has been reviewed against a results table. But
``shared/email.py::_top_candidate_summary`` also builds
``templates/email/job_complete.{html,txt}``'s ``top_score_caption`` from a
legend — a 13px line under a single number, documented in the template as a
"1-line interpretation of the top score".

Nothing in this repo tested it. So when a multi-chain caveat was written into
the BoltzGen ipTM explanation — "These designs are also ranked on this number,
so on an older multi-chain run treat the order of the table as indicative
too" — it went out in every BoltzGen completion email: a message with one
design, one score, and no table. It went out on single-chain runs too, because
the legend is keyed on ``(tool, column)`` and knows nothing about chains.

The correction to THAT was over-corrected, and this docstring used to carry the
over-correction: "no legend's ``caveat`` ever reaches the email", because "the
email has no chain-conditional caveat to get wrong". Both halves were wrong.
``shared/jobs.complete_job`` sends this mail, and it is called by
``timeout_stuck_job`` (which finalizes a result ``shared/job_recovery``
rebuilt from a Storage listing), by the inline poll in
``blueprints/jobs.job_status``, and by ``scripts/finalize_stuck_job.py`` — so
the mail IS sent about results stored long before it. Driven, all three: each
mailed a pre-deploy BoltzGen score described as "the binder-to-target
interface" with no caveat, which is false for exactly the runs the caveat
exists for. And the email CAN be chain-conditional, because unlike the legend
it has the job, and ``job.inputs`` carries the chain the run was submitted
with.

These tests render what the user receives, through ``send_job_complete_email``
with the transport captured, and hold four things:

  * the caption for a single-chain BoltzGen run is the legend's one line and
    nothing else, and describes nothing that is not in the message;
  * on a MULTI-chain run it carries the legend's ``caveat`` verbatim as well,
    on every path that sends this mail — including the two that finalize a
    stored result;
  * neither half ever describes page furniture, because there is no table in
    an email and exactly one design;
  * no legend's explanation outgrows the slot. That is the generic half: it
    fails for the next legend that tries this, not only for BoltzGen's.
"""
from __future__ import annotations

import re
import uuid
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

from shared import email as email_mod
from shared.jobs import ToolJob
from shared.score_legends import SCORE_LEGENDS, get_legend, legend_text

# The email path never touches Supabase, but ``shared.jobs`` imports the
# client module; blanking the env keeps that honest for the same reason every
# other suite here does.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

# A PICKED ceiling, and the comment here used to claim otherwise — "deliberately
# derived rather than picked … the number moves with the corpus". It never
# moved with anything: it is a literal, the corpus maximum is 161 characters
# (``('mpnn','score')``) and BoltzGen's is 134, so 220 sat 36% above the real
# maximum while describing itself as tracking it.
#
# It cannot honestly be derived from the corpus, either, and that is the point
# worth writing down rather than the number: a limit computed as
# ``max(len(explanation) …)`` is satisfied by every corpus including one whose
# longest entry has just grown to 496, which is the mutation this constant
# exists to catch. A ceiling has to come from the SLOT, not from what is
# currently in it.
#
# So: 220 characters ≈ 1.4× the longest honest explanation today, roughly three
# rendered lines in the 13px caption block of a 560px-wide email body. Room to
# reword an entry, not room for a second paragraph.
# ``test_no_legend_outgrows_the_one_line_slot`` reports the measured corpus
# maximum in its failure message, so the slack stays visible instead of
# becoming a claim.
_SLOT_LIMIT = 220

# The template's own description of the slot — the premise this limit rests on.
# Asserted rather than quoted, so a template that grows the caption into a
# paragraph makes the constant's justification fail instead of silently
# outliving it.
_SLOT_CONTRACT = "1-line interpretation of the top score"


def _job(*, target_chain: str = "A,B", tool: str = "boltzgen") -> ToolJob:
    return ToolJob.from_row({
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": tool,
        "preset": "pilot",
        "status": "succeeded",
        "inputs": {"target_chain": target_chain},
        "result": {
            "tier": "pilot",
            "candidates": [{
                "design_name": "d0",
                "pdb_key": "designs/design_0.pdb",
                "sequence": "MKTAY",
                # ipTM first: _top_candidate_summary takes the first scored
                # column that has a registered legend.
                "scores": {"ipTM": 0.91, "pLDDT": 88.0},
            }],
        },
        "error": None,
        "modal_function_call_id": "fc-stub-x",
        "job_token": "t" * 64,
        "gpu_seconds_used": 120,
        "created_at": "2026-08-08T12:00:00Z",
        "started_at": "2026-08-08T12:00:01Z",
        "completed_at": "2026-08-08T12:30:00Z",
    })


def _sent(job: ToolJob) -> dict:
    """The exact payload the provider would receive.

    Through ``send_job_complete_email`` rather than through the two helpers it
    calls, because the defect this file exists for lived in the wiring: the
    caption is assembled in one module, formatted in another, and nobody had
    looked at the join.
    """
    captured = {}

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
    source: ``select_autoescape`` covers .html and not .txt, so a legend with
    an apostrophe (three of them have one) reaches the HTML body as ``&#39;``
    and a raw substring assertion would fail — or, worse, pass a mutation for
    an escaping reason that has nothing to do with the claim under test.
    """
    parser = _Text()
    parser.feed(payload["html"])
    return {
        "html": " ".join("".join(parser.chunks).split()),
        "text": " ".join(payload["text"].split()),
    }


def _caption_of(job: ToolJob) -> str:
    label, value, caption, _pdb = email_mod._top_candidate_summary(
        job=job, tone="success",
    )
    assert (label, value) == ("ipTM", "0.910"), (label, value)
    return caption


@pytest.mark.parametrize("chain", ["A", "A,B"])
def test_the_boltzgen_caption_is_in_the_mail_and_describes_only_the_mail(chain):
    """One design, one number, no table — so the caption may not name one.

    Run on BOTH chain counts, because the multi-chain caption is longer and
    carries a second sentence: whatever the email says, it may not describe
    furniture that no email has.
    """
    job = _job(target_chain=chain)
    payload = _sent(job)
    bodies = _bodies(payload)
    caption = _caption_of(job)
    assert caption, "the top-score caption is empty; nothing to check"

    for part, body in bodies.items():
        assert caption in body, f"the caption never reaches the {part} body"
        assert "ipTM" in body and "0.910" in body, (
            f"the {part} body lost the score the caption interprets"
        )

    # What it must say: the metric, and the bar.
    assert "binder-to-target interface" in caption
    assert "0.7" in caption and "0.8" in caption, (
        "the caption dropped the thresholds, which are the actionable half of "
        f"a one-line interpretation: {caption!r}"
    )

    # The premise: there is no table in this message for the copy to describe.
    assert "<table" not in payload["html"], (
        "the completion email has grown a table; the whole reason this "
        "caption may not describe one no longer holds"
    )
    # And exactly one design, so nothing plural and nothing pointed at.
    assert len(job.result["candidates"]) == 1
    for banned in ("table", "these designs", "this design", "the column",
                   "the row", "this page", "below", "above the",
                   "the order of"):
        assert banned not in caption.lower(), (
            f"the completion email's caption says {banned!r}. It is one "
            f"score for one design, in a message with no table and no "
            f"ordering to point at: {caption!r}"
        )


def test_the_era_caveat_reaches_the_mail_when_the_target_is_multi_chain():
    """The round-4 regression, in both directions.

    Round 3 put the era caveat in the BoltzGen ipTM ``explanation``; it then
    went out in every completion mail, including single-chain ones, describing
    a table that is not there. Round 4 fixed that by asserting the caveat may
    NEVER reach the email, on the written premise that this mail is only ever
    sent by ``complete_job`` about a run that just finished. That premise is
    false — ``timeout_stuck_job``, the inline poll in
    ``blueprints/jobs.job_status`` and ``scripts/finalize_stuck_job.py`` all
    finalize STORED results through ``complete_job``, which sends this mail —
    so the fix deleted a true statement from the one surface a user of a
    pre-deploy multi-chain run actually opens.

    The property is not "never" and not "always": the caveat belongs where its
    own antecedent can hold, which is a multi-chain target, and the email is
    the one legend consumer that can tell.
    """
    caveat = get_legend("boltzgen", "ipTM").get("caveat")
    assert caveat, "boltzgen ipTM lost its caveat"

    multi = _bodies(_sent(_job(target_chain="A,B")))
    for part, body in multi.items():
        assert caveat in body, (
            f"the {part} body of a MULTI-chain BoltzGen completion carries no "
            f"era caveat. The mail is sent about stored results too — a run "
            f"recovered from Storage predates the container update that made "
            f"'the binder-to-target interface' true."
        )

    single = _bodies(_sent(_job(target_chain="A")))
    for part, body in single.items():
        assert caveat not in body, (
            f"the {part} body of a SINGLE-chain BoltzGen completion carries "
            f"the multi-chain caveat. Its first clause is 'On a multi-chain "
            f"target …', so here it is a conditional nobody can act on, and a "
            f"caveat shown to everyone is a caveat nobody reads."
        )
        assert "complex-wide" not in body.lower()
        assert "older multi-chain run" not in body.lower()


def test_the_stuck_job_sweeper_mails_the_caveat_for_a_recovered_result():
    """The path the deleted comment said could not exist, driven.

    ``shared/jobs.timeout_stuck_job`` asks ``shared/job_recovery`` for a
    result — which that module's docstring describes as rebuilt from
    ``inputs._partial_candidates`` or "a direct Storage listing" — and
    finalizes it through the same ``complete_job`` the webhook uses, which
    sends this mail. The job below was submitted 2026-08-01, before the
    container update that made "the binder-to-target interface" true of a
    BoltzGen number, and is recovered a week later.

    Asserted through the sweeper rather than through ``_top_candidate_summary``
    because the claim under test is about WHICH CALLERS SEND THIS MAIL. A unit
    test of the caption cannot fail when someone re-writes the comment that
    got this wrong twice.
    """
    from shared.jobs import timeout_stuck_job

    row = {
        "id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "tool": "boltzgen", "preset": "pilot", "status": "running",
        "inputs": {"target_chain": "A,B"}, "result": None, "error": None,
        "modal_function_call_id": "fc-stub-x", "job_token": "t" * 64,
        "gpu_seconds_used": 120,
        "created_at": "2026-08-01T09:00:00Z",
        "started_at": "2026-08-01T09:00:01Z", "completed_at": None,
    }
    recovered = _job(target_chain="A,B").result
    stuck = ToolJob.from_row(row)
    done = ToolJob.from_row(dict(row, status="succeeded", result=recovered,
                                 completed_at="2026-08-08T10:00:00Z"))
    # ``timeout_stuck_job`` reads the row, then ``complete_job`` re-reads it
    # (and returns early if it is already terminal) before re-reading it once
    # more for the email payload.
    seen = {"n": 0}

    def _get_job(job_id, **kw):
        seen["n"] += 1
        return stuck if seen["n"] <= 2 else done

    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "resend-stub"}

    with patch.dict("os.environ", {"RESEND_API_KEY": "test-key"}), \
            patch("shared.email.requests.post",
                  side_effect=lambda url, **kw: (
                      captured.update(kw.get("json") or {}) or _Resp()
                  )), \
            patch("shared.jobs.get_job", side_effect=_get_job), \
            patch("shared.job_recovery.recover_stuck_job_result",
                  return_value=recovered), \
            patch("shared.jobs.mark_succeeded", return_value=True), \
            patch("shared.jobs._charge_workspace_for_completed_job"), \
            patch("shared.jobs._settle_wallet_hold_for_completed_job"), \
            patch("shared.jobs.resolve_user_email_and_meta",
                  return_value=("u@example.com", {})):
        outcome = timeout_stuck_job(stuck.id)

    assert outcome == "recovered", outcome
    assert captured, (
        "the sweeper finalized a recovered result and sent no mail; this "
        "test's premise — that this path emails at all — is gone"
    )
    caveat = get_legend("boltzgen", "ipTM")["caveat"]
    for part in ("html", "text"):
        assert caveat in _bodies(captured)[part], (
            f"the {part} mail for a job SUBMITTED 2026-08-01 and recovered "
            f"out of Storage says the number is the binder-to-target "
            f"interface with no era caveat. That is the case the caveat "
            f"exists for."
        )


def test_the_caption_is_the_legend_and_nothing_invented():
    """Both halves, asserted as string equality against the legend itself.

    So the email cannot grow a paraphrase of the caveat that drifts from the
    one the results table shows — two copies of one claim is how the last
    three defects here were made.
    """
    legend = get_legend("boltzgen", "ipTM")
    assert _caption_of(_job(target_chain="A")) == legend["explanation"]
    assert _caption_of(_job(target_chain="A,B")) == legend_text(legend)
    # "A,A" is ONE chain everywhere else in this app
    # (tools.base.parse_target_chains de-duplicates), so it must not trip a
    # caveat about cross-chain interfaces that do not exist.
    assert _caption_of(_job(target_chain="A,A")) == legend["explanation"]
    assert _caption_of(_job(target_chain="A B")) == legend_text(legend)


def test_no_legend_explanation_is_chain_conditional():
    """The half of the split that IS about what a legend cannot know.

    A legend is keyed on ``(tool, column)`` and never sees a job, so a chain
    condition written into ``explanation`` is unevaluable and goes out on
    every run of that tool. That is the real reason the era note lives in
    ``caveat`` — not that the email is safe from caveats — and it fails here
    for the next legend that tries it.
    """
    offenders = {
        key: legend["explanation"]
        for key, legend in SCORE_LEGENDS.items()
        if re.search(r"multi.chain|chain.chain|complex.wide|single.chain",
                     legend["explanation"], re.I)
    }
    assert not offenders, (
        f"legend explanation(s) state a chain condition the legend cannot "
        f"evaluate: {offenders!r}. Chain-conditional text belongs in "
        f"``caveat``, which shared/score_legends.email_caption gates on the "
        f"job's own target and components/candidate_table.html renders."
    )


def test_no_legend_outgrows_the_one_line_slot():
    """The generic half: this fails for the NEXT legend, not just BoltzGen's.

    ``templates/email/job_complete.html`` documents the field as a "1-line
    interpretation of the top score" and renders it as a 13px line under a
    single number. Any legend can land there — the email picks the first
    scored column with a registered legend — so the constraint belongs to the
    table, not to one entry.
    """
    # The premise the constant rests on, read off the template rather than
    # quoted in a comment.
    tpl = (Path(__file__).resolve().parents[1]
           / "templates" / "email" / "job_complete.html").read_text("utf-8")
    assert _SLOT_CONTRACT in tpl, (
        f"templates/email/job_complete.html no longer documents "
        f"top_score_caption as a {_SLOT_CONTRACT!r}; the ceiling below is "
        f"justified by that description and needs re-deriving without it"
    )

    lengths = {k: len(v["explanation"]) for k, v in SCORE_LEGENDS.items()}
    over = {key: n for key, n in lengths.items() if n > _SLOT_LIMIT}
    assert not over, (
        f"legend explanation(s) too long for the completion email's one-line "
        f"caption slot ({_SLOT_LIMIT} chars): {over!r}. The rest of the corpus "
        f"peaks at {max(n for k, n in lengths.items() if k not in over)}. "
        f"Long-form context belongs in the optional ``caveat``, which "
        f"components/candidate_table.html renders and which reaches the email "
        f"only on a job whose target names more than one chain."
    )


def test_no_legend_text_describes_a_table():
    """The email has no table, so nothing that can land in it may name one.

    Same rule as components/multichain_iptm_notice.html, for the same reason:
    a string with two consumers may not describe the furniture of one of
    them.

    BOTH FIELDS. This used to walk ``explanation`` only, on the reasoning that
    ``caveat`` never left the results table — and ``caveat`` is now the half
    that reaches the email on a multi-chain run, as well as the half rendered
    in the pooled per-row Score tooltip, where the visible order is by
    percentile and not by the number the caveat is about. Deixis is worse
    there, not better.
    """
    deictic = re.compile(
        r"\b(?:this|these|that|those|the)\s+(?:\w[\w-]*[\s-]+){0,2}"
        r"(?:table|column|columns|row|rows|page|panel|designs|candidates)\b",
        re.I,
    )
    offenders = {}
    for key, legend in SCORE_LEGENDS.items():
        for field in ("explanation", "caveat"):
            hit = deictic.search(legend.get(field) or "")
            if hit:
                offenders[(key, field)] = hit.group(0)
    assert not offenders, (
        f"legend text points at page furniture: {offenders!r}. It renders as "
        f"the job-completion email caption, where there is no table and "
        f"exactly one design, and as a per-row tooltip in a table sorted by "
        f"something else."
    )
    # The premise: there IS a caveat to have checked. Without this the loop
    # above passes by iterating over nothing but explanations.
    assert any(v.get("caveat") for v in SCORE_LEGENDS.values()), (
        "no legend carries a caveat any more, so the ``caveat`` half of this "
        "check guards nothing; re-point it rather than leave it passing"
    )
