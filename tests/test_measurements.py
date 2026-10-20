# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Measurement authoring, persistence, and result presentation."""

import json
import math
from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog
from qgis.core import QgsExpression, QgsExpressionContext

from acalc.dialogs import FeatureDialog
from acalc.layers import load_layer, validate_layer
from acalc.object_geometry import feature_value
from acalc.measurement import MEASUREMENT_FIELDS, broadband_from_octaves, validate_measurement
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError, definition, SCHEMA_VERSION
from acalc.storage import ModelStore
from tests.test_model import APP, FakeIface, ModelFixture


class MeasurementTests(ModelFixture):
    def test_values_are_authored_and_display_without_a_calculation(self):
        record = self.store.add_layer("measurement", "Field survey")
        variant = self.store.variants()[0]
        self.store.save_variant(variant["variant_id"], variant["name"], definition([record["layer_id"]]))
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            plugin.dock.show_view("measurement")

            def author(dialog):
                self.assertEqual(dialog.kind.key, "measurement")
                for column, value in enumerate((100, 200, 0, 1.5)):
                    dialog.table.item(0, column).setText(str(value))
                dialog.name.setText("North monitor")
                dialog.notes.setPlainText("Field observation; microphone at 1.5 m")
                dialog.measurement_a.setText("47.25")
                dialog.measurement_bands.item(0, 0).setText("0")
                self.assertEqual(dialog.measurement_a.text(), "-26.2")
                dialog.measurement_bands.item(0, 1).setText("42.5")
                self.assertEqual(dialog.measurement_a.text(), "26.4")
                self.assertTrue(dialog.measurement_a.isReadOnly())
                dialog.validate()
                self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                return QDialog.Accepted

            with patch.object(FeatureDialog, "exec", author):
                plugin.edit_feature(True)
            layer = plugin.layers[record["layer_id"]]
            self.assertEqual(validate_layer(layer, LAYER_TYPES["measurement"]), [])
            plugin.save_layer()
            reopened = ModelStore.open(self.path)
            values = reopened.measurements(variant["variant_id"])
            self.assertEqual(len(values), 1)
            self.assertEqual(values[0]["broadband_a_db"], 26.4)
            self.assertNotIn("broadband_z_db", values[0])
            self.assertEqual(values[0]["octave_levels"][:3], [0.0, 42.5, None])
            feature = next(layer.getFeatures())
            self.assertAlmostEqual(feature["broadband_a_db"], values[0]["broadband_a_db"])
            self.assertTrue(layer.labelsEnabled())
            label = QgsExpression(layer.labeling().settings().fieldName)
            context = QgsExpressionContext()
            context.setFeature(feature)
            self.assertEqual(label.evaluate(context), "26.4 dBA")
            feature["broadband_a_db"] = None
            context.setFeature(feature)
            self.assertIsNone(label.evaluate(context))
            self.assertTrue(layer.startEditing())
            self.assertTrue(layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("level_125_db"), 43.0))
            plugin.save_layer()
            updated = reopened.measurements(variant["variant_id"])[0]
            self.assertIsNone(feature_value(next(layer.getFeatures()), "broadband_z_db"))
            context.setFeature(next(layer.getFeatures()))
            self.assertEqual(label.evaluate(context), "26.9 dBA")
            plugin.reload_layers()
            layer = plugin.layers[record["layer_id"]]
            self.assertTrue(layer.labelsEnabled())
            self.assertEqual(QgsExpression(layer.labeling().settings().fieldName).evaluate(context), "26.9 dBA")
            plugin.dock.show_view("results")
            page = plugin.dock.results_page
            self.assertEqual(page.table.rowCount(), 1)
            self.assertIn("Measurement", page.table.item(0, 0).text())
            self.assertEqual(page.table.item(0, 1).text(), "26.9")
            self.assertEqual(page.table.item(0, 2).text(), "0.0")
            self.assertEqual(page.table.item(0, 4).text(), "")
        finally:
            for layer in plugin.layers.values():
                if layer.isEditable():
                    layer.rollBack()
            plugin.unload()
            iface.window.close()
            APP.processEvents()

    def test_invalid_measurements_are_rejected_and_schema_nine_upgrades(self):
        with self.assertRaises(ModelError):
            validate_measurement({MEASUREMENT_FIELDS[0]: math.nan})
        variant = self.store.variants()[0]
        with self.store.connection() as db:
            db.execute("UPDATE acalc_project SET schema_version=9")
            db.execute("UPDATE acalc_variants SET definition_json=?",
                       (json.dumps({"schema_version": 9, "layer_ids": []}),))
        upgraded = ModelStore.open(self.path)
        self.assertEqual(upgraded.metadata()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(upgraded.variants()[0]["variant_id"], variant["variant_id"])
        self.assertIsNotNone(upgraded.migration_backup)

    def test_broadband_math_uses_entered_bands_and_zero_is_a_level(self):
        dba = broadband_from_octaves([0] * 8)
        self.assertAlmostEqual(dba, 10 * math.log10(sum(10 ** (weight / 10) for weight in
            (-26.2, -16.1, -8.6, -3.2, 0, 1.2, 1, -1.1))))
        self.assertIsNone(broadband_from_octaves([None] * 8))
        self.assertEqual(broadband_from_octaves([None] * 4 + [50] + [None] * 3), 50)

    def test_editor_switches_between_manual_and_octave_derived_broadband(self):
        dialog = FeatureDialog(None, LAYER_TYPES["measurement"], "m")
        try:
            dialog.measurement_a.setText("54")
            self.assertEqual(dialog.measurement_values()["broadband_a_db"], 54)
            dialog.measurement_bands.item(0, 4).setText("50")
            self.assertEqual(dialog.measurement_a.text(), "50.0")
            self.assertTrue(dialog.measurement_a.isReadOnly())
            dialog.measurement_bands.item(0, 4).setText("")
            self.assertEqual(dialog.measurement_a.text(), "")
            self.assertFalse(dialog.measurement_a.isReadOnly())
            self.assertEqual(dialog.measurement_values()["broadband_a_db"], None)
        finally:
            dialog.close()
