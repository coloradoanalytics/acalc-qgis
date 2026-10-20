# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Building height rules, Qt edit commits, and saved Z/M representations."""

import unittest

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog, QLineEdit
from qgis.core import QgsFeature

from acalc.dialogs import FeatureDialog
from acalc.layers import load_layer, validate_layer, vertex_rows
from acalc.object_geometry import HEIGHT_MODES
from acalc.schema import LAYER_TYPES, new_id
from acalc.storage import ModelStore
from tests.test_model import APP, ModelFixture


BUILDING = LAYER_TYPES["building"]
ROWS = ((0, 0, 0, 10, 3), (0, 10, 0, 12, 4), (0, 0, 10, 14, 5))


def draft(test, rows=ROWS, feature=None):
    dialog = FeatureDialog(None, BUILDING, "m", feature)
    test.addCleanup(APP.processEvents)
    test.addCleanup(dialog.close)
    if feature is None:
        dialog.table.setRowCount(0)
        for row in rows:
            dialog.add_values(row)
    return dialog


def set_mode(dialog, mode):
    dialog.height_mode.setCurrentIndex(dialog.height_mode.findData(mode))


def roof_levels(dialog):
    points = dialog.building_preview.vertices
    return [point[2] for point in points[len(points) // 2:]]


class BuildingHeightModesTests(unittest.TestCase):
    def test_six_modes_and_ground_controls_are_independent(self):
        dialog = draft(self)
        self.assertEqual([dialog.height_mode.itemText(i) for i in range(6)],
                         ["Relative to average Z", "Relative to highest Z", "Relative to lowest Z",
                          "Absolute", "Relative per point", "Absolute per point"])
        for ground, enabled in (("per_point", {0, 1, 2}), ("uniform", {0})):
            dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData(ground))
            for mode in HEIGHT_MODES:
                with self.subTest(ground=ground, height=mode):
                    set_mode(dialog, mode)
                    per_point = mode in ("individual", "absolute_individual")
                    self.assertEqual(dialog.height_value.isEnabled(), not per_point)
                    for row in range(3):
                        self.assertEqual(bool(dialog.table.item(row, 2).flags() & Qt.ItemIsEditable), row in enabled)
                        self.assertEqual(bool(dialog.table.item(row, 3).flags() & Qt.ItemIsEditable), per_point)

    def test_shared_modes_have_flat_roofs_and_save_relative_m(self):
        for mode, h, roof in (("average", 8, 20), ("highest", 8, 22),
                              ("lowest", 8, 18), ("absolute", 30, 30)):
            with self.subTest(mode=mode):
                dialog = draft(self)
                set_mode(dialog, mode)
                dialog.height_value.setValue(h)
                self.assertEqual(roof_levels(dialog), [roof] * 3)
                self.assertEqual(dialog.table.horizontalHeaderItem(3).text(), "Roof Z (m)")
                self.assertEqual([float(dialog.table.item(row, 3).text()) for row in range(3)], [roof] * 3)
                dialog.validate()
                self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                self.assertEqual([row[4] for row in vertex_rows(dialog.geometry, BUILDING)],
                                 [roof - 10, roof - 12, roof - 14])

    def test_absolute_per_point_preserves_roof_when_ground_changes(self):
        dialog = draft(self)
        set_mode(dialog, "absolute_individual")
        self.assertEqual([float(dialog.table.item(row, 3).text()) for row in range(3)], [13, 16, 19])
        dialog.table.item(0, 3).setText("25")
        dialog.table.item(0, 2).setText("20")
        self.assertEqual(roof_levels(dialog), [25, 16, 19])
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        self.assertEqual(vertex_rows(dialog.geometry, BUILDING)[0], (0, 0, 0, 20, 5))
        set_mode(dialog, "individual")
        self.assertEqual([float(dialog.table.item(row, 3).text()) for row in range(3)], [5, 4, 5])
        self.assertEqual(roof_levels(dialog), [25, 16, 19])
        dialog.table.item(0, 2).setText("22")
        self.assertEqual(roof_levels(dialog), [27, 16, 19])

    def test_shared_modes_restore_per_point_values_and_basis(self):
        dialog = draft(self)
        set_mode(dialog, "absolute_individual")
        dialog.table.item(0, 3).setText("24")
        set_mode(dialog, "average")
        dialog.height_value.setValue(20)
        self.assertEqual(roof_levels(dialog), [32] * 3)
        set_mode(dialog, "absolute_individual")
        self.assertEqual(roof_levels(dialog), [24, 16, 19])
        self.assertEqual([float(dialog.table.item(row, 3).text()) for row in range(3)], [24, 16, 19])

    def test_absolute_edit_waits_for_commit_and_escape_cancels(self):
        dialog = draft(self)
        set_mode(dialog, "absolute_individual")
        dialog.show()
        APP.processEvents()
        for commit in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Tab):
            before = roof_levels(dialog)
            item = dialog.table.item(0, 3)
            dialog.table.setCurrentItem(item)
            dialog.table.editItem(item)
            APP.processEvents()
            editor = next(e for e in dialog.table.findChildren(QLineEdit) if e.isVisible())
            editor.selectAll()
            QTest.keyClicks(editor, "30")
            self.assertEqual(roof_levels(dialog), before)
            QTest.keyClick(editor, commit)
            APP.processEvents()
            self.assertEqual(roof_levels(dialog), before if commit == Qt.Key_Escape else [30, 16, 19])

    def test_absolute_roof_below_ground_rejected_but_negative_elevations_allowed(self):
        for mode in ("absolute", "absolute_individual"):
            with self.subTest(mode=mode):
                dialog = draft(self)
                set_mode(dialog, mode)
                dialog.height_value.setValue(9)
                if mode == "absolute_individual":
                    dialog.table.item(0, 3).setText("9")
                self.assertFalse(dialog.building_preview.vertices)
                dialog.validate()
                self.assertEqual(dialog.result(), QDialog.Rejected)
                self.assertIn("below", dialog.error.text())
        dialog = draft(self, [(ring, x, y, -30, 5) for ring, x, y, _, _ in ROWS])
        set_mode(dialog, "absolute")
        dialog.height_value.setValue(-20)
        self.assertEqual(roof_levels(dialog), [-20] * 3)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        self.assertEqual([row[4] for row in vertex_rows(dialog.geometry, BUILDING)], [10] * 3)

    def test_legacy_inferred_absolute_roof_requires_explicit_ground(self):
        from qgis.core import QgsField, QgsFields
        from acalc.layers import geometry_from_rows
        fields = QgsFields()
        for name in ("name", "notes", "ground_mode", "height_mode", "height_value"):
            fields.append(QgsField(name))
        for mode in ("absolute", "absolute_individual"):
            with self.subTest(mode=mode):
                feature = QgsFeature(fields)
                feature["name"], feature["notes"], feature["ground_mode"] = "Legacy", "", "unresolved"
                feature["height_mode"], feature["height_value"] = mode, 30
                feature.setGeometry(geometry_from_rows(ROWS, BUILDING))
                dialog = draft(self, feature=feature)
                self.assertFalse(dialog.building_preview.vertices)
                dialog.validate()
                self.assertEqual(dialog.result(), QDialog.Rejected)
                for row in range(3):
                    dialog.table.item(row, 2).setText("10")
                dialog.validate()
                self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                saved = vertex_rows(dialog.geometry, BUILDING)
                self.assertEqual([row[3] + row[4] for row in saved],
                                 [30] * 3 if mode == "absolute" else [13, 16, 19])
                self.assertNotIn("infer_z", dialog.object_attributes())


class BuildingHeightPersistenceTests(ModelFixture):
    def test_absolute_modes_roundtrip_and_remain_valid_for_export(self):
        record = self.store.add_layer("building", "Buildings")
        layer = load_layer(self.store, record)
        self.assertTrue(layer.startEditing())
        for mode in ("absolute", "absolute_individual"):
            dialog = draft(self)
            set_mode(dialog, mode)
            dialog.height_value.setValue(30)
            if mode == "absolute_individual":
                for row, roof in enumerate((30, 32, 34)):
                    dialog.table.item(row, 3).setText(str(roof))
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            feature = QgsFeature(layer.fields())
            feature["object_id"], feature["name"], feature["enabled"] = new_id(), mode, 1
            for name, value in dialog.object_attributes().items():
                feature[name] = value
            feature.setGeometry(dialog.geometry)
            self.assertTrue(layer.addFeature(feature))
        self.assertFalse(validate_layer(layer, BUILDING))
        self.assertTrue(layer.commitChanges())
        reopened = load_layer(ModelStore.open(self.path), record)
        for feature in reopened.getFeatures():
            dialog = draft(self, feature=feature)
            self.assertEqual(dialog.height_mode.currentData(), feature["name"])
            expected = [30] * 3 if feature["name"] == "absolute" else [30, 32, 34]
            self.assertEqual(roof_levels(dialog), expected)
            self.assertEqual([float(dialog.table.item(row, 3).text()) for row in range(3)], expected)
            self.assertFalse(validate_layer(reopened, BUILDING))


if __name__ == "__main__":
    unittest.main()
