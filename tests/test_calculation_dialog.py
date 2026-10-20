# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""API calculation setup, model counts and elapsed run progress."""

from unittest.mock import patch

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import QMessageBox

from acalc.api_connections import SETTINGS_KEY, engine_identity
from acalc.calculation_settings import default_settings
from acalc.plugin import AcalcPlugin
from acalc.schema import ModelError, LAYER_TYPES
from tests.test_calculation_ui import add_api
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


class CalculationDialogTests(ModelFixture):
    def setUp(self):
        super().setUp()
        QSettings().remove(SETTINGS_KEY)
        self.connection = add_api()
        self.engine_id = engine_identity(self.connection)
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.first = self.store.variants()[0]["variant_id"]
        self.second = self.store.add_variant("Night")
        self.plugin.open_project(self.path)

    def tearDown(self):
        if self.plugin.dock:
            self.plugin.unload()
        self.iface.window.close()
        QSettings().remove(SETTINGS_KEY)
        APP.processEvents()
        super().tearDown()

    def open_dialog(self):
        self.plugin.open_calculation_dialog()
        return self.plugin.calculation_dialog

    def test_setup_remembers_variant_selection_and_separate_engine_drafts(self):
        other = add_api("Second API", "http://127.0.0.1:9343")
        self.plugin.dock.calculation_page.refresh()
        dialog = self.open_dialog()
        self.assertFalse(self.plugin.engine_runner.active)
        self.assertEqual(dialog.selected_variants(), [self.first])
        dialog.select_all(False)
        self.assertFalse(dialog.start_button.isEnabled())
        dialog.close()
        dialog = self.open_dialog()
        self.assertEqual(dialog.selected_variants(), [])
        dialog.select_all(True)
        editor = dialog.panels[self.engine_id]
        editor.fields["temperature_c"].setValue(22)
        dialog.engine.setCurrentIndex(dialog.engine.findData(engine_identity(other)))
        dialog.panels[engine_identity(other)].fields["temperature_c"].setValue(12)
        dialog.engine.setCurrentIndex(dialog.engine.findData(self.engine_id))
        self.assertEqual(editor.settings()["temperature_c"], 22)
        self.assertTrue(dialog.start_button.isEnabled())

    def test_api_progress_hides_task_points_and_caps_running_percent(self):
        dialog = self.open_dialog()
        dialog.select_all(True)
        dialog._selected = [self.first, self.second]
        dialog.set_running(True)
        context = {"store": self.plugin.store, "variant_id": self.first,
                   "variant_name": "First", "batch_index": 0, "batch_total": 2}
        dialog.update_progress(context, {"phase": "checking", "completed": 0, "total": 0})
        self.assertEqual(dialog.progress.value(), 0)
        dialog.update_progress(context, {"state": "running", "phase": "propagation",
                                        "percent": 50, "completed_task_points": 2.5,
                                        "total_task_points": 5, "path_counts": {"checked": 12345, "pruned": 678}})
        self.assertEqual(dialog.path_counts.text(), "Calculations checked: 12,345 · Pruned: 678\n(one per source–receiver–reflection-path combination)")
        self.assertIn("Each receiver counts separately", dialog.path_counts.toolTip())
        self.assertIn("older engine runs", dialog.path_counts.toolTip())
        self.assertIn("never exceeds", dialog.path_counts.toolTip())
        self.assertEqual(dialog.path_counts.accessibleName(), "Calculations checked and pruned")
        self.assertEqual(dialog.progress.value(), 250)
        self.assertIn("Calculating sound levels · 50%", dialog.status.text())
        self.assertNotIn("task points", dialog.status.text())
        context.update(batch_index=1, variant_id=self.second)
        dialog.variant_started(context)
        self.assertTrue(dialog.path_counts.text().startswith("Calculations checked: awaiting API information"))
        dialog.update_progress(context, {"state": "running", "phase": "propagation",
                                        "percent": 99.999999, "completed_task_points": 4.999999,
                                        "total_task_points": 5})
        self.assertLess(dialog.progress.value(), 1000)
        dialog._completed = 1
        dialog.succeeded(context, {"pathway_warnings": ["Completed with warnings: 1 receiver(s) are incomplete."]})
        self.assertEqual(dialog.progress.value(), 1000)
        self.assertFalse(dialog._running)
        # A pathway problem completes the variant and is reported, not failed.
        self.assertIn("Complete with warnings", "\n".join(
            dialog.variants.item(row, 1).text() for row in range(dialog.variants.rowCount())))
        self.assertIn("Calculation complete", dialog.status.text())
        self.assertIn("1 receiver(s) are incomplete", dialog.status.text())

    def test_worker_activity_updates_while_beam_progress_is_unchanged(self):
        dialog = self.open_dialog()
        dialog.set_running(True)
        context = {"store": self.plugin.store, "variant_id": self.first,
                   "variant_name": "First", "batch_index": 0, "batch_total": 1}
        value = {"state": "running", "phase": "propagation", "percent": 50,
                 "path_counts": {"checked": 0, "pruned": 0},
                 "work": {"workers": 8, "active_workers": 8, "queued_jobs": 1250,
                          "completed_jobs": 100, "operations": 25}}
        dialog.update_progress(context, value)
        self.assertFalse(dialog.work_progress.isHidden())
        self.assertIn("Workers: 8/8 active", dialog.work_progress.text())
        self.assertIn("Queued jobs: 1,250", dialog.work_progress.text())
        value["work"].update(completed_jobs=200, operations=50)
        dialog.update_progress(context, value)
        self.assertIn("Jobs completed: 200 · Evaluations: 50", dialog.work_progress.text())
        self.assertEqual(dialog.path_counts.text(), "Calculations checked: 0 · Pruned: 0\n(one per source–receiver–reflection-path combination)")
        # Without pruning the always-zero pruned total is not shown.
        dialog._pruning = False
        dialog.update_progress(context, value)
        self.assertEqual(dialog.path_counts.text(), "Calculations checked: 0\n(one per source–receiver–reflection-path combination)")
        self.assertEqual(dialog.progress.value(), 500)
        dialog.variant_started(context)
        self.assertTrue(dialog.work_progress.isHidden())
        # Engines without the optional extension continue to display normal progress.
        dialog.update_progress(context, {"state": "running", "phase": "propagation", "percent": 50})
        self.assertTrue(dialog.work_progress.isHidden())
        dialog.update_progress(context, value)
        dialog.set_running(False)
        self.assertTrue(dialog.work_progress.isHidden())

    def test_elapsed_clock_updates_without_api_events_and_freezes_at_completion(self):
        dialog = self.open_dialog()
        dialog._selected = [self.first]
        context = {"store": self.plugin.store, "variant_id": self.first,
                   "variant_name": "First", "batch_index": 0, "batch_total": 1,
                   "model_summary": {"source_count": 1120, "receiver_count": 11,
                                     "building_count": 153, "receivers_resolved": True}}
        with patch("acalc.calculation_dialog.monotonic", return_value=100) as clock:
            dialog.set_running(True)
            dialog.variant_started(context)
            self.assertTrue(dialog._clock.isActive())
            self.assertIn("12,320 source–receiver pairs", dialog.model_summary.text())
            clock.return_value = 165
            dialog._clock.timeout.emit()
            self.assertEqual(dialog.run_time.text(), "Elapsed: 1 min 05 s")
            clock.return_value = 190
            dialog.succeeded(context, {})
            self.assertEqual(dialog.run_time.text(), "Total time: 1 min 30 s")
            self.assertFalse(dialog._clock.isActive())
            self.assertEqual(dialog.variants.item(0, 1).text(), "Complete · 1 min 30 s")
            clock.return_value = 999
            dialog.update_run_time()
            self.assertEqual(dialog.run_time.text(), "Total time: 1 min 30 s")

    def test_area_counts_stay_unresolved_until_complete_and_batch_times_are_separate(self):
        dialog = self.open_dialog()
        dialog._selected = [self.first, self.second]
        context = {"store": self.plugin.store, "variant_id": self.first,
                   "variant_name": "First", "batch_index": 0, "batch_total": 2,
                   "model_summary": {"source_count": 10, "receiver_count": 2,
                                     "area_receiver_count": 1, "receivers_resolved": False}}
        with patch("acalc.calculation_dialog.monotonic", return_value=100) as clock:
            dialog.set_running(True)
            dialog.variant_started(context)
            self.assertIn("20 point-receiver pairs", dialog.model_summary.text())
            self.assertIn("additional grid pairs", dialog.model_summary.text())
            clock.return_value = 110
            dialog.succeeded(context, {"source_count": 10, "receiver_count": 100})
            self.assertIn("1,000 source–receiver pairs", dialog.model_summary.text())
            self.assertNotIn("additional grid", dialog.model_summary.text())
            context = {**context, "variant_id": self.second, "variant_name": "Night", "batch_index": 1}
            context.pop("model_summary")
            dialog.variant_started(context)
            self.assertTrue(dialog.model_summary.isHidden())
            clock.return_value = 115
            dialog.update_run_time()
            self.assertEqual(dialog.run_time.text(), "Elapsed: 15.0 s · Current variant: 5.0 s")
            dialog.failed(context, "test failure")
            self.assertEqual(dialog.run_time.text(), "Total time: 15.0 s")
            self.assertEqual(dialog.variants.item(1, 1).text(), "Failed · 5.0 s")
            self.assertFalse(dialog._clock.isActive())
            clock.return_value = 200
            dialog.set_running(True)
            self.assertEqual(dialog.run_time.text(), "Elapsed: 0.0 s")
            dialog.stopped(context)
            self.assertFalse(dialog._clock.isActive())

    def test_start_passes_engine_and_failure_is_visible_without_locking(self):
        dialog = self.open_dialog()
        with patch.object(self.plugin, "calculate", side_effect=ModelError("API offline")) as calculate:
            dialog.start()
        self.assertEqual(calculate.call_args.kwargs["engine_id"], self.engine_id)
        self.assertIn("API offline", dialog.status.text())
        self.assertFalse(dialog._running)
        self.assertTrue(dialog.start_button.isEnabled())

    def test_cancel_save_prompt_retains_edits_and_settings(self):
        record = self.store.add_layer("point_receiver", "Receivers")
        self.plugin.reload_layers()
        layer = self.plugin.layers[record["layer_id"]]
        add_feature(layer, LAYER_TYPES["point_receiver"])
        dialog = self.open_dialog()
        dialog.panels[self.engine_id].fields["temperature_c"].setValue(24)
        original = self.store.calculation()
        with patch("acalc.plugin.QMessageBox.question", return_value=QMessageBox.Cancel), \
                patch.object(self.plugin.engine_runner, "start_batch") as start:
            dialog.start()
        start.assert_not_called()
        self.assertTrue(layer.isModified())
        self.assertEqual(self.store.calculation(), original)
        self.assertFalse(dialog._running)
        self.assertTrue(dialog.start_button.isEnabled())

    def test_only_active_api_capability_mismatch_is_reported(self):
        settings = default_settings()
        settings["reflection_order"] = 5
        self.store.save_calculation(settings)
        dialog = self.open_dialog()
        editor = dialog.panels[self.engine_id]
        self.assertEqual(editor.fields["reflection_order"].value(), 5)
        self.assertNotIn("lateral_diffraction", editor.settings())
        self.assertFalse(hasattr(editor, "lateral_diffraction"))
        self.assertIn("maximum", editor.compatibility.text())

    def test_failure_stops_batch_and_cancel_preserves_completed_status(self):
        dialog = self.open_dialog()
        dialog.select_all(True)
        dialog._selected = [self.first, self.second]
        dialog.set_running(True)
        context = {"store": self.plugin.store, "variant_id": self.first,
                   "variant_name": "First", "batch_index": 0, "batch_total": 2}
        dialog.succeeded(context, {})
        self.assertTrue(dialog._running)
        context.update(variant_id=self.second, variant_name="Night", batch_index=1)
        dialog.failed(context, "unsupported: terrain")
        self.assertIn("unsupported: terrain", dialog.status.text())
        self.assertIn("1 of 2 variants", dialog.status.text())
        self.assertFalse(dialog._running)

    def test_stopping_message_survives_later_progress_until_confirmed(self):
        dialog = self.open_dialog()
        dialog._selected = [self.first]
        dialog.set_running(True)
        context = {"store": self.plugin.store, "variant_id": self.first,
                   "variant_name": "First", "batch_index": 0, "batch_total": 1}
        with patch.object(self.plugin, "cancel_calculation") as cancel:
            dialog.cancel()
            cancel.assert_called_once()
        for phase in ("propagation", "cancelling"):
            dialog.update_progress(context, {"state": "running", "phase": phase, "percent": 35})
            self.assertIn("Stopping calculation", dialog.status.text())
            self.assertTrue(dialog._running)
            self.assertFalse(dialog.cancel_button.isEnabled())
            self.assertFalse(dialog.start_button.isEnabled())
        dialog.stopped(context)
        self.assertFalse(dialog._running)
        self.assertIn("Calculation stopped", dialog.status.text())
