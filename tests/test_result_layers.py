# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Each receiver layer has its own results in every calculated variant."""

import json
from pathlib import Path
from unittest.mock import patch, PropertyMock

from qgis.PyQt.QtWidgets import QDialog, QMessageBox
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsGeometry, QgsPalLayerSettings, QgsProject, QgsVectorLayer, QgsVectorLayerSimpleLabeling

from acalc.calculation_settings import ENGINE_ID, default_settings
from acalc.plugin import AcalcPlugin
from acalc.result_layers import clear_variant_results, result_layer_tables, result_table_name, sync_result_layers
from acalc.schema import LAYER_TYPES, ModelError, new_id
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


class ResultLayerTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.record = self.store.add_layer("point_receiver", "Receivers")
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.open_project(self.path)
        self.receiver = self.plugin.layers[self.record["layer_id"]]
        feature = add_feature(self.receiver, LAYER_TYPES["point_receiver"], "Porch")
        self.object_id = str(feature["object_id"])
        self.plugin.commit_layer(self.receiver)
        self.variant = self.store.variants()[0]["variant_id"]

    def tearDown(self):
        if self.plugin.dock:
            self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def save_run(self, variant=None, levels=None, snapshot=True, objects=None):
        variant = variant or self.variant
        levels = [10, 20, 30, 40, 50, 40, 30, None] if levels is None else levels
        run_id = new_id()
        project = self.store.metadata()
        timestamp = f"2026-09-19T18:00:{len(self.store.calculation_runs()):02d}Z"
        objects = objects or [(self.record, self.object_id)]
        with self.store.connection() as db:
            db.execute("DELETE FROM acalc_calculation_runs WHERE variant_id=?", (variant,))
            db.execute("""INSERT INTO acalc_calculation_runs VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,'[]','{}')""",
                (run_id, project["project_id"], variant, "Saved variant", ENGINE_ID, "test", "ISO 9613-2:2024",
                 timestamp, timestamp, "f" * 64, project["modified_at"], json.dumps(default_settings()), 1, len(objects), 1))
            for record, object_id in objects:
                geometry = db.execute(f'SELECT geom FROM "{record["table_name"]}" WHERE object_id=?', (object_id,)).fetchone()[0]
                metadata = {"geometry_gpkg_hex": geometry.hex()} if snapshot else {}
                db.execute("INSERT INTO acalc_receiver_results VALUES(?,?,?,?,?,?,?,?)",
                           (run_id, record["layer_id"], object_id, "Porch", json.dumps(levels),
                            51 if any(v is not None for v in levels) else None,
                            50 if any(v is not None for v in levels) else None, json.dumps(metadata)))
        return run_id

    def test_variants_get_separate_persistent_layers_and_receiver_properties(self):
        other = self.store.add_variant("Night")
        first = self.save_run()
        second = self.save_run(other, levels=[None] * 8)
        before = self.store.metadata()["modified_at"]
        fields = self.receiver.fields().names()
        self.plugin.dock.results_page.publish_saved_results()
        self.assertEqual(set(self.plugin.result_layers), {(self.variant, self.record["layer_id"]), (other, self.record["layer_id"])})
        self.assertEqual(self.receiver.fields().names(), fields)
        self.assertEqual(self.store.metadata()["modified_at"], before)
        self.assertEqual(len(self.store.layers()), 1)
        result_layer = self.plugin.result_layers[self.variant, self.record["layer_id"]]
        result = next(result_layer.getFeatures())
        self.assertTrue(result_layer.readOnly())
        self.assertFalse(result_layer.startEditing())
        self.assertEqual(result["run_id"], first)
        self.assertEqual(result["broadband_a_db"], 50)
        self.assertEqual(result["lz_63_db"], 10)
        self.assertEqual(result.geometry().asWkt(), next(self.receiver.getFeatures()).geometry().asWkt())
        self.assertEqual(result_layer.crs(), self.receiver.crs())
        node = QgsProject.instance().layerTreeRoot().findLayer(result_layer.id())
        self.assertEqual(node.parent().customProperty("acalc/result_variant_id"), self.variant)
        self.assertEqual(node.parent().parent().name(), "Results")
        self.assertIs(node.parent().parent().parent(), self.plugin.group)
        other_node = QgsProject.instance().layerTreeRoot().findLayer(self.plugin.result_layers[other, self.record["layer_id"]].id())
        self.assertEqual(other_node.parent().name(), "Night")
        self.assertEqual(other_node.parent().customProperty("acalc/result_variant_id"), other)
        self.assertIs(other_node.parent().parent(), node.parent().parent())
        standalone = QgsVectorLayer(result_layer.source(), "Standalone", "ogr")
        self.assertTrue(standalone.isValid())
        self.assertEqual(standalone.featureCount(), 1)
        self.assertEqual(next(standalone.getFeatures())["broadband_a_db"], 50)

        def inspect(dialog):
            page = dialog.receiver_results
            self.assertIs(dialog.tabs.currentWidget(), page)
            self.assertFalse(hasattr(page, "run_combo"))
            self.assertEqual(page.variant.currentData(), self.variant)
            self.assertEqual(page.table.item(0, 1).text(), "50.0")
            page.variant.setCurrentIndex(page.variant.findData(other))
            self.assertEqual(page.variant.currentData(), other)
            self.assertEqual(page.table.item(0, 1).text(), "−∞")
            return QDialog.Rejected

        with patch("acalc.plugin.FeatureDialog.exec", inspect):
            self.plugin.open_feature_properties(result_layer.id(), result.id())
        # Opening the receiver normally selects its latest calculated result.
        def inspect_default(dialog):
            self.assertEqual(dialog.receiver_results.variant.currentData(), other)
            self.assertEqual(dialog.receiver_results.table.item(0, 1).text(), "−∞")
            return QDialog.Rejected
        with patch("acalc.plugin.FeatureDialog.exec", inspect_default):
            self.plugin.edit_feature(layer=self.receiver, feature_id=next(self.receiver.getFeatures()).id())

    def test_projection_rounds_legacy_saved_results(self):
        run_id = self.save_run(levels=[10.25, None, None, None, None, None, None, None])
        with self.store.connection() as db:
            db.execute("UPDATE acalc_receiver_results SET broadband_a_db=50.25,broadband_z_db=51.24 WHERE run_id=?",
                       (run_id,))
        records = sync_result_layers(self.store)
        with self.store.connection(readonly=True) as db:
            row = db.execute(f'SELECT broadband_a_db,lz_63_db FROM "{records[0]["table_name"]}"').fetchone()
            self.assertNotIn("broadband_z_db", [field[1] for field in db.execute(f'PRAGMA table_info("{records[0]["table_name"]}")')])
        self.assertEqual(tuple(row), (50.3, 10.3))
        self.assertEqual(self.store.receiver_results(run_id=run_id)[0]["broadband_a_db"], 50.25)

    def test_clear_results_confirms_results_page_selection_and_persists(self):
        other_record, other_id = self.add_receiver_layer("Other receivers")
        other = self.store.add_variant("Night")
        objects = [(self.record, self.object_id), (other_record, other_id)]
        kept_run = self.save_run(objects=objects)
        removed_run = self.save_run(other, objects=objects)
        page = self.plugin.dock.results_page
        page.publish_saved_results(other)
        before = self.store.metadata()["modified_at"]
        originals = dict(self.plugin.result_layers)
        removed_ids = [layer.id() for key, layer in originals.items() if key[0] == other]
        self.assertEqual(self.plugin.selected_variant()["variant_id"], self.variant)
        self.assertTrue(page.clear_button.isEnabled())
        with patch("acalc.plugin.QMessageBox.question", return_value=QMessageBox.No) as question:
            page.clear_button.click()
        self.assertIn("Night", question.call_args.args[2])
        self.assertEqual(question.call_args.args[4], QMessageBox.No)
        self.assertEqual(len(self.plugin.result_layers), 4)
        self.assertTrue(self.store.receiver_results(run_id=removed_run))
        with patch("acalc.plugin.QMessageBox.question", return_value=QMessageBox.Yes):
            page.clear_button.click()
        self.assertEqual(page.variant.currentData(), other)
        self.assertEqual(page.table.rowCount(), 0)
        self.assertFalse(page.clear_button.isEnabled())
        self.assertEqual(self.store.calculation_runs(other), [])
        self.assertEqual(self.store.receiver_results(run_id=removed_run), [])
        self.assertTrue(self.store.receiver_results(run_id=kept_run))
        self.assertEqual(self.store.metadata()["modified_at"], before)
        self.assertEqual(len(self.store.layers()), 2)
        for layer_id in removed_ids:
            self.assertIsNone(QgsProject.instance().mapLayer(layer_id))
        for key, layer in self.plugin.result_layers.items():
            self.assertEqual(key[0], self.variant)
            self.assertIs(layer, originals[key])
        with self.store.connection(readonly=True) as db:
            for record in (self.record, other_record):
                table = result_table_name(other, record["layer_id"])
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())
                self.assertIsNone(db.execute("SELECT 1 FROM gpkg_contents WHERE table_name=?", (table,)).fetchone())
        self.plugin.close_project()
        self.plugin.open_project(self.path)
        self.assertEqual({key[0] for key in self.plugin.result_layers}, {self.variant})

    def test_clear_deleted_variant_and_block_during_calculation(self):
        self.save_run()
        self.store.add_variant("Uncalculated")
        self.store.delete_variant(self.variant)
        page = self.plugin.dock.results_page
        page.publish_saved_results(self.variant)
        with patch.object(type(self.plugin.engine_runner), "active", new_callable=PropertyMock, return_value=True):
            self.plugin.dock.set_calculating(True)
            self.assertFalse(page.clear_button.isEnabled())
            with patch("acalc.plugin.QMessageBox.question") as question:
                self.plugin.run(self.plugin.clear_results)
                question.assert_not_called()
        self.plugin.dock.set_calculating(False)
        self.assertTrue(page.clear_button.isEnabled())
        with patch("acalc.plugin.QMessageBox.question", return_value=QMessageBox.Yes):
            page.clear_button.click()
        self.assertEqual(page.variant.findData(self.variant), -1)
        self.assertEqual(self.plugin.result_layers, {})

    def test_clear_failure_rolls_back_saved_results_and_projection(self):
        run = self.save_run()
        records = sync_result_layers(self.store)
        from acalc.result_layers import _drop_projection
        def fail_after_drop(db, table):
            _drop_projection(db, table)
            raise ModelError("Cannot clear")
        with patch("acalc.result_layers._drop_projection", side_effect=fail_after_drop):
            with self.assertRaisesRegex(ModelError, "Cannot clear"):
                clear_variant_results(self.store, self.variant)
        self.assertTrue(self.store.receiver_results(run_id=run))
        with self.store.connection(readonly=True) as db:
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM "{records[0]["table_name"]}"').fetchone()[0], 1)

    def test_version_three_projection_is_rebuilt_without_z_total(self):
        self.save_run()
        self.plugin.refresh_result_layers()
        record = sync_result_layers(self.store)[0]
        table = record["table_name"]
        layer = self.plugin.result_layers[self.variant, self.record["layer_id"]]
        with self.store.connection() as db:
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN broadband_z_db REAL')
            db.execute(f'UPDATE "{table}" SET broadband_z_db=51')
            description = json.loads(db.execute("SELECT description FROM gpkg_contents WHERE table_name=?", (table,)).fetchone()[0])
            description["acalc_result_layer"] = 3
            db.execute("UPDATE gpkg_contents SET description=? WHERE table_name=?", (json.dumps(description), table))
        sync_result_layers(self.store)
        with self.store.connection(readonly=True) as db:
            fields = {field[1] for field in db.execute(f'PRAGMA table_info("{table}")')}
            row = db.execute(f'SELECT geom,broadband_a_db FROM "{table}"').fetchone()
        self.assertNotIn("broadband_z_db", fields)
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], 50)
        self.assertEqual(layer.attributeDisplayName(layer.fields().indexFromName("broadband_a_db")), "dBA")
        self.assertIn("dBA", layer.labeling().settings().fieldName)

    def test_result_folder_placement_and_names_are_not_reapplied(self):
        self.save_run()
        self.plugin.refresh_result_layers()
        layer = self.plugin.result_layers[self.variant, self.record["layer_id"]]
        root = QgsProject.instance().layerTreeRoot()
        variant_group = root.findLayer(layer.id()).parent()
        group = variant_group.parent()
        self.plugin.place_layer(layer, group)
        group.removeChildNode(variant_group)
        root.findLayer(layer.id()).setItemVisibilityChecked(False)
        layer.setOpacity(0.4)
        original_name = layer.name()
        self.plugin.refresh_result_layers()
        node = root.findLayer(layer.id())
        self.assertIs(node.parent(), group)
        self.assertFalse(node.itemVisibilityChecked())
        self.assertEqual(layer.opacity(), 0.4)
        variant = self.store.variants()[0]
        self.store.save_variant(self.variant, "Renamed variant", variant["definition"])
        self.plugin.dock.results_page.refresh()
        self.assertEqual(layer.name(), original_name)
        self.assertEqual(len(group.children()), 1)

    def test_recalculation_reuses_layer_preserves_style_replaces_results_and_reopens(self):
        first = self.save_run()
        self.plugin.dock.results_page.publish_saved_results()
        layer = self.plugin.result_layers[self.variant, self.record["layer_id"]]
        layer_id = layer.id()
        layer.setOpacity(0.4)
        node = QgsProject.instance().layerTreeRoot().findLayer(layer_id)
        node.setItemVisibilityChecked(False)
        second = self.save_run(levels=[20] * 8)
        self.plugin.dock.results_page.publish_saved_results(self.variant)
        self.assertEqual(self.plugin.result_layers[self.variant, self.record["layer_id"]].id(), layer_id)
        self.assertEqual(layer.opacity(), 0.4)
        self.assertFalse(node.itemVisibilityChecked())
        self.assertEqual(layer.featureCount(), 1)
        self.assertEqual(next(layer.getFeatures())["lz_63_db"], 20)
        self.assertEqual(self.store.receiver_results(run_id=first), [])
        with self.store.connection(readonly=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acalc_calculation_runs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acalc_receiver_results").fetchone()[0], 1)
        self.assertEqual(len(QgsProject.instance().mapLayers()), 2)
        # Plugin reload adopts existing providers without duplicating layers.
        self.plugin.unload()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.assertEqual(self.plugin.result_layers[self.variant, self.record["layer_id"]].id(), layer_id)
        self.assertEqual(len(QgsProject.instance().mapLayers()), 2)
        self.plugin.close_project()
        self.assertEqual(len(QgsProject.instance().mapLayers()), 0)
        self.plugin.open_project(self.path)
        self.assertEqual(next(self.plugin.result_layers[self.variant, self.record["layer_id"]].getFeatures())["run_id"], second)
        self.assertEqual(len(self.plugin.layers), 1)
        project_path = str(Path(self.directory.name) / "saved map.qgs")
        self.assertTrue(QgsProject.instance().write(project_path))
        QgsProject.instance().clear()
        self.assertTrue(QgsProject.instance().read(project_path))
        self.assertEqual(len(self.plugin.result_layers), 1)
        self.assertEqual(len(QgsProject.instance().mapLayers()), 2)
        self.assertEqual(next(self.plugin.result_layers[self.variant, self.record["layer_id"]].getFeatures())["run_id"], second)

    def test_custom_result_labels_survive_selection_updates_and_project_restore(self):
        self.save_run()
        self.plugin.refresh_result_layers()
        layer = self.plugin.result_layers[self.variant, self.record["layer_id"]]
        self.assertTrue(layer.labelsEnabled())
        self.assertIn("broadband_a_db", layer.labeling().settings().fieldName)
        labels = QgsPalLayerSettings()
        labels.fieldName = "receiver_name"
        text_format = labels.format()
        text_format.setColor(QColor("#c13570"))
        text_format.setSize(17)
        labels.setFormat(text_format)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
        layer.renderer().symbol().setSize(9)

        def check():
            self.assertEqual(layer.labeling().settings().fieldName, "receiver_name")
            self.assertEqual(layer.labeling().settings().format().color().name(), "#c13570")
            self.assertEqual(layer.labeling().settings().format().size(), 17)
            self.assertEqual(layer.renderer().symbol().size(), 9)

        self.iface.setActiveLayer(layer)
        check()
        layer.setLabelsEnabled(False)
        self.iface.setActiveLayer(self.receiver)
        self.iface.setActiveLayer(layer)
        check()
        self.assertFalse(layer.labelsEnabled())
        self.save_run(levels=[20] * 8)
        self.plugin.refresh_result_layers()
        check()
        self.assertFalse(layer.labelsEnabled())
        self.assertEqual(next(layer.getFeatures())["lz_63_db"], 20)
        project_path = str(Path(self.directory.name) / "custom styles.qgs")
        self.assertTrue(QgsProject.instance().write(project_path))
        QgsProject.instance().clear()
        self.assertTrue(QgsProject.instance().read(project_path))
        layer = self.plugin.result_layers[self.variant, self.record["layer_id"]]
        check()
        self.assertFalse(layer.labelsEnabled())

    def test_renamed_and_duplicate_layers_share_recalculated_data_and_keep_styles(self):
        first = self.save_run()
        self.plugin.refresh_result_layers()
        key = (self.variant, self.record["layer_id"])
        saved = self.plugin.result_layers[key]
        default_name, source, saved_id = saved.name(), saved.source(), saved.id()
        saved.setName("Report results")
        saved.setOpacity(0.27)
        duplicate = saved.clone()
        duplicate.setName("Alternative labels")
        duplicate.setOpacity(0.63)
        duplicate.setLabelsEnabled(False)
        duplicate.renderer().symbol().setSize(11)
        duplicate_id = duplicate.id()
        QgsProject.instance().addMapLayer(duplicate)
        root = QgsProject.instance().layerTreeRoot()
        # The duplicate is deliberately outside Results; it still shares data.
        duplicate_parent = root.findLayer(duplicate_id).parent()
        group = root.findLayer(saved_id).parent()
        unrelated = QgsVectorLayer("Point?crs=EPSG:26913", "Survey notes", "memory")
        QgsProject.instance().addMapLayer(unrelated, False)
        group.addLayer(unrelated)
        self.assertEqual(duplicate.source(), source)
        self.assertEqual(len(result_layer_tables(self.store)), 1)
        self.plugin.unload()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.assertEqual(saved.name(), "Report results")
        self.assertNotIn(key, self.plugin.result_layers)
        with patch("acalc.plugin.sync_result_layers") as publish:
            self.iface.setActiveLayer(saved)
            self.iface.setActiveLayer(duplicate)
            self.receiver.startEditing()
            feature = next(self.receiver.getFeatures())
            self.receiver.changeAttributeValue(feature.id(), self.receiver.fields().indexFromName("notes"), "Edited")
            self.plugin.commit_layer(self.receiver)
            APP.processEvents()
            self.plugin.dock.refresh_project(metadata=False)
            publish.assert_not_called()

        second = self.save_run(levels=[22] * 8)
        self.plugin.dock.results_page.refresh()
        self.assertEqual(next(saved.getFeatures())["run_id"], first)
        context = dict(store=self.plugin.store, model_path=self.plugin.store.path,
                       variant_id=self.variant, validate=False)
        self.plugin.calculation_succeeded(context, dict(run_id=second, receiver_count=1))
        current = self.plugin.result_layers[key]
        self.assertIsNot(current, saved)
        self.assertEqual(current.name(), default_name)
        for layer in (current, saved, duplicate):
            self.assertEqual(layer.source(), source)
            self.assertEqual(next(layer.getFeatures())["lz_63_db"], 22)
        self.assertEqual(saved.name(), "Report results")
        self.assertEqual(saved.opacity(), 0.27)
        self.assertEqual(duplicate.name(), "Alternative labels")
        self.assertEqual(duplicate.opacity(), 0.63)
        self.assertEqual(duplicate.renderer().symbol().size(), 11)
        self.assertFalse(duplicate.labelsEnabled())
        self.assertIs(root.findLayer(duplicate_id).parent(), duplicate_parent)
        self.assertIs(root.findLayer(unrelated.id()).parent(), group)
        current.setOpacity(0.42)
        third = self.save_run(levels=[33] * 8)
        self.plugin.calculation_succeeded(context, dict(run_id=third, receiver_count=1))
        self.assertIs(self.plugin.result_layers[key], current)
        self.assertEqual(current.opacity(), 0.42)
        for layer in (current, saved, duplicate):
            self.assertEqual(layer.source(), source)
            self.assertEqual(next(layer.getFeatures())["lz_63_db"], 33)
        self.assertEqual(len(result_layer_tables(self.store)), 1)

        path = str(Path(self.directory.name) / "shared results.qgs")
        self.assertTrue(QgsProject.instance().write(path))
        QgsProject.instance().clear()
        self.assertTrue(QgsProject.instance().read(path))
        saved = QgsProject.instance().mapLayer(saved_id)
        duplicate = QgsProject.instance().mapLayer(duplicate_id)
        self.assertEqual(saved.name(), "Report results")
        self.assertEqual(duplicate.name(), "Alternative labels")
        self.assertFalse(duplicate.labelsEnabled())
        self.assertEqual(duplicate.renderer().symbol().size(), 11)
        self.save_run(levels=[44] * 8)
        self.plugin.refresh_result_layers()
        for layer in (saved, duplicate):
            self.assertEqual(layer.source(), source)
            self.assertEqual(next(layer.getFeatures())["lz_63_db"], 44)
        with patch("acalc.plugin.QMessageBox.question", return_value=QMessageBox.Yes):
            self.plugin.clear_results()
        # Clearing the current calculation must not break retained shared views.
        self.assertTrue(saved.isValid())
        self.assertEqual(next(saved.getFeatures())["lz_63_db"], 44)

    def test_earlier_snapshot_layers_and_duplicates_reconnect_to_shared_results(self):
        run = self.save_run()
        record = sync_result_layers(self.store)[0]
        table = record["table_name"]
        old_table = table + "_" + run.replace("-", "")
        old_record = dict(record, table_name=old_table, snapshot_id=run, acalc_result_layer=4)
        with self.store.connection() as db:
            db.execute(f'CREATE TABLE "{old_table}" AS SELECT * FROM "{table}"')
            db.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,description,srs_id) "
                       "SELECT ?,data_type,?,?,srs_id FROM gpkg_contents WHERE table_name=?",
                       (old_table, old_table, json.dumps(old_record), table))
            db.execute("INSERT INTO gpkg_geometry_columns "
                       "SELECT ?,column_name,geometry_type_name,srs_id,z,m FROM gpkg_geometry_columns WHERE table_name=?",
                       (old_table, table))
        old = QgsVectorLayer(f"{self.path}|layername={old_table}", "My results", "ogr")
        self.assertTrue(old.isValid())
        old.setOpacity(0.24)
        duplicate = old.clone()
        duplicate.setName("My copy")
        QgsProject.instance().addMapLayers([old, duplicate])
        self.save_run(levels=[26] * 8)
        self.plugin.refresh_result_layers()
        for layer in (old, duplicate):
            self.assertEqual(layer.source(), f"{self.path}|layername={table}")
            self.assertEqual(next(layer.getFeatures())["lz_63_db"], 26)
            self.assertEqual(layer.opacity(), 0.24)
        self.assertEqual(old.name(), "My results")
        self.assertEqual(duplicate.name(), "My copy")

    def test_saved_positions_survive_receiver_edits_and_deletion(self):
        run_id = self.save_run()
        original = next(self.receiver.getFeatures())
        self.receiver.startEditing()
        self.receiver.changeGeometry(original.id(), QgsGeometry.fromWkt("POINT ZM (1 2 3 4)"))
        self.plugin.commit_layer(self.receiver)
        # First projection happens after the receiver has moved.
        self.plugin.dock.results_page.publish_saved_results()
        layer = self.plugin.result_layers[self.variant, self.record["layer_id"]]
        result = next(layer.getFeatures())
        self.assertEqual(result.geometry().asWkt(), original.geometry().asWkt())
        self.assertEqual(result["out_of_date"], 1)
        self.assertNotIn("out of date", layer.name())
        self.receiver.startEditing()
        self.receiver.deleteFeature(original.id())
        self.plugin.commit_layer(self.receiver)
        APP.processEvents()
        self.assertEqual(layer.featureCount(), 1)
        self.assertEqual(next(layer.getFeatures())["run_id"], run_id)
        with self.assertRaisesRegex(ModelError, "deleted"):
            self.plugin.open_feature_properties(layer.id(), next(layer.getFeatures()).id())

    def test_legacy_geometry_is_only_recovered_from_matching_revision(self):
        self.save_run(snapshot=False)
        self.plugin.dock.results_page.publish_saved_results()
        original = next(self.plugin.result_layers[self.variant, self.record["layer_id"]].getFeatures()).geometry().asWkt()
        self.store.update_metadata("Changed", "", "", "", {})
        self.plugin.dock.results_page.publish_saved_results()
        result = next(self.plugin.result_layers[self.variant, self.record["layer_id"]].getFeatures())
        self.assertEqual(result.geometry().asWkt(), original)
        self.assertEqual(result["geometry_source"], "matching model revision")
        other = self.store.add_variant("Outdated calculation")
        self.save_run(other, snapshot=False)
        self.store.update_metadata("Changed again", "", "", "", {})
        self.plugin.dock.results_page.publish_saved_results()
        result = next(self.plugin.result_layers[other, self.record["layer_id"]].getFeatures())
        self.assertTrue(result.geometry().isNull())
        self.assertEqual(result["geometry_source"], "unavailable")
        self.assertEqual(result["broadband_a_db"], 50)

    def test_duplicate_object_ids_across_receiver_layers_and_deleted_variant(self):
        other_record = self.store.add_layer("point_receiver", "Other receivers")
        self.plugin.reload_layers()
        other = self.plugin.layers[other_record["layer_id"]]
        feature = add_feature(other, LAYER_TYPES["point_receiver"])
        other.changeAttributeValue(feature.id(), other.fields().indexFromName("object_id"), self.object_id)
        self.plugin.commit_layer(other)
        self.save_run(objects=[(self.record, self.object_id), (other_record, self.object_id)])
        self.store.add_variant("Uncalculated")
        self.store.delete_variant(self.variant)
        self.plugin.dock.results_page.publish_saved_results()
        expected = {(self.variant, self.record["layer_id"]), (self.variant, other_record["layer_id"])}
        self.assertEqual(set(self.plugin.result_layers), expected)
        for key, layer in self.plugin.result_layers.items():
            self.assertIn("deleted variant", layer.name())
            group = QgsProject.instance().layerTreeRoot().findLayer(layer.id()).parent()
            self.assertEqual(group.name(), "Saved variant (deleted variant)")
            self.assertEqual(group.parent().name(), "Results")
            self.assertEqual(layer.featureCount(), 1)
            self.assertEqual(next(layer.getFeatures())["layer_id"], key[1])
            self.assertEqual(layer.customProperty("acalc/receiver_layer_id"), key[1])
        layer = self.plugin.result_layers[self.variant, self.record["layer_id"]]
        QgsProject.instance().removeMapLayer(layer.id())
        self.assertEqual(len(self.plugin.result_layers), 1)
        self.plugin.reload_layers()
        self.assertEqual(len(self.plugin.result_layers), 1)
        self.plugin.dock.results_page.publish_saved_results()
        self.assertEqual(set(self.plugin.result_layers), expected)
        self.assertEqual(self.plugin.result_layers[self.variant, self.record["layer_id"]].featureCount(), 1)

    def test_bad_snapshot_does_not_replace_previous_layer(self):
        first = self.save_run()
        records = sync_result_layers(self.store)
        second = self.save_run()
        with self.store.connection() as db:
            db.execute("UPDATE acalc_receiver_results SET metadata_json=? WHERE run_id=?",
                       (json.dumps({"geometry_gpkg_hex": "invalid"}), second))
        with self.assertRaisesRegex(ModelError, "geometry"):
            sync_result_layers(self.store)
        self.plugin.dock.results_page.publish_saved_results()
        self.assertIn("could not be refreshed", self.plugin.dock.results_page.summary.text())
        self.assertEqual(self.plugin.dock.results_page.table.rowCount(), 1)
        with self.store.connection(readonly=True) as db:
            self.assertEqual(db.execute(f'SELECT run_id FROM "{records[0]["table_name"]}"').fetchone()[0], first)

    def add_receiver_layer(self, name):
        record = self.store.add_layer('point_receiver', name)
        self.plugin.reload_layers()
        layer = self.plugin.layers[record['layer_id']]
        feature = add_feature(layer, LAYER_TYPES['point_receiver'], name)
        self.plugin.commit_layer(layer)
        return record, str(feature['object_id'])

    def test_two_receiver_layers_in_two_variants_update_independently_and_keep_styles(self):
        other_record, other_id = self.add_receiver_layer('Other receivers')
        other_variant = self.store.add_variant('Night')
        objects = [(self.record, self.object_id), (other_record, other_id)]
        self.save_run(objects=objects)
        unchanged_run = self.save_run(other_variant, objects=objects)
        self.plugin.dock.results_page.publish_saved_results()
        expected = {(variant, record['layer_id']) for variant in (self.variant, other_variant)
                    for record in (self.record, other_record)}
        self.assertEqual(set(self.plugin.result_layers), expected)
        originals = dict(self.plugin.result_layers)
        for key, layer in originals.items():
            layer.setOpacity(0.35)
            self.assertEqual({result['layer_id'] for result in layer.getFeatures()}, {key[1]})
        updated = self.save_run(levels=[25] * 8, objects=objects)
        self.plugin.dock.results_page.publish_saved_results()
        for key, layer in originals.items():
            self.assertIs(self.plugin.result_layers[key], layer)
            self.assertEqual(layer.opacity(), 0.35)
            result = next(layer.getFeatures())
            self.assertEqual(result['run_id'], updated if key[0] == self.variant else unchanged_run)
            self.assertEqual(result['lz_63_db'], 25 if key[0] == self.variant else 10)
        self.store.rename_layer(other_record['layer_id'], 'Renamed receivers')
        self.plugin.dock.results_page.publish_saved_results()
        self.assertIn('Other receivers', originals[self.variant, other_record['layer_id']].name())
        self.plugin.unload()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        for key, layer in originals.items():
            self.assertIsNotNone(QgsProject.instance().mapLayer(layer.id()))
            self.assertEqual(layer.opacity(), 0.35)
        self.plugin.close_project()
        self.plugin.open_project(self.path)
        self.plugin.dock.results_page.publish_saved_results()
        self.assertEqual(set(self.plugin.result_layers), expected)

    def test_recalculation_leaves_excluded_result_layers_alone(self):
        other_record, other_id = self.add_receiver_layer('Other receivers')
        other_variant = self.store.add_variant('Night')
        objects = [(self.record, self.object_id), (other_record, other_id)]
        self.save_run(objects=objects)
        self.save_run(other_variant, objects=objects)
        self.plugin.dock.results_page.publish_saved_results()
        removed = self.plugin.result_layers[self.variant, other_record['layer_id']].id()
        kept = self.plugin.result_layers[other_variant, other_record['layer_id']]
        self.save_run()  # The replacement run evaluates only the first layer.
        self.plugin.dock.results_page.publish_saved_results()
        self.assertIsNotNone(QgsProject.instance().mapLayer(removed))
        self.assertEqual(QgsProject.instance().mapLayer(removed).featureCount(), 1)
        self.assertIs(self.plugin.result_layers[other_variant, other_record['layer_id']], kept)
        with self.store.connection(readonly=True) as db:
            table = result_table_name(self.variant, other_record['layer_id'])
            self.assertIsNotNone(db.execute('SELECT 1 FROM gpkg_contents WHERE table_name=?', (table,)).fetchone())
            self.assertIsNotNone(db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone())
        self.plugin.close_project()
        self.plugin.open_project(self.path)
        self.assertEqual(len(self.plugin.result_layers), 3)

    def test_empty_layer_membership_comes_from_saved_run_after_variant_changes(self):
        empty = self.store.add_layer('point_receiver', 'Empty receivers')
        run_id = self.save_run()
        snapshot = [{'layer_id': record['layer_id'], 'name': record['name'], 'feature_type': 'point_receiver'}
                    for record in (self.record, empty)]
        with self.store.connection() as db:
            db.execute('UPDATE acalc_calculation_runs SET metadata_json=? WHERE run_id=?',
                       (json.dumps({'receiver_layers': snapshot}), run_id))
        self.store.update_metadata('Changed since calculation', '', '', '', {})
        self.plugin.dock.results_page.publish_saved_results()
        result = self.plugin.result_layers[self.variant, empty['layer_id']]
        self.assertTrue(result.isValid())
        self.assertEqual(result.featureCount(), 0)
        self.assertEqual(result.customProperty('acalc/run_id'), run_id)
        self.assertNotIn('out of date', result.name())
        self.assertEqual(result.crs(), self.receiver.crs())

    def make_legacy_projection(self, records, run_id, version):
        """Recreate the previous combined-table layout, including cached positions."""
        table = 'acalc_results_' + self.variant.replace('-', '')
        with self.store.connection() as db:
            first = records[0]['table_name']
            db.execute(f'CREATE TABLE "{table}" AS SELECT * FROM "{first}"')
            for index, record in enumerate(records[1:], 1):
                columns = [row['name'] for row in db.execute(f'PRAGMA table_info("{first}")') if row['name'] != 'fid']
                names = ','.join('"' + name + '"' for name in columns)
                db.execute(f'INSERT INTO "{table}" SELECT fid+?,{names} FROM "{record["table_name"]}"', (index,))
            description = json.dumps({'acalc_result_layer': version, 'variant_id': self.variant, 'run_id': run_id})
            db.execute('''INSERT INTO gpkg_contents
                SELECT ?,data_type,?, ?,last_change,min_x,min_y,max_x,max_y,srs_id FROM gpkg_contents WHERE table_name=?''',
                       (table, table, description, first))
            db.execute('''INSERT INTO gpkg_geometry_columns
                SELECT ?,column_name,geometry_type_name,srs_id,z,m FROM gpkg_geometry_columns WHERE table_name=?''', (table, first))
            for record in records:
                db.execute(f'DROP TABLE "{record["table_name"]}"')
                db.execute('DELETE FROM gpkg_geometry_columns WHERE table_name=?', (record['table_name'],))
                db.execute('DELETE FROM gpkg_contents WHERE table_name=?', (record['table_name'],))
            if version == 1:
                db.execute(f'ALTER TABLE "{table}" RENAME COLUMN out_of_date TO historical')
        return table

    def test_legacy_combined_layers_split_preserving_recovered_positions(self):
        other_record, other_id = self.add_receiver_layer('Other receivers')
        for version in (1, 2):
            with self.subTest(version=version):
                run = self.save_run(snapshot=False, objects=[(self.record, self.object_id), (other_record, other_id)])
                records = sync_result_layers(self.store)
                # Release existing result providers before constructing the old fixture.
                self.plugin.close_project()
                legacy = self.make_legacy_projection(records, run, version)
                self.store.update_metadata(f'Changed after legacy run {version}', '', '', '', {})
                layer = QgsVectorLayer(f'{self.path}|layername={legacy}', 'Old combined results', 'ogr')
                self.assertTrue(layer.isValid())
                old_id = layer.id()
                QgsProject.instance().addMapLayer(layer)
                self.plugin.open_project(self.path)
                self.assertIs(QgsProject.instance().mapLayer(old_id), layer)
                self.plugin.dock.results_page.publish_saved_results()
                self.assertEqual(len(self.plugin.result_layers), 2)
                for key, result_layer in self.plugin.result_layers.items():
                    self.assertNotIn('historical', result_layer.fields().names())
                    result = next(result_layer.getFeatures())
                    self.assertEqual(result['layer_id'], key[1])
                    self.assertEqual(result['geometry_source'], 'matching model revision')
                    self.assertFalse(result.geometry().isNull())
                    self.assertEqual(result['out_of_date'], 1)
                with self.store.connection(readonly=True) as db:
                    self.assertIsNotNone(db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (legacy,)).fetchone())

    def test_migration_failure_keeps_previous_combined_projection(self):
        other_record, other_id = self.add_receiver_layer('Other receivers')
        run = self.save_run(objects=[(self.record, self.object_id), (other_record, other_id)])
        records = sync_result_layers(self.store)
        self.plugin.close_project()
        legacy = self.make_legacy_projection(records, run, 2)
        with self.store.connection() as db:
            db.execute('UPDATE acalc_receiver_results SET metadata_json=? WHERE layer_id=?',
                       (json.dumps({'geometry_gpkg_hex': 'invalid'}), other_record['layer_id']))
        with self.assertRaisesRegex(ModelError, 'geometry'):
            sync_result_layers(self.store)
        with self.store.connection(readonly=True) as db:
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM "{legacy}"').fetchone()[0], 2)
            self.assertTrue(db.execute('SELECT 1 FROM gpkg_contents WHERE table_name=?', (legacy,)).fetchone())
            for record in records:
                self.assertIsNone(db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (record['table_name'],)).fetchone())

    def test_unrelated_dataset_with_result_table_name_is_not_overwritten(self):
        self.save_run()
        table = result_table_name(self.variant, self.record['layer_id'])
        with self.store.connection() as db:
            db.execute(f'CREATE TABLE "{table}" (value TEXT)')
            db.execute(f'INSERT INTO "{table}" VALUES (?)', ('unrelated data',))
        with self.assertRaisesRegex(ModelError, 'another dataset'):
            sync_result_layers(self.store)
        with self.store.connection(readonly=True) as db:
            self.assertEqual(db.execute(f'SELECT value FROM "{table}"').fetchone()[0], 'unrelated data')
