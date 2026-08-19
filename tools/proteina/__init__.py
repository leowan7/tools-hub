"""Proteina-Complexa — de novo binder design via inference-time search.

Modal app: ``ranomics-proteina-prod``. GPU: A100-80GB. Campaign tool.

Proteina-Complexa (NVIDIA-BioNeMo, NVIDIA Open Model License) is a
flow-matching generator wrapped in an inference-time search that filters
candidates through an AF2 / RF3 / force-field reward stack. It designs de
novo binders against a **protein** target (PDB), a **small-molecule**
target (SDF), or an enzyme/motif scaffold (AME).

Runs as a fund-and-drain compute campaign of independent search shards
(see ``shared/compute_campaigns.py``), NOT a single giant job. One shard is
one seeded ``proteinfoundation.generate`` run on one A100-80GB; every shard
gets a distinct ``++seed`` (generate.py: ``seed = cfg.seed + job_id`` makes
them independent), and the hub does global cross-shard top-K + post-hoc
diversity. ``num_designs`` scales the SHARD COUNT, not the width/depth inside
a shard — the generation profile per shard is fixed (see ``_SHARD_*`` below)
so every shard deterministically yields ``_SHARD_DESIGNS`` designs, which is
exactly the campaign ``chunk_size`` (``_CHUNK_SIZE_OVERRIDE["proteina"]``).

The four presets are the **model variants**, not a pilot/full tier — each
maps to a checked-in pipeline config + checkpoint pair. ``preset`` becomes
``campaign.preset`` and selects the container's variant:

- ``protein_binder`` — binder vs a protein target
  (``search_binder_local_pipeline``, AF2 reward).
- ``ligand_binder``  — binder vs a small-molecule target (SDF)
  (``search_ligand_binder_local_pipeline``, RF3 reward; no AF2 fallback).
- ``motif_ame``      — motif scaffolding / enzyme active-site (AME)
  (``search_ame_local_pipeline``, RF3 reward; no AF2 fallback).
- ``validate``       — the free CPU-only ``complexa validate`` pre-flight
  gate (the staging smoke; no GPU, no wallet).

Target model: a run designs against EITHER a curated ``task_name`` (a
repo-bundled benchmark task whose target is baked into the config) OR a
caller-uploaded PDB staged by the route — never both, and which one is in
play is declared explicitly as ``target_source`` rather than inferred.

Bring-your-own is a ``protein_binder`` capability only. Upstream's
``complexa target add`` writes ``configs/targets/targets_dict.yaml``, which
only the binder pipeline composes (``target_dict_cfg``); ``ligand_binder``
and ``motif_ame`` resolve separate registries (``ligand_targets_dict`` and
``configs/design_tasks/ame_dict_v2.yaml`` -> ``motif_target_dict_cfg``,
whose records key on ``contig_atoms``, which the CLI cannot emit). Those two
stay on curated tasks; see ``_CUSTOM_TARGET_PRESETS``.

A custom target additionally accepts ``target_input`` (a chain/residue
contig such as ``A1-150``, or ``A12-157,B12-157,C12-157`` for a multi-chain
target, or ``A1-50,A60-240`` for one chain with a disordered loop), hotspots,
and ``binder_length``. All three are written into the registered record; a
curated task carries its own and rejects them. The container splits a range at
every gap in the uploaded structure before registering it
(``run_pipeline.contig_runs``), so the third form rarely has to be typed —
upstream resolves every residue number in a range and raises on the first one
the file does not hold.

Hotspots arrive on either of two form keys, ``chain_hotspots`` first and the
shared ``hotspot_residues`` as a fallback. Chain-prefixed (``A45 A67``) is the
native form; a bare number is promoted onto the single target chain so one
shared launch field can still drive proteina alongside the other tools, and is
REFUSED when the run names more than one chain, where "the target chain" does
not identify a residue. The shared field is why the split exists: it is posted
to EVERY tool on the launch screen, and none of the other five will accept a
chain-qualified token arriving there from a target's saved default. rfdiffusion,
bindcraft, boltzgen and pxdesign refuse any token naming a chain the RUN does
not target (``tools/base.py::parse_hotspot_residues``); rfantibody parses it
with a bare ``int(tok)`` and refuses a prefix on any chain at all. So anything
chain-qualified has to come in on proteina's own key. See :func:`validate`.

RF3 dependency (product decision): ``ligand_binder`` and ``motif_ame`` score
on RF3 only — AF2RewardModel has no ligand protocol, so there is no AF2
fallback. If the operator turns RF3 off (the ``PROTEINA_RF3`` kill-switch),
these two variants have no valid reward and are hard-blocked in the pipeline
preflight (``rf3_required`` below flags them for the container). RF3 ships on,
so this is a defensive degraded-path guard, not a normal-operation branch.

BUILD-TIME VERIFICATION (pinned commit + canary P-1/P-2/P-3): the exact
``task_name`` set per config, the SDF -> chain-A HETATM+CONECT PDB conversion
(done in-container via RDKit, which is not in the web tier), and the reward
score keys the results viewer surfaces.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Variant registry. preset slug -> Hydra --config-name (None = free validate).
# ---------------------------------------------------------------------------

_PRESET_CONFIG: dict[str, Optional[str]] = {
    "protein_binder": "search_binder_local_pipeline",
    "ligand_binder": "search_ligand_binder_local_pipeline",
    "motif_ame": "search_ame_local_pipeline",
    "validate": None,
}
_PRESETS = tuple(_PRESET_CONFIG.keys())

# Default curated benchmark task per design variant (the canary targets). A
# blank ``task_name`` field falls back to these so the demo path needs no
# upload. Verify each against the pinned repo's configs at build time.
_DEFAULT_TASK: dict[str, str] = {
    # Curated benchmark tasks, each a top-level key in the upstream config dicts
    # (protein: configs/targets/targets_dict.yaml; ligand:
    # configs/targets/ligand_targets_dict.yaml; AME:
    # configs/design_tasks/ame_dict_v2.yaml). Each MUST carry an explicit
    # target_path pointing at a git-bundled assets/target_data/*.pdb (verified
    # 2026-07-16 against 916eaaed) so it resolves offline.
    #   protein_binder 02_PDL1        -> assets/.../bindcraft_targets/PD-L1.pdb
    #   ligand_binder  39_7V11_LIGAND -> ligand_targets_dict target
    #   motif_ame      M0024_1nzy_og  -> assets/.../ame_input_structures/M0024_1nzy.pdb
    # NOTE: the sibling AME key `M0024_1nzy` (v2) has NO bundled target (it points
    # at ame_targets/M0024_1nzy_v2.pdb, absent), so _og is the resolvable default.
    # motif_ame remains the least-verified variant (its reward_model block is
    # commented out upstream) — canary it before exposing it, protein/ligand first.
    "protein_binder": "02_PDL1",
    "ligand_binder": "39_7V11_LIGAND",
    "motif_ame": "M0024_1nzy_og",
}

# Variants whose reward stack is RF3-only (no AF2 ligand protocol). The
# container hard-blocks these when PROTEINA_RF3 is off; surfaced here so the
# adapter can stamp the flag onto the job_spec.
_RF3_REQUIRED = {"ligand_binder", "motif_ame"}

# Variants that take a protein target chain (protein PDB / motif PDB). The
# ligand variant's target is an SDF, which has no chain.
_CHAIN_PRESETS = {"protein_binder", "motif_ame"}

# Variants that can design against a CALLER-SUPPLIED target. Only the protein
# binder: `complexa target add` writes configs/targets/targets_dict.yaml, which
# is composed into `target_dict_cfg` by configs/pipeline/binder/binder_generate
# .yaml (`defaults: - /targets/targets_dict@_here_`). The other two variants
# resolve a DIFFERENT registry — ligand_binder against ligand_targets_dict and
# motif_ame against configs/design_tasks/ame_dict_v2.yaml (-> motif_target_dict
# _cfg, whose records key on `contig_atoms`, a motif atom spec the target CLI
# has no flag to emit). Verified against 916eaaed. So bring-your-own is a
# protein-binder capability upstream, not a wrapper limitation; the other two
# keep their curated tasks and the container refuses a staged target for them.
_CUSTOM_TARGET_PRESETS = {"protein_binder"}

# ---------------------------------------------------------------------------
# Fixed per-shard generation profile. Locked (not user-tunable) so every shard
# yields exactly _SHARD_DESIGNS designs == the campaign chunk_size; the user's
# scale knob is num_designs (the shard count). Exposing nsamples/replicas would
# desync the chunk math (chunk_size is pinned to 8 in _CHUNK_SIZE_OVERRIDE).
# Values are the upstream protein-variant defaults: designs/shard =
# nsamples x nrepeat_per_sample x best_of_n.replicas = 4 x 1 x 2 = 8.
# ---------------------------------------------------------------------------

_SHARD_NSAMPLES = 4      # generation.dataloader.dataset.nres.nsamples
_SHARD_REPLICAS = 2      # generation.search.best_of_n.replicas
_SHARD_NSTEPS = 400      # generation.args.nsteps (upstream default)
_SHARD_DESIGNS = _SHARD_NSAMPLES * _SHARD_REPLICAS  # == _CHUNK_SIZE_OVERRIDE

# task_name is a Hydra ++generation.task_name selector resolved against the
# repo configs in-container; bound it to a safe token here and let the pipeline
# preflight reject an unknown name before GPU spend.
_TASK_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# ---------------------------------------------------------------------------
# Custom-target grammar (bring-your-own PDB).
#
# Upstream's target record (configs/targets/targets_dict.yaml @916eaaed) is:
#     target_input:      "A1-115"  or  "A12-157,B12-157,C12-157"  (multi-chain)
#     hotspot_residues:  ["A37", "A39", "A49", "A98"]
#     binder_length:     [64, 155]
#
# Both residue fields are ORIGINAL PDB AUTHOR NUMBERING, matching every other
# tool in the hub.
#
# The hotspot format is the load-bearing one. load_target_from_pdb does:
#     if f"{atom.chain_id}{atom.res_id}" in target_hotspots: mask[idx] = True
# — a literal, case-sensitive string match with NO separator, and a token that
# matches nothing is SILENTLY DROPPED to an all-zero mask. A run that quietly
# ignored every hotspot is indistinguishable from one that honoured them, so
# format validation here is only half the guard; run_pipeline re-checks every
# token against the actual uploaded structure before any GPU is spent.
#
# Chain ids are restricted to a SINGLE character — deliberately narrower than
# upstream's contig grammar. "A1-115" is otherwise ambiguous about where the
# chain id ends, and the upstream parser (atomworks AtomSelectionStack.
# from_contig) has an unverified failure mode on a chain it cannot resolve.
# Narrow is the right call when the failure is silent.
# ---------------------------------------------------------------------------

# Chain ids are matched as a single LETTER, not [A-Za-z0-9]. A digit chain id
# would make the whole grammar ambiguous: with an optional alphanumeric prefix,
# "42" parses as chain "4" residue 2 rather than bare residue 42 — and bare
# residue numbers are exactly what the shared launch field posts for every
# other tool (_SHARED_LAUNCH_FIELDS, blueprints/targets.py). Numeric chain ids
# are legal in mmCIF but vanishingly rare in author-numbered PDBs, and they
# fail here with a readable message rather than silently binding to the wrong
# residue. That trade is the whole reason this regex is not [A-Za-z0-9].
_SEGMENT_RE = re.compile(r"^([A-Za-z])(-?\d+)-(-?\d+)$")
_WHOLE_CHAIN_RE = re.compile(r"^([A-Za-z])$")
_HOTSPOT_RE = re.compile(r"^([A-Za-z])?(-?\d+)$")

# A cap on what a human TYPES here, not on what the container ships.
#
# UNCALIBRATED, and it always was — >8 hand-written chain ranges is a modelling
# smell, which is a judgement and not a measurement. Its scope is now the part
# worth stating: the container splits a range at every disordered gap before it
# reaches `complexa target add` (`run_pipeline.contig_runs`), so ONE segment
# typed here can legitimately become several runs down there, and the ceiling
# that bounds those is `run_pipeline.MAX_CONTIG_RUNS` (64), not this. Raising
# this number is a form-usability decision; raising that one is a decision about
# what structures the service will design against.
_MAX_SEGMENTS = 8
_MAX_HOTSPOTS = 64       # mirrors iggm's EPITOPE_MAX order of magnitude
_MAX_CHAIN_FIELD = 32    # "A B C D ..." — bounds the space-joined chain string

# The widest ``target_input`` this parser will look at, and the number the three
# templates that render the field set ``maxlength`` to (templates/tools/
# proteina_form.html, templates/runs/new.html, templates/targets/launch.html).
#
# DERIVED, not chosen, unlike the two above. It is an upper bound on the longest
# contig anything here would accept, plus headroom:
#
#   * ``_MAX_SEGMENTS`` (8) ranges is the most this parser takes;
#   * a range renders as ``<letter><lo>-<hi>`` — the chain id is ONE character
#     (``_SEGMENT_RE`` is ``[A-Za-z]``) and a residue number is at most FOUR
#     ("9999", or "-999" on a tagged construct), because the numbering comes out
#     of a PDB and ``run_pipeline.pdb_ca_residues`` reads ``line[22:26]``, a
#     four-column resSeq. So a range is at most 1 + 4 + 1 + 4 = 10 characters;
#   * 8 of those, comma-joined, is 8 * 10 + 7 = 87.
#
# 128 is the next round number above 87, ~47% of headroom. The headroom is not
# decoration: the point of the cap is that nothing a user can legitimately type
# — and nothing ``run_pipeline`` can legitimately PRINT for them to paste (see
# ``MAX_HINT_RUNS``, bounded by the same ``_MAX_SEGMENTS``) — ever reaches it,
# so the field can never silently keep a prefix of a contig. A truncated contig
# is still a valid contig, so a browser that trims one produces a smaller target
# that no gate downstream can distinguish from an intended one.
#
# IT IS ALSO A REAL SERVER-SIDE REFUSAL, not just a mirror of an attribute:
# ``maxlength`` is an affordance in a browser and nothing at all to curl.
# ``_SEGMENT_RE``'s ``(-?\d+)`` is unbounded and this parser calls ``int()`` on
# what it captures — and since Python 3.11 ``int()`` REFUSES a string over 4300
# digits — so ``A1-<5000 nines>`` used to come back out of ``validate()`` as an
# unhandled ValueError, i.e. a 500. The length check runs before the loop, so
# those digits are never converted.
_MAX_TARGET_INPUT_FIELD = 128

# Binder length envelope. Upstream's own curated records span [50, 155]; the
# target-CLI default is [60, 120]. The generator samples the binder length
# uniformly from this range per design (UniformInt on
# generation.dataloader.dataset.nres), so it is a real design knob, not a cap.
_BINDER_LEN_DEFAULT = (60, 120)
_BINDER_LEN_MIN = 20
_BINDER_LEN_MAX = 300


def _parse_target_input(
    raw: str,
) -> tuple[list[tuple[str, int, int]], str, list[str], Optional[str]]:
    """Parse a chain/residue-range contig into segments.

    ``"A1-150"`` -> ``[("A", 1, 150)]``; ``"A12-157,B12-157"`` -> two segments.
    A bare chain id (``"A"``) means "the whole chain" and is returned with a
    ``(None, None)`` sentinel range for run_pipeline to resolve against the
    real structure (it, not the web tier, has the residue numbering).

    Returns ``(segments, canonical, chain_ids, error)``. An empty input is not
    an error — it means "derive the full observed range in-container".
    """
    text = (raw or "").strip()
    if not text:
        return [], "", [], None
    # BEFORE THE LOOP, because the loop calls ``int()`` on an unbounded
    # ``(-?\d+)`` capture and ``int()`` itself raises above 4300 digits — which
    # left ``validate()`` returning an exception rather than a message. See
    # ``_MAX_TARGET_INPUT_FIELD`` for where 128 comes from; the ceiling is far
    # above the longest contig ``_MAX_SEGMENTS`` ranges can spell, so nothing a
    # user would type on purpose can reach it.
    if len(text) > _MAX_TARGET_INPUT_FIELD:
        return [], "", [], (
            f"Target chain range is too long (max {_MAX_TARGET_INPUT_FIELD} "
            f"characters, this is {len(text)}). Give at most {_MAX_SEGMENTS} "
            "ranges, like A1-150 or A1-50,A60-240."
        )

    segments: list[tuple[str, int, int]] = []
    chain_ids: list[str] = []
    for token in (t.strip() for t in text.replace(";", ",").split(",")):
        if not token:
            continue
        whole = _WHOLE_CHAIN_RE.match(token)
        if whole:
            chain, lo, hi = whole.group(1), None, None
        else:
            m = _SEGMENT_RE.match(token)
            if not m:
                return [], "", [], (
                    f'Target chain range "{token}" is not valid. Use a chain '
                    "letter with a residue range, like A1-150, or A1-150,B12-157 "
                    "for several chains."
                )
            chain, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
            if lo > hi:
                return [], "", [], (
                    f'Target chain range "{token}" runs backwards — write it '
                    f"low to high, like {chain}{hi}-{lo}."
                )
            if lo < 0 or hi < 0:
                # Upstream parses the contig with r"([A-Za-z]+)(\d+)-(\d+)" and
                # raises ValueError on no match, so a negative bound would die
                # on the GPU after the shard has booted. run_pipeline refuses
                # again in-container (including for ranges it derives itself
                # from a tagged structure); this catches the typed case for
                # free, before a campaign exists.
                return [], "", [], (
                    f'Target chain range "{token}" uses negative residue '
                    "numbers, which the design engine cannot express. Pick a "
                    f"range starting at 0 or above, like {chain}1-{max(hi, 1)}."
                )
        # A CHAIN MAY REPEAT WHEN THE RANGES ARE DISJOINT. This used to be a
        # flat "Chain A appears more than once", which made the CORRECT contig
        # for a gapped target un-typable: upstream resolves every integer
        # between a range's endpoints and raises on the first residue the file
        # does not hold, so a chain with a disordered loop has to be written
        # A1-50,A60-240 — a chain named twice — and a user who knew that could
        # not say it. run_pipeline now derives that split itself
        # (``contig_runs``), but the two must agree about what is legal or the
        # container would accept a contig the form refuses.
        #
        # OVERLAP IS STILL REFUSED, and what it used to backstop is now held
        # properly downstream. The flat rule existed because A10-20,A10-20
        # counted 22 residues for 11 and defeated the container's 20-residue
        # floor; ``run_pipeline.target_too_small`` now counts
        # ``n_selected_residues``, which is ``len(selected_residue_keys(...))``
        # — a de-duplicated key SET, as its docstring states — so the same
        # contig counts 11 and is refused there whether or not it arrives
        # through this parser. The web tier's own size gate
        # (``shared/targets.py::selection_residue_count``) sums per segment and
        # is documented as an UPPER bound that rounds up, so a repeat can only
        # make it more conservative, never less.
        for prev_chain, prev_lo, prev_hi in segments:
            if prev_chain != chain:
                continue
            # A bare chain id is "the whole chain", so it overlaps every other
            # range on that chain — including a second bare id.
            if lo is None or prev_lo is None or (lo <= prev_hi and prev_lo <= hi):
                return [], "", [], (
                    f"Target chain ranges "
                    f"{_format_contig([(prev_chain, prev_lo, prev_hi)])} and "
                    f"{_format_contig([(chain, lo, hi)])} overlap. A chain may "
                    "be listed more than once only when its ranges do not "
                    "overlap, like A1-50,A60-240 for a chain with a disordered "
                    "loop."
                )
        if chain not in chain_ids:
            # DISTINCT chains, in first-appearance order. `chain_ids` becomes
            # `target_chain` ("A B") and the allow-list `_parse_hotspots`
            # judges a hotspot's prefix against; a repeat here would render
            # "chain A A" and make the hotspot refusal read "write A241 or
            # A241".
            chain_ids.append(chain)
        segments.append((chain, lo, hi))

    if not segments:
        return [], "", [], None
    if len(segments) > _MAX_SEGMENTS:
        return [], "", [], (
            f"Too many chain ranges (max {_MAX_SEGMENTS})."
        )
    return segments, _format_contig(segments), chain_ids, None


def parse_target_segments(raw: str) -> list:
    """Public wrapper over :func:`_parse_target_input`'s segment list.

    Exists so the preflight size gate can size the CONTIG'S SELECTION rather
    than the whole uploaded file without keeping a second copy of the contig
    grammar. ``shared/pdb_intake.preflight_target_segments`` imports this
    lazily; a duplicated parser there would drift from this one, and the drift
    would land in whichever copy is not the money gate.

    Returns ``[]`` on an unparseable or empty contig — the caller treats that
    as "no selection declared" and sizes the whole named chain(s), which
    over-counts rather than under-counts. Never raises: a contig this rejects
    is separately refused by ``validate()`` with a real message, and preflight
    must not 500 ahead of it.
    """
    segments, _contig, _chains, err = _parse_target_input(raw)
    return [] if err else list(segments)


def _format_contig(segments: list[tuple[str, int, int]]) -> str:
    """Render segments back to upstream's contig string. Whole-chain segments
    render as the bare chain id; run_pipeline expands them once it can see the
    structure's real residue numbering."""
    parts = []
    for chain, lo, hi in segments:
        parts.append(chain if lo is None else f"{chain}{lo}-{hi}")
    return ",".join(parts)


def _english_list(items: list[str], conj: str = "and") -> str:
    """``["A", "B"] -> "A and B"``; ``["A", "B", "C"] -> "A, B and C"``.

    Only ever renders chain ids and hotspot tokens into a refusal message.
    """
    items = list(items)
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} {conj} {items[-1]}"


def _parse_hotspots(
    raw: str, chain_ids: list[str], default_chain: str
) -> tuple[list[str], list[int], Optional[str]]:
    """Parse hotspot residues into BOTH representations the stack needs.

    Accepts ``A45 A67 A89`` (upstream's chain-prefixed form) and plain
    ``54,56,115`` (what the shared launch field posts for every other tool —
    see ``_SHARED_LAUNCH_FIELDS`` in blueprints/targets.py).

    ``chain_ids`` is EVERY chain this run targets — the contig's chains when
    there is a contig, otherwise ``target_chain``'s. It used to be handed only
    the contig's, so a run that named its chains in ``target_chain`` ("A B",
    no contig) looked single-chain here, which broke this function in both
    directions at once: a bare token was silently promoted onto A, and an
    explicit ``B264`` was refused as "not one of this run's target chains (A)".

    A BARE NUMBER IS REFUSED WHEN THE RUN NAMES MORE THAN ONE CHAIN. Upstream
    matches hotspots literally, as ``f"{chain_id}{res_id}"``, so a bare number
    has to be attributed to some chain before it addresses anything — and
    "attribute it to the first one" is only unambiguous when there IS only one.
    On an IgG1 Fc, whose protomers share one author numbering, ``241`` became
    ``A241``, which is a real residue: the route's range check passed, preflight
    passed, the container's own ``normalize_hotspots`` guard never saw a bare
    token to refuse, and the run designed against protomer A with B entirely
    unconstrained — indistinguishable, at every layer, from a correct run. The
    refusal has to happen here because here is the last place the ambiguity is
    still visible.

    Single-chain runs promote exactly as before, which is what keeps proteina
    co-launchable with rfdiffusion/pxdesign from one shared hotspot field.

    Returns ``(spec, resnums, error)``:
      * ``spec``    — ``["A45", "A67"]``, what upstream string-matches on and
        what ``build_payload`` ships. THE AUTHORITY.
      * ``resnums`` — ``[45, 67]``, bare author numbers. A LOSSY COPY, kept
        because it is the shape the shared ``hotspot_residues`` key carries
        fleet-wide and several older readers still expect ints there.

    Nothing that decides money reads ``resnums`` directly any more. The four
    paid gates call ``shared.pdb_preflight.shipped_hotspots(inputs)``, which
    prefers ``hotspot_spec`` and only falls back to ``hotspot_residues`` for
    tools that have no spec — so the token the gates judge is the token the
    container matches on. That is what makes the bare copy safe to keep here:
    it is no longer load-bearing. Do not reintroduce a range check that reads
    it, because ``[600]`` cannot distinguish a typed ``600`` from a stripped
    ``B600`` and answering with "600 exists on some chain" is the exact
    question the run had already decided differently.

    Empty input is not an error — proteina's hotspots are OPTIONAL (an
    unconstrained search is a legitimate run), matching boltzgen rather than
    rfdiffusion/bindcraft/pxdesign, which require them.
    """
    text = (raw or "").strip()
    if not text:
        return [], [], None

    allowed = list(chain_ids) or ([default_chain] if default_chain else [])
    spec: list[str] = []
    resnums: list[int] = []
    seen: set[str] = set()
    for token in (t.strip() for t in text.replace(";", ",").replace(",", " ").split()):
        if not token:
            continue
        m = _HOTSPOT_RE.match(token)
        if not m:
            return [], [], (
                f'Hotspot residue "{token}" is not valid. Use residue numbers, '
                "optionally chain-prefixed, separated by spaces or commas "
                "(e.g. A45 A67 A89)."
            )
        chain, number = m.group(1), int(m.group(2))
        if chain is None:
            if len(allowed) > 1:
                return [], [], (
                    f'Hotspot "{token}" needs a chain prefix — this run '
                    f"targets chains {_english_list(allowed)}, so write "
                    f'{_english_list([f"{c}{number}" for c in allowed], "or")}.'
                )
            if not default_chain:
                return [], [], (
                    f'Hotspot residue "{token}" needs a chain prefix (e.g. '
                    f"A{number}) because this run has no single target chain."
                )
            chain = default_chain
        elif allowed and chain not in allowed:
            return [], [], (
                f'Hotspot "{token}" names chain {chain}, which is not one '
                f"of this run's target chains ({', '.join(allowed)})."
            )
        key = f"{chain}{number}"
        if key in seen:
            continue
        seen.add(key)
        spec.append(key)
        resnums.append(number)

    if len(spec) > _MAX_HOTSPOTS:
        return [], [], f"Too many hotspot residues (max {_MAX_HOTSPOTS})."
    return spec, resnums, None


def _parse_binder_length(
    raw_min: str, raw_max: str
) -> tuple[tuple[int, int], Optional[str]]:
    """Parse the binder length range written into the target record.

    Upstream samples each design's length uniformly from this range, so an
    unset value is not "no constraint" — it is the CLI's [60, 120] default.
    Exposing it is what stops that default silently capping the design space.
    """
    lo_text = (raw_min or "").strip()
    hi_text = (raw_max or "").strip()
    if not lo_text and not hi_text:
        return _BINDER_LEN_DEFAULT, None
    try:
        lo = int(lo_text) if lo_text else _BINDER_LEN_DEFAULT[0]
        hi = int(hi_text) if hi_text else _BINDER_LEN_DEFAULT[1]
    except ValueError:
        return _BINDER_LEN_DEFAULT, "Binder length must be whole numbers."
    if lo > hi:
        return _BINDER_LEN_DEFAULT, (
            "Binder length minimum must not exceed the maximum."
        )
    if lo < _BINDER_LEN_MIN or hi > _BINDER_LEN_MAX:
        return _BINDER_LEN_DEFAULT, (
            f"Binder length must be between {_BINDER_LEN_MIN} and "
            f"{_BINDER_LEN_MAX} residues."
        )
    return (lo, hi), None


def _describe(
    preset: str,
    task_name: str,
    target_chain: str,
    contig: str = "",
    hotspot_spec: Optional[list[str]] = None,
) -> str:
    if preset == "ligand_binder":
        return f"ligand binder vs {task_name or 'uploaded SDF'}"
    if preset == "motif_ame":
        return f"motif/enzyme scaffold ({task_name or 'uploaded motif'})"
    if preset == "validate":
        return "free validate dry-run"
    where = task_name or "uploaded target"
    # The contig already names its chains, so printing target_chain too would
    # say "chain A B" next to "A1-150,B12-157".
    if contig:
        scope = f" {contig}"
    elif target_chain:
        scope = f" chain {target_chain}"
    else:
        scope = ""
    hot = f" @ {' '.join(hotspot_spec)}" if hotspot_spec else ""
    return f"protein binder vs {where}{scope}{hot}"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce the Proteina form into a shard job_spec.

    ``num_designs`` (the campaign scale) is NOT validated here — the campaign
    route injects a placeholder count and the driver sets the real per-chunk
    value. This validates the per-shard params only. The target FILE is staged
    by the route, not read here; what this settles is which of the two target
    sources the run declares, and that its parameters are coherent with that
    choice.

    ``_has_custom_target`` is injected by the route AFTER it has resolved
    whether a structure actually exists (a bound target or an attached upload).
    It is deliberately not a user-controllable field: both routes assign it
    over the top of the form dict, so a crafted ``proteina___has_custom_target``
    cannot forge a custom run that has no staged structure behind it. An absent
    key means ``curated``, which is the safe default — the container refuses
    independently if a URL shows up on a run that did not declare one.
    """
    preset = (form.get("preset") or "protein_binder").strip()
    if preset not in _PRESET_CONFIG:
        return None, "Pick a design variant."

    is_custom = _truthy(form.get("_has_custom_target"))
    if is_custom and preset not in _CUSTOM_TARGET_PRESETS and preset != "validate":
        return None, (
            f"The {preset} variant cannot design against your own target — "
            "upstream resolves it from a separate task registry. Pick the "
            "protein binder variant, or choose a curated benchmark task."
        )
    target_source = "custom" if is_custom else "curated"

    # A curated run falls back to the variant's default benchmark task. A CUSTOM
    # run must NOT: this default-fill runs before the exclusivity check below,
    # so leaving it unconditional would stamp 02_PDL1 onto every bring-your-own
    # run whose task field is blank — which is the normal case — and design
    # against PD-L1 instead of the user's structure. That is precisely the
    # silent-wrong-target failure the custom-target path exists to avoid.
    task_name = (form.get("task_name") or "").strip()
    if not task_name and target_source == "curated":
        task_name = _DEFAULT_TASK.get(preset, "")
    if task_name and not _TASK_RE.match(task_name):
        return None, (
            "Target task name may use letters, digits, underscore and "
            "hyphen (max 64 characters)."
        )
    if preset != "validate" and target_source == "curated" and not task_name:
        return None, "Choose a target task or upload a custom target."
    if target_source == "custom" and task_name:
        return None, (
            "A custom target and a curated benchmark task are mutually "
            "exclusive. Clear the target task to design against your own "
            "structure."
        )

    raw_contig = (form.get("target_input") or "").strip()
    # PROTEINA'S OWN HOTSPOT FIELD FIRST, the shared one only as a fallback.
    #
    # `hotspot_residues` is the ONE field the multi-tool launch screen posts to
    # every selected tool (`blueprints/targets._SHARED_LAUNCH_FIELDS`), and it
    # can only ever carry plain integers: rfdiffusion, bindcraft, boltzgen and
    # pxdesign refuse a token naming a chain the run does not target, and
    # `tools/rfantibody` parses it with a bare `int(tok)` that refuses a prefix
    # on ANY target chain. So a chain-qualified hotspot cannot travel in it.
    # `chain_hotspots` is proteina-scoped — posted as `proteina__chain_hotspots`
    # on the launch screen, which `_tool_form` un-prefixes, and as
    # `chain_hotspots` on the campaign form — so it can.
    #
    # `or`, not "present": an EMPTY proteina field falls back to the shared one,
    # which is what keeps a single-chain co-launch driven entirely from the
    # shared field working unchanged. It also means proteina's field cannot be
    # used to CLEAR a shared hotspot; clear the shared field for that.
    raw_hotspots = (
        (form.get("chain_hotspots") or "").strip()
        or (form.get("hotspot_residues") or "").strip()
    )
    raw_len_min = form.get("binder_length_min") or ""
    raw_len_max = form.get("binder_length_max") or ""

    # These three only mean anything against a structure we registered. A
    # curated task carries its own contig, hotspots and length range, and
    # ++generation.task_name cannot override them — so accepting them here
    # would silently discard what the user typed.
    if target_source == "curated" and (raw_contig or raw_hotspots):
        return None, (
            "Chain ranges and hotspot residues apply to your own uploaded "
            f"target. The curated benchmark task {task_name} carries its own."
        )

    segments, contig, contig_chains, err = _parse_target_input(raw_contig)
    if err:
        return None, err

    target_chain = ""
    if preset in _CHAIN_PRESETS:
        if contig_chains:
            # The contig names its own chains, so deriving target_chain from it
            # keeps one source of truth and still feeds the routes' existing
            # DesignTarget.chain_error range check.
            target_chain = " ".join(contig_chains)
        else:
            target_chain = " ".join(
                (form.get("target_chain") or "A").replace(",", " ").split()
            )
        if not target_chain:
            return None, "Target chain is required."
        if len(target_chain) > _MAX_CHAIN_FIELD:
            return None, (
                f"Target chain is too long (max {_MAX_CHAIN_FIELD} characters). "
                "List chains separated by spaces, like A B C."
            )
        for chain in target_chain.split():
            if len(chain) > 4:
                return None, f"Chain id {chain!r} is too long (max 4 characters)."

    # EVERY chain this run targets. A contig REPLACES target_chain rather than
    # adding to it (prepare_custom_target derives its segments from
    # target_input and never looks at target_chain again), so the contig's
    # chains ARE the design when there is one. Passing only `contig_chains`
    # here made a contig-less "A B" run look single-chain to _parse_hotspots.
    run_chains = contig_chains or target_chain.split()
    default_chain = (run_chains or [""])[0]
    hotspot_spec, hotspot_residues, err = _parse_hotspots(
        raw_hotspots, run_chains, default_chain
    )
    if err:
        return None, err

    binder_length, err = _parse_binder_length(raw_len_min, raw_len_max)
    if err:
        return None, err

    return (
        {
            "preset": preset,
            "config_name": _PRESET_CONFIG[preset],
            "task_name": task_name,
            "target_source": target_source,
            "target_chain": target_chain,
            "target_input": contig,
            # Kept out of the payload — the route range-checks these against the
            # target's persisted chain_summary, the container against the real
            # structure. Prefixed so sanitize_shared_params drops it from
            # campaign.params rather than replaying a stale copy.
            "_target_segments": segments,
            # Two representations, ONE authority. `hotspot_spec` is the
            # chain-prefixed form upstream string-matches on and build_payload
            # ships; `hotspot_residues` is the same tokens with the chain
            # letter stripped, kept because the shared launch field carries
            # bare ints fleet-wide and older readers still expect them here.
            # The bare copy is LOSSY and nothing that spends money reads it:
            # every paid gate goes through
            # `shared.pdb_preflight.shipped_hotspots`, which prefers the spec.
            # See _parse_hotspots for why re-deriving a verdict from the bare
            # copy is the defect, not the fix.
            "hotspot_residues": hotspot_residues,
            "hotspot_spec": hotspot_spec,
            "binder_length": list(binder_length),
            # RF3-only reward -> the container hard-blocks these when RF3 is off.
            "rf3_required": preset in _RF3_REQUIRED,
            # Locked generation profile (see _SHARD_*). run_pipeline reads these
            # for the Hydra overrides; keeping them here (not on the form) is what
            # guarantees designs/shard == chunk_size for the wallet math.
            "nsamples": _SHARD_NSAMPLES,
            "replicas": _SHARD_REPLICAS,
            "nsteps": _SHARD_NSTEPS,
            "designs_per_shard": _SHARD_DESIGNS,
            "target": _describe(
                preset, task_name, target_chain, contig, hotspot_spec
            ),
            "parameters": {"n_designs_total": _SHARD_DESIGNS},
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Proteina shard job_spec.

    A custom target's presigned URL is forwarded by the campaign dispatch path
    (from ``campaign.target_storage_path``), not embedded here — matches boltz2
    / boltzgen / iggm. A curated-task run carries no target file; the container
    resolves the target from the repo config for ``task_name``.

    The five custom-target keys are read with ``.get`` defaults on purpose: a
    campaign created before they existed replays its stored ``params`` through
    this function on every later wave, and a bare ``[]`` lookup would strand it
    with a KeyError mid-drain. The defaults reproduce the old curated behaviour
    exactly.
    """
    return {
        "preset": inputs["preset"],
        "config_name": inputs["config_name"],
        "task_name": inputs["task_name"],
        "target_source": inputs.get("target_source", "curated"),
        "target_chain": inputs["target_chain"],
        "target_input": inputs.get("target_input", ""),
        "hotspot_residues": inputs.get("hotspot_residues", []),
        "hotspot_spec": inputs.get("hotspot_spec", []),
        "binder_length": inputs.get("binder_length", list(_BINDER_LEN_DEFAULT)),
        "rf3_required": inputs["rf3_required"],
        "nsamples": inputs["nsamples"],
        "replicas": inputs["replicas"],
        "nsteps": inputs["nsteps"],
        "parameters": inputs["parameters"],
    }


adapter = ToolAdapter(
    slug="proteina",
    label="Proteina-Complexa",
    # The BLURB says what this form does; the lede above it
    # (blueprints/tools.py::_PREVIEW_SEO_PHRASES) sells the task. They
    # render two paragraphs apart in the same hero, so near-identical
    # sentences read as a stutter.
    #
    # It said "filters every candidate through three independent scoring
    # checks", which is false — see the note on ``comparison_one_liner``
    # in meta.py and Dockerfile.modal:229-231. The scoring model follows
    # the target; no variant runs all three.
    blurb=(
        "Upload a protein or small-molecule target, name the chain and "
        "the residues you want gripped, and set how many designs to "
        "fund. The run fans out across GPUs and stops when your wallet "
        "does."
    ),
    presets=(
        Preset(
            slug="protein_binder",
            label="Protein binder (de novo, vs a protein target)",
            description=(
                "Design de novo binders against a protein target. Search is "
                "scored by AlphaFold2 confidence plus a force-field reward. "
                "Pick a curated target task or upload your own target PDB."
            ),
            requires_pdb=False,
            long_running=True,
        ),
        Preset(
            slug="ligand_binder",
            label="Ligand binder (de novo, vs a small molecule)",
            description=(
                "Design de novo binders against a small-molecule target "
                "supplied as an SDF. Scored by the RoseTTAFold3 reward "
                "(the force field does not support protein-ligand complexes). "
                "Pick a curated ligand task or upload your own SDF."
            ),
            requires_pdb=False,
            long_running=True,
        ),
        Preset(
            slug="motif_ame",
            label="Motif scaffolding / enzyme (AME)",
            description=(
                "Scaffold a functional motif or enzyme active site. Scored by "
                "the RoseTTAFold3 reward. Pick a curated AME task or upload "
                "your own motif."
            ),
            requires_pdb=False,
            long_running=True,
        ),
        Preset(
            slug="validate",
            label="Validate (free dry-run)",
            description=(
                "Free CPU-only pre-flight that checks your target + config "
                "load before you commit GPU to a paid search. No wallet charge."
            ),
            requires_pdb=False,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    # Target is OPTIONAL (curated task vs custom upload) and the SDF path is
    # ligand-only, so the campaign route handles staging, not the generic
    # requires_pdb upload plumbing.
    requires_pdb=False,
    form_template="tools/proteina_form.html",
    results_partial="tools/proteina_results.html",
)

register(adapter)
