# Acalc for QGIS releases

Download the versioned plugin installer and keep it zipped. In QGIS 3.34–3.99,
use **Plugins → Manage and Install Plugins → Install from ZIP**. QGIS supplies
Python/GDAL; no extra Python packages are required.

| Version | Plugin installer | Verification and installation |
| --- | --- | --- |
| **0.8.3 (current)** | [acalc-qgis-0.8.3.zip](https://github.com/coloradoanalytics/acalc-qgis/raw/refs/heads/main/releases/0.8.3/acalc-qgis-0.8.3.zip) | [Install notes](0.8.3/README.md) · [SHA256SUMS](0.8.3/SHA256SUMS) · [BUILD-INFO.json](0.8.3/BUILD-INFO.json) |

Each `releases/<version>/` directory contains its versioned artifact,
`SHA256SUMS`, `BUILD-INFO.json` and installation README. Packaging refreshes this
index, listing the newest version first and retaining earlier version directories.
There are no duplicate `latest` or root download files.

The installer contains the native `acalc/` plugin folder. Repository source ZIPs
are for development and are not QGIS plugin installers. Model editing works
offline; calculations need the separate [Acalc Engine service](https://github.com/coloradoanalytics/acalc-engine/tree/main/releases)
and [ISO calculation engine](https://github.com/coloradoanalytics/engine-iso/tree/main/releases).
See [complete setup](../README.md#install-and-run-acalc).
