# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Independent geometry and real editor checks for explicit squaring."""

import math
import unittest
from unittest.mock import patch

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog, QMessageBox

from acalc.building_geometry import square_footprint
from acalc.dialogs import FeatureDialog
from acalc.layers import vertex_rows
from acalc.schema import LAYER_TYPES, ModelError
from tests.test_model import APP


class SquareCornersTests(unittest.TestCase):
    def assert_square(self, points):
        for i, (x, y) in enumerate(points):
            a = (points[i - 1][0] - x, points[i - 1][1] - y)
            b = (points[(i + 1) % len(points)][0] - x,
                 points[(i + 1) % len(points)][1] - y)
            self.assertGreater(math.hypot(*a), 0)
            self.assertAlmostEqual((a[0] * b[0] + a[1] * b[1]) /
                                   (math.hypot(*a) * math.hypot(*b)), 0, delta=1e-8)

    def dialog(self, points):
        dialog = FeatureDialog(None, LAYER_TYPES["building"], "m")
        self.addCleanup(dialog.close)
        dialog.table.setRowCount(0)
        for i, (x, y) in enumerate(points):
            dialog.add_values((0, x, y, 10 + i, 6 + i))
        return dialog

    def table_values(self, dialog):
        return [[dialog.table.item(row, col).text() for col in range(4)]
                for row in range(dialog.table.rowCount())]

    def test_independently_specified_trapezoid_fit(self):
        points = [(0, 0), (10, 0), (9, 6), (1, 6)]
        self.assertEqual(square_footprint(points), [(0.5, 0), (9.5, 0), (9.5, 6), (0.5, 6)])

    def test_rotated_concave_and_convex_perimeters_keep_center_and_order(self):
        for points in ([(0, 0), (10, 0), (9, 6), (1, 6)],
                       [(0, 0), (10, 0.2), (10.1, 4), (4, 4.1), (4.1, 10), (0.1, 10)]):
            for reverse in (False, True):
                ordered = list(reversed(points)) if reverse else points
                for start in range(len(points)):
                    shifted = ordered[start:] + ordered[:start]
                    angle = math.radians(37)
                    c, s = math.cos(angle), math.sin(angle)
                    rotated = [(500000 + c * x - s * y, 4400000 + s * x + c * y) for x, y in shifted]
                    result = square_footprint(rotated)
                    self.assert_square(result)
                    self.assertEqual(len(result), len(points))
                    for axis in (0, 1):
                        self.assertAlmostEqual(sum(p[axis] for p in result),
                                               sum(p[axis] for p in rotated), delta=1e-7)
                    for actual, expected in zip(result, rotated):
                        self.assertLess(math.dist(actual, expected), 1)

    def test_ten_degree_cutoff_is_inclusive(self):
        for degrees in (9.999, 10, 10.001):
            offset = 6 * math.tan(math.radians(degrees))
            points = [(0, 0), (10, 0), (10 + offset, 6), (offset, 6)]
            if degrees <= 10:
                self.assert_square(square_footprint(points))
            else:
                with self.assertRaisesRegex(ModelError, "within 10°"):
                    square_footprint(points)

    def test_already_square_is_unchanged_and_invalid_inputs_fail(self):
        points = [(0, 0), (10, 0), (10, 6), (0, 6)]
        self.assertEqual(square_footprint(points), points)
        for bad in ([], points[:3], points[:2] + points[1:],
                    [(0, 0), (10, 10), (0, 10), (10, 0)],
                    [(0, 0), (10, 0), (float("nan"), 6), (0, 6)]):
            with self.subTest(points=bad), self.assertRaises(ModelError):
                square_footprint(bad)

    def test_button_updates_preview_and_apply_preserves_vertical_values(self):
        dialog = self.dialog([(0, 0), (10, 0), (9, 6), (1, 6)])
        original = self.table_values(dialog)
        dialog.show()
        APP.processEvents()
        self.assertLess(dialog.square_corners_button.geometry().left(), dialog.raster_button.geometry().left())
        QTest.mouseClick(dialog.square_corners_button, Qt.LeftButton)
        values = self.table_values(dialog)
        self.assertEqual([r[2:] for r in values], [r[2:] for r in original])
        self.assert_square([(float(r[0]), float(r[1])) for r in values])
        self.assertTrue(dialog.building_preview.vertices)
        self.assertIsNone(dialog.geometry)  # Draft only, until Apply.
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        rows = vertex_rows(dialog.geometry, dialog.kind)
        self.assertEqual([(r[1], r[2]) for r in rows], [(0.5, 0), (9.5, 0), (9.5, 6), (0.5, 6)])
        self.assertEqual([(r[3], r[4]) for r in rows], [(10, 6), (11, 7), (12, 8), (13, 9)])

    def test_rejection_leaves_every_cell_unchanged(self):
        for points in ([(0, 0), (10, 0), (8, 6), (0, 6)],
                       [(0, 0), (10, 0), ("", 6), (0, 6)]):
            dialog = self.dialog(points)
            before = self.table_values(dialog)
            with patch.object(QMessageBox, "information") as message:
                dialog.square_corners_button.click()
            self.assertEqual(self.table_values(dialog), before)
            message.assert_called_once()
            self.assertIn("Square corners isn't available", message.call_args.args[2])
            self.assertIsNone(dialog.geometry)

    def test_absorption_controls_belong_to_material_tab_and_cancel_discards_fit(self):
        dialog = self.dialog([(0, 0), (10, 0), (9, 6), (1, 6)])
        dialog.show()
        APP.processEvents()
        material = dialog.tabs.widget(1)
        self.assertEqual(dialog.tabs.tabText(1), "Surface material")
        for control in (dialog.absorption_coefficient, dialog.use_default_reflection):
            self.assertTrue(material.isAncestorOf(control))
            self.assertFalse(control.isVisible())
        dialog.square_corners()
        dialog.tabs.setCurrentWidget(material)
        APP.processEvents()
        self.assertTrue(dialog.absorption_coefficient.isVisible())
        self.assertTrue(dialog.use_default_reflection.isVisible())
        dialog.reject()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertIsNone(dialog.geometry)
