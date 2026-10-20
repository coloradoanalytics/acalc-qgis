# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Object raster sampling, Apply/Cancel and height semantics with real Qt."""

import struct
from unittest.mock import patch

from osgeo import gdal
from qgis.PyQt.QtWidgets import QDialog
from qgis.core import QgsFeature

from acalc.dialogs import FeatureDialog
from acalc.layers import load_layer, vertex_rows
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError, new_id
from tests.test_model import APP, FakeIface, ModelFixture


class RasterObjectTests(ModelFixture):
    def setUp(self):
        super().setUp()
        path = str(self.path.with_suffix('.tif'))
        dataset = gdal.GetDriverByName('GTiff').Create(path, 2, 2, 1, gdal.GDT_Float64)
        dataset.SetProjection(self.store.metadata()['crs_wkt'])
        dataset.SetGeoTransform((0, 10, 0, 20, 0, -10))
        dataset.GetRasterBand(1).WriteRaster(0, 0, 2, 2, struct.pack('4d', 1, 2, 3, 4), buf_type=gdal.GDT_Float64)
        dataset = None
        self.store.add_elevation_raster(path)

    def dialog(self, kind):
        dialog = FeatureDialog(None, kind, 'm', model_wkt=self.store.metadata()['crs_wkt'],
                               elevation_rasters=self.store.elevation_rasters())
        self.addCleanup(dialog.close)
        dialog.table.setRowCount(0)
        points = [(5, 15)] if kind.geometry == 'Point' else [(5, 15), (15, 15)]
        if kind.geometry == 'Polygon':
            points += [(15, 5), (5, 5)]
        for x, y in points:
            dialog.add_values((0, x, y, 0, 6))
        return dialog

    def sample(self, dialog):
        with patch('acalc.terrain_ui.RasterSelectionDialog.exec', return_value=QDialog.Accepted):
            dialog.raster_button.click()

    def test_every_object_samples_and_persists_explicit_ground_without_changing_xy_or_height(self):
        for kind in (kind for kind in LAYER_TYPES.values() if kind.ground_points):
            with self.subTest(kind=kind.key):
                dialog = self.dialog(kind)
                self.assertEqual(dialog.ground_mode.findData('terrain'), -1)
                self.assertTrue(dialog.raster_button.isEnabled())
                self.sample(dialog)
                self.assertEqual(dialog.error.text(), '')
                self.assertFalse(hasattr(dialog, "infer_ground"))
                self.assertEqual(dialog.ground_mode.currentData(), 'per_point')
                dialog.validate()
                self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                rows = vertex_rows(dialog.geometry, kind)
                expected = [1] if kind.geometry == 'Point' else [1, 2]
                if kind.geometry == 'Polygon':
                    expected += [4, 3]
                self.assertEqual([row[3] for row in rows], expected)
                if kind.height:
                    self.assertEqual([row[4] for row in rows], [6] * len(rows))
                record = self.store.add_layer(kind.key, kind.title)
                layer = load_layer(self.store, record)
                layer.startEditing()
                feature = QgsFeature(layer.fields())
                feature['object_id'], feature['name'] = new_id(), 'Sampled'
                for name, value in dialog.object_attributes().items():
                    feature[name] = value
                feature.setGeometry(dialog.geometry)
                layer.addFeature(feature)
                self.assertTrue(layer.commitChanges(), layer.commitErrors())
                reopened = load_layer(self.store, record)
                saved = next(reopened.getFeatures())
                self.assertNotIn('infer_z', saved.fields().names())
                self.assertEqual(vertex_rows(saved.geometry(), kind), rows)

    def test_sampling_exits_uniform_mode_and_preserves_absolute_roofs(self):
        for mode in ('absolute', 'absolute_individual'):
            dialog = self.dialog(LAYER_TYPES['building'])
            dialog.height_mode.setCurrentIndex(dialog.height_mode.findData(mode))
            dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData('uniform'))
            self.sample(dialog)
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            rows = vertex_rows(dialog.geometry, dialog.kind)
            self.assertEqual([row[3] for row in rows], [1, 2, 4, 3])
            self.assertEqual([row[3] + row[4] for row in rows], [6] * 4)

    def test_failure_and_raster_picker_cancel_leave_all_coordinates_unchanged(self):
        dialog = self.dialog(LAYER_TYPES['terrain_line'])
        dialog.table.item(1, 0).setText('1000')
        self.assertEqual(dialog.ground_mode.findData('terrain'), -1)
        before = [dialog.table.item(row, 2).text() for row in range(2)]
        self.sample(dialog)
        self.assertIn('Ground point 2', dialog.error.text())
        self.assertEqual([dialog.table.item(row, 2).text() for row in range(2)], before)
        self.assertFalse(hasattr(dialog, "infer_ground"))
        with patch('acalc.terrain_ui.RasterSelectionDialog.exec', return_value=QDialog.Rejected):
            dialog.raster_button.click()
        self.assertEqual([dialog.table.item(row, 2).text() for row in range(2)], before)
        with self.assertRaises(ModelError):
            dialog.apply_raster_elevations([1, float('nan')])
        self.assertEqual([dialog.table.item(row, 2).text() for row in range(2)], before)

    def test_legacy_inferred_ground_can_be_replaced_by_raster_samples(self):
        kind = LAYER_TYPES['building']
        initial = self.dialog(kind)
        initial.validate()
        record = self.store.add_layer('building', 'Legacy building')
        layer = load_layer(self.store, record)
        feature = QgsFeature(layer.fields())
        feature['name'], feature['ground_mode'] = 'Legacy', 'unresolved'
        feature.setGeometry(initial.geometry)
        dialog = FeatureDialog(None, kind, 'm', feature, model_wkt=self.store.metadata()['crs_wkt'],
                               elevation_rasters=self.store.elevation_rasters())
        self.addCleanup(dialog.close)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertFalse(dialog.building_preview.vertices)
        self.sample(dialog)
        self.assertEqual(sorted(vertex[2] for vertex in dialog.building_preview.vertices[:4]), [1, 2, 3, 4])
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        self.assertNotIn('infer_z', dialog.object_attributes())

    def test_properties_cancel_apply_and_undo(self):
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(plugin.unload)
        record = self.store.add_layer('point_receiver', 'Receiver')
        layer = load_layer(self.store, record)
        layer.startEditing()
        feature = QgsFeature(layer.fields())
        feature['object_id'], feature['name'] = new_id(), 'Receiver'
        from qgis.core import QgsGeometry
        feature.setGeometry(QgsGeometry.fromWkt('POINT ZM (10 10 0 1.5)'))
        layer.addFeature(feature)
        layer.commitChanges()
        plugin.open_project(self.path)
        layer = plugin.layers[record['layer_id']]
        fid = next(layer.getFeatures()).id()
        for accept in (False, True):
            def execute(dialog):
                self.sample(dialog)
                if accept:
                    dialog.validate()
                    return dialog.result()
                return QDialog.Rejected
            with patch.object(FeatureDialog, 'exec', execute):
                plugin.edit_feature(layer=layer, feature_id=fid)
            self.assertEqual(vertex_rows(layer.getFeature(fid).geometry(), LAYER_TYPES['point_receiver'])[0][3], 2.5 if accept else 0)
        layer.undoStack().undo()
        self.assertEqual(vertex_rows(layer.getFeature(fid).geometry(), LAYER_TYPES['point_receiver'])[0][3], 0)
        layer.rollBack()
