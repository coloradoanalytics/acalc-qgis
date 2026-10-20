# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Surface material assignments through real QGIS editors and GeoPackages."""

from contextlib import closing
import json
import sqlite3
from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog, QMessageBox

from acalc.calculation_settings import ISO_ENGINE, default_settings
from acalc.dialogs import FeatureDialog, null_text
from acalc.layers import load_layer, validate_layer
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, SCHEMA_VERSION, definition, new_id
from acalc.spectra import spectrum_definition
from acalc.spectra_ui import SpectrumDialog
from acalc.storage import ModelError, ModelStore
from tests.test_model import APP, FakeIface, ModelFixture, add_feature, rows_for


def material():
    return spectrum_definition("absorption", [0, 0.1, 0.2, 0.4, 0.5, 0.7, 0.9, 1], "Synthetic material reference")


class SurfaceMaterialEditorTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.material_id = self.store.add_spectrum("Panel", material())
        self.power_id = self.store.add_spectrum("Fan", spectrum_definition("sound_power", [70] * 8))
        self.draft_id = self.store.add_spectrum("Draft material", spectrum_definition("absorption"))
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.open_project(self.path)

    def tearDown(self):
        self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def test_assign_clear_undo_cancel_save_reopen_preserves_scalar_fallback(self):
        for kind_key in ("building", "barrier"):
            with self.subTest(kind=kind_key):
                record = self.store.add_layer(kind_key, kind_key)
                self.plugin.reload_layers()
                layer = self.plugin.layers[record["layer_id"]]
                kind = LAYER_TYPES[kind_key]

                def create(dialog):
                    self.assertEqual(dialog.absorption_combo.findData(self.power_id), -1)
                    self.assertIn("unknown bands", dialog.absorption_combo.itemText(dialog.absorption_combo.findData(self.draft_id)))
                    dialog.table.setRowCount(0)
                    for row in rows_for(kind):
                        dialog.add_values(row)
                    dialog.use_default_reflection.setChecked(False)
                    dialog.absorption_coefficient.setValue(0.65)
                    dialog.absorption_combo.setCurrentIndex(dialog.absorption_combo.findData(self.material_id))
                    self.assertFalse(dialog.use_default_reflection.isEnabled())
                    self.assertFalse(dialog.absorption_coefficient.isEnabled())
                    self.assertEqual(dialog.absorption_preview.item(0, 0).text(), "0")
                    self.assertEqual(dialog.absorption_preview.rowCount(), 1)
                    self.assertEqual(dialog.absorption_preview.item(0, 7).text(), "1")
                    self.assertIn("Synthetic material reference", dialog.absorption_status.text())
                    dialog.validate()
                    self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                    return dialog.result()

                with patch.object(FeatureDialog, "exec", create):
                    self.assertTrue(self.plugin.edit_feature(create=True, layer=layer))
                feature = next(layer.getFeatures())
                self.assertEqual(feature["absorption_spectrum_id"], self.material_id)
                self.assertEqual(feature["reflection_coefficient"], 0.35)
                self.assertEqual(self.plugin.validation_errors(require_spectra=True), [])
                self.plugin.commit_layer(layer)
                feature = next(layer.getFeatures())
                saved_geometry = bytes(feature.geometry().asWkb())

                def clear(dialog):
                    self.assertEqual(dialog.absorption_combo.currentData(), self.material_id)
                    dialog.absorption_combo.setCurrentIndex(0)
                    self.assertTrue(dialog.use_default_reflection.isEnabled())
                    self.assertTrue(dialog.absorption_coefficient.isEnabled())
                    self.assertEqual(dialog.absorption_coefficient.value(), 0.65)
                    dialog.validate()
                    return dialog.result()

                with patch.object(FeatureDialog, "exec", clear):
                    self.assertTrue(self.plugin.edit_feature(layer=layer, feature_id=feature.id()))
                self.assertEqual(null_text(layer.getFeature(feature.id())["absorption_spectrum_id"]), "")
                layer.undoStack().undo()
                self.assertEqual(layer.getFeature(feature.id())["absorption_spectrum_id"], self.material_id)
                layer.undoStack().redo()
                self.assertEqual(null_text(layer.getFeature(feature.id())["absorption_spectrum_id"]), "")
                layer.undoStack().undo()
                self.plugin.commit_layer(layer)

                def cancel(dialog):
                    dialog.absorption_combo.setCurrentIndex(0)
                    dialog.reject()
                    return dialog.result()

                with patch.object(FeatureDialog, "exec", cancel):
                    self.assertFalse(self.plugin.edit_feature(layer=layer, feature_id=feature.id()))
                self.assertFalse(layer.isEditable())
                reopened = load_layer(ModelStore.open(self.path), record)
                saved = next(reopened.getFeatures())
                self.assertEqual(saved["absorption_spectrum_id"], self.material_id)
                self.assertEqual(saved["reflection_coefficient"], 0.35)
                self.assertEqual(bytes(saved.geometry().asWkb()), saved_geometry)
                dialog = FeatureDialog(None, kind, "m", saved, self.store.spectra())
                self.addCleanup(dialog.close)
                self.assertEqual(dialog.absorption_combo.currentData(), self.material_id)
                self.assertFalse(dialog.absorption_coefficient.isEnabled())
                if kind_key == "building":
                    dialog.show()
                    dialog.tabs.setCurrentIndex(1)
                    APP.processEvents()
                    self.assertFalse(dialog.building_preview.isVisible())
                    self.assertEqual(dialog.absorption_preview.horizontalScrollBar().maximum(), 0)
                    dialog.tabs.setCurrentIndex(0)
                    self.assertTrue(dialog.building_preview.isVisible())

    def test_missing_wrong_and_unknown_materials_remain_visible_and_block_readiness(self):
        for kind_key in ("building", "barrier"):
            record = self.store.add_layer(kind_key, kind_key)
            self.plugin.reload_layers()
            layer = self.plugin.layers[record["layer_id"]]
            kind = LAYER_TYPES[kind_key]
            feature = add_feature(layer, kind)
            index = layer.fields().indexFromName("absorption_spectrum_id")
            for reference in (self.power_id, new_id()):
                layer.changeAttributeValue(feature.id(), index, reference)
                with self.assertRaisesRegex(ModelError, "absorption spectrum"):
                    self.plugin.commit_layer(layer)
                dialog = FeatureDialog(None, kind, "m", layer.getFeature(feature.id()), self.store.spectra())
                self.addCleanup(dialog.close)
                self.assertEqual(dialog.absorption_combo.currentData(), reference)
                self.assertIn("unavailable", dialog.absorption_status.text().lower())
                self.assertFalse(dialog.absorption_coefficient.isEnabled())
            layer.changeAttributeValue(feature.id(), index, self.draft_id)
            self.plugin.commit_layer(layer)  # Incomplete catalog entries remain authorable drafts.
            errors = self.plugin.validation_errors(require_spectra=True)
            self.assertIn("Surface absorption is unknown at 63", " ".join(errors))
            feature = next(layer.getFeatures())
            dialog = FeatureDialog(None, kind, "m", feature, self.store.spectra())
            self.addCleanup(dialog.close)
            self.assertIn("unknown at 63", dialog.absorption_status.text())
            self.assertEqual(dialog.absorption_preview.item(0, 0).text(), "Unknown")
            layer.startEditing()
            layer.changeAttributeValue(feature.id(), index, self.material_id)
            self.plugin.commit_layer(layer)
            self.assertEqual(self.plugin.validation_errors(require_spectra=True), [])
            variant = self.store.variants()[0]
            self.store.save_variant(variant["variant_id"], "Default", definition([record["layer_id"]]))
            self.assertEqual(self.store.export_variant(variant["variant_id"])["schema_version"], SCHEMA_VERSION)
            layer.startEditing()
            feature = next(layer.getFeatures())
            layer.changeAttributeValue(feature.id(), index, self.power_id)
            self.assertTrue(layer.commitChanges())  # Native QGIS save can bypass Acalc validation.
            with self.assertRaisesRegex(ModelError, "non-absorption"):
                self.store.export_variant(variant["variant_id"])
            layer.startEditing()
            layer.changeAttributeValue(feature.id(), index, None)
            self.plugin.commit_layer(layer)

    def test_shared_edit_usage_and_deletion_cover_pending_saved_and_archived_assignments(self):
        record = self.store.add_layer("barrier", "Walls")
        self.plugin.reload_layers()
        layer = self.plugin.layers[record["layer_id"]]
        feature = add_feature(layer, LAYER_TYPES["barrier"])
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("absorption_spectrum_id"), self.material_id)
        page = self.plugin.dock.spectra_page
        self.plugin.dock.show_view("spectra")
        page.tabs.setCurrentIndex(1)
        page.refresh(self.material_id)

        def edit(dialog):
            self.assertIn("Walls: 1 in current edit buffer", dialog.usage.text())
            dialog.bands.item(0, 0).setText("0.75")
            dialog.validate()
            return dialog.result()

        with patch.object(SpectrumDialog, "exec", edit):
            self.plugin.edit_spectrum()
        self.assertEqual(self.store.spectra("absorption")[1]["spectrum"]["values"][0], 0.75)
        self.assertTrue(layer.isEditable())
        with self.assertRaisesRegex(ModelError, "undo"):
            self.plugin.delete_spectrum()
        self.plugin.commit_layer(layer)
        self.assertEqual(self.store.spectrum_usage(self.material_id)[0]["count"], 1)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            with self.assertRaisesRegex(ModelError, "used by"):
                self.plugin.delete_spectrum()
        layer.startEditing()
        feature = next(layer.getFeatures())
        layer.beginEditCommand("Unassign material")
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("absorption_spectrum_id"), None)
        layer.endEditCommand()
        with self.assertRaisesRegex(ModelError, "undo"):
            self.plugin.delete_spectrum()
        layer.undoStack().undo()
        self.plugin.commit_layer(layer)
        self.store.archive_layer(record["layer_id"])
        self.assertTrue(self.store.spectrum_usage(self.material_id)[0]["archived"])
        with self.assertRaisesRegex(ModelError, "removed layers"):
            self.store.delete_spectrum(self.material_id)


class SurfaceMaterialMigrationTests(ModelFixture):
    def legacy_model(self, omit_scalar=False):
        records = []
        for key in ("building", "barrier"):
            record = self.store.add_layer(key, key)
            records.append(record)
            layer = load_layer(self.store, record)
            feature = add_feature(layer, LAYER_TYPES[key])
            layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("reflection_coefficient"), 0.42)
            self.assertTrue(layer.commitChanges())
            del feature, layer
        variant = self.store.variants()[0]
        self.store.save_variant(variant["variant_id"], "All objects", definition([r["layer_id"] for r in records]))
        self.store.add_spectrum("Panel", material())
        with self.store.connection() as db:
            db.execute("UPDATE acalc_project SET schema_version=7")
            value = {"schema_version": 7, "layer_ids": [r["layer_id"] for r in records]}
            db.execute("UPDATE acalc_variants SET definition_json=?", (json.dumps(value),))
            for record in records:
                db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN absorption_spectrum_id')
                db.execute(f'ALTER TABLE "{record["table_name"]}" ADD COLUMN infer_z INTEGER NOT NULL DEFAULT 0')
                if omit_scalar:
                    db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN reflection_coefficient')
        return records

    def test_schema7_upgrade_is_backed_up_preserves_content_and_is_idempotent(self):
        records = self.legacy_model()
        settings = {**default_settings(), "reflection_order": 2, "building_reflection_coefficient": 0.61}
        self.store.save_calculation(settings)
        project = self.store.metadata()
        variant_id = self.store.variants()[0]["variant_id"]
        run_id = new_id()
        with self.store.connection() as db:
            # References may point to an unavailable raster at reopen time.
            db.execute("INSERT INTO acalc_elevation_rasters VALUES (?,?,?,?)",
                       (new_id(), "Survey", str(self.path.parent / "survey.tif"), 1))
            db.execute("""INSERT INTO acalc_calculation_runs VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,'[]','{}')""",
                (run_id, project["project_id"], variant_id, "All objects", ISO_ENGINE, "0.13.0", "ISO 9613-2:2024",
                 "2026-09-23", "2026-09-23", "f" * 64, project["modified_at"], json.dumps(settings), 1, 1, 0.125))
            db.execute("INSERT INTO acalc_receiver_results VALUES (?,?,?,?,?,?,?,'{}')",
                       (run_id, new_id(), new_id(), "Previous receiver", json.dumps([40] * 8), 49, 47))
        with self.store.connection(readonly=True) as db:
            tables = [r["table_name"] for r in records] + ["acalc_spectra", "acalc_calculation", "acalc_elevation_rasters",
                                                         "acalc_calculation_runs", "acalc_receiver_results"]
            before = {table: [tuple(row) for row in db.execute(f'SELECT * FROM "{table}"')] for table in tables}
        upgraded = ModelStore.open(self.path)
        self.assertEqual(upgraded.metadata()["schema_version"], SCHEMA_VERSION)
        self.assertTrue(upgraded.migration_backup)
        with closing(sqlite3.connect(upgraded.migration_backup)) as db:
            self.assertEqual(db.execute("SELECT schema_version FROM acalc_project").fetchone()[0], 7)
            for table in tables:
                self.assertEqual(db.execute(f'SELECT * FROM "{table}"').fetchall(), before[table])
        with upgraded.connection(readonly=True) as db:
            for table in tables:
                after = [tuple(row) for row in db.execute(f'SELECT * FROM "{table}"')]
                if table.startswith("model_"):
                    self.assertTrue(all(row[-1] is None for row in after))
                    after = [row[:-1] for row in after]
                self.assertEqual(after, [row[:-1] for row in before[table]] if table.startswith("model_") else before[table])
            for record in records:
                column = next(row for row in db.execute(f'PRAGMA table_info("{record["table_name"]}")')
                              if row["name"] == "absorption_spectrum_id")
                self.assertEqual((column["type"], column["notnull"], column["dflt_value"]), ("TEXT", 0, None))
        self.assertEqual(upgraded.variants()[0]["definition"], definition([r["layer_id"] for r in records]))
        self.assertEqual(upgraded.calculation()["settings"], settings)
        self.assertTrue(upgraded.latest_calculation_run(variant_id)["stale"])
        self.assertIsNone(ModelStore.open(self.path).migration_backup)

    def test_schema7_missing_optional_scalar_fields_and_transaction_rollback(self):
        records = self.legacy_model(omit_scalar=True)
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_material_migration BEFORE UPDATE ON acalc_project BEGIN SELECT RAISE(ABORT,'material migration failure'); END")
        with self.assertRaisesRegex(ModelError, "material migration failure"):
            ModelStore.open(self.path)
        with self.store.connection(readonly=True) as db:
            self.assertEqual(db.execute("SELECT schema_version FROM acalc_project").fetchone()[0], 7)
            self.assertEqual(json.loads(db.execute("SELECT definition_json FROM acalc_variants").fetchone()[0])["schema_version"], 7)
            for record in records:
                self.assertNotIn("absorption_spectrum_id", [row["name"] for row in db.execute(f'PRAGMA table_info("{record["table_name"]}")')])
        self.assertEqual(len(list(self.path.parent.glob("*.schema-7-backup-*.gpkg"))), 1)
        with self.store.connection() as db:
            db.execute("DROP TRIGGER fail_material_migration")
        upgraded = ModelStore.open(self.path)
        for record in records:
            layer = load_layer(upgraded, record)
            self.assertEqual(validate_layer(layer, LAYER_TYPES[record["feature_type"]], {}), [])
            feature = next(layer.getFeatures())
            self.assertEqual(null_text(feature["absorption_spectrum_id"]), "")
            self.assertEqual(null_text(feature["reflection_coefficient"]), "")

    def test_schema8_missing_assignment_column_is_rejected(self):
        record = self.store.add_layer("barrier", "Walls")
        with self.store.connection() as db:
            db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN absorption_spectrum_id')
        with self.assertRaisesRegex(ModelError, "Missing required fields"):
            ModelStore.open(self.path)

    def test_schema8_assignment_column_requires_nullable_text(self):
        record = self.store.add_layer("building", "Buildings")
        for declaration in ("INTEGER", "TEXT NOT NULL DEFAULT ''"):
            with self.subTest(declaration=declaration):
                with self.store.connection() as db:
                    db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN absorption_spectrum_id')
                    db.execute(f'ALTER TABLE "{record["table_name"]}" ADD COLUMN absorption_spectrum_id {declaration}')
                with self.assertRaisesRegex(ModelError, "nullable TEXT"):
                    ModelStore.open(self.path)

    def test_version1_absorption_semantics_are_unchanged(self):
        material_id = self.store.add_spectrum("Panel", material())
        legacy = {**material(), "schema_version": 1}
        with self.store.connection() as db:
            db.execute("UPDATE acalc_spectra SET spectrum_json=? WHERE spectrum_id=?", (json.dumps(legacy), material_id))
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.spectra("absorption")[0]["spectrum"], legacy)
