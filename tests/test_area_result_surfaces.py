# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Area result presentation with explicit synthetic sample values, not acoustic references."""

import json
import math
import struct
from unittest.mock import patch

from osgeo import gdal
from qgis.PyQt.QtCore import QPoint, QEvent, Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog, QMenu, QMessageBox
from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsRasterDataProvider, QgsVectorLayer
from qgis.gui import QgsMapMouseEvent

from acalc.area_results import NODATA, BAND_NAMES, band_table_names, grouped_results, results_directory, AreaSurface
from acalc.calculation_settings import ENGINE_ID, default_settings
from acalc.dialogs import FeatureDialog
from acalc.plugin import AcalcPlugin
from acalc.result_layers import sync_result_layers, result_layer_tables, result_table_name
from acalc.schema import LAYER_TYPES, ModelError, new_id
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


class AreaResultSurfaceTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.record = self.store.add_layer("area_receiver", "Gardens")
        self.variant = self.store.variants()[0]["variant_id"]
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.open_project(self.path)
        self.area_layer = self.plugin.layers[self.record["layer_id"]]
        self.object_id = self.add_area("Garden")

    def tearDown(self):
        self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def add_area(self, name):
        feature = add_feature(self.area_layer, LAYER_TYPES["area_receiver"], name)
        self.area_layer.changeGeometry(feature.id(), QgsGeometry.fromWkt("POLYGON ZM ((100 0 0 4,106 0 0 4,106 6 0 4,100 6 0 4,100 0 0 4))"))
        self.area_layer.changeAttributeValue(feature.id(), self.area_layer.fields().indexFromName("spacing_m"), 2)
        object_id = str(feature["object_id"])
        self.plugin.commit_layer(self.area_layer)
        return object_id

    def save_run(self, variant=None, empty=False, silent=False, offset=0):
        variant = variant or self.variant
        run_id = new_id()
        project = self.store.metadata()
        grids = {}
        for feature in self.area_layer.getFeatures():
            bounds = feature.geometry().boundingBox()
            spacing = float(feature["spacing_m"])
            grids[str(feature["object_id"])] = (bounds.xMinimum(), bounds.yMinimum(), spacing,
                math.floor(bounds.width()/spacing)+1, math.floor(bounds.height()/spacing)+1)
        with self.store.connection() as db:
            objects = db.execute(f'SELECT object_id,name,geom,spacing_m FROM "{self.record["table_name"]}"').fetchall()
            srs = db.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?", (self.record["table_name"],)).fetchone()[0]
            snapshots = [dict(layer_id=self.record["layer_id"], object_id=row[0], name=row[1], geometry_gpkg_hex=row[2].hex(), spacing_m=row[3], height_m=4) for row in objects]
            metadata = {"receiver_layers": [dict(layer_id=self.record["layer_id"], name="Gardens", feature_type="area_receiver")], "area_receivers": snapshots}
            db.execute("DELETE FROM acalc_calculation_runs WHERE variant_id=?", (variant,))
            db.execute("""INSERT INTO acalc_calculation_runs VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,'[]',?)""",
                (run_id, project["project_id"], variant, "Saved", ENGINE_ID, "0.24.0", "ISO 9613-2:2024",
                 "2026-09-26T00:00:00Z", "2026-09-26T00:00:01Z", "f"*64, project["modified_at"],
                 json.dumps(default_settings()), 1, 0 if empty else sum(cols*rows - int(cols > 1 and rows > 1) for _, _, _, cols, rows in grids.values()), 1, json.dumps(metadata)))
            if not empty:
                for object_id, name, _, _ in objects:
                    xmin, ymin, spacing, columns, rows = grids[object_id]
                    for row in range(rows):
                        for column in range(columns):
                            if (column, row) == (1, 1):
                                continue  # Building exclusion / missing sample stays NoData.
                            level = None if silent else 30 + 10*column + 2*row + offset
                            blob = b"GP\x00\x01" + struct.pack("<iBI4d", srs, 1, 3001, xmin+spacing*column, ymin+spacing*row, 10+row, 4)
                            position = dict(geometry_gpkg_hex=blob.hex(), area_receiver_id=object_id, grid_column=column, grid_row=row)
                            db.execute("INSERT INTO acalc_receiver_results VALUES(?,?,?,?,?,?,?,?)",
                                       (run_id, self.record["layer_id"], new_id(), f"{name} [{column+1}, {row+1}]", json.dumps([level]*8), None, level, json.dumps(position)))
        return run_id

    def save_raster_run(self, variant=None, level=55.0):
        """A current-format run: the engine GeoTIFF beside the model, no sample rows."""
        import numpy as np
        variant = variant or self.variant
        run_id = new_id()
        project = self.store.metadata()
        feature = next(self.area_layer.getFeatures())
        relative = f"{run_id}/Garden.tif"
        path = results_directory(self.store) / relative
        path.parent.mkdir(parents=True)
        dataset = gdal.GetDriverByName("GTiff").Create(str(path), 4, 4, 9, gdal.GDT_Float32)
        dataset.SetGeoTransform((99, 2, 0, 7, 0, -2))
        dataset.SetProjection(project["crs_wkt"])
        pixels = np.full((4, 4), level, dtype=np.float32)
        pixels[0, 0] = NODATA
        for band in range(1, 10):
            dataset.GetRasterBand(band).SetNoDataValue(NODATA)
            dataset.GetRasterBand(band).SetDescription(BAND_NAMES[band - 1])
            dataset.GetRasterBand(band).WriteArray(pixels)
        dataset = None
        with self.store.connection() as db:
            snapshot = dict(layer_id=self.record["layer_id"], object_id=self.object_id, name="Garden",
                            geometry_gpkg_hex=db.execute(f'SELECT geom FROM "{self.record["table_name"]}"').fetchone()[0].hex(),
                            spacing_m=2, height_m=4)
            metadata = {"receiver_layers": [dict(layer_id=self.record["layer_id"], name="Gardens", feature_type="area_receiver")],
                        "area_receivers": [snapshot],
                        "area_rasters": [dict(api_area_receiver_id="api", layer_id=self.record["layer_id"], object_id=self.object_id,
                                              path=relative, receiver_count=15, columns=4, rows=4)]}
            db.execute("DELETE FROM acalc_calculation_runs WHERE variant_id=?", (variant,))
            db.execute("""INSERT INTO acalc_calculation_runs VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,'[]',?)""",
                (run_id, project["project_id"], variant, "Saved", ENGINE_ID, "0.24.0", "ISO 9613-2:2024",
                 "2026-09-26T00:00:00Z", "2026-09-26T00:00:01Z", "f"*64, project["modified_at"],
                 json.dumps(default_settings()), 1, 15, 1, json.dumps(metadata)))
        return path

    def test_engine_raster_is_the_map_layer_restored_and_cleared_with_its_run(self):
        path = self.save_raster_run()
        page = self.plugin.dock.results_page
        page.publish_saved_results(self.variant)
        layer = self.raster()
        self.assertTrue(layer.isValid())
        self.assertEqual(layer.source(), str(path.resolve()))
        self.assertEqual(layer.bandCount(), 9)
        self.assertEqual(layer.dataProvider().sample(QgsPointXY(104, 2), 1)[0], 55)
        with self.store.connection(readonly=True) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'acalc_area_%'").fetchone())
        # A recalculation re-points the same layer at the new run's file.
        layer_id = layer.id()
        newer = self.save_raster_run(level=61)
        page.publish_saved_results(self.variant)
        self.assertEqual(self.raster().id(), layer_id)
        self.assertEqual(self.raster().source(), str(newer.resolve()))
        self.assertEqual(self.raster().dataProvider().sample(QgsPointXY(104, 2), 1)[0], 61)
        self.assertFalse(path.parent.exists())  # The replaced run's folder is pruned.
        self.plugin.close_project()
        self.plugin.open_project(self.path)
        self.assertEqual(self.raster().source(), str(newer.resolve()))
        page = self.plugin.dock.results_page
        page.refresh(self.variant)
        with patch("acalc.plugin.QMessageBox.question", return_value=QMessageBox.Yes):
            page.clear_button.click()
        self.assertNotIn((self.variant, self.record["layer_id"], self.object_id), self.plugin.result_layers)
        self.assertFalse(newer.parent.exists())

    def test_legacy_projected_area_layer_moves_to_the_engine_raster(self):
        self.save_run()
        page = self.plugin.dock.results_page
        page.publish_saved_results(self.variant)
        legacy = self.raster()
        legacy.setName("My garden levels")
        path = self.save_raster_run()
        page.publish_saved_results(self.variant)
        self.assertEqual(legacy.source(), str(path.resolve()))
        self.assertEqual(legacy.name(), "My garden levels")
        self.assertTrue(legacy.isValid())
        self.assertEqual(self.raster().source(), str(path.resolve()))

    def raster(self, object_id=None, variant=None):
        return self.plugin.result_layers[variant or self.variant, self.record["layer_id"], object_id or self.object_id]

    def test_clear_results_removes_only_selected_variant_surfaces(self):
        other = self.store.add_variant("Night")
        self.save_run()
        self.save_run(other)
        page = self.plugin.dock.results_page
        page.publish_saved_results(self.variant)
        removed_id = self.raster().id()
        kept = self.raster(variant=other)
        tables = [band for table, record in result_layer_tables(self.store).items()
                  if record["variant_id"] == self.variant for band in band_table_names(table)]
        with patch("acalc.plugin.QMessageBox.question", return_value=QMessageBox.Yes):
            page.clear_button.click()
        self.assertIsNone(QgsProject.instance().mapLayer(removed_id))
        self.assertIs(self.raster(variant=other), kept)
        self.assertTrue(kept.isValid())
        with self.store.connection(readonly=True) as db:
            for table in tables:
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())
                self.assertIsNone(db.execute("SELECT 1 FROM gpkg_contents WHERE table_name=?", (table,)).fetchone())
                self.assertIsNone(db.execute("SELECT 1 FROM gpkg_tile_matrix WHERE table_name=?", (table,)).fetchone())
        self.plugin.close_project()
        self.plugin.open_project(self.path)
        self.assertEqual({key[0] for key in self.plugin.result_layers}, {other})

    def test_reprojection_preserves_saved_grid_crs_spacing_and_location(self):
        from qgis.core import QgsCoordinateReferenceSystem
        from acalc.project_coordinates import change_coordinates
        self.save_run()
        self.plugin.dock.results_page.publish_saved_results()
        before = gdal.Open(self.raster().source())
        transform, projection = before.GetGeoTransform(), before.GetProjection()
        values = before.ReadAsArray().copy()
        before = None
        target = QgsCoordinateReferenceSystem.fromProj('+proj=utm +zone=13 +datum=NAD83 +units=ft +type=crs')
        change_coordinates(self.store, target, 'ft', QgsProject.instance().transformContext())
        self.plugin.dock.results_page.publish_saved_results()
        after = gdal.Open(self.raster().source())
        self.assertEqual(after.GetGeoTransform(), transform)
        self.assertEqual(after.GetProjection(), projection)
        self.assertTrue((after.ReadAsArray() == values).all())
        self.assertTrue(self.store.latest_calculation_run(self.variant)['stale'])
        after = None

    def test_one_surface_and_one_blank_list_row_with_exact_grid_and_mask(self):
        self.save_run()
        before = self.store.metadata()["modified_at"]
        self.plugin.dock.results_page.publish_saved_results()
        layer = self.raster()
        self.assertIsInstance(layer, QgsRasterLayer)
        self.assertEqual((layer.width(), layer.height()), (4, 4))
        dataset = gdal.Open(layer.source())
        self.assertEqual(dataset.GetGeoTransform(), (99, 2, 0, 7, 0, -2))
        self.assertEqual(dataset.RasterCount, 9)
        self.assertEqual([dataset.GetRasterBand(i).GetDescription() for i in range(1, 10)], list(BAND_NAMES))
        values = dataset.GetRasterBand(1).ReadAsArray()
        self.assertEqual(values[0, 3], 66)
        self.assertEqual(values[3, 0], 30)
        self.assertEqual(values[2, 1], NODATA)
        dataset = None
        self.assertEqual(self.store.metadata()["modified_at"], before)
        table = self.plugin.dock.results_page.table
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 0).text(), "Garden · Area")
        self.assertEqual([table.item(0, c).text() for c in range(1, 10)], [""]*9)
        self.assertEqual(table.item(0, 0).data(256), (self.record["layer_id"], self.object_id))
        with self.store.connection(readonly=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gpkg_contents WHERE table_name LIKE 'acalc_results_%'").fetchone()[0], 0)
        self.plugin.properties_actions.set_surface_mode(layer.id(), "lines")
        self.assertEqual(layer.renderer().type(), "contour")
        self.plugin.refresh_result_layers()
        self.assertEqual(self.raster().renderer().type(), "contour")

    def test_nine_band_pixels_preserve_octaves_nodata_and_export(self):
        from pathlib import Path
        self.save_run()
        levels = [-5, 0, 12.5, None, 31, 42, 53, 64]
        with self.store.connection() as db:
            db.execute("UPDATE acalc_receiver_results SET octave_levels_json=?", (json.dumps(levels),))
        self.plugin.refresh_result_layers()
        layer = self.raster()
        self.assertEqual(layer.bandCount(), 9)
        dataset = gdal.Open(layer.source())
        self.assertEqual(dataset.GetRasterBand(1).ReadAsArray()[3, 0], 30)
        for index, level in enumerate(levels, 2):
            band = dataset.GetRasterBand(index)
            self.assertEqual(band.GetUnitType(), "dB")
            self.assertEqual(band.ReadAsArray()[3, 0], NODATA if level is None else level)
            self.assertEqual(band.ReadAsArray()[2, 1], NODATA)
        export = str(Path(self.directory.name) / "area.tif")
        saved = gdal.Translate(export, dataset, format="GTiff")
        saved = None
        saved = gdal.Open(export)
        self.assertEqual(saved.RasterCount, 9)
        self.assertEqual(saved.GetGeoTransform(), dataset.GetGeoTransform())
        self.assertTrue((saved.ReadAsArray() == dataset.ReadAsArray()).all())
        self.assertEqual(saved.GetRasterBand(9).GetDescription(), "8000 Hz")
        saved = dataset = None
        self.plugin.properties_actions.set_surface_band(layer.id(), 9)
        # Pseudocolor gained inputBand() in 3.38; band() also works on 3.34.
        self.assertEqual(layer.renderer().band(), 9)
        self.plugin.properties_actions.set_surface_mode(layer.id(), "lines")
        self.assertEqual(layer.renderer().inputBand(), 9)
        self.plugin.refresh_result_layers()
        self.assertEqual(layer.renderer().inputBand(), 9)

    def test_octave_table_collision_rolls_back_every_band(self):
        self.save_run()
        self.plugin.refresh_result_layers()
        table = next(iter(result_layer_tables(self.store)))
        child = band_table_names(table)[-1]
        # A different dataset owns this name; publication must not delete it.
        with self.store.connection() as db:
            db.execute("UPDATE gpkg_contents SET description='Unrelated data' WHERE table_name=?", (child,))
        self.save_run(offset=10)
        with self.assertRaisesRegex(ModelError, "already used"):
            sync_result_layers(self.store)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT description FROM gpkg_contents WHERE table_name=?", (child,)).fetchone()[0], "Unrelated data")
        dataset = gdal.Open(self.raster().source())
        self.assertEqual(dataset.GetRasterBand(1).ReadAsArray()[3, 0], 30)
        self.assertEqual(dataset.GetRasterBand(8).ReadAsArray()[3, 0], 30)
        dataset = None

    def test_properties_hover_selects_nearest_existing_sample_and_one_row(self):
        self.save_run()
        self.plugin.refresh_result_layers()
        def inspect(dialog):
            page = dialog.receiver_results
            view = page.area_view
            self.assertIsNotNone(view.surface)
            self.assertEqual(page.table.rowCount(), 1)
            dialog.show()
            APP.processEvents()
            view.canvas.waitWhileRendering()
            pixel = view.canvas.getCoordinateTransform().transform(105.8, 5.9)
            QTest.mouseMove(view.canvas.viewport(), QPoint(round(pixel.x()), round(pixel.y())))
            QTest.qWait(60)
            self.assertEqual(page.table.item(0, 1).text(), "66.0")
            # Hover near an excluded node; select an actual neighbour, not a
            # fabricated interpolated value or the missing cell.
            view.queue_hover(QgsPointXY(102.2, 2.1))
            view.show_hover()
            self.assertEqual(page.table.item(0, 1).text(), "52.0")
            view.queue_hover(QgsPointXY(105.8, 5.9))
            view.show_hover()
            self.assertEqual(page.table.item(0, 1).text(), "66.0")
            self.assertEqual([page.table.item(0, c).text() for c in range(2, 10)], ["66.0"]*8)
            view.mode.setCurrentIndex(1)
            self.assertEqual(view.layer.renderer().type(), "contour")
            self.assertEqual(view.band.count(), 9)
            view.band.setCurrentIndex(8)
            self.assertEqual(view.layer.renderer().inputBand(), 9)
            APP.processEvents()
            view.canvas.waitWhileRendering()
            view.queue_hover(QgsPointXY(90, 0))
            view.show_hover()
            self.assertEqual(page.table.item(0, 1).text(), "")
            self.assertEqual(page.table.rowCount(), 1)
            dialog.reject()
            return QDialog.Rejected
        with patch.object(FeatureDialog, "exec", inspect):
            self.plugin.open_feature_properties(self.raster().id(), None)

    def test_multiple_areas_variants_recalculation_stale_geometry_and_reopen(self):
        other_area = self.add_area("Courtyard")
        other_variant = self.store.add_variant("Night")
        self.save_run()
        self.save_run(other_variant, offset=5)
        self.plugin.refresh_result_layers()
        self.assertEqual(len(self.plugin.result_layers), 4)
        layer = self.raster()
        layer.setOpacity(0.4)
        self.plugin.properties_actions.set_surface_mode(layer.id(), "lines")
        self.save_run(offset=10)
        self.plugin.refresh_result_layers()
        self.assertIs(self.raster(), layer)
        self.assertEqual(layer.opacity(), 0.4)
        self.assertEqual(layer.renderer().type(), "contour")
        dataset = gdal.Open(layer.source())
        self.assertEqual(dataset.GetRasterBand(1).ReadAsArray()[3, 0], 40)
        dataset = None
        feature = next(f for f in self.area_layer.getFeatures() if str(f["object_id"]) == self.object_id)
        self.area_layer.startEditing()
        self.area_layer.changeGeometry(feature.id(), QgsGeometry.fromWkt("POLYGON ZM ((0 0 0 4,2 0 0 4,2 2 0 4,0 0 0 4))"))
        self.plugin.commit_layer(self.area_layer)
        self.plugin.refresh_result_layers()
        self.assertEqual(layer.extent().xMinimum(), 99)
        self.assertNotIn("out of date", layer.name())
        self.plugin.close_project()
        self.plugin.open_project(self.path)
        self.assertEqual(len(self.plugin.result_layers), 4)
        self.assertTrue(self.raster(other_area, other_variant).isValid())
        path = str(self.path.with_suffix(".qgs"))
        self.assertTrue(QgsProject.instance().write(path))
        QgsProject.instance().clear()
        self.assertTrue(QgsProject.instance().read(path))
        APP.processEvents()
        self.assertEqual(len(self.plugin.result_layers), 4)
        self.assertEqual(sum(isinstance(layer, QgsRasterLayer) for layer in QgsProject.instance().mapLayers().values()), 4)

    def test_recalculation_replaces_live_raster_pixels_extent_and_resolution(self):
        other_variant = self.store.add_variant("Night")
        self.save_run()
        self.save_run(other_variant, offset=5)
        self.plugin.refresh_result_layers()
        layer = self.raster()
        other = self.raster(variant=other_variant)
        original_extent = layer.extent()
        # Prime the live GDAL provider's tile cache, just as displaying the map does.
        self.assertEqual(layer.dataProvider().block(1, layer.extent(), 4, 4).value(3, 0), 30)
        self.assertEqual(other.dataProvider().block(1, other.extent(), 4, 4).value(3, 0), 35)
        layer.setOpacity(0.4)
        self.plugin.properties_actions.set_surface_mode(layer.id(), "lines")
        layer.renderer().setContourInterval(3)
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        node.setItemVisibilityChecked(False)
        for wkt, spacing, size, extent, offset in (
            (None, 2, (4, 4), (99, -1, 107, 7), 10),
            ("POLYGON ZM ((200 40 0 4,210 40 0 4,210 50 0 4,200 50 0 4,200 40 0 4))", 2, (6, 6), (199, 39, 211, 51), 20),
            ("POLYGON ZM ((300 20 0 4,308 20 0 4,308 24 0 4,300 24 0 4,300 20 0 4))", 4, (3, 2), (298, 18, 310, 26), 30),
        ):
            with self.subTest(size=size):
                if wkt:
                    feature = next(self.area_layer.getFeatures())
                    self.area_layer.startEditing()
                    self.area_layer.changeGeometry(feature.id(), QgsGeometry.fromWkt(wkt))
                    self.area_layer.changeAttributeValue(feature.id(), self.area_layer.fields().indexFromName("spacing_m"), spacing)
                    self.plugin.commit_layer(self.area_layer)
                run_id = self.save_run(offset=offset)
                self.plugin.refresh_result_layers()
                self.assertIs(self.raster(), layer)
                self.assertTrue(layer.isValid())
                self.assertEqual((layer.width(), layer.height()), size)
                actual = layer.extent()
                self.assertEqual((actual.xMinimum(), actual.yMinimum(), actual.xMaximum(), actual.yMaximum()), extent)
                self.assertEqual(layer.rasterUnitsPerPixelX(), spacing)
                self.assertEqual(layer.rasterUnitsPerPixelY(), spacing)
                block = layer.dataProvider().block(1, actual, *size)
                self.assertEqual(block.value(size[1]-1, 0), 30+offset)
                self.assertTrue(block.isNoData(size[1]-2, 1))
                self.assertEqual(layer.customProperty("acalc/run_id"), run_id)
                self.assertEqual(layer.renderer().type(), "contour")
                self.assertEqual(layer.renderer().contourInterval(), 3)
                self.assertEqual(layer.resamplingStage(), Qgis.RasterResamplingStage.Provider)
                self.assertTrue(layer.dataProvider().isProviderResamplingEnabled())
                resampling = getattr(Qgis, "RasterResamplingMethod", None)
                if resampling is None:
                    resampling = QgsRasterDataProvider.ResamplingMethod
                self.assertEqual(layer.dataProvider().zoomedInResamplingMethod(), resampling.Bilinear)
                self.assertEqual(layer.opacity(), 0.4)
                self.assertFalse(node.itemVisibilityChecked())
                self.assertIs(self.raster(variant=other_variant), other)
                self.assertEqual(other.extent(), original_extent)
                self.assertEqual(other.dataProvider().block(1, original_extent, 4, 4).value(3, 0), 35)
                self.assertEqual(len(result_layer_tables(self.store)), 2)
                self.assertEqual(sum(isinstance(item, QgsRasterLayer) for item in QgsProject.instance().mapLayers().values()), 2)

    def test_renamed_and_duplicate_rasters_update_pixels_extent_and_keep_styles(self):
        from pathlib import Path

        self.save_run()
        self.plugin.refresh_result_layers()
        saved = self.raster()
        saved_id, source, extent, default_name = saved.id(), saved.source(), saved.extent(), saved.name()
        duplicate = saved.clone()
        duplicate.setName("Filled levels")
        duplicate.setOpacity(0.62)
        duplicate_id = duplicate.id()
        QgsProject.instance().addMapLayer(duplicate)
        self.plugin.properties_actions.set_surface_mode(saved.id(), "lines")
        saved.renderer().setContourInterval(3)
        saved.setOpacity(0.31)
        saved.setName("Report contours")
        self.iface.setActiveLayer(saved)
        self.iface.setActiveLayer(duplicate)
        self.plugin.dock.results_page.refresh()
        self.assertEqual(saved.name(), "Report contours")
        for layer in (saved, duplicate):
            self.assertEqual(layer.source(), source)
            self.assertEqual(layer.dataProvider().block(1, extent, 4, 4).value(3, 0), 30)
        feature = next(self.area_layer.getFeatures())
        self.area_layer.startEditing()
        self.area_layer.changeGeometry(feature.id(), QgsGeometry.fromWkt("POLYGON ZM ((200 0 0 4,210 0 0 4,210 10 0 4,200 10 0 4,200 0 0 4))"))
        self.plugin.commit_layer(self.area_layer)
        self.save_run(offset=10)
        self.plugin.refresh_result_layers()
        current = self.raster()
        self.assertIsNot(current, saved)
        self.assertEqual(current.name(), default_name)
        for layer in (current, saved, duplicate):
            # The inline raster definition changes extent; all views still
            # reference the same nine persisted GeoPackage band tables.
            self.assertEqual(layer.source(), current.source())
            self.assertEqual((layer.width(), layer.height()), (6, 6))
            self.assertNotEqual(layer.extent(), extent)
            self.assertEqual(layer.dataProvider().block(1, layer.extent(), 6, 6).value(5, 0), 40)
        self.assertEqual(saved.renderer().contourInterval(), 3)
        self.assertEqual(saved.opacity(), 0.31)
        self.assertEqual(duplicate.renderer().type(), "singlebandpseudocolor")
        self.assertEqual(duplicate.opacity(), 0.62)
        self.assertEqual(len(result_layer_tables(self.store)), 1)
        path = str(Path(self.directory.name) / "shared rasters.qgs")
        self.assertTrue(QgsProject.instance().write(path))
        QgsProject.instance().clear()
        self.assertTrue(QgsProject.instance().read(path))
        saved = QgsProject.instance().mapLayer(saved_id)
        duplicate = QgsProject.instance().mapLayer(duplicate_id)
        self.area_layer = self.plugin.layers[self.record["layer_id"]]
        self.save_run(offset=20)
        self.plugin.refresh_result_layers()
        for layer in (saved, duplicate):
            self.assertEqual(layer.source(), self.raster().source())
            self.assertEqual(layer.dataProvider().block(1, layer.extent(), 6, 6).value(5, 0), 50)
        self.assertEqual(saved.name(), "Report contours")
        self.assertEqual(saved.renderer().contourInterval(), 3)
        self.assertEqual(saved.opacity(), 0.31)
        self.assertEqual(duplicate.name(), "Filled levels")
        self.assertEqual(duplicate.opacity(), 0.62)

    def test_empty_and_silent_areas_remain_objects_and_failed_projection_is_atomic(self):
        self.save_run(empty=True)
        self.plugin.dock.results_page.publish_saved_results()
        self.assertEqual(self.plugin.dock.results_page.table.rowCount(), 1)
        self.assertEqual((self.raster().width(), self.raster().height()), (4, 4))
        self.save_run(silent=True)
        self.plugin.refresh_result_layers()
        run = self.store.latest_calculation_run(self.variant)
        area = grouped_results(self.store, run, self.store.receiver_results(self.variant))[0]
        surface = AreaSurface(area, 1)
        self.assertIsNotNone(surface.nearest(100, 0))
        self.assertIsNone(surface.samples[surface.nearest(100, 0)]["broadband_a_db"])
        prior = result_layer_tables(self.store)
        self.save_run(offset=10)
        with self.store.connection() as db:
            row = db.execute("SELECT metadata_json FROM acalc_calculation_runs").fetchone()
            metadata = json.loads(row[0])
            metadata["area_receivers"][0]["spacing_m"] = 0
            db.execute("UPDATE acalc_calculation_runs SET metadata_json=?", (json.dumps(metadata),))
        with self.assertRaisesRegex(ModelError, "spacing"):
            sync_result_layers(self.store)
        self.assertEqual(result_layer_tables(self.store), prior)

    def test_old_point_projection_is_kept_and_raster_replacement_rolls_back(self):
        run_id = self.save_run()
        table = result_table_name(self.variant, self.record["layer_id"])
        record = dict(acalc_result_layer=4, variant_id=self.variant, receiver_layer_id=self.record["layer_id"], run_id=run_id)
        with self.store.connection() as db:
            srs = db.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?", (self.record["table_name"],)).fetchone()[0]
            db.execute(f'CREATE TABLE "{table}"(fid INTEGER PRIMARY KEY, geom POINT)')
            db.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,description,srs_id) VALUES(?,'features',?,?,?)", (table, table, json.dumps(record), srs))
            db.execute("INSERT INTO gpkg_geometry_columns VALUES(?,'geom','POINT',?,1,1)", (table,srs))
        old = QgsVectorLayer(f"{self.path}|layername={table}", "Old grid points", "ogr")
        QgsProject.instance().addMapLayer(old)
        old_id = old.id()
        self.plugin.refresh_result_layers()
        self.assertIs(QgsProject.instance().mapLayer(old_id), old)
        self.assertIn(table, result_layer_tables(self.store))
        previous = result_layer_tables(self.store)
        self.save_run(offset=10)
        with patch("acalc.result_layers.write_projection", side_effect=ModelError("Cannot write tiles")):
            with self.assertRaisesRegex(ModelError, "tiles"):
                sync_result_layers(self.store)
        self.assertEqual(result_layer_tables(self.store), previous)
        dataset = gdal.Open(self.raster().source())
        self.assertEqual(dataset.GetRasterBand(1).ReadAsArray()[3, 0], 30)

    def test_raster_map_menu_opens_parent_and_switches_display(self):
        self.save_run()
        self.plugin.refresh_result_layers()
        layer = self.raster()
        canvas = self.iface.mapCanvas()
        self.iface.window.show()
        canvas.setDestinationCrs(layer.crs())
        canvas.setLayers([layer])
        canvas.setExtent(layer.extent())
        APP.processEvents()
        pixel = canvas.getCoordinateTransform().transform(104, 4)
        event = QgsMapMouseEvent(canvas, QEvent.MouseButtonPress, QPoint(round(pixel.x()), round(pixel.y())), Qt.RightButton)
        menu = QMenu()
        canvas.contextMenuAboutToShow.emit(menu, event)
        display = next(action.menu() for action in menu.actions() if action.text() == "Area result display")
        next(action for action in display.actions() if action.text() == "Contour lines").trigger()
        self.assertEqual(layer.renderer().type(), "contour")
        with patch.object(self.plugin, "open_receiver_results") as opened:
            next(action for action in menu.actions() if action.text() == "Acalc properties").trigger()
            opened.assert_called_once_with(self.record["layer_id"], self.object_id, self.variant)
        canvas.stopRendering()
