"""Every GPU image installs certifi at or above the web tier's floor.

requirements.txt only reaches the Railway web tier. Each Modal app builds its
own image, and every run_pipeline.py talks HTTPS through ``requests``, which
verifies against certifi's bundle. A floor raised in requirements.txt alone
leaves all nine containers on whatever bundle their last build resolved.
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


def test_all_nine_images_are_discovered():
    assert len(_IMAGE_FILES) == 9, [str(p) for p in _IMAGE_FILES]


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
