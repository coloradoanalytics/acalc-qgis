# Acalc for QGIS 0.8.3

Download [acalc-qgis-0.8.3.zip](https://github.com/coloradoanalytics/acalc-qgis/raw/refs/heads/main/releases/0.8.3/acalc-qgis-0.8.3.zip); keep it zipped. In QGIS 3.34–3.99, use
**Plugins → Manage and Install Plugins → Install from ZIP**, choose that file,
then enable **Acalc**. The installed plugin folder is `acalc/`.
This is the plugin installer, not the repository source ZIP.

Verify the download with `sha256sum -c SHA256SUMS` (or compare its SHA-256 with
[SHA256SUMS](SHA256SUMS)). [BUILD-INFO.json](BUILD-INFO.json) records exact packaged
file hashes, their combined tree hash and packaging tools. Source files are in
the installer and on [GitHub main](https://github.com/coloradoanalytics/acalc-qgis/tree/main).
The hashes identify the packaged snapshot independently of Git history.

Model editing works offline. Calculations also need the separate
[Acalc Engine service](https://github.com/coloradoanalytics/acalc-engine/tree/main/releases)
and [ISO calculation engine](https://github.com/coloradoanalytics/engine-iso/tree/main/releases).
See the bundled user guide or [repository setup guide](../../README.md#install-and-run-acalc).

[All QGIS releases](../README.md)
