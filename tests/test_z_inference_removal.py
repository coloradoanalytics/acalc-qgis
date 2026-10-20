# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Explicit-Z schema migration and removal of the inference request path."""
import json
from pathlib import Path
from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog

from acalc.dialogs import FeatureDialog
from acalc.layers import load_layer, validate_layer
from acalc.schema import LAYER_TYPES, ModelError, SCHEMA_VERSION
from acalc.storage import ModelStore
from tests.test_model import ModelFixture, add_feature


def legacy_schema11(store, inferred=True):
    """Reconstruct the old column contract, independently of the migration."""
    with store.connection() as db:
        for layer in store.layers():
            if LAYER_TYPES[layer['feature_type']].elevation:
                db.execute(f'ALTER TABLE "{layer["table_name"]}" ADD COLUMN infer_z INTEGER NOT NULL DEFAULT {int(inferred)}')
        db.execute('UPDATE acalc_project SET schema_version=11')
        for row in db.execute('SELECT variant_id,definition_json FROM acalc_variants').fetchall():
            value = json.loads(row['definition_json'])
            value['schema_version'] = 11
            db.execute('UPDATE acalc_variants SET definition_json=? WHERE variant_id=?', (json.dumps(value), row['variant_id']))


class ZInferenceRemovalTests(ModelFixture):
    def objects(self):
        originals = {}
        for kind in LAYER_TYPES.values():
            record = self.store.add_layer(kind.key, kind.title)
            layer = load_layer(self.store, record)
            add_feature(layer, kind)
            self.assertTrue(layer.commitChanges())
            feature = next(layer.getFeatures())
            originals[record['layer_id']] = (feature['object_id'], bytes(feature.geometry().asWkb()))
        return originals

    def test_every_object_type_has_no_inference_fields_or_controls(self):
        self.objects()
        for record in self.store.layers():
            kind = LAYER_TYPES[record['feature_type']]
            layer = load_layer(self.store, record)
            self.assertNotIn('infer_z', layer.fields().names())
            dialog = FeatureDialog(None, kind, 'm', next(layer.getFeatures()))
            self.addCleanup(dialog.close)
            self.assertFalse(hasattr(dialog, 'infer_ground'))
            self.assertNotIn('infer_z', dialog.object_attributes())
            if kind.ground_points:
                self.assertEqual(dialog.ground_mode.findData('terrain'), -1)
            else:
                self.assertNotIn('ground_mode', layer.fields().names())

    def test_schema11_inferred_objects_need_explicit_z_and_backup_retains_originals(self):
        originals = self.objects()
        legacy_schema11(self.store)
        upgraded = ModelStore.open(self.path)
        self.assertEqual(upgraded.metadata()['schema_version'], SCHEMA_VERSION)
        backup = ModelStore(upgraded.migration_backup)
        self.assertTrue(Path(backup.path).is_file())
        for record in upgraded.layers():
            kind = LAYER_TYPES[record['feature_type']]
            layer = load_layer(upgraded, record)
            feature = next(layer.getFeatures())
            self.assertEqual((feature['object_id'], bytes(feature.geometry().asWkb())), originals[record['layer_id']])
            self.assertNotIn('infer_z', layer.fields().names())
            if kind.ground_points:
                self.assertEqual(feature['ground_mode'], 'unresolved')
                self.assertTrue(any('explicit ground Z' in error for error in validate_layer(layer, kind)))
                dialog = FeatureDialog(None, kind, 'm', feature)
                self.addCleanup(dialog.close)
                self.assertTrue(all(dialog.table.item(row, 2).text() == '' for row in range(dialog.table.rowCount())))
                dialog.validate()
                self.assertEqual(dialog.result(), QDialog.Rejected)
            with backup.connection(readonly=True) as db:
                columns = {row['name'] for row in db.execute(f'PRAGMA table_info("{record["table_name"]}")')}
                self.assertEqual('infer_z' in columns, kind.elevation)
        self.assertIsNone(ModelStore.open(self.path).migration_backup)

    def test_explicit_objects_keep_ordinates_and_authoring_mode(self):
        originals = self.objects()
        legacy_schema11(self.store, inferred=False)
        upgraded = ModelStore.open(self.path)
        for record in upgraded.layers():
            layer = load_layer(upgraded, record)
            feature = next(layer.getFeatures())
            self.assertEqual((feature['object_id'], bytes(feature.geometry().asWkb())), originals[record['layer_id']])
            if LAYER_TYPES[record['feature_type']].ground_points:
                self.assertIn(str(feature['ground_mode']), ('NULL', 'per_point'))

    def test_failed_migration_rolls_back_column_removal_and_keeps_backup(self):
        self.objects()
        legacy_schema11(self.store)
        migrate = ModelStore._remove_z_inference

        def fail(db):
            migrate(db)
            raise ModelError('Interrupted migration')

        with patch.object(ModelStore, '_remove_z_inference', side_effect=fail):
            with self.assertRaisesRegex(ModelError, 'Interrupted migration'):
                ModelStore.open(self.path)
        self.assertEqual(self.store.metadata()['schema_version'], 11)
        self.assertTrue(list(Path(self.directory.name).glob('*.schema-11-backup-*.gpkg')))
        with self.store.connection(readonly=True) as db:
            for record in self.store.layers():
                if LAYER_TYPES[record['feature_type']].elevation:
                    self.assertEqual(db.execute(f'SELECT infer_z FROM "{record["table_name"]}"').fetchone()[0], 1)
        self.assertEqual(ModelStore.open(self.path).metadata()['schema_version'], SCHEMA_VERSION)

    def test_open_nonediting_legacy_layer_refreshes_removed_columns(self):
        from qgis.core import QgsProject
        from acalc.plugin import AcalcPlugin
        from tests.test_model import FakeIface
        self.objects()
        legacy_schema11(self.store, inferred=False)
        record = self.store.layers()[0]
        layer = load_layer(self.store, record)
        self.assertIn('infer_z', layer.fields().names())
        QgsProject.instance().addMapLayer(layer)
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(iface.window.close)
        self.addCleanup(plugin.unload)
        plugin.open_project(self.path)
        self.assertIs(plugin.layers[record['layer_id']], layer)
        self.assertNotIn('infer_z', layer.fields().names())

    def test_open_legacy_edit_buffer_requires_session_to_close_before_migration(self):
        from qgis.core import QgsProject
        from acalc.plugin import AcalcPlugin
        from tests.test_model import FakeIface
        self.objects()
        legacy_schema11(self.store, inferred=False)
        record = self.store.layers()[0]
        layer = load_layer(self.store, record)
        QgsProject.instance().addMapLayer(layer)
        layer.startEditing()
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(iface.window.close)
        self.addCleanup(plugin.unload)
        with self.assertRaisesRegex(ModelError, 'close its edit session'):
            plugin.open_project(self.path)
        self.assertEqual(self.store.metadata()['schema_version'], 11)
        self.assertIn('infer_z', layer.fields().names())
        layer.rollBack()
