"""The lab-handoff emails must report the real shortlist size.

A 'web' lab_campaigns row keeps its shortlist in ``candidate_indices``, but a
'campaign' row (migration 0037) leaves that empty and stores the shortlist in
``candidate_refs`` instead. Both the customer confirmation and the
leo@ranomics.com staff notify read ``candidate_indices`` alone, so every
campaign-sourced handoff was announced as "0 candidates" to both parties.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from shared import email as em

# Register item B-12: the only file in this slice that lacked it. Nothing here
# reaches Supabase today, but `shared.email` is one import away from code that
# does, and the fixture is what makes that stay true.
pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _flat(text: str) -> str:
    """Collapse whitespace: the HTML template wraps mid-sentence, so the
    rendered count and its noun sit on different lines."""
    return re.sub(r"\s+", " ", text or "")


def _campaign(*, indices=None, refs=None):
    return SimpleNamespace(
        id="11111111-2222-3333-4444-555555555555",
        target_name="HER2",
        assay_type="yeast_display",
        budget_band="pilot",
        candidate_indices=list(indices or []),
        candidate_refs=list(refs) if refs is not None else None,
    )


@pytest.fixture
def sent(monkeypatch):
    """Capture Resend payloads instead of posting them."""
    captured: list[dict] = []

    class _Resp:
        status_code = 200

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.append(json or {})
        return _Resp()

    monkeypatch.setattr(em.requests, "post", fake_post)
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    return captured


def test_campaign_shortlist_count_comes_from_candidate_refs(sent):
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[
            {"job_id": "j1", "index": 0},
            {"job_id": "j1", "index": 4},
            {"job_id": "j2", "index": 2},
        ]),
        user_email="scientist@example.com",
    )

    assert len(sent) == 2, "one confirmation + one staff notify"
    user_mail, staff_mail = sent
    # The regression printed "0 candidates" in both.
    assert "3 candidates)" in _flat(user_mail["html"])
    assert "0 candidate" not in _flat(user_mail["html"])
    assert "<td>3</td>" in _flat(staff_mail["html"])
    assert "Candidates: 3" in staff_mail["text"]


def test_web_shortlist_still_counts_candidate_indices(sent):
    em.send_campaign_submitted_emails(
        campaign=_campaign(indices=[0, 1]),
        user_email="scientist@example.com",
    )
    user_mail, staff_mail = sent
    assert "2 candidates)" in _flat(user_mail["html"])
    assert "Candidates: 2" in staff_mail["text"]


def test_singular_wording_for_one_candidate(sent):
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
    )
    assert "1 candidate)" in _flat(sent[0]["html"])


def test_no_key_sends_nothing(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    calls: list = []
    monkeypatch.setattr(
        em.requests, "post",
        lambda *a, **k: calls.append(1),
    )
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
    )
    assert calls == []


# ---------------------------------------------------------------------------
# Target-sourced handoffs (Phase 5.3, migration 0040)
# ---------------------------------------------------------------------------

_TID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _target_campaign(refs):
    """A 'target' lab_campaigns row: shortlist in candidate_refs, parent is a
    design target rather than a compute campaign or a single job."""
    c = _campaign(refs=refs)
    c.submission_source = "target"
    c.source_target_id = _TID
    c.source_campaign_id = None
    c.source_job_id = None
    return c


def test_a_target_shortlist_counts_candidate_refs_too(sent):
    """The count fix generalises: a 'target' row leaves candidate_indices empty
    for the same reason a 'campaign' row does."""
    em.send_campaign_submitted_emails(
        campaign=_target_campaign([
            {"job_id": "j1", "index": 0},
            {"job_id": "j2", "index": 0},
        ]),
        user_email="scientist@example.com",
        source_tools={"bindcraft": 1, "pxdesign": 1},
    )
    user_mail, staff_mail = sent
    assert "2 candidates)" in _flat(user_mail["html"])
    assert "Candidates: 2" in staff_mail["text"]


def test_the_staff_notify_names_the_tools_the_designs_came_from(sent):
    """The cross-tool spread at a glance. Ordered by count descending then
    slug, so the same shortlist always renders the same string."""
    em.send_campaign_submitted_emails(
        campaign=_target_campaign([{"job_id": "j1", "index": i} for i in range(7)]),
        user_email="scientist@example.com",
        source_tools={"pxdesign": 3, "rfdiffusion": 4},
    )
    staff_mail = sent[1]
    assert "Designs from: rfdiffusion (4), pxdesign (3)" in staff_mail["text"]
    assert "rfdiffusion (4), pxdesign (3)" in _flat(staff_mail["html"])


def test_the_staff_notify_links_back_to_the_target(sent, monkeypatch):
    """Target-aware detail URL. Before this the staff email carried only the
    admin link, so the one page showing WHICH designs were picked, and what
    else the target holds, was not reachable from the notification at all.

    PUBLIC_BASE_URL is pinned rather than assumed: ``app.py`` calls
    ``load_dotenv()`` at import, so whichever test imported the app first
    leaks the repo-root .env's value into this process and the base differs
    between a solo run and a full-suite run. Same guard test_email_real.py
    already uses.
    """
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tools.ranomics.com")
    em.send_campaign_submitted_emails(
        campaign=_target_campaign([{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
        source_tools={"bindcraft": 1},
    )
    staff_mail = sent[1]
    assert f"Target: https://tools.ranomics.com/targets/{_TID}" in staff_mail["text"]
    assert f"/targets/{_TID}" in staff_mail["html"]


def test_a_campaign_row_links_back_to_its_run_not_a_target(sent, monkeypatch):
    """The sibling arm. _handoff_source_link branches on submission_source, so
    each shape has to be checked; a single 'whichever id is set' rule would
    have printed a target link for a campaign row once 0040 landed."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tools.ranomics.com")
    c = _campaign(refs=[{"job_id": "j1", "index": 0}])
    c.submission_source = "campaign"
    c.source_campaign_id = "cccccccc-1111-2222-3333-444444444444"
    c.source_job_id = None
    em.send_campaign_submitted_emails(campaign=c, user_email="s@example.com")
    staff_mail = sent[1]
    assert "Run: https://tools.ranomics.com/campaigns/cccccccc" in staff_mail["text"]
    assert "/targets/" not in staff_mail["text"]


def test_without_source_tools_no_designs_from_line_is_printed(sent):
    """Omitted, not printed empty. The campaign and single-job branches do not
    pass it, because they have exactly one tool by construction."""
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
    )
    assert "Designs from" not in sent[1]["text"]
    assert "Designs from" not in sent[1]["html"]


# ---------------------------------------------------------------------------
# ROUND 19 (register item A-7): the accepted count is not the requested one
# ---------------------------------------------------------------------------

def test_a_dropped_count_is_reported_to_both_parties(sent):
    """A user who starred ten designs and had three rejected reads "7
    candidates" as the number they chose, and ops reads it as the whole order.
    Neither message previously carried anything to compare it against.
    """
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": i} for i in range(7)]),
        user_email="scientist@example.com",
        dropped=3,
    )
    user_mail, staff_mail = sent
    user_html = _flat(user_mail["html"])
    assert "7 candidates)" in user_html
    assert "3 starred designs could not be matched to a design in the results " \
        "this shortlist was built from" in user_html
    assert "This request covers 7 designs." in user_html
    assert "3 starred design" in _flat(user_mail["text"])
    # Ops reads the staff mail, so the shortfall has to reach it too.
    assert "3 starred designs rejected" in _flat(staff_mail["html"])
    assert "Not included: 3 starred design(s) rejected" in staff_mail["text"]


def test_the_plain_text_body_carries_the_count_its_shortfall_note_compares_to(sent):
    """ROUND 20. The note names a request size and the text body never stated
    one: it read "Only the 7 above were sent" in a message with no 7 anywhere
    above it, because the count lived only in the HTML lead. The sentence was
    also a claim about STAGING, which nothing in this module observes -- so it
    now reports what the ROW covers, and the text body states that figure.
    """
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": i} for i in range(7)]),
        user_email="scientist@example.com",
        dropped=3,
    )
    user_text = sent[0]["text"]
    assert "(7 candidates)" in user_text
    assert "This request covers 7 designs." in user_text
    # The claim that was never verified, in either body.
    assert "were sent" not in user_text
    assert "were sent" not in _flat(sent[0]["html"])


def test_no_dropped_count_means_no_shortfall_wording_anywhere(sent):
    """The pair. Rendering the note unconditionally would satisfy the test
    above while telling every clean submission something went missing."""
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
    )
    for mail in sent:
        assert "could not be matched" not in _flat(mail["html"])
        assert "Not included" not in _flat(mail["html"])
        assert "Not included" not in mail["text"]
        assert "over the per-request limit" not in _flat(mail["html"])
        assert "Over the limit" not in mail["text"]


def test_a_single_dropped_design_reads_as_singular(sent):
    """`1 starred designs were not included` is the kind of thing that makes a
    paid-intake message look automated and untrustworthy."""
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
        dropped=1,
    )
    user_html = _flat(sent[0]["html"])
    assert "1 starred design could not be matched" in user_html
    assert "was left out" in user_html
    assert "This request covers 1 design." in user_html


def test_a_truncated_count_is_reported_separately_from_a_rejection(sent):
    """ROUND 20. `_MAX_CANDIDATE_REFS` cuts the shortlist at parse time, so
    those designs were never judged against the target at all. Folding them
    into `dropped` would assert a verdict nobody reached, and would give them
    the rejection's remedy -- when the one that works is a second, smaller
    request.
    """
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": i} for i in range(500)]),
        user_email="scientist@example.com",
        truncated=120,
    )
    user_mail, staff_mail = sent
    user_html = _flat(user_mail["html"])
    assert "120 further starred designs were over the per-request limit" \
        in user_html
    # It is NOT a rejection, so the rejection wording must not appear.
    assert "could not be matched" not in user_html
    assert "120 starred refs past the per-request cap" in _flat(staff_mail["html"])
    assert "Over the limit: 120 starred ref(s) past the per-request cap" \
        in staff_mail["text"]


def test_the_truncated_count_is_hedged_for_the_customer_and_exact_for_ops(sent):
    """ROUND 21, THE UNIT MISMATCH. `truncated` counts REFS: the tail past
    `_MAX_CANDIDATE_REFS` is never parsed into (job, index) pairs, so a repeat
    hiding in it cannot be subtracted and the figure is an UPPER BOUND on the
    designs actually missing.

    The customer's sentence counts designs, so it must say "up to"; the staff
    row already counts refs and must keep saying so. Both bodies previously said
    "starred designs" for the same number, so the two parties were handed
    different-unit answers under one noun.
    """
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": i} for i in range(500)]),
        user_email="scientist@example.com",
        truncated=120,
    )
    user_mail, staff_mail = sent
    assert "Up to 120 further starred designs" in _flat(user_mail["html"])
    assert "Up to 120 further starred designs" in user_mail["text"]
    # Ops keeps the exact unit, on both the table row and the text body.
    staff_html = _flat(staff_mail["html"])
    assert "120 starred refs past the per-request cap" in staff_html
    assert "Up to" not in staff_html
    assert "starred designs" not in staff_html


def test_the_truncation_note_gives_no_advice_that_duplicates_the_order(sent):
    """MEDIUM-4. This note used to say "Star them again on the target page and
    send a second request".

    Following it created a SECOND paid lab project covering the SAME designs:
    `static/js/candidate_table.js` never clears the shortlist and the modal
    serialises it in stored order, so the second POST carries the identical
    first `_MAX_CANDIDATE_REFS` refs -- and the designs over the limit are still
    over it. The route is now `@idempotent()`, but its TTL is 60 seconds, which
    makes it a double-click guard rather than a remedy, so nothing here may
    promise that a resend is harmless either.

    Asserted as an absence plus a replacement, not as an absence alone: a note
    reduced to silence would pass half of this while leaving a user who lost 120
    designs with no idea what happens next.
    """
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": i} for i in range(500)]),
        user_email="scientist@example.com",
        truncated=120,
    )
    for mail in sent:
        body = _flat(mail["html"]) + " " + mail["text"]
        assert "send a second request" not in body
        assert "Star them again" not in body
        assert "star them again" not in body
    user_html = _flat(sent[0]["html"])
    assert "would repeat this request rather than add them" in user_html
    assert "will follow up about the rest" in user_html


def test_both_shortfalls_at_once_read_as_two_separate_sentences(sent):
    """They can co-occur: a 620-star shortlist is truncated to 500 AND can have
    refs among those 500 that fail the provenance check. One merged number
    would have to be wrong about one of the two."""
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": i} for i in range(498)]),
        user_email="scientist@example.com",
        dropped=2,
        truncated=120,
    )
    user_html = _flat(sent[0]["html"])
    assert "2 starred designs could not be matched" in user_html
    assert "120 further starred designs were over the per-request limit" \
        in user_html
    assert "This request covers 498 designs." in user_html
    staff_text = sent[1]["text"]
    assert "Not included: 2 starred design(s) rejected" in staff_text
    assert "Over the limit: 120 starred ref(s)" in staff_text


def test_a_single_truncated_design_reads_as_singular(sent):
    """Exactly 501 well-formed refs is one over the cap, and reads as one.

    THE FIXTURE IS THE 501st REF, not a bare `truncated=1`. `truncated` is
    `requested - len(accepted)` and `accepted` saturates at
    `_MAX_CANDIDATE_REFS`, so `truncated=1` can only ever occur ALONGSIDE a
    persisted shortlist at the cap. The earlier version of this test passed one
    ref with `truncated=1` -- a combination the route cannot produce -- so its
    docstring described a state its fixture had not built, and the singular
    grammar was being checked against an impossible campaign row.
    """
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    refs = [{"job_id": "j1", "index": i} for i in range(_MAX_CANDIDATE_REFS)]
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=refs),
        user_email="scientist@example.com",
        truncated=1,
    )
    user_html = _flat(sent[0]["html"])
    assert "Up to 1 further starred design was over the per-request limit" \
        in user_html
    assert "rather than add it" in user_html
    # Singular on the staff row too, which counts the same thing in refs.
    assert "1 starred ref past the per-request cap" in _flat(sent[1]["html"])


# ---------------------------------------------------------------------------
# A88: the shortfall sentence serves every parent kind
# ---------------------------------------------------------------------------

_SHORTFALL = re.compile(r"3 starred designs could not be matched to [^.]+\.")


def _shortfall_sentence(payload) -> str:
    match = _SHORTFALL.search(_flat(payload["html"]))
    assert match, _flat(payload["html"])
    return match.group(0)


def test_the_shortfall_sentence_is_the_same_for_every_parent_kind(sent):
    """ONE STRING, EVERY PARENT. The sentence used to say the designs "could
    not be matched to a design on this target", which is false for the campaign
    arm: that arm has no target in scope and refuses a ref because it is not a
    child of the named compute campaign.

    Fixed by REWORDING rather than by branching on ``submission_source``, and
    this test is why. The fixture in this file builds a campaign object with no
    ``submission_source`` attribute at all, so a branch would silently take its
    else-arm in every other test here and the suite would stay green while the
    sentence went wrong -- and the branch would acquire a stale else-arm the
    moment a fifth source is added besides. Three sources, byte-identical
    output.
    """
    seen = []
    for source in ("web", "campaign", "target"):
        sent.clear()
        campaign = _campaign(refs=[{"job_id": "j1", "index": i} for i in range(7)])
        campaign.submission_source = source
        em.send_campaign_submitted_emails(
            campaign=campaign, user_email="scientist@example.com", dropped=3,
        )
        seen.append(_shortfall_sentence(sent[0]))
    assert len(set(seen)) == 1, seen
    assert seen[0] == (
        "3 starred designs could not be matched to a design in the results "
        "this shortlist was built from and were left out."
    )


def test_a_campaign_arm_shortfall_names_no_target(sent):
    """The pair, stated as the thing that was wrong. A campaign-sourced
    handoff's refusals are decided against "child of this compute campaign";
    naming a target in that message describes a check nobody ran."""
    campaign = _campaign(refs=[{"job_id": "j1", "index": i} for i in range(7)])
    campaign.submission_source = "campaign"
    em.send_campaign_submitted_emails(
        campaign=campaign, user_email="scientist@example.com", dropped=3,
    )
    sentence = _shortfall_sentence(sent[0])
    assert "on this target" not in sentence
    assert "source target" not in sentence


# ---------------------------------------------------------------------------
# The assay named in customer copy (A93)
# ---------------------------------------------------------------------------
#
# The customer HTML hardcoded "yeast display", so a mammalian_display or dms
# submission was confirmed back with an assay the customer never picked. The
# _campaign fixture above pins assay_type="yeast_display", so every other test
# in this file renders a yeast row: a label map returning "yeast display" for
# all three values would pass all of them. The parametrised presence checks
# below close that gap -- each asserts the label its own row records rather
# than whatever the map returns, so collapsing the map to a single label fails
# their mammalian_display and dms cases in both bodies. The absence assertions
# in test_customer_copy_names_no_assay_but_the_row_s_own cover what presence
# cannot see: a body naming the row's assay AND a second one alongside it.

_ALL_CUSTOMER_LABELS = ("yeast display", "mammalian display",
                        "deep mutational scanning")
# The staff table's transform, which must never appear in customer copy: it is
# title case and renders 'dms' as "Dms".
_STAFF_FORMS = ("Yeast Display", "Mammalian Display", "Dms")


_MISSING = object()


def _with_assay(assay):
    """A submitted campaign carrying `assay`. ``_MISSING`` deletes the
    attribute entirely, which is a different failure from None."""
    c = _campaign(refs=[{"job_id": "j1", "index": 0}, {"job_id": "j1", "index": 1}])
    if assay is _MISSING:
        del c.assay_type
    else:
        c.assay_type = assay
    return c


def _customer(sent):
    """The customer confirmation's two bodies. It is sent first."""
    return _flat(sent[0]["html"]), _flat(sent[0]["text"])


@pytest.mark.parametrize("assay,label", [
    ("yeast_display", "yeast display"),
    ("mammalian_display", "mammalian display"),
    ("dms", "deep mutational scanning"),
])
def test_customer_html_names_the_assay_the_row_records(sent, assay, label):
    em.send_campaign_submitted_emails(
        campaign=_with_assay(assay), user_email="scientist@example.com",
    )
    html, _ = _customer(sent)
    assert f"your {label} scoping request for" in html


@pytest.mark.parametrize("assay,label", [
    ("yeast_display", "Yeast display"),
    ("mammalian_display", "Mammalian display"),
    ("dms", "Deep mutational scanning"),
])
def test_customer_plain_text_names_the_assay_the_row_records(sent, assay, label):
    """The text half never claimed an assay before this change; it now leads
    with the same one the HTML does, capitalised because it is sentence
    initial."""
    em.send_campaign_submitted_emails(
        campaign=_with_assay(assay), user_email="scientist@example.com",
    )
    _, text = _customer(sent)
    assert text.startswith(f"{label} scoping request received for HER2")


@pytest.mark.parametrize("assay,label", [
    ("yeast_display", "yeast display"),
    ("mammalian_display", "mammalian display"),
    ("dms", "deep mutational scanning"),
])
def test_customer_copy_names_no_assay_but_the_row_s_own(sent, assay, label):
    """No assay other than the row's own appears in either customer body.

    The parametrised presence checks above already fail a map that returns one
    label for everything: forcing that map fails their mammalian_display and
    dms cases. This adds the case they cannot see -- copy that names the row's
    own assay AND a second one elsewhere in the same body.
    """
    em.send_campaign_submitted_emails(
        campaign=_with_assay(assay), user_email="scientist@example.com",
    )
    html, text = _customer(sent)
    for other in _ALL_CUSTOMER_LABELS:
        if other == label:
            continue
        assert other not in html.lower(), other
        assert other not in text.lower(), other


@pytest.mark.parametrize("assay", ["yeast_display", "mammalian_display", "dms"])
def test_customer_copy_never_uses_the_staff_title_case_forms(sent, assay):
    """The HTML embeds the label mid-sentence after "your" and the text body
    opens a sentence with it, so the staff table's .title() transform -- which
    also renders 'dms' as "Dms" -- is the wrong vocabulary for either."""
    em.send_campaign_submitted_emails(
        campaign=_with_assay(assay), user_email="scientist@example.com",
    )
    html, text = _customer(sent)
    for form in _STAFF_FORMS:
        assert form not in html, form
        assert form not in text, form


@pytest.mark.parametrize("assay", ["yeast_display", "mammalian_display", "dms"])
def test_customer_copy_never_shows_the_raw_enum(sent, assay):
    em.send_campaign_submitted_emails(
        campaign=_with_assay(assay), user_email="scientist@example.com",
    )
    html, text = _customer(sent)
    assert assay not in html, assay
    assert assay not in text, assay


@pytest.mark.parametrize("missing", [None, "", "  ", _MISSING])
def test_customer_copy_drops_the_adjective_when_the_assay_is_unknown(sent, missing):
    """No assay to name, so the sentence names none rather than guessing one.

    A row in this state is not reachable through any writer in this repo --
    assay_type is NOT NULL and CHECKed, and all four writers validate first --
    so this pins the behaviour of the fallback, not of any stored row.
    """
    em.send_campaign_submitted_emails(
        campaign=_with_assay(missing), user_email="scientist@example.com",
    )
    html, text = _customer(sent)
    assert "your scoping request for" in html
    assert text.startswith("Scoping request received for HER2")
    for label in _ALL_CUSTOMER_LABELS:
        assert label not in html.lower(), label
        assert label not in text.lower(), label
    assert "None" not in html
    assert "None" not in text


@pytest.mark.parametrize("unknown", ["phage_display", "bli"])
def test_an_assay_outside_the_map_is_dropped_rather_than_printed(sent, unknown):
    """Cover for a future widening of the assay_type CHECK: an enum this
    module has no customer copy for must not reach the customer as an enum."""
    em.send_campaign_submitted_emails(
        campaign=_with_assay(unknown), user_email="scientist@example.com",
    )
    html, text = _customer(sent)
    assert "your scoping request for" in html
    assert unknown not in html
    assert unknown not in text


def test_the_customer_subject_names_no_assay(sent):
    """Left alone deliberately. It never named an assay, so there was nothing
    to correct; this pins that so a later edit cannot reintroduce a hardcoded
    one on the one line no body test reads."""
    em.send_campaign_submitted_emails(
        campaign=_with_assay("dms"), user_email="scientist@example.com",
    )
    subject = sent[0]["subject"]
    assert subject == "Scoping request received — HER2"
    for label in _ALL_CUSTOMER_LABELS:
        assert label not in subject.lower()


@pytest.mark.parametrize("assay,staff_label", [
    ("yeast_display", "Yeast Display"),
    ("mammalian_display", "Mammalian Display"),
    ("dms", "Dms"),
])
def test_the_staff_notify_keeps_its_own_title_case_assay(sent, assay, staff_label):
    """Staff copy was never wrong and is not being restyled: it reads the row
    and prints the title-cased form in both of its bodies."""
    em.send_campaign_submitted_emails(
        campaign=_with_assay(assay), user_email="scientist@example.com",
    )
    staff = sent[1]
    assert f"<td>{staff_label}</td>" in _flat(staff["html"])
    assert f"Assay: {staff_label}\n" in staff["text"]


@pytest.mark.parametrize("missing", [None, _MISSING])
def test_a_missing_assay_still_sends_both_emails(sent, missing):
    """The staff half read campaign.assay_type.replace(...) directly, above the
    only try block in the sender, so a row without that attribute raised into
    the callers' except blocks and lost BOTH messages. Guarding only the
    customer half would have left the unknown-assay wording unreachable."""
    em.send_campaign_submitted_emails(
        campaign=_with_assay(missing), user_email="scientist@example.com",
    )
    assert len(sent) == 2
    assert "Assay: —\n" in sent[1]["text"]
