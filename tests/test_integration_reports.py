# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Opt-in integration evidence through metadata, normalization, and live discovery."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from acalc.api_model import prepare_calculation, prepare_relationships
from acalc.engine_runner import EngineRunner
from acalc.schema import ModelError
from acalc.spectra import OCTAVE_BANDS, spectrum_definition
from acalc.storage import ModelStore
from tests import test_api_model as fixtures
from tests.test_api_runner import FakeCalculationAPI, wait_until
from tests.test_model import APP


class IntegrationReportsTests(unittest.TestCase):
    def setUp(self):
        self.scene = fixtures.ApiModelTests()
        self.scene.setUp()
        self.addCleanup(self.scene.tearDown)
        self.scene.scene()
        self.store = self.scene.store
        self.variant_id = self.scene.variant_id
        self.capabilities = deepcopy(fixtures.CAPABILITIES)
        self.capabilities["features"].append("integration_reports")

    def metadata(self, extra):
        project = self.store.metadata()
        self.store.update_metadata(project["name"], project["author"], project["description"],
                                   project["vertical_datum"], extra)

    def prepare(self, capabilities=None):
        return prepare_calculation(self.store, self.variant_id,
                                   self.capabilities if capabilities is None else capabilities)

    def test_absent_flag_stays_absent_on_creation_reopen_and_preparation(self):
        self.assertNotIn("keep_integration_reports", json.loads(self.store.metadata()["metadata_json"]))
        self.store = ModelStore.open(self.store.path)
        before = self.store.metadata()
        self.assertNotIn("output", self.prepare()["request"])
        self.assertNotIn("output", self.prepare(fixtures.CAPABILITIES)["request"])
        self.assertEqual(self.store.metadata(), before)

    def test_false_and_non_boolean_values_never_request_full_reports(self):
        for value in (False, None, 0, 1, 1.0, "true", "false", "full", [], [True], {}, {"full": True}):
            with self.subTest(value=value):
                self.metadata({"keep_integration_reports": value})
                before = self.store.metadata()
                for capabilities in (self.capabilities, fixtures.CAPABILITIES):
                    self.assertNotIn("output", self.prepare(capabilities)["request"])
                self.assertEqual(self.store.metadata(), before)

    def test_true_adds_only_full_report_option_without_copying_other_metadata(self):
        self.metadata({"keep_integration_reports": True, "client": "Example",
                       "output": {"unknown": True, "integration_reports": "invalid"}})
        before = self.store.metadata()
        self.assertEqual(self.prepare()["request"]["output"], {"integration_reports": "full"})
        self.assertEqual(self.store.metadata(), before)

    def test_true_requires_capability_before_preparing_scene(self):
        self.metadata({"keep_integration_reports": True})
        with patch("acalc.api_scene.prepare_scene") as prepare_scene:
            with self.assertRaisesRegex(ModelError, "does not advertise integration_reports"):
                self.prepare(fixtures.CAPABILITIES)
            prepare_scene.assert_not_called()

    def test_area_output_option_is_preserved(self):
        self.scene.feature("area_receiver", [(20, 0, 0, 0), (30, 0, 0, 0),
                                             (30, 10, 0, 0), (20, 0, 0, 0)],
                           height_m=4, spacing_m=10)
        self.capabilities["features"].append("area_receiver_totals")
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.metadata({"keep_integration_reports": enabled})
                expected = {"area_source_contributions": False}
                if enabled:
                    expected["integration_reports"] = "full"
                self.assertEqual(self.prepare()["request"]["output"], expected)

    def test_back_calculation_output_option_is_preserved(self):
        self.store.save_spectrum(self.scene.spectrum_id, "Power", spectrum_definition(
            "sound_power", [90] * 8, back_calculation="adjustable", initial_values=[90] * 8))
        self.scene.feature("measurement", [(100, 10, 12, 1.5)],
                           **{f"level_{band}_db": 40 for band in OCTAVE_BANDS})
        self.capabilities["features"].append("source_receiver_transfers")
        self.metadata({"keep_integration_reports": True})
        snapshot = prepare_relationships(self.store, self.variant_id, self.capabilities)
        self.assertEqual(snapshot["request"]["output"],
                         {"source_power_transfers": True, "integration_reports": "full"})

    def test_live_capabilities_control_submission_and_saved_request(self):
        self.metadata({"keep_integration_reports": True})
        before = self.store.metadata()
        # Both stale-cache directions: a newly supported engine may run, but a
        # server that has lost support must refuse before sending any calculation.
        for supported in (True, False):
            with self.subTest(live_support=supported), FakeCalculationAPI() as api:
                engine = SimpleNamespace(id="test-reports-api", connection=dict(
                    id="test-reports-api", name="Reports API", base_url=api.url),
                    capabilities=deepcopy(api.capabilities), backend_settings={})
                target = api.capabilities if supported else engine.capabilities
                target["features"] = [*target["features"], "integration_reports"]
                runner = EngineRunner()
                runner.POLL_INTERVAL_MS = 10
                successes, failures = [], []
                runner.succeeded.connect(lambda context, value: successes.append(value))
                runner.failed.connect(lambda context, message: failures.append(message))
                try:
                    with patch("acalc.calculation_engines.calculation_engine", return_value=engine):
                        runner.start(self.store, self.variant_id, engine_id=engine.id)
                        wait_until(lambda: not runner.active)
                    self.assertEqual([request[:2] for request in api.requests[:2]],
                                     [("GET", "/healthz"), ("GET", "/v1/capabilities")])
                    submitted = [body for method, _, body in api.requests if method == "POST"]
                    if supported:
                        self.assertEqual(failures, [])
                        self.assertEqual(len(successes), 1)
                        self.assertEqual(len(submitted), 1)
                        self.assertEqual(submitted[0]["output"], {"integration_reports": "full"})
                        saved = self.store.latest_calculation_run(self.variant_id)
                        self.assertEqual(saved["metadata"]["api_snapshot"]["request"], submitted[0])
                        saved_run_id = saved["run_id"]
                    else:
                        self.assertEqual(successes, [])
                        self.assertEqual(submitted, [])
                        self.assertEqual(len(failures), 1)
                        self.assertIn("keep_integration_reports=true", failures[0])
                        self.assertIn("does not advertise integration_reports", failures[0])
                        self.assertEqual(self.store.latest_calculation_run(self.variant_id)["run_id"], saved_run_id)
                    self.assertEqual(self.store.metadata(), before)
                finally:
                    runner.shutdown()
                    runner.deleteLater()
                    APP.processEvents()


if __name__ == "__main__":
    unittest.main()
