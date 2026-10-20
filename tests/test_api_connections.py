# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Named API persistence, discovery gating and explicit numerical choices."""

from copy import deepcopy
import tempfile
import unittest

from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsCoordinateReferenceSystem

from acalc.api_connections import (
    ApiConnectionStore, LOCAL_BASE_URL, capabilities_problem,
    normalize_base_url,
)
from acalc.api_settings_ui import ApiISOSettingsEditor, ApiSettingsPage
from acalc.calculation_engines import available_engines
from acalc.calculation_settings import default_settings
from acalc.schema import ModelError
from acalc.storage import ModelStore
from tests.test_model import APP


def capabilities(**overrides):
    return {
        "api_version": "1.0",
        "implementation": {"name": "test_engine", "version": "0.1"},
        "method": "ISO 9613-2:2024",
        "atmospheric_absorption": "ISO 9613-1:1993",
        "frequencies_hz": [63, 125, 250, 500, 1000, 2000, 4000, 8000],
        "source_kinds": ["point", "area"], "receiver_kinds": ["point", "area"],
        "max_reflect_order": 3, "verified_conformity": False,
        "limitations": ["Uniform horizontal ground only", "Reference cases remain unverified"],
        **overrides,
    }


class FakeClient:
    def __init__(self):
        self.requests = []
        self.aborted = False

    def configure_connection(self, connection, *, client_key=None):
        self.connection = deepcopy(connection)
        self.client_key = client_key

    def clear_credentials(self):
        self.client_key = None

    def resolve_url(self, url):
        from urllib.parse import urljoin, urlsplit
        if not isinstance(url, str) or not url:
            raise ModelError("Missing link")
        absolute = urljoin(self.connection["base_url"] + "/", url)
        if urlsplit(absolute).netloc != urlsplit(self.connection["base_url"]).netloc:
            raise ModelError("different server")
        return absolute

    def request(self, method, url, data, callback):
        self.requests.append((method, url, data, callback))

    def respond(self, data=None, error=None):
        self.requests[-1][-1](data, error, {})

    def abort_all(self):
        self.aborted = True


class ApiConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = QSettings(self.directory.name + "/apis.ini", QSettings.IniFormat)
        self.store = ApiConnectionStore(self.settings)
        self.pages = []

    def tearDown(self):
        for page in self.pages:
            if isinstance(page, ApiSettingsPage):
                page.shutdown()
            page.close()
            page.deleteLater()
        APP.processEvents()
        self.directory.cleanup()

    def add_connection(self, name="Local", url="http://127.0.0.1:8343", discovered=False):
        value = self.store.save(name, url)
        if discovered:
            self.store.record_check(value["id"], "capabilities", capabilities())
        return self.store.get(value["id"])

    def page(self):
        client = FakeClient()
        page = ApiSettingsPage(connection_store=self.store, client=client)
        self.pages.append(page)
        return page, client

    def test_multiple_named_connections_persist_without_a_default_engine(self):
        self.assertEqual(available_engines(self.store), ())
        first = self.add_connection("Local", discovered=True)
        second = self.add_connection("Remote", "https://engine.example.test/service/", discovered=True)
        reloaded = ApiConnectionStore(QSettings(self.directory.name + "/apis.ini", QSettings.IniFormat))
        engines = available_engines(reloaded)
        self.assertEqual(len(engines), 2)
        self.assertIn("Local — ISO 9613-2:2024 — test_engine 0.1", [e.name for e in engines])
        self.assertEqual(len({e.id for e in engines}), 2)
        self.assertEqual(engines[1].connection["base_url"], "https://engine.example.test/service")
        self.assertEqual({c["id"] for c in reloaded.connections()}, {first["id"], second["id"]})
        self.assertFalse(hasattr(engines[0], "backend_settings"))

    def test_name_changes_retain_identity_but_endpoint_changes_invalidate_discovery(self):
        original = self.add_connection(discovered=True)
        original_engine = available_engines(self.store)[0].id
        self.store.save("Renamed", original["base_url"], original["id"])
        self.assertEqual(available_engines(self.store)[0].id, original_engine)
        updated = self.store.save("Renamed", "http://localhost:9999", original["id"])
        self.assertNotIn("capabilities", updated)
        self.assertEqual(available_engines(self.store), ())
        self.assertFalse(self.store.record_check(original["id"], "capabilities", capabilities(), expected_url=original["base_url"]))
        self.store.remove(original["id"])
        self.assertFalse(self.store.record_check(original["id"], "health", {"status": "ok"}))

    def test_names_and_urls_are_validated(self):
        self.add_connection()
        for name in ("", "  ", "LOCAL"):
            with self.assertRaises(ModelError):
                self.store.save(name, "http://localhost:343")
        for url in ("localhost:343", "file:///tmp/api", "http://user:secret@localhost", "https://host/#fragment",
                    "https://host/?token=a", "http://localhost:99999", "http://local host", "http://localhost:0"):
            with self.subTest(url=url), self.assertRaises(ModelError):
                normalize_base_url(url)
        self.assertEqual(normalize_base_url(" http://[::1]:343/ "), "http://[::1]:343")

    def test_failed_checks_keep_cached_evidence_but_remove_available_engine(self):
        value = self.add_connection(discovered=True)
        self.store.record_check(value["id"], "health", {"status": "ok", "version": "0.1"})
        self.assertEqual(len(available_engines(self.store)), 1)
        self.store.record_check(value["id"], "health", error="Connection refused")
        self.assertEqual(available_engines(self.store), ())
        self.assertEqual(self.store.get(value["id"])["capabilities"], capabilities())
        self.assertTrue(self.store.get(value["id"])["health_checked_at"].endswith("Z"))
        self.store.record_check(value["id"], "health", {"status": "ok"})
        self.store.record_check(value["id"], "capabilities", error="Invalid JSON")
        self.assertEqual(available_engines(self.store), ())
        self.store.record_check(value["id"], "capabilities", capabilities())
        self.assertEqual(len(available_engines(self.store)), 1)

    def test_incompatible_method_or_incomplete_capabilities_are_not_selectable(self):
        value = self.add_connection()
        for override in ({"method": "ISO 9613-2:1996"}, {"atmospheric_absorption": "other"},
                         {"api_version": "2.0"}, {"frequencies_hz": [1000]},
                         {"max_reflect_order": True}, {"source_kinds": "point"},
                         {"implementation": None}):
            with self.subTest(override=override):
                payload = capabilities(**override)
                self.store.record_check(value["id"], "capabilities", payload)
                self.assertTrue(capabilities_problem(payload))
                self.assertEqual(available_engines(self.store), ())
        self.store.record_check(value["id"], "health", {"status": "starting"})
        self.assertEqual(self.store.get(value["id"])["health_status"], "error")

    def test_connection_save_removes_obsolete_numerical_switch(self):
        value = self.add_connection(discovered=True)
        self.store._write([{**value, "backend_settings": {"replace_legacy_numerical_settings": False}}])
        saved = self.store.save(value["name"], value["base_url"], value["id"])
        self.assertNotIn("backend_settings", saved)

    def test_api_shared_editor_preserves_incompatible_and_dormant_model_settings(self):
        value = self.add_connection(discovered=True)
        editor = ApiISOSettingsEditor(value)
        self.pages.append(editor)
        settings = default_settings()
        settings.update(reflection_order=5)
        original = deepcopy(settings)
        editor.set_settings(settings)
        self.assertEqual(editor.settings(), original)
        self.assertIn("maximum of 3", editor.compatibility.text())
        self.assertFalse(hasattr(editor, "lateral_diffraction"))
        self.assertNotIn("subdivision_tolerance_db", editor.fields)
        self.assertNotIn("max_operations", editor.fields)
        self.assertNotIn("max_operations", editor.settings())
        editor.fields["temperature_c"].setValue(15)
        self.assertEqual(editor.settings(), {**original, "temperature_c": 15})

    def test_model_pruning_switch_selects_a_weighted_strategy_without_numbers(self):
        editor = ApiISOSettingsEditor(self.add_connection(discovered=True))
        self.pages.append(editor)
        self.assertIsNone(editor.settings()["pruning_strategy"])
        self.assertNotIn("minimum_contribution_db", editor.settings())
        self.assertFalse(editor.pruning_check.isChecked())
        self.assertFalse(editor.pruning_basis.isEnabled())
        self.assertEqual([editor.pruning_basis.itemData(i) for i in range(editor.pruning_basis.count())],
                         ["dba", "dbc"])
        self.assertIn("octave bands", editor.pruning_note.text())
        editor.pruning_check.click()
        self.assertTrue(editor.pruning_basis.isEnabled())
        self.assertEqual(editor.settings()["pruning_strategy"], "dba")
        editor.pruning_basis.setCurrentIndex(editor.pruning_basis.findData("dbc"))
        self.assertEqual(editor.settings()["pruning_strategy"], "dbc")
        self.assertNotIn("receiver_margin_db", editor.settings())
        editor.numerical_fields["area_absolute_tolerance"].setText("2e-14")
        editor.ground_method.setCurrentIndex(editor.ground_method.findData("mean_path_height"))
        settings = editor.settings()
        self.assertEqual(settings["area_absolute_tolerance"], 2e-14)
        self.assertEqual(settings["ground_method"], "mean_path_height")
        editor.set_settings(settings)
        self.assertEqual(editor.settings(), settings)
        self.assertEqual(editor.settings()["pruning_strategy"], "dbc")

    def test_disabled_pruning_basis_survives_project_reopen(self):
        connection = self.add_connection(discovered=True)
        editor = ApiISOSettingsEditor(connection)
        self.pages.append(editor)
        editor.pruning_check.click()
        editor.pruning_basis.setCurrentIndex(editor.pruning_basis.findData("dbc"))
        editor.pruning_check.click()
        self.assertIsNone(editor.settings()["pruning_strategy"])
        path = self.directory.name + "/pruning.gpkg"
        model = ModelStore.create(path, "Pruning", QgsCoordinateReferenceSystem("EPSG:26913").toWkt())
        model.save_calculation(editor.settings())
        reopened = ApiISOSettingsEditor(connection)
        self.pages.append(reopened)
        reopened.set_settings(ModelStore(path).calculation()["settings"])
        self.assertFalse(reopened.pruning_check.isChecked())
        self.assertEqual(reopened.pruning_basis.currentData(), "dbc")
        reopened.pruning_check.click()
        self.assertEqual(reopened.settings()["pruning_strategy"], "dbc")
        # A different project has its own presets, not the previous project's values.
        reopened.set_settings(default_settings())
        reopened.pruning_check.click()
        self.assertEqual(reopened.settings()["pruning_strategy"], "dba")

    def test_existing_native_settings_discard_receiver_margin(self):
        editor = ApiISOSettingsEditor(self.add_connection(discovered=True))
        self.pages.append(editor)
        settings = default_settings()
        del settings["pruning_values"]
        settings.update(receiver_margin_db=31)
        editor.set_settings(settings)
        self.assertNotIn("receiver_margin_db", editor.optional_fields)
        self.assertNotIn("receiver_margin_db", editor.settings())
        self.assertNotIn("receiver_margin_db", editor.settings()["pruning_values"])

    def test_removed_pruning_control_is_cleaned_when_saved_project_opens(self):
        import json
        path = self.directory.name + "/removed-control.gpkg"
        model = ModelStore.create(path, "Pruning", QgsCoordinateReferenceSystem("EPSG:26913").toWkt())
        settings = default_settings()
        del settings["pruning_strategy"]  # saved before automatic pruning existed
        settings.update(minimum_contribution_db=0, receiver_margin_db=30,
                        receiver_omission_budget_db=None)
        settings["pruning_values"].update(minimum_contribution_db=-12,
                                          receiver_margin_db=34, receiver_omission_budget_db=0.1)
        with model.connection() as db:
            db.execute("UPDATE acalc_calculation SET settings_json=?", (json.dumps(settings),))
        editor = ApiISOSettingsEditor(self.add_connection(discovered=True))
        self.pages.append(editor)
        editor.set_settings(ModelStore(path).calculation()["settings"])
        # An enabled numeric cutoff becomes the dBA strategy.
        self.assertEqual(editor.settings()["pruning_strategy"], "dba")
        self.assertNotIn("minimum_contribution_db", editor.settings())
        self.assertNotIn("receiver_margin_db", editor.settings())
        self.assertNotIn("receiver_omission_budget_db", editor.settings())
        self.assertEqual(editor.settings()["pruning_values"], {"pruning_strategy": "dba"})
        model.save_calculation(editor.settings())
        with model.connection(readonly=True) as db:
            saved = db.execute("SELECT settings_json FROM acalc_calculation").fetchone()[0]
        self.assertNotIn("receiver_omission_budget_db", saved)
        self.assertNotIn("receiver_margin_db", saved)
