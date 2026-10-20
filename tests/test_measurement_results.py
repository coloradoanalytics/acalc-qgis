# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Measurement prediction persistence and properties comparisons."""

import unittest

from qgis.PyQt.QtWidgets import QAbstractItemView

from acalc.api_model import prepare_calculation, save_completed_calculation
from acalc.dialogs import FeatureDialog
from acalc.layers import load_layer
from acalc.result_layers import clear_variant_results, sync_result_layers
from acalc.schema import LAYER_TYPES, definition
from acalc.storage import ModelStore
from tests import test_api_model as fixtures
from tests.test_model import APP


class MeasurementResultTests(unittest.TestCase):
    def setUp(self):
        self.scene = fixtures.ApiModelTests()
        self.scene.setUp()
        self.addCleanup(self.scene.tearDown)
        self.scene.scene()
        self.layer, self.object_id = self.scene.feature(
            "measurement", [(50, 0, 12, 1.5)], name="Survey point",
            level_63_db=0, level_125_db=40, level_250_db=45,
            level_1000_db=42, level_2000_db=50, level_4000_db=55, level_8000_db=60)
        self.empty_layer, self.empty_id = self.scene.feature(
            "measurement", [(75, 0, 12, 1.5)], name="Unmeasured point")
        self.store = self.scene.store
        self.variant_id = self.scene.variant_id
        self.other_id = self.store.add_variant("Other conditions", definition(self.scene.layers))
        self.excluded_id = self.store.add_variant("Without measurements", definition(self.scene.layers[:2]))
        self.observations = self.store.measurements(self.variant_id)

    def calculate(self, variant_id, silent=False):
        snapshot = prepare_calculation(self.store, variant_id, fixtures.CAPABILITIES)
        save_completed_calculation(self.store, snapshot, self.scene.result(snapshot, silent),
                                   fixtures.BACKEND, fixtures.JOB)
        return snapshot

    def test_prediction_roundtrip_variant_membership_replacement_and_clear(self):
        snapshot = self.calculate(self.variant_id)
        receivers = snapshot["request"]["receivers"]
        self.assertEqual(len(receivers), 3)
        for record, identity, x in ((self.layer, self.object_id, 50),
                                    (self.empty_layer, self.empty_id, 75)):
            receiver = next(r for r in receivers if snapshot["id_mapping"][r["id"]]["object_id"] == identity)
            self.assertEqual(receiver["ground_ref"], {"position": [x, 0, 12]})
            self.assertEqual(receiver["height_m"], 1.5)
            saved = self.store.receiver_results(self.variant_id, layer_id=record["layer_id"], object_id=identity)
            self.assertEqual(saved[0]["octave_levels"], [42] * 8)
            self.assertEqual(saved[0]["receiver_name"], "Survey point" if x == 50 else "Unmeasured point")
        self.calculate(self.other_id, silent=True)
        self.assertEqual(self.store.receiver_results(self.other_id)[0]["octave_levels"], [None] * 8)
        excluded = self.calculate(self.excluded_id)
        self.assertEqual(len(excluded["request"]["receivers"]), 1)
        self.assertEqual(self.store.measurements(self.excluded_id), [])

        projections = sync_result_layers(self.store)
        self.assertEqual({r["receiver_layer_id"] for r in projections if r["variant_id"] == self.variant_id},
                         {r["layer_id"] for r in snapshot["receiver_layers"]})
        reopened = ModelStore.open(self.store.path)
        self.assertEqual(reopened.measurements(self.variant_id), self.observations)
        self.assertEqual(reopened.receiver_results(self.variant_id)[0]["octave_levels"], [42] * 8)
        self.calculate(self.variant_id, silent=True)
        self.assertEqual(len(self.store.calculation_runs(self.variant_id)), 1)
        self.assertEqual(self.store.receiver_results(self.variant_id)[0]["octave_levels"], [None] * 8)
        clear_variant_results(self.store, self.variant_id)
        self.assertEqual(self.store.receiver_results(self.variant_id), [])
        self.assertEqual(self.store.measurements(self.variant_id), self.observations)
        self.assertEqual(len(self.store.receiver_results(self.other_id)), 3)

    def test_properties_compare_bands_and_follow_edits_and_variant_selection(self):
        self.calculate(self.variant_id)
        self.calculate(self.other_id, silent=True)
        layer = load_layer(self.store, self.layer)
        dialog = FeatureDialog(None, LAYER_TYPES["measurement"], "m", next(layer.getFeatures()),
                               results_context=(self.store, self.layer["layer_id"], self.object_id, self.variant_id))
        try:
            page = dialog.receiver_results
            table = page.table
            self.assertEqual(dialog.tabs.tabText(dialog.tabs.indexOf(page)), "Measured vs calculated")
            self.assertEqual(table.editTriggers(), QAbstractItemView.NoEditTriggers)
            self.assertEqual([table.item(row, 0).text() for row in range(3)],
                             ["Measured", "Calculated", "Calculated − measured"])
            self.assertEqual([table.item(1, col).text() for col in range(2, 10)], ["42.0"] * 8)
            self.assertEqual([table.item(2, col).text() for col in range(2, 10)],
                             ["+42.0", "+2.0", "-3.0", "", "+0.0", "-8.0", "-13.0", "-18.0"])
            self.assertEqual(table.item(0, 2).text(), "0.0")
            measured_a = dialog.measurement_values()["broadband_a_db"]
            self.assertEqual(table.item(2, 1).text(), f"{48.99 - measured_a:+.1f}")
            dialog.measurement_bands.item(0, 3).setText("44")
            self.assertEqual(table.item(0, 5).text(), "44.0")
            self.assertEqual(table.item(2, 5).text(), "-2.0")
            page.variant.setCurrentIndex(page.variant.findData(self.other_id))
            self.assertEqual(table.item(1, 2).text(), "−∞")
            self.assertEqual(table.item(2, 2).text(), "−∞")
            page.variant.setCurrentIndex(page.variant.findData(self.excluded_id))
            self.assertEqual(table.item(0, 5).text(), "44.0")
            self.assertTrue(all(table.item(row, col).text() == ""
                                for row in (1, 2) for col in range(1, 10)))
            page.variant.setCurrentIndex(page.variant.findData(self.variant_id))
            self.assertEqual(table.item(1, 2).text(), "42.0")
            self.assertEqual(self.store.measurements(self.variant_id), self.observations)
        finally:
            dialog.close()
            APP.processEvents()

    def test_unsaved_measurement_shows_observations_without_fabricated_predictions(self):
        dialog = FeatureDialog(None, LAYER_TYPES["measurement"], "m")
        try:
            dialog.measurement_a.setText("54")
            table = dialog.receiver_results.table
            self.assertEqual(table.item(0, 1).text(), "54.0")
            self.assertTrue(all(table.item(row, col).text() == ""
                                for row in (1, 2) for col in range(1, 10)))
        finally:
            dialog.close()
            APP.processEvents()
