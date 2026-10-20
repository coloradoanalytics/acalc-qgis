# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Terrain object navigation and transient raster selection."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from osgeo import gdal
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox
from qgis.core import QgsProject, QgsRasterLayer

from acalc.plugin import AcalcPlugin
from acalc.terrain_ui import TerrainPage, RasterSelectionDialog
from tests.test_model import APP, FakeIface, ModelFixture


class TerrainPageTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.controller = SimpleNamespace(store=self.store, run=lambda callback: callback(),
                                          terrain_changed=Mock(), generate_raster_terrain=Mock(return_value=None), engine_runner=SimpleNamespace(active=False))
        self.page = TerrainPage(self.controller)
        self.addCleanup(self.page.close)
        self.page.refresh()

    def test_no_group_or_density_controls(self):
        self.assertEqual([self.page.tabs.tabText(i) for i in range(self.page.tabs.count())], ['Points', 'Lines', 'Generated terrain', 'Ground Absorption', 'Trees'])
        self.assertFalse(hasattr(self.page, 'groups_page'))
        self.assertFalse(hasattr(self.page.rasters_page, 'sample_step'))
        dialog = RasterSelectionDialog(self.page)
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).isEnabled())

    def test_generate_uses_qgis_raster_without_registering_it(self):
        path = str(self.path.with_suffix('.tif'))
        dataset = gdal.GetDriverByName('GTiff').Create(path, 2, 2, 1, gdal.GDT_Float64)
        dataset.SetProjection(self.store.metadata()['crs_wkt'])
        dataset.SetGeoTransform((0, 1, 0, 2, 0, -1))
        dataset.GetRasterBand(1).Fill(5)
        dataset = None
        layer = QgsRasterLayer(path, 'Survey')
        QgsProject.instance().addMapLayer(layer)
        with patch.object(RasterSelectionDialog, 'exec', return_value=QDialog.Accepted):
            self.page.rasters_page.generate_terrain()
        self.assertEqual(self.store.elevation_rasters(), [])
        self.assertEqual(self.page.rasters_page.terrains.count(), 0)
        self.controller.generate_raster_terrain.assert_called_once_with({'name': 'Survey', 'path': path, 'band': 1})
        self.assertTrue(self.page.rasters_page.generate_button.isEnabled())
        self.assertFalse(self.page.rasters_page.zoom_button.isEnabled())

    def test_browse_and_cancel_leave_project_unchanged(self):
        dialog = RasterSelectionDialog(self.page)
        self.addCleanup(dialog.close)
        path = str(self.path.with_suffix('.tif'))
        with patch('acalc.terrain_ui.QFileDialog.getOpenFileName', return_value=(path, '')):
            dialog.browse()
        self.assertEqual(dialog.selected_raster(), {'name': self.path.stem, 'path': path, 'band': 1})
        self.assertTrue(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).isEnabled())
        with patch.object(RasterSelectionDialog, 'exec', return_value=QDialog.Rejected):
            self.page.rasters_page.generate_terrain()
        self.controller.generate_raster_terrain.assert_not_called()
        self.assertEqual(self.store.elevation_rasters(), [])
        self.assertEqual(self.store.layers(), [])
        self.assertEqual(QgsProject.instance().mapLayers(), {})


class TerrainDockTests(ModelFixture):
    def test_nested_object_editors_and_variant_layer_membership(self):
        records = [self.store.add_layer(kind, kind) for kind in
                   ('terrain_point', 'terrain_line', 'ground_absorption', 'trees')]
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(plugin.unload)
        plugin.open_project(self.path)
        dock = plugin.dock
        dock.show_view('terrain')
        self.assertEqual(dock.current_kind, 'terrain_point')
        self.assertIs(dock.feature_page.parent(), dock.terrain_page.point_host)
        dock.terrain_page.tabs.setCurrentIndex(1)
        self.assertEqual(dock.current_kind, 'terrain_line')
        self.assertIs(dock.feature_page.parent(), dock.terrain_page.line_host)
        dock.terrain_page.tabs.setCurrentIndex(2)
        self.assertIs(dock.stack.currentWidget(), dock.terrain_page)
        for index, kind in ((3, 'ground_absorption'), (4, 'trees')):
            dock.terrain_page.tabs.setCurrentIndex(index)
            self.assertEqual(dock.current_kind, kind)
            self.assertEqual(dock.heading.text(), 'Terrain')
            self.assertIs(dock.feature_page.parent(), dock.terrain_page.hosts[kind])
            dock.show_view('project')
            dock.show_view('terrain')
            self.assertEqual(dock.current_kind, kind)
            plugin.open_layer_properties(records[index - 1]['layer_id'])
            self.assertIs(dock.stack.currentWidget(), dock.terrain_page)
            self.assertEqual(dock.current_kind, kind)
        dock.show_view('variants')
        self.assertFalse(hasattr(dock, 'terrain_members'))
        self.assertEqual(len(list(dock.member_items())), 4)
        from qgis.PyQt.QtCore import Qt
        next(item for item in dock.member_items()
             if item.data(0, Qt.UserRole) == records[0]['layer_id']).setCheckState(0, Qt.Checked)
        self.assertEqual(self.store.variants()[0]['definition']['layer_ids'], [records[0]['layer_id']])
        plugin.calculation_busy_changed(True)
        self.assertFalse(dock.terrain_page.isEnabled())
        plugin.calculation_busy_changed(False)
        self.assertTrue(dock.terrain_page.isEnabled())
        APP.processEvents()
