# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""QGIS API selection, edit-buffer protection and persisted result display."""

import json
import time
from unittest.mock import patch

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import QAbstractItemView, QApplication, QMessageBox
from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

from acalc.api_connections import ApiConnectionStore, SETTINGS_KEY, engine_identity
from acalc.calculation_settings import ENGINE_ID, default_settings
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError, new_id, definition
from acalc.spectra import spectrum_definition
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


CAPABILITIES = {
    "api_version": "1.0", "implementation": {"name": "Test ISO", "version": "0.1.0"},
    "method": "ISO 9613-2:2024", "atmospheric_absorption": "ISO 9613-1:1993",
    "frequencies_hz": [63,125,250,500,1000,2000,4000,8000],
    "source_kinds": ["point", "area"], "receiver_kinds": ["point", "area"],
    "max_reflect_order": 3, "verified_conformity": False, "limitations": ["Test fixture"],
}


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for the calculation API")


def add_api(name="Local service", base_url="http://127.0.0.1:8343"):
    connections = ApiConnectionStore()
    connection = connections.save(name, base_url)
    connections.record_check(connection["id"], "capabilities", CAPABILITIES)
    return connections.get(connection["id"])


class CalculationUITests(ModelFixture):
    def setUp(self):
        super().setUp()
        QSettings().remove(SETTINGS_KEY)
        self.connection = add_api()
        self.engine_id = engine_identity(self.connection)
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.config_id = self.store.variants()[0]["variant_id"]
        self.store.save_variant(self.config_id, "Calculation", definition())
        self.plugin.open_project(self.path)

    def tearDown(self):
        if self.plugin.dock:
            self.plugin.unload()
        self.iface.window.close()
        QSettings().remove(SETTINGS_KEY)
        APP.processEvents()
        super().tearDown()

    def test_multiple_apis_show_named_engine_and_remember_selection(self):
        second = add_api("Remote service", "https://example.invalid/solver")
        page = self.plugin.dock.calculation_page
        page.refresh()
        self.assertEqual(page.engine.count(), 2)
        self.assertIn("Local service", page.engine.itemText(0))
        self.assertIn("ISO 9613-2:2024", page.engine.itemText(0))
        selected = engine_identity(second)
        page.select_engine(selected)
        self.plugin.dock.show_view("project")
        self.plugin.dock.show_view("calculation")
        self.assertEqual(page.engine.currentData(), selected)
        self.plugin.close_project()
        self.plugin.open_project(self.path)
        self.assertEqual(page.engine.currentData(), selected)
        self.plugin.open_calculation_dialog()
        dialog = self.plugin.calculation_dialog
        self.assertEqual(dialog.engine.currentData(), selected)
        self.assertIn("Remote service", dialog.engine.currentText())
        self.assertEqual(set(dialog.panels), {self.engine_id, selected})

    def test_calculation_page_has_export_without_api_controls(self):
        from acalc.export_dialog import ExportDialog
        page = self.plugin.dock.calculation_page
        self.assertTrue(page.engine.isHidden())
        self.assertFalse(hasattr(page, "api_settings_button"))
        self.assertTrue(page.export_button.isEnabled())
        with patch.object(ExportDialog, "exec_", return_value=0) as opened:
            page.export_button.click()
        opened.assert_called_once()
        page.set_calculating(True)
        self.assertFalse(page.export_button.isEnabled())
        page.set_calculating(False)
        self.plugin.close_project()
        self.assertFalse(page.export_button.isEnabled())

    def test_long_error_does_not_expand_dock_and_can_be_copied_in_full(self):
        dock = self.plugin.dock
        dock.show_view("calculation")
        dock.setFloating(True)
        dock.resize(760, 800)
        dock.show()
        APP.processEvents()
        before = dock.minimumSizeHint().width()
        message = "Calculation failed: " + "very_long_path_without_breaks_" * 200 + "\nSecond line <details>"
        self.plugin.message(message, error=True)
        APP.processEvents()
        self.assertLessEqual(dock.minimumSizeHint().width(), before)
        self.assertEqual(dock.calculation_page.messages.toPlainText(), message)
        dock.calculation_page.copy_messages_button.click()
        self.assertEqual(QApplication.clipboard().text(), message)
        dock.close()
        dock.show()
        dock.resize(760, 800)
        APP.processEvents()
        self.assertLessEqual(dock.width(), 760)

    def test_variant_tree_can_shrink_to_give_messages_more_space(self):
        dock = self.plugin.dock
        dock.show_view("calculation")
        dock.setFloating(True)
        dock.resize(760, 850)
        dock.show()
        APP.processEvents()
        page = dock.calculation_page
        self.assertFalse(page.copy_messages_button.isEnabled())
        page.splitter.setSizes([400, 100])
        APP.processEvents()
        initial_tree, initial_messages = page.splitter.sizes()
        page.splitter.setSizes([150, 350])
        APP.processEvents()
        tree, messages = page.splitter.sizes()
        self.assertLess(tree, initial_tree)
        self.assertGreater(messages, initial_messages)
        self.plugin.message("First calculation message")
        self.plugin.message("Second calculation message")
        page.copy_messages_button.click()
        self.assertEqual(QApplication.clipboard().text(), "First calculation message\nSecond calculation message")

    def test_export_dialog_variant_format_selection_and_no_api_dependency(self):
        from acalc.export_dialog import ExportDialog
        from acalc.model_export import export_variant
        from pathlib import Path
        QSettings().remove(SETTINGS_KEY)
        self.plugin.dock.api_connections_changed()
        other = self.store.add_variant("Export this variant", definition())
        dialog = ExportDialog(self.plugin, self.plugin.dock)
        self.assertEqual(dialog.variant.currentData(), self.config_id)
        dialog.variant.setCurrentIndex(dialog.variant.findData(other))
        dialog.format.setCurrentIndex(dialog.format.findData("shp"))
        dialog.destination.setText(str(Path(self.directory.name) / "variant-export"))
        with patch("acalc.export_dialog.export_variant", wraps=export_variant) as exported:
            dialog.export_button.click()
        self.assertEqual(exported.call_args.args[1], other)
        self.assertEqual(exported.call_args.args[3], "shp")
        self.assertIn("Exported to", dialog.status.text())
        dialog.close()

    def test_settings_available_without_model_and_no_binary_fallback(self):
        self.plugin.close_project()
        QSettings().remove(SETTINGS_KEY)
        self.plugin.dock.api_connections_changed()
        self.plugin.dock.show_view("settings")
        self.assertIs(self.plugin.dock.stack.currentWidget(), self.plugin.dock.settings_scroll)
        self.assertTrue(self.plugin.dock.settings_page.isEnabled())
        self.plugin.open_project(self.path)
        self.assertFalse(self.plugin.dock.calculate_button.isEnabled())
        with self.assertRaisesRegex(ModelError, "API|Settings|unavailable"):
            self.plugin.calculate()
        self.assertFalse(self.plugin.engine_runner.active)

    def test_unavailable_api_does_not_select_a_different_backend(self):
        add_api("Other service", "http://127.0.0.1:9343")
        page = self.plugin.dock.calculation_page
        page.refresh()
        page.select_engine(self.engine_id)
        ApiConnectionStore().record_check(self.connection["id"], "health", error="Offline")
        page.refresh()
        self.assertEqual(page.engine.count(), 1)
        self.assertIsNone(page.engine.currentData())
        self.assertTrue(page.calculate_button.isEnabled())
        self.plugin.open_calculation_dialog()
        dialog = self.plugin.calculation_dialog
        self.assertIsNone(dialog.engine.currentData())
        dialog.engine.setCurrentIndex(0)
        self.assertIsNotNone(dialog.engine.currentData())

    def test_api_v1_back_calculation_is_explicitly_unavailable(self):
        self.assertFalse(self.plugin.dock.calculation_page.back_calculate_button.isEnabled())
        with self.assertRaisesRegex(ModelError, "does not provide"):
            self.plugin.start_back_calculation(self.config_id, default_settings())
        self.assertFalse(self.plugin.engine_runner.active)

    def enable_back_calculation(self, connection=None):
        connection = connection or self.connection
        capabilities = dict(CAPABILITIES, features=["source_receiver_transfers"])
        ApiConnectionStore().record_check(connection["id"], "capabilities", capabilities)
        self.plugin.dock.calculation_page.refresh()
        return engine_identity(connection)

    def test_back_calculation_selects_capable_api_and_passes_it_to_runner(self):
        second = add_api("Fitting service", "http://127.0.0.1:9343")
        selected = self.enable_back_calculation(second)
        page = self.plugin.dock.calculation_page
        self.assertTrue(page.back_calculate_button.isEnabled())
        self.plugin.open_back_calculation_dialog()
        dialog = self.plugin.back_calculation_dialog
        self.assertEqual(dialog.engine.count(), 1)
        self.assertIsNone(dialog.engine.currentData())
        self.assertFalse(dialog.evaluate_button.isEnabled())
        dialog.engine.setCurrentIndex(0)
        self.assertEqual(dialog.engine.currentData(), selected)
        self.assertTrue(dialog.evaluate_button.isEnabled())
        self.assertFalse(dialog.settings_editor.pruning_check.isChecked())
        self.assertFalse(dialog.settings_editor.pruning_check.isEnabled())
        self.assertEqual(dialog.settings_editor.settings()["ground_method"], "general")
        self.assertFalse(dialog.settings_editor.ground_method.isEnabled())
        self.assertEqual(dialog.settings_editor.settings()["area_absolute_tolerance"], 0)
        self.assertFalse(dialog.settings_editor.numerical_fields["area_absolute_tolerance"].isEnabled())
        self.assertEqual(dialog.table.editTriggers(), QAbstractItemView.NoEditTriggers)
        with patch.object(self.plugin, "start_back_calculation", return_value=True) as start:
            dialog.evaluate()
        self.assertEqual(start.call_args.args[0], self.config_id)
        self.assertIsNone(start.call_args.args[1]["pruning_strategy"])
        self.assertEqual(start.call_args.kwargs["engine_id"], selected)
        context = dict(relationships=True, store=self.plugin.store, variant_id=self.config_id, engine=selected)
        dialog.progress_changed(context, dict(phase="propagation", percent=37.5,
                                             total_task_points=8, completed_task_points=3))
        self.assertEqual(dialog.progress.value(), 38)
        self.assertIn("38%", dialog.status.text())
        dialog.stopped(context)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertTrue(dialog.evaluate_button.isEnabled())

    def test_back_calculation_preserves_synchronous_failure_status(self):
        self.enable_back_calculation()
        self.plugin.open_back_calculation_dialog()
        dialog = self.plugin.back_calculation_dialog
        context = dict(relationships=True, store=self.plugin.store, variant_id=self.config_id, engine=self.engine_id)
        def fail_immediately(*args, **kwargs):
            self.plugin.engine_runner.failed.emit(context, "Immediate API failure")
            return True
        with patch.object(self.plugin, "start_back_calculation", side_effect=fail_immediately):
            dialog.evaluate()
        self.assertFalse(dialog._running)
        self.assertTrue(dialog.evaluate_button.isEnabled())
        self.assertEqual(dialog.status.text(), "Evaluation failed: Immediate API failure")

    def test_back_calculation_long_limitations_scroll_without_expanding_dialog(self):
        self.enable_back_calculation()
        self.plugin.open_back_calculation_dialog()
        dialog = self.plugin.back_calculation_dialog
        dialog.status.setText("Engine qualification with supporting details. " * 1000)
        APP.processEvents()
        self.assertLessEqual(dialog.status_scroll.height(), 150)
        self.assertGreater(dialog.status_scroll.verticalScrollBar().maximum(), 0)
        self.assertLess(dialog.minimumSizeHint().height(), 750)
        self.assertGreater(dialog.table.height(), 70)

    def test_back_calculation_fit_is_invalidated_by_new_api_settings(self):
        self.enable_back_calculation()
        self.plugin.open_back_calculation_dialog()
        dialog = self.plugin.back_calculation_dialog
        editor = dialog.settings_editor
        changes = [lambda: editor.meteorology.setCurrentIndex(1),
                   lambda: editor.numerical_fields["area_relative_tolerance"].setText("0.02"),
                   lambda: editor.optional_fields["area_max_extent_m"].setText("20"),
                   lambda: editor.fields["temperature_c"].setValue(22)]
        for change in changes:
            dialog.fit = {"proposals": {"spectrum": [60] * 8}}
            dialog.relationships = {"spectra": []}
            dialog.set_running(False)
            self.assertTrue(dialog.apply_button.isEnabled())
            change()
            self.assertIsNone(dialog.fit)
            self.assertIsNone(dialog.relationships)
            self.assertFalse(dialog.apply_button.isEnabled())
        self.plugin.dock.api_connections_changed()
        self.assertIsNone(self.plugin.back_calculation_dialog)

    def test_back_calculation_reviews_fit_before_applying_to_catalog(self):
        from acalc.api_model import _revision
        self.enable_back_calculation()
        spectrum = self.store.add_spectrum("Adjustable fan", spectrum_definition(
            "sound_power", [None] * 8, back_calculation="adjustable", initial_values=[50] * 8))
        project = self.store.metadata()
        revision_tables = ["acalc_project", "acalc_spectra"]
        with self.store.connection(readonly=True) as db:
            revision = _revision(db, revision_tables)
        relationships = dict(
            project_id=project["project_id"], project_modified_at=project["modified_at"],
            revision_tables=revision_tables, model_revision=revision,
            spectra=[dict(id=spectrum, mode="adjustable", current=[None] * 8, initial=[50] * 8)],
            sources=[dict(id="fan", spectrum_id=spectrum)],
            measurements=[dict(observed=[40] * 8, transfer_db={"fan": [-20] * 8})],
            limitations=["Engine qualification for this fit"])
        self.plugin.open_back_calculation_dialog()
        dialog = self.plugin.back_calculation_dialog
        dialog.set_running(True)
        context = dict(relationships=True, store=self.plugin.store, variant_id=self.config_id, engine=self.engine_id)
        dialog.succeeded(context, relationships)
        self.assertTrue(dialog.apply_button.isEnabled(), dialog.status.text())
        self.assertIn("Engine qualification for this fit", dialog.status.text())
        self.assertEqual(dialog.table.rowCount(), 3)
        self.assertEqual(dialog.table.item(2, 0).text(), "Adjustable fan")
        self.assertEqual(dialog.table.item(2, 2).text(), "60.0")
        self.assertEqual(self.store.spectra("sound_power")[0]["spectrum"]["values"], [None] * 8)
        dialog.apply_button.click()
        saved = self.store.spectra("sound_power")[0]["spectrum"]
        self.assertEqual(saved["values"], [60] * 8)
        self.assertEqual(saved["initial_values"], [50] * 8)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertIn("Fitted sound power saved", dialog.status.text())

    def test_back_calculation_failed_fit_cannot_be_applied(self):
        self.enable_back_calculation()
        self.plugin.open_back_calculation_dialog()
        dialog = self.plugin.back_calculation_dialog
        dialog.set_running(True)
        context = dict(relationships=True, store=self.plugin.store, variant_id=self.config_id, engine=self.engine_id)
        # A fit result must not become applicable if its display cannot be built.
        with patch("acalc.back_calculation_dialog.fit_spectra", return_value={"proposals": {"missing": [60] * 8}}):
            dialog.succeeded(context, {"spectra": []})
        self.assertIn("Fit failed", dialog.status.text())
        self.assertIsNone(dialog.fit)
        self.assertIsNone(dialog.relationships)
        self.assertFalse(dialog.apply_button.isEnabled())

    def test_back_calculation_preflight_saves_buffers_and_restores_locks_on_failure(self):
        self.enable_back_calculation()
        record = self.store.add_layer("measurement", "Survey")
        self.plugin.reload_layers()
        layer = self.plugin.layers[record["layer_id"]]
        add_feature(layer, LAYER_TYPES["measurement"])
        settings = dict(default_settings(), temperature_c=23, pruning_strategy="dba")
        original = self.store.calculation()
        with patch("acalc.plugin.QMessageBox.question", return_value=QMessageBox.Cancel), \
                patch.object(self.plugin.engine_runner, "start_relationships") as start:
            self.assertFalse(self.plugin.start_back_calculation(self.config_id, settings, self.engine_id))
        start.assert_not_called()
        self.assertEqual(self.store.calculation(), original)
        self.assertTrue(layer.isModified())

        def fail_start(*args, **kwargs):
            self.assertFalse(layer.isEditable())
            self.assertTrue(layer.readOnly())
            self.assertEqual(kwargs["engine_id"], self.engine_id)
            raise ModelError("Fixture failure")

        with patch("acalc.plugin.QMessageBox.question", return_value=QMessageBox.Save), \
                patch.object(self.plugin.engine_runner, "start_relationships", side_effect=fail_start):
            with self.assertRaisesRegex(ModelError, "Fixture failure"):
                self.plugin.start_back_calculation(self.config_id, settings, self.engine_id)
        self.assertFalse(layer.isModified())
        self.assertFalse(layer.readOnly())
        self.assertEqual(self.store.calculation()["settings"]["temperature_c"], 23)
        self.assertIsNone(self.store.calculation()["settings"]["pruning_strategy"])
        self.assertFalse(self.plugin.engine_runner.active)
        self.assertTrue(self.plugin.dock.calculation_page.back_calculate_button.isEnabled())

    def test_applying_fit_rejects_unsaved_edits_in_untagged_measurement_provider(self):
        record = self.store.add_layer("measurement", "Survey")
        duplicate = QgsVectorLayer(f'{self.path}|layername={record["table_name"]}', "Other Survey view", "ogr")
        QgsProject.instance().addMapLayer(duplicate)
        add_feature(duplicate, LAYER_TYPES["measurement"])
        with patch.object(self.plugin.store, "apply_back_calculation") as apply:
            with self.assertRaisesRegex(ModelError, "unsaved layer edits"):
                self.plugin.apply_back_calculation({}, {})
        apply.assert_not_called()
        self.assertTrue(duplicate.isModified())

    def test_selected_api_is_passed_to_runner_and_settings_are_shared(self):
        settings = default_settings()
        settings["temperature_c"] = 23
        with patch.object(self.plugin.engine_runner, "start_batch") as start:
            self.assertTrue(self.plugin.calculate(settings=settings, engine_id=self.engine_id))
        self.assertEqual(start.call_args.kwargs["engine_id"], self.engine_id)
        self.assertEqual(self.store.calculation()["settings"]["temperature_c"], 23)
        self.plugin.calculation_busy_changed(False)

    def test_local_compatibility_message_does_not_claim_unsupported_effects(self):
        context = {"store": self.plugin.store, "model_path": self.plugin.store.path,
                   "variant_id": self.config_id, "validate": True}
        self.plugin.calculation_succeeded(context, {"source_count": 1, "receiver_count": 2,
                                                   "area_receiver_count": 3})
        message = self.plugin.dock.status.text()
        self.assertIn("compatibility", message)
        self.assertIn("3 receiver areas", message)
        self.assertNotIn("foliage", message)

    def test_http_calculation_saves_results_and_displays_named_backend(self):
        from tests.test_api_runner import FakeCalculationAPI
        with FakeCalculationAPI() as api, \
                patch("acalc.api_connections.QgisCredentialStore.save", return_value="testcfg"), \
                patch("acalc.api_connections.QgisCredentialStore.load", return_value="ui-test-client-key"):
            api.client_key = "ui-test-client-key"
            connections = ApiConnectionStore()
            connections.save("Local service", api.url, self.connection["id"],
                             mode="remote", client_key="ui-test-client-key")
            connections.record_check(self.connection["id"], "capabilities", api.capabilities)
            selected = engine_identity(connections.get(self.connection["id"]))
            self.plugin.dock.calculation_page.refresh()
            self.plugin.dock.calculation_page.select_engine(selected)
            source = self.store.add_layer("point_source", "Fan")
            receiver = self.store.add_layer("point_receiver", "Porch")
            power = self.store.add_spectrum("Power", spectrum_definition("sound_power", [90] * 8))
            self.store.save_variant(self.config_id, "Calculation", definition([source["layer_id"], receiver["layer_id"]]))
            self.plugin.reload_layers()
            for record, x in ((source, 0), (receiver, 100)):
                layer = self.plugin.layers[record["layer_id"]]
                layer.startEditing()
                feature = QgsFeature(layer.fields())
                feature["object_id"], feature["name"] = new_id(), record["name"]
                if record is source:
                    feature["spectrum_id"] = power
                feature.setGeometry(QgsGeometry.fromWkt(f"POINT ZM ({x} 0 12 1.5)"))
                self.assertTrue(layer.addFeature(feature))
                self.plugin.commit_layer(layer)
            self.plugin.calculate()
            self.assertTrue(self.plugin.engine_runner.active)
            wait_until(lambda: not self.plugin.engine_runner.active)
            run = self.store.latest_calculation_run(self.config_id)
            self.assertIsNotNone(run, self.plugin.dock.status.text())
            self.assertFalse(run["stale"])
            self.assertEqual(run["metadata"]["backend"]["api_name"], "Local service")
            self.assertEqual(self.plugin.dock.results_page.table.rowCount(), 1)
            self.assertIn("Results match", self.plugin.dock.results_page.summary.text())
            self.assertIn("Local service", self.plugin.dock.results_page.summary.text())
            self.assertTrue(all(not layer.readOnly() for layer in self.plugin.layers.values()))
            posts = [body for method, _, body in api.requests if method == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["receivers"][0]["ground_ref"]["position"], [100, 0, 12])
            self.assertNotIn(self.store.path, json.dumps(posts[0]))

    def test_cancel_preserves_unsaved_changes_then_clean_buffers_are_saved_and_locked(self):
        record = self.store.add_layer("point_receiver", "Receivers")
        self.plugin.reload_layers()
        layer = self.plugin.layers[record["layer_id"]]
        add_feature(layer, LAYER_TYPES["point_receiver"])
        with patch('acalc.plugin.QMessageBox.question', return_value=QMessageBox.Cancel), \
                patch.object(self.plugin.engine_runner, 'start_batch') as start:
            self.assertFalse(self.plugin.calculate())
        start.assert_not_called()
        self.assertTrue(layer.isModified())
        self.plugin.commit_layer(layer)
        self.plugin.dock.project_name.setText("Unsaved name")
        with self.assertRaisesRegex(ModelError, "project detail changes"):
            self.plugin.calculate()
        self.plugin.dock.refresh_project()
        self.assertTrue(layer.startEditing())
        def started(*args, **kwargs):
            self.plugin.engine_runner._busy = True
            self.plugin.engine_runner.busyChanged.emit(True)

        with patch.object(self.plugin.engine_runner, "start_batch", side_effect=started), \
                patch('acalc.plugin.QMessageBox.question', return_value=QMessageBox.Save) as question:
            self.assertTrue(self.plugin.calculate(validate=True))
            question.assert_called_once()
            self.assertIn(layer.name(), question.call_args.args[2])
            self.assertFalse(layer.isEditable())
            self.assertTrue(layer.readOnly())
            self.assertFalse(self.plugin.dock.calculate_button.isEnabled())
            self.assertFalse(self.plugin.dock.members.isEnabled())
            with self.assertRaisesRegex(ModelError, "Cancel the active calculation"):
                self.plugin.close_project()
        self.plugin.engine_runner._busy = False
        self.plugin.engine_runner.busyChanged.emit(False)
        self.assertFalse(layer.readOnly())
        self.assertTrue(self.plugin.dock.calculate_button.isEnabled())

    def test_save_all_layers_persists_every_buffer_before_starting(self):
        records = [self.store.add_layer('point_receiver', name) for name in ('North', 'South')]
        self.plugin.reload_layers()
        layers = [self.plugin.layers[record['layer_id']] for record in records]
        for layer in layers:
            add_feature(layer, LAYER_TYPES['point_receiver'], layer.name() + ' receiver')
        unrelated = QgsVectorLayer('Point?crs=EPSG:26913', 'Unrelated sketch', 'memory')
        QgsProject.instance().addMapLayer(unrelated)
        self.assertTrue(unrelated.startEditing())
        feature = QgsFeature(unrelated.fields())
        feature.setGeometry(QgsGeometry.fromWkt('POINT (0 0)'))
        self.assertTrue(unrelated.addFeature(feature))

        def check_saved_before_start(*args, **kwargs):
            for record, layer in zip(records, layers):
                self.assertFalse(layer.isEditable())
                self.assertFalse(layer.isModified())
                self.assertTrue(layer.readOnly())
                with self.store.connection() as db:
                    names = [row[0] for row in db.execute(f'SELECT name FROM "{record["table_name"]}"')]
                self.assertEqual(names, [layer.name() + ' receiver'])

        with patch('acalc.plugin.QMessageBox.question', return_value=QMessageBox.Save) as question, \
                patch.object(self.plugin.engine_runner, 'start_batch', side_effect=check_saved_before_start) as start:
            self.assertTrue(self.plugin.calculate())
        question.assert_called_once()
        self.assertIs(question.call_args.args[0], self.plugin.dock)
        self.assertEqual(question.call_args.args[1], 'Save layers before calculating')
        for layer in layers:
            self.assertIn(layer.name(), question.call_args.args[2])
        self.assertNotIn(unrelated.name(), question.call_args.args[2])
        self.assertEqual(question.call_args.args[3:], (QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Save))
        start.assert_called_once()
        self.assertTrue(unrelated.isModified())
        self.assertTrue(unrelated.isEditable())
        self.plugin.calculation_busy_changed(False)

    def test_untagged_duplicate_layer_is_validated_and_saved(self):
        record = self.store.add_layer('point_receiver', 'Receivers')
        self.plugin.reload_layers()
        duplicate = QgsVectorLayer(f'{self.path}|layername={record["table_name"]}', 'Duplicate receivers', 'ogr')
        self.assertTrue(duplicate.isValid())
        QgsProject.instance().addMapLayer(duplicate)
        self.assertFalse(duplicate.customProperty('acalc/feature_type'))
        feature = add_feature(duplicate, LAYER_TYPES['point_receiver'], '')
        with patch('acalc.plugin.QMessageBox.question', return_value=QMessageBox.Save), \
                patch.object(self.plugin.engine_runner, 'start_batch') as start:
            with self.assertRaisesRegex(ModelError, 'Duplicate receivers'):
                self.plugin.calculate()
            start.assert_not_called()
            self.assertTrue(duplicate.isModified())
            self.assertTrue(duplicate.changeAttributeValue(feature.id(), duplicate.fields().indexFromName('name'), 'Saved receiver'))
            self.assertTrue(self.plugin.calculate())
            start.assert_called_once()
        self.assertFalse(duplicate.isEditable())
        with self.store.connection() as db:
            self.assertEqual(db.execute(f'SELECT name FROM "{record["table_name"]}"').fetchone()[0], 'Saved receiver')
        self.plugin.calculation_busy_changed(False)

    def test_layer_save_failure_preserves_buffer_and_parameters_without_starting(self):
        record = self.store.add_layer('point_receiver', 'Receivers with failed save')
        self.plugin.reload_layers()
        layer = self.plugin.layers[record['layer_id']]
        add_feature(layer, LAYER_TYPES['point_receiver'])
        settings = default_settings()
        settings['temperature_c'] = 24
        original = self.store.calculation()
        with patch('acalc.plugin.QMessageBox.question', return_value=QMessageBox.Save), \
                patch.object(layer, 'commitChanges', return_value=False), \
                patch.object(layer, 'commitErrors', return_value=['Provider rejected the write']), \
                patch.object(self.plugin.engine_runner, 'start_batch') as start:
            with self.assertRaisesRegex(ModelError, 'Receivers with failed save') as raised:
                self.plugin.calculate(settings=settings, engine_id=self.engine_id)
        self.assertIn('Provider rejected the write', str(raised.exception))
        start.assert_not_called()
        self.assertTrue(layer.isModified())
        self.assertTrue(layer.isEditable())
        self.assertFalse(layer.readOnly())
        self.assertEqual(self.store.calculation(), original)

    def test_results_use_stable_receiver_ids_show_both_broadbands_and_outdated_status(self):
        run_id, layer_id, object_id = new_id(), new_id(), new_id()
        project = self.store.metadata()
        with self.store.connection() as db:
            db.execute("""INSERT INTO acalc_calculation_runs VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,'[]','{}')""",
                (run_id, project["project_id"], self.config_id, "Calculation", ENGINE_ID, "test", "ISO 9613-2:2024",
                 "2026-09-18T18:00:00Z", "2026-09-18T18:00:01Z", "f" * 64, project["modified_at"],
                 json.dumps(default_settings()), 1, 1, 1))
            db.execute("INSERT INTO acalc_receiver_results VALUES (?,?,?,?,?,?,?,'{}')",
                       (run_id, layer_id, object_id, "Porch", json.dumps([10, 20, 30, 40, 50, 40, 30, None]), 51, 50))
            db.execute("UPDATE acalc_calculation_runs SET metadata_json=?, warnings_json=? WHERE run_id=?",
                       (json.dumps({"building_count": 2, "barrier_count": 3, "api_job": {"progress": {"path_counts": {"checked": 12345, "pruned": 678}}}}),
                        json.dumps(["Detailed run warning " + "with supporting information. " * 40]), run_id))
        page = self.plugin.dock.results_page
        page.refresh(self.config_id)
        self.assertEqual(page.table.rowCount(), 1)
        self.assertEqual(page.table.item(0, 0).data(Qt.UserRole), (layer_id, object_id))
        self.assertEqual(page.table.item(0, 1).text(), "50.0")
        self.assertEqual(page.table.item(0, 2).text(), "10.0")
        self.assertEqual(page.table.item(0, 9).text(), "−∞")
        self.assertIn("match the saved model revision", page.summary.text())
        self.assertIn("1 sources", page.summary.text())
        self.assertIn("2 buildings", page.summary.text())
        self.assertIn("3 barriers", page.summary.text())
        self.assertNotIn("Detailed run warning", page.summary.text())
        self.assertTrue(page.details_button.isEnabled())
        page.details_button.click()
        self.assertTrue(page._details_dialog.isVisible())
        self.assertTrue(page._details_text.isReadOnly())
        self.assertIn("Detailed run warning", page._details_text.toPlainText())
        # The stored run used default settings, which have pruning switched off.
        self.assertIn("Calculations checked: 12,345\n", page._details_text.toPlainText())
        self.assertIn("must not be compared", page._details_text.toPlainText())
        self.assertNotIn("Pruned", page._details_text.toPlainText())
        page._details_dialog.close()
        # Authored counts stay stored; calculation summaries use the submitted
        # components and include the Source Building's obstacle.
        with self.store.connection() as db:
            metadata = json.loads(db.execute("SELECT metadata_json FROM acalc_calculation_runs WHERE run_id=?",
                                             (run_id,)).fetchone()[0])
            metadata.update(source_building_count=1, component_source_count=3, solid_building_count=3)
            db.execute("UPDATE acalc_calculation_runs SET source_count=2,metadata_json=? WHERE run_id=?",
                       (json.dumps(metadata), run_id))
        page.refresh()
        self.assertIn("3 sources", page.summary.text())
        self.assertIn("3 buildings", page.summary.text())
        self.assertIn("3 component sources", page._run_details)
        with self.store.connection() as db:
            db.execute("UPDATE acalc_project SET modified_at='2026-09-19T00:00:00Z'")
        page.refresh()
        self.assertIn("OUT OF DATE", page.summary.text())
        self.store.add_variant("Other")
        self.store.delete_variant(self.config_id)
        page.refresh(self.config_id)
        self.assertIn("deleted variant", page.variant.currentText())
        self.assertEqual(page.table.item(0, 0).text(), "Porch")

    def test_source_building_calculation_counts_survive_completion_and_reopen(self):
        from tests.test_api_model import ApiModelTests, extended_capabilities
        from tests.test_api_runner import FakeCalculationAPI
        from acalc.storage import ModelStore

        scene = ApiModelTests()
        scene.setUp()
        self.addCleanup(scene.tearDown)
        scene.scene()  # One ordinary point source and receiver.
        footprint = [(0, 10, 12, 5), (4, 10, 12, 5), (4, 14, 12, 5),
                     (0, 14, 12, 5), (0, 10, 12, 5)]
        scene.feature("building", footprint)
        components = [dict(id=new_id(), name=name, face="roof", vertex_count=4,
                           spectrum_id=scene.spectrum_id, frame="building_local", x_m=x, y_m=1,
                           type="point", mounting="surface", distance_m=0)
                      for name, x in (("Fan A", 1), ("Fan B", 2))]
        scene.feature("source_building", footprint, name="Plant", face_sources=json.dumps(components))
        with FakeCalculationAPI() as api, \
                patch("acalc.api_connections.QgisCredentialStore.save", return_value="testcfg"), \
                patch("acalc.api_connections.QgisCredentialStore.load", return_value="ui-test-client-key"):
            api.capabilities.update(extended_capabilities())
            connections = ApiConnectionStore()
            connections.save("Count service", api.url, self.connection["id"],
                             mode="remote", client_key="ui-test-client-key")
            connections.record_check(self.connection["id"], "capabilities", api.capabilities)
            self.plugin.open_project(scene.store.path)
            self.plugin.open_calculation_dialog()
            dialog = self.plugin.calculation_dialog
            running = []
            self.plugin.engine_runner.progressChanged.connect(
                lambda context, value: running.append(dialog.model_summary.text())
                if context.get("model_summary") else None)
            dialog.start_button.click()
            wait_until(lambda: not self.plugin.engine_runner.active)
            self.assertIn("Calculation complete", dialog.status.text())
            self.assertTrue(running)
            for text in running + [dialog.model_summary.text()]:
                self.assertIn("3 sources", text)
                self.assertIn("2 buildings", text)
                self.assertIn("3 source–receiver pairs", text)
            run = scene.store.latest_calculation_run(scene.variant_id)
            self.assertEqual(run["source_count"], 2)  # Authored objects remain distinct.
            self.assertEqual(run["metadata"]["building_count"], 1)
            submitted = next(body for method, _, body in api.requests if method == "POST")
            self.assertEqual((len(submitted["sources"]), len(submitted["buildings"])), (3, 2))
            contributions = scene.store.receiver_results(scene.variant_id)[0]["metadata"]["sources"]
            self.assertEqual(len(contributions), 3)
            for reopen in (False, True):
                if reopen:
                    self.plugin.close_project()
                    self.plugin.open_project(scene.store.path)
                    self.assertEqual(ModelStore.open(scene.store.path).latest_calculation_run(scene.variant_id)["source_count"], 2)
                page = self.plugin.dock.results_page
                page.refresh(scene.variant_id)
                self.assertIn("3 sources", page.summary.text())
                self.assertIn("2 buildings", page.summary.text())
                self.assertEqual(page.table.rowCount(), 1)
