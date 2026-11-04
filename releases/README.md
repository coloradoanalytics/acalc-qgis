# Acalc for QGIS releases

Download the versioned plugin installer and keep it zipped. In QGIS 3.34–3.99,
use **Plugins → Manage and Install Plugins → Install from ZIP**. QGIS supplies
Python/GDAL; no extra Python packages are required.

| Version | Plugin installer | Verification and installation |
| --- | --- | --- |
| **0.8.4 (current)** | [acalc-qgis-0.8.4.zip](https://github.com/coloradoanalytics/acalc-qgis/raw/refs/heads/main/releases/0.8.4/acalc-qgis-0.8.4.zip) | [Install notes](0.8.4/README.md) · [SHA256SUMS](0.8.4/SHA256SUMS) · [BUILD-INFO.json](0.8.4/BUILD-INFO.json) |

The `releases/<version>/` directory contains the versioned artifact,
`SHA256SUMS`, `BUILD-INFO.json` and installation README. Only the latest version
is kept here; earlier versions remain in Git history. There are no duplicate
`latest` or root download files.

The installer contains the native `acalc/` plugin folder. Repository source ZIPs
are for development and are not QGIS plugin installers. Model editing works
offline; calculations need the separate [Acalc Engine service](https://github.com/coloradoanalytics/acalc-engine/tree/main/releases)
and [ISO calculation engine](https://github.com/coloradoanalytics/engine-iso/tree/main/releases).
See [complete setup](../README.md#install-and-run-acalc).
