"""Tests for rfantibody CDR-length validation (gap 3).

``cdr_lengths`` used to be forwarded verbatim to the GPU pipeline. A
malformed value (``H3:abc``) or an out-of-envelope length crashed
RFdiffusion's contig builder mid-run, 30-60 min in. ``validate()`` now
rejects them at submit with an actionable message.

Pure-function tests: no Flask app, no Supabase, no Modal.
"""
from __future__ import annotations

import pytest

from tools.rfantibody import _validate_cdr_lengths, validate


# ---------------------------------------------------------------------------
# _validate_cdr_lengths: valid specs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spec",
    [
        "H1:8,H2:7,H3:10-16",   # the default
        "H1:8, H2:7, H3:12",    # spaces + single H3 length
        "h1:8,h2:7,h3:10-16",   # case-insensitive keys
        "H3:5-20",              # H3 at both bounds, subset of CDRs
        "H1:1,H2:20",           # H1/H2 at their bounds
    ],
)
def test_valid_cdr_specs_pass(spec):
    canon, err = _validate_cdr_lengths(spec)
    assert err is None
    assert canon  # non-empty canonical form


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("H1:8,H2:7,H3:10-16", "H1:8,H2:7,H3:10-16"),  # already canonical
        ("h1:8,h2:7,h3:10-16", "H1:8,H2:7,H3:10-16"),  # uppercased
        ("H1:8, H2:7, H3:12", "H1:8,H2:7,H3:12"),      # spaces stripped
        ("H3: 10 - 16", "H3:10-16"),                   # range spaces stripped
    ],
)
def test_cdr_canonicalization(spec, expected):
    """The forwarded spec is cleaned so off-format whitespace/case never
    reaches the GPU."""
    canon, err = _validate_cdr_lengths(spec)
    assert err is None
    assert canon == expected


def test_empty_spec_treated_as_default():
    # The adapter never passes empty (it defaults), but the validator
    # must not reject a blank string.
    assert _validate_cdr_lengths("")[1] is None
    assert _validate_cdr_lengths("   ")[1] is None


# ---------------------------------------------------------------------------
# _validate_cdr_lengths: rejections (each must name the offending CDR/value)
# ---------------------------------------------------------------------------

def test_non_numeric_length_rejected():
    _, err = _validate_cdr_lengths("H3:abc")
    assert err is not None
    assert "H3" in err
    assert "abc" in err


def test_plus_prefixed_number_rejected():
    # int("+8") == 8 would pass a lenient parse and forward "+8" to the GPU.
    _, err = _validate_cdr_lengths("H3:+8")
    assert err is not None
    assert "H3" in err


def test_underscore_number_rejected():
    # int("1_0") == 10 under Python's lenient int(); reject it.
    _, err = _validate_cdr_lengths("H3:1_0")
    assert err is not None


def test_internal_space_in_number_rejected():
    _, err = _validate_cdr_lengths("H3:1 0")
    assert err is not None


def test_h3_too_long_rejected():
    _, err = _validate_cdr_lengths("H1:8,H2:7,H3:25")
    assert err is not None
    assert "H3" in err
    assert "5" in err and "20" in err
    # Copy rule: ranges spelled out, no connector hyphen in prose.
    assert "5-20" not in err


def test_h3_too_short_rejected():
    _, err = _validate_cdr_lengths("H3:3")
    assert err is not None
    assert "H3" in err


def test_range_upper_within_bounds_but_over_ceiling_rejected():
    _, err = _validate_cdr_lengths("H3:10-30")
    assert err is not None
    assert "H3" in err


def test_backwards_range_rejected():
    _, err = _validate_cdr_lengths("H3:16-10")
    assert err is not None
    assert "backwards" in err.lower()


def test_unknown_cdr_key_rejected():
    _, err = _validate_cdr_lengths("H4:8")
    assert err is not None
    assert "H4" in err


def test_light_chain_cdr_rejected_vhh_only():
    # VHH = heavy chain only; L-chain CDRs are not designable here.
    _, err = _validate_cdr_lengths("L1:8")
    assert err is not None
    assert "L1" in err


def test_missing_colon_rejected():
    _, err = _validate_cdr_lengths("H3")
    assert err is not None


def test_missing_upper_bound_rejected():
    _, err = _validate_cdr_lengths("H3:10-")
    assert err is not None
    assert "upper bound" in err.lower()


def test_duplicate_cdr_rejected():
    _, err = _validate_cdr_lengths("H3:10,H3:12")
    assert err is not None
    assert "more than once" in err.lower()


# ---------------------------------------------------------------------------
# End-to-end through validate(): a bad cdr_lengths blocks the submit
# ---------------------------------------------------------------------------

def _base_form(**over):
    form = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "50,52",
        "num_designs": "4",
        "cdr_lengths": "H1:8,H2:7,H3:10-16",
    }
    form.update(over)
    return form


def test_validate_accepts_good_cdr():
    inputs, err = validate(_base_form(), {})
    assert err is None
    assert inputs is not None
    assert inputs["cdr_lengths"] == "H1:8,H2:7,H3:10-16"


def test_validate_rejects_bad_cdr():
    inputs, err = validate(_base_form(cdr_lengths="H3:abc"), {})
    assert inputs is None
    assert err is not None
    assert "H3" in err
