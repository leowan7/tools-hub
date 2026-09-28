"""The delegated anchor listener in static/js/track.js.

The result-page exit events (``result_download``, ``outbound_click``) are
matched on the href, with no attribute in any template to grep for, so the
only artifact that can be checked is the shipped script itself. It runs under
node against a stubbed DOM (``tests/js/track_anchor_harness.cjs``), the same
way tests/test_af2_download_refusals.py drives scout-download.js, and the
assertions below are on the event types it posted, not on its source text.

SKIPS where node is off PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from shared.events import USER_EVENT_TYPES

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "static" / "js" / "track.js"
HARNESS = REPO_ROOT / "tests" / "js" / "track_anchor_harness.cjs"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)


@pytest.fixture(scope="module")
def results() -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS), str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, "harness failed:\n" + proc.stderr
    return json.loads(proc.stdout)


@needs_node
@pytest.mark.parametrize(
    "case",
    ["csv", "fasta", "zip", "pdb", "npz", "api_pdb", "inline_pdb"],
)
def test_download_links_emit_result_download(results: dict, case: str) -> None:
    assert results[case] == ["result_download"], results


@needs_node
@pytest.mark.parametrize("case", ["outbound", "outbound_www"])
def test_ranomics_links_emit_outbound_click(results: dict, case: str) -> None:
    assert results[case] == ["outbound_click"], results


@needs_node
@pytest.mark.parametrize("case", ["internal", "scale_up_form", "not_an_anchor"])
def test_ordinary_clicks_emit_nothing(results: dict, case: str) -> None:
    assert results[case] == [], results


def test_both_event_types_are_allowlisted() -> None:
    # shared/events.py::log_event stamps the type straight into the row; the
    # allowlist is what documents which strings the code emits.
    assert {"result_download", "outbound_click"} <= USER_EVENT_TYPES
