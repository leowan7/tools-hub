"""Regression: the headline "best design" must honour the tool's own bar.

Real completed job ``2b917b54-0871-44af-a3d1-5d07ea5dcaeb`` (esmfold2-design,
PD-L1 minibinder, n_seeds=2) shipped the WRONG sequence to the results page:

    seed0: ipTM 0.9556, pI 11.95 -> rejected
    seed1: ipTM 0.9354, pI  5.67 -> clears the bar

``_aggregate`` picked purely on iPTM, so ``best_sequence`` was seed0 -- a
158 aa poly-Leu/Arg run with pI ~12, a hallucination artifact that would be
insoluble and non-specific in the lab, rendered in a panel captioned
"De novo minibinder" with nothing marking it as rejected. That panel is the
copy-paste source for a peptide synthesis order.

THREE writers are pinned here, because fixing one does not reach the others:

1. ``modal_app._aggregate`` -- the multi-seed orchestrator.
2. ``run_pipeline._pick_best`` -- the single-seed path, which previously took
   ``design()``'s own top pick, a value computed with no knowledge of this
   tool's bar at all.
3. The results template, which DERIVES the pick from the measurements it
   renders. Neither pipeline fix can reach job 2b917b54: its wrong pick is
   already frozen in the database. Only a derivation repairs a job that has
   already run, which is the same reason #216 stopped storing verdicts.

The template tests therefore assert on measurements, and two of them prove a
stored verdict word cannot move the answer in either direction.
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

import pytest

from tools.esmfold2_design.modal_app import _aggregate
from tools.esmfold2_design.run_pipeline import _pick_best

# The two designs from job 2b917b54, binder sequences abbreviated.
DROP_SEQ = "LLRRLLRRLLRRGGGGGGGLLRRLLRR"
PASS_SEQ = "SEEDLTKAQNLIDEAKKLNDAQAPKG"

# The caution rendered above the sequence when it does not clear the bar.
CAUTION = "No design in this run clears the bar stated above"
UNCHECKED = "has not been checked against the bar"


def _cand(
    seq: str,
    iptm: float | None = None,
    pi: float | None = None,
    status: str | None = None,
    proxy: float | None = None,
    sequence: bool = True,
    designed: bool = True,
    name: str = "design_0",
) -> dict:
    """One candidate in the shape run_pipeline.py emits.

    ``status`` is omitted entirely when None, so a test can present the
    record shape a job written after the producers stop stamping a verdict
    will have. ``sequence`` / ``designed`` drop the binder-only and the
    concatenated field respectively: with neither, no binder can be quoted
    for this candidate at all and the panel has to fall back.
    """
    scores: dict = {
        "ipTM": iptm,
        "iPTM_proxy": proxy,
        "final_loss": 1.0,
        "pI": pi,
    }
    if status is not None:
        scores["filter_status"] = status
    cand = {
        "rank": 0,
        "name": name,
        "pdb_key": "design_0_complex.pdb",
        "scores": scores,
    }
    if designed:
        cand["designed_sequence"] = f"TARGET|{seq}"
    if sequence:
        cand["sequence"] = seq
    return cand


def _child(seed: int, cand: dict) -> dict:
    return {
        "exit_code": 0,
        "provider_job_id": f"child-{seed}",
        "smoke_result": {
            "status": "COMPLETED",
            "tier": "design",
            "preset": "default",
            "designs_total": 1,
            "designs_completed": 1,
            "n_failures": 0,
            "designs": [],
            "candidates": [cand],
            "runtime_seconds": 600,
        },
    }


def _design(
    seq: str,
    iptm: float | None,
    pi: float | None,
    status: str | None = None,
    proxy: float | None = None,
    cdr_proxy: float | None = None,
) -> dict:
    """One design in the shape _shape_designs emits (flat + nested scores).

    ``proxy`` and ``cdr_proxy`` are SEPARATE on purpose. Setting both to one
    value made the template's mode switch untestable: swapping the two keys
    in the reshape left the whole suite green.
    """
    design = {
        "rank": 0,
        "name": "design_0",
        "pdb_key": "design_0_complex.pdb",
        "designed_sequence": f"TARGET|{seq}",
        "sequence": seq,
        "iptm": iptm,
        "isoelectric_point": pi,
        "final_loss": 1.0,
        "distogram_iptm_proxy": proxy,
        "cdr_distogram_iptm_proxy": cdr_proxy,
    }
    if status is not None:
        design["filter_status"] = status
    return design


# ==========================================================================
# 1. The multi-seed orchestrator
# ==========================================================================


class TestMultiSeedBestSequence:
    def test_strict_pass_beats_higher_iptm_drop(self):
        """The exact seed0/seed1 numbers from job 2b917b54."""
        successes = [
            (0, _child(0, _cand(DROP_SEQ, 0.9556, 11.95, "drop"))),
            (1, _child(1, _cand(PASS_SEQ, 0.9354, 5.67, "strict_pass"))),
        ]
        smoke = _aggregate(successes, [], {"tier": "design", "job_id": "u"})[
            "smoke_result"
        ]
        assert smoke["best_sequence"] == PASS_SEQ
        # The rejected design still LEADS the table — only the headline pick
        # changed. Re-ordering the table would hide the measurement.
        assert smoke["candidates"][0]["scores"]["ipTM"] == 0.9556

    def test_borderline_beats_higher_iptm_drop(self):
        successes = [
            (0, _child(0, _cand(DROP_SEQ, 0.9556, 11.95, "drop"))),
            (1, _child(1, _cand(PASS_SEQ, 0.7300, 5.67, "borderline"))),
        ]
        smoke = _aggregate(successes, [], {"tier": "design", "job_id": "u"})[
            "smoke_result"
        ]
        assert smoke["best_sequence"] == PASS_SEQ

    def test_strict_pass_beats_borderline_with_higher_iptm(self):
        successes = [
            (0, _child(0, _cand(DROP_SEQ, 0.9000, 5.5, "borderline"))),
            (1, _child(1, _cand(PASS_SEQ, 0.8000, 5.67, "strict_pass"))),
        ]
        smoke = _aggregate(successes, [], {"tier": "design", "job_id": "u"})[
            "smoke_result"
        ]
        assert smoke["best_sequence"] == PASS_SEQ

    def test_all_dropped_falls_back_to_top_iptm(self):
        """Nothing cleared the bar — still return the best available. The
        page labels it; withholding it entirely would just hide the run."""
        successes = [
            (0, _child(0, _cand(DROP_SEQ, 0.9556, 11.95, "drop"))),
            (1, _child(1, _cand(PASS_SEQ, 0.4000, 11.20, "drop"))),
        ]
        smoke = _aggregate(successes, [], {"tier": "design", "job_id": "u"})[
            "smoke_result"
        ]
        assert smoke["best_sequence"] == DROP_SEQ

    def test_ties_within_a_tier_still_go_to_highest_iptm(self):
        successes = [
            (0, _child(0, _cand(DROP_SEQ, 0.8000, 5.5, "strict_pass"))),
            (1, _child(1, _cand(PASS_SEQ, 0.9000, 5.67, "strict_pass"))),
        ]
        smoke = _aggregate(successes, [], {"tier": "design", "job_id": "u"})[
            "smoke_result"
        ]
        assert smoke["best_sequence"] == PASS_SEQ

    def test_the_concatenated_sequence_never_becomes_best_sequence(self):
        """``designed_sequence`` is ``target|binder``. The old fallback
        returned it whole, so the one record class that reaches the fallback
        put the target into the field the order-form panel renders."""
        successes = [
            (0, _child(0, _cand(PASS_SEQ, 0.9354, 5.67, "strict_pass",
                                sequence=False))),
        ]
        smoke = _aggregate(successes, [], {"tier": "design", "job_id": "u"})[
            "smoke_result"
        ]
        assert smoke["best_sequence"] == PASS_SEQ
        assert "|" not in smoke["best_sequence"]
        assert "TARGET" not in smoke["best_sequence"]

    def test_an_unmeasured_design_does_not_lead_the_table(self):
        """The sort sentinel was -1.0, which is the negation of a PERFECT
        iPTM, so a design with no iPTM at all sorted above every measured
        one -- and candidates[0] is what the email and share card read."""
        successes = [
            (0, _child(0, _cand(DROP_SEQ, None, 5.5, "drop"))),
            (1, _child(1, _cand(PASS_SEQ, 0.6000, 5.67, "drop"))),
        ]
        smoke = _aggregate(successes, [], {"tier": "design", "job_id": "u"})[
            "smoke_result"
        ]
        assert smoke["candidates"][0]["scores"]["ipTM"] == 0.6000
        assert smoke["best_sequence"] == PASS_SEQ

    def test_the_summary_stores_no_verdict_of_its_own(self):
        """A ``best_filter_status`` key would be a stored verdict, which is
        what #216 removed from this page, and its NAME also trips the guard
        that keeps the word out of every template expression."""
        successes = [
            (0, _child(0, _cand(PASS_SEQ, 0.9354, 5.67, "strict_pass"))),
        ]
        smoke = _aggregate(successes, [], {"tier": "design", "job_id": "u"})[
            "smoke_result"
        ]
        assert "best_filter_status" not in smoke


# ==========================================================================
# 2. The single-seed pipeline
# ==========================================================================


class TestSingleSeedBestSequence:
    """``_pick_best`` is what the single-seed pipeline writes as
    ``best_sequence``, replacing design()'s own filter-blind top pick."""

    def test_strict_pass_beats_higher_iptm_drop(self):
        designs = [
            _design(DROP_SEQ, 0.9556, 11.95, "drop"),
            _design(PASS_SEQ, 0.9354, 5.67, "strict_pass"),
        ]
        assert _pick_best(designs)["sequence"] == PASS_SEQ

    def test_borderline_beats_drop(self):
        designs = [
            _design(DROP_SEQ, 0.9556, 11.95, "drop"),
            _design(PASS_SEQ, 0.7300, 5.67, "borderline"),
        ]
        assert _pick_best(designs)["sequence"] == PASS_SEQ

    def test_all_dropped_falls_back_to_first(self):
        designs = [
            _design(DROP_SEQ, 0.9556, 11.95, "drop"),
            _design(PASS_SEQ, 0.4000, 11.20, "drop"),
        ]
        assert _pick_best(designs)["sequence"] == DROP_SEQ

    def test_strict_pass_beats_borderline_with_higher_iptm(self):
        """Pins the ORDER of _FILTER_TIERS, not just its membership.
        Reversing the tuple left every other test in this file green."""
        designs = [
            _design(DROP_SEQ, 0.9000, 5.50, "borderline"),
            _design(PASS_SEQ, 0.8000, 5.67, "strict_pass"),
        ]
        assert _pick_best(designs)["sequence"] == PASS_SEQ

    def test_no_designs(self):
        assert _pick_best([]) is None


class TestSingleSeedPipelineWiring:
    """_pick_best above is tested in isolation. This pins that ``_run``
    actually writes it: reverting the summary line to design()'s own pick --
    re-introducing the original bug verbatim -- left every other test in this
    file green."""

    def _summary(self, monkeypatch, designs, upstream_pick):
        import json
        import sys
        import types

        from tools.esmfold2_design import run_pipeline as rp

        fake = types.ModuleType("binder_design")

        class _Designer:
            def load(self, *a, **k):
                return None

            def design(self, **kwargs):
                # (best_seq, trajectory, critic_results) — best_seq is the
                # upstream's own filter-blind pick.
                return upstream_pick, [], [{"critic_name": "x"}]

        fake.ESMFold2Design = _Designer
        monkeypatch.setitem(sys.modules, "binder_design", fake)
        monkeypatch.setenv(
            "JOB_PAYLOAD",
            json.dumps(
                {
                    "job_spec": {
                        "preset": "minibinder",
                        "target_name": "pdl1",
                        "target_sequence": "MTARGET",
                        "batch_size": 2,
                    },
                    "job_token": "tok",
                }
            ),
        )
        monkeypatch.setenv("JOB_ID", "2b917b54")
        monkeypatch.setattr(rp, "_shape_designs", lambda *a, **k: designs)
        monkeypatch.setattr(rp, "_dump_raw_critic_results", lambda *a: None)
        written: dict = {}
        monkeypatch.setattr(rp, "_write_result", written.update)
        assert rp._run() == 0
        return written

    def test_the_pipeline_writes_the_tier_pick_not_the_upstream_one(
        self, monkeypatch
    ):
        summary = self._summary(
            monkeypatch,
            designs=[
                _design(DROP_SEQ, 0.9556, 11.95, "drop"),
                _design(PASS_SEQ, 0.9354, 5.67, "strict_pass"),
            ],
            upstream_pick=f"TARGET|{DROP_SEQ}",
        )
        assert summary["best_sequence"] == PASS_SEQ
        assert summary["status"] == "COMPLETED"

    def test_upstreams_pick_survives_only_when_nothing_was_shaped(
        self, monkeypatch
    ):
        summary = self._summary(
            monkeypatch, designs=[], upstream_pick=f"TARGET|{DROP_SEQ}"
        )
        assert summary["best_sequence"] == DROP_SEQ


# ==========================================================================
# 3. The results page, which is the only half that reaches a finished job
# ==========================================================================


def test_every_stated_threshold_uses_the_classifiers_operator():
    """``_classify`` gates on ``>=``, so a design at exactly 0.75 passes.

    Three user-facing surfaces state that rule in prose and they disagreed:
    the tool page's output summary said "> 0.75" while the how-to-read-it on
    the SAME page said ">= 0.75", and the results panel said "> 0.75" about
    code that admits 0.75. Prose is the surface that goes unguarded in this
    repo, so this is the guard. Scoped to the threshold numbers, because
    other ">" in these files ("Seeds to run > 1") are correct.
    """
    from tools.esmfold2_design.run_pipeline import (
        STRICT_CDR_IPTM_PROXY,
        STRICT_IPTM,
        _classify,
    )

    # The behaviour the prose has to describe: a design sitting exactly on
    # the bar is admitted. If that ever changes, ">" becomes correct again
    # and this test should be the thing that says so.
    assert _classify(False, STRICT_IPTM, None, None, 5.0) == "strict_pass"
    assert _classify(True, None, None, STRICT_CDR_IPTM_PROXY, None) == (
        "strict_pass"
    )

    repo = pathlib.Path(__file__).resolve().parents[1]
    surfaces = [
        repo / "tools/esmfold2_design/meta.py",
        repo / "templates/tools/esmfold2_design_results.html",
    ]
    strictly_greater = re.compile(r"&gt;(?:&nbsp;|\s)*0\.(?:75|5)\b")
    at_least = re.compile(r"&ge;(?:&nbsp;|\s)*0\.(?:75|5)\b")

    for path in surfaces:
        text = path.read_text(encoding="utf-8")
        assert not strictly_greater.search(text), (
            f"{path.name} states a threshold the classifier does not use: "
            f"{strictly_greater.search(text).group(0)}"
        )
        assert at_least.search(text), (
            f"{path.name} no longer states either threshold; this check "
            f"would pass over prose that had simply been deleted"
        )


@pytest.fixture
def flask_app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app


def _render(flask_app, result: dict) -> str:
    job = SimpleNamespace(id="2b917b54", status="succeeded", result=result)
    with flask_app.test_request_context():
        return flask_app.jinja_env.get_template(
            "tools/esmfold2_design_results.html"
        ).render(job=job, send_target_tools=None)


# The copy-paste box itself, not "somewhere on the page". An earlier version
# of these tests anchored on a Jinja COMMENT that never renders, so its
# "block" was the whole rest of the document and the check was far weaker
# than it read.
_SEQ_BOX = re.compile(
    r"word-break: break-all;[^>]*>\s*([A-Za-z]+)\s*</div>", re.S
)


def _headline(html: str) -> str:
    """The sequence rendered in the copy-paste box."""
    match = _SEQ_BOX.search(html)
    assert match, "no sequence box rendered; this assertion would be hollow"
    return match.group(1)


def _flat(html: str) -> str:
    """Rendered output with runs of whitespace collapsed.

    Prose assertions must not depend on where the template happens to wrap a
    sentence -- re-wrapping a paragraph is not a behaviour change, and one of
    these checks silently stopped matching the first time a line moved.
    """
    return re.sub(r"\s+", " ", html)


def _badge(html: str) -> str | None:
    """The tier word in the best-design panel's badge, or None if no badge.

    Checked separately from the caution prose because the same word appears
    in both, so an assertion on the word alone was satisfied by either one
    and neither was actually pinned.

    Scoped to the text AFTER the panel title: the run's tier renders in an
    identical ``panel-badge`` span higher up the page, and an unscoped match
    returns that one instead.
    """
    flat = _flat(html)
    start = flat.find("Best design")
    assert start != -1, "best-design panel did not render"
    match = re.search(
        r'<span class="panel-badge"[^>]*>\s*([a-z ]+?)\s*</span>', flat[start:]
    )
    return match.group(1) if match else None


class TestResultsTemplate:
    """The page resolves its own best from the candidates it renders, so a
    job COMPLETED BEFORE the pipeline fix — whose stored best_sequence is the
    rejected design — shows the right one without a GPU re-run."""

    def _stored_result(self, **over) -> dict:
        result = {
            "status": "COMPLETED",
            "preset": "default",
            "is_antibody": False,
            "designs_total": 2,
            "designs_completed": 2,
            "n_seeds": 2,
            # What job 2b917b54 actually has stored: the rejected design.
            "best_sequence": DROP_SEQ,
            "designs": [],
            "candidates": [
                _cand(DROP_SEQ, 0.9556, 11.95, "drop"),
                _cand(PASS_SEQ, 0.9354, 5.67, "strict_pass"),
            ],
        }
        result.update(over)
        return result

    def test_the_stored_pick_is_not_the_one_rendered(self, flask_app):
        """The headline case: job 2b917b54 as it sits in the database."""
        html = _render(flask_app, self._stored_result())
        assert _headline(html) == PASS_SEQ
        assert CAUTION not in _flat(html)

    def test_the_derivation_needs_no_stored_verdict(self, flask_app):
        """Same numbers with the word absent entirely — the shape a job gets
        once the producers stop stamping one."""
        html = _render(
            flask_app,
            self._stored_result(
                candidates=[
                    _cand(DROP_SEQ, 0.9556, 11.95),
                    _cand(PASS_SEQ, 0.9354, 5.67),
                ]
            ),
        )
        assert _headline(html) == PASS_SEQ

    def test_a_stale_label_cannot_move_the_answer(self, flask_app):
        """Both words inverted against their measurements. A record labelled
        by a container version whose bar has since moved must not be able to
        float the rejected design or sink the good one."""
        html = _render(
            flask_app,
            self._stored_result(
                candidates=[
                    _cand(DROP_SEQ, 0.9556, 11.95, "strict_pass"),
                    _cand(PASS_SEQ, 0.9354, 5.67, "drop"),
                ]
            ),
        )
        assert _headline(html) == PASS_SEQ
        assert CAUTION not in _flat(html)

    def test_pi_is_a_hard_gate_at_any_iptm(self, flask_app):
        """pI 6.0 is outside ``pI < 6`` — an undisplayable scaffold is
        rejected however well it scores. This is the exact shape of the bug:
        the top iPTM in the run is not the design to order."""
        html = _render(
            flask_app,
            self._stored_result(
                candidates=[
                    _cand(DROP_SEQ, 0.9900, 6.0),
                    _cand(PASS_SEQ, 0.8000, 5.0),
                ]
            ),
        )
        assert _headline(html) == PASS_SEQ

    def test_scfv_mode_judges_the_cdr_proxy_and_has_no_pi(self, flask_app):
        """The mode-dependence is why this bar cannot be one conjunction in
        shared/score_legends: an scFv is judged on the CDR distogram proxy at
        0.50 and carries no pI at all."""
        html = _render(
            flask_app,
            self._stored_result(
                is_antibody=True,
                preset="scfv",
                candidates=[
                    _cand(DROP_SEQ, 0.9900, None, proxy=0.30),
                    _cand(PASS_SEQ, 0.8000, None, proxy=0.62),
                ],
            ),
        )
        assert _headline(html) == PASS_SEQ
        assert CAUTION not in _flat(html)

    def test_caution_and_badge_when_nothing_clears_the_bar(self, flask_app):
        html = _render(
            flask_app,
            self._stored_result(
                candidates=[
                    _cand(DROP_SEQ, 0.9556, 11.95),
                    _cand(PASS_SEQ, 0.4000, 11.20),
                ]
            ),
        )
        assert _headline(html) == DROP_SEQ
        assert CAUTION in _flat(html)
        assert _badge(html) == "rejected"
        # Stated BEFORE the box, because the box is what gets copied.
        assert _flat(html).index(CAUTION) < _flat(html).index(DROP_SEQ)

    def test_caution_and_badge_when_best_is_only_borderline(self, flask_app):
        html = _render(
            flask_app,
            self._stored_result(
                candidates=[
                    _cand(DROP_SEQ, 0.9556, 11.95),
                    _cand(PASS_SEQ, 0.7300, 5.67),
                ]
            ),
        )
        assert _headline(html) == PASS_SEQ
        assert CAUTION in _flat(html)
        assert _badge(html) == "borderline"

    def test_a_barely_missed_design_is_borderline_not_rejected(
        self, flask_app
    ):
        """Pins the WIDTH of the borderline band, not just its existence.
        Widening it left every other test green, and the band decides which
        of two very differently-worded cautions the reader gets."""
        just_inside = _render(
            flask_app,
            self._stored_result(candidates=[_cand(PASS_SEQ, 0.7000, 5.0)]),
        )
        just_outside = _render(
            flask_app,
            self._stored_result(candidates=[_cand(PASS_SEQ, 0.6999, 5.0)]),
        )
        assert _badge(just_inside) == "borderline"
        assert _badge(just_outside) == "rejected"

    def test_a_design_exactly_on_the_bar_clears_it(self, flask_app):
        """``_classify`` gates on >=, so 0.75 exactly is a pass. Pinned
        because no other fixture sits on the boundary, and because the panel
        prose states the same rule and has to agree with it."""
        html = _render(
            flask_app,
            self._stored_result(candidates=[_cand(PASS_SEQ, 0.7500, 5.0)]),
        )
        assert _badge(html) is None
        assert CAUTION not in _flat(html)
        assert "iptm&nbsp;&ge;&nbsp;0.75" in html

    def test_strict_pass_is_preferred_over_borderline(self, flask_app):
        """Pins the ORDER of the tier branches. Swapping them left every
        other template test green -- and this is the writer that reaches
        jobs which have already run."""
        html = _render(
            flask_app,
            self._stored_result(
                candidates=[
                    _cand(DROP_SEQ, 0.7300, 5.50),
                    _cand(PASS_SEQ, 0.8000, 5.67),
                ]
            ),
        )
        assert _headline(html) == PASS_SEQ
        assert _badge(html) is None

    def test_the_panel_says_when_its_pick_is_not_the_top_row(self, flask_app):
        """The pick need not be the table's first row, and without this the
        page shows a sequence that contradicts the table directly above it
        with nothing to explain the difference."""
        swapped = _render(flask_app, self._stored_result())
        assert _headline(swapped) == PASS_SEQ
        assert "not the top row below" in _flat(swapped)

        # ...and stays quiet when the pick IS the top row.
        agreeing = _render(
            flask_app,
            self._stored_result(
                candidates=[
                    _cand(PASS_SEQ, 0.9354, 5.67),
                    _cand(DROP_SEQ, 0.4000, 11.95),
                ]
            ),
        )
        assert _headline(agreeing) == PASS_SEQ
        assert "not the top row below" not in _flat(agreeing)

    def test_a_record_with_no_binder_sequence_says_it_is_unchecked(
        self, flask_app
    ):
        """Only a binder-only ``sequence`` can be offered as the thing to
        order. Records too old to carry one fall back to the stored pick,
        whose tier is then unknown — and an unknown tier must read as
        unchecked, never as a pass.

        The stored value on a record of this class is the CONCATENATION,
        because that is what modal_app's old fallback wrote for exactly the
        candidates that have no ``sequence``. The target half must not reach
        the order-form box.
        """
        html = _render(
            flask_app,
            self._stored_result(
                best_sequence=f"TARGET|{DROP_SEQ}",
                candidates=[
                    _cand(DROP_SEQ, 0.9556, 11.95,
                          sequence=False, designed=False),
                    _cand(PASS_SEQ, 0.9354, 5.67,
                          sequence=False, designed=False),
                ],
            ),
        )
        assert _headline(html) == DROP_SEQ
        assert "TARGET|" not in html
        assert _badge(html) is None
        assert UNCHECKED in _flat(html)
        assert _flat(html).index(UNCHECKED) < _flat(html).index(DROP_SEQ)

    def test_a_candidate_with_only_the_concatenation_is_still_eligible(
        self, flask_app
    ):
        """The two halves of this fix must agree on which candidates exist.

        modal_app._binder_only splits ``designed_sequence`` when there is no
        ``sequence``; the template used to skip such a candidate entirely.
        With a strict-pass design in that shape and a rejected one carrying a
        plain ``sequence``, the page picked the REJECTED design and printed
        "No design in this run clears the bar" — a false statement about a
        design it had silently excluded.
        """
        html = _render(
            flask_app,
            self._stored_result(
                candidates=[
                    _cand(DROP_SEQ, 0.9556, 11.95),
                    _cand(PASS_SEQ, 0.9354, 5.67, sequence=False),
                ]
            ),
        )
        assert _headline(html) == PASS_SEQ
        assert "TARGET|" not in html
        assert _badge(html) is None
        assert CAUTION not in _flat(html)

    def test_an_unmeasured_design_is_unjudged_not_rejected(self, flask_app):
        """A design can still arrive with no CDR proxy measured, and every one
        in such a run was being narrated as having failed a bar nothing
        measured it against. Unmeasured is unjudged, never failed.

        This used to say the proxy was absent "unless the scaling critics are
        on, and they are off by default". Wrong twice over: #242 established
        that upstream puts the proxy on the hero critic's row, so the ensemble
        never governed it, and the toggle that sentence describes no longer
        exists. The case it guards is real regardless of cause."""
        html = _render(
            flask_app,
            self._stored_result(
                is_antibody=True,
                preset="scfv",
                candidates=[_cand(PASS_SEQ, 0.9354, None, proxy=None)],
            ),
        )
        assert _headline(html) == PASS_SEQ
        assert _badge(html) is None
        assert CAUTION not in _flat(html)
        assert UNCHECKED in _flat(html)

    def test_the_panels_numbers_match_the_tables(self, flask_app):
        """The panel exists so a reader can tell WHICH design this is. That
        only works if its numbers are the table's numbers, digit for digit:
        a panel reading "pI 5.669" beside a cell reading "5.67" invites
        exactly the wrong conclusion.

        Compared against the RENDERED cells rather than against expected
        literals, so the two cannot drift apart without this failing.
        """
        # pI carries a third decimal so a formatter mismatch is visible.
        html = _render(
            flask_app,
            self._stored_result(
                candidates=[
                    _cand(DROP_SEQ, 0.9556, 11.95, proxy=0.623),
                    _cand(PASS_SEQ, 0.9354, 5.669, proxy=0.623,
                          name="seed1_rank1"),
                ]
            ),
        )
        flat = _flat(html)
        panel = re.search(
            r'font-family: var\(--font-mono\);">\s*(seed1_rank1.*?)</div>',
            flat,
        )
        assert panel, "the which-design line did not render"
        line = re.sub(r"<[^>]+>", "", panel.group(1))

        for label, column in (("iPTM", "ipTM"), ("proxy", "iPTM_proxy"),
                              ("pI", "pI")):
            shown = re.search(rf"{label} ([\d.]+)", line)
            assert shown, f"panel does not show {label}: {line!r}"
            cells = re.findall(
                rf'<td data-col="{column}" data-val="[^"]*">([^<]*)</td>',
                flat,
            )
            assert cells, f"table rendered no {column} cells"
            assert shown.group(1) in [c.strip() for c in cells], (
                f"panel {label}={shown.group(1)} matches no {column} cell "
                f"{cells}"
            )

    def test_legacy_designs_only_shape_still_resolves(self, flask_app):
        """Pre-candidates jobs: the template reshapes designs[] itself, and
        that reshape has to carry ``sequence`` across for the pick to be
        offerable at all."""
        result = self._stored_result()
        del result["candidates"]
        result["designs"] = [
            _design(DROP_SEQ, 0.9556, 11.95),
            _design(PASS_SEQ, 0.9354, 5.67),
        ]
        html = _render(flask_app, result)
        assert _headline(html) == PASS_SEQ

    def test_the_legacy_reshape_reads_the_mode_specific_proxy(
        self, flask_app
    ):
        """In scFv mode the reshape must take ``cdr_distogram_iptm_proxy``,
        not the plain one. The two are given DIFFERENT values here because
        with equal values the key swap is invisible."""
        result = self._stored_result(is_antibody=True, preset="scfv")
        del result["candidates"]
        result["designs"] = [
            # Plain proxy would clear the 0.50 bar; the CDR proxy does not.
            _design(DROP_SEQ, 0.9900, None, proxy=0.99, cdr_proxy=0.30),
            _design(PASS_SEQ, 0.8000, None, proxy=0.10, cdr_proxy=0.62),
        ]
        html = _render(flask_app, result)
        assert _headline(html) == PASS_SEQ
        assert _badge(html) is None

    def test_a_run_with_one_unmeasured_design_still_renders(self, flask_app):
        """The legacy reshape used to sort on scores.ipTM directly, which
        raises TypeError and 500s the entire results page as soon as one
        design has a null iPTM and another has a number."""
        result = self._stored_result()
        del result["candidates"]
        result["designs"] = [
            _design(DROP_SEQ, None, 11.95),
            _design(PASS_SEQ, 0.9354, 5.67),
        ]
        html = _render(flask_app, result)
        assert _headline(html) == PASS_SEQ
        # Unmeasured sorts LAST, so the measured design leads the table.
        assert "not the top row below" not in _flat(html)
