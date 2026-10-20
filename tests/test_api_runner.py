# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""HTTP transport tests through a real local server and the QGIS Qt event loop.

The server returns synthetic levels to test transport/persistence boundaries;
these fixtures are not acoustic reference cases.
"""

from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qgis.PyQt.QtCore import QTimer

from acalc.api_client import ApiClient
from acalc.engine_runner import EngineRunner
from acalc.schema import ModelError
from tests.test_model import APP


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for the calculation API")


class FakeCalculationAPI:
    """Context-managed server shared by API/UI tests; override on_request.

    on_request(method, path, body) can return (HTTP status, JSON response),
    or None for the standard handler. ``requests`` retains those three fields.
    Raw bytes as a response are sent verbatim. No acoustic solver runs here.
    """

    def __init__(self):
        self.capabilities = {
            "api_version": "1.0", "implementation": {"name": "test_http", "version": "0.1.0"},
            "method": "ISO 9613-2:2024", "atmospheric_absorption": "ISO 9613-1:1993",
            "verified_conformity": False, "quality_guidance": ["Synthetic transport fixture only"],
            "frequencies_hz": [63, 125, 250, 500, 1000, 2000, 4000, 8000],
            "source_kinds": ["point", "area"], "receiver_kinds": ["point", "area"],
            "max_reflect_order": 3, "profile_limits": {},
            "transport": {"max_request_bytes": 33554432, "event_interval_ms": 500},
            "limitations": ["Synthetic levels; no acoustic verification"],
        }
        from tests.test_api_model import CAPABILITIES
        self.capabilities.update({key: CAPABILITIES[key] for key in ("features", "component_profiles", "ground_region_policies")})
        self.requests = []
        self.request_headers = []
        self.response_headers = {}
        self.client_key = None
        self.jobs = {}
        self.on_request = None
        self.hold = False
        self.post_delay = 0
        self.poll_failures = 0
        self.content_type = "application/json"
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(body) if body else None
                api.requests.append((self.command, self.path, body))
                api.request_headers.append(dict(self.headers))
                if (api.client_key is not None and self.path != "/healthz"
                        and self.headers.get("Authorization") != "Bearer " + api.client_key):
                    response = (401, {"error": {"code": "unauthorized", "message": "Missing or incorrect key"}})
                else:
                    response = api.on_request(self.command, self.path, body) if api.on_request else None
                if response is None:
                    response = api.default_response(self.command, self.path, body)
                status, value = response
                if status is None:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                payload = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "image/tiff" if "/artifacts/" in self.path and status == 200
                                 else api.content_type)
                self.send_header("Content-Length", str(len(payload)))
                for name, value in api.response_headers.items():
                    self.send_header(name, value)
                if status == 202 and isinstance(value, dict):
                    self.send_header("Location", value.get("links", {}).get("status", "/"))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_DELETE = respond

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def snapshot(self, identity, state="running", completed=0, sequence=1):
        job = {"id": identity, "state": state,
               "progress": {"total_task_points": 2, "completed_task_points": completed,
                            "percent": completed * 50, "phase": "propagation" if state == "running" else state},
               "sequence": sequence, "created_at": "2026-10-08T18:00:00Z",
               "updated_at": "2026-10-08T18:00:01Z",
               "links": {"status": "/service-jobs/" + identity,
                         "results": "/service-jobs/" + identity + "/output"}}
        if state in ("failed", "cancelled"):
            job["error"] = {"code": state, "message": "Fixture " + state}
        if state in ("failed", "cancelled", "completed"):
            job["finished_at"] = "2026-10-08T18:00:01Z"
        return job

    def default_response(self, method, path, body):
        if method == "GET" and path == "/healthz":
            return 200, {"status": "ok", "version": "0.1.0", "method": self.capabilities["method"],
                         "role": "calculation", "key_required": self.client_key is not None,
                         "atmosphere": self.capabilities["atmospheric_absorption"]}
        if method == "GET" and path == "/v1/capabilities":
            return 200, self.capabilities
        if method == "POST" and path == "/v1/calculations":
            identity = "opaque%20job-" + str(len(self.jobs) + 1)
            self.jobs[identity] = {"request": body, "polls": 0}
            if self.post_delay:
                time.sleep(self.post_delay)
            return 202, self.snapshot(identity, "queued")
        identity = path.split("/")[2] if path.startswith("/service-jobs/") else None
        if identity not in self.jobs:
            return 404, {"error": {"code": "not_found", "message": "Unknown test job"}}
        if method == "DELETE":
            return 200, self.snapshot(identity, "cancelled", sequence=10)
        job = self.jobs[identity]
        if "/artifacts/" in path:
            return 200, self.raster_bytes(path.rsplit("/", 1)[1])
        if path.endswith("/output"):
            return 200, self.result(job["request"], identity)
        if self.poll_failures:
            self.poll_failures -= 1
            return 503, {"error": {"code": "shutting_down", "message": "Temporary test outage"}}
        job["polls"] += 1
        if self.hold or job["polls"] == 1:
            return 200, self.snapshot(identity, completed=0.5, sequence=2)
        return 200, self.snapshot(identity, "completed", completed=2, sequence=3)

    @staticmethod
    def raster_bytes(name):
        return ("synthetic raster " + name).encode()

    @classmethod
    def area_summaries(cls, request, identity):
        """Raster links shaped like the API grid contract; bytes are not a TIFF."""
        from acalc.api_model import AREA_RASTER_BANDS, AREA_RASTER_NODATA, _grid_axis_count
        summaries = []
        for index, area in enumerate(request.get("area_receivers", [])):
            xs, ys = [p[0] for p in area["boundary"]], [p[1] for p in area["boundary"]]
            spacing = area["spacing_m"]
            columns, rows = _grid_axis_count(min(xs), max(xs), spacing), _grid_axis_count(min(ys), max(ys), spacing)
            payload = cls.raster_bytes(f"area-{index}.tif")
            summaries.append({
                "area_receiver_id": area["id"], "receiver_count": columns * rows,
                "spacing_m": spacing, "height_m": area["height_m"],
                "grid": {"origin": [min(xs), min(ys)], "columns": columns, "rows": rows},
                "raster": {"artifact": f"area-{index}.tif", "media_type": "image/tiff", "format": "GeoTIFF",
                           "href": f"/service-jobs/{identity}/artifacts/area-{index}.tif", "bytes": len(payload),
                           "sha256": hashlib.sha256(payload).hexdigest(), "columns": columns, "rows": rows,
                           "data_type": "Float32", "bands": AREA_RASTER_BANDS, "nodata": AREA_RASTER_NODATA,
                           "geotransform": [min(xs) - spacing / 2, spacing, 0, min(ys) + (rows - 0.5) * spacing, 0, -spacing]},
                "sample_warnings": {"samples_with_warnings": 0, "incomplete_samples": 0, "bounded_samples": 0,
                                    "failed_pathways": 0, "warning_counts": {}, "examples": []}})
        return summaries

    @classmethod
    def result(cls, request, identity="job"):
        levels = {"octave_db": [20] * 8, "band_states": ["calculated"] * 8,
                  "a_weighted_db": 27, "state": "calculated", "omitted_energy": [0] * 8,
                  "omission_increase_db": [0] * 8}
        return {"method": "ISO 9613-2:2024 / ISO 9613-1:1993", "profile": "Synthetic test levels",
                "atmosphere": request.get("atmosphere", {}), "ground": request.get("ground", {}),
                "settings": request.get("settings", {}), "limitations": ["Synthetic transport fixture"],
                "operations": 1, "area_receivers": cls.area_summaries(request, identity),
                "receivers": [dict(deepcopy(levels), receiver_id=receiver["id"], position=receiver.get("position") or receiver["ground_ref"]["position"][:2] + [receiver["ground_ref"]["position"][2] + receiver["height_m"]],
                                   ground_elevation_m=receiver.get("ground_ref", {}).get("position", [0, 0, 0])[2],
                                   height_m=receiver.get("height_m", 0),
                                   sources=[dict(deepcopy(levels), source_id=source["id"])
                                            for source in request["sources"]])
                              for receiver in request.get("receivers", [])]}


class ApiRunnerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeCalculationAPI().__enter__()
        self.engine = SimpleNamespace(id="test-api-engine", connection={"id": "test-api", "name": "Lab API", "base_url": self.api.url},
                                      capabilities=deepcopy(self.api.capabilities), backend_settings={})
        self.directory = tempfile.TemporaryDirectory()
        self.store = SimpleNamespace(path=self.directory.name + "/model.gpkg",
                                     variants=lambda: [{"variant_id": "one", "name": "First"}, {"variant_id": "two", "name": "Second"}],
                                     calculation=lambda: {"engine": self.engine.id})
        self.request = {"sources": [{"id": "s", "kind": "point", "position": [0, 0, 1.5], "spectrum": [90] * 8}],
                        "receivers": [{"id": "r", "position": [100, 0, 1.5]}]}
        self.snapshot = {"request": self.request}
        self.engine_patch = patch("acalc.calculation_engines.calculation_engine", return_value=self.engine)
        self.prepare_patch = patch("acalc.api_model.prepare_calculation", return_value=self.snapshot)
        self.save_patch = patch("acalc.api_model.save_completed_calculation", return_value={"run_id": "saved"})
        self.engine_patch.start()
        self.prepare = self.prepare_patch.start()
        self.save = self.save_patch.start()
        self.runner = EngineRunner()
        self.runner.POLL_INTERVAL_MS = 15
        self.success, self.failures, self.stops, self.progress, self.busy = [], [], [], [], []
        self.runner.succeeded.connect(lambda context, value: self.success.append((context, value)))
        self.runner.failed.connect(lambda context, value: self.failures.append(value))
        self.runner.stopped.connect(self.stops.append)
        self.runner.progressChanged.connect(lambda context, value: self.progress.append(value))
        self.runner.busyChanged.connect(self.busy.append)

    def tearDown(self):
        self.runner.shutdown()
        self.runner.deleteLater()
        APP.processEvents()
        self.engine_patch.stop()
        self.prepare_patch.stop()
        self.save_patch.stop()
        self.api.__exit__()
        self.directory.cleanup()

    def run_to_end(self, **kwargs):
        self.runner.start(self.store, "one", **kwargs)
        wait_until(lambda: not self.runner.active)

    def methods(self, method):
        return [entry for entry in self.api.requests if entry[0] == method]

    def test_selected_engine_pins_its_capabilities_and_submission_routes(self):
        self.engine.capabilities_path = "/v1/engines/selected/capabilities"
        self.engine.calculations_path = "/v1/engines/selected/calculations"
        def respond(method, path, body):
            if path == self.engine.capabilities_path:
                return 200, self.api.capabilities
            if method == "POST" and path == self.engine.calculations_path:
                return self.api.default_response(method, "/v1/calculations", body)
            return None
        self.api.on_request = respond
        self.run_to_end()
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.success), 1)
        self.assertEqual([r[:2] for r in self.api.requests[:3]],
            [("GET", "/healthz"), ("GET", self.engine.capabilities_path), ("POST", self.engine.calculations_path)])
        self.assertNotIn(("GET", "/v1/capabilities", None), self.api.requests)

    def test_success_follows_links_and_uses_fractional_service_progress(self):
        self.run_to_end()
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.success), 1)
        self.assertEqual(self.busy, [True, False])
        self.assertEqual([request[:2] for request in self.api.requests[:2]],
                         [("GET", "/healthz"), ("GET", "/v1/capabilities")])
        self.assertEqual(self.methods("POST"), [("POST", "/v1/calculations", self.request)])
        self.assertTrue(any(value.get("completed_task_points") == 0.5 and value["percent"] == 25
                            for value in self.progress))
        context = self.success[0][0]
        self.assertEqual(context["model_summary"]["source_count"], len(self.request["sources"]))
        self.assertEqual(context["model_summary"]["receiver_count"], len(self.request["receivers"]))
        self.assertTrue(context["model_summary"]["receivers_resolved"])
        self.assertTrue(any(path.endswith("/output") for _, path, _ in self.methods("GET")))
        args = self.save.call_args.args
        self.assertIs(args[0], self.store)
        self.assertIs(args[1], self.snapshot)
        self.assertEqual(args[3]["transport"], "http_json_v1")
        self.assertEqual(args[3]["api_name"], "Lab API")
        self.assertEqual(args[4]["state"], "completed")

    def test_activity_only_progress_reaches_frontend(self):
        polls = 0
        def respond(method, path, body):
            nonlocal polls
            if method != "GET" or not path.startswith("/service-jobs/") or path.endswith("/output"):
                return None
            polls += 1
            identity = path.split("/")[2]
            if polls > 2:
                return 200, self.api.snapshot(identity, "completed", completed=2, sequence=4)
            value = self.api.snapshot(identity, completed=0.5, sequence=polls+1)
            value["progress"]["work"] = {"workers": 4, "active_workers": 4, "queued_jobs": 10,
                                         "completed_jobs": polls * 100, "operations": polls * 20}
            return 200, value
        self.api.on_request = respond
        self.run_to_end()
        values = [v for v in self.progress if v.get("work") and v.get("percent") == 25]
        self.assertEqual([v["work"]["completed_jobs"] for v in values], [100, 200])
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.success), 1)

    def test_get_outage_recovers_existing_job_without_reposting(self):
        self.api.poll_failures = 1
        self.run_to_end()
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.methods("POST")), 1)
        self.assertEqual(self.save.call_count, 1)

    def test_large_results_use_separate_limits_including_on_retry(self):
        original_request = ApiClient.request
        options_seen = []
        downloads = 0

        def request(client, method, url, body, callback, **options):
            options_seen.append((url, dict(options)))
            # Scale down the ordinary transport limit so the regression stays
            # small, while still exercising actual HTTP reads and JSON decode.
            options.setdefault("max_response_bytes", 16 * 1024)
            return original_request(client, method, url, body, callback, **options)

        def respond(method, path, body):
            nonlocal downloads
            if not path.endswith("/output"):
                return None
            downloads += 1
            if downloads == 1:
                return 503, {"error": {"message": "Temporary download failure"}}
            result = self.api.result(self.request)
            result["padding"] = "x" * (32 * 1024)
            return 200, result

        self.api.on_request = respond
        self.runner.RESULT_MAX_RESPONSE_BYTES = 64 * 1024
        self.runner.RESULT_TIMEOUT_MS = 12345
        with patch.object(ApiClient, "request", new=request):
            self.run_to_end()
        self.assertEqual(self.failures, [])
        self.assertEqual(self.save.call_count, 1)
        self.assertEqual(len(self.methods("POST")), 1)
        self.assertEqual(downloads, 2)
        self.assertEqual(len(self.save.call_args.args[2]["padding"]), 32 * 1024)
        for url, options in options_seen:
            self.assertEqual(options, {"max_response_bytes": 64 * 1024, "timeout_ms": 12345}
                             if url.endswith("/output") else {})

    def test_results_over_the_larger_limit_preserve_previous_results(self):
        self.runner.RESULT_MAX_RESPONSE_BYTES = 100
        self.run_to_end()
        self.assertEqual(self.success, [])
        self.assertEqual(len(self.failures), 1)
        self.assertIn("100 bytes", self.failures[0])
        self.assertIn("was not resubmitted", self.failures[0])
        self.assertEqual(len(self.methods("POST")), 1)
        self.save.assert_not_called()

    def test_progress_updates_resolved_receiver_grid_counts(self):
        self.request["area_receivers"] = [{"id": "grid", "boundary": [[0, 0], [2, 0], [2, 2], [0, 2]],
                                           "spacing_m": 1, "height_m": 1.5}]
        original = self.api.snapshot
        def snapshot(*args, **kwargs):
            value = original(*args, **kwargs)
            value["progress"]["model_summary"] = {"source_count": len(self.request["sources"]), "receiver_count": 10}
            value["progress"]["path_counts"] = {"checked": 10, "pruned": 3}
            return value
        with patch.object(self.api, "snapshot", side_effect=snapshot):
            self.run_to_end()
        self.assertEqual(self.failures, [])
        summary = self.success[0][0]["model_summary"]
        self.assertTrue(summary["receivers_resolved"])
        self.assertEqual(summary["receiver_count"], 10)
        self.assertEqual(next(v for v in reversed(self.progress) if "path_counts" in v)["path_counts"],
                         {"checked": 10, "pruned": 3})
        saved_job = self.save.call_args.args[4]
        self.assertEqual(saved_job["progress"]["path_counts"], {"checked": 10, "pruned": 3})

    def test_area_rasters_download_after_results_and_reach_persistence(self):
        self.request["area_receivers"] = [{"id": "grid", "boundary": [[0, 0], [2, 0], [2, 2], [0, 2]],
                                           "spacing_m": 1, "height_m": 1.5},
                                          {"id": "yard", "boundary": [[5, 5], [6, 5], [6, 6]],
                                           "spacing_m": 0.5, "height_m": 4}]
        staged = {}
        self.save.side_effect = lambda *args: staged.update(
            {key: open(info["path"], "rb").read() for key, info in args[5].items()}) or {"run_id": "saved"}
        self.run_to_end()
        self.assertEqual(self.failures, [])
        gets = [path for _, path, _ in self.methods("GET")]
        output = next(i for i, path in enumerate(gets) if path.endswith("/output"))
        artifacts = [path for path in gets[output:] if "/artifacts/" in path]
        self.assertEqual([path.rsplit("/", 1)[1] for path in artifacts], ["area-0.tif", "area-1.tif"])
        rasters = self.save.call_args.args[5]
        self.assertEqual(set(rasters), {"grid", "yard"})
        for identity, name in (("grid", "area-0.tif"), ("yard", "area-1.tif")):
            payload = self.api.raster_bytes(name)
            self.assertEqual(staged[identity], payload)
            self.assertEqual(rasters[identity]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertTrue(any(v.get("phase") == "downloading" for v in self.progress))
        # Staged downloads never outlive the attempt.
        from pathlib import Path
        self.assertEqual(list(Path(self.directory.name, "model_results").iterdir()), [])

    def test_failed_area_raster_download_keeps_previous_results(self):
        self.request["area_receivers"] = [{"id": "grid", "boundary": [[0, 0], [2, 0], [2, 2], [0, 2]],
                                           "spacing_m": 1, "height_m": 1.5}]
        self.api.on_request = lambda method, path, body: (
            (404, {"error": {"code": "not_found", "message": "Evicted"}}) if "/artifacts/" in path else None)
        self.run_to_end()
        self.assertEqual(self.success, [])
        self.assertIn("Could not download an area result raster", self.failures[0])
        self.assertIn("Evicted", self.failures[0])
        self.save.assert_not_called()

    def test_post_error_is_never_retried(self):
        self.api.on_request = lambda method, path, body: (503, {"error": {"code": "job_capacity", "message": "Full"}}) if method == "POST" else None
        self.run_to_end()
        self.assertEqual(len(self.methods("POST")), 1)
        self.assertIn("job_capacity", self.failures[0])
        self.assertIn("not retried", self.failures[0])
        self.save.assert_not_called()

    def test_cancel_sends_delete_and_preserves_results(self):
        self.api.hold = True
        self.runner.start(self.store, "one")
        wait_until(lambda: self.runner._job is not None)
        self.runner.cancel()
        wait_until(lambda: not self.runner.active)
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.stops), 1)
        self.assertEqual(len(self.methods("DELETE")), 1)
        self.save.assert_not_called()

    def test_cancel_keeps_polling_until_workers_confirm_stop(self):
        self.api.hold = True
        self.runner.start(self.store, "one")
        wait_until(lambda: self.runner._job is not None)
        allow_stop = [False]
        sequence = [10]
        def respond(method, path, body):
            if method == "DELETE" or (method == "GET" and path.startswith("/service-jobs/")):
                identity = path.split("/")[2]
                sequence[0] += 1
                value = self.api.snapshot(identity, "cancelled" if allow_stop[0] else "running", sequence=sequence[0])
                if not allow_stop[0]:
                    value["progress"]["phase"] = "cancelling"
                return 200, value
        self.api.on_request = respond
        self.runner.cancel()
        wait_until(lambda: any(v.get("phase") == "cancelling" for v in self.progress))
        self.assertTrue(self.runner.active)
        self.assertEqual(self.stops, [])
        self.save.assert_not_called()
        allow_stop[0] = True
        wait_until(lambda: not self.runner.active)
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.stops), 1)
        self.assertEqual(len(self.methods("DELETE")), 1)
        self.save.assert_not_called()

    def test_cancel_during_post_waits_for_id_then_deletes(self):
        self.api.hold = True
        self.api.post_delay = 0.1
        self.runner.start(self.store, "one")
        wait_until(lambda: bool(self.methods("POST")))
        self.runner.cancel()
        wait_until(lambda: not self.runner.active)
        self.assertEqual(len(self.stops), 1)
        self.assertEqual(len(self.methods("DELETE")), 1)
        self.assertEqual(len(self.methods("POST")), 1)
        self.save.assert_not_called()

    def test_cancel_from_synchronous_status_signal_is_safe(self):
        self.runner.progressChanged.connect(lambda context, value: self.runner.cancel()
                                            if value["phase"] == "checking" else None)
        self.run_to_end()
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.stops), 1)
        self.assertEqual(self.methods("POST"), [])
        self.prepare.assert_not_called()

    def test_cancellation_failure_is_reported_without_writing(self):
        self.api.hold = True
        self.api.on_request = lambda method, path, body: (503, {"error": {"code": "shutting_down", "message": "Unavailable"}}) if method == "DELETE" else None
        self.runner.start(self.store, "one")
        wait_until(lambda: self.runner._job is not None)
        self.runner.cancel()
        wait_until(lambda: not self.runner.active)
        self.assertIn("Could not confirm cancellation", self.failures[0])
        self.save.assert_not_called()

    def test_local_input_check_does_not_submit_or_claim_acoustic_validation(self):
        self.run_to_end(validate=True)
        self.assertEqual(self.success[0][1]["validation_scope"], "local_api_adapter")
        self.assertEqual(self.methods("POST"), [])
        self.save.assert_not_called()

    def test_batch_keeps_busy_until_all_variants_finish(self):
        self.runner.start_batch(self.store, ["one", "two"])
        wait_until(lambda: not self.runner.active)
        self.assertEqual([entry[0]["variant_id"] for entry in self.success], ["one", "two"])
        self.assertEqual(self.busy, [True, False])
        self.assertEqual(len(self.methods("POST")), 2)
        self.assertEqual(self.save.call_count, 2)

    def test_failed_job_reports_field_diagnostic_without_writing(self):
        def fail(method, path, body):
            if method == "GET" and path.startswith("/service-jobs/"):
                result = self.api.snapshot(next(iter(self.api.jobs)), "failed", sequence=2)
                result["error"] = {"code": "unsupported", "field": "sources[0]", "message": "Unsupported path"}
                return 200, result
        self.api.on_request = fail
        self.run_to_end()
        self.assertIn("sources[0]: Unsupported path [unsupported]", self.failures[0])
        self.save.assert_not_called()

    def test_submission_and_running_errors_show_saved_building_names(self):
        from tests.test_api_diagnostics import BUILDING_ID, REFERENCE
        self.snapshot["id_mapping"] = {BUILDING_ID: dict(REFERENCE)}
        raw = f"building:{BUILDING_ID}:wall:3:4: prepared terrain 185.339747 m reaches or exceeds surface top 178.322667 m"
        contexts = []
        self.runner.failed.connect(lambda context, message: contexts.append(context))
        for submission in (True, False):
            with self.subTest(submission=submission):
                def fail(method, path, body):
                    error = {"code": "invalid_input", "field": "buildings/barriers", "message": raw}
                    if submission and method == "POST":
                        return 422, {"error": error}
                    if not submission and method == "GET" and path.startswith("/service-jobs/"):
                        result = self.api.snapshot(path.split("/")[2], "failed", sequence=2)
                        result["error"] = error
                        return 200, result
                self.api.on_request = fail
                self.run_to_end()
                self.assertIn('Building “OSM way 1311885728” (layer “OpenStreetMap buildings”, feature 27)', self.failures[-1])
                self.assertIn(":wall:3:4: prepared terrain 185.339747 m", self.failures[-1])
                self.assertIn("[invalid_input]", self.failures[-1])
                self.assertNotIn(BUILDING_ID, self.failures[-1])
                self.assertIn(raw, contexts[-1]["api_error_raw"])
                self.save.assert_not_called()

    def test_invalid_snapshot_and_nonfinite_json_fail_without_writing(self):
        for response in (b'{"id": NaN}', {"id": "job", "state": "unexpected"},
                         self.api.snapshot("job", "running", completed=2)):
            with self.subTest(response=response):
                self.api.on_request = lambda method, path, body: (202, response) if method == "POST" else None
                self.run_to_end()
                self.assertFalse(self.success)
                self.assertTrue(self.failures)
                self.save.assert_not_called()

    def test_cross_origin_returned_links_are_rejected(self):
        result = self.api.snapshot("job")
        result["links"]["status"] = "http://example.invalid/other-service"
        self.api.on_request = lambda method, path, body: (202, result) if method == "POST" else None
        self.run_to_end()
        self.assertIn("different server", self.failures[0])
        self.save.assert_not_called()

    def test_snapshot_cannot_change_id_or_move_backwards(self):
        for invalid in ("id", "state", "sequence", "progress"):
            with self.subTest(invalid=invalid):
                count = [0]

                def respond(method, path, body):
                    if method == "GET" and path.startswith("/service-jobs/"):
                        count[0] += 1
                        identity = path.split("/")[2]
                        result = self.api.snapshot(identity, completed=1, sequence=2)
                        if count[0] > 1:
                            if invalid == "id":
                                result["id"] = "different"
                            elif invalid == "state":
                                result["state"] = "queued"
                                result["sequence"] = 3
                            elif invalid == "sequence":
                                result["sequence"] = 1
                            else:
                                result["progress"].update(completed_task_points=0.5, percent=25)
                                result["sequence"] = 3
                        return 200, result

                self.api.on_request = respond
                self.run_to_end()
                self.assertFalse(self.success)
                self.assertTrue(self.failures)
                self.save.assert_not_called()

    def test_unreachable_accepted_job_does_not_trigger_submission_retry(self):
        self.api.on_request = lambda method, path, body: (404, {"error": {"code": "not_found", "message": "Job lost after restart"}}) if path.startswith("/service-jobs/") else None
        self.run_to_end()
        self.assertEqual(len(self.methods("POST")), 1)
        self.assertIn("not_found", self.failures[0])
        self.assertIn("not resubmitted", self.failures[0])
        self.save.assert_not_called()

    def test_submission_serialization_failure_clears_submission_flag(self):
        self.request["invalid"] = float("nan")
        self.run_to_end()
        self.assertIn("finite JSON", self.failures[0])
        self.assertFalse(self.runner._submitting)
        self.assertEqual(self.methods("POST"), [])

    def test_model_write_failure_does_not_report_success(self):
        self.save.side_effect = ModelError("Model inputs changed during calculation; previous results were preserved.")
        self.run_to_end()
        self.assertEqual(self.success, [])
        self.assertIn("Model inputs changed", self.failures[0])

    def test_changed_advertised_engine_requires_rediscovery(self):
        self.api.capabilities["implementation"]["name"] = "replacement-engine"
        self.run_to_end()
        self.assertIn("Refresh capabilities", self.failures[0])
        self.assertEqual(self.methods("POST"), [])

    def test_relationships_are_explicitly_unsupported(self):
        with self.assertRaisesRegex(ModelError, "does not provide source"):
            self.runner.start_relationships(self.store, "one")
        self.assertFalse(self.runner.active)
        self.assertEqual(self.api.requests, [])

    def test_shutdown_deletes_accepted_job_without_accessing_model(self):
        self.api.hold = True
        self.runner.start(self.store, "one")
        wait_until(lambda: self.runner._job is not None)
        self.runner.shutdown()
        wait_until(lambda: len(self.methods("DELETE")) == 1)
        self.assertFalse(self.runner.active)
        self.assertEqual(self.success, [])
        self.save.assert_not_called()

    def test_shutdown_during_post_cancels_after_acceptance(self):
        self.api.post_delay = 0.1
        self.runner.start(self.store, "one")
        wait_until(lambda: bool(self.methods("POST")))
        self.runner.shutdown()
        wait_until(lambda: len(self.methods("DELETE")) == 1)
        self.assertFalse(self.runner.active)
        self.assertEqual(self.success, [])
        self.save.assert_not_called()


class ApiClientTests(unittest.TestCase):
    def test_timeout_is_finite_and_does_not_block_qt(self):
        with FakeCalculationAPI() as api:
            api.on_request = lambda *_: (time.sleep(0.2) or (200, {"status": "ok"}))
            client = ApiClient(timeout_ms=40)
            events, ticks = [], []
            timer = QTimer()
            timer.timeout.connect(lambda: ticks.append(True))
            timer.start(5)
            client.request("GET", api.url + "/healthz", None, lambda data, error, headers: events.append(error))
            wait_until(lambda: bool(events))
            timer.stop()
            self.assertIn("timed out", str(events[0]))
            self.assertGreater(len(ticks), 1)
            client.deleteLater()

    def test_excessive_response_is_rejected(self):
        with FakeCalculationAPI() as api:
            api.on_request = lambda *_: (200, {"response": "x" * 2048})
            client = ApiClient()
            events = []
            client.request("GET", api.url + "/healthz", None, lambda data, error, headers: events.append(error), max_response_bytes=1024)
            wait_until(lambda: bool(events))
            self.assertIn("exceeds", str(events[0]))
            client.deleteLater()

    def test_http_diagnostic_preserves_code_and_field(self):
        with FakeCalculationAPI() as api:
            api.on_request = lambda *_: (422, {"error": {"code": "unsupported", "field": "sources[2]", "message": "Line source unsupported"}})
            client = ApiClient()
            events = []
            client.request("POST", api.url + "/v1/calculations", {}, lambda data, error, headers: events.append(error))
            wait_until(lambda: bool(events))
            self.assertEqual(events[0].status, 422)
            self.assertEqual(events[0].code, "unsupported")
            self.assertIn("sources[2]", str(events[0]))
            client.deleteLater()


class PathCountValidationTests(unittest.TestCase):
    def test_optional_counts_are_strict_nonnegative_integers(self):
        from acalc.run_info import validate_path_counts
        self.assertIsNone(validate_path_counts(None))
        self.assertEqual(validate_path_counts({"checked":10,"pruned":4}), {"checked":10,"pruned":4})
        for value in ({}, {"checked":1,"pruned":2}, {"checked":True,"pruned":0},
                      {"checked":1.5,"pruned":0}, {"checked":1,"pruned":-1}):
            with self.subTest(value=value), self.assertRaises(ModelError):
                validate_path_counts(value)
