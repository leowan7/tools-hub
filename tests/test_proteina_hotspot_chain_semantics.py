"""Proteina hotspots must mean the same thing to the gate and to the model.

Proteina carried hotspots in two representations that disagreed:

    hotspot_spec     ["A600"]   what upstream string-matches on, literally,
                                as f"{chain_id}{res_id}"
    hotspot_residues [600]      bare ints -- and this is what EVERY pre-money
                                check read

The bare form cannot express WHICH CHAIN, so the route's range check answered a
different question from the one that decided the run. On a target with chain A
234-444 and chain B 500-700, a bare ``600`` (which exists only on B) was
silently promoted to ``A600`` and the route PASSED -- on a residue that does
not exist.

The case that actually costs money is quieter. An IgG1 Fc is a homodimer:
chains A and B are BOTH numbered 234-444. A bare ``241`` was promoted to
``A241``, which is a genuinely real residue. Route passes, preflight passes,
the container's own ``normalize_hotspots`` guard never fires (it only sees the
already-promoted token), the run completes, and chain B is never steered
toward. Nothing at any layer could detect it. That is silent wrong-protomer
design, delivered as a successful run.

These tests pin the two halves of the fix:

  1. A bare hotspot is REFUSED when the run names more than one chain. The
     container already implements exactly this rule (see the "A BARE INTEGER IS
     REFUSED WHEN THE TARGET HAS MORE THAN ONE CHAIN" paragraph in
     run_pipeline.normalize_hotspots); the adapter's silent promotion is what
     made that guard unreachable. The refusal moves to where the operator can
     read it.

  2. The chain-qualified token is what the checks range-check. ``hotspot_spec``
     stops being a representation only the container sees.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.targets import DesignTarget
from tools import proteina as adapter

_REPO = Path(__file__).resolve().parents[1]


def _form(**over):
    form = {"preset": "protein_binder", "_has_custom_target": "1"}
    form.update(over)
    return form


def _target(chains):
    """A DesignTarget whose chain_summary is ``[(chain, lo, hi), ...]``."""
    return DesignTarget(
        id=str(uuid.uuid4()),
        user_id="u-1",
        chain_summary={"chains": [
            {"chain_id": cid, "standard_residue_count": hi - lo + 1,
             "hetatm_resnames": [], "water_count": 0,
             "min_resnum": lo, "max_resnum": hi}
            for cid, lo, hi in chains
        ]},
    )


# ---------------------------------------------------------------------------
# Decision 1 -- a bare hotspot is ambiguous on a multi-chain run, so refuse it
# ---------------------------------------------------------------------------


def test_a_bare_hotspot_is_refused_when_the_contig_names_two_chains():
    inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700", hotspot_residues="600"), {})
    assert inp is None
    assert err is not None
    assert "600" in err and "chain prefix" in err


def test_the_refusal_names_the_chains_and_shows_both_prefixed_forms():
    """The message has to be actionable without leaving the page: the operator
    typed a number, and the only way to fix it is to know which chains are on
    offer and what the corrected token looks like."""
    _inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700", hotspot_residues="241"), {})
    assert err == (
        'Hotspot "241" needs a chain prefix — this run targets chains A and '
        'B, so write A241 or B241.'
    )


def test_the_homodimer_case_is_refused_even_though_the_residue_is_real():
    """THE ONE THAT MATTERS. Both Fc protomers are numbered 234-444, so the
    promoted ``A241`` resolves against a real atom and every downstream check
    -- route range check, preflight, the container's missing_hotspots guard --
    passes. There is no later layer that can catch this, which is why the
    refusal has to happen here."""
    inp, err = adapter.validate(
        _form(target_input="A234-444,B234-444", hotspot_residues="241"), {})
    assert inp is None, "a bare hotspot on a homodimer was accepted"
    assert "chain prefix" in err


def test_a_bare_hotspot_is_refused_when_target_chain_names_two_chains():
    """The contig is not the only way a run names two chains. With no contig at
    all, ``target_chain`` carries them -- and ``_parse_hotspots`` used to be
    handed ``contig_chains`` (empty here) and never saw the second chain."""
    inp, err = adapter.validate(
        _form(target_chain="A B", hotspot_residues="264"), {})
    assert inp is None
    assert "chain prefix" in err


def test_an_explicitly_named_chain_is_accepted_without_a_contig():
    """The other half of the same root cause, and a FALSE REJECTION rather than
    a false pass: with ``target_chain="A B"`` and no contig, ``B264`` -- the
    operator naming the chain, exactly as instructed -- was refused as "not one
    of this run's target chains (A)"."""
    inp, err = adapter.validate(
        _form(target_chain="A B", hotspot_residues="B264"), {})
    assert err is None, err
    assert inp["hotspot_spec"] == ["B264"]


def test_a_single_chain_run_still_promotes_a_bare_hotspot():
    """Load-bearing. _SHARED_LAUNCH_FIELDS feeds ONE hotspot field to every
    selected tool, so bare ints must keep working wherever they are still
    unambiguous."""
    inp, err = adapter.validate(
        _form(target_input="A234-444", hotspot_residues="241 300"), {})
    assert err is None, err
    assert inp["hotspot_spec"] == ["A241", "A300"]


def test_a_single_chain_run_with_no_contig_still_promotes():
    inp, err = adapter.validate(
        _form(target_chain="A", hotspot_residues="241"), {})
    assert err is None, err
    assert inp["hotspot_spec"] == ["A241"]


def test_prefixed_hotspots_on_a_multi_chain_run_are_accepted():
    inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700",
              hotspot_residues="A241 B600"), {})
    assert err is None, err
    assert inp["hotspot_spec"] == ["A241", "B600"]


def test_a_bare_hotspot_mixed_with_a_prefixed_one_is_still_refused():
    """Refusing only when EVERY token is bare would let the ambiguous one ride
    along beside an unambiguous one."""
    inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700",
              hotspot_residues="A241 600"), {})
    assert inp is None
    assert '"600"' in err


# ---------------------------------------------------------------------------
# Decision 2 -- the chain-qualified token is what the checks check
# ---------------------------------------------------------------------------


def test_hotspot_residues_is_chain_qualified_on_a_multi_chain_run():
    """This is the field every pre-money check reads. Emitting the bare number
    here is what made the route answer a different question from the one the
    run asked."""
    inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700",
              hotspot_residues="A241 B600"), {})
    assert err is None, err
    assert inp["hotspot_residues"] == ["A241", "B600"]


def test_hotspot_residues_stays_bare_ints_on_a_single_chain_all_bare_run():
    """Byte-identical to the payload submitted before this change, and the same
    rule tools/base.py::parse_hotspot_residues already applies for
    rfdiffusion/bindcraft/pxdesign/boltzgen: one chain plus all-bare tokens
    emits plain ints, anything else emits chain-prefixed strings."""
    inp, err = adapter.validate(
        _form(target_chain="A", hotspot_residues="42,88"), {})
    assert err is None, err
    assert inp["hotspot_residues"] == [42, 88]
    assert inp["hotspot_spec"] == ["A42", "A88"]


def test_a_single_chain_run_that_names_its_chain_keeps_the_prefix():
    """Mirrors parse_hotspot_residues' ``all_bare`` half: an operator who typed
    the chain gets it back, on one chain as on two."""
    inp, err = adapter.validate(
        _form(target_chain="A", hotspot_residues="A42"), {})
    assert err is None, err
    assert inp["hotspot_residues"] == ["A42"]


# ---------------------------------------------------------------------------
# The whole point: the gate and the model now answer the SAME question
# ---------------------------------------------------------------------------


def _resolves(spec, chains):
    """What upstream would find, matched the way upstream matches it."""
    real = {f"{cid}{n}" for cid, lo, hi in chains for n in range(lo, hi + 1)}
    return [t for t in spec if t not in real]


@pytest.mark.parametrize("typed", ["A600", "600"])
def test_the_route_no_longer_passes_a_hotspot_that_does_not_exist(typed):
    """Probe #23, as a test. Chain A is 234-444 and chain B is 500-700, so
    residue 600 exists on B and nowhere else. ``A600`` is a typo for ``B600``
    and ``600`` is the ambiguous form of it; both used to reach
    ``hotspot_error`` as the bare int 600, which is in range on B, so the route
    funded a run aimed at nothing."""
    chains = [("A", 234, 444), ("B", 500, 700)]
    inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700", hotspot_residues=typed), {})

    if inp is None:
        # Refused at the adapter (the bare form). That is a correct answer.
        assert "chain prefix" in err
        return

    route = _target(chains).hotspot_error(
        inp["target_chain"], inp["hotspot_residues"])
    assert _resolves(inp["hotspot_spec"], chains) == ["A600"], (
        "precondition: A600 must be the token that resolves to nothing")
    assert route is not None, (
        "the route passed a hotspot that addresses no residue in the structure")


def test_a_real_prefixed_hotspot_still_passes_the_route():
    """The complement: the fix must not start blocking correct work."""
    chains = [("A", 234, 444), ("B", 500, 700)]
    inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700",
              hotspot_residues="A241 B600"), {})
    assert err is None, err
    assert _resolves(inp["hotspot_spec"], chains) == []
    assert _target(chains).hotspot_error(
        inp["target_chain"], inp["hotspot_residues"]) is None


def test_the_homodimer_wrong_protomer_hotspot_is_caught_end_to_end():
    """A241 is real on an Fc homodimer, so the route CANNOT catch it -- and
    should not, because designing against one protomer is legitimate work. What
    must not happen is the operator getting there without saying so."""
    chains = [("A", 234, 444), ("B", 234, 444)]
    typed, err = adapter.validate(
        _form(target_input="A234-444,B234-444", hotspot_residues="A241"), {})
    assert err is None, err
    assert _target(chains).hotspot_error(
        typed["target_chain"], typed["hotspot_residues"]) is None

    promoted, err = adapter.validate(
        _form(target_input="A234-444,B234-444", hotspot_residues="241"), {})
    assert promoted is None, (
        "the ambiguous form still produces the same payload as the deliberate "
        "one, so nothing downstream can tell them apart")


# ---------------------------------------------------------------------------
# Persistence -- the new column is ADDITIVE
# ---------------------------------------------------------------------------


def _migration_sql():
    hits = sorted(_REPO.glob("supabase/migrations/*_design_targets_hotspot_spec.sql"))
    assert hits, "no hotspot_spec migration found"
    assert len(hits) == 1, f"more than one hotspot_spec migration: {hits}"
    return hits[0], hits[0].read_text(encoding="utf-8")


def test_the_migration_follows_the_existing_numbering_convention():
    path, _sql = _migration_sql()
    assert re.match(r"^\d{4}_[a-z0-9_]+\.sql$", path.name), path.name
    numbers = sorted(
        int(p.name[:4]) for p in (_REPO / "supabase/migrations").glob("*.sql")
    )
    assert int(path.name[:4]) == numbers[-1], (
        "the new migration must be the highest-numbered one")
    assert len(numbers) == len(set(numbers)), "duplicate migration number"


def test_the_migration_adds_a_nullable_text_array_and_nothing_else():
    """Additive means additive: no default, no backfill, no NOT NULL, and the
    integer[] column every existing reader depends on is untouched."""
    _path, sql = _migration_sql()
    body = re.sub(r"--[^\n]*", "", sql)  # strip comments before asserting
    assert re.search(
        r"add\s+column\s+if\s+not\s+exists\s+hotspot_spec\s+text\[\]",
        body, re.I,
    ), body
    assert "not null" not in body.lower()
    assert not re.search(r"\bdefault\b", body, re.I), "no default"
    assert not re.search(r"\bupdate\b|\binsert\b", body, re.I), "no backfill"
    assert not re.search(r"drop\s+column|alter\s+column", body, re.I)
    assert "hotspot_residues" not in body, (
        "the existing integer[] column must not be touched")


def test_the_existing_column_is_still_integer_array_in_0039():
    """Guards the direction of the change: 0039 keeps its integer[] so old
    readers keep reading what they always read."""
    sql = (_REPO / "supabase/migrations/0039_design_targets.sql").read_text(
        encoding="utf-8")
    assert re.search(r"hotspot_residues\s+integer\[\]", sql)


# ---------------------------------------------------------------------------
# Defensive -- blueprints/jobs.py must not raise on a chain-prefixed hotspot
# ---------------------------------------------------------------------------


def test_the_refold_hotspot_coercion_survives_chain_prefixed_tokens():
    """``int("A241")`` raises ValueError. Proteina is not in SOURCE_TOOLS so
    this path cannot see a proteina job -- but rfdiffusion, bindcraft, pxdesign
    and boltzgen ALL are, and tools/base.py::parse_hotspot_residues already
    emits ``["A296", "B264"]`` for every one of them on a multi-chain target.
    The comment claiming SOURCE_TOOLS "all persist hotspot_residues as
    list[int]" is false today, so this is a live crash, not a hypothetical."""
    from blueprints.jobs import _refold_hotspot_ints

    assert _refold_hotspot_ints(["A296", "B264"]) == [296, 264]
    assert _refold_hotspot_ints([296, 264]) == [296, 264]
    assert _refold_hotspot_ints("A296, B264") == [296, 264]
    assert _refold_hotspot_ints("296,264") == [296, 264]
    assert _refold_hotspot_ints([]) == []
    assert _refold_hotspot_ints(None) == []
    # Junk is dropped, not raised on: this runs while a refold job is being
    # spawned, and a ValueError there is a 500 on a button click.
    assert _refold_hotspot_ints(["A296", "zzz", None, "  "]) == [296]


# ---------------------------------------------------------------------------
# Decision 3 -- the plain-text fields have to state the rule
# ---------------------------------------------------------------------------


def _template(name):
    return (_REPO / "templates" / name).read_text(encoding="utf-8")


# Every surface that describes proteina's hotspot field. The first two are the
# plain-text fields Decision 3 names; the third is the atomic form, whose
# picker is already chain-prefixed but whose PROSE made the same false promise.
_HOTSPOT_COPY_TEMPLATES = [
    "runs/new.html",
    "targets/launch.html",
    "tools/proteina_form.html",
]

# Phrases that promise unconditional promotion. Each was true before a bare
# token became a refusal on a multi-chain run, and each is false now.
_SILENT_PROMOTION_PROMISES = [
    "Plain numbers use the target chain",
    "Plain numbers apply to the target chain",
    "plain numbers\n                use the target chain",
    "use the target chain, or prefix the chain",
]


@pytest.mark.parametrize("name", _HOTSPOT_COPY_TEMPLATES)
def test_the_hotspot_copy_does_not_promise_silent_promotion(name):
    """The promise the fix invalidates. Leaving it is how a false comment gets
    shipped by the change that made it false."""
    body = " ".join(_template(name).split())  # normalise wrapped prose
    for promise in _SILENT_PROMOTION_PROMISES:
        flat = " ".join(promise.split())
        assert flat not in body, f"{name} still promises: {flat}"


@pytest.mark.parametrize("name", _HOTSPOT_COPY_TEMPLATES)
def test_the_hotspot_copy_states_when_a_prefix_is_required(name):
    """The plain-text fields have no picker, so this copy is the only place the
    rule can be read before the form is submitted."""
    body = " ".join(_template(name).split()).lower()
    assert "more than one chain" in body, (
        f"{name} never says a prefix is required on a multi-chain run")


def test_the_proteina_form_already_drives_a_chain_prefixed_picker():
    """Verified, not duplicated: the atomic form's picker already emits chain-
    prefixed tokens, so it needs no new control -- only honest copy."""
    body = _template("tools/proteina_form.html")
    assert "chainPrefixed: true" in body
