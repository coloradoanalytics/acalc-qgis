# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Attached point persistence and validation.

These are application storage/power-accounting regressions, not ISO cases.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from acalc.building_faces import read_face_sources
from acalc.layers import geometry_from_rows, load_layer, validate_layer
from acalc.schema import LAYER_TYPES, ModelError, SCHEMA_VERSION, definition, new_id
from acalc.spectra import spectrum_definition
from acalc.storage import ModelStore
from tests.test_face_sources import ROWS, rectangle
from tests.test_model import ModelFixture, add_feature


def point_source(face="wall:1:2", **values):
    return dict(id=new_id(), name="Point vent", type="point", face=face,
                vertex_count=4, x_m=2, y_m=2, mounting="standoff", distance_m=1.5,
                spectrum_id=None) | values


class AttachedPointStorageTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.record = self.store.add_layer("source_building", "Mixed emitters")
        self.layer = load_layer(self.store, self.record)
        feature = add_feature(self.layer, LAYER_TYPES["source_building"])
        self.layer.changeGeometry(feature.id(), geometry_from_rows(ROWS, LAYER_TYPES["source_building"]))
        self.assertTrue(self.layer.commitChanges())
        self.power = self.store.add_spectrum("Total", spectrum_definition("sound_power", [80]*8))
        self.variant = self.store.variants()[0]["variant_id"]
        self.store.save_variant(self.variant, "Building", definition([self.record["layer_id"]]))

    def sources(self, values):
        self.assertTrue(self.layer.startEditing())
        feature = next(self.layer.getFeatures())
        self.layer.changeAttributeValue(feature.id(), self.layer.fields().indexFromName("face_sources"), json.dumps(values))
        self.assertTrue(self.layer.commitChanges())

    def validate(self, require_spectra=False):
        return validate_layer(self.layer, LAYER_TYPES["source_building"],
                              {row["spectrum_id"]: row for row in self.store.spectra()}, require_spectra)

    def test_persistence_usage_and_catalog_guards_include_both_source_types(self):
        point = point_source("roof", spectrum_id=self.power, frame="building_local", distance_m=1.75)
        area = rectangle(spectrum_id=self.power)
        self.sources([point, area])
        self.assertEqual(self.validate(True), [])
        reopened = ModelStore.open(self.path)
        saved_layer = load_layer(reopened, self.record)
        self.assertEqual(read_face_sources(next(saved_layer.getFeatures())["face_sources"]), [point, area])
        self.assertEqual(reopened.spectrum_usage(self.power)[0]["count"], 2)
        for basis in ("per_square_metre", "per_metre"):
            with self.subTest(basis=basis), self.assertRaisesRegex(ModelError, "building point sources"):
                reopened.save_spectrum(self.power, "Total", spectrum_definition("sound_power", [80]*8, power_basis=basis))
        with self.assertRaisesRegex(ModelError, "used by"):
            reopened.delete_spectrum(self.power)
        self.store.archive_layer(self.record["layer_id"])
        self.assertTrue(reopened.spectrum_usage(self.power)[0]["archived"])
        with self.assertRaisesRegex(ModelError, "building point sources"):
            reopened.save_spectrum(self.power, "Total", spectrum_definition(
                "sound_power", [80]*8, power_basis="per_square_metre"))
        self.store.restore_layer(self.record["layer_id"])
        # Removing only the point permits area power density for the rectangle.
        self.sources([area])
        reopened.save_spectrum(self.power, "Density", spectrum_definition("sound_power", [80]*8, power_basis="per_square_metre"))
        self.assertEqual(self.validate(True), [])
        self.assertEqual(reopened.spectrum_usage(self.power)[0]["count"], 1)
        self.store.archive_layer(self.record["layer_id"])
        self.assertTrue(reopened.spectrum_usage(self.power)[0]["archived"])
        with self.assertRaises(ModelError):
            reopened.delete_spectrum(self.power)

    def test_layer_validation_and_variant_export_reject_point_power_density(self):
        density = self.store.add_spectrum("Area density", spectrum_definition(
            "sound_power", [80]*8, power_basis="per_square_metre"))
        self.sources([point_source(spectrum_id=density)])
        self.assertIn("point sound power", self.validate(True)[0])
        with self.assertRaisesRegex(ModelError, "incompatible power basis"):
            self.store.export_variant(self.variant)
        self.sources([point_source(spectrum_id=self.power)])
        self.assertEqual(self.validate(True), [])
        self.store.export_variant(self.variant)

    def test_unassigned_unknown_missing_and_invalid_point_inputs_are_not_passes(self):
        point = point_source()
        self.sources([point])
        self.assertEqual(self.validate(), [])
        self.assertIn("assign a sound power", self.validate(True)[0])
        unknown = self.store.add_spectrum("Incomplete", spectrum_definition("sound_power", [None]*8))
        self.sources([point | {"spectrum_id": unknown}])
        self.assertIn("bands are unknown", self.validate(True)[0])
        self.sources([point | {"spectrum_id": new_id()}])
        self.assertIn("point sound power", self.validate(True)[0])
        with self.assertRaisesRegex(ModelError, "missing sound power"):
            self.store.export_variant(self.variant)
        for changes in ({"distance_m": -0.1}, {"x_m": 100}, {"vertex_count": 5}):
            with self.subTest(changes=changes):
                self.sources([point | changes])
                self.assertTrue(self.validate())

    def test_schema_13_upgrade_declares_legacy_point_mounting(self):
        # Before schema 13 the distance selected the convention: zero was a
        # surface emitter, a positive distance a displaced point.
        surface, standoff, close = (point_source(distance_m=d) for d in (0, 1.5, .02))
        legacy = [{k: v for k, v in source.items() if k != "mounting"} for source in (surface, standoff, close)]
        legacy.append(rectangle())
        self.sources(legacy)
        table = self.record["table_name"]
        with self.store.connection() as db:
            db.execute("UPDATE acalc_project SET schema_version=12")
            db.execute("UPDATE acalc_layers SET feature_type='building'")
            variant = self.store.variants()[0]
            old = variant["definition"]
            old["schema_version"] = 12
            db.execute("UPDATE acalc_variants SET definition_json=?", (json.dumps(old),))
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.metadata()["schema_version"], SCHEMA_VERSION)
        with closing(sqlite3.connect(reopened.migration_backup)) as db:
            self.assertEqual(json.loads(db.execute(f'SELECT face_sources FROM "{table}"').fetchone()[0]), legacy)
        with reopened.connection(readonly=True) as db:
            upgraded = read_face_sources(db.execute(f'SELECT face_sources FROM "{table}"').fetchone()[0])
        self.assertEqual([s.get("mounting") for s in upgraded], ["surface", "standoff", None, None])
        self.assertEqual(upgraded[:2], [dict(surface, mounting="surface"), dict(standoff, mounting="standoff")])
        # Below the minimum standoff the meaning is ambiguous; it stays undeclared for review.
        self.assertEqual(upgraded[2:], legacy[2:])
        self.assertTrue(Path(reopened.migration_backup).is_file())
