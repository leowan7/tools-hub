"""The job-completion email's top-score caption.

THE SECOND CONSUMER. ``shared/score_legends.py`` exists to fill a column
tooltip, and every change to it has been reviewed against a results table. But
``shared/email.py::_top_candidate_summary`` reads ``legend["explanation"]``
verbatim and hands it to ``templates/email/job_complete.{html,txt}`` as
``top_score_caption`` — a 13px line under a single number, documented in the
template as a "1-line interpretation of the top score".

Nothing in this repo tested it. So when a multi-chain caveat was written into
the BoltzGen ipTM explanation — "These designs are also ranked on this number,
so on an older multi-chain run treat the order of the table as indicative
too" — it went out in every BoltzGen completion email: a message with one
design, one score, and no table. It went out on single-chain runs too, because
the legend is keyed on ``(tool, column)`` and knows nothing about chains. The
suite stayed green because nothing here rendered the email.

These tests render what the user receives, through ``send_job_complete_email``
with the transport captured, and hold three things:

  * the caption for a BoltzGen run says what the number is and what counts as
    good, and describes nothing that is not in the message;
  * it does not vary with chain count — the email has no chain-conditional
    caveat to get wrong, which is the point;
  * NO legend's explanation outgrows the slot, and no legend's ``caveat`` ever
    reaches the email. That is the generic half: it fails for the next legend
    that tries this, not only for BoltzGen's.
"""
from __future__ import annotations

import re
import uuid
from html.parser import HTMLParser
from unittest.mock import patch

import pytest

from shared import email as email_mod
from shared.jobs import ToolJob
from shared.score_legends import SCORE_LEGENDS, get_legend

# The email path never touches Supabase, but ``shared.jobs`` imports the
# client module; blanking the env keeps that honest for the same reason every
# other suite here does.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

# The longest explanation among the legends that do NOT carry a caveat, plus
# room. Deliberately derived rather than picked: the slot's real constraint is
# "no longer than the other lines that share it", and the number moves with
# the corpus instead of going stale. 496 characters — what the BoltzGen entry
# briefly grew to — is three times the longest of the other 31.
_SLOT_LIMIT = 220


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


def test_the_boltzgen_caption_is_in_the_mail_and_describes_only_the_mail():
    """One design, one number, no table — so the caption may not name one."""
    job = _job(target_chain="A,B")
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
    # And exactly one design, so nothing plural and nothing ordered.
    assert len(job.result["candidates"]) == 1
    for banned in ("table", "these designs", "the order", "ordering",
                   "ranked", "older run", "complex-wide", "chain-chain"):
        assert banned not in caption.lower(), (
            f"the completion email's caption says {banned!r}. It is one "
            f"score for one design, from a run that finished seconds ago on "
            f"the container running now: {caption!r}"
        )


def test_the_caption_does_not_depend_on_the_chain_count():
    """The legend is keyed on ``(tool, column)`` and cannot know the chains.

    That is fine as long as it says nothing chain-conditional. When it did,
    every single-chain BoltzGen completion carried a multi-chain ranking
    caveat — asserting the two are IDENTICAL is what makes that impossible
    rather than merely absent today.
    """
    multi = _caption_of(_job(target_chain="A,B"))
    single = _caption_of(_job(target_chain="A"))
    assert multi == single, (
        "the email caption now varies with chain count. Either the legend "
        "grew a conditional it cannot evaluate, or the email started "
        "choosing between legends; both need a chain-aware surface, and the "
        "banner is it (components/multichain_iptm_notice.html)"
    )
    assert _bodies(_sent(_job(target_chain="A")))["html"].count(single) == 1


def test_no_legend_outgrows_the_one_line_slot():
    """The generic half: this fails for the NEXT legend, not just BoltzGen's.

    ``templates/email/job_complete.html`` documents the field as a "1-line
    interpretation of the top score" and renders it as a 13px line under a
    single number. Any legend can land there — the email picks the first
    scored column with a registered legend — so the constraint belongs to the
    table, not to one entry.
    """
    over = {
        key: len(legend["explanation"])
        for key, legend in SCORE_LEGENDS.items()
        if len(legend["explanation"]) > _SLOT_LIMIT
    }
    assert not over, (
        f"legend explanation(s) too long for the completion email's one-line "
        f"caption slot: {over!r}. Long-form context belongs in the optional "
        f"``caveat``, which components/candidate_table.html renders and the "
        f"email deliberately does not."
    )


def test_no_legend_explanation_describes_a_table():
    """The email has no table, so nothing that can land in it may name one.

    Same rule as components/multichain_iptm_notice.html, for the same reason:
    a string with two consumers may not describe the furniture of one of
    them.
    """
    deictic = re.compile(
        r"\b(?:this|these|that|those|the)\s+(?:\w[\w-]*[\s-]+){0,2}"
        r"(?:table|column|columns|row|rows|page|panel|designs|candidates)\b",
        re.I,
    )
    offenders = {
        key: deictic.search(legend["explanation"]).group(0)
        for key, legend in SCORE_LEGENDS.items()
        if deictic.search(legend["explanation"])
    }
    assert not offenders, (
        f"legend explanation(s) point at page furniture: {offenders!r}. They "
        f"also render as the job-completion email caption, where there is no "
        f"table and exactly one design."
    )


def test_a_caveat_never_reaches_the_email():
    """The split, asserted at the seam rather than assumed from the field name.

    ``caveat`` is about what an OLD STORED result may hold. The email is sent
    from ``shared/jobs.complete_job`` at the terminal transition, so its number
    is always from the container running now. If a future change routes the
    email through ``legend_text``, this is what says so.
    """
    with_caveat = {k: v for k, v in SCORE_LEGENDS.items() if v.get("caveat")}
    assert with_caveat, (
        "no legend carries a caveat any more, so this test guards nothing; "
        "re-point it or delete it rather than leave it passing"
    )
    caveat = get_legend("boltzgen", "ipTM").get("caveat")
    assert caveat, "boltzgen ipTM lost its caveat"

    bodies = _bodies(_sent(_job(target_chain="A,B")))
    for part, body in bodies.items():
        assert caveat not in body, (
            f"the boltzgen ipTM caveat is in the {part} email body"
        )
        # Not the whole string only: the sentences that made this a defect,
        # so a reworded caveat cannot slip back in under a new phrasing.
        assert "order of the table" not in body.lower()
        assert "older multi-chain run" not in body.lower()
        assert "complex-wide" not in body.lower()
