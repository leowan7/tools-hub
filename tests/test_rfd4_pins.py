"""The RFD4-Proteina image and seeder keep their pins in code, not in prose.

Everything here is offline and reads the two step-1 files. It cannot check that
a pin is CORRECT — that took a build and a GPU run (scope doc §5.4-§5.7) — only
that the shapes which made those results reproducible are still present:

  * no default for the accelerator, and no permissive arm in the case statement
    that selects the wheel set;
  * an immutable upstream commit rather than a branch;
  * a named per-file allow-list in the seeder rather than a whole-repo snapshot,
    which is what keeps the licence-excluded oracle runtimes off the Volume;
  * the flow checkpoint keeping its ``-ema.ckpt`` suffix, which upstream's
    ``rfproteina/utils/weights.py:20`` dispatches on.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DOCKERFILE = _ROOT / "tools" / "rfd4" / "Dockerfile.modal"
_SEEDER = _ROOT / "tools" / "rfd4" / "seed_volumes.py"

_UPSTREAM_SHA = "aaccdd2ad0720c180468df5f9cf037a713d403e5"
_HF_REVISION = "a06e2509c7033b010fc43614e9fd840303588502"


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _seeder() -> str:
    return _SEEDER.read_text(encoding="utf-8")


def test_accelerator_arg_has_no_default():
    """A default is what lets a rebuild pick a wheel set nothing has run.

    cu12 and cu13 are different products, not fallbacks: cu12 ships no
    bionemo-ir, so the BioIR inference backend is absent (scope doc §5.4).
    """
    args = re.findall(r"^ARG\s+RFD4_ACCELERATOR(.*)$", _dockerfile(), re.MULTILINE)
    assert args == [""], f"ARG RFD4_ACCELERATOR must carry no default, got {args!r}"


def test_accelerator_case_statement_refuses_anything_else():
    """The `*)` arm must exit, not resolve to a wheel set.

    An unset ARG expands to the empty string, which matches `*)` and nothing
    else, so this arm is also what makes the missing default loud.
    """
    text = _dockerfile()
    assert "cu13) set -- --extra all-cuda13 --torch-backend cu130 ;;" in text
    assert "cu12) set -- --extra all-cuda12 --torch-backend cu126 ;;" in text
    catchall = re.search(r"^\s*\*\)(.*)$", text, re.MULTILINE)
    assert catchall, "the accelerator case statement has no `*)` arm"
    assert "exit 2" in catchall.group(1), (
        f"the `*)` arm must exit, got {catchall.group(1)!r}"
    )


def test_upstream_commit_is_pinned_to_a_full_sha():
    m = re.search(r"^ARG\s+RFD4_SHA=([0-9a-f]+)$", _dockerfile(), re.MULTILINE)
    assert m, "ARG RFD4_SHA not found"
    assert m.group(1) == _UPSTREAM_SHA, (
        f"RFD4_SHA is {m.group(1)!r}; the built-and-run commit is {_UPSTREAM_SHA!r}. "
        "A bump is a build change and belongs in its own commit, with this value "
        "updated in the same diff."
    )
    # A 40-hex literal, never a branch: `main` re-resolves on every rebuild.
    assert len(m.group(1)) == 40


def test_the_image_builds_from_a_clone_and_copies_nothing():
    """No repo file reaches this image.

    Upstream's Dockerfile uses `COPY . .`; there is no RFD4 build context on
    Modal, so ours clones instead. A COPY appearing here would also put the
    image inside the scope of tests/test_deploy_trigger_covers_dockerfile_copies.py
    with nothing in deploy-modal.yml's trigger list covering it.
    """
    copies = [
        line
        for line in _dockerfile().splitlines()
        if re.match(r"^\s*COPY\s", line)
    ]
    assert not copies, copies
    assert 'git fetch --depth 1 origin "${RFD4_SHA}"' in _dockerfile()


def test_seeder_names_its_files_instead_of_snapshotting_the_repo():
    """`snapshot_download` would pull the licence-excluded oracle runtimes.

    ~71.4 GB of the pinned 82.57 GB revision is alphafold2/runtime/** and
    openfold3/runtime/**; the two checkpoints we seed are 2.59 GiB of it.
    (GB for the two upstream figures, which the scope doc reports in GB; GiB for
    ours, which is the two byte counts in ``_FILES`` added up.)
    """
    # Matched as a CALL: the seeder's own docstring names `snapshot_download` to
    # say it is not used, and a bare substring test reads that as the thing it
    # forbids.
    text = _seeder()
    assert "snapshot_download(" not in text
    assert "hf_hub_download(" in text


def test_seeder_pins_the_weights_revision():
    assert f'_HF_REVISION = "{_HF_REVISION}"' in _seeder()
    assert '_HF_REPO = "nvidia/RFD4-Proteina"' in _seeder()


def test_flow_checkpoint_keeps_its_ema_suffix_and_the_ae_takes_the_dated_name():
    """The asymmetry is load-bearing, in both directions.

    ``rfproteina/utils/weights.py:20`` dispatches on the flow file's
    ``-ema.ckpt`` suffix — a renamed flow checkpoint raises "has no SimpleEMA
    state, but inference requires EMA weights", which is how §5.7's first attempt
    failed. The autoencoder has no such rule and must sit on the literal path
    upstream's ``configs/inference.yaml:46`` reads.
    """
    text = _seeder()
    flow_dest = re.search(
        r'"flow_checkpoints/[^"]+",\s*\n\s*"(flow_checkpoints/[^"]+)"', text
    )
    assert flow_dest, "could not find the flow checkpoint's Volume path in _FILES"
    assert flow_dest.group(1).endswith("-ema.ckpt"), flow_dest.group(1)

    assert (
        '"ae_checkpoints/rfd4-proteina-ae-release-2026-10-19.ckpt"' in text
    ), "the autoencoder must land on the path configs/inference.yaml:46 reads"


def test_seeder_verifies_both_checkpoints_by_digest():
    """A size check alone would accept a different release of the same shape.

    The flow checkpoint and the autoencoder are a matched pair; upstream's
    release-checkpoints.json says mixing releases is unsupported, and §5.7
    records that a mismatch produces nonsense rather than an error.
    """
    text = _seeder()
    digests = re.findall(r'"([0-9a-f]{64})"', text)
    assert len(digests) == 2, digests
    assert "sha256" in text and "raise RuntimeError" in text
