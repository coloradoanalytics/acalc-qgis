# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Real GeoPackage tests for calculation inputs, migrations and current results."""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from osgeo import ogr, osr

from acalc.calculation_settings import (
    ISO_ENGINE, default_calculation_settings, validate_settings,
)
from acalc.schema import ModelError, SCHEMA_VERSION, new_id, definition
from acalc.spectra import spectrum_definition
from acalc.storage import ModelStore, canonical_crs_wkt


def downgrade_to_legacy(db, version):
    """Restore the real pre-variant table names for migration fixtures."""
    shared = db.execute("SELECT engine,settings_json FROM acalc_calculation").fetchone()
    for row in db.execute("SELECT variant_id,definition_json FROM acalc_variants").fetchall():
        value = json.loads(row["definition_json"])
        value["schema_version"] = version
        value.pop("terrain_group_ids", None)
        value.setdefault("engine", shared[0] if version >= 3 else None)
        value.setdefault("settings", json.loads(shared[1]) if version >= 3 else {})
        db.execute("UPDATE acalc_variants SET definition_json=? WHERE variant_id=?", (json.dumps(value), row["variant_id"]))
    db.execute("ALTER TABLE acalc_variants RENAME COLUMN variant_id TO configuration_id")
    db.execute("ALTER TABLE acalc_variants RENAME TO acalc_configurations")
    db.execute("UPDATE gpkg_contents SET table_name='acalc_configurations',identifier='acalc_configurations' WHERE table_name='acalc_variants'")
    db.execute("DROP TABLE acalc_calculation")
    db.execute("DELETE FROM gpkg_contents WHERE table_name='acalc_calculation'")
    run_fields = {row["name"] for row in db.execute("PRAGMA table_info(acalc_calculation_runs)")}
    if "variant_id" in run_fields:
        db.execute("ALTER TABLE acalc_calculation_runs RENAME COLUMN variant_id TO configuration_id")
        db.execute("ALTER TABLE acalc_calculation_runs RENAME COLUMN variant_name TO configuration_name")
    db.execute("UPDATE acalc_project SET schema_version=?", (version,))


class CalculationStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "calculation #1.gpkg"
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(26913)
        self.store = ModelStore.create(self.path, "Calculation model", srs.ExportToWkt(), "EPSG:26913")
        self.config_id = self.store.variants()[0]["variant_id"]

    def tearDown(self):
        self.directory.cleanup()

    def insert_run(self, finished="2026-09-18T18:00:00.000+00:00", silent=False):
        run_id, layer_id, object_id = new_id(), new_id(), new_id()
        project = self.store.metadata()
        with self.store.connection() as db:
            db.execute("""INSERT INTO acalc_calculation_runs VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,'[]','{}')""",
                (run_id, project["project_id"], self.config_id, "Default", ISO_ENGINE, "test", "ISO 9613-2:2024",
                 finished, finished, "f" * 64, project["modified_at"],
                 json.dumps(default_calculation_settings()), 1, 1, 0.125))
            levels = [None] * 8 if silent else [10, 20, 30, 40, 50, 40, 30, None]
            db.execute("INSERT INTO acalc_receiver_results VALUES (?,?,?,?,?,?,?,'{}')",
                       (run_id, layer_id, object_id, "Receiver", json.dumps(levels),
                        None if silent else 51.0, None if silent else 50.0))
        return run_id, layer_id, object_id

    def test_project_has_units_explicit_draft_and_registered_result_tables(self):
        self.assertEqual(self.store.metadata()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(self.store.metadata()["horizontal_to_m"], 1)
        self.assertEqual(self.store.variants()[0]["definition"], definition())
        self.assertEqual(self.store.calculation()["settings"], default_calculation_settings())
        self.assertEqual(self.store.calculation_runs(self.config_id), [])
        self.assertEqual(self.store.receiver_results(self.config_id), [])
        with self.store.connection(readonly=True) as db:
            tables = {row[0] for row in db.execute("SELECT table_name FROM gpkg_contents WHERE data_type='attributes'")}
        self.assertTrue({"acalc_calculation_runs", "acalc_receiver_results"} <= tables)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(2232)  # Colorado Central (US survey feet).
        feet = ModelStore.create(Path(self.directory.name) / "feet.gpkg", "Feet", srs.ExportToWkt())
        self.assertAlmostEqual(feet.metadata()["horizontal_to_m"], 1200 / 3937, places=12)
        with feet.connection() as db:
            db.execute("UPDATE acalc_project SET horizontal_to_m=1")
        with self.assertRaisesRegex(ModelError, "does not match"):
            ModelStore.open(feet.path)

    def test_iso_settings_are_explicit_persistent_and_strict(self):
        layer = self.store.add_layer("point_receiver", "Receivers")
        value = definition([layer["layer_id"]])
        self.store.save_variant(self.config_id, "Calculation", value)
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.variants()[0]["definition"], value)
        self.assertEqual(reopened.export_variant(self.config_id)["definition"], value)
        cases = (("schema_version", True), ("schema_version", 3), ("temperature_c", -21), ("temperature_c", 51),
                 ("relative_humidity_percent", 9), ("relative_humidity_percent", 101),
                 ("pressure_kpa", 49), ("pressure_kpa", 121), ("ground_factor", 1.01),
                 ("c0_db", -1), ("c0_db", 5.01), ("temperature_c", 10 ** 400),
                 ("meteorology", "upwind"), ("ground_method", "flat"), ("undefined", 1),
                 # Retired version-1 controls are not version-2 settings.
                 ("ground_elevation_m", 0.0), ("line_segment_max_m", 10.0), ("source_directivity", "omnidirectional"))
        for key, number in cases:
            bad = default_calculation_settings()
            bad[key] = number
            with self.subTest(key=key, value=str(number)[:30]), self.assertRaises(ModelError):
                self.store.save_calculation(bad)
        incomplete = default_calculation_settings()
        del incomplete["ground_method"]
        with self.assertRaisesRegex(ModelError, "Missing calculation settings"):
            validate_settings(incomplete)
        self.assertEqual(self.store.variants()[0]["definition"], value)

    def test_wkt2_creation_canonicalizes_registered_and_custom_projected_crs(self):
        custom = osr.SpatialReference()
        custom.ImportFromProj4("+proj=tmerc +lat_0=39 +lon_0=-104.5 +k=0.9997 "
                              "+x_0=123456 +y_0=765432 +datum=WGS84 +units=m +no_defs")
        registered = osr.SpatialReference()
        registered.ImportFromEPSG(2227)
        for name, original in (("registered", registered), ("custom", custom)):
            with self.subTest(crs=name):
                original_wkt = original.ExportToWkt(["FORMAT=WKT2_2019"])
                parsed = osr.SpatialReference()
                parsed.ImportFromWkt(original_wkt)
                store = ModelStore.create(Path(self.directory.name) / f"{name}.gpkg", name, original_wkt)
                record = store.add_layer("point_receiver", "Receivers")
                stored_wkt = store.metadata()["crs_wkt"]
                self.assertTrue(stored_wkt.startswith("PROJCS["))
                expected_wkt = registered.ExportToWkt() if name == "registered" else parsed.ExportToWkt()
                self.assertEqual(stored_wkt, expected_wkt)
                with store.connection(readonly=True) as db:
                    layer_wkt = db.execute("""SELECT s.definition FROM gpkg_spatial_ref_sys s
                        JOIN gpkg_geometry_columns g ON s.srs_id=g.srs_id WHERE g.table_name=?""",
                        (record["table_name"],)).fetchone()[0]
                self.assertEqual(stored_wkt, layer_wkt)
                restored = osr.SpatialReference()
                restored.ImportFromWkt(stored_wkt)
                self.assertTrue(parsed.IsSame(restored))
        altered = registered.Clone()
        altered.SetProjParm("false_easting", 12345)
        self.assertEqual(altered.GetAuthorityCode(None), "2227")
        self.assertFalse(altered.IsSame(registered))
        self.assertEqual(canonical_crs_wkt(altered), altered.ExportToWkt())

    def test_version_1_settings_keep_shared_inputs_and_drop_retired_controls(self):
        # The version-1 format of the retired in-plugin solver (schema 10).
        legacy = {"schema_version": 1, "temperature_c": 15.0, "relative_humidity_percent": 80.0,
                  "pressure_kpa": 100.0, "ground_factor": 0.5, "ground_elevation_m": 12.0,
                  "meteorology": "long_term", "c0_db": 2.0, "source_directivity": "omnidirectional",
                  "line_segment_max_m": 10.0, "lateral_diffraction": "fresnel",
                  "reflection_search_distance_m": 200.0, "subdivision_tolerance_db": 0.05,
                  "reflection_order": 2, "building_reflection_coefficient": 0.8,
                  "barrier_reflection_coefficient": 0.7}
        expected = {**default_calculation_settings(), "temperature_c": 15.0, "relative_humidity_percent": 80.0,
                    "pressure_kpa": 100.0, "ground_factor": 0.5, "meteorology": "long_term", "c0_db": 2.0,
                    "reflection_order": 2, "building_reflection_coefficient": 0.8,
                    "barrier_reflection_coefficient": 0.7}
        self.assertEqual(validate_settings(legacy), expected)
        self.assertEqual(legacy["schema_version"], 1)
        # Early version-1 files predate the reflection controls.
        early = {key: value for key, value in legacy.items() if not key.endswith("reflection_coefficient")
                 and key != "reflection_order"}
        self.assertEqual(validate_settings(early), {**expected, "reflection_order": 3,
                                                    "building_reflection_coefficient": 0.9,
                                                    "barrier_reflection_coefficient": 0.9})
        with self.assertRaisesRegex(ModelError, "Unsupported calculation settings: undefined"):
            validate_settings({**legacy, "undefined": 1})
        with self.store.connection() as db:
            db.execute("UPDATE acalc_calculation SET settings_json=?", (json.dumps(legacy),))
        self.assertEqual(ModelStore.open(self.path).calculation()["settings"], expected)

    def test_schema2_upgrade_preserves_catalog_and_variant_and_backs_up(self):
        power_id = self.store.add_spectrum("Fan", spectrum_definition("sound_power", [90] * 8))
        layer = self.store.add_layer("point_source", "Sources")
        before = self.store.variants()[0]
        legacy_definition = before["definition"]
        legacy_definition["schema_version"] = 2
        legacy_definition["layer_ids"] = [layer["layer_id"]]
        original_crs = osr.SpatialReference()
        original_crs.ImportFromWkt(self.store.metadata()["crs_wkt"])
        legacy_wkt2 = original_crs.ExportToWkt(["FORMAT=WKT2_2019"])
        with self.store.connection() as db:
            db.execute("DROP TABLE acalc_receiver_results")
            db.execute("DROP TABLE acalc_calculation_runs")
            db.execute("DELETE FROM gpkg_contents WHERE table_name IN ('acalc_receiver_results','acalc_calculation_runs')")
            db.execute("ALTER TABLE acalc_project DROP COLUMN horizontal_to_m")
            db.execute("UPDATE acalc_project SET schema_version=2")
            db.execute("UPDATE acalc_project SET crs_wkt=?", (legacy_wkt2,))
            db.execute("UPDATE acalc_variants SET definition_json=?", (json.dumps(legacy_definition),))
            downgrade_to_legacy(db, 2)
        upgraded = ModelStore.open(self.path)
        self.assertEqual(upgraded.metadata()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(upgraded.metadata()["horizontal_to_m"], 1)
        self.assertTrue(upgraded.metadata()["crs_wkt"].startswith("PROJCS["))
        migrated_crs = osr.SpatialReference()
        migrated_crs.ImportFromWkt(upgraded.metadata()["crs_wkt"])
        self.assertTrue(original_crs.IsSame(migrated_crs))
        with upgraded.connection(readonly=True) as db:
            registered_wkt = db.execute("""SELECT s.definition FROM gpkg_spatial_ref_sys s
                JOIN gpkg_geometry_columns g ON s.srs_id=g.srs_id WHERE g.table_name=?""",
                (layer["table_name"],)).fetchone()[0]
        self.assertEqual(upgraded.metadata()["crs_wkt"], registered_wkt)
        self.assertEqual(upgraded.spectra()[0]["spectrum_id"], power_id)
        self.assertEqual(upgraded.layers()[0], layer)
        self.assertEqual(upgraded.variants()[0]["variant_id"], before["variant_id"])
        expected = deepcopy(legacy_definition)
        expected["schema_version"] = SCHEMA_VERSION
        self.assertEqual(upgraded.variants()[0]["definition"], expected)
        with closing(sqlite3.connect(upgraded.migration_backup)) as backup:
            self.assertEqual(backup.execute("SELECT schema_version FROM acalc_project").fetchone()[0], 2)
            self.assertEqual(backup.execute("SELECT crs_wkt FROM acalc_project").fetchone()[0], legacy_wkt2)
            self.assertEqual(backup.execute("SELECT spectrum_id FROM acalc_spectra").fetchone()[0], power_id)
            self.assertNotIn("horizontal_to_m", {row[1] for row in backup.execute("PRAGMA table_info(acalc_project)")})
        self.assertEqual(upgraded.calculation_runs(), [])
        self.assertIsNone(ModelStore.open(self.path).migration_backup)
        self.assertEqual(len(list(self.path.parent.glob("*.schema-2-backup-*.gpkg"))), 1)

    def test_schema4_upgrade_keeps_results_and_offers_conflicting_settings(self):
        other_id = self.store.add_variant("Hot day")
        run_id, _, _ = self.insert_run()
        power_id = self.store.add_spectrum("Legacy fan", spectrum_definition("sound_power", [100] * 8))
        watts = spectrum_definition("sound_power", [0.01, 0, None, None, 1, 2, 3, 4])
        watts.update(schema_version=1, unit="W", quantity="sound_power", zero_energy_bands_hz=[250])
        hot = default_calculation_settings()
        hot["temperature_c"] = 35
        with self.store.connection() as db:
            downgrade_to_legacy(db, 4)
            value = {"schema_version": 4, "layer_ids": [], "engine": ISO_ENGINE, "settings": hot}
            db.execute("UPDATE acalc_configurations SET definition_json=? WHERE configuration_id=?",
                       (json.dumps(value), other_id))
            db.execute("UPDATE acalc_spectra SET spectrum_json=? WHERE spectrum_id=?", (json.dumps(watts), power_id))
        upgraded = ModelStore.open(self.path)
        self.assertEqual({v["variant_id"] for v in upgraded.variants()}, {self.config_id, other_id})
        self.assertTrue(all(v["definition"] == definition() for v in upgraded.variants()))
        self.assertEqual(upgraded.calculation_runs(self.config_id)[0]["run_id"], run_id)
        self.assertEqual(upgraded.calculation_runs(self.config_id)[0]["variant_name"], "Default")
        options = upgraded.calculation()
        self.assertIsNone(options["engine"])
        self.assertEqual({v["name"]: v["settings"]["temperature_c"] for v in options["legacy_settings"]},
                         {"Default": 10, "Hot day": 35})
        power = upgraded.spectra()[0]["spectrum"]
        self.assertEqual(power["schema_version"], 2)
        self.assertEqual(power["unit"], "dB re 1 pW")
        self.assertEqual(power["values"][:5], [100, 0, 0, None, 120])
        self.assertNotIn("zero_energy_bands_hz", power)
        upgraded.save_calculation(hot)
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.calculation()["settings"], hot)
        self.assertEqual(reopened.calculation()["engine"], ISO_ENGINE)
        self.assertEqual(len(reopened.calculation()["legacy_settings"]), 2)
        with closing(sqlite3.connect(upgraded.migration_backup)) as backup:
            self.assertEqual(json.loads(backup.execute("SELECT spectrum_json FROM acalc_spectra").fetchone()[0]), watts)
            self.assertEqual(backup.execute("SELECT configuration_id FROM acalc_calculation_runs").fetchone()[0], self.config_id)

    def test_variants_reject_calculation_parameters_and_shared_edits_stale_results(self):
        self.insert_run()
        self.assertFalse(self.store.latest_calculation_run(self.config_id)["stale"])
        value = definition()
        value["settings"] = default_calculation_settings()
        with self.assertRaisesRegex(ModelError, "layer membership only"):
            self.store.save_variant(self.config_id, "Wrong", value)
        settings = default_calculation_settings()
        settings["temperature_c"] = 20
        self.store.save_calculation(settings)
        self.assertTrue(self.store.latest_calculation_run(self.config_id)["stale"])
        self.assertEqual(self.store.variants()[0]["definition"], definition())

    def test_results_before_current_integration_criterion_require_recalculation(self):
        self.insert_run()
        for version, stale in (("0.8.0", True), ("0.9.0", True), ("0.9.1", True), ("0.10.0", True), ("0.11.0", True), ("0.12.0", True), ("0.13.0", True), ("0.14.0", True), ("0.16.0", True), ("0.16.1", True), ("0.17.0", True), ("0.17.1", True), ("0.17.2", True), ("0.18.0", True), ("0.18.1", True), ("0.18.2", True), ("0.18.3", True), ("0.18.5", True), ("0.19.0", True), ("0.20.0", True), ("0.20.1", True), ("0.20.2", True), ("0.20.3", True), ("0.22.0", True), ("0.23.0", True), ("0.24.0", False)):
            with self.store.connection() as db:
                db.execute("UPDATE acalc_calculation_runs SET engine_version=?", (version,))
            self.assertEqual(self.store.latest_calculation_run(self.config_id)["stale"], stale)

    def test_results_have_octaves_broadband_stable_identity_and_staleness(self):
        modified = self.store.metadata()["modified_at"]
        first, layer_id, object_id = self.insert_run()
        self.assertEqual(self.store.metadata()["modified_at"], modified)
        self.assertEqual(self.store.latest_calculation_run(self.config_id)["run_id"], first)
        self.assertEqual(self.store.calculation_runs(self.config_id)[0]["settings"], default_calculation_settings())
        self.assertEqual(self.store.calculation_runs(self.config_id)[0]["warnings"], [])
        latest = self.store.receiver_results(self.config_id)[0]
        self.assertEqual(latest["octave_levels"], [10, 20, 30, 40, 50, 40, 30, None])
        self.assertEqual(latest["broadband_a_db"], 50)
        self.assertFalse(latest["stale"])
        earlier = self.store.receiver_results(run_id=first, layer_id=layer_id, object_id="{" + object_id + "}")
        self.assertEqual(len(earlier), 1)
        self.assertEqual(earlier[0]["object_id"], object_id)
        self.assertEqual(earlier[0]["octave_levels"], [10, 20, 30, 40, 50, 40, 30, None])
        self.assertEqual(earlier[0]["broadband_z_db"], 51.0)
        with self.store.connection() as db:
            db.execute("UPDATE acalc_project SET modified_at='2026-09-19T00:00:00.000+00:00'")
        self.assertTrue(self.store.receiver_results(run_id=first)[0]["stale"])
        other_id = self.store.add_variant("Other")
        with self.assertRaisesRegex(ModelError, "different variant"):
            self.store.receiver_results(other_id, run_id=first)
        self.store.delete_variant(self.config_id)
        self.assertEqual(len(self.store.calculation_runs(self.config_id)), 1)
        self.assertEqual(len(self.store.receiver_results(run_id=first)), 1)

    def test_open_older_file_discards_superseded_calculations_and_keeps_other_variants(self):
        first, _, _ = self.insert_run()
        second, _, _ = self.insert_run("2026-09-18T18:01:00.000+00:00", silent=True)
        original = self.config_id
        self.config_id = self.store.add_variant("Other")
        other, _, _ = self.insert_run()
        self.config_id = original
        modified = self.store.metadata()["modified_at"]
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.receiver_results(run_id=first), [])
        self.assertEqual(reopened.receiver_results(original)[0]["octave_levels"], [None] * 8)
        self.assertEqual({r["run_id"] for r in reopened.calculation_runs()}, {second, other})
        self.assertEqual(reopened.metadata()["modified_at"], modified)
        with reopened.connection(readonly=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acalc_calculation_runs").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acalc_receiver_results").fetchone()[0], 2)
        # Reopening is idempotent, including after all older rows were removed.
        self.assertEqual(len(ModelStore.open(self.path).calculation_runs()), 2)

    def test_corrupt_levels_cannot_be_presented_as_success(self):
        run_id, _, _ = self.insert_run()
        with self.assertRaises(ModelError):
            with self.store.connection() as db:
                db.execute("UPDATE acalc_receiver_results SET octave_levels_json='[1,2]' WHERE run_id=?", (run_id,))
        with self.store.connection() as db:
            db.execute("UPDATE acalc_receiver_results SET broadband_a_db=NULL WHERE run_id=?", (run_id,))
        with self.assertRaisesRegex(ModelError, "zero received energy"):
            self.store.receiver_results(run_id=run_id)

    def test_external_feature_and_catalog_edits_invalidate_results(self):
        record = self.store.add_layer("point_receiver", "Receivers")
        run_id, _, _ = self.insert_run()
        before = self.store.metadata()["modified_at"]
        dataset = ogr.Open(self.store.path, update=1)
        layer = dataset.GetLayerByName(record["table_name"])
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("object_id", new_id())
        feature.SetField("name", "External receiver")
        point = ogr.Geometry(ogr.wkbPointZM)
        point.AddPointZM(500000, 4400000, 0, 1.5)
        feature.SetGeometry(point)
        self.assertEqual(layer.CreateFeature(feature), 0)
        feature = layer = dataset = None
        self.assertNotEqual(self.store.metadata()["modified_at"], before)
        self.assertTrue(self.store.receiver_results(run_id=run_id)[0]["stale"])
        # Geometry tables require the normal GeoPackage spatial SQL functions,
        # supplied by GDAL. Both edits still bypass the plugin's Python helpers.
        dataset = ogr.Open(self.store.path, update=1)
        layer = dataset.GetLayerByName(record["table_name"])
        feature = layer.GetNextFeature()
        before = self.store.metadata()["modified_at"]
        feature.SetField("name", "Renamed externally")
        self.assertEqual(layer.SetFeature(feature), 0)
        self.assertGreater(self.store.metadata()["modified_at"], before)
        before = self.store.metadata()["modified_at"]
        self.assertEqual(layer.DeleteFeature(feature.GetFID()), 0)
        self.assertGreater(self.store.metadata()["modified_at"], before)
        feature = layer = dataset = None
        # Use an ordinary SQLite writer, rather than any ModelStore touch method.
        mutations = [
            ("UPDATE acalc_layers SET name=? WHERE layer_id=?", ("Changed externally", record["layer_id"])),
            ("UPDATE acalc_variants SET name=? WHERE variant_id=?", ("External scenario", self.config_id)),
            ("INSERT INTO acalc_spectra(spectrum_id,name,spectrum_json) VALUES (?,?,?)",
             (new_id(), "External power", json.dumps(spectrum_definition("sound_power", [80] * 8)))),
            ("UPDATE acalc_spectra SET name=?", ("Renamed power",)),
            ("DELETE FROM acalc_spectra", ()),
        ]
        for statement, parameters in mutations:
            before = self.store.metadata()["modified_at"]
            with closing(sqlite3.connect(self.store.path)) as external:
                external.execute(statement, parameters)
                external.commit()
            with self.subTest(statement=statement):
                self.assertGreater(self.store.metadata()["modified_at"], before)


if __name__ == "__main__":
    unittest.main()
