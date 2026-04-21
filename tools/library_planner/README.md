# Yeast Display Library Planner (Prototype)

Scoping calculator for yeast surface display library campaigns. Given a design intent (scaffold, diversified positions, codon scheme, target KD, starting material), returns a structured experimental plan covering library complexity, codon bias warnings, NGS read depth, sort strategy, and feasibility flags.

## Public API

```python
from library_planner.planner import plan_library

plan = plan_library(
    scaffold="VHH",
    diversification_positions=6,
    diversification_scheme="NNK",
    target_kd_nm=10.0,
    starting_material="naive",
    target_coverage=0.90,
)

print(plan["summary"])
# Full structured output under plan["library"], plan["codon_analysis"],
# plan["ngs_depth"], plan["sort_strategy"], plan["feasibility"].
```

Returns a pure-data dict (JSON-serializable) with these sections:

- `inputs` — echo of the provided parameters
- `library` — theoretical size, functional size (stop-codon corrected), AA space, yeast feasibility flag, recommended scheme
- `codon_analysis` — S. cerevisiae codon bias warnings, scheme recommendation
- `ngs_depth` — reads required for 50 / 95 / 99 percent coverage, recommended value, per-round breakdown
- `sort_strategy` — 3 to 4 recommended MACS/FACS rounds with target concentrations, gate widths, methods
- `feasibility` — list of severity-tagged warnings and errors
- `summary` — one-paragraph prose summary

## Inputs

| Parameter | Type | Values |
|-----------|------|--------|
| `scaffold` | str | `scFv`, `VHH`, `Fab`, `DARPin`, `custom` |
| `diversification_positions` | int | >= 1 |
| `diversification_scheme` | str | `NNK`, `NNS`, `NNN`, `trimer` |
| `target_kd_nm` | float | > 0 |
| `starting_material` | str | `naive`, `immunized`, `computational_pool` |
| `target_coverage` | float | in (0, 1), default 0.90 |
| `yeast_transformation_ceiling` | int | default 1e8 |

## CLI

```
python -m library_planner.cli \
  --scaffold VHH --positions 6 --scheme NNK \
  --kd 10 --starting-material naive
```

Add `--summary` for text-only output, `--pretty` for indented JSON.

## Scientific Assumptions

### Codon combinatorics
- NNK (N1N2K3 where K=G/T): 32 codons per position, 20 AA + 1 stop
- NNS (N1N2S3 where S=G/C): 32 codons per position, 20 AA + 1 stop
- NNN: 64 codons per position, 20 AA + 3 stops (~5 percent stop rate)
- Trimer: 20 codons per position, no stops, uniform AA distribution
- Functional size accounts for stop codon dilution: `total^positions * (1 - stop_fraction)^positions`

### Yeast transformation ceiling
Default 1e8 based on high-efficiency LiAc electroporation protocols (Benatuil et al. 2010 and subsequent optimizations). User can override for alternative hosts or optimized pipelines.

### NGS coverage (Poisson)
To sample a library of size L with probability p per variant, required reads R solves `p = 1 - exp(-R/L)`:
- 95 percent coverage: R ~ 3 * L
- 99 percent coverage: R ~ 4.6 * L

### Sort strategy heuristics
- KD titration log-linear from 10x above goal down to 0.1x goal across 3 to 4 rounds
- MACS recommended for round 1 when library exceeds 5e7 cells
- FACS gates 0.1 to 1 percent stringency, tighter in later rounds
- Expected enrichment 50x to 200x per FACS round at steady state

### S. cerevisiae codon bias
Codon frequency tables from published S. cerevisiae usage data (Kazusa GCUA database). Rare codons (<5 percent frequency for the corresponding AA) flagged when NNK/NNS/NNN schemes would sample them. Trimer synthesis sidesteps this by using pre-optimized codons.

## Prototype Limitations

A v1 production tool would add:

- Target-specific KD estimation tied to published literature for the target class
- Actual library QC from previous campaigns (not just published rules of thumb)
- Updated and host-specific codon tables (e.g. Pichia pastoris, HEK293 display, mammalian display)
- Structural context for scaffold-specific diversification rules (Kabat vs. IMGT numbering, loop-length preferences)
- Empirical calibration against Ranomics internal campaign outcomes
- Cost estimation (synthesis, NGS, FACS time)
- Kinetic binding models for more accurate sort yield prediction

## Integration Signature for Flask Wrapper

```python
from library_planner.planner import plan_library

inputs = {
    "scaffold": request.form["scaffold"],
    "diversification_positions": int(request.form["positions"]),
    "diversification_scheme": request.form["scheme"],
    "target_kd_nm": float(request.form["kd_nm"]),
    "starting_material": request.form["starting_material"],
}
plan = plan_library(**inputs)
# render plan dict in results template
```

The module is Flask-free and pure Python. No HTTP, no session, no templates.

## Local Development

```
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m pytest tests/ -v
```

All tests should pass.
