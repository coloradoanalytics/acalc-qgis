# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Versioned plugin packaging without QGIS or external dependencies."""
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.package_plugin import package


class PackagePluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "acalc").mkdir()
        (self.root / "docs").mkdir()
        self.set_version("0.8.2")
        for name, content in (("__init__.py", "def classFactory(iface): pass\n"),
                              ("icon.svg", "<svg/>\n"), (".env", "must not ship"),
                              ("user.gpkg", "must not ship")):
            (self.root / "acalc" / name).write_text(content)
        for name in ("README.md", "LICENSE", "COPYRIGHT", "CONTRIBUTING.md"):
            (self.root / name).write_text(name + "\n")
        for name in ("USER_GUIDE.md", "GEOPACKAGE.md", "API.md", "CADNAA_EXPORT.md"):
            (self.root / "docs" / name).write_text(name + "\n")

    def set_version(self, version):
        (self.root / "acalc" / "metadata.txt").write_text(f"[general]\nversion={version}\n")

    def build(self, **options):
        with redirect_stdout(StringIO()):
            return package(self.root, **options)

    @staticmethod
    def snapshot(path):
        return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in path.rglob("*") if p.is_file()}

    def test_release_layout_checksum_provenance_and_installable_folder(self):
        release = self.build()
        name = "acalc-qgis-0.8.2.zip"
        self.assertEqual(release, self.root / "releases" / "0.8.2")
        self.assertEqual({p.name for p in release.iterdir()},
                         {name, "SHA256SUMS", "BUILD-INFO.json", "README.md"})
        payload = (release / name).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        self.assertEqual((release / "SHA256SUMS").read_text(), f"{digest}  {name}\n")
        info = json.loads((release / "BUILD-INFO.json").read_text())
        self.assertEqual((info["product"], info["version"], info["sha256"]),
                         ("acalc-qgis", "0.8.2", digest))
        self.assertNotIn("revision", info["source"])
        self.assertEqual(info["source"]["url"], "https://github.com/coloradoanalytics/acalc-qgis/tree/main")
        canonical = json.dumps(info["inputs_sha256"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(info["source"]["tree_sha256"], hashlib.sha256(canonical).hexdigest())
        with zipfile.ZipFile(release / name) as archive:
            self.assertIsNone(archive.testzip())
            self.assertTrue(all(p.startswith("acalc/") for p in archive.namelist()))
            self.assertIn("acalc/metadata.txt", archive.namelist())
            self.assertIn("acalc/__init__.py", archive.namelist())
            self.assertNotIn("acalc/.env", archive.namelist())
            self.assertNotIn("acalc/user.gpkg", archive.namelist())
            self.assertEqual(info["inputs_sha256"],
                             {p: hashlib.sha256(archive.read(p)).hexdigest() for p in archive.namelist()})
        self.assertFalse((self.root / "acalc-qgis.zip").exists())
        self.assertFalse((self.root / "dist").exists())

    def test_idempotent_rerun_changes_neither_bytes_nor_build_record(self):
        release = self.build()
        before = self.snapshot(release.parent)
        self.build()
        self.assertEqual(self.snapshot(release.parent), before)

    def test_new_versions_refresh_index_with_direct_downloads_and_numeric_order(self):
        first = self.build()
        original = self.snapshot(first)
        self.set_version("0.8.10")
        self.build()
        index = (first.parent / "README.md").read_text()
        self.assertIn("**0.8.10 (current)**", index)
        self.assertLess(index.index("**0.8.10"), index.index("| 0.8.2 |"))
        self.assertIn("https://github.com/coloradoanalytics/acalc-qgis/raw/refs/heads/main/releases/0.8.10/acalc-qgis-0.8.10.zip", index)
        self.assertIn("[SHA256SUMS](0.8.2/SHA256SUMS)", index)
        self.assertEqual(self.snapshot(first), original)
        self.set_version("0.8.10-rc.1")
        self.build()
        index = (first.parent / "README.md").read_text()
        self.assertIn("**0.8.10 (current)**", index)

    def test_changed_release_requires_explicit_replace_and_preserves_other_versions(self):
        release = self.build()
        before = self.snapshot(release)
        self.set_version("0.8.3")
        later = self.build()
        later_before = self.snapshot(later)
        self.set_version("0.8.2")
        (self.root / "acalc" / "__init__.py").write_text("# deliberately changed package\n")
        with self.assertRaisesRegex(ValueError, "--replace"):
            self.build()
        self.assertEqual(self.snapshot(release), before)
        self.build(replace=True)
        self.assertNotEqual(self.snapshot(release), before)
        self.assertEqual(self.snapshot(later), later_before)

    def test_invalid_version_components_never_create_release_paths(self):
        for version in ("../outside", "..", ".", "/tmp/outside", "1/2", "1\\2", "v0.8.2", "1..2", "0.8.2%2foutside"):
            with self.subTest(version=version):
                self.set_version(version)
                with self.assertRaisesRegex(ValueError, "Invalid plugin version"):
                    self.build()
                self.assertFalse((self.root / "releases").exists())

    def test_corrupt_release_does_not_get_silently_replaced(self):
        release = self.build()
        checksum = release / "SHA256SUMS"
        checksum.write_text("bad\n")
        before = self.snapshot(release)
        with self.assertRaisesRegex(ValueError, "--replace"):
            self.build()
        self.assertEqual(self.snapshot(release), before)
        self.build(replace=True)
        self.assertNotEqual(checksum.read_text(), "bad\n")

    def test_discarded_history_provenance_requires_explicit_correction(self):
        release = self.build()
        path = release / "BUILD-INFO.json"
        info = json.loads(path.read_text())
        info["source"] = {"revision": "discarded-history"}
        path.write_text(json.dumps(info))
        with self.assertRaisesRegex(ValueError, "--replace"):
            self.build()
        self.build(replace=True)
        corrected = json.loads(path.read_text())
        self.assertNotIn("revision", corrected["source"])
        self.assertEqual(corrected["source"]["tree_hash_format"], "canonical-inputs-json-v1")

    def test_unexpected_contents_and_symlinks_remain_untouched(self):
        release = self.build()
        extra = release / "owner.txt"
        extra.write_text("preserve me\n")
        before = self.snapshot(release)
        with self.assertRaisesRegex(ValueError, "unexpected"), redirect_stdout(StringIO()):
            self.build(replace=True)
        self.assertEqual(self.snapshot(release), before)
        extra.unlink()
        archive = release / "acalc-qgis-0.8.2.zip"
        archive.unlink()
        outside = self.root / "outside.zip"
        outside.write_bytes(b"preserve me")
        archive.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.build(replace=True)
        self.assertEqual(outside.read_bytes(), b"preserve me")
