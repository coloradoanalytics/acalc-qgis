# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Building roles, legacy migration and component catalog references."""

import json
import sqlite3
from contextlib import closing

from acalc.layers import geometry_from_rows, load_layer, validate_layer
from acalc.schema import BUILDING_TYPES, LAYER_TYPES, SCHEMA_VERSION, SOURCE_TYPES, ModelError, definition, new_id
from acalc.spectra import spectrum_definition
from acalc.storage import ModelStore
from tests.test_model import ModelFixture, add_feature


ROWS = [(0, 0, 0, 10, 6), (0, 10, 0, 10, 6), (0, 10, 8, 10, 6), (0, 0, 8, 10, 6)]


def component(**values):
    return dict(id=new_id(), name="Vent", face="wall:0:1", vertex_count=4,
                x_m=1, y_m=2, width_m=3, height_m=2, spectrum_id=None) | values


class SourceBuildingStorageTests(ModelFixture):
    def building_layer(self, kind_key="source_building", attachments=(None,)):
        record = self.store.add_layer(kind_key, kind_key)
        kind = LAYER_TYPES[kind_key]
        layer = load_layer(self.store, record)
        for sources in attachments:
            feature = add_feature(layer, kind)
            self.assertTrue(layer.changeGeometry(feature.id(), geometry_from_rows(ROWS, kind)))
            if sources is not None:
                self.assertTrue(layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("face_sources"), sources))
        self.assertTrue(layer.commitChanges(), layer.commitErrors())
        return record, layer

    def saved_rows(self, record):
        with self.store.connection(readonly=True) as db:
            return [tuple(row) for row in db.execute(f'SELECT * FROM "{record["table_name"]}" ORDER BY fid')]

    def test_building_roles_and_shared_authoring_fields(self):
        self.assertEqual(BUILDING_TYPES, {"building", "source_building"})
        self.assertIn("source_building", SOURCE_TYPES)
        self.assertNotIn("building", SOURCE_TYPES)
        for kind_key in BUILDING_TYPES:
            with self.subTest(kind=kind_key):
                kind = LAYER_TYPES[kind_key]
                record, layer = self.building_layer(kind_key)
                self.assertTrue(kind.is_building)
                self.assertEqual(kind.is_source, kind_key == "source_building")
                self.assertFalse(kind.spectrum)
                self.assertEqual(kind.dimensions, "ZM")
                self.assertNotIn("spectrum_id", layer.fields().names())
                for field in ("height_mode", "height_value", "reflection_coefficient", "absorption_spectrum_id", "ground_mode", "face_sources"):
                    self.assertIn(field, layer.fields().names())
                self.assertEqual(validate_layer(layer, kind), [])
                saved = next(row for row in ModelStore.open(self.path).layers() if row["layer_id"] == record["layer_id"])
                self.assertEqual(saved["feature_type"], kind_key)
                if kind_key == "building":
                    index = layer.fields().indexFromName("face_sources")
                    self.assertEqual(layer.editorWidgetSetup(index).type(), "Hidden")
                    self.assertTrue(layer.editFormConfig().readOnly(index))
                    self.assertTrue(next(c.hidden for c in layer.attributeTableConfig().columns() if c.name == "face_sources"))

    def test_legacy_layers_migrate_by_content_without_changing_objects_or_membership(self):
        power = self.store.add_spectrum("Power", spectrum_definition("sound_power", [80] * 8))
        payload = json.dumps([component(spectrum_id=power)])
        records = []
        for attachments in ((None, "", "[ ]"), (payload, None), (payload,), ("[]",)):
            record, layer = self.building_layer("building", attachments)
            records.append(record)
            del layer
        self.store.archive_layer(records[2]["layer_id"])
        self.store.archive_layer(records[3]["layer_id"])
        variant = self.store.variants()[0]
        membership = [r["layer_id"] for r in records[:2]]
        self.store.save_variant(variant["variant_id"], "Mixed buildings", definition(membership))
        snapshots = {r["layer_id"]: self.saved_rows(r) for r in records}
        with self.store.connection() as db:
            db.execute("UPDATE acalc_project SET schema_version=13")
            old = definition(membership) | {"schema_version": 13}
            db.execute("UPDATE acalc_variants SET definition_json=?", (json.dumps(old),))
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.metadata()["schema_version"], SCHEMA_VERSION)
        migrated = {r["layer_id"]: r for r in reopened.layers() + reopened.layers(archived=True)}
        for index, record in enumerate(records):
            with self.subTest(layer=index):
                self.assertEqual(migrated[record["layer_id"]]["feature_type"], "source_building" if index in (1, 2) else "building")
                self.assertEqual(self.saved_rows(record), snapshots[record["layer_id"]])
                for field in ("name", "table_name", "sort_order", "created_at"):
                    self.assertEqual(migrated[record["layer_id"]][field], record[field])
        self.assertEqual(reopened.variants()[0]["definition"], definition(membership))
        self.assertEqual(reopened.variants()[0]["variant_id"], variant["variant_id"])
        self.assertTrue(migrated[records[2]["layer_id"]]["archived"])
        usage = reopened.spectrum_usage(power)
        self.assertEqual({r["layer_id"] for r in usage}, {r["layer_id"] for r in records[1:3]})
        with closing(sqlite3.connect(reopened.migration_backup)) as backup:
            self.assertEqual(backup.execute("SELECT schema_version FROM acalc_project").fetchone()[0], 13)
            self.assertEqual({r[0] for r in backup.execute("SELECT feature_type FROM acalc_layers")}, {"building"})
        again = ModelStore.open(self.path)
        self.assertEqual(again.layers(), reopened.layers())
        self.assertEqual(again.layers(archived=True), reopened.layers(archived=True))

    def test_regular_buildings_reject_attachments_in_validation_and_export(self):
        record, layer = self.building_layer("building", (json.dumps([component()]),))
        errors = validate_layer(layer, LAYER_TYPES["building"])
        self.assertIn("Buildings cannot contain attached sources", errors[0])
        variant = self.store.variants()[0]["variant_id"]
        self.store.save_variant(variant, "Buildings", definition([record["layer_id"]]))
        with self.assertRaisesRegex(ModelError, "Use Source Buildings"):
            self.store.export_variant(variant)

    def test_source_components_and_material_references_include_archived_layers(self):
        power = self.store.add_spectrum("Power", spectrum_definition("sound_power", [80] * 8))
        material = self.store.add_spectrum("Material", spectrum_definition("absorption", [.4] * 8))
        point = dict(id=new_id(), name="Point vent", type="point", face="wall:1:2", vertex_count=4,
                     x_m=2, y_m=2, mounting="standoff", distance_m=1.5, spectrum_id=power)
        record, layer = self.building_layer(attachments=(json.dumps([component(spectrum_id=power), point]),))
        self.assertTrue(layer.startEditing())
        feature = next(layer.getFeatures())
        self.assertTrue(layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("absorption_spectrum_id"), material))
        self.assertTrue(layer.commitChanges(), layer.commitErrors())
        spectra = {row["spectrum_id"]: row for row in self.store.spectra()}
        self.assertEqual(validate_layer(layer, LAYER_TYPES["source_building"], spectra, True), [])
        self.assertEqual(self.store.spectrum_usage(power)[0]["count"], 2)
        self.assertEqual(self.store.spectrum_usage(material)[0]["count"], 1)
        self.store.archive_layer(record["layer_id"])
        self.assertTrue(self.store.spectrum_usage(power)[0]["archived"])
        for identity in (power, material):
            with self.subTest(spectrum=identity), self.assertRaisesRegex(ModelError, "used by"):
                self.store.delete_spectrum(identity)
        with self.assertRaisesRegex(ModelError, "building point sources"):
            self.store.save_spectrum(power, "Power", spectrum_definition("sound_power", [80] * 8, power_basis="per_square_metre"))
