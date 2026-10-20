# ISO/TR 17534-3 example projects (T01–T19)

These are the 19 test cases of **ISO/TR 17534-3:2015** as ready-to-open Acalc
QGIS projects, distributed in `acalc-engine/examples/iso17534-3/` (previously
in `engine-iso`). Each project includes the standard's published expected result at
receiver R as **Measurement** objects, so you can calculate the case yourself
and see how close Acalc gets.

The projects contain no calculated results. You produce those by running the
calculation.

## Open and compare a case

1. Copy this folder to wherever you want to work. Keep each `Txx.qgz` beside
   its `Txx.gpkg`; the project opens its GeoPackage by relative path.
2. In QGIS with the Acalc plugin enabled, open `Txx.qgz`.
3. In **Acalc Settings**, choose your Acalc calculation service. No service
   address or key is stored in these projects.
4. Open the **Calculation** page and calculate the case's variant (named after
   the case, e.g. `T08`). Every model has a single variant, which already holds
   the case's weather, ground and reflection settings.
5. Open **Receivers → Measurements**, select the layer
   *Published reference levels · ISO/TR 17534-3 (1996 method)*, open either
   object's properties, choose **Measured vs calculated** and select the
   variant. The tab shows the published level, the calculated level and
   *calculated − measured* (positive means Acalc is higher). The global
   **Results** list also shows both rows; double-click opens the properties.

Each case has two co-located reference objects at receiver R:

| Object | Holds | Use it to compare |
| --- | --- | --- |
| `Txx R - published dBA total (1996)` | The exact published A-weighted total only | dBA |
| `Txx R - published unweighted octaves; dBA derived` | The published unweighted octave levels, 63 Hz–8 kHz | Octave bands. Its dBA is derived from the rounded bands, so it can differ slightly from the published total. |

The total is kept as a separate object because the plugin recalculates a
Measurement's dBA from its bands whenever bands are present. T10's printed
unweighted bands are only given to 0.1 dB, for example.

**T19** publishes only A-weighted bands. Its octave object is named
`T19 R - converted unweighted octaves from published dBA bands; dBA derived`
and holds the published A-weighted bands minus the A corrections
(−26.2, −16.1, −8.6, −3.2, 0, +1.2, +1.0, −1.1 dB). Those unweighted values
are not printed in the report.

Each object's **notes** give the source table, the tolerance and the
original published values, exactly as printed. The **1996 reference values**
table layer lists the same published bands and Z/A totals.

The comparison tab rounds to 0.1 dB. To check against the report's ±0.05 dB
tolerance, read the full-precision values from the reference tables and the
calculation results.

## Cases

| Case | Scene | Published dBA at R | Table |
| --- | --- | --- | --- |
| [T01](T01.qgz) | Reflecting ground | 44.29 | 4 |
| [T02](T02.qgz) | Mixed ground | 41.53 | 5 |
| [T03](T03.qgz) | Porous ground | 39.14 | 6 |
| [T04](T04.qgz) | Spatially varying ground | 42.23 | 9 |
| [T05](T05.qgz) | Spatially varying ground · alternative method | 39.30 | 10 |
| [T06](T06.qgz) | Terrain and spatially varying ground | 40.59 | 15 |
| [T07](T07.qgz) | Terrain · alternative method | 39.75 | 18 |
| [T08](T08.qgz) | Long barrier and spatially varying ground | 32.48 | 21 |
| [T09](T09.qgz) | Short barrier and spatially varying ground | 32.93 | 23 |
| [T10](T10.qgz) | Terrain and short barrier | 29.30 | 27 |
| [T11](T11.qgz) | Cubic building · low receiver | 41.30 | 31 |
| [T12](T12.qgz) | Cubic building · high receiver | 43.81 | 34 |
| [T13](T13.qgz) | Polygonal building · low receiver | 42.71 | 39 |
| [T14](T14.qgz) | Terrain and polygonal building | 25.38 | 45 |
| [T15](T15.qgz) | Polygonal building · high receiver | 49.92 | 49 |
| [T16](T16.qgz) | Three buildings | 32.54 | 54 |
| [T17](T17.qgz) | Three buildings · alternative positions | 32.72 | 57 |
| [T18](T18.qgz) | Complex building with backyard | 34.89 | 63 |
| [T19](T19.qgz) | Terrain and reflecting barrier | 42.00 | 68 |

## What the comparison means

The report's expected values were calculated with **ISO 9613-2:1996**. Acalc
calculates **ISO 9613-2:2024** with ISO 9613-1:1993 atmospheric absorption.
The 2024 edition changed the ground, barrier and meteorological equations, so
several cases are expected to differ from the published values by more than the
tolerance. A difference is a reason to investigate the edition effects, not
proof of an error, and agreement is not a conformity claim.

Each project also has a **2024 test expectations** table layer. Its
supplementary limits are the published values adjusted only for independently
evaluated 2024 equation changes. They include later corrections for T09 and T19
that were reported by a software vendor. They are engineering checks, not
ISO-issued 2024 values, and they are not loaded as Measurements.

Inputs follow the report: local Cartesian metres (EPSG:32631 is only a
metre-unit carrier, not a real site), 93 dB unweighted sound power in every
octave as specified in Table 2 (printed page 7),
20 °C, 70 % relative humidity and an assumed 101.325 kPa. Pruning is off;
reflections are off except order one in T19. Each model's description lists
the interpretations made (T10's table cross-references, T14's absolute
elevations, T05/T07's mean-path-height method).

## Provenance and rebuilding

[manifest.json](manifest.json) records the report's identity and hash, the
input fixture
([engine-iso/tests/fixtures/iso17534/cases-full.json](https://github.com/coloradoanalytics/engine-iso/blob/main/tests/fixtures/iso17534/cases-full.json)),
the reviewed source models these were built from and the hash of every file
here. The licensed report itself is not included.

[`../build_iso17534_examples.py`](../build_iso17534_examples.py) rebuilds this
folder from those source models with QGIS's Python and acalc-qgis on
`PYTHONPATH`. It refuses to overwrite an existing folder. Its `--verify` mode
relocates a copy and checks layers, reference values, settings and request
export.

The input fixtures and their provenance remain in the sibling `engine-iso`
repository; they are engine regression inputs, not part of this move. The
manifest's fixture path is relative to the `acalc-engine` repository root.
Rebuilding also requires the reviewed source models identified by the manifest;
opening and calculating the shipped projects does not require those sources.
Public model metadata retains standards identities and hashes, but excludes
local document paths, acquisition notes and saving-account metadata. GeoPackages
are compacted after sanitization to remove obsolete metadata bytes. Geometry,
reference values and Measurements are preserved.

From the `acalc-engine` repository root, verify the shipped projects with:

```sh
../engine-iso/target/release/engine-iso --capabilities > /tmp/iso17534-capabilities.json
PYTHONPATH=../acalc-qgis QT_QPA_PLATFORM=offscreen python3 examples/build_iso17534_examples.py \
  --verify --output examples/iso17534-3 \
  --fixture ../engine-iso/tests/fixtures/iso17534/cases-full.json \
  --capabilities /tmp/iso17534-capabilities.json
```

Use a Python installation with QGIS available. To rebuild into a new folder,
omit `--verify` and `--capabilities`, choose an unused `--output` path, and add
`--provenance ../engine-iso/tests/fixtures/iso17534/provenance.json`,
`--source-models /path/to/reviewed/models` and `--acalc-qgis-revision REVISION`.
