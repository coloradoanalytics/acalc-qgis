# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Shared ground authoring across every point, line and polygon object type."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDialog
from qgis.core import QgsFeature

from acalc.dialogs import FeatureDialog
from acalc.layers import load_layer, validate_layer, vertex_rows
from acalc.schema import LAYER_TYPES, new_id
from acalc.storage import ModelStore
from tests.test_model import ModelFixture, add_feature, rows_for


class GroundModesTests(ModelFixture):
    def test_modes_coordinate_values_and_persistence_for_every_type(self):
        for kind in (kind for kind in LAYER_TYPES.values() if kind.ground_points):
            with self.subTest(kind=kind.key):
                record = self.store.add_layer(kind.key, kind.title)
                layer = load_layer(self.store, record)
                layer.startEditing()
                modes = ["per_point"] if kind.geometry == "Point" else ["per_point", "uniform"]
                for mode in modes:
                    dialog = FeatureDialog(None, kind, "m")
                    self.addCleanup(dialog.close)
                    self.assertFalse(hasattr(dialog, "enabled"))
                    self.assertFalse(hasattr(dialog, "infer_z"))
                    self.assertEqual([dialog.ground_mode.itemData(i) for i in range(dialog.ground_mode.count())], modes)
                    if kind.geometry == "Point":
                        self.assertEqual(dialog.ground_mode.itemText(0), "Specify")
                    dialog.table.setRowCount(0)
                    for row in rows_for(kind):
                        dialog.add_values(row)
                    dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData(mode))
                    if mode == "uniform":
                        dialog.table.item(0, 2).setText("37.5")
                    for row in range(dialog.table.rowCount()):
                        self.assertEqual(bool(dialog.table.item(row, 2).flags() & Qt.ItemIsEditable),
                                         mode == "per_point" or (mode == "uniform" and row == 0))
                    dialog.validate()
                    self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                    expected = [37.5] * dialog.table.rowCount() if mode == "uniform" else [r[3] for r in rows_for(kind)]
                    self.assertEqual([row[3] for row in vertex_rows(dialog.geometry, kind)], expected)
                    feature = QgsFeature(layer.fields())
                    feature["object_id"], feature["name"] = new_id(), mode
                    for name, value in dialog.object_attributes().items():
                        feature[name] = value
                    feature.setGeometry(dialog.geometry)
                    self.assertTrue(layer.addFeature(feature))
                self.assertTrue(layer.commitChanges(), layer.commitErrors())
                reopened = load_layer(ModelStore.open(self.path), record)
                self.assertEqual(validate_layer(reopened, kind), [])
                for feature in reopened.getFeatures():
                    dialog = FeatureDialog(None, kind, "m", feature)
                    self.addCleanup(dialog.close)
                    self.assertEqual(dialog.ground_mode.currentData(), feature["name"])
                    self.assertFalse(hasattr(dialog, "infer_ground"))
