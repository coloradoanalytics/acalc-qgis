# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Raster reference persistence, independent interpolation checks and migration."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from osgeo import gdal, ogr, osr

from acalc.raster_elevation import RasterElevation
from acalc.schema import LAYER_TYPES, ModelError, SCHEMA_VERSION, definition, new_id
from acalc.storage import ModelStore


class TerrainStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "terrain.gpkg"
        self.srs = osr.SpatialReference()
        self.srs.ImportFromEPSG(26913)
        self.store = ModelStore.create(self.path, "Terrain project", self.srs.ExportToWkt())
        self.variant_id = self.store.variants()[0]["variant_id"]

    def tearDown(self):
        self.directory.cleanup()

    def raster(self, values=(10, 20, 30, 40), unit="m", scale=1, offset=0, affine=(100, 10, 0, 200, 0, -10)):
        path = Path(self.directory.name) / "heights.tif"
        dataset = gdal.GetDriverByName("GTiff").Create(str(path), 2, 2, 1, gdal.GDT_Float64)
        dataset.SetSpatialRef(self.srs)
        dataset.SetGeoTransform(affine)
        band = dataset.GetRasterBand(1)
        band.SetNoDataValue(-9999)
        band.SetScale(scale)
        band.SetOffset(offset)
        band.SetUnitType(unit)
        band.WriteRaster(0, 0, 2, 2, struct.pack("4d", *values), buf_type=gdal.GDT_Float64)
        band = dataset = None
        return path

    def test_reference_contains_no_samples_or_settings_and_does_not_touch_calculation_revision(self):
        path = self.raster()
        revision = self.store.metadata()["modified_at"]
        ident = self.store.add_elevation_raster(path, "Survey")
        expected = {"raster_id": ident, "name": "Survey", "path": str(path), "band": 1}
        self.assertEqual(self.store.elevation_rasters(), [expected])
        self.assertEqual(ModelStore.open(self.path).elevation_rasters(), [expected])
        path.unlink()
        self.assertEqual(ModelStore.open(self.path).elevation_rasters(), [expected])
        self.store.remove_elevation_raster(ident)
        self.assertEqual(self.store.metadata()["modified_at"], revision)
        self.assertEqual(self.store.elevation_rasters(), [])

    def test_native_centres_bilinear_and_edges(self):
        with RasterElevation(str(self.raster()), self.srs.ExportToWkt()) as raster:
            # A plane through centres: Z=10+(X-105)+2*(195-Y).
            for x, y, z in ((105, 195, 10), (115, 185, 40), (110, 190, 25),
                            (107.5, 192.5, 17.5), (100, 200, 10), (120, 180, 40),
                            (100, 190, 20), (110, 180, 35)):
                with self.subTest(x=x, y=y):
                    self.assertAlmostEqual(raster.elevation(x, y), z)
            for x, y in ((99, 190), (121, 190), (110, 201), (110, 179), (float('nan'), 190)):
                with self.assertRaises(ModelError):
                    raster.elevation(x, y)

    def test_scale_offset_and_vertical_units(self):
        with RasterElevation(str(self.raster(unit="ft", scale=2, offset=10)), self.srs.ExportToWkt()) as raster:
            self.assertAlmostEqual(raster.elevation(110, 190), 60 * .3048)
        with self.assertRaisesRegex(ModelError, "vertical unit"):
            self.store.add_elevation_raster(self.raster(unit="furlong"))

    def test_nodata_renormalizes_neighbours_but_never_invents_a_missing_value(self):
        with RasterElevation(str(self.raster(values=(10, 20, 30, -9999))), self.srs.ExportToWkt()) as raster:
            self.assertEqual(raster.elevation(110, 190), 20)
            with self.assertRaisesRegex(ModelError, "NoData"):
                raster.elevation(115, 185)
            with self.assertRaisesRegex(ModelError, "Ground point 2"):
                raster.elevations([(105, 195), (115, 185)])
        with RasterElevation(str(self.raster(values=(-9999,) * 4)), self.srs.ExportToWkt()) as raster:
            with self.assertRaisesRegex(ModelError, "NoData"):
                raster.elevation(110, 190)

    def test_rotated_affine_and_mask(self):
        affine = (100, 10, 2, 200, -3, -10)
        path = self.raster(affine=affine)
        dataset = gdal.Open(str(path), gdal.GA_Update)
        band = dataset.GetRasterBand(1)
        band.CreateMaskBand(gdal.GMF_PER_DATASET)
        band.GetMaskBand().WriteRaster(0, 0, 2, 2, bytes((255, 0, 255, 255)), buf_type=gdal.GDT_Byte)
        band = dataset = None
        with RasterElevation(str(path), self.srs.ExportToWkt()) as raster:
            self.assertAlmostEqual(raster.elevation(106, 193.5), 10)
            self.assertAlmostEqual(raster.elevation(112, 187), 80 / 3)

    def test_horizontal_crs_conversion_preserves_metres_z(self):
        feet = osr.SpatialReference()
        feet.ImportFromEPSG(2232)
        for crs in (self.srs, feet):
            crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        x, y, _ = osr.CoordinateTransformation(self.srs, feet).TransformPoint(110, 190)
        with RasterElevation(str(self.raster()), feet.ExportToWkt()) as raster:
            self.assertAlmostEqual(raster.elevation(x, y), 25, places=5)

    def test_single_pixel_raster(self):
        path = Path(self.directory.name) / "single.tif"
        dataset = gdal.GetDriverByName("GTiff").Create(str(path), 1, 1, 1, gdal.GDT_Float64)
        dataset.SetSpatialRef(self.srs)
        dataset.SetGeoTransform((0, 1, 0, 1, 0, -1))
        dataset.GetRasterBand(1).Fill(123)
        dataset = None
        with RasterElevation(str(path), self.srs.ExportToWkt()) as raster:
            self.assertEqual(raster.elevation(.25, .75), 123)

    def test_invalid_metadata_rejected_without_saving_reference(self):
        path = self.raster()
        with self.assertRaisesRegex(ModelError, "band"):
            self.store.add_elevation_raster(path, band=2)
        dataset = gdal.Open(str(path), gdal.GA_Update)
        dataset.SetProjection("")
        dataset = None
        with self.assertRaisesRegex(ModelError, "coordinate systems"):
            self.store.add_elevation_raster(path)
        self.assertEqual(self.store.elevation_rasters(), [])

    def test_variant_membership_contains_only_object_layers(self):
        record = self.store.add_layer("terrain_line", "Survey")
        self.store.save_variant(self.variant_id, "Terrain", definition([record["layer_id"]]))
        with self.assertRaisesRegex(ModelError, "layer membership only"):
            self.store.save_variant(self.variant_id, "Invalid", dict(definition(), terrain_group_ids=[]))
        self.store.archive_layer(record["layer_id"])
        self.assertEqual(self.store.variants()[0]["definition"], definition())
        self.store.restore_layer(record["layer_id"])
        self.assertNotIn("terrain_group_id", self.store.layers()[0])
        with self.store.connection(readonly=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse(tables & {"acalc_terrain_groups", "acalc_terrain_members", "acalc_terrain_cache"})

    def test_schema6_migration_preserves_geometry_membership_references_and_backup(self):
        record = self.store.add_layer("terrain_point", "Survey")
        dataset = ogr.Open(str(self.path), 1)
        layer = dataset.GetLayerByName(record["table_name"])
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("object_id", new_id())
        feature.SetField("name", "Survey")
        feature.SetGeometry(ogr.CreateGeometryFromWkt("POINT Z (1 2 3)"))
        layer.CreateFeature(feature)
        feature = layer = dataset = None
        ident = self.store.add_elevation_raster(self.raster(), "Reference")
        raster = self.store.elevation_rasters()[0]
        self.store.save_variant(self.variant_id, "Variant", definition([record["layer_id"]]))
        with self.store.connection() as db:
            original = db.execute(f'SELECT geom FROM "{record["table_name"]}"').fetchone()[0]
            db.execute("CREATE TABLE acalc_terrain_groups(group_id TEXT PRIMARY KEY,name TEXT,is_base INTEGER,created_at TEXT)")
            db.execute("INSERT INTO acalc_terrain_groups VALUES ('base','Base',1,'now'),('local','Phase',0,'now')")
            db.execute("CREATE TABLE acalc_terrain_members(layer_id TEXT,group_id TEXT,sort_order INTEGER)")
            db.execute("INSERT INTO acalc_terrain_members VALUES (?,'local',0)", (record["layer_id"],))
            db.execute("CREATE TABLE acalc_terrain_cache(group_id TEXT,surface_json TEXT,input_sha256 TEXT)")
            db.execute("INSERT INTO acalc_terrain_cache VALUES ('local','{}','old')")
            db.execute("ALTER TABLE acalc_elevation_rasters ADD COLUMN group_id TEXT DEFAULT 'local'")
            db.execute("ALTER TABLE acalc_elevation_rasters ADD COLUMN sample_step INTEGER DEFAULT 8")
            db.execute(f'''CREATE TRIGGER acalc_terrain_dirty_{record['table_name']}_update
                AFTER UPDATE ON "{record['table_name']}" BEGIN DELETE FROM acalc_terrain_cache; END''')
            db.execute("CREATE TRIGGER acalc_touch_acalc_elevation_rasters_delete AFTER DELETE ON acalc_elevation_rasters BEGIN UPDATE acalc_project SET modified_at='old'; END")
            for legacy_layer in self.store.layers():
                if LAYER_TYPES[legacy_layer['feature_type']].elevation:
                    db.execute(f'ALTER TABLE "{legacy_layer["table_name"]}" ADD COLUMN infer_z INTEGER NOT NULL DEFAULT 0')
            db.execute("UPDATE acalc_project SET schema_version=6")
            db.execute("UPDATE acalc_variants SET definition_json=?", (json.dumps({
                "schema_version": 6, "layer_ids": [record["layer_id"]], "terrain_group_ids": ["local"]}),))
        Path(raster["path"]).unlink()  # Missing references do not block migration.
        store = ModelStore.open(self.path)
        self.assertEqual(store.metadata()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(store.variants()[0]["definition"], definition([record["layer_id"]]))
        self.assertEqual(store.elevation_rasters(), [raster])
        with store.connection() as db:
            self.assertEqual(db.execute(f'SELECT geom FROM "{record["table_name"]}"').fetchone()[0], original)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'acalc_terrain_%'").fetchall())
        dataset = ogr.Open(str(self.path), 1)
        layer = dataset.GetLayerByName(record["table_name"])
        feature = next(iter(layer))
        feature.SetField("notes", "edit after migration")
        self.assertEqual(layer.SetFeature(feature), 0)
        feature = layer = dataset = None
        store.remove_elevation_raster(ident)
        with closing(sqlite3.connect(store.migration_backup)) as db:
            self.assertEqual(db.execute("SELECT schema_version FROM acalc_project").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acalc_terrain_groups").fetchone()[0], 2)

    def test_schema5_upgrade_preserves_settings_and_membership(self):
        record = self.store.add_layer("building", "Existing")
        settings = self.store.calculation()["settings"]
        settings["temperature_c"] = 22
        self.store.save_calculation(settings)
        with self.store.connection() as db:
            db.execute("DROP TABLE acalc_elevation_rasters")
            db.execute("DELETE FROM gpkg_contents WHERE table_name='acalc_elevation_rasters'")
            for legacy_layer in self.store.layers():
                if LAYER_TYPES[legacy_layer['feature_type']].elevation:
                    db.execute(f'ALTER TABLE "{legacy_layer["table_name"]}" ADD COLUMN infer_z INTEGER NOT NULL DEFAULT 0')
            db.execute("UPDATE acalc_project SET schema_version=5")
            db.execute("UPDATE acalc_variants SET definition_json=?", (json.dumps({
                "schema_version": 5, "layer_ids": [record["layer_id"]]}),))
        store = ModelStore.open(self.path)
        self.assertEqual(store.calculation()["settings"], settings)
        self.assertEqual(store.variants()[0]["definition"], definition([record["layer_id"]]))
        self.assertEqual(store.elevation_rasters(), [])
