# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Source buildings share object geometry but exclusively own attached-source UI."""

import json
from unittest.mock import patch

from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt
from qgis.PyQt.QtGui import QMouseEvent, QWheelEvent
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QDialog, QLabel

from acalc.dialogs import FeatureDialog
from acalc.face_sources_ui import FaceSourcesDialog
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES
from acalc.spectra import spectrum_definition
from tests.test_face_sources import ROWS, rectangle
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


class SourceBuildingUiTests(ModelFixture):
    def dialog(self, kind, **kwargs):
        dialog = FeatureDialog(None, LAYER_TYPES[kind], "m", **kwargs)
        self.addCleanup(dialog.close)
        dialog.table.setRowCount(0)
        for row in ROWS:
            dialog.add_values(row)
        return dialog

    def test_regular_buildings_keep_geometry_materials_without_source_controls(self):
        material = self.store.add_spectrum("Brick", spectrum_definition("absorption", [.2] * 8))
        ordinary = self.dialog("building", spectra=self.store.spectra())
        source = self.dialog("source_building", spectra=self.store.spectra())
        for dialog in (ordinary, source):
            self.assertTrue(dialog.building_preview.vertices)
            self.assertTrue(hasattr(dialog, "square_corners_button"))
            self.assertEqual(dialog.height_mode.count(), 6)
            self.assertEqual(dialog.absorption_combo.findData(material), 1)
            dialog.height_mode.setCurrentIndex(dialog.height_mode.findData("absolute"))
            dialog.height_value.setValue(20)
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        self.assertEqual(ordinary.geometry.asWkb(), source.geometry.asWkb())
        self.assertEqual(ordinary.building_preview.vertices, source.building_preview.vertices)
        self.assertFalse(hasattr(ordinary, "face_sources_button"))
        self.assertFalse(hasattr(ordinary, "unattached_sources_button"))
        self.assertNotIn("source", ordinary.building_preview.accessibleDescription().lower())
        self.assertFalse(any("sources" in label.text().lower() for label in ordinary.findChildren(QLabel)))
        self.assertNotIn("face_sources", ordinary.object_attributes())
        self.assertEqual(source.object_attributes()["face_sources"], "[]")
        self.assertIn("Sources", source.building_preview.accessibleDescription())
        self.assertTrue(hasattr(source, "face_sources_button"))
        ordinary.building_preview.set_selected_face("roof")
        with patch.object(FaceSourcesDialog, "exec") as execute:
            ordinary.edit_face_sources()
            execute.assert_not_called()

    def test_preview_navigation_preserves_geometry_and_only_source_buildings_select_faces(self):
        for kind in ("building", "source_building"):
            with self.subTest(kind=kind):
                dialog = self.dialog(kind)
                dialog.show()
                APP.processEvents()
                preview = dialog.building_preview
                original_vertices = preview.vertices
                center = preview.rect().center()
                face = preview.face_at(center)
                self.assertIsNotNone(face)
                selections = []
                preview.face_selected.connect(selections.append)
                QTest.mouseClick(preview, Qt.LeftButton, pos=center)
                if kind == "building":
                    self.assertIsNone(preview.selected_face)
                    self.assertEqual(selections, [])
                    preview.set_selected_face(face)
                    self.assertIsNone(preview.selected_face)
                    self.assertNotIn("select", preview.toolTip().lower())
                    self.assertNotIn("select", preview.accessibleDescription().lower())
                    self.assertFalse(any("selected face" in label.text().lower()
                                         for label in dialog.findChildren(QLabel)))
                else:
                    self.assertEqual(preview.selected_face, face)
                    self.assertEqual(selections, [face])
                    self.assertTrue(dialog.face_sources_button.isVisible())
                selected = preview.selected_face
                initial_camera = (preview.azimuth, preview.elevation, preview.zoom)
                for button, modifiers in ((Qt.MiddleButton, Qt.NoModifier),
                                          (Qt.LeftButton, Qt.ShiftModifier)):
                    origin = preview._projection()[2]
                    QTest.mousePress(preview, button, modifiers, pos=center)
                    delta = QPoint(35, 20)
                    QApplication.sendEvent(preview, QMouseEvent(
                        QEvent.MouseMove, QPointF(center + delta), Qt.NoButton, button, modifiers))
                    QTest.mouseRelease(preview, button, modifiers, pos=center + delta)
                    self.assertEqual(preview._projection()[2], origin + QPointF(delta))
                    self.assertEqual((preview.azimuth, preview.elevation, preview.zoom), initial_camera)
                    self.assertEqual(preview.selected_face, selected)
                QTest.mousePress(preview, Qt.LeftButton, pos=center)
                QApplication.sendEvent(preview, QMouseEvent(
                    QEvent.MouseMove, QPointF(center + QPoint(40, 15)),
                    Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
                QTest.mouseRelease(preview, Qt.LeftButton, pos=center + QPoint(40, 15))
                self.assertNotEqual((preview.azimuth, preview.elevation), initial_camera[:2])
                QApplication.sendEvent(preview, QWheelEvent(
                    QPointF(center), QPointF(preview.mapToGlobal(center)), QPoint(), QPoint(0, 120),
                    Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
                self.assertGreater(preview.zoom, initial_camera[2])
                self.assertEqual(preview.vertices, original_vertices)
                QTest.mouseClick(dialog.reset_preview_button, Qt.LeftButton)
                self.assertEqual(preview.pan, QPointF())
                self.assertEqual((preview.azimuth, preview.elevation, preview.zoom), initial_camera)
                self.assertEqual(preview.vertices, original_vertices)
                dialog.close()

    def test_source_building_component_editing_keeps_one_building_object(self):
        source = self.dialog("source_building")
        source.show()
        APP.processEvents()
        source.building_preview.set_selected_face("wall:0:1")
        self.assertTrue(source.face_sources_button.isVisible())
        components = [rectangle(), rectangle(name="Other vent", x_m=5)]

        def accept(dialog):
            for component in components:
                dialog.add_source(component)
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            return dialog.result()

        with patch.object(FaceSourcesDialog, "exec", accept):
            source.edit_face_sources()
        source.validate()
        self.assertEqual(source.result(), QDialog.Accepted, source.error.text())
        self.assertEqual(len(json.loads(source.object_attributes()["face_sources"])), 2)
        self.assertTrue(all(not button.isEnabled() for button in source.vertex_buttons))
        source.building_preview.set_selected_face(None)
        self.assertFalse(source.face_sources_button.isVisible())

    def test_controller_opens_source_building_with_power_materials_and_building_defaults(self):
        material = self.store.add_spectrum("Brick", spectrum_definition("absorption", [.2] * 8))
        power = self.store.add_spectrum("Vent", spectrum_definition("sound_power", [75] * 8, power_basis="total"))
        record = self.store.add_layer("source_building", "Factory sources")
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(iface.window.close)
        self.addCleanup(plugin.unload)
        plugin.open_project(self.path)
        layer = plugin.layers[record["layer_id"]]
        add_feature(layer, LAYER_TYPES["source_building"])
        plugin.commit_layer(layer)
        feature = next(layer.getFeatures())

        def inspect(dialog):
            self.assertEqual(dialog.kind.key, "source_building")
            self.assertEqual({s["spectrum_id"] for s in dialog.face_power_spectra}, {power, material})
            self.assertEqual(set(dialog.absorption_records), {material})
            self.assertEqual(dialog.go_to_button.text(), "Go to Source building")
            self.assertTrue(dialog.use_default_reflection.isChecked())
            dialog.reject()
            return dialog.result()

        with patch.object(FeatureDialog, "exec", inspect):
            plugin.open_feature_properties(layer.id(), feature.id())
        plugin.dock.show_view("source_building")
        self.assertIs(plugin.dock.stack.currentWidget(), plugin.dock.sources_page)
        self.assertEqual(plugin.dock.sources_page.tabs.tabText(3), "Source Buildings")
        self.assertEqual(plugin.dock.current_layer, layer)
