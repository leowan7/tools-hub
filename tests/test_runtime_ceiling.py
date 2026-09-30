"""The runtime reconciliation: what one GPU container is stopped at vs what we start.

WHY THIS FILE EXISTS. Paid runs failed through our fault in September. The QA
brief calls it six runs and names five by job id; of those five, THREE died on
the pipeline's own wall-clock (``c43329f3``, ``dd7eaf99``, ``b5707a1d``) and
were refunded in full, which is the class this file is about. The other two are
different mechanisms and are not addressed here: a no-progress timeout
(refunded) and a safety kill that absorbed $3.63
(``docs/qa/RUNTIME-CEILING-2026-09-30.md`` section 1). Nothing in the hub had ever compared
the runtime a job would take against the timeout the pipeline actually enforces:
``gpu/modal_client.py``'s PRESET_CAPS is never sent in the Modal payload, the
Modal function timeout is 23h, and the bound that fires is a hardcoded
``subprocess`` timeout inside llm-proteinDesigner's ``run_pipeline.py`` (14400s
for bindcraft, 6600s for boltzgen). A kill there raises TimeoutExpired, which
is not caught at the call site, so the wrapper posts a bucket-less FAILED
webhook, ``shared/jobs.py::classify_terminal_state`` buckets it
``unclassified``, the hold is refunded in full, and every Accepted design is
discarded. The customer pays nothing and we absorb the whole GPU bill.

WHAT IS PINNED. Three things, each of which was silently wrong before:
  * The two runtime curves still reproduce the one real run each tool has been
    measured at. The curves are now load-bearing (they gate submissions), so a
    drift that used to only mis-label a panel now mis-admits a job.
  * Every (target size, design count) whose estimate overruns its tool's
    ceiling is REFUSED, on the campaign money routes as well as the tool form.
  * A campaign chunk is planned against the timeout that actually stops it, not
    against the 23h Modal session. bindcraft chunks were sized at 36000s
    against a 14400s kill.

WHAT IS NOT PINNED, AND IS NOT CLAIMED ANYWHERE. That a run which PASSES the
gate finishes. Job c43329f3 was estimated at 74.5 min and hit 14400s -- at
LEAST 3.2x over, since it was killed rather than finished -- so the curve's
exponent is optimistic somewhere above the one size it
was fitted at. This gate refuses what the estimator says cannot finish -- the
estimator, not a proof: both alphas are fitted on one target size per tool, so
the boundary itself inherits that uncertainty. It does not make the estimate
trustworthy in the other direction either. Both alphas are unmeasured (one measured target size
per tool), and the follow-ups that would close the rest are in
``docs/qa/RUNTIME-CEILING-2026-09-30.md``.
"""

from __future__ import annotations

import pytest

from shared.compute_campaigns import (
    _campaign_container_seconds,
    _chunk_size_for,
)
from shared.pdb_preflight import size_only_refusal
from shared.pdb_preflight_rules import (
    TOOL_RULES,
    largest_target_aa_within_ceiling,
    max_designs_within_ceiling,
    runtime_estimate_min,
)

# Tools whose envelope declares the pipeline timeout its container is stopped at.
CEILING_TOOLS = sorted(
    slug for slug, rules in TOOL_RULES.items() if rules.size.runtime_ceiling_s
)


def test_there_is_at_least_one_ceiling_tool():
    """Guards the two loops below against vacuously passing on an empty set."""
    assert CEILING_TOOLS == ["bindcraft", "boltzgen"]


def test_boltzgen_runtime_curve_reproduces_its_one_measured_run():
    """docs/VALIDATION-LOG.md, job 758c45e5 (2026-05-28).

    4944 GPU-s = 82.4 min, at 4ZQK chain A (115 aa) with the pinned 200-design
    pool. The envelope is anchored on exactly this point -- 86.0 min at 120 aa
    -- so reproducing it is arithmetic, not a coincidence, and that is the
    point: if someone re-anchors the curve without a new measurement, this
    fails.
    """
    rules = TOOL_RULES["boltzgen"]
    est = runtime_estimate_min(rules, 115, rules.size.runtime_fixed_designs)
    assert est == pytest.approx(82.4, abs=0.2)


def test_bindcraft_runtime_curve_reproduces_its_one_measured_run():
    """docs/VALIDATION-LOG.md, job 1c4d5803: 1170 GPU-s = 19.5 min, 2 designs
    at 4ZQK chain A (115 aa). The curve predicts 18.75 min there.
    """
    est = runtime_estimate_min(TOOL_RULES["bindcraft"], 115, 2)
    assert est == pytest.approx(19.5, rel=0.05)


@pytest.mark.parametrize("tool", CEILING_TOOLS)
@pytest.mark.parametrize("target_aa", [60, 115, 153, 200, 250, 300, 360, 421, 480])
@pytest.mark.parametrize("num_designs", [1, 2, 4, 6, 16, 50])
def test_a_run_that_cannot_finish_is_refused(tool, target_aa, num_designs):
    """The reconciliation invariant: refused if and only if it overruns.

    ``size_only_refusal`` is the shared gate: ``blueprints/campaigns.py``
    reaches it on both branches, ``blueprints/targets.py`` on the multi-tool
    launch route, and ``preflight_for_tool`` shares ``_check_size_envelope``
    with it. So this pins the VERDICT for all four, but not that each caller
    asks for it -- for a PER-DESIGN tool a caller that omits ``num_designs``
    gets no runtime estimate at all, and this test cannot see that. (A
    pinned-pool tool like boltzgen is judged anyway: ``_check_size_envelope``
    reads ``env.runtime_fixed_designs or num_designs``.)
    ``blueprints/targets.py`` shipped exactly that hole, and it is a bindcraft
    hole for exactly that reason. One route-level test per money route covers the asking:
    ``tests/test_campaign_size_gates.py`` and
    ``tests/test_target_multi_launch_routes.py::
    test_a_bindcraft_chunk_that_cannot_finish_is_refused_on_this_route``.
    """
    rules = TOOL_RULES[tool]
    env = rules.size
    # The count the CONTAINER runs. boltzgen's form field is a filter budget;
    # the pool it folds is pinned at 200 regardless (SizeEnvelope
    # .runtime_fixed_designs), so the form number does not move its runtime.
    effective = env.runtime_fixed_designs or num_designs
    overruns = runtime_estimate_min(rules, target_aa, effective) * 60 > env.runtime_ceiling_s
    refusal = size_only_refusal(tool, target_aa, num_designs=num_designs)
    if overruns:
        assert refusal is not None, (
            f"{tool} {num_designs}d at {target_aa}aa overruns "
            f"{env.runtime_ceiling_s}s and was ACCEPTED"
        )
        assert "returns nothing" in refusal
    elif target_aa <= env.hard_cap_target_aa:
        # Above the hard cap a refusal is expected but for the other reason,
        # so only sizes inside the envelope pin acceptance.
        assert refusal is None, f"{tool} {num_designs}d at {target_aa}aa wrongly refused"


@pytest.mark.parametrize("tool", CEILING_TOOLS)
def test_the_inverses_land_on_the_boundary(tool):
    """The numbers the refusal copy quotes have to be runnable.

    Both messages name a value the user is told to fall back to. If an inverse
    is off by one in the wrong direction, the gate refuses the fix it just
    recommended.
    """
    rules = TOOL_RULES[tool]
    env = rules.size

    fits_aa = largest_target_aa_within_ceiling(rules)
    assert fits_aa > 0
    n = env.runtime_fixed_designs or env.runtime_baseline_designs
    assert runtime_estimate_min(rules, fits_aa, n) * 60 <= env.runtime_ceiling_s
    assert runtime_estimate_min(rules, fits_aa + 1, n) * 60 > env.runtime_ceiling_s

    aa = min(300, env.hard_cap_target_aa)
    fits_n = max_designs_within_ceiling(rules, aa)
    if env.runtime_fixed_designs:
        # The design-count inverse is not applicable: the estimator ignores
        # the caller's count for a pinned pool, so every count gives the same
        # runtime and a recommended count would be a lie. It has to say so
        # rather than hand back a number that does not change anything.
        assert fits_n == 0
        return
    assert fits_n >= 1
    assert runtime_estimate_min(rules, aa, fits_n) * 60 <= env.runtime_ceiling_s
    assert runtime_estimate_min(rules, aa, fits_n + 1) * 60 > env.runtime_ceiling_s


def test_the_advertised_defaults_still_run():
    """A gate that refuses the front page is a broken gate.

    bindcraft's form defaults to 4 designs (``tools/bindcraft/__init__.py``);
    both tools were measured at 115 aa and quote a 300 / 360 aa soft warn.
    """
    assert size_only_refusal("bindcraft", 115, num_designs=4) is None
    assert size_only_refusal("bindcraft", 300, num_designs=4) is None
    assert size_only_refusal("boltzgen", 115, num_designs=4) is None


@pytest.mark.parametrize("tool", CEILING_TOOLS)
def test_a_campaign_chunk_is_planned_against_the_timeout_that_stops_it(tool):
    """bindcraft chunks were planned against 36000s and killed at 14400s.

    The container a chunk is sized from must not exceed the pipeline timeout
    the envelope declares, or ``plan_chunks`` hands out chunks that cannot
    finish by construction -- and a chunk that is killed is refunded, so we
    absorb its GPU bill and lose its designs.
    """
    ceiling = TOOL_RULES[tool].size.runtime_ceiling_s
    assert _campaign_container_seconds(tool, "pilot") <= ceiling


def test_a_bindcraft_campaign_chunk_fits_its_container_at_the_measured_size():
    """The chunk size derived from the corrected container has to be usable.

    At the one size bindcraft has been measured at, a whole chunk must fit
    inside the timeout -- otherwise every chunk of every campaign overruns.
    """
    chunk = _chunk_size_for("bindcraft", "pilot")
    assert chunk >= 1
    est_s = runtime_estimate_min(TOOL_RULES["bindcraft"], 115, chunk) * 60
    assert est_s <= TOOL_RULES["bindcraft"].size.runtime_ceiling_s


@pytest.mark.parametrize("tool,num_designs", [("bindcraft", 6), ("boltzgen", None)])
def test_an_over_cap_refusal_keeps_its_fix_sentence(tool, num_designs):
    """The two flags are INDEPENDENT, and the message is not.

    ``over_runtime_ceiling`` and ``over_hard_cap`` are computed separately, but
    ``_check_size_envelope``'s elif chain gives the CAP its message when both
    fire. A ``size_only_refusal`` that returned early on the ceiling flag
    therefore shipped the cap message with no action in it at all -- and for a
    pinned-pool tool that needs no caller to pass a count, which is why
    boltzgen is parametrized with None: every boltzgen size refusal on both
    campaign routes was advice-free.
    """
    rules = TOOL_RULES[tool]
    aa = rules.size.hard_cap_target_aa + 200
    msg = size_only_refusal(tool, aa, num_designs=num_designs)
    assert msg is not None
    assert "Narrow the target region to at most" in msg, msg


def test_both_panels_read_the_same_envelope_fields():
    """The AJAX panel shipped blind to ``over_runtime_ceiling``.

    ``_check_size_envelope`` clears ``over_soft_warn`` when the ceiling fires
    (``shared/pdb_preflight.py``), so a panel keyed on ``over_soft_warn``
    renders the one refusal that is ABOUT runtime unhighlighted and with no
    runtime line. The server twin was updated with the gate and
    ``shared/pdb_intake.py::_verdict_to_json`` was not, which is the drift the
    comment at ``static/js/preflight.js`` says the mirror exists to prevent.

    This compares the fields the Jinja twin reads against the keys
    ``_verdict_to_json`` emits, matched by NAME over its whole source rather
    than only its size block -- permissive in the other direction (a key from
    another block would satisfy a template field of the same name), and enough
    to fail when a field is added to one panel and not the other.
    """
    import inspect
    import re
    from pathlib import Path

    from shared.pdb_intake import _verdict_to_json

    template = Path("templates/components/preflight_panel.html").read_text(
        encoding="utf-8"
    )
    read_by_template = set(
        re.findall(r"verdict\.size_envelope\.(\w+)", template)
    )
    assert "over_runtime_ceiling" in read_by_template, (
        "the server twin stopped reading the flag; this test is now vacuous"
    )
    emitted = set(re.findall(r'"(\w+)":', inspect.getsource(_verdict_to_json)))
    assert not (read_by_template - emitted), (
        "server-rendered panel reads envelope fields the JSON panel never "
        f"receives: {sorted(read_by_template - emitted)}"
    )

    # The flag has to be read in the branch that actually receives a ceiling
    # verdict. A mention anywhere in the file is not enough: the first repair
    # put the whole envelope block in ``renderVerdict``'s ready branch, where
    # ``shared/pdb_preflight.py:689-708`` guarantees the flag is false, so the
    # refusal rendered with no envelope at all while this assertion passed.
    js = Path("static/js/preflight.js").read_text(encoding="utf-8")
    needs_fix_at = js.index('v.kind === "needs_fix"')
    # Bounded at the far end too: `if (!html)` is the first statement after the
    # kind branches close, so the slice is the needs_fix branch and nothing
    # else. Slicing to end-of-file would pass on a mention in any later
    # function.
    branch_end = js.index("if (!html) {", needs_fix_at)
    assert "over_runtime_ceiling" in js[needs_fix_at:branch_end], (
        "the JS reads the ceiling flag only before its needs_fix branch, and a "
        "ceiling refusal is never kind=ready"
    )


def test_a_pinned_pool_tool_keeps_its_runtime_figure_on_the_result_page():
    """The form panel and the result page have to agree, and did not.

    ``job_preflight_for_display`` re-derives the minutes from the job's stored
    inputs, and boltzgen's validated inputs carry ``budget`` -- none of the
    three keys ``_parse_preflight_size_params`` reads. Once the envelope
    started substituting ``runtime_fixed_designs`` the form panel showed a
    figure the result page dropped. Nothing in the stored dict below is a
    design count, which is the point.
    """
    from shared.pdb_intake import job_preflight_for_display

    shown = job_preflight_for_display(
        {
            "budget": 12,
            "_preflight": {
                "tool_slug": "boltzgen",
                "size_envelope": {"residue_count": 115},
            },
        }
    )
    assert shown["size_envelope"]["runtime_estimate_min"] == pytest.approx(
        82.4, abs=0.2
    )


def test_the_ceiling_refusal_names_one_lever_not_two():
    """Reason and fix disagreed about which knob to turn.

    ``_check_size_envelope``'s per-design branch says "Ask for at most N designs
    against a target this size" whenever ``max_designs_within_ceiling`` finds a
    count that fits. The fix line was clamped to the RESIDUE inverse on every
    ceiling refusal regardless, so bindcraft at 115 aa / 100 designs asked for
    at most 25 designs and then to keep the target at or under 46 residues --
    40% of a target it runs happily at 25. Following it works, which is why this
    is copy rather than a dead end, but the two lines have to name the same
    lever.
    """
    from shared.pdb_preflight import _check_size_envelope, preflight_for_tool
    from tests.test_pdb_preflight import _chain_pdb

    rules = TOOL_RULES["bindcraft"]
    env = _check_size_envelope(rules, 115, binder_max_aa=None, num_designs=100)
    assert env.over_runtime_ceiling
    fits_n = max_designs_within_ceiling(rules, 115)
    assert fits_n >= 1
    assert f"at most {fits_n} designs" in env.hard_fail_message
    # The residue inverse at this count is far below the target, so a fix line
    # clamped to it is the contradiction.
    tight_aa = largest_target_aa_within_ceiling(rules, 100)
    assert tight_aa < 115

    verdict = preflight_for_tool(
        "bindcraft", _chain_pdb("A", range(1, 116)),
        target_chain="A", hotspots=[], num_designs=100,
    )
    assert verdict.kind.value == "needs_fix"
    assert f"at most {fits_n} designs" in verdict.reason
    assert f"{tight_aa} residues" not in verdict.suggested_fix, (
        "the fix demands a target smaller than one the reason says this count "
        f"runs on: {verdict.suggested_fix}"
    )


def test_a_revived_runtime_figure_brings_its_own_basis():
    """Reviving the minutes without the basis printed "82.4 min for None".

    ``job_preflight_for_display`` substitutes the pinned pool so a boltzgen job
    gets an estimate. A job stored before that substitution existed has BOTH
    ``runtime_estimate_min`` and ``runtime_basis`` as None, and
    ``templates/components/preflight_panel.html`` interpolates the basis
    directly with no Jinja ``finalize`` hook, so the literal string "None"
    reached the page.
    """
    from shared.pdb_intake import job_preflight_for_display

    env = job_preflight_for_display(
        {
            "budget": 12,
            "_preflight": {
                "tool_slug": "boltzgen",
                "size_envelope": {
                    "residue_count": 115,
                    "runtime_estimate_min": None,
                    "runtime_basis": None,
                },
            },
        }
    )["size_envelope"]
    assert env["runtime_estimate_min"] is not None
    assert env["runtime_basis"] == "200 designs"


def test_the_panel_posts_the_count_that_now_decides_admission():
    """The panel answered "ready" for a run submit refuses.

    ``/preflight`` parses the design count out of the posted form
    (``shared/pdb_intake.py::_parse_preflight_size_params``), and the panel's
    ``appendTargetFields`` posted only chain, hotspots and contig. So the panel
    always saw ``num_designs=None``, left the estimate None and could not reach
    the ceiling branch -- while submit refused a 100-trajectory bindcraft run.
    Harmless while the count only moved an advisory number; a false green light
    once it decides admission.
    """
    import re
    from pathlib import Path

    js = Path("static/js/preflight.js").read_text(encoding="utf-8")
    body = js[js.index("function appendTargetFields")
              :js.index("function appendBinderFields")]
    assert "designsInput" in body, (
        "the preflight POST omits the design count, so the panel cannot see "
        "the ceiling refusal that count causes"
    )
    # And the field it reads has to be one the server actually parses. The
    # element supplies its own name, so the names live in the selector.
    from shared.pdb_intake import _parse_preflight_size_params

    at = js.index("const designsInput")
    selector = js[at:js.index(");", at)]
    posted = set(re.findall(r'name="(\w+)"', selector))
    assert posted == {"num_designs", "designs_per_shard"}, posted
    assert _parse_preflight_size_params({"num_designs": "100"})[1] == 100
    assert _parse_preflight_size_params({"designs_per_shard": "7"})[1] == 7
