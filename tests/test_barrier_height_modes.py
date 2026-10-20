# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Barrier authoring, persistence and migration (no acoustic reference claims)."""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDialog
from qgis.core import QgsFeature
from acalc.dialogs import FeatureDialog
from acalc.layer_elevation import set_layer_z_values
from acalc.layers import geometry_from_rows, load_layer, vertex_rows, validate_layer
from acalc.schema import LAYER_TYPES, new_id
from acalc.storage import ModelStore
from tests.test_model import APP, ModelFixture

KIND = LAYER_TYPES['barrier']
ROWS = [(0, 0, 0, 10, 3), (0, 10, 0, 14, 5)]

class BarrierHeightTests(ModelFixture):
    def dialog(self, feature=None, unit='m'):
        dialog = FeatureDialog(None, KIND, unit, feature, elevation_unit=unit)
        self.addCleanup(APP.processEvents)
        self.addCleanup(dialog.close)
        if feature is None:
            dialog.table.setRowCount(0)
            for row in ROWS:
                dialog.add_values(row)
        return dialog

    def mode(self, dialog, mode):
        dialog.height_mode.setCurrentIndex(dialog.height_mode.findData(mode))

    def saved(self, dialog):
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        return vertex_rows(dialog.geometry, KIND)

    def test_modes_nominal_height_and_point_edits(self):
        d = self.dialog()
        self.assertEqual([d.height_mode.itemText(i) for i in range(d.height_mode.count())],
                         ['Relative to average Z', 'Relative per point', 'Absolute per point'])
        d.height_value.setValue(8)
        self.assertEqual([r[4] for r in self.saved(d)], [8, 8])
        d.table.item(1, 3).setText('9')
        self.assertEqual([r[4] for r in self.saved(d)], [8, 9])
        self.mode(d, 'average')
        self.assertTrue(d.height_value.isEnabled())
        self.assertFalse(d.table.item(0, 3).flags() & Qt.ItemIsEditable)
        self.assertEqual([r[3] + r[4] for r in self.saved(d)], [20, 20])
        self.mode(d, 'individual')
        self.assertEqual([r[4] for r in self.saved(d)], [8, 9])
        self.mode(d, 'absolute_individual')
        self.assertFalse(d.height_value.isEnabled())
        d.table.item(0, 2).setText('12')
        self.assertEqual([r[3] + r[4] for r in self.saved(d)], [18, 23])

    def test_feet_and_new_points(self):
        d = self.dialog(unit='ft')
        d.height_value.setValue(3.048)
        self.assertAlmostEqual(float(d.table.item(0, 3).text()), 10)
        self.assertEqual([r[4] for r in self.saved(d)], [3.048, 3.048])
        d.add_row()
        self.assertAlmostEqual(float(d.table.item(2, 3).text()), 10)

    def test_below_ground_rejected(self):
        for mode in ('average', 'absolute_individual'):
            d = self.dialog()
            self.mode(d, mode)
            d.height_value.setValue(1)
            if mode == 'absolute_individual':
                d.table.item(0, 3).setText('9')
            d.validate()
            self.assertEqual(d.result(), QDialog.Rejected)
            self.assertIn('below', d.error.text())

    def test_roundtrip_and_ground_edit(self):
        record = self.store.add_layer('barrier', 'Barriers')
        layer = load_layer(self.store, record)
        layer.startEditing()
        for mode in ('average', 'individual', 'absolute_individual'):
            d = self.dialog()
            d.height_value.setValue(8)
            self.mode(d, mode)
            self.saved(d)
            f = QgsFeature(layer.fields())
            f['object_id'], f['name'], f['enabled'] = new_id(), mode, 1
            for name, value in d.object_attributes().items():
                f[name] = value
            f.setGeometry(d.geometry)
            self.assertTrue(layer.addFeature(f))
        self.assertTrue(layer.commitChanges())
        layer = load_layer(ModelStore.open(self.path), record)
        self.assertFalse(validate_layer(layer, KIND))
        for f in layer.getFeatures():
            d = self.dialog(f)
            self.assertEqual(d.height_mode.currentData(), f['name'])
            self.assertEqual(d.height_value.value(), 8)
            self.assertEqual(self.saved(d), vertex_rows(f.geometry(), KIND))
        set_layer_z_values(layer, value=5)
        for f in layer.getFeatures():
            rows = vertex_rows(f.geometry(), KIND)
            self.assertEqual([r[3] + r[4] for r in rows],
                             [18, 22] if f['name'] == 'absolute_individual' else [13, 13])

    def test_schema13_migration_preserves_geometry(self):
        self.check_migration(13)

    def test_schema14_migration_preserves_geometry(self):
        self.check_migration(14)

    def check_migration(self, version):
        record = self.store.add_layer('barrier', 'Legacy barrier')
        layer = load_layer(self.store, record)
        layer.startEditing()
        f = QgsFeature(layer.fields())
        f['object_id'], f['name'], f['enabled'] = new_id(), 'Legacy', 1
        f.setGeometry(geometry_from_rows(ROWS, KIND))
        layer.addFeature(f)
        self.assertTrue(layer.commitChanges())
        del layer
        with self.store.connection() as db:
            db.execute('UPDATE acalc_project SET schema_version=?', (version,))
            db.execute("UPDATE acalc_variants SET definition_json=json_set(definition_json, '$.schema_version', ?)", (version,))
            db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN height_mode')
            db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN height_value')
        reopened = ModelStore.open(self.path)
        self.assertTrue(reopened.migration_backup)
        layer = load_layer(reopened, record)
        f = next(layer.getFeatures())
        self.assertEqual(f['height_mode'], 'individual')
        self.assertEqual(f['height_value'], 3)
        self.assertEqual(vertex_rows(f.geometry(), KIND), ROWS)
