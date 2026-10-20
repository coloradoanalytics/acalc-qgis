# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Draw-on-map tests using real QGIS capture tools and Qt mouse events."""

from unittest.mock import patch

from qgis.PyQt.QtCore import QEvent, QPoint, Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsRectangle
from qgis.gui import QgsMapToolPan

from acalc.dialogs import FeatureDialog
from acalc.layers import validate_layer, vertex_rows
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES
from tests.test_model import APP, FakeIface, ModelFixture


class DigitizingTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.iface.window.show()
        self.canvas = self.iface.mapCanvas()
        self.pan = QgsMapToolPan(self.canvas)
        self.canvas.setMapTool(self.pan)
        APP.processEvents()

    def tearDown(self):
        self.canvas.stopRendering()
        self.canvas.setLayers([])
        if self.plugin.dock:
            self.plugin.unload()
        # processEvents alone does not deliver DeferredDelete outside exec().
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.canvas.unsetMapTool(self.pan)
        self.pan.deleteLater()
        if self.iface.cad is not None:
            self.iface.cad.deleteLater()
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.iface.window.close()
        self.iface.window.deleteLater()
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()
        super().tearDown()

    def setup_layer(self, key):
        if not any(record["feature_type"] == key for record in self.store.layers()):
            self.store.add_layer(key, LAYER_TYPES[key].title)
        if self.plugin.store is None:
            self.plugin.open_project(self.path)
        else:
            self.plugin.reload_layers()
        self.plugin.dock.show_view(key)
        layer = self.plugin.selected_layer()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.canvas.setLayers([layer])
        self.transform = QgsCoordinateTransform(layer.crs(), self.canvas.mapSettings().destinationCrs(), self.plugin.project)
        center = self.transform.transform(QgsPointXY(500000, 4400000))
        self.canvas.setExtent(QgsRectangle(center.x() - 100, center.y() - 100, center.x() + 100, center.y() + 100))
        APP.processEvents()
        return layer

    def click(self, x, y, button=Qt.LeftButton):
        point = self.transform.transform(QgsPointXY(x, y))
        pixel = self.canvas.getCoordinateTransform().transform(point)
        QTest.mouseClick(self.canvas.viewport(), button, Qt.NoModifier, QPoint(round(pixel.x()), round(pixel.y())))

    def draw(self, kind):
        self.click(499980, 4399980)
        if kind.geometry != "Point":
            self.click(500020, 4399980)
            self.click(500020, 4400020)
            if kind.geometry == "Polygon":
                self.click(499980, 4400020)
            self.click(499980, 4400020, Qt.RightButton)

    def test_all_layer_types_use_acalc_dialog_and_add_one_undoable_feature(self):
        for kind in LAYER_TYPES.values():
            self.store.add_layer(kind.key, kind.title)
        native_dialog_action = []
        self.iface.add_action.triggered.connect(lambda: native_dialog_action.append(True))
        for kind in LAYER_TYPES.values():
            with self.subTest(kind=kind.key):
                layer = self.setup_layer(kind.key)
                original_suppression = layer.editFormConfig().suppress()
                self.plugin.draw_feature()
                captured = []

                def accept(dialog):
                    self.assertIn("Acalc properties", dialog.windowTitle())
                    self.assertEqual(layer.featureCount(), 0)  # Still only a sketch.
                    x_column = 0
                    self.assertAlmostEqual(float(dialog.table.item(0, x_column).text()), 499980, delta=1)
                    self.assertAlmostEqual(float(dialog.table.item(0, x_column + 1).text()), 4399980, delta=1)
                    z_column = x_column + 2
                    for row in range(dialog.table.rowCount()):
                        if kind.ground_points:
                            dialog.table.item(row, z_column).setText(str(1500 + row * .5))
                        if kind.height and kind.ground_points:
                            dialog.table.item(row, z_column + 1).setText(str(1.5 + row))
                    if kind.key == "area_receiver":
                        self.assertEqual(dialog.columns, ["X", "Y"])
                        self.assertFalse(hasattr(dialog, "ground_mode"))
                        dialog.height_m.setValue(4.25)
                    if kind.key == "trees":
                        self.assertEqual(dialog.columns, ["X", "Y"])
                        self.assertFalse(hasattr(dialog, "ground_mode"))
                        self.assertEqual(dialog.canopy_height.value(), 10)
                        dialog.canopy_height.setValue(14)
                    dialog.name.setText("Drawn " + kind.singular)
                    captured.append(dialog.table.rowCount())
                    dialog.validate()
                    return dialog.result()

                with patch.object(FeatureDialog, "exec", accept):
                    self.draw(kind)
                    APP.processEvents()
                    self.assertEqual(captured, [{"Point": 1, "LineString": 3, "Polygon": 4}[kind.geometry]])
                    self.assertEqual(layer.featureCount(), 1)
                    self.assertEqual(validate_layer(layer, kind), [])
                    feature = next(layer.getFeatures())
                    rows = vertex_rows(feature.geometry(), kind)
                    self.assertEqual([row[3] for row in rows], [1500 + i * .5 if kind.ground_points else 0 for i in range(len(rows))])
                    if kind.key == "area_receiver":
                        self.assertEqual(feature["height_m"], 4.25)
                        self.assertEqual([row[4] for row in rows], [4.25]*len(rows))
                    if kind.key == "trees":
                        self.assertEqual(feature["canopy_height"], 14)
                        self.assertEqual([row[4] for row in rows], [0]*len(rows))
                    self.assertEqual(layer.undoStack().count(), 1)
                    layer.undoStack().undo()
                    self.assertEqual(layer.featureCount(), 0)
                    layer.undoStack().redo()
                    APP.processEvents()
                    self.assertEqual(layer.featureCount(), 1)
                    self.assertEqual(len(captured), 1)  # Redo must not reopen properties.
                self.assertEqual(layer.editFormConfig().suppress(), original_suppression)
                self.plugin.commit_layer(layer)
                self.assertIsNone(self.plugin.digitizer.tool)
                self.assertIs(self.canvas.mapTool(), self.pan)
                self.assertEqual(next(layer.getFeatures())["name"], "Drawn " + kind.singular)
        self.assertEqual(native_dialog_action, [])

    def test_cancel_discards_sketch_and_repeated_draw_does_not_duplicate_dialogs(self):
        layer = self.setup_layer("point_receiver")
        self.plugin.draw_feature()
        tool = self.plugin.digitizer.tool
        self.plugin.draw_feature()
        self.assertIs(self.plugin.digitizer.tool, tool)
        opened = []

        def reject(dialog):
            opened.append(dialog.name.text())
            return QDialog.Rejected

        with patch.object(FeatureDialog, "exec", reject):
            self.draw(LAYER_TYPES["point_receiver"])
            APP.processEvents()
            self.assertEqual(layer.featureCount(), 0)
            self.assertFalse(layer.isModified())
            self.assertEqual(layer.undoStack().count(), 0)
            self.assertIs(self.canvas.mapTool(), tool)
            self.draw(LAYER_TYPES["point_receiver"])
            APP.processEvents()
        self.assertEqual(len(opened), 2)

    def test_queued_capture_is_canceled_on_tool_switch_and_unload(self):
        layer = self.setup_layer("point_source")
        self.plugin.draw_feature()
        with patch.object(FeatureDialog, "exec") as dialog:
            self.draw(LAYER_TYPES["point_source"])
            self.assertIsNotNone(self.plugin.digitizer.pending_geometry)
            self.canvas.setMapTool(self.pan)
            APP.processEvents()
            dialog.assert_not_called()
            self.assertIsNone(self.plugin.digitizer.tool)
            self.assertEqual(layer.featureCount(), 0)
            self.plugin.draw_feature()
            self.draw(LAYER_TYPES["point_source"])
            self.plugin.unload()
            APP.processEvents()
            dialog.assert_not_called()
            self.assertIs(self.canvas.mapTool(), self.pan)
            self.assertEqual(layer.featureCount(), 0)

    def test_escape_and_layer_switch_cancel_unfinished_sketch(self):
        layer = self.setup_layer("barrier")
        self.plugin.draw_feature()
        with patch.object(FeatureDialog, "exec") as dialog:
            self.click(499980, 4399980)
            QTest.keyClick(self.canvas, Qt.Key_Escape)
            APP.processEvents()
            dialog.assert_not_called()
            self.assertEqual(layer.featureCount(), 0)
            self.assertIsNotNone(self.plugin.digitizer.tool)
            self.click(499980, 4399980)
            self.iface.setActiveLayer(None)
            APP.processEvents()
            self.assertIsNone(self.plugin.digitizer.tool)
            self.assertIs(self.canvas.mapTool(), self.pan)
            dialog.assert_not_called()
