# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Catalog persistence, legacy upgrade, source references and the real Qt editors."""

from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog, QLineEdit, QMessageBox, QToolButton

from acalc.dialogs import FeatureDialog, null_text
from acalc.layers import load_layer
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, SCHEMA_VERSION, definition, new_id
from acalc.spectra import OCTAVE_BANDS, missing_bands, spectrum_definition
from acalc.spectra_ui import SpectrumDialog
from acalc.storage import ModelError, ModelStore
from tests.test_calculation_storage import downgrade_to_legacy
from tests.test_model import APP, FakeIface, ModelFixture, add_feature, rows_for


def power():
    return spectrum_definition("sound_power", [0, -5, 65, 70, 75, 70, 60, 50], "Synthetic test data")


def absorption():
    return spectrum_definition("absorption", [0, 0.1, 0.2, 0.4, 0.5, 0.7, 0.9, 1], "Synthetic material")


class SpectrumStoreTests(ModelFixture):
    def test_catalog_roundtrip_rename_and_export_with_distinct_unknown_and_zero_energy(self):
        self.assertEqual(self.store.spectra(), [])
        value = power()
        value["values"][2:4] = [0, None]
        source = self.store.add_spectrum("Fan", value)
        material = self.store.add_spectrum("Panel", absorption())
        self.assertEqual(missing_bands(value), [500])
        self.store.save_spectrum(source, "Renamed fan", value)
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.spectra("sound_power"), [{"spectrum_id": source, "name": "Renamed fan", "spectrum": value}])
        self.assertEqual(reopened.spectra("absorption")[0]["spectrum_id"], material)
        exported = reopened.export_variant(reopened.variants()[0]["variant_id"])
        decoded = json.loads(json.dumps(exported, allow_nan=False))
        self.assertEqual(decoded["schema_version"], SCHEMA_VERSION)
        self.assertEqual(decoded["spectra"], reopened.spectra())
        self.assertEqual(decoded["spectra"][1]["spectrum"]["values"][0], 0)
        self.store.delete_spectrum(material)
        self.assertEqual(self.store.spectra("absorption"), [])
        other = ModelStore.create(Path(self.directory.name) / "other.gpkg", "Other", self.store.metadata()["crs_wkt"])
        self.assertEqual(other.spectra(), [])

    def test_invalid_values_names_and_kinds_do_not_change_saved_catalog(self):
        spectrum_id = self.store.add_spectrum("Existing", power())
        before = self.store.spectra()
        for bad in (float("nan"), float("inf"), float("-inf"), True, "75", 10 ** 400):
            value = power()
            value["values"][0] = bad
            with self.subTest(value=str(bad)[:20]), self.assertRaises(ModelError):
                self.store.save_spectrum(spectrum_id, "Changed", value)
        for key, bad in (("frequencies_hz", [125] * 8), ("values", [20]), ("unit", "dBA"),
                         ("weighting", "A"), ("power_basis", "invalid"), ("kind", "pressure"),
                         ("kind", {}), ("schema_version", True), ("provenance", None),
                         ("zero_energy_bands_hz", None), ("zero_energy_bands_hz", [63, 63])):
            value = power()
            value[key] = bad
            with self.subTest(key=key), self.assertRaises(ModelError):
                self.store.add_spectrum("Invalid", value)
        for bad in (-0.01, 1.01):
            value = absorption()
            value["values"][7] = bad
            with self.assertRaisesRegex(ModelError, "between 0 and 1"):
                self.store.add_spectrum("Invalid material", value)
        value = power()
        value["zero_energy_bands_hz"] = [63]
        with self.assertRaisesRegex(ModelError, "Enter 0"):
            self.store.add_spectrum("Conflicting", value)
        with self.assertRaisesRegex(ModelError, "already exists"):
            self.store.add_spectrum(" EXISTING ", power())
        with self.assertRaisesRegex(ModelError, "change type"):
            self.store.save_spectrum(spectrum_id, "Material", absorption())
        with self.assertRaises(ModelError):
            self.store.add_spectrum("  ", power())
        self.assertEqual(self.store.spectra(), before)
        # Names are unique within, not across, the two catalogs.
        self.store.add_spectrum("Existing", absorption())
        with self.store.connection() as db:
            db.execute("UPDATE acalc_spectra SET spectrum_json='{}' WHERE spectrum_id=?", (spectrum_id,))
        with self.assertRaisesRegex(ModelError, "Invalid spectrum"):
            ModelStore.open(self.path)

    def test_saved_and_archived_source_references_protect_deletion_and_survive_moving(self):
        spectrum_id = self.store.add_spectrum("Fan", power())
        self.store.add_spectrum("Wall", absorption())
        record = self.store.add_layer("point_source", "Plant")
        layer = load_layer(self.store, record)
        feature = add_feature(layer, LAYER_TYPES["point_source"])
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("spectrum_id"), spectrum_id)
        self.assertTrue(layer.commitChanges())
        del layer
        config = self.store.variants()[0]
        self.store.save_variant(config["variant_id"], "Selected", definition([record["layer_id"]]))
        for archived in (False, True):
            if archived:
                self.store.archive_layer(record["layer_id"])
            with self.assertRaisesRegex(ModelError, "used by"):
                self.store.delete_spectrum(spectrum_id)
        self.store.restore_layer(record["layer_id"])
        self.store.save_variant(config["variant_id"], "Selected", definition([record["layer_id"]]))
        package = Path(self.directory.name) / "moved"
        package.mkdir()
        exported = self.store.export_variant(config["variant_id"])
        (package / "variant.json").write_text(json.dumps(exported))
        shutil.copy2(self.path, package / self.path.name)
        moved = ModelStore.open(package / exported["geopackage"])
        self.assertEqual(moved.export_variant(config["variant_id"]), exported)
        with moved.connection(readonly=True) as db:
            self.assertEqual(db.execute(f'SELECT spectrum_id FROM "{record["table_name"]}"').fetchone()[0], spectrum_id)

    def test_legacy_example_upgrade_preserves_geometry_ids_membership_and_backup(self):
        legacy = Path(__file__).resolve().parents[1] / "tests/fixtures" / "example-site.gpkg"
        copied = Path(self.directory.name) / "legacy.gpkg"
        shutil.copy2(legacy, copied)
        with closing(sqlite3.connect(copied)) as db:
            self.assertEqual(db.execute("SELECT schema_version FROM acalc_project").fetchone()[0], 1)
            tables = [row[0] for row in db.execute("SELECT table_name FROM acalc_layers")]
            geometry = {table: db.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}
            layers = db.execute("SELECT * FROM acalc_layers").fetchall()
            configs = db.execute("SELECT * FROM acalc_configurations").fetchall()
            project_id = db.execute("SELECT project_id FROM acalc_project").fetchone()[0]
        store = ModelStore.open(copied)
        self.assertTrue(Path(store.migration_backup).is_file())
        self.assertEqual(store.metadata()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(store.metadata()["project_id"], project_id)
        self.assertEqual(store.spectra(), [])
        with store.connection(readonly=True) as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM acalc_layers")], layers)
            for table in tables:
                self.assertEqual([tuple(row)[:len(geometry[table][0])] for row in db.execute(f'SELECT * FROM "{table}"')], geometry[table])
        for before, after in zip(configs, store.variants()):
            value = definition(json.loads(before[2])["layer_ids"])
            self.assertEqual(after["variant_id"], before[0])
            self.assertEqual(after["definition"], value)
        with closing(sqlite3.connect(store.migration_backup)) as db:
            self.assertEqual(db.execute("SELECT schema_version FROM acalc_project").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT * FROM acalc_configurations").fetchall(), configs)
            for table in tables:
                self.assertEqual(db.execute(f'SELECT * FROM "{table}"').fetchall(), geometry[table])
        self.assertIsNone(ModelStore.open(copied).migration_backup)
        self.assertEqual(len(list(copied.parent.glob("legacy.schema-1-backup-*.gpkg"))), 1)
        store.add_spectrum("After upgrade", power())
        self.assertEqual(len(ModelStore.open(copied).spectra()), 1)

    def test_legacy_unknown_data_and_failed_migration_leave_original_intact(self):
        def make_legacy():
            with self.store.connection() as db:
                db.execute("UPDATE acalc_project SET schema_version=1")
                for row in db.execute("SELECT * FROM acalc_variants").fetchall():
                    value = json.loads(row["definition_json"])
                    value["schema_version"] = 1
                    db.execute("UPDATE acalc_variants SET definition_json=? WHERE variant_id=?", (json.dumps(value), row["variant_id"]))
                downgrade_to_legacy(db, 1)

        make_legacy()
        with self.store.connection() as db:
            db.execute("INSERT INTO acalc_spectra(spectrum_id,name,spectrum_json) VALUES (?,?,?)", (new_id(), "Undefined", "{}"))
        with self.assertRaisesRegex(ModelError, "reserved spectrum table"):
            ModelStore.open(self.path)
        self.assertEqual(self.store.metadata()["schema_version"], 1)
        with self.store.connection() as db:
            db.execute("DELETE FROM acalc_spectra")
            db.execute("CREATE TRIGGER fail_upgrade BEFORE UPDATE ON acalc_project BEGIN SELECT RAISE(ABORT,'migration test'); END")
        with self.store.connection(readonly=True) as db:
            before = [tuple(row) for row in db.execute("SELECT * FROM acalc_configurations")]
        with self.assertRaisesRegex(ModelError, "migration test"):
            ModelStore.open(self.path)
        self.assertEqual(self.store.metadata()["schema_version"], 1)
        with self.store.connection(readonly=True) as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM acalc_configurations")], before)
        self.assertEqual(len(list(self.path.parent.glob("*.schema-1-backup-*.gpkg"))), 1)


class SpectrumEditorTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.open_project(self.path)

    def tearDown(self):
        self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def test_sound_power_total_updates_after_edit_and_paste(self):
        dialog = SpectrumDialog(self.plugin.dock, "sound_power", {
            "name": "Single band", "spectrum": spectrum_definition(
                "sound_power", [0, 0, 0, 0, 80, 0, 0, 0])})
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.overall.text(), "Overall A-weighted level: 80.0 dBA")
        dialog.show()
        dialog.bands.editItem(dialog.bands.item(0, 4))
        APP.processEvents()
        editor = dialog.bands.findChild(QLineEdit)
        editor.selectAll()
        QTest.keyClicks(editor, "90")
        QTest.keyClick(editor, Qt.Key_Tab)
        self.assertEqual(dialog.overall.text(), "Overall A-weighted level: 90.0 dBA")
        dialog.bands.setCurrentCell(0, 0)
        # Two equal weighted 80 dB bands sum to 83.0103 dBA.
        APP.clipboard().setText("0\t0\t0\t83.2\t80\t0\t0\t0")
        dialog.bands.paste_cells()
        self.assertEqual(dialog.overall.text(), "Overall A-weighted level: 83.0 dBA")
        dialog.power_basis.setCurrentIndex(dialog.power_basis.findData("per_metre"))
        self.assertEqual(dialog.overall.text(), "Overall A-weighted level: 83.0 dBA per meter")
        dialog.bands.item(1, 4).setText("120")
        self.assertEqual(dialog.overall.text(), "Overall A-weighted level: 83.0 dBA per meter")

    def test_sound_power_total_preserves_silence_unknown_and_invalid(self):
        dialog = SpectrumDialog(self.plugin.dock, "sound_power")
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.overall.text(), "Overall A-weighted level: Unknown")
        for column in range(8):
            dialog.bands.item(0, column).setText("0")
        self.assertEqual(dialog.overall.text(), "Overall A-weighted level: −∞ dBA")
        dialog.bands.item(0, 4).setText("-5")
        self.assertEqual(dialog.overall.text(), "Overall A-weighted level: -5.0 dBA")
        for text in ("", "text", "nan", "inf"):
            dialog.bands.item(0, 4).setText(text)
            self.assertEqual(dialog.overall.text(), "Overall A-weighted level: " +
                             ("Unknown" if not text else "Invalid values"))

    def test_nrc_updates_only_from_required_bands(self):
        dialog = SpectrumDialog(self.plugin.dock, "absorption")
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.overall.text(), "NRC: Unknown")
        for column in (2, 3, 4, 5):
            dialog.bands.item(0, column).setText("0.5")
        self.assertEqual(dialog.overall.text(), "NRC: 0.50")
        for column in (2, 3, 4, 5):
            dialog.bands.item(0, column).setText("0.7")
            self.assertEqual(dialog.overall.text(), f"NRC: {0.5 + (column - 1) * 0.05:.2f}")
        dialog.bands.item(0, 0).setText("bad")
        self.assertEqual(dialog.overall.text(), "NRC: 0.70")
        dialog.bands.item(0, 5).setText("0.8")  # Mean 0.725 rounds up to 0.75.
        self.assertEqual(dialog.overall.text(), "NRC: 0.75")
        for text in ("", "-0.1", "1.1", "nan", "text"):
            dialog.bands.item(0, 5).setText(text)
            self.assertEqual(dialog.overall.text(), "NRC: " +
                             ("Unknown" if not text else "Invalid values"))

    def test_catalog_menu_create_edit_duplicate_cancel_delete_and_project_switch(self):
        dock = self.plugin.dock
        menu = dock.findChild(QToolButton).menu()
        next(action for action in menu.actions() if action.text() == "Spectra").trigger()
        page = dock.spectra_page
        self.assertIs(dock.stack.currentWidget(), page)
        self.assertFalse(page.edit_button.isEnabled())
        # Catalog saves must not discard unsaved project metadata.
        dock.project_name.setText("Pending project name")

        def create(dialog):
            dialog.name.setText("Fan")
            dialog.provenance.setPlainText("Test data sheet")
            for row, number in enumerate(power()["values"]):
                dialog.bands.item(0, row).setText(str(number))
            dialog.bands.item(0, 2).setText("0")
            dialog.bands.item(0, 3).setText("")
            dialog.validate()
            return dialog.result()

        with patch.object(SpectrumDialog, "exec", create):
            QTest.mouseClick(page.new_button, Qt.LeftButton)
        spectrum_id = page.selected_id()
        self.assertEqual(page.table.rowCount(), 1)
        self.assertEqual(page.table.columnCount(), 2)
        self.assertEqual(page.table.item(0, 1).text(), "")
        self.assertTrue(page.table.item(0, 1).icon().isNull())
        self.assertEqual(dock.project_name.text(), "Pending project name")

        def edit(dialog):
            self.assertEqual(dialog.bands.item(0, 2).text(), "0.0")
            self.assertEqual(dialog.bands.item(0, 0).text(), "0.0")
            dialog.name.setText("Renamed fan")
            dialog.bands.item(0, 3).setText("77")
            dialog.validate()
            return dialog.result()

        with patch.object(SpectrumDialog, "exec", edit):
            self.plugin.edit_spectrum()
        self.assertEqual(page.selected_id(), spectrum_id)
        self.assertEqual(page.table.item(0, 1).text(), "")

        def accept(dialog):
            dialog.validate()
            return dialog.result()

        with patch.object(SpectrumDialog, "exec", accept):
            QTest.mouseClick(page.duplicate_button, Qt.LeftButton)
        copy_id = page.selected_id()
        self.assertNotEqual(copy_id, spectrum_id)
        self.assertEqual(page.table.rowCount(), 2)
        saved = deepcopy(self.store.spectra())
        with patch.object(SpectrumDialog, "exec", return_value=QDialog.Rejected):
            self.plugin.edit_spectrum()
        self.assertEqual(self.store.spectra(), saved)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            QTest.mouseClick(page.delete_button, Qt.LeftButton)
        self.assertEqual(page.table.rowCount(), 1)
        page.tabs.setCurrentIndex(1)

        def material(dialog):
            self.assertEqual(dialog.bands.columnCount(), 8)
            dialog.name.setText("Wall lining")
            for row, number in enumerate(absorption()["values"]):
                dialog.bands.item(0, row).setText(str(number))
            dialog.validate()
            return dialog.result()

        with patch.object(SpectrumDialog, "exec", material):
            QTest.mouseClick(page.new_button, Qt.LeftButton)
        self.assertEqual(len(self.store.spectra("absorption")), 1)
        dock.project_name.setText(self.store.metadata()["name"])
        self.assertTrue(self.plugin.close_project())
        self.assertFalse(page.isEnabled())
        self.assertEqual(page.table.rowCount(), 0)
        self.plugin.open_project(self.path)
        self.assertEqual(page.table.rowCount(), 1)
        second = ModelStore.create(Path(self.directory.name) / "empty.gpkg", "Empty", self.store.metadata()["crs_wkt"])
        self.plugin.open_project(second.path)
        self.assertEqual(page.table.rowCount(), 0)

    def test_editor_rejects_bad_bands_and_keeps_explicit_unknowns(self):
        for kind, bad in (("sound_power", "nan"), ("sound_power", "text"), ("absorption", "1.01"), ("absorption", "-0.1")):
            dialog = SpectrumDialog(self.plugin.dock, kind)
            dialog.name.setText("Example")
            dialog.bands.item(0, 0).setText(bad)
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Rejected)
            self.assertTrue(dialog.error.text())
            dialog.bands.item(0, 0).setText("0")
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted)
            self.assertEqual(dialog.value["values"], [0] + [None] * 7)

    def test_point_and_line_assignment_undo_cancel_save_reopen_and_deletion_guard(self):
        spectrum_id = self.store.add_spectrum("Fan", power())
        material_id = self.store.add_spectrum("Material", absorption())
        for kind_key in ("point_source", "line_source"):
            record = self.store.add_layer(kind_key, kind_key)
            self.plugin.reload_layers()
            dock = self.plugin.dock
            dock.show_view(kind_key)
            layer = self.plugin.selected_layer()

            def create(dialog):
                self.assertEqual(dialog.spectrum_combo.findData(material_id), -1)
                for row in range(dialog.table.rowCount()):
                    for column, number in enumerate(rows_for(LAYER_TYPES[kind_key])[row][1:]):
                        dialog.table.item(row, column).setText(str(number))
                dialog.spectrum_combo.setCurrentIndex(dialog.spectrum_combo.findData(spectrum_id))
                dialog.validate()
                return dialog.result()

            with patch.object(FeatureDialog, "exec", create):
                self.assertTrue(self.plugin.edit_feature(create=True))
            feature = next(layer.getFeatures())
            self.assertEqual(feature["spectrum_id"], spectrum_id)
            self.plugin.commit_layer(layer)
            feature = next(layer.getFeatures())

            def clear(dialog):
                self.assertEqual(dialog.spectrum_combo.currentData(), spectrum_id)
                self.assertEqual(dialog.spectrum_preview.item(0, 0).text(), "0")
                dialog.spectrum_combo.setCurrentIndex(0)
                dialog.validate()
                return dialog.result()

            with patch.object(FeatureDialog, "exec", clear):
                self.plugin.edit_feature(layer=layer, feature_id=feature.id())
            self.assertEqual(null_text(layer.getFeature(feature.id())["spectrum_id"]), "")
            self.assertIn("No sound power spectrum", " ".join(self.plugin.validation_errors(require_spectra=True)))
            dock.show_view("spectra")
            dock.spectra_page.tabs.setCurrentIndex(0)
            dock.spectra_page.refresh(spectrum_id)
            with self.assertRaisesRegex(ModelError, "undo"):
                self.plugin.delete_spectrum()
            layer.undoStack().undo()
            self.assertEqual(layer.getFeature(feature.id())["spectrum_id"], spectrum_id)
            layer.undoStack().redo()
            self.assertEqual(null_text(layer.getFeature(feature.id())["spectrum_id"]), "")
            layer.undoStack().undo()
            self.plugin.commit_layer(layer)
            with patch.object(FeatureDialog, "exec", return_value=QDialog.Rejected):
                self.assertFalse(self.plugin.edit_feature(layer=layer, feature_id=feature.id()))
            self.assertFalse(layer.isEditable())
            reopened = load_layer(ModelStore.open(self.path), record)
            self.assertEqual(next(reopened.getFeatures())["spectrum_id"], spectrum_id)
            self.assertEqual(self.plugin.validation_errors(require_spectra=True), [])
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            with self.assertRaisesRegex(ModelError, "used by"):
                self.plugin.delete_spectrum()

    def test_validation_rejects_wrong_references_and_reports_incomplete_emission(self):
        power_id = self.store.add_spectrum("Draft", spectrum_definition("sound_power"))
        material_id = self.store.add_spectrum("Wall", absorption())
        record = self.store.add_layer("point_source", "Sources")
        self.plugin.reload_layers()
        layer = self.plugin.layers[record["layer_id"]]
        feature = add_feature(layer, LAYER_TYPES["point_source"])
        for reference in (material_id, new_id()):
            layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("spectrum_id"), reference)
            with self.assertRaisesRegex(ModelError, "sound power spectrum"):
                self.plugin.commit_layer(layer)
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("spectrum_id"), power_id)
        self.plugin.commit_layer(layer)  # Drafts can be authored and saved.
        self.assertIn("unknown at 63", " ".join(self.plugin.validation_errors(require_spectra=True)))
        config = self.store.variants()[0]
        self.store.save_variant(config["variant_id"], "Draft", definition([record["layer_id"]]))
        self.assertEqual(len(self.store.export_variant(config["variant_id"])["spectra"]), 2)
        layer.startEditing()
        feature = next(layer.getFeatures())
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("spectrum_id"), material_id)
        self.assertTrue(layer.commitChanges())  # Simulate QGIS native save bypassing Acalc.
        with self.assertRaisesRegex(ModelError, "non-sound-power"):
            self.store.export_variant(config["variant_id"])
