# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Back-calculation over HTTP with real GeoPackage inputs and retained results.

The service fixture supplies analytic transfer coefficients, not an acoustic
solver. Engine physics and extended-source integration have separate tests.
"""

from contextlib import contextmanager
from copy import deepcopy
import math
import json
from types import SimpleNamespace
from unittest.mock import patch

from osgeo import ogr

from acalc.api_model import prepare_calculation, prepare_relationships, relationships_from_result, save_completed_calculation
from acalc.back_calculation import fit_spectra
from acalc.engine_runner import EngineRunner
from acalc.schema import ModelError, definition, new_id
from acalc.spectra import OCTAVE_BANDS, spectrum_definition
from acalc.storage import ModelStore
from tests.test_api_model import BACKEND, CAPABILITIES, JOB, fields
from tests.test_api_runner import FakeCalculationAPI, wait_until
from tests.test_model import APP, ModelFixture


TRANSFER_METADATA = dict(reference_db=100, power_basis="per_source_input", pruning="disabled")


class TransferCalculationAPI(FakeCalculationAPI):
    def __init__(self, transfers):
        super().__init__()
        self.capabilities["features"] = [*self.capabilities["features"], "source_receiver_transfers"]
        self.transfers = transfers

    def result(self, request, identity="job"):
        value = super().result(request, identity)
        value["source_power_transfers"] = deepcopy(TRANSFER_METADATA)
        for receiver in value["receivers"]:
            levels = []
            for source in receiver["sources"]:
                transfer = self.transfers[source["source_id"].rsplit("/", 1)[1]]
                level = 100 + transfer
                source.update(fields(level))
                levels.append(level)
            receiver.update(fields(10 * math.log10(sum(10 ** (level / 10) for level in levels))))
        return value


class BackCalculationApiTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.variant_id = self.store.variants()[0]["variant_id"]
        self.layer_ids = []
        self.capabilities = deepcopy(CAPABILITIES)
        self.capabilities["features"].append("source_receiver_transfers")
        self.adjustable = self.store.add_spectrum("Shared adjustable", spectrum_definition(
            "sound_power", [65] * 8, back_calculation="adjustable", initial_values=[60] * 8))
        self.locked = self.store.add_spectrum("Locked background", spectrum_definition("sound_power", [60] * 8))
        self.sources = [self.point("point_source", x, spectrum_id=spectrum_id)
                        for x, spectrum_id in ((500000, self.adjustable), (500010, self.adjustable),
                                               (500020, self.locked))]
        self.transfers = {self.sources[0]["object_id"]: -20,
                          self.sources[1]["object_id"]: -20 + 10 * math.log10(0.5),
                          self.sources[2]["object_id"]: -30}
        target = 10 * math.log10(1.5 * 10 ** 5 + 10 ** 3)
        self.measurement = self.point("measurement", 500100,
                                      **{f"level_{band}_db": target for band in OCTAVE_BANDS})

    def feature(self, kind, wkt, **values):
        object_id = new_id()
        def populate(layer):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("object_id", object_id)
            feature.SetField("name", kind + " fixture")
            feature.SetField("enabled", 1)
            feature.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
            for key, value in values.items():
                if value is not None:
                    feature.SetField(key, value)
            self.assertEqual(layer.CreateFeature(feature), 0)
        record = self.store.add_layer(kind, kind + " layer", populate=populate)
        self.layer_ids.append(record["layer_id"])
        self.store.save_variant(self.variant_id, "Fit variant", definition(self.layer_ids))
        return dict(record, object_id=object_id)

    def point(self, kind, x, **values):
        return self.feature(kind, f"POINT ZM ({x} 4400000 1500 1.5)", **values)

    def snapshot(self):
        return prepare_relationships(self.store, self.variant_id, self.capabilities)

    def result(self, snapshot):
        api = TransferCalculationAPI(self.transfers)
        try:
            return api.result(snapshot["request"])
        finally:
            api.server.server_close()

    def relationships(self):
        snapshot = self.snapshot()
        return relationships_from_result(snapshot, self.result(snapshot))

    def save_forward_result(self):
        self.point("point_receiver", 500200)
        snapshot = prepare_calculation(self.store, self.variant_id, self.capabilities)
        save_completed_calculation(self.store, snapshot, FakeCalculationAPI.result(snapshot["request"]), BACKEND, JOB)
        return self.store.calculation_runs(), self.store.receiver_results(self.variant_id)

    @contextmanager
    def runner(self):
        with TransferCalculationAPI(self.transfers) as api:
            engine = SimpleNamespace(id="test-transfer-api", connection=dict(
                id="test-transfer-api", name="Transfer test API", base_url=api.url),
                capabilities=deepcopy(api.capabilities), backend_settings={})
            runner = EngineRunner()
            runner.POLL_INTERVAL_MS = 10
            success, failures, stopped = [], [], []
            runner.succeeded.connect(lambda context, value: success.append((context, value)))
            runner.failed.connect(lambda context, message: failures.append(message))
            runner.stopped.connect(stopped.append)
            try:
                with patch("acalc.calculation_engines.calculation_engine", return_value=engine):
                    yield api, runner, success, failures, stopped
            finally:
                runner.shutdown()
                runner.deleteLater()
                APP.processEvents()

    def test_measurement_only_variant_uses_finite_reference_and_disables_pruning(self):
        self.store.save_spectrum(self.adjustable, "Shared adjustable", spectrum_definition(
            "sound_power", [None, 0, -5, 65, None, 0, 65, 65],
            back_calculation="adjustable", initial_values=[60] * 8))
        settings = self.store.calculation()["settings"]
        settings["pruning_strategy"] = "dba"
        self.store.save_calculation(settings)
        spectra_before = self.store.spectra()
        snapshot = self.snapshot()
        request = snapshot["request"]
        self.assertEqual(len(request["receivers"]), 1)
        self.assertTrue(request["receivers"][0]["id"].endswith("/" + self.measurement["object_id"]))
        self.assertEqual(request["area_receivers"], [])
        self.assertEqual(request["output"], {"source_power_transfers": True})
        self.assertIsNone(request["settings"]["minimum_contribution_db"])
        self.assertEqual(request["settings"]["area_absolute_tolerance"], 0)
        self.assertNotIn("pruning_strategy", request["settings"])
        self.assertTrue(all(source["spectrum"] == [100] * 8 for source in request["sources"]))
        self.assertEqual(self.store.spectra(), spectra_before)
        self.assertEqual(self.store.calculation()["settings"]["pruning_strategy"], "dba")
        relationships = relationships_from_result(snapshot, self.result(snapshot))
        fit = fit_spectra(relationships)
        self.assertEqual(fit["proposals"], {self.adjustable: [70] * 8})

    def test_only_octave_measurements_are_fitted_and_legacy_enabled_is_ignored(self):
        self.point("point_receiver", 500200)
        self.point("measurement", 500300, enabled=0, **{f"level_{band}_db": 25 for band in OCTAVE_BANDS})
        self.point("measurement", 500400)  # No octave observations.
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot["request"]["receivers"]), 2)
        relationships = relationships_from_result(snapshot, self.result(snapshot))
        self.assertEqual(len(relationships["measurements"]), 2)
        self.assertEqual(fit_spectra(relationships)["observations"], 16)

    def test_source_input_power_basis_survives_reference_substitution(self):
        spectrum_id = self.store.add_spectrum("Pipeline", spectrum_definition(
            "sound_power", [60] * 8, power_basis="per_metre"))
        record = self.feature("line_source", "LINESTRING ZM (500000 4400010 1500 1.5,500010 4400010 1500 1.5)",
                              spectrum_id=spectrum_id)
        snapshot = self.snapshot()
        source = next(source for source in snapshot["request"]["sources"]
                      if source["id"].endswith("/" + record["object_id"]))
        self.assertEqual(source["power_basis"], "power_per_m")
        self.assertEqual(source["spectrum"], [100] * 8)

    def test_source_building_components_remain_independent_back_calculation_inputs(self):
        components = [dict(id=new_id(), name=name, face="roof", vertex_count=4,
                           spectrum_id=spectrum_id, frame="building_local", x_m=x, y_m=1,
                           type="point", mounting="surface", distance_m=0)
                      for name, x, spectrum_id in (("Adjustable fan", 1, self.adjustable),
                                                   ("Locked fan", 2, self.locked))]
        building = self.feature("source_building",
            "POLYGON ZM ((500000 4400010 1500 5,500004 4400010 1500 5,"
            "500004 4400014 1500 5,500000 4400014 1500 5,500000 4400010 1500 5))",
            face_sources=json.dumps(components))
        for component, transfer in zip(components, (-25, -35)):
            self.transfers[component["id"]] = transfer
        snapshot = self.snapshot()
        request = snapshot["request"]
        self.assertEqual(len(request["buildings"]), 1)
        self.assertEqual(len(request["sources"]), 5)
        relationships = relationships_from_result(snapshot, self.result(snapshot))
        self.assertEqual(len(relationships["sources"]), 5)
        for component in components:
            source = next(source for source in relationships["sources"]
                          if source["id"].endswith("/" + component["id"]))
            self.assertEqual(source["spectrum_id"], component["spectrum_id"])
            self.assertEqual(snapshot["id_mapping"][source["id"]]["object_id"], building["object_id"])
            self.assertEqual(relationships["measurements"][0]["transfer_db"][source["id"]],
                             [self.transfers[component["id"]]] * 8)
        self.assertEqual(set(fit_spectra(relationships)["proposals"]), {self.adjustable})

    def test_fit_applies_only_adjustable_current_values_and_retains_initial_guesses(self):
        relationships = self.relationships()
        before = {row["spectrum_id"]: row for row in self.store.spectra()}
        fit = fit_spectra(relationships)
        self.assertEqual(fit["proposals"], {self.adjustable: [70] * 8})
        self.assertLess(fit["fitted_rms_db"], 0.001)
        self.assertEqual(len(relationships["sources"]), 3)
        self.assertEqual(self.store.spectra(), list(before.values()))
        self.store.apply_back_calculation(relationships, fit["proposals"])
        reopened = ModelStore.open(self.path)
        after = {row["spectrum_id"]: row for row in reopened.spectra()}
        self.assertEqual(after[self.adjustable]["spectrum"]["values"], [70] * 8)
        self.assertEqual(after[self.adjustable]["spectrum"]["initial_values"], [60] * 8)
        self.assertEqual(after[self.locked], before[self.locked])
        self.assertEqual(reopened.calculation_runs(), [])

    def test_stale_raw_measurement_or_source_edits_reject_apply_atomically(self):
        for record, field, value in ((self.measurement, "level_63_db", 99),
                                     (self.sources[0], "name", "External edit")):
            with self.subTest(field=field):
                relationships = self.relationships()
                before = self.store.spectra()
                modified_at = self.store.metadata()["modified_at"]
                with self.store.connection() as db:
                    # Simulate an external writer bypassing the project touch trigger.
                    db.execute(f'DROP TRIGGER IF EXISTS "acalc_touch_{record["table_name"]}_update"')
                dataset = ogr.Open(str(self.path), 1)
                layer = dataset.GetLayerByName(record["table_name"])
                feature = next(iter(layer))
                feature.SetField(field, value)
                self.assertEqual(layer.SetFeature(feature), 0)
                feature, layer, dataset = None, None, None
                self.assertEqual(self.store.metadata()["modified_at"], modified_at)
                with self.assertRaisesRegex(ModelError, "changed|again|revision"):
                    self.store.apply_back_calculation(relationships, {self.adjustable: [70] * 8})
                self.assertEqual(self.store.spectra(), before)

    def test_missing_capability_or_unknown_locked_power_fails_before_submission(self):
        capabilities = deepcopy(self.capabilities)
        capabilities["features"].remove("source_receiver_transfers")
        with self.assertRaises(ModelError):
            prepare_relationships(self.store, self.variant_id, capabilities)
        self.store.save_spectrum(self.locked, "Locked background", spectrum_definition("sound_power", [None] * 8))
        with self.assertRaisesRegex(ModelError, "[Ll]ocked"):
            self.snapshot()

    def test_rejects_unreliable_or_mismatched_transfer_results(self):
        snapshot = self.snapshot()
        valid = self.result(snapshot)
        cases = {}
        value = deepcopy(valid); value.pop("source_power_transfers"); cases["missing reference marker"] = value
        value = deepcopy(valid); value["source_power_transfers"]["reference_db"] = 0; cases["wrong reference"] = value
        value = deepcopy(valid); value["source_power_transfers"]["power_basis"] = "total"; cases["wrong power basis"] = value
        value = deepcopy(valid); value["source_power_transfers"]["pruning"] = "enabled"; cases["pruned transfers"] = value
        value = deepcopy(valid); value["receivers"] = []; cases["missing measurement"] = value
        value = deepcopy(valid); value["receivers"].append(deepcopy(value["receivers"][0])); cases["duplicate measurement"] = value
        value = deepcopy(valid); value["receivers"][0]["sources"].pop(); cases["missing source"] = value
        value = deepcopy(valid); value["receivers"][0]["sources"][0]["source_id"] = "unknown"; cases["unknown source"] = value
        value = deepcopy(valid); value["receivers"][0]["sources"].append(deepcopy(value["receivers"][0]["sources"][0])); cases["duplicate source"] = value
        value = deepcopy(valid); value["receivers"][0]["sources"][0]["omitted_energy"][0] = 1; cases["omitted source energy"] = value
        value = deepcopy(valid); value["receivers"][0]["omitted_energy"][0] = 1; cases["omitted receiver energy"] = value
        value = deepcopy(valid); value["settings"]["minimum_contribution_db"] = 10; cases["changed settings"] = value
        value = deepcopy(valid); value["receivers"][0].update(complete=False, warnings=[dict(
            kind="failed_pathway", message="Synthetic pathway failure", bounded=False)]); cases["incomplete receiver"] = value
        value = deepcopy(valid); value["receivers"][0].pop("height_m"); cases["missing receiver height"] = value
        value = deepcopy(valid); value["receivers"][0]["ground_elevation_m"] -= 1; cases["inconsistent ground"] = value
        value = deepcopy(valid); value["receivers"][0]["ground_elevation_m"] -= 1; value["receivers"][0]["height_m"] += 1; cases["changed height at same position"] = value
        for label, value in cases.items():
            with self.subTest(label=label), self.assertRaises(ModelError):
                relationships_from_result(snapshot, value)

    def test_exact_zero_source_path_is_not_converted_to_a_finite_transfer(self):
        snapshot = self.snapshot()
        value = self.result(snapshot)
        source = value["receivers"][0]["sources"][0]
        source.update(fields(None))
        relationships = relationships_from_result(snapshot, value)
        self.assertEqual(relationships["measurements"][0]["transfer_db"][source["source_id"]], [None] * 8)

    def test_http_fit_returns_relationships_without_replacing_forward_results(self):
        previous_runs, previous_results = self.save_forward_result()
        spectra_before = self.store.spectra()
        with self.runner() as (api, runner, success, failures, stopped):
            runner.start_relationships(self.store, self.variant_id)
            wait_until(lambda: not runner.active)
            self.assertEqual(failures, [])
            self.assertEqual(stopped, [])
            self.assertEqual(len(success), 1)
            context, relationships = success[0]
            self.assertTrue(context["relationships"])
            self.assertEqual(fit_spectra(relationships)["proposals"], {self.adjustable: [70] * 8})
            posts = [body for method, path, body in api.requests if method == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertTrue(posts[0]["output"]["source_power_transfers"])
        self.assertEqual(self.store.calculation_runs(), previous_runs)
        self.assertEqual(self.store.receiver_results(self.variant_id), previous_results)
        self.assertEqual(self.store.spectra(), spectra_before)

    def test_http_cancel_preserves_forward_results_and_spectra(self):
        previous_runs, previous_results = self.save_forward_result()
        spectra_before = self.store.spectra()
        with self.runner() as (api, runner, success, failures, stopped):
            api.hold = True
            runner.start_relationships(self.store, self.variant_id)
            wait_until(lambda: runner._job is not None)
            runner.cancel()
            wait_until(lambda: not runner.active)
            self.assertEqual(success, [])
            self.assertEqual(failures, [])
            self.assertEqual(len(stopped), 1)
            self.assertEqual(sum(method == "DELETE" for method, _, _ in api.requests), 1)
        self.assertEqual(self.store.calculation_runs(), previous_runs)
        self.assertEqual(self.store.receiver_results(self.variant_id), previous_results)
        self.assertEqual(self.store.spectra(), spectra_before)

    def test_http_failed_job_preserves_forward_results_and_spectra(self):
        previous_runs, previous_results = self.save_forward_result()
        spectra_before = self.store.spectra()
        with self.runner() as (api, runner, success, failures, stopped):
            def fail_poll(method, path, body):
                if method == "GET" and path.startswith("/service-jobs/"):
                    return 200, api.snapshot(path.split("/")[2], "failed", sequence=4)
            api.on_request = fail_poll
            runner.start_relationships(self.store, self.variant_id)
            wait_until(lambda: not runner.active)
            self.assertEqual(success, [])
            self.assertEqual(stopped, [])
            self.assertEqual(len(failures), 1)
            self.assertIn("Fixture failed", failures[0])
            self.assertEqual(sum(method == "POST" for method, _, _ in api.requests), 1)
        self.assertEqual(self.store.calculation_runs(), previous_runs)
        self.assertEqual(self.store.receiver_results(self.variant_id), previous_results)
        self.assertEqual(self.store.spectra(), spectra_before)
