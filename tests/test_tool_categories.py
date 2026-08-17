"""Every GPU adapter must carry a real workflow-stage category.

Proteina-Complexa and OpenDDE both shipped without an entry in
``_TOOL_CATEGORIES``, so they silently fell into the homepage's "Other"
bucket. This guards the next adapter from doing the same.
"""

# ``tools.base._REGISTRY`` is populated only by the ``import tools.<slug>``
# side effects in app.py, so importing app is what makes the adapters
# visible here. Without it this test iterates an empty registry and
# passes vacuously -- green while the bug ships. The explicit count
# assertion below keeps that failure mode from coming back silently.
import app  # noqa: F401
from tools import base as tool_base

from shared.tools_catalog import _TOOL_CATEGORIES


def test_every_adapter_has_a_category():
    adapters = tool_base.all_adapters()
    assert adapters, (
        "no tool adapters registered -- this test cannot prove anything; "
        "app must be imported so tools.base._REGISTRY is populated"
    )
    missing = sorted(a.slug for a in adapters if a.slug not in _TOOL_CATEGORIES)
    assert not missing, (
        f"adapters with no _TOOL_CATEGORIES entry (they render under "
        f'"Other" on the homepage): {missing}'
    )
