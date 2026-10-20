# Acalc for QGIS

**[Releases and downloads](releases/README.md)** · [QGIS plugin 0.8.3 ZIP](https://github.com/coloradoanalytics/acalc-qgis/raw/refs/heads/main/releases/0.8.3/acalc-qgis-0.8.3.zip) · [Complete setup](#install-and-run-acalc)

Acalc is a QGIS plugin for authoring outdoor acoustical models and calculating
receiver sound levels through a separately running HTTP calculation engine.
The model lives in a single GeoPackage, independent of the surrounding QGIS
map. QGIS supplies map navigation, digitizing, snapping and undo; Acalc supplies
the model vocabulary, coordinate and property editors, variants, spectra and
result layers.

> **Status: beta (0.8.3).** The plugin is marked experimental in QGIS. Model
> formats and the calculation API may still change between releases, and
> calculation results are not certified for regulatory use.

## Install and run Acalc

For local calculations, install three parts: **acalc-qgis** provides the QGIS
interface, **acalc-engine** runs the local service and web interface, and
**engine-iso** performs the ISO calculations. You do not need Git or a compiler.

### 1. Install the QGIS plugin

Use QGIS **3.34 or later in the 3.x series**, with its bundled Python and GDAL.
The plugin needs no additional Python packages. Model editing works offline;
the precompiled calculation programs below are for Linux and Windows x86-64
(Intel/AMD 64-bit computers).

1. Download **[acalc-qgis-0.8.3.zip](https://github.com/coloradoanalytics/acalc-qgis/raw/refs/heads/main/releases/0.8.3/acalc-qgis-0.8.3.zip)**. Keep this file zipped.
   Use this installer link, rather than GitHub's **Code → Download ZIP** source archive.
2. In QGIS, open **Plugins → Manage and Install Plugins → Install from ZIP**,
   select `acalc-qgis-0.8.3.zip`, and click **Install Plugin**.
3. Enable **Acalc** under **Installed**, then open **Plugins → Acalc → Acalc**
   or click the Acalc toolbar icon.

If QGIS reports “File is not a zip file”, use the direct download link above.
Saving a GitHub file-view page can save HTML instead of the plugin installer.

### 2. Download and start the calculation programs

Download **both archives in the row for your computer**. The [service releases](https://github.com/coloradoanalytics/acalc-engine/tree/main/releases) and [ISO engine releases](https://github.com/coloradoanalytics/engine-iso/tree/main/releases) list versions, checksums and build provenance:

| Computer | Acalc Engine service | ISO calculation engine |
| --- | --- | --- |
| Linux x86-64 | [acalc-engine-0.1.2-linux-x86_64.tar.gz](https://github.com/coloradoanalytics/acalc-engine/raw/refs/heads/main/releases/0.1.2/acalc-engine-0.1.2-linux-x86_64.tar.gz) | [engine-iso-0.1.0-linux-x86_64.tar.gz](https://github.com/coloradoanalytics/engine-iso/raw/refs/heads/main/releases/0.1.0/engine-iso-0.1.0-linux-x86_64.tar.gz) |
| Windows x86-64 | [acalc-engine-0.1.2-windows-x86_64.zip](https://github.com/coloradoanalytics/acalc-engine/raw/refs/heads/main/releases/0.1.2/acalc-engine-0.1.2-windows-x86_64.zip) | [engine-iso-0.1.0-windows-x86_64.zip](https://github.com/coloradoanalytics/engine-iso/raw/refs/heads/main/releases/0.1.0/engine-iso-0.1.0-windows-x86_64.zip) |

1. Extract the **acalc-engine service archive** into a writable location. Open
   the extracted service folder containing `acalc-engine` (or `acalc-engine.exe`)
   and its empty `engines/` directory.
2. Extract the entire **engine-iso archive** directly into that `engines/`
   directory, keeping the archive's root folder and license files. The resulting
   paths, relative to the service folder, are:

   | Linux | Windows |
   | --- | --- |
   | `acalc-engine` | `acalc-engine.exe` |
   | `engines/engine-iso-0.1.0-linux-x86_64/engine-iso-0.1.0` | `engines/engine-iso-0.1.0-windows-x86_64/engine-iso-0.1.0.exe` |

3. On Windows, open `acalc-engine.exe`. On Linux, open a terminal in that folder
   and run `chmod +x acalc-engine engines/engine-iso-0.1.0-linux-x86_64/engine-iso-0.1.0`,
   then `./acalc-engine`.
   Keep the console open while using Acalc; press **Ctrl+C** to stop it.
   Startup discovers engines in the extracted subfolders automatically. Launching
   the service by its full path from another working directory also works: it
   uses the `engines/` directory beside its executable. Keep the engine's
   downloaded filename and let the service start it.
4. Open <http://127.0.0.1:8342> in a browser. **Installed engines** should show
   ISO as ready. If you add an engine archive after startup, restart the service
   to discover it. Existing configured engine selections are preserved; if
   multiple versions are installed, choose one explicitly on the **Engines** page.

The web interface is included in the service executable. Node.js, Go and Rust
are not required to run these downloads. See the [service guide](https://github.com/coloradoanalytics/acalc-engine#download-and-run)
and [engine guide](https://github.com/coloradoanalytics/engine-iso#download-and-install) for checksums,
build details and alternative configuration. If ports 8342/8343 are already
in use, stop the other instance or configure different ports.

### 3. Connect QGIS and calculate

In the Acalc panel, open **☰ → Settings**, select **Local engine** in the
connection dropdown, then choose **Test connection**. It uses
`http://127.0.0.1:8343` without a client key. Port **8342** is the browser interface; port **8343**
is the calculation API used by QGIS.

Open or create an Acalc model, choose a variant on the **Calculation** page,
and run the calculation. The [user guide](docs/USER_GUIDE.md) covers model
creation and reviewing results. The plugin saves completed results to the model;
retrieve results before stopping the service, which keeps jobs in memory.

### 4. Try a reference model

The service repository includes [19 ISO/TR 17534-3 examples](https://github.com/coloradoanalytics/acalc-engine/tree/main/examples/iso17534-3)
with published expected results stored as Measurements. To get them without Git,
[download the service repository ZIP](https://github.com/coloradoanalytics/acalc-engine/archive/refs/heads/main.zip),
extract it, and open `acalc-engine-main/examples/iso17534-3/T01.qgz` in QGIS.
Keep every `.qgz` beside its matching `.gpkg`. Follow the examples' README to
calculate and compare; it explains the differences between the 1996 reference
results and the engine's 2024 method.

## Features

- Point, line and area sources; point and area receivers; measurements.
- Buildings, source buildings with wall- and roof-mounted sources, barriers,
  terrain points and lines, ground absorption areas and dense foliage.
- Terrain generated from elevation rasters, plus raster sampling of object
  ground elevations.
- Project catalogs of octave-band sound power and absorption spectra, and
  directivity authoring.
- Variants (scenarios) built from model layers.
- Import from QGIS vector layers and OpenStreetMap buildings.
- Calculations to ISO 9613-2:2024 through a configurable calculation API,
  with saved, provenance-tracked results, area-receiver contour surfaces, and
  measured-versus-calculated comparisons.
- Back-calculation of source sound power from measurements.
- Export to CadnaA 2021 (QSI) and to GIS shapefiles.

## Connecting to Acalc Engine

The plugin does not calculate sound levels itself. It sends model snapshots to
**[Acalc Engine](https://github.com/coloradoanalytics/acalc-engine)**, which is distributed separately, and saves the completed
results. Acalc Engine serves the calculation API and runs each calculation on
one of its engines, such as its ISO 9613-2:2024 engine. The community service runs on
your own computer. Shared remote services use Acalc Engine Pro.

Connections are managed in the Acalc panel under **☰ → Settings**:

- **Local:** for Acalc Engine on the same computer. This is the default
  connection, at `http://127.0.0.1:8343`, and needs no key with Acalc Engine's
  default settings. Start Acalc Engine, then choose **Test connection**.
- **Remote (Acalc Engine Pro):** for Acalc Engine on another computer. Its administrator configures
  it to accept network connections and creates a client key on its admin pages.
  Choose **☰ → New engine…**, then **Remote**, enter the instance's base URL (for example
  `http://<server-address>:8343`) and the client key, save, then choose **Test
  connection**. The key is stored only in QGIS's encrypted authentication
  database, never in the project.

Remote connections support HTTP on the LAN and HTTPS for public services. You can
save several named connections using the adjacent **☰** menu; **Edit engine…**
opens the connection editor and **Delete engine…** asks for confirmation. The
editor can test draft changes before saving; **Cancel** leaves the saved connection
and key unchanged. **Test connection** checks health, access and each installed
engine, then shows one row per calculation method with its installed version.
Choose the engine when starting a calculation; see
the [user guide](docs/USER_GUIDE.md#calculate-receiver-levels) for details.
The request and result contract for ISO 9613-2 calculations is documented in
[docs/API.md](docs/API.md).

## Documentation

- [User guide](docs/USER_GUIDE.md): building a model, spectra, calculation,
  results and import/export.
- [GeoPackage schema](docs/GEOPACKAGE.md): the model file format, for
  engine-independent readers and writers.
- [Calculation API](docs/API.md): the HTTP/JSON contract with calculation engines.
- [CadnaA export](docs/CADNAA_EXPORT.md): export support and verification limits.

## Development

### Packaging

Create a release with the standard-library-only command:

```bash
python3 scripts/package_plugin.py
```

The metadata version selects `releases/<version>/acalc-qgis-<version>.zip`.
That directory also receives `SHA256SUMS`, `BUILD-INFO.json` (exact packaged file
hashes, their combined tree hash, source location and packaging tools), and an
installation README. Commit the complete version directory and the automatically
refreshed [releases/README.md](releases/README.md), which lists all release versions
with the newest first and direct download links.
Older version directories remain untouched; no root or `dist/` download copy is
created. Keep the native `acalc/` installation folder and fixed archive metadata.
When adding runtime file types or subdirectories, update the script’s explicit
file selection.

An identical rerun validates the existing files and leaves them unchanged.
Different input for an existing version is rejected. Normally bump the metadata
version to add a release; to intentionally correct that version during review,
use `python3 scripts/package_plugin.py --replace`. This explicit operation only
replaces that version’s four known files and never deletes other versions.
Versions must be raw package versions such as `0.8.3`, without a `v` prefix or
path components. Provenance works without Git history: `inputs_sha256` maps
archive paths to file SHA-256 hashes. `source.tree_sha256` hashes that map encoded
as UTF-8 JSON with sorted keys and separators `(',', ':')`
(`canonical-inputs-json-v1`). It identifies packaged files, not the entire
repository or a future commit. Source is included in the installer and available
on [GitHub main](https://github.com/coloradoanalytics/acalc-qgis/tree/main).
Ordinary reruns retain the original validated build record.

### Developer installation

Link the plugin into a QGIS profile:

```bash
python3 scripts/install_dev.py
# or, for another profile:
python3 scripts/install_dev.py --plugins-dir /path/to/profile/python/plugins
```

The installer never replaces an existing folder or link. Restart QGIS and enable
**Acalc** under **Plugins → Manage and Install Plugins → Installed**. Plugin
Reloader can reload `acalc` during development.

### Tests

Run the tests with a Python interpreter that can import `qgis` and `osgeo`
(on Linux, usually the system `python3` that QGIS uses):

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -v
```

The tests use real QGIS vector layers, GDAL GeoPackages, a map canvas and the
actual dock; only the QGIS application interface is substituted. API tests use
temporary localhost HTTP servers. An optional end-to-end back-calculation test
runs against a locally built ISO 9613-2 engine binary:

```bash
QT_QPA_PLATFORM=offscreen ACALC_ISO_BINARY=/path/to/iso-rust \
  python3 -m unittest tests.test_engine_back_calculation -v
```

A smoke test runs the plugin inside a real QGIS application. Run it only in its
own process and profile; it exits QGIS when finished and writes results and a
screenshot to `/tmp/acalc-qgis-smoke/`:

```bash
QT_QPA_PLATFORM=offscreen ACALC_QGIS_REPO="$PWD" timeout 30s qgis \
  --nologo --noversioncheck --noplugins \
  --profiles-path /tmp/acalc-qgis-smoke-profile --profile smoke \
  --code "$PWD/scripts/smoke_qgis.py"
```

### Code layout

The plugin package is `acalc/`. Principal modules:

- `plugin.py`: QGIS lifecycle, layer groups, selection and editing.
- `dock.py` / `dialogs.py`: the Acalc panel, feature properties and coordinate entry.
- `storage.py`: GeoPackage creation, validation, metadata and variants.
- `schema.py`: shared types and identity helpers, without QGIS imports.
- `layers.py`: QGIS styling, coordinate encoding and feature validation.
- `spectra.py` / `spectra_ui.py`: octave-band data, project catalogs and editor.
- `api_connections.py` / `api_settings_ui.py`: named engine connections and capability discovery.
- `api_client.py` / `engine_runner.py`: asynchronous HTTP requests and the calculation lifecycle.
- `api_model.py`: normalized model snapshots and atomic result persistence.
- `calculation_settings.py` / `calculation_ui.py`: calculation settings and saved results.
- `raster_terrain.py` / `raster_elevation.py`: adaptive terrain triangulation and raster sampling.
- `building_preview.py`: interactive 3D building display.
- `map_actions.py` / `digitizing.py`: map context actions and geometry capture.
- `model_export.py`: CadnaA and shapefile export.

## Contributions

Contributions are accepted under **GPL-3.0-or-later**, the same license as this
project. See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution terms.

## License

Copyright © 2026 Colorado Analytics.

Acalc for QGIS is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See [LICENSE](LICENSE) for details.
