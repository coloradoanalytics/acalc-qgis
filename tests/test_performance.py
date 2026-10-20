# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Guard expensive UI work with real QGIS providers and selection signals."""

import json
from unittest.mock import patch

from tests.test_model import APP, FakeIface, ModelFixture, add_feature
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsFeatureRequest, QgsGeometry

from acalc.calculation_settings import ENGINE_ID, default_settings
from acalc.layers import load_layer
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, new_id


class PerformanceTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.record = self.store.add_layer("point_source", "Sources")
        layer = load_layer(self.store, self.record)
        for name in ("Alpha", "Beta", "Gamma"):
            add_feature(layer, LAYER_TYPES["point_source"], name)
        self.assertTrue(layer.commitChanges())
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.open_project(self.path)
        self.dock = self.plugin.dock
        self.layer = self.plugin.layers[self.record["layer_id"]]
        APP.processEvents()

    def tearDown(self):
        for layer in self.plugin.layers.values():
            if layer.isEditable():
                layer.rollBack()
        self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def add_run(self, variant_id=None, finished="2026-10-15T12:00:00Z"):
        project = self.store.metadata()
        variant_id = variant_id or self.store.variants()[0]["variant_id"]
        run_id = new_id()
        with self.store.connection() as db:
            db.execute("""INSERT INTO acalc_calculation_runs VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,'[]','{}')""",
                (run_id, project["project_id"], variant_id, "Saved variant", ENGINE_ID,
                 "test", "ISO 9613-2:2024", finished, finished, "f" * 64,
                 project["modified_at"], json.dumps(default_settings()), 1, 0, 1))
        return run_id

    def rows(self):
        return {self.dock.features.item(row, 0).data(Qt.UserRole): self.dock.features.item(row, 0)
                for row in range(self.dock.features.rowCount())}

    def test_hidden_results_are_deferred_and_refresh_when_opened(self):
        self.add_run()
        store = self.plugin.store
        with patch.object(store, "receiver_results", side_effect=AssertionError("Hidden results read")), \
                patch.object(store, "calculation_runs", side_effect=AssertionError("Hidden diagnostics read")):
            self.dock.refresh_project()
            self.dock.set_calculating(True)
            self.dock.set_calculating(False)
        self.dock.show_view("results")
        self.assertIn("Results match", self.dock.results_page.summary.text())
        self.dock.show_view("project")
        store.touch()
        self.dock.refresh_project()
        self.dock.show_view("results")
        self.assertIn("OUT OF DATE", self.dock.results_page.summary.text())
        self.plugin.close_project()
        self.assertEqual(self.dock.results_page.variant.count(), 0)
        self.assertFalse(self.dock.results_page.clear_button.isEnabled())

    def test_visible_results_refresh_once_and_busy_changes_preserve_rows(self):
        self.add_run()
        self.dock.show_view("results")
        store = self.plugin.store
        with patch.object(store, "receiver_results", wraps=store.receiver_results) as reads:
            self.dock.refresh_project()
            self.assertEqual(reads.call_count, 1)
            self.dock.set_calculating(True)
            self.assertFalse(self.dock.results_page.clear_button.isEnabled())
            self.dock.set_calculating(False)
            self.assertTrue(self.dock.results_page.clear_button.isEnabled())
            self.assertEqual(reads.call_count, 1)

    def test_run_summaries_match_latest_runs_without_decoding_diagnostics(self):
        self.add_run(finished="2026-10-14T12:00:00Z")
        self.add_run()
        other = self.store.add_variant("Other")
        self.add_run(other)
        self.add_run(other)  # Equal timestamps must use the same run-ID tie break.
        expected = [{key: run[key] for key in ("run_id", "variant_id", "variant_name")}
                    for run in self.store.calculation_runs()]
        with patch.object(self.store, "_decode_calculation_run", side_effect=AssertionError("Decoded diagnostics")):
            self.assertEqual(self.store.calculation_run_summaries(), expected)
        self.assertEqual(len(expected), 2)

    def test_feature_list_reads_only_names_and_fetches_only_missing_selections(self):
        self.dock.show_view("point_source")
        self.layer.startEditing()
        for i in range(505):
            add_feature(self.layer, LAYER_TYPES["point_source"], f"Extra {i}")
        APP.processEvents()
        self.dock.refresh_features()
        shown = set(self.rows())
        self.assertEqual(len(shown), 500)
        outside = next(f.id() for f in self.layer.getFeatures() if f.id() not in shown)
        inside = next(iter(shown))
        self.layer.selectByIds([inside, outside])
        with patch.object(self.layer, "getFeatures", wraps=self.layer.getFeatures) as reads:
            self.dock.refresh_features()
        self.assertEqual(reads.call_count, 2)
        for call in reads.call_args_list:
            request = call.args[0]
            self.assertTrue(request.flags() & QgsFeatureRequest.NoGeometry)
            self.assertEqual(request.subsetOfAttributes(), [self.layer.fields().indexFromName("name")])
        self.assertEqual(set(reads.call_args_list[1].args[0].filterFids()), {outside})
        self.assertEqual(len(self.rows()), 501)
        self.dock.filter.setText("No matching names")
        self.dock.refresh_features()
        self.assertEqual(set(self.rows()), {inside, outside})
        self.assertEqual(len(self.dock.features.selectedIndexes()), 2)

    def test_geometry_edits_keep_rows_and_dirty_state_through_undo_redo(self):
        self.dock.show_view("point_source")
        self.layer.startEditing()
        APP.processEvents()
        before = self.rows()
        fid = next(iter(before))
        self.layer.beginEditCommand("Move source")
        self.layer.changeGeometry(fid, QgsGeometry.fromWkt("POINT ZM (10 20 30 4)"))
        self.layer.endEditCommand()
        APP.processEvents()
        self.assertTrue(self.dock.save_edits_button.isEnabled())
        self.assertIs(self.rows()[fid], before[fid])
        self.layer.undoStack().undo()
        APP.processEvents()
        self.assertFalse(self.dock.save_edits_button.isEnabled())
        self.assertIs(self.rows()[fid], before[fid])
        self.layer.undoStack().redo()
        APP.processEvents()
        self.assertTrue(self.dock.save_edits_button.isEnabled())
        self.assertIs(self.rows()[fid], before[fid])
        self.layer.beginEditCommand("Rename source")
        self.layer.changeAttributeValue(fid, self.layer.fields().indexFromName("name"), "Renamed")
        self.layer.endEditCommand()
        APP.processEvents()
        self.assertEqual(self.rows()[fid].text(), "Renamed")
        self.layer.undoStack().undo()
        APP.processEvents()
        self.assertNotEqual(self.rows()[fid].text(), "Renamed")

    def test_multi_selection_is_applied_as_one_ui_change(self):
        self.dock.show_view("point_source")
        rows = self.rows()
        changes = []
        self.dock.features.itemSelectionChanged.connect(lambda: changes.append(True))
        self.layer.selectByIds(list(rows))
        self.assertEqual(len(changes), 1)
        self.assertEqual(len(self.dock.features.selectedIndexes()), len(rows))
        for fid, item in rows.items():
            self.assertIs(self.rows()[fid], item)
