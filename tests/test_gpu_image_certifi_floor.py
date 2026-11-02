"""Every GPU image installs certifi at or above the web tier's floor.

requirements.txt only reaches the Railway web tier. Each Modal app builds its
own image, and every run_pipeline.py talks HTTPS through ``requests``, which
verifies against certifi's bundle. A floor raised in requirements.txt alone
leaves every container on whatever bundle its last build resolved.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_IMAGE_FILES = sorted((_ROOT / "tools").glob("*/Dockerfile.modal")) + [
    _ROOT / "tools" / "esmfold2_design" / "modal_app.py"
]
_PIN_RE = re.compile(r"certifi==([0-9][0-9.]*)")


def _version(s: str) -> tuple[int, ...]:
    return tuple(int(p) for p in s.split("."))


def _floor() -> tuple[int, ...]:
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    m = re.search(r"^certifi>=([0-9.]+)", text, re.MULTILINE)
    assert m, "requirements.txt no longer sets a certifi floor"
    return _version(m.group(1))


def test_every_image_is_discovered():
    # Ten: the eight tools/*/Dockerfile.modal files, esmfold2_design's
    # modal_app.py (no Dockerfile — it builds via modal.Image.micromamba), and
    # tools/rfd4/Dockerfile.modal, which is an image without a deployed app yet
    # (docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md §8 step 1). The floor binds
    # on an image, not on a deploy, so rfd4 is in scope from the moment the
    # Dockerfile exists.
    assert len(_IMAGE_FILES) == 10, [str(p) for p in _IMAGE_FILES]


@pytest.mark.parametrize("path", _IMAGE_FILES, ids=lambda p: p.parent.name)
def test_image_pins_certifi_at_or_above_the_floor(path):
    pins = [
        m.group(1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
        for m in _PIN_RE.finditer(line)
    ]
    assert pins, f"{path.relative_to(_ROOT)} installs no certifi==<version>"
    low = [p for p in pins if _version(p) < _floor()]
    assert not low, f"{path.relative_to(_ROOT)} pins certifi {low}, below requirements.txt"
