# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Building ground authoring, committed edits, and GeoPackage round trips."""

import unittest

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog, QLineEdit
from qgis.core import QgsFeature, QgsField, QgsFields

from acalc.dialogs import FeatureDialog
from acalc.layers import feature_value, geometry_from_rows, load_layer, vertex_rows
from acalc.schema import LAYER_TYPES, new_id
from acalc.storage import ModelStore
from tests.test_model import APP, ModelFixture, add_feature


BUILDING = LAYER_TYPES["building"]
ROWS = ((0, 0, 0, 10, 3), (0, 10, 0, 12, 4), (0, 0, 10, 14, 5))


class BuildingGroundModesTests(unittest.TestCase):
    def dialog(self, rows=ROWS, feature=None):
        dialog = FeatureDialog(None, BUILDING, "m", feature)
        self.addCleanup(APP.processEvents)
        self.addCleanup(dialog.close)
        if feature is None:
            dialog.table.setRowCount(0)
            for row in rows:
                dialog.add_values(row)
        return dialog

    def set_mode(self, dialog, mode):
        index = dialog.ground_mode.findData(mode)
        self.assertGreaterEqual(index, 0)
        dialog.ground_mode.setCurrentIndex(index)

    def z_texts(self, dialog):
        return [dialog.table.item(row, 2).text() for row in range(dialog.table.rowCount())]

    def elevations(self, dialog):
        points = dialog.building_preview.vertices
        count = len(points) // 2
        return ([point[2] for point in points[:count]], [point[2] for point in points[count:]])

    def assert_editable(self, dialog, editable_rows):
        for row in range(dialog.table.rowCount()):
            flags = dialog.table.item(row, 2).flags()
            self.assertEqual(bool(flags & Qt.ItemIsEditable), row in editable_rows)
            self.assertEqual(bool(flags & Qt.ItemIsEnabled), row in editable_rows)
            # Ground choices do not disable footprint coordinates.
            for col in (0, 1):
                self.assertTrue(dialog.table.item(row, col).flags() & Qt.ItemIsEditable)

    def edit_cell(self, dialog, row, column, text):
        dialog.show()
        APP.processEvents()
        item = dialog.table.item(row, column)
        dialog.table.setCurrentItem(item)
        dialog.table.editItem(item)
        APP.processEvents()
        editor = next(editor for editor in dialog.table.findChildren(QLineEdit)
                      if editor.isVisible())
        editor.selectAll()
        QTest.keyClicks(editor, text)
        APP.processEvents()
        return editor

    def test_explicit_options_enable_only_the_ground_cells_the_user_can_edit(self):
        dialog = self.dialog()
        self.assertEqual([(dialog.ground_mode.itemText(i), dialog.ground_mode.itemData(i))
                          for i in range(dialog.ground_mode.count())],
                         [("Specify per point", "per_point"), ("Uniform", "uniform")])
        self.assertEqual(dialog.ground_mode.currentData(), "per_point")
        for mode, editable in (("per_point", {0, 1, 2}), ("uniform", {0}),
                               ("per_point", {0, 1, 2})):
            self.set_mode(dialog, mode)
            self.assert_editable(dialog, editable)
            self.assertFalse(hasattr(dialog, "infer_ground"))
            self.assertNotIn("infer_z", dialog.object_attributes())
            self.assertEqual(dialog.object_attributes()["ground_mode"], mode)

    def test_per_point_committed_edit_changes_only_its_own_ground_elevation(self):
        dialog = self.dialog()
        editor = self.edit_cell(dialog, 1, 2, "17")
        self.assertEqual(self.z_texts(dialog), ["10", "12", "14"])
        QTest.keyClick(editor, Qt.Key_Return)
        APP.processEvents()
        self.assertEqual(self.z_texts(dialog), ["10", "17", "14"])
        self.assertEqual(sorted(self.elevations(dialog)[0]), [10, 14, 17])

    def test_uniform_copies_ground_and_recalculates_roof_only_after_cell_commit(self):
        for commit in ("enter", "tab", "click away"):
            with self.subTest(commit=commit):
                dialog = self.dialog()
                dialog.height_value.setValue(8)
                dialog.height_mode.setCurrentIndex(dialog.height_mode.findData("average"))
                self.set_mode(dialog, "uniform")
                self.assertEqual(self.z_texts(dialog), ["10"] * 3)
                self.assertEqual(self.elevations(dialog), ([10] * 3, [18] * 3))
                before_heights = [dialog.table.item(row, 3).text() for row in range(3)]
                editor = self.edit_cell(dialog, 0, 2, "16.25")
                self.assertEqual(self.z_texts(dialog), ["10"] * 3)
                self.assertEqual(self.elevations(dialog), ([10] * 3, [18] * 3))
                self.assertEqual([dialog.table.item(row, 3).text() for row in range(3)], before_heights)
                if commit == "click away":
                    QTest.mouseClick(dialog.name, Qt.LeftButton)
                else:
                    QTest.keyClick(editor, Qt.Key_Return if commit == "enter" else Qt.Key_Tab)
                APP.processEvents()
                self.assertEqual(self.z_texts(dialog), ["16.25"] * 3)
                self.assertEqual(self.elevations(dialog), ([16.25] * 3, [24.25] * 3))
                self.assertEqual([float(dialog.table.item(row, 3).text()) for row in range(3)], [24.25] * 3)
                self.assert_editable(dialog, {0})

    def test_escape_cancels_uniform_ground_edit_and_preserves_every_vertex(self):
        dialog = self.dialog()
        self.set_mode(dialog, "uniform")
        original = list(dialog.building_preview.vertices)
        editor = self.edit_cell(dialog, 0, 2, "99")
        QTest.keyClick(editor, Qt.Key_Escape)
        APP.processEvents()
        self.assertEqual(self.z_texts(dialog), ["10"] * 3)
        self.assertEqual(list(dialog.building_preview.vertices), original)

    def test_mode_switches_retain_the_ground_values_shown_to_the_user(self):
        dialog = self.dialog()
        self.set_mode(dialog, "uniform")
        self.assertEqual(self.z_texts(dialog), ["10"] * 3)
        self.set_mode(dialog, "per_point")
        self.assertEqual(self.z_texts(dialog), ["10"] * 3)
        self.assert_editable(dialog, {0, 1, 2})
        dialog.table.item(1, 2).setText("18")
        self.assertEqual(dialog.ground_mode.findData("terrain"), -1)
        self.assertEqual(self.z_texts(dialog), ["10", "18", "10"])
        self.assertEqual(sorted(self.elevations(dialog)[0]), [10, 10, 18])

    def test_uniform_added_removed_and_reordered_vertices_keep_one_editable_z(self):
        dialog = self.dialog()
        self.set_mode(dialog, "uniform")
        dialog.table.item(0, 2).setText("23.5")
        dialog.add_row()
        self.assertEqual(self.z_texts(dialog), ["23.5"] * 4)
        self.assert_editable(dialog, {0})
        dialog.table.selectRow(1)
        dialog.move_row(-1)
        self.assertEqual(dialog.table.item(0, 0).text(), "10")
        self.assertEqual(self.z_texts(dialog), ["23.5"] * 4)
        self.assert_editable(dialog, {0})
        dialog.table.selectRow(0)
        dialog.remove_row()
        self.assertEqual(self.z_texts(dialog), ["23.5"] * 3)
        self.assert_editable(dialog, {0})
        dialog.table.item(0, 2).setText("25")
        self.assertEqual(self.z_texts(dialog), ["25"] * 3)

    def test_invalid_uniform_ground_is_shared_and_cannot_save_a_stale_preview(self):
        dialog = self.dialog()
        self.set_mode(dialog, "uniform")
        dialog.table.item(0, 2).setText("-")
        self.assertEqual(self.z_texts(dialog), ["-"] * 3)
        self.assertFalse(dialog.building_preview.vertices)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        dialog.table.item(0, 2).setText("15")
        self.assertEqual(self.z_texts(dialog), ["15"] * 3)
        self.assertEqual(self.elevations(dialog)[0], [15] * 3)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        self.assertEqual([row[3] for row in vertex_rows(dialog.geometry, BUILDING)], [15] * 3)

    def test_unresolved_ground_requires_values_without_changing_stored_geometry(self):
        fields = QgsFields()
        for name in ("name", "notes", "ground_mode"):
            fields.append(QgsField(name))
        feature = QgsFeature(fields)
        feature["name"], feature["notes"], feature["ground_mode"] = "Needs elevations", "", "unresolved"
        feature.setGeometry(geometry_from_rows(ROWS, BUILDING))
        original = bytes(feature.geometry().asWkb())
        dialog = self.dialog(feature=feature)
        self.assertEqual(dialog.ground_mode.currentData(), "per_point")
        self.assertEqual(self.z_texts(dialog), [""] * 3)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertEqual(bytes(feature.geometry().asWkb()), original)


class BuildingGroundPersistenceTests(ModelFixture):
    def test_save_and_reopen_preserve_all_modes_even_when_per_point_z_values_match(self):
        record = self.store.add_layer("building", "Buildings")
        layer = load_layer(self.store, record)
        self.assertTrue(layer.startEditing())
        for mode in ("per_point", "uniform"):
            dialog = FeatureDialog(None, BUILDING, "m")
            self.addCleanup(dialog.close)
            dialog.table.setRowCount(0)
            for ring, x, y, z, height in ROWS:
                # Equal explicit elevations must not be interpreted as Uniform.
                dialog.add_values((ring, x, y, 35, height))
            dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData(mode))
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            feature = QgsFeature(layer.fields())
            feature["object_id"], feature["name"], feature["notes"], feature["enabled"] = new_id(), mode, "", 1
            for name, value in dialog.object_attributes().items():
                feature[name] = value
            feature.setGeometry(dialog.geometry)
            self.assertTrue(layer.addFeature(feature))
        self.assertTrue(layer.commitChanges(), layer.commitErrors())
        reopened = load_layer(ModelStore.open(self.path), record)
        for feature in reopened.getFeatures():
            with self.subTest(mode=feature["name"]):
                mode = feature["name"]
                self.assertEqual(feature["ground_mode"], mode)
                self.assertNotIn("infer_z", feature.fields().names())
                self.assertEqual([row[3] for row in vertex_rows(feature.geometry(), BUILDING)], [35] * 3)
                dialog = FeatureDialog(None, BUILDING, "m", feature)
                self.addCleanup(dialog.close)
                self.assertEqual(dialog.ground_mode.currentData(), mode)
                self.assertEqual(dialog.object_attributes()["ground_mode"], mode)


if __name__ == "__main__":
    unittest.main()
