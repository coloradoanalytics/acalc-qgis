# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Building draft geometry and real Qt preview interactions."""

import unittest

from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt
from qgis.PyQt.QtGui import QMouseEvent, QWheelEvent
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QDialog, QLineEdit
from qgis.core import QgsFeature, QgsField, QgsFields, QgsGeometry

from acalc.building_preview import BuildingPreview
from acalc.dialogs import FeatureDialog
from acalc.layers import vertex_rows
from acalc.schema import LAYER_TYPES
from tests.test_model import APP


class BuildingPreviewTests(unittest.TestCase):
    ROWS = ((0, 0, 0, 10, 3), (0, 10, 0, 12, 4), (0, 0, 10, 14, 5))

    def keep(self, widget):
        self.addCleanup(APP.processEvents)
        self.addCleanup(widget.close)
        return widget

    def dialog(self, rows=None, **kwargs):
        dialog = self.keep(FeatureDialog(None, LAYER_TYPES["building"], "m", **kwargs))
        rows = self.ROWS if rows is None else rows
        dialog.table.setRowCount(0)
        for row in rows:
            dialog.add_values(row)
        return dialog

    def preview(self, rows=None, scale=1.0):
        preview = self.keep(BuildingPreview())
        preview.resize(520, 430)
        preview.set_building(self.ROWS if rows is None else rows, scale)
        return preview

    def elevations(self, preview):
        count = len(preview.vertices) // 2
        return ([point[2] for point in preview.vertices[:count]],
                [point[2] for point in preview.vertices[count:]])

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

    def test_explicit_vertices_and_horizontal_units_preserve_vertical_metres(self):
        rows = ((0, 500000, 4400000, 100, 3),
                (0, 500020, 4400000, 102, 4),
                (0, 500000, 4400030, 104, 5))
        preview = self.preview(rows, 0.3048)
        base, roof = self.elevations(preview)
        self.assertEqual(sorted(base), [100, 102, 104])
        self.assertEqual(sorted(roof), [103, 106, 109])
        for axis, expected in ((0, 6.096), (1, 9.144)):
            values = [point[axis] for point in preview.vertices]
            self.assertAlmostEqual(max(values) - min(values), expected, places=8)
        self.assertFalse(preview.message)

    def test_height_modes_and_committed_coordinate_edits_update_immediately(self):
        dialog = self.dialog()
        preview = dialog.building_preview
        self.assertEqual(sorted(self.elevations(preview)[1]), [13, 16, 19])
        dialog.height_value.setValue(8)
        for mode, roof in (("lowest", 18), ("highest", 22), ("average", 20)):
            dialog.height_mode.setCurrentIndex(dialog.height_mode.findData(mode))
            self.assertEqual(self.elevations(preview)[1], [roof] * 3)
        dialog.height_mode.setCurrentIndex(dialog.height_mode.findData("individual"))
        self.assertEqual(sorted(self.elevations(preview)[1]), [13, 16, 19])
        dialog.table.item(0, 2).setText("20")
        dialog.table.item(0, 3).setText("7")
        self.assertEqual(sorted(self.elevations(preview)[0]), [12, 14, 20])
        self.assertEqual(sorted(self.elevations(preview)[1]), [16, 19, 27])
        dialog.table.item(1, 0).setText("25")
        xs = [point[0] for point in preview.vertices]
        self.assertEqual(max(xs) - min(xs), 25)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        self.assertEqual(vertex_rows(dialog.geometry, dialog.kind)[0], (0, 0, 0, 20, 7))

    def test_concave_nonplanar_roof_has_expected_faces_after_rotation(self):
        # An L footprint whose six different roof heights make diagonal choices
        # observable. These four triangles tile its area of seven square metres.
        roof = ((0, 0, 2), (4, 0, 3), (4, 1, 4), (1, 1, 5), (1, 4, 6), (0, 4, 7))
        expected = {frozenset(roof[i] for i in triangle) for triangle in
                    ((0, 1, 2), (0, 2, 3), (5, 0, 3), (3, 4, 5))}
        rows = [(0, x, y, 0, top) for x, y, top in roof]
        preview = self.preview(rows)
        for c, s in ((1, 0), (0, 1), (-1, 0), (.6, .8)):
            preview.set_building([(r, c*x-s*y+30, s*x+c*y-15, z, h) for r,x,y,z,h in rows])
            actual = {frozenset(preview.vertices[i] for i in triangle)
                      for triangle in preview.roof_triangles}
            transformed = {frozenset((c*x-s*y+30, s*x+c*y-15, z) for x,y,z in triangle)
                           for triangle in expected}
            self.assertEqual(actual, transformed)

    def test_typing_waits_for_commit_and_escape_preserves_preview(self):
        dialog = self.dialog()
        original = list(dialog.building_preview.vertices)
        editor = self.edit_cell(dialog, 1, 0, "25")
        self.assertEqual(dialog.table.item(1, 0).text(), "10")
        self.assertEqual(list(dialog.building_preview.vertices), original)
        self.assertIsNone(dialog.geometry)
        QTest.keyClick(editor, Qt.Key_Escape)
        APP.processEvents()
        self.assertEqual(list(dialog.building_preview.vertices), original)
        self.assertEqual(dialog.table.item(1, 0).text(), "10")
        editor = self.edit_cell(dialog, 1, 0, "24")
        self.assertEqual(list(dialog.building_preview.vertices), original)
        QTest.keyClick(editor, Qt.Key_Return)
        APP.processEvents()
        self.assertEqual(dialog.table.item(1, 0).text(), "24")
        xs = [point[0] for point in dialog.building_preview.vertices]
        self.assertEqual(max(xs) - min(xs), 24)

    def test_tab_and_click_away_commit_coordinate_changes(self):
        for commit in ("tab", "click away"):
            with self.subTest(commit=commit):
                dialog = self.dialog()
                original = dialog.building_preview.vertices
                editor = self.edit_cell(dialog, 1, 0, "25")
                self.assertEqual(dialog.building_preview.vertices, original)
                if commit == "tab":
                    QTest.keyClick(editor, Qt.Key_Tab)
                else:
                    QTest.mouseClick(dialog.name, Qt.LeftButton)
                APP.processEvents()
                self.assertEqual(dialog.table.item(1, 0).text(), "25")
                xs = [point[0] for point in dialog.building_preview.vertices]
                self.assertEqual(max(xs) - min(xs), 25)

    def test_ground_editor_recalculates_shared_roof_only_after_commit(self):
        dialog = self.dialog(horizontal_scale=0.5)
        dialog.height_value.setValue(8)
        dialog.height_mode.setCurrentIndex(dialog.height_mode.findData("average"))
        heights = [dialog.table.item(row, 3).text() for row in range(3)]
        editor = self.edit_cell(dialog, 0, 2, "16")
        self.assertEqual(self.elevations(dialog.building_preview)[1], [20, 20, 20])
        self.assertEqual([dialog.table.item(row, 3).text() for row in range(3)], heights)
        self.assertEqual(dialog.table.item(0, 2).text(), "10")
        xs = [point[0] for point in dialog.building_preview.vertices]
        self.assertEqual(max(xs) - min(xs), 5)
        QTest.keyClick(editor, Qt.Key_Escape)
        APP.processEvents()
        self.assertEqual(self.elevations(dialog.building_preview)[1], [20, 20, 20])
        editor = self.edit_cell(dialog, 0, 2, "16")
        QTest.keyClick(editor, Qt.Key_Return)
        APP.processEvents()
        self.assertEqual(self.elevations(dialog.building_preview)[1], [22, 22, 22])
        self.assertEqual([float(dialog.table.item(row, 3).text()) for row in range(3)],
                         [22, 22, 22])

    def test_shared_height_typing_waits_for_edit_commit(self):
        dialog = self.dialog()
        dialog.height_value.setValue(8)
        dialog.height_mode.setCurrentIndex(dialog.height_mode.findData("average"))
        dialog.show()
        APP.processEvents()
        editor = dialog.height_value.lineEdit()
        editor.setFocus()
        editor.selectAll()
        QTest.keyClicks(editor, "18")
        self.assertEqual(dialog.height_value.value(), 8)
        self.assertEqual(self.elevations(dialog.building_preview)[1], [20, 20, 20])
        QTest.keyClick(editor, Qt.Key_Tab)
        APP.processEvents()
        self.assertEqual(dialog.height_value.value(), 18)
        self.assertEqual(self.elevations(dialog.building_preview)[1], [30, 30, 30])

    def test_preview_uses_explicit_ground_and_preserves_authored_z(self):
        dialog = self.dialog()
        self.assertEqual(dialog.ground_mode.findData("terrain"), -1)
        self.assertEqual(sorted(self.elevations(dialog.building_preview)[0]), [10, 12, 14])
        self.assertEqual([dialog.table.item(row, 2).text() for row in range(3)],
                         ["10", "12", "14"])
        dialog.height_value.setValue(8)
        dialog.height_mode.setCurrentIndex(dialog.height_mode.findData("average"))
        self.assertEqual(self.elevations(dialog.building_preview)[1], [20, 20, 20])
        self.assertNotIn("flat base", dialog.preview_status.text())

    def test_invalid_and_incomplete_drafts_clear_preview_and_recover(self):
        dialog = self.dialog()
        preview = dialog.building_preview
        for text in ("", "-", "nan", "inf"):
            with self.subTest(text=text):
                dialog.table.item(1, 0).setText(text)
                self.assertFalse(preview.vertices)
                self.assertTrue(preview.message)
                dialog.table.item(1, 0).setText("10")
                self.assertEqual(len(preview.vertices), 6)
        dialog.height_mode.setCurrentIndex(dialog.height_mode.findData("lowest"))
        dialog.height_value.setValue(1)
        self.assertFalse(preview.vertices)
        self.assertIn("below", preview.message)
        dialog.height_value.setValue(8)
        self.assertEqual(len(preview.vertices), 6)

    def test_crossing_repeated_and_collinear_perimeters_do_not_draw_false_solids(self):
        for points in (((0, 0), (10, 10), (0, 10), (10, 0)),
                       ((0, 0), (10, 0), (10, 0), (0, 10)),
                       ((0, 0), (5, 0), (10, 0))):
            with self.subTest(points=points):
                dialog = self.dialog([(0, x, y, 0, 5) for x, y in points])
                self.assertFalse(dialog.building_preview.vertices)
                self.assertTrue(dialog.building_preview.message)

    def test_vertex_add_remove_and_reorder_refresh_the_draft(self):
        dialog = self.dialog()
        dialog.add_row()
        self.assertFalse(dialog.building_preview.vertices)
        # Add a distinct fourth corner after the original triangular perimeter.
        for column, value in enumerate((-5, 5, 10, 3)):
            dialog.table.item(3, column).setText(str(value))
        self.assertEqual(len(dialog.building_preview.vertices), 8)
        dialog.table.selectRow(3)
        dialog.remove_row()
        self.assertEqual(len(dialog.building_preview.vertices), 6)
        original_vertices = set(dialog.building_preview.vertices)
        dialog.table.selectRow(1)
        dialog.move_row(-1)
        self.assertEqual(dialog.table.item(0, 0).text(), "10")
        self.assertEqual(set(dialog.building_preview.vertices), original_vertices)
        dialog.table.selectRow(0)
        dialog.remove_row()
        self.assertFalse(dialog.building_preview.vertices)
        self.assertTrue(dialog.building_preview.message)

    def test_drag_wheel_and_reset_operate_on_rendered_preview_and_preserve_draft(self):
        dialog = self.dialog()
        dialog.show()
        APP.processEvents()
        preview = dialog.building_preview
        start = (preview.azimuth, preview.elevation, preview.zoom)
        original = preview.grab().toImage()
        center = preview.rect().center()
        QTest.mousePress(preview, Qt.LeftButton, pos=center)
        move = QMouseEvent(QEvent.MouseMove, QPointF(center + QPoint(70, 35)),
                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(preview, move)
        QTest.mouseRelease(preview, Qt.LeftButton, pos=center + QPoint(70, 35))
        APP.processEvents()
        self.assertNotEqual((preview.azimuth, preview.elevation), start[:2])
        self.assertNotEqual(preview.grab().toImage(), original)
        wheel = QWheelEvent(QPointF(center), QPointF(preview.mapToGlobal(center)),
                            QPoint(), QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                            Qt.NoScrollPhase, False)
        QApplication.sendEvent(preview, wheel)
        self.assertGreater(preview.zoom, start[2])
        camera = (preview.azimuth, preview.elevation, preview.zoom)
        dialog.table.item(0, 3).setText("9")
        self.assertEqual((preview.azimuth, preview.elevation, preview.zoom), camera)
        QTest.mouseClick(dialog.reset_preview_button, Qt.LeftButton)
        self.assertEqual((preview.azimuth, preview.elevation, preview.zoom), start)
        self.assertEqual(dialog.table.item(0, 3).text(), "9")

    def test_legacy_hole_is_not_silently_drawn_as_a_solid_building(self):
        fields = QgsFields()
        for name in ("name", "notes"):
            fields.append(QgsField(name))
        feature = QgsFeature(fields)
        feature["name"] = "Legacy courtyard"
        feature["notes"] = ""
        feature.setGeometry(QgsGeometry.fromWkt(
            "POLYGON ZM ((0 0 0 5, 10 0 0 5, 10 10 0 5, 0 10 0 5, 0 0 0 5),"
            "(2 2 0 5, 2 4 0 5, 4 4 0 5, 4 2 0 5, 2 2 0 5))"))
        original = bytes(feature.geometry().asWkb())
        dialog = self.keep(FeatureDialog(None, LAYER_TYPES["building"], "m", feature))
        self.assertFalse(dialog.building_preview.vertices)
        self.assertIn("interior boundaries", dialog.building_preview.message)
        dialog.reject()
        self.assertEqual(bytes(feature.geometry().asWkb()), original)


if __name__ == "__main__":
    unittest.main()
