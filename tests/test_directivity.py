# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Directionality authoring through real GeoPackages, Qt editors and edit buffers."""

from copy import deepcopy
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from qgis.PyQt.QtCore import Qt, QEvent, QPoint, QPointF
from qgis.PyQt.QtGui import QMouseEvent
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox

from acalc import directivity as model
from acalc.dialogs import FeatureDialog, null_text
from acalc.directivity_ui import DirectivityDialog, SourceDirectivityWidget
from acalc.directivity_templates import DirectivityTemplateDialog
from acalc.layers import load_layer
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError, SCHEMA_VERSION, definition, new_id
from acalc.spectra import spectrum_definition
from acalc.storage import ModelStore
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


def pattern():
    return model.sectors([45, 120], [[0]*8, [-6]*8, [-18]*8], 30)


class DirectionalityMathTests(unittest.TestCase):
    def test_axis_rotation_order_antipodal_and_roll(self):
        # Independently enumerated cardinal directions and rotations.
        for target in model.AXES.values():
            pose = model.align([1, 0, 0], target)
            model.validate_orientation(pose)
            for actual, expected in zip(model.transform(pose, [1, 0, 0]), target):
                self.assertAlmostEqual(actual, expected, places=12)
        z = model.rotation([0, 0, 1], 90)
        x = model.rotation([1, 0, 0], 90)
        for actual, expected in zip(model.transform(model.compose(x, z), [1, 0, 0]), [0, 0, 1]):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(model.transform(model.compose(z, x), [1, 0, 0]), [0, 1, 0]):
            self.assertAlmostEqual(actual, expected)
        with self.assertRaises(ModelError):
            model.validate_orientation([[1, 0, 0], [0, 1, 0], [0, 0, -1]])
        with self.assertRaises(ModelError):
            model.read_orientation('[[2,0,0],[0,1,0],[0,0,1]]')

    def test_revolution_profile_interpolation_and_sectors(self):
        profile = [{"angle_deg": a, "levels_db": [v]*8} for a, v in ((0, 0), (90, -10), (180, -20))]
        value = model.revolve(profile, 45, "up", "db")
        selected = {(r["azimuth_deg"], r["elevation_deg"]): r["levels_db"] for r in value["directions"]}
        self.assertEqual(len(selected), 26)
        for actual, expected in ((selected[0, 90][0], 0), (selected[0, 45][0], -5),
                                 (selected[90, 0][0], -10), (selected[0, -90][0], -20)):
            self.assertAlmostEqual(actual, expected)
        value = model.revolve(profile, 45, "front", "energy")
        row = next(r for r in value["directions"] if r["azimuth_deg"] == 45 and r["elevation_deg"] == 0)
        self.assertEqual(row["levels_db"][0], -2.6)
        value = pattern()
        levels = {r["azimuth_deg"]: r["levels_db"][0] for r in value["directions"] if r["elevation_deg"] == 0}
        self.assertEqual([levels[a] for a in (0, 90, 180)], [0, -6, -18])
        with self.assertRaises(ModelError):
            model.revolve(profile + [profile[0]])
        with self.assertRaises(ModelError):
            model.sectors([120, 45], [[0]*8]*3)

    def test_invalid_data_and_unknowns_are_not_zero(self):
        value = model.import_table('azimuth_deg,elevation_deg,63,1000\n0,0,0,\n90,0,-6,-8\n')
        self.assertEqual(value["directions"][0]["levels_db"], [0, None])
        for change in (lambda d: d["frequencies_hz"].reverse(),
                       lambda d: d["directions"].append(deepcopy(d["directions"][0])),
                       lambda d: d["directions"][0]["levels_db"].__setitem__(0, float('nan'))):
            invalid = deepcopy(value)
            change(invalid)
            with self.assertRaises(ModelError):
                model.validate_definition(invalid)
        draft = model.stack_draft(.8, 150, 30)
        self.assertTrue(model.upright(draft))
        self.assertTrue(all(v is None for row in draft["directions"] for v in row["levels_db"]))
        self.assertEqual(draft["generator"]["rules"]["numerical_status"], "not_implemented")
        corrupt = pattern()
        corrupt["generator"]["parameters"]["levels_db"] = [None]
        with self.assertRaises(ModelError):
            model.validate_definition(corrupt)
        corrupt = deepcopy(value)
        corrupt["frequencies_hz"][0] = 10 ** 1000
        with self.assertRaises(ModelError):
            model.validate_definition(corrupt)


class DirectionalityStoreTests(ModelFixture):
    def test_roundtrip_revision_conflict_export_and_reference_protection(self):
        identity = self.store.add_directivity("Vent", pattern())
        record = self.store.directivities()[0]
        stamp = self.store.metadata()["modified_at"]
        self.store.save_directivity(identity, "Vent renamed", record["definition"], record["revision"])
        self.assertGreater(self.store.metadata()["modified_at"], stamp)
        with self.assertRaisesRegex(ModelError, "another editor"):
            self.store.save_directivity(identity, "Overwrite", pattern(), record["revision"])
        with self.assertRaisesRegex(ModelError, "already exists"):
            self.store.add_directivity("VENT RENAMED", pattern())
        layer_record = self.store.add_layer("point_source", "Sources")
        layer = load_layer(self.store, layer_record)
        feature = add_feature(layer, LAYER_TYPES["point_source"])
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("directivity_id"), identity)
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("directivity_orientation"), json.dumps(model.IDENTITY))
        self.assertTrue(layer.commitChanges())
        variant_id = self.store.variants()[0]["variant_id"]
        self.store.save_variant(variant_id, "Default", definition([layer_record["layer_id"]]))
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.directivities()[0]["definition"], pattern())
        self.assertEqual(reopened.directivities()[0]["revision"], 2)
        self.assertEqual(reopened.export_variant(variant_id)["directivities"], reopened.directivities())
        with self.assertRaisesRegex(ModelError, "assigned"):
            reopened.delete_directivity(identity)
        del layer
        reopened.archive_layer(layer_record["layer_id"])
        self.assertTrue(reopened.directivity_usage(identity)[0]["archived"])
        with self.assertRaisesRegex(ModelError, "assigned"):
            reopened.delete_directivity(identity)

    def test_original_clf_is_portable_but_never_fabricated_as_direction_data(self):
        path = self.path.parent / "original.cf2"
        data = b"Synthetic binary attachment for authoring tests\x00\xff"
        path.write_bytes(data)
        value = model.attach_clf(path)
        self.assertFalse(value["attachment"]["decoded"])
        self.assertEqual(value["directions"], [])
        self.store.add_directivity("Imported draft", value)
        path.unlink()
        reopened = ModelStore.open(self.path)
        import base64
        self.assertEqual(base64.b64decode(reopened.directivities()[0]["definition"]["attachment"]["data_base64"]), data)
        value["attachment"]["sha256"] = "wrong"
        with self.assertRaises(ModelError):
            model.validate_definition(value)

    def test_schema8_upgrade_backs_up_and_preserves_source_values(self):
        record = self.store.add_layer("point_source", "Existing sources")
        layer = load_layer(self.store, record)
        add_feature(layer, LAYER_TYPES["point_source"])
        self.assertTrue(layer.commitChanges())
        del layer
        table = record["table_name"]
        with self.store.connection() as db:
            db.execute(f'ALTER TABLE "{table}" DROP COLUMN directivity_id')
            db.execute(f'ALTER TABLE "{table}" DROP COLUMN directivity_orientation')
            db.execute("DROP TABLE acalc_directivities")
            db.execute("DELETE FROM gpkg_contents WHERE table_name='acalc_directivities'")
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN infer_z INTEGER NOT NULL DEFAULT 0')
            db.execute("UPDATE acalc_project SET schema_version=8")
            variant = self.store.variants()[0]
            old = variant["definition"]
            old["schema_version"] = 8
            db.execute("UPDATE acalc_variants SET definition_json=?", (json.dumps(old),))
            before = [tuple(r) for r in db.execute(f'SELECT * FROM "{table}"')]
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.metadata()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(reopened.directivities(), [])
        self.assertTrue(Path(reopened.migration_backup).is_file())
        with closing(sqlite3.connect(reopened.migration_backup)) as db:
            self.assertEqual(db.execute("SELECT schema_version FROM acalc_project").fetchone()[0], 8)
            self.assertEqual(db.execute(f'SELECT * FROM "{table}"').fetchall(), before)
        with reopened.connection(readonly=True) as db:
            after = [tuple(r) for r in db.execute(f'SELECT * FROM "{table}"')]
        self.assertEqual([row[:-2] for row in after], [row[:-1] for row in before])
        self.assertTrue(all(row[-2:] == (None, None) for row in after))


class DirectionalityEditorTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.identity = self.store.add_directivity("Vent", pattern())
        self.power = self.store.add_spectrum("Power", spectrum_definition("sound_power", [70]*8))
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.open_project(self.path)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def keep(self, widget):
        self.widgets.append(widget)
        return widget

    def test_library_menu_editor_templates_and_draft_cancel(self):
        self.plugin.dock.show_view("directionality")
        self.assertIs(self.plugin.dock.stack.currentWidget(), self.plugin.dock.directivity_page)
        def create(dialog):
            dialog.name.setText("Manual")
            dialog.table.setRowCount(1)
            QApplication.clipboard().setText("0\t0\t0\t-1\t-2\t-3\t-4\t-5\t-6\t")
            dialog.table.setCurrentCell(0, 0)
            dialog.table.paste_cells()
            dialog.notes.setPlainText("Manual definition notes")
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            return dialog.result()
        with patch.object(DirectivityDialog, "exec", create):
            identity = self.plugin.edit_directivity(create=True)
        record = next(r for r in self.store.directivities() if r["directivity_id"] == identity)
        self.assertEqual(record["definition"]["directions"][0]["levels_db"], [0, -1, -2, -3, -4, -5, -6, None])
        self.assertEqual(record["definition"]["notes"], "Manual definition notes")
        def cancel(dialog):
            dialog.name.setText("Cancelled")
            return QDialog.Rejected
        with patch.object(DirectivityDialog, "exec", cancel):
            self.plugin.edit_directivity(identity=identity)
        self.assertNotIn("Cancelled", [r["name"] for r in self.store.directivities()])
        template = self.keep(DirectivityTemplateDialog())
        template.generate()
        self.assertIsNotNone(template.value, template.error.text())
        self.assertTrue(template.use_button.isEnabled())
        template.axis.setCurrentIndex(1)
        self.assertIsNone(template.value)
        self.assertFalse(template.use_button.isEnabled())
        template.generate()
        self.assertEqual(template.value["principal"]["elevation_deg"], 90)
        template.kind.setCurrentIndex(1)
        template.generate()
        self.assertEqual(template.value["generator"]["kind"], "sectors")
        template.kind.setCurrentIndex(2)
        template.generate()
        self.assertIn("unknown", template.review_label.text())

    def test_manual_edit_precision_add_band_and_template_provenance(self):
        record = self.store.directivities()[0]
        record["definition"]["principal"] = {"azimuth_deg": 12.123456789, "elevation_deg": -1.987654321}
        dialog = self.keep(DirectivityDialog(record=record))
        self.assertEqual(dialog.read_value(), record["definition"])
        dialog.table.item(0, 2).setText("-1.23456789")
        self.assertTrue(dialog.read_value()["generator"]["modified"])
        self.assertEqual(dialog.read_value()["directions"][0]["levels_db"][0], -1.23456789)
        with patch("acalc.directivity_ui.get_decimal", return_value=(3150., True)):
            dialog.add_frequency()
        value = dialog.read_value()
        self.assertEqual(value["frequencies_hz"], [63, 125, 250, 500, 1000, 2000, 3150, 4000, 8000])
        self.assertIsNone(value["directions"][0]["levels_db"][6])
        dialog.table.item(1, 0).setText("0")
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertIn("duplicate", dialog.error.text())

    def test_assignment_apply_cancel_undo_redo_save_reopen(self):
        record = self.store.add_layer("point_source", "Sources")
        self.plugin.reload_layers()
        layer = self.plugin.layers[record["layer_id"]]
        add_feature(layer, LAYER_TYPES["point_source"])
        self.assertTrue(layer.commitChanges())
        feature = next(layer.getFeatures())
        before = bytes(feature.geometry().asWkb())
        def assign(dialog):
            widget = dialog.directivity
            widget.choice.setCurrentIndex(widget.choice.findData(self.identity))
            widget.aim("Up")
            widget.increments["East"].setValue(30)
            widget.rotate("East", 1)
            self.assertIn("60.00", widget.readout.text())
            self.assertIn("180.00", widget.readout.text())
            widget.undo()
            self.assertIn("Principal: Up", widget.readout.text())
            dialog.spectrum_combo.setCurrentIndex(dialog.spectrum_combo.findData(self.power))
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            return dialog.result()
        with patch.object(FeatureDialog, "exec", assign):
            self.assertTrue(self.plugin.edit_feature(layer=layer, feature_id=feature.id()))
        self.assertEqual(layer.getFeature(feature.id())["directivity_id"], self.identity)
        pose = json.loads(layer.getFeature(feature.id())["directivity_orientation"])
        self.assertAlmostEqual(model.transform(pose, [1, 0, 0])[2], 1)
        layer.undoStack().undo()
        self.assertEqual(null_text(layer.getFeature(feature.id())["directivity_id"]), "")
        layer.undoStack().redo()
        self.assertEqual(layer.getFeature(feature.id())["directivity_id"], self.identity)
        self.plugin.commit_layer(layer)
        self.assertEqual(bytes(layer.getFeature(feature.id()).geometry().asWkb()), before)
        self.assertIn("not yet supported", " ".join(self.plugin.validation_errors(require_spectra=True)))
        def cancel(dialog):
            dialog.directivity.choice.setCurrentIndex(0)
            return QDialog.Rejected
        with patch.object(FeatureDialog, "exec", cancel):
            self.assertFalse(self.plugin.edit_feature(layer=layer, feature_id=feature.id()))
        self.assertEqual(layer.getFeature(feature.id())["directivity_id"], self.identity)
        reopened = load_layer(ModelStore.open(self.path), record)
        self.assertEqual(json.loads(next(reopened.getFeatures())["directivity_orientation"]), pose)
        del reopened
        layer.startEditing()
        with self.assertRaisesRegex(ModelError, "undo"):
            self.plugin.delete_directivity()

    def test_world_axes_preview_orbit_and_stack_rules(self):
        widget = self.keep(SourceDirectivityWidget(self.store.directivities(), self.identity))
        widget.resize(850, 600)
        widget.show()
        APP.processEvents()
        widget.aim("North")
        widget.increments["Up"].setValue(90)
        QTest.mouseClick(widget.rotation_buttons["Up", 1], Qt.LeftButton)
        self.assertIn("270.00", widget.readout.text())
        before = deepcopy(widget.pose)
        canvas = widget.preview.canvas
        QTest.mousePress(canvas, Qt.LeftButton, pos=canvas.rect().center())
        end = canvas.rect().center() + QPoint(25, 20)
        QApplication.sendEvent(canvas, QMouseEvent(QEvent.MouseMove, QPointF(end), Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=end)
        self.assertEqual(widget.pose, before)
        self.assertNotEqual(canvas.azimuth, -45)
        self.assertTrue(canvas.world)
        self.assertTrue(canvas.mesh)
        stack = model.stack_draft(.8, 150)
        constrained = self.keep(SourceDirectivityWidget([{"directivity_id": "stack", "name": "Stack", "definition": stack}], None))
        constrained.choice.setCurrentIndex(1)
        self.assertTrue(constrained.aim_buttons["Up"].isEnabled())
        self.assertFalse(constrained.aim_buttons["North"].isEnabled())
        self.assertFalse(constrained.rotation_buttons["East", 1].isEnabled())
        constrained.attributes()
