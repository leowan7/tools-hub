"""Platform API — calibrated antigen catalogue.

A small in-memory catalogue of antigens we have already wet-lab-validated
against. Calibrated targets get two pieces of automation that custom
targets don't:

1. ``GET /api/v1/targets`` lists them so agents can discover the catch
   set during planning.
2. ``POST /api/v1/experiments/cost-estimate`` returns a real range
   (``requires_human_quote: false``) instead of a placeholder.
3. ``POST /api/v1/experiments`` accepts ``target.target_id`` referring
   to a catalogue entry and skips the human-scoping handoff for
   workflows that fit a previously-run shape.

Design choices
--------------
- In-memory Python rather than a Postgres table: the alpha catalogue is
  small (5 entries), changes via PR are auditable, and the cost-estimate
  call avoids a DB round-trip on the hot path. Migrate to a table when
  the catalogue exceeds ~25 entries or operator-managed additions become
  routine.
- Each entry maps ``experiment_type`` to a typical campaign cost band.
  Bands are deliberately wide (sort-round count, NGS depth, and library
  size all materially shift the final number); a calibrated band is
  narrower than the all-purpose placeholder in ``_placeholder_range``
  because we have ground-truth campaign data for it.
- ``antigen_sequence`` carries a stub (signal peptide trimmed, ECD or
  soluble form only) so an agent can plan its design panel without an
  extra UniProt round-trip. The lab uses an internal canonical form
  during library construction; the field exists to inform the design
  side, not to lock the wet-lab side.
- ``target_id`` is a stable, opaque, human-readable string. We do not
  use UUIDs because catalogue entries are intentionally curated and
  long-lived; ``tgt_her2_ecd_v1`` is more useful in logs than a UUID.

This module is import-safe: no side effects, no DB calls, no network.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

# Each entry is the on-wire shape returned by ``GET /api/v1/targets`` plus
# the internal ``typical_campaign_range_usd`` map used by cost-estimate.
# Keep this list short. When a new entry duplicates an existing one in
# substance (same antigen, different vendor tag), prefer adding it as a
# variant of the existing ``target_id`` rather than minting a new id.

CATALOG: list[dict] = [
    {
        "target_id": "tgt_her2_ecd_v1",
        "name": "HER2 ECD (subdomain IV)",
        "official_symbol": "ERBB2",
        "uniprot_id": "P04626",
        "antigen_form": "Recombinant soluble ECD, biotinylated",
        "antigen_sequence_stub": "TQVCTGTDMKLRLPASPETHLDMLRHLYQGCQVVQGNLELTYLPTNASLSFLQDIQEVQGYVLIAHNQVRQVPLQRLRIVRGTQLFEDNYALAVLDNGDPLNNTTPVTGASPGGLRELQLRSLTEILKGGVLIQRNPQLCYQDTILWKDIFHKNNQLALTLIDTNRSRACHPCSPMCKGSRCWGESSEDCQSLTRTVCAGGCARCKGPLPTDCCHEQCAAGCTGPKHSDCLAC",
        "supported_experiment_types": ["yeast_display", "mammalian_display"],
        "indication_area": "oncology",
        "calibration_notes": (
            "Sorting against subdomain IV with anti-HER2 detection; "
            "validated panel against trastuzumab-class scaffolds. "
            "Mammalian display is the better fit when full-length IgG "
            "assembly is part of the read."
        ),
        # Real campaign bands. The mammalian band is wider because PTM
        # variability across CHO clones widens the rerun probability.
        "typical_campaign_range_usd": {
            "yeast_display": [14000, 38000],
            "mammalian_display": [32000, 95000],
        },
    },
    {
        "target_id": "tgt_pdl1_ecd_v1",
        "name": "PD-L1 ECD",
        "official_symbol": "CD274",
        "uniprot_id": "Q9NZQ7",
        "antigen_form": "Recombinant soluble ECD, biotinylated",
        "antigen_sequence_stub": "FTVTVPKDLYVVEYGSNMTIECKFPVEKQLDLAALIVYWEMEDKNIIQFVHGEEDLKVQHSSYRQRARLLKDQLSLGNAALQITDVKLQDAGVYRCMISYGGADYKRITVKVNAPYNKINQRILVVDPVTSEHELTCQAEGYPKAEVIWTSSDHQVLSGKTTTTNSKREEKLFNVTSTLRINTTTNEIFYCTFRRLDPEENHTAELVIPELPLAHPPNERTHLVILGAILLCLGVALTFI",
        "supported_experiment_types": ["yeast_display", "mammalian_display", "dms"],
        "indication_area": "immuno-oncology",
        "calibration_notes": (
            "PD-1 cross-block panel routinely available. "
            "DMS supported around the canonical PD-1 binding face. "
            "Mammalian display recommended when downstream IgG4 "
            "developability matters for the program."
        ),
        "typical_campaign_range_usd": {
            "yeast_display": [14000, 36000],
            "mammalian_display": [32000, 92000],
            "dms": [22000, 65000],
        },
    },
    {
        "target_id": "tgt_cd3e_ecd_v1",
        "name": "CD3 epsilon ECD",
        "official_symbol": "CD3E",
        "uniprot_id": "P07766",
        "antigen_form": "Recombinant soluble ECD heterodimer (CD3 epsilon/gamma)",
        "antigen_sequence_stub": "DGNEEMGGITQTPYKVSISGTTVILTCPQYPGSEILWQHNDKNIGGDEDDKNIGSDEDHLSLKEFSELEQSGYYVCYPRGSKPEDANFYLYLRARVCENCMEMDVMSVATIVIVDICITGGLLLLVYYWS",
        "supported_experiment_types": ["yeast_display", "mammalian_display"],
        "indication_area": "T-cell engagers",
        "calibration_notes": (
            "Standard for the engager arm of T-cell bispecifics. "
            "Sort gates include a soluble CD3 competition step. "
            "Yeast display is the fast triage; mammalian display catches "
            "epsilon/gamma assembly failures earlier."
        ),
        "typical_campaign_range_usd": {
            "yeast_display": [15000, 40000],
            "mammalian_display": [35000, 100000],
        },
    },
    {
        "target_id": "tgt_vegfa_v1",
        "name": "VEGF-A (VEGF-165 isoform)",
        "official_symbol": "VEGFA",
        "uniprot_id": "P15692",
        "antigen_form": "Recombinant soluble homodimer, biotinylated",
        "antigen_sequence_stub": "APMAEGGGQNHHEVVKFMDVYQRSYCHPIETLVDIFQEYPDEIEYIFKPSCVPLMRCGGCCNDEGLECVPTEESNITMQIMRIKPHQGQHIGEMSFLQHNKCECRPKKDRARQENPCGPCSERRKHLFVQDPQTCKCSCKNTDSRCKARQLELNERTCRCDKPRR",
        "supported_experiment_types": ["yeast_display", "mammalian_display"],
        "indication_area": "neovascular",
        "calibration_notes": (
            "Bevacizumab-class triage panel available. "
            "Sort gates use VEGFR2 competition to anchor on the "
            "therapeutic-relevant epitope, not the dimerisation face."
        ),
        "typical_campaign_range_usd": {
            "yeast_display": [13000, 35000],
            "mammalian_display": [30000, 88000],
        },
    },
    {
        "target_id": "tgt_tnfa_v1",
        "name": "TNF-alpha (soluble homotrimer)",
        "official_symbol": "TNF",
        "uniprot_id": "P01375",
        "antigen_form": "Recombinant soluble homotrimer, biotinylated",
        "antigen_sequence_stub": "VRSSSRTPSDKPVAHVVANPQAEGQLQWLNRRANALLANGVELRDNQLVVPSEGLYLIYSQVLFKGQGCPSTHVLLTHTISRIAVSYQTKVNLLSAIKSPCQRETPEGAEAKPWYEPIYLGGVFQLEKGDRLSAEINRPDYLDFAESGQVYFGIIAL",
        "supported_experiment_types": ["yeast_display", "mammalian_display", "dms"],
        "indication_area": "inflammation",
        "calibration_notes": (
            "Adalimumab-class triage panel. "
            "Yeast display works on soluble homotrimer; the membrane "
            "form is a separate target id (not in alpha catalogue). "
            "DMS around the TNFR1 contact face is supported."
        ),
        "typical_campaign_range_usd": {
            "yeast_display": [13000, 36000],
            "mammalian_display": [30000, 90000],
            "dms": [20000, 60000],
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def list_catalog() -> list[dict]:
    """Return the full catalogue as the API exposes it.

    Returns shallow copies so callers cannot mutate the module-level
    CATALOG by accident.
    """
    return [dict(t) for t in CATALOG]


def get_target(target_id: str) -> Optional[dict]:
    """Look up a single target by id. ``None`` if unknown."""
    if not target_id or not isinstance(target_id, str):
        return None
    for entry in CATALOG:
        if entry["target_id"] == target_id:
            return dict(entry)
    return None


def supports_experiment_type(entry: dict, experiment_type: str) -> bool:
    """Return True if the entry has a calibration for ``experiment_type``.

    The catalogue field is ``supported_experiment_types``; bands live on
    ``typical_campaign_range_usd``. Both must agree, otherwise the entry
    is malformed and we treat the experiment as unsupported.
    """
    supported = entry.get("supported_experiment_types") or []
    bands = entry.get("typical_campaign_range_usd") or {}
    return experiment_type in supported and experiment_type in bands


def cost_band(entry: dict, experiment_type: str) -> Optional[list[int]]:
    """Return ``[low, high]`` USD band for the entry+assay, or ``None``.

    Returns ``None`` when the entry doesn't support the experiment type.
    """
    bands = entry.get("typical_campaign_range_usd") or {}
    band = bands.get(experiment_type)
    if not band or len(band) != 2:
        return None
    return [int(band[0]), int(band[1])]


def supported_experiment_types(entry: dict) -> list[str]:
    """List the experiment types the entry is calibrated for."""
    return list(entry.get("supported_experiment_types") or [])
