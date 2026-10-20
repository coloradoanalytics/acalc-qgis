# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Source-power fitting, one-pass relationships, and atomic spectrum updates."""

import math

from qgis.core import QgsFeature, QgsGeometry

from acalc.back_calculation import fit_spectra
from acalc.calculation_settings import default_settings
from acalc.layers import load_layer
from acalc.plugin import AcalcPlugin
from acalc.schema import definition, new_id
from acalc.spectra import spectrum_definition
from acalc.spectra_ui import SpectrumDialog
from tests.test_model import APP, FakeIface, ModelFixture

class BackCalculationTests(ModelFixture):
    def test_spectrum_editor_keeps_initial_guess_separate(self):
        dialog = SpectrumDialog(None, "sound_power")
        try:
            dialog.name.setText("Fan")
            self.assertTrue(dialog.bands.isRowHidden(1))
            dialog.back_calculation.setCurrentIndex(dialog.back_calculation.findData("adjustable"))
            self.assertFalse(dialog.bands.isRowHidden(1))
            for band in range(8):
                dialog.bands.item(0, band).setText("70")
                dialog.bands.item(1, band).setText("60")
            dialog.validate()
            self.assertEqual(dialog.value["values"], [70] * 8)
            self.assertEqual(dialog.value["initial_values"], [60] * 8)
            self.assertEqual(dialog.value["back_calculation"], "adjustable")
        finally:
            dialog.close()

    def test_two_spectra_fit_analytic_linear_energy_mixture(self):
        def measurement(first, second):
            level = 10 * math.log10(10 ** (first / 10) + 10 ** (second / 10))
            return [level] * 8
        relationships = {
            "spectra": [
                {"id": "a", "mode": "adjustable", "current": [60] * 8, "initial": [60] * 8},
                {"id": "b", "mode": "adjustable", "current": [60] * 8, "initial": [60] * 8},
            ],
            "sources": [{"id": "source-a", "spectrum_id": "a"}, {"id": "source-b", "spectrum_id": "b"}],
            "measurements": [
                {"observed": measurement(70, 55), "transfer_db": {"source-a": [0] * 8, "source-b": [-10] * 8}},
                {"observed": measurement(60, 65), "transfer_db": {"source-a": [-10] * 8, "source-b": [0] * 8}},
            ],
        }
        fit = fit_spectra(relationships)
        self.assertEqual(fit["proposals"], {"a": [70] * 8, "b": [65] * 8})
        self.assertLess(fit["fitted_rms_db"], 0.01)
        self.assertGreater(fit["initial_rms_db"], 1)

    def test_shared_adjustable_spectrum_and_locked_background(self):
        target = 10 * math.log10(1.5 * 10 ** 7 + 10 ** 5)
        relationships = {
            "spectra": [
                {"id": "adjust", "mode": "adjustable", "current": [65] * 8, "initial": [60] * 8},
                {"id": "fixed", "mode": "locked", "current": [60] * 8},
            ],
            "sources": [{"id": "a", "spectrum_id": "adjust"},
                        {"id": "b", "spectrum_id": "adjust"},
                        {"id": "c", "spectrum_id": "fixed"}],
            "measurements": [{"observed": [target] * 8,
                              "transfer_db": {"a": [0] * 8,
                                              "b": [10 * math.log10(0.5)] * 8,
                                              "c": [-10] * 8}}],
        }
        fit = fit_spectra(relationships)
        self.assertEqual(fit["proposals"], {"adjust": [70] * 8})
        self.assertLess(fit["fitted_rms_db"], 0.01)

    def test_finite_zero_db_fit_stays_audible_in_catalog(self):
        fit = fit_spectra({
            "spectra": [{"id": "s", "mode": "adjustable", "current": [-10] * 8, "initial": [-10] * 8}],
            "sources": [{"id": "source", "spectrum_id": "s"}],
            "measurements": [{"observed": [0] * 8, "transfer_db": {"source": [0] * 8}}],
        })
        self.assertEqual(fit["proposals"]["s"], [0.1] * 8)
        self.assertAlmostEqual(fit["fitted_rms_db"], 0.1)

    def make_model(self):
        source = self.store.add_layer("point_source", "Plant")
        measurement = self.store.add_layer("measurement", "Survey")
        variant = self.store.variants()[0]
        self.store.save_variant(variant["variant_id"], variant["name"],
                                definition([source["layer_id"], measurement["layer_id"]]))
        self.store.save_calculation(default_settings())
        spectrum_id = self.store.add_spectrum("Fan", spectrum_definition(
            "sound_power", [None] * 8, back_calculation="adjustable", initial_values=[75] * 8))
        for record, x in ((source, 500000), (measurement, 500100)):
            layer = load_layer(self.store, record)
            self.assertTrue(layer.startEditing())
            feature = QgsFeature(layer.fields())
            feature["object_id"] = new_id()
            feature["name"] = record["name"]
            feature["enabled"] = 1
            if record is source:
                feature["spectrum_id"] = spectrum_id
            else:
                for band in (63, 125, 250, 500, 1000, 2000, 4000, 8000):
                    feature[f"level_{band}_db"] = 55.0
            feature.setGeometry(QgsGeometry.fromWkt(f"POINT ZM ({x} 4400000 1500 1.5)"))
            self.assertTrue(layer.addFeature(feature))
            self.assertTrue(layer.commitChanges(), layer.commitErrors())
            del layer
        self.store.touch()
        return variant, spectrum_id

    def test_unknown_initial_guesses_have_an_actionable_preflight_error(self):
        from copy import deepcopy
        from acalc.api_model import prepare_relationships
        from acalc.schema import ModelError
        from tests.test_api_model import CAPABILITIES
        variant, spectrum_id = self.make_model()
        self.store.save_spectrum(spectrum_id, "Fan", spectrum_definition(
            "sound_power", [None] * 8, back_calculation="adjustable"))
        capabilities = deepcopy(CAPABILITIES)
        capabilities["features"].append("source_receiver_transfers")
        with self.assertRaisesRegex(ModelError, "Fan: enter initial sound power guesses for 63"):
            prepare_relationships(self.store, variant["variant_id"], capabilities)

    def test_back_calculation_requires_a_capable_api(self):
        self.make_model()
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            self.assertFalse(plugin.dock.calculation_page.back_calculate_button.isEnabled())
            with self.assertRaisesRegex(Exception, "API|capabilit"):
                plugin.open_back_calculation_dialog()
            self.assertFalse(plugin.engine_runner.active)
            self.assertIsNone(plugin.back_calculation_dialog)
        finally:
            plugin.unload()
            iface.window.close()
            APP.processEvents()
