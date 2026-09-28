"""ColabFold cannot do PDB templates, so the option is refused for free.

Four of seven ColabFold standalone runs in the 2026-08-26 → 2026-09-27 window
failed, every one of them with ``use_templates: true`` in its inputs:
3a75ca7b, 90ec7966, 52eeaf55 (one user, who then switched to AF2 and
succeeded) and f42de6aa (2026-09-27).

``colabfold_batch --templates`` spawns ``hhsearch``. ``tools/colabfold/
Dockerfile.modal`` installs curl, git, python3-pip, python3-dev and
build-essential — no hhsuite — and bakes no PDB70/PDB100 database. The
sibling AF2 image installs hhsuite for exactly this reason and carries the
same runtime probe (``tools/af2/run_pipeline.py::_hhsearch_available``);
ColabFold never got either. All four runs surfaced as the parser's
``scores_missing`` — the empty-output, exit-0 arm — after the GPU was
billed; that the swallowed exception is specifically the missing binary is
inferred from AF2's fix (``0bb3bbf8``), not proven in this repo.

Two layers, both pinned below: the free refusal in ``validate`` (before
billing, which is the point) and the GPU-side downgrade for any payload that
does not come through ``validate`` — refolds, campaign chunks, a stored
``inputs`` replayed by a clone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import colabfold
from tools.colabfold import run_pipeline as colabfold_rp

_REPO = Path(__file__).resolve().parents[1]

_FASTA = ">design_1\n" + "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQ" * 2


def _standalone(**over) -> dict:
    form = {"preset": "standalone", "fasta_text": _FASTA, "num_recycles": "1"}
    form.update(over)
    return form


def _batch(**over) -> dict:
    form = {"preset": "batch", "sequences": _FASTA, "num_recycles": "1"}
    form.update(over)
    return form


class TestValidateRefusesBeforeBilling:
    @pytest.mark.parametrize("form", [_standalone, _batch], ids=["standalone", "batch"])
    @pytest.mark.parametrize("checked", ["on", "true", "1"])
    def test_a_truthy_templates_field_is_refused(self, form, checked):
        spec, err = colabfold.validate(form(use_templates=checked), {})
        assert spec is None
        assert err is not None
        assert "templates are not available" in err.lower()

    def test_the_refusal_names_the_cause_and_the_way_forward(self):
        _, err = colabfold.validate(_standalone(use_templates="on"), {})
        # Cause the user can act on, not a stack trace: WHY it cannot run,
        # and the one thing to change before resubmitting.
        assert "hhsearch" in err
        assert "PDB70" in err
        assert "resubmit" in err

    @pytest.mark.parametrize("form", [_standalone, _batch], ids=["standalone", "batch"])
    def test_the_normal_path_is_untouched_and_carries_templates_off(self, form):
        spec, err = colabfold.validate(form(), {})
        assert err is None, err
        assert spec["use_templates"] is False

    def test_a_falsy_field_is_not_refused(self):
        """An unchecked box posts nothing, but a clone or an API caller may
        send the key explicitly."""
        spec, err = colabfold.validate(_standalone(use_templates="off"), {})
        assert err is None, err
        assert spec["use_templates"] is False


class TestGpuSideDowngrade:
    def test_templates_are_dropped_when_hhsearch_is_absent(self, monkeypatch):
        monkeypatch.setattr(colabfold_rp, "_templates_available", lambda: False)
        assert colabfold_rp._effective_use_templates(True) is False

    def test_templates_survive_on_an_image_that_ships_hhsearch(self, monkeypatch):
        """The downgrade is conditional on the binary, not a hard-coded
        False — so installing hhsuite in the image is all it takes to make
        the flag live again."""
        monkeypatch.setattr(colabfold_rp, "_templates_available", lambda: True)
        assert colabfold_rp._effective_use_templates(True) is True

    def test_off_stays_off(self, monkeypatch):
        monkeypatch.setattr(colabfold_rp, "_templates_available", lambda: True)
        assert colabfold_rp._effective_use_templates(False) is False

    def test_the_probe_reads_path(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/hhsearch"
                            if name == "hhsearch" else None)
        assert colabfold_rp._templates_available() is True
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert colabfold_rp._templates_available() is False


class TestTheFormNoLongerOffersIt:
    def test_no_use_templates_control_on_the_colabfold_form(self):
        """Anti-regression on re-adding a control the image cannot honour.
        The AF2 form keeps its checkbox: that image ships hhsearch, and its
        own guard downgrades rather than crashes."""
        body = (_REPO / "templates/tools/colabfold_form.html").read_text(
            encoding="utf-8")
        assert 'name="use_templates"' not in body
        assert 'name="use_templates"' in (
            _REPO / "templates/tools/af2_form.html").read_text(encoding="utf-8")

    def test_the_image_still_ships_no_hhsuite(self):
        """The premise of both layers. If this fails because hhsuite was
        installed, the refusal above is the thing to revisit."""
        dockerfile = (_REPO / "tools/colabfold/Dockerfile.modal").read_text(
            encoding="utf-8")
        assert "hhsuite" not in dockerfile
