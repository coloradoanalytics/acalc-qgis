# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Optional end-to-end fit against a rebuilt ISO engine.

Run with ACALC_ISO_BINARY=/absolute/path/to/iso-rust and the usual QGIS Python.
The engine generates observations from known powers, then the real Qt runner
retrieves transfer coefficients and recovers those powers from unknown values.
"""

from copy import deepcopy
import json
import os
import re
import select
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from osgeo import ogr

from acalc.back_calculation import fit_spectra
from acalc.schema import new_id
from acalc.engine_runner import EngineRunner
from acalc.spectra import OCTAVE_BANDS, spectrum_definition
from acalc.storage import ModelStore
from tests import test_back_calculation_api as fixtures
from tests import test_model as model_fixture
from tests.test_api_runner import wait_until


@unittest.skipUnless(os.environ.get("ACALC_ISO_BINARY"), "Set ACALC_ISO_BINARY to a rebuilt iso-rust executable")
class EngineBackCalculationTests(unittest.TestCase):
    def setUp(self):
        # Reuse scene construction without importing a TestCase into this module
        # or inheriting its synthetic-service tests into discovery a second time.
        self.scene = fixtures.BackCalculationApiTests()
        self.scene.setUp()
        self.addCleanup(self.scene.tearDown)
        self.process = subprocess.Popen(
            [os.environ["ACALC_ISO_BINARY"], "--host", "127.0.0.1", "--port", "0", "--threads", "2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_engine)
        deadline, startup = time.monotonic() + 15, []
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.process.stderr], [], [], 0.1)
            if readable:
                line = self.process.stderr.readline()
                startup.append(line)
                match = re.search(r"listening on (http://127\.0\.0\.1:\d+)", line)
                if match:
                    self.base_url = match.group(1)
                    break
            if self.process.poll() is not None:
                self.fail("ISO engine exited during startup: " + "".join(startup))
        else:
            self.fail("ISO engine did not announce its HTTP address: " + "".join(startup))
        self.capabilities = self.json_request("GET", "/v1/capabilities")
        self.assertIn("source_receiver_transfers", self.capabilities.get("features", []))
        self.scene.capabilities = self.capabilities

    def stop_engine(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stderr.close()

    def json_request(self, method, path, value=None):
        payload = None if value is None else json.dumps(value, allow_nan=False).encode()
        request = Request(urljoin(self.base_url, path), payload, {"Content-Type": "application/json"}, method=method)
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    def calculate(self, request):
        job = self.json_request("POST", "/v1/calculations", request)
        deadline = time.monotonic() + 30
        while job["state"] in ("queued", "running"):
            self.assertLess(time.monotonic(), deadline, "Real ISO calculation timed out")
            time.sleep(0.01)
            job = self.json_request("GET", job["links"]["status"])
        self.assertEqual(job["state"], "completed", job)
        return self.json_request("GET", job["links"]["results"])

    def test_engine_generated_measurements_recover_unknown_shared_power_and_apply(self):
        self.assert_recovers_shared_power()

    def test_measurement_forward_results_match_colocated_point_receiver(self):
        scene = self.scene
        regular = scene.point("point_receiver", 500100)
        unmeasured = scene.point("measurement", 500150)
        observed = scene.store.measurements(scene.variant_id)
        snapshot = fixtures.prepare_calculation(scene.store, scene.variant_id, self.capabilities)
        result = self.calculate(snapshot["request"])
        self.assertEqual(len(result["receivers"]), 3)
        backend = dict(fixtures.BACKEND, base_url=self.base_url,
                       implementation=self.capabilities["implementation"])
        fixtures.save_completed_calculation(scene.store, snapshot, result, backend, fixtures.JOB)
        reopened = ModelStore.open(scene.path)
        results = {row["object_id"]: row for row in reopened.receiver_results(scene.variant_id)}
        measured_result = results[scene.measurement["object_id"]]
        regular_result = results[regular["object_id"]]
        self.assertEqual(measured_result["octave_levels"], regular_result["octave_levels"])
        self.assertEqual(measured_result["broadband_a_db"], regular_result["broadband_a_db"])
        self.assertTrue(all(value is not None for value in measured_result["octave_levels"]))
        self.assertTrue(all(value is not None for value in results[unmeasured["object_id"]]["octave_levels"]))
        self.assertEqual(reopened.measurements(scene.variant_id), observed)

    def test_source_building_components_and_regular_building_roundtrip_to_engine(self):
        components = [dict(id=new_id(), name=name, face="roof", vertex_count=4,
                           spectrum_id=spectrum_id, frame="building_local", x_m=x, y_m=1,
                           type="point", mounting="surface", distance_m=0)
                      for name, x, spectrum_id in (("Adjustable roof fan", 1, self.scene.adjustable),
                                                   ("Locked roof fan", 2, self.scene.locked))]
        self.scene.feature("source_building",
            "POLYGON ZM ((500040 4400010 1500 5,500044 4400010 1500 5,"
            "500044 4400014 1500 5,500040 4400014 1500 5,500040 4400010 1500 5))",
            face_sources=json.dumps(components))
        self.scene.feature("building",
            "POLYGON ZM ((500050 4400020 1500 5,500054 4400020 1500 5,"
            "500054 4400024 1500 5,500050 4400024 1500 5,500050 4400020 1500 5))")
        request = self.scene.snapshot()["request"]
        self.assertEqual(len(request["buildings"]), 2)
        self.assertEqual(len(request["sources"]), 5)
        self.assertEqual(sum("host_building_id" in source for source in request["sources"]), 2)
        self.assert_recovers_shared_power()

    def assert_recovers_shared_power(self):
        scene = self.scene
        snapshot = scene.snapshot()
        request = deepcopy(snapshot["request"])
        request.pop("output")
        for source in request["sources"]:
            spectrum_id = snapshot["id_mapping"][source["id"]]["spectrum_id"]
            source["spectrum"] = [70 if spectrum_id == scene.adjustable else 60] * 8
        forward = self.calculate(request)
        self.assertEqual(len(forward["receivers"]), 1)
        observed = forward["receivers"][0]["octave_db"]
        self.assertTrue(all(value is not None for value in observed))

        dataset = ogr.Open(str(scene.path), 1)
        layer = dataset.GetLayerByName(scene.measurement["table_name"])
        feature = next(iter(layer))
        for band, level in zip(OCTAVE_BANDS, observed):
            feature.SetField(f"level_{band}_db", level)
        self.assertEqual(layer.SetFeature(feature), 0)
        feature, layer, dataset = None, None, None
        scene.store.save_spectrum(scene.adjustable, "Shared adjustable", spectrum_definition(
            "sound_power", [None] * 8, back_calculation="adjustable", initial_values=[60] * 8))
        spectra_before = scene.store.spectra()

        engine = SimpleNamespace(id="real-iso-test", connection=dict(
            id="real-iso-test", name="Real ISO integration", base_url=self.base_url),
            capabilities=deepcopy(self.capabilities), backend_settings={})
        runner = EngineRunner()
        runner.POLL_INTERVAL_MS = 10
        successes, failures, stops = [], [], []
        runner.succeeded.connect(lambda context, value: successes.append((context, value)))
        runner.failed.connect(lambda context, message: failures.append(message))
        runner.stopped.connect(stops.append)
        try:
            with patch("acalc.calculation_engines.calculation_engine", return_value=engine):
                runner.start_relationships(scene.store, scene.variant_id, engine_id=engine.id)
                wait_until(lambda: not runner.active, timeout=30)
            self.assertEqual(failures, [])
            self.assertEqual(stops, [])
            self.assertEqual(len(successes), 1)
            context, relationships = successes[0]
            self.assertTrue(context["relationships"])
            self.assertEqual(len(relationships["sources"]), len(request["sources"]))
            fit = fit_spectra(relationships)
            self.assertEqual(fit["proposals"], {scene.adjustable: [70] * 8})
            self.assertLess(fit["fitted_rms_db"], 0.001)
            self.assertEqual(scene.store.spectra(), spectra_before)
            scene.store.apply_back_calculation(relationships, fit["proposals"])
        finally:
            runner.shutdown()
            runner.deleteLater()
            model_fixture.APP.processEvents()

        reopened = ModelStore.open(scene.path)
        spectra = {row["spectrum_id"]: row["spectrum"] for row in reopened.spectra()}
        self.assertEqual(spectra[scene.adjustable]["values"], [70] * 8)
        self.assertEqual(spectra[scene.adjustable]["initial_values"], [60] * 8)
        self.assertEqual(spectra[scene.locked], next(row["spectrum"] for row in spectra_before
                                                  if row["spectrum_id"] == scene.locked))
        self.assertEqual(reopened.calculation_runs(), [])
        self.assertEqual(reopened.receiver_results(scene.variant_id), [])
