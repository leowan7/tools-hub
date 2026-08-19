"""Interface detection has no silent 500x fallback any more.

``detect_interfaces`` used to carry a pure-Python brute-force alternative to
``scipy.spatial.cKDTree``, selected by an ``except ImportError`` inside the
per-partner-chain loop. It was an O(n_target_atoms x n_partner_atoms) double
loop: measured at 323 CPU-s on a 6 MB structure against 0.64 on the cKDTree
path, a ~500x cliff, inside the anonymous compute slot, triggered by an import
failure rather than by anything about the input.

``scipy>=1.11`` is in requirements.txt, so in production that branch could only
ever have been a silent catastrophe, never a feature. It matters because it is
an unbounded slow path with no upper limit on how long one request can burn —
survivable today only because gunicorn's sync worker gets killed at ``timeout``.

The requirement is not "it is fast". It is that a missing scipy is IMPOSSIBLE
to hit silently: the feature degrades to "no interfaces" and says so at error
level.

    pytest tests/test_scout_interfaces_scipy.py -v
"""

from __future__ import annotations

import builtins
import logging

import pytest

from scout.interfaces import detect_interfaces


def _two_chain_pdb(tmp_path, n_per_chain: int = 6):
    """Two chains close enough to be in contact, so the real path returns
    something and a broken guard cannot pass by returning [] for the wrong
    reason."""
    lines = []
    serial = 1
    for chain, offset in (("A", 0.0), ("B", 3.0)):
        for i in range(1, n_per_chain + 1):
            lines.append(
                f"ATOM  {serial:5d}  CA  ALA {chain}{i:4d}    "
                f"{i * 3.0:8.3f}{offset:8.3f}{0.0:8.3f}  1.00 20.00           C"
            )
            serial += 1
    lines.append("END")
    path = tmp_path / "pair.pdb"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def no_scipy(monkeypatch):
    """Make ``import scipy...`` fail, and nothing else."""
    real_import = builtins.__import__

    def _fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "scipy" or name.startswith("scipy."):
            raise ImportError("no scipy (simulated)")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake)
    monkeypatch.delitem(__import__("sys").modules, "scipy.spatial", raising=False)
    monkeypatch.delitem(__import__("sys").modules, "scipy", raising=False)


class TestScipyIsRequiredNotOptional:
    """These are not "scipy is absent" tests, they are "there is NOTHING
    behind scipy" tests, and that is what makes them a guard: any fallback
    reinstated behind the import returns answers where these expect ``[]``,
    so ``test_a_missing_scipy_degrades_instead_of_grinding`` goes red.

    (An earlier draft reached for an AST scan of the source instead. It
    flagged the function's own legitimate chain/residue/atom loops and was red
    against correct code — worse than no guard at all.)
    """

    def test_the_happy_path_still_finds_the_interface(self, tmp_path):
        """Guard the guard: if this returned [] anyway, the absence tests
        below would prove nothing, because both cases would look identical.

        Skipped where scipy is missing, the same way ``requires_freesasa``
        handles the other C dependency this repo does not install on Windows
        dev boxes. Note that scipy being absent from a venv while sitting in
        requirements.txt is exactly the condition that made the deleted
        fallback a live 323-CPU-s path rather than a theoretical one.
        """
        pytest.importorskip("scipy", reason="scipy is not installed here")
        found = detect_interfaces(_two_chain_pdb(tmp_path), "A")
        assert found, "the two chains are 3 A apart and must be in contact"

    def test_a_missing_scipy_degrades_instead_of_grinding(
        self, tmp_path, no_scipy
    ):
        assert detect_interfaces(_two_chain_pdb(tmp_path), "A") == []

    def test_a_missing_scipy_is_logged_at_error_level(
        self, tmp_path, no_scipy, caplog
    ):
        """Silence is the actual defect. A degraded feature that says nothing
        is how a fallback nobody chose ran in production for months."""
        with caplog.at_level(logging.ERROR, logger="scout.interfaces"):
            detect_interfaces(_two_chain_pdb(tmp_path), "A")
        assert any(
            rec.levelno >= logging.ERROR for rec in caplog.records
        ), "a missing hard requirement was swallowed without an error log"

