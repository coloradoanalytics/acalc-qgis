# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Properties entry points with real Qt mouse events and QGIS spatial identify."""

from unittest.mock import patch

from qgis.PyQt.QtCore import QEvent, QPoint, Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog, QMenu, QMessageBox
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRectangle, QgsVectorLayer
from qgis.gui import QgsGui, QgsMapLayerAction, QgsMapLayerActionContext, QgsMapMouseEvent

from acalc.dialogs import FeatureDialog
from acalc.layers import load_layer
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError
from acalc.spectra import spectrum_definition
from tests.test_model import APP, FakeIface, ModelFixture, add_feature, rows_for


class PropertiesTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()

    def tearDown(self):
        if self.plugin.dock:
            self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def object_layer(self, key="point_source"):
        record = self.store.add_layer(key, "Objects")
        self.plugin.open_project(self.path)
        layer = self.plugin.layers[record["layer_id"]]
        add_feature(layer, LAYER_TYPES[key], "Original")
        add_feature(layer, LAYER_TYPES[key], "Other")
        self.plugin.commit_layer(layer)
        self.plugin.dock.show_view(key)
        return layer, list(layer.getFeatures())

    def test_properties_delete_confirmation_and_undo(self):
        layer, (original, other) = self.object_layer()

        def delete(dialog):
            dialog.name.setText("Unapplied name")
            with patch.object(QMessageBox, "question", return_value=QMessageBox.No) as question:
                QTest.mouseClick(dialog.delete_button, Qt.LeftButton)
                self.assertIn("Original", question.call_args.args[2])
                self.assertIs(question.call_args.args[0], dialog)
                self.assertFalse(layer.isEditable())
                self.assertEqual(layer.featureCount(), 2)
            with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), patch.object(dialog, "reject", wraps=dialog.reject) as reject:
                QTest.mouseClick(dialog.delete_button, Qt.LeftButton)
                reject.assert_called_once()
            return dialog.result()

        with patch.object(FeatureDialog, "exec", delete):
            self.plugin.open_feature_properties(layer.id(), original.id())
        self.assertFalse(layer.getFeature(original.id()).isValid())
        self.assertTrue(layer.getFeature(other.id()).isValid())
        layer.undoStack().undo()
        self.assertEqual(layer.getFeature(original.id())["name"], "Original")

    def test_properties_duplicate_exists_before_editor_and_cancel_keeps_copy(self):
        layer, (original, other) = self.object_layer("building")
        opened = []

        def duplicate_or_cancel(dialog):
            opened.append(dialog.name.text())
            if len(opened) == 1:
                dialog.name.setText("Unapplied name")
                QTest.mouseClick(dialog.duplicate_button, Qt.LeftButton)
            else:
                self.assertEqual(layer.featureCount(), 3)
                copy = next(layer.getSelectedFeatures())
                self.assertNotEqual(copy["object_id"], original["object_id"])
                self.assertNotEqual(copy.id(), original.id())
                self.assertEqual(copy.geometry().asWkb(), original.geometry().asWkb())
                for name in ("notes", "enabled", "height_mode", "height_value"):
                    self.assertEqual(copy[name], original[name])
                dialog.reject()
            return dialog.result()

        with patch.object(FeatureDialog, "exec", duplicate_or_cancel):
            self.plugin.open_feature_properties(layer.id(), original.id())
        self.assertEqual(opened, ["Original", "Original copy"])
        self.assertEqual(layer.getFeature(original.id())["name"], "Original")
        self.assertEqual(layer.featureCount(), 3)
        layer.undoStack().undo()
        self.assertEqual(layer.featureCount(), 2)
        layer.undoStack().redo()
        self.plugin.commit_layer(layer)
        copies = [f for f in layer.getFeatures() if f["name"] == "Original copy"]
        self.assertEqual(len(copies), 1)
        self.assertNotEqual(copies[0]["fid"], original["fid"])

    def test_go_to_is_available_and_applies_edits_for_every_object_type(self):
        for key in LAYER_TYPES:
            with self.subTest(kind=key):
                layer, (original, _) = self.object_layer(key)

                def navigate(dialog):
                    self.assertEqual(dialog.go_to_button.text(), "Go to " + LAYER_TYPES[key].singular)
                    dialog.name.setText("Located object")
                    QTest.mouseClick(dialog.go_to_button, Qt.LeftButton)
                    self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                    return dialog.result()

                with patch.object(FeatureDialog, "exec", navigate), patch.object(self.iface.canvas, "zoomToSelected") as zoom:
                    self.plugin.open_feature_properties(layer.id(), original.id())
                zoom.assert_called_once_with(layer)
                self.assertEqual(layer.selectedFeatureIds(), [original.id()])
                self.assertEqual(layer.getFeature(original.id())["name"], "Located object")

    def test_browsing_applies_edits_preserves_tab_and_cancel_discards_current_changes(self):
        for key in LAYER_TYPES:
            with self.subTest(kind=key):
                layer, (original, other) = self.object_layer(key)
                opened = []
                selected_tab = []

                def browse(dialog):
                    opened.append(dialog.name.text())
                    if len(opened) == 1:
                        self.assertFalse(dialog.previous_button.isEnabled())
                        self.assertTrue(dialog.next_button.isEnabled())
                        dialog.tabs.setCurrentIndex(min(1, dialog.tabs.count() - 1))
                        selected_tab.append(dialog.tabs.currentIndex())
                        dialog.name.setText("Edited first")
                        QTest.mouseClick(dialog.next_button, Qt.LeftButton)
                    elif len(opened) == 2:
                        self.assertEqual(dialog.tabs.currentIndex(), selected_tab[0])
                        self.assertTrue(dialog.previous_button.isEnabled())
                        self.assertFalse(dialog.next_button.isEnabled())
                        self.assertEqual(layer.selectedFeatureIds(), [other.id()])
                        self.assertEqual(layer.getFeature(original.id())["name"], "Edited first")
                        dialog.name.setText("Edited second")
                        QTest.mouseClick(dialog.previous_button, Qt.LeftButton)
                    else:
                        self.assertEqual(layer.selectedFeatureIds(), [original.id()])
                        dialog.name.setText("Canceled change")
                        dialog.reject()
                    return dialog.result()

                with patch.object(FeatureDialog, "exec", browse):
                    self.plugin.open_feature_properties(layer.id(), original.id())
                self.assertEqual(opened, ["Original", "Other", "Edited first"])
                self.assertEqual(layer.getFeature(original.id())["name"], "Edited first")
                self.assertEqual(layer.getFeature(other.id())["name"], "Edited second")
                self.assertTrue(layer.isModified())
                self.assertEqual(self.iface.activeLayer(), layer)

    def test_browsing_includes_unsaved_objects_with_negative_feature_ids(self):
        layer, _ = self.object_layer()
        pending = add_feature(layer, LAYER_TYPES["point_source"], "Pending")
        self.assertLess(pending.id(), 0)
        objects = list(layer.getFeatures())
        expected = [item["object_id"] for item in objects]
        opened = []

        def browse(dialog):
            current = next(layer.getSelectedFeatures())
            opened.append(current["object_id"])
            if dialog.next_button.isEnabled():
                QTest.mouseClick(dialog.next_button, Qt.LeftButton)
            else:
                dialog.reject()
            return dialog.result()

        with patch.object(FeatureDialog, "exec", browse):
            self.plugin.open_feature_properties(layer.id(), objects[0].id())
        self.assertEqual(opened, expected)
        self.assertTrue(layer.getFeature(pending.id()).isValid())

    def test_browsing_invalid_edits_keeps_current_object_open(self):
        layer, (original, _) = self.object_layer()

        def browse(dialog):
            dialog.show()
            dialog.name.clear()
            QTest.mouseClick(dialog.next_button, Qt.LeftButton)
            self.assertIsNone(dialog.navigation_target)
            self.assertTrue(dialog.isVisible())
            self.assertTrue(dialog.error.text())
            dialog.reject()
            return dialog.result()

        with patch.object(FeatureDialog, "exec", browse):
            self.plugin.open_feature_properties(layer.id(), original.id())
        self.assertFalse(layer.isModified())

    def test_go_to_building_applies_edits_closes_and_zooms_in_canvas_crs(self):
        layer, (original, other) = self.object_layer("building")
        canvas = self.iface.canvas
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        canvas.setExtent(QgsRectangle(-20000000, -10000000, 20000000, 10000000))
        before_width = canvas.extent().width()
        layer.selectByIds([other.id()])

        def navigate(dialog):
            dialog.show()
            dialog.name.setText("Located building")
            QTest.mouseClick(dialog.go_to_building_button, Qt.LeftButton)
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            self.assertFalse(dialog.isVisible())
            return dialog.result()

        with patch.object(FeatureDialog, "exec", navigate):
            self.plugin.open_feature_properties(layer.id(), original.id())
        self.assertEqual(layer.selectedFeatureIds(), [original.id()])
        self.assertEqual(self.iface.activeLayer(), layer)
        self.assertEqual(layer.getFeature(original.id())["name"], "Located building")
        self.assertTrue(layer.isModified())
        target = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(),
                                        QgsProject.instance()).transformBoundingBox(original.geometry().boundingBox())
        self.assertTrue(canvas.extent().contains(target))
        self.assertLess(canvas.extent().width(), before_width / 100)

    def test_go_to_building_keeps_invalid_edits_open(self):
        layer, (original, _) = self.object_layer("building")

        def navigate(dialog):
            dialog.show()
            dialog.name.clear()
            QTest.mouseClick(dialog.go_to_building_button, Qt.LeftButton)
            self.assertTrue(dialog.isVisible())
            self.assertFalse(dialog.go_to_building)
            self.assertTrue(dialog.error.text())
            dialog.reject()
            return dialog.result()

        with patch.object(FeatureDialog, "exec", navigate):
            self.plugin.open_feature_properties(layer.id(), original.id())
        self.assertFalse(layer.isModified())

    def test_list_menu_targets_clicked_object_and_ignores_empty_space(self):
        layer, (original, other) = self.object_layer()
        table = self.plugin.dock.features
        self.iface.window.show()
        APP.processEvents()
        layer.selectByIds([other.id()])
        row = next(row for row in range(table.rowCount()) if table.item(row, 0).text() == "Original")
        position = table.visualItemRect(table.item(row, 0)).center()

        def choose(label):
            def execute(menu, position):
                actions = {action.text(): action for action in menu.actions()}
                self.assertTrue({"Delete", "Duplicate"} <= actions.keys())
                actions[label].trigger()
            return execute

        with patch.object(QMenu, "exec", choose("Delete")), patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            table.customContextMenuRequested.emit(position)
        self.assertEqual(layer.featureCount(), 2)
        with patch.object(QMenu, "exec", choose("Duplicate")), patch.object(FeatureDialog, "exec", return_value=QDialog.Rejected):
            table.customContextMenuRequested.emit(position)
        self.assertEqual(layer.featureCount(), 3)
        self.assertEqual(next(layer.getSelectedFeatures())["name"], "Original copy")
        row = next(row for row in range(table.rowCount()) if table.item(row, 0).text() == "Original")
        position = table.visualItemRect(table.item(row, 0)).center()
        with patch.object(QMenu, "exec", choose("Delete")), patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            table.customContextMenuRequested.emit(position)
        self.assertFalse(layer.getFeature(original.id()).isValid())
        self.assertTrue(layer.getFeature(other.id()).isValid())
        with patch.object(QMenu, "exec") as menu:
            table.customContextMenuRequested.emit(QPoint(5, table.viewport().height() - 5))
            menu.assert_not_called()

    def test_copy_all_object_types_and_pending_edits(self):
        records = [self.store.add_layer(kind.key, kind.title) for kind in LAYER_TYPES.values()]
        spectrum_id = self.store.add_spectrum("Shared power", spectrum_definition("sound_power", [70] * 8, "Synthetic test data"))
        self.plugin.open_project(self.path)
        for record in records:
            with self.subTest(kind=record["feature_type"]):
                layer = self.plugin.layers[record["layer_id"]]
                kind = LAYER_TYPES[record["feature_type"]]
                original = add_feature(layer, kind)
                layer.changeAttributeValue(original.id(), layer.fields().indexFromName("notes"), "Pending notes")
                layer.changeAttributeValue(original.id(), layer.fields().indexFromName("enabled"), 0)
                if kind.spectrum:
                    layer.changeAttributeValue(original.id(), layer.fields().indexFromName("spectrum_id"), spectrum_id)
                copy = self.plugin.copy_feature(layer.id(), original.id())
                self.assertEqual(copy.geometry().asWkb(), original.geometry().asWkb())
                self.assertEqual(copy["notes"], "Pending notes")
                self.assertEqual(copy["enabled"], 0)
                self.assertNotEqual(copy["object_id"], original["object_id"])
                if kind.spectrum:
                    self.assertEqual(copy["spectrum_id"], spectrum_id)
                self.plugin.commit_layer(layer)
                self.assertEqual(layer.featureCount(), 2)

    def test_move_menu_filters_layers_and_moves_clicked_object(self):
        target_record = self.store.add_layer("point_source", "Destination")
        self.store.add_layer("point_receiver", "Wrong point type")
        layer, (original, other) = self.object_layer()
        target = self.plugin.layers[target_record["layer_id"]]
        self.plugin.dock.select_layer(layer)
        table = self.plugin.dock.features
        self.iface.window.show()
        APP.processEvents()
        layer.selectByIds([other.id()])
        row = next(row for row in range(table.rowCount()) if table.item(row, 0).text() == "Original")
        position = table.visualItemRect(table.item(row, 0)).center()

        def execute(menu, position):
            actions = {action.text(): action for action in menu.actions()}
            self.assertEqual(set(actions), {"Properties…", "Duplicate", "Move to layer", "Delete"})
            move = actions["Move to layer"].menu()
            self.assertEqual([action.text() for action in move.actions()], ["Destination"])
            move.actions()[0].trigger()

        with patch.object(QMenu, "exec", execute):
            table.customContextMenuRequested.emit(position)
        self.assertFalse(layer.getFeature(original.id()).isValid())
        self.assertTrue(layer.getFeature(other.id()).isValid())
        self.assertEqual(next(target.getSelectedFeatures())["object_id"], original["object_id"])
        self.assertIs(self.iface.activeLayer(), target)
        self.assertIs(self.plugin.dock.current_layer, target)

    def test_move_menu_disabled_without_another_layer_of_same_type(self):
        self.store.add_layer("point_receiver", "Wrong point type")
        layer, _ = self.object_layer()
        table = self.plugin.dock.features
        shown = []

        def execute(menu, position):
            shown.append(menu)
            action = next(action for action in menu.actions() if action.text() == "Move to layer")
            self.assertFalse(action.isEnabled())

        with patch.object(QMenu, "exec", execute):
            self.plugin.dock.feature_context_menu(table.visualItemRect(table.item(0, 0)).center())
        self.assertEqual(len(shown), 1)

    def test_move_preserves_all_types_pending_edits_identity_and_geometry(self):
        pairs = [(kind, self.store.add_layer(kind.key, "From " + kind.title),
                  self.store.add_layer(kind.key, "To " + kind.title)) for kind in LAYER_TYPES.values()]
        spectrum_id = self.store.add_spectrum("Shared power", spectrum_definition("sound_power", [70] * 8))
        self.plugin.open_project(self.path)
        for kind, source_record, target_record in pairs:
            with self.subTest(kind=kind.key):
                source = self.plugin.layers[source_record["layer_id"]]
                target = self.plugin.layers[target_record["layer_id"]]
                original = add_feature(source, kind)
                source.changeAttributeValue(original.id(), source.fields().indexFromName("notes"), "Pending notes")
                if kind.spectrum:
                    source.changeAttributeValue(original.id(), source.fields().indexFromName("spectrum_id"), spectrum_id)
                add_feature(target, kind, "Existing destination object")
                self.plugin.commit_layer(target)
                moved = self.plugin.move_feature(source.id(), original.id(), target_record["layer_id"])
                self.assertEqual(source.featureCount(), 0)
                self.assertEqual(target.featureCount(), 2)
                self.assertEqual(moved["object_id"], original["object_id"])
                self.assertEqual(moved["name"], original["name"])
                self.assertEqual(moved["notes"], "Pending notes")
                self.assertEqual(moved.geometry().asWkb(), original.geometry().asWkb())
                if kind.spectrum:
                    self.assertEqual(moved["spectrum_id"], spectrum_id)
                source.undoStack().undo()
                target.undoStack().undo()
                self.assertEqual(source.featureCount(), 1)
                self.assertEqual(target.featureCount(), 1)
                source.undoStack().redo()
                target.undoStack().redo()
                self.plugin.commit_layer(target)
                self.plugin.commit_layer(source)
                reloaded_source = load_layer(self.store, source_record)
                reloaded_target = load_layer(self.store, target_record)
                self.assertEqual(reloaded_source.featureCount(), 0)
                saved = next(f for f in reloaded_target.getFeatures() if f["object_id"] == original["object_id"])
                self.assertEqual(saved.geometry().asWkb(), original.geometry().asWkb())
                self.assertEqual(saved["notes"], "Pending notes")
                self.assertEqual(reloaded_target.featureCount(), 2)

    def test_move_rejects_invalid_destinations_and_rolls_back_failure(self):
        target_record = self.store.add_layer("point_source", "Destination")
        wrong_record = self.store.add_layer("point_receiver", "Wrong type")
        source, (original, _) = self.object_layer()
        target = self.plugin.layers[target_record["layer_id"]]
        for destination_id in (wrong_record["layer_id"], source.customProperty("acalc/layer_id"), "missing"):
            with self.assertRaisesRegex(ModelError, "same type"):
                self.plugin.move_feature(source.id(), original.id(), destination_id)
        self.assertFalse(source.isEditable())
        for layer, operation in ((target, "addFeature"), (source, "deleteFeature")):
            with self.subTest(operation=operation), patch.object(layer, operation, return_value=False):
                with self.assertRaises(ModelError):
                    self.plugin.move_feature(source.id(), original.id(), target_record["layer_id"])
            self.assertEqual(source.featureCount(), 2)
            self.assertEqual(target.featureCount(), 0)
            self.assertEqual(source.getFeature(original.id())["object_id"], original["object_id"])
            self.assertFalse(source.isModified())
            self.assertFalse(target.isModified())

    def test_new_feature_dialog_has_no_object_actions(self):
        layer, _ = self.object_layer()

        def cancel(dialog):
            self.assertFalse(hasattr(dialog, "delete_button"))
            self.assertFalse(hasattr(dialog, "duplicate_button"))
            return QDialog.Rejected

        with patch.object(FeatureDialog, "exec", cancel):
            self.plugin.edit_feature(create=True, layer=layer)

    def test_mouse_double_click_opens_clicked_row_and_cancel_preserves_data(self):
        record = self.store.add_layer("point_source", "Plant")
        self.plugin.open_project(self.path)
        layer = self.plugin.layers[record["layer_id"]]
        add_feature(layer, LAYER_TYPES["point_source"], "Pump A")
        add_feature(layer, LAYER_TYPES["point_source"], "Pump B")
        self.plugin.commit_layer(layer)
        self.plugin.dock.show_view("point_source")
        self.iface.window.show()
        APP.processEvents()
        features = list(layer.getFeatures())
        layer.selectByIds([feature.id() for feature in features])
        APP.processEvents()
        table = self.plugin.dock.features
        self.assertFalse(hasattr(self.plugin.dock, "vertex_preview"))
        row = next(row for row in range(table.rowCount()) if table.item(row, 0).text() == "Pump B")
        item = table.item(row, 0)
        position = table.visualItemRect(item).center()
        opened = []

        def cancel(dialog):
            self.assertTrue(dialog.isModal())
            self.assertIn("Acalc properties", dialog.windowTitle())
            self.assertEqual([dialog.tabs.tabText(i) for i in range(dialog.tabs.count())], ["Coordinates", "Spectrum", "Directionality"])
            opened.append(dialog.name.text())
            self.assertEqual(float(dialog.table.item(0, 3).text()), 1.5)
            dialog.name.setText("Do not save this")
            return QDialog.Rejected

        with patch.object(FeatureDialog, "exec", cancel):
            QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, position)
            APP.processEvents()  # Selection signals must not reset the table model.
            QTest.mouseDClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, position)
        self.assertEqual(opened, ["Pump B"])
        self.assertEqual(layer.getFeature(features[1].id())["name"], "Pump B")
        self.assertFalse(layer.isEditable())

    def map_event(self, layer, x, y):
        canvas = self.iface.mapCanvas()
        transform = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
        point = transform.transform(QgsPointXY(x, y))
        pixel = canvas.getCoordinateTransform().transform(point)
        return QgsMapMouseEvent(canvas, QEvent.MouseButtonPress, QPoint(round(pixel.x()), round(pixel.y())), Qt.RightButton)

    def prepare_canvas(self, layers):
        canvas = self.iface.mapCanvas()
        self.iface.window.show()
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        transform = QgsCoordinateTransform(layers[0].crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
        point = transform.transform(QgsPointXY(500000.125, 4400000.5))
        canvas.setLayers(layers)
        canvas.setExtent(QgsRectangle(point.x() - 80, point.y() - 80, point.x() + 80, point.y() + 80))
        APP.processEvents()
        return canvas

    def test_canvas_menu_identifies_overlaps_across_crs_and_edits_exact_feature(self):
        records = [self.store.add_layer(key, name) for key, name in (("point_source", "Sources"), ("point_receiver", "Receivers"), ("point_source", "Hidden sources"))]
        self.plugin.open_project(self.path)
        layers = [self.plugin.layers[record["layer_id"]] for record in records]
        for layer, record in zip(layers, records):
            add_feature(layer, LAYER_TYPES[record["feature_type"]], record["name"] + " object")
            self.plugin.commit_layer(layer)
        unrelated = QgsVectorLayer("Point?crs=EPSG:26913", "Reference", "memory")
        QgsProject.instance().addMapLayer(unrelated)
        self.iface.setActiveLayer(unrelated)
        canvas = self.prepare_canvas(layers[:2] + [unrelated])
        menu = QMenu()
        event = self.map_event(layers[0], 500000.125, 4400000.5)
        canvas.contextMenuAboutToShow.emit(menu, event)
        entry = next(action for action in menu.actions() if action.text() == "Acalc properties")
        self.assertIsNotNone(entry.menu())
        choices = entry.menu().actions()
        self.assertEqual(len(choices), 2)
        self.assertIs(self.iface.activeLayer(), unrelated)
        self.assertFalse(any(layer.isEditable() for layer in layers))
        receiver_action = next(action for action in choices if "Receivers object" in action.text())

        def edit(dialog):
            self.assertEqual(dialog.name.text(), "Receivers object")
            self.assertEqual(dialog.tabs.count(), 2)  # Coordinates and receiver results.
            dialog.name.setText("Receiver updated")
            dialog.table.item(0, 3).setText("4.25")
            dialog.validate()
            return dialog.result()

        with patch.object(FeatureDialog, "exec", edit):
            receiver_action.trigger()
        receiver = layers[1]
        self.assertIs(self.iface.activeLayer(), receiver)
        self.assertTrue(receiver.isModified())
        self.assertFalse(layers[0].isModified())
        feature = next(receiver.getFeatures())
        self.assertEqual(feature["name"], "Receiver updated")
        self.assertEqual(next(feature.geometry().vertices()).m(), 4.25)
        receiver.undoStack().undo()
        self.assertEqual(next(receiver.getFeatures())["name"], "Receivers object")
        self.plugin.commit_layer(receiver)
        empty_menu = QMenu()
        canvas.contextMenuAboutToShow.emit(empty_menu, self.map_event(layers[0], 500060, 4400060))
        self.assertFalse(empty_menu.actions())
        canvas.setLayers([unrelated])
        only_reference = QMenu()
        canvas.contextMenuAboutToShow.emit(only_reference, event)
        self.assertFalse(only_reference.actions())

    def test_single_hit_and_native_feature_action_are_removed_on_close(self):
        record = self.store.add_layer("barrier", "Walls")
        self.plugin.open_project(self.path)
        layer = self.plugin.layers[record["layer_id"]]
        add_feature(layer, LAYER_TYPES["barrier"], "Wall")
        self.plugin.commit_layer(layer)
        feature = next(layer.getFeatures())
        canvas = self.prepare_canvas([layer])
        menu = QMenu()
        canvas.contextMenuAboutToShow.emit(menu, self.map_event(layer, 500000.125, 4400000.5))
        action = next(action for action in menu.actions() if action.text() == "Acalc properties")
        self.assertIsNone(action.menu())
        native = self.plugin.properties_actions.layer_actions[layer.id()]
        registry = QgsGui.mapLayerActionRegistry()
        self.assertIn(native, registry.mapLayerActions(layer, QgsMapLayerAction.SingleFeature))
        with patch.object(self.plugin, "open_feature_properties") as properties:
            native.triggerForFeature(layer, feature, QgsMapLayerActionContext())
            properties.assert_called_once_with(layer.id(), feature.id())
        self.plugin.close_project()
        self.assertFalse(self.plugin.properties_actions.layer_actions)
        self.assertNotIn(native, registry.mapLayerActions(None, QgsMapLayerAction.SingleFeature))
