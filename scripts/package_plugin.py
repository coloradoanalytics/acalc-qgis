#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Create a versioned QGIS release; existing different releases need --replace."""
import argparse
from configparser import ConfigParser
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "acalc-qgis"
VERSION = re.compile(r"[0-9][0-9A-Za-z]*(?:[.+-][0-9A-Za-z]+)*")


def download_url(version):
    return (f"https://github.com/coloradoanalytics/{PRODUCT}/raw/refs/heads/main/"
            f"releases/{version}/{PRODUCT}-{version}.zip")


def refresh_index(releases):
    versions = [p.name for p in releases.iterdir() if p.is_dir() and not p.is_symlink()
                and VERSION.fullmatch(p.name) and (p / f"{PRODUCT}-{p.name}.zip").is_file()]
    # Numeric ordering puts 0.8.10 above 0.8.2, and stable versions above
    # prerelease labels for the same numeric version.
    def version_key(version):
        core, suffix = re.fullmatch(r"([0-9]+(?:\.[0-9]+)*)(.*)", version).groups()
        label = tuple((1, int(p)) if p.isdigit() else (0, p)
                      for p in re.split(r"(\d+)", suffix) if p)
        return tuple(map(int, core.split("."))), not suffix or suffix.startswith("+"), label
    versions.sort(key=version_key, reverse=True)
    rows = []
    for i, version in enumerate(versions):
        title = f"**{version} (current)**" if i == 0 else version
        rows.append(f"| {title} | [{PRODUCT}-{version}.zip]({download_url(version)}) | "
                    f"[Install notes]({version}/README.md) · [SHA256SUMS]({version}/SHA256SUMS) · "
                    f"[BUILD-INFO.json]({version}/BUILD-INFO.json) |")
    content = """# Acalc for QGIS releases

Download the versioned plugin installer and keep it zipped. In QGIS 3.34–3.99,
use **Plugins → Manage and Install Plugins → Install from ZIP**. QGIS supplies
Python/GDAL; no extra Python packages are required.

| Version | Plugin installer | Verification and installation |
| --- | --- | --- |
""" + "\n".join(rows) + """

Each `releases/<version>/` directory contains its versioned artifact,
`SHA256SUMS`, `BUILD-INFO.json` and installation README. Packaging refreshes this
index, listing the newest version first and retaining earlier version directories.
There are no duplicate `latest` or root download files.

The installer contains the native `acalc/` plugin folder. Repository source ZIPs
are for development and are not QGIS plugin installers. Model editing works
offline; calculations need the separate [Acalc Engine service](https://github.com/coloradoanalytics/acalc-engine/tree/main/releases)
and [ISO calculation engine](https://github.com/coloradoanalytics/engine-iso/tree/main/releases).
See [complete setup](../README.md#install-and-run-acalc).
"""
    index = releases / "README.md"
    if not index.exists() or index.read_text() != content:
        with tempfile.NamedTemporaryFile(mode="w", dir=releases, delete=False) as temporary:
            temporary.write(content)
            path = Path(temporary.name)
        try:
            os.replace(path, index)
        finally:
            path.unlink(missing_ok=True)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def plugin_files(root):
    plugin = root / "acalc"
    # Explicit selection excludes caches, credentials and local project data.
    files = {f"acalc/{path.name}": path for path in plugin.iterdir()
             if path.is_file() and not path.name.startswith(".")
             and (path.suffix in (".py", ".svg") or path.name == "metadata.txt")}
    files.update({f"acalc/{name}": root / name
                  for name in ("README.md", "LICENSE", "COPYRIGHT", "CONTRIBUTING.md")})
    for name in ("USER_GUIDE.md", "GEOPACKAGE.md", "API.md", "CADNAA_EXPORT.md"):
        files[f"acalc/{name}"] = root / "docs" / name
    return files


def source_state(input_hashes):
    """Identify exact packaged source independently of Git history."""
    manifest = json.dumps(input_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"url": "https://github.com/coloradoanalytics/acalc-qgis/tree/main",
            "scope": "packaged files listed in inputs_sha256",
            "tree_sha256": sha256(manifest), "tree_hash_format": "canonical-inputs-json-v1"}


def install_readme(version, filename):
    return f"""# Acalc for QGIS {version}

Download [{filename}]({download_url(version)}); keep it zipped. In QGIS 3.34–3.99, use
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
"""


def package(root=ROOT, *, replace=False):
    root = Path(root).resolve()
    metadata = ConfigParser(interpolation=None)
    metadata.read(root / "acalc" / "metadata.txt")
    version = metadata["general"]["version"]
    # Raw package version, no v prefix or path/URL components. Accept ordinary
    # dotted versions and prerelease/build labels without permitting traversal.
    if not VERSION.fullmatch(version):
        raise ValueError("Invalid plugin version: use a raw version such as 0.8.2, without a path or v prefix.")
    releases = root / "releases"
    destination = releases / version
    if releases.is_symlink() or destination.is_symlink():
        raise ValueError("Release directories must not be symlinks.")
    filename = f"{PRODUCT}-{version}.zip"
    files = plugin_files(root)
    inputs = {name: path.read_bytes() for name, path in sorted(files.items())}
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in inputs.items():
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    payload = buffer.getvalue()
    input_hashes = {name: sha256(data) for name, data in inputs.items()}
    source = source_state(input_hashes)
    checksum = f"{sha256(payload)}  {filename}\n".encode()
    readme = install_readme(version, filename).encode()
    expected = {filename, "SHA256SUMS", "BUILD-INFO.json", "README.md"}
    if destination.exists():
        if not destination.is_dir() or {p.name for p in destination.iterdir()} != expected:
            raise ValueError("Existing release has unexpected or incomplete contents; leave it intact and inspect it first.")
        if any((destination / name).is_symlink() for name in expected):
            raise ValueError("Release files must not be symlinks.")
        if any(not (destination / name).is_file() for name in expected):
            raise ValueError("Release entries must be regular files.")
        same = ((destination / filename).read_bytes() == payload
                and (destination / "SHA256SUMS").read_bytes() == checksum
                and (destination / "README.md").read_bytes() == readme)
        try:
            existing = json.loads((destination / "BUILD-INFO.json").read_text())
            valid = (existing["product"] == PRODUCT and existing["version"] == version
                     and existing["artifact"] == filename and existing["sha256"] == sha256(payload)
                     and existing["inputs_sha256"] == input_hashes
                     and existing["source"] == source
                     and existing["packager_sha256"] == sha256(Path(__file__).read_bytes()))
        except (ValueError, KeyError, TypeError):
            valid = False
        if same and valid and not replace:
            refresh_index(releases)
            print(f"Validated existing release (unchanged): {destination}")
            return destination
        if not replace:
            raise ValueError(f"Release {version} differs from the current package. Choose a new version or explicitly use --replace.")
    info = {"product": PRODUCT, "version": version, "artifact": filename, "sha256": sha256(payload),
            "source": source, "inputs_sha256": input_hashes,
            "packager_sha256": sha256(Path(__file__).read_bytes()),
            "tools": {"python": platform.python_version(), "zlib": zlib.ZLIB_VERSION},
            "archive": {"plugin_directory": "acalc", "fixed_timestamp": "1980-01-01T00:00:00",
                        "compression": "deflate", "compression_level": 9}}
    outputs = {filename: payload, "SHA256SUMS": checksum, "README.md": readme,
               "BUILD-INFO.json": (json.dumps(info, indent=2, sort_keys=True) + "\n").encode()}
    releases.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{version}-", dir=releases) as temporary:
        staging = Path(temporary)
        for name, data in outputs.items():
            (staging / name).write_bytes(data)
        if destination.exists():
            for name in sorted(outputs):
                os.replace(staging / name, destination / name)
        else:
            staging.rename(destination)
    refresh_index(releases)
    print(f"Packaged release: {destination}")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace", action="store_true", help="explicitly replace this version's release; never remove other versions")
    args = parser.parse_args()
    try:
        package(replace=args.replace)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
