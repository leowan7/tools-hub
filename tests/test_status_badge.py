"""Every status this app can persist must render as a TINTED badge.

Register item A39: ``status_badge`` tinted only the six ``tool_jobs`` statuses,
so all SIX campaign-only states fell through to a plain untinted pill. Six, not
the five A39 itself enumerated: that entry omitted campaign ``completed``,
which is a distinct status from ``tool_jobs`` ``succeeded`` and was untinted
for the same reason. Counted against migration 0034's CHECK rather than against
the register entry, which is the same reason this file drives off the migration
below. The
target page renders one badge per run, which makes it the surface where that
matters most: a run paused because the wallet ran dry looked exactly like a
healthy one.

The list of campaign statuses here is deliberately NOT
``shared.compute_campaigns.CAMPAIGN_STATUSES``. That constant is missing
``paused_insufficient_funds`` (register item A38) even though migration 0035
added the value to the DB CHECK and ``cron/tick_campaigns.py`` writes it, so a
test driven off the constant would assert coverage of a set that excludes the
single most important status to get right. Driving off the migration's real set
is what makes this test able to fail for the reason it exists.

Renders the macro through a bare Jinja environment rather than ``create_app()``:
the macro takes no app globals, and a test that boots the app would need
``isolate_supabase`` to avoid the production database.
"""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

# tool_jobs.status, migration 0005.
_JOB_STATUSES = (
    "pending", "running", "succeeded", "failed", "timeout", "cancelled",
)

# compute_campaigns.status: the CHECK in migration 0034, widened by 0035.
_CAMPAIGN_STATUSES = (
    "draft", "funded", "running", "completing",
    "completed", "completed_with_failures", "failed", "cancelled",
    "paused_insufficient_funds",
)


def _render(status: str) -> str:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    tmpl = env.from_string(
        '{% from "components/status_badge.html" import status_badge %}'
        "{{ status_badge(status) }}"
    )
    return tmpl.render(status=status)


@pytest.mark.parametrize("status", sorted(set(_JOB_STATUSES + _CAMPAIGN_STATUSES)))
def test_every_persistable_status_is_tinted(status):
    html = _render(status)
    assert "panel-badge" in html
    assert "style=" in html and "background:" in html, (
        f"{status!r} renders an untinted pill, so it is visually identical to "
        f"every other untinted status on the target page"
    )


def test_paused_for_funds_is_not_tinted_like_a_healthy_run():
    """The specific confusion A39 describes: a wallet-paused run must not look
    like a running or completed one."""
    paused = _render("paused_insufficient_funds")
    for healthy in ("running", "completing", "completed", "succeeded"):
        assert paused != _render(healthy)

    # Amber, shared with completed_with_failures and timeout: needs attention,
    # but recoverable by topping up. Red stays reserved for failed.
    assert "#fbbf24" in paused


def test_multiword_statuses_render_readable_labels():
    """A raw snake_case status in a pill reads as a database value, not a
    status. Only the multi-word ones are relabelled; the rest stay verbatim."""
    assert "paused, low balance" in _render("paused_insufficient_funds")
    assert "paused_insufficient_funds" not in _render("paused_insufficient_funds")
    assert "completed with failures" in _render("completed_with_failures")
    assert ">running<" in _render("running").replace(" ", "").replace("\n", "")


def test_unknown_status_degrades_to_a_plain_pill_and_is_not_dropped():
    """Forward-compat: a status added to the DB before this map still shows."""
    html = _render("some_future_state")
    assert "panel-badge" in html
    assert "some_future_state" in html
