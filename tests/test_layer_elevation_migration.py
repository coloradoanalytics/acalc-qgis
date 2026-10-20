# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Bulk Z edits preserve pending objects across appended model fields."""

from unittest.mock import patch

from qgis.core import QgsVectorLayer

from acalc.layer_elevation import set_layer_z_values
from acalc.layers import configure_layer, load_layer, vertex_rows
from acalc.object_geometry import feature_value
from acalc.schema import LAYER_TYPES, ModelError
from acalc.storage import ModelStore
from tests.test_model import ModelFixture, add_feature


class LayerElevationMigrationTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.pending_layers = []

    def tearDown(self):
        for layer in self.pending_layers:
            if layer.isEditable():
                layer.rollBack()
        self.pending_layers.clear()
        super().tearDown()

    def pending_legacy_object(self, key):
        kind = LAYER_TYPES[key]
        record = self.store.add_layer(key, key)
        with self.store.connection() as db:
            db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN ground_mode')
        layer = QgsVectorLayer(f'{self.path}|layername={record["table_name"]}', key, "ogr")
        self.pending_layers.append(layer)
        added = add_feature(layer, kind)
        reopened = ModelStore.open(self.path)
        configure_layer(layer, reopened, record)
        layer.selectByIds([added.id()])
        return kind, record, reopened, layer, added

    def test_pending_object_fields_and_geometry_are_one_undoable_edit(self):
        for key in ("point_receiver", "terrain_line"):
            with self.subTest(kind=key):
                kind, record, store, layer, added = self.pending_legacy_object(key)
                original_geometry = bytes(added.geometry().asWkb())
                undo_index, undo_count = layer.undoStack().index(), layer.undoStack().count()
                self.assertEqual(set_layer_z_values(layer, value=25), 1)
                updated = next(layer.getFeatures())
                self.assertNotEqual(updated.id(), added.id())
                self.assertEqual(updated["object_id"], added["object_id"])
                self.assertNotIn("infer_z", updated.fields().names())
                self.assertEqual(updated["ground_mode"], "per_point" if kind.geometry == "Point" else "uniform")
                self.assertEqual({row[3] for row in vertex_rows(updated.geometry(), kind)}, {25})
                self.assertEqual(layer.selectedFeatureIds(), [updated.id()])
                self.assertEqual(layer.undoStack().count(), undo_count + 1)
                self.assertEqual(layer.undoStack().index(), undo_index + 1)

                layer.undoStack().undo()
                original = layer.getFeature(added.id())
                self.assertTrue(original.isValid())
                self.assertEqual(bytes(original.geometry().asWkb()), original_geometry)
                self.assertNotIn("infer_z", original.fields().names())
                self.assertIsNone(feature_value(original, "ground_mode"))
                self.assertEqual(layer.undoStack().index(), undo_index)

                layer.undoStack().redo()
                self.assertTrue(layer.commitChanges(), layer.commitErrors())
                saved = next(load_layer(store, record).getFeatures())
                self.assertEqual(saved["object_id"], added["object_id"])
                self.assertNotIn("infer_z", saved.fields().names())
                self.assertEqual(saved["ground_mode"], "per_point" if kind.geometry == "Point" else "uniform")
                self.assertEqual({row[3] for row in vertex_rows(saved.geometry(), kind)}, {25})

    def test_failed_write_restores_migrated_pending_object_and_selection(self):
        kind, record, store, layer, added = self.pending_legacy_object("terrain_line")
        original_geometry = bytes(added.geometry().asWkb())
        undo_index, undo_count = layer.undoStack().index(), layer.undoStack().count()
        change_attribute = layer.changeAttributeValue
        ground_index = layer.fields().indexFromName("ground_mode")

        def fail_ground_mode(fid, index, value, *args, **kwargs):
            return False if index == ground_index else change_attribute(fid, index, value, *args, **kwargs)

        with patch.object(layer, "changeAttributeValue", side_effect=fail_ground_mode):
            with self.assertRaisesRegex(ModelError, "Could not update ground_mode"):
                set_layer_z_values(layer, value=25)
        original = layer.getFeature(added.id())
        self.assertTrue(layer.isEditable())
        self.assertEqual(layer.featureCount(), 1)
        self.assertTrue(original.isValid())
        self.assertEqual(original["object_id"], added["object_id"])
        self.assertEqual(bytes(original.geometry().asWkb()), original_geometry)
        self.assertNotIn("infer_z", original.fields().names())
        self.assertIsNone(feature_value(original, "ground_mode"))
        self.assertEqual(layer.selectedFeatureIds(), [added.id()])
        self.assertEqual(layer.undoStack().count(), undo_count)
        self.assertEqual(layer.undoStack().index(), undo_index)
