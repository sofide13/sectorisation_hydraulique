# Hydraulic Sectorisation — QGIS plugin

> **Author: Kouidri Sofiane** (sofihydro@gmail.com) — © 2026 Kouidri Sofiane. License: GNU GPL v2 or later (see `LICENSE`). Any redistribution must retain this notice.

**Version:** 1.1.5 · **QGIS:** 3.16+

Automated hydraulic sectorisation (DMA design) of water supply (AEP) networks. From the pipes, subscribers and reservoirs layers of a QGIS project, the plugin builds hydraulic sectors controlled by configurable design parameters, places isolation valves on every boundary pipe and validates the final pressures with a hydraulic solver.

## Main features

- Multi-source growth of sectors from reservoirs, with an adaptive loop.
- Elevation-based splitting (virtual PRVs) and merging of undersized sectors.
- Sub-tree redistribution and local refinement.
- Strict territory partition based on urban blocks: **one sector = one contiguous territory**, no overlap, no convex hulls; boundaries follow street centre lines.
- Topological base consolidated on the reference hydraulic partition (same number of sectors, checked in the log).
- Isolation valves on every boundary pipe; `vannes_PRV_frontieres` and `vannes_sectorisation` layers.
- Final pressure validation with WNTR/EPANET, before/after comparison of violations and transfers.
- Diagnostics for subscribers and pipes left outside any sector.
- Optional import of buildings, stadiums, hospitals and railways from OpenStreetMap (Overpass API, HTTPS).

## Design parameters

The default ranges below are **configurable design parameters**, not a claim of compliance with a standard:

| Parameter | Default range |
|---|---|
| Service pressure | 10–60 m |
| Pipe velocity | 0.3–2 m/s |
| Subscribers per sector | 500–3000 |

A lower bound below 500 subscribers must be justified and documented for the network concerned.

## Requirements

Install the Python packages **in the same Python as QGIS**:

```text
python -m pip install -r requirements.txt
```

On Windows (OSGeo4W):

```text
C:\OSGeo4W\bin\python-qgis.bat -m pip install wntr networkx
```

Check in the QGIS Python console:

```python
import wntr
print(wntr.__version__)
```

Without `wntr`, the hydraulic validation status is explicitly reported as `INDISPONIBLE` (unavailable) and only geometric/topological diagnostics are produced. The result must then not be presented as solver-validated. `WNTR/EPANETSimulator` is used first, `WNTRSimulator` only as a fallback.

## Installation

*Plugins → Manage and Install Plugins → Install from ZIP*, then select the plugin ZIP. Restart QGIS completely after an update.

## Usage

1. Open a QGIS project containing the pipes, subscribers and reservoirs layers.
2. The side panel opens on the right.
3. Click **Modify configuration**:
   - **Essential parameters** tab: layer names, pressures, subscriber ranges, elevation splitting.
   - **Advanced** tab: every engine parameter.
4. Load or save your configuration as JSON (📂 / 💾 buttons).
5. Click **Run sectorisation**; the log shows the progress.

### Output layers

`secteurs_hydrauliques`, `secteurs_limites`, `frontieres_secteurs`, `secteurs_hydrauliques_topo`, `conduites_sectorisees`, `vannes_PRV_frontieres`, `vannes_sectorisation`, `reservoirs`, `diagnostic_reservoirs_viables`, `abonnes_pression_hors_plage`, `pressions_wntr`, `abonnes_hors_secteur`, `conduites_hors_secteur`.

## Configuration

`config_defaut.json` holds every parameter, grouped by section (`COUCHES`, `CHAMPS`, `HYDRAULIQUE`, `PLAGES_SECTEUR`, `ALTIMETRIQUE`, `VITESSES`, `BOUCLE_ADAPTATIVE`, `REDISTRIBUTION`, `RAFFINEMENT`, `PARTITION`, `DECOUPE_ROUTES`, …). Create several JSON files for different projects or scenarios and load them with the 📂 button. A configuration saved with an older version is completed automatically when loaded.

Territory-contiguity parameters (`PARTITION` section):

| Key | Default | Role |
|---|---|---|
| `ASSURER_CONTIGUITE_TERRITOIRES` | `true` | Reduces every sector to one contiguous territory |
| `AIRE_EXCLAVE_TOLEREE_M2` | `0.0` | If > 0, detached parts of at least this area are kept |
| `MAX_PASSES_CONTIGUITE` | `60` | Maximum iterations |
| `DIST_MAX_ILOT_SANS_NOEUD_M` | `300.0` | Maximum attachment distance of a block with no network node (`0` = no limit) |

## Project structure

```
sectorisation_hydraulique/
├── __init__.py, plugin_main.py, metadata.txt, config_defaut.json
├── LICENSE, CITATION.cff, README.md, REFERENCES.md
├── core/      engine, partition, topology graph, WNTR validation, configuration
├── ui/        configuration dialog and side panel
├── outils/    OpenStreetMap import, installation checker
├── tests/     tests runnable outside QGIS (shapely backend)
└── resources/ toolbar icons
```

## Tests

Outside QGIS, from the plugin folder: `python3 tests/test_partition_synthetic.py` (and the other `tests/test_*.py` files).

## Troubleshooting

- **Layers not found** → check the layer names in the *Essential parameters* tab.
- **CRS error** → make sure all layers use the same projected CRS.
- **Missing module** → the whole plugin folder must be installed; restart QGIS.

## Changelog (summary)

- **1.1.x** — Configurable design thresholds, WNTR validation of final pressures, before/after comparison, isolation valves on boundary pipes, diagnostics, HTTPS-only network access, relative imports, toolbar icons.
- **1.0.0** — Contiguous territories: one sector = one territory; scattered pieces handed back to the neighbouring sector with the longest shared boundary; blocks without network nodes beyond a configurable distance join no sector.
- **0.9.0** — Sector count consolidated on the reference hydraulic partition; dead code removed.
- **0.8.0** — Strict urban-block partition without overlap; iterative topological cleaning.
- **0.1** — Initial version.

See `REFERENCES.md` for the methodological references.

## Citation and contact

See `CITATION.cff`. Kouidri Sofiane — sofihydro@gmail.com
