# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Connection menus, modal drafts, discovery and credential boundaries."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qgis.PyQt.QtCore import QSettings, QTimer
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLineEdit, QMessageBox
from qgis.core import QgsCoordinateReferenceSystem

from acalc.api_client import ApiClient, ApiError
from acalc.api_connections import ApiConnectionStore, LOCAL_BASE_URL, SETTINGS_KEY, connection_client_key
from acalc.api_settings_ui import ApiSettingsPage, EngineConnectionDialog
from acalc.calculation_engines import available_engines
from acalc.schema import ModelError
from acalc.storage import ModelStore
from tests.test_api_connections import FakeClient, capabilities
from tests.test_model import APP
class FakeCredentials:
    """Stand in for encrypted QGIS storage, separate from profile settings."""

    def __init__(self):
        self.keys = {}
        self.saved = []
        self.removed = []
        self.serial = 0

    def save(self, key, name, authcfg=None):
        self.serial += 1
        authcfg = authcfg or f"auth{self.serial:03}"
        self.keys[authcfg] = key
        self.saved.append((key, name, authcfg))
        return authcfg

    def load(self, authcfg):
        return self.keys[authcfg]

    def remove(self, authcfg):
        self.removed.append(authcfg)
        self.keys.pop(authcfg, None)



def engine(name="iso", *, default=True, version="1.2.3", state="ready", **extra):
    return dict(name=name, default=default, state=state, engine="engine-" + name,
                version=version, method="ISO 9613-2:2024",
                links={"capabilities": f"/v1/engines/{name}/capabilities", "calculations": f"/v1/engines/{name}/calculations"}, **extra)


class RemoteConnectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings_path = self.directory.name + "/engines.ini"
        self.settings = QSettings(self.settings_path, QSettings.IniFormat)
        self.credentials = FakeCredentials()
        self.store = ApiConnectionStore(self.settings, self.credentials)
        self.widgets = []
        self.warning_patch = patch("acalc.api_settings_ui.QMessageBox.warning")
        self.warning = self.warning_patch.start()

    def tearDown(self):
        for widget in self.widgets:
            if isinstance(widget, ApiSettingsPage):
                widget.shutdown()
            widget.close()
            widget.deleteLater()
        APP.processEvents()
        self.warning_patch.stop()
        self.directory.cleanup()

    def remote(self, **overrides):
        return self.store.save("Remote engine", overrides.get("url", "https://engine.example.test:8343"),
                               mode="remote", client_key=overrides.get("key", "saved-secret"))

    def page(self):
        client = FakeClient()
        widget = ApiSettingsPage(connection_store=self.store, client=client)
        self.widgets.append(widget)
        return widget, client

    def editor(self, connection=None):
        client = FakeClient()
        widget = EngineConnectionDialog(connection_store=self.store, connection=connection, client=client)
        self.widgets.append(widget)
        return widget, client

    def complete(self, client, profiles=None, caps=None):
        client.respond({"status": "ok", "version": "SERVICE-VERSION-MUST-NOT-APPEAR"})
        client.respond({"engines": profiles or [engine()]})
        client.respond(caps or capabilities())

    def assert_secret_not_saved(self, secret):
        self.settings.sync()
        self.assertNotIn(secret, Path(self.settings_path).read_text())
        self.assertNotIn(secret, json.dumps(self.store.connections()))

    def test_selector_menu_has_only_saved_connections_and_modal_cancel_preserves_selection(self):
        first = self.remote()
        second = self.store.save("Local", mode="local")
        page, _ = self.page()
        self.assertEqual(page.connections.count(), 2)
        self.assertEqual(page.connections.currentData(), first["id"])
        page.connections.setCurrentIndex(page.connections.findData(second["id"]))
        page.refresh()
        self.assertEqual(page.connections.currentData(), second["id"])
        self.assertEqual([a.text() for a in page.menu_button.menu().actions()],
                         ["New engine…", "Edit engine…", "Delete engine…"])
        before = deepcopy(self.store.connections())
        seen = []
        def inspect_dialog():
            seen.append(page.editor.isModal() and page.editor.isVisible())
            page.editor.name.setText("Canceled")
            page.editor.reject()
        QTimer.singleShot(0, inspect_dialog)
        page.new_action.trigger()
        self.assertEqual(seen, [True])
        self.assertEqual(self.store.connections(), before)
        self.assertEqual(page.connections.currentData(), second["id"])
        QTimer.singleShot(0, inspect_dialog)
        page.edit_action.trigger()
        self.assertEqual(seen, [True, True])
        self.assertEqual(self.store.connections(), before)

    def test_new_edit_save_and_local_controls(self):
        editor, _ = self.editor()
        self.assertEqual(editor.mode.currentData(), "local")
        self.assertTrue(editor.remote_fields.isHidden())
        self.assertFalse(editor.local_note.isHidden())
        editor.name.setText("Local calculation")
        editor.buttons.button(QDialogButtonBox.Save).click()
        self.assertEqual(editor.result(), QDialog.Accepted)
        saved = editor.saved_connection
        self.assertEqual(saved["base_url"], LOCAL_BASE_URL)
        self.assertNotIn("authcfg", saved)
        edited, _ = self.editor(saved)
        edited.name.setText("Renamed")
        edited._save()
        self.assertEqual(self.store.get(saved["id"])["name"], "Renamed")

    def test_remote_save_encrypted_and_cancel_keeps_saved_key(self):
        editor, _ = self.editor()
        editor.mode.setCurrentIndex(editor.mode.findData("remote"))
        editor.base_url.setText("https://remote.example.test/")
        editor.client_key.setText("private-client-key")
        self.assertEqual(editor.client_key.echoMode(), QLineEdit.Password)
        editor._save()
        saved = editor.saved_connection
        self.assertEqual(saved["base_url"], "https://remote.example.test")
        self.assertEqual(self.credentials.keys[saved["authcfg"]], "private-client-key")
        self.assertEqual(editor.client_key.text(), "")
        self.assert_secret_not_saved("private-client-key")
        edit, _ = self.editor(saved)
        self.assertIn("Saved securely", edit.client_key.placeholderText())
        edit.client_key.setText("canceled-key")
        edit.reject()
        self.assertEqual(self.store.get(saved["id"]), saved)
        self.assertEqual(self.credentials.keys[saved["authcfg"]], "private-client-key")

    def test_missing_key_and_secure_store_failure_do_not_save(self):
        editor, client = self.editor()
        editor.mode.setCurrentIndex(1)
        editor.base_url.setText("https://remote.example.test")
        editor._test()
        editor._save()
        self.assertIn("client key", editor.status.text())
        self.assertEqual(client.requests, [])
        self.assertEqual(self.store.connections(), [])
        editor.client_key.setText("private-key")
        with patch.object(self.credentials, "save", side_effect=ModelError("Unlock QGIS authentication")):
            editor._save()
        self.assertIn("Unlock", editor.status.text())
        self.assertEqual(self.store.connections(), [])
        self.assertEqual(editor.result(), QDialog.Rejected)

    def test_draft_test_never_saves_endpoint_key_or_successful_cache(self):
        saved = self.remote()
        self.store.record_check(saved["id"], "capabilities", capabilities())
        saved = self.store.get(saved["id"])
        editor, client = self.editor(saved)
        editor.base_url.setText("https://different.example.test")
        editor._test()
        self.assertEqual(client.requests, [])
        self.assertIn("client key for this engine URL", editor.status.text())
        editor.client_key.setText("explicit-draft-key")
        editor._test()
        self.assertEqual(client.client_key, "explicit-draft-key")
        self.assertNotIn("authcfg", client.connection)
        self.complete(client)
        self.assertEqual(editor.engines.count(), 1)
        self.assertEqual(self.store.get(saved["id"]), saved)
        editor.reject()
        self.assertEqual(self.store.get(saved["id"]), saved)
        self.assertEqual(self.credentials.saved, [("saved-secret", "Remote engine", saved["authcfg"])])
        self.assertEqual(self.credentials.removed, [])
        self.assert_secret_not_saved("explicit-draft-key")

    def test_unchanged_remote_draft_reuses_auth_reference_only_until_explicit_save(self):
        saved = self.remote()
        editor, client = self.editor(saved)
        editor._test()
        self.assertEqual(client.connection["authcfg"], saved["authcfg"])
        self.assertIsNone(client.client_key)
        self.complete(client)
        self.assertNotIn("engine_profiles", self.store.get(saved["id"]))
        editor._save()
        value = self.store.get(saved["id"])
        self.assertEqual(len(value["engine_profiles"]), 1)
        self.assertEqual(len(self.credentials.saved), 1)
        self.assertEqual(len(available_engines(self.store)), 1)

    def test_edit_and_close_ignore_late_callbacks(self):
        saved = self.remote()
        editor, client = self.editor(saved)
        editor._test()
        late = client.requests[-1][-1]
        editor.base_url.setText("https://other.example.test")
        late({"status": "ok"}, None, {})
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(editor.status.text(), "")
        self.assertTrue(editor.test_button.isEnabled())
        editor.client_key.setText("draft-key")
        editor._test()
        late = client.requests[-1][-1]
        editor.reject()
        late({"status": "ok"}, None, {})
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(self.store.get(saved["id"]), saved)
        self.assertIsNone(client.client_key)

    def test_delete_confirmation_cancel_then_accept_removes_key(self):
        saved = self.remote()
        page, _ = self.page()
        with patch("acalc.api_settings_ui.QMessageBox.question", return_value=QMessageBox.No):
            page.delete_action.trigger()
        self.assertEqual(self.store.get(saved["id"]), saved)
        self.assertEqual(self.credentials.removed, [])
        with patch("acalc.api_settings_ui.QMessageBox.question", return_value=QMessageBox.Yes):
            page.delete_action.trigger()
        self.assertEqual(self.store.connections(), [])
        self.assertEqual(self.credentials.removed, [saved["authcfg"]])
        self.assertFalse(page.test_button.isEnabled())
        self.assertFalse(page.edit_action.isEnabled())

    def test_combined_test_fetches_each_engine_and_displays_real_versions(self):
        saved = self.remote()
        page, client = self.page()
        page.test_button.click()
        self.assertEqual(client.requests[-1][1], "healthz")
        self.assertFalse(page.test_button.isEnabled())
        client.respond({"status": "ok", "version": "SERVICE-VERSION-MUST-NOT-APPEAR"})
        self.assertEqual(client.requests[-1][1], "v1/engines")
        client.respond({"engines": [engine(), engine("iso-pro", default=False, version="9.8.7")]})
        self.assertEqual(client.requests[-1][1], "/v1/engines/iso/capabilities")
        client.respond(capabilities())
        self.assertEqual(client.requests[-1][1], "/v1/engines/iso-pro/capabilities")
        client.respond(capabilities(implementation={"name": "pro", "version": "9.8.7"}))
        self.assertEqual([page.engines.item(i).text() for i in range(2)],
                         ["ISO 9613-2:2024 — iso (engine-iso) 1.2.3", "ISO 9613-2:2024 — iso-pro (engine-iso-pro) 9.8.7"])
        self.assertFalse(page.engines.wordWrap())
        self.assertIn("2 of 2", page.status.text())
        self.assertNotIn("SERVICE-VERSION", page.status.text())
        cached = self.store.get(saved["id"])
        self.assertEqual(cached["engine_profiles"][0]["capabilities"], capabilities())
        engines = available_engines(self.store)
        self.assertEqual(len(engines), 2)
        self.assertEqual(engines[1].calculations_path, "/v1/engines/iso-pro/calculations")
        self.assertEqual(engines[1].capabilities_path, "/v1/engines/iso-pro/capabilities")
        self.assertNotEqual(engines[0].id, engines[1].id)
        self.assertTrue(page.test_button.isEnabled())

    def test_distinct_configured_engines_with_identical_reported_identity_are_distinguished(self):
        self.remote()
        page, client = self.page()
        first, second = engine(), engine("iso-pro", default=False)
        second["engine"] = first["engine"]
        page._test()
        client.respond({"status": "ok"})
        client.respond({"engines": [first, second]})
        client.respond(capabilities())
        client.respond(capabilities())
        self.assertNotEqual(page.engines.item(0).text(), page.engines.item(1).text())
        self.assertIn("iso (engine-iso) 1.2.3", page.engines.item(0).text())
        self.assertIn("iso-pro (engine-iso) 1.2.3", page.engines.item(1).text())
        self.assertEqual(len(available_engines(self.store)), 2)

    def test_partial_failure_unsupported_disabled_and_unknown_versions_are_honest(self):
        self.remote()
        page, client = self.page()
        page._test()
        client.respond({"status": "ok"})
        client.respond({"engines": [engine("broken"), engine("good", default=False, version=None),
                                  engine("future", default=False), engine("disabled", default=False, state="disabled")]})
        client.respond(error=ApiError("Capabilities offline", status=503))
        client.respond(capabilities(implementation={"name": "good", "version": "4.5.6"}))
        client.respond(capabilities(method="Future method"))
        rows = [page.engines.item(i).text() for i in range(4)]
        self.assertIn("unavailable", rows[0])
        self.assertIn("4.5.6", rows[1])
        self.assertIn("unsupported", rows[2])
        self.assertIn("version unknown — disabled", rows[3])
        self.assertEqual(len(available_engines(self.store)), 1)
        self.assertEqual(len(client.requests), 5)
        self.assertIn("Capabilities offline", page.engines.item(0).toolTip())

    def test_unknown_engine_version_never_uses_service_version(self):
        self.remote()
        page, client = self.page()
        page._test()
        self.complete(client, [engine(version=None)], capabilities(implementation={"name": "engine-iso", "version": ""}))
        self.assertIn("version unknown", page.engines.item(0).text())
        self.assertEqual(available_engines(self.store), ())

    def test_legacy_service_falls_back_only_when_engine_list_missing(self):
        self.remote()
        page, client = self.page()
        page._test()
        client.respond({"status": "ok"})
        client.respond(error=ApiError("Not found", status=404))
        self.assertEqual(client.requests[-1][1], "v1/capabilities")
        client.respond(capabilities())
        self.assertIn("test_engine 0.1", page.engines.item(0).text())
        self.assertEqual(available_engines(self.store)[0].calculations_path, "v1/calculations")
        page._test()
        client.respond({"status": "ok"})
        client.respond(error=ApiError("Client key rejected", status=401))
        self.assertEqual(client.requests[-1][1], "v1/engines")
        self.assertIn("Client key rejected", page.status.text())
        self.assertEqual(available_engines(self.store), ())

    def test_nonready_health_and_network_failures_stop_checks_and_release_button(self):
        self.remote()
        page, client = self.page()
        for data, error, message in [(None, ModelError("Connection refused"), "Connection refused"),
                ([], None, "not ready"), ({"status": "ok", "role": "node"}, None, "internal node")]:
            with self.subTest(message=message):
                before = len(client.requests)
                page._test()
                client.respond(data, error)
                self.assertEqual(len(client.requests), before + 1)
                self.assertIn(message, page.status.text())
                self.assertTrue(page.test_button.isEnabled())
        self.assertEqual(available_engines(self.store), ())

    def test_switch_delete_shutdown_and_key_replacement_reject_stale_saved_checks(self):
        saved = self.remote()
        other = self.store.save("Local", mode="local")
        page, client = self.page()
        page._test()
        late = client.requests[-1][-1]
        page.connections.setCurrentIndex(page.connections.findData(other["id"]))
        late({"status": "ok"}, None, {})
        self.assertEqual(len(client.requests), 1)
        page.connections.setCurrentIndex(page.connections.findData(saved["id"]))
        page._test()
        self.store.save(saved["name"], saved["base_url"], saved["id"], mode="remote", client_key="new-key")
        self.complete(client)
        self.assertNotIn("engine_profiles", self.store.get(saved["id"]))
        page._test()
        late = client.requests[-1][-1]
        page.shutdown()
        late({"status": "ok"}, None, {})
        self.assertIsNone(client.client_key)
        self.assertTrue(client.aborted)
        self.assert_secret_not_saved("new-key")

    def test_cross_origin_and_missing_links_do_not_hide_valid_engine(self):
        self.remote()
        page, client = self.page()
        profiles = [engine("bad"), engine("good", default=False)]
        profiles[0]["links"]["calculations"] = "https://different.test/calculations"
        page._test()
        self.complete(client, profiles)
        self.assertIn("unavailable", page.engines.item(0).text())
        self.assertEqual(len(available_engines(self.store)), 1)
        self.assertEqual(len(client.requests), 3)

    def test_local_key_required_opens_remote_editor_with_fixed_url(self):
        page, client = self.page()
        page._test()
        client.respond({"status": "ok", "key_required": True})
        self.assertFalse(page.use_remote_button.isHidden())
        seen = []
        def inspect():
            seen.append((page.editor.mode.currentData(), page.editor.base_url.text()))
            page.editor.reject()
        QTimer.singleShot(0, inspect)
        page.use_remote_button.click()
        self.assertEqual(seen, [("remote", LOCAL_BASE_URL)])
        self.warning.assert_not_called()

    def test_lan_http_draft_and_saved_connections_test_without_warning(self):
        saved = self.remote(url="http://192.0.2.100:8343")
        editor, client = self.editor(saved)
        editor._test()
        self.complete(client)
        editor.reject()
        self.assertEqual(self.store.get(saved["id"]), saved)
        page, client = self.page()
        page._test()
        self.complete(client)
        page._test()
        self.complete(client)
        self.warning.assert_not_called()
        self.assertEqual(len(available_engines(self.store)), 1)

    def test_key_replacement_invalidates_all_cached_profiles_and_endpoint_switch_removes_old_key(self):
        saved = self.remote()
        page, client = self.page()
        page._test()
        self.complete(client)
        updated = self.store.save(saved["name"], saved["base_url"], saved["id"], mode="remote", client_key="new-key")
        self.assertNotIn("engine_profiles", updated)
        self.assertNotIn("capabilities", updated)
        self.assertEqual(updated["authcfg"], saved["authcfg"])
        self.assertNotEqual(updated["revision"], saved["revision"])
        changed = self.store.save(saved["name"], "https://other.test", saved["id"], mode="remote", client_key="other-key")
        self.assertIn(saved["authcfg"], self.credentials.removed)
        local = self.store.save(saved["name"], LOCAL_BASE_URL, saved["id"], mode="local")
        self.assertIn(changed["authcfg"], self.credentials.removed)
        self.assertNotIn("authcfg", local)

    def test_profile_settings_leave_project_bytes_unchanged(self):
        path = self.directory.name + "/shared.gpkg"
        model = ModelStore.create(path, "Shared", QgsCoordinateReferenceSystem("EPSG:26913").toWkt())
        before = Path(path).read_bytes()
        self.remote()
        page, client = self.page()
        page._test()
        self.complete(client)
        self.assertEqual(Path(path).read_bytes(), before)
        self.assert_secret_not_saved("saved-secret")
