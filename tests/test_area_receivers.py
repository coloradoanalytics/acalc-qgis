# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Area receiver authoring uses real QGIS edit buffers and GeoPackages."""

from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog

from acalc.dialogs import FeatureDialog
from acalc.layers import load_layer, validate_layer, vertex_rows
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES
from acalc.storage import ModelError, ModelStore
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


class AreaReceiverAuthoringTests(ModelFixture):
    def test_height_spacing_defaults_edit_undo_save_and_reopen(self):
        record = self.store.add_layer("area_receiver", "Garden")
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            layer = plugin.layers[record["layer_id"]]

            def accept(dialog):
                self.assertEqual(dialog.height_m.value(), 1.5)
                self.assertEqual(dialog.spacing_m.value(), 10)
                self.assertEqual(dialog.columns, ["X", "Y"])
                self.assertFalse(hasattr(dialog, "ground_mode"))
                self.assertFalse(hasattr(dialog, "raster_button"))
                dialog.height_m.setValue(4)
                dialog.spacing_m.setValue(2.5)
                for row, (x, y) in enumerate(((0, 0), (10, 0), (0, 10))):
                    for column, value in enumerate((x, y)):
                        dialog.table.item(row, column).setText(str(value))
                dialog.validate()
                return dialog.result()

            with patch.object(FeatureDialog, "exec", accept):
                self.assertTrue(plugin.edit_feature(create=True, layer=layer))
            feature = next(layer.getFeatures())
            self.assertEqual((feature["height_m"], feature["spacing_m"]), (4, 2.5))
            self.assertFalse({"infer_z", "ground_mode"} & set(feature.fields().names()))
            self.assertEqual([row[3] for row in vertex_rows(feature.geometry(), LAYER_TYPES["area_receiver"])], [0]*3)
            self.assertEqual([row[4] for row in vertex_rows(feature.geometry(), LAYER_TYPES["area_receiver"])], [4]*3)
            self.assertEqual(validate_layer(layer, LAYER_TYPES["area_receiver"]), [])
            layer.undoStack().undo()
            self.assertEqual(layer.featureCount(), 0)
            layer.undoStack().redo()
            plugin.commit_layer(layer)
            loaded = load_layer(ModelStore.open(self.path), record)
            feature = next(loaded.getFeatures())
            dialog = FeatureDialog(None, LAYER_TYPES["area_receiver"], "m", feature)
            self.assertEqual((dialog.height_m.value(), dialog.spacing_m.value()), (4, 2.5))
            self.assertTrue(hasattr(dialog, "receiver_results"))
            self.assertEqual(dialog.columns, ["X", "Y"])
            dialog.close()
        finally:
            plugin.unload()
            iface.window.close()
            APP.processEvents()

    def test_legacy_ground_fields_are_ignored_and_hidden(self):
        kind = LAYER_TYPES["area_receiver"]
        record = self.store.add_layer(kind.key, "Legacy garden")
        layer = load_layer(self.store, record)
        feature = add_feature(layer, kind)
        self.assertEqual(validate_layer(layer, kind), [])
        self.assertFalse({"infer_z", "ground_mode"} & set(layer.fields().names()))
        dialog = FeatureDialog(None, kind, "m", feature)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.table.columnCount(), 2)
        self.assertFalse(hasattr(dialog, "ground_mode"))
        dialog.height_m.setValue(3.25)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        self.assertEqual([row[3:] for row in vertex_rows(dialog.geometry, kind)], [(0, 3.25)]*4)

    def test_invalid_properties_and_missing_schema_are_rejected(self):
        record = self.store.add_layer("area_receiver", "Garden")
        layer = load_layer(self.store, record)
        feature = add_feature(layer, LAYER_TYPES["area_receiver"])
        for field, value, message in (("spacing_m", 0, "spacing"), ("spacing_m", -1, "spacing"),
                                       ("height_m", -1, "Height"), ("height_m", None, "Height")):
            with self.subTest(field=field, value=value):
                index = layer.fields().indexFromName(field)
                old = layer.getFeature(feature.id())[field]
                layer.changeAttributeValue(feature.id(), index, value)
                self.assertIn(message, validate_layer(layer, LAYER_TYPES["area_receiver"])[0])
                layer.changeAttributeValue(feature.id(), index, old)
        self.assertTrue(layer.commitChanges(), layer.commitErrors())
        del feature, layer
        with self.store.connection() as db:
            db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN spacing_m')
        with self.assertRaisesRegex(ModelError, "Missing required fields"):
            ModelStore.open(self.path)
