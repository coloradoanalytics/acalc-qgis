# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Project log window and metadata editing through the reorganized page."""

import json

from acalc.plugin import AcalcPlugin
from tests.test_model import APP, FakeIface, ModelFixture


class ProjectPageTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.open_project(self.path)

    def tearDown(self):
        self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def test_log_window_retains_history_and_updates_while_open_or_hidden(self):
        dock = self.plugin.dock
        self.plugin.message("First message\nSecond line <literal>")
        dock.view_log_button.click()
        window = dock._log_dialog
        self.assertTrue(window.isVisible())
        self.assertTrue(dock._log_text.isReadOnly())
        self.assertEqual(dock._log_text.toPlainText(), dock.project_log.toPlainText())
        self.plugin.message("New message while viewing log")
        self.assertIn("First message\nSecond line <literal>", dock._log_text.toPlainText())
        self.assertTrue(dock._log_text.toPlainText().endswith("New message while viewing log"))
        window.close()
        self.plugin.message("New message after closing log")
        dock.view_log_button.click()
        self.assertIs(dock._log_dialog, window)
        self.assertEqual(dock._log_text.toPlainText(), dock.project_log.toPlainText())
        self.assertEqual(dock.status.text(), "New message after closing log")
        window.close()

    def test_moved_metadata_fields_save_and_lock_during_calculation(self):
        dock = self.plugin.dock
        dock.project_name.setText("Updated project")
        dock.vertical_datum.setText("Local benchmark")
        dock.extra_metadata.setPlainText('{"client": "Test client"}')
        dock.save_metadata_button.click()
        saved = self.store.metadata()
        self.assertEqual(saved["name"], "Updated project")
        self.assertEqual(saved["vertical_datum"], "Local benchmark")
        self.assertEqual(json.loads(saved["metadata_json"]), {"client": "Test client"})
        dock.set_calculating(True)
        self.assertFalse(dock.extra_metadata.isEnabled())
        self.assertFalse(dock.save_metadata_button.isEnabled())
        self.assertTrue(dock.view_log_button.isEnabled())
        dock.set_calculating(False)
        self.assertTrue(dock.extra_metadata.isEnabled())
