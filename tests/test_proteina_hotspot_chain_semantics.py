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

Half 2 shipped on main as PR #127, by a different mechanism than this branch
first used. ``shared.pdb_preflight.shipped_hotspots`` prefers ``hotspot_spec``
and every money gate calls it, so ``hotspot_residues`` stays the bare stripped
copy -- lossy, and read by nothing that spends money. The branch's earlier
approach (chain-qualifying ``hotspot_residues`` itself) was superseded and
removed; what remains here pins the guarantee at its new location. Half 1 is
this branch's alone: main still promotes a bare hotspot onto the first chain.
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
#
# The branch first did this by putting the chain-qualified tokens INTO
# `hotspot_residues`. PR #127 landed the same guarantee on main by a different
# mechanism: `shared.pdb_preflight.shipped_hotspots` prefers `hotspot_spec`,
# and all four money gates route through it (blueprints/campaigns.py,
# blueprints/targets.py, blueprints/tools.py x2). So `hotspot_residues` goes
# back to being the bare stripped copy -- LOSSY, and deliberately not the
# thing anything judges. These tests pin the guarantee at its new location.
# ---------------------------------------------------------------------------


def test_the_token_the_gate_judges_is_the_token_the_payload_ships():
    """Decision 2, at its post-#127 location. `hotspot_residues` is the lossy
    copy again, so the assertion that matters is on `shipped_hotspots` -- what
    the four paid gates actually call -- not on the bare field."""
    from shared.pdb_preflight import shipped_hotspots

    inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700",
              hotspot_residues="A241 B600"), {})
    assert err is None, err
    assert inp["hotspot_spec"] == ["A241", "B600"]
    # The bare copy really is lossy, so a gate reading IT could not get this
    # right by accident -- which is the precondition that makes the next
    # assertion a measurement.
    assert inp["hotspot_residues"] == [241, 600]
    assert shipped_hotspots(inp) == ["A241", "B600"]
    assert shipped_hotspots(inp) == adapter.build_payload(
        inp, "https://example.invalid/t.pdb")["hotspot_spec"]


def test_reading_the_lossy_copy_would_fund_a_run_the_spec_refuses():
    """WHY it must be `shipped_hotspots` and not the bare field, in the
    direction that costs money rather than the one that only annoys.

    Chain A spans 1-700 and chain B only 500-539, so B600 is out of range on
    the chain it names but its stripped form 600 is in range on the FIRST named
    chain -- which is how `hotspot_error` reads an unprefixed token. A gate on
    the bare copy therefore FUNDS a run whose one steering token addresses no
    atom; upstream drops an unmatched hotspot to an all-zero mask silently, so
    that bills in full and delivers an unsteered run.
    """
    from shared.pdb_preflight import shipped_hotspots

    chains = [("A", 1, 700), ("B", 500, 539)]
    inp, err = adapter.validate(
        _form(target_input="A1-700,B500-539", hotspot_residues="B600"), {})
    assert err is None, err
    assert inp["hotspot_spec"] == ["B600"] and inp["hotspot_residues"] == [600]
    t, run_chain = _target(chains), inp["target_chain"]
    assert t.hotspot_error(run_chain, inp["hotspot_residues"]) is None, (
        "precondition: 600 is in range on the first named chain, so a gate "
        "reading the bare copy would fund this"
    )
    gate = t.hotspot_error(run_chain, shipped_hotspots(inp))
    assert gate and "B600" in gate, gate


def test_hotspot_residues_stays_bare_ints_on_a_single_chain_run():
    """Byte-identical to the payload submitted before any of this."""
    inp, err = adapter.validate(
        _form(target_chain="A", hotspot_residues="42,88"), {})
    assert err is None, err
    assert inp["hotspot_residues"] == [42, 88]
    assert inp["hotspot_spec"] == ["A42", "A88"]


def test_a_single_chain_run_that_names_its_chain_keeps_the_prefix_on_the_spec():
    """An operator who typed the chain gets it back where it counts. The bare
    copy strips it -- on one chain as on two -- and that is fine because the
    spec is what `shipped_hotspots` hands the gate."""
    from shared.pdb_preflight import shipped_hotspots

    inp, err = adapter.validate(
        _form(target_chain="A", hotspot_residues="A42"), {})
    assert err is None, err
    assert inp["hotspot_spec"] == ["A42"]
    assert inp["hotspot_residues"] == [42]
    assert shipped_hotspots(inp) == ["A42"]


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
    funded a run aimed at nothing.

    Modelled the way the route really does it post-#127: ``run_chain`` is
    ``validated["target_chain"]`` (blueprints/targets.py) and the tokens are
    ``shipped_hotspots(validated)``, NOT the bare ``hotspot_residues``."""
    from shared.pdb_preflight import shipped_hotspots

    chains = [("A", 234, 444), ("B", 500, 700)]
    inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700", hotspot_residues=typed), {})

    if inp is None:
        # Refused at the adapter (the bare form). That is a correct answer.
        assert "chain prefix" in err
        return

    route = _target(chains).hotspot_error(
        inp["target_chain"], shipped_hotspots(inp))
    assert _resolves(inp["hotspot_spec"], chains) == ["A600"], (
        "precondition: A600 must be the token that resolves to nothing")
    assert route is not None, (
        "the route passed a hotspot that addresses no residue in the structure")


def test_a_real_prefixed_hotspot_still_passes_the_route():
    """The complement: the fix must not start blocking correct work.

    This is the FALSE REFUSAL half. The bare copy is [241, 600]; unprefixed
    tokens are judged against the FIRST named chain, and 600 is not on A
    234-444 -- so a gate reading the bare field refuses a launch in which every
    token the model receives is real and in range."""
    from shared.pdb_preflight import shipped_hotspots

    chains = [("A", 234, 444), ("B", 500, 700)]
    inp, err = adapter.validate(
        _form(target_input="A234-444,B500-700",
              hotspot_residues="A241 B600"), {})
    assert err is None, err
    assert _resolves(inp["hotspot_spec"], chains) == []
    assert _target(chains).hotspot_error(
        inp["target_chain"], shipped_hotspots(inp)) is None


def test_the_homodimer_wrong_protomer_hotspot_is_caught_end_to_end():
    """A241 is real on an Fc homodimer, so the route CANNOT catch it -- and
    should not, because designing against one protomer is legitimate work. What
    must not happen is the operator getting there without saying so."""
    from shared.pdb_preflight import shipped_hotspots

    chains = [("A", 234, 444), ("B", 234, 444)]
    typed, err = adapter.validate(
        _form(target_input="A234-444,B234-444", hotspot_residues="A241"), {})
    assert err is None, err
    assert _target(chains).hotspot_error(
        typed["target_chain"], shipped_hotspots(typed)) is None

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


# --- where the rule has to be, not merely that the file contains it ---------
#
# ``assert "more than one chain" in body`` against a whole template cannot fail
# usefully: runs/new.html and targets/launch.html carry the phrase TWICE each,
# so deleting the copy a user actually reads still passes. Prior art in this
# repo: ``assert "600" in body`` passed on a Google Fonts URL carrying
# ``wght@400;500;600``. So each region below is extracted first and the
# assertion is scoped to it.


def _flat(text: str) -> str:
    return " ".join(text.split())


def _between(name: str, start: str, end: str) -> str:
    """The template text between two anchors, whitespace-flattened."""
    body = _template(name)
    i = body.find(start)
    assert i != -1, f"{name}: anchor {start!r} is gone; this test cannot locate the copy"
    j = body.find(end, i + len(start))
    assert j != -1, f"{name}: closing anchor {end!r} is gone after {start!r}"
    return _flat(body[i:j])


def test_the_runs_form_states_the_rule_under_proteinas_own_hotspot_field():
    """PROTEINA HAS ITS OWN FIELD NOW, so its rule belongs under that field and
    nowhere else. The rule used to live in a ``refreshTool()`` ternary that
    rewrote #hotspot-hint per tool, because ONE field served every tool; that
    ternary is gone (see the assertion below), so this server-rendered hint IS
    the string a browser shows."""
    hint = _between(
        "runs/new.html", '<div class="hint" id="chain-hotspot-hint">', "</div>")
    assert "more than one chain it is refused" in hint, (
        "runs/new.html never tells a proteina user a prefix is required")
    assert "prefix the chain instead" in hint, (
        "runs/new.html states the refusal without stating the fix")


def test_the_runs_form_does_not_advertise_the_proteina_rule_to_other_tools():
    """#hotspot-hint governs the SHARED field, which is read by five tools that
    cannot parse a chain prefix at all. Only proteina's own field may say so."""
    shared = _between(
        "runs/new.html", '<div class="hint" id="hotspot-hint">', "</div>")
    assert "more than one chain" not in shared, (
        "the shared hint claims a rule that only proteina enforces")
    assert "prefix" not in shared.lower(), (
        "the shared hint teaches a prefix to tools that refuse one")


def test_the_runs_form_no_longer_rewrites_the_shared_hint_per_tool():
    """The ternary was live copy only while one field served every tool. With
    proteina on its own field, a proteina arm there would be copy no user can
    reach — and #hotspot-hint became live server-rendered text, so a rewrite
    would silently shadow it."""
    body = _template("runs/new.html")
    assert not re.search(
        r"getElementById\('hotspot-hint'\)\.textContent\s*=", body
    ), "runs/new.html still rewrites the shared hint per tool"


def test_the_launch_forms_shared_field_says_plain_numbers_only():
    """THE COPY THAT CAUSED THE P0, INVERTED. This helper governs the ONE field
    posted to every selected tool, and five of the six refuse a chain prefix —
    rfantibody unconditionally. It must not teach one.

    targets/launch.html has no JS that touches this helper, so the string in the
    template IS the string on the page."""
    help_text = _between(
        "targets/launch.html", 'field_text("hotspot_residues"', "placeholder=")
    assert "Plain numbers only" in help_text
    assert "prefix the chain" not in help_text
    assert "Proteina's own hotspot field" in help_text, (
        "the shared field refuses a protomer without saying where one goes")


def test_the_launch_form_states_the_rule_under_proteinas_own_field():
    """The proteina-scoped helper, under the field it governs. Separately live —
    nothing overwrites it either — so it needs its own assertion."""
    helper = _between(
        "targets/launch.html", 'id="proteina__chain_hotspots"',
        '<label class="field-label">Binder length</label>')
    assert "refused when this run targets more than one chain" in helper
    assert "prefix the chain" in helper


def test_the_proteina_form_states_the_rule_under_the_hotspot_input():
    """The atomic form's own help block, directly under the input it governs."""
    help_text = _between(
        "tools/proteina_form.html",
        '<label for="hotspot_residues"',
        '<div class="hotspot-picker"',
    )
    assert "when the run targets more than one chain it is refused" in help_text
    assert "prefix the chain instead (<code>A113,C73</code>)" in help_text


def test_the_proteina_form_already_drives_a_chain_prefixed_picker():
    """Verified, not duplicated: the atomic form's picker already emits chain-
    prefixed tokens, so it needs no new control -- only honest copy."""
    body = _template("tools/proteina_form.html")
    assert "chainPrefixed: true" in body
