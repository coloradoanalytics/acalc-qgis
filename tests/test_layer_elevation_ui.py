# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Layer command scope, dialog choices, and the existing pending-save workflow."""

import json
from unittest.mock import patch

from osgeo import gdal
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox
from qgis.core import QgsProject, QgsRasterLayer

from acalc.layer_elevation_dialog import SetLayerZDialog
from acalc.layers import vertex_rows
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES
from acalc.terrain_ui import RasterSelectionDialog
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


class LayerElevationUITests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.records = {key: self.store.add_layer(key, kind.title) for key, kind in LAYER_TYPES.items()}
        self.plugin.open_project(self.path)

    def tearDown(self):
        for layer in self.plugin.layers.values():
            if layer.isEditable():
                layer.rollBack()
        self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def choose(self, key):
        self.plugin.open_layer_properties(self.records[key]["layer_id"])
        return self.plugin.dock.current_layer

    def test_command_visible_only_for_authored_ground_and_enabled_with_objects(self):
        dock = self.plugin.dock
        for key, kind in LAYER_TYPES.items():
            with self.subTest(kind=key):
                layer = self.choose(key)
                self.assertEqual(dock.set_z_values_button.isVisible(), kind.ground_points)
                self.assertFalse(dock.set_z_values_button.isEnabled())
                add_feature(layer, kind)
                dock.refresh_features()
                if kind.ground_points:
                    self.assertTrue(dock.set_z_values_button.isEnabled())
        dock.show_view("project")
        self.assertFalse(dock.set_z_values_button.isVisible())
        self.assertFalse(dock.set_z_values_button.isEnabled())

    def test_dialog_uses_elevation_units_and_requires_raster_only_in_raster_mode(self):
        with self.store.connection() as db:
            db.execute("UPDATE acalc_project SET metadata_json=?", (json.dumps({"elevation_unit": "ft"}),))
        layer = self.choose("point_receiver")
        add_feature(layer, LAYER_TYPES["point_receiver"])
        dialog = SetLayerZDialog(None, self.store, layer)
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.value.suffix().endswith("ft"))
        dialog.value.lineEdit().setText("10 ft")
        dialog.value.interpretText()
        self.assertAlmostEqual(dialog.options()["value"], 3.048)
        dialog.method.setCurrentIndex(1)
        self.assertFalse(dialog.value.isEnabled())
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())
        dialog.method.setCurrentIndex(0)
        self.assertTrue(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())

    def test_raster_choices_share_saved_and_qgis_references(self):
        path = str(self.path.with_suffix(".tif"))
        dataset = gdal.GetDriverByName("GTiff").Create(path, 2, 2, 1, gdal.GDT_Float64)
        dataset.SetProjection(self.store.metadata()["crs_wkt"])
        dataset.SetGeoTransform((0, 1, 0, 2, 0, -1))
        dataset.GetRasterBand(1).Fill(5)
        dataset = None
        QgsProject.instance().addMapLayer(QgsRasterLayer(path, "Survey"))
        self.store.add_elevation_raster(path, "Saved survey")
        dialog = SetLayerZDialog(None, self.store, self.choose("terrain_point"))
        self.addCleanup(dialog.close)
        dialog.method.setCurrentIndex(1)
        self.assertEqual(dialog.raster.count(), 1)
        self.assertEqual(dialog.options()["raster_record"]["path"], path)
        self.assertTrue(dialog.raster.isEnabled())
        self.assertTrue(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())

    def test_choose_raster_file_without_registering_source(self):
        dialog = SetLayerZDialog(None, self.store, self.choose("terrain_point"))
        self.addCleanup(dialog.close)
        dialog.method.setCurrentIndex(1)
        self.assertTrue(dialog.choose_raster.isEnabled())
        record = {'name': 'Survey', 'path': str(self.path.with_suffix('.tif')), 'band': 1}
        with patch.object(RasterSelectionDialog, 'exec', return_value=QDialog.Accepted), \
                patch.object(RasterSelectionDialog, 'selected_raster', return_value=record):
            dialog.choose_raster.click()
        self.assertEqual(dialog.options(), {'raster_record': record})
        self.assertTrue(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())
        self.assertEqual(self.store.elevation_rasters(), [])

    def test_menu_applies_all_objects_despite_selection_and_filter_then_undoes(self):
        kind = LAYER_TYPES["point_receiver"]
        layer = self.choose(kind.key)
        add_feature(layer, kind, "Visible")
        add_feature(layer, kind, "Hidden")
        self.assertTrue(layer.commitChanges())
        original = {f.id(): bytes(f.geometry().asWkb()) for f in layer.getFeatures()}
        layer.selectByIds([next(iter(original))])
        self.plugin.dock.filter.setText("Visible")

        def accept(dialog):
            dialog.value.setValue(22.5)
            return QDialog.Accepted

        with patch.object(SetLayerZDialog, "exec", accept):
            self.plugin.dock.set_z_values_button.trigger()
        APP.processEvents()
        self.assertEqual([vertex_rows(f.geometry(), kind)[0][3] for f in layer.getFeatures()], [22.5, 22.5])
        self.assertTrue(layer.isModified())
        self.assertTrue(self.plugin.dock.save_edits_button.isEnabled())
        self.assertEqual(layer.selectedFeatureIds(), [next(iter(original))])
        layer.undoStack().undo()
        self.assertEqual({f.id(): bytes(f.geometry().asWkb()) for f in layer.getFeatures()}, original)

    def test_cancel_does_not_start_editing_and_error_can_be_corrected(self):
        kind = LAYER_TYPES["building"]
        layer = self.choose(kind.key)
        feature = add_feature(layer, kind)
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("height_mode"), "absolute")
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("height_value"), 1510)
        self.assertTrue(layer.commitChanges())
        with patch.object(SetLayerZDialog, "exec", return_value=QDialog.Rejected):
            self.plugin.set_layer_z_values()
        self.assertFalse(layer.isEditable())
        calls = []

        def accept(dialog):
            calls.append(dialog)
            if len(calls) == 1:
                dialog.value.setValue(2000)
            else:
                self.assertIn("below", dialog.error.text())
                self.assertFalse(layer.isEditable())
                dialog.value.setValue(1500)
            return QDialog.Accepted

        with patch.object(SetLayerZDialog, "exec", accept):
            self.plugin.set_layer_z_values()
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0], calls[1])
        self.assertEqual({row[3:] for row in vertex_rows(next(layer.getFeatures()).geometry(), kind)}, {(1500, 10)})
