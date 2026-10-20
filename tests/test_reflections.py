# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Reflection settings, nullable material overrides and real QGIS authoring."""

import json
from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog
from qgis.core import QgsVectorLayer

from acalc.calculation_settings import ISO_ENGINE, REFLECTION_DEFAULTS, default_settings, validate_settings
from acalc.calculation_ui import ISOSettingsEditor
from acalc.dialogs import FeatureDialog
from acalc.layers import configure_layer, initialize_added_object_fields, load_layer, validate_layer
from acalc.object_geometry import feature_value
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError, SCHEMA_VERSION, new_id
from acalc.storage import ModelStore
from tests.test_calculation_storage import downgrade_to_legacy
from tests.test_model import APP, FakeIface, ModelFixture, add_feature, rows_for


class ReflectionSettingsTests(ModelFixture):
    def test_clear_reflection_results_are_stale_and_retained_settings_load_in_ui(self):
        settings = {**default_settings(), "reflection_order": 2,
                    "building_reflection_coefficient": 0.65, "barrier_reflection_coefficient": 0}
        self.store.save_calculation(settings)
        receiver = self.store.add_layer("point_receiver", "Receivers")
        layer = load_layer(self.store, receiver)
        add_feature(layer, LAYER_TYPES["point_receiver"], "Receiver")
        self.assertTrue(layer.commitChanges())
        feature = next(layer.getFeatures())
        object_id = feature["object_id"]
        del feature, layer
        variant_id = self.store.variants()[0]["variant_id"]
        project = self.store.metadata()
        run_id = new_id()
        with self.store.connection() as db:
            db.execute("""INSERT INTO acalc_calculation_runs VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,'[]','{}')""",
                (run_id, project["project_id"], variant_id, "Default", ISO_ENGINE, "0.12.0", "ISO 9613-2:2024",
                 "2026-09-23", "2026-09-23", "f" * 64, project["modified_at"], json.dumps(settings), 1, 1, 0.125))
            db.execute("INSERT INTO acalc_receiver_results VALUES (?,?,?,?,?,?,?,'{}')",
                       (run_id, receiver["layer_id"], object_id, "Receiver", json.dumps([40] * 8), 49, 47))
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            plugin.dock.show_view("results")
            page = plugin.dock.results_page
            self.assertIn("OUT OF DATE", page.summary.text())
            self.assertIn("calculation engine changed", page.summary.text())
            self.assertEqual(page.table.rowCount(), 1)
            self.assertEqual(plugin.dock.calculation_page.settings(), settings)
            self.assertEqual(self.store.metadata()["modified_at"], project["modified_at"])
            self.assertEqual(self.store.calculation()["settings"], settings)
            stored = self.store.latest_calculation_run(variant_id)
            self.assertEqual(stored["settings"], settings)
            self.assertEqual(stored["run_id"], run_id)
            self.assertTrue(stored["stale"])
        finally:
            plugin.unload()
            iface.window.close()
            APP.processEvents()

    def test_defaults_strict_validation_and_legacy_normalization(self):
        current = default_settings()
        self.assertEqual({key: current[key] for key in REFLECTION_DEFAULTS}, {
            "reflection_order": 3, "building_reflection_coefficient": 0.9,
            "barrier_reflection_coefficient": 0.9})
        # Only version-1 settings predate the reflection controls.
        legacy = {key: value for key, value in current.items() if key not in REFLECTION_DEFAULTS} | {"schema_version": 1}
        self.assertEqual(validate_settings(legacy), current)
        self.assertFalse(set(legacy) & set(REFLECTION_DEFAULTS))
        for key in REFLECTION_DEFAULTS:
            bad_values = ((-1, 6, 1.5, 3.0, True, None, "3") if key == "reflection_order"
                          else (-0.01, 1.01, float("nan"), float("inf"), 10 ** 400, True, None, "0.5"))
            for bad in bad_values:
                with self.subTest(key=key, bad=bad), self.assertRaises(ModelError):
                    validate_settings({**current, key: bad})
            for good in (0, 1):
                self.assertEqual(validate_settings({**current, key: good})[key], good)
        for bad in ({**legacy, "reflection_depth": 3}, {key: value for key, value in legacy.items() if key != "ground_factor"}):
            with self.assertRaises(ModelError):
                validate_settings(bad)
        self.store.save_calculation(legacy)
        with self.store.connection(readonly=True) as db:
            self.assertEqual(json.loads(db.execute("SELECT settings_json FROM acalc_calculation").fetchone()[0]), current)

    def test_shared_settings_and_preserved_legacy_presets_normalize_without_rewriting(self):
        settings = {key: value for key, value in default_settings().items() if key not in REFLECTION_DEFAULTS} | {"schema_version": 1}
        hot = {**settings, "temperature_c": 30}
        raw = json.dumps(settings)
        presets = json.dumps([{"name": "Hot", "settings": hot}])
        with self.store.connection() as db:
            db.execute("UPDATE acalc_calculation SET settings_json=?,legacy_settings_json=?", (raw, presets))
        stamp = self.store.metadata()["modified_at"]
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.calculation()["settings"], default_settings())
        self.assertEqual(reopened.calculation()["legacy_settings"][0]["settings"], {**default_settings(), "temperature_c": 30})
        self.assertEqual(reopened.metadata()["modified_at"], stamp)
        with reopened.connection(readonly=True) as db:
            self.assertEqual(tuple(db.execute("SELECT settings_json,legacy_settings_json FROM acalc_calculation").fetchone()), (raw, presets))

    def test_pre_shared_calculation_migration_normalizes_old_settings(self):
        with self.store.connection() as db:
            downgrade_to_legacy(db, 4)
            row = db.execute("SELECT configuration_id,definition_json FROM acalc_configurations").fetchone()
            value = json.loads(row["definition_json"])
            for key in REFLECTION_DEFAULTS:
                value["settings"].pop(key)
            value["settings"]["schema_version"] = 1
            db.execute("UPDATE acalc_configurations SET definition_json=?", (json.dumps(value),))
        upgraded = ModelStore.open(self.path)
        self.assertEqual(upgraded.calculation()["settings"], default_settings())
        self.assertEqual(upgraded.metadata()["schema_version"], SCHEMA_VERSION)

    def test_general_editor_controls_persist_and_discard(self):
        from qgis.PyQt.QtCore import QSettings
        from acalc.api_connections import SETTINGS_KEY, engine_identity
        from tests.test_calculation_ui import add_api

        preferences = QSettings()
        previous = preferences.value(SETTINGS_KEY)
        preferences.remove(SETTINGS_KEY)
        self.addCleanup(lambda: preferences.remove(SETTINGS_KEY) if previous is None
                        else preferences.setValue(SETTINGS_KEY, previous))
        engine_id = engine_identity(add_api("Reflection settings API"))
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            page = plugin.dock.calculation_page
            page.calculate_button.click()
            dialog = plugin.calculation_dialog
            editor = dialog.panels[engine_id]
            self.assertEqual((editor.fields["reflection_order"].minimum(), editor.fields["reflection_order"].maximum()), (0, 5))
            for key, value in (("reflection_order", 2), ("building_reflection_coefficient", 0.35), ("barrier_reflection_coefficient", 1)):
                editor.fields[key].setValue(value)
            self.store.save_calculation(editor.settings())
            saved = ModelStore.open(self.path).calculation()["settings"]
            self.assertEqual([saved[key] for key in REFLECTION_DEFAULTS], [2, 0.65, 0])
            editor.fields["reflection_order"].setValue(0)
            dialog.close()
            plugin.close_calculation_dialog()
            page.calculate_button.click()
            self.assertEqual(plugin.calculation_dialog.panels[engine_id].fields["reflection_order"].value(), 2)
            panel = ISOSettingsEditor()
            self.addCleanup(panel.close)
            panel.set_settings({key: value for key, value in saved.items() if key not in REFLECTION_DEFAULTS} | {"schema_version": 1})
            self.assertEqual(panel.settings(), default_settings())
            self.assertEqual(panel.fields["building_reflection_coefficient"].value(), 0.1)
            # Display conversion must preserve exact saved values, including
            # coefficients with more precision than the absorption editor.
            precise = {**saved, "building_reflection_coefficient": 0.123456789,
                       "barrier_reflection_coefficient": 0.1}
            panel.set_settings(precise)
            self.assertEqual(panel.settings(), precise)
            panel.fields["building_reflection_coefficient"].setValue(0)
            panel.fields["barrier_reflection_coefficient"].setValue(1)
            self.assertEqual(panel.settings()["building_reflection_coefficient"], 1)
            self.assertEqual(panel.settings()["barrier_reflection_coefficient"], 0)
        finally:
            plugin.unload()
            iface.window.close()
            APP.processEvents()


class ReflectionObjectTests(ModelFixture):
    def test_upgrade_preserves_live_pending_additions_and_initializes_null_override(self):
        for key in ("building", "barrier"):
            kind = LAYER_TYPES[key]
            record = self.store.add_layer(key, key)
            with self.store.connection() as db:
                db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN reflection_coefficient')
            layer = QgsVectorLayer(f'{self.path}|layername={record["table_name"]}', key, "ogr")
            added = add_feature(layer, kind)
            object_id = added["object_id"]
            geometry = bytes(added.geometry().asWkb())
            undo_count = layer.undoStack().count()
            reopened = ModelStore.open(self.path)
            configure_layer(layer, reopened, record)
            self.assertTrue(layer.isEditable())
            self.assertEqual(layer.undoStack().count(), undo_count)
            remapped = initialize_added_object_fields(layer, kind)
            self.assertIn(added.id(), remapped)
            feature = layer.getFeature(remapped[added.id()])
            self.assertEqual((feature["object_id"], bytes(feature.geometry().asWkb())), (object_id, geometry))
            self.assertIsNone(feature_value(feature, "reflection_coefficient"))
            layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("reflection_coefficient"), 0.25)
            self.assertTrue(layer.commitChanges(), layer.commitErrors())
            saved = next(load_layer(ModelStore.open(self.path), record).getFeatures())
            self.assertEqual(saved["reflection_coefficient"], 0.25)
            self.assertEqual(saved["object_id"], object_id)

    def test_nullable_columns_upgrade_without_changing_geometry_or_schema(self):
        for key in ("building", "barrier"):
            kind = LAYER_TYPES[key]
            record = self.store.add_layer(key, kind.title)
            layer = load_layer(self.store, record)
            add_feature(layer, kind)
            self.assertTrue(layer.commitChanges())
            feature = next(layer.getFeatures())
            original = bytes(feature.geometry().asWkb()), feature["object_id"]
            del feature, layer
            with self.store.connection() as db:
                db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN reflection_coefficient')
            stamp = self.store.metadata()["modified_at"]
            reopened = ModelStore.open(self.path)
            self.assertEqual(reopened.metadata()["schema_version"], SCHEMA_VERSION)
            self.assertEqual(reopened.metadata()["modified_at"], stamp)
            layer = load_layer(reopened, record)
            feature = next(layer.getFeatures())
            self.assertEqual((bytes(feature.geometry().asWkb()), feature["object_id"]), original)
            self.assertIsNone(feature_value(feature, "reflection_coefficient"))
            with reopened.connection(readonly=True) as db:
                column = next(row for row in db.execute(f'PRAGMA table_info("{record["table_name"]}")') if row["name"] == "reflection_coefficient")
                self.assertEqual((column["type"], column["notnull"], column["dflt_value"]), ("REAL", 0, None))
            self.assertEqual(validate_layer(layer, kind), [])
            ModelStore.open(self.path)  # Reopening is idempotent.

    def test_object_defaults_overrides_zero_cancel_undo_and_roundtrip(self):
        records = [self.store.add_layer(key, key) for key in ("building", "barrier")]
        settings = default_settings()
        settings.update(building_reflection_coefficient=0.7, barrier_reflection_coefficient=0.4)
        self.store.save_calculation(settings)
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            for record in records:
                kind = LAYER_TYPES[record["feature_type"]]
                layer = plugin.layers[record["layer_id"]]
                expected_default = round(1 - settings[f"{kind.key}_reflection_coefficient"], 4)

                def create(dialog):
                    self.assertTrue(dialog.use_default_reflection.isChecked())
                    self.assertFalse(dialog.absorption_coefficient.isEnabled())
                    self.assertEqual(dialog.absorption_coefficient.value(), expected_default)
                    self.assertIn(f"{expected_default:g}", dialog.use_default_reflection.text())
                    dialog.table.setRowCount(0)
                    for row in rows_for(kind):
                        dialog.add_values(row)
                    dialog.validate()
                    self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                    return dialog.result()

                with patch.object(FeatureDialog, "exec", create):
                    self.assertTrue(plugin.edit_feature(create=True, layer=layer))
                feature = next(layer.getFeatures())
                fid = feature.id()
                self.assertIsNone(feature_value(feature, "reflection_coefficient"))

                def edit(dialog):
                    dialog.use_default_reflection.setChecked(False)
                    self.assertTrue(dialog.absorption_coefficient.isEnabled())
                    dialog.absorption_coefficient.setValue(1)
                    dialog.validate()
                    return dialog.result()

                with patch.object(FeatureDialog, "exec", edit):
                    self.assertTrue(plugin.edit_feature(layer=layer, feature_id=fid))
                self.assertEqual(layer.getFeature(fid)["reflection_coefficient"], 0)
                layer.undoStack().undo()
                self.assertIsNone(feature_value(layer.getFeature(fid), "reflection_coefficient"))
                layer.undoStack().redo()
                self.assertEqual(layer.getFeature(fid)["reflection_coefficient"], 0)

                def cancel(dialog):
                    self.assertFalse(dialog.use_default_reflection.isChecked())
                    self.assertEqual(dialog.absorption_coefficient.value(), 1)
                    dialog.use_default_reflection.setChecked(True)
                    self.assertEqual(dialog.absorption_coefficient.value(), expected_default)
                    self.assertFalse(dialog.absorption_coefficient.isEnabled())
                    dialog.use_default_reflection.setChecked(False)
                    self.assertEqual(dialog.absorption_coefficient.value(), 1)
                    dialog.use_default_reflection.setChecked(True)
                    return QDialog.Rejected

                with patch.object(FeatureDialog, "exec", cancel):
                    self.assertFalse(plugin.edit_feature(layer=layer, feature_id=fid))
                self.assertEqual(layer.getFeature(fid)["reflection_coefficient"], 0)
                plugin.commit_layer(layer)
                reopened = load_layer(ModelStore.open(self.path), record)
                saved = next(reopened.getFeatures())
                self.assertEqual(saved["reflection_coefficient"], 0)
                self.assertEqual(validate_layer(reopened, kind), [])

                def inherit(dialog):
                    dialog.use_default_reflection.setChecked(True)
                    dialog.validate()
                    return dialog.result()

                with patch.object(FeatureDialog, "exec", inherit):
                    self.assertTrue(plugin.edit_feature(layer=layer, feature_id=next(layer.getFeatures()).id()))
                self.assertIsNone(feature_value(next(layer.getFeatures()), "reflection_coefficient"))
                plugin.commit_layer(layer)
        finally:
            plugin.unload()
            iface.window.close()
            APP.processEvents()

    def test_object_validation_rejects_malformed_coefficients(self):
        for key in ("building", "barrier"):
            kind = LAYER_TYPES[key]
            record = self.store.add_layer(key, key)
            layer = load_layer(self.store, record)
            feature = add_feature(layer, kind)
            index = layer.fields().indexFromName("reflection_coefficient")
            for value in (-0.1, 1.1, float("inf"), float("nan"), "invalid", True):
                with self.subTest(kind=key, value=value):
                    layer.changeAttributeValue(feature.id(), index, value)
                    self.assertIn("Reflection coefficient", validate_layer(layer, kind)[0])
            for value in (None, 0, 0.375, 1):
                layer.changeAttributeValue(feature.id(), index, value)
                self.assertEqual(validate_layer(layer, kind), [])
            layer.rollBack()
