# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Real native model / Core hierarchy adapter round trips, isolated owner state."""

import base64
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from osgeo import gdal

from tests.test_model import APP, FakeIface
from tests import test_api_model as api_fixture
from tests.test_api_model import BACKEND, JOB, CAPABILITIES
from acalc.api_model import prepare_calculation, save_completed_calculation
from acalc.plugin import AcalcPlugin
from acalc.portable_project import (MAX_BYTES, Number, decode, encode, export_model,
                                    file_bytes, import_model, models, read)
from acalc.schema import ModelError
from acalc.storage import ModelStore


class PortableProjectTests(unittest.TestCase):
    def setUp(self):
        self.fixture = api_fixture.ApiModelTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = Path(self.fixture.directory.name)
        self.store = self.fixture.store
        self.fixture.scene()
        self.snapshot = self.fixture.prepare()
        save_completed_calculation(self.store, self.snapshot, self.fixture.result(self.snapshot), BACKEND, JOB)
        self.output = self.root / "project.acalc-project.json"

    def exported(self):
        return export_model(self.store, self.output)

    def imported(self, document=None, name="working"):
        document = document if document is not None else self.exported()
        self.output.write_bytes(encode(document))
        return import_model(self.output, self.root / name, models(document)[0]["id"])

    def test_native_bytes_identity_spectra_variants_results_and_api_roundtrip(self):
        native = Path(self.store.path).read_bytes()
        document = self.exported()
        model = models(document)[0]
        self.assertEqual(model["payload"]["model_schema_version"], 15)
        self.assertNotEqual(model["id"], document["project"]["info"]["project_id"])
        imported = self.imported(document)
        self.assertEqual(Path(imported.path).read_bytes(), native)
        self.assertEqual(imported.metadata(), self.store.metadata())
        self.assertEqual(imported.variants(), self.store.variants())
        self.assertEqual(imported.spectra(), self.store.spectra())
        self.assertEqual(imported.receiver_results(self.fixture.variant_id), self.store.receiver_results(self.fixture.variant_id))
        self.assertEqual(prepare_calculation(imported, self.fixture.variant_id, CAPABILITIES)["request"], self.snapshot["request"])
        again = export_model(imported, self.root / "again.acalc-project.json")
        self.assertEqual(len(again["files"]), len(document["files"]))
        reopened = ModelStore.open(imported.path)
        self.assertEqual(Path(reopened.path).read_bytes(), native)

    def test_unrelated_sections_unknown_fields_and_exact_source_numeric_tokens(self):
        document = self.exported()
        # Opaque future payloads are retained; the model adapter does not interpret them.
        document["project"]["calculations"] = decode(b'[{"id":"future","name":"Opaque","payload":{"x":0.123456789012345678901234567890,"n":9007199254740993123456789,"z":-0,"e":1.2300e+9,"source":"{ \\"n\\": 1.00 }"}}]')
        document["project"]["extensions"] = {"another-app": {"future": [None, True]}}
        payload = models(document)[0]["payload"]
        payload["units"]["future"] = "retain"
        payload["metadata"]["other-app"] = "retain"
        payload["extensions"]["acalc-qgis"]["future"] = "retain"
        original = encode(document["project"]["calculations"])
        imported = self.imported(document)
        imported.update_metadata("Edited QGIS model", "A", "D", "NAVD88", {})
        result = export_model(imported, self.root / "edited.acalc-project.json")
        self.assertEqual(encode(result["project"]["calculations"]), original)
        self.assertEqual(result["project"]["extensions"], document["project"]["extensions"])
        self.assertEqual(result["project"]["info"], read(self.output)["project"]["info"])
        self.assertEqual(result["transfer"]["admin"], "omitted")
        self.assertNotIn("admin", result["project"])
        self.assertEqual(models(result)[0]["name"], "Edited QGIS model")
        self.assertEqual(models(result)[0]["payload"]["units"]["future"], "retain")
        self.assertEqual(models(result)[0]["payload"]["metadata"]["other-app"], "retain")
        self.assertEqual(models(result)[0]["payload"]["extensions"]["acalc-qgis"]["future"], "retain")
        self.assertEqual(len(result["files"]), len(document["files"]) + 1)
        # Old content records are immutable and survive references from other sections.
        self.assertEqual(encode(result["files"][0]), encode(read(self.output)["files"][0]))

    def test_elevation_raster_relocation_without_native_path_or_byte_changes(self):
        path = self.root / "heights.tif"
        raster = gdal.GetDriverByName("GTiff").Create(str(path), 2, 2, 1, gdal.GDT_Float32)
        raster.SetProjection(self.store.metadata()["crs_wkt"])
        raster.SetGeoTransform((0, 10, 0, 20, 0, -10))
        raster.GetRasterBand(1).SetUnitType("m")
        raster.GetRasterBand(1).Fill(42)
        raster = None
        ident = self.store.add_elevation_raster(path)
        native = Path(self.store.path).read_bytes()
        document = self.exported()
        path.unlink()
        imported = self.imported(document)
        from acalc.raster_elevation import RasterElevation
        relocated = imported.elevation_rasters()[0]
        self.assertEqual(relocated["raster_id"], ident)
        self.assertTrue(Path(relocated["path"]).is_file())
        self.assertNotEqual(relocated["path"], str(path))
        with RasterElevation(relocated["path"], imported.metadata()["crs_wkt"]) as reader:
            self.assertEqual(reader.elevation(5, 15), 42)
        self.assertEqual(Path(imported.path).read_bytes(), native)
        self.assertEqual(ModelStore.open(imported.path).elevation_rasters(), imported.elevation_rasters())
        export_model(imported, self.root / "relocated.acalc-project.json")

    def test_corrupt_file_is_rejected_before_working_folder_exists(self):
        document = self.exported()
        document["files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ModelError, "hash mismatch"):
            self.imported(document)
        self.assertFalse((self.root / "working").exists())

    def test_native_identity_mismatch_rejected_without_partial_output(self):
        document = self.exported()
        models(document)[0]["payload"]["extensions"]["acalc-qgis"]["native_model_id"] = "wrong"
        with self.assertRaisesRegex(ModelError, "identity/schema"):
            self.imported(document)
        self.assertFalse((self.root / "working").exists())

    def test_missing_native_file_is_not_silently_omitted(self):
        document = self.exported()
        document["files"][0].pop("data")
        document["files"][0]["missing"] = True
        document["transfer"]["files"] = "external"
        with self.assertRaisesRegex(ModelError, "missing"):
            self.imported(document)
        self.assertFalse((self.root / "working").exists())

    def test_missing_unowned_file_is_retained_with_honest_external_transfer(self):
        document = self.exported()
        record = dict(document["files"][0])
        record.update(id="unavailable-report", name="report.pdf", original_name="report.pdf", missing=True)
        record.pop("data")
        document["files"].append(record)
        document["transfer"]["files"] = "external"
        imported = self.imported(document)
        result = export_model(imported, self.root / "external.acalc-project.json")
        self.assertEqual(result["transfer"]["files"], "external")
        self.assertTrue(result["files"][1]["missing"])
        self.assertNotIn("data", result["files"][1])

    def test_saved_result_raster_closure_and_missing_result_failure(self):
        from acalc.area_results import results_directory
        folder = results_directory(self.store)
        folder.mkdir()
        path = folder / "result.tif"
        dataset = gdal.GetDriverByName("GTiff").Create(str(path), 1, 1, 1, gdal.GDT_Float32)
        dataset.GetRasterBand(1).Fill(42)
        dataset = None
        with self.store.connection() as db:
            row = db.execute("SELECT metadata_json FROM acalc_calculation_runs").fetchone()
            metadata = json.loads(row[0])
            metadata["area_rasters"] = [dict(path="result.tif")]
            db.execute("UPDATE acalc_calculation_runs SET metadata_json=?", (json.dumps(metadata),))
        unrelated = folder / "unrelated-owner.txt"
        unrelated.write_text("not a model asset")
        document = self.exported()
        binding = models(document)[0]["payload"]["extensions"]["acalc-qgis"]
        self.assertIn("model_results/result.tif", binding["files"].values())
        self.assertNotIn("model_results/unrelated-owner.txt", binding["files"].values())
        imported = self.imported(document)
        self.assertEqual((results_directory(imported) / "result.tif").read_bytes(), path.read_bytes())
        path.unlink()
        with self.assertRaisesRegex(ModelError, "raster is missing"):
            self.exported()

    def test_portable_open_retains_historical_runs_byte_for_byte(self):
        with self.store.connection() as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(acalc_calculation_runs)")]
            values = list(db.execute("SELECT * FROM acalc_calculation_runs").fetchone())
            values[columns.index("run_id")] = "old-run"
            values[columns.index("finished_at")] = "2000-01-01T00:00:00Z"
            db.execute("INSERT INTO acalc_calculation_runs VALUES (" + ",".join("?" for _ in values) + ")", values)
        native = Path(self.store.path).read_bytes()
        imported = self.imported()
        with imported.connection(readonly=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acalc_calculation_runs").fetchone()[0], 2)
        self.assertEqual(Path(imported.path).read_bytes(), native)

    def test_existing_working_directory_is_never_replaced(self):
        document = self.exported()
        destination = self.root / "working"
        destination.mkdir()
        marker = destination / "owner.txt"
        marker.write_text("keep")
        with self.assertRaisesRegex(ModelError, "already exists"):
            self.imported(document)
        self.assertEqual(marker.read_text(), "keep")

    def test_unsafe_extraction_and_external_paths_are_rejected(self):
        for path in ("../escape", "/tmp/escape", "a/../b", "a\\b", "C:/escape"):
            with self.subTest(path=path):
                document = self.exported()
                binding = models(document)[0]["payload"]["extensions"]["acalc-qgis"]
                binding["files"][binding["geopackage_file_id"]] = path
                with self.assertRaises(ModelError):
                    self.imported(document)
                record = document["files"][0]
                record.pop("data")
                record["path"] = path
                with self.assertRaises(ModelError):
                    file_bytes(record, self.root)

    def test_external_project_embeds_unowned_files_before_relocation(self):
        document = self.exported()
        record = document["files"][0]
        (self.root / "native.gpkg").write_bytes(base64.b64decode(record.pop("data")))
        record["path"] = "native.gpkg"
        document["transfer"]["files"] = "external"
        imported = self.imported(document)
        (self.root / "native.gpkg").unlink()
        self.output.unlink()
        exported = export_model(imported, self.root / "new.acalc-project.json")
        self.assertEqual(exported["transfer"]["files"], "embedded")
        self.assertTrue(all("data" in r and "path" not in r for r in exported["files"]))

    def test_bounds_duplicate_keys_nonfinite_numbers(self):
        for value in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.assertRaises(ModelError):
                decode(value)
        with self.assertRaisesRegex(ModelError, "32 MiB"):
            encode({"large": "a" * MAX_BYTES})
        with self.assertRaisesRegex(ModelError, "32 MiB"):
            decode(b" " * (MAX_BYTES + 1))

    def test_plugin_open_export_and_reopen_actual_native_layers(self):
        document = self.exported()
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(iface.window.close)
        self.addCleanup(plugin.unload)
        target = self.root / "through-ui.acalc-project.json"
        with patch("acalc.plugin.QFileDialog.getOpenFileName", return_value=(str(self.output), "")), \
             patch("acalc.plugin.QFileDialog.getExistingDirectory", return_value=str(self.root)), \
             patch("acalc.plugin.QInputDialog.getText", return_value=("ui-working", True)):
            plugin.choose_project()
        self.assertIsNotNone(plugin.store)
        self.assertEqual(len(plugin.layers), len(self.store.layers()))
        self.assertTrue(plugin.dock.export_project_button.isEnabled())
        with patch("acalc.plugin.QFileDialog.getSaveFileName", return_value=(str(target), "")):
            plugin.dock.export_project_button.click()
        self.assertEqual(len(models(read(target))), 1)
        self.assertTrue(plugin.close_project())
        self.assertTrue(plugin.open_project(self.root / "ui-working" / "model.gpkg"))
        self.assertEqual(plugin.store.metadata()["project_id"], self.store.metadata()["project_id"])

    def test_multi_model_selection_cancel_and_dirty_export(self):
        document = self.exported()
        second = copy.deepcopy(models(document)[0])
        second.update(id="second-native-model", name="Second model")
        document["project"]["models"].append(second)
        self.output.write_bytes(encode(document))
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(iface.window.close)
        self.addCleanup(plugin.unload)
        plugin.open_project(self.store.path)
        original = plugin.store.path
        with patch("acalc.plugin.QInputDialog.getItem", return_value=("", False)):
            self.assertFalse(plugin.import_portable_project(self.output))
        self.assertEqual(plugin.store.path, original)
        with patch("acalc.plugin.QInputDialog.getItem", return_value=("Second model (second-native-model)", True)), \
             patch("acalc.plugin.QFileDialog.getExistingDirectory", return_value=str(self.root)), \
             patch("acalc.plugin.QInputDialog.getText", return_value=("second-model-working", True)):
            self.assertTrue(plugin.import_portable_project(self.output))
        self.assertEqual(plugin.store.portable_context["model_id"], second["id"])
        plugin.dock.project_name.setText("Unsaved")
        with patch("acalc.plugin.QFileDialog.getSaveFileName") as dialog:
            with self.assertRaisesRegex(ModelError, "Save project details"):
                plugin.export_portable_project()
            dialog.assert_not_called()
