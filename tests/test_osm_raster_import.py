# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Analytic raster elevations and atomic OSM import through real QGIS/GDAL.

The invented footprints and pixel values are authoring regressions, not OSM
survey evidence or ISO acoustic reference cases.
"""

import json
import struct
from unittest.mock import patch

from osgeo import gdal
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox
from qgis.core import QgsCoordinateReferenceSystem, QgsProject, QgsRasterLayer, QgsRectangle

from acalc.layer_import import ImportOptions, import_layer
from acalc.layers import load_layer, validate_layer, vertex_rows
from acalc.osm_import import build_osm_layer
from acalc.osm_import_dialog import OsmImportDialog
from acalc.plugin import AcalcPlugin
from acalc.raster_elevation import RasterElevation
from acalc.schema import LAYER_TYPES, ModelError
from acalc.storage import ModelStore
from acalc.terrain_ui import RasterSelectionDialog
from tests.test_model import APP, FakeIface, ModelFixture


def payload(longitudes=(-105.0, -104.998)):
    elements = []
    for index, x in enumerate(longitudes, 1):
        ring = [(x, 0), (x + .0005, 0), (x + .0005, .0005), (x, .0005), (x, 0)]
        tags = {"building": "yes", "name": f"Building {index}"}
        if index == 1:
            tags["height"] = "12"
        elements.append({"type": "way", "id": index, "tags": tags,
                         "geometry": [{"lon": lon, "lat": lat} for lon, lat in ring]})
    return {"elements": elements}


class OsmRasterImportTests(ModelFixture):
    def raster(self, name="ground", *, bands=1, unit="m", scale=1, offset=0, nodata_from=None):
        path = self.path.with_name(name + ".tif")
        dataset = gdal.GetDriverByName("GTiff").Create(str(path), 8, 4, bands, gdal.GDT_Float64)
        dataset.SetProjection(QgsCoordinateReferenceSystem("EPSG:4326").toWkt())
        dataset.SetGeoTransform((-105.001, .001, 0, .003, 0, -.001))
        # A bilinear surface includes a cross term, so nearest-pixel sampling,
        # flat ground and swapped raster rows cannot reproduce the expectations.
        values = [100 + 5 * x + 13 * y + 4 * x * y
                  if nodata_from is None or x < nodata_from else -9999
                  for y in range(4) for x in range(8)]
        for number in range(1, bands + 1):
            band = dataset.GetRasterBand(number)
            data = values if number == bands else [-2000] * 32
            band.WriteRaster(0, 0, 8, 4, struct.pack("32d", *data), buf_type=gdal.GDT_Float64)
            band.SetNoDataValue(-9999)
            band.SetUnitType(unit)
            band.SetScale(scale)
            band.SetOffset(offset)
        band = dataset = None
        return str(path)

    def options(self, path, band=1):
        return ImportOptions("building", "Raster buildings", name_field="name", notes_field="notes",
                             ground_mode="raster", raster_path=path, raster_band=band, height_field="height_m")

    def database_snapshot(self):
        with self.store.connection(readonly=True) as db:
            tables = [tuple(row) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return tables, self.store.layers(), self.store.variants(), self.store.metadata()

    def iface(self):
        iface = FakeIface()
        iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:26913"))
        iface.canvas.setExtent(QgsRectangle(500000, 0, 500500, 500))
        self.addCleanup(APP.processEvents)
        self.addCleanup(iface.window.close)
        return iface

    def dialog(self):
        dialog = OsmImportDialog(None, self.store, self.iface().canvas)
        self.addCleanup(dialog.close)
        return dialog

    def plugin(self):
        plugin = AcalcPlugin(self.iface())
        plugin.initGui()
        self.addCleanup(plugin.unload)
        plugin.open_project(self.path)
        return plugin

    def test_bilinear_ground_converts_crs_band_scaling_and_feet_without_changing_heights_or_source(self):
        path = self.raster(bands=2, unit="ft", scale=2, offset=10)
        source, _ = build_osm_layer(payload(), default_height=7.5)
        original = [(bytes(f.geometry().asWkb()), f.attributes()) for f in source.getFeatures()]
        with patch("acalc.raster_elevation.gdal.Open", wraps=gdal.Open) as open_raster:
            record, source_count, count = import_layer(self.store, source, self.options(path, 2))
        self.assertEqual((source_count, count), (2, 2))
        open_raster.assert_called_once_with(path, gdal.GA_ReadOnly)
        self.assertEqual([(bytes(f.geometry().asWkb()), f.attributes()) for f in source.getFeatures()], original)
        saved = load_layer(ModelStore.open(self.path), record)
        self.assertEqual(validate_layer(saved, LAYER_TYPES["building"]), [])
        features = list(saved.getFeatures())
        # At footprint 1's vertices, pixel-centre coordinates are (.5,2.5),
        # (1,2.5), (1,2), (.5,2); footprint 2 is shifted two pixel columns.
        expected_ground = ([140, 147.5, 139, 132.5], [170, 177.5, 165, 158.5])
        for feature, raw, height in zip(features, expected_ground, (12, 7.5)):
            self.assertNotIn("infer_z", feature.fields().names())
            self.assertEqual(feature["ground_mode"], "per_point")
            self.assertEqual(feature["height_mode"], "individual")
            self.assertEqual(feature["height_value"], height)
            rows = vertex_rows(feature.geometry(), LAYER_TYPES["building"])
            for row, value in zip(rows, raw):
                self.assertAlmostEqual(row[3], (value * 2 + 10) * .3048, places=6)
                self.assertEqual(row[4], height)
            attributes = json.loads(feature["source_attributes"])
            self.assertEqual(attributes["source_url"], f"https://www.openstreetmap.org/way/{feature['source_fid']}")
            self.assertIn("OpenStreetMap", feature["notes"])
        # The known UTM central-meridian origin proves that sampling did not
        # accidentally leave the imported geometry in the raster/source CRS.
        row = vertex_rows(features[0].geometry(), LAYER_TYPES["building"])[0]
        self.assertAlmostEqual(row[1], 500000, places=5)
        self.assertAlmostEqual(row[2], 0, places=5)
        self.assertEqual(self.store.variants()[0]["definition"]["layer_ids"], [])

    def test_later_building_nodata_or_outside_failure_does_not_create_partial_tables(self):
        self.store.add_layer("building", "Existing buildings")
        cases = (("outside", self.raster(), (-105, -104.98)),
                 ("NoData", self.raster("holes", nodata_from=3), (-105, -104.997)))
        for message, path, longitudes in cases:
            with self.subTest(message=message):
                source, _ = build_osm_layer(payload(longitudes))
                before = self.database_snapshot()
                sampled = []
                original = RasterElevation.elevation

                def track(raster, x, y):
                    value = original(raster, x, y)
                    sampled.append(value)
                    return value

                with patch.object(RasterElevation, "elevation", track), self.assertRaisesRegex(ModelError, message):
                    import_layer(self.store, source, self.options(path))
                self.assertGreaterEqual(len(sampled), 4, "The complete first building must succeed before the failure.")
                self.assertEqual(self.database_snapshot(), before)

    def test_unusable_raster_configuration_never_writes_a_layer(self):
        path = self.raster()
        source, _ = build_osm_layer(payload())
        before = self.database_snapshot()
        for missing_path, band in (("", 1), (path + ".missing", 1), (path, 0), (path, 2)):
            with self.subTest(path=missing_path, band=band), self.assertRaises(ModelError):
                import_layer(self.store, source, self.options(missing_path, band))
            self.assertEqual(self.database_snapshot(), before)

    def test_picker_uses_saved_and_loaded_rasters_and_cancel_preserves_selection(self):
        saved_path = self.raster("saved", bands=2)
        self.store.add_elevation_raster(saved_path, "Saved survey", band=2)
        loaded_path = self.raster("loaded")
        loaded = QgsRasterLayer(loaded_path, "Loaded survey")
        self.assertTrue(loaded.isValid())
        QgsProject.instance().addMapLayer(loaded)
        dialog = self.dialog()
        dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData("raster"))
        self.assertTrue(dialog.raster_button.isEnabled())
        self.assertFalse(dialog.ground_z.isEnabled())
        seen = []

        def select(picker):
            seen.extend(picker.raster.itemData(i) for i in range(picker.raster.count()))
            return QDialog.Accepted

        with patch.object(RasterSelectionDialog, "exec", select):
            dialog.raster_button.click()
        self.assertEqual({r["path"] for r in seen}, {saved_path, loaded_path})
        self.assertEqual((dialog.options().raster_path, dialog.options().raster_band), (saved_path, 2))
        self.assertIn("Saved survey", dialog.raster_label.text())
        chosen = dict(dialog.raster_record)
        with patch.object(RasterSelectionDialog, "exec", return_value=QDialog.Rejected):
            dialog.raster_button.click()
        self.assertEqual(dialog.raster_record, chosen)

        def select_loaded(picker):
            index = next(i for i in range(picker.raster.count()) if picker.raster.itemData(i)["path"] == loaded_path)
            picker.raster.setCurrentIndex(index)
            return QDialog.Accepted

        with patch.object(RasterSelectionDialog, "exec", select_loaded):
            dialog.raster_button.click()
        self.assertEqual(dialog.options().raster_path, loaded_path)
        self.assertEqual(dialog.options().raster_band, 1)
        self.assertIn("Loaded survey", dialog.raster_label.text())
        for mode in ("constant",):
            dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData(mode))
            self.assertFalse(dialog.raster_button.isEnabled())
            self.assertEqual(dialog.ground_z.isEnabled(), mode == "constant")
            self.assertEqual(dialog.options().ground_mode, mode)
        dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData("raster"))
        self.assertEqual(dialog.options().raster_path, loaded_path)
        self.assertEqual(self.store.layers(), [])

    def test_raster_mode_requires_selection_and_empty_picker_cannot_accept(self):
        dialog = self.dialog()
        dialog.downloaded(payload())
        self.assertFalse(dialog.raster_button.isEnabled())
        dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData("raster"))
        self.assertIsNone(dialog.raster_record)
        with self.assertRaisesRegex(ModelError, "raster"):
            dialog.options()
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertIn("raster", dialog.error.text())

        def reject_empty(picker):
            self.assertEqual(picker.raster.count(), 0)
            self.assertFalse(picker.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).isEnabled())
            return QDialog.Rejected

        with patch.object(RasterSelectionDialog, "exec", reject_empty):
            dialog.raster_button.click()
        self.assertIsNone(dialog.raster_record)
        dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData("constant"))
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertEqual(self.store.layers(), [])

    def test_controller_retries_failed_sampling_on_same_preview_without_redownload(self):
        bad_path = self.raster("nodata", nodata_from=3)
        good_path = self.raster("complete")
        plugin = self.plugin()
        attempts = []
        sources = []

        def accept(dialog):
            if attempts:
                self.assertIn("NoData", dialog.error.text())
                self.assertEqual(self.store.layers(), [])
                self.assertIs(dialog.source_layer(), sources[0])
            else:
                # This steep synthetic raster needs a tall enough shared roof
                # to clear its highest ground vertex in average-Z mode.
                dialog.default_height.setValue(20)
                dialog.downloaded(payload((-105, -104.997)))
                sources.append(dialog.source_layer())
            dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData("raster"))
            dialog.raster_record = {"name": "Survey", "path": good_path if attempts else bad_path, "band": 1}
            attempts.append(dialog)
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            return dialog.result()

        with patch.object(OsmImportDialog, "exec", accept), patch("acalc.osm_download.OsmDownload.start") as download:
            plugin.import_osm_buildings()
        download.assert_not_called()
        self.assertEqual(len(attempts), 2)
        self.assertIs(attempts[0], attempts[1])
        record, = self.store.layers()
        layer = plugin.selected_layer()
        self.assertEqual(layer.featureCount(), 2)
        self.assertEqual(validate_layer(layer, LAYER_TYPES["building"]), [])
        self.assertEqual(record["feature_type"], "building")
        self.assertTrue(all("infer_z" not in feature.fields().names() for feature in layer.getFeatures()))
        first = next(layer.getFeatures())
        self.assertTrue(all(feature["height_mode"] == "average" for feature in layer.getFeatures()))
        for row, expected in zip(vertex_rows(first.geometry(), LAYER_TYPES["building"]), [140, 147.5, 139, 132.5]):
            self.assertAlmostEqual(row[3], expected, places=6)
            self.assertAlmostEqual(row[3] + row[4], 139.75 + 12, places=6)
        self.assertIn("Copy saved", plugin.dock.status.text())
