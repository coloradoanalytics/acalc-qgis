# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Credential boundaries and authenticated calculation lifecycle over HTTP."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acalc.api_client import ApiClient
from acalc.schema import ModelError
from tests import test_api_runner
from tests.test_api_runner import FakeCalculationAPI, wait_until


CLIENT_KEY = "fixture-client-key-never-save-7c912e"


def remote_connection(api):
    return {"id": "test-api", "name": "Lab API", "mode": "remote",
            "base_url": api.url, "authcfg": "testcfg"}


class AuthenticatedClientTests(unittest.TestCase):
    def request(self, client, method, url, body=None, **options):
        events = []
        client.request(method, url, body, lambda *args: events.append(args), **options)
        wait_until(lambda: bool(events))
        return events[0]

    def test_health_does_not_load_or_send_key_but_all_api_methods_do(self):
        with FakeCalculationAPI() as api, patch("acalc.api_connections.connection_client_key", return_value=CLIENT_KEY) as load:
            api.client_key = CLIENT_KEY
            client = ApiClient(connection=remote_connection(api))
            try:
                self.assertIsNone(self.request(client, "GET", "healthz")[1])
                load.assert_not_called()
                self.assertNotIn("Authorization", api.request_headers[0])
                self.assertIsNone(self.request(client, "GET", "v1/capabilities")[1])
                load.assert_called_once()
                job, error, _ = self.request(client, "POST", "v1/calculations", {"sources": []})
                self.assertIsNone(error)
                self.assertIsNone(self.request(client, "GET", job["links"]["status"])[1])
                self.assertIsNone(self.request(client, "GET", job["links"]["results"])[1])
                api.on_request = lambda method, path, body: (200, {"event": "progress"}) if path.endswith("/events") else None
                self.assertIsNone(self.request(client, "GET", job["links"]["status"] + "/events")[1])
                self.assertIsNone(self.request(client, "DELETE", job["links"]["status"])[1])
                self.assertTrue(all(h.get("Authorization") == "Bearer " + CLIENT_KEY for h in api.request_headers[1:]))
                # Health stays unauthenticated after an authenticated request.
                self.assertIsNone(self.request(client, "GET", "/healthz")[1])
                self.assertNotIn("Authorization", api.request_headers[-1])
            finally:
                client.deleteLater()

    def test_draft_key_stays_transient_and_is_cleared_after_shared_check(self):
        from acalc.api_connection_check import ConnectionCheck
        with FakeCalculationAPI() as api, patch("acalc.api_connections.connection_client_key") as load:
            api.client_key = CLIENT_KEY
            checker = ConnectionCheck()
            events = []
            checker.finished.connect(events.append)
            try:
                checker.start({"mode": "remote", "base_url": api.url}, client_key=CLIENT_KEY)
                wait_until(lambda: bool(events))
                self.assertEqual(events[0]["error"], "")
                self.assertEqual([r[1] for r in api.requests], ["/healthz", "/v1/engines", "/v1/capabilities"])
                self.assertNotIn("Authorization", api.request_headers[0])
                self.assertTrue(all(h.get("Authorization") == "Bearer " + CLIENT_KEY for h in api.request_headers[1:]))
                load.assert_not_called()
                self.assertIsNone(checker.client._client_key)
                self.assertNotIn(CLIENT_KEY, json.dumps(events))
                checker.start({"mode": "remote", "base_url": api.url}, client_key=CLIENT_KEY)
                checker.cancel()
                self.assertIsNone(checker.client._client_key)
            finally:
                checker.cancel()
                checker.deleteLater()

    def test_auth_store_failure_happens_after_health_succeeds(self):
        with FakeCalculationAPI() as api, patch("acalc.api_connections.connection_client_key", side_effect=ModelError("Unlock the credential store.")) as load:
            client = ApiClient(connection=remote_connection(api))
            try:
                self.assertIsNone(self.request(client, "GET", "healthz")[1])
                load.assert_not_called()
                with self.assertRaisesRegex(ModelError, "Unlock"):
                    self.request(client, "GET", "v1/capabilities")
                self.assertEqual(len(api.requests), 1)
            finally:
                client.deleteLater()

    def test_401_is_always_a_key_problem_without_exposing_server_text(self):
        with FakeCalculationAPI() as api, patch("acalc.api_connections.connection_client_key", return_value=CLIENT_KEY):
            api.on_request = lambda *_: (401, {"error": {"code": CLIENT_KEY, "message": CLIENT_KEY}})
            client = ApiClient(connection=remote_connection(api))
            try:
                data, error, _ = self.request(client, "GET", "v1/capabilities")
                self.assertIsNone(data)
                self.assertEqual((error.status, error.code, error.transient), (401, "unauthorized", False))
                self.assertIn("client key", str(error))
                self.assertNotIn("Could not reach", str(error))
                self.assertNotIn(CLIENT_KEY, str(error))
            finally:
                client.deleteLater()

    def test_reflected_secret_is_removed_from_success_json_headers_and_errors(self):
        with FakeCalculationAPI() as api, patch("acalc.api_connections.connection_client_key", return_value=CLIENT_KEY):
            api.response_headers = {"X-Fixture-Detail": CLIENT_KEY}
            api.on_request = lambda *_: (200, {"detail": [CLIENT_KEY, {CLIENT_KEY: "Bearer " + CLIENT_KEY}]})
            client = ApiClient(connection=remote_connection(api))
            try:
                data, error, headers = self.request(client, "GET", "v1/capabilities")
                self.assertIsNone(error)
                self.assertNotIn(CLIENT_KEY, json.dumps([data, headers]))
                self.assertIn("[redacted]", json.dumps(data))
                api.on_request = lambda *_: (422, {"error": {"code": CLIENT_KEY, "field": CLIENT_KEY, "message": CLIENT_KEY}})
                _, error, headers = self.request(client, "POST", "v1/calculations", {})
                self.assertNotIn(CLIENT_KEY, str(error) + error.code + json.dumps(headers))
            finally:
                client.deleteLater()

    def test_auth_is_sent_on_artifact_download_and_401_removes_partial_file(self):
        with FakeCalculationAPI() as api, tempfile.TemporaryDirectory() as directory, patch("acalc.api_connections.connection_client_key", return_value=CLIENT_KEY):
            api.client_key = CLIENT_KEY
            api.on_request = lambda *_: (200, b"raster data")
            client = ApiClient(connection=remote_connection(api))
            try:
                path = str(Path(directory, "area.tif"))
                events = []
                client.download("v1/calculations/job/artifacts/area.tif", path, lambda *args: events.append(args),
                                media_type="image/tiff", max_response_bytes=1024)
                wait_until(lambda: bool(events))
                self.assertIsNone(events[0][1])
                self.assertEqual(Path(path).read_bytes(), b"raster data")
                self.assertEqual(api.request_headers[-1]["Authorization"], "Bearer " + CLIENT_KEY)
                events.clear()
                api.on_request = lambda *_: (401, {"error": {"message": CLIENT_KEY}})
                client.download("v1/calculations/job/artifacts/area.tif", path, lambda *args: events.append(args),
                                media_type="image/tiff", max_response_bytes=1024)
                wait_until(lambda: bool(events))
                self.assertEqual(events[0][1].code, "unauthorized")
                self.assertNotIn(CLIENT_KEY, str(events[0][1]))
                self.assertFalse(Path(path).exists())
            finally:
                client.deleteLater()

    def test_cross_origin_links_and_redirects_cannot_forward_the_key(self):
        with FakeCalculationAPI() as api, FakeCalculationAPI() as other, patch("acalc.api_connections.connection_client_key", return_value=CLIENT_KEY):
            client = ApiClient(connection=remote_connection(api))
            try:
                with self.assertRaisesRegex(ModelError, "different server"):
                    self.request(client, "GET", other.url + "/v1/capabilities")
                api.on_request = lambda *_: (302, {"redirect": "elsewhere"})
                api.response_headers = {"Location": other.url + "/v1/capabilities"}
                _, error, _ = self.request(client, "GET", "v1/capabilities")
                self.assertEqual(error.status, 302)
                self.assertEqual(other.requests, [])
            finally:
                client.deleteLater()


class AuthenticatedRunnerTests(unittest.TestCase):
    run_to_end = test_api_runner.ApiRunnerTests.run_to_end
    methods = test_api_runner.ApiRunnerTests.methods

    def setUp(self):
        test_api_runner.ApiRunnerTests.setUp(self)
        self.api.client_key = CLIENT_KEY
        self.engine.connection = remote_connection(self.api)
        self.auth_patch = patch("acalc.api_connections.connection_client_key", return_value=CLIENT_KEY)
        self.auth = self.auth_patch.start()

    def tearDown(self):
        test_api_runner.ApiRunnerTests.tearDown(self)
        self.auth_patch.stop()

    def test_authenticated_run_persists_no_client_key_or_auth_reference(self):
        self.api.capabilities["fixture_detail"] = CLIENT_KEY
        self.request["area_receivers"] = [{"id": "grid", "boundary": [[0, 0], [2, 0], [2, 2], [0, 2]],
                                           "spacing_m": 1, "height_m": 1.5}]
        self.run_to_end()
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.success), 1)
        self.assertNotIn("Authorization", self.api.request_headers[0])
        self.assertTrue(all(h.get("Authorization") == "Bearer " + CLIENT_KEY for h in self.api.request_headers[1:]))
        self.assertTrue(any("/artifacts/" in path for _, path, _ in self.api.requests))
        saved = json.dumps(self.save.call_args.args[2:5])
        self.assertNotIn(CLIENT_KEY, saved)
        self.assertNotIn("testcfg", saved)
        self.assertIn("[redacted]", saved)

    def test_node_health_refuses_before_key_lookup_or_submission(self):
        self.api.on_request = lambda method, path, body: (200, {"status": "ok", "role": "node", "key_required": True}) if path == "/healthz" else None
        self.run_to_end()
        self.assertIn("coordinator", self.failures[0].lower())
        self.assertEqual([path for _, path, _ in self.api.requests], ["/healthz"])
        self.auth.assert_not_called()
        self.save.assert_not_called()

    def test_local_key_required_refuses_before_capabilities(self):
        self.engine.connection["mode"] = "local"
        self.run_to_end()
        self.assertIn("Remote", self.failures[0])
        self.assertEqual([path for _, path, _ in self.api.requests], ["/healthz"])
        self.auth.assert_not_called()

    def test_wrong_key_refuses_without_submission_or_connectivity_error(self):
        self.auth.return_value = "wrong-fixture-key"
        self.run_to_end()
        self.assertIn("client key", self.failures[0])
        self.assertNotIn("Could not reach", self.failures[0])
        self.assertEqual(self.methods("POST"), [])
        self.save.assert_not_called()
