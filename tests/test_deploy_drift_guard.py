"""The deploy-drift guard in `.github/workflows/synthetic-smoke.yml`.

The guard's shell is not executed here. Running shell-inside-YAML needs a bash
the runner and every developer machine agree on, and that mismatch has already
cost this change a debugging cycle. What IS pinned here is the structure, and
every assertion below stands over a specific defect an independent QC round
found in the first version of this guard. Each one is a property that can rot
silently: the shell would still run, still exit 0, and still be wrong.

The guard exists because a merge to main does not reliably redeploy. On
2026-08-20 Railway created no deployment at all for `2ec3cce` (#173) and
production served the previous commit for 8.6 hours with every other alerting
layer correctly green -- stale code is healthy code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "synthetic-smoke.yml"
_ALERTING = _REPO / "ALERTING.md"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def guard_step(workflow: dict) -> dict:
    steps = workflow["jobs"]["deploy-drift"]["steps"]
    return next(s for s in steps if "run" in s)


def test_the_guard_is_its_own_job_not_a_step_before_the_smoke(workflow: dict):
    """As a step it exited 1 on four of five paths ahead of the smoke.

    A /health blip therefore deleted the deep end-to-end signal entirely, and
    the guard's own message told the reader to compare against "the smoke
    below" -- a result its placement guaranteed would not exist.
    """
    jobs = workflow["jobs"]
    assert "deploy-drift" in jobs, "the guard must be its own job"
    assert "smoke" in jobs

    smoke_run = " ".join(s.get("run", "") for s in jobs["smoke"]["steps"])
    assert "HEALTH_URL" not in smoke_run, (
        "the drift guard has leaked back into the smoke job; a /health failure "
        "would again suppress the Platform API smoke"
    )
    assert "needs" not in jobs["smoke"], (
        "the smoke must not depend on the drift job, or drift suppresses it "
        "exactly as it did when the guard was a step"
    )


def test_checkout_fetches_full_history(workflow: dict):
    """`merge-base` and `rev-list --before` have nothing to walk at depth 1."""
    checkout = next(
        s for s in workflow["jobs"]["deploy-drift"]["steps"]
        if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0


def test_the_bar_is_taken_from_the_remote_main_ref_never_head(guard_step: dict):
    """`workflow_dispatch` from a branch made HEAD that branch.

    The workflow advertises manual dispatch, and on any non-main ref the guard
    compared production against unmerged WIP and reported drift that did not
    exist.
    """
    assert guard_step["env"]["MAIN_REF"] == "refs/remotes/origin/main"
    rev_list = re.search(r"BAR=\$\(git rev-list[^\n]*", guard_step["run"])
    assert rev_list, "no BAR=$(git rev-list ...) line found"
    assert '"$MAIN_REF"' in rev_list.group(0)
    assert "HEAD" not in rev_list.group(0)


def test_the_bar_walks_first_parent_only(guard_step: dict):
    """Merge commits splice side-branch commits in with their ORIGINAL dates.

    This repo allows merge commits and has many. Without `--first-parent`,
    `rev-list --before` can return a just-arrived commit that is already older
    than the grace window, and the window stops protecting anything.
    """
    rev_list = re.search(r"BAR=\$\(git rev-list[^\n]*", guard_step["run"]).group(0)
    assert "--first-parent" in rev_list


def test_production_must_be_on_main_not_merely_descended_from_the_bar(guard_step: dict):
    """The false pass QC round 1 found.

    Forward ancestry alone asks only "is production at or ahead of the bar", so
    a commit deployed by hand from any pushed branch descending from the bar was
    certified OK with exit 0 -- and `fetch-depth: 0` puts every such branch in
    the runner's clone, so git resolves it happily.
    """
    run = guard_step["run"]
    assert 'git merge-base --is-ancestor "$DEPLOYED" "$MAIN_REF"' in run, (
        "the reverse ancestry gate is missing; an unmerged commit deployed by "
        "hand would be certified as current"
    )


def test_a_non_sha_build_value_is_refused(guard_step: dict):
    """`_build_sha()` returns the literal "unknown", never an empty string.

    So the likeliest misconfiguration (RAILWAY_GIT_COMMIT_SHA unset) arrives as
    a VALUE. An emptiness test alone is dead code, and bare `git` would resolve
    ref names like "main" or "HEAD" and certify a pass against them.
    """
    run = guard_step["run"]
    assert re.search(r"grep -Eq '\^\[0-9a-f\]\{7,40\}\$'", run), (
        "no hex object-name check; 'unknown', 'main' and 'HEAD' would all get "
        "past the build-SHA gate"
    )
    assert "RAILWAY_GIT_COMMIT_SHA" in run, (
        "the operator is not told which variable to fix"
    )


def test_the_runbook_pointer_resolves_to_a_real_heading(guard_step: dict):
    """The failure message sends the operator to ALERTING.md by name.

    A renamed heading turns the one instruction an operator gets mid-incident
    into a dead reference, and nothing else would notice.
    """
    pointer = re.search(r"'([^']+)' in ALERTING\.md", guard_step["run"])
    assert pointer, "the guard no longer names a section of ALERTING.md"
    heading = pointer.group(1)

    text = _ALERTING.read_text(encoding="utf-8")
    assert re.search(rf"^#+\s+{re.escape(heading)}\s*$", text, re.MULTILINE), (
        f"ALERTING.md has no heading {heading!r}"
    )


def test_the_runbook_sits_under_the_runbook_section():
    """An operator mid-incident reads the runbook half, not the architecture half."""
    lines = _ALERTING.read_text(encoding="utf-8").splitlines()
    runbook_h2 = next(i for i, ln in enumerate(lines) if ln.startswith("## Runbook"))
    entry = next(i for i, ln in enumerate(lines) if ln.strip() == "### Deploy drift detected")
    assert entry > runbook_h2

    later_h2 = [i for i, ln in enumerate(lines) if ln.startswith("## ") and i > runbook_h2]
    if later_h2:
        assert entry < later_h2[0], "the entry fell outside the Runbook section"

# ---------------------------------------------------------------------------
# What the assertions above could NOT see
# ---------------------------------------------------------------------------
# An independent QC round mutated this workflow eighteen ways and found the
# tests above green for eight of them. Two of those eight are SILENT failures --
# the guard keeps running, keeps reporting, and stops protecting anything:
#
#   * `exit 1` on the drift branch changed to `exit 0`. The guard still prints
#     `::error::DEPLOY DRIFT`, into a GREEN job. No failure, so no email, so no
#     signal at all. One character reintroduces the exact outage this whole
#     change exists to catch.
#   * `GRACE` widened from minutes to days. The guard then tolerates weeks of
#     drift and cheerfully reports OK.
#
# The other six fail loudly in production rather than passing silently, but they
# include INVERTING the reverse-ancestry gate -- the defect that got the first
# version of this guard a DO NOT SHIP. The old test matched the command as a
# substring, which survives its own negation intact.
#
# So these pin BEHAVIOUR OF THE BRANCHES, not the presence of strings.


def _if_block(run: str, gate: str, label: str) -> str:
    """The whole `if ! <gate>; then ... fi` block, and proof it is negated.

    Matching the command as a substring cannot tell a gate from its negation:
    deleting the `!` leaves the command untouched and flips the guard into
    certifying exactly what it was written to refuse.
    """
    lines = run.splitlines()
    for i, line in enumerate(lines):
        if re.search(gate, line):
            assert line.lstrip().startswith("if ! "), (
                f"the {label} gate is no longer negated: {line.strip()!r}. "
                f"Without the `!` it fires on success and passes on failure, "
                f"which is the inversion QC rejected in round 1."
            )
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "fi":
                    return "\n".join(lines[i:j + 1])
            raise AssertionError(f"the {label} gate's if-block is unterminated")
    raise AssertionError(f"no {label} gate found (looked for {gate!r})")


@pytest.mark.parametrize(
    "gate,label",
    [
        (r"grep -Eq '\^\[0-9a-f\]", "hex build-SHA"),
        (r"git cat-file -e", "commit-exists"),
        (r'git merge-base --is-ancestor "\$DEPLOYED" "\$MAIN_REF"', "on-main"),
    ],
)
def test_each_gate_is_negated_and_fails_the_job(guard_step: dict, gate, label):
    """Every refusal path must both be negated AND exit non-zero.

    A gate that runs and prints but exits 0 is worse than no gate: it reads as
    a considered check in the log while certifying the thing it names.
    """
    block = _if_block(guard_step["run"], gate, label)
    assert "exit 1" in block, (
        f"the {label} gate no longer exits non-zero, so it reports its own "
        f"failure into a passing job:\n{block}"
    )


def test_the_drift_branch_actually_fails_the_job(guard_step: dict):
    """The whole point, and nothing pinned it.

    This is the fall-through branch: no `if`, just the error and the exit. A
    mutation to `exit 0` left every other test in this file green while the
    guard printed DEPLOY DRIFT into a green job -- no failure email, no signal.
    """
    run = guard_step["run"]
    assert "::error::DEPLOY DRIFT" in run, (
        "the drift branch no longer emits an ::error:: annotation"
    )
    tail = run[run.index("::error::DEPLOY DRIFT"):]
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    assert lines[-1] == "exit 1", (
        "the drift branch does not end in `exit 1`, so detecting drift does "
        f"not FAIL the job and nobody is emailed. Tail was: {lines[-1]!r}"
    )


def test_the_grace_window_is_minutes_and_stays_small(guard_step: dict):
    """``GRACE`` bounds how long a legitimate deploy may take. It is NOT a
    tuning knob -- widening it widens the window in which real drift goes
    unreported -- and that invariant was stated only in a comment.

    Bounded rather than frozen: the observed deploy latency was ~4 minutes, so
    anything inside an hour is arguably a deploy still in flight. A value in
    days is not a slower deploy, it is the guard switched off.
    """
    grace = guard_step["env"]["GRACE"]
    m = re.fullmatch(r"(\d+) minutes? ago", grace)
    assert m, (
        f"GRACE is {grace!r}. It must be expressed in minutes: a window in "
        f"hours or days does not tolerate a slow deploy, it stops reporting "
        f"drift."
    )
    assert int(m.group(1)) <= 60, (
        f"GRACE is {grace!r}, over an hour. Deploy latency was measured at "
        f"~4 minutes; this is the window real drift hides in."
    )


def _github_slug(heading: str) -> str:
    """GitHub's anchor rule: lowercase, drop punctuation, spaces to hyphens."""
    s = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", s)


def test_every_in_document_link_in_the_runbook_resolves():
    """Both directions of the doc pointer, which nothing checked.

    The workflow names a heading and a test above confirms that heading exists.
    Nothing confirmed the reverse: ALERTING.md's own `](#...)` cross-links can
    be pointed at a heading that does not exist, or a heading can be renamed
    out from under them, and the suite stays green either way. A runbook whose
    links go nowhere is read exactly once, during an incident.
    """
    text = _ALERTING.read_text(encoding="utf-8")
    headings = {
        _github_slug(h) for h in re.findall(r"^#+\s+(.+?)\s*$", text, re.MULTILINE)
    }
    anchors = set(re.findall(r"\]\(#([\w-]+)\)", text))
    assert anchors, (
        "no in-document anchors found at all -- this guard is asserting over "
        "nothing, so the regex has probably drifted from the markdown"
    )
    missing = sorted(a for a in anchors if a not in headings)
    assert not missing, (
        f"ALERTING.md links to headings that do not exist: {missing}. "
        f"Either the link or the heading was renamed."
    )
