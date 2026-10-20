# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Layer-wide Z authoring through real QGIS edit buffers and GDAL rasters.

The analytic pixel values are authoring regressions, not acoustic references.
"""

import math
import struct
from unittest.mock import patch

from osgeo import gdal
from qgis.core import QgsCoordinateReferenceSystem

from acalc.layer_elevation import set_layer_z_values
from acalc.layers import geometry_from_rows, load_layer, validate_layer, vertex_rows
from acalc.object_geometry import building_editor_rows
from acalc.raster_elevation import RasterElevation
from acalc.schema import LAYER_TYPES, ModelError
from acalc.storage import ModelStore
from tests.test_model import ModelFixture, add_feature


class LayerElevationTests(ModelFixture):
    def layer(self, key="point_source"):
        record = self.store.add_layer(key, LAYER_TYPES[key].title)
        return record, load_layer(self.store, record)

    def feature(self, layer, rows, name="Example", **attributes):
        kind = LAYER_TYPES[layer.customProperty("acalc/feature_type")]
        feature = add_feature(layer, kind, name)
        self.assertTrue(layer.changeGeometry(feature.id(), geometry_from_rows(rows, kind)))
        for name, value in attributes.items():
            self.assertTrue(layer.changeAttributeValue(feature.id(), layer.fields().indexFromName(name), value))
        return feature.id()

    def snapshot(self, layer):
        return {feature.id(): (bytes(feature.geometry().asWkb()), feature.attributes())
                for feature in layer.getFeatures()}

    def raster(self, name="ground", values=(10, 20, 30, 40), *, bands=1,
               unit="m", scale=1, offset=0, crs="EPSG:26913",
               affine=(100, 10, 0, 200, 0, -10)):
        path = self.path.with_name(name + ".tif")
        dataset = gdal.GetDriverByName("GTiff").Create(str(path), 2, 2, bands, gdal.GDT_Float64)
        dataset.SetProjection(QgsCoordinateReferenceSystem(crs).toWkt())
        dataset.SetGeoTransform(affine)
        for number in range(1, bands + 1):
            band = dataset.GetRasterBand(number)
            pixels = values if number == bands else (-2000,) * 4
            band.WriteRaster(0, 0, 2, 2, struct.pack("4d", *pixels), buf_type=gdal.GDT_Float64)
            band.SetNoDataValue(-9999)
            band.SetUnitType(unit)
            band.SetScale(scale)
            band.SetOffset(offset)
        band = dataset = None
        return {"name": name, "path": str(path), "band": bands}

    def test_fixed_z_updates_every_supported_kind_and_survives_save_reload(self):
        for kind in (kind for kind in LAYER_TYPES.values() if kind.ground_points):
            with self.subTest(kind=kind.key):
                record, layer = self.layer(kind.key)
                saved = add_feature(layer, kind, "Saved disabled object")
                self.assertTrue(layer.changeAttributeValue(saved.id(), layer.fields().indexFromName("enabled"), 0))
                self.assertTrue(layer.commitChanges(), layer.commitErrors())
                saved = next(layer.getFeatures())
                layer.selectByIds([saved.id()])
                pending = add_feature(layer, kind, "Pending object")
                if kind.height_controls:
                    layer.changeAttributeValue(pending.id(), layer.fields().indexFromName("height_mode"), "individual")
                    layer.changeAttributeValue(pending.id(), layer.fields().indexFromName("height_value"), 8.0)
                for feature in layer.getFeatures():
                    layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("ground_mode"), "unresolved")
                    layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("ground_mode"), "unresolved")
                before = {f["name"]: f for f in layer.getFeatures()}

                self.assertEqual(set_layer_z_values(layer, value=-12.75), 2)
                self.assertTrue(layer.isEditable())
                self.assertTrue(layer.isModified())
                self.assertEqual(layer.selectedFeatureIds(), [saved.id()])
                ground_mode = "per_point" if kind.geometry == "Point" else "uniform"
                for feature in layer.getFeatures():
                    original = before[feature["name"]]
                    expected = [(ring, x, y, -12.75, height)
                                for ring, x, y, _, height in vertex_rows(original.geometry(), kind)]
                    self.assertEqual(vertex_rows(feature.geometry(), kind), expected)
                    self.assertEqual(feature["ground_mode"], ground_mode)
                    for field in layer.fields().names():
                        if field not in ("ground_mode",):
                            self.assertEqual(feature[field], original[field], field)
                self.assertEqual(validate_layer(layer, kind), [])
                expected = {f["name"]: vertex_rows(f.geometry(), kind) for f in layer.getFeatures()}
                self.assertTrue(layer.commitChanges(), layer.commitErrors())
                reopened = load_layer(ModelStore.open(self.path), record)
                self.assertEqual({f["name"]: vertex_rows(f.geometry(), kind) for f in reopened.getFeatures()}, expected)
                self.assertTrue(all(f["ground_mode"] == ground_mode for f in reopened.getFeatures()))

    def test_one_undo_step_preserves_older_unsaved_edits_and_pending_features(self):
        _, layer = self.layer("barrier")
        add_feature(layer, LAYER_TYPES["barrier"])
        self.assertTrue(layer.commitChanges())
        self.assertTrue(layer.startEditing())
        layer.beginEditCommand("Earlier edits")
        feature = next(layer.getFeatures())
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("name"), "Earlier rename")
        add_feature(layer, LAYER_TYPES["barrier"], "Earlier new object")
        layer.endEditCommand()
        before, index = self.snapshot(layer), layer.undoStack().index()

        self.assertEqual(set_layer_z_values(layer, value=32.5), 2)
        after = self.snapshot(layer)
        self.assertEqual(layer.undoStack().index(), index + 1)
        layer.undoStack().undo()
        self.assertEqual(self.snapshot(layer), before)
        self.assertTrue(layer.isModified())
        layer.undoStack().redo()
        self.assertEqual(self.snapshot(layer), after)

    def test_later_write_failure_rolls_back_only_the_z_operation(self):
        for already_editable in (False, True):
            with self.subTest(already_editable=already_editable):
                _, layer = self.layer()
                add_feature(layer, LAYER_TYPES["point_source"], "First")
                add_feature(layer, LAYER_TYPES["point_source"], "Later")
                self.assertTrue(layer.commitChanges())
                if already_editable:
                    self.assertTrue(layer.startEditing())
                    layer.beginEditCommand("Earlier edit")
                    first = next(layer.getFeatures())
                    layer.changeAttributeValue(first.id(), layer.fields().indexFromName("notes"), "Keep this edit")
                    layer.endEditCommand()
                before, index = self.snapshot(layer), layer.undoStack().index()
                original = type(layer).changeGeometry
                calls = []

                def reject_second(target, fid, geometry, *args):
                    if target is layer:
                        calls.append(fid)
                        if len(calls) == 2:
                            return False
                    return original(target, fid, geometry, *args)

                with patch.object(type(layer), "changeGeometry", reject_second), self.assertRaises(ModelError):
                    set_layer_z_values(layer, value=42)
                self.assertEqual(len(calls), 2)
                self.assertEqual(self.snapshot(layer), before)
                self.assertEqual(layer.undoStack().index(), index)
                self.assertEqual(layer.isEditable(), already_editable)
                self.assertEqual(layer.isModified(), already_editable)

    def test_raster_samples_each_vertex_with_band_scaling_and_vertical_units(self):
        _, layer = self.layer("barrier")
        rows = [(0, 105, 195, 100, 1), (0, 110, 190, 110, 2), (0, 115, 185, 120, 3)]
        self.feature(layer, rows, ground_mode="unresolved")
        record = self.raster(bands=2, unit="ft", scale=2, offset=10)
        with patch("acalc.raster_elevation.gdal.Open", wraps=gdal.Open) as open_raster:
            self.assertEqual(set_layer_z_values(layer, raster_record=record), 1)
        open_raster.assert_called_once_with(record["path"], gdal.GA_ReadOnly)
        feature = next(layer.getFeatures())
        self.assertEqual(feature["ground_mode"], "per_point")
        for row, original, raw in zip(vertex_rows(feature.geometry(), LAYER_TYPES["barrier"]), rows, (10, 25, 40)):
            self.assertEqual(row[:3], original[:3])
            self.assertAlmostEqual(row[3], (raw * 2 + 10) * .3048)
            self.assertEqual(row[4], original[4])

    def test_raster_uses_layer_crs_to_transform_horizontal_coordinates(self):
        _, layer = self.layer()
        self.feature(layer, [(0, 500000, 0, 100, 2)])
        record = self.raster(crs="EPSG:4326", affine=(-105.001, .001, 0, .001, 0, -.001))
        self.assertEqual(set_layer_z_values(layer, raster_record=record), 1)
        row, = vertex_rows(next(layer.getFeatures()).geometry(), LAYER_TYPES["point_source"])
        self.assertEqual(row[:3], (0, 500000, 0))
        self.assertAlmostEqual(row[3], 25, places=6)
        self.assertEqual(row[4], 2)

    def test_later_raster_failure_keeps_all_geometry_attributes_and_prior_edits(self):
        for case, point, values in (("outside", (125, 185), (10, 20, 30, 40)),
                                    ("NoData", (115, 185), (10, 20, 30, -9999))):
            with self.subTest(case=case):
                _, layer = self.layer()
                self.feature(layer, [(0, 105, 195, 100, 1)], "First", ground_mode="unresolved")
                self.feature(layer, [(0, *point, 200, 2)], "Later", ground_mode="unresolved")
                self.assertTrue(layer.commitChanges())
                self.assertTrue(layer.startEditing())
                first = next(layer.getFeatures())
                layer.changeAttributeValue(first.id(), layer.fields().indexFromName("notes"), "Keep this edit")
                before, index = self.snapshot(layer), layer.undoStack().index()
                sampled = []
                original = RasterElevation.elevation

                def track(raster, x, y):
                    result = original(raster, x, y)
                    sampled.append(result)
                    return result

                with patch.object(RasterElevation, "elevation", track), self.assertRaisesRegex(ModelError, case):
                    set_layer_z_values(layer, raster_record=self.raster(case, values))
                self.assertEqual(sampled, [10], "The earlier feature must sample successfully before the failure.")
                self.assertEqual(self.snapshot(layer), before)
                self.assertEqual(layer.undoStack().index(), index)
                self.assertTrue(layer.isEditable())
                self.assertTrue(layer.isModified())

    def test_invalid_requests_leave_committed_layer_outside_edit_mode(self):
        _, layer = self.layer()
        add_feature(layer, LAYER_TYPES["point_source"])
        self.assertTrue(layer.commitChanges())
        before = self.snapshot(layer)
        record = self.raster()
        for options in ({}, {"value": math.nan}, {"value": math.inf},
                        {"value": 10, "raster_record": record},
                        {"raster_record": {"path": record["path"], "band": 2}},
                        {"raster_record": {"path": record["path"] + ".missing", "band": 1}}):
            with self.subTest(options=options), self.assertRaises(ModelError):
                set_layer_z_values(layer, **options)
            self.assertEqual(self.snapshot(layer), before)
            self.assertFalse(layer.isEditable())
        for key in ("ground_absorption", "area_receiver", "trees"):
            with self.subTest(kind=key):
                _, unsupported = self.layer(key)
                add_feature(unsupported, LAYER_TYPES[key])
                self.assertTrue(unsupported.commitChanges())
                before = self.snapshot(unsupported)
                with self.assertRaises(ModelError):
                    set_layer_z_values(unsupported, value=10)
                self.assertEqual(self.snapshot(unsupported), before)
                self.assertFalse(unsupported.isEditable())

    def test_raster_preserves_building_height_intent_for_all_six_modes(self):
        _, layer = self.layer("building")
        kind = LAYER_TYPES["building"]
        base = [(0, 105, 195, 100, 30), (0, 115, 195, 102, 40), (0, 105, 185, 104, 50)]
        for mode in ("average", "highest", "lowest", "absolute", "individual", "absolute_individual"):
            height = 200 if mode == "absolute" else 40
            editor_rows = [(ring, x, y, z, z + h if mode == "absolute_individual" else h)
                           for ring, x, y, z, h in base]
            rows = building_editor_rows(editor_rows, mode, height)
            self.feature(layer, rows, mode, ground_mode="per_point",
                         height_mode=mode, height_value=height)
        self.assertEqual(set_layer_z_values(layer, raster_record=self.raster()), 6)
        shared_roofs = {"average": 60, "highest": 70, "lowest": 50, "absolute": 200}
        for feature in layer.getFeatures():
            mode = feature["height_mode"]
            rows = vertex_rows(feature.geometry(), kind)
            self.assertEqual([row[3] for row in rows], [10, 20, 30])
            self.assertEqual(feature["height_value"], 200 if mode == "absolute" else 40)
            if mode == "individual":
                self.assertEqual([row[4] for row in rows], [30, 40, 50])
            else:
                expected = [130, 142, 154] if mode == "absolute_individual" else [shared_roofs[mode]] * 3
                self.assertEqual([row[3] + row[4] for row in rows], expected)
        self.assertEqual(validate_layer(layer, kind), [])

    def test_ground_above_absolute_roof_rejects_whole_layer_without_starting_edit(self):
        for mode in ("absolute", "absolute_individual"):
            with self.subTest(mode=mode):
                _, layer = self.layer("building")
                rows = [(0, 105, 195, 10, 10), (0, 115, 195, 10, 10), (0, 105, 185, 10, 10)]
                self.feature(layer, rows, "Relative first", height_mode="individual", height_value=10)
                self.feature(layer, rows, "Absolute later", height_mode=mode, height_value=20)
                self.assertTrue(layer.commitChanges())
                before = self.snapshot(layer)
                with self.assertRaisesRegex(ModelError, "[Rr]oof"):
                    set_layer_z_values(layer, value=25)
                self.assertEqual(self.snapshot(layer), before)
                self.assertFalse(layer.isEditable())
